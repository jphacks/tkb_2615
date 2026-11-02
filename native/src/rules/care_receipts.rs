//! `domain/care_receipts.py`: a care transition that has happened, as its event and its receipt.
//! One call plans with nothing changed and then writes; no pydantic model and no Python record are
//! made. The Python rule stays as the comparison base and says why in its own text for whatever is
//! refused here (docs/rust-port/lanes/m6-handlers.md).

use std::sync::{Arc, LazyLock};

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

use crate::core::row::{attribute, exact_int, exact_str, namespace_is_absent, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::core::world_core::{mirror_of, NamespaceMirror};
use crate::rules::common_operations::{plan_record, write_record, CareRecord, PlannedRecord};
use crate::rules::handlers::{assumption, log_record, new_event, status_member, NewEvent};
use crate::state::events::{EventKind, EventRow, EventStatus, EventWriter};
use crate::state::shared_operations::{is_identifier, is_support_kind, CareStatus, SharedOperationsRow, NAME};
use crate::Unsupported;

const SOURCE: &str = "ASM-CARE-TRANSITION-PRODUCER-V1";
const POLICY: &str = "applied-care-transition-v1";
const LIFECYCLE_SOURCE: &str = "ASM-STAFF-SUPPORT-LIFECYCLE-V1";

struct Names {
    care: Arc<str>,
    event_type: Arc<str>,
    not_applicable: Arc<str>,
    source_type: Arc<str>,
    source_id: Arc<str>,
    policy_version: Arc<str>,
    assumption: Arc<str>,
    source: Arc<str>,
    policy: Arc<str>,
    lifecycle_source: Arc<str>,
}

static NAMES: LazyLock<Names> = LazyLock::new(|| Names {
    care: Arc::from("care"),
    event_type: Arc::from("care_support_transition"),
    not_applicable: Arc::from("not_applicable"),
    source_type: Arc::from("source_type"),
    source_id: Arc::from("source_id"),
    policy_version: Arc::from("policy_version"),
    assumption: Arc::from("assumption"),
    source: Arc::from(SOURCE),
    policy: Arc::from(POLICY),
    lifecycle_source: Arc::from(LIFECYCLE_SOURCE),
});

/// `tuple(sorted({...}))`: bytes of UTF-8 sort as code points do.
fn sorted_unique(mut items: Vec<Arc<str>>) -> Vec<Arc<str>> {
    items.sort_unstable();
    items.dedup();
    items
}

/// What a transition says, as the arguments of `append_applied_care_transition`.
pub(crate) struct Transition {
    pub(crate) request_id: Arc<str>,
    pub(crate) agent_id: Arc<str>,
    pub(crate) household_id: Option<Arc<str>>,
    pub(crate) support_kind: Arc<str>,
    pub(crate) status: &'static str,
    pub(crate) staff_id: Option<Arc<str>>,
    pub(crate) causation_id: Option<Arc<str>>,
    pub(crate) source_ids: Vec<Arc<str>>,
    /// `outcome_observation` and `state_changes`, of a completed one.
    pub(crate) outcome: Option<(&'static str, V)>,
}

fn status_of(text: &str) -> PyResult<(&'static str, CareStatus)> {
    let name = match text {
        "requested" => "requested",
        "assigned" => "assigned",
        "started" => "started",
        "completed" => "completed",
        "interrupted" => "interrupted",
        "declined" => "declined",
        _ => return unsupported("an unknown care status"),
    };
    let Some(status) = CareStatus::parse(name) else {
        return unsupported("an unknown care status");
    };
    Ok((name, status))
}

/// A transition planned: everything below changes the world and cannot refuse.
pub(crate) struct PlannedTransition {
    record: CareRecord,
    document: V,
    row: EventRow,
    plan: PlannedRecord,
    detail: String,
}

/// `make_care_transition_record` and the checks of `record_shared_operation`, with `sequence` for
/// what `allocate_event_sequence` will give then.
pub(crate) fn plan_transition(
    world: &Bound<'_, PyAny>,
    writer: &EventWriter<'_>,
    transition: Transition,
    sequence: i64,
) -> PyResult<PlannedTransition> {
    let py = world.py();
    let names = &*NAMES;
    let Transition { request_id, agent_id, household_id, support_kind, status, staff_id, causation_id, source_ids, outcome } = transition;
    let (status_text, care_status) = status_of(status)?;
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let optional_ok = |value: &Option<Arc<str>>| value.as_deref().is_none_or(is_identifier);
    if minute < 0
        || !is_identifier(&request_id)
        || !is_identifier(&agent_id)
        || !optional_ok(&household_id)
        || !optional_ok(&staff_id)
        || !optional_ok(&causation_id)
        || !is_support_kind(&support_kind)
    {
        return unsupported("a value the receipt's contract is for the Python rule to judge");
    }
    let mut sources = source_ids;
    if !sources.iter().all(|source| is_identifier(source)) {
        return unsupported("a source the receipt's contract is for the Python rule to judge");
    }
    sources.push(names.source.clone());
    let sources = sorted_unique(sources);
    let event_id: Arc<str> = Arc::from(format!("care-support-transition-{sequence}"));
    {
        let log = writer.log().borrow();
        if log.has_event_id(&event_id) || log.has_sequence(sequence) {
            return unsupported("the event's identity is taken");
        }
    }
    let (observation, changes) = match outcome {
        Some((observation, changes)) => (Arc::from(observation), changes),
        None => (names.not_applicable.clone(), V::List(Vec::new())),
    };
    let record = CareRecord {
        record_type: names.care.clone(),
        schema: 1,
        event_type: names.event_type.clone(),
        event_id: event_id.clone(),
        request_id,
        agent_id,
        household_id,
        support_kind,
        status_text: Arc::from(status_text),
        status: care_status,
        minute,
        staff_id,
        observation,
        changes,
        causation_id: causation_id.clone(),
        sources,
    };
    let document = record.document();
    let V::Map(payload) = document.clone() else {
        return unsupported("a receipt is a document");
    };
    let mut provenance = IndexMap::with_capacity(3);
    provenance.insert(names.source_type.clone(), V::Str(names.assumption.clone()));
    provenance.insert(names.source_id.clone(), V::Str(names.source.clone()));
    provenance.insert(names.policy_version.clone(), V::Str(names.policy.clone()));
    let Some(row) = EventRow::new(
        event_id,
        EventKind::Endogenous,
        minute,
        0,
        sequence,
        names.event_type.clone(),
        payload,
        Some(names.source.clone()),
        causation_id,
        provenance,
    ) else {
        return unsupported("the event's values are not what an event takes");
    };
    let mirror = mirror_of(world, NAME)?;
    let table = attribute(world, intern!(py, "model_state"))?;
    match crate::core::row::exact_dict(&table)?.get_item(NAME)? {
        Some(state) if mirror.try_borrow()?.root_is(&state) => {}
        _ => return unsupported("the world holds another document than the row's"),
    }
    let plan = plan_record(writer, &mirror, &record)?;
    let detail = format!("care transition {status_text} recorded after state application");
    Ok(PlannedTransition { record, document, row, plan, detail })
}

/// `allocate_event_sequence` for what `plan_transition` planned.
pub(crate) fn allocate_transition(writer: &EventWriter<'_>, planned: &PlannedTransition) -> PyResult<()> {
    let sequence = writer.allocate_sequence()?;
    if sequence != planned.row.sequence {
        return Err(pyo3::exceptions::PyRuntimeError::new_err("the event counter moved after the plan"));
    }
    Ok(())
}

/// The record of an allocated transition, and its receipt.
pub(crate) fn record_transition(world: &Bound<'_, PyAny>, writer: &EventWriter<'_>, planned: PlannedTransition) -> PyResult<()> {
    let py = world.py();
    let PlannedTransition { record, document, row, plan, detail } = planned;
    writer.record(&row, record.minute, EventStatus::Applied, Some(&detail))?;
    let mirror = mirror_of(world, NAME)?;
    write_record(py, &mirror, &record, &document, plan)
}

/// What `plan_transition` planned, with its sequence allocated now.
pub(crate) fn write_transition(world: &Bound<'_, PyAny>, writer: &EventWriter<'_>, planned: PlannedTransition) -> PyResult<()> {
    allocate_transition(writer, &planned)?;
    record_transition(world, writer, planned)
}

/// The record of an allocated transition as Python's object, which the dispatcher then appends and
/// projects. Nothing is written.
pub(crate) fn transition_record<'py>(py: Python<'py>, planned: &PlannedTransition) -> PyResult<Bound<'py, PyAny>> {
    let models = py.import(intern!(py, "sheltersim.events.models"))?;
    let applied = models.getattr(intern!(py, "EventStatus"))?.getattr(intern!(py, "APPLIED"))?;
    let kwargs = PyDict::new(py);
    kwargs.set_item(intern!(py, "event"), planned.row.to_py(py)?)?;
    kwargs.set_item(intern!(py, "realized_minute"), planned.record.minute)?;
    kwargs.set_item(intern!(py, "status"), applied)?;
    kwargs.set_item(intern!(py, "detail"), &planned.detail)?;
    models.getattr(intern!(py, "EventLogRecord"))?.call((), Some(&kwargs))
}

