//! The information model's sample step (`_sample_information_contacts`, `InformationSpreadPolicy.update`
//! and its two subclasses): the bounded local contacts of a minute, and the claims they may pass on.
//!
//! As in `rules/psychology.rs`, `plan` computes everything from the rows and changes nothing; any
//! value the Python rule would turn down, or that this kernel does not handle, gives Unsupported,
//! so the Python step runs and raises its own text. Then the plan goes to the row and the tree
//! value by value, and the namespace is never forwarded whole. Every float operation keeps the
//! order of the Python expression. The events of the transfers are scheduled by the Python method
//! the Python step uses, after the state is written, as the Python step does.

use std::collections::HashMap;
use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PyString, PyType};

use crate::contacts::{
    exact_int, key_head, nearby_buckets, pair_key, quoted_ids, select_contacts, zone_members, Basis,
    Bucket,
};
use crate::core::mathx::{expm1, py_max, py_min};
use crate::core::rng::{start_draw_log, take_draw_log, Key, KeyedDraws};
use crate::core::row::{map_set, Field};
use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::core::world_core::{mirror_of, raw_dict_get, NamespaceMirror};
use crate::state::entities::{entities_of, EntityStore};
use crate::state::events::{EventKind, EventRow, EventWriter};
use crate::state::information::{belief_rank, ContactRow, InformationRow, NAME, SCHEMA_VERSION};
use crate::Unsupported;

fn unsupported<T>(reason: &'static str) -> PyResult<T> {
    Err(Unsupported::new_err(reason))
}

// ---- What the Python step reads besides the rows ------------------------------------------------

#[derive(Clone, Copy, PartialEq)]
enum Model {
    Sparse,
    Hazard,
    EvidenceAware,
}

impl Model {
    fn version(self) -> &'static str {
        match self {
            Model::Sparse => "sparse-information-contact-v1",
            Model::Hazard => "hazard-information-v1",
            Model::EvidenceAware => "evidence-aware-rumor-v1",
        }
    }
}

struct KnownTypes {
    sparse: Py<PyType>,
    hazard: Py<PyType>,
    evidence_aware: Py<PyType>,
    counter_rng: Py<PyType>,
}

fn known_types(py: Python<'_>) -> PyResult<&KnownTypes> {
    static TYPES: GILOnceCell<KnownTypes> = GILOnceCell::new();
    TYPES.get_or_try_init(py, || {
        let class = |module: &str, name: &str| -> PyResult<Py<PyType>> {
            Ok(py.import(module)?.getattr(name)?.downcast_into::<PyType>()?.unbind())
        };
        Ok(KnownTypes {
            sparse: class("sheltersim.policies.information", "InformationSpreadPolicy")?,
            hazard: class("sheltersim.policies.alternatives", "HazardInformationPolicy")?,
            evidence_aware: class("sheltersim.policies.information", "EvidenceAwareInformationPolicy")?,
            counter_rng: class("sheltersim.core.rng", "CounterRNG")?,
        })
    })
}

/// An exact float, or an exact int the config allows (0..=4 at most): both are the same f64 in the
/// Python expressions, where each meets a float.
fn number(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    if let Ok(float) = value.downcast_exact::<PyFloat>() {
        return Ok(float.value());
    }
    let int = exact_int(value)?;
    if !(0..=1 << 53).contains(&int) {
        return unsupported("a setting is out of range");
    }
    Ok(int as f64)
}

/// The config's values the rule reads, and the rule's derived forgetting probability.
struct Settings {
    maximum_contacts: i64,
    minimum_contact_minutes: i64,
    duration_scale: f64,
    base_probability: f64,
    correction_multiplier: f64,
    trust_sensitivity: f64,
    family_multiplier: f64,
    adjacent_multiplier: f64,
    pressure_decay: f64,
    distress_sensitivity: f64,
    share_decay: f64,
    forget_probability: f64,
}

