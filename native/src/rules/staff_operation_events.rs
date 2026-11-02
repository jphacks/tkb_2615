//! The events of an autonomous staff operation (`domain/staff_operations.py`): arrival, relief,
//! completion and interruption, with `StaffOperationCompletionHandler`'s receipts.
//!
//! Each plans with nothing changed and then writes, on the same Python containers as the rule:
//! `staff_operations_v1` is Rust's row, kept by single-item forwarded writes. Anything but a success
//! says Unsupported before the first write, and the Python rule says it in its own words: a stale relief,
//! a person who became unavailable (which interrupts and suppresses), a value not of the row's types
//! (docs/rust-port/lanes/m6-handlers-rest.md).

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySlice, PyString, PyTuple};

use crate::core::mathx::{py_max, py_min};
use crate::core::row::{attribute, exact_float, exact_int, exact_str, unsupported};
use crate::rules::care_receipts::{facility_recovery_record, handoff_record, staff_operation_record, OperationOutcome};
use crate::rules::effects::{active_outage, has_active_modifier, refused_by_python};
use crate::rules::handlers::{
    assumption, counted, fields_are, get, get_is, kind_is, log_record, new_event, number, pending_of, text_is, NewEvent,
};
use crate::rules::staff_dynamics::{is_available, staff_status};

const NAME: &str = "staff_operations_v1";
const MOVEMENT: &str = "movement_v1";
const MAX_HISTORY: usize = 20_000;
const RELIEF_POLICY: &str = "same-site-staff-relief-v1";
const RELIEF_SOURCE: &str = "ASM-STAFF-RELIEF-V1";
const OPERATIONS_SOURCE: &str = "ASM-STAFF-OPERATIONS-V1";
const OPERATIONS_POLICY: &str = "autonomous-routed-staff-v1";

fn table_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    attribute(world, intern!(world.py(), "model_state"))
}

fn staff_of<'py>(world: &Bound<'py, PyAny>, id: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    attribute(world, intern!(world.py(), "staff"))?.call_method1(intern!(world.py(), "get"), (id,))
}

fn task_of<'py>(world: &Bound<'py, PyAny>, id: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    attribute(world, intern!(world.py(), "active_tasks"))?.call_method1(intern!(world.py(), "get"), (id,))
}

/// `staff_is_available`.
fn available(world: &Bound<'_, PyAny>, id: &str) -> PyResult<bool> {
    is_available(staff_status(world)?.as_ref(), &PyString::new(world.py(), id))
}

/// `_idle_record()`.
fn idle_record(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let record = PyDict::new(py);
    for name in ["phase", "task_id", "task_kind", "target_id", "target_zone_id", "reason", "started_minute", "travel_due_minute", "arrival_minute", "due_minute"] {
        record.set_item(name, py.None())?;
    }
    record.set_item("phase", "idle")?;
    record.set_item("route_zone_ids", PyList::empty(py))?;
    record.set_item("route_edge_ids", PyList::empty(py))?;
    record.set_item("travel_minutes", 0)?;
    record.set_item("causation_event_ids", PyList::empty(py))?;
    Ok(record)
}

/// `sorted(set(ids + [event_id]))`.
fn causation_with(record: &Bound<'_, PyAny>, extra: &[String]) -> PyResult<Vec<String>> {
    let mut ids = extra.to_vec();
    for id in record.get_item("causation_event_ids")?.try_iter()? {
        ids.push(exact_str(&id?)?.to_owned());
    }
    ids.sort_unstable();
    ids.dedup();
    Ok(ids)
}

/// `state["history"].append(entry)` and the cut to its limit.
fn append_history(state: &Bound<'_, PyAny>, entry: Bound<'_, PyDict>) -> PyResult<()> {
    let py = state.py();
    let history = state.get_item("history")?;
    history.call_method1(intern!(py, "append"), (entry,))?;
    let length = history.len()?;
    if length > MAX_HISTORY {
        history.del_item(PySlice::new(py, 0, (length - MAX_HISTORY) as isize, 1))?;
    }
    Ok(())
}

fn free_staff(world: &Bound<'_, PyAny>, staff: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    attribute(staff, intern!(py, "work_queue"))?.call_method0(intern!(py, "clear"))?;
    staff.setattr(intern!(py, "active_task_id"), py.None())?;
    staff.setattr(intern!(py, "busy_until"), 0)?;
    Ok(())
}

fn position_snapshots<'py>(world: &Bound<'py, PyAny>, boundary: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let kwargs = PyDict::new(py);
    kwargs.set_item("boundary_minute", boundary)?;
    py.import("sheltersim.domain.movement")?.getattr("staff_position_snapshots")?.call((world,), Some(&kwargs))
}

// ---- interrupt_staff_operation ---------------------------------------------------------------------

