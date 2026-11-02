//! `domain/common_operations.py`: `record_shared_operation`. The plan makes every
//! check of the Python rule, and says Unsupported before it changes anything for what fails one or
//! for any other kind of receipt, so the Python rule then runs and raises its own text. The Python
//! rule stays as the comparison base (docs/rust-port/lanes/m4-staff.md).

use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PySlice, PyString, PyTuple, PyType};

use crate::core::row::{attribute, exact_dict, exact_float, exact_int, exact_str, unsupported, Field};
use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::core::world_core::{mirror_of, NamespaceMirror};
use crate::state::events::{EventRow, EventWriter};
use crate::state::shared_operations::{
    is_identifier, CareHead, CareStatus, Receipt, SharedOperationsRow, MAX_HISTORY, MAX_REQUESTS, NAME,
};
use crate::{Encoder, Unsupported};

static CARE_RECEIPT: GILOnceCell<Py<PyType>> = GILOnceCell::new();

fn care_receipt_class(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    CARE_RECEIPT
        .get_or_try_init(py, || {
            py.import("sheltersim.contracts.operations")?
                .getattr("CareTransitionReceipt")?
                .downcast_into::<PyType>()
                .map(Bound::unbind)
                .map_err(PyErr::from)
        })
        .map(|class| class.bind(py))
}

fn optional_text(value: &Bound<'_, PyAny>) -> PyResult<Option<Arc<str>>> {
    if value.is_none() {
        return Ok(None);
    }
    Ok(Some(Arc::from(exact_str(value)?)))
}

fn text_value(value: &Option<Arc<str>>) -> V {
    match value {
        Some(text) => V::Str(text.clone()),
        None => V::Null,
    }
}

/// `_CARE_TRANSITIONS`.
fn transition_is_allowed(prior: Option<CareStatus>, next: CareStatus) -> bool {
    match prior {
        None => next == CareStatus::Requested,
        Some(CareStatus::Requested) => {
            matches!(next, CareStatus::Assigned | CareStatus::Started | CareStatus::Declined | CareStatus::Interrupted)
        }
        Some(CareStatus::Assigned) => {
            matches!(next, CareStatus::Started | CareStatus::Declined | CareStatus::Interrupted)
        }
        Some(CareStatus::Started) => matches!(next, CareStatus::Completed | CareStatus::Interrupted),
        Some(_) => false,
    }
}

/// What the receipt's one state change reads now, as `_validate_observed_state_changes` walks to it.
fn observed_value<'py>(
    world: &Bound<'py, PyAny>,
    kind: &str,
    entity: &str,
    field_path: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = world.py();
    let parts: Vec<&str> = field_path.split('.').collect();
    let map_item = |map: &Bound<'py, PyAny>| -> PyResult<Option<Bound<'py, PyAny>>> {
        exact_dict(map)?.get_item(entity)
    };
    let (row, path): (Option<Bound<'py, PyAny>>, &[&str]) = match kind {
        "model_state" => {
            let [namespace, collection, path @ ..] = parts.as_slice() else {
                return unsupported("a state change's path is too short");
            };
            if path.is_empty() {
                return unsupported("a state change's path is too short");
            }
            let table = attribute(world, intern!(py, "model_state"))?;
            let Some(state) = exact_dict(&table)?.get_item(*namespace)? else {
                return Ok(None);
            };
            let Some(state) = as_readable_dict(&state) else {
                return Ok(None);
            };
            let Some(items) = state.get_item(*collection)? else {
                return Ok(None);
            };
            (map_item(&items)?, path)
        }
        "resource" => {
            if parts != ["value"] {
                return unsupported("a resource's change is not of its value");
            }
            (map_item(&attribute(world, intern!(py, "resources"))?)?, &[])
        }
        "agent" => (map_item(&attribute(world, intern!(py, "agents"))?)?, parts.as_slice()),
        "staff" => (map_item(&attribute(world, intern!(py, "staff"))?)?, parts.as_slice()),
        "facility" => (map_item(&attribute(world, intern!(py, "facilities"))?)?, parts.as_slice()),
        "zone" => (map_item(&attribute(world, intern!(py, "zones"))?)?, parts.as_slice()),
        _ => return unsupported("an unknown kind of entity"),
    };
    let mut current = row;
    for part in path {
        let Some(item) = current else {
            return Ok(None);
        };
        current = if let Some(document) = as_readable_dict(&item) {
            document.get_item(*part)?
        } else if item.is_instance_of::<PyDict>() {
            return unsupported("a state change reads another dict");
        } else {
            item.getattr(*part).ok()
        };
    }
    Ok(current)
}

