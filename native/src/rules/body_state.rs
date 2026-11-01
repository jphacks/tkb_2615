//! `record_social_event` of `domain/body_state.py`, with the affect operators of
//! `policies/affect.py` and the trust operators of `policies/relationships.py`. The Python rule
//! stays as the comparison base and for every value this kernel does not decide
//! (docs/rust-port/lanes/m3-body-state.md).
//!
//! One event is one plan: the inputs are read first, the plan is computed from the rows with no
//! Python object in sight, and only then is anything written, so an Unsupported changes nothing and
//! the Python rule then runs as if the kernel weren't there.

use std::sync::Arc;

use pyo3::exceptions::PyRuntimeError;
use pyo3::ffi;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyModule as PyModuleType, PyString};
use smallvec::SmallVec;

use crate::core::mathx::{hypot, py_max, py_min};
use crate::core::row::{attribute, exact_dict, exact_int, exact_str, namespace_is_absent, unsupported, Field, IdMap};
use crate::core::value::V;
use crate::core::world_core::{mirror_of, NamespaceMirror};
use crate::encode;
use crate::state::body_state::{
    new_relationship, BodyRecord, BodyRow, Relationship, MAX_SOCIAL_HISTORY, MAX_SOCIAL_RELATIONSHIPS, NAME,
};
use crate::state::entities::{entities_of, EntityStore};
use crate::state::movement::{MovementRow, Position};

const LINEAR_AFFECT: &str = "linear-positive-negative-v1";
const TRUST_WEIGHTED_AFFECT: &str = "trust-weighted-positive-negative-v1";
const DISTANCE_TRUST: &str = "distance-duration-repeat-trust-v1";
const DISTANCE_TRUST_SOURCE: &str = "ASM-DISTANCE-DURATION-REPEAT-TRUST-V1";
const DURATION_TRUST: &str = "duration-contact-trust-v1";
const DURATION_TRUST_SOURCE: &str = "ASM-DURATION-CONTACT-TRUST-V1";
const RELATIONSHIP_POLICY_VERSION: &str = "1";
const EVIDENCE_AWARE_INFORMATION: &str = "evidence-aware-rumor-v1";
const MOVEMENT: &str = "movement_v1";
/// Larger than this a float's ceil may not fit the row's int.
const LARGEST_DURATION: f64 = 9e15;

// ---- The operators ------------------------------------------------------------------------------

fn unit(value: f64) -> f64 {
    py_min(1.0, py_max(0.0, value))
}

#[derive(Clone, Copy, PartialEq)]
enum Affect {
    Linear,
    TrustWeighted,
}

#[derive(Default, Clone, Copy)]
struct Contribution {
    positive_affect: f64,
    negative_affect: f64,
    reassurance: f64,
    uncertainty: f64,
    discontent: f64,
}

fn bounded_difference(source: f64, target: f64, rate: f64) -> f64 {
    py_max(-rate, py_min(rate, (source - target) * rate))
}

impl Affect {
    fn weighted(self, rate: f64, relationship_trust: f64) -> f64 {
        match self {
            Self::Linear => rate,
            Self::TrustWeighted => rate * (0.5 + unit(relationship_trust)),
        }
    }

    /// `contact` of the operators.
    fn contact(self, source: &BodyRecord, target: &BodyRecord, rate: f64, relationship_trust: f64) -> Contribution {
        let rate = self.weighted(rate, relationship_trust);
        let positive = bounded_difference(source.positive_affect, target.positive_affect, rate);
        let negative = bounded_difference(source.negative_affect, target.negative_affect, rate);
        Contribution {
            positive_affect: positive,
            negative_affect: negative,
            reassurance: py_max(0.0, positive - negative * 0.25),
            uncertainty: py_max(0.0, negative - positive) * 0.25,
            discontent: 0.0,
        }
    }

    /// `social_action` of the operators.
    fn social_action(self, signal: f64, rate: f64, relationship_trust: f64) -> Contribution {
        let effect = self.weighted(rate, relationship_trust) * signal;
        Contribution {
            positive_affect: effect,
            negative_affect: -effect * 0.5,
            reassurance: effect,
            discontent: -effect * 0.5,
            uncertainty: -py_max(0.0, effect),
        }
    }
}

#[derive(Clone, Copy)]
enum TrustPolicy {
    DistanceDurationRepeat { distance_scale_m: f64, repeat_contact_bonus: f64 },
    DurationOnly,
}

impl TrustPolicy {
    /// `update_trust` of the operators. The arguments are the rule's, in its units.
    #[allow(clippy::too_many_arguments)]
    fn update_trust(
        self,
        current: f64,
        duration_minutes: f64,
        interaction_scale_minutes: i64,
        prior_contact_count: i64,
        elapsed_minutes: i64,
        trust_gain: f64,
        trust_decay_per_day: f64,
        distance_m: Option<f64>,
    ) -> f64 {
        let decay = py_min(1.0, py_max(0.0, trust_decay_per_day * elapsed_minutes as f64 / 1440.0));
        let baseline = current * (1.0 - decay) + 0.5 * decay;
        let duration_factor =
            py_min(1.0, py_max(0.0, duration_minutes) / py_max(1.0, interaction_scale_minutes as f64));
        match self {
            Self::DistanceDurationRepeat { distance_scale_m, repeat_contact_bonus } => {
                let distance_factor = match distance_m {
                    Some(distance) => 1.0 / (1.0 + py_max(0.0, distance) / distance_scale_m),
                    None => 1.0,
                };
                let repeats = prior_contact_count.clamp(0, 10);
                let repeat_factor = 1.0 + repeat_contact_bonus * repeats as f64;
                let result = baseline + trust_gain * duration_factor * repeat_factor * distance_factor;
                py_min(1.0, py_max(0.0, result))
            }
            Self::DurationOnly => py_min(1.0, py_max(0.0, baseline + trust_gain * duration_factor)),
        }
    }

