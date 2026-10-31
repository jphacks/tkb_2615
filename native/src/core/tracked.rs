//! Which dict and list types a kernel may read through the C API.
//!
//! A kernel walks a dict with `PyDict_Next` and a list with `PyList_GetItem`, so a subclass's
//! overrides do not run. That is right for a plain container and for the tracked containers of
//! `core/state_mirror.py`, whose reads are the inherited C code, and wrong for any other
//! subclass: `json.dumps`, for one, calls an overridden `items`. So these are the only types
//! kernels accept, and any other subclass still goes to the Python code.
//!
//! Python hands the classes over when `core/state_mirror.py` is imported. This holds types, not
//! state of a world, so one set serves every world and thread of the process. Until then only
//! plain containers match.

use std::ptr::{addr_of_mut, null_mut};
use std::sync::atomic::{AtomicPtr, Ordering};

use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyType};

static TRACKED_DICT: AtomicPtr<ffi::PyTypeObject> = AtomicPtr::new(null_mut());
static NAMESPACE_TABLE: AtomicPtr<ffi::PyTypeObject> = AtomicPtr::new(null_mut());
static TRACKED_LIST: AtomicPtr<ffi::PyTypeObject> = AtomicPtr::new(null_mut());

/// Remember the exact classes. Each stays alive for the life of the process.
#[pyfunction]
pub fn install_tracked_types(
    tracked_dict: &Bound<'_, PyType>,
    tracked_list: &Bound<'_, PyType>,
    namespace_table: &Bound<'_, PyType>,
) -> PyResult<()> {
    if !tracked_dict.is_subclass_of::<PyDict>()?
        || !namespace_table.is_subclass_of::<PyDict>()?
        || !tracked_list.is_subclass_of::<PyList>()?
    {
        return Err(pyo3::exceptions::PyTypeError::new_err("not dict and list subclasses"));
    }
    for (slot, class) in [
        (&TRACKED_DICT, tracked_dict),
        (&TRACKED_LIST, tracked_list),
        (&NAMESPACE_TABLE, namespace_table),
    ] {
        // The new reference is never dropped, so the pointer stays valid.
        let pointer = class.clone().unbind().into_ptr();
        slot.store(pointer.cast(), Ordering::Release);
    }
    Ok(())
}

pub(crate) fn tracked_dict_type() -> *mut ffi::PyTypeObject {
    TRACKED_DICT.load(Ordering::Acquire)
}

pub(crate) fn tracked_list_type() -> *mut ffi::PyTypeObject {
    TRACKED_LIST.load(Ordering::Acquire)
}

/// A plain dict, or a dict of a class whose reads are the inherited C code.
#[inline]
pub(crate) fn is_readable_dict_type(kind: *mut ffi::PyTypeObject) -> bool {
    kind == addr_of_mut!(ffi::PyDict_Type)
        || kind == TRACKED_DICT.load(Ordering::Relaxed)
        || kind == NAMESPACE_TABLE.load(Ordering::Relaxed)
}

/// A plain list, or a list of a class whose reads are the inherited C code.
#[inline]
pub(crate) fn is_readable_list_type(kind: *mut ffi::PyTypeObject) -> bool {
    kind == addr_of_mut!(ffi::PyList_Type) || kind == TRACKED_LIST.load(Ordering::Relaxed)
}

#[inline]
pub(crate) fn as_readable_dict<'a, 'py>(
    value: &'a Bound<'py, PyAny>,
) -> Option<&'a Bound<'py, PyDict>> {
    // SAFETY: the type's pointer is only compared, and every accepted type is a dict.
    if is_readable_dict_type(unsafe { ffi::Py_TYPE(value.as_ptr()) }) {
        Some(unsafe { value.downcast_unchecked() })
    } else {
        None
    }
}

#[inline]
pub(crate) fn as_readable_list<'a, 'py>(
    value: &'a Bound<'py, PyAny>,
) -> Option<&'a Bound<'py, PyList>> {
    // SAFETY: the type's pointer is only compared, and every accepted type is a list.
    if is_readable_list_type(unsafe { ffi::Py_TYPE(value.as_ptr()) }) {
        Some(unsafe { value.downcast_unchecked() })
    } else {
        None
    }
}
