//! Fast acceptance of large state dicts, read where Python keeps them.
//!
//! Each check returns true only for values the Python rules it stands in front of
//! accept. Anything else, including values those rules would accept but that are not
//! plain (a subclass, an int too large for i64), returns false, and the caller runs
//! the Python rules, which accept the value or raise their own error. So a false
//! here never changes a result, and these checks hold no error messages.
//!
//! The loops read dict entries through the C API: one pass over a day-7 world's
//! relationships takes about a third of the time of doing the same through typed
//! wrappers, and a tick makes that pass over all of them. No Python code runs while a
//! dict is being walked, except the key checks passed in, which do not touch it.

use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyString};
use std::ptr::{addr_of_mut, null_mut};

use crate::core::tracked::{is_readable_dict_type, is_readable_list_type};

type Object = *mut ffi::PyObject;

fn type_is(value: Object, expected: *mut ffi::PyTypeObject) -> bool {
    // SAFETY: value is a live object reached from a dict the caller holds.
    unsafe { ffi::Py_TYPE(value) == expected }
}

fn is_exact_str(value: Object) -> bool {
    type_is(value, addr_of_mut!(ffi::PyUnicode_Type))
}

/// A plain dict or a tracked one; `core/tracked.rs` says why no other subclass.
fn is_exact_dict(value: Object) -> bool {
    // SAFETY: value is a live object reached from a dict the caller holds.
    is_readable_dict_type(unsafe { ffi::Py_TYPE(value) })
}

/// A plain list or a tracked one.
pub(crate) fn is_exact_list(value: Object) -> bool {
    // SAFETY: as above.
    is_readable_list_type(unsafe { ffi::Py_TYPE(value) })
}

fn is_none(value: Object) -> bool {
    // SAFETY: the None singleton always exists while the GIL is held.
    value == unsafe { ffi::Py_None() }
}

/// The value of an exact int that fits i64.
fn exact_int(value: Object) -> Option<i64> {
    if !type_is(value, addr_of_mut!(ffi::PyLong_Type)) {
        return None;
    }
    let mut overflow = 0;
    // SAFETY: value is an exact int, so this neither runs Python code nor raises.
    let number = unsafe { ffi::PyLong_AsLongAndOverflow(value, &mut overflow) };
    (overflow == 0).then_some(number as i64)
}

/// The value of an exact float or of an exact int that fits i64.
fn exact_number(value: Object) -> Option<f64> {
    if type_is(value, addr_of_mut!(ffi::PyFloat_Type)) {
        // SAFETY: value is an exact float.
        return Some(unsafe { ffi::PyFloat_AsDouble(value) });
    }
    // An int whose f64 rounds is compared with bounds far below the rounding.
    exact_int(value).map(|number| number as f64)
}

fn is_count(value: Object) -> bool {
    exact_int(value).is_some_and(|number| number >= 0)
}

fn is_count_or_none(value: Object) -> bool {
    is_none(value) || is_count(value)
}

/// A finite number within lower..=upper; NaN fails the range.
fn is_within(value: Object, lower: f64, upper: f64) -> bool {
    exact_number(value).is_some_and(|number| (lower..=upper).contains(&number))
}

fn is_unit(value: Object) -> bool {
    is_within(value, 0.0, 1.0)
}

/// In a dict or set of plain str keys; never raises for a str.
fn contains(mapping: Object, key: Object, is_set: bool) -> bool {
    // SAFETY: mapping is a live dict or set, and hashing and comparing an exact str
    // runs no Python code. Keys of other types are left to Python.
    unsafe {
        is_exact_str(key)
            && if is_set {
                ffi::PySet_Contains(mapping, key) == 1
            } else {
                ffi::PyDict_Contains(mapping, key) == 1
            }
    }
}

