//! `staff_operations_v1`: the sanitation of each zone, the routed task of each staff member and the
//! finished tasks (`domain/staff_operations.py`). The rules that run every tick are
//! `rules/staff.rs`. The rules of a task's events write one value or one record at a time through
//! the forwarded writes (docs/rust-port/lanes/m4-staff.md).
//!
//! A value of another type than the rules write taints the namespace, and the Python rules and
//! their errors run on the tree: an int `sanitation`, which validation accepts, only costs the row.

use std::collections::HashSet;
use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyString;

use crate::core::row::{exact_dict, load_row, namespace_is_absent, state_row, str_enum, IdMap};
use crate::core::value::V;
use crate::core::world_core::{mirror_of, NamespaceMirror, RowType};
use crate::state::entities::{entities_of, Entities, EntityStore};

pub(crate) const NAME: &str = "staff_operations_v1";
const POLICY: &str = "autonomous-routed-staff-v1";
const SOURCE: &str = "ASM-STAFF-OPERATIONS-V1";
const MAX_HISTORY: usize = 20_000;
const TASK_KINDS: [&str; 5] = ["cleaning", "maintenance", "information_outreach", "intake", "patrol"];
const THRESHOLDS: [&str; 3] =
    ["cleaning_sanitation_below", "outreach_uncertainty_at_least", "patrol_stress_at_least"];
/// A global rate of the config and the upper bound of the same rate in a zone.
const EFFECTS: [(&str, f64); 4] = [
    ("sanitation_use_per_person_minute", 0.01),
    ("cleaning_sanitation_recovery", 1.0),
    ("outreach_uncertainty_relief", 1.0),
    ("patrol_reassurance", 1.0),
];
const MAX_TASK_MINUTES: i64 = 24 * 60;

str_enum! {
    pub(crate) enum Phase { Idle = "idle", Travel = "travel", Working = "working" }
}

str_enum! {
    pub(crate) enum Outcome { Completed = "completed", Blocked = "blocked", Interrupted = "interrupted" }
}

state_row! {
    #[derive(Clone, Copy)]
    pub(crate) struct ZoneRecord {
        sanitation: f64 => set_sanitation,
        updated_minute: i64 => set_updated_minute,
    }
}

state_row! {
    pub(crate) struct OperationRecord {
        phase: Phase,
        task_id: Option<Arc<str>>,
        task_kind: Option<Arc<str>>,
        target_id: Option<Arc<str>>,
        target_zone_id: Option<Arc<str>>,
        reason: Option<Arc<str>>,
        started_minute: Option<i64>,
        travel_due_minute: Option<i64>,
        arrival_minute: Option<i64>,
        due_minute: Option<i64>,
        route_zone_ids: Vec<Arc<str>>,
        route_edge_ids: Vec<Arc<str>>,
        travel_minutes: i64,
        causation_event_ids: Vec<Arc<str>>,
    }
}

state_row! {
    pub(crate) struct FinishedTask {
        task_id: Arc<str>,
        staff_id: Arc<str>,
        task_kind: Arc<str>,
        target_id: Option<Arc<str>>,
        target_zone_id: Arc<str>,
        reason: Arc<str>,
        status: Outcome,
        start_minute: i64,
        arrival_minute: Option<i64>,
        complete_minute: i64,
        travel_minutes: i64,
        causation_event_ids: Vec<Arc<str>>,
    }
}

state_row! {
    pub(crate) struct StaffOperationsRow {
        schema_version: i64,
        policy_version: Arc<str>,
        source_id: Arc<str>,
        config: V,
        zones: IdMap<ZoneRecord>,
        staff: IdMap<OperationRecord>,
        history: Vec<FinishedTask>,
    }
}

impl OperationRecord {
    /// `record == _idle_record()`, when `phase` is idle.
    fn is_the_idle_record(&self) -> bool {
        self.task_id.is_none()
            && self.task_kind.is_none()
            && self.target_id.is_none()
            && self.target_zone_id.is_none()
            && self.reason.is_none()
            && self.started_minute.is_none()
            && self.travel_due_minute.is_none()
            && self.arrival_minute.is_none()
            && self.due_minute.is_none()
            && self.route_zone_ids.is_empty()
            && self.route_edge_ids.is_empty()
            && self.travel_minutes == 0
            && self.causation_event_ids.is_empty()
    }
}

impl ZoneRecord {
    fn is_valid(&self) -> bool {
        (0.0..=1.0).contains(&self.sanitation) && self.updated_minute >= 0
    }
}

