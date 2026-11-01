//! The physical queues of a facility: `prepare_facility_queue` (`domain/queueing.py`) and what
//! `domain/movement.py` does for them. The Python rules stay as the comparison base and for every
//! value these kernels do not decide, which is also how a bad value gets its error text
//! (docs/rust-port/lanes/m4-queues.md).
//!
//! Each kernel is one call that plans, with nothing changed, and then writes. It reads the rows at
//! the moment of the call; the caller's loop is Python, and nobody else moves between its calls, so
//! the order of the people served is the order of those calls, as in the Python rules.
//!
//! What goes to Python, before anything is written:
//! - an independent FIFO (`queue_id`, `facility_queues_v1`), which no 7-day Run has;
//! - a person who gives up on the queue (`_abandon_and_retry`, 4 times in 7 days): that writes a
//!   return walk, and the whole call is the Python rule's then;
//! - anything not plainly what the Python rule accepts.

use std::collections::{BTreeMap, HashSet};
use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyList, PySlice, PyString};

use crate::core::mathx::{py_max, py_min};
use crate::core::row::{attribute, exact_dict, exact_int, exact_str, map_set, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::core::world_core::{field_mirror_of, raw_dict_get, NamespaceMirror};
use crate::rules::movement::{
    approach, is_queue_trip, mirror_held, mobility_text, plan_start, position_point, queue_slot, refused, room_minutes,
    write_tree, Changes, Scene, EXACT_INT, KEY, MOVEMENT_SOURCE_ID, POLICY_VERSION,
};
use crate::rules::movement_geometry::{distance, polyline_length, usable, Geo, Pt};
use crate::state::entities::{entities_of, raw_setattr, Activity, Entities, EntityStore, FacilityRow, NeedRow};
use crate::state::events::{EventKind, EventRow, EventWriter};
use crate::state::metric_state::MetricRow;
use crate::state::movement::{Journey, MovementRow, Point};
use crate::Unsupported;

const QUEUES_KEY: &str = "facility_queues_v1";
const SERVICE_KEY: &str = "resource_service_v1";
const BLOCKED_METRIC: &str = "physical_queue_route_blocked_count";
/// `NeedWeightedPhysicalQueuePolicy`: abandon limits and the need that stays in line.
const LIMIT_TOILET: i64 = 120;
const LIMIT_WATER: i64 = 240;
const LIMIT_MEAL: i64 = 360;
const PERSISTENCE_THRESHOLD: f64 = 0.8;
/// `priority`: the most a wait adds, over the minutes of a day.
const AGING_WEIGHT: f64 = 0.1;
const AGING_MINUTES: f64 = 1440.0;
/// How near a person counts as standing in their slot, in metres.
const SLOT_TOLERANCE: f64 = 0.05;

fn exact_bool(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    match value.downcast_exact::<PyBool>() {
        Ok(flag) => Ok(flag.is_true()),
        Err(_) => unsupported("not a bool"),
    }
}

/// A minute a float holds exactly.
fn plain_minute(value: &Bound<'_, PyAny>) -> PyResult<i64> {
    let minute = exact_int(value)?;
    if !(0..EXACT_INT).contains(&minute) {
        return unsupported("not a plain minute");
    }
    Ok(minute)
}

fn given_queue_id(queue_id: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
    if queue_id.is_some_and(|queue_id| !queue_id.is_none()) {
        return unsupported("an independent FIFO");
    }
    Ok(())
}

fn members(facility: &FacilityRow, resource: bool) -> &Vec<Arc<str>> {
    if resource {
        &facility.resource_queue_agent_ids
    } else {
        &facility.queue_agent_ids
    }
}

// ---- prepare_facility_queue --------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq)]
enum Service {
    Toilet,
    Water,
    Meal,
}

/// `_service_name`.
fn service_of(kind: &str) -> PyResult<Service> {
    if !kind.is_ascii() {
        return unsupported("a facility kind that casefold may change");
    }
    if kind.to_ascii_lowercase().contains("toilet") {
        Ok(Service::Toilet)
    } else if kind == "water_station" {
        Ok(Service::Water)
    } else if kind == "meal_distribution" {
        Ok(Service::Meal)
    } else {
        unsupported("not a queue service's kind")
    }
}

/// `_service_need`.
fn need_of(needs: &NeedRow, service: Service) -> PyResult<f64> {
    let value = match service {
        Service::Toilet => needs.toilet_urgency,
        Service::Water => needs.hydration_need,
        Service::Meal => needs.hunger,
    };
    if !value.is_finite() {
        return unsupported("a need is not finite");
    }
    Ok(py_min(1.0, py_max(0.0, value)))
}

/// `create_queue_policy`, for the two bundled policies.
enum Policy {
    Strict,
    NeedWeighted { limits: [i64; 3], threshold: f64 },
}

fn selected_queue_model<'py>(configs: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = configs.py();
    static SELECTED: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    let selected = SELECTED.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
        Ok(py.import("sheltersim.contracts.policies")?.getattr("selected_models")?.unbind())
    })?;
    // What it refuses, it refuses in the Python rule too.
    let selection = selected.bind(py).call1((configs,)).map_err(|_| Unsupported::new_err("the model selection refuses"))?;
    Ok(selection.downcast_exact::<PyDict>().map_err(|_| Unsupported::new_err("not a selection"))?.get_item(intern!(py, "queue"))?)
}

