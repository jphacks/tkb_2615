//! `AgentTravelCompletionHandler` of `events/handlers.py`: a walk that ended. It reads the event's
//! row and the entity rows, decides, and writes the task, the person's row and object. Nothing is
//! drawn and nothing is scheduled here.
//!
//! What a walk leads to is `after_arrival` here (the approach inside a room, the toilet's and the
//! service's queue). What it refuses, having changed nothing, is `handler._after_arrival`, as are
//! what the queue walks ask of `domain/movement.py` and `domain/facility_queues.py`. Those are
//! called with no borrow held.
//!
//! A walk the rule refuses says Unsupported before any change, and the Python rule says why in its
//! own text. So do `sleep_bed` and `presence_relocation`, which write `movement_v1` and
//! `population_lifecycle_v1` (docs/rust-port/lanes/m3-dispatch.md).

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyString};

use crate::core::row::{attribute, exact_dict, exact_int, exact_str, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::raw_dict_get;
use crate::state::entities::{entities_of, raw_setattr, Activity, EntityStore, RequestStatus};
use crate::rules::travel::{approach_route, plan_room_approach, write_travel};
use crate::rules::{service, toilet};
use crate::state::events::{EventRef, EventRow, EventWriter};
use crate::Unsupported;

const QUEUES_KEY: &str = "facility_queues_v1";
static ARRIVALS_RAN: AtomicU64 = AtomicU64::new(0);
static ARRIVALS_REFUSED: AtomicU64 = AtomicU64::new(0);
static ARRIVAL_REFUSALS: Mutex<BTreeMap<String, u64>> = Mutex::new(BTreeMap::new());

/// What the rule reads of a walk's event. `origin` is `origin_zone_id` when a str, or else the
/// first of the route.
struct Walk {
    event_id: Arc<str>,
    due_minute: i64,
    agent_id: Arc<str>,
    task_id: Arc<str>,
    destination: Arc<str>,
    purpose: Arc<str>,
    facility_id: Option<Arc<str>>,
    origin: Option<Arc<str>>,
    route_ends: Option<(Option<Arc<str>>, Option<Arc<str>>)>,
}

fn required(event: EventRef<'_, '_>, key: &str) -> PyResult<Arc<str>> {
    match event.text(key)? {
        Some(value) if !value.is_empty() => Ok(value),
        _ => unsupported("the walk lacks an ID"),
    }
}

impl Walk {
    fn read(event: EventRef<'_, '_>) -> PyResult<Self> {
        let route_ends = event.list_ends("route_zone_ids")?;
        let origin = match event.text("origin_zone_id")? {
            Some(origin) => Some(origin),
            None => match &route_ends {
                Some((Some(first), _)) => Some(first.clone()),
                // A list that starts with another value is for the Python rule to read.
                Some((None, Some(_))) => return unsupported("a route of other values"),
                _ => None,
            },
        };
        Ok(Self {
            event_id: event.event_id()?,
            due_minute: event.due_minute()?,
            agent_id: required(event, "agent_id")?,
            task_id: required(event, "task_id")?,
            destination: required(event, "destination_zone_id")?,
            purpose: required(event, "purpose")?,
            facility_id: event.text("facility_id")?,
            origin,
            route_ends,
        })
    }
}

/// `ResourceServiceManager.resource_for_facility(kind) is None`.
fn is_resource_facility(kind: &str) -> bool {
    matches!(kind, "water_station" | "meal_distribution")
}

fn movement_module<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyModule>> {
    py.import(intern!(py, "sheltersim.domain.movement"))
}

/// Whether `dict.get(key) != expected` cannot be told without the Python rule's comparison. A value
/// of another type is not equal; only a str subclass could be, and the Python rule then decides.
fn get_is(dict: &Bound<'_, PyDict>, key: &Bound<'_, PyString>, expected: &str) -> PyResult<bool> {
    Ok(match dict.get_item(key)? {
        Some(value) => exact_str(&value).is_ok_and(|value| value == expected),
        None => false,
    })
}

/// `isinstance(value, dict)`, as the rule asks of the trees it reads.
fn tree<'a, 'py>(value: &'a Bound<'py, PyAny>) -> PyResult<&'a Bound<'py, PyDict>> {
    match as_readable_dict(value) {
        Some(dict) => Ok(dict),
        None => unsupported("a tree of the rule is not a plain dict"),
    }
}

/// The walks to a queue's place or service point: only checks, then nothing changes.
fn check_queue_walk(world: &Bound<'_, PyAny>, row: EventRef<'_, '_>, walk: &Walk) -> PyResult<()> {
    let py = world.py();
    let (agent_id, task_id, destination, purpose) = (&walk.agent_id, &walk.task_id, &walk.destination, &*walk.purpose);
    let facility_id = walk.facility_id.as_ref();
    let resource = purpose.starts_with("resource_");
    let expected_kind = if resource { "resource_service_queue" } else { "toilet_queue" };
    let position = purpose.ends_with("_position");

    let table = attribute(world, intern!(py, "model_state"))?;
    let journey = match raw_dict_get(&table, intern!(py, "movement_v1").as_any())? {
        None => None,
        Some(movement) => match tree(&movement)?.get_item(intern!(py, "journeys"))? {
            None => None,
            Some(journeys) => tree(&journeys)?.get_item(&**agent_id)?,
        },
    };
    let journey = match journey {
        Some(journey) => journey,
        None => PyDict::new(py).into_any(),
    };
    let journey_dict = tree(&journey)?;

    let movement = movement_module(py)?;
    let queues = py.import(intern!(py, "sheltersim.domain.facility_queues"))?;
    let independent = match facility_id {
        Some(facility) => queues
            .call_method1(intern!(py, "facility_has_independent_queues"), (world, &**facility))?
            .is_truthy()?,
        None => false,
    };
    let at_slot = |queue: Option<&str>| -> PyResult<bool> {
        let Some(facility) = facility_id else {
            return Ok(false);
        };
        let at = match queue {
            Some(queue) => movement.call_method1(intern!(py, "agent_is_at_queue_slot"), (world, &**agent_id, &**facility, queue))?,
            None => movement.call_method1(intern!(py, "agent_is_at_queue_slot"), (world, &**agent_id, &**facility))?,
        };
        if at.is_truthy()? {
            return Ok(true);
        }
        movement
            .call_method1(intern!(py, "agent_is_at_queue_trip_destination"), (world, &**agent_id, &journey))?
            .is_truthy()
    };
    let (contains_request, at_destination) = if independent {
        let state = raw_dict_get(&table, intern!(py, "facility_queues_v1").as_any())?
            .ok_or_else(|| Unsupported::new_err("no queue state"))?;
        let state = tree(&state)?;
        let (request_id, queue_id) = (row.text("request_id")?, row.text("queue_id")?);
        let (Some(request_id), Some(queue_id), Some(requests), Some(rows)) = (
            request_id.as_ref(),
            queue_id.as_ref(),
            state.get_item(intern!(py, "requests"))?,
            state.get_item(intern!(py, "queues"))?,
        ) else {
            return unsupported("an independent queue walk without its request");
        };
        let request = tree(&requests)?.get_item(&**request_id)?;
        let queue_row = tree(&rows)?.get_item(&**queue_id)?;
        let (Some(request), Some(queue_row)) = (request, queue_row) else {
            return unsupported("the queue no longer holds the walk");
        };
        let (request, queue_row) = (tree(&request)?, tree(&queue_row)?);
        let queued = queue_row
            .get_item(intern!(py, "queued_request_ids"))?
            .ok_or_else(|| Unsupported::new_err("a queue without its list"))?;
        let Some(facility) = facility_id else {
            return unsupported("an independent queue walk without its facility");
        };
        let contains = queued.contains(&**request_id)?
            && get_is(request, intern!(py, "agent_id"), agent_id)?
            && get_is(request, intern!(py, "facility_id"), facility)?
            && get_is(request, intern!(py, "queue_id"), queue_id)?;
        let at = if position {
            at_slot(Some(queue_id))?
        } else {
            match request.get_item(intern!(py, "service_point_id"))? {
                Some(point) if exact_str(&point).is_ok() => movement
                    .call_method1(
                        intern!(py, "agent_is_at_facility_service_point"),
                        (world, &**agent_id, &**facility, point),
                    )?
                    .is_truthy()?,
                _ => false,
            }
        };
        (Some(contains), at)
    } else {
        let at = if position {
            at_slot(None)?
        } else {
            match facility_id {
                Some(facility) => movement
                    .call_method1(intern!(py, "agent_is_at_facility_service_point"), (world, &**agent_id, &**facility))?
                    .is_truthy()?,
                None => false,
            }
        };
        (None, at)
    };

    let journey_fits = if journey_dict.is_empty() {
        let minute = attribute(world, intern!(py, "minute_index"))?;
        crate::core::row::exact_int(&minute)? == walk.due_minute
    } else {
        let due = journey_dict.get_item(intern!(py, "due_minute"))?;
        let queue_id = row.text("queue_id")?;
        get_is(journey_dict, intern!(py, "event_id"), &walk.event_id)?
            && get_is(journey_dict, intern!(py, "task_id"), task_id)?
            && get_is(journey_dict, intern!(py, "purpose"), purpose)?
            && due.is_some_and(|due| crate::core::row::exact_int(&due).is_ok_and(|due| due == walk.due_minute))
            && (!independent || queue_id.is_some_and(|queue| get_is(journey_dict, intern!(py, "queue_id"), &queue).unwrap_or(false)))
    };

    let store = entities_of(world)?;
    let fits = EntityStore::with_rows(&store, |rows| {
        let (Some(task), Some(agent)) = (rows.active_tasks.get(task_id), rows.agents.get(agent_id)) else {
            return Ok(false);
        };
        let facility = facility_id.and_then(|id| rows.facilities.get(id));
        let contains = match contains_request {
            Some(contains) => contains,
            None => facility.is_some_and(|facility| {
                let queue = if resource { &facility.resource_queue_agent_ids } else { &facility.queue_agent_ids };
                queue.iter().any(|id| id == agent_id)
            }),
        };
        Ok(&*task.kind == expected_kind
            && task.due_minute == task.start_minute
            && agent.activity == Activity::Queued
            && agent.target_id.as_deref() == facility_id.map(|id| &**id)
            && contains
            && facility.is_some_and(|facility| facility.zone_id == *destination)
            && agent.zone_id == *destination
            && at_destination
            && journey_fits)
    })?;
    if !fits {
        return unsupported("the queue walk does not fit, which the Python rule says");
    }
    Ok(())
}

/// `AgentTravelCompletionHandler._after_arrival`, where no rejection follows. A rejection reads the
/// event and is the Python rule's, which then runs from the start.
fn arrive<'py>(
    handler: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    agent_id: &str,
    purpose: &str,
    facility_id: &str,
    destination: &str,
) -> PyResult<()> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    if exact_dict(&table)?.contains(QUEUES_KEY)? {
        return unsupported("an independent FIFO may be here");
    }
    let is_toilet = purpose == "toilet_request";
    if !is_toilet && purpose != "resource_request" {
        return Ok(());
    }
    let configs = attribute(world, intern!(py, "model_configs"))?;
    if exact_dict(&configs)?.contains(intern!(py, "facility_geometry"))? {
        let Some((route, points)) = approach_route(world, agent_id, facility_id)? else {
            return unsupported("a blocked room is the Python rule's");
        };
        if route.minutes > 0 {
            let writer = EventWriter::of(world)?;
            let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
            let (agent_arc, facility_arc, origin): (Arc<str>, Arc<str>, Arc<str>) =
                (Arc::from(agent_id), Arc::from(facility_id), Arc::from(destination));
            let Some(planned) = plan_room_approach(world, &writer, &agent_arc, &facility_arc, purpose, route, &points, minute, &origin)? else {
                return unsupported("a blocked room is the Python rule's");
            };
            return write_travel(world, &writer, planned);
        }
    }
    let (agent, facility) = (PyString::new(py, agent_id), PyString::new(py, facility_id));
    // A method set on the manager (a test's patch) is the Python rule's to call.
    let own_request = |manager: &Bound<'py, PyAny>| -> PyResult<()> {
        if raw_dict_get(&manager.getattr(intern!(py, "__dict__"))?, intern!(py, "request").as_any())?.is_some() {
            return unsupported("the manager's request is another function");
        }
        Ok(())
    };
    if is_toilet {
        // `validate_request_state`: only the False that the controller gives.
        let validate = attribute(handler, intern!(py, "validate_request_state"))?;
        if !validate.is(PyBool::new(py, false).as_any()) {
            return unsupported("the world's whole validation is Python's");
        }
        own_request(&attribute(handler, intern!(py, "toilet_service"))?)?;
        toilet::queue_up(world, agent.as_any(), facility.as_any())?;
    } else {
        let manager = attribute(handler, intern!(py, "resource_service"))?;
        own_request(&manager)?;
        service::queue_up(&manager, agent.as_any(), facility.as_any())?;
    }
    Ok(())
}

