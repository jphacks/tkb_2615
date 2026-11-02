//! The rules of `events/effects.py` that run in a tick: a guard, the five handlers of an effect's
//! events, what other modules ask of the modifier stacks, and the check of `effect_program_v1`.
//!
//! A handler's Python rule makes a copy of the world, changes the copy, checks it and swaps it in.
//! The rules here do the same through the same calls (`copy_of`, `commit`). That transaction is the
//! design's until `core.fork()`: docs/rust-port/step3-design.md.
//!
//! A suppressed or failed effect raises the Python rule's text, in its order of checks, through
//! `core/errors.rs`, with the world as it was (docs/rust-port/lanes/l8-texts-events.md). Where the
//! Python rule wraps what a call raises (`str(exc)` of `apply_geometry_change`, a pydantic contract,
//! the check of the copy), the rule makes the same call and wraps the same classes. A value the rule
//! isn't sure of, and any other exception from a call before the swap, says Unsupported with the
//! world as it was: the Python rule then runs from the start and decides. Two rules change the
//! world in place after their checks and let an exception pass, as their Python rules do:
//! `facility_repair`'s `world.validate()` and the receipts after a swap.
//!
//! The pydantic contracts of the parameters stay Python's: a rule calls `model_validate` where its
//! Python rule does, through `_PARAMETER_MODELS` of `events/effects.py`. So do the rules of modules
//! other lanes hold (`order_staff_operation`, the receipts): they're called by name, with no borrow
//! held. Of `rules/overlay.rs`, a rule whose text the handler wraps (`apply_geometry_change`,
//! `register_temporary_space`, `place_installed_facility`) is called by name too, its kernel first;
//! the others are called directly. Nothing here reads LLM output: what
//! a program may do was validated before it was scheduled, by `validate_effect_program`, which is
//! not touched (docs/rust-port/lanes/m6-effects.md).

use std::collections::{BTreeMap, HashSet};
use std::sync::{Arc, Mutex};

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::exceptions::{PyKeyError, PyTypeError, PyValueError};
use pyo3::types::{PyBool, PyDict, PyList, PySlice, PyString, PyTuple};
use sha2::{Digest, Sha256};