/// The state changes as the receipt dumps them, after they pass `_validate_observed_state_changes`.
fn state_changes(world: &Bound<'_, PyAny>, changes: &Bound<'_, PyAny>) -> PyResult<V> {
    let py = world.py();
    let changes = changes.downcast_exact::<PyTuple>().map_err(|_| Unsupported::new_err("the changes are not a tuple"))?;
    let mut dumped = Vec::with_capacity(changes.len());
    for change in changes.iter() {
        let entity = attribute(&change, intern!(py, "entity_id"))?;
        let kind = attribute(&change, intern!(py, "entity_kind"))?;
        let field_path = attribute(&change, intern!(py, "field_path"))?;
        let before = exact_float(&attribute(&change, intern!(py, "before"))?)?;
        let after = exact_float(&attribute(&change, intern!(py, "after"))?)?;
        let unit = attribute(&change, intern!(py, "unit"))?;
        let (entity, kind, field_path, unit) =
            (exact_str(&entity)?, exact_str(&kind)?, exact_str(&field_path)?, exact_str(&unit)?);
        let Some(current) = observed_value(world, kind, entity, field_path)? else {
            return unsupported("a state change reads nothing");
        };
        // `type(current) in (int, float) and current == change.after`.
        let same = if let Ok(number) = current.downcast_exact::<PyFloat>() {
            number.value() == after
        } else if current.downcast_exact::<PyInt>().is_ok() {
            let number = exact_int(&current)?;
            if number.unsigned_abs() >= 1 << 53 {
                return unsupported("a state change reads a large int");
            }
            number as f64 == after
        } else {
            false
        };
        if !same || current.is_instance_of::<PyBool>() {
            return unsupported("a state change does not match the world");
        }
        let mut document: IndexMap<Arc<str>, V> = IndexMap::with_capacity(6);
        document.insert(Arc::from("entity_id"), V::Str(Arc::from(entity)));
        document.insert(Arc::from("entity_kind"), V::Str(Arc::from(kind)));
        document.insert(Arc::from("field_path"), V::Str(Arc::from(field_path)));
        document.insert(Arc::from("before"), V::Float(before));
        document.insert(Arc::from("after"), V::Float(after));
        document.insert(Arc::from("unit"), V::Str(Arc::from(unit)));
        dumped.push(V::Map(document));
    }
    Ok(V::List(dumped))
}

