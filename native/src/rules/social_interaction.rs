//! The rule of `SocialInteractionHandler` in `events/handlers.py` that nearly every social event
//! takes: a social action between two people (`reassurance_exchange`, `family_care`,
//! `voluntary_support`). It reads the event, the entity rows, `body_state_v1` and
//! `population_presence_v1`, computes, and writes the outcome to the rows and their objects. It calls
//! no Python and draws nothing.
//!
//! An event that does not apply gets the Python rule's text, which is saved as the event's `detail`,
//! from the Python rule's first failing check (docs/rust-port/lanes/l8-texts-events.md). The action
//! runs every tick, so on the way to an applied action its checks keep their cheap order; Python's
//! order comes out because each text is given only once every check the Python rule makes before it
//! has passed: the Agent's lookup, which the rows answer, is asked of the world for a text that comes
//! earlier. Unsupported comes before any change, for what Rust cannot decide as Python does (a
//! subclass, a state of another shape); the Python rule then runs and says what it says. The other
//! types of interaction (`complaint_response`, `cooperation`, `information_reaction`, an untyped
//! event) are at the end (docs/rust-port/lanes/m3-social-network.md, m3-dispatch.md,
//! m6-handlers-rest.md).

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyFloat, PyFrozenSet, PyInt, PyString};

use crate::core::errors::{self, Outcome};
use crate::core::mathx::{py_max, py_min};
use crate::core::row::{attribute, exact_float, exact_int, exact_str, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::{mirror_of, raw_dict_get, NamespaceMirror};
use crate::rules::handlers::{get, kind_is, number, status_member};
use crate::rules::social_dialogue::{context_is_valid, context_verdict, is_other_json_value, is_plain_mapping};
use crate::rules::social_network::{action_relief, body_state_of, mapping_get};
use crate::rules::support::household_of;
use crate::state::entities::{entities_of, raw_setattr, AgentRow, EntityStore};
use crate::state::information::{InformationRow, NAME as INFORMATION, SCHEMA_VERSION as INFORMATION_VERSION};
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

/// What the rule says of an event that does not apply: its outcome and text. None: it applied.
pub(crate) type Said = Option<(Outcome, &'static str)>;

/// A failed event, of a text noted where it is given (`errors::noted`).
fn failure(text: &'static str) -> PyResult<Said> {
    Ok(Some((Outcome::Failed, text)))
}

/// A suppressed event, of a text noted where it is given.
fn suppression(text: &'static str) -> PyResult<Said> {
    Ok(Some((Outcome::Suppressed, text)))
}

fn field<'py>(payload: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    mapping_get(payload, name)?.ok_or_else(|| Unsupported::new_err("the payload lacks a field"))
}

/// A payload value as `==` with a str sees it: the text of an exact str, None for another JSON value
/// (no str equals it), Unsupported for a value whose comparison is its own (a str subclass).
fn text_of<'a>(value: &'a Bound<'_, PyAny>) -> PyResult<Option<&'a str>> {
    if value.is_exact_instance_of::<PyString>() {
        return exact_str(value).map(Some);
    }
    if is_other_json_value(value) || is_plain_mapping(value)? {
        return Ok(None);
    }
    unsupported("a value compares in its own way")
}

/// `type(value) is int and 1 <= value <= 1440`. An int past 64 bits is outside.
fn is_duration(value: &Bound<'_, PyAny>) -> bool {
    value
        .downcast_exact::<PyInt>()
        .is_ok_and(|minutes| minutes.extract::<i64>().is_ok_and(|minutes| (1..=MAX_DURATION_MINUTES).contains(&minutes)))
}

/// `world.agents.get(agent_id) is not None`, for a text the Python rule gives after that check but
/// Rust before it reads the rows.
fn agent_is_known(world: &Bound<'_, PyAny>, key: &Bound<'_, PyString>) -> PyResult<bool> {
    let agents = attribute(world, intern!(world.py(), "agents"))?;
    let Some(agents) = as_readable_dict(&agents) else {
        return unsupported("the Agents are not a plain dict");
    };
    Ok(raw_dict_get(agents.as_any(), key.as_any())?.is_some())
}

/// Whether the dialogue context is the one `validate_social_state_context` accepts: the quick
/// answer first, the Python rule's in full only when that has none.
fn context_holds(context: &Bound<'_, PyAny>, ids: &[&Bound<'_, PyString>], due_minute: i64) -> PyResult<bool> {
    Ok(context_is_valid(context, ids, due_minute)? || context_verdict(context, ids, due_minute)?)
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

const BAD_TIME: &str = "typed social interaction provenance or time is invalid";
const NO_REASON: &str = "typed social interaction requires a reason code";
const UNKNOWN_AGENT: &str = "typed social interaction references an unknown Agent";
const BAD_CONTEXT: &str = "social dialogue state context is invalid";
const NOT_ALLOWED: &str = "typed social interaction type is not allowed";
const ACTION_FIELDS: &str = "autonomous social action payload fields do not match";
const NOT_COLOCATED: &str = "social action participants are no longer co-located";
const OFF_TOPIC: &str = "social action does not match its typed topic";
const ACTION_REASON: &str = "social action reason code is not allowed";
const ACTION_DURATION: &str = "social action duration must be within 1..1440 minutes";
const NOT_SAME_HOUSEHOLD: &str = "family support target is no longer in the same household";
const NOT_HIGHER_NEED: &str = "the selected person no longer has a higher immediate need";

/// `SocialInteractionHandler.apply`. Raises what the Python rule raises for an event that does not
/// apply; the dispatch loop calls `apply_event`, which has no exception to pay for.
#[pyfunction]
fn apply<'py>(
    _handler: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    match apply_event(world, event)? {
        None => Ok(()),
        Some((outcome, text)) => errors::raise_noted(py, outcome, text),
    }
}