fn policy_of(world: &Bound<'_, PyAny>) -> PyResult<Policy> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let table = exact_dict(&configs)?;
    let need_weighted = match selected_queue_model(&configs)? {
        Some(model) => match exact_str(&model)? {
            "need-weighted-physical-queue-v1" => true,
            "strict-fifo-queue-v1" => false,
            _ => return unsupported("another queue model"),
        },
        None => table.contains(intern!(py, "facility_geometry"))?,
    };
    let raw = table.get_item(intern!(py, "facility_queues"))?;
    let Some(raw) = raw else {
        return Ok(if need_weighted {
            Policy::NeedWeighted { limits: [LIMIT_TOILET, LIMIT_WATER, LIMIT_MEAL], threshold: PERSISTENCE_THRESHOLD }
        } else {
            Policy::Strict
        });
    };
    let Some(config) = as_readable_dict(&raw) else {
        return unsupported("the queue config is not a dict");
    };
    if !need_weighted {
        return Ok(Policy::Strict);
    }
    let (mut limits, mut threshold) = ([LIMIT_TOILET, LIMIT_WATER, LIMIT_MEAL], PERSISTENCE_THRESHOLD);
    let mut retry = None;
    for (key, value) in config.iter() {
        match exact_str(&key)? {
            "schema_version" => {
                if exact_int(&value)? != 1 {
                    return unsupported("another schema version");
                }
            }
            "queue_capacity_by_facility" => {}
            "abandon_after_minutes_by_service" => {
                let Some(given) = as_readable_dict(&value) else {
                    return unsupported("the queue limits are not a dict");
                };
                for (service, limit) in given.iter() {
                    let place = match exact_str(&service)? {
                        "toilet" => 0,
                        "water" => 1,
                        "meal" => 2,
                        _ => return unsupported("a limit of another service"),
                    };
                    limits[place] = exact_int(&limit)?;
                }
            }
            "retry_delay_minutes" => retry = Some(exact_int(&value)?),
            "persistence_urgency_threshold" => {
                threshold = if let Ok(number) = value.downcast_exact::<pyo3::types::PyFloat>() {
                    number.value()
                } else {
                    match exact_int(&value)? {
                        0 => 0.0,
                        1 => 1.0,
                        _ => return unsupported("the threshold is not normalized"),
                    }
                };
            }
            _ => return unsupported("the queue config has another field"),
        }
    }
    let good_limit = |limit: &i64| (1..=10080).contains(limit);
    let good_retry = retry.is_none_or(|retry| (1..=1440).contains(&retry));
    // As written, for a NaN.
    if !limits.iter().all(good_limit) || !good_retry || !(0.0..=1.0).contains(&threshold) {
        return unsupported("the queue config is out of its bounds");
    }
    Ok(Policy::NeedWeighted { limits, threshold })
}

/// `priority`.
fn priority(policy: &Policy, need: f64, waited: i64) -> f64 {
    match policy {
        Policy::Strict => 0.0,
        Policy::NeedWeighted { .. } => need + AGING_WEIGHT * py_min(1.0, waited as f64 / AGING_MINUTES),
    }
}

/// `should_abandon`.
fn abandons(policy: &Policy, service: Service, need: f64, waited: i64) -> bool {
    match policy {
        Policy::Strict => false,
        Policy::NeedWeighted { limits, threshold } => {
            let limit = match service {
                Service::Toilet => limits[0],
                Service::Water => limits[1],
                Service::Meal => limits[2],
            };
            waited >= limit && need < *threshold
        }
    }
}

