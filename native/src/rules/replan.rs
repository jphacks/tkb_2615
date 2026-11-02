//! `_replan_journeys` and `_replan_staff_support_trip` of `experiments/phases/update.py`: the people
//! whose route changed under them, in the order the walk returned them.
//!
//! Each person is two steps. Their old trip stops, and then a new route is planned from where that
//! left them, reading what the stop wrote: their zone, and that nobody counts them as on the way.
//! So one kernel cannot plan a person and then write them, as the others do. Instead a refusal says
//! how far it got: before anyone's stop, the Python step does the rest of the people; after a stop
//! and before a write of the second step, `_replan_stopped` does that person's second step.
//! A replan is rare, so this goes through the Python objects' own `__setattr__`, as
//! `rules/handlers.rs` does for what happens rarely. See docs/rust-port/lanes/l3-movement.md.

use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyString, PyTuple};

use crate::core::census;
use crate::core::row::{attribute, exact_int, exact_str, unsupported, Field};
use crate::core::value::V;
use crate::core::world_core::{raw_dict_get, NamespaceMirror};
use crate::rules::care_receipts::continuity_record;
use crate::rules::movement::{mirror_held, place_in_room, stop_journey};
use crate::rules::overlay;
use crate::rules::proposals::place_in_bed;
use crate::rules::service::{add_one, metrics_of, set_agent};
use crate::rules::travel::{approach_route, plan_travel, route as route_kernel, write_travel, Route, Trip};
use crate::state::entities::{entities_of, raw_setattr, Activity, EntityStore, RequestStatus};
use crate::state::events::{EventKind, EventRow, EventStatus, EventWriter};
use crate::state::movement::{Journey, MovementRow};
use crate::Unsupported;

const REPLAN: &str = "movement_replan_count";
const BLOCKED: &str = "movement_replan_blocked_count";
const STAFF_SUPPORT_SOURCE: &str = "ASM-STAFF-SUPPORT-LIFECYCLE-V1";

/// Say Unsupported no more: the people before this one have been written.
fn after_a_write(py: Python<'_>, result: PyResult<()>) -> PyResult<()> {
    match result {
        Err(error) if error.is_instance_of::<Unsupported>(py) => {
            Err(PyRuntimeError::new_err(format!("a kernel said Unsupported after a replan was written: {}", error.value(py))))
        }
        other => other,
    }
}

/// The controller's `_route`, read as a route. The kernel, or with edge capacities the Python step.
fn route_of<'py>(controller: &Bound<'py, PyAny>, origin: &str, destination: &str, agent_id: &str) -> PyResult<Option<Route>> {
    let py = controller.py();
    let (from, to, who) = (PyString::new(py, origin), PyString::new(py, destination), PyString::new(py, agent_id));
    let found = match route_kernel(controller, from.as_any(), to.as_any(), who.as_any()) {
        Err(error) if error.is_instance_of::<Unsupported>(py) => {
            census::caught(py, "replan route", &error);
            controller.call_method1(intern!(py, "_route"), (&from, &to, &who))?
        }
        other => other?,
    };
    if found.is_none() {
        return Ok(None);
    }
    Ok(Some(Route {
        origin: Arc::from(exact_str(&attribute(&found, intern!(py, "origin_zone_id"))?)?),
        destination: Arc::from(exact_str(&attribute(&found, intern!(py, "destination_zone_id"))?)?),
        zone_ids: crate::rules::movement::str_tuple(&attribute(&found, intern!(py, "zone_ids"))?)?,
        edge_ids: crate::rules::movement::str_tuple(&attribute(&found, intern!(py, "edge_ids"))?)?,
        minutes: exact_int(&attribute(&found, intern!(py, "travel_minutes"))?)?,
    }))
}

struct Replan<'a, 'py> {
    controller: &'a Bound<'py, PyAny>,
    world: &'a Bound<'py, PyAny>,
    mirror: &'a Bound<'py, NamespaceMirror>,
    store: &'a Bound<'py, EntityStore>,
    writer: &'a EventWriter<'py>,
    metrics: &'a Bound<'py, NamespaceMirror>,
    minute: i64,
}

