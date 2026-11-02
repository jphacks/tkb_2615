//! `domain/staff.py` and `domain/staff_operations.py`. A tick where no staff member starts anything
//! is nearly every tick, and takes no call to Python. A tick that starts something plans from the
//! rows and then writes on the Python containers, one value or record at a time
//! (docs/rust-port/lanes/m4-staff.md, l2-staff.md).
//!
//! The Python rules stay as the comparison base and for what these kernels do not decide, which is
//! also how a bad value gets its error text.

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyInt, PyList, PySlice, PyString, PyTuple};
use rustc_hash::FxHashMap;

use crate::core::mathx::{py_fsum, py_max, py_round_int};
use crate::core::row::{attribute, exact_dict, exact_int, exact_str, map_set, namespace_is_absent, unsupported};
use crate::core::value::V;
use crate::core::world_core::{field_mirror_of, mirror_of, raw_dict_get, NamespaceMirror};
use crate::rules::effects::{active_outage, has_active_modifier, refused_by_python};
use crate::rules::handlers::{assumption, get, get_is, log_record, new_event, NewEvent};
use crate::rules::movement::{mobility_text, movement_minutes};
use crate::rules::movement_geometry::Geo;
use crate::rules::service::active_task_class;
use crate::rules::staff_operation_events::relief_skills;
use crate::rules::staff_dynamics::{is_available, on_shift, staff_status};
use crate::rules::travel::{active_edge_loads, passage_edge_loads, staff_router_rows, world_graph, EdgeLoads, Route, RouteRows};
use crate::state::body_state::{BodyRow, NAME as BODY_STATE};
use crate::state::entities::{entities_of, raw_setattr, Activity, EntityStore, RequestStatus};
use crate::state::metric_state::MetricRow;
use crate::state::staff_dynamics::{StaffDynamicsRow, NAME as STAFF_DYNAMICS};
use crate::state::staff_operations::{Phase, StaffOperationsRow, NAME};
use crate::Unsupported;

const SHARED_OPERATIONS: &str = "shared_operations_v1";
const SUPPORT_SOURCE: &str = "ASM-STAFF-SUPPORT-LIFECYCLE-V1";
const PREEMPTION_SOURCE: &str = "ASM-URGENT-STAFF-SUPPORT-PREEMPTION-V1";
const OPERATIONS_SOURCE: &str = "ASM-STAFF-OPERATIONS-V1";
const OPERATIONS_POLICY: &str = "autonomous-routed-staff-v1";
const RELIEF_SOURCE: &str = "ASM-STAFF-RELIEF-V1";
const RELIEF_POLICY: &str = "same-site-staff-relief-v1";
const MOVEMENT: &str = "movement_v1";
/// `support_task_minutes`'s default, in `_support_route_wait_reasons` and `StaffService`.
const DEFAULT_SUPPORT_MINUTES: i64 = 10;
const DEFAULT_KIND: &str = "emotional_support";
const WAIT_REASON: &str = "no_available_qualified_staff";
const NO_QUALIFIED_METRIC: &str = "support_no_qualified_worker_request_minutes";
const ATTEMPT_METRIC: &str = "support_assignment_metrics_minute";
const BUSY_METRIC: &str = "support_wait_staff_busy_request_minutes";
const OFF_SHIFT_METRIC: &str = "support_wait_staff_off_shift_request_minutes";
const CLEANING_BELOW: f64 = 0.85;
const OUTREACH_AT_LEAST: f64 = 0.45;
const PATROL_AT_LEAST: f64 = 0.6;
/// Above this an int does not always fit a float.
const FLOAT_EXACT_INT: i64 = 1 << 53;

/// `_CARE_SKILL_ALTERNATIVES.get(kind, {"support"})`.
fn required_skills(kind: &str) -> &'static [&'static str] {
    match kind {
        "emotional_support" => &["support"],
        "medical_assessment" => &["medical", "nursing"],
        "medication_support" => &["medication", "nursing"],
        "caregiver_assistance" => &["caregiver", "support"],
        "dependent_assistance" => &["caregiver", "support", "nursing"],
        "privacy_accommodation" => &["privacy", "support"],
        "mobility_assistance" => &["mobility", "mobility_assistance", "support"],
        "hygiene_support" => &["hygiene", "support"],
        "language_access" => &["interpreter", "language_access"],
        _ => &["support"],
    }
}

/// `world.minute_index` and `world.model_state`.
fn table_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let table = attribute(world, intern!(world.py(), "model_state"))?;
    exact_dict(&table)?;
    Ok(table)
}

fn minute_of(world: &Bound<'_, PyAny>) -> PyResult<i64> {
    exact_int(&attribute(world, intern!(world.py(), "minute_index"))?)
}

/// The mirror of a namespace, unless the world holds something else there (a store past
/// `NamespaceTable`), which only the Python rules read as it is.
pub(crate) fn mirror_held<'py>(world: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, NamespaceMirror>> {
    let table = table_of(world)?;
    let mirror = mirror_of(world, name)?;
    match exact_dict(&table)?.get_item(name)? {
        Some(state) if mirror.try_borrow()?.root_is(&state) => Ok(mirror),
        _ => unsupported("the world holds another document than the row's"),
    }
}

// ---- What the rules read about the staff ---------------------------------------------------------

/// What `staff_is_available` and `staff_dynamics_*` answer for one member at one minute, and what
/// `support_wait_reason_codes` reads from the same records.
struct Worker {
    id: Arc<str>,
    zone: Arc<str>,
    skills: Vec<Arc<str>>,
    has_task: bool,
    /// `staff_is_available`.
    available: bool,
    /// `staff_dynamics_on_shift`.
    on_shift: bool,
    /// `staff_dynamics_is_available`.
    can_start: bool,
    /// The `rest_until_minute` of its `staff_dynamics_v1` record, if it has one.
    rest_until: Option<i64>,
    /// The phase of its `staff_operations_v1` record is not idle.
    on_operation: bool,
}

/// Every staff member in the order of `world.staff`.
fn workers_at(world: &Bound<'_, PyAny>, minute: i64) -> PyResult<Vec<Worker>> {
    let py = world.py();
    let status = staff_status(world)?;
    let dynamics =
        if namespace_is_absent(world, intern!(py, STAFF_DYNAMICS))? { None } else { Some(mirror_held(world, STAFF_DYNAMICS)?) };
    let operations = if namespace_is_absent(world, intern!(py, NAME))? { None } else { Some(mirror_held(world, NAME)?) };
    let store = entities_of(world)?;
    let mut workers = EntityStore::with_rows(&store, |rows| {
        Ok(rows
            .staff
            .iter()
            .map(|(id, row)| Worker {
                id: id.clone(),
                zone: row.zone_id.clone(),
                skills: row.skills.0.clone(),
                has_task: row.active_task_id.is_some(),
                available: true,
                on_shift: true,
                can_start: true,
                rest_until: None,
                on_operation: false,
            })
            .collect::<Vec<_>>())
    })?;
    if let Some(operations) = &operations {
        NamespaceMirror::with_row::<StaffOperationsRow, _>(operations, |row| {
            for worker in &mut workers {
                worker.on_operation = row.staff.get(&worker.id).is_some_and(|record| record.phase != Phase::Idle);
            }
            Ok(())
        })?;
    }
    if let Some(dynamics) = &dynamics {
        NamespaceMirror::with_row::<StaffDynamicsRow, _>(dynamics, |row| {
            for worker in &mut workers {
                let record = row.staff.get(&worker.id);
                worker.rest_until = record.map(|record| record.rest_until_minute);
                worker.on_shift = match record {
                    Some(record) => on_shift(&row.config, &worker.id, minute)? && minute >= record.rest_until_minute,
                    None => false,
                };
            }
            Ok(())
        })?;
    }
    for worker in &mut workers {
        let id = PyString::new(py, &worker.id);
        worker.available = is_available(status.as_ref(), &id)?;
        worker.can_start = dynamics.is_none() || (worker.on_shift && worker.available && !worker.on_operation);
    }
    Ok(workers)
}

/// The kind of each request as `_support_kind` reads it, from `shared_operations_v1`.
struct CareKinds<'py> {
    requests: Option<Bound<'py, PyDict>>,
}

impl<'py> CareKinds<'py> {
    fn of(table: &Bound<'py, PyAny>) -> PyResult<Self> {
        let py = table.py();
        let Some(state) = exact_dict(table)?.get_item(intern!(py, SHARED_OPERATIONS))? else {
            return Ok(Self { requests: None });
        };
        if state.is_none() {
            return Ok(Self { requests: None });
        }
        let Some(requests) = exact_dict(&state)?.get_item(intern!(py, "support_requests"))? else {
            return Ok(Self { requests: None });
        };
        if requests.is_none() {
            return Ok(Self { requests: None });
        }
        Ok(Self { requests: Some(exact_dict(&requests)?.clone()) })
    }

    /// `_support_kind`. A kind that is not a str could raise there.
    fn kind_of(&self, id: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let Some(requests) = &self.requests else {
            return Ok(None);
        };
        let Some(record) = requests.get_item(id)? else {
            return Ok(None);
        };
        if record.is_none() {
            return Ok(None);
        }
        let Ok(record) = exact_dict(&record) else {
            return unsupported("a care record is not a plain dict");
        };
        let kind = record.get_item(intern!(id.py(), "support_kind"))?;
        if let Some(kind) = &kind {
            exact_str(kind)?;
        }
        Ok(kind)
    }
}

/// `_support_priority(world, id) > 0` for any of these requests would be asked: None when nothing
/// could say so.
fn priorities_of<'py>(table: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyDict>>> {
    let py = table.py();
    let Some(state) = exact_dict(table)?.get_item(intern!(py, "llm"))? else {
        return Ok(None);
    };
    let Ok(state) = exact_dict(&state) else {
        return if state.is_instance_of::<PyDict>() { unsupported("llm is another dict") } else { Ok(None) };
    };
    let Some(priorities) = state.get_item(intern!(py, "support_priorities"))? else {
        return Ok(None);
    };
    let Ok(priorities) = exact_dict(&priorities) else {
        return if priorities.is_instance_of::<PyDict>() { unsupported("priorities are another dict") } else { Ok(None) };
    };
    Ok((!priorities.is_empty()).then(|| priorities.clone()))
}

fn is_urgent(priorities: Option<&Bound<'_, PyDict>>, id: &Bound<'_, PyAny>) -> PyResult<bool> {
    let Some(priorities) = priorities else {
        return Ok(false);
    };
    let Some(value) = priorities.get_item(id)? else {
        return Ok(false);
    };
    if value.is_instance_of::<PyBool>() || !value.is_instance_of::<PyInt>() {
        return Ok(false);
    }
    Ok(exact_int(&value)? > 0)
}