/// `interrupt_staff_operation`. In a kernel with no changes yet its reads refuse first; a transaction's
/// copy has nothing to lose.
pub(crate) fn interrupt_operation<'py>(
    world: &Bound<'py, PyAny>,
    staff_id: &str,
    event: &Bound<'py, PyAny>,
    current_event_id: Option<&str>,
) -> PyResult<bool> {
    let py = world.py();
    let state = get(&table_of(world)?, NAME)?;
    let staff_key = PyString::new(py, staff_id);
    let staff = staff_of(world, staff_key.as_any())?;
    let record = if state.is_instance_of::<PyDict>() { state.get_item("staff")?.call_method1(intern!(py, "get"), (staff_id,))? } else { py.None().into_bound(py) };
    if staff.is_none() || !record.is_instance_of::<PyDict>() || get_is(&record, "phase", "idle")? {
        return Ok(false);
    }
    let task_id = record.get_item("task_id")?;
    let due_object = attribute(event, intern!(py, "due_minute"))?;
    let due = exact_int(&due_object)?;
    let started = exact_int(&record.get_item("started_minute")?)?;
    let travel = exact_int(&record.get_item("travel_minutes")?)?;
    let elapsed = 0.max(travel.min(due - started));
    let mut nearest = None;
    if text_is(&record.get_item("phase")?, "travel") {
        let zones = record.get_item("route_zone_ids")?;
        let edges = attribute(world, intern!(py, "zone_edges"))?;
        let mut cumulative = 0_i64;
        let mut zone = zones.get_item(0)?;
        for (index, edge_id) in record.get_item("route_edge_ids")?.try_iter()?.enumerate() {
            let edge_minutes = exact_int(&attribute(&edges.get_item(edge_id?)?, intern!(py, "travel_minutes"))?)?;
            if edge_minutes != 0 && elapsed < cumulative + edge_minutes {
                let progress = (elapsed - cumulative) as f64 / edge_minutes as f64;
                zone = zones.get_item(if progress < 0.5 { index } else { index + 1 })?;
                break;
            }
            cumulative += edge_minutes;
            zone = zones.get_item(index + 1)?;
            if elapsed <= cumulative {
                break;
            }
        }
        nearest = Some(zone);
    }
    let event_id = attribute(event, intern!(py, "event_id"))?;
    let mut causation_ids = vec![exact_str(&event_id)?.to_owned()];
    let mut interrupted = Vec::new();
    for pending in pending_of(world)? {
        let pending_id = attribute(&pending, intern!(py, "event_id"))?;
        let pending_type = attribute(&pending, intern!(py, "event_type"))?;
        if current_event_id.is_some_and(|current| text_is(&pending_id, current))
            || !(text_is(&pending_type, "staff_operation_arrive") || text_is(&pending_type, "staff_operation_complete"))
            || !get(&attribute(&pending, intern!(py, "payload"))?, "task_id")?.eq(&task_id)?
        {
            continue;
        }
        causation_ids.push(exact_str(&pending_id)?.to_owned());
        interrupted.push(pending);
    }
    let arrival = record.get_item("arrival_minute")?;
    let arrival = if arrival.is_none() && travel > 0 && elapsed >= travel { (started + travel).into_pyobject(py)?.into_any() } else { arrival };
    let causation = causation_with(&record, &causation_ids)?;
    let entry = PyDict::new(py);
    entry.set_item("task_id", &task_id)?;
    entry.set_item("staff_id", staff_id)?;
    entry.set_item("task_kind", record.get_item("task_kind")?)?;
    entry.set_item("target_id", record.get_item("target_id")?)?;
    entry.set_item("target_zone_id", record.get_item("target_zone_id")?)?;
    entry.set_item("reason", record.get_item("reason")?)?;
    entry.set_item("status", "interrupted")?;
    entry.set_item("start_minute", started)?;
    entry.set_item("arrival_minute", arrival)?;
    entry.set_item("complete_minute", due)?;
    entry.set_item("travel_minutes", travel)?;
    entry.set_item("causation_event_ids", PyList::new(py, &causation)?)?;
    let queue = attribute(world, intern!(py, "event_queue"))?;
    let log = attribute(world, intern!(py, "event_log"))?;

    // Write.
    if let Some(zone) = nearest {
        staff.setattr(intern!(py, "zone_id"), zone)?;
    }
    for pending in interrupted {
        queue.call_method1(intern!(py, "remove"), (attribute(&pending, intern!(py, "event_id"))?,))?;
        log.call_method1(
            intern!(py, "append"),
            (log_record(py, &pending, &due_object, "SUPPRESSED", Some("Staff operation was interrupted by staff unavailability"))?,),
        )?;
    }
    append_history(&state, entry)?;
    attribute(world, intern!(py, "active_tasks"))?.call_method1(intern!(py, "pop"), (&task_id, py.None()))?;
    free_staff(world, &staff)?;
    state.get_item("staff")?.set_item(staff_id, idle_record(py)?)?;
    if table_of(world)?.contains(MOVEMENT)? {
        position_snapshots(world, &due_object)?;
    }
    Ok(true)
}

