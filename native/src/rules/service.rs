//! The staffed water and meal service of `domain/resource_service.py`, for a facility's own queue.
//! Each kernel is one call that plans with nothing changed and then writes; the Python rules stay
//! for the comparison, and for every case below, which is also how a bad value gets its error text
//! (docs/rust-port/lanes/m4-service.md).
//!
//! What goes to Python, before anything is written:
//! - an independent FIFO (`facility_queues_v1`), which no 7-day Run has;
//! - `cancel` and `interrupt_for_staff`, which no 7-day Run reaches;
//! - anything not plainly what the Python rule accepts, such as a reservation ID in use.

use std::sync::{Arc, LazyLock};

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyList, PySlice, PyString, PyTuple};

use crate::core::mathx::py_max;
use crate::core::row::{attribute, exact_dict, exact_float, exact_int, exact_str, map_set, unsupported, Field};
use crate::core::value::V;
use crate::core::world_core::{field_mirror_of, raw_dict_get, NamespaceMirror};
use crate::rules::movement::KEY as MOVEMENT_KEY;
use crate::rules::movement_geometry::Geo;
use crate::rules::queues::head_to_service;
use crate::rules::resources::{apply_planned, Operation};
use crate::rules::social_network::mapping_get;
use crate::rules::staff_dynamics::{is_available, is_available_now, staff_status};
use crate::state::entities::{entities_of, raw_setattr, Activity, EntityStore};
use crate::state::events::{is_stable_id, EventKind, EventRef, EventRow, EventStatus, EventWriter};
use crate::state::metric_state::MetricRow;
use crate::state::resource_ledger::{held_mirror as ledger_mirror, resource_index, LedgerRow};
use crate::state::resource_service::{held_mirror, RequestRecord, ServiceRow, Stall, StallRecord};
use crate::Unsupported;

const QUEUES_KEY: &str = "facility_queues_v1";
const OPERATIONAL_KEY: &str = "operational_events_v1";
const WATER: &str = "water_ml";
const FOOD: &str = "food_meal_units";
/// The tasks `_agent_has_exclusive_task` looks for.
const EXCLUSIVE_TASKS: [&str; 8] = [
    "toilet_queue",
    "toilet_service",
    "resource_service_queue",
    "resource_service_water",
    "resource_service_food",
    "travel_to_toilet",
    "travel_to_resource",
    "travel_return",
];

/// The fixed strings of the events, each shared by all their rows.
struct Names {
    facility_id: Arc<str>,
    agent_id: Arc<str>,
    task_id: Arc<str>,
    resource_id: Arc<str>,
    reservation_id: Arc<str>,
    quantity: Arc<str>,
    staff_id: Arc<str>,
    warehouse_id: Arc<str>,
    source_type: Arc<str>,
    source_id: Arc<str>,
    policy_version: Arc<str>,
    action: Arc<str>,
    service_minutes: Arc<str>,
    completion_type: Arc<str>,
    water: Arc<str>,
    food: Arc<str>,
    service_scheduled: Arc<str>,
}

static NAMES: LazyLock<Names> = LazyLock::new(|| Names {
    facility_id: Arc::from("facility_id"),
    agent_id: Arc::from("agent_id"),
    task_id: Arc::from("task_id"),
    resource_id: Arc::from("resource_id"),
    reservation_id: Arc::from("reservation_id"),
    quantity: Arc::from("quantity"),
    staff_id: Arc::from("staff_id"),
    warehouse_id: Arc::from("warehouse_id"),
    source_type: Arc::from("source_type"),
    source_id: Arc::from("source_id"),
    policy_version: Arc::from("policy_version"),
    action: Arc::from("action"),
    service_minutes: Arc::from("service_minutes"),
    completion_type: Arc::from("resource_service_complete"),
    water: Arc::from(WATER),
    food: Arc::from(FOOD),
    service_scheduled: Arc::from("service_scheduled"),
});

/// `resource_for_facility`.
fn resource_of(kind: &str) -> Option<&'static str> {
    match kind {
        "water_station" => Some(WATER),
        "meal_distribution" => Some(FOOD),
        _ => None,
    }
}

struct Manager<'py> {
    world: Bound<'py, PyAny>,
    config: Bound<'py, PyAny>,
    table: Bound<'py, PyAny>,
}

impl<'py> Manager<'py> {
    /// The manager's world and config, or Unsupported where an independent FIFO may be.
    fn of(manager: &Bound<'py, PyAny>) -> PyResult<Self> {
        let py = manager.py();
        let world = attribute(manager, intern!(py, "world"))?;
        let config = attribute(manager, intern!(py, "config"))?;
        exact_dict(&config)?;
        let table = attribute(&world, intern!(py, "model_state"))?;
        if exact_dict(&table)?.contains(QUEUES_KEY)? {
            return unsupported("an independent FIFO may be here");
        }
        Ok(Self { world, config, table })
    }

    fn text(&self, name: &Bound<'py, PyString>) -> PyResult<Arc<str>> {
        let Some(value) = exact_dict(&self.config)?.get_item(name)? else {
            return unsupported("the config lacks a field");
        };
        Ok(Arc::from(exact_str(&value)?))
    }

    fn integer(&self, name: &Bound<'py, PyString>) -> PyResult<i64> {
        let Some(value) = exact_dict(&self.config)?.get_item(name)? else {
            return unsupported("the config lacks a field");
        };
        exact_int(&value)
    }

    fn float(&self, name: &Bound<'py, PyString>) -> PyResult<f64> {
        let Some(value) = exact_dict(&self.config)?.get_item(name)? else {
            return unsupported("the config lacks a field");
        };
        exact_float(&value)
    }