/// `movement_v1`'s queue trips to this facility, as `_queue_trips` of a facility's own queue.
fn queue_trips<'a>(row: &'a MovementRow, facility_id: &'a str) -> impl Iterator<Item = (&'a Arc<str>, &'a Journey)> {
    row.journeys
        .iter()
        .filter(move |(_, journey)| journey.facility_id.as_deref() == Some(facility_id) && is_queue_trip(&journey.purpose))
}

/// `_drop_stale_resource_stall` once the queue is in its new order: whether to delete the record,
/// and the map it is in.
fn stale_stall<'py>(
    table: &Bound<'py, PyDict>,
    facility_id: &str,
    new_order: &[Arc<str>],
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = table.py();
    let Some(state) = table.get_item(SERVICE_KEY)? else {
        return Ok(None);
    };
    let Some(state) = as_readable_dict(&state) else {
        return unsupported("the service state is not a plain dict");
    };
    let Some(stalls) = state.get_item(intern!(py, "stall_state"))? else {
        return unsupported("no stall_state");
    };
    let Some(map) = as_readable_dict(&stalls) else {
        return unsupported("the stall_state is not a dict");
    };
    let Some(record) = map.get_item(facility_id)? else {
        return Ok(None);
    };
    if record.is_none() {
        return Ok(None);
    }
    if table.contains(QUEUES_KEY)? {
        return unsupported("an independent FIFO may hold the head");
    }
    let Some(record) = as_readable_dict(&record) else {
        return unsupported("a stall record is not a dict");
    };
    let Some(agent_id) = record.get_item(intern!(py, "agent_id"))? else {
        return unsupported("a stall record has no agent");
    };
    let agent_id = exact_str(&agent_id)?;
    let is_head = new_order.first().is_some_and(|head| &**head == agent_id);
    Ok(if is_head { None } else { Some(stalls.clone()) })
}

/// `prepare_facility_queue`.
#[pyfunction]
#[pyo3(signature = (world, facility_id, *, resource, minute))]
fn prepare_facility_queue<'py>(
    world: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    resource: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let facility_name = exact_str(facility_id)?;
    let resource = exact_bool(resource)?;
    let store = entities_of(world)?;
    let is_empty = EntityStore::with_rows(&store, |rows| {
        Ok(rows.facilities.get(facility_name).is_none_or(|facility| members(facility, resource).is_empty()))
    })?;
    if is_empty {
        return Ok(());
    }
    let minute = plain_minute(minute)?;
    let policy = policy_of(world)?;
    let model_state = attribute(world, intern!(py, "model_state"))?;
    let table = exact_dict(&model_state)?;
    if table.contains(KEY)? {
        let mirror = mirror_held(world)?;
        let moving = NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| {
            Ok(queue_trips(row, facility_name).next().is_some())
        })?;
        if moving {
            return Ok(());
        }
    }
    let (current, new_order) = EntityStore::with_rows(&store, |rows| {
        let Some(facility) = rows.facilities.get(facility_name) else {
            return unsupported("no such facility");
        };
        let service = service_of(&facility.kind)?;
        let kind = if resource { "resource_service_queue" } else { "toilet_queue" };
        let mut tasks: BTreeMap<&str, i64> = BTreeMap::new();
        for (_, task) in rows.active_tasks.iter() {
            if task.resource_id.as_deref() == Some(facility_name) && &*task.kind == kind {
                let Some(agent_id) = &task.agent_id else {
                    return unsupported("a queue task has no agent");
                };
                // The last in the map's order, as a dict comprehension keeps.
                tasks.insert(agent_id, task.start_minute);
            }
        }
        let queue = members(facility, resource);
        let mut seen = HashSet::with_capacity(queue.len());
        let mut ranked = Vec::with_capacity(queue.len());
        for (order, agent_id) in queue.iter().enumerate() {
            if !seen.insert(&**agent_id) {
                return unsupported("a person stands in the queue twice");
            }
            let Some(&start) = tasks.get(&**agent_id) else {
                continue;
            };
            let waited = minute.checked_sub(start).ok_or_else(|| Unsupported::new_err("a start out of range"))?.max(0);
            if waited >= EXACT_INT {
                return unsupported("a wait too long for a float");
            }
            let Some(agent) = rows.agents.get(agent_id) else {
                return unsupported("no such agent");
            };
            let need = match policy {
                Policy::Strict => 0.0,
                Policy::NeedWeighted { .. } => need_of(&agent.needs, service)?,
            };
            if abandons(&policy, service, need, waited) {
                return unsupported("someone gives up on the queue");
            }
            ranked.push((-priority(&policy, need, waited), start, order, agent_id.clone()));
        }
        // As `sorted` on the tuples: -0.0 equals 0.0, and every value is finite.
        ranked.sort_by(|first, second| {
            let by_urgency = first.0.partial_cmp(&second.0).unwrap_or(std::cmp::Ordering::Equal);
            by_urgency.then(first.1.cmp(&second.1)).then(first.2.cmp(&second.2))
        });
        let new_order: Vec<Arc<str>> = ranked.into_iter().map(|entry| entry.3).collect();
        Ok((queue.clone(), new_order))
    })?;
    let stale = if resource { stale_stall(table, facility_name, &new_order)? } else { None };
    if new_order != current {
        let facilities = attribute(world, intern!(py, "facilities"))?;
        let Some(facility) = raw_dict_get(&facilities, facility_id)? else {
            return unsupported("the rows' facility has no object");
        };
        let name = if resource { intern!(py, "resource_queue_agent_ids") } else { intern!(py, "queue_agent_ids") };
        let list = attribute(&facility, name)?;
        let order = PyList::new(py, new_order.iter().map(|id| &**id))?;
        // `queue[:] = ...`, through the list's own method, which keeps the row.
        list.set_item(PySlice::full(py), order)?;
    }
    if let Some(stalls) = stale {
        // Through the map's own method: the map may be a tree's.
        stalls.del_item(facility_id)?;
    }
    Ok(())
}

