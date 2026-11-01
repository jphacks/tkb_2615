//! The rule of `SocialInteractionHandler` in `events/handlers.py` that nearly every social event
//! takes: a social action between two people (`reassurance_exchange`, `family_care`,
//! `voluntary_support`) that goes through. It reads the event, the entity rows, `body_state_v1`
//! and `population_presence_v1`, computes, and writes the outcome to the rows and their objects.
//! It calls no Python and draws nothing.
//!
//! A social action that no longer applies is suppressed here too, with the texts of the Python rule,
//! which are the event's `detail`: each is given only after everything the Python rule asks before
//! it has passed. Everything else says Unsupported before any change, and the Python rule runs and
//! says what it says: a failed event, and the other types of interaction, which read the event log
//! (lane E) or `TypedMessage` and are 0.2% of social events at the gate
//! (docs/rust-port/lanes/m3-social-network.md, m3-dispatch.md).

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyFloat, PyString, PyType};

use crate::core::mathx::{py_max, py_min};
use crate::core::row::{attribute, exact_int, exact_str, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::raw_dict_get;
use crate::rules::social_dialogue::context_is_valid;
use crate::rules::social_network::{action_relief, body_state_of, mapping_get};
use crate::state::entities::{entities_of, raw_setattr, AgentRow, EntityStore};
use crate::Unsupported;

const SOURCE_ID: &str = "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1";
const PAYLOAD_FIELDS: usize = 8;
/// A helper's need rises by this, and the target must need this much more than the helper.
const ACTOR_FATIGUE_COST: f64 = 0.002;
const REQUIRED_NEED_MARGIN: f64 = 0.15;
const PARTICIPANT_TRUST_SHARE: f64 = 0.5;
const ACTOR_TRUST_SHARE: f64 = 0.25;
const MAX_DURATION_MINUTES: i64 = 1440;

static ENDOGENOUS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();

fn field<'py>(payload: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    mapping_get(payload, name)?.ok_or_else(|| Unsupported::new_err("the payload lacks a field"))
}

/// `current_household_id` of `domain/population_lifecycle.py`, where a person the presence state
/// does not list has none, unlike a view's `group_of`.
fn current_household(
    presence: Option<&Bound<'_, PyAny>>,
    id: &Bound<'_, PyString>,
    profile_group: &Option<Arc<str>>,
) -> PyResult<Option<Arc<str>>> {
    let Some(presence) = presence else {
        return Ok(profile_group.clone());
    };
    let py = id.py();
    let Some(people) = raw_dict_get(presence, intern!(py, "people").as_any())? else {
        return Ok(None);
    };
    if as_readable_dict(&people).is_none() {
        return unsupported("the presence state's people are not a dict");
    }
    let Some(person) = raw_dict_get(&people, id.as_any())? else {
        return Ok(None);
    };
    let Some(person) = as_readable_dict(&person) else {
        return unsupported("a presence record is not a plain dict");
    };
    match person.get_item(intern!(py, "household_id"))? {
        Some(household) if !household.is_none() => Ok(Some(Arc::from(exact_str(&household)?))),
        _ => Ok(None),
    }
}

fn immediate_need(row: &AgentRow) -> f64 {
    // max(a, b, ...) keeps the first of equal values, as the fold does.
    [row.needs.toilet_urgency, row.needs.hunger, row.needs.fatigue, row.needs.hydration_need]
        .into_iter()
        .fold(row.stress, py_max)
}

const NOT_COLOCATED: &str = "social action participants are no longer co-located";
const NOT_SAME_HOUSEHOLD: &str = "family support target is no longer in the same household";
const NOT_HIGHER_NEED: &str = "the selected person no longer has a higher immediate need";

static SUPPRESSED: GILOnceCell<Py<PyType>> = GILOnceCell::new();

