//! Each person's psychology in the update phase: the new values of a minute
//! (`policies/psychology.py`, all five built-in models and `BoundedStressPolicy` of
//! `policies/alternatives.py`), the distress monitor (`domain/distress_monitor.py`), the long-term
//! distress (`update_psychological_state` in `domain/psychological_state.py`) and writing the new
//! values (`apply_state_deltas`). The entity fields come from the rows of `state/entities.rs`, and
//! the zone's conditions, the body record, the group and what a person heard from the snapshot of
//! `rules/world_view.rs`.
//!
//! A kernel changes nothing until it knows the Python rules would pass: first `plan_*` computes
//! everything from the rows, and any value the Python rules would turn down, or that this kernel
//! does not handle, gives Unsupported, so the Python step runs and raises its own text. Then the plan is written to
//! the row and the tree, value by value, without replacing anything: the namespace is never
//! forwarded whole. Every float operation keeps the order of the Python expression.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyString, PyTuple, PyType};
use rustc_hash::{FxHashMap, FxHashSet};

use crate::core::mathx::{self, py_max, py_min, py_sum};
use crate::core::row::{map_set, Field};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::{field_mirror_of, mirror_of, raw_dict_get, raw_dict_set, NamespaceMirror, Tree};
use crate::rules::world_view::{view_of, RustView, ViewAgent, ViewZone};
use crate::state::entities::{entities_of, raw_setattr, Activity, EntityStore, RequestStatus};
use crate::state::metric_state::MetricRow;
use crate::state::psychological_distress::{AgentRecord, DistressRow, NAME};
use crate::Unsupported;

const LONG_TERM_POLICIES: [&str; 2] = ["mean-reverting-distress-v1", "mean-reverting-life-state-v1"];
const MONITOR_PREFIX: &str = "acute_distress_monitor:";
const MONITOR_SUFFIXES: [&str; 5] =
    [":current_run", ":longest_run", ":episodes", ":recovered_run_total", ":recovered_run_count"];
const MINUTES_PER_DAY: i64 = 1440;

fn unsupported<T>(reason: &'static str) -> PyResult<T> {
    Err(Unsupported::new_err(reason))
}

/// An exact float the Python check `validate_unit_interval` passes.
fn unit_float(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    let Ok(number) = value.downcast_exact::<PyFloat>() else {
        return unsupported("a value is not a float");
    };
    let number = number.value();
    if !(0.0..=1.0).contains(&number) {
        return unsupported("a value is outside 0..1");
    }
    Ok(number)
}

/// One person's values of this tick, sorted by id as the Python rules go through them.
struct Person<'py> {
    id: Bound<'py, PyString>,
    previous: f64,
    current: f64,
}

/// `previous_by_agent` and `current_by_agent` of the Python step, or Unsupported if the Python
/// rules would do anything but read them: their keys differ, or a value is out of bounds. The
/// previous values are the rows': nothing has written a person since the view was made.
fn people<'py>(world: &Bound<'py, PyAny>, deltas: &Bound<'py, PyAny>) -> PyResult<Vec<Person<'py>>> {
    let py = world.py();
    let Ok(deltas) = deltas.downcast_exact::<PyTuple>() else {
        return unsupported("the deltas are not a tuple");
    };
    let mut found = Vec::with_capacity(deltas.len());
    for delta in deltas.iter_borrowed() {
        let id = delta.getattr(intern!(py, "agent_id"))?;
        let Ok(id) = id.downcast_exact::<PyString>() else {
            return unsupported("an id is not a str");
        };
        // A lone surrogate has no UTF-8; after this, to_str does not fail for these ids.
        if id.to_str().is_err() {
            return unsupported("an id is not UTF-8");
        }
        found.push(Person {
            previous: 0.0,
            current: unit_float(&delta.getattr(intern!(py, "stress"))?)?,
            id: id.clone(),
        });
    }
    EntityStore::with_rows(&entities_of(world)?, |rows| {
        if found.len() != rows.agents.len() {
            return unsupported("the world and the deltas have different people");
        }
        for person in &mut found {
            let Some(row) = rows.agents.get(person.id.to_str()?) else {
                return unsupported("the world and the deltas have different people");
            };
            if !(0.0..=1.0).contains(&row.stress) {
                return unsupported("a value is outside 0..1");
            }
            person.previous = row.stress;
        }
        Ok(())
    })?;
    found.sort_unstable_by(|a, b| {
        let (first, second) = (a.id.to_str().unwrap_or(""), b.id.to_str().unwrap_or(""));
        first.as_bytes().cmp(second.as_bytes())
    });
    if found.windows(2).any(|pair| pair[0].id.to_str().ok() == pair[1].id.to_str().ok()) {
        return unsupported("two deltas for one person");
    }
    Ok(found)
}

// ---- The distress monitor ----------------------------------------------------------------------

/// run, longest, episodes, recovered total, recovered count, in the order of `MONITOR_SUFFIXES`.
#[inline]
fn next_monitor_values(values: [f64; 5], previous: f64, current: f64, threshold: f64) -> [f64; 5] {
    let [mut run, mut longest, mut episodes, mut recovered_total, mut recovered_count] = values;
    let was_high = previous >= threshold;
    let is_high = current >= threshold;
    if run > 0.0 && !(was_high && is_high) {
        recovered_total += run;
        recovered_count += 1.0;
        run = 0.0;
    }
    if is_high {
        if run == 0.0 {
            run = 1.0;
            episodes += 1.0;
        } else {
            run += 1.0;
        }
        longest = py_max(longest, run);
    }
    [run, longest, episodes, recovered_total, recovered_count]
}

fn metric_key<'a>(buffer: &'a mut String, id: &str, suffix: &str) -> &'a str {
    buffer.clear();
    buffer.push_str(MONITOR_PREFIX);
    buffer.push_str(id);
    buffer.push_str(suffix);
    buffer
}