    fn identity(self) -> (&'static str, &'static str) {
        match self {
            Self::DistanceDurationRepeat { .. } => (DISTANCE_TRUST, DISTANCE_TRUST_SOURCE),
            Self::DurationOnly => (DURATION_TRUST, DURATION_TRUST_SOURCE),
        }
    }
}

// ---- The event, as plain values ------------------------------------------------------------------

/// A value of a frozen payload. Only what the rule tells apart.
#[derive(Clone)]
enum Item {
    Nothing,
    Bool,
    Int(i64),
    Float(f64),
    Text(Arc<str>),
    Other,
}

impl Item {
    fn read(value: Option<Bound<'_, PyAny>>) -> PyResult<Self> {
        let Some(value) = value else {
            return Ok(Self::Nothing);
        };
        Ok(if value.is_none() {
            Self::Nothing
        } else if value.is_exact_instance_of::<PyBool>() {
            Self::Bool
        } else if value.is_exact_instance_of::<PyInt>() {
            Self::Int(exact_int(&value)?)
        } else if let Ok(number) = value.downcast_exact::<PyFloat>() {
            Self::Float(number.value())
        } else if value.is_exact_instance_of::<PyString>() {
            Self::Text(Arc::from(exact_str(&value)?))
        } else {
            Self::Other
        })
    }

    fn text(&self) -> Option<&Arc<str>> {
        match self {
            Self::Text(text) => Some(text),
            _ => None,
        }
    }

    /// An ID the rule looks up in `world.agents`. None and a number are simply not there; what can
    /// raise there is left to Python, and so is an ID of another type than a str, kept simple.
    fn id(&self) -> PyResult<Option<Arc<str>>> {
        match self {
            Self::Text(text) => Ok(Some(text.clone())),
            Self::Nothing => Ok(None),
            _ => unsupported("an ID is not a str"),
        }
    }

    /// The `duration` that `_record_relationship` computes.
    fn relationship_duration(&self) -> f64 {
        match self {
            Self::Int(value) if *value >= 0 => *value as f64,
            Self::Float(value) if value.is_finite() && *value >= 0.0 => *value,
            _ => 1.0,
        }
    }

    /// `float(duration)` when `record_social_event` writes it into a transfer's deltas.
    fn transfer_duration(&self) -> Option<f64> {
        match self {
            Self::Int(value) if *value > 0 => Some(*value as f64),
            Self::Float(value) if value.is_finite() && *value > 0.0 => Some(*value),
            _ => None,
        }
    }
}

fn payload_of<'py>(event: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let payload = attribute(event, intern!(event.py(), "payload"))?;
    // SAFETY: the type pointers are only compared.
    let kind = unsafe { ffi::Py_TYPE(payload.as_ptr()) };
    if kind != std::ptr::addr_of_mut!(ffi::PyDictProxy_Type) && kind != std::ptr::addr_of_mut!(ffi::PyDict_Type) {
        return unsupported("the payload is not frozen JSON");
    }
    Ok(payload)
}

fn read<'py>(payload: &Bound<'py, PyAny>, key: &Bound<'py, PyString>) -> PyResult<Item> {
    let value = payload.call_method1(intern!(payload.py(), "get"), (key,))?;
    Item::read(Some(value))
}

/// `payload.get(key)`, telling a key that holds None from one that is absent.
fn holds<'py>(payload: &Bound<'py, PyAny>, key: &Bound<'py, PyString>) -> PyResult<bool> {
    payload.contains(key)
}

enum Interaction {
    Cooperation,
    /// `include_uncertainty` is true only for `reassurance_exchange`.
    Transfer { kind: Arc<str>, include_uncertainty: bool },
    ComplaintResponse,
    None,
}

enum Action {
    Proximity { first: Arc<str>, second: Arc<str>, observed_minute: i64, distance: Option<f64>, duration: f64 },
    Contact { sender: Arc<str>, recipient: Arc<str>, topic: Option<Arc<str>>, duration: f64 },
    Social { kind: Interaction, actor: Arc<str>, participant: Option<Arc<str>>, topic: Option<Arc<str>>, duration: f64 },
    Correction { actors: Vec<Arc<str>> },
    Nothing,
}

/// What the items a rule appends all share.
struct EventFacts {
    minute: i64,
    transfer_topic: Option<Arc<str>>,
    transfer_duration: Option<f64>,
}

// ---- The plan ------------------------------------------------------------------------------------