/// `record_shared_operation(world, record)` of a `CareTransitionReceipt`.
#[pyfunction]
pub(crate) fn record_shared_operation<'py>(world: &Bound<'py, PyAny>, record: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    if !record.get_type().is(care_receipt_class(py)?) {
        return record_other(world, record);
    }
    let writer = EventWriter::of(world)?;
    let mirror = mirror_of(world, NAME)?;
    let table = attribute(world, intern!(py, "model_state"))?;
    match exact_dict(&table)?.get_item(NAME)? {
        Some(state) if mirror.try_borrow()?.root_is(&state) => {}
        _ => return unsupported("the world holds another document than the row's"),
    }
    let world_minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let field = |name: &Bound<'py, PyString>| attribute(record, name);
    let minute = exact_int(&field(intern!(py, "minute"))?)?;
    if minute != world_minute {
        return unsupported("the receipt's minute is not the world's");
    }
    let event_id: Arc<str> = Arc::from(exact_str(&field(intern!(py, "event_id"))?)?);
    let request_id: Arc<str> = Arc::from(exact_str(&field(intern!(py, "request_id"))?)?);
    let agent_id: Arc<str> = Arc::from(exact_str(&field(intern!(py, "agent_id"))?)?);
    let household_id = optional_text(&field(intern!(py, "household_id"))?)?;
    let support_kind: Arc<str> = Arc::from(exact_str(&field(intern!(py, "support_kind"))?)?);
    let status_text: Arc<str> = Arc::from(exact_str(&field(intern!(py, "status"))?)?);
    let Some(status) = CareStatus::parse(&status_text) else {
        return unsupported("an unknown care status");
    };
    let staff_id = optional_text(&field(intern!(py, "staff_id"))?)?;
    let observation = Arc::from(exact_str(&field(intern!(py, "outcome_observation"))?)?);
    let causation_id = optional_text(&field(intern!(py, "causation_id"))?)?;
    let source_ids_object = field(intern!(py, "source_ids"))?;
    let source_ids = source_ids_object
        .downcast_exact::<PyTuple>()
        .map_err(|_| Unsupported::new_err("the sources are not a tuple"))?;
    let mut sources: Vec<Arc<str>> = Vec::with_capacity(source_ids.len());
    for source in source_ids.iter() {
        sources.push(Arc::from(exact_str(&source)?));
    }
    if !is_identifier(&event_id) || !is_identifier(&request_id) {
        return unsupported("an ID is not an identifier");
    }

    let schema = exact_int(&field(intern!(py, "schema_version"))?)?;
    let record_type = field(intern!(py, "record_type"))?;
    let event_type = field(intern!(py, "event_type"))?;
    let (record_type, event_type) = (exact_str(&record_type)?, exact_str(&event_type)?);
    let changes = state_changes(world, &field(intern!(py, "state_changes"))?)?;

    let record = CareRecord {
        record_type: Arc::from(record_type),
        schema,
        event_type: Arc::from(event_type),
        event_id,
        request_id,
        agent_id,
        household_id,
        support_kind,
        status_text,
        status,
        minute,
        staff_id,
        observation,
        changes,
        causation_id,
        sources,
    };
    let document = record.document();

    // The event of the receipt: applied at this minute, of this type, with this payload.
    let log = world.getattr(intern!(py, "event_log"))?;
    let event_record = log.call_method1(intern!(py, "find"), (&*record.event_id,))?;
    if event_record.is_none() {
        return unsupported("the receipt has no event");
    }
    if exact_int(&attribute(&event_record, intern!(py, "realized_minute"))?)? != minute
        || attribute(&event_record, intern!(py, "status"))?.getattr(intern!(py, "_value_"))?.extract::<String>()?
            != "applied"
    {
        return unsupported("the receipt's event is not applied at this minute");
    }
    let event = attribute(&event_record, intern!(py, "event"))?;
    let Some(row) = EventRow::from_event(&event)? else {
        return unsupported("the receipt's event has no row");
    };
    if *row.event_type != *record.event_type
        || row.payload != document
        || row.causation_id != record.causation_id
        || !row.source_id.as_ref().is_none_or(|source| record.sources.contains(source))
    {
        return unsupported("the receipt does not match its event");
    }

    let plan = plan_record(world, &writer, &mirror, &record)?;
    write_record(py, &mirror, &record, &document, plan)
}

/// What the contract says of the recent receipts when some were not checked here: a restored or
/// forwarded one. `SharedOperationsState.model_validate({**state, "history": recent})`.
fn recent_receipts_hold(world: &Bound<'_, PyAny>, mirror: &Bound<'_, NamespaceMirror>, minute: i64) -> PyResult<()> {
    let py = world.py();
    let start = NamespaceMirror::with_row::<SharedOperationsRow, _>(mirror, |row| {
        Ok(if row.history.recent_verified(minute).is_some() { None } else { Some(row.history.recent_start(minute)) })
    })?;
    let Some(start) = start else {
        return Ok(());
    };
    let table = attribute(world, intern!(py, "model_state"))?;
    let Some(state) = exact_dict(&table)?.get_item(NAME)? else {
        return unsupported("no shared operations");
    };
    let history = state.get_item("history")?;
    let recent = history.get_item(PySlice::new(py, start as isize, history.len()? as isize, 1))?;
    let document = PyDict::new(py);
    document.call_method1(intern!(py, "update"), (&state,))?;
    document.set_item("history", recent)?;
    py.import(intern!(py, "sheltersim.contracts.operations"))?
        .getattr("SharedOperationsState")?
        .call_method1("model_validate", (document,))
        .map_err(|_| Unsupported::new_err("the recent receipts are not what the contract takes"))?;
    Ok(())
}

