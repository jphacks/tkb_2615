//! The rules of `events/effects.py` that run in a tick: a guard, the five handlers of an effect's
//! events, what other modules ask of the modifier stacks, and the check of `effect_program_v1`.
//!
//! A handler's Python rule makes a copy of the world, changes the copy, checks it and swaps it in.
//! The rules here do the same through the same calls (`copy_of`, `commit`). That transaction is the
//! design's until `core.fork()`: docs/rust-port/step3-design.md.
//!
//! Anything but a success says Unsupported with the world as it was: a suppressed effect, a failed
//! one, a value the rule isn't sure of, and any exception from a call before the swap. The Python
//! rule then runs from the start and says it in its own words. Two rules change the world in place
//! after their checks and let an exception pass, as their Python rules do: `facility_repair`'s
//! `world.validate()` and the receipts after a swap.
//!
//! The pydantic contracts of the parameters stay Python's: a rule calls `model_validate` where its
//! Python rule does, through `_PARAMETER_MODELS` of `events/effects.py`. So do the rules of modules
//! other lanes hold (`apply_geometry_change`, the two of temporary spaces, `order_staff_operation`,
//! the receipts): they're called by name, with no borrow held. Nothing here reads LLM output: what
//! a program may do was validated before it was scheduled, by `validate_effect_program`, which is
//! not touched (docs/rust-port/lanes/m6-effects.md).

use std::collections::{BTreeMap, HashSet};
use std::sync::{Arc, Mutex};

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySlice, PyString, PyTuple};
use sha2::{Digest, Sha256};

use crate::core::row::{attribute, exact_int, exact_str, namespace_is_absent, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::core::world_core::{mirror_of, raw_dict_get, NamespaceMirror};
use crate::rules::staff_dynamics::{is_available, staff_status};
use crate::state::effect_program::{EffectProgramRow, Program, NAME};
use crate::state::entities::{entities_of, EntityStore};
use crate::state::events::{is_stable_id, EventRow, EventWriter};
use crate::state::resource_ledger::{held_mirror, resource_index, LedgerRow};
use crate::Unsupported;

const MARKER: &str = "effect_program_v1";

// ---- What ran and what was refused --------------------------------------------------------------

static RAN: Mutex<BTreeMap<&'static str, u64>> = Mutex::new(BTreeMap::new());
static REFUSALS: Mutex<BTreeMap<String, u64>> = Mutex::new(BTreeMap::new());

/// Count a kernel's result under its label. Each handler also has one label per operator.
fn counted<T>(py: Python<'_>, label: &'static str, result: PyResult<T>) -> PyResult<T> {
    match &result {
        Ok(_) => {
            if let Ok(mut ran) = RAN.lock() {
                *ran.entry(label).or_insert(0) += 1;
            }
        }
        Err(error) if error.is_instance_of::<Unsupported>(py) => {
            if let Ok(mut refusals) = REFUSALS.lock() {
                *refusals.entry(format!("{label}: {}", error.value(py))).or_insert(0) += 1;
            }
        }
        Err(_) => {}
    }
    result
}

/// An exception of the stage before any change: the Python rule decides, and raises its own.
fn refused_by_python<T>(py: Python<'_>, result: PyResult<T>) -> PyResult<T> {
    result.map_err(|error| {
        if error.is_instance_of::<Unsupported>(py) {
            error
        } else {
            let kind = error.get_type(py).name().map_or_else(|_| "?".to_owned(), |name| name.to_string());
            Unsupported::new_err(format!("a call raised {kind}"))
        }
    })
}

// ---- Reading -------------------------------------------------------------------------------------

type Doc = IndexMap<Arc<str>, V>;

fn map_of(value: &V) -> PyResult<&Doc> {
    match value {
        V::Map(map) => Ok(map),
        _ => unsupported("not an object"),
    }
}

fn str_of<'a>(map: &'a Doc, key: &str) -> Option<&'a Arc<str>> {
    match map.get(key) {
        Some(V::Str(text)) => Some(text),
        _ => None,
    }
}

fn required_str<'a>(map: &'a Doc, key: &str) -> PyResult<&'a Arc<str>> {
    str_of(map, key).map_or_else(|| unsupported("a value is not a str"), Ok)
}

/// `set(map) == set(names)`.
fn has_exactly(map: &Doc, names: &[&str]) -> bool {
    map.len() == names.len() && names.iter().all(|name| map.contains_key(*name))
}

/// A list of str of the marker, for `name in list` and the sources of a record.
fn str_list<'a>(map: &'a Doc, key: &str) -> PyResult<Vec<&'a Arc<str>>> {
    match map.get(key) {
        None => Ok(Vec::new()),
        Some(V::List(items)) => items
            .iter()
            .map(|item| match item {
                V::Str(text) => Ok(text),
                _ => unsupported("a list of the marker holds another value"),
            })
            .collect(),
        Some(_) => unsupported("a list of the marker is another value"),
    }
}

/// The row of `effect_program_v1`, or None when the world doesn't hold the namespace.
fn program_state<R>(
    world: &Bound<'_, PyAny>,
    read: impl FnOnce(Option<&mut EffectProgramRow>) -> PyResult<R>,
) -> PyResult<R> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, MARKER))? {
        return read(None);
    }
    let mirror = held_program_mirror(world)?;
    NamespaceMirror::with_row::<EffectProgramRow, R>(&mirror, |row| read(Some(row)))
}

/// The mirror of the namespace the world holds.
fn held_program_mirror<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, NamespaceMirror>> {
    let py = world.py();
    let mirror = mirror_of(world, NAME)?;
    let table = attribute(world, intern!(py, "model_state"))?;
    if as_readable_dict(&table).is_none() {
        return unsupported("model_state is another kind of mapping");
    }
    let held = raw_dict_get(&table, intern!(py, "effect_program_v1").as_any())?;
    if !held.is_some_and(|held| mirror.try_borrow().is_ok_and(|mirror| mirror.root_is(&held))) {
        return unsupported("the world holds another effect_program_v1");
    }
    Ok(mirror)
}

fn modifier_id(program_id: &str, effect_id: &str) -> String {
    let digest = Sha256::digest(format!("{program_id}:{effect_id}").as_bytes());
    let mut text = String::with_capacity(41);
    text.push_str("modifier-");
    for byte in &digest[..16] {
        text.push_str(&format!("{byte:02x}"));
    }
    text
}

fn event_row(event: &Bound<'_, PyAny>) -> PyResult<EventRow> {
    EventRow::from_event(event)?.map_or_else(|| unsupported("not a plain ScheduledEvent"), Ok)
}

fn get_or_none<'py>(map: &Bound<'py, PyAny>, key: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    let value = map.call_method1(intern!(map.py(), "get"), (key,))?;
    Ok((!value.is_none()).then_some(value))
}

fn model_state<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    attribute(world, intern!(world.py(), "model_state"))
}

fn is_dict(value: &Bound<'_, PyAny>) -> bool {
    value.is_instance_of::<PyDict>()
}

fn ints_equal(value: &Bound<'_, PyAny>, wanted: i64) -> PyResult<bool> {
    Ok(exact_int(value)? == wanted)
}

// ---- The transaction -----------------------------------------------------------------------------

/// `MutableWorldState.from_dict(world.to_dict())`.
fn copy_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let class = py.import(intern!(py, "sheltersim.core.world"))?.getattr(intern!(py, "WorldState"))?;
    class.call_method1(intern!(py, "from_dict"), (world.call_method0(intern!(py, "to_dict"))?,))
}

fn validate(candidate: &Bound<'_, PyAny>) -> PyResult<()> {
    candidate.call_method0(intern!(candidate.py(), "validate"))?;
    Ok(())
}

/// `world.__dict__.clear(); world.__dict__.update(candidate.__dict__)`.
fn swap(world: &Bound<'_, PyAny>, candidate: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let target = world.getattr(intern!(py, "__dict__"))?;
    let source = candidate.getattr(intern!(py, "__dict__"))?;
    target.call_method0(intern!(py, "clear"))?;
    target.call_method1(intern!(py, "update"), (source,))?;
    Ok(())
}

/// `validate` and `swap`. An error here leaves the world as it was.
fn commit(world: &Bound<'_, PyAny>, candidate: &Bound<'_, PyAny>) -> PyResult<()> {
    validate(candidate)?;
    swap(world, candidate)
}

fn call_rule<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
    arguments: impl IntoPyObject<'py, Target = PyTuple>,
    keywords: Option<&Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyAny>> {
    py.import(module)?.getattr(name)?.call(arguments, keywords)
}

/// `Model.model_validate(raw)`, with the model by operator. An invalid value raises, for the
/// caller to turn into Unsupported.
fn parameters<'py>(py: Python<'py>, op: &str, raw: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    py.import(intern!(py, "sheltersim.events.effects"))?
        .getattr(intern!(py, "_PARAMETER_MODELS"))?
        .get_item(op)?
        .call_method1(intern!(py, "model_validate"), (raw,))
}

fn payload_item<'py>(event: &Bound<'py, PyAny>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    attribute(event, intern!(event.py(), "payload"))?.get_item(key)
}

fn expire_of(handler: &Bound<'_, PyAny>) -> PyResult<bool> {
    handler.getattr(intern!(handler.py(), "expire"))?.extract()
}

// ---- The guard -----------------------------------------------------------------------------------

#[derive(Clone, Copy)]
enum Compare {
    Eq,
    Ne,
    Gt,
    Gte,
    Lt,
    Lte,
}

#[derive(Clone, Copy)]
enum GuardField {
    Minute,
    Capacity,
    WorkingUnits,
    QueueLength,
    ZoneOccupancy,
    ResourceAvailable,
    StaffAvailable,
}

enum Guard<'a> {
    All(Vec<Guard<'a>>),
    Any(Vec<Guard<'a>>),
    Leaf { compare: Compare, field: GuardField, value: &'a V },
}

/// The fields of `GuardExpression` of one node, or None when the contract refuses them.
/// `depth` starts at 1 and `nodes` counts the tree, as `measure` does.
fn parse_guard<'a>(value: &'a V, depth: usize, nodes: &mut usize) -> Option<Guard<'a>> {
    if depth > 4 {
        return None;
    }
    *nodes += 1;
    if *nodes > 20 {
        return None;
    }
    let V::Map(map) = value else {
        return None;
    };
    if map.keys().any(|key| !matches!(&**key, "operator" | "field" | "value" | "operands")) {
        return None;
    }
    let V::Str(operator) = map.get("operator")? else {
        return None;
    };
    let field = match map.get("field") {
        None | Some(V::Null) => None,
        Some(V::Str(field)) => Some(field),
        Some(_) => return None,
    };
    let leaf_value = match map.get("value") {
        None | Some(V::Null) => None,
        Some(item) => Some(item),
    };
    let operands: &[V] = match map.get("operands") {
        None => &[],
        Some(V::List(items)) if items.len() <= 4 => items,
        Some(_) => return None,
    };
    let compare = match &**operator {
        "eq" => Compare::Eq,
        "ne" => Compare::Ne,
        "gt" => Compare::Gt,
        "gte" => Compare::Gte,
        "lt" => Compare::Lt,
        "lte" => Compare::Lte,
        "and" | "or" => {
            if field.is_some() || leaf_value.is_some() || operands.len() < 2 {
                return None;
            }
            let children = operands
                .iter()
                .map(|item| parse_guard(item, depth + 1, nodes))
                .collect::<Option<Vec<_>>>()?;
            return Some(if &**operator == "and" { Guard::All(children) } else { Guard::Any(children) });
        }
        _ => return None,
    };
    if !operands.is_empty() {
        return None;
    }
    let value = leaf_value?;
    let numeric = match value {
        V::Int(_) => true,
        V::Float(number) if number.is_finite() => true,
        V::Bool(_) => false,
        V::Str(text) if text.chars().count() <= 256 => false,
        // An int past 64 bits, or what the contract refuses.
        _ => return None,
    };
    if !numeric && !matches!(compare, Compare::Eq | Compare::Ne) {
        return None;
    }
    let field = match &***field.as_ref()? {
        "simulation.minute" => GuardField::Minute,
        "target.capacity" => GuardField::Capacity,
        "target.working_units" => GuardField::WorkingUnits,
        "target.queue_length" => GuardField::QueueLength,
        "zone.occupancy" => GuardField::ZoneOccupancy,
        "resource.available_quantity" => GuardField::ResourceAvailable,
        "staff.available" => GuardField::StaffAvailable,
        _ => return None,
    };
    Some(Guard::Leaf { compare, field, value })
}

