//! `domain/movement.py`: the rules of a tick. The Python rules stay as the comparison base and for
//! every value these kernels do not decide, which is also how a bad value gets its error text
//! (docs/rust-port/lanes/m4-movement.md).
//!
//! Every kernel plans first: it reads the rows and the plan of the floor and works out each record,
//! with nothing changed, and says Unsupported for anything it does not decide. Then it writes.
//! Planning reads only what the tick started with, so one person's result depends on nobody else's.
//! That is why `advance_movement` refuses the branches where it would: a queue trip that must
//! replan, and a person placed in a queue slot, read and change the others' state in the serial
//! order of the Python rule (docs/rust-port/lanes/i-movement.md), so the whole tick goes to Python.
//!
//! No Python runs in the plan or the writes but `Geo::of`, before them, and reads of plain data.

use std::sync::{Arc, LazyLock};

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyInt, PyString, PyTuple, PyType};

use crate::core::mathx::{py_fsum, py_max, py_min, py_sum};
use crate::core::row::{attribute, exact_dict, exact_float, exact_int, exact_list, exact_str, map_set, unsupported, Field};
use crate::core::world_core::{mirror_of, NamespaceMirror, Tree};
use crate::rules::movement_geometry::{
    distance, point_along_polyline, point_in_polygon, point_walkable, polyline_length, usable, Geo, Pt,
};
use crate::state::entities::{entities_of, Activity, AgentRow, Entities, EntityStore};
use crate::state::events::QueueStore;
use crate::state::movement::{Journey, MovementRow, Point, Position};
use crate::Unsupported;

const KEY: &str = "movement_v1";
const OVERLAY_KEY: &str = "geometry_overlay_v1";
const POLICY_VERSION: &str = "spatial-route-and-room-waypoint-v2";
/// Above this an int does not always fit a float, and a Python division of ints is not an f64 one.
const EXACT_INT: i64 = 1 << 53;

// `_MOVEMENT_DEFAULTS`.
const SPEED_INDEPENDENT: f64 = 72.0;
const SPEED_ASSISTED: f64 = 36.0;
const SPEED_WHEELCHAIR: f64 = 48.0;
const DENSITY_SLOWDOWN: f64 = 0.12;
const MAXIMUM_SLOWDOWN: f64 = 2.5;
const MOVEMENT_SOURCE_ID: &str = "ASM-SPATIAL-MOVEMENT-V2";
// `_queue_slot_position`'s spacing and `facility_approach_route`'s clearance, in metres.
const SPACING_INDEPENDENT: f64 = 0.6;
const SPACING_ASSISTED: f64 = 0.9;
const SPACING_WHEELCHAIR: f64 = 1.0;
const APPROACH_INDEPENDENT: f64 = 0.30;
const APPROACH_ASSISTED: f64 = 0.45;
const APPROACH_WHEELCHAIR: f64 = 0.50;

struct Kinds {
    physical: Arc<str>,
    display_only: Arc<str>,
    unavailable: Arc<str>,
}

static KINDS: LazyLock<Kinds> = LazyLock::new(|| Kinds {
    physical: Arc::from("physical"),
    display_only: Arc::from("display_only"),
    unavailable: Arc::from("unavailable"),
});

fn is_queue_trip(purpose: &str) -> bool {
    matches!(
        purpose,
        "toilet_queue_position" | "resource_queue_position" | "toilet_queue_service" | "resource_queue_service"
    )
}

fn point_row(point: Pt) -> Point {
    Point { x: point.x, y: point.y }
}

fn same_position(first: &Position, second: &Position) -> bool {
    let same_point = match (&first.point, &second.point) {
        (None, None) => true,
        (Some(a), Some(b)) => a.x.to_bits() == b.x.to_bits() && a.y.to_bits() == b.y.to_bits(),
        _ => false,
    };
    same_point
        && first.progress.to_bits() == second.progress.to_bits()
        && first.zone_id == second.zone_id
        && first.edge_id == second.edge_id
        && first.position_kind == second.position_kind
}

/// `_position_record`, with its kind.
fn record(zone_id: Arc<str>, point: Option<Pt>, position_kind: Arc<str>, edge_id: Option<Arc<str>>, progress: f64) -> Position {
    Position { zone_id, point: point.map(point_row), edge_id, progress, position_kind }
}

/// The physical-or-unavailable kind of a person on a route.
fn on_route_kind(point: &Option<Pt>) -> Arc<str> {
    if point.is_some() {
        KINDS.physical.clone()
    } else {
        KINDS.unavailable.clone()
    }
}

/// What a tick's kernel reads beside the rows.
struct Scene<'py> {
    world: Bound<'py, PyAny>,
    geo: Arc<Geo>,
    zones: Bound<'py, PyAny>,
    edges: Bound<'py, PyAny>,
    overlay: Option<Bound<'py, PyAny>>,
}

impl<'py> Scene<'py> {
    fn of(world: &Bound<'py, PyAny>) -> PyResult<Self> {
        let py = world.py();
        let table = attribute(world, intern!(py, "model_state"))?;
        let overlay = exact_dict(&table)?.get_item(OVERLAY_KEY)?;
        Ok(Self {
            world: world.clone(),
            geo: Geo::of(world)?,
            zones: attribute(world, intern!(py, "zones"))?,
            edges: attribute(world, intern!(py, "zone_edges"))?,
            overlay,
        })
    }

    fn edge(&self, edge_id: &str) -> PyResult<Bound<'py, PyAny>> {
        let edges = exact_dict(&self.edges)?;
        edges.get_item(edge_id)?.ok_or_else(|| Unsupported::new_err("a journey's edge is unknown"))
    }

    fn edge_minutes(&self, edge_id: &str) -> PyResult<i64> {
        let minutes = exact_int(&attribute(&self.edge(edge_id)?, intern!(self.world.py(), "travel_minutes"))?)?;
        if minutes <= 0 || minutes >= EXACT_INT {
            return unsupported("an edge's minutes are not a plain duration");
        }
        Ok(minutes)
    }

    /// `_route_point`.
    fn route_point(&self, edge_id: &str, progress: f64, current_zone: &str) -> PyResult<Option<Pt>> {
        let Some(layout) = &self.geo.layout else {
            return Ok(None);
        };
        let Some(route) = layout.routes.get(edge_id) else {
            return self.geo.zone_center(current_zone);
        };
        let py = self.world.py();
        let from_zone = attribute(&self.edge(edge_id)?, intern!(py, "from_zone_id"))?;
        let forward = exact_str(&from_zone)? == current_zone;
        let at = |index: usize| {
            if forward {
                route.points[index]
            } else {
                route.points[route.points.len() - 1 - index]
            }
        };
        let segments = route.points.len() - 1;
        let lengths: Vec<f64> = (0..segments).map(|index| distance(at(index), at(index + 1))).collect();
        let total = py_sum(&lengths);
        if total <= 0.0 {
            return Ok(Some(at(0)));
        }
        let mut remaining = py_min(1.0, py_max(0.0, progress)) * total;
        for (index, &length) in lengths.iter().enumerate() {
            if remaining <= length || index == segments - 1 {
                let ratio = if length == 0.0 { 0.0 } else { py_min(1.0, py_max(0.0, remaining / length)) };
                let (first, second) = (at(index), at(index + 1));
                let point = Pt { x: first.x + (second.x - first.x) * ratio, y: first.y + (second.y - first.y) * ratio };
                return Ok(Some(point));
            }
            remaining -= length;
        }
        Ok(Some(at(segments)))
    }

    /// `_reserved_bed_point`.
    fn reserved_bed_point(&self, agent_id: &str) -> PyResult<Option<Pt>> {
        let py = self.world.py();
        let Some(overlay) = &self.overlay else {
            return Ok(None);
        };
        let Some(overlay) = crate::core::tracked::as_readable_dict(overlay) else {
            return unsupported("the overlay is not a plain dict");
        };
        let Some(reservations) = overlay.get_item(intern!(py, "bed_reservations"))? else {
            return Ok(None);
        };
        let Some(reservation) = exact_dict(&reservations)?.get_item(agent_id)? else {
            return Ok(None);
        };
        let Some(reservation) = crate::core::tracked::as_readable_dict(&reservation) else {
            return if reservation.is_none() { Ok(None) } else { unsupported("a reservation is not a plain dict") };
        };
        let Some(rooms) = overlay.get_item(intern!(py, "temporary_rooms"))? else {
            return Ok(None);
        };
        let Some(room_id) = reservation.get_item(intern!(py, "room_id"))? else {
            return Ok(None);
        };
        let Some(room) = exact_dict(&rooms)?.get_item(exact_str(&room_id)?)? else {
            return Ok(None);
        };
        let Some(beds) = exact_dict(&room)?.get_item(intern!(py, "bed_points_m"))? else {
            return Ok(None);
        };
        let beds = exact_list(&beds)?;
        let Some(index) = reservation.get_item(intern!(py, "bed_index"))? else {
            return Ok(None);
        };
        let Ok(index) = index.downcast_exact::<PyInt>() else {
            return Ok(None);
        };
        let Ok(index) = index.extract::<usize>() else {
            return Ok(None);
        };
        if index >= beds.len() {
            return Ok(None);
        }
        Ok(Some(plain_point(&beds.get_item(index)?)?))
    }

    /// `_set_stationary_position`'s record. A person in a queue slot reads the people ahead, so
    /// `slots` is what they are read from, or None where who walked first would matter.
    fn stationary(&self, agent_id: &str, agent: &AgentRow, slots: Option<(&MovementRow, &Entities)>) -> PyResult<Position> {
        let py = self.world.py();
        let mut point = None;
        let target = agent.target_id.as_deref().filter(|target| !target.is_empty());
        let placed = self.geo.layout.as_ref();
        if let Some(layout) = placed {
            if agent.activity == Activity::Queued && target.is_some() {
                let Some((row, rows)) = slots else {
                    return unsupported("a person stands in a queue slot, which depends on the people ahead");
                };
                point = queue_slot(&self.geo, row, rows, agent_id, target.unwrap_or_default())?;
            } else if agent.activity == Activity::InService && target.is_some() {
                point = target.and_then(|target| layout.facility_points.get(target)).copied();
            } else if agent.activity == Activity::Sleeping {
                point = self.reserved_bed_point(agent_id)?;
            }
            if point.is_none() {
                point = self.geo.zone_center(&agent.zone_id)?;
            }
        } else if let Some(zone) = exact_dict(&self.zones)?.get_item(&*agent.zone_id)? {
            let x = attribute(&zone, intern!(py, "display_x"))?;
            let y = attribute(&zone, intern!(py, "display_y"))?;
            if !x.is_none() && !y.is_none() {
                let (x, y) = (exact_float(&x)?, exact_float(&y)?);
                if !usable(x, y) {
                    return unsupported("a display point is not a usable point");
                }
                point = Some(Pt { x, y });
            }
        }
        let kind = match (placed, &point) {
            (Some(_), Some(_)) => KINDS.physical.clone(),
            (None, Some(_)) => KINDS.display_only.clone(),
            _ => KINDS.unavailable.clone(),
        };
        Ok(record(agent.zone_id.clone(), point, kind, None, 0.0))
    }
}