/// `record_shared_operation` of what isn't a care receipt. These are few in a Run, so the checks
/// read Python's objects, and the writes go through the namespace's containers as the rule's do.
fn record_other<'py>(world: &Bound<'py, PyAny>, record: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    let contracts = py.import(intern!(py, "sheltersim.contracts.operations"))?;
    let is_a = |name: &str| -> PyResult<bool> { record.is_instance(&contracts.getattr(name)?) };
    let (continuity, handoff, recovery, operation) =
        (is_a("StaffSupportContinuityReceipt")?, is_a("StaffHandoffReceipt")?, is_a("FacilityRecoveryReceipt")?, is_a("StaffOperationReceipt")?);
    if !(continuity || handoff || recovery || operation) {
        return unsupported("an unknown kind of receipt");
    }
    let mirror = mirror_of(world, NAME)?;
    let table = attribute(world, intern!(py, "model_state"))?;
    let state = match exact_dict(&table)?.get_item(NAME)? {
        Some(state) if mirror.try_borrow()?.root_is(&state) => state,
        _ => return unsupported("the world holds another document than the row's"),
    };
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let field = |name: &Bound<'py, PyString>| attribute(record, name);
    if exact_int(&field(intern!(py, "minute"))?)? != minute {
        return unsupported("the receipt's minute is not the world's");
    }
    let event_id_object = field(intern!(py, "event_id"))?;
    let event_id = exact_str(&event_id_object)?;
    recent_receipts_hold(world, &mirror, minute)?;
    let log = attribute(world, intern!(py, "event_log"))?;
    let event_record = log.call_method1(intern!(py, "find"), (event_id,))?;
    if event_record.is_none() || !event_is(&event_record, "applied")? || exact_int(&attribute(&event_record, intern!(py, "realized_minute"))?)? != minute {
        return unsupported("the receipt's event is not applied at this minute");
    }
    let event = attribute(&event_record, intern!(py, "event"))?;
    if !attribute(&event, intern!(py, "event_type"))?.eq(field(intern!(py, "event_type"))?)? {
        return unsupported("the receipt's event is of another type");
    }
    let (over_limit, recorded, valid) = NamespaceMirror::with_row::<SharedOperationsRow, _>(&mirror, |row| {
        let start = row.history.recent_start(minute);
        let recorded = row.history.items.iter().skip(start).any(|item| item.event_id.as_deref() == Some(event_id));
        Ok((row.history.items.len() + 1 > MAX_HISTORY, recorded, row.header_is_valid()))
    })?;
    if recorded || !valid {
        return unsupported("the event already has a receipt, or the state is not one the contract takes");
    }
    let options = PyDict::new(py);
    options.set_item("mode", "json")?;
    let common = record.call_method("model_dump", (), Some(&options))?;
    // `_event_payload_matches`.
    let expected = PyDict::new(py);
    expected.call_method1(intern!(py, "update"), (&common,))?;
    let payload = event.call_method0(intern!(py, "payload_dict"))?;
    if continuity && !payload.contains("cause_type")? && text_of(&field(intern!(py, "cause_type"))?) == Some("staff_unavailable".to_owned()) {
        expected.del_item("cause_type")?;
    }
    let source = attribute(&event, intern!(py, "source_id"))?;
    if !payload.eq(&expected)?
        || !attribute(&event, intern!(py, "causation_id"))?.eq(field(intern!(py, "causation_id"))?)?
        || !(source.is_none() || field(intern!(py, "source_ids"))?.contains(&source)?)
    {
        return unsupported("the receipt does not match its event");
    }
    let heads = state.get_item("support_requests")?;
    let new_head = if continuity {
        let request_id = field(intern!(py, "request_id"))?;
        let prior = heads.call_method1(intern!(py, "get"), (&request_id,))?;
        check_continuity(world, record, &prior, &request_id)?;
        if text_of(&field(intern!(py, "status"))?).as_deref() == Some("requeued") {
            let head = PyDict::new(py);
            head.set_item("request_id", &request_id)?;
            head.set_item("agent_id", field(intern!(py, "agent_id"))?)?;
            head.set_item("household_id", prior.call_method1(intern!(py, "get"), ("household_id",))?)?;
            head.set_item("support_kind", prior.get_item("support_kind")?)?;
            head.set_item("status", "requested")?;
            head.set_item("created_minute", prior.get_item("created_minute")?)?;
            head.set_item("updated_minute", minute)?;
            head.set_item("latest_event_id", event_id)?;
            if exact_int(&prior.get_item("created_minute")?)? > minute {
                return unsupported("the update precedes the creation");
            }
            Some((request_id, Some(head)))
        } else {
            Some((request_id, None))
        }
    } else {
        None
    };
    if handoff {
        check_handoff(world, record)?;
    }
    if recovery {
        let facility = get_of(&attribute(world, intern!(py, "facilities"))?, &field(intern!(py, "facility_id"))?)?;
        let units = exact_int(&field(intern!(py, "working_units"))?)?;
        if facility.is_none() || units > exact_int(&attribute(&facility, intern!(py, "capacity"))?)? || exact_int(&attribute(&facility, intern!(py, "working_units"))?)? != units {
            return unsupported("the facility recovery does not match the facility");
        }
    }
    if operation {
        check_staff_operation(world, record, state.get_item("history_truncated")?.is_truthy()?)?;
        state_changes(world, &field(intern!(py, "state_changes"))?)?;
    }

    // Write.
    if let Some((request_id, head)) = new_head {
        match head {
            Some(head) => heads.set_item(request_id, head)?,
            None => {
                heads.call_method1(intern!(py, "pop"), (request_id, py.None()))?;
            }
        }
    }
    let history = state.get_item("history")?;
    history.call_method1(intern!(py, "append"), (common,))?;
    if over_limit {
        state.set_item("history_truncated", true)?;
        history.del_item(PySlice::new(py, 0, (history.len()? - MAX_HISTORY) as isize, 1))?;
    }
    // Checked above as a tick's check would check it, so that check needs nothing more of it but
    // the staff completions, which it reads again (`state/shared_operations.rs`).
    NamespaceMirror::with_row::<SharedOperationsRow, _>(&mirror, |row| {
        row.history.appended_passed(event_id);
        Ok(())
    })
}