enum Actual {
    Int(i64),
    Bool(bool),
}

/// Above this an int doesn't always fit a float; Python compares the two exactly.
const FLOAT_EXACT_INT: i64 = 1 << 53;

/// `actual` against `expected` by value, or Unsupported where the cast could round.
fn numeric_order(actual: i64, expected: &V) -> PyResult<std::cmp::Ordering> {
    match expected {
        V::Int(expected) => Ok(actual.cmp(expected)),
        V::Float(expected) => {
            if actual.abs() > FLOAT_EXACT_INT {
                return unsupported("a guard compares a large int with a float");
            }
            (actual as f64).partial_cmp(expected).map_or_else(|| unsupported("a guard's value is not finite"), Ok)
        }
        _ => unsupported("a guard's value is not a number"),
    }
}

fn compare_guard(compare: Compare, actual: &Actual, expected: &V) -> PyResult<bool> {
    use std::cmp::Ordering::{Equal, Greater, Less};
    let number = match actual {
        Actual::Int(number) if matches!(expected, V::Int(_) | V::Float(_)) => Some(*number),
        _ => None,
    };
    let equal = match (number, actual, expected) {
        (Some(number), _, _) => numeric_order(number, expected)? == Equal,
        (None, Actual::Bool(left), V::Bool(right)) => left == right,
        // Different types: `type(actual) is type(expected)` is false.
        _ => false,
    };
    Ok(match compare {
        Compare::Eq => equal,
        Compare::Ne => !equal,
        _ => {
            let Actual::Int(actual) = actual else {
                return Ok(false);
            };
            let order = numeric_order(*actual, expected)?;
            match compare {
                Compare::Gt => order == Greater,
                Compare::Gte => order != Less,
                Compare::Lt => order == Less,
                _ => order != Greater,
            }
        }
    })
}

fn evaluate_guard(world: &Bound<'_, PyAny>, row: &EventRow, guard: &Guard<'_>) -> PyResult<bool> {
    match guard {
        Guard::All(children) => {
            for child in children {
                if !evaluate_guard(world, row, child)? {
                    return Ok(false);
                }
            }
            Ok(true)
        }
        Guard::Any(children) => {
            for child in children {
                if evaluate_guard(world, row, child)? {
                    return Ok(true);
                }
            }
            Ok(false)
        }
        Guard::Leaf { compare, field, value } => {
            compare_guard(*compare, &guard_field(world, row, *field)?, value)
        }
    }
}

/// The first of the payload's IDs that is a str; a payload without one is a shipment's.
fn guard_target(world: &Bound<'_, PyAny>, row: &EventRow) -> PyResult<Option<Arc<str>>> {
    let payload = map_of(&row.payload)?;
    for name in ["facility_id", "staff_id", "zone_id", "resource_id", "target_id"] {
        if let Some(target) = str_of(payload, name) {
            return Ok(Some(target.clone()));
        }
    }
    let marker = match map_of(&row.provenance)?.get(MARKER) {
        Some(V::Map(marker)) => marker,
        _ => return unsupported("the guard's marker is another value"),
    };
    let single = match marker.get("program_id") {
        Some(V::Str(program_id)) => program_state(world, |state| {
            Ok(state
                .and_then(|state| state.programs.get(&**program_id))
                .is_some_and(|program| program.effect_ids.len() == 1))
        })?,
        Some(V::Null) | None => false,
        Some(_) => return unsupported("the guard's marker has another program ID"),
    };
    if !single {
        return Ok(None);
    }
    match payload.get("shipment_id") {
        None | Some(V::Null) => Ok(None),
        Some(V::Str(shipment)) => Ok(Some(shipment.clone())),
        Some(_) => unsupported("a shipment ID is another value"),
    }
}

fn guard_field(world: &Bound<'_, PyAny>, row: &EventRow, field: GuardField) -> PyResult<Actual> {
    let py = world.py();
    let payload = map_of(&row.payload)?;
    let target = guard_target(world, row)?;
    let target_text = target.as_deref().unwrap_or("");
    let store = entities_of(world)?;
    match field {
        GuardField::Minute => Ok(Actual::Int(exact_int(&attribute(world, intern!(py, "minute_index"))?)?)),
        GuardField::Capacity | GuardField::WorkingUnits | GuardField::QueueLength => {
            EntityStore::with_rows(&store, |rows| {
                let Some(facility) = rows.facilities.get(target_text) else {
                    return unsupported("a guard has no facility target");
                };
                Ok(Actual::Int(match field {
                    GuardField::Capacity => facility.capacity,
                    GuardField::WorkingUnits => facility.working_units,
                    _ => (facility.queue_agent_ids.len() + facility.resource_queue_agent_ids.len()) as i64,
                }))
            })
        }
        GuardField::ZoneOccupancy => {
            let own_zone = match payload.get("zone_id") {
                None | Some(V::Null) => None,
                Some(V::Str(zone)) if zone.is_empty() => None,
                Some(V::Str(zone)) => Some(zone.clone()),
                Some(_) => return unsupported("a zone ID is another value"),
            };
            let (zone, count) = EntityStore::with_rows(&store, |rows| {
                let zone = match own_zone {
                    Some(zone) => zone,
                    None => match rows.facilities.get(target_text) {
                        Some(facility) => facility.zone_id.clone(),
                        None => match &target {
                            Some(target) => target.clone(),
                            None => return unsupported("a guard has no zone target"),
                        },
                    },
                };
                let count = rows.agents.iter().filter(|(_, agent)| agent.zone_id == zone).count();
                Ok((zone, count))
            })?;
            if !attribute(world, intern!(py, "zones"))?.contains(&*zone)? {
                return unsupported("a guard has no known zone target");
            }
            Ok(Actual::Int(count as i64))
        }
        GuardField::ResourceAvailable => {
            let resource = str_of(payload, "resource_id")
                .map_or_else(|| unsupported("a guard has no ledger target"), Ok)?;
            let Some(index) = resource_index(resource) else {
                return unsupported("a guard has no ledger target");
            };
            let mirror = held_mirror(world)?;
            NamespaceMirror::with_row::<LedgerRow, _>(&mirror, |ledger| {
                let account = ledger.accounts.of(index);
                account
                    .remaining
                    .checked_sub(account.reserved)
                    .map_or_else(|| unsupported("a quantity past 64 bits"), |left| Ok(Actual::Int(left)))
            })
        }
        GuardField::StaffAvailable => {
            let staff_id = match payload.get("staff_id") {
                Some(V::Str(staff)) if !staff.is_empty() => staff.clone(),
                None | Some(V::Null) | Some(V::Str(_)) => match &target {
                    Some(target) => target.clone(),
                    None => return unsupported("a guard has no staff target"),
                },
                Some(_) => return unsupported("a staff ID is another value"),
            };
            let known = EntityStore::with_rows(&store, |rows| Ok(rows.staff.get(&staff_id).is_some()))?;
            if !known {
                return unsupported("a guard has no known staff target");
            }
            let status = staff_status(world)?;
            let id = PyString::new(py, &staff_id);
            Ok(Actual::Bool(is_available(status.as_ref(), &id)?))
        }
    }
}

fn guard_of_row(world: &Bound<'_, PyAny>, row: &EventRow) -> PyResult<bool> {
    let marker = match map_of(&row.provenance)?.get(MARKER) {
        Some(V::Map(marker)) => marker,
        _ => return Ok(true),
    };
    let guard = match marker.get("guard") {
        None | Some(V::Null) => return Ok(true),
        Some(guard) => guard,
    };
    let mut nodes = 0;
    let Some(parsed) = parse_guard(guard, 1, &mut nodes) else {
        return unsupported("the guard isn't one the contract surely takes");
    };
    evaluate_guard(world, row, &parsed)
}

/// `effect_event_guard_or_suppress` of a rule: false is a suppressed effect, the Python rule's.
fn guard_passes(world: &Bound<'_, PyAny>, row: &EventRow) -> PyResult<()> {
    if guard_of_row(world, row)? {
        Ok(())
    } else {
        unsupported("the guard is false, so the effect is suppressed")
    }
}

/// `should_apply_effect_guard`. Most events have no marker, and need nothing but that answer.
#[pyfunction]
fn should_apply_guard(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    let result = (|| {
        let provenance = attribute(event, intern!(py, "provenance"))?;
        if provenance.call_method1(intern!(py, "get"), (intern!(py, MARKER),))?.is_none() {
            return Ok(true);
        }
        guard_of_row(world, &event_row(event)?)
    })();
    counted(py, "guard", refused_by_python(py, result))
}

// ---- What other modules ask ----------------------------------------------------------------------

/// `has_active_facility_modifier`.
#[pyfunction]
fn has_active_modifier(world: &Bound<'_, PyAny>, facility_id: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    let result = (|| {
        let id = exact_str(facility_id)?;
        program_state(world, |state| {
            Ok(state
                .and_then(|state| state.active_facility_modifiers.get(id))
                .is_some_and(|record| !record.modifiers.is_empty()))
        })
    })();
    counted(py, "has_active_modifier", refused_by_python(py, result))
}

fn pending_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    attribute(world, intern!(py, "event_queue"))?.call_method0(intern!(py, "pending"))
}

/// `has_pending_facility_modifier`.
#[pyfunction]
fn has_pending_modifier(world: &Bound<'_, PyAny>, facility_id: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    let result = (|| {
        let id = exact_str(facility_id)?;
        for event in pending_of(world)?.try_iter()? {
            let event = event?;
            let event_type = attribute(&event, intern!(py, "event_type"))?;
            let event_type = exact_str(&event_type)?;
            if !matches!(event_type, "effect_program_apply" | "effect_program_expire") {
                continue;
            }
            let payload = attribute(&event, intern!(py, "payload"))?;
            if let Some(target) = get_or_none(&payload, "facility_id")? {
                if target.eq(id)? {
                    return Ok(true);
                }
            }
        }
        Ok(false)
    })();
    counted(py, "has_pending_modifier", refused_by_python(py, result))
}