fn plan_monitor(
    metrics: &MetricRow,
    people: &[Person<'_>],
    threshold: f64,
) -> PyResult<Vec<[f64; 5]>> {
    let mut buffer = String::with_capacity(64);
    let mut steps = Vec::with_capacity(people.len());
    for person in people {
        let id = person.id.to_str()?;
        let mut values = [0.0; 5];
        for (place, suffix) in MONITOR_SUFFIXES.iter().enumerate() {
            if let Some(value) = metrics.get(metric_key(&mut buffer, id, suffix)) {
                // Not finite or negative, and NaN fails the comparison: the Python rule raises.
                if !(0.0 <= *value && *value < f64::INFINITY) {
                    return unsupported("a monitor value is negative or not finite");
                }
                values[place] = *value;
            }
        }
        steps.push(next_monitor_values(values, person.previous, person.current, threshold));
    }
    Ok(steps)
}

fn write_monitor(
    metrics: &mut MetricRow,
    tree: &Tree<'_>,
    people: &[Person<'_>],
    steps: &[[f64; 5]],
) -> PyResult<()> {
    let mut buffer = String::with_capacity(64);
    for (person, next) in people.iter().zip(steps) {
        let id = person.id.to_str()?;
        for (suffix, value) in MONITOR_SUFFIXES.iter().zip(next) {
            map_set(metrics, tree, metric_key(&mut buffer, id, suffix), *value)?;
        }
    }
    Ok(())
}

// ---- Long-term distress ------------------------------------------------------------------------

#[derive(Clone, Copy)]
struct LongTermParameters {
    retention: f64,
    acute_weight: f64,
    high_distress_weight: f64,
    high_distress_threshold: f64,
}

/// The day's end value of D: `next_long_term_distress` in `domain/psychological_state.py`.
#[inline]
fn next_long_term_distress(record: &AgentRecord, parameters: &LongTermParameters) -> f64 {
    let observed = record.daily_observed_minutes as f64;
    let mean_acute = record.daily_acute_sum / observed;
    let high_fraction = record.daily_high_distress_minutes as f64 / observed;
    let baseline = record.baseline_distress;
    let next_value = baseline
        + parameters.retention * (record.long_term_distress - baseline)
        + parameters.acute_weight * (mean_acute - baseline)
        + parameters.high_distress_weight * high_fraction;
    py_min(1.0, py_max(0.0, next_value))
}

#[inline]
fn accumulate(record: &mut AgentRecord, acute: f64, threshold: f64) {
    record.daily_acute_sum += acute;
    record.daily_observed_minutes += 1;
    if acute >= threshold {
        record.daily_high_distress_minutes += 1;
    }
}

#[inline]
fn close_day(record: &mut AgentRecord, parameters: &LongTermParameters) -> PyResult<()> {
    record.long_term_distress = next_long_term_distress(record, parameters);
    record.daily_acute_sum = 0.0;
    record.daily_high_distress_minutes = 0;
    record.daily_observed_minutes = 0;
    record.completed_days = match record.completed_days.checked_add(1) {
        Some(days) => days,
        None => return unsupported("completed days do not fit"),
    };
    Ok(())
}

struct LongTermPlan {
    /// By position in the row: the record's next values, if it changes.
    changed: Vec<Option<AgentRecord>>,
    /// New records, in the order they are added.
    added: Vec<(usize, AgentRecord)>,
}

fn plan_long_term(
    row: &DistressRow,
    people: &[Person<'_>],
    minute: i64,
    parameters: &LongTermParameters,
    baseline_by_agent: &Bound<'_, PyAny>,
) -> PyResult<LongTermPlan> {
    if !row.is_valid() {
        return unsupported("the namespace does not pass validation");
    }
    if row.last_updated_minute != minute - 1 {
        return unsupported("the namespace is not at the previous minute");
    }
    let threshold = parameters.high_distress_threshold;
    let mut changed: Vec<Option<AgentRecord>> = vec![None; row.agents.len()];
    let mut added = Vec::new();
    for (place, person) in people.iter().enumerate() {
        let id = person.id.to_str()?;
        match row.agents.get_index_of(id) {
            Some(index) => {
                let mut record = row.agents[index];
                accumulate(&mut record, person.current, threshold);
                changed[index] = Some(record);
            }
            None => {
                let baseline = match as_readable_dict(baseline_by_agent) {
                    Some(baselines) => match baselines.get_item(&person.id)? {
                        Some(baseline) => unit_float(&baseline)?,
                        None => 0.0,
                    },
                    None => return unsupported("the baselines are not a plain dict"),
                };
                let mut record = AgentRecord {
                    baseline_distress: baseline,
                    long_term_distress: baseline,
                    daily_acute_sum: 0.0,
                    daily_high_distress_minutes: 0,
                    daily_observed_minutes: 0,
                    completed_days: 0,
                };
                accumulate(&mut record, person.current, threshold);
                added.push((place, record));
            }
        }
    }
    if (minute + 1) % MINUTES_PER_DAY == 0 {
        for (index, (_, before)) in row.agents.iter().enumerate() {
            let mut record = changed[index].unwrap_or(*before);
            if record.daily_observed_minutes != 0 {
                close_day(&mut record, parameters)?;
                changed[index] = Some(record);
            }
        }
        for (_, record) in &mut added {
            close_day(record, parameters)?;
        }
    }
    let all_valid = changed.iter().flatten().all(AgentRecord::is_valid)
        && added.iter().all(|(place, record)| {
            record.is_valid() && people[*place].id.to_str().is_ok_and(|id| !id.is_empty())
        })
        && row.agents.len() + added.len() <= 100_000;
    if !all_valid {
        return unsupported("the new values do not pass validation");
    }
    Ok(LongTermPlan { changed, added })
}

fn write_long_term<'py>(
    mirror: &Bound<'py, NamespaceMirror>,
    people: &[Person<'py>],
    minute: i64,
    plan: LongTermPlan,
) -> PyResult<()> {
    let py = mirror.py();
    NamespaceMirror::with_row_tree::<DistressRow, _>(mirror, |row, tree| {
        let agents = tree.child(intern!(py, "agents"))?;
        for (index, next) in plan.changed.iter().enumerate() {
            let Some(next) = next else {
                continue;
            };
            let Some((id, record)) = row.agents.get_index_mut(index) else {
                return unsupported("the row changed under the plan");
            };
            let node = agents.child_str(id)?;
            record.set_long_term_distress(&node, next.long_term_distress)?;
            record.set_daily_acute_sum(&node, next.daily_acute_sum)?;
            record.set_daily_high_distress_minutes(&node, next.daily_high_distress_minutes)?;
            record.set_daily_observed_minutes(&node, next.daily_observed_minutes)?;
            record.set_completed_days(&node, next.completed_days)?;
        }
        row.set_last_updated_minute(tree, minute)
    })?;
    for (place, record) in plan.added {
        let id = people[place].id.to_str()?;
        NamespaceMirror::with_row::<DistressRow, _>(mirror, |row| {
            row.agents.insert(Arc::from(id), record);
            Ok(())
        })?;
        NamespaceMirror::write_through(mirror, &["agents"], id, &Field::to_py(&record, py)?)?;
    }
    Ok(())
}

// ---- The step ----------------------------------------------------------------------------------

/// `_update_long_term_psychology` of the update phase (experiments/phases/segments.py).
#[pyfunction]
fn update_long_term<'py>(
    controller: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    _view: &Bound<'py, PyAny>,
    final_deltas: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let policy = controller.getattr(intern!(py, "psychology"))?;
    let known = policy.getattr(intern!(py, "policy_version")).is_ok_and(|version| {
        version
            .downcast_exact::<PyString>()
            .is_ok_and(|text| text.to_str().is_ok_and(|text| LONG_TERM_POLICIES.contains(&text)))
    });
    if !known {
        return unsupported("the policy has no long-term state this kernel knows");
    }
    let Ok(minute) = minute.downcast_exact::<PyInt>() else {
        return unsupported("the minute is not an int");
    };
    let minute: i64 = minute.extract().map_err(|_| Unsupported::new_err("the minute is too large"))?;
    if minute < 0 || minute == i64::MAX {
        return unsupported("the minute is negative or too large");
    }
    let settings = policy.getattr(intern!(py, "parameters"))?;
    let parameters = LongTermParameters {
        retention: unit_float(&settings.getattr(intern!(py, "chronic_retention"))?)?,
        acute_weight: unit_float(&settings.getattr(intern!(py, "chronic_acute_weight"))?)?,
        high_distress_weight: unit_float(&settings.getattr(intern!(py, "chronic_high_distress_weight"))?)?,
        high_distress_threshold: unit_float(&settings.getattr(intern!(py, "high_distress_threshold"))?)?,
    };
    let baseline_by_agent = settings.getattr(intern!(py, "baseline_distress_by_agent"))?;
    let people = people(world, final_deltas)?;

    let distress = mirror_of(world, NAME)?;
    let metrics = field_mirror_of(world, intern!(py, "metric_state"))?;
    let monitor = NamespaceMirror::with_row::<MetricRow, _>(&metrics, |row| {
        plan_monitor(row, &people, parameters.high_distress_threshold)
    })?;
    let long_term = NamespaceMirror::with_row::<DistressRow, _>(&distress, |row| {
        plan_long_term(row, &people, minute, &parameters, &baseline_by_agent)
    })?;

    NamespaceMirror::with_row_tree::<MetricRow, _>(&metrics, |row, tree| {
        write_monitor(row, tree, &people, &monitor)
    })?;
    write_long_term(&distress, &people, minute, long_term)
}

// ---- A minute of each person's values ----------------------------------------------------------

#[derive(Clone, Copy, PartialEq)]
enum Model {
    /// `SimplePsychologyPolicy`
    Simple,
    /// `BoundedStressPolicy`
    Bounded,
    /// `MeanRevertingDistressPolicy`
    MeanReverting,
    /// `LifeStatePsychologyPolicy`
    LifeState,
    /// `IntegratedDistressLifeStatePolicy`
    Integrated,
}

/// The policy by its exact class: a subclass may change any rule, and is left to Python.
fn model_of(policy: &Bound<'_, PyAny>) -> PyResult<Model> {
    let py = policy.py();
    let class = policy.get_type();
    let module = class.getattr(intern!(py, "__module__"))?;
    let name = class.getattr(intern!(py, "__qualname__"))?;
    let (Ok(module), Ok(name)) = (module.downcast_exact::<PyString>(), name.downcast_exact::<PyString>())
    else {
        return unsupported("the policy has no plain class name");
    };
    match (module.to_str()?, name.to_str()?) {
        ("sheltersim.policies.psychology", "SimplePsychologyPolicy") => Ok(Model::Simple),
        ("sheltersim.policies.alternatives", "BoundedStressPolicy") => Ok(Model::Bounded),
        ("sheltersim.policies.psychology", "MeanRevertingDistressPolicy") => Ok(Model::MeanReverting),
        ("sheltersim.policies.psychology", "LifeStatePsychologyPolicy") => Ok(Model::LifeState),
        ("sheltersim.policies.psychology", "IntegratedDistressLifeStatePolicy") => Ok(Model::Integrated),
        _ => unsupported("the policy is not one this kernel knows"),
    }
}

fn exact_float(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    match value.downcast_exact::<PyFloat>() {
        Ok(number) => Ok(number.value()),
        Err(_) => unsupported("a value is not a float"),
    }
}

/// An item of a dict that must be a float.
fn float_item(dict: &Bound<'_, PyAny>, key: &Bound<'_, PyString>) -> PyResult<f64> {
    match raw_dict_get(dict, key.as_any())? {
        Some(value) => exact_float(&value),
        None => unsupported("a field is missing"),
    }
}

/// `by_agent.get(id, default)` of a dict of floats.
#[inline]
fn rate_of(by_agent: &Bound<'_, PyAny>, id: &Bound<'_, PyString>, default: f64) -> PyResult<f64> {
    match raw_dict_get(by_agent, id.as_any())? {
        Some(value) => exact_float(&value),
        None => Ok(default),
    }
}

fn readable_dict<'py>(value: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if as_readable_dict(&value).is_none() {
        return unsupported("a table of the policy is not a plain dict");
    }
    Ok(value)
}

