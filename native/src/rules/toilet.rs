//! `ToiletServiceManager` of `domain/facilities.py` and `ToiletServiceCompletionHandler` of
//! `events/handlers.py`. Each kernel is one call that plans with nothing changed and then writes.
//! What goes to Python, before anything is written: the world's whole validation, and anything not
//! plainly what the Python rule accepts, which is also how a bad value gets its error text
//! (docs/rust-port/lanes/m6-handlers.md, m6-overlay.md).

use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyList, PySlice, PyString};
use sha2::{Digest, Sha256};

use crate::core::mathx::py_max;
use crate::core::rng::{Key, KeyedDraws};
use crate::core::row::{attribute, exact_dict, exact_int, exact_str, map_set, namespace_is_absent, unsupported};
use crate::core::value::V;
use crate::core::world_core::{mirror_of, raw_dict_get, NamespaceMirror};
use crate::rules::facility_queues::{
    choose_queue_id, has_independent, point_of, position_in, request_of_task, transition_row, with_queues, write_enqueue, write_finish, write_stall,
    write_start, write_transition,
};
use crate::rules::movement_geometry::Geo;
use crate::rules::queues::head_to_service;
use crate::rules::service::{active_task_class, add_one, metrics_of, service_capacity, set_agent};
use crate::rules::staff_dynamics::{is_available, is_available_now, staff_status};
use crate::state::body_state::{BodyRow, NAME as BODY_STATE};
use crate::state::entities::{entities_of, raw_setattr, Activity, EntityStore};
use crate::state::events::{EventKind, EventRef, EventRow, EventWriter};
use crate::state::facility_queues::QueueRequest;
use crate::state::metric_state::MetricRow;
use crate::Unsupported;

/// `ToiletServiceCompletionHandler`: what a completed visit takes off the hygiene need.
const HYGIENE_RELIEF: f64 = 0.2;
const COMPLETED_METRIC: &str = "toilet_service_completed_count";
const DURATION_SOURCE: &str = "ASM-TOILET-SERVICE-DURATION-V1";
static COUNTER_RNG: GILOnceCell<Py<PyAny>> = GILOnceCell::new();

/// `"toilet" in kind.casefold()`. Only ASCII: casefold changes others in ways for the Python rule.
fn is_toilet(kind: &str) -> PyResult<bool> {
    if !kind.is_ascii() {
        return unsupported("a facility kind that casefold may change");
    }
    Ok(kind.to_ascii_lowercase().contains("toilet"))
}

fn receipt_class(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    static CLASS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    Ok(CLASS
        .get_or_try_init(py, || -> PyResult<Py<PyAny>> { Ok(py.import("sheltersim.domain.facilities")?.getattr("ToiletRequestReceipt")?.unbind()) })?
        .bind(py))
}

/// `sha256(f"{facility}\0{agent}\0{minute}")`, its first 24 hex digits.
fn queue_task_id(facility_id: &str, agent_id: &str, minute: i64) -> Arc<str> {
    let mut hash = Sha256::new();
    hash.update(facility_id.as_bytes());
    hash.update([0]);
    hash.update(agent_id.as_bytes());
    hash.update([0]);
    hash.update(minute.to_string().as_bytes());
    let digest = hash.finalize();
    let mut text = String::with_capacity(12 + 24);
    text.push_str("toilet-queue-");
    for byte in &digest[..12] {
        text.push(char::from_digit(u32::from(byte >> 4), 16).expect("a digit"));
        text.push(char::from_digit(u32::from(byte & 15), 16).expect("a digit"));
    }
    Arc::from(text)
}

