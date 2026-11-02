//! The one exit for the texts of an event's expected outcomes: `EventSuppressed` and
//! `EventApplicationError` of `events/models.py` (docs/rust-port/lanes/l8-texts-events.md).
//!
//! These texts are saved data: the dispatcher writes `str(exc)` to the event's record as its
//! `detail`, which is hashed and shown. So a kernel that raises one must say what the Python rule
//! says, byte for byte, and in the same order of checks. The classes stay Python's; a kernel raises
//! them through here.
//!
//! benchmarks/rule_shadow/text_check.py compares each text made here with what the Python rule
//! raises for the same input, while the Python rules still exist. It turns the record on: then each
//! text says where it was made (`#[track_caller]`), as `core/census.rs` does for `Unsupported`.
//! Off, a text costs one load of a flag.

use std::collections::BTreeMap;
use std::panic::Location;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyFloat, PyInt, PyList, PyString, PyType};

type Site = (&'static str, u32);

/// Which of the two outcomes.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug)]
pub(crate) enum Outcome {
    Suppressed,
    Failed,
}

impl Outcome {
    fn class_name(self) -> &'static str {
        match self {
            Outcome::Suppressed => "EventSuppressed",
            Outcome::Failed => "EventApplicationError",
        }
    }
}

static ON: AtomicBool = AtomicBool::new(false);
static MADE: Mutex<Record> = Mutex::new(Record { made: BTreeMap::new(), last: None });
/// The attribute an outcome made here carries; its text, `str(exc)`, is the rule's alone.
const MARK: &str = "_sheltersim_native_outcome";
static CLASSES: GILOnceCell<(Py<PyType>, Py<PyType>)> = GILOnceCell::new();

struct Record {
    made: BTreeMap<(Site, Outcome, String), u64>,
    last: Option<(Site, Outcome, String)>,
}

#[cold]
#[inline(never)]
fn note(site: Site, outcome: Outcome, text: &str) {
    if let Ok(mut record) = MADE.lock() {
        *record.made.entry((site, outcome, text.to_owned())).or_default() += 1;
        record.last = Some((site, outcome, text.to_owned()));
    }
}

