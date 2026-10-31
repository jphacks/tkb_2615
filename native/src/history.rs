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
