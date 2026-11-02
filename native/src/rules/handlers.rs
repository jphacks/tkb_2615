//! The rest of `events/handlers.py`: proximity contacts, the resource ledger's events, facility
//! outages and recoveries, the operational events (deliveries, water, staff) and infrastructure.
//! These events are rare, and each kernel is the Python rule's steps in order, on the same Python
//! containers: `model_state` writes are single items, which reach Rust's rows as forwarded writes.
//!
//! Anything but a success says Unsupported with the world as it was: a suppressed event, a failed one,
//! a value the rule isn't sure of, and any exception before the first change. The Python rule then runs
//! from the start and says it in its own words. So the error texts stay Python's until step 4
//! (docs/rust-port/lanes/m6-handlers-rest.md).
//!
//! A handler whose Python rule copies the world does the same here (`transaction`): that flow stays
//! until `core.fork()` (docs/rust-port/step3-design.md).

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Mutex;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyList, PyMapping, PyString, PyTuple};

use crate::core::mathx::{hypot, py_is_close};
use crate::core::row::{attribute, exact_int, exact_str, unsupported};
use crate::rules::care_receipts::{continuity_record, facility_recovery_record};
use crate::rules::effects::{active_outage, copy_of, has_active_modifier, refused_by_python, swap};
use crate::rules::staff_operation_events::interrupt_operation;
use crate::Unsupported;

const OPERATIONAL: &str = "operational_events_v1";
const INFRASTRUCTURE: &str = "infrastructure_v1";
const MOVEMENT: &str = "movement_v1";
const PROXIMITY_SOURCE: &str = "ASM-PROXIMITY-CONTACT-V1";
const OPERATIONAL_SOURCE: &str = "ASM-H03-OPERATIONAL-EVENTS-V1";
const OPERATIONAL_POLICY: &str = "operational-events-v1";
const INFRASTRUCTURE_POLICY: &str = "infrastructure-events-v1";
const SUPPORT_SOURCE: &str = "ASM-STAFF-SUPPORT-LIFECYCLE-V1";
const SHARED_CONFIG: &str = "shared_operations";

// ---- What ran and what was refused --------------------------------------------------------------

static RAN: Mutex<BTreeMap<&'static str, u64>> = Mutex::new(BTreeMap::new());
static REFUSALS: Mutex<BTreeMap<String, u64>> = Mutex::new(BTreeMap::new());

/// Count a kernel's result under its label.
pub(crate) fn counted<T>(py: Python<'_>, label: &'static str, result: PyResult<T>) -> PyResult<T> {
    match &result {
        Ok(_) => {
            if let Ok(mut ran) = RAN.lock() {
                *ran.entry(label).or_insert(0) += 1;
            }
        }
        Err(error) if error.is_instance_of::<Unsupported>(py) => {
            if let Ok(mut refusals) = REFUSALS.lock() {
                *refusals.entry(format!("{label}: {}", error.value(py))).or_insert(0) += 1;
            }
        }
        Err(_) => {}
    }
    result
}

/// What has run in this process: `(counts, refusals by reason)`.
#[pyfunction]
fn stats<'py>(py: Python<'py>) -> PyResult<(Bound<'py, PyDict>, Bound<'py, PyDict>)> {
    let counts = PyDict::new(py);
    if let Ok(ran) = RAN.lock() {
        for (label, times) in ran.iter() {
            counts.set_item(*label, *times)?;
        }
    }
    let reasons = PyDict::new(py);
    if let Ok(refusals) = REFUSALS.lock() {
        for (reason, times) in refusals.iter() {
            reasons.set_item(reason, *times)?;
        }
    }
    Ok((counts, reasons))
}

// ---- Reading and making Python's values ----------------------------------------------------------

/// `map.get(key)`, which is None when absent.
pub(crate) fn get<'py>(map: &Bound<'py, PyAny>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    map.call_method1(intern!(map.py(), "get"), (key,))
}

/// `value == wanted`, of an exact str. Any other value is not the text.
pub(crate) fn text_is(value: &Bound<'_, PyAny>, wanted: &str) -> bool {
    exact_str(value).is_ok_and(|text| text == wanted)
}

pub(crate) fn get_is(map: &Bound<'_, PyAny>, key: &str, wanted: &str) -> PyResult<bool> {
    Ok(text_is(&get(map, key)?, wanted))
}

/// `set(map) == set(names)`, of distinct names.
pub(crate) fn fields_are(map: &Bound<'_, PyAny>, names: &[&str]) -> PyResult<bool> {
    if map.len()? != names.len() {
        return Ok(false);
    }
    for name in names {
        if !map.contains(*name)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// An exact float, or an exact int that fits one: the same f64 in an expression with a float.
pub(crate) fn number(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    if let Ok(float) = value.downcast_exact::<PyFloat>() {
        return Ok(float.value());
    }
    let int = exact_int(value)?;
    if !(-(1 << 53)..=1 << 53).contains(&int) {
        return unsupported("a number does not fit a float");
    }
    Ok(int as f64)
}

/// A value that is an exact str, or None for None: `text or None` is the caller's.
pub(crate) fn optional_text<'a>(value: &'a Bound<'_, PyAny>) -> PyResult<Option<&'a str>> {
    if value.is_none() {
        return Ok(None);
    }
    exact_str(value).map(Some)
}

pub(crate) fn is_mapping(value: &Bound<'_, PyAny>) -> bool {
    value.downcast::<PyMapping>().is_ok()
}

fn models(py: Python<'_>) -> PyResult<Bound<'_, PyModule>> {
    py.import(intern!(py, "sheltersim.events.models"))
}

pub(crate) fn kind_is(event: &Bound<'_, PyAny>, member: &str) -> PyResult<bool> {
    let py = event.py();
    Ok(attribute(event, intern!(py, "kind"))?.is(&models(py)?.getattr(intern!(py, "EventKind"))?.getattr(member)?))
}

pub(crate) fn status_member<'py>(py: Python<'py>, member: &str) -> PyResult<Bound<'py, PyAny>> {
    models(py)?.getattr(intern!(py, "EventStatus"))?.getattr(member)
}

/// The arguments of a new `ScheduledEvent`.
pub(crate) struct NewEvent<'py, 'a> {
    pub(crate) event_id: &'a str,
    pub(crate) kind: &'a str,
    pub(crate) due_minute: i64,
    pub(crate) priority: i64,
    pub(crate) sequence: i64,
    pub(crate) event_type: &'a str,
    pub(crate) payload: Bound<'py, PyAny>,
    pub(crate) source_id: Option<Bound<'py, PyAny>>,
    pub(crate) causation_id: Option<Bound<'py, PyAny>>,
    pub(crate) provenance: Bound<'py, PyAny>,
}

pub(crate) fn new_event<'py>(py: Python<'py>, event: NewEvent<'py, '_>) -> PyResult<Bound<'py, PyAny>> {
    let module = models(py)?;
    let kwargs = PyDict::new(py);
    kwargs.set_item(intern!(py, "event_id"), event.event_id)?;
    kwargs.set_item(intern!(py, "kind"), module.getattr(intern!(py, "EventKind"))?.getattr(event.kind)?)?;
    kwargs.set_item(intern!(py, "due_minute"), event.due_minute)?;
    kwargs.set_item(intern!(py, "priority"), event.priority)?;
    kwargs.set_item(intern!(py, "sequence"), event.sequence)?;
    kwargs.set_item(intern!(py, "event_type"), event.event_type)?;
    kwargs.set_item(intern!(py, "payload"), event.payload)?;
    kwargs.set_item(intern!(py, "source_id"), event.source_id)?;
    kwargs.set_item(intern!(py, "causation_id"), event.causation_id)?;
    kwargs.set_item(intern!(py, "provenance"), event.provenance)?;
    module.getattr(intern!(py, "ScheduledEvent"))?.call((), Some(&kwargs))
}

pub(crate) fn log_record<'py>(
    py: Python<'py>,
    event: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    status: &str,
    detail: Option<&str>,
) -> PyResult<Bound<'py, PyAny>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item(intern!(py, "event"), event)?;
    kwargs.set_item(intern!(py, "realized_minute"), minute)?;
    kwargs.set_item(intern!(py, "status"), status_member(py, status)?)?;
    kwargs.set_item(intern!(py, "detail"), detail)?;
    models(py)?.getattr(intern!(py, "EventLogRecord"))?.call((), Some(&kwargs))
}

/// `{"source_type": "assumption", "source_id": source, "policy_version": policy}`.
pub(crate) fn assumption<'py>(py: Python<'py>, source: &Bound<'py, PyAny>, policy: &str) -> PyResult<Bound<'py, PyDict>> {
    let provenance = PyDict::new(py);
    provenance.set_item(intern!(py, "source_type"), "assumption")?;
    provenance.set_item(intern!(py, "source_id"), source)?;
    provenance.set_item(intern!(py, "policy_version"), policy)?;
    Ok(provenance)
}

/// `_ensure_event_identity_free`; a taken identity is the Python rule's to report.
pub(crate) fn ensure_identity_free(world: &Bound<'_, PyAny>, event_id: &str, sequence: i64) -> PyResult<()> {
    let py = world.py();
    for current in attribute(world, intern!(py, "event_queue"))?.call_method0(intern!(py, "pending"))?.try_iter()? {
        let current = current?;
        if text_is(&attribute(&current, intern!(py, "event_id"))?, event_id) || exact_int(&attribute(&current, intern!(py, "sequence"))?)? == sequence {
            return unsupported("a generated event's identity is taken");
        }
    }
    let log = attribute(world, intern!(py, "event_log"))?;
    if log.call_method1(intern!(py, "contains_id"), (event_id,))?.is_truthy()?
        || log.call_method1(intern!(py, "contains_sequence"), (sequence,))?.is_truthy()?
    {
        return unsupported("a generated event's identity is taken");
    }
    Ok(())
}

pub(crate) fn pending_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py = world.py();
    attribute(world, intern!(py, "event_queue"))?
        .call_method0(intern!(py, "pending"))?
        .try_iter()?
        .collect()
}

fn minute_of(world: &Bound<'_, PyAny>) -> PyResult<i64> {
    exact_int(&attribute(world, intern!(world.py(), "minute_index"))?)
}

fn duration_of(world: &Bound<'_, PyAny>) -> PyResult<i64> {
    exact_int(&attribute(world, intern!(world.py(), "duration_minutes"))?)
}

fn table_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    attribute(world, intern!(world.py(), "model_state"))
}

/// `event.source_id is None` is a failed event in every operational rule.
fn require_source(event: &Bound<'_, PyAny>) -> PyResult<()> {
    if attribute(event, intern!(event.py(), "source_id"))?.is_none() {
        return unsupported("an event without a source");
    }
    Ok(())
}

/// `set_event(metric, get + 1.0)`, of `world.metric_state`.
pub(crate) fn add_metric(world: &Bound<'_, PyAny>, name: &str) -> PyResult<()> {
    let metrics = attribute(world, intern!(world.py(), "metric_state"))?;
    let value = number(&metrics.call_method1(intern!(world.py(), "get"), (name, 0.0))?)?;
    metrics.set_item(name, value + 1.0)
}

// ---- The transaction ------------------------------------------------------------------------------