// ---- StaffService.assign_pending ------------------------------------------------------------------

/// Of the qualified staff of a kind: none at all, one has a task, one is not working now.
/// None if one could take the request.
fn kind_exposure(workers: &[Worker], kind: &str) -> Option<(bool, bool, bool)> {
    let required = required_skills(kind);
    let (mut any, mut busy, mut off_shift) = (false, false, false);
    for worker in workers {
        if worker.skills.iter().any(|skill| required.contains(&&**skill)) {
            if !worker.has_task && worker.available && worker.can_start {
                return None;
            }
            any = true;
            busy |= worker.has_task;
            off_shift |= !worker.on_shift || !worker.available;
        }
    }
    Some((!any, busy, off_shift))
}

/// `StaffService.assign_pending`: the plain case without a call to Python, and the rest.
#[pyfunction]
#[pyo3(signature = (service, minute, *, validate_state=true))]
pub(crate) fn assign_pending<'py>(
    service: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    validate_state: bool,
) -> PyResult<Bound<'py, PyTuple>> {
    let py = service.py();
    let rules = service_rules(py)?;
    let plain = rules.call_method(intern!(py, "assign_none"), (service, minute), Some(&validate_option(py, validate_state)?))?;
    if plain.is_none() {
        return rules.call_method(intern!(py, "assign_work"), (service, minute), Some(&validate_option(py, validate_state)?))?.downcast_into::<PyTuple>().map_err(PyErr::from);
    }
    plain.downcast_into::<PyTuple>().map_err(PyErr::from)
}

/// The module of these kernels: a shadow replaces what it finds there.
fn service_rules(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    py.import(intern!(py, "sheltersim.native"))?.getattr(intern!(py, "rules"))?.call1(("staff",))
}

fn validate_option(py: Python<'_>, validate_state: bool) -> PyResult<Bound<'_, PyDict>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item(intern!(py, "validate_state"), validate_state)?;
    Ok(kwargs)
}

fn priority_of(priorities: Option<&Bound<'_, PyDict>>, id: &Bound<'_, PyAny>) -> PyResult<i64> {
    let Some(priorities) = priorities else {
        return Ok(0);
    };
    let Some(value) = priorities.get_item(id)? else {
        return Ok(0);
    };
    if value.is_instance_of::<PyBool>() || !value.is_instance_of::<PyInt>() {
        return Ok(0);
    }
    exact_int(&value)
}

/// `StaffService.assign_pending` when a request can be taken, on the objects of the world. Its route and
/// its trip are the callbacks the service was given, called in the same order as the Python rule. What
/// it reads first refuses with nothing changed; past that what is raised is what the Python rule raises.
#[pyfunction]
#[pyo3(signature = (service, minute, *, validate_state=true))]
fn assign_work<'py>(service: &Bound<'py, PyAny>, minute: &Bound<'py, PyAny>, validate_state: bool) -> PyResult<Bound<'py, PyTuple>> {
    let py = service.py();
    struct Open<'py> {
        object: Bound<'py, PyAny>,
        id: Bound<'py, PyAny>,
        agent_id: Bound<'py, PyAny>,
        requested: i64,
        kind: String,
        priority: i64,
    }
    let plan = (|| -> PyResult<_> {
        let world = attribute(service, intern!(py, "world"))?;
        let at = exact_int(minute)?;
        if at != minute_of(&world)? {
            return unsupported("the minute is not the world's");
        }
        let table = table_of(&world)?;
        let kinds = CareKinds::of(&table)?;
        let priorities = priorities_of(&table)?;
        let fairness = exact_int(&attribute(service, intern!(py, "fairness_after_minutes"))?)?;
        let mut pending: Vec<Open<'py>> = Vec::new();
        for request in world.call_method0(intern!(py, "unsettled_support_requests"))?.try_iter()? {
            let object = request?;
            if !exact_str(&attribute(&attribute(&object, intern!(py, "status"))?, intern!(py, "value"))?).is_ok_and(|status| status == "pending") {
                continue;
            }
            let id = attribute(&object, intern!(py, "id"))?;
            exact_str(&id)?;
            let kind = kinds.kind_of(&id)?;
            let kind = match &kind {
                Some(kind) => exact_str(kind)?.to_owned(),
                None => DEFAULT_KIND.to_owned(),
            };
            pending.push(Open {
                agent_id: attribute(&object, intern!(py, "agent_id"))?,
                requested: exact_int(&attribute(&object, intern!(py, "requested_minute"))?)?,
                priority: priority_of(priorities.as_ref(), &id)?,
                object,
                id,
                kind,
            });
        }
        // `_support_assignment_order`.
        let mut ordered: Vec<((i64, i64, String), Open<'py>)> = pending
            .into_iter()
            .map(|open| {
                let effective = i64::from(open.priority > 0 || at - open.requested >= fairness);
                ((-effective, open.requested, exact_str(&open.id).map(str::to_owned).unwrap_or_default()), open)
            })
            .collect();
        ordered.sort_by(|first, second| first.0.cmp(&second.0));
        let workers = workers_at(&world, at)?;
        let staff_map = attribute(&world, intern!(py, "staff"))?;
        let mut objects = Vec::with_capacity(workers.len());
        for worker in &workers {
            objects.push(staff_map.get_item(&*worker.id)?);
        }
        let metrics = attribute(&world, intern!(py, "metric_state"))?;
        let attempt = number_at(&metrics, ATTEMPT_METRIC, -1.0)?;
        let no_qualified = ordered.iter().filter(|(_, open)| !workers.iter().any(|worker| qualifies(worker, &open.kind))).count();
        let current_no_qualified = number_at(&metrics, NO_QUALIFIED_METRIC, 0.0)?;
        Ok((world, at, ordered, workers, objects, metrics, attempt, no_qualified, current_no_qualified, fairness))
    })();
    let (world, at, ordered, workers, objects, metrics, attempt, no_qualified, current_no_qualified, fairness) = crate::rules::effects::refused_by_python(py, plan)?;

    // Sorted by ID: the workers free to start.
    let mut free: Vec<usize> = (0..workers.len()).filter(|index| !workers[*index].has_task && workers[*index].available && workers[*index].can_start).collect();
    free.sort_by(|first, second| workers[*first].id.cmp(&workers[*second].id));
    let record_metrics = attempt != at as f64;
    if record_metrics {
        metrics.set_item(NO_QUALIFIED_METRIC, current_no_qualified + no_qualified as f64)?;
    }
    let (mut busy, mut off_shift) = (0_i64, 0_i64);
    let duration = attribute(&world, intern!(py, "duration_minutes"))?.extract::<i64>()?;
    let task_minutes = attribute(service, intern!(py, "task_duration_minutes"))?.extract::<i64>()?;
    let resolver = attribute(service, intern!(py, "route_resolver"))?;
    let scheduler = attribute(service, intern!(py, "travel_scheduler"))?;
    let policy = attribute(service, intern!(py, "policy_version"))?;
    let agents = attribute(&world, intern!(py, "agents"))?;
    let mut assigned: Vec<Bound<'py, PyAny>> = Vec::new();
    for (_, open) in ordered {
        open.object.setattr(intern!(py, "last_assignment_attempt_minute"), at)?;
        open.object.setattr(intern!(py, "last_assignment_wait_reason_code"), WAIT_REASON)?;
        let mut person = free.iter().copied().find(|index| qualifies(&workers[*index], &open.kind));
        if person.is_none() && open.priority > 0 {
            person = preempt_worker(&world, &workers, &objects, &open.kind, at, &open.id, &open.agent_id, &resolver, task_minutes, duration)?;
            if let Some(index) = person {
                free.push(index);
            }
        }
        let Some(person) = person else {
            if record_metrics {
                let mut any_busy = false;
                let mut any_away = false;
                for (index, worker) in workers.iter().enumerate() {
                    if qualifies(worker, &open.kind) {
                        any_busy |= !attribute(&objects[index], intern!(py, "active_task_id"))?.is_none();
                        any_away |= !worker.on_shift || !worker.available;
                    }
                }
                busy += i64::from(any_busy);
                off_shift += i64::from(any_away);
            }
            continue;
        };
        let agent = agents.get_item(&open.agent_id)?;
        let staff = &objects[person];
        let staff_id = attribute(staff, intern!(py, "id"))?;
        let mut route = py.None().into_bound(py);
        let mut travel_minutes = 0_i64;
        if !resolver.is_none() {
            route = resolver.call1((attribute(&agent, intern!(py, "zone_id"))?, attribute(staff, intern!(py, "zone_id"))?, attribute(&agent, intern!(py, "id"))?))?;
            if route.is_none() {
                open.object.setattr(intern!(py, "last_assignment_wait_reason_code"), "route_unavailable_at_assignment")?;
                continue;
            }
            travel_minutes = attribute(&route, intern!(py, "travel_minutes"))?.extract()?;
        }
        let due = at + travel_minutes + task_minutes;
        if due >= duration {
            open.object.setattr(intern!(py, "last_assignment_wait_reason_code"), "insufficient_run_time")?;
            continue;
        }
        let sequence: i64 = world.call_method0(intern!(py, "allocate_event_sequence"))?.extract()?;
        let task_id = format!("staff-support-{sequence}");
        let event_id = format!("staff-support-complete-{sequence}");
        if travel_minutes > 0 {
            let scheduled = if scheduler.is_none() || route.is_none() {
                false
            } else {
                scheduler.call1((attribute(&agent, intern!(py, "id"))?, &route, &open.id, &staff_id, at))?.is_truthy()?
            };
            if !scheduled {
                open.object.setattr(intern!(py, "last_assignment_wait_reason_code"), "resident_travel_schedule_rejected")?;
                continue;
            }
        }
        let task_kwargs = PyDict::new(py);
        task_kwargs.set_item("id", &task_id)?;
        task_kwargs.set_item("kind", "staff_support")?;
        task_kwargs.set_item("agent_id", &open.agent_id)?;
        task_kwargs.set_item("resource_id", py.None())?;
        task_kwargs.set_item("start_minute", at + travel_minutes)?;
        task_kwargs.set_item("due_minute", due)?;
        task_kwargs.set_item("staff_id", &staff_id)?;
        let task = crate::rules::service::active_task_class(py)?.call((), Some(&task_kwargs))?;
        let promoted = open.priority == 0 && at - open.requested >= fairness;
        let payload = PyDict::new(py);
        payload.set_item("request_id", &open.id)?;
        payload.set_item("agent_id", &open.agent_id)?;
        payload.set_item("staff_id", &staff_id)?;
        payload.set_item("task_id", &task_id)?;
        payload.set_item("support_duration_minutes", task_minutes)?;
        let provenance = assumption(py, PyString::new(py, SUPPORT_SOURCE).as_any(), exact_str(&policy)?)?;
        provenance.set_item("assignment_wait_minutes", at - open.requested)?;
        provenance.set_item("fairness_promoted", promoted)?;
        let event = new_event(
            py,
            NewEvent {
                event_id: &event_id,
                kind: "COMPLETION",
                due_minute: due,
                priority: 0,
                sequence,
                event_type: "staff_support_complete",
                payload: payload.into_any(),
                source_id: Some(PyString::new(py, &task_id).into_any()),
                causation_id: Some(open.id.clone()),
                provenance: provenance.into_any(),
            },
        )?;
        attribute(&world, intern!(py, "active_tasks"))?.set_item(&task_id, task)?;
        staff.setattr(intern!(py, "active_task_id"), &task_id)?;
        attribute(staff, intern!(py, "work_queue"))?.set_item(pyo3::types::PySlice::full(py), PyList::new(py, [&task_id])?)?;
        staff.setattr(intern!(py, "busy_until"), due)?;
        open.object.setattr(intern!(py, "status"), RequestStatus::Assigned.member(py)?)?;
        open.object.setattr(intern!(py, "assigned_staff_id"), &staff_id)?;
        open.object.setattr(intern!(py, "task_id"), &task_id)?;
        open.object.setattr(intern!(py, "last_assignment_wait_reason_code"), py.None())?;
        world.call_method1(intern!(py, "schedule_event"), (event,))?;
        if promoted {
            let count = number_at(&metrics, "support_fairness_promoted_assignment_count", 0.0)?;
            metrics.set_item("support_fairness_promoted_assignment_count", count + 1.0)?;
        }
        assigned.push(open.id.clone());
        free.retain(|index| *index != person);
    }
    if record_metrics {
        for (metric, count) in [(BUSY_METRIC, busy), (OFF_SHIFT_METRIC, off_shift)] {
            if count != 0 {
                let value = number_at(&metrics, metric, 0.0)?;
                metrics.set_item(metric, value + count as f64)?;
            }
        }
        metrics.set_item(ATTEMPT_METRIC, at as f64)?;
    }
    if validate_state {
        world.call_method0(intern!(py, "validate"))?;
    }
    PyTuple::new(py, assigned)
}

