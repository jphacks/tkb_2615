//! `domain/staff.py` and `domain/staff_operations.py`: the rules of a tick where no staff member
//! starts anything, which is nearly every tick. Each kernel plans from the rows, and says
//! Unsupported, before it changes anything, as soon as a staff member would take a request or a
//! task: that needs a route and events, which are the Python rule's, and it then runs the whole
//! step (docs/rust-port/lanes/m4-staff.md).
//!
//! The Python rules stay as the comparison base and for what these kernels do not decide, which is
//! also how a bad value gets its error text.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyInt, PyList, PyString, PyTuple};
use rustc_hash::FxHashMap;

use crate::core::mathx::{py_fsum, py_max};
use crate::core::row::{attribute, exact_dict, exact_int, exact_str, map_set, namespace_is_absent, unsupported};
use crate::core::value::V;
use crate::core::world_core::{field_mirror_of, mirror_of, raw_dict_get, NamespaceMirror};
use crate::rules::handlers::{assumption, get, get_is, log_record, new_event, NewEvent};
use crate::rules::staff_dynamics::{is_available, on_shift, staff_status};
use crate::state::body_state::{BodyRow, NAME as BODY_STATE};
use crate::state::entities::{entities_of, raw_setattr, Activity, EntityStore, RequestStatus};
use crate::state::metric_state::MetricRow;
use crate::state::staff_dynamics::{StaffDynamicsRow, NAME as STAFF_DYNAMICS};
use crate::state::staff_operations::{Phase, StaffOperationsRow, NAME};
use crate::Unsupported;

const SHARED_OPERATIONS: &str = "shared_operations_v1";
const SUPPORT_SOURCE: &str = "ASM-STAFF-SUPPORT-LIFECYCLE-V1";
const PREEMPTION_SOURCE: &str = "ASM-URGENT-STAFF-SUPPORT-PREEMPTION-V1";
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
fn mirror_held<'py>(world: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, NamespaceMirror>> {
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
/// Unsupported if one could take the request.
fn kind_exposure(workers: &[Worker], kind: &str) -> PyResult<(bool, bool, bool)> {
    let required = required_skills(kind);
    let (mut any, mut busy, mut off_shift) = (false, false, false);
    for worker in workers {
        if worker.skills.iter().any(|skill| required.contains(&&**skill)) {
            if !worker.has_task && worker.available && worker.can_start {
                return unsupported("a free worker can take a request");
            }
            any = true;
            busy |= worker.has_task;
            off_shift |= !worker.on_shift || !worker.available;
        }
    }
    Ok((!any, busy, off_shift))
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
    match rules.call_method(intern!(py, "assign_none"), (service, minute), Some(&validate_option(py, validate_state)?)) {
        Err(error) if error.is_instance_of::<Unsupported>(py) => {
            rules.call_method(intern!(py, "assign_work"), (service, minute), Some(&validate_option(py, validate_state)?))?.downcast_into::<PyTuple>().map_err(PyErr::from)
        }
        other => other?.downcast_into::<PyTuple>().map_err(PyErr::from),
    }
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
fn consider_due_complaints<'py>(controller: &Bound<'py, PyAny>, minute: &Bound<'py, PyAny>) -> PyResult<()> {
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
/// and the wait's reason on each, and the minute's exposure. Unsupported as soon as a request has
/// a qualified free worker or a priority, which `assign_work` takes.
#[pyfunction]
#[pyo3(signature = (service, minute, *, validate_state=true))]
fn assign_none<'py>(
    service: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    validate_state: bool,
) -> PyResult<Bound<'py, PyTuple>> {
    let py = service.py();
    if validate_state {
        return unsupported("the step validates the state");
    }
    let world = attribute(service, intern!(py, "world"))?;
    let at = exact_int(minute)?;
    if at != minute_of(&world)? {
        return unsupported("the minute is not the world's");
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
            return unsupported("the request is not the world's");
        }
        if !attribute(&request.object, listener_name)?.is_none() {
            return unsupported("a pending request reports its writes");
        }
        let kind = kinds.kind_of(&request.id)?;
        let kind = match &kind {
            Some(kind) => exact_str(kind)?,
            None => DEFAULT_KIND,
        };
        let exposure = match exposures.iter().find(|(known, _)| known == kind) {
            Some((_, exposure)) => *exposure,
            None => {
                let exposure = kind_exposure(&workers, kind)?;
                exposures.push((kind.to_owned(), exposure));
                exposure
            }
        };
        if is_urgent(priorities.as_ref(), &request.id)? {
            return unsupported("an urgent request may preempt a task");
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
    Ok(PyTuple::empty(py))
}

