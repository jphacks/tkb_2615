//! The floor plan as data for `rules/movement.rs`, and the geometry of `domain/movement.py` and
//! `contracts/extensions.py`. Every function here is the Python one with its order of operations,
//! its tolerances and its float results (docs/rust-port/lanes/m4-movement.md).
//!
//! `Geo::of` reads what `_geometry` and `_spatial_constraints` give a Python rule. The models come
//! from Python, parsed and validated there, so a bad config or overlay keeps its error text: any
//! failure is Unsupported, and the Python rule then raises it. A plan stays in the cache while the
//! parsed configs are the same objects and the overlay's moves and partitions the same bytes.

use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};
use std::sync::{Arc, Mutex};

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyString, PyTuple};
use rustc_hash::FxBuildHasher;

use crate::core::mathx::{hypot, py_fsum, py_max, py_min, py_round, py_sum};
use crate::core::row::{attribute, exact_dict, exact_float, exact_str, unsupported, IdMap};
use crate::Unsupported;

/// Past this a product could overflow where Python's `Point2D` would raise. Nothing real is here.
const COORDINATE_LIMIT: f64 = 1e100;
const OVERLAY_KEY: &str = "geometry_overlay_v1";
const CACHED_PLANS: usize = 8;
/// `MAX_REMEMBERED_ROOM_ROUTES`.
const REMEMBERED_ROUTES: usize = 4096;

#[derive(Clone, Copy, Debug)]
pub(crate) struct Pt {
    pub(crate) x: f64,
    pub(crate) y: f64,
}

/// A coordinate pair a kernel can compute with: what `Point2D` accepts, and not so large as to overflow.
pub(crate) fn usable(x: f64, y: f64) -> bool {
    x.abs() <= COORDINATE_LIMIT && y.abs() <= COORDINATE_LIMIT
}

pub(crate) fn distance(first: Pt, second: Pt) -> f64 {
    hypot(&[second.x - first.x, second.y - first.y])
}

/// `point_in_polygon`.
pub(crate) fn point_in_polygon(point: Pt, polygon: &[Pt]) -> bool {
    let mut inside = false;
    let mut previous = polygon[polygon.len() - 1];
    for &current in polygon {
        let cross = (point.x - previous.x) * (current.y - previous.y) - (point.y - previous.y) * (current.x - previous.x);
        if cross.abs() < 1e-9
            && py_min(previous.x, current.x) - 1e-9 <= point.x
            && point.x <= py_max(previous.x, current.x) + 1e-9
            && py_min(previous.y, current.y) - 1e-9 <= point.y
            && point.y <= py_max(previous.y, current.y) + 1e-9
        {
            return true;
        }
        if (current.y > point.y) != (previous.y > point.y) {
            let crossing = (previous.x - current.x) * (point.y - current.y) / (previous.y - current.y) + current.x;
            if point.x < crossing {
                inside = !inside;
            }
        }
        previous = current;
    }
    inside
}

fn segment_intersection_parameter(start: Pt, end: Pt, first: Pt, second: Pt) -> Option<f64> {
    let (dx, dy) = (end.x - start.x, end.y - start.y);
    let (ex, ey) = (second.x - first.x, second.y - first.y);
    let denominator = dx * ey - dy * ex;
    if denominator.abs() <= 1e-10 {
        return None;
    }
    let (ax, ay) = (first.x - start.x, first.y - start.y);
    let t = (ax * ey - ay * ex) / denominator;
    let u = (ax * dy - ay * dx) / denominator;
    (-1e-9 <= t && t <= 1.0 + 1e-9 && -1e-9 <= u && u <= 1.0 + 1e-9).then_some(t)
}

/// `_segment_intersection`.
pub(crate) fn segment_intersection(start: Pt, end: Pt, first: Pt, second: Pt) -> Option<Pt> {
    if let Some(t) = segment_intersection_parameter(start, end, first, second) {
        return Some(Pt { x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t });
    }
    let (dx, dy) = (end.x - start.x, end.y - start.y);
    let denominator = dx * dx + dy * dy;
    if denominator <= 1e-18 || (dx * (first.y - start.y) - dy * (first.x - start.x)).abs() > 1e-9 {
        return None;
    }
    let first_t = ((first.x - start.x) * dx + (first.y - start.y) * dy) / denominator;
    let second_t = ((second.x - start.x) * dx + (second.y - start.y) * dy) / denominator;
    let overlap_start = py_max(0.0, py_min(first_t, second_t));
    let overlap_end = py_min(1.0, py_max(first_t, second_t));
    if overlap_start > overlap_end + 1e-9 {
        return None;
    }
    let midpoint = (overlap_start + overlap_end) / 2.0;
    Some(Pt { x: start.x + dx * midpoint, y: start.y + dy * midpoint })
}

