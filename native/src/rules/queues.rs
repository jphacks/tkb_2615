//! The physical queues of a facility: `prepare_facility_queue` (`domain/queueing.py`) and what
//! `domain/movement.py` does for them. The Python rules stay as the comparison base and for every
//! value these kernels do not decide, which is also how a bad value gets its error text
//! (docs/rust-port/lanes/m4-queues.md).
//!
//! Each kernel is one call that plans, with nothing changed, and then writes. It reads the rows at
//! the moment of the call; the caller's loop is Python, and nobody else moves between its calls, so
//! the order of the people served is the order of those calls, as in the Python rules.
//!
//! What goes to Python, before anything is written: anything not plainly what the Python rule accepts.

use std::collections::{BTreeMap, HashSet};
use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyList, PySlice, PyString};

use crate::core::errors;
use crate::core::mathx::{py_max, py_min};
use crate::core::row::{attribute, exact_dict, exact_int, exact_str, map_set, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::core::world_core::{field_mirror_of, raw_dict_get, NamespaceMirror};
use crate::rules::facility_queues::{
    independent_heads, point_of, request_of_task, transition_row, with_queues, write_finish, write_transition,
};
use crate::rules::movement::{
    approach_point, is_queue_trip, mirror_held, mobility_text, plan_start, position_point, queue_slot_in, refused, room_minutes,
    write_tree, Changes, Scene, EXACT_INT, KEY, MOVEMENT_SOURCE_ID, POLICY_VERSION,
};
use crate::rules::movement_geometry::{distance, polyline_length, usable, Geo, Pt};
use crate::rules::population::writing;
use crate::rules::service::{active_task_class, add_one, metrics_of, set_agent};
use crate::state::entities::{entities_of, raw_setattr, Activity, Entities, EntityStore, FacilityRow, NeedRow};
use crate::state::events::{EventKind, EventRow, EventStatus, EventWriter};
use crate::state::facility_queues::QueueRequest;
use crate::state::metric_state::MetricRow;
use crate::state::movement::{Journey, MovementRow, Point};
use crate::Unsupported;

const SERVICE_KEY: &str = "resource_service_v1";
pub(crate) const BLOCKED_METRIC: &str = "physical_queue_route_blocked_count";
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
/// `NeedWeightedPhysicalQueuePolicy.model_id`, `source_id`, and its `retry_delay_minutes` by default.
const NEED_WEIGHTED_ID: &str = "need-weighted-physical-queue-v1";
const NEED_WEIGHTED_SOURCE: &str = "ASM-NEED-WEIGHTED-PHYSICAL-QUEUE-V1";
const RETRY_DELAY: i64 = 15;
const ABANDON_DETAIL: &str = "Agent left a prolonged low-urgency queue and will reconsider after a bounded delay";

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

/// The independent FIFO a call is about, read when the call starts.
pub(crate) struct Fifo {
    queue_id: Arc<str>,
    service_point_id: Arc<str>,
    members: Vec<Arc<str>>,
    signed_point: Option<Pt>,
    requests: Vec<QueueRequest>,
    queued_request_ids: Vec<Arc<str>>,
}

impl Fifo {
    /// None for a world without the namespace. A queue it doesn't have is the Python rule's error.
    pub(crate) fn read(world: &Bound<'_, PyAny>, geo: &Geo, queue_id: &str) -> PyResult<Option<Self>> {
        let signed_point = point_of(geo, queue_id).map(|point| point.point);
        with_queues(world, |row| {
            let Some(queue) = row.queues.get(queue_id) else {
                return unsupported("no such queue");
            };
            let members = crate::rules::facility_queues::members_of(row, queue_id)?;
            Ok(Self {
                queue_id: Arc::from(queue_id),
                service_point_id: queue.service_point_id.clone(),
                members,
                signed_point,
                requests: row.requests.values().filter(|request| &*request.queue_id == queue_id).cloned().collect(),
                queued_request_ids: queue.queued_request_ids.clone(),
            })
        })
    }

    /// `request_for_agent` of this queue.
    pub(crate) fn request_of(&self, agent_id: &str) -> Option<&QueueRequest> {
        self.requests.iter().find(|request| &*request.agent_id == agent_id)
    }

    pub(crate) fn slot_view(&self) -> (&[Arc<str>], Option<Pt>) {
        (&self.members, self.signed_point)
    }
}

/// The FIFO of a kernel's `queue_id`. A queue ID without the namespace goes to Python.
fn fifo_of(world: &Bound<'_, PyAny>, geo: &Geo, queue_id: Option<&Bound<'_, PyAny>>) -> PyResult<Option<Fifo>> {
    let Some(queue_id) = queue_id.filter(|queue_id| !queue_id.is_none()) else {
        return Ok(None);
    };
    match Fifo::read(world, geo, exact_str(queue_id)?)? {
        Some(fifo) => Ok(Some(fifo)),
        None => unsupported("a queue ID without the queues"),
    }
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
    NeedWeighted { limits: [i64; 3], threshold: f64, retry: i64 },
}

fn selected_queue_model<'py>(configs: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = configs.py();
    static SELECTED: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    let selected = SELECTED.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
        Ok(py.import("sheltersim.contracts.policies")?.getattr("selected_models")?.unbind())
    })?;
    // What it refuses, it refuses in the Python rule too, with the same error.
    let selection = match selected.bind(py).call1((configs,)) {
        Ok(selection) => selection,
        Err(error) => return Err(errors::relayed(py, error)),
    };
    Ok(selection.downcast_exact::<PyDict>().map_err(|_| Unsupported::new_err("not a selection"))?.get_item(intern!(py, "queue"))?)
}