/// `_apply_operational_transaction`. `operation` works on the copy and gives the follow-up records.
fn transaction<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    operation: impl FnOnce(&Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py = world.py();
    let candidate = copy_of(world)?;
    let records = operation(&candidate)?;
    if records.is_empty() {
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "_in_flight_event"), event)?;
        candidate.call_method(intern!(py, "validate"), (), Some(&kwargs))?;
    } else {
        attribute(&candidate, intern!(py, "event_queue"))?
            .call_method1(intern!(py, "remove"), (attribute(event, intern!(py, "event_id"))?,))?;
        let log = attribute(&candidate, intern!(py, "event_log"))?;
        log.call_method1(
            intern!(py, "append"),
            (log_record(py, event, &attribute(&candidate, intern!(py, "minute_index"))?, "APPLIED", None)?,),
        )?;
        for record in &records {
            log.call_method1(intern!(py, "append"), (record,))?;
        }
        if attribute(&candidate, intern!(py, "model_configs"))?.contains(SHARED_CONFIG)? {
            for record in &records {
                let recorded = attribute(record, intern!(py, "event"))?;
                if text_is(&attribute(&recorded, intern!(py, "event_type"))?, "staff_support_continuity") {
                    let contract = py.import("sheltersim.contracts.operations")?.getattr("StaffSupportContinuityReceipt")?;
                    let adapter = py.import("pydantic")?.getattr("TypeAdapter")?.call1((contract,))?;
                    let receipt = adapter.call_method1("validate_python", (attribute(&recorded, intern!(py, "payload"))?,))?;
                    py.import("sheltersim.domain.common_operations")?
                        .getattr("record_shared_operation")?
                        .call1((&candidate, receipt))?;
                }
            }
        }
        candidate.call_method0(intern!(py, "validate"))?;
    }
    swap(world, &candidate)?;
    Ok(records)
}

/// A handler's result: `tuple(records)`, or None for none.
fn records_tuple<'py>(py: Python<'py>, records: Vec<Bound<'py, PyAny>>) -> PyResult<Option<Bound<'py, PyTuple>>> {
    if records.is_empty() {
        return Ok(None);
    }
    Ok(Some(PyTuple::new(py, records)?))
}

// ---- ProximityContactHandler -----------------------------------------------------------------------

fn proximity_on(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let config = py
        .import("sheltersim.domain.social_network")?
        .getattr("canonical_proximity_contact_config")?
        .call1((get(&configs, "social_proximity")?,))?;
    if config.is_none() {
        return unsupported("no proximity model");
    }
    let provenance = attribute(event, intern!(py, "provenance"))?;
    let due = exact_int(&attribute(event, intern!(py, "due_minute"))?)?;
    if !kind_is(event, "ENDOGENOUS")?
        || due != minute_of(world)?
        || !text_is(&attribute(event, intern!(py, "source_id"))?, PROXIMITY_SOURCE)
        || !fields_are(&provenance, &["source_type", "source_id", "policy_version", "observed_minute"])?
        || !get_is(&provenance, "source_type", "assumption")?
        || !get_is(&provenance, "source_id", PROXIMITY_SOURCE)?
        || !get(&provenance, "policy_version")?.eq(config.get_item("policy_version")?)?
    {
        return unsupported("the provenance or time is for the Python rule to judge");
    }
    let payload = attribute(event, intern!(py, "payload"))?;
    if !fields_are(
        &payload,
        &["first_agent_id", "second_agent_id", "zone_id", "observed_minute", "contact_minutes", "distance_m", "first_point", "second_point"],
    )? {
        return unsupported("the payload has other fields");
    }
    let first_value = payload.get_item("first_agent_id")?;
    let second_value = payload.get_item("second_agent_id")?;
    let zone_value = payload.get_item("zone_id")?;
    let first_id = exact_str(&first_value)?;
    let second_id = exact_str(&second_value)?;
    let agents = attribute(world, intern!(py, "agents"))?;
    let zones = attribute(world, intern!(py, "zones"))?;
    exact_str(&zone_value)?;
    let distance = number(&payload.get_item("distance_m")?)?;
    if first_id.is_empty()
        || second_id.is_empty()
        || first_id == second_id
        || !agents.contains(first_id)?
        || !agents.contains(second_id)?
        || !zones.contains(&zone_value)?
        || exact_int(&payload.get_item("observed_minute")?)? != due
        || exact_int(&get(&provenance, "observed_minute")?)? != due
        || exact_int(&payload.get_item("contact_minutes")?)? != 0
        || !distance.is_finite()
        || distance < 0.0
        || distance > number(&config.get_item("max_distance_m")?)?
    {
        return unsupported("the participants or measurement are for the Python rule to judge");
    }
    let first_point = payload.get_item("first_point")?;
    let second_point = payload.get_item("second_point")?;
    let mut coordinates = [0.0_f64; 4];
    for (index, point) in [&first_point, &second_point].into_iter().enumerate() {
        if !is_mapping(point) || !fields_are(point, &["x", "y"])? {
            return unsupported("the coordinates are for the Python rule to judge");
        }
        let (x, y) = (number(&point.get_item("x")?)?, number(&point.get_item("y")?)?);
        if !x.is_finite() || !y.is_finite() {
            return unsupported("the coordinates are for the Python rule to judge");
        }
        coordinates[index * 2] = x;
        coordinates[index * 2 + 1] = y;
    }
    let measured = hypot(&[coordinates[2] - coordinates[0], coordinates[3] - coordinates[1]]);
    if !py_is_close(distance, measured) && (measured - distance).abs() > 1e-9 {
        return unsupported("the distance is for the Python rule to judge");
    }
    let movement = get(&table_of(world)?, MOVEMENT)?;
    if !movement.is_instance_of::<PyDict>() {
        return unsupported("the sample is stale");
    }
    let positions = movement.call_method1(intern!(py, "get"), ("positions", PyDict::new(py)))?;
    if !positions.is_instance_of::<PyDict>() {
        return unsupported("the sample is stale");
    }
    for (id, point) in [(first_id, &first_point), (second_id, &second_point)] {
        let position = get(&positions, id)?;
        if !position.is_instance_of::<PyDict>()
            || !get_is(&position, "position_kind", "physical")?
            || !get(&position, "zone_id")?.eq(&zone_value)?
            || get(&position, "point")?.ne(point)?
        {
            return unsupported("the sample is stale");
        }
    }
    Ok(())
}

/// `ProximityContactHandler.apply`: it checks and changes nothing.
#[pyfunction]
fn proximity(_handler: &Bound<'_, PyAny>, world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    counted(py, "proximity", refused_by_python(py, proximity_on(world, event)))
}

// ---- ResourceLedgerHandler -------------------------------------------------------------------------

fn ledger_event_of<'py>(world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>, operation: &str) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let payload = attribute(event, intern!(py, "payload"))?;
    let table = table_of(world)?;
    let mut ledger_event = event.clone();
    if operation == "delivery" && payload.contains("shipment_id")? {
        let operational = get(&table, OPERATIONAL)?;
        if !operational.is_truthy()? {
            return unsupported("the shipment has no operational state");
        }
        let shipments = operational.call_method1(intern!(py, "get"), ("shipments", PyDict::new(py)))?;
        let shipment = shipments.call_method1(intern!(py, "get"), (payload.get_item("shipment_id")?,))?;
        if !fields_are(&payload, &["warehouse_id", "resource_id", "quantity", "shipment_id"])?
            || !shipment.is_instance_of::<PyDict>()
            || !get(&shipment, "event_id")?.eq(attribute(event, intern!(py, "event_id"))?)?
            || !get(&shipment, "resource_id")?.eq(payload.get_item("resource_id")?)?
            || !get(&shipment, "due_minute")?.eq(attribute(event, intern!(py, "due_minute"))?)?
        {
            return unsupported("the delivery does not match its shipment");
        }
        let without = PyDict::new(py);
        for name in ["warehouse_id", "resource_id", "quantity"] {
            without.set_item(name, payload.get_item(name)?)?;
        }
        // In the order of the payload, as the Python rule's comprehension.
        let ordered = PyDict::new(py);
        for key in payload.try_iter()? {
            let key = key?;
            if !text_is(&key, "shipment_id") {
                ordered.set_item(&key, without.get_item(&key)?)?;
            }
        }
        let kwargs = PyDict::new(py);
        for name in ["event_id", "kind", "due_minute", "priority", "sequence", "event_type", "source_id", "causation_id"] {
            kwargs.set_item(name, event.getattr(name)?)?;
        }
        kwargs.set_item("payload", ordered)?;
        kwargs.set_item("provenance", py.import("builtins")?.getattr("dict")?.call1((attribute(event, intern!(py, "provenance"))?,))?)?;
        ledger_event = models(py)?.getattr(intern!(py, "ScheduledEvent"))?.call((), Some(&kwargs))?;
    }
    if exact_int(&attribute(world, intern!(py, "schema_version"))?)? == 6 && table.contains("resource_service_v1")? {
        let service = table.get_item("resource_service_v1")?;
        let reservation = get(&payload, "reservation_id")?;
        if matches!(operation, "release" | "consume")
            && exact_str(&reservation).is_ok()
            && service.get_item("active_reservation_ids")?.contains(&reservation)?
        {
            return unsupported("the reservation is the service's");
        }
        if operation == "reserve" {
            for task in attribute(world, intern!(py, "active_tasks"))?.call_method0(intern!(py, "values"))?.try_iter()? {
                let task = task?;
                if attribute(&task, intern!(py, "id"))?.eq(&reservation)?
                    && text_is(&attribute(&task, intern!(py, "kind"))?, "resource_service_queue")
                {
                    return unsupported("the reservation ID is a queued task's");
                }
            }
        }
    }
    Ok(ledger_event)
}

/// `ResourceLedgerHandler.apply`, with `apply_resource_operation`'s own kernel.
#[pyfunction]
fn resource_ledger(handler: &Bound<'_, PyAny>, world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let result = (|| {
        let operation = attribute(handler, intern!(py, "operation"))?;
        let ledger_event = refused_by_python(py, ledger_event_of(world, event, exact_str(&operation)?))?;
        crate::rules::resources::apply_operation(world, &ledger_event, &operation)
    })();
    counted(py, "resource_ledger", result)
}

// ---- Facility outages and recoveries ---------------------------------------------------------------

/// `FacilityOutageHandler.apply` and `FacilityRecoveryHandler.apply`: `units` of a sign.
fn facility_units<'py>(world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>, field: &str, restores: bool) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let py = world.py();
    let plan = (|| -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>, i64, i64)> {
        let payload = attribute(event, intern!(py, "payload"))?;
        if !fields_are(&payload, &["facility_id", field])? {
            return unsupported("the payload has other fields");
        }
        let facility_id = payload.get_item("facility_id")?;
        let id = exact_str(&facility_id)?;
        let units = exact_int(&payload.get_item(field)?)?;
        if id.is_empty() || units <= 0 {
            return unsupported("the units are for the Python rule to judge");
        }
        let facility = get(&attribute(world, intern!(py, "facilities"))?, id)?;
        if facility.is_none() || active_outage(world, id)? || has_active_modifier(world, &facility_id)? {
            return unsupported("the facility is for the Python rule to judge");
        }
        let previous = exact_int(&attribute(&facility, intern!(py, "working_units"))?)?;
        let next = if restores { previous.checked_add(units) } else { previous.checked_sub(units) };
        let Some(next) = next else {
            return unsupported("the units are past 64 bits");
        };
        let capacity = exact_int(&attribute(&facility, intern!(py, "capacity"))?)?;
        if next < 0 || next > capacity {
            return unsupported("the units are for the Python rule to judge");
        }
        Ok((facility_id, facility, previous, next))
    })();
    let (facility_id, facility, previous, next) = refused_by_python(py, plan)?;
    let id = exact_str(&facility_id)?;

    // Write. As with an outage's Python rule, those being served finish.
    facility.setattr(intern!(py, "working_units"), next)?;
    let infrastructure = get(&table_of(world)?, INFRASTRUCTURE)?;
    if infrastructure.is_instance_of::<PyDict>() {
        infrastructure.get_item("base_working_units")?.set_item(id, next)?;
    }
    let event_id = attribute(event, intern!(py, "event_id"))?;
    let source = attribute(event, intern!(py, "source_id"))?;
    let sources: Vec<String> = match optional_text(&source) {
        Ok(Some(text)) if !text.is_empty() => vec![text.to_owned()],
        _ => Vec::new(),
    };
    let record = facility_recovery_record(
        world,
        id,
        exact_str(&event_id)?,
        if restores { "restored" } else { "degraded" },
        previous,
        next,
        exact_str(&event_id)?,
        sources,
    )?;
    Ok(match record {
        Some(record) => Some(PyTuple::new(py, [record])?),
        None => None,
    })
}

