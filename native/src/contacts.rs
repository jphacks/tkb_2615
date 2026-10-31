//! What the per-tick contact models share: sampling keyed partners and reading the last
//! tick's contact records. The models are in `rules/social_propagation.rs` and
//! `rules/information.rs`. Each returns the same floats, draws and JSON keys as the Python
//! code: every float operation keeps its order, and anything the Python code would reject,
//! or any value of another type, raises `Unsupported` so the caller runs that code instead.

use std::collections::HashMap;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyString};

use crate::core::mathx::py_fsum;
use crate::core::rng::{Key, KeyedDraws};
use crate::core::tracked::as_readable_dict;
use crate::{write_json_string, Unsupported};

/// math.fsum of finite values, for tests.
#[pyfunction]
pub fn fsum(values: Vec<f64>) -> PyResult<f64> {
    py_fsum(&values).ok_or_else(|| Unsupported::new_err("values are not finite"))
}

pub(crate) fn exact_float(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    value
        .downcast_exact::<PyFloat>()
        .map(|number| number.value())
        .map_err(|_| Unsupported::new_err("not a float"))
}

pub(crate) fn exact_int(value: &Bound<'_, PyAny>) -> PyResult<i64> {
    value
        .downcast_exact::<PyInt>()
        .map_err(|_| Unsupported::new_err("not an int"))?
        .extract()
        .map_err(|_| Unsupported::new_err("int out of range"))
}

pub(crate) fn exact_dict<'a, 'py>(
    value: &'a Bound<'py, PyAny>,
) -> PyResult<&'a Bound<'py, PyDict>> {
    as_readable_dict(value).ok_or_else(|| Unsupported::new_err("not a dict"))
}

pub(crate) fn texts<'a>(values: &'a [Bound<'_, PyString>]) -> PyResult<Vec<&'a str>> {
    values
        .iter()
        .map(|value| {
            value
                .to_str()
                .map_err(|_| Unsupported::new_err("not UTF-8"))
        })
        .collect()
}

/// The {pair key: {last_seen_minute, consecutive_minutes}} of the last tick, as
/// non-negative ints.
pub(crate) fn previous_contacts(
    contacts: &Bound<'_, PyDict>,
) -> PyResult<HashMap<String, (i64, i64)>> {
    let py = contacts.py();
    exact_dict(contacts.as_any())?;
    let last_seen = intern!(py, "last_seen_minute");
    let consecutive = intern!(py, "consecutive_minutes");
    let mut previous = HashMap::with_capacity(contacts.len());
    for (key, value) in contacts.iter() {
        let key = key
            .downcast_exact::<PyString>()
            .map_err(|_| Unsupported::new_err("contact key is not a str"))?
            .to_str()
            .map_err(|_| Unsupported::new_err("not UTF-8"))?
            .to_owned();
        let value = exact_dict(&value)?;
        let minute = value.get_item(last_seen)?;
        let minutes = value.get_item(consecutive)?;
        let (Some(minute), Some(minutes)) = (minute, minutes) else {
            return Err(Unsupported::new_err("contact field is missing"));
        };
        let numbers = (exact_int(&minute)?, exact_int(&minutes)?);
        if numbers.0 < 0 || numbers.1 < 0 {
            return Err(Unsupported::new_err("contact field is negative"));
        }
        previous.insert(key, numbers);
    }
    Ok(previous)
}

pub(crate) fn contact_entry<'py>(
    py: Python<'py>,
    minute: i64,
    minutes: i64,
) -> PyResult<Bound<'py, PyDict>> {
    let entry = PyDict::new(py);
    entry.set_item(intern!(py, "last_seen_minute"), minute)?;
    entry.set_item(intern!(py, "consecutive_minutes"), minutes)?;
    Ok(entry)
}

#[derive(Clone, Copy)]
pub(crate) enum Basis {
    Family,
    SameZone,
    Nearby,
}

pub(crate) struct Bucket<'a> {
    pub(crate) basis: Basis,
    pub(crate) members: &'a [usize],
    pub(crate) includes_self: bool,
}

