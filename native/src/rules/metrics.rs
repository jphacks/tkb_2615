//! `experiments/metrics.py` and `experiments/operational_kpis.py`: the KPI columns of a minute, read
//! from the rows in one call. They only read, so a call changes nothing.
//!
//! Anything the Python rule would read differently says Unsupported, and it runs and gives the same
//! columns or its own error: a tainted row, a kind that is not ASCII (`str.casefold`), a pending
//! request with a free qualified worker (`rules/staff.rs`), a staff queue naming no task (KeyError).
//!
//! The ID lists and the JSON are written by byte order, which is the code point order of Python's
//! `sorted` and of `canonical_json`.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

use crate::core::mathx::py_sum;
use crate::core::row::{attribute, exact_dict, exact_int, exact_str, namespace_is_absent, unsupported};
use crate::core::world_core::{field_mirror_of, NamespaceMirror};
use crate::rules::staff::{open_waits, OpenWaits};
use crate::state::entities::{entities_of, EntityStore};
use crate::state::events::EventWriter;
use crate::state::metric_state::MetricRow;
use crate::state::resource_ledger::{held_mirror, LedgerRow};
use crate::write_json_string;

const HIGH_STRESS_AT_LEAST: f64 = 0.7;
const EXPOSURE_SEMANTICS: &str = "per-minute observed request exposure by blocker; reason totals may overlap \
                                  and are not exclusive causal attribution";

/// `"toilet" in kind.casefold()`. Only for ASCII, where casefold is lower.
fn is_toilet(kind: &str) -> PyResult<bool> {
    if !kind.is_ascii() {
        return unsupported("a facility kind is not ASCII");
    }
    Ok(kind.as_bytes().windows(6).any(|part| part.eq_ignore_ascii_case(b"toilet")))
}

fn write_ids(out: &mut Vec<u8>, ids: &[impl AsRef<str>]) {
    out.push(b'[');
    for (index, id) in ids.iter().enumerate() {
        if index > 0 {
            out.push(b',');
        }
        write_json_string(out, id.as_ref());
    }
    out.push(b']');
}

fn write_key(out: &mut Vec<u8>, key: &str, first: bool) {
    if !first {
        out.push(b',');
    }
    write_json_string(out, key);
    out.push(b':');
}

/// `support_wait_by_agent`, by agent: a later request of the same agent replaces an earlier one, as
/// in the dict the Python rule fills.
fn write_waits(out: &mut Vec<u8>, open: &OpenWaits<'_>) -> PyResult<()> {
    let mut order: Vec<usize> = (0..open.requests.len()).collect();
    order.sort_by(|first, second| open.requests[*first].agent.as_bytes().cmp(open.requests[*second].agent.as_bytes()));
    out.push(b'{');
    let mut first = true;
    for (place, index) in order.iter().enumerate() {
        let request = &open.requests[*index];
        if order.get(place + 1).is_some_and(|next| open.requests[*next].agent == request.agent) {
            continue;
        }
        write_key(out, &request.agent, first);
        first = false;
        out.extend_from_slice(b"{\"reason_codes\":");
        write_ids(out, &open.reasons_of(request));
        out.extend_from_slice(b",\"request_id\":");
        write_json_string(out, exact_str(&request.id)?);
        out.extend_from_slice(b",\"waited_minutes\":");
        out.extend_from_slice(itoa::Buffer::new().format(request.waited).as_bytes());
        out.push(b'}');
    }
    out.push(b'}');
    Ok(())
}

