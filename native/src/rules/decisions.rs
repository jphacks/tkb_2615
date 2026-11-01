//! The tail of `_apply_proposal` (`experiments/phases/propose.py`): what a decision records, with no
//! Python object for the proposal.
//!
//! `propose_rows` is `_propose_due`, returning `ProposalRow`s: what `_apply_proposal`'s Python body
//! reads (`agent_id`, `action`, `next_decision_at`, `target_id`, `observed_state_hash`) comes from the
//! row, and anything else makes the `ActionProposal` of rules/behavior.rs, once. `observed_row` is
//! `_observed_state`, which keeps only where to find the person in the snapshot. `record_outcome` is
//! `_record_outcome`: `record_decision`, then the lifecycle, then the memory, in that order.
//!
//! The record `_IndexedMetricSink.record_decision` keeps is written as JSON bytes, with the keys in the
//! order of its dict, and goes into `sink.observations` as bytes; the index reads them
//! (`HistoryIndex.ingest`). Everything that can refuse the record is read before it is appended, so
//! Unsupported from there runs the Python step, which changes nothing twice: a sink of another class,
//! a trace (`RunArtifactWriter`'s document needs the Python objects, so a Run with a trace is left to
//! Python and its document stays the same), a NaN, a large observed state, a tainted store. After the
//! append each rule says for itself: Unsupported from `commit_action` or `remember_decision` has
//! changed nothing, and that one rule then runs in Python.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyBytes, PyDict, PyString, PyTuple};

use crate::core::row::{attribute, exact_dict, exact_int, exact_list, exact_str, unsupported};
use crate::core::world_core::{raw_dict_get, raw_list_append};
use crate::rules::action_lifecycle::{commit_action, ActiveActions, Proposal};
use crate::rules::agent_dynamics::{remember_decision, Decision};
use crate::rules::behavior::{
    classes, demand_of, due_outcomes, observed_dict, person_of, proposal_of, snapshot_of, Common, Outcome,
};
use crate::rules::world_view::RustView;
use crate::state::entities::{entities_of, Activity, EntityStore, RequestStatus};
use crate::{write_json_string, Encoder, Unsupported};

/// `ActionProposal.trigger` of a scheduled decision.
const TRIGGER: &str = "scheduled_decision";
/// `len(json.dumps(observed))` is at most twice the length without its spaces, so a shorter one is
/// within the 16,384 of `record_decision`. A longer one is left to Python, which measures it.
const MAX_OBSERVED_BYTES: usize = 8192;
const AGENT_DYNAMICS: &str = "agent_dynamics_v1";
const PRESENCE: &str = "population_presence_v1";

// ---- The proposal ------------------------------------------------------------------------------

/// A proposal that has not become a Python object.
#[pyclass(module = "sheltersim_native", frozen)]
pub struct ProposalRow {
    view: Py<RustView>,
    index: usize,
    outcome: Outcome,
    common: Arc<Common>,
    made: GILOnceCell<Py<PyAny>>,
}

impl ProposalRow {
    fn made<'py>(&self, py: Python<'py>) -> PyResult<&Bound<'py, PyAny>> {
        let made = self.made.get_or_try_init(py, || {
            Ok::<_, PyErr>(proposal_of(py, &self.common, self.view.get(), self.index, &self.outcome)?.unbind())
        })?;
        Ok(made.bind(py))
    }

    /// `proposal.with_outcome(target_id, next_decision_at)`.
    fn recorded<'py>(
        &self,
        py: Python<'py>,
        target_id: &Bound<'py, PyAny>,
        next_decision_at: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.made(py)?.call_method1(intern!(py, "with_outcome"), (target_id, next_decision_at))
    }
}

