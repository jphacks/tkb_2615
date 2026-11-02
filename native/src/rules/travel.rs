//! Where a person walks: `_route`, `_schedule_travel`, and the choice of a facility
//! (`experiments/phases/propose.py`, `policies/space.py`). The Python rules stay for the comparison
//! and for what's refused here, which says so before it writes (docs/rust-port/lanes/m4-service.md):
//! another route policy. Edge capacities count every trip under way (docs/rust-port/lanes/l2-staff.md).

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, HashSet};
use std::sync::{Arc, LazyLock, Mutex};

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyString, PyTuple};

use crate::core::row::{attribute, exact_dict, exact_int, exact_str, unsupported};
use crate::core::value::V;
use crate::core::world_core::{raw_dict_get, NamespaceMirror};
use crate::rules::agent_dynamics::agent_knows_facility;
use crate::rules::handlers::{log_record, new_event, NewEvent};
use crate::rules::movement::{
    approach, mirror_held, mobility_text, movement_minutes, optional_str, plan_start, route_points_of, str_tuple, write_tree, Changes, Scene,
    EXACT_INT, KEY,
};
use crate::rules::movement_geometry::{Geo, Pt};
use crate::rules::service::{active_task_class, set_agent};
use crate::state::entities::{entities_of, Activity, Entities, EntityStore, FacilityRow};
use crate::state::events::{EventKind, EventRef, EventRow, EventWriter, PendingEvent, PendingSource};
use crate::state::movement::{Journey, MovementRow, Point};
use crate::state::staff_operations::{Phase, StaffOperationsRow, NAME as STAFF_OPERATIONS};
use crate::Unsupported;

const ROUTING_SOURCE: &str = "ASM-ZONE-ROUTING-V1";
const ROUTING_POLICY: &str = "shortest-travel-time-v1";
const CAPACITY_SOURCE: &str = "ASM-A05-ROUTE-EDIT-V1";

/// `edge_loads`: looked up, never walked, so its order decides nothing.
pub(crate) type EdgeLoads = HashMap<Arc<str>, i64>;

struct Edge {
    id: Arc<str>,
    to: Arc<str>,
    minutes: i64,
    /// `max_agents_in_transit`.
    capacity: Option<i64>,
}

/// A controller's route graph as rows, kept while `self.space.graph` is the same object.
#[pyclass(module = "sheltersim_native", frozen)]
pub struct RouteRows {
    graph: Py<PyAny>,
    /// `controller.route_capacities`, read when it is asked.
    capacities: Option<Py<PyDict>>,
    zones: HashSet<Arc<str>>,
    outgoing: HashMap<Arc<str>, Vec<Edge>>,
    /// The edges closed to a mobility in a floor plan, kept while that plan is the same.
    closed: Mutex<Vec<(&'static str, Arc<Geo>, Arc<HashSet<Arc<str>>>)>>,
}

/// A route: `ZoneRoute` of a walk through the graph.
pub(crate) struct Route {
    pub(crate) origin: Arc<str>,
    pub(crate) destination: Arc<str>,
    pub(crate) zone_ids: Vec<Arc<str>>,
    pub(crate) edge_ids: Vec<Arc<str>>,
    pub(crate) minutes: i64,
}

impl Route {
    fn within(zone: &Arc<str>) -> Self {
        Self { origin: zone.clone(), destination: zone.clone(), zone_ids: vec![zone.clone()], edge_ids: Vec::new(), minutes: 0 }
    }
}

fn read_graph(graph: &Bound<'_, PyAny>, capacities: Option<&Bound<'_, PyDict>>) -> PyResult<RouteRows> {
    let py = graph.py();
    let zones_object = attribute(graph, intern!(py, "zones"))?;
    let mut zones = HashSet::new();
    let mut outgoing: HashMap<Arc<str>, Vec<Edge>> = HashMap::new();
    for key in zones_object.try_iter()? {
        zones.insert(Arc::from(exact_str(&key?)?));
    }
    let edges = attribute(graph, intern!(py, "edges"))?;
    for edge in edges.downcast_exact::<PyTuple>().map_err(|_| Unsupported::new_err("the edges are not a tuple"))?.iter() {
        let closed = attribute(&edge, intern!(py, "closed"))?;
        if closed.downcast_exact::<PyBool>().map_err(|_| Unsupported::new_err("closed is not a bool"))?.is_true() {
            continue;
        }
        let capacity = attribute(&edge, intern!(py, "max_agents_in_transit"))?;
        let capacity = if capacity.is_none() { None } else { Some(exact_int(&capacity)?) };
        // `loads.get(edge_id, 0) >= capacity`, with no trips counted.
        if capacity.is_some_and(|capacity| capacity <= 0) {
            continue;
        }
        let from: Arc<str> = Arc::from(exact_str(&attribute(&edge, intern!(py, "from_zone_id"))?)?);
        outgoing.entry(from).or_default().push(Edge {
            id: Arc::from(exact_str(&attribute(&edge, intern!(py, "id"))?)?),
            to: Arc::from(exact_str(&attribute(&edge, intern!(py, "to_zone_id"))?)?),
            minutes: exact_int(&attribute(&edge, intern!(py, "travel_minutes"))?)?,
            capacity,
        });
    }
    Ok(RouteRows { graph: graph.clone().unbind(), capacities: capacities.map(|table| table.clone().unbind()), zones, outgoing, closed: Mutex::default() })
}

/// The graph of this controller, or Unsupported for another policy.
pub(crate) fn rows_of<'py>(controller: &Bound<'py, PyAny>) -> PyResult<Bound<'py, RouteRows>> {
    let py = controller.py();
    let capacities = attribute(controller, intern!(py, "route_capacities"))?;
    let capacities = exact_dict(&capacities)?;
    let space = attribute(controller, intern!(py, "space"))?;
    router_rows(&space, Some(capacities), controller)
}

/// The graph of a `SpaceRouter`, kept on `holder` while the router's graph is the same object.
fn router_rows<'py>(router: &Bound<'py, PyAny>, capacities: Option<&Bound<'py, PyDict>>, holder: &Bound<'py, PyAny>) -> PyResult<Bound<'py, RouteRows>> {
    let py = router.py();
    let policy = attribute(router, intern!(py, "policy"))?;
    if policy.get_type().name()?.to_str()? != "ShortestTravelTimePolicy" {
        return unsupported("another route policy");
    }
    let graph = attribute(router, intern!(py, "graph"))?;
    if let Ok(known) = holder.getattr(intern!(py, "_route_rows")) {
        if let Ok(rows) = known.downcast_exact::<RouteRows>() {
            let held = rows.get();
            let same_capacities = match (&held.capacities, capacities) {
                (Some(first), Some(second)) => first.bind(py).is(second),
                (None, None) => true,
                _ => false,
            };
            if held.graph.bind(py).is(&graph) && same_capacities {
                return Ok(rows.clone());
            }
        }
    }
    let rows = Bound::new(py, read_graph(&graph, capacities)?)?;
    holder.setattr(intern!(py, "_route_rows"), &rows)?;
    Ok(rows)
}