/// `request`: the task's ID and the place in the queue.
pub(crate) fn queue_up<'py>(
    world: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    queue_id: Option<&str>,
) -> PyResult<(Arc<str>, i64, i64)> {
    let py = world.py();
    let (agent_name, facility_name) = (exact_str(agent_id)?, exact_str(facility_id)?);
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let independent = has_independent(world, facility_name)?;
    if queue_id.is_some() && !independent {
        return unsupported("a queue ID of a facility without independent queues");
    }
    let chosen_queue = match (independent, queue_id) {
        (false, _) => None,
        (true, Some(queue)) if !queue.is_empty() => Some(queue.to_owned()),
        (true, _) => choose_queue_id(world, facility_id, agent_id)?,
    };
    if independent && chosen_queue.is_none() {
        return unsupported("no accessible independent queue");
    }
    let geo = Geo::of(world)?;
    let point = match &chosen_queue {
        Some(queue) => match point_of(&geo, queue) {
            Some(point) if &*point.facility_id == facility_name => Some(point),
            _ => return unsupported("no accessible independent queue"),
        },
        None => None,
    };
    let queue_tail = match &chosen_queue {
        Some(queue) => with_queues(world, |row| match row.queues.get(&**queue) {
            Some(queue) => Ok(queue.queued_request_ids.len() as i64 + 1),
            None => unsupported("no such queue"),
        })?,
        None => None,
    };
    let store = entities_of(world)?;
    let task_id = queue_task_id(facility_name, agent_name, minute);
    let position = EntityStore::with_rows(&store, |rows| {
        let (Some(agent), Some(facility)) = (rows.agents.get(agent_name), rows.facilities.get(facility_name)) else {
            return unsupported("no such agent or facility");
        };
        if !is_toilet(&facility.kind)? {
            return unsupported("not a toilet");
        }
        if !matches!(agent.activity, Activity::Idle | Activity::Resting | Activity::Sleeping) {
            return unsupported("the person cannot queue now");
        }
        for (_, other) in rows.facilities.iter() {
            if other.queue_agent_ids.iter().chain(&other.active_agent_ids).any(|id| &**id == agent_name) {
                return unsupported("the person has a toilet request");
            }
        }
        if rows.active_tasks.get(&*task_id).is_some() {
            return unsupported("the queue task's ID is taken");
        }
        Ok(queue_tail.unwrap_or(facility.queue_agent_ids.len() as i64 + 1))
    })?;
    let facilities = attribute(world, intern!(py, "facilities"))?;
    let Some(facility) = raw_dict_get(&facilities, facility_id)? else {
        return unsupported("a row has no object");
    };
    let queue = attribute(&facility, intern!(py, "queue_agent_ids"))?;
    let tasks = attribute(world, intern!(py, "active_tasks"))?;
    let writer = EventWriter::of(world)?;
    let transition = match point {
        Some(point) => {
            let known = with_queues(world, |row| Ok(row.requests.contains_key(&*task_id)))?;
            if known != Some(false) {
                return unsupported("the request's ID is in use");
            }
            let request = QueueRequest {
                request_id: task_id.clone(),
                queue_id: point.queue_id.clone(),
                service_point_id: point.id.clone(),
                facility_id: point.facility_id.clone(),
                agent_id: Arc::from(agent_name),
                service_kind: Arc::from("toilet"),
                requested_minute: minute,
                status: Arc::from("queued"),
                queue_task_id: task_id.clone(),
                active_task_id: task_id.clone(),
            };
            let sequence = writer.next_sequence()?;
            Some(transition_row(py, &writer, &request, "queued", &task_id, None, None, Some(position), Some(point.service_capacity), sequence, minute)?)
        }
        None => None,
    };

    // Write.
    queue.call_method1(intern!(py, "append"), (agent_id,))?;
    if let Some(point) = point {
        write_enqueue(world, point, &task_id, &task_id, agent_name, "toilet", minute)?;
    }
    let task_text = PyString::new(py, &task_id);
    let task = active_task_class(py)?.call1((&task_text, "toilet_queue", agent_id, facility_id, minute, minute))?;
    tasks.set_item(&task_text, task)?;
    set_agent(world, &store, agent_id, Activity::Queued, Some(facility_id))?;
    if let Some(transition) = &transition {
        write_transition(&writer, transition, minute)?;
    }
    Ok((task_id, minute, position))
}