/// A delta of a history item: a number or a name.
enum DeltaValue {
    Number(f64),
    Name(&'static str),
}

struct PlannedItem {
    minute: i64,
    kind: Arc<str>,
    actors: SmallVec<[Arc<str>; 2]>,
    topic: Option<Arc<str>>,
    deltas: SmallVec<[(&'static str, DeltaValue); 9]>,
}

/// A relationship after the event, as `new` when the row has no such key yet.
struct PlannedRelationship {
    key: Arc<str>,
    record: Relationship,
    new: bool,
    new_topics: bool,
}

#[derive(Default)]
struct Plan {
    records: SmallVec<[(Arc<str>, BodyRecord); 2]>,
    relationships: SmallVec<[PlannedRelationship; 2]>,
    items: SmallVec<[PlannedItem; 4]>,
}

struct Models {
    affect: Affect,
    trust: TrustPolicy,
}

/// Python's `sorted({first, second})`.
fn sorted_pair(first: &Arc<str>, second: &Arc<str>) -> SmallVec<[Arc<str>; 2]> {
    let mut actors = SmallVec::new();
    if first == second {
        actors.push(first.clone());
    } else if first.as_bytes() < second.as_bytes() {
        actors.push(first.clone());
        actors.push(second.clone());
    } else {
        actors.push(second.clone());
        actors.push(first.clone());
    }
    actors
}

fn pair_key(first: &str, second: &str) -> Option<Arc<str>> {
    // `ordered_pair_key`, for IDs that JSON writes as they are; the rest are Python's.
    let plain = |id: &str| id.bytes().all(|byte| byte.is_ascii_graphic() && byte != b'"' && byte != b'\\');
    (plain(first) && plain(second)).then(|| Arc::from(format!("[\"{first}\",\"{second}\"]")))
}

fn config_float(config: &V, name: &str) -> PyResult<f64> {
    if let V::Map(map) = config {
        if let Some(V::Float(value)) = map.get(name) {
            return Ok(*value);
        }
    }
    unsupported("a body config value is not a float")
}

/// `_recorded_contact_distance`.
fn contact_distance(positions: Option<&IdMap<Position>>, first: &str, second: &str) -> Option<f64> {
    let positions = positions?;
    let first = positions.get(first)?.point.as_ref()?;
    let second = positions.get(second)?.point.as_ref()?;
    Some(hypot(&[second.x - first.x, second.y - first.y]))
}

/// The rule computed from the rows. Reads and changes nothing but its own plan.
struct Planner<'a> {
    row: &'a BodyRow,
    positions: Option<&'a IdMap<Position>>,
    models: &'a Models,
    event: &'a EventFacts,
    plan: Plan,
    relationship_count: usize,
}

impl Planner<'_> {
    fn record(&self, id: &Arc<str>) -> PyResult<BodyRecord> {
        if let Some((_, record)) = self.plan.records.iter().find(|(known, _)| known == id) {
            return Ok(*record);
        }
        match self.row.agents.get(&**id) {
            Some(record) => Ok(*record),
            None => unsupported("a person has no body record"),
        }
    }

    fn store(&mut self, id: &Arc<str>, record: BodyRecord) {
        match self.plan.records.iter_mut().find(|(known, _)| known == id) {
            Some(slot) => slot.1 = record,
            None => self.plan.records.push((id.clone(), record)),
        }
    }

    /// `_relationship_trust`.
    fn relationship_trust(&self, recipient: &str, sender: &str) -> PyResult<f64> {
        let Some(key) = pair_key(recipient, sender) else {
            return unsupported("an ID is not plain");
        };
        let known = self.plan.relationships.iter().find(|planned| planned.key == key).map(|planned| &planned.record);
        let record = known.or_else(|| self.row.relationships.map.get(&*key));
        Ok(record.and_then(|record| record.trust).unwrap_or(0.5))
    }