/// `zip(polygon, (*polygon[1:], polygon[0]))`.
fn sides(polygon: &[Pt]) -> impl Iterator<Item = (Pt, Pt)> + '_ {
    polygon.iter().copied().zip(polygon.iter().copied().skip(1).chain(polygon.first().copied()))
}

/// `_segment_inside_polygon`.
pub(crate) fn segment_inside_polygon(start: Pt, end: Pt, polygon: &[Pt]) -> bool {
    let mut cuts = vec![0.0, 1.0];
    cuts.extend(sides(polygon).filter_map(|(first, second)| segment_intersection_parameter(start, end, first, second)));
    // sorted(set(cuts)): 0.0 is first in the list, so a -0.0 goes as a set would drop it.
    cuts.sort_by(|first, second| first.partial_cmp(second).unwrap_or(std::cmp::Ordering::Equal));
    cuts.dedup_by(|later, kept| *later == *kept);
    cuts.windows(2).all(|pair| {
        let middle = (pair[0] + pair[1]) / 2.0;
        point_in_polygon(Pt { x: start.x + (end.x - start.x) * middle, y: start.y + (end.y - start.y) * middle }, polygon)
    })
}

fn segment_hits_polygon(start: Pt, end: Pt, polygon: &[Pt]) -> bool {
    if point_in_polygon(start, polygon) || point_in_polygon(end, polygon) {
        return true;
    }
    if sides(polygon).any(|(first, second)| segment_intersection(start, end, first, second).is_some()) {
        return true;
    }
    point_in_polygon(Pt { x: (start.x + end.x) / 2.0, y: (start.y + end.y) / 2.0 }, polygon)
}

fn point_segment_distance(point: Pt, start: Pt, end: Pt) -> f64 {
    let (dx, dy) = (end.x - start.x, end.y - start.y);
    let denominator = dx * dx + dy * dy;
    if denominator <= 1e-18 {
        return distance(point, start);
    }
    let fraction = py_min(1.0, py_max(0.0, ((point.x - start.x) * dx + (point.y - start.y) * dy) / denominator));
    distance(point, Pt { x: start.x + fraction * dx, y: start.y + fraction * dy })
}

/// `_segment_clear_of_polygon`.
pub(crate) fn segment_clear_of_polygon(start: Pt, end: Pt, polygon: &[Pt], clearance: f64) -> bool {
    if segment_hits_polygon(start, end, polygon) {
        return false;
    }
    sides(polygon).all(|(first, second)| {
        let nearest = py_min(
            py_min(
                py_min(point_segment_distance(start, first, second), point_segment_distance(end, first, second)),
                point_segment_distance(first, start, end),
            ),
            point_segment_distance(second, start, end),
        );
        nearest + 1e-9 >= clearance
    })
}

/// The clearance of `route_is_physically_clear` and `_room_segment_clear`, by the mobility's value.
pub(crate) fn walking_clearance(mobility: &str) -> Option<f64> {
    match mobility {
        "independent" => Some(0.25),
        "assisted" => Some(0.4),
        "wheelchair" => Some(0.45),
        _ => None,
    }
}

/// `_point_along_polyline`, for points that are not empty. None when `math.fsum` would not be the plain case.
pub(crate) fn point_along_polyline(points: &[Pt], progress: f64) -> Option<Pt> {
    let lengths: Vec<f64> = points.windows(2).map(|pair| distance(pair[0], pair[1])).collect();
    let total = py_fsum(&lengths)?;
    let last = points[points.len() - 1];
    if total <= 1e-12 {
        return Some(last);
    }
    let mut remaining = total * py_min(1.0, py_max(0.0, progress));
    for (pair, &length) in points.windows(2).zip(&lengths) {
        if remaining <= length {
            let ratio = if length <= 1e-12 { 0.0 } else { remaining / length };
            let (first, second) = (pair[0], pair[1]);
            return Some(Pt { x: first.x + (second.x - first.x) * ratio, y: first.y + (second.y - first.y) * ratio });
        }
        remaining -= length;
    }
    Some(last)
}

// ---- The plan ----------------------------------------------------------------------------------

pub(crate) struct Zone {
    pub(crate) polygon: Vec<Pt>,
    pub(crate) usable_area: f64,
    /// `_zone_center`. Err for a centre `Point2D` would refuse.
    pub(crate) center: Result<Option<Pt>, ()>,
}

pub(crate) struct Route {
    pub(crate) points: Vec<Pt>,
    pub(crate) width: f64,
    pub(crate) accessible: bool,
}

pub(crate) struct Layout {
    pub(crate) zones: IdMap<Zone>,
    pub(crate) routes: IdMap<Route>,
    pub(crate) facility_points: IdMap<Pt>,
}

