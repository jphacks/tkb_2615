//! The support requests of `domain/staff.py`: `request` and `timeout_support_request`, and the care
//! receipts of the steps around them (`experiments/phases/dispatch.py`). Each kernel plans with
//! nothing changed and then writes (docs/rust-port/lanes/m6-handlers.md).
//!
//! A `SupportRequestState` reports its writes (`_write_listener`), so what changes in one goes through
//! its own `__setattr__`.

use std::sync::{Arc, LazyLock};

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use sha2::{Digest, Sha256};

use crate::core::row::{attribute, exact_dict, exact_int, exact_str, namespace_is_absent, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::core::world_core::{mirror_of, raw_dict_get, NamespaceMirror};
use crate::core::mathx::{py_max, py_min};
use crate::rules::care_receipts::{
    allocate_transition, append, person_context, plan_transition, source_ids_of, transition_record, PlannedTransition, Transition,
};
use crate::rules::effects::refused_by_python;
use crate::rules::handlers::{get, number, text_is};
use crate::rules::service::{add_one, metrics_of, set_agent};
use crate::state::body_state::{BodyRecord, BodyRow, NAME as BODY};
use crate::state::entities::{entities_of, raw_setattr, Activity, EntityStore, RequestStatus};
use crate::state::events::{EventKind, EventRef, EventRow, EventWriter};
use crate::Unsupported;
use crate::state::shared_operations::{SharedOperationsRow, NAME as SHARED};

const STAFF_SUPPORT_SOURCE: &str = "ASM-STAFF-SUPPORT-LIFECYCLE-V1";
const UNRESOLVED_METRIC: &str = "support_unresolved_count";
const RESOLVED_METRIC: &str = "support_resolved_count";
const BODY_SOURCE: &str = "ASM-LIFE-STATE-FEEDBACK-V1";
const PRESENCE: &str = "population_presence_v1";
const EMOTIONAL_SUPPORT: &str = "emotional_support";
const TYPED_CARE_MODEL: &str = "typed-care-support-response-v1";
const PROPORTIONAL_HYGIENE_MODEL: &str = "hygiene-support-proportional-relief-v1";
/// What the handler says of a request that has left `pending`.
pub(crate) const NO_LONGER_PENDING: &str = "support request is no longer pending";

struct Names {
    request_id: Arc<str>,
    agent_id: Arc<str>,
    event_type: Arc<str>,
    source_type: Arc<str>,
    source_id: Arc<str>,
    policy_version: Arc<str>,
    timeout_minutes: Arc<str>,
    assumption: Arc<str>,
    source: Arc<str>,
}

static NAMES: LazyLock<Names> = LazyLock::new(|| Names {
    request_id: Arc::from("request_id"),
    agent_id: Arc::from("agent_id"),
    event_type: Arc::from("support_request_timeout"),
    source_type: Arc::from("source_type"),
    source_id: Arc::from("source_id"),
    policy_version: Arc::from("policy_version"),
    timeout_minutes: Arc::from("timeout_minutes"),
    assumption: Arc::from("assumption"),
    source: Arc::from(STAFF_SUPPORT_SOURCE),
});

fn request_class(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    static CLASS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    Ok(CLASS
        .get_or_try_init(py, || -> PyResult<Py<PyAny>> { Ok(py.import("sheltersim.domain.models")?.getattr("SupportRequestState")?.unbind()) })?
        .bind(py))
}

/// `SupportRequestTimeoutHandler.apply` up to the error text: `Some(text)` when Python would suppress the
/// event. `cooldown_minutes` is the handler's.
pub(crate) fn timeout_event<'py>(
    world: &Bound<'py, PyAny>,
    event: EventRef<'_, 'py>,
    cooldown_minutes: i64,
) -> PyResult<Option<&'static str>> {
    let py = world.py();
    let (Some(request_id), Some(agent_id)) = (event.text("request_id")?, event.text("agent_id")?) else {
        return unsupported("a timeout without its IDs");
    };
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let store = entities_of(world)?;
    let status = EntityStore::with_rows(&store, |rows| {
        let Some(request) = rows.support_requests.get(&*request_id) else {
            return unsupported("no such request");
        };
        if *request.agent_id != *agent_id {
            return unsupported("the request is another person's");
        }
        Ok(request.status)
    })?;
    if status != RequestStatus::Pending {
        return Ok(Some(NO_LONGER_PENDING));
    }
    let Some(until) = minute.checked_add(cooldown_minutes) else {
        return unsupported("a minute out of range");
    };
    let metrics = metrics_of(world)?;
    let requests = attribute(world, intern!(py, "support_requests"))?;
    let agents = attribute(world, intern!(py, "agents"))?;
    let agent_text = PyString::new(py, &agent_id);
    let (Some(request), Some(agent)) = (
        raw_dict_get(&requests, PyString::new(py, &request_id).as_any())?,
        raw_dict_get(&agents, agent_text.as_any())?,
    ) else {
        return unsupported("no such request or person");
    };

    // Write.
    request.setattr(intern!(py, "status"), RequestStatus::Unresolved.member(py)?)?;
    request.setattr(intern!(py, "resolved_minute"), minute)?;
    request.setattr(intern!(py, "cooldown_until"), until)?;
    EntityStore::with_rows(&store, |rows| {
        let Some(row) = rows.agents.get_mut(&*agent_id) else {
            return unsupported("no such agent");
        };
        row.support_request_id = None;
        row.support_cooldown_until = until;
        Ok(())
    })?;
    raw_setattr(&agent, intern!(py, "support_request_id"), py.None().as_ptr())?;
    raw_setattr(&agent, intern!(py, "support_cooldown_until"), until.into_pyobject(py)?.as_ptr())?;
    set_agent(world, &store, agent_text.as_any(), Activity::Idle, None)?;
    add_one(&metrics, UNRESOLVED_METRIC)?;
    Ok(None)
}