/// `raise ValueError(text)` of the queue policy's constructor.
#[track_caller]
fn config_error<T>(text: &str) -> PyResult<T> {
    Err(errors::value_error(text))
}

/// The checks of `NeedWeightedPhysicalQueuePolicy.__init__` (or of the strict policy), in their
/// order: the Python rule's ValueError where one fails,
/// Unsupported where a value's type is one the rows don't judge as Python does.
fn config_errors(raw: &Bound<'_, PyAny>, need_weighted: bool) -> PyResult<()> {
    let py = raw.py();
    if raw.is_none() {
        return Ok(());
    }
    if !raw.is_instance_of::<PyDict>() {
        return config_error("facility_queues config must be an object");
    }
    if !need_weighted {
        return Ok(());
    }
    let config = as_readable_dict(raw).ok_or_else(|| Unsupported::new_err("the queue config is another dict"))?;
    const FIELDS: [&str; 5] = [
        "schema_version",
        "queue_capacity_by_facility",
        "abandon_after_minutes_by_service",
        "retry_delay_minutes",
        "persistence_urgency_threshold",
    ];
    for (key, _) in config.iter() {
        if !key.downcast_exact::<PyString>().is_ok_and(|key| key.to_str().is_ok_and(|key| FIELDS.contains(&key))) {
            exact_str(&key)?;
            return config_error("facility_queues config must be an object");
        }
    }
    if let Some(schema) = config.get_item(intern!(py, "schema_version"))? {
        // `1.0` and `True` pass the Python rule: for it to decide.
        let Some(schema) = errors::exactly_int(&schema)? else {
            return unsupported("another schema version");
        };
        if schema != 1 {
            return config_error("facility_queues requires schema_version 1");
        }
    }
    let mut limits: Vec<Bound<'_, PyAny>> = Vec::new();
    if let Some(given) = config.get_item(intern!(py, "abandon_after_minutes_by_service"))? {
        if !given.is_instance_of::<PyDict>() {
            return config_error("queue abandonment limits contain an unsupported service");
        }
        let given = as_readable_dict(&given).ok_or_else(|| Unsupported::new_err("the queue limits are another dict"))?;
        for (service, limit) in given.iter() {
            if !service.downcast_exact::<PyString>().is_ok_and(|key| key.to_str().is_ok_and(|key| matches!(key, "toilet" | "water" | "meal"))) {
                exact_str(&service)?;
                return config_error("queue abandonment limits contain an unsupported service");
            }
            limits.push(limit);
        }
    }
    let within = |value: &Bound<'_, PyAny>, upper: i64| -> PyResult<bool> { Ok(errors::exactly_int(value)?.is_some_and(|value| (1..=upper).contains(&value))) };
    for limit in &limits {
        if !within(limit, 10080)? {
            return config_error("queue abandonment limits must be within 1..10080 minutes");
        }
    }
    if let Some(retry) = config.get_item(intern!(py, "retry_delay_minutes"))? {
        if !within(&retry, 1440)? {
            return config_error("queue retry delay must be between 1 and 1440 minutes");
        }
    }
    if let Some(threshold) = config.get_item(intern!(py, "persistence_urgency_threshold"))? {
        if !errors::real_number(&threshold)?.is_some_and(|value| (0.0..=1.0).contains(&value)) {
            return config_error("queue persistence threshold must be normalized to 0..1");
        }
    }
    Ok(())
}

