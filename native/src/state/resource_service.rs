//! `resource_service_v1`: what the staffed water and meal service holds (`domain/resource_service.py`).
//! `rules/service.rs` writes it in place; the rest of the Python rules write one value at a time
//! through the forwarded writes (docs/rust-port/lanes/m4-service.md).
//!
//! A value of another type than the rules write taints the namespace, and the Python rules and their
//! errors run on the tree.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

use crate::core::row::{attribute, load_row, namespace_is_absent, state_row, IdMap};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::{mirror_of, raw_dict_get, NamespaceMirror, RowType};
use crate::state::entities::{entities_of, Entities, EntityStore};
use crate::Unsupported;

pub(crate) const NAME: &str = "resource_service_v1";
const QUEUES_KEY: &str = "facility_queues_v1";
const LIFECYCLE_KEY: &str = "population_lifecycle_v1";
const SCHEMA_VERSION: i64 = 1;

state_row! {
    #[derive(Clone)]
    pub(crate) struct RequestRecord {
        minute: i64,
        agent_id: Arc<str>,
        action: Arc<str>,
        outcome: Arc<str>,
        reason: Arc<str>,
        facility_id: Option<Arc<str>>,
        origin_zone_id: Arc<str>,
        detail: Option<Arc<str>>,
    }
}

state_row! {
    #[derive(Clone)]
    pub(crate) struct StallRecord {
        minute: i64,
        facility_id: Arc<str>,
        agent_id: Arc<str>,
        reason: Arc<str>,
    }
}

state_row! {
    #[derive(Clone)]
    pub(crate) struct Stall {
        agent_id: Arc<str>,
        reason: Arc<str>,
    }
}

state_row! {
    pub(crate) struct ServiceRow {
        schema_version: i64,
        cancelled_task_ids: Vec<Arc<str>>,
        active_reservation_ids: Vec<Arc<str>>,
        request_history: Vec<RequestRecord>,
        queue_stall_history: Vec<StallRecord>,
        stall_state: IdMap<Stall>,
    }
}

/// The mirror of the namespace the world really holds: a row left behind by a write past the table
/// answers for nothing.
pub(crate) fn held_mirror<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, NamespaceMirror>> {
    let py = world.py();
    let mirror = mirror_of(world, NAME)?;
    let table = attribute(world, intern!(py, "model_state"))?;
    if as_readable_dict(&table).is_none() {
        return Err(Unsupported::new_err("model_state is another kind of mapping"));
    }
    let held = raw_dict_get(&table, intern!(py, "resource_service_v1").as_any())?;
    if !held.is_some_and(|held| mirror.try_borrow().is_ok_and(|mirror| mirror.root_is(&held))) {
        return Err(Unsupported::new_err("the world holds another service state"));
    }
    Ok(mirror)
}

fn listed_once(ids: &[Arc<str>]) -> bool {
    // A handful of IDs at a time for the active ones. The cancelled ones grow only with cancellations.
    let mut sorted: Vec<&str> = ids.iter().map(|id| &**id).collect();
    sorted.sort_unstable();
    sorted.iter().all(|id| !id.is_empty()) && sorted.windows(2).all(|pair| pair[0] != pair[1])
}

fn request_outcome_is_known(record: &RequestRecord) -> bool {
    matches!(
        (&*record.outcome, &*record.reason),
        ("queued", "arrived_at_facility")
            | ("travel_scheduled", "route_available" | "in_room_route_available")
            | (
                "rejected",
                "room_route_blocked" | "no_facility" | "no_route" | "request_rejected" | "travel_rejected" | "service_disabled"
            )
    )
}

fn stall_reason_is_known(reason: &str) -> bool {
    matches!(reason, "capacity" | "staff" | "stock" | "run_end" | "water_outage")
}

/// `records_since_previous_tick`: the newest records, from the last one before the previous tick's.
fn window_start<T>(history: &[T], minute_of: impl Fn(&T) -> i64, minute_index: i64) -> usize {
    let mut start = history.len();
    while start > 0 && minute_of(&history[start - 1]) >= minute_index - 1 {
        start -= 1;
    }
    start.saturating_sub(1)
}

struct Known<'py> {
    agents: Bound<'py, PyAny>,
    zones: Bound<'py, PyAny>,
    archived: Option<Bound<'py, PyDict>>,
}

