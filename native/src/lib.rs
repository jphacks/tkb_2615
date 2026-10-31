//! Rust kernels for ShelterSim (docs/rust-port.md).
//!
//! `canonical_json` writes the bytes of
//! `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
//! allow_nan=False).encode()` for plain JSON values: dict with str keys, list,
//! tuple, str, int, float, bool and None, and the tracked containers (`core/tracked.rs`). Anything else, and NaN or infinity,
//! raises `Unsupported`, so the Python caller falls back to the json module and
//! keeps its exact results and errors.

use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyFloat, PyInt, PyString, PyTuple};
use sha2::{Digest, Sha256};

use crate::core::tracked::{as_readable_dict, as_readable_list};

mod checkpoint;
mod contacts;
mod core;
mod eventlog;
mod history;
mod psychology;
mod records;
mod validation;
mod watch;

// The `state` and `rules` modules, found by build.rs.
include!(concat!(env!("OUT_DIR"), "/lanes.rs"));

create_exception!(sheltersim_native, Unsupported, PyException);

// Deeper values are left to the json module, which raises RecursionError itself.
pub(crate) const MAX_DEPTH: usize = 512;

pub(crate) struct Encoder {
    pub(crate) out: Vec<u8>,
}

impl Encoder {
    pub(crate) fn value(&mut self, value: &Bound<'_, PyAny>, depth: usize) -> PyResult<()> {
        if depth > MAX_DEPTH {
            return Err(Unsupported::new_err("value nests too deeply"));
        }
        if value.is_none() {
            self.out.extend_from_slice(b"null");
        } else if let Ok(text) = value.downcast::<PyString>() {
            self.string(text.to_str()?);
        } else if let Ok(flag) = value.downcast::<PyBool>() {
            self.out
                .extend_from_slice(if flag.is_true() { b"true" } else { b"false" });
        } else if let Ok(number) = value.downcast::<PyInt>() {
            match number.extract::<i64>() {
                Ok(small) => self.out.extend_from_slice(itoa::Buffer::new().format(small).as_bytes()),
                // json writes int.__repr__ of any other int.
                Err(_) => {
                    let text = py_int_repr(number)?;
                    self.out.extend_from_slice(text.as_bytes());
                }
            }
        } else if let Ok(number) = value.downcast::<PyFloat>() {
            self.float(number.value())?;
        } else if let Some(object) = as_readable_dict(value) {
            let mut entries: Vec<(Bound<'_, PyAny>, Bound<'_, PyAny>)> =
                Vec::with_capacity(object.len());
            for (key, item) in object.iter() {
                entries.push((key, item));
            }
            let mut items: Vec<(&str, &Bound<'_, PyAny>)> = Vec::with_capacity(entries.len());
            for (key, item) in &entries {
                let key = key
                    .downcast::<PyString>()
                    .map_err(|_| Unsupported::new_err("object key is not a str"))?;
                items.push((key.to_str()?, item));
            }
            // Python orders str by code point, the same as UTF-8 bytes.
            items.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
            self.out.push(b'{');
            for (index, (key, item)) in items.iter().enumerate() {
                if index > 0 {
                    self.out.push(b',');
                }
                self.string(key);
                self.out.push(b':');
                self.value(item, depth + 1)?;
            }
            self.out.push(b'}');
        } else if let Some(array) = as_readable_list(value) {
            self.out.push(b'[');
            for (index, item) in array.iter().enumerate() {
                if index > 0 {
                    self.out.push(b',');
                }
                self.value(&item, depth + 1)?;
            }
            self.out.push(b']');
        } else if let Ok(array) = value.downcast::<PyTuple>() {
            self.out.push(b'[');
            for (index, item) in array.iter().enumerate() {
                if index > 0 {
                    self.out.push(b',');
                }
                self.value(&item, depth + 1)?;
            }
            self.out.push(b']');
        } else {
            return Err(Unsupported::new_err("value is not a plain JSON value"));
        }
        Ok(())
    }

    fn string(&mut self, text: &str) {
        write_json_string(&mut self.out, text);
    }

    pub(crate) fn float(&mut self, value: f64) -> PyResult<()> {
        if !value.is_finite() {
            return Err(Unsupported::new_err("NaN and infinity are left to json"));
        }
        // In this range ryu and float.__repr__ both write the shortest digits in fixed
        // notation with at least one digit after the point.
        if (1e-4..1e16).contains(&value.abs()) {
            let mut buffer = ryu::Buffer::new();
            self.out.extend_from_slice(buffer.format_finite(value).as_bytes());
        } else {
            self.out.extend_from_slice(python_float_repr(value).as_bytes());
        }
        Ok(())
    }
}

/// A JSON string as json.dumps(ensure_ascii=False) writes it.
pub(crate) fn write_json_string(out: &mut Vec<u8>, text: &str) {
    // json's ensure_ascii=False escapes only '"', '\\' and control characters.
    out.push(b'"');
    let bytes = text.as_bytes();
    let mut start = 0;
    for (index, &byte) in bytes.iter().enumerate() {
        let escape: &[u8] = match byte {
            b'"' => b"\\\"",
            b'\\' => b"\\\\",
            b'\n' => b"\\n",
            b'\r' => b"\\r",
            b'\t' => b"\\t",
            0x08 => b"\\b",
            0x0c => b"\\f",
            0x00..=0x1f => b"",
            _ => continue,
        };
        out.extend_from_slice(&bytes[start..index]);
        if escape.is_empty() {
            out
                .extend_from_slice(format!("\\u{:04x}", byte).as_bytes());
        } else {
            out.extend_from_slice(escape);
        }
        start = index + 1;
    }
    out.extend_from_slice(&bytes[start..]);
    out.push(b'"');
}


fn py_int_repr(number: &Bound<'_, PyInt>) -> PyResult<String> {
    let int_type = number.py().get_type::<PyInt>();
    int_type
        .getattr("__repr__")?
        .call1((number,))?
        .extract::<String>()
}

/// float.__repr__: the shortest digits that read back as the same float, in fixed
/// notation when the decimal point falls within 16 digits of the first digit.
pub fn python_float_repr(value: f64) -> String {
    if value == 0.0 {
        return if value.is_sign_negative() { "-0.0" } else { "0.0" }.to_owned();
    }
    let mut buffer = ryu::Buffer::new();
    let shortest = buffer.format_finite(value);
    let (negative, unsigned) = match shortest.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, shortest),
    };
    let (mantissa, exponent) = match unsigned.split_once(['e', 'E']) {
        Some((mantissa, exponent)) => (mantissa, exponent.parse::<i32>().unwrap()),
        None => (unsigned, 0),
    };
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let mut digits: String = format!("{whole}{fraction}");
    let mut point = whole.len() as i32 + exponent;
    let leading = digits.len() - digits.trim_start_matches('0').len();
    digits.drain(..leading);
    point -= leading as i32;
    let trimmed = digits.trim_end_matches('0').len();
    digits.truncate(trimmed);

    let mut text = String::with_capacity(32);
    if negative {
        text.push('-');
    }
    let count = digits.len() as i32;
    if point <= -4 || point > 16 {
        text.push_str(&digits[..1]);
        if count > 1 {
            text.push('.');
            text.push_str(&digits[1..]);
        }
        let shown = point - 1;
        text.push('e');
        text.push(if shown < 0 { '-' } else { '+' });
        text.push_str(&format!("{:02}", shown.abs()));
    } else if point <= 0 {
        text.push_str("0.");
        text.push_str(&"0".repeat((-point) as usize));
        text.push_str(&digits);
    } else if point >= count {
        text.push_str(&digits);
        text.push_str(&"0".repeat((point - count) as usize));
        text.push_str(".0");
    } else {
        text.push_str(&digits[..point as usize]);
        text.push('.');
        text.push_str(&digits[point as usize..]);
    }
    text
}