fn policy_of(world: &Bound<'_, PyAny>) -> PyResult<Policy> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let table = exact_dict(&configs)?;
    let selected = selected_queue_model(&configs)?;
    let need_weighted = match &selected {
        Some(model) => match exact_str(model)? {
            "need-weighted-physical-queue-v1" => true,
            "strict-fifo-queue-v1" => false,
            _ => return unsupported("another queue model"),
        },
        None => table.contains(intern!(py, "facility_geometry"))?,
    };
    let raw = table.get_item(intern!(py, "facility_queues"))?;
    // The bundled factories build the same two classes from the same config.
    if let Some(raw) = &raw {
        config_errors(raw, need_weighted)?;
    }
    let Some(raw) = raw else {
        return Ok(if need_weighted {
            Policy::NeedWeighted { limits: [LIMIT_TOILET, LIMIT_WATER, LIMIT_MEAL], threshold: PERSISTENCE_THRESHOLD, retry: RETRY_DELAY }
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
    Ok(Policy::NeedWeighted { limits, threshold, retry: retry.unwrap_or(RETRY_DELAY) })
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
        Policy::NeedWeighted { limits, threshold, .. } => {
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
    fifo_trips(row, facility_id, None)
}

/// `_queue_trips`.
fn fifo_trips<'a>(row: &'a MovementRow, facility_id: &'a str, queue_id: Option<&'a str>) -> impl Iterator<Item = (&'a Arc<str>, &'a Journey)> {
    row.journeys.iter().filter(move |(_, journey)| {
        journey.facility_id.as_deref() == Some(facility_id)
            && queue_id.is_none_or(|queue| journey.queue_id.as_deref() == Some(queue))
            && is_queue_trip(&journey.purpose)
    })
}

/// `_drop_stale_resource_stall` once the queue is in its new order: whether to delete the record,
/// and the map it is in.
fn stale_stall<'py>(
    world: &Bound<'py, PyAny>,
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
    let Some(record) = as_readable_dict(&record) else {
        return unsupported("a stall record is not a dict");
    };
    let Some(agent_id) = record.get_item(intern!(py, "agent_id"))? else {
        return unsupported("a stall record has no agent");
    };
    let agent_id = exact_str(&agent_id)?;
    let is_head = match independent_heads(world, facility_id)? {
        Some(heads) => heads.iter().any(|head| &**head == agent_id),
        None => new_order.first().is_some_and(|head| &**head == agent_id),
    };
    Ok(if is_head { None } else { Some(stalls.clone()) })
}

/// `prepare_facility_queue`.
#[pyfunction]
#[pyo3(signature = (world, facility_id, *, resource, minute))]
pub(crate) fn prepare_facility_queue<'py>(
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
    let (current, new_order, leaving, service) = EntityStore::with_rows(&store, |rows| {
        let Some(facility) = rows.facilities.get(facility_name) else {
            return unsupported("no such facility");
        };
        let service = service_of(&facility.kind)?;
        let kind = if resource { "resource_service_queue" } else { "toilet_queue" };
        let mut tasks: BTreeMap<&str, (i64, &Arc<str>)> = BTreeMap::new();
        for (_, task) in rows.active_tasks.iter() {
            if task.resource_id.as_deref() == Some(facility_name) && &*task.kind == kind {
                let Some(agent_id) = &task.agent_id else {
                    return unsupported("a queue task has no agent");
                };
                // The last in the map's order, as a dict comprehension keeps.
                tasks.insert(agent_id, (task.start_minute, &task.id));
            }
        }
        let queue = members(facility, resource);
        let mut seen = HashSet::with_capacity(queue.len());
        let mut ranked = Vec::with_capacity(queue.len());
        for (order, agent_id) in queue.iter().enumerate() {
            if !seen.insert(&**agent_id) {
                return unsupported("a person stands in the queue twice");
            }
            let Some(&(start, task_id)) = tasks.get(&**agent_id) else {
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
            let leaves = abandons(&policy, service, need, waited).then(|| Leaving { agent_id: agent_id.clone(), task_id: task_id.clone(), start });
            ranked.push((-priority(&policy, need, waited), start, order, agent_id.clone(), leaves));
        }
        // As `sorted` on the tuples: -0.0 equals 0.0, and every value is finite.
        ranked.sort_by(|first, second| {
            let by_urgency = first.0.partial_cmp(&second.0).unwrap_or(std::cmp::Ordering::Equal);
            by_urgency.then(first.1.cmp(&second.1)).then(first.2.cmp(&second.2))
        });
        let mut new_order = Vec::with_capacity(ranked.len());
        let mut leaving = Vec::new();
        for (_, _, _, agent_id, leaves) in ranked {
            new_order.push(agent_id);
            leaving.extend(leaves);
        }
        Ok((queue.clone(), new_order, leaving, service))
    })?;
    let leave = LeaveCall { world, facility_id, facility_name, resource, minute, service, retry: policy_retry(&policy), queue_id: None };
    plan_all_leaving(&leave, &leaving)?;
    let stale = if resource { stale_stall(world, table, facility_name, &new_order)? } else { None };
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
    writing(write_all_leaving(&leave, &leaving))
}

/// `prepare_independent_facility_queue`: an independent FIFO keeps its order, and only those who
/// give up change anything.
#[pyfunction]
#[pyo3(signature = (world, queue_id, *, minute, resource))]
pub(crate) fn prepare_independent_facility_queue<'py>(
    world: &Bound<'py, PyAny>,
    queue_id: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    resource: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let queue_name = exact_str(queue_id)?;
    let resource = exact_bool(resource)?;
    let minute = plain_minute(minute)?;
    let found = with_queues(world, |row| {
        let Some(queue) = row.queues.get(queue_name) else {
            return Ok(None);
        };
        let requests = queue
            .queued_request_ids
            .iter()
            .filter_map(|id| row.requests.get(&**id))
            .map(|request| (request.agent_id.clone(), request.queue_task_id.clone()))
            .collect::<Vec<_>>();
        Ok(Some((queue.facility_id.clone(), requests)))
    })?;
    let Some((facility_id, requests)) = found.flatten() else {
        return Ok(());
    };
    let model_state = attribute(world, intern!(py, "model_state"))?;
    if exact_dict(&model_state)?.contains(KEY)? {
        let mirror = mirror_held(world)?;
        let moving = NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| {
            Ok(row.journeys.values().any(|journey| journey.queue_id.as_deref() == Some(queue_name) && is_queue_trip(&journey.purpose)))
        })?;
        if moving {
            return Ok(());
        }
    }
    if requests.is_empty() {
        return Ok(());
    }
    let policy = policy_of(world)?;
    let store = entities_of(world)?;
    let (leaving, service) = EntityStore::with_rows(&store, |rows| {
        let Some(facility) = rows.facilities.get(&*facility_id) else {
            return unsupported("no such facility");
        };
        let service = service_of(&facility.kind)?;
        let mut leaving = Vec::new();
        for (agent_id, task_id) in &requests {
            let Some(task) = rows.active_tasks.get(task_id) else {
                continue;
            };
            let waited = minute.checked_sub(task.start_minute).ok_or_else(|| Unsupported::new_err("a start out of range"))?.max(0);
            let need = match policy {
                Policy::Strict => 0.0,
                Policy::NeedWeighted { .. } => match rows.agents.get(agent_id) {
                    Some(agent) => need_of(&agent.needs, service)?,
                    None => return unsupported("no such agent"),
                },
            };
            // `_abandon_and_retry`'s first check: someone not in the facility's list stays as they are.
            if abandons(&policy, service, need, waited) && members(facility, resource).contains(agent_id) {
                leaving.push(Leaving { agent_id: agent_id.clone(), task_id: task.id.clone(), start: task.start_minute });
            }
        }
        Ok((leaving, service))
    })?;
    if leaving.is_empty() {
        return Ok(());
    }
    let facility_object = PyString::new(py, &facility_id);
    let leave = LeaveCall {
        world,
        facility_id: facility_object.as_any(),
        facility_name: &facility_id,
        resource,
        minute,
        service,
        retry: policy_retry(&policy),
        queue_id: Some(queue_name),
    };
    plan_all_leaving(&leave, &leaving)?;
    writing(write_all_leaving(&leave, &leaving))
}

