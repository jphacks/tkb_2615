//! Checkpoint save kernels (docs/rust-port.md, stage B3).
//!
//! `sensitive_candidates` reads a chunk's canonical JSON bytes and returns what the
//! privacy check of `ProjectStore` must look at: the distinct object keys, and the
//! distinct string values that its patterns could match. The patterns stay in
//! Python; a string left out here can not match them, for these reasons:
//!
//! - `_SECRET`'s address alternative needs "@".
//! - `sk-(?:ant-)?[A-Za-z0-9_-]{12,}` needs "sk-" and 12 more such characters.
//! - `Bearer\s+\S+` needs "bearer", in any case.
//! - `PHONE_NUMBER` needs two separators, each between digits.
//!
//! The patterns ignore case, under which U+017F matches "s" and U+212A matches "k",
//! and `\d` matches every Unicode decimal digit; a string with one of those is
//! returned whatever else it holds (`char::is_numeric` covers more than `\d`).

use std::collections::HashSet;

use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use rayon::prelude::*;
use sha2::{Digest, Sha256};

const SK_RUN: usize = 12;

fn token_char(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-'
}

fn starts_ignoring_case(bytes: &[u8], lower: &[u8]) -> bool {
    bytes.len() >= lower.len() && bytes[..lower.len()].eq_ignore_ascii_case(lower)
}

/// Whether the patterns could match text, the UTF-8 of one decoded JSON string.
fn could_match(text: &[u8]) -> bool {
    let mut separators = 0;
    for (index, &byte) in text.iter().enumerate() {
        match byte {
            b'@' => return true,
            b's' | b'S' if starts_ignoring_case(&text[index..], b"sk-") => {
                let run = text[index + 3..].iter().take(SK_RUN).take_while(|b| token_char(**b));
                if run.count() == SK_RUN {
                    return true;
                }
            }
            b'b' | b'B' if starts_ignoring_case(&text[index..], b"bearer") => return true,
            b'-' | b' ' => {
                let digit_before = index > 0 && text[index - 1].is_ascii_digit();
                let digit_after = text.get(index + 1).is_some_and(u8::is_ascii_digit);
                if digit_before && digit_after {
                    separators += 1;
                    if separators == 2 {
                        return true;
                    }
                }
            }
            _ => {}
        }
    }
    if text.is_ascii() {
        return false;
    }
    match std::str::from_utf8(text) {
        Ok(decoded) => decoded
            .chars()
            .any(|c| c == '\u{17f}' || c == '\u{212a}' || (!c.is_ascii() && c.is_numeric())),
        Err(_) => true,
    }
}

fn decode(raw: &[u8]) -> Result<String, String> {
    serde_json::from_slice(raw).map_err(|error| error.to_string())
}

/// The distinct keys and the distinct string values to check in raw, one JSON text.
fn candidates(raw: &[u8]) -> Result<(HashSet<String>, HashSet<String>), String> {
    let mut keys: HashSet<String> = HashSet::new();
    let mut strings: HashSet<String> = HashSet::new();
    let mut seen_keys: HashSet<&[u8]> = HashSet::new();
    let mut seen_strings: HashSet<&[u8]> = HashSet::new();
    let mut index = 0;
    let length = raw.len();
    while index < length {
        if raw[index] != b'"' {
            index += 1;
            continue;
        }
        let start = index;
        index += 1;
        let mut escaped = false;
        while index < length && raw[index] != b'"' {
            if raw[index] == b'\\' {
                escaped = true;
                index += 1;
            }
            index += 1;
        }
        if index >= length {
            return Err("unterminated JSON string".to_owned());
        }
        index += 1;
        let quoted = &raw[start..index];
        // In JSON only an object key is followed by ":".
        if raw.get(index) == Some(&b':') {
            if seen_keys.insert(quoted) {
                keys.insert(decode(quoted)?);
            }
        } else if seen_strings.insert(quoted) {
            if escaped {
                let text = decode(quoted)?;
                if could_match(text.as_bytes()) {
                    strings.insert(text);
                }
            } else if could_match(&quoted[1..quoted.len() - 1]) {
                strings.insert(decode(quoted)?);
            }
        }
    }
    Ok((keys, strings))
}