impl Settings {
    fn read(config: &Bound<'_, PyAny>, model: Model) -> PyResult<Self> {
        let py = config.py();
        let enabled = config.getattr(intern!(py, "enabled"))?;
        if !enabled.is_exact_instance_of::<pyo3::types::PyBool>() || !enabled.is_truthy()? {
            return unsupported("the model is not enabled");
        }
        let maximum_contacts = exact_int(&config.getattr(intern!(py, "max_contacts_per_agent"))?)?;
        let minimum_contact_minutes = exact_int(&config.getattr(intern!(py, "minimum_contact_minutes"))?)?;
        let scale = exact_int(&config.getattr(intern!(py, "contact_duration_scale_minutes"))?)?;
        if !(0..=16).contains(&maximum_contacts) || !(1..=1 << 53).contains(&scale) {
            return unsupported("a contact setting is out of range");
        }
        let read = |name: &Bound<'_, PyString>| number(&config.getattr(name)?);
        let forgetting = read(intern!(py, "forgetting_rate_per_minute"))?;
        let forget_probability = if model == Model::EvidenceAware {
            let Some(result) = expm1(-forgetting) else {
                return unsupported("the forgetting rate is out of range");
            };
            -result
        } else {
            0.0
        };
        Ok(Self {
            maximum_contacts,
            minimum_contact_minutes,
            duration_scale: scale as f64,
            base_probability: read(intern!(py, "base_transmission_probability"))?,
            correction_multiplier: read(intern!(py, "correction_transmission_multiplier"))?,
            trust_sensitivity: read(intern!(py, "trust_sensitivity"))?,
            family_multiplier: read(intern!(py, "family_contact_multiplier"))?,
            adjacent_multiplier: read(intern!(py, "adjacent_zone_contact_multiplier"))?,
            pressure_decay: read(intern!(py, "query_pressure_decay_per_minute"))?,
            distress_sensitivity: read(intern!(py, "distress_sharing_sensitivity"))?,
            share_decay: read(intern!(py, "share_propensity_decay_per_minute"))?,
            forget_probability,
        })
    }
}

struct Inputs<'py> {
    world: Bound<'py, PyAny>,
    model: Model,
    settings: Settings,
    minute: i64,
    seed: String,
    neighbors: HashMap<String, Vec<String>>,
    /// `population_presence_v1.people`, for the household each view reports; None for no such dict.
    presence_people: Option<Bound<'py, PyAny>>,
}

/// What the Python step reads besides the rows, or None when no model is installed: the Python
/// step then does nothing.
fn inputs<'py>(
    controller: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    rng: &Bound<'py, PyAny>,
) -> PyResult<Option<Inputs<'py>>> {
    let py = controller.py();
    let policy = controller.getattr(intern!(py, "information_policy"))?;
    if policy.is_none() {
        return Ok(None);
    }
    let known = known_types(py)?;
    let kind = policy.get_type();
    let model = if kind.is(&known.sparse) {
        Model::Sparse
    } else if kind.is(&known.evidence_aware) {
        Model::EvidenceAware
    } else if kind.is(&known.hazard) {
        Model::Hazard
    } else {
        return unsupported("a model this kernel does not know");
    };
    if !rng.get_type().is(&known.counter_rng) {
        return unsupported("the random source is not a CounterRNG");
    }
    let trace_level = controller.getattr(intern!(py, "artifacts"))?.getattr(intern!(py, "trace_level"))?;
    if trace_level.downcast::<PyString>()?.to_str()? != "off" {
        return unsupported("a trace records the contacts");
    }
    let Ok(minute) = minute.downcast_exact::<PyInt>() else {
        return unsupported("the minute is not an int");
    };
    let minute: i64 = minute.extract().map_err(|_| Unsupported::new_err("the minute is too large"))?;
    let seed = exact_int(&rng.getattr(intern!(py, "seed"))?)?;
    if minute < 0 || minute == i64::MAX || seed < 0 {
        return unsupported("the minute or the seed is out of range");
    }
    let world = controller.getattr(intern!(py, "world"))?;
    // The Python rule reads the view's minute.
    if exact_int(&world.getattr(intern!(py, "minute_index"))?)? != minute {
        return unsupported("the world is at another minute");
    }
    let settings = Settings::read(&policy.getattr(intern!(py, "config"))?, model)?;
    let neighbors: HashMap<String, Vec<String>> = controller
        .getattr(intern!(py, "zone_neighbors"))?
        .extract()
        .map_err(|_| Unsupported::new_err("the zone neighbors are not str tuples"))?;
    let presence = world
        .getattr(intern!(py, "model_state"))?
        .call_method1(intern!(py, "get"), (intern!(py, "population_presence_v1"),))?;
    let presence_people = match as_readable_dict(&presence) {
        Some(state) => state.get_item(intern!(py, "people"))?,
        None if presence.is_none() => None,
        None if presence.is_instance_of::<PyDict>() => {
            return unsupported("the presence state is another mapping");
        }
        None => None,
    };
    if let Some(people) = &presence_people {
        if as_readable_dict(people).is_none() {
            return unsupported("the presence state's people are not a dict");
        }
    }
    Ok(Some(Inputs { world, model, settings, minute, seed: seed.to_string(), neighbors, presence_people }))
}

