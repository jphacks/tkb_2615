//! `_apply_effect` (`experiments/phases/propose.py`): what a decision does to the world before it is
//! recorded, for the decisions that start no trip and ask no service.
//!
//! One call plans with nothing changed and then writes. What it doesn't take goes to the Python step
//! before any write, which is also how a bad value gets its error text
//! (docs/rust-port/lanes/m4-service.md):
//! - the actions with a trip, a queue or a request: `request_toilet`, `fetch_water`, `collect_meal`,
//!   `seek_help`, `seek_information`;
//! - `sleep` where a temporary room exists, since a bed can then be reserved;
//! - anything that isn't plainly what the Python step accepts.

use std::sync::{Arc, LazyLock};

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyString, PyTuple};

use crate::core::row::{attribute, exact_dict, exact_int, exact_str, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::core::world_core::{raw_dict_get, NamespaceMirror};
use crate::rules::movement::{mirror_held, KEY};
use crate::rules::movement_geometry::{usable, Geo};
use crate::rules::social_dialogue::context_value;
use crate::state::entities::{entities_of, raw_setattr, Activity, EntityStore};
use crate::state::events::{EventKind, EventRow, EventWriter};
use crate::state::movement::MovementRow;


const OVERLAY_KEY: &str = "geometry_overlay_v1";
/// `interaction_minutes` where the config has none.
const DEFAULT_INTERACTION_MINUTES: i64 = 5;
/// The schedule's priority of a person's own social action.
const SOCIAL_PRIORITY: i64 = 35;
const SOCIAL_POLICY: &str = "event-triggered-social-v2";
const SOCIAL_SOURCE: &str = "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1";

/// The fixed strings of the event, each shared by all its rows.
struct Names {
    interaction_type: Arc<str>,
    agent_id: Arc<str>,
    participant_agent_id: Arc<str>,
    conversation_state_context: Arc<str>,
    zone_id: Arc<str>,
    topic_id: Arc<str>,
    action: Arc<str>,
    reason_short: Arc<str>,
    interaction_duration_minutes: Arc<str>,
    source_type: Arc<str>,
    source_id: Arc<str>,
    policy_version: Arc<str>,
    event_type: Arc<str>,
    autonomous_need_choice: Arc<str>,
    assumption: Arc<str>,
    social_source: Arc<str>,
    social_policy: Arc<str>,
}

static NAMES: LazyLock<Names> = LazyLock::new(|| Names {
    interaction_type: Arc::from("interaction_type"),
    agent_id: Arc::from("agent_id"),
    participant_agent_id: Arc::from("participant_agent_id"),
    conversation_state_context: Arc::from("conversation_state_context"),
    zone_id: Arc::from("zone_id"),
    topic_id: Arc::from("topic_id"),
    action: Arc::from("action"),
    reason_short: Arc::from("reason_short"),
    interaction_duration_minutes: Arc::from("interaction_duration_minutes"),
    source_type: Arc::from("source_type"),
    source_id: Arc::from("source_id"),
    policy_version: Arc::from("policy_version"),
    event_type: Arc::from("social_interaction"),
    autonomous_need_choice: Arc::from("autonomous_need_choice"),
    assumption: Arc::from("assumption"),
    social_source: Arc::from(SOCIAL_SOURCE),
    social_policy: Arc::from(SOCIAL_POLICY),
});

/// `interaction_data`: the interaction, its action and its topic.
struct Interaction {
    kind: &'static str,
    action: &'static str,
    topic: &'static str,
}

const SOCIALIZE: Interaction = Interaction { kind: "reassurance_exchange", action: "offer_reassurance", topic: "emotional_check_in" };
const CARE_FAMILY: Interaction = Interaction { kind: "family_care", action: "provide_family_support", topic: "family_care" };
const HELP_OTHERS: Interaction = Interaction { kind: "voluntary_support", action: "offer_practical_help", topic: "mutual_aid" };

enum Effect {
    /// `wait`, `rest`, or `sleep` without a bed: the activity to take, with no target.
    Still(Activity),
    Social(Interaction),
}

/// `release_sleep_bed`, not done yet: the reservations to delete from, if the person has one.
fn held_bed<'py>(table: &Bound<'py, PyDict>, agent_id: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = table.py();
    let Some(state) = table.get_item(OVERLAY_KEY)? else {
        return Ok(None);
    };
    let Some(fields) = as_readable_dict(&state) else {
        return unsupported("the overlay is not a plain dict");
    };
    let Some(reservations) = fields.get_item(intern!(py, "bed_reservations"))? else {
        return Ok(None);
    };
    let Some(held) = as_readable_dict(&reservations) else {
        return unsupported("the bed reservations are not a plain dict");
    };
    Ok(if raw_dict_get(held.as_any(), agent_id)?.is_some() { Some(reservations) } else { None })
}