/// `active_facility_outage`: `bool(infrastructure_event_context_ids(world, facility_id))`.
fn active_outage(world: &Bound<'_, PyAny>, facility_id: &str) -> PyResult<bool> {
    let py = world.py();
    let table = model_state(world)?;
    let Some(state) = get_or_none(&table, "infrastructure_v1")? else {
        return Ok(false);
    };
    if !is_dict(&state) {
        return Ok(false);
    }
    let outages = state.get_item(intern!(py, "outages"))?;
    for record in outages.call_method0(intern!(py, "values"))?.try_iter()? {
        let record = record?;
        if record.get_item(intern!(py, "active"))?.is_truthy()?
            && record.get_item(intern!(py, "affected_facility_ids"))?.contains(facility_id)?
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn occupancy(world: &Bound<'_, PyAny>, facility: &Bound<'_, PyAny>) -> PyResult<i64> {
    let py = world.py();
    let mut count = attribute(facility, intern!(py, "active_agent_ids"))?.len()?;
    if ints_equal(&attribute(world, intern!(py, "schema_version"))?, 6)? {
        count += attribute(facility, intern!(py, "resource_active_agent_ids"))?.len()?;
    }
    Ok(count as i64)
}

// ---- EffectProgramFacilityHandler ----------------------------------------------------------------

const FACILITY_APPLY_FIELDS: [&str; 5] = ["program_id", "effect_id", "modifier_id", "facility_id", "working_units"];
const FACILITY_EXPIRE_FIELDS: [&str; 4] = ["program_id", "effect_id", "modifier_id", "facility_id"];

fn effect_is_scheduled(program: &Program, effect_id: &str) -> bool {
    program.effect_ids.iter().any(|item| &**item == effect_id)
}

fn modifier_dict<'py>(
    py: Python<'py>,
    modifier: &str,
    program_id: &str,
    effect_id: &str,
    units: i64,
    row: &EventRow,
) -> PyResult<Bound<'py, PyDict>> {
    let item = PyDict::new(py);
    item.set_item("modifier_id", modifier)?;
    item.set_item("program_id", program_id)?;
    item.set_item("effect_id", effect_id)?;
    item.set_item("working_units", units)?;
    item.set_item("start_minute", row.due_minute)?;
    item.set_item("sequence", row.sequence)?;
    Ok(item)
}

fn facility_rule(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, expire: bool) -> PyResult<()> {
    let py = world.py();
    let row = event_row(event)?;
    let payload = map_of(&row.payload)?;
    let fields: &[&str] = if expire { &FACILITY_EXPIRE_FIELDS } else { &FACILITY_APPLY_FIELDS };
    if !has_exactly(payload, fields) {
        return unsupported("the payload has other fields");
    }
    let program_id = required_str(payload, "program_id")?;
    let effect_id = required_str(payload, "effect_id")?;
    let modifier = required_str(payload, "modifier_id")?;
    let facility_id = required_str(payload, "facility_id")?;
    let known = attribute(world, intern!(py, "facilities"))?;
    let Some(facility) = get_or_none(&known, facility_id)? else {
        return unsupported("an unknown facility");
    };
    if active_outage(world, facility_id)? {
        return unsupported("an active infrastructure outage");
    }
    let working = exact_int(&attribute(&facility, intern!(py, "working_units"))?)?;
    let capacity = exact_int(&attribute(&facility, intern!(py, "capacity"))?)?;
    let occupied = occupancy(world, &facility)?;
    // Whether the facility has a stack; the modifier's units, or how many others stay; and what
    // the facility's units become.
    let (has_stack, units_or_others, target) = program_state(world, |state| {
        let Some(state) = state else {
            return unsupported("no effect_program_v1");
        };
        let scheduled = state.programs.get(&**program_id).is_some_and(|program| effect_is_scheduled(program, effect_id));
        if !scheduled {
            return unsupported("the event's effect is not scheduled");
        }
        let current = state.active_facility_modifiers.get(&**facility_id);
        if !expire {
            let units = match payload.get("working_units") {
                Some(V::Int(units)) => *units,
                _ => return unsupported("working_units is not an int"),
            };
            if units < occupied || units < 0 || units > working || units > capacity {
                return unsupported("the modifier violates capacity or occupancy");
            }
            let baseline = current.map_or(working, |record| record.baseline_working_units);
            if current.is_some_and(|record| record.modifiers.iter().any(|item| item.modifier_id == *modifier)) {
                return unsupported("the modifier is already active");
            }
            let mut target = baseline.min(units);
            for item in current.iter().flat_map(|record| record.modifiers.iter()) {
                target = target.min(item.working_units);
            }
            return Ok((current.is_some(), units, target));
        }
        let Some(current) = current else {
            return unsupported("the modifier is not active");
        };
        if !current.modifiers.iter().any(|item| item.modifier_id == *modifier) {
            return unsupported("the modifier has expired or never started");
        }
        let mut target = current.baseline_working_units;
        let mut others = 0;
        for item in current.modifiers.iter().filter(|item| item.modifier_id != *modifier) {
            target = target.min(item.working_units);
            others += 1;
        }
        if target < occupied {
            return unsupported("the expiry would reduce capacity below occupancy");
        }
        Ok((true, others, target))
    })?;

    let candidate = copy_of(world)?;
    let records = model_state(&candidate)?
        .get_item(intern!(py, "effect_program_v1"))?
        .get_item(intern!(py, "active_facility_modifiers"))?;
    let facility = attribute(&candidate, intern!(py, "facilities"))?.get_item(&**facility_id)?;
    if expire {
        // The items stay the nodes they are, as the Python rule's list comprehension keeps them.
        if units_or_others > 0 {
            let current = records.get_item(&**facility_id)?;
            let remaining = PyList::empty(py);
            for item in current.get_item(intern!(py, "modifiers"))?.try_iter()? {
                let item = item?;
                if !item.get_item(intern!(py, "modifier_id"))?.eq(&**modifier)? {
                    remaining.append(item)?;
                }
            }
            facility.setattr(intern!(py, "working_units"), target)?;
            current.set_item(intern!(py, "modifiers"), remaining)?;
        } else {
            facility.setattr(intern!(py, "working_units"), target)?;
            records.call_method1(intern!(py, "pop"), (&**facility_id,))?;
        }
    } else {
        if !has_stack {
            let record = PyDict::new(py);
            record.set_item("baseline_working_units", working)?;
            record.set_item("modifiers", PyList::empty(py))?;
            records.set_item(&**facility_id, record)?;
        }
        let current = records.get_item(&**facility_id)?;
        let item = modifier_dict(py, modifier, program_id, effect_id, units_or_others, &row)?;
        current.get_item(intern!(py, "modifiers"))?.call_method1(intern!(py, "append"), (item,))?;
        facility.setattr(intern!(py, "working_units"), target)?;
    }
    commit(world, &candidate)
}

/// `EffectProgramFacilityHandler.apply`.
#[pyfunction]
fn facility(handler: &Bound<'_, PyAny>, world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let result = expire_of(handler).and_then(|expire| facility_rule(world, event, expire));
    counted(py, "facility", refused_by_python(py, result))
}

// ---- EffectProgramEnvironmentHandler -------------------------------------------------------------

const ENVIRONMENT_STATE: &str = "zone_environment_v1";
const ENVIRONMENT_POLICY: &str = "zone_environment_v1";
const ZONE_EXPIRE_FIELDS: [&str; 4] = ["program_id", "effect_id", "modifier_id", "zone_id"];
const ENVIRONMENT_APPLY_FIELDS: [&str; 7] =
    ["program_id", "effect_id", "modifier_id", "zone_id", "op", "parameters", "end_minute"];

fn sorted_unique<'a>(values: impl IntoIterator<Item = &'a str>) -> Vec<&'a str> {
    let mut items: Vec<&str> = values.into_iter().collect();
    items.sort_unstable();
    items.dedup();
    items
}

