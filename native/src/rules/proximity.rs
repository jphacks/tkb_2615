//! `DistanceSampledProximityPolicy.sample` (`policies/proximity.py`) and the step that schedules what
//! it finds, `_sample_proximity_contacts` (`experiments/phases/sample.py`). The positions come from the
//! row of `movement_v1`; a tainted row (an int coordinate, which validation accepts) from its tree.
//! A coordinate whose cell does not fit an i64 says `Unsupported`, as `math.floor` gives an exact int
//! of any size. Validation does not bound coordinates, but no floor plan reaches them.

use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple, PyType};
use rustc_hash::{FxHashMap, FxHashSet};

use crate::core::mathx::hypot;
use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::core::world_core::{mirror_of, raw_dict_get, NamespaceMirror};
use crate::rules::movement::KEY as MOVEMENT;
use crate::state::events::{EventKind, EventRow, EventWriter};
use crate::state::movement::MovementRow;
use crate::Unsupported;

const SOURCE_ID: &str = "ASM-PROXIMITY-CONTACT-V1";

#[track_caller]
fn unsupported<T>(reason: &'static str) -> PyResult<T> {
    Err(Unsupported::new_err(reason))
}

static POLICY: GILOnceCell<Py<PyType>> = GILOnceCell::new();

fn is_the_policy(policy: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = policy.py();
    let kind = POLICY.get_or_try_init(py, || -> PyResult<_> {
        Ok(py
            .import("sheltersim.policies.proximity")?
            .getattr("DistanceSampledProximityPolicy")?
            .downcast_into::<PyType>()?
            .unbind())
    })?;
    Ok(policy.get_type().is(kind.bind(py)))
}

/// A person the sampler sees, as `points` holds them.
struct Placed {
    id: Arc<str>,
    zone: Arc<str>,
    x: f64,
    y: f64,
}

/// The positions the head of `sample` keeps from the row: `(agent, zone, x, y)`.
fn from_row(mirror: &Bound<'_, NamespaceMirror>) -> PyResult<Vec<Placed>> {
    NamespaceMirror::with_row::<MovementRow, _>(mirror, |row| {
        let mut found = Vec::with_capacity(row.positions.len());
        for (id, position) in row.positions.iter() {
            if &*position.position_kind != "physical" {
                continue;
            }
            if let Some(point) = &position.point {
                found.push(Placed { id: id.clone(), zone: position.zone_id.clone(), x: point.x, y: point.y });
            }
        }
        Ok(found)
    })
}

/// `float(value)` of a coordinate; None for one the sampler skips.
fn coordinate(value: &Bound<'_, PyAny>) -> PyResult<Option<f64>> {
    if let Ok(number) = value.downcast_exact::<PyFloat>() {
        return Ok(Some(number.value()));
    }
    if value.is_instance_of::<PyBool>() {
        return Ok(None);
    }
    if let Ok(number) = value.downcast_exact::<PyInt>() {
        return match number.extract::<i64>() {
            Ok(number) => Ok(Some(number as f64)),
            Err(_) => unsupported("a coordinate is too large an int"),
        };
    }
    if value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>() {
        return unsupported("a coordinate is a subclass of a number");
    }
    Ok(None)
}

