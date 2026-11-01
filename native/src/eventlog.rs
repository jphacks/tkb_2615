//! The realized EventLog held in Rust (docs/rust-port.md, stage B1).
//!
//! Each record is its canonical JSON bytes, a Python EventLogRecord object, or both.
//! A record appended from Python has its object and, written in the same step from the
//! object's frozen fields, its bytes. A record a Rust rule appends (`append_row`) and a
//! record read back from a saved Checkpoint start as the bytes; the object is made by the
//! store's factory when Python reads it. The indexes the EventLog keeps (ID, sequence,
//! minute, event type, provenance object keys, care requests, completed support kinds,
//! applied facility events) live here, so lookups and audits need no record objects.
//!
//! The bytes are what restoring the record reads, so a store whose every record has bytes
//! written here or checked here (`all_encoded`) is its own restored log: a save's restore
//! check neither parses those bytes nor compares them with the objects
//! (`core/checkpoint_cache.py`). A record with a value the writer refuses, such as a str
//! subclass in a payload, has only its object; the Python encoder makes its bytes when
//! asked, and the restore check then runs as it always did.
//!
//! Appending checks duplicate IDs and sequences and returns a code; the Python
//! EventLog raises the same DuplicateEventError as before.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple};

use crate::core::value::V;
use crate::state::events::{EventRow, EventStatus};

/// What the indexes and a Rust rule read of one record. Holds no Python handle.
struct Entry {
    event_id: Arc<str>,
    /// The bytes until Python asks for them; then they are in `Handles::bytes`.
    written: Option<Box<[u8]>>,
    event_type: u32,
    applied: bool,
    minute: i64,
}

/// The Python objects of one entry, at the same position in `EventStore::handles`.
#[derive(Default)]
struct Handles {
    record: Option<Py<PyAny>>,
    bytes: Option<Py<PyBytes>>,
}

/// What the indexes read from one record.
struct Fields {
    event_id: String,
    sequence: i64,
    event_type: String,
    minute: i64,
    applied: bool,
    request_id: Option<String>,
    agent_id: Option<String>,
    support_kind: Option<String>,
    payload_status_completed: bool,
    facility_id: Option<String>,
    provenance_objects: Vec<String>,
    // For agent_action_lifecycle records only.
    transition: Option<String>,
}

/// What one agent_action_lifecycle record does to its agent's active action.
#[derive(Clone, Copy, PartialEq)]
enum LifecycleStep {
    Set,
    End,
    Other,
}

const ACTION_LIFECYCLE_EVENT_TYPE: &str = "agent_action_lifecycle";

#[pyclass(module = "sheltersim_native")]
pub struct EventStore {
    entries: Vec<Entry>,
    handles: Vec<Handles>,
    // Records with bytes only the Python encoder can make.
    unencoded: usize,
    type_names: Vec<String>,
    type_ids: HashMap<String, u32>,
    by_id: HashMap<Arc<str>, usize>,
    sequences: HashSet<i64>,
    max_sequence: i64,
    by_minute: BTreeMap<i64, Vec<usize>>,
    by_type: HashMap<u32, Vec<usize>>,
    by_provenance: HashMap<String, Vec<usize>>,
    first_care_request: HashMap<String, String>,
    completed_support_kinds: HashMap<String, Vec<String>>,
    applied_facility_events: HashMap<String, Vec<String>>,
    // In append order: each agent_action_lifecycle record with a str agent_id.
    lifecycle: Vec<(u32, LifecycleStep, usize)>,
    lifecycle_agents: Vec<String>,
    lifecycle_agent_ids: HashMap<String, u32>,
    // Records an audit checked and the bounds it checked them against.
    audited: usize,
    audit_bounds: (Option<i64>, Option<i64>),
    // record -> canonical bytes, and canonical bytes -> record (set from Python).
    encoder: Option<Py<PyAny>>,
    factory: Option<Py<PyAny>>,
    // The exact types whose records `checkpoint::encode_record` may write.
    types: Option<(Py<PyAny>, Py<PyAny>)>,
}

fn mapping_get<'py>(
    mapping: &Bound<'py, PyAny>,
    key: &Bound<'py, PyString>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let value = mapping.call_method1(intern!(mapping.py(), "get"), (key,))?;
    Ok(if value.is_none() { None } else { Some(value) })
}

fn as_str(value: &Option<Bound<'_, PyAny>>) -> Option<String> {
    value
        .as_ref()
        .and_then(|item| item.downcast::<PyString>().ok())
        .and_then(|text| text.to_str().ok().map(str::to_owned))
}

impl EventStore {
    fn empty() -> Self {
        Self {
            entries: Vec::new(),
            handles: Vec::new(),
            unencoded: 0,
            type_names: Vec::new(),
            type_ids: HashMap::new(),
            by_id: HashMap::new(),
            sequences: HashSet::new(),
            max_sequence: -1,
            by_minute: BTreeMap::new(),
            by_type: HashMap::new(),
            by_provenance: HashMap::new(),
            first_care_request: HashMap::new(),
            completed_support_kinds: HashMap::new(),
            applied_facility_events: HashMap::new(),
            lifecycle: Vec::new(),
            lifecycle_agents: Vec::new(),
            lifecycle_agent_ids: HashMap::new(),
            audited: 0,
            audit_bounds: (None, None),
            encoder: None,
            factory: None,
            types: None,
        }
    }

