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
use pyo3::types::{PyBool, PyDict, PyInt, PyList, PySet, PyString, PyTuple};
use rustc_hash::FxHashMap;

use crate::core::mathx::{py_fsum, py_max};
use crate::core::row::{attribute, exact_dict, exact_int, exact_str, map_set, namespace_is_absent, unsupported};
use crate::core::value::V;
use crate::core::world_core::{field_mirror_of, mirror_of, raw_dict_get, raw_dict_set, NamespaceMirror};
use crate::rules::staff_dynamics::{is_available, on_shift, staff_status};
use crate::state::body_state::{BodyRow, NAME as BODY_STATE};
use crate::state::entities::{entities_of, raw_setattr, Activity, EntityStore, RequestStatus};
use crate::state::metric_state::MetricRow;
use crate::state::staff_dynamics::{StaffDynamicsRow, NAME as STAFF_DYNAMICS};
use crate::state::staff_operations::{Phase, StaffOperationsRow, NAME};

const SHARED_OPERATIONS: &str = "shared_operations_v1";
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
        if namespace_is_absent(world, intern!(py, STAFF_DYNAMICS))? { None } else { Some(mirror_of(world, STAFF_DYNAMICS)?) };
    let operations = if namespace_is_absent(world, intern!(py, NAME))? { None } else { Some(mirror_of(world, NAME)?) };
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

/// `StaffService.assign_pending` when every pending request stays pending: it records the attempt
/// and the wait's reason on each, and the minute's exposure. Unsupported as soon as a request has
/// a qualified free worker or a priority, because taking it needs a route.
#[pyfunction]
#[pyo3(signature = (service, minute, *, validate_state=true))]
fn assign_pending<'py>(
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
        id: Arc<str>,
        same_reason: bool,
        same_minute: bool,
    }
    let mut pending: Vec<Pending<'py>> = Vec::new();
    let mut no_qualified = 0.0;
    let mut busy = 0.0;
    let mut off_shift = 0.0;
    let listener_name = intern!(py, "_write_listener");
    let id_name = intern!(py, "id");
    for request in open.try_iter()? {
        let request = request?;
        let id_object = attribute(&request, id_name)?;
        let id = exact_str(&id_object)?;
        let Some((is_pending, same_reason, same_minute)) = EntityStore::with_rows(&store, |rows| {
            Ok(rows.support_requests.get(id).map(|row| {
                (
                    row.status == RequestStatus::Pending,
                    row.last_assignment_wait_reason_code.as_deref() == Some(WAIT_REASON),
                    row.last_assignment_attempt_minute == Some(at),
                )
            }))
        })?
        else {
            return unsupported("a request has no row");
        };
        if !is_pending {
            continue;
        }
        if !raw_dict_get(&map, &id_object)?.is_some_and(|object| object.is(&request)) {
            return unsupported("the request is not the world's");
        }
        if !attribute(&request, listener_name)?.is_none() {
            return unsupported("a pending request reports its writes");
        }
        let kind = kinds.kind_of(&id_object)?;
        let kind = match &kind {
            Some(kind) => exact_str(kind)?,
            None => DEFAULT_KIND,
        };
        let required = required_skills(kind);
        let qualifies = |worker: &&Worker| worker.skills.iter().any(|skill| required.contains(&&**skill));
        if workers.iter().filter(qualifies).any(|worker| !worker.has_task && worker.available && worker.can_start) {
            return unsupported("a free worker can take a request");
        }
        if is_urgent(priorities.as_ref(), &id_object)? {
            return unsupported("an urgent request may preempt a task");
        }
        let mut qualified = workers.iter().filter(qualifies).peekable();
        if qualified.peek().is_none() {
            no_qualified += 1.0;
        }
        if qualified.clone().any(|worker| worker.has_task) {
            busy += 1.0;
        }
        if qualified.any(|worker| !worker.on_shift || !worker.available) {
            off_shift += 1.0;
        }
        pending.push(Pending { object: request, id: Arc::from(id), same_reason, same_minute });
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
    let attempt_name = intern!(py, "last_assignment_attempt_minute");
    let reason_name = intern!(py, "last_assignment_wait_reason_code");
    let reason = intern!(py, WAIT_REASON);
    for request in &pending {
        if request.same_minute && request.same_reason {
            continue;
        }
        EntityStore::with_rows(&store, |rows| {
            let Some(row) = rows.support_requests.get_mut(&request.id) else {
                return unsupported("a request has no row");
            };
            row.last_assignment_attempt_minute = Some(at);
            row.last_assignment_wait_reason_code = Some(Arc::from(WAIT_REASON));
            Ok(())
        })?;
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

// ---- support_wait_reason_codes, support_waited_minutes --------------------------------------------

fn answer<'py>(py: Python<'py>, reasons: &[&str]) -> PyResult<Bound<'py, PyTuple>> {
    PyTuple::new(py, reasons)
}

