//! The check at the end of a tick (`WorldState.validate_after_tick`) in one call, for the tick where
//! everything `TickValidationMemo.begin` compares says "as at the last passed check".
//!
//! It holds no error text. It returns only if every rule the Python check would run passes, and
//! anything else says `Unsupported` before the memo changes: `validate_after_tick` then runs
//! `begin`, `validate` and `commit` as if this weren't here, so they pass or raise their own text,
//! and a passed Python check is what makes the next `Plan`. So what a yes means is the one thing
//! that must hold (`benchmarks/rule_shadow/shadow_after_tick.py`,
//! `tests/integration/test_after_tick_kernel.py`). Since every rule passes, their order can't show.
//!
//! The memo stays the Python object, because the Python check reads and writes it too and the
//! scans during a tick read it. What the configs say comes from `Plan`, which Python makes from what
//! it has parsed. The rows' validators are called from `_RUST_VALIDATORS`, and what has no kernel yet
//! is the Python rule called from here, so a lane that registers a validator is used at once.
//!
//! **The entity rules read the objects, not the rows**: see `tick_validation.rs`, whose kernel this
//! calls, and docs/rust-port/lanes/m6-validate.md.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use pyo3::exceptions::PyException;
use pyo3::ffi;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFrozenSet, PyList, PyString, PyTuple};
use rustc_hash::FxHashSet;

use crate::core::row::{attribute, exact_dict, exact_int, exact_str, unsupported};
use crate::core::tracked::{as_readable_dict, as_readable_list};
use crate::core::world_core::{core_of, raw_dict_get, NamespaceMirror};
use crate::rules::tick_validation::entity_rules_watching;
use crate::state::entities::entities_of;
use crate::Unsupported;

const PRESENCE: &str = "population_presence_v1";
const LIFECYCLE: &str = "population_lifecycle_v1";
const DYNAMICS: &str = "population_dynamics_v1";
const OVERLAY: &str = "geometry_overlay_v1";
const BODY: &str = "body_state_v1";
const SHARED: &str = "shared_operations_v1";
const QUEUES: &str = "facility_queues_v1";
const QUEUES_LEGACY: &str = "facility_queues_legacy_mode_v1";
const SPACES: &str = "temporary_spaces_v1";
const OPERATIONAL: &str = "operational_events_v1";
const INFRASTRUCTURE: &str = "infrastructure_v1";
const ENVIRONMENT: &str = "zone_environment_v1";
const SERVICE: &str = "resource_service_v1";
const LEDGER: &str = "resource_ledger_v1";
const INFORMATION: &str = "information_spread_v1";
const PSYCHOLOGY: &str = "psychological_distress_v1";
const ENVIRONMENT_EVENTS: &str = "effect_program_environment";

/// What Python gives once, in `domain/simulator_state.py`.
struct Table {
    /// `(holder, name, as defined)` of what the Python check would call. A patch of one, or the
    /// `python_rules` mark, leaves the tick to the Python check.
    watched: Vec<(Py<PyAny>, Py<PyString>, Py<PyAny>)>,
    /// `_RUST_VALIDATORS`.
    validators: Py<PyDict>,
    rows_valid: Py<PyAny>,
    tick_valid: Py<PyAny>,
    effects_valid: Py<PyAny>,
    lifecycle_rule: Py<PyAny>,
    overlay_rule: Py<PyAny>,
    queues_rule: Py<PyAny>,
    spaces_rule: Py<PyAny>,
    initial_needs: Py<PyAny>,
    needs: Py<PyAny>,
}

static TABLE: GILOnceCell<Table> = GILOnceCell::new();
static RAN: AtomicU64 = AtomicU64::new(0);
static REFUSALS: Mutex<BTreeMap<String, u64>> = Mutex::new(BTreeMap::new());

/// What the rules read from `model_configs`, from a check that passed on these configs. A new one
/// is made when the configs change. Rows and the plan never hold each other.
#[pyclass(frozen, module = "sheltersim_native.rules.after_tick")]
struct Plan {
    declared: Vec<(Py<PyString>, i64)>,
    required: Vec<Py<PyString>>,
    independent_queues: bool,
    information_enabled: bool,
    mean_reverting: bool,
    shared_configured: bool,
    operational_enabled: bool,
    environment_configured: bool,
    operational_events: FxHashSet<String>,
    infrastructure_events: FxHashSet<String>,
    resource_config: Py<PyAny>,
}