fn environment_rule(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, expire: bool) -> PyResult<()> {
    let py = world.py();
    let row = event_row(event)?;
    let payload = map_of(&row.payload)?;
    let fields: &[&str] = if expire { &ZONE_EXPIRE_FIELDS } else { &ENVIRONMENT_APPLY_FIELDS };
    if !has_exactly(payload, fields) {
        return unsupported("the payload has other fields");
    }
    let program_id = required_str(payload, "program_id")?;
    let effect_id = required_str(payload, "effect_id")?;
    let modifier = required_str(payload, "modifier_id")?;
    let zone_id = required_str(payload, "zone_id")?;
    let marker = match map_of(&row.provenance)?.get(MARKER) {
        Some(V::Map(marker)) => marker,
        _ => return unsupported("no marker"),
    };
    if !str_list(marker, "policy_dependencies")?.iter().any(|item| &***item == ENVIRONMENT_POLICY) {
        return unsupported("no installed policy");
    }
    let approved_op = program_state(world, |state| {
        let program = state.and_then(|state| state.programs.get(&**program_id));
        match program {
            Some(program) if effect_is_scheduled(program, effect_id) => Ok(program.operators.get(&**effect_id).cloned()),
            _ => unsupported("the event's effect is not scheduled"),
        }
    })?;
    let table = model_state(world)?;
    let Some(environment) = get_or_none(&table, ENVIRONMENT_STATE)? else {
        return unsupported("no zone_environment_v1");
    };
    if !is_dict(&environment) {
        return unsupported("zone_environment_v1 is another value");
    }
    if *modifier.as_ref() != *modifier_id(program_id, effect_id) {
        return unsupported("the modifier ID does not match its effect");
    }
    let modifiers = environment.get_item(intern!(py, "modifiers"))?;
    if expire {
        let Some(record) = get_or_none(&modifiers, modifier)? else {
            return unsupported("the modifier is not active");
        };
        let same = |key: &Bound<'_, PyString>, wanted: Option<&str>| -> PyResult<bool> {
            let value = record.get_item(key)?;
            Ok(match wanted {
                Some(wanted) => value.eq(wanted)?,
                None => value.is_none(),
            })
        };
        if !same(intern!(py, "program_id"), Some(program_id))?
            || !same(intern!(py, "effect_id"), Some(effect_id))?
            || !same(intern!(py, "zone_id"), Some(zone_id))?
            || !same(intern!(py, "end_event_id"), Some(&row.event_id))?
            || !same(intern!(py, "start_event_id"), row.causation_id.as_deref())?
        {
            return unsupported("the expiry does not match its start");
        }
        let candidate = copy_of(world)?;
        model_state(&candidate)?
            .get_item(ENVIRONMENT_STATE)?
            .get_item(intern!(py, "modifiers"))?
            .call_method1(intern!(py, "pop"), (&**modifier,))?;
        return commit(world, &candidate);
    }

    let op = required_str(payload, "op")?;
    if !matches!(
        &**op,
        "set_ambient_noise" | "modify_demand_rate" | "set_ventilation" | "set_lighting" | "set_privacy" | "set_temperature"
    ) {
        return unsupported("an unsupported operator");
    }
    if approved_op.as_deref() != Some(&**op) {
        return unsupported("the operator differs from its program");
    }
    if !attribute(world, intern!(py, "zones"))?.contains(&**zone_id)? {
        return unsupported("the zone does not exist");
    }
    if modifiers.contains(&**modifier)? {
        return unsupported("the modifier is already active");
    }
    let parsed = parameters(py, op, &payload_item(event, "parameters")?)?;
    if &**op == "modify_demand_rate" {
        let action = attribute(&parsed, intern!(py, "action"))?;
        let action = exact_str(&action)?;
        if matches!(action, "fetch_water" | "collect_meal") && !table.contains("resource_service_v1")? {
            return unsupported("the action requires the resource-service policy");
        }
    }
    let end_minute = match payload.get("end_minute") {
        Some(V::Int(minute)) => *minute,
        _ => return unsupported("the end minute is not an int"),
    };
    let duration = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
    if end_minute <= row.due_minute || end_minute >= duration {
        return unsupported("the end minute is outside the Run");
    }
    let mut expiries = Vec::new();
    for pending in pending_of(world)?.try_iter()? {
        let pending = pending?;
        let event_type = attribute(&pending, intern!(py, "event_type"))?;
        if exact_str(&event_type)? != "effect_program_environment_expire" {
            continue;
        }
        let payload = attribute(&pending, intern!(py, "payload"))?;
        if get_or_none(&payload, "modifier_id")?.is_some_and(|value| value.eq(&**modifier).unwrap_or(false)) {
            expiries.push(pending);
        }
    }
    let [expiry] = expiries.as_slice() else {
        return unsupported("no unique expiry event");
    };
    if !ints_equal(&attribute(expiry, intern!(py, "due_minute"))?, end_minute)? {
        return unsupported("the expiry event is at another minute");
    }
    let end_event_id = attribute(expiry, intern!(py, "event_id"))?;
    let config_data = attribute(world, intern!(py, "model_configs"))?.get_item("zone_environment")?;
    let config = py
        .import(intern!(py, "sheltersim.domain.environment"))?
        .getattr(intern!(py, "ScenarioZoneEnvironmentConfig"))?
        .call_method1(intern!(py, "model_validate"), (config_data,))?;
    let policy_version = attribute(&config, intern!(py, "policy_version"))?;
    if matches!(&**op, "set_ventilation" | "set_lighting" | "set_privacy" | "set_temperature")
        && !policy_version.eq("zone-environment-v2")?
    {
        return unsupported("the operator requires zone-environment-v2");
    }
    let Some(V::List(_)) = marker.get("assumption_ids") else {
        return unsupported("the assumption provenance is invalid");
    };
    let assumptions = str_list(marker, "assumption_ids")?;
    let assumptions = sorted_unique(assumptions.iter().map(|item| &***item));

    let record = PyDict::new(py);
    record.set_item("modifier_id", &**modifier)?;
    record.set_item("program_id", &**program_id)?;
    record.set_item("effect_id", &**effect_id)?;
    record.set_item("op", &**op)?;
    record.set_item("zone_id", &**zone_id)?;
    let keywords = PyDict::new(py);
    keywords.set_item("mode", "json")?;
    record.set_item(
        "parameters",
        parsed.call_method(intern!(py, "model_dump"), (), Some(&keywords))?,
    )?;
    record.set_item("start_event_id", &*row.event_id)?;
    record.set_item("end_event_id", end_event_id)?;
    record.set_item("start_minute", row.due_minute)?;
    record.set_item("end_minute", end_minute)?;
    record.set_item("source_id", attribute(&config, intern!(py, "source_id"))?)?;
    record.set_item("assumption_ids", PyList::new(py, assumptions)?)?;

    let candidate = copy_of(world)?;
    model_state(&candidate)?
        .get_item(ENVIRONMENT_STATE)?
        .get_item(intern!(py, "modifiers"))?
        .set_item(&**modifier, record)?;
    commit(world, &candidate)
}

/// `EffectProgramEnvironmentHandler.apply`.
#[pyfunction]
fn environment(handler: &Bound<'_, PyAny>, world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let result = expire_of(handler).and_then(|expire| environment_rule(world, event, expire));
    counted(py, "environment", refused_by_python(py, result))
}

// ---- EffectProgramTemporarySpaceHandler ----------------------------------------------------------

const TEMPORARY_SPACES: &str = "temporary_spaces_v1";
const TEMPORARY_MODULE: &str = "sheltersim.domain.temporary_spaces";

fn temporary_space_rule(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, expire: bool) -> PyResult<()> {
    let py = world.py();
    let row = event_row(event)?;
    let payload = map_of(&row.payload)?;
    let table = model_state(world)?;
    let Some(state) = get_or_none(&table, TEMPORARY_SPACES)? else {
        return unsupported("no temporary_spaces_v1");
    };
    if !is_dict(&state) {
        return unsupported("temporary_spaces_v1 is another value");
    }
    let program_id = required_str(payload, "program_id")?;
    let effect_id = required_str(payload, "effect_id")?;
    let modifier = required_str(payload, "modifier_id")?;
    let zone_id = required_str(payload, "zone_id")?;
    let marker = match map_of(&row.provenance)?.get(MARKER) {
        Some(V::Map(marker)) => marker,
        _ => return unsupported("no marker"),
    };
    let approved = program_state(world, |state| {
        Ok(state.and_then(|state| state.programs.get(&**program_id)).is_some_and(|program| {
            effect_is_scheduled(program, effect_id)
                && program.operators.get(&**effect_id).is_some_and(|op| &**op == "add_temporary_space")
        }))
    })?;
    if !approved
        || **modifier != *modifier_id(program_id, effect_id)
        || !str_list(marker, "policy_dependencies")?.iter().any(|item| &***item == TEMPORARY_SPACES)
        || !attribute(world, intern!(py, "zones"))?.contains(&**zone_id)?
    {
        return unsupported("the event does not reference an approved installed effect");
    }
    if expire {
        if !has_exactly(payload, &ZONE_EXPIRE_FIELDS) {
            return unsupported("the payload has other fields");
        }
        let spaces = state.get_item(intern!(py, "spaces"))?;
        let Some(space) = get_or_none(&spaces, modifier)? else {
            return unsupported("the space did not start");
        };
        if !is_dict(&space) || !space.get_item(intern!(py, "active"))?.is_truthy()? {
            return unsupported("the space is not active");
        }
        let start_event = space.get_item(intern!(py, "start_event_id"))?;
        let starts_this = match &row.causation_id {
            Some(cause) => start_event.eq(&**cause)?,
            None => start_event.is_none(),
        };
        if !space.get_item(intern!(py, "zone_id"))?.eq(&**zone_id)?
            || !space.get_item(intern!(py, "program_id"))?.eq(&**program_id)?
            || !space.get_item(intern!(py, "effect_id"))?.eq(&**effect_id)?
            || !starts_this
            || !space.get_item(intern!(py, "end_minute"))?.eq(row.due_minute)?
        {
            return unsupported("the space does not match its expiry");
        }
        let candidate = copy_of(world)?;
        let keywords = PyDict::new(py);
        keywords.set_item("event_id", &*row.event_id)?;
        keywords.set_item("causation_id", row.causation_id.as_deref())?;
        let expired = call_rule(py, TEMPORARY_MODULE, "expire_temporary_space", (&candidate, &**modifier), Some(&keywords))?;
        if !expired.is_truthy()? {
            return unsupported("the space is already expired");
        }
        return commit(world, &candidate);
    }
    if !has_exactly(payload, &ENVIRONMENT_APPLY_FIELDS) || str_of(payload, "op").is_none_or(|op| &**op != "add_temporary_space") {
        return unsupported("the payload has other fields");
    }
    guard_passes(world, &row)?;
    let parsed = parameters(py, "add_temporary_space", &payload_item(event, "parameters")?)?;
    let end_minute = match payload.get("end_minute") {
        Some(V::Int(minute)) => *minute,
        _ => return unsupported("the end minute is not an int"),
    };
    let duration = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
    if row.due_minute >= end_minute || end_minute >= duration {
        return unsupported("the end minute is outside its interval");
    }
    // sorted(set([event.source_id, *assumption_ids])); None doesn't sort with a str.
    let Some(source) = row.source_id.as_deref() else {
        return unsupported("no source");
    };
    let assumptions = str_list(marker, "assumption_ids")?;
    let sources = sorted_unique(std::iter::once(source).chain(assumptions.iter().map(|item| &***item)));
    let record = PyDict::new(py);
    record.set_item("space_id", &**modifier)?;
    record.set_item("zone_id", &**zone_id)?;
    record.set_item("people_capacity", attribute(&parsed, intern!(py, "people_capacity"))?)?;
    record.set_item("sleeping_capacity", attribute(&parsed, intern!(py, "sleeping_capacity"))?)?;
    record.set_item("start_minute", row.due_minute)?;
    record.set_item("end_minute", end_minute)?;
    record.set_item("active", true)?;
    record.set_item("start_event_id", &*row.event_id)?;
    record.set_item("end_event_id", py.None())?;
    record.set_item("program_id", &**program_id)?;
    record.set_item("effect_id", &**effect_id)?;
    record.set_item("source_ids", PyList::new(py, sources)?)?;
    record.set_item("geometry_status", "zone_only_capacity")?;
    let candidate = copy_of(world)?;
    let keywords = PyDict::new(py);
    keywords.set_item("event_id", &*row.event_id)?;
    call_rule(py, TEMPORARY_MODULE, "register_temporary_space", (&candidate, record), Some(&keywords))?;
    commit(world, &candidate)
}

/// `EffectProgramTemporarySpaceHandler.apply`.
#[pyfunction]
fn temporary_space(handler: &Bound<'_, PyAny>, world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let result = expire_of(handler).and_then(|expire| temporary_space_rule(world, event, expire));
    counted(py, "temporary_space", refused_by_python(py, result))
}

// ---- EffectProgramGeometryHandler ----------------------------------------------------------------

const GEOMETRY_FIELDS: [&str; 7] = ["program_id", "effect_id", "modifier_id", "target_id", "op", "parameters", "end_minute"];
const GEOMETRY_OPERATORS: [&str; 5] =
    ["move_facility", "transfer_equipment", "set_partition", "set_route_access", "temporary_room_geometry"];