    /// `config["water_ml_per_service"]` or the meal's.
    fn quantity(&self, resource_id: &str) -> PyResult<i64> {
        let py = self.world.py();
        self.integer(if resource_id == WATER { intern!(py, "water_ml_per_service") } else { intern!(py, "food_meal_units_per_service") })
    }

    fn minute(&self) -> PyResult<i64> {
        exact_int(&attribute(&self.world, intern!(self.world.py(), "minute_index"))?)
    }
}

/// `metric_state[name] = metric_state.get(name, 0.0) + 1.0`.
pub(crate) fn add_one(metrics: &Bound<'_, NamespaceMirror>, name: &str) -> PyResult<()> {
    NamespaceMirror::with_row_tree::<MetricRow, _>(metrics, |row, tree| {
        let count = row.get(name).copied().unwrap_or(0.0) + 1.0;
        map_set(row, tree, name, count)
    })
}

pub(crate) fn metrics_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, NamespaceMirror>> {
    let metrics = field_mirror_of(world, intern!(world.py(), "metric_state"))?;
    NamespaceMirror::with_row::<MetricRow, _>(&metrics, |_| Ok(()))?;
    Ok(metrics)
}

/// `_record_queue_stall`.
fn record_stall(
    world: &Bound<'_, PyAny>,
    mirror: &Bound<'_, NamespaceMirror>,
    metrics: &Bound<'_, NamespaceMirror>,
    minute: i64,
    facility_id: &str,
    agent_id: &Arc<str>,
    reason: &str,
) -> PyResult<()> {
    let py = world.py();
    let stall = Stall { agent_id: agent_id.clone(), reason: Arc::from(reason) };
    let record = StallRecord {
        minute,
        facility_id: Arc::from(facility_id),
        agent_id: agent_id.clone(),
        reason: stall.reason.clone(),
    };
    let changed = NamespaceMirror::with_row::<ServiceRow, _>(mirror, |row| {
        if row.stall_state.get(facility_id).is_some_and(|known| known.agent_id == stall.agent_id && known.reason == stall.reason) {
            return Ok(false);
        }
        row.stall_state.insert(record.facility_id.clone(), stall.clone());
        row.queue_stall_history.push(record.clone());
        Ok(true)
    })?;
    if !changed {
        return Ok(());
    }
    NamespaceMirror::write_through(mirror, &["stall_state"], facility_id, &Field::to_py(&stall, py)?)?;
    NamespaceMirror::append_through(mirror, &["queue_stall_history"], &Field::to_py(&record, py)?)?;
    add_one(metrics, &format!("resource_service_wait_{reason}_count"))
}

/// `_clear_queue_stall`.
fn clear_stall(mirror: &Bound<'_, NamespaceMirror>, facility_id: &str) -> PyResult<()> {
    let held = NamespaceMirror::with_row::<ServiceRow, _>(mirror, |row| Ok(row.stall_state.shift_remove(facility_id).is_some()))?;
    if held {
        NamespaceMirror::remove_through(mirror, &["stall_state"], facility_id)?;
    }
    Ok(())
}

/// The list at `resource_service_v1[name]`: its own methods keep the row.
fn service_list<'py>(table: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    let py = table.py();
    let Some(state) = exact_dict(table)?.get_item(intern!(py, "resource_service_v1"))? else {
        return unsupported("no service state");
    };
    match exact_dict(&state)?.get_item(name)? {
        Some(list) => Ok(list),
        None => unsupported("the service state lacks a list"),
    }
}

/// Whether a pending `resource_reserve` event names this reservation: `_reservation_id_is_in_use`'s
/// scan. Through the queue's tuple, the same objects the Python rule reads.
fn pending_reserves(writer: &EventWriter<'_>, reservation_id: &str) -> PyResult<bool> {
    let py = writer.queue().py();
    let pending = writer.queue().call_method0(intern!(py, "pending"))?;
    let pending = pending.downcast_exact::<PyTuple>().map_err(|_| Unsupported::new_err("the pending events are not a tuple"))?;
    for event in pending.iter() {
        if exact_str(&attribute(&event, intern!(py, "event_type"))?)? == "resource_reserve" {
            let payload = attribute(&event, intern!(py, "payload"))?;
            if let Some(id) = mapping_get(&payload, intern!(py, "reservation_id"))? {
                if id.downcast_exact::<PyString>().is_ok_and(|id| id.to_str().is_ok_and(|id| id == reservation_id)) {
                    return Ok(true);
                }
            } else {
                return unsupported("a pending reservation has no ID");
            }
        }
    }
    Ok(false)
}

/// `water_service_blocked`.
fn water_is_blocked(table: &Bound<'_, PyAny>, facility_id: &str, zone_id: &str) -> PyResult<bool> {
    let py = table.py();
    let Some(state) = exact_dict(table)?.get_item(OPERATIONAL_KEY)? else {
        return Ok(false);
    };
    if state.is_none() {
        return Ok(false);
    }
    let Some(outages) = exact_dict(&state)?.get_item(intern!(py, "water_outages"))? else {
        return unsupported("no water_outages");
    };
    for (_, outage) in exact_dict(&outages)?.iter() {
        let outage = exact_dict(&outage)?;
        let lacks = || Unsupported::new_err("an outage lacks a field");
        let active = outage.get_item(intern!(py, "active"))?.ok_or_else(lacks)?;
        if !active.downcast_exact::<PyBool>().map_err(|_| Unsupported::new_err("active is not a bool"))?.is_true() {
            continue;
        }
        let kind = outage.get_item(intern!(py, "target_kind"))?.ok_or_else(lacks)?;
        let target = outage.get_item(intern!(py, "target_id"))?.ok_or_else(lacks)?;
        let (kind, target) = (exact_str(&kind)?, exact_str(&target)?);
        if (kind == "zone" && target == zone_id) || (kind == "facility" && target == facility_id) {
            return Ok(true);
        }
    }
    Ok(false)
}

