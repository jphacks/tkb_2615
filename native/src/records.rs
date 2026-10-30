//! Repeated work on the records a tick makes: the checks and frozen copies of a
//! `ScheduledEvent`, and the audit hash of a `WorldView`. Each gives what the Python code
//! gives, and any value of another type or any value that Python would reject raises
//! `Unsupported`, so that code runs and raises its own error.

use pyo3::ffi;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use sha2::{Digest, Sha256};

use crate::{Encoder, Unsupported};

// Deeper values are left to Python, which can raise its own errors about them.
const MAX_FROZEN_DEPTH: usize = 64;

/// `_STABLE_ID.fullmatch(value)`: `[A-Za-z0-9][A-Za-z0-9._:-]{0,63}`.
fn is_stable_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.is_empty() || bytes.len() > 64 || !bytes[0].is_ascii_alphanumeric() {
        return false;
    }
    bytes[1..]
        .iter()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn stable_id(value: &Bound<'_, PyAny>, optional: bool) -> PyResult<()> {
    if optional && value.is_none() {
        return Ok(());
    }
    let text = value
        .downcast_exact::<PyString>()
        .map_err(|_| Unsupported::new_err("an ID is not a str"))?;
    // A lone surrogate has no UTF-8 form, and is not a stable ID either.
    match text.to_str() {
        Ok(text) if is_stable_id(text) => Ok(()),
        _ => Err(Unsupported::new_err("an ID is not stable")),
    }
}

fn plain_int(value: &Bound<'_, PyAny>) -> PyResult<i64> {
    value
        .downcast_exact::<PyInt>()
        .map_err(|_| Unsupported::new_err("a value is not an int"))?
        .extract::<i64>()
        .map_err(|_| Unsupported::new_err("an int is out of range"))
}

/// `_freeze_json(value)` of a value that `json.dumps(value, allow_nan=False)` accepts.
fn frozen<'py>(value: &Bound<'py, PyAny>, depth: usize) -> PyResult<Bound<'py, PyAny>> {
    if depth > MAX_FROZEN_DEPTH {
        return Err(Unsupported::new_err("value nests too deeply"));
    }
    if value.is_none()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyBool>()
    {
        return Ok(value.clone());
    }
    if let Ok(number) = value.downcast_exact::<PyFloat>() {
        if !number.value().is_finite() {
            return Err(Unsupported::new_err("NaN and infinity are left to Python"));
        }
        return Ok(value.clone());
    }
    if let Ok(object) = value.downcast_exact::<PyDict>() {
        return frozen_object(object, depth);
    }
    let py = value.py();
    if let Ok(array) = value.downcast_exact::<PyList>() {
        let items = array
            .iter()
            .map(|item| frozen(&item, depth + 1))
            .collect::<PyResult<Vec<_>>>()?;
        return Ok(PyTuple::new(py, items)?.into_any());
    }
    if let Ok(array) = value.downcast_exact::<PyTuple>() {
        let items = array
            .iter()
            .map(|item| frozen(&item, depth + 1))
            .collect::<PyResult<Vec<_>>>()?;
        return Ok(PyTuple::new(py, items)?.into_any());
    }
    Err(Unsupported::new_err("value is not a plain JSON value"))
}

fn frozen_object<'py>(object: &Bound<'py, PyDict>, depth: usize) -> PyResult<Bound<'py, PyAny>> {
    let py = object.py();
    let copy = PyDict::new(py);
    for (key, item) in object.iter() {
        if !key.is_exact_instance_of::<PyString>() {
            return Err(Unsupported::new_err("object key is not a str"));
        }
        copy.set_item(key, frozen(&item, depth + 1)?)?;
    }
    // SAFETY: the mapping is a live dict; the call returns a new reference or sets an error.
    unsafe {
        Bound::from_owned_ptr_or_err(py, ffi::PyDictProxy_New(copy.as_ptr()))
    }
}