#[pyfunction]
fn facility_outage<'py>(_handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyTuple>>> {
    counted(world.py(), "facility_outage", facility_units(world, event, "units_out", false))
}

#[pyfunction]
fn facility_recovery<'py>(_handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyTuple>>> {
    counted(world.py(), "facility_recovery", facility_units(world, event, "units_restored", true))
}

// ---- Deliveries and water ---------------------------------------------------------------------------

fn delivery_delay_on(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    transaction(world, event, |candidate| {
        let payload = attribute(event, intern!(py, "payload"))?;
        require_source(event)?;
        if !fields_are(&payload, &["shipment_id", "to_minute"])? {
            return unsupported("the payload has other fields");
        }
        let operational = get(&table_of(candidate)?, OPERATIONAL)?;
        if operational.is_none() {
            return unsupported("no operational state");
        }
        let shipment_id = payload.get_item("shipment_id")?;
        let record = operational.get_item("shipments")?.call_method1(intern!(py, "get"), (&shipment_id,))?;
        if record.is_none() {
            return unsupported("an unknown shipment");
        }
        let wanted = record.get_item("event_id")?;
        let mut target = None;
        for pending in pending_of(candidate)? {
            if attribute(&pending, intern!(py, "event_id"))?.eq(&wanted)? {
                target = Some(pending);
                break;
            }
        }
        let Some(target) = target else {
            return unsupported("the shipment is no longer pending");
        };
        let target_payload = attribute(&target, intern!(py, "payload"))?;
        let target_due = exact_int(&attribute(&target, intern!(py, "due_minute"))?)?;
        if !text_is(&attribute(&target, intern!(py, "event_type"))?, "resource_delivery")
            || !get(&target_payload, "shipment_id")?.eq(&shipment_id)?
            || !record.get_item("due_minute")?.eq(target_due)?
        {
            return unsupported("the shipment is no longer pending");
        }
        let to_minute = exact_int(&payload.get_item("to_minute")?)?;
        let now = minute_of(candidate)?;
        if target_due <= now || to_minute <= target_due || to_minute <= now || to_minute >= duration_of(candidate)? {
            return unsupported("the delay does not move a not-yet-due delivery forward");
        }
        let target_document = target.call_method0(intern!(py, "to_dict"))?;
        let provenance = target_document.get_item("provenance")?;
        provenance.set_item("last_delay_event_id", attribute(event, intern!(py, "event_id"))?)?;
        let event_provenance = event.call_method0(intern!(py, "to_dict"))?.get_item("provenance")?;
        let marker = get(&event_provenance, "effect_program_v1")?;
        if is_mapping(&marker) {
            let derived = PyDict::new(py);
            derived.update(marker.downcast::<PyMapping>()?)?;
            derived.set_item("phase", "derived")?;
            provenance.set_item("effect_program_v1", derived)?;
        }
        let kwargs = PyDict::new(py);
        for name in ["event_id", "kind", "priority", "sequence", "event_type", "source_id"] {
            kwargs.set_item(name, target.getattr(name)?)?;
        }
        kwargs.set_item("due_minute", to_minute)?;
        kwargs.set_item("payload", target_document.get_item("payload")?)?;
        kwargs.set_item("causation_id", attribute(event, intern!(py, "event_id"))?)?;
        kwargs.set_item("provenance", provenance)?;
        let replacement = models(py)?.getattr(intern!(py, "ScheduledEvent"))?.call((), Some(&kwargs))?;
        attribute(candidate, intern!(py, "event_queue"))?
            .call_method1(intern!(py, "replace"), (attribute(&target, intern!(py, "event_id"))?, replacement))?;
        record.set_item("due_minute", to_minute)?;
        Ok(Vec::new())
    })
    .map(|_| ())
}

#[pyfunction]
fn delivery_delay(_handler: &Bound<'_, PyAny>, world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    counted(py, "delivery_delay", refused_by_python(py, delivery_delay_on(world, event)))
}

#[pyfunction]
fn water_outage(_handler: &Bound<'_, PyAny>, world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let result = transaction(world, event, |candidate| {
        let payload = attribute(event, intern!(py, "payload"))?;
        require_source(event)?;
        let is_zone = fields_are(&payload, &["outage_id", "zone_id"])?;
        if !is_zone && !fields_are(&payload, &["outage_id", "facility_id"])? {
            return unsupported("the payload has other fields");
        }
        let operational = get(&table_of(candidate)?, OPERATIONAL)?;
        if !operational.is_truthy()? {
            return unsupported("no operational state");
        }
        let record = operational.get_item("water_outages")?.call_method1(intern!(py, "get"), (payload.get_item("outage_id")?,))?;
        if record.is_none() || !record.get_item("outage_event_id")?.eq(attribute(event, intern!(py, "event_id"))?)? {
            return unsupported("the outage does not match its state");
        }
        let target_id = payload.get_item(if is_zone { "zone_id" } else { "facility_id" })?;
        if !text_is(&record.get_item("target_kind")?, if is_zone { "zone" } else { "facility" }) || !record.get_item("target_id")?.eq(&target_id)? {
            return unsupported("the outage's target does not match its state");
        }
        let facilities = attribute(candidate, intern!(py, "facilities"))?;
        if is_zone {
            let mut found = false;
            for facility in facilities.call_method0(intern!(py, "values"))?.try_iter()? {
                let facility = facility?;
                if text_is(&attribute(&facility, intern!(py, "kind"))?, "water_station")
                    && attribute(&facility, intern!(py, "zone_id"))?.eq(&target_id)?
                {
                    found = true;
                    break;
                }
            }
            if !found {
                return unsupported("the zone has no water station");
            }
        } else {
            let facility = facilities.call_method1(intern!(py, "get"), (&target_id,))?;
            if facility.is_none() || !text_is(&attribute(&facility, intern!(py, "kind"))?, "water_station") {
                return unsupported("the target is not a water station");
            }
        }
        if record.get_item("active")?.is_truthy()? {
            return unsupported("the outage is already active");
        }
        record.set_item("active", true)?;
        Ok(Vec::new())
    });
    counted(py, "water_outage", refused_by_python(py, result).map(|_| ()))
}

#[pyfunction]
fn water_restoration(_handler: &Bound<'_, PyAny>, world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let result = transaction(world, event, |candidate| {
        let payload = attribute(event, intern!(py, "payload"))?;
        require_source(event)?;
        if !fields_are(&payload, &["outage_id"])? {
            return unsupported("the payload has other fields");
        }
        let operational = get(&table_of(candidate)?, OPERATIONAL)?;
        if !operational.is_truthy()? {
            return unsupported("no operational state");
        }
        let record = operational.get_item("water_outages")?.call_method1(intern!(py, "get"), (payload.get_item("outage_id")?,))?;
        if record.is_none()
            || !record.get_item("restore_event_id")?.eq(attribute(event, intern!(py, "event_id"))?)?
            || !record.get_item("active")?.is_truthy()?
        {
            return unsupported("the restoration does not match an active outage");
        }
        record.set_item("active", false)?;
        Ok(Vec::new())
    });
    counted(py, "water_restoration", refused_by_python(py, result).map(|_| ()))
}

// ---- Staff unavailable and restored -----------------------------------------------------------------

/// `{**event.to_dict()["provenance"]["effect_program_v1"], "phase": "derived"}` if the event has a marker.
pub(crate) fn derived_marker<'py>(event: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyDict>>> {
    let py = event.py();
    if !is_mapping(&get(&attribute(event, intern!(py, "provenance"))?, "effect_program_v1")?) {
        return Ok(None);
    }
    let marker = event.call_method0(intern!(py, "to_dict"))?.get_item("provenance")?.get_item("effect_program_v1")?;
    let derived = PyDict::new(py);
    derived.update(marker.downcast::<PyMapping>()?)?;
    derived.set_item("phase", "derived")?;
    Ok(Some(derived))
}

fn task_is(task: &Bound<'_, PyAny>, staff_id: &str, kinds: &[&str]) -> PyResult<bool> {
    let py = task.py();
    let kind = attribute(task, intern!(py, "kind"))?;
    Ok(text_is(&attribute(task, intern!(py, "staff_id"))?, staff_id) && kinds.iter().any(|wanted| text_is(&kind, wanted)))
}

fn staff_unavailable_on<'py>(world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py = world.py();
    transaction(world, event, |candidate| {
        let mut records = Vec::new();
        let payload = attribute(event, intern!(py, "payload"))?;
        require_source(event)?;
        if !fields_are(&payload, &["staff_id", "until_minute"])? {
            return unsupported("the payload has other fields");
        }
        let table = table_of(candidate)?;
        let operational = get(&table, OPERATIONAL)?;
        if operational.is_none() {
            return unsupported("no operational state");
        }
        let staff_value = payload.get_item("staff_id")?;
        let staff_id = exact_str(&staff_value)?;
        let staff_member = get(&attribute(candidate, intern!(py, "staff"))?, staff_id)?;
        if staff_member.is_none() {
            return unsupported("an unknown staff member");
        }
        let until = exact_int(&payload.get_item("until_minute")?)?;
        let duration = duration_of(candidate)?;
        if until <= minute_of(candidate)? || until > duration {
            return unsupported("the end of the absence is invalid");
        }
        let status = operational.get_item("staff_status")?.get_item(staff_id)?;
        let event_id = attribute(event, intern!(py, "event_id"))?;
        if status.get_item("unavailability_event_id")?.eq(&event_id)? {
            return unsupported("the absence was already applied");
        }
        let mut restore_event_id: Option<String> = None;
        if until < duration {
            let sequence = exact_int(&attribute(candidate, intern!(py, "id_counters"))?.call_method1(intern!(py, "get"), ("event", 0))?)?;
            let id = format!("staff-restored-{sequence}");
            let restore_payload = PyDict::new(py);
            restore_payload.set_item("staff_id", &staff_value)?;
            restore_payload.set_item("unavailability_event_id", &event_id)?;
            let provenance = assumption(py, PyString::new(py, OPERATIONAL_SOURCE).as_any(), OPERATIONAL_POLICY)?;
            if let Some(marker) = derived_marker(event)? {
                provenance.set_item("effect_program_v1", marker)?;
            }
            let restore = new_event(
                py,
                NewEvent {
                    event_id: &id,
                    kind: "COMPLETION",
                    due_minute: until,
                    priority: 10,
                    sequence,
                    event_type: "staff_restored",
                    payload: restore_payload.into_any(),
                    source_id: Some(attribute(event, intern!(py, "source_id"))?),
                    causation_id: Some(event_id.clone()),
                    provenance: provenance.into_any(),
                },
            )?;
            ensure_identity_free(candidate, &id, sequence)?;
            if exact_int(&candidate.call_method0(intern!(py, "allocate_event_sequence"))?)? != sequence {
                return unsupported("the event sequence changed");
            }
            candidate.call_method1(intern!(py, "schedule_event"), (restore,))?;
            restore_event_id = Some(id);
        }
        let mut support_tasks = Vec::new();
        let mut resource_tasks = 0_usize;
        for task in attribute(candidate, intern!(py, "active_tasks"))?.call_method0(intern!(py, "values"))?.try_iter()? {
            let task = task?;
            if task_is(&task, staff_id, &["staff_support"])? {
                support_tasks.push(task);
            } else if task_is(&task, staff_id, &["resource_service_water", "resource_service_food"])? {
                resource_tasks += 1;
            }
        }
        let operations = table.call_method1(intern!(py, "get"), ("staff_operations_v1", PyDict::new(py)))?;
        let record = if operations.is_instance_of::<PyDict>() {
            operations.call_method1(intern!(py, "get"), ("staff", PyDict::new(py)))?.call_method1(intern!(py, "get"), (staff_id,))?
        } else {
            py.None().into_bound(py)
        };
        let autonomous = !record.is_none() && !get_is(&record, "phase", "idle")?;
        if support_tasks.len() + resource_tasks + usize::from(autonomous) > 1 {
            return unsupported("the staff member owns several tasks");
        }
        if resource_tasks > 0 {
            let manager = py.import("sheltersim.domain.resource_service")?.getattr("ResourceServiceManager")?.call1((candidate,))?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("causation_event", event)?;
            manager.call_method("interrupt_for_staff", (staff_id,), Some(&kwargs))?;
        } else if let Some(task) = support_tasks.first() {
            records.push(interrupt_support_task(candidate, task, &staff_member, event)?);
        } else if autonomous {
            interrupt_operation(candidate, staff_id, event, None)?;
        }
        status.set_item("available", false)?;
        status.set_item("unavailability_event_id", &event_id)?;
        status.set_item("until_minute", until)?;
        status.set_item("restore_event_id", restore_event_id)?;
        Ok(records)
    })
}

