//! `domain/temporary_spaces.py` and `domain/geometry_overlay.py`: capacity added during a Run, beds
//! and intake in a physical temporary room, the floor-plan point of a facility installed during a
//! Run, and the approved changes of the floor plan.
//!
//! Each call plans with nothing changed and then writes through the namespace's tree, whose writes
//! reach the rows. Anything but a plain success says Unsupported before any write: the Python rule
//! then runs from the start and raises its own text (docs/rust-port/lanes/m6-overlay.md). The
//! pydantic contracts of the parameters stay Python's: a rule calls `model_validate` where its
//! Python rule does.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Arc, Mutex};

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PySlice, PyString, PyTuple};
use sha2::{Digest, Sha256};

use crate::core::mathx::{hypot, pow, py_max, py_min, py_round, py_sum};
use crate::core::row::{attribute, exact_dict, exact_float, exact_int, exact_str, unsupported};
use crate::core::value::V;
use crate::core::world_core::{raw_dict_get, NamespaceMirror};
use crate::rules::movement::{mirror_held, mobility_text, point_object, room_minutes, KEY as MOVEMENT_KEY};
use crate::rules::movement_geometry::{
    is_identifier, point_in_polygon, point_in_value, point_segment_distance, point_walkable, points_in_value, polyline_length,
    segment_clear_of_polygon, segment_inside_polygon, usable, Constraints, Geo, Layout, Pt,
};
use crate::state::entities::{entities_of, EntityStore};
use crate::state::events::EventRow;
use crate::state::movement::MovementRow;
use crate::Unsupported;

pub(crate) const OVERLAY_KEY: &str = "geometry_overlay_v1";
const SPACES_KEY: &str = "temporary_spaces_v1";
const MAX_SPACES: usize = 256;
const MAX_SPACE_HISTORY: usize = 20_000;
const MAX_OVERLAY_HISTORY: usize = 20_000;
const POLICY_SPACES: &str = "typed-temporary-capacity-v1";
const POLICY_OVERLAY: &str = "bounded-runtime-geometry-v1";
const PLACEMENT_SOURCE: &str = "ASM-INSTALLED-FACILITY-PLACEMENT-V1";
const INTAKE_SOURCE: &str = "ASM-TEMPORARY-ROOM-INTAKE-V1";
/// `INSTALLED_FACILITY_MIN_SEPARATION_M`, `MAX_INSTALLED_FACILITY_GRID_GROWTH`.
const MIN_SEPARATION: f64 = 0.6;
const GRID_GROWTH: usize = 16;
const MAX_BED_ROUTES: usize = 256;
const MAX_ADMISSION_POINTS: usize = 256;
/// `reserve_sleep_bed`: how near another person stands to be using a bed, in metres.
const BED_RADIUS: f64 = 0.75;
/// `room_admission_point`: the grid of candidates and how near a resident stands to take a point.
const ADMISSION_STEP: f64 = 0.75;
const ADMISSION_RADIUS: f64 = 0.5;
/// How near a moved facility may stand to a wall.
const WALL_CLEARANCE: f64 = 0.5;

type Doc = IndexMap<Arc<str>, V>;

// ---- What ran and what was refused --------------------------------------------------------------

static RAN: Mutex<BTreeMap<&'static str, u64>> = Mutex::new(BTreeMap::new());
static REFUSALS: Mutex<BTreeMap<String, u64>> = Mutex::new(BTreeMap::new());

fn counted<T>(py: Python<'_>, label: &'static str, result: PyResult<T>) -> PyResult<T> {
    match &result {
        Ok(_) => {
            if let Ok(mut ran) = RAN.lock() {
                *ran.entry(label).or_insert(0) += 1;
            }
        }
        Err(error) if error.is_instance_of::<Unsupported>(py) => {
            if let Ok(mut refusals) = REFUSALS.lock() {
                *refusals.entry(format!("{label}: {}", error.value(py))).or_insert(0) += 1;
            }
        }
        Err(_) => {}
    }
    result
}

#[pyfunction]
fn stats<'py>(py: Python<'py>) -> PyResult<(Bound<'py, PyDict>, Bound<'py, PyDict>)> {
    let counts = PyDict::new(py);
    if let Ok(ran) = RAN.lock() {
        for (label, times) in ran.iter() {
            counts.set_item(*label, *times)?;
        }
    }
    let reasons = PyDict::new(py);
    if let Ok(refusals) = REFUSALS.lock() {
        for (reason, times) in refusals.iter() {
            reasons.set_item(reason, *times)?;
        }
    }
    Ok((counts, reasons))
}

// ---- Reading ---------------------------------------------------------------------------------------

fn table_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let table = attribute(world, intern!(world.py(), "model_state"))?;
    exact_dict(&table)?;
    Ok(table)
}

/// `world.model_state.get(name)`, the tree's node.
fn state_of<'py>(world: &Bound<'py, PyAny>, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    let table = table_of(world)?;
    exact_dict(&table)?.get_item(name)
}

/// A node of a dict, read as Python's `node[name]`.
fn child<'py>(node: &Bound<'py, PyAny>, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    exact_dict(node)?.get_item(name)
}

fn required<'py>(node: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    child(node, name)?.ok_or_else(|| Unsupported::new_err("the state has no such field"))
}

/// `state.get(name, {})` as a value.
fn part(state: &Bound<'_, PyAny>, name: &str) -> PyResult<V> {
    Ok(match child(state, name)? {
        Some(value) => V::from_py(&value)?,
        None => V::Map(IndexMap::new()),
    })
}

fn map_of(value: &V) -> PyResult<&Doc> {
    match value {
        V::Map(map) => Ok(map),
        _ => unsupported("a value is not a dict"),
    }
}

fn list_of(value: &V) -> PyResult<&[V]> {
    match value {
        V::List(items) => Ok(items),
        _ => unsupported("a value is not a list"),
    }
}

fn int_of(map: &Doc, key: &str) -> PyResult<i64> {
    match map.get(key) {
        Some(V::Int(number)) => Ok(*number),
        _ => unsupported("a value is not a plain int"),
    }
}

fn str_of<'a>(map: &'a Doc, key: &str) -> PyResult<&'a Arc<str>> {
    match map.get(key) {
        Some(V::Str(text)) => Ok(text),
        _ => unsupported("a value is not a str"),
    }
}

/// `row.get(key)` as a truth: a bool or nothing. Other values go to Python.
fn flag_of(map: &Doc, key: &str) -> PyResult<bool> {
    match map.get(key) {
        Some(V::Bool(flag)) => Ok(*flag),
        Some(V::Null) | None => Ok(false),
        _ => unsupported("a flag is not a bool"),
    }
}

fn has_exactly(map: &Doc, names: &[&str]) -> bool {
    map.len() == names.len() && names.iter().all(|name| map.contains_key(*name))
}

/// `tuple(Point2D.model_validate(point) for point in value)`, for a polygon a rule then tests.
fn polygon_of(value: Option<&V>) -> PyResult<Vec<Pt>> {
    let Some(value) = value else {
        return unsupported("no polygon");
    };
    let polygon = points_in_value(value)?;
    if polygon.is_empty() {
        return unsupported("a polygon has no corners");
    }
    Ok(polygon)
}

fn square(value: f64) -> PyResult<f64> {
    pow(value, 2.0).ok_or_else(|| Unsupported::new_err("a square Python decides"))
}

/// `(x - a) ** 2 + (y - b) ** 2`.
fn squared_distance(first: Pt, second: Pt) -> PyResult<f64> {
    Ok(square(first.x - second.x)? + square(first.y - second.y)?)
}

fn dict_of<'py>(py: Python<'py>, items: &[(&str, Bound<'py, PyAny>)]) -> PyResult<Bound<'py, PyAny>> {
    let dict = PyDict::new(py);
    for (key, value) in items {
        dict.set_item(*key, value)?;
    }
    Ok(dict.into_any())
}

fn py_str<'py>(py: Python<'py>, text: &str) -> Bound<'py, PyAny> {
    PyString::new(py, text).into_any()
}

fn py_int<'py>(py: Python<'py>, number: i64) -> PyResult<Bound<'py, PyAny>> {
    Ok(number.into_pyobject(py)?.into_any())
}

fn py_flag<'py>(py: Python<'py>, flag: bool) -> Bound<'py, PyAny> {
    PyBool::new(py, flag).to_owned().into_any()
}

fn point_dict<'py>(py: Python<'py>, point: Pt) -> PyResult<Bound<'py, PyAny>> {
    let dict = PyDict::new(py);
    dict.set_item(intern!(py, "x"), point.x)?;
    dict.set_item(intern!(py, "y"), point.y)?;
    Ok(dict.into_any())
}

fn hex_of(text: &str) -> String {
    let digest = Sha256::digest(text.as_bytes());
    let mut out = String::with_capacity(24);
    for byte in digest.iter().take(12) {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

/// `_bed_target_id`.
fn bed_target_id(room_id: &str, bed_index: i64) -> String {
    format!("bed-{}", hex_of(&format!("{room_id}:{bed_index}")))
}

/// `sorted({...})`.
fn sorted_set<'a>(values: impl IntoIterator<Item = &'a str>) -> Vec<&'a str> {
    let mut out: Vec<&str> = values.into_iter().collect();
    out.sort_unstable_by(|first, second| first.as_bytes().cmp(second.as_bytes()));
    out.dedup();
    out
}

/// `del history[:-limit]`.
fn trim_history(history: &Bound<'_, PyAny>, limit: usize) -> PyResult<()> {
    let py = history.py();
    let count = history.len()?;
    if count > limit {
        history.del_item(PySlice::new(py, 0, (count - limit) as isize, 1))?;
    }
    Ok(())
}

// ---- domain/temporary_spaces.py -----------------------------------------------------------------

/// `register_temporary_space`, on a dict the caller made.
pub(crate) fn register_space(world: &Bound<'_, PyAny>, record: &Bound<'_, PyAny>, event_id: &str) -> PyResult<()> {
    let py = world.py();
    let Some(state) = state_of(world, SPACES_KEY)? else {
        return unsupported("temporary_spaces_v1 is not installed");
    };
    let fields = exact_dict(record)?;
    let Some(space_id) = fields.get_item(intern!(py, "space_id"))? else {
        return unsupported("the record has no space_id");
    };
    let space_text = exact_str(&space_id)?;
    let Some(zone_id) = fields.get_item(intern!(py, "zone_id"))? else {
        return unsupported("the record has no zone_id");
    };
    let spaces = required(&state, "spaces")?;
    let history = required(&state, "history")?;
    if exact_dict(&spaces)?.contains(space_text)? {
        return unsupported("the space's ID already exists");
    }
    if spaces.len()? >= MAX_SPACES {
        return unsupported("the space limit is reached");
    }
    let minute = attribute(world, intern!(py, "minute_index"))?;
    let entry = dict_of(
        py,
        &[
            ("event_id", py_str(py, event_id)),
            ("space_id", space_id.clone()),
            ("zone_id", zone_id),
            ("minute", minute),
            ("transition", py_str(py, "activated")),
            ("causation_id", py.None().into_bound(py)),
        ],
    )?;
    spaces.set_item(&space_id, record)?;
    history.call_method1(intern!(py, "append"), (entry,))?;
    trim_history(&history, MAX_SPACE_HISTORY)
}

/// `expire_temporary_space`.
pub(crate) fn expire_space(world: &Bound<'_, PyAny>, space_id: &str, event_id: &str, causation_id: Option<&str>) -> PyResult<bool> {
    let py = world.py();
    let Some(state) = state_of(world, SPACES_KEY)? else {
        return unsupported("temporary_spaces_v1 is not installed");
    };
    let spaces = required(&state, "spaces")?;
    let Some(record) = child(&spaces, space_id)? else {
        return Ok(false);
    };
    let active = required(&record, "active")?;
    if !active.downcast_exact::<PyBool>().map_err(|_| Unsupported::new_err("not a bool"))?.is_true() {
        return Ok(false);
    }
    let held_id = required(&record, "space_id")?;
    let zone_id = required(&record, "zone_id")?;
    let history = required(&state, "history")?;
    let minute = attribute(world, intern!(py, "minute_index"))?;
    let entry = dict_of(
        py,
        &[
            ("event_id", py_str(py, event_id)),
            ("space_id", held_id),
            ("zone_id", zone_id),
            ("minute", minute),
            ("transition", py_str(py, "expired")),
            ("causation_id", causation_id.map_or_else(|| py.None().into_bound(py), |id| py_str(py, id))),
        ],
    )?;
    record.set_item(intern!(py, "active"), false)?;
    record.set_item(intern!(py, "end_event_id"), event_id)?;
    history.call_method1(intern!(py, "append"), (entry,))?;
    trim_history(&history, MAX_SPACE_HISTORY)?;
    Ok(true)
}

#[pyfunction]
#[pyo3(signature = (world, record, *, event_id))]
fn register_temporary_space(world: &Bound<'_, PyAny>, record: &Bound<'_, PyAny>, event_id: &Bound<'_, PyAny>) -> PyResult<()> {
    counted(world.py(), "register_temporary_space", register_space(world, record, exact_str(event_id)?))
}

#[pyfunction]
#[pyo3(signature = (world, space_id, *, event_id, causation_id))]
fn expire_temporary_space(
    world: &Bound<'_, PyAny>,
    space_id: &Bound<'_, PyAny>,
    event_id: &Bound<'_, PyAny>,
    causation_id: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let cause = if causation_id.is_none() { None } else { Some(exact_str(causation_id)?) };
    counted(world.py(), "expire_temporary_space", expire_space(world, exact_str(space_id)?, exact_str(event_id)?, cause))
}