    /// `_record_relationship`, with an event ID, which every event has.
    #[allow(clippy::too_many_arguments)]
    fn record_relationship(
        &mut self,
        recipient: &Arc<str>,
        sender: &Arc<str>,
        minute: i64,
        kind: &Arc<str>,
        topic: Option<&Arc<str>>,
        duration: f64,
        distance_m: Option<f64>,
    ) -> PyResult<()> {
        let Some(key) = pair_key(recipient, sender) else {
            return unsupported("an ID is not plain");
        };
        let place = self.plan.relationships.iter().position(|planned| planned.key == key);
        let (mut record, new, mut new_topics) = match place {
            Some(place) => {
                let planned = &self.plan.relationships[place];
                (planned.record.clone(), planned.new, planned.new_topics)
            }
            None => match self.row.relationships.map.get(&*key) {
                Some(record) => (record.clone(), false, false),
                None => {
                    if self.relationship_count >= MAX_SOCIAL_RELATIONSHIPS {
                        return Ok(());
                    }
                    self.relationship_count += 1;
                    (new_relationship(), true, false)
                }
            },
        };
        let elapsed = match record.last_contact_minute {
            None => 0,
            Some(previous) => minute.checked_sub(previous).map_or_else(
                || unsupported("a minute does not fit"),
                |elapsed| Ok(elapsed.max(0)),
            )?,
        };
        let config = &self.row.config;
        let gain = config_float(config, "relationship_trust_gain")?;
        let decay = config_float(config, "relationship_trust_decay_per_day")?;
        let scale = match &config {
            V::Map(map) => match map.get("social_interaction_minutes") {
                Some(V::Int(minutes)) => (*minutes).max(1),
                None => 5,
                _ => return unsupported("the interaction minutes are not an int"),
            },
            _ => return unsupported("the body config is not a map"),
        };
        let Some(before_trust) = record.trust else {
            return unsupported("a relationship has no trust");
        };
        // The bound keeps int(math.ceil(duration)) in the row's int.
        if duration > LARGEST_DURATION {
            return unsupported("a duration is too long");
        }
        let trust = self.models.trust.update_trust(
            before_trust,
            duration,
            scale,
            record.contact_count,
            elapsed,
            gain,
            decay,
            distance_m,
        );
        let Some(contact_count) = record.contact_count.checked_add(1) else {
            return unsupported("a count does not fit");
        };
        let Some(contact_minutes) = record.contact_minutes.checked_add(duration.ceil() as i64) else {
            return unsupported("a count does not fit");
        };
        record.trust = Some(trust);
        record.contact_count = contact_count;
        record.contact_minutes = contact_minutes;
        record.last_contact_minute = Some(minute);
        record.last_interaction = Some(kind.clone());
        if let Some(topic) = topic {
            if !record.topic_ids.contains(topic) {
                record.topic_ids.push(topic.clone());
                if record.topic_ids.len() > 32 {
                    let extra = record.topic_ids.len() - 32;
                    record.topic_ids.drain(..extra);
                }
                new_topics = true;
            }
        }
        let (policy_id, source_id) = self.models.trust.identity();
        let mut deltas: SmallVec<[(&'static str, DeltaValue); 9]> = SmallVec::new();
        deltas.push(("relationship_trust_before", DeltaValue::Number(before_trust)));
        deltas.push(("relationship_trust_after", DeltaValue::Number(trust)));
        deltas.push(("relationship_trust_delta", DeltaValue::Number(trust - before_trust)));
        deltas.push(("contact_duration_minutes", DeltaValue::Number(duration)));
        deltas.push(("relationship_policy_id", DeltaValue::Name(policy_id)));
        deltas.push(("relationship_policy_version", DeltaValue::Name(RELATIONSHIP_POLICY_VERSION)));
        deltas.push(("relationship_policy_source_id", DeltaValue::Name(source_id)));
        if let Some(distance) = distance_m {
            if distance.is_finite() && distance >= 0.0 {
                deltas.push(("contact_distance_m", DeltaValue::Number(distance)));
            }
        }
        self.plan.items.push(PlannedItem {
            minute,
            kind: Arc::from("relationship_update"),
            actors: sorted_pair(recipient, sender),
            topic: topic.cloned(),
            deltas,
        });
        let planned = PlannedRelationship { key, record, new, new_topics };
        match place {
            Some(place) => self.plan.relationships[place] = planned,
            None => self.plan.relationships.push(planned),
        }
        Ok(())
    }

    /// One item of `transfers`, from `_body_deltas` and the loop at the end of the rule. The two
    /// lists of the Python rule keep their order: an item here is added after the relationships'.
    fn transfer_deltas(before: &BodyRecord, after: &BodyRecord) -> SmallVec<[(&'static str, DeltaValue); 9]> {
        let mut deltas: SmallVec<[(&'static str, DeltaValue); 9]> = SmallVec::new();
        // The fields in sorted order, as `sorted(set(before) & set(after))` has them.
        for (name, first, second) in [
            ("discontent", before.discontent, after.discontent),
            ("hygiene_need", before.hygiene_need, after.hygiene_need),
            ("negative_affect", before.negative_affect, after.negative_affect),
            ("positive_affect", before.positive_affect, after.positive_affect),
            ("reassurance", before.reassurance, after.reassurance),
            ("sleep_debt_minutes", before.sleep_debt_minutes, after.sleep_debt_minutes),
            ("uncertainty", before.uncertainty, after.uncertainty),
        ] {
            if second != first {
                deltas.push((name, DeltaValue::Number(second - first)));
            }
        }
        deltas
    }

    fn apply_contribution(record: &mut BodyRecord, contribution: &Contribution, include_uncertainty: bool) {
        record.positive_affect = unit(record.positive_affect + contribution.positive_affect);
        record.negative_affect = unit(record.negative_affect + contribution.negative_affect);
        record.reassurance = unit(record.reassurance + contribution.reassurance);
        record.discontent = unit(record.discontent + contribution.discontent);
        if include_uncertainty {
            record.uncertainty = unit(record.uncertainty + contribution.uncertainty);
        }
    }

    fn run(&mut self, action: &Action) -> PyResult<()> {
        let mut transfers: SmallVec<[(Arc<str>, Arc<str>, Arc<str>, SmallVec<[(&'static str, DeltaValue); 9]>); 2]> =
            SmallVec::new();
        match action {
            Action::Nothing => {}
            Action::Proximity { first, second, observed_minute, distance, duration } => {
                let kind: Arc<str> = Arc::from("observed_proximity_contact");
                self.record_relationship(first, second, *observed_minute, &kind, None, *duration, *distance)?;
                self.record_relationship(second, first, *observed_minute, &kind, None, *duration, *distance)?;
            }
            Action::Contact { sender, recipient, topic, duration } => {
                let source = self.record(sender)?;
                let mut target = self.record(recipient)?;
                let before = target;
                let rate = config_float(&self.row.config, "affect_transfer_rate")?;
                let trust = self.relationship_trust(recipient, sender)?;
                let contribution = self.models.affect.contact(&source, &target, rate, trust);
                Self::apply_contribution(&mut target, &contribution, true);
                self.store(recipient, target);
                let kind: Arc<str> = Arc::from("information_contact");
                transfers.push((recipient.clone(), sender.clone(), kind.clone(), Self::transfer_deltas(&before, &target)));
                let distance = contact_distance(self.positions, recipient, sender);
                self.record_relationship(recipient, sender, self.event.minute, &kind, topic.as_ref(), *duration, distance)?;
            }
            Action::Social { kind, actor, participant, topic, duration } => match kind {
                Interaction::None => {}
                Interaction::Cooperation => {
                    let Some(participant) = participant else {
                        return Ok(());
                    };
                    let name: Arc<str> = Arc::from("cooperation");
                    for (agent_id, other_id) in [(actor, participant), (participant, actor)] {
                        let before = self.record(agent_id)?;
                        let mut record = before;
                        let trust = self.relationship_trust(agent_id, other_id)?;
                        let trust_scale = self.models.affect.social_action(1.0, 1.0, trust).positive_affect;
                        let config = &self.row.config;
                        record.positive_affect =
                            unit(record.positive_affect + config_float(config, "cooperation_positive_gain")? * trust_scale);
                        record.negative_affect = unit(
                            record.negative_affect - config_float(config, "cooperation_negative_reduction")? * trust_scale,
                        );
                        record.reassurance =
                            unit(record.reassurance + config_float(config, "cooperation_reassurance_gain")? * trust_scale);
                        record.discontent = unit(
                            record.discontent - config_float(config, "cooperation_discontent_reduction")? * trust_scale,
                        );
                        self.store(agent_id, record);
                        transfers.push((actor.clone(), agent_id.clone(), name.clone(), Self::transfer_deltas(&before, &record)));
                    }
                    let distance = contact_distance(self.positions, actor, participant);
                    self.record_relationship(actor, participant, self.event.minute, &name, topic.as_ref(), *duration, distance)?;
                    let distance = contact_distance(self.positions, participant, actor);
                    self.record_relationship(participant, actor, self.event.minute, &name, topic.as_ref(), *duration, distance)?;
                }
                Interaction::Transfer { kind, include_uncertainty } => {
                    let Some(participant) = participant else {
                        return Ok(());
                    };
                    let actor_record = self.record(actor)?;
                    let mut target = self.record(participant)?;
                    let before = target;
                    let mut signal = unit(actor_record.positive_affect - actor_record.negative_affect);
                    if actor_record.positive_affect < actor_record.negative_affect {
                        signal = -unit(actor_record.negative_affect - actor_record.positive_affect);
                    }
                    let rate = config_float(&self.row.config, "social_action_affect_transfer")?;
                    let trust = self.relationship_trust(participant, actor)?;
                    let contribution = self.models.affect.social_action(signal, rate, trust);
                    Self::apply_contribution(&mut target, &contribution, *include_uncertainty);
                    self.store(participant, target);
                    transfers.push((actor.clone(), participant.clone(), kind.clone(), Self::transfer_deltas(&before, &target)));
                    let distance = contact_distance(self.positions, participant, actor);
                    self.record_relationship(participant, actor, self.event.minute, kind, topic.as_ref(), *duration, distance)?;
                }
                Interaction::ComplaintResponse => {
                    let before = self.record(actor)?;
                    let mut record = before;
                    let config = &self.row.config;
                    record.reassurance = unit(record.reassurance + config_float(config, "complaint_reassurance_gain")?);
                    record.discontent = unit(record.discontent - config_float(config, "complaint_discontent_reduction")?);
                    record.negative_affect = unit(record.negative_affect - 0.01);
                    self.store(actor, record);
                    transfers.push((actor.clone(), actor.clone(), Arc::from("complaint_response"), Self::transfer_deltas(&before, &record)));
                }
            },
            Action::Correction { actors } => {
                let name: Arc<str> = Arc::from("information_correction");
                for actor in actors {
                    let before = self.record(actor)?;
                    let mut record = before;
                    let config = &self.row.config;
                    record.uncertainty = unit(record.uncertainty - config_float(config, "correction_uncertainty_reduction")?);
                    record.reassurance = unit(record.reassurance + config_float(config, "correction_reassurance_gain")?);
                    record.negative_affect = unit(record.negative_affect - 0.01);
                    self.store(actor, record);
                    transfers.push((actor.clone(), actor.clone(), name.clone(), Self::transfer_deltas(&before, &record)));
                }
            }
        }
        for (actor, other, kind, mut deltas) in transfers {
            if actor != other {
                if let Some(distance) = contact_distance(self.positions, &actor, &other) {
                    deltas.push(("contact_distance_m", DeltaValue::Number(distance)));
                }
            }
            if let Some(duration) = self.event.transfer_duration {
                deltas.push(("contact_duration_minutes", DeltaValue::Number(duration)));
            }
            self.plan.items.push(PlannedItem {
                minute: self.event.minute,
                kind,
                actors: sorted_pair(&actor, &other),
                topic: self.event.transfer_topic.clone(),
                deltas,
            });
        }
        Ok(())
    }
}

// ---- Reading the world ----------------------------------------------------------------------------

fn selected_models<'py>(py: Python<'py>, configs: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    static POLICIES: GILOnceCell<Py<PyModuleType>> = GILOnceCell::new();
    let policies = POLICIES
        .get_or_try_init(py, || Ok::<_, PyErr>(py.import("sheltersim.contracts.policies")?.unbind()))?
        .bind(py);
    // The rule's own selection, which raises for what it refuses. That is Python's to say.
    let selection = policies
        .getattr(intern!(py, "selected_models"))?
        .call1((configs,))
        .map_err(|_| crate::Unsupported::new_err("the model selection raises"))?;
    Ok(selection)
}

/// `create_affect` without a selection: the config's, by the function that also refuses a bad one.
fn affect_of_the_config<'py>(configs: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = configs.py();
    let raw = exact_dict(configs)?.get_item(intern!(py, "body_dynamics"))?;
    let config = py
        .import(intern!(py, "sheltersim.domain.body_state"))?
        .getattr(intern!(py, "canonical_body_config"))?
        .call1((raw,))
        .map_err(|_| crate::Unsupported::new_err("the body config raises"))?;
    Ok(config.get_item(intern!(py, "affect_policy"))?)
}

/// The operators the rule creates. An ID this kernel doesn't know is left to Python's factory.
fn models_of<'py>(selection: &Bound<'py, PyAny>, configs: &Bound<'py, PyAny>) -> PyResult<Models> {
    let py = selection.py();
    let selection = exact_dict(selection)?;
    let affect = match selection.get_item(intern!(py, "affect"))? {
        Some(affect) => affect,
        None => affect_of_the_config(configs)?,
    };
    let affect = match exact_str(&affect)? {
        LINEAR_AFFECT => Affect::Linear,
        TRUST_WEIGHTED_AFFECT => Affect::TrustWeighted,
        _ => return unsupported("an affect model this kernel doesn't know"),
    };
    let Some(relationship) = selection.get_item(intern!(py, "relationship"))? else {
        // `create_relationship_policy` without a selection.
        return Ok(Models { affect, trust: TrustPolicy::DurationOnly });
    };
    let trust = match exact_str(&relationship)? {
        DURATION_TRUST => TrustPolicy::DurationOnly,
        DISTANCE_TRUST => {
            let configs = exact_dict(configs)?;
            let body = configs.get_item(intern!(py, "body_dynamics"))?;
            let number = |name: &Bound<'py, PyString>, default: f64| -> PyResult<f64> {
                let Some(body) = &body else {
                    return Ok(default);
                };
                match exact_dict(body)?.get_item(name)? {
                    None => Ok(default),
                    Some(value) => match Item::read(Some(value))? {
                        Item::Float(number) => Ok(number),
                        Item::Int(number) => Ok(number as f64),
                        _ => unsupported("a relationship parameter is not a number"),
                    },
                }
            };
            let distance_scale_m = number(intern!(py, "relationship_distance_scale_m"), 2.0)?;
            // Zero divides, which raises in Python. Its validation keeps it within 0.1..100.
            if distance_scale_m <= 0.0 {
                return unsupported("the distance scale is not positive");
            }
            TrustPolicy::DistanceDurationRepeat {
                distance_scale_m,
                repeat_contact_bonus: number(intern!(py, "relationship_repeat_contact_bonus"), 0.05)?,
            }
        }
        _ => return unsupported("a relationship model this kernel doesn't know"),
    };
    Ok(Models { affect, trust })
}

/// `information_correction`'s people who took the correction: those in the zone, in ID order, and
/// with the evidence-aware model only those whose belief says so.
fn correction_actors<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    payload: &Bound<'py, PyAny>,
    selection: &Bound<'py, PyAny>,
) -> PyResult<Vec<Arc<str>>> {
    let py = world.py();
    let zone = read(payload, intern!(py, "zone_id"))?;
    let mut actors: Vec<Arc<str>> = match &zone {
        Item::Text(zone) => EntityStore::with_rows(&entities_of(world)?, |rows| {
            Ok(rows
                .agents
                .iter()
                .filter(|(_, agent)| agent.zone_id == *zone)
                .map(|(id, _)| id.clone())
                .collect())
        })?,
        Item::Nothing => Vec::new(),
        _ => return unsupported("a zone is not a str"),
    };
    actors.sort_unstable_by(|first, second| first.as_bytes().cmp(second.as_bytes()));
    let selection = exact_dict(selection)?;
    let Some(information) = selection.get_item(intern!(py, "information"))? else {
        return unsupported("no information model is selected");
    };
    if exact_str(&information)? != EVIDENCE_AWARE_INFORMATION {
        return Ok(actors);
    }
    let table = attribute(world, intern!(py, "model_state"))?;
    let message = read(payload, intern!(py, "message_id"))?;
    let event_id = attribute(event, intern!(py, "event_id"))?;
    let event_id = exact_str(&event_id)?;
    let beliefs = exact_dict(&table)?
        .get_item(intern!(py, "information_spread_v1"))?
        .map(|state| -> PyResult<Option<Bound<'py, PyAny>>> {
            exact_dict(&state)?.get_item(intern!(py, "beliefs")).map_err(Into::into)
        })
        .transpose()?
        .flatten();
    let Item::Text(message) = message else {
        return match message {
            Item::Nothing => Ok(Vec::new()),
            _ => unsupported("a message is not a str"),
        };
    };
    let message = PyString::new(py, &message);
    let mut accepted = Vec::new();
    for actor in actors {
        let Some(beliefs) = &beliefs else {
            continue;
        };
        let Some(held) = exact_dict(beliefs)?.get_item(&*actor)? else {
            continue;
        };
        let Some(raw) = exact_dict(&held)?.get_item(&message)? else {
            continue;
        };
        if raw.is_none() {
            continue;
        }
        let raw = exact_dict(&raw)?;
        let Some(last_event) = raw.get_item(intern!(py, "last_event_id"))? else {
            continue;
        };
        if last_event.is_none() || exact_str(&last_event)? != event_id {
            continue;
        }
        let Some(took_it) = raw.get_item(intern!(py, "last_correction_accepted"))? else {
            continue;
        };
        let Ok(took_it) = took_it.downcast_exact::<PyBool>() else {
            return unsupported("a correction flag is not a bool");
        };
        if took_it.is_true() {
            accepted.push(actor);
        }
    }
    Ok(accepted)
}

/// What the event asks of the rule, or Unsupported for what is Python's to decide.
fn action_of<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    event_type: &str,
    payload: &Bound<'py, PyAny>,
    selection: &Bound<'py, PyAny>,
    minute: i64,
) -> PyResult<(Action, EventFacts)> {
    let py = world.py();
    let contact_minutes = read(payload, intern!(py, "contact_minutes"))?;
    let (message, topic_id) = (read(payload, intern!(py, "message_id"))?, read(payload, intern!(py, "topic_id"))?);
    let transfer_topic = message.text().or(topic_id.text()).cloned();
    let interaction_duration = intern!(py, "interaction_duration_minutes");
    let transfer_duration = if holds(payload, interaction_duration)? {
        read(payload, interaction_duration)?.transfer_duration()
    } else {
        contact_minutes.transfer_duration()
    };
    let facts = EventFacts { minute, transfer_topic, transfer_duration };
    let people_are_known = |first: &Arc<str>, second: &Arc<str>| -> PyResult<bool> {
        EntityStore::with_rows(&entities_of(world)?, |rows| {
            Ok(rows.agents.get(first).is_some() && rows.agents.get(second).is_some())
        })
    };
    let action = match event_type {
        "proximity_contact_update" => {
            let first = read(payload, intern!(py, "first_agent_id"))?.id()?;
            let second = read(payload, intern!(py, "second_agent_id"))?.id()?;
            let observed = read(payload, intern!(py, "observed_minute"))?;
            let distance = match read(payload, intern!(py, "distance_m"))? {
                Item::Nothing => None,
                Item::Float(distance) => Some(distance),
                _ => return unsupported("a distance is not a float"),
            };
            match (first, second, observed) {
                (Some(first), Some(second), Item::Int(observed_minute))
                    if first != second && people_are_known(&first, &second)? =>
                {
                    Action::Proximity {
                        first,
                        second,
                        observed_minute,
                        distance,
                        duration: contact_minutes.relationship_duration(),
                    }
                }
                _ => Action::Nothing,
            }
        }
        "information_contact_update" => {
            let sender = read(payload, intern!(py, "sender_agent_id"))?.id()?;
            let recipient = read(payload, intern!(py, "recipient_agent_id"))?.id()?;
            match (sender, recipient) {
                (Some(sender), Some(recipient)) if sender != recipient && people_are_known(&sender, &recipient)? => {
                    Action::Contact {
                        sender,
                        recipient,
                        topic: message.text().cloned(),
                        // `payload.get("contact_minutes", 1)`: both an absent key and None give 1.0.
                        duration: contact_minutes.relationship_duration(),
                    }
                }
                _ => Action::Nothing,
            }
        }
        "social_interaction" => {
            let interaction = read(payload, intern!(py, "interaction_type"))?;
            let actor = read(payload, intern!(py, "agent_id"))?.id()?;
            let participant = read(payload, intern!(py, "participant_agent_id"))?.id()?;
            let Some(actor) = actor else {
                return Ok((Action::Nothing, facts));
            };
            let kind = match interaction.text().map(|text| &**text) {
                Some("cooperation") => Interaction::Cooperation,
                Some(name @ ("reassurance_exchange" | "family_care" | "voluntary_support")) => Interaction::Transfer {
                    kind: Arc::from(name),
                    include_uncertainty: name == "reassurance_exchange",
                },
                Some("complaint_response") => Interaction::ComplaintResponse,
                _ => Interaction::None,
            };
            if matches!(kind, Interaction::Cooperation | Interaction::Transfer { .. }) && participant.as_ref() == Some(&actor) {
                return unsupported("a person interacts with themselves");
            }
            let known = match (&kind, &participant) {
                (Interaction::ComplaintResponse, _) => people_are_known(&actor, &actor)?,
                (Interaction::None, _) | (_, None) => false,
                (_, Some(participant)) => people_are_known(&actor, participant)?,
            };
            if !known {
                Action::Nothing
            } else {
                let duration = read(payload, interaction_duration)?;
                Action::Social {
                    kind,
                    actor,
                    participant,
                    topic: topic_id.text().cloned(),
                    // `payload.get("interaction_duration_minutes", 1)`: None and absent give 1.0.
                    duration: duration.relationship_duration(),
                }
            }
        }
        "information_correction" => Action::Correction { actors: correction_actors(world, event, payload, selection)? },
        _ => Action::Nothing,
    };
    Ok((action, facts))
}

/// The item's document, in the order of the Python rule's keys.
fn item_document<'py>(
    py: Python<'py>,
    item: &PlannedItem,
    event_id: &Bound<'py, PyAny>,
    causation_id: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let document = PyDict::new(py);
    document.set_item(intern!(py, "event_id"), event_id)?;
    document.set_item(intern!(py, "minute"), item.minute)?;
    document.set_item(intern!(py, "kind"), &*item.kind)?;
    let actors = PyList::empty(py);
    for actor in &item.actors {
        actors.append(&**actor)?;
    }
    document.set_item(intern!(py, "actor_ids"), actors)?;
    document.set_item(intern!(py, "topic_id"), Field::to_py(&item.topic, py)?)?;
    document.set_item(intern!(py, "causation_id"), causation_id)?;
    let deltas = PyDict::new(py);
    for (name, value) in &item.deltas {
        match value {
            DeltaValue::Number(number) => deltas.set_item(*name, *number)?,
            DeltaValue::Name(text) => deltas.set_item(*name, *text)?,
        }
    }
    document.set_item(intern!(py, "deltas"), deltas)?;
    Ok(document)
}

// ---- The kernel -----------------------------------------------------------------------------------

/// `record_social_event`.
#[pyfunction]
pub(crate) fn record_social_event<'py>(world: &Bound<'py, PyAny>, event: &Bound<'py, PyAny>, minute: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(());
    }
    let event_type = attribute(event, intern!(py, "event_type"))?;
    let event_type = exact_str(&event_type)?;
    if !matches!(
        event_type,
        "proximity_contact_update" | "information_contact_update" | "social_interaction" | "information_correction"
    ) {
        return Ok(());
    }
    let minute = exact_int(minute)?;
    let payload = payload_of(event)?;
    let event_id = attribute(event, intern!(py, "event_id"))?;
    exact_str(&event_id)?;
    let causation_id = attribute(event, intern!(py, "causation_id"))?;
    if !causation_id.is_none() {
        exact_str(&causation_id)?;
    }
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let selection = selected_models(py, &configs)?;
    let models = models_of(&selection, &configs)?;
    let (action, facts) = action_of(world, event, event_type, &payload, &selection, minute)?;
    if matches!(action, Action::Nothing) {
        return Ok(());
    }