/// The rates of `PsychologyParameters`.
struct Rates {
    toilet_growth: f64,
    hunger_growth: f64,
    awake_fatigue_growth: f64,
    resting_fatigue_recovery: f64,
    sleeping_fatigue_recovery: f64,
    stress_pressure: f64,
    queued_stress: f64,
    resting_stress_recovery: f64,
    sleeping_stress_recovery: f64,
    need_pressure_threshold: f64,
    hydration_growth: f64,
    support_wait_stress: f64,
    natural_recovery_rate: f64,
    direct_rest_recovery: bool,
    resource_service_enabled: bool,
}

/// What `canonical_body_config` keeps for the life-state models.
struct BodyRates {
    awake_sleep_debt: f64,
    rest_sleep_debt_recovery: f64,
    sleep_sleep_debt_recovery: f64,
    sleep_debt_stress: f64,
    reassurance_decay: f64,
    uncertainty_drift: f64,
    uncertainty_decay: f64,
    discontent_queue: f64,
    discontent_relief: f64,
    discontent_stress: f64,
    positive_affect_decay: f64,
    hygiene_growth: f64,
    sanitation_hygiene_growth: f64,
    negative_affect_stress: f64,
    sanitation_stress: f64,
    /// `body-feedback-sleep-debt-v1`: only the debt feeds back.
    sleep_debt_only: bool,
    tick_start_reference: bool,
}

/// The dicts by person, for lookups that a scenario may fill with one value per person.
struct RateTables<'py> {
    toilet: Bound<'py, PyAny>,
    hunger: Bound<'py, PyAny>,
    awake_fatigue: Bound<'py, PyAny>,
    natural_recovery: Bound<'py, PyAny>,
    baseline: Bound<'py, PyAny>,
    // Only with a formula; the built-in feedback of a life-state policy without one has no tables.
    sleep_debt: Option<Bound<'py, PyAny>>,
    negative: Option<Bound<'py, PyAny>>,
    discontent: Option<Bound<'py, PyAny>>,
}

macro_rules! float_attr {
    ($object:expr, $name:literal) => {
        exact_float(&$object.getattr(intern!($object.py(), $name))?)?
    };
}

fn exact_bool(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    match value.downcast_exact::<PyBool>() {
        Ok(flag) => Ok(flag.is_true()),
        Err(_) => unsupported("a flag is not a bool"),
    }
}

fn read_rates<'py>(
    policy: &Bound<'py, PyAny>,
    parameters: &Bound<'py, PyAny>,
    view: &Bound<'py, PyAny>,
    model: Model,
) -> PyResult<Rates> {
    let py = policy.py();
    let holder = match model {
        Model::LifeState | Model::Integrated => policy.getattr(intern!(py, "base"))?,
        _ => policy.clone(),
    };
    let rates = Rates {
        toilet_growth: float_attr!(parameters, "toilet_growth"),
        hunger_growth: float_attr!(parameters, "hunger_growth"),
        awake_fatigue_growth: float_attr!(parameters, "awake_fatigue_growth"),
        resting_fatigue_recovery: float_attr!(parameters, "resting_fatigue_recovery"),
        sleeping_fatigue_recovery: float_attr!(parameters, "sleeping_fatigue_recovery"),
        stress_pressure: float_attr!(parameters, "stress_pressure"),
        queued_stress: float_attr!(parameters, "queued_stress"),
        resting_stress_recovery: float_attr!(parameters, "resting_stress_recovery"),
        sleeping_stress_recovery: float_attr!(parameters, "sleeping_stress_recovery"),
        need_pressure_threshold: float_attr!(parameters, "need_pressure_threshold"),
        hydration_growth: float_attr!(parameters, "hydration_growth"),
        support_wait_stress: float_attr!(parameters, "support_wait_stress"),
        natural_recovery_rate: float_attr!(parameters, "natural_recovery_rate_per_minute"),
        direct_rest_recovery: exact_bool(
            &holder.getattr(intern!(py, "include_direct_rest_stress_recovery"))?,
        )?,
        resource_service_enabled: exact_bool(&view.getattr(intern!(py, "resource_service_enabled"))?)?,
    };
    Ok(rates)
}

