//! `domain/population_lifecycle.py`: the same person leaving and coming back. See
//! docs/rust-port/lanes/m6-population-rules.md and the head of `rules/population.rs`, whose two
//! stages and whose way of writing these kernels share.
//!
//! `apply_population_presence_command` keeps its parsing of the command and its copy of the world
//! in Python until step 4: `apply_presence_action` is the part of it between those two.

use std::collections::HashSet;
use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

use crate::core::row::{attribute, exact_int, exact_str, unsupported};
use crate::core::value::V;
use crate::rules::movement::{mobility_text, start_movement};
use crate::rules::movement_geometry::{Geo, Pt};
use crate::rules::population::{
    cancel_agent_events, check_admission_paths, check_information, check_queue_rows, check_relationship_keys, drop_support_requests, event_row,
    filter_support_requests, get, get_map, has_namespace, identifier, is_dict, namespace, optional_identifier, optional_text, pair_of,
    payload, person_in, place_in_room, planning, presence_is_plain, presence_state, profile_object, record_hash, release_room, reset_position,
    sha_hex, sorted_keys, sorted_set, strings, text, v_map, write_presence, write_room_admission, writing, Person, Places, Transition, AGENT_DYNAMICS,
    BODY, INFORMATION, MOVEMENT, PRESENCE, SOCIAL_NETWORK, SOCIAL_RUNTIME,
};
use crate::rules::service::active_task_class;
use crate::rules::travel::{route_class, world_route};
use crate::state::entities::{entities_of, Activity, EntityStore};
use crate::state::events::{EventKind, EventRow, EventWriter, QueueStore};
use crate::Unsupported;

const KEY: &str = "population_lifecycle_v1";
const POLICY_VERSION: &str = "same-id-population-lifecycle-v1";
const SOURCE_ID: &str = "ASM-POPULATION-LIFECYCLE-V1";
const MAX_HISTORY: usize = 20_000;
const MAX_RETAINED_PEOPLE: usize = 10_000;
const MAX_RELATIONSHIP_ARCHIVE: usize = 200_000;

fn deep_copy<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    value.py().import("copy")?.getattr("deepcopy")?.call1((value,))
}

fn pop<'py>(map: &Bound<'py, PyAny>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    map.call_method1(intern!(map.py(), "pop"), (key,))
}

fn pop_or_none<'py>(map: &Bound<'py, PyAny>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    let py = map.py();
    map.call_method1(intern!(py, "pop"), (key, py.None()))
}

fn append<'py>(list: &Bound<'py, PyAny>, value: Bound<'py, PyDict>) -> PyResult<()> {
    list.call_method1(intern!(list.py(), "append"), (value,))?;
    Ok(())
}

// ---- `_set_presence`, `_history` -----------------------------------------------------------------

/// What `_set_presence` reads of an event.
struct EventIds {
    event_id: Arc<str>,
    causation_id: Option<Arc<str>>,
    source_id: Arc<str>,
}

impl EventIds {
    /// The event of a command, checked as `PresenceTransition` will.
    fn of(row: &EventRow) -> PyResult<Self> {
        identifier(&row.event_id)?;
        optional_identifier(row.causation_id.as_deref())?;
        let Some(source_id) = row.source_id.clone() else {
            return unsupported("an event without a source");
        };
        identifier(&source_id)?;
        Ok(Self { event_id: row.event_id.clone(), causation_id: row.causation_id.clone(), source_id })
    }
}

#[derive(Default, Clone)]
struct PresenceChange {
    from_zone: Option<Arc<str>>,
    to_zone: Option<Arc<str>>,
    return_zone: Option<Arc<str>>,
    expected_return: Option<i64>,
    household_id: Option<Arc<str>>,
    source_ids: Vec<Arc<str>>,
    action: Option<&'static str>,
}

/// `_set_presence`. A plan has called `presence_state`, so the record there is a plain one.
fn set_presence(presence: &Bound<'_, PyAny>, event: &EventIds, agent_id: &Arc<str>, status: &str, minute: i64, change: &PresenceChange) -> PyResult<()> {
    let old = person_in(presence, agent_id)?;
    let current_household = change.household_id.clone().or_else(|| old.as_ref().and_then(|person| person.household_id.clone()));
    let retained = matches!(status, "temporarily_absent" | "transferred" | "awaiting_admission");
    let archive_path = retained.then(|| vec![Arc::from(KEY), Arc::from("people"), agent_id.clone()]);
    let admitted = status == "admitted";
    let return_zone = if admitted { None } else { change.return_zone.clone().or_else(|| old.as_ref().and_then(|person| person.return_zone_id.clone())) };
    let expected = if admitted { None } else { change.expected_return.or_else(|| old.as_ref().and_then(|person| person.expected_return_minute)) };
    let mut events = old.as_ref().map_or(Vec::new(), |person| person.source_event_ids.clone());
    events.push(event.event_id.clone());
    events.extend(change.source_ids.iter().cloned());
    let person = Person {
        agent_id: agent_id.clone(),
        household_id: current_household.clone(),
        status: Arc::from(status),
        since_minute: minute,
        transition_zone_id: change.to_zone.clone().or_else(|| change.from_zone.clone()).or_else(|| old.as_ref().and_then(|person| person.transition_zone_id.clone())),
        return_zone_id: return_zone,
        expected_return_minute: expected,
        archive_path,
        source_event_ids: sorted_set(events),
    };
    let action = change.action.unwrap_or(match status {
        "temporarily_absent" => "temporary_exit",
        "transferred" => "transferred",
        "awaiting_admission" => "waiting",
        _ => "admitted",
    });
    let digest = sha_hex(&format!("{}\0{agent_id}\0{action}", event.event_id));
    let mut sources = change.source_ids.clone();
    sources.push(event.source_id.clone());
    let transition = Transition {
        transition_id: format!("presence-{}", &digest[..24]),
        event_id: &event.event_id,
        agent_id,
        minute,
        action,
        from_zone_id: change.from_zone.clone(),
        to_zone_id: change.to_zone.clone(),
        from_household_id: old.as_ref().and_then(|person| person.household_id.clone()),
        to_household_id: current_household,
        causation_id: event.causation_id.clone(),
        source_ids: sorted_set(sources),
        state_snapshot_hash: Some(record_hash(&person)?),
    };
    write_presence(presence, &person, &transition)
}

fn append_history(state: &Bound<'_, PyAny>, row: Bound<'_, PyDict>) -> PyResult<()> {
    let py = state.py();
    let history = state.get_item("history")?;
    append(&history, row)?;
    let length = history.len()?;
    if length > MAX_HISTORY {
        history.del_item(pyo3::types::PySlice::new(py, 0, (length - MAX_HISTORY) as isize, 1))?;
    }
    Ok(())
}

/// `current_household_id`.
fn household_of(world: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<Option<Arc<str>>> {
    let py = world.py();
    if let Some(presence) = namespace(world, PRESENCE)?.filter(is_dict) {
        let person = get_map(&get_map(&presence, "people")?, agent_id)?;
        if is_dict(&person) {
            return optional_text(&get(&person, "household_id")?);
        }
    }
    let agent = get(&attribute(world, intern!(py, "agents"))?, agent_id)?;
    if agent.is_none() {
        return Ok(None);
    }
    optional_text(&attribute(&attribute(&agent, intern!(py, "profile"))?, intern!(py, "group_id"))?)
}

#[pyfunction]
fn current_household_id(world: &Bound<'_, PyAny>, agent_id: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    Ok(planning(household_of(world, exact_str(agent_id)?))?.map(|text| text.to_string()))
}

struct Command {
    action: Arc<str>,
    zone_id: Option<Arc<str>>,
    household_id: Option<Arc<str>>,
    expected_return: Option<i64>,
    source_ids: Vec<Arc<str>>,
    ids: Vec<Arc<str>>,
}

impl Command {
    /// What `PopulationPresenceCommand` validated, read back.
    fn of(typed: &Bound<'_, PyAny>, ids: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = typed.py();
        let mut source_ids = Vec::new();
        for source in attribute(typed, intern!(py, "source_ids"))?.try_iter()? {
            source_ids.push(text(&source?)?);
        }
        let expected = attribute(typed, intern!(py, "expected_return_minute"))?;
        let mut people = Vec::new();
        for id in ids.try_iter()? {
            people.push(text(&id?)?);
        }
        Ok(Self {
            action: text(&attribute(typed, intern!(py, "action"))?)?,
            zone_id: optional_text(&attribute(typed, intern!(py, "zone_id"))?)?,
            household_id: optional_text(&attribute(typed, intern!(py, "household_id"))?)?,
            expected_return: if expected.is_none() { None } else { Some(exact_int(&expected)?) },
            source_ids,
            ids: people,
        })
    }

    fn sources_with(&self, event: &EventIds) -> Vec<Arc<str>> {
        let mut sources = self.source_ids.clone();
        sources.push(event.source_id.clone());
        sorted_set(sources)
    }
}

/// `_history`: the row, written, and its copy for the receipt.
#[allow(clippy::too_many_arguments)]
fn history<'py>(
    world: &Bound<'py, PyAny>,
    state: &Bound<'py, PyAny>,
    event: &EventIds,
    agent_id: &str,
    action: &str,
    minute: i64,
    from_zone: Option<&Arc<str>>,
    to_zone: Option<&Arc<str>>,
    command: &Command,
) -> PyResult<Bound<'py, PyDict>> {
    let py = world.py();
    let household = match &command.household_id {
        Some(household) => Some(household.clone()),
        None => household_of(world, agent_id)?,
    };
    let row = || -> PyResult<Bound<'py, PyDict>> {
        let row = PyDict::new(py);
        row.set_item("event_id", &*event.event_id)?;
        row.set_item("agent_id", agent_id)?;
        row.set_item("action", action)?;
        row.set_item("minute", minute)?;
        row.set_item("from_zone_id", from_zone.map(|zone| &**zone))?;
        row.set_item("to_zone_id", to_zone.map(|zone| &**zone))?;
        row.set_item("household_id", household.as_deref())?;
        row.set_item("source_ids", strings(&command.sources_with(event)).to_py(py)?)?;
        Ok(row)
    };
    append_history(state, row()?)?;
    row()
}