#[pymethods]
impl Plan {
    #[new]
    #[pyo3(signature = (*, declared, required, independent_queues, information_enabled, mean_reverting, shared_configured, operational_enabled, environment_configured, operational_events, infrastructure_events, resource_config))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        declared: Vec<(Py<PyString>, i64)>,
        required: Vec<Py<PyString>>,
        independent_queues: bool,
        information_enabled: bool,
        mean_reverting: bool,
        shared_configured: bool,
        operational_enabled: bool,
        environment_configured: bool,
        operational_events: &Bound<'_, PyFrozenSet>,
        infrastructure_events: &Bound<'_, PyFrozenSet>,
        resource_config: Py<PyAny>,
    ) -> PyResult<Self> {
        let texts = |items: &Bound<'_, PyFrozenSet>| -> PyResult<FxHashSet<String>> {
            items.iter().map(|item| item.extract::<String>()).collect()
        };
        Ok(Self {
            declared,
            required,
            independent_queues,
            information_enabled,
            mean_reverting,
            shared_configured,
            operational_enabled,
            environment_configured,
            operational_events: texts(operational_events)?,
            infrastructure_events: texts(infrastructure_events)?,
            resource_config,
        })
    }
}

/// How a namespace stands against what `begin` kept in `_seen`.
enum Seen<'py> {
    Same,
    Changed(Bound<'py, PyAny>),
}

struct Check<'a, 'py> {
    world: &'a Bound<'py, PyAny>,
    model_state: &'a Bound<'py, PyDict>,
    mirrors: &'a Bound<'py, PyAny>,
    validators: &'a Bound<'py, PyDict>,
}

fn as_mirror<'a, 'py>(value: &'a Bound<'py, PyAny>) -> PyResult<&'a Bound<'py, NamespaceMirror>> {
    Ok(value.downcast::<NamespaceMirror>()?)
}