/// Walk a dict's entries. The callback returns false to stop, and the walk then
/// returns false. The dict must not change during the walk.
fn all_items(
    dict: Object,
    mut check: impl FnMut(Object, Object) -> PyResult<bool>,
) -> PyResult<bool> {
    if !is_exact_dict(dict) {
        return Ok(false);
    }
    let mut position: ffi::Py_ssize_t = 0;
    let mut key: Object = null_mut();
    let mut value: Object = null_mut();
    // SAFETY: dict is a live dict; key and value are borrowed for this step.
    while unsafe { ffi::PyDict_Next(dict, &mut position, &mut key, &mut value) } != 0 {
        if !check(key, value)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Reads records that must have exactly the fields of a fixed list.
///
/// A record is usually written with its fields in one order, and all records share
/// a few key objects, so most keys match by address. Each key object seen is held
/// for the walk, so its address cannot be reused by another object.
struct RecordReader<'py, const N: usize> {
    names: [Bound<'py, PyAny>; N],
    // The key objects seen for each field: a restored record and one made by a tick
    // usually hold different ones.
    known: [[Object; KEY_OBJECTS]; N],
    next_slot: [usize; N],
    held: Vec<Bound<'py, PyAny>>,
}

const KEY_OBJECTS: usize = 3;

impl<'py, const N: usize> RecordReader<'py, N> {
    fn new(py: Python<'py>, fields: [&str; N]) -> Self {
        Self {
            names: fields.map(|name| PyString::new(py, name).into_any()),
            known: [[null_mut(); KEY_OBJECTS]; N],
            next_slot: [0; N],
            held: Vec::new(),
        }
    }

    fn field_of(&mut self, key: Object, hint: usize) -> Option<usize> {
        if self.known[hint].contains(&key) {
            return Some(hint);
        }
        if let Some(field) = self.known.iter().position(|objects| objects.contains(&key)) {
            return Some(field);
        }
        if !is_exact_str(key) {
            return None;
        }
        // SAFETY: key is borrowed from the dict being walked, and is an exact str.
        let field = self
            .names
            .iter()
            .position(|name| unsafe { ffi::PyUnicode_Compare(name.as_ptr(), key) } == 0)?;
        self.known[field][self.next_slot[field]] = key;
        self.next_slot[field] = (self.next_slot[field] + 1) % KEY_OBJECTS;
        // SAFETY: as above; the new reference keeps the address taken.
        self.held
            .push(unsafe { Bound::from_borrowed_ptr(self.names[field].py(), key) });
        Some(field)
    }

    /// The record's values in field order, or None unless it is a plain dict with
    /// exactly these fields.
    fn read(&mut self, record: Object) -> Option<[Object; N]> {
        // SAFETY: a dict's size is read after checking its type.
        if !is_exact_dict(record) || unsafe { ffi::PyDict_Size(record) } != N as ffi::Py_ssize_t {
            return None;
        }
        let mut values = [null_mut(); N];
        let mut position: ffi::Py_ssize_t = 0;
        let mut key: Object = null_mut();
        let mut value: Object = null_mut();
        let mut index = 0;
        // SAFETY: record is a live dict; a size of N with N distinct fields found
        // means every field is filled.
        while unsafe { ffi::PyDict_Next(record, &mut position, &mut key, &mut value) } != 0 {
            values[self.field_of(key, index)?] = value;
            index += 1;
        }
        values
            .iter()
            .all(|value| !value.is_null())
            .then_some(values)
    }
}

/// Whether a relationship key was checked before, or passes the Python check now.
/// key_ok returns true for a good key, which is remembered.
fn known_or_good_key(
    good_keys: &Bound<'_, PySet>,
    key_ok: &Bound<'_, PyAny>,
    key: Object,
) -> PyResult<bool> {
    if !is_exact_str(key) {
        return Ok(false);
    }
    if contains(good_keys.as_ptr(), key, true) {
        return Ok(true);
    }
    // SAFETY: key is borrowed from the dict being walked.
    let key = unsafe { Bound::from_borrowed_ptr(good_keys.py(), key) };
    let good = key_ok.call1((&key,))?.is_truthy()?;
    if good {
        good_keys.add(&key)?;
    }
    Ok(good)
}

/// Every social_network_v1 relationship passes validate_social_network_state's rules.
#[pyfunction]
pub fn social_relationships_valid(
    relationships: &Bound<'_, PyDict>,
    good_keys: &Bound<'_, PySet>,
    key_ok: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let mut reader = RecordReader::new(
        relationships.py(),
        [
            "contact_count",
            "conversation_count",
            "cooperation_count",
            "trust",
            "shared_message_ids",
            "last_contact_minute",
            "last_interaction",
        ],
    );
    all_items(relationships.as_ptr(), |key, record| {
        if !known_or_good_key(good_keys, key_ok, key)? {
            return Ok(false);
        }
        let Some([contacts, conversations, cooperations, trust, messages, minute, _]) =
            reader.read(record)
        else {
            return Ok(false);
        };
        Ok(is_count(contacts)
            && is_count(conversations)
            && is_count(cooperations)
            && is_exact_list(messages)
            && (is_none(trust) || is_unit(trust))
            && is_count_or_none(minute))
    })
}

pub(crate) fn topics_valid(topics: Object) -> bool {
    // SAFETY: topics is an exact list, and nothing here runs Python code.
    unsafe {
        let count = ffi::PyList_Size(topics);
        (0..count).all(|index| {
            let topic = ffi::PyList_GetItem(topics, index);
            is_exact_str(topic) && ffi::PyUnicode_GetLength(topic) <= 64
        }) && count <= 32
    }
}

/// Every record of movement_v1 "positions" passes validate_movement_runtime_state's
/// rules for these zones and edges.
#[pyfunction]
pub fn movement_positions_valid(
    positions: &Bound<'_, PyDict>,
    zones: &Bound<'_, PyDict>,
    edges: &Bound<'_, PyDict>,
) -> PyResult<bool> {
    let py = positions.py();
    let mut reader = RecordReader::new(
        py,
        ["zone_id", "point", "edge_id", "progress", "position_kind"],
    );
    let mut point_reader = RecordReader::new(py, ["x", "y"]);
    all_items(positions.as_ptr(), |_, record| {
        let Some([zone, point, edge, progress, kind]) = reader.read(record) else {
            return Ok(false);
        };
        if !contains(zones.as_ptr(), zone, false)
            || !is_unit(progress)
            || !(is_none(edge) || contains(edges.as_ptr(), edge, false))
            || !is_position_kind(kind)
        {
            return Ok(false);
        }
        if is_none(point) {
            return Ok(true);
        }
        let Some([x, y]) = point_reader.read(point) else {
            return Ok(false);
        };
        Ok(is_within(x, f64::MIN, f64::MAX) && is_within(y, f64::MIN, f64::MAX))
    })
}

fn is_position_kind(kind: Object) -> bool {
    is_exact_str(kind)
        && [c"physical", c"display_only", c"unavailable"]
            .iter()
            .any(|name| {
                // SAFETY: kind is an exact str and name is NUL-terminated ASCII.
                unsafe { ffi::PyUnicode_CompareWithASCIIString(kind, name.as_ptr()) == 0 }
            })
}

/// Every AgentState passes the per-Agent rules of WorldState.validate, for a world of
/// schema version 6. Agent objects are read by attribute, as they have no dict to walk.
#[pyfunction]
pub fn world_agents_valid(
    agents: &Bound<'_, PyDict>,
    zones: &Bound<'_, PyDict>,
    activity_type: &Bound<'_, PyAny>,
    initial_needs_type: &Bound<'_, PyAny>,
    needs_type: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let py = agents.py();
    let name = |text: &str| PyString::intern(py, text);
    let (zone_id, activity, profile, initial_needs, needs) = (
        name("zone_id"),
        name("activity"),
        name("profile"),
        name("initial_needs"),
        name("needs"),
    );
    let (toilet, hunger, fatigue, hydration, stress, trust) = (
        name("toilet_urgency"),
        name("hunger"),
        name("fatigue"),
        name("hydration_need"),
        name("stress"),
        name("trust"),
    );
    let (next_decision, cooldown) = (name("next_decision_at"), name("support_cooldown_until"));
    all_items(agents.as_ptr(), |_, agent| {
        // SAFETY: agent is borrowed from the dict being walked.
        let agent = unsafe { Bound::from_borrowed_ptr(py, agent) };
        let zone = agent.getattr(&zone_id)?;
        if !contains(zones.as_ptr(), zone.as_ptr(), false) {
            return Ok(false);
        }
        let plain =
            |value: &Bound<'_, PyAny>, expected: &Bound<'_, PyAny>| value.get_type().is(expected);
        if !plain(&agent.getattr(&activity)?, activity_type)
            || !plain(
                &agent.getattr(&profile)?.getattr(&initial_needs)?,
                initial_needs_type,
            )
        {
            return Ok(false);
        }
        let current = agent.getattr(&needs)?;
        if !plain(&current, needs_type) {
            return Ok(false);
        }
        for level in [&toilet, &hunger, &fatigue, &hydration] {
            if !is_unit(current.getattr(level)?.as_ptr()) {
                return Ok(false);
            }
        }
        Ok(is_unit(agent.getattr(&stress)?.as_ptr())
            && is_unit(agent.getattr(&trust)?.as_ptr())
            && is_count(agent.getattr(&next_decision)?.as_ptr())
            && is_count(agent.getattr(&cooldown)?.as_ptr()))
    })
}

/// The values a dict held at the last check, in order, held so their identity can be
/// compared with the dict's values later.
#[pyclass(module = "sheltersim_native")]
#[derive(Default)]
pub struct ValuesSnapshot {
    values: Vec<Py<PyAny>>,
}

#[pymethods]
impl ValuesSnapshot {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    /// The dict's values after the snapshot's, or None unless the dict starts with
    /// the very objects of the snapshot, in order.
    fn added_values<'py>(
        &self,
        values: &Bound<'py, PyDict>,
    ) -> PyResult<Option<Bound<'py, PyList>>> {
        if values.len() < self.values.len() {
            return Ok(None);
        }
        let added = PyList::empty(values.py());
        let mut position = 0;
        let same = all_items(values.as_ptr(), |_, value| {
            if position < self.values.len() {
                position += 1;
                return Ok(self.values[position - 1].as_ptr() == value);
            }
            // SAFETY: value is borrowed from the dict being walked.
            added.append(unsafe { Bound::from_borrowed_ptr(values.py(), value) })?;
            Ok(true)
        })?;
        Ok(same.then_some(added))
    }

    /// Hold these values after the ones held already.
    fn extend(&mut self, added: &Bound<'_, PyList>) {
        self.values.extend(added.iter().map(Bound::unbind));
    }

    /// Forget every value.
    fn clear(&mut self) {
        self.values.clear();
    }

    fn __len__(&self) -> usize {
        self.values.len()
    }
}
