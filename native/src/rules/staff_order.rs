//! `validate_staff_operation_order` and `order_staff_operation` of `domain/staff_operations.py`: an
//! operator's order of one routed staff task, as an EffectProgram's `staff_task_order` gives it in the
//! tick (`effects.rs` `staff_task_order`, on the copy of its transaction).
//!
//! The checks are the Python rule's, in its order, with its texts (`core/errors.rs`): each is raised
//! before anything is written. What the rule does not let Rust decide is Unsupported, before the first
//! write too. The writes are `order_staff_operation`'s, in its order, on the same Python containers.
//! `validate_staff_operation_order` stays in Python for `validate_effect_program`, which checks a
//! program before it is scheduled (docs/rust-port/lanes/l8-texts-events.md).

use std::collections::HashSet;
use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySlice, PyString};

use crate::core::errors;
use crate::core::row::{attribute, exact_dict, exact_int, exact_str, namespace_is_absent, unsupported};
use crate::core::value::V;
use crate::core::world_core::NamespaceMirror;
use crate::rules::effects::{active_outage, has_active_modifier};
use crate::rules::handlers::{assumption, get, new_event, NewEvent};
use crate::rules::movement_geometry::Geo;
use crate::rules::service::active_task_class;
use crate::rules::staff::{formula_of, mirror_held};
use crate::rules::staff_dynamics::{is_available, is_available_now, staff_status};
use crate::rules::staff_operation_events::relief_skills;
use crate::rules::travel::{passage_edge_loads, world_graph, Route};
use crate::state::entities::{entities_of, EntityStore};
use crate::state::staff_operations::{Phase, StaffOperationsRow, NAME};

const SHARED_OPERATIONS: &str = "shared_operations_v1";
const MOVEMENT: &str = "movement_v1";

/// `_TASK_NAMES`.
fn task_name(kind: &str) -> &'static str {
    match kind {
        "cleaning" => "清掃",
        "maintenance" => "保守",
        "information_outreach" => "情報提供",
        "intake" => "受付",
        _ => "巡回",
    }
}

/// An order, as `order_staff_operation` takes it.
pub(crate) struct Order<'a> {
    pub(crate) staff_id: &'a str,
    pub(crate) task_kind: &'a str,
    pub(crate) target_zone_id: &'a str,
    pub(crate) target_id: Option<&'a str>,
    pub(crate) reason: &'a str,
    pub(crate) causation_event_id: &'a str,
    pub(crate) source_id: Option<&'a str>,
}

/// `_missing_task_skill_message`: who could take the order instead, by ID.
fn missing_skill_message(world: &Bound<'_, PyAny>, staff_id: &str, kind: &str, skills: [&'static str; 2]) -> PyResult<String> {
    let store = entities_of(world)?;
    let mut qualified = EntityStore::with_rows(&store, |rows| {
        Ok(rows
            .staff
            .iter()
            .filter(|(_, other)| other.skills.0.iter().any(|skill| skills.contains(&&**skill)))
            .map(|(id, _)| id.to_string())
            .collect::<Vec<_>>())
    })?;
    qualified.sort_unstable();
    let mut sorted = skills;
    sorted.sort_unstable();
    let name = task_name(kind);
    let skill_list = sorted.join("・");
    if !qualified.is_empty() {
        return Ok(format!(
            "staff_lacks_task_skill: {staff_id}は{name}の技能（{skill_list}）を持ちません。技能を持つ職員（{}）に指示してください。",
            qualified.join("、")
        ));
    }
    let advice = if kind == "maintenance" {
        "設備の修理は修理の手配（facility_repair）で指示するか、担当変更（set_staff_role）で職員に技能を与えてから指示してください。"
    } else {
        "担当変更（set_staff_role）で職員に技能を与えてから指示してください。"
    };
    Ok(format!("no_qualified_staff: {name}の技能（{skill_list}）を持つ職員がいません。{advice}"))
}

/// What the checks found: the route and the task's minutes of work.
struct Checked {
    route: Route,
    duration: i64,
}

