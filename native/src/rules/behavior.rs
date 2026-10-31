//! `BehaviorDecisionRunner.propose_due` with `UtilityBehaviorPolicy` and
//! `PriorityRuleBehaviorPolicy` (`policies/behavior.py`), reading the snapshot of
//! `rules/world_view.rs`, and `_agent_view_snapshot` (`experiments/phases/propose.py`).
//!
//! `propose_due` and `observed_state` replace the steps `_propose_due` and `_observed_state` of the
//! propose phase (experiments/phases/segments.py). They change nothing and the draws keep no state,
//! so saying `Unsupported` at any point is safe: the Python step then runs and raises its own text.
//! That covers a view Python made, a policy, runner or RNG of another class (subclasses too), and any
//! value on which the Python rule would raise, which `check_candidate` and `check_proposal` repeat
//! from `ActionCandidate` and `ActionProposal`. The proposals are the Python classes, without their
//! generated `__init__`. Nothing here writes a trace: what a run records of a decision is the proposal.
//!
//! The coefficients are the ones of `UtilityBehaviorPolicy.propose`: `score` and `candidates_of` have
//! them with the same order of operations, and the policy's own fields are read from it at each call.
//! `compute` reads no Python, so it can run per person at step 4.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple, PyType};
use pyo3::IntoPyObjectExt;

use crate::core::mathx::{exp, py_fsum, py_is_close, py_max, py_round_int, py_sum};
use crate::core::rng::{Key, KeyedDraws};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::{raw_dict_get, raw_dict_set};
use crate::rules::world_view::{DemandModifier, RustView, ViewAgent, ViewData};
use crate::state::entities::{raw_setattr, Activity, RequestStatus};
use crate::Unsupported;

fn unsupported<T>(reason: &'static str) -> PyResult<T> {
    Err(Unsupported::new_err(reason))
}

// ---- The actions -----------------------------------------------------------------------------------

/// `ActionKind`; the discriminant is the place in `ACTION_VALUES` and in `Classes::actions`.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Action {
    Wait,
    RequestToilet,
    Rest,
    Sleep,
    SeekHelp,
    FetchWater,
    CollectMeal,
    SeekInformation,
    Socialize,
    CareFamily,
    HelpOthers,
}

const ACTION_VALUES: [&str; 11] = [
    "wait",
    "request_toilet",
    "rest",
    "sleep",
    "seek_help",
    "fetch_water",
    "collect_meal",
    "seek_information",
    "socialize",
    "care_family",
    "help_others",
];

impl Action {
    fn value(self) -> &'static str {
        ACTION_VALUES[self as usize]
    }
}

// ---- What the rule reads ---------------------------------------------------------------------------

/// The fields of `UtilityBehaviorPolicy` the scoring reads. An int of a field counts as its float, as
/// it does in each operation with a float here.
struct Settings<'a> {
    /// The scorer's `policy_version`, in the key of the draw.
    version: &'a str,
    sleep_aware: bool,
    sleep_debt_weight: f64,
    resource_service: bool,
    autonomy: bool,
    hydration_threshold: f64,
    hunger_threshold: f64,
    water_base: f64,
    water_weight: f64,
    meal_base: f64,
    meal_weight: f64,
    query_threshold: f64,
    query_weight: f64,
    priority_rule: bool,
}

impl Settings<'_> {
    fn query_enabled(&self) -> bool {
        self.query_threshold <= 1.0 || self.query_weight > 0.0
    }
}

/// What every person's scoring shares in one call.
struct Frozen<'a> {
    data: &'a ViewData,
    settings: &'a Settings<'a>,
    draws: &'a KeyedDraws,
    /// The people of each zone of `data.zones`, in ID order.
    by_zone: Vec<Vec<usize>>,
    /// `view.group_members`, sorted by group.
    groups: Vec<(&'a Arc<str>, Vec<usize>)>,
    /// `view.immediate_needs()`, by person.
    pressure: Vec<f64>,
}

impl<'a> Frozen<'a> {
    /// A NaN would make `max` over tuples depend on identity, so it is left to Python.
    fn new(data: &'a ViewData, settings: &'a Settings<'a>, draws: &'a KeyedDraws) -> Result<Self, &'static str> {
        let mut by_zone: Vec<Vec<usize>> = vec![Vec::new(); data.zones.len()];
        let mut pressure = Vec::with_capacity(data.agents.len());
        for (index, agent) in data.agents.iter().enumerate() {
            by_zone[agent.zone].push(index);
            let needs = &agent.needs;
            let mut value = agent.stress;
            for need in [needs.toilet_urgency, needs.hunger, needs.fatigue, needs.hydration_need] {
                value = py_max(value, need);
            }
            for number in [agent.stress, needs.toilet_urgency, needs.hunger, needs.fatigue, needs.hydration_need] {
                if number.is_nan() {
                    return Err("a person's pressure is NaN");
                }
            }
            pressure.push(value);
        }
        Ok(Self { data, settings, draws, by_zone, groups: data.group_members(), pressure })
    }

    /// `view.group_members.get(agent.group_id or "", ())`.
    fn group_of(&self, agent: &ViewAgent) -> &[usize] {
        let key = agent.group_id.as_deref().unwrap_or("");
        match self.groups.binary_search_by(|(group, _)| (***group).cmp(key)) {
            Ok(place) => &self.groups[place].1,
            Err(_) => &[],
        }
    }

    /// `max(peers, key=lambda peer: (pressure[peer], peer), default=None)` of peers in ID order: with
    /// no NaN, a later peer with an equal pressure has the larger tuple.
    fn most_pressed(&self, peers: impl Iterator<Item = usize>) -> Option<usize> {
        let mut best = None;
        for peer in peers {
            if best.is_none_or(|kept: usize| self.pressure[peer] >= self.pressure[kept]) {
                best = Some(peer);
            }
        }
        best
    }
}

