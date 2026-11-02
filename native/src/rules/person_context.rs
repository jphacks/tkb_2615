//! `PersonContext.model_validate` (`contracts/population.py`) and `_fields_of_person`
//! (`policies/world_view.py`): what a view reads of a person's context.
//!
//! A context is checked as the contract checks it, so a document it refuses says `Unsupported` and
//! the Python rule raises its own text. Documents hold JSON values; pydantic's lax forms of other
//! Python types (a set for a tuple, bytes for a str) are refused, too. No Run makes them.

use std::collections::HashSet;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::core::tracked::{as_readable_dict, as_readable_list};
use crate::rules::world_view::PersonFields;
use crate::state::events::is_stable_id;
use crate::Unsupported;

const REFUSED: &str = "a person's context does not parse";

const ROLES: [&str; 6] = ["parent", "child", "caregiver", "dependent", "partner", "alone"];
const GUARDIAN_STATUSES: [&str; 3] = ["accompanied", "unaccompanied", "not_applicable"];
/// `SupportKind` of `contracts/operations.py`.
const SUPPORT_KINDS: [&str; 9] = [
    "medical_assessment",
    "medication_support",
    "caregiver_assistance",
    "privacy_accommodation",
    "mobility_assistance",
    "hygiene_support",
    "emotional_support",
    "language_access",
    "dependent_assistance",
];

fn refused<T>() -> PyResult<T> {
    Err(Unsupported::new_err(REFUSED))
}

fn text<'a>(value: &'a Bound<'_, PyAny>) -> PyResult<&'a str> {
    match value.downcast_exact::<PyString>() {
        Ok(text) => text.to_str().map_err(|_| Unsupported::new_err(REFUSED)),
        Err(_) => refused(),
    }
}

/// The items of a `tuple[..., ...]` field: a list, as documents hold it, or a tuple.
fn items<'py>(value: &Bound<'py, PyAny>, limit: usize) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let found: Vec<Bound<'py, PyAny>> = if let Some(list) = as_readable_list(value) {
        list.iter().collect()
    } else if let Ok(tuple) = value.downcast_exact::<PyTuple>() {
        tuple.iter().collect()
    } else {
        return refused();
    };
    if found.len() > limit {
        return refused();
    }
    Ok(found)
}

fn identifier(value: &Bound<'_, PyAny>) -> PyResult<Arc<str>> {
    let found = text(value)?;
    if is_stable_id(found) {
        Ok(Arc::from(found))
    } else {
        refused()
    }
}

fn identifiers(value: &Bound<'_, PyAny>, limit: usize) -> PyResult<Vec<Arc<str>>> {
    items(value, limit)?.iter().map(identifier).collect()
}

fn optional_identifier(value: &Bound<'_, PyAny>) -> PyResult<()> {
    if !value.is_none() {
        identifier(value)?;
    }
    Ok(())
}

/// A field of `bool`, in pydantic's lax mode: the bools, 0 and 1, and the words for them.
fn lax_bool(value: &Bound<'_, PyAny>) -> PyResult<()> {
    if value.is_instance_of::<PyBool>() {
        return Ok(());
    }
    if let Ok(number) = value.downcast_exact::<PyInt>() {
        return match number.extract::<i64>() {
            Ok(0 | 1) => Ok(()),
            _ => refused(),
        };
    }
    if let Ok(number) = value.downcast_exact::<PyFloat>() {
        let number = number.value();
        return if number == 0.0 || number == 1.0 { Ok(()) } else { refused() };
    }
    let word = text(value)?.to_ascii_lowercase();
    if matches!(&*word, "0" | "off" | "f" | "n" | "no" | "false" | "1" | "on" | "t" | "y" | "yes" | "true") {
        Ok(())
    } else {
        refused()
    }
}

fn optional_lax_bool(value: &Bound<'_, PyAny>) -> PyResult<()> {
    if value.is_none() {
        Ok(())
    } else {
        lax_bool(value)
    }
}

/// A field of `int | None = Field(strict=True, ge=0, le=high)`.
fn optional_int(value: &Bound<'_, PyAny>, high: i64) -> PyResult<Option<i64>> {
    if value.is_none() {
        return Ok(None);
    }
    match value.downcast_exact::<PyInt>().map(|number| number.extract::<i64>()) {
        Ok(Ok(number)) if (0..=high).contains(&number) => Ok(Some(number)),
        _ => refused(),
    }
}