    fn record_at(&mut self, py: Python<'_>, index: usize) -> PyResult<Py<PyAny>> {
        if let Some(record) = &self.handles[index].record {
            return Ok(record.clone_ref(py));
        }
        let bytes = self.bytes_at(py, index)?;
        let factory = self
            .factory
            .as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("no record factory"))?;
        let record = factory.call1(py, (bytes,))?;
        self.handles[index].record = Some(record.clone_ref(py));
        Ok(record)
    }

    fn bytes_at(&mut self, py: Python<'_>, index: usize) -> PyResult<Py<PyBytes>> {
        if let Some(bytes) = &self.handles[index].bytes {
            return Ok(bytes.clone_ref(py));
        }
        let bytes: Py<PyBytes> = if let Some(written) = self.entries[index].written.take() {
            PyBytes::new(py, &written).unbind()
        } else {
            let record = self.handles[index].record.as_ref().unwrap().clone_ref(py);
            let encoder = self
                .encoder
                .as_ref()
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("no record encoder"))?;
            encoder.call1(py, (record,))?.extract(py)?
        };
        self.handles[index].bytes = Some(bytes.clone_ref(py));
        Ok(bytes)
    }

    /// 0, or 1 for a realized ID, 2 for a realized sequence.
    fn add(&mut self, fields: Fields, handles: Handles, written: Option<Box<[u8]>>) -> u8 {
        if self.by_id.contains_key(fields.event_id.as_str()) {
            return 1;
        }
        if self.sequences.contains(&fields.sequence) {
            return 2;
        }
        let index = self.entries.len();
        if fields.event_type == "care_support_transition" {
            if let Some(request_id) = &fields.request_id {
                self.first_care_request
                    .entry(request_id.clone())
                    .or_insert_with(|| fields.event_id.clone());
            }
            if let (true, true, Some(agent), Some(kind)) = (
                fields.applied,
                fields.payload_status_completed,
                &fields.agent_id,
                &fields.support_kind,
            ) {
                let kinds = self.completed_support_kinds.entry(agent.clone()).or_default();
                if !kinds.contains(kind) {
                    kinds.push(kind.clone());
                }
            }
        }
        if fields.applied
            && (fields.event_type == "facility_outage" || fields.event_type == "facility_recovery")
        {
            if let Some(facility_id) = &fields.facility_id {
                let events = self
                    .applied_facility_events
                    .entry(facility_id.clone())
                    .or_default();
                if !events.contains(&fields.event_id) {
                    events.push(fields.event_id.clone());
                }
            }
        }
        if fields.event_type == ACTION_LIFECYCLE_EVENT_TYPE {
            if let Some(agent) = &fields.agent_id {
                let agent = match self.lifecycle_agent_ids.get(agent) {
                    Some(id) => *id,
                    None => {
                        let id = self.lifecycle_agents.len() as u32;
                        self.lifecycle_agents.push(agent.clone());
                        self.lifecycle_agent_ids.insert(agent.clone(), id);
                        id
                    }
                };
                let step = match fields.transition.as_deref() {
                    Some("started" | "continued") => LifecycleStep::Set,
                    Some("completed" | "interrupted" | "abandoned" | "censored") => {
                        LifecycleStep::End
                    }
                    _ => LifecycleStep::Other,
                };
                self.lifecycle.push((agent, step, index));
            }
        }
        for key in fields.provenance_objects {
            self.by_provenance.entry(key).or_default().push(index);
        }
        let type_id = self.type_id(&fields.event_type);
        if written.is_none() && handles.bytes.is_none() {
            self.unencoded += 1;
        }
        let event_id: Arc<str> = Arc::from(fields.event_id);
        self.entries.push(Entry {
            event_id: event_id.clone(),
            written,
            event_type: type_id,
            applied: fields.applied,
            minute: fields.minute,
        });
        self.handles.push(handles);
        self.by_id.insert(event_id, index);
        self.sequences.insert(fields.sequence);
        self.max_sequence = self.max_sequence.max(fields.sequence);
        self.by_minute.entry(fields.minute).or_default().push(index);
        self.by_type.entry(type_id).or_default().push(index);
        0
    }

    fn type_id(&mut self, name: &str) -> u32 {
        if let Some(id) = self.type_ids.get(name) {
            return *id;
        }
        let id = self.type_names.len() as u32;
        self.type_names.push(name.to_owned());
        self.type_ids.insert(name.to_owned(), id);
        id
    }

    fn records_of(
        &mut self,
        py: Python<'_>,
        indices: impl Iterator<Item = usize>,
    ) -> PyResult<Vec<Py<PyAny>>> {
        indices.map(|index| self.record_at(py, index)).collect()
    }
}

#[pymethods]
impl EventStore {
    #[new]
    fn new() -> Self {
        Self::empty()
    }