/// The last modifier of an action wins, as in `{item.action: item for item in ...}`.
fn demand_of<'a>(modifiers: &'a [DemandModifier], action: Action) -> Option<&'a DemandModifier> {
    modifiers.iter().rev().find(|modifier| &*modifier.action == action.value())
}

/// One candidate of `candidates` in `propose`, before its probability.
struct Candidate {
    action: Action,
    eligible: bool,
    utility: f64,
    priority: i64,
}

/// A candidate with what `ActionCandidate` holds. `utility` includes the priority's half.
#[derive(Clone, Copy)]
struct Scored {
    action: Action,
    eligible: bool,
    utility: f64,
    priority: i64,
    probability: f64,
}

/// What a proposal says.
struct Outcome {
    candidates: Vec<Scored>,
    choice_value: f64,
    tie_broken: bool,
    selection_probability: f64,
    action: Action,
    next_decision_at: i64,
    target: Option<usize>,
}

/// `ActionCandidate.__post_init__`, on what the scorer can make.
fn check_candidate(candidate: &Scored) -> Result<(), &'static str> {
    if !candidate.utility.is_finite()
        || !candidate.probability.is_finite()
        || !(0.0..=1.0).contains(&candidate.probability)
        || (!candidate.eligible && candidate.probability != 0.0)
    {
        return Err("a candidate Python turns down");
    }
    Ok(())
}

/// `ActionProposal.__post_init__`, on what the scorer can make.
fn check_proposal(
    minute: i64,
    next_decision_at: i64,
    choice_value: f64,
    selection_probability: f64,
    candidates: &[Scored],
) -> Result<(), &'static str> {
    if minute < 0 || next_decision_at <= minute {
        return Err("a proposal has invalid decision timing");
    }
    if !choice_value.is_finite() || !(0.0..1.0).contains(&choice_value) {
        return Err("a choice value Python turns down");
    }
    if !selection_probability.is_finite() || selection_probability <= 0.0 || selection_probability > 1.0 {
        return Err("a selected probability Python turns down");
    }
    let eligible: Vec<f64> = candidates.iter().filter(|item| item.eligible).map(|item| item.probability).collect();
    if eligible.is_empty() || !py_is_close(py_sum(&eligible), 1.0) {
        return Err("the probabilities do not sum to one");
    }
    Ok(())
}

/// `_decision_interval_minutes`.
fn decision_interval(
    base_minutes: i64,
    action: Action,
    modifier: Option<&DemandModifier>,
    minimum: i64,
    maximum: i64,
) -> Result<i64, &'static str> {
    let Some(modifier) = modifier else {
        return Ok(base_minutes);
    };
    if !matches!(action, Action::Rest | Action::Sleep) || modifier.duration_multiplier == 1.0 {
        return Ok(base_minutes);
    }
    let Some(scaled) = py_round_int(base_minutes as f64 * modifier.duration_multiplier) else {
        return Err("a scaled interval does not round to an i64");
    };
    Ok(scaled.max(minimum).min(maximum))
}

/// `_sleep_debt_utility`.
fn sleep_debt_utility(agent: &ViewAgent, settings: &Settings<'_>) -> f64 {
    if !settings.sleep_aware {
        return agent.body.sleep_debt_minutes / 1440.0 * 0.45;
    }
    let required = match agent.person.required_sleep_minutes {
        None | Some(0) => 480,
        Some(minutes) => minutes,
    };
    settings.sleep_debt_weight * agent.body.sleep_debt_minutes / required as f64
}

/// `_target_for_action`.
fn target_for_action(frozen: &Frozen<'_>, index: usize, action: Action) -> Option<usize> {
    let data = frozen.data;
    let agent = &data.agents[index];
    let zone_peers = || frozen.by_zone[agent.zone].iter().copied().filter(|peer| *peer != index);
    match action {
        Action::Socialize => zone_peers().next(),
        Action::CareFamily => {
            let peers: Vec<usize> = frozen
                .group_of(agent)
                .iter()
                .copied()
                .filter(|peer| *peer != index && data.agents[*peer].zone == agent.zone)
                .collect();
            let explicit: Vec<usize> =
                peers.iter().copied().filter(|peer| is_care_linked(agent, &data.agents[*peer])).collect();
            if !explicit.is_empty() {
                frozen.most_pressed(explicit.into_iter())
            } else if agent.person.personal_context_present
                || peers.iter().any(|peer| data.agents[*peer].person.personal_context_present)
            {
                None
            } else {
                frozen.most_pressed(peers.into_iter())
            }
        }
        Action::HelpOthers => {
            let threshold = frozen.pressure[index] + 0.15;
            frozen.most_pressed(zone_peers().filter(|peer| frozen.pressure[*peer] > threshold))
        }
        _ => None,
    }
}

/// `peer in agent.care_for_ids or agent.id in peer.caregiver_ids or agent.id in peer.parent_ids`.
fn is_care_linked(agent: &ViewAgent, peer: &ViewAgent) -> bool {
    agent.person.care_for_ids.iter().any(|id| *id == peer.id)
        || peer.person.caregiver_ids.iter().any(|id| *id == agent.id)
        || peer.person.parent_ids.iter().any(|id| *id == agent.id)
}

