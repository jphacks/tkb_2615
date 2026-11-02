//! The trace documents of a Run, with their shape unchanged (docs/rust-port/step4-plan.md, D1):
//! `AgentPsychologyTrace` and `PsychologyUpdateResult.trace_document` (`policies/psychology.py`),
//! `SocialPropagationResult.trace_document` (`policies/social.py`) and
//! `InformationSpreadResult.trace_document` (`policies/information.py`). `_record_psychology_trace`
//! of the update phase is here too. A trace does not feed back into the state and draws nothing.
//!
//! `TraceSink` hands a finished document to the sink's own `record_*` method. The writer stays in
//! Python (`inspection/traces.py`): it holds the files, the spool, and `_sanitize`, which is not a
//! rule of the model and stays the one place that redacts what a Run writes. So each document here is
//! the same Python dict, with the same keys in the same order, and the writer gives the same bytes.
//!
//! The kernels keep what a document needs in plain rows, and make the document only when a sink takes
//! it. `TraceRows.__iter__` gives the Python rule's `AgentPsychologyTrace` objects, for a step of
//! Python that reads them and for the comparisons of benchmarks/rule_shadow.

use std::cmp::Ordering;
use std::collections::HashMap;
use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyList, PyString, PyTuple};

use crate::contacts::Basis;
use crate::core::mathx::py_fsum;
use crate::core::row::{exact_float, exact_str, unsupported};
use crate::rules::world_view::{status_text, RustView, ViewAgent, ViewData};

/// How many a summary document keeps of each list.
const SUMMARY_ITEMS: usize = 20;

#[derive(Clone, Copy, PartialEq)]
pub(crate) enum Level {
    Off,
    Summary,
    Full,
}

impl Level {
    pub(crate) fn of(artifacts: &Bound<'_, PyAny>) -> PyResult<Level> {
        let py = artifacts.py();
        let level = artifacts.getattr(intern!(py, "trace_level"))?;
        Ok(match level.downcast::<PyString>()?.to_str()? {
            "off" => Level::Off,
            "summary" => Level::Summary,
            "full" => Level::Full,
            _ => return unsupported("a trace level this kernel does not know"),
        })
    }
}

/// Where a tick's documents go: the controller's `artifacts`, if its trace is on.
pub(crate) struct TraceSink<'py> {
    artifacts: Bound<'py, PyAny>,
    pub(crate) level: Level,
}

impl<'py> TraceSink<'py> {
    pub(crate) fn of(controller: &Bound<'py, PyAny>) -> PyResult<Option<TraceSink<'py>>> {
        let artifacts = controller.getattr(intern!(controller.py(), "artifacts"))?;
        let level = Level::of(&artifacts)?;
        Ok((level != Level::Off).then_some(TraceSink { artifacts, level }))
    }

    /// `maximum_contacts` of the sample phase's document.
    pub(crate) fn information_items(&self, contacts: usize) -> usize {
        if self.level == Level::Full {
            contacts
        } else {
            SUMMARY_ITEMS
        }
    }

    pub(crate) fn psychology(&self, document: &Bound<'py, PyDict>, contributions: &Bound<'py, PyList>) -> PyResult<()> {
        let py = document.py();
        let options = PyDict::new(py);
        options.set_item(intern!(py, "agent_contributions"), contributions)?;
        self.artifacts.call_method(intern!(py, "record_psychology_trace"), (document,), Some(&options)).map(|_| ())
    }

    pub(crate) fn social(&self, document: &Bound<'py, PyDict>) -> PyResult<()> {
        let py = document.py();
        self.artifacts.call_method1(intern!(py, "record_social_contact_trace"), (document,)).map(|_| ())
    }

    pub(crate) fn information(&self, document: &Bound<'py, PyDict>) -> PyResult<()> {
        let py = document.py();
        self.artifacts.call_method1(intern!(py, "record_information_spread_trace"), (document,)).map(|_| ())
    }
}

fn texts<'py>(py: Python<'py>, items: &[Arc<str>]) -> PyResult<Bound<'py, PyList>> {
    PyList::new(py, items.iter().map(|item| &**item))
}