// ---- _abandon_and_retry --------------------------------------------------------------------------

/// Someone `should_abandon` picked, in the order the rule goes through them.
struct Leaving {
    agent_id: Arc<str>,
    task_id: Arc<str>,
    start: i64,
}

/// What a call of `_abandon_and_retry` has in common for those who leave.
struct LeaveCall<'a, 'py> {
    world: &'a Bound<'py, PyAny>,
    facility_id: &'a Bound<'py, PyAny>,
    facility_name: &'a str,
    resource: bool,
    minute: i64,
    service: Service,
    retry: i64,
    queue_id: Option<&'a str>,
}

/// What `_abandon_and_retry` writes for a person, with the sequences it takes from `sequence`.
struct LeavePlan {
    abandon: EventRow,
    /// `finish_service`'s request and `record_queue_transition`'s record.
    transition: Option<(QueueRequest, EventRow)>,
    walk: Option<LeaveWalk>,
    sequences: i64,
}

struct LeaveWalk {
    task_id: Arc<str>,
    destination: Arc<str>,
    due: i64,
    event: EventRow,
    changes: Changes,
}

fn policy_retry(policy: &Policy) -> i64 {
    match policy {
        Policy::Strict => RETRY_DELAY,
        Policy::NeedWeighted { retry, .. } => *retry,
    }
}

impl Service {
    fn name(self) -> &'static str {
        match self {
            Service::Toilet => "toilet",
            Service::Water => "water",
            Service::Meal => "meal",
        }
    }
}

/// The walk back to the middle of the zone: None to leave at once, `Some(None)` when no path lets
/// the person out, so the rule leaves them in line.
fn return_path(geo: &Geo, movement: &MovementRow, rows: &Entities, raw_model: Option<Bound<'_, PyAny>>, agent_id: &str) -> PyResult<Option<Option<(i64, Arc<[Pt]>)>>> {
    let Some(agent) = rows.agents.get(agent_id) else {
        return unsupported("no such agent");
    };
    let current = position_point(movement.positions.get(agent_id));
    let destination = geo.zone_center(&agent.zone_id)?;
    let (Some(current), Some(destination)) = (current, destination) else {
        return Ok(None);
    };
    if !usable(current.x, current.y) {
        return unsupported("a position is not a usable point");
    }
    let mobility = mobility_text(agent);
    let Some(path) = geo.room_route(&agent.zone_id, Some(current), destination, mobility).map_err(refused)? else {
        return Ok(Some(None));
    };
    let mut minutes = 0;
    if path.len() >= 2 {
        let length = polyline_length(&path).ok_or_else(|| Unsupported::new_err("not a plain sum"))?;
        minutes = room_minutes(raw_model, geo, rows, &agent.zone_id, mobility, length)?;
    }
    Ok(Some(Some((minutes, path))))
}

fn identity_is_free(writer: &EventWriter<'_>, event_id: &str, sequence: i64) -> bool {
    let past = writer.log().borrow();
    !(past.has_event_id(event_id) || past.has_sequence(sequence) || writer.queue().borrow().has_event(event_id))
}