fn number_at(metrics: &Bound<'_, PyAny>, name: &str, default: f64) -> PyResult<f64> {
    crate::rules::handlers::number(&metrics.call_method1(intern!(metrics.py(), "get"), (name, default))?)
}

fn qualifies(worker: &Worker, kind: &str) -> bool {
    let required = required_skills(kind);
    worker.skills.iter().any(|skill| required.contains(&&**skill))
}

/// `_preempt_background_staff`: frees one qualified routed worker for an urgent request.
#[allow(clippy::too_many_arguments)]
fn preempt_worker<'py>(
    world: &Bound<'py, PyAny>,
    workers: &[Worker],
    objects: &[Bound<'py, PyAny>],
    kind: &str,
    at: i64,
    request_id: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    resolver: &Bound<'py, PyAny>,
    task_minutes: i64,
    duration: i64,
) -> PyResult<Option<usize>> {
    let py = world.py();
    let state = get(&table_of(world)?, NAME)?;
    if !state.is_instance_of::<PyDict>() {
        return Ok(None);
    }
    let empty = PyDict::new(py);
    let records = state.call_method1(intern!(py, "get"), ("staff", &empty))?;
    let mut order: Vec<usize> = (0..workers.len()).collect();
    order.sort_by(|first, second| workers[*first].id.cmp(&workers[*second].id));
    let agents = attribute(world, intern!(py, "agents"))?;
    let mut candidates = Vec::new();
    for index in order {
        let (worker, staff) = (&workers[index], &objects[index]);
        if !qualifies(worker, kind)
            || attribute(staff, intern!(py, "active_task_id"))?.is_none()
            || get_is(&records.call_method1(intern!(py, "get"), (&*worker.id, &empty))?, "phase", "idle")?
            || !worker.available
            || !worker.on_shift
        {
            continue;
        }
        if !resolver.is_none() {
            let zone = attribute(&agents.get_item(agent_id)?, intern!(py, "zone_id"))?;
            let route = resolver.call1((zone, attribute(staff, intern!(py, "zone_id"))?, agent_id))?;
            if route.is_none() || at + attribute(&route, intern!(py, "travel_minutes"))?.extract::<i64>()? + task_minutes >= duration {
                continue;
            }
        }
        candidates.push(index);
    }
    let Some(chosen) = candidates.first().copied() else {
        return Ok(None);
    };
    let worker = &workers[chosen];
    let record = records.get_item(&*worker.id)?;
    let sequence: i64 = world.call_method0(intern!(py, "allocate_event_sequence"))?.extract()?;
    let payload = PyDict::new(py);
    payload.set_item("staff_id", &*worker.id)?;
    payload.set_item("task_id", record.get_item("task_id")?)?;
    payload.set_item("request_id", request_id)?;
    payload.set_item("reason_code", "urgent_resident_support")?;
    let event = new_event(
        py,
        NewEvent {
            event_id: &format!("staff-operation-preempted-{sequence}"),
            kind: "ENDOGENOUS",
            due_minute: at,
            priority: -10,
            sequence,
            event_type: "staff_operation_preempted",
            payload: payload.into_any(),
            source_id: Some(PyString::new(py, "urgent-resident-support-preemption-v1").into_any()),
            causation_id: Some(request_id.clone()),
            provenance: assumption(py, PyString::new(py, PREEMPTION_SOURCE).as_any(), "urgent-staff-support-preemption-v1")?.into_any(),
        },
    )?;
    attribute(world, intern!(py, "event_log"))?.call_method1(
        intern!(py, "append"),
        (log_record(py, &event, at.into_pyobject(py)?.as_any(), "APPLIED", Some("A priority-1 support request preempted a routed background staff task."))?,),
    )?;
    let interrupted = py.import("sheltersim.domain.staff_operations")?.getattr("interrupt_staff_operation")?.call1((world, &*worker.id, &event))?;
    if !interrupted.is_truthy()? {
        return Err(pyo3::exceptions::PyValueError::new_err("urgent support preemption lost its active staff operation"));
    }
    Ok(Some(chosen))
}

/// `_consider_due_complaints`, whose decision stays the policy's: its provider boundary is Python's.
#[pyfunction]
pub(crate) fn consider_due_complaints<'py>(controller: &Bound<'py, PyAny>, minute: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = controller.py();
    let policy = attribute(controller, intern!(py, "social_policy"))?;
    if policy.is_none() {
        return Ok(());
    }
    let threshold = exact_int(&attribute(&attribute(&policy, intern!(py, "limits"))?, intern!(py, "complaint_wait_minutes"))?)?;
    let at = exact_int(minute)?;
    let world = attribute(controller, intern!(py, "world"))?;
    let store = entities_of(&world)?;
    let open = world.call_method0(intern!(py, "unsettled_support_requests"))?;
    let due_of = |id: &str| -> PyResult<Option<Arc<str>>> {
        EntityStore::with_rows(&store, |rows| {
            let Some(row) = rows.support_requests.get(id) else {
                return unsupported("a request has no row");
            };
            Ok((row.status == RequestStatus::Pending && row.requested_minute + threshold == at).then(|| row.agent_id.clone()))
        })
    };
    let mut due: Vec<(String, Bound<'py, PyAny>)> = Vec::new();
    let id_name = intern!(py, "id");
    for request in open.try_iter()? {
        let request = request?;
        let id = attribute(&request, id_name)?;
        let text = exact_str(&id)?;
        if due_of(text)?.is_some() {
            due.push((text.to_owned(), id));
        }
    }
    due.sort_unstable_by(|first, second| first.0.cmp(&second.0));
    for (text, id) in due {
        // A decision before this one may have settled the request.
        let Some(agent_id) = due_of(&text).map_err(|error| {
            if error.is_instance_of::<Unsupported>(py) {
                pyo3::exceptions::PyRuntimeError::new_err("a request left its row in a complaint's decision")
            } else {
                error
            }
        })?
        else {
            continue;
        };
        let kwargs = PyDict::new(py);
        kwargs.set_item("agent_id", &*agent_id)?;
        kwargs.set_item("request_id", id)?;
        policy.call_method(intern!(py, "consider_complaint_response"), (&world,), Some(&kwargs))?;
    }
    Ok(())
}