/// The rule: None for an applied event, or the outcome and text of one that does not apply.
pub(crate) fn apply_event<'py>(world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<Said> {
    let py = world.py();
    let payload = attribute(event, intern!(py, "payload"))?;
    let interaction = mapping_get(&payload, intern!(py, "interaction_type"))?.filter(|value| !value.is_none());
    let Some(interaction) = interaction else {
        return untyped_event(world, &payload);
    };
    let kind = if interaction.is_exact_instance_of::<PyString>() { Some(exact_str(&interaction)?) } else { None };
    let (expected_action, expected_topic) = match kind {
        Some("reassurance_exchange") => ("offer_reassurance", "emotional_check_in"),
        Some("family_care") => ("provide_family_support", "family_care"),
        Some("voluntary_support") => ("offer_practical_help", "mutual_aid"),
        _ => return other_typed_event(world, event, &payload, &interaction, kind),
    };
    let interaction = kind.expect("one of the three");

    // The event's provenance and time.
    let endogenous = ENDOGENOUS.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import("sheltersim.events.models")?.getattr("EventKind")?.getattr("ENDOGENOUS")?.unbind())
    })?;
    if !attribute(event, intern!(py, "kind"))?.is(endogenous.bind(py)) {
        return failure(errors::noted(Outcome::Failed, BAD_TIME));
    }
    let due_object = attribute(event, intern!(py, "due_minute"))?;
    let due_minute = exact_int(&due_object)?;
    if due_minute != exact_int(&attribute(world, intern!(py, "minute_index"))?)? {
        return failure(errors::noted(Outcome::Failed, BAD_TIME));
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
        return failure(errors::noted(Outcome::Failed, BAD_TIME));
    }

    // The reason code, then the Agent: here only that it is a str. The rows say below whether it is
    // known; before that, a text the Python rule gives after this check first asks `agent_is_known`.
    let Some(reason) = mapping_get(&payload, intern!(py, "reason_short"))?.filter(|value| !value.is_none()) else {
        return failure(errors::noted(Outcome::Failed, NO_REASON));
    };
    let Some(actor_object) = mapping_get(&payload, intern!(py, "agent_id"))? else {
        return failure(errors::noted(Outcome::Failed, UNKNOWN_AGENT));
    };
    let Some(actor_id) = errors::str_of(&actor_object)? else {
        return failure(errors::noted(Outcome::Failed, UNKNOWN_AGENT));
    };
    let actor_key = actor_object.downcast_exact::<PyString>()?;
    let participant_object = mapping_get(&payload, intern!(py, "participant_agent_id"))?;
    let participant_id = match &participant_object {
        Some(value) => errors::str_of(value)?,
        None => None,
    };

    // The dialogue context, of the actor and of the other person if that is a str.
    let context = mapping_get(&payload, intern!(py, "conversation_state_context"))?;
    if let Some(context) = &context {
        let holds = match (participant_id, &participant_object) {
            (Some(id), Some(other)) if id != actor_id => context_holds(context, &[actor_key, other.downcast_exact::<PyString>()?], due_minute)?,
            _ => context_holds(context, &[actor_key], due_minute)?,
        };
        if !holds {
            if !agent_is_known(world, actor_key)? {
                return failure(errors::noted(Outcome::Failed, UNKNOWN_AGENT));
            }
            return failure(errors::noted(Outcome::Failed, BAD_CONTEXT));
        }
    }

    // The payload's fields: these eight, and the context if there is one.
    let zone = mapping_get(&payload, intern!(py, "zone_id"))?;
    let topic = mapping_get(&payload, intern!(py, "topic_id"))?;
    let action = mapping_get(&payload, intern!(py, "action"))?;
    let duration = mapping_get(&payload, intern!(py, "interaction_duration_minutes"))?;
    let counted = participant_object.is_some() && payload.len()? == PAYLOAD_FIELDS + usize::from(context.is_some());
    let (Some(zone), Some(topic), Some(action), Some(duration), true) = (zone, topic, action, duration, counted) else {
        if !agent_is_known(world, actor_key)? {
            return failure(errors::noted(Outcome::Failed, UNKNOWN_AGENT));
        }
        return failure(errors::noted(Outcome::Failed, ACTION_FIELDS));
    };
    // What the later checks compare; Unsupported here for a value whose comparison is its own.
    let zone = text_of(&zone)?;
    let topic = text_of(&topic)?;
    let action = text_of(&action)?;
    let reason = text_of(&reason)?;
    let duration_holds = is_duration(&duration);

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
    enum Pair {
        Together(Option<Arc<str>>, Option<Arc<str>>),
        Apart,
        NoActor,
        NoParticipant,
    }
    let store = entities_of(world)?;
    let pair = EntityStore::with_rows(&store, |rows| {
        let Some(actor) = rows.agents.get(actor_id) else {
            return Ok(Pair::NoActor);
        };
        let Some(participant_id) = participant_id else {
            return Ok(Pair::Apart);
        };
        let Some(participant) = rows.agents.get(participant_id) else {
            return Ok(Pair::NoParticipant);
        };
        if participant_id == actor_id {
            // `participant.id == agent.id` of one object.
            return Ok(Pair::Apart);
        }
        if actor.profile.id == participant.profile.id {
            return unsupported("two people of one profile");
        }
        if zone != Some(&*actor.zone_id) || participant.zone_id != actor.zone_id {
            return Ok(Pair::Apart);
        }
        Ok(Pair::Together(actor.profile.group_id.clone(), participant.profile.group_id.clone()))
    })?;
    let household_groups = match pair {
        Pair::Together(actor, participant) => (actor, participant),
        Pair::Apart => return suppression(errors::noted(Outcome::Suppressed, NOT_COLOCATED)),
        Pair::NoActor if agent_is_known(world, actor_key)? => return unsupported("the rows lack a person of the world"),
        Pair::NoActor => return failure(errors::noted(Outcome::Failed, UNKNOWN_AGENT)),
        Pair::NoParticipant => {
            let other = participant_object.as_ref().expect("a str").downcast_exact::<PyString>()?;
            if agent_is_known(world, other)? {
                return unsupported("the rows lack a person of the world");
            }
            return suppression(errors::noted(Outcome::Suppressed, NOT_COLOCATED));
        }
    };
    if action != Some(expected_action) || topic != Some(expected_topic) {
        return failure(errors::noted(Outcome::Failed, OFF_TOPIC));
    }
    if reason != Some("autonomous_need_choice") {
        return failure(errors::noted(Outcome::Failed, ACTION_REASON));
    }
    if !duration_holds {
        return failure(errors::noted(Outcome::Failed, ACTION_DURATION));
    }
    let participant_key = participant_object.as_ref().expect("a str").downcast_exact::<PyString>()?;
    if interaction == "family_care" {
        let actor_home = current_household(presence.as_ref(), actor_key, &household_groups.0)?;
        let participant_home = current_household(presence.as_ref(), participant_key, &household_groups.1)?;
        if actor_home.is_none() || actor_home != participant_home {
            return suppression(errors::noted(Outcome::Suppressed, NOT_SAME_HOUSEHOLD));
        }
    }
    let participant_id = participant_id.expect("a str");
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
        return suppression(errors::noted(Outcome::Suppressed, NOT_HIGHER_NEED));
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
// Each is rare, reads Python's containers and the event log, and checks in the Python rule's order
// with Python's comparisons, so each text comes where the Python rule's does.

