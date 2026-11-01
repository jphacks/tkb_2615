//! `domain/population.py`: who arrives, waits, is admitted and leaves. See
//! docs/rust-port/lanes/m6-population-rules.md.
//!
//! Each kernel plans, refusing with `Unsupported` before its first write whatever the Python rule
//! would raise on, and then writes. These rules run at a few events of a Run, so a write is the
//! same operation on the same container as in the Python rule, in the same order: the tracked
//! containers forward it to the namespace's row, and no kernel keeps a row of its own here. An
//! exception of such an operation is the one Python raises at that point.
//!
//! Error texts stay in the Python rules until step 4, which a refusal runs.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PySlice, PyString, PyTuple};
use sha2::{Digest, Sha256};

use crate::core::mathx::{pow, py_max, py_min, py_round, py_sum};
use crate::core::row::{attribute, exact_dict, exact_float, exact_int, exact_str, unsupported};
use crate::core::value::V;
use crate::rules::movement::{mirror_held, refused, Scene};
use crate::rules::movement_geometry::{point_in_polygon, Geo, Pt};
use crate::state::entities::{entities_of, Activity, EntityStore};
use crate::state::events::{EventKind, EventRow, EventStatus, EventWriter};
use crate::state::movement::MovementRow;
use crate::{Encoder, Unsupported};

pub(crate) const PRESENCE: &str = "population_presence_v1";
pub(crate) const DYNAMICS: &str = "population_dynamics_v1";
pub(crate) const MOVEMENT: &str = "movement_v1";
pub(crate) const INFORMATION: &str = "information_spread_v1";
pub(crate) const BODY: &str = "body_state_v1";
pub(crate) const AGENT_DYNAMICS: &str = "agent_dynamics_v1";
pub(crate) const SOCIAL_NETWORK: &str = "social_network_v1";
pub(crate) const SOCIAL_RUNTIME: &str = "social_propagation_v1";
pub(crate) const OVERLAY: &str = "geometry_overlay_v1";
const SPACES: &str = "temporary_spaces_v1";
const POLICY_VERSION: &str = "capacity-aware-arrivals-departures-v1";
pub(crate) const SOURCE_ID: &str = "ASM-POPULATION-DYNAMICS-V1";
const ROOM_INTAKE_SOURCE: &str = "ASM-TEMPORARY-ROOM-INTAKE-V1";
const MAX_ARRIVALS_PER_EVENT: i64 = 1000;
const MAX_PENDING_ARRIVALS: i64 = 10_000;
const MAX_ROOM_ADMISSION_POINTS: usize = 256;
const MAX_PRESENCE_HISTORY: usize = 20_000;
const MAX_RETRIES: i64 = 1440;
const SUPPORT_KINDS: [&str; 9] = [
    "medical_assessment",
    "medication_support",
    "caregiver_assistance",
    "privacy_accommodation",
    "mobility_assistance",
    "hygiene_support",
    "emotional_support",
    "language_access",
    "dependent_assistance",
];

// ---- The two stages ------------------------------------------------------------------------------

/// A plan's result: any error of what it read means the Python rule has something to say.
pub(crate) fn planning<T>(result: PyResult<T>) -> PyResult<T> {
    result.map_err(|error| Python::with_gil(|py| if error.is_instance_of::<Unsupported>(py) { error } else { Unsupported::new_err(format!("the plan read: {error}")) }))
}

/// A write stage's result: the Python rule must not run after a write, so a refusal here is a bug
/// to see. Every other error is what the same operation raises in the Python rule.
pub(crate) fn writing<T>(result: PyResult<T>) -> PyResult<T> {
    result.map_err(|error| Python::with_gil(|py| if error.is_instance_of::<Unsupported>(py) { PyRuntimeError::new_err(format!("a kernel refused after it began writing: {error}")) } else { error }))
}

// ---- Reads through the object protocol -----------------------------------------------------------

/// `world.model_state.get(name)`, or None.
pub(crate) fn namespace<'py>(world: &Bound<'py, PyAny>, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    let table = attribute(world, intern!(world.py(), "model_state"))?;
    Ok(exact_dict(&table)?.get_item(name)?.filter(|state| !state.is_none()))
}

pub(crate) fn has_namespace(world: &Bound<'_, PyAny>, name: &str) -> PyResult<bool> {
    let table = attribute(world, intern!(world.py(), "model_state"))?;
    exact_dict(&table)?.contains(name)
}

/// `value.get(key)`.
pub(crate) fn get<'py>(value: &Bound<'py, PyAny>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    value.call_method1(intern!(value.py(), "get"), (key,))
}

/// `value.get(key, {})`, for a read of a chain of mappings that may be absent.
pub(crate) fn get_map<'py>(value: &Bound<'py, PyAny>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    value.call_method1(intern!(value.py(), "get"), (key, PyDict::new(value.py())))
}

pub(crate) fn is_dict(value: &Bound<'_, PyAny>) -> bool {
    value.is_instance_of::<PyDict>()
}

pub(crate) fn text(value: &Bound<'_, PyAny>) -> PyResult<Arc<str>> {
    Ok(Arc::from(exact_str(value)?))
}

pub(crate) fn optional_text(value: &Bound<'_, PyAny>) -> PyResult<Option<Arc<str>>> {
    if value.is_none() {
        Ok(None)
    } else {
        text(value).map(Some)
    }
}

/// The keys of a mapping, each an exact str, sorted as Python sorts them.
pub(crate) fn sorted_keys(mapping: &Bound<'_, PyAny>) -> PyResult<Vec<Arc<str>>> {
    let mut keys = Vec::new();
    for key in mapping.try_iter()? {
        keys.push(text(&key?)?);
    }
    keys.sort_unstable_by(|first, second| first.as_bytes().cmp(second.as_bytes()));
    Ok(keys)
}

pub(crate) fn new_list<'py>(py: Python<'py>, items: &[Arc<str>]) -> PyResult<Bound<'py, PyList>> {
    PyList::new(py, items.iter().map(|item| &**item))
}

/// The world's event, as a row, or Unsupported.
pub(crate) fn event_row(event: &Bound<'_, PyAny>) -> PyResult<EventRow> {
    EventRow::from_event(event)?.ok_or_else(|| Unsupported::new_err("the event has no row"))
}

pub(crate) fn payload<'a>(row: &'a EventRow, key: &str) -> Option<&'a V> {
    let V::Map(map) = &row.payload else {
        return None;
    };
    map.get(key)
}

pub(crate) fn provenance<'a>(row: &'a EventRow, key: &str) -> Option<&'a V> {
    let V::Map(map) = &row.provenance else {
        return None;
    };
    map.get(key)
}

// ---- Hashes and identifiers --------------------------------------------------------------------

fn hex(digest: &[u8]) -> String {
    let mut out = String::with_capacity(digest.len() * 2);
    for byte in digest {
        out.push(char::from_digit((byte >> 4) as u32, 16).unwrap_or('0'));
        out.push(char::from_digit((byte & 15) as u32, 16).unwrap_or('0'));
    }
    out
}

pub(crate) fn sha_hex(text: &str) -> String {
    hex(&Sha256::digest(text.as_bytes()))
}

/// `Identifier` of contracts/extensions.py.
pub(crate) fn is_identifier(text: &str) -> bool {
    let bytes = text.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 64
        && bytes[0].is_ascii_alphanumeric()
        && bytes[1..].iter().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

pub(crate) fn identifier(text: &str) -> PyResult<()> {
    if is_identifier(text) {
        Ok(())
    } else {
        unsupported("a value is not an Identifier")
    }
}

pub(crate) fn optional_identifier(text: Option<&str>) -> PyResult<()> {
    text.map_or(Ok(()), identifier)
}

// ---- The presence ledger: contracts/presence.py as far as a plain document goes ------------------

const RECORD_FIELDS: [&str; 9] = [
    "agent_id",
    "household_id",
    "status",
    "since_minute",
    "transition_zone_id",
    "return_zone_id",
    "expected_return_minute",
    "archive_path",
    "source_event_ids",
];
const STATUSES: [&str; 6] = ["admitted", "approaching", "awaiting_admission", "temporarily_absent", "departed", "transferred"];
const TRANSITION_FIELDS: [&str; 14] = [
    "schema_version",
    "transition_id",
    "event_id",
    "agent_id",
    "minute",
    "action",
    "from_zone_id",
    "to_zone_id",
    "from_household_id",
    "to_household_id",
    "causation_id",
    "source_ids",
    "state_snapshot_hash",
    "time_semantics",
];
const ACTIONS: [&str; 11] = [
    "arrived",
    "approaching",
    "approach_completed",
    "waiting",
    "admitted",
    "departed",
    "temporary_exit",
    "returned",
    "transferred",
    "reunited",
    "relocated",
];

fn required_identifier(map: &IndexMap<Arc<str>, V>, key: &str) -> PyResult<Arc<str>> {
    match map.get(key) {
        Some(V::Str(value)) if is_identifier(value) => Ok(value.clone()),
        _ => unsupported("a presence field is not an Identifier"),
    }
}

fn maybe_identifier(map: &IndexMap<Arc<str>, V>, key: &str) -> PyResult<Option<Arc<str>>> {
    match map.get(key) {
        None | Some(V::Null) => Ok(None),
        Some(V::Str(value)) if is_identifier(value) => Ok(Some(value.clone())),
        _ => unsupported("a presence field is not an Identifier or None"),
    }
}

fn identifiers(value: Option<&V>) -> PyResult<Vec<Arc<str>>> {
    match value {
        None => Ok(Vec::new()),
        Some(V::List(items)) => items
            .iter()
            .map(|item| match item {
                V::Str(text) if is_identifier(text) => Ok(text.clone()),
                _ => unsupported("a list holds what is not an Identifier"),
            })
            .collect(),
        _ => unsupported("a list of Identifiers is something else"),
    }
}

fn minute_field(value: Option<&V>) -> PyResult<i64> {
    match value {
        Some(V::Int(minute)) if *minute >= 0 => Ok(*minute),
        _ => unsupported("a minute is not a plain non-negative int"),
    }
}

/// `PresenceRecord`, of a document whose result is certain.
#[derive(Clone)]
pub(crate) struct Person {
    pub(crate) agent_id: Arc<str>,
    pub(crate) household_id: Option<Arc<str>>,
    pub(crate) status: Arc<str>,
    pub(crate) since_minute: i64,
    pub(crate) transition_zone_id: Option<Arc<str>>,
    pub(crate) return_zone_id: Option<Arc<str>>,
    pub(crate) expected_return_minute: Option<i64>,
    pub(crate) archive_path: Option<Vec<Arc<str>>>,
    pub(crate) source_event_ids: Vec<Arc<str>>,
}

impl Person {
    pub(crate) fn parse(value: &V) -> PyResult<Self> {
        let V::Map(map) = value else {
            return unsupported("a presence record is not a mapping");
        };
        if map.keys().any(|key| !RECORD_FIELDS.contains(&&**key)) {
            return unsupported("a presence record has another field");
        }
        let status = match map.get("status") {
            Some(V::Str(status)) if STATUSES.contains(&&**status) => status.clone(),
            _ => return unsupported("a presence status is another value"),
        };
        let expected_return_minute = match map.get("expected_return_minute") {
            None | Some(V::Null) => None,
            other => Some(minute_field(other)?),
        };
        let archive_path = match map.get("archive_path") {
            None | Some(V::Null) => None,
            other => {
                let path = identifiers(other)?;
                if path.is_empty() || path.len() > 5 {
                    return unsupported("an archive path has another length");
                }
                Some(path)
            }
        };
        let person = Self {
            agent_id: required_identifier(map, "agent_id")?,
            household_id: maybe_identifier(map, "household_id")?,
            status,
            since_minute: minute_field(map.get("since_minute"))?,
            transition_zone_id: maybe_identifier(map, "transition_zone_id")?,
            return_zone_id: maybe_identifier(map, "return_zone_id")?,
            expected_return_minute,
            archive_path,
            source_event_ids: identifiers(map.get("source_event_ids"))?,
        };
        person.check()?;
        Ok(person)
    }

    /// `coherent_return`.
    pub(crate) fn check(&self) -> PyResult<()> {
        if &*self.status == "temporarily_absent" {
            if self.expected_return_minute.is_some_and(|minute| minute < self.since_minute) {
                return unsupported("expected return precedes departure");
            }
            if self.archive_path.is_none() {
                return unsupported("temporary absence requires a retained state reference");
            }
        }
        if &*self.status == "admitted" && self.transition_zone_id.is_none() {
            return unsupported("admitted presence requires a zone");
        }
        Ok(())
    }

    /// `model_dump(mode="json")`.
    pub(crate) fn dump(&self) -> V {
        let mut map = IndexMap::with_capacity(RECORD_FIELDS.len());
        map.insert(Arc::from("agent_id"), V::Str(self.agent_id.clone()));
        map.insert(Arc::from("household_id"), optional(&self.household_id));
        map.insert(Arc::from("status"), V::Str(self.status.clone()));
        map.insert(Arc::from("since_minute"), V::Int(self.since_minute));
        map.insert(Arc::from("transition_zone_id"), optional(&self.transition_zone_id));
        map.insert(Arc::from("return_zone_id"), optional(&self.return_zone_id));
        map.insert(Arc::from("expected_return_minute"), self.expected_return_minute.map_or(V::Null, V::Int));
        map.insert(Arc::from("archive_path"), self.archive_path.as_ref().map_or(V::Null, |path| strings(path)));
        map.insert(Arc::from("source_event_ids"), strings(&self.source_event_ids));
        V::Map(map)
    }
}

pub(crate) fn optional(value: &Option<Arc<str>>) -> V {
    value.as_ref().map_or(V::Null, |text| V::Str(text.clone()))
}

pub(crate) fn strings(items: &[Arc<str>]) -> V {
    V::List(items.iter().map(|item| V::Str(item.clone())).collect())
}

/// `sorted(set(items))`.
pub(crate) fn sorted_set(mut items: Vec<Arc<str>>) -> Vec<Arc<str>> {
    items.sort_unstable_by(|first, second| first.as_bytes().cmp(second.as_bytes()));
    items.dedup();
    items
}

/// `PresenceTransition`, with values the plan has checked.
pub(crate) struct Transition<'a> {
    pub(crate) transition_id: String,
    pub(crate) event_id: &'a str,
    pub(crate) agent_id: &'a str,
    pub(crate) minute: i64,
    pub(crate) action: &'a str,
    pub(crate) from_zone_id: Option<Arc<str>>,
    pub(crate) to_zone_id: Option<Arc<str>>,
    pub(crate) from_household_id: Option<Arc<str>>,
    pub(crate) to_household_id: Option<Arc<str>>,
    pub(crate) causation_id: Option<Arc<str>>,
    pub(crate) source_ids: Vec<Arc<str>>,
    pub(crate) state_snapshot_hash: Option<String>,
}