/// `StaffService.assign_pending` when every pending request stays pending: it records the attempt
/// and the wait's reason on each, and the minute's exposure. None, with nothing changed, as soon as
/// `assign_work` is what the step needs: a request has a qualified free worker or a priority, its
/// writes are reported, or the step validates.
#[pyfunction]
#[pyo3(signature = (service, minute, *, validate_state=true))]
fn assign_none<'py>(
    service: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    validate_state: bool,
) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let py = service.py();
    if validate_state {
        return Ok(None);
    }
    let world = attribute(service, intern!(py, "world"))?;
    let at = exact_int(minute)?;
    if at != minute_of(&world)? {
        return Ok(None);
    }
    let table = table_of(&world)?;
    let open = world.call_method0(intern!(py, "unsettled_support_requests"))?;
    let kinds = CareKinds::of(&table)?;
    let priorities = priorities_of(&table)?;
    let workers = workers_at(&world, at)?;
    let store = entities_of(&world)?;
    let map = world.getattr(intern!(py, "support_requests"))?;

    // The plan: which requests are pending, what each may be served by.
    struct Pending<'py> {
        object: Bound<'py, PyAny>,
        id: Bound<'py, PyAny>,
        is_pending: bool,
        same_reason: bool,
        same_minute: bool,
    }
    let id_name = intern!(py, "id");
    let mut requests: Vec<Pending<'py>> = Vec::new();
    for request in open.try_iter()? {
        let object = request?;
        let id = attribute(&object, id_name)?;
        exact_str(&id)?;
        requests.push(Pending { object, id, is_pending: false, same_reason: false, same_minute: false });
    }
    EntityStore::with_rows(&store, |rows| {
        for request in &mut requests {
            let Some(row) = rows.support_requests.get(exact_str(&request.id)?) else {
                return unsupported("a request has no row");
            };
            request.is_pending = row.status == RequestStatus::Pending;
            request.same_reason = row.last_assignment_wait_reason_code.as_deref() == Some(WAIT_REASON);
            request.same_minute = row.last_assignment_attempt_minute == Some(at);
        }
        Ok(())
    })?;
    requests.retain(|request| request.is_pending);
    let mut exposures: Vec<(String, (bool, bool, bool))> = Vec::new();
    let mut no_qualified = 0.0;
    let mut busy = 0.0;
    let mut off_shift = 0.0;
    let listener_name = intern!(py, "_write_listener");
    for request in &requests {
        if !raw_dict_get(&map, &request.id)?.is_some_and(|object| object.is(&request.object)) {
            return Ok(None);
        }
        if !attribute(&request.object, listener_name)?.is_none() {
            return Ok(None);
        }
        let kind = kinds.kind_of(&request.id)?;
        let kind = match &kind {
            Some(kind) => exact_str(kind)?,
            None => DEFAULT_KIND,
        };
        let exposure = match exposures.iter().find(|(known, _)| known == kind) {
            Some((_, exposure)) => *exposure,
            None => {
                let Some(exposure) = kind_exposure(&workers, kind) else {
                    return Ok(None);
                };
                exposures.push((kind.to_owned(), exposure));
                exposure
            }
        };
        if is_urgent(priorities.as_ref(), &request.id)? {
            return Ok(None);
        }
        no_qualified += f64::from(u8::from(exposure.0));
        busy += f64::from(u8::from(exposure.1));
        off_shift += f64::from(u8::from(exposure.2));
    }
    let metrics = field_mirror_of(&world, intern!(py, "metric_state"))?;
    let record_metrics = NamespaceMirror::with_row::<MetricRow, _>(&metrics, |row| {
        Ok(row.get(ATTEMPT_METRIC).copied().unwrap_or(-1.0) != at as f64)
    })?;
    if at.abs() >= FLOAT_EXACT_INT {
        return unsupported("the minute does not fit a float");
    }

    // The writes.
    if record_metrics {
        NamespaceMirror::with_row_tree::<MetricRow, _>(&metrics, |row, tree| {
            let sum = row.get(NO_QUALIFIED_METRIC).copied().unwrap_or(0.0) + no_qualified;
            map_set(row, tree, NO_QUALIFIED_METRIC, sum)
        })?;
    }
    EntityStore::with_rows(&store, |rows| {
        let reason: Arc<str> = Arc::from(WAIT_REASON);
        for request in &requests {
            if request.same_minute && request.same_reason {
                continue;
            }
            let Some(row) = rows.support_requests.get_mut(exact_str(&request.id)?) else {
                return unsupported("a request has no row");
            };
            row.last_assignment_attempt_minute = Some(at);
            if !request.same_reason {
                row.last_assignment_wait_reason_code = Some(reason.clone());
            }
        }
        Ok(())
    })?;
    let attempt_name = intern!(py, "last_assignment_attempt_minute");
    let reason_name = intern!(py, "last_assignment_wait_reason_code");
    let reason = intern!(py, WAIT_REASON);
    for request in &requests {
        if !request.same_minute {
            raw_setattr(&request.object, attempt_name, minute.as_ptr())?;
        }
        if !request.same_reason {
            raw_setattr(&request.object, reason_name, reason.as_ptr())?;
        }
    }
    if record_metrics {
        NamespaceMirror::with_row_tree::<MetricRow, _>(&metrics, |row, tree| {
            for (metric, count) in [(BUSY_METRIC, busy), (OFF_SHIFT_METRIC, off_shift)] {
                if count != 0.0 {
                    let sum = row.get(metric).copied().unwrap_or(0.0) + count;
                    map_set(row, tree, metric, sum)?;
                }
            }
            map_set(row, tree, ATTEMPT_METRIC, at as f64)
        })?;
    }
    Ok(Some(PyTuple::empty(py)))
}

// ---- What the metrics read of each open request ---------------------------------------------------

/// What `_support_wait_snapshot` needs of the qualified staff of one kind.
struct KindWait {
    kind: String,
    qualified: bool,
    /// The sorted blockers of `support_wait_reason_codes` that come from the staff.
    blockers: Vec<&'static str>,
    /// `sorted({person.zone_id for person in _route_candidates(...)})`.
    destinations: Vec<Arc<str>>,
}

fn kind_wait(workers: &[Worker], kind: &str, sample: i64) -> KindWait {
    let required = required_skills(kind);
    let mut qualified = false;
    let mut unavailable = false;
    let mut staff_busy = false;
    let mut other_operation = false;
    let mut resting = false;
    let mut off_shift = false;
    let mut destinations = BTreeSet::new();
    for worker in workers {
        if !worker.skills.iter().any(|skill| required.contains(&&**skill)) {
            continue;
        }
        qualified = true;
        if !worker.has_task && worker.available && worker.can_start && worker.on_shift {
            destinations.insert(worker.zone.clone());
        }
        unavailable |= !worker.available;
        staff_busy |= worker.has_task;
        other_operation |= worker.on_operation;
        if worker.rest_until.is_some_and(|until| until > sample) {
            resting = true;
        } else if !worker.on_shift {
            off_shift = true;
        }
    }
    // Sorted, as `tuple(sorted(blockers))`.
    let mut blockers = Vec::with_capacity(5);
    if staff_busy {
        blockers.push("staff_busy");
    }
    if off_shift {
        blockers.push("staff_off_shift");
    }
    if other_operation {
        blockers.push("staff_on_other_operation");
    }
    if resting {
        blockers.push("staff_resting");
    }
    if unavailable {
        blockers.push("staff_unavailable");
    }
    KindWait { kind: kind.to_owned(), qualified, blockers, destinations: destinations.into_iter().collect() }
}

/// What `_support_route_wait_reasons` reads once for all the requests of a snapshot. Rebuilt for each
/// snapshot where one has a route to look at, as the Python rule builds it for each request.
struct RouteReads {
    graph: RouteRows,
    loads: EdgeLoads,
    has_plan: bool,
    /// `model_configs["staff"].get("support_task_minutes", 10)`.
    service_minutes: i64,
    duration: i64,
    closed: Vec<(&'static str, Arc<HashSet<Arc<str>>>)>,
}

impl RouteReads {
    fn of(world: &Bound<'_, PyAny>, minute: i64) -> PyResult<Self> {
        let py = world.py();
        let configs = attribute(world, intern!(py, "model_configs"))?;
        let configs = exact_dict(&configs)?;
        let service_minutes = match configs.get_item(intern!(py, "staff"))? {
            Some(staff) => match exact_dict(&staff)?.get_item(intern!(py, "support_task_minutes"))? {
                Some(minutes) => exact_int(&minutes)?,
                None => DEFAULT_SUPPORT_MINUTES,
            },
            None => DEFAULT_SUPPORT_MINUTES,
        };
        Ok(Self {
            graph: world_graph(world)?,
            loads: active_edge_loads(world, minute)?,
            has_plan: configs.contains(intern!(py, "facility_geometry"))?,
            service_minutes,
            duration: exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?,
            closed: Vec::new(),
        })
    }

    fn closed_to(&mut self, world: &Bound<'_, PyAny>, mobility: &'static str) -> PyResult<Arc<HashSet<Arc<str>>>> {
        if let Some((_, closed)) = self.closed.iter().find(|(name, _)| *name == mobility) {
            return Ok(closed.clone());
        }
        let closed = if self.has_plan { self.graph.edges_closed_by(&*Geo::of(world)?, mobility) } else { HashSet::new() };
        let closed = Arc::new(closed);
        self.closed.push((mobility, closed.clone()));
        Ok(closed)
    }
}

/// `_support_route_wait_reasons`, of a person with candidates: the reasons, and whether a route is
/// feasible.
fn route_wait_reasons(
    world: &Bound<'_, PyAny>,
    reads: &mut RouteReads,
    agent_id: &str,
    destinations: &[Arc<str>],
    minute: i64,
) -> PyResult<(BTreeSet<&'static str>, bool)> {
    let py = world.py();
    let store = entities_of(world)?;
    let (zone, mobility) = EntityStore::with_rows(&store, |rows| {
        let Some(agent) = rows.agents.get(agent_id) else {
            return unsupported("no such agent");
        };
        Ok((agent.zone_id.clone(), mobility_text(agent)))
    })?;
    let closed = reads.closed_to(world, mobility)?;
    let zones = attribute(world, intern!(py, "zones"))?;
    let mut reasons = BTreeSet::new();
    let mut feasible = false;
    for destination in destinations {
        let Some(route) = reads.graph.shortest(&closed, Some(&reads.loads), &zone, destination)? else {
            let free = reads.graph.shortest(&closed, None, &zone, destination)?;
            reasons.insert(if free.is_some() { "route_capacity_full" } else { "no_route_to_qualified_staff" });
            continue;
        };
        let zone_ids: Vec<&str> = route.zone_ids.iter().map(|id| &**id).collect();
        let edge_ids: Vec<&str> = route.edge_ids.iter().map(|id| &**id).collect();
        let minutes = movement_minutes(world, agent_id, &zone_ids, &edge_ids, route.minutes)?;
        let Some(place) = raw_dict_get(&zones, PyString::new(py, destination).as_any())? else {
            return unsupported("no such zone");
        };
        let capacity = attribute(&place, intern!(py, "capacity"))?;
        if *destination != zone && !capacity.is_none() {
            let capacity = exact_int(&capacity)?;
            let (occupied, incoming) = EntityStore::with_rows(&store, |rows| {
                let (mut occupied, mut incoming) = (0_i64, 0_i64);
                for (_, person) in rows.agents.iter() {
                    occupied += i64::from(person.zone_id == *destination);
                    incoming += i64::from(person.activity == Activity::Moving && person.target_id.as_ref() == Some(destination));
                }
                Ok((occupied, incoming))
            })?;
            if occupied + incoming >= capacity {
                reasons.insert("staff_destination_full");
                continue;
            }
        }
        if minute.saturating_add(minutes).saturating_add(reads.service_minutes) >= reads.duration {
            reasons.insert("insufficient_run_time");
            continue;
        }
        feasible = true;
    }
    Ok((reasons, feasible))
}

/// The reason codes of an open request.
enum WaitReason {
    /// `waits[index]`, whose qualified staff are none of them free.
    OfKind(usize),
    One(&'static str),
    /// What the routes of the free ones add.
    Routed(Vec<Arc<str>>),
}

/// What the metrics read of one open request.
pub(crate) struct OpenWait<'py> {
    pub(crate) agent: Arc<str>,
    pub(crate) id: Bound<'py, PyAny>,
    pub(crate) waited: i64,
    reason: WaitReason,
}

/// The requests of `_support_wait_snapshot`, in the order given.
pub(crate) struct OpenWaits<'py> {
    pub(crate) requests: Vec<OpenWait<'py>>,
    waits: Vec<KindWait>,
}

impl OpenWaits<'_> {
    pub(crate) fn reasons_of<'a>(&'a self, request: &'a OpenWait<'_>) -> Vec<&'a str> {
        match &request.reason {
            WaitReason::OfKind(kind) => {
                let wait = &self.waits[*kind];
                if !wait.qualified {
                    vec!["no_qualified_staff"]
                } else if wait.blockers.is_empty() {
                    vec!["no_route_to_qualified_staff"]
                } else {
                    wait.blockers.clone()
                }
            }
            WaitReason::One(reason) => vec![*reason],
            WaitReason::Routed(reasons) => reasons.iter().map(|reason| &**reason).collect(),
        }
    }

    pub(crate) fn has_no_qualified_staff(&self, request: &OpenWait<'_>) -> bool {
        matches!(request.reason, WaitReason::OfKind(kind) if !self.waits[kind].qualified)
    }
}