    /// The encoder (record -> canonical JSON bytes), the factory (bytes -> record), and the
    /// only types of record and event whose bytes are written on appending.
    #[pyo3(signature = (encoder, factory, record_type=None, event_type=None))]
    fn set_codecs(
        &mut self,
        encoder: Py<PyAny>,
        factory: Py<PyAny>,
        record_type: Option<Py<PyAny>>,
        event_type: Option<Py<PyAny>>,
    ) {
        self.encoder = Some(encoder);
        self.factory = Some(factory);
        self.types = record_type.zip(event_type);
    }

    /// Append one EventLogRecord: 0, or 1 for a realized ID, 2 for a realized sequence.
    fn append(&mut self, py: Python<'_>, record: &Bound<'_, PyAny>) -> PyResult<u8> {
        let fields = fields_of_record(record)?;
        if self.by_id.contains_key(fields.event_id.as_str()) {
            return Ok(1);
        }
        if self.sequences.contains(&fields.sequence) {
            return Ok(2);
        }
        let written = match &self.types {
            // Anything this can not write waits for the Python encoder, which raises its own
            // error at the save, as it always did.
            Some((record_type, event_type)) => {
                crate::checkpoint::encode_record(record, record_type.bind(py), event_type.bind(py))
                    .unwrap_or(None)
                    .map(Vec::into_boxed_slice)
            }
            None => None,
        };
        let handles = Handles { record: Some(record.clone().unbind()), bytes: None };
        Ok(self.add(fields, handles, written))
    }

    /// Whether every record has bytes written or checked here, so restoring this log gives
    /// the log itself.
    fn all_encoded(&self) -> bool {
        self.unencoded == 0
    }

    /// Append records saved as canonical JSON bytes (event_log documents of a
    /// Checkpoint), in order: 0, or the code of the first refused one.
    fn extend_saved(&mut self, py: Python<'_>, items: &Bound<'_, PyAny>) -> PyResult<u8> {
        let mut saved: Vec<Py<PyBytes>> = Vec::new();
        for item in items.try_iter()? {
            saved.push(item?.downcast_into::<PyBytes>()?.unbind());
        }
        // Records are parsed and checked in parallel without the GIL (the bytes are
        // immutable and held here), then added in order, as one by one.
        let slices: Vec<&[u8]> = saved.iter().map(|bytes| bytes.as_bytes(py)).collect();
        let parsed: Vec<Result<Fields, String>> = py.allow_threads(|| {
            use rayon::prelude::*;
            slices.par_iter().map(|bytes| fields_of_bytes(bytes)).collect()
        });
        for (fields, bytes) in parsed.into_iter().zip(saved) {
            let fields = fields.map_err(pyo3::exceptions::PyValueError::new_err)?;
            let refused = self.add(fields, Handles { record: None, bytes: Some(bytes) }, None);
            if refused != 0 {
                return Ok(refused);
            }
        }
        Ok(0)
    }

    /// The canonical JSON bytes of the records from start to end.
    fn items_bytes(&mut self, py: Python<'_>, start: usize, end: usize) -> PyResult<Vec<Py<PyBytes>>> {
        let end = end.min(self.entries.len());
        (start.min(end)..end).map(|index| self.bytes_at(py, index)).collect()
    }

    /// How many records have no object yet (read back and not read since).
    fn unread(&self) -> usize {
        self.handles.iter().filter(|handles| handles.record.is_none()).count()
    }

    fn __len__(&self) -> usize {
        self.entries.len()
    }

    fn contains_id(&self, event_id: &str) -> bool {
        self.by_id.contains_key(event_id)
    }

    fn contains_sequence(&self, sequence: i64) -> bool {
        self.sequences.contains(&sequence)
    }

    #[getter]
    fn max_sequence(&self) -> i64 {
        self.max_sequence
    }

    fn find(&mut self, py: Python<'_>, event_id: &Bound<'_, PyAny>) -> PyResult<Option<Py<PyAny>>> {
        let Ok(text) = event_id.downcast::<PyString>() else {
            return Ok(None);
        };
        match self.by_id.get(text.to_str()?).copied() {
            Some(index) => Ok(Some(self.record_at(py, index)?)),
            None => Ok(None),
        }
    }

    fn first_care_request_event(&self, request_id: &str) -> Option<String> {
        self.first_care_request.get(request_id).cloned()
    }

    fn completed_support_kinds(&self, agent_id: &str) -> Vec<String> {
        self.completed_support_kinds
            .get(agent_id)
            .cloned()
            .unwrap_or_default()
    }

    fn applied_facility_event_ids(&self, facility_id: &str) -> Vec<String> {
        self.applied_facility_events
            .get(facility_id)
            .cloned()
            .unwrap_or_default()
    }

    fn at_minute(&mut self, py: Python<'_>, minute: i64) -> PyResult<Vec<Py<PyAny>>> {
        let indices = self.by_minute.get(&minute).cloned().unwrap_or_default();
        self.records_of(py, indices.into_iter())
    }

    fn event_types(&self) -> Vec<String> {
        self.by_type
            .keys()
            .map(|id| self.type_names[*id as usize].clone())
            .collect()
    }

    fn with_provenance_object(&mut self, py: Python<'_>, key: &str) -> PyResult<Vec<Py<PyAny>>> {
        let indices = self.by_provenance.get(key).cloned().unwrap_or_default();
        self.records_of(py, indices.into_iter())
    }

