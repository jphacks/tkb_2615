//! `domain/agent_dynamics.py`: decision memory and facility awareness. The Python rules stay as the
//! comparison base and for every value these kernels do not decide, which is also how a bad value
//! gets its error text (docs/rust-port/lanes/f-action-lifecycle.md).
//!
//! Each kernel reads the rows and what it was given, computes, and writes the row and its tree. No
//! Python runs in between. `remember_decision` is what a Rust caller uses (lane J).

use std::collections::BTreeSet;
use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyString};

use crate::core::row::{
    attribute, exact_dict, exact_int, exact_list, exact_str, namespace_is_absent, unsupported, Field, IdMap,
};
use crate::core::world_core::{mirror_of, NamespaceMirror};
use crate::state::agent_dynamics::{
    canonical_policy, policy_of, AgentDynamicsRow, AgentMemory, NAME, ORIENTATION_SOURCE, POLICY_V1, POLICY_V3,
};
use crate::state::entities::{entities_of, AgentRow, Entities, EntityStore};
use crate::state::events::is_stable_id;
use crate::Unsupported;

/// `ORIENTATION_FACILITY_KINDS`.
const ORIENTATION_KINDS: [&str; 6] =
    ["intake", "meal_distribution", "medical_support", "quiet_space", "toilet", "water_station"];
const AWARENESS_SOURCE: &str = "ASM-FACILITY-ENTRY-ORIENTATION-V1";
const MAX_INITIAL_IDS: usize = 1000;

/// What `record_proposal` stores of an applied decision.
pub(crate) struct Decision<'a> {
    pub(crate) agent_id: &'a str,
    pub(crate) action: &'a str,
    pub(crate) target_id: Option<&'a str>,
    pub(crate) minute: i64,
    pub(crate) next_decision_at: i64,
    pub(crate) trigger: Option<&'a str>,
}

/// `record_proposal`, with the namespace present: Unsupported without it, for the Python rule to
/// create it. For a Rust caller after `action_lifecycle::commit_action`, as `_apply_proposal` does.
pub(crate) fn remember_decision(world: &Bound<'_, PyAny>, decision: &Decision<'_>) -> PyResult<()> {
    let py = world.py();
    let mirror = mirror_of(world, NAME)?;
    NamespaceMirror::with_row_tree::<AgentDynamicsRow, _>(&mirror, |row, tree| {
        let Some(record) = row.agents.get_mut(decision.agent_id) else {
            return Ok(());
        };
        // Before any write: a row that has a record its tree lacks was written past the tree.
        let node = tree
            .child(intern!(py, "agents"))
            .and_then(|agents| agents.child_str(decision.agent_id))
            .map_err(|_| Unsupported::new_err("the tree does not have the row's record"))?;
        record.set_last_action(&node, Some(Arc::from(decision.action)))?;
        record.set_last_target_id(&node, decision.target_id.map(Arc::from))?;
        record.set_last_decision_minute(&node, Some(decision.minute))?;
        record.set_decision_due_minute(&node, decision.next_decision_at)?;
        record.set_decision_reason(&node, decision.trigger.map(Arc::from))
    })
}

/// A None or an exact str.
fn optional_str<'a>(value: &'a Bound<'_, PyAny>) -> PyResult<Option<&'a str>> {
    if value.is_none() {
        Ok(None)
    } else {
        exact_str(value).map(Some)
    }
}

/// `record_proposal`.
#[pyfunction]
#[pyo3(signature = (world, proposal, *, target_id))]
fn record_proposal(
    world: &Bound<'_, PyAny>,
    proposal: &Bound<'_, PyAny>,
    target_id: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let agent_id = attribute(proposal, intern!(py, "agent_id"))?;
    let action = attribute(&attribute(proposal, intern!(py, "action"))?, intern!(py, "value"))?;
    let trigger = attribute(proposal, intern!(py, "trigger"))?;
    remember_decision(
        world,
        &Decision {
            agent_id: exact_str(&agent_id)?,
            action: exact_str(&action)?,
            target_id: optional_str(target_id)?,
            minute: exact_int(&attribute(proposal, intern!(py, "minute_index"))?)?,
            next_decision_at: exact_int(&attribute(proposal, intern!(py, "next_decision_at"))?)?,
            trigger: optional_str(&trigger)?,
        },
    )
}

