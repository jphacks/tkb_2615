//! `AgentTravelCompletionHandler` of `events/handlers.py`: a walk that ended. It reads the event's
//! row and the entity rows, decides, and writes the task, the person's row and object. Nothing is
//! drawn and nothing is scheduled here.
//!
//! What a walk leads to is `after_arrival` here (the approach inside a room, the toilet's and the
//! service's queue). What it refuses, having changed nothing, is `handler._after_arrival`.
//!
//! A walk the rule refuses raises the rule's own `EventApplicationError` text before any change,
//! in the rule's order of checks (`decide`). Every check of the rule comes before its first write,
//! so nothing is written when one fails. Where Rust can't tell what the rule would read or compare
//! (a call that would raise, a value of another shape), it says Unsupported, still before any change,
//! and the Python rule decides. `sleep_bed` and `presence_relocation` read `movement_v1` and
//! `population_lifecycle_v1` (docs/rust-port/lanes/m3-dispatch.md).

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyInt, PyList, PyString};

use crate::core::errors;
use crate::core::row::{attribute, exact_dict, exact_int, exact_str, str_value, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::raw_dict_get;
use crate::state::entities::{entities_of, raw_setattr, Activity, EntityStore, RequestStatus};
use crate::rules::movement::{at_queue_slot, at_service_point, at_trip_destination};
use crate::rules::travel::{approach_route, plan_room_approach, write_travel};
use crate::rules::effects::refused_by_python;
use crate::rules::{service, toilet};
use crate::state::resource_service::RequestRecord;
use crate::state::events::{EventRef, EventRow, EventWriter};
use crate::Unsupported;

static ARRIVALS_RAN: AtomicU64 = AtomicU64::new(0);
static ARRIVALS_REFUSED: AtomicU64 = AtomicU64::new(0);
static ARRIVAL_REFUSALS: Mutex<BTreeMap<String, u64>> = Mutex::new(BTreeMap::new());

/// The rule's `origin_zone_id`: the event's when a str, else the first of a list or tuple route,
/// else None.
enum Origin {
    Zone(Arc<str>),
    /// No route: None.
    Nothing,
    /// A route whose first is not a str, or an empty one (the ends Rust reads don't tell them
    /// apart): not a str, which may be None.
    NotStr,
}

impl Origin {
    fn zone(&self) -> Option<&str> {
        match self {
            Origin::Zone(zone) => Some(zone),
            _ => None,
        }
    }
}

/// What the rule reads of a walk's event.
struct Walk {
    event_id: Arc<str>,
    due_minute: i64,
    agent_id: Arc<str>,
    task_id: Arc<str>,
    destination: Arc<str>,
    purpose: Arc<str>,
    facility_id: Option<Arc<str>>,
    origin: Origin,
    route_ends: Option<(Option<Arc<str>>, Option<Arc<str>>)>,
}

fn required(event: EventRef<'_, '_>, key: &str) -> PyResult<Arc<str>> {
    match event.text(key)? {
        Some(value) if !value.is_empty() => Ok(value),
        _ => Python::with_gil(|py| errors::failed(py, "travel completion requires Agent, task, destination, and purpose")),
    }
}

impl Walk {
    fn read(event: EventRef<'_, '_>) -> PyResult<Self> {
        let route_ends = event.list_ends("route_zone_ids")?;
        let origin = match event.text("origin_zone_id")? {
            Some(origin) => Origin::Zone(origin),
            None => match &route_ends {
                Some((Some(first), _)) => Origin::Zone(first.clone()),
                Some(_) => Origin::NotStr,
                None => Origin::Nothing,
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

/// `value != text`, as Python compares: an exact str in Rust, any other value by Python's `!=`.
fn differs(value: &Bound<'_, PyAny>, text: &str) -> PyResult<bool> {
    match value.downcast_exact::<PyString>() {
        Ok(value) => Ok(value.to_str().map_err(|_| Unsupported::new_err("a str is not UTF-8"))? != text),
        Err(_) => value.ne(text),
    }
}

/// `value != number`, as Python compares.
fn differs_int(value: &Bound<'_, PyAny>, number: i64) -> PyResult<bool> {
    if let Ok(int) = value.downcast_exact::<PyInt>() {
        if let Ok(value) = int.extract::<i64>() {
            return Ok(value != number);
        }
    }
    value.ne(number)
}

/// `map.get(key)`, Python's own.
fn get_of<'py>(map: &Bound<'py, PyAny>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    map.call_method1(intern!(map.py(), "get"), (key,))
}

/// `map.get(key, default)`, Python's own.
fn get_or<'py>(map: &Bound<'py, PyAny>, key: &str, default: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    map.call_method1(intern!(map.py(), "get"), (key, default))
}

/// `isinstance(value, dict)`, as the rule asks of the trees it reads.
fn tree<'a, 'py>(value: &'a Bound<'py, PyAny>) -> PyResult<&'a Bound<'py, PyDict>> {
    match as_readable_dict(value) {
        Some(dict) => Ok(dict),
        None => unsupported("a tree of the rule is not a plain dict"),
    }
}

/// The walks to a queue's place or service point: whether the walk fits its FIFO reservation. The
/// rule reads the queue's trees and the person's place first, then checks; any check that fails
/// raises one text. A read that would raise in Python, or a value Rust can't compare as Python
/// does, is Unsupported.
fn queue_walk_fits(world: &Bound<'_, PyAny>, row: EventRef<'_, '_>, walk: &Walk) -> PyResult<bool> {
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

    let independent = match facility_id {
        Some(facility) => crate::rules::facility_queues::has_independent(world, facility)?,
        None => false,
    };
    let queue_id = row.text("queue_id")?;
    let (contains_request, at_destination) = if independent {
        let facility = facility_id.expect("an independent queue is a facility's");
        // `world.model_state[FACILITY_QUEUES_STATE_KEY]`, then `["requests"]` and `["queues"]` as
        // the rule reads them: only for an ID that is a str.
        let state = raw_dict_get(&table, intern!(py, "facility_queues_v1").as_any())?
            .ok_or_else(|| Unsupported::new_err("no queue state"))?;
        let request_id = row.text("request_id")?;
        let request = match &request_id {
            Some(id) => Some(get_of(&state.get_item(intern!(py, "requests"))?, id)?),
            None => None,
        };
        let queue_row = match &queue_id {
            Some(id) => Some(get_of(&state.get_item(intern!(py, "queues"))?, id)?),
            None => None,
        };
        let request = request.filter(|request| request.is_instance_of::<PyDict>());
        let queue_row = queue_row.filter(|queue_row| queue_row.is_instance_of::<PyDict>());
        let contains = match (&request, &queue_row, &request_id, &queue_id) {
            (Some(request), Some(queue_row), Some(request_id), Some(queue_id)) => {
                queue_row.get_item(intern!(py, "queued_request_ids"))?.contains(&**request_id)?
                    && !differs(&request.get_item(intern!(py, "agent_id"))?, agent_id)?
                    && !differs(&request.get_item(intern!(py, "facility_id"))?, facility)?
                    && !differs(&request.get_item(intern!(py, "queue_id"))?, queue_id)?
            }
            _ => false,
        };
        let at = if position {
            // `agent_is_at_queue_slot(world, agent_id, facility_id, queue_id)` of the event's own
            // value, which Rust reads only as a str.
            let Some(queue) = &queue_id else {
                return unsupported("a queue walk's queue_id is not a str");
            };
            at_queue_slot(world, agent_id, facility, Some(queue))? || at_trip_destination(world, agent_id)?
        } else {
            let point = match &request {
                Some(request) => get_of(request, "service_point_id")?,
                None => py.None().into_bound(py),
            };
            match errors::str_of(&point)? {
                Some(point) => at_service_point(world, agent_id, facility, Some(point))?,
                None => false,
            }
        };
        (Some(contains), at)
    } else {
        let at = match facility_id {
            Some(facility) if position => at_queue_slot(world, agent_id, facility, None)? || at_trip_destination(world, agent_id)?,
            Some(facility) => at_service_point(world, agent_id, facility, None)?,
            None => false,
        };
        (None, at)
    };

    let journey_fits = if journey_dict.is_empty() {
        let minute = attribute(world, intern!(py, "minute_index"))?;
        !differs_int(&minute, walk.due_minute)?
    } else {
        !differs(&get_of(&journey, "event_id")?, &walk.event_id)?
            && !differs(&get_of(&journey, "task_id")?, task_id)?
            && !differs(&get_of(&journey, "purpose")?, purpose)?
            && !differs_int(&get_of(&journey, "due_minute")?, walk.due_minute)?
            && (!independent
                || match &queue_id {
                    Some(queue) => !differs(&get_of(&journey, "queue_id")?, queue)?,
                    None => return unsupported("a queue walk's queue_id is not a str"),
                })
    };

    let store = entities_of(world)?;
    EntityStore::with_rows(&store, |rows| {
        let (Some(task), Some(agent)) = (rows.active_tasks.get(task_id), rows.agents.get(agent_id)) else {
            return unsupported("the task or the person left before the queue walk's checks");
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
    })
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
    origin: Option<&str>,
) -> PyResult<()> {
    let py = world.py();
    let is_toilet = purpose == "toilet_request";
    if !is_toilet && purpose != "resource_request" {
        return Ok(());
    }
    let configs = attribute(world, intern!(py, "model_configs"))?;
    if exact_dict(&configs)?.contains(intern!(py, "facility_geometry"))? && !crate::rules::facility_queues::has_independent(world, facility_id)? {
        let Some((route, points)) = approach_route(world, agent_id, facility_id)? else {
            return reject_room(handler, world, purpose, agent_id, facility_id, origin);
        };
        if route.minutes > 0 {
            let writer = EventWriter::of(world)?;
            let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
            let (agent_arc, facility_arc, origin_arc): (Arc<str>, Arc<str>, Arc<str>) =
                (Arc::from(agent_id), Arc::from(facility_id), Arc::from(destination));
            let Some(planned) = plan_room_approach(world, &writer, &agent_arc, &facility_arc, purpose, route, &points, minute, &origin_arc)? else {
                return reject_room(handler, world, purpose, agent_id, facility_id, origin);
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
        toilet::queue_up(world, agent.as_any(), facility.as_any(), None)?;
    } else {
        let manager = attribute(handler, intern!(py, "resource_service"))?;
        own_request(&manager)?;
        service::queue_up(&manager, agent.as_any(), facility.as_any())?;
    }
    Ok(())
}

/// `_reject_room_approach`. `origin` is the event's origin when a str.
fn reject_room<'py>(
    handler: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    purpose: &str,
    agent_id: &str,
    facility_id: &str,
    origin: Option<&str>,
) -> PyResult<()> {
    let py = world.py();
    let metrics = service::metrics_of(world)?;
    if purpose == "resource_request" && !attribute(handler, intern!(py, "resource_service"))?.is_none() {
        let store = entities_of(world)?;
        let (kind, zone) = EntityStore::with_rows(&store, |rows| {
            let Some(agent) = rows.agents.get(agent_id) else {
                return unsupported("no such person");
            };
            Ok((rows.facilities.get(facility_id).map(|facility| facility.kind.clone()), agent.zone_id.clone()))
        })?;
        let action = if kind.as_deref().and_then(service::resource_of) == Some("water_ml") { "fetch_water" } else { "collect_meal" };
        let mirror = crate::state::resource_service::held_mirror(world)?;
        service::append_outcome(
            py,
            &mirror,
            RequestRecord {
                minute: exact_int(&attribute(world, intern!(py, "minute_index"))?)?,
                agent_id: Arc::from(agent_id),
                action: Arc::from(action),
                outcome: Arc::from("rejected"),
                reason: Arc::from("room_route_blocked"),
                facility_id: Some(Arc::from(facility_id)),
                origin_zone_id: origin.map_or(zone, Arc::from),
                detail: None,
            },
        )?;
        service::add_one(&metrics, "resource_service_request_rejected_count")?;
    }
    service::add_one(&metrics, "facility_approach_rejected_count")
}

/// `_reject_room_approach`, called from Python.
#[pyfunction]
fn reject_room_approach<'py>(
    handler: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    origin_zone_id: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let purpose = crate::rules::handlers::get(&attribute(event, intern!(world.py(), "payload"))?, "purpose")?;
    let purpose = if text_is_str(&purpose, "resource_request") { "resource_request" } else { "other" };
    reject_room(handler, world, purpose, exact_str(agent_id)?, exact_str(facility_id)?, str_value(origin_zone_id))
}

fn text_is_str(value: &Bound<'_, PyAny>, wanted: &str) -> bool {
    str_value(value) == Some(wanted)
}

/// `sleep_bed`'s reads, then its checks, with Python's own `get`, `len`, `[]` and `!=`: the bed's
/// point when the walk fits its live reservation, None when the rule raises its text. `rows_fit` is
/// its first two checks, the task's kind and the person's target.
fn sleep_bed_fits<'py>(world: &Bound<'py, PyAny>, walk: &Walk, rows_fit: bool) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = world.py();
    let none = || py.None().into_bound(py);
    let dict = || PyDict::new(py).into_any();
    let table = attribute(world, intern!(py, "model_state"))?;
    let overlay = get_or(&table, "geometry_overlay_v1", dict())?;
    let overlay_is_dict = overlay.is_instance_of::<PyDict>();
    let reservations = if overlay_is_dict { get_or(&overlay, "bed_reservations", dict())? } else { dict() };
    let reservation = if reservations.is_instance_of::<PyDict>() { get_of(&reservations, &walk.agent_id)? } else { none() };
    let reservation_is_dict = reservation.is_instance_of::<PyDict>();
    let rooms = if overlay_is_dict { get_or(&overlay, "temporary_rooms", dict())? } else { dict() };
    let room = if reservation_is_dict {
        rooms.call_method1(intern!(py, "get"), (get_of(&reservation, "room_id")?,))?
    } else {
        none()
    };
    let movement = get_or(&table, "movement_v1", dict())?;
    let journey = if movement.is_instance_of::<PyDict>() { get_of(&get_or(&movement, "journeys", dict())?, &walk.agent_id)? } else { none() };
    let bed_index = if reservation_is_dict { get_of(&reservation, "bed_index")? } else { none() };
    let room_is_dict = room.is_instance_of::<PyDict>();
    let bed_points = || get_or(&room, "bed_points_m", PyList::empty(py).into_any());
    // `type(bed_index) is int and 0 <= bed_index < len(room.get("bed_points_m", []))`.
    let bed = if room_is_dict
        && bed_index.is_exact_instance_of::<PyInt>()
        && bed_index.ge(0)?
        && bed_index.lt(bed_points()?.len()?)?
    {
        bed_points()?.get_item(&bed_index)?
    } else {
        none()
    };
    let journey_is_dict = journey.is_instance_of::<PyDict>();
    let route_points = if journey_is_dict { get_or(&journey, "route_points", PyList::empty(py).into_any())? } else { PyList::empty(py).into_any() };
    let fits = rows_fit
        && reservation_is_dict
        && room_is_dict
        && !differs(&get_of(&room, "zone_id")?, &walk.destination)?
        && journey_is_dict
        && !differs(&get_of(&journey, "event_id")?, &walk.event_id)?
        && !differs(&get_of(&journey, "task_id")?, &walk.task_id)?
        && !differs(&get_of(&journey, "purpose")?, "sleep_bed")?
        && route_points.is_truthy()?
        && !route_points.get_item(-1)?.ne(&bed)?;
    Ok(fits.then_some(bed))
}

/// `presence_relocation`'s reads and checks, with Python's own `get` and `!=`. `rows_fit` is its
/// first two checks, the task's kind and the person's target.
fn relocation_fits(world: &Bound<'_, PyAny>, walk: &Walk, rows_fit: bool) -> PyResult<bool> {
    let py = world.py();
    let dict = || PyDict::new(py).into_any();
    let table = attribute(world, intern!(py, "model_state"))?;
    let lifecycle = get_or(&table, "population_lifecycle_v1", dict())?;
    let relocation = get_of(&get_or(&lifecycle, "pending_relocations", dict())?, &walk.agent_id)?;
    Ok(rows_fit
        && relocation.is_instance_of::<PyDict>()
        && !differs(&get_of(&relocation, "task_id")?, &walk.task_id)?
        && !differs(&get_of(&relocation, "event_id")?, &walk.event_id)?
        && !match &walk.origin {
            Origin::Zone(origin) => differs(&get_of(&relocation, "from_zone_id")?, origin)?,
            Origin::Nothing => get_of(&relocation, "from_zone_id")?.ne(py.None())?,
            Origin::NotStr => return unsupported("a relocation's origin is not a str Rust has"),
        }
        && !differs(&get_of(&relocation, "to_zone_id")?, &walk.destination)?)
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
    let _ = event;
    arrive(handler, world, exact_str(agent_id)?, exact_str(purpose)?, exact_str(facility_id)?, exact_str(destination_zone_id)?, str_value(origin_zone_id))
}

/// The attribute of an outcome `decide` raised: the rule's checks failed and nothing was written.
const UNWRITTEN: &str = "_sheltersim_travel_unwritten";

/// Whether this error is a text of the rule's checks, raised before any change. dispatch.rs shows
/// these to text_check.py; a text raised after the walk's writes (from what the walk leads to) it
/// does not.
pub(crate) fn raised_before_writing(py: Python<'_>, error: &PyErr) -> bool {
    error.value(py).hasattr(intern!(py, UNWRITTEN)).unwrap_or(false)
}

/// What a walk that fits writes, decided before any change.
struct Decided<'py> {
    walk: Walk,
    activity: Activity,
    bed: Option<Bound<'py, PyAny>>,
}

/// The rule's checks, in its order, before any change: None for a queue walk that fits (it
/// writes nothing), what to write for another walk that fits, or the rule's text.
fn decide<'py>(handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: EventRef<'_, 'py>) -> PyResult<Option<Decided<'py>>> {
    let py = world.py();
    let walk = Walk::read(event)?;
    let (agent_id, task_id, destination, purpose) = (&walk.agent_id, &walk.task_id, &walk.destination, &*walk.purpose);
    let facility_id = walk.facility_id.as_deref();

    let store = entities_of(world)?;
    // The checks before a purpose is looked at, which every walk passes.
    // Which of the rule's two checks failed: 1, the task's; 2, the person's.
    let failed = EntityStore::with_rows(&store, |rows| {
        let Some(task) = rows.active_tasks.get(task_id) else {
            return Ok(1);
        };
        if !matches!(
            &*task.kind,
            "travel_to_toilet" | "travel_to_resource" | "travel_return" | "staff_support_travel" | "toilet_queue" | "resource_service_queue"
        ) {
            return Ok(1);
        }
        let Some(agent) = rows.agents.get(agent_id) else {
            return Ok(2);
        };
        if task.agent_id.as_ref() != Some(agent_id) || agent.profile.id != *agent_id {
            return Ok(2);
        }
        Ok(0)
    })?;
    match failed {
        1 => return errors::failed(py, "travel completion does not match an active task"),
        2 => return errors::failed(py, "travel completion Agent does not match its active task"),
        _ => {}
    }
    if matches!(purpose, "toilet_queue_position" | "resource_queue_position" | "toilet_queue_service" | "resource_queue_service") {
        if !queue_walk_fits(world, event, &walk)? {
            return errors::failed(py, "physical queue travel does not match its FIFO reservation");
        }
        return Ok(None);
    }

    // `task.due_minute != event.due_minute`, then the person's activity: 1 and 2.
    let failed = EntityStore::with_rows(&store, |rows| {
        let task = rows.active_tasks.get(task_id).expect("read above");
        let agent = rows.agents.get(agent_id).expect("read above");
        Ok(if task.due_minute != walk.due_minute {
            1
        } else if agent.activity != Activity::Moving {
            2
        } else {
            0
        })
    })?;
    match failed {
        1 => return errors::failed(py, "travel completion does not match an active due task"),
        2 => return errors::failed(py, "travel completion Agent does not match its active task"),
        _ => {}
    }
    let zones = attribute(world, intern!(py, "zones"))?;
    if raw_dict_get(&zones, PyString::new(py, destination).as_any())?.is_none() {
        return errors::failed(py, format!("destination zone {} does not exist", errors::repr_str(py, destination)?));
    }

    // The task's kind and the person's target, which each purpose's checks read.
    let (task_kind, target) = EntityStore::with_rows(&store, |rows| {
        let task = rows.active_tasks.get(task_id).expect("read above");
        let agent = rows.agents.get(agent_id).expect("read above");
        Ok((task.kind.clone(), agent.target_id.clone()))
    })?;
    let at_target = |wanted: &str| target.as_deref() == Some(wanted);
    let returning = &*task_kind == "travel_return" && at_target(destination);
    let facility_of = |id: &str| -> PyResult<Option<(Arc<str>, Arc<str>)>> {
        EntityStore::with_rows(&store, |rows| Ok(rows.facilities.get(id).map(|found| (found.zone_id.clone(), found.kind.clone()))))
    };
    let mut bed = None;
    match purpose {
        "toilet_request" => {
            let Some(facility) = facility_id.filter(|id| !id.is_empty()) else {
                return errors::failed(py, "toilet travel completion requires a facility_id");
            };
            if !facility_of(facility)?.is_some_and(|(zone, _)| zone == *destination) {
                return errors::failed(py, "toilet travel destination does not match its facility");
            }
            if &*task_kind != "travel_to_toilet" || !at_target(facility) {
                return errors::failed(py, "toilet travel completion does not match its target");
            }
        }
        "resource_request" => {
            if attribute(handler, intern!(py, "resource_service"))?.is_none() {
                return errors::failed(py, "resource travel is not enabled for this scenario");
            }
            let Some(facility) = facility_id.filter(|id| !id.is_empty()) else {
                return errors::failed(py, "resource travel completion requires a facility_id");
            };
            // The origin a str and a zone, and the route a list or tuple from it to the destination.
            let route_fits = match &walk.origin {
                Origin::Zone(origin) => {
                    raw_dict_get(&zones, PyString::new(py, origin).as_any())?.is_some()
                        && matches!(&walk.route_ends, Some((Some(first), Some(last))) if first == origin && last == destination)
                }
                Origin::Nothing | Origin::NotStr => false,
            };
            let fits = facility_of(facility)?.is_some_and(|(zone, kind)| zone == *destination && route_fits && is_resource_facility(&kind))
                && &*task_kind == "travel_to_resource"
                && at_target(facility);
            if !fits {
                return errors::failed(py, "resource travel completion does not match its target facility");
            }
        }
        "return" | "queue_abandon" => {
            if !returning {
                return errors::failed(py, "return travel completion does not match its target");
            }
        }
        "staff_support_travel" => {
            let (request_id, staff_id) = (event.text("request_id")?, event.text("staff_id")?);
            let fits = EntityStore::with_rows(&store, |rows| {
                let agent = rows.agents.get(agent_id).expect("read above");
                let request = request_id.as_ref().and_then(|id| rows.support_requests.get(id));
                let staff = staff_id.as_ref().and_then(|id| rows.staff.get(id));
                Ok(match (request, staff) {
                    (Some(request), Some(staff)) => {
                        &*task_kind == "staff_support_travel"
                            && request.agent_id == *agent_id
                            && request.status == RequestStatus::Assigned
                            && request.assigned_staff_id == staff_id
                            && request.task_id.is_some()
                            && agent.support_request_id.as_ref() == Some(&request.id)
                            && staff.zone_id == *destination
                            && at_target(destination)
                    }
                    _ => false,
                })
            })?;
            if !fits {
                return errors::failed(py, "staff support travel does not match its assigned request and staff member");
            }
        }
        "sleep_bed" => match sleep_bed_fits(world, &walk, returning)? {
            Some(point) => bed = Some(point),
            None => return errors::failed(py, "sleep-bed travel does not match its live reservation"),
        },
        "presence_relocation" => {
            if !relocation_fits(world, &walk, returning)? {
                return errors::failed(py, "presence relocation completion does not match its route");
            }
        }
        _ => return errors::failed(py, format!("unsupported travel purpose {}", errors::repr_str(py, purpose)?)),
    }
    let activity = match purpose {
        "staff_support_travel" => Activity::SeekingHelp,
        "sleep_bed" => Activity::Sleeping,
        _ => Activity::Idle,
    };
    Ok(Some(Decided { walk, activity, bed }))
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
    // A read that raises in Python is the Python rule's to raise: Unsupported, with nothing written.
    let decided = match refused_by_python(py, decide(handler, world, event)) {
        Ok(Some(decided)) => decided,
        Ok(None) => return Ok(()),
        Err(error) => {
            if errors::made_here(py, &error) {
                error.value(py).setattr(intern!(py, UNWRITTEN), true)?;
            }
            return Err(error);
        }
    };
    let Decided { walk, activity, bed } = decided;
    let (agent_id, task_id, destination, purpose) = (&walk.agent_id, &walk.task_id, &walk.destination, &walk.purpose);
    let facility_id = walk.facility_id.as_ref();
    let store = entities_of(world)?;

    if &**purpose == "presence_relocation" {
        crate::rules::population_lifecycle::check_relocation_completes(world, &object()?, destination)?;
    }

    // Everything is decided, and no Unsupported comes from here on.
    if let Some(bed) = bed {
        let update = PyDict::new(py);
        update.set_item("zone_id", &**destination)?;
        update.set_item("point", bed)?;
        update.set_item("edge_id", py.None())?;
        update.set_item("progress", 1.0)?;
        update.set_item("position_kind", "physical")?;
        attribute(world, intern!(py, "model_state"))?
            .get_item("movement_v1")?
            .get_item("positions")?
            .get_item(&**agent_id)?
            .call_method1(intern!(py, "update"), (update,))?;
    }
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
    let destination_key = PyString::new(py, destination);
    raw_setattr(&person, intern!(py, "zone_id"), destination_key.as_ptr())?;
    raw_setattr(&person, intern!(py, "activity"), activity.member(py)?.as_ptr())?;
    raw_setattr(&person, intern!(py, "target_id"), py.None().as_ptr())?;

    if &**purpose == "presence_relocation" {
        crate::rules::population::writing(crate::rules::population_lifecycle::complete_presence_relocation(world, &object()?))?;
    }
    if matches!(&**purpose, "toilet_request" | "resource_request") {
        let facility = facility_id.map(|id| &**id).unwrap_or_default();
        // A resource walk got here with its origin a str; a toilet walk's `_after_arrival` doesn't
        // read it.
        let origin = walk.origin.zone();
        if direct {
            match arrive(handler, world, agent_id, purpose, facility, destination, origin) {
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
    m.add_function(wrap_pyfunction!(reject_room_approach, m)?)?;
    Ok(())
}
