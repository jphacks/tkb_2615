//! `shared_operations_v1`: the receipts of care, staff and facility transitions, and the head of
//! each open care request (`domain/common_operations.py`). `rules/common_operations.rs` appends a
//! care receipt; the other receipts come through the forwarded writes of the Python rule
//! (docs/rust-port/lanes/m4-staff.md).
//!
//! A receipt is its canonical JSON, with what the rules read of it. `verified` says the kernel
//! itself checked it against its event when it was appended, so that a tick's validation needs
//! nothing more of it. A forwarded or restored receipt isn't, and the Python rule checks it while
//! it is among the recent ones.

use std::collections::VecDeque;
use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};

use crate::core::row::{exact_int, load_row, namespace_is_absent, state_row, str_enum, Field, IdMap};
use crate::core::tracked::{as_readable_dict, as_readable_list};
use crate::core::world_core::{mirror_of, Misfit, NamespaceMirror, Part, RowType, Write};
use crate::state::entities::{entities_of, EntityStore, RequestStatus};
use crate::state::events::EventWriter;
use crate::{encode, Encoder};

pub(crate) const NAME: &str = "shared_operations_v1";
pub(crate) const POLICY: &str = "shared-operations-v1";
pub(crate) const MAX_HISTORY: usize = 20_000;
pub(crate) const MAX_REQUESTS: usize = 100_000;
const SUPPORT_KINDS: [&str; 9] = [
    "medical_assessment",
    "medication_support",
    "caregiver_assistance",
    "privacy_accommodation",
    "mobility_assistance",
    "hygiene_support",
    "emotional_support",
    "language_access",
    "dependent_assistance",
];

str_enum! {
    pub(crate) enum CareStatus {
        Requested = "requested",
        Assigned = "assigned",
        Started = "started",
        Completed = "completed",
        Interrupted = "interrupted",
        Declined = "declined",
    }
}

impl CareStatus {
    pub(crate) fn parse(text: &str) -> Option<Self> {
        Some(match text {
            "requested" => Self::Requested,
            "assigned" => Self::Assigned,
            "started" => Self::Started,
            "completed" => Self::Completed,
            "interrupted" => Self::Interrupted,
            "declined" => Self::Declined,
            _ => return None,
        })
    }

    /// Whether a head may hold this status.
    pub(crate) fn is_open(self) -> bool {
        matches!(self, Self::Requested | Self::Assigned | Self::Started)
    }
}

state_row! {
    pub(crate) struct CareHead {
        request_id: Arc<str>,
        agent_id: Arc<str>,
        household_id: Option<Arc<str>>,
        support_kind: Arc<str>,
        status: CareStatus,
        created_minute: i64,
        updated_minute: i64,
        latest_event_id: Arc<str>,
    }
}

/// The contract's `Identifier`: `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`.
pub(crate) fn is_identifier(text: &str) -> bool {
    let bytes = text.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 64
        && bytes[0].is_ascii_alphanumeric()
        && bytes.iter().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

impl CareHead {
    /// What `CareRequestState` and `SharedOperationsState` ask of a head under this key.
    fn is_valid(&self, key: &str) -> bool {
        &*self.request_id == key
            && is_identifier(&self.request_id)
            && is_identifier(&self.agent_id)
            && self.household_id.as_deref().is_none_or(is_identifier)
            && SUPPORT_KINDS.contains(&&*self.support_kind)
            && self.status.is_open()
            && self.created_minute >= 0
            && self.updated_minute >= self.created_minute
            && is_identifier(&self.latest_event_id)
    }
}

/// The heads, and whether a forwarded write reached them since they last passed.
#[derive(Default)]
pub(crate) struct Heads {
    pub(crate) map: IdMap<CareHead>,
    unchecked: bool,
}

impl Heads {
    /// Whether every head is one the contract takes. Reads them only after a forwarded write.
    pub(crate) fn are_valid(&mut self) -> bool {
        if self.unchecked {
            if self.map.len() > MAX_REQUESTS || !self.map.iter().all(|(key, head)| head.is_valid(key)) {
                return false;
            }
            self.unchecked = false;
        }
        true
    }
}

impl Field for Heads {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        Ok(Self { map: Field::from_py(value)?, unchecked: true })
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.map.to_py(py)
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        self.map.write_json(encoder)
    }

    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        self.unchecked = true;
        self.map.apply(path, write)
    }
}