impl Transition<'_> {
    fn dump(&self) -> V {
        let mut map = IndexMap::with_capacity(TRANSITION_FIELDS.len());
        map.insert(Arc::from("schema_version"), V::Int(2));
        map.insert(Arc::from("transition_id"), V::Str(Arc::from(self.transition_id.as_str())));
        map.insert(Arc::from("event_id"), V::Str(Arc::from(self.event_id)));
        map.insert(Arc::from("agent_id"), V::Str(Arc::from(self.agent_id)));
        map.insert(Arc::from("minute"), V::Int(self.minute));
        map.insert(Arc::from("action"), V::Str(Arc::from(self.action)));
        map.insert(Arc::from("from_zone_id"), optional(&self.from_zone_id));
        map.insert(Arc::from("to_zone_id"), optional(&self.to_zone_id));
        map.insert(Arc::from("from_household_id"), optional(&self.from_household_id));
        map.insert(Arc::from("to_household_id"), optional(&self.to_household_id));
        map.insert(Arc::from("causation_id"), optional(&self.causation_id));
        map.insert(Arc::from("source_ids"), strings(&self.source_ids));
        map.insert(Arc::from("state_snapshot_hash"), self.state_snapshot_hash.as_ref().map_or(V::Null, |hash| V::Str(Arc::from(hash.as_str()))));
        map.insert(Arc::from("time_semantics"), V::Str(Arc::from("recorded_event_minute")));
        V::Map(map)
    }
}

fn transition_is_plain(value: &V) -> PyResult<()> {
    let V::Map(map) = value else {
        return unsupported("a presence transition is not a mapping");
    };
    if map.keys().any(|key| !TRANSITION_FIELDS.contains(&&**key)) {
        return unsupported("a presence transition has another field");
    }
    if !matches!(map.get("schema_version"), None | Some(V::Int(2))) {
        return unsupported("a presence transition has another schema");
    }
    required_identifier(map, "transition_id")?;
    required_identifier(map, "event_id")?;
    required_identifier(map, "agent_id")?;
    minute_field(map.get("minute"))?;
    if !matches!(map.get("action"), Some(V::Str(action)) if ACTIONS.contains(&&**action)) {
        return unsupported("a presence transition has another action");
    }
    for key in ["from_zone_id", "to_zone_id", "from_household_id", "to_household_id", "causation_id"] {
        maybe_identifier(map, key)?;
    }
    identifiers(map.get("source_ids"))?;
    match map.get("state_snapshot_hash") {
        None | Some(V::Null) => {}
        Some(V::Str(hash)) if hash.len() == 64 && hash.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f')) => {}
        _ => return unsupported("a presence transition has another hash"),
    }
    if !matches!(map.get("time_semantics"), None) && !matches!(map.get("time_semantics"), Some(V::Str(text)) if &**text == "recorded_event_minute") {
        return unsupported("a presence transition has other time semantics");
    }
    Ok(())
}

/// `PresenceState.model_validate(raw)`, for a document it certainly accepts.
pub(crate) fn presence_is_plain(raw: &Bound<'_, PyAny>) -> PyResult<()> {
    let V::Map(state) = V::from_py(raw)? else {
        return unsupported("the presence state is not a mapping");
    };
    if state.keys().any(|key| !["schema_version", "people", "history"].contains(&&**key)) {
        return unsupported("the presence state has another field");
    }
    if !matches!(state.get("schema_version"), None | Some(V::Int(1))) {
        return unsupported("the presence state has another schema");
    }
    let Some(V::Map(people)) = state.get("people") else {
        return unsupported("the presence state lacks its people");
    };
    for (key, record) in people {
        identifier(key)?;
        Person::parse(record)?;
    }
    match state.get("history") {
        None => {}
        Some(V::List(items)) if items.len() <= MAX_PRESENCE_HISTORY => {
            for item in items {
                transition_is_plain(item)?;
            }
        }
        _ => return unsupported("the presence history is something else"),
    }
    Ok(())
}

/// The namespace if a Python rule would use it, checked. `_presence_state`.
pub(crate) fn presence_state<'py>(world: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    match namespace(world, PRESENCE)? {
        Some(raw) if is_dict(&raw) => {
            presence_is_plain(&raw)?;
            Ok(Some(raw))
        }
        _ => Ok(None),
    }
}

/// The record of a person in the ledger, if any.
pub(crate) fn person_in<'py>(raw: &Bound<'py, PyAny>, agent_id: &str) -> PyResult<Option<Person>> {
    let found = get(&raw.get_item("people")?, agent_id)?;
    if found.is_none() {
        Ok(None)
    } else {
        Person::parse(&V::from_py(&found)?).map(Some)
    }
}

/// `raw["people"][agent_id] = record` and the append of `_append_presence_history`.
pub(crate) fn write_presence(raw: &Bound<'_, PyAny>, person: &Person, transition: &Transition<'_>) -> PyResult<()> {
    let py = raw.py();
    raw.get_item("people")?.set_item(&*person.agent_id, person.dump().to_py(py)?)?;
    let history = raw.get_item("history")?;
    history.call_method1(intern!(py, "append"), (transition.dump().to_py(py)?,))?;
    let length = history.len()?;
    if length > MAX_PRESENCE_HISTORY {
        history.del_item(PySlice::new(py, 0, (length - MAX_PRESENCE_HISTORY) as isize, 1))?;
    }
    Ok(())
}

/// What `_record_presence_transition` asks of an ID it will write, checked in a plan.
fn check_presence_ids(event_id: &str, agent_id: &str, zone: Option<&str>, household: Option<&str>, source_id: &str) -> PyResult<()> {
    identifier(event_id)?;
    identifier(agent_id)?;
    optional_identifier(zone)?;
    optional_identifier(household)?;
    identifier(source_id)
}

/// `_record_presence_transition`. The plan checked the ledger and the IDs.
#[allow(clippy::too_many_arguments)]
fn record_presence_transition(
    world: &Bound<'_, PyAny>,
    agent_id: &str,
    event_id: &str,
    minute: i64,
    action: &str,
    zone_id: Option<&str>,
    household_id: Option<Arc<str>>,
    source_id: &str,
) -> PyResult<()> {
    let py = world.py();
    let Some(raw) = namespace(world, PRESENCE)?.filter(is_dict) else {
        return Ok(());
    };
    let previous = person_in(&raw, agent_id)?;
    let agent = get(&attribute(world, intern!(py, "agents"))?, agent_id)?;
    let admitted = !agent.is_none();
    let mut household = household_id;
    if household.is_none() {
        household = previous.as_ref().and_then(|person| person.household_id.clone());
    }
    if household.is_none() && admitted {
        household = optional_text(&attribute(&attribute(&agent, intern!(py, "profile"))?, intern!(py, "group_id"))?)?;
    }
    let zone: Option<Arc<str>> = match zone_id {
        Some(zone) => Some(Arc::from(zone)),
        None if admitted => Some(text(&attribute(&agent, intern!(py, "zone_id"))?)?),
        None => previous.as_ref().and_then(|person| person.transition_zone_id.clone()),
    };
    let mut events = previous.as_ref().map_or(Vec::new(), |person| person.source_event_ids.clone());
    events.push(Arc::from(event_id));
    let person = Person {
        agent_id: Arc::from(agent_id),
        household_id: household.clone(),
        status: Arc::from(if admitted { "admitted" } else { "departed" }),
        since_minute: minute,
        transition_zone_id: zone.clone(),
        return_zone_id: None,
        expected_return_minute: None,
        archive_path: None,
        source_event_ids: sorted_set(events),
    };
    // A backslash and a zero, not a NUL, in the ID of this one kind of receipt.
    let digest = sha_hex(&format!("{event_id}\\0{agent_id}\\0{action}"));
    let transition = Transition {
        transition_id: format!("presence-{}", &digest[..24]),
        event_id,
        agent_id,
        minute,
        action,
        from_zone_id: previous.as_ref().and_then(|person| person.transition_zone_id.clone()),
        to_zone_id: zone,
        from_household_id: previous.as_ref().and_then(|person| person.household_id.clone()),
        to_household_id: household,
        causation_id: None,
        source_ids: vec![Arc::from(source_id)],
        state_snapshot_hash: None,
    };
    write_presence(&raw, &person, &transition)
}

/// `json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))`'s hash: the
/// values of a record are Identifiers, ints and None, so the canonical bytes are those.
pub(crate) fn record_hash(person: &Person) -> PyResult<String> {
    let mut encoder = Encoder { out: Vec::new() };
    person.dump().write_json(&mut encoder)?;
    Ok(hex(&Sha256::digest(&encoder.out)))
}

// ---- Room in a zone: `capacity_metrics` as far as admission reads it -----------------------------

/// What `effective_capacity` is made of, for one zone.
#[derive(Clone, Copy)]
struct Capacity {
    nominal: i64,
    active_people: i64,
    expired_people: i64,
}

impl Capacity {
    /// `nominal + active_people + retained`, for this many people.
    fn effective(self, current: i64) -> i64 {
        let retained = self.expired_people.min((current - self.nominal - self.active_people).max(0));
        self.nominal + self.active_people + retained
    }
}

fn truth(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    match value.downcast_exact::<pyo3::types::PyBool>() {
        Ok(flag) => Ok(flag.is_true()),
        Err(_) if value.is_none() => Ok(false),
        Err(_) => unsupported("a flag is not a bool"),
    }
}

/// `capacity_metrics`' reads for this zone, each of which raises nothing on what is accepted here.
fn capacity_of(world: &Bound<'_, PyAny>, zone_id: &str) -> PyResult<Option<Capacity>> {
    let py = world.py();
    let zones = attribute(world, intern!(py, "zones"))?;
    let zone = zones.get_item(zone_id)?;
    let nominal = attribute(&zone, intern!(py, "capacity"))?;
    if nominal.is_none() {
        return Ok(None);
    }
    let nominal = exact_int(&nominal)?;
    let mut active_people = 0;
    let mut expired_people = 0;
    if let Some(state) = namespace(world, SPACES)?.filter(is_dict) {
        let spaces = get_map(&state, "spaces")?;
        for row in spaces.call_method0(intern!(py, "values"))?.try_iter()? {
            let row = row?;
            if !get(&row, "zone_id")?.eq(zone_id)? {
                continue;
            }
            let people = exact_int(&row.get_item("people_capacity")?)?;
            if truth(&row.get_item("active")?)? {
                active_people += people;
                exact_int(&row.get_item("sleeping_capacity")?)?;
            } else {
                expired_people += people;
            }
        }
    }
    if let Some(overlay) = namespace(world, OVERLAY)?.filter(is_dict) {
        let rooms = get_map(&overlay, "temporary_rooms")?;
        if is_dict(&rooms) {
            for row in rooms.call_method0(intern!(py, "values"))?.try_iter()? {
                let row = row?;
                if is_dict(&row) && get(&row, "zone_id")?.eq(zone_id)? {
                    exact_int(&row.get_item("people_capacity")?)?;
                    exact_str(&row.get_item("room_id")?)?;
                    truth(&get(&row, "active")?)?;
                }
            }
        }
    }
    Ok(Some(Capacity { nominal, active_people, expired_people }))
}

/// A point the Python rule's `Point2D.model_validate` certainly accepts.
fn plain_point(value: &V) -> PyResult<Pt> {
    let V::Map(map) = value else {
        return unsupported("a point is not a mapping");
    };
    match (map.len(), map.get("x"), map.get("y")) {
        (2, Some(V::Float(x)), Some(V::Float(y))) if x.is_finite() && y.is_finite() => Ok(Pt { x: *x, y: *y }),
        _ => unsupported("a point is not two finite floats"),
    }
}

fn points_of(room: &IndexMap<Arc<str>, V>, key: &str) -> PyResult<Vec<Pt>> {
    match room.get(key) {
        None => Ok(Vec::new()),
        Some(V::List(items)) => items.iter().map(plain_point).collect(),
        Some(_) => unsupported("the points of a room are something else"),
    }
}

/// `value ** 2.0`.
fn squared(value: f64) -> PyResult<f64> {
    pow(value, 2.0).ok_or_else(|| Unsupported::new_err("a square overflows"))
}

/// The people of a world and the rooms given in this plan, as admission counts them.
pub(crate) struct Places<'py> {
    world: Bound<'py, PyAny>,
    occupancy: HashMap<Arc<str>, i64>,
    incoming: HashMap<Arc<str>, i64>,
    capacity: HashMap<String, Option<Capacity>>,
    planned_rooms: Vec<(Arc<str>, Option<Pt>)>,
}

impl<'py> Places<'py> {
    pub(crate) fn of(world: &Bound<'py, PyAny>) -> PyResult<Self> {
        let (occupancy, incoming) = EntityStore::with_rows(&entities_of(world)?, |rows| {
            let mut occupancy: HashMap<Arc<str>, i64> = HashMap::new();
            let mut incoming: HashMap<Arc<str>, i64> = HashMap::new();
            for (_, agent) in rows.agents.iter() {
                *occupancy.entry(agent.zone_id.clone()).or_default() += 1;
                if agent.activity == Activity::Moving {
                    if let Some(target) = &agent.target_id {
                        // `incoming_travelers`: a facility's zone, or the target itself.
                        let zone = rows.facilities.get(target).map_or(target, |facility| &facility.zone_id);
                        *incoming.entry(zone.clone()).or_default() += 1;
                    }
                }
            }
            Ok((occupancy, incoming))
        })?;
        Ok(Self { world: world.clone(), occupancy, incoming, capacity: HashMap::new(), planned_rooms: Vec::new() })
    }

    fn capacity(&mut self, zone_id: &str) -> PyResult<Option<Capacity>> {
        if let Some(known) = self.capacity.get(zone_id) {
            return Ok(*known);
        }
        let found = capacity_of(&self.world, zone_id)?;
        self.capacity.insert(zone_id.to_owned(), found);
        Ok(found)
    }

    fn occupancy(&self, zone_id: &str) -> i64 {
        self.occupancy.get(zone_id).copied().unwrap_or(0)
    }

    /// population.py's `_zone_has_room`.
    pub(crate) fn has_room(&mut self, zone_id: &str) -> PyResult<bool> {
        let Some(capacity) = self.capacity(zone_id)? else {
            return Ok(true);
        };
        let occupancy = self.occupancy(zone_id);
        let effective = capacity.effective(occupancy);
        Ok(occupancy + self.incoming.get(zone_id).copied().unwrap_or(0) < effective)
    }

    /// population_lifecycle.py's `_zone_has_room`.
    pub(crate) fn has_room_for(&mut self, zone_id: &str, excluding: Option<&str>) -> PyResult<bool> {
        let mut total = self.occupancy(zone_id) + self.incoming.get(zone_id).copied().unwrap_or(0);
        if let Some(excluded) = excluding {
            let leaving = EntityStore::with_rows(&entities_of(&self.world)?, |rows| {
                let Some(agent) = rows.agents.get(excluded) else {
                    return Ok(0);
                };
                let mut count = i64::from(&*agent.zone_id == zone_id);
                if agent.activity == Activity::Moving {
                    if let Some(target) = &agent.target_id {
                        let zone = rows.facilities.get(target).map_or(target, |facility| &facility.zone_id);
                        count += i64::from(&**zone == zone_id);
                    }
                }
                Ok(count)
            })?;
            total -= leaving;
        }
        let Some(capacity) = self.capacity(zone_id)? else {
            return Ok(true);
        };
        Ok(total < capacity.effective(total))
    }

    /// A person is planned into this zone.
    pub(crate) fn add(&mut self, zone_id: &Arc<str>) {
        *self.occupancy.entry(zone_id.clone()).or_default() += 1;
    }