/// `support_wait_reason_codes` of a pending request whose kind has free staff, past its routes.
#[allow(clippy::too_many_arguments)]
fn routed_reasons(
    world: &Bound<'_, PyAny>,
    reads: &mut Option<RouteReads>,
    wait: &KindWait,
    agent_exists: bool,
    agent_id: &str,
    last_attempt: Option<i64>,
    last_reason: Option<&Arc<str>>,
    sample: i64,
) -> PyResult<Vec<Arc<str>>> {
    let (route_reasons, feasible) = if agent_exists {
        if reads.is_none() {
            *reads = Some(RouteReads::of(world, sample)?);
        }
        let Some(known) = reads.as_mut() else {
            return unsupported("the routes were not read");
        };
        route_wait_reasons(world, known, agent_id, &wait.destinations, sample)?
    } else {
        (BTreeSet::new(), false)
    };
    let mut blockers: BTreeSet<&str> = wait.blockers.iter().copied().collect();
    blockers.extend(route_reasons);
    if !blockers.is_empty() {
        return Ok(blockers.into_iter().map(Arc::from).collect());
    }
    if !feasible {
        return Ok(vec![Arc::from("no_route_to_qualified_staff")]);
    }
    if last_attempt == Some(sample) {
        return Ok(vec![match last_reason {
            Some(reason) if !reason.is_empty() => reason.clone(),
            _ => Arc::from("assignment_failure_reason_not_recorded"),
        }]);
    }
    let py = world.py();
    let metrics = field_mirror_of(world, intern!(py, "metric_state"))?;
    let attempted = NamespaceMirror::with_row::<MetricRow, _>(&metrics, |row| Ok(row.get(ATTEMPT_METRIC).copied().unwrap_or(-1.0) == sample as f64))?;
    Ok(vec![Arc::from(if attempted { "assignment_failure_reason_not_recorded" } else { "assignment_not_attempted_at_sampled_minute" })])
}

/// `support_waited_minutes` and `support_wait_reason_codes` of each request, or with `only_open`
/// of each one that is pending or assigned.
pub(crate) fn open_waits<'py>(
    world: &Bound<'py, PyAny>,
    list: &Bound<'py, PyList>,
    sample: i64,
    only_open: bool,
) -> PyResult<OpenWaits<'py>> {
    let py = world.py();
    let table = table_of(world)?;
    let kinds = CareKinds::of(&table)?;
    let store = entities_of(world)?;
    let mut waits: Vec<KindWait> = Vec::new();
    let mut workers: Option<Vec<Worker>> = None;
    let mut reads: Option<RouteReads> = None;
    let mut requests: Vec<OpenWait<'py>> = Vec::with_capacity(list.len());
    let id_name = intern!(py, "id");
    for request in list.iter() {
        let id_object = attribute(&request, id_name)?;
        let id = exact_str(&id_object)?;
        let read = EntityStore::with_rows(&store, |rows| {
            let Some(request) = rows.support_requests.get(id) else {
                return unsupported("a request has no row");
            };
            let agent = rows.agents.get(&request.agent_id);
            let moving = agent.is_some_and(|agent| agent.activity == Activity::Moving);
            let mut endpoint = sample;
            if request.status == RequestStatus::Assigned {
                let task = rows.active_tasks.get(request.task_id.as_deref().unwrap_or(""));
                if let (Some(_), false, Some(task)) = (agent, moving, task) {
                    endpoint = sample.min(task.start_minute);
                }
            }
            let Some(waited) = endpoint.checked_sub(request.requested_minute) else {
                return unsupported("the wait does not fit an i64");
            };
            Ok((
                request.agent_id.clone(),
                request.status,
                moving,
                waited.max(0),
                agent.is_some(),
                request.last_assignment_attempt_minute,
                request.last_assignment_wait_reason_code.clone(),
            ))
        })?;
        let (agent, status, moving, waited, agent_exists, last_attempt, last_reason) = read;
        if only_open && !matches!(status, RequestStatus::Pending | RequestStatus::Assigned) {
            continue;
        }
        let reason = match status {
            RequestStatus::Assigned => WaitReason::One(if moving { "resident_travel" } else { "support_in_progress" }),
            RequestStatus::Pending => {
                let kind = kinds.kind_of(&id_object)?;
                let kind = match &kind {
                    Some(kind) => exact_str(kind)?,
                    None => DEFAULT_KIND,
                };
                let index = match waits.iter().position(|wait| wait.kind == kind) {
                    Some(index) => index,
                    None => {
                        if workers.is_none() {
                            workers = Some(workers_at(world, sample)?);
                        }
                        waits.push(kind_wait(workers.as_deref().unwrap_or(&[]), kind, sample));
                        waits.len() - 1
                    }
                };
                if waits[index].destinations.is_empty() {
                    WaitReason::OfKind(index)
                } else {
                    WaitReason::Routed(routed_reasons(world, &mut reads, &waits[index], agent_exists, &agent, last_attempt, last_reason.as_ref(), sample)?)
                }
            }
            _ => WaitReason::One("request_closed"),
        };
        requests.push(OpenWait { agent, id: id_object, waited, reason });
    }
    Ok(OpenWaits { requests, waits })
}

/// `_support_wait_snapshot` in `experiments/metrics.py`, for the snapshots `rules/metrics.rs`
/// leaves to the Python rule.
#[pyfunction]
fn wait_snapshot<'py>(
    world: &Bound<'py, PyAny>,
    active_support: &Bound<'py, PyAny>,
    sample_minute: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyDict>, Bound<'py, PyList>)> {
    let py = world.py();
    let sample = exact_int(sample_minute)?;
    let list = active_support.downcast_exact::<PyList>().map_err(|_| Unsupported::new_err("the requests are not a list"))?;
    let open = open_waits(world, list, sample, false)?;
    let output = &open.requests;
    // `sorted(active_support, key=agent_id)`, stable.
    let mut order: Vec<usize> = (0..output.len()).collect();
    order.sort_by(|first, second| output[*first].agent.as_bytes().cmp(output[*second].agent.as_bytes()));
    let details = PyDict::new(py);
    for index in &order {
        let row = &output[*index];
        let detail = PyDict::new(py);
        detail.set_item(intern!(py, "request_id"), &row.id)?;
        detail.set_item(intern!(py, "waited_minutes"), row.waited)?;
        detail.set_item(intern!(py, "reason_codes"), PyList::new(py, open.reasons_of(row))?)?;
        details.set_item(&*row.agent, detail)?;
    }
    let mut no_qualified: Vec<&str> = output
        .iter()
        .filter(|row| open.has_no_qualified_staff(row))
        .map(|row| &*row.agent)
        .collect();
    no_qualified.sort_unstable();
    Ok((details, PyList::new(py, no_qualified)?))
}

// ---- domain/staff_operations.py --------------------------------------------------------------------

/// The value under `name` of a config document.
fn config_item<'a>(config: &'a V, name: &str) -> PyResult<&'a V> {
    match config {
        V::Map(config) => match config.get(name) {
            Some(value) => Ok(value),
            None => unsupported("the config lacks a key"),
        },
        _ => unsupported("the config is not a map"),
    }
}

/// `config[table].get(zone, {}).get(name)`. A table that is `required` is read as `config[table]`.
fn zone_override<'a>(config: &'a V, table: &str, zone: &str, name: &str, required: bool) -> PyResult<Option<&'a V>> {
    let V::Map(document) = config else {
        return unsupported("the config is not a map");
    };
    let Some(table) = document.get(table) else {
        return if required { unsupported("the config lacks a table") } else { Ok(None) };
    };
    let V::Map(table) = table else {
        return unsupported("a table of the config is not a map");
    };
    match table.get(zone) {
        None => Ok(None),
        Some(V::Map(entry)) => Ok(entry.get(name)),
        Some(_) => unsupported("an entry of the config is not a map"),
    }
}

fn exact_rate(value: &V) -> PyResult<f64> {
    match value {
        V::Float(rate) => Ok(*rate),
        _ => unsupported("a rate is not a float"),
    }
}

/// A threshold a comparison with a float reads: both types compare as their value does.
fn threshold(value: Option<&V>, default: f64) -> PyResult<f64> {
    match value {
        None => Ok(default),
        Some(V::Float(number)) => Ok(*number),
        Some(V::Int(number)) if number.abs() < FLOAT_EXACT_INT => Ok(*number as f64),
        Some(_) => unsupported("a threshold is not a number"),
    }
}

/// The IDs of `world.zones`.
fn world_zones(world: &Bound<'_, PyAny>) -> PyResult<Vec<Arc<str>>> {
    let zones = attribute(world, intern!(world.py(), "zones"))?;
    exact_dict(&zones)?.keys().iter().map(|key| Ok(Arc::from(exact_str(&key)?))).collect()
}

/// `update_staff_operations`: each zone's sanitation falls with who is in it.
#[pyfunction]
pub(crate) fn update_operations(world: &Bound<'_, PyAny>, minute: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(());
    }
    let at = exact_int(minute)?;
    let zones = world_zones(world)?;
    let mirror = mirror_held(world, NAME)?;
    let loads = EntityStore::with_rows(&entities_of(world)?, |rows| {
        let mut loads: FxHashMap<&str, i64> = FxHashMap::default();
        for (_, agent) in rows.agents.iter() {
            *loads.entry(&agent.zone_id).or_insert(0) += 1;
        }
        Ok(zones.iter().map(|zone| loads.get(&**zone).copied().unwrap_or(0)).collect::<Vec<i64>>())
    })?;
    let plan = NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror, |row| {
        let mut plan = Vec::with_capacity(zones.len());
        let global = config_item(&row.config, "sanitation_use_per_person_minute")?;
        for (zone_id, load) in zones.iter().zip(&loads) {
            let Some((index, _, record)) = row.zones.get_full(zone_id) else {
                return unsupported("a zone has no record");
            };
            let Some(elapsed) = at.checked_sub(record.updated_minute) else {
                return unsupported("the time since the update does not fit an i64");
            };
            let elapsed = elapsed.max(0);
            if elapsed >= FLOAT_EXACT_INT {
                return unsupported("the time since the update does not fit a float");
            }
            let rate = zone_override(&row.config, "effects_by_zone", zone_id, "sanitation_use_per_person_minute", true)?
                .unwrap_or(global);
            let rate = exact_rate(rate)?;
            // `load * rate * elapsed`, from the left.
            let used = *load as f64 * rate * elapsed as f64;
            plan.push((index, py_max(0.0, record.sanitation - used)));
        }
        Ok(plan)
    })?;
    NamespaceMirror::with_row_tree::<StaffOperationsRow, _>(&mirror, |row, tree| {
        let zones_tree = tree.child(intern!(py, "zones"))?;
        for (index, sanitation) in plan {
            let Some((id, record)) = row.zones.get_index_mut(index) else {
                return unsupported("a zone has no record");
            };
            if record.sanitation.to_bits() != sanitation.to_bits() || record.updated_minute != at {
                let node = zones_tree.child_str(id)?;
                record.set_sanitation(&node, sanitation)?;
                record.set_updated_minute(&node, at)?;
            }
        }
        Ok(())
    })
}