/// `StaffUnavailabilityHandler.apply`, which returns a tuple even when empty.
#[pyfunction]
fn staff_unavailable<'py>(_handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
    let py = world.py();
    let records = counted(py, "staff_unavailable", refused_by_python(py, staff_unavailable_on(world, event)))?;
    PyTuple::new(py, records)
}

#[pyfunction]
fn staff_restoration(_handler: &Bound<'_, PyAny>, world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let result = transaction(world, event, |candidate| {
        let payload = attribute(event, intern!(py, "payload"))?;
        require_source(event)?;
        if !fields_are(&payload, &["staff_id", "unavailability_event_id"])? {
            return unsupported("the payload has other fields");
        }
        let operational = get(&table_of(candidate)?, OPERATIONAL)?;
        if !operational.is_truthy()? {
            return unsupported("no operational state");
        }
        let record = operational.get_item("staff_status")?.call_method1(intern!(py, "get"), (payload.get_item("staff_id")?,))?;
        if record.is_none()
            || !record.get_item("unavailability_event_id")?.eq(payload.get_item("unavailability_event_id")?)?
            || !record.get_item("restore_event_id")?.eq(attribute(event, intern!(py, "event_id"))?)?
            || record.get_item("available")?.is_truthy()?
        {
            return unsupported("the restoration is for the Python rule to judge");
        }
        record.set_item("available", true)?;
        Ok(Vec::new())
    });
    counted(py, "staff_restoration", refused_by_python(py, result).map(|_| ()))
}

/// `_interrupt_support_task`, on a copy of the world. Its record.
fn interrupt_support_task<'py>(
    world: &Bound<'py, PyAny>,
    task: &Bound<'py, PyAny>,
    staff_member: &Bound<'py, PyAny>,
    causation: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let task_id = attribute(task, intern!(py, "id"))?;
    let task_text = exact_str(&task_id)?;
    let mut request = None;
    for candidate in world.call_method0(intern!(py, "unsettled_support_requests"))?.try_iter()? {
        let candidate = candidate?;
        if text_is(&attribute(&candidate, intern!(py, "status"))?.getattr(intern!(py, "value"))?, "assigned")
            && attribute(&candidate, intern!(py, "task_id"))?.eq(&task_id)?
        {
            request = Some(candidate);
            break;
        }
    }
    let Some(request) = request else {
        return unsupported("the task has no request");
    };
    let task_agent = attribute(task, intern!(py, "agent_id"))?;
    let agents = attribute(world, intern!(py, "agents"))?;
    let agent = agents.call_method1(intern!(py, "get"), (if task_agent.is_none() { PyString::new(py, "").into_any() } else { task_agent },))?;
    if agent.is_none() {
        return unsupported("the task has no person");
    }
    let pending = pending_of(world)?;
    let mut completions = Vec::new();
    for item in &pending {
        if text_is(&attribute(item, intern!(py, "event_type"))?, "staff_support_complete")
            && get(&attribute(item, intern!(py, "payload"))?, "task_id")?.eq(&task_id)?
        {
            completions.push(item);
        }
    }
    let due = attribute(task, intern!(py, "due_minute"))?;
    if !attribute(staff_member, intern!(py, "active_task_id"))?.eq(&task_id)?
        || !attribute(staff_member, intern!(py, "work_queue"))?.eq(PyList::new(py, [&task_id])?)?
        || completions.len() != 1
        || !attribute(completions[0], intern!(py, "due_minute"))?.eq(&due)?
    {
        return unsupported("the task's ownership is inconsistent");
    }
    let request_id = attribute(&request, intern!(py, "id"))?;
    let request_agent = attribute(&request, intern!(py, "agent_id"))?;
    let timeout_minute = attribute(&request, intern!(py, "timeout_minute"))?;
    let timeout_value = exact_int(&timeout_minute)?;
    let mut timeouts = Vec::new();
    for item in &pending {
        if text_is(&attribute(item, intern!(py, "event_type"))?, "support_request_timeout")
            && get(&attribute(item, intern!(py, "payload"))?, "request_id")?.eq(&request_id)?
        {
            timeouts.push(item);
        }
    }
    if timeouts.len() > 1 || timeouts.first().is_some_and(|item| !attribute(item, intern!(py, "due_minute")).is_ok_and(|minute| minute.eq(&timeout_minute).unwrap_or(false))) {
        return unsupported("the timeout is inconsistent");
    }
    let now = minute_of(world)?;
    let causation_id = attribute(causation, intern!(py, "event_id"))?;
    if timeout_value > now && timeouts.is_empty() {
        let sequence = exact_int(&attribute(world, intern!(py, "id_counters"))?.call_method1(intern!(py, "get"), ("event", 0))?)?;
        let id = format!("support-timeout-rescheduled-{sequence}");
        let payload = PyDict::new(py);
        payload.set_item("request_id", &request_id)?;
        payload.set_item("agent_id", &request_agent)?;
        let policy = py.import("sheltersim.domain.staff")?.getattr("STAFF_SUPPORT_POLICY_VERSION")?;
        let provenance = assumption(py, PyString::new(py, SUPPORT_SOURCE).as_any(), exact_str(&policy)?)?;
        provenance.set_item("rescheduled_after_staff_unavailable", true)?;
        let timeout = new_event(
            py,
            NewEvent {
                event_id: &id,
                kind: "COMPLETION",
                due_minute: timeout_value,
                priority: 20,
                sequence,
                event_type: "support_request_timeout",
                payload: payload.into_any(),
                source_id: Some(request_id.clone()),
                causation_id: Some(causation_id.clone()),
                provenance: provenance.into_any(),
            },
        )?;
        ensure_identity_free(world, &id, sequence)?;
        if exact_int(&world.call_method0(intern!(py, "allocate_event_sequence"))?)? != sequence {
            return unsupported("the event sequence changed");
        }
        world.call_method1(intern!(py, "schedule_event"), (timeout,))?;
    }

    let agent_id = attribute(&agent, intern!(py, "id"))?;
    let tasks = attribute(world, intern!(py, "active_tasks"))?;
    let mut trips = Vec::new();
    for candidate in tasks.call_method0(intern!(py, "values"))?.try_iter()? {
        let candidate = candidate?;
        if text_is(&attribute(&candidate, intern!(py, "kind"))?, "staff_support_travel") && attribute(&candidate, intern!(py, "agent_id"))?.eq(&agent_id)? {
            trips.push(candidate);
        }
    }
    if trips.len() > 1 {
        return unsupported("the person has two trips to support");
    }
    if let Some(trip) = trips.first() {
        let trip_id = attribute(trip, intern!(py, "id"))?;
        let mut walks = Vec::new();
        for item in pending_of(world)? {
            let payload = attribute(&item, intern!(py, "payload"))?;
            if text_is(&attribute(&item, intern!(py, "event_type"))?, "agent_travel_complete")
                && get(&payload, "task_id")?.eq(&trip_id)?
                && get_is(&payload, "purpose", "staff_support_travel")?
                && get(&payload, "request_id")?.eq(&request_id)?
            {
                walks.push(item);
            }
        }
        if walks.len() != 1 || !attribute(&walks[0], intern!(py, "due_minute"))?.eq(attribute(trip, intern!(py, "due_minute"))?)? {
            return unsupported("the person's trip is inconsistent");
        }
        let walk = attribute(world, intern!(py, "event_queue"))?.call_method1(intern!(py, "remove"), (attribute(&walks[0], intern!(py, "event_id"))?,))?;
        attribute(world, intern!(py, "event_log"))?.call_method1(
            intern!(py, "append"),
            (log_record(
                py,
                &walk,
                &attribute(world, intern!(py, "minute_index"))?,
                "SUPPRESSED",
                Some("assigned staff became unavailable before resident arrival"),
            )?,),
        )?;
        tasks.del_item(&trip_id)?;
        let movement = get(&table_of(world)?, MOVEMENT)?;
        let position = if movement.is_instance_of::<PyDict>() {
            movement.call_method1(intern!(py, "get"), ("positions", PyDict::new(py)))?.call_method1(intern!(py, "get"), (&agent_id,))?
        } else {
            py.None().into_bound(py)
        };
        if position.is_instance_of::<PyDict>() {
            let zone = get(&position, "zone_id")?;
            if attribute(world, intern!(py, "zones"))?.contains(&zone)? {
                agent.setattr(intern!(py, "zone_id"), zone)?;
            }
        }
        py.import("sheltersim.domain.movement")?.getattr("finish_movement")?.call1((world, &agent_id))?;
    }

    tasks.del_item(&task_id)?;
    attribute(staff_member, intern!(py, "work_queue"))?.call_method0(intern!(py, "clear"))?;
    staff_member.setattr(intern!(py, "active_task_id"), py.None())?;
    staff_member.setattr(intern!(py, "busy_until"), 0)?;
    table_of(world)?.get_item(OPERATIONAL)?.get_item("cancelled_staff_task_ids")?.call_method1(intern!(py, "append"), (&task_id,))?;
    request.setattr(intern!(py, "assigned_staff_id"), py.None())?;
    request.setattr(intern!(py, "task_id"), py.None())?;
    let statuses = py.import("sheltersim.domain.models")?.getattr("SupportRequestStatus")?;
    let activities = py.import("sheltersim.domain.models")?.getattr("Activity")?;
    let requeued = timeout_value > now;
    if requeued {
        request.setattr(intern!(py, "status"), statuses.getattr("PENDING")?)?;
        agent.setattr(intern!(py, "activity"), activities.getattr("SEEKING_HELP")?)?;
        agent.setattr(intern!(py, "target_id"), py.None())?;
        agent.setattr(intern!(py, "support_request_id"), &request_id)?;
    } else {
        let configs = attribute(world, intern!(py, "model_configs"))?;
        let cooldown = configs
            .call_method1(intern!(py, "get"), ("staff", PyDict::new(py)))?
            .call_method1(intern!(py, "get"), ("support_cooldown_minutes", 30))?;
        let until = now + exact_int(&cooldown)?;
        request.setattr(intern!(py, "status"), statuses.getattr("UNRESOLVED")?)?;
        request.setattr(intern!(py, "resolved_minute"), now)?;
        request.setattr(intern!(py, "cooldown_until"), until)?;
        agent.setattr(intern!(py, "activity"), activities.getattr("IDLE")?)?;
        agent.setattr(intern!(py, "target_id"), py.None())?;
        agent.setattr(intern!(py, "support_request_id"), py.None())?;
        agent.setattr(intern!(py, "support_cooldown_until"), until)?;
        add_metric(world, "support_unresolved_count")?;
    }
    let previous = attribute(staff_member, intern!(py, "id"))?;
    let cause_type = if text_is(&attribute(causation, intern!(py, "event_type"))?, "staff_shift_change") { "staff_shift_change" } else { "staff_unavailable" };
    continuity_record(
        world,
        exact_str(&request_id)?,
        exact_str(&request_agent)?,
        task_text,
        exact_str(&previous)?,
        if requeued { "requeued" } else { "unresolved" },
        exact_str(&causation_id)?,
        cause_type,
    )
}