pub(crate) struct Wall {
    pub(crate) id: Arc<str>,
    pub(crate) start: Pt,
    pub(crate) end: Pt,
}

pub(crate) struct Opening {
    pub(crate) wall_id: Arc<str>,
    pub(crate) center: Pt,
    pub(crate) width: f64,
    pub(crate) accessible: bool,
    pub(crate) edge_ids: Vec<Arc<str>>,
}

pub(crate) struct Constraints {
    pub(crate) walls: Vec<Wall>,
    pub(crate) openings: Vec<Opening>,
    /// Obstacles, then equipment footprints.
    pub(crate) blocked: Vec<Vec<Pt>>,
    /// The equipment footprints, by facility.
    pub(crate) footprints: Vec<(Arc<str>, Vec<Pt>)>,
}

/// What a route found in a room depends on, beside the plan it is kept in.
#[derive(Eq, Hash, PartialEq)]
struct RouteKey {
    zone_id: Arc<str>,
    mobility: &'static str,
    start: Option<(u64, u64)>,
    destination: (u64, u64),
}

/// What `_geometry(world)` and `_spatial_constraints(world)` return.
#[derive(Default)]
pub(crate) struct Geo {
    pub(crate) layout: Option<Layout>,
    pub(crate) constraints: Option<Constraints>,
    /// `plan_in_room_route`'s memo. A route is a pure function of this plan and its key.
    routes: Mutex<HashMap<RouteKey, Option<Arc<[Pt]>>, FxBuildHasher>>,
}

/// `_zone_center` of one polygon.
fn zone_center(polygon: &[Pt]) -> Result<Option<Pt>, ()> {
    let crosses: Vec<f64> = sides(polygon).map(|(a, b)| a.x * b.y - b.x * a.y).collect();
    let twice_area = py_sum(&crosses);
    let count = polygon.len() as f64;
    let candidate = if twice_area.abs() < 1e-12 {
        let xs: Vec<f64> = polygon.iter().map(|point| point.x).collect();
        let ys: Vec<f64> = polygon.iter().map(|point| point.y).collect();
        Pt { x: py_sum(&xs) / count, y: py_sum(&ys) / count }
    } else {
        let xs: Vec<f64> = sides(polygon).map(|(a, b)| (a.x + b.x) * (a.x * b.y - b.x * a.y)).collect();
        let ys: Vec<f64> = sides(polygon).map(|(a, b)| (a.y + b.y) * (a.x * b.y - b.x * a.y)).collect();
        Pt { x: py_sum(&xs) / (3.0 * twice_area), y: py_sum(&ys) / (3.0 * twice_area) }
    };
    if !candidate.x.is_finite() || !candidate.y.is_finite() {
        return Err(());
    }
    if point_in_polygon(candidate, polygon) {
        return Ok(Some(candidate));
    }
    // The area centroid of a concave room may lie outside its walls.
    let orientation = if twice_area > 0.0 { 1.0 } else { -1.0 };
    let extent = |value: fn(&Pt) -> f64| {
        let values = polygon.iter().map(value);
        let highest = values.clone().fold(f64::NEG_INFINITY, py_max);
        highest - values.fold(f64::INFINITY, py_min)
    };
    let scale = py_max(py_max(extent(|point| point.x), extent(|point| point.y)), 1.0);
    for (first, second) in sides(polygon) {
        let (dx, dy) = (second.x - first.x, second.y - first.y);
        let length = hypot(&[dx, dy]);
        if length <= 1e-12 {
            continue;
        }
        for fraction in [1e-7, 1e-5, 1e-3, 1e-2] {
            let offset = scale * fraction;
            let candidate = Pt {
                x: (first.x + second.x) / 2.0 - orientation * dy / length * offset,
                y: (first.y + second.y) / 2.0 + orientation * dx / length * offset,
            };
            if !candidate.x.is_finite() || !candidate.y.is_finite() {
                return Err(());
            }
            if point_in_polygon(candidate, polygon) {
                return Ok(Some(candidate));
            }
        }
    }
    Ok(None)
}

fn point_of(model: &Bound<'_, PyAny>) -> PyResult<Pt> {
    let py = model.py();
    let x = exact_float(&attribute(model, intern!(py, "x"))?)?;
    let y = exact_float(&attribute(model, intern!(py, "y"))?)?;
    if !usable(x, y) {
        return unsupported("a coordinate is too large");
    }
    Ok(Pt { x, y })
}

fn tuple_of<'py>(model: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyTuple>> {
    attribute(model, name)?.downcast_into_exact::<PyTuple>().map_err(|_| Unsupported::new_err("not a tuple"))
}