/// `append_applied_care_transition` once the namespace is known to be there.
pub(crate) fn append(world: &Bound<'_, PyAny>, transition: Transition) -> PyResult<()> {
    let writer = EventWriter::of(world)?;
    let planned = plan_transition(world, &writer, transition, writer.next_sequence()?)?;
    write_transition(world, &writer, planned)
}

fn optional_text(value: Option<&Bound<'_, PyAny>>) -> PyResult<Option<Arc<str>>> {
    match value {
        Some(value) if !value.is_none() => Ok(Some(Arc::from(exact_str(value)?))),
        _ => Ok(None),
    }
}

/// `append_applied_care_transition`. It returns nothing, as nobody reads its receipt.
#[pyfunction]
#[pyo3(signature = (world, *, request_id, agent_id, household_id, support_kind, status, staff_id=None, causation_id=None, source_ids=None))]
#[allow(clippy::too_many_arguments)]
fn append_transition<'py>(
    world: &Bound<'py, PyAny>,
    request_id: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
    household_id: Option<&Bound<'py, PyAny>>,
    support_kind: &Bound<'py, PyAny>,
    status: &Bound<'py, PyAny>,
    staff_id: Option<&Bound<'py, PyAny>>,
    causation_id: Option<&Bound<'py, PyAny>>,
    source_ids: Option<&Bound<'py, PyAny>>,
) -> PyResult<()> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        let table = attribute(world, intern!(py, "model_state"))?;
        if crate::core::row::exact_dict(&table)?.contains(intern!(py, NAME))? {
            return unsupported("the namespace is None");
        }
        return Ok(());
    }
    let mut sources = Vec::new();
    if let Some(source_ids) = source_ids {
        let source_ids = source_ids.downcast_exact::<PyTuple>().map_err(|_| Unsupported::new_err("the sources are not a tuple"))?;
        sources.reserve(source_ids.len() + 1);
        for source in source_ids.iter() {
            sources.push(Arc::from(exact_str(&source)?));
        }
    }
    let (status_text, _) = status_of(exact_str(status)?)?;
    append(
        world,
        Transition {
            request_id: Arc::from(exact_str(request_id)?),
            agent_id: Arc::from(exact_str(agent_id)?),
            household_id: optional_text(household_id)?,
            support_kind: Arc::from(exact_str(support_kind)?),
            status: status_text,
            staff_id: optional_text(staff_id)?,
            causation_id: optional_text(causation_id)?,
            source_ids: sources,
            outcome: None,
        },
    )
}