/// Where `reserve_sleep_bed` finds no bed and returns None, or Unsupported. It reads the positions
/// of the person and of who is in the same zone, and each must be what `Point2D` takes.
fn no_bed_to_reserve(world: &Bound<'_, PyAny>, table: &Bound<'_, PyDict>, agent_id: &str, zone_id: &str) -> PyResult<()> {
    let py = world.py();
    let Some(state) = table.get_item(OVERLAY_KEY)? else {
        return Ok(());
    };
    let Some(state) = as_readable_dict(&state) else {
        return unsupported("the overlay is not a plain dict");
    };
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let schema = match exact_dict(&configs)?.get_item(intern!(py, "geometry_overlay"))? {
        Some(config) => exact_dict(&config)?.get_item(intern!(py, "schema_version"))?,
        None => None,
    };
    let schema_takes_beds = match &schema {
        Some(schema) => matches!(exact_int(schema), Ok(2..=4)),
        None => false,
    };
    if !schema_takes_beds {
        return if schema.is_none() || schema.as_ref().is_some_and(|schema| exact_int(schema).is_ok()) {
            Ok(())
        } else {
            unsupported("the overlay's schema is not an int")
        };
    }
    if state.get_item(intern!(py, "bed_reservations"))?.is_none() {
        return unsupported("the reservations are still to be made");
    }
    Geo::of(world)?;
    if !table.contains(KEY)? {
        return unsupported("a bed's positions without the movement state");
    }
    let mirror = mirror_held(world)?;
    let store = entities_of(world)?;
    NamespaceMirror::with_row::<MovementRow, _>(&mirror, |row| {
        EntityStore::with_rows(&store, |rows| {
            for (other_id, other) in rows.agents.iter() {
                if &**other_id != agent_id && &*other.zone_id != zone_id {
                    continue;
                }
                if let Some(point) = row.positions.get(&**other_id).and_then(|position| position.point) {
                    if !usable(point.x, point.y) {
                        return unsupported("a position is not a usable point");
                    }
                }
            }
            Ok(())
        })
    })?;
    match state.get_item(intern!(py, "temporary_rooms"))? {
        Some(rooms) if !exact_dict(&rooms)?.is_empty() => unsupported("a temporary room may have a bed"),
        _ => Ok(()),
    }
}