/// The graph of `assign_staff_operations`' router, whose routes `edge_loads` always reach.
pub(crate) fn staff_router_rows<'py>(router: &Bound<'py, PyAny>) -> PyResult<Bound<'py, RouteRows>> {
    router_rows(router, None, router)
}

impl RouteRows {
    /// `_physically_closed_edges`, with the plan a world has now.
    fn closed_to(&self, geo: &Arc<Geo>, mobility: &'static str) -> PyResult<Arc<HashSet<Arc<str>>>> {
        let mut known = self.closed.lock().map_err(|_| Unsupported::new_err("the closed edges are poisoned"))?;
        if let Some((_, _, closed)) = known.iter().find(|(name, plan, _)| *name == mobility && Arc::ptr_eq(plan, geo)) {
            return Ok(closed.clone());
        }
        let Some(layout) = &geo.layout else {
            return Ok(Arc::new(HashSet::new()));
        };
        let closed: HashSet<Arc<str>> = layout.routes.keys().filter(|id| !geo.route_is_clear(id, mobility)).cloned().collect();
        let closed = Arc::new(closed);
        known.retain(|(name, _, _)| *name != mobility);
        known.push((mobility, geo.clone(), closed.clone()));
        Ok(closed)
    }

    /// The edges of this graph `route_is_physically_clear` turns down, for a person of this mobility.
    pub(crate) fn edges_closed_by(&self, geo: &Geo, mobility: &str) -> HashSet<Arc<str>> {
        self.outgoing.values().flatten().filter(|edge| !geo.route_is_clear(&edge.id, mobility)).map(|edge| edge.id.clone()).collect()
    }

    /// `ShortestTravelTimePolicy.route`, without these edges and with these trips counted.
    pub(crate) fn shortest(&self, closed: &HashSet<Arc<str>>, loads: Option<&EdgeLoads>, origin: &Arc<str>, destination: &Arc<str>) -> PyResult<Option<Route>> {
        if !self.zones.contains(origin) || !self.zones.contains(destination) {
            return unsupported("an unknown zone");
        }
        if origin == destination {
            return Ok(Some(Route::within(origin)));
        }
        type Score = (i64, Vec<Arc<str>>, Vec<Arc<str>>);
        let start: Score = (0, Vec::new(), vec![origin.clone()]);
        let mut best: HashMap<Arc<str>, Score> = HashMap::new();
        best.insert(origin.clone(), start.clone());
        let mut pending = BinaryHeap::new();
        pending.push(Reverse((start, origin.clone())));
        while let Some(Reverse((score, current))) = pending.pop() {
            if best.get(&current) != Some(&score) {
                continue;
            }
            let (minutes, edge_ids, zone_ids) = score;
            if &current == destination {
                return Ok(Some(Route { origin: origin.clone(), destination: destination.clone(), zone_ids, edge_ids, minutes }));
            }
            let Some(edges) = self.outgoing.get(&current) else {
                continue;
            };
            for edge in edges {
                if closed.contains(&edge.id) {
                    continue;
                }
                if let (Some(capacity), Some(loads)) = (edge.capacity, loads) {
                    if loads.get(&edge.id).copied().unwrap_or(0) >= capacity {
                        continue;
                    }
                }
                let Some(total) = minutes.checked_add(edge.minutes) else {
                    return unsupported("a route's minutes past 64 bits");
                };
                let mut next_edges = edge_ids.clone();
                next_edges.push(edge.id.clone());
                let mut next_zones = zone_ids.clone();
                next_zones.push(edge.to.clone());
                let candidate: Score = (total, next_edges, next_zones);
                if best.get(&edge.to).is_none_or(|known| candidate < *known) {
                    best.insert(edge.to.clone(), candidate.clone());
                    pending.push(Reverse((candidate, edge.to.clone())));
                }
            }
        }
        Ok(None)
    }
}

/// `SpaceRouter(ZoneGraph.from_states(zones, edges)).route(...)` with nothing closed, for a rule
/// that has no controller: `edge_capacities` are refused, which read every trip under way.
pub(crate) fn world_route(world: &Bound<'_, PyAny>, origin: &Arc<str>, destination: &Arc<str>) -> PyResult<Option<Route>> {
    let py = world.py();
    let graph = py.import("sheltersim.domain.zones")?.getattr("ZoneGraph")?.call_method1(
        intern!(py, "from_states"),
        (attribute(world, intern!(py, "zones"))?, attribute(world, intern!(py, "zone_edges"))?),
    )?;
    read_graph(&graph, None)?.shortest(&HashSet::new(), None, origin, destination)
}

/// `ZoneGraph.from_states(world.zones, world.zone_edges, edge_capacities=capacities)` as rows, for
/// the routes `_support_route_wait_reasons` reads. What the constructor would turn down is refused.
pub(crate) fn world_graph(world: &Bound<'_, PyAny>) -> PyResult<RouteRows> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let edges = attribute(world, intern!(py, "zone_edges"))?;
    let edges = exact_dict(&edges)?;
    let mut capacities: HashMap<String, i64> = HashMap::new();
    if let Some(routing) = exact_dict(&configs)?.get_item(intern!(py, "layout_routing"))? {
        if let Some(table) = exact_dict(&routing)?.get_item(intern!(py, "max_agents_in_transit_by_edge"))? {
            for (edge, capacity) in exact_dict(&table)?.iter() {
                let capacity = exact_int(&capacity)?;
                if !edges.contains(&edge)? || !(1..=100).contains(&capacity) {
                    return unsupported("a capacity the zone graph turns down");
                }
                capacities.insert(exact_str(&edge)?.to_owned(), capacity);
            }
        }
    }
    let zones_object = attribute(world, intern!(py, "zones"))?;
    let mut zones = HashSet::new();
    for key in exact_dict(&zones_object)?.keys() {
        zones.insert(Arc::<str>::from(exact_str(&key)?));
    }
    let mut outgoing: HashMap<Arc<str>, Vec<Edge>> = HashMap::new();
    for edge in edges.values() {
        let from: Arc<str> = Arc::from(exact_str(&attribute(&edge, intern!(py, "from_zone_id"))?)?);
        let to: Arc<str> = Arc::from(exact_str(&attribute(&edge, intern!(py, "to_zone_id"))?)?);
        if !zones.contains(&from) || !zones.contains(&to) || from == to {
            return unsupported("an edge the zone graph turns down");
        }
        let minutes = exact_int(&attribute(&edge, intern!(py, "travel_minutes"))?)?;
        if minutes <= 0 {
            return unsupported("an edge the zone graph turns down");
        }
        let id = attribute(&edge, intern!(py, "id"))?;
        let id = exact_str(&id)?;
        let closed = attribute(&edge, intern!(py, "closed"))?;
        if closed.downcast_exact::<PyBool>().map_err(|_| Unsupported::new_err("closed is not a bool"))?.is_true() {
            continue;
        }
        outgoing.entry(from).or_default().push(Edge { id: Arc::from(id), to, minutes, capacity: capacities.get(id).copied() });
    }
    Ok(RouteRows { graph: py.None(), capacities: None, zones, outgoing, closed: Mutex::default() })
}