// ---- The plan -----------------------------------------------------------------------------------

/// One person of the view, in ID order.
struct Person<'a> {
    id: &'a str,
    zone: &'a str,
    trust: f64,
    stress: f64,
    /// `group_id` of the view, read only if it can matter: somebody holds a belief.
    group: Option<Arc<str>>,
    /// The position of the person's beliefs in the row.
    beliefs: usize,
}

/// A belief after the forgetting of this tick.
#[derive(Clone, Copy)]
struct Current<'a> {
    rank: u8,
    status: &'a str,
    share: f64,
}

struct BeliefChange {
    agent: usize,
    message: usize,
    share: f64,
    forgotten: bool,
}

struct Proposal {
    message: Arc<str>,
    sender: usize,
    recipient: usize,
    belief: Arc<str>,
    basis: &'static str,
    minutes: i64,
    probability: f64,
    source: Arc<str>,
    causation: Arc<str>,
}

/// One contact of `InformationContactTrace`, for the bit comparison only.
struct ContactTrace {
    sender: usize,
    recipient: usize,
    basis: &'static str,
    minutes: i64,
    message: Option<Arc<str>>,
    belief: Option<Arc<str>>,
    probability: f64,
    selected: bool,
    source: Option<Arc<str>>,
    causation: Option<Arc<str>>,
}

struct Plan {
    minute: i64,
    ids: Vec<Arc<str>>,
    pressure: Vec<f64>,
    beliefs: Vec<BeliefChange>,
    contacts: Vec<(Arc<str>, ContactRow)>,
    contact_attempts: i64,
    proposals: Vec<Proposal>,
    traces: Vec<ContactTrace>,
}

fn basis_name(basis: Basis) -> &'static str {
    match basis {
        Basis::Family => "family",
        Basis::SameZone => "same_zone",
        Basis::Nearby => "adjacent_zone",
    }
}

/// `InformationSpreadPolicy._transmission_probability`, and its override in
/// `HazardInformationPolicy`.
fn transmission_probability(
    settings: &Settings,
    model: Model,
    trust: f64,
    basis: Basis,
    contact_minutes: i64,
    correction: bool,
) -> PyResult<f64> {
    let duration_factor = py_min(1.0, contact_minutes as f64 / settings.duration_scale);
    let trust_factor = 1.0 + settings.trust_sensitivity * (2.0 * trust - 1.0);
    let basis_factor = match basis {
        Basis::Family => settings.family_multiplier,
        Basis::SameZone => 1.0,
        Basis::Nearby => settings.adjacent_multiplier,
    };
    let correction_factor = if correction { settings.correction_multiplier } else { 1.0 };
    let propensity = py_min(
        1.0,
        settings.base_probability * duration_factor * trust_factor * basis_factor * correction_factor,
    );
    if model != Model::Hazard {
        return Ok(propensity);
    }
    match expm1(-propensity) {
        Some(result) => Ok(-result),
        None => unsupported("the hazard is out of range"),
    }
}

