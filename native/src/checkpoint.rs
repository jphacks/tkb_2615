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

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};
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