/// One receipt of the history.
pub(crate) struct Receipt {
    pub(crate) minute: Option<i64>,
    pub(crate) event_id: Option<Arc<str>>,
    pub(crate) verified: bool,
    /// Its rule also reads the retained `staff_operations_v1.history`, so a tick's rules read it
    /// again as long as it is recent.
    pub(crate) reads_staff_history: bool,
    pub(crate) bytes: Box<[u8]>,
}

fn receipt_from(value: &Bound<'_, PyAny>) -> Result<Receipt, Misfit> {
    let py = value.py();
    let mut receipt = Receipt {
        minute: None,
        event_id: None,
        verified: false,
        reads_staff_history: true,
        bytes: encode(value)?.into_boxed_slice(),
    };
    if let Some(record) = as_readable_dict(value) {
        if let Some(minute) = record.get_item(intern!(py, "minute"))? {
            receipt.minute = exact_int(&minute).ok();
        }
        if let Some(event_id) = record.get_item(intern!(py, "event_id"))? {
            if let Ok(event_id) = event_id.downcast_exact::<PyString>() {
                receipt.event_id = Some(Arc::from(event_id.to_str()?));
            }
        }
        if let Some(event_type) = record.get_item(intern!(py, "event_type"))? {
            receipt.reads_staff_history = !event_type
                .downcast_exact::<PyString>()
                .is_ok_and(|text| text.to_str().is_ok_and(|text| text != "staff_operation_transition"));
        }
    }
    Ok(receipt)
}

/// A deque because at its limit each new receipt drops the oldest. A write into a receipt doesn't
/// fit, and taints the namespace.
#[derive(Default)]
pub(crate) struct Receipts {
    pub(crate) items: VecDeque<Receipt>,
}

impl Receipts {
    /// Where `records_since_previous_tick` starts.
    /// `validate_shared_operations_state` of this tick passed, reading these.
    fn recent_passed(&mut self, minute_index: i64) {
        let start = self.recent_start(minute_index);
        for item in self.items.iter_mut().skip(start) {
            item.verified = !item.reads_staff_history;
        }
    }

    pub(crate) fn recent_start(&self, minute_index: i64) -> usize {
        let mut start = self.items.len();
        while start > 0 {
            if self.items[start - 1].minute.is_some_and(|minute| minute < minute_index - 1) {
                break;
            }
            start -= 1;
        }
        start.saturating_sub(1)
    }

    /// The receipts of `records_since_previous_tick`, if the kernel checked each and they are in order.
    pub(crate) fn recent_verified(&self, minute_index: i64) -> Option<impl Iterator<Item = &Receipt>> {
        let start = self.recent_start(minute_index);
        let recent = || self.items.iter().skip(start);
        let mut last = None;
        for item in recent() {
            let (true, Some(minute), Some(_)) = (item.verified, item.minute, &item.event_id) else {
                return None;
            };
            if last.is_some_and(|last| last > minute) || minute > minute_index {
                return None;
            }
            last = Some(minute);
        }
        Some(recent())
    }
}

impl Field for Receipts {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        let list = as_readable_list(value).ok_or_else(|| Misfit::new("not a list"))?;
        let mut items = VecDeque::with_capacity(list.len());
        for item in list.iter() {
            items.push_back(receipt_from(&item)?);
        }
        Ok(Self { items })
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let loads = py.import(intern!(py, "json"))?.getattr(intern!(py, "loads"))?;
        let list = PyList::empty(py);
        for item in &self.items {
            list.append(loads.call1((std::str::from_utf8(&item.bytes)?,))?)?;
        }
        Ok(list.into_any())
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        encoder.out.push(b'[');
        for (index, item) in self.items.iter().enumerate() {
            if index > 0 {
                encoder.out.push(b',');
            }
            encoder.out.extend_from_slice(&item.bytes);
        }
        encoder.out.push(b']');
        Ok(())
    }

    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        match (path, write) {
            ([], Write::Append { value }) => self.items.push_back(receipt_from(value)?),
            ([], Write::Extend { values }) => {
                for value in values.iter() {
                    self.items.push_back(receipt_from(&value)?);
                }
            }
            ([], Write::DeleteRange { start, stop }) if start <= stop && *stop <= self.items.len() => {
                self.items.drain(*start..*stop);
            }
            ([], Write::Delete { key: Part::Index(index) }) if *index < self.items.len() => {
                self.items.remove(*index);
            }
            ([], Write::Set { key: Part::Index(index), value }) if *index < self.items.len() => {
                self.items[*index] = receipt_from(value)?;
            }
            ([], Write::Insert { index, value }) if *index <= self.items.len() => {
                self.items.insert(*index, receipt_from(value)?);
            }
            ([], Write::Clear) => self.items.clear(),
            ([], Write::Reverse) => self.items.make_contiguous().reverse(),
            ([], Write::ReplaceAll { items }) => {
                self.items = items.iter().map(|item| receipt_from(&item)).collect::<Result<_, _>>()?;
            }
            _ => return Err(Misfit::new("the write does not fit the history")),
        }
        Ok(())
    }
}