// ---- What the metrics read of each open request ---------------------------------------------------

/// What `_support_wait_snapshot` needs of the qualified staff of one kind.
struct KindWait {
    kind: String,
    qualified: bool,
    /// `support_wait_reason_codes` of a pending request of this kind, when no qualified worker is free.
    reasons: Vec<&'static str>,
}

fn kind_wait(workers: &[Worker], kind: &str, sample: i64) -> PyResult<KindWait> {
    let required = required_skills(kind);
    let mut qualified = false;
    let mut unavailable = false;
    let mut staff_busy = false;
    let mut other_operation = false;
    let mut resting = false;
    let mut off_shift = false;
    for worker in workers {
        if !worker.skills.iter().any(|skill| required.contains(&&**skill)) {
            continue;
        }
        qualified = true;
        if !worker.has_task && worker.available && worker.can_start && worker.on_shift {
            return unsupported("a qualified worker is free, so routes are read");
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
    let mut reasons = Vec::with_capacity(5);
    if !qualified {
        reasons.push("no_qualified_staff");
    } else {
        if staff_busy {
            reasons.push("staff_busy");
        }
        if off_shift {
            reasons.push("staff_off_shift");
        }
        if other_operation {
            reasons.push("staff_on_other_operation");
        }
        if resting {
            reasons.push("staff_resting");
        }
        if unavailable {
            reasons.push("staff_unavailable");
        }
        if reasons.is_empty() {
            reasons.push("no_route_to_qualified_staff");
        }
    }
    Ok(KindWait { kind: kind.to_owned(), qualified, reasons })
}

/// What the metrics read of one open request.
pub(crate) struct OpenWait<'py> {
    pub(crate) agent: Arc<str>,
    pub(crate) id: Bound<'py, PyAny>,
    pub(crate) waited: i64,
    /// The index of its kind in `OpenWaits::waits`, or its one reason.
    reason: Result<usize, &'static str>,
}

/// The requests of `_support_wait_snapshot`, in the order given.
pub(crate) struct OpenWaits<'py> {
    pub(crate) requests: Vec<OpenWait<'py>>,
    waits: Vec<KindWait>,
}

impl OpenWaits<'_> {
    pub(crate) fn reasons_of(&self, request: &OpenWait<'_>) -> Vec<&'static str> {
        match request.reason {
            Ok(kind) => self.waits[kind].reasons.clone(),
            Err(reason) => vec![reason],
        }
    }

    pub(crate) fn has_no_qualified_staff(&self, request: &OpenWait<'_>) -> bool {
        matches!(request.reason, Ok(kind) if !self.waits[kind].qualified)
    }
}