/// The list `candidates` of `propose`, in its order.
fn candidates_of(frozen: &Frozen<'_>, index: usize) -> Vec<Candidate> {
    let data = frozen.data;
    let settings = frozen.settings;
    let agent = &data.agents[index];
    let zone = data.zone_of(agent);
    let needs = &agent.needs;
    let body = &agent.body;
    let person = &agent.person;
    let resource_service = settings.resource_service && data.resource_service_enabled;

    let mut distress = py_max(needs.toilet_urgency, needs.hunger);
    distress = py_max(distress, needs.fatigue);
    if resource_service {
        distress = py_max(distress, needs.hydration_need);
    }
    distress = py_max(distress, agent.stress);
    distress = py_max(distress, agent.chronic_distress);
    distress = py_max(distress, body.discontent * 0.75);
    distress = py_max(distress, body.negative_affect * 0.6);

    let has_open_request = agent.support_request_id.is_some()
        && matches!(agent.support_request_status, None | Some(RequestStatus::Pending) | Some(RequestStatus::Assigned));
    let query_enabled = settings.query_enabled();
    let query_trigger = query_enabled
        && (agent.information_query_pressure >= settings.query_threshold
            || body.uncertainty >= py_max(0.65, settings.query_threshold));
    let cooled_down = agent.support_cooldown_remaining_minutes == 0;
    let can_seek_help = (agent.support_need >= 0.25 && distress >= 0.50 && !has_open_request && cooled_down)
        || (query_trigger && !has_open_request && cooled_down);
    let can_seek_information = query_enabled && query_trigger && !has_open_request && cooled_down;

    let minute_of_day = data.minute_index.rem_euclid(1440);
    let mut sleep_window = false;
    if let (Some(sleep_time), Some(wake_time)) = (person.usual_sleep_minute, person.usual_wake_minute) {
        sleep_window = if sleep_time > wake_time {
            minute_of_day >= sleep_time || minute_of_day < wake_time
        } else {
            sleep_time <= minute_of_day && minute_of_day < wake_time
        };
    }
    let sleep_rhythm_due =
        data.autonomy_enabled && sleep_window && person.required_sleep_minutes.is_some_and(|minutes| minutes > 0);
    let mut meal_rhythm_due = false;
    if data.autonomy_enabled {
        if let Some(wake_time) = person.usual_wake_minute {
            // Wider than i64 for a wake time near its limits. The result fits: it is below 1440.
            let since_wake = (i128::from(minute_of_day) - i128::from(wake_time)).rem_euclid(1440);
            meal_rhythm_due = [120_i128, 420, 660].iter().map(|offset| (since_wake - offset).abs()).min().unwrap_or(0) <= 45;
        }
    }

    let mut candidates = Vec::with_capacity(11);
    candidates.push(Candidate {
        action: Action::Wait,
        eligible: true,
        utility: 0.10 + 0.15 * (1.0 - distress),
        priority: 0,
    });
    candidates.push(Candidate {
        action: Action::RequestToilet,
        eligible: needs.toilet_urgency >= 0.25 || body.hygiene_need >= 0.5,
        utility: 0.20 + 0.90 * needs.toilet_urgency + 0.35 * body.hygiene_need,
        priority: i64::from(needs.toilet_urgency >= 0.80 || body.hygiene_need >= 0.8),
    });
    candidates.push(Candidate {
        action: Action::Rest,
        eligible: needs.fatigue >= 0.25 || needs.hunger >= 0.70 || agent.stress >= 0.50,
        utility: 0.10 + 0.45 * needs.fatigue + 0.20 * needs.hunger + 0.30 * agent.stress,
        priority: 0,
    });
    candidates.push(Candidate {
        action: Action::Sleep,
        eligible: needs.fatigue >= 0.65 || body.sleep_debt_minutes >= 240.0 || sleep_rhythm_due,
        utility: 0.25 + 0.90 * needs.fatigue + sleep_debt_utility(agent, settings)
            + (if sleep_rhythm_due { 0.20 } else { 0.0 })
            - 0.20 * py_max(0.0, zone.lighting_level - 0.3),
        priority: 0,
    });
    candidates.push(Candidate {
        action: Action::SeekHelp,
        eligible: can_seek_help,
        utility: 0.15 + 0.65 * distress + 0.25 * agent.support_need + 0.10 * agent.trust + 0.20 * body.uncertainty
            + 0.15 * body.discontent
            - 0.10 * body.reassurance
            + settings.query_weight * agent.information_query_pressure,
        priority: i64::from(distress >= 0.80),
    });
    candidates.push(Candidate {
        action: Action::SeekInformation,
        eligible: can_seek_information,
        utility: 0.20 + 0.55 * body.uncertainty + settings.query_weight * agent.information_query_pressure,
        priority: i64::from(body.uncertainty >= 0.9),
    });
    if data.autonomy_enabled && settings.autonomy {
        let zone_people = &frozen.by_zone[agent.zone];
        let zone_peers = || zone_people.iter().copied().filter(|peer| *peer != index);
        let mut family: Vec<usize> = frozen
            .group_of(agent)
            .iter()
            .copied()
            .filter(|peer| *peer != index && data.agents[*peer].zone == agent.zone)
            .collect();
        let explicit: Vec<usize> = zone_peers().filter(|peer| is_care_linked(agent, &data.agents[*peer])).collect();
        if !explicit.is_empty() {
            family.retain(|peer| explicit.contains(peer));
        } else if person.personal_context_present {
            family.clear();
        }
        let family_target = frozen.most_pressed(family.into_iter());
        let help_threshold = distress + 0.15;
        let help_target = frozen.most_pressed(zone_peers().filter(|peer| frozen.pressure[*peer] > help_threshold));
        candidates.push(Candidate {
            action: Action::Socialize,
            eligible: zone_peers().next().is_some(),
            utility: 0.10 + 0.25 * body.uncertainty + 0.15 * body.negative_affect + 0.10 * (1.0 - body.reassurance),
            priority: 0,
        });
        candidates.push(Candidate {
            action: Action::CareFamily,
            eligible: family_target.is_some_and(|target| frozen.pressure[target] >= 0.25),
            utility: 0.12 + family_target.map_or(0.0, |target| 0.45 * frozen.pressure[target]),
            priority: i64::from(family_target.is_some_and(|target| data.agents[target].stress >= 0.8)),
        });
        candidates.push(Candidate {
            action: Action::HelpOthers,
            eligible: help_target.is_some(),
            utility: 0.15 + help_target.map_or(0.0, |target| 0.4 * frozen.pressure[target]),
            priority: i64::from(help_target.is_some_and(|target| data.agents[target].stress >= 0.8)),
        });
    }
    if resource_service {
        candidates.push(Candidate {
            action: Action::FetchWater,
            eligible: needs.hydration_need >= settings.hydration_threshold,
            utility: settings.water_base + settings.water_weight * needs.hydration_need,
            priority: i64::from(needs.hydration_need >= 0.8),
        });
        candidates.push(Candidate {
            action: Action::CollectMeal,
            eligible: needs.hunger >= settings.hunger_threshold || meal_rhythm_due,
            utility: settings.meal_base + settings.meal_weight * needs.hunger + (if meal_rhythm_due { 0.12 } else { 0.0 }),
            priority: i64::from(needs.hunger >= 0.85),
        });
    }
    candidates
}