fn tuple_of<'py>(py: Python<'py>, items: &[Arc<str>]) -> PyResult<Bound<'py, PyTuple>> {
    PyTuple::new(py, items.iter().map(|item| &**item))
}

fn pairs<'py>(py: Python<'py>, items: &[(&'static str, f64)]) -> PyResult<Bound<'py, PyTuple>> {
    PyTuple::new(py, items.iter().copied())
}

// ---- A person's psychology -----------------------------------------------------------------------

/// What `AgentPsychologyTrace` holds that the view doesn't.
pub(crate) struct PersonTrace {
    /// In the view's people.
    pub(crate) index: usize,
    /// The name, the value before and after.
    pub(crate) needs: Vec<(&'static str, f64, f64)>,
    pub(crate) stress_before: f64,
    pub(crate) stress_after: f64,
    pub(crate) highest_need: &'static str,
    pub(crate) highest_need_value: f64,
    pub(crate) need_pressure: f64,
    pub(crate) need_pressure_stress: f64,
    pub(crate) queue_wait_stress: f64,
    pub(crate) support_wait_stress: f64,
    pub(crate) recovery_stress: f64,
    pub(crate) environmental_stress: f64,
    pub(crate) natural_recovery_stress: f64,
    pub(crate) life_state_feedback_stress: f64,
    pub(crate) information_uncertainty_stress: f64,
    /// `life_state_before` and `life_state_after`, of the life-state models.
    pub(crate) life_state: Option<(LifeStateTrace, LifeStateTrace)>,
    pub(crate) components: Vec<(&'static str, f64)>,
    pub(crate) raw_stress_delta: f64,
    pub(crate) candidate_before_clip: f64,
    pub(crate) clipped_delta: f64,
}

/// The values of `LIFE_STATE_NAMES`.
pub(crate) type LifeStateTrace = [f64; 9];

const LIFE_STATE_NAMES: [&str; 9] = [
    "sleep_debt_minutes",
    "uncertainty",
    "reassurance",
    "discontent",
    "positive_affect",
    "negative_affect",
    "hygiene_need",
    "family_separation_pressure",
    "information_belief_pressure",
];

fn queue_source_of(agent: &ViewAgent) -> Option<&'static str> {
    match agent.queue_kind.as_deref() {
        Some("toilet_queue") => Some("ASM-TOILET-SERVICE-DURATION-V1"),
        Some("resource_service_queue") => Some("ASM-H02-RESOURCE-SERVICE-V1"),
        _ => None,
    }
}

/// `view.facilities[agent.queue_target_id]`, if it is there.
fn queue_facility<'a>(data: &'a ViewData, agent: &ViewAgent) -> Option<&'a crate::rules::world_view::ViewFacility> {
    let target = agent.queue_target_id.as_deref()?;
    let place = data.facilities.binary_search_by(|facility| (*facility.id).cmp(target)).ok()?;
    Some(&data.facilities[place])
}

/// `AgentPsychologyTrace.to_dict`.
fn person_document<'py>(
    py: Python<'py>,
    data: &ViewData,
    row: &PersonTrace,
    threshold: f64,
    stress_after: f64,
    social_delta: f64,
    source_id: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let agent = &data.agents[row.index];
    let zone = data.zone_of(agent);
    let facility = queue_facility(data, agent);
    let queue_source = queue_source_of(agent);
    let facility_events = facility.map_or(&[][..], |facility| &facility.facility_event_context_ids);

    let causes = PyList::empty(py);
    {
        let cause = PyDict::new(py);
        cause.set_item("kind", "unmet_need_pressure")?;
        cause.set_item("need", row.highest_need)?;
        cause.set_item("value", row.highest_need_value)?;
        cause.set_item("threshold", threshold)?;
        cause.set_item("excess_above_threshold", row.need_pressure)?;
        causes.append(cause)?;
    }
    if agent.queue_task_id.is_some() {
        let cause = PyDict::new(py);
        cause.set_item("kind", "facility_queue_wait")?;
        cause.set_item("queue_kind", agent.queue_kind.as_deref())?;
        cause.set_item("queue_task_id", agent.queue_task_id.as_deref())?;
        cause.set_item("source_id", queue_source)?;
        cause.set_item("facility_id", agent.queue_target_id.as_deref())?;
        cause.set_item("wait_minutes", agent.queue_wait_minutes)?;
        cause.set_item("facility_capacity", facility.map(|facility| facility.capacity))?;
        cause.set_item("facility_working_units", facility.map(|facility| facility.working_units))?;
        cause.set_item("capacity_event_context_ids", texts(py, facility_events)?)?;
        causes.append(cause)?;
    }
    if agent.support_request_id.is_some() {
        let cause = PyDict::new(py);
        cause.set_item("kind", "staff_support_wait")?;
        cause.set_item("support_request_id", agent.support_request_id.as_deref())?;
        cause.set_item("status", agent.support_request_status.map(status_text))?;
        cause.set_item("assigned_staff_id", agent.support_assigned_staff_id.as_deref())?;
        cause.set_item("wait_minutes", agent.support_wait_minutes)?;
        causes.append(cause)?;
    }
    let environmental = row.environmental_stress > 0.0;
    if environmental {
        let cause = PyDict::new(py);
        cause.set_item("kind", "experimental_environmental_stress")?;
        cause.set_item("stress_per_minute", row.environmental_stress)?;
        cause.set_item("stressor_event_ids", texts(py, &zone.environmental_stressor_event_ids)?)?;
        cause.set_item("assumption_source_id", zone.environmental_assumption_source_id.as_deref())?;
        causes.append(cause)?;
    }
    let sources = &agent.misinformation_effect_sources;
    let belief_pressure = agent.misinformation_belief_pressure;
    if !sources.is_empty() || belief_pressure > 0.0 {
        let cause = PyDict::new(py);
        cause.set_item("kind", "active_false_claim_belief")?;
        cause.set_item("pressure", belief_pressure)?;
        cause.set_item("message_ids", PyList::new(py, sources.iter().map(|item| &*item.message_id))?)?;
        cause.set_item("origin_event_ids", PyList::new(py, sources.iter().map(|item| &*item.origin_event_id))?)?;
        cause.set_item("belief_event_ids", PyList::new(py, sources.iter().map(|item| &*item.last_event_id))?)?;
        cause.set_item("source_ids", PyList::new(py, sources.iter().map(|item| &*item.source_id))?)?;
        causes.append(cause)?;
    }
    let noise_events = &zone.zone_environment_modifier_event_ids;
    if !noise_events.is_empty() {
        let cause = PyDict::new(py);
        cause.set_item("kind", "experimental_zone_noise_reduced_rest_efficiency")?;
        cause.set_item("ambient_noise_level", zone.ambient_noise_level)?;
        cause.set_item("rest_efficiency_multiplier", zone.rest_efficiency_multiplier)?;
        cause.set_item("modifier_event_ids", texts(py, noise_events)?)?;
        cause.set_item("assumption_ids", texts(py, &zone.zone_environment_assumption_ids)?)?;
        causes.append(cause)?;
    }

    let components = PyDict::new(py);
    for &(name, amount) in &row.components {
        components.set_item(name, amount)?;
    }
    if row.life_state_feedback_stress != 0.0 {
        components.set_item("life_state_feedback", row.life_state_feedback_stress)?;
    }
    if row.information_uncertainty_stress != 0.0 {
        components.set_item("information_belief_via_uncertainty", row.information_uncertainty_stress)?;
    }
    if environmental {
        components.set_item("experimental_environmental_stress", row.environmental_stress)?;
    }

    let references = PyDict::new(py);
    references.set_item("psychology_assumption_id", source_id)?;
    references.set_item("queue_task_id", agent.queue_task_id.as_deref())?;
    references.set_item("queue_source_id", queue_source)?;
    references.set_item("facility_event_context_ids", texts(py, facility_events)?)?;
    references.set_item("support_request_id", agent.support_request_id.as_deref())?;
    references.set_item(
        "support_source_id",
        agent.support_request_id.as_ref().map(|_| "ASM-STAFF-SUPPORT-LIFECYCLE-V1"),
    )?;
    if environmental {
        references.set_item("environmental_assumption_id", zone.environmental_assumption_source_id.as_deref())?;
        references.set_item("environmental_stressor_event_ids", texts(py, &zone.environmental_stressor_event_ids)?)?;
    }

    let before_after = |before: f64, after: f64| -> PyResult<Bound<'py, PyDict>> {
        let item = PyDict::new(py);
        item.set_item("before", before)?;
        item.set_item("after", after)?;
        item.set_item("delta", after - before)?;
        Ok(item)
    };

    let needs = PyDict::new(py);
    for &(name, before, after) in &row.needs {
        needs.set_item(name, before_after(before, after)?)?;
    }
    let stress = PyDict::new(py);
    stress.set_item("before", row.stress_before)?;
    stress.set_item("after", stress_after)?;
    stress.set_item("applied_delta", stress_after - row.stress_before)?;
    stress.set_item("raw_delta", row.raw_stress_delta)?;
    stress.set_item("candidate_before_clip", row.candidate_before_clip)?;
    stress.set_item("psychology_clipped_delta", row.clipped_delta)?;
    stress.set_item("social_delta", social_delta)?;
    stress.set_item("clip_delta", row.clipped_delta - row.raw_stress_delta)?;
    stress.set_item("components", components)?;

    let document = PyDict::new(py);
    document.set_item("agent_id", &*agent.id)?;
    document.set_item("needs", needs)?;
    document.set_item("stress", stress)?;
    document.set_item("direct_causes", causes)?;
    document.set_item("source_references", references)?;
    if !noise_events.is_empty() {
        let item = PyDict::new(py);
        item.set_item("ambient_noise_level", zone.ambient_noise_level)?;
        item.set_item("rest_efficiency_multiplier", zone.rest_efficiency_multiplier)?;
        item.set_item("modifier_event_ids", texts(py, noise_events)?)?;
        item.set_item("assumption_ids", texts(py, &zone.zone_environment_assumption_ids)?)?;
        item.set_item("source_type", "assumption")?;
        document.set_item("zone_environment", item)?;
    }
    if !zone.condition_modifier_event_ids.is_empty() || !zone.condition_assumption_ids.is_empty() {
        let item = PyDict::new(py);
        item.set_item("ventilation_effectiveness", zone.ventilation_effectiveness)?;
        item.set_item("lighting_level", zone.lighting_level)?;
        item.set_item("privacy_level", zone.privacy_level)?;
        item.set_item("temperature_c", zone.temperature_c)?;
        item.set_item("modifier_event_ids", texts(py, &zone.condition_modifier_event_ids)?)?;
        item.set_item("assumption_ids", texts(py, &zone.condition_assumption_ids)?)?;
        item.set_item("source_type", "assumption")?;
        document.set_item("environment_conditions", item)?;
    }
    if let Some((before, after)) = &row.life_state {
        let item = PyDict::new(py);
        for (place, name) in LIFE_STATE_NAMES.into_iter().enumerate() {
            item.set_item(name, before_after(before[place], after[place])?)?;
        }
        document.set_item("life_state", item)?;
    }
    if !sources.is_empty() || belief_pressure != 0.0 {
        let records = PyList::empty(py);
        for source in sources {
            let record = PyDict::new(py);
            record.set_item("message_id", &*source.message_id)?;
            record.set_item("origin_event_id", &*source.origin_event_id)?;
            record.set_item("belief_event_id", &*source.last_event_id)?;
            record.set_item("source_id", &*source.source_id)?;
            record.set_item("origin_minute", source.time)?;
            records.append(record)?;
        }
        let item = PyDict::new(py);
        item.set_item("active_false_claim_pressure", belief_pressure)?;
        item.set_item("source_records", records)?;
        item.set_item("effect_kind", "persistent_belief_state_input")?;
        document.set_item("information_belief_effect", item)?;
    }
    Ok(document)
}

