//! The events of `information_spread_v1` in `events/handlers.py`: a rumor, a correction and a contact,
//! each of which plans from the row and then writes single items to the tree, which reach the row as
//! forwarded writes. Anything but a success says Unsupported before the first write: a suppressed
//! contact, a failed one, a value not of the row's types. The Python rule then says it in its own words
//! (docs/rust-port/lanes/m6-handlers-rest.md).
//!
//! `InformationSpreadConfig.from_mapping` and `selected_models` stay Python's: they read the Scenario's
//! contract, and neither changes anything.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

use crate::core::mathx::{py_max, py_min};
use crate::core::rng::{Key, KeyedDraws};
use crate::core::row::{attribute, exact_float, exact_int, exact_str, unsupported};
use crate::core::world_core::{mirror_of, NamespaceMirror};
use crate::rules::effects::refused_by_python;
use crate::rules::handlers::{counted, fields_are, get, get_is, kind_is, number, text_is};
use crate::rules::support::household_of;
use crate::state::information::{belief_rank, is_stable_id, BeliefRow, InformationRow, NAME, SCHEMA_VERSION};

const EVIDENCE_AWARE: &str = "evidence-aware-rumor-v1";
const CONTACT_SOURCE: &str = "ASM-INFORMATION-SPREAD-V1";
const MAX_MESSAGES: usize = 1024;

/// The config object, and the namespace's tree whose row is Rust's.
pub(crate) fn runtime_state<'py>(world: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>, Bound<'py, NamespaceMirror>)> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let config = py
        .import("sheltersim.domain.models")?
        .getattr("InformationSpreadConfig")?
        .call_method1("from_mapping", (get(&configs, "information_spread")?,))?;
    if !config.getattr(intern!(py, "enabled"))?.is_truthy()? {
        return unsupported("the model is not enabled");
    }
    let state = get(&attribute(world, intern!(py, "model_state"))?, NAME)?;
    if !state.is_instance_of::<PyDict>() {
        return unsupported("the model has no state");
    }
    let mirror = mirror_of(world, NAME)?;
    if !mirror.try_borrow()?.root_is(&state) {
        return unsupported("the world holds another document than the row's");
    }
    let version = NamespaceMirror::with_row::<InformationRow, _>(&mirror, |row| Ok(row.schema_version))?;
    if version != SCHEMA_VERSION {
        return unsupported("an older state is upgraded in Python");
    }
    Ok((config, state, mirror))
}

fn evidence_aware(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    let selected = py.import("sheltersim.contracts.policies")?.getattr("selected_models")?.call1((attribute(world, intern!(py, "model_configs"))?,))?;
    Ok(text_is(&selected.get_item("information")?, EVIDENCE_AWARE))
}

/// What a contact's rule reads of `selected_models(...)["information"]`.
fn information_model<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    py.import("sheltersim.contracts.policies")?.getattr("selected_models")?.call1((attribute(world, intern!(py, "model_configs"))?,))?.get_item("information")
}

fn seed_of(world: &Bound<'_, PyAny>) -> PyResult<KeyedDraws> {
    let seed = exact_int(&attribute(world, intern!(world.py(), "seed"))?)?;
    if seed < 0 {
        return unsupported("a negative seed");
    }
    Ok(KeyedDraws::new(&seed.to_string()))
}

/// `CounterRNG(world.seed).random(label, minute, event_id, agent_id, message_id)`.
fn draw(draws: &KeyedDraws, label: &str, minute: i64, event_id: &str, agent_id: &str, message_id: &str) -> f64 {
    let mut key = Key::labelled(label);
    key.int(minute).text(event_id).text(agent_id).text(message_id);
    key.draw(draws)
}

fn trust_of(world: &Bound<'_, PyAny>, agent_id: &str) -> PyResult<f64> {
    let py = world.py();
    exact_float(&attribute(&attribute(world, intern!(py, "agents"))?.get_item(agent_id)?, intern!(py, "trust"))?)
}

