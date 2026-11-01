//! The namespaces and the world field that hold free-form JSON and change only at events:
//! `population_presence_v1`, `population_lifecycle_v1`, `population_dynamics_v1`,
//! `temporary_spaces_v1`, `geometry_overlay_v1` and `model_configs`.
//!
//! Their row is a `V` (docs/rust-port/step3-design.md). A tick reads them often and, in most Runs,
//! never writes them, so what owning them gives is the mirror's generation: the check at the end
//! of a tick knows they are unchanged without encoding or comparing them (`core/tick_validation.py`).
//! The rules of the population_* ones are `rules/population.rs` and `rules/population_lifecycle.rs`;
//! docs/rust-port/lanes/m6-population.md and m6-population-rules.md say which and why.

use pyo3::prelude::*;

use crate::core::world_core::{load_value_row, RowType};

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("population", load_value_row))?)?;
    Ok(())
}