fn points_of(model: &Bound<'_, PyAny>, name: &Bound<'_, PyString>) -> PyResult<Vec<Pt>> {
    tuple_of(model, name)?.iter().map(|point| point_of(&point)).collect()
}

fn id_of(model: &Bound<'_, PyAny>, name: &Bound<'_, PyString>) -> PyResult<Arc<str>> {
    Ok(Arc::from(exact_str(&attribute(model, name)?)?))
}

fn flag_of(model: &Bound<'_, PyAny>, name: &Bound<'_, PyString>) -> PyResult<bool> {
    attribute(model, name)?
        .downcast_exact::<pyo3::types::PyBool>()
        .map(|flag| flag.is_true())
        .map_err(|_| Unsupported::new_err("not a bool"))
}

fn layout_of(model: &Bound<'_, PyAny>) -> PyResult<Layout> {
    let py = model.py();
    let mut layout = Layout { zones: IdMap::default(), routes: IdMap::default(), facility_points: IdMap::default() };
    for zone in tuple_of(model, intern!(py, "zones"))?.iter() {
        let polygon = points_of(&zone, intern!(py, "polygon"))?;
        if polygon.is_empty() {
            return unsupported("a zone has no polygon");
        }
        let usable_area = exact_float(&attribute(&zone, intern!(py, "usable_area_m2"))?)?;
        let center = zone_center(&polygon);
        // The first of a repeated ID, as `next(...)` finds; the model refuses a repeat anyway.
        layout.zones.entry(id_of(&zone, intern!(py, "zone_id"))?).or_insert(Zone { polygon, usable_area, center });
    }
    for route in tuple_of(model, intern!(py, "routes"))?.iter() {
        let points = points_of(&route, intern!(py, "points"))?;
        if points.is_empty() {
            return unsupported("a route has no points");
        }
        let width = exact_float(&attribute(&route, intern!(py, "width_m"))?)?;
        let accessible = flag_of(&route, intern!(py, "accessible"))?;
        layout.routes.entry(id_of(&route, intern!(py, "edge_id"))?).or_insert(Route { points, width, accessible });
    }
    let points = attribute(model, intern!(py, "facility_points"))?;
    for (id, point) in exact_dict(&points)?.iter() {
        layout.facility_points.insert(Arc::from(exact_str(&id)?), point_of(&point)?);
    }
    Ok(layout)
}

fn constraints_of(model: &Bound<'_, PyAny>) -> PyResult<Constraints> {
    let py = model.py();
    let mut blocked = Vec::new();
    for item in tuple_of(model, intern!(py, "obstacles"))?.iter() {
        let polygon = points_of(&item, intern!(py, "polygon"))?;
        if polygon.is_empty() {
            return unsupported("an obstacle has no polygon");
        }
        blocked.push(polygon);
    }
    let mut footprints = Vec::new();
    for item in tuple_of(model, intern!(py, "equipment_footprints"))?.iter() {
        let polygon = points_of(&item, intern!(py, "polygon"))?;
        if polygon.is_empty() {
            return unsupported("a footprint has no polygon");
        }
        blocked.push(polygon.clone());
        footprints.push((id_of(&item, intern!(py, "facility_id"))?, polygon));
    }
    let mut walls = Vec::new();
    for wall in tuple_of(model, intern!(py, "walls"))?.iter() {
        walls.push(Wall {
            id: id_of(&wall, intern!(py, "id"))?,
            start: point_of(&attribute(&wall, intern!(py, "start"))?)?,
            end: point_of(&attribute(&wall, intern!(py, "end"))?)?,
        });
    }
    let mut openings = Vec::new();
    for opening in tuple_of(model, intern!(py, "openings"))?.iter() {
        let edge_ids = tuple_of(&opening, intern!(py, "edge_ids"))?;
        openings.push(Opening {
            wall_id: id_of(&opening, intern!(py, "wall_id"))?,
            center: point_of(&attribute(&opening, intern!(py, "center"))?)?,
            width: exact_float(&attribute(&opening, intern!(py, "width_m"))?)?,
            accessible: flag_of(&opening, intern!(py, "accessible"))?,
            edge_ids: edge_ids.iter().map(|id| Ok(Arc::from(exact_str(&id)?))).collect::<PyResult<_>>()?,
        });
    }
    Ok(Constraints { walls, openings, blocked, footprints })
}

// ---- Which plan a world has now --------------------------------------------------------------

struct PythonParts {
    parsed_config: Py<PyAny>,
    effective_geometry: Py<PyAny>,
    effective_constraints: Py<PyAny>,
    geometry_model: Py<PyAny>,
    constraints_model: Py<PyAny>,
    empty_constraints: Py<PyAny>,
    config_scope: Py<PyAny>,
}

static PYTHON: GILOnceCell<PythonParts> = GILOnceCell::new();