impl Bucket<'_> {
    fn eligible(&self) -> i64 {
        self.members.len() as i64 - i64::from(self.includes_self)
    }
}

/// Sorted person indexes of each zone (people are indexed in ID order).
pub(crate) fn zone_members<'a>(zones: &[&'a str]) -> HashMap<&'a str, Vec<usize>> {
    let mut members: HashMap<&str, Vec<usize>> = HashMap::new();
    for (index, &zone) in zones.iter().enumerate() {
        members.entry(zone).or_default().push(index);
    }
    members
}

/// The adjacent zones of `zone` that have people, in sorted order.
pub(crate) fn nearby_buckets<'a>(
    zone: &str,
    neighbors: &'a HashMap<String, Vec<String>>,
    members: &'a HashMap<&str, Vec<usize>>,
) -> Vec<&'a [usize]> {
    let mut names: Vec<&str> = neighbors
        .get(zone)
        .map(|names| names.iter().map(String::as_str).collect())
        .unwrap_or_default();
    names.sort_unstable();
    names
        .into_iter()
        .filter_map(|name| members.get(name))
        .filter(|people| !people.is_empty())
        .map(Vec::as_slice)
        .collect()
}

/// `_select_contacts`: at most `maximum` partners of `me`, with the draws of
/// `{key}` written as `["<label>",minute,"<me>",slot,attempt]`. The social model
/// stops at a slot that used all its attempts; the information model goes on.
pub(crate) fn select_contacts(
    draws: &KeyedDraws,
    key: &mut Key,
    me: usize,
    buckets: &[Bucket<'_>],
    maximum: i64,
    stop_at_exhausted_slot: bool,
    selected: &mut Vec<(usize, Basis)>,
) {
    selected.clear();
    if maximum == 0 || buckets.is_empty() {
        return;
    }
    let total: i64 = buckets.iter().map(Bucket::eligible).sum();
    if total <= 0 {
        return;
    }
    let head = key.mark();
    for slot in 0..maximum {
        let mut exhausted = true;
        for attempt in 0..maximum * 4 {
            key.rewind(head);
            let draw = key.int(slot).int(attempt).draw(draws);
            let mut offset = (total - 1).min((draw * total as f64) as i64);
            let mut chosen = None;
            for bucket in buckets {
                if offset < bucket.eligible() {
                    chosen = Some(bucket);
                    break;
                }
                offset -= bucket.eligible();
            }
            let Some(bucket) = chosen else {
                exhausted = false;
                break;
            };
            let mut local = offset as usize;
            if bucket.includes_self && local >= bucket.members.partition_point(|&other| other < me)
            {
                local += 1;
            }
            let source = bucket.members[local];
            if source == me || selected.iter().any(|&(other, _)| other == source) {
                continue;
            }
            selected.push((source, bucket.basis));
            exhausted = false;
            break;
        }
        if exhausted && stop_at_exhausted_slot {
            break;
        }
    }
}

/// `"<id>"` as JSON for each person.
pub(crate) fn quoted_ids(ids: &[&str]) -> Vec<Vec<u8>> {
    ids.iter()
        .map(|id| {
            let mut quoted = Vec::with_capacity(id.len() + 2);
            write_json_string(&mut quoted, id);
            quoted
        })
        .collect()
}

pub(crate) fn pair_key(first: &[u8], second: &[u8]) -> PyResult<String> {
    let mut key = String::with_capacity(first.len() + second.len() + 3);
    key.push('[');
    key.push_str(std::str::from_utf8(first).map_err(|_| Unsupported::new_err("not UTF-8"))?);
    key.push(',');
    key.push_str(std::str::from_utf8(second).map_err(|_| Unsupported::new_err("not UTF-8"))?);
    key.push(']');
    Ok(key)
}

pub(crate) fn key_head(label: &str, minute: i64, quoted: &[u8]) -> Key {
    let mut head = Key::labelled(label);
    head.int(minute).quoted(quoted);
    head
}