/// `interrupt_staff_operation`, called from Python.
#[pyfunction]
#[pyo3(signature = (world, staff_id, event, *, current_event_id=None))]
fn interrupt<'py>(world: &Bound<'py, PyAny>, staff_id: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>, current_event_id: Option<&str>) -> PyResult<bool> {
    counted(world.py(), "interrupt", interrupt_operation(world, exact_str(staff_id)?, event, current_event_id))
}

// ---- Arrival -----------------------------------------------------------------------------------------

/// `arrive_staff_operation`, of a person still available.
#[pyfunction]
fn arrive<'py>(_handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    let plan = (|| -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>, Bound<'py, PyAny>, Bound<'py, PyAny>)> {
        if !text_is(&attribute(event, intern!(py, "event_type"))?, "staff_operation_arrive") {
            return unsupported("another event");
        }
        let payload = attribute(event, intern!(py, "payload"))?;
        let state = get(&table_of(world)?, NAME)?;
        let staff_id = get(&payload, "staff_id")?;
        let task_id = get(&payload, "task_id")?;
        let id = exact_str(&staff_id)?;
        exact_str(&task_id)?;
        if !state.is_instance_of::<PyDict>() {
            return unsupported("no staff operations");
        }
        let record = state.get_item("staff")?.call_method1(intern!(py, "get"), (id,))?;
        let staff = staff_of(world, &staff_id)?;
        let task = task_of(world, &task_id)?;
        let due = attribute(event, intern!(py, "due_minute"))?;
        if record.is_none()
            || !text_is(&record.get_item("phase")?, "travel")
            || !record.get_item("task_id")?.eq(&task_id)?
            || staff.is_none()
            || task.is_none()
            || !attribute(&staff, intern!(py, "active_task_id"))?.eq(&task_id)?
            || !due.eq(record.get_item("travel_due_minute")?)?
            || !get(&payload, "target_zone_id")?.eq(record.get_item("target_zone_id")?)?
            || !available(world, id)?
        {
            return unsupported("the arrival is for the Python rule to judge");
        }
        Ok((record.clone(), staff, record.get_item("target_zone_id")?, due))
    })();
    let (record, staff, zone, due) = counted(py, "arrive", refused_by_python(py, plan))?;
    staff.setattr(intern!(py, "zone_id"), zone)?;
    record.set_item("phase", "working")?;
    record.set_item("travel_due_minute", py.None())?;
    record.set_item("arrival_minute", due)?;
    record.set_item("route_zone_ids", PyList::empty(py))?;
    record.set_item("route_edge_ids", PyList::empty(py))?;
    record.get_item("causation_event_ids")?.call_method1(intern!(py, "append"), (attribute(event, intern!(py, "event_id"))?,))?;
    Ok(())
}

// ---- Relief ------------------------------------------------------------------------------------------

/// `{"cleaning": {...}, ...}[kind]`.
fn relief_skills(kind: &str) -> Option<[&'static str; 2]> {
    match kind {
        "cleaning" => Some(["cleaning", "sanitation"]),
        "maintenance" => Some(["maintenance", "repair"]),
        "information_outreach" => Some(["support", "information"]),
        "intake" => Some(["support", "intake"]),
        "patrol" => Some(["support", "patrol"]),
        _ => None,
    }
}

