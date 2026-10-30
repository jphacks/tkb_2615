//! Telling which records of a large dict of dicts were written, without reading them.
//!
//! A tick writes a few of the thousands of relationship records, but the end-of-tick
//! check has to accept all of them, and in a long run those objects are scattered over
//! memory, so even a plain walk costs several milliseconds. CPython's dict watchers
//! report every change to a watched dict, whichever code makes it (`PyDict_Watch`,
//! Python 3.12). So a record that reported nothing still holds the immutable values
//! it held when it passed.
//!
//! Dict watchers are outside the stable ABI this extension builds for, so Python looks
//! the three functions up with ctypes and passes their addresses to `install`; where
//! that fails, callers keep walking every record. A list inside a record can change
//! without its dict knowing, so a list a rule reads has to be read at every check.

use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use std::collections::{HashMap, HashSet};
use std::os::raw::c_int;
use std::sync::Mutex;

use crate::validation::{is_exact_list, topics_valid};

type DictCallback = unsafe extern "C" fn(
    c_int,
    *mut ffi::PyObject,
    *mut ffi::PyObject,
    *mut ffi::PyObject,
) -> c_int;
type AddWatcher = unsafe extern "C" fn(DictCallback) -> c_int;
type WatchCall = unsafe extern "C" fn(c_int, *mut ffi::PyObject) -> c_int;

#[derive(Default)]
struct Registry {
    api: Option<(c_int, WatchCall, WatchCall)>,
    /// Addresses of the dicts that reported a change, not yet handed to their owner.
    events: Vec<usize>,
    /// The RecordWatch of each watched dict.
    owners: HashMap<usize, u64>,
    /// Changed dicts of each RecordWatch.
    changed: HashMap<u64, Vec<usize>>,
    /// RecordWatches that had a dict taken over by another one.
    robbed: HashSet<u64>,
    next_owner: u64,
}

impl Registry {
    fn hand_out_events(&mut self) {
        let events = std::mem::take(&mut self.events);
        for address in &events {
            if let Some(owner) = self.owners.get(address) {
                self.changed.entry(*owner).or_default().push(*address);
            }
        }
        self.events = events;
        self.events.clear();
    }
}

static REGISTRY: Mutex<Option<Registry>> = Mutex::new(None);

/// Runs without any Python code or Python reference counting inside, so the callback
/// below can never wait for a lock its own thread holds.
fn with_registry<T>(action: impl FnOnce(&mut Registry) -> T) -> T {
    let mut guard = REGISTRY
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    action(guard.get_or_insert_with(Registry::default))
}

unsafe extern "C" fn on_change(
    _event: c_int,
    dict: *mut ffi::PyObject,
    _key: *mut ffi::PyObject,
    _new_value: *mut ffi::PyObject,
) -> c_int {
    with_registry(|registry| registry.events.push(dict as usize));
    0
}

/// Register the dict watcher, given the addresses of PyDict_AddWatcher, PyDict_Watch
/// and PyDict_Unwatch. Returns whether watching can be used.
#[pyfunction]
pub fn install_dict_watcher(add_watcher: usize, watch: usize, unwatch: usize) -> bool {
    if with_registry(|registry| registry.api.is_some()) {
        return true;
    }
    if add_watcher == 0 || watch == 0 || unwatch == 0 {
        return false;
    }
    // SAFETY: the caller passes the addresses of these CPython functions, whose
    // signatures are the ones declared above.
    let (add_watcher, watch, unwatch) = unsafe {
        (
            std::mem::transmute::<usize, AddWatcher>(add_watcher),
            std::mem::transmute::<usize, WatchCall>(watch),
            std::mem::transmute::<usize, WatchCall>(unwatch),
        )
    };
    // SAFETY: on_change never fails and runs no Python code.
    let id = unsafe { add_watcher(on_change) };
    if id < 0 {
        // SAFETY: the failed call set an exception, which is not of interest.
        unsafe { ffi::PyErr_Clear() };
        return false;
    }
    with_registry(|registry| registry.api = Some((id, watch, unwatch)));
    true
}

/// The records of one dict, which changed since the last accepted check.
#[pyclass(module = "sheltersim_native")]
pub struct RecordWatch {
    owner: u64,
    list_field: Option<Py<PyString>>,
    outer: Option<Py<PyDict>>,
    /// The dict's entries at the last accepted check, in order.
    entries: Vec<(Py<PyAny>, Py<PyAny>)>,
    positions: HashMap<usize, usize>,
    /// For each entry, the list in list_field, held so it can be read at every check.
    lists: Vec<Py<PyAny>>,
    // What the check in progress reads, for accept.
    reread_all: bool,
    added: Vec<(Py<PyAny>, Py<PyAny>)>,
    written: Vec<usize>,
}

/// A list of body_state_v1 topic IDs that passes the rules.
fn is_valid_list(list: &Py<PyAny>) -> bool {
    is_exact_list(list.as_ptr()) && topics_valid(list.as_ptr())
}

impl RecordWatch {
    fn stop_watching(&mut self) {
        let owner = self.owner;
        let mut addresses: Vec<usize> = self.positions.keys().copied().collect();
        if let Some(outer) = &self.outer {
            addresses.push(outer.as_ptr() as usize);
        }
        let unwatch = with_registry(|registry| {
            registry.changed.remove(&owner);
            registry.robbed.remove(&owner);
            let Some((id, _, unwatch)) = registry.api else {
                return None;
            };
            // A dict taken over by another RecordWatch stays watched for it.
            addresses.retain(|address| {
                let mine = registry.owners.get(address) == Some(&owner);
                if mine {
                    registry.owners.remove(address);
                }
                mine
            });
            Some((id, unwatch))
        });
        if let Some((id, unwatch)) = unwatch {
            for address in addresses {
                // SAFETY: every address is of a dict still held by self.
                unsafe { unwatch(id, address as *mut ffi::PyObject) };
            }
        }
        // The references go only after the dicts are unwatched.
        self.outer = None;
        self.entries.clear();
        self.lists.clear();
        self.positions.clear();
    }

