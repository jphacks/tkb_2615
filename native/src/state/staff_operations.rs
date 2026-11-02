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

use crate::core::row::{attribute, exact_dict, exact_int, load_row, namespace_is_absent, state_row, str_enum, unsupported, IdMap};
use crate::core::tracked::as_readable_list;
use crate::core::value::V;
use crate::core::world_core::{mirror_of, NamespaceMirror, RowType};
use crate::state::entities::{entities_of, Entities, EntityStore};
use crate::state::events::{EventWriter, PendingEvent, PendingSource};
use crate::Unsupported;

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

/// What an active record says, read out of the row so that the objects can be read after it.
struct Active {
    staff_id: String,
    working: bool,
    task_id: String,
    task_kind: String,
    target_id: Option<String>,
    target_zone_id: String,
    started: i64,
    travel_due: Option<i64>,
    arrival: Option<i64>,
    due: i64,
    route_zones: Vec<String>,
    route_edges: Vec<String>,
    travel_minutes: i64,
    /// `config["task_duration_minutes"][task_kind]`.
    duration: i64,
}

/// The fields of an active record that `validate_staff_operations_state` reads of it alone, or None
/// if one fails, which is then the Python rule's to say.
fn active_of(staff_id: &str, record: &OperationRecord, config: &V) -> Option<Active> {
    let (Some(task_id), Some(task_kind), Some(target_zone_id), Some(reason), Some(started), Some(due)) = (
        record.task_id.as_deref(),
        record.task_kind.as_deref(),
        record.target_zone_id.as_deref(),
        record.reason.as_deref(),
        record.started_minute,
        record.due_minute,
    ) else {
        return None;
    };
    let V::Map(config) = config else { return None };
    let Some(V::Map(durations)) = config.get("task_duration_minutes") else { return None };
    let Some(V::Int(duration)) = durations.get(task_kind) else { return None };
    if !TASK_KINDS.contains(&task_kind) || due <= started || record.travel_minutes < 0 || started < 0 || reason.is_empty() {
        return None;
    }
    Some(Active {
        staff_id: staff_id.to_owned(),
        working: record.phase == Phase::Working,
        task_id: task_id.to_owned(),
        task_kind: task_kind.to_owned(),
        target_id: record.target_id.as_deref().map(str::to_owned),
        target_zone_id: target_zone_id.to_owned(),
        started,
        travel_due: record.travel_due_minute,
        arrival: record.arrival_minute,
        due,
        route_zones: record.route_zone_ids.iter().map(|id| id.to_string()).collect(),
        route_edges: record.route_edge_ids.iter().map(|id| id.to_string()).collect(),
        travel_minutes: record.travel_minutes,
        duration: *duration,
    })
}

/// `event.payload.get(key)` as the rule compares it with a str or None.
enum Payload {
    Null,
    Text(String),
    Other,
}

impl Payload {
    fn same(&self, expected: Option<&str>) -> bool {
        match (self, expected) {
            (Payload::Null, None) => true,
            (Payload::Text(text), Some(expected)) => text == expected,
            _ => false,
        }
    }
}

fn payload_of(event: &PendingEvent<'_>, key: &str) -> PyResult<Payload> {
    match &event.source {
        PendingSource::Row(row) => {
            let V::Map(fields) = &row.payload else {
                return unsupported("a payload is not a mapping");
            };
            Ok(match fields.get(key) {
                None | Some(V::Null) => Payload::Null,
                Some(V::Str(text)) => Payload::Text(text.to_string()),
                Some(_) => Payload::Other,
            })
        }
        PendingSource::Object(object) => {
            let py = object.py();
            let value = object.getattr(intern!(py, "payload"))?.call_method1(intern!(py, "get"), (key,))?;
            if value.is_none() {
                return Ok(Payload::Null);
            }
            if let Ok(text) = value.downcast_exact::<PyString>() {
                return Ok(Payload::Text(text.to_str()?.to_owned()));
            }
            if value.is_instance_of::<PyString>() {
                // A str subclass may be equal to a str; the Python rule says which.
                return unsupported("a payload value is a str subclass");
            }
            Ok(Payload::Other)
        }
    }
}

fn event_type_of(event: &PendingEvent<'_>) -> PyResult<String> {
    match &event.source {
        PendingSource::Row(row) => Ok(row.event_type.to_string()),
        PendingSource::Object(object) => {
            let value = object.getattr(intern!(object.py(), "event_type"))?;
            Ok(text_of(&value)?.to_owned())
        }
    }
}

/// An exact str, or Unsupported: a subclass may be equal to a str, and the Python rule says which.
fn text_of<'a>(value: &'a Bound<'_, PyAny>) -> PyResult<&'a str> {
    match value.downcast_exact::<PyString>() {
        Ok(text) => text.to_str(),
        Err(_) => unsupported("a value the rule compares is not a plain str"),
    }
}