fn read_body_rates<'py>(
    policy: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    formula: &Bound<'py, PyAny>,
) -> PyResult<BodyRates> {
    let py = policy.py();
    let sleep_debt_only = if formula.is_none() {
        false
    } else {
        let class = formula.get_type();
        let module = class.getattr(intern!(py, "__module__"))?;
        let name = class.getattr(intern!(py, "__qualname__"))?;
        let known = module.downcast_exact::<PyString>().is_ok_and(|text| {
            text.to_str().is_ok_and(|text| text == "sheltersim.policies.registry")
        }) && name
            .downcast_exact::<PyString>()
            .is_ok_and(|text| text.to_str().is_ok_and(|text| text == "BodyDynamicsFormula"));
        if !known {
            return unsupported("the body formula is not one this kernel knows");
        }
        let model_id = formula.getattr(intern!(py, "model_id"))?;
        let Ok(model_id) = model_id.downcast_exact::<PyString>() else {
            return unsupported("the body formula has no plain id");
        };
        model_id.to_str()? == "body-feedback-sleep-debt-v1"
    };
    let item = |key: &Bound<'py, PyString>| float_item(config, key);
    let rates = BodyRates {
        awake_sleep_debt: item(intern!(py, "awake_sleep_debt_minutes_per_minute"))?,
        rest_sleep_debt_recovery: item(intern!(py, "rest_sleep_debt_recovery_per_minute"))?,
        sleep_sleep_debt_recovery: item(intern!(py, "sleep_sleep_debt_recovery_per_minute"))?,
        sleep_debt_stress: item(intern!(py, "sleep_debt_stress_rate"))?,
        reassurance_decay: item(intern!(py, "reassurance_decay_per_minute"))?,
        uncertainty_drift: item(intern!(py, "uncertainty_drift_per_minute"))?,
        uncertainty_decay: item(intern!(py, "uncertainty_decay_per_minute"))?,
        discontent_queue: item(intern!(py, "discontent_queue_rate"))?,
        discontent_relief: item(intern!(py, "discontent_relief_per_minute"))?,
        discontent_stress: item(intern!(py, "discontent_stress_rate"))?,
        positive_affect_decay: item(intern!(py, "positive_affect_decay_per_minute"))?,
        hygiene_growth: item(intern!(py, "hygiene_growth_per_minute"))?,
        sanitation_hygiene_growth: item(intern!(py, "sanitation_hygiene_growth_per_minute"))?,
        negative_affect_stress: item(intern!(py, "negative_affect_stress_rate"))?,
        sanitation_stress: item(intern!(py, "sanitation_stress_rate"))?,
        sleep_debt_only,
        tick_start_reference: exact_bool(
            &policy.getattr(intern!(py, "use_tick_start_stress_for_feedback"))?,
        )?,
    };
    // The checks of sleep_debt_step on the rates; the others only meet arithmetic.
    let usable = |rate: f64| rate.is_finite() && rate >= 0.0;
    if !(usable(rates.awake_sleep_debt)
        && usable(rates.rest_sleep_debt_recovery)
        && usable(rates.sleep_sleep_debt_recovery))
    {
        return unsupported("a sleep debt rate is out of bounds");
    }
    Ok(rates)
}

/// A person's values from the view that the life-state models read.
#[derive(Clone, Copy, Default)]
struct LifeInput {
    debt: f64,
    /// `required_sleep_minutes`, which is an int within 0..1440, or None.
    required_sleep: Option<i64>,
    reassurance: f64,
    uncertainty: f64,
    query_pressure: f64,
    misinformation_pressure: f64,
    discontent: f64,
    privacy_discontent: f64,
    positive: f64,
    hygiene: f64,
    ventilation_growth: f64,
    sanitation: f64,
    negative: f64,
    family_separated: bool,
    sleep_rate: f64,
    negative_rate: f64,
    discontent_rate: f64,
}

/// What the rules read of one person.
#[derive(Clone, Copy)]
struct Input {
    activity: Activity,
    rest_efficiency: f64,
    toilet: f64,
    hunger: f64,
    fatigue: f64,
    hydration: f64,
    stress: f64,
    sensitivity: f64,
    support_open: bool,
    environmental_per_minute: f64,
    toilet_growth: f64,
    hunger_growth: f64,
    awake_fatigue: f64,
    kappa: f64,
    baseline: f64,
    life: LifeInput,
}

/// What `AgentStateDelta` holds: the needs and stress, and the body values if the model has them.
#[derive(Clone, Copy)]
struct Next {
    toilet: f64,
    hunger: f64,
    fatigue: f64,
    hydration: f64,
    stress: f64,
    /// sleep_debt_minutes, uncertainty, reassurance, discontent, positive_affect,
    /// negative_affect, hygiene_need.
    body: Option<[f64; 7]>,
}

#[inline]
fn clamp(value: f64) -> f64 {
    py_min(1.0, py_max(0.0, value))
}

/// `_mean_reverting_terms(value, baseline, kappa, 1.0)`, whose checks the caller has done.
#[inline]
fn mean_reverting_terms(value: f64, baseline: f64, kappa: f64) -> Option<(f64, f64)> {
    let decay = mathx::exp(-kappa * 1.0)?;
    let natural = (baseline - value) * (1.0 - decay);
    if kappa == 0.0 {
        return Some((natural, 1.0));
    }
    Some((natural, -mathx::expm1(-kappa * 1.0)? / kappa))
}