/// `ToiletServiceManager.request`, without the world's whole validation.
#[pyfunction]
#[pyo3(signature = (manager, agent_id, facility_id, *, minute=None, validate_state=None, queue_id=None))]
fn request<'py>(
    manager: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    minute: Option<&Bound<'py, PyAny>>,
    validate_state: Option<&Bound<'py, PyAny>>,
    queue_id: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = manager.py();
    if !validate_state.is_some_and(|validate| validate.downcast_exact::<PyBool>().is_ok_and(|validate| !validate.is_true())) {
        return unsupported("the world's whole validation is Python's");
    }
    let queue_name = queue_id.filter(|queue| !queue.is_none()).map(exact_str).transpose()?;
    let world = attribute(manager, intern!(py, "world"))?;
    if let Some(minute) = minute {
        let now = exact_int(&attribute(&world, intern!(py, "minute_index"))?)?;
        if !minute.is_none() && exact_int(minute)? != now {
            return unsupported("another minute");
        }
    }
    let (task_id, requested_at, position) = queue_up(&world, agent_id, facility_id, queue_name)?;
    receipt_class(py)?.call1((agent_id, facility_id, requested_at, &*task_id, position))
}

/// `arbitrate`'s facilities with anyone queued, in ID order. Those with none do nothing there.
#[pyfunction]
fn facilities_to_serve<'py>(manager: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
    let py = manager.py();
    let world = attribute(manager, intern!(py, "world"))?;
    // An independent queue of a facility whose list says nothing has its say in the Python rule.
    let waiting = with_queues(&world, |row| {
        Ok(row.queues.values().filter(|queue| !queue.queued_request_ids.is_empty()).map(|queue| queue.facility_id.clone()).collect::<Vec<_>>())
    })?
    .unwrap_or_default();
    let mut due: Vec<Arc<str>> = Vec::new();
    EntityStore::with_rows(&entities_of(&world)?, |rows| {
        for (id, facility) in rows.facilities.iter() {
            if is_toilet(&facility.kind)? && (!facility.queue_agent_ids.is_empty() || waiting.contains(id)) {
                due.push(id.clone());
            }
        }
        Ok(())
    })?;
    due.sort_unstable();
    let result = PyList::empty(py);
    if !due.is_empty() {
        let facilities = attribute(&world, intern!(py, "facilities"))?;
        for id in due {
            let Some(facility) = raw_dict_get(&facilities, PyString::new(py, &id).as_any())? else {
                return unsupported("a row has no object");
            };
            result.append(facility)?;
        }
    }
    Ok(result)
}

/// `arbitrate`'s condition of a shared queue: someone in it and a free place.
#[pyfunction]
fn has_room<'py>(manager: &Bound<'py, PyAny>, facility_id: &Bound<'py, PyAny>) -> PyResult<bool> {
    let py = manager.py();
    let world = attribute(manager, intern!(py, "world"))?;
    let table = attribute(&world, intern!(py, "model_state"))?;
    exact_dict(&table)?;
    let name = exact_str(facility_id)?;
    if has_independent(&world, name)? {
        return unsupported("a facility with independent queues");
    }
    let found = EntityStore::with_rows(&entities_of(&world)?, |rows| {
        let Some(facility) = rows.facilities.get(name) else {
            return unsupported("no such facility");
        };
        Ok(if facility.queue_agent_ids.is_empty() { None } else { Some((facility.active_agent_ids.len() as i64, facility.working_units)) })
    })?;
    let Some((active, working_units)) = found else {
        return Ok(false);
    };
    Ok(active < service_capacity(&world, &table, name, working_units)?)
}

/// The rest of a pass of `arbitrate`'s loop: the head starts its service, and this says the started
/// event's ID, or None when the service could not finish before the Run ends.
#[pyfunction]
fn start_head<'py>(
    manager: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    rng: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyString>>> {
    let world = attribute(manager, intern!(manager.py(), "world"))?;
    if has_independent(&world, exact_str(facility_id)?)? {
        return unsupported("a facility with independent queues");
    }
    start_one(manager, facility_id, minute, rng, None)
}

/// A pass of `_arbitrate_independent_facility`'s loop after the positions: the started event's ID,
/// or None to stop the queue, with what stopped it marked on the queue.
#[pyfunction]
fn serve_independent_head<'py>(
    manager: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    queue_id: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    rng: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyString>>> {
    start_one(manager, facility_id, minute, rng, Some(exact_str(queue_id)?))
}

/// The head of an independent queue, read when a pass starts.
struct IndependentHead {
    queue_id: Arc<str>,
    request: QueueRequest,
    active_count: i64,
    point_capacity: i64,
    required_skills: Vec<Arc<str>>,
}