/// `Point2D.model_validate` of a plain record: only the case where it gives the same two floats.
fn plain_point(value: &Bound<'_, PyAny>) -> PyResult<Pt> {
    let py = value.py();
    let record = exact_dict(value)?;
    if record.len() != 2 {
        return unsupported("a point has other fields");
    }
    let field = |name| -> PyResult<f64> {
        match record.get_item(name)? {
            Some(item) => exact_float(&item),
            None => unsupported("a point lacks a field"),
        }
    };
    let (x, y) = (field(intern!(py, "x"))?, field(intern!(py, "y"))?);
    if !usable(x, y) {
        return unsupported("a point is not a usable point");
    }
    Ok(Pt { x, y })
}

fn points_of_journey(journey: &Journey) -> PyResult<Vec<Pt>> {
    journey
        .route_points
        .iter()
        .map(|point| {
            if usable(point.x, point.y) {
                Ok(Pt { x: point.x, y: point.y })
            } else {
                unsupported("a route point is not a usable point")
            }
        })
        .collect()
}

// ---- Writing a plan ----------------------------------------------------------------------------

/// What a kernel found to change. Applied in this order, each map in its own order.
#[derive(Default)]
struct Changes {
    /// `(record's key, new record)`, dropped when the same bits are there already.
    positions: Vec<(Arc<str>, Position)>,
    gone_positions: Vec<Arc<str>>,
    gone_journeys: Vec<Arc<str>>,
    new_journey: Option<(Arc<str>, Journey)>,
    staff: Vec<(Arc<str>, Position)>,
    /// The edges one step more, in the order of the walk.
    usage: Vec<Arc<str>>,
    minute: Option<i64>,
}

/// One write of the tree, which needs the mirror itself.
enum TreeWrite<'py> {
    Set(&'static str, Arc<str>, Bound<'py, PyAny>),
    Delete(&'static str, Arc<str>),
}

impl Changes {
    /// Put the plan in the row, and the scalars in the tree. Returns what the tree still needs.
    fn apply<'py>(self, row: &mut MovementRow, tree: &Tree<'py>) -> PyResult<Vec<TreeWrite<'py>>> {
        let py = tree.py();
        let mut writes = Vec::new();
        if let Some(minute) = self.minute {
            row.set_last_updated_minute(tree, minute)?;
        }
        for id in self.gone_journeys {
            if row.journeys.shift_remove(&*id).is_some() {
                writes.push(TreeWrite::Delete("journeys", id));
            }
        }
        for id in self.gone_positions {
            if row.positions.shift_remove(&*id).is_some() {
                writes.push(TreeWrite::Delete("positions", id));
            }
        }
        if let Some((id, journey)) = self.new_journey {
            writes.push(TreeWrite::Set("journeys", id.clone(), Field::to_py(&journey, py)?));
            row.journeys.insert(id, journey);
        }
        for (name, map, changes) in
            [("positions", &mut row.positions, self.positions), ("staff_positions", &mut row.staff_positions, self.staff)]
        {
            for (id, position) in changes {
                if map.get(&*id).is_some_and(|current| same_position(current, &position)) {
                    continue;
                }
                writes.push(TreeWrite::Set(name, id.clone(), Field::to_py(&position, py)?));
                map.insert(id, position);
            }
        }
        if !self.usage.is_empty() {
            let node = tree.child(intern!(py, "edge_usage_minutes"))?;
            for edge in self.usage {
                let count = row.edge_usage_minutes.get(&*edge).copied().unwrap_or(0) + 1;
                map_set(&mut row.edge_usage_minutes, &node, &edge, count)?;
            }
        }
        Ok(writes)
    }
}

fn write_tree<'py>(mirror: &Bound<'py, NamespaceMirror>, writes: Vec<TreeWrite<'py>>) -> PyResult<()> {
    for write in writes {
        match write {
            TreeWrite::Set(map, id, value) => NamespaceMirror::write_through(mirror, &[map], &id, &value)?,
            TreeWrite::Delete(map, id) => NamespaceMirror::remove_through(mirror, &[map], &id)?,
        }
    }
    Ok(())
}

/// The mirror of a namespace the world really holds: Python stores past the table are not the row's.
fn mirror_held<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, NamespaceMirror>> {
    let py = world.py();
    let mirror = mirror_of(world, KEY)?;
    let table = attribute(world, intern!(py, "model_state"))?;
    let held = exact_dict(&table)?.get_item(KEY)?;
    if !held.is_some_and(|state| mirror.borrow().root_is(&state)) {
        return unsupported("the world holds another movement state");
    }
    Ok(mirror)
}

/// `_state`'s records for the people who have none.
fn missing_positions(row: &MovementRow, rows: &Entities, changes: &mut Changes) {
    for (id, agent) in rows.agents.iter() {
        if !row.positions.contains_key(&**id) {
            changes.positions.push((
                id.clone(),
                record(agent.zone_id.clone(), None, KINDS.unavailable.clone(), None, 0.0),
            ));
        }
    }
}

/// The record of a queue trip's end: `finish_movement`'s branch for it.
fn arrived(current: &Position) -> Option<Position> {
    current.point.map(|point| Position {
        zone_id: current.zone_id.clone(),
        point: Some(point),
        edge_id: None,
        progress: 0.0,
        position_kind: KINDS.physical.clone(),
    })
}

// ---- Queue slots and the walk to a facility --------------------------------------------------

