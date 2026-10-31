//! `social_propagation_v1`: the contacts of the last tick and the cooldowns of the social model
//! (`_read_runtime_state` in `policies/social.py`). The rule that writes it is
//! `rules/social_propagation.rs`.

use pyo3::prelude::*;

use crate::core::row::{load_row, state_row, IdMap};
use crate::core::world_core::RowType;

pub(crate) const NAME: &str = "social_propagation_v1";
pub(crate) const SCHEMA_VERSION: i64 = 1;

state_row! {
    #[derive(Clone, Copy)]
    pub(crate) struct ContactRecord {
        last_seen_minute: i64,
        consecutive_minutes: i64,
    }
}

state_row! {
    pub(crate) struct SocialRow {
        schema_version: i64,
        contacts: IdMap<ContactRecord>,
        agent_cooldown_until: IdMap<i64>,
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("social_propagation", load_row::<SocialRow>))?)?;
    Ok(())
}