fn start_one<'py>(
    manager: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    rng: &Bound<'py, PyAny>,
    queue_id: Option<&str>,
) -> PyResult<Option<Bound<'py, PyString>>> {
    let py = manager.py();
    let world = attribute(manager, intern!(py, "world"))?;
    let facility_name = exact_str(facility_id)?;
    let minute_value = exact_int(minute)?;
    if minute_value != exact_int(&attribute(&world, intern!(py, "minute_index"))?)? {
        return unsupported("another minute");
    }
    let counter_rng = COUNTER_RNG
        .get_or_try_init(py, || -> PyResult<Py<PyAny>> { Ok(py.import("sheltersim.core.rng")?.getattr("CounterRNG")?.unbind()) })?
        .bind(py);
    if !rng.get_type().is(counter_rng) {
        return unsupported("the RNG is not a CounterRNG");
    }
    let seed = exact_int(&attribute(rng, intern!(py, "seed"))?)?;
    let lowest = exact_int(&attribute(manager, intern!(py, "min_service_minutes"))?)?;
    let highest = exact_int(&attribute(manager, intern!(py, "max_service_minutes"))?)?;
    let policy_object = attribute(manager, intern!(py, "policy_version"))?;
    let policy: Arc<str> = Arc::from(exact_str(&policy_object)?);
    let store = entities_of(&world)?;
    let geo = Geo::of(&world)?;
    let head = match queue_id {
        Some(queue_name) => {
            let Some(point) = point_of(&geo, queue_name) else {
                return unsupported("a queue with no signed point");
            };
            let read = with_queues(&world, |row| {
                let Some(queue) = row.queues.get(queue_name) else {
                    return unsupported("no such queue");
                };
                let Some(first) = queue.queued_request_ids.first() else {
                    return unsupported("the queue is empty");
                };
                let Some(request) = row.requests.get(&**first) else {
                    return unsupported("a queue lists an unknown request");
                };
                Ok(IndependentHead {
                    queue_id: Arc::from(queue_name),
                    request: request.clone(),
                    active_count: queue.active_request_ids.len() as i64,
                    point_capacity: if &*point.facility_id == facility_name { point.service_capacity } else { -1 },
                    required_skills: point.required_staff_skills.clone(),
                })
            })?;
            let Some(read) = read else {
                return unsupported("no queue state");
            };
            if read.point_capacity < 0 {
                return unsupported("a queue's point is another facility's");
            }
            Some(read)
        }
        None => None,
    };
    if let Some(head) = &head {
        let (active, limit) = EntityStore::with_rows(&store, |rows| {
            let Some(facility) = rows.facilities.get(facility_name) else {
                return unsupported("no such facility");
            };
            Ok((facility.active_agent_ids.len() as i64, facility.capacity.min(facility.working_units)))
        })?;
        if head.active_count >= head.point_capacity || active >= limit {
            write_stall(&world, &head.queue_id, &head.request.request_id, "capacity")?;
            return Ok(None);
        }
        // Writes only what ends this pass.
        if head_to_service(&world, facility_id, false, minute_value, Some(&head.queue_id))? {
            return Ok(None);
        }
    }
    let (agent_id, queue_task_id, queue_start) = EntityStore::with_rows(&store, |rows| {
        let Some(facility) = rows.facilities.get(facility_name) else {
            return unsupported("no such facility");
        };
        if let Some(head) = &head {
            let Some(task) = rows.active_tasks.get(&*head.request.queue_task_id).filter(|task| &*task.kind == "toilet_queue") else {
                return unsupported("a queue's request lost its task");
            };
            if !facility.queue_agent_ids.contains(&head.request.agent_id) {
                return unsupported("a request is missing from the facility's list");
            }
            return Ok((head.request.agent_id.clone(), task.id.clone(), task.start_minute));
        }
        let Some(agent_id) = facility.queue_agent_ids.first() else {
            return unsupported("the queue is empty");
        };
        // `_queue_task`.
        let mut found = None;
        for (_, task) in rows.active_tasks.iter() {
            if &*task.kind == "toilet_queue" && task.agent_id.as_deref() == Some(&**agent_id) && task.resource_id.as_deref() == Some(facility_name) {
                if found.is_some() {
                    return unsupported("a person with two queue tasks");
                }
                found = Some((task.id.clone(), task.start_minute));
            }
        }
        let Some((task_id, start)) = found else {
            return unsupported("a person with no queue task");
        };
        Ok((agent_id.clone(), task_id, start))
    })?;
    let wait = minute_value.checked_sub(queue_start).ok_or_else(|| Unsupported::new_err("a start out of range"))?;
    if !(0..1 << 53).contains(&wait) {
        return unsupported("a wait for the Python rule to judge");
    }
    // The draw and its key, as `rng_key` holds them.
    let draws = KeyedDraws::new(&seed.to_string());
    let mut key = Key::labelled("toilet-service");
    key.text(facility_name).text(&agent_id).text("minute").int(minute_value).text(&policy).text("duration");
    if let Some(head) = &head {
        key.text(&head.queue_id);
    }
    let draw = key.draw(&draws);
    let span = highest - lowest + 1;
    let service_minutes = (lowest + (draw * span as f64) as i64).min(highest);
    let Some(due) = minute_value.checked_add(service_minutes) else {
        return unsupported("a due minute out of range");
    };
    let writer = EventWriter::of(&world)?;
    if due >= writer.duration() {
        if let Some(head) = &head {
            write_stall(&world, &head.queue_id, &head.request.request_id, "run_end")?;
        }
        return Ok(None);
    }
    // The first free skilled person in ID order, where the point asks for skills.
    let mut staff_id: Option<Arc<str>> = None;
    if let Some(head) = head.as_ref().filter(|head| !head.required_skills.is_empty()) {
        let mut candidates: Vec<Arc<str>> = EntityStore::with_rows(&store, |rows| {
            let Some(facility) = rows.facilities.get(facility_name) else {
                return unsupported("no such facility");
            };
            Ok(rows
                .staff
                .iter()
                .filter(|(_, person)| {
                    head.required_skills.iter().all(|wanted| person.skills.0.contains(wanted))
                        && person.zone_id == facility.zone_id
                        && person.active_task_id.is_none()
                })
                .map(|(_, person)| person.id.clone())
                .collect())
        })?;
        candidates.sort_unstable_by(|first, second| first.as_bytes().cmp(second.as_bytes()));
        let status = staff_status(&world)?;
        for candidate in candidates {
            let id = PyString::new(py, &candidate);
            if is_available(status.as_ref(), &id)? && is_available_now(&world, id.as_any(), minute)? {
                staff_id = Some(candidate);
                break;
            }
        }
        if staff_id.is_none() {
            write_stall(&world, &head.queue_id, &head.request.request_id, "staff")?;
            return Ok(None);
        }
    }
    let sequence = writer.next_sequence()?;
    let event_id: Arc<str> = Arc::from(format!("toilet-complete-{sequence}"));
    let task_id: Arc<str> = Arc::from(format!("toilet-service-{sequence}"));
    {
        let past = writer.log().borrow();
        let task_is_taken = EntityStore::with_rows(&store, |rows| Ok(rows.active_tasks.get(&*task_id).is_some()))?;
        if past.has_event_id(&event_id) || writer.queue().borrow().has_event(&event_id) || task_is_taken {
            return unsupported("the service's identity is taken");
        }
        if past.has_sequence(sequence) {
            return unsupported("the service's sequence is taken");
        }
    }
    let facility_arc: Arc<str> = Arc::from(facility_name);
    let text = |value: &str| V::Str(Arc::from(value));
    let mut payload = IndexMap::with_capacity(3);
    payload.insert(Arc::from("facility_id"), V::Str(facility_arc.clone()));
    payload.insert(Arc::from("agent_id"), V::Str(agent_id.clone()));
    payload.insert(Arc::from("task_id"), V::Str(task_id.clone()));
    if head.is_some() {
        payload.insert(Arc::from("staff_id"), staff_id.as_ref().map_or(V::Null, |id| V::Str(id.clone())));
    }
    let mut provenance = IndexMap::with_capacity(12);
    provenance.insert(Arc::from("source_type"), text("assumption"));
    provenance.insert(Arc::from("source_id"), text(DURATION_SOURCE));
    provenance.insert(Arc::from("policy_version"), V::Str(policy.clone()));
    provenance.insert(Arc::from("duration_distribution"), text("discrete_uniform_inclusive"));
    provenance.insert(Arc::from("minimum_minutes"), V::Int(lowest));
    provenance.insert(Arc::from("maximum_minutes"), V::Int(highest));
    provenance.insert(Arc::from("service_minutes"), V::Int(service_minutes));
    provenance.insert(Arc::from("queue_wait_minutes"), V::Int(wait));
    provenance.insert(Arc::from("requested_at"), V::Int(queue_start));
    provenance.insert(Arc::from("service_started_at"), V::Int(minute_value));
    let mut rng_key = vec![
        text("toilet-service"),
        V::Str(facility_arc.clone()),
        V::Str(agent_id.clone()),
        text("minute"),
        V::Int(minute_value),
        V::Str(policy),
        text("duration"),
    ];
    if let Some(head) = &head {
        rng_key.push(V::Str(head.queue_id.clone()));
    }
    provenance.insert(Arc::from("rng_key"), V::List(rng_key));
    provenance.insert(Arc::from("rng_draw"), V::Float(draw));
    if let Some(head) = &head {
        provenance.insert(Arc::from("queue_id"), V::Str(head.queue_id.clone()));
    }
    let Some(event) = EventRow::new(
        event_id.clone(),
        EventKind::Completion,
        due,
        0,
        sequence,
        Arc::from("toilet_service_complete"),
        payload,
        Some(task_id.clone()),
        Some(queue_task_id.clone()),
        provenance,
    ) else {
        return unsupported("the event's values are not what an event takes");
    };
    let metrics = metrics_of(&world)?;
    let facilities = attribute(&world, intern!(py, "facilities"))?;
    let Some(facility) = raw_dict_get(&facilities, facility_id)? else {
        return unsupported("a row has no object");
    };
    let tasks = attribute(&world, intern!(py, "active_tasks"))?;
    let transition = match &head {
        Some(head) => {
            let capacity = Some(head.point_capacity);
            Some(transition_row(py, &writer, &head.request, "service_started", &task_id, Some(wait), Some(&event_id), None, capacity, sequence + 1, minute_value)?)
        }
        None => None,
    };
    let staff_object = match &staff_id {
        Some(id) => match raw_dict_get(&attribute(&world, intern!(py, "staff"))?, PyString::new(py, id).as_any())? {
            Some(staff) => Some(staff),
            None => return unsupported("a row has no object"),
        },
        None => None,
    };

    // Write.
    writer.allocate_sequence()?;
    let agent_text = PyString::new(py, &agent_id);
    let task_text = PyString::new(py, &task_id);
    let waiting_list = attribute(&facility, intern!(py, "queue_agent_ids"))?;
    if head.is_some() {
        waiting_list.call_method1(intern!(py, "remove"), (&agent_text,))?;
    } else {
        waiting_list.call_method1(intern!(py, "pop"), (0,))?;
    }
    attribute(&facility, intern!(py, "active_agent_ids"))?.call_method1(intern!(py, "append"), (&agent_text,))?;
    tasks.del_item(&*queue_task_id)?;
    let task = match &staff_id {
        Some(staff) => active_task_class(py)?.call1((&task_text, "toilet_service", &agent_text, facility_id, minute_value, due, &**staff))?,
        None => active_task_class(py)?.call1((&task_text, "toilet_service", &agent_text, facility_id, minute_value, due))?,
    };
    tasks.set_item(&task_text, task)?;
    if let (Some(staff_id), Some(staff)) = (&staff_id, &staff_object) {
        EntityStore::with_rows(&store, |rows| {
            let Some(row) = rows.staff.get_mut(staff_id) else {
                return unsupported("no such staff");
            };
            row.active_task_id = Some(task_id.clone());
            row.busy_until = due;
            Ok(())
        })?;
        raw_setattr(staff, intern!(py, "active_task_id"), task_text.as_ptr())?;
        raw_setattr(staff, intern!(py, "busy_until"), due.into_pyobject(py)?.as_ptr())?;
        attribute(staff, intern!(py, "work_queue"))?.set_item(PySlice::full(py), PyList::new(py, [&task_text])?)?;
    }
    set_agent(&world, &store, agent_text.as_any(), Activity::InService, Some(facility_id))?;
    writer.schedule(event)?;
    if let (Some(head), Some(transition)) = (&head, &transition) {
        write_start(&world, &head.request.request_id, &head.queue_id, &task_id)?;
        write_transition(&writer, transition, minute_value)?;
    }
    // `_record_queue_wait`.
    NamespaceMirror::with_row_tree::<MetricRow, _>(&metrics, |row, tree| {
        let value = row.get("toilet_wait_count").copied().unwrap_or(0.0) + 1.0;
        map_set(row, tree, "toilet_wait_count", value)?;
        let value = row.get("toilet_wait_total_minutes").copied().unwrap_or(0.0) + wait as f64;
        map_set(row, tree, "toilet_wait_total_minutes", value)?;
        let value = py_max(row.get("toilet_wait_max_minutes").copied().unwrap_or(0.0), wait as f64);
        map_set(row, tree, "toilet_wait_max_minutes", value)
    })?;
    Ok(Some(PyString::new(py, &event_id)))
}