/// One person's next minute. `None` where a math function raises in Python. Every float operation
/// keeps the order of `policies/psychology.py`, and `py_max`/`py_min` return the first on a tie.
fn step(model: Model, rates: &Rates, body: Option<&BodyRates>, a: &Input) -> Option<Next> {
    // SimplePsychologyPolicy.explain_update
    let (fatigue_delta, stress_recovery) = match a.activity {
        Activity::Sleeping => (
            -rates.sleeping_fatigue_recovery * a.rest_efficiency,
            if rates.direct_rest_recovery { rates.sleeping_stress_recovery } else { 0.0 },
        ),
        Activity::Resting => (
            -rates.resting_fatigue_recovery * a.rest_efficiency,
            if rates.direct_rest_recovery { rates.resting_stress_recovery } else { 0.0 },
        ),
        _ => (a.awake_fatigue, 0.0),
    };
    let mut highest = py_max(py_max(a.toilet, a.hunger), a.fatigue);
    if rates.resource_service_enabled {
        highest = py_max(highest, a.hydration);
    }
    let unmet = py_max(0.0, highest - rates.need_pressure_threshold);
    let need_pressure_stress = rates.stress_pressure * unmet * a.sensitivity;
    let queued = a.activity == Activity::Queued;
    let queue_wait_stress = if queued { rates.queued_stress } else { 0.0 };
    let support_wait_stress = if a.support_open { rates.support_wait_stress } else { 0.0 };
    let environmental_stress = a.environmental_per_minute * a.sensitivity;
    let simple_stress = clamp(
        a.stress
            + (need_pressure_stress + queue_wait_stress + support_wait_stress + environmental_stress
                - stress_recovery),
    );
    let needs = |stress: f64, body: Option<[f64; 7]>| Next {
        toilet: clamp(a.toilet + a.toilet_growth),
        hunger: clamp(a.hunger + a.hunger_growth),
        fatigue: clamp(a.fatigue + fatigue_delta),
        hydration: if rates.resource_service_enabled {
            clamp(a.hydration + rates.hydration_growth)
        } else {
            0.0
        },
        stress,
        body,
    };
    match model {
        Model::Simple => return Some(needs(simple_stress, None)),
        // BoundedStressPolicy.explain_update
        Model::Bounded => {
            let pressure = py_sum(&[
                need_pressure_stress,
                queue_wait_stress,
                support_wait_stress,
                environmental_stress,
            ]);
            let added = (1.0 - a.stress) * -mathx::expm1(-pressure)?;
            let recovery = a.stress * -mathx::expm1(-stress_recovery)?;
            return Some(needs(py_min(1.0, py_max(0.0, a.stress + added - recovery)), None));
        }
        // MeanRevertingDistressPolicy.explain_update
        Model::MeanReverting => {
            let (natural, multiplier) = mean_reverting_terms(a.stress, a.baseline, a.kappa)?;
            let forced = py_sum(&[
                need_pressure_stress * multiplier,
                queue_wait_stress * multiplier,
                support_wait_stress * multiplier,
                environmental_stress * multiplier,
                -stress_recovery * multiplier,
                0.0 * multiplier,
            ]);
            return Some(needs(clamp(a.stress + (natural + forced)), None));
        }
        Model::LifeState | Model::Integrated => {}
    }

    // LifeStatePsychologyPolicy.explain_update and sleep_debt_step
    let g = body?;
    let life = &a.life;
    let family_pressure = if life.family_separated { g.discontent_stress } else { 0.0 };
    let effective_awake = match life.required_sleep {
        None => g.awake_sleep_debt,
        Some(0) => 0.0,
        // Both are small ints: their true division is this f64 division.
        Some(required) => py_min(10.0, required as f64 / (1440 - required).max(1) as f64),
    };
    let next_debt = match a.activity {
        Activity::Sleeping => life.debt - g.sleep_sleep_debt_recovery * a.rest_efficiency,
        Activity::Resting => life.debt - g.rest_sleep_debt_recovery * a.rest_efficiency,
        _ => life.debt + effective_awake,
    };
    let debt = py_min(1440.0, py_max(0.0, next_debt));
    let reassurance = py_max(0.0, life.reassurance - g.reassurance_decay);
    let uncertainty_without = py_min(
        1.0,
        py_max(
            0.0,
            life.uncertainty + life.query_pressure * g.uncertainty_drift
                - g.uncertainty_decay
                - reassurance * g.uncertainty_decay,
        ),
    );
    let uncertainty = py_min(
        1.0,
        py_max(
            0.0,
            life.uncertainty
                + (life.query_pressure + life.misinformation_pressure) * g.uncertainty_drift
                - g.uncertainty_decay
                - reassurance * g.uncertainty_decay,
        ),
    );
    let queue_pressure = if queued { g.discontent_queue } else { 0.0 };
    let need_pressure = py_max(py_max(py_max(a.toilet, a.hunger), a.fatigue), a.hydration);
    let resting = matches!(a.activity, Activity::Resting | Activity::Sleeping);
    let support_relief = if resting && !a.support_open { g.discontent_relief } else { 0.0 };
    let reference = if g.tick_start_reference { a.stress } else { simple_stress };
    let discontent = py_min(
        1.0,
        py_max(
            0.0,
            life.discontent + reference * g.discontent_stress + queue_pressure
                + life.privacy_discontent
                + py_max(0.0, need_pressure - 0.5) * g.discontent_stress
                - support_relief
                - reassurance * g.discontent_relief,
        ),
    );
    let positive = 0.5 + (life.positive - 0.5) * py_max(0.0, 1.0 - g.positive_affect_decay);
    let hygiene = py_min(
        1.0,
        py_max(
            0.0,
            life.hygiene + g.hygiene_growth + life.ventilation_growth
                + (1.0 - life.sanitation) * g.sanitation_hygiene_growth,
        ),
    );
    let negative = py_min(
        1.0,
        py_max(
            0.0,
            life.negative + reference * g.negative_affect_stress
                - reassurance * g.negative_affect_stress,
        ),
    );
    // BodyDynamicsFormula.stress_feedback, and the same with the shared rates without a formula.
    let feedback_with = |uncertainty: f64| -> f64 {
        let debt_component = debt / 1440.0 * life.sleep_rate;
        if g.sleep_debt_only {
            return debt_component;
        }
        debt_component + uncertainty * life.negative_rate + discontent * life.discontent_rate
            + hygiene * g.sanitation_stress
            + (1.0 - life.sanitation) * g.sanitation_stress
            + family_pressure
            - reassurance * life.negative_rate
            - positive * g.discontent_relief
    };
    let feedback = feedback_with(uncertainty);
    let body_values = Some([debt, uncertainty, reassurance, discontent, positive, negative, hygiene]);
    if model == Model::LifeState {
        return Some(needs(py_min(1.0, py_max(0.0, simple_stress + feedback)), body_values));
    }

    // IntegratedDistressLifeStatePolicy.explain_update
    let information_feedback = feedback - feedback_with(uncertainty_without);
    let life_feedback = feedback - information_feedback;
    let (natural, multiplier) = mean_reverting_terms(a.stress, a.baseline, a.kappa)?;
    let forced = py_sum(&[
        need_pressure_stress * multiplier,
        queue_wait_stress * multiplier,
        support_wait_stress * multiplier,
        environmental_stress * multiplier,
        life_feedback * multiplier,
        information_feedback * multiplier,
    ]);
    Some(needs(clamp(a.stress + (natural + forced)), body_values))
}

/// Whether `AgentStateDelta`'s checks pass these values. NaN fails the comparison.
#[inline]
fn within_bounds(next: &Next) -> bool {
    let unit = |value: f64| (0.0..=1.0).contains(&value);
    unit(next.toilet)
        && unit(next.hunger)
        && unit(next.fatigue)
        && unit(next.hydration)
        && unit(next.stress)
        && next.body.is_none_or(|body| {
            (0.0..=1440.0).contains(&body[0]) && body[1..].iter().copied().all(unit)
        })
}

/// A person's fields that only the view has.
struct ViewPerson<'py> {
    key: Bound<'py, PyString>,
    /// `group_id`, for the life-state models; an empty one is left to Python.
    group: Option<Arc<str>>,
    rest_efficiency: f64,
    environmental_per_minute: f64,
    toilet_growth: f64,
    hunger_growth: f64,
    awake_fatigue: f64,
    kappa: f64,
    baseline: f64,
    life: LifeInput,
}

fn read_life<'py>(
    agent: &ViewAgent,
    zone: &ViewZone,
    key: &Bound<'py, PyString>,
    tables: &RateTables<'py>,
    g: &BodyRates,
    rest_efficiency: f64,
) -> PyResult<LifeInput> {
    let required_sleep = match agent.person.required_sleep_minutes {
        None => None,
        Some(minutes) if (0..=1440).contains(&minutes) => Some(minutes),
        Some(_) => return unsupported("the required sleep is out of bounds"),
    };
    let life = LifeInput {
        debt: agent.body.sleep_debt_minutes,
        required_sleep,
        reassurance: agent.body.reassurance,
        uncertainty: agent.body.uncertainty,
        query_pressure: agent.information_query_pressure,
        misinformation_pressure: agent.misinformation_belief_pressure,
        discontent: agent.body.discontent,
        privacy_discontent: zone.privacy_discontent_per_minute,
        positive: agent.body.positive_affect,
        hygiene: agent.body.hygiene_need,
        ventilation_growth: zone.ventilation_hygiene_growth_per_minute,
        sanitation: zone.sanitation_level,
        negative: agent.body.negative_affect,
        family_separated: false,
        sleep_rate: match &tables.sleep_debt {
            Some(by_agent) => rate_of(by_agent, key, g.sleep_debt_stress)?,
            None => g.sleep_debt_stress,
        },
        negative_rate: match &tables.negative {
            Some(by_agent) => rate_of(by_agent, key, g.negative_affect_stress)?,
            None => g.negative_affect_stress,
        },
        discontent_rate: match &tables.discontent {
            Some(by_agent) => rate_of(by_agent, key, g.discontent_stress)?,
            None => g.discontent_stress,
        },
    };
    // The checks of sleep_debt_step on what the view holds.
    if !(life.debt.is_finite() && (0.0..=1440.0).contains(&life.debt))
        || !(0.0..=1.0).contains(&rest_efficiency)
    {
        return unsupported("the sleep debt or the sleep efficiency is out of bounds");
    }
    Ok(life)
}

