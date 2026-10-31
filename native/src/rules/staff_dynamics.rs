//! `domain/staff_dynamics.py`: staff shift, rest and fatigue. The Python rules stay as the
//! comparison base and for every value these kernels do not decide, which is also how a bad value
//! gets its error text (docs/rust-port/lanes/s-staff-dynamics.md).
//!
//! `update` plans everything from the rows first, so an Unsupported changes nothing and the Python
//! rule then raises at the record it would have reached. Every other kernel only reads, or builds
//! a new value.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyString};

use crate::core::mathx::{exp, py_max, py_min};
use crate::core::row::{
    attribute, exact_dict, exact_float, exact_int, exact_list, exact_str, namespace_is_absent,
    unsupported, Field,
};
use crate::core::world_core::{mirror_of, NamespaceMirror};
use crate::state::entities::{entities_of, EntityStore};
use crate::state::staff_dynamics::{
    shifts_are_valid, ConfigRow, LastActivity, Shift, StaffDynamicsRow, StaffRecord, MAX_REST_MINUTES,
    MINUTES_PER_DAY, MINUTES_PER_WEEK, NAME, POLICY_V1, POLICY_V2,
};

const SOURCE_V1: &str = "ASM-STAFF-DYNAMICS-V1";
const SOURCE_V2: &str = "ASM-STAFF-DYNAMICS-V2";
const LINEAR_FATIGUE: &str = "staff-fatigue-linear-v1";
const SATURATING_FATIGUE: &str = "staff-fatigue-saturating-v1";
const CONFIG_FIELDS: [&str; 8] = [
    "schema_version",
    "policy_version",
    "source_id",
    "max_continuous_work_minutes",
    "max_continuous_work_minutes_by_staff",
    "rest_minutes",
    "shift_windows_by_staff",
    "off_shift_fatigue_recovery_per_minute",
];
/// Above this an int does not always fit a float, and Python's int / int is correctly rounded.
const FLOAT_EXACT_INT: u64 = 1 << 53;

// ---- What the rules read from the rest of the world ------------------------------------------

/// `state["staff_status"]` of `operational_events_v1`, or None without that namespace: the input
/// of `staff_is_available` in `domain/operational.py`.
fn staff_status<'py>(world: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    let Some(state) = exact_dict(&table)?.get_item(intern!(py, "operational_events_v1"))? else {
        return Ok(None);
    };
    if state.is_none() {
        return Ok(None);
    }
    match exact_dict(&state)?.get_item(intern!(py, "staff_status"))? {
        Some(status) => {
            exact_dict(&status)?;
            Ok(Some(status))
        }
        None => unsupported("operational_events_v1 has no staff_status"),
    }
}

/// `staff_is_available`.
fn is_available(status: Option<&Bound<'_, PyAny>>, id: &Bound<'_, PyString>) -> PyResult<bool> {
    let Some(status) = status else {
        return Ok(true);
    };
    let py = id.py();
    match exact_dict(status)?.get_item(id)? {
        None => Ok(true),
        Some(record) if record.is_none() => Ok(true),
        Some(record) => match exact_dict(&record)?.get_item(intern!(py, "available"))? {
            Some(available) => match available.downcast_exact::<pyo3::types::PyBool>() {
                Ok(available) => Ok(available.is_true()),
                Err(_) => unsupported("available is not a bool"),
            },
            None => unsupported("a staff status has no available"),
        },
    }
}

/// `_on_shift`. A config of the oldest schema has no windows, and the Python rule raises.
fn on_shift(config: &ConfigRow, id: &str, minute: i64) -> PyResult<bool> {
    let Some(windows) = &config.shift_windows_by_staff.0 else {
        return unsupported("the config has no shift windows");
    };
    let Some(shifts) = windows.get(id) else {
        return Ok(true);
    };
    let week_minute = minute.rem_euclid(MINUTES_PER_WEEK);
    Ok(shifts
        .iter()
        .any(|shift| shift.start_minute <= week_minute && week_minute < shift.end_minute))
}

// ---- The fatigue formula -----------------------------------------------------------------------

struct Fatigue<'py> {
    saturating: bool,
    limits: Option<Bound<'py, PyDict>>,
}