    fn of_types(&mut self, py: Python<'_>, event_types: &Bound<'_, PyAny>) -> PyResult<Vec<Py<PyAny>>> {
        let mut indices: Vec<usize> = Vec::new();
        let mut seen: HashSet<u32> = HashSet::new();
        for name in event_types.try_iter()? {
            let name = name?;
            let Ok(text) = name.downcast::<PyString>() else {
                continue;
            };
            if let Some(id) = self.type_ids.get(text.to_str()?) {
                if seen.insert(*id) {
                    if let Some(list) = self.by_type.get(id) {
                        indices.extend(list.iter().copied());
                    }
                }
            }
        }
        if seen.len() > 1 {
            indices.sort_unstable();
        }
        self.records_of(py, indices.into_iter())
    }

    /// The agent_action_lifecycle records that decide each agent's active action, in
    /// the order a pass over all of them inserts the agents into a dict: a pass over
    /// just these gives the same dict, without making the other records' objects.
    fn active_lifecycle_records(&mut self, py: Python<'_>) -> PyResult<Vec<Py<PyAny>>> {
        // Agent -> (position of its dict entry, its last record that set the entry).
        let mut active: HashMap<u32, (usize, usize)> = HashMap::new();
        for (order, (agent, step, index)) in self.lifecycle.iter().enumerate() {
            match step {
                LifecycleStep::Set => {
                    active
                        .entry(*agent)
                        .and_modify(|entry| entry.1 = *index)
                        .or_insert((order, *index));
                }
                LifecycleStep::End => {
                    active.remove(agent);
                }
                LifecycleStep::Other => {}
            }
        }
        let mut entries: Vec<(usize, usize)> = active.into_values().collect();
        entries.sort_unstable();
        self.records_of(py, entries.into_iter().map(|(_, index)| index))
    }

    /// The records of event_types at position start or later, in append order.
    fn of_types_from(
        &mut self,
        py: Python<'_>,
        event_types: &Bound<'_, PyAny>,
        start: usize,
    ) -> PyResult<Vec<Py<PyAny>>> {
        let mut indices: Vec<usize> = Vec::new();
        let mut seen: HashSet<u32> = HashSet::new();
        for name in event_types.try_iter()? {
            let name = name?;
            let Ok(text) = name.downcast::<PyString>() else {
                continue;
            };
            if let Some(id) = self.type_ids.get(text.to_str()?) {
                if seen.insert(*id) {
                    if let Some(list) = self.by_type.get(id) {
                        let first = list.partition_point(|index| *index < start);
                        indices.extend(list[first..].iter().copied());
                    }
                }
            }
        }
        if seen.len() > 1 {
            indices.sort_unstable();
        }
        self.records_of(py, indices.into_iter())
    }

    /// The records from start on, in append order.
    #[pyo3(signature = (start=0))]
    fn records_from(&mut self, py: Python<'_>, start: usize) -> PyResult<Vec<Py<PyAny>>> {
        let start = start.min(self.entries.len());
        let end = self.entries.len();
        self.records_of(py, start..end)
    }

    fn last(&mut self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        match self.entries.len() {
            0 => Ok(None),
            length => Ok(Some(self.record_at(py, length - 1)?)),
        }
    }