/// `StaffOperationReliefHandler.apply`.
#[pyfunction]
fn relief<'py>(_handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let py = world.py();
    struct Plan<'py> {
        state: Bound<'py, PyAny>,
        operation: Bound<'py, PyAny>,
        task: Bound<'py, PyAny>,
        task_id: Bound<'py, PyAny>,
        previous_id: Bound<'py, PyAny>,
        replacement_id: Bound<'py, PyAny>,
        previous: Bound<'py, PyAny>,
        replacement: Bound<'py, PyAny>,
        pending: Vec<Bound<'py, PyAny>>,
        work_minutes: i64,
        causation: Vec<String>,
        formula_source: Bound<'py, PyAny>,
        formula_policy: Bound<'py, PyAny>,
        event_source: Bound<'py, PyAny>,
    }
    let plan = (|| -> PyResult<Plan<'py>> {
        let provenance = attribute(event, intern!(py, "provenance"))?;
        let payload = attribute(event, intern!(py, "payload"))?;
        let now = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
        if !text_is(&attribute(event, intern!(py, "event_type"))?, "staff_operation_relief")
            || !kind_is(event, "ENDOGENOUS")?
            || exact_int(&attribute(event, intern!(py, "due_minute"))?)? != now
            || !text_is(&attribute(event, intern!(py, "source_id"))?, RELIEF_POLICY)
            || !get_is(&provenance, "source_type", "assumption")?
            || !get_is(&provenance, "source_id", RELIEF_SOURCE)?
            || !get_is(&provenance, "policy_version", RELIEF_POLICY)?
            || !fields_are(&payload, &["task_id", "previous_staff_id", "replacement_staff_id", "reason_code"])?
        {
            return unsupported("the relief's provenance or payload is for the Python rule to judge");
        }
        let task_id = payload.get_item("task_id")?;
        let previous_id = payload.get_item("previous_staff_id")?;
        let replacement_id = payload.get_item("replacement_staff_id")?;
        let previous_text = exact_str(&previous_id)?.to_owned();
        let replacement_text = exact_str(&replacement_id)?.to_owned();
        let state = get(&table_of(world)?, NAME)?;
        if !state.is_instance_of::<PyDict>() {
            return unsupported("no staff operations");
        }
        let staff_records = state.get_item("staff")?;
        let operation = staff_records.call_method1(intern!(py, "get"), (&previous_id,))?;
        let task = task_of(world, &task_id)?;
        let previous = staff_of(world, &previous_id)?;
        let replacement = staff_of(world, &replacement_id)?;
        if !operation.is_instance_of::<PyDict>() || task.is_none() || previous.is_none() || replacement.is_none() {
            return unsupported("the relief is stale");
        }
        let due = exact_int(&operation.get_item("due_minute").and_then(|value| if value.is_none() { unsupported("an idle record") } else { Ok(value) })?)?;
        let dynamics = py.import("sheltersim.domain.staff_dynamics")?;
        let replacement_record = staff_records.call_method1(intern!(py, "get"), (&replacement_id, PyDict::new(py)))?;
        if text_is(&operation.get_item("phase")?, "idle")
            || !operation.get_item("task_id")?.eq(&task_id)?
            || !operation.get_item("target_zone_id")?.eq(attribute(&replacement, intern!(py, "zone_id"))?)?
            || !attribute(&task, intern!(py, "staff_id"))?.eq(&previous_id)?
            || !attribute(&previous, intern!(py, "active_task_id"))?.eq(&task_id)?
            || !attribute(&replacement, intern!(py, "active_task_id"))?.is_none()
            || !get_is(&replacement_record, "phase", "idle")?
            || !available(world, &replacement_text)?
            || !dynamics.getattr("staff_dynamics_is_available")?.call1((world, &replacement_id, now))?.is_truthy()?
            || (dynamics.getattr("staff_dynamics_on_shift")?.call1((world, &previous_id, now))?.is_truthy()? && available(world, &previous_text)?)
            || due <= now
        {
            return unsupported("the relief is stale");
        }
        let kind = operation.get_item("task_kind")?;
        let Some(required) = relief_skills(exact_str(&kind)?) else {
            return unsupported("an unknown kind of task");
        };
        let mut skilled = false;
        for skill in attribute(&replacement, intern!(py, "skills"))?.try_iter()? {
            let skill = skill?;
            skilled |= required.iter().any(|name| text_is(&skill, name));
        }
        if !skilled {
            return unsupported("the replacement lacks the skill");
        }
        let formula = py.import("sheltersim.policies.registry")?.getattr("create_staff_operations_formula")?.call1((attribute(world, intern!(py, "model_configs"))?,))?;
        let selected = attribute(world, intern!(py, "model_configs"))?
            .call_method1(intern!(py, "get"), ("policy_registry", PyDict::new(py)))?
            .contains("staff_operations")?;
        let (formula_source, formula_policy, event_source) = if selected {
            let source = attribute(&formula, intern!(py, "source_id"))?;
            (source.clone(), attribute(&formula, intern!(py, "model_id"))?, source)
        } else {
            let policy = PyString::new(py, OPERATIONS_POLICY).into_any();
            (PyString::new(py, OPERATIONS_SOURCE).into_any(), policy.clone(), policy)
        };
        let mut pending = Vec::new();
        let mut has_completion = false;
        for candidate in pending_of(world)? {
            let event_type = attribute(&candidate, intern!(py, "event_type"))?;
            let completes = text_is(&event_type, "staff_operation_complete");
            if (completes || text_is(&event_type, "staff_operation_arrive")) && get(&attribute(&candidate, intern!(py, "payload"))?, "task_id")?.eq(&task_id)? {
                has_completion |= completes;
                pending.push(candidate);
            }
        }
        if !has_completion {
            return unsupported("the relieved operation lacks a pending completion");
        }
        let work_minutes = exact_int(&state.get_item("config")?.get_item("task_duration_minutes")?.get_item(&kind)?)?;
        let causation = causation_with(&operation, &[exact_str(&attribute(event, intern!(py, "event_id"))?)?.to_owned()])?;
        Ok(Plan {
            state,
            operation,
            task,
            task_id,
            previous_id,
            replacement_id,
            previous,
            replacement,
            pending,
            work_minutes,
            causation,
            formula_source,
            formula_policy,
            event_source,
        })
    })();
    let plan = counted(py, "relief", refused_by_python(py, plan))?;
    let Plan { state, operation, task, task_id, previous_id, replacement_id, previous, replacement, pending, work_minutes, causation, formula_source, formula_policy, event_source } = plan;
    let now_object = attribute(world, intern!(py, "minute_index"))?;
    let now = exact_int(&now_object)?;

    // Write.
    let has_movement = table_of(world)?.contains(MOVEMENT)?;
    let mut previous_position = None;
    if has_movement {
        let position = position_snapshots(world, &now_object)?.call_method1(intern!(py, "get"), (&previous_id,))?;
        if !position.is_none() {
            previous.setattr(intern!(py, "zone_id"), position.get_item("zone_id")?)?;
            previous_position = Some(position);
        }
    }
    let queue = attribute(world, intern!(py, "event_queue"))?;
    let log = attribute(world, intern!(py, "event_log"))?;
    for candidate in &pending {
        queue.call_method1(intern!(py, "remove"), (attribute(candidate, intern!(py, "event_id"))?,))?;
        log.call_method1(
            intern!(py, "append"),
            (log_record(py, candidate, &now_object, "SUPPRESSED", Some("staff operation was replaced by a qualified co-located worker"))?,),
        )?;
    }
    previous.setattr(intern!(py, "active_task_id"), py.None())?;
    attribute(&previous, intern!(py, "work_queue"))?.call_method0(intern!(py, "clear"))?;
    previous.setattr(intern!(py, "busy_until"), 0)?;
    replacement.setattr(intern!(py, "active_task_id"), &task_id)?;
    let queue_of_replacement = attribute(&replacement, intern!(py, "work_queue"))?;
    queue_of_replacement.call_method0(intern!(py, "clear"))?;
    queue_of_replacement.call_method1(intern!(py, "append"), (&task_id,))?;
    let updated = py.import("copy")?.getattr("deepcopy")?.call1((&operation,))?;
    let due = now + work_minutes;
    updated.set_item("phase", "working")?;
    updated.set_item("started_minute", now)?;
    updated.set_item("travel_due_minute", py.None())?;
    updated.set_item("arrival_minute", now)?;
    updated.set_item("due_minute", due)?;
    updated.set_item("route_zone_ids", PyList::empty(py))?;
    updated.set_item("route_edge_ids", PyList::empty(py))?;
    updated.set_item("travel_minutes", 0)?;
    updated.set_item("causation_event_ids", PyList::new(py, &causation)?)?;
    task.setattr(intern!(py, "staff_id"), &replacement_id)?;
    task.setattr(intern!(py, "start_minute"), now)?;
    task.setattr(intern!(py, "due_minute"), due)?;
    replacement.setattr(intern!(py, "busy_until"), due)?;
    let records = state.get_item("staff")?;
    records.set_item(&previous_id, idle_record(py)?)?;
    let completion_payload = PyDict::new(py);
    completion_payload.set_item("staff_id", &replacement_id)?;
    completion_payload.set_item("task_id", &task_id)?;
    completion_payload.set_item("task_kind", updated.get_item("task_kind")?)?;
    completion_payload.set_item("target_id", updated.get_item("target_id")?)?;
    completion_payload.set_item("target_zone_id", updated.get_item("target_zone_id")?)?;
    let provenance = assumption(py, &formula_source, exact_str(&formula_policy)?)?;
    provenance.set_item("reason", updated.get_item("reason")?)?;
    records.set_item(&replacement_id, &updated)?;

    let sequence = exact_int(&world.call_method0(intern!(py, "allocate_event_sequence"))?)?;
    let event_id = attribute(event, intern!(py, "event_id"))?;
    let completion = new_event(
        py,
        NewEvent {
            event_id: &format!("staff-operation-complete-{sequence}"),
            kind: "COMPLETION",
            due_minute: due,
            priority: 5,
            sequence,
            event_type: "staff_operation_complete",
            payload: completion_payload.into_any(),
            source_id: Some(event_source),
            causation_id: Some(event_id.clone()),
            provenance: provenance.into_any(),
        },
    )?;
    world.call_method1(intern!(py, "schedule_event"), (completion,))?;
    if has_movement {
        position_snapshots(world, &now_object)?;
        if let Some(position) = previous_position {
            table_of(world)?.get_item(MOVEMENT)?.get_item("staff_positions")?.set_item(&previous_id, position)?;
        }
    }
    let payload = attribute(event, intern!(py, "payload"))?;
    let handoff = handoff_record(
        world,
        exact_str(&task_id)?,
        exact_str(&previous_id)?,
        exact_str(&replacement_id)?,
        exact_str(&payload.get_item("reason_code")?)?,
        exact_str(&event_id)?,
        vec![RELIEF_SOURCE.to_owned()],
    )?;
    Ok(match handoff {
        Some(record) => Some(PyTuple::new(py, [record])?),
        None => None,
    })
}