/// `isinstance(value, dict)`, as `population_context_people` asks. A kind of dict that can't be read
/// directly is the Python rule's.
fn dict_of<'a, 'py>(value: &'a Bound<'py, PyAny>) -> PyResult<Option<&'a Bound<'py, PyDict>>> {
    match as_readable_dict(value) {
        Some(dict) => Ok(Some(dict)),
        None if value.is_instance_of::<PyDict>() => unsupported("another kind of dict"),
        None => Ok(None),
    }
}

/// `population_context_people(world).get(agent_id)`, without merging the three: the last that has
/// the person wins.
pub(crate) fn person_context<'py>(world: &Bound<'py, PyAny>, agent_id: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = world.py();
    let key = PyString::new(py, agent_id);
    let in_part = |part: Option<Bound<'py, PyAny>>, name: &Bound<'py, PyString>| -> PyResult<Option<Bound<'py, PyAny>>> {
        let Some(part) = part else {
            return Ok(None);
        };
        let Some(part) = dict_of(&part)? else {
            return Ok(None);
        };
        let Some(people) = part.get_item(name)? else {
            return Ok(None);
        };
        let Some(people) = dict_of(&people)? else {
            return Ok(None);
        };
        people.get_item(&key)
    };
    let table = attribute(world, intern!(py, "model_state"))?;
    let table = crate::core::row::exact_dict(&table)?;
    if let Some(person) = in_part(table.get_item(intern!(py, "population_dynamics_v1"))?, intern!(py, "profile_context"))? {
        return Ok(Some(person));
    }
    if let Some(person) = in_part(table.get_item(intern!(py, "population_context_v1"))?, intern!(py, "people"))? {
        return Ok(Some(person));
    }
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let Some(configs) = dict_of(&configs)? else {
        return unsupported("the configs are not a dict");
    };
    in_part(configs.get_item(intern!(py, "population_context"))?, intern!(py, "people"))
}