/// `validate_staff_operation_order(world, ..., minute=minute)`: the route, or the rule's text.
fn validate(world: &Bound<'_, PyAny>, order: &Order<'_>, minute: i64) -> PyResult<Checked> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    let failed = |text: &str| errors::failed::<Checked>(py, text);
    // `state is None or SHARED_OPERATIONS_KEY not in world.model_state`.
    if namespace_is_absent(world, intern!(py, NAME))? || !exact_dict(&table)?.contains(SHARED_OPERATIONS)? {
        return failed("staff task order requires staff and shared operation state");
    }
    let mirror = mirror_held(world, NAME)?;
    let store = entities_of(world)?;
    let staff = EntityStore::with_rows(&store, |rows| {
        Ok(rows.staff.get(order.staff_id).map(|staff| {
            (staff.zone_id.clone(), staff.skills.0.clone(), staff.active_task_id.is_some())
        }))
    })?;
    let idle = NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror, |row| {
        Ok(row.staff.get(order.staff_id).map(|record| record.phase == Phase::Idle))
    })?;
    let (Some((zone, skills, has_task)), Some(idle)) = (staff, idle) else {
        return failed("staff task order references unknown staff");
    };
    // Each test is read only if those before it passed, as `or` reads them.
    let id = PyString::new(py, order.staff_id);
    let busy = !idle
        || has_task
        || !is_available(staff_status(world)?.as_ref(), &id)?
        || !is_available_now(world, id.as_any(), &minute.into_pyobject(py)?.into_any())?;
    if busy {
        return failed("staff task order requires an idle, available, on-shift worker");
    }
    let Some(required) = relief_skills(order.task_kind) else {
        return failed("staff task order uses an unregistered task kind");
    };
    if !skills.iter().any(|skill| required.contains(&&**skill)) {
        let text = missing_skill_message(world, order.staff_id, order.task_kind, required)?;
        return errors::failed(py, text);
    }
    if !attribute(world, intern!(py, "zones"))?.contains(order.target_zone_id)? {
        return failed("staff task order references an unknown target zone");
    }
    if order.task_kind == "maintenance" {
        // `world.facilities.get(target_id or "")`.
        let facility = EntityStore::with_rows(&store, |rows| {
            Ok(rows
                .facilities
                .get(order.target_id.unwrap_or(""))
                .map(|facility| (facility.id.clone(), facility.zone_id.clone(), facility.working_units, facility.capacity)))
        })?;
        let Some((facility_id, facility_zone, working, capacity)) = facility else {
            return failed("maintenance order must target a facility in its selected zone");
        };
        if &*facility_zone != order.target_zone_id {
            return failed("maintenance order must target a facility in its selected zone");
        }
        // `_maintenance_blocked`: the outage, then the modifiers.
        if working >= capacity
            || active_outage(world, &facility_id)?
            || has_active_modifier(world, PyString::new(py, &facility_id).as_any())?
        {
            return failed("maintenance order has no currently repairable facility unit");
        }
    } else if order.target_id.is_some() {
        return failed("only maintenance orders accept a target facility");
    }
    if matches!(order.task_kind, "information_outreach" | "patrol") {
        let anyone = EntityStore::with_rows(&store, |rows| {
            Ok(rows.agents.iter().any(|(_, agent)| &*agent.zone_id == order.target_zone_id))
        })?;
        if !anyone {
            return failed("staff support order requires residents in its target zone");
        }
        if order.task_kind == "information_outreach" {
            // `world.model_state.get("body_state_v1", {}).get("agents")`.
            let body = table.call_method1(intern!(py, "get"), ("body_state_v1", PyDict::new(py)))?;
            if !get(&body, "agents")?.is_truthy()? {
                return failed("information outreach requires the body-state policy");
            }
        }
    }
    if order.task_kind == "intake" {
        let population = get(&table, "population_dynamics_v1")?;
        if !population.is_instance_of::<PyDict>() || !get(&population, "pending_arrivals")?.is_truthy()? {
            return failed("intake order requires pending arrivals");
        }
    }
    // `ZoneGraph.from_states(world.zones, world.zone_edges)` has no edge capacities, so the loads it is
    // given turn no edge down; they are read as the rule reads them.
    let graph = world_graph(world)?;
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let closed = if exact_dict(&configs)?.contains("facility_geometry")? {
        graph.edges_closed_by(&*Geo::of(world)?, "independent")
    } else {
        HashSet::new()
    };
    passage_edge_loads(world, minute)?;
    let Some(route) = graph.shortest(&closed, None, &zone, &Arc::from(order.target_zone_id))? else {
        return failed("staff task order has no reachable route");
    };
    let duration = NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror, |row| {
        let V::Map(config) = &row.config else {
            return unsupported("the config is not a map");
        };
        let Some(V::Map(durations)) = config.get("task_duration_minutes") else {
            return unsupported("the task durations are not a map");
        };
        match durations.get(order.task_kind) {
            Some(V::Int(minutes)) => Ok(*minutes),
            _ => unsupported("a task duration is not an int"),
        }
    })?;
    let run_end = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
    if i128::from(minute) + i128::from(route.minutes) + i128::from(duration) >= i128::from(run_end) {
        return failed("staff task order would finish beyond the Run boundary");
    }
    Ok(Checked { route, duration })
}