/// The probabilities of `propose`: softmax over the eligible candidates, with the demand multipliers,
/// and the last eligible one takes what the others leave.
fn score(candidates: &[Candidate], modifiers: &[DemandModifier], temperature: f64) -> Result<Vec<Scored>, &'static str> {
    let totals: Vec<f64> = candidates.iter().map(|item| item.utility + item.priority as f64 * 0.5).collect();
    let mut maximum = None;
    for (candidate, total) in candidates.iter().zip(&totals) {
        if candidate.eligible {
            maximum = Some(maximum.map_or(*total, |kept| py_max(kept, *total)));
        }
    }
    let Some(maximum) = maximum else {
        return Err("no candidate is eligible");
    };
    if temperature == 0.0 {
        return Err("the temperature is zero");
    }
    let mut weights = Vec::with_capacity(candidates.len());
    for (candidate, total) in candidates.iter().zip(&totals) {
        if !candidate.eligible {
            weights.push(0.0);
            continue;
        }
        let Some(weight) = exp((*total - maximum) / temperature) else {
            return Err("an exponent is out of range");
        };
        let multiplier = demand_of(modifiers, candidate.action).map_or(1.0, |modifier| modifier.probability_multiplier);
        weights.push(weight * multiplier);
    }
    let total_weight = py_sum(&weights);
    if total_weight == 0.0 {
        return Err("the weights sum to zero");
    }
    let mut probabilities: Vec<f64> = weights.iter().map(|weight| weight / total_weight).collect();
    let eligible: Vec<usize> = (0..candidates.len()).filter(|place| candidates[*place].eligible).collect();
    let Some((last, others)) = eligible.split_last() else {
        return Err("no candidate is eligible");
    };
    let rest: Vec<f64> = others.iter().map(|place| probabilities[*place]).collect();
    let Some(others_sum) = py_fsum(&rest) else {
        return Err("a sum of probabilities is out of range");
    };
    probabilities[*last] = py_max(0.0, 1.0 - others_sum);
    let mut scored = Vec::with_capacity(candidates.len());
    for ((candidate, total), probability) in candidates.iter().zip(totals).zip(probabilities) {
        let item = Scored {
            action: candidate.action,
            eligible: candidate.eligible,
            utility: total,
            priority: candidate.priority,
            probability,
        };
        check_candidate(&item)?;
        scored.push(item);
    }
    Ok(scored)
}

/// `UtilityBehaviorPolicy.propose`, then `PriorityRuleBehaviorPolicy.propose` if it is that policy.
fn compute(frozen: &Frozen<'_>, index: usize, temperature: f64, base_minutes: i64) -> Result<Outcome, &'static str> {
    let data = frozen.data;
    let agent = &data.agents[index];
    let zone = data.zone_of(agent);
    let modifiers = &zone.demand_modifiers[..];
    let minute = data.minute_index;
    let candidates = candidates_of(frozen, index);
    let mut evidence = score(&candidates, modifiers, temperature)?;

    let mut key = Key::labelled("behavior");
    key.text(&agent.rng_key).text(&agent.id).text("minute").int(minute);
    key.text("policy").text(frozen.settings.version).text("choice");
    let mut choice_value = key.draw(frozen.draws);
    let mut cumulative = 0.0;
    let Some(last) = evidence.iter().rev().find(|item| item.eligible).copied() else {
        return Err("no candidate is eligible");
    };
    let mut selected = last;
    for candidate in &evidence {
        if !candidate.eligible {
            continue;
        }
        cumulative += candidate.probability;
        if choice_value < cumulative {
            selected = *candidate;
            break;
        }
    }
    let interval = |action: Action| -> Result<i64, &'static str> {
        let minutes = decision_interval(
            base_minutes,
            action,
            demand_of(modifiers, action),
            zone.min_rest_decision_minutes,
            zone.max_rest_decision_minutes,
        )?;
        minute.checked_add(minutes).ok_or("a minute is out of range")
    };
    let mut next_decision_at = interval(selected.action)?;
    check_proposal(minute, next_decision_at, choice_value, selected.probability, &evidence)?;
    let mut selection_probability = selected.probability;
    let mut action = selected.action;

    let priority_rule = frozen.settings.priority_rule;
    if priority_rule {
        // Finite utilities, from check_candidate, so a tuple compares as these two do.
        let mut best: Option<(i64, f64)> = None;
        for item in evidence.iter().filter(|item| item.eligible) {
            best = match best {
                Some((priority, utility))
                    if item.priority < priority || (item.priority == priority && item.utility <= utility) =>
                {
                    Some((priority, utility))
                }
                _ => Some((item.priority, item.utility)),
            };
        }
        let Some(best) = best else {
            return Err("no candidate is eligible");
        };
        let tied = |item: &Scored| item.eligible && (item.priority, item.utility) == best;
        let mut ties: Vec<Scored> = evidence.iter().filter(|item| tied(item)).copied().collect();
        ties.sort_unstable_by_key(|item| item.action.value());
        key.text("priority-tie-break");
        choice_value = key.draw(frozen.draws);
        let count = ties.len();
        let place = ((choice_value * count as f64) as usize).min(count - 1);
        let chosen = ties[place];
        let probability = 1.0 / count as f64;
        for item in evidence.iter_mut() {
            item.probability = if tied(&*item) { probability } else { 0.0 };
            check_candidate(item)?;
        }
        action = chosen.action;
        selection_probability = probability;
        next_decision_at = interval(action)?;
        check_proposal(minute, next_decision_at, choice_value, probability, &evidence)?;
    }
    Ok(Outcome {
        candidates: evidence,
        choice_value,
        tie_broken: priority_rule,
        selection_probability,
        action,
        next_decision_at,
        target: target_for_action(frozen, index, action),
    })
}

