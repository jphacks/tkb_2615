//! The entity and event rules of `WorldState.validate` at the end of a tick, in one call: the keys
//! and IDs, zone edges, people, facilities and their queues, active tasks, staff, the support
//! requests the memo has not settled, the inventory, the ID counters and the event queue.
//!
//! This holds no error text. It returns only if every rule passes, and anything else, including
//! what the rules would accept but is not plain (a bool for an int, a str subclass), says
//! `Unsupported`; `_validate_entity_rules` and `_validate_event_rules` in `core/world.py` then run
//! as if this weren't here, and pass or raise their own text. So an `Unsupported` never changes a
//! result, and the one thing that must hold is that a return means the Python rules pass
//! (`tests/integration/test_entity_rules_kernel.py`, `benchmarks/rule_shadow/shadow_metrics.py`).
//!
//! **It reads the objects, not the entity rows.** `agent.__dict__["stress"] = 1.5` reaches no row,
//! and the rules must see it: see docs/rust-port/lanes/t-tick-validation.md. Events are read from
//! the queue's rows, which are frozen.
//!
//! No Python code runs during a call: attributes of the dataclasses are instance attributes, and
//! reads of dicts and lists are the C code of `core/tracked.rs`'s types.

use std::cell::RefCell;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyList, PyString, PyTuple};
use rustc_hash::{FxHashMap, FxHashSet};

use crate::core::row::{attribute, exact_dict, exact_int, exact_list, exact_str, unsupported};
use crate::core::tracked::as_readable_list;
use crate::core::value::V;
use crate::core::world_core::{raw_dict_get, NamespaceMirror};
use crate::state::entities::{Activity, RequestStatus};
use crate::state::events::{EventWriter, PendingEvent, PendingSource};
use crate::state::resource_ledger::{held_mirror, LedgerRow};
use crate::validation::{is_count, is_unit};

const WATER_KIND: &str = "water_station";
const MEAL_KIND: &str = "meal_distribution";

/// Keeps the str objects read, so the `&str` of each stays valid for the whole call.
struct Held<'py> {
    objects: RefCell<Vec<Bound<'py, PyAny>>>,
}

impl<'py> Held<'py> {
    fn new() -> Self {
        Self { objects: RefCell::new(Vec::with_capacity(1024)) }
    }

    fn text(&self, value: Bound<'py, PyAny>) -> PyResult<&str> {
        let text: *const str = exact_str(&value)?;
        self.objects.borrow_mut().push(value);
        // SAFETY: the UTF-8 of a str object lives as long as the object and never changes, and
        // `objects` holds the object until self is dropped; moving a Bound does not move the str.
        Ok(unsafe { &*text })
    }

    fn optional(&self, value: Bound<'py, PyAny>) -> PyResult<Option<&str>> {
        if value.is_none() {
            return Ok(None);
        }
        self.text(value).map(Some)
    }

    /// A list of a facility or a staff member. Lists are usually empty.
    fn ids(&self, value: Bound<'py, PyAny>) -> PyResult<Vec<&str>> {
        let Some(list) = as_readable_list(&value) else {
            return unsupported("an ID list is another kind of list");
        };
        let mut ids = Vec::with_capacity(list.len());
        for item in list.iter() {
            ids.push(self.text(item)?);
        }
        Ok(ids)
    }
}

fn no_repeats(ids: &[&str]) -> bool {
    match ids.len() {
        0 | 1 => true,
        // These lists hold a few IDs; a queue may hold a hundred.
        count if count <= 16 => ids.iter().enumerate().all(|(place, id)| !ids[..place].contains(id)),
        _ => {
            let distinct: FxHashSet<&str> = ids.iter().copied().collect();
            distinct.len() == ids.len()
        }
    }
}

struct Person<'a> {
    id: &'a str,
    activity: Activity,
    zone: &'a str,
    target: Option<&'a str>,
    request: Option<&'a str>,
}

struct Place<'a> {
    id: &'a str,
    kind: &'a str,
    zone: &'a str,
    is_toilet: bool,
    is_resource: bool,
    queue: Vec<&'a str>,
    active: Vec<&'a str>,
    resource_queue: Vec<&'a str>,
    resource_active: Vec<&'a str>,
}

struct Work<'a> {
    id: &'a str,
    kind: &'a str,
    agent: Option<&'a str>,
    resource: Option<&'a str>,
    start: i64,
    due: i64,
    staff: Option<&'a str>,
}

struct Worker<'a> {
    active_task: Option<&'a str>,
    work_queue: Vec<&'a str>,
    busy_until: i64,
    zone: &'a str,
    skills: Bound<'a, PyTuple>,
}

/// How many events of a kind name a task, and the first.
#[derive(Clone, Copy)]
struct Named {
    count: u32,
    first: usize,
}

fn add_named<'a>(map: &mut FxHashMap<&'a str, Named>, key: &'a str, index: usize) {
    map.entry(key).and_modify(|named| named.count += 1).or_insert(Named { count: 1, first: index });
}

fn one_named(map: &FxHashMap<&str, Named>, key: &str) -> Option<usize> {
    map.get(key).filter(|named| named.count == 1).map(|named| named.first)
}

/// `payload.get(key)`, where a key that is absent reads as None.
#[derive(Clone, Copy)]
enum Field<'a> {
    Null,
    Text(&'a str),
    Int(i64),
    Other,
}

impl<'f> Field<'f> {
    /// `payload.get(key) == text`: a value of another type differs, as it does in Python.
    fn is(self, text: &str) -> bool {
        matches!(self, Field::Text(value) if value == text)
    }

    /// `payload.get(key) == expected`, where expected may be None.
    fn same(self, expected: Option<&str>) -> bool {
        match (self, expected) {
            (Field::Null, None) => true,
            (Field::Text(value), Some(text)) => value == text,
            _ => false,
        }
    }