/// `create_staff_fatigue_formula`, for the formulas this kernel knows. A later registered one, or
/// anything but plain documents, is the Python rule's.
fn fatigue_formula<'py>(world: &Bound<'py, PyAny>) -> PyResult<Fatigue<'py>> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let configs = exact_dict(&configs)?;
    let selected = match configs.get_item(intern!(py, "policy_registry"))? {
        Some(registry) => exact_dict(&registry)?.get_item(intern!(py, "staff_fatigue"))?,
        None => None,
    };
    let saturating = match &selected {
        Some(selected) => match exact_str(selected)? {
            LINEAR_FATIGUE => false,
            SATURATING_FATIGUE => true,
            _ => return unsupported("another fatigue formula"),
        },
        None => false,
    };
    let limits = match configs.get_item(intern!(py, "staff_dynamics"))? {
        Some(config) => {
            match exact_dict(&config)?.get_item(intern!(py, "max_continuous_work_minutes_by_staff"))? {
                Some(limits) => Some(exact_dict(&limits)?.clone()),
                None => None,
            }
        }
        None => None,
    };
    Ok(Fatigue { saturating, limits: limits.filter(|limits| !limits.is_empty()) })
}

/// `StaffFatigueFormula.fatigue`, or None where Python's result isn't sure to be f64 division.
#[inline]
fn fatigue_of(saturating: bool, consecutive: i64, maximum: i64) -> Option<f64> {
    if maximum == 0
        || consecutive.unsigned_abs() > FLOAT_EXACT_INT
        || maximum.unsigned_abs() > FLOAT_EXACT_INT
    {
        return None;
    }
    let fraction = consecutive as f64 / maximum as f64;
    if saturating {
        return Some(1.0 - exp(-fraction)?);
    }
    Some(py_min(1.0, fraction))
}

// ---- update_staff_dynamics ---------------------------------------------------------------------

/// What one tick needs to know of one member, read before the row is.
struct Member<'py> {
    id: Bound<'py, PyString>,
    working: bool,
    /// `staff_is_available`; only asked of who has no task.
    available: bool,
    maximum: Option<i64>,
}

struct Parameters {
    saturating: bool,
    default_maximum: i64,
    rest_minutes: i64,
    recovery: Option<f64>,
    duration: i64,
    minute: i64,
}

/// One member's next record: the loop body of `update_staff_dynamics`. None when an int leaves
/// what the kernel holds or a config key the Python rule reads is not there.
fn next_record(
    record: &StaffRecord,
    working: bool,
    available_on_shift: bool,
    maximum: i64,
    parameters: &Parameters,
) -> Option<StaffRecord> {
    let mut next = *record;
    if working {
        next.consecutive_work_minutes = next.consecutive_work_minutes.checked_add(1)?;
        next.total_work_minutes = next.total_work_minutes.checked_add(1)?;
        next.fatigue = fatigue_of(parameters.saturating, next.consecutive_work_minutes, maximum)?;
        next.last_activity = LastActivity::Working;
        return Some(next);
    }
    if !available_on_shift {
        next.last_activity = LastActivity::OffDuty;
        next.consecutive_work_minutes = 0;
        next.total_rest_minutes = next.total_rest_minutes.checked_add(1)?;
        next.fatigue = py_max(0.0, next.fatigue - parameters.recovery?);
        return Some(next);
    }
    if parameters.minute < next.rest_until_minute {
        next.total_rest_minutes = next.total_rest_minutes.checked_add(1)?;
        next.last_activity = LastActivity::Resting;
        return Some(next);
    }
    if next.rest_until_minute != 0 {
        next.consecutive_work_minutes = 0;
        next.fatigue = 0.0;
        next.rest_until_minute = 0;
    }
    if next.consecutive_work_minutes >= maximum {
        next.rest_until_minute =
            parameters.duration.min(parameters.minute.checked_add(parameters.rest_minutes)?);
        next.last_activity = LastActivity::Resting;
    } else {
        next.last_activity = LastActivity::OffDuty;
    }
    Some(next)
}