// ---- `_archive_person` ---------------------------------------------------------------------------

/// `_archive_relationship`.
fn archive_relationship(state: &Bound<'_, PyAny>, namespace_name: &str, key: &str, pair: &[String; 2], value: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = state.py();
    let archive = state.get_item("relationship_archive")?;
    for item in archive.try_iter()? {
        let item = item?;
        if item.get_item("namespace")?.eq(namespace_name)? && item.get_item("pair_key")?.eq(key)? {
            return Ok(());
        }
    }
    let record = PyDict::new(py);
    record.set_item("namespace", namespace_name)?;
    record.set_item("pair_key", key)?;
    record.set_item("agent_ids", PyList::new(py, pair.iter().map(String::as_str))?)?;
    record.set_item("value", deep_copy(value)?)?;
    record.set_item("restore_when_both_active", true)?;
    append(&archive, record)
}

fn archive_pairs(state: &Bound<'_, PyAny>, name: &str, map: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<()> {
    for key in sorted_keys(map)? {
        let pair = pair_of(&key)?;
        if pair.iter().any(|part| part == agent_id) {
            let value = pop(map, &key)?;
            archive_relationship(state, name, &key, &pair, &value)?;
        }
    }
    Ok(())
}

/// `_agent_record`.
fn agent_record<'py>(agent: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let py = agent.py();
    let profile = attribute(agent, intern!(py, "profile"))?;
    let needs_of = |object: &Bound<'py, PyAny>| -> PyResult<Bound<'py, PyDict>> {
        let needs = PyDict::new(py);
        for name in ["toilet_urgency", "hunger", "fatigue", "hydration_need"] {
            needs.set_item(name, object.getattr(name)?)?;
        }
        Ok(needs)
    };
    let saved = PyDict::new(py);
    for name in ["id", "group_id", "initial_zone_id"] {
        saved.set_item(name, profile.getattr(name)?)?;
    }
    saved.set_item("mobility", profile.getattr("mobility")?.getattr("value")?)?;
    for name in ["support_need", "stress_sensitivity", "rng_key"] {
        saved.set_item(name, profile.getattr(name)?)?;
    }
    saved.set_item("initial_needs", needs_of(&profile.getattr("initial_needs")?)?)?;
    saved.set_item("provenance", deep_copy(&profile.getattr("provenance")?)?)?;
    let record = PyDict::new(py);
    record.set_item("profile", saved)?;
    record.set_item("zone_id", agent.getattr("zone_id")?)?;
    record.set_item("activity", agent.getattr("activity")?.getattr("value")?)?;
    record.set_item("target_id", agent.getattr("target_id")?)?;
    record.set_item("needs", needs_of(&agent.getattr("needs")?)?)?;
    for name in ["stress", "trust", "next_decision_at", "support_request_id", "support_cooldown_until"] {
        record.set_item(name, agent.getattr(name)?)?;
    }
    Ok(record)
}

fn all_finite(value: &V) -> bool {
    match value {
        V::Float(number) => number.is_finite(),
        V::List(items) => items.iter().all(all_finite),
        V::Map(map) => map.values().all(all_finite),
        _ => true,
    }
}

/// `_validate_agent_snapshot` of the record `_agent_record` will make, and the other asks of
/// `_archive_person`.
fn check_archive(world: &Bound<'_, PyAny>, state: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<()> {
    if state.get_item("people")?.contains(agent_id)? {
        return unsupported("the person already has an archive");
    }
    EntityStore::with_rows(&entities_of(world)?, |rows| {
        let Some(agent) = rows.agents.get(agent_id) else {
            return unsupported("no such person");
        };
        let unit = |value: f64| value.is_finite() && (0.0..=1.0).contains(&value);
        let profile = &agent.profile;
        let needs_fit = [agent.needs.toilet_urgency, agent.needs.hunger, agent.needs.fatigue, agent.needs.hydration_need].iter().all(|value| value.is_finite());
        if !unit(agent.stress) || !unit(agent.trust) || !unit(profile.support_need) || !unit(profile.stress_sensitivity) || !needs_fit || !all_finite(&profile.provenance) {
            return unsupported("a person's values do not fit a snapshot");
        }
        Ok(())
    })?;
    check_information(world)?;
    check_relationship_keys(world)?;
    check_queue_rows(world)
}

/// `_archive_person`.
fn archive_person(world: &Bound<'_, PyAny>, agent_id: &str, row: &EventRow, state: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let agents = attribute(world, intern!(py, "agents"))?;
    let agent = agents.get_item(agent_id)?;
    release_room(world, agent_id)?;
    let information = PyDict::new(py);
    information.set_item("beliefs", PyDict::new(py))?;
    information.set_item("query_pressure", py.None())?;
    if let Some(info) = namespace(world, INFORMATION)?.filter(is_dict) {
        let beliefs = info.get_item("beliefs")?;
        let popped = beliefs.call_method1(intern!(py, "pop"), (agent_id, PyDict::new(py)))?;
        information.set_item("beliefs", deep_copy(&popped)?)?;
        information.set_item("query_pressure", info.get_item("query_pressure")?.call_method1(intern!(py, "pop"), (agent_id, 0.0))?)?;
        archive_pairs(state, INFORMATION, &info.get_item("contacts")?, agent_id)?;
        let messages = info.get_item("messages")?;
        for message_id in sorted_keys(&messages)? {
            let message = messages.get_item(&*message_id)?;
            if !get(&message, "origin_agent_id")?.eq(agent_id)? {
                continue;
            }
            let moved = pop(&messages, &message_id)?;
            state.get_item("messages")?.set_item(&*message_id, moved)?;
            let deferred = state.get_item("deferred_beliefs")?;
            let defer = |other: &Bound<'_, PyAny>, belief: Bound<'_, PyAny>| -> PyResult<()> {
                deferred.call_method1(intern!(py, "setdefault"), (other, PyDict::new(py)))?.set_item(&*message_id, belief)
            };
            for item in beliefs.call_method0(intern!(py, "items"))?.try_iter()? {
                let (other, held): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
                let belief = pop_or_none(&held, &message_id)?;
                if !belief.is_none() {
                    defer(&other, belief)?;
                }
            }
            for item in state.get_item("people")?.call_method0(intern!(py, "items"))?.try_iter()? {
                let (archived, archive): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
                let belief = pop_or_none(&archive.get_item("information")?.get_item("beliefs")?, &message_id)?;
                if !belief.is_none() {
                    defer(&archived, belief)?;
                }
            }
        }
        filter_support_requests(&info, agent_id)?;
    }
    let mut withdrawn = HashSet::new();
    for item in state.get_item("messages")?.call_method0(intern!(py, "items"))?.try_iter()? {
        let (message_id, message): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
        if get(&message, "origin_agent_id")?.eq(agent_id)? {
            withdrawn.insert(text(&message_id)?);
        }
    }
    let mut body_agent = py.None().into_bound(py);
    if let Some(body) = namespace(world, BODY)?.filter(is_dict) {
        body_agent = deep_copy(&pop_or_none(&body.get_item("agents")?, agent_id)?)?;
        archive_pairs(state, BODY, &body.get_item("relationships")?, agent_id)?;
    }
    let mut dynamics_record = py.None().into_bound(py);
    if let Some(dynamics) = namespace(world, AGENT_DYNAMICS)?.filter(is_dict) {
        dynamics_record = pop_or_none(&dynamics.get_item("agents")?, agent_id)?;
    }
    if let Some(social) = namespace(world, SOCIAL_NETWORK)?.filter(is_dict) {
        archive_pairs(state, SOCIAL_NETWORK, &social.get_item("relationships")?, agent_id)?;
    }
    if let Some(runtime) = namespace(world, SOCIAL_RUNTIME)?.filter(is_dict) {
        let contacts = get_map(&runtime, "contacts")?;
        archive_pairs(state, SOCIAL_RUNTIME, &contacts, agent_id)?;
        pop_or_none(&get_map(&runtime, "agent_cooldown_until")?, agent_id)?;
    }
    let snapshot = PyDict::new(py);
    snapshot.set_item("agent", agent_record(&agent)?)?;
    snapshot.set_item("agent_dynamics", dynamics_record)?;
    snapshot.set_item("body_agent", body_agent)?;
    snapshot.set_item("information", information)?;
    snapshot.set_item("since_minute", row.due_minute)?;
    let mut events = vec![row.event_id.clone()];
    events.extend(row.causation_id.iter().filter(|id| !id.is_empty()).cloned());
    snapshot.set_item("source_event_ids", strings(&sorted_set(events)).to_py(py)?)?;
    state.get_item("people")?.set_item(agent_id, snapshot)?;
    cancel_agent_events(world, agent_id, &withdrawn)?;
    drop_support_requests(world, agent_id)?;
    if let Some(movement) = namespace(world, MOVEMENT)? {
        pop_or_none(&movement.get_item("journeys")?, agent_id)?;
        pop_or_none(&movement.get_item("positions")?, agent_id)?;
    }
    pop(&agents, agent_id)?;
    Ok(())
}