impl<'py> Check<'_, 'py> {
    fn py(&self) -> Python<'py> {
        self.world.py()
    }

    /// `model_state.get(name)`, where a stored None reads as absent, as the rules read it.
    fn state(&self, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
        Ok(self.model_state.get_item(name)?.filter(|state| !state.is_none()))
    }

    /// `_rows_are_what_the_world_holds`, past its check of the entities, made once in `check`.
    fn rows_hold(&self, name: &Bound<'py, PyString>) -> PyResult<bool> {
        let Some(mirror) = raw_dict_get(self.mirrors, name.as_any())? else {
            return Ok(true);
        };
        let mirror = as_mirror(&mirror)?;
        match raw_dict_get(self.model_state.as_any(), name.as_any())? {
            None => Ok(mirror.call_method0(intern!(self.py(), "row_json"))?.is_none()),
            Some(value) => Ok(mirror.try_borrow()?.root_is(&value)),
        }
    }

    /// `rust_accepts`.
    fn accepts(&self, name: &str, kernel: &Bound<'py, PyAny>) -> PyResult<bool> {
        let name = PyString::intern(self.py(), name);
        if !self.rows_hold(&name)? {
            return Ok(false);
        }
        match kernel.call1((self.world,)) {
            Ok(answer) => Ok(answer.is_exact_instance_of::<PyBool>() && answer.is_truthy()?),
            Err(error) if error.is_instance_of::<Unsupported>(self.py()) => Ok(false),
            Err(error) => Err(error),
        }
    }

    /// `rust_validated`, or None if no kernel is registered.
    fn validated(&self, name: &str) -> PyResult<Option<bool>> {
        let Some(kernel) = self.validators.get_item(name)? else {
            return Ok(None);
        };
        self.accepts(name, &kernel).map(Some)
    }

    /// Whether a kernel took the namespace, so what stands in for its rule below can be skipped.
    /// A false says nothing about the state, and the Python check reads it.
    fn taken_by_kernel(&self, name: &'static str) -> PyResult<bool> {
        match self.validated(name)? {
            Some(true) => Ok(true),
            Some(false) => Err(Unsupported::new_err(format!("{name}: its kernel did not take it"))),
            None => Ok(false),
        }
    }

    /// The namespace's validator must say yes; its Python rule stays the way to say no.
    fn must_validate(&self, name: &'static str) -> PyResult<()> {
        if self.validated(name)? == Some(true) {
            return Ok(());
        }
        Err(Unsupported::new_err(format!("{name}: its kernel did not take it")))
    }

    /// As `must_validate`, or the Python rule where no kernel is registered.
    fn validate_or_rule(&self, name: &'static str, rule: &Py<PyAny>) -> PyResult<()> {
        match self.validated(name)? {
            Some(true) => Ok(()),
            Some(false) => Err(Unsupported::new_err(format!("{name}: its kernel did not take it"))),
            None => rule.bind(self.py()).call1((self.world,)).map(|_| ()),
        }
    }

    fn seen(&self, memo_seen: &Bound<'py, PyAny>, name: &'static str) -> PyResult<Seen<'py>> {
        let py = self.py();
        let name = PyString::intern(py, name);
        let before = raw_dict_get(memo_seen, name.as_any())?;
        let Some(mirror) = raw_dict_get(self.mirrors, name.as_any())? else {
            return unsupported("a namespace the memo compares has no mirror");
        };
        let Some(value) = raw_dict_get(self.model_state.as_any(), name.as_any())? else {
            // `saved_json(None)`, which `_state_seen` keeps for an absent namespace.
            let null = PyBytes::new(py, b"null");
            let same = before
                .as_ref()
                .is_some_and(|before| before.downcast::<PyBytes>().is_ok_and(|bytes| bytes.as_bytes() == b"null"));
            return Ok(if same { Seen::Same } else { Seen::Changed(null.into_any()) });
        };
        let generation = as_mirror(&mirror)?.call_method1(intern!(py, "generation_of"), (&value,))?;
        if generation.is_none() {
            return unsupported("a namespace the memo compares holds another tree");
        }
        if let Some(before) = before.as_ref().and_then(|before| before.downcast::<PyTuple>().ok()) {
            if before.get_item(0)?.is(&value) && before.get_item(1)?.eq(&generation)? {
                return Ok(Seen::Same);
            }
        }
        Ok(Seen::Changed(PyTuple::new(py, [value, generation])?.into_any()))
    }

    /// The history rule of `validate_body_state`, for the records since the last tick, past the
    /// rows' own check and the config's.
    fn body_history(&self, body: &Bound<'py, PyAny>, minute: i64, agents: &Bound<'py, PyDict>) -> PyResult<()> {
        let py = self.py();
        let Some(state) = as_readable_dict(body) else {
            return unsupported("the body state is another kind of dict");
        };
        let Some(history) = state.get_item(intern!(py, "history"))? else {
            return unsupported("the body state has no history");
        };
        let Some(history) = as_readable_list(&history) else {
            return unsupported("the body history is another kind of list");
        };
        // records_since_previous_tick
        let mut start = history.len();
        while start > 0 {
            let item = history.get_item(start - 1)?;
            let recorded = as_readable_dict(&item).map(|item| item.get_item(intern!(py, "minute"))).transpose()?.flatten();
            if let Some(recorded) = recorded {
                if recorded.is_exact_instance_of::<pyo3::types::PyInt>() && exact_int(&recorded)? < minute - 1 {
                    break;
                }
            }
            start -= 1;
        }
        let first = start.saturating_sub(1);
        for position in first..history.len() {
            let item = history.get_item(position)?;
            let Some(item) = as_readable_dict(&item) else {
                return unsupported("a body history item is the Python rule's");
            };
            let read = |name: &Bound<'py, PyString>| -> PyResult<Bound<'py, PyAny>> {
                item.get_item(name)?.ok_or_else(|| Unsupported::new_err("a body history item lacks a field"))
            };
            exact_str(&read(intern!(py, "event_id"))?)?;
            exact_str(&read(intern!(py, "kind"))?)?;
            read(intern!(py, "topic_id"))?;
            read(intern!(py, "causation_id"))?;
            let actors = read(intern!(py, "actor_ids"))?;
            let deltas = read(intern!(py, "deltas"))?;
            if item.len() != 7
                || exact_int(&read(intern!(py, "minute"))?)? < 0
                || as_readable_dict(&deltas).is_none()
            {
                return unsupported("a body history item is the Python rule's");
            }
            let Some(actors) = as_readable_list(&actors) else {
                return unsupported("a body history item's actors are the Python rule's");
            };
            for actor in actors.iter() {
                exact_str(&actor)?;
                // Only the people of the world: someone else is in the sets the rule builds, which
                // this doesn't, and the Python check builds them.
                if !agents.contains(&actor)? {
                    return unsupported("a body history actor is no longer here");
                }
            }
        }
        Ok(())
    }

    /// `_has_exact_schema`.
    fn environment_schema(&self, state: Option<&Bound<'py, PyAny>>) -> PyResult<()> {
        let py = self.py();
        let Some(state) = state.and_then(as_readable_dict) else {
            return unsupported("the zone environment is the Python rule's");
        };
        let version = state.get_item(intern!(py, "schema_version"))?;
        let modifiers = state.get_item(intern!(py, "modifiers"))?;
        if state.len() != 2
            || exact_int(&version.ok_or_else(|| Unsupported::new_err("no schema version"))?)? != 1
            || modifiers.as_ref().and_then(as_readable_dict).is_none()
        {
            return unsupported("the zone environment is the Python rule's");
        }
        Ok(())
    }

    /// The first half of `validate_operational_events_state`, which is all of it at a tick. The
    /// events are read by `watch`.
    fn operational_schema(&self, state: &Bound<'py, PyAny>) -> PyResult<()> {
        let py = self.py();
        let Some(state) = as_readable_dict(state) else {
            return unsupported("the operational events are the Python rule's");
        };
        let is_dict = |name: &Bound<'py, PyString>| -> PyResult<bool> {
            Ok(state.get_item(name)?.as_ref().and_then(as_readable_dict).is_some())
        };
        let list = state.get_item(intern!(py, "cancelled_staff_task_ids"))?;
        let version = state.get_item(intern!(py, "schema_version"))?;
        if state.len() != 5
            || exact_int(&version.ok_or_else(|| Unsupported::new_err("no schema version"))?)? != 1
            || !is_dict(intern!(py, "shipments"))?
            || !is_dict(intern!(py, "water_outages"))?
            || !is_dict(intern!(py, "staff_status"))?
            || list.as_ref().and_then(as_readable_list).is_none()
        {
            return unsupported("the operational events are the Python rule's");
        }
        Ok(())
    }
}