// ---- progress_physical_queue_positions and _head_to_service --------------------------------------

/// A route to walk: `_schedule_queue_trip`'s arguments.
struct Route {
    agent_id: Arc<str>,
    index: usize,
    minutes: i64,
    path: Arc<[Pt]>,
}

/// What a kernel found to write.
enum Plan {
    Nothing,
    Blocked,
    /// `_schedule_queue_trip` of a route that really walks.
    Trip(Box<Trip>),
}

struct Trip {
    agent_id: Arc<str>,
    event: EventRow,
    changes: Changes,
}

fn text(value: &str) -> V {
    V::Str(Arc::from(value))
}

/// `_schedule_queue_trip`, changing nothing.
#[allow(clippy::too_many_arguments)]
fn plan_trip(
    scene: &Scene<'_>,
    writer: &EventWriter<'_>,
    row: &MovementRow,
    rows: &Entities,
    facility_id: &str,
    resource: bool,
    route: Route,
    minute: i64,
    phase: &'static str,
) -> PyResult<Plan> {
    let due = minute.checked_add(route.minutes).ok_or_else(|| Unsupported::new_err("a due minute out of range"))?;
    if route.minutes <= 0 || due >= writer.duration() {
        return Ok(Plan::Blocked);
    }
    if route.minutes >= EXACT_INT {
        return unsupported("a journey has no plain duration");
    }
    let Some(facility) = rows.facilities.get(facility_id) else {
        return unsupported("no such facility");
    };
    if facility_id.is_empty() {
        return unsupported("a facility with an empty ID");
    }
    let task_kind = if resource { "resource_service_queue" } else { "toilet_queue" };
    let Some((_, task)) = rows.active_tasks.iter().find(|(_, task)| {
        task.agent_id.as_deref() == Some(&*route.agent_id)
            && task.resource_id.as_deref() == Some(facility_id)
            && &*task.kind == task_kind
    }) else {
        return unsupported("a queue reservation has no matching task");
    };
    if route.path.iter().any(|point| !usable(point.x, point.y)) {
        return unsupported("a route point is not a usable point");
    }
    let sequence = writer.next_sequence()?;
    let event_id: Arc<str> = Arc::from(format!("physical-queue-trip-{sequence}"));
    let purpose: Arc<str> = Arc::from(format!("{}_queue_{phase}", if resource { "resource" } else { "toilet" }));
    let zone = facility.zone_id.clone();
    let zone_ids = vec![zone.clone(), zone.clone()];
    let mut payload = IndexMap::with_capacity(10);
    payload.insert(Arc::from("agent_id"), V::Str(route.agent_id.clone()));
    payload.insert(Arc::from("task_id"), V::Str(task.id.clone()));
    payload.insert(Arc::from("purpose"), V::Str(purpose.clone()));
    payload.insert(Arc::from("destination_zone_id"), V::Str(zone.clone()));
    payload.insert(Arc::from("facility_id"), text(facility_id));
    payload.insert(Arc::from("origin_zone_id"), V::Str(zone.clone()));
    payload.insert(Arc::from("route_zone_ids"), V::List(vec![V::Str(zone.clone()), V::Str(zone.clone())]));
    payload.insert(Arc::from("route_edge_ids"), V::List(Vec::new()));
    payload.insert(Arc::from("travel_minutes"), V::Int(route.minutes));
    payload.insert(Arc::from("start_minute"), V::Int(minute));
    let mut provenance = IndexMap::with_capacity(6);
    provenance.insert(Arc::from("source_type"), text("assumption"));
    provenance.insert(Arc::from("source_id"), text(MOVEMENT_SOURCE_ID));
    provenance.insert(Arc::from("policy_version"), text(POLICY_VERSION));
    provenance.insert(Arc::from("queue_phase"), text(phase));
    provenance.insert(Arc::from("queue_requested_at"), V::Int(task.start_minute));
    provenance.insert(Arc::from("fifo_position"), V::Int(route.index as i64));
    let Some(event) = EventRow::new(
        event_id.clone(),
        EventKind::Completion,
        due,
        0,
        sequence,
        Arc::from("agent_travel_complete"),
        payload,
        Some(task.id.clone()),
        Some(task.id.clone()),
        provenance,
    ) else {
        return unsupported("the event's values are not what an event takes");
    };
    let writes_past = writer.log().borrow();
    if writes_past.has_event_id(&event_id) || writes_past.has_sequence(sequence) || writer.queue().borrow().has_event(&event_id) {
        return unsupported("the event's identity is taken");
    }
    drop(writes_past);
    let journey = Journey {
        event_id,
        task_id: task.id.clone(),
        purpose,
        facility_id: Some(Arc::from(facility_id)),
        destination_zone_id: zone.clone(),
        route_zone_ids: zone_ids,
        route_edge_ids: Vec::new(),
        route_points: route.path.iter().map(|point| Point { x: point.x, y: point.y }).collect(),
        started_minute: minute,
        due_minute: due,
        queue_id: None,
        support_request_id: None,
        support_staff_id: None,
    };
    let changes = plan_start(scene, row, rows, &route.agent_id, journey, &zone, &route.path, &[], minute, route.minutes)?;
    Ok(Plan::Trip(Box::new(Trip { agent_id: route.agent_id, event, changes })))
}