// ---- Python's side ---------------------------------------------------------------------------------

struct Classes {
    runner: Py<PyType>,
    utility: Py<PyType>,
    priority: Py<PyType>,
    counter_rng: Py<PyType>,
    candidate: Py<PyType>,
    proposal: Py<PyType>,
    actions: Vec<Py<PyAny>>,
    new: Py<PyAny>,
}

static CLASSES: GILOnceCell<Classes> = GILOnceCell::new();

fn classes(py: Python<'_>) -> PyResult<&Classes> {
    CLASSES.get_or_try_init(py, || {
        let module = py.import("sheltersim.policies.behavior")?;
        let kind = |name: &str| -> PyResult<Py<PyType>> { Ok(module.getattr(name)?.downcast_into::<PyType>()?.unbind()) };
        let action_kind = module.getattr("ActionKind")?;
        let mut actions = Vec::with_capacity(ACTION_VALUES.len());
        for value in ACTION_VALUES {
            actions.push(action_kind.call1((value,))?.unbind());
        }
        Ok(Classes {
            runner: kind("BehaviorDecisionRunner")?,
            utility: kind("UtilityBehaviorPolicy")?,
            priority: kind("PriorityRuleBehaviorPolicy")?,
            counter_rng: py.import("sheltersim.core.rng")?.getattr("CounterRNG")?.downcast_into::<PyType>()?.unbind(),
            candidate: kind("ActionCandidate")?,
            proposal: kind("ActionProposal")?,
            actions,
            new: py.import("builtins")?.getattr("object")?.getattr("__new__")?.unbind(),
        })
    })
}

fn exact_i64(value: &Bound<'_, PyAny>) -> PyResult<i64> {
    match value.downcast_exact::<PyInt>() {
        Ok(number) => number.extract().map_err(|_| Unsupported::new_err("an int is too large")),
        Err(_) => unsupported("a policy value is not an int"),
    }
}

/// A float, or an int that converts exactly. Only operations with a float read these, in which
/// Python converts the int the same way. The trigger texts format the object itself.
fn number(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    if let Ok(float) = value.downcast_exact::<PyFloat>() {
        return Ok(float.value());
    }
    let integer = exact_i64(value)?;
    if integer.unsigned_abs() > (1_u64 << 53) {
        return unsupported("a policy value is too large");
    }
    Ok(integer as f64)
}

/// The snapshot of a view the phases made. One Python made (`from_python_view`) has zeros for most
/// of what this rule reads, so `partial` is refused, as a view without a snapshot is.
fn snapshot_of<'py>(view: &Bound<'py, PyAny>) -> PyResult<Bound<'py, RustView>> {
    let py = view.py();
    let fields = view.getattr(intern!(py, "__dict__"))?;
    let Some(found) = raw_dict_get(&fields, intern!(py, "_rust_view").as_any())? else {
        return unsupported("the view is Python's");
    };
    let rust = found.downcast_into::<RustView>()?;
    if rust.get().data().partial {
        return unsupported("the view is partial");
    }
    Ok(rust)
}

/// `format(value, "g")`, as `{value:g}` does.
fn g<'py>(value: &Bound<'py, PyAny>) -> PyResult<String> {
    let py = value.py();
    let text = value.call_method1(intern!(py, "__format__"), (intern!(py, "g"),))?;
    Ok(text.downcast_exact::<PyString>()?.to_str()?.to_owned())
}

/// The trigger text of each action, made once a call that has someone due.
struct Triggers<'py> {
    texts: Vec<Option<Bound<'py, PyString>>>,
}