// ---- `_restore_person` ---------------------------------------------------------------------------

/// What `_restore_person` asks that the Python rule alone answers.
fn check_restore(world: &Bound<'_, PyAny>, state: &Bound<'_, PyAny>, agent_id: &str, zone: &str) -> PyResult<()> {
    let snapshot = get(&state.get_item("people")?, agent_id)?;
    if !is_dict(&snapshot) {
        return unsupported("the retained person is missing");
    }
    if has_namespace(world, AGENT_DYNAMICS)? && snapshot.get_item("agent_dynamics")?.is_none() {
        return unsupported("a new record of all people for one person is the Python rule's");
    }
    check_admission_paths(world, &[zone])
}

/// `_restore_person`.
#[allow(clippy::too_many_arguments)]
fn restore_person(world: &Bound<'_, PyAny>, agent_id: &str, zone_id: &str, minute: i64, state: &Bound<'_, PyAny>, room: Option<&(Arc<str>, Pt)>, event_id: &str) -> PyResult<()> {
    let py = world.py();
    let snapshot = state.get_item("people")?.get_item(agent_id)?;
    let raw = snapshot.get_item("agent")?;
    let (profile, _) = profile_object(world, &raw.get_item("profile")?)?;
    let models = py.import("sheltersim.domain.models")?;
    let needs = PyDict::new(py);
    needs.call_method1(intern!(py, "update"), (raw.get_item("needs")?,))?;
    if exact_int(&attribute(world, intern!(py, "schema_version"))?)? == 5 {
        needs.set_item("hydration_need", 0.0)?;
    }
    let options = PyDict::new(py);
    options.set_item("profile", profile)?;
    options.set_item("zone_id", zone_id)?;
    options.set_item("activity", models.getattr("Activity")?.getattr("IDLE")?)?;
    options.set_item("target_id", py.None())?;
    options.set_item("needs", models.getattr("NeedState")?.call((), Some(&needs))?)?;
    options.set_item("stress", raw.get_item("stress")?)?;
    options.set_item("trust", raw.get_item("trust")?)?;
    options.set_item("next_decision_at", minute)?;
    options.set_item("support_request_id", py.None())?;
    options.set_item("support_cooldown_until", raw.get_item("support_cooldown_until")?)?;
    let agent = models.getattr("AgentState")?.call((), Some(&options))?;
    attribute(world, intern!(py, "agents"))?.set_item(agent_id, agent)?;
    if let Some(dynamics) = namespace(world, AGENT_DYNAMICS)? {
        let agents = dynamics.get_item("agents")?;
        agents.set_item(agent_id, deep_copy(&snapshot.get_item("agent_dynamics")?)?)?;
        agents.get_item(agent_id)?.set_item("decision_due_minute", minute)?;
    }
    if let Some(body) = namespace(world, BODY)? {
        let record = snapshot.get_item("body_agent")?;
        if !record.is_none() {
            body.get_item("agents")?.set_item(agent_id, deep_copy(&record)?)?;
        }
    }
    if let Some(info) = namespace(world, INFORMATION)? {
        let deferred_all = state.get_item("deferred_beliefs")?;
        let deferred = deferred_all.call_method1(intern!(py, "setdefault"), (agent_id, PyDict::new(py)))?;
        let messages = info.get_item("messages")?;
        let saved = snapshot.get_item("information")?;
        let beliefs = saved.get_item("beliefs")?;
        let restored = PyDict::new(py);
        for item in beliefs.call_method0(intern!(py, "items"))?.try_iter()? {
            let (message_id, belief): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
            if messages.contains(&message_id)? {
                restored.set_item(message_id, deep_copy(&belief)?)?;
            }
        }
        info.get_item("beliefs")?.set_item(agent_id, restored)?;
        for item in beliefs.call_method0(intern!(py, "items"))?.try_iter()? {
            let (message_id, belief): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
            if !messages.contains(&message_id)? {
                deferred.set_item(message_id, deep_copy(&belief)?)?;
            }
        }
        if !deferred.is_truthy()? {
            pop_or_none(&deferred_all, agent_id)?;
        }
        let pressure = saved.get_item("query_pressure")?;
        info.get_item("query_pressure")?.set_item(agent_id, if pressure.is_truthy()? { pressure } else { 0.0f64.into_pyobject(py)?.into_any() })?;
        let retained = state.get_item("messages")?;
        let mut moving = Vec::new();
        for item in retained.call_method0(intern!(py, "items"))?.try_iter()? {
            let (message_id, message): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
            if get(&message, "origin_agent_id")?.eq(agent_id)? {
                moving.push(text(&message_id)?);
            }
        }
        for message_id in moving {
            messages.set_item(&*message_id, pop(&retained, &message_id)?)?;
        }
    }
    if let Some(movement) = namespace(world, MOVEMENT)?.filter(is_dict) {
        pop_or_none(&movement.get_item("journeys")?, agent_id)?;
        reset_position(world, &movement, agent_id, zone_id)?;
    }
    if let Some((room_id, point)) = room {
        write_room_admission(world, agent_id, room_id, minute, event_id)?;
        if let Some(movement) = namespace(world, MOVEMENT)?.filter(is_dict) {
            let position = get(&get_map(&movement, "positions")?, agent_id)?;
            if is_dict(&position) {
                place_in_room(&position, zone_id, *point)?;
            }
        }
    }
    restore_ready_relationships(world, state)?;
    restore_deferred_beliefs(world, state)?;
    pop(&state.get_item("people")?, agent_id)?;
    let kept = PyList::empty(py);
    for item in state.get_item("pending_returns")?.try_iter()? {
        let item = item?;
        if !item.get_item("agent_id")?.eq(agent_id)? {
            kept.append(item)?;
        }
    }
    state.set_item("pending_returns", kept)
}

/// `_restore_ready_relationships`. A list that keeps everyone stays, since each new one is read again.
fn restore_ready_relationships(world: &Bound<'_, PyAny>, state: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let agents = attribute(world, intern!(py, "agents"))?;
    let archive = state.get_item("relationship_archive")?;
    let remaining = PyList::empty(py);
    let mut total = 0;
    for item in archive.try_iter()? {
        let item = item?;
        total += 1;
        let ids = item.get_item("agent_ids")?;
        let (first, second) = (ids.get_item(0)?, ids.get_item(1)?);
        if item.get_item("restore_when_both_active")?.is_truthy()? && (!agents.contains(&first)? || !agents.contains(&second)?) {
            remaining.append(item)?;
            continue;
        }
        let name = item.get_item("namespace")?;
        let name_text = exact_str(&name)?;
        let target = namespace(world, name_text)?;
        let Some(target) = target.filter(is_dict) else {
            remaining.append(item)?;
            continue;
        };
        let collection = if name_text == SOCIAL_NETWORK || name_text == BODY {
            target.get_item("relationships")?
        } else if name_text == INFORMATION {
            target.get_item("contacts")?
        } else if name_text == SOCIAL_RUNTIME {
            let contacts = get(&target, "contacts")?;
            if !is_dict(&contacts) {
                remaining.append(item)?;
                continue;
            }
            contacts
        } else {
            remaining.append(item)?;
            continue;
        };
        let key = item.get_item("pair_key")?;
        if !collection.contains(&key)? {
            collection.set_item(key, deep_copy(&item.get_item("value")?)?)?;
        }
    }
    if remaining.len() != total {
        state.set_item("relationship_archive", remaining)?;
    }
    Ok(())
}