/// `care_source_ids_for_agent`.
pub(crate) fn source_ids_of(world: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<Vec<Arc<str>>> {
    let py = world.py();
    let mut sources = vec![NAMES.lifecycle_source.clone()];
    let Some(person) = person_context(world, agent_id)? else {
        return Ok(sources);
    };
    let Some(person) = dict_of(&person)? else {
        return Ok(sources);
    };
    let Some(listed) = person.get_item(intern!(py, "source_ids"))? else {
        return Ok(sources);
    };
    let mut add = |item: Bound<'_, PyAny>| -> PyResult<()> {
        if item.is_exact_instance_of::<PyString>() {
            sources.push(Arc::from(exact_str(&item)?));
        } else if item.is_instance_of::<PyString>() {
            return unsupported("a subclass of str");
        }
        Ok(())
    };
    if let Ok(list) = listed.downcast::<PyList>() {
        for item in list.iter() {
            add(item)?;
        }
    } else if let Ok(tuple) = listed.downcast_exact::<PyTuple>() {
        for item in tuple.iter() {
            add(item)?;
        }
    } else {
        return unsupported("the person's sources are for the Python rule to read");
    }
    Ok(sorted_unique(sources))
}

// ---- The other records of `domain/care_receipts.py` -------------------------------------------------
//
// Each has the same event shape and goes to the dispatcher as a Python record. What the receipt's
// contract would refuse goes to the Python function by name, so its own error is raised; a caller may
// be past its first change. The checks here are of the contract's fields only.

const FACILITY_SOURCE: &str = "ASM-FACILITY-RECOVERY-PRODUCER-V1";
const FACILITY_POLICY: &str = "applied-facility-recovery-v1";
const HANDOFF_SOURCE: &str = "ASM-STAFF-HANDOFF-PRODUCER-V1";
const HANDOFF_POLICY: &str = "applied-staff-handoff-v1";
const OPERATION_SOURCE: &str = "ASM-STAFF-OPERATION-RECEIPT-V1";
const OPERATION_POLICY: &str = "applied-staff-operation-receipt-v1";
const CONTINUITY_POLICY: &str = "staff-support-continuity-v1";

struct Receipt<'py, 'a> {
    event_id: &'a str,
    sequence: i64,
    minute: i64,
    event_type: &'a str,
    payload: Bound<'py, PyDict>,
    source: &'a str,
    causation: &'a str,
    policy: &'a str,
    detail: String,
}

fn receipt_record<'py>(world: &Bound<'py, PyAny>, receipt: Receipt<'py, '_>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let source = PyString::new(py, receipt.source);
    let event = new_event(
        py,
        NewEvent {
            event_id: receipt.event_id,
            kind: "ENDOGENOUS",
            due_minute: receipt.minute,
            priority: 0,
            sequence: receipt.sequence,
            event_type: receipt.event_type,
            payload: receipt.payload.into_any(),
            source_id: Some(source.clone().into_any()),
            causation_id: Some(PyString::new(py, receipt.causation).into_any()),
            provenance: assumption(py, source.as_any(), receipt.policy)?.into_any(),
        },
    )?;
    let minute = attribute(world, intern!(py, "minute_index"))?;
    log_record(py, &event, &minute, "APPLIED", Some(&receipt.detail))
}