/// `config.correction_source_reliability_by_id.get(source_id, config.default_source_reliability)`.
fn reliability_of(config: &Bound<'_, PyAny>, source_id: &str) -> PyResult<f64> {
    let py = config.py();
    let default = config.getattr(intern!(py, "default_source_reliability"))?;
    number(&config.getattr(intern!(py, "correction_source_reliability_by_id"))?.call_method1(intern!(py, "get"), (source_id, default))?)
}

fn setting(config: &Bound<'_, PyAny>, name: &Bound<'_, PyString>) -> PyResult<f64> {
    number(&config.getattr(name)?)
}

/// `_sample_claim_belief_strength`.
fn sample_strength(world: &Bound<'_, PyAny>, config: &Bound<'_, PyAny>, agent_id: &str, source_id: &str, event_id: &str, minute: i64, message_id: &str) -> PyResult<f64> {
    let py = world.py();
    let reliability = reliability_of(config, source_id)?;
    let trust = trust_of(world, agent_id)?;
    let probability = py_min(
        1.0,
        py_max(
            0.0,
            setting(config, intern!(py, "rumor_belief_probability"))?
                + 2.0 * setting(config, intern!(py, "rumor_trust_sensitivity"))? * (trust - 0.5)
                + 2.0 * setting(config, intern!(py, "rumor_source_sensitivity"))? * (reliability - 0.5),
        ),
    );
    let drawn = draw(&seed_of(world)?, "rumor-belief-acceptance-v1", minute, event_id, agent_id, message_id);
    Ok(if drawn < probability { 1.0 } else { 0.0 })
}

/// `_claim_is_misbelieved`.
fn misbelieved(truth: &str, strength: f64) -> bool {
    (truth == "true" && strength < 0.5) || (truth == "false" && strength > 0.5)
}

/// The fields of a belief as `AgentInformationState.to_dict` lists them.
struct Belief {
    message_id: Arc<str>,
    belief: Arc<str>,
    source: Arc<str>,
    last_contact: i64,
    last_event_id: Arc<str>,
    belief_strength: f64,
    share_propensity: f64,
    correction_exposed: bool,
    correction_accepted: bool,
    ever_misbelieved: bool,
    ever_forgotten: bool,
    last_correction_accepted: bool,
}

impl Belief {
    /// What `AgentInformationState.__post_init__` would turn down, beyond what the row guarantees.
    fn is_valid(&self) -> bool {
        is_stable_id(&self.message_id)
            && is_stable_id(&self.source)
            && is_stable_id(&self.last_event_id)
            && self.last_contact >= 0
            && (!self.correction_accepted || self.correction_exposed)
            && (!self.last_correction_accepted || self.correction_exposed)
            && (0.0..=1.0).contains(&self.belief_strength)
            && (0.0..=1.0).contains(&self.share_propensity)
            && (&*self.belief != "unknown" || self.share_propensity == 0.0)
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let record = PyDict::new(py);
        record.set_item("message_id", &*self.message_id)?;
        record.set_item("belief", &*self.belief)?;
        record.set_item("source", &*self.source)?;
        record.set_item("last_contact", self.last_contact)?;
        record.set_item("last_event_id", &*self.last_event_id)?;
        record.set_item("exposed", true)?;
        record.set_item("belief_strength", self.belief_strength)?;
        record.set_item("share_propensity", self.share_propensity)?;
        record.set_item("correction_exposed", self.correction_exposed)?;
        record.set_item("correction_accepted", self.correction_accepted)?;
        record.set_item("ever_misbelieved", self.ever_misbelieved)?;
        record.set_item("ever_forgotten", self.ever_forgotten)?;
        record.set_item("last_correction_accepted", self.last_correction_accepted)?;
        Ok(record)
    }
}