/// `_restore_deferred_beliefs`.
fn restore_deferred_beliefs(world: &Bound<'_, PyAny>, state: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let Some(info) = namespace(world, INFORMATION)?.filter(is_dict) else {
        return Ok(());
    };
    let agents = attribute(world, intern!(py, "agents"))?;
    let deferred = state.get_item("deferred_beliefs")?;
    let messages = info.get_item("messages")?;
    for agent_id in sorted_keys(&deferred)? {
        if !agents.contains(&*agent_id)? {
            continue;
        }
        let beliefs = deferred.get_item(&*agent_id)?;
        for message_id in sorted_keys(&beliefs)? {
            if messages.contains(&*message_id)? {
                info.get_item("beliefs")?.get_item(&*agent_id)?.set_item(&*message_id, pop(&beliefs, &message_id)?)?;
            }
        }
        if !beliefs.is_truthy()? {
            pop(&deferred, &agent_id)?;
        }
    }
    Ok(())
}

// ---- The commands ---------------------------------------------------------------------------------

fn free(activity: Activity) -> bool {
    matches!(activity, Activity::Idle | Activity::Resting | Activity::Sleeping)
}

/// `_defer_while_busy`'s question.
fn busy(world: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<bool> {
    EntityStore::with_rows(&entities_of(world)?, |rows| {
        let Some(agent) = rows.agents.get(agent_id) else {
            return unsupported("no such person");
        };
        Ok(!free(agent.activity) || agent.support_request_id.is_some() || rows.active_tasks.iter().any(|(_, task)| task.agent_id.as_deref() == Some(agent_id)))
    })
}

/// `_program_presence_minutes`.
fn program_minutes(world: &Bound<'_, PyAny>, row: &EventRow, agent_id: &str, action: &str) -> PyResult<Vec<i64>> {
    let py = world.py();
    let mut minutes = Vec::new();
    let Some(V::Map(marker)) = crate::rules::population::provenance(row, "effect_program_v1") else {
        return Ok(minutes);
    };
    let Some(V::Str(program_id)) = marker.get("program_id") else {
        return Ok(minutes);
    };
    let Some(effects) = namespace(world, "effect_program_v1")? else {
        return Ok(minutes);
    };
    let program = get(&get_map(&effects, "programs")?, program_id)?;
    if !is_dict(&program) {
        return Ok(minutes);
    }
    let scheduled_ids = program.call_method1(intern!(py, "get"), ("scheduled_event_ids", PyList::empty(py)))?;
    let queue = attribute(world, intern!(py, "event_queue"))?;
    let log = attribute(world, intern!(py, "event_log"))?;
    let pending = PyDict::new(py);
    for item in queue.call_method0(intern!(py, "pending"))?.try_iter()? {
        let item = item?;
        pending.set_item(attribute(&item, intern!(py, "event_id"))?, item)?;
    }
    for event_id in scheduled_ids.try_iter()? {
        let event_id = event_id?;
        let scheduled = match pending.get_item(&event_id)? {
            Some(event) => Some(event),
            None => {
                let record = log.call_method1(intern!(py, "find"), (&event_id,))?;
                if record.is_none() {
                    None
                } else {
                    Some(attribute(&record, intern!(py, "event"))?)
                }
            }
        };
        let Some(scheduled) = scheduled else {
            continue;
        };
        if !attribute(&scheduled, intern!(py, "event_type"))?.eq("effect_program_population")? {
            continue;
        }
        let scheduled = event_row(&scheduled)?;
        if let Some(V::Map(command)) = payload(&scheduled, "command") {
            if matches!(command.get("action"), Some(V::Str(found)) if &**found == action) && matches!(command.get("agent_id"), Some(V::Str(found)) if &**found == agent_id) {
                minutes.push(scheduled.due_minute);
            }
        }
    }
    Ok(minutes)
}

struct PlannedRoute {
    route: Py<PyAny>,
    due: i64,
    event: EventRow,
    task_id: Arc<str>,
}

enum Plan {
    Exit { agent_id: Arc<str>, from_zone: Arc<str> },
    Transfer { agent_id: Arc<str>, from_zone: Arc<str> },
    ReturnWait { agent_id: Arc<str>, destination: Arc<str>, household: Option<Arc<str>>, from_zone: Option<Arc<str>> },
    Return { agent_id: Arc<str>, destination: Arc<str>, household: Option<Arc<str>>, room: Option<(Arc<str>, Pt)> },
    RelocateHere { agent_id: Arc<str>, from_zone: Arc<str> },
    Relocate { agent_id: Arc<str>, from_zone: Arc<str>, destination: Arc<str>, route: Box<PlannedRoute> },
    Reunite { zone: Arc<str> },
}

fn plan_relocation(candidate: &Bound<'_, PyAny>, row: &EventRow, agent_id: &Arc<str>, from_zone: &Arc<str>, destination: &Arc<str>) -> PyResult<Box<PlannedRoute>> {
    let py = candidate.py();
    let configs = attribute(candidate, intern!(py, "model_configs"))?;
    let capacities = get_map(&get_map(&configs, "layout_routing")?, "max_agents_in_transit_by_edge")?;
    if capacities.is_truthy()? {
        return unsupported("passage capacities read every trip under way");
    }
    let Some(route) = world_route(candidate, from_zone, destination)? else {
        return unsupported("no route");
    };
    let mobility = EntityStore::with_rows(&entities_of(candidate)?, |rows| {
        rows.agents.get(&**agent_id).map(mobility_text).ok_or_else(|| Unsupported::new_err("no such person"))
    })?;
    let geo = Geo::of(candidate)?;
    if route.edge_ids.iter().any(|edge| !geo.route_is_clear(edge, mobility)) {
        return unsupported("the route is not clear");
    }
    let zones = PyTuple::new(py, route.zone_ids.iter().map(|id| &**id))?;
    let edges = PyTuple::new(py, route.edge_ids.iter().map(|id| &**id))?;
    let object = route_class(py)?.call1((&**from_zone, &**destination, zones, edges, route.minutes))?;
    let agent_key = PyString::new(py, agent_id);
    let timed = crate::rules::movement::route_with_movement_time(candidate, agent_key.as_any(), &object)?;
    let minutes = exact_int(&attribute(&timed, intern!(py, "travel_minutes"))?)?;
    let due = row.due_minute + minutes;
    let writer = EventWriter::of(candidate)?;
    if due >= writer.duration() {
        return unsupported("the route ends after the scenario");
    }
    if has_namespace(candidate, MOVEMENT)? {
        if minutes <= 0 {
            return unsupported("a journey has no duration");
        }
        check_admission_paths(candidate, &[])?;
    }
    let sequence = writer.next_sequence()?;
    let task_id: Arc<str> = Arc::from(format!("presence-relocation-{sequence}"));
    let event_id: Arc<str> = Arc::from(format!("presence-relocation-arrival-{sequence}"));
    let event = EventRow::new(
        event_id,
        EventKind::Completion,
        due,
        row.priority,
        sequence,
        Arc::from("agent_travel_complete"),
        v_map! {
            "agent_id" => V::Str(agent_id.clone()),
            "task_id" => V::Str(task_id.clone()),
            "purpose" => V::Str(Arc::from("presence_relocation")),
            "destination_zone_id" => V::Str(destination.clone()),
            "origin_zone_id" => V::Str(from_zone.clone()),
            "route_zone_ids" => strings(&route.zone_ids),
            "route_edge_ids" => strings(&route.edge_ids),
            "travel_minutes" => V::Int(minutes),
        },
        Some(Arc::from(SOURCE_ID)),
        Some(row.event_id.clone()),
        v_map! {
            "source_type" => V::Str(Arc::from("assumption")),
            "source_id" => V::Str(Arc::from(SOURCE_ID)),
            "policy_version" => V::Str(Arc::from(POLICY_VERSION)),
        },
    )
    .ok_or_else(|| Unsupported::new_err("ScheduledEvent refuses the arrival"))?;
    Ok(Box::new(PlannedRoute { route: timed.unbind(), due, event, task_id }))
}

/// The plan of a command on `candidate`, which is the copy `apply_population_presence_command` made.
fn plan_action(world: &Bound<'_, PyAny>, candidate: &Bound<'_, PyAny>, row: &EventRow, command: &Command, state: &Bound<'_, PyAny>) -> PyResult<Plan> {
    let py = world.py();
    let minute = row.due_minute;
    let agents = attribute(candidate, intern!(py, "agents"))?;
    let zones = attribute(candidate, intern!(py, "zones"))?;
    let zone_of = |id: &str| -> PyResult<Option<Arc<str>>> {
        let agent = get(&agents, id)?;
        if agent.is_none() {
            Ok(None)
        } else {
            text(&attribute(&agent, intern!(py, "zone_id"))?).map(Some)
        }
    };
    let presence = presence_state(candidate)?;
    let first = command.ids.first().cloned().ok_or_else(|| Unsupported::new_err("no one is selected"))?;
    let action = &*command.action;
    match action {
        "temporary_exit" | "transfer" => {
            let Some(from_zone) = zone_of(&first)? else {
                return unsupported("the person is not admitted");
            };
            if action == "temporary_exit" && program_minutes(world, row, &first, "return")?.iter().any(|due| *due <= minute) {
                return unsupported("the return of the same program has passed");
            }
            if busy(world, &first)? {
                return unsupported("the person is busy: the Python rule defers");
            }
            identifier(&first)?;
            identifier(&from_zone)?;
            check_archive(candidate, state, &first)?;
            if let Some(raw) = &presence {
                let old = person_in(raw, &first)?;
                let expected = command.expected_return.or_else(|| old.and_then(|person| person.expected_return_minute));
                if action == "temporary_exit" && expected.is_some_and(|due| due < minute) {
                    return unsupported("an expected return before the exit");
                }
                if let Some(zone) = &command.zone_id {
                    identifier(zone)?;
                }
            }
            if action == "temporary_exit" {
                Ok(Plan::Exit { agent_id: first, from_zone })
            } else {
                Ok(Plan::Transfer { agent_id: first, from_zone })
            }
        }
        "return" => {
            let admitted = agents.contains(&*first)?;
            if admitted && program_minutes(world, row, &first, "temporary_exit")?.iter().any(|due| *due < minute) {
                return unsupported("the exit of the same program was dropped");
            }
            if admitted || !state.get_item("people")?.contains(&*first)? {
                return unsupported("no retained absent person");
            }
            let old = match &presence {
                Some(raw) => person_in(raw, &first)?,
                None => None,
            };
            let destination = command.zone_id.clone().or_else(|| old.as_ref().and_then(|person| person.return_zone_id.clone()));
            let Some(destination) = destination.filter(|zone| zones.contains(&**zone).unwrap_or(false)) else {
                return unsupported("no valid destination");
            };
            identifier(&destination)?;
            identifier(&first)?;
            let household = command.household_id.clone().or_else(|| old.as_ref().and_then(|person| person.household_id.clone()));
            let mut places = Places::of(candidate)?;
            if places.has_room_for(&destination, None)? {
                check_restore(candidate, state, &first, &destination)?;
                check_information(candidate)?;
                return Ok(Plan::Return { agent_id: first, destination, household, room: None });
            }
            match places.available_room(&destination)? {
                Some(room) => {
                    check_restore(candidate, state, &first, &destination)?;
                    check_information(candidate)?;
                    Ok(Plan::Return { agent_id: first, destination, household, room: Some(room) })
                }
                None => Ok(Plan::ReturnWait { agent_id: first, destination, household, from_zone: old.and_then(|person| person.transition_zone_id) }),
            }
        }
        "relocate" => {
            let Some(from_zone) = zone_of(&first)? else {
                return unsupported("the person is not admitted");
            };
            let Some(destination) = command.zone_id.clone().filter(|zone| zones.contains(&**zone).unwrap_or(false)) else {
                return unsupported("no valid destination");
            };
            let activity = EntityStore::with_rows(&entities_of(candidate)?, |rows| {
                rows.agents.get(&*first).map(|agent| agent.activity).ok_or_else(|| Unsupported::new_err("no row"))
            })?;
            if !free(activity) {
                return unsupported("the person is in an active task");
            }
            if !Places::of(candidate)?.has_room_for(&destination, Some(&first))? {
                return unsupported("the destination has no room");
            }
            identifier(&first)?;
            identifier(&from_zone)?;
            identifier(&destination)?;
            if destination == from_zone {
                Ok(Plan::RelocateHere { agent_id: first, from_zone })
            } else {
                let route = plan_relocation(candidate, row, &first, &from_zone, &destination)?;
                Ok(Plan::Relocate { agent_id: first, from_zone, destination, route })
            }
        }
        "reunite" => {
            if command.household_id.is_none() || presence.is_none() {
                return unsupported("a reunion needs a household and the presence contract");
            }
            let mut zone: Option<Arc<str>> = None;
            for id in &command.ids {
                identifier(id)?;
                let Some(found) = zone_of(id)? else {
                    return unsupported("someone is not admitted");
                };
                match &zone {
                    Some(known) if *known != found => return unsupported("the people are not co-located"),
                    _ => zone = Some(found),
                }
            }
            let zone = zone.ok_or_else(|| Unsupported::new_err("no one is selected"))?;
            identifier(&zone)?;
            Ok(Plan::Reunite { zone })
        }
        _ => unsupported("another action"),
    }
}

fn write_action<'py>(candidate: &Bound<'py, PyAny>, row: &EventRow, event: &EventIds, command: &Command, state: &Bound<'py, PyAny>, plan: Plan) -> PyResult<Bound<'py, PyAny>> {
    let py = candidate.py();
    let minute = row.due_minute;
    let change = |value: PresenceChange| PresenceChange { household_id: command.household_id.clone(), source_ids: command.source_ids.clone(), ..value };
    let presence = namespace(candidate, PRESENCE)?.filter(is_dict);
    let receipt = match plan {
        Plan::Exit { agent_id, from_zone } | Plan::Transfer { agent_id, from_zone } => {
            let exit = &*command.action == "temporary_exit";
            archive_person(candidate, &agent_id, row, state)?;
            if let Some(raw) = &presence {
                let status = if exit { "temporarily_absent" } else { "transferred" };
                let return_zone = if exit { Some(from_zone.clone()) } else { command.zone_id.clone() };
                set_presence(raw, event, &agent_id, status, minute, &change(PresenceChange { from_zone: Some(from_zone.clone()), return_zone, expected_return: command.expected_return, ..Default::default() }))?;
            }
            let (action, to_zone) = if exit { ("temporary_exit", None) } else { ("transferred", command.zone_id.as_ref()) };
            history(candidate, state, event, &agent_id, action, minute, Some(&from_zone), to_zone, command)?
        }
        Plan::ReturnWait { agent_id, destination, household, from_zone } => {
            let item = PyDict::new(py);
            item.set_item("agent_id", &*agent_id)?;
            item.set_item("zone_id", &*destination)?;
            item.set_item("event_id", &*event.event_id)?;
            item.set_item("minute", minute)?;
            item.set_item("household_id", household.as_deref())?;
            item.set_item("source_ids", strings(&command.sources_with(event)).to_py(py)?)?;
            let kept = PyList::empty(py);
            for other in state.get_item("pending_returns")?.try_iter()? {
                let other = other?;
                if !other.get_item("agent_id")?.eq(&*agent_id)? {
                    kept.append(other)?;
                }
            }
            state.set_item("pending_returns", kept)?;
            append(&state.get_item("pending_returns")?, item)?;
            if let Some(raw) = &presence {
                let details = PresenceChange { from_zone, return_zone: Some(destination.clone()), household_id: household, source_ids: command.source_ids.clone(), ..Default::default() };
                set_presence(raw, event, &agent_id, "awaiting_admission", minute, &details)?;
            }
            history(candidate, state, event, &agent_id, "waiting", minute, None, Some(&destination), command)?
        }
        Plan::Return { agent_id, destination, household, room } => {
            restore_person(candidate, &agent_id, &destination, minute, state, room.as_ref(), &event.event_id)?;
            if let Some(raw) = &presence {
                let details = PresenceChange { to_zone: Some(destination.clone()), household_id: household, source_ids: command.source_ids.clone(), ..Default::default() };
                set_presence(raw, event, &agent_id, "admitted", minute, &details)?;
            }
            history(candidate, state, event, &agent_id, "returned", minute, None, Some(&destination), command)?
        }
        Plan::RelocateHere { agent_id, from_zone } => {
            attribute(candidate, intern!(py, "agents"))?.get_item(&*agent_id)?.setattr(intern!(py, "next_decision_at"), minute + 1)?;
            if let Some(raw) = &presence {
                set_presence(raw, event, &agent_id, "admitted", minute, &change(PresenceChange { from_zone: Some(from_zone.clone()), to_zone: Some(from_zone.clone()), action: Some("relocated"), ..Default::default() }))?;
            }
            history(candidate, state, event, &agent_id, "relocated", minute, Some(&from_zone), Some(&from_zone), command)?
        }
        Plan::Relocate { agent_id, from_zone, destination, route } => {
            release_room(candidate, &agent_id)?;
            let writer = EventWriter::of(candidate)?;
            writer.allocate_sequence()?;
            let event_id = route.event.event_id.clone();
            let task = PyDict::new(py);
            task.set_item("id", &*route.task_id)?;
            task.set_item("kind", "travel_return")?;
            task.set_item("agent_id", &*agent_id)?;
            task.set_item("resource_id", py.None())?;
            task.set_item("start_minute", minute)?;
            task.set_item("due_minute", route.due)?;
            let task = active_task_class(py)?.call((), Some(&task))?;
            attribute(candidate, intern!(py, "active_tasks"))?.set_item(&*route.task_id, task)?;
            let agent = attribute(candidate, intern!(py, "agents"))?.get_item(&*agent_id)?;
            agent.setattr(intern!(py, "activity"), py.import("sheltersim.domain.models")?.getattr("Activity")?.getattr("MOVING")?)?;
            agent.setattr(intern!(py, "target_id"), &*destination)?;
            writer.schedule(route.event.clone())?;
            if has_namespace(candidate, MOVEMENT)? {
                let none = py.None().into_bound(py);
                start_movement(
                    candidate,
                    PyString::new(py, &agent_id).as_any(),
                    route.route.bind(py),
                    PyString::new(py, "presence_relocation").as_any(),
                    &none,
                    minute.into_pyobject(py)?.as_any(),
                    route.due.into_pyobject(py)?.as_any(),
                    PyString::new(py, &event_id).as_any(),
                    PyString::new(py, &route.task_id).as_any(),
                    None,
                    None,
                    None,
                    None,
                )?;
            }
            let sources = strings(&command.sources_with(event));
            let record = PyDict::new(py);
            record.set_item("task_id", &*route.task_id)?;
            record.set_item("event_id", &*event_id)?;
            record.set_item("from_zone_id", &*from_zone)?;
            record.set_item("to_zone_id", &*destination)?;
            record.set_item("household_id", command.household_id.as_deref())?;
            record.set_item("source_ids", sources.to_py(py)?)?;
            record.set_item("started_minute", minute)?;
            record.set_item("due_minute", route.due)?;
            state.get_item("pending_relocations")?.set_item(&*agent_id, record)?;
            let receipt = PyDict::new(py);
            receipt.set_item("event_id", &*event.event_id)?;
            receipt.set_item("agent_id", &*agent_id)?;
            receipt.set_item("action", "relocation_scheduled")?;
            receipt.set_item("minute", minute)?;
            receipt.set_item("from_zone_id", &*from_zone)?;
            receipt.set_item("to_zone_id", &*destination)?;
            receipt.set_item("arrival_minute", route.due)?;
            receipt.set_item("task_id", &*route.task_id)?;
            receipt.set_item("source_ids", sources.to_py(py)?)?;
            receipt
        }
        Plan::Reunite { zone } => {
            let raw = presence.as_ref().ok_or_else(|| Unsupported::new_err("the presence left"))?;
            for agent_id in &command.ids {
                set_presence(raw, event, agent_id, "admitted", minute, &change(PresenceChange { from_zone: Some(zone.clone()), to_zone: Some(zone.clone()), action: Some("reunited"), ..Default::default() }))?;
                history(candidate, state, event, agent_id, "reunited", minute, Some(&zone), Some(&zone), command)?;
            }
            let receipt = PyDict::new(py);
            receipt.set_item("event_id", &*event.event_id)?;
            receipt.set_item("agent_ids", strings(&command.ids).to_py(py)?)?;
            receipt.set_item("household_id", command.household_id.as_deref())?;
            receipt.set_item("minute", minute)?;
            receipt.set_item("action", "reunited")?;
            receipt
        }
    };
    Ok(receipt.into_any())
}

/// The part of `apply_population_presence_command` between its parse and its validation.
#[pyfunction]
fn apply_presence_action<'py>(
    world: &Bound<'py, PyAny>,
    candidate: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    typed: &Bound<'py, PyAny>,
    ids: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let (row, command, state, ids_of_event, plan) = planning((|| {
        let row = event_row(event)?;
        let command = Command::of(typed, ids)?;
        let Some(state) = namespace(candidate, KEY)? else {
            return unsupported("the lifecycle is not installed");
        };
        let ids_of_event = EventIds::of(&row)?;
        let plan = plan_action(world, candidate, &row, &command, &state)?;
        Ok((row, command, state, ids_of_event, plan))
    })())?;
    writing(write_action(candidate, &row, &ids_of_event, &command, &state, plan))
}