fn header<'py>(py: Python<'py>, record_type: &str, event_type: &str, event_id: &str) -> PyResult<Bound<'py, PyDict>> {
    let payload = PyDict::new(py);
    payload.set_item("record_type", record_type)?;
    payload.set_item("schema_version", 1)?;
    payload.set_item("event_type", event_type)?;
    payload.set_item("event_id", event_id)?;
    Ok(payload)
}

fn shared_absent(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    Ok(!attribute(world, intern!(py, "model_state"))?.contains(intern!(py, NAME))?)
}

fn python_receipt<'py>(world: &Bound<'py, PyAny>, name: &str, kwargs: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyAny>> {
    world.py().import("sheltersim.domain.care_receipts")?.getattr(name)?.call((world,), Some(kwargs))
}

fn minute_and_sequence(world: &Bound<'_, PyAny>) -> PyResult<(i64, i64)> {
    let py = world.py();
    let sequence = exact_int(&world.call_method0(intern!(py, "allocate_event_sequence"))?)?;
    Ok((exact_int(&attribute(world, intern!(py, "minute_index"))?)?, sequence))
}

/// `make_facility_recovery_record(...)[0]`, or None without the shared contract.
#[allow(clippy::too_many_arguments)]
pub(crate) fn facility_recovery_record<'py>(
    world: &Bound<'py, PyAny>,
    facility_id: &str,
    component_id: &str,
    status: &str,
    previous: i64,
    units: i64,
    causation: &str,
    source_ids: Vec<String>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = world.py();
    if shared_absent(world)? {
        return Ok(None);
    }
    let fits = is_identifier(facility_id)
        && is_identifier(component_id)
        && is_identifier(causation)
        && source_ids.iter().all(|source| is_identifier(source))
        && match status {
            "degraded" | "outage" | "restored" => units != previous,
            "repair_started" | "repaired" => true,
            _ => false,
        }
        && previous >= 0
        && units >= 0
        && exact_int(&attribute(world, intern!(py, "minute_index"))?)? >= 0;
    if !fits {
        let kwargs = PyDict::new(py);
        kwargs.set_item("facility_id", facility_id)?;
        kwargs.set_item("component_id", component_id)?;
        kwargs.set_item("status", status)?;
        kwargs.set_item("previous_working_units", previous)?;
        kwargs.set_item("working_units", units)?;
        kwargs.set_item("causation_id", causation)?;
        kwargs.set_item("source_ids", PyTuple::new(py, &source_ids)?)?;
        return Ok(Some(python_receipt(world, "make_facility_recovery_record", &kwargs)?.get_item(0)?));
    }
    let (minute, sequence) = minute_and_sequence(world)?;
    let event_id = format!("facility-recovery-transition-{sequence}");
    let mut sources = source_ids;
    sources.push(FACILITY_SOURCE.to_owned());
    sources.sort_unstable();
    sources.dedup();
    let payload = header(py, "operations", "facility_recovery_transition", &event_id)?;
    payload.set_item("facility_id", facility_id)?;
    payload.set_item("component_id", component_id)?;
    payload.set_item("status", status)?;
    payload.set_item("minute", minute)?;
    payload.set_item("previous_working_units", previous)?;
    payload.set_item("working_units", units)?;
    payload.set_item("source_ids", PyList::new(py, &sources)?)?;
    payload.set_item("causation_id", causation)?;
    Ok(Some(receipt_record(
        world,
        Receipt {
            event_id: &event_id,
            sequence,
            minute,
            event_type: "facility_recovery_transition",
            payload,
            source: FACILITY_SOURCE,
            causation,
            policy: FACILITY_POLICY,
            detail: format!("facility {status} receipt recorded after working-unit update"),
        },
    )?))
}

