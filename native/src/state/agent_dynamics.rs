//! `agent_dynamics_v1`: each agent's decision memory and known facilities. The rules that write and
//! read it are `rules/agent_dynamics.rs`; `domain/agent_dynamics.py` stays the comparison base and
//! gives every error text (docs/rust-port/lanes/f-action-lifecycle.md).

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;

use crate::core::row::{
    attribute, exact_dict, exact_int, exact_str, load_row, namespace_is_absent, state_row, unsupported, IdMap,
};
use crate::core::world_core::{mirror_of, NamespaceMirror, RowType};
use crate::state::entities::{entities_of, EntityStore, FacilityRow, Table};

pub(crate) const NAME: &str = "agent_dynamics_v1";
pub(crate) const POLICY_V1: &str = "need-led-autonomy-memory-v1";
pub(crate) const POLICY_V2: &str = "need-led-autonomy-memory-v2";
pub(crate) const POLICY_V3: &str = "need-led-autonomy-memory-v3";
pub(crate) const ORIENTATION_SOURCE: &str = "ASM-INTAKE-FACILITY-ORIENTATION-V1";

state_row! {
    pub(crate) struct AgentMemory {
        known_facility_ids: Vec<Arc<str>>,
        last_action: Option<Arc<str>> => set_last_action,
        last_target_id: Option<Arc<str>> => set_last_target_id,
        last_decision_minute: Option<i64> => set_last_decision_minute,
        decision_due_minute: i64 => set_decision_due_minute,
        decision_reason: Option<Arc<str>> => set_decision_reason,
    }
}

state_row! {
    pub(crate) struct AgentDynamicsRow {
        schema_version: i64,
        policy_version: Arc<str>,
        agents: IdMap<AgentMemory>,
    }
}

impl AgentMemory {
    /// The per-record rules of `validate_agent_dynamics_state`, for a record already of its types.
    fn is_valid(&self, facilities: &Table<FacilityRow>) -> bool {
        self.decision_due_minute >= 0
            && self.last_decision_minute.is_none_or(|minute| minute >= 0)
            && self.known_facility_ids.iter().all(|id| facilities.get(id).is_some())
            && has_no_repeats(&self.known_facility_ids)
    }
}

/// Nearly always a sorted list, which needs no set.
fn has_no_repeats(ids: &[Arc<str>]) -> bool {
    if ids.windows(2).all(|pair| pair[0] < pair[1]) {
        return true;
    }
    let mut ordered: Vec<&Arc<str>> = ids.iter().collect();
    ordered.sort_unstable();
    ordered.windows(2).all(|pair| pair[0] != pair[1])
}

/// `canonical_agent_dynamics_config(raw)["policy_version"]`; Unsupported where the Python rule
/// raises, or takes a value of another type, such as `schema_version: 1.0`.
pub(crate) fn canonical_policy(raw: Option<&Bound<'_, PyAny>>) -> PyResult<&'static str> {
    let Some(raw) = raw.filter(|raw| !raw.is_none()) else {
        return Ok(POLICY_V1);
    };
    let py = raw.py();
    let raw = exact_dict(raw)?;
    for (key, _) in raw.iter() {
        if !matches!(exact_str(&key)?, "schema_version" | "policy_version" | "source_id") {
            return unsupported("the config has other fields");
        }
    }
    if let Some(schema) = raw.get_item(intern!(py, "schema_version"))? {
        if exact_int(&schema)? != 1 {
            return unsupported("another schema");
        }
    }
    let policy = match raw.get_item(intern!(py, "policy_version"))? {
        Some(version) => match exact_str(&version)? {
            POLICY_V1 => POLICY_V1,
            POLICY_V2 => POLICY_V2,
            POLICY_V3 => POLICY_V3,
            _ => return unsupported("another policy"),
        },
        None => POLICY_V1,
    };
    let source = raw.get_item(intern!(py, "source_id"))?;
    match (policy, source) {
        (POLICY_V1, Some(_)) => return unsupported("v1 has no orientation source"),
        (POLICY_V1, None) => {}
        (_, Some(source)) if exact_str(&source)? != ORIENTATION_SOURCE => {
            return unsupported("another orientation source");
        }
        _ => {}
    }
    Ok(policy)
}

/// The policy `world.model_configs` selects.
pub(crate) fn policy_of(world: &Bound<'_, PyAny>) -> PyResult<&'static str> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    canonical_policy(exact_dict(&configs)?.get_item(intern!(py, "agent_dynamics"))?.as_ref())
}

/// `validate_agent_dynamics_state`: accepts, or leaves it to the Python rule and its error text.
#[pyfunction]
fn validate(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    if namespace_is_absent(world, intern!(world.py(), NAME))? {
        return Ok(true);
    }
    let policy = policy_of(world)?;
    let mirror = mirror_of(world, NAME)?;
    let store = entities_of(world)?;
    NamespaceMirror::with_row::<AgentDynamicsRow, _>(&mirror, |row| {
        if row.schema_version != 1 || &*row.policy_version != policy {
            return Ok(false);
        }
        EntityStore::with_rows(&store, |rows| {
            Ok(row.agents.len() == rows.agents.len()
                && row
                    .agents
                    .iter()
                    .all(|(id, record)| rows.agents.get(id).is_some() && record.is_valid(&rows.facilities)))
        })
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("agent_dynamics", load_row::<AgentDynamicsRow>))?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    Ok(())
}
