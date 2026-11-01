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
use crate::core::world_core::mirror_of;
use crate::rules::common_operations::{plan_record, write_record, CareRecord, PlannedRecord};
use crate::state::events::{EventKind, EventRow, EventStatus, EventWriter};
use crate::state::shared_operations::{is_identifier, is_support_kind, CareStatus, NAME};
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

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(append_transition, m)?)?;
    Ok(())
}
