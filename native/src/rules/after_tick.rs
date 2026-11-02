//! The check at the end of a tick (`WorldState.validate_after_tick`) in one call, with its memo.
//!
//! It holds no error text. It returns only if every rule of `validate(validate_event_history=False)`
//! passes, and anything else says `Unsupported`: `validate_after_tick` then runs that Python check,
//! with no memo, which passes or raises its own text. So what a yes means is the one thing that must
//! hold (`benchmarks/rule_shadow/shadow_after_tick.py`, `tests/integration/test_after_tick_kernel.py`).
//! Since every rule passes, their order can't show.
//!
//! The memo (`TickMemo`) is the facts of the last check this kernel passed, and lets it skip what is
//! provably unchanged since: see "What the memo skips" below. A check that refuses leaves the memo as
//! it was, but for the plan of the configs, which holds whatever the state is.
//!
//! The configs are read by the contracts in Python (`core/tick_validation.py`, decision D3 of
//! docs/rust-port/step4-plan.md): `tick_plan` when the configs have no plan yet, which is the first
//! tick of a memo and the tick after a write to `model_configs`, and `body_config_ids` when the saved
//! body config changed. Every rule of the state is here or in the kernel of its namespace
//! (`state_mirror._RUST_VALIDATORS`); a namespace with no kernel is refused.
//!
//! **The entity rules read the objects, not the rows**: see `tick_validation.rs`, whose kernel this
//! calls, and docs/rust-port/lanes/t-tick-validation.md. So do the rules of an active staff task.
//!
//! What the memo skips (each rests on one argument; a full validation at each save reads them all):
//! - The configs are parsed only when the stamp of `model_configs` changes: its mirror's generation,
//!   which sees every write through the tree. A world whose configs no mirror follows is refused.
//! - A settled support request (resolved or unresolved) depends only on its own fields, which
//!   `SupportRequestState` reports writing, on the set of Agent IDs, and on the clock only through
//!   "not after the current minute". It is checked again when written or when the Agent IDs change.
//!   The scans of unsettled requests during a tick use the same reports (`unsettled_requests`).
//! - The population presence, lifecycle and dynamics states and the geometry overlay are skipped when
//!   everything their rules read is unchanged: their own state by its mirror's generation, the Agent
//!   and zone IDs, and the facilities' zones for the overlay. A write of the same value counts as a
//!   change. The generation does not see dict.__setitem__ on a node of the tree; the next full
//!   validation reads it, as for the rows' own checks (docs/rust-port/lanes/t-tick-validation.md).
//! - The saved body config is checked again when its JSON or the configs change.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use pyo3::exceptions::PyException;
use pyo3::ffi;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyFrozenSet, PyList, PyString, PyTuple};
use rustc_hash::FxHashSet;

use crate::core::row::{attribute, exact_dict, exact_int, exact_str, unsupported};
use crate::core::tracked::{as_readable_dict, as_readable_list};
use crate::core::world_core::{core_of, raw_dict_get, NamespaceMirror};
use crate::rules::tick_validation::entity_rules_watching;
use crate::state::entities::entities_of;
use crate::validation::ValuesSnapshot;
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
/// The namespaces `begin` compared, in the order of `TickMemo::seen`.
const COMPARED: [&str; 4] = [PRESENCE, LIFECYCLE, DYNAMICS, OVERLAY];

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
    /// `core/tick_validation.py`: the contracts' reading of the configs.
    tick_plan: Py<PyAny>,
    body_config_ids: Py<PyAny>,
    initial_needs: Py<PyAny>,
    needs: Py<PyAny>,
}

static TABLE: GILOnceCell<Table> = GILOnceCell::new();
static RAN: AtomicU64 = AtomicU64::new(0);
static REFUSALS: Mutex<BTreeMap<String, u64>> = Mutex::new(BTreeMap::new());

