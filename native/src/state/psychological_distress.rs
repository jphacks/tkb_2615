//! `psychological_distress_v1`: the template of an owned namespace
//! (docs/rust-port/lanes/p5-template-namespace.md). The rule that writes it is
//! `rules/psychology.rs`; `domain/psychological_state.py` stays the comparison base.

use pyo3::prelude::*;

use crate::core::row::{load_row, state_row, IdMap};
use crate::core::world_core::{mirror_of, NamespaceMirror, RowType};

pub(crate) const NAME: &str = "psychological_distress_v1";
const SCHEMA_VERSION: i64 = 1;
const MAX_AGENTS: usize = 100_000;

state_row! {
    #[derive(Clone, Copy)]
    pub(crate) struct AgentRecord {
        baseline_distress: f64,
        long_term_distress: f64 => set_long_term_distress,
        daily_acute_sum: f64 => set_daily_acute_sum,
        daily_high_distress_minutes: i64 => set_daily_high_distress_minutes,
        daily_observed_minutes: i64 => set_daily_observed_minutes,
        completed_days: i64 => set_completed_days,
    }
}

state_row! {
    pub(crate) struct DistressRow {
        schema_version: i64,
        last_updated_minute: i64 => set_last_updated_minute,
        agents: IdMap<AgentRecord>,
    }
}

impl AgentRecord {
    /// validate_psychological_state's rules for one record of the row's exact types.
    pub(crate) fn is_valid(&self) -> bool {
        let observed = self.daily_observed_minutes;
        let high = self.daily_high_distress_minutes;
        let total = self.daily_acute_sum;
        (0.0..=1.0).contains(&self.baseline_distress)
            && (0.0..=1.0).contains(&self.long_term_distress)
            && total.is_finite()
            && (0..1440).contains(&observed)
            && (0..=observed).contains(&high)
            && self.completed_days >= 0
            && total >= 0.0
            && total <= observed as f64 + 1e-9
    }
}

impl DistressRow {
    /// Whether validate_psychological_state passes the namespace. False says nothing about it:
    /// the Python rules then say what is wrong.
    pub(crate) fn is_valid(&self) -> bool {
        self.schema_version == SCHEMA_VERSION
            && self.last_updated_minute >= -1
            && self.agents.len() <= MAX_AGENTS
            && self.agents.iter().all(|(id, record)| !id.is_empty() && record.is_valid())
    }
}

/// The namespace's validator: accepts, or leaves it to the Python rules and their error text.
#[pyfunction]
fn validate(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let mirror = mirror_of(world, NAME)?;
    NamespaceMirror::with_row::<DistressRow, _>(&mirror, |row| Ok(row.is_valid()))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("psychological_distress", load_row::<DistressRow>))?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    Ok(())
}