/// `agent_knows_facility`.
#[pyfunction]
pub(crate) fn agent_knows_facility(
    world: &Bound<'_, PyAny>,
    agent_id: &Bound<'_, PyAny>,
    facility_id: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let configs = exact_dict(&configs)?;
    if !configs.contains(intern!(py, "facility_awareness"))? && !configs.contains(intern!(py, "agent_dynamics"))? {
        return Ok(true);
    }
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(false);
    }
    let (agent_id, facility_id) = (exact_str(agent_id)?, exact_str(facility_id)?);
    let mirror = mirror_of(world, NAME)?;
    NamespaceMirror::with_row::<AgentDynamicsRow, _>(&mirror, |row| {
        Ok(row
            .agents
            .get(agent_id)
            .is_some_and(|record| record.known_facility_ids.iter().any(|id| &**id == facility_id)))
    })
}

// ---- What a new agent knows ---------------------------------------------------------------------

/// `FacilityAwarenessConfig` of a plain document; Unsupported for what the model refuses or coerces.
struct Awareness {
    initial: Vec<Arc<str>>,
    discover: bool,
}

/// `model_validate` of `model_configs["facility_awareness"]`, or None when that is not a dict.
fn awareness_of(world: &Bound<'_, PyAny>) -> PyResult<Option<Awareness>> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let Some(document) = exact_dict(&configs)?.get_item(intern!(py, "facility_awareness"))? else {
        return Ok(None);
    };
    if document.is_none() {
        return Ok(None);
    }
    let document = exact_dict(&document)?;
    let mut awareness = Awareness { initial: Vec::new(), discover: true };
    for (key, value) in document.iter() {
        match exact_str(&key)? {
            "schema_version" => {
                if exact_int(&value)? != 1 {
                    return unsupported("another awareness schema");
                }
            }
            "initial_known_facility_ids" => {
                let ids = exact_list(&value)?;
                if ids.len() > MAX_INITIAL_IDS {
                    return unsupported("too many initial facilities");
                }
                for id in ids.iter() {
                    let id = exact_str(&id)?;
                    if !is_stable_id(id) {
                        return unsupported("an initial facility is not a stable ID");
                    }
                    awareness.initial.push(Arc::from(id));
                }
                awareness.initial.sort_unstable();
                if awareness.initial.windows(2).any(|pair| pair[0] == pair[1]) {
                    return unsupported("an initial facility repeats");
                }
            }
            "discover_co_located_facilities_on_arrival" => match value.downcast_exact::<PyBool>() {
                Ok(flag) => awareness.discover = flag.is_true(),
                Err(_) => return unsupported("discovery is not a bool"),
            },
            "source_type" => {
                if exact_str(&value)? != "assumption" {
                    return unsupported("another awareness source type");
                }
            }
            "source_id" => {
                if exact_str(&value)? != AWARENESS_SOURCE {
                    return unsupported("another awareness source");
                }
            }
            _ => return unsupported("the awareness config has other fields"),
        }
    }
    Ok(Some(awareness))
}

/// `_new_agent_dynamics_record`.
fn new_memory(rows: &Entities, agent: &AgentRow, oriented: bool, awareness: Option<&Awareness>) -> AgentMemory {
    let mut known: BTreeSet<Arc<str>> = rows
        .facilities
        .iter()
        .filter(|(_, facility)| {
            facility.zone_id == agent.zone_id || (oriented && ORIENTATION_KINDS.contains(&&*facility.kind))
        })
        .map(|(id, _)| id.clone())
        .collect();
    if let Some(awareness) = awareness {
        known.extend(awareness.initial.iter().cloned());
    }
    AgentMemory {
        known_facility_ids: known.into_iter().collect(),
        last_action: None,
        last_target_id: None,
        last_decision_minute: None,
        decision_due_minute: agent.next_decision_at,
        decision_reason: None,
    }
}