/// The distinct object keys of the JSON texts raws, and their distinct string values
/// the privacy patterns could match, read in parallel without the GIL. Raises ValueError
/// when a string can not be read.
#[pyfunction]
pub fn sensitive_candidates(
    py: Python<'_>,
    raws: Vec<Bound<'_, PyBytes>>,
) -> PyResult<(Vec<String>, Vec<String>)> {
    if let [raw] = raws.as_slice() {
        let (keys, strings) =
            candidates(raw.as_bytes()).map_err(pyo3::exceptions::PyValueError::new_err)?;
        return Ok((keys.into_iter().collect(), strings.into_iter().collect()));
    }
    let slices: Vec<&[u8]> = raws.iter().map(|raw| raw.as_bytes()).collect();
    let (keys, strings) = py
        .allow_threads(|| {
            slices
                .par_iter()
                .map(|raw| candidates(raw))
                .try_reduce(
                    || (HashSet::new(), HashSet::new()),
                    |(mut keys, mut strings), (more_keys, more_strings)| {
                        keys.extend(more_keys);
                        strings.extend(more_strings);
                        Ok((keys, strings))
                    },
                )
        })
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    Ok((keys.into_iter().collect(), strings.into_iter().collect()))
}

/// The SHA-256 hex digest of `[item,item,...]` for each (start, count) of items, a list
/// of bytes, computed in parallel without the GIL and without joining the items.
#[pyfunction]
pub fn chunk_hashes(
    py: Python<'_>,
    items: &Bound<'_, PyList>,
    chunks: Vec<(usize, usize)>,
) -> PyResult<Vec<String>> {
    let mut held: Vec<Vec<Bound<'_, PyBytes>>> = Vec::with_capacity(chunks.len());
    for (start, count) in &chunks {
        let mut chunk = Vec::with_capacity(*count);
        for index in *start..*start + *count {
            chunk.push(items.get_item(index)?.downcast_into::<PyBytes>()?);
        }
        held.push(chunk);
    }
    let slices: Vec<Vec<&[u8]>> = held
        .iter()
        .map(|chunk| chunk.iter().map(|item| item.as_bytes()).collect())
        .collect();
    Ok(py.allow_threads(|| {
        slices
            .par_iter()
            .map(|chunk| {
                let mut hasher = Sha256::new();
                hasher.update(b"[");
                for (index, item) in chunk.iter().enumerate() {
                    if index > 0 {
                        hasher.update(b",");
                    }
                    hasher.update(item);
                }
                hasher.update(b"]");
                hasher.finalize().iter().map(|byte| format!("{byte:02x}")).collect()
            })
            .collect()
    }))
}

// Keys of an index row that link it to events, by how the row holds them.
const EVENT_REFERENCE_KEYS: [&str; 2] = ["event_id", "causation_id"];
const EVENT_REFERENCE_LISTS: [&str; 5] = [
    "event_ids",
    "source_event_ids",
    "causation_event_ids",
    "modifier_event_ids",
    "environmental_stressor_event_ids",
];
// Deeper rows are left to Python.
const MAX_ROW_DEPTH: usize = 64;

fn strings_of<'py>(list: &Bound<'py, PyList>, into: &mut Vec<Bound<'py, PyString>>) {
    for item in list.iter() {
        if let Ok(text) = item.downcast_into_exact::<PyString>() {
            into.push(text);
        }
    }
}