/// `SupportRequestTimeoutHandler.apply`, called from Python.
#[pyfunction]
fn timeout<'py>(handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = handler.py();
    let cooldown = exact_int(&attribute(handler, intern!(py, "cooldown_minutes"))?)?;
    if let Some(text) = timeout_event(world, EventRef::Object(event), cooldown)? {
        let suppressed = py.import(intern!(py, "sheltersim.events.models"))?.getattr(intern!(py, "EventSuppressed"))?;
        return Err(PyErr::from_value(suppressed.call1((text,))?));
    }
    Ok(())
}

/// `_record_request_interrupted`, called from Python.
#[pyfunction]
#[pyo3(name = "record_interrupted")]
fn record_interrupted_step<'py>(_controller: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    record_interrupted(world, EventRef::Object(event))
}

/// `_record_staff_support_started`, called from Python.
#[pyfunction]
#[pyo3(name = "record_started")]
fn record_started_step<'py>(_controller: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    record_started(world, EventRef::Object(event))
}

// ---- The care receipts around a request ------------------------------------------------------------

/// `care_receipt_for_request`'s `household_id` and `support_kind`. None without a receipt.
fn prior_of(world: &Bound<'_, PyAny>, request_id: &str) -> PyResult<Option<(Option<Arc<str>>, Arc<str>)>> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, SHARED))? {
        let table = attribute(world, intern!(py, "model_state"))?;
        if exact_dict(&table)?.contains(intern!(py, SHARED))? {
            return unsupported("the namespace is None");
        }
        return Ok(None);
    }
    NamespaceMirror::with_row::<SharedOperationsRow, _>(&mirror_of(world, SHARED)?, |row| {
        Ok(row.support_requests.map.get(request_id).map(|head| (head.household_id.clone(), head.support_kind.clone())))
    })
}

/// `_record_request_interrupted`, of an applied `support_request_timeout`.
pub(crate) fn record_interrupted(world: &Bound<'_, PyAny>, event: EventRef<'_, '_>) -> PyResult<()> {
    let Some(request_id) = event.text("request_id")? else {
        return unsupported("a timeout without its request");
    };
    let store = entities_of(world)?;
    let agent_id = EntityStore::with_rows(&store, |rows| {
        Ok(rows.support_requests.get(&*request_id).and_then(|request| {
            (request.status == RequestStatus::Unresolved).then(|| request.agent_id.clone())
        }))
    })?;
    let Some(agent_id) = agent_id else {
        return Ok(());
    };
    let Some((household_id, support_kind)) = prior_of(world, &request_id)? else {
        return Ok(());
    };
    let source_ids = source_ids_of(world, &agent_id)?;
    append(
        world,
        Transition {
            request_id,
            agent_id,
            household_id,
            support_kind,
            status: "interrupted",
            staff_id: None,
            causation_id: Some(event.event_id()?),
            source_ids,
            outcome: None,
        },
    )
}

/// `_record_staff_support_started`, of an applied `agent_travel_complete` of a staff trip.
pub(crate) fn record_started(world: &Bound<'_, PyAny>, event: EventRef<'_, '_>) -> PyResult<()> {
    let Some(request_id) = event.text("request_id")? else {
        return unsupported("a staff trip without its request");
    };
    let staff_id = event.text("staff_id")?;
    let store = entities_of(world)?;
    let found = EntityStore::with_rows(&store, |rows| {
        let Some(request) = rows.support_requests.get(&*request_id) else {
            return Ok(None);
        };
        if request.status != RequestStatus::Assigned || request.assigned_staff_id != staff_id {
            return Ok(None);
        }
        let Some(agent) = rows.agents.get(&*request.agent_id) else {
            return unsupported("the request's person is gone");
        };
        Ok((agent.activity == Activity::SeekingHelp).then(|| (request.agent_id.clone(), request.task_id.clone())))
    })?;
    let Some((agent_id, task_id)) = found else {
        return Ok(());
    };
    let Some((household_id, support_kind)) = prior_of(world, &request_id)? else {
        return Ok(());
    };
    let source_ids = source_ids_of(world, &agent_id)?;
    append(
        world,
        Transition { request_id, agent_id, household_id, support_kind, status: "started", staff_id, causation_id: task_id, source_ids, outcome: None },
    )
}

// ---- A staff member's support ends ------------------------------------------------------------------

fn config_float(config: &V, by_agent: &str, name: &str, agent_id: &str) -> PyResult<f64> {
    // `float(config.get(by_agent, {}).get(agent, config.get(name, 0.0)))`, of floats.
    let V::Map(config) = config else {
        return unsupported("the config is not a document");
    };
    let value = match config.get(by_agent) {
        None => None,
        Some(V::Map(values)) => values.get(agent_id),
        Some(_) => return unsupported("the config's table is not a document"),
    };
    match value.or_else(|| config.get(name)) {
        None => Ok(0.0),
        Some(V::Float(value)) => Ok(*value),
        Some(_) => unsupported("a relief that is not a float"),
    }
}

fn change(names: &ChangeNames, kind: &Arc<str>, entity: &Arc<str>, path: &str, before: f64, after: f64) -> V {
    let mut document: IndexMap<Arc<str>, V> = IndexMap::with_capacity(6);
    document.insert(names.entity_id.clone(), V::Str(entity.clone()));
    document.insert(names.entity_kind.clone(), V::Str(kind.clone()));
    document.insert(names.field_path.clone(), V::Str(Arc::from(path)));
    document.insert(names.before.clone(), V::Float(before));
    document.insert(names.after.clone(), V::Float(after));
    document.insert(names.unit.clone(), V::Str(names.normalized.clone()));
    V::Map(document)
}

struct ChangeNames {
    entity_id: Arc<str>,
    entity_kind: Arc<str>,
    field_path: Arc<str>,
    before: Arc<str>,
    after: Arc<str>,
    unit: Arc<str>,
    normalized: Arc<str>,
    agent: Arc<str>,
    model_state: Arc<str>,
}

