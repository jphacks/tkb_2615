//! `body_state_v1`: the long-term body and affect records, the relationships and the social
//! history (`domain/body_state.py`). `rules/body_state.rs` and `rules/psychology.rs` write it; the
//! handlers and the population rules that still run in Python write one value or one record at a
//! time through the forwarded writes (docs/rust-port/lanes/m3-body-state.md).
//!
//! A value of another type than the rules write taints the namespace, and the Python rules and
//! their errors run on the tree: an int in a record, which validation accepts, only costs the row.

use std::collections::VecDeque;
use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyList;

use crate::core::row::{load_row, namespace_is_absent, state_row, Field, IdMap};
use crate::core::tracked::as_readable_list;
use crate::core::value::V;
use crate::core::world_core::{mirror_of, Misfit, NamespaceMirror, Part, RowType, Write};
use crate::state::entities::{entities_of, EntityStore};
use crate::{encode, Encoder};

pub(crate) const NAME: &str = "body_state_v1";
pub(crate) const MAX_SOCIAL_HISTORY: usize = 20_000;
pub(crate) const MAX_SOCIAL_RELATIONSHIPS: usize = 100_000;
const POLICY_VERSION: &str = "life-state-feedback-v1";
const SOURCE_ID: &str = "ASM-LIFE-STATE-FEEDBACK-V1";
const MAX_SLEEP_DEBT_MINUTES: f64 = 1440.0;
const MAX_TOPICS: usize = 32;
const MAX_TOPIC_CHARS: usize = 64;

state_row! {
    #[derive(Clone, Copy)]
    pub(crate) struct BodyRecord {
        sleep_debt_minutes: f64 => set_sleep_debt_minutes,
        uncertainty: f64 => set_uncertainty,
        reassurance: f64 => set_reassurance,
        discontent: f64 => set_discontent,
        positive_affect: f64 => set_positive_affect,
        negative_affect: f64 => set_negative_affect,
        hygiene_need: f64 => set_hygiene_need,
    }
}

state_row! {
    #[derive(Clone)]
    pub(crate) struct Relationship {
        contact_count: i64 => set_contact_count,
        trust: Option<f64> => set_trust,
        contact_minutes: i64 => set_contact_minutes,
        last_contact_minute: Option<i64> => set_last_contact_minute,
        last_interaction: Option<Arc<str>> => set_last_interaction,
        topic_ids: Vec<Arc<str>>,
    }
}

/// The relationships, and which were written since `rows_valid` last accepted them, whoever wrote
/// them: a tick writes a few of the thousands, and the rules of a record read only it and its key.
#[derive(Default)]
pub(crate) struct Relationships {
    pub(crate) map: IdMap<Relationship>,
    pub(crate) written: Vec<Arc<str>>,
    read_all: bool,
    /// The people the accepted keys were checked against, in the order of the body records.
    people: Vec<Arc<str>>,
}

impl Field for Relationships {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        Ok(Self { map: Field::from_py(value)?, written: Vec::new(), read_all: true, people: Vec::new() })
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.map.to_py(py)
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        self.map.write_json(encoder)
    }

    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        let key = match (path, write) {
            ([Part::Key(key), ..], _) | ([], Write::Set { key: Part::Key(key), .. }) => Some(*key),
            _ => None,
        };
        self.map.apply(path, write)?;
        if let Some(key) = key {
            self.written.push(Arc::from(key));
        }
        Ok(())
    }
}

/// The social history: the canonical JSON of each item. Nothing reads an item here, only its
/// bytes, and a write into an old item doesn't fit: the namespace is then tainted.
///
/// A deque because at its limit each new item drops the oldest, which in a `Vec` would move all
/// 20,000 at each of the ~13 appends of a tick.
#[derive(Default)]
pub(crate) struct History {
    pub(crate) items: VecDeque<Box<[u8]>>,
}

fn item_bytes(value: &Bound<'_, PyAny>) -> Result<Box<[u8]>, Misfit> {
    Ok(encode(value)?.into_boxed_slice())
}