fn geometry_rule(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, expire: bool) -> PyResult<()> {
    let py = world.py();
    let row = event_row(event)?;
    let payload = map_of(&row.payload)?;
    if !has_exactly(payload, &GEOMETRY_FIELDS) {
        return unsupported("the payload has other fields");
    }
    let op = required_str(payload, "op")?;
    if !GEOMETRY_OPERATORS.contains(&&**op) {
        return unsupported("not an operator of the floor plan");
    }
    let program_id = required_str(payload, "program_id")?;
    let effect_id = required_str(payload, "effect_id")?;
    let modifier = required_str(payload, "modifier_id")?;
    let approved = program_state(world, |state| {
        Ok(state
            .and_then(|state| state.programs.get(&**program_id))
            .and_then(|program| program.operators.get(&**effect_id))
            .is_some_and(|approved| approved == op))
    })?;
    if !approved || **modifier != *modifier_id(program_id, effect_id) {
        return unsupported("the effect does not reference its approved program");
    }
    guard_passes(world, &row)?;
    if expire {
        let table = model_state(world)?;
        let Some(overlay) = get_or_none(&table, "geometry_overlay_v1")? else {
            return unsupported("no modifier to expire");
        };
        let active = match &**op {
            "set_partition" => match get_or_none(&overlay, "partitions")? {
                Some(partitions) => partitions.contains(&**modifier)?,
                None => false,
            },
            "temporary_room_geometry" => {
                let parameters = map_of(payload.get("parameters").unwrap_or(&V::Null))?;
                let room_id = required_str(parameters, "room_id")?;
                let room = match get_or_none(&overlay, "temporary_rooms")? {
                    Some(rooms) => get_or_none(&rooms, room_id)?,
                    None => None,
                };
                match room {
                    Some(room) if is_dict(&room) => {
                        let started = get_or_none(&room, "start_event_id")?;
                        let cause_matches = match (&started, &row.causation_id) {
                            (Some(started), Some(cause)) => started.eq(&**cause)?,
                            (None, None) => true,
                            _ => false,
                        };
                        let ends = get_or_none(&room, "end_minute")?;
                        get_or_none(&room, "active")?.is_some_and(|active| active.downcast_exact::<pyo3::types::PyBool>().is_ok_and(|flag| flag.is_true()))
                            && cause_matches
                            && ends.is_some_and(|ends| ends.eq(row.due_minute).unwrap_or(false))
                    }
                    _ => false,
                }
            }
            _ => {
                let target = required_str(payload, "target_id")?;
                let modifiers = match get_or_none(&overlay, "route_modifiers")? {
                    Some(routes) => match get_or_none(&routes, target)? {
                        Some(route) => get_or_none(&route, "modifiers")?,
                        None => None,
                    },
                    None => None,
                };
                match modifiers {
                    Some(modifiers) => modifiers.contains(&**modifier)?,
                    None => false,
                }
            }
        };
        if !active {
            return unsupported("the modifier was not applied or has expired");
        }
    }
    let candidate = copy_of(world)?;
    let keywords = PyDict::new(py);
    keywords.set_item("expire", expire)?;
    call_rule(py, "sheltersim.domain.geometry_overlay", "apply_geometry_change", (&candidate, event), Some(&keywords))?;
    commit(world, &candidate)
}

/// `EffectProgramGeometryHandler.apply`.
#[pyfunction]
fn geometry(handler: &Bound<'_, PyAny>, world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let result = expire_of(handler).and_then(|expire| geometry_rule(world, event, expire));
    counted(py, "geometry", refused_by_python(py, result))
}

// ---- EffectProgramStructureHandler ---------------------------------------------------------------

const STAFF_MODULE: &str = "sheltersim.domain.staff_operations";
const CARE_MODULE: &str = "sheltersim.domain.care_receipts";
const SHARED_OPERATIONS: &str = "shared_operations_v1";

/// What the operators of a marker with an approved program have in common.
fn approved_marked_effect(
    world: &Bound<'_, PyAny>,
    row: &EventRow,
    payload: &Doc,
    op: &str,
    target: &str,
) -> PyResult<()> {
    let marker = match map_of(&row.provenance)?.get(MARKER) {
        Some(V::Map(marker)) => marker,
        _ => return unsupported("no marker"),
    };
    let program_id = required_str(payload, "program_id")?;
    let effect_id = required_str(payload, "effect_id")?;
    let matches_marker = str_of(marker, "program_id") == Some(program_id)
        && str_of(marker, "effect_id") == Some(effect_id)
        && str_of(marker, "op").is_some_and(|item| &**item == op)
        && str_of(marker, "phase").is_some_and(|item| &**item == "start");
    let approved = program_state(world, |state| {
        Ok(state.and_then(|state| state.programs.get(&**program_id)).is_some_and(|program| {
            effect_is_scheduled(program, effect_id)
                && program.operators.get(&**effect_id).is_some_and(|item| &**item == op)
                && program.scheduled_event_ids.iter().any(|item| *item == row.event_id)
        }))
    })?;
    if !matches_marker
        || !approved
        || str_of(payload, "op").is_none_or(|item| &**item != op)
        || str_of(payload, "modifier_id").is_none_or(|item| **item != *modifier_id(program_id, effect_id))
        || str_of(payload, target).is_none()
    {
        return unsupported("the effect does not reference its approved program");
    }
    Ok(())
}

fn staff_task_order(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, row: &EventRow) -> PyResult<()> {
    let py = world.py();
    let payload = map_of(&row.payload)?;
    approved_marked_effect(world, row, payload, "staff_task_order", "staff_id")?;
    if !ints_equal(&attribute(world, intern!(py, "minute_index"))?, row.due_minute)? {
        return unsupported("the order is not for this minute");
    }
    let order = parameters(py, "staff_task_order", &payload_item(event, "parameters")?)?;
    guard_passes(world, row)?;
    let candidate = copy_of(world)?;
    let keywords = PyDict::new(py);
    keywords.set_item("staff_id", &**required_str(payload, "staff_id")?)?;
    keywords.set_item("task_kind", attribute(&order, intern!(py, "task_kind"))?)?;
    keywords.set_item("target_zone_id", attribute(&order, intern!(py, "target_zone_id"))?)?;
    keywords.set_item("target_id", attribute(&order, intern!(py, "target_id"))?)?;
    keywords.set_item("reason", attribute(&order, intern!(py, "reason"))?)?;
    keywords.set_item("causation_event_id", &*row.event_id)?;
    keywords.set_item("source_id", row.source_id.as_deref())?;
    call_rule(py, STAFF_MODULE, "order_staff_operation", (&candidate,), Some(&keywords))?;
    commit(world, &candidate)
}

/// `_staff_has_task_skill`.
fn has_task_skill(skills: &Bound<'_, PyAny>, task_kind: &Bound<'_, PyAny>) -> PyResult<bool> {
    let wanted: [&str; 2] = match task_kind.downcast_exact::<PyString>().map(|kind| kind.to_str()) {
        Ok(Ok("cleaning")) => ["cleaning", "sanitation"],
        Ok(Ok("maintenance")) => ["maintenance", "repair"],
        Ok(Ok("information_outreach")) => ["support", "information"],
        Ok(Ok("intake")) => ["support", "intake"],
        Ok(Ok("patrol")) => ["support", "patrol"],
        Ok(Ok(_)) => return Ok(false),
        _ => return unsupported("a task kind is not a plain str"),
    };
    for skill in skills.try_iter()? {
        let skill = skill?;
        if wanted.contains(&exact_str(&skill)?) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn idle_record(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let record = PyDict::new(py);
    for key in [
        "phase",
        "task_id",
        "task_kind",
        "target_id",
        "target_zone_id",
        "reason",
        "started_minute",
        "travel_due_minute",
        "arrival_minute",
        "due_minute",
    ] {
        record.set_item(key, py.None())?;
    }
    record.set_item("phase", "idle")?;
    record.set_item("route_zone_ids", PyList::empty(py))?;
    record.set_item("route_edge_ids", PyList::empty(py))?;
    record.set_item("travel_minutes", 0)?;
    record.set_item("causation_event_ids", PyList::empty(py))?;
    Ok(record)
}

/// The checks and the copy of `staff_handoff`: everything but the receipt, which is after the swap.
fn staff_handoff_on_copy<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    row: &EventRow,
) -> PyResult<(Bound<'py, PyAny>, String, String, String, String)> {
    let py = world.py();
    let payload = map_of(&row.payload)?;
    approved_marked_effect(world, row, payload, "staff_handoff", "staff_id")?;
    let staff_id = required_str(payload, "staff_id")?;
    let handoff = parameters(py, "staff_handoff", &payload_item(event, "parameters")?)?;
    let task_id: String = attribute(&handoff, intern!(py, "task_id"))?.extract()?;
    let reason_code: String = attribute(&handoff, intern!(py, "reason_code"))?.extract()?;
    let replacement_id = attribute(&handoff, intern!(py, "replacement_staff_id"))?;
    if replacement_id.is_none() {
        return unsupported("no replacement");
    }
    let replacement_id: String = replacement_id.extract()?;
    let known = attribute(world, intern!(py, "staff"))?;
    if !attribute(&handoff, intern!(py, "staff_id"))?.eq(&**staff_id)?
        || !known.contains(&**staff_id)?
        || !known.contains(&*replacement_id)?
        || !attribute(world, intern!(py, "active_tasks"))?.contains(&*task_id)?
        || !model_state(world)?.contains(SHARED_OPERATIONS)?
    {
        return unsupported("the participants or the task are invalid");
    }
    guard_passes(world, row)?;

    let candidate = copy_of(world)?;
    let minute = exact_int(&attribute(&candidate, intern!(py, "minute_index"))?)?;
    let table = model_state(&candidate)?;
    let Some(operations) = get_or_none(&table, "staff_operations_v1")? else {
        return unsupported("no staff_operations_v1");
    };
    if !is_dict(&operations) {
        return unsupported("staff_operations_v1 is another value");
    }
    let records = operations.get_item(intern!(py, "staff"))?;
    let (Some(old_record), Some(replacement_record)) =
        (get_or_none(&records, staff_id)?, get_or_none(&records, &replacement_id)?)
    else {
        return unsupported("no operation record");
    };
    let task = attribute(&candidate, intern!(py, "active_tasks"))?.get_item(&*task_id)?;
    let staff_map = attribute(&candidate, intern!(py, "staff"))?;
    let old_staff = staff_map.get_item(&**staff_id)?;
    let replacement = staff_map.get_item(&*replacement_id)?;
    let task_due = exact_int(&attribute(&task, intern!(py, "due_minute"))?)?;
    let target_zone = get_or_none(&old_record, "target_zone_id")?;
    let in_target_zone = |person: &Bound<'_, PyAny>| -> PyResult<bool> {
        let zone = attribute(person, intern!(py, "zone_id"))?;
        Ok(match &target_zone {
            Some(target) => zone.eq(target)?,
            None => zone.is_none(),
        })
    };
    let task_kind = old_record.call_method1(intern!(py, "get"), ("task_kind",))?;
    let phase_is = |record: &Bound<'_, PyAny>, phase: &str| -> PyResult<bool> {
        Ok(get_or_none(record, "phase")?.is_some_and(|value| value.eq(phase).unwrap_or(false)))
    };
    if !attribute(&task, intern!(py, "kind"))?.eq("staff_operation")?
        || !attribute(&task, intern!(py, "staff_id"))?.eq(&**staff_id)?
        || task_due <= minute
        || !is_dict(&old_record)
        || !phase_is(&old_record, "working")?
        || !get_or_none(&old_record, "task_id")?.is_some_and(|id| id.eq(&*task_id).unwrap_or(false))
        || !is_dict(&replacement_record)
        || !phase_is(&replacement_record, "idle")?
        || !attribute(&replacement, intern!(py, "active_task_id"))?.is_none()
        || !in_target_zone(&old_staff)?
        || !in_target_zone(&replacement)?
        || !has_task_skill(&attribute(&replacement, intern!(py, "skills"))?, &task_kind)?
    {
        return unsupported("the replacement isn't a qualified idle one at the active work");
    }
    let replacement_text = PyString::new(py, &replacement_id);
    if !is_available(staff_status(&candidate)?.as_ref(), &replacement_text)? {
        return unsupported("the replacement is unavailable");
    }
    let on_duty = call_rule(
        py,
        "sheltersim.domain.staff_dynamics",
        "staff_dynamics_is_available",
        (&candidate, &replacement_text, minute),
        None,
    )?;
    if !on_duty.is_truthy()? {
        return unsupported("the replacement is unavailable");
    }
    let mut completions = Vec::new();
    for pending in pending_of(&candidate)?.try_iter()? {
        let pending = pending?;
        if !attribute(&pending, intern!(py, "event_type"))?.eq("staff_operation_complete")? {
            continue;
        }
        let payload = attribute(&pending, intern!(py, "payload"))?;
        if get_or_none(&payload, "task_id")?.is_some_and(|id| id.eq(&*task_id).unwrap_or(false))
            && get_or_none(&payload, "staff_id")?.is_some_and(|id| id.eq(&**staff_id).unwrap_or(false))
            && ints_equal(&attribute(&pending, intern!(py, "due_minute"))?, task_due)?
        {
            completions.push(pending);
        }
    }
    let [completion] = completions.as_slice() else {
        return unsupported("no one matching pending completion");
    };
    let document = completion.call_method0(intern!(py, "to_dict"))?;
    let new_payload = PyDict::new(py);
    new_payload.call_method1(intern!(py, "update"), (document.get_item(intern!(py, "payload"))?,))?;
    new_payload.set_item("staff_id", &*replacement_id)?;
    let keywords = PyDict::new(py);
    for name in ["event_id", "kind", "due_minute", "priority", "sequence", "event_type", "source_id", "causation_id"] {
        keywords.set_item(name, completion.getattr(name)?)?;
    }
    keywords.set_item("payload", new_payload)?;
    keywords.set_item("provenance", document.get_item(intern!(py, "provenance"))?)?;
    let replaced = py
        .import(intern!(py, "sheltersim.events.models"))?
        .getattr(intern!(py, "ScheduledEvent"))?
        .call((), Some(&keywords))?;
    attribute(&candidate, intern!(py, "event_queue"))?.call_method1(
        intern!(py, "replace"),
        (attribute(completion, intern!(py, "event_id"))?, replaced),
    )?;

    task.setattr(intern!(py, "staff_id"), &*replacement_id)?;
    attribute(&old_staff, intern!(py, "work_queue"))?.call_method0(intern!(py, "clear"))?;
    old_staff.setattr(intern!(py, "active_task_id"), py.None())?;
    old_staff.setattr(intern!(py, "busy_until"), 0)?;
    records.set_item(&**staff_id, idle_record(py)?)?;
    replacement.setattr(intern!(py, "active_task_id"), &*task_id)?;
    attribute(&replacement, intern!(py, "work_queue"))?
        .set_item(PySlice::full(py), PyList::new(py, [&*task_id])?)?;
    replacement.setattr(intern!(py, "busy_until"), task_due)?;
    let handed_off = PyDict::new(py);
    handed_off.call_method1(intern!(py, "update"), (&old_record,))?;
    handed_off.set_item("reason", &*reason_code)?;
    let mut causes: Vec<String> = Vec::new();
    for cause in old_record.get_item(intern!(py, "causation_event_ids"))?.try_iter()? {
        causes.push(exact_str(&cause?)?.to_owned());
    }
    causes.push(row.event_id.to_string());
    causes.sort_unstable();
    causes.dedup();
    handed_off.set_item("causation_event_ids", PyList::new(py, causes)?)?;
    records.set_item(&*replacement_id, handed_off)?;
    Ok((candidate, staff_id.to_string(), replacement_id, task_id, reason_code))
}