fn refused(reason: &'static str) -> PyErr {
    Unsupported::new_err(reason)
}

fn position_point(position: Option<&Position>) -> Option<Pt> {
    position.and_then(|position| position.point).map(|point| Pt { x: point.x, y: point.y })
}

/// `_queue_slot_position` of a facility's own queue: the slot first free of the people ahead as
/// they stand now, with a real route from where the person is.
fn queue_slot(geo: &Geo, row: &MovementRow, rows: &Entities, agent_id: &str, facility_id: &str) -> PyResult<Option<Pt>> {
    let Some(facility) = rows.facilities.get(facility_id) else {
        return Ok(None);
    };
    let Some(layout) = &geo.layout else {
        return Ok(None);
    };
    let (Some(&facility_point), Some(zone)) = (layout.facility_points.get(facility_id), layout.zones.get(&*facility.zone_id)) else {
        return Ok(None);
    };
    let holds = |queue: &Vec<Arc<str>>| queue.iter().position(|id| &**id == agent_id);
    let (queue, slot_index) = match holds(&facility.resource_queue_agent_ids) {
        Some(slot) => (&facility.resource_queue_agent_ids, Some(slot)),
        None => (&facility.queue_agent_ids, holds(&facility.queue_agent_ids)),
    };
    let Some(slot_index) = slot_index else {
        return Ok(Some(facility_point));
    };
    let Some(anchor) = geo.zone_center(&facility.zone_id)? else {
        return Ok(None);
    };
    let (dx, dy) = (anchor.x - facility_point.x, anchor.y - facility_point.y);
    let length = crate::core::mathx::hypot(&[dx, dy]);
    if length <= 1e-9 {
        return Ok(None);
    }
    let (ux, uy) = (dx / length, dy / length);
    let Some(agent) = rows.agents.get(agent_id) else {
        return unsupported("no such agent");
    };
    let mobility = mobility_text(agent);
    let spacing = match mobility {
        "independent" => SPACING_INDEPENDENT,
        "assisted" => SPACING_ASSISTED,
        _ => SPACING_WHEELCHAIR,
    };
    let occupied: Vec<Pt> =
        queue[..slot_index].iter().filter_map(|other| position_point(row.positions.get(&**other))).collect();
    if occupied.iter().any(|point| !usable(point.x, point.y)) {
        return unsupported("a position is not a usable point");
    }
    let start = position_point(row.positions.get(agent_id)).or(occupied.last().copied()).unwrap_or(anchor);
    if !usable(start.x, start.y) {
        return unsupported("a position is not a usable point");
    }
    let no_blocks = Vec::new();
    let blocked = geo.constraints.as_ref().map_or(&no_blocks, |constraints| &constraints.blocked);
    for ring in slot_index + 1..slot_index + 65 {
        for side in [0.0, 1.0, -1.0, 2.0, -2.0] {
            let along = spacing * ring as f64;
            let across = spacing * side;
            let candidate =
                Pt { x: facility_point.x + ux * along - uy * across, y: facility_point.y + uy * along + ux * across };
            if point_walkable(candidate, &zone.polygon, blocked)
                && occupied.iter().all(|other| distance(candidate, *other) >= spacing)
                && geo.room_route(&facility.zone_id, Some(start), candidate, mobility).map_err(refused)?.is_some()
            {
                return Ok(Some(candidate));
            }
        }
    }
    Ok(None)
}

/// `Point2D(x=..., y=...)`.
fn point_object<'py>(py: Python<'py>, point: Pt) -> PyResult<Bound<'py, PyAny>> {
    static CLASS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    let class = CLASS.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
        Ok(py.import("sheltersim.contracts.extensions")?.getattr("Point2D")?.unbind())
    })?;
    let fields = PyDict::new(py);
    fields.set_item(intern!(py, "x"), point.x)?;
    fields.set_item(intern!(py, "y"), point.y)?;
    class.bind(py).call((), Some(&fields))
}

/// The `Point2D` a kernel was given, or None for None.
fn given_point(value: &Bound<'_, PyAny>) -> PyResult<Option<Pt>> {
    if value.is_none() {
        return Ok(None);
    }
    let py = value.py();
    if value.get_type().name()?.to_str()? != "Point2D" {
        return unsupported("a point is not a Point2D");
    }
    let (x, y) = (exact_float(&attribute(value, intern!(py, "x"))?)?, exact_float(&attribute(value, intern!(py, "y"))?)?);
    if !usable(x, y) {
        return unsupported("a point is not a usable point");
    }
    Ok(Some(Pt { x, y }))
}

/// A mobility as the Python rules pass it, the member or its value.
fn mobility_of(value: &Bound<'_, PyAny>) -> PyResult<&'static str> {
    let text = match value.downcast_exact::<PyString>() {
        Ok(text) => text.to_str().map(str::to_owned),
        Err(_) => Ok(exact_str(&attribute(value, intern!(value.py(), "value"))?)?.to_owned()),
    }
    .map_err(|_: PyErr| Unsupported::new_err("a mobility is not a str"))?;
    match &*text {
        "independent" => Ok("independent"),
        "assisted" => Ok("assisted"),
        "wheelchair" => Ok("wheelchair"),
        _ => unsupported("another mobility"),
    }
}

/// `plan_in_room_route`.
#[pyfunction]
#[pyo3(signature = (world, *, zone_id, start, destination, mobility=None))]
fn plan_in_room_route<'py>(
    world: &Bound<'py, PyAny>,
    zone_id: &Bound<'py, PyAny>,
    start: &Bound<'py, PyAny>,
    destination: &Bound<'py, PyAny>,
    mobility: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let zone_id = exact_str(zone_id)?;
    let start = given_point(start)?;
    let Some(destination) = given_point(destination)? else {
        return unsupported("no destination");
    };
    let mobility = match mobility {
        Some(mobility) => mobility_of(mobility)?,
        None => "independent",
    };
    let geo = Geo::of(world)?;
    match geo.room_route(zone_id, start, destination, mobility).map_err(refused)? {
        Some(route) => {
            let points = route.iter().map(|point| point_object(py, *point)).collect::<PyResult<Vec<_>>>()?;
            Ok(PyTuple::new(py, points)?.into_any())
        }
        None => Ok(py.None().into_bound(py)),
    }
}

/// `route_is_physically_clear`.
#[pyfunction]
#[pyo3(signature = (world, edge_id, *, mobility=None))]
fn route_is_physically_clear(
    world: &Bound<'_, PyAny>,
    edge_id: &Bound<'_, PyAny>,
    mobility: Option<&Bound<'_, PyAny>>,
) -> PyResult<bool> {
    let mobility = match mobility {
        Some(mobility) => exact_str(mobility)?,
        None => "independent",
    };
    Ok(Geo::of(world)?.route_is_clear(exact_str(edge_id)?, mobility))
}

/// `_zone_center` of `_geometry(world)`, for the tests' comparison with the Python rule.
#[pyfunction]
fn zone_center(world: &Bound<'_, PyAny>, zone_id: &str) -> PyResult<Option<(f64, f64)>> {
    Ok(Geo::of(world)?.zone_center(zone_id)?.map(|point| (point.x, point.y)))
}

/// `_queue_slot_position`, for a facility's own queue. An independent FIFO reads its namespace: Python.
#[pyfunction]
#[pyo3(signature = (world, geometry, agent_id, facility_id, queue_id=None))]
fn queue_slot_position<'py>(
    world: &Bound<'py, PyAny>,
    geometry: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    queue_id: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let _ = geometry; // What _geometry(world) gives, as every caller passes.
    let table = attribute(world, intern!(py, "model_state"))?;
    if queue_id.is_some_and(|queue_id| !queue_id.is_none()) && exact_dict(&table)?.contains("facility_queues_v1")? {
        return unsupported("an independent FIFO");
    }
    let (agent_id, facility_id) = (exact_str(agent_id)?, exact_str(facility_id)?);
    let mirror = mirror_held(world)?;
    let store = entities_of(world)?;
    let geo = Geo::of(world)?;
    let slot = NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| {
        EntityStore::with_rows(&store, |rows| queue_slot(&geo, row, rows, agent_id, facility_id))
    })?;
    match slot {
        Some(point) => point_object(py, point),
        None => Ok(py.None().into_bound(py)),
    }
}