fn python(py: Python<'_>) -> PyResult<&'static PythonParts> {
    PYTHON.get_or_try_init(py, || {
        let overlay = py.import("sheltersim.domain.geometry_overlay")?;
        let extensions = py.import("sheltersim.contracts.extensions")?;
        let spatial = py.import("sheltersim.contracts.spatial")?;
        Ok(PythonParts {
            parsed_config: overlay.getattr("_parsed_config")?.unbind(),
            effective_geometry: overlay.getattr("effective_geometry")?.unbind(),
            effective_constraints: overlay.getattr("effective_spatial_constraints")?.unbind(),
            geometry_model: extensions.getattr("FacilityGeometry")?.unbind(),
            constraints_model: spatial.getattr("SpatialConstraints")?.unbind(),
            empty_constraints: overlay.getattr("_EMPTY_SPATIAL_CONSTRAINTS")?.unbind(),
            config_scope: overlay.getattr("_CONFIG_SCOPE")?.unbind(),
        })
    })
}

/// What a plan is made of. Parsed configs are compared by identity, as `_parsed_config` keeps
/// them: in a scope one object for as long as the raw config stands, and outside a scope a new one
/// at each call, which reads a config edited in place as a Python rule would.
struct Plan {
    geometry: Option<Py<PyAny>>,
    constraints: Option<Py<PyAny>>,
    moves: Vec<u8>,
    partitions: Vec<u8>,
    geo: Arc<Geo>,
}

/// Kept across ticks and worlds; the handles are here so an object's address cannot be reused.
static PLANS: Mutex<Vec<Plan>> = Mutex::new(Vec::new());

fn same_object(kept: &Option<Py<PyAny>>, now: &Option<Bound<'_, PyAny>>) -> bool {
    match (kept, now) {
        (None, None) => true,
        (Some(kept), Some(now)) => kept.as_ptr() == now.as_ptr(),
        _ => false,
    }
}

/// The bytes of a part of the overlay: empty for no entries, so most ticks encode nothing.
fn overlay_bytes(overlay: Option<&Bound<'_, PyDict>>, name: &Bound<'_, PyString>) -> PyResult<Vec<u8>> {
    let Some(part) = overlay.map(|overlay| overlay.get_item(name)).transpose()?.flatten() else {
        return Ok(Vec::new());
    };
    if exact_dict(&part)?.is_empty() {
        return Ok(Vec::new());
    }
    crate::encode(&part)
}

/// `_parsed_config`, whose error the Python rule raises itself.
fn parsed<'py>(
    functions: &PythonParts,
    name: &Bound<'py, PyString>,
    raw: &Bound<'py, PyAny>,
    model: &Py<PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = name.py();
    // What `_parsed_config` finds in its scope, where a tick is. Anything else is its own to do.
    let scope = functions.config_scope.bind(py).call_method0(intern!(py, "get"))?;
    if let Ok(scope) = scope.downcast_exact::<PyDict>() {
        if let Some(entry) = scope.get_item((name, raw.as_ptr() as usize))? {
            if let Ok(entry) = entry.downcast_exact::<PyTuple>() {
                if entry.get_item(0)?.is(raw) {
                    return entry.get_item(1);
                }
            }
        }
    }
    functions
        .parsed_config
        .bind(py)
        .call1((name, raw, model.bind(py)))
        .map_err(|_| Unsupported::new_err("the Python rule's parse raises"))
}