/// `make_staff_handoff_record(...)[0]`, or None without the shared contract.
pub(crate) fn handoff_record<'py>(
    world: &Bound<'py, PyAny>,
    task_id: &str,
    previous_staff: &str,
    replacement: &str,
    reason_code: &str,
    causation: &str,
    source_ids: Vec<String>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = world.py();
    if shared_absent(world)? {
        return Ok(None);
    }
    let fits = [task_id, previous_staff, replacement, reason_code, causation].iter().all(|id| is_identifier(id))
        && source_ids.iter().all(|source| is_identifier(source))
        && replacement != previous_staff
        && exact_int(&attribute(world, intern!(py, "minute_index"))?)? >= 0;
    if !fits {
        let kwargs = PyDict::new(py);
        kwargs.set_item("task_id", task_id)?;
        kwargs.set_item("previous_staff_id", previous_staff)?;
        kwargs.set_item("replacement_staff_id", replacement)?;
        kwargs.set_item("reason_code", reason_code)?;
        kwargs.set_item("causation_id", causation)?;
        kwargs.set_item("source_ids", PyTuple::new(py, &source_ids)?)?;
        return Ok(Some(python_receipt(world, "make_staff_handoff_record", &kwargs)?.get_item(0)?));
    }
    let (minute, sequence) = minute_and_sequence(world)?;
    let event_id = format!("staff-handoff-{sequence}");
    let mut sources = source_ids;
    sources.push(HANDOFF_SOURCE.to_owned());
    sources.sort_unstable();
    sources.dedup();
    let payload = header(py, "operations", "staff_handoff", &event_id)?;
    payload.set_item("task_id", task_id)?;
    payload.set_item("previous_staff_id", previous_staff)?;
    payload.set_item("replacement_staff_id", replacement)?;
    payload.set_item("status", "reassigned")?;
    payload.set_item("minute", minute)?;
    payload.set_item("reason_code", reason_code)?;
    payload.set_item("causation_id", causation)?;
    payload.set_item("source_ids", PyList::new(py, &sources)?)?;
    Ok(Some(receipt_record(
        world,
        Receipt {
            event_id: &event_id,
            sequence,
            minute,
            event_type: "staff_handoff",
            payload,
            source: HANDOFF_SOURCE,
            causation,
            policy: HANDOFF_POLICY,
            detail: "staff task assignment updated before handoff receipt".to_owned(),
        },
    )?))
}

/// `make_staff_support_continuity_record`.
#[allow(clippy::too_many_arguments)]
pub(crate) fn continuity_record<'py>(
    world: &Bound<'py, PyAny>,
    request_id: &str,
    agent_id: &str,
    task_id: &str,
    previous_staff: &str,
    status: &str,
    causation: &str,
    cause_type: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let fits = [request_id, agent_id, task_id, previous_staff, causation].iter().all(|id| is_identifier(id))
        && exact_int(&attribute(world, intern!(py, "minute_index"))?)? >= 0;
    if !fits {
        let kwargs = PyDict::new(py);
        kwargs.set_item("request_id", request_id)?;
        kwargs.set_item("agent_id", agent_id)?;
        kwargs.set_item("task_id", task_id)?;
        kwargs.set_item("previous_staff_id", previous_staff)?;
        kwargs.set_item("status", status)?;
        kwargs.set_item("causation_id", causation)?;
        kwargs.set_item("cause_type", cause_type)?;
        return python_receipt(world, "make_staff_support_continuity_record", &kwargs);
    }
    let (minute, sequence) = minute_and_sequence(world)?;
    let event_id = format!("staff-support-continuity-{sequence}");
    let payload = header(py, "care", "staff_support_continuity", &event_id)?;
    payload.set_item("request_id", request_id)?;
    payload.set_item("agent_id", agent_id)?;
    payload.set_item("task_id", task_id)?;
    payload.set_item("previous_staff_id", previous_staff)?;
    payload.set_item("status", status)?;
    payload.set_item("cause_type", cause_type)?;
    payload.set_item("minute", minute)?;
    payload.set_item("causation_id", causation)?;
    payload.set_item("source_ids", PyList::new(py, [LIFECYCLE_SOURCE])?)?;
    receipt_record(
        world,
        Receipt {
            event_id: &event_id,
            sequence,
            minute,
            event_type: "staff_support_continuity",
            payload,
            source: LIFECYCLE_SOURCE,
            causation,
            policy: CONTINUITY_POLICY,
            detail: format!("staff support request was {status} after assigned staff became unavailable"),
        },
    )
}