    /// `available_temporary_room`, and the point `admit_to_temporary_room` then gives.
    pub(crate) fn available_room(&self, zone_id: &str) -> PyResult<Option<(Arc<str>, Pt)>> {
        let py = self.world.py();
        let configs = attribute(&self.world, intern!(py, "model_configs"))?;
        let config = get_map(&configs, "geometry_overlay")?;
        if !is_dict(&config) || !schema_has_rooms(&config)? {
            return Ok(None);
        }
        let Some(state) = namespace(&self.world, OVERLAY)?.filter(is_dict) else {
            return Ok(None);
        };
        let admissions = get_map(&state, "room_admissions")?;
        let mut counts: HashMap<Arc<str>, i64> = HashMap::new();
        if is_dict(&admissions) {
            for row in admissions.call_method0(intern!(py, "values"))?.try_iter()? {
                let row = row?;
                if is_dict(&row) {
                    let room = get(&row, "room_id")?;
                    if room.is_exact_instance_of::<PyString>() {
                        *counts.entry(text(&room)?).or_default() += 1;
                    } else if room.is_instance_of::<PyString>() {
                        return unsupported("a room ID is a str subclass");
                    }
                }
            }
        }
        for (room_id, _) in &self.planned_rooms {
            *counts.entry(room_id.clone()).or_default() += 1;
        }
        let rooms = get_map(&state, "temporary_rooms")?;
        for room_id in sorted_keys(&rooms)? {
            let room = rooms.get_item(&*room_id)?;
            if !get(&room, "active")?.is_exact_instance_of::<pyo3::types::PyBool>() || !truth(&get(&room, "active")?)? {
                continue;
            }
            if !get(&room, "zone_id")?.eq(zone_id)? {
                continue;
            }
            let people = room.call_method1(intern!(py, "get"), ("people_capacity", 0))?;
            if counts.get(&room_id).copied().unwrap_or(0) >= exact_int(&people)? {
                continue;
            }
            if let Some(point) = self.admission_point(&state, &room_id, &room)? {
                return Ok(Some((room_id, point)));
            }
        }
        Ok(None)
    }

    /// `room_admission_point`.
    fn admission_point(&self, state: &Bound<'py, PyAny>, room_id: &str, room: &Bound<'py, PyAny>) -> PyResult<Option<Pt>> {
        let py = self.world.py();
        let V::Map(fields) = V::from_py(room)? else {
            return unsupported("a room is not a mapping");
        };
        let polygon = points_of(&fields, "polygon_m")?;
        if polygon.len() < 3 {
            return Ok(None);
        }
        let first = polygon[0];
        let (mut min_x, mut max_x, mut min_y, mut max_y) = (first.x, first.x, first.y, first.y);
        for point in &polygon[1..] {
            min_x = py_min(min_x, point.x);
            max_x = py_max(max_x, point.x);
            min_y = py_min(min_y, point.y);
            max_y = py_max(max_y, point.y);
        }
        let mut occupied = Vec::new();
        let movement = namespace(&self.world, MOVEMENT)?;
        let positions = match &movement {
            Some(movement) if is_dict(movement) => Some(get_map(movement, "positions")?),
            _ => None,
        };
        let admissions = get_map(state, "room_admissions")?;
        if is_dict(&admissions) {
            for item in admissions.call_method0(intern!(py, "items"))?.try_iter()? {
                let (agent_id, admission): (Bound<'py, PyAny>, Bound<'py, PyAny>) = item?.extract()?;
                if !is_dict(&admission) || !get(&admission, "room_id")?.eq(room_id)? {
                    continue;
                }
                let Some(positions) = positions.as_ref().filter(|positions| is_dict(positions)) else {
                    continue;
                };
                let record = positions.call_method1(intern!(py, "get"), (&agent_id, PyDict::new(py)))?;
                let raw = if is_dict(&record) { get(&record, "point")? } else { py.None().into_bound(py) };
                if is_dict(&raw) {
                    occupied.push(plain_point(&V::from_py(&raw)?)?);
                }
            }
        }
        for (other, point) in &self.planned_rooms {
            if &**other == room_id {
                occupied.extend(point.iter().copied());
            }
        }
        let mut candidates = points_of(&fields, "bed_points_m")?;
        let step = 0.75;
        let mut x = min_x + step / 2.0;
        let mut inspected = 0;
        while x < max_x && inspected < MAX_ROOM_ADMISSION_POINTS {
            let mut y = min_y + step / 2.0;
            while y < max_y && inspected < MAX_ROOM_ADMISSION_POINTS {
                let candidate = Pt { x, y };
                if point_in_polygon(candidate, &polygon) {
                    candidates.push(candidate);
                }
                inspected += 1;
                y += step;
            }
            x += step;
        }
        // A dict keeps a key's first place and its last value.
        let mut unique: Vec<Pt> = Vec::with_capacity(candidates.len());
        let mut places: HashMap<(u64, u64), usize> = HashMap::with_capacity(candidates.len());
        for point in candidates {
            let rounded = |value: f64| -> PyResult<u64> {
                let value = py_round(value, 8).ok_or_else(|| Unsupported::new_err("a point does not round"))?;
                // 0.0 and -0.0 are one key.
                Ok(if value == 0.0 { 0.0f64.to_bits() } else { value.to_bits() })
            };
            let key = (rounded(point.x)?, rounded(point.y)?);
            match places.get(&key) {
                Some(&place) => unique[place] = point,
                None => {
                    places.insert(key, unique.len());
                    unique.push(point);
                }
            }
        }
        let center_x = py_sum(&polygon.iter().map(|point| point.x).collect::<Vec<_>>()) / polygon.len() as f64;
        let center_y = py_sum(&polygon.iter().map(|point| point.y).collect::<Vec<_>>()) / polygon.len() as f64;
        let mut ordered = Vec::with_capacity(unique.len());
        for point in unique {
            ordered.push((squared(point.x - center_x)? + squared(point.y - center_y)?, point));
        }
        ordered.sort_by(|first, second| {
            (first.0, first.1.x, first.1.y).partial_cmp(&(second.0, second.1.x, second.1.y)).unwrap_or(std::cmp::Ordering::Equal)
        });
        let Some(V::Str(zone)) = fields.get("zone_id") else {
            return unsupported("a room's zone is not a str");
        };
        let geo = Geo::of(&self.world)?;
        for (_, candidate) in ordered.into_iter().take(MAX_ROOM_ADMISSION_POINTS) {
            let mut taken = false;
            for other in &occupied {
                if squared(candidate.x - other.x)? + squared(candidate.y - other.y)? < 0.25 {
                    taken = true;
                    break;
                }
            }
            if taken {
                continue;
            }
            if geo.room_route(zone, Some(candidate), candidate, "independent").map_err(refused)?.is_some() {
                return Ok(Some(candidate));
            }
        }
        Ok(None)
    }

    /// The room this plan fills, for the admissions that follow it.
    pub(crate) fn take_room(&mut self, room_id: &Arc<str>, point: Pt) -> PyResult<()> {
        let held = has_namespace(&self.world, MOVEMENT)?;
        self.planned_rooms.push((room_id.clone(), held.then_some(point)));
        Ok(())
    }
}

/// The overlay config's `schema_version`, in {3, 4}.
fn schema_has_rooms(config: &Bound<'_, PyAny>) -> PyResult<bool> {
    let version = get(config, "schema_version")?;
    Ok(version.eq(3)? || version.eq(4)?)
}

// ---- `_admit_profile` ----------------------------------------------------------------------------

/// `admit_to_temporary_room`, whose checks the plan made, and a point of a position's record.
pub(crate) fn write_room_admission(
    world: &Bound<'_, PyAny>,
    agent_id: &str,
    room_id: &str,
    minute: i64,
    event_id: &str,
) -> PyResult<()> {
    let py = world.py();
    let state = namespace(world, OVERLAY)?.ok_or_else(|| PyRuntimeError::new_err("the overlay left during an admission"))?;
    let room = state.get_item("temporary_rooms")?.get_item(room_id)?;
    let mut sources = vec![Arc::from(ROOM_INTAKE_SOURCE)];
    for source in room.get_item("source_ids")?.try_iter()? {
        sources.push(text(&source?)?);
    }
    let record = PyDict::new(py);
    record.set_item("agent_id", agent_id)?;
    record.set_item("room_id", room_id)?;
    record.set_item("admitted_minute", minute)?;
    record.set_item("event_id", event_id)?;
    record.set_item("source_ids", new_list(py, &sorted_set(sources))?)?;
    state.get_item("room_admissions")?.set_item(agent_id, record)
}

pub(crate) fn point_document(py: Python<'_>, point: Pt) -> PyResult<Bound<'_, PyDict>> {
    let document = PyDict::new(py);
    document.set_item("x", point.x)?;
    document.set_item("y", point.y)?;
    Ok(document)
}

/// `position.update({...})` after an admission to a room.
pub(crate) fn place_in_room(position: &Bound<'_, PyAny>, zone_id: &str, point: Pt) -> PyResult<()> {
    let py = position.py();
    let update = PyDict::new(py);
    update.set_item("zone_id", zone_id)?;
    update.set_item("point", point_document(py, point)?)?;
    update.set_item("edge_id", py.None())?;
    update.set_item("progress", 1.0)?;
    update.set_item("position_kind", "physical")?;
    position.call_method1(intern!(py, "update"), (update,))?;
    Ok(())
}

fn models<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyModule>> {
    py.import("sheltersim.domain.models")
}

/// A new `AgentState` from a saved profile and needs, as `_admit_profile` and `_restore_person` make.
pub(crate) fn profile_object<'py>(world: &Bound<'py, PyAny>, raw: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyDict>)> {
    let py = world.py();
    let models = models(py)?;
    let initial = PyDict::new(py);
    initial.call_method1(intern!(py, "update"), (raw.get_item("initial_needs")?,))?;
    if exact_int(&attribute(world, intern!(py, "schema_version"))?)? == 5 {
        initial.set_item("hydration_need", 0.0)?;
    }
    let provenance = PyDict::new(py);
    provenance.call_method1(intern!(py, "update"), (raw.get_item("provenance")?,))?;
    let options = PyDict::new(py);
    options.set_item("id", raw.get_item("id")?)?;
    options.set_item("group_id", raw.get_item("group_id")?)?;
    options.set_item("initial_zone_id", raw.get_item("initial_zone_id")?)?;
    options.set_item("mobility", models.getattr("Mobility")?.call1((raw.get_item("mobility")?,))?)?;
    options.set_item("support_need", raw.get_item("support_need")?)?;
    options.set_item("stress_sensitivity", raw.get_item("stress_sensitivity")?)?;
    options.set_item("rng_key", raw.get_item("rng_key")?)?;
    options.set_item("initial_needs", models.getattr("InitialNeedProfile")?.call((), Some(&initial))?)?;
    options.set_item("provenance", provenance)?;
    Ok((models.getattr("AgentProfile")?.call((), Some(&options))?, initial))
}

/// `add_body_agent` for a person with no stress yet.
fn add_body_record(world: &Bound<'_, PyAny>, agent_id: &str, fatigue: f64) -> PyResult<()> {
    let py = world.py();
    let Some(state) = namespace(world, BODY)? else {
        return Ok(());
    };
    let record = PyDict::new(py);
    record.set_item("sleep_debt_minutes", py_min(1440.0, fatigue * 360.0))?;
    record.set_item("uncertainty", py_min(1.0, 0.25 + 0.0 * 0.5))?;
    record.set_item("reassurance", 0.0)?;
    record.set_item("discontent", 0.0)?;
    record.set_item("positive_affect", 0.5)?;
    record.set_item("negative_affect", 0.0)?;
    record.set_item("hygiene_need", 0.0)?;
    state.get_item("agents")?.set_item(agent_id, record)
}

/// What admission and restoration ask of the Rust paths they use, in a plan: their writes must not
/// refuse. Their own checks say so here.
pub(crate) fn check_admission_paths(world: &Bound<'_, PyAny>, zones: &[&str]) -> PyResult<()> {
    if has_namespace(world, AGENT_DYNAMICS)? {
        crate::rules::agent_dynamics::check_add_agent(world)?;
    }
    if has_namespace(world, MOVEMENT)? {
        let mirror = mirror_held(world)?;
        crate::core::world_core::NamespaceMirror::with_row::<MovementRow, _>(&mirror, |_| Ok(()))?;
        let scene = Scene::of(world)?;
        for zone in zones {
            scene.check_idle_point(zone)?;
        }
    }
    Ok(())
}

/// `_admit_profile`. `room` is the plan's room and point.
fn write_admission(world: &Bound<'_, PyAny>, item: &Bound<'_, PyAny>, minute: i64, zone_id: &str, room: Option<&(Arc<str>, Pt)>) -> PyResult<()> {
    let py = world.py();
    let raw = item.get_item("profile")?;
    let (profile, initial) = profile_object(world, &raw)?;
    let models = models(py)?;
    let needs = models.getattr("NeedState")?.call((), Some(&initial))?;
    let options = PyDict::new(py);
    options.set_item("profile", &profile)?;
    options.set_item("zone_id", zone_id)?;
    options.set_item("needs", &needs)?;
    options.set_item("next_decision_at", minute)?;
    let agent = models.getattr("AgentState")?.call((), Some(&options))?;
    let agent_text = text(&attribute(&agent, intern!(py, "id"))?)?;
    let agent_id = &*agent_text;
    attribute(world, intern!(py, "agents"))?.set_item(agent_id, &agent)?;
    if let Some(state) = namespace(world, INFORMATION)? {
        state.get_item("beliefs")?.set_item(agent_id, PyDict::new(py))?;
        state.get_item("query_pressure")?.set_item(agent_id, 0.0)?;
    }
    let agent_key = PyString::new(py, agent_id);
    crate::rules::agent_dynamics::add_agent(world, agent_key.as_any())?;
    let fatigue = exact_float(&attribute(&needs, intern!(py, "fatigue"))?).or_else(|_| attribute(&needs, intern!(py, "fatigue"))?.extract::<f64>())?;
    add_body_record(world, agent_id, fatigue)?;
    let state = namespace(world, DYNAMICS)?.ok_or_else(|| PyRuntimeError::new_err("the population state left during an admission"))?;
    let event_id = item.get_item("event_id")?;
    let record = PyDict::new(py);
    record.set_item("event_id", &event_id)?;
    record.set_item("agent_id", agent_id)?;
    record.set_item("zone_id", zone_id)?;
    record.set_item("preferred_zone_id", item.get_item("zone_id")?)?;
    record.set_item("room_id", room.map(|(room_id, _)| &**room_id))?;
    record.set_item("created_minute", item.get_item("created_minute")?)?;
    record.set_item("admitted_minute", minute)?;
    if item.contains("approach_route_id")? {
        record.set_item("approach_route_id", item.get_item("approach_route_id")?)?;
        record.set_item("approach_started_minute", item.get_item("approach_started_minute")?)?;
        let completed = if item.contains("approach_completed_minute")? { item.get_item("approach_completed_minute")? } else { item.get_item("approach_due_minute")? };
        record.set_item("approach_completed_minute", completed)?;
    }
    state.get_item("arrivals")?.call_method1(intern!(py, "append"), (record,))?;
    let event_text = text(&event_id)?;
    if let Some(movement) = namespace(world, MOVEMENT)? {
        reset_position(world, &movement, agent_id, zone_id)?;
    }
    if let Some((room_id, point)) = room {
        write_room_admission(world, agent_id, room_id, minute, &event_text)?;
        if let Some(movement) = namespace(world, MOVEMENT)? {
            place_in_room(&movement.get_item("positions")?.get_item(agent_id)?, zone_id, *point)?;
        }
    }
    let presence = namespace(world, PRESENCE)?;
    let status = match &presence {
        Some(presence) if is_dict(presence) => person_in(presence, agent_id)?.map(|person| person.status),
        _ => None,
    };
    let action = if status.as_deref() == Some("awaiting_admission") { "admitted" } else { "arrived" };
    let traits = get(item, "traits")?;
    let household = if is_dict(&traits) { optional_text(&get(&traits, "household_id")?)? } else { None };
    let source = get_map(&raw, "provenance")?.call_method1(intern!(py, "get"), ("source_id", SOURCE_ID))?;
    record_presence_transition(world, agent_id, &event_text, minute, action, Some(zone_id), household, exact_str(&source)?)
}