/// `(receipt[0],)` of a `make_*_record`, or None.
fn receipt_records<'py>(py: Python<'py>, receipt: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if receipt.is_none() {
        return Ok(py.None().into_bound(py));
    }
    Ok(PyTuple::new(py, [receipt.get_item(0)?])?.into_any())
}

fn staff_handoff<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    row: &EventRow,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let marker = match map_of(&row.provenance)?.get(MARKER) {
        Some(V::Map(marker)) => marker,
        _ => return unsupported("no marker"),
    };
    let sources = str_list(marker, "assumption_ids")?;
    let sources = PyTuple::new(py, sources.iter().map(|item| &***item))?;
    let (candidate, staff_id, replacement_id, task_id, reason_code) = refused_by_python(py, (|| {
        let done = staff_handoff_on_copy(world, event, row)?;
        validate(&done.0)?;
        Ok(done)
    })())?;
    // Past here the world has changed, and what is raised goes on as the Python rule's does.
    swap(world, &candidate)?;
    let keywords = PyDict::new(py);
    keywords.set_item("task_id", task_id)?;
    keywords.set_item("previous_staff_id", staff_id)?;
    keywords.set_item("replacement_staff_id", replacement_id)?;
    keywords.set_item("reason_code", reason_code)?;
    keywords.set_item("causation_id", &*row.event_id)?;
    keywords.set_item("source_ids", sources)?;
    let receipt = call_rule(py, CARE_MODULE, "make_staff_handoff_record", (world,), Some(&keywords))?;
    receipt_records(py, receipt)
}

fn facility_repair<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    row: &EventRow,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let payload = map_of(&row.payload)?;
    let plan = (|| {
        approved_marked_effect(world, row, payload, "facility_repair", "facility_id")?;
        let facility_id = required_str(payload, "facility_id")?;
        let repair = parameters(py, "facility_repair", &payload_item(event, "parameters")?)?;
        let facility_map = attribute(world, intern!(py, "facilities"))?;
        if !attribute(&repair, intern!(py, "facility_id"))?.eq(&**facility_id)? || !facility_map.contains(&**facility_id)? {
            return unsupported("the target is not a known facility");
        }
        if !model_state(world)?.contains(SHARED_OPERATIONS)? {
            return unsupported("no shared receipt state");
        }
        guard_passes(world, row)?;
        let facility = facility_map.get_item(&**facility_id)?;
        let working = exact_int(&attribute(&facility, intern!(py, "working_units"))?)?;
        let capacity = exact_int(&attribute(&facility, intern!(py, "capacity"))?)?;
        let expected = exact_int(&attribute(&repair, intern!(py, "expected_working_units"))?)?;
        let restored = exact_int(&attribute(&repair, intern!(py, "restored_working_units"))?)?;
        let modified = program_state(world, |state| {
            Ok(state
                .and_then(|state| state.active_facility_modifiers.get(&**facility_id))
                .is_some_and(|record| !record.modifiers.is_empty()))
        })?;
        if working != expected || working >= restored || restored > capacity || active_outage(world, facility_id)? || modified {
            return unsupported("the repair's preconditions no longer match");
        }
        let mut sources: Vec<String> = Vec::new();
        for source in attribute(&repair, intern!(py, "source_ids"))?.try_iter()? {
            sources.push(exact_str(&source?)?.to_owned());
        }
        sources.extend(row.source_id.as_deref().map(str::to_owned));
        sources.sort_unstable();
        sources.dedup();
        sources.retain(|source| !source.is_empty());
        Ok((facility, working, restored, attribute(&repair, intern!(py, "component_id"))?, facility_id.clone(), sources))
    })();
    let (facility, previous, restored, component, facility_id, sources) = refused_by_python(py, plan)?;
    // In place, like the Python rule: a failed check of the world now goes on as its own error.
    facility.setattr(intern!(py, "working_units"), restored)?;
    if let Some(infrastructure) = get_or_none(&model_state(world)?, "infrastructure_v1")? {
        if is_dict(&infrastructure) {
            infrastructure.get_item(intern!(py, "base_working_units"))?.set_item(&*facility_id, restored)?;
        }
    }
    validate(world)?;
    let keywords = PyDict::new(py);
    keywords.set_item("facility_id", &*facility_id)?;
    keywords.set_item("component_id", component)?;
    keywords.set_item("status", "repaired")?;
    keywords.set_item("previous_working_units", previous)?;
    keywords.set_item("working_units", attribute(&facility, intern!(py, "working_units"))?)?;
    keywords.set_item("causation_id", &*row.event_id)?;
    keywords.set_item("source_ids", PyTuple::new(py, sources)?)?;
    let receipt = call_rule(py, CARE_MODULE, "make_facility_recovery_record", (world,), Some(&keywords))?;
    receipt_records(py, receipt)
}

fn program_lists_effect(world: &Bound<'_, PyAny>, payload: &Doc) -> PyResult<bool> {
    let program_id = required_str(payload, "program_id")?;
    let effect_id = required_str(payload, "effect_id")?;
    program_state(world, |state| {
        Ok(state
            .and_then(|state| state.programs.get(&**program_id))
            .is_some_and(|program| effect_is_scheduled(program, effect_id)))
    })
}

fn set_staff_role(world: &Bound<'_, PyAny>, row: &EventRow, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let payload = map_of(&row.payload)?;
    let staff_id = required_str(payload, "staff_id")?;
    if !program_lists_effect(world, payload)?
        || str_of(payload, "op").is_none_or(|op| &**op != "set_staff_role")
        || !attribute(world, intern!(py, "staff"))?.contains(&**staff_id)?
    {
        return unsupported("the event does not reference an approved staff target");
    }
    let raw = PyDict::new(py);
    raw.set_item("skills", payload_item(event, "skills")?)?;
    let role = parameters(py, "set_staff_role", raw.as_any())?;
    let candidate = copy_of(world)?;
    let staff = attribute(&candidate, intern!(py, "staff"))?.get_item(&**staff_id)?;
    if !attribute(&staff, intern!(py, "active_task_id"))?.is_none() {
        return unsupported("the worker has an active task");
    }
    if let Some(operations) = get_or_none(&model_state(&candidate)?, "staff_operations_v1")? {
        if let Some(record) = get_or_none(&operations.get_item(intern!(py, "staff"))?, staff_id)? {
            if is_dict(&record) && !get_or_none(&record, "phase")?.is_some_and(|phase| phase.eq("idle").unwrap_or(false)) {
                return unsupported("the worker has active work");
            }
        }
    }
    let mut skills: Vec<String> = Vec::new();
    for skill in attribute(&role, intern!(py, "skills"))?.try_iter()? {
        skills.push(exact_str(&skill?)?.to_owned());
    }
    skills.sort_unstable();
    staff.setattr(intern!(py, "skills"), PyTuple::new(py, skills)?)?;
    commit(world, &candidate)
}