fn plan(
    row: &StaffDynamicsRow,
    members: &[Member<'_>],
    parameters: &Parameters,
) -> PyResult<Vec<StaffRecord>> {
    let mut planned = Vec::with_capacity(members.len());
    for member in members {
        let id = member.id.to_str()?;
        let Some(record) = row.staff.get(id) else {
            return unsupported("a member has no record");
        };
        let maximum = member.maximum.unwrap_or(parameters.default_maximum);
        // In the Python rule's order: _on_shift is only asked of who has no task and is available.
        let available_on_shift =
            !member.working && member.available && on_shift(&row.config, id, parameters.minute)?;
        let Some(next) = next_record(record, member.working, available_on_shift, maximum, parameters)
        else {
            return unsupported("a value leaves what this kernel holds");
        };
        planned.push(next);
    }
    Ok(planned)
}

/// `update_staff_dynamics`.
#[pyfunction]
fn update(world: &Bound<'_, PyAny>, minute: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(());
    }
    let minute = exact_int(minute)?;
    let duration = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
    let mirror = mirror_of(world, NAME)?;
    let formula = fatigue_formula(world)?;
    let status = staff_status(world)?;

    let store = entities_of(world)?;
    let staff: Vec<(Arc<str>, bool)> = EntityStore::with_rows(&store, |rows| {
        Ok(rows
            .staff
            .iter()
            .map(|(id, row)| (id.clone(), row.active_task_id.is_some()))
            .collect())
    })?;
    let mut members = Vec::with_capacity(staff.len());
    for (id, working) in staff {
        let id = PyString::new(py, &id);
        let maximum = match &formula.limits {
            Some(limits) => limits.get_item(&id)?.map(|limit| exact_int(&limit)).transpose()?,
            None => None,
        };
        let available = working || is_available(status.as_ref(), &id)?;
        members.push(Member { id, working, available, maximum });
    }

    let planned = NamespaceMirror::with_row::<StaffDynamicsRow, _>(&mirror, |row| {
        let parameters = Parameters {
            saturating: formula.saturating,
            default_maximum: row.config.max_continuous_work_minutes,
            rest_minutes: row.config.rest_minutes,
            recovery: row.config.off_shift_fatigue_recovery_per_minute.0,
            duration,
            minute,
        };
        plan(row, &members, &parameters)
    })?;
    NamespaceMirror::with_row_tree::<StaffDynamicsRow, _>(&mirror, |row, tree| {
        let staff = tree.child(intern!(py, "staff"))?;
        for (member, next) in members.iter().zip(&planned) {
            let id = member.id.to_str()?;
            let Some(record) = row.staff.get_mut(id) else {
                return unsupported("the row changed under the plan");
            };
            let node = staff.child(&member.id)?;
            record.set_all(&node, next)?;
        }
        Ok(())
    })
}

// ---- The readers --------------------------------------------------------------------------------

/// `staff_dynamics_on_shift`.
#[pyfunction(name = "on_shift")]
fn on_shift_now(
    world: &Bound<'_, PyAny>,
    staff_id: &Bound<'_, PyAny>,
    minute: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    if namespace_is_absent(world, intern!(world.py(), NAME))? {
        return Ok(true);
    }
    let id = exact_str(staff_id)?;
    let minute = exact_int(minute)?;
    let mirror = mirror_of(world, NAME)?;
    NamespaceMirror::with_row::<StaffDynamicsRow, _>(&mirror, |row| {
        let Some(record) = row.staff.get(id) else {
            return Ok(false);
        };
        Ok(on_shift(&row.config, id, minute)? && minute >= record.rest_until_minute)
    })
}

/// `operation is None or operation.get("phase") == "idle"` for staff_operations_v1 as the Python
/// rule reads it, which is before anything else, so a bad document always goes to it.
fn operation_is_idle(world: &Bound<'_, PyAny>, id: &Bound<'_, PyString>) -> PyResult<bool> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    let Some(operations) = exact_dict(&table)?.get_item(intern!(py, "staff_operations_v1"))? else {
        return Ok(true);
    };
    if operations.is_none() {
        return Ok(true);
    }
    let Some(staff) = exact_dict(&operations)?.get_item(intern!(py, "staff"))? else {
        return Ok(true);
    };
    let Some(operation) = exact_dict(&staff)?.get_item(id)? else {
        return Ok(true);
    };
    if operation.is_none() {
        return Ok(true);
    }
    match exact_dict(&operation)?.get_item(intern!(py, "phase"))? {
        Some(phase) if !phase.is_none() => Ok(exact_str(&phase)? == "idle"),
        _ => Ok(false),
    }
}