/// Both rules of the staff's tasks round each member's fatigue on their way, which raises for this.
fn every_fatigue_is_finite(world: &Bound<'_, PyAny>) -> PyResult<()> {
    if namespace_is_absent(world, intern!(world.py(), STAFF_DYNAMICS))? {
        return Ok(());
    }
    NamespaceMirror::with_row::<StaffDynamicsRow, _>(&mirror_held(world, STAFF_DYNAMICS)?, |row| {
        if row.staff.values().all(|record| record.fatigue.is_finite()) {
            Ok(())
        } else {
            unsupported("a fatigue is not finite")
        }
    })
}

/// `format(value, "g")` of a float, or of an int as a float: six significant digits.
fn g_text(value: f64) -> PyResult<String> {
    if !value.is_finite() {
        return unsupported("a threshold is not finite");
    }
    if value == 0.0 {
        return Ok(if value.is_sign_negative() { "-0" } else { "0" }.to_owned());
    }
    // Both round the exact value of the double half to even, as CPython's `%g` does.
    let scientific = format!("{value:.5e}");
    let Some((mantissa, exponent)) = scientific.split_once('e') else {
        return unsupported("a float with no exponent");
    };
    let exponent: i32 = exponent.parse().map_err(|_| Unsupported::new_err("a float with no exponent"))?;
    fn trimmed(text: &str) -> &str {
        if text.contains('.') {
            text.trim_end_matches('0').trim_end_matches('.')
        } else {
            text
        }
    }
    if (-4..6).contains(&exponent) {
        let fixed = format!("{:.*}", (5 - exponent) as usize, value);
        return Ok(trimmed(&fixed).to_owned());
    }
    let sign = if exponent < 0 { '-' } else { '+' };
    Ok(format!("{}e{sign}{:02}", trimmed(mantissa), exponent.abs()))
}

/// `format(value, "g")`, to compare with Python's in the tests.
#[pyfunction]
fn format_g(value: f64) -> PyResult<String> {
    g_text(value)
}

/// The people of a zone: their stress, and the uncertainty of those with a body record.
#[derive(Default)]
struct ZonePeople {
    stress: Vec<f64>,
    uncertainty: Vec<f64>,
}

/// Who is in each zone, in the order of the zones' IDs.
fn zone_people(world: &Bound<'_, PyAny>) -> PyResult<BTreeMap<Arc<str>, ZonePeople>> {
    let py = world.py();
    let store = entities_of(world)?;
    let body = if namespace_is_absent(world, intern!(py, BODY_STATE))? { None } else { Some(mirror_held(world, BODY_STATE)?) };
    let mut people: BTreeMap<Arc<str>, ZonePeople> = BTreeMap::new();
    EntityStore::with_rows(&store, |rows| {
        let mut read = |body: Option<&BodyRow>| {
            for (id, agent) in rows.agents.iter() {
                let zone = match people.get_mut(&agent.zone_id) {
                    Some(zone) => zone,
                    None => people.entry(agent.zone_id.clone()).or_default(),
                };
                zone.stress.push(agent.stress);
                if let Some(record) = body.and_then(|body| body.agents.get(id)) {
                    zone.uncertainty.push(record.uncertainty);
                }
            }
        };
        match &body {
            Some(body) => NamespaceMirror::with_row::<BodyRow, _>(body, |body| {
                read(Some(&*body));
                Ok(())
            }),
            None => {
                read(None);
                Ok(())
            }
        }
    })?;
    Ok(people)
}

/// `math.fsum(values) / len(values)`, of at least one value.
fn mean_of(values: &[f64], refusal: &'static str) -> PyResult<f64> {
    match py_fsum(values) {
        Some(sum) => Ok(sum / values.len() as f64),
        None => unsupported(refusal),
    }
}

fn is_free_for_a_task(worker: &Worker) -> bool {
    !worker.has_task && !worker.on_operation && worker.available && worker.can_start
}

fn has_skill(worker: &Worker, skills: &[&str]) -> bool {
    worker.skills.iter().any(|skill| skills.contains(&&**skill))
}

const CLEANING_SKILLS: [&str; 2] = ["cleaning", "sanitation"];
const MAINTENANCE_SKILLS: [&str; 2] = ["maintenance", "repair"];
const OUTREACH_SKILLS: [&str; 2] = ["support", "information"];
const PATROL_SKILLS: [&str; 2] = ["support", "patrol"];

/// `assign_staff_operations`: the plain tick where no free worker has a task to take, which changes
/// nothing and reads no route, and the rest.
#[pyfunction]
pub(crate) fn assign_operations<'py>(
    world: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    router: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let py = world.py();
    let done = PyTuple::empty(py);
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(done);
    }
    let at = exact_int(minute)?;
    let configs = attribute(world, intern!(py, "model_configs"))?;
    if let Some(registry) = exact_dict(&configs)?.get_item(intern!(py, "policy_registry"))? {
        if let Some(selected) = exact_dict(&registry)?.get_item(intern!(py, "staff_operations"))? {
            if !matches!(exact_str(&selected)?, "staff-urgency-first-v1" | "staff-travel-first-v1") {
                return unsupported("another staff operations formula");
            }
        }
    }
    let workers = workers_at(world, at)?;
    let store = entities_of(world)?;
    let free: Vec<&Worker> = workers.iter().filter(|worker| is_free_for_a_task(worker)).collect();
    let has = |skills: &[&str]| free.iter().any(|worker| has_skill(worker, skills));
    let cleaners = has(&CLEANING_SKILLS);
    let repairers = has(&MAINTENANCE_SKILLS);
    let outreach = has(&OUTREACH_SKILLS);
    let patrol = has(&PATROL_SKILLS);
    let mirror = mirror_held(world, NAME)?;
    every_fatigue_is_finite(world)?;
    let a_task_is_open = NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror, |row| {
        // `state["staff"][staff_id]` raises for one without a record.
        if workers.iter().any(|worker| !worker.has_task && !row.staff.contains_key(&worker.id)) {
            return unsupported("a staff member has no record");
        }
        let reserved = |kind: &str, zone: &str| {
            row.staff.values().any(|record| {
                record.phase != Phase::Idle
                    && record.task_kind.as_deref() == Some(kind)
                    && record.target_zone_id.as_deref() == Some(zone)
                    && record.target_id.is_none()
            })
        };
        if cleaners {
            for (zone, record) in &row.zones {
                let below = threshold(
                    zone_override(&row.config, "thresholds_by_zone", zone, "cleaning_sanitation_below", false)?,
                    CLEANING_BELOW,
                )?;
                if record.sanitation < below && !reserved("cleaning", zone) {
                    return Ok(true);
                }
            }
        }
        if repairers {
            let any_deficit = EntityStore::with_rows(&store, |rows| {
                Ok(rows.facilities.iter().any(|(_, facility)| facility.capacity > facility.working_units))
            })?;
            if any_deficit {
                return Ok(true);
            }
        }
        if !outreach && !patrol {
            return Ok(false);
        }
        let people = zone_people(world)?;
        if outreach {
            for (zone, group) in &people {
                if group.uncertainty.is_empty() {
                    continue;
                }
                let mean = mean_of(&group.uncertainty, "an uncertainty is not finite")?;
                let at_least = threshold(
                    zone_override(&row.config, "thresholds_by_zone", zone, "outreach_uncertainty_at_least", false)?,
                    OUTREACH_AT_LEAST,
                )?;
                if mean >= at_least && !reserved("information_outreach", zone) {
                    return Ok(true);
                }
            }
        }
        if patrol {
            for zone in world_zones(world)? {
                let Some(group) = people.get(&zone) else {
                    continue;
                };
                let mean = mean_of(&group.stress, "a stress is not finite")?;
                let at_least = threshold(
                    zone_override(&row.config, "thresholds_by_zone", &zone, "patrol_stress_at_least", false)?,
                    PATROL_AT_LEAST,
                )?;
                if mean >= at_least && !reserved("patrol", &zone) {
                    return Ok(true);
                }
            }
        }
        Ok(false)
    })?;
    if !a_task_is_open {
        return Ok(done);
    }
    let plan = plan_operations(world, at, router, &workers);
    let plan = refused_by_python(py, plan)?;
    write_operations(world, minute, at, &plan, &workers)
}

/// A task a worker could take, with what `_TASK_SKILLS` asks of them.
struct OpenTask {
    urgency: f64,
    kind: &'static str,
    zone: Arc<str>,
    target: Option<Arc<str>>,
    reason: String,
    skills: &'static [&'static str; 2],
}

/// One start of `assign_staff_operations`, nothing changed yet.
struct PlannedOperation {
    worker: usize,
    kind: &'static str,
    zone: Arc<str>,
    target: Option<Arc<str>>,
    reason: String,
    route: Route,
    due: i64,
}

struct PlannedOperations<'py> {
    starts: Vec<PlannedOperation>,
    source: Bound<'py, PyAny>,
    policy: Bound<'py, PyAny>,
    event_source: Bound<'py, PyAny>,
}

/// `(recent, total, round(1000 * fatigue))` of each member by ID: how both rules rotate the staff.
fn workload_of(world: &Bound<'_, PyAny>, row: &StaffOperationsRow, workers: &[Worker], at: i64) -> PyResult<Vec<(i64, i64, i64)>> {
    let cutoff = 0.max(at - 240);
    let mut counts: FxHashMap<&str, (i64, i64)> = FxHashMap::default();
    for item in &row.history {
        let entry = counts.entry(&item.staff_id).or_insert((0, 0));
        entry.0 += i64::from(item.complete_minute >= cutoff);
        entry.1 += 1;
    }
    let py = world.py();
    let dynamics = if namespace_is_absent(world, intern!(py, STAFF_DYNAMICS))? { None } else { Some(mirror_held(world, STAFF_DYNAMICS)?) };
    let mut rounds = Vec::with_capacity(workers.len());
    let round_of = |fatigue: f64| match py_round_int(1000.0 * fatigue) {
        Some(rounded) => Ok(rounded),
        None => unsupported("a fatigue does not round"),
    };
    match &dynamics {
        Some(dynamics) => NamespaceMirror::with_row::<StaffDynamicsRow, _>(dynamics, |dynamics| {
            for worker in workers {
                rounds.push(match dynamics.staff.get(&worker.id) {
                    Some(record) => round_of(record.fatigue)?,
                    None => 0,
                });
            }
            Ok(())
        })?,
        None => rounds.resize(workers.len(), 0),
    }
    Ok(workers
        .iter()
        .zip(rounds)
        .map(|(worker, rounded)| {
            let (recent, total) = counts.get(&*worker.id).copied().unwrap_or((0, 0));
            (recent, total, rounded)
        })
        .collect())
}