/// What the rules read from `model_configs`, from the contracts (`tick_plan`). A new one is made
/// when the configs change. Rows and the plan never hold each other.
#[pyclass(frozen, module = "sheltersim_native.rules.after_tick")]
struct Plan {
    declared: Vec<(Py<PyString>, i64)>,
    required: Vec<Py<PyString>>,
    independent_queues: bool,
    information_enabled: bool,
    mean_reverting: bool,
    shared_configured: bool,
    /// None where `operational_events_enabled` raises, which the rule reads only without a state.
    operational_enabled: Option<bool>,
    /// The zones of the zone environment's baselines, or None without its config.
    environment_zones: Option<Vec<String>>,
    infrastructure_configured: bool,
    operational_events: FxHashSet<String>,
    infrastructure_events: FxHashSet<String>,
    resource_config: Py<PyAny>,
}

#[pymethods]
impl Plan {
    #[new]
    #[pyo3(signature = (*, declared, required, independent_queues, information_enabled, mean_reverting, shared_configured, operational_enabled, environment_zones, infrastructure_configured, operational_events, infrastructure_events, resource_config))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        declared: Vec<(Py<PyString>, i64)>,
        required: Vec<Py<PyString>>,
        independent_queues: bool,
        information_enabled: bool,
        mean_reverting: bool,
        shared_configured: bool,
        operational_enabled: Option<bool>,
        environment_zones: Option<Vec<String>>,
        infrastructure_configured: bool,
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
            environment_zones,
            infrastructure_configured,
            operational_events: texts(operational_events)?,
            infrastructure_events: texts(infrastructure_events)?,
            resource_config,
        })
    }
}

/// A namespace as the last passed check saw it.
enum Stamp {
    Absent,
    /// The tree and its mirror's generation.
    Tree(Py<PyAny>, Py<PyAny>),
}

/// The facts of the last check this kernel passed for one world (`world.__dict__["_tick_memo"]`).
/// It holds no simulation state: dropping it costs one slower check and changes no result.
#[pyclass(module = "sheltersim_native.rules.after_tick")]
pub struct TickMemo {
    /// The minute of that check; rules bound past times by the clock, so a memo only holds going
    /// forward, and `validate_after_tick` starts a new one for an earlier minute.
    #[pyo3(get)]
    minute: i64,
    /// `(model_configs, its generation)` the plan was made for.
    configs: Option<(Py<PyAny>, Py<PyAny>)>,
    plan: Option<Py<Plan>>,
    /// `geometry_overlay._CONFIG_SCOPE` across ticks while the configs are unchanged: the parsed
    /// geometry configs (`_parsed_config`, `rules/movement_geometry.rs`).
    #[pyo3(get)]
    geometry_scope: Py<PyDict>,
    agent_ids: Option<FxHashSet<String>>,
    zone_ids: Option<FxHashSet<String>>,
    /// `{key: item.zone_id for key, item in facilities.items()}`.
    facility_zones: Option<Py<PyDict>>,
    seen: [Option<Stamp>; 4],
    presence_is_self_contained: bool,
    /// Support requests: every value of the dict, in order; the ones open; the settled ones written
    /// since (each settled one reports a write to this list).
    requests: ValuesSnapshot,
    open: Py<PyList>,
    written: Py<PyList>,
    /// The saved body config's JSON that passed under these configs, and the people it names.
    body_config: Option<(Vec<u8>, Vec<String>)>,
    /// What the last passed check found unchanged: configs, presence, lifecycle, dynamics, overlay.
    unchanged: [bool; 5],
}

impl TickMemo {
    fn empty(py: Python<'_>) -> Self {
        Self {
            minute: -1,
            configs: None,
            plan: None,
            geometry_scope: PyDict::new(py).unbind(),
            agent_ids: None,
            zone_ids: None,
            facility_zones: None,
            seen: [None, None, None, None],
            presence_is_self_contained: false,
            requests: ValuesSnapshot::default(),
            open: PyList::empty(py).unbind(),
            written: PyList::empty(py).unbind(),
            body_config: None,
            unchanged: [false; 5],
        }
    }
}

#[pymethods]
impl TickMemo {
    #[new]
    fn new(py: Python<'_>) -> Self {
        Self::empty(py)
    }

