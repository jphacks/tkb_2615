//! Whether history entries are still exactly the documents saved for them.
//!
//! A save's complete validation does not read a history entry again if it is the entry
//! that passed before (`core/save_validation.py`). This compares every entry with the
//! JSON document the last save made from it, and is stricter than `==`: types must be
//! the same, so 1, 1.0 and True differ and so do a tuple and a list, and floats compare
//! by their bits, so 0.0 and -0.0 differ. A document holds only plain JSON values, so
//! anything else in an entry (a subclass, a non-str key) makes it different, and the
//! caller reads the entry again. So a false here never changes a result.
//!
//! Only exact built-in types and the tracked containers, whose reads are the inherited C code,
//! are compared, so no Python code runs during the walk. A tracked container counts as the plain
//! one its document holds.

use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::PyList;
use std::ptr::{addr_of_mut, null_mut};

use crate::core::tracked::{as_readable_list, is_readable_dict_type, is_readable_list_type};

type Object = *mut ffi::PyObject;

// Deeper values are left to the caller, which reads the entry again.
const MAX_DEPTH: usize = 32;

/// The built-in type a tracked container stands for; any other type is itself.
#[inline]
fn plain_kind(kind: *mut ffi::PyTypeObject) -> *mut ffi::PyTypeObject {
    if is_readable_dict_type(kind) {
        addr_of_mut!(ffi::PyDict_Type)
    } else if is_readable_list_type(kind) {
        addr_of_mut!(ffi::PyList_Type)
    } else {
        kind
    }
}

fn same_value(left: Object, right: Object, depth: usize) -> bool {
    // SAFETY: both are live objects reached from lists the caller holds, the GIL is
    // held, and nothing below runs Python code, so nothing borrowed here goes away.
    unsafe {
        let kind = plain_kind(ffi::Py_TYPE(left));
        if kind != plain_kind(ffi::Py_TYPE(right)) {
            return false;
        }
        if kind == addr_of_mut!(ffi::PyUnicode_Type) || kind == addr_of_mut!(ffi::PyLong_Type) {
            return left == right || ffi::PyObject_RichCompareBool(left, right, ffi::Py_EQ) == 1;
        }
        if kind == addr_of_mut!(ffi::PyFloat_Type) {
            return ffi::PyFloat_AsDouble(left).to_bits() == ffi::PyFloat_AsDouble(right).to_bits();
        }
        if left == ffi::Py_None() || kind == addr_of_mut!(ffi::PyBool_Type) {
            return left == right;
        }
        if depth == 0 {
            return false;
        }
        if kind == addr_of_mut!(ffi::PyList_Type) {
            let count = ffi::PyList_Size(left);
            return count == ffi::PyList_Size(right)
                && (0..count).all(|index| {
                    same_value(
                        ffi::PyList_GetItem(left, index),
                        ffi::PyList_GetItem(right, index),
                        depth - 1,
                    )
                });
        }
        if kind == addr_of_mut!(ffi::PyDict_Type) {
            if ffi::PyDict_Size(left) != ffi::PyDict_Size(right) {
                return false;
            }
            let mut position = 0;
            let mut key: Object = null_mut();
            let mut value: Object = null_mut();
            while ffi::PyDict_Next(left, &mut position, &mut key, &mut value) != 0 {
                if ffi::Py_TYPE(key) != addr_of_mut!(ffi::PyUnicode_Type) {
                    return false;
                }
                let other = ffi::PyDict_GetItemWithError(right, key);
                if other.is_null() || !same_value(value, other, depth - 1) {
                    ffi::PyErr_Clear();
                    return false;
                }
            }
            return true;
        }
        false
    }
}

/// Whether each of `current[:count]` is exactly `documents[start + i]`.
#[pyfunction]
pub fn same_json_items(
    current: &Bound<'_, PyAny>,
    documents: &Bound<'_, PyList>,
    start: usize,
    count: usize,
) -> bool {
    let Some(current) = as_readable_list(current) else {
        return false;
    };
    if count > current.len() || start.saturating_add(count) > documents.len() {
        return false;
    }
    let (left, right) = (current.as_ptr(), documents.as_ptr());
    (0..count).all(|index| {
        // SAFETY: both indexes were checked against lists that nothing here changes.
        unsafe {
            same_value(
                ffi::PyList_GetItem(left, index as ffi::Py_ssize_t),
                ffi::PyList_GetItem(right, (start + index) as ffi::Py_ssize_t),
                MAX_DEPTH,
            )
        }
    })
}

const SCALAR: u8 = 0;
const DICT: u8 = 1;
const LIST: u8 = 2;

/// Reference to every object inside each entry of one history, as the entries held them when they
/// were shown equal to their documents (`same_json_items`' rule).
///
/// Most of the cost of comparing an old entry with its document is reading the values and looking
/// up the keys on both sides, in objects scattered over memory. An entry that still holds the same
/// objects in the same order is equal to what it was, because the objects inside are immutable or
/// are themselves followed, and they stay alive here so an address can't come back with another
/// value. So `unchanged` reads only the entries' containers. Anything else, also a mere change of
/// order, says false, and the caller compares with the documents as before.
#[pyclass(module = "sheltersim_native")]
#[derive(Default)]
pub struct HistoryPins {
    // Each entry in preorder: the entry, then for a list its items and for a dict its keys and
    // values, each followed by its own insides.
    objects: Vec<Py<PyAny>>,
    kinds: Vec<u8>,
    // The size of each container, in the same order.
    sizes: Vec<isize>,
    // Where each entry starts in objects and in sizes.
    entries: Vec<(usize, usize)>,
    // How many leading entries have been dropped.
    head: usize,
    // An entry that isn't equal to its document: nothing can be said until the next clear.
    broken: bool,
}