/// `staff_dynamics_is_available`.
#[pyfunction(name = "is_available")]
fn is_available_now(
    world: &Bound<'_, PyAny>,
    staff_id: &Bound<'_, PyAny>,
    minute: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    if namespace_is_absent(world, intern!(world.py(), NAME))? {
        return Ok(true);
    }
    let Ok(id) = staff_id.downcast_exact::<PyString>() else {
        return unsupported("the id is not a str");
    };
    let text = exact_str(staff_id)?;
    let minute = exact_int(minute)?;
    let idle = operation_is_idle(world, id)?;
    let mirror = mirror_of(world, NAME)?;
    let eligible = NamespaceMirror::with_row::<StaffDynamicsRow, _>(&mirror, |row| {
        let Some(record) = row.staff.get(text) else {
            return Ok(false);
        };
        Ok(on_shift(&row.config, text, minute)? && minute >= record.rest_until_minute)
    })?;
    if !eligible {
        return Ok(false);
    }
    let status = staff_status(world)?;
    Ok(is_available(status.as_ref(), id)? && idle)
}

// ---- add_staff, and what a new world starts from ---------------------------------------------------

/// `add_staff`: one record, forwarded neither way.
#[pyfunction]
fn add_staff(world: &Bound<'_, PyAny>, staff_id: &Bound<'_, PyAny>) -> PyResult<()> {
    if namespace_is_absent(world, intern!(world.py(), NAME))? {
        return Ok(());
    }
    let id = exact_str(staff_id)?;
    let mirror = mirror_of(world, NAME)?;
    let record = StaffRecord::new();
    NamespaceMirror::with_row::<StaffDynamicsRow, _>(&mirror, |row| {
        match row.staff.get_mut(id) {
            Some(slot) => *slot = record,
            None => {
                row.staff.insert(Arc::from(id), record);
            }
        }
        Ok(())
    })?;
    NamespaceMirror::write_through(&mirror, &["staff"], id, &Field::to_py(&record, world.py())?)
}

/// An exact non-empty str key of a config map.
fn staff_key<'a>(key: &'a Bound<'_, PyAny>) -> PyResult<&'a str> {
    let id = exact_str(key)?;
    if id.is_empty() {
        return unsupported("an empty staff id");
    }
    Ok(id)
}

fn shift_of(row: &Bound<'_, PyAny>) -> PyResult<Shift> {
    let py = row.py();
    let row = exact_dict(row)?;
    let (Some(start), Some(end), 2) = (
        row.get_item(intern!(py, "start_minute"))?,
        row.get_item(intern!(py, "end_minute"))?,
        row.len(),
    ) else {
        return unsupported("a shift has other keys");
    };
    Ok(Shift { start_minute: exact_int(&start)?, end_minute: exact_int(&end)? })
}

fn shifts_of(rows: &Bound<'_, PyList>) -> PyResult<Vec<Shift>> {
    let shifts = rows.iter().map(|row| shift_of(&row)).collect::<PyResult<Vec<Shift>>>()?;
    if !shifts_are_valid(&shifts) {
        return unsupported("the shifts of a person are not valid");
    }
    Ok(shifts)
}