/// `order_staff_operation`: the task's ID. Its checks raise before any change; past them, the writes
/// are the rule's, in its order. Called on the copy of a transaction.
pub(crate) fn order_operation(world: &Bound<'_, PyAny>, order: &Order<'_>) -> PyResult<String> {
    let py = world.py();
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let Checked { route, duration } = validate(world, order, minute)?;
    let due = minute + route.minutes + duration;
    let run_end = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
    // As the checks have it, with the same minute: never reached.
    if due >= run_end {
        return errors::failed(py, "staff task order would finish beyond the Run boundary");
    }
    let (_, formula_source, formula_policy, _) = formula_of(world)?;
    let formula_policy = exact_str(&formula_policy)?.to_owned();
    // `source_id or formula_source_id`.
    let event_source = match order.source_id {
        Some(source) if !source.is_empty() => PyString::new(py, source).into_any(),
        _ => formula_source.clone(),
    };
    let table = attribute(world, intern!(py, "model_state"))?;
    let records = get(&get(&table, NAME)?, "staff")?;
    let movement = exact_dict(&table)?.contains(MOVEMENT)?;
    let travel = route.minutes;
    let arrival = minute + travel;

    // Write.
    let sequence: i64 = world.call_method0(intern!(py, "allocate_event_sequence"))?.extract()?;
    let task_id = format!("staff-operation-{sequence}");
    let target = order.target_id.map(|target| PyString::new(py, target).into_any()).unwrap_or_else(|| py.None().into_bound(py));
    let record = PyDict::new(py);
    record.set_item("phase", if travel != 0 { "travel" } else { "working" })?;
    record.set_item("task_id", &task_id)?;
    record.set_item("task_kind", order.task_kind)?;
    record.set_item("target_id", &target)?;
    record.set_item("target_zone_id", order.target_zone_id)?;
    record.set_item("reason", order.reason)?;
    record.set_item("started_minute", minute)?;
    record.set_item("travel_due_minute", if travel != 0 { Some(arrival) } else { None })?;
    record.set_item("arrival_minute", if travel != 0 { None } else { Some(arrival) })?;
    record.set_item("due_minute", due)?;
    let (zones, edges): (&[Arc<str>], &[Arc<str>]) = if travel != 0 { (&route.zone_ids, &route.edge_ids) } else { (&[], &[]) };
    record.set_item("route_zone_ids", PyList::new(py, zones.iter().map(|id| &**id))?)?;
    record.set_item("route_edge_ids", PyList::new(py, edges.iter().map(|id| &**id))?)?;
    record.set_item("travel_minutes", travel)?;
    record.set_item("causation_event_ids", PyList::new(py, [order.causation_event_id])?)?;
    records.set_item(order.staff_id, record)?;
    let task = PyDict::new(py);
    task.set_item("id", &task_id)?;
    task.set_item("kind", "staff_operation")?;
    task.set_item("agent_id", py.None())?;
    task.set_item("resource_id", py.None())?;
    task.set_item("start_minute", minute)?;
    task.set_item("due_minute", due)?;
    task.set_item("staff_id", order.staff_id)?;
    attribute(world, intern!(py, "active_tasks"))?.set_item(&task_id, active_task_class(py)?.call((), Some(&task))?)?;
    let staff = attribute(world, intern!(py, "staff"))?.get_item(order.staff_id)?;
    staff.setattr(intern!(py, "active_task_id"), &task_id)?;
    attribute(&staff, intern!(py, "work_queue"))?.set_item(PySlice::full(py), PyList::new(py, [&task_id])?)?;
    staff.setattr(intern!(py, "busy_until"), due)?;
    let provenance = assumption(py, &formula_source, &formula_policy)?;
    provenance.set_item("reason", order.reason)?;
    let causation = PyString::new(py, order.causation_event_id).into_any();
    if travel != 0 {
        let payload = PyDict::new(py);
        payload.set_item("staff_id", order.staff_id)?;
        payload.set_item("task_id", &task_id)?;
        payload.set_item("target_zone_id", order.target_zone_id)?;
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
                source_id: Some(event_source.clone()),
                causation_id: Some(causation.clone()),
                provenance: provenance.clone().into_any(),
            },
        )?;
        world.call_method1(intern!(py, "schedule_event"), (event,))?;
    }
    let completion: i64 = world.call_method0(intern!(py, "allocate_event_sequence"))?.extract()?;
    let payload = PyDict::new(py);
    payload.set_item("staff_id", order.staff_id)?;
    payload.set_item("task_id", &task_id)?;
    payload.set_item("task_kind", order.task_kind)?;
    payload.set_item("target_id", &target)?;
    payload.set_item("target_zone_id", order.target_zone_id)?;
    let event = new_event(
        py,
        NewEvent {
            event_id: &format!("staff-operation-complete-{completion}"),
            kind: "COMPLETION",
            due_minute: due,
            priority: 5,
            sequence: completion,
            event_type: "staff_operation_complete",
            payload: payload.into_any(),
            source_id: Some(event_source),
            causation_id: Some(causation),
            provenance: provenance.into_any(),
        },
    )?;
    world.call_method1(intern!(py, "schedule_event"), (event,))?;
    if movement {
        let kwargs = PyDict::new(py);
        kwargs.set_item("boundary_minute", minute + 1)?;
        py.import("sheltersim.domain.movement")?.getattr("staff_position_snapshots")?.call((world,), Some(&kwargs))?;
    }
    Ok(task_id)
}