/// `state["positions"][id] = {...}` of a person now standing in a zone, and `finish_movement`.
pub(crate) fn reset_position(world: &Bound<'_, PyAny>, movement: &Bound<'_, PyAny>, agent_id: &str, zone_id: &str) -> PyResult<()> {
    let py = world.py();
    let position = PyDict::new(py);
    position.set_item("zone_id", zone_id)?;
    position.set_item("point", py.None())?;
    position.set_item("edge_id", py.None())?;
    position.set_item("progress", 0.0)?;
    position.set_item("position_kind", "unavailable")?;
    movement.get_item("positions")?.set_item(agent_id, position)?;
    let key = PyString::new(py, agent_id);
    crate::rules::movement::finish_movement(world, key.as_any())
}

/// What the plan asks of a person about to be admitted.
fn check_item_for_admission(item: &Bound<'_, PyAny>, zone: &str) -> PyResult<()> {
    let raw = item.get_item("profile")?;
    let agent_object = raw.get_item("id")?;
    let agent_id = exact_str(&agent_object)?;
    let event_id = item.get_item("event_id")?;
    let traits = get(item, "traits")?;
    let household = if is_dict(&traits) { optional_text(&get(&traits, "household_id")?)? } else { None };
    let group = optional_text(&raw.get_item("group_id")?)?;
    let provenance = get_map(&raw, "provenance")?;
    let source = provenance.call_method1(intern!(item.py(), "get"), ("source_id", SOURCE_ID))?;
    check_presence_ids(exact_str(&event_id)?, agent_id, Some(zone), household.as_deref().or(group.as_deref()), exact_str(&source)?)?;
    exact_int(&item.get_item("created_minute")?)?;
    if item.contains("approach_route_id")? {
        exact_str(&item.get_item("approach_route_id")?)?;
    }
    Ok(())
}

// ---- `admit_pending_arrivals` --------------------------------------------------------------------

struct Waiting<'py> {
    item: Bound<'py, PyAny>,
    created: i64,
    event_id: Arc<str>,
    agent_id: Arc<str>,
}

/// `_choose_admission_target`.
fn choose_target(places: &mut Places<'_>, item: &Bound<'_, PyAny>) -> PyResult<Option<(Arc<str>, Option<(Arc<str>, Pt)>)>> {
    let py = item.py();
    let mut zones = vec![text(&item.get_item("zone_id")?)?];
    let alternates = item.call_method1(intern!(py, "get"), ("admission_zone_ids", PyTuple::empty(py)))?;
    for zone in alternates.try_iter()? {
        zones.push(text(&zone?)?);
    }
    for zone in zones {
        if places.has_room(&zone)? {
            return Ok(Some((zone, None)));
        }
        if let Some(room) = places.available_room(&zone)? {
            return Ok(Some((zone, Some(room))));
        }
    }
    Ok(None)
}

struct AdmitPlan<'py> {
    state: Bound<'py, PyAny>,
    pending: Bound<'py, PyAny>,
    order: Vec<Bound<'py, PyAny>>,
    admit: Vec<(Bound<'py, PyAny>, Arc<str>, Option<(Arc<str>, Pt)>, Arc<str>)>,
    remaining: Vec<Waiting<'py>>,
    summaries: Vec<(Bound<'py, PyAny>, Arc<str>, i64)>,
}

fn plan_admit_pending<'py>(world: &Bound<'py, PyAny>) -> PyResult<Option<AdmitPlan<'py>>> {
    let Some(state) = namespace(world, DYNAMICS)? else {
        return Ok(None);
    };
    let pending = state.get_item("pending_arrivals")?;
    if !pending.is_truthy()? {
        return Ok(None);
    }
    let mut waiting = Vec::new();
    for item in pending.try_iter()? {
        let item = item?;
        let created = exact_int(&item.get_item("created_minute")?)?;
        let event_id = text(&item.get_item("event_id")?)?;
        let agent_id = text(&item.get_item("profile")?.get_item("id")?)?;
        waiting.push(Waiting { item, created, event_id, agent_id });
    }
    // Stable, as sorted is; IDs compare as their bytes do.
    waiting.sort_by(|first, second| (first.created, first.event_id.as_bytes(), first.agent_id.as_bytes()).cmp(&(second.created, second.event_id.as_bytes(), second.agent_id.as_bytes())));
    let mut places = Places::of(world)?;
    let mut admit = Vec::new();
    let mut remaining = Vec::with_capacity(waiting.len());
    let mut order = Vec::with_capacity(waiting.len());
    let mut admit_zones: Vec<Arc<str>> = Vec::new();
    for entry in waiting {
        if entry.item.contains("approach_route_id")? && get(&entry.item, "approach_completed_minute")?.is_none() {
            order.push(entry.item.clone());
            remaining.push(entry);
            continue;
        }
        if let Some((zone, room)) = choose_target(&mut places, &entry.item)? {
            check_item_for_admission(&entry.item, &zone)?;
            places.add(&zone);
            if let Some((room_id, point)) = &room {
                places.take_room(room_id, *point)?;
            }
            admit_zones.push(zone.clone());
            admit.push((entry.item, zone, room, entry.agent_id));
        } else {
            order.push(entry.item.clone());
            remaining.push(entry);
        }
    }
    if !admit.is_empty() {
        let zones: Vec<&str> = admit_zones.iter().map(|zone| &**zone).collect();
        check_admission_paths(world, &zones)?;
        presence_state(world)?;
        if admit.iter().any(|(_, _, room, _)| room.is_some()) && !is_dict(&get(&namespace(world, OVERLAY)?.ok_or_else(|| Unsupported::new_err("no overlay"))?, "room_admissions")?) {
            return unsupported("a room admission has no admissions to join");
        }
    }
    let mut summaries = Vec::new();
    for record in state.get_item("arrivals")?.try_iter()? {
        let record = record?;
        if !record.contains("requested_count")? {
            continue;
        }
        let requested = exact_int(&record.get_item("requested_count")?)?;
        let event_id = text(&record.get_item("event_id")?)?;
        summaries.push((record, event_id, requested));
    }
    Ok(Some(AdmitPlan { state, pending, order, admit, remaining, summaries }))
}

fn write_admit_pending<'py>(world: &Bound<'py, PyAny>, minute: i64, plan: AdmitPlan<'py>) -> PyResult<Bound<'py, PyTuple>> {
    let py = world.py();
    let mut admitted: Vec<Arc<str>> = Vec::with_capacity(plan.admit.len());
    for (item, zone, room, agent_id) in &plan.admit {
        write_admission(world, item, minute, zone, room.as_ref())?;
        admitted.push(agent_id.clone());
    }
    // Everyone stays where they were on most ticks, and a new list is converted again.
    let held = plan.pending.len()?;
    let mut same = plan.order.len() == held;
    if same {
        for (index, item) in plan.order.iter().enumerate() {
            if !item.is(&plan.pending.get_item(index)?) {
                same = false;
                break;
            }
        }
    }
    if !same {
        plan.state.set_item("pending_arrivals", PyList::new(py, &plan.order)?)?;
    }
    let metrics = attribute(world, intern!(py, "metric_state"))?;
    if !admitted.is_empty() {
        let count = metrics.call_method1(intern!(py, "get"), ("population_arrival_count", 0.0))?;
        metrics.set_item("population_arrival_count", count.add(admitted.len())?)?;
    }
    metrics.set_item("population_waiting_count", PyFloat::new(py, plan.remaining.len() as f64))?;
    let mut waiting: HashMap<&str, HashSet<&str>> = HashMap::new();
    for entry in &plan.remaining {
        waiting.entry(&entry.event_id).or_default().insert(&entry.agent_id);
    }
    for (record, event_id, requested) in &plan.summaries {
        let count = waiting.get(&**event_id).map_or(0, HashSet::len) as i64;
        // The same value again changes nothing but the mirror's generation.
        let (key_waiting, key_admitted) = (get(record, "waiting_count")?, get(record, "admitted_count")?);
        if !(key_waiting.is_exact_instance_of::<PyInt>() && key_waiting.eq(count)?) {
            record.set_item("waiting_count", count)?;
        }
        if !(key_admitted.is_exact_instance_of::<PyInt>() && key_admitted.eq(requested - count)?) {
            record.set_item("admitted_count", requested - count)?;
        }
    }
    PyTuple::new(py, admitted.iter().map(|id| &**id))
}

fn admit_inner<'py>(world: &Bound<'py, PyAny>, minute: i64) -> PyResult<Bound<'py, PyTuple>> {
    let py = world.py();
    match planning(plan_admit_pending(world))? {
        None => Ok(PyTuple::empty(py)),
        Some(plan) => writing(write_admit_pending(world, minute, plan)),
    }
}

#[pyfunction]
pub(crate) fn admit_pending_arrivals<'py>(world: &Bound<'py, PyAny>, minute: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
    let minute = exact_int(minute)?;
    admit_inner(world, minute)
}

// ---- `queue_arrivals` ----------------------------------------------------------------------------

struct ApproachRoute {
    route_id: Arc<str>,
    travel_minutes: i64,
    source_ids: Vec<Arc<str>>,
    walking_speed: f64,
    distance: f64,
}

/// `parsed_facility_geometry(...).arrival_approach_routes`, the Scenario's parsed contract.
pub(crate) fn approach_routes<'py>(world: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let raw = get_map(&configs, "facility_geometry")?;
    let parse = py.import("sheltersim.domain.geometry_overlay")?.getattr("parsed_facility_geometry")?;
    attribute(&parse.call1((raw,))?, intern!(py, "arrival_approach_routes"))?.try_iter()?.collect()
}

fn read_route(route: &Bound<'_, PyAny>) -> PyResult<ApproachRoute> {
    let py = route.py();
    let mut source_ids = Vec::new();
    for source in attribute(route, intern!(py, "source_ids"))?.try_iter()? {
        source_ids.push(text(&source?)?);
    }
    Ok(ApproachRoute {
        route_id: text(&attribute(route, intern!(py, "route_id"))?)?,
        travel_minutes: exact_int(&attribute(route, intern!(py, "travel_minutes"))?)?,
        source_ids,
        walking_speed: exact_float(&attribute(route, intern!(py, "walking_speed_m_per_min"))?)?,
        distance: exact_float(&attribute(route, intern!(py, "distance_m"))?)?,
    })
}

/// `V::Map` from pairs in this order.
macro_rules! v_map {
    ($($key:expr => $value:expr),* $(,)?) => {{
        let mut map: indexmap::IndexMap<Arc<str>, V> = indexmap::IndexMap::new();
        $(map.insert(Arc::from($key), $value);)*
        map
    }};
}
pub(crate) use v_map;

fn v_str(value: &str) -> V {
    V::Str(Arc::from(value))
}

fn map_get<'a>(value: &'a V, key: &str) -> Option<&'a V> {
    match value {
        V::Map(map) => map.get(key),
        _ => None,
    }
}

struct Prepared {
    item: IndexMap<Arc<str>, V>,
    agent_id: Arc<str>,
    household: Arc<str>,
    role: &'static str,
    care_needs: Vec<Arc<str>>,
    source: Arc<str>,
    completion: Option<EventRow>,
    decision: Option<(Arc<str>, Option<(Arc<str>, Pt)>)>,
}

/// `_generated_arrival_context`: the same fields as `model_dump(exclude_defaults, exclude_none)`.
fn generated_context(items: &[Prepared]) -> PyResult<Vec<(Arc<str>, V)>> {
    let mut households: Vec<(&str, Vec<&Prepared>)> = Vec::new();
    for item in items {
        match households.iter_mut().find(|(name, _)| *name == &*item.household) {
            Some((_, members)) => members.push(item),
            None => households.push((&item.household, vec![item])),
        }
    }
    households.sort_by(|first, second| first.0.as_bytes().cmp(second.0.as_bytes()));
    let mut contexts = Vec::with_capacity(items.len());
    for (household, mut members) in households {
        members.sort_by(|first, second| first.agent_id.as_bytes().cmp(second.agent_id.as_bytes()));
        let caregiver = members.iter().find(|item| item.role == "adult").map(|item| item.agent_id.clone());
        let mut care_for: Vec<Arc<str>> = Vec::new();
        if caregiver.is_some() {
            care_for = members.iter().filter(|item| matches!(item.role, "child" | "dependent")).map(|item| item.agent_id.clone()).collect();
            care_for.sort_by(|first, second| first.as_bytes().cmp(second.as_bytes()));
        }
        if care_for.len() > 16 {
            return unsupported("a household has more dependents than a context holds");
        }
        for item in members {
            let dependent = matches!(item.role, "child" | "dependent");
            let caregiver_ids: Vec<Arc<str>> = if dependent { caregiver.iter().cloned().collect() } else { Vec::new() };
            let is_caregiver = caregiver.as_deref() == Some(&*item.agent_id);
            let requires_review = dependent || !care_for.is_empty();
            let role_name = if item.role == "adult" && is_caregiver && !care_for.is_empty() {
                Some("caregiver")
            } else {
                match item.role {
                    "child" => Some("child"),
                    "dependent" => Some("dependent"),
                    "alone" => Some("alone"),
                    _ => None,
                }
            };
            let mut context = v_map! { "household_id" => v_str(household) };
            if !caregiver_ids.is_empty() {
                context.insert(Arc::from("caregiver_ids"), strings(&caregiver_ids));
            }
            if is_caregiver && !care_for.is_empty() {
                context.insert(Arc::from("care_for_ids"), strings(&care_for));
            }
            if requires_review {
                context.insert(Arc::from("requires_review"), V::Bool(true));
                context.insert(Arc::from("review_reasons"), V::List(vec![v_str("synthetic_arrival_care_relationship_requires_review")]));
            }
            if let Some(name) = role_name {
                context.insert(Arc::from("roles"), V::List(vec![v_str(name)]));
            }
            if item.role == "child" {
                context.insert(Arc::from("guardian_status"), v_str(if caregiver_ids.is_empty() { "unaccompanied" } else { "accompanied" }));
            }
            if !item.care_needs.is_empty() {
                context.insert(Arc::from("care_support_needs"), strings(&item.care_needs));
            }
            let sources = sorted_set(vec![Arc::from(SOURCE_ID), item.source.clone()]);
            for source in &sources {
                identifier(source)?;
            }
            context.insert(Arc::from("source_ids"), strings(&sources));
            contexts.push((item.agent_id.clone(), V::Map(context)));
        }
    }
    Ok(contexts)
}