/// `SocialInteractionHandler.apply`, for a social action. Raises what the Python rule raises for a
/// suppressed one; the dispatch loop calls `apply_event`, which has no exception to pay for.
#[pyfunction]
fn apply<'py>(
    _handler: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    match apply_event(world, event)? {
        None => Ok(()),
        Some(text) => {
            let class = SUPPRESSED.get_or_try_init(py, || {
                Ok::<_, PyErr>(
                    py.import("sheltersim.events.models")?.getattr("EventSuppressed")?.downcast_into::<PyType>()?.unbind(),
                )
            })?;
            Err(PyErr::from_type(class.bind(py).clone(), text))
        }
    }
}

/// The rule: None for an applied action, or the text of a suppressed one.
pub(crate) fn apply_event<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
) -> PyResult<Option<&'static str>> {
    let py = world.py();
    let payload = attribute(event, intern!(py, "payload"))?;
    let Some(interaction) = mapping_get(&payload, intern!(py, "interaction_type"))? else {
        return unsupported("an event without a typed interaction");
    };
    let interaction = exact_str(&interaction)?;
    let (expected_action, expected_topic) = match interaction {
        "reassurance_exchange" => ("offer_reassurance", "emotional_check_in"),
        "family_care" => ("provide_family_support", "family_care"),
        "voluntary_support" => ("offer_practical_help", "mutual_aid"),
        _ => return unsupported("another type of interaction"),
    };

    // The event's provenance and time.
    let endogenous = ENDOGENOUS.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import("sheltersim.events.models")?.getattr("EventKind")?.getattr("ENDOGENOUS")?.unbind())
    })?;
    if !attribute(event, intern!(py, "kind"))?.is(endogenous.bind(py)) {
        return unsupported("the event is not endogenous");
    }
    let due_object = attribute(event, intern!(py, "due_minute"))?;
    let due_minute = exact_int(&due_object)?;
    if due_minute != exact_int(&attribute(world, intern!(py, "minute_index"))?)? {
        return unsupported("the event is not due now");
    }
    let provenance = attribute(event, intern!(py, "provenance"))?;
    let source = field(&provenance, intern!(py, "source_id"))?;
    let source_type = field(&provenance, intern!(py, "source_type"))?;
    let version = field(&provenance, intern!(py, "policy_version"))?;
    let event_source = attribute(event, intern!(py, "source_id"))?;
    if exact_str(&source)? != SOURCE_ID
        || exact_str(&source_type)? != "assumption"
        || exact_str(&event_source)? != exact_str(&version)?
    {
        return unsupported("the event's provenance is for the Python rule to judge");
    }

    // The payload's fields.
    let actor_object = field(&payload, intern!(py, "agent_id"))?;
    let actor_id = exact_str(&actor_object)?;
    let actor_key = actor_object.downcast_exact::<PyString>()?;
    let participant_object = field(&payload, intern!(py, "participant_agent_id"))?;
    let participant_id = exact_str(&participant_object)?;
    let participant_key = participant_object.downcast_exact::<PyString>()?;
    if actor_id == participant_id {
        return unsupported("an action of a person on themselves");
    }
    let context = mapping_get(&payload, intern!(py, "conversation_state_context"))?;
    if payload.len()? != PAYLOAD_FIELDS + context.is_some() as usize {
        return unsupported("the payload has other fields");
    }
    let zone = field(&payload, intern!(py, "zone_id"))?;
    let zone = exact_str(&zone)?;
    let action = field(&payload, intern!(py, "action"))?;
    let topic = field(&payload, intern!(py, "topic_id"))?;
    let reason = field(&payload, intern!(py, "reason_short"))?;
    if exact_str(&action)? != expected_action
        || exact_str(&topic)? != expected_topic
        || exact_str(&reason)? != "autonomous_need_choice"
    {
        return unsupported("the action does not match its type");
    }
    let duration = exact_int(&field(&payload, intern!(py, "interaction_duration_minutes"))?)?;
    if !(1..=MAX_DURATION_MINUTES).contains(&duration) {
        return unsupported("the duration is out of bounds");
    }
    if let Some(context) = &context {
        if !context_is_valid(context, &[actor_key, participant_key], due_minute)? {
            return unsupported("the context is for the Python rule to judge");
        }
    }

    // What the rule reads outside the rows.
    let table = attribute(world, intern!(py, "model_state"))?;
    let Some(table_dict) = as_readable_dict(&table) else {
        return unsupported("model_state is not readable");
    };
    let presence = if interaction == "family_care" {
        match table_dict.get_item(intern!(py, "population_presence_v1"))? {
            Some(state) if as_readable_dict(&state).is_some() => Some(state),
            Some(state) if state.is_instance_of::<pyo3::types::PyDict>() => {
                return unsupported("the presence state is another kind of dict");
            }
            _ => None,
        }
    } else {
        None
    };
    let body = body_state_of(world)?;
    let relief = action_relief(body.as_ref(), actor_key)?;
    let helps = interaction != "reassurance_exchange";

    // The rows: read, decide, write. No Python code runs in here.
    let store = entities_of(world)?;
    let household_groups = EntityStore::with_rows(&store, |rows| {
        let (Some(actor), Some(participant)) = (rows.agents.get(actor_id), rows.agents.get(participant_id)) else {
            return unsupported("an unknown person");
        };
        if actor.profile.id == participant.profile.id {
            return unsupported("two people of one profile");
        }
        if &*actor.zone_id != zone || participant.zone_id != actor.zone_id {
            return Ok(None);
        }
        Ok(Some((actor.profile.group_id.clone(), participant.profile.group_id.clone())))
    })?;
    let Some(household_groups) = household_groups else {
        return Ok(Some(NOT_COLOCATED));
    };
    if interaction == "family_care" {
        let actor_home = current_household(presence.as_ref(), actor_key, &household_groups.0)?;
        let participant_home = current_household(presence.as_ref(), participant_key, &household_groups.1)?;
        if actor_home.is_none() || actor_home != participant_home {
            return Ok(Some(NOT_SAME_HOUSEHOLD));
        }
    }
    let values = EntityStore::with_rows(&store, |rows| {
        let (Some(actor), Some(participant)) = (rows.agents.get(actor_id), rows.agents.get(participant_id)) else {
            return unsupported("an unknown person");
        };
        if interaction == "voluntary_support"
            && immediate_need(participant) <= immediate_need(actor) + REQUIRED_NEED_MARGIN
        {
            return Ok(None);
        }
        let stress = py_min(1.0, py_max(0.0, participant.stress - relief));
        let trust = py_min(1.0, py_max(0.0, participant.trust + relief * PARTICIPANT_TRUST_SHARE));
        let fatigue = py_min(1.0, actor.needs.fatigue + ACTOR_FATIGUE_COST);
        let actor_trust = py_min(1.0, actor.trust + py_max(0.0, relief * ACTOR_TRUST_SHARE));
        let participant = rows.agents.get_mut(participant_id).expect("found above");
        participant.stress = stress;
        participant.trust = trust;
        if helps {
            let actor = rows.agents.get_mut(actor_id).expect("found above");
            actor.needs.fatigue = fatigue;
            actor.trust = actor_trust;
        }
        Ok(Some((stress, trust, fatigue, actor_trust)))
    })?;
    let Some(values) = values else {
        return Ok(Some(NOT_HIGHER_NEED));
    };

    // The objects of the rows.
    let agents = world.getattr(intern!(py, "agents"))?;
    let object_of = |key: &Bound<'py, PyString>| -> PyResult<Bound<'py, PyAny>> {
        raw_dict_get(&agents, key.as_any())?
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("a row has no object"))
    };
    let participant = object_of(participant_key)?;
    raw_setattr(&participant, intern!(py, "stress"), PyFloat::new(py, values.0).as_ptr())?;
    raw_setattr(&participant, intern!(py, "trust"), PyFloat::new(py, values.1).as_ptr())?;
    if helps {
        let actor = object_of(actor_key)?;
        let needs = actor.getattr(intern!(py, "needs"))?;
        raw_setattr(&needs, intern!(py, "fatigue"), PyFloat::new(py, values.2).as_ptr())?;
        raw_setattr(&actor, intern!(py, "trust"), PyFloat::new(py, values.3).as_ptr())?;
    }
    Ok(None)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(apply, m)?)?;
    Ok(())
}