/// `validate_temporary_spaces_state`: true only for what the Python rule passes.
#[pyfunction]
fn validate_spaces(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    let result = (|| {
        let Some(state) = state_of(world, SPACES_KEY)? else {
            return Ok(true);
        };
        let state = V::from_py(&state)?;
        let state = map_of(&state)?;
        let zones = attribute(world, intern!(py, "zones"))?;
        let zones = exact_dict(&zones)?;
        let (Some(V::Map(spaces)), Some(V::List(history))) = (state.get("spaces"), state.get("history")) else {
            return unsupported("the state has another shape");
        };
        if !has_exactly(state, &["schema_version", "policy_version", "spaces", "history"])
            || state.get("schema_version") != Some(&V::Int(1))
            || str_of(state, "policy_version")?.as_ref() != POLICY_SPACES
            || spaces.len() > MAX_SPACES
            || history.len() > MAX_SPACE_HISTORY
        {
            return unsupported("the state has another shape");
        }
        for (space_id, row) in spaces {
            let row = map_of(row)?;
            let names = [
                "space_id", "zone_id", "people_capacity", "sleeping_capacity", "start_minute", "end_minute", "active",
                "start_event_id", "end_event_id", "program_id", "effect_id", "source_ids", "geometry_status",
            ];
            if !has_exactly(row, &names) || str_of(row, "space_id")? != space_id || !zones.contains(&**str_of(row, "zone_id")?)? {
                return unsupported("a space has another shape");
            }
            let people = int_of(row, "people_capacity")?;
            let sleeping = int_of(row, "sleeping_capacity")?;
            let (start, end) = (int_of(row, "start_minute")?, int_of(row, "end_minute")?);
            let Some(V::Bool(active)) = row.get("active") else {
                return unsupported("a space has another shape");
            };
            let ended = match row.get("end_event_id") {
                Some(V::Null) => false,
                Some(V::Str(id)) => !id.is_empty(),
                _ => return unsupported("a space's end event is another value"),
            };
            if !(1..=1000).contains(&people)
                || !(0..=people).contains(&sleeping)
                || start < 0
                || end <= start
                || str_of(row, "start_event_id")?.is_empty()
                || (*active && ended)
                || (*active && row.get("end_event_id") != Some(&V::Null))
                || (!*active && !ended)
            {
                return unsupported("a space's values are not what the Python rule passes");
            }
            str_of(row, "program_id")?;
            str_of(row, "effect_id")?;
            if !list_of(row.get("source_ids").unwrap_or(&V::Null))?.iter().all(|item| matches!(item, V::Str(id) if !id.is_empty())) {
                return unsupported("a space's source is another value");
            }
            if str_of(row, "geometry_status")?.as_ref() != "zone_only_capacity" {
                return unsupported("a space's status is another value");
            }
        }
        for row in history {
            let row = map_of(row)?;
            if !has_exactly(row, &["event_id", "space_id", "zone_id", "minute", "transition", "causation_id"]) {
                return unsupported("a history record has another shape");
            }
            str_of(row, "event_id")?;
            str_of(row, "space_id")?;
            int_of(row, "minute")?;
            if !zones.contains(&**str_of(row, "zone_id")?)?
                || !matches!(&**str_of(row, "transition")?, "activated" | "expired")
                || !matches!(row.get("causation_id"), Some(V::Null | V::Str(_)))
            {
                return unsupported("a history record has another value");
            }
        }
        Ok(true)
    })();
    counted(py, "validate_spaces", result)
}

struct Capacity {
    effective: i64,
    active_people: i64,
    retained: i64,
    sleeping: i64,
    room_capacity: i64,
    active_admissions: i64,
    retained_rooms: i64,
}

/// `capacity_metrics` of a zone with a capacity.
fn capacity_of(world: &Bound<'_, PyAny>, zone_id: &str, nominal: i64, occupancy: Option<i64>) -> PyResult<Capacity> {
    let py = world.py();
    let current = match occupancy {
        Some(occupancy) => occupancy,
        None => {
            let store = entities_of(world)?;
            EntityStore::with_rows(&store, |rows| Ok(rows.agents.iter().filter(|(_, agent)| &*agent.zone_id == zone_id).count() as i64))?
        }
    };
    let add = |first: i64, second: i64| first.checked_add(second).ok_or_else(|| Unsupported::new_err("a capacity out of range"));
    let (mut active_people, mut expired_people, mut sleeping) = (0, 0, 0);
    let table = table_of(world)?;
    let table = exact_dict(&table)?;
    if let Some(state) = table.get_item(SPACES_KEY)? {
        if let Some(fields) = crate::core::tracked::as_readable_dict(&state) {
            if let Some(spaces) = fields.get_item(intern!(py, "spaces"))? {
                for (_, row) in exact_dict(&spaces)?.iter() {
                    let row = V::from_py(&row)?;
                    let row = map_of(&row)?;
                    if !matches!(row.get("zone_id"), Some(V::Str(zone)) if &**zone == zone_id) {
                        if !matches!(row.get("zone_id"), Some(V::Str(_) | V::Null) | None) {
                            return unsupported("a space's zone is another value");
                        }
                        continue;
                    }
                    let Some(V::Bool(active)) = row.get("active") else {
                        return unsupported("a space's active is not a bool");
                    };
                    let people = int_of(row, "people_capacity")?;
                    if *active {
                        active_people = add(active_people, people)?;
                        sleeping = add(sleeping, int_of(row, "sleeping_capacity")?)?;
                    } else {
                        expired_people = add(expired_people, people)?;
                    }
                }
            }
        } else {
            return unsupported("the spaces' state is not a plain dict");
        }
    }
    let beyond = current.checked_sub(nominal).and_then(|over| over.checked_sub(active_people));
    let retained = expired_people.min(beyond.ok_or_else(|| Unsupported::new_err("a capacity out of range"))?.max(0));
    let effective = add(add(nominal, active_people)?, retained)?;

    let configs = attribute(world, intern!(py, "model_configs"))?;
    let rooms_count = match exact_dict(&configs)?.get_item(intern!(py, "geometry_overlay"))? {
        Some(config) => match crate::core::tracked::as_readable_dict(&config) {
            Some(config) => match config.get_item(intern!(py, "schema_version"))? {
                Some(schema) if schema.is_none() => false,
                Some(schema) => matches!(exact_int(&schema)?, 3 | 4),
                None => false,
            },
            None => false,
        },
        None => false,
    };
    let (mut room_capacity, mut active_admissions, mut expired_capacity, mut expired_admissions) = (0, 0, 0, 0);
    if let Some(overlay) = table.get_item(OVERLAY_KEY)? {
        if crate::core::tracked::as_readable_dict(&overlay).is_none() {
            return unsupported("the overlay is not a plain dict");
        }
        let rooms = part(&overlay, "temporary_rooms")?;
        let rooms = map_of(&rooms)?;
        if !rooms.is_empty() {
            let admissions = part(&overlay, "room_admissions")?;
            let counts = admission_counts(&admissions)?;
            let mut active_ids: HashSet<Arc<str>> = HashSet::new();
            for row in rooms.values() {
                let V::Map(row) = row else {
                    continue;
                };
                if !matches!(row.get("zone_id"), Some(V::Str(zone)) if &**zone == zone_id) {
                    continue;
                }
                let room_id = str_of(row, "room_id")?;
                let held = counts.get(&**room_id).copied().unwrap_or(0);
                let people = int_of(row, "people_capacity")?;
                if flag_of(row, "active")? {
                    if rooms_count {
                        room_capacity = add(room_capacity, people)?;
                    }
                    if active_ids.insert(room_id.clone()) {
                        active_admissions = add(active_admissions, held)?;
                    }
                } else {
                    expired_capacity = add(expired_capacity, people)?;
                    expired_admissions = add(expired_admissions, held)?;
                }
            }
        }
    }
    Ok(Capacity {
        effective,
        active_people,
        retained,
        sleeping,
        room_capacity,
        active_admissions,
        retained_rooms: expired_capacity.min(expired_admissions),
    })
}

/// `zone.capacity`, the zone being one the world has.
fn nominal_of(world: &Bound<'_, PyAny>, zone_id: &Bound<'_, PyAny>) -> PyResult<Option<i64>> {
    let py = world.py();
    let zones = attribute(world, intern!(py, "zones"))?;
    let Some(zone) = raw_dict_get(&zones, zone_id)? else {
        return unsupported("no such zone");
    };
    let capacity = attribute(&zone, intern!(py, "capacity"))?;
    if capacity.is_none() {
        return Ok(None);
    }
    Ok(Some(exact_int(&capacity)?))
}

fn occupancy_of(occupancy: Option<&Bound<'_, PyAny>>) -> PyResult<Option<i64>> {
    match occupancy {
        Some(value) if !value.is_none() => Ok(Some(exact_int(value)?)),
        _ => Ok(None),
    }
}

#[pyfunction]
#[pyo3(name = "capacity_metrics", signature = (world, zone_id, *, occupancy=None))]
fn capacity_metrics_kernel<'py>(world: &Bound<'py, PyAny>, zone_id: &Bound<'py, PyAny>, occupancy: Option<&Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyDict>> {
    let py = world.py();
    let result = (|| {
        let out = PyDict::new(py);
        let Some(nominal) = nominal_of(world, zone_id)? else {
            let none = py.None();
            out.set_item(intern!(py, "nominal_capacity"), &none)?;
            out.set_item(intern!(py, "effective_capacity"), &none)?;
            out.set_item(intern!(py, "temporary_people_capacity"), 0)?;
            out.set_item(intern!(py, "retained_expired_capacity"), 0)?;
            out.set_item(intern!(py, "temporary_sleeping_capacity"), 0)?;
            out.set_item(intern!(py, "physical_room_capacity"), 0)?;
            out.set_item(intern!(py, "active_room_admissions"), 0)?;
            out.set_item(intern!(py, "retained_expired_room_admissions"), 0)?;
            out.set_item(intern!(py, "effective_total_capacity"), &none)?;
            return Ok(out);
        };
        let found = capacity_of(world, exact_str(zone_id)?, nominal, occupancy_of(occupancy)?)?;
        let total = found.effective.checked_add(found.room_capacity).and_then(|sum| sum.checked_add(found.retained_rooms));
        out.set_item(intern!(py, "nominal_capacity"), nominal)?;
        out.set_item(intern!(py, "effective_capacity"), found.effective)?;
        out.set_item(intern!(py, "temporary_people_capacity"), found.active_people)?;
        out.set_item(intern!(py, "retained_expired_capacity"), found.retained)?;
        out.set_item(intern!(py, "temporary_sleeping_capacity"), found.sleeping)?;
        out.set_item(intern!(py, "physical_room_capacity"), found.room_capacity)?;
        out.set_item(intern!(py, "active_room_admissions"), found.active_admissions)?;
        out.set_item(intern!(py, "retained_expired_room_admissions"), found.retained_rooms)?;
        out.set_item(intern!(py, "effective_total_capacity"), total.ok_or_else(|| Unsupported::new_err("a capacity out of range"))?)?;
        Ok(out)
    })();
    counted(py, "capacity_metrics", result)
}

#[pyfunction]
#[pyo3(signature = (world, zone_id, *, occupancy=None))]
fn effective_zone_capacity(world: &Bound<'_, PyAny>, zone_id: &Bound<'_, PyAny>, occupancy: Option<&Bound<'_, PyAny>>) -> PyResult<Option<i64>> {
    let result = (|| {
        let Some(nominal) = nominal_of(world, zone_id)? else {
            return Ok(None);
        };
        Ok(Some(capacity_of(world, exact_str(zone_id)?, nominal, occupancy_of(occupancy)?)?.effective))
    })();
    counted(world.py(), "effective_zone_capacity", result)
}

#[pyfunction]
#[pyo3(signature = (world, zone_id, *, occupancy=None))]
fn effective_total_zone_capacity(world: &Bound<'_, PyAny>, zone_id: &Bound<'_, PyAny>, occupancy: Option<&Bound<'_, PyAny>>) -> PyResult<Option<i64>> {
    let result = (|| {
        let Some(nominal) = nominal_of(world, zone_id)? else {
            return Ok(None);
        };
        let found = capacity_of(world, exact_str(zone_id)?, nominal, occupancy_of(occupancy)?)?;
        let total = found.effective.checked_add(found.room_capacity).and_then(|sum| sum.checked_add(found.retained_rooms));
        total.map(Some).ok_or_else(|| Unsupported::new_err("a capacity out of range"))
    })();
    counted(world.py(), "effective_total_zone_capacity", result)
}

/// `incoming_travelers`.
#[pyfunction]
#[pyo3(signature = (world, zone_id, *, excluding=None))]
fn incoming_travelers(world: &Bound<'_, PyAny>, zone_id: &Bound<'_, PyAny>, excluding: Option<&Bound<'_, PyAny>>) -> PyResult<i64> {
    let result = (|| {
        let zone_id = exact_str(zone_id)?;
        let excluding = match excluding {
            Some(value) if !value.is_none() => Some(exact_str(value)?),
            _ => None,
        };
        let store = entities_of(world)?;
        EntityStore::with_rows(&store, |rows| {
            let mut count = 0;
            for (agent_id, agent) in rows.agents.iter() {
                if excluding == Some(&**agent_id) || agent.activity != crate::state::entities::Activity::Moving {
                    continue;
                }
                let Some(target) = &agent.target_id else {
                    continue;
                };
                let place = rows.facilities.get(target).map_or(target, |facility| &facility.zone_id);
                count += i64::from(&**place == zone_id);
            }
            Ok(count)
        })
    })();
    counted(world.py(), "incoming_travelers", result)
}

// ---- Beds and intake ----------------------------------------------------------------------------

/// `config.get("schema_version")` of the overlay's config, for a dict config.
fn overlay_schema(world: &Bound<'_, PyAny>) -> PyResult<Option<i64>> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let Some(config) = exact_dict(&configs)?.get_item(intern!(py, "geometry_overlay"))? else {
        return Ok(None);
    };
    let Some(config) = crate::core::tracked::as_readable_dict(&config) else {
        return Ok(None);
    };
    match config.get_item(intern!(py, "schema_version"))? {
        Some(schema) if !schema.is_none() => Ok(Some(exact_int(&schema)?)),
        _ => Ok(None),
    }
}