fn text_of(value: &Bound<'_, PyAny>) -> Option<String> {
    exact_str(value).ok().map(str::to_owned)
}

fn get_of<'py>(map: &Bound<'py, PyAny>, key: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    map.call_method1(intern!(map.py(), "get"), (key,))
}

/// `record.status is EventStatus.<wanted>`.
fn event_is(record: &Bound<'_, PyAny>, wanted: &str) -> PyResult<bool> {
    let py = record.py();
    Ok(text_of(&attribute(record, intern!(py, "status"))?.getattr(intern!(py, "_value_"))?).as_deref() == Some(wanted))
}

/// The branch of `StaffSupportContinuityReceipt`.
fn check_continuity<'py>(world: &Bound<'py, PyAny>, record: &Bound<'py, PyAny>, prior: &Bound<'py, PyAny>, request_id: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    let field = |name: &Bound<'py, PyString>| attribute(record, name);
    let refused = || unsupported("the continuity receipt does not match the interrupted request");
    if prior.is_none() {
        return refused();
    }
    let request = get_of(&attribute(world, intern!(py, "support_requests"))?, request_id)?;
    if request.is_none() {
        return refused();
    }
    let log = attribute(world, intern!(py, "event_log"))?;
    let cause = log.call_method1(intern!(py, "find"), (field(intern!(py, "causation_id"))?,))?;
    if cause.is_none() {
        return refused();
    }
    let previous_staff = field(intern!(py, "previous_staff_id"))?;
    let cause_event = attribute(&cause, intern!(py, "event"))?;
    let cause_payload = attribute(&cause_event, intern!(py, "payload"))?;
    let cause_of = |key: &str| cause_payload.call_method1(intern!(py, "get"), (key,));
    let cause_type = text_of(&field(intern!(py, "cause_type"))?);
    let event_type = text_of(&attribute(&cause_event, intern!(py, "event_type"))?);
    let cause_matches = match cause_type.as_deref() {
        Some("route_unreachable") => {
            event_is(&cause, "suppressed")?
                && event_type.as_deref() == Some("agent_travel_complete")
                && text_of(&cause_of("purpose")?).as_deref() == Some("staff_support_travel")
                && cause_of("request_id")?.eq(request_id)?
                && cause_of("staff_id")?.eq(&previous_staff)?
        }
        Some("staff_unavailable" | "staff_shift_change") => event_is(&cause, "applied")? && event_type == cause_type && cause_of("staff_id")?.eq(&previous_staff)?,
        _ => false,
    };
    let prior_status = text_of(&prior.get_item("status")?);
    let agent_id = field(intern!(py, "agent_id"))?;
    if !matches!(prior_status.as_deref(), Some("assigned" | "started"))
        || !prior.get_item("agent_id")?.eq(&agent_id)?
        || !attribute(&request, intern!(py, "agent_id"))?.eq(&agent_id)?
        || !attribute(&request, intern!(py, "assigned_staff_id"))?.is_none()
        || !attribute(&request, intern!(py, "task_id"))?.is_none()
        || !cause_matches
        || attribute(world, intern!(py, "active_tasks"))?.contains(field(intern!(py, "task_id"))?)?
    {
        return refused();
    }
    let expected = if text_of(&field(intern!(py, "status"))?).as_deref() == Some("requeued") { "pending" } else { "unresolved" };
    if text_of(&attribute(&request, intern!(py, "status"))?.getattr(intern!(py, "value"))?).as_deref() != Some(expected) {
        return unsupported("the continuity receipt's status disagrees with the request");
    }
    let prior_event = log.call_method1(intern!(py, "find"), (prior.get_item("latest_event_id")?,))?;
    if prior_event.is_none() || !attribute(&attribute(&prior_event, intern!(py, "event"))?, intern!(py, "payload"))?.call_method1(intern!(py, "get"), ("staff_id",))?.eq(&previous_staff)? {
        return unsupported("the continuity receipt does not match its previous assignment");
    }
    Ok(())
}

