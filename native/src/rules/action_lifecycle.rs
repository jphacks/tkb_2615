//! `domain/action_lifecycle.py`: lifecycle receipts of autonomous actions. The Python rules stay as
//! the comparison base and for every value these kernels do not decide, which is also how a bad
//! value gets its error text (docs/rust-port/lanes/f-action-lifecycle.md).
//!
//! `ActiveActions` holds the open action of each agent as rows. The controller keeps it, and it is
//! not saved: a restored Run makes it again from the event log (`active_action_index`).
//!
//! Every kernel plans first: it reads the rows and what it was given and builds each receipt as an
//! `EventRow`, with the sequence it will get. Anything the Python rule would hand to the receipt's
//! model, or refuse, is Unsupported with nothing changed. Then `commit` allocates the sequences and
//! appends the records through `EventWriter`. No Python runs in the rule, and no Python object is
//! made: a record's object is made when Python first reads it.
//!
//! A Rust caller (lane J) uses `commit_action` with a `Proposal`.

use std::collections::BTreeMap;
use std::sync::{Arc, LazyLock};

use indexmap::IndexMap;
use pyo3::exceptions::PyTypeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyString};

use crate::core::row::{attribute, exact_dict, exact_int, exact_list, exact_str, state_row, unsupported, Field, IdMap};
use crate::core::value::V;
use crate::core::world_core::raw_dict_get;
use crate::eventlog::{AppliedOutcome, EventStore};
use crate::state::entities::{entities_of, Activity, EntityStore};
use crate::state::events::{is_stable_id, EventKind, EventRow, EventStatus, EventWriter};
use crate::Unsupported;

const SOURCE_ID: &str = "ASM-ACTION-LIFECYCLE-V1";
const POLICY_VERSION: &str = "applied-action-lifecycle-v2";
const EVENT_TYPE: &str = "agent_action_lifecycle";
const MAX_REASON_CHARS: usize = 120;
const MAX_ACTIVITY_CHARS: usize = 80;
/// Above this an int does not always fit a float, and `float(int)` rounds it.
const FLOAT_EXACT_INT: u64 = 1 << 53;

/// The strings every receipt holds, so that a receipt allocates none of them.
struct Words {
    schema_version: Arc<str>,
    action_id: Arc<str>,
    agent_id: Arc<str>,
    action: Arc<str>,
    target_id: Arc<str>,
    transition: Arc<str>,
    start_minute: Arc<str>,
    transition_minute: Arc<str>,
    duration_minutes: Arc<str>,
    reason_code: Arc<str>,
    activity: Arc<str>,
    start_activity: Arc<str>,
    start_needs: Arc<str>,
    current_needs: Arc<str>,
    model_id: Arc<str>,
    model_source_id: Arc<str>,
    source_type: Arc<str>,
    source_id: Arc<str>,
    policy_version: Arc<str>,
    assumption: Arc<str>,
    event_type: Arc<str>,
    model: Arc<str>,
    model_source: Arc<str>,
    needs: [Arc<str>; 6],
}

static WORDS: LazyLock<Words> = LazyLock::new(|| Words {
    schema_version: Arc::from("schema_version"),
    action_id: Arc::from("action_id"),
    agent_id: Arc::from("agent_id"),
    action: Arc::from("action"),
    target_id: Arc::from("target_id"),
    transition: Arc::from("transition"),
    start_minute: Arc::from("start_minute"),
    transition_minute: Arc::from("transition_minute"),
    duration_minutes: Arc::from("duration_minutes"),
    reason_code: Arc::from("reason_code"),
    activity: Arc::from("activity"),
    start_activity: Arc::from("start_activity"),
    start_needs: Arc::from("start_needs"),
    current_needs: Arc::from("current_needs"),
    model_id: Arc::from("model_id"),
    model_source_id: Arc::from("model_source_id"),
    source_type: Arc::from("source_type"),
    source_id: Arc::from("source_id"),
    policy_version: Arc::from("policy_version"),
    assumption: Arc::from("assumption"),
    event_type: Arc::from(EVENT_TYPE),
    model: Arc::from(POLICY_VERSION),
    model_source: Arc::from(SOURCE_ID),
    needs: NEED_NAMES.map(Arc::from),
});