impl Field for History {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        let list = as_readable_list(value).ok_or_else(|| Misfit::new("not a list"))?;
        let mut items = VecDeque::with_capacity(list.len());
        for item in list.iter() {
            items.push_back(item_bytes(&item)?);
        }
        Ok(Self { items })
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let loads = py.import(intern!(py, "json"))?.getattr(intern!(py, "loads"))?;
        let list = PyList::empty(py);
        for item in &self.items {
            list.append(loads.call1((std::str::from_utf8(item)?,))?)?;
        }
        Ok(list.into_any())
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        encoder.out.push(b'[');
        for (index, item) in self.items.iter().enumerate() {
            if index > 0 {
                encoder.out.push(b',');
            }
            encoder.out.extend_from_slice(item);
        }
        encoder.out.push(b']');
        Ok(())
    }

    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        match (path, write) {
            ([], Write::Append { value }) => self.items.push_back(item_bytes(value)?),
            ([], Write::Extend { values }) => {
                for value in values.iter() {
                    self.items.push_back(item_bytes(&value)?);
                }
            }
            ([], Write::DeleteRange { start, stop }) if start <= stop && *stop <= self.items.len() => {
                self.items.drain(*start..*stop);
            }
            ([], Write::Delete { key: Part::Index(index) }) if *index < self.items.len() => {
                self.items.remove(*index);
            }
            ([], Write::Set { key: Part::Index(index), value }) if *index < self.items.len() => {
                self.items[*index] = item_bytes(value)?;
            }
            ([], Write::Insert { index, value }) if *index <= self.items.len() => {
                self.items.insert(*index, item_bytes(value)?);
            }
            ([], Write::Clear) => self.items.clear(),
            ([], Write::Reverse) => self.items.make_contiguous().reverse(),
            ([], Write::ReplaceAll { items }) => {
                self.items = items.iter().map(|item| item_bytes(&item)).collect::<Result<_, _>>()?;
            }
            _ => return Err(Misfit::new("the write does not fit the history")),
        }
        Ok(())
    }
}

state_row! {
    pub(crate) struct BodyRow {
        schema_version: i64,
        policy_version: Arc<str>,
        source_id: Arc<str>,
        config: V,
        agents: IdMap<BodyRecord>,
        relationships: Relationships,
        history: History,
    }
}

fn in_range(value: f64, upper: f64) -> bool {
    // A NaN fails both, like `not 0.0 <= value <= upper`.
    (0.0..=upper).contains(&value)
}

fn record_is_valid(record: &BodyRecord) -> bool {
    in_range(record.sleep_debt_minutes, MAX_SLEEP_DEBT_MINUTES)
        && in_range(record.uncertainty, 1.0)
        && in_range(record.reassurance, 1.0)
        && in_range(record.discontent, 1.0)
        && in_range(record.positive_affect, 1.0)
        && in_range(record.negative_affect, 1.0)
        && in_range(record.hygiene_need, 1.0)
}

/// The two IDs of a key the Python rule certainly accepts: `["a","b"]`, of IDs that JSON writes as
/// they are. None says nothing about the key, and the Python rule then reads it.
fn pair_of(key: &str) -> Option<(&str, &str)> {
    fn plain(id: &str) -> bool {
        !id.is_empty()
            && id.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
    }
    let (first, second) = key.strip_prefix("[\"")?.strip_suffix("\"]")?.split_once("\",\"")?;
    (plain(first) && plain(second)).then_some((first, second))
}

fn relationship_is_valid(record: &Relationship) -> bool {
    record.contact_count >= 0
        && record.contact_minutes >= 0
        && record.trust.is_none_or(|trust| in_range(trust, 1.0))
        && record.last_contact_minute.is_none_or(|minute| minute >= 0)
        && record.topic_ids.len() <= MAX_TOPICS
        && record.topic_ids.iter().all(|topic| {
            topic.len() <= MAX_TOPIC_CHARS || topic.chars().count() <= MAX_TOPIC_CHARS
        })
}

fn key_is_valid<T>(key: &str, people: &IdMap<T>) -> bool {
    pair_of(key).is_some_and(|(first, second)| {
        first != second && people.contains_key(first) && people.contains_key(second)
    })
}

