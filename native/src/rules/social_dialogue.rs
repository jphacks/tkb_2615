//! `domain/social_dialogue.py`: the participants' modeled state a social event carries. Both
//! kernels only read and build. Anything but what the Python rules accept without a question says
//! Unsupported, and they then accept it or raise their own text.
//!
//! `virtual_dialogue_example` stays in Python: it runs when a history is read, not in a tick.

use std::sync::{Arc, LazyLock};

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyFloat, PyList, PyString, PyTuple, PyType};
use pyo3::IntoPyObjectExt;

use crate::core::row::{attribute, exact_int, exact_str, unsupported};
use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::core::world_core::raw_dict_get;
use crate::rules::social_network::body_state_of;
use crate::state::entities::{entities_of, AgentRow, EntityStore};

const NEEDS: [&str; 4] = ["toilet_urgency", "hunger", "fatigue", "hydration_need"];
const AFFECT: [&str; 6] = [
    "positive_affect",
    "negative_affect",
    "reassurance",
    "discontent",
    "uncertainty",
    "hygiene_need",
];

static MAPPING_PROXY: GILOnceCell<Py<PyType>> = GILOnceCell::new();

/// A dict or the frozen form of an event's payload: reading either runs no Python code.
fn is_plain_mapping(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    if as_readable_dict(value).is_some() {
        return Ok(true);
    }
    let py = value.py();
    let proxy = MAPPING_PROXY.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import("types")?.getattr("MappingProxyType")?.downcast_into::<PyType>()?.unbind())
    })?;
    Ok(value.get_type().is(proxy.bind(py)))
}

/// The value of each field of a mapping with exactly these fields, or None for anything else.
fn exact_fields<'py, const N: usize>(
    value: &Bound<'py, PyAny>,
    names: [&Bound<'py, PyString>; N],
) -> PyResult<Option<[Bound<'py, PyAny>; N]>> {
    if !is_plain_mapping(value)? || value.len()? != N {
        return Ok(None);
    }
    let mut items = Vec::with_capacity(N);
    for name in names {
        match value.get_item(name) {
            Ok(item) => items.push(item),
            Err(_) => return Ok(None),
        }
    }
    Ok(items.try_into().ok())
}

/// A float within bounds. An int passes `_validate_unit` too, but only its slow path; Python's.
fn is_unit(value: &Bound<'_, PyAny>) -> bool {
    value.downcast_exact::<PyFloat>().is_ok_and(|value| (0.0..=1.0).contains(&value.value()))
}

fn all_named_units<'py>(value: &Bound<'py, PyAny>, names: &[&'static str]) -> PyResult<bool> {
    if !is_plain_mapping(value)? || value.len()? != names.len() {
        return Ok(false);
    }
    let py = value.py();
    for name in names {
        match value.get_item(PyString::intern(py, name)) {
            Ok(item) if is_unit(&item) => {}
            _ => return Ok(false),
        }
    }
    Ok(true)
}

/// `validate_social_state_context` accepts these, without its slow paths. `ids` have no repeats.
pub(crate) fn context_is_valid(context: &Bound<'_, PyAny>, ids: &[&Bound<'_, PyString>], due_minute: i64) -> PyResult<bool> {
    let py = context.py();
    let Some([version, captured, participants]) = exact_fields(
        context,
        [intern!(py, "schema_version"), intern!(py, "captured_minute"), intern!(py, "participants")],
    )?
    else {
        return Ok(false);
    };
    if exact_int(&version).ok() != Some(1) {
        return Ok(false);
    }
    let Ok(captured) = exact_int(&captured) else {
        return Ok(false);
    };
    if !(0..=due_minute).contains(&captured) || !is_plain_mapping(&participants)? || participants.len()? != ids.len() {
        return Ok(false);
    }
    for id in ids {
        let Ok(state) = participants.get_item(id) else {
            return Ok(false);
        };
        let Some([stress, trust, needs, affect]) = exact_fields(
            &state,
            [intern!(py, "stress"), intern!(py, "trust"), intern!(py, "needs"), intern!(py, "affect")],
        )?
        else {
            return Ok(false);
        };
        if !is_unit(&stress) || !is_unit(&trust) || !all_named_units(&needs, &NEEDS)? {
            return Ok(false);
        }
        if !affect.is_none() && !all_named_units(&affect, &AFFECT)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// The IDs of `set(participant_ids)`, exact strs in a tuple or a list.
fn id_set<'py>(participant_ids: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyString>>> {
    let items: Vec<Bound<'py, PyAny>> = if let Ok(ids) = participant_ids.downcast_exact::<PyTuple>() {
        ids.iter().collect()
    } else if let Ok(ids) = participant_ids.downcast_exact::<PyList>() {
        ids.iter().collect()
    } else {
        return unsupported("the participants are neither a tuple nor a list");
    };
    let mut ids: Vec<Bound<'py, PyString>> = Vec::with_capacity(items.len());
    for item in items {
        exact_str(&item)?;
        let id = item.downcast_into::<PyString>()?;
        let mut known = false;
        for other in &ids {
            known = known || other.to_str()? == id.to_str()?;
        }
        if !known {
            ids.push(id);
        }
    }
    Ok(ids)
}