/// `ToiletServiceCompletionHandler.apply` up to `after_complete`, with `complete_toilet_service`.
pub(crate) fn complete_event<'py>(world: &Bound<'py, PyAny>, event: EventRef<'_, 'py>) -> PyResult<()> {
    let py = world.py();
    let (Some(facility_id), Some(agent_id), Some(task_id)) = (event.text("facility_id")?, event.text("agent_id")?, event.text("task_id")?) else {
        return unsupported("the completion lacks an ID");
    };
    if facility_id.is_empty() || agent_id.is_empty() || task_id.is_empty() {
        return unsupported("the completion lacks an ID");
    }
    let due = event.due_minute()?;
    let store = entities_of(world)?;
    let (staff_id, task_start) = EntityStore::with_rows(&store, |rows| {
        let Some(task) = rows.active_tasks.get(&*task_id) else {
            return unsupported("no active task");
        };
        if task.due_minute != due
            || &*task.kind != "toilet_service"
            || task.agent_id.as_deref() != Some(&*agent_id)
            || task.resource_id.as_deref() != Some(&*facility_id)
        {
            return unsupported("the task does not match");
        }
        // `complete_toilet_service`'s check of a reserved person.
        if let Some(staff_id) = &task.staff_id {
            let Some(staff) = rows.staff.get(&**staff_id) else {
                return unsupported("the staff reservation is inconsistent");
            };
            if staff.active_task_id.as_deref() != Some(&*task.id)
                || staff.work_queue.len() != 1
                || staff.work_queue[0] != task.id
                || staff.busy_until != task.due_minute
            {
                return unsupported("the staff reservation is inconsistent");
            }
        }
        let (Some(facility), Some(agent)) = (rows.facilities.get(&*facility_id), rows.agents.get(&*agent_id)) else {
            return unsupported("no such facility or agent");
        };
        if !is_toilet(&facility.kind)?
            || agent.activity != Activity::InService
            || agent.target_id.as_deref() != Some(&*facility_id)
            || facility.active_agent_ids.iter().filter(|id| ***id == *agent_id).count() != 1
        {
            return unsupported("the service's state does not match");
        }
        Ok((task.staff_id.clone(), task.start_minute))
    })?;
    let metrics = metrics_of(world)?;
    // `finish_service` and `record_queue_transition`.
    let finishing = with_queues(world, |row| {
        let Some(request) = request_of_task(row, &task_id).cloned() else {
            return Ok(None);
        };
        let Some(queue) = row.queues.get(&*request.queue_id) else {
            return unsupported("no such queue");
        };
        // In the list it isn't removed from, the record gives its place.
        let position = if &*request.status == "queued" { None } else { position_in(queue, &request.request_id) };
        Ok(Some((request, position)))
    })?
    .flatten();
    let writer = EventWriter::of(world)?;
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let finished = match finishing {
        Some((request, position)) => {
            let wait = task_start.checked_sub(request.requested_minute).ok_or_else(|| Unsupported::new_err("a wait out of range"))?;
            let capacity = point_of(&*Geo::of(world)?, &request.queue_id).map(|point| point.service_capacity);
            let sequence = writer.next_sequence()?;
            let row = transition_row(py, &writer, &request, "completed", &task_id, Some(wait), Some(&task_id), position, capacity, sequence, minute)?;
            Some((request, row))
        }
        None => None,
    };
    let staff_object = match &staff_id {
        Some(id) => match raw_dict_get(&attribute(world, intern!(py, "staff"))?, PyString::new(py, id).as_any())? {
            Some(staff) => Some(staff),
            None => return unsupported("a row has no object"),
        },
        None => None,
    };
    // `world.model_state.get("body_state_v1", {}).get("agents", {})`.
    let body = if namespace_is_absent(world, intern!(py, BODY_STATE))? {
        let table = attribute(world, intern!(py, "model_state"))?;
        if exact_dict(&table)?.contains(intern!(py, BODY_STATE))? {
            return unsupported("the body state is None");
        }
        None
    } else {
        let mirror = mirror_of(world, BODY_STATE)?;
        let need = NamespaceMirror::with_row::<BodyRow, _>(&mirror, |row| Ok(row.agents.get(&*agent_id).map(|record| record.hygiene_need)))?;
        if need.is_some_and(|need| !need.is_finite()) {
            return unsupported("a need is not finite");
        }
        need.map(|need| (mirror, py_max(0.0, need - HYGIENE_RELIEF)))
    };
    let facilities = attribute(world, intern!(py, "facilities"))?;
    let agents = attribute(world, intern!(py, "agents"))?;
    let agent_text = PyString::new(py, &agent_id);
    let (Some(facility), Some(agent)) = (
        raw_dict_get(&facilities, PyString::new(py, &facility_id).as_any())?,
        raw_dict_get(&agents, agent_text.as_any())?,
    ) else {
        return unsupported("a row has no object");
    };
    let needs = attribute(&agent, intern!(py, "needs"))?;
    let tasks = attribute(world, intern!(py, "active_tasks"))?;

    // Write.
    if let Some((request, row)) = &finished {
        write_finish(world, request)?;
        write_transition(&writer, row, minute)?;
    }
    attribute(&facility, intern!(py, "active_agent_ids"))?.call_method1(intern!(py, "remove"), (&agent_text,))?;
    if let (Some(staff_id), Some(staff)) = (&staff_id, &staff_object) {
        EntityStore::with_rows(&store, |rows| {
            let Some(row) = rows.staff.get_mut(staff_id) else {
                return unsupported("no such staff");
            };
            row.active_task_id = None;
            row.busy_until = 0;
            Ok(())
        })?;
        raw_setattr(staff, intern!(py, "active_task_id"), py.None().as_ptr())?;
        attribute(staff, intern!(py, "work_queue"))?.call_method0(intern!(py, "clear"))?;
        raw_setattr(staff, intern!(py, "busy_until"), 0_i64.into_pyobject(py)?.as_ptr())?;
    }
    tasks.del_item(&*task_id)?;
    set_agent(world, &store, agent_text.as_any(), Activity::Idle, None)?;
    EntityStore::with_rows(&store, |rows| {
        let Some(row) = rows.agents.get_mut(&*agent_id) else {
            return unsupported("no such agent");
        };
        row.needs.toilet_urgency = 0.0;
        Ok(())
    })?;
    raw_setattr(&needs, intern!(py, "toilet_urgency"), 0.0_f64.into_pyobject(py)?.as_ptr())?;
    add_one(&metrics, COMPLETED_METRIC)?;
    if let Some((mirror, need)) = body {
        NamespaceMirror::with_row_tree::<BodyRow, _>(&mirror, |row, tree| {
            let record = row
                .agents
                .get_mut(&*agent_id)
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("a planned record left the row"))?;
            let records = tree.child(intern!(py, "agents"))?;
            record.set_hygiene_need(&records.child_str(&agent_id)?, need)
        })?;
    }
    Ok(())
}

/// `ToiletServiceCompletionHandler.apply`, called from Python: then its `after_complete`.
#[pyfunction]
fn complete<'py>(handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = handler.py();
    complete_event(world, EventRef::Object(event))?;
    let after = attribute(handler, intern!(py, "after_complete"))?;
    if !after.is_none() {
        after.call1((world, event))?;
    }
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(request, m)?)?;
    m.add_function(wrap_pyfunction!(facilities_to_serve, m)?)?;
    m.add_function(wrap_pyfunction!(has_room, m)?)?;
    m.add_function(wrap_pyfunction!(start_head, m)?)?;
    m.add_function(wrap_pyfunction!(serve_independent_head, m)?)?;
    m.add_function(wrap_pyfunction!(complete, m)?)?;
    Ok(())
}