const NEED_NAMES: [&str; 6] =
    ["toilet_urgency", "hunger", "hydration_need", "fatigue", "sleep_debt_minutes", "hygiene_need"];
const FATIGUE: usize = 3;
const SLEEP_DEBT: usize = 4;

state_row! {
    /// One open action, as `active_actions[agent_id]` holds it.
    #[derive(Clone)]
    pub(crate) struct ActiveAction {
        action_id: Arc<str>,
        agent_id: Arc<str>,
        action: Arc<str>,
        target_id: Option<Arc<str>>,
        start_minute: i64,
        reason_code: Arc<str>,
        start_activity: Option<Arc<str>>,
        start_needs: IdMap<f64>,
        model_id: Arc<str>,
        model_source_id: Arc<str>,
        continued: bool,
    }
}

/// The open actions of a controller. Rows while they fit; a Python rule that runs on them gets a
/// plain dict (`as_plain`), which then is what holds them, until a kernel can read it into rows.
/// So a value that does not fit keeps the store in Python, and the Python rules run on it.
#[pyclass(module = "sheltersim_native.rules.action_lifecycle")]
pub struct ActiveActions {
    rows: IdMap<ActiveAction>,
    plain: Option<Py<PyDict>>,
}

impl ActiveActions {
    /// Read the plain dict into rows if there is one; Unsupported, keeping it, if it does not fit.
    fn into_rows(&mut self, py: Python<'_>) -> PyResult<()> {
        let Some(plain) = &self.plain else {
            return Ok(());
        };
        let rows = IdMap::<ActiveAction>::from_py(plain.bind(py))
            .map_err(|_| Unsupported::new_err("an active action does not fit its row"))?;
        if rows.iter().any(|(key, action)| *key != action.agent_id) {
            return unsupported("an active action is under another agent's key");
        }
        self.rows = rows;
        self.plain = None;
        Ok(())
    }

    fn rows_as_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let plain = Field::to_py(&self.rows, py)?;
        Ok(plain.downcast_into::<PyDict>()?)
    }
}

#[pymethods]
impl ActiveActions {
    /// What `active_action_index` gives, with each `start_needs` a dict.
    #[new]
    fn new(py: Python<'_>, records: Bound<'_, PyDict>) -> Self {
        let mut this = Self { rows: IdMap::default(), plain: Some(records.unbind()) };
        // A store that doesn't fit stays plain, for the Python rules.
        let _ = this.into_rows(py);
        this
    }

    /// The dict a Python rule works on. It holds the actions from now on, so what the rule does to
    /// it stays, even if the rule raises.
    fn as_plain<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        if let Some(plain) = &self.plain {
            return Ok(plain.bind(py).clone());
        }
        let plain = self.rows_as_dict(py)?;
        self.plain = Some(plain.clone().unbind());
        self.rows = IdMap::default();
        Ok(plain)
    }

    /// A detached copy, changing nothing: for tests and benchmarks/rule_shadow.
    fn snapshot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        match &self.plain {
            Some(plain) => plain.bind(py).copy(),
            None => self.rows_as_dict(py),
        }
    }

    /// Whether the actions are in rows, for tests.
    fn in_rows(&self) -> bool {
        self.plain.is_none()
    }

    fn __contains__(&self, py: Python<'_>, agent_id: &Bound<'_, PyAny>) -> PyResult<bool> {
        if let Some(plain) = &self.plain {
            return plain.bind(py).contains(agent_id);
        }
        let Ok(text) = agent_id.downcast::<PyString>() else {
            return Ok(false);
        };
        Ok(self.rows.contains_key(text.to_str()?))
    }

    fn __len__(&self, py: Python<'_>) -> usize {
        match &self.plain {
            Some(plain) => plain.bind(py).len(),
            None => self.rows.len(),
        }
    }
}

// ---- What the rule reads ---------------------------------------------------------------------------

/// `_need_snapshot`, in `NEED_NAMES` order.
type Needs = [f64; 6];