/// `_after_arrival`, called from Python.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn after_arrival<'py>(
    handler: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    purpose: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    destination_zone_id: &Bound<'py, PyAny>,
    origin_zone_id: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let _ = (event, origin_zone_id);
    arrive(handler, world, exact_str(agent_id)?, exact_str(purpose)?, exact_str(facility_id)?, exact_str(destination_zone_id)?)
}

/// `AgentTravelCompletionHandler.apply` of a walk. `object` gives the event's object, for Python.
/// `direct` asks `_after_arrival`'s kernel with no Python between.
pub(crate) fn apply_event<'py>(
    handler: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    event: EventRef<'_, 'py>,
    object: &dyn Fn() -> PyResult<Bound<'py, PyAny>>,
    direct: bool,
) -> PyResult<()> {
    let py = world.py();
    let walk = Walk::read(event)?;
    let (agent_id, task_id, destination, purpose) = (&walk.agent_id, &walk.task_id, &walk.destination, &walk.purpose);
    let facility_id = walk.facility_id.as_ref();

    let store = entities_of(world)?;
    // The checks before a purpose is looked at, which every walk passes.
    let known_kinds = EntityStore::with_rows(&store, |rows| {
        let (Some(task), Some(agent)) = (rows.active_tasks.get(task_id), rows.agents.get(agent_id)) else {
            return unsupported("no such task or person");
        };
        Ok(matches!(
            &*task.kind,
            "travel_to_toilet" | "travel_to_resource" | "travel_return" | "staff_support_travel" | "toilet_queue" | "resource_service_queue"
        ) && task.agent_id.as_ref() == Some(agent_id)
            && agent.profile.id == *agent_id)
    })?;
    if !known_kinds {
        return unsupported("the walk does not match its task");
    }
    if matches!(
        &**purpose,
        "toilet_queue_position" | "resource_queue_position" | "toilet_queue_service" | "resource_queue_service"
    ) {
        return check_queue_walk(world, event, &walk);
    }

    let has_resource_service = !attribute(handler, intern!(py, "resource_service"))?.is_none();
    let zones = attribute(world, intern!(py, "zones"))?;
    let destination_key = PyString::new(py, destination);
    if raw_dict_get(&zones, destination_key.as_any())?.is_none() {
        return unsupported("the destination is not a zone");
    }
    // `route_zone_ids[0] == origin`, `[-1] == destination`, and that the origin is a zone.
    let route_fits = if &**purpose == "resource_request" {
        let Some(origin) = &walk.origin else {
            return unsupported("a resource walk without its origin");
        };
        raw_dict_get(&zones, PyString::new(py, origin).as_any())?.is_some()
            && matches!(&walk.route_ends, Some((Some(first), Some(last))) if first == origin && last == destination)
    } else {
        true
    };
    if !route_fits {
        return unsupported("the route does not match its target");
    }

    let (request_id, staff_id) = if &**purpose == "staff_support_travel" {
        (event.text("request_id")?, event.text("staff_id")?)
    } else {
        (None, None)
    };
    let activity = if &**purpose == "staff_support_travel" { Activity::SeekingHelp } else { Activity::Idle };
    EntityStore::with_rows(&store, |rows| {
        let task = rows.active_tasks.get(task_id).expect("read above");
        let agent = rows.agents.get(agent_id).expect("read above");
        if task.due_minute != walk.due_minute || agent.activity != Activity::Moving {
            return unsupported("the walk does not match its task");
        }
        let at_target = |facility: &str| agent.target_id.as_deref() == Some(facility);
        let good = match &**purpose {
            "toilet_request" => match facility_id {
                Some(facility) if !facility.is_empty() => {
                    rows.facilities.get(facility).is_some_and(|found| found.zone_id == *destination)
                        && &*task.kind == "travel_to_toilet"
                        && at_target(facility)
                }
                _ => false,
            },
            "resource_request" => match facility_id {
                Some(facility) if !facility.is_empty() && has_resource_service => {
                    rows.facilities
                        .get(facility)
                        .is_some_and(|found| found.zone_id == *destination && is_resource_facility(&found.kind))
                        && &*task.kind == "travel_to_resource"
                        && at_target(facility)
                }
                _ => false,
            },
            "return" | "queue_abandon" => &*task.kind == "travel_return" && at_target(destination),
            "staff_support_travel" => {
                let request = request_id.as_ref().and_then(|id| rows.support_requests.get(id));
                let staff = staff_id.as_ref().and_then(|id| rows.staff.get(id));
                match (request, staff) {
                    (Some(request), Some(staff)) => {
                        &*task.kind == "staff_support_travel"
                            && request.agent_id == *agent_id
                            && request.status == RequestStatus::Assigned
                            && request.assigned_staff_id == staff_id
                            && request.task_id.is_some()
                            && agent.support_request_id.as_ref() == Some(&request.id)
                            && staff.zone_id == *destination
                            && at_target(destination)
                    }
                    _ => false,
                }
            }
            // A bed's position, a relocation's lifecycle, and any other purpose.
            _ => false,
        };
        if !good {
            return unsupported("the walk is not one this rule takes, or does not fit");
        }
        Ok(())
    })?;

    // Everything is decided, and no Unsupported comes from here on.
    EntityStore::with_rows(&store, |rows| {
        let agent = rows.agents.get_mut(agent_id).expect("read above");
        agent.zone_id = destination.clone();
        agent.activity = activity;
        agent.target_id = None;
        Ok(())
    })?;
    let tasks = attribute(world, intern!(py, "active_tasks"))?;
    tasks.del_item(&**task_id)?;
    let agents = attribute(world, intern!(py, "agents"))?;
    let person = raw_dict_get(&agents, PyString::new(py, agent_id).as_any())?
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("a row has no object"))?;
    raw_setattr(&person, intern!(py, "zone_id"), destination_key.as_ptr())?;
    raw_setattr(&person, intern!(py, "activity"), activity.member(py)?.as_ptr())?;
    raw_setattr(&person, intern!(py, "target_id"), py.None().as_ptr())?;

    if matches!(&**purpose, "toilet_request" | "resource_request") {
        let facility = facility_id.map(|id| &**id).unwrap_or_default();
        if direct {
            match arrive(handler, world, agent_id, purpose, facility, destination) {
                Ok(()) => {
                    ARRIVALS_RAN.fetch_add(1, Ordering::Relaxed);
                    return Ok(());
                }
                Err(error) if error.is_instance_of::<Unsupported>(py) => {
                    ARRIVALS_REFUSED.fetch_add(1, Ordering::Relaxed);
                    if let Ok(mut refusals) = ARRIVAL_REFUSALS.lock() {
                        *refusals.entry(error.value(py).to_string()).or_insert(0) += 1;
                    }
                }
                Err(error) => return Err(error),
            }
        }
        let origin = walk.origin.as_deref();
        handler
            .call_method1(
                intern!(py, "_after_arrival"),
                (world, object()?, &**agent_id, &**purpose, facility, &**destination, origin),
            )
            .map_err(|error| {
                if error.is_instance_of::<Unsupported>(py) {
                    PyRuntimeError::new_err("a kernel said Unsupported after the walk was written")
                } else {
                    error
                }
            })?;
    }
    Ok(())
}

/// `AgentTravelCompletionHandler.apply`, called from Python.
#[pyfunction]
fn apply<'py>(handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    if EventRow::from_event(event)?.is_none() {
        return unsupported("the event has no row");
    }
    apply_event(handler, world, EventRef::Object(event), &|| Ok(event.clone()), false)
}

/// How often `direct` found its way: `(ran, refused, reasons)`.
pub(crate) fn arrival_counts() -> (u64, u64, BTreeMap<String, u64>) {
    let reasons = ARRIVAL_REFUSALS.lock().map(|refusals| refusals.clone()).unwrap_or_default();
    (ARRIVALS_RAN.load(Ordering::Relaxed), ARRIVALS_REFUSED.load(Ordering::Relaxed), reasons)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(apply, m)?)?;
    m.add_function(wrap_pyfunction!(after_arrival, m)?)?;
    Ok(())
}