/// `create_agent_dynamics_state`.
#[pyfunction]
fn create_state<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let policy = policy_of(world)?;
    let awareness = awareness_of(world)?;
    let store = entities_of(world)?;
    let row = EntityStore::with_rows(&store, |rows| {
        let mut ids: Vec<Arc<str>> = rows.agents.iter().map(|(id, _)| id.clone()).collect();
        ids.sort_unstable();
        let mut agents = IdMap::default();
        for id in ids {
            let Some(agent) = rows.agents.get(&id) else {
                return unsupported("an agent has no row");
            };
            agents.insert(id, new_memory(rows, agent, policy != POLICY_V1, awareness.as_ref()));
        }
        Ok(AgentDynamicsRow { schema_version: 1, policy_version: Arc::from(policy), agents })
    })?;
    Field::to_py(&row, world.py())
}

/// What `add_agent` refuses for a person about to be added, for a plan that adds one.
pub(crate) fn check_add_agent(world: &Bound<'_, PyAny>) -> PyResult<()> {
    policy_of(world)?;
    awareness_of(world)?;
    let mirror = mirror_of(world, NAME)?;
    NamespaceMirror::with_row::<AgentDynamicsRow, _>(&mirror, |_| Ok(()))
}

/// `add_agent`: one record, forwarded neither way.
#[pyfunction]
pub(crate) fn add_agent(world: &Bound<'_, PyAny>, agent_id: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(());
    }
    let id = exact_str(agent_id)?;
    let store = entities_of(world)?;
    // Python asks these only once the agent is there, so a bad config with no agent is no error.
    let known_agent = EntityStore::with_rows(&store, |rows| Ok(rows.agents.get(id).is_some()))?;
    if !known_agent {
        return Ok(());
    }
    let policy = policy_of(world)?;
    let awareness = awareness_of(world)?;
    let mirror = mirror_of(world, NAME)?;
    let record = EntityStore::with_rows(&store, |rows| {
        let Some(agent) = rows.agents.get(id) else {
            return unsupported("an agent has no row");
        };
        Ok(new_memory(rows, agent, policy != POLICY_V1, awareness.as_ref()))
    })?;
    let value = Field::to_py(&record, py)?;
    NamespaceMirror::write_through(&mirror, &["agents"], id, &value)?;
    NamespaceMirror::with_row::<AgentDynamicsRow, _>(&mirror, |row| {
        match row.agents.get_mut(id) {
            Some(slot) => *slot = record,
            None => {
                row.agents.insert(Arc::from(id), record);
            }
        }
        Ok(())
    })
}

// ---- What an agent learns -------------------------------------------------------------------------

/// Store one record's new list, in the tree and then in the row: a tree that lacks the record, written
/// past its containers, says Unsupported with the row as it was. `known` is new, so nothing else holds
/// it.
fn write_known(
    mirror: &Bound<'_, NamespaceMirror>,
    agent_id: &str,
    known: Vec<Arc<str>>,
) -> PyResult<()> {
    let py = mirror.py();
    let value = PyList::new(py, known.iter().map(|id| &**id))?;
    NamespaceMirror::write_through(mirror, &["agents", agent_id], "known_facility_ids", value.as_any())?;
    NamespaceMirror::with_row::<AgentDynamicsRow, _>(mirror, |row| {
        match row.agents.get_mut(agent_id) {
            Some(record) => record.known_facility_ids = known,
            None => return unsupported("the row changed under the plan"),
        }
        Ok(())
    })
}

/// `learn_facility`. A list that would come out the same is not written.
#[pyfunction]
fn learn_facility(
    world: &Bound<'_, PyAny>,
    agent_id: &Bound<'_, PyAny>,
    facility_id: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(());
    }
    learn_facility_of(world, exact_str(agent_id)?, exact_str(facility_id)?)
}

