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

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyList, PyMapping, PyString, PyTuple};

use crate::core::mathx::{hypot, py_is_close};
use crate::core::errors;
use crate::core::row::{attribute, exact_int, exact_str, str_value, unsupported};
use crate::rules::care_receipts::{continuity_record, facility_recovery_record};
use crate::rules::effects::{active_outage, copy_of, has_active_modifier, refused_by_python, swap};
use crate::rules::population as population_rules;
use crate::rules::{population_lifecycle, service};
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
    str_value(value) == Some(wanted)
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
        if attribute(&current, intern!(py, "event_id"))?.eq(event_id)? || attribute(&current, intern!(py, "sequence"))?.eq(sequence)? {
            return errors::failed(py, format!("generated event identity {} is not unique", errors::repr_str(py, event_id)?));
        }
    }
    let log = attribute(world, intern!(py, "event_log"))?;
    if log.call_method1(intern!(py, "contains_id"), (event_id,))?.is_truthy()?
        || log.call_method1(intern!(py, "contains_sequence"), (sequence,))?.is_truthy()?
    {
        return errors::failed(py, format!("generated event identity {} is not unique", errors::repr_str(py, event_id)?));
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
                    crate::rules::common_operations::record_shared_operation(&candidate, &receipt)?;
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
    let provenance = attribute(event, intern!(py, "provenance"))?;
    let due_value = attribute(event, intern!(py, "due_minute"))?;
    if config.is_none()
        || !kind_is(event, "ENDOGENOUS")?
        || due_value.ne(attribute(world, intern!(py, "minute_index"))?)?
        || attribute(event, intern!(py, "source_id"))?.ne(PROXIMITY_SOURCE)?
        || !fields_are(&provenance, &["source_type", "source_id", "policy_version", "observed_minute"])?
        || get(&provenance, "source_type")?.ne("assumption")?
        || get(&provenance, "source_id")?.ne(PROXIMITY_SOURCE)?
        || get(&provenance, "policy_version")?.ne(config.get_item("policy_version")?)?
    {
        return errors::failed(py, "proximity contact provenance or time is invalid");
    }
    let due = exact_int(&due_value)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    if !fields_are(
        &payload,
        &["first_agent_id", "second_agent_id", "zone_id", "observed_minute", "contact_minutes", "distance_m", "first_point", "second_point"],
    )? {
        return errors::failed(py, "proximity contact payload fields do not match");
    }
    let first_value = payload.get_item("first_agent_id")?;
    let second_value = payload.get_item("second_agent_id")?;
    let zone_value = payload.get_item("zone_id")?;
    let distance_value = payload.get_item("distance_m")?;
    let agents = attribute(world, intern!(py, "agents"))?;
    let zones = attribute(world, intern!(py, "zones"))?;
    // `or` in the Python rule's order: each test is reached only if those before it passed.
    let invalid = (|| -> PyResult<Option<(&str, &str, f64)>> {
        let (Some(first_id), Some(second_id)) = (errors::nonempty_str(&first_value)?, errors::nonempty_str(&second_value)?) else {
            return Ok(None);
        };
        if first_id == second_id || !agents.contains(first_id)? || !agents.contains(second_id)? {
            return Ok(None);
        }
        if errors::str_of(&zone_value)?.is_none() || !zones.contains(&zone_value)? {
            return Ok(None);
        }
        if errors::exactly_int(&payload.get_item("observed_minute")?)? != Some(due)
            || errors::exactly_int(&get(&provenance, "observed_minute")?)? != Some(due)
            || errors::exactly_int(&payload.get_item("contact_minutes")?)? != Some(0)
        {
            return Ok(None);
        }
        let Some(distance) = errors::real_number(&distance_value)? else {
            return Ok(None);
        };
        if !distance.is_finite() || distance < 0.0 || distance > number(&config.get_item("max_distance_m")?)? {
            return Ok(None);
        }
        Ok(Some((first_id, second_id, distance)))
    })()?;
    let Some((first_id, second_id, distance)) = invalid else {
        return errors::failed(py, "proximity contact participants or measurement are invalid");
    };
    let first_point = payload.get_item("first_point")?;
    let second_point = payload.get_item("second_point")?;
    let mut coordinates = [0.0_f64; 4];
    for (index, point) in [&first_point, &second_point].into_iter().enumerate() {
        if !is_mapping(point) || !fields_are(point, &["x", "y"])? {
            return errors::failed(py, "proximity contact coordinates are invalid");
        }
        let (Some(x), Some(y)) = (errors::real_number(&point.get_item("x")?)?, errors::real_number(&point.get_item("y")?)?) else {
            return errors::failed(py, "proximity contact coordinates are invalid");
        };
        if !x.is_finite() || !y.is_finite() {
            return errors::failed(py, "proximity contact coordinates are invalid");
        }
        coordinates[index * 2] = x;
        coordinates[index * 2 + 1] = y;
    }
    let measured = hypot(&[coordinates[2] - coordinates[0], coordinates[3] - coordinates[1]]);
    if !py_is_close(distance, measured) && (measured - distance).abs() > 1e-9 {
        return errors::failed(py, "proximity contact distance does not match coordinates");
    }
    let movement = get(&table_of(world)?, MOVEMENT)?;
    let positions = if movement.is_instance_of::<PyDict>() {
        movement.call_method1(intern!(py, "get"), ("positions", PyDict::new(py)))?
    } else {
        PyDict::new(py).into_any()
    };
    for (id, point) in [(first_id, &first_point), (second_id, &second_point)] {
        let position = if positions.is_instance_of::<PyDict>() { get(&positions, id)? } else { py.None().into_bound(py) };
        if !position.is_instance_of::<PyDict>()
            || get(&position, "position_kind")?.ne("physical")?
            || get(&position, "zone_id")?.ne(&zone_value)?
            || get(&position, "point")?.ne(point)?
        {
            return errors::suppressed(py, "proximity contact sample is stale");
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
        let shipment = if operational.is_truthy()? {
            let shipments = operational.call_method1(intern!(py, "get"), ("shipments", PyDict::new(py)))?;
            shipments.call_method1(intern!(py, "get"), (payload.get_item("shipment_id")?,))?
        } else {
            py.None().into_bound(py)
        };
        if !fields_are(&payload, &["warehouse_id", "resource_id", "quantity", "shipment_id"])?
            || !shipment.is_instance_of::<PyDict>()
            || !get(&shipment, "event_id")?.eq(attribute(event, intern!(py, "event_id"))?)?
            || !get(&shipment, "resource_id")?.eq(payload.get_item("resource_id")?)?
            || !get(&shipment, "due_minute")?.eq(attribute(event, intern!(py, "due_minute"))?)?
        {
            return errors::failed(py, "shipment delivery does not match operational state");
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
            && str_value(&reservation).is_some()
            && service.get_item("active_reservation_ids")?.contains(&reservation)?
        {
            return errors::failed(
                py,
                "service-owned resource reservations must be completed or cancelled by ResourceServiceManager",
            );
        }
        if operation == "reserve" {
            for task in attribute(world, intern!(py, "active_tasks"))?.call_method0(intern!(py, "values"))?.try_iter()? {
                let task = task?;
                if attribute(&task, intern!(py, "id"))?.eq(&reservation)?
                    && text_is(&attribute(&task, intern!(py, "kind"))?, "resource_service_queue")
                {
                    return errors::failed(py, "resource reservation ID conflicts with a queued resource service task");
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
        match crate::rules::resources::apply_operation(world, &ledger_event, &operation) {
            // What the kernel leaves, the ledger's rule decides (`apply_resource_operation`, which
            // changes nothing when it turns the operation down) and says why. The handler's
            // `except ResourceLedgerError as exc: raise EventApplicationError(str(exc))` is here.
            Err(error) if error.is_instance_of::<Unsupported>(py) => {
                let resources = py.import(intern!(py, "sheltersim.domain.resources"))?;
                match resources.getattr(intern!(py, "apply_resource_operation"))?.call1((world, &ledger_event, &operation)) {
                    Ok(_) => Ok(()),
                    Err(error) if error.is_instance(py, &resources.getattr(intern!(py, "ResourceLedgerError"))?) => {
                        errors::failed(py, error.value(py).str()?.to_str()?.to_owned())
                    }
                    Err(error) => Err(error),
                }
            }
            done => done,
        }
    })();
    counted(py, "resource_ledger", result)
}

// ---- Facility outages and recoveries ---------------------------------------------------------------

/// `FacilityOutageHandler.apply` and `FacilityRecoveryHandler.apply`: `units` of a sign. The checks and
/// their texts are the Python rule's, in its order.
fn facility_units<'py>(world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>, field: &str, restores: bool) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let py = world.py();
    let what = if restores { "facility recovery" } else { "facility outage" };
    let plan = (|| -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>, i64, i64)> {
        let payload = attribute(event, intern!(py, "payload"))?;
        if !fields_are(&payload, &["facility_id", field])? {
            return errors::failed(py, format!("{what} payload fields do not match"));
        }
        let facility_id = payload.get_item("facility_id")?;
        let Some(id) = errors::nonempty_str(&facility_id)? else {
            return errors::failed(py, format!("{what} requires a facility_id"));
        };
        let units = errors::int_not_bool(&payload.get_item(field)?)?;
        let Some(units) = units else {
            return errors::failed(py, format!("{what} {field} must be an integer"));
        };
        if units < 0 {
            return errors::failed(py, format!("{what} {field} cannot be negative"));
        }
        let facility = get(&attribute(world, intern!(py, "facilities"))?, id)?;
        if facility.is_none() {
            return errors::failed(py, format!("facility {} does not exist", errors::repr(&facility_id)?));
        }
        if active_outage(world, id)? {
            return errors::failed(py, format!("{what} conflicts with an active infrastructure outage"));
        }
        if has_active_modifier(world, &facility_id)? {
            return errors::failed(py, format!("{what} conflicts with an active EffectProgram availability modifier"));
        }
        if units == 0 {
            return errors::suppressed(py, format!("zero-unit {what} has no effect"));
        }
        let previous = exact_int(&attribute(&facility, intern!(py, "working_units"))?)?;
        let next = if restores { previous.checked_add(units) } else { previous.checked_sub(units) };
        let Some(next) = next else {
            return unsupported("the units are past 64 bits");
        };
        if !restores && next < 0 {
            return errors::failed(py, "facility outage exceeds currently working capacity");
        }
        if restores && next > exact_int(&attribute(&facility, intern!(py, "capacity"))?)? {
            return errors::failed(py, "facility recovery exceeds installed capacity");
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
        if !fields_are(&payload, &["shipment_id", "to_minute"])? || attribute(event, intern!(py, "source_id"))?.is_none() {
            return errors::failed(py, "delivery delay payload fields or source are invalid");
        }
        let operational = get(&table_of(candidate)?, OPERATIONAL)?;
        if operational.is_none() {
            return errors::failed(py, "delivery delay requires operational_events_v1 state");
        }
        let shipment_id = payload.get_item("shipment_id")?;
        let record = operational.get_item("shipments")?.call_method1(intern!(py, "get"), (&shipment_id,))?;
        if record.is_none() {
            return errors::failed(py, "delivery delay references an unknown shipment");
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
            return errors::failed(py, "shipment is no longer pending for delay");
        };
        let target_payload = attribute(&target, intern!(py, "payload"))?;
        let target_due_value = attribute(&target, intern!(py, "due_minute"))?;
        if attribute(&target, intern!(py, "event_type"))?.ne("resource_delivery")?
            || get(&target_payload, "shipment_id")?.ne(&shipment_id)?
            || target_due_value.ne(record.get_item("due_minute")?)?
        {
            return errors::failed(py, "shipment is no longer pending for delay");
        }
        let target_due = exact_int(&target_due_value)?;
        let now = minute_of(candidate)?;
        let to_minute = match errors::int_not_bool(&payload.get_item("to_minute")?)? {
            Some(to_minute) if !(target_due <= now || to_minute <= target_due || to_minute <= now || to_minute >= duration_of(candidate)?) => to_minute,
            _ => return errors::failed(py, "shipment delay must move a not-yet-due delivery forward"),
        };
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
        let is_zone = fields_are(&payload, &["outage_id", "zone_id"])?;
        if attribute(event, intern!(py, "source_id"))?.is_none() || (!is_zone && !fields_are(&payload, &["outage_id", "facility_id"])?) {
            return errors::failed(py, "water outage payload fields or source are invalid");
        }
        let operational = get(&table_of(candidate)?, OPERATIONAL)?;
        let record = if operational.is_truthy()? {
            operational.get_item("water_outages")?.call_method1(intern!(py, "get"), (payload.get_item("outage_id")?,))?
        } else {
            py.None().into_bound(py)
        };
        if record.is_none() || record.get_item("outage_event_id")?.ne(attribute(event, intern!(py, "event_id"))?)? {
            return errors::failed(py, "water outage does not match operational state");
        }
        let target_id = payload.get_item(if is_zone { "zone_id" } else { "facility_id" })?;
        if record.get_item("target_kind")?.ne(if is_zone { "zone" } else { "facility" })? || record.get_item("target_id")?.ne(&target_id)? {
            return errors::failed(py, "water outage target does not match operational state");
        }
        let facilities = attribute(candidate, intern!(py, "facilities"))?;
        if is_zone {
            let mut found = false;
            for facility in facilities.call_method0(intern!(py, "values"))?.try_iter()? {
                let facility = facility?;
                if attribute(&facility, intern!(py, "kind"))?.eq("water_station")?
                    && attribute(&facility, intern!(py, "zone_id"))?.eq(&target_id)?
                {
                    found = true;
                    break;
                }
            }
            if !found {
                return errors::failed(py, "water outage zone has no water_station");
            }
        } else {
            let facility = facilities.call_method1(intern!(py, "get"), (&target_id,))?;
            if facility.is_none() || attribute(&facility, intern!(py, "kind"))?.ne("water_station")? {
                return errors::failed(py, "water outage must target a water_station");
            }
        }
        if record.get_item("active")?.is_truthy()? {
            return errors::suppressed(py, "water outage is already active");
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
        if attribute(event, intern!(py, "source_id"))?.is_none() || !fields_are(&payload, &["outage_id"])? {
            return errors::failed(py, "water restoration payload fields or source are invalid");
        }
        let operational = get(&table_of(candidate)?, OPERATIONAL)?;
        let record = if operational.is_truthy()? {
            operational.get_item("water_outages")?.call_method1(intern!(py, "get"), (payload.get_item("outage_id")?,))?
        } else {
            py.None().into_bound(py)
        };
        if record.is_none() || record.get_item("restore_event_id")?.ne(attribute(event, intern!(py, "event_id"))?)? {
            return errors::failed(py, "water restoration does not match its outage");
        }
        if !record.get_item("active")?.is_truthy()? {
            return errors::suppressed(py, "water outage is not active");
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
        if attribute(event, intern!(py, "source_id"))?.is_none() || !fields_are(&payload, &["staff_id", "until_minute"])? {
            return errors::failed(py, "staff unavailability payload fields or source are invalid");
        }
        let table = table_of(candidate)?;
        let operational = get(&table, OPERATIONAL)?;
        if operational.is_none() {
            return errors::failed(py, "staff unavailability requires operational event state");
        }
        let staff_value = payload.get_item("staff_id")?;
        let staff_id = exact_str(&staff_value)?;
        let staff_member = get(&attribute(candidate, intern!(py, "staff"))?, staff_id)?;
        if staff_member.is_none() {
            return errors::failed(py, "staff unavailability references unknown staff");
        }
        let duration = duration_of(candidate)?;
        let until = match errors::int_not_bool(&payload.get_item("until_minute")?)? {
            Some(until) if until > minute_of(candidate)? && until <= duration => until,
            _ => return errors::failed(py, "staff unavailable until_minute is invalid"),
        };
        let status = operational.get_item("staff_status")?.get_item(staff_id)?;
        let event_id = attribute(event, intern!(py, "event_id"))?;
        if status.get_item("unavailability_event_id")?.eq(&event_id)? {
            return errors::suppressed(py, "staff unavailability was already applied");
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
            if candidate.call_method0(intern!(py, "allocate_event_sequence"))?.ne(sequence)? {
                return errors::failed(py, "event sequence changed during staff preflight");
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
            return errors::failed(py, "staff owns multiple active service tasks");
        }
        if resource_tasks > 0 {
            counted(py, "interrupt_for_staff", service::interrupt_service_of_world(candidate, PyString::new(py, staff_id).as_any(), event))?;
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
        if attribute(event, intern!(py, "source_id"))?.is_none() || !fields_are(&payload, &["staff_id", "unavailability_event_id"])? {
            return errors::failed(py, "staff restoration payload fields or source are invalid");
        }
        let operational = get(&table_of(candidate)?, OPERATIONAL)?;
        let record = if operational.is_truthy()? {
            operational.get_item("staff_status")?.call_method1(intern!(py, "get"), (payload.get_item("staff_id")?,))?
        } else {
            py.None().into_bound(py)
        };
        if record.is_none() {
            return errors::failed(py, "staff restoration references unknown staff");
        }
        if record.get_item("unavailability_event_id")?.ne(payload.get_item("unavailability_event_id")?)? {
            return errors::suppressed(py, "staff restoration belongs to an older unavailability");
        }
        if record.get_item("restore_event_id")?.ne(attribute(event, intern!(py, "event_id"))?)? {
            return errors::failed(py, "staff restoration event ID does not match its token");
        }
        if record.get_item("available")?.is_truthy()? {
            return errors::suppressed(py, "staff is already available");
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
        return errors::failed(py, "interrupted support task has inconsistent ownership");
    };
    let task_agent = attribute(task, intern!(py, "agent_id"))?;
    let agents = attribute(world, intern!(py, "agents"))?;
    let agent = agents.call_method1(intern!(py, "get"), (if task_agent.is_none() { PyString::new(py, "").into_any() } else { task_agent },))?;
    if agent.is_none() {
        return errors::failed(py, "interrupted support task has inconsistent ownership");
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
        return errors::failed(py, "interrupted support task has inconsistent ownership");
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
        return errors::failed(py, "support timeout state is inconsistent");
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
        if world.call_method0(intern!(py, "allocate_event_sequence"))?.ne(sequence)? {
            return errors::failed(py, "event sequence changed during support preflight");
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
        return errors::failed(py, "resident has duplicate trips to assigned support");
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
            return errors::failed(py, "resident support trip has inconsistent travel ownership");
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
        crate::rules::movement::finish_movement(world, &agent_id)?;
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
// The rules are `rules/service.rs`.

/// `ResourceServiceManager.cancel`, counted.
#[pyfunction]
fn cancel<'py>(manager: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    counted(manager.py(), "resource_service_cancel", service::cancel_service(manager, event))
}

/// `ResourceServiceCancellationHandler.apply`.
#[pyfunction]
fn service_cancellation<'py>(handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    if attribute(world, intern!(py, "schema_version"))?.ne(6)? {
        return errors::failed(py, "resource service cancellation requires WorldState v6");
    }
    attribute(handler, intern!(py, "service"))?.call_method1(intern!(py, "cancel"), (event,))?;
    Ok(())
}

/// `ResourceServiceManager.interrupt_for_staff`.
#[pyfunction]
#[pyo3(signature = (manager, staff_id, *, causation_event))]
fn interrupt_for_staff<'py>(manager: &Bound<'py, PyAny>, staff_id: &Bound<'py, PyAny>, causation_event: &Bound<'py, PyAny>) -> PyResult<()> {
    counted(manager.py(), "interrupt_for_staff", service::interrupt_service(manager, staff_id, causation_event))
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

/// `except (TypeError, ValueError) as exc: raise EventApplicationError(str(exc))` of the rule, for an
/// exception of a Python call made where the rule makes it. Any other error goes on as it is.
fn wrapped(py: Python<'_>, error: PyErr) -> PyErr {
    if error.is_instance_of::<Unsupported>(py) || errors::made_here(py, &error) || !(error.is_instance_of::<PyTypeError>(py) || error.is_instance_of::<PyValueError>(py)) {
        return error;
    }
    match error.value(py).str() {
        Ok(text) => errors::error(py, errors::Outcome::Failed, text.to_string_lossy().into_owned()),
        Err(failure) => failure,
    }
}

/// `EffectProgramPopulationHandler.apply`.
#[pyfunction]
fn population<'py>(_handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    if text_is(&attribute(event, intern!(py, "event_type"))?, "population_approach_complete") {
        return counted(py, "population.approach", population_rules::apply_arrival_approach_completion(world, event));
    }
    let checked = (|| -> PyResult<_> {
        let payload = attribute(event, intern!(py, "payload"))?;
        let op_value = get(&payload, "op")?;
        let op = match errors::str_of(&op_value)? {
            Some(op @ ("add_evacuees" | "depart_evacuees" | "population_presence")) => op.to_owned(),
            _ => return errors::failed(py, "population primitive operation is invalid"),
        };
        if !population_fields_fit(&payload, &op)? {
            return errors::failed(py, "population primitive payload fields do not match");
        }
        let not_approved = || errors::failed(py, "population primitive is not in the approved program");
        let state = get(&table_of(world)?, "effect_program_v1")?;
        if !state.is_instance_of::<PyDict>() {
            return not_approved();
        }
        let programs = state.call_method1(intern!(py, "get"), ("programs", PyDict::new(py)))?;
        let program = programs.call_method1(intern!(py, "get"), (get(&payload, "program_id")?,))?;
        if !program.is_instance_of::<PyDict>() {
            return not_approved();
        }
        let effect_id = get(&payload, "effect_id")?;
        let operators = program.call_method1(intern!(py, "get"), ("operators", PyDict::new(py)))?;
        if !program.call_method1(intern!(py, "get"), ("effect_ids", PyList::empty(py)))?.contains(&effect_id)?
            || !operators.call_method1(intern!(py, "get"), (&effect_id,))?.eq(&op_value)?
            || !program.call_method1(intern!(py, "get"), ("scheduled_event_ids", PyList::empty(py)))?.contains(attribute(event, intern!(py, "event_id"))?)?
            || (op != "population_presence" && !attribute(world, intern!(py, "zones"))?.contains(get(&payload, "zone_id")?)?)
        {
            return not_approved();
        }
        Ok((payload, op))
    })();
    let (payload, op) = counted(py, "population.checks", refused_by_python(py, checked))?;
    if op == "population_presence" {
        // Past its checks the Python rule lets this rule's exceptions go, as this does.
        population_lifecycle::apply_presence_command(world, event, &payload.get_item("command")?)?;
        return Ok(());
    }
    // A deferral: the copy with its retry takes the world's place, and the event is suppressed.
    let changed = (|| -> PyResult<Option<PyErr>> {
        let candidate = copy_of(world)?;
        let (zone, count) = (payload.get_item("zone_id")?, payload.get_item("count")?);
        let done = if op == "add_evacuees" {
            let tuple = py.import("builtins")?.getattr("tuple")?;
            let empty = PyTuple::empty(py);
            // The call's arguments, in the order the rule's call evaluates them, in its `try`: what
            // `tuple()` raises there is the rule's `EventApplicationError(str(exc))`.
            let as_tuple = |key: &str| -> PyResult<Bound<'py, PyAny>> {
                tuple.call1((payload.call_method1(intern!(py, "get"), (key, &empty))?,)).map_err(|error| wrapped(py, error))
            };
            let options = population_rules::ArrivalOptions {
                household_size: Some(payload.get_item("household_size")?),
                mobility: Some(payload.get_item("mobility")?),
                language: Some(payload.get_item("language")?),
                admission_zone_ids: Some(as_tuple("admission_zone_ids")?),
                approach_route_id: Some(get(&payload, "approach_route_id")?),
                person_context_overrides: Some(as_tuple("person_context_overrides")?),
            };
            population_rules::arrivals(&candidate, event, &zone, &count, &options).map(|_| ())
        } else {
            let include = payload.call_method1(intern!(py, "get"), ("include_initial", false))?;
            population_rules::departures(&candidate, event, &zone, &count, Some(&include)).and_then(|_| {
                population_lifecycle::admit_pending_returns(&candidate, &attribute(event, intern!(py, "due_minute"))?).map(|_| ())
            })
        };
        let deferred = match done {
            Ok(()) => None,
            Err(error) if error.is_instance(py, &models(py)?.getattr(intern!(py, "EventSuppressed"))?) => Some(error),
            Err(error) => return Err(error),
        };
        candidate.call_method0(intern!(py, "validate"))?;
        py.import("sheltersim.domain.simulator_state")?.getattr("validate_simulator_extensions")?.call1((&candidate,))?;
        swap(world, &candidate)?;
        Ok(deferred)
    })();
    match counted(py, "population.copy", refused_by_python(py, changed))? {
        Some(deferred) => Err(deferred),
        None => Ok(()),
    }
}

// ---- InfrastructureEventHandler --------------------------------------------------------------------

/// `_infrastructure_state`.
fn infrastructure_state<'py>(world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let state = get(&table_of(world)?, INFRASTRUCTURE)?;
    if !state.is_instance_of::<PyDict>() {
        let event_type = attribute(event, intern!(py, "event_type"))?;
        return errors::failed(py, format!("{} requires the opted-in infrastructure model", event_type.str()?));
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
            return errors::failed(py, "combined infrastructure failures exceed installed units");
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
    let state = infrastructure_state(world, cause)?;
    let outage_id = get(&attribute(cause, intern!(py, "payload"))?, "outage_id")?;
    let changed = state.get_item("outages")?.call_method1(intern!(py, "get"), (outage_id,))?;
    if changed.is_none() {
        return errors::failed(py, "infrastructure capacity update references an unknown outage");
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

/// The payload's `until_minute` if it is an int (not a bool) after the event's minute and within the
/// Run; None where the Python rule's check says it is invalid.
fn until_of(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<Option<i64>> {
    let py = world.py();
    let Some(until) = errors::int_not_bool(&get(&attribute(event, intern!(py, "payload"))?, "until_minute")?)? else {
        return Ok(None);
    };
    if attribute(event, intern!(py, "due_minute"))?.ge(until)? || until > duration_of(world)? {
        return Ok(None);
    }
    Ok(Some(until))
}

fn no_source(event: &Bound<'_, PyAny>) -> PyResult<bool> {
    Ok(attribute(event, intern!(event.py(), "source_id"))?.is_none())
}

fn start_outage(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let state = infrastructure_state(world, event)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let event_id = attribute(event, intern!(py, "event_id"))?;
    let event_type = attribute(event, intern!(py, "event_type"))?;
    let outage_id = get(&payload, "outage_id")?;
    let record = state.get_item("outages")?.call_method1(intern!(py, "get"), (&outage_id,))?;
    if record.is_none() || record.get_item("start_event_id")?.ne(&event_id)? {
        return errors::failed(py, "infrastructure outage does not match scenario state");
    }
    if record.get_item("event_type")?.ne(&event_type)? {
        return errors::failed(py, "infrastructure outage event type does not match scenario state");
    }
    if record.get_item("active")?.is_truthy()? || no_source(event)? {
        return errors::failed(py, "infrastructure outage is already active or lacks source");
    }
    let power = event_type.eq("power_outage")?;
    let target = get(&payload, if power { "component_id" } else { "facility_id" })?;
    let right_fields =
        if power { fields_are(&payload, &["outage_id", "component_id", "until_minute"])? } else { fields_are(&payload, &["outage_id", "facility_id", "units_out", "until_minute"])? };
    let until = if right_fields { until_of(world, event)? } else { None };
    let Some(until) = until else {
        return errors::failed(py, "infrastructure outage payload or duration is invalid");
    };
    let config = infrastructure_config(world)?;
    let affected = dependents_of(&config, exact_str(&target)?, &facility_ids(world)?)?;
    if affected.is_empty() || record.get_item("affected_facility_ids")?.ne(PyList::new(py, &affected)?)? {
        return errors::failed(py, "infrastructure outage has no valid affected facilities");
    }
    for id in &affected {
        if has_active_modifier(world, PyString::new(py, id).as_any())? {
            return errors::failed(py, "infrastructure outage conflicts with an active facility modifier");
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
    let state = infrastructure_state(world, event)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let record = state.get_item("outages")?.call_method1(intern!(py, "get"), (get(&payload, "outage_id")?,))?;
    if !fields_are(&payload, &["outage_id"])?
        || record.is_none()
        || !record.get_item("active")?.is_truthy()?
        || record.get_item("restore_event_id")?.ne(attribute(event, intern!(py, "event_id"))?)?
        || record.get_item("start_event_id")?.ne(attribute(event, intern!(py, "causation_id"))?)?
        || no_source(event)?
    {
        return errors::failed(py, "infrastructure restoration does not match an active outage");
    }
    let expected = if record.get_item("event_type")?.eq("power_outage")? { "power_restored" } else { "equipment_repaired" };
    if attribute(event, intern!(py, "event_type"))?.ne(expected)? {
        return errors::failed(py, "infrastructure restoration type does not match its cause");
    }
    record.set_item("active", false)?;
    recalculate_capacity(world, event)
}

fn start_stressor(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let state = infrastructure_state(world, event)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let stressor_id = get(&payload, "stressor_id")?;
    let stressors = state.get_item("environmental_stressors")?;
    let record = stressors.call_method1(intern!(py, "get"), (&stressor_id,))?;
    let until = if !fields_are(&payload, &["stressor_id", "zone_id", "until_minute"])?
        || record.is_none()
        || record.get_item("start_event_id")?.ne(attribute(event, intern!(py, "event_id"))?)?
        || record.get_item("active")?.is_truthy()?
        || !attribute(world, intern!(py, "zones"))?.contains(get(&payload, "zone_id")?)?
        || no_source(event)?
    {
        None
    } else {
        until_of(world, event)?
    };
    let Some(until) = until else {
        return errors::failed(py, "environmental stressor payload or duration is invalid");
    };
    let zone = record.get_item("zone_id")?;
    let mut active = 0;
    for other in stressors.call_method0(intern!(py, "values"))?.try_iter()? {
        let other = other?;
        if other.get_item("active")?.is_truthy()? && other.get_item("zone_id")?.eq(&zone)? {
            active += 1;
        }
    }
    if active >= 16 {
        return errors::failed(py, "zone environmental stressor limit was reached");
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
    let state = infrastructure_state(world, event)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let record = state.get_item("environmental_stressors")?.call_method1(intern!(py, "get"), (get(&payload, "stressor_id")?,))?;
    if !fields_are(&payload, &["stressor_id", "zone_id"])?
        || record.is_none()
        || !record.get_item("active")?.is_truthy()?
        || record.get_item("end_event_id")?.ne(attribute(event, intern!(py, "event_id"))?)?
        || record.get_item("start_event_id")?.ne(attribute(event, intern!(py, "causation_id"))?)?
        || record.get_item("zone_id")?.ne(get(&payload, "zone_id")?)?
        || no_source(event)?
    {
        return errors::failed(py, "environmental end event does not match an active stressor");
    }
    record.set_item("active", false)?;
    Ok(())
}

/// `_interrupt_route`: the records of the deliveries it delays.
fn interrupt_route<'py>(world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let py = world.py();
    let state = infrastructure_state(world, event)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let outage_id = get(&payload, "outage_id")?;
    let interruptions = state.get_item("route_interruptions")?;
    let record = interruptions.call_method1(intern!(py, "get"), (&outage_id,))?;
    let until = if !fields_are(&payload, &["outage_id", "route_id", "shipment_ids", "until_minute"])?
        || record.is_none()
        || record.get_item("start_event_id")?.ne(attribute(event, intern!(py, "event_id"))?)?
        || record.get_item("active")?.is_truthy()?
    {
        None
    } else if no_source(event)? {
        None
    } else {
        until_of(world, event)?
    };
    let Some(until) = until else {
        return errors::failed(py, "supply route interruption payload or duration is invalid");
    };
    let route_id = record.get_item("route_id")?;
    for other in interruptions.call_method0(intern!(py, "values"))?.try_iter()? {
        let other = other?;
        if other.get_item("active")?.is_truthy()? && other.get_item("route_id")?.eq(&route_id)? {
            return errors::failed(py, "supply route already has an active interruption");
        }
    }
    let operational = get(&table_of(world)?, OPERATIONAL)?;
    if operational.is_none() {
        return errors::failed(py, "supply route interruption requires shipment records");
    }
    let due = exact_int(&attribute(event, intern!(py, "due_minute"))?)?;
    let delay = until - due;
    let duration = duration_of(world)?;
    let mut delays: Vec<(Bound<'py, PyAny>, i64)> = Vec::new();
    for shipment_id in record.get_item("shipment_ids")?.try_iter()? {
        let shipment_id = shipment_id?;
        let shipment = operational.get_item("shipments")?.call_method1(intern!(py, "get"), (&shipment_id,))?;
        let cannot = || errors::failed(py, format!("shipment {} cannot be safely delayed by this route event", errors::repr(&shipment_id)?));
        if shipment.is_none() {
            return cannot();
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
            return cannot();
        };
        let delivery_due = exact_int(&attribute(&delivery, intern!(py, "due_minute"))?)?;
        if attribute(&delivery, intern!(py, "event_type"))?.ne("resource_delivery")?
            || delivery_due <= due
            || shipment.get_item("due_minute")?.ne(delivery_due)?
            || delivery_due + delay >= duration
        {
            return cannot();
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
    let state = infrastructure_state(world, event)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let record = state.get_item("route_interruptions")?.call_method1(intern!(py, "get"), (get(&payload, "outage_id")?,))?;
    if !fields_are(&payload, &["outage_id", "route_id"])?
        || record.is_none()
        || !record.get_item("active")?.is_truthy()?
        || record.get_item("restore_event_id")?.ne(attribute(event, intern!(py, "event_id"))?)?
        || record.get_item("start_event_id")?.ne(attribute(event, intern!(py, "causation_id"))?)?
        || record.get_item("route_id")?.ne(get(&payload, "route_id")?)?
        || no_source(event)?
    {
        return errors::failed(py, "route restoration does not match an active interruption");
    }
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
        _ => {
            let text = format!("unsupported infrastructure event type {}", errors::repr(&event_type)?);
            return counted(py, "infrastructure", errors::failed(py, text));
        }
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
