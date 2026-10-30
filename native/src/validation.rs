//! Per-tick validation of large state dicts, read where Python keeps them.
//!
//! Each check accepts and rejects exactly what the Python code it replaces does, in
//! the same order, and returns which rule the first bad entry broke; the caller
//! raises the same error as before.

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet};

/// isinstance(value, int) and not isinstance(value, bool) and value >= 0
fn is_count(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    if value.downcast::<PyBool>().is_ok() {
        return Ok(false);
    }
    let Ok(number) = value.downcast::<PyInt>() else {
        return Ok(false);
    };
    match number.extract::<i64>() {
        Ok(small) => Ok(small >= 0),
        // An int too large for i64 compares like any int.
        Err(_) => Ok(!number.lt(0)?),
    }
}

/// trust is None, or an int or float (not bool) within 0..1
fn is_trust(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    if value.is_none() {
        return Ok(true);
    }
    if value.downcast::<PyBool>().is_ok() {
        return Ok(false);
    }
    if let Ok(number) = value.downcast::<PyFloat>() {
        let trust = number.value();
        return Ok((0.0..=1.0).contains(&trust));
    }
    if let Ok(number) = value.downcast::<PyInt>() {
        return Ok(match number.extract::<i64>() {
            Ok(small) => (0..=1).contains(&small),
            Err(_) => false,
        });
    }
    Ok(false)
}

const SOCIAL_FIELDS: [&str; 7] = [
    "contact_count",
    "conversation_count",
    "cooperation_count",
    "trust",
    "shared_message_ids",
    "last_contact_minute",
    "last_interaction",
];

/// The first rule a social_network_v1 relationship breaks, in validation order:
/// 1 unparsable key, 2 key that is not a pair or malformed record, 3 trust,
/// 4 contact minute; None when every relationship is valid.
///
/// good_keys holds str keys whose check found no problem; key_problem is the
/// Python check for any other key, and a str key it accepts is added.
#[pyfunction]
pub fn social_relationships_problem(
    relationships: &Bound<'_, PyDict>,
    good_keys: &Bound<'_, PySet>,
    key_problem: &Bound<'_, PyAny>,
) -> PyResult<Option<u8>> {
    let py = relationships.py();
    let fields: Vec<Bound<'_, PyAny>> = SOCIAL_FIELDS
        .iter()
        .map(|name| pyo3::types::PyString::new(py, name).into_any())
        .collect();
    for (key, record) in relationships.iter() {
        let known = key.is_instance_of::<pyo3::types::PyString>() && good_keys.contains(&key)?;
        if !known {
            let problem = key_problem.call1((&key,))?;
            if !problem.is_none() {
                let text: String = problem.extract()?;
                return Ok(Some(if text == "unparsable" { 1 } else { 2 }));
            }
            if key.is_instance_of::<pyo3::types::PyString>() {
                good_keys.add(&key)?;
            }
        }
        let Ok(record) = record.downcast::<PyDict>() else {
            return Ok(Some(2));
        };
        if record.len() != fields.len() {
            return Ok(Some(2));
        }
        for field in &fields {
            if !record.contains(field)? {
                return Ok(Some(2));
            }
        }
        for field in &fields[..3] {
            if !is_count(&record.get_item(field)?.unwrap())? {
                return Ok(Some(2));
            }
        }
        if record.get_item(&fields[4])?.unwrap().downcast::<PyList>().is_err() {
            return Ok(Some(2));
        }
        if !is_trust(&record.get_item(&fields[3])?.unwrap())? {
            return Ok(Some(3));
        }
        let minute = record.get_item(&fields[5])?.unwrap();
        if !minute.is_none() && !is_count(&minute)? {
            return Ok(Some(4));
        }
    }
    Ok(None)
}

/// type(value) is int and value >= 0
fn is_exact_count(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    if !value.is_exact_instance_of::<PyInt>() {
        return Ok(false);
    }
    let number = value.downcast::<PyInt>()?;
    match number.extract::<i64>() {
        Ok(small) => Ok(small >= 0),
        Err(_) => Ok(!number.lt(0)?),
    }
}

const BODY_FIELDS: [&str; 6] = [
    "contact_count",
    "trust",
    "contact_minutes",
    "last_contact_minute",
    "last_interaction",
    "topic_ids",
];

/// The first rule a body_state_v1 relationship breaks, in validation order:
/// 1 unparsable key, 2 key or record, 3 trust, 4 contact minute; None when all are
/// valid. parsed_keys maps str keys to their parse, (pair or None, compact);
/// parse_key is the Python parse for any other key. Whether the pair's people are
/// in agents is checked every time.
#[pyfunction]
pub fn body_relationships_problem(
    relationships: &Bound<'_, PyDict>,
    agents: &Bound<'_, PyDict>,
    parsed_keys: &Bound<'_, PyDict>,
    parse_key: &Bound<'_, PyAny>,
) -> PyResult<Option<u8>> {
    let py = relationships.py();
    let fields: Vec<Bound<'_, PyAny>> = BODY_FIELDS
        .iter()
        .map(|name| pyo3::types::PyString::new(py, name).into_any())
        .collect();
    for (key, record) in relationships.iter() {
        let is_str = key.is_instance_of::<pyo3::types::PyString>();
        let parsed = match if is_str { parsed_keys.get_item(&key)? } else { None } {
            Some(parsed) => parsed,
            None => {
                let parsed = parse_key.call1((&key,))?;
                if is_str {
                    parsed_keys.set_item(&key, &parsed)?;
                }
                parsed
            }
        };
        if parsed.is_none() {
            return Ok(Some(1));
        }
        let pair = parsed.get_item(0)?;
        let compact = parsed.get_item(1)?.is_truthy()?;
        if pair.is_none() {
            return Ok(Some(2));
        }
        let first = pair.get_item(0)?;
        let second = pair.get_item(1)?;
        if !agents.contains(&first)? || !agents.contains(&second)? || first.eq(&second)? || !compact
        {
            return Ok(Some(2));
        }
        let Ok(record) = record.downcast::<PyDict>() else {
            return Ok(Some(2));
        };
        if record.len() != fields.len() {
            return Ok(Some(2));
        }
        for field in &fields {
            if !record.contains(field)? {
                return Ok(Some(2));
            }
        }
        if !is_exact_count(&record.get_item(&fields[0])?.unwrap())?
            || !is_exact_count(&record.get_item(&fields[2])?.unwrap())?
        {
            return Ok(Some(2));
        }
        let topics = record.get_item(&fields[5])?.unwrap();
        let Ok(topics) = topics.downcast::<PyList>() else {
            return Ok(Some(2));
        };
        if topics.len() > 32 {
            return Ok(Some(2));
        }
        for topic in topics.iter() {
            let Ok(text) = topic.downcast::<pyo3::types::PyString>() else {
                return Ok(Some(2));
            };
            if text.len()? > 64 {
                return Ok(Some(2));
            }
        }
        if !is_trust(&record.get_item(&fields[1])?.unwrap())? {
            return Ok(Some(3));
        }
        let minute = record.get_item(&fields[3])?.unwrap();
        if !minute.is_none() && !is_exact_count(&minute)? {
            return Ok(Some(4));
        }
    }
    Ok(None)
}