impl<'py> Replan<'_, 'py> {
    /// What the old trip's stop leaves: the event suppressed, the task gone, the person idle where
    /// their position says, and no journey. Says Unsupported only before its first write.
    fn stop(&self, agent_id: &Arc<str>, journey: &Journey) -> PyResult<()> {
        let py = self.world.py();
        let old = self.writer.queue().borrow_mut().row(py, &journey.event_id)?;
        if self.writer.queue().borrow().has_event(&journey.event_id) && old.is_none() {
            return unsupported("the trip's event has no row");
        }
        let zone = NamespaceMirror::with_row::<MovementRow, _>(self.mirror, |row| {
            Ok(row.positions.get(&**agent_id).map(|position| position.zone_id.clone()))
        })?;
        let agents = attribute(self.world, intern!(py, "agents"))?;
        let agent_text = PyString::new(py, agent_id);
        let Some(agent) = raw_dict_get(&agents, agent_text.as_any())? else {
            return unsupported("a row has no object");
        };

        // Write.
        if let Some(old) = old {
            self.writer.settle(&old, self.minute, EventStatus::Suppressed, Some("the route changed before travel completion"))?;
        }
        attribute(self.world, intern!(py, "active_tasks"))?.call_method1(intern!(py, "pop"), (&*journey.task_id, py.None()))?;
        if let Some(zone) = zone {
            EntityStore::with_rows(self.store, |rows| {
                if let Some(row) = rows.agents.get_mut(&**agent_id) {
                    row.zone_id = zone.clone();
                }
                Ok(())
            })?;
            raw_setattr(&agent, intern!(py, "zone_id"), PyString::new(py, &zone).as_ptr())?;
        }
        set_agent(self.world, self.store, agent_text.as_any(), Activity::Idle, None)?;
        stop_journey(self.mirror, agent_id)
    }

    fn zone_of(&self, agent_id: &str) -> PyResult<Arc<str>> {
        EntityStore::with_rows(self.store, |rows| match rows.agents.get(agent_id) {
            Some(agent) => Ok(agent.zone_id.clone()),
            None => unsupported("no such agent"),
        })
    }

    fn travel(&self, agent_id: &Arc<str>, route: Route, points: Vec<crate::rules::movement_geometry::Pt>, journey: &Journey) -> PyResult<Option<crate::rules::travel::PlannedTrip>> {
        plan_travel(
            self.world,
            self.writer,
            Trip {
                agent_id: agent_id.clone(),
                route,
                points,
                purpose: &journey.purpose,
                facility_id: journey.facility_id.clone(),
                minute: self.minute,
                causation_id: Some(journey.event_id.clone()),
                support_request_id: None,
                support_staff_id: None,
            },
        )
    }

    /// `_replan_stopped`. Says Unsupported only before its first write.
    fn second_step(&self, agent_id: &Arc<str>, journey: &Journey) -> PyResult<()> {
        match &*journey.purpose {
            "staff_support_travel" => {
                if !self.staff_support(agent_id, journey)? {
                    add_one(self.metrics, BLOCKED)?;
                }
                add_one(self.metrics, REPLAN)
            }
            "sleep_bed" => self.sleep_bed(agent_id, journey),
            _ => self.walk(agent_id, journey),
        }
    }

    fn sleep_bed(&self, agent_id: &Arc<str>, journey: &Journey) -> PyResult<()> {
        let py = self.world.py();
        let zone = self.zone_of(agent_id)?;
        let agent_text = PyString::new(py, agent_id);
        let bed = overlay::plan_bed(self.world, agent_id)?;
        let trip = match &bed {
            Some(bed) if bed.travel_minutes > 0 => {
                let route = Route {
                    origin: zone.clone(),
                    destination: zone.clone(),
                    zone_ids: vec![zone.clone(), zone.clone()],
                    edge_ids: Vec::new(),
                    minutes: bed.travel_minutes,
                };
                self.travel(agent_id, route, bed.path.to_vec(), journey)?
            }
            _ => None,
        };
        if let (Some(bed), Some(trip)) = (&bed, trip) {
            overlay::write_bed(self.world, agent_id, bed)?;
            write_travel(self.world, self.writer, trip)?;
            return add_one(self.metrics, REPLAN);
        }
        match &bed {
            Some(bed) if bed.travel_minutes <= 0 => {
                overlay::write_bed(self.world, agent_id, bed)?;
                place_in_bed(self.world, agent_text.as_any(), bed, &zone)?;
            }
            _ => {
                add_one(self.metrics, BLOCKED)?;
                overlay::release_bed(self.world, agent_text.as_any())?;
            }
        }
        set_agent(self.world, self.store, agent_text.as_any(), Activity::Sleeping, None)?;
        add_one(self.metrics, REPLAN)
    }

    fn walk(&self, agent_id: &Arc<str>, journey: &Journey) -> PyResult<()> {
        let zone = self.zone_of(agent_id)?;
        let facility = EntityStore::with_rows(self.store, |rows| {
            Ok(journey.facility_id.as_ref().and_then(|id| rows.facilities.get(id).map(|facility| (id.clone(), facility.zone_id.clone()))))
        })?;
        let mut target = journey.destination_zone_id.clone();
        if let Some((facility_id, facility_zone)) = facility {
            target = facility_zone;
            if zone == target {
                return self.within_the_room(agent_id, journey, &facility_id, &zone);
            }
        }
        let route = route_of(self.controller, &zone, &target, agent_id)?.filter(|route| !route.edge_ids.is_empty());
        match route {
            Some(route) => {
                // `_schedule_travel`, whose answer no one reads here.
                if let Some(trip) = self.travel(agent_id, route, Vec::new(), journey)? {
                    write_travel(self.world, self.writer, trip)?;
                }
            }
            None => add_one(self.metrics, BLOCKED)?,
        }
        add_one(self.metrics, REPLAN)
    }

    fn within_the_room(&self, agent_id: &Arc<str>, journey: &Journey, facility_id: &str, zone: &Arc<str>) -> PyResult<()> {
        let found = approach_route(self.world, agent_id, facility_id)?.filter(|(_, points)| !points.is_empty());
        match found {
            Some((route, points)) if route.minutes > 0 && points.len() > 1 => {
                if let Some(trip) = self.travel(agent_id, route, points, journey)? {
                    write_travel(self.world, self.writer, trip)?;
                }
            }
            Some((_, points)) => place_in_room(self.mirror, agent_id, zone, points[points.len() - 1])?,
            None => add_one(self.metrics, BLOCKED)?,
        }
        add_one(self.metrics, REPLAN)
    }

    /// `_replan_staff_support_trip`: whether the person still has their support.
    fn staff_support(&self, agent_id: &Arc<str>, journey: &Journey) -> PyResult<bool> {
        let py = self.world.py();
        let minute = self.minute;
        let Some(request_id) = journey.support_request_id.clone() else {
            return unsupported("the trip lost its support");
        };
        struct Found {
            staff_id: Arc<str>,
            staff_zone: Arc<str>,
            task_id: Arc<str>,
            start: i64,
            due: i64,
            timeout: i64,
        }
        let found = EntityStore::with_rows(self.store, |rows| {
            let Some(request) = rows.support_requests.get(&*request_id) else {
                return unsupported("the trip lost its support");
            };
            let (Some(staff_id), Some(task_id)) = (&request.assigned_staff_id, &request.task_id) else {
                return unsupported("the trip lost its support");
            };
            let (Some(staff), Some(task)) = (rows.staff.get(staff_id), rows.active_tasks.get(task_id)) else {
                return unsupported("the trip lost its support");
            };
            if request.status != RequestStatus::Assigned || &*task.kind != "staff_support" {
                return unsupported("the trip lost its support");
            }
            Ok(Found {
                staff_id: staff_id.clone(),
                staff_zone: staff.zone_id.clone(),
                task_id: task_id.clone(),
                start: task.start_minute,
                due: task.due_minute,
                timeout: request.timeout_minute,
            })
        })?;
        // The completion of this task, which must be one and due with it.
        let ids = self.writer.queue().borrow().pending_ids(None);
        let mut completions = Vec::new();
        for id in ids {
            let Some(row) = self.writer.queue().borrow_mut().row(py, &id)? else {
                return unsupported("a pending event has no row");
            };
            let V::Map(payload) = &row.payload else {
                continue;
            };
            let names_it = matches!(payload.get("task_id"), Some(V::Str(task)) if **task == *found.task_id);
            if &*row.event_type == "staff_support_complete" && names_it {
                completions.push(row);
            }
        }
        if completions.len() != 1 || completions[0].due_minute != found.due {
            return unsupported("the support's completion is inconsistent");
        }
        let previous = completions.remove(0);
        let service_minutes = found.due - found.start;
        let zone = self.zone_of(agent_id)?;
        let route = route_of(self.controller, &zone, &found.staff_zone, agent_id)?;
        let route_minutes = route.as_ref().map_or(0, |route| route.minutes);
        let Some(due) = minute.checked_add(route_minutes).and_then(|at| at.checked_add(service_minutes)) else {
            return unsupported("a due minute out of range");
        };
        let mut can_schedule = route.is_some() && due < self.writer.duration();
        let mut trip = None;
        if let Some(route) = route.filter(|route| can_schedule && route.minutes > 0) {
            trip = plan_travel(
                self.world,
                self.writer,
                Trip {
                    agent_id: agent_id.clone(),
                    route,
                    points: Vec::new(),
                    purpose: "staff_support_travel",
                    facility_id: None,
                    minute,
                    causation_id: Some(journey.event_id.clone()),
                    support_request_id: Some(request_id.clone()),
                    support_staff_id: Some(found.staff_id.clone()),
                },
            )?;
            can_schedule = trip.is_some();
        }
        let table = |map: &str, id: &str| -> PyResult<Bound<'py, PyAny>> {
            raw_dict_get(&self.world.getattr(map)?, PyString::new(py, id).as_any())?.ok_or_else(|| Unsupported::new_err("a row has no object"))
        };
        let (request_object, staff_object, task_object, agent_object) =
            (table("support_requests", &request_id)?, table("staff", &found.staff_id)?, table("active_tasks", &found.task_id)?, table("agents", agent_id)?);
        let agent_text = PyString::new(py, agent_id);
        if can_schedule {
            let sequence = self.writer.next_sequence()? + i64::from(trip.is_some());
            let policy = attribute(&attribute(self.controller, intern!(py, "staff_service"))?, intern!(py, "policy_version"))?;
            let event = reschedule_event(&request_id, agent_id, &found.staff_id, &found.task_id, service_minutes, due, sequence, journey, exact_str(&policy)?)?;
            // Write.
            match trip {
                Some(trip) => write_travel(self.world, self.writer, trip)?,
                None => set_agent(self.world, self.store, agent_text.as_any(), Activity::SeekingHelp, None)?,
            }
            self.writer.settle(&previous, minute, EventStatus::Suppressed, Some("support completion was rescheduled after a route change"))?;
            self.writer.allocate_sequence()?;
            task_object.setattr(intern!(py, "start_minute"), minute + route_minutes)?;
            task_object.setattr(intern!(py, "due_minute"), due)?;
            staff_object.setattr(intern!(py, "busy_until"), due)?;
            self.writer.schedule(event)?;
            return Ok(true);
        }
        let service = attribute(self.controller, intern!(py, "staff_service"))?;
        let cooldown = exact_int(&attribute(&service, intern!(py, "cooldown_minutes"))?)?;
        let Some(until) = minute.checked_add(cooldown) else {
            return unsupported("a minute out of range");
        };
        // Write.
        self.writer.settle(&previous, minute, EventStatus::Suppressed, Some("support completion was cancelled because the staff route is unavailable"))?;
        attribute(self.world, intern!(py, "active_tasks"))?.del_item(&*found.task_id)?;
        attribute(&staff_object, intern!(py, "work_queue"))?.call_method0(intern!(py, "clear"))?;
        staff_object.setattr(intern!(py, "active_task_id"), py.None())?;
        staff_object.setattr(intern!(py, "busy_until"), 0)?;
        request_object.setattr(intern!(py, "assigned_staff_id"), py.None())?;
        request_object.setattr(intern!(py, "task_id"), py.None())?;
        agent_object.setattr(intern!(py, "target_id"), py.None())?;
        let unresolved = found.timeout <= minute;
        if unresolved {
            request_object.setattr(intern!(py, "status"), RequestStatus::Unresolved.member(py)?)?;
            request_object.setattr(intern!(py, "resolved_minute"), minute)?;
            request_object.setattr(intern!(py, "cooldown_until"), until)?;
            agent_object.setattr(intern!(py, "activity"), Activity::Idle.member(py)?)?;
            agent_object.setattr(intern!(py, "support_request_id"), py.None())?;
            agent_object.setattr(intern!(py, "support_cooldown_until"), until)?;
            add_one(self.metrics, "support_unresolved_count")?;
        } else {
            request_object.setattr(intern!(py, "status"), RequestStatus::Pending.member(py)?)?;
            agent_object.setattr(intern!(py, "activity"), Activity::SeekingHelp.member(py)?)?;
            agent_object.setattr(intern!(py, "support_request_id"), &*request_id)?;
        }
        let continuity = continuity_record(
            self.world,
            &request_id,
            agent_id,
            &found.task_id,
            &found.staff_id,
            if unresolved { "unresolved" } else { "requeued" },
            &journey.event_id,
            "route_unreachable",
        )?;
        attribute(self.world, intern!(py, "event_log"))?.call_method1(intern!(py, "append"), (&continuity,))?;
        py.import("sheltersim.domain.care_receipts")?
            .getattr("ingest_applied_shared_operation_records")?
            .call1((self.world, PyTuple::new(py, [&continuity])?))?;
        py.import("sheltersim.domain.movement")?.getattr("finish_movement")?.call1((self.world, &agent_text))?;
        Ok(false)
    }
}