/// `_sender_sharing_factor`.
fn sharing_factor(settings: &Settings, model: Model, share: f64, stress: f64) -> f64 {
    if model != Model::EvidenceAware {
        return 1.0;
    }
    let distress_multiplier = 1.0 + settings.distress_sensitivity * stress;
    py_min(1.0, share * distress_multiplier)
}

/// The view's `group_id`: the household of the presence record, or the profile's.
fn group_of(
    people: Option<&Bound<'_, PyAny>>,
    id: &str,
    profile_group: &Option<Arc<str>>,
) -> PyResult<Option<Arc<str>>> {
    let Some(people) = people else {
        return Ok(profile_group.clone());
    };
    let py = people.py();
    let Some(record) = raw_dict_get(people, PyString::new(py, id).as_any())? else {
        return Ok(profile_group.clone());
    };
    let Some(record) = as_readable_dict(&record) else {
        if record.is_instance_of::<PyDict>() {
            return unsupported("a presence record is another mapping");
        }
        return Ok(profile_group.clone());
    };
    let Some(household) = record.get_item(intern!(py, "household_id"))? else {
        return Ok(profile_group.clone());
    };
    if household.is_none() {
        return Ok(None);
    }
    let Ok(household) = household.downcast_exact::<PyString>() else {
        return unsupported("a household ID is not a str");
    };
    Ok(Some(Arc::from(household.to_str().map_err(|_| Unsupported::new_err("not UTF-8"))?)))
}

/// `EvidenceAwareInformationPolicy.update` before the contacts: sharing fades, and some people
/// forget. Returns each belief as the contacts see it, by position in the row, and what changes.
fn forget<'a>(
    row: &'a InformationRow,
    settings: &Settings,
    model: Model,
    minute: i64,
    draws: &KeyedDraws,
    recording: bool,
) -> (Vec<Vec<Current<'a>>>, Vec<BeliefChange>) {
    let mut changes = Vec::new();
    let current = row
        .beliefs
        .iter()
        .enumerate()
        .map(|(agent_place, (agent_id, records))| {
            records
                .iter()
                .enumerate()
                .map(|(message_place, (message_id, record))| {
                    let rank = belief_rank(&record.belief).unwrap_or(0);
                    if model != Model::EvidenceAware {
                        return Current { rank, status: &record.belief, share: record.share_propensity };
                    }
                    // A draw is never below 0.0, so a zero probability needs none.
                    let forgotten = (settings.forget_probability > 0.0 || recording) && {
                        let mut key = Key::labelled("information-forgetting-v1");
                        key.int(minute).text(agent_id).text(message_id);
                        key.draw(draws) < settings.forget_probability
                    };
                    let share = if forgotten {
                        0.0
                    } else {
                        py_max(0.0, record.share_propensity - settings.share_decay)
                    };
                    if forgotten || share.to_bits() != record.share_propensity.to_bits() {
                        changes.push(BeliefChange { agent: agent_place, message: message_place, share, forgotten });
                    }
                    if forgotten {
                        Current { rank: 0, status: "unknown", share }
                    } else {
                        Current { rank, status: &record.belief, share }
                    }
                })
                .collect()
        })
        .collect();
    (current, changes)
}

