//! `world.metric_state`: floats by name, a world field rather than a part of `model_state`
//! (`OWNED_FIELDS` in `core/state_mirror.py`). Python's rules write single keys here, and each
//! write is forwarded; the distress monitor of `rules/psychology.rs` is Rust's own writer.

use pyo3::prelude::*;

use crate::core::row::{load_row, IdMap};
use crate::core::world_core::RowType;

pub(crate) type MetricRow = IdMap<f64>;

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("ROW", Py::new(m.py(), RowType::new("metric_state", load_row::<MetricRow>))?)?;
    Ok(())
}