    fn text(self) -> Option<&'f str> {
        match self {
            Field::Text(value) => Some(value),
            _ => None,
        }
    }
}

/// `event.payload.get(key)`, from the row or, for an event Python scheduled, from the object.
fn field<'a, 'py>(event: &'a PendingEvent<'py>, held: &'a Held<'py>, key: &Bound<'py, PyString>) -> PyResult<Field<'a>> {
    match &event.source {
        PendingSource::Row(row) => {
            let V::Map(fields) = &row.payload else {
                return unsupported("a payload is not a mapping");
            };
            Ok(match fields.get(key.to_str()?) {
                None | Some(V::Null) => Field::Null,
                Some(V::Str(text)) => Field::Text(text),
                Some(V::Int(number)) => Field::Int(*number),
                Some(_) => Field::Other,
            })
        }
        PendingSource::Object(object) => {
            let py = object.py();
            let value = object.getattr(intern!(py, "payload"))?.call_method1(intern!(py, "get"), (key,))?;
            if value.is_none() {
                return Ok(Field::Null);
            }
            if value.is_exact_instance_of::<PyString>() {
                return held.text(value).map(Field::Text);
            }
            if value.is_exact_instance_of::<PyInt>() {
                return Ok(value.extract().map_or(Field::Other, Field::Int));
            }
            Ok(Field::Other)
        }
    }
}

/// `key in event.payload`.
fn has_field<'py>(event: &PendingEvent<'py>, key: &Bound<'py, PyString>) -> PyResult<bool> {
    match &event.source {
        PendingSource::Row(row) => Ok(matches!(&row.payload, V::Map(fields) if fields.contains_key(key.to_str()?))),
        PendingSource::Object(object) => object.getattr(intern!(object.py(), "payload"))?.contains(key),
    }
}

/// `event.event_type`.
fn event_type<'a, 'py>(event: &'a PendingEvent<'py>, held: &'a Held<'py>) -> PyResult<&'a str> {
    match &event.source {
        PendingSource::Row(row) => Ok(&row.event_type),
        PendingSource::Object(object) => held.text(object.getattr(intern!(object.py(), "event_type"))?),
    }
}

fn map_of<'py>(world: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyDict>> {
    let map = attribute(world, name)?;
    Ok(exact_dict(&map)?.clone())
}

macro_rules! f {
    ($event:expr, $held:expr, $py:expr, $key:literal) => {
        field($event, $held, intern!($py, $key))?
    };
}