fn plan(
    inputs: &Inputs<'_>,
    entities: &crate::state::entities::Entities,
    row: &InformationRow,
    draws: &KeyedDraws,
    recording: bool,
) -> PyResult<Plan> {
    if row.schema_version != SCHEMA_VERSION {
        return unsupported("the state has another schema");
    }
    if !row.beliefs_parse() {
        return unsupported("the claims or beliefs do not parse");
    }
    let settings = &inputs.settings;
    let minute = inputs.minute;
    let any_belief = row.beliefs.values().any(|records| !records.is_empty());

    let mut agents: Vec<_> = entities.agents.iter().collect();
    agents.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let mut people = Vec::with_capacity(agents.len());
    let mut pressure = Vec::with_capacity(agents.len());
    for (id, agent) in &agents {
        if ***id != *agent.profile.id {
            return unsupported("a person is under another ID");
        }
        let (Some(beliefs), Some(value)) = (row.beliefs.get_index_of(&***id), row.query_pressure.get(&***id)) else {
            return unsupported("a person has no beliefs or no query pressure");
        };
        pressure.push(py_max(0.0, *value - settings.pressure_decay));
        let group = if any_belief || recording {
            group_of(inputs.presence_people.as_ref(), id, &agent.profile.group_id)?
        } else {
            None
        };
        people.push(Person {
            id,
            zone: &agent.zone_id,
            trust: agent.trust,
            stress: agent.stress,
            group,
            beliefs,
        });
    }

    let (current, belief_changes) = forget(row, settings, inputs.model, minute, draws, recording);

    let id_texts: Vec<&str> = people.iter().map(|person| person.id).collect();
    let zone_texts: Vec<&str> = people.iter().map(|person| person.zone).collect();
    let quoted = quoted_ids(&id_texts);
    let members = zone_members(&zone_texts);

    let mut contacts: Vec<(Arc<str>, ContactRow)> = Vec::with_capacity(row.contacts.len());
    let mut attempts = row.counters.contact_attempts;
    let mut proposals = Vec::new();
    let mut traces = Vec::new();
    let mut planned: Vec<(usize, &str)> = Vec::new();
    let mut selected = Vec::new();
    for (me, person) in people.iter().enumerate() {
        let zone = person.zone;
        let mut buckets = Vec::new();
        let same_zone = &members[zone];
        if same_zone.len() > 1 {
            buckets.push(Bucket { basis: Basis::SameZone, members: same_zone, includes_self: true });
        }
        for neighbors in nearby_buckets(zone, &inputs.neighbors, &members) {
            buckets.push(Bucket { basis: Basis::Nearby, members: neighbors, includes_self: false });
        }
        let mut head = key_head("information-contact-partner-v1", minute, &quoted[me]);
        select_contacts(draws, &mut head, me, &buckets, settings.maximum_contacts, false, &mut selected);
        for &(source, drawn_basis) in &selected {
            let sender = &people[source];
            let basis = if person.group.is_some() && person.group == sender.group {
                Basis::Family
            } else {
                drawn_basis
            };
            let key = pair_key(&quoted[me], &quoted[source])?;
            let contact_minutes = match row.contacts.get(key.as_str()) {
                Some(previous) if previous.last_seen_minute == minute - 1 => {
                    match previous.consecutive_minutes.checked_add(1) {
                        Some(minutes) if (minutes as u64) < 1 << 53 => minutes,
                        _ => return unsupported("a contact's minutes do not fit"),
                    }
                }
                _ => 1,
            };
            contacts.push((
                Arc::from(key),
                ContactRow { last_seen_minute: minute, consecutive_minutes: contact_minutes },
            ));
            attempts = match attempts.checked_add(1) {
                Some(attempts) => attempts,
                None => return unsupported("the contact counter does not fit"),
            };

            let trace = |message, belief, probability, selected, message_source, causation| ContactTrace {
                sender: source,
                recipient: me,
                basis: basis_name(basis),
                minutes: contact_minutes,
                message,
                belief,
                probability,
                selected,
                source: message_source,
                causation,
            };

            // The transferable claim first by rank, then age, then ID, as in the Python min().
            let sender_records = &row.beliefs[sender.beliefs];
            let sender_current = &current[sender.beliefs];
            let recipient_records = &row.beliefs[person.beliefs];
            let recipient_current = &current[person.beliefs];
            let mut best: Option<(usize, i64)> = None;
            for (place, (message_id, _)) in sender_records.iter().enumerate() {
                let rank = sender_current[place].rank;
                let receiver_rank =
                    recipient_records.get_index_of(&**message_id).map_or(0, |index| recipient_current[index].rank);
                if rank <= receiver_rank {
                    continue;
                }
                let time = row.messages[&**message_id].time;
                let better = match best {
                    None => true,
                    Some((other, other_time)) => {
                        let other_id = sender_records.get_index(other).map_or("", |(id, _)| &**id);
                        (std::cmp::Reverse(rank), time, message_id.as_bytes())
                            < (std::cmp::Reverse(sender_current[other].rank), other_time, other_id.as_bytes())
                    }
                };
                if better {
                    best = Some((place, time));
                }
            }
            let Some((place, _)) = best else {
                if recording {
                    traces.push(trace(None, None, 0.0, false, None, None));
                }
                continue;
            };
            let Some((message_id, record)) = sender_records.get_index(place) else {
                return unsupported("a belief left the row");
            };
            let belief = sender_current[place];
            let message = &row.messages[&**message_id];
            if contact_minutes < settings.minimum_contact_minutes {
                if recording {
                    traces.push(trace(
                        Some(message_id.clone()),
                        Some(Arc::from(belief.status)),
                        0.0,
                        false,
                        Some(message.source_id.clone()),
                        Some(record.last_event_id.clone()),
                    ));
                }
                continue;
            }
            let propensity =
                transmission_probability(settings, inputs.model, person.trust, basis, contact_minutes, belief.rank == 2)?;
            let probability = py_min(
                1.0,
                propensity * sharing_factor(settings, inputs.model, belief.share, sender.stress),
            );
            let mut key = Key::labelled("information-contact-transfer-v1");
            key.int(minute).quoted(&quoted[me]).quoted(&quoted[source]).text(message_id).text(belief.status);
            let transfers = key.draw(draws) < probability;
            if recording {
                traces.push(trace(
                    Some(message_id.clone()),
                    Some(Arc::from(belief.status)),
                    probability,
                    transfers,
                    Some(message.source_id.clone()),
                    Some(record.last_event_id.clone()),
                ));
            }
            if transfers && !planned.contains(&(me, &**message_id)) {
                planned.push((me, message_id));
                proposals.push(Proposal {
                    message: message_id.clone(),
                    sender: source,
                    recipient: me,
                    belief: Arc::from(belief.status),
                    basis: basis_name(basis),
                    minutes: contact_minutes,
                    probability,
                    source: message.source_id.clone(),
                    causation: record.last_event_id.clone(),
                });
            }
        }
    }
    // People are in ID order, so their places sort as their IDs do.
    proposals.sort_by(|a, b| {
        (a.recipient, a.sender, a.message.as_bytes()).cmp(&(b.recipient, b.sender, b.message.as_bytes()))
    });
    Ok(Plan {
        minute,
        ids: agents.iter().map(|(id, _)| Arc::clone(id)).collect(),
        pressure,
        beliefs: belief_changes,
        contacts,
        contact_attempts: attempts,
        proposals,
        traces,
    })
}

