//! The dispatch phase (`experiments/phases/dispatch.py`): the loop over what is due, then what follows
//! from the applied events (docs/rust-port/lanes/m3-dispatch.md).
//!
//! The table says which event types have a Rust function: `handlers.py` installs them with the class
//! and `apply` they stand for. An event of another type, or whose handler is not that class with that
//! `apply` (a test's fake or patch), goes to `controller._dispatch_one_event`, which is the Python
//! dispatcher for one event: its `ScheduledEvent` is made then and not before. So is an event that a
//! Rust function refuses: Unsupported means nothing changed, and the Python rule runs and says why.
//!
//! No borrow is held across a call into Python. The writer is asked for again after each one, as it
//! may find another queue. This takes Unsupported only before its first change.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyList, PyString, PyType};

use crate::core::row::{attribute, exact_int};
use crate::core::world_core::{mirror_of, raw_dict_get, NamespaceMirror};
use crate::rules::action_lifecycle::{complete_for_event, ends_any_action};
use crate::rules::agent_dynamics::learn_facility_of;
use crate::rules::body_state::record_social_event;
use crate::rules::social_interaction::apply_event;
use crate::rules::social_network::observe_event;
use crate::rules::travel_completion;
use crate::state::entities::{entities_of, Activity, EntityStore};
use crate::state::events::{DueEvent, DueState, EventRef, EventRow, EventStatus, EventWriter};
use crate::state::movement::MovementRow;
use crate::Unsupported;

const SOCIAL_INTERACTION: &str = "social_interaction";
const AGENT_TRAVEL_COMPLETE: &str = "agent_travel_complete";
const MOVEMENT: &str = "movement_v1";
const SOCIAL_NETWORK: &str = "social_network_v1";

/// What Python gives once: the classes of the table's handlers and their `apply` as defined.
struct Table {
    social: (Py<PyType>, Py<PyAny>),
    travel: (Py<PyType>, Py<PyAny>),
    dispatcher: Py<PyType>,
    suppressed: Py<PyType>,
    failed: Py<PyType>,
    statuses: [Py<PyAny>; 3],
}

static TABLE: GILOnceCell<Table> = GILOnceCell::new();
static PHASE_MODULE: GILOnceCell<Py<PyModule>> = GILOnceCell::new();

/// Each call goes through the Python entry of its rule, for benchmarks/rule_shadow, which replaces
/// those. Then the same kernels run, and a shadow sees each call.
static PYTHON_ENTRIES: AtomicBool = AtomicBool::new(false);

// ---- What ran, for benchmarks/rule_shadow and its report ------------------------------------------

macro_rules! counters {
    ($($name:ident),* $(,)?) => {
        #[derive(Default)]
        struct Counters { $($name: AtomicU64),* }
        static COUNTS: Counters = Counters { $($name: AtomicU64::new(0)),* };
        fn counts_of<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
            let out = PyDict::new(py);
            $(out.set_item(stringify!($name), COUNTS.$name.load(Ordering::Relaxed))?;)*
            Ok(out)
        }
    };
}

counters!(
    phases_ran,
    phases_refused,
    events_rust,
    events_python,
    events_skipped,
    events_replaced,
    social_ran,
    social_suppressed,
    social_refused,
    travel_ran,
    travel_failed,
    travel_refused,
    record_social_ran,
    record_social_refused,
    observe_ran,
    observe_refused,
    completion_ran,
    completion_refused,
    learn_ran,
    learn_refused,
    sync_ran,
    sync_refused,
    settled_rows,
    settled_python,
);

static REFUSALS: Mutex<BTreeMap<String, u64>> = Mutex::new(BTreeMap::new());

fn count(counter: &AtomicU64) {
    counter.fetch_add(1, Ordering::Relaxed);
}

fn refused(py: Python<'_>, label: &str, counter: &AtomicU64, error: &PyErr) {
    count(counter);
    let reason = format!("{label}: {}", error.value(py));
    if let Ok(mut refusals) = REFUSALS.lock() {
        *refusals.entry(reason).or_insert(0) += 1;
    }
}