/// What a completed staff operation says, for `make_staff_operation_record`.
pub(crate) struct OperationOutcome<'py, 'a> {
    /// The record's document, which holds what the receipt copies.
    pub(crate) operation: &'a Bound<'py, PyDict>,
    /// Sorted, as `NumericStateChange` documents.
    pub(crate) changes: Vec<Bound<'py, PyDict>>,
    pub(crate) measured: bool,
    pub(crate) causation: &'a str,
}

fn number_of(value: &Bound<'_, PyAny>) -> Option<f64> {
    value.extract::<f64>().ok().filter(|value| value.is_finite())
}

fn change_fits(change: &Bound<'_, PyDict>) -> PyResult<bool> {
    let text = |name: &str| -> PyResult<Option<String>> { Ok(change.get_item(name)?.and_then(|value| value.extract().ok())) };
    Ok(text("entity_id")?.is_some_and(|id| is_identifier(&id))
        && change.get_item("before")?.is_some_and(|value| number_of(&value).is_some())
        && change.get_item("after")?.is_some_and(|value| number_of(&value).is_some()))
}

/// `make_staff_operation_record(...)[0]`, or None without the shared contract.
pub(crate) fn staff_operation_record<'py>(world: &Bound<'py, PyAny>, outcome: OperationOutcome<'py, '_>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = world.py();
    if shared_absent(world)? {
        return Ok(None);
    }
    let OperationOutcome { operation, changes, measured, causation } = outcome;
    let item = |name: &str| -> PyResult<Bound<'py, PyAny>> {
        operation.get_item(name)?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(name.to_owned()))
    };
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let (task_id, staff_id, kind, target, zone, reason, status) =
        (item("task_id")?, item("staff_id")?, item("task_kind")?, item("target_id")?, item("target_zone_id")?, item("reason")?, item("status")?);
    let (start, arrival, travel) = (item("start_minute")?, item("arrival_minute")?, item("travel_minutes")?);
    let observation = if !changes.is_empty() {
        "observed_change"
    } else if measured {
        "observed_no_change"
    } else {
        "not_measured"
    };
    let total = changes.len();
    let sampled = &changes[..total.min(256)];
    let times_fit = match (exact_int(&start), exact_int(&arrival), exact_int(&travel)) {
        (Ok(start), Ok(arrival), Ok(travel)) => start >= 0 && travel >= 0 && start <= arrival && arrival <= minute && travel <= arrival - start,
        _ => false,
    };
    let mut changes_fit = true;
    for change in sampled {
        changes_fit &= change_fits(change)?;
    }
    let reason_fits = exact_str(&reason).is_ok_and(|text| (1..=240).contains(&text.chars().count()));
    let status_text = exact_str(&status).ok();
    let id_fits = |value: &Bound<'py, PyAny>| exact_str(value).is_ok_and(is_identifier);
    let fits = times_fit
        && changes_fit
        && reason_fits
        && id_fits(&task_id)
        && id_fits(&staff_id)
        && (target.is_none() || id_fits(&target))
        && id_fits(&zone)
        && is_identifier(causation)
        && minute >= 0
        && exact_str(&kind).is_ok_and(|kind| matches!(kind, "cleaning" | "maintenance" | "information_outreach" | "intake" | "patrol"))
        && match status_text {
            Some("completed") => true,
            Some("blocked") => total == 0 && measured,
            _ => false,
        };
    if !fits {
        let contract = py.import("sheltersim.contracts.operations")?.getattr("NumericStateChange")?;
        let objects: Vec<Bound<'py, PyAny>> = changes.iter().map(|change| contract.call_method1("model_validate", (change,))).collect::<PyResult<_>>()?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("operation", operation)?;
        kwargs.set_item("state_changes", PyTuple::new(py, objects)?)?;
        kwargs.set_item("outcome_measured", measured)?;
        kwargs.set_item("causation_id", causation)?;
        return Ok(Some(python_receipt(world, "make_staff_operation_record", &kwargs)?.get_item(0)?));
    }
    let (minute, sequence) = minute_and_sequence(world)?;
    let event_id = format!("staff-operation-transition-{sequence}");
    let payload = header(py, "operations", "staff_operation_transition", &event_id)?;
    payload.set_item("task_id", task_id)?;
    payload.set_item("staff_id", staff_id)?;
    payload.set_item("task_kind", kind)?;
    payload.set_item("target_id", target)?;
    payload.set_item("target_zone_id", zone)?;
    payload.set_item("reason", reason)?;
    payload.set_item("status", &status)?;
    payload.set_item("minute", minute)?;
    payload.set_item("start_minute", start)?;
    payload.set_item("arrival_minute", arrival)?;
    payload.set_item("travel_minutes", travel)?;
    payload.set_item("outcome_observation", observation)?;
    payload.set_item("state_changes", PyList::new(py, sampled)?)?;
    payload.set_item("total_state_change_count", total)?;
    payload.set_item("state_changes_truncated", total > sampled.len())?;
    payload.set_item("causation_id", causation)?;
    payload.set_item("source_ids", PyList::new(py, [OPERATION_SOURCE])?)?;
    let detail = format!("staff operation {} outcome recorded after state application", exact_str(&status)?);
    Ok(Some(receipt_record(
        world,
        Receipt {
            event_id: &event_id,
            sequence,
            minute,
            event_type: "staff_operation_transition",
            payload,
            source: OPERATION_SOURCE,
            causation,
            policy: OPERATION_POLICY,
            detail,
        },
    )?))
}