#[pymethods]
impl ProposalRow {
    #[getter]
    fn agent_id<'py>(&self, py: Python<'py>) -> Bound<'py, PyString> {
        self.view.get().key(py, self.index).clone()
    }

    #[getter]
    fn action<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(classes(py)?.actions[self.outcome.action as usize].bind(py).clone())
    }

    #[getter]
    fn next_decision_at(&self) -> i64 {
        self.outcome.next_decision_at
    }

    #[getter]
    fn target_id<'py>(&self, py: Python<'py>) -> Bound<'py, PyAny> {
        match self.outcome.target {
            Some(target) => self.view.get().key(py, target).clone().into_any(),
            None => py.None().into_bound(py),
        }
    }

    #[getter]
    fn minute_index(&self) -> i64 {
        self.view.get().data().minute_index
    }

    #[getter]
    fn observed_state_hash<'py>(&self, py: Python<'py>) -> Bound<'py, PyAny> {
        self.common.state_hash.bind(py).clone()
    }

    #[getter]
    fn policy_version<'py>(&self, py: Python<'py>) -> Bound<'py, PyAny> {
        self.common.policy_version.bind(py).clone()
    }

    /// Whether anything has needed the `ActionProposal` yet.
    #[getter]
    fn has_object(&self, py: Python<'_>) -> bool {
        self.made.get(py).is_some()
    }

    /// The `ActionProposal`, the same object each time.
    fn materialize<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.made(py).cloned()
    }

    fn with_outcome<'py>(
        &self,
        py: Python<'py>,
        target_id: &Bound<'py, PyAny>,
        next_decision_at: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.recorded(py, target_id, next_decision_at)
    }

    /// Any other field of the `ActionProposal`.
    fn __getattr__<'py>(&self, py: Python<'py>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
        self.made(py)?.getattr(name)
    }
}

/// `_propose_due`, whose proposals the rest of the propose phase reads as rows.
#[pyfunction]
fn propose_rows<'py>(
    controller: &Bound<'py, PyAny>,
    view: &Bound<'py, PyAny>,
    rng: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = controller.py();
    let Some(due) = due_outcomes(controller, view, rng)? else {
        return Ok(PyTuple::empty(py).into_any());
    };
    let common = Arc::new(due.common);
    let snapshot = due.rust.unbind();
    let mut rows = Vec::with_capacity(due.outcomes.len());
    for (index, outcome) in due.outcomes {
        let row = ProposalRow {
            view: snapshot.clone_ref(py),
            index,
            outcome,
            common: common.clone(),
            made: GILOnceCell::new(),
        };
        rows.push(Bound::new(py, row)?);
    }
    Ok(PyTuple::new(py, rows)?.into_any())
}

// ---- The observed state ------------------------------------------------------------------------

/// `_agent_view_snapshot`, not yet made. `dict(row)` makes it, as `record_decision` does.
#[pyclass(module = "sheltersim_native", frozen)]
pub struct ObservedRow {
    view: Py<RustView>,
    index: usize,
    made: GILOnceCell<Py<PyDict>>,
}

impl ObservedRow {
    fn made<'py>(&self, py: Python<'py>) -> PyResult<&Bound<'py, PyDict>> {
        let made = self
            .made
            .get_or_try_init(py, || Ok::<_, PyErr>(observed_dict(py, self.view.get(), self.index)?.unbind()))?;
        Ok(made.bind(py))
    }
}

#[pymethods]
impl ObservedRow {
    fn keys<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.made(py)?.call_method0(intern!(py, "keys"))
    }

    fn __getitem__<'py>(&self, py: Python<'py>, key: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.made(py)?.as_any().get_item(key)
    }
}

/// What a sink does with a decision.
enum Sink<'py> {
    /// Nothing: `_MetricSink`, or `RunArtifactWriter` without a trace.
    Silent,
    /// `_IndexedMetricSink`.
    Indexed(Bound<'py, PyAny>),
    /// `artifacts.record_decision` runs in Python.
    Python,
}