/// `_abandon_and_retry` of one person, changing nothing. None when the rule returns at once and the
/// person stays in line. Whether someone leaves doesn't turn on who left before: the path and the
/// crowd of the zone stay what they were.
fn plan_leave(call: &LeaveCall<'_, '_>, person: &Leaving, sequence: i64) -> PyResult<Option<LeavePlan>> {
    let (world, minute) = (call.world, call.minute);
    let py = world.py();
    let writer = EventWriter::of(world)?;
    let store = entities_of(world)?;
    let table = attribute(world, intern!(py, "model_state"))?;
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let placed = exact_dict(&table)?.get_item(KEY)?.is_some_and(|state| !state.is_none())
        && exact_dict(&configs)?.contains(intern!(py, "facility_geometry"))?;
    let geo = if placed { Some(Geo::of(world)?) } else { None };
    let mirror = if placed { Some(mirror_held(world)?) } else { None };
    let raw_model = raw_movement_model(world)?;
    let path = match (&geo, &mirror) {
        (Some(geo), Some(mirror)) => NamespaceMirror::with_row::<MovementRow, _>(mirror, |row| {
            EntityStore::with_rows(&store, |rows| return_path(geo, row, rows, raw_model, &person.agent_id))
        })?,
        _ => None,
    };
    let (minutes, points) = match path {
        Some(None) => return Ok(None),
        Some(Some((minutes, points))) => (minutes, Some(points)),
        None => (0, None),
    };
    let due = minute.checked_add(minutes).ok_or_else(|| Unsupported::new_err("a due minute out of range"))?;
    if due >= writer.duration() {
        return Ok(None);
    }
    let retry_at = minute.checked_add(call.retry).ok_or_else(|| Unsupported::new_err("a minute out of range"))?;
    let abandon_id: Arc<str> = Arc::from(format!("facility-queue-abandon-{sequence}"));
    if !identity_is_free(&writer, &abandon_id, sequence) {
        return unsupported("the event's identity is taken");
    }
    let mut payload = IndexMap::with_capacity(8);
    payload.insert(Arc::from("facility_id"), text(call.facility_name));
    payload.insert(Arc::from("agent_id"), V::Str(person.agent_id.clone()));
    payload.insert(Arc::from("task_id"), V::Str(person.task_id.clone()));
    payload.insert(Arc::from("service"), text(call.service.name()));
    payload.insert(Arc::from("start_minute"), V::Int(person.start));
    payload.insert(Arc::from("wait_minutes"), V::Int(minute - person.start));
    payload.insert(Arc::from("retry_at_minute"), V::Int(retry_at));
    payload.insert(Arc::from("reason"), text("need_weighted_wait_threshold"));
    let mut provenance = IndexMap::with_capacity(4);
    provenance.insert(Arc::from("source_type"), text("assumption"));
    provenance.insert(Arc::from("source_id"), text(NEED_WEIGHTED_SOURCE));
    provenance.insert(Arc::from("policy_version"), text(NEED_WEIGHTED_ID));
    provenance.insert(Arc::from("transition"), text("abandon_queue_then_retry"));
    let Some(abandon) = EventRow::new(
        abandon_id.clone(),
        EventKind::Endogenous,
        minute,
        0,
        sequence,
        Arc::from("facility_queue_abandon"),
        payload,
        Some(Arc::from(NEED_WEIGHTED_SOURCE)),
        Some(person.task_id.clone()),
        provenance,
    ) else {
        return unsupported("the event's values are not what an event takes");
    };
    let mut next = sequence + 1;
    let transition = match call.queue_id {
        None => None,
        Some(queue_id) => {
            match with_queues(world, |row| Ok(request_of_task(row, &person.task_id).cloned()))?.flatten() {
                None => None,
                Some(request) => {
                    let capacity = point_of(&*Geo::of(world)?, queue_id).map(|point| point.service_capacity);
                    // After `finish_service` the request has no place in the queue.
                    let row = transition_row(py, &writer, &request, "abandoned", &person.task_id, Some(minute - request.requested_minute), Some(&person.task_id), None, capacity, next, minute)?;
                    next += 1;
                    Some((request, row))
                }
            }
        }
    };
    let walk = match points.filter(|_| minutes > 0) {
        None => None,
        Some(points) => {
            let task_id: Arc<str> = Arc::from(format!("queue-abandon-travel-{next}"));
            let event_id: Arc<str> = Arc::from(format!("queue-abandon-arrival-{next}"));
            if !identity_is_free(&writer, &event_id, next) {
                return unsupported("the event's identity is taken");
            }
            if points.iter().any(|point| !usable(point.x, point.y)) {
                return unsupported("a route point is not a usable point");
            }
            let Some(mirror) = &mirror else {
                return unsupported("a walk without the floor plan");
            };
            let scene = Scene::of(world)?;
            let (event, destination, changes) = NamespaceMirror::with_row::<MovementRow, _>(mirror, |row| {
                EntityStore::with_rows(&store, |rows| {
                    let Some(agent) = rows.agents.get(&person.agent_id) else {
                        return unsupported("no such agent");
                    };
                    let zone = agent.zone_id.clone();
                    let mut payload = IndexMap::with_capacity(9);
                    payload.insert(Arc::from("agent_id"), V::Str(person.agent_id.clone()));
                    payload.insert(Arc::from("task_id"), V::Str(task_id.clone()));
                    payload.insert(Arc::from("purpose"), text("queue_abandon"));
                    payload.insert(Arc::from("destination_zone_id"), V::Str(zone.clone()));
                    payload.insert(Arc::from("origin_zone_id"), V::Str(zone.clone()));
                    payload.insert(Arc::from("route_zone_ids"), V::List(vec![V::Str(zone.clone()), V::Str(zone.clone())]));
                    payload.insert(Arc::from("route_edge_ids"), V::List(Vec::new()));
                    payload.insert(Arc::from("travel_minutes"), V::Int(minutes));
                    payload.insert(Arc::from("start_minute"), V::Int(minute));
                    let mut provenance = IndexMap::with_capacity(4);
                    provenance.insert(Arc::from("source_type"), text("assumption"));
                    provenance.insert(Arc::from("source_id"), text(NEED_WEIGHTED_SOURCE));
                    provenance.insert(Arc::from("policy_version"), text(NEED_WEIGHTED_ID));
                    provenance.insert(Arc::from("movement"), text("in_room_return_to_zone_anchor"));
                    let Some(event) = EventRow::new(
                        event_id.clone(),
                        EventKind::Completion,
                        due,
                        0,
                        next,
                        Arc::from("agent_travel_complete"),
                        payload,
                        Some(task_id.clone()),
                        Some(abandon_id.clone()),
                        provenance,
                    ) else {
                        return unsupported("the event's values are not what an event takes");
                    };
                    let journey = Journey {
                        event_id: event_id.clone(),
                        task_id: task_id.clone(),
                        purpose: Arc::from("queue_abandon"),
                        facility_id: None,
                        destination_zone_id: zone.clone(),
                        route_zone_ids: vec![zone.clone(), zone.clone()],
                        route_edge_ids: Vec::new(),
                        route_points: points.iter().map(|point| Point { x: point.x, y: point.y }).collect(),
                        started_minute: minute,
                        due_minute: due,
                        queue_id: None,
                        support_request_id: None,
                        support_staff_id: None,
                    };
                    let changes = plan_start(&scene, row, rows, &person.agent_id, journey, &zone, &points, &[], minute, minutes)?;
                    Ok((event, zone, changes))
                })
            })?;
            next += 1;
            Some(LeaveWalk { task_id, destination, due, event, changes })
        }
    };
    Ok(Some(LeavePlan { abandon, transition, walk, sequences: next - sequence }))
}