/// `float(body.get(name, 0.0))` of a value the conversion leaves exact.
fn body_value(record: &Bound<'_, PyAny>, name: &Bound<'_, PyString>) -> PyResult<f64> {
    let Some(value) = raw_dict_get(record, name.as_any())? else {
        return Ok(0.0);
    };
    if let Ok(number) = value.downcast_exact::<pyo3::types::PyFloat>() {
        return Ok(number.value());
    }
    let whole = exact_int(&value)?;
    if whole.unsigned_abs() > FLOAT_EXACT_INT {
        return unsupported("a body value does not fit a float");
    }
    Ok(whole as f64)
}

/// `sleep_debt_minutes` and `hygiene_need` of `body_state_v1.agents[id]`. The one place here that
/// reads a Python document: `body_state_v1` is not Rust's yet. Tracked trees read the same way.
fn body_needs(world: &Bound<'_, PyAny>, id: &str) -> PyResult<(f64, f64)> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    let table = exact_dict(&table)?;
    let Some(state) = table.get_item(intern!(py, "body_state_v1"))? else {
        return Ok((0.0, 0.0));
    };
    let Some(agents) = exact_dict(&state)?.get_item(intern!(py, "agents"))? else {
        return Ok((0.0, 0.0));
    };
    let Some(record) = exact_dict(&agents)?.get_item(id)? else {
        return Ok((0.0, 0.0));
    };
    exact_dict(&record)?;
    Ok((
        body_value(&record, intern!(py, "sleep_debt_minutes"))?,
        body_value(&record, intern!(py, "hygiene_need"))?,
    ))
}

/// What the rule reads of an agent who is in the world.
struct Agent {
    activity: &'static str,
    needs: Needs,
}

fn agent_of(world: &Bound<'_, PyAny>, store: &Bound<'_, EntityStore>, id: &str) -> PyResult<Option<Agent>> {
    let found = EntityStore::with_rows(store, |rows| {
        let Some(agent) = rows.agents.get(id) else {
            return Ok(None);
        };
        let needs = &agent.needs;
        Ok(Some((
            agent.activity,
            [needs.toilet_urgency, needs.hunger, needs.hydration_need, needs.fatigue],
            agent.profile.id.clone(),
        )))
    })?;
    let Some((activity, [toilet, hunger, hydration, fatigue], profile)) = found else {
        return Ok(None);
    };
    let (debt, hygiene) = body_needs(world, &profile)?;
    Ok(Some(Agent {
        activity: Activity::text(activity)?,
        needs: [toilet, hunger, hydration, fatigue, debt, hygiene],
    }))
}

/// `_completion_status`, whose cause is always None.
fn completion_status(
    world: &Bound<'_, PyAny>,
    action: &ActiveAction,
    needs: &Needs,
) -> PyResult<&'static str> {
    Ok(match &*action.action {
        "wait" => {
            let now = exact_int(&attribute(world, intern!(world.py(), "minute_index"))?)?;
            if now > action.start_minute {
                "completed"
            } else {
                "interrupted"
            }
        }
        "rest" | "sleep" => {
            let lower = |index: usize| {
                action.start_needs.get(NEED_NAMES[index]).is_some_and(|started| needs[index] < *started)
            };
            if lower(FATIGUE) || (&*action.action == "sleep" && lower(SLEEP_DEBT)) {
                "completed"
            } else {
                "interrupted"
            }
        }
        _ => "interrupted",
    })
}

// ---- A receipt ---------------------------------------------------------------------------------------

/// A receipt to append, and what it does at `commit`.
struct Receipt {
    row: EventRow,
    minute: i64,
    detail: String,
}

fn needs_value(needs: &Needs) -> V {
    let mut map = IndexMap::with_capacity(6);
    for (name, value) in WORDS.needs.iter().zip(needs) {
        map.insert(name.clone(), V::Float(*value));
    }
    V::Map(map)
}

fn optional_text(value: Option<&Arc<str>>) -> V {
    value.map_or(V::Null, |text| V::Str(text.clone()))
}

fn fits_chars(value: Option<&str>, limit: usize) -> bool {
    value.is_none_or(|text| text.chars().nth(limit).is_none())
}