// ---- A resource service cancelled or interrupted -----------------------------------------------------
//
// The manager's small helpers and the queue's rules (`domain/facility_queues.py`) are called by name:
// what they hold is the state of other lanes' rules.

fn service_module(py: Python<'_>) -> PyResult<Bound<'_, PyModule>> {
    py.import("sheltersim.domain.resource_service")
}

/// `ResourceServiceManager.resource_for_facility`.
fn resource_for(kind: &Bound<'_, PyAny>) -> Option<&'static str> {
    crate::rules::service::resource_of(exact_str(kind).ok()?)
}

/// `self.world.agents[task.agent_id or ""]`, and the same of the facility.
fn or_empty<'py>(map: &Bound<'py, PyAny>, key: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if key.is_truthy()? {
        map.get_item(key)
    } else {
        map.get_item("")
    }
}

/// `ResourceServiceManager.cancel`.
#[pyfunction]
pub(crate) fn cancel_service<'py>(manager: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = manager.py();
    let world = attribute(manager, intern!(py, "world"))?;
    struct Active<'py> {
        resource_id: &'static str,
        staff: Bound<'py, PyAny>,
    }
    let plan = (|| -> PyResult<_> {
        let payload = attribute(event, intern!(py, "payload"))?;
        let task_id = get(&payload, "task_id")?;
        if exact_str(&task_id)?.is_empty() || !fields_are(&payload, &["task_id", "reason"])? {
            return unsupported("the cancellation is for the Python rule to judge");
        }
        let reason = payload.get_item("reason")?;
        let reason = exact_str(&reason)?.to_owned();
        if !matches!(reason.as_str(), "operator_cancelled" | "facility_stopped" | "staff_unavailable") {
            return unsupported("the cancellation is for the Python rule to judge");
        }
        if manager.call_method0("_cancelled_task_ids")?.contains(&task_id)? {
            return unsupported("the task was already cancelled");
        }
        let task = attribute(&world, intern!(py, "active_tasks"))?.call_method1(intern!(py, "get"), (&task_id,))?;
        if task.is_none() {
            return unsupported("no active task");
        }
        let kind = attribute(&task, intern!(py, "kind"))?;
        let queued = text_is(&kind, "resource_service_queue");
        if !queued && !text_is(&kind, "resource_service_water") && !text_is(&kind, "resource_service_food") {
            return unsupported("the task is another kind");
        }
        let agent = or_empty(&attribute(&world, intern!(py, "agents"))?, attribute(&task, intern!(py, "agent_id"))?)?;
        let facility = or_empty(&attribute(&world, intern!(py, "facilities"))?, attribute(&task, intern!(py, "resource_id"))?)?;
        let agent_id = attribute(&agent, intern!(py, "id"))?;
        let mut active = None;
        if queued {
            if !attribute(&facility, intern!(py, "resource_queue_agent_ids"))?.contains(&agent_id)? {
                return unsupported("the queue has no such person");
            }
        } else {
            let Some(resource_id) = resource_for(&attribute(&facility, intern!(py, "kind"))?) else {
                return unsupported("not a resource facility");
            };
            if !attribute(&facility, intern!(py, "resource_active_agent_ids"))?.contains(&agent_id)? {
                return unsupported("the facility has no such slot");
            }
            let staff_key = attribute(&task, intern!(py, "staff_id"))?;
            let staff = attribute(&world, intern!(py, "staff"))?.call_method1(intern!(py, "get"), (if staff_key.is_truthy()? { staff_key } else { PyString::new(py, "").into_any() },))?;
            let reservation = table_of(&world)?.get_item("resource_ledger_v1")?.get_item("accounts")?.get_item(resource_id)?.get_item("reservations")?.call_method1(intern!(py, "get"), (&task_id,))?;
            if staff.is_none()
                || !attribute(&staff, intern!(py, "active_task_id"))?.eq(&task_id)?
                || !attribute(&staff, intern!(py, "work_queue"))?.eq(PyList::new(py, [&task_id])?)?
                || !reservation.is_instance_of::<PyDict>()
                || !get(&reservation, "agent_id")?.eq(&agent_id)?
                || !get_is(&reservation, "resource_id", resource_id)?
                || !manager.call_method0("_service_reservation_ids")?.contains(&task_id)?
            {
                return unsupported("the cancellation does not match its reservation");
            }
            active = Some(Active { resource_id, staff });
        }
        let warehouse = if active.is_some() { Some(manager.call_method0("_warehouse_id")?) } else { None };
        Ok((task_id, agent, agent_id, facility, active, warehouse, reason))
    })();
    let (task_id, agent, agent_id, facility, active, warehouse, reason) = refused_by_python(py, plan)?;
    let event_id = attribute(event, intern!(py, "event_id"))?;
    let facility_id = attribute(&facility, intern!(py, "id"))?;
    let module = service_module(py)?;

    // Write.
    match active {
        None => {
            attribute(&facility, intern!(py, "resource_queue_agent_ids"))?.call_method1("remove", (&agent_id,))?;
            manager.call_method1("_clear_queue_stall", (&facility_id,))?;
        }
        Some(Active { resource_id, staff }) => {
            let payload = PyDict::new(py);
            payload.set_item("warehouse_id", warehouse)?;
            payload.set_item("resource_id", resource_id)?;
            payload.set_item("reservation_id", &task_id)?;
            payload.set_item("agent_id", &agent_id)?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("causation_id", &event_id)?;
            let release = manager.call_method("_audit_event", ("release", payload), Some(&kwargs))?;
            if let Err(error) = module.getattr("apply_resource_operation")?.call1((&world, &release, "release")) {
                if error.is_instance_of::<pyo3::exceptions::PyValueError>(py) {
                    let failed = models(py)?.getattr(intern!(py, "EventApplicationError"))?;
                    let text = format!("resource reservation release rejected: {}", error.value(py).str()?);
                    return Err(PyErr::from_value(failed.call1((text,))?));
                }
                return Err(error);
            }
            manager.call_method1("_record_internal_event", (&release,))?;
            manager.call_method1("_remove_service_reservation", (&task_id,))?;
            attribute(&facility, intern!(py, "resource_active_agent_ids"))?.call_method1("remove", (&agent_id,))?;
            manager.call_method1("_release_staff", (staff,))?;
        }
    }
    let interrupted = reason == "staff_unavailable";
    let status_kwargs = PyDict::new(py);
    status_kwargs.set_item("status", if interrupted { "interrupted" } else { "cancelled" })?;
    let request = module.getattr("finish_facility_queue_service")?.call((&world, &task_id), Some(&status_kwargs))?;
    if !request.is_none() {
        let kwargs = PyDict::new(py);
        kwargs.set_item("transition", if interrupted { "interrupted" } else { "cancelled" })?;
        kwargs.set_item("task_id", &task_id)?;
        kwargs.set_item("causation_id", &event_id)?;
        module.getattr("record_queue_transition")?.call((&world, request), Some(&kwargs))?;
    }
    attribute(&world, intern!(py, "active_tasks"))?.del_item(&task_id)?;
    let activities = py.import("sheltersim.domain.models")?.getattr("Activity")?;
    agent.setattr(intern!(py, "activity"), activities.getattr("IDLE")?)?;
    agent.setattr(intern!(py, "target_id"), py.None())?;
    manager.call_method0("_cancelled_task_ids")?.call_method1("append", (&task_id,))?;
    manager.call_method1("_increment_metric", ("resource_service_cancelled_count",))?;
    Ok(())
}

/// `ResourceServiceManager.cancel`, counted.
#[pyfunction]
fn cancel<'py>(manager: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    counted(manager.py(), "resource_service_cancel", cancel_service(manager, event))
}

/// `ResourceServiceCancellationHandler.apply`.
#[pyfunction]
fn service_cancellation<'py>(handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    if exact_int(&attribute(world, intern!(py, "schema_version"))?)? != 6 {
        return unsupported("resource service cancellation requires WorldState v6");
    }
    attribute(handler, intern!(py, "service"))?.call_method1(intern!(py, "cancel"), (event,))?;
    Ok(())
}