// ---- Completion --------------------------------------------------------------------------------------

/// One value of `_staff_operation_measurements`: kind, entity, path, value, unit.
struct Measure<'py> {
    kind: &'static str,
    entity: String,
    path: String,
    value: Bound<'py, PyAny>,
    unit: &'static str,
}

const BODY_MEASURES: [&str; 3] = ["uncertainty", "reassurance", "positive_affect"];

fn is_number(value: &Bound<'_, PyAny>) -> bool {
    !value.is_instance_of::<pyo3::types::PyBool>() && (value.is_instance_of::<pyo3::types::PyInt>() || value.is_instance_of::<pyo3::types::PyFloat>())
}

fn sorted_agents<'py>(world: &Bound<'py, PyAny>) -> PyResult<Vec<(String, Bound<'py, PyAny>)>> {
    let py = world.py();
    let mut agents = Vec::new();
    for agent in attribute(world, intern!(py, "agents"))?.call_method0(intern!(py, "values"))?.try_iter()? {
        let agent = agent?;
        agents.push((exact_str(&attribute(&agent, intern!(py, "id"))?)?.to_owned(), agent));
    }
    agents.sort_unstable_by(|first, second| first.0.cmp(&second.0));
    Ok(agents)
}

/// `_staff_operation_measurements`.
fn measurements<'py>(world: &Bound<'py, PyAny>, kind: &str, target: &Bound<'py, PyAny>, zone: &Bound<'py, PyAny>) -> PyResult<Vec<Measure<'py>>> {
    let py = world.py();
    let mut values = Vec::new();
    let table = table_of(world)?;
    let empty = PyDict::new(py);
    let mut add = |kind: &'static str, entity: &str, path: String, value: Bound<'py, PyAny>, unit: &'static str| {
        if is_number(&value) {
            values.push(Measure { kind, entity: entity.to_owned(), path, value, unit });
        }
    };
    match kind {
        "cleaning" if zone.is_instance_of::<PyString>() => {
            let record = table
                .call_method1(intern!(py, "get"), (NAME, &empty))?
                .call_method1(intern!(py, "get"), ("zones", &empty))?
                .call_method1(intern!(py, "get"), (zone,))?;
            if record.is_instance_of::<PyDict>() {
                add("model_state", exact_str(zone)?, "staff_operations_v1.zones.sanitation".to_owned(), get(&record, "sanitation")?, "normalized_0_1");
            }
        }
        "maintenance" if target.is_instance_of::<PyString>() => {
            let facility = attribute(world, intern!(py, "facilities"))?.call_method1(intern!(py, "get"), (target,))?;
            if !facility.is_none() {
                let id = attribute(&facility, intern!(py, "id"))?;
                add("facility", exact_str(&id)?, "working_units".to_owned(), attribute(&facility, intern!(py, "working_units"))?, "facility_unit");
            }
        }
        "information_outreach" | "patrol" if zone.is_instance_of::<PyString>() => {
            let body = table.call_method1(intern!(py, "get"), ("body_state_v1", &empty))?.call_method1(intern!(py, "get"), ("agents", &empty))?;
            for (id, agent) in sorted_agents(world)? {
                if !attribute(&agent, intern!(py, "zone_id"))?.eq(zone)? {
                    continue;
                }
                add("agent", &id, "trust".to_owned(), attribute(&agent, intern!(py, "trust"))?, "normalized_0_1");
                let record = body.call_method1(intern!(py, "get"), (&id,))?;
                if record.is_instance_of::<PyDict>() {
                    for field in BODY_MEASURES {
                        add("model_state", &id, format!("body_state_v1.agents.{field}"), get(&record, field)?, "normalized_0_1");
                    }
                }
            }
        }
        _ => {}
    }
    Ok(values)
}