/// What the event of `_append_transition` is; Unsupported where `_is_plain_receipt` is false, or
/// where the event or the log would refuse it. So the Python rule then decides, and raises.
#[allow(clippy::too_many_arguments)]
fn receipt(
    log: &EventStore,
    action: &ActiveAction,
    transition: &str,
    minute: i64,
    reason_code: &str,
    activity: Option<&str>,
    causation_id: Option<&str>,
    sequence: i64,
    current: Option<&Needs>,
) -> PyResult<Receipt> {
    let plain = 0 <= action.start_minute
        && action.start_minute <= minute
        && !reason_code.is_empty()
        && fits_chars(Some(reason_code), MAX_REASON_CHARS)
        && fits_chars(activity, MAX_ACTIVITY_CHARS)
        && fits_chars(action.start_activity.as_deref(), MAX_ACTIVITY_CHARS)
        && action.target_id.as_deref().is_none_or(is_stable_id)
        && is_stable_id(&action.action_id)
        && is_stable_id(&action.agent_id)
        && is_stable_id(&action.action)
        && is_stable_id(&action.model_id)
        && is_stable_id(&action.model_source_id)
        && action.start_needs.values().all(|value| value.is_finite())
        && current.is_none_or(|needs| needs.iter().all(|value| value.is_finite()));
    if !plain {
        return unsupported("a receipt the model has to decide");
    }
    let words = &*WORDS;
    let start_needs = action.start_needs.iter().map(|(name, value)| (name.clone(), V::Float(*value))).collect();
    let mut payload = IndexMap::with_capacity(16);
    payload.insert(words.schema_version.clone(), V::Int(1));
    payload.insert(words.action_id.clone(), V::Str(action.action_id.clone()));
    payload.insert(words.agent_id.clone(), V::Str(action.agent_id.clone()));
    payload.insert(words.action.clone(), V::Str(action.action.clone()));
    payload.insert(words.target_id.clone(), optional_text(action.target_id.as_ref()));
    payload.insert(words.transition.clone(), V::Str(Arc::from(transition)));
    payload.insert(words.start_minute.clone(), V::Int(action.start_minute));
    payload.insert(words.transition_minute.clone(), V::Int(minute));
    payload.insert(words.duration_minutes.clone(), V::Int(minute - action.start_minute));
    payload.insert(words.reason_code.clone(), V::Str(Arc::from(reason_code)));
    payload.insert(words.activity.clone(), activity.map_or(V::Null, |text| V::Str(Arc::from(text))));
    payload.insert(words.start_activity.clone(), optional_text(action.start_activity.as_ref()));
    payload.insert(words.start_needs.clone(), V::Map(start_needs));
    payload.insert(
        words.current_needs.clone(),
        current.map_or_else(|| V::Map(IndexMap::new()), needs_value),
    );
    payload.insert(words.model_id.clone(), V::Str(action.model_id.clone()));
    payload.insert(words.model_source_id.clone(), V::Str(action.model_source_id.clone()));
    let mut provenance = IndexMap::with_capacity(3);
    provenance.insert(words.source_type.clone(), V::Str(words.assumption.clone()));
    provenance.insert(words.source_id.clone(), V::Str(words.model_source.clone()));
    provenance.insert(words.policy_version.clone(), V::Str(words.model.clone()));
    let event_id: Arc<str> = Arc::from(format!("action-lifecycle-{sequence}"));
    if log.has_event_id(&event_id) || log.has_sequence(sequence) {
        return unsupported("the log already has this receipt's identity");
    }
    let row = EventRow::new(
        event_id,
        EventKind::Endogenous,
        minute,
        0,
        sequence,
        words.event_type.clone(),
        payload,
        Some(words.model.clone()),
        causation_id.map(Arc::from),
        provenance,
    );
    let Some(row) = row else {
        return unsupported("an event the Python class refuses");
    };
    Ok(Receipt { row, minute, detail: format!("autonomous action {transition}") })
}