/// A sink says what it does with `decision_sink` in its own class, not one it inherits: a subclass may
/// have another `record_decision`.
fn sink_of<'py>(controller: &Bound<'py, PyAny>) -> PyResult<Sink<'py>> {
    let py = controller.py();
    let Ok(artifacts) = controller.getattr(intern!(py, "artifacts")) else {
        return Ok(Sink::Python);
    };
    let class = artifacts.get_type();
    let Ok(kind) = class.getattr(intern!(py, "__dict__"))?.get_item(intern!(py, "decision_sink")) else {
        return Ok(Sink::Python);
    };
    Ok(match kind.downcast_exact::<PyString>()?.to_str()? {
        "silent" => Sink::Silent,
        "indexed" => Sink::Indexed(artifacts),
        "artifacts" => {
            let off = artifacts
                .getattr(intern!(py, "trace_level"))?
                .getattr(intern!(py, "value"))?
                .eq(intern!(py, "off"))?;
            let started = artifacts.getattr(intern!(py, "_started"))?.is_truthy()?;
            let finalized = artifacts.getattr(intern!(py, "_finalized"))?.is_truthy()?;
            if off && started && !finalized {
                Sink::Silent
            } else {
                Sink::Python
            }
        }
        _ => Sink::Python,
    })
}

/// `_observed_state`. A sink in Python may read the document as a dict, so it gets one.
#[pyfunction]
fn observed_row<'py>(
    controller: &Bound<'py, PyAny>,
    view: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = controller.py();
    let snapshot = snapshot_of(view)?;
    let index = person_of(snapshot.get(), agent_id)?;
    if matches!(sink_of(controller)?, Sink::Python) {
        return Ok(observed_dict(py, snapshot.get(), index)?.into_any());
    }
    Ok(Bound::new(py, ObservedRow { view: snapshot.unbind(), index, made: GILOnceCell::new() })?.into_any())
}

// ---- The record as JSON ------------------------------------------------------------------------

fn text(out: &mut Encoder, value: &str) {
    write_json_string(&mut out.out, value);
}

fn option_text(out: &mut Encoder, value: Option<&str>) {
    match value {
        Some(value) => text(out, value),
        None => out.out.extend_from_slice(b"null"),
    }
}

fn integer(out: &mut Encoder, value: i64) {
    out.out.extend_from_slice(itoa::Buffer::new().format(value).as_bytes());
}

fn text_list(out: &mut Encoder, items: &[Arc<str>]) {
    out.out.push(b'[');
    for (place, item) in items.iter().enumerate() {
        if place > 0 {
            out.out.push(b',');
        }
        text(out, item);
    }
    out.out.push(b']');
}

/// Writes `,"name":` before a value.
macro_rules! key {
    ($out:expr, $name:literal) => {
        $out.out.extend_from_slice(concat!(",\"", $name, "\":").as_bytes())
    };
}