#[pyfunction]
fn release_sleep_bed(world: &Bound<'_, PyAny>, agent_id: &Bound<'_, PyAny>) -> PyResult<bool> {
    counted(world.py(), "release_sleep_bed", release_from(world, "bed_reservations", agent_id))
}

#[pyfunction]
fn release_temporary_room_admission(world: &Bound<'_, PyAny>, agent_id: &Bound<'_, PyAny>) -> PyResult<bool> {
    counted(world.py(), "release_temporary_room_admission", release_from(world, "room_admissions", agent_id))
}

/// `isinstance(part, dict) and part.pop(agent_id, None) is not None`, through the part's own method.
fn release_from(world: &Bound<'_, PyAny>, name: &str, agent_id: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    let Some(state) = state_of(world, OVERLAY_KEY)? else {
        return Ok(false);
    };
    let Some(part) = child(&state, name)? else {
        return Ok(false);
    };
    let held = exact_dict(&part)?;
    exact_str(agent_id)?;
    if !held.contains(agent_id)? {
        return Ok(false);
    }
    Ok(!part.call_method1(intern!(py, "pop"), (agent_id, py.None()))?.is_none())
}

/// `{room_id: count}` of the room admissions, as `available_temporary_room` and `capacity_metrics`.
fn admission_counts(admissions: &V) -> PyResult<HashMap<Arc<str>, i64>> {
    let mut counts = HashMap::new();
    if let V::Map(admissions) = admissions {
        for row in admissions.values() {
            if let V::Map(row) = row {
                if let Some(V::Str(room_id)) = row.get("room_id") {
                    *counts.entry(room_id.clone()).or_insert(0) += 1;
                }
            }
        }
    }
    Ok(counts)
}

fn sorted_rooms(rooms: &Doc) -> Vec<(&Arc<str>, &V)> {
    let mut sorted: Vec<(&Arc<str>, &V)> = rooms.iter().collect();
    sorted.sort_by(|first, second| first.0.as_bytes().cmp(second.0.as_bytes()));
    sorted
}

/// `movement_v1`, if the world has one.
fn movement_mirror<'py>(world: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, NamespaceMirror>>> {
    if state_of(world, MOVEMENT_KEY)?.is_none() {
        return Ok(None);
    }
    Ok(Some(mirror_held(world)?))
}

fn position_of(mirror: &Option<Bound<'_, NamespaceMirror>>, agent_id: &str) -> PyResult<Option<Pt>> {
    let Some(mirror) = mirror else {
        return Ok(None);
    };
    NamespaceMirror::with_row::<MovementRow, _>(mirror, |row| {
        let Some(point) = row.positions.get(agent_id).and_then(|position| position.point) else {
            return Ok(None);
        };
        if !usable(point.x, point.y) {
            return unsupported("a position is not a usable point");
        }
        Ok(Some(Pt { x: point.x, y: point.y }))
    })
}

/// `room_admission_point`.
fn admission_point(world: &Bound<'_, PyAny>, state: &Bound<'_, PyAny>, room_id: &str) -> PyResult<Option<Pt>> {
    let rooms = part(state, "temporary_rooms")?;
    let Some(V::Map(room)) = map_of(&rooms)?.get(room_id) else {
        return if map_of(&rooms)?.contains_key(room_id) { unsupported("a room is another value") } else { Ok(None) };
    };
    let polygon = match room.get("polygon_m") {
        Some(value) => points_in_value(value)?,
        None => Vec::new(),
    };
    if polygon.len() < 3 {
        return Ok(None);
    }
    let lowest = |value: fn(&Pt) -> f64| polygon.iter().map(value).fold(f64::INFINITY, py_min);
    let highest = |value: fn(&Pt) -> f64| polygon.iter().map(value).fold(f64::NEG_INFINITY, py_max);
    let (min_x, max_x, min_y, max_y) = (lowest(|point| point.x), highest(|point| point.x), lowest(|point| point.y), highest(|point| point.y));
    let admissions = part(state, "room_admissions")?;
    let movement = movement_mirror(world)?;
    let mut occupied = Vec::new();
    if let V::Map(admissions) = &admissions {
        for (agent_id, admission) in admissions {
            let V::Map(admission) = admission else {
                continue;
            };
            if !matches!(admission.get("room_id"), Some(V::Str(id)) if &**id == room_id) {
                continue;
            }
            if let Some(point) = position_of(&movement, agent_id)? {
                occupied.push(point);
            }
        }
    }
    let mut candidates = match room.get("bed_points_m") {
        Some(value) => points_in_value(value)?,
        None => Vec::new(),
    };
    let mut inspected = 0;
    let mut x = min_x + ADMISSION_STEP / 2.0;
    while x < max_x && inspected < MAX_ADMISSION_POINTS {
        let mut y = min_y + ADMISSION_STEP / 2.0;
        while y < max_y && inspected < MAX_ADMISSION_POINTS {
            let candidate = Pt { x, y };
            if point_in_polygon(candidate, &polygon) {
                candidates.push(candidate);
            }
            inspected += 1;
            y += ADMISSION_STEP;
        }
        x += ADMISSION_STEP;
    }
    // {(round(x, 8), round(y, 8)): point}: the later point in the earlier place, -0.0 being 0.0.
    let mut places: HashMap<(u64, u64), usize> = HashMap::with_capacity(candidates.len());
    let mut unique: Vec<Pt> = Vec::with_capacity(candidates.len());
    let bits = |value: f64| (value + 0.0).to_bits();
    for point in candidates {
        let not_plain = || Unsupported::new_err("a round Python decides");
        let key = (bits(py_round(point.x, 8).ok_or_else(not_plain)?), bits(py_round(point.y, 8).ok_or_else(not_plain)?));
        match places.get(&key) {
            Some(&place) => unique[place] = point,
            None => {
                places.insert(key, unique.len());
                unique.push(point);
            }
        }
    }
    let count = polygon.len() as f64;
    let center = Pt {
        x: py_sum(&polygon.iter().map(|point| point.x).collect::<Vec<_>>()) / count,
        y: py_sum(&polygon.iter().map(|point| point.y).collect::<Vec<_>>()) / count,
    };
    let mut ordered = Vec::with_capacity(unique.len());
    for point in unique {
        ordered.push((squared_distance(point, center)?, point));
    }
    if ordered.iter().any(|(key, _)| !key.is_finite()) {
        return unsupported("a distance is not finite");
    }
    ordered.sort_by(|first, second| {
        let (a, b) = (first.1, second.1);
        first.0.partial_cmp(&second.0).unwrap_or(std::cmp::Ordering::Equal).then(a.x.partial_cmp(&b.x).unwrap_or(std::cmp::Ordering::Equal)).then(a.y.partial_cmp(&b.y).unwrap_or(std::cmp::Ordering::Equal))
    });
    let radius = square(ADMISSION_RADIUS)?;
    let zone_id = match room.get("zone_id") {
        Some(V::Str(zone_id)) => zone_id.clone(),
        _ => return unsupported("a room's zone is not a str"),
    };
    let geo = Geo::of(world)?;
    for (_, candidate) in ordered.into_iter().take(MAX_ADMISSION_POINTS) {
        let mut taken = false;
        for other in &occupied {
            if squared_distance(candidate, *other)? < radius {
                taken = true;
                break;
            }
        }
        if taken {
            continue;
        }
        if geo.room_route(&zone_id, Some(candidate), candidate, "independent").map_err(crate::rules::movement::refused)?.is_some() {
            return Ok(Some(candidate));
        }
    }
    Ok(None)
}

#[pyfunction]
fn room_admission_point<'py>(world: &Bound<'py, PyAny>, room_id: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let result = (|| {
        let room_id = exact_str(room_id)?;
        let Some(state) = state_of(world, OVERLAY_KEY)? else {
            return Ok(py.None().into_bound(py));
        };
        exact_dict(&state)?;
        match admission_point(world, &state, room_id)? {
            Some(point) => point_object(py, point),
            None => Ok(py.None().into_bound(py)),
        }
    })();
    counted(py, "room_admission_point", result)
}

/// `available_temporary_room`.
#[pyfunction]
fn available_temporary_room(world: &Bound<'_, PyAny>, zone_id: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    let result = (|| {
        let zone_id = exact_str(zone_id)?;
        if !matches!(overlay_schema(world)?, Some(3 | 4)) {
            return Ok(None);
        }
        let Some(state) = state_of(world, OVERLAY_KEY)? else {
            return Ok(None);
        };
        exact_dict(&state)?;
        let admissions = part(&state, "room_admissions")?;
        let counts = admission_counts(&admissions)?;
        let rooms = part(&state, "temporary_rooms")?;
        for (room_id, room) in sorted_rooms(map_of(&rooms)?) {
            let room = map_of(room)?;
            if !matches!(room.get("active"), Some(V::Bool(true))) || !matches!(room.get("zone_id"), Some(V::Str(zone)) if &**zone == zone_id) {
                continue;
            }
            let capacity = match room.get("people_capacity") {
                Some(V::Int(capacity)) => *capacity,
                None => 0,
                _ => return unsupported("a room's capacity is not a plain int"),
            };
            if counts.get(room_id).copied().unwrap_or(0) < capacity && admission_point(world, &state, room_id)?.is_some() {
                return Ok(Some(room_id.to_string()));
            }
        }
        Ok(None)
    })();
    counted(world.py(), "available_temporary_room", result)
}

/// `admit_to_temporary_room`.
#[pyfunction]
#[pyo3(signature = (world, *, agent_id, room_id, minute, event_id))]
fn admit_to_temporary_room<'py>(
    world: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    room_id: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    event_id: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let result = (|| {
        let (agent_text, room_text) = (exact_str(agent_id)?, exact_str(room_id)?);
        let minute = exact_int(minute)?;
        exact_str(event_id)?;
        let Some(state) = state_of(world, OVERLAY_KEY)? else {
            return unsupported("no overlay");
        };
        let admissions = required(&state, "room_admissions")?;
        exact_dict(&admissions)?;
        let rooms = part(&state, "temporary_rooms")?;
        let Some(V::Map(room)) = map_of(&rooms)?.get(room_text) else {
            return unsupported("no such room");
        };
        let agents = attribute(world, intern!(py, "agents"))?;
        if !matches!(room.get("active"), Some(V::Bool(true))) || minute < 0 || raw_dict_get(&agents, agent_id)?.is_none() {
            return unsupported("the admission is not available");
        }
        let Some(point) = admission_point(world, &state, room_text)? else {
            return unsupported("the room has no point");
        };
        let sources = list_of(room.get("source_ids").unwrap_or(&V::Null))?;
        let mut names = vec![INTAKE_SOURCE];
        for source in sources {
            let V::Str(source) = source else {
                return unsupported("a source is not a str");
            };
            names.push(source);
        }
        let record = dict_of(
            py,
            &[
                ("agent_id", py_str(py, agent_text)),
                ("room_id", py_str(py, room_text)),
                ("admitted_minute", py_int(py, minute)?),
                ("event_id", event_id.clone()),
                ("source_ids", PyList::new(py, sorted_set(names))?.into_any()),
            ],
        )?;
        let point = point_object(py, point)?;
        admissions.set_item(agent_id, record)?;
        Ok(point)
    })();
    counted(py, "admit_to_temporary_room", result)
}

/// What `reserve_sleep_bed` finds, written or not.
pub(crate) struct Bed {
    pub(crate) room_id: Arc<str>,
    pub(crate) bed_index: i64,
    pub(crate) target_id: String,
    pub(crate) reserved_minute: i64,
    pub(crate) point: Pt,
    pub(crate) path: Arc<[Pt]>,
    pub(crate) travel_minutes: i64,
    /// A reservation to make: the person has none.
    pub(crate) is_new: bool,
}