/// `ResourceServiceManager.interrupt_for_staff`.
#[pyfunction]
#[pyo3(signature = (manager, staff_id, *, causation_event))]
fn interrupt_for_staff<'py>(manager: &Bound<'py, PyAny>, staff_id: &Bound<'py, PyAny>, causation_event: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = manager.py();
    let world = attribute(manager, intern!(py, "world"))?;
    let plan = (|| -> PyResult<_> {
        let id = exact_str(staff_id)?;
        let mut tasks = Vec::new();
        for task in attribute(&world, intern!(py, "active_tasks"))?.call_method0(intern!(py, "values"))?.try_iter()? {
            let task = task?;
            if task_is(&task, id, &["resource_service_water", "resource_service_food"])? {
                tasks.push(task);
            }
        }
        if tasks.is_empty() {
            return Ok(None);
        }
        let [task] = tasks.as_slice() else {
            return unsupported("the staff member owns several services");
        };
        let task_id = attribute(task, intern!(py, "id"))?;
        let module = service_module(py)?;
        let prior = module.getattr("facility_queue_request_for_task")?.call1((&world, &task_id))?;
        let (agent_key, resource_key) = (attribute(task, intern!(py, "agent_id"))?, attribute(task, intern!(py, "resource_id"))?);
        if agent_key.is_none() || resource_key.is_none() {
            return unsupported("the service has incomplete ownership");
        }
        let agent = attribute(&world, intern!(py, "agents"))?.call_method1(intern!(py, "get"), (&agent_key,))?;
        let facility = attribute(&world, intern!(py, "facilities"))?.call_method1(intern!(py, "get"), (&resource_key,))?;
        let staff = attribute(&world, intern!(py, "staff"))?.call_method1(intern!(py, "get"), (id,))?;
        if agent.is_none() || facility.is_none() || staff.is_none() {
            return unsupported("the service has inconsistent state");
        }
        let agent_id = attribute(&agent, intern!(py, "id"))?;
        let in_service = py.import("sheltersim.domain.models")?.getattr("Activity")?.getattr("IN_SERVICE")?;
        if !attribute(&agent, intern!(py, "zone_id"))?.eq(attribute(&facility, intern!(py, "zone_id"))?)?
            || !attribute(&agent, intern!(py, "activity"))?.is(&in_service)
            || !attribute(&staff, intern!(py, "active_task_id"))?.eq(&task_id)?
            || !attribute(&staff, intern!(py, "work_queue"))?.eq(PyList::new(py, [&task_id])?)?
            || !attribute(&staff, intern!(py, "busy_until"))?.eq(attribute(task, intern!(py, "due_minute"))?)?
            || !attribute(&facility, intern!(py, "resource_active_agent_ids"))?.contains(&agent_id)?
        {
            return unsupported("the service has inconsistent state");
        }
        let Some(resource_id) = resource_for(&attribute(&facility, intern!(py, "kind"))?) else {
            return unsupported("not a resource facility");
        };
        let table = table_of(&world)?;
        let reservation = table.get_item("resource_ledger_v1")?.get_item("accounts")?.get_item(resource_id)?.get_item("reservations")?.call_method1(intern!(py, "get"), (&task_id,))?;
        if !reservation.is_instance_of::<PyDict>()
            || !get(&reservation, "agent_id")?.eq(&agent_id)?
            || !get_is(&reservation, "resource_id", resource_id)?
            || !manager.call_method0("_service_reservation_ids")?.contains(&task_id)?
        {
            return unsupported("the reservation is inconsistent");
        }
        let counters = attribute(&world, intern!(py, "id_counters"))?;
        let next = manager.call_method1("_next_queue_task_id", (counters.call_method1(intern!(py, "get"), ("resource_task", 0))?,))?;
        let (queue_task_id, next_counter) = (next.get_item(0)?, next.get_item(1)?);
        let now = attribute(&world, intern!(py, "minute_index"))?;
        // The last time this person queued here, or now.
        let mut queued_at = now.clone();
        let history = table.get_item("resource_service_v1")?.get_item("request_history")?;
        for record in history.call_method0("__reversed__")?.try_iter()? {
            let record = record?;
            if record.get_item("agent_id")?.eq(&agent_id)?
                && record.get_item("facility_id")?.eq(attribute(&facility, intern!(py, "id"))?)?
                && text_is(&record.get_item("outcome")?, "queued")
            {
                queued_at = record.get_item("minute")?;
                break;
            }
        }
        let sequence = exact_int(&counters.call_method1(intern!(py, "get"), ("event", 0))?)?;
        let details = PyDict::new(py);
        details.set_item("action", "staff_interrupted")?;
        details.set_item("staff_id", id)?;
        let provenance = manager.call_method("_provenance", (), Some(&details))?;
        let payload = PyDict::new(py);
        payload.set_item("task_id", &task_id)?;
        payload.set_item("reason", "staff_unavailable")?;
        let id_text = format!("resource-service-cancel-{sequence}");
        let cancellation = new_event(
            py,
            NewEvent {
                event_id: &id_text,
                kind: "ENDOGENOUS",
                due_minute: exact_int(&now)?,
                priority: 0,
                sequence,
                event_type: "resource_service_cancel",
                payload: payload.into_any(),
                source_id: Some(attribute(manager, intern!(py, "config"))?.get_item("source_id")?),
                causation_id: Some(attribute(causation_event, intern!(py, "event_id"))?),
                provenance,
            },
        )?;
        manager.call_method1("_ensure_event_identity_free", (&cancellation,))?;
        Ok(Some((agent, agent_id, facility, prior, queue_task_id, next_counter, queued_at, cancellation, sequence, resource_id, counters)))
    })();
    let Some((agent, agent_id, facility, prior, queue_task_id, next_counter, queued_at, cancellation, sequence, resource_id, counters)) = counted(py, "interrupt_for_staff", refused_by_python(py, plan))? else {
        return Ok(());
    };

    // Write.
    if exact_int(&world.call_method0(intern!(py, "allocate_event_sequence"))?)? != sequence {
        return Err(pyo3::exceptions::PyRuntimeError::new_err("event sequence changed during service interruption"));
    }
    manager.call_method1(intern!(py, "cancel"), (&cancellation,))?;
    manager.call_method1("_record_internal_event", (&cancellation,))?;
    let activities = py.import("sheltersim.domain.models")?.getattr("Activity")?;
    let facility_id = attribute(&facility, intern!(py, "id"))?;
    agent.setattr(intern!(py, "activity"), activities.getattr("QUEUED")?)?;
    agent.setattr(intern!(py, "target_id"), &facility_id)?;
    let task_kwargs = PyDict::new(py);
    task_kwargs.set_item("id", &queue_task_id)?;
    task_kwargs.set_item("kind", "resource_service_queue")?;
    task_kwargs.set_item("agent_id", &agent_id)?;
    task_kwargs.set_item("resource_id", &facility_id)?;
    task_kwargs.set_item("start_minute", &queued_at)?;
    task_kwargs.set_item("due_minute", &queued_at)?;
    let queued_task = crate::rules::service::active_task_class(py)?.call((), Some(&task_kwargs))?;
    counters.set_item("resource_task", next_counter)?;
    attribute(&world, intern!(py, "active_tasks"))?.set_item(&queue_task_id, queued_task)?;
    attribute(&facility, intern!(py, "resource_queue_agent_ids"))?.call_method1("insert", (0, &agent_id))?;
    if !prior.is_none() {
        let module = service_module(py)?;
        let queue_id = prior.get_item("queue_id")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("queue_id", &queue_id)?;
        kwargs.set_item("request_id", &queue_task_id)?;
        kwargs.set_item("task_id", &queue_task_id)?;
        kwargs.set_item("agent_id", &agent_id)?;
        kwargs.set_item("service_kind", prior.get_item("service_kind")?)?;
        kwargs.set_item("requested_minute", &queued_at)?;
        module.getattr("enqueue_facility_queue")?.call((&world,), Some(&kwargs))?;
        let queues = table_of(&world)?.get_item("facility_queues_v1")?;
        let queued = queues.get_item("queues")?.get_item(&queue_id)?.get_item("queued_request_ids")?;
        queued.call_method1("remove", (&queue_task_id,))?;
        queued.call_method1("insert", (0, &queue_task_id))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("transition", "requeued")?;
        kwargs.set_item("task_id", &queue_task_id)?;
        kwargs.set_item("causation_id", attribute(causation_event, intern!(py, "event_id"))?)?;
        module.getattr("record_queue_transition")?.call((&world, queues.get_item("requests")?.get_item(&queue_task_id)?), Some(&kwargs))?;
    }
    manager.call_method1("_clear_queue_stall", (&facility_id,))?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("agent_id", &agent_id)?;
    kwargs.set_item("action", if resource_id == "water_ml" { "fetch_water" } else { "collect_meal" })?;
    kwargs.set_item("outcome", "queued")?;
    kwargs.set_item("reason", "arrived_at_facility")?;
    kwargs.set_item("facility_id", &facility_id)?;
    kwargs.set_item("origin_zone_id", attribute(&agent, intern!(py, "zone_id"))?)?;
    kwargs.set_item("detail", "service_requeued_after_staff_unavailable")?;
    manager.call_method("record_request_outcome", (), Some(&kwargs))?;
    Ok(())
}

// ---- EffectProgramPopulationHandler ------------------------------------------------------------------
//
// The rules themselves are `domain/population.py` and `domain/population_lifecycle.py`, with their own
// kernels (docs/rust-port/lanes/m6-population-rules.md). This is what checks a primitive against its
// approved program and runs it on a copy. A suppressed one swaps its copy in Python too: it is the
// Python rule's, from the start.

const BASE_FIELDS: [&str; 5] = ["program_id", "effect_id", "op", "zone_id", "count"];

fn with_fields(extra: &[&'static str]) -> Vec<&'static str> {
    let mut fields = BASE_FIELDS.to_vec();
    fields.extend_from_slice(extra);
    fields
}

fn population_fields_fit(payload: &Bound<'_, PyAny>, op: &str) -> PyResult<bool> {
    let shapes: Vec<Vec<&'static str>> = match op {
        "add_evacuees" => vec![
            with_fields(&["household_size", "mobility", "language", "admission_zone_ids", "approach_route_id", "person_context_overrides"]),
            with_fields(&["household_size", "mobility", "language", "admission_zone_ids", "approach_route_id"]),
            with_fields(&["household_size", "mobility", "language", "admission_zone_ids"]),
            with_fields(&["household_size", "mobility", "language"]),
        ],
        "depart_evacuees" => vec![with_fields(&["include_initial"]), with_fields(&[])],
        _ => vec![vec!["program_id", "effect_id", "op", "command"]],
    };
    for shape in shapes {
        if fields_are(payload, &shape)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// `EffectProgramPopulationHandler.apply`.
#[pyfunction]
fn population<'py>(_handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    if text_is(&attribute(event, intern!(py, "event_type"))?, "population_approach_complete") {
        py.import("sheltersim.domain.population")?.getattr("apply_arrival_approach_completion")?.call1((world, event))?;
        return counted(py, "population.approach", Ok(()));
    }
    let checked = (|| -> PyResult<_> {
        let payload = attribute(event, intern!(py, "payload"))?;
        let op_value = get(&payload, "op")?;
        let op = exact_str(&op_value)?.to_owned();
        if !matches!(op.as_str(), "add_evacuees" | "depart_evacuees" | "population_presence") || !population_fields_fit(&payload, &op)? {
            return unsupported("the primitive is for the Python rule to judge");
        }
        let state = get(&table_of(world)?, "effect_program_v1")?;
        if !state.is_instance_of::<PyDict>() {
            return unsupported("the primitive is not in a program");
        }
        let programs = state.call_method1(intern!(py, "get"), ("programs", PyDict::new(py)))?;
        let program = programs.call_method1(intern!(py, "get"), (get(&payload, "program_id")?,))?;
        if !program.is_instance_of::<PyDict>() {
            return unsupported("the primitive is not in a program");
        }
        let effect_id = get(&payload, "effect_id")?;
        let operators = program.call_method1(intern!(py, "get"), ("operators", PyDict::new(py)))?;
        if !program.call_method1(intern!(py, "get"), ("effect_ids", PyList::empty(py)))?.contains(&effect_id)?
            || !operators.call_method1(intern!(py, "get"), (&effect_id,))?.eq(&op_value)?
            || !program.call_method1(intern!(py, "get"), ("scheduled_event_ids", PyList::empty(py)))?.contains(attribute(event, intern!(py, "event_id"))?)?
            || (op != "population_presence" && !attribute(world, intern!(py, "zones"))?.contains(get(&payload, "zone_id")?)?)
        {
            return unsupported("the primitive is not in the approved program");
        }
        Ok((payload, op))
    })();
    let (payload, op) = counted(py, "population.checks", refused_by_python(py, checked))?;
    if op == "population_presence" {
        // Past its checks the Python rule lets this rule's exceptions go, as this does.
        py.import("sheltersim.domain.population_lifecycle")?
            .getattr("apply_population_presence_command")?
            .call1((world, event, payload.get_item("command")?))?;
        return Ok(());
    }
    let changed = (|| -> PyResult<_> {
        let candidate = copy_of(world)?;
        let tuple = py.import("builtins")?.getattr("tuple")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("event", event)?;
        kwargs.set_item("zone_id", payload.get_item("zone_id")?)?;
        kwargs.set_item("count", payload.get_item("count")?)?;
        let empty = PyTuple::empty(py);
        let module = py.import("sheltersim.domain.population")?;
        if op == "add_evacuees" {
            for name in ["household_size", "mobility", "language"] {
                kwargs.set_item(name, payload.get_item(name)?)?;
            }
            kwargs.set_item("admission_zone_ids", tuple.call1((payload.call_method1(intern!(py, "get"), ("admission_zone_ids", &empty))?,))?)?;
            kwargs.set_item("approach_route_id", get(&payload, "approach_route_id")?)?;
            kwargs.set_item("person_context_overrides", tuple.call1((payload.call_method1(intern!(py, "get"), ("person_context_overrides", &empty))?,))?)?;
            module.getattr("queue_arrivals")?.call((&candidate,), Some(&kwargs))?;
        } else {
            kwargs.set_item("include_initial", payload.call_method1(intern!(py, "get"), ("include_initial", false))?)?;
            module.getattr("queue_departures")?.call((&candidate,), Some(&kwargs))?;
            py.import("sheltersim.domain.population_lifecycle")?
                .getattr("admit_pending_returns")?
                .call1((&candidate, attribute(event, intern!(py, "due_minute"))?))?;
        }
        candidate.call_method0(intern!(py, "validate"))?;
        py.import("sheltersim.domain.simulator_state")?.getattr("validate_simulator_extensions")?.call1((&candidate,))?;
        swap(world, &candidate)
    })();
    counted(py, "population.copy", refused_by_python(py, changed))
}

// ---- InfrastructureEventHandler --------------------------------------------------------------------

fn infrastructure_state<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let state = get(&table_of(world)?, INFRASTRUCTURE)?;
    if !state.is_instance_of::<PyDict>() {
        return unsupported("no infrastructure model");
    }
    Ok(state)
}

