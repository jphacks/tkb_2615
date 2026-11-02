//! One tick's phases in order (`experiments/phases/*.py`), calling kernels directly and Python parts
//! through one entry per part (docs/rust-port/lanes/m6-driver.md).
//!
//! `TickEngine.step` asks `controller.run_tick` and, on false, runs the phases through `phase_handler`
//! as without the kernels. This refuses, with Unsupported and before anything has changed, a tick that
//! a hook changes: a benchmark's shadow, a patch of any step or rule it would skip. `install` gives
//! what those are as defined, and they are compared at each tick, so a patch shows there. A trace and
//! a Provider run here: the kernels write the trace's documents, and the Provider's hooks are Python
//! that the kernels call where the phases call them.
//!
//! After that, a step goes as its `segments.py` wrapper or `kernel_first` does: the kernel, and on its
//! Unsupported (nothing changed) the Python rule. No borrow is held across a call into Python.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;

use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyList, PyString, PyTuple, PyType};

use crate::core::census;
use crate::core::row::{attribute, exact_int, exact_str, unsupported};
use crate::core::world_core::raw_dict_get;
use crate::rules::movement_geometry::Geo;
use crate::rules::{decisions, dispatch, facility_queues, information, metrics, movement, proposals, proximity, psychology, queues, replan, service, social_propagation, staff, staff_dynamics, support, toilet, trace, world_view};
use crate::Unsupported;

const MOVEMENT: &str = "movement_v1";

/// What Python gives once.
struct Table {
    controller: Py<PyType>,
    toilet: Py<PyType>,
    resource: Py<PyType>,
    staff: Py<PyType>,
    /// The names of the controller's steps, and each as defined: on the class or on its bases.
    steps: Vec<(Py<PyString>, Py<PyAny>)>,
    /// `(holder, name, as defined)` of what a step calls: a module's function, a manager's method.
    rules: Vec<(Py<PyAny>, Py<PyString>, Py<PyAny>)>,
    /// `TICK_PHASE_ORDER`.
    phases: Vec<Py<PyAny>>,
    arbitrate: Py<PyModule>,
    update: Py<PyModule>,
    facilities: Py<PyModule>,
    metrics_recorder: Py<PyType>,
    clock_error: Py<PyType>,
}

static TABLE: GILOnceCell<Table> = GILOnceCell::new();

/// For benchmarks/rule_shadow, whose shadows replace kernels at their Python entries.
static PYTHON_PATH_ONLY: AtomicBool = AtomicBool::new(false);
static TICKS: AtomicU64 = AtomicU64::new(0);
static KERNELS_REFUSED: AtomicU64 = AtomicU64::new(0);
static REFUSALS: Mutex<BTreeMap<&'static str, u64>> = Mutex::new(BTreeMap::new());
static KERNEL_REASONS: Mutex<BTreeMap<String, u64>> = Mutex::new(BTreeMap::new());

#[track_caller]
fn refuse<T>(reason: &'static str) -> PyResult<T> {
    if let Ok(mut refusals) = REFUSALS.lock() {
        *refusals.entry(reason).or_default() += 1;
    }
    unsupported(reason)
}

/// Whether a kernel said Unsupported, which leaves the step to its Python rule.
fn refused(py: Python<'_>, error: &PyErr, kernel: &str) -> bool {
    if error.is_instance_of::<Unsupported>(py) {
        census::caught(py, kernel, error);
        KERNELS_REFUSED.fetch_add(1, Ordering::Relaxed);
        if let Ok(mut reasons) = KERNEL_REASONS.lock() {
            *reasons.entry(error.value(py).to_string()).or_default() += 1;
        }
        true
    } else {
        false
    }
}

/// The kernel's result, or the Python rule's where the kernel refused.
macro_rules! or_rule {
    ($py:expr, $kernel:expr, $rule:expr) => {
        match $kernel {
            Err(error) if refused($py, &error, concat!("or_rule ", stringify!($kernel))) => $rule,
            other => other,
        }
    };
}