/// `reserve_sleep_bed`, changing nothing: None for no bed.
pub(crate) fn plan_bed(world: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<Option<Bed>> {
    let py = world.py();
    let Some(state) = state_of(world, OVERLAY_KEY)? else {
        return Ok(None);
    };
    exact_dict(&state)?;
    if !matches!(overlay_schema(world)?, Some(2..=4)) {
        return Ok(None);
    }
    let store = entities_of(world)?;
    let known = EntityStore::with_rows(&store, |rows| Ok(rows.agents.get(agent_id).map(|agent| agent.zone_id.clone())))?;
    let Some(zone_id) = known else {
        return Ok(None);
    };
    let Some(reservations) = child(&state, "bed_reservations")? else {
        return unsupported("the reservations are still to be made");
    };
    let geo = Geo::of(world)?;
    if geo.layout.is_none() {
        return Ok(None);
    }
    let movement = movement_mirror(world)?;
    let Some(mirror) = &movement else {
        return unsupported("a bed's positions without the movement state");
    };
    let rooms_node = child(&state, "temporary_rooms")?;
    let rooms_empty = match &rooms_node {
        Some(rooms) => exact_dict(rooms)?.is_empty(),
        None => true,
    };
    let start = position_of(&movement, agent_id)?;
    // Each person in the zone: a cell of the grid the size of a bed's radius.
    let mut by_cell: HashMap<(i64, i64), Vec<Pt>> = HashMap::new();
    NamespaceMirror::with_row::<MovementRow, _>(mirror, |row| {
        EntityStore::with_rows(&store, |rows| {
            for (other_id, other) in rows.agents.iter() {
                if &**other_id == agent_id || other.zone_id != zone_id {
                    continue;
                }
                let Some(point) = row.positions.get(&**other_id).and_then(|position| position.point) else {
                    continue;
                };
                if !usable(point.x, point.y) {
                    return unsupported("a position is not a usable point");
                }
                if !rooms_empty {
                    by_cell.entry(cell_of(Pt { x: point.x, y: point.y })?).or_default().push(Pt { x: point.x, y: point.y });
                }
            }
            Ok(())
        })
    })?;
    if rooms_empty {
        return Ok(None);
    }
    let reserved = V::from_py(&reservations)?;
    let reserved = map_of(&reserved)?;
    let mut occupied: HashSet<(Arc<str>, i64)> = HashSet::new();
    for row in reserved.values() {
        if let V::Map(row) = row {
            occupied.insert((str_of(row, "room_id")?.clone(), int_of(row, "bed_index")?));
        }
    }
    let existing = match reserved.get(agent_id) {
        None | Some(V::Null) => None,
        Some(V::Map(row)) => Some(row),
        _ => return unsupported("a reservation is another value"),
    };
    let held_room = existing.map(|row| str_of(row, "room_id")).transpose()?;
    let held_index = existing.map(|row| int_of(row, "bed_index")).transpose()?;
    let rooms = V::from_py(rooms_node.as_ref().ok_or_else(|| Unsupported::new_err("no rooms"))?)?;
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let raw_model = exact_dict(&configs)?.get_item(intern!(py, "movement_model"))?;
    let radius = square(BED_RADIUS)?;
    let mut attempts = 0;
    let mut best: Option<(i64, Arc<str>, i64, Pt, Arc<[Pt]>)> = None;
    let mut consider = |travel: i64, room_id: &Arc<str>, index: i64, bed: Pt, path: Arc<[Pt]>| {
        let better = best.as_ref().is_none_or(|(known, known_room, known_index, _, _)| {
            (travel, room_id.as_bytes(), index) < (*known, known_room.as_bytes(), *known_index)
        });
        if better {
            best = Some((travel, room_id.clone(), index, bed, path));
        }
    };
    let (mobility, agent_zone) = EntityStore::with_rows(&store, |rows| {
        let Some(agent) = rows.agents.get(agent_id) else {
            return unsupported("no such agent");
        };
        Ok((mobility_text(agent), agent.zone_id.clone()))
    })?;
    'rooms: for (room_id, room) in sorted_rooms(map_of(&rooms)?) {
        if attempts >= MAX_BED_ROUTES {
            break;
        }
        let room = map_of(room)?;
        if !matches!(room.get("zone_id"), Some(V::Str(zone)) if *zone == zone_id) {
            continue;
        }
        if existing.is_none() && !flag_of(room, "active")? {
            continue;
        }
        if held_room.is_some_and(|held| held != room_id) {
            continue;
        }
        let beds = match room.get("bed_points_m") {
            Some(beds) => list_of(beds)?,
            None => &[],
        };
        for (index, raw_bed) in beds.iter().enumerate() {
            let index = index as i64;
            if attempts >= MAX_BED_ROUTES {
                break 'rooms;
            }
            if existing.is_none() && occupied.contains(&(room_id.clone(), index)) {
                continue;
            }
            if held_index.is_some_and(|held| held != index) {
                continue;
            }
            let bed = point_in_value(raw_bed)?;
            let cell = cell_of(bed)?;
            let mut used = false;
            'cells: for dx in [-1, 0, 1] {
                for dy in [-1, 0, 1] {
                    for point in by_cell.get(&(cell.0 + dx, cell.1 + dy)).map_or(&[][..], Vec::as_slice) {
                        if squared_distance(*point, bed)? <= radius {
                            used = true;
                            break 'cells;
                        }
                    }
                }
            }
            if used {
                continue;
            }
            attempts += 1;
            let Some(path) = geo.room_route(&zone_id, start, bed, mobility).map_err(crate::rules::movement::refused)? else {
                continue;
            };
            let mut lengths = Vec::with_capacity(path.len());
            for pair in path.windows(2) {
                let length = pow(squared_distance(pair[1], pair[0])?, 0.5).ok_or_else(|| Unsupported::new_err("a root Python decides"))?;
                lengths.push(length);
            }
            let travel = if py_sum(&lengths) <= 1e-8 {
                0
            } else if path.len() < 2 {
                0
            } else {
                let length = polyline_length(&path).ok_or_else(|| Unsupported::new_err("not a plain sum"))?;
                EntityStore::with_rows(&store, |rows| room_minutes(raw_model.clone(), &geo, rows, &agent_zone, mobility, length))?
            };
            consider(travel, room_id, index, bed, path);
            if attempts >= MAX_BED_ROUTES {
                break 'rooms;
            }
        }
    }
    let Some((travel_minutes, room_id, bed_index, point, path)) = best else {
        return Ok(None);
    };
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let reserved_minute = match existing {
        Some(row) => int_of(row, "reserved_minute")?,
        None => minute,
    };
    let target_id = bed_target_id(&room_id, bed_index);
    Ok(Some(Bed { room_id, bed_index, target_id, reserved_minute, point, path, travel_minutes, is_new: existing.is_none() }))
}

/// `(math.floor(x / 0.75), math.floor(y / 0.75))`.
fn cell_of(point: Pt) -> PyResult<(i64, i64)> {
    let (x, y) = ((point.x / BED_RADIUS).floor(), (point.y / BED_RADIUS).floor());
    const LIMIT: f64 = 4_503_599_627_370_496.0;
    if x.abs() >= LIMIT || y.abs() >= LIMIT {
        return unsupported("a cell of the grid is too far");
    }
    Ok((x as i64, y as i64))
}

/// The reservation of `plan_bed`, when it is new.
pub(crate) fn write_bed(world: &Bound<'_, PyAny>, agent_id: &str, bed: &Bed) -> PyResult<()> {
    if !bed.is_new {
        return Ok(());
    }
    let py = world.py();
    let state = state_of(world, OVERLAY_KEY)?.ok_or_else(|| Unsupported::new_err("no overlay"))?;
    let reservations = required(&state, "bed_reservations")?;
    let record = dict_of(
        py,
        &[
            ("agent_id", py_str(py, agent_id)),
            ("room_id", py_str(py, &bed.room_id)),
            ("bed_index", py_int(py, bed.bed_index)?),
            ("target_id", py_str(py, &bed.target_id)),
            ("reserved_minute", py_int(py, bed.reserved_minute)?),
        ],
    )?;
    reservations.set_item(agent_id, record)
}

#[pyfunction]
fn reserve_sleep_bed<'py>(world: &Bound<'py, PyAny>, agent_id: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let result = (|| {
        let agent_text = exact_str(agent_id)?;
        let Some(bed) = plan_bed(world, agent_text)? else {
            return Ok(py.None().into_bound(py));
        };
        let route = bed.path.iter().map(|point| point_object(py, *point)).collect::<PyResult<Vec<_>>>()?;
        let answer = dict_of(
            py,
            &[
                ("agent_id", agent_id.clone()),
                ("room_id", py_str(py, &bed.room_id)),
                ("bed_index", py_int(py, bed.bed_index)?),
                ("target_id", py_str(py, &bed.target_id)),
                ("reserved_minute", py_int(py, bed.reserved_minute)?),
                ("point", point_object(py, bed.point)?),
                ("route_points", PyTuple::new(py, route)?.into_any()),
                ("travel_minutes", py_int(py, bed.travel_minutes)?),
            ],
        )?;
        write_bed(world, agent_text, &bed)?;
        Ok(answer)
    })();
    counted(py, "reserve_sleep_bed", result)
}

// ---- Facilities installed during a Run ----------------------------------------------------------

/// `installed_facility_placement_enabled`.
pub(crate) fn placement_enabled(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    if overlay_schema(world)? != Some(4) {
        return Ok(false);
    }
    Ok(state_of(world, OVERLAY_KEY)?.is_some_and(|state| crate::core::tracked::as_readable_dict(&state).is_some()))
}

#[pyfunction]
fn installed_facility_placement_enabled(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    counted(world.py(), "installed_facility_placement_enabled", placement_enabled(world))
}

/// `facility_grid_point`.
fn grid_point(polygon: &[Pt], index: usize, count: usize) -> Pt {
    let min_x = polygon.iter().map(|point| point.x).fold(f64::INFINITY, py_min);
    let max_x = polygon.iter().map(|point| point.x).fold(f64::NEG_INFINITY, py_max);
    let min_y = polygon.iter().map(|point| point.y).fold(f64::INFINITY, py_min);
    let max_y = polygon.iter().map(|point| point.y).fold(f64::NEG_INFINITY, py_max);
    let columns = (count as f64).sqrt().ceil() as usize;
    let rows = (count as f64 / columns as f64).ceil() as usize;
    let (column, row) = (index % columns, index / columns);
    let x_fraction = if count == 1 { 0.35 } else { (column + 1) as f64 / (columns + 1) as f64 };
    let y_fraction = 0.68 + 0.28 * (row + 1) as f64 / (rows + 1) as f64;
    Pt { x: min_x + (max_x - min_x) * x_fraction, y: min_y + (max_y - min_y) * y_fraction }
}

/// `installed_facility_point`.
fn installed_point(world: &Bound<'_, PyAny>, facility_id: &str) -> PyResult<Option<Pt>> {
    let store = entities_of(world)?;
    let geo = Geo::of(world)?;
    let Some(layout) = &geo.layout else {
        return Ok(None);
    };
    let found = EntityStore::with_rows(&store, |rows| {
        let Some(facility) = rows.facilities.get(facility_id) else {
            return Ok(None);
        };
        let zone_id = facility.zone_id.clone();
        let mut others = Vec::new();
        let mut ids: Vec<&Arc<str>> = rows.facilities.iter().map(|(id, _)| id).collect();
        ids.sort_unstable_by(|first, second| first.as_bytes().cmp(second.as_bytes()));
        for id in ids {
            if &**id == facility_id || rows.facilities.get(id).is_none_or(|item| item.zone_id != zone_id) {
                continue;
            }
            if let Some(point) = layout.facility_points.get(&**id) {
                others.push(*point);
            }
        }
        Ok(Some((zone_id, others)))
    })?;
    let Some((zone_id, others)) = found else {
        return Ok(None);
    };
    let Some(zone) = layout.zones.get(&*zone_id) else {
        return Ok(None);
    };
    let mut blocked: Vec<Vec<Pt>> = geo.constraints.as_ref().map_or_else(Vec::new, |constraints| constraints.blocked.clone());
    if let Some(state) = state_of(world, OVERLAY_KEY)? {
        let rooms = part(&state, "temporary_rooms")?;
        for room in map_of(&rooms)?.values() {
            let room = map_of(room)?;
            if flag_of(room, "active")? {
                blocked.push(polygon_of(room.get("polygon_m"))?);
            }
        }
    }
    for count in others.len() + 1..others.len() + 1 + GRID_GROWTH {
        for index in 0..count {
            let point = grid_point(&zone.polygon, index, count);
            if !usable(point.x, point.y) {
                return unsupported("a grid point is not a usable point");
            }
            if point_walkable(point, &zone.polygon, &blocked)
                && others.iter().all(|other| hypot(&[point.x - other.x, point.y - other.y]) >= MIN_SEPARATION)
                && geo.room_route(&zone_id, None, point, "independent").map_err(crate::rules::movement::refused)?.is_some()
            {
                return Ok(Some(point));
            }
        }
    }
    Ok(None)
}

/// `place_installed_facility`.
pub(crate) fn place_facility(world: &Bound<'_, PyAny>, facility_id: &str, event_id: &str, source_ids: &[&str]) -> PyResult<Pt> {
    let py = world.py();
    let Some(point) = installed_point(world, facility_id)? else {
        return unsupported("no free floor-plan point");
    };
    let state = state_of(world, OVERLAY_KEY)?.ok_or_else(|| Unsupported::new_err("no overlay"))?;
    let points = required(&state, "facility_points")?;
    let history = required(&state, "history")?;
    let minute = attribute(world, intern!(py, "minute_index"))?;
    let names = sorted_set(source_ids.iter().copied().chain([PLACEMENT_SOURCE]));
    let sources = || PyList::new(py, names.iter().copied()).map(Bound::into_any);
    let record = dict_of(
        py,
        &[
            ("point_m", point_dict(py, point)?),
            ("event_id", py_str(py, event_id)),
            ("source_ids", sources()?),
            ("minute", minute.clone()),
        ],
    )?;
    let transition = format!("install-{}", hex_of(&format!("{event_id}:{facility_id}")));
    let entry = dict_of(
        py,
        &[
            ("transition_id", py_str(py, &transition)),
            ("op", py_str(py, "install_facility")),
            ("target_id", py_str(py, facility_id)),
            ("status", py_str(py, "applied")),
            ("causation_id", py_str(py, event_id)),
            ("event_id", py_str(py, event_id)),
            ("source_ids", sources()?),
            ("minute", minute),
        ],
    )?;
    points.set_item(facility_id, record)?;
    history.call_method1(intern!(py, "append"), (entry,))?;
    trim_history(&history, MAX_OVERLAY_HISTORY)?;
    Ok(point)
}

#[pyfunction]
#[pyo3(signature = (world, facility_id))]
fn installed_facility_point<'py>(world: &Bound<'py, PyAny>, facility_id: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let result = (|| match installed_point(world, exact_str(facility_id)?)? {
        Some(point) => point_object(py, point),
        None => Ok(py.None().into_bound(py)),
    })();
    counted(py, "installed_facility_point", result)
}

#[pyfunction]
#[pyo3(signature = (world, facility_id, *, event_id, source_ids))]
fn place_installed_facility<'py>(
    world: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    event_id: &Bound<'py, PyAny>,
    source_ids: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let result = (|| {
        let tuple = source_ids.downcast_exact::<PyTuple>().map_err(|_| Unsupported::new_err("the sources are not a tuple"))?;
        let held: Vec<Bound<'_, PyAny>> = tuple.iter().collect();
        let names = held.iter().map(|item| exact_str(item)).collect::<PyResult<Vec<&str>>>()?;
        let point = place_facility(world, exact_str(facility_id)?, exact_str(event_id)?, &names)?;
        point_object(py, point)
    })();
    counted(py, "place_installed_facility", result)
}