/// `physical_service_capacity`, for a facility's own queue.
pub(crate) fn service_capacity(world: &Bound<'_, PyAny>, table: &Bound<'_, PyAny>, facility_id: &str, configured: i64) -> PyResult<i64> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    if !exact_dict(table)?.contains(MOVEMENT_KEY)? || !exact_dict(&configs)?.contains(intern!(py, "facility_geometry"))? {
        return Ok(configured);
    }
    let geo = Geo::of(world)?;
    let placed = geo.layout.as_ref().is_some_and(|layout| layout.facility_points.contains_key(facility_id));
    Ok(if placed { configured.min(1) } else { configured })
}

pub(crate) fn active_task_class(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    static CLASS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    Ok(CLASS.get_or_try_init(py, || -> PyResult<Py<PyAny>> { Ok(py.import("sheltersim.domain.models")?.getattr("ActiveTask")?.unbind()) })?.bind(py))
}

fn receipt_class(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    static CLASS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    Ok(CLASS
        .get_or_try_init(py, || -> PyResult<Py<PyAny>> { Ok(py.import("sheltersim.domain.resource_service")?.getattr("ResourceRequestReceipt")?.unbind()) })?
        .bind(py))
}

/// What `_provenance` and `_audit_event` read of the config.
struct Source {
    source_type: Arc<str>,
    source_id: Arc<str>,
    policy_version: Arc<str>,
}

impl Source {
    fn of(manager: &Manager<'_>) -> PyResult<Self> {
        let py = manager.world.py();
        let source = Self {
            source_type: manager.text(intern!(py, "source_type"))?,
            source_id: manager.text(intern!(py, "source_id"))?,
            policy_version: manager.text(intern!(py, "policy_version"))?,
        };
        if !is_stable_id(&source.source_id) {
            return unsupported("the source is for the Python rule to judge");
        }
        Ok(source)
    }

    fn provenance(&self, action: V) -> IndexMap<Arc<str>, V> {
        let names = &*NAMES;
        let mut provenance = IndexMap::with_capacity(5);
        provenance.insert(names.source_type.clone(), V::Str(self.source_type.clone()));
        provenance.insert(names.source_id.clone(), V::Str(self.source_id.clone()));
        provenance.insert(names.policy_version.clone(), V::Str(self.policy_version.clone()));
        provenance.insert(names.action.clone(), action);
        provenance
    }
}

/// `_audit_event` of the sequence given: the row, or Unsupported when its identity is taken, for
/// the Python rule's search for a free one.
#[allow(clippy::too_many_arguments)]
fn audit_row(
    writer: &EventWriter<'_>,
    source: &Source,
    operation: &'static str,
    sequence: i64,
    minute: i64,
    payload: IndexMap<Arc<str>, V>,
    causation_id: Arc<str>,
) -> PyResult<EventRow> {
    let event_id: Arc<str> = Arc::from(format!("resource-{operation}-{sequence}"));
    {
        let past = writer.log().borrow();
        if past.has_event_id(&event_id) || past.has_sequence(sequence) || writer.queue().borrow().has_event(&event_id) {
            return unsupported("the event's identity is taken");
        }
    }
    let provenance = source.provenance(V::Str(Arc::from(operation)));
    let Some(row) = EventRow::new(
        event_id,
        EventKind::Endogenous,
        minute,
        0,
        sequence,
        Arc::from(format!("resource_{operation}")),
        payload,
        Some(source.source_id.clone()),
        Some(causation_id),
        provenance,
    ) else {
        return unsupported("the event's values are not what an event takes");
    };
    Ok(row)
}

fn operation_payload(warehouse_id: &Arc<str>, resource_id: &Arc<str>, reservation_id: &Arc<str>, agent_id: &Arc<str>, quantity: i64) -> IndexMap<Arc<str>, V> {
    let names = &*NAMES;
    let mut payload = IndexMap::with_capacity(5);
    payload.insert(names.warehouse_id.clone(), V::Str(warehouse_id.clone()));
    payload.insert(names.resource_id.clone(), V::Str(resource_id.clone()));
    payload.insert(names.reservation_id.clone(), V::Str(reservation_id.clone()));
    payload.insert(names.agent_id.clone(), V::Str(agent_id.clone()));
    payload.insert(names.quantity.clone(), V::Int(quantity));
    payload
}

/// A person's activity and target, on the row and the object.
pub(crate) fn set_agent<'py>(
    world: &Bound<'py, PyAny>,
    store: &Bound<'py, EntityStore>,
    agent_id: &Bound<'py, PyAny>,
    activity: Activity,
    target: Option<&Bound<'py, PyAny>>,
) -> PyResult<()> {
    let py = world.py();
    let id = exact_str(agent_id)?;
    let target_text = target.map(exact_str).transpose()?;
    EntityStore::with_rows(store, |rows| {
        let Some(row) = rows.agents.get_mut(id) else {
            return unsupported("no such agent");
        };
        row.activity = activity;
        row.target_id = target_text.map(Arc::from);
        Ok(())
    })?;
    let agents = attribute(world, intern!(py, "agents"))?;
    let Some(agent) = raw_dict_get(&agents, agent_id)? else {
        return Err(pyo3::exceptions::PyRuntimeError::new_err("a row has no object"));
    };
    raw_setattr(&agent, intern!(py, "activity"), activity.member(py)?.as_ptr())?;
    let none = py.None();
    let target = target.map_or(none.as_ptr(), |target| target.as_ptr());
    raw_setattr(&agent, intern!(py, "target_id"), target)
}