    fn event_ids<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        PyList::new(py, self.entries.iter().map(|entry| &*entry.event_id))
    }

    /// The event IDs of `at_minute(minute)`, making no record object.
    fn event_ids_at_minute<'py>(&self, py: Python<'py>, minute: i64) -> PyResult<Bound<'py, PyList>> {
        let indices = self.by_minute.get(&minute).map_or(&[][..], Vec::as_slice);
        PyList::new(py, indices.iter().map(|index| &*self.entries[*index].event_id))
    }

    fn recent(&mut self, py: Python<'_>, limit: usize) -> PyResult<Vec<Py<PyAny>>> {
        let end = self.entries.len();
        let start = end.saturating_sub(limit);
        self.records_of(py, start..end)
    }

    /// Up to limit applied records of event_types from the end, scanning at most
    /// scan_limit records, in append order; and whether records were left unscanned.
    fn recent_matching(
        &mut self,
        py: Python<'_>,
        event_types: &Bound<'_, PyAny>,
        limit: usize,
        scan_limit: usize,
    ) -> PyResult<(Vec<Py<PyAny>>, bool)> {
        let mut wanted: HashSet<u32> = HashSet::new();
        for name in event_types.try_iter()? {
            let name = name?;
            if let Ok(text) = name.downcast::<PyString>() {
                if let Some(id) = self.type_ids.get(text.to_str()?) {
                    wanted.insert(*id);
                }
            }
        }
        let mut found: Vec<usize> = Vec::new();
        let mut scanned = 0;
        for index in (0..self.entries.len()).rev() {
            if scanned == scan_limit {
                break;
            }
            scanned += 1;
            let entry = &self.entries[index];
            if entry.applied && wanted.contains(&entry.event_type) {
                found.push(index);
                if found.len() == limit {
                    break;
                }
            }
        }
        found.reverse();
        let more = scanned < self.entries.len();
        Ok((self.records_of(py, found.into_iter())?, more))
    }

    /// Audit realization times: 0, 3 for a future realization, 4 for one outside the
    /// duration. IDs and sequences are unique by construction. Records an earlier
    /// audit checked at an earlier or the same minute and duration are not checked
    /// again.
    #[pyo3(signature = (minute_index=None, duration_minutes=None))]
    fn audit(&mut self, minute_index: Option<i64>, duration_minutes: Option<i64>) -> u8 {
        let (audited_minute, audited_duration) = self.audit_bounds;
        let mut start = self.audited.min(self.entries.len());
        if let Some(minute) = minute_index {
            if audited_minute.is_none_or(|previous| previous > minute) {
                start = 0;
            }
        }
        if duration_minutes.is_some() && audited_duration != duration_minutes {
            start = 0;
        }
        for entry in &self.entries[start..] {
            if minute_index.is_some_and(|minute| entry.minute > minute) {
                return 3;
            }
            if duration_minutes.is_some_and(|duration| entry.minute >= duration) {
                return 4;
            }
        }
        self.audited = self.entries.len();
        self.audit_bounds = (minute_index, duration_minutes);
        0
    }

    /// An independent store sharing the frozen records.
    fn copy(&self, py: Python<'_>) -> Self {
        Self {
            entries: self
                .entries
                .iter()
                .map(|entry| Entry {
                    event_id: entry.event_id.clone(),
                    written: entry.written.clone(),
                    event_type: entry.event_type,
                    applied: entry.applied,
                    minute: entry.minute,
                })
                .collect(),
            handles: self
                .handles
                .iter()
                .map(|handles| Handles {
                    record: handles.record.as_ref().map(|record| record.clone_ref(py)),
                    bytes: handles.bytes.as_ref().map(|bytes| bytes.clone_ref(py)),
                })
                .collect(),
            unencoded: self.unencoded,
            type_names: self.type_names.clone(),
            type_ids: self.type_ids.clone(),
            by_id: self.by_id.clone(),
            sequences: self.sequences.clone(),
            max_sequence: self.max_sequence,
            by_minute: self.by_minute.clone(),
            by_type: self.by_type.clone(),
            by_provenance: self.by_provenance.clone(),
            first_care_request: self.first_care_request.clone(),
            completed_support_kinds: self.completed_support_kinds.clone(),
            applied_facility_events: self.applied_facility_events.clone(),
            lifecycle: self.lifecycle.clone(),
            lifecycle_agents: self.lifecycle_agents.clone(),
            lifecycle_agent_ids: self.lifecycle_agent_ids.clone(),
            audited: self.audited,
            audit_bounds: self.audit_bounds,
            encoder: self.encoder.as_ref().map(|encoder| encoder.clone_ref(py)),
            factory: self.factory.as_ref().map(|factory| factory.clone_ref(py)),
            types: self
                .types
                .as_ref()
                .map(|(record, event)| (record.clone_ref(py), event.clone_ref(py))),
        }
    }

    /// The indexes' sizes, for tests that compare them with the records.
    fn index_sizes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let sizes = PyDict::new(py);
        sizes.set_item("records", self.entries.len())?;
        sizes.set_item("ids", self.by_id.len())?;
        sizes.set_item("sequences", self.sequences.len())?;
        Ok(sizes)
    }
}

/// How the log holds an event a rule asks about (`EventStore::applied_outcome`).
pub(crate) enum AppliedOutcome {
    /// Not realized, or realized but not applied.
    No,
    /// Applied, and the record holds this very object.
    ThisEvent,
    /// Applied, in a record that holds another object of the same ID, with no bytes to read.
    AnotherObject,
    /// Applied, in a record of another object or none yet: the event its bytes hold.
    SavedRow(EventRow),
}

/// What a Rust rule does with the log: no Python record is made or read.
impl EventStore {
    /// `record = log.find(event_id)`, `record.status is APPLIED` and `record.event is event`.
    pub(crate) fn applied_outcome(&self, event_id: &str, event: &Bound<'_, PyAny>) -> PyResult<AppliedOutcome> {
        let py = event.py();
        let Some(index) = self.by_id.get(event_id).copied() else {
            return Ok(AppliedOutcome::No);
        };
        if !self.entries[index].applied {
            return Ok(AppliedOutcome::No);
        }
        if let Some(record) = &self.handles[index].record {
            if record.bind(py).getattr(intern!(py, "event"))?.is(event) {
                return Ok(AppliedOutcome::ThisEvent);
            }
        }
        Ok(match self.saved_document(py, index) {
            Some((row, ..)) => AppliedOutcome::SavedRow(row),
            None => AppliedOutcome::AnotherObject,
        })
    }

    /// `EventLog.append` of an outcome of `event`: 0, or 1 for a realized ID, 2 for a
    /// realized sequence, with nothing changed. `crate::state::events::EventWriter::record`
    /// raises the errors `EventLog.append` raises.
    pub(crate) fn append_row(
        &mut self,
        event: &EventRow,
        realized_minute: i64,
        status: EventStatus,
        detail: Option<&str>,
    ) -> PyResult<u8> {
        if self.by_id.contains_key(&*event.event_id) {
            return Ok(1);
        }
        if self.sequences.contains(&event.sequence) {
            return Ok(2);
        }
        let written = event.record_json(realized_minute, status, detail)?;
        Ok(self.add(event.index_fields(realized_minute, status), Handles::default(), Some(written)))
    }