// ---- An event's outcome ---------------------------------------------------------------------------

/// One record of the phase, as the steps after the loop read it.
struct Outcome<'py> {
    event_type: Arc<str>,
    applied: bool,
    row: Option<Arc<EventRow>>,
    event: Option<Bound<'py, PyAny>>,
}

impl<'py> Outcome<'py> {
    fn event(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        if let Some(event) = &self.event {
            return Ok(event.clone());
        }
        let event = self.row.as_ref().expect("an outcome has a row or an object").to_py(py)?;
        self.event = Some(event.clone());
        Ok(event)
    }

    fn reference(&self) -> EventRef<'_, 'py> {
        match (&self.row, &self.event) {
            (Some(row), _) => EventRef::Row(row),
            (None, Some(event)) => EventRef::Object(event),
            (None, None) => unreachable!("an outcome has a row or an object"),
        }
    }
}

struct Phase<'a, 'py> {
    controller: &'a Bound<'py, PyAny>,
    world: &'a Bound<'py, PyAny>,
    minute: &'a Bound<'py, PyAny>,
    minute_value: i64,
    dispatcher: Bound<'py, PyAny>,
    table: &'static Table,
    module: Bound<'py, PyModule>,
    direct: bool,
}

/// What a Rust function made of an event; None when it left it for Python, with nothing changed.
type Handled = Option<(EventStatus, Option<String>)>;