const REASSURANCE_STRESS: f64 = 0.03;
const REASSURANCE_TRUST: f64 = 0.01;
const PRACTICAL_TRUST: f64 = 0.005;
const COMPLAINT_ACKNOWLEDGE_STRESS: f64 = 0.02;
const COMPLAINT_EXPLAIN_STRESS: f64 = 0.01;
const COMPLAINT_TRUST: f64 = 0.01;
const COOPERATION_STRESS: f64 = 0.02;
const COOPERATION_TRUST: f64 = 0.01;

fn stress_and_trust(agent: &Bound<'_, PyAny>) -> PyResult<(f64, f64)> {
    let py = agent.py();
    Ok((exact_float(&attribute(agent, intern!(py, "stress"))?)?, exact_float(&attribute(agent, intern!(py, "trust"))?)?))
}

/// `value not in {names}`, as Python's set decides it.
fn not_in(value: &Bound<'_, PyAny>, names: &[&str]) -> PyResult<bool> {
    Ok(!PyFrozenSet::new(value.py(), names)?.contains(value)?)
}

/// `mapping.get(key)` of any key.
fn get_any<'py>(mapping: &Bound<'py, PyAny>, key: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    mapping.call_method1(intern!(mapping.py(), "get"), (key,))
}

/// `world.agents.get(value) if isinstance(value, str) else None`.
fn agent_of<'py>(world: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    match errors::str_of(value)? {
        Some(id) => get(&attribute(world, intern!(py, "agents"))?, id),
        None => Ok(py.None().into_bound(py)),
    }
}