impl Geo {
    /// The plan of this world now. Unsupported for what the Python functions raise on.
    pub(crate) fn of(world: &Bound<'_, PyAny>) -> PyResult<Arc<Geo>> {
        let py = world.py();
        let functions = python(py)?;
        let configs = attribute(world, intern!(py, "model_configs"))?;
        let configs = exact_dict(&configs)?;
        let table = attribute(world, intern!(py, "model_state"))?;
        let overlay_value = exact_dict(&table)?.get_item(OVERLAY_KEY)?;
        let geometry = match configs.get_item(intern!(py, "facility_geometry"))? {
            Some(raw) if !raw.is_none() => Some(parsed(functions, intern!(py, "facility_geometry"), &raw, &functions.geometry_model)?),
            _ => None,
        };
        let raw_constraints = configs.get_item(intern!(py, "spatial_constraints"))?;
        let constraints = if raw_constraints.is_some() || overlay_value.is_some() {
            let raw = raw_constraints.unwrap_or_else(|| functions.empty_constraints.bind(py).clone());
            Some(parsed(functions, intern!(py, "spatial_constraints"), &raw, &functions.constraints_model)?)
        } else {
            None
        };
        let overlay = match &overlay_value {
            Some(value) => Some(exact_dict(value)?),
            None => None,
        };
        let moves = overlay_bytes(overlay, intern!(py, "facility_points"))?;
        let partitions = overlay_bytes(overlay, intern!(py, "partitions"))?;
        {
            let mut plans = PLANS.lock().map_err(|_| Unsupported::new_err("the plans are poisoned"))?;
            let found = plans.iter().position(|plan| {
                same_object(&plan.geometry, &geometry)
                    && same_object(&plan.constraints, &constraints)
                    && plan.moves == moves
                    && plan.partitions == partitions
            });
            if let Some(found) = found {
                let geo = plans[found].geo.clone();
                if found != 0 {
                    let plan = plans.remove(found);
                    plans.insert(0, plan);
                }
                return Ok(geo);
            }
        }
        // The effective models, which run Python, outside the lock.
        let refuse = |_| Unsupported::new_err("the Python rule's effective plan raises");
        let layout = match &geometry {
            Some(_) => Some(layout_of(&functions.effective_geometry.bind(py).call1((world,)).map_err(refuse)?)?),
            None => None,
        };
        let built = match &constraints {
            Some(_) => Some(constraints_of(&functions.effective_constraints.bind(py).call1((world,)).map_err(refuse)?)?),
            None => None,
        };
        let geo = Arc::new(Geo { layout, constraints: built, routes: Mutex::default() });
        let mut plans = PLANS.lock().map_err(|_| Unsupported::new_err("the plans are poisoned"))?;
        plans.insert(
            0,
            Plan {
                geometry: geometry.map(Bound::unbind),
                constraints: constraints.map(Bound::unbind),
                moves,
                partitions,
                geo: geo.clone(),
            },
        );
        // Dropped past the lock, since a release can run Python.
        let keep = plans.len().min(CACHED_PLANS);
        let old = plans.split_off(keep);
        drop(plans);
        drop(old);
        Ok(geo)
    }

    /// `_zone_center`. Unsupported when Python would raise.
    pub(crate) fn zone_center(&self, zone_id: &str) -> PyResult<Option<Pt>> {
        let Some(zone) = self.layout.as_ref().and_then(|layout| layout.zones.get(zone_id)) else {
            return Ok(None);
        };
        zone.center.map_err(|()| Unsupported::new_err("the centre of a zone is not finite"))
    }

    /// `_room_segment_clear`, for a zone polygon.
    pub(crate) fn room_segment_clear(constraints: &Constraints, start: Pt, end: Pt, polygon: &[Pt], mobility: &str) -> bool {
        let Some(clearance) = walking_clearance(mobility) else {
            return false;
        };
        if !segment_inside_polygon(start, end, polygon) {
            return false;
        }
        if constraints.blocked.iter().any(|blocked| !segment_clear_of_polygon(start, end, blocked, clearance)) {
            return false;
        }
        constraints.walls.iter().all(|wall| {
            let Some(crossing) = segment_intersection(start, end, wall.start, wall.end) else {
                return true;
            };
            constraints.openings.iter().any(|item| {
                item.wall_id == wall.id
                    && item.width + 1e-9 >= 2.0 * clearance
                    && (item.accessible || mobility == "independent")
                    && distance(crossing, item.center) <= item.width / 2.0 - clearance + 1e-8
            })
        })
    }

    /// `route_is_physically_clear`.
    pub(crate) fn route_is_clear(&self, edge_id: &str, mobility: &str) -> bool {
        let Some(layout) = &self.layout else {
            return true;
        };
        let route = layout.routes.get(edge_id);
        let clearance = walking_clearance(mobility);
        let (Some(route), Some(clearance)) = (route, clearance) else {
            return false;
        };
        if !route.accessible && (mobility == "wheelchair" || mobility == "assisted") {
            return false;
        }
        if route.width + 1e-9 < 2.0 * clearance {
            return false;
        }
        let Some(constraints) = &self.constraints else {
            return true;
        };
        let openings: Vec<&Opening> =
            constraints.openings.iter().filter(|item| item.edge_ids.iter().any(|id| &**id == edge_id)).collect();
        for pair in route.points.windows(2) {
            let (first, second) = (pair[0], pair[1]);
            if constraints.blocked.iter().any(|polygon| !segment_clear_of_polygon(first, second, polygon, clearance)) {
                return false;
            }
            for wall in &constraints.walls {
                let Some(crossing) = segment_intersection(first, second, wall.start, wall.end) else {
                    continue;
                };
                let allowed = openings.iter().any(|opening| {
                    opening.wall_id == wall.id
                        && (opening.accessible || mobility == "independent")
                        && opening.width + 1e-9 >= 2.0 * clearance
                        && distance(crossing, opening.center) <= opening.width / 2.0 - clearance + 1e-9
                });
                if !allowed {
                    return false;
                }
            }
        }
        true
    }
}