/// Allocate each receipt's sequence and append it: `allocate_event_sequence` and `EventLog.append`.
fn commit(writer: &EventWriter<'_>, receipts: Vec<Receipt>) -> PyResult<()> {
    for receipt in receipts {
        let sequence = writer.allocate_sequence()?;
        debug_assert_eq!(sequence, receipt.row.sequence);
        writer.record(&receipt.row, receipt.minute, EventStatus::Applied, Some(&receipt.detail))?;
    }
    Ok(())
}

// ---- record_action_proposal --------------------------------------------------------------------------

/// What `record_action_proposal` reads of an applied proposal.
pub(crate) struct Proposal<'a> {
    pub(crate) agent_id: &'a str,
    pub(crate) action: &'a str,
    pub(crate) target_id: Option<&'a str>,
    pub(crate) minute: i64,
    pub(crate) trigger: &'a str,
}

/// `record_action_proposal` of a committed proposal, for a Rust caller too. Unsupported, with
/// nothing changed, for the Python rule on `active.as_plain()`.
pub(crate) fn commit_action(
    world: &Bound<'_, PyAny>,
    active: &Bound<'_, ActiveActions>,
    proposal: &Proposal<'_>,
) -> PyResult<()> {
    let py = world.py();
    let store = entities_of(world)?;
    let Some(agent) = agent_of(world, &store, proposal.agent_id)? else {
        return Ok(());
    };
    let writer = EventWriter::of(world)?;
    let mut this = active.try_borrow_mut()?;
    this.into_rows(py)?;
    let (minute, activity) = (proposal.minute, Some(agent.activity));
    let first = writer.next_sequence()?;
    let log = writer.log().borrow();
    let mut receipts = Vec::with_capacity(2);
    if let Some(current) = this.rows.get(proposal.agent_id) {
        let same_target = current.target_id.as_deref() == proposal.target_id;
        if &*current.action == proposal.action && same_target {
            if current.continued {
                return Ok(());
            }
            receipts.push(receipt(
                &log,
                current,
                "continued",
                minute,
                proposal.trigger,
                activity,
                None,
                first,
                Some(&agent.needs),
            )?);
            drop(log);
            commit(&writer, receipts)?;
            if let Some(current) = this.rows.get_mut(proposal.agent_id) {
                current.continued = true;
            }
            return Ok(());
        }
        let status = completion_status(world, current, &agent.needs)?;
        receipts.push(receipt(
            &log,
            current,
            status,
            minute,
            "superseded_by_new_applied_action",
            activity,
            None,
            first,
            Some(&agent.needs),
        )?);
    }
    let sequence = first + receipts.len() as i64;
    let mut start_needs = IdMap::with_capacity_and_hasher(6, Default::default());
    for (name, value) in WORDS.needs.iter().zip(agent.needs) {
        start_needs.insert(name.clone(), value);
    }
    let started = ActiveAction {
        action_id: Arc::from(format!("action-{sequence}")),
        agent_id: Arc::from(proposal.agent_id),
        action: Arc::from(proposal.action),
        target_id: proposal.target_id.map(Arc::from),
        start_minute: minute,
        reason_code: Arc::from(proposal.trigger),
        start_activity: Some(Arc::from(agent.activity)),
        start_needs,
        model_id: WORDS.model.clone(),
        model_source_id: WORDS.model_source.clone(),
        continued: false,
    };
    receipts.push(receipt(
        &log,
        &started,
        "started",
        minute,
        proposal.trigger,
        activity,
        None,
        sequence,
        Some(&agent.needs),
    )?);
    drop(log);
    commit(&writer, receipts)?;
    // The old action goes and the new one is the dict's last key, as pop and insert leave it.
    this.rows.shift_remove(proposal.agent_id);
    this.rows.insert(started.agent_id.clone(), started);
    Ok(())
}