/// The same from the tree, with each thing the Python rule skips.
fn from_tree(world: &Bound<'_, PyAny>) -> PyResult<Vec<Placed>> {
    let py = world.py();
    let movement = world
        .getattr(intern!(py, "model_state"))?
        .call_method1(intern!(py, "get"), (MOVEMENT,))?;
    if !movement.is_instance_of::<PyDict>() {
        return Ok(Vec::new());
    }
    let Some(movement) = as_readable_dict(&movement) else {
        return unsupported("the movement state is another kind of dict");
    };
    let Some(positions) = movement.get_item(intern!(py, "positions"))? else {
        return Ok(Vec::new());
    };
    let Some(positions) = as_readable_dict(&positions) else {
        return unsupported("the positions are not a plain dict");
    };
    let mut found = Vec::with_capacity(positions.len());
    for (id, position) in positions.iter() {
        let Ok(id) = id.downcast_exact::<PyString>() else {
            return unsupported("a position's key is not a str");
        };
        let Some(position) = as_readable_dict(&position) else {
            if position.is_instance_of::<PyDict>() {
                return unsupported("a position is another kind of dict");
            }
            continue;
        };
        let is_physical = match position.get_item(intern!(py, "position_kind"))? {
            Some(kind) => kind.downcast_exact::<PyString>().is_ok_and(|text| text.to_str().is_ok_and(|text| text == "physical")),
            None => false,
        };
        if !is_physical {
            continue;
        }
        let zone = match position.get_item(intern!(py, "zone_id"))? {
            Some(zone) => zone,
            None => continue,
        };
        let Ok(zone) = zone.downcast_exact::<PyString>() else {
            if zone.is_none() || zone.is_exact_instance_of::<PyInt>() || zone.is_exact_instance_of::<PyFloat>() {
                continue;
            }
            return unsupported("a position's zone is neither a str nor a scalar");
        };
        let Some(point) = position.get_item(intern!(py, "point"))? else {
            continue;
        };
        let Some(point) = as_readable_dict(&point) else {
            if point.is_instance_of::<PyDict>() {
                return unsupported("a point is another kind of dict");
            }
            continue;
        };
        if point.len() != 2 {
            continue;
        }
        let (Some(x), Some(y)) = (point.get_item(intern!(py, "x"))?, point.get_item(intern!(py, "y"))?) else {
            continue;
        };
        let (Some(x), Some(y)) = (coordinate(&x)?, coordinate(&y)?) else {
            continue;
        };
        found.push(Placed { id: Arc::from(id.to_str()?), zone: Arc::from(zone.to_str()?), x, y });
    }
    Ok(found)
}

/// The cell of a coordinate: `math.floor(value / radius)`.
fn cell(value: f64, radius: f64) -> PyResult<i64> {
    const LIMIT: f64 = (1_u64 << 62) as f64;
    let floor = (value / radius).floor();
    if floor.abs() < LIMIT {
        Ok(floor as i64)
    } else {
        unsupported("a coordinate's cell does not fit an i64")
    }
}

/// `(distance, first, second)`, the indexes in `points`, sorted as `sample` sorts them and cut to
/// the limit. `points` is in ID order, so comparing indexes is comparing IDs.
fn pairs_within(points: &[Placed], radius: f64, limit: usize) -> PyResult<Vec<(f64, usize, usize)>> {
    let mut cells: FxHashMap<(&str, i64, i64), Vec<usize>> = FxHashMap::default();
    let mut places = Vec::with_capacity(points.len());
    for (index, point) in points.iter().enumerate() {
        let place = (cell(point.x, radius)?, cell(point.y, radius)?);
        places.push(place);
        cells.entry((&point.zone, place.0, place.1)).or_default().push(index);
    }
    let mut candidates = Vec::new();
    let mut visited: FxHashSet<(usize, usize)> = FxHashSet::default();
    for (index, point) in points.iter().enumerate() {
        let (cell_x, cell_y) = places[index];
        for offset_x in [-1, 0, 1] {
            for offset_y in [-1, 0, 1] {
                let Some(others) = cells.get(&(&*point.zone, cell_x + offset_x, cell_y + offset_y)) else {
                    continue;
                };
                for &other in others {
                    if other == index {
                        continue;
                    }
                    let pair = (index.min(other), index.max(other));
                    if !visited.insert(pair) {
                        continue;
                    }
                    let (first, second) = (&points[pair.0], &points[pair.1]);
                    let distance = hypot(&[second.x - first.x, second.y - first.y]);
                    if distance <= radius {
                        candidates.push((distance, pair.0, pair.1));
                    }
                }
            }
        }
    }
    candidates.sort_unstable_by(|first, second| {
        first.0.total_cmp(&second.0).then((first.1, first.2).cmp(&(second.1, second.2)))
    });
    candidates.truncate(limit);
    Ok(candidates)
}