/// What a rule keeps of a belief that was there.
#[derive(Clone)]
struct Prior {
    belief: Arc<str>,
    belief_strength: f64,
    share_propensity: f64,
    correction_exposed: bool,
    correction_accepted: bool,
    ever_misbelieved: bool,
    ever_forgotten: bool,
    last_correction_accepted: bool,
}

fn prior_of(record: &BeliefRow) -> PyResult<Prior> {
    if !record.parses() {
        return unsupported("a belief is for the Python rule to read");
    }
    Ok(Prior {
        belief: record.belief.clone(),
        belief_strength: record.belief_strength,
        share_propensity: record.share_propensity,
        correction_exposed: record.correction_exposed,
        correction_accepted: record.correction_accepted,
        ever_misbelieved: record.ever_misbelieved,
        ever_forgotten: record.ever_forgotten,
        last_correction_accepted: record.last_correction_accepted,
    })
}

fn add_counter(state: &Bound<'_, PyAny>, name: &str) -> PyResult<()> {
    let counters = state.get_item("counters")?;
    let value = exact_int(&counters.get_item(name)?)?;
    counters.set_item(name, value + 1)
}

// ---- InformationRumorHandler ------------------------------------------------------------------------

#[pyfunction]
fn rumor<'py>(_handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    let plan = (|| -> PyResult<_> {
        let (config, state, mirror) = runtime_state(world)?;
        let payload = attribute(event, intern!(py, "payload"))?;
        if !fields_are(&payload, &["message_id", "truth_status", "origin_agent_id"])? {
            return unsupported("the payload has other fields");
        }
        let source_value = attribute(event, intern!(py, "source_id"))?;
        let source: Arc<str> = Arc::from(exact_str(&source_value)?);
        let message_value = payload.get_item("message_id")?;
        let origin_value = payload.get_item("origin_agent_id")?;
        let message_id: Arc<str> = Arc::from(exact_str(&message_value)?);
        let origin: Arc<str> = Arc::from(exact_str(&origin_value)?);
        let truth_value = payload.get_item("truth_status")?;
        let truth = exact_str(&truth_value)?.to_owned();
        if !matches!(truth.as_str(), "unknown" | "true" | "false") {
            return unsupported("a truth status for the Python rule to judge");
        }
        let (taken, messages, has_agent, pressure) = NamespaceMirror::with_row::<InformationRow, _>(&mirror, |row| {
            Ok((
                row.messages.contains_key(&*message_id),
                row.messages.len(),
                row.beliefs.contains_key(&*origin),
                row.query_pressure.get(&*origin).copied(),
            ))
        })?;
        let agent = get(&attribute(world, intern!(py, "agents"))?, &origin)?;
        let Some(pressure) = pressure else {
            return unsupported("the person has no record");
        };
        if taken || messages >= MAX_MESSAGES || agent.is_none() || !has_agent {
            return unsupported("the rumor is for the Python rule to judge");
        }
        let zone = attribute(&agent, intern!(py, "zone_id"))?;
        let zone_text = exact_str(&zone)?;
        let event_id_value = attribute(event, intern!(py, "event_id"))?;
        let event_id: Arc<str> = Arc::from(exact_str(&event_id_value)?);
        let due = exact_int(&attribute(event, intern!(py, "due_minute"))?)?;
        if due < 0 || ![&*message_id, &*source, zone_text, &*event_id, &*origin].iter().all(|id| is_stable_id(id)) {
            return unsupported("an ID for the Python rule to judge");
        }
        let message = PyDict::new(py);
        message.set_item("id", &*message_id)?;
        message.set_item("truth_status", &truth)?;
        message.set_item("source_id", &*source)?;
        message.set_item("zone_id", zone_text)?;
        message.set_item("time", due)?;
        message.set_item("origin_event_id", &*event_id)?;
        message.set_item("origin_agent_id", &*origin)?;
        let strength = if evidence_aware(world)? {
            sample_strength(world, &config, &origin, &source, &event_id, due, &message_id)?
        } else {
            0.5
        };
        let belief = Belief {
            message_id: message_id.clone(),
            belief: Arc::from("heard"),
            source: origin.clone(),
            last_contact: due,
            last_event_id: event_id,
            belief_strength: strength,
            share_propensity: 0.5,
            correction_exposed: false,
            correction_accepted: false,
            ever_misbelieved: misbelieved(&truth, strength),
            ever_forgotten: false,
            last_correction_accepted: false,
        };
        let next = py_min(1.0, pressure + setting(&config, intern!(py, "query_pressure_per_claim"))?);
        Ok((state, message_id, origin, message, belief, next))
    })();
    let (state, message_id, origin, message, belief, next) = counted(py, "rumor", refused_by_python(py, plan))?;
    state.get_item("messages")?.set_item(&*message_id, message)?;
    state.get_item("beliefs")?.get_item(&*origin)?.set_item(&*message_id, belief.to_py(py)?)?;
    state.get_item("query_pressure")?.set_item(&*origin, next)?;
    Ok(())
}