// ---- `admit_pending_returns` ---------------------------------------------------------------------

struct PendingReturn<'py> {
    item: Bound<'py, PyAny>,
    minute: i64,
    event_id: Arc<str>,
    agent_id: Arc<str>,
    zone_id: Arc<str>,
}

fn returns_inner<'py>(world: &Bound<'py, PyAny>, minute: i64) -> PyResult<Bound<'py, PyTuple>> {
    let py = world.py();
    let plan = planning((|| {
        let Some(state) = namespace(world, KEY)?.filter(is_dict) else {
            return Ok(None);
        };
        let pending = state.get_item("pending_returns")?;
        if !pending.is_truthy()? {
            return Ok(None);
        }
        presence_state(world)?;
        let mut waiting = Vec::new();
        for item in pending.try_iter()? {
            let item = item?;
            waiting.push(PendingReturn {
                minute: exact_int(&item.get_item("minute")?)?,
                event_id: text(&item.get_item("event_id")?)?,
                agent_id: text(&item.get_item("agent_id")?)?,
                zone_id: text(&item.get_item("zone_id")?)?,
                item,
            });
        }
        waiting.sort_by(|first, second| (first.minute, first.event_id.as_bytes(), first.agent_id.as_bytes()).cmp(&(second.minute, second.event_id.as_bytes(), second.agent_id.as_bytes())));
        let mut places = Places::of(world)?;
        let mut admit = Vec::new();
        let mut remaining = Vec::new();
        for entry in waiting {
            let room = if places.has_room_for(&entry.zone_id, None)? {
                Some(None)
            } else {
                places.available_room(&entry.zone_id)?.map(Some)
            };
            let Some(room) = room else {
                remaining.push(entry.item);
                continue;
            };
            check_restore(world, &state, &entry.agent_id, &entry.zone_id)?;
            identifier(&entry.event_id)?;
            identifier(&entry.agent_id)?;
            identifier(&entry.zone_id)?;
            optional_identifier(optional_text(&entry.item.get_item("household_id")?)?.as_deref())?;
            for source in entry.item.get_item("source_ids")?.try_iter()? {
                identifier(exact_str(&source?)?)?;
            }
            places.add(&entry.zone_id);
            if let Some((room_id, point)) = &room {
                places.take_room(room_id, *point)?;
            }
            admit.push((entry, room));
        }
        if !admit.is_empty() {
            check_information(world)?;
            check_relationship_keys(world)?;
        }
        check_lifecycle(world, None)?;
        Ok(Some((state, pending, admit, remaining)))
    })())?;
    let Some((state, pending, admit, remaining)) = plan else {
        return Ok(PyTuple::empty(py));
    };
    writing((|| {
        let presence = namespace(world, PRESENCE)?.filter(is_dict);
        let mut admitted: Vec<Arc<str>> = Vec::with_capacity(admit.len());
        for (entry, room) in &admit {
            restore_person(world, &entry.agent_id, &entry.zone_id, minute, &state, room.as_ref(), &entry.event_id)?;
            let household = optional_text(&entry.item.get_item("household_id")?)?;
            let mut sources = Vec::new();
            for source in entry.item.get_item("source_ids")?.try_iter()? {
                sources.push(text(&source?)?);
            }
            if let Some(raw) = &presence {
                let synthetic = EventIds { event_id: entry.event_id.clone(), causation_id: None, source_id: Arc::from(SOURCE_ID) };
                let details = PresenceChange { to_zone: Some(entry.zone_id.clone()), household_id: household.clone(), source_ids: sources.clone(), action: Some("returned"), ..Default::default() };
                set_presence(raw, &synthetic, &entry.agent_id, "admitted", minute, &details)?;
            }
            let row = PyDict::new(py);
            row.set_item("event_id", &*entry.event_id)?;
            row.set_item("agent_id", &*entry.agent_id)?;
            row.set_item("action", "admitted")?;
            row.set_item("minute", minute)?;
            row.set_item("from_zone_id", py.None())?;
            row.set_item("to_zone_id", &*entry.zone_id)?;
            row.set_item("household_id", household.as_deref())?;
            row.set_item("source_ids", strings(&sources).to_py(py)?)?;
            append_history(&state, row)?;
            admitted.push(entry.agent_id.clone());
        }
        // A list of the same people in the same order is the same state.
        let mut same = admit.is_empty() && remaining.len() == pending.len()?;
        if same {
            for (index, item) in remaining.iter().enumerate() {
                if !item.is(&pending.get_item(index)?) {
                    same = false;
                    break;
                }
            }
        }
        if !same {
            state.set_item("pending_returns", PyList::new(py, &remaining)?)?;
        }
        restore_ready_relationships(world, &state)?;
        PyTuple::new(py, admitted.iter().map(|id| &**id))
    })())
}

