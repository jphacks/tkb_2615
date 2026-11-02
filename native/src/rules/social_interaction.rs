//! The rule of `SocialInteractionHandler` in `events/handlers.py` that nearly every social event
//! takes: a social action between two people (`reassurance_exchange`, `family_care`,
//! `voluntary_support`) that goes through. It reads the event, the entity rows, `body_state_v1`
//! and `population_presence_v1`, computes, and writes the outcome to the rows and their objects.
//! It calls no Python and draws nothing.
//!
//! A social action that no longer applies is suppressed here too, with the texts of the Python rule,
//! which are the event's `detail`: each is given only after everything the Python rule asks before
//! it has passed. Everything else says Unsupported before any change, and the Python rule runs and
//! says what it says: a failed event. The other types of interaction (`complaint_response`,
//! `cooperation`, `information_reaction`, an untyped event) are at the end, and say Unsupported for
//! whatever isn't applied (docs/rust-port/lanes/m3-social-network.md, m3-dispatch.md,
//! m6-handlers-rest.md).

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyFloat, PyString, PyType};

use crate::core::mathx::{py_max, py_min};
use crate::core::row::{attribute, exact_float, exact_int, exact_str, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::{raw_dict_get, NamespaceMirror};
use crate::rules::handlers::{get, get_is, kind_is, number, status_member, text_is};
use crate::rules::information_events::runtime_state;
use crate::rules::social_dialogue::context_is_valid;
use crate::rules::social_network::{action_relief, body_state_of, mapping_get};
use crate::rules::support::household_of;
use crate::state::information::InformationRow;
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
    let interaction = mapping_get(&payload, intern!(py, "interaction_type"))?.filter(|value| !value.is_none());
    let Some(interaction) = interaction else {
        return untyped_event(world, &payload).map(|()| None);
    };
    let interaction = exact_str(&interaction)?;
    let (expected_action, expected_topic) = match interaction {
        "reassurance_exchange" => ("offer_reassurance", "emotional_check_in"),
        "family_care" => ("provide_family_support", "family_care"),
        "voluntary_support" => ("offer_practical_help", "mutual_aid"),
        "complaint_response" | "cooperation" | "information_reaction" => {
            return other_typed_event(world, event, &payload, interaction).map(|()| None);
        }
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

// ---- The other social events -------------------------------------------------------------------------
//
// Each is rare, reads Python's containers and the event log, and says Unsupported for anything but an
// applied event, so its suppressed and failed texts stay the Python rule's.

const REASSURANCE_STRESS: f64 = 0.03;
const REASSURANCE_TRUST: f64 = 0.01;
const PRACTICAL_TRUST: f64 = 0.005;
const COMPLAINT_ACKNOWLEDGE_STRESS: f64 = 0.02;
const COMPLAINT_EXPLAIN_STRESS: f64 = 0.01;
const COMPLAINT_TRUST: f64 = 0.01;
const COOPERATION_STRESS: f64 = 0.02;
const COOPERATION_TRUST: f64 = 0.01;

fn text_field<'py>(payload: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    let value = field(payload, name)?;
    if !exact_str(&value)?.is_empty() {
        return Ok(value);
    }
    unsupported("an empty ID")
}

fn stress_and_trust(agent: &Bound<'_, PyAny>) -> PyResult<(f64, f64)> {
    let py = agent.py();
    Ok((exact_float(&attribute(agent, intern!(py, "stress"))?)?, exact_float(&attribute(agent, intern!(py, "trust"))?)?))
}

/// `SocialInteractionHandler.apply` of an event without a typed interaction.
fn untyped_event(world: &Bound<'_, PyAny>, payload: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    let agent_id = text_field(payload, intern!(py, "agent_id"))?;
    let request_id = text_field(payload, intern!(py, "request_id"))?;
    let action = text_field(payload, intern!(py, "action"))?;
    let request = get(&attribute(world, intern!(py, "support_requests"))?, exact_str(&request_id)?)?;
    let agent = get(&attribute(world, intern!(py, "agents"))?, exact_str(&agent_id)?)?;
    if request.is_none() || agent.is_none() || !attribute(&request, intern!(py, "agent_id"))?.eq(&agent_id)? {
        return unsupported("the request or person is for the Python rule to judge");
    }
    let (stress, trust) = stress_and_trust(&agent)?;
    match exact_str(&action)? {
        "offer_reassurance" => {
            agent.setattr(intern!(py, "stress"), py_max(0.0, stress - REASSURANCE_STRESS))?;
            agent.setattr(intern!(py, "trust"), py_min(1.0, trust + REASSURANCE_TRUST))?;
        }
        "share_practical_information" => agent.setattr(intern!(py, "trust"), py_min(1.0, trust + PRACTICAL_TRUST))?,
        _ => return unsupported("the action is for the Python rule to judge"),
    }
    Ok(())
}

/// `event.payload` holds exactly these fields, with or without `conversation_state_context`.
fn typed_fields(payload: &Bound<'_, PyAny>, names: &[&str]) -> PyResult<bool> {
    let extra = usize::from(payload.contains("conversation_state_context")?);
    if payload.len()? != names.len() + extra {
        return Ok(false);
    }
    for name in names {
        if !payload.contains(*name)? {
            return Ok(false);
        }
    }
    Ok(true)
}

fn threshold_of(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    let threshold = number(value)?;
    if !(0.0..=1.0).contains(&threshold) {
        return unsupported("the threshold is invalid");
    }
    Ok(threshold)
}

/// The applied contact that an interaction answers, from the event log.
fn contact_matches(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, trigger: &Bound<'_, PyAny>, sender: &Bound<'_, PyAny>, recipient: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    exact_str(trigger)?;
    let contact = attribute(world, intern!(py, "event_log"))?.call_method1(intern!(py, "find"), (trigger,))?;
    if contact.is_none() || !attribute(&contact, intern!(py, "status"))?.is(&status_member(py, "APPLIED")?) {
        return Ok(false);
    }
    let recorded = attribute(&contact, intern!(py, "event"))?;
    let recorded_payload = attribute(&recorded, intern!(py, "payload"))?;
    Ok(text_is(&attribute(&recorded, intern!(py, "event_type"))?, "information_contact_update")
        && exact_int(&attribute(&contact, intern!(py, "realized_minute"))?)? == exact_int(&attribute(event, intern!(py, "due_minute"))?)? - 1
        && get(&recorded_payload, "sender_agent_id")?.eq(sender)?
        && get(&recorded_payload, "recipient_agent_id")?.eq(recipient)?)
}

/// `_apply_typed_interaction` of `complaint_response`, `cooperation` and `information_reaction`.
fn other_typed_event(world: &Bound<'_, PyAny>, event: &Bound<'_, PyAny>, payload: &Bound<'_, PyAny>, interaction: &str) -> PyResult<()> {
    let py = world.py();
    let provenance = attribute(event, intern!(py, "provenance"))?;
    let due = exact_int(&attribute(event, intern!(py, "due_minute"))?)?;
    if !kind_is(event, "ENDOGENOUS")?
        || due != exact_int(&attribute(world, intern!(py, "minute_index"))?)?
        || !get_is(&provenance, "source_id", SOURCE_ID)?
        || !get_is(&provenance, "source_type", "assumption")?
        || !attribute(event, intern!(py, "source_id"))?.eq(get(&provenance, "policy_version")?)?
    {
        return unsupported("the provenance or time is for the Python rule to judge");
    }
    let reason = get(payload, "reason_short")?;
    let agent_id = get(payload, "agent_id")?;
    let agent_text = exact_str(&agent_id)?;
    let agents = attribute(world, intern!(py, "agents"))?;
    let agent = get(&agents, agent_text)?;
    if reason.is_none() || agent.is_none() {
        return unsupported("the event is for the Python rule to judge");
    }
    let context = mapping_get(payload, intern!(py, "conversation_state_context"))?;
    let participant_id = get(payload, "participant_agent_id")?;
    if let Some(context) = &context {
        let actor_key = agent_id.downcast_exact::<PyString>()?;
        let valid = match participant_id.downcast_exact::<PyString>() {
            Ok(participant_key) => context_is_valid(context, &[actor_key, participant_key], due)?,
            Err(_) if participant_id.is_instance_of::<PyString>() => return unsupported("a subclass of str"),
            Err(_) => context_is_valid(context, &[actor_key], due)?,
        };
        if !valid {
            return unsupported("the context is for the Python rule to judge");
        }
    }
    let action = get(payload, "action")?;
    let (stress, trust) = stress_and_trust(&agent)?;
    let zone = attribute(&agent, intern!(py, "zone_id"))?;
    match interaction {
        "complaint_response" => {
            if !typed_fields(payload, &["interaction_type", "agent_id", "request_id", "zone_id", "stress_threshold", "complaint_wait_minutes", "action", "reason_short"])? {
                return unsupported("the payload has other fields");
            }
            let request_id = payload.get_item("request_id")?;
            let threshold = threshold_of(&payload.get_item("stress_threshold")?)?;
            let wait = exact_int(&payload.get_item("complaint_wait_minutes")?)?;
            let request = attribute(world, intern!(py, "support_requests"))?.call_method1(intern!(py, "get"), (&request_id,))?;
            if wait <= 0
                || request.is_none()
                || !attribute(&request, intern!(py, "agent_id"))?.eq(&agent_id)?
                || !text_is(&attribute(&request, intern!(py, "status"))?.getattr(intern!(py, "value"))?, "pending")
                || !payload.get_item("zone_id")?.eq(&zone)?
                || !attribute(event, intern!(py, "causation_id"))?.eq(&request_id)?
                || due - exact_int(&attribute(&request, intern!(py, "requested_minute"))?)? < wait
                || stress < threshold
                || !matches!(exact_str(&reason)?, "long_wait" | "high_stress" | "request_still_pending" | "rule_fallback")
            {
                return unsupported("the complaint is for the Python rule to judge");
            }
            match exact_str(&action)? {
                "acknowledge_complaint" => {
                    agent.setattr(intern!(py, "stress"), py_max(0.0, stress - COMPLAINT_ACKNOWLEDGE_STRESS))?;
                    agent.setattr(intern!(py, "trust"), py_min(1.0, trust + COMPLAINT_TRUST))?;
                }
                "explain_staff_process" => {
                    agent.setattr(intern!(py, "stress"), py_max(0.0, stress - COMPLAINT_EXPLAIN_STRESS))?;
                    agent.setattr(intern!(py, "trust"), py_min(1.0, trust + COMPLAINT_TRUST))?;
                }
                "share_practical_information" => agent.setattr(intern!(py, "trust"), py_min(1.0, trust + PRACTICAL_TRUST))?,
                _ => return unsupported("the action is for the Python rule to judge"),
            }
            Ok(())
        }
        "cooperation" => {
            if !typed_fields(payload, &["interaction_type", "agent_id", "participant_agent_id", "zone_id", "stress_threshold", "relationship", "trigger_event_id", "action", "reason_short"])? {
                return unsupported("the payload has other fields");
            }
            let threshold = threshold_of(&payload.get_item("stress_threshold")?)?;
            let participant_text = exact_str(&participant_id)?;
            let participant = get(&agents, participant_text)?;
            if participant.is_none() || participant_text == agent_text {
                return unsupported("cooperation requires two known people");
            }
            let (other_stress, other_trust) = stress_and_trust(&participant)?;
            let home = household_of(world, agent_text)?;
            let trigger = payload.get_item("trigger_event_id")?;
            if home.is_none()
                || home != household_of(world, participant_text)?
                || !payload.get_item("zone_id")?.eq(&zone)?
                || !zone.eq(attribute(&participant, intern!(py, "zone_id"))?)?
                || !get_is(payload, "relationship", "same_group")?
                || !attribute(event, intern!(py, "causation_id"))?.eq(&trigger)?
                || !contact_matches(world, event, &trigger, &agent_id, &participant_id)?
                || py_max(stress, other_stress) < threshold
                || !matches!(exact_str(&reason)?, "shared_group_contact" | "elevated_stress" | "rule_fallback")
                || !text_is(&action, "offer_mutual_support")
            {
                return unsupported("the cooperation is for the Python rule to judge");
            }
            agent.setattr(intern!(py, "stress"), py_max(0.0, stress - COOPERATION_STRESS))?;
            participant.setattr(intern!(py, "stress"), py_max(0.0, other_stress - COOPERATION_STRESS))?;
            agent.setattr(intern!(py, "trust"), py_min(1.0, trust + COOPERATION_TRUST))?;
            participant.setattr(intern!(py, "trust"), py_min(1.0, other_trust + COOPERATION_TRUST))?;
            Ok(())
        }
        _ => {
            if !typed_fields(payload, &["interaction_type", "agent_id", "participant_agent_id", "zone_id", "message_id", "trigger_event_id", "action", "reason_short"])? {
                return unsupported("the payload has other fields");
            }
            let participant_text = exact_str(&participant_id)?;
            let participant = get(&agents, participant_text)?;
            let trigger = payload.get_item("trigger_event_id")?;
            if participant.is_none()
                || participant_text == agent_text
                || !payload.get_item("zone_id")?.eq(&zone)?
                || !zone.eq(attribute(&participant, intern!(py, "zone_id"))?)?
                || !attribute(event, intern!(py, "causation_id"))?.eq(&trigger)?
                || !contact_matches(world, event, &trigger, &participant_id, &agent_id)?
            {
                return unsupported("the reaction is for the Python rule to judge");
            }
            let (config, state, mirror) = runtime_state(world)?;
            let message_value = payload.get_item("message_id")?;
            let message_id = exact_str(&message_value)?;
            let pressure = NamespaceMirror::with_row::<InformationRow, _>(&mirror, |row| {
                let (Some(message), Some(belief)) = (row.messages.get(message_id), row.beliefs.get(agent_text).and_then(|beliefs| beliefs.get(message_id))) else {
                    return unsupported("the claim is no longer current");
                };
                if !message.parses() || !belief.parses() || &*message.truth_status != "false" || &*belief.belief != "heard" {
                    return unsupported("the claim is for the Python rule to judge");
                }
                row.query_pressure.get(agent_text).copied().map_or_else(|| unsupported("the person has no record"), Ok)
            })?;
            if !text_is(&action, "seek_claim_verification") || !matches!(exact_str(&reason)?, "heard_through_contact" | "request_verification" | "rule_fallback") {
                return unsupported("the reaction is for the Python rule to judge");
            }
            let gain = number(&config.getattr(intern!(py, "query_pressure_per_claim"))?)?;
            state.get_item("query_pressure")?.set_item(agent_text, py_min(1.0, pressure + gain))?;
            Ok(())
        }
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(apply, m)?)?;
    Ok(())
}