/// `canonical_staff_dynamics_config`: the new dict, or Unsupported for the Python rule to raise
/// or to take a value of another type that it accepts, such as `schema_version: 1.0`.
fn canonical_config<'py>(raw: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let py = raw.py();
    let empty;
    let raw = if raw.is_none() {
        empty = PyDict::new(py);
        &empty
    } else {
        exact_dict(raw)?
    };
    for (key, _) in raw.iter() {
        if !CONFIG_FIELDS.contains(&exact_str(&key)?) {
            return unsupported("the config has other fields");
        }
    }
    let windows = raw.get_item(intern!(py, "shift_windows_by_staff"))?;
    let recovery = raw.get_item(intern!(py, "off_shift_fatigue_recovery_per_minute"))?;
    let legacy = match raw.get_item(intern!(py, "policy_version"))? {
        Some(version) => match exact_str(&version)? {
            POLICY_V1 => true,
            POLICY_V2 => false,
            _ => return unsupported("another policy"),
        },
        None => windows.is_none() && recovery.is_none(),
    };
    if let Some(schema) = raw.get_item(intern!(py, "schema_version"))? {
        if exact_int(&schema)? != 1 {
            return unsupported("another schema");
        }
    }
    let source = raw.get_item(intern!(py, "source_id"))?;
    if let Some(source) = &source {
        if exact_str(source)?.is_empty() {
            return unsupported("an empty source");
        }
    }

    let integer_in = |name: &Bound<'py, PyString>, default: i64, upper: i64| -> PyResult<()> {
        let value = match raw.get_item(name)? {
            Some(value) => exact_int(&value)?,
            None => default,
        };
        if !(1..=upper).contains(&value) {
            return unsupported("a limit is outside its range");
        }
        Ok(())
    };
    integer_in(intern!(py, "max_continuous_work_minutes"), 240, MINUTES_PER_DAY)?;
    integer_in(intern!(py, "rest_minutes"), 30, MAX_REST_MINUTES)?;
    if let Some(limits) = raw.get_item(intern!(py, "max_continuous_work_minutes_by_staff"))? {
        for (id, limit) in exact_dict(&limits)?.iter() {
            staff_key(&id)?;
            if !(1..=MINUTES_PER_DAY).contains(&exact_int(&limit)?) {
                return unsupported("a limit is outside its range");
            }
        }
    }
    let mut any_windows = false;
    if let Some(windows) = &windows {
        for (id, rows) in exact_dict(windows)?.iter() {
            staff_key(&id)?;
            shifts_of(exact_list(&rows)?)?;
            any_windows = true;
        }
    }
    let recovery_value = match &recovery {
        Some(recovery) => exact_float(recovery)?,
        None => {
            if legacy {
                0.0
            } else {
                0.005
            }
        }
    };
    if !(0.0..=1.0).contains(&recovery_value) {
        return unsupported("the recovery is outside 0..1");
    }
    let policy = if legacy { POLICY_V1 } else { POLICY_V2 };
    if policy == POLICY_V1 && (any_windows || recovery_value != 0.0) {
        return unsupported("policy v1 has no shifts or recovery");
    }

    // {defaults, **raw}: every key of raw is one of the defaults, so this is their order.
    let config = PyDict::new(py);
    let take = |name: &Bound<'py, PyString>, default: Bound<'py, PyAny>| -> PyResult<()> {
        match raw.get_item(name)? {
            Some(value) => config.set_item(name, value),
            None => config.set_item(name, default),
        }
    };
    take(intern!(py, "schema_version"), 1_i64.into_pyobject(py)?.into_any())?;
    take(intern!(py, "policy_version"), PyString::new(py, policy).into_any())?;
    take(
        intern!(py, "source_id"),
        PyString::new(py, if legacy { SOURCE_V1 } else { SOURCE_V2 }).into_any(),
    )?;
    take(intern!(py, "max_continuous_work_minutes"), 240_i64.into_pyobject(py)?.into_any())?;
    take(intern!(py, "max_continuous_work_minutes_by_staff"), PyDict::new(py).into_any())?;
    take(intern!(py, "rest_minutes"), 30_i64.into_pyobject(py)?.into_any())?;
    take(intern!(py, "shift_windows_by_staff"), PyDict::new(py).into_any())?;
    take(
        intern!(py, "off_shift_fatigue_recovery_per_minute"),
        pyo3::types::PyFloat::new(py, recovery_value).into_any(),
    )?;
    Ok(config)
}

#[pyfunction(name = "canonical_config")]
fn canonical_config_of<'py>(raw: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    canonical_config(raw)
}

/// `create_staff_dynamics_state`.
#[pyfunction]
fn create_state<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let py = world.py();
    let configs = attribute(world, intern!(py, "model_configs"))?;
    let none = py.None().into_bound(py);
    let raw = exact_dict(&configs)?.get_item(intern!(py, "staff_dynamics"))?.unwrap_or(none);
    let config = canonical_config(&raw)?;
    let limits_key = intern!(py, "max_continuous_work_minutes_by_staff");
    let policy = config.get_item(intern!(py, "policy_version"))?;
    let source = config.get_item(intern!(py, "source_id"))?;
    if config.get_item(limits_key)?.is_some_and(|limits| limits.is_empty().unwrap_or(false)) {
        config.del_item(limits_key)?;
    }
    let world_staff = attribute(world, intern!(py, "staff"))?;
    let mut ids: Vec<Bound<'py, PyAny>> = exact_dict(&world_staff)?.keys().iter().collect();
    for id in &ids {
        exact_str(id)?;
    }
    ids.sort_unstable_by(|a, b| {
        let (first, second) = (exact_str(a).unwrap_or(""), exact_str(b).unwrap_or(""));
        first.as_bytes().cmp(second.as_bytes())
    });
    let staff = PyDict::new(py);
    let record = StaffRecord::new();
    for id in ids {
        staff.set_item(id, Field::to_py(&record, py)?)?;
    }
    let state = PyDict::new(py);
    state.set_item(intern!(py, "schema_version"), 1_i64)?;
    state.set_item(intern!(py, "policy_version"), policy)?;
    state.set_item(intern!(py, "source_id"), source)?;
    state.set_item(intern!(py, "config"), config)?;
    state.set_item(intern!(py, "staff"), staff)?;
    Ok(state)
}