// ---- Who is on an edge --------------------------------------------------------------------------

/// `event.payload.get(name)`, of an event in the queue.
fn pending_payload_text(event: &PendingEvent<'_>, name: &str) -> PyResult<Option<Arc<str>>> {
    match &event.source {
        PendingSource::Row(row) => EventRef::Row(row).text(name),
        PendingSource::Object(object) => EventRef::Object(object).text(name),
    }
}

fn pending_type_is(event: &PendingEvent<'_>, wanted: &str) -> PyResult<bool> {
    Ok(match &event.source {
        PendingSource::Row(row) => &*row.event_type == wanted,
        PendingSource::Object(object) => exact_str(&attribute(object, intern!(object.py(), "event_type"))?)? == wanted,
    })
}

/// `event.payload.get("route_edge_ids", ())`: the IDs that are str. A value that is not a list is
/// no plain trip's, and is left to the Python rule.
fn pending_edges(event: &PendingEvent<'_>) -> PyResult<Vec<Arc<str>>> {
    match &event.source {
        PendingSource::Row(row) => {
            let V::Map(payload) = &row.payload else {
                return unsupported("a payload is not a map");
            };
            match payload.get("route_edge_ids") {
                None => Ok(Vec::new()),
                Some(V::List(items)) => Ok(items.iter().filter_map(|item| if let V::Str(id) = item { Some(id.clone()) } else { None }).collect()),
                Some(_) => unsupported("a trip's edges are not a list"),
            }
        }
        PendingSource::Object(object) => {
            let py = object.py();
            let payload = attribute(object, intern!(py, "payload"))?;
            let edges = payload.call_method1(intern!(py, "get"), (intern!(py, "route_edge_ids"),))?;
            if edges.is_none() {
                return Ok(Vec::new());
            }
            if !edges.is_exact_instance_of::<PyList>() && !edges.is_exact_instance_of::<PyTuple>() {
                return unsupported("a trip's edges are not a list");
            }
            let mut ids = Vec::new();
            for item in edges.try_iter()? {
                if let Ok(id) = exact_str(&item?) {
                    ids.push(Arc::from(id));
                }
            }
            Ok(ids)
        }
    }
}

fn pending_events<'py>(world: &Bound<'py, PyAny>) -> PyResult<Vec<PendingEvent<'py>>> {
    let writer = EventWriter::of(world)?;
    let pending = writer.queue().borrow().pending_events(world.py())?;
    pending.ok_or_else(|| Unsupported::new_err("an event in the queue is not a plain one"))
}

/// `event.payload.get("task_id")` of a `staff_operation_relief` in the queue.
pub(crate) fn relief_task_of(event: &PendingEvent<'_>) -> PyResult<Option<Arc<str>>> {
    if pending_type_is(event, "staff_operation_relief")? {
        pending_payload_text(event, "task_id")
    } else {
        Ok(None)
    }
}

fn staff_operations_mirror<'py>(world: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, NamespaceMirror>>> {
    if crate::core::row::namespace_is_absent(world, intern!(world.py(), STAFF_OPERATIONS))? {
        return Ok(None);
    }
    crate::rules::staff::mirror_held(world, STAFF_OPERATIONS).map(Some)
}

/// `_active_route_edge_loads` and the loads of `_support_route_wait_reasons`: every edge of each
/// trip that ends after this minute, and of each staff member on the way.
pub(crate) fn active_edge_loads(world: &Bound<'_, PyAny>, minute: i64) -> PyResult<EdgeLoads> {
    let mut loads = EdgeLoads::new();
    for event in pending_events(world)? {
        if event.due_minute <= minute || !pending_type_is(&event, "agent_travel_complete")? {
            continue;
        }
        for edge in pending_edges(&event)? {
            *loads.entry(edge).or_insert(0) += 1;
        }
    }
    if let Some(mirror) = staff_operations_mirror(world)? {
        NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror, |row| {
            for record in row.staff.values() {
                if record.phase == Phase::Travel {
                    for edge in &record.route_edge_ids {
                        *loads.entry(edge.clone()).or_insert(0) += 1;
                    }
                }
            }
            Ok(())
        })?;
    }
    Ok(loads)
}

/// `_current_journey_edge`, of a journey with these edges and times.
fn current_edge(edges_object: &Bound<'_, PyAny>, ids: &[Arc<str>], start: i64, due: i64, minute: i64) -> PyResult<Option<Arc<str>>> {
    let py = edges_object.py();
    if ids.is_empty() {
        return Ok(None);
    }
    let duration = due - start;
    if duration <= 0 {
        return Ok(None);
    }
    let elapsed = minute - start;
    let mut weights = Vec::with_capacity(ids.len());
    for id in ids {
        let Some(edge) = raw_dict_get(edges_object, PyString::new(py, id).as_any())? else {
            return unsupported("a trip's edge is not in the world");
        };
        weights.push(exact_int(&attribute(&edge, intern!(py, "travel_minutes"))?)?);
    }
    let mut total = 0_i64;
    for weight in &weights {
        total = total.checked_add(*weight).ok_or_else(|| Unsupported::new_err("a trip's minutes past 64 bits"))?;
    }
    if total <= 0 {
        return Ok(Some(ids[0].clone()));
    }
    if [elapsed.abs(), duration, total].iter().any(|value| *value >= EXACT_INT) {
        return unsupported("a trip's minutes do not fit a float");
    }
    // `elapsed / duration * total`, from the left: an int's true division is the floats' here.
    let scaled = elapsed as f64 / duration as f64 * total as f64;
    let mut cumulative = 0_i64;
    for (id, weight) in ids.iter().zip(&weights) {
        if scaled < (cumulative + weight) as f64 {
            return Ok(Some(id.clone()));
        }
        cumulative += weight;
    }
    Ok(ids.last().cloned())
}