/// `_identities` and `_event_references` of `storage/history_index.py` over one value.
/// False for a value that is not plain lists, str-keyed dicts and JSON scalars.
fn row_links<'py>(
    value: &Bound<'py, PyAny>,
    identities: &mut Vec<Bound<'py, PyString>>,
    references: &mut Vec<Bound<'py, PyString>>,
    depth: usize,
) -> PyResult<bool> {
    if depth == 0 {
        return Ok(false);
    }
    if let Ok(object) = value.downcast_exact::<PyDict>() {
        for (key, item) in object.iter() {
            let Ok(name) = key.downcast_exact::<PyString>() else {
                return Ok(false);
            };
            let name = name.to_str()?;
            let container = item.is_exact_instance_of::<PyDict>() || item.is_exact_instance_of::<PyList>();
            let identity = name.ends_with("_id") || name.ends_with("_ids");
            if identity {
                if let Ok(text) = item.downcast_exact::<PyString>() {
                    identities.push(text.clone());
                } else if let Ok(list) = item.downcast_exact::<PyList>() {
                    strings_of(list, identities);
                }
            }
            let single = EVENT_REFERENCE_KEYS.contains(&name);
            if single && item.is_exact_instance_of::<PyString>() {
                references.push(item.clone().downcast_into_exact::<PyString>()?);
            } else if EVENT_REFERENCE_LISTS.contains(&name) && item.is_exact_instance_of::<PyList>() {
                strings_of(item.downcast_exact::<PyList>()?, references);
            } else if container {
                // Identities are not read under an identity key; references are.
                let mut unread = Vec::new();
                let into = if identity { &mut unread } else { &mut *identities };
                if !row_links(&item, into, references, depth - 1)? {
                    return Ok(false);
                }
            } else if !plain_scalar(&item) {
                return Ok(false);
            }
        }
        Ok(true)
    } else if let Ok(list) = value.downcast_exact::<PyList>() {
        for item in list.iter() {
            if !row_links(&item, identities, references, depth - 1)? {
                return Ok(false);
            }
        }
        Ok(true)
    } else {
        Ok(plain_scalar(value))
    }
}

fn plain_scalar(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyFloat>()
}

/// What `HistoryIndex` stores of one row of plain lists, str-keyed dicts and JSON scalars:
/// its canonical JSON, the SHA-256 of that, its identities and event references, and the
/// keys and strings the privacy check reads. None for any other row.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn index_row<'py>(
    py: Python<'py>,
    row: &Bound<'py, PyDict>,
) -> PyResult<
    Option<(
        Bound<'py, PyString>,
        String,
        Vec<Bound<'py, PyString>>,
        Vec<Bound<'py, PyString>>,
        Vec<String>,
        Vec<String>,
    )>,
> {
    let mut identities = Vec::new();
    let mut references = Vec::new();
    if !row_links(row.as_any(), &mut identities, &mut references, MAX_ROW_DEPTH)? {
        return Ok(None);
    }
    let Ok(raw) = crate::encode(row.as_any()) else {
        return Ok(None);
    };
    let (keys, strings) =
        candidates(&raw).map_err(pyo3::exceptions::PyValueError::new_err)?;
    let hash = Sha256::digest(&raw).iter().map(|byte| format!("{byte:02x}")).collect();
    let text = std::str::from_utf8(&raw).map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
    Ok(Some((
        PyString::new(py, text),
        hash,
        identities,
        references,
        keys.into_iter().collect(),
        strings.into_iter().collect(),
    )))
}

// Deeper values are left to the caller, which compares the restored records.
const MAX_SAME_DEPTH: usize = 64;