/// `re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", id)`.
fn is_override_id(id: &str) -> bool {
    let bytes = id.as_bytes();
    (1..=64).contains(&bytes.len())
        && bytes[0].is_ascii_alphanumeric()
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

/// The length of `json.dumps(value, separators=(",", ":"))` once every ID is `is_override_id`, so
/// nothing needs an escape, and every row has its two keys.
fn override_json_length(rows_by_id: &[(&str, Vec<Shift>, &Bound<'_, PyAny>)]) -> usize {
    let digits = |value: i64| itoa::Buffer::new().format(value).len();
    let mut total = 2 + rows_by_id.len().saturating_sub(1);
    for (id, shifts, _) in rows_by_id {
        total += id.len() + 3 + 2 + shifts.len().saturating_sub(1);
        for shift in shifts {
            total += r#"{"start_minute":,"end_minute":}"#.len()
                + digits(shift.start_minute)
                + digits(shift.end_minute);
        }
    }
    total
}

/// `validate_staff_shift_windows_override`.
#[pyfunction]
#[pyo3(signature = (value, *, known_staff_ids=None))]
fn validate_override<'py>(
    value: &Bound<'py, PyAny>,
    known_staff_ids: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyDict>> {
    let py = value.py();
    let value = exact_dict(value)?;
    if !(1..=100).contains(&value.len()) {
        return unsupported("the number of staff IDs is out of range");
    }
    let known = match known_staff_ids {
        Some(known) if !known.is_none() => match known.downcast_exact::<PySet>() {
            Ok(known) => Some(known.clone()),
            Err(_) => return unsupported("the known IDs are not a set"),
        },
        _ => None,
    };
    let entries: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)> = value.iter().collect();
    let mut parsed: Vec<(&str, Vec<Shift>, &Bound<'py, PyAny>)> = Vec::with_capacity(entries.len());
    for (id, rows) in &entries {
        let text = exact_str(id)?;
        if !is_override_id(text) {
            return unsupported("an invalid staff ID");
        }
        if let Some(known) = &known {
            if !known.contains(id)? {
                return unsupported("an unknown staff ID");
            }
        }
        parsed.push((text, shifts_of(exact_list(rows)?)?, rows));
    }
    if override_json_length(&parsed) > 16_384 {
        return unsupported("the override exceeds the limit");
    }
    parsed.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let normalized = PyDict::new(py);
    for (id, shifts, rows) in &parsed {
        let mut order: Vec<usize> = (0..shifts.len()).collect();
        order.sort_by_key(|index| (shifts[*index].start_minute, shifts[*index].end_minute));
        let rows = exact_list(rows)?;
        let sorted = PyList::empty(py);
        for index in order {
            // dict(row): a plain copy, its keys in the row's order.
            let row = rows.get_item(index)?;
            sorted.append(exact_dict(&row)?.copy()?)?;
        }
        normalized.set_item(*id, sorted)?;
    }
    Ok(normalized)
}

/// The fatigue formula alone, for the bit comparison of the tests.
#[pyfunction]
fn fatigue_value(saturating: bool, consecutive: i64, maximum: i64) -> PyResult<f64> {
    fatigue_of(saturating, consecutive, maximum)
        .ok_or_else(|| crate::Unsupported::new_err("the formula's inputs are out of range"))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(update, m)?)?;
    m.add_function(wrap_pyfunction!(on_shift_now, m)?)?;
    m.add_function(wrap_pyfunction!(is_available_now, m)?)?;
    m.add_function(wrap_pyfunction!(add_staff, m)?)?;
    m.add_function(wrap_pyfunction!(canonical_config_of, m)?)?;
    m.add_function(wrap_pyfunction!(create_state, m)?)?;
    m.add_function(wrap_pyfunction!(validate_override, m)?)?;
    m.add_function(wrap_pyfunction!(fatigue_value, m)?)?;
    Ok(())
}