/// `record_action_proposal`.
#[pyfunction]
#[pyo3(signature = (world, proposal, *, target_id, committed, active_actions))]
fn record_action_proposal(
    world: &Bound<'_, PyAny>,
    proposal: &Bound<'_, PyAny>,
    target_id: &Bound<'_, PyAny>,
    committed: &Bound<'_, PyAny>,
    active_actions: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let Ok(committed) = committed.downcast_exact::<PyBool>() else {
        return unsupported("committed is not a bool");
    };
    if !committed.is_true() {
        return Ok(());
    }
    let active = store_of(active_actions)?;
    let agent_id = attribute(proposal, intern!(py, "agent_id"))?;
    let action = attribute(&attribute(proposal, intern!(py, "action"))?, intern!(py, "value"))?;
    let trigger = attribute(proposal, intern!(py, "trigger"))?;
    commit_action(
        world,
        active,
        &Proposal {
            agent_id: exact_str(&agent_id)?,
            action: exact_str(&action)?,
            target_id: if target_id.is_none() { None } else { Some(exact_str(target_id)?) },
            minute: exact_int(&attribute(proposal, intern!(py, "minute_index"))?)?,
            trigger: exact_str(&trigger)?,
        },
    )
}

fn store_of<'a, 'py>(value: &'a Bound<'py, PyAny>) -> PyResult<&'a Bound<'py, ActiveActions>> {
    value.downcast::<ActiveActions>().map_err(|_| PyTypeError::new_err("active_actions is not the store"))
}

// ---- The applied events ------------------------------------------------------------------------------

/// `_COMPLETION_ACTIONS`, `_ABANDONED_ACTIONS` and `_INTERRUPTED_ACTIONS`: what an event of this type
/// does to an action, and None for an action it does not end.
fn event_ends(event_type: &str, action: &str) -> Option<&'static str> {
    match (event_type, action) {
        ("toilet_service_complete", "request_toilet")
        | ("resource_service_complete", "fetch_water" | "collect_meal")
        | ("staff_support_complete", "seek_help" | "seek_information")
        | ("social_interaction", "socialize" | "care_family" | "help_others") => Some("completed"),
        ("facility_queue_abandon", "request_toilet" | "fetch_water" | "collect_meal") => Some("abandoned"),
        ("resource_service_cancel", "fetch_water" | "collect_meal")
        | ("support_request_timeout", "seek_help" | "seek_information") => Some("interrupted"),
        _ => None,
    }
}

fn ends_any_action(event_type: &str) -> bool {
    matches!(
        event_type,
        "toilet_service_complete"
            | "resource_service_complete"
            | "staff_support_complete"
            | "social_interaction"
            | "facility_queue_abandon"
            | "resource_service_cancel"
            | "support_request_timeout"
    )
}

/// `payload.get(key)` when `isinstance(value, str)`; Unsupported for a subclass of str.
fn payload_str(payload: &Bound<'_, PyAny>, key: &Bound<'_, PyString>) -> PyResult<Option<String>> {
    let value = payload.call_method1(intern!(payload.py(), "get"), (key,))?;
    if value.downcast::<PyString>().is_err() {
        return Ok(None);
    }
    exact_str(&value).map(|text| Some(text.to_owned()))
}

/// Whether the log holds this event applied, as the Python rules ask. The event must be the object
/// the record holds: another object of the same values is the Python rule's to compare.
fn is_applied(writer: &EventWriter<'_>, event_id: &str, event: &Bound<'_, PyAny>) -> PyResult<bool> {
    match writer.log().borrow().applied_outcome(event_id, event)? {
        AppliedOutcome::No => Ok(false),
        AppliedOutcome::ThisEvent => Ok(true),
        AppliedOutcome::AnotherObject => unsupported("the record holds another object of the event"),
    }
}

