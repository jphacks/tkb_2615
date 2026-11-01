//! `movement_v1`: where people and staff are and the trips under way (`domain/movement.py`). The
//! rules that run every tick are `rules/movement.rs`; the others write one record at a time through
//! the forwarded writes (docs/rust-port/lanes/i-movement.md, m4-movement.md).
//!
//! A value of another type than the rules write taints the namespace, and the Python rules and
//! their errors run on the tree: an int `progress` or an int coordinate, which validation accepts,
//! only costs the row.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

use crate::core::row::{load_row, state_row, Field, IdMap};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::{Misfit, Part, RowType, Write};
use crate::{write_json_string, Encoder};

state_row! {
    #[derive(Clone, Copy)]
    pub(crate) struct Point {
        x: f64,
        y: f64,
    }
}

state_row! {
    #[derive(Clone)]
    pub(crate) struct Position {
        zone_id: Arc<str>,
        point: Option<Point>,
        edge_id: Option<Arc<str>>,
        progress: f64,
        position_kind: Arc<str>,
    }
}

/// A trip. The last three fields are absent from the record when they are `None`, which `state_row!`
/// cannot say: only queue trips have `queue_id`, and only staff support trips the other two. A
/// null there fits no rule (`validate_movement_runtime_state`), so it is a `Misfit` here.
pub(crate) struct Journey {
    pub(crate) event_id: Arc<str>,
    pub(crate) task_id: Arc<str>,
    pub(crate) purpose: Arc<str>,
    pub(crate) facility_id: Option<Arc<str>>,
    pub(crate) destination_zone_id: Arc<str>,
    pub(crate) route_zone_ids: Vec<Arc<str>>,
    pub(crate) route_edge_ids: Vec<Arc<str>>,
    pub(crate) route_points: Vec<Point>,
    pub(crate) started_minute: i64,
    pub(crate) due_minute: i64,
    pub(crate) queue_id: Option<Arc<str>>,
    pub(crate) support_request_id: Option<Arc<str>>,
    pub(crate) support_staff_id: Option<Arc<str>>,
}

const JOURNEY_REQUIRED_FIELDS: usize = 10;

impl Journey {
    /// A present field of the record that may be absent. `Arc<str>` is what refuses a null.
    fn optional(record: &Bound<'_, PyDict>, key: &Bound<'_, PyString>, found: &mut usize) -> Result<Option<Arc<str>>, Misfit> {
        match record.get_item(key)? {
            Some(value) => {
                *found += 1;
                Ok(Some(Field::from_py(&value)?))
            }
            None => Ok(None),
        }
    }

    fn write_optional(encoder: &mut Encoder, name: &str, value: &Option<Arc<str>>) -> PyResult<()> {
        if let Some(value) = value {
            encoder.out.push(b',');
            write_json_string(&mut encoder.out, name);
            encoder.out.push(b':');
            Field::write_json(value, encoder)?;
        }
        Ok(())
    }
}

fn required<T: Field>(record: &Bound<'_, PyDict>, key: &Bound<'_, PyString>) -> Result<T, Misfit> {
    T::from_py(&record.get_item(key)?.ok_or_else(|| Misfit::new("a journey field is missing"))?)
}