impl<'py> Triggers<'py> {
    fn new(py: Python<'py>, policy: &Bound<'py, PyAny>, query_enabled: bool, resource_service: bool) -> PyResult<Self> {
        let mut texts: Vec<Option<Bound<'py, PyString>>> = vec![None; ACTION_VALUES.len()];
        let mut set = |action: Action, text: &Bound<'py, PyString>| texts[action as usize] = Some(text.clone());
        set(Action::Wait, intern!(py, "always"));
        set(Action::RequestToilet, intern!(py, "toilet_urgency>=0.25 or hygiene_need>=0.5"));
        set(Action::Rest, intern!(py, "fatigue>=0.25 or hunger>=0.70 or stress>=0.50"));
        set(Action::Sleep, intern!(py, "fatigue>=0.65 or sleep_debt_minutes>=240 or individual_sleep_window"));
        if query_enabled {
            let threshold = g(&policy.getattr(intern!(py, "information_query_threshold"))?)?;
            let text = format!(
                "support_need>=0.25 and maximum need/stress>=0.50, or information query pressure \
                 >={threshold} or uncertainty>=0.65; no_open_support_request and support_cooldown_elapsed"
            );
            set(Action::SeekHelp, &PyString::new(py, &text));
        } else {
            set(
                Action::SeekHelp,
                intern!(
                    py,
                    "support_need>=0.25 and maximum need/stress>=0.50 and no_open_support_request and \
                     support_cooldown_elapsed"
                ),
            );
        }
        set(Action::SeekInformation, intern!(py, "information_query_pressure>=threshold or uncertainty>=0.65"));
        set(Action::Socialize, intern!(py, "at least one co-located person is available"));
        set(Action::CareFamily, intern!(py, "co-located family or care-linked person has an unmet need"));
        set(Action::HelpOthers, intern!(py, "co-located person has substantially higher immediate need or stress"));
        if resource_service {
            let hydration = g(&policy.getattr(intern!(py, "hydration_action_threshold"))?)?;
            set(Action::FetchWater, &PyString::new(py, &format!("hydration_need>={hydration}")));
            let hunger = g(&policy.getattr(intern!(py, "hunger_action_threshold"))?)?;
            set(Action::CollectMeal, &PyString::new(py, &format!("hunger>={hunger} or individual_meal_window")));
        }
        Ok(Self { texts })
    }

    /// `_demand_trigger`.
    fn of(&self, py: Python<'py>, action: Action, modifier: Option<&DemandModifier>) -> PyResult<Bound<'py, PyString>> {
        let Some(trigger) = &self.texts[action as usize] else {
            return unsupported("an action has no trigger");
        };
        let Some(modifier) = modifier else {
            return Ok(trigger.clone());
        };
        let multiplier = g(PyFloat::new(py, modifier.probability_multiplier).as_any())?;
        let mut text = format!(
            "{}; experimental zone action multiplier x{multiplier}; events={}; assumptions={}",
            trigger.to_str()?,
            modifier.modifier_event_ids.join(","),
            modifier.assumption_ids.join(","),
        );
        if modifier.duration_multiplier != 1.0 {
            let duration = g(PyFloat::new(py, modifier.duration_multiplier).as_any())?;
            text.push_str(&format!("; decision interval x{duration}"));
        }
        Ok(PyString::new(py, &text))
    }
}

fn new_object<'py>(classes: &Classes, kind: &Py<PyType>, fields: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyAny>> {
    let py = fields.py();
    let object = classes.new.bind(py).call1((kind.bind(py),))?;
    raw_setattr(&object, intern!(py, "__dict__"), fields.as_ptr())?;
    Ok(object)
}

macro_rules! set {
    ($py:expr, $dict:expr, $name:literal, $value:expr) => {
        raw_dict_set($dict.as_any(), intern!($py, $name).as_any(), $value.as_any())?
    };
}

/// One `ActionProposal`, with the fields in the order its `__init__` sets them.
#[allow(clippy::too_many_arguments)]
fn proposal_of<'py>(
    py: Python<'py>,
    classes: &Classes,
    triggers: &Triggers<'py>,
    rust: &RustView,
    index: usize,
    outcome: &Outcome,
    state_hash: &Bound<'py, PyAny>,
    scorer_version: &Bound<'py, PyAny>,
    policy_version: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let data = rust.data();
    let agent = &data.agents[index];
    let modifiers = &data.zone_of(agent).demand_modifiers;
    let id = rust.key(py, index);
    let mut candidates = Vec::with_capacity(outcome.candidates.len());
    for item in &outcome.candidates {
        let fields = PyDict::new(py);
        set!(py, fields, "action", classes.actions[item.action as usize].bind(py));
        set!(py, fields, "eligible", PyBool::new(py, item.eligible));
        set!(py, fields, "trigger", triggers.of(py, item.action, demand_of(modifiers, item.action))?);
        set!(py, fields, "utility", PyFloat::new(py, item.utility));
        set!(py, fields, "urgency_priority", item.priority.into_bound_py_any(py)?);
        set!(py, fields, "probability", PyFloat::new(py, item.probability));
        candidates.push(new_object(classes, &classes.candidate, &fields)?);
    }
    let minute = data.minute_index.into_bound_py_any(py)?;
    let mut key: Vec<Bound<'py, PyAny>> = vec![
        intern!(py, "behavior").clone().into_any(),
        PyString::new(py, &agent.rng_key).into_any(),
        id.clone().into_any(),
        intern!(py, "minute").clone().into_any(),
        minute.clone(),
        intern!(py, "policy").clone().into_any(),
        scorer_version.clone(),
        intern!(py, "choice").clone().into_any(),
    ];
    if outcome.tie_broken {
        key.push(intern!(py, "priority-tie-break").clone().into_any());
    }
    let fields = PyDict::new(py);
    set!(py, fields, "agent_id", id);
    set!(py, fields, "minute_index", minute);
    set!(py, fields, "observed_state_hash", state_hash);
    set!(py, fields, "action", classes.actions[outcome.action as usize].bind(py));
    set!(py, fields, "candidates", PyTuple::new(py, candidates)?);
    set!(py, fields, "trigger", intern!(py, "scheduled_decision"));
    set!(py, fields, "choice_value", PyFloat::new(py, outcome.choice_value));
    set!(py, fields, "rng_key", PyTuple::new(py, key)?);
    set!(py, fields, "selection_probability", PyFloat::new(py, outcome.selection_probability));
    set!(py, fields, "policy_version", policy_version);
    set!(py, fields, "next_decision_at", outcome.next_decision_at.into_bound_py_any(py)?);
    match outcome.target {
        Some(target) => set!(py, fields, "target_id", rust.key(py, target)),
        None => set!(py, fields, "target_id", py.None().into_bound(py)),
    }
    new_object(classes, &classes.proposal, &fields)
}