    /// `EventLog.append` of an outcome of an event object, with no record object: its bytes come
    /// from the same writer as a record's, and Python makes the record when it reads one. 3, with
    /// nothing changed, for a value the writer refuses, which needs Python's record and encoder.
    pub(crate) fn append_outcome(
        &mut self,
        event: &Bound<'_, PyAny>,
        realized_minute: i64,
        status: EventStatus,
        detail: Option<&str>,
    ) -> PyResult<u8> {
        let py = event.py();
        let Some((record_type, event_type)) = &self.types else {
            return Ok(3);
        };
        if !event.get_type().is(event_type.bind(py)) || record_type.is_none(py) {
            return Ok(3);
        }
        let fields = fields_of_outcome(event, realized_minute, status == EventStatus::Applied)?;
        if self.by_id.contains_key(fields.event_id.as_str()) {
            return Ok(1);
        }
        if self.sequences.contains(&fields.sequence) {
            return Ok(2);
        }
        let detail = match detail {
            Some(detail) => PyString::new(py, detail).into_any(),
            None => py.None().into_bound(py),
        };
        let written = crate::checkpoint::encode_outcome(
            event,
            realized_minute.into_pyobject(py)?.as_any(),
            PyString::new(py, status.as_str()).as_any(),
            &detail,
        )
        .unwrap_or(None);
        let Some(written) = written else {
            return Ok(3);
        };
        Ok(self.add(fields, Handles::default(), Some(written.into_boxed_slice())))
    }

    pub(crate) fn has_event_id(&self, event_id: &str) -> bool {
        self.by_id.contains_key(event_id)
    }

    pub(crate) fn has_sequence(&self, sequence: i64) -> bool {
        self.sequences.contains(&sequence)
    }

    /// The positions of a type's records in append order; empty for a type with none.
    pub(crate) fn positions_of_type(&self, event_type: &str) -> &[usize] {
        self.type_ids
            .get(event_type)
            .and_then(|id| self.by_type.get(id))
            .map_or(&[], Vec::as_slice)
    }

    /// The saved document of the record at a position: `(event, realized_minute, status, detail)`,
    /// read from its bytes. None for a record whose bytes only Python can make.
    pub(crate) fn saved_document(
        &self,
        py: Python<'_>,
        index: usize,
    ) -> Option<(EventRow, i64, EventStatus, Option<Arc<str>>)> {
        let bytes = match (&self.entries.get(index)?.written, &self.handles[index].bytes) {
            (Some(written), _) => &**written,
            (None, Some(bytes)) => bytes.as_bytes(py),
            (None, None) => return None,
        };
        EventRow::from_saved_record(bytes)
    }
}

pub(crate) struct NewFields<'a> {
    pub(crate) event_id: &'a str,
    pub(crate) sequence: i64,
    pub(crate) event_type: &'a str,
    pub(crate) minute: i64,
    pub(crate) applied: bool,
    pub(crate) payload: Option<&'a V>,
    pub(crate) provenance: Option<&'a V>,
}

fn fields_of_row(new: NewFields<'_>) -> Fields {
    let text = |key: &str| match new.payload {
        Some(V::Map(payload)) => match payload.get(key) {
            Some(V::Str(value)) => Some(String::from(&**value)),
            _ => None,
        },
        _ => None,
    };
    let provenance_objects = match new.provenance {
        Some(V::Map(provenance)) => provenance
            .iter()
            .filter(|(_, value)| matches!(value, V::Map(_)))
            .map(|(key, _)| String::from(&**key))
            .collect(),
        _ => Vec::new(),
    };
    Fields {
        event_id: new.event_id.to_owned(),
        sequence: new.sequence,
        event_type: new.event_type.to_owned(),
        minute: new.minute,
        applied: new.applied,
        request_id: text("request_id"),
        agent_id: text("agent_id"),
        support_kind: text("support_kind"),
        payload_status_completed: text("status").as_deref() == Some("completed"),
        facility_id: text("facility_id"),
        provenance_objects,
        transition: text("transition"),
    }
}

impl EventRow {
    fn index_fields(&self, minute: i64, status: EventStatus) -> Fields {
        fields_of_row(NewFields {
            event_id: &self.event_id,
            sequence: self.sequence,
            event_type: &self.event_type,
            minute,
            applied: status == EventStatus::Applied,
            payload: Some(&self.payload),
            provenance: Some(&self.provenance),
        })
    }
}

fn fields_of_record(record: &Bound<'_, PyAny>) -> PyResult<Fields> {
    let py = record.py();
    let event = record.getattr(intern!(py, "event"))?;
    let status: String = record.getattr(intern!(py, "status"))?.getattr(intern!(py, "_value_"))?.extract()?;
    let minute = record.getattr(intern!(py, "realized_minute"))?.extract()?;
    fields_of_outcome(&event, minute, status == "applied")
}