/// What `sample` finds of a world, given the policy's config: `points`, and the pairs.
fn sample(world: &Bound<'_, PyAny>, policy_config: &Bound<'_, PyAny>) -> PyResult<(Vec<Placed>, Vec<(f64, usize, usize)>)> {
    let py = world.py();
    let radius = policy_config.get_item(intern!(py, "max_distance_m"))?;
    let radius = if let Ok(number) = radius.downcast_exact::<PyFloat>() {
        number.value()
    } else if let Ok(number) = radius.downcast_exact::<PyInt>() {
        number.extract::<i64>().map_err(|_| Unsupported::new_err("the proximity radius is too large"))? as f64
    } else {
        return unsupported("the proximity radius is not a number");
    };
    if !(radius.is_finite() && radius > 0.0) {
        return unsupported("the proximity radius is not positive");
    }
    let limit = policy_config.get_item(intern!(py, "max_pairs_per_sample"))?;
    let Ok(limit) = limit.downcast_exact::<PyInt>().map(|number| number.extract::<i64>()) else {
        return unsupported("the proximity limit is not an int");
    };
    let limit = match limit {
        Ok(limit) if limit >= 0 => limit as usize,
        _ => return unsupported("the proximity limit is out of range"),
    };
    let placed = match mirror_of(world, MOVEMENT).and_then(|mirror| from_row(&mirror)) {
        Ok(placed) => placed,
        Err(error) if error.is_instance_of::<Unsupported>(py) => from_tree(world)?,
        Err(error) => return Err(error),
    };
    let agents = world.getattr(intern!(py, "agents"))?;
    let Some(agents) = as_readable_dict(&agents) else {
        return unsupported("the people are not a plain dict");
    };
    let zones = world.getattr(intern!(py, "zones"))?;
    let Some(zones) = as_readable_dict(&zones) else {
        return unsupported("the zones are not a plain dict");
    };
    let mut points = Vec::with_capacity(placed.len());
    for point in placed {
        // The checks in the order of the Python rule's `or`; none has an effect.
        if !point.x.is_finite() || !point.y.is_finite() {
            continue;
        }
        if raw_dict_get(agents.as_any(), PyString::new(py, &point.id).as_any())?.is_none()
            || raw_dict_get(zones.as_any(), PyString::new(py, &point.zone).as_any())?.is_none()
        {
            continue;
        }
        points.push(point);
    }
    points.sort_unstable_by(|first, second| first.id.cmp(&second.id));
    let pairs = pairs_within(&points, radius, limit)?;
    Ok((points, pairs))
}

fn point_value(point: &Placed) -> V {
    let mut map = IndexMap::with_capacity(2);
    map.insert(Arc::from("x"), V::Float(point.x));
    map.insert(Arc::from("y"), V::Float(point.y));
    V::Map(map)
}