impl<'py> Phase<'_, 'py> {
    fn py(&self) -> Python<'py> {
        self.world.py()
    }

    /// `except EventSuppressed` and `except EventApplicationError` of the Python dispatcher.
    fn expected_outcome(&self, error: PyErr) -> PyResult<(EventStatus, Option<String>)> {
        let py = self.py();
        let status = if error.is_instance(py, self.table.suppressed.bind(py)) {
            EventStatus::Suppressed
        } else if error.is_instance(py, self.table.failed.bind(py)) {
            EventStatus::Failed
        } else {
            return Err(error);
        };
        Ok((status, Some(error.value(py).str()?.to_str()?.to_owned())))
    }

    /// The handler this event's table entry stands for, or None for any other.
    fn table_handler(&self, name: &str, entry: &(Py<PyType>, Py<PyAny>)) -> PyResult<Option<Bound<'py, PyAny>>> {
        let py = self.py();
        let handlers = attribute(&self.dispatcher, intern!(py, "handlers"))?;
        let Some(handler) = raw_dict_get(&handlers, PyString::new(py, name).as_any())? else {
            return Ok(None);
        };
        let class = entry.0.bind(py);
        if !handler.get_type().is(class) || !class.getattr(intern!(py, "apply"))?.is(entry.1.bind(py)) {
            return Ok(None);
        }
        Ok(Some(handler))
    }

    fn social(&self, handler: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Handled> {
        let py = self.py();
        let world = self.world;
        let done = if self.direct {
            match apply_event(world, event) {
                Ok(text) => {
                    count(&COUNTS.social_ran);
                    text.map(|text| {
                        count(&COUNTS.social_suppressed);
                        (EventStatus::Suppressed, Some(text.to_owned()))
                    })
                }
                Err(error) if error.is_instance_of::<Unsupported>(py) => {
                    refused(py, "social_interaction", &COUNTS.social_refused, &error);
                    return Ok(None);
                }
                Err(error) => return Err(error),
            }
        } else {
            match handler.call_method1(intern!(py, "apply"), (world, event)) {
                Ok(_) => None,
                Err(error) => Some(self.expected_outcome(error)?),
            }
        };
        if let Some(done) = done {
            return Ok(Some(done));
        }
        self.social_records(event)?;
        Ok(Some((EventStatus::Applied, None)))
    }

    /// `record_social_event` and `observe_event` of an applied event; never left for later.
    fn social_records(&self, event: &Bound<'py, PyAny>) -> PyResult<()> {
        let py = self.py();
        let world = self.world;
        let minute = self.minute;
        let python_rule = |module: &Bound<'py, PyString>, name: &Bound<'py, PyString>| -> PyResult<()> {
            py.import(module)?.getattr(name)?.call1((world, event, minute))?;
            Ok(())
        };
        let body = intern!(py, "sheltersim.domain.body_state");
        let network = intern!(py, "sheltersim.domain.social_network");
        let record = intern!(py, "record_social_event");
        let observe = intern!(py, "observe_event");
        if self.direct {
            match record_social_event(world, event, minute) {
                Ok(()) => count(&COUNTS.record_social_ran),
                Err(error) if error.is_instance_of::<Unsupported>(py) => {
                    refused(py, "record_social_event", &COUNTS.record_social_refused, &error);
                    python_rule(body, record)?;
                }
                Err(error) => return Err(error),
            }
        } else {
            python_rule(body, record)?;
        }
        if !attribute(world, intern!(py, "model_state"))?.contains(intern!(py, SOCIAL_NETWORK))? {
            return Ok(());
        }
        if self.direct {
            match observe_event(world, event, minute) {
                Ok(()) => count(&COUNTS.observe_ran),
                Err(error) if error.is_instance_of::<Unsupported>(py) => {
                    refused(py, "observe_event", &COUNTS.observe_refused, &error);
                    python_rule(network, observe)?;
                }
                Err(error) => return Err(error),
            }
        } else {
            python_rule(network, observe)?;
        }
        Ok(())
    }

    fn travel(
        &self,
        handler: &Bound<'py, PyAny>,
        event: EventRef<'_, 'py>,
        object: &dyn Fn() -> PyResult<Bound<'py, PyAny>>,
    ) -> PyResult<Handled> {
        let py = self.py();
        if !self.direct {
            return match handler.call_method1(intern!(py, "apply"), (self.world, object()?)) {
                Ok(_) => Ok(Some((EventStatus::Applied, None))),
                Err(error) => self.expected_outcome(error).map(Some),
            };
        }
        match travel_completion::apply_event(handler, self.world, event, object) {
            Ok(()) => {
                count(&COUNTS.travel_ran);
                Ok(Some((EventStatus::Applied, None)))
            }
            Err(error) if error.is_instance_of::<Unsupported>(py) => {
                refused(py, "agent_travel_complete", &COUNTS.travel_refused, &error);
                Ok(None)
            }
            Err(error) => {
                // From what the walk leads to, past the rule's own checks.
                let done = self.expected_outcome(error)?;
                count(&COUNTS.travel_failed);
                Ok(Some(done))
            }
        }
    }

    /// The Python dispatcher for one event; its outcomes.
    fn dispatch_in_python(&self, event: &Bound<'py, PyAny>, outcomes: &mut Vec<Outcome<'py>>) -> PyResult<()> {
        let py = self.py();
        count(&COUNTS.events_python);
        let records = self
            .controller
            .call_method1(intern!(py, "_dispatch_one_event"), (self.world, event, self.minute))?;
        for record in records.downcast::<PyList>()?.iter() {
            let recorded = attribute(&record, intern!(py, "event"))?;
            let event_type = attribute(&recorded, intern!(py, "event_type"))?;
            outcomes.push(Outcome {
                event_type: Arc::from(event_type.downcast::<PyString>()?.to_str()?),
                applied: attribute(&record, intern!(py, "status"))?.is(self.table.statuses[0].bind(py)),
                row: None,
                event: Some(recorded),
            });
        }
        Ok(())
    }

    /// `EventDispatcher.settle` of what a Rust function did.
    fn settle(
        &self,
        due: &DueEvent,
        object: &dyn Fn() -> PyResult<Bound<'py, PyAny>>,
        status: EventStatus,
        detail: Option<&str>,
    ) -> PyResult<()> {
        let py = self.py();
        let settled = match EventWriter::of(self.world) {
            Ok(writer) => match (&due.object, &due.row) {
                (Some(event), _) => {
                    writer.settle_object(&due.event_id, event.bind(py), self.minute_value, status, detail)?
                }
                (None, Some(row)) => writer.settle(row, self.minute_value, status, detail)?,
                (None, None) => false,
            },
            Err(error) if error.is_instance_of::<Unsupported>(py) => false,
            Err(error) => return Err(error),
        };
        if settled {
            count(&COUNTS.settled_rows);
            return Ok(());
        }
        count(&COUNTS.settled_python);
        let index = match status {
            EventStatus::Applied => 0,
            EventStatus::Failed => 1,
            EventStatus::Suppressed => 2,
        };
        self.dispatcher.call_method1(
            intern!(py, "settle"),
            (self.world, object()?, self.minute, self.table.statuses[index].bind(py), detail),
        )?;
        Ok(())
    }

    /// One event of the snapshot.
    fn one_event(&self, due: DueEvent, outcomes: &mut Vec<Outcome<'py>>) -> PyResult<()> {
        let py = self.py();
        let writer = match EventWriter::of(self.world) {
            Ok(writer) => Some(writer),
            Err(error) if error.is_instance_of::<Unsupported>(py) => None,
            Err(error) => return Err(error),
        };
        let state = match &writer {
            Some(writer) => writer.queue().borrow().state_of_due(&due),
            None => DueState::Changed,
        };
        let snapshot_object = |due: &DueEvent| -> PyResult<Bound<'py, PyAny>> {
            match &due.object {
                Some(object) => Ok(object.bind(py).clone()),
                None => due.row.as_ref().expect("a slot has a row or an object").to_py(py),
            }
        };
        match state {
            DueState::Gone => {
                count(&COUNTS.events_skipped);
                return Ok(());
            }
            DueState::Changed => {
                count(&COUNTS.events_replaced);
                return self.dispatch_in_python(&snapshot_object(&due)?, outcomes);
            }
            DueState::Same => {}
        }
        let writer = writer.expect("the state came from it");
        // The object the queue holds, made once if Python had none: Python then sees that one.
        let object = || -> PyResult<Bound<'py, PyAny>> {
            match &due.object {
                Some(object) => Ok(object.bind(py).clone()),
                None => match EventWriter::of(self.world) {
                    Ok(writer) if writer.queue().borrow().state_of_due(&due) == DueState::Same => {
                        Ok(writer.queue().borrow_mut().object_of_due(py, &due)?.into_bound(py))
                    }
                    _ => snapshot_object(&due),
                },
            }
        };
        let bound = due.object.as_ref().map(|object| object.bind(py));
        let reference = match (&due.row, bound) {
            (Some(row), _) => EventRef::Row(row),
            (None, Some(object)) => EventRef::Object(object),
            (None, None) => unreachable!("a slot has a row or an object"),
        };
        let event_type = match reference.event_type() {
            Ok(event_type) => event_type,
            Err(error) if error.is_instance_of::<Unsupported>(py) => {
                return self.dispatch_in_python(&object()?, outcomes);
            }
            Err(error) => return Err(error),
        };
        let table = self.table;
        let handler = match &*event_type {
            SOCIAL_INTERACTION => self.table_handler(SOCIAL_INTERACTION, &table.social)?,
            AGENT_TRAVEL_COMPLETE => self.table_handler(AGENT_TRAVEL_COMPLETE, &table.travel)?,
            _ => None,
        };
        drop(writer);
        // A marker of an EffectProgram may hold a guard, which is `effect_event_guard_or_suppress`'s.
        let handler = match handler {
            Some(handler) if !reference.has_provenance("effect_program_v1")? => handler,
            _ => return self.dispatch_in_python(&object()?, outcomes),
        };
        let done = if &*event_type == SOCIAL_INTERACTION {
            self.social(&handler, &object()?)?
        } else {
            self.travel(&handler, reference, &object)?
        };
        let Some((status, detail)) = done else {
            return self.dispatch_in_python(&object()?, outcomes);
        };
        count(&COUNTS.events_rust);
        // Before the slot goes, so that a record Python compares has the queue's object.
        let event = due.object.as_ref().map(|object| object.bind(py).clone());
        self.settle(&due, &object, status, detail.as_deref())?;
        outcomes.push(Outcome { event_type, applied: status == EventStatus::Applied, row: due.row.clone(), event });
        Ok(())
    }

    /// `_record_applied_outcomes`, an outcome at a time as the Python step goes.
    fn record_applied(&self, outcome: &mut Outcome<'py>) -> PyResult<()> {
        let py = self.py();
        let world = self.world;
        let event_type = outcome.event_type.clone();
        let active_keyword = |active: &Bound<'py, PyAny>| -> PyResult<Bound<'py, PyDict>> {
            let kwargs = PyDict::new(py);
            kwargs.set_item(intern!(py, "active_actions"), active)?;
            Ok(kwargs)
        };
        if ends_any_action(&event_type) {
            let active = attribute(self.controller, intern!(py, "active_actions"))?;
            let done = if self.direct {
                let writer = EventWriter::of(world).and_then(|writer| {
                    complete_for_event(world, &writer, &active, outcome.reference(), self.minute_value)
                });
                match writer {
                    Ok(()) => {
                        count(&COUNTS.completion_ran);
                        true
                    }
                    Err(error) if error.is_instance_of::<Unsupported>(py) => {
                        refused(py, "record_applied_action_completion", &COUNTS.completion_refused, &error);
                        false
                    }
                    Err(error) => return Err(error),
                }
            } else {
                false
            };
            if !done {
                self.module.getattr(intern!(py, "record_applied_action_completion"))?.call(
                    (world, outcome.event(py)?, self.minute),
                    Some(&active_keyword(&active)?),
                )?;
            }
        }
        if &*event_type == "effect_program_population" {
            let active = attribute(self.controller, intern!(py, "active_actions"))?;
            self.module.getattr(intern!(py, "record_applied_population_exit"))?.call(
                (world, outcome.event(py)?, self.minute),
                Some(&active_keyword(&active)?),
            )?;
        }
        if &*event_type == AGENT_TRAVEL_COMPLETE {
            self.learn(outcome)?;
            // A str subclass is compared by `==`, as in the Python step.
            let staff_travel = match outcome.reference().text("purpose") {
                Ok(purpose) => purpose.as_deref() == Some("staff_support_travel"),
                Err(error) if error.is_instance_of::<Unsupported>(py) => {
                    let event = outcome.event(py)?;
                    let purpose = attribute(&event, intern!(py, "payload"))?.call_method1(intern!(py, "get"), ("purpose",))?;
                    purpose.eq("staff_support_travel")?
                }
                Err(error) => return Err(error),
            };
            if staff_travel {
                self.controller
                    .call_method1(intern!(py, "_record_staff_support_started"), (world, outcome.event(py)?))?;
            }
        }
        if &*event_type == "support_request_timeout" {
            self.controller
                .call_method1(intern!(py, "_record_request_interrupted"), (world, outcome.event(py)?))?;
        }
        Ok(())
    }

    fn learn(&self, outcome: &mut Outcome<'py>) -> PyResult<()> {
        let py = self.py();
        let world = self.world;
        let python_rule = |outcome: &mut Outcome<'py>| -> PyResult<()> {
            let event = outcome.event(py)?;
            let payload = attribute(&event, intern!(py, "payload"))?;
            let agent_id = payload.call_method1(intern!(py, "get"), ("agent_id",))?;
            let facility_id = payload.call_method1(intern!(py, "get"), ("facility_id",))?;
            if agent_id.is_instance_of::<PyString>() && facility_id.is_instance_of::<PyString>() {
                self.module.getattr(intern!(py, "learn_facility"))?.call1((world, agent_id, facility_id))?;
            }
            Ok(())
        };
        if !self.direct {
            return python_rule(outcome);
        }
        let ids = (|| Ok::<_, PyErr>((outcome.reference().text("agent_id")?, outcome.reference().text("facility_id")?)))();
        let ids = match ids {
            Ok(ids) => ids,
            Err(error) if error.is_instance_of::<Unsupported>(py) => return python_rule(outcome),
            Err(error) => return Err(error),
        };
        let (Some(agent_id), Some(facility_id)) = ids else {
            return Ok(());
        };
        match learn_facility_of(world, &agent_id, &facility_id) {
            Ok(()) => {
                count(&COUNTS.learn_ran);
                Ok(())
            }
            Err(error) if error.is_instance_of::<Unsupported>(py) => {
                refused(py, "learn_facility", &COUNTS.learn_refused, &error);
                python_rule(outcome)
            }
            Err(error) => Err(error),
        }
    }

    /// `_sync_movement_after_events`. Only a stopped journey calls Python.
    fn sync_movement(&self) -> PyResult<()> {
        let py = self.py();
        let table = attribute(self.world, intern!(py, "model_state"))?;
        if raw_dict_get(&table, intern!(py, MOVEMENT).as_any())?.is_none() {
            return Ok(());
        }
        match self.stopped_journeys() {
            Ok(()) => {
                count(&COUNTS.sync_ran);
                Ok(())
            }
            Err(error) if error.is_instance_of::<Unsupported>(py) => {
                // It goes over all journeys, and those finished above have left.
                refused(py, "_sync_movement_after_events", &COUNTS.sync_refused, &error);
                self.controller.call_method0(intern!(py, "_sync_movement_after_events"))?;
                Ok(())
            }
            Err(error) => Err(error),
        }
    }

    fn stopped_journeys(&self) -> PyResult<()> {
        let py = self.py();
        let mirror = mirror_of(self.world, MOVEMENT)?;
        let mut ids: Vec<Arc<str>> =
            NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| Ok(row.journeys.iter().map(|(id, _)| id.clone()).collect()))?;
        // `sorted`: bytes of UTF-8 sort as code points do.
        ids.sort_unstable();
        let store = entities_of(self.world)?;
        for id in ids {
            let purpose = NamespaceMirror::with_row::<MovementRow, _>(&mirror_of(self.world, MOVEMENT)?, |row| {
                Ok(row.journeys.get(&*id).map(|journey| journey.purpose.clone()))
            })?;
            let stopped = EntityStore::with_rows(&store, |rows| {
                Ok(match rows.agents.get(&*id) {
                    None => true,
                    Some(agent) => {
                        let queue_trip = agent.activity == Activity::Queued
                            && matches!(
                                purpose.as_deref(),
                                Some("toilet_queue_position" | "resource_queue_position" | "toilet_queue_service" | "resource_queue_service")
                            );
                        agent.activity != Activity::Moving && !queue_trip
                    }
                })
            })?;
            if stopped {
                self.controller.call_method1(intern!(py, "_finish_stopped_journey"), (&*id,))?;
            }
        }
        Ok(())
    }

    fn run(&self) -> PyResult<()> {
        let py = self.py();
        let world = self.world;
        let controller = self.controller;
        let due_events = {
            let writer = EventWriter::of(world)?;
            let due = writer.queue().borrow().due_events(py, self.minute_value);
            due
        };
        let mut outcomes = Vec::with_capacity(due_events.len());
        for due in due_events {
            self.one_event(due, &mut outcomes)?;
        }
        for outcome in outcomes.iter_mut().filter(|outcome| outcome.applied) {
            self.record_applied(outcome)?;
        }
        let applied = |outcome: &&mut Outcome<'py>| outcome.applied;
        if outcomes.iter_mut().filter(applied).any(|outcome| {
            matches!(&*outcome.event_type, "effect_program_geometry" | "effect_program_geometry_expire" | "effect_program_structure")
        }) {
            controller.call_method0(intern!(py, "_refresh_space"))?;
        }
        for outcome in outcomes.iter_mut().filter(applied) {
            if &*outcome.event_type == "effect_program_geometry" {
                controller.call_method1(intern!(py, "_interrupt_trips_of_event"), (world, outcome.event(py)?))?;
            }
        }
        controller.call_method1(intern!(py, "_admit_pending_returns"), (world, self.minute))?;
        self.sync_movement()?;
        self.module.getattr(intern!(py, "admit_pending_arrivals"))?.call1((world, self.minute))?;
        for outcome in outcomes.iter_mut().filter(applied) {
            if &*outcome.event_type == "information_contact_update" {
                controller.call_method1(intern!(py, "_consider_social_contact_event"), (outcome.event(py)?,))?;
            }
        }
        Ok(())
    }
}