use crate::core::errors::{self, Outcome};
use crate::core::row::{attribute, exact_int, exact_str, namespace_is_absent, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::core::world_core::{mirror_of, raw_dict_get, NamespaceMirror};
use crate::rules::care_receipts::{facility_recovery_record, handoff_record};
use crate::rules::staff_dynamics::{is_available, staff_status};
use crate::state::effect_program::{EffectProgramRow, Program, NAME};
use crate::rules::overlay;
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

/// An exception of the stage before any change: the Python rule decides, and raises its own. An
/// outcome a kernel made with the rule's text (`core/errors.rs`) goes on as it is.
pub(crate) fn refused_by_python<T>(py: Python<'_>, result: PyResult<T>) -> PyResult<T> {
    result.map_err(|error| {
        if error.is_instance_of::<Unsupported>(py) || errors::made_here(py, &error) {
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

// ---- Python's answers on the values of a frozen payload ------------------------------------------
//
// An event's payload and provenance are frozen (`_freeze_json`): a dict is a mappingproxy, a list a
// tuple, and every other value an exact JSON type. The checks that raise a rule's text answer
// as Python's operators do on them; where Python would raise instead, they say Unsupported.

/// Whether Python can hash the value (`x in a_set`, `a_dict.get(x)`): a mappingproxy can't, and a
/// tuple can when its items can.
fn hashable(value: &V) -> bool {
    match value {
        V::Map(_) => false,
        V::List(items) => items.iter().all(hashable),
        _ => true,
    }
}

/// `value == text`, for a value that may be absent (None): only a str equals a str.
fn is_text(value: Option<&V>, text: &str) -> bool {
    matches!(value, Some(V::Str(item)) if **item == *text)
}

/// `a != b` for two values that may be absent (None), where Rust can tell what Python says.
fn differ(a: Option<&V>, b: Option<&V>) -> PyResult<bool> {
    let null = |value: Option<&V>| matches!(value, None | Some(V::Null));
    match (a, b) {
        (Some(V::Str(a)), Some(V::Str(b))) => Ok(a != b),
        (Some(V::Str(_)), _) | (_, Some(V::Str(_))) => Ok(true),
        _ if null(a) && null(b) => Ok(false),
        _ if null(a) || null(b) => Ok(true),
        _ => unsupported("two values Python compares by its own rules"),
    }
}

/// `needle in marker.get(key, [])`: absent is the empty list, a str is searched for a substring and
/// a mapping for a key. In anything else Python can't search, and raises.
fn holds(value: Option<&V>, needle: &str) -> PyResult<bool> {
    match value {
        None => Ok(false),
        Some(V::List(items)) => Ok(items.iter().any(|item| matches!(item, V::Str(item) if **item == *needle))),
        Some(V::Str(text)) => Ok(text.contains(needle)),
        Some(V::Map(map)) => Ok(map.contains_key(needle)),
        Some(_) => unsupported("`in` of a value Python can't search"),
    }
}

/// `key in mapping` for a dict of the world whose keys are str (`world.zones`, `world.staff`): any
/// other value Python can hash is not a key; one it can't hash raises.
fn key_in(mapping: &Bound<'_, PyAny>, key: Option<&V>) -> PyResult<bool> {
    match key {
        Some(V::Str(text)) => mapping.contains(&**text),
        Some(value) if !hashable(value) => unsupported("a key Python can't hash"),
        _ => Ok(false),
    }
}

/// The classes of a Python rule's `except ... as exc`.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Catch {
    Value,
    ValueOrType,
    ValueKeyOrType,
}

/// `str(exc)` of an exception a Python call raised, when it is one the rule catches; any other goes
/// on as it is. Unsupported is not a ValueError, so a kernel's refusal is never caught.
fn caught(py: Python<'_>, error: PyErr, catch: Catch) -> PyResult<String> {
    let taken = error.is_instance_of::<PyValueError>(py)
        || (catch != Catch::Value && error.is_instance_of::<PyTypeError>(py))
        || (catch == Catch::ValueKeyOrType && error.is_instance_of::<PyKeyError>(py));
    if !taken {
        return Err(error);
    }
    Ok(error.value(py).str()?.to_str()?.to_owned())
}

/// `map.get(key, {})`, as the Python rule reads a nested record.
fn get_or_empty<'py, K: IntoPyObject<'py>>(map: &Bound<'py, PyAny>, key: K) -> PyResult<Bound<'py, PyAny>> {
    let py = map.py();
    map.call_method1(intern!(py, "get"), (key, PyDict::new(py)))
}

// ---- The transaction -----------------------------------------------------------------------------

/// `MutableWorldState.from_dict(world.to_dict())`.
pub(crate) fn copy_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let class = py.import(intern!(py, "sheltersim.core.world"))?.getattr(intern!(py, "WorldState"))?;
    class.call_method1(intern!(py, "from_dict"), (world.call_method0(intern!(py, "to_dict"))?,))
}

fn validate(candidate: &Bound<'_, PyAny>) -> PyResult<()> {
    candidate.call_method0(intern!(candidate.py(), "validate"))?;
    Ok(())
}

/// `world.__dict__.clear(); world.__dict__.update(candidate.__dict__)`.
pub(crate) fn swap(world: &Bound<'_, PyAny>, candidate: &Bound<'_, PyAny>) -> PyResult<()> {
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
pub(crate) fn has_active_modifier(world: &Bound<'_, PyAny>, facility_id: &Bound<'_, PyAny>) -> PyResult<bool> {
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
pub(crate) fn active_outage(world: &Bound<'_, PyAny>, facility_id: &str) -> PyResult<bool> {
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
        let action = if expire { "expire" } else { "apply" };
        return errors::failed(py, format!("effect program {action} payload fields do not match"));
    }
    let program_id = required_str(payload, "program_id")?;
    let effect_id = required_str(payload, "effect_id")?;
    let modifier = required_str(payload, "modifier_id")?;
    let facility_id = required_str(payload, "facility_id")?;
    let scheduled = program_state(world, |state| {
        Ok(state.is_some_and(|state| state.programs.get(&**program_id).is_some_and(|program| effect_is_scheduled(program, effect_id))))
    })?;
    if !scheduled {
        return errors::failed(py, "effect program event does not reference a scheduled effect");
    }
    let known = attribute(world, intern!(py, "facilities"))?;
    let Some(facility) = get_or_none(&known, facility_id)? else {
        return errors::failed(py, "effect program references an unknown facility");
    };
    if active_outage(world, facility_id)? {
        return errors::failed(py, "facility availability modifier conflicts with an active infrastructure outage");
    }
    let working = exact_int(&attribute(&facility, intern!(py, "working_units"))?)?;
    let capacity = exact_int(&attribute(&facility, intern!(py, "capacity"))?)?;
    let occupied = occupancy(world, &facility)?;
    // Whether the facility has a stack; the modifier's units, or how many others stay; and what
    // the facility's units become.
    // The rule's outcome, if it has one, as a text to raise once the row is let go.
    let planned = program_state(world, |state| {
        let Some(state) = state else {
            return unsupported("no effect_program_v1");
        };
        let current = state.active_facility_modifiers.get(&**facility_id);
        if !expire {
            let units = match payload.get("working_units") {
                Some(V::Int(units)) => *units,
                Some(V::Big(_)) => return unsupported("working_units is past 64 bits"),
                _ => return Ok(Err((Outcome::Failed, "working_units must be an integer"))),
            };
            if units < occupied || units < 0 || units > working || units > capacity {
                return Ok(Err((Outcome::Failed, "facility availability modifier violates capacity or occupancy")));
            }
            let baseline = current.map_or(working, |record| record.baseline_working_units);
            if current.is_some_and(|record| record.modifiers.iter().any(|item| item.modifier_id == *modifier)) {
                return Ok(Err((Outcome::Suppressed, "facility availability modifier is already active")));
            }
            let mut target = baseline.min(units);
            for item in current.iter().flat_map(|record| record.modifiers.iter()) {
                target = target.min(item.working_units);
            }
            return Ok(Ok((current.is_some(), units, target)));
        }
        let Some(current) = current else {
            return Ok(Err((Outcome::Suppressed, "facility availability modifier is not active")));
        };
        if !current.modifiers.iter().any(|item| item.modifier_id == *modifier) {
            return Ok(Err((Outcome::Suppressed, "facility availability modifier already expired or never started")));
        }
        let mut target = current.baseline_working_units;
        let mut others = 0;
        for item in current.modifiers.iter().filter(|item| item.modifier_id != *modifier) {
            target = target.min(item.working_units);
            others += 1;
        }
        if target < occupied {
            return Ok(Err((Outcome::Failed, "modifier expiry would reduce capacity below active occupancy")));
        }
        Ok(Ok((true, others, target)))
    })?;
    let (has_stack, units_or_others, target) = match planned {
        Ok(planned) => planned,
        Err((outcome, text)) => return Err(errors::error(py, outcome, text)),
    };

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

const ENVIRONMENT_OPERATORS: [&str; 6] =
    ["set_ambient_noise", "modify_demand_rate", "set_ventilation", "set_lighting", "set_privacy", "set_temperature"];

/// `except (EventApplicationError, EventSuppressed): raise` before an `except ValueError`.
fn is_outcome(py: Python<'_>, error: &PyErr) -> PyResult<bool> {
    let models = py.import(intern!(py, "sheltersim.events.models"))?;
    let value = error.value(py);
    Ok(value.is_instance(&models.getattr(intern!(py, "EventApplicationError"))?)?
        || value.is_instance(&models.getattr(intern!(py, "EventSuppressed"))?)?)
}

/// The handler's `except ValueError as exc`, around its rule and the check of the copy.
fn environment_invalid<T>(py: Python<'_>, result: PyResult<T>) -> PyResult<T> {
    match result {
        Err(error) if !is_outcome(py, &error)? => {
            let text = caught(py, error, Catch::Value)?;
            errors::failed(py, format!("zone environment modifier is invalid: {text}"))
        }
        other => other,
    }
}

/// `validate` and `swap`, with the handler's wrap of a ValueError of the check.
fn environment_commit(world: &Bound<'_, PyAny>, candidate: &Bound<'_, PyAny>) -> PyResult<()> {
    environment_invalid(world.py(), validate(candidate))?;
    swap(world, candidate)
}

fn environment_rule(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, expire: bool) -> PyResult<()> {
    let py = world.py();
    let row = event_row(event)?;
    let payload = map_of(&row.payload)?;
    let fields: &[&str] = if expire { &ZONE_EXPIRE_FIELDS } else { &ENVIRONMENT_APPLY_FIELDS };
    if !has_exactly(payload, fields) {
        return errors::failed(py, "zone environment event payload fields do not match");
    }
    let (Some(program_id), Some(effect_id), Some(modifier), Some(zone_id)) =
        (str_of(payload, "program_id"), str_of(payload, "effect_id"), str_of(payload, "modifier_id"), str_of(payload, "zone_id"))
    else {
        return errors::failed(py, "zone environment event IDs must be strings");
    };
    // The approved operator of a program that lists the effect; None when it doesn't.
    let approved_op = program_state(world, |state| {
        Ok(state
            .and_then(|state| state.programs.get(&**program_id))
            .filter(|program| effect_is_scheduled(program, effect_id))
            .map(|program| program.operators.get(&**effect_id).cloned()))
    })?;
    let table = model_state(world)?;
    let environment = get_or_none(&table, ENVIRONMENT_STATE)?.filter(is_dict);
    let marker = match map_of(&row.provenance)?.get(MARKER) {
        Some(V::Map(marker)) => Some(marker),
        _ => None,
    };
    let installed = match (&approved_op, marker) {
        (Some(_), Some(marker)) => holds(marker.get("policy_dependencies"), ENVIRONMENT_POLICY)? && environment.is_some(),
        _ => false,
    };
    let (Some(approved_op), Some(marker), Some(environment), true) = (approved_op, marker, environment, installed) else {
        return errors::failed(py, "zone environment event has no installed program policy");
    };
    if *modifier.as_ref() != *modifier_id(program_id, effect_id) {
        return errors::failed(py, "zone environment modifier ID does not match its effect");
    }
    let modifiers = environment.get_item(intern!(py, "modifiers"))?;
    if expire {
        let Some(record) = get_or_none(&modifiers, modifier)? else {
            return errors::suppressed(py, "zone environment modifier is not active");
        };
        let differs = |key: &Bound<'_, PyString>, wanted: &str| -> PyResult<bool> { record.get_item(key)?.ne(wanted) };
        let cause = attribute(event, intern!(py, "causation_id"))?;
        if differs(intern!(py, "program_id"), program_id)?
            || differs(intern!(py, "effect_id"), effect_id)?
            || differs(intern!(py, "zone_id"), zone_id)?
            || differs(intern!(py, "end_event_id"), &row.event_id)?
            || cause.ne(record.get_item(intern!(py, "start_event_id"))?)?
        {
            return errors::failed(py, "zone environment expiry does not match its start");
        }
        let candidate = copy_of(world)?;
        model_state(&candidate)?
            .get_item(ENVIRONMENT_STATE)?
            .get_item(intern!(py, "modifiers"))?
            .call_method1(intern!(py, "pop"), (&**modifier,))?;
        return environment_commit(world, &candidate);
    }

    let op = match payload.get("op") {
        Some(V::Str(op)) if ENVIRONMENT_OPERATORS.contains(&&**op) => op,
        Some(value) if !hashable(value) => return unsupported("an operator Python can't hash"),
        _ => return errors::failed(py, "zone environment operator is unsupported"),
    };
    if approved_op.as_deref() != Some(&**op) {
        return errors::failed(py, "zone environment operator does not match its program");
    }
    if !attribute(world, intern!(py, "zones"))?.contains(&**zone_id)? {
        return errors::failed(py, "zone environment target does not exist");
    }
    if modifiers.contains(&**modifier)? {
        return errors::suppressed(py, "zone environment modifier is already active");
    }
    let parsed = match parameters(py, op, &payload_item(event, "parameters")?) {
        Ok(parsed) => parsed,
        Err(error) => {
            let text = caught(py, error, Catch::Value)?;
            return errors::failed(py, format!("zone environment parameters are invalid: {text}"));
        }
    };
    if &**op == "modify_demand_rate" {
        let action = attribute(&parsed, intern!(py, "action"))?;
        let action = exact_str(&action)?;
        if matches!(action, "fetch_water" | "collect_meal") && !table.contains("resource_service_v1")? {
            return errors::failed(py, "zone demand action requires resource-service policy");
        }
    }
    let duration = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
    // An int past 64 bits is outside the Run either way; any other type is not an int.
    let end_minute = match payload.get("end_minute") {
        Some(V::Int(minute)) if *minute > row.due_minute && *minute < duration => *minute,
        _ => return errors::failed(py, "zone environment modifier expiry minute is invalid"),
    };
    let mut expiries = Vec::new();
    for pending in pending_of(world)?.try_iter()? {
        let pending = pending?;
        let event_type = attribute(&pending, intern!(py, "event_type"))?;
        if exact_str(&event_type)? != "effect_program_environment_expire" {
            continue;
        }
        let payload = attribute(&pending, intern!(py, "payload"))?;
        if payload.call_method1(intern!(py, "get"), ("modifier_id",))?.eq(&**modifier)? {
            expiries.push(pending);
        }
    }
    let unique = match expiries.as_slice() {
        [expiry] => ints_equal(&attribute(expiry, intern!(py, "due_minute"))?, end_minute)?,
        _ => false,
    };
    if !unique {
        return errors::failed(py, "zone environment modifier has no unique expiry event");
    }
    let end_event_id = attribute(&expiries[0], intern!(py, "event_id"))?;
    let config_data = attribute(world, intern!(py, "model_configs"))?.get_item("zone_environment")?;
    // The Python rule wraps a ValueError of this too, but a world whose config fails it fails the
    // copy the Python rule makes first (`from_dict` checks the configs): the Python rule decides.
    let config = py
        .import(intern!(py, "sheltersim.domain.environment"))?
        .getattr(intern!(py, "ScenarioZoneEnvironmentConfig"))?
        .call_method1(intern!(py, "model_validate"), (config_data,))?;
    let policy_version = attribute(&config, intern!(py, "policy_version"))?;
    if matches!(&**op, "set_ventilation" | "set_lighting" | "set_privacy" | "set_temperature")
        && policy_version.ne("zone-environment-v2")?
    {
        return errors::failed(py, "condition modifier requires zone-environment-v2");
    }
    let assumptions = match marker.get("assumption_ids") {
        Some(V::List(items)) if items.iter().all(|item| matches!(item, V::Str(_))) => str_list(marker, "assumption_ids")?,
        _ => return errors::failed(py, "zone environment assumption provenance is invalid"),
    };
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
    environment_commit(world, &candidate)
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

fn temporary_space_rule(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, expire: bool) -> PyResult<()> {
    let py = world.py();
    let row = event_row(event)?;
    let payload = map_of(&row.payload)?;
    let table = model_state(world)?;
    let state = get_or_none(&table, TEMPORARY_SPACES)?.filter(is_dict);
    let marker = match map_of(&row.provenance)?.get(MARKER) {
        Some(V::Map(marker)) => Some(marker),
        _ => None,
    };
    let (program_id, effect_id) = (payload.get("program_id"), payload.get("effect_id"));
    // `programs.get(program_id) if isinstance(program_id, str) else None`, then whether it lists
    // the effect with this operator.
    let approved = match (program_id, effect_id) {
        (Some(V::Str(program_id)), effect_id) => program_state(world, |state| {
            Ok(state.and_then(|state| state.programs.get(&**program_id)).is_some_and(|program| match effect_id {
                Some(V::Str(effect_id)) => {
                    effect_is_scheduled(program, effect_id)
                        && program.operators.get(&**effect_id).is_some_and(|op| &**op == "add_temporary_space")
                }
                _ => false,
            }))
        })?,
        _ => false,
    };
    let references = (|| -> PyResult<bool> {
        let (Some(_), true, Some(V::Str(program_id)), Some(V::Str(effect_id))) = (&state, approved, program_id, effect_id) else {
            return Ok(false);
        };
        if !is_text(payload.get("modifier_id"), &modifier_id(program_id, effect_id)) {
            return Ok(false);
        }
        let Some(marker) = marker else {
            return Ok(false);
        };
        if !holds(marker.get("policy_dependencies"), TEMPORARY_SPACES)? {
            return Ok(false);
        }
        key_in(&attribute(world, intern!(py, "zones"))?, payload.get("zone_id"))
    })()?;
    let (Some(state), Some(marker), true) = (state, marker, references) else {
        return errors::failed(py, "temporary-space event does not reference an approved installed effect");
    };
    let program_id = required_str(payload, "program_id")?;
    let effect_id = required_str(payload, "effect_id")?;
    let modifier = required_str(payload, "modifier_id")?;
    let zone_id = required_str(payload, "zone_id")?;
    if expire {
        if !has_exactly(payload, &ZONE_EXPIRE_FIELDS) {
            return errors::failed(py, "temporary-space expiry payload fields do not match");
        }
        let spaces = state.get_item(intern!(py, "spaces"))?;
        let matches = match get_or_none(&spaces, modifier)?.filter(is_dict) {
            None => false,
            Some(space) => {
                space.get_item(intern!(py, "active"))?.is_truthy()?
                    && !space.get_item(intern!(py, "zone_id"))?.ne(&**zone_id)?
                    && !space.get_item(intern!(py, "program_id"))?.ne(&**program_id)?
                    && !space.get_item(intern!(py, "effect_id"))?.ne(&**effect_id)?
                    && !attribute(event, intern!(py, "causation_id"))?.ne(space.get_item(intern!(py, "start_event_id"))?)?
                    && !attribute(event, intern!(py, "due_minute"))?.ne(space.get_item(intern!(py, "end_minute"))?)?
            }
        };
        if !matches {
            return errors::suppressed(py, "temporary-space effect did not start or does not match its expiry");
        }
        let candidate = copy_of(world)?;
        // False only for a space that is gone or not active, which the checks above saw it is not.
        if !overlay::expire_space(&candidate, modifier, &row.event_id, row.causation_id.as_deref())? {
            return errors::suppressed(py, "temporary-space effect is already expired");
        }
        return commit(world, &candidate);
    }
    if !has_exactly(payload, &ENVIRONMENT_APPLY_FIELDS) || !is_text(payload.get("op"), "add_temporary_space") {
        return errors::failed(py, "temporary-space event payload fields do not match");
    }
    // "temporary-space operator differs from its approved program" can't follow: the program's
    // operator for the effect and the payload's op were both just seen to be add_temporary_space.
    if !guard_of_row(world, &row)? {
        return errors::suppressed(py, "EffectProgram guard evaluated false");
    }
    let parsed = match parameters(py, "add_temporary_space", &payload_item(event, "parameters")?) {
        Ok(parsed) => parsed,
        Err(error) => {
            let text = caught(py, error, Catch::Value)?;
            return errors::failed(py, format!("temporary-space parameters are invalid: {text}"));
        }
    };
    let duration = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
    // An int past 64 bits is outside the interval either way; any other type is not an int.
    let end_minute = match payload.get("end_minute") {
        Some(V::Int(minute)) if row.due_minute < *minute && *minute < duration => *minute,
        _ => return errors::failed(py, "temporary-space end minute is outside its approved interval"),
    };
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
    // By name, as the Python rule calls it (its kernel first), for the text of its ValueError.
    let keywords = PyDict::new(py);
    keywords.set_item("event_id", &*row.event_id)?;
    let registered = call_rule(py, "sheltersim.domain.temporary_spaces", "register_temporary_space", (&candidate, &record), Some(&keywords));
    if let Err(error) = registered {
        return errors::failed(py, caught(py, error, Catch::Value)?);
    }
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
        return errors::failed(py, "geometry effect has invalid fields or operator");
    }
    let op = match payload.get("op") {
        Some(V::Str(op)) if GEOMETRY_OPERATORS.contains(&&**op) => op,
        Some(value) if !hashable(value) => return unsupported("an operator Python can't hash"),
        _ => return errors::failed(py, "geometry effect has invalid fields or operator"),
    };
    let (program_id, effect_id) = (payload.get("program_id"), payload.get("effect_id"));
    // `.get(payload["program_id"], {})` and `.get(payload["effect_id"])` raise on a key Python
    // can't hash, whatever the dicts hold.
    if program_id.is_some_and(|id| !hashable(id)) || effect_id.is_some_and(|id| !hashable(id)) {
        return unsupported("an ID Python can't hash");
    }
    let approved = match (program_id, effect_id) {
        (Some(V::Str(program_id)), Some(V::Str(effect_id))) => {
            program_state(world, |state| {
                Ok(state
                    .and_then(|state| state.programs.get(&**program_id))
                    .and_then(|program| program.operators.get(&**effect_id))
                    .is_some_and(|approved| approved == op))
            })? && is_text(payload.get("modifier_id"), &modifier_id(program_id, effect_id))
        }
        _ => false,
    };
    if !approved {
        return errors::failed(py, "geometry effect does not reference its approved program");
    }
    guard_passes(world, &row)?;
    if expire {
        // The Python rule's reads, on the Python values: `.get(key, {})` all the way down.
        let overlay = get_or_empty(&model_state(world)?, "geometry_overlay_v1")?;
        let raw = attribute(event, intern!(py, "payload"))?;
        let modifier = raw.get_item(intern!(py, "modifier_id"))?;
        let active = match &**op {
            "set_partition" => get_or_empty(&overlay, "partitions")?.contains(&modifier)?,
            "temporary_room_geometry" => {
                let room_id = get_or_empty(&raw, "parameters")?.call_method1(intern!(py, "get"), ("room_id",))?;
                let room = get_or_empty(&overlay, "temporary_rooms")?.call_method1(intern!(py, "get"), (room_id,))?;
                is_dict(&room)
                    && room.call_method1(intern!(py, "get"), ("active",))?.is(PyBool::new(py, true).as_any())
                    && !room
                        .call_method1(intern!(py, "get"), ("start_event_id",))?
                        .ne(attribute(event, intern!(py, "causation_id"))?)?
                    && !room
                        .call_method1(intern!(py, "get"), ("end_minute",))?
                        .ne(attribute(event, intern!(py, "due_minute"))?)?
            }
            _ => {
                let routes = get_or_empty(&overlay, "route_modifiers")?;
                let route = get_or_empty(&routes, raw.get_item(intern!(py, "target_id"))?)?;
                get_or_empty(&route, "modifiers")?.contains(&modifier)?
            }
        };
        if !active {
            return errors::suppressed(py, "geometry modifier was not applied or already expired");
        }
    }
    let candidate = copy_of(world)?;
    // By name, as the Python rule calls it (its kernel first), for the text of what it raises.
    let applied = (|| {
        let keywords = PyDict::new(py);
        keywords.set_item("expire", expire)?;
        call_rule(py, "sheltersim.domain.geometry_overlay", "apply_geometry_change", (&candidate, event), Some(&keywords))?;
        validate(&candidate)
    })();
    if let Err(error) = applied {
        return errors::failed(py, caught(py, error, Catch::ValueKeyOrType)?);
    }
    swap(world, &candidate)
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
        // `isinstance(task_kind, str) and ...`: not a str at all is no skill.
        Err(_) if !task_kind.is_instance_of::<PyString>() => return Ok(false),
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

/// The first check of `staff_handoff` and `facility_repair`, in the Python rule's order: the marker
/// and the payload reference an effect of `op` that a current approved program scheduled as this
/// event, and `target` is a str.
fn references_program(world: &Bound<'_, PyAny>, row: &EventRow, payload: &Doc, op: &str, target: &str) -> PyResult<bool> {
    let (program_id, effect_id) = (payload.get("program_id"), payload.get("effect_id"));
    // `state.get("programs", {}).get(program_id) if isinstance(state, dict) else None`, before the
    // checks: whether it lists the effect, with this operator, and this event.
    let program = program_state(world, |state| {
        let Some(state) = state else {
            return Ok(None);
        };
        match program_id {
            Some(V::Str(program_id)) => Ok(state.programs.get(&**program_id).map(|program| match effect_id {
                Some(V::Str(effect_id)) => (
                    effect_is_scheduled(program, effect_id),
                    program.operators.get(&**effect_id).is_some_and(|item| &**item == op),
                    program.scheduled_event_ids.iter().any(|item| *item == row.event_id),
                ),
                _ => (false, false, false),
            })),
            Some(value) if !hashable(value) => unsupported("a program ID Python can't hash"),
            _ => Ok(None),
        }
    })?;
    let Some(V::Map(marker)) = map_of(&row.provenance)?.get(MARKER) else {
        return Ok(false);
    };
    if differ(marker.get("program_id"), program_id)?
        || differ(marker.get("effect_id"), effect_id)?
        || !is_text(marker.get("op"), op)
        || !is_text(marker.get("phase"), "start")
    {
        return Ok(false);
    }
    let (Some((true, true, true)), Some(V::Str(program_id)), Some(V::Str(effect_id))) = (program, program_id, effect_id) else {
        return Ok(false);
    };
    Ok(is_text(payload.get("modifier_id"), &modifier_id(program_id, effect_id))
        && is_text(payload.get("op"), op)
        && matches!(payload.get(target), Some(V::Str(_))))
}

/// The checks and the copy of `staff_handoff`: everything but the receipt, which is after the swap.
fn staff_handoff_on_copy<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    row: &EventRow,
) -> PyResult<(Bound<'py, PyAny>, String, String, String, String)> {
    let py = world.py();
    let payload = map_of(&row.payload)?;
    if !references_program(world, row, payload, "staff_handoff", "staff_id")? {
        return errors::failed(py, "staff handoff does not reference an approved program");
    }
    let staff_id = required_str(payload, "staff_id")?;
    let handoff = match parameters(py, "staff_handoff", &payload_item(event, "parameters")?) {
        Ok(handoff) => handoff,
        Err(error) => {
            let text = caught(py, error, Catch::ValueOrType)?;
            return errors::failed(py, format!("staff handoff parameters are invalid: {text}"));
        }
    };
    let replacement_id = attribute(&handoff, intern!(py, "replacement_staff_id"))?;
    let task_id = attribute(&handoff, intern!(py, "task_id"))?;
    let known = attribute(world, intern!(py, "staff"))?;
    if attribute(&handoff, intern!(py, "staff_id"))?.ne(&**staff_id)?
        || replacement_id.is_none()
        || !known.contains(&**staff_id)?
        || !known.contains(&replacement_id)?
        || !attribute(world, intern!(py, "active_tasks"))?.contains(&task_id)?
        || !model_state(world)?.contains(SHARED_OPERATIONS)?
    {
        return errors::failed(py, "staff handoff participants or task are invalid");
    }
    let task_id: String = task_id.extract()?;
    let reason_code: String = attribute(&handoff, intern!(py, "reason_code"))?.extract()?;
    let replacement_id: String = replacement_id.extract()?;
    guard_passes(world, row)?;

    let candidate = copy_of(world)?;
    let minute = exact_int(&attribute(&candidate, intern!(py, "minute_index"))?)?;
    let table = model_state(&candidate)?;
    let operations = get_or_none(&table, "staff_operations_v1")?.filter(is_dict);
    let task = attribute(&candidate, intern!(py, "active_tasks"))?.get_item(&*task_id)?;
    // `operations.get("staff", {}).get(id) if isinstance(operations, dict) else None`.
    let record_of = |id: &str| -> PyResult<Option<Bound<'py, PyAny>>> {
        match &operations {
            Some(operations) => get_or_none(&get_or_empty(operations, "staff")?, id),
            None => Ok(None),
        }
    };
    let old_record = record_of(staff_id)?.filter(is_dict);
    let replacement_record = record_of(&replacement_id)?.filter(is_dict);
    let staff_map = attribute(&candidate, intern!(py, "staff"))?;
    let old_staff = staff_map.get_item(&**staff_id)?;
    let replacement = staff_map.get_item(&*replacement_id)?;
    let get = |record: &Bound<'py, PyAny>, key: &str| record.call_method1(intern!(py, "get"), (key,));
    let qualified = (|| -> PyResult<bool> {
        if attribute(&task, intern!(py, "kind"))?.ne("staff_operation")?
            || attribute(&task, intern!(py, "staff_id"))?.ne(&**staff_id)?
            || attribute(&task, intern!(py, "due_minute"))?.le(minute)?
        {
            return Ok(false);
        }
        let Some(old_record) = &old_record else {
            return Ok(false);
        };
        if get(old_record, "phase")?.ne("working")? || get(old_record, "task_id")?.ne(attribute(&task, intern!(py, "id"))?)? {
            return Ok(false);
        }
        let Some(replacement_record) = &replacement_record else {
            return Ok(false);
        };
        let target_zone = get(old_record, "target_zone_id")?;
        if get(replacement_record, "phase")?.ne("idle")?
            || !attribute(&replacement, intern!(py, "active_task_id"))?.is_none()
            || attribute(&old_staff, intern!(py, "zone_id"))?.ne(&target_zone)?
            || attribute(&replacement, intern!(py, "zone_id"))?.ne(&target_zone)?
        {
            return Ok(false);
        }
        has_task_skill(&attribute(&replacement, intern!(py, "skills"))?, &get(old_record, "task_kind")?)
    })()?;
    let Some(old_record) = old_record.filter(|_| qualified) else {
        return errors::failed(py, "staff handoff requires a qualified idle replacement already at active work");
    };
    let task_due = exact_int(&attribute(&task, intern!(py, "due_minute"))?)?;
    let replacement_text = PyString::new(py, &replacement_id);
    let available = is_available(staff_status(&candidate)?.as_ref(), &replacement_text)?
        && call_rule(
            py,
            "sheltersim.domain.staff_dynamics",
            "staff_dynamics_is_available",
            (&candidate, &replacement_text, minute),
            None,
        )?
        .is_truthy()?;
    if !available {
        return errors::failed(py, "staff handoff replacement is unavailable");
    }
    let task_key = attribute(&task, intern!(py, "id"))?;
    let task_due_object = attribute(&task, intern!(py, "due_minute"))?;
    let mut completions = Vec::new();
    for pending in pending_of(&candidate)?.try_iter()? {
        let pending = pending?;
        if !attribute(&pending, intern!(py, "event_type"))?.eq("staff_operation_complete")? {
            continue;
        }
        let payload = attribute(&pending, intern!(py, "payload"))?;
        if get(&payload, "task_id")?.eq(&task_key)?
            && get(&payload, "staff_id")?.eq(&**staff_id)?
            && attribute(&pending, intern!(py, "due_minute"))?.eq(&task_due_object)?
        {
            completions.push(pending);
        }
    }
    let [completion] = completions.as_slice() else {
        return errors::failed(py, "staff handoff requires one matching pending completion");
    };
    // The records were found in `operations["staff"]`, a dict.
    let Some(operations) = &operations else {
        return unsupported("no staff_operations_v1");
    };
    let records = operations.get_item(intern!(py, "staff"))?;
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

/// `(record,)` of a receipt, or None.
fn receipt_tuple<'py>(py: Python<'py>, record: Option<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    Ok(match record {
        Some(record) => PyTuple::new(py, [record])?.into_any(),
        None => py.None().into_bound(py),
    })
}

fn staff_handoff<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    row: &EventRow,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let (candidate, staff_id, replacement_id, task_id, reason_code, sources) = refused_by_python(py, (|| {
        let (candidate, staff_id, replacement_id, task_id, reason_code) = staff_handoff_on_copy(world, event, row)?;
        validate(&candidate)?;
        // The checks saw the marker is a mapping.
        let Some(V::Map(marker)) = map_of(&row.provenance)?.get(MARKER) else {
            return unsupported("no marker");
        };
        let sources: Vec<String> = str_list(marker, "assumption_ids")?.iter().map(|item| item.to_string()).collect();
        Ok((candidate, staff_id, replacement_id, task_id, reason_code, sources))
    })())?;
    // Past here the world has changed, and what is raised goes on as the Python rule's does.
    swap(world, &candidate)?;
    let record = handoff_record(world, &task_id, &staff_id, &replacement_id, &reason_code, &row.event_id, sources)?;
    receipt_tuple(py, record)
}

fn facility_repair<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    row: &EventRow,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let payload = map_of(&row.payload)?;
    let plan = (|| {
        if !references_program(world, row, payload, "facility_repair", "facility_id")? {
            return errors::failed(py, "facility repair does not reference an approved program");
        }
        let facility_id = required_str(payload, "facility_id")?;
        let repair = match parameters(py, "facility_repair", &payload_item(event, "parameters")?) {
            Ok(repair) => repair,
            Err(error) => {
                let text = caught(py, error, Catch::ValueOrType)?;
                return errors::failed(py, format!("facility repair parameters are invalid: {text}"));
            }
        };
        let facility_map = attribute(world, intern!(py, "facilities"))?;
        if attribute(&repair, intern!(py, "facility_id"))?.ne(&**facility_id)? || !facility_map.contains(&**facility_id)? {
            return errors::failed(py, "facility repair target does not match a known facility");
        }
        if !model_state(world)?.contains(SHARED_OPERATIONS)? {
            return errors::failed(py, "facility repair requires shared receipt state");
        }
        guard_passes(world, row)?;
        let facility = facility_map.get_item(&**facility_id)?;
        let working = exact_int(&attribute(&facility, intern!(py, "working_units"))?)?;
        let capacity = exact_int(&attribute(&facility, intern!(py, "capacity"))?)?;
        let expected = exact_int(&attribute(&repair, intern!(py, "expected_working_units"))?)?;
        let restored = exact_int(&attribute(&repair, intern!(py, "restored_working_units"))?)?;
        let modified = || {
            program_state(world, |state| {
                Ok(state
                    .and_then(|state| state.active_facility_modifiers.get(&**facility_id))
                    .is_some_and(|record| !record.modifiers.is_empty()))
            })
        };
        if working != expected || working >= restored || restored > capacity || active_outage(world, facility_id)? || modified()? {
            return errors::failed(py, "facility repair preconditions no longer match the live facility state");
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
    let units = exact_int(&attribute(&facility, intern!(py, "working_units"))?)?;
    let record = facility_recovery_record(world, &facility_id, exact_str(&component)?, "repaired", previous, units, &row.event_id, sources)?;
    receipt_tuple(py, record)
}

/// `state.get("programs", {}).get(payload.get("program_id")) if isinstance(state, dict) else None`
/// of `set_staff_role` and the additions, and whether it lists the payload's effect: None when there
/// is no program. A program ID Python can't hash raises, before the checks.
fn program_lists_effect(world: &Bound<'_, PyAny>, payload: &Doc) -> PyResult<Option<bool>> {
    let (program_id, effect_id) = (payload.get("program_id"), payload.get("effect_id"));
    program_state(world, |state| {
        let Some(state) = state else {
            return Ok(None);
        };
        match program_id {
            Some(V::Str(program_id)) => Ok(state.programs.get(&**program_id).map(|program| match effect_id {
                Some(V::Str(effect_id)) => effect_is_scheduled(program, effect_id),
                _ => false,
            })),
            Some(value) if !hashable(value) => unsupported("a program ID Python can't hash"),
            _ => Ok(None),
        }
    })
}

fn set_staff_role(world: &Bound<'_, PyAny>, row: &EventRow, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let payload = map_of(&row.payload)?;
    let approved = program_lists_effect(world, payload)? == Some(true)
        && is_text(payload.get("op"), "set_staff_role")
        && match payload.get("staff_id") {
            Some(V::Str(staff_id)) => attribute(world, intern!(py, "staff"))?.contains(&**staff_id)?,
            _ => false,
        };
    if !approved {
        return errors::failed(py, "staff-role event does not reference an approved staff target");
    }
    let staff_id = required_str(payload, "staff_id")?;
    let raw = PyDict::new(py);
    raw.set_item("skills", payload_item(event, "skills")?)?;
    let role = match parameters(py, "set_staff_role", raw.as_any()) {
        Ok(role) => role,
        Err(error) => {
            let text = caught(py, error, Catch::Value)?;
            return errors::failed(py, format!("staff-role event skills are invalid: {text}"));
        }
    };
    let candidate = copy_of(world)?;
    let staff = attribute(&candidate, intern!(py, "staff"))?.get_item(&**staff_id)?;
    // `candidate.model_state.get("staff_operations_v1", {}).get("staff", {}).get(staff_id)`.
    let operations = get_or_empty(&get_or_empty(&model_state(&candidate)?, "staff_operations_v1")?, "staff")?
        .call_method1(intern!(py, "get"), (&**staff_id,))?;
    if !attribute(&staff, intern!(py, "active_task_id"))?.is_none()
        || (is_dict(&operations) && operations.call_method1(intern!(py, "get"), ("phase",))?.ne("idle")?)
    {
        return errors::failed(py, "staff role cannot change while work is active");
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
    let keywords = PyDict::new(py);
    keywords.set_item("id", facility_id)?;
    keywords.set_item("kind", "toilet")?;
    keywords.set_item("zone_id", zone_id)?;
    keywords.set_item("capacity", 1)?;
    keywords.set_item("working_units", 1)?;
    let toilet = py.import(intern!(py, "sheltersim.domain.models"))?.getattr(intern!(py, "FacilityState"))?.call((), Some(&keywords))?;
    attribute(candidate, intern!(py, "facilities"))?.set_item(facility_id, toilet)?;
    if overlay::placement_enabled(candidate)? {
        // By name, as the Python rule calls it (its kernel first), for the text of its ValueError.
        let sources = match row.source_id.as_deref().filter(|source| !source.is_empty()) {
            Some(source) => PyTuple::new(py, [source])?,
            None => PyTuple::empty(py),
        };
        let keywords = PyDict::new(py);
        keywords.set_item("event_id", &*row.event_id)?;
        keywords.set_item("source_ids", sources)?;
        let placed = call_rule(py, "sheltersim.domain.geometry_overlay", "place_installed_facility", (candidate, facility_id), Some(&keywords));
        if let Err(error) = placed {
            return errors::failed(py, caught(py, error, Catch::Value)?);
        }
    }
    call_rule(py, "sheltersim.domain.agent_dynamics", "announce_installed_facility", (candidate, facility_id), None)?;
    Ok(())
}

fn add_structure(world: &Bound<'_, PyAny>, row: &EventRow) -> PyResult<&'static str> {
    let py = world.py();
    let payload = map_of(&row.payload)?;
    if !has_exactly(payload, &["program_id", "effect_id", "modifier_id", "op", "zone_id", "count"]) {
        return errors::failed(py, "structural primitive payload fields do not match");
    }
    let listed = program_lists_effect(world, payload)? == Some(true);
    // The count, or None for an int past 64 bits that is positive: past any bound.
    let valid = (|| -> PyResult<Option<Option<i64>>> {
        if !listed {
            return Ok(None);
        }
        match payload.get("op") {
            Some(V::Str(op)) if matches!(&**op, "add_staff" | "add_toilet") => {}
            Some(value) if !hashable(value) => return unsupported("an operator Python can't hash"),
            _ => return Ok(None),
        }
        let count = match payload.get("count") {
            Some(V::Int(count)) if *count > 0 => Some(*count),
            Some(V::Big(count)) if !count.starts_with('-') => None,
            _ => return Ok(None),
        };
        Ok(key_in(&attribute(world, intern!(py, "zones"))?, payload.get("zone_id"))?.then_some(count))
    })()?;
    let Some(count) = valid else {
        return errors::failed(py, "structural primitive target or operation is invalid");
    };
    let op = required_str(payload, "op")?;
    let zone_id = required_str(payload, "zone_id")?;
    // `.removeprefix` of a value that is not a str raises, for the Python rule to raise.
    let modifier = required_str(payload, "modifier_id")?;
    let suffix = modifier.strip_prefix("modifier-").unwrap_or(modifier);
    let candidate = copy_of(world)?;
    if &**op == "add_staff" {
        let staff = attribute(&candidate, intern!(py, "staff"))?;
        let room = 100 - staff.len()? as i64;
        let Some(count) = count.filter(|count| *count <= room) else {
            return errors::failed(py, "adding staff would exceed the 100-staff runtime bound");
        };
        for index in 1..=count {
            let staff_id = format!("effect-staff-{suffix}-{index}");
            if staff.contains(&*staff_id)? {
                return errors::failed(py, "generated staff ID already exists");
            }
            add_one_staff(py, &candidate, &staff_id, zone_id)?;
        }
        if model_state(&candidate)?.contains("movement_v1")? {
            call_rule(py, "sheltersim.domain.movement", "staff_position_snapshots", (&candidate,), None)?;
        }
    } else {
        let facilities = attribute(&candidate, intern!(py, "facilities"))?;
        let room = 200 - facilities.len()? as i64;
        let Some(count) = count.filter(|count| *count <= room) else {
            return errors::failed(py, "adding toilets would exceed the 200-facility bound");
        };
        for index in 1..=count {
            let facility_id = format!("effect-toilet-{suffix}-{index}");
            if facilities.contains(&*facility_id)? {
                return errors::failed(py, "generated toilet ID already exists");
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
                if &**phase != "start" {
                    return Err(errors::marked_value_error(py, "only a start event may store a guard"));
                }
                let mut nodes = 0;
                if parse_guard(&marker["guard"], 1, &mut nodes).is_none() {
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