#[pyfunction]
#[pyo3(signature = (world, *, event, zone_id, count, household_size=None, mobility=None, language=None, admission_zone_ids=None, approach_route_id=None, person_context_overrides=None))]
#[allow(clippy::too_many_arguments)]
fn queue_arrivals<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    zone_id: &Bound<'py, PyAny>,
    count: &Bound<'py, PyAny>,
    household_size: Option<&Bound<'py, PyAny>>,
    mobility: Option<&Bound<'py, PyAny>>,
    language: Option<&Bound<'py, PyAny>>,
    admission_zone_ids: Option<&Bound<'py, PyAny>>,
    approach_route_id: Option<&Bound<'py, PyAny>>,
    person_context_overrides: Option<&Bound<'py, PyAny>>,
) -> PyResult<i64> {
    let plan = planning(plan_arrivals(world, event, zone_id, count, household_size, mobility, language, admission_zone_ids, approach_route_id, person_context_overrides))?;
    writing(write_arrivals(world, &plan))?;
    Ok(plan.admitted)
}

struct ArrivalsPlan {
    zone: Arc<str>,
    prepared: Vec<Prepared>,
    contexts: Vec<(Arc<str>, V)>,
    summary: V,
    event_id: Arc<str>,
    due_minute: i64,
    route_sources: Vec<Arc<str>>,
    admitted: i64,
}

#[allow(clippy::too_many_arguments)]
fn plan_arrivals<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    zone_id: &Bound<'py, PyAny>,
    count: &Bound<'py, PyAny>,
    household_size: Option<&Bound<'py, PyAny>>,
    mobility: Option<&Bound<'py, PyAny>>,
    language: Option<&Bound<'py, PyAny>>,
    admission_zone_ids: Option<&Bound<'py, PyAny>>,
    approach_route_id: Option<&Bound<'py, PyAny>>,
    person_context_overrides: Option<&Bound<'py, PyAny>>,
) -> PyResult<ArrivalsPlan> {
    let py = world.py();
    let zones = attribute(world, intern!(py, "zones"))?;
    let zone = text(zone_id)?;
    if !zones.contains(&*zone)? {
        return unsupported("an unknown zone");
    }
    let mut alternates = Vec::new();
    if let Some(given) = admission_zone_ids {
        if !(given.is_exact_instance_of::<PyTuple>() || given.is_exact_instance_of::<PyList>()) {
            return unsupported("the alternate zones are not a list or tuple");
        }
        for item in given.try_iter()? {
            let item = text(&item?)?;
            if alternates.contains(&item) || !zones.contains(&*item)? || item == zone {
                return unsupported("the alternate zones are invalid");
            }
            alternates.push(item);
        }
    }
    let route = match approach_route_id.filter(|route| !route.is_none()) {
        None => None,
        Some(route_id) => {
            if !alternates.is_empty() {
                return unsupported("an approach route with alternate zones");
            }
            let route_id = exact_str(route_id)?;
            let mut found = None;
            for route in approach_routes(world)? {
                if attribute(&route, intern!(py, "route_id"))?.eq(route_id)? {
                    if !attribute(&route, intern!(py, "destination_zone_id"))?.eq(&*zone)? {
                        return unsupported("the route targets another zone");
                    }
                    found = Some(read_route(&route)?);
                    break;
                }
            }
            Some(found.ok_or_else(|| Unsupported::new_err("the route is unknown"))?)
        }
    };
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let overlay_config = get_map(&configs, "geometry_overlay")?;
    if !alternates.is_empty() && (!is_dict(&overlay_config) || !schema_has_rooms(&overlay_config)?) {
        return unsupported("alternate rooms need another overlay schema");
    }
    let count = exact_int(count)?;
    if !(1..=MAX_ARRIVALS_PER_EVENT).contains(&count) {
        return unsupported("the count is out of range");
    }
    let household_size = household_size.map_or(Ok(1), exact_int)?;
    if !(1..=12).contains(&household_size) {
        return unsupported("the household size is out of range");
    }
    let mobility = mobility.map_or(Ok("independent"), exact_str)?;
    if !["independent", "assisted", "wheelchair"].contains(&mobility) {
        return unsupported("the mobility is not a member, or is the Python rule's to check");
    }
    let language = language.map_or(Ok("ja"), exact_str)?;
    identifier(language)?;
    let mut care_by_index: HashMap<i64, Vec<Arc<str>>> = HashMap::new();
    if let Some(overrides) = person_context_overrides {
        let model = py.import("sheltersim.contracts.population")?.getattr("DynamicArrivalPersonContext")?;
        for item in overrides.try_iter()? {
            let parsed = model.call_method1(intern!(py, "model_validate"), (item?,))?;
            let index = exact_int(&attribute(&parsed, intern!(py, "member_index"))?)?;
            if index >= count || care_by_index.contains_key(&index) {
                return unsupported("a context's member index is invalid");
            }
            let mut needs = Vec::new();
            for kind in attribute(&parsed, intern!(py, "care_support_needs"))?.try_iter()? {
                let kind = text(&kind?)?;
                if !SUPPORT_KINDS.contains(&&*kind) {
                    return unsupported("a support kind is not known");
                }
                needs.push(kind);
            }
            care_by_index.insert(index, needs);
        }
    }
    let Some(state) = namespace(world, DYNAMICS)? else {
        return unsupported("the population state is made on first use by the Python rule");
    };
    let pending = state.get_item("pending_arrivals")?;
    let pending_count = pending.len()? as i64;
    if pending_count + count > MAX_PENDING_ARRIVALS {
        return unsupported("past the pending bound");
    }
    let agents = attribute(world, intern!(py, "agents"))?;
    if agents.len()? as i64 + pending_count + count > 10_000 {
        return unsupported("past the runtime bound");
    }
    let mut known: HashSet<Arc<str>> = HashSet::new();
    for id in state.get_item("initial_agent_ids")?.try_iter()? {
        known.insert(text(&id?)?);
    }
    let traits_map = state.get_item("profile_traits")?;
    for id in traits_map.try_iter()? {
        known.insert(text(&id?)?);
    }
    if known.len() as i64 + count > 10_000 {
        return unsupported("past the history bound");
    }
    let mut reserved: HashSet<Arc<str>> = HashSet::new();
    for id in agents.try_iter()? {
        reserved.insert(text(&id?)?);
    }
    for item in pending.try_iter()? {
        reserved.insert(text(&item?.get_item("profile")?.get_item("id")?)?);
    }
    for id in traits_map.try_iter()? {
        reserved.insert(text(&id?)?);
    }
    let row = event_row(event)?;
    identifier(&row.event_id)?;
    let seed = exact_int(&attribute(world, intern!(py, "seed"))?)?;
    let schema_version = exact_int(&attribute(world, intern!(py, "schema_version"))?)?;
    let hydrated = schema_version == 6;
    let event_digest = sha_hex(&row.event_id);
    let event_digest = &event_digest[..20];
    let context_source = row.source_id.clone().unwrap_or_else(|| Arc::from(SOURCE_ID));
    let mut prepared = Vec::with_capacity(count as usize);
    for index in 0..count {
        let digest = sha_hex(&format!("{seed}\0{}\0{index}", row.event_id));
        let byte = |start: usize| -> i64 { i64::from_str_radix(&digest[start..start + 2], 16).unwrap_or(0) };
        let agent_id: Arc<str> = Arc::from(format!("arrival-{}", &digest[..28]));
        if !reserved.insert(agent_id.clone()) {
            return unsupported("a stable ID repeats");
        }
        let group_id: Arc<str> = Arc::from(format!("household-{event_digest}-{:03}", index / household_size));
        let age_years = 5 + byte(0) % 79;
        let role = if age_years < 18 {
            "child"
        } else if age_years >= 75 {
            "dependent"
        } else if household_size == 1 {
            "alone"
        } else {
            "adult"
        };
        let traits = V::Map(v_map! {
            "age_years" => V::Int(age_years),
            "household_id" => V::Str(group_id.clone()),
            "household_role" => v_str(role),
            "languages" => V::List(vec![v_str(language)]),
            "accessibility_needs" => V::List(Vec::new()),
            "medical_support" => V::Bool(false),
            "source_ids" => V::List(Vec::new()),
        });
        let level = |start: usize| -> f64 { byte(start) as f64 / 255.0 * 0.35 };
        let mut initial = v_map! {
            "toilet_urgency" => V::Float(level(2)),
            "hunger" => V::Float(level(4)),
            "fatigue" => V::Float(level(6)),
        };
        if hydrated {
            initial.insert(Arc::from("hydration_need"), V::Float(level(8)));
        }
        let profile = V::Map(v_map! {
            "id" => V::Str(agent_id.clone()),
            "group_id" => V::Str(group_id.clone()),
            "initial_zone_id" => V::Str(zone.clone()),
            "mobility" => v_str(mobility),
            "support_need" => V::Float(byte(10) as f64 / 255.0 * 0.5),
            "stress_sensitivity" => V::Float(byte(12) as f64 / 255.0),
            "rng_key" => V::Str(Arc::from(format!("arrival:{}", &digest[..24]))),
            "initial_needs" => V::Map(initial),
            "provenance" => V::Map(v_map! {
                "source_type" => v_str("assumption"),
                "source_id" => v_str(SOURCE_ID),
                "event_id" => V::Str(row.event_id.clone()),
                "seed" => V::Int(seed),
            }),
        });
        let care_needs = care_by_index.get(&index).cloned().unwrap_or_default();
        let mut item = v_map! {
            "profile" => profile,
            "traits" => traits,
            "member_index" => V::Int(index),
            "care_support_needs" => strings(&care_needs),
            "context_source_id" => V::Str(context_source.clone()),
            "zone_id" => V::Str(zone.clone()),
            "admission_zone_ids" => strings(&alternates),
            "created_minute" => V::Int(row.due_minute),
            "event_id" => V::Str(row.event_id.clone()),
        };
        if let Some(route) = &route {
            item.insert(Arc::from("approach_route_id"), V::Str(route.route_id.clone()));
            item.insert(Arc::from("approach_started_minute"), V::Int(row.due_minute));
            item.insert(Arc::from("approach_due_minute"), V::Int(row.due_minute + route.travel_minutes));
        }
        prepared.push(Prepared {
            item,
            agent_id,
            household: group_id,
            role,
            care_needs,
            source: context_source.clone(),
            completion: None,
            decision: None,
        });
    }
    let contexts = generated_context(&prepared)?;
    let presence = presence_state(world)?;
    let writer = EventWriter::of(world)?;
    let mut sequence = writer.next_sequence()?;
    let mut places = Places::of(world)?;
    let mut admitted = 0;
    let route_sources = route.as_ref().map_or(Vec::new(), |route| route.source_ids.clone());
    let zone_list: Vec<&str> = std::iter::once(&*zone).chain(alternates.iter().map(|zone| &**zone)).collect();
    let mut any_admission = false;
    for item in &mut prepared {
        if let Some(route) = &route {
            let due = row.due_minute + route.travel_minutes;
            if due < writer.duration() {
                let completion = plan_completion(world, &row, item, route, due, sequence)?;
                sequence += 1;
                item.item.insert(Arc::from("approach_completion_event_id"), V::Str(completion.event_id.clone()));
                item.completion = Some(completion);
            }
            check_waiting_person(presence.as_ref(), item, &zone, &row.event_id, &route_sources)?;
            continue;
        }
        let mut decision = None;
        for candidate in &zone_list {
            if places.has_room(candidate)? {
                decision = Some((Arc::<str>::from(*candidate), None));
                break;
            }
            if let Some(found) = places.available_room(candidate)? {
                decision = Some((Arc::<str>::from(*candidate), Some(found)));
                break;
            }
        }
        match decision {
            Some((target, room)) => {
                check_presence_ids(&row.event_id, &item.agent_id, Some(&target), Some(&item.household), SOURCE_ID)?;
                places.add(&target);
                if let Some((room_id, point)) = &room {
                    places.take_room(room_id, *point)?;
                    let overlay = namespace(world, OVERLAY)?.ok_or_else(|| Unsupported::new_err("no overlay"))?;
                    if !is_dict(&get(&overlay, "room_admissions")?) {
                        return unsupported("a room admission has no admissions to join");
                    }
                }
                any_admission = true;
                admitted += 1;
                item.decision = Some((target, room));
            }
            None => check_waiting_person(presence.as_ref(), item, &zone, &row.event_id, &route_sources)?,
        }
    }
    if any_admission {
        check_admission_paths(world, &zone_list)?;
    }
    let summary = V::Map(v_map! {
        "event_id" => V::Str(row.event_id.clone()),
        "zone_id" => V::Str(zone.clone()),
        "requested_count" => V::Int(count),
        "admitted_count" => V::Int(admitted),
        "waiting_count" => V::Int(count - admitted),
        "minute" => V::Int(row.due_minute),
    });
    Ok(ArrivalsPlan { zone, prepared, contexts, summary, event_id: row.event_id.clone(), due_minute: row.due_minute, route_sources, admitted })
}

fn check_waiting_person(presence: Option<&Bound<'_, PyAny>>, item: &Prepared, zone: &str, event_id: &str, route_sources: &[Arc<str>]) -> PyResult<()> {
    let Some(raw) = presence else {
        return Ok(());
    };
    if raw.get_item("people")?.contains(&*item.agent_id)? {
        return unsupported("a waiting arrival already has a presence record");
    }
    identifier(zone)?;
    identifier(event_id)?;
    identifier(&item.household)?;
    for source in route_sources {
        identifier(source)?;
    }
    Ok(())
}

/// `_schedule_arrival_approach_completion`, as a row.
fn plan_completion(world: &Bound<'_, PyAny>, arrival: &EventRow, item: &Prepared, route: &ApproachRoute, due: i64, sequence: i64) -> PyResult<EventRow> {
    let program_id = payload(arrival, "program_id").cloned().unwrap_or(V::Null);
    let effect_id = payload(arrival, "effect_id").cloned().unwrap_or(V::Null);
    let V::Str(program_key) = &program_id else {
        return unsupported("the arrival has no program ID");
    };
    let V::Str(effect_key) = &effect_id else {
        return unsupported("the arrival has no effect ID");
    };
    let Some(effect_state) = namespace(world, "effect_program_v1")? else {
        return unsupported("no approved program");
    };
    let program = get(&get_map(&effect_state, "programs")?, program_key)?;
    if !is_dict(&program)
        || !get(&program, "effect_ids")?.contains(&**effect_key)?
        || !get(&get_map(&program, "operators")?, effect_key)?.eq("add_evacuees")?
    {
        return unsupported("the approach is not in an approved program");
    }
    let Some(V::Map(marker)) = provenance(arrival, "effect_program_v1") else {
        return unsupported("the arrival lacks its program's provenance");
    };
    let mut derived = marker.clone();
    derived.insert(Arc::from("phase"), v_str("derived"));
    let completion_id: Arc<str> = Arc::from(format!("arrival-approach-complete-{sequence}"));
    let source = route.source_ids.first().cloned().ok_or_else(|| Unsupported::new_err("a route has no sources"))?;
    EventRow::new(
        completion_id,
        EventKind::Completion,
        due,
        0,
        sequence,
        Arc::from("population_approach_complete"),
        v_map! {
            "program_id" => program_id.clone(),
            "effect_id" => effect_id.clone(),
            "agent_id" => V::Str(item.agent_id.clone()),
            "arrival_event_id" => V::Str(arrival.event_id.clone()),
            "route_id" => V::Str(route.route_id.clone()),
            "zone_id" => map_get(&V::Map(item.item.clone()), "zone_id").cloned().unwrap_or(V::Null),
        },
        Some(source),
        Some(arrival.event_id.clone()),
        v_map! {
            "effect_program_v1" => V::Map(derived),
            "arrival_approach" => V::Map(v_map! {
                "route_id" => V::Str(route.route_id.clone()),
                "source_ids" => strings(&route.source_ids),
                "walking_speed_m_per_min" => V::Float(route.walking_speed),
                "distance_m" => V::Float(route.distance),
            }),
        },
    )
    .ok_or_else(|| Unsupported::new_err("ScheduledEvent refuses the completion"))
}