/// The entity and event rules of `validate`, for a world v6 at the end of a tick. `requests` is
/// `TickValidationMemo.requests_to_check`. The result lists `(zone_id, occupancy)` of the zones
/// whose capacity is the Python rule's: their temporary spaces and rooms count too.
#[pyfunction]
fn entity_rules<'py>(
    world: &Bound<'py, PyAny>,
    requests: &Bound<'py, PyList>,
    resource_config: &Bound<'py, PyAny>,
    initial_needs_type: &Bound<'py, PyAny>,
    needs_type: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyList>> {
    let py = world.py();
    let held = Held::new();
    if exact_int(&attribute(world, intern!(py, "schema_version"))?)? != 6 {
        return unsupported("not a WorldState v6");
    }
    if resource_config.is_none() {
        return unsupported("no resource service config");
    }
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let duration = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
    if duration <= 0 || !(0..=duration).contains(&minute) || exact_int(&attribute(world, intern!(py, "seed"))?)? < 0 {
        return unsupported("the clock or seed is the Python rule's");
    }
    let agents = map_of(world, intern!(py, "agents"))?;
    let zones = map_of(world, intern!(py, "zones"))?;
    let edges = map_of(world, intern!(py, "zone_edges"))?;
    let facilities = map_of(world, intern!(py, "facilities"))?;
    let staff = map_of(world, intern!(py, "staff"))?;
    let tasks = map_of(world, intern!(py, "active_tasks"))?;
    let support_requests = map_of(world, intern!(py, "support_requests"))?;
    let id_name = intern!(py, "id");

    // A ledger the Python rules would stumble over is theirs, even with no reservation to read.
    let ledger = held_mirror(world)?;
    NamespaceMirror::with_row::<LedgerRow, _>(&ledger, |_| Ok(()))?;

    // ---- Zones and edges. Each map's key is the item's stable ID.
    let mut occupancy: FxHashMap<&str, i64> = FxHashMap::default();
    let mut zone_order: Vec<(&str, Bound<'py, PyAny>)> = Vec::with_capacity(zones.len());
    for (key, zone) in zones.iter() {
        let id = held.text(key)?;
        if id.is_empty() || exact_str(&attribute(&zone, id_name)?)? != id {
            return unsupported("a zone key is not its ID");
        }
        occupancy.insert(id, 0);
        zone_order.push((id, zone));
    }
    for (key, edge) in edges.iter() {
        let id = held.text(key)?;
        if id.is_empty() || exact_str(&attribute(&edge, id_name)?)? != id {
            return unsupported("an edge key is not its ID");
        }
        let from = held.text(attribute(&edge, intern!(py, "from_zone_id"))?)?;
        let to = held.text(attribute(&edge, intern!(py, "to_zone_id"))?)?;
        if !occupancy.contains_key(from)
            || !occupancy.contains_key(to)
            || from == to
            || exact_int(&attribute(&edge, intern!(py, "travel_minutes"))?)? <= 0
        {
            return unsupported("an edge is the Python rule's");
        }
    }
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let configs = exact_dict(&configs)?;
    if let Some(route_config) = configs.get_item(intern!(py, "layout_routing"))? {
        if !route_config.is_none() {
            route_capacities_pass(&route_config, &edges)?;
        }
    }

    // ---- People: world_agents_valid, the keys, and what the rules below read.
    let (moving, queued, in_service, seeking) = (
        Activity::Moving.member(py)?,
        Activity::Queued.member(py)?,
        Activity::InService.member(py)?,
        Activity::SeekingHelp.member(py)?,
    );
    let others = [
        Activity::Idle.member(py)?,
        Activity::Sleeping.member(py)?,
        Activity::Resting.member(py)?,
    ];
    let mut people: Vec<Person<'_>> = Vec::with_capacity(agents.len());
    let mut person_at: FxHashMap<&str, usize> = FxHashMap::default();
    person_at.reserve(agents.len());
    for (key, agent) in agents.iter() {
        let key = held.text(key)?;
        let profile = attribute(&agent, intern!(py, "profile"))?;
        let id = attribute(&profile, id_name)?;
        if key.is_empty() || exact_str(&id)? != key {
            return unsupported("an agent key is not its ID");
        }
        if !attribute(&profile, intern!(py, "initial_needs"))?.get_type().is(initial_needs_type) {
            return unsupported("an agent's initial needs are the Python rule's");
        }
        let current = attribute(&agent, intern!(py, "needs"))?;
        if !current.get_type().is(needs_type) {
            return unsupported("an agent's needs are the Python rule's");
        }
        for level in [
            intern!(py, "toilet_urgency"),
            intern!(py, "hunger"),
            intern!(py, "fatigue"),
            intern!(py, "hydration_need"),
        ] {
            if !is_unit(attribute(&current, level)?.as_ptr()) {
                return unsupported("a need is the Python rule's");
            }
        }
        if !is_unit(attribute(&agent, intern!(py, "stress"))?.as_ptr())
            || !is_unit(attribute(&agent, intern!(py, "trust"))?.as_ptr())
            || !is_count(attribute(&agent, intern!(py, "next_decision_at"))?.as_ptr())
            || !is_count(attribute(&agent, intern!(py, "support_cooldown_until"))?.as_ptr())
        {
            return unsupported("an agent's value is the Python rule's");
        }
        let zone = held.text(attribute(&agent, intern!(py, "zone_id"))?)?;
        let Some(count) = occupancy.get_mut(zone) else {
            return unsupported("an agent's zone is unknown");
        };
        *count += 1;
        let activity = attribute(&agent, intern!(py, "activity"))?;
        let activity = if activity.is(&moving) {
            Activity::Moving
        } else if activity.is(&queued) {
            Activity::Queued
        } else if activity.is(&in_service) {
            Activity::InService
        } else if activity.is(&seeking) {
            Activity::SeekingHelp
        } else if others.iter().any(|member| activity.is(member)) {
            Activity::Idle
        } else {
            return unsupported("an activity is the Python rule's");
        };
        person_at.insert(key, people.len());
        people.push(Person {
            id: key,
            activity,
            zone,
            target: held.optional(attribute(&agent, intern!(py, "target_id"))?)?,
            request: held.optional(attribute(&agent, intern!(py, "support_request_id"))?)?,
        });
    }

    // ---- Facilities and who is in them.
    let mut places: Vec<Place<'_>> = Vec::with_capacity(facilities.len());
    let mut place_at: FxHashMap<&str, usize> = FxHashMap::default();
    for (key, facility) in facilities.iter() {
        let id = held.text(key)?;
        if id.is_empty() || exact_str(&attribute(&facility, id_name)?)? != id {
            return unsupported("a facility key is not its ID");
        }
        let kind = held.text(attribute(&facility, intern!(py, "kind"))?)?;
        if !kind.is_ascii() {
            return unsupported("a facility kind is not ASCII");
        }
        let zone = held.text(attribute(&facility, intern!(py, "zone_id"))?)?;
        let capacity = exact_int(&attribute(&facility, intern!(py, "capacity"))?)?;
        let working = exact_int(&attribute(&facility, intern!(py, "working_units"))?)?;
        if !occupancy.contains_key(zone) || capacity < 0 || !(0..=capacity).contains(&working) {
            return unsupported("a facility's zone or units are the Python rule's");
        }
        let place = Place {
            id,
            kind,
            zone,
            is_toilet: kind.as_bytes().windows(6).any(|part| part.eq_ignore_ascii_case(b"toilet")),
            is_resource: kind == WATER_KIND || kind == MEAL_KIND,
            queue: held.ids(attribute(&facility, intern!(py, "queue_agent_ids"))?)?,
            active: held.ids(attribute(&facility, intern!(py, "active_agent_ids"))?)?,
            resource_queue: held.ids(attribute(&facility, intern!(py, "resource_queue_agent_ids"))?)?,
            resource_active: held.ids(attribute(&facility, intern!(py, "resource_active_agent_ids"))?)?,
        };
        if place.active.len() as i64 > capacity || place.resource_active.len() as i64 > capacity {
            return unsupported("a facility is over capacity");
        }
        if !no_repeats(&place.queue)
            || !no_repeats(&place.active)
            || !no_repeats(&place.resource_queue)
            || !no_repeats(&place.resource_active)
        {
            return unsupported("a facility has duplicate entries");
        }
        if place.is_resource && (!place.queue.is_empty() || !place.active.is_empty()) {
            return unsupported("a resource facility uses the legacy queues");
        }
        if !place.is_resource && (!place.resource_queue.is_empty() || !place.resource_active.is_empty()) {
            return unsupported("another facility holds resource slots");
        }
        place_at.insert(id, places.len());
        places.push(place);
    }
    let mut waiting: FxHashSet<&str> = FxHashSet::default();
    let mut served: FxHashSet<&str> = FxHashSet::default();
    let person = |id: &str| person_at.get(id).map(|index| &people[*index]);
    for place in &places {
        for id in &place.queue {
            if person(id).is_none_or(|who| who.activity != Activity::Queued) || served.contains(id) || !waiting.insert(id) {
                return unsupported("a facility queue is the Python rule's");
            }
        }
        for id in &place.active {
            if person(id).is_none_or(|who| who.activity != Activity::InService) || waiting.contains(id) || !served.insert(id) {
                return unsupported("a facility service is the Python rule's");
            }
        }
    }
    for place in &places {
        let holds_here = |who: &Person<'_>| who.target == Some(place.id) && who.zone == place.zone;
        for id in &place.resource_queue {
            if person(id).is_none_or(|who| who.activity != Activity::Queued || !holds_here(who))
                || served.contains(id)
                || !waiting.insert(id)
            {
                return unsupported("a resource queue is the Python rule's");
            }
        }
        for id in &place.resource_active {
            if person(id).is_none_or(|who| who.activity != Activity::InService || !holds_here(who))
                || waiting.contains(id)
                || !served.insert(id)
            {
                return unsupported("a resource service is the Python rule's");
            }
        }
    }
    for who in &people {
        if (who.activity == Activity::Queued && !waiting.contains(who.id))
            || (who.activity == Activity::InService && !served.contains(who.id))
        {
            return unsupported("someone is missing from the facilities");
        }
    }

    // ---- The pending events, from the rows of the queue.
    let writer = EventWriter::of(world)?;
    if writer.queue().call_method0(intern!(py, "validate"))?.extract::<u8>()? != 0 {
        return unsupported("the event queue repeats an identity");
    }
    let Some(events) = writer.queue().borrow().pending_events(py)? else {
        return unsupported("a pending event is not a plain ScheduledEvent");
    };
    let mut toilet_events: FxHashMap<&str, Named> = FxHashMap::default();
    let mut resource_events: FxHashMap<&str, Named> = FxHashMap::default();
    let mut travel_events: FxHashMap<&str, Named> = FxHashMap::default();
    let mut staff_events: FxHashMap<&str, Named> = FxHashMap::default();
    let mut timeouts: FxHashMap<&str, Named> = FxHashMap::default();
    for (index, event) in events.iter().enumerate() {
        let kind = event_type(event, &held)?;
        let target = match kind {
            "toilet_service_complete" => &mut toilet_events,
            "resource_service_complete" => &mut resource_events,
            "agent_travel_complete" => &mut travel_events,
            "staff_support_complete" => &mut staff_events,
            "support_request_timeout" => {
                // Another type of request ID is no ID of a request.
                if let Some(request_id) = f!(event, &held, py, "request_id").text() {
                    add_named(&mut timeouts, request_id, index);
                }
                continue;
            }
            _ => continue,
        };
        // A task ID that is no str names no task here, as it fills no list in Python.
        if let Some(task_id) = f!(event, &held, py, "task_id").text() {
            add_named(target, task_id, index);
        } else if kind == "resource_service_complete" {
            return unsupported("a resource completion lacks a task ID");
        }
    }

    // ---- Tasks.
    let mut works: Vec<Work<'_>> = Vec::with_capacity(tasks.len());
    let mut work_at: FxHashMap<&str, usize> = FxHashMap::default();
    let mut travel_by_agent: FxHashMap<&str, usize> = FxHashMap::default();
    let mut toilet_queue: FxHashSet<(&str, &str)> = FxHashSet::default();
    let mut toilet_service: FxHashMap<(&str, &str), usize> = FxHashMap::default();
    let mut resource_queue: FxHashSet<(&str, &str)> = FxHashSet::default();
    let mut resource_service: FxHashMap<(&str, &str), usize> = FxHashMap::default();
    for (key, task) in tasks.iter() {
        let id = held.text(key)?;
        if id.is_empty() || exact_str(&attribute(&task, id_name)?)? != id {
            return unsupported("a task key is not its ID");
        }
        let work = Work {
            id,
            kind: held.text(attribute(&task, intern!(py, "kind"))?)?,
            agent: held.optional(attribute(&task, intern!(py, "agent_id"))?)?,
            resource: held.optional(attribute(&task, intern!(py, "resource_id"))?)?,
            start: exact_int(&attribute(&task, intern!(py, "start_minute"))?)?,
            due: exact_int(&attribute(&task, intern!(py, "due_minute"))?)?,
            staff: held.optional(attribute(&task, intern!(py, "staff_id"))?)?,
        };
        let index = works.len();
        match work.kind {
            "travel_to_toilet" | "travel_to_resource" | "travel_return" | "staff_support_travel" => {
                let (Some(agent), None) = (work.agent, work.resource) else {
                    return unsupported("a travel task's references are the Python rule's");
                };
                if work.start > minute || work.due <= work.start || travel_by_agent.insert(agent, index).is_some() {
                    return unsupported("a travel task is the Python rule's");
                }
            }
            "resource_service_queue" | "resource_service_water" | "resource_service_food" => {
                let (Some(agent), Some(resource)) = (work.agent, work.resource) else {
                    return unsupported("a resource task's references are the Python rule's");
                };
                if !place_at.get(resource).is_some_and(|at| places[*at].is_resource) || work.start > minute {
                    return unsupported("a resource task is the Python rule's");
                }
                if work.kind == "resource_service_queue" {
                    if work.due != work.start || work.staff.is_some() || !resource_queue.insert((resource, agent)) {
                        return unsupported("a resource queue task is the Python rule's");
                    }
                } else if work.due <= work.start
                    || work.staff.is_none()
                    || resource_service.insert((resource, agent), index).is_some()
                {
                    return unsupported("a resource service task is the Python rule's");
                }
            }
            "toilet_queue" | "toilet_service" => {
                let (Some(agent), Some(resource)) = (work.agent, work.resource) else {
                    return unsupported("a toilet task's references are the Python rule's");
                };
                if !place_at.get(resource).is_some_and(|at| places[*at].is_toilet) || work.start > minute {
                    return unsupported("a toilet task is the Python rule's");
                }
                if work.kind == "toilet_queue" {
                    if work.due != work.start || !toilet_queue.insert((resource, agent)) {
                        return unsupported("a toilet queue task is the Python rule's");
                    }
                } else if work.due <= work.start || toilet_service.insert((resource, agent), index).is_some() {
                    return unsupported("a toilet service task is the Python rule's");
                }
            }
            _ => {}
        }
        work_at.insert(id, index);
        works.push(work);
    }

    // ---- Staff, read before the tasks are matched with them.
    let mut workers: FxHashMap<&str, Worker<'_>> = FxHashMap::default();
    let mut assigned: FxHashSet<&str> = FxHashSet::default();
    for (key, member) in staff.iter() {
        let id = held.text(key)?;
        if id.is_empty() || exact_str(&attribute(&member, id_name)?)? != id {
            return unsupported("a staff key is not its ID");
        }
        let zone = held.text(attribute(&member, intern!(py, "zone_id"))?)?;
        let work_queue = held.ids(attribute(&member, intern!(py, "work_queue"))?)?;
        let busy_until = exact_int(&attribute(&member, intern!(py, "busy_until"))?)?;
        let active_task = held.optional(attribute(&member, intern!(py, "active_task_id"))?)?;
        if !occupancy.contains_key(zone) || !no_repeats(&work_queue) || busy_until < 0 {
            return unsupported("a staff member is the Python rule's");
        }
        for task_id in &work_queue {
            if !work_at.contains_key(task_id) || !assigned.insert(task_id) {
                return unsupported("a staff queue is the Python rule's");
            }
        }
        match active_task {
            None if !work_queue.is_empty() || busy_until != 0 => {
                return unsupported("an idle staff member keeps work");
            }
            Some(task) if work_queue.as_slice() != [task] => {
                return unsupported("a staff member's work is the Python rule's");
            }
            _ => {}
        }
        let skills = attribute(&member, intern!(py, "skills"))?;
        let Ok(skills) = skills.downcast_into_exact::<PyTuple>() else {
            return unsupported("a staff member's skills are not a tuple");
        };
        workers.insert(id, Worker { active_task, work_queue, busy_until, zone, skills });
    }

    // ---- Facilities and tasks each name the other.
    let queue_person_targets = |id: &str, place: &Place<'_>| person(id).is_some_and(|who| who.target == Some(place.id));
    for place in &places {
        if place.is_toilet {
            for id in &place.queue {
                if !toilet_queue.contains(&(place.id, *id)) || !queue_person_targets(id, place) {
                    return unsupported("someone in a toilet queue has no task");
                }
            }
            for id in &place.active {
                if !toilet_service.contains_key(&(place.id, *id)) || !queue_person_targets(id, place) {
                    return unsupported("someone in a toilet has no task");
                }
            }
        }
        if place.is_resource {
            for id in &place.resource_queue {
                if !resource_queue.contains(&(place.id, *id)) || !queue_person_targets(id, place) {
                    return unsupported("someone in a resource queue has no task");
                }
            }
            for id in &place.resource_active {
                if !resource_service.contains_key(&(place.id, *id)) || !queue_person_targets(id, place) {
                    return unsupported("someone at a resource has no task");
                }
            }
        }
    }
    let place_of = |id: &str| place_at.get(id).map(|index| &places[*index]);
    for (facility, agent) in &toilet_queue {
        if !place_of(facility).is_some_and(|place| place.queue.contains(agent))
            || person(agent).is_none_or(|who| who.activity != Activity::Queued)
        {
            return unsupported("a toilet queue task has no entry");
        }
    }
    for ((facility, agent), index) in &toilet_service {
        let work = &works[*index];
        if !place_of(facility).is_some_and(|place| place.active.contains(agent))
            || person(agent).is_none_or(|who| who.activity != Activity::InService)
        {
            return unsupported("a toilet service task has no slot");
        }
        let Some(event) = one_named(&toilet_events, work.id).map(|at| &events[at]) else {
            return unsupported("a toilet service task has no completion");
        };
        let staff_matches = !has_field(event, intern!(py, "staff_id"))? || f!(event, &held, py, "staff_id").same(work.staff);
        if !event.is_completion()?
            || event.due_minute != work.due
            || !f!(event, &held, py, "facility_id").is(facility)
            || !f!(event, &held, py, "agent_id").is(agent)
            || !staff_matches
        {
            return unsupported("a toilet service task's completion is the Python rule's");
        }
    }
    for (facility, agent) in &resource_queue {
        if !place_of(facility).is_some_and(|place| place.resource_queue.contains(agent))
            || person(agent).is_none_or(|who| who.activity != Activity::Queued)
        {
            return unsupported("a resource queue task has no entry");
        }
    }
    let mut active_reservations: FxHashSet<&str> = FxHashSet::default();
    if !resource_service.is_empty() {
        let water_quantity = exact_int(&attribute(resource_config, intern!(py, "water_ml_per_service"))?)?;
        let meal_quantity = exact_int(&attribute(resource_config, intern!(py, "food_meal_units_per_service"))?)?;
        let water_skill = attribute(resource_config, intern!(py, "water_staff_skill"))?;
        let meal_skill = attribute(resource_config, intern!(py, "meal_staff_skill"))?;
        let water_skill = held.text(water_skill)?;
        let meal_skill = held.text(meal_skill)?;
        for ((facility, agent), index) in &resource_service {
            let work = &works[*index];
            let Some(place) = place_of(facility) else {
                return unsupported("a resource service task's facility is unknown");
            };
            if !place.resource_active.contains(agent) || person(agent).is_none_or(|who| who.activity != Activity::InService) {
                return unsupported("a resource service task has no slot");
            }
            let is_water = place.kind == WATER_KIND;
            let resource_id = if is_water { "water_ml" } else { "food_meal_units" };
            let quantity = if is_water { water_quantity } else { meal_quantity };
            let skill = if is_water { water_skill } else { meal_skill };
            let Some(event) = one_named(&resource_events, work.id).map(|at| &events[at]) else {
                return unsupported("a resource service task has no completion");
            };
            if event.due_minute != work.due
                || !f!(event, &held, py, "facility_id").is(facility)
                || !f!(event, &held, py, "agent_id").is(agent)
                || !f!(event, &held, py, "resource_id").is(resource_id)
                || !f!(event, &held, py, "reservation_id").is(work.id)
                || !f!(event, &held, py, "staff_id").same(work.staff)
                || !event.is_completion()?
                || !matches!(f!(event, &held, py, "quantity"), Field::Int(value) if value == quantity)
            {
                return unsupported("a resource service task's completion is the Python rule's");
            }
            let reserved = NamespaceMirror::with_row::<LedgerRow, _>(&ledger, |row| {
                let account = row.accounts.of(usize::from(!is_water));
                Ok(account.reservations.get(work.id).is_some_and(|reservation| {
                    &*reservation.agent_id == *agent && &*reservation.resource_id == resource_id && reservation.quantity == quantity
                }))
            })?;
            if !reserved {
                return unsupported("a resource service task has no reservation");
            }
            active_reservations.insert(work.id);
            let Some(worker) = work.staff.and_then(|id| workers.get(id)) else {
                return unsupported("a resource service task's staff is unknown");
            };
            let has_skill = worker.skills.iter().any(|item| exact_str(&item).is_ok_and(|text| text == skill));
            if worker.active_task != Some(work.id)
                || worker.work_queue.as_slice() != [work.id]
                || worker.busy_until != work.due
                || worker.zone != place.zone
                || !has_skill
            {
                return unsupported("a resource service task's staff is the Python rule's");
            }
        }
    }
    let table = attribute(world, intern!(py, "model_state"))?;
    let table = exact_dict(&table)?;
    let Some(service) = raw_dict_get(table, intern!(py, "resource_service_v1").as_any())? else {
        return unsupported("no resource service state");
    };
    let service = exact_dict(&service)?;
    let Some(cancelled) = service.get_item(intern!(py, "cancelled_task_ids"))? else {
        return unsupported("the resource service state lacks a field");
    };
    let cancelled = held.ids(cancelled)?;
    for (task_id, _) in resource_events.iter() {
        if !work_at.contains_key(task_id) && !cancelled.contains(task_id) {
            return unsupported("a resource completion names an inactive task");
        }
    }
    let Some(listed) = service.get_item(intern!(py, "active_reservation_ids"))? else {
        return unsupported("the resource service state lacks a field");
    };
    let listed = held.ids(listed)?;
    let listed_set: FxHashSet<&str> = listed.iter().copied().collect();
    if listed_set != active_reservations {
        return unsupported("the active reservations differ from the active tasks");
    }
    // Each of these was found in the ledger above, in its task's account.

    // ---- Travel.
    for (agent_id, index) in &travel_by_agent {
        let work = &works[*index];
        let Some(who) = person(agent_id).filter(|who| who.activity == Activity::Moving) else {
            return unsupported("a travel task needs a moving agent");
        };
        let Some(event) = one_named(&travel_events, work.id).map(|at| &events[at]) else {
            return unsupported("a travel task has no completion");
        };
        if event.due_minute != work.due {
            return unsupported("a travel task's completion is late");
        }
        let purpose = f!(event, &held, py, "purpose").text();
        let Some(destination) = f!(event, &held, py, "destination_zone_id").text() else {
            return unsupported("a travel event's destination is not a str");
        };
        if !occupancy.contains_key(destination) {
            return unsupported("a travel event's destination is unknown");
        }
        let points_right = if matches!(purpose, Some("toilet_request" | "resource_request")) {
            f!(event, &held, py, "facility_id").same(who.target)
        } else {
            who.target == Some(destination)
        };
        if !points_right {
            return unsupported("a travel event's target differs");
        }
        if work.kind == "staff_support_travel" {
            let request_known = f!(event, &held, py, "request_id").text().map(|id| support_requests.contains(id)).transpose()?;
            let staff_known = f!(event, &held, py, "staff_id").text().is_some_and(|id| workers.contains_key(id));
            if purpose != Some("staff_support_travel") || request_known != Some(true) || !staff_known {
                return unsupported("a staff support trip is the Python rule's");
            }
        }
    }
    for who in &people {
        if who.activity == Activity::Moving && !travel_by_agent.contains_key(who.id) {
            return unsupported("someone moving has no travel task");
        }
    }

    // ---- The support requests the memo says may have changed.
    let (pending, assigned_status, resolved, unresolved) = (
        RequestStatus::Pending.member(py)?,
        RequestStatus::Assigned.member(py)?,
        RequestStatus::Resolved.member(py)?,
        RequestStatus::Unresolved.member(py)?,
    );
    // Open requests by agent: how many, and the ID of the first.
    let mut open_by_agent: FxHashMap<&str, (u32, &str)> = FxHashMap::default();
    for request in requests.iter() {
        let id_object = attribute(&request, id_name)?;
        if !support_requests.contains(&id_object)? {
            return unsupported("a request's ID is the Python rule's");
        }
        let id = held.text(id_object)?;
        if id.is_empty() {
            return unsupported("a request's ID is the Python rule's");
        }
        let agent = held.text(attribute(&request, intern!(py, "agent_id"))?)?;
        let requested = exact_int(&attribute(&request, intern!(py, "requested_minute"))?)?;
        let timeout = exact_int(&attribute(&request, intern!(py, "timeout_minute"))?)?;
        let cooldown = exact_int(&attribute(&request, intern!(py, "cooldown_until"))?)?;
        if !person_at.contains_key(agent) || !(0..=minute).contains(&requested) || timeout < requested || cooldown < 0 {
            return unsupported("a request's agent or timing is the Python rule's");
        }
        let attempt = attribute(&request, intern!(py, "last_assignment_attempt_minute"))?;
        let attempted = !attempt.is_none();
        if attempted && !(requested..=minute).contains(&exact_int(&attempt)?) {
            return unsupported("a request's attempt is the Python rule's");
        }
        let reason = attribute(&request, intern!(py, "last_assignment_wait_reason_code"))?;
        if !reason.is_none()
            && (!attempted
                || !matches!(
                    exact_str(&reason)?,
                    "no_available_qualified_staff"
                        | "route_unavailable_at_assignment"
                        | "insufficient_run_time"
                        | "resident_travel_schedule_rejected"
                ))
        {
            return unsupported("a request's wait reason is the Python rule's");
        }
        let status = attribute(&request, intern!(py, "status"))?;
        let staff_id = held.optional(attribute(&request, intern!(py, "assigned_staff_id"))?)?;
        let task_id = held.optional(attribute(&request, intern!(py, "task_id"))?)?;
        if status.is(&assigned_status) {
            let (Some(worker_id), Some(task_id)) = (staff_id, task_id) else {
                return unsupported("an assigned request names no staff or task");
            };
            let (Some(worker), Some(work)) = (workers.get(worker_id), work_at.get(task_id).map(|at| &works[*at])) else {
                return unsupported("an assigned request's staff or task is unknown");
            };
            if work.kind != "staff_support"
                || work.staff != Some(worker_id)
                || work.agent != Some(agent)
                || worker.active_task != Some(work.id)
                || work.due != worker.busy_until
            {
                return unsupported("an assigned request lacks one matching staff task");
            }
            let Some(who) = person(agent) else {
                return unsupported("an assigned request's agent is unknown");
            };
            let travel = travel_by_agent.get(agent).map(|at| &works[*at]);
            if who.activity == Activity::Moving {
                let event = travel.and_then(|trip| one_named(&travel_events, trip.id)).map(|at| &events[at]);
                let right = match (travel, event) {
                    (Some(trip), Some(event)) => {
                        trip.kind == "staff_support_travel"
                            && f!(event, &held, py, "request_id").is(id)
                            && f!(event, &held, py, "staff_id").is(worker_id)
                            && f!(event, &held, py, "destination_zone_id").is(worker.zone)
                    }
                    _ => false,
                };
                if !right {
                    return unsupported("an assigned request's travel is the Python rule's");
                }
            } else if who.activity != Activity::SeekingHelp || travel.is_some_and(|trip| trip.kind == "staff_support_travel") {
                return unsupported("an assigned request must seek help or travel to staff");
            }
            let Some(event) = one_named(&staff_events, task_id).map(|at| &events[at]) else {
                return unsupported("an assigned request needs one completion");
            };
            if event.due_minute != work.due
                || !f!(event, &held, py, "request_id").is(id)
                || !f!(event, &held, py, "agent_id").is(agent)
                || !f!(event, &held, py, "staff_id").is(worker_id)
            {
                return unsupported("an assigned request's completion is the Python rule's");
            }
            let entry = open_by_agent.entry(agent).or_insert((0, id));
            entry.0 += 1;
        } else if status.is(&pending) {
            if staff_id.is_some() || task_id.is_some() {
                return unsupported("a pending request keeps an assignment");
            }
            if timeout < duration {
                if !one_named(&timeouts, id).is_some_and(|at| events[at].due_minute == timeout) {
                    return unsupported("a pending request needs one timeout");
                }
            }
            let entry = open_by_agent.entry(agent).or_insert((0, id));
            entry.0 += 1;
        } else if !status.is(&resolved) && !status.is(&unresolved) {
            return unsupported("a request's status is the Python rule's");
        }
    }
    for who in &people {
        match open_by_agent.get(who.id) {
            Some((count, _)) if *count > 1 => return unsupported("someone has duplicate open requests"),
            Some((_, request)) => {
                if who.request != Some(*request) || !matches!(who.activity, Activity::SeekingHelp | Activity::Moving) {
                    return unsupported("someone's open request is the Python rule's");
                }
            }
            None => {
                if who.request.is_some() {
                    return unsupported("someone names a request that is not open");
                }
            }
        }
    }

    // ---- Every task once more.
    for work in &works {
        if work.agent.is_some_and(|agent| !person_at.contains_key(agent)) {
            return unsupported("a task's agent is unknown");
        }
        if work.kind != "staff_support" && work.resource.is_some_and(|resource| !place_at.contains_key(resource)) {
            return unsupported("a task's facility is unknown");
        }
        if work.kind == "staff_support" {
            let right = work.agent.is_some()
                && work.resource.is_none()
                && work.due > work.start
                && work
                    .staff
                    .and_then(|id| workers.get(id))
                    .is_some_and(|worker| worker.active_task == Some(work.id) && worker.busy_until == work.due)
                && one_named(&staff_events, work.id).is_some_and(|at| events[at].due_minute == work.due);
            if !right {
                return unsupported("a staff support task is inconsistent");
            }
        }
        if work.start < 0 || work.due < work.start {
            return unsupported("a task's timing is the Python rule's");
        }
    }

    // ---- The inventory, the counters and the event identities.
    let resources = attribute(world, intern!(py, "resources"))?;
    for value in exact_dict(&resources)?.values().iter() {
        if exact_int(&value)? < 0 {
            return unsupported("the inventory is the Python rule's");
        }
    }
    let counters = attribute(world, intern!(py, "id_counters"))?;
    let counters = exact_dict(&counters)?;
    for value in counters.values().iter() {
        if exact_int(&value)? < 0 {
            return unsupported("an ID counter is negative");
        }
    }
    let log = writer.log().borrow();
    let mut newest = log.max_realized_sequence();
    for event in &events {
        if log.has_event_id(&event.event_id) || log.has_sequence(event.sequence) || event.due_minute >= duration {
            return unsupported("a pending event is the Python rule's");
        }
        newest = newest.max(event.sequence);
    }
    let counter = counters.get_item(intern!(py, "event"))?.map_or(Ok(0), |value| exact_int(&value))?;
    if counter <= newest {
        return unsupported("the event counter is not past the sequences");
    }

    // ---- Zone capacity, last so a Python rule reads it after everything else passed.
    let spaces_are_empty = namespace_part_is_empty(table, intern!(py, "temporary_spaces_v1").as_any(), intern!(py, "spaces").as_any())?
        && namespace_part_is_empty(table, intern!(py, "geometry_overlay_v1").as_any(), intern!(py, "temporary_rooms").as_any())?;
    let left = PyList::empty(py);
    for (id, zone) in &zone_order {
        let capacity = attribute(zone, intern!(py, "capacity"))?;
        if capacity.is_none() {
            continue;
        }
        let nominal = exact_int(&capacity)?;
        let inside = occupancy[id];
        // With no space or room, effective_total_zone_capacity is the zone's own.
        if !spaces_are_empty || inside > nominal {
            left.append((*id, inside))?;
        }
    }
    Ok(left)
}

/// Whether `model_state.get(name, {}).get(part, {})` is an empty dict.
fn namespace_part_is_empty(table: &Bound<'_, PyDict>, name: &Bound<'_, PyAny>, part: &Bound<'_, PyAny>) -> PyResult<bool> {
    let Some(state) = raw_dict_get(table, name)? else {
        return Ok(true);
    };
    let Ok(state) = exact_dict(&state) else {
        return Ok(false);
    };
    let Some(items) = state.get_item(part)? else {
        return Ok(true);
    };
    Ok(exact_dict(&items).is_ok_and(|items| items.is_empty()))
}

/// The `layout_routing` config of `validate`.
fn route_capacities_pass(config: &Bound<'_, PyAny>, edges: &Bound<'_, PyDict>) -> PyResult<()> {
    let py = config.py();
    let config = exact_dict(config)?;
    let text_is = |name: &Bound<'_, PyString>, expected: &str| -> PyResult<bool> {
        Ok(config.get_item(name)?.is_some_and(|value| exact_str(&value).is_ok_and(|text| text == expected)))
    };
    let version_is_one = config
        .get_item(intern!(py, "schema_version"))?
        .is_some_and(|value| exact_int(&value).is_ok_and(|number| number == 1));
    let Some(capacities) = config.get_item(intern!(py, "max_agents_in_transit_by_edge"))? else {
        return unsupported("the route config lacks its capacities");
    };
    if config.len() != 4
        || !version_is_one
        || !text_is(intern!(py, "source_type"), "assumption")?
        || !text_is(intern!(py, "source_id"), "ASM-A05-ROUTE-EDIT-V1")?
    {
        return unsupported("the route config is the Python rule's");
    }
    for (edge, capacity) in exact_dict(&capacities)?.iter() {
        if !edges.contains(&edge)? || !(1..=100).contains(&exact_int(&capacity)?) {
            return unsupported("a route capacity is the Python rule's");
        }
    }
    Ok(())
}