/// Everyone's plan, for what it refuses: the writes then plan again, on the state they find.
fn plan_all_leaving(call: &LeaveCall<'_, '_>, leaving: &[Leaving]) -> PyResult<()> {
    if leaving.is_empty() {
        return Ok(());
    }
    let mut sequence = EventWriter::of(call.world)?.next_sequence()?;
    metrics_of(call.world)?;
    for person in leaving {
        if let Some(plan) = plan_leave(call, person, sequence)? {
            sequence += plan.sequences;
        }
    }
    Ok(())
}

/// `_abandon_and_retry`, in the order of its writes.
fn write_all_leaving(call: &LeaveCall<'_, '_>, leaving: &[Leaving]) -> PyResult<()> {
    let (world, minute) = (call.world, call.minute);
    let py = world.py();
    for person in leaving {
        let writer = EventWriter::of(world)?;
        let Some(plan) = plan_leave(call, person, writer.next_sequence()?)? else {
            continue;
        };
        let store = entities_of(world)?;
        writer.allocate_sequence()?;
        let facilities = attribute(world, intern!(py, "facilities"))?;
        let Some(facility) = raw_dict_get(&facilities, call.facility_id)? else {
            return unsupported("the rows' facility has no object");
        };
        let name = if call.resource { intern!(py, "resource_queue_agent_ids") } else { intern!(py, "queue_agent_ids") };
        let agent_text = PyString::new(py, &person.agent_id);
        attribute(&facility, name)?.call_method1(intern!(py, "remove"), (&agent_text,))?;
        if call.resource {
            let remaining = EntityStore::with_rows(&store, |rows| match rows.facilities.get(call.facility_name) {
                Some(facility) => Ok(members(facility, true).clone()),
                None => unsupported("no such facility"),
            })?;
            let table = attribute(world, intern!(py, "model_state"))?;
            if let Some(stalls) = stale_stall(world, exact_dict(&table)?, call.facility_name, &remaining)? {
                stalls.del_item(call.facility_id)?;
            }
        }
        if let Some((request, row)) = &plan.transition {
            write_finish(world, request)?;
            write_transition(&writer, row, minute)?;
        }
        let tasks = attribute(world, intern!(py, "active_tasks"))?;
        tasks.del_item(&*person.task_id)?;
        let next = minute + call.retry;
        EntityStore::with_rows(&store, |rows| {
            let Some(row) = rows.agents.get_mut(&person.agent_id) else {
                return unsupported("no such agent");
            };
            row.next_decision_at = next;
            Ok(())
        })?;
        let agents = attribute(world, intern!(py, "agents"))?;
        let Some(agent) = raw_dict_get(&agents, agent_text.as_any())? else {
            return unsupported("a row has no object");
        };
        raw_setattr(&agent, intern!(py, "next_decision_at"), next.into_pyobject(py)?.as_ptr())?;
        writer.record(&plan.abandon, minute, EventStatus::Applied, Some(ABANDON_DETAIL))?;
        match plan.walk {
            Some(LeaveWalk { task_id, destination, due, event, changes }) => {
                writer.allocate_sequence()?;
                let task_text = PyString::new(py, &task_id);
                let task = active_task_class(py)?.call1((&task_text, "travel_return", &agent_text, py.None(), minute, due))?;
                tasks.set_item(&task_text, task)?;
                set_agent(world, &store, agent_text.as_any(), Activity::Moving, Some(PyString::new(py, &destination).as_any()))?;
                writer.schedule(event)?;
                let mirror = mirror_held(world)?;
                let writes = NamespaceMirror::with_row_tree::<MovementRow, _>(&mirror, |row, tree| changes.apply(row, tree))?;
                write_tree(&mirror, writes)?;
            }
            None => set_agent(world, &store, agent_text.as_any(), Activity::Idle, None)?,
        }
        add_one(&metrics_of(world)?, "facility_queue_abandon_count")?;
    }
    Ok(())
}