/// `order_staff_operation`, called from Python with its keywords: for the tests, which compare it
/// with the Python rule on copies of one world.
#[pyfunction]
#[pyo3(signature = (world, *, staff_id, task_kind, target_zone_id, target_id, reason, causation_event_id, source_id))]
#[allow(clippy::too_many_arguments)]
fn order(
    world: &Bound<'_, PyAny>,
    staff_id: &Bound<'_, PyAny>,
    task_kind: &Bound<'_, PyAny>,
    target_zone_id: &Bound<'_, PyAny>,
    target_id: &Bound<'_, PyAny>,
    reason: &Bound<'_, PyAny>,
    causation_event_id: &Bound<'_, PyAny>,
    source_id: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let optional = |value: &Bound<'_, PyAny>| -> PyResult<Option<String>> {
        if value.is_none() {
            Ok(None)
        } else {
            Ok(Some(exact_str(value)?.to_owned()))
        }
    };
    let target_id = optional(target_id)?;
    let source_id = optional(source_id)?;
    order_operation(
        world,
        &Order {
            staff_id: exact_str(staff_id)?,
            task_kind: exact_str(task_kind)?,
            target_zone_id: exact_str(target_zone_id)?,
            target_id: target_id.as_deref(),
            reason: exact_str(reason)?,
            causation_event_id: exact_str(causation_event_id)?,
            source_id: source_id.as_deref(),
        },
    )
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(order, m)?)?;
    Ok(())
}