// ---- arbitrate ----------------------------------------------------------------------------------

/// The facilities `arbitrate` has anything to do at, in ID order, as `(facility, resource_id)`. An
/// empty queue does nothing in the Python rule.
#[pyfunction]
fn facilities_to_serve<'py>(manager: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
    let py = manager.py();
    let manager = Manager::of(manager)?;
    let mut due: Vec<(Arc<str>, &'static str)> = EntityStore::with_rows(&entities_of(&manager.world)?, |rows| {
        Ok(rows
            .facilities
            .iter()
            .filter(|(_, facility)| !facility.resource_queue_agent_ids.is_empty())
            .filter_map(|(id, facility)| resource_of(&facility.kind).map(|resource| (id.clone(), resource)))
            .collect())
    })?;
    due.sort_unstable_by(|first, second| first.0.as_bytes().cmp(second.0.as_bytes()));
    let result = PyList::empty(py);
    if !due.is_empty() {
        let facilities = attribute(&manager.world, intern!(py, "facilities"))?;
        for (id, resource) in due {
            let Some(facility) = raw_dict_get(&facilities, PyString::new(py, &id).as_any())? else {
                return unsupported("a row has no object");
            };
            result.append((facility, resource))?;
        }
    }
    Ok(result)
}

/// One pass of `arbitrate`'s loop after the positions: the started event's ID, or None to stop.
#[pyfunction]
fn serve_head<'py>(
    manager: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    resource_id: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyString>>> {
    let py = manager.py();
    let manager = Manager::of(manager)?;
    let world = &manager.world;
    let facility_name = exact_str(facility_id)?;
    let resource_name = exact_str(resource_id)?;
    let Some(index) = resource_index(resource_name) else {
        return unsupported("an unknown resource");
    };
    let is_water = resource_name == WATER;
    let minute_value = exact_int(minute)?;
    if minute_value != manager.minute()? {
        return unsupported("another minute");
    }
    let store = entities_of(world)?;
    let mirror = held_mirror(world)?;
    let ledger = ledger_mirror(world)?;
    let metrics = metrics_of(world)?;
    let writer = EventWriter::of(world)?;
    NamespaceMirror::with_row::<ServiceRow, _>(&mirror, |_| Ok(()))?;

    // `_queue_task` and what is read of the facility.
    let (agent_id, task_id, task_start, zone_id, configured, active_count) = EntityStore::with_rows(&store, |rows| {
        let Some(facility) = rows.facilities.get(facility_name) else {
            return unsupported("no such facility");
        };
        let Some(agent_id) = facility.resource_queue_agent_ids.first() else {
            return unsupported("the queue is empty");
        };
        let mut found = None;
        for (_, task) in rows.active_tasks.iter() {
            if &*task.kind == "resource_service_queue"
                && task.agent_id.as_deref() == Some(&**agent_id)
                && task.resource_id.as_deref() == Some(facility_name)
            {
                if found.is_some() {
                    return unsupported("a person with two queue tasks");
                }
                found = Some((task.id.clone(), task.start_minute));
            }
        }
        let Some((task_id, start)) = found else {
            return unsupported("a person with no queue task");
        };
        Ok((
            agent_id.clone(),
            task_id,
            start,
            facility.zone_id.clone(),
            facility.capacity.min(facility.working_units),
            facility.resource_active_agent_ids.len() as i64,
        ))
    })?;
    if !is_stable_id(&task_id) || !is_stable_id(&agent_id) {
        return unsupported("an ID is for the Python rule to judge");
    }
    let (used, warehouse, available) = NamespaceMirror::with_row::<LedgerRow, _>(&ledger, |row| {
        let account = row.accounts.of(index);
        Ok((
            row.used_reservation_ids.contains(&task_id),
            row.warehouse_id.clone(),
            account.remaining.checked_sub(account.reserved).ok_or_else(|| Unsupported::new_err("a balance past 64 bits"))?,
        ))
    })?;
    if used || pending_reserves(&writer, &task_id)? {
        return unsupported("the task's ID is a reservation's");
    }

    let stop = |reason: &str| -> PyResult<Option<Bound<'py, PyString>>> {
        record_stall(world, &mirror, &metrics, minute_value, facility_name, &agent_id, reason)?;
        Ok(None)
    };
    if is_water && water_is_blocked(&manager.table, facility_name, &zone_id)? {
        return stop("water_outage");
    }
    if active_count >= service_capacity(world, &manager.table, facility_name, configured)? {
        return stop("capacity");
    }

    // The first free person in ID order.
    let skill = manager.text(if is_water { intern!(py, "water_staff_skill") } else { intern!(py, "meal_staff_skill") })?;
    let mut candidates: Vec<Arc<str>> = EntityStore::with_rows(&store, |rows| {
        Ok(rows
            .staff
            .iter()
            .filter(|(_, person)| {
                person.zone_id == zone_id && person.skills.0.iter().any(|known| *known == skill) && person.active_task_id.is_none()
            })
            .map(|(_, person)| person.id.clone())
            .collect())
    })?;
    candidates.sort_unstable_by(|first, second| first.as_bytes().cmp(second.as_bytes()));
    let status = staff_status(world)?;
    let mut chosen = None;
    for candidate in candidates {
        let id = PyString::new(py, &candidate);
        if is_available(status.as_ref(), &id)? && is_available_now(world, id.as_any(), minute)? {
            chosen = Some(candidate);
            break;
        }
    }
    let Some(staff_id) = chosen else {
        return stop("staff");
    };
    if !is_stable_id(&staff_id) {
        return unsupported("an ID is for the Python rule to judge");
    }
    let quantity = manager.quantity(resource_name)?;
    if available < quantity {
        return stop("stock");
    }
    if quantity <= 0 {
        return unsupported("a quantity that is not positive");
    }
    let service_minutes = manager.integer(intern!(py, "service_minutes"))?;
    let Some(due) = minute_value.checked_add(service_minutes) else {
        return unsupported("a due minute out of range");
    };
    if due >= writer.duration() {
        return stop("run_end");
    }
    // Writes only what ends this pass.
    if head_to_service(world, facility_id, true, minute_value)? {
        return Ok(None);
    }

    // The start of the service: everything here is checked before the first write.
    let names = &*NAMES;
    let source = Source::of(&manager)?;
    let resource_arc = if is_water { names.water.clone() } else { names.food.clone() };
    let completion_sequence = writer.next_sequence()?;
    let completion_id: Arc<str> = Arc::from(format!("resource-service-complete-{completion_sequence}"));
    {
        let past = writer.log().borrow();
        if past.has_event_id(&completion_id) || past.has_sequence(completion_sequence) || writer.queue().borrow().has_event(&completion_id) {
            return unsupported("the event's identity is taken");
        }
    }
    let facility_arc: Arc<str> = Arc::from(facility_name);
    let mut payload = IndexMap::with_capacity(7);
    payload.insert(names.facility_id.clone(), V::Str(facility_arc.clone()));
    payload.insert(names.agent_id.clone(), V::Str(agent_id.clone()));
    payload.insert(names.task_id.clone(), V::Str(task_id.clone()));
    payload.insert(names.resource_id.clone(), V::Str(resource_arc.clone()));
    payload.insert(names.reservation_id.clone(), V::Str(task_id.clone()));
    payload.insert(names.quantity.clone(), V::Int(quantity));
    payload.insert(names.staff_id.clone(), V::Str(staff_id.clone()));
    let mut provenance = source.provenance(V::Str(names.service_scheduled.clone()));
    provenance.insert(names.service_minutes.clone(), V::Int(service_minutes));
    let Some(completion) = EventRow::new(
        completion_id.clone(),
        EventKind::Completion,
        due,
        0,
        completion_sequence,
        names.completion_type.clone(),
        payload,
        Some(task_id.clone()),
        Some(task_id.clone()),
        provenance,
    ) else {
        return unsupported("the event's values are not what an event takes");
    };
    let audit = audit_row(
        &writer,
        &source,
        "reserve",
        completion_sequence + 1,
        minute_value,
        operation_payload(&warehouse, &resource_arc, &task_id, &agent_id, quantity),
        task_id.clone(),
    )?;
    let reservation_ids = service_list(&manager.table, intern!(py, "active_reservation_ids"))?;
    let facilities = attribute(world, intern!(py, "facilities"))?;
    let Some(facility_object) = raw_dict_get(&facilities, facility_id)? else {
        return unsupported("a row has no object");
    };
    let staff_map = attribute(world, intern!(py, "staff"))?;
    let staff_key = PyString::new(py, &staff_id);
    let Some(staff_object) = raw_dict_get(&staff_map, staff_key.as_any())? else {
        return unsupported("a row has no object");
    };
    let wait = minute_value.checked_sub(task_start).ok_or_else(|| Unsupported::new_err("a start out of range"))?;
    if wait.abs() >= 1 << 53 {
        return unsupported("a wait too long for a float");
    }

    // The ledger says last whether it takes it. It changes nothing when it doesn't.
    apply_planned(
        world,
        &ledger,
        &Operation {
            operation: "reserve",
            warehouse_id: &warehouse,
            resource_id: resource_name,
            index,
            quantity,
            reservation_id: Some(&task_id),
            agent_id: Some(&agent_id),
            source_id: &source.source_id,
            event_id: &audit.event_id,
        },
    )?;

    // Write.
    writer.allocate_sequence()?;
    writer.allocate_sequence()?;
    writer.record(&audit, minute_value, EventStatus::Applied, None)?;
    let task_text = PyString::new(py, &task_id);
    reservation_ids.call_method1(intern!(py, "append"), (&task_text,))?;
    reservation_ids.call_method0(intern!(py, "sort"))?;
    clear_stall(&mirror, facility_name)?;
    let agent_text = PyString::new(py, &agent_id);
    attribute(&facility_object, intern!(py, "resource_queue_agent_ids"))?.call_method1(intern!(py, "pop"), (0,))?;
    attribute(&facility_object, intern!(py, "resource_active_agent_ids"))?.call_method1(intern!(py, "append"), (&agent_text,))?;
    let kind = if is_water { "resource_service_water" } else { "resource_service_food" };
    let task = active_task_class(py)?.call1((&task_text, kind, &agent_text, facility_id, minute_value, due, &staff_key))?;
    attribute(world, intern!(py, "active_tasks"))?.set_item(&task_text, task)?;
    EntityStore::with_rows(&store, |rows| {
        let Some(row) = rows.staff.get_mut(&staff_id) else {
            return unsupported("no such staff");
        };
        row.active_task_id = Some(task_id.clone());
        row.busy_until = due;
        Ok(())
    })?;
    raw_setattr(&staff_object, intern!(py, "active_task_id"), task_text.as_ptr())?;
    raw_setattr(&staff_object, intern!(py, "busy_until"), due.into_pyobject(py)?.as_ptr())?;
    attribute(&staff_object, intern!(py, "work_queue"))?.set_item(PySlice::full(py), PyList::new(py, [&task_text])?)?;
    set_agent(world, &store, agent_text.as_any(), Activity::InService, Some(facility_id))?;
    writer.schedule(completion)?;
    // `_record_queue_wait`.
    let key = if is_water { "water_service_wait" } else { "meal_service_wait" };
    NamespaceMirror::with_row_tree::<MetricRow, _>(&metrics, |row, tree| {
        let (count, total, maximum) = (format!("{key}_count"), format!("{key}_total_minutes"), format!("{key}_max_minutes"));
        let value = row.get(&*count).copied().unwrap_or(0.0) + 1.0;
        map_set(row, tree, &count, value)?;
        let value = row.get(&*total).copied().unwrap_or(0.0) + wait as f64;
        map_set(row, tree, &total, value)?;
        let value = py_max(row.get(&*maximum).copied().unwrap_or(0.0), wait as f64);
        map_set(row, tree, &maximum, value)
    })?;
    Ok(Some(PyString::new(py, &completion_id)))
}