pub(crate) fn encode(value: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    let mut encoder = Encoder {
        out: Vec::with_capacity(256),
    };
    encoder.value(value, 0)?;
    Ok(encoder.out)
}

/// The canonical JSON bytes of a plain JSON value.
#[pyfunction]
fn canonical_json<'py>(py: Python<'py>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    let bytes = encode(value)?;
    Ok(PyBytes::new(py, &bytes))
}

/// The SHA-256 hex digest of canonical_json(value).
#[pyfunction]
fn canonical_sha256(value: &Bound<'_, PyAny>) -> PyResult<String> {
    let bytes = encode(value)?;
    let digest = Sha256::digest(&bytes);
    Ok(digest.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// float.__repr__ of value, for tests.
#[pyfunction]
fn float_repr(value: f64) -> String {
    python_float_repr(value)
}

/// The JSON of a float as the Encoder writes it, for tests.
#[pyfunction]
fn float_json(value: f64) -> PyResult<String> {
    let mut encoder = Encoder { out: Vec::with_capacity(32) };
    encoder.float(value)?;
    Ok(String::from_utf8(encoder.out).expect("a float is ASCII"))
}

/// One minute of the mean-reverting life-state psychology for every agent.
/// `globals` has 23 floats and each agent 32; see psychology.rs.
#[pyfunction]
fn psychology_integrated(globals: Vec<f64>, agents: Vec<Vec<f64>>) -> PyResult<Vec<[f64; 12]>> {
    let globals = psychology::Globals::from_slice(&globals)
        .ok_or_else(|| Unsupported::new_err("psychology globals have the wrong length"))?;
    agents
        .iter()
        .map(|agent| {
            if agent.len() != psychology::AGENT_FIELDS {
                return Err(Unsupported::new_err("agent inputs have the wrong length"));
            }
            let row = psychology::step(&globals, agent)
                .ok_or_else(|| Unsupported::new_err("math.exp would raise"))?;
            // Values outside AgentStateDelta's bounds are left to its Python checks.
            let in_bounds = |value: f64| (0.0..=1.0).contains(&value);
            if !row[..5].iter().copied().all(in_bounds)
                || !(0.0..=1440.0).contains(&row[5])
                || !row[6..].iter().copied().all(in_bounds)
            {
                return Err(Unsupported::new_err("a new value is out of bounds"));
            }
            Ok(row)
        })
        .collect()
}

#[pymodule]
fn sheltersim_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("Unsupported", module.py().get_type::<Unsupported>())?;
    module.add_function(wrap_pyfunction!(core::tracked::install_tracked_types, module)?)?;
    module.add_function(wrap_pyfunction!(core::tracked::install_id_map_type, module)?)?;
    module.add_class::<core::world_core::WorldCore>()?;
    module.add_class::<core::world_core::NamespaceMirror>()?;
    module.add_class::<core::world_core::RowType>()?;
    register_lanes(module)?;
    module.add_function(wrap_pyfunction!(canonical_json, module)?)?;
    module.add_function(wrap_pyfunction!(canonical_sha256, module)?)?;
    module.add_function(wrap_pyfunction!(float_repr, module)?)?;
    module.add_function(wrap_pyfunction!(float_json, module)?)?;
    module.add_function(wrap_pyfunction!(psychology_integrated, module)?)?;
    module.add_function(wrap_pyfunction!(records::scheduled_event_checked, module)?)?;
    module.add_function(wrap_pyfunction!(records::view_state_hash, module)?)?;
    module.add_function(wrap_pyfunction!(checkpoint::sensitive_candidates, module)?)?;
    module.add_function(wrap_pyfunction!(checkpoint::chunk_hashes, module)?)?;
    module.add_function(wrap_pyfunction!(checkpoint::same_saved_records, module)?)?;
    module.add_function(wrap_pyfunction!(checkpoint::index_row, module)?)?;
    module.add_function(wrap_pyfunction!(checkpoint::dumps_length, module)?)?;
    module.add_function(wrap_pyfunction!(checkpoint::record_json, module)?)?;
    module.add_function(wrap_pyfunction!(history::same_json_items, module)?)?;
    module.add_class::<history::HistoryPins>()?;
    module.add_function(wrap_pyfunction!(validation::social_relationships_valid, module)?)?;
    module.add_function(wrap_pyfunction!(validation::body_relationships_valid, module)?)?;
    module.add_function(wrap_pyfunction!(validation::body_agents_valid, module)?)?;
    module.add_function(wrap_pyfunction!(validation::psychological_agents_valid, module)?)?;
    module.add_function(wrap_pyfunction!(validation::agent_dynamics_valid, module)?)?;
    module.add_function(wrap_pyfunction!(validation::movement_positions_valid, module)?)?;
    module.add_function(wrap_pyfunction!(validation::information_contacts_valid, module)?)?;
    module.add_function(wrap_pyfunction!(validation::unit_values_valid, module)?)?;
    module.add_function(wrap_pyfunction!(validation::world_agents_valid, module)?)?;
    module.add_class::<validation::ValuesSnapshot>()?;
    module.add_function(wrap_pyfunction!(watch::install_dict_watcher, module)?)?;
    module.add_class::<watch::RecordWatch>()?;
    module.add_function(wrap_pyfunction!(contacts::fsum, module)?)?;
    module.add_function(wrap_pyfunction!(rules::information::information_contacts_without_messages, module)?)?;
    module.add_class::<eventlog::EventStore>()?;
    module.add_function(wrap_pyfunction!(eventlog::split_json_array, module)?)?;
    module.add_function(wrap_pyfunction!(eventlog::saved_record_fields, module)?)?;
    Ok(())
}