fn class(py: Python<'_>, outcome: Outcome) -> PyResult<&Bound<'_, PyType>> {
    let classes = CLASSES.get_or_try_init(py, || -> PyResult<_> {
        let models = py.import(intern!(py, "sheltersim.events.models"))?;
        Ok((
            models.getattr(intern!(py, "EventSuppressed"))?.downcast_into::<PyType>()?.unbind(),
            models.getattr(intern!(py, "EventApplicationError"))?.downcast_into::<PyType>()?.unbind(),
        ))
    })?;
    Ok(match outcome {
        Outcome::Suppressed => classes.0.bind(py),
        Outcome::Failed => classes.1.bind(py),
    })
}

/// A fixed text of an outcome a kernel returns as a value, noted where it was made.
#[track_caller]
#[inline]
pub(crate) fn noted(outcome: Outcome, text: &'static str) -> &'static str {
    if ON.load(Ordering::Relaxed) {
        let location = Location::caller();
        note((location.file(), location.line()), outcome, text);
    }
    text
}

/// The exception of an outcome.
#[track_caller]
#[inline]
pub(crate) fn error(py: Python<'_>, outcome: Outcome, text: impl Into<String>) -> PyErr {
    let text = text.into();
    if ON.load(Ordering::Relaxed) {
        let location = Location::caller();
        note((location.file(), location.line()), outcome, &text);
    }
    let made = (|| -> PyResult<PyErr> {
        let instance = class(py, outcome)?.call1((text,))?;
        instance.setattr(intern!(py, MARK), true)?;
        Ok(PyErr::from_value(instance))
    })();
    made.unwrap_or_else(|error| error)
}

/// `raise ValueError(text)` in the Python rule's words, marked as made here so that a step which turns
/// a Python call's exception into Unsupported lets it through (a check of `effect_program_v1`).
pub(crate) fn marked_value_error(py: Python<'_>, text: &str) -> PyErr {
    let made = (|| -> PyResult<PyErr> {
        let instance = py.get_type::<pyo3::exceptions::PyValueError>().call1((text,))?;
        instance.setattr(intern!(py, MARK), true)?;
        Ok(PyErr::from_value(instance))
    })();
    made.unwrap_or_else(|error| error)
}

/// Whether this error is an outcome made here, rather than one a Python call raised: a step that
/// turns any exception of a call into Unsupported lets these through.
pub(crate) fn made_here(py: Python<'_>, error: &PyErr) -> bool {
    error.value(py).hasattr(intern!(py, MARK)).unwrap_or(false)
}

/// `raise EventSuppressed(text)`.
#[track_caller]
#[inline]
pub(crate) fn suppressed<T>(py: Python<'_>, text: impl Into<String>) -> PyResult<T> {
    Err(error(py, Outcome::Suppressed, text))
}

/// `raise EventApplicationError(text)`.
#[track_caller]
#[inline]
pub(crate) fn failed<T>(py: Python<'_>, text: impl Into<String>) -> PyResult<T> {
    Err(error(py, Outcome::Failed, text))
}

/// `repr(value)` of a Python value, for the `{x!r}` of a Python rule's f-string.
pub(crate) fn repr(value: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(value.repr()?.to_str()?.to_owned())
}

/// `repr(text)` of a str, as Python writes it.
pub(crate) fn repr_str(py: Python<'_>, text: &str) -> PyResult<String> {
    repr(PyString::new(py, text).as_any())
}

// ---- The type tests of a rule's checks ---------------------------------------------------------------
//
// A check that raises a text must say yes and no exactly where the Python rule does. Where Rust can't
// tell (a subclass, an int past 64 bits) it says Unsupported, and the Python rule runs and decides.

/// `isinstance(value, int) and not isinstance(value, bool)`: Some(value), None when it isn't one.
pub(crate) fn int_not_bool(value: &Bound<'_, PyAny>) -> PyResult<Option<i64>> {
    if value.is_instance_of::<PyBool>() || !value.is_instance_of::<PyInt>() {
        return Ok(None);
    }
    crate::core::row::exact_int(value).map(Some)
}

/// `type(value) is int`: Some(value), None when it isn't.
pub(crate) fn exactly_int(value: &Bound<'_, PyAny>) -> PyResult<Option<i64>> {
    if !value.is_exact_instance_of::<PyInt>() {
        return Ok(None);
    }
    crate::core::row::exact_int(value).map(Some)
}

/// `isinstance(value, str)`: Some(text), None when it isn't one. A subclass is refused.
pub(crate) fn str_of<'a>(value: &'a Bound<'_, PyAny>) -> PyResult<Option<&'a str>> {
    if !value.is_instance_of::<PyString>() {
        return Ok(None);
    }
    crate::core::row::exact_str(value).map(Some)
}

/// `isinstance(value, str) and value`: Some(text), None when it isn't a non-empty str.
pub(crate) fn nonempty_str<'a>(value: &'a Bound<'_, PyAny>) -> PyResult<Option<&'a str>> {
    Ok(str_of(value)?.filter(|text| !text.is_empty()))
}

/// `not isinstance(value, bool) and isinstance(value, (int, float))`, as `float(value)`: None when it
/// isn't one. A subclass, or an int a float can't hold exactly, is refused.
pub(crate) fn real_number(value: &Bound<'_, PyAny>) -> PyResult<Option<f64>> {
    if value.is_instance_of::<PyBool>() || !(value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>()) {
        return Ok(None);
    }
    if let Ok(float) = value.downcast_exact::<PyFloat>() {
        return Ok(Some(float.value()));
    }
    let int = crate::core::row::exact_int(value)?;
    if !(-(1 << 53)..=1 << 53).contains(&int) {
        return crate::core::row::unsupported("a number does not fit a float");
    }
    Ok(Some(int as f64))
}

/// `Enum(value)` of a str enum whose values are `members`: Some(the value), None where the Python rule
/// catches the ValueError or TypeError. A str subclass (a member of the enum itself) is refused.
pub(crate) fn member_of<'a>(value: &'a Bound<'_, PyAny>, members: &[&str]) -> PyResult<Option<&'a str>> {
    if !value.is_instance_of::<PyString>() {
        return Ok(None);
    }
    let text = crate::core::row::exact_str(value)?;
    Ok(members.contains(&text).then_some(text))
}

/// Turn the record on or off for this process.
#[pyfunction]
pub(crate) fn texts_switch(on: bool) {
    ON.store(on, Ordering::Relaxed);
}

/// The last text made: `(file, line, class name, text)`, or None.
#[pyfunction]
pub(crate) fn texts_last() -> Option<(&'static str, u32, &'static str, String)> {
    let record = MADE.lock().ok()?;
    let ((file, line), outcome, text) = record.last.clone()?;
    Some((file, line, outcome.class_name(), text))
}

/// Take what was made: rows of `(file, line, class name, text, n)`, and start again.
#[pyfunction]
pub(crate) fn texts_take(py: Python<'_>) -> PyResult<Bound<'_, PyList>> {
    let made = match MADE.lock() {
        Ok(mut record) => {
            record.last = None;
            std::mem::take(&mut record.made)
        }
        Err(_) => BTreeMap::new(),
    };
    let rows = PyList::empty(py);
    for (((file, line), outcome, text), n) in made {
        rows.append((file, line, outcome.class_name(), text, n))?;
    }
    Ok(rows)
}

// ---- social (lane L8c) -------------------------------------------------------------------------------

/// The exception of an outcome a kernel already noted where it gave the text (`noted`), raised without
/// noting it again: the record's last text, which a comparison counts, stays at that site.
pub(crate) fn raise_noted<T>(py: Python<'_>, outcome: Outcome, text: &str) -> PyResult<T> {
    let made = (|| -> PyResult<PyErr> {
        let instance = class(py, outcome)?.call1((text,))?;
        instance.setattr(intern!(py, MARK), true)?;
        Ok(PyErr::from_value(instance))
    })();
    Err(made.unwrap_or_else(|error| error))
}