static CHANGE_NAMES: LazyLock<ChangeNames> = LazyLock::new(|| ChangeNames {
    entity_id: Arc::from("entity_id"),
    entity_kind: Arc::from("entity_kind"),
    field_path: Arc::from("field_path"),
    before: Arc::from("before"),
    after: Arc::from("after"),
    unit: Arc::from("unit"),
    normalized: Arc::from("normalized_0_1"),
    agent: Arc::from("agent"),
    model_state: Arc::from("model_state"),
});

/// What the care does to the person and their body: their new values.
enum Care {
    None,
    /// What a selected care formula does: each person's new stress and body record.
    Formula { people: Vec<CaredPerson> },
    Emotional { stress: f64, trust: f64, record: BodyRecord },
    Hygiene { record: BodyRecord },
}

/// `StaffSupportCompletionHandler.apply` up to the receipt: what it returns as a record, planned with
/// its sequence allocated, or None without a receipt. A kind that a selected care formula decides,
/// and whatever fails a check, is the Python rule's.
pub(crate) fn complete_support_event<'py>(
    handler: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    writer: &EventWriter<'py>,
    event: EventRef<'_, 'py>,
) -> PyResult<Option<PlannedTransition>> {
    let py = world.py();
    let (Some(request_id), Some(agent_id), Some(staff_id), Some(task_id)) =
        (event.text("request_id")?, event.text("agent_id")?, event.text("staff_id")?, event.text("task_id")?)
    else {
        return unsupported("a completion without its IDs");
    };
    if request_id.is_empty() || agent_id.is_empty() || staff_id.is_empty() || task_id.is_empty() {
        return unsupported("a completion without its IDs");
    }
    let cooldown = exact_int(&attribute(handler, intern!(py, "cooldown_minutes"))?)?;
    let colocated = attribute(handler, intern!(py, "require_colocated"))?.is_truthy()?;
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let due = event.due_minute()?;
    let store = entities_of(world)?;
    let (stress, trust) = EntityStore::with_rows(&store, |rows| {
        let Some(task) = rows.active_tasks.get(&*task_id) else {
            return unsupported("no active task");
        };
        let (Some(request), Some(agent), Some(person)) =
            (rows.support_requests.get(&*request_id), rows.agents.get(&*agent_id), rows.staff.get(&*staff_id))
        else {
            return unsupported("no such request, person or staff");
        };
        if task.due_minute != due
            || request.status != RequestStatus::Assigned
            || &*task.kind != "staff_support"
            || *request.agent_id != *agent_id
            || request.assigned_staff_id.as_deref() != Some(&*staff_id)
            || request.task_id.as_deref() != Some(&*task_id)
            || task.agent_id.as_deref() != Some(&*agent_id)
            || task.staff_id.as_deref() != Some(&*staff_id)
            || task.due_minute != minute
            || person.active_task_id.as_deref() != Some(&*task_id)
            || (colocated && agent.activity != Activity::SeekingHelp)
            || (colocated && agent.zone_id != person.zone_id)
        {
            return unsupported("the completion does not match its task");
        }
        Ok((agent.stress, agent.trust))
    })?;
    let Some(until) = minute.checked_add(cooldown) else {
        return unsupported("a minute out of range");
    };
    let metrics = metrics_of(world)?;
    let prior = prior_of(world, &request_id)?;
    let mut plan_outcome = None;
    let (care, transition) = match prior {
        None => (Care::None, None),
        Some((household_id, support_kind)) => {
            let emotional = &*support_kind == EMOTIONAL_SUPPORT;
            // Of the kinds, only emotional support is the same under every care formula.
            let formula = if emotional { None } else { selected_care_formula(world)? };
            let hygiene = &*support_kind == "hygiene_support";
            let mut care = Care::None;
            if let Some(formula) = formula {
                let (care, outcome, source_ids) = formula_care(world, &formula, &agent_id, &support_kind, household_id.as_deref(), stress)?;
                let transition = Transition {
                    request_id: request_id.clone(),
                    agent_id: agent_id.clone(),
                    household_id,
                    support_kind,
                    status: "completed",
                    staff_id: Some(staff_id.clone()),
                    causation_id: Some(event.event_id()?),
                    source_ids,
                    outcome: Some(outcome),
                };
                plan_outcome = Some((care, Some(plan_transition(world, writer, transition, writer.next_sequence()?)?)));
                (Care::None, None)
            } else {
            let mut outcome = ("not_measured", V::List(Vec::new()));
            let mut source_ids = vec![NAMES.source.clone()];
            if emotional || hygiene {
                let names = &*CHANGE_NAMES;
                if namespace_is_absent(world, intern!(py, BODY))? {
                    return unsupported("the body state is for the Python rule");
                }
                let mirror = mirror_of(world, BODY)?;
                let found = NamespaceMirror::with_row::<BodyRow, _>(&mirror, |row| {
                    let Some(record) = row.agents.get(&*agent_id).copied() else {
                        return unsupported("the person has no body record");
                    };
                    let name = if emotional { "complaint_reassurance_gain" } else { "hygiene_support_relief" };
                    let by_agent = if emotional { "complaint_reassurance_gain_by_agent" } else { "hygiene_support_relief_by_agent" };
                    Ok((record, config_float(&row.config, by_agent, name, &agent_id)?))
                })?;
                let (record, relief) = found;
                let mut next = record;
                let mut changes = Vec::with_capacity(5);
                let mut changed = false;
                let mut note = |kind: &Arc<str>, path: &str, before: f64, after: f64| {
                    changed |= before != after;
                    changes.push(change(names, kind, &agent_id, path, before, after));
                };
                if emotional {
                    let (stress_after, trust_after) = (py_max(0.0, stress - relief), py_min(1.0, trust + relief * 0.25));
                    next.reassurance = py_min(1.0, record.reassurance + relief);
                    next.positive_affect = py_min(1.0, record.positive_affect + relief * 0.5);
                    next.negative_affect = py_max(0.0, record.negative_affect - relief * 0.5);
                    note(&names.agent, "stress", stress, stress_after);
                    note(&names.agent, "trust", trust, trust_after);
                    note(&names.model_state, "body_state_v1.agents.reassurance", record.reassurance, next.reassurance);
                    note(&names.model_state, "body_state_v1.agents.positive_affect", record.positive_affect, next.positive_affect);
                    note(&names.model_state, "body_state_v1.agents.negative_affect", record.negative_affect, next.negative_affect);
                    care = Care::Emotional { stress: stress_after, trust: trust_after, record: next };
                } else {
                    next.hygiene_need = py_max(0.0, record.hygiene_need - relief);
                    note(&names.model_state, "body_state_v1.agents.hygiene_need", record.hygiene_need, next.hygiene_need);
                    care = Care::Hygiene { record: next };
                }
                if ![stress, trust, relief, record.reassurance, record.positive_affect, record.negative_affect, record.hygiene_need]
                    .iter()
                    .all(|value| value.is_finite())
                {
                    return unsupported("a value is not finite");
                }
                outcome = (if changed { "observed_change" } else { "observed_no_change" }, V::List(changes));
                source_ids.push(Arc::from(BODY_SOURCE));
            }
            let transition = Transition {
                request_id: request_id.clone(),
                agent_id: agent_id.clone(),
                household_id,
                support_kind,
                status: "completed",
                staff_id: Some(staff_id.clone()),
                causation_id: Some(event.event_id()?),
                source_ids,
                outcome: Some(outcome),
            };
            (care, Some(plan_transition(world, writer, transition, writer.next_sequence()?)?))
            }
        }
    };
    let (care, transition) = plan_outcome.unwrap_or((care, transition));
    let requests = attribute(world, intern!(py, "support_requests"))?;
    let agents = attribute(world, intern!(py, "agents"))?;
    let staff_map = attribute(world, intern!(py, "staff"))?;
    let agent_text = PyString::new(py, &agent_id);
    let (Some(request), Some(agent), Some(person)) = (
        raw_dict_get(&requests, PyString::new(py, &request_id).as_any())?,
        raw_dict_get(&agents, agent_text.as_any())?,
        raw_dict_get(&staff_map, PyString::new(py, &staff_id).as_any())?,
    ) else {
        return unsupported("a row has no object");
    };
    let tasks = attribute(world, intern!(py, "active_tasks"))?;

    // Write: `complete_support_task`, then what the care does.
    tasks.del_item(&*task_id)?;
    attribute(&person, intern!(py, "work_queue"))?.call_method0(intern!(py, "clear"))?;
    EntityStore::with_rows(&store, |rows| {
        let Some(row) = rows.staff.get_mut(&*staff_id) else {
            return unsupported("no such staff");
        };
        row.active_task_id = None;
        row.busy_until = 0;
        Ok(())
    })?;
    raw_setattr(&person, intern!(py, "active_task_id"), py.None().as_ptr())?;
    raw_setattr(&person, intern!(py, "busy_until"), 0_i64.into_pyobject(py)?.as_ptr())?;
    request.setattr(intern!(py, "status"), RequestStatus::Resolved.member(py)?)?;
    request.setattr(intern!(py, "resolved_minute"), minute)?;
    request.setattr(intern!(py, "cooldown_until"), until)?;
    set_agent(world, &store, agent_text.as_any(), Activity::Idle, None)?;
    EntityStore::with_rows(&store, |rows| {
        let Some(row) = rows.agents.get_mut(&*agent_id) else {
            return unsupported("no such agent");
        };
        row.support_request_id = None;
        row.support_cooldown_until = until;
        if let Care::Emotional { stress, trust, .. } = care {
            row.stress = stress;
            row.trust = trust;
        }
        Ok(())
    })?;
    if let Care::Formula { people } = &care {
        EntityStore::with_rows(&store, |rows| {
            for person in people {
                if let (Some(stress), Some(row)) = (person.stress, rows.agents.get_mut(&*person.agent_id)) {
                    row.stress = stress;
                }
            }
            Ok(())
        })?;
    }
    raw_setattr(&agent, intern!(py, "support_request_id"), py.None().as_ptr())?;
    raw_setattr(&agent, intern!(py, "support_cooldown_until"), until.into_pyobject(py)?.as_ptr())?;
    add_one(&metrics, RESOLVED_METRIC)?;
    if let Care::Formula { people } = &care {
        let mirror = mirror_of(world, BODY)?;
        for person in people {
            if let Some(stress) = person.stress {
                let object = raw_dict_get(&agents, PyString::new(py, &person.agent_id).as_any())?
                    .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("a row has no object"))?;
                raw_setattr(&object, intern!(py, "stress"), stress.into_pyobject(py)?.as_ptr())?;
            }
            if let Some(next) = &person.record {
                NamespaceMirror::with_row_tree::<BodyRow, _>(&mirror, |row, tree| {
                    let record = row
                        .agents
                        .get_mut(&*person.agent_id)
                        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("a planned record left the row"))?;
                    record.set_all(&tree.child(intern!(py, "agents"))?.child_str(&person.agent_id)?, next)
                })?;
            }
        }
    }
    let next_record = match care {
        Care::None | Care::Formula { .. } => None,
        Care::Emotional { stress, trust, record } => {
            raw_setattr(&agent, intern!(py, "stress"), stress.into_pyobject(py)?.as_ptr())?;
            raw_setattr(&agent, intern!(py, "trust"), trust.into_pyobject(py)?.as_ptr())?;
            Some(record)
        }
        Care::Hygiene { record } => Some(record),
    };
    if let Some(next) = next_record {
        let mirror = mirror_of(world, BODY)?;
        NamespaceMirror::with_row_tree::<BodyRow, _>(&mirror, |row, tree| {
            let record = row
                .agents
                .get_mut(&*agent_id)
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("a planned record left the row"))?;
            record.set_all(&tree.child(intern!(py, "agents"))?.child_str(&agent_id)?, &next)
        })?;
    }
    match transition {
        Some(planned) => {
            allocate_transition(writer, &planned)?;
            Ok(Some(planned))
        }
        None => Ok(None),
    }
}