    /// A copied world holds new objects, so it starts with nothing proven.
    fn __deepcopy__(&self, py: Python<'_>, _memo: &Bound<'_, PyAny>) -> Self {
        Self::empty(py)
    }

    /// What the memo holds, for the tests and `shadow_after_tick.py`: a dict of copies.
    fn facts<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let facts = PyDict::new(py);
        let sorted = |ids: &Option<FxHashSet<String>>| {
            ids.as_ref().map(|ids| {
                let mut ids: Vec<&String> = ids.iter().collect();
                ids.sort();
                ids.into_iter().cloned().collect::<Vec<String>>()
            })
        };
        facts.set_item("minute", self.minute)?;
        facts.set_item("planned", self.plan.is_some())?;
        facts.set_item("agent_ids", sorted(&self.agent_ids))?;
        facts.set_item("zone_ids", sorted(&self.zone_ids))?;
        facts.set_item("facility_zones", self.facility_zones.as_ref().map(|zones| zones.bind(py).copy()).transpose()?)?;
        facts.set_item("requests", self.requests.values(py)?)?;
        facts.set_item("open", self.open.bind(py).get_slice(0, usize::MAX))?;
        facts.set_item("written", self.written.bind(py))?;
        facts.set_item("body_config_ids", self.body_config.as_ref().map(|(_, ids)| ids.clone()))?;
        facts.set_item("seen", self.seen.iter().map(|stamp| stamp.is_some()).collect::<Vec<bool>>())?;
        facts.set_item("presence_is_self_contained", self.presence_is_self_contained)?;
        let unchanged = PyDict::new(py);
        for (name, fact) in ["configs", "presence", "lifecycle", "dynamics", "overlay"].iter().zip(self.unchanged) {
            unchanged.set_item(name, fact)?;
        }
        facts.set_item("unchanged", unchanged)?;
        Ok(facts)
    }

    /// The requests of the dict that may not be settled, in its order, or None if that is not known
    /// and the caller has to read them all. For scans during a tick (`unsettled_support_requests`).
    ///
    /// What was settled at the last check reports any write, so this needs only the requests open
    /// then, the ones added since, and that no settled one was reopened. The result may hold
    /// requests settled since; the caller reads their status.
    fn unsettled_requests<'py>(&self, requests: &Bound<'py, PyDict>) -> PyResult<Option<Bound<'py, PyList>>> {
        let py = requests.py();
        let Some(added) = self.requests.added_values(requests)? else {
            return Ok(None);
        };
        for request in self.written.bind(py).iter() {
            if !is_settled(&request)? {
                return Ok(None);
            }
        }
        let result = PyList::empty(py);
        for request in self.open.bind(py).iter().chain(added.iter()) {
            result.append(request)?;
        }
        Ok(Some(result))
    }
}

/// How a namespace stands against the memo.
enum Seen {
    Same,
    Changed(Stamp),
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

    /// The namespace's validator must say yes; its Python rule stays the way to say no.
    fn must_validate(&self, name: &'static str) -> PyResult<()> {
        let Some(kernel) = self.validators.get_item(name)? else {
            return Err(Unsupported::new_err(format!("{name}: no kernel is registered")));
        };
        if self.accepts(name, &kernel)? {
            return Ok(());
        }
        Err(Unsupported::new_err(format!("{name}: its kernel did not take it")))
    }

    fn seen(&self, kept: Option<&Stamp>, name: &'static str) -> PyResult<Seen> {
        let py = self.py();
        let key = PyString::intern(py, name);
        let Some(mirror) = raw_dict_get(self.mirrors, key.as_any())? else {
            return unsupported("a namespace the memo compares has no mirror");
        };
        let Some(value) = raw_dict_get(self.model_state.as_any(), key.as_any())? else {
            return Ok(if matches!(kept, Some(Stamp::Absent)) { Seen::Same } else { Seen::Changed(Stamp::Absent) });
        };
        let generation = as_mirror(&mirror)?.call_method1(intern!(py, "generation_of"), (&value,))?;
        if generation.is_none() {
            return unsupported("a namespace the memo compares holds another tree");
        }
        if let Some(Stamp::Tree(before, before_generation)) = kept {
            if before.bind(py).is(&value) && before_generation.bind(py).eq(&generation)? {
                return Ok(Seen::Same);
            }
        }
        Ok(Seen::Changed(Stamp::Tree(value.unbind(), generation.unbind())))
    }