// ---- A minute's psychology -----------------------------------------------------------------------

/// `PsychologyUpdateResult.traces` of a kernel: rows, with the objects made if something asks.
#[pyclass(module = "sheltersim_native", frozen)]
pub struct TraceRows {
    view: Py<RustView>,
    threshold: f64,
    rows: Vec<PersonTrace>,
}

impl TraceRows {
    pub(crate) fn new(view: Py<RustView>, threshold: f64, rows: Vec<PersonTrace>) -> TraceRows {
        TraceRows { view, threshold, rows }
    }

    fn object<'py>(&self, py: Python<'py>, class: &Bound<'py, PyAny>, row: &PersonTrace) -> PyResult<Bound<'py, PyAny>> {
        let view = self.view.get();
        let data = view.data();
        let agent = &data.agents[row.index];
        let zone = data.zone_of(agent);
        let facility = queue_facility(data, agent);
        let before: Vec<(&str, f64)> = row.needs.iter().map(|&(name, before, _)| (name, before)).collect();
        let after: Vec<(&str, f64)> = row.needs.iter().map(|&(name, _, after)| (name, after)).collect();
        let fields = PyDict::new(py);
        fields.set_item("agent_id", view.key(py, row.index))?;
        fields.set_item("needs_before", PyTuple::new(py, before)?)?;
        fields.set_item("needs_after", PyTuple::new(py, after)?)?;
        fields.set_item("stress_before", row.stress_before)?;
        fields.set_item("stress_after", row.stress_after)?;
        fields.set_item("highest_need", row.highest_need)?;
        fields.set_item("highest_need_value", row.highest_need_value)?;
        fields.set_item("need_pressure_threshold", self.threshold)?;
        fields.set_item("need_pressure", row.need_pressure)?;
        fields.set_item("need_pressure_stress", row.need_pressure_stress)?;
        fields.set_item("queue_wait_stress", row.queue_wait_stress)?;
        fields.set_item("support_wait_stress", row.support_wait_stress)?;
        fields.set_item("recovery_stress", row.recovery_stress)?;
        fields.set_item("queue_task_id", agent.queue_task_id.as_deref())?;
        fields.set_item("queue_kind", agent.queue_kind.as_deref())?;
        fields.set_item("queue_target_id", agent.queue_target_id.as_deref())?;
        fields.set_item("queue_source_id", queue_source_of(agent))?;
        fields.set_item("queue_wait_minutes", agent.queue_wait_minutes)?;
        fields.set_item("queue_facility_capacity", facility.map(|facility| facility.capacity))?;
        fields.set_item("queue_facility_working_units", facility.map(|facility| facility.working_units))?;
        fields.set_item(
            "facility_event_context_ids",
            tuple_of(py, facility.map_or(&[][..], |facility| &facility.facility_event_context_ids))?,
        )?;
        fields.set_item("support_request_id", agent.support_request_id.as_deref())?;
        fields.set_item("support_request_status", agent.support_request_status.map(status_text))?;
        fields.set_item("support_assigned_staff_id", agent.support_assigned_staff_id.as_deref())?;
        fields.set_item("support_wait_minutes", agent.support_wait_minutes)?;
        fields.set_item("environmental_stress", row.environmental_stress)?;
        fields.set_item("environmental_stressor_event_ids", tuple_of(py, &zone.environmental_stressor_event_ids)?)?;
        fields.set_item("environmental_assumption_source_id", zone.environmental_assumption_source_id.as_deref())?;
        fields.set_item("information_uncertainty_stress", row.information_uncertainty_stress)?;
        fields.set_item("ambient_noise_level", zone.ambient_noise_level)?;
        fields.set_item("rest_efficiency_multiplier", zone.rest_efficiency_multiplier)?;
        fields.set_item("zone_environment_modifier_event_ids", tuple_of(py, &zone.zone_environment_modifier_event_ids)?)?;
        fields.set_item("zone_environment_assumption_ids", tuple_of(py, &zone.zone_environment_assumption_ids)?)?;
        fields.set_item("ventilation_effectiveness", zone.ventilation_effectiveness)?;
        fields.set_item("lighting_level", zone.lighting_level)?;
        fields.set_item("privacy_level", zone.privacy_level)?;
        fields.set_item("temperature_c", zone.temperature_c)?;
        fields.set_item("condition_modifier_event_ids", tuple_of(py, &zone.condition_modifier_event_ids)?)?;
        fields.set_item("condition_assumption_ids", tuple_of(py, &zone.condition_assumption_ids)?)?;
        fields.set_item("life_state_feedback_stress", row.life_state_feedback_stress)?;
        fields.set_item("natural_recovery_stress", row.natural_recovery_stress)?;
        if let Some((before, after)) = &row.life_state {
            let named = |values: &LifeStateTrace| {
                PyTuple::new(py, LIFE_STATE_NAMES.iter().copied().zip(values.iter().copied()))
            };
            fields.set_item("life_state_before", named(before)?)?;
            fields.set_item("life_state_after", named(after)?)?;
        }
        fields.set_item("information_belief_pressure", agent.misinformation_belief_pressure)?;
        let mut sources = Vec::with_capacity(agent.misinformation_effect_sources.len());
        for source in &agent.misinformation_effect_sources {
            sources.push((
                &*source.message_id,
                &*source.origin_event_id,
                &*source.last_event_id,
                &*source.source_id,
                source.time,
            ));
        }
        fields.set_item("information_effect_sources", PyTuple::new(py, sources)?)?;
        fields.set_item("component_contributions", pairs(py, &row.components)?)?;
        fields.set_item("raw_stress_delta", row.raw_stress_delta)?;
        fields.set_item("stress_candidate_before_clip", row.candidate_before_clip)?;
        fields.set_item("psychology_clipped_delta", row.clipped_delta)?;
        class.call((), Some(&fields))
    }
}