/// `create_care_outcome_formula(configs)`, which the registry makes from a selected model.
fn selected_care_formula<'py>(world: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    static SELECTED: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    let selected = SELECTED.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
        Ok(py.import("sheltersim.contracts.policies")?.getattr("selected_models")?.unbind())
    })?;
    let selection = selected.bind(py).call1((&configs,)).map_err(|_| Unsupported::new_err("the model selection refuses"))?;
    let selection = selection.downcast_exact::<PyDict>().map_err(|_| Unsupported::new_err("not a selection"))?;
    if !selection.get_item(intern!(py, "care_outcome"))?.is_some_and(|name| !name.is_none()) {
        return Ok(None);
    }
    let formula = py
        .import("sheltersim.policies.registry")?
        .getattr("create_care_outcome_formula")?
        .call1((configs,))
        .map_err(|_| Unsupported::new_err("the formula refuses"))?;
    Ok(Some(formula))
}

// ---- What a selected care formula does ---------------------------------------------------------------

/// One person whom a formula's care changes.
pub(crate) struct CaredPerson {
    agent_id: Arc<str>,
    stress: Option<f64>,
    record: Option<BodyRecord>,
}

/// `configured_value` of `support_state_deltas`.
fn configured_value(config: &Bound<'_, PyAny>, name: &str, default: f64, agent_id: &str) -> PyResult<f64> {
    let py = config.py();
    let per_agent = config.call_method1(intern!(py, "get"), (format!("{name}_by_agent"), PyDict::new(py)))?;
    if per_agent.is_instance_of::<PyDict>() && per_agent.contains(agent_id)? {
        return number(&per_agent.get_item(agent_id)?);
    }
    match get(config, name)? {
        value if value.is_none() && !config.contains(name)? => Ok(default),
        value => number(&value),
    }
}