/// `self.decisions.propose_due(view, rng)` of the propose phase.
#[pyfunction]
fn propose_due<'py>(
    controller: &Bound<'py, PyAny>,
    view: &Bound<'py, PyAny>,
    rng: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = controller.py();
    let classes = classes(py)?;
    let runner = controller.getattr(intern!(py, "decisions"))?;
    if !runner.get_type().is(classes.runner.bind(py)) {
        return unsupported("the decisions are another class");
    }
    let policy = runner.getattr(intern!(py, "policy"))?;
    let policy_type = policy.get_type();
    let (scorer, priority_rule) = if policy_type.is(classes.utility.bind(py)) {
        (policy.clone(), false)
    } else if policy_type.is(classes.priority.bind(py)) {
        let scorer = policy.getattr(intern!(py, "scorer"))?;
        if !scorer.get_type().is(classes.utility.bind(py)) {
            return unsupported("the policy's scorer is another class");
        }
        (scorer, true)
    } else {
        return unsupported("the policy is another class");
    };
    if !rng.get_type().is(classes.counter_rng.bind(py)) {
        return unsupported("the RNG is not a CounterRNG");
    }
    let seed = exact_i64(&rng.getattr(intern!(py, "seed"))?)?;
    if seed < 0 {
        return unsupported("the seed is negative");
    }
    let rust = snapshot_of(view)?;
    let rust = rust.get();
    let data = rust.data();
    let state_hash = view.getattr(intern!(py, "state_hash"))?;
    let hash_is_good = state_hash.downcast_exact::<PyString>().is_ok_and(|text| text.len().is_ok_and(|length| length == 64));
    if !hash_is_good {
        return unsupported("the view has no hash of a state");
    }

    let due = |agent: &ViewAgent| {
        data.minute_index >= agent.next_decision_at
            && matches!(agent.activity, Activity::Idle | Activity::Resting | Activity::Sleeping)
    };
    if !data.agents.iter().any(due) {
        return Ok(PyTuple::empty(py).into_any());
    }

    let scorer_version = scorer.getattr(intern!(py, "policy_version"))?;
    let Ok(version) = scorer_version.downcast_exact::<PyString>() else {
        return unsupported("the policy version is not a str");
    };
    let version_text = version.to_str().map_err(|_| Unsupported::new_err("the policy version is not UTF-8"))?;
    let policy_version =
        if priority_rule { policy.getattr(intern!(py, "policy_version"))? } else { scorer_version.clone() };
    if !policy_version.downcast_exact::<PyString>().is_ok_and(|text| text.len().is_ok_and(|length| length > 0)) {
        return unsupported("the policy version is empty or not a str");
    }
    let float = |name: &Bound<'py, PyString>| number(&scorer.getattr(name)?);
    let settings = Settings {
        version: version_text,
        sleep_aware: version_text.starts_with("sleep-aware-utility-softmax-v2"),
        sleep_debt_weight: float(intern!(py, "sleep_debt_utility_weight"))?,
        resource_service: scorer.getattr(intern!(py, "resource_service_enabled"))?.is_truthy()?,
        autonomy: scorer.getattr(intern!(py, "autonomy_enabled"))?.is_truthy()?,
        hydration_threshold: float(intern!(py, "hydration_action_threshold"))?,
        hunger_threshold: float(intern!(py, "hunger_action_threshold"))?,
        water_base: float(intern!(py, "water_utility_base"))?,
        water_weight: float(intern!(py, "water_utility_need_weight"))?,
        meal_base: float(intern!(py, "meal_utility_base"))?,
        meal_weight: float(intern!(py, "meal_utility_need_weight"))?,
        query_threshold: float(intern!(py, "information_query_threshold"))?,
        query_weight: float(intern!(py, "information_query_utility_weight"))?,
        priority_rule,
    };
    let temperature = float(intern!(py, "temperature"))?;
    let interval = exact_i64(&scorer.getattr(intern!(py, "decision_interval_minutes"))?)?;
    let temperatures = scorer.getattr(intern!(py, "temperature_by_agent"))?;
    let intervals = scorer.getattr(intern!(py, "decision_interval_minutes_by_agent"))?;
    if as_readable_dict(&temperatures).is_none() || as_readable_dict(&intervals).is_none() {
        return unsupported("the per-person coefficients are not a plain dict");
    }
    let draws = KeyedDraws::new(&seed.to_string());
    let frozen = Frozen::new(data, &settings, &draws).map_err(Unsupported::new_err)?;
    let triggers = Triggers::new(
        py,
        &scorer,
        settings.query_enabled(),
        settings.resource_service && data.resource_service_enabled,
    )?;

    let mut proposals = Vec::new();
    for (index, agent) in data.agents.iter().enumerate() {
        if !due(agent) {
            continue;
        }
        let key = rust.key(py, index);
        let temperature = match raw_dict_get(&temperatures, key.as_any())? {
            Some(value) => number(&value)?,
            None => temperature,
        };
        let interval = match raw_dict_get(&intervals, key.as_any())? {
            Some(value) => exact_i64(&value)?,
            None => interval,
        };
        let outcome = compute(&frozen, index, temperature, interval).map_err(Unsupported::new_err)?;
        proposals.push(proposal_of(py, classes, &triggers, rust, index, &outcome, &state_hash, &scorer_version, &policy_version)?);
    }
    Ok(PyTuple::new(py, proposals)?.into_any())
}