/// `_run_dispatch_phase`.
#[pyfunction]
fn run_phase<'py>(
    controller: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let refuse = |error: PyErr| -> PyErr {
        refused(py, "phase", &COUNTS.phases_refused, &error);
        error
    };
    let Some(table) = TABLE.get(py) else {
        return Err(refuse(Unsupported::new_err("events/handlers.py has not installed the table")));
    };
    let minute_value = exact_int(minute).map_err(refuse)?;
    if minute_value < 0 {
        return Err(refuse(Unsupported::new_err("a negative minute")));
    }
    let dispatcher = attribute(controller, intern!(py, "event_dispatcher")).map_err(refuse)?;
    if !dispatcher.get_type().is(table.dispatcher.bind(py)) {
        return Err(refuse(Unsupported::new_err("another kind of dispatcher")));
    }
    EventWriter::of(world).map_err(refuse)?;
    let module = PHASE_MODULE
        .get_or_try_init(py, || {
            Ok::<_, PyErr>(py.import("sheltersim.experiments.phases.dispatch")?.unbind())
        })?
        .bind(py)
        .clone();
    let phase = Phase {
        controller,
        world,
        minute,
        minute_value,
        dispatcher,
        table,
        module,
        direct: !PYTHON_ENTRIES.load(Ordering::Relaxed),
    };
    count(&COUNTS.phases_ran);
    phase.run().map_err(|error| {
        // The phase has begun, so the Python step must not run over it.
        if error.is_instance_of::<Unsupported>(py) {
            PyRuntimeError::new_err(format!("a rule refused in the dispatch phase past its checks: {}", error.value(py)))
        } else {
            error
        }
    })
}