/// `getattr(holder, name).__wrapped__`: the Python rule behind `kernel_first`.
fn rule_of<'py>(holder: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    census::called_back(&name.to_string_lossy());
    holder.getattr(name)?.getattr(intern!(holder.py(), "__wrapped__"))
}

struct Tick<'a, 'py> {
    table: &'a Table,
    controller: &'a Bound<'py, PyAny>,
    world: &'a Bound<'py, PyAny>,
    minute: &'a Bound<'py, PyAny>,
    minute_value: i64,
    rng: &'a Bound<'py, PyAny>,
}

impl<'py> Tick<'_, 'py> {
    fn py(&self) -> Python<'py> {
        self.world.py()
    }

    /// The Python step of the phases' mixins, past the wrapper of `segments.py`.
    fn python_step(&self, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
        let py = self.py();
        let proxy = py
            .import(intern!(py, "builtins"))?
            .getattr(intern!(py, "super"))?
            .call1((self.table.controller.bind(py), self.controller))?;
        proxy.getattr(name)
    }

    fn dispatch(&self) -> PyResult<()> {
        let py = self.py();
        or_rule!(py, dispatch::run_for_driver(self.controller, self.world, self.minute), {
            self.python_step(intern!(py, "_run_dispatch_phase"))?.call1((self.world, self.minute)).map(|_| ())
        })
    }

    fn update(&self) -> PyResult<()> {
        let py = self.py();
        let (controller, world, minute) = (self.controller, self.world, self.minute);
        // `_update_movement`.
        let table = attribute(world, intern!(py, "model_state"))?;
        if let Some(state) = raw_dict_get(&table, intern!(py, MOVEMENT).as_any())?.filter(|state| !state.is_none()) {
            let moved = or_rule!(py, movement::advance_movement(world, minute).map(Bound::into_any), {
                rule_of(self.table.update.bind(py).as_any(), intern!(py, "advance_movement"))?.call1((world, minute))
            })?;
            if moved.is_truthy()? {
                or_rule!(py, replan::replan_journeys(controller, &state, &moved, minute), {
                    self.python_step(intern!(py, "_replan_journeys"))?.call1((&state, &moved, minute)).map(|_| ())
                })?;
            }
        }
        let view = or_rule!(py, world_view::view_after_movement(controller, world), {
            self.python_step(intern!(py, "_view_after_movement"))?.call1((world,))
        })?;
        let result = or_rule!(py, psychology::update_psychology(controller, &view, minute), {
            self.python_step(intern!(py, "_psychology_update"))?.call1((&view, minute))
        })?;
        let base = result.getattr(intern!(py, "deltas"))?;
        let deltas = or_rule!(py, social_propagation::update_social_propagation(controller, world, &view, &base, minute, self.rng), {
            self.python_step(intern!(py, "_social_propagation"))?.call1((world, &view, &base, minute, self.rng))
        })?;
        or_rule!(py, trace::record_psychology_trace(controller, &result, &deltas, minute), {
            self.python_step(intern!(py, "_record_psychology_trace"))?.call1((&result, &deltas, minute)).map(|_| ())
        })?;
        or_rule!(py, psychology::update_long_term(controller, world, &view, &deltas, minute), {
            self.python_step(intern!(py, "_update_long_term_psychology"))?.call1((world, &view, &deltas, minute)).map(|_| ())
        })?;
        or_rule!(py, psychology::apply_deltas(controller, world, &deltas), {
            self.python_step(intern!(py, "_apply_state_deltas"))?.call1((world, &deltas)).map(|_| ())
        })?;
        controller.setattr(intern!(py, "_view_before_proposals"), view)
    }

    fn propose(&self) -> PyResult<()> {
        let py = self.py();
        let (controller, world, minute) = (self.controller, self.world, self.minute);
        let view = or_rule!(py, world_view::view_for_proposals(controller, world, minute), {
            self.python_step(intern!(py, "_view_for_proposals"))?.call1((world, minute))
        })?;
        let proposals_due = or_rule!(py, decisions::propose_rows(controller, &view, self.rng), {
            self.python_step(intern!(py, "_propose_due"))?.call1((&view, self.rng))
        })?;
        for proposal in proposals_due.try_iter()? {
            let proposal = proposal?;
            let agent_id = proposal.getattr(intern!(py, "agent_id"))?;
            let observed = or_rule!(py, decisions::observed_row(controller, &view, &agent_id), {
                self.python_step(intern!(py, "_observed_state"))?.call1((&view, &agent_id))
            })?;
            let effect = or_rule!(py, proposals::apply_effect(controller, &proposal).map(Bound::into_any), {
                self.python_step(intern!(py, "_apply_effect"))?.call1((&proposal,))
            })?;
            let effect = effect.downcast_into::<PyTuple>()?;
            let (target_id, next_decision_at, committed) = (effect.get_item(0)?, effect.get_item(1)?, effect.get_item(2)?);
            or_rule!(py, decisions::record_outcome(controller, &proposal, &target_id, &next_decision_at, &committed, &observed), {
                let options = PyDict::new(py);
                options.set_item(intern!(py, "target_id"), &target_id)?;
                options.set_item(intern!(py, "next_decision_at"), &next_decision_at)?;
                options.set_item(intern!(py, "committed"), &committed)?;
                options.set_item(intern!(py, "observed_state"), &observed)?;
                self.python_step(intern!(py, "_record_outcome"))?.call((&proposal,), Some(&options)).map(|_| ())
            })?;
        }
        Ok(())
    }

    /// `manager.arbitrate`, by the method, of a manager of another class.
    fn arbitrate_in_python(&self, manager: &Bound<'py, PyAny>) -> PyResult<()> {
        let py = self.py();
        let options = PyDict::new(py);
        options.set_item(intern!(py, "validate_state"), false)?;
        manager.call_method(intern!(py, "arbitrate"), (self.minute, self.rng), Some(&options)).map(|_| ())
    }

    fn prepare_queue(&self, facility_id: &Bound<'py, PyAny>, resource: bool) -> PyResult<()> {
        let py = self.py();
        let flag = PyBool::new(py, resource).to_owned().into_any();
        or_rule!(py, queues::prepare_facility_queue(self.world, facility_id, &flag, self.minute), {
            let options = PyDict::new(py);
            options.set_item(intern!(py, "resource"), resource)?;
            options.set_item(intern!(py, "minute"), self.minute)?;
            rule_of(self.table.facilities.bind(py).as_any(), intern!(py, "prepare_facility_queue"))?
                .call((self.world, facility_id), Some(&options))
                .map(|_| ())
        })
    }

    fn prepare_independent_queue(&self, queue_id: &Bound<'py, PyAny>, resource: bool) -> PyResult<()> {
        let py = self.py();
        let flag = PyBool::new(py, resource).to_owned().into_any();
        or_rule!(py, queues::prepare_independent_facility_queue(self.world, queue_id, self.minute, &flag), {
            let options = PyDict::new(py);
            options.set_item(intern!(py, "minute"), self.minute)?;
            options.set_item(intern!(py, "resource"), resource)?;
            rule_of(self.table.facilities.bind(py).as_any(), intern!(py, "prepare_independent_facility_queue"))?
                .call((self.world, queue_id), Some(&options))
                .map(|_| ())
        })
    }

    /// `progress_physical_queue_positions` or `progress_physical_queue_head_to_service`.
    fn progress_queue(&self, facility_id: &Bound<'py, PyAny>, resource: bool, head: bool) -> PyResult<bool> {
        self.progress_fifo(facility_id, resource, head, None)
    }

    fn progress_fifo(&self, facility_id: &Bound<'py, PyAny>, resource: bool, head: bool, queue_id: Option<&Bound<'py, PyAny>>) -> PyResult<bool> {
        let py = self.py();
        let flag = PyBool::new(py, resource).to_owned().into_any();
        let kernel = if head {
            queues::progress_physical_queue_head_to_service(self.world, facility_id, &flag, self.minute, queue_id)
        } else {
            queues::progress_physical_queue_positions(self.world, facility_id, &flag, self.minute, queue_id)
        };
        or_rule!(py, kernel, {
            let options = PyDict::new(py);
            options.set_item(intern!(py, "resource"), resource)?;
            options.set_item(intern!(py, "minute"), self.minute)?;
            if let Some(queue_id) = queue_id {
                options.set_item(intern!(py, "queue_id"), queue_id)?;
            }
            let name = if head { intern!(py, "progress_physical_queue_head_to_service") } else { intern!(py, "progress_physical_queue_positions") };
            rule_of(self.table.facilities.bind(py).as_any(), name)?
                .call((self.world, facility_id), Some(&options))?
                .is_truthy()
        })
    }

    /// The queues of a facility with independent ones, each with the check of its signed point.
    fn independent_queues(&self, facility_id: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        if !facility_queues::has_independent(self.world, exact_str(facility_id)?)? {
            return Ok(None);
        }
        facility_queues::queue_ids_for_facility(self.world, facility_id).map(Some)
    }

    /// `raise StateInvariantError(f"{label} queue {queue_id!r} lost its signed point")`, as needed.
    fn check_signed_point(&self, queue_id: &Bound<'py, PyAny>, label: &str) -> PyResult<()> {
        let py = self.py();
        if facility_queues::point_of(&*Geo::of(self.world)?, exact_str(queue_id)?).is_some() {
            return Ok(());
        }
        let class = py.import(intern!(py, "sheltersim.core.world"))?.getattr(intern!(py, "StateInvariantError"))?;
        let text = format!("{label} queue {} lost its signed point", queue_id.repr()?);
        Err(PyErr::from_value(class.call1((text,))?))
    }

    fn queue_has_people(&self, queue_id: &Bound<'py, PyAny>) -> PyResult<bool> {
        let id = exact_str(queue_id)?;
        let queued = facility_queues::with_queues(self.world, |row| match row.queues.get(id) {
            Some(queue) => Ok(!queue.queued_request_ids.is_empty()),
            None => unsupported("no such queue"),
        })?;
        queued.ok_or_else(|| Unsupported::new_err("no queue state"))
    }

    /// `ToiletServiceManager._arbitrate_independent_facility`.
    fn arbitrate_independent_toilets(&self, manager: &Bound<'py, PyAny>, id: &Bound<'py, PyAny>, queues: &Bound<'py, PyTuple>) -> PyResult<()> {
        let py = self.py();
        for queue_id in queues.iter() {
            self.prepare_independent_queue(&queue_id, false)?;
            self.check_signed_point(&queue_id, "service")?;
            while self.queue_has_people(&queue_id)? {
                if self.progress_fifo(id, false, false, Some(&queue_id))? {
                    break;
                }
                let started = or_rule!(py, toilet::serve_independent_head(manager, id, &queue_id, self.minute, self.rng).map(|event| event.map(Bound::into_any)), {
                    rule_of(manager, intern!(py, "_serve_independent_head"))?.call1((manager, id, &queue_id, self.minute, self.rng)).map(|event| Some(event).filter(|event| !event.is_none()))
                })?;
                if started.is_none() {
                    break;
                }
            }
        }
        Ok(())
    }

    /// `ResourceServiceManager._arbitrate_independent_facility`.
    fn arbitrate_independent_resources(&self, manager: &Bound<'py, PyAny>, id: &Bound<'py, PyAny>, resource: &Bound<'py, PyAny>, queues: &Bound<'py, PyTuple>) -> PyResult<()> {
        let py = self.py();
        let stalls = PyList::empty(py);
        let table = attribute(self.world, intern!(py, "model_state"))?;
        let previous = table.get_item(intern!(py, "resource_service_v1"))?.get_item(intern!(py, "stall_state"))?.call_method1(intern!(py, "get"), (id,))?;
        for queue_id in queues.iter() {
            self.prepare_independent_queue(&queue_id, true)?;
            self.check_signed_point(&queue_id, "resource")?;
            while self.queue_has_people(&queue_id)? {
                if self.progress_fifo(id, true, false, Some(&queue_id))? {
                    break;
                }
                let served = or_rule!(py, service::serve_independent_head(manager, id, resource, &queue_id, self.minute).map(Bound::into_any), {
                    rule_of(manager, intern!(py, "_serve_independent_head"))?.call1((manager, id, resource, &queue_id, self.minute))
                })?;
                let (started, stall) = (served.get_item(0)?, served.get_item(1)?);
                if !stall.is_none() {
                    stalls.append(stall)?;
                }
                if started.is_none() {
                    break;
                }
            }
        }
        or_rule!(py, service::record_independent_stall(manager, id, stalls.as_any(), &previous), {
            rule_of(manager, intern!(py, "_record_independent_queue_stall"))?.call1((manager, id, &stalls, &previous)).map(|_| ())
        })
    }

    /// `ToiletServiceManager.arbitrate`. Nothing reads what it returns here, so the started events
    /// aren't looked up.
    fn arbitrate_toilets(&self, manager: &Bound<'py, PyAny>) -> PyResult<()> {
        let py = self.py();
        let minute = self.minute;
        let due = or_rule!(py, toilet::facilities_to_serve(manager).map(Bound::into_any), {
            rule_of(manager, intern!(py, "_facilities_to_serve"))?.call1((manager,))
        })?;
        for facility in due.try_iter()? {
            let id = facility?.getattr(intern!(py, "id"))?;
            if let Some(queues) = self.independent_queues(&id)? {
                self.arbitrate_independent_toilets(manager, &id, &queues)?;
                continue;
            }
            self.prepare_queue(&id, false)?;
            loop {
                let room = or_rule!(py, toilet::has_room(manager, &id), {
                    rule_of(manager, intern!(py, "_has_room"))?.call1((manager, &id))?.is_truthy()
                })?;
                if !room || self.progress_queue(&id, false, false)? || self.progress_queue(&id, false, true)? {
                    break;
                }
                let started = or_rule!(py, toilet::start_head(manager, &id, minute, self.rng).map(|event| event.map(Bound::into_any)), {
                    rule_of(manager, intern!(py, "_start_head"))?.call1((manager, &id, minute, self.rng)).map(|event| Some(event).filter(|event| !event.is_none()))
                })?;
                if started.is_none() {
                    break;
                }
            }
        }
        Ok(())
    }

    /// `ResourceServiceManager.arbitrate`.
    fn arbitrate_resources(&self, manager: &Bound<'py, PyAny>) -> PyResult<()> {
        let py = self.py();
        let due = or_rule!(py, service::facilities_to_serve(manager).map(Bound::into_any), {
            rule_of(manager, intern!(py, "_facilities_to_serve"))?.call1((manager,))
        })?;
        for item in due.try_iter()? {
            let item = item?;
            let (facility, resource) = (item.get_item(0)?, item.get_item(1)?);
            let id = facility.getattr(intern!(py, "id"))?;
            if let Some(queues) = self.independent_queues(&id)? {
                self.arbitrate_independent_resources(manager, &id, &resource, &queues)?;
                continue;
            }
            self.prepare_queue(&id, true)?;
            while facility.getattr(intern!(py, "resource_queue_agent_ids"))?.is_truthy()? {
                if self.progress_queue(&id, true, false)? {
                    break;
                }
                let started = or_rule!(py, service::serve_head(manager, &id, &resource, self.minute).map(|event| event.map(Bound::into_any)), {
                    rule_of(manager, intern!(py, "_serve_head"))?.call1((manager, &id, &resource, self.minute)).map(|event| Some(event).filter(|event| !event.is_none()))
                })?;
                if started.is_none() {
                    break;
                }
            }
        }
        Ok(())
    }

    fn arbitrate(&self) -> PyResult<()> {
        let py = self.py();
        let (controller, world, minute) = (self.controller, self.world, self.minute);
        let module = self.table.arbitrate.bind(py).as_any();
        or_rule!(py, staff_dynamics::update(world, minute), {
            rule_of(module, intern!(py, "update_staff_dynamics"))?.call1((world, minute)).map(|_| ())
        })?;
        or_rule!(py, staff::update_operations(world, minute), {
            rule_of(module, intern!(py, "update_staff_operations"))?.call1((world, minute)).map(|_| ())
        })?;
        // `_arbitrate_services`.
        let toilets = controller.getattr(intern!(py, "toilet_service"))?;
        if !toilets.get_type().is(self.table.toilet.bind(py)) {
            self.arbitrate_in_python(&toilets)?;
        } else {
            self.arbitrate_toilets(&toilets)?;
        }
        let resources = controller.getattr(intern!(py, "resource_service"))?;
        if !resources.is_none() {
            if !resources.get_type().is(self.table.resource.bind(py)) {
                self.arbitrate_in_python(&resources)?;
            } else {
                self.arbitrate_resources(&resources)?;
            }
        }
        // `_assign_staff_support`.
        let service = controller.getattr(intern!(py, "staff_service"))?;
        let assigned = if service.get_type().is(self.table.staff.bind(py)) {
            or_rule!(py, staff::assign_pending(&service, minute, false).map(Bound::into_any), {
                let options = PyDict::new(py);
                options.set_item(intern!(py, "validate_state"), false)?;
                rule_of(&service, intern!(py, "assign_pending"))?.call((&service, minute), Some(&options))
            })?
        } else {
            let options = PyDict::new(py);
            options.set_item(intern!(py, "validate_state"), false)?;
            service.call_method(intern!(py, "assign_pending"), (minute,), Some(&options))?
        };
        if assigned.is_truthy()? {
            // The kernel's receipts: past the service's writes nothing goes back to a Python rule.
            support::record_assigned(world, &assigned).map_err(|error| {
                if error.is_instance_of::<Unsupported>(py) {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("a receipt of an assignment was refused: {}", error.value(py)))
                } else {
                    error
                }
            })?;
        }
        let router = controller.getattr(intern!(py, "staff_space"))?;
        or_rule!(py, staff::assign_operations(world, minute, &router).map(|_| ()), {
            rule_of(module, intern!(py, "assign_staff_operations"))?.call1((world, minute, &router)).map(|_| ())
        })?;
        // Its decision is the policy's, called through `consider_complaint_response`.
        or_rule!(py, staff::consider_due_complaints(controller, minute), {
            self.python_step(intern!(py, "_consider_due_complaints"))?.call1((minute,)).map(|_| ())
        })?;
        or_rule!(py, staff::relief(world, minute).map(|_| ()), {
            rule_of(module, intern!(py, "schedule_staff_operation_relief"))?.call1((world, minute)).map(|_| ())
        })
    }

    fn sample(&self) -> PyResult<()> {
        let py = self.py();
        or_rule!(py, information::sample_contacts(self.controller, self.minute, self.rng), {
            self.python_step(intern!(py, "_sample_information_contacts"))?.call1((self.minute, self.rng)).map(|_| ())
        })?;
        if !self.controller.getattr(intern!(py, "proximity_contact_config"))?.is_none() {
            or_rule!(py, proximity::sample_contacts(self.controller, self.minute), {
                self.python_step(intern!(py, "_sample_proximity_contacts"))?.call1((self.minute,)).map(|_| ())
            })?;
        }
        Ok(())
    }

    fn metrics(&self) -> PyResult<()> {
        let py = self.py();
        let artifacts = self.controller.getattr(intern!(py, "artifacts"))?;
        // `_MetricSink.capture_metric` is only `MetricsRecorder.capture`. Anything else is the sink's.
        let sink = artifacts.get_type();
        let recorder = sink.getattr(intern!(py, "capture_metric_as_defined")).ok().filter(|defined| {
            sink.getattr(intern!(py, "capture_metric")).is_ok_and(|now| now.is(defined))
        });
        let own = artifacts.getattr(intern!(py, "__dict__"))?;
        let recorder = match recorder {
            Some(_) if raw_dict_get(&own, intern!(py, "capture_metric").as_any())?.is_none() => {
                raw_dict_get(&own, intern!(py, "metrics").as_any())?
                    .filter(|recorder| recorder.get_type().is(self.table.metrics_recorder.bind(py)))
            }
            _ => None,
        };
        let by_sink = || {
            let options = PyDict::new(py);
            options.set_item(intern!(py, "minute"), self.minute)?;
            artifacts.call_method(intern!(py, "capture_metric"), (self.world,), Some(&options)).map(|_| ())
        };
        match &recorder {
            Some(recorder) => or_rule!(py, metrics::capture(self.world, recorder, self.minute), by_sink())?,
            None => by_sink()?,
        }
        // `_finalize_actions_at_end` reads it again, and acts at the Run's last minute.
        let duration = self.world.getattr(intern!(py, "duration_minutes"))?;
        if exact_int(&duration).map_or(true, |duration| self.minute_value + 1 >= duration) {
            self.controller.call_method1(intern!(py, "_finalize_actions_at_end"), (self.world, self.minute))?;
        }
        Ok(())
    }

    fn phase(&self, index: usize) -> PyResult<()> {
        match index {
            0 => self.dispatch(),
            // `complete_scheduled_tasks` has no step in the controller.
            1 => Ok(()),
            2 => self.update(),
            3 => self.propose(),
            4 => self.arbitrate(),
            5 => self.sample(),
            6 => self.metrics(),
            _ => Err(PyRuntimeError::new_err("a phase the driver doesn't know")),
        }
    }

    fn run(&self, observer: &Bound<'py, PyAny>) -> PyResult<()> {
        let py = self.py();
        let watched = !observer.is_none();
        for (index, phase) in self.table.phases.iter().enumerate() {
            if watched {
                observer.call1((phase.bind(py), true))?;
            }
            self.phase(index)?;
            if !self.world.getattr(intern!(py, "minute_index"))?.eq(self.minute)? {
                return Err(PyErr::from_type(
                    self.table.clock_error.bind(py).clone(),
                    "tick phase attempted to change the engine-owned clock",
                ));
            }
            if watched {
                observer.call1((phase.bind(py), false))?;
            }
        }
        Ok(())
    }
}