fn read_view_people<'py>(
    rust_view: &Bound<'py, RustView>,
    tables: &RateTables<'py>,
    rates: &Rates,
    body: Option<&BodyRates>,
    model: Model,
) -> PyResult<Vec<ViewPerson<'py>>> {
    let py = rust_view.py();
    let rust_view = rust_view.get();
    let data = rust_view.data();
    let reversion = matches!(model, Model::MeanReverting | Model::Integrated);
    let mut found = Vec::with_capacity(data.agents.len());
    for (index, agent) in data.agents.iter().enumerate() {
        // An empty id fails the checks of AgentStateDelta.
        if agent.id.is_empty() {
            return unsupported("an id is empty");
        }
        let key = rust_view.key(py, index);
        let zone = data.zone_of(agent);
        let rest_efficiency = zone.rest_efficiency_multiplier;
        let (life, group) = match body {
            Some(g) => {
                // `group_id or ""` meets the people of an empty group.
                if agent.group_id.as_ref().is_some_and(|group| group.is_empty()) {
                    return unsupported("a group id is empty");
                }
                (read_life(agent, zone, key, tables, g, rest_efficiency)?, agent.group_id.clone())
            }
            None => (LifeInput::default(), None),
        };
        found.push(ViewPerson {
            group,
            rest_efficiency,
            environmental_per_minute: zone.environmental_stress_per_minute,
            toilet_growth: rate_of(&tables.toilet, key, rates.toilet_growth)?,
            hunger_growth: rate_of(&tables.hunger, key, rates.hunger_growth)?,
            awake_fatigue: rate_of(&tables.awake_fatigue, key, rates.awake_fatigue_growth)?,
            kappa: if reversion {
                rate_of(&tables.natural_recovery, key, rates.natural_recovery_rate)?
            } else {
                0.0
            },
            baseline: if reversion { rate_of(&tables.baseline, key, 0.0)? } else { 0.0 },
            life,
            key: key.clone(),
        });
    }
    Ok(found)
}

/// The groups with people in more than one zone. Everyone in such a group has a peer elsewhere,
/// and nobody in any other group does: the `any(...)` of `LifeStatePsychologyPolicy`.
fn separated_groups<'a>(
    people: &'a [ViewPerson<'_>],
    zones: &[Arc<str>],
) -> PyResult<FxHashMap<&'a str, bool>> {
    let mut first_zone: FxHashMap<&str, &Arc<str>> = FxHashMap::default();
    let mut groups: FxHashMap<&'a str, bool> = FxHashMap::default();
    for (person, zone) in people.iter().zip(zones) {
        if let Some(group) = &person.group {
            let group: &str = group;
            let spread = groups.entry(group).or_insert(false);
            match first_zone.get(group) {
                Some(first) => *spread |= **first != *zone,
                None => {
                    first_zone.insert(group, zone);
                }
            }
        }
    }
    Ok(groups)
}

static DELTA_TYPES: GILOnceCell<(Py<PyType>, Py<PyType>, Py<PyAny>)> = GILOnceCell::new();

/// `AgentStateDelta`, `PsychologyUpdateResult` and `object.__new__`.
fn delta_types(py: Python<'_>) -> PyResult<&(Py<PyType>, Py<PyType>, Py<PyAny>)> {
    DELTA_TYPES.get_or_try_init(py, || {
        let module = py.import("sheltersim.policies.psychology")?;
        let new = py.import("builtins")?.getattr("object")?.getattr("__new__")?;
        Ok((
            module.getattr("AgentStateDelta")?.downcast_into::<PyType>()?.unbind(),
            module.getattr("PsychologyUpdateResult")?.downcast_into::<PyType>()?.unbind(),
            new.unbind(),
        ))
    })
}

/// An `AgentStateDelta` of values known to pass its checks, as `AgentStateDelta.within_bounds`.
fn new_delta<'py>(
    types: &(Py<PyType>, Py<PyType>, Py<PyAny>),
    key: &Bound<'py, PyString>,
    minute: &Bound<'py, PyAny>,
    trust: f64,
    next: &Next,
) -> PyResult<Bound<'py, PyAny>> {
    let py = key.py();
    let fields = PyDict::new(py);
    let any = fields.as_any();
    let float = |value: f64| PyFloat::new(py, value).into_any();
    raw_dict_set(any, intern!(py, "agent_id").as_any(), key.as_any())?;
    raw_dict_set(any, intern!(py, "minute_index").as_any(), minute)?;
    raw_dict_set(any, intern!(py, "toilet_urgency").as_any(), &float(next.toilet))?;
    raw_dict_set(any, intern!(py, "hunger").as_any(), &float(next.hunger))?;
    raw_dict_set(any, intern!(py, "fatigue").as_any(), &float(next.fatigue))?;
    raw_dict_set(any, intern!(py, "hydration_need").as_any(), &float(next.hydration))?;
    raw_dict_set(any, intern!(py, "stress").as_any(), &float(next.stress))?;
    raw_dict_set(any, intern!(py, "trust").as_any(), &float(trust))?;
    let names = [
        intern!(py, "sleep_debt_minutes"),
        intern!(py, "uncertainty"),
        intern!(py, "reassurance"),
        intern!(py, "discontent"),
        intern!(py, "positive_affect"),
        intern!(py, "negative_affect"),
        intern!(py, "hygiene_need"),
    ];
    match &next.body {
        Some(body) => {
            for (name, value) in names.iter().zip(body) {
                raw_dict_set(any, name.as_any(), &float(*value))?;
            }
        }
        None => {
            let none = py.None().into_bound(py);
            for name in names {
                raw_dict_set(any, name.as_any(), &none)?;
            }
        }
    }
    let delta = types.2.bind(py).call1((types.0.bind(py),))?;
    raw_setattr(&delta, intern!(py, "__dict__"), fields.as_ptr())?;
    Ok(delta)
}

