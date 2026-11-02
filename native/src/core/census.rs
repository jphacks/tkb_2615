//! Where each `Unsupported` was raised and who caught it, for benchmarks/rule_shadow/refusal_census.py.
//!
//! Off until that tool turns it on. Nothing here runs unless a kernel refuses, and then only a load
//! of one flag on the way to building the exception, so a run without the tool pays nothing a refusal
//! didn't already pay. The tool reads the environment; the kernels never do.

use std::collections::BTreeMap;
use std::panic::Location;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use pyo3::prelude::*;
use pyo3::types::PyList;

type Site = (&'static str, u32);

static ON: AtomicBool = AtomicBool::new(false);
static COUNTS: Mutex<Counts> = Mutex::new(Counts {
    raised: BTreeMap::new(),
    caught: BTreeMap::new(),
    last: None,
    moved: BTreeMap::new(),
    fell_back: BTreeMap::new(),
    last_of: BTreeMap::new(),
});

struct Counts {
    raised: BTreeMap<(Site, String), u64>,
    /// By `(who caught it, the site if it was the last raised, reason)`.
    caught: BTreeMap<(String, Option<Site>, String), u64>,
    last: Option<(Site, String)>,
    /// The errors of `core/errors.rs` raised, by site and text.
    moved: BTreeMap<(Site, String), u64>,
    /// What the Python rule raised after a refusal: by `(who fell back, the refusal's last site,
    /// reason, the Python class and text)`.
    fell_back: BTreeMap<(String, Option<Site>, String, String), u64>,
    /// The last site that raised each reason.
    last_of: BTreeMap<String, Site>,
}

/// A refusal being built at the caller's line.
#[cold]
#[inline(never)]
#[track_caller]
pub(crate) fn raised(reason: &str) {
    if !ON.load(Ordering::Relaxed) {
        return;
    }
    let location = Location::caller();
    let site = (location.file(), location.line());
    if let Ok(mut counts) = COUNTS.lock() {
        *counts.raised.entry((site, reason.to_owned())).or_default() += 1;
        counts.last = Some((site, reason.to_owned()));
        counts.last_of.insert(reason.to_owned(), site);
    }
}

/// An error of `core/errors.rs` being built at the caller's caller's line.
#[cold]
#[inline(never)]
#[track_caller]
pub(crate) fn moved(text: &str) {
    if !ON.load(Ordering::Relaxed) {
        return;
    }
    let location = Location::caller();
    if let Ok(mut counts) = COUNTS.lock() {
        *counts.moved.entry(((location.file(), location.line()), text.to_owned())).or_default() += 1;
    }
}

fn note_fell_back(by: &str, reason: String, outcome: String) {
    if let Ok(mut counts) = COUNTS.lock() {
        let site = counts.last_of.get(&reason).copied();
        *counts.fell_back.entry((by.to_owned(), site, reason, outcome)).or_default() += 1;
    }
}

/// The Python rule raised `error` after the refusal `refusal`, at a fallback in Rust.
#[cold]
#[inline(never)]
pub(crate) fn fell_back(py: Python<'_>, by: &str, refusal: &PyErr, error: &PyErr) {
    if ON.load(Ordering::Relaxed) {
        let class = error.get_type(py).qualname().map(|name| name.to_string()).unwrap_or_default();
        note_fell_back(by, refusal.value(py).to_string(), format!("{class}: {}", error.value(py)));
    }
}

/// The Python rule raised `error` after the refusal `refusal`, at a fallback in Python.
#[pyfunction]
pub(crate) fn census_fell_back(by: &str, refusal: Bound<'_, PyAny>, error: Bound<'_, PyAny>) -> PyResult<()> {
    if ON.load(Ordering::Relaxed) {
        let class = error.get_type().qualname()?.to_string();
        note_fell_back(by, refusal.str()?.to_string(), format!("{class}: {}", error.str()?));
    }
    Ok(())
}

fn note_caught(by: &str, reason: String) {
    if let Ok(mut counts) = COUNTS.lock() {
        let site = counts.last.as_ref().filter(|(_, last)| *last == reason).map(|(site, _)| *site);
        *counts.caught.entry((by.to_owned(), site, reason)).or_default() += 1;
    }
}

/// A fallback in Rust: `or_rule!` in the driver, or a `__wrapped__` call-back.
#[cold]
#[inline(never)]
pub(crate) fn caught(py: Python<'_>, by: &str, error: &PyErr) {
    if ON.load(Ordering::Relaxed) {
        note_caught(by, error.value(py).to_string());
    }
}

/// A `__wrapped__` call-back with no refusal at hand.
#[cold]
#[inline(never)]
pub(crate) fn called_back(name: &str) {
    if ON.load(Ordering::Relaxed) {
        note_caught(&format!("__wrapped__ {name}"), String::new());
    }
}

/// Turn the census on or off for this process.
#[pyfunction]
pub(crate) fn census_switch(on: bool) {
    ON.store(on, Ordering::Relaxed);
}

/// A refusal that reached a Python entry of a kernel.
#[pyfunction]
pub(crate) fn census_caught(by: &str, reason: String) {
    note_caught(by, reason);
}

/// Take what was counted: `(raised, caught)`, rows of `(file, line, reason, n)` and
/// `(by, file, line, reason, n)`, and start again. The moved errors and the fallbacks' errors wait
/// for `census_take_errors`.
#[pyfunction]
pub(crate) fn census_take<'py>(py: Python<'py>) -> PyResult<(Bound<'py, PyList>, Bound<'py, PyList>)> {
    let (raised, caught) = match COUNTS.lock() {
        Ok(mut counts) => (std::mem::take(&mut counts.raised), std::mem::take(&mut counts.caught)),
        Err(_) => Default::default(),
    };
    let raised_rows = PyList::empty(py);
    for (((file, line), reason), n) in raised {
        raised_rows.append((file, line, reason, n))?;
    }
    let caught_rows = PyList::empty(py);
    for ((by, site, reason), n) in caught {
        let (file, line) = site.unwrap_or(("", 0));
        caught_rows.append((by, file, line, reason, n))?;
    }
    Ok((raised_rows, caught_rows))
}

/// Take the moved errors, rows of `(file, line, text, n)`, and what the Python rules raised after
/// refusals, rows of `(by, file, line, reason, outcome, n)`, and start again.
#[pyfunction]
pub(crate) fn census_take_errors<'py>(py: Python<'py>) -> PyResult<(Bound<'py, PyList>, Bound<'py, PyList>)> {
    let (moved, fell_back) = match COUNTS.lock() {
        Ok(mut counts) => (std::mem::take(&mut counts.moved), std::mem::take(&mut counts.fell_back)),
        Err(_) => Default::default(),
    };
    let moved_rows = PyList::empty(py);
    for (((file, line), text), n) in moved {
        moved_rows.append((file, line, text, n))?;
    }
    let fell_back_rows = PyList::empty(py);
    for ((by, site, reason, outcome), n) in fell_back {
        let (file, line) = site.unwrap_or(("", 0));
        fell_back_rows.append((by, file, line, reason, outcome, n))?;
    }
    Ok((moved_rows, fell_back_rows))
}