/// The branch of `StaffHandoffReceipt`.
fn check_handoff<'py>(world: &Bound<'py, PyAny>, record: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    let field = |name: &Bound<'py, PyString>| attribute(record, name);
    let task = get_of(&attribute(world, intern!(py, "active_tasks"))?, &field(intern!(py, "task_id"))?)?;
    let status = text_of(&field(intern!(py, "status"))?);
    let holds = |wanted: &Bound<'py, PyAny>| -> PyResult<bool> { Ok(!task.is_none() && attribute(&task, intern!(py, "staff_id"))?.eq(wanted)?) };
    let fits = match status.as_deref() {
        Some("reassigned") => holds(&field(intern!(py, "replacement_staff_id"))?)?,
        Some("unfilled") => !task.is_none() && attribute(&task, intern!(py, "staff_id"))?.is_none(),
        Some("rejected") => holds(&field(intern!(py, "previous_staff_id"))?)?,
        _ => true,
    };
    if !fits {
        return unsupported("the handoff does not match the active task");
    }
    Ok(())
}

/// `_validate_staff_operation_receipt`.
fn check_staff_operation<'py>(world: &Bound<'py, PyAny>, record: &Bound<'py, PyAny>, history_truncated: bool) -> PyResult<()> {
    let py = world.py();
    let field = |name: &Bound<'py, PyString>| attribute(record, name);
    let table = attribute(world, intern!(py, "model_state"))?;
    let operations = exact_dict(&table)?.get_item("staff_operations_v1")?;
    let history = match &operations {
        Some(operations) if operations.is_instance_of::<PyDict>() => get_of(operations, PyString::new(py, "history").as_any())?,
        _ => py.None().into_bound(py),
    };
    let rows: Vec<Bound<'py, PyAny>> = if history.is_none() { Vec::new() } else { history.try_iter()?.collect::<PyResult<_>>()? };
    let rows: Vec<_> = rows.into_iter().filter(|row| row.is_instance_of::<PyDict>()).collect();
    let (task_id, minute) = (field(intern!(py, "task_id"))?, field(intern!(py, "minute"))?);
    let of = |row: &Bound<'py, PyAny>, key: &str| row.call_method1(intern!(py, "get"), (key,));
    let mut matches = Vec::new();
    for row in &rows {
        if of(row, "task_id")?.eq(&task_id)? && of(row, "complete_minute")?.eq(&minute)? {
            matches.push(row);
        }
    }
    let Some(row) = matches.first() else {
        let minute = exact_int(&minute)?;
        let mut all_later = !rows.is_empty();
        for row in &rows {
            let complete = row.call_method1(intern!(py, "get"), ("complete_minute", 0))?;
            all_later = all_later && exact_int(&complete)? > minute;
        }
        // `history and ...`: an empty history, or one only of what isn't a dict, has no later row.
        if history_truncated && all_later {
            return Ok(());
        }
        return unsupported("the receipt has no matching retained completion");
    };
    if matches.len() != 1 {
        return unsupported("the receipt matches duplicate completion history");
    }
    for (key, name) in [
        ("staff_id", intern!(py, "staff_id")),
        ("task_kind", intern!(py, "task_kind")),
        ("target_id", intern!(py, "target_id")),
        ("target_zone_id", intern!(py, "target_zone_id")),
        ("reason", intern!(py, "reason")),
        ("status", intern!(py, "status")),
        ("start_minute", intern!(py, "start_minute")),
        ("arrival_minute", intern!(py, "arrival_minute")),
        ("travel_minutes", intern!(py, "travel_minutes")),
    ] {
        if !of(row, key)?.eq(field(name)?)? {
            return unsupported("the receipt disagrees with retained completion history");
        }
    }
    let causes = row.call_method1(intern!(py, "get"), ("causation_event_ids", PyTuple::empty(py)))?;
    if !causes.contains(field(intern!(py, "causation_id"))?)? {
        return unsupported("the receipt disagrees with retained completion history");
    }
    Ok(())
}