/// `_agent_view_snapshot` with its keys in that function's order.
fn write_observed(out: &mut Encoder, py: Python<'_>, rust: &RustView, index: usize) -> PyResult<()> {
    let data = rust.data();
    let agent = &data.agents[index];
    let zone = data.zone_of(agent);
    out.out.extend_from_slice(b"{\"agent_id\":");
    text(out, rust.key(py, index).to_str()?);
    key!(out, "zone_id");
    text(out, &zone.id);
    key!(out, "activity");
    text(out, Activity::text(agent.activity)?);
    key!(out, "target_id");
    option_text(out, agent.target_id.as_deref());
    out.out.extend_from_slice(b",\"needs\":{\"toilet_urgency\":");
    out.float(agent.needs.toilet_urgency)?;
    key!(out, "hunger");
    out.float(agent.needs.hunger)?;
    key!(out, "fatigue");
    out.float(agent.needs.fatigue)?;
    key!(out, "hygiene_need");
    out.float(agent.body.hygiene_need)?;
    if data.resource_service_enabled {
        key!(out, "hydration_need");
        out.float(agent.needs.hydration_need)?;
    }
    out.out.extend_from_slice(b"},\"psychological\":{\"sleep_debt_minutes\":");
    out.float(agent.body.sleep_debt_minutes)?;
    key!(out, "uncertainty");
    out.float(agent.body.uncertainty)?;
    key!(out, "reassurance");
    out.float(agent.body.reassurance)?;
    key!(out, "discontent");
    out.float(agent.body.discontent)?;
    key!(out, "positive_affect");
    out.float(agent.body.positive_affect)?;
    key!(out, "negative_affect");
    out.float(agent.body.negative_affect)?;
    out.out.extend_from_slice(b"},\"sanitation_level\":");
    out.float(zone.sanitation_level)?;
    key!(out, "stress");
    out.float(agent.stress)?;
    key!(out, "trust");
    out.float(agent.trust)?;
    out.out.extend_from_slice(b",\"queue\":{\"task_id\":");
    option_text(out, agent.queue_task_id.as_deref());
    key!(out, "kind");
    option_text(out, agent.queue_kind.as_deref());
    key!(out, "target_id");
    option_text(out, agent.queue_target_id.as_deref());
    key!(out, "wait_minutes");
    integer(out, agent.queue_wait_minutes);
    out.out.extend_from_slice(b"},\"support\":{\"request_id\":");
    option_text(out, agent.support_request_id.as_deref());
    key!(out, "status");
    option_text(
        out,
        agent.support_request_status.map(|status| match status {
            RequestStatus::Pending => "pending",
            RequestStatus::Assigned => "assigned",
            RequestStatus::Resolved => "resolved",
            RequestStatus::Unresolved => "unresolved",
        }),
    );
    key!(out, "assigned_staff_id");
    option_text(out, agent.support_assigned_staff_id.as_deref());
    key!(out, "wait_minutes");
    integer(out, agent.support_wait_minutes);
    key!(out, "cooldown_remaining_minutes");
    integer(out, agent.support_cooldown_remaining_minutes);
    out.out.push(b'}');
    if !zone.zone_environment_modifier_event_ids.is_empty() || !zone.demand_modifiers.is_empty() {
        out.out.extend_from_slice(b",\"zone_environment\":{\"ambient_noise_level\":");
        out.float(zone.ambient_noise_level)?;
        key!(out, "rest_efficiency_multiplier");
        out.float(zone.rest_efficiency_multiplier)?;
        key!(out, "modifier_event_ids");
        text_list(out, &zone.zone_environment_modifier_event_ids);
        key!(out, "assumption_ids");
        text_list(out, &zone.zone_environment_assumption_ids);
        out.out.extend_from_slice(b",\"demand_modifiers\":[");
        for (place, item) in zone.demand_modifiers.iter().enumerate() {
            if place > 0 {
                out.out.push(b',');
            }
            out.out.extend_from_slice(b"{\"action\":");
            text(out, &item.action);
            key!(out, "probability_multiplier");
            out.float(item.probability_multiplier)?;
            key!(out, "duration_multiplier");
            out.float(item.duration_multiplier)?;
            key!(out, "event_ids");
            text_list(out, &item.modifier_event_ids);
            key!(out, "assumption_ids");
            text_list(out, &item.assumption_ids);
            out.out.push(b'}');
        }
        out.out.extend_from_slice(b"]}");
    }
    out.out.push(b'}');
    Ok(())
}

/// `current_household_id`, written. A value it cannot write is Python's.
fn write_household(
    out: &mut Encoder,
    world: &Bound<'_, PyAny>,
    agent_id: &Bound<'_, PyString>,
    group_id: Option<&str>,
) -> PyResult<()> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    let presence = raw_dict_get(exact_dict(&table)?.as_any(), intern!(py, PRESENCE).as_any())?;
    if let Some(presence) = presence.filter(|presence| presence.is_instance_of::<PyDict>()) {
        let Some(people) = raw_dict_get(&presence, intern!(py, "people").as_any())? else {
            out.out.extend_from_slice(b"null");
            return Ok(());
        };
        if !people.is_instance_of::<PyDict>() {
            return unsupported("the presence's people are not a dict");
        }
        let Some(person) = raw_dict_get(&people, agent_id.as_any())? else {
            out.out.extend_from_slice(b"null");
            return Ok(());
        };
        if person.is_instance_of::<PyDict>() {
            match raw_dict_get(&person, intern!(py, "household_id").as_any())? {
                Some(household) if !household.is_none() => text(out, exact_str(&household)?),
                _ => out.out.extend_from_slice(b"null"),
            }
            return Ok(());
        }
    }
    option_text(out, group_id);
    Ok(())
}