/// Why Python runs this tick, before anything has changed.
fn accept(table: &Table, controller: &Bound<'_, PyAny>, world: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    if PYTHON_PATH_ONLY.load(Ordering::Relaxed) || dispatch::python_entries() {
        return refuse("a benchmark's shadow is installed");
    }
    let class = table.controller.bind(py);
    if !controller.get_type().is(class) {
        return refuse("another class of controller");
    }
    if !attribute(controller, intern!(py, "world"))?.is(world) {
        return refuse("the controller is bound to another world");
    }
    let own = controller.getattr(intern!(py, "__dict__"))?;
    for (name, defined) in &table.steps {
        let name = name.bind(py);
        if !class.getattr(name)?.is(defined.bind(py)) {
            return refuse("a step of the controller is patched");
        }
        if raw_dict_get(&own, name.as_any())?.is_some() {
            return refuse("a step of this controller is patched");
        }
    }
    for (holder, name, defined) in &table.rules {
        if !holder.bind(py).getattr(name.bind(py))?.is(defined.bind(py)) {
            return refuse("a rule a step calls is patched");
        }
    }
    Ok(())
}

/// One tick, or Unsupported with nothing changed. `observer(phase, before)` is `TickEngine.phase_observer`.
#[pyfunction]
fn run_tick<'py>(
    controller: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    rng: &Bound<'py, PyAny>,
    observer: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let Some(table) = TABLE.get(py) else {
        return refuse("experiments/phases/driver.py has not installed the table");
    };
    accept(table, controller, world)?;
    let tick = Tick { table, controller, world, minute, minute_value: exact_int(minute)?, rng };
    TICKS.fetch_add(1, Ordering::Relaxed);
    tick.run(observer).map_err(|error| {
        // The tick has begun, so the Python path must not run over it.
        if error.is_instance_of::<Unsupported>(py) {
            PyRuntimeError::new_err(format!("a rule refused in the tick past its checks: {}", error.value(py)))
        } else {
            error
        }
    })
}