/// `create_staff_operations_formula` and what `assign_staff_operations` takes from it: its model, the
/// source and policy of the events' provenance, and their `source_id`.
fn formula_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<(bool, Bound<'py, PyAny>, Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let formula = py.import("sheltersim.policies.registry")?.getattr("create_staff_operations_formula")?.call1((&configs,))?;
    let selected = get(&configs, "policy_registry")?;
    let selected = !selected.is_none() && selected.contains("staff_operations")?;
    let model = attribute(&formula, intern!(py, "model_id"))?;
    let travel_first = exact_str(&model)? == "staff-travel-first-v1";
    if selected {
        let source = attribute(&formula, intern!(py, "source_id"))?;
        Ok((travel_first, source.clone(), model, source))
    } else {
        let policy = PyString::new(py, OPERATIONS_POLICY).into_any();
        Ok((travel_first, PyString::new(py, OPERATIONS_SOURCE).into_any(), policy.clone(), policy))
    }
}

/// The tasks `assign_staff_operations` would list for someone with every skill, in its order, without
/// the ones `reserved_operations` takes.
fn open_tasks(world: &Bound<'_, PyAny>, row: &StaffOperationsRow, workers: &[Worker]) -> PyResult<Vec<OpenTask>> {
    let py = world.py();
    let eligible = |skills: &[&str]| workers.iter().any(|worker| is_free_for_a_task(worker) && has_skill(worker, skills));
    let mut tasks = Vec::new();
    if eligible(&CLEANING_SKILLS) {
        // `sorted(state["zones"].items())`.
        let mut zones: Vec<(&Arc<str>, f64)> = row.zones.iter().map(|(zone, record)| (zone, record.sanitation)).collect();
        zones.sort_by(|first, second| first.0.cmp(second.0));
        for (zone, sanitation) in zones {
            let below = threshold(zone_override(&row.config, "thresholds_by_zone", zone, "cleaning_sanitation_below", false)?, CLEANING_BELOW)?;
            if sanitation < below {
                tasks.push(OpenTask {
                    urgency: 1.0 - sanitation,
                    kind: "cleaning",
                    zone: zone.clone(),
                    target: None,
                    reason: format!("sanitation_index_below_{}", g_text(below)?),
                    skills: &CLEANING_SKILLS,
                });
            }
        }
    }
    if eligible(&MAINTENANCE_SKILLS) {
        // `sorted(world.facilities.values(), key=id)`.
        let mut short = EntityStore::with_rows(&entities_of(world)?, |rows| {
            Ok(rows
                .facilities
                .iter()
                .filter(|(_, facility)| facility.capacity > facility.working_units)
                .map(|(id, facility)| (id.clone(), facility.zone_id.clone(), facility.capacity - facility.working_units))
                .collect::<Vec<_>>())
        })?;
        short.sort_by(|first, second| first.0.cmp(&second.0));
        for (id, zone, deficit) in short {
            if deficit >= FLOAT_EXACT_INT {
                return unsupported("a deficit does not fit a float");
            }
            let key = PyString::new(py, &id);
            if !active_outage(world, &id)? && !has_active_modifier(world, key.as_any())? {
                tasks.push(OpenTask {
                    urgency: deficit as f64,
                    kind: "maintenance",
                    zone,
                    target: Some(id),
                    reason: "facility_working_units_below_capacity".to_owned(),
                    skills: &MAINTENANCE_SKILLS,
                });
            }
        }
    }
    let outreach = eligible(&OUTREACH_SKILLS);
    let patrol = eligible(&PATROL_SKILLS);
    if outreach || patrol {
        let people = zone_people(world)?;
        if outreach {
            for (zone, group) in &people {
                if group.uncertainty.is_empty() {
                    continue;
                }
                let mean = mean_of(&group.uncertainty, "an uncertainty is not finite")?;
                let at_least = threshold(zone_override(&row.config, "thresholds_by_zone", zone, "outreach_uncertainty_at_least", false)?, OUTREACH_AT_LEAST)?;
                if mean >= at_least {
                    tasks.push(OpenTask {
                        urgency: mean,
                        kind: "information_outreach",
                        zone: zone.clone(),
                        target: None,
                        reason: format!("mean_information_uncertainty_at_least_{}", g_text(at_least)?),
                        skills: &OUTREACH_SKILLS,
                    });
                }
            }
        }
        if patrol {
            let mut zones = world_zones(world)?;
            zones.sort();
            for zone in zones {
                let Some(group) = people.get(&zone) else {
                    continue;
                };
                let mean = mean_of(&group.stress, "a stress is not finite")?;
                let at_least = threshold(zone_override(&row.config, "thresholds_by_zone", &zone, "patrol_stress_at_least", false)?, PATROL_AT_LEAST)?;
                if mean >= at_least {
                    tasks.push(OpenTask {
                        urgency: mean * 0.5,
                        kind: "patrol",
                        reason: format!("mean_zone_stress_at_least_{}", g_text(at_least)?),
                        zone,
                        target: None,
                        skills: &PATROL_SKILLS,
                    });
                }
            }
        }
    }
    Ok(tasks)
}

/// `formula.rank_key(...)` compared as Python compares the tuples. The minutes fit a float.
fn ranks_above(first: (f64, f64, &OpenTask), second: (f64, f64, &OpenTask)) -> bool {
    let text = |task: &OpenTask| (task.kind, task.zone.clone(), task.target.clone().unwrap_or_else(|| Arc::from("")));
    if first.0 != second.0 {
        return first.0 > second.0;
    }
    if first.1 != second.1 {
        return first.1 > second.1;
    }
    text(first.2) > text(second.2)
}

/// `assign_staff_operations`, changing nothing: who starts what, and on which route.
fn plan_operations<'py>(world: &Bound<'py, PyAny>, at: i64, router: &Bound<'py, PyAny>, workers: &[Worker]) -> PyResult<PlannedOperations<'py>> {
    let py = world.py();
    let (travel_first, source, policy, event_source) = formula_of(world)?;
    let graph = staff_router_rows(router)?;
    let mut loads = passage_edge_loads(world, at)?;
    let duration = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
    let mirror = mirror_held(world, NAME)?;
    let (tasks, workload, durations, mut reserved) = NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror, |row| {
        let mut durations = Vec::new();
        for kind in ["cleaning", "maintenance", "information_outreach", "intake", "patrol"] {
            let V::Map(table) = config_item(&row.config, "task_duration_minutes")? else {
                return unsupported("the task durations are not a map");
            };
            let Some(V::Int(minutes)) = table.get(kind) else {
                return unsupported("a task duration is not an int");
            };
            durations.push((kind, *minutes));
        }
        let reserved: HashSet<(Arc<str>, Arc<str>, Option<Arc<str>>)> = row
            .staff
            .values()
            .filter(|record| record.phase != Phase::Idle)
            .filter_map(|record| Some((record.task_kind.clone()?, record.target_zone_id.clone()?, record.target_id.clone())))
            .collect();
        Ok((open_tasks(world, row, workers)?, workload_of(world, row, workers, at)?, durations, reserved))
    })?;
    let mut order: Vec<usize> = (0..workers.len()).collect();
    order.sort_by(|first, second| (workload[*first], &workers[*first].id).cmp(&(workload[*second], &workers[*second].id)));
    let nothing_closed = HashSet::new();
    let mut starts = Vec::new();
    for index in order {
        let worker = &workers[index];
        if !is_free_for_a_task(worker) {
            continue;
        }
        let mut best: Option<(f64, f64, &OpenTask, Route)> = None;
        for task in &tasks {
            if !has_skill(worker, task.skills) || reserved.contains(&(Arc::from(task.kind), task.zone.clone(), task.target.clone())) {
                continue;
            }
            let Some(route) = graph.get().shortest(&nothing_closed, Some(&loads), &worker.zone, &task.zone)? else {
                continue;
            };
            if route.minutes >= FLOAT_EXACT_INT {
                return unsupported("a route's minutes do not fit a float");
            }
            let (first, second) = if travel_first { (-route.minutes as f64, task.urgency) } else { (task.urgency, -route.minutes as f64) };
            // `max` keeps the first of equal keys.
            if best.as_ref().is_none_or(|known| ranks_above((first, second, task), (known.0, known.1, known.2))) {
                best = Some((first, second, task, route));
            }
        }
        let Some((_, _, task, route)) = best else {
            continue;
        };
        let Some((_, work)) = durations.iter().find(|(kind, _)| *kind == task.kind) else {
            return unsupported("a task without a duration");
        };
        let Some(due) = at.checked_add(route.minutes).and_then(|arrival| arrival.checked_add(*work)) else {
            return unsupported("a due minute out of range");
        };
        if due >= duration {
            continue;
        }
        for edge in &route.edge_ids {
            *loads.entry(edge.clone()).or_insert(0) += 1;
        }
        reserved.insert((Arc::from(task.kind), task.zone.clone(), task.target.clone()));
        starts.push(PlannedOperation { worker: index, kind: task.kind, zone: task.zone.clone(), target: task.target.clone(), reason: task.reason.clone(), route, due });
    }
    Ok(PlannedOperations { starts, source, policy, event_source })
}

fn optional_text<'py>(py: Python<'py>, text: Option<&Arc<str>>) -> Bound<'py, PyAny> {
    match text {
        Some(text) => PyString::new(py, text).into_any(),
        None => py.None().into_bound(py),
    }
}