/// `world.metric_state[BLOCKED_METRIC] += 1.0`, and what a plan needs of it before it writes anything.
fn add_blocked(world: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let metrics = field_mirror_of(world, intern!(py, "metric_state"))?;
    NamespaceMirror::with_row_tree::<MetricRow, _>(&metrics, |row, tree| {
        let count = row.get(BLOCKED_METRIC).copied().unwrap_or(0.0) + 1.0;
        map_set(row, tree, BLOCKED_METRIC, count)
    })
}

fn write_plan<'py>(
    world: &Bound<'py, PyAny>,
    mirror: &Bound<'py, NamespaceMirror>,
    store: &Bound<'py, EntityStore>,
    writer: &EventWriter<'py>,
    facility_id: &Bound<'py, PyAny>,
    plan: Plan,
) -> PyResult<()> {
    let py = world.py();
    let trip = match plan {
        Plan::Nothing => return Ok(()),
        Plan::Blocked => return add_blocked(world),
        Plan::Trip(trip) => trip,
    };
    let facility_name = exact_str(facility_id)?;
    let Trip { agent_id, event, changes } = *trip;
    let (activity_changed, target_changed) = EntityStore::with_rows(store, |rows| {
        let Some(agent) = rows.agents.get(&agent_id) else {
            return unsupported("no such agent");
        };
        let changed = (agent.activity != Activity::Queued, agent.target_id.as_deref() != Some(facility_name));
        if changed.0 || changed.1 {
            if let Some(agent) = rows.agents.get_mut(&agent_id) {
                agent.activity = Activity::Queued;
                agent.target_id = Some(Arc::from(facility_name));
            }
        }
        Ok(changed)
    })?;
    if activity_changed || target_changed {
        let agents = attribute(world, intern!(py, "agents"))?;
        let Some(agent) = raw_dict_get(&agents, PyString::new(py, &agent_id).as_any())? else {
            return Err(pyo3::exceptions::PyRuntimeError::new_err("a row has no object"));
        };
        if activity_changed {
            raw_setattr(&agent, intern!(py, "activity"), Activity::Queued.member(py)?.as_ptr())?;
        }
        if target_changed {
            raw_setattr(&agent, intern!(py, "target_id"), facility_id.as_ptr())?;
        }
    }
    writer.allocate_sequence()?;
    writer.schedule(event)?;
    let writes = NamespaceMirror::with_row_tree::<MovementRow, _>(mirror, |row, tree| changes.apply(row, tree))?;
    write_tree(mirror, writes)
}