/// `_fields_of_person(PersonContext.model_validate(raw))`.
pub(crate) fn parse(raw: &Bound<'_, PyAny>) -> PyResult<PersonFields> {
    let Some(record) = as_readable_dict(raw) else {
        return refused();
    };
    let mut fields = PersonFields { personal_context_present: true, ..PersonFields::default() };
    for (name, value) in record.iter() {
        match text(&name)? {
            "household_id" | "daily_rhythm_id" => optional_identifier(&value)?,
            "parent_ids" => fields.parent_ids = identifiers(&value, 4)?,
            "caregiver_ids" => fields.caregiver_ids = identifiers(&value, 16)?,
            "care_for_ids" => fields.care_for_ids = identifiers(&value, 16)?,
            "partner_ids" => {
                identifiers(&value, 16)?;
            }
            "relative_ids" => {
                identifiers(&value, 32)?;
            }
            "requires_review" => lax_bool(&value)?,
            "review_reasons" => {
                for reason in items(&value, 32)? {
                    text(&reason)?;
                }
            }
            "roles" => {
                let mut roles = Vec::new();
                for role in items(&value, usize::MAX)? {
                    let role = text(&role)?;
                    if !ROLES.contains(&role) {
                        return refused();
                    }
                    roles.push(Arc::from(role));
                }
                fields.roles = roles;
            }
            "medical_support" | "medication_support" => optional_lax_bool(&value)?,
            "guardian_status" => {
                if !value.is_none() && !GUARDIAN_STATUSES.contains(&text(&value)?) {
                    return refused();
                }
            }
            "care_support_needs" => {
                let mut seen = HashSet::new();
                for kind in items(&value, 8)? {
                    let kind = text(&kind)?.to_owned();
                    if !SUPPORT_KINDS.contains(&&*kind) || !seen.insert(kind) {
                        return refused();
                    }
                }
            }
            "pet_count" => {
                optional_int(&value, 20)?;
            }
            "information_access" => {
                if !value.is_none() {
                    identifiers(&value, usize::MAX)?;
                }
            }
            "usual_wake_minute" => fields.usual_wake_minute = optional_int(&value, 1439)?,
            "usual_sleep_minute" => fields.usual_sleep_minute = optional_int(&value, 1439)?,
            "required_sleep_minutes" => fields.required_sleep_minutes = optional_int(&value, 1440)?,
            "source_ids" => {
                identifiers(&value, usize::MAX)?;
            }
            _ => return refused(),
        }
    }
    Ok(fields)
}

/// A copy of a document that later writes cannot reach, as `deepcopy` makes it: what a view compares
/// the next document with. Containers come out plain; `==` does not tell them from tracked ones.
pub(crate) fn plain_copy<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    if let Some(record) = as_readable_dict(value) {
        let copy = PyDict::new(py);
        for (name, item) in record.iter() {
            copy.set_item(name, plain_copy(&item)?)?;
        }
        return Ok(copy.into_any());
    }
    if let Some(list) = as_readable_list(value) {
        let copy = PyList::empty(py);
        for item in list.iter() {
            copy.append(plain_copy(&item)?)?;
        }
        return Ok(copy.into_any());
    }
    if let Ok(tuple) = value.downcast_exact::<PyTuple>() {
        let items = tuple.iter().map(|item| plain_copy(&item)).collect::<PyResult<Vec<_>>>()?;
        return Ok(PyTuple::new(py, items)?.into_any());
    }
    if value.is_none()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>()
    {
        return Ok(value.clone());
    }
    refused()
}

/// The fields as `_fields_of_person` gives them, for the shadow and the tests.
#[pyfunction]
fn fields_of<'py>(raw: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let py = raw.py();
    let fields = parse(raw)?;
    let texts = |items: &[Arc<str>]| PyTuple::new(py, items.iter().map(|item| &**item));
    let result = PyDict::new(py);
    result.set_item("caregiver_ids", texts(&fields.caregiver_ids)?)?;
    result.set_item("care_for_ids", texts(&fields.care_for_ids)?)?;
    result.set_item("parent_ids", texts(&fields.parent_ids)?)?;
    result.set_item("roles", texts(&fields.roles)?)?;
    result.set_item("usual_wake_minute", fields.usual_wake_minute)?;
    result.set_item("usual_sleep_minute", fields.usual_sleep_minute)?;
    result.set_item("required_sleep_minutes", fields.required_sleep_minutes)?;
    result.set_item("personal_context_present", fields.personal_context_present)?;
    Ok(result)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(fields_of, m)?)?;
    Ok(())
}