impl Geo {
    /// `plan_in_room_route`. Err for what the Python function would raise or a kernel doesn't decide.
    pub(crate) fn room_route(
        &self,
        zone_id: &str,
        start: Option<Pt>,
        destination: Pt,
        mobility: &'static str,
    ) -> Result<Option<Arc<[Pt]>>, &'static str> {
        let key = RouteKey {
            zone_id: Arc::from(zone_id),
            mobility,
            start: start.map(|point| (point.x.to_bits(), point.y.to_bits())),
            destination: (destination.x.to_bits(), destination.y.to_bits()),
        };
        if let Ok(routes) = self.routes.lock() {
            if let Some(route) = routes.get(&key) {
                return Ok(route.clone());
            }
        }
        let route = self.find_room_route(zone_id, start, destination, mobility)?;
        let route: Option<Arc<[Pt]>> = route.map(Arc::from);
        if let Ok(mut routes) = self.routes.lock() {
            if routes.len() >= REMEMBERED_ROUTES {
                routes.clear();
            }
            routes.insert(key, route.clone());
        }
        Ok(route)
    }

    /// `_plan_in_room_route`.
    fn find_room_route(
        &self,
        zone_id: &str,
        start: Option<Pt>,
        destination: Pt,
        mobility: &str,
    ) -> Result<Option<Vec<Pt>>, &'static str> {
        const REFUSED: &str = "the centre of a zone is not finite";
        let Some(zone) = self.layout.as_ref().and_then(|layout| layout.zones.get(zone_id)) else {
            return Ok(None);
        };
        let origin = match start {
            Some(start) => Some(start),
            None => zone.center.map_err(|()| REFUSED)?,
        };
        let Some(constraints) = &self.constraints else {
            return Ok(origin.filter(|origin| segment_inside_polygon(*origin, destination, &zone.polygon)).map(|origin| vec![origin, destination]));
        };
        let Some(clearance) = walking_clearance(mobility) else {
            return Ok(None);
        };
        let Some(origin) = origin else {
            return Ok(None);
        };
        let walkable = |point: Pt| point_walkable(point, &zone.polygon, &constraints.blocked);
        if !walkable(origin) || !walkable(destination) {
            return Ok(None);
        }
        let mut nodes = vec![origin, destination];
        let clearance = clearance + 0.05;
        for polygon in &constraints.blocked {
            let area = py_fsum(&sides(polygon).map(|(a, b)| a.x * b.y - b.x * a.y).collect::<Vec<f64>>())
                .ok_or("an area is not a plain sum")?;
            let orientation = if area > 0.0 { 1.0 } else { -1.0 };
            let count = polygon.len();
            for (index, &corner) in polygon.iter().enumerate() {
                let previous = polygon[(index + count - 1) % count];
                let following = polygon[(index + 1) % count];
                let incoming = (corner.x - previous.x, corner.y - previous.y);
                let outgoing = (following.x - corner.x, following.y - corner.y);
                let turn = incoming.0 * outgoing.1 - incoming.1 * outgoing.0;
                if turn * orientation <= 1e-10 {
                    continue;
                }
                let first_length = hypot(&[incoming.0, incoming.1]);
                let second_length = hypot(&[outgoing.0, outgoing.1]);
                if first_length <= 1e-9 || second_length <= 1e-9 {
                    continue;
                }
                let normal_first = (orientation * incoming.1 / first_length, -orientation * incoming.0 / first_length);
                let normal_second = (orientation * outgoing.1 / second_length, -orientation * outgoing.0 / second_length);
                let miter = (normal_first.0 + normal_second.0, normal_first.1 + normal_second.1);
                let miter_length = hypot(&[miter.0, miter.1]);
                if miter_length <= 1e-9 {
                    continue;
                }
                let miter = (miter.0 / miter_length, miter.1 / miter_length);
                let scale = clearance / (miter.0 * normal_first.0 + miter.1 * normal_first.1);
                if !scale.is_finite() || scale <= 0.0 || scale > 20.0 {
                    continue;
                }
                let candidate = Pt { x: corner.x + miter.0 * scale, y: corner.y + miter.1 * scale };
                if walkable(candidate) {
                    nodes.push(candidate);
                }
            }
        }
        for wall in &constraints.walls {
            for opening in constraints.openings.iter().filter(|item| item.wall_id == wall.id) {
                let (dx, dy) = (wall.end.x - wall.start.x, wall.end.y - wall.start.y);
                let length = hypot(&[dx, dy]);
                if length <= 1e-9 {
                    continue;
                }
                let (nx, ny) = (-dy / length * clearance, dx / length * clearance);
                for side in [-1.0, 1.0] {
                    let candidate = Pt { x: opening.center.x + nx * side, y: opening.center.y + ny * side };
                    if walkable(candidate) {
                        nodes.push(candidate);
                    }
                }
            }
        }
        let key_of = |point: Pt| -> Result<(f64, f64), &'static str> {
            Ok((py_round(point.x, 8).ok_or("a round is not plain")?, py_round(point.y, 8).ok_or("a round is not plain")?))
        };
        // {key: point} of the Python rule: the later point takes a key's place, and -0.0 is 0.0.
        let mut unique: Vec<((f64, f64), Pt)> = Vec::with_capacity(nodes.len());
        let mut put = |key: (f64, f64), point: Pt| match unique.iter_mut().find(|(known, _)| *known == key) {
            Some(slot) => slot.1 = point,
            None => unique.push((key, point)),
        };
        for &point in &nodes {
            put(key_of(point)?, point);
        }
        let (origin_key, destination_key) = (key_of(origin)?, key_of(destination)?);
        put(destination_key, destination);
        put(origin_key, origin);
        unique.sort_by(|first, second| first.0.partial_cmp(&second.0).unwrap_or(Ordering::Equal));
        let start_index = unique.iter().position(|(key, _)| *key == origin_key).ok_or("no origin")?;
        let end_index = unique.iter().position(|(key, _)| *key == destination_key).ok_or("no destination")?;
        let nodes: Vec<Pt> = unique.iter().map(|(_, point)| *point).collect();
        let mut graph: Vec<Vec<(f64, u32)>> = vec![Vec::new(); nodes.len()];
        for left in 0..nodes.len() {
            for right in left + 1..nodes.len() {
                if Self::room_segment_clear(constraints, nodes[left], nodes[right], &zone.polygon, mobility) {
                    let length = distance(nodes[left], nodes[right]);
                    graph[left].push((length, right as u32));
                    graph[right].push((length, left as u32));
                }
            }
        }
        for adjacent in &mut graph {
            adjacent.sort_by_key(|item| item.1);
        }
        let mut best: Vec<Option<(f64, Vec<u32>)>> = vec![None; nodes.len()];
        best[start_index] = Some((0.0, vec![start_index as u32]));
        let mut pending = BinaryHeap::new();
        pending.push(Walk { distance: 0.0, path: vec![start_index as u32], current: start_index as u32 });
        while let Some(Walk { distance: walked, path, current }) = pending.pop() {
            let at = current as usize;
            if best[at].as_ref().is_none_or(|(known, known_path)| *known != walked || *known_path != path) {
                continue;
            }
            if at == end_index {
                return Ok(Some(path.iter().map(|index| nodes[*index as usize]).collect()));
            }
            for &(weight, adjacent) in &graph[at] {
                let next_distance = walked + weight;
                let mut next_path = path.clone();
                next_path.push(adjacent);
                let better = match &best[adjacent as usize] {
                    None => true,
                    Some((known, known_path)) => {
                        next_distance < *known || (next_distance == *known && next_path < *known_path)
                    }
                };
                if better {
                    best[adjacent as usize] = Some((next_distance, next_path.clone()));
                    pending.push(Walk { distance: next_distance, path: next_path, current: adjacent });
                }
            }
        }
        Ok(None)
    }
}