fn optional_text_of<'a>(value: &'a Bound<'_, PyAny>) -> PyResult<Option<&'a str>> {
    if value.is_none() {
        return Ok(None);
    }
    text_of(value).map(Some)
}

/// The rules of an active record that read the world: its task, its staff member, the graph and the
/// pending events. The objects are read, not the entity rows, as `rules/tick_validation.rs` does:
/// `staff.__dict__[...] = ...` reaches no row (docs/rust-port/lanes/t-tick-validation.md).
fn active_records_pass(world: &Bound<'_, PyAny>, active: &[Active]) -> PyResult<bool> {
    let py = world.py();
    let tasks = attribute(world, intern!(py, "active_tasks"))?;
    let tasks = exact_dict(&tasks)?;
    let staff = attribute(world, intern!(py, "staff"))?;
    let staff = exact_dict(&staff)?;
    let zones = attribute(world, intern!(py, "zones"))?;
    let zones = exact_dict(&zones)?;
    let edges = attribute(world, intern!(py, "zone_edges"))?;
    let edges = exact_dict(&edges)?;
    let facilities = attribute(world, intern!(py, "facilities"))?;
    let facilities = exact_dict(&facilities)?;
    let writer = EventWriter::of(world)?;
    let queue = writer.queue().borrow();
    let Some(pending) = queue.pending_events(py)? else {
        return unsupported("a pending event is not a plain ScheduledEvent");
    };
    for record in active {
        let Some(task) = tasks.get_item(&record.task_id)? else {
            return Ok(false);
        };
        let Some(member) = staff.get_item(&record.staff_id)? else {
            return Ok(false);
        };
        let task_staff = attribute(&task, intern!(py, "staff_id"))?;
        let active_task = attribute(&member, intern!(py, "active_task_id"))?;
        let work_queue = attribute(&member, intern!(py, "work_queue"))?;
        let Some(work_queue) = as_readable_list(&work_queue) else {
            return unsupported("a work queue is another kind of list");
        };
        let busy_until = attribute(&member, intern!(py, "busy_until"))?;
        let member_zone = attribute(&member, intern!(py, "zone_id"))?;
        let member_zone = text_of(&member_zone)?;
        if optional_text_of(&task_staff)? != Some(&*record.staff_id)
            || optional_text_of(&active_task)? != Some(&*record.task_id)
            || work_queue.len() != 1
            || text_of(&work_queue.get_item(0)?)? != record.task_id
            || busy_until.is_none()
            || exact_int(&busy_until)? != record.due
            || !zones.contains(&record.target_zone_id)?
            || record.route_zones.iter().map(|zone| zones.contains(zone)).any(|found| !matches!(found, Ok(true)))
            || record.route_edges.iter().map(|edge| edges.contains(edge)).any(|found| !matches!(found, Ok(true)))
            || record.target_id.as_ref().is_some_and(|target| !matches!(facilities.contains(target), Ok(true)))
        {
            return Ok(false);
        }
        if record.working {
            if !record.route_zones.is_empty()
                || !record.route_edges.is_empty()
                || record.travel_due.is_some()
                || record.arrival != Some(record.started + record.travel_minutes)
                || member_zone != record.target_zone_id
            {
                return Ok(false);
            }
        } else {
            // route_is_valid
            if record.route_zones.len() != record.route_edges.len() + 1
                || record.route_edges.is_empty()
                || record.route_zones[0] != member_zone
                || record.route_zones.last().map(String::as_str) != Some(&*record.target_zone_id)
            {
                return Ok(false);
            }
            let mut total: i64 = 0;
            for (index, edge_id) in record.route_edges.iter().enumerate() {
                let Some(edge) = edges.get_item(edge_id)? else {
                    return Ok(false);
                };
                let from = attribute(&edge, intern!(py, "from_zone_id"))?;
                let to = attribute(&edge, intern!(py, "to_zone_id"))?;
                if text_of(&from)? != record.route_zones[index] || text_of(&to)? != record.route_zones[index + 1] {
                    return Ok(false);
                }
                total = total
                    .checked_add(exact_int(&attribute(&edge, intern!(py, "travel_minutes"))?)?)
                    .ok_or_else(|| Unsupported::new_err("a route's minutes out of range"))?;
            }
            if total != record.travel_minutes
                || record.travel_due != Some(record.started + record.travel_minutes)
                || record.arrival.is_some()
            {
                return Ok(false);
            }
        }
        if record.due != record.started + record.travel_minutes + record.duration {
            return Ok(false);
        }
        let (mut completions, mut arrivals) = (Vec::new(), Vec::new());
        for event in &pending {
            let kind = event_type_of(event)?;
            let is_completion = kind == "staff_operation_complete";
            if !is_completion && kind != "staff_operation_arrive" {
                continue;
            }
            if !payload_of(event, "task_id")?.same(Some(&record.task_id))
                || !payload_of(event, "staff_id")?.same(Some(&record.staff_id))
            {
                continue;
            }
            if is_completion {
                if event.due_minute == record.due {
                    completions.push(event);
                }
            } else {
                arrivals.push(event);
            }
        }
        let expected_arrivals = usize::from(!record.working);
        if completions.len() != 1
            || !payload_of(completions[0], "task_kind")?.same(Some(&record.task_kind))
            || !payload_of(completions[0], "target_id")?.same(record.target_id.as_deref())
            || arrivals.len() != expected_arrivals
        {
            return Ok(false);
        }
        if let Some(arrival) = arrivals.first() {
            if Some(arrival.due_minute) != record.travel_due
                || !payload_of(arrival, "target_zone_id")?.same(Some(&record.target_zone_id))
            {
                return Ok(false);
            }
        }
    }
    Ok(true)
}