/// `shared_passage_edge_loads`: who is on which edge at this minute.
pub(crate) fn passage_edge_loads(world: &Bound<'_, PyAny>, minute: i64) -> PyResult<EdgeLoads> {
    let py = world.py();
    let edges = attribute(world, intern!(py, "zone_edges"))?;
    let store = entities_of(world)?;
    let mut loads = EdgeLoads::new();
    let mut represented: HashSet<Arc<str>> = HashSet::new();
    let table = attribute(world, intern!(py, "model_state"))?;
    if exact_dict(&table)?.contains(KEY)? {
        let trips = NamespaceMirror::with_row::<MovementRow, _>(&mirror_held(world)?, |row| {
            Ok(row
                .journeys
                .iter()
                .filter(|(_, journey)| journey.started_minute <= minute && minute < journey.due_minute)
                .map(|(agent, journey)| (agent.clone(), journey.route_edge_ids.clone(), journey.started_minute, journey.due_minute))
                .collect::<Vec<_>>())
        })?;
        for (agent, ids, start, due) in trips {
            if let Some(edge) = current_edge(&edges, &ids, start, due, minute)? {
                *loads.entry(edge).or_insert(0) += 1;
                represented.insert(agent);
            }
        }
    }
    for event in pending_events(world)? {
        if !pending_type_is(&event, "agent_travel_complete")? {
            continue;
        }
        if pending_payload_text(&event, "agent_id")?.is_some_and(|agent| represented.contains(&agent)) {
            continue;
        }
        if pending_payload_text(&event, "origin_zone_id")?.is_none_or(|zone| zone.is_empty()) {
            continue;
        }
        let ids = pending_edges(&event)?;
        if ids.is_empty() {
            continue;
        }
        let Some(task_id) = pending_payload_text(&event, "task_id")? else {
            continue;
        };
        let task = EntityStore::with_rows(&store, |rows| Ok(rows.active_tasks.get(&*task_id).map(|task| (task.start_minute, task.due_minute))))?;
        let Some((start, due)) = task else {
            continue;
        };
        if start <= minute && minute < due {
            if let Some(edge) = current_edge(&edges, &ids, start, due, minute)? {
                *loads.entry(edge).or_insert(0) += 1;
            }
        }
    }
    if let Some(mirror) = staff_operations_mirror(world)? {
        let trips = NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror, |row| {
            let mut trips = Vec::new();
            for record in row.staff.values() {
                if record.phase == Phase::Travel {
                    let (Some(start), Some(due)) = (record.started_minute, record.travel_due_minute) else {
                        return unsupported("a trip of the staff has no times");
                    };
                    if start <= minute && minute < due {
                        trips.push((record.route_edge_ids.clone(), start, due));
                    }
                }
            }
            Ok(trips)
        })?;
        for (ids, start, due) in trips {
            if let Some(edge) = current_edge(&edges, &ids, start, due, minute)? {
                *loads.entry(edge).or_insert(0) += 1;
            }
        }
    }
    Ok(loads)
}

// ---- _route ---------------------------------------------------------------------------------------

/// `_record_route_capacity_wait`, not done yet: a rule plans its route before it writes anything.
pub(crate) struct CapacityWait {
    agent_id: Arc<str>,
    origin: Arc<str>,
    destination: Arc<str>,
    edge_id: Arc<str>,
    capacity: i64,
    active: i64,
}

/// `_record_route_capacity_wait`.
pub(crate) fn write_capacity_wait(world: &Bound<'_, PyAny>, writer: &EventWriter<'_>, wait: CapacityWait) -> PyResult<()> {
    let py = world.py();
    let minute = attribute(world, intern!(py, "minute_index"))?;
    let sequence = writer.allocate_reserved()?;
    let payload = PyDict::new(py);
    payload.set_item("agent_id", &*wait.agent_id)?;
    payload.set_item("origin_zone_id", &*wait.origin)?;
    payload.set_item("destination_zone_id", &*wait.destination)?;
    payload.set_item("edge_id", &*wait.edge_id)?;
    payload.set_item("capacity", wait.capacity)?;
    payload.set_item("active_travelers", wait.active)?;
    let provenance = PyDict::new(py);
    provenance.set_item("source_type", "assumption")?;
    provenance.set_item("source_id", CAPACITY_SOURCE)?;
    let event = new_event(
        py,
        NewEvent {
            event_id: &format!("zone-edge-capacity-wait-{sequence}"),
            kind: "ENDOGENOUS",
            due_minute: exact_int(&minute)?,
            priority: 0,
            sequence,
            event_type: "zone_edge_capacity_wait",
            payload: payload.into_any(),
            source_id: Some(PyString::new(py, CAPACITY_SOURCE).into_any()),
            causation_id: None,
            provenance: provenance.into_any(),
        },
    )?;
    attribute(world, intern!(py, "event_log"))?.call_method1(
        intern!(py, "append"),
        (log_record(py, &event, &minute, "SUPPRESSED", Some("route edge capacity is full; the Agent remains at its origin"))?,),
    )?;
    Ok(())
}

/// `_route`, for a person in the world, changing nothing: the waits it would record go in `waits`.
pub(crate) fn route_for<'py>(
    world: &Bound<'py, PyAny>,
    graph: &Bound<'py, RouteRows>,
    origin: &Arc<str>,
    destination: &Arc<str>,
    agent_id: &str,
    waits: &mut Vec<CapacityWait>,
) -> PyResult<Option<Route>> {
    let py = world.py();
    let store = entities_of(world)?;
    let mobility = EntityStore::with_rows(&store, |rows| {
        let Some(agent) = rows.agents.get(agent_id) else {
            return unsupported("no such agent");
        };
        Ok(mobility_text(agent))
    })?;
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let has_plan = exact_dict(&configs)?.get_item(intern!(py, "facility_geometry"))?.is_some_and(|raw| !raw.is_none());
    let closed = if has_plan {
        graph.get().closed_to(&Geo::of(world)?, mobility)?
    } else {
        Arc::new(HashSet::new())
    };
    let capacities = graph.get().capacities.as_ref().map(|table| table.bind(py)).filter(|table| !table.is_empty());
    let found = match capacities {
        None => graph.get().shortest(&closed, None, origin, destination)?,
        Some(table) => {
            let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
            let loads = active_edge_loads(world, minute)?;
            let found = graph.get().shortest(&closed, Some(&loads), origin, destination)?;
            if found.is_none() {
                if let Some(free) = graph.get().shortest(&closed, None, origin, destination)? {
                    for edge in &free.edge_ids {
                        let Some(capacity) = table.get_item(&**edge)? else {
                            continue;
                        };
                        if capacity.is_none() {
                            continue;
                        }
                        let capacity = exact_int(&capacity)?;
                        let active = loads.get(edge).copied().unwrap_or(0);
                        if active >= capacity {
                            waits.push(CapacityWait {
                                agent_id: Arc::from(agent_id),
                                origin: origin.clone(),
                                destination: destination.clone(),
                                edge_id: edge.clone(),
                                capacity,
                                active,
                            });
                            break;
                        }
                    }
                }
            }
            found
        }
    };
    let Some(mut route) = found else {
        return Ok(None);
    };
    let zones: Vec<&str> = route.zone_ids.iter().map(|id| &**id).collect();
    let edges: Vec<&str> = route.edge_ids.iter().map(|id| &**id).collect();
    route.minutes = movement_minutes(world, agent_id, &zones, &edges, route.minutes)?;
    Ok(Some(route))
}

pub(crate) fn route_class(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    py.import("sheltersim.policies.space")?.getattr("ZoneRoute")
}

/// `_route`.
#[pyfunction]
fn route<'py>(
    controller: &Bound<'py, PyAny>,
    origin_zone_id: &Bound<'py, PyAny>,
    destination_zone_id: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = controller.py();
    let world = attribute(controller, intern!(py, "world"))?;
    let graph = rows_of(controller)?;
    let origin: Arc<str> = Arc::from(exact_str(origin_zone_id)?);
    let destination: Arc<str> = Arc::from(exact_str(destination_zone_id)?);
    let mut waits = Vec::new();
    let found = route_for(&world, &graph, &origin, &destination, exact_str(agent_id)?, &mut waits)?;
    if !waits.is_empty() {
        let writer = EventWriter::of(&world)?;
        writer.reserve(waits.len());
        for wait in waits {
            write_capacity_wait(&world, &writer, wait)?;
        }
    }
    let Some(route) = found else {
        return Ok(py.None().into_bound(py));
    };
    let zones = PyTuple::new(py, route.zone_ids.iter().map(|id| &**id))?;
    let edges = PyTuple::new(py, route.edge_ids.iter().map(|id| &**id))?;
    route_class(py)?.call1((origin_zone_id, destination_zone_id, zones, edges, route.minutes))
}