/// `MetricSnapshot.from_world`: the fields of the dataclass in their order, for a sample minute
/// that is the world's. `unsettled` is `world.unsettled_support_requests()` as a list.
#[pyfunction]
fn snapshot_fields<'py>(
    world: &Bound<'py, PyAny>,
    unsettled: &Bound<'py, PyList>,
    sample_minute: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let py = world.py();
    let sample = exact_int(sample_minute)?;
    let open = open_waits(world, unsettled, sample, true)?;
    let writer = EventWriter::of(world)?;
    let store = entities_of(world)?;
    let mut queued: Vec<std::sync::Arc<str>> = Vec::new();
    let mut active: Vec<std::sync::Arc<str>> = Vec::new();
    let mut high_stress: Vec<std::sync::Arc<str>> = Vec::new();
    let mut staff_tasks: Vec<std::sync::Arc<str>> = Vec::new();
    let mut busy_staff: Vec<std::sync::Arc<str>> = Vec::new();
    let (working_units, stress_mean, staff_count) = EntityStore::with_rows(&store, |rows| {
        let mut working: i64 = 0;
        for (_, facility) in rows.facilities.iter() {
            if is_toilet(&facility.kind)? {
                queued.extend(facility.queue_agent_ids.iter().cloned());
                active.extend(facility.active_agent_ids.iter().cloned());
                let Some(total) = working.checked_add(facility.working_units) else {
                    return unsupported("the working units do not fit an i64");
                };
                working = total;
            }
        }
        let mut stresses = Vec::with_capacity(rows.agents.len());
        for (_, agent) in rows.agents.iter() {
            stresses.push(agent.stress);
            if agent.stress >= HIGH_STRESS_AT_LEAST {
                high_stress.push(agent.profile.id.clone());
            }
        }
        let mean = if stresses.is_empty() { 0.0 } else { py_sum(&stresses) / stresses.len() as f64 };
        for (_, member) in rows.staff.iter() {
            let mut busy = false;
            for task_id in &member.work_queue {
                staff_tasks.push(task_id.clone());
                let Some(task) = rows.active_tasks.get(task_id) else {
                    return unsupported("a staff queue names no task");
                };
                busy |= task.start_minute <= sample && sample < task.due_minute;
            }
            if busy {
                busy_staff.push(member.id.clone());
            }
        }
        Ok((working, mean, rows.staff.len()))
    })?;
    queued.sort_unstable();
    active.sort_unstable();
    high_stress.sort_unstable();
    staff_tasks.sort_unstable();
    busy_staff.sort_unstable();
    let mut unresolved: Vec<&str> = open.requests.iter().map(|request| &*request.agent).collect();
    unresolved.sort_unstable();
    let mut no_qualified: Vec<&str> = open
        .requests
        .iter()
        .filter(|request| open.has_no_qualified_staff(request))
        .map(|request| &*request.agent)
        .collect();
    no_qualified.sort_unstable();
    let log = writer.log().borrow();
    let mut events: Vec<&str> = log.event_ids_at(sample).collect();
    events.sort_unstable();

    // Keys by their bytes, as canonical_json writes them.
    let mut out: Vec<u8> = Vec::with_capacity(1024);
    out.push(b'{');
    write_key(&mut out, "busy_staff", true);
    write_ids(&mut out, &busy_staff);
    write_key(&mut out, "high_stress_agents", false);
    write_ids(&mut out, &high_stress);
    write_key(&mut out, "realized_events", false);
    write_ids(&mut out, &events);
    write_key(&mut out, "staff_tasks", false);
    write_ids(&mut out, &staff_tasks);
    write_key(&mut out, "support_requests_without_qualified_staff", false);
    write_ids(&mut out, &no_qualified);
    write_key(&mut out, "support_wait_by_agent", false);
    write_waits(&mut out, &open)?;
    write_key(&mut out, "support_wait_exposure_semantics", false);
    write_json_string(&mut out, EXPOSURE_SEMANTICS);
    write_key(&mut out, "toilet_active_agents", false);
    write_ids(&mut out, &active);
    write_key(&mut out, "toilet_queue_agents", false);
    write_ids(&mut out, &queued);
    write_key(&mut out, "unresolved_support_agents", false);
    write_ids(&mut out, &unresolved);
    out.push(b'}');
    // Only str went in, and write_json_string writes UTF-8.
    let source_ids = PyString::new(py, std::str::from_utf8(&out)?);

    let metrics = field_mirror_of(world, intern!(py, "metric_state"))?;
    let read = |row: &MetricRow, name: &str| row.get(name).copied().unwrap_or(0.0);
    let (count, total, longest, no_worker, promoted, staff_busy, off_shift) =
        NamespaceMirror::with_row::<MetricRow, _>(&metrics, |row| {
            Ok((
                read(row, "toilet_wait_count"),
                read(row, "toilet_wait_total_minutes"),
                read(row, "toilet_wait_max_minutes"),
                read(row, "support_no_qualified_worker_request_minutes"),
                read(row, "support_fairness_promoted_assignment_count"),
                read(row, "support_wait_staff_busy_request_minutes"),
                read(row, "support_wait_staff_off_shift_request_minutes"),
            ))
        })?;
    let mean_wait = if count != 0.0 { total / count } else { 0.0 };
    let load = if staff_count == 0 { 0.0 } else { busy_staff.len() as f64 / staff_count as f64 };
    let fields: [Bound<'py, PyAny>; 20] = [
        sample_minute.clone(),
        queued.len().into_pyobject(py)?.into_any(),
        active.len().into_pyobject(py)?.into_any(),
        working_units.into_pyobject(py)?.into_any(),
        count.into_pyobject(py)?.into_any(),
        total.into_pyobject(py)?.into_any(),
        mean_wait.into_pyobject(py)?.into_any(),
        longest.into_pyobject(py)?.into_any(),
        stress_mean.into_pyobject(py)?.into_any(),
        high_stress.len().into_pyobject(py)?.into_any(),
        staff_tasks.len().into_pyobject(py)?.into_any(),
        busy_staff.len().into_pyobject(py)?.into_any(),
        load.into_pyobject(py)?.into_any(),
        unresolved.len().into_pyobject(py)?.into_any(),
        source_ids.into_any(),
        no_qualified.len().into_pyobject(py)?.into_any(),
        no_worker.into_pyobject(py)?.into_any(),
        promoted.into_pyobject(py)?.into_any(),
        staff_busy.into_pyobject(py)?.into_any(),
        off_shift.into_pyobject(py)?.into_any(),
    ];
    PyTuple::new(py, fields)
}