    // No Python runs from here until the plan is done, so the world stays the one read here.
    let mirror = mirror_of(world, NAME)?;
    let movement = if namespace_is_absent(world, intern!(py, MOVEMENT))? {
        None
    } else {
        Some(mirror_of(world, MOVEMENT)?)
    };
    let plan = {
        let compute = |positions: Option<&IdMap<Position>>| {
            NamespaceMirror::with_row::<BodyRow, _>(&mirror, |row| {
                let mut planner = Planner {
                    relationship_count: row.relationships.map.len(),
                    row,
                    positions,
                    models: &models,
                    event: &facts,
                    plan: Plan::default(),
                };
                planner.run(&action)?;
                Ok(planner.plan)
            })
        };
        match &movement {
            Some(movement) => NamespaceMirror::with_row::<MovementRow, _>(movement, |row| compute(Some(&row.positions)))?,
            None => compute(None)?,
        }
    };

    // A value canonical JSON refuses is refused here, before any change.
    let mut items = Vec::with_capacity(plan.items.len());
    for item in &plan.items {
        let document = item_document(py, item, &event_id, &causation_id)?;
        let bytes = encode(document.as_any())?.into_boxed_slice();
        items.push((document, bytes));
    }

    // From here on nothing may be refused.
    let dropped = NamespaceMirror::with_row_tree::<BodyRow, _>(&mirror, |row, tree| {
        let records = tree.child(intern!(py, "agents"))?;
        for (id, next) in &plan.records {
            let record = row
                .agents
                .get_mut(&**id)
                .ok_or_else(|| PyRuntimeError::new_err("a planned record left the row"))?;
            record.set_all(&records.child_str(id)?, next)?;
        }
        let relationships = tree.child(intern!(py, "relationships"))?;
        for planned in &plan.relationships {
            row.relationships.written.push(planned.key.clone());
            if planned.new {
                row.relationships.map.insert(planned.key.clone(), planned.record.clone());
            } else {
                let record = row
                    .relationships
                    .map
                    .get_mut(&*planned.key)
                    .ok_or_else(|| PyRuntimeError::new_err("a planned relationship left the row"))?;
                record.set_all(&relationships.child_str(&planned.key)?, &planned.record)?;
                if planned.new_topics {
                    record.topic_ids = planned.record.topic_ids.clone();
                }
            }
        }
        for (_, bytes) in &items {
            row.history.items.push_back(bytes.clone());
        }
        let dropped = row.history.items.len().saturating_sub(MAX_SOCIAL_HISTORY);
        row.history.items.drain(..dropped);
        Ok(dropped)
    })?;
    for planned in &plan.relationships {
        if planned.new {
            NamespaceMirror::write_through(&mirror, &["relationships"], &planned.key, &Field::to_py(&planned.record, py)?)?;
        } else if planned.new_topics {
            NamespaceMirror::write_through(
                &mirror,
                &["relationships", &planned.key],
                "topic_ids",
                &Field::to_py(&planned.record.topic_ids, py)?,
            )?;
        }
    }
    for (document, _) in &items {
        NamespaceMirror::append_through(&mirror, &["history"], document.as_any())?;
    }
    if dropped > 0 {
        NamespaceMirror::drop_oldest_through(&mirror, &["history"], dropped)?;
    }
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(record_social_event, m)?)?;
    Ok(())
}