/// `_point_walkable`.
pub(crate) fn point_walkable(point: Pt, zone: &[Pt], blocked: &[Vec<Pt>]) -> bool {
    point_in_polygon(point, zone) && !blocked.iter().any(|polygon| point_in_polygon(point, polygon))
}

/// An entry of the Python rule's heap: the least `(distance, path, current)` comes first.
struct Walk {
    distance: f64,
    path: Vec<u32>,
    current: u32,
}

impl Walk {
    fn order(&self, other: &Self) -> Ordering {
        self.distance
            .partial_cmp(&other.distance)
            .unwrap_or(Ordering::Equal)
            .then_with(|| self.path.cmp(&other.path))
            .then_with(|| self.current.cmp(&other.current))
    }
}

impl PartialEq for Walk {
    fn eq(&self, other: &Self) -> bool {
        self.order(other) == Ordering::Equal
    }
}

impl Eq for Walk {}

impl PartialOrd for Walk {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Walk {
    /// Reversed, since `BinaryHeap` pops the greatest.
    fn cmp(&self, other: &Self) -> Ordering {
        other.order(self)
    }
}

/// `_polyline_length`. None when `math.fsum` is not the plain case.
pub(crate) fn polyline_length(points: &[Pt]) -> Option<f64> {
    let lengths: Vec<f64> = points.windows(2).map(|pair| distance(pair[0], pair[1])).collect();
    py_fsum(&lengths)
}

/// Nothing to register: `rules/movement.rs` is what Python calls.
pub fn register(_m: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