/// `ScheduledEvent._check` apart from its `kind`: returns the frozen payload and provenance.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn scheduled_event_checked<'py>(
    event_id: &Bound<'py, PyAny>,
    event_type: &Bound<'py, PyAny>,
    source_id: &Bound<'py, PyAny>,
    causation_id: &Bound<'py, PyAny>,
    due_minute: &Bound<'py, PyAny>,
    sequence: &Bound<'py, PyAny>,
    priority: &Bound<'py, PyAny>,
    payload: &Bound<'py, PyAny>,
    provenance: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    stable_id(event_id, false)?;
    stable_id(event_type, false)?;
    stable_id(source_id, true)?;
    stable_id(causation_id, true)?;
    if plain_int(due_minute)? < 0 || plain_int(sequence)? < 0 {
        return Err(Unsupported::new_err("an event integer is negative"));
    }
    plain_int(priority)?;
    let as_object = |value: &Bound<'py, PyAny>| -> PyResult<Bound<'py, PyDict>> {
        Ok(value
            .downcast_exact::<PyDict>()
            .map_err(|_| Unsupported::new_err("not a dict"))?
            .clone())
    };
    Ok((
        frozen_object(&as_object(payload)?, 0)?,
        frozen_object(&as_object(provenance)?, 0)?,
    ))
}

fn key(encoder: &mut Encoder, first: bool, name: &str) {
    if !first {
        encoder.out.push(b',');
    }
    encoder.out.push(b'"');
    encoder.out.extend_from_slice(name.as_bytes());
    encoder.out.extend_from_slice(b"\":");
}

fn item<'py>(fields: &Bound<'py, PyDict>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    fields
        .get_item(name)?
        .ok_or_else(|| Unsupported::new_err("a view field is missing"))
}