    /// The IDs a body record or config may name besides the people here: the population state's
    /// `initial_agent_ids` and `profile_traits`, and its departures for the history.
    fn historical_ids(&self, with_departures: bool) -> PyResult<FxHashSet<String>> {
        let py = self.py();
        let mut ids = FxHashSet::default();
        let Some(population) = self.state(DYNAMICS)? else {
            return Ok(ids);
        };
        let Some(population) = as_readable_dict(&population) else {
            return Ok(ids);
        };
        if let Some(initial) = population.get_item(intern!(py, "initial_agent_ids"))? {
            let Some(initial) = as_readable_list(&initial) else {
                return unsupported("the initial agents are not a list");
            };
            for id in initial.iter() {
                ids.insert(exact_str(&id)?.to_owned());
            }
        }
        if let Some(traits) = population.get_item(intern!(py, "profile_traits"))? {
            for (id, _) in exact_dict(&traits)?.iter() {
                ids.insert(exact_str(&id)?.to_owned());
            }
        }
        if with_departures {
            if let Some(departures) = population.get_item(intern!(py, "departures"))? {
                let Some(departures) = as_readable_list(&departures) else {
                    return unsupported("the departures are not a list");
                };
                for item in departures.iter() {
                    if let Some(item) = as_readable_dict(&item) {
                        if let Some(id) = item.get_item(intern!(py, "agent_id"))? {
                            if let Ok(id) = id.downcast_exact::<PyString>() {
                                ids.insert(id.to_str()?.to_owned());
                            } else if id.is_instance_of::<PyString>() {
                                return unsupported("a departure's ID is a str subclass");
                            }
                        }
                    }
                }
            }
        }
        Ok(ids)
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
        let mut historical: Option<FxHashSet<String>> = None;
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
                let actor_id = exact_str(&actor)?;
                if agents.contains(&actor)? {
                    continue;
                }
                if historical.is_none() {
                    historical = Some(self.historical_ids(true)?);
                }
                if !historical.as_ref().is_some_and(|ids| ids.contains(actor_id)) {
                    return unsupported("a body history actor is unknown");
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

    /// `validate_infrastructure_runtime_state` of a tick with its config, past the config's own
    /// check (`tick_plan`): the state's shape and the base capacity of each facility.
    fn infrastructure(&self, state: Option<&Bound<'py, PyAny>>, facilities: &Bound<'py, PyDict>) -> PyResult<()> {
        let py = self.py();
        let Some(state) = state.and_then(as_readable_dict) else {
            return unsupported("the infrastructure state is the Python rule's");
        };
        let version = state.get_item(intern!(py, "schema_version"))?;
        let part = |name: &Bound<'py, PyString>| -> PyResult<Option<Bound<'py, PyDict>>> {
            Ok(state.get_item(name)?.as_ref().and_then(as_readable_dict).cloned())
        };
        let base = part(intern!(py, "base_working_units"))?;
        if state.len() != 5
            || exact_int(&version.ok_or_else(|| Unsupported::new_err("no schema version"))?)? != 1
            || part(intern!(py, "outages"))?.is_none()
            || part(intern!(py, "environmental_stressors"))?.is_none()
            || part(intern!(py, "route_interruptions"))?.is_none()
        {
            return unsupported("the infrastructure state is the Python rule's");
        }
        let Some(base) = base else {
            return unsupported("the infrastructure state is the Python rule's");
        };
        for (facility_id, units) in base.iter() {
            let Some(facility) = facilities.get_item(&facility_id)? else {
                return unsupported("an infrastructure base capacity names an unknown facility");
            };
            let capacity = exact_int(&attribute(&facility, intern!(py, "capacity"))?)?;
            let units = exact_int(&units)?;
            if !(0..=capacity).contains(&units) {
                return unsupported("an infrastructure base capacity is out of range");
            }
        }
        Ok(())
    }
}

/// `{key: item.zone_id for key, item in facilities.items()} == kept`.
fn same_facility_zones(facilities: &Bound<'_, PyDict>, kept: &Bound<'_, PyDict>) -> PyResult<bool> {
    let py = facilities.py();
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

fn facility_zones_of<'py>(facilities: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyDict>> {
    let py = facilities.py();
    let zones = PyDict::new(py);
    for (key, facility) in facilities.iter() {
        zones.set_item(key, attribute(&facility, intern!(py, "zone_id"))?)?;
    }
    Ok(zones)
}

/// `map.keys() == kept`.
fn same_keys(map: &Bound<'_, PyDict>, kept: Option<&FxHashSet<String>>) -> PyResult<bool> {
    let Some(kept) = kept else {
        return Ok(false);
    };
    if kept.len() != map.len() {
        return Ok(false);
    }
    for (key, _) in map.iter() {
        if !kept.contains(exact_str(&key)?) {
            return Ok(false);
        }
    }
    Ok(true)
}

fn keys_of(map: &Bound<'_, PyDict>) -> PyResult<FxHashSet<String>> {
    let mut keys = FxHashSet::default();
    keys.reserve(map.len());
    for (key, _) in map.iter() {
        keys.insert(exact_str(&key)?.to_owned());
    }
    Ok(keys)
}

/// `_is_settled`, of a status that is a str: the values of SupportRequestStatus.RESOLVED and
/// UNRESOLVED; a StrEnum equals its value.
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

/// A dict's `pending` part (`pending_relocations`, `pending_arrivals`, ...) is empty or absent, in a
/// state that is a dict.
fn empty_part(state: Option<&Bound<'_, PyAny>>, part: &Bound<'_, PyString>) -> PyResult<bool> {
    let Some(state) = state.and_then(as_readable_dict) else {
        return Ok(false);
    };
    Ok(!state.get_item(part)?.is_some_and(|value| value.is_truthy().unwrap_or(true)))
}

/// Whether no presence record points into other model state, which may change.
fn no_archive_paths(presence: Option<&Bound<'_, PyAny>>) -> PyResult<bool> {
    let Some(presence) = presence.and_then(as_readable_dict) else {
        return Ok(false);
    };
    let Some(people) = presence.get_item(intern!(presence.py(), "people"))? else {
        return Ok(false);
    };
    let Some(people) = as_readable_dict(&people) else {
        return Ok(false);
    };
    for (_, person) in people.iter() {
        if let Some(person) = as_readable_dict(&person) {
            if let Some(path) = person.get_item(intern!(person.py(), "archive_path"))? {
                if path.is_truthy()? {
                    return Ok(false);
                }
            }
        }
    }
    Ok(true)
}

fn check(table: &Table, world: &Bound<'_, PyAny>, memo: &Bound<'_, TickMemo>) -> PyResult<()> {
    let py = world.py();
    for (holder, name, defined) in &table.watched {
        if !holder.bind(py).getattr(name.bind(py))?.is(defined.bind(py)) {
            return unsupported("a rule of the check is patched");
        }
    }

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
    // A WorldState v5 is a model before resource_service: docs/rust-port/step4-plan.md, L9.
    if exact_int(&attribute(world, intern!(py, "schema_version"))?)? != 6 {
        return unsupported("not a WorldState v6");
    }
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;

    // ---- The configs: the plan of the configs the world holds.
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let config_mirror = core.call_method1(intern!(py, "mirror"), ("model_configs",))?;
    if config_mirror.is_none() {
        return unsupported("no mirror follows the configs");
    }
    let generation = as_mirror(&config_mirror)?.call_method1(intern!(py, "generation_of"), (&configs,))?;
    if generation.is_none() {
        return unsupported("the configs are another tree than the mirror's");
    }
    let planned = {
        let kept = memo.borrow();
        match &kept.configs {
            Some((before, before_generation)) => before.bind(py).is(&configs) && before_generation.bind(py).eq(&generation)?,
            None => false,
        }
    };
    let configs_unchanged = planned && memo.borrow().plan.is_some();
    if !configs_unchanged {
        // The contracts, which raise for configs the check would turn down.
        let plan = table.tick_plan.bind(py).call1((world,))?;
        let plan = plan.downcast_exact::<Plan>()?.clone().unbind();
        let mut kept = memo.borrow_mut();
        kept.geometry_scope.bind(py).clear();
        kept.configs = Some((configs.clone().unbind(), generation.clone().unbind()));
        kept.plan = Some(plan);
        kept.body_config = None;
    }
    let plan = memo.borrow().plan.as_ref().map(|plan| plan.clone_ref(py)).ok_or_else(|| Unsupported::new_err("no plan"))?;
    let plan = plan.get();

    // ---- What the memo compares.
    let agents = attribute(world, intern!(py, "agents"))?;
    let agents = agents.downcast::<PyDict>()?;
    let zones = attribute(world, intern!(py, "zones"))?;
    let zones = zones.downcast::<PyDict>()?;
    let facilities = attribute(world, intern!(py, "facilities"))?;
    let facilities = facilities.downcast::<PyDict>()?;
    let (agents_unchanged, zones_unchanged, facilities_unchanged, seen, self_contained_before) = {
        let kept = memo.borrow();
        let facilities_unchanged = match &kept.facility_zones {
            Some(zones) => same_facility_zones(facilities, zones.bind(py))?,
            None => false,
        };
        let mut seen = Vec::with_capacity(COMPARED.len());
        for (index, name) in COMPARED.iter().enumerate() {
            seen.push(cx.seen(kept.seen[index].as_ref(), name)?);
        }
        (
            same_keys(agents, kept.agent_ids.as_ref())?,
            same_keys(zones, kept.zone_ids.as_ref())?,
            facilities_unchanged,
            seen,
            kept.presence_is_self_contained,
        )
    };
    let same = |index: usize| matches!(seen[index], Seen::Same);
    let presence = cx.state(PRESENCE)?;
    let presence_is_self_contained = if same(0) { self_contained_before } else { no_archive_paths(presence.as_ref())? };
    let presence_unchanged = agents_unchanged && zones_unchanged && same(0) && presence_is_self_contained;
    let lifecycle_unchanged = agents_unchanged
        && zones_unchanged
        && same(1)
        && (presence.is_none() || presence_unchanged)
        // A pending relocation is compared with the tasks and the event queue.
        && empty_part(cx.state(LIFECYCLE)?.as_ref(), intern!(py, "pending_relocations"))?;
    let dynamics_unchanged = agents_unchanged
        && zones_unchanged
        && same(2)
        // A pending arrival is compared with the event queue, the clock and the presence.
        && empty_part(cx.state(DYNAMICS)?.as_ref(), intern!(py, "pending_arrivals"))?;
    let overlay = cx.state(OVERLAY)?;
    let overlay_unchanged = configs_unchanged
        && agents_unchanged
        && facilities_unchanged
        && same(3)
        // Rooms expire with the clock, and route access is compared with the graph.
        && empty_part(overlay.as_ref(), intern!(py, "temporary_rooms"))?
        && empty_part(overlay.as_ref(), intern!(py, "route_modifiers"))?;

    // ---- The support requests to check: open, written or new since the last check. Their order does
    // not matter to a valid world; with several broken ones it decides only which error comes first.
    let support_requests = attribute(world, intern!(py, "support_requests"))?;
    let support_requests = support_requests.downcast::<PyDict>()?;
    let (added, restart) = {
        let kept = memo.borrow();
        match agents_unchanged.then(|| kept.requests.added_values(support_requests)).transpose()?.flatten() {
            Some(added) => (added, false),
            // The dict no longer starts with the requests held, or the Agents changed: read them all.
            None => (PyList::new(py, support_requests.values())?, true),
        }
    };
    let (open, written) = {
        let kept = memo.borrow();
        if restart {
            (PyList::empty(py), PyList::empty(py))
        } else {
            (kept.open.bind(py).clone(), kept.written.bind(py).clone())
        }
    };
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
        let Some(kernel) = validators.get_item(name)? else {
            return unsupported("a declared namespace has no kernel");
        };
        if !cx.accepts(text, &kernel)? {
            return unsupported("a declared namespace's kernel did not take it");
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
        // A checkpoint of the queues before facility_queues_v1, an old model: L9 of step4-plan.md.
        return unsupported("a legacy queue checkpoint");
    }
    cx.must_validate("agent_dynamics_v1")?;
    if let Some(body) = cx.state(BODY)? {
        if !cx.accepts(BODY, table.rows_valid.bind(py))? {
            return unsupported("the rows of the body state are the Python rule's");
        }
        let Some(state) = as_readable_dict(&body) else {
            return unsupported("the body state is another kind of dict");
        };
        let Some(config) = state.get_item(intern!(py, "config"))? else {
            return unsupported("the body state has no config");
        };
        let config_json = crate::encode(&config)?;
        let known = memo.borrow().body_config.as_ref().filter(|(json, _)| *json == config_json).map(|(_, ids)| ids.clone());
        let named = match known {
            Some(named) => named,
            None => {
                let named: Vec<String> = table.body_config_ids.bind(py).call1((world,))?.extract()?;
                memo.borrow_mut().body_config = Some((config_json, named.clone()));
                named
            }
        };
        // Each person a per-person parameter names is here or in the population state's history.
        let mut historical: Option<FxHashSet<String>> = None;
        for id in &named {
            if agents.contains(id.as_str())? {
                continue;
            }
            if historical.is_none() {
                historical = Some(cx.historical_ids(false)?);
            }
            if !historical.as_ref().is_some_and(|ids| ids.contains(id)) {
                return unsupported("the body config names an unknown resident");
            }
        }
        cx.body_history(&body, minute, agents)?;
    }
    if !dynamics_unchanged {
        cx.must_validate(DYNAMICS)?;
    }
    if !lifecycle_unchanged {
        cx.must_validate(LIFECYCLE)?;
    }
    cx.must_validate("social_network_v1")?;
    cx.must_validate("staff_dynamics_v1")?;
    cx.must_validate("staff_operations_v1")?;
    cx.must_validate(SPACES)?;
    cx.must_validate("movement_v1")?;
    if !overlay_unchanged {
        cx.must_validate(OVERLAY)?;
    }
    if cx.state(QUEUES)?.is_some() || plan.independent_queues {
        cx.must_validate(QUEUES)?;
    }
    if model_state.contains(SHARED)? && !plan.shared_configured {
        return unsupported("shared operations without their contract");
    }
    if cx.state(SHARED)?.is_some() && !cx.accepts(SHARED, table.tick_valid.bind(py))? {
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
    match &operational {
        Some(state) => cx.operational_schema(state)?,
        None if plan.operational_enabled != Some(false) => return unsupported("operational events without their state"),
        None => {}
    }
    let infrastructure = cx.state(INFRASTRUCTURE)?;
    if plan.infrastructure_configured {
        cx.infrastructure(infrastructure.as_ref(), facilities)?;
    } else if infrastructure.is_some() {
        return unsupported("infrastructure state without its model");
    }
    let environment = cx.state(ENVIRONMENT)?;
    match &plan.environment_zones {
        Some(baselines) => {
            for zone in baselines {
                if !zones.contains(zone.as_str())? {
                    return unsupported("a zone environment baseline names an unknown zone");
                }
            }
            cx.environment_schema(environment.as_ref())?;
        }
        None if environment.is_some() => return unsupported("a zone environment without its config"),
        None => {}
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
            if !plan.infrastructure_configured && plan.infrastructure_events.contains(kind) {
                event_rule_broken = Some("an infrastructure event without the model");
            } else if plan.environment_zones.is_none() && kind.starts_with(ENVIRONMENT_EVENTS) {
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
    // `_validate_zone_capacity` of the zones the entity rules leave: their temporary spaces and
    // rooms count too (`rules/overlay.rs`).
    for item in left.iter() {
        let item = item.downcast::<PyTuple>()?;
        let (zone, occupancy) = (item.get_item(0)?, exact_int(&item.get_item(1)?)?);
        if let Some(capacity) = crate::rules::overlay::total_zone_capacity(world, &zone, occupancy)? {
            if occupancy > capacity {
                return unsupported("a zone holds more than its capacity");
            }
        }
    }

    // ---- Everything passed: what the memo keeps. Nothing above wrote to it but the configs' plan.
    let mut reopened = false;
    for request in written.iter() {
        if !is_settled(&request)? {
            reopened = true;
            break;
        }
    }
    // The memo's list, or after a restart a new one: requests settled before write to the old.
    let new_written = written.clone();
    let still_open = PyList::empty(py);
    let none = py.None().into_bound(py);
    for request in checked.iter() {
        if is_settled(&request)? {
            report_writes_to(&request, new_written.as_any())?;
        } else {
            report_writes_to(&request, &none)?;
            still_open.append(&request)?;
        }
    }
    // A reopened request came last in the check; unsettled_requests keeps the dict's order.
    let still_open = if reopened {
        let every = PyList::empty(py);
        for (_, request) in support_requests.iter() {
            if !is_settled(&request)? {
                every.append(request)?;
            }
        }
        every
    } else {
        still_open
    };
    let mut kept = memo.borrow_mut();
    if restart {
        kept.requests.clear();
        kept.written = new_written.clone().unbind();
    }
    kept.requests.extend(&added);
    new_written.call_method0(intern!(py, "clear"))?;
    kept.open = still_open.unbind();
    if !agents_unchanged {
        kept.agent_ids = Some(keys_of(agents)?);
    }
    if !zones_unchanged {
        kept.zone_ids = Some(keys_of(zones)?);
    }
    if !facilities_unchanged {
        kept.facility_zones = Some(facility_zones_of(facilities)?.unbind());
    }
    for (index, now) in seen.into_iter().enumerate() {
        if let Seen::Changed(stamp) = now {
            kept.seen[index] = Some(stamp);
        }
    }
    kept.presence_is_self_contained = presence_is_self_contained;
    kept.unchanged = [configs_unchanged, presence_unchanged, lifecycle_unchanged, dynamics_unchanged, overlay_unchanged];
    kept.minute = minute;
    Ok(())
}

/// The check, with the memo moved on, or Unsupported with the memo as it was but for the configs'
/// plan. `shadow` is for `benchmarks/rule_shadow/shadow_after_tick.py`, the one caller that runs this
/// while the shadows keep the rest of the tick on the Python path.
#[pyfunction]
#[pyo3(signature = (world, memo, shadow=false))]
fn after_tick(world: &Bound<'_, PyAny>, memo: &Bound<'_, TickMemo>, shadow: bool) -> PyResult<()> {
    let py = world.py();
    let answer = match TABLE.get(py) {
        None => unsupported("domain/simulator_state.py has not installed the table"),
        Some(_) if !shadow && (crate::rules::tick::path_is_python_only() || crate::rules::dispatch::python_entries()) => {
            unsupported("a benchmark's shadow is installed")
        }
        Some(table) => check(table, world, memo),
    };
    match answer {
        Ok(()) => {
            RAN.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }
        // A rule or contract that raises, called from here or read here, is the Python check's to raise.
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
    configs: (Py<PyAny>, Py<PyAny>),
    needs: (Py<PyAny>, Py<PyAny>),
) -> PyResult<()> {
    let mut defined = Vec::with_capacity(watched.len());
    for (holder, name) in watched {
        let value = holder.bind(py).getattr(name.bind(py))?.unbind();
        defined.push((holder, name, value));
    }
    let (tick_plan, body_config_ids) = configs;
    TABLE.get_or_init(py, || Table {
        watched: defined,
        validators,
        rows_valid,
        tick_valid,
        effects_valid,
        tick_plan,
        body_config_ids,
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
    m.add_class::<TickMemo>()?;
    m.add_function(wrap_pyfunction!(after_tick, m)?)?;
    m.add_function(wrap_pyfunction!(install, m)?)?;
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    Ok(())
}