// ---- progress_physical_queue_positions and _head_to_service --------------------------------------

/// A route to walk: `_schedule_queue_trip`'s arguments.
pub(crate) struct Route {
    pub(crate) agent_id: Arc<str>,
    pub(crate) index: usize,
    pub(crate) minutes: i64,
    pub(crate) path: Arc<[Pt]>,
}

/// What a kernel found to write.
pub(crate) enum Plan {
    Nothing,
    Blocked,
    /// `_schedule_queue_trip` of a route that really walks.
    Trip(Box<Trip>),
}

impl Plan {
    /// What the plan changes in the movement row, for a caller that writes several together.
    pub(crate) fn take_changes(&mut self) -> Changes {
        match self {
            Plan::Trip(trip) => std::mem::take(&mut trip.changes),
            _ => Changes::default(),
        }
    }
}

pub(crate) struct Trip {
    agent_id: Arc<str>,
    event: EventRow,
    changes: Changes,
}

fn text(value: &str) -> V {
    V::Str(Arc::from(value))
}

/// `_schedule_queue_trip`, changing nothing, taking this event number.
#[allow(clippy::too_many_arguments)]
pub(crate) fn plan_trip(
    scene: &Scene<'_>,
    writer: &EventWriter<'_>,
    row: &MovementRow,
    rows: &Entities,
    facility_id: &str,
    resource: bool,
    route: Route,
    minute: i64,
    phase: &'static str,
    fifo: Option<&Fifo>,
    sequence: i64,
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
    // `request_for_agent`, whose task and place in the queue a trip names.
    let queue_request = match fifo {
        Some(fifo) => match fifo.requests.iter().find(|request| request.agent_id == route.agent_id) {
            Some(request) => Some((fifo, request)),
            None => return unsupported("a person of the queue has no request"),
        },
        None => None,
    };
    let task = match queue_request {
        Some((_, request)) => rows.active_tasks.get(&*request.queue_task_id),
        None => rows
            .active_tasks
            .iter()
            .find(|(_, task)| {
                task.agent_id.as_deref() == Some(&*route.agent_id)
                    && task.resource_id.as_deref() == Some(facility_id)
                    && &*task.kind == task_kind
            })
            .map(|(_, task)| task),
    };
    let Some(task) = task else {
        return unsupported("a queue reservation has no matching task");
    };
    let fifo_position = match queue_request {
        Some((fifo, request)) => match fifo.queued_request_ids.iter().position(|id| *id == request.request_id) {
            Some(place) => place,
            None => return unsupported("a request of the walk is not queued"),
        },
        None => route.index,
    };
    if route.path.iter().any(|point| !usable(point.x, point.y)) {
        return unsupported("a route point is not a usable point");
    }
    let event_id: Arc<str> = Arc::from(format!("physical-queue-trip-{sequence}"));
    let purpose: Arc<str> = Arc::from(format!("{}_queue_{phase}", if resource { "resource" } else { "toilet" }));
    let zone = facility.zone_id.clone();
    let zone_ids = vec![zone.clone(), zone.clone()];
    let mut payload = IndexMap::with_capacity(12);
    payload.insert(Arc::from("agent_id"), V::Str(route.agent_id.clone()));
    payload.insert(Arc::from("task_id"), V::Str(task.id.clone()));
    payload.insert(Arc::from("purpose"), V::Str(purpose.clone()));
    payload.insert(Arc::from("destination_zone_id"), V::Str(zone.clone()));
    payload.insert(Arc::from("facility_id"), text(facility_id));
    if let Some((fifo, request)) = queue_request {
        payload.insert(Arc::from("queue_id"), V::Str(fifo.queue_id.clone()));
        payload.insert(Arc::from("request_id"), V::Str(request.request_id.clone()));
    }
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
    provenance.insert(Arc::from("fifo_position"), V::Int(fifo_position as i64));
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
        queue_id: fifo.map(|fifo| fifo.queue_id.clone()),
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

pub(crate) fn write_plan<'py>(
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
#[allow(clippy::too_many_arguments)]
pub(crate) fn slot_route(
    geo: &Geo,
    row: &MovementRow,
    rows: &Entities,
    raw_model: Option<Bound<'_, PyAny>>,
    agent_id: &str,
    facility: &FacilityRow,
    facility_id: &str,
    fifo: Option<&Fifo>,
) -> PyResult<Option<(i64, Arc<[Pt]>)>> {
    let Some(destination) = queue_slot_in(geo, row, rows, agent_id, facility_id, fifo.map(Fifo::slot_view))? else {
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

/// `progress_physical_queue_positions`.
#[pyfunction]
#[pyo3(signature = (world, facility_id, *, resource, minute, queue_id=None))]
pub(crate) fn progress_physical_queue_positions<'py>(
    world: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    resource: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    queue_id: Option<&Bound<'py, PyAny>>,
) -> PyResult<bool> {
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
    let fifo = fifo_of(world, &geo, queue_id)?;
    let queue_name = fifo.as_ref().map(|fifo| &*fifo.queue_id);
    let service_point = fifo.as_ref().map(|fifo| &*fifo.service_point_id);
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
            let queue: &[Arc<str>] = match &fifo {
                Some(fifo) => &fifo.members,
                None => members(facility, resource),
            };
            let head = queue.first();
            let head_walks = |row: &MovementRow| {
                head.is_some_and(|head| fifo_trips(row, facility_name, queue_name).any(|(id, _)| id == head))
            };
            if fifo_trips(row, facility_name, queue_name).any(|(_, journey)| journey.purpose.ends_with("_position")) {
                return Ok((head_walks(row), Plan::Nothing));
            }
            for (index, agent_id) in queue.iter().enumerate() {
                if index == 0 {
                    if head_walks(row) {
                        continue;
                    }
                    let route = approach_point(&geo, row, rows, raw_model.clone(), agent_id, facility_name, service_point)?;
                    if route.is_some_and(|(minutes, path)| minutes <= 0 || path.len() < 2) {
                        continue;
                    }
                }
                let target = queue_slot_in(&geo, row, rows, agent_id, facility_name, fifo.as_ref().map(Fifo::slot_view))?;
                let current = position_point(row.positions.get(&**agent_id));
                if current.is_some_and(|point| !usable(point.x, point.y)) {
                    return unsupported("a position is not a usable point");
                }
                if let (Some(target), Some(current)) = (target, current) {
                    if distance(target, current) <= SLOT_TOLERANCE {
                        continue;
                    }
                }
                let route = slot_route(&geo, row, rows, raw_model.clone(), agent_id, facility, facility_name, fifo.as_ref())?;
                let Some((minutes, path)) = route.filter(|(_, path)| !path.is_empty()) else {
                    if index > 0 {
                        return Ok((false, Plan::Blocked));
                    }
                    return Ok((true, Plan::Blocked));
                };
                // `list.index`, which a person listed twice would make another place.
                let place = queue.iter().position(|other| other == agent_id).unwrap_or(index);
                let route = Route { agent_id: agent_id.clone(), index: place, minutes, path };
                let plan = plan_trip(&scene, &writer, row, rows, facility_name, resource, route, minute, "position", fifo.as_ref(), writer.next_sequence()?)?;
                return Ok((index == 0, plan));
            }
            Ok((false, Plan::Nothing))
        })
    })?;
    write_plan(world, &mirror, &store, &writer, facility_id, plan)?;
    Ok(result)
}