impl Field for Journey {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        let py = value.py();
        let record = as_readable_dict(value).ok_or_else(|| Misfit::new("not a dict"))?;
        let mut found = JOURNEY_REQUIRED_FIELDS;
        let journey = Self {
            event_id: required(&record, intern!(py, "event_id"))?,
            task_id: required(&record, intern!(py, "task_id"))?,
            purpose: required(&record, intern!(py, "purpose"))?,
            facility_id: required(&record, intern!(py, "facility_id"))?,
            destination_zone_id: required(&record, intern!(py, "destination_zone_id"))?,
            route_zone_ids: required(&record, intern!(py, "route_zone_ids"))?,
            route_edge_ids: required(&record, intern!(py, "route_edge_ids"))?,
            route_points: required(&record, intern!(py, "route_points"))?,
            started_minute: required(&record, intern!(py, "started_minute"))?,
            due_minute: required(&record, intern!(py, "due_minute"))?,
            queue_id: Self::optional(&record, intern!(py, "queue_id"), &mut found)?,
            support_request_id: Self::optional(&record, intern!(py, "support_request_id"), &mut found)?,
            support_staff_id: Self::optional(&record, intern!(py, "support_staff_id"), &mut found)?,
        };
        if record.len() != found {
            return Err(Misfit::new("other fields than the row's"));
        }
        Ok(journey)
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        // The order of the record `start_movement` writes.
        let record = PyDict::new(py);
        record.set_item(intern!(py, "event_id"), Field::to_py(&self.event_id, py)?)?;
        record.set_item(intern!(py, "task_id"), Field::to_py(&self.task_id, py)?)?;
        record.set_item(intern!(py, "purpose"), Field::to_py(&self.purpose, py)?)?;
        record.set_item(intern!(py, "facility_id"), Field::to_py(&self.facility_id, py)?)?;
        record.set_item(intern!(py, "destination_zone_id"), Field::to_py(&self.destination_zone_id, py)?)?;
        record.set_item(intern!(py, "route_zone_ids"), Field::to_py(&self.route_zone_ids, py)?)?;
        record.set_item(intern!(py, "route_edge_ids"), Field::to_py(&self.route_edge_ids, py)?)?;
        record.set_item(intern!(py, "route_points"), Field::to_py(&self.route_points, py)?)?;
        record.set_item(intern!(py, "started_minute"), Field::to_py(&self.started_minute, py)?)?;
        record.set_item(intern!(py, "due_minute"), Field::to_py(&self.due_minute, py)?)?;
        if let Some(value) = &self.queue_id {
            record.set_item(intern!(py, "queue_id"), Field::to_py(value, py)?)?;
        }
        if let Some(value) = &self.support_request_id {
            record.set_item(intern!(py, "support_request_id"), Field::to_py(value, py)?)?;
        }
        if let Some(value) = &self.support_staff_id {
            record.set_item(intern!(py, "support_staff_id"), Field::to_py(value, py)?)?;
        }
        Ok(record.into_any())
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        fn field<T: Field>(encoder: &mut Encoder, name: &str, value: &T) -> PyResult<()> {
            write_json_string(&mut encoder.out, name);
            encoder.out.push(b':');
            value.write_json(encoder)
        }
        // The keys in byte order, as canonical_json has them.
        encoder.out.push(b'{');
        field(encoder, "destination_zone_id", &self.destination_zone_id)?;
        encoder.out.push(b',');
        field(encoder, "due_minute", &self.due_minute)?;
        encoder.out.push(b',');
        field(encoder, "event_id", &self.event_id)?;
        encoder.out.push(b',');
        field(encoder, "facility_id", &self.facility_id)?;
        encoder.out.push(b',');
        field(encoder, "purpose", &self.purpose)?;
        Self::write_optional(encoder, "queue_id", &self.queue_id)?;
        encoder.out.push(b',');
        field(encoder, "route_edge_ids", &self.route_edge_ids)?;
        encoder.out.push(b',');
        field(encoder, "route_points", &self.route_points)?;
        encoder.out.push(b',');
        field(encoder, "route_zone_ids", &self.route_zone_ids)?;
        encoder.out.push(b',');
        field(encoder, "started_minute", &self.started_minute)?;
        Self::write_optional(encoder, "support_request_id", &self.support_request_id)?;
        Self::write_optional(encoder, "support_staff_id", &self.support_staff_id)?;
        encoder.out.push(b',');
        field(encoder, "task_id", &self.task_id)?;
        encoder.out.push(b'}');
        Ok(())
    }

    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        match (path, write) {
            ([], Write::Set { key: Part::Key(key), value }) => {
                match *key {
                    "event_id" => self.event_id = Field::from_py(value)?,
                    "task_id" => self.task_id = Field::from_py(value)?,
                    "purpose" => self.purpose = Field::from_py(value)?,
                    "facility_id" => self.facility_id = Field::from_py(value)?,
                    "destination_zone_id" => self.destination_zone_id = Field::from_py(value)?,
                    "route_zone_ids" => self.route_zone_ids = Field::from_py(value)?,
                    "route_edge_ids" => self.route_edge_ids = Field::from_py(value)?,
                    "route_points" => self.route_points = Field::from_py(value)?,
                    "started_minute" => self.started_minute = Field::from_py(value)?,
                    "due_minute" => self.due_minute = Field::from_py(value)?,
                    "queue_id" => self.queue_id = Some(Field::from_py(value)?),
                    "support_request_id" => self.support_request_id = Some(Field::from_py(value)?),
                    "support_staff_id" => self.support_staff_id = Some(Field::from_py(value)?),
                    _ => return Err(Misfit::new(format!("no field {key:?}"))),
                }
                Ok(())
            }
            ([], Write::Delete { key: Part::Key(key) }) => {
                match *key {
                    "queue_id" if self.queue_id.is_some() => self.queue_id = None,
                    "support_request_id" if self.support_request_id.is_some() => self.support_request_id = None,
                    "support_staff_id" if self.support_staff_id.is_some() => self.support_staff_id = None,
                    _ => return Err(Misfit::new("the write changes which fields a record has")),
                }
                Ok(())
            }
            ([Part::Key("route_zone_ids"), rest @ ..], write) => self.route_zone_ids.apply(rest, write),
            ([Part::Key("route_edge_ids"), rest @ ..], write) => self.route_edge_ids.apply(rest, write),
            ([Part::Key("route_points"), rest @ ..], write) => self.route_points.apply(rest, write),
            _ => Err(Misfit::new("the write does not fit a journey")),
        }
    }
}

state_row! {
    pub(crate) struct MovementRow {
        schema_version: i64,
        policy_version: Arc<str>,
        positions: IdMap<Position>,
        staff_positions: IdMap<Position>,
        journeys: IdMap<Journey>,
        edge_usage_minutes: IdMap<i64>,
        last_updated_minute: i64 => set_last_updated_minute,
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("movement", load_row::<MovementRow>))?)?;
    Ok(())
}