/// What `events/handlers.py` gives once: the handlers the table stands for, as defined.
#[pyfunction]
fn install_table<'py>(
    py: Python<'py>,
    social: (Bound<'py, PyType>, Bound<'py, PyAny>),
    travel: (Bound<'py, PyType>, Bound<'py, PyAny>),
    dispatcher: Bound<'py, PyType>,
) -> PyResult<()> {
    let models = py.import("sheltersim.events.models")?;
    let status = models.getattr("EventStatus")?;
    let table = Table {
        social: (social.0.unbind(), social.1.unbind()),
        travel: (travel.0.unbind(), travel.1.unbind()),
        dispatcher: dispatcher.unbind(),
        suppressed: models.getattr("EventSuppressed")?.downcast_into::<PyType>()?.unbind(),
        failed: models.getattr("EventApplicationError")?.downcast_into::<PyType>()?.unbind(),
        statuses: [
            status.getattr("APPLIED")?.unbind(),
            status.getattr("FAILED")?.unbind(),
            status.getattr("SUPPRESSED")?.unbind(),
        ],
    };
    // A second import of the module, as `importlib.reload` makes, keeps the first table: the
    // classes then differ from the handlers', which go to Python.
    let _ = TABLE.set(py, table);
    Ok(())
}

/// Call each rule through its Python entry, for benchmarks/rule_shadow.
#[pyfunction]
fn use_python_entries(value: bool) {
    PYTHON_ENTRIES.store(value, Ordering::Relaxed);
}

/// What has run in this process: `(counts, refusals by reason)`.
#[pyfunction]
fn stats<'py>(py: Python<'py>) -> PyResult<(Bound<'py, PyDict>, Bound<'py, PyDict>)> {
    let reasons = PyDict::new(py);
    if let Ok(refusals) = REFUSALS.lock() {
        for (reason, times) in refusals.iter() {
            reasons.set_item(reason, *times)?;
        }
    }
    Ok((counts_of(py)?, reasons))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_phase, m)?)?;
    m.add_function(wrap_pyfunction!(install_table, m)?)?;
    m.add_function(wrap_pyfunction!(use_python_entries, m)?)?;
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    Ok(())
}