/// `record_applied_action_completion`.
#[pyfunction]
#[pyo3(signature = (world, event, minute, *, active_actions))]
fn record_applied_action_completion(
    world: &Bound<'_, PyAny>,
    event: &Bound<'_, PyAny>,
    minute: &Bound<'_, PyAny>,
    active_actions: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let event_type = attribute(event, intern!(py, "event_type"))?;
    let event_type = exact_str(&event_type)?;
    // Nothing below changes anything for another type of event.
    if !ends_any_action(event_type) {
        return Ok(());
    }
    let active = store_of(active_actions)?;
    let event_id = attribute(event, intern!(py, "event_id"))?;
    let event_id = exact_str(&event_id)?;
    let writer = EventWriter::of(world)?;
    if !is_applied(&writer, event_id, event)? {
        return Ok(());
    }
    let minute = exact_int(minute)?;
    let payload = attribute(event, intern!(py, "payload"))?;
    let agent_id = match payload_str(&payload, intern!(py, "agent_id"))? {
        Some(id) => id,
        None => match payload_str(&payload, intern!(py, "sender_agent_id"))? {
            Some(id) => id,
            None => return Ok(()),
        },
    };
    let mut this = active.try_borrow_mut()?;
    this.into_rows(py)?;
    let Some(current) = this.rows.get(agent_id.as_str()) else {
        return Ok(());
    };
    let support = matches!(event_type, "staff_support_complete" | "support_request_timeout");
    let keys: &[&Bound<'_, PyString>] = if support {
        &[intern!(py, "request_id"), intern!(py, "staff_id")]
    } else {
        &[
            intern!(py, "facility_id"),
            intern!(py, "participant_agent_id"),
            intern!(py, "staff_id"),
            intern!(py, "request_id"),
        ]
    };
    let mut observed = None;
    for key in keys {
        observed = payload_str(&payload, key)?;
        if observed.is_some() {
            break;
        }
    }
    if let (Some(expected), Some(observed)) = (&current.target_id, &observed) {
        if **expected != **observed {
            return Ok(());
        }
    }
    if exact_int(&attribute(event, intern!(py, "due_minute"))?)? < current.start_minute {
        return Ok(());
    }
    let Some(status) = event_ends(event_type, &current.action) else {
        return Ok(());
    };
    let store = entities_of(world)?;
    let agent = agent_of(world, &store, &agent_id)?;
    let sequence = writer.next_sequence()?;
    let planned = receipt(
        &writer.log().borrow(),
        current,
        status,
        minute,
        event_type,
        agent.as_ref().map(|agent| agent.activity),
        Some(event_id),
        sequence,
        agent.as_ref().map(|agent| &agent.needs),
    )?;
    commit(&writer, vec![planned])?;
    this.rows.shift_remove(agent_id.as_str());
    Ok(())
}

/// `state.get(field, ())` of a namespace when `isinstance(state, dict)`, for a list.
fn rows_of<'py>(
    table: &Bound<'py, PyDict>,
    name: &Bound<'py, PyString>,
    field: &Bound<'py, PyString>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let Some(state) = table.get_item(name)? else {
        return Ok(Vec::new());
    };
    if state.downcast::<PyDict>().is_err() {
        return Ok(Vec::new());
    }
    match exact_dict(&state)?.get_item(field)? {
        Some(rows) => Ok(exact_list(&rows)?.iter().collect()),
        None => Ok(Vec::new()),
    }
}

/// The agents an applied population event took away, and why: the middle of
/// `record_applied_population_exit`. `population_*` is not Rust's yet.
fn population_causes(world: &Bound<'_, PyAny>, event_id: &str) -> PyResult<BTreeMap<String, &'static str>> {
    let py = world.py();
    let table = attribute(world, intern!(py, "model_state"))?;
    let table = exact_dict(&table)?;
    let mut causes = BTreeMap::new();
    let matching = |row: &Bound<'_, PyAny>| -> PyResult<Option<String>> {
        if row.downcast::<PyDict>().is_err() {
            return Ok(None);
        }
        let row = exact_dict(row)?;
        let same_event = match row.get_item(intern!(py, "event_id"))? {
            Some(value) => value.downcast::<PyString>().is_ok() && exact_str(&value)? == event_id,
            None => false,
        };
        if !same_event {
            return Ok(None);
        }
        match row.get_item(intern!(py, "agent_id"))? {
            Some(value) if value.downcast::<PyString>().is_ok() => Ok(Some(exact_str(&value)?.to_owned())),
            _ => Ok(None),
        }
    };
    for row in rows_of(table, intern!(py, "population_lifecycle_v1"), intern!(py, "history"))? {
        let Some(agent_id) = matching(&row)? else {
            continue;
        };
        let action = exact_dict(&row)?.get_item(intern!(py, "action"))?;
        match action.as_ref().and_then(|action| action.downcast_exact::<PyString>().ok()).map(|text| text.to_str()) {
            Some(Ok("temporary_exit")) => {
                causes.insert(agent_id, "population_temporary_exit");
            }
            Some(Ok("transferred")) => {
                causes.insert(agent_id, "population_transfer");
            }
            _ => {}
        }
    }
    for row in rows_of(table, intern!(py, "population_dynamics_v1"), intern!(py, "departures"))? {
        if let Some(agent_id) = matching(&row)? {
            causes.insert(agent_id, "population_departure");
        }
    }
    Ok(causes)
}