// ---- The writes ---------------------------------------------------------------------------------

fn write(mirror: &Bound<'_, NamespaceMirror>, plan: &Plan) -> PyResult<()> {
    let py = mirror.py();
    NamespaceMirror::with_row_tree::<InformationRow, _>(mirror, |row, tree| {
        if !plan.beliefs.is_empty() {
            let beliefs = tree.child(intern!(py, "beliefs"))?;
            let unknown: Arc<str> = Arc::from("unknown");
            for change in &plan.beliefs {
                let Some((agent_id, records)) = row.beliefs.get_index_mut(change.agent) else {
                    return Err(PyRuntimeError::new_err("the row changed under the plan"));
                };
                let Some((message_id, record)) = records.get_index_mut(change.message) else {
                    return Err(PyRuntimeError::new_err("the row changed under the plan"));
                };
                let node = beliefs.child_str(agent_id)?.child_str(message_id)?;
                record.set_share_propensity(&node, change.share)?;
                if change.forgotten {
                    record.set_belief(&node, unknown.clone())?;
                    record.set_ever_forgotten(&node, true)?;
                }
            }
        }
        let pressure = tree.child(intern!(py, "query_pressure"))?;
        for (id, value) in plan.ids.iter().zip(&plan.pressure) {
            map_set(&mut row.query_pressure, &pressure, id, *value)?;
        }
        row.counters.set_contact_attempts(&tree.child(intern!(py, "counters"))?, plan.contact_attempts)?;
        row.contacts.clear();
        for (key, contact) in &plan.contacts {
            row.contacts.insert(key.clone(), *contact);
        }
        Ok(())
    })?;
    // Every contact's last minute is this one, and Python's dicts share its one int too.
    let minute = plan.minute.to_py(py)?;
    NamespaceMirror::refill_record_map(
        mirror,
        intern!(py, "contacts"),
        [intern!(py, "last_seen_minute"), intern!(py, "consecutive_minutes")],
        plan.contacts.iter().map(|(key, contact)| {
            Ok((PyString::new(py, key), [minute.clone(), contact.consecutive_minutes.to_py(py)?]))
        }),
    )
}