fn write_waiting_presence(world: &Bound<'_, PyAny>, prepared: &Prepared, event_id: &str, created: i64, zone: &Arc<str>, route_sources: &[Arc<str>], approaching: bool) -> PyResult<()> {
    let Some(raw) = namespace(world, PRESENCE)?.filter(is_dict) else {
        return Ok(());
    };
    let status = if approaching { "approaching" } else { "awaiting_admission" };
    let action = if approaching { "approaching" } else { "waiting" };
    let person = Person {
        agent_id: prepared.agent_id.clone(),
        household_id: Some(prepared.household.clone()),
        status: Arc::from(status),
        since_minute: created,
        transition_zone_id: Some(zone.clone()),
        return_zone_id: None,
        expected_return_minute: None,
        archive_path: None,
        source_event_ids: vec![Arc::from(event_id)],
    };
    let digest = sha_hex(&format!("{event_id}\0{}\0{action}", prepared.agent_id));
    let mut sources = vec![Arc::from(SOURCE_ID)];
    sources.extend(route_sources.iter().cloned());
    let transition = Transition {
        transition_id: format!("presence-{}", &digest[..24]),
        event_id,
        agent_id: &prepared.agent_id,
        minute: created,
        action,
        from_zone_id: None,
        to_zone_id: Some(zone.clone()),
        from_household_id: None,
        to_household_id: Some(prepared.household.clone()),
        causation_id: None,
        source_ids: sorted_set(sources),
        state_snapshot_hash: None,
    };
    write_presence(&raw, &person, &transition)
}

fn write_arrivals(world: &Bound<'_, PyAny>, plan: &ArrivalsPlan) -> PyResult<()> {
    let py = world.py();
    let state = namespace(world, DYNAMICS)?.ok_or_else(|| PyRuntimeError::new_err("the population state left"))?;
    let contexts = state.call_method1(intern!(py, "setdefault"), ("profile_context", PyDict::new(py)))?;
    let update = PyDict::new(py);
    for (agent_id, context) in &plan.contexts {
        update.set_item(&**agent_id, context.to_py(py)?)?;
    }
    contexts.call_method1(intern!(py, "update"), (update,))?;
    let writer = EventWriter::of(world)?;
    let pending_list = || state.get_item("pending_arrivals");
    for prepared in &plan.prepared {
        let document = V::Map(prepared.item.clone());
        let traits = map_get(&document, "traits").ok_or_else(|| PyRuntimeError::new_err("an item lost its traits"))?;
        state.get_item("profile_traits")?.set_item(&*prepared.agent_id, traits.to_py(py)?)?;
        let approaching = !plan.route_sources.is_empty();
        if approaching {
            if let Some(completion) = &prepared.completion {
                writer.allocate_sequence()?;
                writer.schedule(completion.clone())?;
            }
            pending_list()?.call_method1(intern!(py, "append"), (document.to_py(py)?,))?;
            write_waiting_presence(world, prepared, &plan.event_id, plan.due_minute, &plan.zone, &plan.route_sources, true)?;
            continue;
        }
        match &prepared.decision {
            Some((zone, room)) => {
                write_admission(world, &document.to_py(py)?, plan.due_minute, zone, room.as_ref())?;
            }
            None => {
                pending_list()?.call_method1(intern!(py, "append"), (document.to_py(py)?,))?;
                write_waiting_presence(world, prepared, &plan.event_id, plan.due_minute, &plan.zone, &plan.route_sources, false)?;
            }
        }
    }
    state.get_item("arrivals")?.call_method1(intern!(py, "append"), (plan.summary.to_py(py)?,))?;
    let metrics = attribute(world, intern!(py, "metric_state"))?;
    let arrived = metrics.call_method1(intern!(py, "get"), ("population_arrival_count", 0.0))?;
    metrics.set_item("population_arrival_count", arrived.add(plan.admitted)?)?;
    metrics.set_item("population_waiting_count", PyFloat::new(py, pending_list()?.len()? as f64))
}

// ---- `apply_arrival_approach_completion` ---------------------------------------------------------

#[pyfunction]
fn apply_arrival_approach_completion(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let (pending, plan) = planning((|| {
        let row = event_row(event)?;
        if &*row.event_type != "population_approach_complete" {
            return unsupported("another event type");
        }
        let V::Map(fields) = &row.payload else {
            return unsupported("a payload is not a mapping");
        };
        let expected = ["program_id", "effect_id", "agent_id", "arrival_event_id", "route_id", "zone_id"];
        if fields.len() != expected.len() || expected.iter().any(|key| !fields.contains_key(*key)) {
            return unsupported("the payload's fields do not match");
        }
        let (Some(V::Str(program_id)), Some(V::Str(effect_id)), Some(V::Str(agent_id)), Some(V::Str(arrival_id)), Some(V::Str(route_id)), Some(V::Str(zone_id))) = (
            fields.get("program_id"),
            fields.get("effect_id"),
            fields.get("agent_id"),
            fields.get("arrival_event_id"),
            fields.get("route_id"),
            fields.get("zone_id"),
        ) else {
            return unsupported("a payload value is not a str");
        };
        let Some(effect_state) = namespace(world, "effect_program_v1")? else {
            return unsupported("no approved program");
        };
        let program = get(&get_map(&effect_state, "programs")?, program_id)?;
        if !is_dict(&program)
            || !get(&program, "effect_ids")?.contains(&**effect_id)?
            || !get(&get_map(&program, "operators")?, effect_id)?.eq("add_evacuees")?
            || row.causation_id.as_deref() != Some(&**arrival_id)
        {
            return unsupported("the completion is outside its program");
        }
        let Some(state) = namespace(world, DYNAMICS)?.filter(is_dict) else {
            return unsupported("no population state");
        };
        let mut found = None;
        for item in state.get_item("pending_arrivals")?.try_iter()? {
            let item = item?;
            if item.get_item("profile")?.get_item("id")?.eq(&**agent_id)? {
                found = Some(item);
                break;
            }
        }
        let Some(pending) = found else {
            return unsupported("no such pending arrival");
        };
        if !get(&pending, "approach_completion_event_id")?.eq(&*row.event_id)?
            || !get(&pending, "approach_route_id")?.eq(&**route_id)?
            || !get(&pending, "event_id")?.eq(&**arrival_id)?
            || !get(&pending, "zone_id")?.eq(&**zone_id)?
            || !get(&pending, "approach_due_minute")?.eq(row.due_minute)?
            || !get(&pending, "approach_completed_minute")?.is_none()
        {
            return unsupported("the completion does not match its arrival");
        }
        let mut route = None;
        for candidate in approach_routes(world)? {
            if attribute(&candidate, intern!(py, "route_id"))?.eq(&**route_id)? {
                route = Some(candidate);
                break;
            }
        }
        let Some(route) = route else {
            return unsupported("the route is unknown");
        };
        let route = {
            if !attribute(&route, intern!(py, "destination_zone_id"))?.eq(&**zone_id)? {
                return unsupported("the route targets another zone");
            }
            read_route(&route)?
        };
        if !row.source_id.as_ref().is_some_and(|source| route.source_ids.contains(source)) {
            return unsupported("the route's provenance does not match");
        }
        let presence = namespace(world, PRESENCE)?.filter(is_dict);
        let mut record = None;
        if let Some(raw) = &presence {
            let previous = get(&get_map(raw, "people")?, agent_id)?;
            if !is_dict(&previous) || !get(&previous, "status")?.eq("approaching")? {
                return unsupported("the approach has no presence");
            }
            let person = Person::parse(&V::from_py(&previous)?)?;
            identifier(&row.event_id)?;
            identifier(agent_id)?;
            identifier(zone_id)?;
            optional_identifier(row.causation_id.as_deref())?;
            for source in &route.source_ids {
                identifier(source)?;
            }
            record = Some(person);
        }
        Ok((pending, (row.clone(), zone_id.clone(), agent_id.clone(), route.source_ids, presence, record)))
    })())?;
    writing((|| {
        let (row, zone_id, agent_id, route_sources, presence, record) = &plan;
        pending.call_method1(intern!(py, "pop"), ("approach_completion_event_id",))?;
        pending.set_item("approach_completed_minute", row.due_minute)?;
        let (Some(raw), Some(old)) = (presence, record) else {
            return Ok(());
        };
        let mut person = old.clone();
        person.status = Arc::from("awaiting_admission");
        person.since_minute = row.due_minute;
        let digest = sha_hex(&format!("{}\0{agent_id}\0approach-completed", row.event_id));
        let mut sources = vec![Arc::from(SOURCE_ID)];
        sources.extend(route_sources.iter().cloned());
        let transition = Transition {
            transition_id: format!("presence-{}", &digest[..24]),
            event_id: &row.event_id,
            agent_id,
            minute: row.due_minute,
            action: "approach_completed",
            from_zone_id: None,
            to_zone_id: Some(zone_id.clone()),
            from_household_id: None,
            to_household_id: person.household_id.clone(),
            causation_id: row.causation_id.clone(),
            source_ids: sorted_set(sources),
            state_snapshot_hash: None,
        };
        write_presence(raw, &person, &transition)
    })())
}

// ---- `schedule_population_retry` -----------------------------------------------------------------

/// The plan of a retry: the row needs the sequence, which is the next one when it is written.
struct Retry {
    row: EventRow,
    program: Option<Arc<str>>,
}

fn plan_retry(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, id_prefix: &str) -> PyResult<Retry> {
    let py = world.py();
    let row = event_row(event)?;
    let writer = EventWriter::of(world)?;
    let due = exact_int(&attribute(world, intern!(py, "minute_index"))?)? + 1;
    if due >= writer.duration() {
        return unsupported("the retry is past the scenario");
    }
    let retry_count = match provenance(&row, "population_retry") {
        Some(V::Map(retry)) => match retry.get("count") {
            None => 0,
            Some(V::Int(count)) => *count,
            Some(_) => return unsupported("a retry count is not an int"),
        },
        _ => 0,
    };
    if retry_count >= MAX_RETRIES {
        return unsupported("the retry limit is reached");
    }
    let sequence = writer.next_sequence()?;
    let digest = sha_hex(&format!("{}:{sequence}", row.event_id));
    let next_id: Arc<str> = Arc::from(format!("{id_prefix}-{}", &digest[..24]));
    let V::Map(mut record) = row.provenance.clone() else {
        return unsupported("provenance is not a mapping");
    };
    record.insert(Arc::from("deferred_from"), V::Str(row.event_id.clone()));
    record.insert(Arc::from("population_retry"), V::Map(v_map! { "count" => V::Int(retry_count + 1) }));
    let mut program = None;
    if let Some(V::Map(compiler)) = record.get("effect_program_v1") {
        if let Some(effects) = namespace(world, "effect_program_v1")? {
            let programs = get_map(&effects, "programs")?;
            if let (Some(V::Str(program_id)), Some(V::Str(effect_id))) = (compiler.get("program_id"), compiler.get("effect_id")) {
                let found = get(&programs, program_id)?;
                if is_dict(&found) && get(&found, "effect_ids")?.contains(&**effect_id)? {
                    found.get_item("scheduled_event_ids")?;
                    program = Some(program_id.clone());
                }
            } else {
                return unsupported("a program's marker is not two strs");
            }
        }
    }
    let V::Map(fields) = row.payload.clone() else {
        return unsupported("a payload is not a mapping");
    };
    let retry = EventRow::new(next_id, EventKind::Endogenous, due, row.priority, sequence, row.event_type.clone(), fields, row.source_id.clone(), Some(row.event_id.clone()), record)
        .ok_or_else(|| Unsupported::new_err("ScheduledEvent refuses the retry"))?;
    Ok(Retry { row: retry, program })
}

#[pyfunction]
#[pyo3(signature = (world, event, *, id_prefix="departure-retry", subject="departure"))]
fn schedule_population_retry(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, id_prefix: &str, subject: &str) -> PyResult<()> {
    let py = world.py();
    let _ = subject;
    let plan = planning(plan_retry(world, event, id_prefix))?;
    writing((|| {
        let writer = EventWriter::of(world)?;
        writer.allocate_sequence()?;
        if let Some(program_id) = &plan.program {
            let effects = namespace(world, "effect_program_v1")?.ok_or_else(|| PyRuntimeError::new_err("the programs left"))?;
            let program = effects.get_item("programs")?.get_item(&**program_id)?;
            program.get_item("scheduled_event_ids")?.call_method1(intern!(py, "append"), (&*plan.row.event_id,))?;
        }
        writer.schedule(plan.row.clone())
    })())
}

// ---- `queue_departures` --------------------------------------------------------------------------

/// `json.loads(key)` of a key of a pair of people, as `_pair_from_key` accepts it.
pub(crate) fn pair_of(key: &str) -> PyResult<[String; 2]> {
    let Ok(parts) = serde_json::from_str::<Vec<String>>(key) else {
        return unsupported("a pair key is not a list of strs");
    };
    match <[String; 2]>::try_from(parts) {
        Ok(pair) => Ok(pair),
        Err(_) => unsupported("a pair key is not two people"),
    }
}

pub(crate) fn check_pairs(mapping: &Bound<'_, PyAny>) -> PyResult<()> {
    for key in mapping.try_iter()? {
        pair_of(exact_str(&key?)?)?;
    }
    Ok(())
}

