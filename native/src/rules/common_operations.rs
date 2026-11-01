//! `domain/common_operations.py`: `record_shared_operation` of a care receipt. The plan makes every
//! check of the Python rule, and says Unsupported before it changes anything for what fails one or
//! for any other kind of receipt, so the Python rule then runs and raises its own text. The Python
//! rule stays as the comparison base (docs/rust-port/lanes/m4-staff.md).

use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyString, PyTuple, PyType};

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
fn record_shared_operation<'py>(world: &Bound<'py, PyAny>, record: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    if !record.get_type().is(care_receipt_class(py)?) {
        return unsupported("another kind of receipt");
    }
    let writer = EventWriter::of(world)?;
    let mirror = mirror_of(world, NAME)?;
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

    let mut document: IndexMap<Arc<str>, V> = IndexMap::with_capacity(15);
    let mut put = |name: &str, value: V| {
        document.insert(Arc::from(name), value);
    };
    put("record_type", V::Str(Arc::from(record_type)));
    put("schema_version", V::Int(schema));
    put("event_type", V::Str(Arc::from(event_type)));
    put("event_id", V::Str(event_id.clone()));
    put("request_id", V::Str(request_id.clone()));
    put("agent_id", V::Str(agent_id.clone()));
    put("household_id", text_value(&household_id));
    put("support_kind", V::Str(support_kind.clone()));
    put("status", V::Str(status_text.clone()));
    put("minute", V::Int(minute));
    put("staff_id", text_value(&staff_id));
    put("outcome_observation", V::Str(observation));
    put("state_changes", changes);
    put("causation_id", text_value(&causation_id));
    put("source_ids", V::List(sources.iter().map(|source| V::Str(source.clone())).collect()));
    let document = V::Map(document);

    // The event of the receipt: applied at this minute, of this type, with this payload.
    let log = world.getattr(intern!(py, "event_log"))?;
    let event_record = log.call_method1(intern!(py, "find"), (&*event_id,))?;
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
    if &*row.event_type != event_type
        || row.payload != document
        || row.causation_id != causation_id
        || !row.source_id.as_ref().is_none_or(|source| sources.contains(source))
    {
        return unsupported("the receipt does not match its event");
    }

    // The plan on the row.
    let first_request = if status == CareStatus::Requested {
        writer.log().borrow().first_care_request_event_of(&request_id).map(Arc::<str>::from)
    } else {
        None
    };
    let plan = NamespaceMirror::with_row::<SharedOperationsRow, _>(&mirror, |row| {
        if !row.header_is_valid() {
            return unsupported("the state is not one the contract takes");
        }
        let Some(mut recent) = row.history.recent_verified(minute) else {
            return unsupported("a recent receipt was not checked here");
        };
        if recent.any(|item| item.event_id.as_deref() == Some(&*event_id)) {
            return unsupported("the event already has a receipt");
        }
        let prior = row.support_requests.map.get(&request_id);
        if !transition_is_allowed(prior.map(|head| head.status), status) {
            return unsupported("the transition is out of order");
        }
        if let Some(prior) = prior {
            if prior.agent_id != agent_id || prior.household_id != household_id || prior.support_kind != support_kind {
                return unsupported("the request's identity changed");
            }
        } else {
            if first_request.as_ref().is_some_and(|first| *first != event_id) {
                return unsupported("the request ID was used before");
            }
            if row.support_requests.map.len() >= MAX_REQUESTS {
                return unsupported("too many requests");
            }
        }
        let created = prior.map_or(minute, |head| head.created_minute);
        if minute < created {
            return unsupported("the update precedes the creation");
        }
        let overflow = row.history.items.len() + 1 > MAX_HISTORY;
        Ok((created, overflow))
    })?;
    let (created_minute, overflow) = plan;

    // The writes. Nothing below fails on a valid state.
    let mut encoder = Encoder { out: Vec::with_capacity(512) };
    document.write_json(&mut encoder)?;
    let receipt = Receipt {
        minute: Some(minute),
        event_id: Some(event_id.clone()),
        verified: true,
        bytes: encoder.out.into_boxed_slice(),
    };
    let tree_document = document.to_py(py)?;
    let new_head = status.is_open().then(|| CareHead {
        request_id: request_id.clone(),
        agent_id: agent_id.clone(),
        household_id: household_id.clone(),
        support_kind: support_kind.clone(),
        status,
        created_minute,
        updated_minute: minute,
        latest_event_id: event_id.clone(),
    });
    let head_document = new_head.as_ref().map(|head| Field::to_py(head, py)).transpose()?;
    NamespaceMirror::with_row::<SharedOperationsRow, _>(&mirror, |row| {
        match new_head {
            Some(head) => match row.support_requests.map.get_mut(&request_id) {
                Some(slot) => *slot = head,
                None => {
                    row.support_requests.map.insert(request_id.clone(), head);
                }
            },
            None => {
                row.support_requests.map.shift_remove(&request_id);
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
        Some(head) => NamespaceMirror::write_through(&mirror, &["support_requests"], &request_id, head)?,
        None => NamespaceMirror::remove_through(&mirror, &["support_requests"], &request_id)?,
    }
    NamespaceMirror::append_through(&mirror, &["history"], &tree_document)?;
    if overflow {
        NamespaceMirror::write_through(&mirror, &[], "history_truncated", PyBool::new(py, true).as_any())?;
        NamespaceMirror::drop_oldest_through(&mirror, &["history"], 1)?;
    }
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(record_shared_operation, m)?)?;
    Ok(())
}
