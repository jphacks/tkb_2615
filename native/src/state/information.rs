//! `information_spread_v1`: claims, each person's belief in them, the contacts of the last tick,
//! the signal that makes people ask, and the requests it raised. The rule that writes it each tick
//! is `rules/information.rs`; `policies/information.py` stays the comparison base. Rumor events,
//! corrections and arrivals still write single items from Python, which reach the row as forwarded
//! writes.

use std::collections::HashSet;
use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyString;

use crate::contacts::exact_int;
use crate::core::row::{load_row, state_row, IdMap};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::{mirror_of, raw_dict_get, NamespaceMirror, RowType};
use crate::state::entities::{entities_of, EntityStore};

pub(crate) const NAME: &str = "information_spread_v1";
pub(crate) const SCHEMA_VERSION: i64 = 2;
const MAX_MESSAGES: usize = 1024;
const MAX_CONTACTS_PER_AGENT: usize = 16;

state_row! {
    pub(crate) struct MessageRow {
        id: Arc<str>,
        truth_status: Arc<str>,
        source_id: Arc<str>,
        zone_id: Arc<str>,
        time: i64,
        origin_event_id: Arc<str>,
        origin_agent_id: Arc<str>,
    }
}

state_row! {
    pub(crate) struct BeliefRow {
        message_id: Arc<str>,
        belief: Arc<str> => set_belief,
        source: Arc<str>,
        last_contact: i64,
        last_event_id: Arc<str>,
        exposed: bool,
        belief_strength: f64,
        share_propensity: f64 => set_share_propensity,
        correction_exposed: bool,
        correction_accepted: bool,
        ever_misbelieved: bool,
        ever_forgotten: bool => set_ever_forgotten,
        last_correction_accepted: bool,
    }
}

state_row! {
    #[derive(Clone, Copy)]
    pub(crate) struct ContactRow {
        last_seen_minute: i64,
        consecutive_minutes: i64,
    }
}

state_row! {
    pub(crate) struct SupportRequestRow {
        request_id: Arc<str>,
        agent_id: Arc<str>,
        minute: i64,
        message_ids: Vec<Arc<str>>,
    }
}

state_row! {
    pub(crate) struct CountersRow {
        contact_attempts: i64 => set_contact_attempts,
        contact_deliveries: i64,
        correction_deliveries: i64,
        support_requests: i64,
    }
}

state_row! {
    pub(crate) struct InformationRow {
        schema_version: i64,
        messages: IdMap<MessageRow>,
        beliefs: IdMap<IdMap<BeliefRow>>,
        contacts: IdMap<ContactRow>,
        query_pressure: IdMap<f64>,
        support_requests: Vec<SupportRequestRow>,
        counters: CountersRow,
    }
}