state_row! {
    pub(crate) struct SharedOperationsRow {
        schema_version: i64,
        policy_version: Arc<str>,
        support_requests: Heads,
        history: Receipts,
        history_truncated: bool,
    }
}

impl SharedOperationsRow {
    /// The part of `SharedOperationsState` that doesn't read the history.
    pub(crate) fn header_is_valid(&mut self) -> bool {
        self.schema_version == 1 && &*self.policy_version == POLICY && self.support_requests.are_valid()
    }
}

/// `validate_shared_operations_state` of a tick (no replay of the history): accepts, or leaves it
/// to the Python rules and their error text.
#[pyfunction]
fn validate_tick(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(true);
    }
    let minute = exact_int(&world.getattr(intern!(py, "minute_index"))?)?;
    let writer = EventWriter::of(world)?;
    let store = entities_of(world)?;
    let mirror = mirror_of(world, NAME)?;
    // (the request, the event that says who has it) of each head with a staff member.
    let mut assigned = Vec::new();
    let accepted = NamespaceMirror::with_row::<SharedOperationsRow, _>(&mirror, |row| {
        if !row.header_is_valid() || row.history.items.len() > MAX_HISTORY {
            return Ok(false);
        }
        let Some(recent) = row.history.recent_verified(minute) else {
            return Ok(false);
        };
        let log = writer.log().borrow();
        let mut seen: Vec<&str> = Vec::new();
        for item in recent {
            let Some(event_id) = item.event_id.as_deref() else {
                return Ok(false);
            };
            if seen.contains(&event_id) || !log.has_event_id(event_id) {
                return Ok(false);
            }
            seen.push(event_id);
        }
        EntityStore::with_rows(&store, |rows| {
            for (id, head) in &row.support_requests.map {
                let Some(request) = rows.support_requests.get(id) else {
                    return Ok(false);
                };
                let expected = if head.status == CareStatus::Requested {
                    RequestStatus::Pending
                } else {
                    RequestStatus::Assigned
                };
                if request.agent_id != head.agent_id || request.status != expected {
                    return Ok(false);
                }
                if head.status != CareStatus::Requested {
                    assigned.push((request.assigned_staff_id.clone(), head.latest_event_id.clone()));
                }
            }
            Ok(true)
        })
    })?;
    if !accepted {
        return Ok(false);
    }
    if !assigned.is_empty() {
        let log = world.getattr(intern!(py, "event_log"))?;
        let (find, event_name, payload_name, staff_name) =
            (intern!(py, "find"), intern!(py, "event"), intern!(py, "payload"), intern!(py, "staff_id"));
        for (staff, event_id) in assigned {
            let record = log.call_method1(find, (&*event_id,))?;
            let evidence = if record.is_none() {
                None
            } else {
                let payload = record.getattr(event_name)?.getattr(payload_name)?;
                let value = payload.get_item(staff_name).ok();
                match value {
                    Some(value) if !value.is_none() => Some(value),
                    _ => None,
                }
            };
            let same = match (&evidence, &staff) {
                (None, None) => true,
                (Some(value), Some(staff)) => {
                    value.downcast_exact::<PyString>().is_ok_and(|text| text.to_str().is_ok_and(|text| text == &**staff))
                }
                _ => false,
            };
            if !same {
                return Ok(false);
            }
        }
    }
    Ok(true)
}

/// The Python rules have just accepted this world's state at its minute, for a tick.
#[pyfunction]
fn recent_passed(world: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(());
    }
    let minute = exact_int(&world.getattr(intern!(py, "minute_index"))?)?;
    NamespaceMirror::with_row::<SharedOperationsRow, _>(&mirror_of(world, NAME)?, |row| {
        row.history.recent_passed(minute);
        Ok(())
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("shared_operations", load_row::<SharedOperationsRow>))?)?;
    m.add_function(wrap_pyfunction!(validate_tick, m)?)?;
    m.add_function(wrap_pyfunction!(recent_passed, m)?)?;
    Ok(())
}