fn option_text<'py>(py: Python<'py>, text: &Option<Arc<str>>) -> Bound<'py, PyAny> {
    match text {
        Some(text) => PyString::new(py, text).into_any(),
        None => py.None().into_bound(py),
    }
}

fn text_list<'py>(py: Python<'py>, items: &[Arc<str>]) -> PyResult<Bound<'py, PyList>> {
    PyList::new(py, items.iter().map(|item| &**item))
}

/// `_agent_view_snapshot(view.agents[agent_id])`, as `_observed_state` of the propose phase.
#[pyfunction]
fn observed_state<'py>(
    controller: &Bound<'py, PyAny>,
    view: &Bound<'py, PyAny>,
    agent_id: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let py = controller.py();
    let rust = snapshot_of(view)?;
    let rust = rust.get();
    let data = rust.data();
    let Ok(id) = agent_id.downcast_exact::<PyString>() else {
        return unsupported("the ID is not a str");
    };
    let id_text = id.to_str().map_err(|_| Unsupported::new_err("the ID is not UTF-8"))?;
    let Some(index) = data.index_of(id_text) else {
        return unsupported("no such person");
    };
    let agent = &data.agents[index];
    let zone = data.zone_of(agent);
    let float = |value: f64| PyFloat::new(py, value);

    let needs = PyDict::new(py);
    set!(py, needs, "toilet_urgency", float(agent.needs.toilet_urgency));
    set!(py, needs, "hunger", float(agent.needs.hunger));
    set!(py, needs, "fatigue", float(agent.needs.fatigue));
    set!(py, needs, "hygiene_need", float(agent.body.hygiene_need));
    if data.resource_service_enabled {
        set!(py, needs, "hydration_need", float(agent.needs.hydration_need));
    }
    let psychological = PyDict::new(py);
    set!(py, psychological, "sleep_debt_minutes", float(agent.body.sleep_debt_minutes));
    set!(py, psychological, "uncertainty", float(agent.body.uncertainty));
    set!(py, psychological, "reassurance", float(agent.body.reassurance));
    set!(py, psychological, "discontent", float(agent.body.discontent));
    set!(py, psychological, "positive_affect", float(agent.body.positive_affect));
    set!(py, psychological, "negative_affect", float(agent.body.negative_affect));
    let queue = PyDict::new(py);
    set!(py, queue, "task_id", option_text(py, &agent.queue_task_id));
    set!(py, queue, "kind", option_text(py, &agent.queue_kind));
    set!(py, queue, "target_id", option_text(py, &agent.queue_target_id));
    set!(py, queue, "wait_minutes", agent.queue_wait_minutes.into_bound_py_any(py)?);
    let support = PyDict::new(py);
    set!(py, support, "request_id", option_text(py, &agent.support_request_id));
    let status = match agent.support_request_status {
        Some(RequestStatus::Pending) => PyString::new(py, "pending").into_any(),
        Some(RequestStatus::Assigned) => PyString::new(py, "assigned").into_any(),
        Some(RequestStatus::Resolved) => PyString::new(py, "resolved").into_any(),
        Some(RequestStatus::Unresolved) => PyString::new(py, "unresolved").into_any(),
        None => py.None().into_bound(py),
    };
    set!(py, support, "status", status);
    set!(py, support, "assigned_staff_id", option_text(py, &agent.support_assigned_staff_id));
    set!(py, support, "wait_minutes", agent.support_wait_minutes.into_bound_py_any(py)?);
    set!(py, support, "cooldown_remaining_minutes", agent.support_cooldown_remaining_minutes.into_bound_py_any(py)?);

    let snapshot = PyDict::new(py);
    set!(py, snapshot, "agent_id", rust.key(py, index));
    set!(py, snapshot, "zone_id", PyString::new(py, &zone.id));
    set!(py, snapshot, "activity", agent.activity.member(py)?.getattr(intern!(py, "value"))?);
    set!(py, snapshot, "target_id", option_text(py, &agent.target_id));
    set!(py, snapshot, "needs", needs);
    set!(py, snapshot, "psychological", psychological);
    set!(py, snapshot, "sanitation_level", float(zone.sanitation_level));
    set!(py, snapshot, "stress", float(agent.stress));
    set!(py, snapshot, "trust", float(agent.trust));
    set!(py, snapshot, "queue", queue);
    set!(py, snapshot, "support", support);
    if !zone.zone_environment_modifier_event_ids.is_empty() || !zone.demand_modifiers.is_empty() {
        let mut modifiers = Vec::with_capacity(zone.demand_modifiers.len());
        for item in &zone.demand_modifiers {
            let modifier = PyDict::new(py);
            set!(py, modifier, "action", PyString::new(py, &item.action));
            set!(py, modifier, "probability_multiplier", float(item.probability_multiplier));
            set!(py, modifier, "duration_multiplier", float(item.duration_multiplier));
            set!(py, modifier, "event_ids", text_list(py, &item.modifier_event_ids)?);
            set!(py, modifier, "assumption_ids", text_list(py, &item.assumption_ids)?);
            modifiers.push(modifier);
        }
        let environment = PyDict::new(py);
        set!(py, environment, "ambient_noise_level", float(zone.ambient_noise_level));
        set!(py, environment, "rest_efficiency_multiplier", float(zone.rest_efficiency_multiplier));
        set!(py, environment, "modifier_event_ids", text_list(py, &zone.zone_environment_modifier_event_ids)?);
        set!(py, environment, "assumption_ids", text_list(py, &zone.zone_environment_assumption_ids)?);
        set!(py, environment, "demand_modifiers", PyList::new(py, modifiers)?);
        set!(py, snapshot, "zone_environment", environment);
    }
    Ok(snapshot)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(propose_due, m)?)?;
    m.add_function(wrap_pyfunction!(observed_state, m)?)?;
    Ok(())
}