/// `isinstance(stress_threshold, (int, float)) and not isinstance(it, bool) and 0.0 <= it <= 1.0`.
fn is_threshold(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    Ok(errors::real_number(value)?.is_some_and(|threshold| (0.0..=1.0).contains(&threshold)))
}

const UNTYPED_IDS: &str = "social interaction requires Agent, request, and action IDs";
const UNTYPED_UNKNOWN: &str = "social interaction references an unknown request or Agent";
const UNTYPED_ACTION: &str = "social interaction action is not allowed";

/// `SocialInteractionHandler.apply` of an event without a typed interaction.
fn untyped_event(world: &Bound<'_, PyAny>, payload: &Bound<'_, PyAny>) -> PyResult<Said> {
    let py = world.py();
    let agent_id = get(payload, "agent_id")?;
    let request_id = get(payload, "request_id")?;
    let action = get(payload, "action")?;
    // `all(isinstance(item, str) and item for item in (agent_id, request_id, action))`
    let Some(agent_text) = errors::nonempty_str(&agent_id)? else {
        return failure(errors::noted(Outcome::Failed, UNTYPED_IDS));
    };
    let Some(request_text) = errors::nonempty_str(&request_id)? else {
        return failure(errors::noted(Outcome::Failed, UNTYPED_IDS));
    };
    let Some(action) = errors::nonempty_str(&action)? else {
        return failure(errors::noted(Outcome::Failed, UNTYPED_IDS));
    };
    let request = get(&attribute(world, intern!(py, "support_requests"))?, request_text)?;
    let agent = get(&attribute(world, intern!(py, "agents"))?, agent_text)?;
    if request.is_none() || attribute(&request, intern!(py, "agent_id"))?.ne(&agent_id)? || agent.is_none() {
        return failure(errors::noted(Outcome::Failed, UNTYPED_UNKNOWN));
    }
    match action {
        "offer_reassurance" => {
            let (stress, trust) = stress_and_trust(&agent)?;
            agent.setattr(intern!(py, "stress"), py_max(0.0, stress - REASSURANCE_STRESS))?;
            agent.setattr(intern!(py, "trust"), py_min(1.0, trust + REASSURANCE_TRUST))?;
        }
        "share_practical_information" => {
            let (_, trust) = stress_and_trust(&agent)?;
            agent.setattr(intern!(py, "trust"), py_min(1.0, trust + PRACTICAL_TRUST))?;
        }
        _ => return failure(errors::noted(Outcome::Failed, UNTYPED_ACTION)),
    }
    Ok(None)
}