// ---- InformationCorrectionHandler --------------------------------------------------------------------

/// `min(0.95, max(0.0, acceptance + 2.0 * trust_sensitivity * (trust - 0.5) + 2.0 * source_sensitivity * (reliability - 0.5)))`.
fn acceptance_probability(config: &Bound<'_, PyAny>, trust: f64, reliability: f64) -> PyResult<f64> {
    let py = config.py();
    Ok(py_min(
        0.95,
        py_max(
            0.0,
            setting(config, intern!(py, "correction_acceptance_probability"))?
                + 2.0 * setting(config, intern!(py, "correction_trust_sensitivity"))? * (trust - 0.5)
                + 2.0 * setting(config, intern!(py, "correction_source_sensitivity"))? * (reliability - 0.5),
        ),
    ))
}

#[pyfunction]
fn correction<'py>(_handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    struct Change {
        agent_id: String,
        record: Belief,
        pressure: Option<f64>,
    }
    let plan = (|| -> PyResult<_> {
        let (config, state, mirror) = runtime_state(world)?;
        let payload = attribute(event, intern!(py, "payload"))?;
        if !fields_are(&payload, &["message_id", "zone_id", "belief"])? {
            return unsupported("the payload has other fields");
        }
        let source_value = attribute(event, intern!(py, "source_id"))?;
        let source: Arc<str> = Arc::from(exact_str(&source_value)?);
        let message_value = payload.get_item("message_id")?;
        let zone_value = payload.get_item("zone_id")?;
        let belief_value = payload.get_item("belief")?;
        let message_id: Arc<str> = Arc::from(exact_str(&message_value)?);
        exact_str(&zone_value)?;
        let belief_text = exact_str(&belief_value)?;
        if !matches!(belief_text, "verified" | "rejected") || !attribute(world, intern!(py, "zones"))?.contains(&zone_value)? {
            return unsupported("the correction is for the Python rule to judge");
        }
        let (truth, origin_event) = NamespaceMirror::with_row::<InformationRow, _>(&mirror, |row| {
            let Some(message) = row.messages.get(&*message_id) else {
                return unsupported("an unknown message");
            };
            if !message.parses() {
                return unsupported("a message is for the Python rule to read");
            }
            Ok((message.truth_status.clone(), message.origin_event_id.clone()))
        })?;
        if !text_is(&attribute(event, intern!(py, "causation_id"))?, &origin_event) {
            return unsupported("the correction must reference its rumor");
        }
        match &*truth {
            "true" if belief_text != "verified" => return unsupported("the correction conflicts with the truth"),
            "false" if belief_text != "rejected" => return unsupported("the correction conflicts with the truth"),
            _ => {}
        }
        let aware = evidence_aware(world)?;
        let reliability = reliability_of(&config, &source)?;
        let mut targets = Vec::new();
        for agent in attribute(world, intern!(py, "agents"))?.call_method0(intern!(py, "values"))?.try_iter()? {
            let agent = agent?;
            if attribute(&agent, intern!(py, "zone_id"))?.eq(&zone_value)? {
                targets.push((exact_str(&attribute(&agent, intern!(py, "id"))?)?.to_owned(), agent));
            }
        }
        targets.sort_unstable_by(|first, second| first.0.cmp(&second.0));
        let event_id_value = attribute(event, intern!(py, "event_id"))?;
        let event_id: Arc<str> = Arc::from(exact_str(&event_id_value)?);
        let due = exact_int(&attribute(event, intern!(py, "due_minute"))?)?;
        let draws = seed_of(world)?;
        let relief = setting(&config, intern!(py, "query_pressure_relief_per_correction"))?;
        let verified = belief_text == "verified";
        let belief: Arc<str> = Arc::from(belief_text);
        let mut changes = Vec::new();
        for (agent_id, agent) in &targets {
            let (previous, pressure) = NamespaceMirror::with_row::<InformationRow, _>(&mirror, |row| {
                let Some(beliefs) = row.beliefs.get(&**agent_id) else {
                    return unsupported("the person has no beliefs");
                };
                let previous = beliefs.get(&*message_id).map(prior_of).transpose()?;
                Ok((previous, row.query_pressure.get(&**agent_id).copied()))
            })?;
            let accepted = if aware {
                let trust = exact_float(&attribute(agent, intern!(py, "trust"))?)?;
                let probability = acceptance_probability(&config, trust, reliability)?;
                draw(&draws, "correction-acceptance-v1", due, &event_id, agent_id, &message_id) < probability
            } else {
                true
            };
            if let Some(previous) = &previous {
                if matches!(&*previous.belief, "verified" | "rejected") {
                    if previous.belief != belief {
                        return unsupported("a conflicting verified state");
                    }
                    if !aware {
                        continue;
                    }
                }
            }
            let prior_strength = previous.as_ref().map_or(0.5, |prior| prior.belief_strength);
            let record = Belief {
                message_id: message_id.clone(),
                belief: if accepted {
                    belief.clone()
                } else {
                    previous.as_ref().map_or_else(|| Arc::from("heard"), |prior| prior.belief.clone())
                },
                source: source.clone(),
                last_contact: due,
                last_event_id: event_id.clone(),
                belief_strength: if accepted {
                    if verified { 1.0 } else { 0.0 }
                } else {
                    prior_strength
                },
                share_propensity: previous.as_ref().map_or(0.5, |prior| prior.share_propensity),
                correction_exposed: true,
                correction_accepted: accepted || previous.as_ref().is_some_and(|prior| prior.correction_accepted),
                last_correction_accepted: accepted,
                ever_misbelieved: previous.as_ref().is_some_and(|prior| prior.ever_misbelieved) || (!accepted && misbelieved(&truth, prior_strength)),
                ever_forgotten: previous.as_ref().is_some_and(|prior| prior.ever_forgotten),
            };
            if !record.is_valid() {
                return unsupported("a belief for the Python rule to judge");
            }
            let pressure = if accepted {
                let Some(pressure) = pressure else {
                    return unsupported("the person has no record");
                };
                Some(py_max(0.0, pressure - relief))
            } else {
                None
            };
            changes.push(Change { agent_id: agent_id.clone(), record, pressure });
        }
        let new_truth = (&*truth == "unknown").then(|| if verified { "true" } else { "false" });
        Ok((state, message_id, new_truth, changes))
    })();
    let (state, message_id, new_truth, changes) = counted(py, "correction", refused_by_python(py, plan))?;
    if let Some(truth) = new_truth {
        // `replace(message, truth_status=...).to_dict()`: the same fields in the same order.
        let messages = state.get_item("messages")?;
        let record = messages.get_item(&*message_id)?;
        let next = PyDict::new(py);
        for name in ["id", "truth_status", "source_id", "zone_id", "time", "origin_event_id", "origin_agent_id"] {
            next.set_item(name, record.get_item(name)?)?;
        }
        next.set_item("truth_status", truth)?;
        messages.set_item(&*message_id, next)?;
    }
    let beliefs = state.get_item("beliefs")?;
    let pressures = state.get_item("query_pressure")?;
    for change in changes {
        beliefs.get_item(&change.agent_id)?.set_item(&*message_id, change.record.to_py(py)?)?;
        if let Some(pressure) = change.pressure {
            pressures.set_item(&change.agent_id, pressure)?;
            add_counter(&state, "correction_deliveries")?;
        }
    }
    Ok(())
}