/// Whether value, from a live record, is exactly what reading document gives once frozen:
/// the same types (an int is not a float), with the text of each number as Python writes
/// it. Anything else, such as a subclass or a plain dict, is reported as different.
fn same_frozen(value: &Bound<'_, PyAny>, document: &serde_json::Value, depth: usize) -> PyResult<bool> {
    use serde_json::Value;
    if depth == 0 {
        return Ok(false);
    }
    Ok(match document {
        Value::Null => value.is_none(),
        Value::Bool(flag) => {
            value.is_exact_instance_of::<PyBool>() && value.downcast_exact::<PyBool>()?.is_true() == *flag
        }
        Value::String(text) => match value.downcast_exact::<PyString>() {
            Ok(string) => string.to_str()? == text,
            Err(_) => false,
        },
        Value::Number(number) => {
            let text = number.to_string();
            if let Ok(float) = value.downcast_exact::<PyFloat>() {
                crate::python_float_repr(float.value()) == text
            } else if value.is_exact_instance_of::<PyInt>() {
                value.str()?.to_str()? == text
            } else {
                false
            }
        }
        Value::Array(items) => match value.downcast_exact::<PyTuple>() {
            Ok(tuple) if tuple.len() == items.len() => {
                for (item, document) in tuple.iter().zip(items) {
                    if !same_frozen(&item, document, depth - 1)? {
                        return Ok(false);
                    }
                }
                true
            }
            _ => false,
        },
        Value::Object(object) => {
            // SAFETY: only the address of the type object is read.
            let proxy = unsafe { std::ptr::addr_of_mut!(ffi::PyDictProxy_Type) };
            if value.get_type().as_ptr() != proxy.cast() || value.len()? != object.len() {
                return Ok(false);
            }
            for (key, document) in object {
                // The counts are equal, so finding every key finds them all.
                match value.get_item(key) {
                    Ok(item) => {
                        if !same_frozen(&item, document, depth - 1)? {
                            return Ok(false);
                        }
                    }
                    Err(error) if error.is_instance_of::<pyo3::exceptions::PyKeyError>(value.py()) => {
                        return Ok(false)
                    }
                    Err(error) => return Err(error),
                }
            }
            true
        }
    })
}

fn same_attribute(owner: &Bound<'_, PyAny>, name: &str, document: &serde_json::Value) -> PyResult<bool> {
    match document.get(name) {
        Some(field) => same_frozen(&owner.getattr(name)?, field, MAX_SAME_DEPTH),
        None => Ok(false),
    }
}

fn same_enum(owner: &Bound<'_, PyAny>, name: &str, document: &serde_json::Value) -> PyResult<bool> {
    match document.get(name) {
        Some(field) => same_frozen(&owner.getattr(name)?.getattr("value")?, field, 1),
        None => Ok(false),
    }
}

fn same_record(
    record: &Bound<'_, PyAny>,
    document: &serde_json::Value,
    types: (&Bound<'_, PyAny>, &Bound<'_, PyAny>),
) -> PyResult<bool> {
    use crate::eventlog::{EVENT_FIELDS, RECORD_FIELDS};
    let exact = |value: &serde_json::Value, count: usize| value.as_object().is_some_and(|fields| fields.len() == count);
    let Some(event_document) = document.get("event") else {
        return Ok(false);
    };
    if !exact(document, RECORD_FIELDS.len()) || !exact(event_document, EVENT_FIELDS.len()) {
        return Ok(false);
    }
    if !record.get_type().is(types.0) {
        return Ok(false);
    }
    let event = record.getattr("event")?;
    if !event.get_type().is(types.1) {
        return Ok(false);
    }
    for name in RECORD_FIELDS {
        let same = match name {
            "event" => true,
            "status" => same_enum(record, name, document)?,
            _ => same_attribute(record, name, document)?,
        };
        if !same {
            return Ok(false);
        }
    }
    for name in EVENT_FIELDS {
        let same = if name == "kind" {
            same_enum(&event, name, event_document)?
        } else {
            same_attribute(&event, name, event_document)?
        };
        if !same {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Whether each of records is exactly the record that restoring the saved bytes of the
/// same position gives, read without building that record. A false says nothing about
/// `==`; the caller then compares restored records.
#[pyfunction]
pub fn same_saved_records(
    py: Python<'_>,
    records: &Bound<'_, PyList>,
    raws: Vec<Bound<'_, PyBytes>>,
    record_type: &Bound<'_, PyAny>,
    event_type: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    if records.len() != raws.len() {
        return Ok(false);
    }
    let slices: Vec<&[u8]> = raws.iter().map(|raw| raw.as_bytes()).collect();
    let documents: Result<Vec<serde_json::Value>, _> = py.allow_threads(|| {
        slices
            .par_iter()
            .map(|raw| serde_json::from_slice(raw))
            .collect()
    });
    let Ok(documents) = documents else {
        return Ok(false);
    };
    for (record, document) in records.iter().zip(&documents) {
        if !same_record(&record, document, (record_type, event_type))? {
            return Ok(false);
        }
    }
    Ok(true)
}