/// `_apply_effect`: the target, the next decision, and whether the decision committed.
#[pyfunction]
fn apply_effect<'py>(controller: &Bound<'py, PyAny>, proposal: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
    let py = controller.py();
    let action = attribute(proposal, intern!(py, "action"))?;
    let action = attribute(&action, intern!(py, "_value_"))?;
    let action = exact_str(&action)?;
    let effect = match action {
        "wait" => Effect::Still(Activity::Idle),
        "rest" => Effect::Still(Activity::Resting),
        "sleep" => Effect::Still(Activity::Sleeping),
        "socialize" => Effect::Social(SOCIALIZE),
        "care_family" => Effect::Social(CARE_FAMILY),
        "help_others" => Effect::Social(HELP_OTHERS),
        _ => return unsupported("an action with a trip or a request"),
    };
    let world = attribute(controller, intern!(py, "world"))?;
    let agent_object = attribute(proposal, intern!(py, "agent_id"))?;
    let agent_id = exact_str(&agent_object)?;
    let planned_next = exact_int(&attribute(proposal, intern!(py, "next_decision_at"))?)?;
    let store = entities_of(&world)?;
    let table_object = attribute(&world, intern!(py, "model_state"))?;
    let table = exact_dict(&table_object)?;
    let agents = attribute(&world, intern!(py, "agents"))?;
    let Some(agent) = raw_dict_get(&agents, &agent_object)? else {
        return unsupported("no such agent");
    };
    let (zone_id, own_id, prior_activity, prior_target_set) = EntityStore::with_rows(&store, |rows| {
        let Some(agent) = rows.agents.get(agent_id) else {
            return unsupported("the rows have no such agent");
        };
        Ok((agent.zone_id.clone(), agent.profile.id.clone(), agent.activity, agent.target_id.is_some()))
    })?;

    // Plan.
    let bed = if action == "sleep" {
        no_bed_to_reserve(&world, table, agent_id, &zone_id)?;
        None
    } else {
        held_bed(table, &agent_object)?
    };
    let mut social: Option<(EventRow, EventWriter<'py>, Arc<str>, i64)> = None;
    if let Effect::Social(interaction) = &effect {
        let configs = attribute(&world, intern!(py, "model_configs"))?;
        let minutes = match exact_dict(&configs)?.get_item(intern!(py, "body_dynamics"))? {
            Some(config) => match exact_dict(&config)?.get_item(intern!(py, "social_interaction_minutes"))? {
                Some(minutes) => exact_int(&minutes)?,
                None => DEFAULT_INTERACTION_MINUTES,
            },
            None => DEFAULT_INTERACTION_MINUTES,
        };
        let minute = exact_int(&attribute(&world, intern!(py, "minute_index"))?)?;
        let duration = exact_int(&attribute(&world, intern!(py, "duration_minutes"))?)?;
        let Some(due) = minute.checked_add(minutes) else {
            return unsupported("a due minute out of range");
        };
        let target = attribute(proposal, intern!(py, "target_id"))?;
        let participant = if target.is_none() { None } else { Some(Arc::<str>::from(exact_str(&target)?)) };
        let meets = EntityStore::with_rows(&store, |rows| {
            let Some(id) = &participant else {
                return Ok(None);
            };
            Ok(rows.agents.get(id).map(|row| (row.profile.id.clone(), row.zone_id.clone())))
        })?;
        if let Some((participant_id, participant_zone)) = meets {
            if participant_id != own_id && participant_zone == zone_id && due < duration {
                let writer = EventWriter::of(&world)?;
                let sequence = writer.next_sequence()?;
                let event_id: Arc<str> = Arc::from(format!("autonomous-social-action-{sequence}"));
                {
                    let past = writer.log().borrow();
                    if past.has_event_id(&event_id) || past.has_sequence(sequence) || writer.queue().borrow().has_event(&event_id) {
                        return unsupported("the event's identity is taken");
                    }
                }
                let names = &*NAMES;
                let mut payload = IndexMap::with_capacity(9);
                payload.insert(names.interaction_type.clone(), V::Str(Arc::from(interaction.kind)));
                payload.insert(names.agent_id.clone(), V::Str(own_id.clone()));
                payload.insert(names.participant_agent_id.clone(), V::Str(participant_id.clone()));
                payload.insert(names.conversation_state_context.clone(), context_value(&world, &own_id, &participant_id)?);
                payload.insert(names.zone_id.clone(), V::Str(zone_id.clone()));
                payload.insert(names.topic_id.clone(), V::Str(Arc::from(interaction.topic)));
                payload.insert(names.action.clone(), V::Str(Arc::from(interaction.action)));
                payload.insert(names.reason_short.clone(), V::Str(names.autonomous_need_choice.clone()));
                payload.insert(names.interaction_duration_minutes.clone(), V::Int(minutes));
                let mut provenance = IndexMap::with_capacity(3);
                provenance.insert(names.source_type.clone(), V::Str(names.assumption.clone()));
                provenance.insert(names.source_id.clone(), V::Str(names.social_source.clone()));
                provenance.insert(names.policy_version.clone(), V::Str(names.social_policy.clone()));
                let Some(event) = EventRow::new(
                    event_id,
                    EventKind::Endogenous,
                    due,
                    SOCIAL_PRIORITY,
                    sequence,
                    names.event_type.clone(),
                    payload,
                    Some(names.social_policy.clone()),
                    None,
                    provenance,
                ) else {
                    return unsupported("the event's values are not what an event takes");
                };
                social = Some((event, writer, participant_id, due));
            }
        }
    }

    // Write. Nothing below refuses.
    if let Some(reservations) = bed {
        // Through the map's own method: the map may be a tree's.
        reservations.del_item(&agent_object)?;
    }
    let next = social.as_ref().map_or(planned_next, |(.., due)| *due);
    let still = if let Effect::Still(activity) = effect { Some(activity) } else { None };
    EntityStore::with_rows(&store, |rows| {
        let Some(row) = rows.agents.get_mut(agent_id) else {
            return unsupported("the rows have no such agent");
        };
        row.next_decision_at = next;
        if let Some(activity) = still {
            row.activity = activity;
            row.target_id = None;
        }
        Ok(())
    })?;
    raw_setattr(&agent, intern!(py, "next_decision_at"), next.into_pyobject(py)?.as_ptr())?;
    if let Some(activity) = still {
        if activity != prior_activity {
            raw_setattr(&agent, intern!(py, "activity"), activity.member(py)?.as_ptr())?;
        }
        if prior_target_set {
            raw_setattr(&agent, intern!(py, "target_id"), py.None().as_ptr())?;
        }
    }
    let target = match social {
        Some((event, writer, participant_id, _)) => {
            writer.allocate_sequence()?;
            writer.schedule(event)?;
            PyString::new(py, &participant_id).into_any()
        }
        None => py.None().into_bound(py),
    };
    let committed = still.is_some() || !target.is_none();
    PyTuple::new(py, [target, next.into_pyobject(py)?.into_any(), PyBool::new(py, committed).to_owned().into_any()])
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(apply_effect, m)?)?;
    Ok(())
}