// ---- The geometry the checks of a change read: region/facility.py --------------------------------

fn sides(polygon: &[Pt]) -> impl Iterator<Item = (Pt, Pt)> + '_ {
    polygon.iter().copied().zip(polygon.iter().copied().skip(1).chain(polygon.first().copied()))
}

/// `_point_on_segment`.
fn point_on_segment(point: Pt, start: Pt, end: Pt) -> bool {
    let cross = (point.x - start.x) * (end.y - start.y) - (point.y - start.y) * (end.x - start.x);
    let within = py_min(start.x, end.x) - 1e-8 <= point.x
        && point.x <= py_max(start.x, end.x) + 1e-8
        && py_min(start.y, end.y) - 1e-8 <= point.y
        && point.y <= py_max(start.y, end.y) + 1e-8;
    cross.abs() <= 1e-8 && within
}

/// Whether `_segment_intersection_points` finds any.
fn segments_meet(first_start: Pt, first_end: Pt, second_start: Pt, second_end: Pt) -> bool {
    let cross = |ax: f64, ay: f64, bx: f64, by: f64| ax * by - ay * bx;
    let (rx, ry) = (first_end.x - first_start.x, first_end.y - first_start.y);
    let (sx, sy) = (second_end.x - second_start.x, second_end.y - second_start.y);
    let denominator = cross(rx, ry, sx, sy);
    let (qpx, qpy) = (second_start.x - first_start.x, second_start.y - first_start.y);
    if denominator.abs() <= 1e-9 {
        if cross(qpx, qpy, rx, ry).abs() > 1e-9 {
            return false;
        }
        return [first_start, first_end, second_start, second_end]
            .iter()
            .any(|point| point_on_segment(*point, first_start, first_end) && point_on_segment(*point, second_start, second_end));
    }
    let t = cross(qpx, qpy, sx, sy) / denominator;
    let u = cross(qpx, qpy, rx, ry) / denominator;
    (-1e-9..=1.0 + 1e-9).contains(&t) && (-1e-9..=1.0 + 1e-9).contains(&u)
}

/// `_point_segment_distance` of region/facility.py: its clamp is the other way round.
fn region_point_distance(point: Pt, start: Pt, end: Pt) -> f64 {
    let (dx, dy) = (end.x - start.x, end.y - start.y);
    let length_squared = dx * dx + dy * dy;
    if length_squared <= 1e-18 {
        return hypot(&[point.x - start.x, point.y - start.y]);
    }
    let fraction = py_max(0.0, py_min(1.0, ((point.x - start.x) * dx + (point.y - start.y) * dy) / length_squared));
    hypot(&[point.x - (start.x + fraction * dx), point.y - (start.y + fraction * dy)])
}

/// `_segment_distance`.
fn segment_distance(start: Pt, end: Pt, other_start: Pt, other_end: Pt) -> f64 {
    if segments_meet(start, end, other_start, other_end) {
        return 0.0;
    }
    let distances = [
        region_point_distance(start, other_start, other_end),
        region_point_distance(end, other_start, other_end),
        region_point_distance(other_start, start, end),
        region_point_distance(other_end, start, end),
    ];
    distances.into_iter().fold(f64::INFINITY, py_min)
}

/// `_segment_distance_to_polygon`.
fn segment_to_polygon(start: Pt, end: Pt, polygon: &[Pt]) -> f64 {
    if point_in_polygon(start, polygon) || point_in_polygon(end, polygon) {
        return 0.0;
    }
    sides(polygon).map(|(first, second)| segment_distance(start, end, first, second)).fold(f64::INFINITY, py_min)
}

/// `_valid_simple_polygon`.
fn valid_simple(polygon: &[Pt]) -> bool {
    if polygon.len() < 3 {
        return false;
    }
    let crosses: Vec<f64> = sides(polygon).map(|(a, b)| a.x * b.y - b.x * a.y).collect();
    if py_sum(&crosses).abs() <= 1e-9 {
        return false;
    }
    let edges: Vec<(Pt, Pt)> = sides(polygon).collect();
    let count = edges.len();
    for (index, &(start, end)) in edges.iter().enumerate() {
        for (other_index, &(other_start, other_end)) in edges.iter().enumerate().skip(index + 1) {
            if other_index == index + 1 || other_index == (index + count - 1) % count {
                continue;
            }
            if segments_meet(start, end, other_start, other_end) {
                return false;
            }
        }
    }
    true
}

/// `_polygons_overlap` of the overlay.
fn polygons_overlap(left: &[Pt], right: &[Pt]) -> bool {
    if left.iter().any(|point| point_in_polygon(*point, right)) || right.iter().any(|point| point_in_polygon(*point, left)) {
        return true;
    }
    sides(left).any(|(a, b)| sides(right).any(|(c, d)| segments_meet(a, b, c, d)))
}

/// `_validate_partition_polygon`, `_validate_room_polygon`: whether the Python rule passes.
fn inside_zone(zone: &[Pt], polygon: &[Pt]) -> bool {
    valid_simple(polygon)
        && polygon.iter().all(|point| point_in_polygon(*point, zone))
        && sides(polygon).all(|(a, b)| segment_inside_polygon(a, b, zone))
}

// ---- validate_geometry_change -------------------------------------------------------------------

const REFUSED: &str = "the Python rule refuses the change";

fn refuse<T>() -> PyResult<T> {
    unsupported(REFUSED)
}

fn model_of<'py>(py: Python<'py>, module: &str, name: &str, raw: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    py.import(module)?
        .getattr(name)?
        .call_method1(intern!(py, "model_validate"), (raw,))
        .map_err(|_| Unsupported::new_err("the contract refuses the parameters"))
}

const OVERLAY_MODULE: &str = "sheltersim.domain.geometry_overlay";
const OPERATIONS_MODULE: &str = "sheltersim.contracts.operations";

fn point_attr(model: &Bound<'_, PyAny>, name: &Bound<'_, PyString>) -> PyResult<Pt> {
    let py = model.py();
    let point = attribute(model, name)?;
    let (x, y) = (exact_float(&attribute(&point, intern!(py, "x"))?)?, exact_float(&attribute(&point, intern!(py, "y"))?)?);
    if !usable(x, y) {
        return unsupported("a point is not a usable point");
    }
    Ok(Pt { x, y })
}

fn points_attr(model: &Bound<'_, PyAny>, name: &Bound<'_, PyString>) -> PyResult<Vec<Pt>> {
    let py = model.py();
    let points = attribute(model, name)?;
    let points = points.downcast_exact::<PyTuple>().map_err(|_| Unsupported::new_err("not a tuple"))?;
    points
        .iter()
        .map(|point| {
            let (x, y) = (exact_float(&attribute(&point, intern!(py, "x"))?)?, exact_float(&attribute(&point, intern!(py, "y"))?)?);
            if !usable(x, y) {
                return unsupported("a point is not a usable point");
            }
            Ok(Pt { x, y })
        })
        .collect()
}

fn text_attr(model: &Bound<'_, PyAny>, name: &Bound<'_, PyString>) -> PyResult<Arc<str>> {
    Ok(Arc::from(exact_str(&attribute(model, name)?)?))
}

/// A route of `world.zone_edges`.
struct Edge {
    from: Arc<str>,
    to: Arc<str>,
    closed: bool,
}

fn edges_of(world: &Bound<'_, PyAny>) -> PyResult<HashMap<Arc<str>, Edge>> {
    let py = world.py();
    let edges = attribute(world, intern!(py, "zone_edges"))?;
    let mut found = HashMap::new();
    for (id, edge) in exact_dict(&edges)?.iter() {
        found.insert(
            Arc::from(exact_str(&id)?),
            Edge {
                from: text_attr(&edge, intern!(py, "from_zone_id"))?,
                to: text_attr(&edge, intern!(py, "to_zone_id"))?,
                closed: attribute(&edge, intern!(py, "closed"))?.downcast_exact::<PyBool>().map_err(|_| Unsupported::new_err("not a bool"))?.is_true(),
            },
        );
    }
    Ok(found)
}

fn constraints_of_plan(geo: &Geo) -> PyResult<&Constraints> {
    geo.constraints.as_ref().ok_or_else(|| Unsupported::new_err("no constraints"))
}

fn layout_of_plan(geo: &Geo) -> PyResult<&Layout> {
    geo.layout.as_ref().ok_or_else(|| Unsupported::new_err("no floor plan"))
}

fn walls_are_near(constraints: &Constraints, point: Pt) -> bool {
    constraints.walls.iter().any(|wall| point_segment_distance(point, wall.start, wall.end) < WALL_CLEARANCE)
}

fn walkable_at(geo: &Geo, zone_id: &str, point: Pt) -> PyResult<bool> {
    Ok(geo.room_route(zone_id, Some(point), point, "independent").map_err(crate::rules::movement::refused)?.is_some())
}

/// `_validate_facility_room_clearance`.
fn clear_of_rooms(geo: &Geo, rooms: &Doc, facility_id: &str, zone_id: &str, point: Pt) -> PyResult<()> {
    let layout = layout_of_plan(geo)?;
    let constraints = constraints_of_plan(geo)?;
    let Some(&old) = layout.facility_points.get(facility_id) else {
        return refuse();
    };
    let (dx, dy) = (point.x - old.x, point.y - old.y);
    for room in rooms.values() {
        let room = map_of(room)?;
        if !flag_of(room, "active")? || !matches!(room.get("zone_id"), Some(V::Str(zone)) if &**zone == zone_id) {
            continue;
        }
        let polygon = polygon_of(room.get("polygon_m"))?;
        if point_in_polygon(point, &polygon) {
            return refuse();
        }
        for service in &layout.service_points {
            if &*service.facility_id == facility_id && point_in_polygon(Pt { x: service.point.x + dx, y: service.point.y + dy }, &polygon) {
                return refuse();
            }
        }
        if let Some((_, footprint)) = constraints.footprints.iter().find(|(id, _)| &**id == facility_id) {
            let shifted: Vec<Pt> = footprint.iter().map(|item| Pt { x: item.x + dx, y: item.y + dy }).collect();
            if polygons_overlap(&shifted, &polygon) {
                return refuse();
            }
        }
    }
    Ok(())
}

/// A facility with people or a staff task: `facility_in_use`.
fn facility_is_free(world: &Bound<'_, PyAny>, target: &str) -> PyResult<(Arc<str>, Arc<str>)> {
    let store = entities_of(world)?;
    let found = EntityStore::with_rows(&store, |rows| {
        let Some(facility) = rows.facilities.get(target) else {
            return refuse();
        };
        if !facility.queue_agent_ids.is_empty()
            || !facility.active_agent_ids.is_empty()
            || !facility.resource_queue_agent_ids.is_empty()
            || !facility.resource_active_agent_ids.is_empty()
        {
            return refuse();
        }
        Ok((facility.zone_id.clone(), facility.kind.clone()))
    })?;
    if let Some(operations) = state_of(world, "staff_operations_v1")? {
        if let Some(staff) = child(&operations, "staff")? {
            for (_, row) in exact_dict(&staff)?.iter() {
                if let Some(row) = crate::core::tracked::as_readable_dict(&row) {
                    if let Some(other) = row.get_item(intern!(world.py(), "target_id"))? {
                        if other.downcast_exact::<PyString>().is_ok_and(|other| other.to_str().is_ok_and(|other| other == target)) {
                            return refuse();
                        }
                        if !other.is_none() && other.downcast_exact::<PyString>().is_err() {
                            return unsupported("a task's target is another value");
                        }
                    }
                }
            }
        }
    }
    Ok(found)
}

/// `_zones_are_connected`.
fn zones_connected(edges: &HashMap<Arc<str>, Edge>, source: &str, destination: &str) -> bool {
    if source == destination {
        return false;
    }
    let mut next: HashMap<&str, Vec<&str>> = HashMap::new();
    for edge in edges.values() {
        if !edge.closed {
            next.entry(&edge.from).or_default().push(&edge.to);
        }
    }
    let mut pending = vec![source];
    let mut visited = HashSet::from([source]);
    while let Some(zone) = pending.pop() {
        for other in next.get(zone).map_or(&[][..], Vec::as_slice) {
            if *other == destination {
                return true;
            }
            if visited.insert(other) {
                pending.push(other);
            }
        }
    }
    false
}

/// `_require_service_staff_in_zone`.
fn service_staff_in_zone(world: &Bound<'_, PyAny>, geo: &Geo, facility_id: &str, kind: &str, zone_id: &str) -> PyResult<()> {
    let py = world.py();
    let skill_key = match kind {
        "water_station" => "water_staff_skill",
        "meal_distribution" => "meal_staff_skill",
        _ => return Ok(()),
    };
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let Some(config) = exact_dict(&configs)?.get_item(intern!(py, "resource_service"))? else {
        return Ok(());
    };
    let Some(config) = crate::core::tracked::as_readable_dict(&config) else {
        return unsupported("the service config is another mapping");
    };
    if state_of(world, "resource_service_v1")?.is_none() {
        return Ok(());
    }
    let Some(skill) = config.get_item(skill_key)? else {
        return Ok(());
    };
    let Ok(skill) = skill.downcast_exact::<PyString>() else {
        return Ok(());
    };
    let mut wanted: Vec<Arc<str>> = vec![Arc::from(skill.to_str()?)];
    for point in &layout_of_plan(geo)?.service_points {
        if &*point.facility_id == facility_id {
            wanted.extend(point.required_staff_skills.iter().cloned());
        }
    }
    let store = entities_of(world)?;
    let staffed = EntityStore::with_rows(&store, |rows| {
        Ok(rows.staff.iter().any(|(_, staff)| &*staff.zone_id == zone_id && wanted.iter().all(|name| staff.skills.0.contains(name))))
    })?;
    if staffed {
        Ok(())
    } else {
        refuse()
    }
}