/// True only if the header, the Agent records and the relationships pass every rule
/// `validate_body_state` has for them. Its checks of the config and the history read what the row
/// doesn't hold, and stay in Python. Anything else, a false or Unsupported, runs the Python rules,
/// so their errors keep their text. What is accepted is not read again until it is written.
#[pyfunction]
fn rows_valid(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    if namespace_is_absent(world, intern!(world.py(), NAME))? {
        return Ok(false);
    }
    let mirror = mirror_of(world, NAME)?;
    let store = entities_of(world)?;
    NamespaceMirror::with_row::<BodyRow, _>(&mirror, |row| {
        let accepted = EntityStore::with_rows(&store, |rows| {
            let people = &rows.agents;
            if row.schema_version != 1
                || &*row.policy_version != POLICY_VERSION
                || &*row.source_id != SOURCE_ID
                || row.agents.len() != people.len()
                || row.relationships.map.len() > MAX_SOCIAL_RELATIONSHIPS
                || row.history.items.len() > MAX_SOCIAL_HISTORY
                || !row.agents.iter().all(|(id, record)| people.get(id).is_some() && record_is_valid(record))
            {
                return Ok(false);
            }
            let relationships = &mut row.relationships;
            let same_people = relationships.people.len() == row.agents.len()
                && relationships.people.iter().zip(row.agents.keys()).all(|(known, id)| known == id);
            if relationships.read_all || !same_people {
                let valid = relationships
                    .map
                    .iter()
                    .all(|(key, record)| key_is_valid(key, &row.agents) && relationship_is_valid(record));
                if !valid {
                    return Ok(false);
                }
                relationships.read_all = false;
                relationships.people = row.agents.keys().cloned().collect();
            } else {
                // A written key that left the map has nothing to check any more.
                let valid = relationships.written.iter().all(|key| {
                    relationships.map.get(key).is_none_or(|record| {
                        key_is_valid(key, &row.agents) && relationship_is_valid(record)
                    })
                });
                if !valid {
                    return Ok(false);
                }
            }
            relationships.written.clear();
            Ok(true)
        })?;
        if !accepted {
            // The Python rules decide now. If they pass it, the next check starts over, and the
            // list of written keys does not grow at each tick of such a world.
            row.relationships.read_all = true;
            row.relationships.written.clear();
        }
        Ok(accepted)
    })
}

/// The canonical JSON of the row's records of these people and these relationships, and of its
/// last history items, for benchmarks/rule_shadow: a whole `verify` per event would be too slow.
#[pyfunction]
fn row_parts<'py>(
    world: &Bound<'py, PyAny>,
    people: Vec<String>,
    relationships: Vec<String>,
    history_tail: usize,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>, Vec<String>, usize)> {
    let py = world.py();
    let mirror = mirror_of(world, NAME)?;
    NamespaceMirror::with_row::<BodyRow, _>(&mirror, |row| {
        let found_people = pyo3::types::PyDict::new(py);
        for id in &people {
            if let Some(record) = row.agents.get(id.as_str()) {
                found_people.set_item(id, Field::to_py(record, py)?)?;
            }
        }
        let found = pyo3::types::PyDict::new(py);
        for key in &relationships {
            if let Some(record) = row.relationships.map.get(key.as_str()) {
                found.set_item(key, Field::to_py(record, py)?)?;
            }
        }
        let count = row.history.items.len();
        let mut tail = Vec::with_capacity(history_tail.min(count));
        for item in row.history.items.iter().skip(count.saturating_sub(history_tail)) {
            tail.push(String::from_utf8_lossy(item).into_owned());
        }
        Ok((found_people.into_any(), found.into_any(), tail, count))
    })
}

/// The record `_record_relationship` starts a pair with.
pub(crate) fn new_relationship() -> Relationship {
    Relationship {
        contact_count: 0,
        trust: Some(0.5),
        contact_minutes: 0,
        last_contact_minute: None,
        last_interaction: None,
        topic_ids: Vec::new(),
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("body_state", load_row::<BodyRow>))?)?;
    m.add_function(wrap_pyfunction!(rows_valid, m)?)?;
    m.add_function(wrap_pyfunction!(row_parts, m)?)?;
    Ok(())
}