/// `_arrival_minute`.
fn arrival_minute(state: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<i64> {
    for record in state.get_item("arrivals")?.try_iter()? {
        let record = record?;
        if get(&record, "agent_id")?.eq(agent_id)? {
            let fallback = record.call_method1(intern!(record.py(), "get"), ("created_minute", 0))?;
            return exact_int(&record.call_method1(intern!(record.py(), "get"), ("admitted_minute", fallback))?);
        }
    }
    Ok(0)
}

/// What `_remove_information_state` and `_archive_person` ask of the information state.
pub(crate) fn check_information(world: &Bound<'_, PyAny>) -> PyResult<()> {
    if let Some(info) = namespace(world, INFORMATION)? {
        for name in ["beliefs", "query_pressure", "messages"] {
            exact_dict(&info.get_item(name)?)?;
        }
        check_pairs(&info.get_item("contacts")?)?;
        info.get_item("support_requests")?.try_iter()?;
    }
    Ok(())
}

pub(crate) fn check_relationship_keys(world: &Bound<'_, PyAny>) -> PyResult<()> {
    for name in [SOCIAL_NETWORK, BODY] {
        if let Some(state) = namespace(world, name)? {
            check_pairs(&state.get_item("relationships")?)?;
        }
    }
    if let Some(runtime) = namespace(world, SOCIAL_RUNTIME)?.filter(is_dict) {
        let contacts = get_map(&runtime, "contacts")?;
        if is_dict(&contacts) {
            check_pairs(&contacts)?;
        }
    }
    Ok(())
}

/// Each pending event has a row: `_cancel_agent_events` records rows.
pub(crate) fn check_queue_rows(world: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let writer = EventWriter::of(world)?;
    let ids = writer.queue().borrow().pending_ids(None);
    for id in ids {
        if writer.queue().borrow_mut().row(py, &id)?.is_none() {
            return unsupported("a pending event has no row");
        }
    }
    Ok(())
}

fn event_names_agent(row: &EventRow, agent_id: &str) -> bool {
    ["agent_id", "sender_agent_id", "recipient_agent_id", "participant_agent_id"]
        .iter()
        .any(|key| matches!(payload(row, key), Some(V::Str(value)) if &**value == agent_id))
}

/// `_cancel_agent_events`.
pub(crate) fn cancel_agent_events(world: &Bound<'_, PyAny>, agent_id: &str, withdrawn: &HashSet<Arc<str>>) -> PyResult<()> {
    let py = world.py();
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let writer = EventWriter::of(world)?;
    let pending = |writer: &EventWriter<'_>| -> PyResult<Vec<Arc<EventRow>>> {
        let ids = writer.queue().borrow().pending_ids(None);
        let mut rows = Vec::with_capacity(ids.len());
        for id in ids {
            if let Some(row) = writer.queue().borrow_mut().row(py, &id)? {
                rows.push(row);
            }
        }
        Ok(rows)
    };
    let detail = format!("Agent {agent_id} departed before the event was due");
    for row in pending(&writer)? {
        if event_names_agent(&row, agent_id) {
            writer.settle(&row, minute, EventStatus::Suppressed, Some(&detail))?;
        }
    }
    let detail = format!("Claim withdrawn after Agent {agent_id} departed");
    for row in pending(&writer)? {
        let from_agent = &*row.event_type == "information_rumor" && matches!(payload(&row, "origin_agent_id"), Some(V::Str(origin)) if &**origin == agent_id);
        let claim = matches!(payload(&row, "message_id"), Some(V::Str(message)) if withdrawn.contains(message));
        if from_agent || claim {
            writer.settle(&row, minute, EventStatus::Suppressed, Some(&detail))?;
        }
    }
    Ok(())
}

/// `release_sleep_bed` and `release_temporary_room_admission`.
pub(crate) fn release_room(world: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<()> {
    let py = world.py();
    if let Some(state) = namespace(world, OVERLAY)?.filter(is_dict) {
        for name in ["bed_reservations", "room_admissions"] {
            let map = get(&state, name)?;
            if is_dict(&map) {
                map.call_method1(intern!(py, "pop"), (agent_id, py.None()))?;
            }
        }
    }
    Ok(())
}

/// `remove_agent` of social_network: the ties, popped in key order.
fn remove_social_relationships<'py>(world: &Bound<'py, PyAny>, agent_id: &str) -> PyResult<Bound<'py, PyList>> {
    let py = world.py();
    let archived = PyList::empty(py);
    let Some(state) = namespace(world, SOCIAL_NETWORK)? else {
        return Ok(archived);
    };
    let relationships = state.get_item("relationships")?;
    for key in sorted_keys(&relationships)? {
        let pair = pair_of(&key)?;
        if pair.iter().any(|part| part == agent_id) {
            let record = PyDict::new(py);
            record.set_item("pair", PyList::new(py, pair.iter().map(String::as_str))?)?;
            record.set_item("record", relationships.call_method1(intern!(py, "pop"), (&*key,))?)?;
            archived.append(record)?;
        }
    }
    Ok(archived)
}

/// `_remove_information_state`: the messages the person started.
fn remove_information_state(world: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<HashSet<Arc<str>>> {
    let py = world.py();
    let mut withdrawn = HashSet::new();
    let Some(state) = namespace(world, INFORMATION)? else {
        return Ok(withdrawn);
    };
    let beliefs = state.get_item("beliefs")?;
    beliefs.call_method1(intern!(py, "pop"), (agent_id, PyDict::new(py)))?;
    state.get_item("query_pressure")?.call_method1(intern!(py, "pop"), (agent_id, py.None()))?;
    let contacts = state.get_item("contacts")?;
    let mut leaving = Vec::new();
    for key in contacts.try_iter()? {
        let key = key?;
        if pair_of(exact_str(&key)?)?.iter().any(|part| part == agent_id) {
            leaving.push(key);
        }
    }
    for key in leaving {
        contacts.call_method1(intern!(py, "pop"), (key, py.None()))?;
    }
    let messages = state.get_item("messages")?;
    for item in messages.call_method0(intern!(py, "items"))?.try_iter()? {
        let (message_id, message): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
        if get(&message, "origin_agent_id")?.eq(agent_id)? {
            withdrawn.insert(text(&message_id)?);
        }
    }
    for message_id in &withdrawn {
        messages.call_method1(intern!(py, "pop"), (&**message_id, py.None()))?;
    }
    if !withdrawn.is_empty() {
        for held in beliefs.call_method0(intern!(py, "values"))?.try_iter()? {
            let held = held?;
            for message_id in &withdrawn {
                held.call_method1(intern!(py, "pop"), (&**message_id, py.None()))?;
            }
        }
    }
    filter_support_requests(&state, agent_id)?;
    Ok(withdrawn)
}

/// `state["support_requests"][:] = [item for item in ... if item.get("agent_id") != agent_id]`.
pub(crate) fn filter_support_requests(state: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<()> {
    let py = state.py();
    let requests = state.get_item("support_requests")?;
    let kept = PyList::empty(py);
    for item in requests.try_iter()? {
        let item = item?;
        if !get(&item, "agent_id")?.eq(agent_id)? {
            kept.append(item)?;
        }
    }
    requests.set_item(PySlice::full(py), kept)
}

/// `world.support_requests = {...}` without this person's requests.
pub(crate) fn drop_support_requests(world: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<()> {
    let py = world.py();
    let kept = PyDict::new(py);
    for item in attribute(world, intern!(py, "support_requests"))?.call_method0(intern!(py, "items"))?.try_iter()? {
        let (key, request): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
        if !attribute(&request, intern!(py, "agent_id"))?.eq(agent_id)? {
            kept.set_item(key, request)?;
        }
    }
    world.setattr(intern!(py, "support_requests"), kept)
}

/// `_profile_record`.
fn profile_record<'py>(profile: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let py = profile.py();
    let record = PyDict::new(py);
    for name in ["id", "group_id", "initial_zone_id"] {
        record.set_item(name, profile.getattr(name)?)?;
    }
    record.set_item("mobility", profile.getattr("mobility")?.getattr("value")?)?;
    for name in ["support_need", "stress_sensitivity", "rng_key"] {
        record.set_item(name, profile.getattr(name)?)?;
    }
    let initial = profile.getattr("initial_needs")?;
    let needs = PyDict::new(py);
    for name in ["toilet_urgency", "hunger", "fatigue", "hydration_need"] {
        needs.set_item(name, initial.getattr(name)?)?;
    }
    record.set_item("initial_needs", needs)?;
    record.set_item("provenance", profile.getattr("provenance")?)?;
    Ok(record)
}

/// `_depart_agent`.
fn write_departure(world: &Bound<'_, PyAny>, agent_id: &str, row: &EventRow, state: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let agents = attribute(world, intern!(py, "agents"))?;
    let agent = agents.get_item(agent_id)?;
    release_room(world, agent_id)?;
    let needs_object = attribute(&agent, intern!(py, "needs"))?;
    let needs = PyDict::new(py);
    for name in ["toilet_urgency", "hunger", "fatigue", "hydration_need"] {
        needs.set_item(name, needs_object.getattr(name)?)?;
    }
    let departure = PyDict::new(py);
    departure.set_item("event_id", &*row.event_id)?;
    departure.set_item("agent_id", agent_id)?;
    departure.set_item("zone_id", attribute(&agent, intern!(py, "zone_id"))?)?;
    departure.set_item("minute", row.due_minute)?;
    departure.set_item("profile", profile_record(&attribute(&agent, intern!(py, "profile"))?)?)?;
    departure.set_item("traits", get(&state.get_item("profile_traits")?, agent_id)?)?;
    departure.set_item("needs", needs)?;
    departure.set_item("relationships", remove_social_relationships(world, agent_id)?)?;
    state.get_item("departures")?.call_method1(intern!(py, "append"), (departure,))?;
    let withdrawn = remove_information_state(world, agent_id)?;
    cancel_agent_events(world, agent_id, &withdrawn)?;
    if let Some(service) = namespace(world, "resource_service_v1")?.filter(is_dict) {
        for name in ["request_history", "queue_stall_history"] {
            let kept = PyList::empty(py);
            for item in service.get_item(name)?.try_iter()? {
                let item = item?;
                if !item.get_item("agent_id")?.eq(agent_id)? {
                    kept.append(item)?;
                }
            }
            service.set_item(name, kept)?;
        }
        let kept = PyDict::new(py);
        for item in service.get_item("stall_state")?.call_method0(intern!(py, "items"))?.try_iter()? {
            let (facility_id, record): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
            if !record.get_item("agent_id")?.eq(agent_id)? {
                kept.set_item(facility_id, record)?;
            }
        }
        service.set_item("stall_state", kept)?;
    }
    if let Some(runtime) = namespace(world, SOCIAL_RUNTIME)?.filter(is_dict) {
        let contacts = get_map(&runtime, "contacts")?;
        if is_dict(&contacts) {
            let kept = PyDict::new(py);
            for item in contacts.call_method0(intern!(py, "items"))?.try_iter()? {
                let (key, value): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
                if !pair_of(exact_str(&key)?)?.iter().any(|part| part == agent_id) {
                    kept.set_item(key, value)?;
                }
            }
            runtime.set_item("contacts", kept)?;
        }
        let cooldowns = get_map(&runtime, "agent_cooldown_until")?;
        if is_dict(&cooldowns) {
            cooldowns.call_method1(intern!(py, "pop"), (agent_id, py.None()))?;
        }
    }
    drop_support_requests(world, agent_id)?;
    if let Some(dynamics) = namespace(world, AGENT_DYNAMICS)? {
        dynamics.get_item("agents")?.call_method1(intern!(py, "pop"), (agent_id, py.None()))?;
    }
    if let Some(body) = namespace(world, BODY)? {
        body.get_item("agents")?.call_method1(intern!(py, "pop"), (agent_id, py.None()))?;
        let relationships = body.get_item("relationships")?;
        for key in sorted_keys(&relationships)? {
            if pair_of(&key)?.iter().any(|part| part == agent_id) {
                relationships.call_method1(intern!(py, "pop"), (&*key,))?;
            }
        }
    }
    if let Some(movement) = namespace(world, MOVEMENT)? {
        movement.get_item("journeys")?.call_method1(intern!(py, "pop"), (agent_id, py.None()))?;
        movement.get_item("positions")?.call_method1(intern!(py, "pop"), (agent_id, py.None()))?;
    }
    agents.call_method1(intern!(py, "pop"), (agent_id,))?;
    record_presence_transition(world, agent_id, &row.event_id, row.due_minute, "departed", None, None, SOURCE_ID)
}

#[pyfunction]
#[pyo3(signature = (world, *, event, zone_id, count, include_initial=None))]
fn queue_departures<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    zone_id: &Bound<'py, PyAny>,
    count: &Bound<'py, PyAny>,
    include_initial: Option<&Bound<'py, PyAny>>,
) -> PyResult<usize> {
    let py = world.py();
    let (row, state, selected) = planning((|| {
        let zone = exact_str(zone_id)?;
        if !attribute(world, intern!(py, "zones"))?.contains(zone)? {
            return unsupported("an unknown zone");
        }
        let count = exact_int(count)?;
        if !(1..=MAX_ARRIVALS_PER_EVENT).contains(&count) {
            return unsupported("the count is out of range");
        }
        let include_initial = match include_initial {
            None => false,
            Some(flag) => flag.downcast_exact::<pyo3::types::PyBool>().map_err(|_| Unsupported::new_err("include_initial is not a bool"))?.is_true(),
        };
        let Some(state) = namespace(world, DYNAMICS)? else {
            return unsupported("the population state is made on first use by the Python rule");
        };
        let mut initial: HashSet<Arc<str>> = HashSet::new();
        for id in state.get_item("initial_agent_ids")?.try_iter()? {
            initial.insert(text(&id?)?);
        }
        let store = entities_of(world)?;
        let candidates: Vec<Arc<str>> = EntityStore::with_rows(&store, |rows| {
            Ok(rows
                .agents
                .iter()
                .filter(|(id, agent)| (include_initial || !initial.contains(*id)) && &*agent.zone_id == zone)
                .map(|(id, _)| id.clone())
                .collect())
        })?;
        let mut keyed = Vec::with_capacity(candidates.len());
        for id in candidates {
            let minute = if initial.contains(&id) { -1 } else { arrival_minute(&state, &id)? };
            keyed.push((minute, id));
        }
        keyed.sort_by(|first, second| (first.0, first.1.as_bytes()).cmp(&(second.0, second.1.as_bytes())));
        if (keyed.len() as i64) < count {
            return unsupported("too few residents to leave");
        }
        let selected: Vec<Arc<str>> = keyed.into_iter().take(count as usize).map(|(_, id)| id).collect();
        let busy = EntityStore::with_rows(&store, |rows| {
            Ok(selected.iter().any(|id| {
                rows.agents.get(id).is_none_or(|agent| !matches!(agent.activity, Activity::Idle | Activity::Resting | Activity::Sleeping) || agent.support_request_id.is_some())
            }))
        })?;
        if busy {
            return unsupported("the residents are busy: the Python rule defers");
        }
        let row = event_row(event)?;
        identifier(&row.event_id)?;
        if row.due_minute < 0 {
            return unsupported("a minute before the Run");
        }
        presence_state(world)?;
        for id in &selected {
            identifier(id)?;
        }
        check_information(world)?;
        check_relationship_keys(world)?;
        check_queue_rows(world)?;
        Ok((row, state, selected))
    })())?;
    writing((|| {
        for agent_id in &selected {
            write_departure(world, agent_id, &row, &state)?;
        }
        let metrics = attribute(world, intern!(py, "metric_state"))?;
        let gone = metrics.call_method1(intern!(py, "get"), ("population_departure_count", 0.0))?;
        metrics.set_item("population_departure_count", gone.add(selected.len())?)?;
        metrics.set_item("population_waiting_count", PyFloat::new(py, state.get_item("pending_arrivals")?.len()? as f64))
    })())?;
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    // The departures are written, so the Python rule cannot start over: only this step can fall
    // back, and the wrapper of the module's function then runs the rule of this step.
    match admit_inner(world, minute) {
        Ok(_) => {}
        Err(error) if error.is_instance_of::<Unsupported>(py) => {
            py.import("sheltersim.domain.population")?.getattr("admit_pending_arrivals")?.getattr("__wrapped__")?.call1((world, minute))?;
        }
        Err(error) => return Err(error),
    }
    Ok(selected.len())
}

// ---- Read-only rules -----------------------------------------------------------------------------

#[pyfunction]
fn zone_has_room(world: &Bound<'_, PyAny>, zone_id: &Bound<'_, PyAny>) -> PyResult<bool> {
    planning(Places::of(world)?.has_room(exact_str(zone_id)?))
}

/// `_choose_admission_target`, for the comparison: the plan uses `choose_target`.
#[pyfunction]
fn choose_admission_target<'py>(world: &Bound<'py, PyAny>, item: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let mut places = planning(Places::of(world))?;
    match planning(choose_target(&mut places, item))? {
        None => Ok(py.None().into_bound(py)),
        Some((zone, room)) => {
            let room_id = room.map(|(room_id, _)| room_id);
            Ok(PyTuple::new(py, [zone.into_pyobject(py)?.into_any(), match room_id {
                Some(room_id) => room_id.into_pyobject(py)?.into_any(),
                None => py.None().into_bound(py),
            }])?
            .into_any())
        }
    }
}