#[pyfunction]
pub(crate) fn admit_pending_returns<'py>(world: &Bound<'py, PyAny>, minute: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
    returns_inner(world, exact_int(minute)?)
}

// ---- `complete_presence_relocation` --------------------------------------------------------------

#[pyfunction]
fn complete_presence_relocation(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let (row, state, agent_id, relocation, ids_of_event) = planning((|| {
        let row = event_row(event)?;
        let Some(V::Str(agent_id)) = payload(&row, "agent_id").cloned() else {
            return unsupported("the event names no one");
        };
        let Some(state) = namespace(world, KEY)?.filter(is_dict) else {
            return unsupported("no lifecycle state");
        };
        let relocation = get(&get_map(&state, "pending_relocations")?, &agent_id)?;
        if !is_dict(&relocation)
            || !relocation.get_item("event_id")?.eq(&*row.event_id)?
            || !relocation.get_item("due_minute")?.eq(row.due_minute)?
        {
            return unsupported("the completion does not match its route");
        }
        let to_zone = text(&relocation.get_item("to_zone_id")?)?;
        let in_place = EntityStore::with_rows(&entities_of(world)?, |rows| Ok(rows.agents.get(&*agent_id).is_some_and(|agent| agent.zone_id == to_zone)))?;
        if !in_place {
            return unsupported("the person is not at the destination");
        }
        presence_state(world)?;
        let ids_of_event = EventIds::of(&row)?;
        identifier(&agent_id)?;
        identifier(&to_zone)?;
        identifier(exact_str(&relocation.get_item("from_zone_id")?)?)?;
        optional_identifier(optional_text(&relocation.get_item("household_id")?)?.as_deref())?;
        for source in relocation.get_item("source_ids")?.try_iter()? {
            identifier(exact_str(&source?)?)?;
        }
        check_lifecycle(world, Some(&agent_id))?;
        Ok((row, state, agent_id, relocation, ids_of_event))
    })())?;
    writing((|| {
        let from_zone = text(&relocation.get_item("from_zone_id")?)?;
        let to_zone = text(&relocation.get_item("to_zone_id")?)?;
        let household = optional_text(&relocation.get_item("household_id")?)?;
        let mut sources = Vec::new();
        for source in relocation.get_item("source_ids")?.try_iter()? {
            sources.push(text(&source?)?);
        }
        if let Some(raw) = namespace(world, PRESENCE)?.filter(is_dict) {
            let details = PresenceChange { from_zone: Some(from_zone.clone()), to_zone: Some(to_zone.clone()), household_id: household.clone(), source_ids: sources.clone(), action: Some("relocated"), ..Default::default() };
            set_presence(&raw, &ids_of_event, &agent_id, "admitted", row.due_minute, &details)?;
        }
        let history_row = PyDict::new(py);
        history_row.set_item("event_id", &*row.event_id)?;
        history_row.set_item("agent_id", &*agent_id)?;
        history_row.set_item("action", "relocated")?;
        history_row.set_item("minute", row.due_minute)?;
        history_row.set_item("from_zone_id", &*from_zone)?;
        history_row.set_item("to_zone_id", &*to_zone)?;
        history_row.set_item("household_id", household.as_deref())?;
        history_row.set_item("source_ids", strings(&sources).to_py(py)?)?;
        append_history(&state, history_row)?;
        pop(&state.get_item("pending_relocations")?, &agent_id)?;
        Ok(())
    })())?;
    // Written, so the Python rule cannot start over: only this step can fall back to its rule.
    match returns_inner(world, row.due_minute) {
        Ok(_) => Ok(()),
        Err(error) if error.is_instance_of::<Unsupported>(py) => {
            crate::core::census::caught(py, "__wrapped__ admit_pending_returns (after writes)", &error);
            py.import("sheltersim.domain.population_lifecycle")?.getattr("admit_pending_returns")?.getattr("__wrapped__")?.call1((world, row.due_minute))?;
            Ok(())
        }
        Err(error) => Err(error),
    }
}