/// The plan, from the rows it reads.
fn with_plan<'py>(
    inputs: &Inputs<'py>,
    mirror: &Bound<'py, NamespaceMirror>,
    draws: &KeyedDraws,
    recording: bool,
) -> PyResult<Plan> {
    EntityStore::with_rows(&entities_of(&inputs.world)?, |entities| {
        NamespaceMirror::with_row::<InformationRow, _>(mirror, |row| plan(inputs, entities, row, draws, recording))
    })
}

// ---- The step -----------------------------------------------------------------------------------

/// `_sample_information_contacts` of the sample phase (experiments/phases/segments.py).
#[pyfunction]
fn sample_contacts<'py>(
    controller: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    rng: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = controller.py();
    let Some(inputs) = inputs(controller, minute, rng)? else {
        return Ok(());
    };
    let mirror = mirror_of(&inputs.world, NAME)?;
    let writer = EventWriter::of(&inputs.world)?;
    let draws = KeyedDraws::new(&inputs.seed);
    let plan = with_plan(&inputs, &mirror, &draws, false)?;
    write(&mirror, &plan)?;
    let schedule = intern!(py, "_schedule_information_contact");
    for proposal in &plan.proposals {
        // `_schedule_information_contact`, which stays for what a row refuses: it raises its own
        // error. The state is written, so nothing may take that for the kernel's refusal.
        let due_minute = inputs.minute + 1;
        if due_minute >= writer.duration() {
            continue;
        }
        let sequence = writer.next_sequence()?;
        match contact_event(proposal, &plan, inputs.model, due_minute, sequence) {
            Some(row) => {
                writer.allocate_sequence()?;
                writer.schedule(row)?;
            }
            None => {
                controller
                    .call_method1(
                        schedule,
                        (
                            inputs.minute,
                            &*proposal.message,
                            &*plan.ids[proposal.sender],
                            &*plan.ids[proposal.recipient],
                            &*proposal.belief,
                            proposal.basis,
                            proposal.minutes,
                            proposal.probability,
                            &*proposal.source,
                            &*proposal.causation,
                        ),
                    )
                    .map_err(|error| {
                        if error.is_instance_of::<Unsupported>(py) {
                            PyRuntimeError::new_err(error.to_string())
                        } else {
                            error
                        }
                    })?;
            }
        }
    }
    Ok(())
}

/// The event of `_schedule_information_contact`, with the keys in its order.
fn contact_event(proposal: &Proposal, plan: &Plan, model: Model, due_minute: i64, sequence: i64) -> Option<EventRow> {
    let text = |value: &str| V::Str(Arc::from(value));
    let mut payload = IndexMap::with_capacity(6);
    payload.insert(Arc::from("message_id"), V::Str(proposal.message.clone()));
    payload.insert(Arc::from("sender_agent_id"), V::Str(plan.ids[proposal.sender].clone()));
    payload.insert(Arc::from("recipient_agent_id"), V::Str(plan.ids[proposal.recipient].clone()));
    payload.insert(Arc::from("belief"), V::Str(proposal.belief.clone()));
    payload.insert(Arc::from("contact_basis"), text(proposal.basis));
    payload.insert(Arc::from("contact_minutes"), V::Int(proposal.minutes));
    let mut provenance = IndexMap::with_capacity(4);
    provenance.insert(Arc::from("source_type"), text("assumption"));
    provenance.insert(Arc::from("source_id"), text("ASM-INFORMATION-SPREAD-V1"));
    provenance.insert(Arc::from("policy_version"), text(model.version()));
    provenance.insert(Arc::from("transmission_probability"), V::Float(proposal.probability));
    EventRow::new(
        Arc::from(format!("information-contact-{sequence}")),
        EventKind::Endogenous,
        due_minute,
        40,
        sequence,
        Arc::from("information_contact_update"),
        payload,
        Some(proposal.source.clone()),
        Some(proposal.causation.clone()),
        provenance,
    )
}