fn fields_of_outcome(event: &Bound<'_, PyAny>, minute: i64, applied: bool) -> PyResult<Fields> {
    let py = event.py();
    let payload = event.getattr(intern!(py, "payload"))?;
    let mut provenance_objects = Vec::new();
    // isinstance(value, collections.abc.Mapping), as the Python EventLog checks. Nearly every
    // value is a str, which the costly check would only reject.
    let mut mapping_type = None;
    for item in event.getattr(intern!(py, "provenance"))?.call_method0(intern!(py, "items"))?.try_iter()? {
        let item = item?;
        let pair = item.downcast::<PyTuple>()?;
        let value = pair.get_item(1)?;
        if value.is_exact_instance_of::<PyString>() {
            continue;
        }
        if mapping_type.is_none() {
            mapping_type = Some(py.import("collections.abc")?.getattr(intern!(py, "Mapping"))?);
        }
        if value.is_instance(mapping_type.as_ref().expect("set above"))? {
            provenance_objects.push(pair.get_item(0)?.extract()?);
        }
    }
    let event_type: String = event.getattr(intern!(py, "event_type"))?.extract()?;
    let mut fields = Fields {
        event_id: event.getattr(intern!(py, "event_id"))?.extract()?,
        sequence: event.getattr(intern!(py, "sequence"))?.extract()?,
        minute,
        applied,
        request_id: None,
        agent_id: None,
        support_kind: None,
        payload_status_completed: false,
        facility_id: None,
        provenance_objects,
        transition: None,
        event_type,
    };
    // `add` reads the payload of these types only.
    match fields.event_type.as_str() {
        "care_support_transition" => {
            fields.request_id = as_str(&mapping_get(&payload, intern!(py, "request_id"))?);
            fields.agent_id = as_str(&mapping_get(&payload, intern!(py, "agent_id"))?);
            fields.support_kind = as_str(&mapping_get(&payload, intern!(py, "support_kind"))?);
            fields.payload_status_completed = mapping_get(&payload, intern!(py, "status"))?
                .map(|value| value.eq("completed"))
                .transpose()?
                .unwrap_or(false);
        }
        "facility_outage" | "facility_recovery" => {
            fields.facility_id = as_str(&mapping_get(&payload, intern!(py, "facility_id"))?);
        }
        ACTION_LIFECYCLE_EVENT_TYPE => {
            fields.agent_id = as_str(&mapping_get(&payload, intern!(py, "agent_id"))?);
            fields.transition = as_str(&mapping_get(&payload, intern!(py, "transition"))?);
        }
        _ => {}
    }
    Ok(fields)
}

fn bad_document(what: &str) -> String {
    format!("saved EventLog record: {what}")
}

/// The index fields of one saved record, after the checks EventLogRecord makes and a
/// check that the bytes are its canonical JSON. Runs without the GIL.
fn fields_of_bytes(bytes: &[u8]) -> Result<Fields, String> {
    use serde_json::Value;
    let document: Value =
        serde_json::from_slice(bytes).map_err(|error| bad_document(&error.to_string()))?;
    check_saved_record(&document).map_err(|what| bad_document(what))?;
    let mut canonical = Vec::with_capacity(bytes.len());
    write_canonical(&mut canonical, &document).map_err(|what| bad_document(what))?;
    if canonical != bytes {
        return Err(bad_document("record is not canonical JSON"));
    }
    let event = document.get("event").ok_or_else(|| bad_document("no event"))?;
    let text = |value: Option<&Value>| value.and_then(Value::as_str).map(str::to_owned);
    let payload = event.get("payload");
    let payload_text = |key: &str| text(payload.and_then(|value| value.get(key)));
    let mut provenance_objects = Vec::new();
    if let Some(Value::Object(provenance)) = event.get("provenance") {
        for (key, value) in provenance {
            if value.is_object() {
                provenance_objects.push(key.clone());
            }
        }
    }
    Ok(Fields {
        event_id: text(event.get("event_id")).ok_or_else(|| bad_document("event_id"))?,
        sequence: event
            .get("sequence")
            .and_then(Value::as_i64)
            .ok_or_else(|| bad_document("sequence"))?,
        event_type: text(event.get("event_type")).ok_or_else(|| bad_document("event_type"))?,
        minute: document
            .get("realized_minute")
            .and_then(Value::as_i64)
            .ok_or_else(|| bad_document("realized_minute"))?,
        applied: document.get("status").and_then(Value::as_str) == Some("applied"),
        request_id: payload_text("request_id"),
        agent_id: payload_text("agent_id"),
        support_kind: payload_text("support_kind"),
        payload_status_completed: payload
            .and_then(|value| value.get("status"))
            .and_then(Value::as_str)
            == Some("completed"),
        facility_id: payload_text("facility_id"),
        provenance_objects,
        transition: payload_text("transition"),
    })
}

pub(crate) const RECORD_FIELDS: [&str; 4] = ["detail", "event", "realized_minute", "status"];
pub(crate) const EVENT_FIELDS: [&str; 10] = [
    "causation_id",
    "due_minute",
    "event_id",
    "event_type",
    "kind",
    "payload",
    "priority",
    "provenance",
    "sequence",
    "source_id",
];
const EVENT_KINDS: [&str; 4] = ["exogenous", "completion", "endogenous", "intervention"];
const EVENT_STATUSES: [&str; 3] = ["applied", "failed", "suppressed"];

/// The field names a saved record and its event must have, for a test against
/// the Python models.
#[pyfunction]
pub fn saved_record_fields() -> (Vec<&'static str>, Vec<&'static str>) {
    (RECORD_FIELDS.to_vec(), EVENT_FIELDS.to_vec())
}