#[pymethods]
impl TraceRows {
    fn __len__(&self) -> usize {
        self.rows.len()
    }

    /// The Python rule's objects, as `explain_update` gives them.
    fn __iter__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let class = py.import("sheltersim.policies.psychology")?.getattr("AgentPsychologyTrace")?;
        let mut objects = Vec::with_capacity(self.rows.len());
        for row in &self.rows {
            objects.push(self.object(py, &class, row)?);
        }
        PyTuple::new(py, objects)?.try_iter().map(Bound::into_any)
    }
}

/// `math.fsum` of one number of each person.
fn total(rows: &[PersonTrace], value: impl Fn(&PersonTrace) -> f64) -> PyResult<f64> {
    let values: Vec<f64> = rows.iter().map(value).collect();
    py_fsum(&values).ok_or_else(|| crate::Unsupported::new_err("a total of the trace is not finite"))
}

/// `_record_psychology_trace` of the update phase. It changes nothing but what the sink keeps.
#[pyfunction]
pub(crate) fn record_psychology_trace<'py>(
    controller: &Bound<'py, PyAny>,
    result: &Bound<'py, PyAny>,
    final_deltas: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = controller.py();
    let Some(sink) = TraceSink::of(controller)? else {
        return Ok(());
    };
    let traces = result.getattr(intern!(py, "traces"))?;
    let Ok(rows) = traces.downcast_exact::<TraceRows>() else {
        return unsupported("the traces are the Python rule's");
    };
    let rows = rows.get();
    let view = rows.view.get();
    let data = view.data();

    // `final_by_agent`. Base stress is each row's own: the psychology delta holds the same value.
    let mut finals: HashMap<String, f64> = HashMap::with_capacity(rows.rows.len());
    for delta in final_deltas.try_iter()? {
        let delta = delta?;
        let id = delta.getattr(intern!(py, "agent_id"))?;
        finals.insert(exact_str(&id)?.to_owned(), exact_float(&delta.getattr(intern!(py, "stress"))?)?);
    }
    let mut stress_after = Vec::with_capacity(rows.rows.len());
    for row in &rows.rows {
        match finals.get(&*data.agents[row.index].id) {
            Some(stress) => stress_after.push(*stress),
            None => return unsupported("a person has no final delta"),
        }
    }

    // `trace_document`, on `traced_result`. The order: most changed first, then by ID.
    let mut order: Vec<usize> = (0..rows.rows.len()).collect();
    let change = |place: usize| -(stress_after[place] - rows.rows[place].stress_before).abs();
    order.sort_by(|&first, &second| {
        change(first).partial_cmp(&change(second)).unwrap_or(Ordering::Equal).then_with(|| {
            data.agents[rows.rows[first].index].id.as_bytes().cmp(data.agents[rows.rows[second].index].id.as_bytes())
        })
    });
    let kept = if sink.level == Level::Full { order.len() } else { order.len().min(SUMMARY_ITEMS) };

    let totals = PyDict::new(py);
    totals.set_item("unmet_need_pressure", total(&rows.rows, |row| row.need_pressure_stress)?)?;
    totals.set_item("facility_queue_wait", total(&rows.rows, |row| row.queue_wait_stress)?)?;
    totals.set_item("staff_support_wait", total(&rows.rows, |row| row.support_wait_stress)?)?;
    totals.set_item("recovery", total(&rows.rows, |row| row.recovery_stress)?)?;
    totals.set_item("natural_recovery", total(&rows.rows, |row| row.natural_recovery_stress)?)?;
    let applied: Vec<f64> =
        rows.rows.iter().zip(&stress_after).map(|(row, after)| after - row.stress_before).collect();
    let applied = py_fsum(&applied).ok_or_else(|| crate::Unsupported::new_err("a total of the trace is not finite"))?;
    totals.set_item("applied_stress_delta", applied)?;
    let environmental = total(&rows.rows, |row| row.environmental_stress)?;
    if environmental != 0.0 {
        totals.set_item("experimental_environmental_stress", environmental)?;
    }
    let feedback = total(&rows.rows, |row| row.life_state_feedback_stress)?;
    if feedback != 0.0 {
        totals.set_item("life_state_feedback", feedback)?;
    }

    let policy = controller.getattr(intern!(py, "psychology"))?;
    let source_id = policy.getattr(intern!(py, "source_id"))?;
    let agents = PyList::empty(py);
    for &place in &order[..kept] {
        let row = &rows.rows[place];
        agents.append(person_document(
            py,
            data,
            row,
            rows.threshold,
            stress_after[place],
            stress_after[place] - row.stress_after,
            &source_id,
        )?)?;
    }
    let document = PyDict::new(py);
    document.set_item("minute_index", minute)?;
    document.set_item("policy_version", result.getattr(intern!(py, "policy_version"))?)?;
    document.set_item("assumption_id", &source_id)?;
    document.set_item("social_propagation_is_separate", true)?;
    document.set_item("agent_count", rows.rows.len())?;
    document.set_item("truncated_agent_count", rows.rows.len() - kept)?;
    document.set_item("component_totals", totals)?;
    document.set_item("agents", agents)?;

    // `agent_contributions`.
    let contributions = PyList::empty(py);
    for (row, after) in rows.rows.iter().zip(&stress_after) {
        let item = PyDict::new(py);
        item.set_item("agent_id", view.key(py, row.index))?;
        item.set_item("stress_before", row.stress_before)?;
        item.set_item("stress_after", *after)?;
        item.set_item("raw_stress_delta", row.raw_stress_delta)?;
        item.set_item("social_stress_delta", after - row.stress_after)?;
        let components = PyDict::new(py);
        for &(name, amount) in &row.components {
            components.set_item(name, amount)?;
        }
        item.set_item("component_contributions", components)?;
        contributions.append(item)?;
    }
    sink.psychology(&document, &contributions)
}