/// `CareOutcomeFormula.support_state_deltas`: each field with its delta, in the formula's order.
fn formula_deltas(formula: &Bound<'_, PyAny>, kind: &str, agent_id: &str) -> PyResult<Vec<(&'static str, f64)>> {
    let py = formula.py();
    if !text_is(&attribute(formula, intern!(py, "model_id"))?, TYPED_CARE_MODEL) {
        return Ok(Vec::new());
    }
    let config = attribute(formula, intern!(py, "config"))?;
    let value = |name: &str, default: f64| configured_value(&config, name, default, agent_id);
    Ok(match kind {
        "caregiver_assistance" => vec![
            ("stress", -value("caregiver_assistance_stress_relief", 0.03)?),
            ("reassurance", value("caregiver_assistance_reassurance_gain", 0.02)?),
        ],
        "dependent_assistance" => vec![
            ("stress", -value("dependent_assistance_stress_relief", 0.04)?),
            ("reassurance", value("dependent_assistance_reassurance_gain", 0.03)?),
        ],
        "privacy_accommodation" => vec![("discontent", -value("privacy_accommodation_discontent_relief", 0.02)?)],
        "mobility_assistance" => vec![("stress", -value("mobility_assistance_stress_relief", 0.03)?)],
        "language_access" => vec![("uncertainty", -value("language_access_uncertainty_relief", 0.05)?)],
        "medical_assessment" => vec![("uncertainty", -value("medical_assessment_uncertainty_relief", 0.02)?)],
        "medication_support" => vec![("uncertainty", -value("medication_support_uncertainty_relief", 0.01)?)],
        _ => Vec::new(),
    })
}

/// `CareOutcomeFormula.hygiene_need_after_support`.
fn hygiene_after(model: &Bound<'_, PyAny>, need: f64, relief: f64) -> f64 {
    let bounded_need = py_min(1.0, py_max(0.0, need));
    let bounded_relief = py_min(1.0, py_max(0.0, relief));
    if text_is(model, PROPORTIONAL_HYGIENE_MODEL) {
        bounded_need * (1.0 - bounded_relief)
    } else {
        py_max(0.0, bounded_need - bounded_relief)
    }
}

/// `explicit_care_dependents`.
fn care_dependents(world: &Bound<'_, PyAny>, caregiver_id: &str, household_id: Option<&str>) -> PyResult<Vec<Arc<str>>> {
    let py = world.py();
    let Some(caregiver) = person_context(world, caregiver_id)? else {
        return Ok(Vec::new());
    };
    if !caregiver.is_instance_of::<PyDict>() || caregiver.call_method1(intern!(py, "get"), ("requires_review", false))?.is_truthy()? {
        return Ok(Vec::new());
    }
    let dependents = caregiver.call_method1(intern!(py, "get"), ("care_for_ids", PyTuple::empty(py)))?;
    let household = get(&caregiver, "household_id")?;
    let is_sequence = dependents.is_instance_of::<PyList>() || dependents.is_instance_of::<PyTuple>();
    if !is_sequence || !dependents.is_truthy()? || !household.is_instance_of::<PyString>() {
        return Ok(Vec::new());
    }
    let household_text = exact_str(&household)?;
    if household_id.is_some_and(|given| given != household_text) || !get(&caregiver, "source_ids")?.is_truthy()? {
        return Ok(Vec::new());
    }
    let agents = attribute(world, intern!(py, "agents"))?;
    let mut linked: Vec<Arc<str>> = Vec::new();
    for dependent_id in dependents.try_iter()? {
        let dependent_id = dependent_id?;
        let id = exact_str(&dependent_id)?;
        let Some(dependent) = person_context(world, id)? else {
            continue;
        };
        if !agents.contains(id)?
            || !dependent.is_instance_of::<PyDict>()
            || dependent.call_method1(intern!(py, "get"), ("requires_review", false))?.is_truthy()?
            || !get(&dependent, "household_id")?.eq(&household)?
            || !dependent.call_method1(intern!(py, "get"), ("caregiver_ids", PyTuple::empty(py)))?.contains(caregiver_id)?
            || !get(&dependent, "source_ids")?.is_truthy()?
        {
            continue;
        }
        linked.push(Arc::from(id));
    }
    linked.sort_unstable();
    linked.dedup();
    Ok(linked)
}