/// What `experiments/phases/driver.py` gives once.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn install<'py>(
    py: Python<'py>,
    controller: Bound<'py, PyType>,
    managers: (Bound<'py, PyType>, Bound<'py, PyType>, Bound<'py, PyType>),
    steps: Vec<Bound<'py, PyString>>,
    rules: Vec<(Bound<'py, PyAny>, Bound<'py, PyString>)>,
    phases: Vec<Bound<'py, PyAny>>,
    modules: (Bound<'py, PyModule>, Bound<'py, PyModule>, Bound<'py, PyModule>),
    metrics_recorder: Bound<'py, PyType>,
    clock_error: Bound<'py, PyType>,
) -> PyResult<()> {
    let mut defined_steps = Vec::with_capacity(steps.len());
    for name in steps {
        let defined = controller.getattr(&name)?.unbind();
        defined_steps.push((name.unbind(), defined));
    }
    let mut defined_rules = Vec::with_capacity(rules.len());
    for (holder, name) in rules {
        let defined = holder.getattr(&name)?.unbind();
        defined_rules.push((holder.unbind(), name.unbind(), defined));
    }
    let table = Table {
        controller: controller.unbind(),
        toilet: managers.0.unbind(),
        resource: managers.1.unbind(),
        staff: managers.2.unbind(),
        steps: defined_steps,
        rules: defined_rules,
        phases: phases.into_iter().map(Bound::unbind).collect(),
        arbitrate: modules.0.unbind(),
        update: modules.1.unbind(),
        facilities: modules.2.unbind(),
        metrics_recorder: metrics_recorder.unbind(),
        clock_error: clock_error.unbind(),
    };
    let _ = TABLE.set(py, table);
    Ok(())
}

/// Every tick through `phase_handler`, for benchmarks/rule_shadow and comparisons.
#[pyfunction]
fn python_path_only(value: bool) {
    PYTHON_PATH_ONLY.store(value, Ordering::Relaxed);
}

/// Whether a benchmark's shadows keep every tick on the Python path.
pub(crate) fn path_is_python_only() -> bool {
    PYTHON_PATH_ONLY.load(Ordering::Relaxed)
}

/// What has run in this process: `(counts, refused ticks and refused kernels by reason)`.
#[pyfunction]
fn stats<'py>(py: Python<'py>) -> PyResult<(Bound<'py, PyDict>, Bound<'py, PyDict>)> {
    let counts = PyDict::new(py);
    counts.set_item("ticks", TICKS.load(Ordering::Relaxed))?;
    counts.set_item("kernels_refused", KERNELS_REFUSED.load(Ordering::Relaxed))?;
    let reasons = PyDict::new(py);
    if let Ok(refusals) = REFUSALS.lock() {
        for (reason, times) in refusals.iter() {
            reasons.set_item(*reason, *times)?;
        }
    }
    if let Ok(refusals) = KERNEL_REASONS.lock() {
        for (reason, times) in refusals.iter() {
            reasons.set_item(format!("kernel: {reason}"), *times)?;
        }
    }
    Ok((counts, reasons))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_tick, m)?)?;
    m.add_function(wrap_pyfunction!(install, m)?)?;
    m.add_function(wrap_pyfunction!(python_path_only, m)?)?;
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    Ok(())
}