// ---- complete -----------------------------------------------------------------------------------

/// `ResourceServiceManager.complete`.
#[pyfunction]
fn complete<'py>(manager: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    complete_event(manager, EventRef::Object(event))
}

/// `complete` of an event's row or object.
pub(crate) fn complete_event<'py>(manager: &Bound<'py, PyAny>, event: EventRef<'_, 'py>) -> PyResult<()> {
    let py = manager.py();
    let manager = Manager::of(manager)?;
    let world = &manager.world;
    // `_assert_service_event_matches_task`. Anything it would refuse goes to it.
    if event.payload_len()? != 7 {
        return unsupported("the payload has other fields");
    }
    let lacks = || Unsupported::new_err("the payload lacks a field");
    let facility_arc = event.text("facility_id")?.ok_or_else(lacks)?;
    let agent_arc = event.text("agent_id")?.ok_or_else(lacks)?;
    let task_arc = event.text("task_id")?.ok_or_else(lacks)?;
    let resource_given = event.text("resource_id")?.ok_or_else(lacks)?;
    let reservation = event.text("reservation_id")?.ok_or_else(lacks)?;
    let staff_arc = event.text("staff_id")?.ok_or_else(lacks)?;
    let quantity = event.int("quantity")?.ok_or_else(lacks)?;
    let (facility_id, agent_id, task_id, resource_id, staff_id) = (&*facility_arc, &*agent_arc, &*task_arc, &*resource_given, &*staff_arc);
    if !event.is_completion()? || &*event.event_type()? != "resource_service_complete" || *reservation != *task_id {
        return unsupported("the event does not match");
    }
    let Some(index) = resource_index(resource_id) else {
        return unsupported("an unknown resource");
    };
    let is_water = resource_id == WATER;
    if !is_stable_id(task_id) || !is_stable_id(agent_id) {
        return unsupported("an ID is for the Python rule to judge");
    }
    let due = event.due_minute()?;
    let event_id = event.event_id()?;
    if !is_stable_id(&event_id) {
        return unsupported("an ID is for the Python rule to judge");
    }
    if quantity != manager.quantity(resource_id)? || quantity <= 0 {
        return unsupported("not the service's quantity");
    }
    let relief = manager.float(if is_water { intern!(py, "hydration_relief") } else { intern!(py, "hunger_relief") })?;
    let store = entities_of(world)?;
    let mirror = held_mirror(world)?;
    let ledger = ledger_mirror(world)?;
    let metrics = metrics_of(world)?;
    let writer = EventWriter::of(world)?;
    let minute = manager.minute()?;
    let need = EntityStore::with_rows(&store, |rows| {
        let Some(task) = rows.active_tasks.get(task_id) else {
            return unsupported("no active task");
        };
        let is_kind = matches!(&*task.kind, "resource_service_water" | "resource_service_food");
        if !is_kind
            || task.due_minute != due
            || task.agent_id.as_deref() != Some(agent_id)
            || task.staff_id.as_deref() != Some(staff_id)
            || task.resource_id.as_deref() != Some(facility_id)
            || &*task.id != task_id
        {
            return unsupported("the task does not match");
        }
        let Some(facility) = rows.facilities.get(facility_id) else {
            return unsupported("no such facility");
        };
        let Some(staff) = rows.staff.get(staff_id) else {
            return unsupported("no such staff");
        };
        let Some(agent) = rows.agents.get(agent_id) else {
            return unsupported("no such agent");
        };
        if resource_of(&facility.kind) != Some(if is_water { WATER } else { FOOD })
            || facility.resource_active_agent_ids.iter().filter(|id| &***id == agent_id).count() == 0
            || staff.active_task_id.as_deref() != Some(task_id)
            || staff.busy_until != due
        {
            return unsupported("the service's state does not match");
        }
        let need = if is_water { agent.needs.hydration_need } else { agent.needs.hunger };
        if !need.is_finite() || !relief.is_finite() {
            return unsupported("a need is not finite");
        }
        Ok(need)
    })?;
    let (matches, warehouse) = NamespaceMirror::with_row::<LedgerRow, _>(&ledger, |row| {
        let matches = row.accounts.of(index).reservations.get(task_id).is_some_and(|found| {
            &*found.reservation_id == task_id
                && &*found.resource_id == resource_id
                && &*found.agent_id == agent_id
                && found.quantity == quantity
        });
        Ok((matches, row.warehouse_id.clone()))
    })?;
    let owned = NamespaceMirror::with_row::<ServiceRow, _>(&mirror, |row| Ok(row.active_reservation_ids.iter().any(|id| &**id == task_id)))?;
    if !matches || !owned {
        return unsupported("the reservation does not match");
    }

    let source = Source::of(&manager)?;
    let resource_arc = if is_water { NAMES.water.clone() } else { NAMES.food.clone() };
    let audit = audit_row(
        &writer,
        &source,
        "consume",
        writer.next_sequence()?,
        minute,
        operation_payload(&warehouse, &resource_arc, &task_arc, &agent_arc, quantity),
        event_id,
    )?;
    let reservation_ids = service_list(&manager.table, intern!(py, "active_reservation_ids"))?;
    let facilities = attribute(world, intern!(py, "facilities"))?;
    let Some(facility) = raw_dict_get(&facilities, PyString::new(py, facility_id).as_any())? else {
        return unsupported("a row has no object");
    };
    let staff_map = attribute(world, intern!(py, "staff"))?;
    let Some(staff) = raw_dict_get(&staff_map, PyString::new(py, staff_id).as_any())? else {
        return unsupported("a row has no object");
    };
    let agents = attribute(world, intern!(py, "agents"))?;
    let agent_object = PyString::new(py, agent_id);
    let Some(agent) = raw_dict_get(&agents, agent_object.as_any())? else {
        return unsupported("a row has no object");
    };
    let task_object = PyString::new(py, task_id);
    let active_tasks = attribute(world, intern!(py, "active_tasks"))?;
    let needs = attribute(&agent, intern!(py, "needs"))?;

    apply_planned(
        world,
        &ledger,
        &Operation {
            operation: "consume",
            warehouse_id: &warehouse,
            resource_id,
            index,
            quantity,
            reservation_id: Some(task_id),
            agent_id: Some(agent_id),
            source_id: &source.source_id,
            event_id: &audit.event_id,
        },
    )?;

    // Write.
    writer.allocate_sequence()?;
    writer.record(&audit, minute, EventStatus::Applied, None)?;
    reservation_ids.call_method1(intern!(py, "remove"), (&task_object,))?;
    let relieved = py_max(0.0, need - relief);
    EntityStore::with_rows(&store, |rows| {
        let Some(row) = rows.agents.get_mut(agent_id) else {
            return unsupported("no such agent");
        };
        if is_water {
            row.needs.hydration_need = relieved;
        } else {
            row.needs.hunger = relieved;
        }
        Ok(())
    })?;
    let need_name = if is_water { intern!(py, "hydration_need") } else { intern!(py, "hunger") };
    raw_setattr(&needs, need_name, relieved.into_pyobject(py)?.as_ptr())?;
    add_one(&metrics, if is_water { "water_service_completed_count" } else { "meal_service_completed_count" })?;
    attribute(&facility, intern!(py, "resource_active_agent_ids"))?.call_method1(intern!(py, "remove"), (&agent_object,))?;
    // `_release_staff`.
    attribute(&staff, intern!(py, "work_queue"))?.call_method0(intern!(py, "clear"))?;
    EntityStore::with_rows(&store, |rows| {
        let Some(row) = rows.staff.get_mut(staff_id) else {
            return unsupported("no such staff");
        };
        row.active_task_id = None;
        row.busy_until = 0;
        Ok(())
    })?;
    raw_setattr(&staff, intern!(py, "active_task_id"), py.None().as_ptr())?;
    raw_setattr(&staff, intern!(py, "busy_until"), 0_i64.into_pyobject(py)?.as_ptr())?;
    active_tasks.del_item(&task_object)?;
    set_agent(world, &store, agent_object.as_any(), Activity::Idle, None)?;
    add_one(&metrics, "resource_service_completed_count")
}