/// The fields of one object in `sorted(names)` order. Names need no JSON escaping.
fn object_of(
    encoder: &mut Encoder,
    fields: &Bound<'_, PyDict>,
    names: &[(&str, Bound<'_, PyString>)],
) -> PyResult<()> {
    encoder.out.push(b'{');
    for (index, (text, name)) in names.iter().enumerate() {
        key(encoder, index == 0, text);
        encoder.value(&item(fields, name)?, 1)?;
    }
    encoder.out.push(b'}');
    Ok(())
}

fn names<'py>(py: Python<'py>, list: &[&'static str]) -> Vec<(&'static str, Bound<'py, PyString>)> {
    list.iter().map(|text| (*text, PyString::intern(py, text))).collect()
}

/// `canonical_sha256` of the document `_view_state_hash` builds, from the `__dict__` of
/// each AgentView and FacilityView. All keys are written in their sorted order.
#[pyfunction]
#[pyo3(signature = (minute_index, agents, facilities, resource_service_config, information_spread_config, life_context))]
pub fn view_state_hash<'py>(
    py: Python<'py>,
    minute_index: i64,
    agents: Vec<Bound<'py, PyDict>>,
    facilities: Vec<Bound<'py, PyDict>>,
    resource_service_config: Option<Bound<'py, PyAny>>,
    information_spread_config: Option<Bound<'py, PyAny>>,
    life_context: bool,
) -> PyResult<String> {
    let resource_service = resource_service_config.is_some();
    let information = information_spread_config.is_some();
    let mut encoder = Encoder { out: Vec::with_capacity(32 * 1024) };
    encoder.out.push(b'{');
    let mut first = true;
    if life_context {
        key(&mut encoder, true, "agent_life_context");
        first = false;
        let context = names(
            py,
            &[
                "care_for_ids",
                "caregiver_ids",
                "discontent",
                "hygiene_need",
                "negative_affect",
                "parent_ids",
                "personal_context_present",
                "positive_affect",
                "reassurance",
                "required_sleep_minutes",
                "roles",
                "sleep_debt_minutes",
                "uncertainty",
                "usual_sleep_minute",
                "usual_wake_minute",
            ],
        );
        let id_name = intern!(py, "id");
        encoder.out.push(b'[');
        for (index, fields) in agents.iter().enumerate() {
            if index > 0 {
                encoder.out.push(b',');
            }
            // "agent_id" sorts first and holds the view's "id".
            encoder.out.extend_from_slice(b"{\"agent_id\":");
            encoder.value(&item(fields, id_name)?, 1)?;
            for (text, name) in &context {
                key(&mut encoder, false, text);
                encoder.value(&item(fields, name)?, 1)?;
            }
            encoder.out.push(b'}');
        }
        encoder.out.push(b']');
    }
    key(&mut encoder, first, "agents");
    let before_needs = names(py, &["activity", "group_id", "id"]);
    let pressure = intern!(py, "information_query_pressure");
    let mobility = intern!(py, "mobility");
    let after_needs = names(
        py,
        &[
            "next_decision_at",
            "queue_kind",
            "queue_target_id",
            "queue_task_id",
            "queue_wait_minutes",
            "rng_key",
            "stress",
            "stress_sensitivity",
            "support_assigned_staff_id",
            "support_cooldown_remaining_minutes",
            "support_need",
            "support_request_id",
            "support_request_status",
            "support_wait_minutes",
            "target_id",
            "trust",
            "zone_id",
        ],
    );
    let needs_name = intern!(py, "needs");
    let fatigue = intern!(py, "fatigue");
    let hunger = intern!(py, "hunger");
    let hydration = intern!(py, "hydration_need");
    let toilet = intern!(py, "toilet_urgency");
    encoder.out.push(b'[');
    for (index, fields) in agents.iter().enumerate() {
        if index > 0 {
            encoder.out.push(b',');
        }
        encoder.out.push(b'{');
        for (position, (text, name)) in before_needs.iter().enumerate() {
            key(&mut encoder, position == 0, text);
            encoder.value(&item(fields, name)?, 1)?;
        }
        if information {
            key(&mut encoder, false, "information_query_pressure");
            encoder.value(&item(fields, pressure)?, 1)?;
        }
        key(&mut encoder, false, "mobility");
        encoder.value(&item(fields, mobility)?, 1)?;
        key(&mut encoder, false, "needs");
        let needs = item(fields, needs_name)?;
        encoder.out.extend_from_slice(b"{\"fatigue\":");
        encoder.value(&needs.getattr(fatigue)?, 2)?;
        encoder.out.extend_from_slice(b",\"hunger\":");
        encoder.value(&needs.getattr(hunger)?, 2)?;
        if resource_service {
            encoder.out.extend_from_slice(b",\"hydration_need\":");
            encoder.value(&needs.getattr(hydration)?, 2)?;
        }
        encoder.out.extend_from_slice(b",\"toilet_urgency\":");
        encoder.value(&needs.getattr(toilet)?, 2)?;
        encoder.out.push(b'}');
        for (text, name) in &after_needs {
            key(&mut encoder, false, text);
            encoder.value(&item(fields, name)?, 1)?;
        }
        encoder.out.push(b'}');
    }
    encoder.out.push(b']');
    key(&mut encoder, false, "facilities");
    let facility_names: Vec<&'static str> = if resource_service {
        vec![
            "active_agent_ids",
            "capacity",
            "id",
            "kind",
            "queue_agent_ids",
            "resource_active_agent_ids",
            "resource_queue_agent_ids",
            "working_units",
            "zone_id",
        ]
    } else {
        vec!["active_agent_ids", "capacity", "id", "kind", "queue_agent_ids", "working_units", "zone_id"]
    };
    let facility_names = names(py, &facility_names);
    encoder.out.push(b'[');
    for (index, fields) in facilities.iter().enumerate() {
        if index > 0 {
            encoder.out.push(b',');
        }
        object_of(&mut encoder, fields, &facility_names)?;
    }
    encoder.out.push(b']');
    if let Some(config) = &information_spread_config {
        key(&mut encoder, false, "information_spread_config");
        encoder.value(config, 1)?;
    }
    key(&mut encoder, false, "minute_index");
    encoder.out.extend_from_slice(minute_index.to_string().as_bytes());
    if let Some(config) = &resource_service_config {
        key(&mut encoder, false, "resource_service_config");
        encoder.value(config, 1)?;
    }
    encoder.out.push(b'}');
    let digest = Sha256::digest(&encoder.out);
    Ok(digest.iter().map(|byte| format!("{byte:02x}")).collect())
}