// ---- Room ----------------------------------------------------------------------------------------

#[pyfunction]
#[pyo3(signature = (world, zone_id, *, excluding=None))]
fn zone_has_room(world: &Bound<'_, PyAny>, zone_id: &Bound<'_, PyAny>, excluding: Option<&str>) -> PyResult<bool> {
    planning(Places::of(world)?.has_room_for(exact_str(zone_id)?, excluding))
}

// ---- `validate_population_lifecycle_state` -------------------------------------------------------

fn snapshot_is_plain(raw: &V, expected_id: &str) -> PyResult<()> {
    let V::Map(fields) = raw else {
        return unsupported("a snapshot is not a mapping");
    };
    const FIELDS: [&str; 10] = ["profile", "zone_id", "activity", "target_id", "needs", "stress", "trust", "next_decision_at", "support_request_id", "support_cooldown_until"];
    if fields.len() != FIELDS.len() || FIELDS.iter().any(|key| !fields.contains_key(*key)) {
        return unsupported("a snapshot has other fields");
    }
    let Some(V::Map(profile)) = fields.get("profile") else {
        return unsupported("a snapshot's profile is not a mapping");
    };
    const PROFILE: [&str; 9] = ["id", "group_id", "initial_zone_id", "mobility", "support_need", "stress_sensitivity", "rng_key", "initial_needs", "provenance"];
    if profile.len() != PROFILE.len() || PROFILE.iter().any(|key| !profile.contains_key(*key)) || !matches!(profile.get("id"), Some(V::Str(id)) if &**id == expected_id) || !matches!(profile.get("initial_needs"), Some(V::Map(_))) {
        return unsupported("a snapshot's profile is other");
    }
    let py_ok = Python::with_gil(|py| -> PyResult<bool> {
        let models = py.import("sheltersim.domain.models")?;
        let activity = fields.get("activity").ok_or_else(|| Unsupported::new_err("no activity"))?.to_py(py)?;
        let mobility = profile.get("mobility").ok_or_else(|| Unsupported::new_err("no mobility"))?.to_py(py)?;
        Ok(models.getattr("Activity")?.call1((activity,)).is_ok() && models.getattr("Mobility")?.call1((mobility,)).is_ok())
    })?;
    if !py_ok {
        return unsupported("a snapshot's enum is another value");
    }
    for value in [fields.get("stress"), fields.get("trust"), profile.get("support_need"), profile.get("stress_sensitivity")] {
        let number = match value {
            Some(V::Float(number)) => *number,
            Some(V::Int(number)) => *number as f64,
            _ => return unsupported("a snapshot's value is not a number"),
        };
        if !number.is_finite() || !(0.0..=1.0).contains(&number) {
            return unsupported("a snapshot's value is out of range");
        }
    }
    Ok(())
}