/// `min(options, key=(distance, points))`: the tuple order on finite floats.
fn is_shorter(first: (f64, &[Pt]), second: (f64, &[Pt])) -> bool {
    if first.0 != second.0 {
        return first.0 < second.0;
    }
    for (a, b) in first.1.iter().zip(second.1) {
        if a.x != b.x {
            return a.x < b.x;
        }
        if a.y != b.y {
            return a.y < b.y;
        }
    }
    first.1.len() < second.1.len()
}

/// `facility_approach_route`, for a facility's own point. A surveyed service point goes to Python.
#[pyfunction]
#[pyo3(signature = (world, agent_id, facility_id, service_point_id=None))]
fn facility_approach_route<'py>(
    world: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    service_point_id: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    if service_point_id.is_some_and(|point| !point.is_none()) {
        return unsupported("a surveyed service point");
    }
    let (agent_id, facility_id) = (exact_str(agent_id)?, exact_str(facility_id)?);
    let mirror = mirror_held(world)?;
    let store = entities_of(world)?;
    let geo = Geo::of(world)?;
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let raw_model = exact_dict(&configs)?.get_item(intern!(py, "movement_model"))?;
    let (agent_zone, facility_zone, mobility, loads) = EntityStore::with_rows(&store, |rows| {
        let (Some(agent), Some(facility)) = (rows.agents.get(agent_id), rows.facilities.get(facility_id)) else {
            return unsupported("no such agent or facility");
        };
        let loads = rows.agents.iter().filter(|(_, other)| other.zone_id == agent.zone_id).count();
        Ok((agent.zone_id.clone(), facility.zone_id.clone(), mobility_text(agent), loads))
    })?;
    let route_class = zone_route_class(py)?;
    let Some(layout) = &geo.layout else {
        let route = route_class.call1((&*agent_zone, &*facility_zone, (&*agent_zone,), PyTuple::empty(py), 0))?;
        return Ok((route, PyTuple::empty(py)).into_pyobject(py)?.into_any());
    };
    if agent_zone != facility_zone {
        return Ok(py.None().into_bound(py));
    }
    let start = NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| Ok(position_point(row.positions.get(agent_id))))?;
    if start.is_some_and(|start| !usable(start.x, start.y)) {
        return unsupported("a position is not a usable point");
    }
    let Some(&facility_point) = layout.facility_points.get(facility_id) else {
        return Ok(py.None().into_bound(py));
    };
    let mut candidates = vec![facility_point];
    let footprint = geo.constraints.as_ref().and_then(|constraints| {
        constraints.footprints.iter().find(|(id, _)| &**id == facility_id).map(|(_, polygon)| polygon)
    });
    if let Some(footprint) = footprint {
        let count = footprint.len() as f64;
        let not_plain = || Unsupported::new_err("a footprint's centre is not a plain sum");
        let center = Pt {
            x: py_fsum(&footprint.iter().map(|point| point.x).collect::<Vec<_>>()).ok_or_else(not_plain)? / count,
            y: py_fsum(&footprint.iter().map(|point| point.y).collect::<Vec<_>>()).ok_or_else(not_plain)? / count,
        };
        let clearance = match mobility {
            "independent" => APPROACH_INDEPENDENT,
            "assisted" => APPROACH_ASSISTED,
            _ => APPROACH_WHEELCHAIR,
        };
        let outward = |candidates: &mut Vec<Pt>, point: Pt| {
            let (dx, dy) = (point.x - center.x, point.y - center.y);
            let length = crate::core::mathx::hypot(&[dx, dy]);
            if length > 1e-9 {
                candidates.push(Pt { x: point.x + dx / length * clearance, y: point.y + dy / length * clearance });
            }
        };
        for (index, &first) in footprint.iter().enumerate() {
            let second = footprint[(index + 1) % footprint.len()];
            outward(&mut candidates, Pt { x: (first.x + second.x) / 2.0, y: (first.y + second.y) / 2.0 });
        }
        for &corner in footprint {
            outward(&mut candidates, corner);
        }
    }
    let mut best: Option<(f64, Arc<[Pt]>)> = None;
    for destination in candidates {
        let Some(path) = geo.room_route(&facility_zone, start, destination, mobility).map_err(refused)? else {
            continue;
        };
        let length = polyline_length(&path).ok_or_else(|| Unsupported::new_err("not a plain sum"))?;
        if best.as_ref().is_none_or(|(known, known_path)| is_shorter((length, &path), (*known, known_path))) {
            best = Some((length, path));
        }
    }
    let Some((length, path)) = best else {
        return Ok(py.None().into_bound(py));
    };
    // in_room_travel_minutes, then what facility_approach_route makes of it.
    let mut duration = if path.len() < 2 {
        0
    } else {
        let model = movement_model(raw_model)?;
        let speed = match mobility {
            "independent" => model.independent,
            "assisted" => model.assisted,
            _ => model.wheelchair,
        };
        let density = match layout.zones.get(&*agent_zone).map(|zone| zone.usable_area).filter(|area| *area > 0.0) {
            Some(area) => loads as f64 / area,
            None => 0.0,
        };
        let crowding = py_min(model.maximum_slowdown, 1.0 + model.density_slowdown * py_max(0.0, density - 0.5));
        let minutes = length / speed * crowding;
        if !minutes.is_finite() || minutes.abs() >= EXACT_INT as f64 {
            return unsupported("the duration is too large");
        }
        (minutes.ceil() as i64).max(1)
    };
    if path.len() == 1 || path.windows(2).all(|pair| distance(pair[0], pair[1]) <= 1e-8) {
        duration = 0;
    }
    let route = route_class.call1((&*facility_zone, &*facility_zone, (&*facility_zone, &*facility_zone), PyTuple::empty(py), duration))?;
    let points = path.iter().map(|point| point_object(py, *point)).collect::<PyResult<Vec<_>>>()?;
    Ok((route, PyTuple::new(py, points)?).into_pyobject(py)?.into_any())
}

fn zone_route_class(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    static CLASS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    let class = CLASS.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
        Ok(py.import("sheltersim.policies.space")?.getattr("ZoneRoute")?.unbind())
    })?;
    Ok(class.bind(py).clone())
}

// ---- advance_movement --------------------------------------------------------------------------

/// `_facility_move_invalidated_journey`.
fn facility_move_invalidates(scene: &Scene<'_>, journey: &Journey) -> PyResult<bool> {
    let py = scene.world.py();
    let Some(facility_id) = &journey.facility_id else {
        return Ok(false);
    };
    let Some(overlay) = &scene.overlay else {
        return Ok(false);
    };
    let Some(moves) = exact_dict(overlay)?.get_item(intern!(py, "facility_points"))? else {
        return Ok(false);
    };
    let Some(moved) = exact_dict(&moves)?.get_item(&**facility_id)? else {
        return Ok(false);
    };
    let Some(moved) = crate::core::tracked::as_readable_dict(&moved) else {
        return Ok(false);
    };
    let Some(minute) = moved.get_item(intern!(py, "minute"))? else {
        return Ok(false);
    };
    let Ok(minute) = minute.downcast_exact::<PyInt>() else {
        return Ok(false);
    };
    Ok(minute.extract::<i64>().map_err(|_| Unsupported::new_err("a move's minute is too large"))? > journey.started_minute)
}

/// `_room_path_is_clear`, with its record of the person.
fn room_path_is_clear(geo: &Geo, position: Option<&Position>, zone_id: &str, points: &[Pt], mobility: &str) -> PyResult<bool> {
    let (Some(constraints), Some(layout)) = (&geo.constraints, &geo.layout) else {
        return Ok(true);
    };
    let Some(zone) = layout.zones.get(zone_id) else {
        return Ok(false);
    };
    let current = match position.and_then(|position| position.point) {
        Some(point) => {
            if !usable(point.x, point.y) {
                return unsupported("a position is not a usable point");
            }
            Pt { x: point.x, y: point.y }
        }
        None => points[0],
    };
    let lengths: Vec<f64> = points.windows(2).map(|pair| distance(pair[0], pair[1])).collect();
    let Some(total) = py_fsum(&lengths) else {
        return unsupported("a route's length is not a plain sum");
    };
    let progress = position.map_or(0.0, |position| position.progress);
    let target_distance = py_min(1.0, py_max(0.0, progress)) * total;
    let mut cumulative = 0.0;
    let mut remaining = vec![current];
    for (index, &length) in lengths.iter().enumerate() {
        if cumulative + length + 1e-9 >= target_distance {
            if length > 1e-12 {
                let fraction = py_min(1.0, py_max(0.0, (target_distance - cumulative) / length));
                if fraction < 1.0 - 1e-9 {
                    remaining.push(points[index + 1]);
                }
            }
            remaining.extend_from_slice(&points[index + 2..]);
            break;
        }
        cumulative += length;
    }
    if remaining.len() == 1 {
        remaining.push(points[points.len() - 1]);
    }
    Ok(remaining
        .windows(2)
        .all(|pair| Geo::room_segment_clear(constraints, pair[0], pair[1], &zone.polygon, mobility)))
}

