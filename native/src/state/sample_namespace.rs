//! The template of an owned namespace, used by tests only: no world owns it unless a test says so
//! (docs/rust-port/lanes/p4-world-core.md). Its tree looks like
//! `{"schema_version": 1, "total": 0.0, "people": {id: {"level": 0.5, "visits": 0, "note": None}},
//! "notes": [...]}`.
//!
//! `ROW` has typed fields and misfits on anything else. `VALUE_ROW` holds any JSON as a `V`.

use std::any::Any;
use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyString};
use pyo3::IntoPyObjectExt;

use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::core::world_core::{
    core_of, load_value_row, mirror_of, Misfit, NamespaceMirror, Part, Row, RowType, Write,
};
use crate::{write_json_string, Encoder, Unsupported};

const NAME: &str = "sample_v1";

#[derive(Clone)]
struct Person {
    level: f64,
    visits: i64,
    note: Option<Arc<str>>,
}

pub(crate) struct SampleRow {
    schema_version: i64,
    total: f64,
    people: IndexMap<Arc<str>, Person>,
    notes: V,
}

// A typed field takes only its exact Python type, so what it writes back is what was written.
fn float_of(value: &Bound<'_, PyAny>) -> Result<f64, Misfit> {
    Ok(value
        .downcast_exact::<PyFloat>()
        .map_err(|_| Misfit::new("not a float"))?
        .value())
}

fn int_of(value: &Bound<'_, PyAny>) -> Result<i64, Misfit> {
    if value.is_exact_instance_of::<PyBool>() {
        return Err(Misfit::new("not an int"));
    }
    Ok(value
        .downcast_exact::<PyInt>()
        .map_err(|_| Misfit::new("not an int"))?
        .extract()?)
}

fn note_of(value: &Bound<'_, PyAny>) -> Result<Option<Arc<str>>, Misfit> {
    if value.is_none() {
        return Ok(None);
    }
    Ok(Some(Arc::from(
        value
            .downcast_exact::<PyString>()
            .map_err(|_| Misfit::new("not a str or None"))?
            .to_str()?,
    )))
}

fn field<'py>(record: &Bound<'py, PyDict>, name: &str) -> Result<Bound<'py, PyAny>, Misfit> {
    record
        .get_item(name)?
        .ok_or_else(|| Misfit::new(format!("no field {name}")))
}

fn fields_are(record: &Bound<'_, PyDict>, count: usize) -> Result<(), Misfit> {
    if record.len() != count {
        return Err(Misfit::new("other fields than the row's"));
    }
    Ok(())
}

fn record_of<'a, 'py>(value: &'a Bound<'py, PyAny>) -> Result<&'a Bound<'py, PyDict>, Misfit> {
    as_readable_dict(value).ok_or_else(|| Misfit::new("not a dict"))
}

fn person_of(value: &Bound<'_, PyAny>) -> Result<Person, Misfit> {
    let record = record_of(value)?;
    fields_are(record, 3)?;
    Ok(Person {
        level: float_of(&field(record, "level")?)?,
        visits: int_of(&field(record, "visits")?)?,
        note: note_of(&field(record, "note")?)?,
    })
}

fn people_of(value: &Bound<'_, PyAny>) -> Result<IndexMap<Arc<str>, Person>, Misfit> {
    let mut people = IndexMap::new();
    for (key, record) in record_of(value)?.iter() {
        let key = key
            .downcast_exact::<PyString>()
            .map_err(|_| Misfit::new("not a str key"))?;
        people.insert(Arc::from(key.to_str()?), person_of(&record)?);
    }
    Ok(people)
}

fn load(tree: &Bound<'_, PyAny>) -> Result<Box<dyn Row>, Misfit> {
    let record = record_of(tree)?;
    fields_are(record, 4)?;
    Ok(Box::new(SampleRow {
        schema_version: int_of(&field(record, "schema_version")?)?,
        total: float_of(&field(record, "total")?)?,
        people: people_of(&field(record, "people")?)?,
        notes: V::from_py(&field(record, "notes")?)?,
    }))
}