// ---- request ------------------------------------------------------------------------------------

/// `record_request_outcome`, on the row and the tree.
pub(crate) fn append_outcome(
    py: Python<'_>,
    mirror: &Bound<'_, NamespaceMirror>,
    record: RequestRecord,
) -> PyResult<()> {
    let object = Field::to_py(&record, py)?;
    NamespaceMirror::with_row::<ServiceRow, _>(mirror, |row| {
        row.request_history.push(record);
        Ok(())
    })?;
    NamespaceMirror::append_through(mirror, &["request_history"], &object)
}

/// `ResourceServiceManager.record_request_outcome`.
#[pyfunction]
#[pyo3(signature = (manager, *, agent_id, action, outcome, reason, facility_id, origin_zone_id, detail=None))]
#[allow(clippy::too_many_arguments)]
fn record_request_outcome<'py>(
    manager: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    action: &Bound<'py, PyAny>,
    outcome: &Bound<'py, PyAny>,
    reason: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    origin_zone_id: &Bound<'py, PyAny>,
    detail: Option<&Bound<'py, PyAny>>,
) -> PyResult<()> {
    let py = manager.py();
    let world = attribute(manager, intern!(py, "world"))?;
    let mirror = held_mirror(&world)?;
    let optional = |value: Option<&Bound<'py, PyAny>>| -> PyResult<Option<Arc<str>>> {
        match value {
            Some(value) if !value.is_none() => Ok(Some(Arc::from(exact_str(value)?))),
            _ => Ok(None),
        }
    };
    let record = RequestRecord {
        minute: exact_int(&attribute(&world, intern!(py, "minute_index"))?)?,
        agent_id: Arc::from(exact_str(agent_id)?),
        action: Arc::from(exact_str(action)?),
        outcome: Arc::from(exact_str(outcome)?),
        reason: Arc::from(exact_str(reason)?),
        facility_id: optional(Some(facility_id))?,
        origin_zone_id: Arc::from(exact_str(origin_zone_id)?),
        detail: optional(detail)?,
    };
    append_outcome(py, &mirror, record)
}