/// `{key: item.zone_id for key, item in facilities.items()} == kept`.
fn same_facility_zones(facilities: &Bound<'_, PyDict>, kept: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = facilities.py();
    let kept = exact_dict(kept)?;
    if kept.len() != facilities.len() {
        return Ok(false);
    }
    for (key, facility) in facilities.iter() {
        let Some(zone) = raw_dict_get(kept.as_any(), &key)? else {
            return Ok(false);
        };
        let now = attribute(&facility, intern!(py, "zone_id"))?;
        if !now.is(&zone) && !now.eq(&zone)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// `map.keys() == kept`, for a frozenset.
fn same_keys(map: &Bound<'_, PyDict>, kept: &Bound<'_, PyAny>) -> PyResult<bool> {
    let kept = kept.downcast_exact::<PyFrozenSet>()?;
    if kept.len() != map.len() {
        return Ok(false);
    }
    for (key, _) in map.iter() {
        if !kept.contains(&key)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// `_is_settled`, of a status that is a str.
fn is_settled(request: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = request.py();
    let status = attribute(request, intern!(py, "status"))?;
    let status = status.downcast::<PyString>()?;
    let status = status.to_str()?;
    Ok(status == "resolved" || status == "unresolved")
}

/// `object.__setattr__(request, "_write_listener", listener)`: `report_writes_to`.
fn report_writes_to(request: &Bound<'_, PyAny>, listener: &Bound<'_, PyAny>) -> PyResult<()> {
    let name = intern!(request.py(), "_write_listener");
    // SAFETY: all three are live objects; the function is in the stable ABI and sets an error on failure.
    if unsafe { ffi::PyObject_GenericSetAttr(request.as_ptr(), name.as_ptr(), listener.as_ptr()) } != 0 {
        return Err(PyErr::fetch(request.py()));
    }
    Ok(())
}

fn check<'py>(table: &Table, world: &Bound<'py, PyAny>, memo: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
    let py = world.py();
    for (holder, name, defined) in &table.watched {
        if !holder.bind(py).getattr(name.bind(py))?.is(defined.bind(py)) {
            return unsupported("a rule of the check is patched");
        }
    }
    let plan = attribute(memo, intern!(py, "steady"))?;
    if plan.is_none() {
        return unsupported("the configs have no plan");
    }
    let plan = plan.downcast_exact::<Plan>()?.get();

    // ---- What `_rows_are_what_the_world_holds` asks of the whole world, for every namespace below.
    let world_state = attribute(world, intern!(py, "__dict__"))?;
    let core = core_of(world)?;
    let store = entities_of(world)?;
    if !store.call_method1(intern!(py, "follows"), (&world_state,))?.is_truthy()? {
        return unsupported("the maps of the world are not the ones the rows follow");
    }
    let Some(model_state) = raw_dict_get(&world_state, intern!(py, "model_state"))? else {
        return unsupported("the world has no model state");
    };
    if model_state.get_type().as_ptr() != crate::core::tracked::namespace_table_type().cast() {
        return unsupported("the model state is not the table, or a tree is detached");
    }
    let model_state = model_state.downcast::<PyDict>()?;
    let mirrors = attribute(model_state, intern!(py, "_mirrors"))?;
    let validators = table.validators.bind(py);
    let cx = Check { world, model_state, mirrors: &mirrors, validators };
    if exact_int(&attribute(world, intern!(py, "schema_version"))?)? != 6 {
        return unsupported("not a WorldState v6");
    }

    // ---- `begin`: all it compares.
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let stamp = attribute(memo, intern!(py, "_configs_stamp"))?;
    let stamp = stamp.downcast_exact::<PyTuple>().map_err(|_| Unsupported::new_err("the configs had no stamp"))?;
    let config_mirror = core.call_method1(intern!(py, "mirror"), ("model_configs",))?;
    if config_mirror.is_none()
        || !stamp.get_item(0)?.is(&configs)
        || !as_mirror(&config_mirror)?.call_method1(intern!(py, "generation_of"), (&configs,))?.eq(stamp.get_item(1)?)?
    {
        return unsupported("the configs were written");
    }
    let agents = attribute(world, intern!(py, "agents"))?;
    let agents = agents.downcast::<PyDict>()?;
    let zones = attribute(world, intern!(py, "zones"))?;
    let zones = zones.downcast::<PyDict>()?;
    let facilities = attribute(world, intern!(py, "facilities"))?;
    let facilities = facilities.downcast::<PyDict>()?;
    if !same_keys(agents, &attribute(memo, intern!(py, "_agent_ids"))?)? {
        return unsupported("the people changed");
    }
    if !same_keys(zones, &attribute(memo, intern!(py, "_zone_ids"))?)? {
        return unsupported("the zones changed");
    }
    if !same_facility_zones(facilities, &attribute(memo, intern!(py, "_facility_zones"))?)? {
        return unsupported("a facility moved or was added");
    }
    let memo_seen = attribute(memo, intern!(py, "_seen"))?;
    if !matches!(cx.seen(&memo_seen, PRESENCE)?, Seen::Same) {
        return unsupported("the presence state changed");
    }
    let presence = cx.state(PRESENCE)?;
    let presence_unchanged = attribute(memo, intern!(py, "_presence_is_self_contained"))?.is_truthy()?;
    let lifecycle_seen = cx.seen(&memo_seen, LIFECYCLE)?;
    let dynamics_seen = cx.seen(&memo_seen, DYNAMICS)?;
    let overlay_seen = cx.seen(&memo_seen, OVERLAY)?;
    let empty_part = |state: &Option<Bound<'py, PyAny>>, part: &Bound<'py, PyString>| -> PyResult<bool> {
        let Some(state) = state.as_ref().and_then(as_readable_dict) else {
            return Ok(false);
        };
        Ok(!state.get_item(part)?.is_some_and(|value| value.is_truthy().unwrap_or(true)))
    };
    let lifecycle_unchanged = matches!(lifecycle_seen, Seen::Same)
        && (presence.is_none() || presence_unchanged)
        && empty_part(&cx.state(LIFECYCLE)?, intern!(py, "pending_relocations"))?;
    let dynamics_unchanged =
        matches!(dynamics_seen, Seen::Same) && empty_part(&cx.state(DYNAMICS)?, intern!(py, "pending_arrivals"))?;
    let overlay = cx.state(OVERLAY)?;
    let overlay_unchanged = matches!(overlay_seen, Seen::Same)
        && empty_part(&overlay, intern!(py, "temporary_rooms"))?
        && empty_part(&overlay, intern!(py, "route_modifiers"))?;

    // ---- `requests_to_check`, kept aside until the check has passed.
    let support_requests = attribute(world, intern!(py, "support_requests"))?;
    let snapshot = attribute(memo, intern!(py, "_requests"))?;
    let added = snapshot.call_method1(intern!(py, "added_values"), (&support_requests,))?;
    if added.is_none() {
        return unsupported("the requests are not the ones held");
    }
    let added = added.downcast_exact::<PyList>()?;
    let open = attribute(memo, intern!(py, "_open_requests"))?;
    let written = attribute(memo, intern!(py, "_written_requests"))?;
    let (open, written) = (open.downcast_exact::<PyList>()?, written.downcast_exact::<PyList>()?);
    let mut read: FxHashSet<*mut ffi::PyObject> = FxHashSet::default();
    let checked = PyList::empty(py);
    for request in open.iter().chain(written.iter()).chain(added.iter()) {
        if read.insert(request.as_ptr()) {
            checked.append(request)?;
        }
    }

    // ---- `validate_declared_states`.
    for (name, version) in &plan.declared {
        let name = name.bind(py);
        let Some(state) = model_state.get_item(name)? else {
            return unsupported("a declared namespace is missing");
        };
        let Some(state) = as_readable_dict(&state) else {
            return unsupported("a declared namespace is the Python rule's");
        };
        let found = state.get_item(intern!(py, "schema_version"))?;
        if exact_int(&found.ok_or_else(|| Unsupported::new_err("no schema version"))?)? != *version {
            return unsupported("a declared namespace has another version");
        }
        let text = name.to_str()?;
        if text == PRESENCE && presence_unchanged {
            continue;
        }
        if cx.validated(text)? != Some(true) {
            return unsupported("a declared namespace has no kernel that takes it");
        }
    }

    // ---- `validate_simulator_extensions`.
    for name in &plan.required {
        if !model_state.contains(name.bind(py))? {
            return unsupported("a required namespace is missing");
        }
    }
    if model_state.contains(SPACES)? && !plan.required.iter().any(|name| name.bind(py).to_str().is_ok_and(|text| text == SPACES)) {
        return unsupported("temporary spaces without their contract");
    }
    if model_state.contains(QUEUES_LEGACY)? {
        return unsupported("a legacy queue checkpoint");
    }
    cx.must_validate("agent_dynamics_v1")?;
    let body = cx.state(BODY)?;
    if let Some(body) = body.as_ref().filter(|_| !matches!(cx.taken_by_kernel(BODY), Ok(true))) {
        if !cx.accepts(BODY, table.rows_valid.bind(py))? {
            return unsupported("the rows of the body state are the Python rule's");
        }
        let kept = attribute(memo, intern!(py, "body_config_json"))?;
        let Ok(kept) = kept.downcast_exact::<PyBytes>() else {
            return unsupported("the body config has no kept JSON");
        };
        let Some(state) = as_readable_dict(body) else {
            return unsupported("the body state is another kind of dict");
        };
        let Some(config) = state.get_item(intern!(py, "config"))? else {
            return unsupported("the body state has no config");
        };
        if crate::encode(&config)? != kept.as_bytes() {
            return unsupported("the body config changed");
        }
        cx.body_history(body, exact_int(&attribute(world, intern!(py, "minute_index"))?)?, agents)?;
    }
    if !dynamics_unchanged {
        cx.must_validate(DYNAMICS)?;
    }
    if !lifecycle_unchanged {
        cx.validate_or_rule(LIFECYCLE, &table.lifecycle_rule)?;
    }
    cx.must_validate("social_network_v1")?;
    cx.must_validate("staff_dynamics_v1")?;
    cx.must_validate("staff_operations_v1")?;
    cx.validate_or_rule(SPACES, &table.spaces_rule)?;
    cx.must_validate("movement_v1")?;
    if !overlay_unchanged {
        cx.validate_or_rule(OVERLAY, &table.overlay_rule)?;
    }
    if cx.state(QUEUES)?.is_some() || plan.independent_queues {
        cx.validate_or_rule(QUEUES, &table.queues_rule)?;
    }
    if model_state.contains(SHARED)? && !plan.shared_configured {
        return unsupported("shared operations without their contract");
    }
    let shared = cx.state(SHARED)?;
    if shared.is_some() && !cx.taken_by_kernel(SHARED)? && !cx.accepts(SHARED, table.tick_valid.bind(py))? {
        return unsupported("the shared operations are the Python rule's");
    }

    // ---- The rest of `validate`.
    if !model_state.contains(LEDGER)? {
        return unsupported("WorldState v6 without a ledger");
    }
    cx.must_validate(SERVICE)?;
    if plan.information_enabled {
        cx.must_validate(INFORMATION)?;
    } else if model_state.contains(INFORMATION)? {
        return unsupported("information state without its model");
    }
    if plan.mean_reverting {
        cx.must_validate(PSYCHOLOGY)?;
    } else if model_state.contains(PSYCHOLOGY)? {
        return unsupported("psychological state without its model");
    }
    let operational = cx.state(OPERATIONAL)?;
    if !cx.taken_by_kernel(OPERATIONAL)? {
        match &operational {
            Some(state) => cx.operational_schema(state)?,
            None if plan.operational_enabled => return unsupported("operational events without their state"),
            None => {}
        }
    }
    // A Plan has no infrastructure config: the Python rule reads a configured one whole.
    if !cx.taken_by_kernel(INFRASTRUCTURE)? && cx.state(INFRASTRUCTURE)?.is_some() {
        return unsupported("infrastructure state without its model");
    }
    if !cx.taken_by_kernel(ENVIRONMENT)? {
        let environment = cx.state(ENVIRONMENT)?;
        if plan.environment_configured {
            cx.environment_schema(environment.as_ref())?;
        } else if environment.is_some() {
            return unsupported("a zone environment without its config");
        }
    }
    let options = PyDict::new(py);
    options.set_item(intern!(py, "validate_event_history"), false)?;
    table.effects_valid.bind(py).call((world,), Some(&options))?;
    let mut event_rule_broken: Option<&'static str> = None;
    let left = entity_rules_watching(
        world,
        &checked,
        plan.resource_config.bind(py),
        table.initial_needs.bind(py),
        table.needs.bind(py),
        &mut |kind: &str| {
            if plan.infrastructure_events.contains(kind) {
                event_rule_broken = Some("an infrastructure event without the model");
            } else if !plan.environment_configured && kind.starts_with(ENVIRONMENT_EVENTS) {
                event_rule_broken = Some("a zone environment event without the policy");
            } else if operational.is_none() && plan.operational_events.contains(kind) {
                event_rule_broken = Some("an operational event without its state");
            }
        },
    )?;
    if let Some(reason) = event_rule_broken {
        return unsupported(reason);
    }
    cx.must_validate(LEDGER)?;

    // ---- Everything passed: `commit`, and what `begin` keeps. Nothing above wrote to the memo.
    // The facts of this check, as `begin` would have set them.
    for (name, fact) in [
        (intern!(py, "configs_unchanged"), true),
        (intern!(py, "agents_unchanged"), true),
        (intern!(py, "zones_unchanged"), true),
        (intern!(py, "presence_unchanged"), presence_unchanged),
        (intern!(py, "lifecycle_unchanged"), lifecycle_unchanged),
        (intern!(py, "dynamics_unchanged"), dynamics_unchanged),
        (intern!(py, "overlay_unchanged"), overlay_unchanged),
    ] {
        memo.setattr(name, fact)?;
    }
    if shared.is_some() {
        memo.setattr(intern!(py, "_operations_unchanged"), true)?;
    }
    for (name, seen) in [(LIFECYCLE, lifecycle_seen), (DYNAMICS, dynamics_seen), (OVERLAY, overlay_seen)] {
        if let Seen::Changed(now) = seen {
            memo_seen.set_item(name, now)?;
        }
    }
    let mut reopened = false;
    for request in written.iter() {
        if !is_settled(&request)? {
            reopened = true;
            break;
        }
    }
    let still_open = PyList::empty(py);
    let none = py.None().into_bound(py);
    for request in checked.iter() {
        if is_settled(&request)? {
            report_writes_to(&request, written.as_any())?;
        } else {
            report_writes_to(&request, &none)?;
            still_open.append(&request)?;
        }
    }
    let still_open = if reopened {
        let every = PyList::empty(py);
        for (_, request) in support_requests.downcast::<PyDict>()?.iter() {
            if !is_settled(&request)? {
                every.append(request)?;
            }
        }
        every
    } else {
        still_open
    };
    memo.setattr(intern!(py, "_open_requests"), still_open)?;
    snapshot.call_method1(intern!(py, "extend"), (added,))?;
    written.call_method0(intern!(py, "clear"))?;
    memo.setattr(intern!(py, "minute"), attribute(world, intern!(py, "minute_index"))?)?;
    Ok(left)
}

/// The zones the kernel leaves to `_validate_zone_capacity`, with the memo moved on, or Unsupported
/// with the memo as it was. `shadow` is for `benchmarks/rule_shadow/shadow_after_tick.py`, the one
/// caller that runs this while the shadows keep the rest of the tick on the Python path.
#[pyfunction]
#[pyo3(signature = (world, memo, shadow=false))]
fn after_tick<'py>(world: &Bound<'py, PyAny>, memo: &Bound<'py, PyAny>, shadow: bool) -> PyResult<Bound<'py, PyList>> {
    let py = world.py();
    let answer = match TABLE.get(py) {
        None => unsupported("domain/simulator_state.py has not installed the table"),
        Some(_) if !shadow && (crate::rules::tick::path_is_python_only() || crate::rules::dispatch::python_entries()) => {
            unsupported("a benchmark's shadow is installed")
        }
        Some(table) => check(table, world, memo),
    };
    match answer {
        Ok(left) => {
            RAN.fetch_add(1, Ordering::Relaxed);
            Ok(left)
        }
        // A rule that raises, called from here or read here, is the Python check's to raise.
        Err(error) if error.is_instance_of::<PyException>(py) => {
            let reason = error.value(py).to_string();
            if let Ok(mut refusals) = REFUSALS.lock() {
                *refusals.entry(reason.clone()).or_default() += 1;
            }
            Err(Unsupported::new_err(reason))
        }
        Err(error) => Err(error),
    }
}

/// What `domain/simulator_state.py` gives once.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn install(
    py: Python<'_>,
    watched: Vec<(Py<PyAny>, Py<PyString>)>,
    validators: Py<PyDict>,
    rows_valid: Py<PyAny>,
    tick_valid: Py<PyAny>,
    effects_valid: Py<PyAny>,
    rules: (Py<PyAny>, Py<PyAny>, Py<PyAny>, Py<PyAny>),
    needs: (Py<PyAny>, Py<PyAny>),
) -> PyResult<()> {
    let mut defined = Vec::with_capacity(watched.len());
    for (holder, name) in watched {
        let value = holder.bind(py).getattr(name.bind(py))?.unbind();
        defined.push((holder, name, value));
    }
    let (lifecycle_rule, overlay_rule, queues_rule, spaces_rule) = rules;
    TABLE.get_or_init(py, || Table {
        watched: defined,
        validators,
        rows_valid,
        tick_valid,
        effects_valid,
        lifecycle_rule,
        overlay_rule,
        queues_rule,
        spaces_rule,
        initial_needs: needs.0,
        needs: needs.1,
    });
    Ok(())
}

/// What has run in this process: `(checks taken, refusals by reason)`.
#[pyfunction]
fn stats<'py>(py: Python<'py>) -> PyResult<(u64, Bound<'py, PyDict>)> {
    let refusals = PyDict::new(py);
    if let Ok(known) = REFUSALS.lock() {
        for (reason, count) in known.iter() {
            refusals.set_item(reason, count)?;
        }
    }
    Ok((RAN.load(Ordering::Relaxed), refusals))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Plan>()?;
    m.add_function(wrap_pyfunction!(after_tick, m)?)?;
    m.add_function(wrap_pyfunction!(install, m)?)?;
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    Ok(())
}