/// `validate_staff_operations_state`: accepts, or leaves it to the Python rules and their error text.
/// An active record's rules read its task, its staff member, the graph and the pending events.
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
    let mut active = Vec::new();
    let accepted = NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror, |row| {
        if row.schema_version != 1
            || &*row.policy_version != POLICY
            || &*row.source_id != SOURCE
            || row.history.len() > MAX_HISTORY
            || row.zones.len() != zones.len()
            || !row.zones.iter().all(|(id, zone)| zones.contains(&**id) && zone.is_valid())
            || !config_is_canonical(&row.config, &zones)
        {
            return Ok(false);
        }
        for (staff_id, record) in row.staff.iter() {
            if record.phase == Phase::Idle {
                if !record.is_the_idle_record() {
                    return Ok(false);
                }
                continue;
            }
            match active_of(staff_id, record, &row.config) {
                Some(found) => active.push(found),
                None => return Ok(false),
            }
        }
        EntityStore::with_rows(&store, |rows| {
            Ok(row.staff.len() == rows.staff.len()
                && row.staff.keys().all(|id| rows.staff.get(id).is_some())
                && row.history.iter().all(|item| item.is_valid(rows, &zones)))
        })
    })?;
    if !accepted {
        return Ok(false);
    }
    active_records_pass(world, &active)
}

/// `_validate_staff_operation_receipt` (`domain/common_operations.py`) of a receipt that passed its
/// contract: whether the retained completions of this namespace agree with it. Unsupported where the
/// tree is not the row's.
pub(crate) fn completion_agrees(world: &Bound<'_, PyAny>, receipt: &serde_json::Value, history_truncated: bool) -> PyResult<bool> {
    let py = world.py();
    let text = |key: &str| receipt.get(key).and_then(serde_json::Value::as_str);
    let int = |key: &str| receipt.get(key).and_then(serde_json::Value::as_i64);
    let (Some(task_id), Some(minute)) = (text("task_id"), int("minute")) else {
        return unsupported("a staff operation receipt lacks its task or minute");
    };
    let table = attribute(world, intern!(py, "model_state"))?;
    let table = exact_dict(&table)?;
    let Some(tree) = table.get_item(NAME)?.filter(|tree| !tree.is_none()) else {
        // No retained completions: the rule finds no match.
        return Ok(false);
    };
    let mirror = mirror_of(world, NAME)?;
    if !mirror.try_borrow()?.root_is(&tree) {
        return unsupported("the staff operations are another tree than the row's");
    }
    NamespaceMirror::with_row::<StaffOperationsRow, _>(&mirror, |row| {
        let mut matches = row.history.iter().filter(|item| &*item.task_id == task_id && item.complete_minute == minute);
        let Some(found) = matches.next() else {
            return Ok(history_truncated && !row.history.is_empty() && row.history.iter().all(|item| item.complete_minute > minute));
        };
        if matches.next().is_some() {
            return Ok(false);
        }
        let target = receipt.get("target_id");
        let target_agrees = match (&found.target_id, target) {
            (None, Some(serde_json::Value::Null)) => true,
            (Some(id), Some(serde_json::Value::String(text))) => &**id == text,
            _ => false,
        };
        let arrival_agrees = match (found.arrival_minute, receipt.get("arrival_minute")) {
            (None, Some(serde_json::Value::Null)) => true,
            (Some(arrival), Some(value)) => value.as_i64() == Some(arrival),
            _ => false,
        };
        Ok(text("staff_id") == Some(&*found.staff_id)
            && text("task_kind") == Some(&*found.task_kind)
            && target_agrees
            && text("target_zone_id") == Some(&*found.target_zone_id)
            && text("reason") == Some(&*found.reason)
            && text("status") == Some(found.status.text())
            && int("start_minute") == Some(found.start_minute)
            && arrival_agrees
            && int("travel_minutes") == Some(found.travel_minutes)
            && text("causation_id").is_some_and(|cause| found.causation_event_ids.iter().any(|id| &**id == cause)))
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("staff_operations", load_row::<StaffOperationsRow>))?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    Ok(())
}