/// `_social_payload_fields(payload) == set(names)`: these fields, with or without
/// `conversation_state_context`.
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

/// The applied contact a minute ago that an interaction answers, from the event log, whose payload
/// holds these values in this order of checks.
fn contact_matches(world: &Bound<'_, PyAny>, due_value: &Bound<'_, PyAny>, trigger: &Bound<'_, PyAny>, values: [(&str, &Bound<'_, PyAny>); 2]) -> PyResult<bool> {
    let py = world.py();
    if errors::str_of(trigger)?.is_none() {
        return Ok(false);
    }
    let contact = attribute(world, intern!(py, "event_log"))?.call_method1(intern!(py, "find"), (trigger,))?;
    if contact.is_none() || !attribute(&contact, intern!(py, "status"))?.is(&status_member(py, "APPLIED")?) {
        return Ok(false);
    }
    let recorded = attribute(&contact, intern!(py, "event"))?;
    if attribute(&recorded, intern!(py, "event_type"))?.ne("information_contact_update")?
        || attribute(&contact, intern!(py, "realized_minute"))?.ne(due_value.sub(1)?)?
    {
        return Ok(false);
    }
    let recorded_payload = attribute(&recorded, intern!(py, "payload"))?;
    for (name, value) in values {
        if get(&recorded_payload, name)?.ne(value)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// What the three other types read of the event, after the checks they share.
struct Typed<'py, 'a> {
    world: &'a Bound<'py, PyAny>,
    event: &'a Bound<'py, PyAny>,
    payload: &'a Bound<'py, PyAny>,
    due_value: Bound<'py, PyAny>,
    agent: Bound<'py, PyAny>,
    agent_id: Bound<'py, PyAny>,
    action: Bound<'py, PyAny>,
    reason: Bound<'py, PyAny>,
}

/// `_apply_typed_interaction` of every type but the three social actions.
fn other_typed_event<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    payload: &Bound<'py, PyAny>,
    interaction: &Bound<'py, PyAny>,
    kind: Option<&str>,
) -> PyResult<Said> {
    let py = world.py();
    let provenance = attribute(event, intern!(py, "provenance"))?;
    let due_value = attribute(event, intern!(py, "due_minute"))?;
    if !kind_is(event, "ENDOGENOUS")?
        || due_value.ne(attribute(world, intern!(py, "minute_index"))?)?
        || get(&provenance, "source_id")?.ne(SOURCE_ID)?
        || get(&provenance, "source_type")?.ne("assumption")?
        || attribute(event, intern!(py, "source_id"))?.ne(get(&provenance, "policy_version")?)?
    {
        return failure(errors::noted(Outcome::Failed, BAD_TIME));
    }
    let due = exact_int(&due_value)?;
    let reason = get(payload, "reason_short")?;
    if reason.is_none() {
        return failure(errors::noted(Outcome::Failed, NO_REASON));
    }
    let agent_id = get(payload, "agent_id")?;
    let agent = agent_of(world, &agent_id)?;
    if agent.is_none() {
        return failure(errors::noted(Outcome::Failed, UNKNOWN_AGENT));
    }
    let actor_key = agent_id.downcast_exact::<PyString>()?;
    if let Some(context) = mapping_get(payload, intern!(py, "conversation_state_context"))? {
        let participant_id = get(payload, "participant_agent_id")?;
        let holds = match errors::str_of(&participant_id)? {
            Some(id) if id != exact_str(&agent_id)? => context_holds(&context, &[actor_key, participant_id.downcast_exact::<PyString>()?], due)?,
            _ => context_holds(&context, &[actor_key], due)?,
        };
        if !holds {
            return failure(errors::noted(Outcome::Failed, BAD_CONTEXT));
        }
    }
    let action = get(payload, "action")?;
    let typed = Typed { world, event, payload, due_value, agent, agent_id, action, reason };
    match kind {
        Some("complaint_response") => complaint_response(&typed),
        Some("cooperation") => cooperation(&typed),
        Some("information_reaction") => information_reaction(&typed),
        Some(_) => failure(errors::noted(Outcome::Failed, NOT_ALLOWED)),
        // Another hashable JSON value is in no set of texts and equals none.
        None if interaction.is_exact_instance_of::<PyInt>()
            || interaction.is_exact_instance_of::<PyFloat>()
            || interaction.is_exact_instance_of::<PyBool>() =>
        {
            failure(errors::noted(Outcome::Failed, NOT_ALLOWED))
        }
        None => unsupported("another type of interaction is for the Python rule to judge"),
    }
}

fn complaint_response(typed: &Typed<'_, '_>) -> PyResult<Said> {
    let Typed { world, event, payload, due_value, agent, agent_id, action, reason } = typed;
    let py = world.py();
    let names = ["interaction_type", "agent_id", "request_id", "zone_id", "stress_threshold", "complaint_wait_minutes", "action", "reason_short"];
    if !typed_fields(payload, &names)? {
        return failure(errors::noted(Outcome::Failed, "complaint response payload fields do not match"));
    }
    let request_id = get(payload, "request_id")?;
    let request = get_any(&attribute(world, intern!(py, "support_requests"))?, &request_id)?;
    let threshold = get(payload, "stress_threshold")?;
    let wait = get(payload, "complaint_wait_minutes")?;
    if !is_threshold(&threshold)? || !errors::int_not_bool(&wait)?.is_some_and(|minutes| minutes > 0) {
        return failure(errors::noted(Outcome::Failed, "complaint response thresholds are invalid"));
    }
    let pending = py.import("sheltersim.domain.models")?.getattr("SupportRequestStatus")?.getattr("PENDING")?;
    if request.is_none()
        || attribute(&request, intern!(py, "agent_id"))?.ne(agent_id)?
        || !attribute(&request, intern!(py, "status"))?.is(&pending)
        || get(payload, "zone_id")?.ne(attribute(agent, intern!(py, "zone_id"))?)?
        || attribute(event, intern!(py, "causation_id"))?.ne(&request_id)?
        || due_value.sub(attribute(&request, intern!(py, "requested_minute"))?)?.lt(&wait)?
    {
        return suppression(errors::noted(Outcome::Suppressed, "complaint response context is no longer current"));
    }
    if attribute(agent, intern!(py, "stress"))?.lt(&threshold)? {
        return suppression(errors::noted(Outcome::Suppressed, "complaint response stress candidate is no longer current"));
    }
    if not_in(reason, &["long_wait", "high_stress", "request_still_pending", "rule_fallback"])? {
        return failure(errors::noted(Outcome::Failed, "complaint response reason code is not allowed"));
    }
    if action.eq("acknowledge_complaint")? {
        let (stress, trust) = stress_and_trust(agent)?;
        agent.setattr(intern!(py, "stress"), py_max(0.0, stress - COMPLAINT_ACKNOWLEDGE_STRESS))?;
        agent.setattr(intern!(py, "trust"), py_min(1.0, trust + COMPLAINT_TRUST))?;
    } else if action.eq("explain_staff_process")? {
        let (stress, trust) = stress_and_trust(agent)?;
        agent.setattr(intern!(py, "stress"), py_max(0.0, stress - COMPLAINT_EXPLAIN_STRESS))?;
        agent.setattr(intern!(py, "trust"), py_min(1.0, trust + COMPLAINT_TRUST))?;
    } else if action.eq("share_practical_information")? {
        let (_, trust) = stress_and_trust(agent)?;
        agent.setattr(intern!(py, "trust"), py_min(1.0, trust + PRACTICAL_TRUST))?;
    } else {
        return failure(errors::noted(Outcome::Failed, "complaint response action is not allowed"));
    }
    Ok(None)
}

fn cooperation(typed: &Typed<'_, '_>) -> PyResult<Said> {
    let Typed { world, event, payload, due_value, agent, agent_id, action, reason } = typed;
    let py = world.py();
    let names = ["interaction_type", "agent_id", "participant_agent_id", "zone_id", "stress_threshold", "relationship", "trigger_event_id", "action", "reason_short"];
    if !typed_fields(payload, &names)? {
        return failure(errors::noted(Outcome::Failed, "cooperation payload fields do not match"));
    }
    let participant = agent_of(world, &get(payload, "participant_agent_id")?)?;
    let trigger = get(payload, "trigger_event_id")?;
    let threshold = get(payload, "stress_threshold")?;
    if !is_threshold(&threshold)? {
        return failure(errors::noted(Outcome::Failed, "cooperation stress threshold is invalid"));
    }
    let id = intern!(py, "id");
    if participant.is_none() || attribute(&participant, id)?.eq(attribute(agent, id)?)? {
        return failure(errors::noted(Outcome::Failed, "cooperation requires two different known Agents"));
    }
    let actor_home = household_of(world, exact_str(&attribute(agent, id)?)?)?;
    let same_group = actor_home.is_some() && actor_home == household_of(world, exact_str(&attribute(&participant, id)?)?)?;
    let zone = attribute(agent, intern!(py, "zone_id"))?;
    if get(payload, "zone_id")?.ne(&zone)?
        || zone.ne(attribute(&participant, intern!(py, "zone_id"))?)?
        || !same_group
        || get(payload, "relationship")?.ne("same_group")?
        || attribute(event, intern!(py, "causation_id"))?.ne(&trigger)?
    {
        return suppression(errors::noted(Outcome::Suppressed, "cooperation participants or relationship changed"));
    }
    let participant_id = attribute(&participant, id)?;
    if !contact_matches(world, due_value, &trigger, [("sender_agent_id", agent_id), ("recipient_agent_id", &participant_id)])? {
        return failure(errors::noted(Outcome::Failed, "cooperation does not match an applied local contact"));
    }
    // `max(agent.stress, participant.stress)` keeps the first unless the second is greater.
    let agent_stress = attribute(agent, intern!(py, "stress"))?;
    let other_stress = attribute(&participant, intern!(py, "stress"))?;
    let highest = if other_stress.gt(&agent_stress)? { other_stress } else { agent_stress };
    if highest.lt(&threshold)? {
        return suppression(errors::noted(Outcome::Suppressed, "cooperation stress candidate is no longer current"));
    }
    if not_in(reason, &["shared_group_contact", "elevated_stress", "rule_fallback"])? {
        return failure(errors::noted(Outcome::Failed, "cooperation reason code is not allowed"));
    }
    if action.ne("offer_mutual_support")? {
        return failure(errors::noted(Outcome::Failed, "cooperation action is not allowed"));
    }
    let (stress, trust) = stress_and_trust(agent)?;
    let (other_stress, other_trust) = stress_and_trust(&participant)?;
    agent.setattr(intern!(py, "stress"), py_max(0.0, stress - COOPERATION_STRESS))?;
    participant.setattr(intern!(py, "stress"), py_max(0.0, other_stress - COOPERATION_STRESS))?;
    agent.setattr(intern!(py, "trust"), py_min(1.0, trust + COOPERATION_TRUST))?;
    participant.setattr(intern!(py, "trust"), py_min(1.0, other_trust + COOPERATION_TRUST))?;
    Ok(None)
}

fn information_reaction(typed: &Typed<'_, '_>) -> PyResult<Said> {
    let Typed { world, event, payload, due_value, agent, agent_id, action, reason } = typed;
    let py = world.py();
    let names = ["interaction_type", "agent_id", "participant_agent_id", "zone_id", "message_id", "trigger_event_id", "action", "reason_short"];
    if !typed_fields(payload, &names)? {
        return failure(errors::noted(Outcome::Failed, "information reaction payload fields do not match"));
    }
    let participant_id = get(payload, "participant_agent_id")?;
    let participant = agent_of(world, &participant_id)?;
    let message_id = get(payload, "message_id")?;
    let trigger = get(payload, "trigger_event_id")?;
    let id = intern!(py, "id");
    if participant.is_none() || attribute(&participant, id)?.eq(attribute(agent, id)?)? {
        return failure(errors::noted(Outcome::Failed, "information reaction requires two known Agents"));
    }
    let zone = attribute(agent, intern!(py, "zone_id"))?;
    if get(payload, "zone_id")?.ne(&zone)?
        || zone.ne(attribute(&participant, intern!(py, "zone_id"))?)?
        || attribute(event, intern!(py, "causation_id"))?.ne(&trigger)?
    {
        return suppression(errors::noted(Outcome::Suppressed, "information reaction contact is no longer local"));
    }
    if !contact_matches(world, due_value, &trigger, [("recipient_agent_id", agent_id), ("sender_agent_id", &participant_id)])? {
        return failure(errors::noted(Outcome::Failed, "information reaction does not match an applied contact"));
    }
    let state = get(&attribute(world, intern!(py, "model_state"))?, INFORMATION)?;
    if !state.is_instance_of::<PyDict>() {
        return suppression(errors::noted(Outcome::Suppressed, "information state is no longer enabled"));
    }
    let raw_message = get_any(&state.get_item(intern!(py, "messages"))?, &message_id)?;
    let beliefs = state.get_item(intern!(py, "beliefs"))?.call_method1(intern!(py, "get"), (agent_id, PyDict::new(py)))?;
    let raw_belief = get_any(&beliefs, &message_id)?;
    if raw_message.is_none() || raw_belief.is_none() {
        return suppression(errors::noted(Outcome::Suppressed, "information claim is no longer current"));
    }
    // `TypedMessage.from_dict` and `AgentInformationState.from_dict` of the records, read in the row.
    let mirror = mirror_of(world, INFORMATION)?;
    if !mirror.try_borrow()?.root_is(&state) {
        return unsupported("the world holds another document than the row's");
    }
    let message_text = exact_str(&message_id)?;
    let agent_text = exact_str(agent_id)?;
    let (settled, pressure) = NamespaceMirror::with_row::<InformationRow, _>(&mirror, |row| {
        if row.schema_version != INFORMATION_VERSION {
            return unsupported("an older state is for the Python rule to read");
        }
        let message = row.messages.get(message_text);
        let belief = row.beliefs.get(agent_text).and_then(|beliefs| beliefs.get(message_text));
        let (Some(message), Some(belief)) = (message, belief) else {
            return unsupported("the row lacks a record the tree holds");
        };
        if !message.parses() || !belief.parses() {
            return unsupported("the record is for the Python rule to judge");
        }
        let settled = &*message.truth_status != "false" || &*belief.belief != "heard";
        Ok((settled, row.query_pressure.get(agent_text).copied()))
    })?;
    if settled {
        return suppression(errors::noted(Outcome::Suppressed, "information claim has already been verified or rejected"));
    }
    if action.ne("seek_claim_verification")? {
        return failure(errors::noted(Outcome::Failed, "information reaction action is not allowed"));
    }
    if not_in(reason, &["heard_through_contact", "request_verification", "rule_fallback"])? {
        return failure(errors::noted(Outcome::Failed, "information reaction reason code is not allowed"));
    }
    let config = (|| {
        let configs = attribute(world, intern!(py, "model_configs"))?;
        py.import("sheltersim.domain.models")?
            .getattr("InformationSpreadConfig")?
            .call_method1("from_mapping", (configs.get_item("information_spread")?,))
    })()
    .map_err(|_| Unsupported::new_err("the config is for the Python rule to read"))?;
    let gain = number(&config.getattr(intern!(py, "query_pressure_per_claim"))?)?;
    let Some(pressure) = pressure else {
        return unsupported("the person has no record");
    };
    state.get_item(intern!(py, "query_pressure"))?.set_item(agent_text, py_min(1.0, pressure + gain))?;
    Ok(None)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(apply, m)?)?;
    Ok(())
}