// ---- Contacts ------------------------------------------------------------------------------------

/// `SocialContactTrace`.
pub(crate) struct SocialContact {
    pub(crate) recipient: usize,
    pub(crate) source: usize,
    pub(crate) basis: Basis,
    pub(crate) minutes: i64,
    pub(crate) cooldown_remaining: i64,
    pub(crate) difference: f64,
    pub(crate) contribution: f64,
    pub(crate) weight: f64,
    pub(crate) aggregate_rate: f64,
    pub(crate) pre_pair_cap: f64,
    pub(crate) pair_cap: f64,
    pub(crate) aggregate_scale: f64,
}

/// `contact_basis` of `_contact_buckets`.
fn social_basis(basis: Basis) -> &'static str {
    match basis {
        Basis::Family => "family",
        Basis::SameZone => "same_zone",
        Basis::Nearby => "nearby_zone",
    }
}

/// `SocialPropagationResult.trace_document`, with the bounded sample of contacts it has always had.
pub(crate) fn social_document<'py>(
    py: Python<'py>,
    minute: &Bound<'py, PyAny>,
    policy_version: &Bound<'py, PyAny>,
    ids: &[&str],
    contacts: &[SocialContact],
    contributing: i64,
    total_added: f64,
) -> PyResult<Bound<'py, PyDict>> {
    let contact_of = |contact: &SocialContact| -> PyResult<Bound<'py, PyDict>> {
        let item = PyDict::new(py);
        item.set_item("recipient_id", ids[contact.recipient])?;
        item.set_item("source_id", ids[contact.source])?;
        item.set_item("contact_basis", social_basis(contact.basis))?;
        item.set_item("consecutive_contact_minutes", contact.minutes)?;
        item.set_item("cooldown_remaining_minutes", contact.cooldown_remaining)?;
        item.set_item("stress_difference", contact.difference)?;
        item.set_item("stress_contribution", contact.contribution)?;
        item.set_item("influence_weight", contact.weight)?;
        item.set_item("aggregate_rate", contact.aggregate_rate)?;
        item.set_item("pre_pair_cap_contribution", contact.pre_pair_cap)?;
        item.set_item("pair_cap_contribution", contact.pair_cap)?;
        item.set_item("aggregate_scale", contact.aggregate_scale)?;
        Ok(item)
    };
    let kept = contacts.len().min(SUMMARY_ITEMS);
    let sample = PyList::empty(py);
    for contact in &contacts[..kept] {
        sample.append(contact_of(contact)?)?;
    }
    let contributions = PyList::empty(py);
    for contact in contacts.iter().filter(|contact| contact.contribution != 0.0) {
        contributions.append(contact_of(contact)?)?;
    }
    let document = PyDict::new(py);
    document.set_item("minute_index", minute)?;
    document.set_item("policy_version", policy_version)?;
    document.set_item("selected_contact_count", contacts.len())?;
    document.set_item("contributing_contact_count", contributing)?;
    document.set_item("total_stress_added", PyFloat::new(py, total_added))?;
    document.set_item(
        "aggregation",
        if policy_version.eq(intern!(py, "expressiveness-channel-openness-v1"))? {
            "snapshot weighted synchrony; pair cap then recipient cap then [0,1] bound"
        } else {
            "legacy directed positive stress propagation"
        },
    )?;
    document.set_item("truncated_contact_count", contacts.len() - kept)?;
    document.set_item("contacts", sample)?;
    document.set_item("contributions", contributions)?;
    Ok(document)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<TraceRows>()?;
    m.add_function(wrap_pyfunction!(record_psychology_trace, m)?)?;
    Ok(())
}