/// The new `staff_support_complete` of a support whose route changed.
#[allow(clippy::too_many_arguments)]
fn reschedule_event(
    request_id: &Arc<str>,
    agent_id: &Arc<str>,
    staff_id: &Arc<str>,
    task_id: &Arc<str>,
    service_minutes: i64,
    due: i64,
    sequence: i64,
    journey: &Journey,
    policy: &str,
) -> PyResult<EventRow> {
    let mut payload: IndexMap<Arc<str>, V> = IndexMap::with_capacity(5);
    payload.insert(Arc::from("request_id"), V::Str(request_id.clone()));
    payload.insert(Arc::from("agent_id"), V::Str(agent_id.clone()));
    payload.insert(Arc::from("staff_id"), V::Str(staff_id.clone()));
    payload.insert(Arc::from("task_id"), V::Str(task_id.clone()));
    payload.insert(Arc::from("support_duration_minutes"), V::Int(service_minutes));
    let mut provenance: IndexMap<Arc<str>, V> = IndexMap::with_capacity(4);
    provenance.insert(Arc::from("source_type"), V::Str(Arc::from("assumption")));
    provenance.insert(Arc::from("source_id"), V::Str(Arc::from(STAFF_SUPPORT_SOURCE)));
    provenance.insert(Arc::from("policy_version"), V::Str(Arc::from(policy)));
    provenance.insert(Arc::from("rescheduled_after_route_change"), V::Bool(true));
    EventRow::new(
        Arc::from(format!("staff-support-complete-{sequence}")),
        EventKind::Completion,
        due,
        0,
        sequence,
        Arc::from("staff_support_complete"),
        payload,
        Some(task_id.clone()),
        Some(journey.event_id.clone()),
        provenance,
    )
    .ok_or_else(|| Unsupported::new_err("the event's values are not what an event takes"))
}