/// `_psychology_update` of the update phase, on a tick that records no psychology trace. A trace
/// stays with Python's `explain_update`: this kernel writes no trace document.
#[pyfunction]
fn update_psychology<'py>(
    controller: &Bound<'py, PyAny>,
    view: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = controller.py();
    let trace = controller.getattr(intern!(py, "artifacts"))?.getattr(intern!(py, "trace_level"))?;
    if trace.downcast::<PyString>()?.to_str()? != "off" {
        return unsupported("a trace is recorded");
    }
    let policy = controller.getattr(intern!(py, "psychology"))?;
    let model = model_of(&policy)?;
    let world = controller.getattr(intern!(py, "world"))?;
    if !minute.is_exact_instance_of::<PyInt>() || !minute.eq(view.getattr(intern!(py, "minute_index"))?)? {
        return unsupported("the minute is not the view's");
    }
    if minute.extract::<i64>().map_or(true, |minute| minute < 0) {
        return unsupported("the minute is negative or too large");
    }
    let parameters = policy.getattr(intern!(py, "parameters"))?;
    let rates = read_rates(&policy, &parameters, view, model)?;
    let life_model = matches!(model, Model::LifeState | Model::Integrated);
    let formula = if life_model { Some(policy.getattr(intern!(py, "body_formula"))?) } else { None };
    let config = if life_model { Some(policy.getattr(intern!(py, "body_config"))?) } else { None };
    let body = match (&config, &formula) {
        (Some(config), Some(formula)) => {
            if as_readable_dict(config).is_none() {
                return unsupported("the body config is not a plain dict");
            }
            Some(read_body_rates(&policy, config, formula)?)
        }
        _ => None,
    };
    let by_agent = |key: &Bound<'py, PyString>| -> PyResult<Option<Bound<'py, PyAny>>> {
        match (&config, &formula) {
            (Some(config), Some(formula)) if !formula.is_none() => match raw_dict_get(config, key.as_any())? {
                Some(table) => readable_dict(table).map(Some),
                None => Ok(Some(PyDict::new(py).into_any())),
            },
            _ => Ok(None),
        }
    };
    let tables = RateTables {
        toilet: readable_dict(parameters.getattr(intern!(py, "toilet_growth_by_agent"))?)?,
        hunger: readable_dict(parameters.getattr(intern!(py, "hunger_growth_by_agent"))?)?,
        awake_fatigue: readable_dict(parameters.getattr(intern!(py, "awake_fatigue_growth_by_agent"))?)?,
        natural_recovery: readable_dict(parameters.getattr(intern!(py, "natural_recovery_rate_by_agent"))?)?,
        baseline: readable_dict(parameters.getattr(intern!(py, "baseline_distress_by_agent"))?)?,
        sleep_debt: by_agent(intern!(py, "sleep_debt_stress_rate_by_agent"))?,
        negative: by_agent(intern!(py, "negative_affect_stress_rate_by_agent"))?,
        discontent: by_agent(intern!(py, "discontent_stress_rate_by_agent"))?,
    };
    let rust_view = view_of(view)?;
    let viewed = read_view_people(&rust_view, &tables, &rates, body.as_ref(), model)?;
    let mut ids = Vec::with_capacity(viewed.len());
    for person in &viewed {
        ids.push(person.key.to_str()?);
    }

    // What the view copied from the entities, from the rows. No Python runs in here.
    let store = entities_of(&world)?;
    let (mut inputs, trusts, zones) = EntityStore::with_rows(&store, |rows| {
        if rows.agents.len() != viewed.len() {
            return unsupported("the view and the world have different people");
        }
        let mut inputs = Vec::with_capacity(viewed.len());
        let mut trusts = Vec::with_capacity(viewed.len());
        let mut zones = Vec::with_capacity(if life_model { viewed.len() } else { 0 });
        for (person, id) in viewed.iter().zip(&ids) {
            let Some(row) = rows.agents.get(id) else {
                return unsupported("the view and the world have different people");
            };
            if &*row.profile.id != *id {
                return unsupported("a person's id differs from its key");
            }
            if !(0.0..=1.0).contains(&row.trust) {
                return unsupported("a trust is outside 0..1");
            }
            let reversion = matches!(model, Model::MeanReverting | Model::Integrated);
            // The checks of _mean_reverting_terms.
            if reversion
                && !((0.0..=1.0).contains(&row.stress)
                    && (0.0..=1.0).contains(&person.baseline)
                    && person.kappa.is_finite()
                    && person.kappa >= 0.0)
            {
                return unsupported("a value of the mean reversion is out of bounds");
            }
            // `request.status` of the view, which is None for an id with no request.
            let support_open = row.support_request_id.as_ref().is_some_and(|request| {
                rows.support_requests.get(request).is_some_and(|request| {
                    matches!(request.status, RequestStatus::Pending | RequestStatus::Assigned)
                })
            });
            inputs.push(Input {
                activity: row.activity,
                rest_efficiency: person.rest_efficiency,
                toilet: row.needs.toilet_urgency,
                hunger: row.needs.hunger,
                fatigue: row.needs.fatigue,
                hydration: row.needs.hydration_need,
                stress: row.stress,
                sensitivity: row.profile.stress_sensitivity,
                support_open,
                environmental_per_minute: person.environmental_per_minute,
                toilet_growth: person.toilet_growth,
                hunger_growth: person.hunger_growth,
                awake_fatigue: person.awake_fatigue,
                kappa: person.kappa,
                baseline: person.baseline,
                life: person.life,
            });
            trusts.push(row.trust);
            if life_model {
                zones.push(row.zone_id.clone());
            }
        }
        Ok((inputs, trusts, zones))
    })?;
    if life_model {
        let groups = separated_groups(&viewed, &zones)?;
        for (input, person) in inputs.iter_mut().zip(&viewed) {
            if let Some(group) = &person.group {
                input.life.family_separated = groups.get(&**group).copied().unwrap_or(false);
            }
        }
    }

    let types = delta_types(py)?;
    let mut deltas = Vec::with_capacity(inputs.len());
    for ((input, trust), person) in inputs.iter().zip(&trusts).zip(&viewed) {
        let Some(next) = step(model, &rates, body.as_ref(), input) else {
            return unsupported("a math function would raise");
        };
        // Values outside AgentStateDelta's bounds raise in its Python checks.
        if !within_bounds(&next) {
            return unsupported("a new value is out of bounds");
        }
        deltas.push(new_delta(types, &person.key, minute, *trust, &next)?);
    }
    types.1.bind(py).call1((PyTuple::new(py, deltas)?, PyTuple::empty(py)))
}

// ---- Writing the new values ---------------------------------------------------------------------

const NEEDS_MASK: u8 = 0b1111;
const STRESS_BIT: u8 = 1 << 4;
const TRUST_BIT: u8 = 1 << 5;

/// One checked delta. The values stay Python's objects, so writing them allocates nothing.
struct Applied<'py> {
    id: Bound<'py, PyString>,
    objects: [Bound<'py, PyAny>; 6],
    floats: [f64; 6],
    /// `body_state_v1.agents[id]` and what to write to it, if the delta has body values.
    record: Option<Bound<'py, PyAny>>,
    body: [Option<Bound<'py, PyAny>>; 7],
    changed: u8,
}

/// `body_state_v1.agents`, if it is a plain dict this kernel may write to with the C API.
fn plain_body_agents<'py>(world: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = world.py();
    let table = world.getattr(intern!(py, "model_state"))?;
    if as_readable_dict(&table).is_none() {
        return unsupported("model_state is not readable");
    }
    let Some(state) = raw_dict_get(&table, intern!(py, "body_state_v1").as_any())? else {
        return Ok(None);
    };
    if !state.is_exact_instance_of::<PyDict>() {
        return unsupported("body_state_v1 is not a plain dict");
    }
    let Some(agents) = raw_dict_get(&state, intern!(py, "agents").as_any())? else {
        return Ok(None);
    };
    if !agents.is_exact_instance_of::<PyDict>() {
        return unsupported("the body records are not a plain dict");
    }
    Ok(Some(agents))
}