/// What `record_shared_operation` holds of a care receipt, as `model_dump(mode="json")` gives it.
pub(crate) struct CareRecord {
    pub(crate) record_type: Arc<str>,
    pub(crate) schema: i64,
    pub(crate) event_type: Arc<str>,
    pub(crate) event_id: Arc<str>,
    pub(crate) request_id: Arc<str>,
    pub(crate) agent_id: Arc<str>,
    pub(crate) household_id: Option<Arc<str>>,
    pub(crate) support_kind: Arc<str>,
    pub(crate) status_text: Arc<str>,
    pub(crate) status: CareStatus,
    pub(crate) minute: i64,
    pub(crate) staff_id: Option<Arc<str>>,
    pub(crate) observation: Arc<str>,
    pub(crate) changes: V,
    pub(crate) causation_id: Option<Arc<str>>,
    pub(crate) sources: Vec<Arc<str>>,
}

impl CareRecord {
    pub(crate) fn document(&self) -> V {
        let mut document: IndexMap<Arc<str>, V> = IndexMap::with_capacity(15);
        let mut put = |name: &str, value: V| {
            document.insert(Arc::from(name), value);
        };
        put("record_type", V::Str(self.record_type.clone()));
        put("schema_version", V::Int(self.schema));
        put("event_type", V::Str(self.event_type.clone()));
        put("event_id", V::Str(self.event_id.clone()));
        put("request_id", V::Str(self.request_id.clone()));
        put("agent_id", V::Str(self.agent_id.clone()));
        put("household_id", text_value(&self.household_id));
        put("support_kind", V::Str(self.support_kind.clone()));
        put("status", V::Str(self.status_text.clone()));
        put("minute", V::Int(self.minute));
        put("staff_id", text_value(&self.staff_id));
        put("outcome_observation", V::Str(self.observation.clone()));
        put("state_changes", self.changes.clone());
        put("causation_id", text_value(&self.causation_id));
        put("source_ids", V::List(self.sources.iter().map(|source| V::Str(source.clone())).collect()));
        V::Map(document)
    }
}

/// What `plan_record` found: the request's first minute, and whether the history is full.
pub(crate) struct PlannedRecord {
    created_minute: i64,
    overflow: bool,
}