/// `ResourceServiceManager.request`, without the world's whole validation.
pub(crate) fn queue_up<'py>(
    manager: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = manager.py();
    request(manager, agent_id, facility_id, None, Some(PyBool::new(py, false).as_any()), None)
}

/// `ResourceServiceManager.request`, without the world's whole validation.
#[pyfunction]
#[pyo3(signature = (manager, agent_id, facility_id, *, minute=None, validate_state=None, queue_id=None))]
fn request<'py>(
    manager: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    minute: Option<&Bound<'py, PyAny>>,
    validate_state: Option<&Bound<'py, PyAny>>,
    queue_id: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = manager.py();
    if !validate_state.is_some_and(|validate| validate.downcast_exact::<PyBool>().is_ok_and(|validate| !validate.is_true())) {
        return unsupported("the world's whole validation is Python's");
    }
    if queue_id.is_some_and(|queue| !queue.is_none()) {
        return unsupported("an independent FIFO");
    }
    let manager = Manager::of(manager)?;
    let world = &manager.world;
    let world_minute = manager.minute()?;
    if let Some(minute) = minute {
        if !minute.is_none() && exact_int(minute)? != world_minute {
            return unsupported("another minute");
        }
    }
    let (agent_name, facility_name) = (exact_str(agent_id)?, exact_str(facility_id)?);
    let store = entities_of(world)?;
    let mirror = held_mirror(world)?;
    let writer = EventWriter::of(world)?;
    let ledger = ledger_mirror(world)?;
    NamespaceMirror::with_row::<ServiceRow, _>(&mirror, |_| Ok(()))?;
    let counters = attribute(world, intern!(py, "id_counters"))?;
    let counters = exact_dict(&counters)?;
    let mut counter = match counters.get_item(intern!(py, "resource_task"))? {
        Some(counter) => exact_int(&counter)?,
        None => 0,
    };
    let (zone, resource, queue_position) = EntityStore::with_rows(&store, |rows| {
        let (Some(agent), Some(facility)) = (rows.agents.get(agent_name), rows.facilities.get(facility_name)) else {
            return unsupported("no such agent or facility");
        };
        let Some(resource) = resource_of(&facility.kind) else {
            return unsupported("not a resource service facility");
        };
        if agent.zone_id != facility.zone_id || !matches!(agent.activity, Activity::Idle | Activity::Resting | Activity::Sleeping) {
            return unsupported("the person cannot queue now");
        }
        if rows.active_tasks.iter().any(|(_, task)| task.agent_id.as_deref() == Some(agent_name) && EXCLUSIVE_TASKS.contains(&&*task.kind)) {
            return unsupported("the person has an exclusive task");
        }
        for (_, other) in rows.facilities.iter() {
            let lists = [&other.queue_agent_ids, &other.active_agent_ids, &other.resource_queue_agent_ids, &other.resource_active_agent_ids];
            if lists.iter().any(|list| list.iter().any(|id| &**id == agent_name)) {
                return unsupported("the person is at a facility");
            }
        }
        Ok((agent.zone_id.clone(), resource, facility.resource_queue_agent_ids.len() as i64 + 1))
    })?;
    // `_next_queue_task_id`.
    let task_id: Arc<str> = loop {
        let candidate = format!("resource-service-task-{counter}");
        counter = counter.checked_add(1).ok_or_else(|| Unsupported::new_err("a counter past 64 bits"))?;
        let is_task = EntityStore::with_rows(&store, |rows| Ok(rows.active_tasks.get(&candidate).is_some()))?;
        let is_used = NamespaceMirror::with_row::<LedgerRow, _>(&ledger, |row| Ok(row.used_reservation_ids.contains(&candidate)))?;
        if !is_task && !is_used && !pending_reserves(&writer, &candidate)? {
            break Arc::from(candidate);
        }
    };
    let facilities = attribute(world, intern!(py, "facilities"))?;
    let Some(facility) = raw_dict_get(&facilities, facility_id)? else {
        return unsupported("a row has no object");
    };
    let queue = attribute(&facility, intern!(py, "resource_queue_agent_ids"))?;
    let active_tasks = attribute(world, intern!(py, "active_tasks"))?;
    let action = if resource == WATER { "fetch_water" } else { "collect_meal" };

    // Write.
    counters.set_item(intern!(py, "resource_task"), counter)?;
    queue.call_method1(intern!(py, "append"), (agent_id,))?;
    let task_text = PyString::new(py, &task_id);
    let task = active_task_class(py)?.call1((&task_text, "resource_service_queue", agent_id, facility_id, world_minute, world_minute))?;
    active_tasks.set_item(&task_text, task)?;
    set_agent(world, &store, agent_id, Activity::Queued, Some(facility_id))?;
    append_outcome(
        py,
        &mirror,
        RequestRecord {
            minute: world_minute,
            agent_id: Arc::from(agent_name),
            action: Arc::from(action),
            outcome: Arc::from("queued"),
            reason: Arc::from("arrived_at_facility"),
            facility_id: Some(Arc::from(facility_name)),
            origin_zone_id: zone,
            detail: None,
        },
    )?;
    receipt_class(py)?.call1((agent_id, facility_id, resource, world_minute, &task_text, queue_position))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(facilities_to_serve, m)?)?;
    m.add_function(wrap_pyfunction!(serve_head, m)?)?;
    m.add_function(wrap_pyfunction!(complete, m)?)?;
    m.add_function(wrap_pyfunction!(record_request_outcome, m)?)?;
    m.add_function(wrap_pyfunction!(request, m)?)?;
    Ok(())
}