// ---- ingest_applied_shared_operation_records ---------------------------------------------------------

/// Whether a receipt of this event is among those the Python rule scans: `recent` keeps to the records
/// of the last tick and the one before them.
fn receipt_is_recorded(world: &Bound<'_, PyAny>, event_id: &str, recent: bool, minute: i64) -> PyResult<bool> {
    let mirror = mirror_of(world, NAME)?;
    NamespaceMirror::with_row::<SharedOperationsRow, _>(&mirror, |row| {
        let start = if recent { row.history.recent_start(minute) } else { 0 };
        Ok(row.history.items.iter().skip(start).any(|item| item.event_id.as_deref() == Some(event_id)))
    })
}

/// `ingest_applied_shared_operation_records`. The receipts' contract stays Python's, and so does
/// `record_shared_operation` of what isn't a care receipt.
#[pyfunction]
fn ingest<'py>(world: &Bound<'py, PyAny>, outcomes: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    if !table.contains(intern!(py, NAME))? {
        return Ok(());
    }
    if !table.get_item(intern!(py, NAME))?.is_instance_of::<PyDict>() {
        return unsupported("the namespace is another value");
    }
    let applied = status_member(py, "APPLIED")?;
    let module = py.import("sheltersim.domain.care_receipts")?;
    let shared = py.import("sheltersim.domain.common_operations")?;
    let mut first = true;
    for outcome in outcomes.try_iter()? {
        let outcome = outcome?;
        let Ok(event) = outcome.getattr(intern!(py, "event")) else {
            continue;
        };
        let status = outcome.getattr(intern!(py, "status")).ok();
        if !status.is_some_and(|status| status.is(&applied)) || event.is_none() {
            continue;
        }
        let event_type = attribute(&event, intern!(py, "event_type"))?;
        if !matches!(
            exact_str(&event_type)?,
            "care_support_transition" | "staff_support_continuity" | "facility_recovery_transition" | "staff_handoff" | "staff_operation_transition"
        ) {
            continue;
        }
        let event_id = attribute(&event, intern!(py, "event_id"))?;
        let minute = attribute(world, intern!(py, "minute_index"))?;
        let realized = outcome.getattr(intern!(py, "realized_minute")).ok();
        let recent = realized.is_some_and(|realized| realized.eq(&minute).unwrap_or(false));
        let recorded = receipt_is_recorded(world, exact_str(&event_id)?, recent, exact_int(&minute)?);
        // Only the first can be refused: nothing has changed yet.
        let recorded = match recorded {
            Err(error) if !first && error.is_instance_of::<Unsupported>(py) => {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(format!("the shared operations left their row: {}", error.value(py))));
            }
            other => other?,
        };
        if recorded {
            continue;
        }
        let adapter = module.getattr(intern!(py, "_operation_record_adapter"))?.call0()?;
        let receipt = adapter.call_method1(intern!(py, "validate_python"), (attribute(&event, intern!(py, "payload"))?,))?;
        shared.getattr(intern!(py, "record_shared_operation"))?.call1((world, receipt))?;
        first = false;
    }
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(append_transition, m)?)?;
    m.add_function(wrap_pyfunction!(ingest, m)?)?;
    Ok(())
}