// ---- InformationContactHandler ------------------------------------------------------------------------

/// `_current_information_contact_basis`, or None.
fn contact_basis(world: &Bound<'_, PyAny>, sender: &str, recipient: &str) -> PyResult<Option<&'static str>> {
    let py = world.py();
    let agents = attribute(world, intern!(py, "agents"))?;
    let sender_zone = attribute(&agents.get_item(sender)?, intern!(py, "zone_id"))?;
    let recipient_zone = attribute(&agents.get_item(recipient)?, intern!(py, "zone_id"))?;
    let (first, second) = (exact_str(&sender_zone)?, exact_str(&recipient_zone)?);
    let same_zone = first == second;
    let mut adjacent = false;
    for edge in attribute(world, intern!(py, "zone_edges"))?.call_method0(intern!(py, "values"))?.try_iter()? {
        let edge = edge?;
        if attribute(&edge, intern!(py, "closed"))?.is_truthy()? {
            continue;
        }
        let (from, to) = (attribute(&edge, intern!(py, "from_zone_id"))?, attribute(&edge, intern!(py, "to_zone_id"))?);
        let (from, to) = (exact_str(&from)?, exact_str(&to)?);
        // `{from, to} == {first, second}`.
        if (from == first && to == second) || (from == second && to == first) {
            adjacent = true;
            break;
        }
    }
    if !same_zone && !adjacent {
        return Ok(None);
    }
    let home = household_of(world, sender)?;
    if home.is_some() && home == household_of(world, recipient)? {
        return Ok(Some("family"));
    }
    Ok(Some(if same_zone { "same_zone" } else { "adjacent_zone" }))
}

#[pyfunction]
fn contact<'py>(_handler: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    let plan = (|| -> PyResult<_> {
        let (config, state, mirror) = runtime_state(world)?;
        let payload = attribute(event, intern!(py, "payload"))?;
        if !fields_are(&payload, &["message_id", "sender_agent_id", "recipient_agent_id", "belief", "contact_basis", "contact_minutes"])? {
            return unsupported("the payload has other fields");
        }
        let due = exact_int(&attribute(event, intern!(py, "due_minute"))?)?;
        if !kind_is(event, "ENDOGENOUS")? || due != exact_int(&attribute(world, intern!(py, "minute_index"))?)? {
            return unsupported("the kind or time is for the Python rule to judge");
        }
        let provenance = attribute(event, intern!(py, "provenance"))?;
        let model = information_model(world)?;
        let aware = text_is(&model, EVIDENCE_AWARE);
        if !get_is(&provenance, "source_type", "assumption")?
            || !get_is(&provenance, "source_id", CONTACT_SOURCE)?
            || !get(&provenance, "policy_version")?.eq(&model)?
        {
            return unsupported("the provenance is for the Python rule to judge");
        }
        let message_value = payload.get_item("message_id")?;
        let sender_value = payload.get_item("sender_agent_id")?;
        let recipient_value = payload.get_item("recipient_agent_id")?;
        let message_id: Arc<str> = Arc::from(exact_str(&message_value)?);
        let sender_id: Arc<str> = Arc::from(exact_str(&sender_value)?);
        let recipient_id: Arc<str> = Arc::from(exact_str(&recipient_value)?);
        let agents = attribute(world, intern!(py, "agents"))?;
        if sender_id == recipient_id || !agents.contains(&*sender_id)? || !agents.contains(&*recipient_id)? {
            return unsupported("the participants are for the Python rule to judge");
        }
        // The sender's belief and the recipient's, the claim, and the signal.
        let belief_value = payload.get_item("belief")?;
        let belief_text = exact_str(&belief_value)?;
        let Some(wanted_rank) = belief_rank(belief_text) else {
            return unsupported("the belief is for the Python rule to judge");
        };
        let (truth, claim_source, sender_belief, sender_source, sender_event, previous, pressure) = NamespaceMirror::with_row::<InformationRow, _>(&mirror, |row| {
            let Some(message) = row.messages.get(&*message_id) else {
                return unsupported("an unknown message");
            };
            if !message.parses() {
                return unsupported("a message is for the Python rule to read");
            }
            let (Some(sender), Some(recipient)) = (row.beliefs.get(&*sender_id), row.beliefs.get(&*recipient_id)) else {
                return unsupported("a person has no beliefs");
            };
            let Some(known) = sender.get(&*message_id) else {
                return unsupported("the sender no longer knows the message");
            };
            if !known.parses() {
                return unsupported("a belief is for the Python rule to read");
            }
            let previous = recipient.get(&*message_id).map(prior_of).transpose()?;
            Ok((
                message.truth_status.clone(),
                message.source_id.clone(),
                known.belief.clone(),
                known.source.clone(),
                known.last_event_id.clone(),
                previous,
                row.query_pressure.get(&*recipient_id).copied(),
            ))
        })?;
        if &*sender_belief != belief_text {
            return unsupported("the sender's belief changed");
        }
        let Some(basis) = contact_basis(world, &sender_id, &recipient_id)? else {
            return unsupported("no longer in a contact zone");
        };
        if !get_is(&payload, "contact_basis", basis)? {
            return unsupported("the sampled basis is not current");
        }
        let contact_minutes = exact_int(&payload.get_item("contact_minutes")?)?;
        if contact_minutes < exact_int(&config.getattr(intern!(py, "minimum_contact_minutes"))?)? {
            return unsupported("the duration is below the minimum");
        }
        let probability = number(&get(&provenance, "transmission_probability")?)?;
        if !(0.0 < probability && probability <= 1.0) {
            return unsupported("the probability is invalid");
        }
        if !text_is(&attribute(event, intern!(py, "source_id"))?, &claim_source) || !text_is(&attribute(event, intern!(py, "causation_id"))?, &sender_event) {
            return unsupported("the provenance does not match the sender");
        }
        let correcting = matches!(belief_text, "verified" | "rejected");
        if correcting && !matches!((&*truth, belief_text), ("true", "verified") | ("false", "rejected")) {
            return unsupported("the correction conflicts with the truth");
        }
        if let Some(prior) = &previous {
            let Some(rank) = belief_rank(&prior.belief) else {
                return unsupported("a belief is for the Python rule to read");
            };
            if rank >= wanted_rank && !(aware && correcting) {
                return unsupported("the recipient already has as strong a belief");
            }
        }
        let event_id_value = attribute(event, intern!(py, "event_id"))?;
        let event_id: Arc<str> = Arc::from(exact_str(&event_id_value)?);
        let mut accepted = true;
        if aware && correcting {
            let reliability = reliability_of(&config, &sender_source)?;
            let trust = trust_of(world, &recipient_id)?;
            let chance = acceptance_probability(&config, trust, reliability)?;
            accepted = draw(&seed_of(world)?, "contact-correction-acceptance-v1", due, &event_id, &recipient_id, &message_id) < chance;
        }
        let strength = if aware && belief_text == "heard" {
            sample_strength(world, &config, &recipient_id, &claim_source, &event_id, due, &message_id)?
        } else {
            previous.as_ref().map_or(0.5, |prior| prior.belief_strength)
        };
        let record = Belief {
            message_id: message_id.clone(),
            belief: if accepted {
                Arc::from(belief_text)
            } else {
                previous.as_ref().map_or_else(|| Arc::from("heard"), |prior| prior.belief.clone())
            },
            source: sender_id.clone(),
            last_contact: due,
            last_event_id: event_id,
            belief_strength: if accepted && correcting {
                if belief_text == "verified" { 1.0 } else { 0.0 }
            } else {
                strength
            },
            share_propensity: previous.as_ref().map_or(0.5, |prior| prior.share_propensity),
            correction_exposed: correcting || previous.as_ref().is_some_and(|prior| prior.correction_exposed),
            correction_accepted: (accepted && correcting) || previous.as_ref().is_some_and(|prior| prior.correction_accepted),
            last_correction_accepted: if correcting { accepted } else { previous.as_ref().is_some_and(|prior| prior.last_correction_accepted) },
            ever_misbelieved: previous.as_ref().is_some_and(|prior| prior.ever_misbelieved) || misbelieved(&truth, strength),
            ever_forgotten: previous.as_ref().is_some_and(|prior| prior.ever_forgotten),
        };
        if !record.is_valid() {
            return unsupported("a belief for the Python rule to judge");
        }
        let Some(pressure) = pressure else {
            return unsupported("the person has no record");
        };
        // `heard` adds, an accepted correction relieves and counts, and every contact counts.
        let heard = belief_text == "heard";
        let next = if heard {
            Some(py_min(1.0, pressure + setting(&config, intern!(py, "query_pressure_per_claim"))?))
        } else if accepted {
            Some(py_max(0.0, pressure - setting(&config, intern!(py, "query_pressure_relief_per_correction"))?))
        } else {
            None
        };
        Ok((state, message_id, recipient_id, record, next, !heard && accepted))
    })();
    let (state, message_id, recipient_id, record, next, delivered) = counted(py, "contact", refused_by_python(py, plan))?;
    state.get_item("beliefs")?.get_item(&*recipient_id)?.set_item(&*message_id, record.to_py(py)?)?;
    if let Some(next) = next {
        state.get_item("query_pressure")?.set_item(&*recipient_id, next)?;
    }
    if delivered {
        add_counter(&state, "correction_deliveries")?;
    }
    add_counter(&state, "contact_deliveries")
}

/// `_sample_claim_belief_strength`, for the tests: a draw hides a small change in its probability.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn claim_strength_of(world: &Bound<'_, PyAny>, config: &Bound<'_, PyAny>, agent_id: &str, source_id: &str, event_id: &str, minute: i64, message_id: &str) -> PyResult<f64> {
    sample_strength(world, config, agent_id, source_id, event_id, minute, message_id)
}

/// A correction's acceptance as the rules draw it, for the tests.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn acceptance_of(world: &Bound<'_, PyAny>, config: &Bound<'_, PyAny>, label: &str, trust: f64, source_id: &str, event_id: &str, minute: i64, agent_id: &str, message_id: &str) -> PyResult<bool> {
    let chance = acceptance_probability(config, trust, reliability_of(config, source_id)?)?;
    Ok(draw(&seed_of(world)?, label, minute, event_id, agent_id, message_id) < chance)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(claim_strength_of, m)?)?;
    m.add_function(wrap_pyfunction!(acceptance_of, m)?)?;
    m.add_function(wrap_pyfunction!(rumor, m)?)?;
    m.add_function(wrap_pyfunction!(correction, m)?)?;
    m.add_function(wrap_pyfunction!(contact, m)?)?;
    Ok(())
}