/// `support_waited_minutes` and `support_wait_reason_codes` of each request, or with `only_open`
/// of each one that is pending or assigned. Unsupported as soon as a pending request has a
/// qualified free worker, whose routes are read.
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
            Ok((request.agent_id.clone(), request.status, moving, waited.max(0)))
        })?;
        let (agent, status, moving, waited) = read;
        if only_open && !matches!(status, RequestStatus::Pending | RequestStatus::Assigned) {
            continue;
        }
        let reason = match status {
            RequestStatus::Assigned => Err(if moving { "resident_travel" } else { "support_in_progress" }),
            RequestStatus::Pending => {
                let kind = kinds.kind_of(&id_object)?;
                let kind = match &kind {
                    Some(kind) => exact_str(kind)?,
                    None => DEFAULT_KIND,
                };
                match waits.iter().position(|wait| wait.kind == kind) {
                    Some(index) => Ok(index),
                    None => {
                        if workers.is_none() {
                            workers = Some(workers_at(world, sample)?);
                        }
                        waits.push(kind_wait(workers.as_deref().unwrap_or(&[]), kind, sample)?);
                        Ok(waits.len() - 1)
                    }
                }
            }
            _ => Err("request_closed"),
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

/// The people of a zone: their stress, and the uncertainty of those with a body record.
#[derive(Default)]
struct ZonePeople {
    stress: Vec<f64>,
    uncertainty: Vec<f64>,
}

/// `assign_staff_operations` when no free worker has a task to take: it changes nothing. Unsupported
/// as soon as one has, since a task needs a route and events.
#[pyfunction]
pub(crate) fn assign_operations<'py>(
    world: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    _router: &Bound<'py, PyAny>,
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
    let free: Vec<&Worker> =
        workers.iter().filter(|worker| !worker.has_task && !worker.on_operation && worker.available && worker.can_start).collect();
    let has = |skills: &[&str]| free.iter().any(|worker| worker.skills.iter().any(|skill| skills.contains(&&**skill)));
    let cleaners = has(&["cleaning", "sanitation"]);
    let repairers = has(&["maintenance", "repair"]);
    let outreach = has(&["support", "information"]);
    let patrol = has(&["support", "patrol"]);
    let mirror = mirror_held(world, NAME)?;
    every_fatigue_is_finite(world)?;
    NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror, |row| {
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
                    return unsupported("a free worker can clean");
                }
            }
        }
        if repairers {
            let any_deficit = EntityStore::with_rows(&store, |rows| {
                Ok(rows.facilities.iter().any(|(_, facility)| facility.capacity > facility.working_units))
            })?;
            if any_deficit {
                return unsupported("a free worker may repair");
            }
        }
        if !outreach && !patrol {
            return Ok(());
        }
        let body = if namespace_is_absent(world, intern!(py, BODY_STATE))? { None } else { Some(mirror_held(world, BODY_STATE)?) };
        let mut people: FxHashMap<Arc<str>, ZonePeople> = FxHashMap::default();
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
        if outreach {
            for (zone, group) in &people {
                if group.uncertainty.is_empty() {
                    continue;
                }
                let Some(sum) = py_fsum(&group.uncertainty) else {
                    return unsupported("an uncertainty is not finite");
                };
                let mean = sum / group.uncertainty.len() as f64;
                let at_least = threshold(
                    zone_override(&row.config, "thresholds_by_zone", zone, "outreach_uncertainty_at_least", false)?,
                    OUTREACH_AT_LEAST,
                )?;
                if mean >= at_least && !reserved("information_outreach", zone) {
                    return unsupported("a free worker can reach out");
                }
            }
        }
        if patrol {
            for zone in world_zones(world)? {
                let Some(group) = people.get(&zone) else {
                    continue;
                };
                let Some(sum) = py_fsum(&group.stress) else {
                    return unsupported("a stress is not finite");
                };
                let mean = sum / group.stress.len() as f64;
                let at_least = threshold(
                    zone_override(&row.config, "thresholds_by_zone", &zone, "patrol_stress_at_least", false)?,
                    PATROL_AT_LEAST,
                )?;
                if mean >= at_least && !reserved("patrol", &zone) {
                    return unsupported("a free worker can patrol");
                }
            }
        }
        Ok(())
    })?;
    Ok(done)
}

/// `schedule_staff_operation_relief` when no staff member has a task: there is nothing to relieve.
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
    NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror_held(world, NAME)?, |row| {
        if row.staff.values().any(|record| record.phase != Phase::Idle) {
            return unsupported("a staff member has a task");
        }
        Ok(())
    })?;
    every_fatigue_is_finite(world)?;
    Ok(done)
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
    Ok(())
}