/// `StaffOperationCompletionHandler.apply`, with `complete_staff_operation` inside it.
#[pyfunction]
fn complete<'py>(_handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let py = world.py();
    enum Work<'py> {
        None,
        Cleaning { zone: Bound<'py, PyAny>, sanitation: f64 },
        Repair { facility: Bound<'py, PyAny>, units: i64 },
        Blocked,
        People { people: Vec<(Bound<'py, PyAny>, Option<(Bound<'py, PyAny>, [f64; 3])>, f64)> },
        Intake,
    }
    struct Plan<'py> {
        state: Bound<'py, PyAny>,
        staff: Bound<'py, PyAny>,
        staff_id: Bound<'py, PyAny>,
        task_id: Bound<'py, PyAny>,
        kind: Bound<'py, PyAny>,
        target_id: Bound<'py, PyAny>,
        zone_id: Bound<'py, PyAny>,
        reason: Bound<'py, PyAny>,
        started: Bound<'py, PyAny>,
        arrival: Bound<'py, PyAny>,
        travel: Bound<'py, PyAny>,
        causation: Vec<String>,
        work: Work<'py>,
        before: Vec<Measure<'py>>,
        previous_units: Option<(Bound<'py, PyAny>, i64)>,
        payload_task: Bound<'py, PyAny>,
    }
    let plan = (|| -> PyResult<Plan<'py>> {
        if !text_is(&attribute(event, intern!(py, "event_type"))?, "staff_operation_complete") {
            return unsupported("another event");
        }
        let payload = attribute(event, intern!(py, "payload"))?;
        let payload_kind = get(&payload, "task_kind")?;
        let payload_target = get(&payload, "target_id")?;
        let payload_zone = get(&payload, "target_zone_id")?;
        let kind_text = exact_str(&payload_kind)?;
        let facility = if payload_target.is_instance_of::<PyString>() {
            attribute(world, intern!(py, "facilities"))?.call_method1(intern!(py, "get"), (&payload_target,))?
        } else {
            py.None().into_bound(py)
        };
        let previous_units = if kind_text == "maintenance" && !facility.is_none() {
            Some((facility.clone(), exact_int(&attribute(&facility, intern!(py, "working_units"))?)?))
        } else {
            None
        };
        let before = measurements(world, kind_text, &payload_target, &payload_zone)?;

        let state = get(&table_of(world)?, NAME)?;
        let staff_id = get(&payload, "staff_id")?;
        let task_id = get(&payload, "task_id")?;
        let id = exact_str(&staff_id)?;
        exact_str(&task_id)?;
        if !state.is_instance_of::<PyDict>() {
            return unsupported("no staff operations");
        }
        let record = state.get_item("staff")?.call_method1(intern!(py, "get"), (id,))?;
        let staff = staff_of(world, &staff_id)?;
        let task = task_of(world, &task_id)?;
        if record.is_none()
            || !text_is(&record.get_item("phase")?, "working")
            || !record.get_item("task_id")?.eq(&task_id)?
            || staff.is_none()
            || task.is_none()
            || !attribute(&staff, intern!(py, "active_task_id"))?.eq(&task_id)?
            || !attribute(event, intern!(py, "due_minute"))?.eq(record.get_item("due_minute")?)?
            || !payload_kind.eq(record.get_item("task_kind")?)?
            || !payload_zone.eq(record.get_item("target_zone_id")?)?
            || !available(world, id)?
        {
            return unsupported("the completion is for the Python rule to judge");
        }
        let kind = record.get_item("task_kind")?;
        let zone_id = record.get_item("target_zone_id")?;
        let target_id = record.get_item("target_id")?;
        let config = state.get_item("config")?;
        let effect = |name: &str| -> PyResult<f64> {
            let zone_effects = config.get_item("effects_by_zone")?.call_method1(intern!(py, "get"), (&zone_id, PyDict::new(py)))?;
            number(&zone_effects.call_method1(intern!(py, "get"), (name, config.get_item(name)?))?)
        };
        let work = match exact_str(&kind)? {
            "cleaning" => {
                let sanitation = exact_float(&state.get_item("zones")?.get_item(&zone_id)?.get_item("sanitation")?)?;
                Work::Cleaning { zone: zone_id.clone(), sanitation: py_min(1.0, sanitation + effect("cleaning_sanitation_recovery")?) }
            }
            "maintenance" if attribute(world, intern!(py, "facilities"))?.contains(&target_id)? => {
                let found = attribute(world, intern!(py, "facilities"))?.get_item(&target_id)?;
                let units = exact_int(&attribute(&found, intern!(py, "working_units"))?)?;
                if units >= exact_int(&attribute(&found, intern!(py, "capacity"))?)?
                    || active_outage(world, exact_str(&target_id)?)?
                    || has_active_modifier(world, &target_id)?
                {
                    Work::Blocked
                } else {
                    Work::Repair { facility: found, units: units + 1 }
                }
            }
            "information_outreach" | "patrol" => {
                let relief = effect(if text_is(&kind, "information_outreach") { "outreach_uncertainty_relief" } else { "patrol_reassurance" })?;
                let empty = PyDict::new(py);
                let body = table_of(world)?.call_method1(intern!(py, "get"), ("body_state_v1", &empty))?.call_method1(intern!(py, "get"), ("agents", &empty))?;
                let mut people = Vec::new();
                for (id, agent) in sorted_agents(world)? {
                    if !attribute(&agent, intern!(py, "zone_id"))?.eq(&zone_id)? {
                        continue;
                    }
                    let record_body = body.call_method1(intern!(py, "get"), (&id,))?;
                    let next_body = if record_body.is_none() {
                        None
                    } else {
                        let uncertainty = py_max(0.0, exact_float(&record_body.get_item("uncertainty")?)? - relief);
                        let reassurance = py_min(1.0, exact_float(&record_body.get_item("reassurance")?)? + relief * 0.5);
                        let positive = py_min(1.0, exact_float(&record_body.get_item("positive_affect")?)? + relief * 0.25);
                        Some((record_body, [uncertainty, reassurance, positive]))
                    };
                    let trust = py_min(1.0, exact_float(&attribute(&agent, intern!(py, "trust"))?)? + relief * 0.1);
                    people.push((agent, next_body, trust));
                }
                Work::People { people }
            }
            "intake" => Work::Intake,
            _ => Work::None,
        };
        let causation = causation_with(&record, &[exact_str(&attribute(event, intern!(py, "event_id"))?)?.to_owned()])?;
        let started = record.get_item("started_minute")?;
        let arrival = record.get_item("arrival_minute")?;
        let arrival = if arrival.is_none() { started.clone() } else { arrival };
        Ok(Plan {
            reason: record.get_item("reason")?,
            travel: record.get_item("travel_minutes")?,
            state,
            staff,
            staff_id,
            payload_task: payload.get_item("task_id")?,
            task_id,
            kind,
            target_id,
            zone_id,
            started,
            arrival,
            causation,
            work,
            before,
            previous_units,
        })
    })();
    let plan = counted(py, "complete", refused_by_python(py, plan))?;
    let Plan { state, staff, staff_id, task_id, kind, target_id, zone_id, reason, started, arrival, travel, causation, work, before, previous_units, payload_task } = plan;
    let now = attribute(world, intern!(py, "minute_index"))?;
    let due = attribute(event, intern!(py, "due_minute"))?;

    // Write: `complete_staff_operation`.
    let mut outcome = "completed";
    match work {
        Work::None => {}
        Work::Blocked => outcome = "blocked",
        Work::Cleaning { zone, sanitation } => {
            state.get_item("zones")?.get_item(&zone)?.set_item("sanitation", sanitation)?;
        }
        Work::Repair { facility, units } => {
            facility.setattr(intern!(py, "working_units"), units)?;
            let infrastructure = get(&table_of(world)?, "infrastructure_v1")?;
            if infrastructure.is_instance_of::<PyDict>() {
                infrastructure.get_item("base_working_units")?.set_item(&target_id, units)?;
            }
        }
        Work::People { people } => {
            for (agent, body, trust) in people {
                if let Some((record, [uncertainty, reassurance, positive])) = body {
                    record.set_item("uncertainty", uncertainty)?;
                    record.set_item("reassurance", reassurance)?;
                    record.set_item("positive_affect", positive)?;
                }
                agent.setattr(intern!(py, "trust"), trust)?;
            }
        }
        Work::Intake => {
            py.import("sheltersim.domain.population")?.getattr("admit_pending_arrivals")?.call1((world, &now))?;
        }
    }
    let operation = PyDict::new(py);
    operation.set_item("task_id", &task_id)?;
    operation.set_item("staff_id", &staff_id)?;
    operation.set_item("task_kind", &kind)?;
    operation.set_item("target_id", &target_id)?;
    operation.set_item("target_zone_id", &zone_id)?;
    operation.set_item("reason", &reason)?;
    operation.set_item("status", outcome)?;
    operation.set_item("start_minute", &started)?;
    operation.set_item("arrival_minute", &arrival)?;
    operation.set_item("complete_minute", &due)?;
    operation.set_item("travel_minutes", &travel)?;
    operation.set_item("causation_event_ids", PyList::new(py, &causation)?)?;
    append_history(&state, py.import("copy")?.getattr("deepcopy")?.call1((&operation,))?.downcast_into::<PyDict>()?)?;
    attribute(world, intern!(py, "active_tasks"))?.del_item(&task_id)?;
    free_staff(world, &staff)?;
    state.get_item("staff")?.set_item(&staff_id, idle_record(py)?)?;

    // The handler's receipts.
    let payload = attribute(event, intern!(py, "payload"))?;
    let kind_text = exact_str(&get(&payload, "task_kind")?)?.to_owned();
    let after = measurements(world, &kind_text, &get(&payload, "target_id")?, &get(&payload, "target_zone_id")?)?;
    let measured = !before.is_empty();
    let mut changes = Vec::new();
    let mut sorted_after = after;
    sorted_after.sort_by(|first, second| (first.kind, &first.entity, &first.path).cmp(&(second.kind, &second.entity, &second.path)));
    for measure in &sorted_after {
        let Some(previous) = before.iter().find(|known| known.kind == measure.kind && known.entity == measure.entity && known.path == measure.path) else {
            continue;
        };
        if previous.value.ne(&measure.value)? {
            let change = PyDict::new(py);
            change.set_item("entity_id", &measure.entity)?;
            change.set_item("entity_kind", measure.kind)?;
            change.set_item("field_path", &measure.path)?;
            change.set_item("before", previous.value.extract::<f64>()?)?;
            change.set_item("after", measure.value.extract::<f64>()?)?;
            change.set_item("unit", measure.unit)?;
            changes.push(change);
        }
    }
    let event_id = attribute(event, intern!(py, "event_id"))?;
    let event_text = exact_str(&event_id)?;
    let mut followups = Vec::new();
    if let Some(record) = staff_operation_record(world, OperationOutcome { operation: &operation, changes, measured, causation: event_text })? {
        followups.push(record);
    }
    if let Some((facility, previous)) = previous_units {
        let units = exact_int(&attribute(&facility, intern!(py, "working_units"))?)?;
        if units != previous {
            let source = attribute(event, intern!(py, "source_id"))?;
            let sources = match exact_str(&source) {
                Ok(text) if !text.is_empty() => vec![text.to_owned()],
                _ => Vec::new(),
            };
            let id = attribute(&facility, intern!(py, "id"))?;
            if let Some(record) = facility_recovery_record(world, exact_str(&id)?, exact_str(&payload_task)?, "restored", previous, units, event_text, sources)? {
                followups.push(record);
            }
        }
    }
    if followups.is_empty() {
        return Ok(None);
    }
    Ok(Some(PyTuple::new(py, followups)?))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(interrupt, m)?)?;
    m.add_function(wrap_pyfunction!(arrive, m)?)?;
    m.add_function(wrap_pyfunction!(relief, m)?)?;
    m.add_function(wrap_pyfunction!(complete, m)?)?;
    Ok(())
}