/// What `assign_staff_operations` writes, in its order: one record at a time on the forwarded writes.
fn write_operations<'py>(
    world: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    at: i64,
    plan: &PlannedOperations<'py>,
    workers: &[Worker],
) -> PyResult<Bound<'py, PyTuple>> {
    let py = world.py();
    let table = table_of(world)?;
    let records = get(&get(&table, NAME)?, "staff")?;
    let staff_map = attribute(world, intern!(py, "staff"))?;
    let tasks = attribute(world, intern!(py, "active_tasks"))?;
    let mut assigned = Vec::with_capacity(plan.starts.len());
    for start in &plan.starts {
        let worker = &workers[start.worker];
        let travel = start.route.minutes;
        let arrival = at + travel;
        let sequence: i64 = world.call_method0(intern!(py, "allocate_event_sequence"))?.extract()?;
        let task_id = format!("staff-operation-{sequence}");
        let target_zone = PyString::new(py, &start.zone);
        let target = optional_text(py, start.target.as_ref());
        let record = PyDict::new(py);
        record.set_item("phase", if travel != 0 { "travel" } else { "working" })?;
        record.set_item("task_id", &task_id)?;
        record.set_item("task_kind", start.kind)?;
        record.set_item("target_id", &target)?;
        record.set_item("target_zone_id", &target_zone)?;
        record.set_item("reason", &start.reason)?;
        record.set_item("started_minute", minute)?;
        record.set_item("travel_due_minute", if travel != 0 { Some(arrival) } else { None })?;
        record.set_item("arrival_minute", if travel != 0 { None } else { Some(arrival) })?;
        record.set_item("due_minute", start.due)?;
        let (zones, edges): (&[Arc<str>], &[Arc<str>]) = if travel != 0 { (&start.route.zone_ids, &start.route.edge_ids) } else { (&[], &[]) };
        record.set_item("route_zone_ids", PyList::new(py, zones.iter().map(|id| &**id))?)?;
        record.set_item("route_edge_ids", PyList::new(py, edges.iter().map(|id| &**id))?)?;
        record.set_item("travel_minutes", travel)?;
        record.set_item("causation_event_ids", PyList::empty(py))?;
        records.set_item(&*worker.id, record)?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("id", &task_id)?;
        kwargs.set_item("kind", "staff_operation")?;
        kwargs.set_item("agent_id", py.None())?;
        kwargs.set_item("resource_id", py.None())?;
        kwargs.set_item("start_minute", minute)?;
        kwargs.set_item("due_minute", start.due)?;
        kwargs.set_item("staff_id", &*worker.id)?;
        tasks.set_item(&task_id, active_task_class(py)?.call((), Some(&kwargs))?)?;
        let staff = staff_map.get_item(&*worker.id)?;
        staff.setattr(intern!(py, "active_task_id"), &task_id)?;
        attribute(&staff, intern!(py, "work_queue"))?.set_item(PySlice::full(py), PyList::new(py, [&task_id])?)?;
        staff.setattr(intern!(py, "busy_until"), start.due)?;
        if travel != 0 {
            let payload = PyDict::new(py);
            payload.set_item("staff_id", &*worker.id)?;
            payload.set_item("task_id", &task_id)?;
            payload.set_item("target_zone_id", &target_zone)?;
            let event = new_event(
                py,
                NewEvent {
                    event_id: &format!("staff-operation-arrive-{sequence}"),
                    kind: "COMPLETION",
                    due_minute: arrival,
                    priority: 5,
                    sequence,
                    event_type: "staff_operation_arrive",
                    payload: payload.into_any(),
                    source_id: Some(plan.event_source.clone()),
                    causation_id: None,
                    provenance: assumption(py, &plan.source, exact_str(&plan.policy)?)?.into_any(),
                },
            )?;
            world.call_method1(intern!(py, "schedule_event"), (event,))?;
        }
        let completion: i64 = world.call_method0(intern!(py, "allocate_event_sequence"))?.extract()?;
        let payload = PyDict::new(py);
        payload.set_item("staff_id", &*worker.id)?;
        payload.set_item("task_id", &task_id)?;
        payload.set_item("task_kind", start.kind)?;
        payload.set_item("target_id", &target)?;
        payload.set_item("target_zone_id", &target_zone)?;
        let provenance = assumption(py, &plan.source, exact_str(&plan.policy)?)?;
        provenance.set_item("reason", &start.reason)?;
        let event = new_event(
            py,
            NewEvent {
                event_id: &format!("staff-operation-complete-{completion}"),
                kind: "COMPLETION",
                due_minute: start.due,
                priority: 5,
                sequence: completion,
                event_type: "staff_operation_complete",
                payload: payload.into_any(),
                source_id: Some(plan.event_source.clone()),
                causation_id: None,
                provenance: provenance.into_any(),
            },
        )?;
        world.call_method1(intern!(py, "schedule_event"), (event,))?;
        assigned.push(task_id);
    }
    if !assigned.is_empty() && exact_dict(&table)?.contains(MOVEMENT)? {
        let kwargs = PyDict::new(py);
        kwargs.set_item("boundary_minute", at + 1)?;
        py.import("sheltersim.domain.movement")?.getattr("staff_position_snapshots")?.call((world,), Some(&kwargs))?;
    }
    PyTuple::new(py, assigned)
}

/// One handoff of `schedule_staff_operation_relief`, nothing changed yet.
struct PlannedRelief {
    task_id: Arc<str>,
    previous: Arc<str>,
    replacement: Arc<str>,
}

/// `schedule_staff_operation_relief`, changing nothing.
fn plan_relief(world: &Bound<'_, PyAny>, at: i64, next: i64, duration: i64) -> PyResult<Vec<PlannedRelief>> {
    let py = world.py();
    let writer = crate::state::events::EventWriter::of(world)?;
    let pending = writer.queue().borrow().pending_events(py)?;
    let Some(pending) = pending else {
        return unsupported("an event in the queue is not a plain one");
    };
    let mut handed: HashSet<Arc<str>> = HashSet::new();
    for event in &pending {
        if let Some(task) = crate::rules::travel::relief_task_of(event)? {
            handed.insert(task);
        }
    }
    let workers = workers_at(world, next)?;
    let store = entities_of(world)?;
    NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror_held(world, NAME)?, |row| {
        let workload = workload_of(world, row, &workers, at)?;
        let mut operations: Vec<(&Arc<str>, &crate::state::staff_operations::OperationRecord)> = row.staff.iter().collect();
        operations.sort_by(|first, second| first.0.cmp(second.0));
        let mut plans = Vec::new();
        for (staff_id, operation) in operations {
            if operation.phase == Phase::Idle {
                continue;
            }
            let Some(task_id) = &operation.task_id else {
                return unsupported("a staff member's task has no ID");
            };
            if handed.contains(task_id) {
                continue;
            }
            let Some(worker) = workers.iter().find(|worker| worker.id == *staff_id) else {
                return unsupported("a record of no staff member");
            };
            if worker.available && worker.on_shift {
                continue;
            }
            let holds = EntityStore::with_rows(&store, |rows| Ok(rows.active_tasks.get(&**task_id).is_some_and(|task| task.staff_id.as_ref() == Some(staff_id))))?;
            if !holds {
                continue;
            }
            let Some(due) = operation.due_minute else {
                return unsupported("a staff member's task has no due minute");
            };
            if due <= next {
                continue;
            }
            let (Some(target), Some(kind)) = (&operation.target_zone_id, &operation.task_kind) else {
                return unsupported("a staff member's task has no target");
            };
            let V::Map(durations) = config_item(&row.config, "task_duration_minutes")? else {
                return unsupported("the task durations are not a map");
            };
            let Some(V::Int(work)) = durations.get(&**kind) else {
                return unsupported("a task duration is not an int");
            };
            let Some(after) = next.checked_add(*work) else {
                return unsupported("a due minute out of range");
            };
            if after >= duration {
                continue;
            }
            let Some(required) = relief_skills(kind) else {
                return unsupported("an unknown kind of task");
            };
            let mut replacement: Option<usize> = None;
            for (index, candidate) in workers.iter().enumerate() {
                if candidate.id == *staff_id || candidate.zone != *target || !has_skill(candidate, &required) || candidate.has_task {
                    continue;
                }
                let Some(record) = row.staff.get(&candidate.id) else {
                    return unsupported("a staff member has no record");
                };
                if record.phase != Phase::Idle || !candidate.available || !candidate.can_start {
                    continue;
                }
                let better = replacement.is_none_or(|known| (workload[index], &candidate.id) < (workload[known], &workers[known].id));
                if better {
                    replacement = Some(index);
                }
            }
            if let Some(index) = replacement {
                plans.push(PlannedRelief { task_id: task_id.clone(), previous: staff_id.clone(), replacement: workers[index].id.clone() });
            }
        }
        Ok(plans)
    })
}

/// `schedule_staff_operation_relief`: a replacement already at the task takes it over at the shift's
/// boundary. With no staff member on a task there is nothing to relieve.
#[pyfunction]
pub(crate) fn relief<'py>(world: &Bound<'py, PyAny>, minute: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
    let py = world.py();
    let done = PyTuple::empty(py);
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(done);
    }
    let at = exact_int(minute)?;
    let duration = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
    let Some(next) = at.checked_add(1) else {
        return unsupported("the minute does not fit an i64");
    };
    if next >= duration {
        return Ok(done);
    }
    let any_task = NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror_held(world, NAME)?, |row| {
        Ok(row.staff.values().any(|record| record.phase != Phase::Idle))
    })?;
    every_fatigue_is_finite(world)?;
    if !any_task {
        return Ok(done);
    }
    let plans = plan_relief(world, at, next, duration);
    let plans = refused_by_python(py, plans)?;
    let mut handoffs = Vec::with_capacity(plans.len());
    for plan in plans {
        let sequence: i64 = world.call_method0(intern!(py, "allocate_event_sequence"))?.extract()?;
        let event_id = format!("staff-operation-relief-{sequence}");
        let payload = PyDict::new(py);
        payload.set_item("task_id", &*plan.task_id)?;
        payload.set_item("previous_staff_id", &*plan.previous)?;
        payload.set_item("replacement_staff_id", &*plan.replacement)?;
        payload.set_item("reason_code", "shift_or_availability_boundary")?;
        let event = new_event(
            py,
            NewEvent {
                event_id: &event_id,
                kind: "ENDOGENOUS",
                due_minute: next,
                priority: -5,
                sequence,
                event_type: "staff_operation_relief",
                payload: payload.into_any(),
                source_id: Some(PyString::new(py, RELIEF_POLICY).into_any()),
                causation_id: None,
                provenance: assumption(py, PyString::new(py, RELIEF_SOURCE).as_any(), RELIEF_POLICY)?.into_any(),
            },
        )?;
        world.call_method1(intern!(py, "schedule_event"), (event,))?;
        handoffs.push(event_id);
    }
    PyTuple::new(py, handoffs)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(assign_pending, m)?)?;
    m.add_function(wrap_pyfunction!(consider_due_complaints, m)?)?;
    m.add_function(wrap_pyfunction!(assign_none, m)?)?;
    m.add_function(wrap_pyfunction!(assign_work, m)?)?;
    m.add_function(wrap_pyfunction!(wait_snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(update_operations, m)?)?;
    m.add_function(wrap_pyfunction!(assign_operations, m)?)?;
    m.add_function(wrap_pyfunction!(relief, m)?)?;
    m.add_function(wrap_pyfunction!(format_g, m)?)?;
    Ok(())
}