/// `validate_population_lifecycle_state`, of a state it certainly accepts: Unsupported otherwise.
/// `leaving` is a person whose finished relocation is about to be dropped.
fn check_lifecycle(world: &Bound<'_, PyAny>, leaving: Option<&str>) -> PyResult<()> {
    let py = world.py();
    let Some(state) = namespace(world, KEY)? else {
        return Ok(());
    };
    let V::Map(document) = V::from_py(&state)? else {
        return unsupported("the lifecycle state is not a mapping");
    };
    const FIELDS: [&str; 10] = ["schema_version", "policy_version", "source_id", "people", "relationship_archive", "messages", "deferred_beliefs", "pending_returns", "pending_relocations", "history"];
    if document.len() != FIELDS.len() || FIELDS.iter().any(|key| !document.contains_key(*key)) {
        return unsupported("the lifecycle state has other fields");
    }
    let header = matches!(document.get("schema_version"), Some(V::Int(1)))
        && matches!(document.get("policy_version"), Some(V::Str(text)) if &**text == POLICY_VERSION)
        && matches!(document.get("source_id"), Some(V::Str(text)) if &**text == SOURCE_ID);
    let (Some(V::Map(people)), Some(V::List(archive)), Some(V::Map(messages)), Some(V::Map(deferred)), Some(V::List(returns)), Some(V::Map(relocations)), Some(V::List(history_rows))) = (
        document.get("people"),
        document.get("relationship_archive"),
        document.get("messages"),
        document.get("deferred_beliefs"),
        document.get("pending_returns"),
        document.get("pending_relocations"),
        document.get("history"),
    ) else {
        return unsupported("the lifecycle state's parts are something else");
    };
    if !header || people.len() > MAX_RETAINED_PEOPLE || archive.len() > MAX_RELATIONSHIP_ARCHIVE || history_rows.len() > MAX_HISTORY {
        return unsupported("the lifecycle state's header is other");
    }
    if !all_finite(&V::Map(document.clone())) {
        return unsupported("the lifecycle state holds a value that is not finite");
    }
    let agents = attribute(world, intern!(py, "agents"))?;
    let zones = attribute(world, intern!(py, "zones"))?;
    const RECORD: [&str; 6] = ["agent", "agent_dynamics", "body_agent", "information", "since_minute", "source_event_ids"];
    for (agent_id, record) in people {
        let V::Map(fields) = record else {
            return unsupported("an archived person is not a mapping");
        };
        if agents.contains(&**agent_id)? || fields.len() != RECORD.len() || RECORD.iter().any(|key| !fields.contains_key(*key)) {
            return unsupported("an archived person is invalid");
        }
        if !matches!(fields.get("since_minute"), Some(V::Int(minute)) if *minute >= 0) || !matches!(fields.get("source_event_ids"), Some(V::List(_))) {
            return unsupported("an archived person's fields are other");
        }
        let Some(V::Map(information)) = fields.get("information") else {
            return unsupported("an archived person's information is other");
        };
        if information.len() != 2 || !matches!(information.get("beliefs"), Some(V::Map(_))) || !information.contains_key("query_pressure") {
            return unsupported("an archived person's information is other");
        }
        snapshot_is_plain(fields.get("agent").ok_or_else(|| Unsupported::new_err("no agent"))?, agent_id)?;
    }
    let mut pending_ids: Vec<&str> = Vec::with_capacity(returns.len());
    const RETURN: [&str; 6] = ["agent_id", "zone_id", "event_id", "minute", "household_id", "source_ids"];
    for item in returns {
        let V::Map(fields) = item else {
            return unsupported("a pending return is not a mapping");
        };
        if fields.len() != RETURN.len() || RETURN.iter().any(|key| !fields.contains_key(*key)) {
            return unsupported("a pending return has other fields");
        }
        let (Some(V::Str(agent_id)), Some(V::Str(zone)), Some(V::Str(_)), Some(V::Int(minute)), Some(V::List(_))) =
            (fields.get("agent_id"), fields.get("zone_id"), fields.get("event_id"), fields.get("minute"), fields.get("source_ids"))
        else {
            return unsupported("a pending return's fields are other");
        };
        if !people.contains_key(agent_id) || agents.contains(&**agent_id)? || !zones.contains(&**zone)? || pending_ids.contains(&&**agent_id) || *minute < 0 {
            return unsupported("a pending return is invalid");
        }
        pending_ids.push(agent_id);
    }
    const RELOCATION: [&str; 8] = ["task_id", "event_id", "from_zone_id", "to_zone_id", "household_id", "source_ids", "started_minute", "due_minute"];
    let queue = attribute(&attribute(world, intern!(py, "event_queue"))?, intern!(py, "_store")).ok().and_then(|store| store.downcast_into::<QueueStore>().ok());
    for (agent_id, relocation) in relocations {
        let V::Map(fields) = relocation else {
            return unsupported("a pending relocation is not a mapping");
        };
        if !agents.contains(&**agent_id)? || fields.len() != RELOCATION.len() || RELOCATION.iter().any(|key| !fields.contains_key(*key)) {
            return unsupported("a pending relocation is invalid");
        }
        let (Some(V::Str(task_id)), Some(V::Str(event_id)), Some(V::Str(from)), Some(V::Str(to)), Some(V::List(_)), Some(V::Int(started)), Some(V::Int(due))) = (
            fields.get("task_id"),
            fields.get("event_id"),
            fields.get("from_zone_id"),
            fields.get("to_zone_id"),
            fields.get("source_ids"),
            fields.get("started_minute"),
            fields.get("due_minute"),
        ) else {
            return unsupported("a pending relocation's fields are other");
        };
        if !zones.contains(&**from)? || !zones.contains(&**to)? || due <= started {
            return unsupported("a pending relocation is invalid");
        }
        if leaving == Some(&**agent_id) {
            continue;
        }
        let task_fits = EntityStore::with_rows(&entities_of(world)?, |rows| {
            let matching: Vec<_> = rows.active_tasks.iter().filter(|(_, task)| task.id == *task_id).collect();
            Ok(matching.len() == 1 && matching[0].1.agent_id.as_deref() == Some(&**agent_id) && &*matching[0].1.kind == "travel_return" && matching[0].1.due_minute == *due)
        })?;
        let Some(queue) = &queue else {
            return unsupported("the queue is not in Rust");
        };
        let event = queue.borrow_mut().row(py, event_id)?;
        let event_fits = event.is_some_and(|event| &*event.event_type == "agent_travel_complete" && matches!(payload(&event, "purpose"), Some(V::Str(purpose)) if &**purpose == "presence_relocation"));
        if !task_fits {
            return unsupported("a pending relocation's task disagrees");
        }
        if !event_fits {
            return unsupported("a pending relocation's event disagrees");
        }
    }
    const NAMES: [&str; 4] = [BODY, SOCIAL_NETWORK, INFORMATION, SOCIAL_RUNTIME];
    const ARCHIVE: [&str; 5] = ["namespace", "pair_key", "agent_ids", "value", "restore_when_both_active"];
    for item in archive {
        let V::Map(fields) = item else {
            return unsupported("an archived tie is not a mapping");
        };
        if fields.len() != ARCHIVE.len() || ARCHIVE.iter().any(|key| !fields.contains_key(*key)) {
            return unsupported("an archived tie has other fields");
        }
        let good = matches!(fields.get("namespace"), Some(V::Str(name)) if NAMES.contains(&&**name))
            && matches!(fields.get("pair_key"), Some(V::Str(_)))
            && matches!(fields.get("agent_ids"), Some(V::List(ids)) if ids.len() == 2 && ids[0] != ids[1])
            && matches!(fields.get("restore_when_both_active"), Some(V::Bool(_)));
        if !good {
            return unsupported("an archived tie is invalid");
        }
    }
    for (message_id, message) in messages {
        let V::Map(fields) = message else {
            return unsupported("a retained message is not a mapping");
        };
        if !matches!(fields.get("id"), Some(V::Str(id)) if id == message_id) {
            return unsupported("a retained message is invalid");
        }
    }
    for beliefs in deferred.values() {
        if !matches!(beliefs, V::Map(_)) {
            return unsupported("deferred beliefs are not a mapping");
        }
    }
    if has_namespace(world, PRESENCE)? {
        let raw = namespace(world, PRESENCE)?.ok_or_else(|| Unsupported::new_err("no presence"))?;
        presence_is_plain(&raw)?;
        for agent_id in pending_ids {
            let record = person_in(&raw, agent_id)?;
            if !record.is_some_and(|person| &*person.status == "awaiting_admission") {
                return unsupported("a pending return disagrees with the presence");
            }
        }
    }
    Ok(())
}

#[pyfunction]
fn validate_population_lifecycle_state(world: &Bound<'_, PyAny>) -> PyResult<()> {
    planning(check_lifecycle(world, None))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(current_household_id, m)?)?;
    m.add_function(wrap_pyfunction!(apply_presence_action, m)?)?;
    m.add_function(wrap_pyfunction!(admit_pending_returns, m)?)?;
    m.add_function(wrap_pyfunction!(complete_presence_relocation, m)?)?;
    m.add_function(wrap_pyfunction!(zone_has_room, m)?)?;
    m.add_function(wrap_pyfunction!(validate_population_lifecycle_state, m)?)?;
    Ok(())
}
