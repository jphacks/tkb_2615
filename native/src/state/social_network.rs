//! `social_network_v1`: who has met whom (`domain/social_network.py`). Its rules are
//! `rules/social_network.rs`; `domain/population.py` and `domain/population_lifecycle.py` take a
//! departing person's records out and put them back through the forwarded writes.
//!
//! A value of another type than the rules write taints the namespace, and the Python rules and
//! their errors run on the tree: an int `trust` or a message ID that is not a str, which validation
//! accepts, only costs the row.
//!
//! The row knows which records were written since the last check, so the check of each tick reads
//! those and not the thousands of others: a forwarded write and a rule's write are the only ways
//! into the row, and both say which record they touch. This is what the dict watcher of `watch.rs`
//! and `social_relationships_valid` did on the tree (docs/rust-port/lanes/m3-social-network.md).

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;

use crate::core::row::{load_row, namespace_is_absent, state_row, Field, IdMap};
use crate::core::world_core::{mirror_of, Misfit, NamespaceMirror, Part, RowType, Write};
use crate::Encoder;

pub(crate) const NAME: &str = "social_network_v1";
const SCHEMA_VERSION: i64 = 1;
const POLICY_VERSION: &str = "directed-contact-relationships-v1";
/// `MAX_RELATIONSHIPS` of `domain/social_network.py`.
pub(crate) const MAX_RELATIONSHIPS: usize = 100_000;
/// Past this many unchecked writes, the next check reads every record instead of a long list.
const MAX_UNCHECKED_KEYS: usize = 4096;

state_row! {
    pub(crate) struct Relationship {
        contact_count: i64 => set_contact_count,
        conversation_count: i64 => set_conversation_count,
        cooperation_count: i64 => set_cooperation_count,
        trust: Option<f64> => set_trust,
        shared_message_ids: Vec<Arc<str>>,
        last_contact_minute: Option<i64> => set_last_contact_minute,
        last_interaction: Option<Arc<str>> => set_last_interaction,
    }
}

impl Relationship {
    pub(crate) fn empty() -> Self {
        Self {
            contact_count: 0,
            conversation_count: 0,
            cooperation_count: 0,
            trust: None,
            shared_message_ids: Vec::new(),
            last_contact_minute: None,
            last_interaction: None,
        }
    }

    /// The rules of `validate_social_network_state` that read the record's values. NaN fails.
    fn is_valid(&self) -> bool {
        self.contact_count >= 0
            && self.conversation_count >= 0
            && self.cooperation_count >= 0
            && self.trust.is_none_or(|trust| (0.0..=1.0).contains(&trust))
            && self.last_contact_minute.is_none_or(|minute| minute >= 0)
    }
}

/// A char of an ID that `json.dumps` writes as it is: ASCII, printable, not `"` nor `\`.
pub(crate) fn is_plain_id(id: &str) -> bool {
    id.bytes().all(|byte| (0x20..0x7f).contains(&byte) && byte != b'"' && byte != b'\\')
}

/// `_pair_key_problem` found nothing wrong: the compact JSON of two different strings. A key with
/// anything `json.dumps` escapes says false here, and the Python rule then decides.
fn pair_key_is_good(key: &str) -> bool {
    let Some(inner) = key.strip_prefix("[\"").and_then(|rest| rest.strip_suffix("\"]")) else {
        return false;
    };
    let Some((first, second)) = inner.split_once("\",\"") else {
        return false;
    };
    first != second && is_plain_id(first) && is_plain_id(second)
}

/// The records and which of them no check has read yet.
pub(crate) struct Relationships {
    pub(crate) map: IdMap<Relationship>,
    unchecked_keys: Vec<Arc<str>>,
    all_unchecked: bool,
}

impl Relationships {
    pub(crate) fn note_write(&mut self, key: &str) {
        if self.all_unchecked {
            return;
        }
        if self.unchecked_keys.len() >= MAX_UNCHECKED_KEYS {
            self.all_unchecked = true;
            self.unchecked_keys = Vec::new();
            return;
        }
        // A rule's record has its key in the map: share that text.
        match self.map.get_key_value(key) {
            Some((shared, _)) => self.unchecked_keys.push(shared.clone()),
            None => self.unchecked_keys.push(Arc::from(key)),
        }
    }

    /// Whether the unchecked records pass. On false they stay unchecked, so the next check says
    /// false again until the bad value is gone.
    fn check(&mut self) -> bool {
        let good = |key: &str, record: &Relationship| pair_key_is_good(key) && record.is_valid();
        if self.all_unchecked {
            if !self.map.iter().all(|(key, record)| good(key, record)) {
                return false;
            }
        } else if !self.unchecked_keys.iter().all(|key| {
            // A record that has left the map since its write has nothing to check.
            self.map.get(&**key).is_none_or(|record| good(key, record))
        }) {
            return false;
        }
        self.all_unchecked = false;
        self.unchecked_keys.clear();
        true
    }
}

impl Field for Relationships {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        Ok(Self { map: Field::from_py(value)?, unchecked_keys: Vec::new(), all_unchecked: true })
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Field::to_py(&self.map, py)
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        Field::write_json(&self.map, encoder)
    }

    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        match (path, write) {
            ([], Write::Set { key: Part::Key(key), .. }) | ([Part::Key(key), ..], _) => {
                let key = Arc::<str>::from(*key);
                Field::apply(&mut self.map, path, write)?;
                self.note_write(&key);
                Ok(())
            }
            _ => Field::apply(&mut self.map, path, write),
        }
    }
}

state_row! {
    pub(crate) struct SocialNetworkRow {
        schema_version: i64,
        policy_version: Arc<str>,
        relationships: Relationships,
        interaction_count: i64 => set_interaction_count,
        information_transfer_count: i64 => set_information_transfer_count,
    }
}

impl SocialNetworkRow {
    fn is_valid(&mut self) -> bool {
        self.schema_version == SCHEMA_VERSION
            && &*self.policy_version == POLICY_VERSION
            && self.relationships.map.len() <= MAX_RELATIONSHIPS
            && self.relationships.check()
    }
}

/// The namespace's validator: accepts, or leaves it to the Python rules and their error text.
#[pyfunction]
fn validate(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    if namespace_is_absent(world, intern!(world.py(), NAME))? {
        return Ok(true);
    }
    let mirror = mirror_of(world, NAME)?;
    NamespaceMirror::with_row::<SocialNetworkRow, _>(&mirror, |row| Ok(row.is_valid()))
}

/// For the comparison with `_pair_key_problem` in the tests.
#[pyfunction]
fn key_is_good(key: &str) -> bool {
    pair_key_is_good(key)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("social_network", load_row::<SocialNetworkRow>))?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    m.add_function(wrap_pyfunction!(key_is_good, m)?)?;
    Ok(())
}