fn no_geometry(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    let configs = attribute(world, intern!(py, "model_configs"))?;
    Ok(!exact_dict(&table)?.contains(KEY)? || !exact_dict(&configs)?.contains(intern!(py, "facility_geometry"))?)
}

fn raw_movement_model<'py>(world: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    exact_dict(&configs)?.get_item(intern!(py, "movement_model"))
}

/// `facility_queue_slot_route`: the route's minutes and its path, or None for no route.
fn slot_route(
    geo: &Geo,
    row: &MovementRow,
    rows: &Entities,
    raw_model: Option<Bound<'_, PyAny>>,
    agent_id: &str,
    facility: &FacilityRow,
    facility_id: &str,
) -> PyResult<Option<(i64, Arc<[Pt]>)>> {
    let Some(destination) = queue_slot(geo, row, rows, agent_id, facility_id)? else {
        return Ok(None);
    };
    let Some(agent) = rows.agents.get(agent_id) else {
        return Ok(None);
    };
    let start = position_point(row.positions.get(agent_id));
    if start.is_some_and(|start| !usable(start.x, start.y)) {
        return unsupported("a position is not a usable point");
    }
    let mobility = mobility_text(agent);
    let Some(path) = geo.room_route(&facility.zone_id, start, destination, mobility).map_err(refused)? else {
        return Ok(None);
    };
    let mut minutes = 0;
    if path.len() >= 2 {
        let length = polyline_length(&path).ok_or_else(|| Unsupported::new_err("not a plain sum"))?;
        minutes = room_minutes(raw_model, geo, rows, &agent.zone_id, mobility, length)?;
    }
    if path.len() < 2 || path.windows(2).all(|pair| distance(pair[0], pair[1]) <= 1e-8) {
        minutes = 0;
    }
    Ok(Some((minutes, path)))
}