// ---- _schedule_travel ---------------------------------------------------------------------------

/// What a trip needs beside its route.
pub(crate) struct Trip<'a> {
    pub(crate) agent_id: Arc<str>,
    pub(crate) route: Route,
    pub(crate) points: Vec<Pt>,
    pub(crate) purpose: &'a str,
    pub(crate) facility_id: Option<Arc<str>>,
    pub(crate) minute: i64,
    pub(crate) causation_id: Option<Arc<str>>,
    pub(crate) support_request_id: Option<Arc<str>>,
    pub(crate) support_staff_id: Option<Arc<str>>,
}

/// A trip planned, with nothing changed: its writes cannot be refused.
pub(crate) struct PlannedTrip {
    agent_id: Arc<str>,
    task_id: Arc<str>,
    task_kind: &'static str,
    target_id: Arc<str>,
    minute: i64,
    due: i64,
    event: EventRow,
    changes: Option<Changes>,
}

const APPROACH_SOURCE: &str = "ASM-SPATIAL-MOVEMENT-V2";
const APPROACH_POLICY: &str = "spatial-route-and-room-waypoint-v2";
const APPROACH_EVENT_SOURCE: &str = "agent-facility-approach-v2";

struct Names {
    agent_id: Arc<str>,
    task_id: Arc<str>,
    purpose: Arc<str>,
    destination_zone_id: Arc<str>,
    facility_id: Arc<str>,
    route_zone_ids: Arc<str>,
    route_edge_ids: Arc<str>,
    travel_minutes: Arc<str>,
    origin_zone_id: Arc<str>,
    request_id: Arc<str>,
    staff_id: Arc<str>,
    source_type: Arc<str>,
    source_id: Arc<str>,
    policy_version: Arc<str>,
    assumption: Arc<str>,
    routing_policy: Arc<str>,
    event_type: Arc<str>,
}

static NAMES: LazyLock<Names> = LazyLock::new(|| Names {
    agent_id: Arc::from("agent_id"),
    task_id: Arc::from("task_id"),
    purpose: Arc::from("purpose"),
    destination_zone_id: Arc::from("destination_zone_id"),
    facility_id: Arc::from("facility_id"),
    route_zone_ids: Arc::from("route_zone_ids"),
    route_edge_ids: Arc::from("route_edge_ids"),
    travel_minutes: Arc::from("travel_minutes"),
    origin_zone_id: Arc::from("origin_zone_id"),
    request_id: Arc::from("request_id"),
    staff_id: Arc::from("staff_id"),
    source_type: Arc::from("source_type"),
    source_id: Arc::from("source_id"),
    policy_version: Arc::from("policy_version"),
    assumption: Arc::from("assumption"),
    routing_policy: Arc::from(ROUTING_POLICY),
    event_type: Arc::from("agent_travel_complete"),
});

fn strings(ids: &[Arc<str>]) -> V {
    V::List(ids.iter().map(|id| V::Str(id.clone())).collect())
}

/// `_schedule_travel`, changing nothing. None is its False. Refused before anything is written, the
/// Python rule then says what it says.
pub(crate) fn plan_travel(world: &Bound<'_, PyAny>, writer: &EventWriter<'_>, trip: Trip<'_>) -> PyResult<Option<PlannedTrip>> {
    let py = world.py();
    let store = entities_of(world)?;
    let Trip { agent_id, route, points, purpose, facility_id, minute, causation_id, support_request_id, support_staff_id } = trip;
    let is_origin = EntityStore::with_rows(&store, |rows| {
        let Some(agent) = rows.agents.get(&agent_id) else {
            return unsupported("no such agent");
        };
        Ok(agent.zone_id == route.origin)
    })?;
    if !is_origin {
        return unsupported("the route starts elsewhere");
    }
    if route.minutes <= 0 {
        return Ok(None);
    }
    if route.minutes >= EXACT_INT {
        return unsupported("a journey has no plain duration");
    }
    let Some(due) = minute.checked_add(route.minutes) else {
        return unsupported("a due minute out of range");
    };
    if due >= writer.duration() {
        return Ok(None);
    }
    let zones = attribute(world, intern!(py, "zones"))?;
    let Some(zone) = raw_dict_get(&zones, PyString::new(py, &route.destination).as_any())? else {
        return unsupported("no such zone");
    };
    let capacity = attribute(&zone, intern!(py, "capacity"))?;
    if !capacity.is_none() && route.origin != route.destination {
        let capacity = exact_int(&capacity)?;
        let (occupancy, incoming) = EntityStore::with_rows(&store, |rows| {
            let mut occupancy = 0;
            let mut incoming = 0;
            for (_, other) in rows.agents.iter() {
                occupancy += i64::from(other.zone_id == route.destination);
                if other.activity == Activity::Moving {
                    if let Some(target) = &other.target_id {
                        let place = rows.facilities.get(target).map_or(target, |facility| &facility.zone_id);
                        incoming += i64::from(*place == route.destination);
                    }
                }
            }
            Ok((occupancy, incoming))
        })?;
        if occupancy + incoming >= capacity {
            return Ok(None);
        }
    }

    let sequence = writer.next_sequence()?;
    let event_id: Arc<str> = Arc::from(format!("agent-travel-complete-{sequence}"));
    let task_id: Arc<str> = Arc::from(format!("agent-travel-{sequence}"));
    {
        let past = writer.log().borrow();
        let task_is_taken = EntityStore::with_rows(&store, |rows| Ok(rows.active_tasks.get(&task_id).is_some()))?;
        if past.has_event_id(&event_id) || past.has_sequence(sequence) || writer.queue().borrow().has_event(&event_id) || task_is_taken {
            return unsupported("the trip's identity is taken");
        }
    }
    let is_facility_trip = matches!(purpose, "toilet_request" | "resource_request");
    let target_id = if is_facility_trip {
        match &facility_id {
            Some(id) => id.clone(),
            None => return unsupported("a facility's trip with no facility"),
        }
    } else {
        route.destination.clone()
    };
    let task_kind = match purpose {
        "toilet_request" => "travel_to_toilet",
        "resource_request" => "travel_to_resource",
        "staff_support_travel" => "staff_support_travel",
        _ => "travel_return",
    };
    let names = &*NAMES;
    let mut payload = IndexMap::with_capacity(11);
    payload.insert(names.agent_id.clone(), V::Str(agent_id.clone()));
    payload.insert(names.task_id.clone(), V::Str(task_id.clone()));
    let purpose_arc: Arc<str> = Arc::from(purpose);
    payload.insert(names.purpose.clone(), V::Str(purpose_arc.clone()));
    payload.insert(names.destination_zone_id.clone(), V::Str(route.destination.clone()));
    payload.insert(names.facility_id.clone(), facility_id.as_ref().map_or(V::Null, |id| V::Str(id.clone())));
    payload.insert(names.route_zone_ids.clone(), strings(&route.zone_ids));
    payload.insert(names.route_edge_ids.clone(), strings(&route.edge_ids));
    payload.insert(names.travel_minutes.clone(), V::Int(route.minutes));
    if purpose == "resource_request" {
        payload.insert(names.origin_zone_id.clone(), V::Str(route.origin.clone()));
    }
    if purpose == "staff_support_travel" {
        let (Some(request), Some(staff)) = (&support_request_id, &support_staff_id) else {
            return unsupported("a support trip without its IDs");
        };
        if request.is_empty() || staff.is_empty() {
            return unsupported("a support trip without its IDs");
        }
        payload.insert(names.request_id.clone(), V::Str(request.clone()));
        payload.insert(names.staff_id.clone(), V::Str(staff.clone()));
    }
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let source: Arc<str> = match exact_dict(&configs)?.get_item(intern!(py, "layout_routing"))? {
        Some(config) => match exact_dict(&config)?.get_item(intern!(py, "source_id"))? {
            Some(source) => Arc::from(exact_str(&source)?),
            None => Arc::from(ROUTING_SOURCE),
        },
        None => Arc::from(ROUTING_SOURCE),
    };
    let mut provenance = IndexMap::with_capacity(3);
    provenance.insert(names.source_type.clone(), V::Str(names.assumption.clone()));
    provenance.insert(names.source_id.clone(), V::Str(source));
    provenance.insert(names.policy_version.clone(), V::Str(names.routing_policy.clone()));
    let Some(event) = EventRow::new(
        event_id.clone(),
        EventKind::Completion,
        due,
        0,
        sequence,
        names.event_type.clone(),
        payload,
        Some(task_id.clone()),
        causation_id,
        provenance,
    ) else {
        return unsupported("the event's values are not what an event takes");
    };
    let table = attribute(world, intern!(py, "model_state"))?;
    let changes = if exact_dict(&table)?.contains(KEY)? {
        let mirror = mirror_held(world)?;
        let scene = Scene::of(world)?;
        let journey = Journey {
            event_id,
            task_id: task_id.clone(),
            purpose: purpose_arc,
            facility_id,
            destination_zone_id: route.destination.clone(),
            route_zone_ids: route.zone_ids,
            route_edge_ids: route.edge_ids.clone(),
            route_points: points.iter().map(|point| Point { x: point.x, y: point.y }).collect(),
            started_minute: minute,
            due_minute: due,
            queue_id: None,
            support_request_id,
            support_staff_id,
        };
        Some(NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| {
            EntityStore::with_rows(&store, |rows| {
                plan_start(&scene, row, rows, &agent_id, journey, &route.origin, &points, &route.edge_ids, minute, route.minutes)
            })
        })?)
    } else {
        None
    };
    Ok(Some(PlannedTrip { agent_id, task_id, task_kind, target_id, minute, due, event, changes }))
}