const POPULATION_POLICY_VERSION: &str = "capacity-aware-arrivals-departures-v1";
const POPULATION_SOURCE_ID: &str = "ASM-POPULATION-DYNAMICS-V1";

fn required_list<'py>(state: &Bound<'py, PyDict>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    match state.get_item(name)? {
        Some(value) if as_readable_list(&value).is_some() => Ok(value),
        _ => unsupported("a list of the population state is the Python rule's"),
    }
}

/// `validate_population_dynamics_state` of a state with no dynamic context and no one pending, the
/// parts of it that read `PersonContext`, `PersonTraits` and the approach routes.
#[pyfunction]
fn population_dynamics(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    let Some(state) = raw_dict_get(exact_dict(&table)?, intern!(py, "population_dynamics_v1").as_any())? else {
        return Ok(true);
    };
    if state.is_none() {
        return Ok(true);
    }
    let state = exact_dict(&state)?;
    let text_is = |name: &Bound<'_, PyString>, expected: &str| -> PyResult<bool> {
        Ok(state.get_item(name)?.is_some_and(|value| exact_str(&value).is_ok_and(|text| text == expected)))
    };
    let version_is_one = state
        .get_item(intern!(py, "schema_version"))?
        .is_some_and(|value| exact_int(&value).is_ok_and(|number| number == 1));
    let context = state.get_item(intern!(py, "profile_context"))?;
    let no_context = match &context {
        None => true,
        Some(context) => exact_dict(context)?.is_empty(),
    };
    let initial = required_list(state, intern!(py, "initial_agent_ids"))?;
    let pending = required_list(state, intern!(py, "pending_arrivals"))?;
    required_list(state, intern!(py, "arrivals"))?;
    required_list(state, intern!(py, "departures"))?;
    let Some(traits) = state.get_item(intern!(py, "profile_traits"))? else {
        return unsupported("the population state lacks its traits");
    };
    let traits = exact_dict(&traits)?;
    if state.len() != if context.is_some() { 8 + 1 } else { 8 }
        || !version_is_one
        || !text_is(intern!(py, "policy_version"), POPULATION_POLICY_VERSION)?
        || !text_is(intern!(py, "source_id"), POPULATION_SOURCE_ID)?
        || !no_context
        || !exact_list(&pending)?.is_empty()
    {
        return unsupported("the population state is the Python rule's");
    }
    let initial = exact_list(&initial)?;
    let mut seen: FxHashSet<&str> = FxHashSet::default();
    seen.reserve(initial.len());
    let held = Held::new();
    for id in initial.iter() {
        if !seen.insert(held.text(id)?) {
            return unsupported("the initial agents repeat an ID");
        }
    }
    let agents = attribute(world, intern!(py, "agents"))?;
    for (id, _) in exact_dict(&agents)?.iter() {
        if !seen.contains(exact_str(&id)?) && !traits.contains(&id)? {
            return unsupported("an active agent has no profile");
        }
    }
    Ok(true)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(entity_rules, m)?)?;
    m.add_function(wrap_pyfunction!(population_dynamics, m)?)?;
    Ok(())
}