/// What `_IndexedMetricSink.record_decision` appends, as JSON with the keys in that order.
fn decision_bytes(
    py: Python<'_>,
    sink: &Bound<'_, PyAny>,
    world: &Bound<'_, PyAny>,
    row: &ProposalRow,
    target_id: Option<&str>,
    next_decision_at: i64,
    observed: &ObservedRow,
) -> PyResult<Vec<u8>> {
    let rust = row.view.get();
    let data = rust.data();
    let agent_id = rust.key(py, row.index);
    let id = agent_id.to_str()?;
    let store = entities_of(world)?;
    let (zone_id, group_id) = EntityStore::with_rows(&store, |rows| {
        let Some(agent) = rows.agents.get(id) else {
            return unsupported("the person is not in the world");
        };
        Ok((agent.zone_id.clone(), agent.profile.group_id.clone()))
    })?;
    let policy_version = row.common.policy_version.bind(py);
    let sources = attribute(sink, intern!(py, "policy_sources"))?;
    let source = raw_dict_get(exact_dict(&sources)?.as_any(), policy_version)?;
    let hash = row.common.state_hash.bind(py);

    let mut out = Encoder { out: Vec::with_capacity(3072) };
    out.out.extend_from_slice(b"{\"record_kind\":\"actions\",\"minute\":");
    integer(&mut out, data.minute_index);
    key!(out, "agent_id");
    text(&mut out, id);
    key!(out, "household_id");
    write_household(&mut out, world, agent_id, group_id.as_deref())?;
    key!(out, "zone_id");
    text(&mut out, &zone_id);
    key!(out, "action");
    text(&mut out, row.outcome.action.value());
    key!(out, "target_id");
    option_text(&mut out, target_id);
    key!(out, "trigger");
    text(&mut out, TRIGGER);
    key!(out, "policy_version");
    text(&mut out, exact_str(policy_version)?);
    key!(out, "source_id");
    match &source {
        Some(source) if !source.is_none() => text(&mut out, exact_str(source)?),
        _ => out.out.extend_from_slice(b"null"),
    }
    key!(out, "observed_state_hash");
    text(&mut out, exact_str(hash)?);
    key!(out, "observed_state");
    let start = out.out.len();
    write_observed(&mut out, py, observed.view.get(), observed.index)?;
    if out.out.len() - start > MAX_OBSERVED_BYTES {
        return unsupported("the observed state is long");
    }
    out.out.extend_from_slice(b",\"observed_state_status\":\"recorded\",\"candidates\":[");
    let modifiers = &data.zone_of(&data.agents[row.index]).demand_modifiers;
    for (place, candidate) in row.outcome.candidates.iter().enumerate() {
        if place > 0 {
            out.out.push(b',');
        }
        out.out.extend_from_slice(b"{\"action\":");
        text(&mut out, candidate.action.value());
        out.out.extend_from_slice(if candidate.eligible { b",\"eligible\":true" } else { b",\"eligible\":false" });
        key!(out, "trigger");
        let trigger = row.common.triggers.of(py, candidate.action, demand_of(modifiers, candidate.action))?;
        text(&mut out, trigger.to_str()?);
        key!(out, "utility");
        out.float(candidate.utility)?;
        key!(out, "probability");
        out.float(candidate.probability)?;
        key!(out, "urgency_priority");
        integer(&mut out, candidate.priority);
        out.out.push(b'}');
    }
    out.out.extend_from_slice(b"],\"candidate_count\":");
    integer(&mut out, row.outcome.candidates.len() as i64);
    out.out.extend_from_slice(b",\"candidates_truncated\":false,\"next_decision_at\":");
    integer(&mut out, next_decision_at);
    out.out.extend_from_slice(
        b",\"status\":\"selected_proposal\",\"execution_evidence\":\
          \"consult event history; selection is not proof of completion\"}",
    );
    Ok(out.out)
}

// ---- The step ----------------------------------------------------------------------------------