/// `_validate_information_id`: `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`, matched in full.
pub(crate) fn is_stable_id(text: &str) -> bool {
    let bytes = text.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 64
        && bytes[0].is_ascii_alphanumeric()
        && bytes.iter().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn is_unit(value: f64) -> bool {
    (0.0..=1.0).contains(&value)
}

/// What a belief's status counts for: `_BELIEF_RANK`, or None for no status.
pub(crate) fn belief_rank(status: &str) -> Option<u8> {
    match status {
        "unknown" => Some(0),
        "heard" => Some(1),
        "verified" | "rejected" => Some(2),
        _ => None,
    }
}

impl MessageRow {
    /// `TypedMessage.from_dict` takes this record of the row's exact types.
    pub(crate) fn parses(&self) -> bool {
        is_stable_id(&self.id)
            && is_stable_id(&self.source_id)
            && is_stable_id(&self.zone_id)
            && is_stable_id(&self.origin_event_id)
            && is_stable_id(&self.origin_agent_id)
            && matches!(&*self.truth_status, "unknown" | "true" | "false")
            && self.time >= 0
    }
}

impl BeliefRow {
    /// `AgentInformationState.from_dict` takes this record of the row's exact types.
    pub(crate) fn parses(&self) -> bool {
        is_stable_id(&self.message_id)
            && is_stable_id(&self.source)
            && is_stable_id(&self.last_event_id)
            && belief_rank(&self.belief).is_some()
            && self.last_contact >= 0
            && (!self.correction_accepted || self.correction_exposed)
            && (!self.last_correction_accepted || self.correction_exposed)
            && is_unit(self.belief_strength)
            && is_unit(self.share_propensity)
            && self.exposed
            && (&*self.belief != "unknown" || self.share_propensity == 0.0)
    }
}

impl InformationRow {
    /// Everything the Python rule parses each tick parses, and each belief's claim exists: then
    /// the rule can't raise on the messages or the beliefs. False says nothing about the state.
    pub(crate) fn beliefs_parse(&self) -> bool {
        self.messages.iter().all(|(id, message)| **id == *message.id && message.parses())
            && self.beliefs.values().all(|records| {
                records.iter().all(|(id, record)| {
                    **id == *record.message_id && self.messages.contains_key(&**id) && record.parses()
                })
            })
    }

    fn has_good_contacts(&self, agents: &HashSet<&str>, duration: i64) -> bool {
        self.contacts.len() <= agents.len().saturating_mul(MAX_CONTACTS_PER_AGENT)
            && self.contacts.iter().all(|(key, record)| {
                let Some((first, second)) = pair_of_key(key) else {
                    return false;
                };
                first != second
                    && agents.contains(first)
                    && agents.contains(second)
                    && (0..duration).contains(&record.last_seen_minute)
                    && record.consecutive_minutes >= 1
                    && record.consecutive_minutes <= record.last_seen_minute.saturating_add(1)
            })
    }

    fn has_good_requests(&self, agents: &HashSet<&str>, duration: i64) -> bool {
        let mut seen: HashSet<&str> = HashSet::with_capacity(self.support_requests.len());
        self.support_requests.iter().all(|request| {
            let mut messages: HashSet<&str> = HashSet::with_capacity(request.message_ids.len());
            !request.request_id.is_empty()
                && seen.insert(&request.request_id)
                && agents.contains(&*request.agent_id)
                && (0..duration).contains(&request.minute)
                && !request.message_ids.is_empty()
                && request.message_ids.iter().all(|message| {
                    messages.insert(message) && self.messages.contains_key(&**message)
                })
        })
    }

    /// Whether validate_information_runtime_state passes the namespace. False says nothing about
    /// it: the Python rules then say what is wrong. A state of schema 1 goes to them too.
    fn is_valid(&self, agents: &HashSet<&str>, duration: i64, has_zone: impl Fn(&str) -> PyResult<bool>) -> PyResult<bool> {
        if self.schema_version != SCHEMA_VERSION
            || self.messages.len() > MAX_MESSAGES
            || self.beliefs.len() != agents.len()
            || self.query_pressure.len() != agents.len()
            || !self.beliefs_parse()
        {
            return Ok(false);
        }
        for message in self.messages.values() {
            if message.time >= duration || !agents.contains(&*message.origin_agent_id) || !has_zone(&message.zone_id)? {
                return Ok(false);
            }
        }
        let counters = &self.counters;
        Ok(self.beliefs.iter().all(|(id, records)| {
            agents.contains(&**id) && records.values().all(|record| record.last_contact < duration)
        }) && self.query_pressure.iter().all(|(id, value)| agents.contains(&**id) && is_unit(*value))
            && self.has_good_contacts(agents, duration)
            && self.has_good_requests(agents, duration)
            && counters.contact_attempts >= 0
            && counters.contact_deliveries >= 0
            && counters.correction_deliveries >= 0
            && counters.support_requests >= 0
            && usize::try_from(counters.support_requests) == Ok(self.support_requests.len()))
    }
}

/// The two IDs of a key that `json.dumps(pair, separators=(",", ":"))` writes unchanged: printable
/// ASCII with nothing to escape. Any other key is left to the Python rules.
fn pair_of_key(key: &str) -> Option<(&str, &str)> {
    let inner = key.strip_prefix("[\"")?.strip_suffix("\"]")?;
    let (first, second) = inner.split_once("\",\"")?;
    let plain = |text: &str| {
        text.bytes().all(|byte| (0x20..0x7f).contains(&byte) && byte != b'"' && byte != b'\\')
    };
    (plain(first) && plain(second)).then_some((first, second))
}

/// The namespace's validator: accepts, or leaves it to the Python rules and their error text.
#[pyfunction]
fn validate(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    let duration = exact_int(&world.getattr(intern!(py, "duration_minutes"))?)?;
    let zones = world.getattr(intern!(py, "zones"))?;
    if as_readable_dict(&zones).is_none() {
        return Ok(false);
    }
    let mirror = mirror_of(world, NAME)?;
    EntityStore::with_rows(&entities_of(world)?, |rows| {
        let agents: HashSet<&str> = rows.agents.iter().map(|(id, _)| &**id).collect();
        NamespaceMirror::with_row::<InformationRow, _>(&mirror, |row| {
            row.is_valid(&agents, duration, |zone| {
                Ok(raw_dict_get(&zones, PyString::new(py, zone).as_any())?.is_some())
            })
        })
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("information", load_row::<InformationRow>))?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    Ok(())
}