/// `min(1.0, max(0.0, a / b))` of ints that a float holds exactly.
fn progress_of(part: i64, whole: i64) -> f64 {
    py_min(1.0, py_max(0.0, part as f64 / whole as f64))
}

/// The plan of one journey in `advance_movement`.
enum Step {
    Replan,
    Stop,
    Finish,
    Walk { position: Position, edge: Option<Arc<str>>, completed: bool },
}

fn plan_journey(
    scene: &Scene<'_>,
    queue: &QueueStore,
    row: &MovementRow,
    rows: &Entities,
    agent_id: &str,
    agent: &AgentRow,
    journey: &Journey,
    minute: i64,
) -> PyResult<Step> {
    let py = scene.world.py();
    let queue_trip = is_queue_trip(&journey.purpose);
    let task_active = rows.active_tasks.get(&journey.task_id).is_some();
    let queued = agent.activity == Activity::Queued;
    let trip_active = queued && queue_trip && task_active;
    let pending = queue.has_event(&journey.event_id);
    if trip_active && !pending {
        return Ok(Step::Finish);
    }
    if (agent.activity != Activity::Moving && !trip_active) || !pending || !task_active {
        return Ok(Step::Stop);
    }
    // From here the person walks or replans. A queue trip's replan writes the queue and allocates
    // an event in the walk's order: the Python rule does the whole tick.
    let replan = || -> PyResult<Step> {
        if queue_trip {
            unsupported("a queue trip must replan, in the serial order of the walk")
        } else {
            Ok(Step::Replan)
        }
    };
    let facility = journey
        .facility_id
        .as_deref()
        .filter(|facility_id| !facility_id.is_empty())
        .and_then(|facility_id| rows.facilities.get(facility_id));
    let destination = facility.map_or(&journey.destination_zone_id, |facility| &facility.zone_id);
    if *destination != journey.destination_zone_id || facility_move_invalidates(scene, journey)? {
        return replan();
    }
    if journey.due_minute <= journey.started_minute
        || journey.due_minute.abs() >= EXACT_INT
        || journey.started_minute.abs() >= EXACT_INT
    {
        return unsupported("a journey has no plain duration");
    }
    let mobility = mobility_text(agent);
    let boundary = (minute + 1).min(journey.due_minute);
    let completed = queued && queue_trip && boundary >= journey.due_minute;
    let duration = journey.due_minute - journey.started_minute;
    if !journey.route_points.is_empty() {
        let points = points_of_journey(journey)?;
        if !room_path_is_clear(&scene.geo, row.positions.get(agent_id), &journey.destination_zone_id, &points, mobility)? {
            return replan();
        }
        let progress = progress_of(boundary - journey.started_minute, duration);
        let Some(point) = point_along_polyline(&points, progress) else {
            return unsupported("a route's length is not a plain sum");
        };
        return Ok(Step::Walk {
            position: record(journey.destination_zone_id.clone(), Some(point), KINDS.physical.clone(), None, progress),
            edge: None,
            completed,
        });
    }
    let durations =
        journey.route_edge_ids.iter().map(|edge| scene.edge_minutes(edge)).collect::<PyResult<Vec<i64>>>()?;
    // A frame at boundary M is the state before tick M, so the sample is the next boundary.
    let elapsed = (boundary - journey.started_minute).max(0);
    let (mut edge_index, mut progress) = (0, 0.0);
    if !durations.is_empty() {
        let total: i64 = durations.iter().sum();
        if total >= EXACT_INT {
            return unsupported("a route's minutes are too large");
        }
        let duration_scale = duration as f64 / total as f64;
        let mut cumulative = 0.0;
        for (index, &raw_duration) in durations.iter().enumerate() {
            let scaled = raw_duration as f64 * duration_scale;
            if (elapsed as f64) < cumulative + scaled || index == durations.len() - 1 {
                edge_index = index;
                progress = py_min(1.0, py_max(0.0, (elapsed as f64 - cumulative) / scaled));
                break;
            }
            cumulative += scaled;
            edge_index = (index + 1).min(durations.len() - 1);
            progress = 0.0;
        }
    }
    let edge_id = journey.route_edge_ids.get(edge_index).filter(|_| !durations.is_empty());
    if let Some(edge_id) = edge_id {
        let closed = attribute(&scene.edge(edge_id)?, intern!(py, "closed"))?;
        let closed = closed.downcast_exact::<PyBool>().map_err(|_| Unsupported::new_err("closed is not a bool"))?;
        if closed.is_true() || !scene.geo.route_is_clear(edge_id, mobility) {
            // A closure invalidates this completion event, whatever the trip is for.
            return Ok(Step::Replan);
        }
    }
    let current_zone = if durations.is_empty() {
        &journey.destination_zone_id
    } else {
        journey.route_zone_ids.get(edge_index).ok_or_else(|| Unsupported::new_err("a route has fewer zones"))?
    };
    let point = match edge_id {
        Some(edge_id) => scene.route_point(edge_id, progress, current_zone)?,
        None => scene.geo.zone_center(current_zone)?,
    };
    let kind = on_route_kind(&point);
    Ok(Step::Walk {
        position: record(current_zone.clone(), point, kind, edge_id.cloned(), progress),
        edge: edge_id.cloned(),
        completed,
    })
}

fn mobility_text(agent: &AgentRow) -> &'static str {
    use crate::state::entities::Mobility;
    match agent.profile.mobility {
        Mobility::Independent => "independent",
        Mobility::Assisted => "assisted",
        Mobility::Wheelchair => "wheelchair",
    }
}

/// `staff_position_snapshots`' record of one staff member.
fn plan_staff(scene: &Scene<'_>, operations: Option<&Bound<'_, PyDict>>, staff_id: &str, zone_id: &Arc<str>, boundary: i64) -> PyResult<Position> {
    let py = scene.world.py();
    let operation = match operations {
        Some(records) => records.get_item(staff_id)?,
        None => None,
    };
    let travel = operation.as_ref().and_then(|operation| crate::core::tracked::as_readable_dict(operation));
    let travelling = match travel {
        Some(operation) => match operation.get_item(intern!(py, "phase"))? {
            Some(phase) => phase.downcast_exact::<PyString>().is_ok_and(|phase| phase.to_str().is_ok_and(|text| text == "travel")),
            None => false,
        },
        None => {
            if operation.as_ref().is_some_and(|operation| !operation.is_none()) {
                return unsupported("a staff operation is not a plain dict");
            }
            false
        }
    };
    if !travelling {
        let point = scene.geo.zone_center(zone_id)?;
        let kind = on_route_kind(&point);
        return Ok(record(zone_id.clone(), point, kind, None, 0.0));
    }
    let operation = travel.ok_or_else(|| Unsupported::new_err("no operation"))?;
    let required = |name| -> PyResult<Bound<'_, PyAny>> {
        operation.get_item(name)?.ok_or_else(|| Unsupported::new_err("a staff operation lacks a field"))
    };
    let travel_minutes = exact_int(&required(intern!(py, "travel_minutes"))?)?;
    let started = exact_int(&required(intern!(py, "started_minute"))?)?;
    let edges = required(intern!(py, "route_edge_ids"))?;
    let edges = exact_list(&edges)?;
    let zones = required(intern!(py, "route_zone_ids"))?;
    let zones = exact_list(&zones)?;
    let elapsed = travel_minutes.min(boundary.saturating_sub(started)).max(0);
    let (mut cumulative, mut edge_id, mut progress) = (0_i64, None::<Arc<str>>, 0.0);
    let mut current_zone = zone_id.clone();
    let last = edges.len().wrapping_sub(1);
    for (index, route_edge) in edges.iter().enumerate() {
        let route_edge = exact_str(&route_edge)?;
        let minutes = scene.edge_minutes(route_edge)?;
        if elapsed < cumulative + minutes {
            edge_id = Some(Arc::from(route_edge));
            progress = (elapsed - cumulative) as f64 / minutes as f64;
            let zone = zones.get_item(index).map_err(|_| Unsupported::new_err("a route has fewer zones"))?;
            current_zone = Arc::from(exact_str(&zone)?);
            break;
        }
        cumulative += minutes;
        let to_zone = attribute(&scene.edge(route_edge)?, intern!(py, "to_zone_id"))?;
        current_zone = Arc::from(exact_str(&to_zone)?);
        if index == last {
            edge_id = Some(Arc::from(route_edge));
            progress = 1.0;
        }
    }
    let point = match &edge_id {
        Some(edge) => scene.route_point(edge, progress, &current_zone)?,
        None => scene.geo.zone_center(&current_zone)?,
    };
    let kind = on_route_kind(&point);
    Ok(record(current_zone, point, kind, edge_id, progress))
}

