//! `facility_queues_v1`: the independent FIFO of each service point (`domain/facility_queues.py`).
//! `rules/facility_queues.rs` reads the row and writes through the tree, whose writes reach it. A
//! value of another type than the rules write taints the namespace, and the Python rules and their
//! errors run on the tree.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;

use crate::core::row::{attribute, load_row, state_row, IdMap};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::{mirror_of, raw_dict_get, NamespaceMirror, RowType};
use crate::Unsupported;

pub(crate) const NAME: &str = "facility_queues_v1";

state_row! {
    #[derive(Clone)]
    pub(crate) struct QueueStall {
        minute: i64,
        request_id: Arc<str>,
        reason: Arc<str>,
    }
}

state_row! {
    #[derive(Clone)]
    pub(crate) struct QueueRow {
        facility_id: Arc<str>,
        service_point_id: Arc<str>,
        queued_request_ids: Vec<Arc<str>>,
        active_request_ids: Vec<Arc<str>>,
        last_stall: Option<QueueStall>,
    }
}

state_row! {
    #[derive(Clone)]
    pub(crate) struct QueueRequest {
        request_id: Arc<str>,
        queue_id: Arc<str>,
        service_point_id: Arc<str>,
        facility_id: Arc<str>,
        agent_id: Arc<str>,
        service_kind: Arc<str>,
        requested_minute: i64,
        status: Arc<str>,
        queue_task_id: Arc<str>,
        active_task_id: Arc<str>,
    }
}

state_row! {
    pub(crate) struct QueuesRow {
        schema_version: i64,
        policy_version: Arc<str>,
        queues: IdMap<QueueRow>,
        requests: IdMap<QueueRequest>,
    }
}

/// The mirror of the namespace, if the world holds it. A row left behind by a write past the table
/// answers for nothing.
pub(crate) fn held_mirror<'py>(world: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, NamespaceMirror>>> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    if as_readable_dict(&table).is_none() {
        return Err(Unsupported::new_err("model_state is another kind of mapping"));
    }
    let Some(held) = raw_dict_get(&table, intern!(py, "facility_queues_v1").as_any())? else {
        return Ok(None);
    };
    let mirror = mirror_of(world, NAME)?;
    if !mirror.try_borrow().is_ok_and(|mirror| mirror.root_is(&held)) {
        return Err(Unsupported::new_err("the world holds another queue state"));
    }
    Ok(Some(mirror))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("facility_queues", load_row::<QueuesRow>))?)?;
    Ok(())
}
