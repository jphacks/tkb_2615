//! A small owned namespace, used by tests only: no world owns it unless a test says so
//! (docs/rust-port/lanes/p4-world-core.md; the production template is
//! `psychological_distress.rs`). Its tree looks like
//! `{"schema_version": 1, "total": 0.0, "people": {id: {"level": 0.5, "visits": 0, "note": None}},
//! "notes": [...]}`.
//!
//! `ROW` has typed fields and misfits on anything else. `VALUE_ROW` holds any JSON as a `V`.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::core::row::{load_row, state_row, Field, IdMap};
use crate::core::value::V;
use crate::core::world_core::{
    core_of, load_value_row, mirror_of, NamespaceMirror, RowType,
};
use crate::{Encoder, Unsupported};

const NAME: &str = "sample_v1";

state_row! {
    pub(crate) struct Person {
        level: f64 => set_level,
        visits: i64,
        note: Option<Arc<str>>,
    }
}

state_row! {
    pub(crate) struct SampleRow {
        schema_version: i64,
        total: f64 => set_total,
        people: IdMap<Person>,
        notes: V,
    }
}

/// A rule's write: the setters change the row and the tree. The tree's write runs no override,
/// and forwards nothing back.
fn write_level(world: &Bound<'_, PyAny>, person: &str, level: f64) -> PyResult<()> {
    let mirror: Bound<'_, NamespaceMirror> = mirror_of(world, NAME)?;
    NamespaceMirror::with_row_tree::<SampleRow, _>(&mirror, |row, tree| {
        let found = row
            .people
            .get_mut(person)
            .ok_or_else(|| Unsupported::new_err("no such person"))?;
        let total = row.total + (level - found.level);
        found.set_level(&tree.child(intern!(tree.py(), "people"))?.child_str(person)?, level)?;
        row.set_total(tree, total)
    })
}

/// Set one person's level and keep the total, as a ported rule would.
#[pyfunction]
fn set_level(world: &Bound<'_, PyAny>, person: &str, level: f64) -> PyResult<()> {
    write_level(world, person, level)
}

/// Run a Python callback, then write: the world's core is read after the callback, because the
/// callback may have swapped the world's state.
#[pyfunction]
fn set_level_after(
    world: &Bound<'_, PyAny>,
    callback: &Bound<'_, PyAny>,
    person: &str,
    level: f64,
) -> PyResult<()> {
    let before = core_of(world)?;
    callback.call0()?;
    drop(before);
    write_level(world, person, level)
}

/// The row's canonical JSON, read from the world's core now.
#[pyfunction]
fn row_json<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    let mirror = mirror_of(world, NAME)?;
    let py = world.py();
    NamespaceMirror::with_row::<SampleRow, _>(&mirror, |row| {
        let mut encoder = Encoder { out: Vec::with_capacity(256) };
        Field::write_json(row, &mut encoder)?;
        Ok(PyBytes::new(py, &encoder.out))
    })
}

/// A validator a lane would register: accepts or says Unsupported, and never raises a rule's text.
#[pyfunction]
fn validate(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let mirror = mirror_of(world, NAME)?;
    NamespaceMirror::with_row::<SampleRow, _>(&mirror, |row| {
        Ok(row.schema_version == 1 && row.people.values().all(|person| (0.0..=1.0).contains(&person.level)))
    })
}

/// A step of a phase done by Rust: returns the sum of the levels, or Unsupported.
#[pyfunction]
fn sum_levels(world: &Bound<'_, PyAny>) -> PyResult<f64> {
    let mirror = mirror_of(world, NAME)?;
    NamespaceMirror::with_row::<SampleRow, _>(&mirror, |row| {
        Ok(row.people.values().map(|person| person.level).fold(0.0, |sum, level| sum + level))
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("sample_namespace", load_row::<SampleRow>))?)?;
    m.add("VALUE_ROW", Py::new(py, RowType::new("sample_namespace_value", load_value_row))?)?;
    m.add_function(wrap_pyfunction!(set_level, m)?)?;
    m.add_function(wrap_pyfunction!(set_level_after, m)?)?;
    m.add_function(wrap_pyfunction!(row_json, m)?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    m.add_function(wrap_pyfunction!(sum_levels, m)?)?;
    Ok(())
}