/// `ScenarioInfrastructureSpec.model_validate(configs["infrastructure"])`: the contract stays Python's.
fn infrastructure_config<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let raw = attribute(world, intern!(py, "model_configs"))?.get_item("infrastructure")?;
    py.import("sheltersim.domain.infrastructure")?.getattr("ScenarioInfrastructureSpec")?.call_method1("model_validate", (raw,))
}

/// `dependent_facilities` from the validated config.
fn dependents_of(config: &Bound<'_, PyAny>, component: &str, facilities: &BTreeSet<String>) -> PyResult<Vec<String>> {
    let py = config.py();
    let mut dependents: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for edge in attribute(config, intern!(py, "dependency_edges"))?.try_iter()? {
        let edge = edge?;
        let dependency = attribute(&edge, intern!(py, "dependency_id"))?;
        let dependent = attribute(&edge, intern!(py, "dependent_id"))?;
        dependents.entry(exact_str(&dependency)?.to_owned()).or_default().insert(exact_str(&dependent)?.to_owned());
    }
    let mut reached = BTreeSet::new();
    let mut pending = VecDeque::from([component.to_owned()]);
    let mut visited = BTreeSet::from([component.to_owned()]);
    while let Some(current) = pending.pop_front() {
        if facilities.contains(&current) {
            reached.insert(current.clone());
        }
        if let Some(next) = dependents.get(&current) {
            for dependent in next {
                if facilities.contains(dependent) {
                    reached.insert(dependent.clone());
                }
                if visited.insert(dependent.clone()) {
                    pending.push_back(dependent.clone());
                }
            }
        }
    }
    Ok(reached.into_iter().collect())
}

fn facility_ids(world: &Bound<'_, PyAny>) -> PyResult<BTreeSet<String>> {
    let mut ids = BTreeSet::new();
    for key in attribute(world, intern!(world.py(), "facilities"))?.try_iter()? {
        ids.insert(exact_str(&key?)?.to_owned());
    }
    Ok(ids)
}

/// `effective_facility_capacities`.
fn capacities_of(world: &Bound<'_, PyAny>, state: &Bound<'_, PyAny>, config: &Bound<'_, PyAny>) -> PyResult<BTreeMap<String, i64>> {
    let py = world.py();
    let facilities = facility_ids(world)?;
    let mut forced: BTreeSet<String> = BTreeSet::new();
    let mut lost: BTreeMap<String, i64> = BTreeMap::new();
    for outage in state.get_item("outages")?.call_method0(intern!(py, "values"))?.try_iter()? {
        let outage = outage?;
        if !outage.get_item("active")?.is_truthy()? {
            continue;
        }
        if text_is(&outage.get_item("event_type")?, "power_outage") {
            for id in outage.get_item("affected_facility_ids")?.try_iter()? {
                forced.insert(exact_str(&id?)?.to_owned());
            }
        } else {
            let target = outage.get_item("target_id")?;
            let units = exact_int(&outage.get_item("units_out")?)?;
            *lost.entry(exact_str(&target)?.to_owned()).or_insert(0) += units;
        }
    }
    let mut capacity: Vec<(String, i64)> = Vec::new();
    for item in state.get_item("base_working_units")?.call_method0(intern!(py, "items"))?.try_iter()? {
        let item = item?;
        let id = item.get_item(0)?;
        let id = exact_str(&id)?;
        let base = exact_int(&item.get_item(1)?)?;
        let units = lost.get(id).copied().unwrap_or(0);
        if units > base {
            return unsupported("the failures exceed the installed units");
        }
        capacity.push((id.to_owned(), base - units));
    }
    for (id, units) in &capacity {
        if *units == 0 {
            forced.extend(dependents_of(config, id, &facilities)?);
        }
    }
    Ok(capacity.into_iter().map(|(id, units)| {
        let value = if forced.contains(&id) { 0 } else { units };
        (id, value)
    }).collect())
}

/// `_recalculate_infrastructure_capacity`.
fn recalculate_capacity(world: &Bound<'_, PyAny>, cause: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let state = infrastructure_state(world)?;
    let outage_id = get(&attribute(cause, intern!(py, "payload"))?, "outage_id")?;
    let changed = state.get_item("outages")?.call_method1(intern!(py, "get"), (outage_id,))?;
    if changed.is_none() {
        return unsupported("an unknown outage");
    }
    let mut ids = BTreeSet::new();
    for id in changed.get_item("affected_facility_ids")?.try_iter()? {
        ids.insert(exact_str(&id?)?.to_owned());
    }
    let capacities = capacities_of(world, &state, &infrastructure_config(world)?)?;
    let facilities = attribute(world, intern!(py, "facilities"))?;
    for id in ids {
        let Some(units) = capacities.get(&id) else {
            return unsupported("a facility has no base units");
        };
        facilities.get_item(&id)?.setattr(intern!(py, "working_units"), *units)?;
    }
    Ok(())
}

/// `_schedule_infrastructure_completion`.
fn schedule_completion<'py>(
    world: &Bound<'py, PyAny>,
    cause: &Bound<'py, PyAny>,
    event_type: &str,
    payload: Bound<'py, PyDict>,
    due_minute: i64,
) -> PyResult<Option<String>> {
    let py = world.py();
    if due_minute >= duration_of(world)? {
        return Ok(None);
    }
    let sequence = exact_int(&world.call_method0(intern!(py, "allocate_event_sequence"))?)?;
    let id = format!("{}-{sequence}", event_type.replace('_', "-"));
    let source = attribute(&infrastructure_config(world)?, intern!(py, "source_id"))?;
    let event = new_event(
        py,
        NewEvent {
            event_id: &id,
            kind: "COMPLETION",
            due_minute,
            priority: 10,
            sequence,
            event_type,
            payload: payload.into_any(),
            source_id: Some(attribute(cause, intern!(py, "source_id"))?),
            causation_id: Some(attribute(cause, intern!(py, "event_id"))?),
            provenance: assumption(py, &source, INFRASTRUCTURE_POLICY)?.into_any(),
        },
    )?;
    ensure_identity_free(world, &id, sequence)?;
    world.call_method1(intern!(py, "schedule_event"), (event,))?;
    Ok(Some(id))
}