/// `support_wait_reason_codes` where no qualified worker is free, so no route is read. Otherwise
/// Unsupported. A `staff_memo` gets the entry the Python rule would keep for this kind, and beside
/// it the answer under `(None, kind)`, a key the Python rule never asks for.
#[pyfunction]
#[pyo3(signature = (world, request_id, *, minute=None, staff_memo=None))]
fn wait_reason_codes<'py>(
    world: &Bound<'py, PyAny>,
    request_id: &Bound<'py, PyAny>,
    minute: Option<&Bound<'py, PyAny>>,
    staff_memo: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyTuple>> {
    let py = world.py();
    let id = exact_str(request_id)?;
    let store = entities_of(world)?;
    let state = EntityStore::with_rows(&store, |rows| {
        let Some(request) = rows.support_requests.get(id) else {
            return Ok(None);
        };
        let moving = rows.agents.get(&request.agent_id).is_some_and(|agent| agent.activity == Activity::Moving);
        Ok(Some((request.status, moving)))
    })?;
    let Some((status, moving)) = state else {
        return answer(py, &["request_missing"]);
    };
    match status {
        RequestStatus::Assigned => {
            return answer(py, &[if moving { "resident_travel" } else { "support_in_progress" }]);
        }
        RequestStatus::Pending => {}
        _ => return answer(py, &["request_closed"]),
    }
    let table = table_of(world)?;
    let kind = CareKinds::of(&table)?.kind_of(request_id)?;
    let kind_name = match &kind {
        Some(kind) => exact_str(kind)?,
        None => DEFAULT_KIND,
    };
    let memo = match staff_memo {
        Some(memo) if !memo.is_none() => Some(memo.downcast_exact::<PyDict>().map_err(|_| {
            crate::Unsupported::new_err("the memo is not a plain dict")
        })?),
        _ => None,
    };
    let kind_object = PyString::new(py, kind_name);
    let answer_key = PyTuple::new(py, [py.None().into_bound(py), kind_object.clone().into_any()])?;
    if let Some(memo) = memo {
        if let Some(known) = memo.get_item(&answer_key)? {
            return Ok(known.downcast_into_exact::<PyTuple>()?);
        }
        if memo.contains(&kind_object)? {
            return unsupported("the Python rule keeps this kind's entry");
        }
    }
    let sample = match minute {
        Some(minute) if !minute.is_none() => exact_int(minute)?,
        _ => minute_of(world)?,
    };
    let required = required_skills(kind_name);
    let workers = workers_at(world, sample)?;
    let qualified: Vec<&Worker> =
        workers.iter().filter(|worker| worker.skills.iter().any(|skill| required.contains(&&**skill))).collect();
    let mut unavailable = false;
    let mut staff_busy = false;
    let mut other_operation = false;
    let mut resting = false;
    let mut off_shift = false;
    for worker in &qualified {
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
    let mut reasons: Vec<&str> = Vec::with_capacity(5);
    if qualified.is_empty() {
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
    let result = answer(py, &reasons)?;
    if let Some(memo) = memo {
        let map = world.getattr(intern!(py, "staff"))?;
        let objects = PyList::empty(py);
        for worker in &qualified {
            let Some(object) = raw_dict_get(&map, PyString::new(py, &worker.id).as_any())? else {
                return unsupported("a worker has no object");
            };
            objects.append(object)?;
        }
        let blockers = if qualified.is_empty() {
            PySet::empty(py)?
        } else {
            let names = reasons.iter().filter(|reason| **reason != "no_route_to_qualified_staff");
            PySet::new(py, names.copied())?
        };
        let entry = PyTuple::new(py, [objects.into_any(), blockers.into_any(), PyList::empty(py).into_any()])?;
        raw_dict_set(memo.as_any(), kind_object.as_any(), entry.as_any())?;
        raw_dict_set(memo.as_any(), answer_key.as_any(), result.as_any())?;
    }
    Ok(result)
}

/// `support_waited_minutes`.
#[pyfunction]
#[pyo3(signature = (world, request_id, *, minute=None))]
fn waited_minutes(
    world: &Bound<'_, PyAny>,
    request_id: &Bound<'_, PyAny>,
    minute: Option<&Bound<'_, PyAny>>,
) -> PyResult<i64> {
    let id = exact_str(request_id)?;
    let sample = match minute {
        Some(minute) if !minute.is_none() => exact_int(minute)?,
        _ => minute_of(world)?,
    };
    EntityStore::with_rows(&entities_of(world)?, |rows| {
        let Some(request) = rows.support_requests.get(id) else {
            return Ok(0);
        };
        let mut endpoint = sample;
        if request.status == RequestStatus::Assigned {
            let agent = rows.agents.get(&request.agent_id);
            let task = rows.active_tasks.get(request.task_id.as_deref().unwrap_or(""));
            if let (Some(agent), Some(task)) = (agent, task) {
                if agent.activity != Activity::Moving {
                    endpoint = sample.min(task.start_minute);
                }
            }
        }
        let Some(waited) = endpoint.checked_sub(request.requested_minute) else {
            return unsupported("the wait does not fit an i64");
        };
        Ok(waited.max(0))
    })
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
fn update_operations(world: &Bound<'_, PyAny>, minute: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(());
    }
    let at = exact_int(minute)?;
    let zones = world_zones(world)?;
    let mirror = mirror_of(world, NAME)?;
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
    NamespaceMirror::with_row::<StaffDynamicsRow, _>(&mirror_of(world, STAFF_DYNAMICS)?, |row| {
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
fn assign_operations<'py>(
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
    let mirror = mirror_of(world, NAME)?;
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
        let body = if namespace_is_absent(world, intern!(py, BODY_STATE))? { None } else { Some(mirror_of(world, BODY_STATE)?) };
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
fn relief<'py>(world: &Bound<'py, PyAny>, minute: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
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
    NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror_of(world, NAME)?, |row| {
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
    m.add_function(wrap_pyfunction!(wait_reason_codes, m)?)?;
    m.add_function(wrap_pyfunction!(waited_minutes, m)?)?;
    m.add_function(wrap_pyfunction!(update_operations, m)?)?;
    m.add_function(wrap_pyfunction!(assign_operations, m)?)?;
    m.add_function(wrap_pyfunction!(relief, m)?)?;
    Ok(())
}