/// `_replan_journeys`.
#[pyfunction]
pub(crate) fn replan_journeys<'py>(
    controller: &Bound<'py, PyAny>,
    state: &Bound<'py, PyAny>,
    agent_ids: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = controller.py();
    let world = attribute(controller, intern!(py, "world"))?;
    let minute_value = exact_int(minute)?;
    let mirror = mirror_held(&world)?;
    let store = entities_of(&world)?;
    let writer = EventWriter::of(&world)?;
    let metrics = metrics_of(&world)?;
    let ids = agent_ids.try_iter()?.collect::<PyResult<Vec<_>>>()?;
    let ids = ids.iter().map(|id| Ok(Arc::<str>::from(exact_str(id)?))).collect::<PyResult<Vec<Arc<str>>>>()?;
    let replan = Replan { controller, world: &world, mirror: &mirror, store: &store, writer: &writer, metrics: &metrics, minute: minute_value };
    for (index, agent_id) in ids.iter().enumerate() {
        let journey = NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| Ok(row.journeys.get(&**agent_id).cloned()))?;
        let known = EntityStore::with_rows(&store, |rows| Ok(rows.agents.get(&**agent_id).is_some()))?;
        let (Some(journey), true) = (journey, known) else {
            continue;
        };
        if let Err(error) = replan.stop(agent_id, &journey) {
            if !error.is_instance_of::<Unsupported>(py) {
                return Err(error);
            }
            // Nobody from here has been touched: the rest are the Python step's.
            census::caught(py, "replan stop", &error);
            if index == 0 {
                return Err(error);
            }
            let rest = PyTuple::new(py, ids[index..].iter().map(|id| &**id))?;
            controller.call_method1(intern!(py, "_replan_in_python"), (state, rest, minute))?;
            return Ok(());
        }
        match replan.second_step(agent_id, &journey) {
            Err(error) if error.is_instance_of::<Unsupported>(py) => {
                census::caught(py, "replan second step", &error);
                let record = Field::to_py(&journey, py)?;
                after_a_write(py, controller.call_method1(intern!(py, "_replan_stopped"), (state, &**agent_id, record, minute)).map(|_| ()))?;
            }
            other => other?,
        }
    }
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(replan_journeys, m)?)?;
    Ok(())
}