/// The rows of the world's event queue.
fn queue_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, QueueStore>> {
    let py = world.py();
    world
        .getattr(intern!(py, "event_queue"))?
        .getattr(intern!(py, "_store"))
        .map_err(|_| Unsupported::new_err("the world's events are not in Rust"))?
        .downcast_into::<QueueStore>()
        .map_err(|_| Unsupported::new_err("the event queue is not in Rust"))
}

/// `advance_movement`.
#[pyfunction]
fn advance_movement<'py>(world: &Bound<'py, PyAny>, minute: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
    let py = world.py();
    let minute = exact_int(minute)?;
    if minute.abs() >= EXACT_INT {
        return unsupported("a minute is too large");
    }
    let mirror = mirror_held(world)?;
    let store = entities_of(world)?;
    let queue = queue_of(world)?;
    let scene = Scene::of(world)?;
    let table = attribute(world, intern!(py, "model_state"))?;
    let operations = exact_dict(&table)?.get_item("staff_operations_v1")?;
    let staff_records = match &operations {
        Some(operations) => match crate::core::tracked::as_readable_dict(operations) {
            Some(operations) => operations.get_item(intern!(py, "staff"))?,
            None => None,
        },
        None => None,
    };
    let staff_records = match &staff_records {
        Some(records) => Some(exact_dict(records)?),
        None => None,
    };
    let (replans, writes) = NamespaceMirror::with_row_tree::<MovementRow, _>(&mirror, |row, tree| {
        let queue = queue.try_borrow()?;
        let (changes, replans) = EntityStore::with_rows(&store, |rows| {
            let mut changes = Changes::default();
            missing_positions(row, rows, &mut changes);
            if row.last_updated_minute >= minute {
                return Ok((changes, Vec::new()));
            }
            changes.minute = Some(minute);
            let mut replans: Vec<Arc<str>> = Vec::new();
            let mut completed: Vec<&str> = Vec::new();
            let mut journeys: Vec<(&Arc<str>, &Journey)> = row.journeys.iter().collect();
            journeys.sort_unstable_by(|first, second| first.0.cmp(second.0));
            for (agent_id, journey) in journeys {
                let Some(agent) = rows.agents.get(agent_id) else {
                    changes.gone_journeys.push(agent_id.clone());
                    changes.gone_positions.push(agent_id.clone());
                    continue;
                };
                match plan_journey(&scene, &queue, row, rows, agent_id, agent, journey, minute)? {
                    Step::Replan => replans.push(agent_id.clone()),
                    Step::Stop => {
                        changes.gone_journeys.push(agent_id.clone());
                        changes.positions.push((agent_id.clone(), scene.stationary(agent_id, agent, None)?));
                    }
                    Step::Finish => {
                        changes.gone_journeys.push(agent_id.clone());
                        if let Some(position) = row.positions.get(&**agent_id).and_then(arrived) {
                            changes.positions.push((agent_id.clone(), position));
                        }
                    }
                    Step::Walk { position, edge, completed: done } => {
                        changes.positions.push((agent_id.clone(), position));
                        changes.usage.extend(edge);
                        if done {
                            completed.push(agent_id);
                        }
                    }
                }
            }
            for (agent_id, agent) in rows.agents.iter() {
                let stays_on_journey = row.journeys.contains_key(&**agent_id)
                    && !changes.gone_journeys.iter().any(|gone| gone == agent_id);
                if !stays_on_journey && agent.activity != Activity::Queued && !completed.contains(&&**agent_id) {
                    changes.positions.push((agent_id.clone(), scene.stationary(agent_id, agent, None)?));
                }
            }
            let mut staff: Vec<_> = rows.staff.iter().collect();
            staff.sort_unstable_by(|first, second| first.0.cmp(second.0));
            for (staff_id, member) in staff {
                changes.staff.push((
                    staff_id.clone(),
                    plan_staff(&scene, staff_records, staff_id, &member.zone_id, minute + 1)?,
                ));
            }
            Ok((changes, replans))
        })?;
        Ok((replans, changes.apply(row, tree)?))
    })?;
    write_tree(&mirror, writes)?;
    PyTuple::new(py, replans.iter().map(|id| PyString::new(py, id)))
}

// ---- start_movement, finish_movement -----------------------------------------------------------

fn optional_str(value: Option<&Bound<'_, PyAny>>) -> PyResult<Option<Arc<str>>> {
    match value {
        Some(value) if !value.is_none() => Ok(Some(Arc::from(exact_str(value)?))),
        _ => Ok(None),
    }
}

fn str_tuple(value: &Bound<'_, PyAny>) -> PyResult<Vec<Arc<str>>> {
    value
        .downcast_exact::<PyTuple>()
        .map_err(|_| Unsupported::new_err("not a tuple"))?
        .iter()
        .map(|item| Ok(Arc::from(exact_str(&item)?)))
        .collect()
}

/// The points of a route: `Point2D` itself, so `model_dump` would give the same two floats.
fn route_points_of(value: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<Pt>> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let points = value.downcast_exact::<PyTuple>().map_err(|_| Unsupported::new_err("the points are not a tuple"))?;
    let mut found = Vec::with_capacity(points.len());
    for point in points.iter() {
        let py = point.py();
        if point.get_type().name()?.to_str()? != "Point2D" {
            return unsupported("a route point is not a Point2D");
        }
        let (x, y) = (exact_float(&attribute(&point, intern!(py, "x"))?)?, exact_float(&attribute(&point, intern!(py, "y"))?)?);
        if !usable(x, y) {
            return unsupported("a route point is not a usable point");
        }
        found.push(Pt { x, y });
    }
    Ok(found)
}