/// `record_applied_population_exit`.
#[pyfunction]
#[pyo3(signature = (world, event, minute, *, active_actions))]
fn record_applied_population_exit(
    world: &Bound<'_, PyAny>,
    event: &Bound<'_, PyAny>,
    minute: &Bound<'_, PyAny>,
    active_actions: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let event_type = attribute(event, intern!(py, "event_type"))?;
    if exact_str(&event_type)? != "effect_program_population" {
        return Ok(());
    }
    let active = store_of(active_actions)?;
    let event_id = attribute(event, intern!(py, "event_id"))?;
    let event_id = exact_str(&event_id)?;
    let writer = EventWriter::of(world)?;
    if !is_applied(&writer, event_id, event)? {
        return Ok(());
    }
    let minute = exact_int(minute)?;
    let causes = population_causes(world, event_id)?;
    let mut this = active.try_borrow_mut()?;
    this.into_rows(py)?;
    let store = entities_of(world)?;
    let mut sequence = writer.next_sequence()?;
    let mut receipts = Vec::new();
    let mut closed = Vec::new();
    {
        let log = writer.log().borrow();
        for (agent_id, reason) in &causes {
            let Some(current) = this.rows.get(agent_id.as_str()) else {
                continue;
            };
            let agent = agent_of(world, &store, agent_id)?;
            receipts.push(receipt(
                &log,
                current,
                "interrupted",
                minute,
                reason,
                None,
                Some(event_id),
                sequence,
                agent.as_ref().map(|agent| &agent.needs),
            )?);
            closed.push(agent_id);
            sequence += 1;
        }
    }
    commit(&writer, receipts)?;
    for agent_id in closed {
        this.rows.shift_remove(agent_id.as_str());
    }
    Ok(())
}

/// `finalize_active_actions`.
#[pyfunction]
#[pyo3(signature = (world, minute, *, active_actions))]
fn finalize_active_actions(
    world: &Bound<'_, PyAny>,
    minute: &Bound<'_, PyAny>,
    active_actions: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let active = store_of(active_actions)?;
    let writer = EventWriter::of(world)?;
    let minute = exact_int(minute)?;
    let store = entities_of(world)?;
    let mut this = active.try_borrow_mut()?;
    this.into_rows(py)?;
    let mut ordered: Vec<&ActiveAction> = this.rows.values().collect();
    ordered.sort_unstable_by(|first, second| first.agent_id.cmp(&second.agent_id));
    let first = writer.next_sequence()?;
    let mut receipts = Vec::with_capacity(ordered.len());
    {
        let log = writer.log().borrow();
        for (index, current) in ordered.into_iter().enumerate() {
            let agent = agent_of(world, &store, &current.agent_id)?;
            let status = match &agent {
                None => "censored",
                Some(agent) if completion_status(world, current, &agent.needs)? == "completed" => "completed",
                Some(_) => "censored",
            };
            receipts.push(receipt(
                &log,
                current,
                status,
                minute,
                "simulation_end_right_censor",
                agent.as_ref().map(|agent| agent.activity),
                None,
                first + index as i64,
                agent.as_ref().map(|agent| &agent.needs),
            )?);
        }
    }
    commit(&writer, receipts)?;
    this.rows.clear();
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<ActiveActions>()?;
    m.add_function(wrap_pyfunction!(record_action_proposal, m)?)?;
    m.add_function(wrap_pyfunction!(record_applied_action_completion, m)?)?;
    m.add_function(wrap_pyfunction!(record_applied_population_exit, m)?)?;
    m.add_function(wrap_pyfunction!(finalize_active_actions, m)?)?;
    Ok(())
}