/// `apply_state_deltas`. Everything is checked, by what Python's checks accept without their slow
/// path, before anything changes; anything else is Python's to accept or to refuse in its own
/// words. A value with the bits of the row is not written again.
#[pyfunction]
fn apply_deltas<'py>(
    _controller: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    deltas: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let Ok(deltas) = deltas.downcast_exact::<PyTuple>() else {
        return unsupported("the deltas are not a tuple");
    };
    let world_minute = world.getattr(intern!(py, "minute_index"))?;
    let delta_class = &delta_types(py)?.0;
    let body_agents = plain_body_agents(world)?;
    let names = [
        intern!(py, "toilet_urgency"),
        intern!(py, "hunger"),
        intern!(py, "fatigue"),
        intern!(py, "hydration_need"),
        intern!(py, "stress"),
        intern!(py, "trust"),
    ];
    let body_names = [
        intern!(py, "sleep_debt_minutes"),
        intern!(py, "uncertainty"),
        intern!(py, "reassurance"),
        intern!(py, "discontent"),
        intern!(py, "positive_affect"),
        intern!(py, "negative_affect"),
        intern!(py, "hygiene_need"),
    ];
    let mut plan = Vec::with_capacity(deltas.len());
    for delta in deltas.iter_borrowed() {
        if !delta.get_type().is(delta_class) {
            return unsupported("a delta is not an AgentStateDelta");
        }
        let fields = delta.getattr(intern!(py, "__dict__"))?;
        let item = |name: &Bound<'py, PyString>| -> PyResult<Bound<'py, PyAny>> {
            raw_dict_get(&fields, name.as_any())?
                .ok_or_else(|| crate::Unsupported::new_err("a field is missing"))
        };
        let id = item(intern!(py, "agent_id"))?;
        let Ok(id) = id.downcast_exact::<PyString>() else {
            return unsupported("an id is not a str");
        };
        if id.to_str().is_err() {
            return unsupported("an id is not UTF-8");
        }
        let minute = item(intern!(py, "minute_index"))?;
        if !minute.is_exact_instance_of::<PyInt>() || !minute.eq(&world_minute)? {
            return unsupported("a delta is not of this minute");
        }
        let mut floats = [0.0; 6];
        let objects = [
            item(names[0])?,
            item(names[1])?,
            item(names[2])?,
            item(names[3])?,
            item(names[4])?,
            item(names[5])?,
        ];
        for (place, value) in objects.iter().enumerate() {
            floats[place] = unit_float(value)?;
        }
        let mut body: [Option<Bound<'py, PyAny>>; 7] = Default::default();
        let mut supplied = false;
        for (place, name) in body_names.iter().enumerate() {
            let value = item(name)?;
            if !value.is_none() {
                if !value.is_exact_instance_of::<PyFloat>() {
                    return unsupported("a body value is not a float");
                }
                body[place] = Some(value);
                supplied = true;
            }
        }
        let record = if supplied {
            let Some(agents) = &body_agents else {
                return unsupported("the delta has body values and the world no body records");
            };
            let Some(record) = raw_dict_get(agents, id.as_any())? else {
                return unsupported("the delta has body values and the person no body record");
            };
            if !record.is_exact_instance_of::<PyDict>() {
                return unsupported("a body record is not a plain dict");
            }
            Some(record)
        } else {
            None
        };
        plan.push(Applied { id: id.clone(), objects, floats, record, body, changed: 0 });
    }
    let mut seen = FxHashSet::with_capacity_and_hasher(plan.len(), Default::default());
    for applied in &plan {
        if !seen.insert(applied.id.to_str()?) {
            return unsupported("two deltas for one person");
        }
    }
    drop(seen);
    let mut ids = Vec::with_capacity(plan.len());
    for applied in &plan {
        ids.push(applied.id.to_str()?.to_owned());
    }

    // Every person first: from here on nothing may be refused. No Python runs in here.
    EntityStore::with_rows(&entities_of(world)?, |rows| {
        for (applied, id) in plan.iter_mut().zip(&ids) {
            let Some(row) = rows.agents.get(id) else {
                return unsupported("a delta names nobody in the world");
            };
            let current = [
                row.needs.toilet_urgency,
                row.needs.hunger,
                row.needs.fatigue,
                row.needs.hydration_need,
                row.stress,
                row.trust,
            ];
            for (place, (now, next)) in current.iter().zip(&applied.floats).enumerate() {
                if now.to_bits() != next.to_bits() {
                    applied.changed |= 1 << place;
                }
            }
        }
        for (applied, id) in plan.iter().zip(&ids) {
            if applied.changed == 0 {
                continue;
            }
            let Some(row) = rows.agents.get_mut(id) else {
                return unsupported("a delta names nobody in the world");
            };
            row.needs.toilet_urgency = applied.floats[0];
            row.needs.hunger = applied.floats[1];
            row.needs.fatigue = applied.floats[2];
            row.needs.hydration_need = applied.floats[3];
            row.stress = applied.floats[4];
            row.trust = applied.floats[5];
        }
        Ok(())
    })?;

    let agents = world.getattr(intern!(py, "agents"))?;
    if as_readable_dict(&agents).is_none() {
        return Err(pyo3::exceptions::PyRuntimeError::new_err("the rows have no map of agents"));
    }
    for applied in &plan {
        if applied.changed != 0 {
            let Some(agent) = raw_dict_get(&agents, applied.id.as_any())? else {
                return Err(pyo3::exceptions::PyRuntimeError::new_err("a row has no object"));
            };
            if applied.changed & NEEDS_MASK != 0 {
                let needs = agent.getattr(intern!(py, "needs"))?;
                for place in 0..4 {
                    if applied.changed & (1 << place) != 0 {
                        raw_setattr(&needs, names[place], applied.objects[place].as_ptr())?;
                    }
                }
            }
            if applied.changed & STRESS_BIT != 0 {
                raw_setattr(&agent, names[4], applied.objects[4].as_ptr())?;
            }
            if applied.changed & TRUST_BIT != 0 {
                raw_setattr(&agent, names[5], applied.objects[5].as_ptr())?;
            }
        }
        if let Some(record) = &applied.record {
            for (name, value) in body_names.iter().zip(&applied.body) {
                if let Some(value) = value {
                    raw_dict_set(record, name.as_any(), value)?;
                }
            }
        }
    }
    Ok(())
}

// ---- For the bit comparisons of tests/integration/test_psychology_row.py -----------------------

/// `next_long_term_distress` of `domain/psychological_state.py`, from a record's fields.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn next_long_term_distress_of(
    baseline: f64,
    long_term: f64,
    daily_acute_sum: f64,
    daily_high_distress_minutes: i64,
    daily_observed_minutes: i64,
    retention: f64,
    acute_weight: f64,
    high_distress_weight: f64,
) -> f64 {
    let record = AgentRecord {
        baseline_distress: baseline,
        long_term_distress: long_term,
        daily_acute_sum,
        daily_high_distress_minutes,
        daily_observed_minutes,
        completed_days: 0,
    };
    let parameters = LongTermParameters {
        retention,
        acute_weight,
        high_distress_weight,
        high_distress_threshold: 1.0,
    };
    next_long_term_distress(&record, &parameters)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(update_psychology, m)?)?;
    m.add_function(wrap_pyfunction!(update_long_term, m)?)?;
    m.add_function(wrap_pyfunction!(apply_deltas, m)?)?;
    m.add_function(wrap_pyfunction!(next_long_term_distress_of, m)?)?;
    Ok(())
}