/// `_schedule_room_approach` of `events/handlers.py`, changing nothing. None is its False.
pub(crate) fn plan_room_approach(
    world: &Bound<'_, PyAny>,
    writer: &EventWriter<'_>,
    agent_id: &Arc<str>,
    facility_id: &Arc<str>,
    purpose: &str,
    route: Route,
    points: &[Pt],
    minute: i64,
    origin_zone_id: &Arc<str>,
) -> PyResult<Option<PlannedTrip>> {
    let py = world.py();
    let store = entities_of(world)?;
    if route.minutes >= EXACT_INT {
        return unsupported("a journey has no plain duration");
    }
    let Some(due) = minute.checked_add(route.minutes) else {
        return unsupported("a due minute out of range");
    };
    if due >= writer.duration() {
        return Ok(None);
    }
    let sequence = writer.next_sequence()?;
    let event_id: Arc<str> = Arc::from(format!("agent-facility-approach-complete-{sequence}"));
    let task_id: Arc<str> = Arc::from(format!("agent-facility-approach-{sequence}"));
    {
        let past = writer.log().borrow();
        let task_is_taken = EntityStore::with_rows(&store, |rows| Ok(rows.active_tasks.get(&task_id).is_some()))?;
        if past.has_event_id(&event_id) || past.has_sequence(sequence) || writer.queue().borrow().has_event(&event_id) || task_is_taken {
            return unsupported("the approach's identity is taken");
        }
    }
    let task_kind = if purpose == "toilet_request" { "travel_to_toilet" } else { "travel_to_resource" };
    let names = &*NAMES;
    let purpose_arc: Arc<str> = Arc::from(purpose);
    let mut payload = IndexMap::with_capacity(9);
    payload.insert(names.agent_id.clone(), V::Str(agent_id.clone()));
    payload.insert(names.task_id.clone(), V::Str(task_id.clone()));
    payload.insert(names.purpose.clone(), V::Str(purpose_arc.clone()));
    payload.insert(names.destination_zone_id.clone(), V::Str(route.destination.clone()));
    payload.insert(names.facility_id.clone(), V::Str(facility_id.clone()));
    payload.insert(names.origin_zone_id.clone(), V::Str(origin_zone_id.clone()));
    payload.insert(names.route_zone_ids.clone(), strings(&route.zone_ids));
    payload.insert(names.route_edge_ids.clone(), strings(&route.edge_ids));
    payload.insert(names.travel_minutes.clone(), V::Int(route.minutes));
    let mut provenance = IndexMap::with_capacity(3);
    provenance.insert(names.source_type.clone(), V::Str(names.assumption.clone()));
    provenance.insert(names.source_id.clone(), V::Str(Arc::from(APPROACH_SOURCE)));
    provenance.insert(names.policy_version.clone(), V::Str(Arc::from(APPROACH_POLICY)));
    let Some(event) = EventRow::new(
        event_id.clone(),
        EventKind::Completion,
        due,
        0,
        sequence,
        names.event_type.clone(),
        payload,
        Some(Arc::from(APPROACH_EVENT_SOURCE)),
        None,
        provenance,
    ) else {
        return unsupported("the event's values are not what an event takes");
    };
    let table = attribute(world, intern!(py, "model_state"))?;
    let changes = if exact_dict(&table)?.contains(KEY)? {
        let mirror = mirror_held(world)?;
        let scene = Scene::of(world)?;
        let journey = Journey {
            event_id,
            task_id: task_id.clone(),
            purpose: purpose_arc,
            facility_id: Some(facility_id.clone()),
            destination_zone_id: route.destination.clone(),
            route_zone_ids: route.zone_ids,
            route_edge_ids: route.edge_ids.clone(),
            route_points: points.iter().map(|point| Point { x: point.x, y: point.y }).collect(),
            started_minute: minute,
            due_minute: due,
            queue_id: None,
            support_request_id: None,
            support_staff_id: None,
        };
        Some(NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| {
            EntityStore::with_rows(&store, |rows| {
                plan_start(&scene, row, rows, agent_id, journey, &route.origin, points, &route.edge_ids, minute, route.minutes)
            })
        })?)
    } else {
        None
    };
    Ok(Some(PlannedTrip { agent_id: agent_id.clone(), task_id, task_kind, target_id: facility_id.clone(), minute, due, event, changes }))
}