/// `add_staff` of `domain/staff_dynamics.py` and `add_staff_operations_agent`, on the copy.
fn add_one_staff(py: Python<'_>, candidate: &Bound<'_, PyAny>, staff_id: &str, zone_id: &str) -> PyResult<()> {
    let table = model_state(candidate)?;
    let keywords = PyDict::new(py);
    keywords.set_item("id", staff_id)?;
    keywords.set_item("zone_id", zone_id)?;
    keywords.set_item("skills", PyTuple::new(py, ["support"])?)?;
    let member = py.import(intern!(py, "sheltersim.domain.models"))?.getattr(intern!(py, "StaffState"))?.call((), Some(&keywords))?;
    attribute(candidate, intern!(py, "staff"))?.set_item(staff_id, member)?;
    if let Some(operational) = get_or_none(&table, "operational_events_v1")? {
        let status = PyDict::new(py);
        status.set_item("available", true)?;
        status.set_item("unavailability_event_id", py.None())?;
        status.set_item("until_minute", py.None())?;
        status.set_item("restore_event_id", py.None())?;
        operational.get_item(intern!(py, "staff_status"))?.set_item(staff_id, status)?;
    }
    if let Some(dynamics) = get_or_none(&table, "staff_dynamics_v1")? {
        let record = PyDict::new(py);
        record.set_item("consecutive_work_minutes", 0)?;
        record.set_item("total_work_minutes", 0)?;
        record.set_item("total_rest_minutes", 0)?;
        record.set_item("fatigue", 0.0)?;
        record.set_item("rest_until_minute", 0)?;
        record.set_item("last_activity", "off_duty")?;
        dynamics.get_item(intern!(py, "staff"))?.set_item(staff_id, record)?;
    }
    if let Some(operations) = get_or_none(&table, "staff_operations_v1")? {
        operations.get_item(intern!(py, "staff"))?.set_item(staff_id, idle_record(py)?)?;
    }
    Ok(())
}

fn add_one_toilet(py: Python<'_>, candidate: &Bound<'_, PyAny>, facility_id: &str, zone_id: &str, row: &EventRow) -> PyResult<()> {
    const OVERLAY: &str = "sheltersim.domain.geometry_overlay";
    let keywords = PyDict::new(py);
    keywords.set_item("id", facility_id)?;
    keywords.set_item("kind", "toilet")?;
    keywords.set_item("zone_id", zone_id)?;
    keywords.set_item("capacity", 1)?;
    keywords.set_item("working_units", 1)?;
    let toilet = py.import(intern!(py, "sheltersim.domain.models"))?.getattr(intern!(py, "FacilityState"))?.call((), Some(&keywords))?;
    attribute(candidate, intern!(py, "facilities"))?.set_item(facility_id, toilet)?;
    if call_rule(py, OVERLAY, "installed_facility_placement_enabled", (candidate,), None)?.is_truthy()? {
        let keywords = PyDict::new(py);
        keywords.set_item("event_id", &*row.event_id)?;
        let sources = match row.source_id.as_deref() {
            Some(source) if !source.is_empty() => PyTuple::new(py, [source])?,
            _ => PyTuple::empty(py),
        };
        keywords.set_item("source_ids", sources)?;
        call_rule(py, OVERLAY, "place_installed_facility", (candidate, facility_id), Some(&keywords))?;
    }
    call_rule(py, "sheltersim.domain.agent_dynamics", "announce_installed_facility", (candidate, facility_id), None)?;
    Ok(())
}

fn add_structure(world: &Bound<'_, PyAny>, row: &EventRow) -> PyResult<&'static str> {
    let py = world.py();
    let payload = map_of(&row.payload)?;
    if !has_exactly(payload, &["program_id", "effect_id", "modifier_id", "op", "zone_id", "count"]) {
        return unsupported("the payload has other fields");
    }
    let op = required_str(payload, "op")?;
    let zone_id = required_str(payload, "zone_id")?;
    let count = match payload.get("count") {
        Some(V::Int(count)) if *count > 0 => *count,
        _ => return unsupported("the count is not a positive int"),
    };
    if !program_lists_effect(world, payload)?
        || !matches!(&**op, "add_staff" | "add_toilet")
        || !attribute(world, intern!(py, "zones"))?.contains(&**zone_id)?
    {
        return unsupported("the target or the operation is invalid");
    }
    let modifier = required_str(payload, "modifier_id")?;
    let suffix = modifier.strip_prefix("modifier-").unwrap_or(modifier);
    let candidate = copy_of(world)?;
    if &**op == "add_staff" {
        let staff = attribute(&candidate, intern!(py, "staff"))?;
        if staff.len()? as i64 + count > 100 {
            return unsupported("the 100-staff bound");
        }
        for index in 1..=count {
            let staff_id = format!("effect-staff-{suffix}-{index}");
            if staff.contains(&*staff_id)? {
                return unsupported("the generated staff ID exists");
            }
            add_one_staff(py, &candidate, &staff_id, zone_id)?;
        }
        if model_state(&candidate)?.contains("movement_v1")? {
            call_rule(py, "sheltersim.domain.movement", "staff_position_snapshots", (&candidate,), None)?;
        }
    } else {
        let facilities = attribute(&candidate, intern!(py, "facilities"))?;
        if facilities.len()? as i64 + count > 200 {
            return unsupported("the 200-facility bound");
        }
        for index in 1..=count {
            let facility_id = format!("effect-toilet-{suffix}-{index}");
            if facilities.contains(&*facility_id)? {
                return unsupported("the generated toilet ID exists");
            }
            add_one_toilet(py, &candidate, &facility_id, zone_id, row)?;
        }
    }
    commit(world, &candidate)?;
    Ok(if &**op == "add_staff" { "structure.add_staff" } else { "structure.add_toilet" })
}

/// `EffectProgramStructureHandler.apply`. Its branches are told apart by the payload's fields, in
/// the Python rule's order.
#[pyfunction]
fn structure<'py>(
    _handler: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let none = || py.None().into_bound(py);
    let row = refused_by_python(py, event_row(event))?;
    let payload = refused_by_python(py, map_of(&row.payload))?;
    let base = ["program_id", "effect_id", "modifier_id", "op"];
    let shape = |extra: &[&str]| payload.len() == 4 + extra.len() && base.iter().chain(extra).all(|name| payload.contains_key(*name));
    if shape(&["staff_id", "parameters"]) {
        if str_of(payload, "op").is_some_and(|op| &**op == "staff_task_order") {
            let result = staff_task_order(world, event, &row).map(|()| none());
            return counted(py, "structure.staff_task_order", refused_by_python(py, result));
        }
        return counted(py, "structure.staff_handoff", staff_handoff(world, event, &row));
    }
    if shape(&["facility_id", "parameters"]) {
        return counted(py, "structure.facility_repair", facility_repair(world, event, &row));
    }
    if shape(&["staff_id", "skills"]) {
        let result = set_staff_role(world, &row, event).map(|()| none());
        return counted(py, "structure.set_staff_role", refused_by_python(py, result));
    }
    let result = add_structure(world, &row).map(|label| {
        if let Ok(mut ran) = RAN.lock() {
            *ran.entry(label).or_insert(0) += 1;
        }
        none()
    });
    counted(py, "structure.add", refused_by_python(py, result))
}

// ---- validate_effect_program_state -------------------------------------------------------------

/// `DEFAULT_PRIMITIVE_REGISTRY`, which a test compares with this list.
const OPERATORS: [&str; 27] = [
    "move_facility",
    "transfer_equipment",
    "set_partition",
    "set_route_access",
    "temporary_room_geometry",
    "set_facility_availability",
    "facility_outage",
    "facility_repair",
    "staff_handoff",
    "staff_task_order",
    "deliver_resource",
    "modify_supply_schedule",
    "set_staff_availability",
    "set_staff_role",
    "add_staff",
    "add_toilet",
    "add_evacuees",
    "depart_evacuees",
    "population_presence",
    "add_temporary_space",
    "water_outage",
    "set_ambient_noise",
    "modify_demand_rate",
    "set_ventilation",
    "set_lighting",
    "set_privacy",
    "set_temperature",
];

fn expected_event_type(phase: &str, op: &str) -> Option<&'static str> {
    Some(match (phase, op) {
        ("start", "set_facility_availability" | "facility_outage") => "effect_program_apply",
        ("start", "facility_repair" | "staff_handoff" | "staff_task_order" | "set_staff_role" | "add_staff" | "add_toilet") => {
            "effect_program_structure"
        }
        ("start", "deliver_resource") => "resource_delivery",
        ("start", "modify_supply_schedule") => "delivery_delayed",
        ("start", "set_staff_availability") => "staff_unavailable",
        ("start", "water_outage") => "water_outage",
        ("start", "set_ambient_noise" | "modify_demand_rate" | "set_ventilation" | "set_lighting" | "set_privacy" | "set_temperature") => {
            "effect_program_environment"
        }
        ("start", "add_evacuees" | "depart_evacuees" | "population_presence") => "effect_program_population",
        ("start", "add_temporary_space") => "effect_program_temporary_space",
        ("start", "move_facility" | "transfer_equipment" | "set_partition" | "set_route_access" | "temporary_room_geometry") => {
            "effect_program_geometry"
        }
        ("expiry", "set_facility_availability" | "facility_outage") => "effect_program_expire",
        ("expiry", "water_outage") => "water_restored",
        ("expiry", "set_ambient_noise" | "modify_demand_rate" | "set_ventilation" | "set_lighting" | "set_privacy" | "set_temperature") => {
            "effect_program_environment_expire"
        }
        ("expiry", "add_temporary_space") => "effect_program_temporary_space_expire",
        ("expiry", "set_partition" | "set_route_access" | "temporary_room_geometry") => "effect_program_geometry_expire",
        ("derived", "modify_supply_schedule") => "resource_delivery",
        ("derived", "set_staff_availability") => "staff_restored",
        ("derived", "add_evacuees") => "population_approach_complete",
        _ => return None,
    })
}

const MARKER_FIELDS: [&str; 9] = [
    "program_id",
    "effect_id",
    "op",
    "source_text_hash",
    "compiler_provenance",
    "assumption_ids",
    "policy_dependencies",
    "units",
    "phase",
];

/// `CompilerProvenance.model_validate`, for a value the contract surely takes.
fn compiler_provenance_passes(value: &V) -> bool {
    let V::Map(map) = value else {
        return false;
    };
    if map.keys().any(|key| !matches!(&**key, "compiler_kind" | "compiler_version" | "model_id" | "prompt_hash")) {
        return false;
    }
    let optional_id = match map.get("model_id") {
        None | Some(V::Null) => true,
        Some(V::Str(id)) => is_stable_id(id),
        Some(_) => false,
    };
    let optional_hash = match map.get("prompt_hash") {
        None | Some(V::Null) => true,
        Some(V::Str(hash)) => hash.chars().count() <= 128,
        Some(_) => false,
    };
    optional_id
        && optional_hash
        && str_of(map, "compiler_kind").is_some_and(|kind| matches!(&**kind, "offline_rule" | "llm"))
        && str_of(map, "compiler_version").is_some_and(|version| is_stable_id(version))
}