/// `_record_outcome`.
#[pyfunction]
#[pyo3(signature = (controller, proposal, *, target_id, next_decision_at, committed, observed_state))]
fn record_outcome<'py>(
    controller: &Bound<'py, PyAny>,
    proposal: &Bound<'py, PyAny>,
    target_id: &Bound<'py, PyAny>,
    next_decision_at: &Bound<'py, PyAny>,
    committed: &Bound<'py, PyAny>,
    observed_state: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = controller.py();
    let Ok(row) = proposal.downcast_exact::<ProposalRow>() else {
        return unsupported("the proposal is a Python object");
    };
    let row = row.get();
    let Ok(committed) = committed.downcast_exact::<PyBool>() else {
        return unsupported("committed is not a bool");
    };
    let committed = committed.is_true();
    let next = exact_int(next_decision_at)?;
    let data = row.view.get().data();
    if next <= data.minute_index {
        return unsupported("the outcome's timing is for Python to refuse");
    }
    let target = if target_id.is_none() { None } else { Some(exact_str(target_id)?) };
    let world = attribute(controller, intern!(py, "world"))?;
    let active = attribute(controller, intern!(py, "active_actions"))?;
    let Ok(active) = active.downcast::<ActiveActions>() else {
        return unsupported("the open actions are not the store");
    };
    let table = attribute(&world, intern!(py, "model_state"))?;
    let remembers = raw_dict_get(exact_dict(&table)?.as_any(), intern!(py, AGENT_DYNAMICS).as_any())?.is_some();

    // The decision's record. All of this reads, so a refusal here runs the Python step.
    match sink_of(controller)? {
        Sink::Python => return unsupported("the sink is Python's, or the run keeps a trace"),
        Sink::Silent => {}
        Sink::Indexed(sink) => {
            let Ok(observed) = observed_state.downcast_exact::<ObservedRow>() else {
                return unsupported("the observed state is a Python object");
            };
            if !attribute(&sink, intern!(py, "world"))?.is(&world) {
                return unsupported("the sink reads another world");
            }
            let observations = attribute(&sink, intern!(py, "observations"))?;
            exact_list(&observations)?;
            let raw = decision_bytes(py, &sink, &world, row, target, next, observed.get())?;
            raw_list_append(&observations, PyBytes::new(py, &raw).as_any())?;
        }
    }

    let action = row.outcome.action.value();
    if committed {
        let lifecycle = Proposal { agent_id: row.view.get().key(py, row.index).to_str()?, action, target_id: target, minute: data.minute_index, trigger: TRIGGER };
        if let Err(error) = commit_action(&world, active, &lifecycle) {
            if !error.is_instance_of::<Unsupported>(py) {
                return Err(error);
            }
            let options = PyDict::new(py);
            options.set_item(intern!(py, "target_id"), target_id)?;
            options.set_item(intern!(py, "committed"), true)?;
            options.set_item(intern!(py, "active_actions"), active)?;
            phase_rule(py, "record_action_proposal")?
                .call((&world, row.recorded(py, target_id, next_decision_at)?), Some(&options))?;
        }
    }
    if remembers {
        let decision = Decision {
            agent_id: row.view.get().key(py, row.index).to_str()?,
            action,
            target_id: target,
            minute: data.minute_index,
            next_decision_at: next,
            trigger: Some(TRIGGER),
        };
        if let Err(error) = remember_decision(&world, &decision) {
            if !error.is_instance_of::<Unsupported>(py) {
                return Err(error);
            }
            let options = PyDict::new(py);
            options.set_item(intern!(py, "target_id"), target_id)?;
            phase_rule(py, "record_agent_proposal")?
                .call((&world, row.recorded(py, target_id, next_decision_at)?), Some(&options))?;
        }
    }
    Ok(())
}

/// A rule as the propose phase's module has it, for what the kernels leave to Python.
fn phase_rule<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import("sheltersim.experiments.phases.propose")?.getattr(name)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<ProposalRow>()?;
    m.add_class::<ObservedRow>()?;
    m.add_function(wrap_pyfunction!(propose_rows, m)?)?;
    m.add_function(wrap_pyfunction!(observed_row, m)?)?;
    m.add_function(wrap_pyfunction!(record_outcome, m)?)?;
    Ok(())
}