/// The writes of `_schedule_travel` and `_schedule_room_approach`.
pub(crate) fn write_travel(world: &Bound<'_, PyAny>, writer: &EventWriter<'_>, trip: PlannedTrip) -> PyResult<()> {
    let py = world.py();
    let store = entities_of(world)?;
    let PlannedTrip { agent_id, task_id, task_kind, target_id, minute, due, event, changes } = trip;
    writer.allocate_sequence()?;
    let task_text = PyString::new(py, &task_id);
    let agent_text = PyString::new(py, &agent_id);
    let task = active_task_class(py)?.call1((&task_text, task_kind, &agent_text, py.None(), minute, due))?;
    attribute(world, intern!(py, "active_tasks"))?.set_item(&task_text, task)?;
    set_agent(world, &store, agent_text.as_any(), Activity::Moving, Some(PyString::new(py, &target_id).as_any()))?;
    writer.schedule(event)?;
    if let Some(changes) = changes {
        let mirror = mirror_held(world)?;
        let writes = NamespaceMirror::with_row_tree::<MovementRow, _>(&mirror, |row, tree| changes.apply(row, tree))?;
        write_tree(&mirror, writes)?;
    }
    Ok(())
}

/// A `ZoneRoute` as a row.
fn route_of_object(route: &Bound<'_, PyAny>) -> PyResult<Route> {
    let py = route.py();
    Ok(Route {
        origin: Arc::from(exact_str(&attribute(route, intern!(py, "origin_zone_id"))?)?),
        destination: Arc::from(exact_str(&attribute(route, intern!(py, "destination_zone_id"))?)?),
        zone_ids: str_tuple(&attribute(route, intern!(py, "zone_ids"))?)?,
        edge_ids: str_tuple(&attribute(route, intern!(py, "edge_ids"))?)?,
        minutes: exact_int(&attribute(route, intern!(py, "travel_minutes"))?)?,
    })
}

/// `_schedule_staff_support_travel`.
#[pyfunction]
fn schedule_support_travel<'py>(
    controller: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    route: &Bound<'py, PyAny>,
    request_id: &Bound<'py, PyAny>,
    staff_id: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
) -> PyResult<bool> {
    let world = attribute(controller, intern!(controller.py(), "world"))?;
    let writer = EventWriter::of(&world)?;
    let request: Arc<str> = Arc::from(exact_str(request_id)?);
    let planned = plan_travel(
        &world,
        &writer,
        Trip {
            agent_id: Arc::from(exact_str(agent_id)?),
            route: route_of_object(route)?,
            points: Vec::new(),
            purpose: "staff_support_travel",
            facility_id: None,
            minute: exact_int(minute)?,
            causation_id: Some(request.clone()),
            support_request_id: Some(request),
            support_staff_id: Some(Arc::from(exact_str(staff_id)?)),
        },
    )?;
    let Some(planned) = planned else {
        return Ok(false);
    };
    write_travel(&world, &writer, planned)?;
    Ok(true)
}

/// `_schedule_travel`, with no validation of the whole world.
#[pyfunction]
#[pyo3(signature = (
    controller, *, agent_id, route, route_points=None, purpose, facility_id, minute, causation_id=None,
    support_request_id=None, support_staff_id=None, validate_state=None
))]
#[allow(clippy::too_many_arguments)]
fn schedule_travel<'py>(
    controller: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    route: &Bound<'py, PyAny>,
    route_points: Option<&Bound<'py, PyAny>>,
    purpose: &Bound<'py, PyAny>,
    facility_id: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    causation_id: Option<&Bound<'py, PyAny>>,
    support_request_id: Option<&Bound<'py, PyAny>>,
    support_staff_id: Option<&Bound<'py, PyAny>>,
    validate_state: Option<&Bound<'py, PyAny>>,
) -> PyResult<bool> {
    let py = controller.py();
    if !validate_state.is_some_and(|validate| validate.downcast_exact::<PyBool>().is_ok_and(|validate| !validate.is_true())) {
        return unsupported("the world's whole validation is Python's");
    }
    let world = attribute(controller, intern!(py, "world"))?;
    let writer = EventWriter::of(&world)?;
    let planned = plan_travel(
        &world,
        &writer,
        Trip {
            agent_id: Arc::from(exact_str(agent_id)?),
            route: route_of_object(route)?,
            points: route_points_of(route_points)?,
            purpose: exact_str(purpose)?,
            facility_id: optional_str(Some(facility_id))?,
            minute: exact_int(minute)?,
            causation_id: optional_str(causation_id)?,
            support_request_id: optional_str(support_request_id)?,
            support_staff_id: optional_str(support_staff_id)?,
        },
    )?;
    let Some(planned) = planned else {
        return Ok(false);
    };
    write_travel(&world, &writer, planned)?;
    Ok(true)
}

/// `_schedule_return_to_initial_zone` of `experiments/phases/dispatch.py`.
pub(crate) fn schedule_return<'py>(controller: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: EventRef<'_, 'py>) -> PyResult<()> {
    let py = world.py();
    let Some(agent_id) = event.text("agent_id")? else {
        return Ok(());
    };
    let found = EntityStore::with_rows(&entities_of(world)?, |rows| {
        Ok(rows.agents.get(&*agent_id).map(|agent| (agent.zone_id.clone(), agent.profile.initial_zone_id.clone())))
    })?;
    let Some((zone_id, destination)) = found else {
        return Ok(());
    };
    if zone_id == destination {
        return Ok(());
    }
    let graph = rows_of(controller)?;
    let writer = EventWriter::of(world)?;
    let mut waits = Vec::new();
    let found = route_for(world, &graph, &zone_id, &destination, &agent_id, &mut waits)?;
    writer.reserve(waits.len());
    for wait in waits {
        write_capacity_wait(world, &writer, wait)?;
    }
    let Some(route) = found else {
        return Ok(());
    };
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let planned = plan_travel(
        world,
        &writer,
        Trip {
            agent_id,
            route,
            points: Vec::new(),
            purpose: "return",
            facility_id: None,
            minute,
            causation_id: Some(event.event_id()?),
            support_request_id: None,
            support_staff_id: None,
        },
    )?;
    if let Some(planned) = planned {
        write_travel(world, &writer, planned)?;
    }
    Ok(())
}

/// `_schedule_return_to_initial_zone`, called from Python.
#[pyfunction]
#[pyo3(name = "schedule_return")]
fn schedule_return_step<'py>(controller: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    schedule_return(controller, world, EventRef::Object(event))
}

// ---- The choice of a facility -------------------------------------------------------------------