fn until_of(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<i64> {
    let py = world.py();
    let until = exact_int(&get(&attribute(event, intern!(py, "payload"))?, "until_minute")?)?;
    if until <= exact_int(&attribute(event, intern!(py, "due_minute"))?)? || until > duration_of(world)? {
        return unsupported("the duration is invalid");
    }
    Ok(until)
}

fn start_outage(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let state = infrastructure_state(world)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let event_id = attribute(event, intern!(py, "event_id"))?;
    let event_type = attribute(event, intern!(py, "event_type"))?;
    let outage_id = get(&payload, "outage_id")?;
    let record = state.get_item("outages")?.call_method1(intern!(py, "get"), (&outage_id,))?;
    if record.is_none() || !record.get_item("start_event_id")?.eq(&event_id)? || !record.get_item("event_type")?.eq(&event_type)? {
        return unsupported("the outage does not match its state");
    }
    require_source(event)?;
    if record.get_item("active")?.is_truthy()? {
        return unsupported("the outage is already active");
    }
    let power = text_is(&event_type, "power_outage");
    let target = get(&payload, if power { "component_id" } else { "facility_id" })?;
    let right_fields =
        if power { fields_are(&payload, &["outage_id", "component_id", "until_minute"])? } else { fields_are(&payload, &["outage_id", "facility_id", "units_out", "until_minute"])? };
    if !right_fields {
        return unsupported("the payload has other fields");
    }
    let until = until_of(world, event)?;
    let config = infrastructure_config(world)?;
    let affected = dependents_of(&config, exact_str(&target)?, &facility_ids(world)?)?;
    if affected.is_empty() || !record.get_item("affected_facility_ids")?.eq(PyList::new(py, &affected)?)? {
        return unsupported("the outage has no valid affected facilities");
    }
    for id in &affected {
        if has_active_modifier(world, PyString::new(py, id).as_any())? {
            return unsupported("the outage conflicts with a modifier");
        }
    }
    record.set_item("active", true)?;
    record.set_item("until_minute", until)?;
    record.set_item("units_out", if power { py.None().into_bound(py) } else { payload.get_item("units_out")? })?;
    let completion = PyDict::new(py);
    completion.set_item("outage_id", payload.get_item("outage_id")?)?;
    let restore = schedule_completion(world, event, if power { "power_restored" } else { "equipment_repaired" }, completion, until)?;
    record.set_item("restore_event_id", restore)?;
    recalculate_capacity(world, event)
}

fn restore_outage(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let state = infrastructure_state(world)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let record = state.get_item("outages")?.call_method1(intern!(py, "get"), (get(&payload, "outage_id")?,))?;
    if !fields_are(&payload, &["outage_id"])?
        || record.is_none()
        || !record.get_item("active")?.is_truthy()?
        || !record.get_item("restore_event_id")?.eq(attribute(event, intern!(py, "event_id"))?)?
        || !record.get_item("start_event_id")?.eq(attribute(event, intern!(py, "causation_id"))?)?
    {
        return unsupported("the restoration does not match an active outage");
    }
    require_source(event)?;
    let expected = if text_is(&record.get_item("event_type")?, "power_outage") { "power_restored" } else { "equipment_repaired" };
    if !text_is(&attribute(event, intern!(py, "event_type"))?, expected) {
        return unsupported("the restoration's type does not match its cause");
    }
    record.set_item("active", false)?;
    recalculate_capacity(world, event)
}

fn start_stressor(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let state = infrastructure_state(world)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let stressor_id = get(&payload, "stressor_id")?;
    let stressors = state.get_item("environmental_stressors")?;
    let record = stressors.call_method1(intern!(py, "get"), (&stressor_id,))?;
    if !fields_are(&payload, &["stressor_id", "zone_id", "until_minute"])?
        || record.is_none()
        || !record.get_item("start_event_id")?.eq(attribute(event, intern!(py, "event_id"))?)?
        || record.get_item("active")?.is_truthy()?
        || !attribute(world, intern!(py, "zones"))?.contains(get(&payload, "zone_id")?)?
    {
        return unsupported("the stressor is for the Python rule to judge");
    }
    require_source(event)?;
    let until = until_of(world, event)?;
    let zone = record.get_item("zone_id")?;
    let mut active = 0;
    for other in stressors.call_method0(intern!(py, "values"))?.try_iter()? {
        let other = other?;
        if other.get_item("active")?.is_truthy()? && other.get_item("zone_id")?.eq(&zone)? {
            active += 1;
        }
    }
    if active >= 16 {
        return unsupported("the zone's stressor limit was reached");
    }
    record.set_item("active", true)?;
    let completion = PyDict::new(py);
    completion.set_item("stressor_id", &stressor_id)?;
    completion.set_item("zone_id", &zone)?;
    let end = schedule_completion(world, event, "environmental_stressor_end", completion, until)?;
    record.set_item("end_event_id", end)?;
    record.set_item("until_minute", until)?;
    Ok(())
}

fn end_stressor(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let state = infrastructure_state(world)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let record = state.get_item("environmental_stressors")?.call_method1(intern!(py, "get"), (get(&payload, "stressor_id")?,))?;
    if !fields_are(&payload, &["stressor_id", "zone_id"])?
        || record.is_none()
        || !record.get_item("active")?.is_truthy()?
        || !record.get_item("end_event_id")?.eq(attribute(event, intern!(py, "event_id"))?)?
        || !record.get_item("start_event_id")?.eq(attribute(event, intern!(py, "causation_id"))?)?
        || !record.get_item("zone_id")?.eq(get(&payload, "zone_id")?)?
    {
        return unsupported("the end does not match an active stressor");
    }
    require_source(event)?;
    record.set_item("active", false)?;
    Ok(())
}

/// `_interrupt_route`: the records of the deliveries it delays.
fn interrupt_route<'py>(world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py = world.py();
    let state = infrastructure_state(world)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let outage_id = get(&payload, "outage_id")?;
    let interruptions = state.get_item("route_interruptions")?;
    let record = interruptions.call_method1(intern!(py, "get"), (&outage_id,))?;
    if !fields_are(&payload, &["outage_id", "route_id", "shipment_ids", "until_minute"])?
        || record.is_none()
        || !record.get_item("start_event_id")?.eq(attribute(event, intern!(py, "event_id"))?)?
        || record.get_item("active")?.is_truthy()?
    {
        return unsupported("the interruption is for the Python rule to judge");
    }
    let until = until_of(world, event)?;
    require_source(event)?;
    let route_id = record.get_item("route_id")?;
    for other in interruptions.call_method0(intern!(py, "values"))?.try_iter()? {
        let other = other?;
        if other.get_item("active")?.is_truthy()? && other.get_item("route_id")?.eq(&route_id)? {
            return unsupported("the route already has an interruption");
        }
    }
    let operational = get(&table_of(world)?, OPERATIONAL)?;
    if operational.is_none() {
        return unsupported("no shipment records");
    }
    let due = exact_int(&attribute(event, intern!(py, "due_minute"))?)?;
    let delay = until - due;
    let duration = duration_of(world)?;
    let mut delays: Vec<(Bound<'py, PyAny>, i64)> = Vec::new();
    for shipment_id in record.get_item("shipment_ids")?.try_iter()? {
        let shipment_id = shipment_id?;
        let shipment = operational.get_item("shipments")?.call_method1(intern!(py, "get"), (&shipment_id,))?;
        if shipment.is_none() {
            return unsupported("the shipment cannot be delayed");
        }
        let wanted = shipment.get_item("event_id")?;
        let mut delivery = None;
        for pending in pending_of(world)? {
            if attribute(&pending, intern!(py, "event_id"))?.eq(&wanted)? {
                delivery = Some(pending);
                break;
            }
        }
        let Some(delivery) = delivery else {
            return unsupported("the shipment cannot be delayed");
        };
        let delivery_due = exact_int(&attribute(&delivery, intern!(py, "due_minute"))?)?;
        if !text_is(&attribute(&delivery, intern!(py, "event_type"))?, "resource_delivery")
            || delivery_due <= due
            || !shipment.get_item("due_minute")?.eq(delivery_due)?
            || delivery_due + delay >= duration
        {
            return unsupported("the shipment cannot be delayed");
        }
        delays.push((shipment_id, delivery_due + delay));
    }
    let mut delayed = Vec::with_capacity(delays.len());
    let source = attribute(&infrastructure_config(world)?, intern!(py, "source_id"))?;
    for (shipment_id, new_due) in delays {
        let sequence = exact_int(&world.call_method0(intern!(py, "allocate_event_sequence"))?)?;
        let id = format!("route-delay-{sequence}");
        let delay_payload = PyDict::new(py);
        delay_payload.set_item("shipment_id", shipment_id)?;
        delay_payload.set_item("to_minute", new_due)?;
        let provenance = assumption(py, &source, INFRASTRUCTURE_POLICY)?;
        provenance.set_item("route_id", &route_id)?;
        let delay_event = new_event(
            py,
            NewEvent {
                event_id: &id,
                kind: "COMPLETION",
                due_minute: due,
                priority: -10,
                sequence,
                event_type: "delivery_delayed",
                payload: delay_payload.into_any(),
                source_id: Some(attribute(event, intern!(py, "source_id"))?),
                causation_id: Some(attribute(event, intern!(py, "event_id"))?),
                provenance: provenance.into_any(),
            },
        )?;
        ensure_identity_free(world, &id, sequence)?;
        world.call_method1(intern!(py, "schedule_event"), (&delay_event,))?;
        // This swaps a copy of the copy in, so what follows reads the state again.
        delivery_delay_on(world, &delay_event)?;
        attribute(world, intern!(py, "event_queue"))?.call_method1(intern!(py, "remove"), (&id,))?;
        delayed.push(log_record(py, &delay_event, &attribute(world, intern!(py, "minute_index"))?, "APPLIED", None)?);
    }
    let record = table_of(world)?.get_item(INFRASTRUCTURE)?.get_item("route_interruptions")?.get_item(&outage_id)?;
    record.set_item("active", true)?;
    record.set_item("until_minute", until)?;
    let completion = PyDict::new(py);
    completion.set_item("outage_id", &outage_id)?;
    completion.set_item("route_id", &route_id)?;
    let restore = schedule_completion(world, event, "supply_route_restored", completion, until)?;
    record.set_item("restore_event_id", restore)?;
    Ok(delayed)
}

fn restore_route(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let state = infrastructure_state(world)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let record = state.get_item("route_interruptions")?.call_method1(intern!(py, "get"), (get(&payload, "outage_id")?,))?;
    if !fields_are(&payload, &["outage_id", "route_id"])?
        || record.is_none()
        || !record.get_item("active")?.is_truthy()?
        || !record.get_item("restore_event_id")?.eq(attribute(event, intern!(py, "event_id"))?)?
        || !record.get_item("start_event_id")?.eq(attribute(event, intern!(py, "causation_id"))?)?
        || !record.get_item("route_id")?.eq(get(&payload, "route_id")?)?
    {
        return unsupported("the restoration does not match an active interruption");
    }
    require_source(event)?;
    record.set_item("active", false)?;
    Ok(())
}

/// The first of the payload's IDs that is a non-empty str, or the event's.
fn component_of(event: &Bound<'_, PyAny>) -> PyResult<String> {
    let py = event.py();
    let payload = attribute(event, intern!(py, "payload"))?;
    for name in ["component_id", "outage_id", "facility_id"] {
        let value = get(&payload, name)?;
        if value.is_instance_of::<PyString>() {
            let text = exact_str(&value)?;
            if !text.is_empty() {
                return Ok(text.to_owned());
            }
        }
    }
    Ok(exact_str(&attribute(event, intern!(py, "event_id"))?)?.to_owned())
}

/// `InfrastructureEventHandler.apply`.
#[pyfunction]
fn infrastructure<'py>(_handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let py = world.py();
    let event_type = attribute(event, intern!(py, "event_type"))?;
    let (label, kind): (&'static str, u8) = match exact_str(&event_type)? {
        "power_outage" | "equipment_failure" => ("infrastructure.start_outage", 0),
        "power_restored" | "equipment_repaired" => ("infrastructure.restore_outage", 1),
        "extreme_heat" | "extreme_cold" => ("infrastructure.start_stressor", 2),
        "environmental_stressor_end" => ("infrastructure.end_stressor", 3),
        "supply_route_interruption" => ("infrastructure.interrupt_route", 4),
        "supply_route_restored" => ("infrastructure.restore_route", 5),
        _ => return counted(py, "infrastructure", unsupported("an unknown event type")),
    };
    let swapped = (|| -> PyResult<(Vec<(String, i64)>, Vec<Bound<'py, PyAny>>, String, Vec<String>)> {
        let mut previous = Vec::new();
        for item in attribute(world, intern!(py, "facilities"))?.call_method0(intern!(py, "items"))?.try_iter()? {
            let item = item?;
            let id = item.get_item(0)?;
            previous.push((exact_str(&id)?.to_owned(), exact_int(&attribute(&item.get_item(1)?, intern!(py, "working_units"))?)?));
        }
        let component = component_of(event)?;
        let mut sources = Vec::new();
        for value in [attribute(event, intern!(py, "source_id"))?, get(&attribute(event, intern!(py, "provenance"))?, "source_id")?] {
            if value.is_instance_of::<PyString>() {
                let text = exact_str(&value)?;
                if !text.is_empty() {
                    sources.push(text.to_owned());
                }
            }
        }
        sources.sort_unstable();
        sources.dedup();
        let mut additional = Vec::new();
        transaction(world, event, |candidate| {
            match kind {
                0 => start_outage(candidate, event)?,
                1 => restore_outage(candidate, event)?,
                2 => start_stressor(candidate, event)?,
                3 => end_stressor(candidate, event)?,
                4 => additional = interrupt_route(candidate, event)?,
                _ => restore_route(candidate, event)?,
            }
            Ok(Vec::new())
        })?;
        Ok((previous, additional, component, sources))
    })();
    let (previous, mut additional, component, sources) = counted(py, label, refused_by_python(py, swapped))?;

    // The world has changed: what fails from here fails in Python too.
    let mut sorted_previous = previous;
    sorted_previous.sort_unstable();
    let facilities = attribute(world, intern!(py, "facilities"))?;
    let event_id = attribute(event, intern!(py, "event_id"))?;
    let mut current_ids: Vec<String> = Vec::new();
    for key in facilities.try_iter()? {
        current_ids.push(key?.extract()?);
    }
    current_ids.sort_unstable();
    for id in current_ids {
        let Ok(index) = sorted_previous.binary_search_by(|(known, _)| known.as_str().cmp(&id)) else {
            continue;
        };
        let was = sorted_previous[index].1;
        let units: i64 = facilities.get_item(&id)?.getattr(intern!(py, "working_units"))?.extract()?;
        if units == was {
            continue;
        }
        let status = if units < was { "degraded" } else { "restored" };
        if let Some(record) = facility_recovery_record(world, &id, &component, status, was, units, event_id.extract::<String>()?.as_str(), sources.clone())? {
            additional.push(record);
        }
    }
    records_tuple(py, additional)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    m.add_function(wrap_pyfunction!(proximity, m)?)?;
    m.add_function(wrap_pyfunction!(resource_ledger, m)?)?;
    m.add_function(wrap_pyfunction!(facility_outage, m)?)?;
    m.add_function(wrap_pyfunction!(facility_recovery, m)?)?;
    m.add_function(wrap_pyfunction!(delivery_delay, m)?)?;
    m.add_function(wrap_pyfunction!(water_outage, m)?)?;
    m.add_function(wrap_pyfunction!(water_restoration, m)?)?;
    m.add_function(wrap_pyfunction!(staff_unavailable, m)?)?;
    m.add_function(wrap_pyfunction!(staff_restoration, m)?)?;
    m.add_function(wrap_pyfunction!(infrastructure, m)?)?;
    m.add_function(wrap_pyfunction!(population, m)?)?;
    m.add_function(wrap_pyfunction!(cancel, m)?)?;
    m.add_function(wrap_pyfunction!(service_cancellation, m)?)?;
    m.add_function(wrap_pyfunction!(interrupt_for_staff, m)?)?;
    Ok(())
}