/// The footprint a move would give: inside the zone, past no wall, overlapping nothing.
fn footprint_fits(constraints: &Constraints, facility_id: &str, zone: &[Pt], dx_from: Pt, to: Pt, transfer: bool) -> PyResult<()> {
    let Some((_, footprint)) = constraints.footprints.iter().find(|(id, _)| &**id == facility_id) else {
        return Ok(());
    };
    // `item.facility_id` of an obstacle raises AttributeError in the transfer's Python rule.
    if transfer && constraints.obstacle_count > 0 {
        return unsupported("the Python rule raises on an obstacle");
    }
    let shifted: Vec<Pt> = footprint
        .iter()
        .map(|point| {
            if transfer {
                Pt { x: point.x + (to.x - dx_from.x), y: point.y + (to.y - dx_from.y) }
            } else {
                Pt { x: point.x + to.x - dx_from.x, y: point.y + to.y - dx_from.y }
            }
        })
        .collect();
    if shifted.iter().any(|point| !usable(point.x, point.y) || !point_in_polygon(*point, zone)) {
        return refuse();
    }
    if sides(&shifted).any(|(a, b)| constraints.walls.iter().any(|wall| segments_meet(a, b, wall.start, wall.end))) {
        return refuse();
    }
    let others = constraints.blocked[..constraints.obstacle_count].iter().chain(
        constraints.footprints.iter().filter(|(id, _)| &**id != facility_id).map(|(_, polygon)| polygon),
    );
    for shape in others {
        if shifted.iter().any(|point| point_in_polygon(*point, shape)) || point_in_polygon(shape[0], &shifted) {
            return refuse();
        }
        if sides(&shifted).any(|(a, b)| segment_to_polygon(a, b, shape) < 1e-8) {
            return refuse();
        }
    }
    Ok(())
}

fn check_change(world: &Bound<'_, PyAny>, op: &str, target: &str, parameters: &Bound<'_, PyAny>, end_minute: Option<i64>) -> PyResult<()> {
    let py = world.py();
    let Some(state) = state_of(world, OVERLAY_KEY)? else {
        return refuse();
    };
    exact_dict(&state)?;
    let geo = Geo::of(world)?;
    let layout = layout_of_plan(&geo)?;
    let constraints = constraints_of_plan(&geo)?;
    match op {
        "set_route_access" => {
            let data = model_of(py, OVERLAY_MODULE, "RouteAccessParameters", parameters)?;
            let accessible = attribute(&data, intern!(py, "accessible"))?.is_truthy()?;
            let edges = edges_of(world)?;
            let Some(edge) = edges.get(target) else {
                return refuse();
            };
            if end_minute.is_none() || (accessible && edge.closed) {
                return refuse();
            }
            if !accessible {
                let rooms = part(&state, "temporary_rooms")?;
                for room in map_of(&rooms)?.values() {
                    let room = map_of(room)?;
                    if !flag_of(room, "active")? {
                        continue;
                    }
                    let ids = match room.get("route_edge_ids") {
                        Some(ids) => list_of(ids)?,
                        None => &[],
                    };
                    if !ids.iter().any(|id| matches!(id, V::Str(id) if &**id == target)) {
                        continue;
                    }
                    let room_zone = str_of(room, "zone_id")?;
                    let mut incoming = false;
                    let mut outgoing = false;
                    for id in ids {
                        let V::Str(id) = id else {
                            return unsupported("a route's ID is not a str");
                        };
                        if &**id == target {
                            continue;
                        }
                        let (Some(edge), Some(route)) = (edges.get(id), layout.routes.get(&**id)) else {
                            continue;
                        };
                        if edge.closed || !route.accessible {
                            continue;
                        }
                        incoming |= edge.to == *room_zone;
                        outgoing |= edge.from == *room_zone;
                    }
                    if !incoming || !outgoing {
                        return refuse();
                    }
                }
            }
            Ok(())
        }
        "transfer_equipment" => {
            let data = model_of(py, OPERATIONS_MODULE, "EquipmentTransferParameters", parameters)?;
            let (equipment, from_zone, to_zone) = (
                text_attr(&data, intern!(py, "equipment_id"))?,
                text_attr(&data, intern!(py, "from_zone_id"))?,
                text_attr(&data, intern!(py, "to_zone_id"))?,
            );
            let point = point_attr(&data, intern!(py, "point_m"))?;
            let store = entities_of(world)?;
            let known = EntityStore::with_rows(&store, |rows| Ok(rows.facilities.get(target).map(|facility| facility.zone_id.clone())))?;
            let (Some(zone_now), Some(&old)) = (known, layout.facility_points.get(target)) else {
                return refuse();
            };
            let zones = attribute(world, intern!(py, "zones"))?;
            if &*equipment != target || from_zone != zone_now || !exact_dict(&zones)?.contains(&*to_zone)? || end_minute.is_some() {
                return refuse();
            }
            let (_, kind) = facility_is_free(world, target)?;
            let (Some(_), Some(destination)) = (layout.zones.get(&*from_zone), layout.zones.get(&*to_zone)) else {
                return refuse();
            };
            if !point_in_polygon(point, &destination.polygon) {
                return refuse();
            }
            let rooms = part(&state, "temporary_rooms")?;
            clear_of_rooms(&geo, map_of(&rooms)?, target, &to_zone, point)?;
            if !zones_connected(&edges_of(world)?, &from_zone, &to_zone) {
                return refuse();
            }
            service_staff_in_zone(world, &geo, target, &kind, &to_zone)?;
            if walls_are_near(constraints, point) || !walkable_at(&geo, &to_zone, point)? {
                return refuse();
            }
            let (dx, dy) = (point.x - old.x, point.y - old.y);
            for service in &layout.service_points {
                if &*service.facility_id != target {
                    continue;
                }
                let shifted = Pt { x: service.point.x + dx, y: service.point.y + dy };
                if !usable(shifted.x, shifted.y)
                    || !point_in_polygon(shifted, &destination.polygon)
                    || walls_are_near(constraints, shifted)
                    || !walkable_at(&geo, &to_zone, shifted)?
                {
                    return refuse();
                }
            }
            footprint_fits(constraints, target, &destination.polygon, old, point, true)
        }
        "move_facility" => {
            let data = model_of(py, OVERLAY_MODULE, "MoveFacilityParameters", parameters)?;
            let point = point_attr(&data, intern!(py, "point_m"))?;
            let store = entities_of(world)?;
            let known = EntityStore::with_rows(&store, |rows| Ok(rows.facilities.get(target).is_some()))?;
            let (true, Some(&old)) = (known, layout.facility_points.get(target)) else {
                return refuse();
            };
            if end_minute.is_some() {
                return refuse();
            }
            let (zone_id, _) = facility_is_free(world, target)?;
            let Some(zone) = layout.zones.get(&*zone_id) else {
                return unsupported("a facility in a zone with no polygon");
            };
            if !point_in_polygon(point, &zone.polygon) {
                return refuse();
            }
            let rooms = part(&state, "temporary_rooms")?;
            clear_of_rooms(&geo, map_of(&rooms)?, target, &zone_id, point)?;
            if walls_are_near(constraints, point) {
                return refuse();
            }
            for service in &layout.service_points {
                if &*service.facility_id != target {
                    continue;
                }
                let shifted = Pt { x: service.point.x + point.x - old.x, y: service.point.y + point.y - old.y };
                if !usable(shifted.x, shifted.y)
                    || !point_in_polygon(shifted, &zone.polygon)
                    || walls_are_near(constraints, shifted)
                    || !walkable_at(&geo, &zone_id, shifted)?
                {
                    return refuse();
                }
            }
            if !walkable_at(&geo, &zone_id, point)? {
                return refuse();
            }
            footprint_fits(constraints, target, &zone.polygon, old, point, false)
        }
        "set_partition" => {
            let data = model_of(py, OVERLAY_MODULE, "PartitionParameters", parameters)?;
            let polygon = points_attr(&data, intern!(py, "polygon_m"))?;
            let Some(zone) = layout.zones.get(target) else {
                return refuse();
            };
            if end_minute.is_none() || !inside_zone(&zone.polygon, &polygon) {
                return refuse();
            }
            if layout.facility_points.values().any(|point| point_in_polygon(*point, &polygon)) {
                return refuse();
            }
            for shape in &constraints.blocked {
                if polygon.iter().any(|point| point_in_polygon(*point, shape))
                    || point_in_polygon(shape[0], &polygon)
                    || sides(&polygon).any(|(a, b)| segment_to_polygon(a, b, shape) < 1e-8)
                {
                    return refuse();
                }
            }
            let movement = movement_mirror(world)?;
            let store = entities_of(world)?;
            if let Some(mirror) = &movement {
                NamespaceMirror::with_row::<MovementRow, _>(mirror, |row| {
                    for position in row.positions.values() {
                        if let Some(point) = position.point {
                            if !usable(point.x, point.y) {
                                return unsupported("a position is not a usable point");
                            }
                            if point_in_polygon(Pt { x: point.x, y: point.y }, &polygon) {
                                return refuse();
                            }
                        }
                    }
                    Ok(())
                })?;
            }
            let staff_zones = EntityStore::with_rows(&store, |rows| Ok(rows.staff.iter().map(|(_, staff)| staff.zone_id.clone()).collect::<Vec<_>>()))?;
            for zone_id in staff_zones {
                if layout.zones.get(&*zone_id).is_some() {
                    if let Some(centre) = geo.zone_center(&zone_id)? {
                        if point_in_polygon(centre, &polygon) {
                            return refuse();
                        }
                    }
                }
            }
            for route in layout.routes.values() {
                if route.points.windows(2).any(|pair| !segment_clear_of_polygon(pair[0], pair[1], &polygon, route.width / 2.0)) {
                    return refuse();
                }
            }
            if let Some(mirror) = &movement {
                NamespaceMirror::with_row::<MovementRow, _>(mirror, |row| {
                    for journey in row.journeys.values() {
                        let points: Vec<Pt> = journey.route_points.iter().map(|point| Pt { x: point.x, y: point.y }).collect();
                        if points.iter().any(|point| !usable(point.x, point.y)) {
                            return unsupported("a route point is not a usable point");
                        }
                        if points.windows(2).any(|pair| !segment_clear_of_polygon(pair[0], pair[1], &polygon, WALL_CLEARANCE))
                            && !crate::rules::movement::is_queue_trip(&journey.purpose)
                        {
                            return refuse();
                        }
                    }
                    Ok(())
                })?;
            }
            Ok(())
        }
        "temporary_room_geometry" => {
            let data = model_of(py, OPERATIONS_MODULE, "TemporaryRoomGeometry", parameters)?;
            let polygon = points_attr(&data, intern!(py, "polygon_m"))?;
            let beds = points_attr(&data, intern!(py, "bed_points_m"))?;
            let room_id = text_attr(&data, intern!(py, "room_id"))?;
            let zone_id = text_attr(&data, intern!(py, "zone_id"))?;
            let route_ids = attribute(&data, intern!(py, "route_edge_ids"))?;
            let route_ids = route_ids.downcast_exact::<PyTuple>().map_err(|_| Unsupported::new_err("not a tuple"))?;
            let route_ids: Vec<Arc<str>> = route_ids.iter().map(|id| Ok(Arc::from(exact_str(&id)?))).collect::<PyResult<_>>()?;
            let duration = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
            if &*zone_id != target || end_minute.is_none_or(|end| end >= duration) {
                return refuse();
            }
            let Some(zone) = layout.zones.get(target) else {
                return refuse();
            };
            if !inside_zone(&zone.polygon, &polygon) {
                return refuse();
            }
            let edges = edges_of(world)?;
            for id in &route_ids {
                let (Some(edge), Some(route)) = (edges.get(id), layout.routes.get(&**id)) else {
                    return refuse();
                };
                if (&*edge.from != target && &*edge.to != target) || edge.closed || !route.accessible {
                    return refuse();
                }
            }
            let incoming = route_ids.iter().any(|id| &*edges[id].to == target);
            let outgoing = route_ids.iter().any(|id| &*edges[id].from == target);
            if !incoming || !outgoing {
                return refuse();
            }
            let rooms = part(&state, "temporary_rooms")?;
            let rooms = map_of(&rooms)?;
            if rooms.contains_key(&*room_id) {
                return refuse();
            }
            for row in rooms.values() {
                let row = map_of(row)?;
                if flag_of(row, "active")?
                    && matches!(row.get("zone_id"), Some(V::Str(zone)) if &**zone == target)
                    && polygons_overlap(&polygon, &polygon_of(row.get("polygon_m"))?)
                {
                    return refuse();
                }
            }
            if layout.facility_points.values().any(|point| point_in_polygon(*point, &polygon)) {
                return refuse();
            }
            let reservations = part(&state, "bed_reservations")?;
            for reservation in map_of(&reservations)?.values() {
                let reservation = map_of(reservation)?;
                let Some(V::Str(room)) = reservation.get("room_id") else {
                    continue;
                };
                let Some(V::Map(room)) = rooms.get(room) else {
                    continue;
                };
                let held = match room.get("bed_points_m") {
                    Some(held) => list_of(held)?,
                    None => &[],
                };
                if let Some(V::Int(index)) = reservation.get("bed_index") {
                    if *index >= 0 && (*index as usize) < held.len() && point_in_polygon(point_in_value(&held[*index as usize])?, &polygon) {
                        return refuse();
                    }
                }
            }
            if constraints.blocked.iter().any(|shape| polygons_overlap(&polygon, shape)) {
                return refuse();
            }
            if sides(&polygon).any(|(a, b)| constraints.walls.iter().any(|wall| segments_meet(a, b, wall.start, wall.end))) {
                return refuse();
            }
            if let Some(mirror) = &movement_mirror(world)? {
                NamespaceMirror::with_row::<MovementRow, _>(mirror, |row| {
                    for position in row.positions.values().chain(row.staff_positions.values()) {
                        if let Some(point) = position.point {
                            if !usable(point.x, point.y) {
                                return unsupported("a position is not a usable point");
                            }
                            if point_in_polygon(Pt { x: point.x, y: point.y }, &polygon) {
                                return refuse();
                            }
                        }
                    }
                    Ok(())
                })?;
            }
            let mut access = Vec::with_capacity(route_ids.len() + 1);
            for id in &route_ids {
                let (edge, route) = (&edges[id], &layout.routes[&**id]);
                let point = if &*edge.to == target { route.points[route.points.len() - 1] } else { route.points[0] };
                access.push(point);
            }
            if let Some(center) = geo.zone_center(target)? {
                access.push(center);
            }
            for bed in &beds {
                let mut reachable = false;
                for entry in &access {
                    if geo.room_route(target, Some(*entry), *bed, "independent").map_err(crate::rules::movement::refused)?.is_some() {
                        reachable = true;
                        break;
                    }
                }
                if !reachable {
                    return refuse();
                }
            }
            Ok(())
        }
        _ => refuse(),
    }
}