/// The rule, for IDs a row holds.
pub(crate) fn learn_facility_of(world: &Bound<'_, PyAny>, agent_id: &str, facility_id: &str) -> PyResult<()> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(());
    }
    let store = entities_of(world)?;
    let zone = EntityStore::with_rows(&store, |rows| {
        Ok(rows.facilities.get(facility_id).map(|facility| facility.zone_id.clone()))
    })?;
    let Some(zone) = zone else {
        return Ok(());
    };
    let mirror = mirror_of(world, NAME)?;
    let has_record =
        NamespaceMirror::with_row::<AgentDynamicsRow, _>(&mirror, |row| Ok(row.agents.get(agent_id).is_some()))?;
    if !has_record {
        return Ok(());
    }
    // Asked after the record, as the Python rule does, so a bad config with no record is no error.
    let discover = awareness_of(world)?.is_some_and(|awareness| awareness.discover);
    let mut discovered: Vec<Arc<str>> = vec![Arc::from(facility_id)];
    if discover {
        EntityStore::with_rows(&store, |rows| {
            discovered.extend(
                rows.facilities
                    .iter()
                    .filter(|(_, facility)| facility.zone_id == zone)
                    .map(|(id, _)| id.clone()),
            );
            Ok(())
        })?;
    }
    let next = NamespaceMirror::with_row::<AgentDynamicsRow, _>(&mirror, |row| {
        let Some(record) = row.agents.get(agent_id) else {
            return unsupported("the row changed under the plan");
        };
        let mut next: Vec<Arc<str>> =
            record.known_facility_ids.iter().chain(&discovered).cloned().collect();
        next.sort_unstable();
        next.dedup();
        Ok((next != record.known_facility_ids).then_some(next))
    })?;
    match next {
        Some(next) => write_known(&mirror, agent_id, next),
        None => Ok(()),
    }
}

/// `announce_installed_facility`.
#[pyfunction]
fn announce_installed_facility(world: &Bound<'_, PyAny>, facility_id: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(());
    }
    let facility_id = exact_str(facility_id)?;
    let store = entities_of(world)?;
    let shared = EntityStore::with_rows(&store, |rows| {
        Ok(rows.facilities.get(facility_id).is_some_and(|facility| ORIENTATION_KINDS.contains(&&*facility.kind)))
    })?;
    if !shared || policy_of(world)? != POLICY_V3 {
        return Ok(());
    }
    let mirror = mirror_of(world, NAME)?;
    let announced: Arc<str> = Arc::from(facility_id);
    let changes = NamespaceMirror::with_row::<AgentDynamicsRow, _>(&mirror, |row| {
        Ok(row
            .agents
            .iter()
            .filter(|(_, record)| !record.known_facility_ids.contains(&announced))
            .map(|(id, record)| {
                let mut next = record.known_facility_ids.clone();
                next.push(announced.clone());
                next.sort_unstable();
                (id.clone(), next)
            })
            .collect::<Vec<_>>())
    })?;
    for (agent_id, next) in changes {
        write_known(&mirror, &agent_id, next)?;
    }
    Ok(())
}

/// `canonical_agent_dynamics_config`: a new dict.
#[pyfunction]
fn canonical_config<'py>(raw: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let py = raw.py();
    let policy = canonical_policy(Some(raw))?;
    let config = PyDict::new(py);
    config.set_item(intern!(py, "schema_version"), 1_i64)?;
    config.set_item(intern!(py, "policy_version"), PyString::new(py, policy))?;
    if policy != POLICY_V1 {
        config.set_item(intern!(py, "source_id"), PyString::new(py, ORIENTATION_SOURCE))?;
    }
    Ok(config)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(record_proposal, m)?)?;
    m.add_function(wrap_pyfunction!(agent_knows_facility, m)?)?;
    m.add_function(wrap_pyfunction!(create_state, m)?)?;
    m.add_function(wrap_pyfunction!(add_agent, m)?)?;
    m.add_function(wrap_pyfunction!(learn_facility, m)?)?;
    m.add_function(wrap_pyfunction!(announce_installed_facility, m)?)?;
    m.add_function(wrap_pyfunction!(canonical_config, m)?)?;
    Ok(())
}