/// `progress_physical_queue_positions`, for a facility's own queue.
#[pyfunction]
#[pyo3(signature = (world, facility_id, *, resource, minute, queue_id=None))]
fn progress_physical_queue_positions<'py>(
    world: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    resource: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    queue_id: Option<&Bound<'py, PyAny>>,
) -> PyResult<bool> {
    given_queue_id(queue_id)?;
    if no_geometry(world)? {
        return Ok(false);
    }
    let facility_name = exact_str(facility_id)?;
    let (resource, minute) = (exact_bool(resource)?, plain_minute(minute)?);
    let store = entities_of(world)?;
    let geo = Geo::of(world)?;
    if geo.layout.is_none() {
        return unsupported("no floor plan");
    }
    let mirror = mirror_held(world)?;
    let writer = EventWriter::of(world)?;
    let scene = Scene::of(world)?;
    let metrics = field_mirror_of(world, intern!(world.py(), "metric_state"))?;
    NamespaceMirror::with_row::<MetricRow, _>(&metrics, |_| Ok(()))?;
    let raw_model = raw_movement_model(world)?;
    let (result, plan) = NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| {
        EntityStore::with_rows(&store, |rows| {
            let Some(facility) = rows.facilities.get(facility_name) else {
                return Ok((true, Plan::Nothing));
            };
            let queue = members(facility, resource);
            let head = queue.first();
            let head_walks = |row: &MovementRow| {
                head.is_some_and(|head| queue_trips(row, facility_name).any(|(id, _)| id == head))
            };
            if queue_trips(row, facility_name).any(|(_, journey)| journey.purpose.ends_with("_position")) {
                return Ok((head_walks(row), Plan::Nothing));
            }
            for (index, agent_id) in queue.iter().enumerate() {
                if index == 0 {
                    if head_walks(row) {
                        continue;
                    }
                    let route = approach(&geo, row, rows, raw_model.clone(), agent_id, facility_name)?;
                    if route.is_some_and(|(minutes, path)| minutes <= 0 || path.len() < 2) {
                        continue;
                    }
                }
                let target = queue_slot(&geo, row, rows, agent_id, facility_name)?;
                let current = position_point(row.positions.get(&**agent_id));
                if current.is_some_and(|point| !usable(point.x, point.y)) {
                    return unsupported("a position is not a usable point");
                }
                if let (Some(target), Some(current)) = (target, current) {
                    if distance(target, current) <= SLOT_TOLERANCE {
                        continue;
                    }
                }
                let route = slot_route(&geo, row, rows, raw_model.clone(), agent_id, facility, facility_name)?;
                let Some((minutes, path)) = route.filter(|(_, path)| !path.is_empty()) else {
                    if index > 0 {
                        return Ok((false, Plan::Blocked));
                    }
                    return Ok((true, Plan::Blocked));
                };
                // `list.index`, which a person listed twice would make another place.
                let place = queue.iter().position(|other| other == agent_id).unwrap_or(index);
                let route = Route { agent_id: agent_id.clone(), index: place, minutes, path };
                let plan = plan_trip(&scene, &writer, row, rows, facility_name, resource, route, minute, "position")?;
                return Ok((index == 0, plan));
            }
            Ok((false, Plan::Nothing))
        })
    })?;
    write_plan(world, &mirror, &store, &writer, facility_id, plan)?;
    Ok(result)
}

/// `progress_physical_queue_head_to_service`, for a facility's own queue.
#[pyfunction]
#[pyo3(signature = (world, facility_id, *, resource, minute, queue_id=None))]
fn progress_physical_queue_head_to_service<'py>(
    world: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    resource: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    queue_id: Option<&Bound<'py, PyAny>>,
) -> PyResult<bool> {
    given_queue_id(queue_id)?;
    head_to_service(world, facility_id, exact_bool(resource)?, plain_minute(minute)?)
}

/// That rule: false has written nothing, and Unsupported changes nothing.
pub(crate) fn head_to_service<'py>(
    world: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    resource: bool,
    minute: i64,
) -> PyResult<bool> {
    if no_geometry(world)? {
        return Ok(false);
    }
    let facility_name = exact_str(facility_id)?;
    let store = entities_of(world)?;
    let geo = Geo::of(world)?;
    if geo.layout.is_none() {
        return unsupported("no floor plan");
    }
    let mirror = mirror_held(world)?;
    let writer = EventWriter::of(world)?;
    let scene = Scene::of(world)?;
    let metrics = field_mirror_of(world, intern!(world.py(), "metric_state"))?;
    NamespaceMirror::with_row::<MetricRow, _>(&metrics, |_| Ok(()))?;
    let raw_model = raw_movement_model(world)?;
    let (result, plan) = NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| {
        EntityStore::with_rows(&store, |rows| {
            let Some(facility) = rows.facilities.get(facility_name) else {
                return Ok((true, Plan::Nothing));
            };
            let Some(agent_id) = members(facility, resource).first() else {
                return Ok((false, Plan::Nothing));
            };
            if queue_trips(row, facility_name).any(|(id, _)| id == agent_id) {
                return Ok((true, Plan::Nothing));
            }
            let Some((minutes, path)) = approach(&geo, row, rows, raw_model, agent_id, facility_name)? else {
                return Ok((true, Plan::Blocked));
            };
            if minutes <= 0 || path.len() < 2 {
                return Ok((false, Plan::Nothing));
            }
            let route = Route { agent_id: agent_id.clone(), index: 0, minutes, path };
            Ok((true, plan_trip(&scene, &writer, row, rows, facility_name, resource, route, minute, "service")?))
        })
    })?;
    write_plan(world, &mirror, &store, &writer, facility_id, plan)?;
    Ok(result)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(prepare_facility_queue, m)?)?;
    m.add_function(wrap_pyfunction!(progress_physical_queue_positions, m)?)?;
    m.add_function(wrap_pyfunction!(progress_physical_queue_head_to_service, m)?)?;
    Ok(())
}