/// `_facility_queue_has_capacity`.
fn queue_has_capacity(world: &Bound<'_, PyAny>, rows: &Entities, facility_id: &str, facility: &FacilityRow, resource: bool) -> PyResult<bool> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let Some(config) = exact_dict(&configs)?.get_item(intern!(py, "facility_queues"))? else {
        return Ok(true);
    };
    let config = exact_dict(&config)?;
    let Some(schema) = config.get_item(intern!(py, "schema_version"))? else {
        return unsupported("the queue config has no schema");
    };
    if exact_int(&schema)? != 1 {
        return unsupported("another queue config");
    }
    let Some(capacities) = config.get_item(intern!(py, "queue_capacity_by_facility"))? else {
        return Ok(true);
    };
    let Some(maximum) = exact_dict(&capacities)?.get_item(facility_id)? else {
        return Ok(true);
    };
    if maximum.is_none() {
        return Ok(true);
    }
    let maximum = exact_int(&maximum)?;
    if maximum < 0 {
        return unsupported("a negative queue capacity");
    }
    let (queued, active) = if resource {
        (&facility.resource_queue_agent_ids, &facility.resource_active_agent_ids)
    } else {
        (&facility.queue_agent_ids, &facility.active_agent_ids)
    };
    let transit = in_transit(rows, facility_id);
    Ok(queued.len() as i64 + active.len() as i64 + transit < maximum)
}

fn in_transit(rows: &Entities, facility_id: &str) -> i64 {
    rows.agents.iter().filter(|(_, other)| other.activity == Activity::Moving && other.target_id.as_deref() == Some(facility_id)).count() as i64
}

struct Candidate {
    id: Arc<str>,
    zone: Arc<str>,
    load: i64,
}

/// What `_select_resource_facility` found: `(facility, route, rejection)`.
pub(crate) enum Selected {
    NoFacility,
    NoRoute(Arc<str>),
    Found(Arc<str>, Route),
}

/// `_select_toilet` and `_select_resource_facility`, which differ in which facilities they take.
fn select(
    world: &Bound<'_, PyAny>,
    graph: &Bound<'_, RouteRows>,
    writer: &EventWriter<'_>,
    agent_id: &Bound<'_, PyAny>,
    resource_kind: Option<&str>,
    waits: &mut Vec<CapacityWait>,
) -> PyResult<Selected> {
    let py = world.py();
    let store = entities_of(world)?;
    let agent_name = exact_str(agent_id)?;
    let resource = resource_kind.is_some();
    let (agent_zone, listed) = EntityStore::with_rows(&store, |rows| {
        let Some(agent) = rows.agents.get(agent_name) else {
            return unsupported("no such agent");
        };
        let mut listed = Vec::new();
        for (id, facility) in rows.facilities.iter() {
            let takes = match resource_kind {
                Some(kind) => &*facility.kind == kind,
                None => {
                    if !facility.kind.is_ascii() {
                        return unsupported("a facility kind that casefold may change");
                    }
                    facility.kind.to_ascii_lowercase().contains("toilet")
                }
            };
            if takes && queue_has_capacity(world, rows, id, facility, resource)? {
                let load = if resource {
                    facility.resource_queue_agent_ids.len() + facility.resource_active_agent_ids.len()
                } else {
                    facility.queue_agent_ids.len() + facility.active_agent_ids.len()
                } as i64
                    + in_transit(rows, id);
                listed.push(Candidate { id: id.clone(), zone: facility.zone_id.clone(), load });
            }
        }
        Ok((agent.zone_id.clone(), listed))
    })?;
    let mut known = Vec::with_capacity(listed.len());
    for candidate in listed {
        if agent_knows_facility(world, agent_id, PyString::new(py, &candidate.id).as_any())? {
            known.push(candidate);
        }
    }
    if known.is_empty() {
        return Ok(Selected::NoFacility);
    }
    let mut best: Option<(i64, i64, Arc<str>, Route)> = None;
    for candidate in &known {
        let Some(route) = route_for(world, graph, &agent_zone, &candidate.zone, agent_name, waits)? else {
            continue;
        };
        let better = best.as_ref().is_none_or(|(load, minutes, id, _)| {
            (candidate.load, route.minutes, candidate.id.as_bytes()) < (*load, *minutes, id.as_bytes())
        });
        if better {
            best = Some((candidate.load, route.minutes, candidate.id.clone(), route));
        }
    }
    // The waits take their sequences before the trip planned after this.
    writer.reserve(waits.len());
    match best {
        Some((.., id, route)) => Ok(Selected::Found(id, route)),
        None => Ok(Selected::NoRoute(known.iter().map(|candidate| candidate.id.clone()).min_by(|first, second| first.as_bytes().cmp(second.as_bytes())).unwrap_or_else(|| Arc::from("")))),
    }
}

pub(crate) fn select_toilet(
    world: &Bound<'_, PyAny>,
    graph: &Bound<'_, RouteRows>,
    writer: &EventWriter<'_>,
    agent_id: &Bound<'_, PyAny>,
    waits: &mut Vec<CapacityWait>,
) -> PyResult<Selected> {
    select(world, graph, writer, agent_id, None, waits)
}

pub(crate) fn select_resource(
    world: &Bound<'_, PyAny>,
    graph: &Bound<'_, RouteRows>,
    writer: &EventWriter<'_>,
    agent_id: &Bound<'_, PyAny>,
    kind: &str,
    waits: &mut Vec<CapacityWait>,
) -> PyResult<Selected> {
    select(world, graph, writer, agent_id, Some(kind), waits)
}

/// `_facility_approach`, with a floor plan: its route and path, or None for no route. Unsupported
/// without one, whose route takes no time.
pub(crate) fn approach_route(world: &Bound<'_, PyAny>, agent_id: &str, facility_id: &str) -> PyResult<Option<(Route, Vec<Pt>)>> {
    let py = world.py();
    let store = entities_of(world)?;
    let geo = Geo::of(world)?;
    if geo.layout.is_none() {
        return unsupported("no floor plan: a request at once");
    }
    let table = attribute(world, intern!(py, "model_state"))?;
    if !exact_dict(&table)?.contains(KEY)? {
        return unsupported("a floor plan without the movement state");
    }
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let raw_model = exact_dict(&configs)?.get_item(intern!(py, "movement_model"))?;
    let mirror = mirror_held(world)?;
    let found = NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| {
        EntityStore::with_rows(&store, |rows| {
            let Some(facility) = rows.facilities.get(facility_id) else {
                return unsupported("no such facility");
            };
            let zone = facility.zone_id.clone();
            Ok(approach(&geo, row, rows, raw_model, agent_id, facility_id)?.map(|found| (found, zone)))
        })
    })?;
    Ok(found.map(|((minutes, path), zone)| {
        let route = Route { origin: zone.clone(), destination: zone.clone(), zone_ids: vec![zone.clone(), zone], edge_ids: Vec::new(), minutes };
        (route, path.to_vec())
    }))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(route, m)?)?;
    m.add_function(wrap_pyfunction!(schedule_travel, m)?)?;
    m.add_function(wrap_pyfunction!(schedule_support_travel, m)?)?;
    m.add_function(wrap_pyfunction!(schedule_return_step, m)?)?;
    Ok(())
}