/// `validate_social_state_context`.
#[pyfunction]
fn validate_context(
    context: &Bound<'_, PyAny>,
    participant_ids: &Bound<'_, PyAny>,
    due_minute: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let ids = id_set(participant_ids)?;
    let ids: Vec<&Bound<'_, PyString>> = ids.iter().collect();
    if context_is_valid(context, &ids, exact_int(due_minute)?)? {
        Ok(())
    } else {
        unsupported("the context is for the Python rule to judge")
    }
}

/// A person's values in a row, which `capture_context` copies: each must pass the validation.
struct Captured {
    stress: f64,
    trust: f64,
    needs: [f64; 4],
}

impl Captured {
    fn of(row: &AgentRow) -> PyResult<Self> {
        let captured = Self {
            stress: row.stress,
            trust: row.trust,
            needs: [row.needs.toilet_urgency, row.needs.hunger, row.needs.fatigue, row.needs.hydration_need],
        };
        let unit = |value: f64| (0.0..=1.0).contains(&value);
        if !unit(captured.stress) || !unit(captured.trust) || !captured.needs.iter().copied().all(unit) {
            return unsupported("a person's value is out of bounds");
        }
        Ok(captured)
    }
}

/// What `capture_social_state_context` copies: the participants, in sorted order.
struct Capture {
    minute: i64,
    people: Vec<Captured>,
    affects: Vec<Option<[f64; 6]>>,
}

/// `capture_social_state_context`, for one or two IDs of no repeats in sorted order.
fn read_capture(world: &Bound<'_, PyAny>, ids: &[&str]) -> PyResult<Capture> {
    let py = world.py();
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    let duration = exact_int(&attribute(world, intern!(py, "duration_minutes"))?)?;
    if !(0..=duration).contains(&minute) {
        return unsupported("the minute is outside the run");
    }
    let mut people = Vec::with_capacity(ids.len());
    EntityStore::with_rows(&entities_of(world)?, |rows| {
        for id in ids {
            let Some(row) = rows.agents.get(id) else {
                return unsupported("a participant is not in the world");
            };
            people.push(Captured::of(row)?);
        }
        Ok(())
    })?;
    let body = body_state_of(world)?;
    let body_agents = match &body {
        Some(body) => match raw_dict_get(body, intern!(py, "agents").as_any())? {
            Some(agents) if as_readable_dict(&agents).is_some() => Some(agents),
            Some(_) => return unsupported("the body records are not a plain dict"),
            None => None,
        },
        None => None,
    };
    let mut affects = Vec::with_capacity(ids.len());
    for id in ids {
        let record = match &body_agents {
            Some(agents) => raw_dict_get(agents, PyString::new(py, id).as_any())?,
            None => None,
        };
        affects.push(affect_of(py, record.as_ref())?);
    }
    Ok(Capture { minute, people, affects })
}