/// `operational_kpi_values`: the columns in the order of the Python rule's dict.
#[pyfunction]
fn kpi_values<'py>(world: &Bound<'py, PyAny>, snapshot: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let py = world.py();
    if exact_int(&attribute(world, intern!(py, "schema_version"))?)? != 6
        || namespace_is_absent(world, intern!(py, "resource_service_v1"))?
    {
        return unsupported("the Python rule says what is wrong with this world");
    }
    let minute = attribute(world, intern!(py, "minute_index"))?;
    if exact_int(&attribute(snapshot, intern!(py, "minute_index"))?)? != exact_int(&minute)? {
        return unsupported("the Python rule says what is wrong with this minute");
    }
    let store = entities_of(world)?;
    let (toilet, hunger, hydration, water_queue, meal_queue, water_active, meal_active) =
        EntityStore::with_rows(&store, |rows| {
            let count = rows.agents.len();
            if count == 0 {
                return unsupported("no agents: the Python rule divides by zero");
            }
            let mean = |need: fn(&crate::state::entities::NeedRow) -> f64| {
                let values: Vec<f64> = rows.agents.iter().map(|(_, agent)| need(&agent.needs)).collect();
                py_sum(&values) / count as f64
            };
            let means = (mean(|needs| needs.toilet_urgency), mean(|needs| needs.hunger), mean(|needs| needs.hydration_need));
            let (mut water_queue, mut meal_queue, mut water_active, mut meal_active) = (0, 0, 0, 0);
            for (_, facility) in rows.facilities.iter() {
                match &*facility.kind {
                    "water_station" => {
                        water_queue += facility.resource_queue_agent_ids.len();
                        water_active += facility.resource_active_agent_ids.len();
                    }
                    "meal_distribution" => {
                        meal_queue += facility.resource_queue_agent_ids.len();
                        meal_active += facility.resource_active_agent_ids.len();
                    }
                    _ => {}
                }
            }
            Ok((means.0, means.1, means.2, water_queue, meal_queue, water_active, meal_active))
        })?;
    let resources = attribute(world, intern!(py, "resources"))?;
    let resources = exact_dict(&resources)?;
    let stock = |name: &Bound<'py, PyString>| -> PyResult<Bound<'py, PyAny>> {
        resources.get_item(name)?.map_or_else(|| unsupported("a resource is missing"), Ok)
    };
    let ledger = held_mirror(world)?;
    let (water_reserved, food_reserved) = NamespaceMirror::with_row::<LedgerRow, _>(&ledger, |row| {
        Ok((row.accounts.of(0).reserved, row.accounts.of(1).reserved))
    })?;
    let metrics = field_mirror_of(world, intern!(py, "metric_state"))?;
    const WAITS: [(&str, &str, &str, &str); 2] = [
        ("water_service_wait_count", "water_service_wait_total_minutes", "water_service_wait_mean_minutes", "water_service_wait_max_minutes"),
        ("meal_service_wait_count", "meal_service_wait_total_minutes", "meal_service_wait_mean_minutes", "meal_service_wait_max_minutes"),
    ];
    let read = |row: &MetricRow, name: &str| row.get(name).copied().unwrap_or(0.0);
    let (wait_values, outage, stock_stall) = NamespaceMirror::with_row::<MetricRow, _>(&metrics, |row| {
        let wait = |names: &(&str, &str, &str, &str)| (read(row, names.0), read(row, names.1), read(row, names.3));
        Ok((
            [wait(&WAITS[0]), wait(&WAITS[1])],
            read(row, "resource_service_wait_water_outage_count"),
            read(row, "resource_service_wait_stock_count"),
        ))
    })?;
    let values = PyDict::new(py);
    values.set_item(intern!(py, "minute_index"), &minute)?;
    values.set_item(intern!(py, "toilet_urgency_mean"), toilet)?;
    values.set_item(intern!(py, "hunger_need_mean"), hunger)?;
    values.set_item(intern!(py, "hydration_need_mean"), hydration)?;
    values.set_item(intern!(py, "water_queue_length"), water_queue)?;
    values.set_item(intern!(py, "meal_queue_length"), meal_queue)?;
    values.set_item(intern!(py, "water_active_service_count"), water_active)?;
    values.set_item(intern!(py, "meal_active_service_count"), meal_active)?;
    values.set_item(intern!(py, "water_ml_remaining"), stock(intern!(py, "water_ml"))?)?;
    values.set_item(intern!(py, "food_meal_units_remaining"), stock(intern!(py, "food_meal_units"))?)?;
    values.set_item(intern!(py, "water_ml_reserved"), water_reserved)?;
    values.set_item(intern!(py, "food_meal_units_reserved"), food_reserved)?;
    for name in [
        intern!(py, "stress_mean"),
        intern!(py, "staff_busy_count"),
        intern!(py, "staff_load_ratio"),
        intern!(py, "unresolved_support_request_count"),
        intern!(py, "support_no_qualified_worker_count"),
        intern!(py, "support_no_qualified_worker_request_minutes"),
        intern!(py, "support_fairness_promoted_assignment_count"),
        intern!(py, "support_wait_staff_busy_request_minutes"),
        intern!(py, "support_wait_staff_off_shift_request_minutes"),
    ] {
        values.set_item(name, attribute(snapshot, name)?)?;
    }
    for (names, (count, total, longest)) in WAITS.iter().zip(wait_values) {
        values.set_item(names.0, count)?;
        values.set_item(names.1, total)?;
        values.set_item(names.2, if count != 0.0 { total / count } else { 0.0 })?;
        values.set_item(names.3, longest)?;
    }
    values.set_item(intern!(py, "water_outage_stall_count"), outage)?;
    values.set_item(intern!(py, "resource_stock_stall_count"), stock_stall)?;
    Ok(values)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(snapshot_fields, m)?)?;
    m.add_function(wrap_pyfunction!(kpi_values, m)?)?;
    Ok(())
}
