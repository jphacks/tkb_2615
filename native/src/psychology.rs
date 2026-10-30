//! The mean-reverting life-state psychology update
//! (`IntegratedDistressLifeStatePolicy.explain_update` with `BodyDynamicsFormula`),
//! returning the same floats as the Python code: every operation keeps its order,
//! and `py_max`/`py_min` return the first argument on a tie as Python's do.

use crate::core::mathx::{self, py_max, py_min, py_sum};

#[inline]
fn clamp(value: f64) -> f64 {
    py_min(1.0, py_max(0.0, value))
}

const SLEEPING: f64 = 1.0;
const RESTING: f64 = 2.0;
const QUEUED: f64 = 3.0;

/// Global parameters, in this order.
pub struct Globals {
    pub sleeping_fatigue_recovery: f64,
    pub resting_fatigue_recovery: f64,
    pub need_pressure_threshold: f64,
    pub stress_pressure: f64,
    pub queued_stress: f64,
    pub support_wait_stress: f64,
    pub hydration_growth: f64,
    pub resource_service_enabled: bool,
    pub awake_sleep_debt: f64,
    pub rest_sleep_debt_recovery: f64,
    pub sleep_sleep_debt_recovery: f64,
    pub reassurance_decay: f64,
    pub uncertainty_drift: f64,
    pub uncertainty_decay: f64,
    pub discontent_queue_rate: f64,
    pub discontent_relief: f64,
    pub discontent_stress_rate: f64,
    pub positive_affect_decay: f64,
    pub hygiene_growth: f64,
    pub sanitation_hygiene_growth: f64,
    pub negative_affect_stress_rate: f64,
    pub sanitation_stress_rate: f64,
    pub sleep_debt_only: bool,
}

impl Globals {
    pub fn from_slice(values: &[f64]) -> Option<Self> {
        if values.len() != 23 {
            return None;
        }
        Some(Self {
            sleeping_fatigue_recovery: values[0],
            resting_fatigue_recovery: values[1],
            need_pressure_threshold: values[2],
            stress_pressure: values[3],
            queued_stress: values[4],
            support_wait_stress: values[5],
            hydration_growth: values[6],
            resource_service_enabled: values[7] != 0.0,
            awake_sleep_debt: values[8],
            rest_sleep_debt_recovery: values[9],
            sleep_sleep_debt_recovery: values[10],
            reassurance_decay: values[11],
            uncertainty_drift: values[12],
            uncertainty_decay: values[13],
            discontent_queue_rate: values[14],
            discontent_relief: values[15],
            discontent_stress_rate: values[16],
            positive_affect_decay: values[17],
            hygiene_growth: values[18],
            sanitation_hygiene_growth: values[19],
            negative_affect_stress_rate: values[20],
            sanitation_stress_rate: values[21],
            sleep_debt_only: values[22] != 0.0,
        })
    }
}

/// Per-agent inputs are 32 floats; see `agent_inputs` in the Python wrapper.
pub const AGENT_FIELDS: usize = 32;