fn body_field(record: &BodyRecord, field: &str) -> PyResult<f64> {
    match field {
        "reassurance" => Ok(record.reassurance),
        "discontent" => Ok(record.discontent),
        "uncertainty" => Ok(record.uncertainty),
        _ => unsupported("another field of the body"),
    }
}

fn set_body_field(record: &mut BodyRecord, field: &str, value: f64) -> PyResult<()> {
    match field {
        "reassurance" => record.reassurance = value,
        "discontent" => record.discontent = value,
        "uncertainty" => record.uncertainty = value,
        _ => return unsupported("another field of the body"),
    }
    Ok(())
}

/// The part of `StaffSupportCompletionHandler.apply` that a selected formula decides, for a kind other
/// than emotional support: what changes, the outcome, and the receipt's sources.
fn formula_care(
    world: &Bound<'_, PyAny>,
    formula: &Bound<'_, PyAny>,
    agent_id: &Arc<str>,
    kind: &str,
    household_id: Option<&str>,
    stress: f64,
) -> PyResult<(Care, (&'static str, V), Vec<Arc<str>>)> {
    let py = world.py();
    let result = (|| -> PyResult<_> {
        let names = &*CHANGE_NAMES;
        let hygiene = kind == "hygiene_support";
        let typed = formula_deltas(formula, kind, agent_id)?;
        let mut relational: Vec<(Arc<str>, Vec<(&'static str, f64)>)> = Vec::new();
        let mut sources = Vec::new();
        if kind == "caregiver_assistance" {
            for dependent in care_dependents(world, agent_id, household_id)? {
                let deltas = formula_deltas(formula, "caregiver_assistance", &dependent)?;
                if !deltas.is_empty() {
                    relational.push((dependent, deltas));
                }
            }
            let mut identities = vec![agent_id.clone()];
            identities.extend(relational.iter().map(|(id, _)| id.clone()));
            for identity in identities {
                if let Some(context) = person_context(world, &identity)? {
                    if context.is_instance_of::<PyDict>() {
                        let listed = context.call_method1(intern!(py, "get"), ("source_ids", PyTuple::empty(py)))?;
                        for source in listed.try_iter()? {
                            let source = source?;
                            if source.is_instance_of::<PyString>() {
                                sources.push(Arc::<str>::from(exact_str(&source)?));
                            }
                        }
                    }
                }
            }
            sources.sort_unstable();
            sources.dedup();
        }
        let mut source_ids = vec![NAMES.source.clone()];
        source_ids.extend(sources);
        let measured_kind = hygiene || !typed.is_empty();
        let body_mirror = if namespace_is_absent(world, intern!(py, BODY))? { None } else { Some(mirror_of(world, BODY)?) };
        let Some(mirror) = body_mirror else {
            if !relational.is_empty() {
                return unsupported("a dependent without a body record");
            }
            return Ok((Care::None, ("not_measured", V::List(Vec::new())), source_ids));
        };
        let store = entities_of(world)?;
        let stress_of = |id: &str| -> PyResult<f64> {
            EntityStore::with_rows(&store, |rows| rows.agents.get(id).map_or_else(|| unsupported("no such person"), |row| Ok(row.stress)))
        };
        // The people as they are, each once; sequential updates read each other's results.
        let mut order: Vec<Arc<str>> = vec![agent_id.clone()];
        for (dependent, _) in &relational {
            if !order.contains(dependent) {
                order.push(dependent.clone());
            }
        }
        let (records, own_has_body, relief) = NamespaceMirror::with_row::<BodyRow, _>(&mirror, |row| {
            let mut records = Vec::with_capacity(order.len());
            for id in &order {
                records.push(row.agents.get(&**id).copied());
            }
            let relief = config_float(&row.config, "hygiene_support_relief_by_agent", "hygiene_support_relief", agent_id)?;
            Ok((records, row.agents.get(&**agent_id).is_some(), relief))
        })?;
        if !own_has_body {
            if !relational.is_empty() {
                return unsupported("a dependent is measured without the person's body");
            }
            return Ok((Care::None, ("not_measured", V::List(Vec::new())), source_ids));
        }
        let mut people: Vec<CaredPerson> = Vec::with_capacity(order.len());
        for (id, record) in order.iter().zip(records) {
            let Some(record) = record else {
                return unsupported("a dependent without a body record");
            };
            people.push(CaredPerson { agent_id: id.clone(), stress: None, record: Some(record) });
        }
        let index_of = |id: &Arc<str>| order.iter().position(|known| known == id).expect("listed above");
        let originals: Vec<BodyRecord> = people.iter().map(|person| person.record.expect("set above")).collect();
        let mut stresses: Vec<f64> = Vec::with_capacity(order.len());
        stresses.push(stress);
        for id in &order[1..] {
            stresses.push(stress_of(id)?);
        }
        let original_stresses = stresses.clone();
        // What the Python rule records before anything changes: (kind, person, field).
        let mut before: Vec<(bool, usize, &'static str)> = Vec::new();
        if measured_kind {
            if hygiene {
                before.push((false, 0, "hygiene_need"));
            }
            for (field, _) in &typed {
                before.push((*field == "stress", 0, field));
            }
        }
        for (dependent, deltas) in &relational {
            let index = index_of(dependent);
            for (field, _) in deltas {
                before.push((*field == "stress", index, field));
            }
        }
        // The changes, in the order of the Python rule.
        let mut outcome_source = false;
        if hygiene {
            let mut record = people[0].record.expect("set above");
            record.hygiene_need = hygiene_after(&attribute(formula, intern!(py, "model_id"))?, record.hygiene_need, relief);
            people[0].record = Some(record);
            outcome_source = true;
        } else if !typed.is_empty() {
            let mut record = people[0].record.expect("set above");
            for (field, delta) in &typed {
                if *field == "stress" {
                    stresses[0] = py_min(1.0, py_max(0.0, stresses[0] + delta));
                } else {
                    let next = py_min(1.0, py_max(0.0, body_field(&record, field)? + delta));
                    set_body_field(&mut record, field, next)?;
                }
            }
            people[0].record = Some(record);
            outcome_source = true;
        }
        for (dependent, deltas) in &relational {
            let index = index_of(dependent);
            let mut record = people[index].record.expect("set above");
            for (field, delta) in deltas {
                if *field == "stress" {
                    stresses[index] = py_min(1.0, py_max(0.0, stresses[index] + delta));
                } else {
                    let next = py_min(1.0, py_max(0.0, body_field(&record, field)? + delta));
                    set_body_field(&mut record, field, next)?;
                }
            }
            people[index].record = Some(record);
            outcome_source = true;
        }
        if !measured_kind && relational.is_empty() {
            return Ok((Care::None, ("not_measured", V::List(Vec::new())), source_ids));
        }
        let mut changes = Vec::with_capacity(before.len());
        let mut changed = false;
        for (is_stress, index, field) in &before {
            let id = &order[*index];
            let (kind_name, path, was, now) = if *is_stress {
                (&names.agent, "stress".to_owned(), original_stresses[*index], stresses[*index])
            } else if *field == "hygiene_need" {
                (&names.model_state, "body_state_v1.agents.hygiene_need".to_owned(), originals[*index].hygiene_need, people[*index].record.expect("set above").hygiene_need)
            } else {
                (
                    &names.model_state,
                    format!("body_state_v1.agents.{field}"),
                    body_field(&originals[*index], field)?,
                    body_field(&people[*index].record.expect("set above"), field)?,
                )
            };
            if !was.is_finite() || !now.is_finite() {
                return unsupported("a value is not finite");
            }
            changed |= was != now;
            changes.push(change(names, kind_name, id, &path, was, now));
        }
        if changes.len() > 32 {
            return unsupported("more changes than a receipt holds");
        }
        for (index, person) in people.iter_mut().enumerate() {
            if stresses[index] != original_stresses[index] {
                person.stress = Some(stresses[index]);
            }
        }
        source_ids.push(Arc::from(BODY_SOURCE));
        if outcome_source {
            source_ids.push(Arc::from(exact_str(&attribute(formula, intern!(py, "source_id"))?)?));
        }
        Ok((
            Care::Formula { people },
            (if changed { "observed_change" } else { "observed_no_change" }, V::List(changes)),
            source_ids,
        ))
    })();
    refused_by_python(py, result)
}

/// `StaffSupportCompletionHandler.apply`, called from Python: its records, which the dispatcher
/// appends and projects.
#[pyfunction]
fn complete_support<'py>(handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let py = world.py();
    let writer = EventWriter::of(world)?;
    let Some(planned) = complete_support_event(handler, world, &writer, EventRef::Object(event))? else {
        return Ok(None);
    };
    Ok(Some(PyTuple::new(py, [transition_record(py, &planned)?])?))
}

// ---- A request, for `seek_help` and `seek_information` -----------------------------------------------

/// What `StaffService.request` will do, with nothing changed.
pub(crate) enum PlannedRequest {
    /// It raises `ValueError`: `support_request_rejected_count` is the step's answer.
    Rejected,
    Opens { request_id: Arc<str>, timeout_minute: i64, event: EventRow },
}

/// `StaffService.request`, without the world's whole validation, at this sequence. The case of a
/// Run's last minute, which closes the request at once, is the Python rule's.
pub(crate) fn plan_request(
    service: &Bound<'_, PyAny>,
    world: &Bound<'_, PyAny>,
    writer: &EventWriter<'_>,
    agent_id: &str,
    minute: i64,
) -> PyResult<PlannedRequest> {
    let py = world.py();
    let store = entities_of(world)?;
    let mut hash = Sha256::new();
    hash.update(agent_id.as_bytes());
    hash.update([0]);
    hash.update(minute.to_string().as_bytes());
    let digest = hash.finalize();
    let mut text = String::with_capacity(8 + 24);
    text.push_str("support-");
    for byte in &digest[..12] {
        text.push(char::from_digit(u32::from(byte >> 4), 16).expect("a digit"));
        text.push(char::from_digit(u32::from(byte & 15), 16).expect("a digit"));
    }
    let request_id: Arc<str> = Arc::from(text);
    let eligible = EntityStore::with_rows(&store, |rows| {
        let Some(agent) = rows.agents.get(agent_id) else {
            return unsupported("no such agent");
        };
        Ok(minute >= agent.support_cooldown_until
            && agent.support_request_id.is_none()
            && matches!(agent.activity, Activity::Idle | Activity::Resting | Activity::Sleeping)
            && rows.support_requests.get(&*request_id).is_none())
    })?;
    if !eligible {
        return Ok(PlannedRequest::Rejected);
    }
    let after = exact_int(&attribute(service, intern!(py, "unresolved_after_minutes"))?)?;
    let Some(after_end) = minute.checked_add(after) else {
        return unsupported("a minute out of range");
    };
    let timeout_minute = after_end.min(writer.duration() - 1);
    if timeout_minute == minute {
        return unsupported("a request closed at once");
    }
    let sequence = writer.next_sequence()?;
    let event_id: Arc<str> = Arc::from(format!("support-timeout-{sequence}"));
    {
        let past = writer.log().borrow();
        if past.has_event_id(&event_id) || past.has_sequence(sequence) || writer.queue().borrow().has_event(&event_id) {
            return unsupported("the event's identity is taken");
        }
    }
    let policy = attribute(service, intern!(py, "policy_version"))?;
    let names = &*NAMES;
    let mut payload = IndexMap::with_capacity(2);
    payload.insert(names.request_id.clone(), V::Str(request_id.clone()));
    payload.insert(names.agent_id.clone(), V::Str(Arc::from(agent_id)));
    let mut provenance = IndexMap::with_capacity(4);
    provenance.insert(names.source_type.clone(), V::Str(names.assumption.clone()));
    provenance.insert(names.source_id.clone(), V::Str(names.source.clone()));
    provenance.insert(names.policy_version.clone(), V::Str(Arc::from(exact_str(&policy)?)));
    provenance.insert(names.timeout_minutes.clone(), V::Int(after));
    let Some(event) = EventRow::new(
        event_id,
        EventKind::Completion,
        timeout_minute,
        20,
        sequence,
        names.event_type.clone(),
        payload,
        Some(request_id.clone()),
        None,
        provenance,
    ) else {
        return unsupported("the event's values are not what an event takes");
    };
    Ok(PlannedRequest::Opens { request_id, timeout_minute, event })
}

/// The writes of `StaffService.request` for a request that opens.
pub(crate) fn write_request(
    world: &Bound<'_, PyAny>,
    writer: &EventWriter<'_>,
    agent_id: &str,
    minute: i64,
    request_id: &Arc<str>,
    timeout_minute: i64,
    event: EventRow,
) -> PyResult<()> {
    let py = world.py();
    let store = entities_of(world)?;
    let agents = attribute(world, intern!(py, "agents"))?;
    let agent_text = PyString::new(py, agent_id);
    let Some(agent) = raw_dict_get(&agents, agent_text.as_any())? else {
        return Err(pyo3::exceptions::PyRuntimeError::new_err("a row has no object"));
    };
    let request_text = PyString::new(py, request_id);
    let request = request_class(py)?.call1((&request_text, &agent_text, minute, timeout_minute))?;
    attribute(world, intern!(py, "support_requests"))?.set_item(&request_text, request)?;
    set_agent(world, &store, agent_text.as_any(), Activity::SeekingHelp, None)?;
    EntityStore::with_rows(&store, |rows| {
        let Some(row) = rows.agents.get_mut(agent_id) else {
            return unsupported("no such agent");
        };
        row.support_request_id = Some(request_id.clone());
        Ok(())
    })?;
    raw_setattr(&agent, intern!(py, "support_request_id"), request_text.as_ptr())?;
    writer.allocate_sequence()?;
    writer.schedule(event)
}

/// `support_kind_for_agent`.
pub(crate) fn support_kind_of(world: &Bound<'_, PyAny>, writer: &EventWriter<'_>, agent_id: &str) -> PyResult<Arc<str>> {
    let py = world.py();
    let default = || Arc::<str>::from(EMOTIONAL_SUPPORT);
    let Some(person) = person_context(world, agent_id)? else {
        return Ok(default());
    };
    let Some(person) = as_readable_dict(&person) else {
        return if person.is_instance_of::<PyDict>() { unsupported("another kind of dict") } else { Ok(default()) };
    };
    let completed = writer.log().borrow().completed_kinds(agent_id).to_vec();
    let done = |kind: &str| completed.iter().any(|known| known == kind);
    if let Some(configured) = person.get_item(intern!(py, "care_support_needs"))? {
        let items: Vec<Bound<'_, PyAny>> = match (configured.downcast::<PyList>(), configured.downcast::<PyTuple>()) {
            (Ok(list), _) => list.iter().collect(),
            (_, Ok(tuple)) => tuple.iter().collect(),
            _ => Vec::new(),
        };
        for item in items {
            if item.is_exact_instance_of::<PyString>() {
                let kind = exact_str(&item)?;
                if !done(kind) {
                    return Ok(Arc::from(kind));
                }
            } else if item.is_instance_of::<PyString>() {
                return unsupported("a subclass of str");
            }
        }
    }
    let yes = pyo3::types::PyBool::new(py, true);
    let is_true = |value: Option<Bound<'_, PyAny>>| value.is_some_and(|value| value.is(&*yes));
    if is_true(person.get_item(intern!(py, "medication_support"))?) && !done("medication_support") {
        return Ok(Arc::from("medication_support"));
    }
    if is_true(person.get_item(intern!(py, "medical_support"))?) && !done("medical_assessment") {
        return Ok(Arc::from("medical_assessment"));
    }
    Ok(default())
}

/// `current_household_id`.
pub(crate) fn household_of(world: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<Option<Arc<str>>> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    if let Some(presence) = exact_dict(&table)?.get_item(intern!(py, PRESENCE))? {
        match as_readable_dict(&presence) {
            Some(presence) => {
                let Some(people) = presence.get_item(intern!(py, "people"))? else {
                    return Ok(None);
                };
                let Some(people) = as_readable_dict(&people) else {
                    return unsupported("the presence state's people are for the Python rule");
                };
                let Some(person) = people.get_item(agent_id)? else {
                    return Ok(None);
                };
                let Some(person) = as_readable_dict(&person) else {
                    return unsupported("a presence record is for the Python rule");
                };
                return match person.get_item(intern!(py, "household_id"))? {
                    Some(household) if !household.is_none() => Ok(Some(Arc::from(exact_str(&household)?))),
                    _ => Ok(None),
                };
            }
            None if presence.is_instance_of::<PyDict>() => return unsupported("another kind of dict"),
            None => {}
        }
    }
    EntityStore::with_rows(&entities_of(world)?, |rows| Ok(rows.agents.get(agent_id).and_then(|agent| agent.profile.group_id.clone())))
}

/// The metric of a request the service turned down.
pub(crate) fn add_rejected(metrics: &Bound<'_, NamespaceMirror>) -> PyResult<()> {
    add_one(metrics, "support_request_rejected_count")
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(timeout, m)?)?;
    m.add_function(wrap_pyfunction!(complete_support, m)?)?;
    m.add_function(wrap_pyfunction!(record_interrupted_step, m)?)?;
    m.add_function(wrap_pyfunction!(record_started_step, m)?)?;
    Ok(())
}