// ---- For the bit comparisons of benchmarks/rule_shadow and tests/integration ---------------------

fn optional<'py>(py: Python<'py>, text: &Option<Arc<str>>) -> Bound<'py, PyAny> {
    match text {
        Some(text) => PyString::new(py, text).into_any(),
        None => py.None().into_bound(py),
    }
}

/// What this tick would do, changing nothing: the draws in the order they were made, as
/// `(key, value)`, each contact as the Python rule's `InformationContactTrace` fields, the
/// model's version and its forgetting probability, which no contact shows. Says Unsupported exactly when `sample_contacts` would. The planned draws show
/// in `core/rng.rs`'s log of this thread, which this leaves off.
#[pyfunction]
fn planned_contacts<'py>(
    controller: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    rng: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    let py = controller.py();
    let Some(inputs) = inputs(controller, minute, rng)? else {
        return Ok(None);
    };
    let mirror = mirror_of(&inputs.world, NAME)?;
    let draws = KeyedDraws::new(&inputs.seed);
    start_draw_log();
    let plan = with_plan(&inputs, &mirror, &draws, true);
    let made = take_draw_log();
    let plan = plan?;
    let document = PyDict::new(py);
    document.set_item("draws", made)?;
    let contacts = PyList::empty(py);
    for trace in &plan.traces {
        contacts.append((
            &*plan.ids[trace.sender],
            &*plan.ids[trace.recipient],
            trace.basis,
            trace.minutes,
            optional(py, &trace.message),
            optional(py, &trace.belief),
            trace.probability,
            trace.selected,
            optional(py, &trace.source),
            optional(py, &trace.causation),
        ))?;
    }
    document.set_item("contacts", contacts)?;
    document.set_item("policy_version", inputs.model.version())?;
    document.set_item("forget_probability", inputs.settings.forget_probability)?;
    Ok(Some(document))
}

/// `-expm1(-rate)`, the forgetting probability, and `_transmission_probability` for the tests.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn transmission_probability_of(
    hazard: bool,
    trust: f64,
    family: bool,
    adjacent: bool,
    contact_minutes: i64,
    correction: bool,
    duration_scale: f64,
    base_probability: f64,
    correction_multiplier: f64,
    trust_sensitivity: f64,
    family_multiplier: f64,
    adjacent_multiplier: f64,
) -> PyResult<f64> {
    let settings = Settings {
        maximum_contacts: 0,
        minimum_contact_minutes: 1,
        duration_scale,
        base_probability,
        correction_multiplier,
        trust_sensitivity,
        family_multiplier,
        adjacent_multiplier,
        pressure_decay: 0.0,
        distress_sensitivity: 0.0,
        share_decay: 0.0,
        forget_probability: 0.0,
    };
    let basis = if family {
        Basis::Family
    } else if adjacent {
        Basis::Nearby
    } else {
        Basis::SameZone
    };
    let model = if hazard { Model::Hazard } else { Model::Sparse };
    transmission_probability(&settings, model, trust, basis, contact_minutes, correction)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sample_contacts, m)?)?;
    m.add_function(wrap_pyfunction!(planned_contacts, m)?)?;
    m.add_function(wrap_pyfunction!(transmission_probability_of, m)?)?;
    Ok(())
}