impl Known<'_> {
    fn person(&self, id: &str) -> PyResult<bool> {
        let key = PyString::new(self.agents.py(), id);
        if raw_dict_get(&self.agents, key.as_any())?.is_some() {
            return Ok(true);
        }
        Ok(match &self.archived {
            Some(people) => people.contains(&key)?,
            None => false,
        })
    }

    fn zone(&self, id: &str) -> PyResult<bool> {
        Ok(raw_dict_get(&self.zones, PyString::new(self.zones.py(), id).as_any())?.is_some())
    }
}

impl ServiceRow {
    /// The rules of a tick's check in `WorldState.validate`, given a row of the right types. True
    /// says the Python rules accept; false decides nothing.
    fn passes_rules(&self, rows: &Entities, known: &Known<'_>, minute_index: i64) -> PyResult<bool> {
        if self.schema_version != SCHEMA_VERSION
            || !listed_once(&self.cancelled_task_ids)
            || !listed_once(&self.active_reservation_ids)
        {
            return Ok(false);
        }
        let from = window_start(&self.request_history, |record| record.minute, minute_index);
        for record in &self.request_history[from..] {
            let expected_kind = match &*record.action {
                "fetch_water" => "water_station",
                "collect_meal" => "meal_distribution",
                _ => return Ok(false),
            };
            if !(0..=minute_index).contains(&record.minute)
                || !request_outcome_is_known(record)
                || !known.person(&record.agent_id)?
                || !known.zone(&record.origin_zone_id)?
            {
                return Ok(false);
            }
            if let Some(facility_id) = &record.facility_id {
                if rows.facilities.get(facility_id).is_none_or(|facility| &*facility.kind != expected_kind) {
                    return Ok(false);
                }
            }
        }
        let from = window_start(&self.queue_stall_history, |record| record.minute, minute_index);
        let mut previous = -1;
        for record in &self.queue_stall_history[from..] {
            if record.minute < 0
                || record.minute < previous
                || record.minute > minute_index
                || rows.facilities.get(&record.facility_id).is_none()
                || !known.person(&record.agent_id)?
                || !stall_reason_is_known(&record.reason)
            {
                return Ok(false);
            }
            previous = record.minute;
        }
        for (facility_id, record) in &self.stall_state {
            let Some(facility) = rows.facilities.get(facility_id) else {
                return Ok(false);
            };
            if !matches!(&*facility.kind, "water_station" | "meal_distribution")
                || rows.agents.get(&record.agent_id).is_none()
                || !stall_reason_is_known(&record.reason)
                || facility.resource_queue_agent_ids.first() != Some(&record.agent_id)
            {
                return Ok(false);
            }
        }
        Ok(true)
    }
}

/// The part of `WorldState.validate` a tick runs: accepts, or leaves it to the Python rules.
#[pyfunction]
fn validate(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = world.py();
    if namespace_is_absent(world, intern!(py, NAME))? {
        return Ok(false);
    }
    let table = attribute(world, intern!(py, "model_state"))?;
    let table = as_readable_dict(&table).ok_or_else(|| Unsupported::new_err("model_state is another kind of mapping"))?;
    if table.contains(QUEUES_KEY)? {
        return Ok(false);
    }
    let archived = match table.get_item(LIFECYCLE_KEY)? {
        Some(lifecycle) => match as_readable_dict(&lifecycle) {
            Some(lifecycle) => match lifecycle.get_item(intern!(py, "people"))? {
                Some(people) => as_readable_dict(&people).cloned(),
                None => None,
            },
            None => None,
        },
        None => None,
    };
    let known = Known { agents: attribute(world, intern!(py, "agents"))?, zones: attribute(world, intern!(py, "zones"))?, archived };
    if as_readable_dict(&known.agents).is_none() || as_readable_dict(&known.zones).is_none() {
        return Ok(false);
    }
    let minute_index = attribute(world, intern!(py, "minute_index"))?.extract::<i64>().map_err(|_| Unsupported::new_err("the minute is not an int"))?;
    let mirror = held_mirror(world)?;
    let store = entities_of(world)?;
    NamespaceMirror::with_row::<ServiceRow, _>(&mirror, |row| {
        EntityStore::with_rows(&store, |rows| row.passes_rules(rows, &known, minute_index))
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("resource_service", load_row::<ServiceRow>))?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    Ok(())
}