/// `start_movement`.
#[pyfunction]
#[pyo3(signature = (
    world, *, agent_id, route, purpose, facility_id, started_minute, due_minute, event_id, task_id,
    route_points=None, queue_id=None, support_request_id=None, support_staff_id=None
))]
#[allow(clippy::too_many_arguments)]
fn start_movement<'py>(
    world: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    route: &Bound<'py, PyAny>,
    purpose: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    started_minute: &Bound<'py, PyAny>,
    due_minute: &Bound<'py, PyAny>,
    event_id: &Bound<'py, PyAny>,
    task_id: &Bound<'py, PyAny>,
    route_points: Option<&Bound<'py, PyAny>>,
    queue_id: Option<&Bound<'py, PyAny>>,
    support_request_id: Option<&Bound<'py, PyAny>>,
    support_staff_id: Option<&Bound<'py, PyAny>>,
) -> PyResult<()> {
    let py = world.py();
    let mirror = mirror_held(world)?;
    let store = entities_of(world)?;
    let started = exact_int(started_minute)?;
    let due = exact_int(due_minute)?;
    let duration = due.checked_sub(started).filter(|duration| *duration > 0 && *duration < EXACT_INT);
    let Some(duration) = duration else {
        return unsupported("a journey has no plain duration");
    };
    let origin: Arc<str> = Arc::from(exact_str(&attribute(route, intern!(py, "origin_zone_id"))?)?);
    let zone_ids = str_tuple(&attribute(route, intern!(py, "zone_ids"))?)?;
    let edge_ids = str_tuple(&attribute(route, intern!(py, "edge_ids"))?)?;
    let points = route_points_of(route_points)?;
    let agent_id: Arc<str> = Arc::from(exact_str(agent_id)?);
    let support = match (optional_str(support_request_id)?, optional_str(support_staff_id)?) {
        (Some(request), Some(staff)) => (Some(request), Some(staff)),
        _ => (None, None),
    };
    let journey = Journey {
        event_id: Arc::from(exact_str(event_id)?),
        task_id: Arc::from(exact_str(task_id)?),
        purpose: Arc::from(exact_str(purpose)?),
        facility_id: optional_str(Some(facility_id))?,
        destination_zone_id: Arc::from(exact_str(&attribute(route, intern!(py, "destination_zone_id"))?)?),
        route_zone_ids: zone_ids,
        route_edge_ids: edge_ids.clone(),
        route_points: points.iter().copied().map(point_row).collect(),
        started_minute: started,
        due_minute: due,
        queue_id: optional_str(queue_id)?,
        support_request_id: support.0,
        support_staff_id: support.1,
    };
    let scene = Scene::of(world)?;
    let writes = NamespaceMirror::with_row_tree::<MovementRow, _>(&mirror, |row, tree| {
        let mut changes = EntityStore::with_rows(&store, |rows| {
            let mut changes = Changes::default();
            missing_positions(row, rows, &mut changes);
            Ok(changes)
        })?;
        let elapsed = i64::from(row.last_updated_minute >= started);
        let initial_progress = py_min(1.0, elapsed as f64 / duration as f64);
        let point = if !points.is_empty() {
            if elapsed != 0 {
                match point_along_polyline(&points, initial_progress) {
                    Some(point) => Some(point),
                    None => return unsupported("a route's length is not a plain sum"),
                }
            } else {
                Some(points[0])
            }
        } else if let Some(edge) = edge_ids.first() {
            let progress = if elapsed != 0 { initial_progress } else { 0.0 };
            scene.route_point(edge, progress, &origin)?
        } else {
            scene.geo.zone_center(&origin)?
        };
        let kind = if point.is_some() && (!edge_ids.is_empty() || !points.is_empty()) {
            KINDS.physical.clone()
        } else {
            KINDS.unavailable.clone()
        };
        changes.positions.push((agent_id.clone(), record(origin.clone(), point, kind, edge_ids.first().cloned(), initial_progress)));
        changes.new_journey = Some((agent_id.clone(), journey));
        changes.apply(row, tree)
    })?;
    write_tree(&mirror, writes)
}

/// `finish_movement`.
#[pyfunction]
fn finish_movement<'py>(world: &Bound<'py, PyAny>, agent_id: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    if !exact_dict(&table)?.contains(KEY)? {
        return Ok(());
    }
    let mirror = mirror_held(world)?;
    let store = entities_of(world)?;
    let agent_id = exact_str(agent_id)?;
    let scene = Scene::of(world)?;
    let writes = NamespaceMirror::with_row_tree::<MovementRow, _>(&mirror, |row, tree| {
        let mut changes = Changes::default();
        let journey = row.journeys.get(agent_id);
        let key: Arc<str> = Arc::from(agent_id);
        if journey.is_some() {
            changes.gone_journeys.push(key.clone());
        }
        if journey.is_some_and(|journey| is_queue_trip(&journey.purpose)) {
            // The person is still reserved in the FIFO queue: keep their surveyed arrival.
            if let Some(position) = row.positions.get(agent_id).and_then(arrived) {
                changes.positions.push((key, position));
            }
        } else {
            let position = EntityStore::with_rows(&store, |rows| match rows.agents.get(agent_id) {
                Some(agent) => scene.stationary(agent_id, agent, Some((&*row, &*rows))).map(Some),
                None => Ok(None),
            })?;
            if let Some(position) = position {
                changes.positions.push((key, position));
            }
        }
        changes.apply(row, tree)
    })?;
    write_tree(&mirror, writes)
}

// ---- route_with_movement_time ------------------------------------------------------------------

/// `canonical_movement_config`, for the values whose result is certain: floats, and what it accepts.
struct MovementModel {
    independent: f64,
    assisted: f64,
    wheelchair: f64,
    density_slowdown: f64,
    maximum_slowdown: f64,
}

fn movement_model(raw: Option<Bound<'_, PyAny>>) -> PyResult<MovementModel> {
    let mut model = MovementModel {
        independent: SPEED_INDEPENDENT,
        assisted: SPEED_ASSISTED,
        wheelchair: SPEED_WHEELCHAIR,
        density_slowdown: DENSITY_SLOWDOWN,
        maximum_slowdown: MAXIMUM_SLOWDOWN,
    };
    let Some(raw) = raw.filter(|raw| !raw.is_none()) else {
        return Ok(model);
    };
    let py = raw.py();
    for (name, value) in exact_dict(&raw)?.iter() {
        match exact_str(&name)? {
            "schema_version" => {
                if exact_int(&value)? != 1 {
                    return unsupported("another movement schema");
                }
            }
            "policy_version" => {
                if exact_str(&value)? != POLICY_VERSION {
                    return unsupported("another movement policy");
                }
            }
            "source_id" => {
                if exact_str(&value)? != MOVEMENT_SOURCE_ID {
                    return unsupported("another movement source");
                }
            }
            "speed_m_per_minute_by_mobility" => {
                let speeds = exact_dict(&value)?;
                if speeds.len() != 3 {
                    return unsupported("the speed table has other fields");
                }
                let speed = |name| -> PyResult<f64> {
                    let value = speeds.get_item(name)?.ok_or_else(|| Unsupported::new_err("a speed is missing"))?;
                    let speed = exact_float(&value)?;
                    if !speed.is_finite() || speed <= 0.0 {
                        return unsupported("a speed is not positive");
                    }
                    Ok(speed)
                };
                model.independent = speed(intern!(py, "independent"))?;
                model.assisted = speed(intern!(py, "assisted"))?;
                model.wheelchair = speed(intern!(py, "wheelchair"))?;
            }
            "density_slowdown_per_person_m2" => {
                model.density_slowdown = exact_float(&value)?;
                if !model.density_slowdown.is_finite() || model.density_slowdown < 0.0 {
                    return unsupported("a slowdown is not a plain value");
                }
            }
            "maximum_density_slowdown" => {
                model.maximum_slowdown = exact_float(&value)?;
                if !model.maximum_slowdown.is_finite() || model.maximum_slowdown < 1.0 {
                    return unsupported("a slowdown is not a plain value");
                }
            }
            _ => return unsupported("the movement config has other fields"),
        }
    }
    Ok(model)
}