impl HistoryPins {
    /// Add value's insides. The caller has shown that value is plain JSON (`same_value`).
    fn pin(&mut self, py: Python<'_>, value: Object, depth: usize) -> bool {
        if depth == 0 {
            return false;
        }
        // SAFETY: value is a live object reached from a list the caller holds, the GIL is held and
        // nothing below runs Python code.
        unsafe {
            let kind = plain_kind(ffi::Py_TYPE(value));
            let tag = if kind == addr_of_mut!(ffi::PyDict_Type) {
                DICT
            } else if kind == addr_of_mut!(ffi::PyList_Type) {
                LIST
            } else {
                SCALAR
            };
            self.objects.push(Py::from_borrowed_ptr(py, value));
            self.kinds.push(tag);
            match tag {
                LIST => {
                    let count = ffi::PyList_Size(value);
                    self.sizes.push(count);
                    (0..count).all(|index| self.pin(py, ffi::PyList_GetItem(value, index), depth - 1))
                }
                DICT => {
                    self.sizes.push(ffi::PyDict_Size(value));
                    let mut position = 0;
                    let mut key: Object = null_mut();
                    let mut item: Object = null_mut();
                    while ffi::PyDict_Next(value, &mut position, &mut key, &mut item) != 0 {
                        self.objects.push(Py::from_borrowed_ptr(py, key));
                        self.kinds.push(SCALAR);
                        if !self.pin(py, item, depth - 1) {
                            return false;
                        }
                    }
                    true
                }
                _ => true,
            }
        }
    }

    fn same(&self, value: Object, object: &mut usize, size: &mut usize) -> bool {
        let at = *object;
        // Not a read of the object: only a scalar's address is compared.
        if self.objects[at].as_ptr() != value {
            return false;
        }
        *object += 1;
        // SAFETY: value is the live object held at this place, so it is the container it was, and
        // the GIL is held. Nothing below runs Python code.
        unsafe {
            match self.kinds[at] {
                LIST => {
                    let count = ffi::PyList_Size(value);
                    if count != self.sizes[*size] {
                        return false;
                    }
                    *size += 1;
                    (0..count).all(|index| self.same(ffi::PyList_GetItem(value, index), object, size))
                }
                DICT => {
                    if ffi::PyDict_Size(value) != self.sizes[*size] {
                        return false;
                    }
                    *size += 1;
                    let mut position = 0;
                    let mut key: Object = null_mut();
                    let mut item: Object = null_mut();
                    while ffi::PyDict_Next(value, &mut position, &mut key, &mut item) != 0 {
                        if self.objects[*object].as_ptr() != key {
                            return false;
                        }
                        *object += 1;
                        if !self.same(item, object, size) {
                            return false;
                        }
                    }
                    true
                }
                _ => true,
            }
        }
    }
}

#[pymethods]
impl HistoryPins {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    fn clear(&mut self) {
        *self = Self::default();
    }

    /// How many entries it holds, for the tests.
    fn __len__(&self) -> usize {
        self.entries.len() - self.head
    }

    /// Whether `current[:count]` still hold what entries `start` to `start + count` held. Reads
    /// only containers. A false says nothing about the entries.
    fn unchanged(&self, current: &Bound<'_, PyAny>, start: usize, count: usize) -> bool {
        let Some(current) = as_readable_list(current) else {
            return false;
        };
        if self.broken
            || start.saturating_add(count) != self.entries.len() - self.head
            || count > current.len()
        {
            return false;
        }
        let list = current.as_ptr();
        if count == 0 {
            return true;
        }
        let (mut object, mut size) = self.entries[self.head + start];
        (0..count).all(|index| {
            // SAFETY: the index was checked against a list that nothing here changes.
            self.same(unsafe { ffi::PyList_GetItem(list, index as ffi::Py_ssize_t) }, &mut object, &mut size)
        })
    }

    /// Forget the first count entries, which left the history.
    fn drop_front(&mut self, count: usize) {
        self.head = (self.head + count).min(self.entries.len());
        // A release costs the move of what stays, so it waits until half is dead.
        if self.head * 2 > self.entries.len() {
            let (objects, sizes) = self.entries.get(self.head).copied().unwrap_or((self.objects.len(), self.sizes.len()));
            self.objects.drain(..objects);
            self.kinds.drain(..objects);
            self.sizes.drain(..sizes);
            self.entries.drain(..self.head);
            for entry in &mut self.entries {
                entry.0 -= objects;
                entry.1 -= sizes;
            }
            self.head = 0;
        }
    }

    /// Add `current[start:]`, each shown equal to the same place of `documents` first.
    fn extend(
        &mut self,
        py: Python<'_>,
        current: &Bound<'_, PyAny>,
        documents: &Bound<'_, PyList>,
        start: usize,
    ) {
        let Some(current) = as_readable_list(current) else {
            self.broken = true;
            return;
        };
        if self.broken || current.len() != documents.len() {
            self.broken = true;
            return;
        }
        let (left, right) = (current.as_ptr(), documents.as_ptr());
        for index in start..current.len() {
            // SAFETY: both lengths were checked, and nothing here changes the lists.
            let (entry, document) = unsafe {
                (
                    ffi::PyList_GetItem(left, index as ffi::Py_ssize_t),
                    ffi::PyList_GetItem(right, index as ffi::Py_ssize_t),
                )
            };
            self.entries.push((self.objects.len(), self.sizes.len()));
            if !same_value(entry, document, MAX_DEPTH) || !self.pin(py, entry, MAX_DEPTH + 1) {
                self.broken = true;
                return;
            }
        }
    }
}