impl Row for SampleRow {
    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        match (path, write) {
            ([], Write::Set { key: Part::Key("schema_version"), value }) => {
                self.schema_version = int_of(value)?;
            }
            ([], Write::Set { key: Part::Key("total"), value }) => self.total = float_of(value)?,
            ([], Write::Set { key: Part::Key("people"), value }) => self.people = people_of(value)?,
            ([], Write::Set { key: Part::Key("notes"), value }) => self.notes = V::from_py(value)?,
            ([Part::Key("people")], Write::Set { key: Part::Key(id), value }) => {
                let person = person_of(value)?;
                match self.people.get_mut(*id) {
                    Some(slot) => *slot = person,
                    None => {
                        self.people.insert(Arc::from(*id), person);
                    }
                }
            }
            ([Part::Key("people")], Write::Delete { key: Part::Key(id) }) => {
                self.people
                    .shift_remove(*id)
                    .ok_or_else(|| Misfit::new("no such person"))?;
            }
            ([Part::Key("people"), Part::Key(id)], Write::Set { key: Part::Key(name), value }) => {
                let person = self
                    .people
                    .get_mut(*id)
                    .ok_or_else(|| Misfit::new("no such person"))?;
                match *name {
                    "level" => person.level = float_of(value)?,
                    "visits" => person.visits = int_of(value)?,
                    "note" => person.note = note_of(value)?,
                    _ => return Err(Misfit::new("no such field")),
                }
            }
            // The free-form part takes any write, as ValueRow does.
            ([Part::Key("notes"), rest @ ..], write) => {
                let mut notes = crate::core::world_core::ValueRow(std::mem::replace(&mut self.notes, V::Null));
                let result = notes.apply(rest, write);
                self.notes = notes.0;
                result?;
            }
            _ => return Err(Misfit::new("the write has no field in the row")),
        }
        Ok(())
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let people = PyDict::new(py);
        for (id, person) in &self.people {
            let record = PyDict::new(py);
            record.set_item("level", person.level)?;
            record.set_item("visits", person.visits)?;
            record.set_item("note", person.note.as_deref())?;
            people.set_item(&**id, record)?;
        }
        let tree = PyDict::new(py);
        tree.set_item("schema_version", self.schema_version)?;
        tree.set_item("total", self.total)?;
        tree.set_item("people", people)?;
        tree.set_item("notes", self.notes.to_py(py)?)?;
        tree.into_bound_py_any(py)
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        encoder.out.extend_from_slice(b"{\"notes\":");
        self.notes.write_json(encoder)?;
        encoder.out.extend_from_slice(b",\"people\":{");
        let mut people: Vec<(&Arc<str>, &Person)> = self.people.iter().collect();
        people.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
        for (index, (id, person)) in people.into_iter().enumerate() {
            if index > 0 {
                encoder.out.push(b',');
            }
            write_json_string(&mut encoder.out, id);
            encoder.out.extend_from_slice(b":{\"level\":");
            encoder.float(person.level)?;
            encoder.out.extend_from_slice(b",\"note\":");
            match &person.note {
                Some(note) => write_json_string(&mut encoder.out, note),
                None => encoder.out.extend_from_slice(b"null"),
            }
            encoder.out.extend_from_slice(b",\"visits\":");
            encoder
                .out
                .extend_from_slice(itoa::Buffer::new().format(person.visits).as_bytes());
            encoder.out.push(b'}');
        }
        encoder.out.extend_from_slice(b"},\"schema_version\":");
        encoder
            .out
            .extend_from_slice(itoa::Buffer::new().format(self.schema_version).as_bytes());
        encoder.out.extend_from_slice(b",\"total\":");
        encoder.float(self.total)?;
        encoder.out.push(b'}');
        Ok(())
    }

    fn as_any_mut(&mut self) -> &mut dyn Any {
        self
    }
}

/// A rule's write: the row first, then the tree. The tree's write runs no override, and forwards
/// nothing back.
fn write_level(world: &Bound<'_, PyAny>, person: &str, level: f64) -> PyResult<()> {
    let py = world.py();
    let mirror: Bound<'_, NamespaceMirror> = mirror_of(world, NAME)?;
    let total = NamespaceMirror::with_row::<SampleRow, f64>(&mirror, |row| {
        let found = row
            .people
            .get_mut(person)
            .ok_or_else(|| Unsupported::new_err("no such person"))?;
        row.total += level - found.level;
        found.level = level;
        Ok(row.total)
    })?;
    NamespaceMirror::write_through(
        &mirror,
        &["people", person],
        "level",
        &level.into_bound_py_any(py)?,
    )?;
    NamespaceMirror::write_through(&mirror, &[], "total", &total.into_bound_py_any(py)?)
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
        row.write_json(&mut encoder)?;
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
    m.add("ROW", Py::new(py, RowType::new("sample_namespace", load))?)?;
    m.add("VALUE_ROW", Py::new(py, RowType::new("sample_namespace_value", load_value_row))?)?;
    m.add_function(wrap_pyfunction!(set_level, m)?)?;
    m.add_function(wrap_pyfunction!(set_level_after, m)?)?;
    m.add_function(wrap_pyfunction!(row_json, m)?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    m.add_function(wrap_pyfunction!(sum_levels, m)?)?;
    Ok(())
}