fn stable_id(value: &serde_json::Value) -> bool {
    // models._STABLE_ID: ^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$
    let Some(text) = value.as_str() else {
        return false;
    };
    let bytes = text.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 64
        && bytes[0].is_ascii_alphanumeric()
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(byte))
}

fn exact_int(value: &serde_json::Value, minimum: Option<i64>) -> bool {
    // A JSON number with a fraction or exponent reads back as a float in Python.
    let integral = matches!(value, serde_json::Value::Number(number)
        if !number.to_string().contains(['.', 'e', 'E']));
    integral && value.as_i64().is_some_and(|number| minimum.is_none_or(|low| number >= low))
}

/// The checks EventLogRecord and ScheduledEvent.from_saved make of a saved record.
fn check_saved_record(document: &serde_json::Value) -> Result<(), &'static str> {
    use serde_json::Value;
    let has_fields = |value: &Value, names: &[&str]| {
        value
            .as_object()
            .is_some_and(|object| object.len() == names.len() && names.iter().all(|name| object.contains_key(*name)))
    };
    if !has_fields(document, &RECORD_FIELDS) {
        return Err("record fields do not match EventLogRecord");
    }
    if !exact_int(&document["realized_minute"], Some(0)) {
        return Err("realized_minute must be a non-negative integer");
    }
    if !document["status"].as_str().is_some_and(|status| EVENT_STATUSES.contains(&status)) {
        return Err("unknown event status");
    }
    let event = &document["event"];
    if !has_fields(event, &EVENT_FIELDS) {
        return Err("event fields do not match ScheduledEvent");
    }
    if !stable_id(&event["event_id"]) || !stable_id(&event["event_type"]) {
        return Err("event_id and event_type must be stable IDs");
    }
    for name in ["source_id", "causation_id"] {
        if !event[name].is_null() && !stable_id(&event[name]) {
            return Err("source_id and causation_id must be stable IDs");
        }
    }
    if !event["kind"].as_str().is_some_and(|kind| EVENT_KINDS.contains(&kind)) {
        return Err("unknown event kind");
    }
    if !exact_int(&event["due_minute"], Some(0)) || !exact_int(&event["sequence"], Some(0)) {
        return Err("event due_minute and sequence must be non-negative integers");
    }
    if !exact_int(&event["priority"], None) {
        return Err("event priority must be an integer");
    }
    if !event["payload"].is_object() || !event["provenance"].is_object() {
        return Err("event payload and provenance must be objects");
    }
    Ok(())
}

/// json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) of
/// a value read from JSON, numbers written as Python reads and writes them back.
fn write_canonical(out: &mut Vec<u8>, value: &serde_json::Value) -> Result<(), &'static str> {
    use serde_json::Value;
    match value {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(flag) => out.extend_from_slice(if *flag { b"true" } else { b"false" }),
        Value::Number(number) => {
            let text = number.to_string();
            if text.contains(['.', 'e', 'E']) {
                let parsed: f64 = text.parse().map_err(|_| "invalid number")?;
                if !parsed.is_finite() {
                    return Err("number is not finite");
                }
                out.extend_from_slice(crate::python_float_repr(parsed).as_bytes());
            } else if text == "-0" {
                out.push(b'0');
            } else {
                out.extend_from_slice(text.as_bytes());
            }
        }
        Value::String(text) => crate::write_json_string(out, text),
        Value::Array(items) => {
            out.push(b'[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                write_canonical(out, item)?;
            }
            out.push(b']');
        }
        Value::Object(object) => {
            // serde_json's map is ordered by key bytes, as Python orders str.
            out.push(b'{');
            for (index, (key, item)) in object.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                crate::write_json_string(out, key);
                out.push(b':');
                write_canonical(out, item)?;
            }
            out.push(b'}');
        }
    }
    Ok(())
}

/// The bytes of each item of a JSON array, split where JSON does: at commas outside
/// strings and nested values. raw must be one canonical JSON array.
#[pyfunction]
pub fn split_json_array<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Vec<Bound<'py, PyBytes>>> {
    let bad = || pyo3::exceptions::PyValueError::new_err("not a JSON array");
    let length = raw.len();
    if length < 2 || raw[0] != b'[' || raw[length - 1] != b']' {
        return Err(bad());
    }
    let mut items = Vec::new();
    let (mut depth, mut in_string, mut escaped) = (0usize, false, false);
    let mut start = 1;
    for (index, &byte) in raw.iter().enumerate().take(length - 1).skip(1) {
        if in_string {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                in_string = false;
            }
            continue;
        }
        match byte {
            b'"' => in_string = true,
            b'[' | b'{' => depth += 1,
            b']' | b'}' => depth = depth.checked_sub(1).ok_or_else(bad)?,
            b',' if depth == 0 => {
                items.push(PyBytes::new(py, &raw[start..index]));
                start = index + 1;
            }
            _ => {}
        }
    }
    if in_string || depth != 0 {
        return Err(bad());
    }
    if start < length - 1 {
        items.push(PyBytes::new(py, &raw[start..length - 1]));
    }
    Ok(items)
}