/// `progress_physical_queue_head_to_service`.
#[pyfunction]
#[pyo3(signature = (world, facility_id, *, resource, minute, queue_id=None))]
pub(crate) fn progress_physical_queue_head_to_service<'py>(
    world: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    resource: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    queue_id: Option<&Bound<'py, PyAny>>,
) -> PyResult<bool> {
    let queue_name = match queue_id {
        Some(queue) if !queue.is_none() => Some(exact_str(queue)?),
        _ => None,
    };
    head_to_service(world, facility_id, exact_bool(resource)?, plain_minute(minute)?, queue_name)
}

/// That rule: false has written nothing, and Unsupported changes nothing.
pub(crate) fn head_to_service<'py>(
    world: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    resource: bool,
    minute: i64,
    queue_id: Option<&str>,
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
    let fifo = match queue_id {
        Some(queue) => match Fifo::read(world, &geo, queue)? {
            Some(fifo) => Some(fifo),
            None => return unsupported("a queue ID without the queues"),
        },
        None => None,
    };
    let queue_name = fifo.as_ref().map(|fifo| &*fifo.queue_id);
    let service_point = fifo.as_ref().map(|fifo| &*fifo.service_point_id);
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
            let queue: &[Arc<str>] = match &fifo {
                Some(fifo) => &fifo.members,
                None => members(facility, resource),
            };
            let Some(agent_id) = queue.first() else {
                return Ok((false, Plan::Nothing));
            };
            if fifo_trips(row, facility_name, queue_name).any(|(id, _)| id == agent_id) {
                return Ok((true, Plan::Nothing));
            }
            let Some((minutes, path)) = approach_point(&geo, row, rows, raw_model, agent_id, facility_name, service_point)? else {
                return Ok((true, Plan::Blocked));
            };
            if minutes <= 0 || path.len() < 2 {
                return Ok((false, Plan::Nothing));
            }
            let route = Route { agent_id: agent_id.clone(), index: 0, minutes, path };
            Ok((true, plan_trip(&scene, &writer, row, rows, facility_name, resource, route, minute, "service", fifo.as_ref(), writer.next_sequence()?)?))
        })
    })?;
    write_plan(world, &mirror, &store, &writer, facility_id, plan)?;
    Ok(result)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(prepare_facility_queue, m)?)?;
    m.add_function(wrap_pyfunction!(prepare_independent_facility_queue, m)?)?;
    m.add_function(wrap_pyfunction!(progress_physical_queue_positions, m)?)?;
    m.add_function(wrap_pyfunction!(progress_physical_queue_head_to_service, m)?)?;
    Ok(())
}