/// The 12 floats of the agent's new state: toilet_urgency, hunger, fatigue,
/// hydration_need, stress, sleep_debt_minutes, uncertainty, reassurance,
/// discontent, positive_affect, negative_affect, hygiene_need. `None` where math.exp raises.
pub fn step(g: &Globals, a: &[f64]) -> Option<[f64; 12]> {
    let activity = a[0];
    let rest_efficiency = a[1];
    let (toilet, hunger, fatigue, hydration) = (a[2], a[3], a[4], a[5]);
    let stress_before = a[6];
    let sensitivity = a[7];
    let support_open = a[8] != 0.0;
    let environmental_per_minute = a[9];
    let debt_before = a[10];
    let required_sleep = a[11];
    let reassurance_before = a[12];
    let uncertainty_before = a[13];
    let query_pressure = a[14];
    let misinformation_pressure = a[15];
    let discontent_before = a[16];
    let privacy_discontent = a[17];
    let positive_before = a[18];
    let hygiene_before = a[19];
    let ventilation_growth = a[20];
    let sanitation = a[21];
    let negative_before = a[22];
    let family_separated = a[23] != 0.0;
    let (toilet_growth, hunger_growth, awake_fatigue) = (a[24], a[25], a[26]);
    let (kappa, baseline) = (a[27], a[28]);
    let (sleep_rate, negative_rate, discontent_rate) = (a[29], a[30], a[31]);

    // SimplePsychologyPolicy, without direct rest stress recovery.
    let fatigue_delta = if activity == SLEEPING {
        -g.sleeping_fatigue_recovery * rest_efficiency
    } else if activity == RESTING {
        -g.resting_fatigue_recovery * rest_efficiency
    } else {
        awake_fatigue
    };
    let stress_recovery = 0.0;
    let mut highest = py_max(py_max(toilet, hunger), fatigue);
    if g.resource_service_enabled {
        highest = py_max(highest, hydration);
    }
    let unmet = py_max(0.0, highest - g.need_pressure_threshold);
    let need_pressure_stress = g.stress_pressure * unmet * sensitivity;
    let queue_wait_stress = if activity == QUEUED { g.queued_stress } else { 0.0 };
    let support_wait_stress = if support_open { g.support_wait_stress } else { 0.0 };
    let environmental_stress = environmental_per_minute * sensitivity;
    let _simple_stress = clamp(
        stress_before
            + (need_pressure_stress + queue_wait_stress + support_wait_stress + environmental_stress
                - stress_recovery),
    );
    let new_toilet = clamp(toilet + toilet_growth);
    let new_hunger = clamp(hunger + hunger_growth);
    let new_fatigue = clamp(fatigue + fatigue_delta);
    let new_hydration = if g.resource_service_enabled {
        clamp(hydration + g.hydration_growth)
    } else {
        0.0
    };

    // LifeStatePsychologyPolicy, with tick-start stress for feedback.
    let family_pressure = if family_separated { g.discontent_stress_rate } else { 0.0 };
    let effective_awake = if required_sleep < 0.0 {
        g.awake_sleep_debt
    } else if required_sleep == 0.0 {
        0.0
    } else {
        py_min(10.0, required_sleep / py_max(1.0, 1440.0 - required_sleep))
    };
    let next_debt = if activity == SLEEPING {
        debt_before - g.sleep_sleep_debt_recovery * rest_efficiency
    } else if activity == RESTING {
        debt_before - g.rest_sleep_debt_recovery * rest_efficiency
    } else {
        debt_before + effective_awake
    };
    let debt = py_min(1440.0, py_max(0.0, next_debt));
    let reassurance = py_max(0.0, reassurance_before - g.reassurance_decay);
    let uncertainty_without = py_min(
        1.0,
        py_max(
            0.0,
            uncertainty_before + query_pressure * g.uncertainty_drift
                - g.uncertainty_decay
                - reassurance * g.uncertainty_decay,
        ),
    );
    let uncertainty = py_min(
        1.0,
        py_max(
            0.0,
            uncertainty_before + (query_pressure + misinformation_pressure) * g.uncertainty_drift
                - g.uncertainty_decay
                - reassurance * g.uncertainty_decay,
        ),
    );
    let queue_pressure = if activity == QUEUED { g.discontent_queue_rate } else { 0.0 };
    let need_pressure = py_max(py_max(py_max(toilet, hunger), fatigue), hydration);
    let support_relief = if (activity == RESTING || activity == SLEEPING) && !support_open {
        g.discontent_relief
    } else {
        0.0
    };
    let reference = stress_before;
    let discontent = py_min(
        1.0,
        py_max(
            0.0,
            discontent_before + reference * g.discontent_stress_rate + queue_pressure
                + privacy_discontent
                + py_max(0.0, need_pressure - 0.5) * g.discontent_stress_rate
                - support_relief
                - reassurance * g.discontent_relief,
        ),
    );
    let positive =
        0.5 + (positive_before - 0.5) * py_max(0.0, 1.0 - g.positive_affect_decay);
    let hygiene = py_min(
        1.0,
        py_max(
            0.0,
            hygiene_before + g.hygiene_growth + ventilation_growth
                + (1.0 - sanitation) * g.sanitation_hygiene_growth,
        ),
    );
    let negative = py_min(
        1.0,
        py_max(
            0.0,
            negative_before + reference * g.negative_affect_stress_rate
                - reassurance * g.negative_affect_stress_rate,
        ),
    );
    let feedback_with = |uncertainty: f64| -> f64 {
        let debt_component = debt / 1440.0 * sleep_rate;
        if g.sleep_debt_only {
            return debt_component;
        }
        debt_component + uncertainty * negative_rate + discontent * discontent_rate
            + hygiene * g.sanitation_stress_rate
            + (1.0 - sanitation) * g.sanitation_stress_rate
            + family_pressure
            - reassurance * negative_rate
            - positive * g.discontent_relief
    };
    let feedback = feedback_with(uncertainty);
    let information_feedback = feedback - feedback_with(uncertainty_without);
    let life_feedback = feedback - information_feedback;

    // IntegratedDistressLifeStatePolicy: exact mean reversion over one minute.
    let decay = mathx::exp(-kappa * 1.0)?;
    let natural = (baseline - stress_before) * (1.0 - decay);
    let multiplier = if kappa == 0.0 {
        1.0
    } else {
        -mathx::expm1(-kappa * 1.0)? / kappa
    };
    let forced = py_sum(&[
        need_pressure_stress * multiplier,
        queue_wait_stress * multiplier,
        support_wait_stress * multiplier,
        environmental_stress * multiplier,
        life_feedback * multiplier,
        information_feedback * multiplier,
    ]);
    let stress = clamp(stress_before + (natural + forced));
    Some([
        new_toilet,
        new_hunger,
        new_fatigue,
        new_hydration,
        stress,
        debt,
        uncertainty,
        reassurance,
        discontent,
        positive,
        negative,
        hygiene,
    ])
}