    fn start_watching(&self, dict: &Bound<'_, PyAny>) -> PyResult<()> {
        let address = dict.as_ptr() as usize;
        let owner = self.owner;
        let call = with_registry(|registry| {
            if let Some(previous) = registry.owners.insert(address, owner) {
                if previous != owner {
                    registry.robbed.insert(previous);
                }
            }
            registry.api.map(|(id, watch, _)| (id, watch))
        });
        let Some((id, watch)) = call else {
            return Err(pyo3::exceptions::PyRuntimeError::new_err("no dict watcher"));
        };
        // SAFETY: dict is a live dict, and the id came from PyDict_AddWatcher.
        if unsafe { watch(id, dict.as_ptr()) } != 0 {
            return Err(PyErr::fetch(dict.py()));
        }
        Ok(())
    }

    fn list_of(&self, record: &Bound<'_, PyAny>) -> PyResult<Option<Py<PyAny>>> {
        let Some(field) = &self.list_field else {
            return Ok(None);
        };
        Ok(record
            .get_item(field.bind(record.py()))
            .ok()
            .map(Bound::unbind))
    }

    fn remember(&mut self, key: Py<PyAny>, record: Py<PyAny>, py: Python<'_>) -> PyResult<()> {
        let bound = record.bind(py).clone();
        self.start_watching(&bound)?;
        self.positions
            .insert(record.as_ptr() as usize, self.entries.len());
        if let Some(list) = self.list_of(&bound)? {
            self.lists.push(list);
        }
        self.entries.push((key, record));
        Ok(())
    }
}

#[pymethods]
impl RecordWatch {
    /// Each record holds in list_field a list of topic IDs, read at every check.
    #[new]
    #[pyo3(signature = (list_field=None))]
    fn new(list_field: Option<Py<PyString>>) -> Self {
        let owner = with_registry(|registry| {
            registry.next_owner += 1;
            registry.next_owner
        });
        Self {
            owner,
            list_field,
            outer: None,
            entries: Vec::new(),
            positions: HashMap::new(),
            lists: Vec::new(),
            reread_all: false,
            added: Vec::new(),
            written: Vec::new(),
        }
    }

    /// The entries to check: the ones written or added since the last accept, or the
    /// whole dict when that is not known. Checking those shows the whole dict is valid.
    fn changed<'py>(&mut self, relationships: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyDict>> {
        let py = relationships.py();
        self.added.clear();
        self.written.clear();
        self.reread_all = false;
        let owner = self.owner;
        let (mut events, robbed) = with_registry(|registry| {
            registry.hand_out_events();
            (
                registry.changed.remove(&owner).unwrap_or_default(),
                registry.robbed.contains(&owner),
            )
        });
        events.sort_unstable();
        events.dedup();
        let same_dict = self
            .outer
            .as_ref()
            .is_some_and(|outer| outer.is(relationships));
        // A list that looks wrong goes to the rules with everything else.
        let mut whole = !same_dict || robbed || !self.lists.iter().all(|list| is_valid_list(list));
        let outer_address = relationships.as_ptr() as usize;
        if !whole && events.binary_search(&outer_address).is_ok() {
            // The dict's own entries changed: the held ones must still lead it.
            let mut position = 0;
            for (key, record) in relationships.iter() {
                match self.entries.get(position) {
                    Some((held_key, held_record)) => {
                        if !held_key.is(&key) || !held_record.is(&record) {
                            whole = true;
                            break;
                        }
                    }
                    None => self.added.push((key.unbind(), record.unbind())),
                }
                position += 1;
            }
            whole = whole || position < self.entries.len();
        }
        if whole {
            self.added.clear();
            self.stop_watching();
            self.reread_all = true;
            return Ok(relationships.clone());
        }
        let changed = PyDict::new(py);
        for address in events {
            if let Some(position) = self.positions.get(&address) {
                let (key, record) = &self.entries[*position];
                changed.set_item(key, record)?;
                self.written.push(*position);
            }
        }
        for (key, record) in &self.added {
            changed.set_item(key, record)?;
        }
        Ok(changed)
    }

    /// The entries given by changed() passed: watch them from now on.
    fn accept(&mut self, relationships: &Bound<'_, PyDict>) -> PyResult<()> {
        let py = relationships.py();
        if self.reread_all {
            self.start_watching(relationships.as_any())?;
            self.outer = Some(relationships.clone().unbind());
            for (key, record) in relationships.iter() {
                self.remember(key.unbind(), record.unbind(), py)?;
            }
        } else {
            for (key, record) in std::mem::take(&mut self.added) {
                self.remember(key, record, py)?;
            }
            if self.list_field.is_some() {
                for position in std::mem::take(&mut self.written) {
                    let record = self.entries[position].1.bind(py).clone();
                    if let Some(list) = self.list_of(&record)? {
                        self.lists[position] = list;
                    }
                }
            }
        }
        self.reread_all = false;
        Ok(())
    }

    /// Forget everything, so the next changed() returns the whole dict.
    fn reset(&mut self) {
        self.stop_watching();
    }
}

impl Drop for RecordWatch {
    fn drop(&mut self) {
        Python::with_gil(|_| self.stop_watching());
    }
}