/// `route_with_movement_time`.
#[pyfunction]
fn route_with_movement_time<'py>(
    world: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    route: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let configs = exact_dict(&configs)?;
    let raw_model = configs.get_item(intern!(py, "movement_model"))?;
    if raw_model.is_none() && !configs.contains(intern!(py, "facility_geometry"))? {
        return Ok(route.clone());
    }
    let model = movement_model(raw_model)?;
    let class: Bound<'py, PyType> = route.get_type();
    if class.name()?.to_str()? != "ZoneRoute" {
        return unsupported("the route is not a ZoneRoute");
    }
    let edge_ids = attribute(route, intern!(py, "edge_ids"))?;
    let edges = edge_ids.downcast_exact::<PyTuple>().map_err(|_| Unsupported::new_err("not a tuple"))?;
    let zone_ids = attribute(route, intern!(py, "zone_ids"))?;
    let zones = zone_ids.downcast_exact::<PyTuple>().map_err(|_| Unsupported::new_err("not a tuple"))?;
    let declared = exact_int(&attribute(route, intern!(py, "travel_minutes"))?)?;
    if declared.abs() >= EXACT_INT {
        return unsupported("the route's minutes are too large");
    }
    let agent_id = exact_str(agent_id)?;
    let geo = Geo::of(world)?;
    let store = entities_of(world)?;
    let zone_items: Vec<Bound<'py, PyAny>> = zones.iter().collect();
    let mut wanted: Vec<(&str, i64)> = Vec::with_capacity(zone_items.len());
    for zone in &zone_items {
        wanted.push((exact_str(zone)?, 0));
    }
    let speed = EntityStore::with_rows(&store, |rows| {
        let Some(agent) = rows.agents.get(agent_id) else {
            return unsupported("no such agent");
        };
        for (_, other) in rows.agents.iter() {
            for (zone, count) in wanted.iter_mut() {
                if *zone == &*other.zone_id {
                    *count += 1;
                }
            }
        }
        Ok(match mobility_text(agent) {
            "independent" => model.independent,
            "assisted" => model.assisted,
            _ => model.wheelchair,
        })
    })?;
    let mut lengths = Vec::new();
    if let Some(layout) = &geo.layout {
        for edge in edges.iter() {
            if let Some(found) = layout.routes.get(exact_str(&edge)?) {
                lengths.push(polyline_length(&found.points).ok_or_else(|| Unsupported::new_err("not a plain sum"))?);
            }
        }
    }
    let distance_m = py_fsum(&lengths).ok_or_else(|| Unsupported::new_err("not a plain sum"))?;
    let base = model.independent;
    let physical = if distance_m > 0.0 { distance_m / base } else { declared as f64 };
    let mut density: Option<f64> = None;
    if let Some(layout) = &geo.layout {
        let mut done: Vec<&str> = Vec::with_capacity(wanted.len());
        for &(zone, count) in &wanted {
            if done.contains(&zone) {
                continue;
            }
            done.push(zone);
            if let Some(area) = layout.zones.get(zone).map(|zone| zone.usable_area).filter(|area| *area > 0.0) {
                let value = count as f64 / area;
                density = Some(density.map_or(value, |best| py_max(best, value)));
            }
        }
    }
    let crowding = py_min(model.maximum_slowdown, 1.0 + model.density_slowdown * py_max(0.0, density.unwrap_or(0.0) - 0.5));
    let duration = py_max(declared as f64, physical) * (base / speed) * crowding;
    if edges.is_empty() {
        return Ok(route.clone());
    }
    if !duration.is_finite() || duration.abs() >= EXACT_INT as f64 {
        return unsupported("the duration is too large");
    }
    let minutes = (duration.ceil() as i64).max(1);
    class.call1((
        attribute(route, intern!(py, "origin_zone_id"))?,
        attribute(route, intern!(py, "destination_zone_id"))?,
        zones,
        edges,
        minutes,
    ))
}

// ---- validate ----------------------------------------------------------------------------------

/// `position_kind` of the two record maps, with the rules both share.
fn position_is_plain(position: &Position, zones: &Bound<'_, PyDict>, edges: &Bound<'_, PyDict>) -> PyResult<bool> {
    if !zones.contains(&*position.zone_id)?
        || !matches!(&*position.position_kind, "physical" | "display_only" | "unavailable")
        || !(0.0..=1.0).contains(&position.progress)
    {
        return Ok(false);
    }
    if position.point.is_some_and(|point| !point.x.is_finite() || !point.y.is_finite()) {
        return Ok(false);
    }
    match &position.edge_id {
        Some(edge) => edges.contains(&**edge),
        None => Ok(true),
    }
}

/// One journey against what `validate_movement_runtime_state` checks.
fn journey_is_valid(
    world: &Bound<'_, PyAny>,
    agent: &AgentRow,
    journey: &Journey,
    zones: &Bound<'_, PyDict>,
    edges: &Bound<'_, PyDict>,
    geo: &mut Option<Arc<Geo>>,
) -> PyResult<bool> {
    let py = world.py();
    let support = journey.support_request_id.is_some() && journey.support_staff_id.is_some();
    if journey.support_request_id.is_some() != journey.support_staff_id.is_some() {
        return Ok(false);
    }
    let staff_travel = &*journey.purpose == "staff_support_travel";
    if support != staff_travel {
        return Ok(false);
    }
    if support && journey.support_request_id.iter().chain(&journey.support_staff_id).any(|id| id.is_empty()) {
        return Ok(false);
    }
    if let Some(queue_id) = &journey.queue_id {
        if queue_id.is_empty() || !is_queue_trip(&journey.purpose) {
            return Ok(false);
        }
        // The Python rule reads the queue's facility: anything with a queue ID goes to it.
        let table = attribute(world, intern!(py, "model_state"))?;
        let Some(queues) = exact_dict(&table)?.get_item("facility_queues_v1")? else {
            return Ok(false);
        };
        let Some(queues) = exact_dict(&queues)?.get_item(intern!(py, "queues"))? else {
            return Ok(false);
        };
        let Some(queue) = exact_dict(&queues)?.get_item(&**queue_id)? else {
            return Ok(false);
        };
        let Some(facility) = exact_dict(&queue)?.get_item(intern!(py, "facility_id"))? else {
            return Ok(false);
        };
        match &journey.facility_id {
            Some(id) if !facility.is_none() && exact_str(&facility)? == &**id => {}
            _ => return Ok(false),
        }
    }
    if !zones.contains(&*journey.destination_zone_id)? || journey.due_minute <= journey.started_minute {
        return Ok(false);
    }
    for zone in &journey.route_zone_ids {
        if !zones.contains(&**zone)? {
            return Ok(false);
        }
    }
    for edge in &journey.route_edge_ids {
        if !edges.contains(&**edge)? {
            return Ok(false);
        }
    }
    if journey.route_points.is_empty() {
        return Ok(true);
    }
    if journey.route_points.len() < 2
        || !journey.route_edge_ids.is_empty()
        || journey.route_zone_ids.len() != 2
        || journey.route_zone_ids[0] != agent.zone_id
        || journey.route_zone_ids[1] != journey.destination_zone_id
    {
        return Ok(false);
    }
    let Ok(points) = points_of_journey(journey) else {
        return Ok(false);
    };
    let geo = match geo {
        Some(geo) => geo,
        None => geo.insert(Geo::of(world)?),
    };
    let Some(zone) = geo.layout.as_ref().and_then(|layout| layout.zones.get(&agent.zone_id)) else {
        return Ok(false);
    };
    Ok(points.iter().all(|point| point_in_polygon(*point, &zone.polygon)))
}

/// `validate_movement_runtime_state`: accepts, or leaves it to the Python rule and its error text.
#[pyfunction]
fn validate(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    let Some(held) = exact_dict(&table)?.get_item(KEY)? else {
        return Ok(true);
    };
    if held.is_none() {
        return Ok(true);
    }
    let mirror = mirror_held(world)?;
    let store = entities_of(world)?;
    let zones = attribute(world, intern!(py, "zones"))?;
    let zones = exact_dict(&zones)?;
    let edges = attribute(world, intern!(py, "zone_edges"))?;
    let edges = exact_dict(&edges)?;
    // Geo::of may run Python, which the row's borrow must not see: only journeys in rooms need it.
    let needs_plan = NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| {
        Ok(row.journeys.values().any(|journey| !journey.route_points.is_empty()))
    })?;
    let mut geo = if needs_plan { Some(Geo::of(world)?) } else { None };
    NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| {
        if row.schema_version != 2 || &*row.policy_version != POLICY_VERSION {
            return Ok(false);
        }
        EntityStore::with_rows(&store, |rows| {
            if row.positions.len() != rows.agents.len() || row.staff_positions.len() != rows.staff.len() {
                return Ok(false);
            }
            for (id, position) in &row.positions {
                if rows.agents.get(id).is_none() || !position_is_plain(position, zones, edges)? {
                    return Ok(false);
                }
            }
            for (id, position) in &row.staff_positions {
                if rows.staff.get(id).is_none() || !position_is_plain(position, zones, edges)? {
                    return Ok(false);
                }
            }
            for (id, journey) in &row.journeys {
                let Some(agent) = rows.agents.get(id) else {
                    return Ok(false);
                };
                if !journey_is_valid(world, agent, journey, zones, edges, &mut geo)? {
                    return Ok(false);
                }
            }
            Ok(true)
        })
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(advance_movement, m)?)?;
    m.add_function(wrap_pyfunction!(start_movement, m)?)?;
    m.add_function(wrap_pyfunction!(finish_movement, m)?)?;
    m.add_function(wrap_pyfunction!(route_with_movement_time, m)?)?;
    m.add_function(wrap_pyfunction!(plan_in_room_route, m)?)?;
    m.add_function(wrap_pyfunction!(route_is_physically_clear, m)?)?;
    m.add_function(wrap_pyfunction!(zone_center, m)?)?;
    m.add_function(wrap_pyfunction!(queue_slot_position, m)?)?;
    m.add_function(wrap_pyfunction!(facility_approach_route, m)?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    Ok(())
}
