//! The realized EventLog held in Rust (docs/rust-port.md, stage B1).
//!
//! The records stay the Python EventLogRecord objects the dispatcher appended; the
//! indexes the EventLog keeps (ID, sequence, minute, event type, provenance object
//! keys, care requests, completed support kinds, applied facility events) live
//! here, so lookups and audits run without Python containers.
//!
//! Appending checks duplicate IDs and sequences and returns a code; the Python
//! EventLog raises the same DuplicateEventError as before.

use std::collections::{BTreeMap, HashMap, HashSet};

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

struct Entry {
    record: Py<PyAny>,
    event_type: u32,
    applied: bool,
    minute: i64,
}

#[pyclass(module = "sheltersim_native")]
pub struct EventStore {
    entries: Vec<Entry>,
    type_names: Vec<String>,
    type_ids: HashMap<String, u32>,
    by_id: HashMap<String, usize>,
    sequences: HashSet<i64>,
    max_sequence: i64,
    by_minute: BTreeMap<i64, Vec<usize>>,
    by_type: HashMap<u32, Vec<usize>>,
    by_provenance: HashMap<String, Vec<usize>>,
    first_care_request: HashMap<String, String>,
    completed_support_kinds: HashMap<String, Vec<String>>,
    applied_facility_events: HashMap<String, Vec<String>>,
    // Records an audit checked and the bounds it checked them against.
    audited: usize,
    audit_bounds: (Option<i64>, Option<i64>),
}

fn mapping_get<'py>(
    mapping: &Bound<'py, PyAny>,
    key: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let value = mapping.call_method1("get", (key,))?;
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
            audited: 0,
            audit_bounds: (None, None),
        }
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

    fn records_of(&self, py: Python<'_>, indices: impl Iterator<Item = usize>) -> Vec<Py<PyAny>> {
        indices.map(|index| self.entries[index].record.clone_ref(py)).collect()
    }
}

#[pymethods]
impl EventStore {
    #[new]
    fn new() -> Self {
        Self::empty()
    }

    /// Append one EventLogRecord: 0, or 1 for a realized ID, 2 for a realized sequence.
    fn append(&mut self, record: &Bound<'_, PyAny>) -> PyResult<u8> {
        let event = record.getattr("event")?;
        let event_id: String = event.getattr("event_id")?.extract()?;
        let sequence: i64 = event.getattr("sequence")?.extract()?;
        if self.by_id.contains_key(&event_id) {
            return Ok(1);
        }
        if self.sequences.contains(&sequence) {
            return Ok(2);
        }
        let event_type: String = event.getattr("event_type")?.extract()?;
        let minute: i64 = record.getattr("realized_minute")?.extract()?;
        let status: String = record.getattr("status")?.getattr("value")?.extract()?;
        let applied = status == "applied";
        let payload = event.getattr("payload")?;
        let index = self.entries.len();
        if event_type == "care_support_transition" {
            if let Some(request_id) = as_str(&mapping_get(&payload, "request_id")?) {
                self.first_care_request
                    .entry(request_id)
                    .or_insert_with(|| event_id.clone());
            }
            let agent_id = as_str(&mapping_get(&payload, "agent_id")?);
            let support_kind = as_str(&mapping_get(&payload, "support_kind")?);
            let completed = mapping_get(&payload, "status")?
                .map(|value| value.eq("completed"))
                .transpose()?
                .unwrap_or(false);
            if let (true, true, Some(agent), Some(kind)) = (applied, completed, agent_id, support_kind)
            {
                let kinds = self.completed_support_kinds.entry(agent).or_default();
                if !kinds.contains(&kind) {
                    kinds.push(kind);
                }
            }
        }
        if applied && (event_type == "facility_outage" || event_type == "facility_recovery") {
            if let Some(facility_id) = as_str(&mapping_get(&payload, "facility_id")?) {
                let events = self.applied_facility_events.entry(facility_id).or_default();
                if !events.contains(&event_id) {
                    events.push(event_id.clone());
                }
            }
        }
        let provenance = event.getattr("provenance")?;
        // isinstance(value, collections.abc.Mapping), as the Python EventLog checks.
        let mapping_type = record.py().import("collections.abc")?.getattr("Mapping")?;
        let items = provenance.call_method0("items")?;
        for item in items.try_iter()? {
            let item = item?;
            let pair = item.downcast::<PyTuple>()?;
            let value = pair.get_item(1)?;
            if value.is_instance(&mapping_type)? {
                let key: String = pair.get_item(0)?.extract()?;
                self.by_provenance.entry(key).or_default().push(index);
            }
        }
        let type_id = self.type_id(&event_type);
        self.entries.push(Entry {
            record: record.clone().unbind(),
            event_type: type_id,
            applied,
            minute,
        });
        self.by_id.insert(event_id, index);
        self.sequences.insert(sequence);
        self.max_sequence = self.max_sequence.max(sequence);
        self.by_minute.entry(minute).or_default().push(index);
        self.by_type.entry(type_id).or_default().push(index);
        Ok(0)
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

    fn find(&self, py: Python<'_>, event_id: &Bound<'_, PyAny>) -> Option<Py<PyAny>> {
        let event_id = event_id.downcast::<PyString>().ok()?.to_str().ok()?;
        self.by_id
            .get(event_id)
            .map(|index| self.entries[*index].record.clone_ref(py))
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

    fn at_minute(&self, py: Python<'_>, minute: i64) -> Vec<Py<PyAny>> {
        match self.by_minute.get(&minute) {
            Some(indices) => self.records_of(py, indices.iter().copied()),
            None => Vec::new(),
        }
    }

    fn event_types(&self) -> Vec<String> {
        self.by_type
            .keys()
            .map(|id| self.type_names[*id as usize].clone())
            .collect()
    }

    fn with_provenance_object(&self, py: Python<'_>, key: &str) -> Vec<Py<PyAny>> {
        match self.by_provenance.get(key) {
            Some(indices) => self.records_of(py, indices.iter().copied()),
            None => Vec::new(),
        }
    }

    fn of_types(&self, py: Python<'_>, event_types: &Bound<'_, PyAny>) -> PyResult<Vec<Py<PyAny>>> {
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
        Ok(self.records_of(py, indices.into_iter()))
    }

    /// The records from start on, in append order.
    #[pyo3(signature = (start=0))]
    fn records_from(&self, py: Python<'_>, start: usize) -> Vec<Py<PyAny>> {
        let start = start.min(self.entries.len());
        self.records_of(py, start..self.entries.len())
    }

    fn last(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.entries.last().map(|entry| entry.record.clone_ref(py))
    }

    fn event_ids<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let mut ids: Vec<(&String, &usize)> = self.by_id.iter().collect();
        ids.sort_unstable_by_key(|(_, index)| **index);
        PyList::new(py, ids.into_iter().map(|(id, _)| id.as_str()))
    }

    fn recent(&self, py: Python<'_>, limit: usize) -> Vec<Py<PyAny>> {
        let start = self.entries.len().saturating_sub(limit);
        self.records_of(py, start..self.entries.len())
    }

    /// Up to limit applied records of event_types from the end, scanning at most
    /// scan_limit records, in append order; and whether records were left unscanned.
    fn recent_matching(
        &self,
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
        Ok((self.records_of(py, found.into_iter()), scanned < self.entries.len()))
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
                    record: entry.record.clone_ref(py),
                    event_type: entry.event_type,
                    applied: entry.applied,
                    minute: entry.minute,
                })
                .collect(),
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
            audited: self.audited,
            audit_bounds: self.audit_bounds,
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