fn end_of(end_minute: &Bound<'_, PyAny>) -> PyResult<Option<i64>> {
    if end_minute.is_none() {
        Ok(None)
    } else {
        Ok(Some(exact_int(end_minute)?))
    }
}

#[pyfunction]
fn validate_geometry_change(
    world: &Bound<'_, PyAny>,
    op: &Bound<'_, PyAny>,
    target: &Bound<'_, PyAny>,
    parameters: &Bound<'_, PyAny>,
    end_minute: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let result = (|| check_change(world, exact_str(op)?, exact_str(target)?, parameters, end_of(end_minute)?))();
    counted(world.py(), "validate_geometry_change", result)
}

// ---- apply_geometry_change ----------------------------------------------------------------------

/// `{..first, **common, "end_minute": end}`.
fn stamped_record<'py>(
    py: Python<'py>,
    first: Vec<(&'static str, Bound<'py, PyAny>)>,
    event_id: &str,
    sources: &[&str],
    minute: &Bound<'py, PyAny>,
    end: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let dict = PyDict::new(py);
    for (key, value) in first {
        dict.set_item(key, value)?;
    }
    dict.set_item("event_id", event_id)?;
    dict.set_item("source_ids", PyList::new(py, sources.iter().copied())?)?;
    dict.set_item("minute", minute)?;
    if let Some(end) = end {
        dict.set_item("end_minute", end)?;
    }
    Ok(dict.into_any())
}

/// `apply_geometry_change`: the event's writes on this world, after its checks.
pub(crate) fn apply_change(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, expire: bool) -> PyResult<()> {
    let py = world.py();
    let Some(row) = EventRow::from_event(event)? else {
        return unsupported("not a ScheduledEvent");
    };
    let V::Map(payload) = &row.payload else {
        return unsupported("the payload is not a dict");
    };
    let (op, target, modifier) = (str_of(payload, "op")?, str_of(payload, "target_id")?, str_of(payload, "modifier_id")?);
    let Some(state) = state_of(world, OVERLAY_KEY)? else {
        return unsupported("no overlay");
    };
    exact_dict(&state)?;
    let raw_parameters = event.getattr(intern!(py, "payload"))?.get_item(intern!(py, "parameters")).map_err(|_| Unsupported::new_err("no parameters"))?;
    let end_minute = match payload.get("end_minute") {
        Some(V::Int(minute)) => Some(*minute),
        Some(V::Null) => None,
        _ => return unsupported("the end minute is another value"),
    };
    if !expire {
        check_change(world, op, target, &raw_parameters, end_minute)?;
    }
    // {event.source_id, *parameters.source_ids, *marker.assumption_ids}, what is a str with text.
    let mut sources: Vec<&str> = Vec::new();
    if let Some(source) = row.source_id.as_deref().filter(|source| !source.is_empty()) {
        sources.push(source);
    }
    match payload.get("parameters") {
        Some(V::Map(parameters)) => {
            if let Some(list) = parameters.get("source_ids") {
                for item in list_of(list)? {
                    if let V::Str(item) = item {
                        if !item.is_empty() {
                            sources.push(item);
                        }
                    }
                }
            }
        }
        None => {}
        _ => return unsupported("the parameters are another value"),
    }
    if let V::Map(provenance) = &row.provenance {
        match provenance.get("effect_program_v1") {
            Some(V::Map(marker)) => {
                if let Some(list) = marker.get("assumption_ids") {
                    for item in list_of(list)? {
                        if let V::Str(item) = item {
                            if !item.is_empty() {
                                sources.push(item);
                            }
                        }
                    }
                }
            }
            None => {}
            _ => return unsupported("the marker is another value"),
        }
    }
    let sources = sorted_set(sources);
    let source_list = || PyList::new(py, sources.iter().copied()).map(Bound::into_any);
    let minute = attribute(world, intern!(py, "minute_index"))?;
    let event_text = py_str(py, &row.event_id);
    let history = required(&state, "history")?;
    let end_object = |py: Python<'_>| -> PyResult<Py<PyAny>> {
        Ok(match end_minute {
            Some(end) => end.into_pyobject(py)?.into_any().unbind(),
            None => py.None(),
        })
    };
    let mut history_target: Arc<str> = target.clone();
    match &**op {
        "move_facility" | "transfer_equipment" => {
            let (point, zone) = if &**op == "transfer_equipment" {
                let data = model_of(py, OPERATIONS_MODULE, "EquipmentTransferParameters", &raw_parameters)?;
                (point_attr(&data, intern!(py, "point_m"))?, Some(attribute(&data, intern!(py, "to_zone_id"))?))
            } else {
                let data = model_of(py, OVERLAY_MODULE, "MoveFacilityParameters", &raw_parameters)?;
                (point_attr(&data, intern!(py, "point_m"))?, None)
            };
            let points = required(&state, "facility_points")?;
            let record = stamped_record(py, vec![("point_m", point_dict(py, point)?)], &row.event_id, &sources, &minute, None)?;
            if let Some(zone) = zone {
                let facilities = attribute(world, intern!(py, "facilities"))?;
                let Some(facility) = raw_dict_get(&facilities, py_str(py, target).as_any())? else {
                    return unsupported("no such facility");
                };
                facility.setattr(intern!(py, "zone_id"), zone)?;
            }
            points.set_item(&**target, record)?;
        }
        "set_partition" => {
            let partitions = required(&state, "partitions")?;
            if expire {
                partitions.call_method1(intern!(py, "pop"), (&**modifier, py.None()))?;
            } else {
                let data = model_of(py, OVERLAY_MODULE, "PartitionParameters", &raw_parameters)?;
                let polygon = points_attr(&data, intern!(py, "polygon_m"))?;
                let corners = polygon.iter().map(|point| point_dict(py, *point)).collect::<PyResult<Vec<_>>>()?;
                let items = vec![("zone_id", py_str(py, target)), ("polygon_m", PyList::new(py, corners)?.into_any())];
                let end = end_object(py)?.into_bound(py);
                partitions.set_item(&**modifier, stamped_record(py, items, &row.event_id, &sources, &minute, Some(end))?)?;
            }
        }
        "set_route_access" => {
            let edges = attribute(world, intern!(py, "zone_edges"))?;
            let Some(edge) = raw_dict_get(&edges, py_str(py, target).as_any())? else {
                return unsupported("no such route");
            };
            let routes = required(&state, "route_modifiers")?;
            let accessible = if expire {
                None
            } else {
                match payload.get("parameters") {
                    Some(V::Map(parameters)) => match parameters.get("accessible") {
                        Some(V::Bool(flag)) => Some(*flag),
                        _ => return unsupported("accessible is not a bool"),
                    },
                    _ => return unsupported("no parameters"),
                }
            };
            let record = match child(&routes, target)? {
                Some(record) => record,
                None => {
                    let fresh = dict_of(py, &[("base_closed", attribute(&edge, intern!(py, "closed"))?), ("modifiers", PyDict::new(py).into_any())])?;
                    routes.set_item(&**target, fresh)?;
                    required(&routes, target)?
                }
            };
            let modifiers = required(&record, "modifiers")?;
            if let Some(accessible) = accessible {
                let items = vec![("accessible", py_flag(py, accessible))];
                let end = end_object(py)?.into_bound(py);
                modifiers.set_item(&**modifier, stamped_record(py, items, &row.event_id, &sources, &minute, Some(end))?)?;
            } else {
                modifiers.call_method1(intern!(py, "pop"), (&**modifier, py.None()))?;
            }
            let base = required(&record, "base_closed")?;
            let mut closed = base.downcast_exact::<PyBool>().map_err(|_| Unsupported::new_err("not a bool"))?.is_true();
            for (_, item) in exact_dict(&modifiers)?.iter() {
                let flag = required(&item, "accessible")?;
                closed |= !flag.downcast_exact::<PyBool>().map_err(|_| Unsupported::new_err("not a bool"))?.is_true();
            }
            edge.setattr(intern!(py, "closed"), closed)?;
            if exact_dict(&modifiers)?.is_empty() {
                routes.del_item(&**target)?;
            }
        }
        "temporary_room_geometry" => {
            let data = model_of(py, OPERATIONS_MODULE, "TemporaryRoomGeometry", &raw_parameters)?;
            let room_id = text_attr(&data, intern!(py, "room_id"))?;
            let rooms = match child(&state, "temporary_rooms")? {
                Some(rooms) => rooms,
                None => return unsupported("the rooms are still to be made"),
            };
            if expire {
                let Some(record) = child(&rooms, &room_id)? else {
                    return unsupported("the room did not start");
                };
                record.set_item(intern!(py, "active"), false)?;
                record.set_item(intern!(py, "end_event_id"), &event_text)?;
            } else {
                let dump = PyDict::new(py);
                dump.set_item("mode", "json")?;
                let record = data.call_method(intern!(py, "model_dump"), (), Some(&dump))?;
                let fields = record.downcast_exact::<PyDict>().map_err(|_| Unsupported::new_err("not a dict"))?;
                fields.set_item("start_minute", &minute)?;
                fields.set_item("end_minute", end_object(py)?)?;
                fields.set_item("active", true)?;
                fields.set_item("start_event_id", &event_text)?;
                fields.set_item("end_event_id", py.None())?;
                fields.set_item("program_id", &**str_of(payload, "program_id")?)?;
                fields.set_item("effect_id", &**str_of(payload, "effect_id")?)?;
                fields.set_item("source_ids", source_list()?)?;
                rooms.set_item(&*room_id, record)?;
            }
            history_target = room_id;
        }
        _ => return unsupported("another operator"),
    }
    let entry = stamped_record(
        py,
        vec![
            ("transition_id", py_str(py, &row.event_id)),
            ("op", py_str(py, op)),
            ("target_id", py_str(py, &history_target)),
            ("status", py_str(py, if expire { "expired" } else { "applied" })),
            ("causation_id", row.causation_id.as_deref().map_or_else(|| py.None().into_bound(py), |id| py_str(py, id))),
        ],
        &row.event_id,
        &sources,
        &minute,
        None,
    )?;
    history.call_method1(intern!(py, "append"), (entry,))?;
    trim_history(&history, MAX_OVERLAY_HISTORY)
}

#[pyfunction]
#[pyo3(signature = (world, event, *, expire=false))]
fn apply_geometry_change(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, expire: bool) -> PyResult<()> {
    counted(world.py(), "apply_geometry_change", apply_change(world, event, expire))
}

// ---- validate_geometry_overlay_state ------------------------------------------------------------

fn id_list_is_good(value: Option<&V>) -> bool {
    matches!(value, Some(V::List(items)) if items.iter().all(|item| matches!(item, V::Str(id) if is_identifier(id))))
}

fn ids_are_unique(value: Option<&V>) -> bool {
    let Some(V::List(items)) = value else {
        return false;
    };
    let mut seen = HashSet::new();
    items.iter().all(|item| matches!(item, V::Str(id) if seen.insert(id.clone())))
}

/// What `_FacilityPointOverride`, `_PartitionOverride` and `_RouteModifier` have in common.
fn stamped_is_good(row: &Doc) -> PyResult<i64> {
    let minute = int_of(row, "minute")?;
    if minute < 0 || !matches!(row.get("event_id"), Some(V::Str(id)) if is_identifier(id)) || !id_list_is_good(row.get("source_ids")) {
        return unsupported("a record has another shape");
    }
    Ok(minute)
}

