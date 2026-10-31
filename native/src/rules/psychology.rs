//! The end of the update phase: the distress monitor (`domain/distress_monitor.py`) and the
//! long-term distress (`update_psychological_state` in `domain/psychological_state.py`).
//!
//! A kernel changes nothing until it knows the Python rules would pass: first `plan_*` computes
//! everything from the rows, and any value the Python rules would turn down, or that this kernel
//! does not handle, gives Unsupported, so the Python step runs and raises its own text. Then the plan is written to
//! the row and the tree, value by value, without replacing anything: the namespace is never
//! forwarded whole. Every float operation keeps the order of the Python expression.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyString, PyTuple};

use crate::core::mathx::{py_max, py_min};
use crate::core::row::{map_set, Field};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::{field_mirror_of, mirror_of, NamespaceMirror, Tree};
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
/// rules would do anything but read them: their keys differ, or a value is out of bounds.
fn people<'py>(view: &Bound<'py, PyAny>, deltas: &Bound<'py, PyAny>) -> PyResult<Vec<Person<'py>>> {
    let py = view.py();
    // A mapping proxy of the view's dict. Python's rule reads it with the same subscript.
    let view_agents = view.getattr(intern!(py, "agents"))?;
    let Ok(deltas) = deltas.downcast_exact::<PyTuple>() else {
        return unsupported("the deltas are not a tuple");
    };
    if deltas.len() != view_agents.len()? {
        return unsupported("the view and the deltas have different people");
    }
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
        let Ok(agent) = view_agents.get_item(id) else {
            return unsupported("the view and the deltas have different people");
        };
        found.push(Person {
            previous: unit_float(&agent.getattr(intern!(py, "stress"))?)?,
            current: unit_float(&delta.getattr(intern!(py, "stress"))?)?,
            id: id.clone(),
        });
    }
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
    view: &Bound<'py, PyAny>,
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
    let people = people(view, final_deltas)?;

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
    m.add_function(wrap_pyfunction!(update_long_term, m)?)?;
    m.add_function(wrap_pyfunction!(next_long_term_distress_of, m)?)?;
    Ok(())
}