/// `_sample_proximity_contacts`, the step of the sample phase.
#[pyfunction]
pub(crate) fn sample_contacts<'py>(controller: &Bound<'py, PyAny>, minute: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = controller.py();
    let config = controller.getattr(intern!(py, "proximity_contact_config"))?;
    if config.is_none() {
        return Ok(());
    }
    let Ok(Ok(minute)) = minute.downcast_exact::<PyInt>().map(|number| number.extract::<i64>()) else {
        return unsupported("the minute is not an int");
    };
    let interval = config.get_item(intern!(py, "sample_interval_minutes"))?;
    let Ok(Ok(interval)) = interval.downcast_exact::<PyInt>().map(|number| number.extract::<i64>()) else {
        return unsupported("the sampling interval is not an int");
    };
    if interval <= 0 {
        return unsupported("the sampling interval is not positive");
    }
    if minute.rem_euclid(interval) != 0 {
        return Ok(());
    }
    let world = controller.getattr(intern!(py, "world"))?;
    let writer = EventWriter::of(&world)?;
    let due_minute = minute + 1;
    if due_minute >= writer.duration() {
        return Ok(());
    }
    let policy = controller.getattr(intern!(py, "proximity_policy"))?;
    if !is_the_policy(&policy)? {
        return unsupported("another proximity policy");
    }
    let version = config.get_item(intern!(py, "policy_version"))?;
    let Ok(version) = version.downcast_exact::<PyString>() else {
        return unsupported("the proximity policy's version is not a str");
    };
    let version: Arc<str> = Arc::from(version.to_str()?);
    if due_minute < 0 || writer.next_sequence()? < 0 {
        return unsupported("a minute or a sequence is negative");
    }
    let (points, pairs) = sample(&world, &policy.getattr(intern!(py, "config"))?)?;
    let source: Arc<str> = Arc::from(SOURCE_ID);
    for (distance, first, second) in pairs {
        let (first, second) = (&points[first], &points[second]);
        let sequence = writer.next_sequence()?;
        let mut payload = IndexMap::with_capacity(8);
        payload.insert(Arc::from("first_agent_id"), V::Str(first.id.clone()));
        payload.insert(Arc::from("second_agent_id"), V::Str(second.id.clone()));
        payload.insert(Arc::from("zone_id"), V::Str(first.zone.clone()));
        payload.insert(Arc::from("observed_minute"), V::Int(due_minute));
        payload.insert(Arc::from("contact_minutes"), V::Int(0));
        payload.insert(Arc::from("distance_m"), V::Float(distance));
        payload.insert(Arc::from("first_point"), point_value(first));
        payload.insert(Arc::from("second_point"), point_value(second));
        let mut provenance = IndexMap::with_capacity(4);
        provenance.insert(Arc::from("source_type"), V::Str(Arc::from("assumption")));
        provenance.insert(Arc::from("source_id"), V::Str(source.clone()));
        provenance.insert(Arc::from("policy_version"), V::Str(version.clone()));
        provenance.insert(Arc::from("observed_minute"), V::Int(due_minute));
        let row = EventRow::new(
            Arc::from(format!("proximity-contact-{sequence}")),
            EventKind::Endogenous,
            due_minute,
            40,
            sequence,
            Arc::from("proximity_contact_update"),
            payload,
            Some(source.clone()),
            None,
            provenance,
        );
        // Finite values, stable IDs and non-negative numbers, all checked above.
        let Some(row) = row else {
            return Err(PyRuntimeError::new_err("a proximity contact is not an event"));
        };
        writer.allocate_sequence()?;
        writer.schedule(row)?;
    }
    Ok(())
}

/// What `policy.sample(world)` returns, as `ProximityPair` fields, for the shadow and the tests.
#[pyfunction]
fn sample_of<'py>(world: &Bound<'py, PyAny>, policy_config: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
    let py = world.py();
    let (points, pairs) = sample(world, policy_config)?;
    let result = PyList::empty(py);
    let point_of = |point: &Placed| -> PyResult<Bound<'py, PyDict>> {
        let map = PyDict::new(py);
        map.set_item("x", point.x)?;
        map.set_item("y", point.y)?;
        Ok(map)
    };
    for (distance, first, second) in pairs {
        let (first, second) = (&points[first], &points[second]);
        result.append(PyTuple::new(
            py,
            [
                distance.into_pyobject(py)?.into_any(),
                PyString::new(py, &first.id).into_any(),
                PyString::new(py, &second.id).into_any(),
                PyString::new(py, &first.zone).into_any(),
                point_of(first)?.into_any(),
                point_of(second)?.into_any(),
            ],
        )?)?;
    }
    Ok(result)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sample_contacts, m)?)?;
    m.add_function(wrap_pyfunction!(sample_of, m)?)?;
    Ok(())
}