/// `PlanAssumption.model_validate`, and its ID for the check that they differ.
fn assumption_id(value: &V) -> Option<&Arc<str>> {
    let V::Map(map) = value else {
        return None;
    };
    if map.keys().any(|key| !matches!(&**key, "assumption_id" | "description" | "source_id" | "units" | "value")) {
        return None;
    }
    let units = match map.get("units") {
        None | Some(V::Null) => true,
        Some(V::Str(units)) => is_stable_id(units),
        Some(_) => false,
    };
    let scalar = match map.get("value") {
        None | Some(V::Null | V::Str(_) | V::Int(_) | V::Bool(_)) => true,
        Some(V::Float(number)) => number.is_finite(),
        Some(_) => false,
    };
    let id = str_of(map, "assumption_id")?;
    (units
        && scalar
        && is_stable_id(id)
        && str_of(map, "source_id").is_some_and(|source| is_stable_id(source))
        && str_of(map, "description").is_some_and(|text| (1..=500).contains(&text.chars().count())))
    .then_some(id)
}

fn is_lower_hex_64(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

/// Whether a pending event has the key, reading no event whole: the only thing a tick asks here.
fn pending_has_marker(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    let writer = EventWriter::of(world)?;
    // A marker that is no mapping passes, and the Python rule knows which it is.
    let found = writer.queue().borrow().any_pending_has_provenance(py, intern!(py, MARKER))?;
    found.map_or_else(|| unsupported("a pending event is not a plain ScheduledEvent"), Ok)
}

fn marker_events<'py>(world: &Bound<'py, PyAny>) -> PyResult<Vec<EventRow>> {
    let py = world.py();
    let mut rows = Vec::new();
    for event in pending_of(world)?.try_iter()? {
        let event = event?;
        if get_or_none(&attribute(&event, intern!(py, "provenance"))?, MARKER)?.is_some() {
            rows.push(event_row(&event)?);
        }
    }
    let logged = attribute(world, intern!(py, "event_log"))?.call_method1(intern!(py, "with_provenance_object"), (MARKER,))?;
    for record in logged.try_iter()? {
        rows.push(event_row(&attribute(&record?, intern!(py, "event"))?)?);
    }
    Ok(rows)
}

fn check_state(world: &Bound<'_, PyAny>, history: bool) -> PyResult<()> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, MARKER))? {
        if history {
            let logged = attribute(world, intern!(py, "event_log"))?
                .call_method1(intern!(py, "with_provenance_object"), (MARKER,))?;
            if logged.is_truthy()? {
                return unsupported("marked events without the state");
            }
        }
        if pending_has_marker(world)? {
            return unsupported("marked events without the state");
        }
        return Ok(());
    }
    let mirror = held_program_mirror(world)?;
    if !history {
        return NamespaceMirror::with_row::<EffectProgramRow, _>(&mirror, |state| {
            if state.schema_version == 1 {
                Ok(())
            } else {
                unsupported("another schema version")
            }
        });
    }
    let events = marker_events(world)?;
    let by_id = py
        .import(intern!(py, "sheltersim.events.queue"))?
        .getattr(intern!(py, "EventsById"))?
        .call1((attribute(world, intern!(py, "event_log"))?, pending_of(world)?))?;
    let facilities = attribute(world, intern!(py, "facilities"))?;
    NamespaceMirror::with_row::<EffectProgramRow, _>(&mirror, |state| {
        if state.schema_version != 1 {
            return unsupported("another schema version");
        }
        let mut scheduled: IndexMap<&str, &str> = IndexMap::new();
        let mut known: HashSet<(&str, &str)> = HashSet::new();
        for (program_id, program) in &state.programs {
            if !is_stable_id(program_id)
                || !is_lower_hex_64(&program.program_hash)
                || !is_lower_hex_64(&program.source_text_hash)
                || !(1..=1000).contains(&program.narrative.chars().count())
                || !matches!(
                    &*program.support_mode,
                    "native" | "composed" | "approximate" | "clarification_required" | "not_simulatable"
                )
                || !compiler_provenance_passes(&program.compiler_provenance)
            {
                return unsupported("a program record the Python rule decides");
            }
            let unique: HashSet<&str> = program.effect_ids.iter().map(|id| &**id).collect();
            if unique.len() != program.effect_ids.len()
                || program.operators.len() != unique.len()
                || program.operators.keys().any(|effect| !unique.contains(&**effect))
                || program.operators.values().any(|op| !OPERATORS.contains(&&**op))
            {
                return unsupported("a program's operators the Python rule decides");
            }
            let V::List(assumptions) = &program.assumptions else {
                return unsupported("a program's assumptions the Python rule decides");
            };
            let mut assumption_ids: HashSet<&str> = HashSet::new();
            for assumption in assumptions {
                match assumption_id(assumption) {
                    Some(id) if assumption_ids.insert(id) => {}
                    _ => return unsupported("a program's assumptions the Python rule decides"),
                }
            }
            for effect_id in &program.effect_ids {
                if !is_stable_id(effect_id) {
                    return unsupported("an effect ID the Python rule decides");
                }
                known.insert((program_id, effect_id));
            }
            for event_id in &program.scheduled_event_ids {
                if !is_stable_id(event_id) || scheduled.insert(event_id, program_id).is_some() {
                    return unsupported("a scheduled event ID the Python rule decides");
                }
            }
        }
        for (event_id, program_id) in &scheduled {
            if !by_id.contains(*event_id)? {
                return unsupported("an unknown scheduled event");
            }
            let provenance = attribute(&by_id.get_item(*event_id)?, intern!(py, "provenance"))?;
            let marker = get_or_none(&provenance, MARKER)?;
            let Some(marker) = marker else {
                return unsupported("a scheduled event has no marker");
            };
            let program = marker.get_item(intern!(py, "program_id")).map_err(|_| Unsupported::new_err("a marker the Python rule decides"))?;
            if !program.eq(*program_id)? {
                return unsupported("a marker of another program");
            }
        }
        for event in &events {
            let Some(V::Map(marker)) = map_of(&event.provenance)?.get(MARKER) else {
                return unsupported("a marker that is no mapping");
            };
            let has_guard = marker.contains_key("guard");
            if marker.len() != MARKER_FIELDS.len() + usize::from(has_guard)
                || !MARKER_FIELDS.iter().all(|name| marker.contains_key(*name))
            {
                return unsupported("a marker's fields the Python rule decides");
            }
            let program_id = required_str(marker, "program_id")?;
            let effect_id = required_str(marker, "effect_id")?;
            if !known.contains(&(&**program_id, &**effect_id)) {
                return unsupported("an unknown effect");
            }
            let program = &state.programs[&**program_id];
            let op = required_str(marker, "op")?;
            if program.operators.get(&**effect_id) != Some(op)
                || str_of(marker, "source_text_hash") != Some(&program.source_text_hash)
            {
                return unsupported("a marker of another program");
            }
            let phase = required_str(marker, "phase")?;
            if !matches!(&**phase, "start" | "expiry" | "derived") {
                return unsupported("a marker's phase the Python rule decides");
            }
            let lists_of_str = [marker.get("assumption_ids"), marker.get("policy_dependencies")].iter().all(|list| {
                matches!(list, Some(V::List(items)) if items.iter().all(|item| matches!(item, V::Str(_))))
            });
            if !compiler_provenance_passes(marker.get("compiler_provenance").unwrap_or(&V::Null))
                || !lists_of_str
                || !str_of(marker, "units").is_some_and(|units| is_stable_id(units))
            {
                return unsupported("a marker's values the Python rule decides");
            }
            if has_guard {
                let mut nodes = 0;
                if &**phase != "start" || parse_guard(&marker["guard"], 1, &mut nodes).is_none() {
                    return unsupported("a marker's guard the Python rule decides");
                }
            }
            if !scheduled.contains_key(&*event.event_id) && &**phase != "derived" {
                return unsupported("an unscheduled event without the derived mark");
            }
            if &**phase == "derived" {
                match &event.causation_id {
                    Some(cause) if by_id.contains(&**cause)? => {}
                    _ => return unsupported("a derived event lacks a known parent"),
                }
            }
            if expected_event_type(phase, op) != Some(&*event.event_type) {
                return unsupported("an event type that isn't its operator's");
            }
        }
        for (facility_id, record) in &state.active_facility_modifiers {
            let Some(facility) = get_or_none(&facilities, facility_id)? else {
                return unsupported("a modifier of an unknown facility");
            };
            let capacity = exact_int(&attribute(&facility, intern!(py, "capacity"))?)?;
            let working = exact_int(&attribute(&facility, intern!(py, "working_units"))?)?;
            let baseline = record.baseline_working_units;
            if baseline < 0 || baseline > capacity || record.modifiers.is_empty() {
                return unsupported("a stack's baseline the Python rule decides");
            }
            let mut ids: HashSet<&str> = HashSet::new();
            let mut lowest = baseline;
            for item in &record.modifiers {
                if *item.modifier_id != *modifier_id(&item.program_id, &item.effect_id)
                    || !ids.insert(&item.modifier_id)
                    || !known.contains(&(&*item.program_id, &*item.effect_id))
                    || item.working_units < 0
                    || item.start_minute < 0
                    || item.sequence < 0
                    || item.working_units > baseline
                {
                    return unsupported("a stack's entry the Python rule decides");
                }
                lowest = lowest.min(item.working_units);
            }
            if working != lowest {
                return unsupported("working units that aren't the stack's");
            }
        }
        Ok(())
    })
}

/// `validate_effect_program_state`: passes, or Unsupported and the Python rule says what is wrong.
#[pyfunction]
#[pyo3(signature = (world, *, validate_event_history=true))]
fn validate_state(world: &Bound<'_, PyAny>, validate_event_history: bool) -> PyResult<()> {
    let py = world.py();
    let label = if validate_event_history { "validate_state.history" } else { "validate_state.tick" };
    counted(py, label, refused_by_python(py, check_state(world, validate_event_history)))
}

/// For the test that compares this table with `EFFECT_EVENT_TYPES`.
#[pyfunction]
fn event_type_of(phase: &str, op: &str) -> Option<&'static str> {
    expected_event_type(phase, op)
}

/// What has run in this process: `(counts, refusals by reason)`.
#[pyfunction]
fn stats<'py>(py: Python<'py>) -> PyResult<(Bound<'py, PyDict>, Bound<'py, PyDict>)> {
    let counts = PyDict::new(py);
    if let Ok(ran) = RAN.lock() {
        for (label, times) in ran.iter() {
            counts.set_item(*label, *times)?;
        }
    }
    let reasons = PyDict::new(py);
    if let Ok(refusals) = REFUSALS.lock() {
        for (reason, times) in refusals.iter() {
            reasons.set_item(reason, *times)?;
        }
    }
    Ok((counts, reasons))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(should_apply_guard, m)?)?;
    m.add_function(wrap_pyfunction!(has_active_modifier, m)?)?;
    m.add_function(wrap_pyfunction!(has_pending_modifier, m)?)?;
    m.add_function(wrap_pyfunction!(facility, m)?)?;
    m.add_function(wrap_pyfunction!(environment, m)?)?;
    m.add_function(wrap_pyfunction!(temporary_space, m)?)?;
    m.add_function(wrap_pyfunction!(geometry, m)?)?;
    m.add_function(wrap_pyfunction!(structure, m)?)?;
    m.add_function(wrap_pyfunction!(validate_state, m)?)?;
    m.add_function(wrap_pyfunction!(event_type_of, m)?)?;
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    m.add("OPERATORS", OPERATORS.to_vec())?;
    Ok(())
}