impl FinishedTask {
    fn is_valid(&self, rows: &Entities, zones: &HashSet<String>) -> bool {
        let times = self.start_minute >= 0
            && self.complete_minute >= self.start_minute
            && self.travel_minutes >= 0
            && match self.arrival_minute {
                None => self.status == Outcome::Interrupted && self.travel_minutes > 0,
                Some(arrival) => self.start_minute <= arrival && arrival <= self.complete_minute,
            };
        times
            && rows.staff.get(&self.staff_id).is_some()
            && TASK_KINDS.contains(&&*self.task_kind)
            && zones.contains(&*self.target_zone_id)
            && self.target_id.as_deref().is_none_or(|id| rows.facilities.get(id).is_some())
            && self.causation_event_ids.iter().all(|id| !id.is_empty())
    }
}

fn number_in(value: &V, lower: f64, upper: f64) -> bool {
    match value {
        // The bounds are small, so an int compares as its float does.
        V::Int(number) => lower <= *number as f64 && *number as f64 <= upper,
        V::Float(number) => lower <= *number && *number <= upper,
        _ => false,
    }
}

/// The per-zone table of the config: each key a non-empty zone with a non-empty entry that passes.
fn zone_table_is_valid(table: &V, zones: &HashSet<String>, entry_is_valid: impl Fn(&str, &V) -> bool) -> bool {
    let V::Map(table) = table else {
        return false;
    };
    table.iter().all(|(zone, entry)| {
        let V::Map(entry) = entry else {
            return false;
        };
        !zone.is_empty()
            && zones.contains(&**zone)
            && !entry.is_empty()
            && entry.iter().all(|(name, value)| entry_is_valid(name, value))
    })
}

/// Whether `canonical_staff_operations_config` passes this config and leaves it as it is, or
/// differs only by the additive tables an older config doesn't have, and its tables name known zones.
fn config_is_canonical(config: &V, zones: &HashSet<String>) -> bool {
    let V::Map(config) = config else {
        return false;
    };
    if config.len() > 10 {
        return false;
    }
    let text = |name: &str, expected: &str| matches!(config.get(name), Some(V::Str(text)) if &**text == expected);
    let durations_are_valid = matches!(config.get("task_duration_minutes"), Some(V::Map(durations))
        if durations.len() == TASK_KINDS.len()
            && TASK_KINDS.iter().all(|kind| {
                matches!(durations.get(*kind), Some(V::Int(minutes)) if (1..=MAX_TASK_MINUTES).contains(minutes))
            }));
    let mut known = 8;
    if !(matches!(config.get("schema_version"), Some(V::Int(1)))
        && text("policy_version", POLICY)
        && text("source_id", SOURCE)
        && durations_are_valid
        && EFFECTS.iter().all(|(name, _)| config.get(*name).is_some_and(|value| number_in(value, 0.0, 1.0))))
    {
        return false;
    }
    if let Some(table) = config.get("thresholds_by_zone") {
        known += 1;
        if !zone_table_is_valid(table, zones, |name, value| {
            THRESHOLDS.contains(&name) && number_in(value, 0.0, 1.0)
        }) {
            return false;
        }
    }
    if let Some(table) = config.get("effects_by_zone") {
        known += 1;
        if !zone_table_is_valid(table, zones, |name, value| {
            EFFECTS.iter().any(|(effect, upper)| *effect == name && number_in(value, 0.0, *upper))
        }) {
            return false;
        }
    }
    config.len() == known
}

/// `validate_staff_operations_state`, for a world where no staff member has a task: accepts, or
/// leaves it to the Python rules and their error text. An active record's rules read the pending
/// events, and a Python-scheduled event has no row until a rule reads it, so those are Python's.
#[pyfunction]
fn validate(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(true);
    }
    let zones_of_world = world.getattr(intern!(py, "zones"))?;
    let zones_of_world = exact_dict(&zones_of_world)?;
    let mut zones = HashSet::with_capacity(zones_of_world.len());
    for key in zones_of_world.keys() {
        let Ok(key) = key.downcast_exact::<PyString>().map(|key| key.to_string()) else {
            return Ok(false);
        };
        zones.insert(key);
    }
    let mirror = mirror_of(world, NAME)?;
    let store = entities_of(world)?;
    NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror, |row| {
        if row.schema_version != 1
            || &*row.policy_version != POLICY
            || &*row.source_id != SOURCE
            || row.history.len() > MAX_HISTORY
            || row.zones.len() != zones.len()
            || !row.zones.iter().all(|(id, zone)| zones.contains(&**id) && zone.is_valid())
            || !config_is_canonical(&row.config, &zones)
            || !row.staff.values().all(|record| record.phase == Phase::Idle && record.is_the_idle_record())
        {
            return Ok(false);
        }
        EntityStore::with_rows(&store, |rows| {
            Ok(row.staff.len() == rows.staff.len()
                && row.staff.keys().all(|id| rows.staff.get(id).is_some())
                && row.history.iter().all(|item| item.is_valid(rows, &zones)))
        })
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("staff_operations", load_row::<StaffOperationsRow>))?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    Ok(())
}