/// The checks of `record_shared_operation` on the row. Unsupported for what fails one; nothing changes.
pub(crate) fn plan_record(
    world: &Bound<'_, PyAny>,
    writer: &EventWriter<'_>,
    mirror: &Bound<'_, NamespaceMirror>,
    record: &CareRecord,
) -> PyResult<PlannedRecord> {
    let (event_id, request_id, minute) = (&record.event_id, &record.request_id, record.minute);
    recent_receipts_hold(world, mirror, minute)?;
    let first_request = if record.status == CareStatus::Requested {
        writer.log().borrow().first_care_request_event_of(request_id).map(Arc::<str>::from)
    } else {
        None
    };
    NamespaceMirror::with_row::<SharedOperationsRow, _>(mirror, |row| {
        if !row.header_is_valid() {
            return unsupported("the state is not one the contract takes");
        }
        let start = row.history.recent_start(minute);
        if row.history.items.iter().skip(start).any(|item| item.event_id.as_deref() == Some(&**event_id)) {
            return unsupported("the event already has a receipt");
        }
        let prior = row.support_requests.map.get(request_id);
        if !transition_is_allowed(prior.map(|head| head.status), record.status) {
            return unsupported("the transition is out of order");
        }
        if let Some(prior) = prior {
            if prior.agent_id != record.agent_id
                || prior.household_id != record.household_id
                || prior.support_kind != record.support_kind
            {
                return unsupported("the request's identity changed");
            }
        } else {
            if first_request.as_ref().is_some_and(|first| first != event_id) {
                return unsupported("the request ID was used before");
            }
            if row.support_requests.map.len() >= MAX_REQUESTS {
                return unsupported("too many requests");
            }
        }
        let created_minute = prior.map_or(minute, |head| head.created_minute);
        if minute < created_minute {
            return unsupported("the update precedes the creation");
        }
        Ok(PlannedRecord { created_minute, overflow: row.history.items.len() + 1 > MAX_HISTORY })
    })
}

/// The writes of `record_shared_operation`. Nothing here fails on a valid state.
pub(crate) fn write_record(
    py: Python<'_>,
    mirror: &Bound<'_, NamespaceMirror>,
    record: &CareRecord,
    document: &V,
    plan: PlannedRecord,
) -> PyResult<()> {
    let PlannedRecord { created_minute, overflow } = plan;
    let (event_id, request_id, minute) = (&record.event_id, &record.request_id, record.minute);
    let mut encoder = Encoder { out: Vec::with_capacity(512) };
    document.write_json(&mut encoder)?;
    let receipt = Receipt {
        minute: Some(minute),
        event_id: Some(event_id.clone()),
        verified: true,
        reads_staff_history: false,
        bytes: encoder.out.into_boxed_slice(),
    };
    let tree_document = document.to_py(py)?;
    let new_head = record.status.is_open().then(|| CareHead {
        request_id: request_id.clone(),
        agent_id: record.agent_id.clone(),
        household_id: record.household_id.clone(),
        support_kind: record.support_kind.clone(),
        status: record.status,
        created_minute,
        updated_minute: minute,
        latest_event_id: event_id.clone(),
    });
    let head_document = new_head.as_ref().map(|head| Field::to_py(head, py)).transpose()?;
    NamespaceMirror::with_row::<SharedOperationsRow, _>(mirror, |row| {
        match new_head {
            Some(head) => match row.support_requests.map.get_mut(request_id) {
                Some(slot) => *slot = head,
                None => {
                    row.support_requests.map.insert(request_id.clone(), head);
                }
            },
            None => {
                row.support_requests.map.shift_remove(request_id);
            }
        }
        row.history.items.push_back(receipt);
        if overflow {
            row.history_truncated = true;
            row.history.items.pop_front();
        }
        Ok(())
    })?;
    match &head_document {
        Some(head) => NamespaceMirror::write_through(mirror, &["support_requests"], request_id, head)?,
        None => NamespaceMirror::remove_through(mirror, &["support_requests"], request_id)?,
    }
    NamespaceMirror::append_through(mirror, &["history"], &tree_document)?;
    if overflow {
        NamespaceMirror::write_through(mirror, &[], "history_truncated", PyBool::new(py, true).as_any())?;
        NamespaceMirror::drop_oldest_through(mirror, &["history"], 1)?;
    }
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(record_shared_operation, m)?)?;
    Ok(())
}