/// `_GeometryOverlayState` for what it takes as it is, and the rule's checks after it.
fn check_overlay(world: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let Some(node) = state_of(world, OVERLAY_KEY)? else {
        return Ok(());
    };
    let value = V::from_py(&node)?;
    let state = map_of(&value)?;
    const NAMES: [&str; 9] = [
        "schema_version", "policy_version", "facility_points", "partitions", "route_modifiers", "history", "temporary_rooms",
        "bed_reservations", "room_admissions",
    ];
    if state.keys().any(|key| !NAMES.contains(&&**key))
        || state.get("schema_version") != Some(&V::Int(1))
        || str_of(state, "policy_version")?.as_ref() != POLICY_OVERLAY
    {
        return unsupported("the overlay has another shape");
    }
    let empty = IndexMap::new();
    let optional = |name: &str| -> PyResult<&Doc> {
        match state.get(name) {
            Some(V::Map(map)) => Ok(map),
            None => Ok(&empty),
            _ => unsupported("a part of the overlay is another value"),
        }
    };
    let points = map_of(state.get("facility_points").unwrap_or(&V::Null))?;
    let partitions = map_of(state.get("partitions").unwrap_or(&V::Null))?;
    let route_modifiers = map_of(state.get("route_modifiers").unwrap_or(&V::Null))?;
    let history = list_of(state.get("history").unwrap_or(&V::Null))?;
    let rooms = optional("temporary_rooms")?;
    let reservations = optional("bed_reservations")?;
    let admissions = optional("room_admissions")?;
    if history.len() > MAX_OVERLAY_HISTORY
        || points.len() > 256
        || partitions.len() > 256
        || route_modifiers.len() > 256
        || rooms.len() > 256
        || reservations.len() > 10_000
        || admissions.len() > 10_000
    {
        return unsupported("the overlay exceeds a limit");
    }
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let mut seen = HashSet::new();
    let mut previous = None;
    for row in history {
        let row = map_of(row)?;
        if !has_exactly(row, &["transition_id", "op", "target_id", "status", "causation_id", "event_id", "source_ids", "minute"]) {
            return unsupported("a history record has another shape");
        }
        let at = stamped_is_good(row)?;
        let transition = str_of(row, "transition_id")?;
        if !is_identifier(transition)
            || !is_identifier(str_of(row, "target_id")?)
            || !matches!(&**str_of(row, "op")?, "move_facility" | "transfer_equipment" | "set_partition" | "set_route_access" | "temporary_room_geometry" | "install_facility")
            || !matches!(&**str_of(row, "status")?, "applied" | "expired")
            || !match row.get("causation_id") {
                Some(V::Null) => true,
                Some(V::Str(id)) => is_identifier(id),
                _ => false,
            }
            || !seen.insert(transition.clone())
            || at > minute
            || previous.is_some_and(|before| before > at)
        {
            return unsupported("a history record is not what the Python rule passes");
        }
        previous = Some(at);
    }
    let geo = Geo::of(world)?;
    let layout = layout_of_plan(&geo)?;
    let constraints = geo.constraints.as_ref();
    let edges = edges_of(world)?;
    let store = entities_of(world)?;
    // Facility and person facts, read once.
    let (facility_zones, agents): (HashMap<Arc<str>, Arc<str>>, HashSet<Arc<str>>) = EntityStore::with_rows(&store, |rows| {
        Ok((
            rows.facilities.iter().map(|(id, facility)| (id.clone(), facility.zone_id.clone())).collect(),
            rows.agents.iter().map(|(id, _)| id.clone()).collect(),
        ))
    })?;
    let mut active_rooms: Vec<(&Arc<str>, Vec<Pt>, &Arc<str>)> = Vec::new();
    let mut room_capacity: HashMap<&str, i64> = HashMap::new();
    let mut sleeping: HashMap<&str, i64> = HashMap::new();
    for (room_id, room) in rooms {
        let room = map_of(room)?;
        let names = [
            "schema_version", "room_id", "zone_id", "polygon_m", "people_capacity", "sleeping_capacity", "bed_points_m", "route_edge_ids",
            "start_minute", "end_minute", "active", "start_event_id", "end_event_id", "program_id", "effect_id", "source_ids",
        ];
        if !has_exactly(room, &names) || room.get("schema_version") != Some(&V::Int(1)) || !is_identifier(room_id) {
            return unsupported("a room has another shape");
        }
        let zone_id = str_of(room, "zone_id")?;
        let (people, beds_wanted) = (int_of(room, "people_capacity")?, int_of(room, "sleeping_capacity")?);
        let (start, end) = (int_of(room, "start_minute")?, int_of(room, "end_minute")?);
        let Some(V::Bool(active)) = room.get("active") else {
            return unsupported("a room's active is not a bool");
        };
        let ended = match room.get("end_event_id") {
            Some(V::Null) => false,
            Some(V::Str(id)) if is_identifier(id) => true,
            _ => return unsupported("a room's end event is another value"),
        };
        let polygon = points_in_value(room.get("polygon_m").unwrap_or(&V::Null))?;
        let beds = points_in_value(room.get("bed_points_m").unwrap_or(&V::Null))?;
        if str_of(room, "room_id")? != room_id
            || !is_identifier(zone_id)
            || !(1..=1_000_000).contains(&people)
            || !(0..=1_000_000).contains(&beds_wanted)
            || start < 0
            || end <= 0
            || end <= start
            || beds_wanted > people
            || beds_wanted != beds.len() as i64
            || *active == ended
            || !id_list_is_good(room.get("route_edge_ids"))
            || !ids_are_unique(room.get("route_edge_ids"))
            || !id_list_is_good(room.get("source_ids"))
            || !ids_are_unique(room.get("source_ids"))
            || !is_identifier(str_of(room, "start_event_id")?)
            || !is_identifier(str_of(room, "program_id")?)
            || !is_identifier(str_of(room, "effect_id")?)
        {
            return unsupported("a room is not what its contract takes");
        }
        let Some(zone) = layout.zones.get(&**zone_id) else {
            return unsupported("a room in an unknown zone");
        };
        if start > minute || (*active && minute > end) || !inside_zone(&zone.polygon, &polygon) {
            return unsupported("a room is not what the Python rule passes");
        }
        if beds.iter().any(|point| !point_in_polygon(*point, &polygon)) {
            return unsupported("a bed lies outside its room");
        }
        let route_ids = list_of(room.get("route_edge_ids").unwrap_or(&V::Null))?;
        let (mut incoming, mut outgoing) = (false, false);
        for id in route_ids {
            let V::Str(id) = id else {
                return unsupported("a route's ID is not a str");
            };
            let Some(edge) = edges.get(id) else {
                return unsupported("a room references an unknown route");
            };
            if !edge.closed && layout.routes.get(&**id).is_some_and(|route| route.accessible) {
                incoming |= edge.to == *zone_id;
                outgoing |= edge.from == *zone_id;
            }
        }
        if *active && (!incoming || !outgoing) {
            return unsupported("an active room lacks a route");
        }
        room_capacity.insert(room_id, people);
        sleeping.insert(room_id, beds_wanted);
        if *active {
            active_rooms.push((room_id, polygon, zone_id));
        }
    }
    for (index, (_, polygon, zone_id)) in active_rooms.iter().enumerate() {
        for (_, other, other_zone) in &active_rooms[index + 1..] {
            if zone_id == other_zone && polygons_overlap(polygon, other) {
                return unsupported("active rooms overlap");
            }
        }
        if layout.facility_points.values().any(|point| point_in_polygon(*point, polygon)) {
            return unsupported("an active room covers a facility");
        }
    }
    for (key, row) in points {
        let row = map_of(row)?;
        if !has_exactly(row, &["point_m", "event_id", "source_ids", "minute"]) || !is_identifier(key) {
            return unsupported("a move has another shape");
        }
        let at = stamped_is_good(row)?;
        let point = point_in_value(row.get("point_m").unwrap_or(&V::Null))?;
        let Some(zone) = facility_zones.get(&**key).and_then(|zone| layout.zones.get(&**zone)) else {
            return unsupported("a move of an unknown facility");
        };
        if at > minute || !point_in_polygon(point, &zone.polygon) {
            return unsupported("a move is not what the Python rule passes");
        }
    }
    for service in &layout.service_points {
        let Some(zone) = facility_zones.get(&*service.facility_id).and_then(|zone| layout.zones.get(&**zone)) else {
            return unsupported("a service point of an unknown facility");
        };
        if !point_in_polygon(service.point, &zone.polygon) {
            return unsupported("a service point leaves its zone");
        }
    }
    for (key, row) in partitions {
        let row = map_of(row)?;
        if !has_exactly(row, &["zone_id", "polygon_m", "event_id", "source_ids", "minute", "end_minute"]) || !is_identifier(key) {
            return unsupported("a partition has another shape");
        }
        let at = stamped_is_good(row)?;
        let end = int_of(row, "end_minute")?;
        let polygon = points_in_value(row.get("polygon_m").unwrap_or(&V::Null))?;
        let zone_id = str_of(row, "zone_id")?;
        let Some(zone) = layout.zones.get(&**zone_id) else {
            return unsupported("a partition in an unknown zone");
        };
        if !is_identifier(zone_id) || !(3..=64).contains(&polygon.len()) || end <= at || end <= 0 || at > minute || !inside_zone(&zone.polygon, &polygon) {
            return unsupported("a partition is not what the Python rule passes");
        }
    }
    let mut nested = 0;
    for (key, row) in route_modifiers {
        let row = map_of(row)?;
        let (Some(V::Bool(base)), Some(V::Map(modifiers)), 2) = (row.get("base_closed"), row.get("modifiers"), row.len()) else {
            return unsupported("a route's overlay has another shape");
        };
        let Some(edge) = edges.get(&**key) else {
            return unsupported("an unknown route");
        };
        if modifiers.is_empty() || !is_identifier(key) {
            return unsupported("a route's overlay has no modifier");
        }
        nested += modifiers.len();
        let mut closed = *base;
        for (id, record) in modifiers {
            let record = map_of(record)?;
            if !has_exactly(record, &["accessible", "event_id", "source_ids", "minute", "end_minute"]) || !is_identifier(id) {
                return unsupported("a route modifier has another shape");
            }
            let at = stamped_is_good(record)?;
            let end = int_of(record, "end_minute")?;
            let Some(V::Bool(accessible)) = record.get("accessible") else {
                return unsupported("accessible is not a bool");
            };
            if end <= at || end <= 0 || at > minute {
                return unsupported("a route modifier is not what the Python rule passes");
            }
            closed |= !*accessible;
        }
        if edge.closed != closed {
            return unsupported("the overlay and the graph disagree");
        }
    }
    if nested > 256 {
        return unsupported("the overlay exceeds a limit");
    }
    if let Some(constraints) = constraints {
        for (facility_id, footprint) in &constraints.footprints {
            let Some(zone_id) = facility_zones.get(facility_id) else {
                return unsupported("a footprint of an unknown facility");
            };
            let Some(zone) = layout.zones.get(&**zone_id) else {
                return unsupported("a footprint in an unknown zone");
            };
            if footprint.iter().any(|point| !point_in_polygon(*point, &zone.polygon)) {
                return unsupported("a footprint leaves its zone");
            }
            if active_rooms.iter().any(|(_, polygon, room_zone)| *room_zone == zone_id && polygons_overlap(polygon, footprint)) {
                return unsupported("an active room overlaps a footprint");
            }
        }
    } else {
        return unsupported("no constraints");
    }
    let mut beds_held: HashSet<(Arc<str>, i64)> = HashSet::new();
    for (agent_id, reservation) in reservations {
        let reservation = map_of(reservation)?;
        if !has_exactly(reservation, &["agent_id", "room_id", "bed_index", "target_id", "reserved_minute"]) {
            return unsupported("a reservation has another shape");
        }
        let room_id = str_of(reservation, "room_id")?;
        let index = int_of(reservation, "bed_index")?;
        let at = int_of(reservation, "reserved_minute")?;
        let target = str_of(reservation, "target_id")?;
        if str_of(reservation, "agent_id")? != agent_id
            || !is_identifier(agent_id)
            || !is_identifier(target)
            || index < 0
            || at < 0
            || !agents.contains(agent_id)
            || sleeping.get(&**room_id).is_none_or(|beds| index >= *beds)
            || **target != *bed_target_id(room_id, index)
            || at > minute
            || !beds_held.insert((room_id.clone(), index))
        {
            return unsupported("a reservation is not what the Python rule passes");
        }
    }
    let mut counts: HashMap<&str, i64> = HashMap::new();
    for (agent_id, admission) in admissions {
        let admission = map_of(admission)?;
        let names_with = has_exactly(admission, &["agent_id", "room_id", "admitted_minute", "event_id", "source_ids"]);
        if !names_with {
            return unsupported("an admission has another shape");
        }
        let room_id = str_of(admission, "room_id")?;
        let at = int_of(admission, "admitted_minute")?;
        if str_of(admission, "agent_id")? != agent_id
            || !is_identifier(agent_id)
            || !is_identifier(room_id)
            || !is_identifier(str_of(admission, "event_id")?)
            || !id_list_is_good(admission.get("source_ids"))
            || at < 0
            || at > minute
            || !agents.contains(agent_id)
            || !room_capacity.contains_key(&**room_id)
        {
            return unsupported("an admission is not what the Python rule passes");
        }
        *counts.entry(room_id).or_insert(0) += 1;
    }
    if counts.iter().any(|(room_id, count)| room_capacity.get(room_id).is_none_or(|capacity| count > capacity)) {
        return unsupported("admissions exceed a room");
    }
    Ok(())
}

/// `validate_geometry_overlay_state`: true only for what the Python rule passes.
#[pyfunction]
fn validate_overlay(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    counted(world.py(), "validate_overlay", check_overlay(world).map(|()| true))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(register_temporary_space, m)?)?;
    m.add_function(wrap_pyfunction!(expire_temporary_space, m)?)?;
    m.add_function(wrap_pyfunction!(validate_spaces, m)?)?;
    m.add_function(wrap_pyfunction!(capacity_metrics_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(effective_zone_capacity, m)?)?;
    m.add_function(wrap_pyfunction!(effective_total_zone_capacity, m)?)?;
    m.add_function(wrap_pyfunction!(incoming_travelers, m)?)?;
    m.add_function(wrap_pyfunction!(release_sleep_bed, m)?)?;
    m.add_function(wrap_pyfunction!(release_temporary_room_admission, m)?)?;
    m.add_function(wrap_pyfunction!(room_admission_point, m)?)?;
    m.add_function(wrap_pyfunction!(available_temporary_room, m)?)?;
    m.add_function(wrap_pyfunction!(admit_to_temporary_room, m)?)?;
    m.add_function(wrap_pyfunction!(reserve_sleep_bed, m)?)?;
    m.add_function(wrap_pyfunction!(installed_facility_placement_enabled, m)?)?;
    m.add_function(wrap_pyfunction!(installed_facility_point, m)?)?;
    m.add_function(wrap_pyfunction!(place_installed_facility, m)?)?;
    m.add_function(wrap_pyfunction!(validate_geometry_change, m)?)?;
    m.add_function(wrap_pyfunction!(apply_geometry_change, m)?)?;
    m.add_function(wrap_pyfunction!(validate_overlay, m)?)?;
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    Ok(())
}