// ---- `validate_population_dynamics_state` --------------------------------------------------------

/// `PersonTraits.model_validate`, of a document it certainly accepts.
fn traits_are_plain(value: &V) -> PyResult<()> {
    let V::Map(map) = value else {
        return unsupported("traits are not a mapping");
    };
    const FIELDS: [&str; 7] = ["age_years", "household_id", "household_role", "languages", "accessibility_needs", "medical_support", "source_ids"];
    if map.keys().any(|key| !FIELDS.contains(&&**key)) {
        return unsupported("traits have another field");
    }
    if !matches!(map.get("age_years"), Some(V::Int(age)) if (0..=120).contains(age)) {
        return unsupported("an age is out of range");
    }
    maybe_identifier(map, "household_id")?;
    if !matches!(map.get("household_role"), None) && !matches!(map.get("household_role"), Some(V::Str(role)) if ["adult", "child", "dependent", "alone"].contains(&&**role)) {
        return unsupported("a role is another value");
    }
    identifiers(map.get("languages"))?;
    identifiers(map.get("accessibility_needs"))?;
    if !matches!(map.get("medical_support"), None | Some(V::Bool(_))) {
        return unsupported("medical support is not a bool");
    }
    identifiers(map.get("source_ids"))?;
    Ok(())
}

/// `PersonContext.model_validate` of a document it certainly accepts: the related IDs and sources.
fn context_is_plain(value: &V) -> PyResult<(Option<Arc<str>>, Vec<Arc<str>>, Vec<Arc<str>>)> {
    let V::Map(map) = value else {
        return unsupported("a context is not a mapping");
    };
    const GENERATED: [&str; 8] = ["household_id", "caregiver_ids", "care_for_ids", "requires_review", "review_reasons", "roles", "guardian_status", "source_ids"];
    if map.keys().any(|key| !GENERATED.contains(&&**key) && &**key != "care_support_needs") {
        return unsupported("a context has a field a generated one lacks");
    }
    let caregiver = identifiers(map.get("caregiver_ids"))?;
    let care_for = identifiers(map.get("care_for_ids"))?;
    if caregiver.len() > 16 || care_for.len() > 16 {
        return unsupported("a context has too many people");
    }
    if !matches!(map.get("requires_review"), None | Some(V::Bool(_))) {
        return unsupported("requires_review is not a bool");
    }
    match map.get("review_reasons") {
        None => {}
        Some(V::List(items)) if items.len() <= 32 && items.iter().all(|item| matches!(item, V::Str(_))) => {}
        _ => return unsupported("the review reasons are something else"),
    }
    match map.get("roles") {
        None => {}
        Some(V::List(items)) if items.iter().all(|item| matches!(item, V::Str(role) if ["parent", "child", "caregiver", "dependent", "partner", "alone"].contains(&&**role))) => {}
        _ => return unsupported("the roles are something else"),
    }
    if !matches!(map.get("guardian_status"), None | Some(V::Null)) && !matches!(map.get("guardian_status"), Some(V::Str(status)) if ["accompanied", "unaccompanied", "not_applicable"].contains(&&**status)) {
        return unsupported("a guardian status is another value");
    }
    match map.get("care_support_needs") {
        None => {}
        Some(V::List(items)) if items.len() <= 8 => {
            let mut seen = HashSet::new();
            for item in items {
                let V::Str(kind) = item else {
                    return unsupported("a support kind is not a str");
                };
                if !SUPPORT_KINDS.contains(&&**kind) || !seen.insert(kind.clone()) {
                    return unsupported("a support kind is unknown or repeats");
                }
            }
        }
        _ => return unsupported("the support kinds are something else"),
    }
    let mut related = caregiver;
    related.extend(care_for);
    Ok((maybe_identifier(map, "household_id")?, related, identifiers(map.get("source_ids"))?))
}

/// `validate_population_dynamics_state`, of a state it certainly accepts.
#[pyfunction]
fn validate_population_dynamics_state(world: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    planning((|| {
        let Some(state) = namespace(world, DYNAMICS)? else {
            return Ok(());
        };
        let V::Map(document) = V::from_py(&state)? else {
            return unsupported("the population state is not a mapping");
        };
        let has_context = document.contains_key("profile_context");
        const FIELDS: [&str; 8] = ["schema_version", "policy_version", "source_id", "initial_agent_ids", "profile_traits", "pending_arrivals", "arrivals", "departures"];
        if document.len() != FIELDS.len() + usize::from(has_context) || FIELDS.iter().any(|key| !document.contains_key(*key)) {
            return unsupported("the population state has other fields");
        }
        let good_header = matches!(document.get("schema_version"), Some(V::Int(1)))
            && matches!(document.get("policy_version"), Some(V::Str(text)) if &**text == POLICY_VERSION)
            && matches!(document.get("source_id"), Some(V::Str(text)) if &**text == SOURCE_ID);
        let (Some(V::List(initial)), Some(V::Map(traits)), Some(V::List(pending)), Some(V::List(_)), Some(V::List(_))) =
            (document.get("initial_agent_ids"), document.get("profile_traits"), document.get("pending_arrivals"), document.get("arrivals"), document.get("departures"))
        else {
            return unsupported("the population state's parts are something else");
        };
        if !good_header || pending.len() as i64 > MAX_PENDING_ARRIVALS {
            return unsupported("the population state's header is other");
        }
        let mut historical: HashSet<&str> = HashSet::with_capacity(initial.len() + traits.len());
        for id in initial {
            let V::Str(id) = id else {
                return unsupported("an initial ID is not a str");
            };
            if !historical.insert(id) {
                return unsupported("an initial ID repeats");
            }
        }
        historical.extend(traits.keys().map(|key| &**key));
        let agents = attribute(world, intern!(py, "agents"))?;
        for id in agents.try_iter()? {
            if !historical.contains(exact_str(&id?)?) {
                return unsupported("an active agent has no profile");
            }
        }
        let empty = IndexMap::new();
        let contexts = match document.get("profile_context") {
            None => &empty,
            Some(V::Map(contexts)) => contexts,
            Some(_) => return unsupported("the contexts are something else"),
        };
        for (identity, context) in contexts {
            if !historical.contains(&**identity) {
                return unsupported("a context is of an unknown person");
            }
            let (household, related, sources) = context_is_plain(context)?;
            let Some(household) = household else {
                return unsupported("a context has no household");
            };
            if related.iter().any(|id| id == identity || !historical.contains(&**id))
                || !sources.iter().any(|source| &**source == SOURCE_ID)
                || !matches!(traits.get(identity).and_then(|item| map_get(item, "household_id")), Some(V::Str(text)) if *text == household)
                || related.iter().any(|id| !matches!(traits.get(id).and_then(|item| map_get(item, "household_id")), Some(V::Str(text)) if *text == household))
            {
                return unsupported("a context disagrees with its profile");
            }
        }
        if pending.is_empty() {
            return Ok(());
        }
        check_pending_items(world, pending, contexts)
    })())
}

fn check_pending_items(world: &Bound<'_, PyAny>, pending: &[V], contexts: &IndexMap<Arc<str>, V>) -> PyResult<()> {
    let py = world.py();
    const BASE: [&str; 5] = ["profile", "traits", "zone_id", "created_minute", "event_id"];
    const CONTEXT: [&str; 3] = ["member_index", "care_support_needs", "context_source_id"];
    const APPROACH: [&str; 3] = ["approach_route_id", "approach_started_minute", "approach_due_minute"];
    let zones = attribute(world, intern!(py, "zones"))?;
    let agents = attribute(world, intern!(py, "agents"))?;
    let minute_index = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let duration = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
    let presence = namespace(world, PRESENCE)?;
    let people = match &presence {
        Some(raw) if is_dict(raw) => Some(get_map(raw, "people")?),
        _ => None,
    };
    let mut seen: HashSet<&str> = HashSet::new();
    let mut routes = None;
    let mut events = None;
    for item in pending {
        let V::Map(fields) = item else {
            return unsupported("a pending arrival is not a mapping");
        };
        if BASE.iter().any(|key| !fields.contains_key(*key)) {
            return unsupported("a pending arrival lacks a field");
        }
        let has_context = CONTEXT.iter().filter(|key| fields.contains_key(**key)).count();
        let has_approach = APPROACH.iter().filter(|key| fields.contains_key(**key)).count();
        let completion = fields.contains_key("approach_completion_event_id");
        let completed = fields.contains_key("approach_completed_minute");
        let alternates = fields.contains_key("admission_zone_ids");
        let expected = BASE.len() + has_context + has_approach + usize::from(completion) + usize::from(completed) + usize::from(alternates);
        if fields.len() != expected
            || !(has_context == 0 || has_context == CONTEXT.len())
            || !(has_approach == 0 || has_approach == APPROACH.len())
            || (completion || completed) && has_approach == 0
            || completion && completed
        {
            return unsupported("a pending arrival has another shape");
        }
        let (Some(V::Str(zone)), Some(V::Str(agent_id))) = (fields.get("zone_id"), fields.get("profile").and_then(|profile| map_get(profile, "id"))) else {
            return unsupported("a pending arrival's zone or ID is not a str");
        };
        if !zones.contains(&**zone)? || !seen.insert(agent_id) || agents.contains(&**agent_id)? {
            return unsupported("a pending arrival is invalid");
        }
        if let Some(value) = fields.get("admission_zone_ids") {
            let V::List(ids) = value else {
                return unsupported("the alternate zones are not a list");
            };
            let mut unique = HashSet::new();
            for id in ids {
                let V::Str(id) = id else {
                    return unsupported("an alternate zone is not a str");
                };
                if !unique.insert(id) || !zones.contains(&**id)? {
                    return unsupported("an alternate zone is invalid");
                }
            }
        }
        if has_context > 0 {
            let good = matches!(fields.get("member_index"), Some(V::Int(index)) if (0..MAX_ARRIVALS_PER_EVENT).contains(index))
                && matches!(fields.get("context_source_id"), Some(V::Str(_)));
            let Some(V::List(needs)) = fields.get("care_support_needs") else {
                return unsupported("pending care needs are not a list");
            };
            let mut unique = HashSet::new();
            if !good || needs.iter().any(|need| !matches!(need, V::Str(_)) || !unique.insert(format!("{need:?}"))) {
                return unsupported("a pending context is invalid");
            }
            let saved = contexts.get(agent_id).and_then(|context| map_get(context, "care_support_needs"));
            let same = match saved {
                None => needs.is_empty(),
                Some(V::List(saved)) => saved == needs,
                Some(_) => false,
            };
            if !same {
                return unsupported("a pending care context disagrees");
            }
        }
        if has_approach > 0 {
            let (Some(V::Str(route_id)), Some(V::Int(started)), Some(V::Int(due)), Some(created)) =
                (fields.get("approach_route_id"), fields.get("approach_started_minute"), fields.get("approach_due_minute"), fields.get("created_minute"))
            else {
                return unsupported("an approach's fields are something else");
            };
            if !matches!(created, V::Int(created) if created == started) || due <= started {
                return unsupported("an approach's timing is invalid");
            }
            if routes.is_none() {
                routes = Some(approach_routes(world)?);
            }
            let mut found = None;
            for route in routes.iter().flatten() {
                if attribute(route, intern!(py, "route_id"))?.eq(&**route_id)? {
                    found = Some(route);
                }
            }
            let Some(route) = found else {
                return unsupported("an approach's route is unknown");
            };
            if !attribute(route, intern!(py, "destination_zone_id"))?.eq(&**zone)? || *due != started + exact_int(&attribute(route, intern!(py, "travel_minutes"))?)? {
                return unsupported("an approach disagrees with its route");
            }
            let finished = match fields.get("approach_completed_minute") {
                None | Some(V::Null) => None,
                Some(V::Int(minute)) if minute >= due && *minute <= minute_index => Some(*minute),
                Some(_) => return unsupported("an approach's completion minute is invalid"),
            };
            let completion_id = match fields.get("approach_completion_event_id") {
                None | Some(V::Null) => None,
                Some(V::Str(id)) => Some(id.clone()),
                Some(_) => return unsupported("an approach's completion event is not a str"),
            };
            if finished.is_none() && *due < duration {
                let Some(completion_id) = completion_id else {
                    return unsupported("an approach's completion event is missing");
                };
                if events.is_none() {
                    events = Some(attribute(&attribute(world, intern!(py, "event_queue"))?, intern!(py, "_store"))?);
                }
                let store = events.as_ref().ok_or_else(|| Unsupported::new_err("no queue"))?;
                let queue = store.downcast::<crate::state::events::QueueStore>().map_err(|_| Unsupported::new_err("the queue is not in Rust"))?;
                let Some(event) = queue.borrow_mut().row(py, &completion_id)? else {
                    return unsupported("an approach's completion event is missing");
                };
                if &*event.event_type != "population_approach_complete"
                    || event.due_minute != *due
                    || !matches!(payload(&event, "agent_id"), Some(V::Str(id)) if id == agent_id)
                    || !matches!(payload(&event, "route_id"), Some(V::Str(id)) if id == route_id)
                {
                    return unsupported("an approach's completion event disagrees");
                }
            } else if completion_id.is_some() {
                return unsupported("an approach has a completion event it should not");
            }
            if let Some(people) = &people {
                let person = get(people, agent_id)?;
                let empty = person.is_none() || (is_dict(&person) && person.is_empty()?);
                let expected = if finished.is_none() { "approaching" } else { "awaiting_admission" };
                if !empty && !get(&person, "status")?.eq(expected)? {
                    return unsupported("an approach disagrees with the presence");
                }
            }
        }
        traits_are_plain(fields.get("traits").ok_or_else(|| Unsupported::new_err("no traits"))?)?;
    }
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(admit_pending_arrivals, m)?)?;
    m.add_function(wrap_pyfunction!(queue_arrivals, m)?)?;
    m.add_function(wrap_pyfunction!(apply_arrival_approach_completion, m)?)?;
    m.add_function(wrap_pyfunction!(schedule_population_retry, m)?)?;
    m.add_function(wrap_pyfunction!(queue_departures, m)?)?;
    m.add_function(wrap_pyfunction!(zone_has_room, m)?)?;
    m.add_function(wrap_pyfunction!(choose_admission_target, m)?)?;
    m.add_function(wrap_pyfunction!(validate_population_dynamics_state, m)?)?;
    Ok(())
}