/// `capture_social_state_context`: one or two current participants' state, as JSON values.
#[pyfunction]
fn capture_context<'py>(
    world: &Bound<'py, PyAny>,
    participant_ids: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let mut ids = id_set(participant_ids)?;
    if ids.is_empty() || ids.len() > 2 {
        return unsupported("not one or two participants");
    }
    // sorted(): Python orders str by code point, the same as UTF-8 bytes.
    if ids.len() == 2 && ids[0].to_str()?.as_bytes() > ids[1].to_str()?.as_bytes() {
        ids.swap(0, 1);
    }
    let names = ids.iter().map(|id| id.to_str()).collect::<PyResult<Vec<_>>>()?;
    let capture = read_capture(world, &names)?;
    let participants = PyDict::new(py);
    for ((id, person), affect) in ids.iter().zip(&capture.people).zip(&capture.affects) {
        let state = PyDict::new(py);
        state.set_item(intern!(py, "stress"), person.stress)?;
        state.set_item(intern!(py, "trust"), person.trust)?;
        let needs = PyDict::new(py);
        for (name, value) in NEEDS.iter().zip(person.needs) {
            needs.set_item(PyString::intern(py, name), value)?;
        }
        state.set_item(intern!(py, "needs"), needs)?;
        match affect {
            Some(values) => {
                let fields = PyDict::new(py);
                for (name, value) in AFFECT.iter().zip(values) {
                    fields.set_item(PyString::intern(py, name), *value)?;
                }
                state.set_item(intern!(py, "affect"), fields)?;
            }
            None => state.set_item(intern!(py, "affect"), py.None())?,
        }
        participants.set_item(id, state)?;
    }
    let context = PyDict::new(py);
    context.set_item(intern!(py, "schema_version"), 1)?;
    context.set_item(intern!(py, "captured_minute"), capture.minute.into_bound_py_any(py)?)?;
    context.set_item(intern!(py, "participants"), participants)?;
    Ok(context.into_any())
}

struct ContextNames {
    schema_version: Arc<str>,
    captured_minute: Arc<str>,
    participants: Arc<str>,
    stress: Arc<str>,
    trust: Arc<str>,
    needs: Arc<str>,
    affect: Arc<str>,
    need_names: [Arc<str>; 4],
    affect_names: [Arc<str>; 6],
}

static NAMES: LazyLock<ContextNames> = LazyLock::new(|| ContextNames {
    schema_version: Arc::from("schema_version"),
    captured_minute: Arc::from("captured_minute"),
    participants: Arc::from("participants"),
    stress: Arc::from("stress"),
    trust: Arc::from("trust"),
    needs: Arc::from("needs"),
    affect: Arc::from("affect"),
    need_names: NEEDS.map(Arc::from),
    affect_names: AFFECT.map(Arc::from),
});

/// `capture_context` of two different people as the value of an event's payload.
pub(crate) fn context_value(world: &Bound<'_, PyAny>, first: &str, second: &str) -> PyResult<V> {
    let (low, high) = if first.as_bytes() <= second.as_bytes() { (first, second) } else { (second, first) };
    let ids = [low, high];
    let capture = read_capture(world, &ids)?;
    let names = &*NAMES;
    let mut participants = IndexMap::with_capacity(2);
    for ((id, person), affect) in ids.iter().zip(&capture.people).zip(&capture.affects) {
        let mut state = IndexMap::with_capacity(4);
        state.insert(names.stress.clone(), V::Float(person.stress));
        state.insert(names.trust.clone(), V::Float(person.trust));
        let needs = names.need_names.iter().zip(person.needs).map(|(name, value)| (name.clone(), V::Float(value)));
        state.insert(names.needs.clone(), V::Map(needs.collect()));
        let affect = match affect {
            Some(values) => {
                V::Map(names.affect_names.iter().zip(values).map(|(name, value)| (name.clone(), V::Float(*value))).collect())
            }
            None => V::Null,
        };
        state.insert(names.affect.clone(), affect);
        participants.insert(Arc::from(*id), V::Map(state));
    }
    let mut context = IndexMap::with_capacity(3);
    context.insert(names.schema_version.clone(), V::Int(1));
    context.insert(names.captured_minute.clone(), V::Int(capture.minute));
    context.insert(names.participants.clone(), V::Map(participants));
    Ok(V::Map(context))
}

/// The affect of a body record: the values of `AFFECT`, or None without a record that has them all.
fn affect_of<'py>(py: Python<'py>, record: Option<&Bound<'py, PyAny>>) -> PyResult<Option<[f64; 6]>> {
    let Some(record) = record else {
        return Ok(None);
    };
    let Some(fields) = as_readable_dict(record) else {
        if record.is_instance_of::<PyDict>() {
            return unsupported("a body record is another kind of dict");
        }
        return Ok(None);
    };
    let mut affect = [0.0; 6];
    for (slot, name) in affect.iter_mut().zip(AFFECT) {
        let key = PyString::intern(py, name);
        let Some(value) = fields.get_item(&key)? else {
            return Ok(None);
        };
        if !is_unit(&value) {
            return unsupported("an affect value is for the Python rule to judge");
        }
        *slot = value.extract()?;
    }
    Ok(Some(affect))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(validate_context, m)?)?;
    m.add_function(wrap_pyfunction!(capture_context, m)?)?;
    Ok(())
}
