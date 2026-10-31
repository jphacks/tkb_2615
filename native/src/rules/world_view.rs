//! What `WorldView.from_world` and `WorldView.after_state_update` provide (`policies/world_view.py`),
//! built from the rows: a detached snapshot in plain Rust values, which Rust rules read directly.
//!
//! `view_after_movement` and `view_for_proposals` replace the two steps of the phases that make a
//! view (experiments/phases/segments.py). They return a real `WorldView` whose `agents`,
//! `facilities`, `zone_members` and `group_members` are made when a Python reader first asks
//! (`WorldView.__getattr__`), from the same snapshot. Anything this module is not sure of says
//! `Unsupported` before anything has changed, and `WorldView.from_world` runs and raises its own
//! text. A value of another type than the Python view would hold is left to Python, too: a typed
//! field here could not keep it, and `1` and `1.0` would hash differently.
//!
//! What stays in Python, called only when its input changes: parsing a person's context
//! (`PersonContext`), and a zone's conditions (`zone_environment_view` and the formulas it selects),
//! each kept here until the Python caches of the phases replace it. No coefficient lives in this
//! file.
//!
//! The rows are not read through `AgentView.__dict__` any more: the psychology and social kernels
//! take `view_of`. Rules at a later phase than the view (the information kernel) take `group_of`,
//! so the household in `population_presence_v1` overrides `profile.group_id` in one place.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyFloat, PyInt, PyString, PyTuple, PyType};
use pyo3::IntoPyObjectExt;
use rustc_hash::FxHashMap;
use sha2::{Digest, Sha256};

use crate::core::mathx::{py_fsum, py_min};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::{mirror_of, raw_dict_get, raw_dict_set, NamespaceMirror};
use crate::state::entities::{entities_of, raw_setattr, Activity, Entities, EntityStore, Mobility, NeedRow, RequestStatus};
use crate::state::information::{InformationRow, NAME as INFORMATION};
use crate::state::psychological_distress::{DistressRow, NAME as DISTRESS};
use crate::{write_json_string, Encoder, Unsupported};

fn unsupported<T>(reason: &'static str) -> PyResult<T> {
    Err(Unsupported::new_err(reason))
}

// ---- The snapshot ---------------------------------------------------------------------------------

/// What is read of a person's context: `_fields_of_person`.
#[derive(Default)]
pub(crate) struct PersonFields {
    pub(crate) caregiver_ids: Vec<Arc<str>>,
    pub(crate) care_for_ids: Vec<Arc<str>>,
    pub(crate) parent_ids: Vec<Arc<str>>,
    pub(crate) roles: Vec<Arc<str>>,
    pub(crate) usual_wake_minute: Option<i64>,
    pub(crate) usual_sleep_minute: Option<i64>,
    pub(crate) required_sleep_minutes: Option<i64>,
    pub(crate) personal_context_present: bool,
}

/// `ZoneDemandView`.
#[derive(Clone)]
pub(crate) struct DemandModifier {
    pub(crate) action: Arc<str>,
    pub(crate) probability_multiplier: f64,
    pub(crate) duration_multiplier: f64,
    pub(crate) modifier_event_ids: Vec<Arc<str>>,
    pub(crate) assumption_ids: Vec<Arc<str>>,
}

/// `ZoneEnvironmentView`, kept while the Python cache keeps the object.
struct ZoneConditions {
    ambient_noise_level: f64,
    rest_efficiency_multiplier: f64,
    noise_modifier_event_ids: Vec<Arc<str>>,
    noise_assumption_ids: Vec<Arc<str>>,
    demand_modifiers: Vec<DemandModifier>,
    min_rest_decision_minutes: i64,
    max_rest_decision_minutes: i64,
    ventilation_effectiveness: f64,
    lighting_level: f64,
    privacy_level: f64,
    temperature_c: f64,
    ventilation_hygiene_growth_per_minute: f64,
    privacy_discontent_per_minute: f64,
    temperature_stress_per_minute: f64,
    condition_modifier_event_ids: Vec<Arc<str>>,
    condition_assumption_ids: Vec<Arc<str>>,
}

/// What everyone in a zone shares of an `AgentView`.
pub(crate) struct ViewZone {
    pub(crate) id: Arc<str>,
    pub(crate) environmental_stress_per_minute: f64,
    pub(crate) environmental_stressor_event_ids: Vec<Arc<str>>,
    pub(crate) environmental_assumption_source_id: Option<Arc<str>>,
    pub(crate) ambient_noise_level: f64,
    pub(crate) rest_efficiency_multiplier: f64,
    pub(crate) zone_environment_modifier_event_ids: Vec<Arc<str>>,
    pub(crate) zone_environment_assumption_ids: Vec<Arc<str>>,
    pub(crate) ventilation_effectiveness: f64,
    pub(crate) lighting_level: f64,
    pub(crate) privacy_level: f64,
    pub(crate) temperature_c: f64,
    pub(crate) ventilation_hygiene_growth_per_minute: f64,
    pub(crate) privacy_discontent_per_minute: f64,
    pub(crate) condition_modifier_event_ids: Vec<Arc<str>>,
    pub(crate) condition_assumption_ids: Vec<Arc<str>>,
    pub(crate) demand_modifiers: Vec<DemandModifier>,
    pub(crate) min_rest_decision_minutes: i64,
    pub(crate) max_rest_decision_minutes: i64,
    pub(crate) sanitation_level: f64,
}

/// One entry of `misinformation_effect_sources`.
#[derive(Clone)]
pub(crate) struct MisinformationSource {
    pub(crate) message_id: Arc<str>,
    pub(crate) origin_event_id: Arc<str>,
    pub(crate) last_event_id: Arc<str>,
    pub(crate) source_id: Arc<str>,
    pub(crate) time: i64,
}

/// A person's values from `body_state_v1`, with `from_world`'s defaults.
#[derive(Clone, Copy)]
pub(crate) struct BodyFields {
    pub(crate) sleep_debt_minutes: f64,
    pub(crate) uncertainty: f64,
    pub(crate) reassurance: f64,
    pub(crate) discontent: f64,
    pub(crate) positive_affect: f64,
    pub(crate) negative_affect: f64,
    pub(crate) hygiene_need: f64,
}

const NO_BODY: BodyFields = BodyFields {
    sleep_debt_minutes: 0.0,
    uncertainty: 0.0,
    reassurance: 0.0,
    discontent: 0.0,
    positive_affect: 0.5,
    negative_affect: 0.0,
    hygiene_need: 0.0,
};

/// `AgentView`, except what `zone` and `person` hold.
#[derive(Clone)]
pub(crate) struct ViewAgent {
    pub(crate) id: Arc<str>,
    /// `household_id` of `population_presence_v1`, over `profile.group_id`.
    pub(crate) group_id: Option<Arc<str>>,
    /// The index in `ViewData::zones`.
    pub(crate) zone: usize,
    pub(crate) mobility: Mobility,
    pub(crate) support_need: f64,
    pub(crate) stress_sensitivity: f64,
    pub(crate) rng_key: Arc<str>,
    pub(crate) activity: Activity,
    pub(crate) target_id: Option<Arc<str>>,
    pub(crate) needs: NeedRow,
    pub(crate) stress: f64,
    pub(crate) chronic_distress: f64,
    pub(crate) trust: f64,
    pub(crate) next_decision_at: i64,
    pub(crate) queue_task_id: Option<Arc<str>>,
    pub(crate) queue_kind: Option<Arc<str>>,
    pub(crate) queue_target_id: Option<Arc<str>>,
    pub(crate) queue_wait_minutes: i64,
    pub(crate) support_request_id: Option<Arc<str>>,
    pub(crate) support_request_status: Option<RequestStatus>,
    pub(crate) support_assigned_staff_id: Option<Arc<str>>,
    pub(crate) support_wait_minutes: i64,
    pub(crate) support_cooldown_remaining_minutes: i64,
    pub(crate) information_query_pressure: f64,
    pub(crate) misinformation_belief_pressure: f64,
    pub(crate) misinformation_effect_sources: Vec<MisinformationSource>,
    pub(crate) body: BodyFields,
    pub(crate) person: Arc<PersonFields>,
}

/// `FacilityView`.
pub(crate) struct ViewFacility {
    pub(crate) id: Arc<str>,
    pub(crate) kind: Arc<str>,
    pub(crate) zone_id: Arc<str>,
    pub(crate) capacity: i64,
    pub(crate) working_units: i64,
    pub(crate) active_agent_ids: Vec<Arc<str>>,
    pub(crate) queue_agent_ids: Vec<Arc<str>>,
    pub(crate) resource_active_agent_ids: Vec<Arc<str>>,
    pub(crate) resource_queue_agent_ids: Vec<Arc<str>>,
    pub(crate) facility_event_context_ids: Vec<Arc<str>>,
}

/// All of a `WorldView`. Nothing here refers to Python, so a rule can read it without the GIL.
pub(crate) struct ViewData {
    pub(crate) minute_index: i64,
    pub(crate) resource_service_enabled: bool,
    pub(crate) autonomy_enabled: bool,
    pub(crate) has_population_context: bool,
    /// Read from a Python view: only what the update kernels read is filled, and a zone is one
    /// person's. See `from_python_view`.
    #[allow(dead_code)]
    pub(crate) partial: bool,
    /// In ID order.
    pub(crate) zones: Vec<Arc<ViewZone>>,
    /// In ID order, as `sorted(world.agents.items())`.
    pub(crate) agents: Vec<ViewAgent>,
    /// In ID order.
    pub(crate) facilities: Vec<Arc<ViewFacility>>,
}

impl ViewData {
    pub(crate) fn zone_of(&self, agent: &ViewAgent) -> &ViewZone {
        &self.zones[agent.zone]
    }

    /// The index of a person in `agents`.
    pub(crate) fn index_of(&self, id: &str) -> Option<usize> {
        self.agents.binary_search_by(|agent| (*agent.id).cmp(id)).ok()
    }

    /// `WorldView.zone_members`: each zone with people, in ID order, with their indexes in ID order.
    pub(crate) fn zone_members(&self) -> Vec<(&Arc<str>, Vec<usize>)> {
        let mut members: Vec<(&Arc<str>, Vec<usize>)> =
            self.zones.iter().map(|zone| (&zone.id, Vec::new())).collect();
        for (index, agent) in self.agents.iter().enumerate() {
            members[agent.zone].1.push(index);
        }
        members.retain(|(_, people)| !people.is_empty());
        members
    }

    /// `WorldView.group_members`: each group, in ID order, with its people's indexes in ID order.
    pub(crate) fn group_members(&self) -> Vec<(&Arc<str>, Vec<usize>)> {
        let mut positions: FxHashMap<&str, usize> = FxHashMap::default();
        let mut members: Vec<(&Arc<str>, Vec<usize>)> = Vec::new();
        for (index, agent) in self.agents.iter().enumerate() {
            if let Some(group) = &agent.group_id {
                let position = *positions.entry(group).or_insert_with(|| {
                    members.push((group, Vec::new()));
                    members.len() - 1
                });
                members[position].1.push(index);
            }
        }
        members.sort_unstable_by(|first, second| first.0.cmp(second.0));
        members
    }
}

// ---- Reading what is not a row yet ----------------------------------------------------------------

/// `model_state.get(name)`.
fn state_get<'py>(model_state: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    model_state.call_method1(intern!(model_state.py(), "get"), (name,))
}

fn exact_float(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    match value.downcast_exact::<PyFloat>() {
        Ok(number) => Ok(number.value()),
        Err(_) => unsupported("a view value is not a float"),
    }
}

fn exact_i64(value: &Bound<'_, PyAny>) -> PyResult<i64> {
    match value.downcast_exact::<PyInt>() {
        Ok(number) => number.extract().map_err(|_| Unsupported::new_err("an int is too large")),
        Err(_) => unsupported("a view value is not an int"),
    }
}

fn exact_text(value: &Bound<'_, PyAny>) -> PyResult<Arc<str>> {
    match value.downcast_exact::<PyString>() {
        Ok(text) => Ok(Arc::from(text.to_str().map_err(|_| Unsupported::new_err("not UTF-8"))?)),
        Err(_) => unsupported("a view value is not a str"),
    }
}

fn optional_text(value: &Bound<'_, PyAny>) -> PyResult<Option<Arc<str>>> {
    if value.is_none() {
        Ok(None)
    } else {
        exact_text(value).map(Some)
    }
}

fn optional_i64(value: &Bound<'_, PyAny>) -> PyResult<Option<i64>> {
    if value.is_none() {
        Ok(None)
    } else {
        exact_i64(value).map(Some)
    }
}

fn text_tuple(value: &Bound<'_, PyAny>) -> PyResult<Vec<Arc<str>>> {
    let Ok(items) = value.downcast_exact::<PyTuple>() else {
        return unsupported("a view value is not a tuple");
    };
    items.iter().map(|item| exact_text(&item)).collect()
}

/// A value as `isinstance(_, dict)` sees it: a plain or tracked dict, or None for no dict. Another
/// class of dict may read differently, and is left to Python.
fn dict_or_nothing<'a, 'py>(value: &'a Bound<'py, PyAny>) -> PyResult<Option<&'a Bound<'py, PyDict>>> {
    match as_readable_dict(value) {
        Some(dict) => Ok(Some(dict)),
        None if value.is_instance_of::<PyDict>() => unsupported("a state is another kind of dict"),
        None => Ok(None),
    }
}

/// The dict a `get(key, {})` gave, where a non-dict raises in Python.
fn required_dict<'a, 'py>(value: &'a Bound<'py, PyAny>) -> PyResult<&'a Bound<'py, PyDict>> {
    match as_readable_dict(value) {
        Some(dict) => Ok(dict),
        None => unsupported("a state is not a plain dict"),
    }
}

/// `record[name]`; a missing one raises in Python.
fn field<'py>(record: &Bound<'py, PyDict>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    record.get_item(name)?.ok_or_else(|| Unsupported::new_err("a record's field is missing"))
}

/// `population_presence_v1.people`, for `group_of`; None for no such dict.
pub(crate) fn presence_people_of<'py>(model_state: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = model_state.py();
    let presence = state_get(model_state, intern!(py, "population_presence_v1"))?;
    let Some(state) = dict_or_nothing(&presence)? else {
        return Ok(None);
    };
    let people = state.get_item(intern!(py, "people"))?;
    if let Some(people) = &people {
        if as_readable_dict(people).is_none() {
            return unsupported("the presence state's people are not a dict");
        }
    }
    Ok(people)
}

/// A person's `group_id` in a view: the household of the presence record, or the profile's.
pub(crate) fn group_of(
    people: Option<&Bound<'_, PyAny>>,
    id: &Bound<'_, PyString>,
    profile_group: &Option<Arc<str>>,
) -> PyResult<Option<Arc<str>>> {
    let Some(people) = people else {
        return Ok(profile_group.clone());
    };
    let py = people.py();
    let Some(record) = raw_dict_get(people, id.as_any())? else {
        return Ok(profile_group.clone());
    };
    let Some(record) = dict_or_nothing(&record)? else {
        return Ok(profile_group.clone());
    };
    match record.get_item(intern!(py, "household_id"))? {
        Some(household) => optional_text(&household),
        None => Ok(profile_group.clone()),
    }
}

/// What this module keeps from one view to the next, for one controller. Each entry holds the
/// Python object it was read from, and is good while the Python cache holds that same object. The
/// Python caches decide when something has changed, for both paths.
#[pyclass(module = "sheltersim_native")]
#[derive(Default)]
pub struct ViewCache {
    keys: FxHashMap<Arc<str>, Py<PyString>>,
    people: FxHashMap<Arc<str>, (Py<PyAny>, Arc<PersonFields>)>,
    zones: FxHashMap<Arc<str>, (Py<PyAny>, Arc<ZoneConditions>)>,
    no_person: Arc<PersonFields>,
}

impl ViewCache {
    fn key<'py>(&mut self, py: Python<'py>, id: &Arc<str>) -> Bound<'py, PyString> {
        if let Some(key) = self.keys.get(id) {
            return key.bind(py).clone();
        }
        let key = PyString::new(py, id);
        self.keys.insert(id.clone(), key.clone().unbind());
        key
    }
}

struct PythonHelpers {
    person_entry: Py<PyAny>,
    zone_environment: Py<PyAny>,
    misinformation_applies: Py<PyAny>,
    world_view: Py<PyType>,
    need_view: Py<PyType>,
    agent_view: Py<PyType>,
    facility_view: Py<PyType>,
    demand_view: Py<PyType>,
    new: Py<PyAny>,
}

static HELPERS: GILOnceCell<PythonHelpers> = GILOnceCell::new();

fn helpers(py: Python<'_>) -> PyResult<&PythonHelpers> {
    HELPERS.get_or_try_init(py, || {
        let module = py.import("sheltersim.policies.world_view")?;
        let kind = |name: &str| -> PyResult<Py<PyType>> {
            Ok(module.getattr(name)?.downcast_into::<PyType>()?.unbind())
        };
        Ok(PythonHelpers {
            person_entry: module.getattr("_person_entry")?.unbind(),
            zone_environment: module.getattr("_zone_environment")?.unbind(),
            misinformation_applies: module.getattr("_misinformation_applies")?.unbind(),
            world_view: kind("WorldView")?,
            need_view: kind("NeedView")?,
            agent_view: kind("AgentView")?,
            facility_view: kind("FacilityView")?,
            demand_view: py
                .import("sheltersim.domain.environment")?
                .getattr("ZoneDemandView")?
                .downcast_into::<PyType>()?
                .unbind(),
            new: py.import("builtins")?.getattr("object")?.getattr("__new__")?.unbind(),
        })
    })
}

/// A failure of the Python code this calls is Python's to raise: `from_world` runs it again.
fn call_failed(_error: PyErr) -> PyErr {
    Unsupported::new_err("the Python rule raises")
}

fn read_person(entry: &Bound<'_, PyAny>) -> PyResult<Arc<PersonFields>> {
    let py = entry.py();
    let fields = entry.get_item(2)?;
    let Some(fields) = as_readable_dict(&fields) else {
        return unsupported("a person's fields are not a dict");
    };
    Ok(Arc::new(PersonFields {
        caregiver_ids: text_tuple(&field(fields, intern!(py, "caregiver_ids"))?)?,
        care_for_ids: text_tuple(&field(fields, intern!(py, "care_for_ids"))?)?,
        parent_ids: text_tuple(&field(fields, intern!(py, "parent_ids"))?)?,
        roles: text_tuple(&field(fields, intern!(py, "roles"))?)?,
        usual_wake_minute: optional_i64(&field(fields, intern!(py, "usual_wake_minute"))?)?,
        usual_sleep_minute: optional_i64(&field(fields, intern!(py, "usual_sleep_minute"))?)?,
        required_sleep_minutes: optional_i64(&field(fields, intern!(py, "required_sleep_minutes"))?)?,
        personal_context_present: true,
    }))
}

/// `state.get(name, {})` if both are dicts: one of the dicts `population_context_people` merges.
fn people_of<'py>(
    state: Option<Bound<'py, PyAny>>,
    name: &Bound<'py, PyString>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let Some(state) = state else { return Ok(None) };
    let Some(state) = dict_or_nothing(&state)? else { return Ok(None) };
    let Some(people) = state.get_item(name)? else { return Ok(None) };
    Ok(dict_or_nothing(&people)?.is_some().then_some(people))
}

/// `_parsed_person_contexts` and `_person_fields` of a world: who has a context, and its fields.
/// Merged as `population_context_people` merges them, the later dict winning.
fn person_contexts(
    world: &Bound<'_, PyAny>,
    configs: &Bound<'_, PyDict>,
    model_state: &Bound<'_, PyAny>,
    person_cache: &Bound<'_, PyAny>,
    cache: &mut ViewCache,
) -> PyResult<(FxHashMap<Arc<str>, Arc<PersonFields>>, bool)> {
    let py = world.py();
    let Some(person_cache) = as_readable_dict(person_cache) else {
        return unsupported("the cache of persons is not a dict");
    };
    let sources = [
        people_of(configs.get_item(intern!(py, "population_context"))?, intern!(py, "people"))?,
        people_of(Some(state_get(model_state, intern!(py, "population_context_v1"))?), intern!(py, "people"))?,
        people_of(
            Some(state_get(model_state, intern!(py, "population_dynamics_v1"))?),
            intern!(py, "profile_context"),
        )?,
    ];
    let later_has = |place: usize, id: &Bound<'_, PyAny>| -> PyResult<bool> {
        for later in &sources[place + 1..] {
            if let Some(later) = later {
                if raw_dict_get(later, id)?.is_some() {
                    return Ok(true);
                }
            }
        }
        Ok(false)
    };
    let mut found = FxHashMap::default();
    for (place, source) in sources.iter().enumerate() {
        let Some(source) = source else { continue };
        let Some(source) = as_readable_dict(source) else {
            return unsupported("a people dict is another kind of dict");
        };
        for (id, raw) in source.iter() {
            if later_has(place, &id)? {
                continue;
            }
            let id_text = exact_text(&id)?;
            let mut entry = person_cache.get_item(&id)?;
            let current = match &entry {
                // `entry[0] != raw`, as a NaN in a record parses it again.
                Some(kept) if kept.len()? == 3 => !kept.get_item(0)?.ne(&raw)?,
                _ => false,
            };
            if !current {
                let made = helpers(py)?
                    .person_entry
                    .bind(py)
                    .call1((&id, &raw, person_cache))
                    .map_err(call_failed)?;
                entry = Some(made);
            }
            let Some(entry) = entry else {
                return unsupported("no entry for a person");
            };
            let fields = match cache.people.get(&id_text) {
                Some((kept, fields)) if kept.bind(py).is(&entry) => fields.clone(),
                _ => {
                    let fields = read_person(&entry)?;
                    cache.people.insert(id_text.clone(), (entry.unbind(), fields.clone()));
                    fields
                }
            };
            found.insert(id_text, fields);
        }
    }
    let any = !found.is_empty();
    Ok((found, any))
}

fn read_zone_conditions(view: &Bound<'_, PyAny>) -> PyResult<Arc<ZoneConditions>> {
    let py = view.py();
    let float = |name: &Bound<'_, PyString>| exact_float(&view.getattr(name)?);
    let int = |name: &Bound<'_, PyString>| exact_i64(&view.getattr(name)?);
    let texts = |name: &Bound<'_, PyString>| text_tuple(&view.getattr(name)?);
    let demand = view.getattr(intern!(py, "demand_modifiers"))?;
    let Ok(demand) = demand.downcast_exact::<PyTuple>() else {
        return unsupported("the demand modifiers are not a tuple");
    };
    let mut demand_modifiers = Vec::with_capacity(demand.len());
    for modifier in demand.iter() {
        demand_modifiers.push(DemandModifier {
            action: exact_text(&modifier.getattr(intern!(py, "action"))?)?,
            probability_multiplier: exact_float(&modifier.getattr(intern!(py, "probability_multiplier"))?)?,
            duration_multiplier: exact_float(&modifier.getattr(intern!(py, "duration_multiplier"))?)?,
            modifier_event_ids: text_tuple(&modifier.getattr(intern!(py, "modifier_event_ids"))?)?,
            assumption_ids: text_tuple(&modifier.getattr(intern!(py, "assumption_ids"))?)?,
        });
    }
    Ok(Arc::new(ZoneConditions {
        ambient_noise_level: float(intern!(py, "ambient_noise_level"))?,
        rest_efficiency_multiplier: float(intern!(py, "rest_efficiency_multiplier"))?,
        noise_modifier_event_ids: texts(intern!(py, "noise_modifier_event_ids"))?,
        noise_assumption_ids: texts(intern!(py, "noise_assumption_ids"))?,
        demand_modifiers,
        min_rest_decision_minutes: int(intern!(py, "min_rest_decision_minutes"))?,
        max_rest_decision_minutes: int(intern!(py, "max_rest_decision_minutes"))?,
        ventilation_effectiveness: float(intern!(py, "ventilation_effectiveness"))?,
        lighting_level: float(intern!(py, "lighting_level"))?,
        privacy_level: float(intern!(py, "privacy_level"))?,
        temperature_c: float(intern!(py, "temperature_c"))?,
        ventilation_hygiene_growth_per_minute: float(intern!(py, "ventilation_hygiene_growth_per_minute"))?,
        privacy_discontent_per_minute: float(intern!(py, "privacy_discontent_per_minute"))?,
        temperature_stress_per_minute: float(intern!(py, "temperature_stress_per_minute"))?,
        condition_modifier_event_ids: texts(intern!(py, "condition_modifier_event_ids"))?,
        condition_assumption_ids: texts(intern!(py, "condition_assumption_ids"))?,
    }))
}

/// An active stressor of a zone: `active_environmental_stress`.
struct Stressor {
    id: Arc<str>,
    stress_per_minute: f64,
    start_event_id: Arc<str>,
    source_id: Option<Arc<str>>,
}

/// The active stressors of each zone, by ID.
fn active_stressors(
    model_state: &Bound<'_, PyAny>,
    zone_ids: &[Arc<str>],
) -> PyResult<FxHashMap<Arc<str>, Vec<Stressor>>> {
    let py = model_state.py();
    let mut found: FxHashMap<Arc<str>, Vec<Stressor>> = FxHashMap::default();
    let state = state_get(model_state, intern!(py, "infrastructure_v1"))?;
    let Some(state) = dict_or_nothing(&state)? else {
        return Ok(found);
    };
    let Some(stressors) = state.get_item(intern!(py, "environmental_stressors"))? else {
        return unsupported("the infrastructure state has no stressors");
    };
    for (id, record) in required_dict(&stressors)?.iter() {
        let record = required_dict(&record)?;
        if !field(record, intern!(py, "active"))?.is_truthy()? {
            continue;
        }
        let zone = exact_text(&field(record, intern!(py, "zone_id"))?)?;
        if zone_ids.binary_search(&zone).is_err() {
            continue;
        }
        found.entry(zone).or_default().push(Stressor {
            id: exact_text(&id)?,
            stress_per_minute: exact_float(&field(record, intern!(py, "stress_per_minute"))?)?,
            start_event_id: exact_text(&field(record, intern!(py, "start_event_id"))?)?,
            source_id: optional_text(&field(record, intern!(py, "source_id"))?)?,
        });
    }
    for entries in found.values_mut() {
        entries.sort_unstable_by(|first, second| first.id.cmp(&second.id));
    }
    Ok(found)
}

fn sorted_unique(mut items: Vec<Arc<str>>) -> Vec<Arc<str>> {
    items.sort_unstable();
    items.dedup();
    items
}

/// The zones of a view: the head of `from_world`.
fn read_zones(
    controller: &Bound<'_, PyAny>,
    world: &Bound<'_, PyAny>,
    configs: &Bound<'_, PyDict>,
    model_state: &Bound<'_, PyAny>,
    cache: &mut ViewCache,
) -> PyResult<Vec<Arc<ViewZone>>> {
    let py = world.py();
    let zones = world.getattr(intern!(py, "zones"))?;
    let Some(zones) = as_readable_dict(&zones) else {
        return unsupported("the zones are not a plain dict");
    };
    let mut zone_ids = Vec::with_capacity(zones.len());
    for id in zones.keys() {
        zone_ids.push(exact_text(&id)?);
    }
    zone_ids.sort_unstable();

    // What _zone_environment checks of its cache.
    let environment_cache = controller.getattr(intern!(py, "_zone_environments"))?;
    let Some(environments) = as_readable_dict(&environment_cache) else {
        return unsupported("the cache of zones is not a dict");
    };
    let state = state_get(model_state, intern!(py, "zone_environment_v1"))?;
    let has_state = state.is_instance_of::<PyDict>();
    let modifiers = if has_state {
        state.call_method1(intern!(py, "get"), (intern!(py, "modifiers"),))?
    } else {
        py.None().into_bound(py)
    };
    let usable = environments.get_item(intern!(py, "configs"))?.is_some_and(|kept| kept.is(configs))
        && environments
            .get_item(intern!(py, "has_state"))?
            .is_some_and(|kept| kept.is_truthy().is_ok_and(|flag| flag == has_state))
        && match environments.get_item(intern!(py, "modifiers"))? {
            Some(kept) => !kept.ne(&modifiers)?,
            None => false,
        };
    let views = if usable { environments.get_item(intern!(py, "views"))? } else { None };

    let stressors = active_stressors(model_state, &zone_ids)?;
    let staff_operations = state_get(model_state, intern!(py, "staff_operations_v1"))?;
    let sanitation_records = match dict_or_nothing(&staff_operations)? {
        Some(state) => state.get_item(intern!(py, "zones"))?,
        None => None,
    };
    let mut result = Vec::with_capacity(zone_ids.len());
    for id in zone_ids {
        let key = cache.key(py, &id);
        let view = match &views {
            Some(views) => raw_dict_get(required_dict(views)?.as_any(), key.as_any())?,
            None => None,
        };
        let view = match view {
            Some(view) => view,
            None => helpers(py)?
                .zone_environment
                .bind(py)
                .call1((world, &key, &environment_cache))
                .map_err(call_failed)?,
        };
        let conditions = match cache.zones.get(&id) {
            Some((kept, conditions)) if kept.bind(py).is(&view) => conditions.clone(),
            _ => {
                let conditions = read_zone_conditions(&view)?;
                cache.zones.insert(id.clone(), (view.unbind(), conditions.clone()));
                conditions
            }
        };
        let entries = stressors.get(&id).map_or(&[][..], Vec::as_slice);
        let rates: Vec<f64> = entries.iter().map(|entry| entry.stress_per_minute).collect();
        let Some(base_stress) = py_fsum(&rates) else {
            return unsupported("a sum of stress rates overflows");
        };
        let sanitation = match &sanitation_records {
            Some(records) => match raw_dict_get(required_dict(records)?.as_any(), key.as_any())? {
                Some(record) => match required_dict(&record)?.get_item(intern!(py, "sanitation"))? {
                    Some(value) => exact_float(&value)?,
                    None => 1.0,
                },
                None => 1.0,
            },
            None => 1.0,
        };
        let mut event_ids: Vec<Arc<str>> = entries.iter().map(|entry| entry.start_event_id.clone()).collect();
        event_ids.extend(conditions.condition_modifier_event_ids.iter().cloned());
        result.push(Arc::new(ViewZone {
            environmental_stress_per_minute: base_stress + conditions.temperature_stress_per_minute,
            environmental_stressor_event_ids: sorted_unique(event_ids),
            environmental_assumption_source_id: match conditions.condition_assumption_ids.first() {
                Some(first) if conditions.temperature_stress_per_minute != 0.0 => Some(first.clone()),
                _ => entries.first().and_then(|entry| entry.source_id.clone()),
            },
            ambient_noise_level: conditions.ambient_noise_level,
            rest_efficiency_multiplier: conditions.rest_efficiency_multiplier,
            zone_environment_modifier_event_ids: conditions.noise_modifier_event_ids.clone(),
            zone_environment_assumption_ids: conditions.noise_assumption_ids.clone(),
            ventilation_effectiveness: conditions.ventilation_effectiveness,
            lighting_level: conditions.lighting_level,
            privacy_level: conditions.privacy_level,
            temperature_c: conditions.temperature_c,
            ventilation_hygiene_growth_per_minute: conditions.ventilation_hygiene_growth_per_minute,
            privacy_discontent_per_minute: conditions.privacy_discontent_per_minute,
            condition_modifier_event_ids: conditions.condition_modifier_event_ids.clone(),
            condition_assumption_ids: conditions.condition_assumption_ids.clone(),
            demand_modifiers: conditions.demand_modifiers.clone(),
            min_rest_decision_minutes: conditions.min_rest_decision_minutes,
            max_rest_decision_minutes: conditions.max_rest_decision_minutes,
            sanitation_level: sanitation,
            id,
        }));
    }
    Ok(result)
}

/// What the rows say of the people, in ID order, and of the facilities.
fn read_rows(
    rows: &Entities,
    zones: &[Arc<ViewZone>],
    no_person: &Arc<PersonFields>,
    minute: i64,
) -> PyResult<(Vec<ViewAgent>, Vec<ViewFacility>)> {
    let mut first_tasks: FxHashMap<&str, &crate::state::entities::TaskRow> = FxHashMap::default();
    for (_, task) in rows.active_tasks.iter() {
        if let Some(agent) = &task.agent_id {
            if matches!(&*task.kind, "toilet_queue" | "resource_service_queue") {
                first_tasks.entry(agent).or_insert(task);
            }
        }
    }
    let waited = |since: i64| -> PyResult<i64> {
        match minute.checked_sub(since) {
            Some(wait) => Ok(wait.max(0)),
            None => unsupported("a minute is out of range"),
        }
    };
    let mut people: Vec<_> = rows.agents.iter().collect();
    people.sort_unstable_by(|first, second| first.0.cmp(second.0));
    let mut agents = Vec::with_capacity(people.len());
    for (id, agent) in people {
        if **id != *agent.profile.id {
            return unsupported("a person is under another ID");
        }
        let Ok(zone) = zones.binary_search_by(|zone| zone.id.cmp(&agent.zone_id)) else {
            return unsupported("a person is in an unknown zone");
        };
        let task = first_tasks.get(&**id).copied();
        let request = agent.support_request_id.as_ref().and_then(|request| rows.support_requests.get(request));
        let Some(cooldown) = agent.support_cooldown_until.checked_sub(minute) else {
            return unsupported("a minute is out of range");
        };
        agents.push(ViewAgent {
            id: id.clone(),
            group_id: agent.profile.group_id.clone(),
            zone,
            mobility: agent.profile.mobility,
            support_need: agent.profile.support_need,
            stress_sensitivity: agent.profile.stress_sensitivity,
            rng_key: agent.profile.rng_key.clone(),
            activity: agent.activity,
            target_id: agent.target_id.clone(),
            needs: agent.needs.clone(),
            stress: agent.stress,
            chronic_distress: 0.0,
            trust: agent.trust,
            next_decision_at: agent.next_decision_at,
            queue_task_id: task.map(|task| task.id.clone()),
            queue_kind: task.map(|task| task.kind.clone()),
            queue_target_id: task.and_then(|task| task.resource_id.clone()),
            queue_wait_minutes: match task {
                Some(task) => waited(task.start_minute)?,
                None => 0,
            },
            support_request_id: agent.support_request_id.clone(),
            support_request_status: request.map(|request| request.status),
            support_assigned_staff_id: request.and_then(|request| request.assigned_staff_id.clone()),
            support_wait_minutes: match request {
                Some(request) => waited(request.requested_minute)?,
                None => 0,
            },
            support_cooldown_remaining_minutes: cooldown.max(0),
            information_query_pressure: 0.0,
            misinformation_belief_pressure: 0.0,
            misinformation_effect_sources: Vec::new(),
            body: NO_BODY,
            person: no_person.clone(),
        });
    }
    let mut places: Vec<_> = rows.facilities.iter().collect();
    places.sort_unstable_by(|first, second| first.0.cmp(second.0));
    let mut facilities = Vec::with_capacity(places.len());
    for (id, facility) in places {
        if **id != *facility.id {
            return unsupported("a facility is under another ID");
        }
        facilities.push(ViewFacility {
            id: id.clone(),
            kind: facility.kind.clone(),
            zone_id: facility.zone_id.clone(),
            capacity: facility.capacity,
            working_units: facility.working_units,
            active_agent_ids: facility.active_agent_ids.clone(),
            queue_agent_ids: facility.queue_agent_ids.clone(),
            resource_active_agent_ids: facility.resource_active_agent_ids.clone(),
            resource_queue_agent_ids: facility.resource_queue_agent_ids.clone(),
            facility_event_context_ids: Vec::new(),
        });
    }
    Ok((agents, facilities))
}

/// `min(1.0, pressure)` and the sources of the false claims a person believes, in `from_world`'s
/// order. `beliefs_parse` holds, so the claims and beliefs would parse.
fn misinformation_of(row: &InformationRow, id: &str) -> (f64, Vec<MisinformationSource>) {
    let mut pressure = 0.0;
    let mut sources = Vec::new();
    let Some(records) = row.beliefs.get(id) else {
        return (0.0, sources);
    };
    let mut entries: Vec<_> = records.iter().collect();
    entries.sort_unstable_by(|first, second| first.0.cmp(second.0));
    for (message_id, belief) in entries {
        let Some(message) = row.messages.get(message_id) else {
            continue;
        };
        if &*message.truth_status == "false" && &*belief.belief != "unknown" && belief.belief_strength > 0.5 {
            pressure += 2.0 * (belief.belief_strength - 0.5);
            sources.push(MisinformationSource {
                message_id: message_id.clone(),
                origin_event_id: message.origin_event_id.clone(),
                last_event_id: belief.last_event_id.clone(),
                source_id: message.source_id.clone(),
                time: message.time,
            });
        }
    }
    (py_min(1.0, pressure), sources)
}

fn read_information(
    world: &Bound<'_, PyAny>,
    state: &Bound<'_, PyAny>,
    applies: bool,
    agents: &mut [ViewAgent],
) -> PyResult<()> {
    if state.is_none() {
        return Ok(());
    }
    let mirror = mirror_of(world, INFORMATION)?;
    NamespaceMirror::with_row::<InformationRow, _>(&mirror, |row| {
        if applies && !row.beliefs_parse() {
            return unsupported("the claims or beliefs do not parse");
        }
        for agent in agents.iter_mut() {
            let Some(pressure) = row.query_pressure.get(&*agent.id) else {
                return unsupported("a person has no query pressure");
            };
            agent.information_query_pressure = *pressure;
            if applies {
                let (pressure, sources) = misinformation_of(row, &agent.id);
                agent.misinformation_belief_pressure = pressure;
                agent.misinformation_effect_sources = sources;
            }
        }
        Ok(())
    })
}

/// The long-term distress and body values of each person: what the update phase writes outside
/// the entities, and so what `after_state_update` reads again.
fn read_body_and_distress(
    world: &Bound<'_, PyAny>,
    model_state: &Bound<'_, PyAny>,
    agents: &mut [ViewAgent],
    keys: &[Py<PyString>],
) -> PyResult<()> {
    let py = world.py();
    let distress = state_get(model_state, intern!(py, "psychological_distress_v1"))?;
    if dict_or_nothing(&distress)?.is_some() {
        // A namespace of another form is not the row's: this says Unsupported.
        let mirror = mirror_of(world, DISTRESS)?;
        NamespaceMirror::with_row::<DistressRow, _>(&mirror, |row| {
            for agent in agents.iter_mut() {
                agent.chronic_distress =
                    row.agents.get(&*agent.id).map_or(0.0, |record| record.long_term_distress);
            }
            Ok(())
        })?;
    } else {
        for agent in agents.iter_mut() {
            agent.chronic_distress = 0.0;
        }
    }
    let body_state = state_get(model_state, intern!(py, "body_state_v1"))?;
    let records = match dict_or_nothing(&body_state)? {
        Some(state) => state.get_item(intern!(py, "agents"))?,
        None => None,
    };
    let Some(records) = records else {
        for agent in agents.iter_mut() {
            agent.body = NO_BODY;
        }
        return Ok(());
    };
    let records = required_dict(&records)?;
    for (agent, key) in agents.iter_mut().zip(keys) {
        let Some(record) = raw_dict_get(records.as_any(), key.bind(py).as_any())? else {
            agent.body = NO_BODY;
            continue;
        };
        let record = required_dict(&record)?;
        let value = |name: &Bound<'_, PyString>, default: f64| -> PyResult<f64> {
            match record.get_item(name)? {
                Some(value) => exact_float(&value),
                None => Ok(default),
            }
        };
        agent.body = BodyFields {
            sleep_debt_minutes: value(intern!(py, "sleep_debt_minutes"), NO_BODY.sleep_debt_minutes)?,
            uncertainty: value(intern!(py, "uncertainty"), NO_BODY.uncertainty)?,
            reassurance: value(intern!(py, "reassurance"), NO_BODY.reassurance)?,
            discontent: value(intern!(py, "discontent"), NO_BODY.discontent)?,
            positive_affect: value(intern!(py, "positive_affect"), NO_BODY.positive_affect)?,
            negative_affect: value(intern!(py, "negative_affect"), NO_BODY.negative_affect)?,
            hygiene_need: value(intern!(py, "hygiene_need"), NO_BODY.hygiene_need)?,
        };
    }
    Ok(())
}

/// `"water_station" in kind.casefold()`. Only ASCII is folded here; other text asks Python.
fn is_water_station(kind: &str, py: Python<'_>) -> PyResult<bool> {
    if kind.is_ascii() {
        return Ok(kind.to_ascii_lowercase().contains("water_station"));
    }
    let folded = PyString::new(py, kind).call_method0(intern!(py, "casefold"))?;
    Ok(folded.downcast_exact::<PyString>()?.to_str()?.contains("water_station"))
}

/// `facility_event_context_ids` of the facilities.
fn read_facility_contexts(
    world: &Bound<'_, PyAny>,
    model_state: &Bound<'_, PyAny>,
    facilities: &mut [ViewFacility],
) -> PyResult<()> {
    let py = world.py();
    let operational = state_get(model_state, intern!(py, "operational_events_v1"))?;
    let water_outages = if operational.is_none() {
        // `get(name, {})` of a stored None raises in Python.
        if model_state.contains(intern!(py, "operational_events_v1"))? {
            return unsupported("the operational state is None");
        }
        None
    } else {
        required_dict(&operational)?.get_item(intern!(py, "water_outages"))?
    };
    let water_outages = match &water_outages {
        Some(outages) => required_dict(outages)?.values().iter().collect::<Vec<_>>(),
        None => Vec::new(),
    };
    let infrastructure = state_get(model_state, intern!(py, "infrastructure_v1"))?;
    let outages = match dict_or_nothing(&infrastructure)? {
        Some(state) => match state.get_item(intern!(py, "outages"))? {
            Some(outages) => required_dict(&outages)?.values().iter().collect::<Vec<_>>(),
            None => return unsupported("the infrastructure state has no outages"),
        },
        None => Vec::new(),
    };
    let event_log = world.getattr(intern!(py, "event_log"))?;
    for facility in facilities.iter_mut() {
        let mut ids: Vec<Arc<str>> = Vec::new();
        if facility.working_units < facility.capacity {
            let applied = event_log.call_method1(intern!(py, "applied_facility_event_ids"), (&*facility.id,))?;
            for id in applied.try_iter()? {
                ids.push(exact_text(&id?)?);
            }
        }
        if !water_outages.is_empty() && is_water_station(&facility.kind, py)? {
            for outage in &water_outages {
                let outage = required_dict(outage)?;
                if !field(outage, intern!(py, "active"))?.is_truthy()? {
                    continue;
                }
                let kind = exact_text(&field(outage, intern!(py, "target_kind"))?)?;
                let target = exact_text(&field(outage, intern!(py, "target_id"))?)?;
                let hit = (&*kind == "zone" && target == facility.zone_id)
                    || (&*kind == "facility" && target == facility.id);
                if hit {
                    ids.push(exact_text(&field(outage, intern!(py, "outage_event_id"))?)?);
                }
            }
        }
        for outage in &outages {
            let outage = required_dict(outage)?;
            if field(outage, intern!(py, "active"))?.is_truthy()?
                && field(outage, intern!(py, "affected_facility_ids"))?.contains(&*facility.id)?
            {
                ids.push(exact_text(&field(outage, intern!(py, "start_event_id"))?)?);
            }
        }
        facility.facility_event_context_ids = sorted_unique(ids);
    }
    Ok(())
}

fn configs_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let configs = world.getattr(intern!(world.py(), "model_configs"))?;
    if as_readable_dict(&configs).is_none() {
        return unsupported("the configs are not a plain dict");
    }
    Ok(configs)
}

fn cache_of<'py>(controller: &Bound<'py, PyAny>) -> PyResult<Bound<'py, ViewCache>> {
    let py = controller.py();
    let name = intern!(py, "_rust_view_cache");
    let found = controller.getattr(name).map_err(|_| Unsupported::new_err("the controller keeps no view cache"))?;
    if let Ok(cache) = found.downcast::<ViewCache>() {
        return Ok(cache.clone());
    }
    let cache = Bound::new(py, ViewCache::default())?;
    controller.setattr(name, &cache)?;
    Ok(cache)
}

/// `from_world`, without its hash.
fn build(controller: &Bound<'_, PyAny>, world: &Bound<'_, PyAny>) -> PyResult<(ViewData, Vec<Py<PyString>>)> {
    let py = world.py();
    let minute = exact_i64(&world.getattr(intern!(py, "minute_index"))?)?;
    let schema_version = exact_i64(&world.getattr(intern!(py, "schema_version"))?)?;
    let model_state = world.getattr(intern!(py, "model_state"))?;
    let configs = configs_of(world)?;
    let Some(configs_dict) = as_readable_dict(&configs) else {
        return unsupported("the configs are not a plain dict");
    };
    let applies = helpers(py)?
        .misinformation_applies
        .bind(py)
        .call1((world,))
        .map_err(call_failed)?
        .is_truthy()?;
    let information = state_get(&model_state, intern!(py, "information_spread_v1"))?;
    let information_applies = applies && information.is_instance_of::<PyDict>();
    if !information.is_none() && !information.is_instance_of::<PyDict>() {
        return unsupported("the information state is not a dict");
    }
    let cache = cache_of(controller)?;
    let mut cache = cache.try_borrow_mut()?;
    let person_cache = controller.getattr(intern!(py, "_person_contexts"))?;
    let (persons, has_population_context) =
        person_contexts(world, configs_dict, &model_state, &person_cache, &mut cache)?;
    let zones = read_zones(controller, world, configs_dict, &model_state, &mut cache)?;
    let store = entities_of(world)?;
    let no_person = cache.no_person.clone();
    let (mut agents, mut facilities) =
        EntityStore::with_rows(&store, |rows| read_rows(rows, &zones, &no_person, minute))?;
    let mut keys = Vec::with_capacity(agents.len());
    for agent in &agents {
        keys.push(cache.key(py, &agent.id).unbind());
    }
    drop(cache);

    // Reads of the Python documents; none of them runs Python code but a mapping's get.
    let presence = presence_people_of(&model_state)?;
    for (agent, key) in agents.iter_mut().zip(&keys) {
        agent.group_id = group_of(presence.as_ref(), key.bind(py), &agent.group_id)?;
        if let Some(person) = persons.get(&agent.id) {
            agent.person = person.clone();
        }
    }
    read_information(world, &information, information_applies, &mut agents)?;
    read_body_and_distress(world, &model_state, &mut agents, &keys)?;
    read_facility_contexts(world, &model_state, &mut facilities)?;

    let extensions = configs_dict.get_item(intern!(py, "simulator_extensions"))?;
    let extensions_enabled = match &extensions {
        Some(extensions) => match extensions.call_method1(intern!(py, "get"), (intern!(py, "enabled"),))? {
            enabled if enabled.is_none() => false,
            enabled => enabled.is_truthy()?,
        },
        None => false,
    };
    let autonomy_enabled =
        extensions_enabled || has_population_context || model_state.contains(intern!(py, "body_state_v1"))?;
    Ok((
        ViewData {
            minute_index: minute,
            resource_service_enabled: schema_version == 6,
            autonomy_enabled,
            has_population_context,
            partial: false,
            zones,
            agents,
            facilities: facilities.into_iter().map(Arc::new).collect(),
        },
        keys,
    ))
}

/// `after_state_update`, without its hash.
fn refresh(prior: &RustView, world: &Bound<'_, PyAny>) -> PyResult<(ViewData, Vec<Py<PyString>>)> {
    let py = world.py();
    let before = &prior.data;
    let model_state = world.getattr(intern!(py, "model_state"))?;
    let mut agents = before.agents.clone();
    let store = entities_of(world)?;
    EntityStore::with_rows(&store, |rows| {
        for agent in agents.iter_mut() {
            let Some(row) = rows.agents.get(&agent.id) else {
                return unsupported("a person of the prior view has left");
            };
            agent.needs = row.needs.clone();
            agent.stress = row.stress;
            agent.trust = row.trust;
        }
        Ok(())
    })?;
    read_body_and_distress(world, &model_state, &mut agents, &prior.keys)?;
    Ok((
        ViewData {
            minute_index: before.minute_index,
            resource_service_enabled: before.resource_service_enabled,
            autonomy_enabled: before.autonomy_enabled,
            has_population_context: before.has_population_context,
            partial: false,
            zones: before.zones.clone(),
            agents,
            facilities: before.facilities.clone(),
        },
        prior.keys.iter().map(|key| key.clone_ref(py)).collect(),
    ))
}

// ---- The audit hash -----------------------------------------------------------------------------

fn key(out: &mut Vec<u8>, first: bool, name: &str) {
    if !first {
        out.push(b',');
    }
    out.push(b'"');
    out.extend_from_slice(name.as_bytes());
    out.extend_from_slice(b"\":");
}

fn write_text(out: &mut Vec<u8>, text: &str) {
    write_json_string(out, text);
}

fn write_option(out: &mut Vec<u8>, text: &Option<Arc<str>>) {
    match text {
        Some(text) => write_text(out, text),
        None => out.extend_from_slice(b"null"),
    }
}

fn write_int(out: &mut Vec<u8>, value: i64) {
    let mut buffer = itoa::Buffer::new();
    out.extend_from_slice(buffer.format(value).as_bytes());
}

fn write_optional_int(out: &mut Vec<u8>, value: Option<i64>) {
    match value {
        Some(value) => write_int(out, value),
        None => out.extend_from_slice(b"null"),
    }
}

fn write_texts(out: &mut Vec<u8>, items: &[Arc<str>]) {
    out.push(b'[');
    for (index, item) in items.iter().enumerate() {
        if index > 0 {
            out.push(b',');
        }
        write_text(out, item);
    }
    out.push(b']');
}

/// `model_configs[name]`; a None is left to the Python path, as in `_view_state_hash`.
fn hashed_config<'py>(configs: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    let value = configs.get_item(name).map_err(|_| Unsupported::new_err("a config is missing"))?;
    if value.is_none() {
        return unsupported("a config is None");
    }
    Ok(value)
}

/// `canonical_sha256` of the document `_view_state_hash` builds. Keys are written sorted. A float
/// the canonical form does not hold says Unsupported, and the Python rule raises its own text.
fn state_hash(data: &ViewData, world: &Bound<'_, PyAny>) -> PyResult<String> {
    let py = world.py();
    let model_state = world.getattr(intern!(py, "model_state"))?;
    let configs = configs_of(world)?;
    let information_state = state_get(&model_state, intern!(py, "information_spread_v1"))?;
    let resource = data.resource_service_enabled;
    let information = !information_state.is_none();
    let resource_config = if resource { Some(hashed_config(&configs, intern!(py, "resource_service"))?) } else { None };
    let information_config =
        if information { Some(hashed_config(&configs, intern!(py, "information_spread"))?) } else { None };
    let body_state = state_get(&model_state, intern!(py, "body_state_v1"))?;
    let life_context = data.has_population_context || (!body_state.is_none() && body_state.is_truthy()?);

    let mut encoder = Encoder { out: Vec::with_capacity(32 * 1024) };
    encoder.out.push(b'{');
    let mut first = true;
    if life_context {
        key(&mut encoder.out, true, "agent_life_context");
        first = false;
        encoder.out.push(b'[');
        for (index, agent) in data.agents.iter().enumerate() {
            if index > 0 {
                encoder.out.push(b',');
            }
            let person = &agent.person;
            let out = &mut encoder.out;
            out.extend_from_slice(b"{\"agent_id\":");
            write_text(out, &agent.id);
            key(out, false, "care_for_ids");
            write_texts(out, &person.care_for_ids);
            key(out, false, "caregiver_ids");
            write_texts(out, &person.caregiver_ids);
            key(out, false, "discontent");
            encoder.float(agent.body.discontent)?;
            key(&mut encoder.out, false, "hygiene_need");
            encoder.float(agent.body.hygiene_need)?;
            key(&mut encoder.out, false, "negative_affect");
            encoder.float(agent.body.negative_affect)?;
            let out = &mut encoder.out;
            key(out, false, "parent_ids");
            write_texts(out, &person.parent_ids);
            key(out, false, "personal_context_present");
            out.extend_from_slice(if person.personal_context_present { b"true" } else { b"false" });
            key(out, false, "positive_affect");
            encoder.float(agent.body.positive_affect)?;
            key(&mut encoder.out, false, "reassurance");
            encoder.float(agent.body.reassurance)?;
            let out = &mut encoder.out;
            key(out, false, "required_sleep_minutes");
            write_optional_int(out, person.required_sleep_minutes);
            key(out, false, "roles");
            write_texts(out, &person.roles);
            key(out, false, "sleep_debt_minutes");
            encoder.float(agent.body.sleep_debt_minutes)?;
            key(&mut encoder.out, false, "uncertainty");
            encoder.float(agent.body.uncertainty)?;
            let out = &mut encoder.out;
            key(out, false, "usual_sleep_minute");
            write_optional_int(out, person.usual_sleep_minute);
            key(out, false, "usual_wake_minute");
            write_optional_int(out, person.usual_wake_minute);
            out.push(b'}');
        }
        encoder.out.push(b']');
    }
    key(&mut encoder.out, first, "agents");
    encoder.out.push(b'[');
    for (index, agent) in data.agents.iter().enumerate() {
        if index > 0 {
            encoder.out.push(b',');
        }
        let out = &mut encoder.out;
        out.push(b'{');
        key(out, true, "activity");
        write_text(out, activity_text(agent.activity));
        key(out, false, "group_id");
        write_option(out, &agent.group_id);
        key(out, false, "id");
        write_text(out, &agent.id);
        if information {
            key(out, false, "information_query_pressure");
            encoder.float(agent.information_query_pressure)?;
        }
        let out = &mut encoder.out;
        key(out, false, "mobility");
        write_text(out, mobility_text(agent.mobility));
        key(out, false, "needs");
        out.extend_from_slice(b"{\"fatigue\":");
        encoder.float(agent.needs.fatigue)?;
        encoder.out.extend_from_slice(b",\"hunger\":");
        encoder.float(agent.needs.hunger)?;
        if resource {
            encoder.out.extend_from_slice(b",\"hydration_need\":");
            encoder.float(agent.needs.hydration_need)?;
        }
        encoder.out.extend_from_slice(b",\"toilet_urgency\":");
        encoder.float(agent.needs.toilet_urgency)?;
        let out = &mut encoder.out;
        out.push(b'}');
        key(out, false, "next_decision_at");
        write_int(out, agent.next_decision_at);
        key(out, false, "queue_kind");
        write_option(out, &agent.queue_kind);
        key(out, false, "queue_target_id");
        write_option(out, &agent.queue_target_id);
        key(out, false, "queue_task_id");
        write_option(out, &agent.queue_task_id);
        key(out, false, "queue_wait_minutes");
        write_int(out, agent.queue_wait_minutes);
        key(out, false, "rng_key");
        write_text(out, &agent.rng_key);
        key(out, false, "stress");
        encoder.float(agent.stress)?;
        key(&mut encoder.out, false, "stress_sensitivity");
        encoder.float(agent.stress_sensitivity)?;
        let out = &mut encoder.out;
        key(out, false, "support_assigned_staff_id");
        write_option(out, &agent.support_assigned_staff_id);
        key(out, false, "support_cooldown_remaining_minutes");
        write_int(out, agent.support_cooldown_remaining_minutes);
        key(out, false, "support_need");
        encoder.float(agent.support_need)?;
        let out = &mut encoder.out;
        key(out, false, "support_request_id");
        write_option(out, &agent.support_request_id);
        key(out, false, "support_request_status");
        match agent.support_request_status {
            Some(status) => write_text(out, status_text(status)),
            None => out.extend_from_slice(b"null"),
        }
        key(out, false, "support_wait_minutes");
        write_int(out, agent.support_wait_minutes);
        key(out, false, "target_id");
        write_option(out, &agent.target_id);
        key(out, false, "trust");
        encoder.float(agent.trust)?;
        let out = &mut encoder.out;
        key(out, false, "zone_id");
        write_text(out, &data.zones[agent.zone].id);
        out.push(b'}');
    }
    encoder.out.push(b']');
    let out = &mut encoder.out;
    key(out, false, "facilities");
    out.push(b'[');
    for (index, facility) in data.facilities.iter().enumerate() {
        if index > 0 {
            out.push(b',');
        }
        out.push(b'{');
        key(out, true, "active_agent_ids");
        write_texts(out, &facility.active_agent_ids);
        key(out, false, "capacity");
        write_int(out, facility.capacity);
        key(out, false, "id");
        write_text(out, &facility.id);
        key(out, false, "kind");
        write_text(out, &facility.kind);
        key(out, false, "queue_agent_ids");
        write_texts(out, &facility.queue_agent_ids);
        if resource {
            key(out, false, "resource_active_agent_ids");
            write_texts(out, &facility.resource_active_agent_ids);
            key(out, false, "resource_queue_agent_ids");
            write_texts(out, &facility.resource_queue_agent_ids);
        }
        key(out, false, "working_units");
        write_int(out, facility.working_units);
        key(out, false, "zone_id");
        write_text(out, &facility.zone_id);
        out.push(b'}');
    }
    out.push(b']');
    if let Some(config) = &information_config {
        key(&mut encoder.out, false, "information_spread_config");
        encoder.value(config, 1)?;
    }
    let out = &mut encoder.out;
    key(out, false, "minute_index");
    write_int(out, data.minute_index);
    if let Some(config) = &resource_config {
        key(&mut encoder.out, false, "resource_service_config");
        encoder.value(config, 1)?;
    }
    encoder.out.push(b'}');
    let digest = Sha256::digest(&encoder.out);
    Ok(digest.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn activity_text(activity: Activity) -> &'static str {
    match activity {
        Activity::Idle => "idle",
        Activity::Moving => "moving",
        Activity::Queued => "queued",
        Activity::InService => "in_service",
        Activity::SeekingHelp => "seeking_help",
        Activity::Sleeping => "sleeping",
        Activity::Resting => "resting",
    }
}

fn mobility_text(mobility: Mobility) -> &'static str {
    match mobility {
        Mobility::Independent => "independent",
        Mobility::Assisted => "assisted",
        Mobility::Wheelchair => "wheelchair",
    }
}

fn status_text(status: RequestStatus) -> &'static str {
    match status {
        RequestStatus::Pending => "pending",
        RequestStatus::Assigned => "assigned",
        RequestStatus::Resolved => "resolved",
        RequestStatus::Unresolved => "unresolved",
    }
}

// ---- The Python view ----------------------------------------------------------------------------

/// The snapshot behind a `WorldView`, in its `_rust_view`.
#[pyclass(module = "sheltersim_native", frozen)]
pub struct RustView {
    data: ViewData,
    /// The people's IDs as Python strings, whose hashes are kept, in the order of `data.agents`.
    keys: Vec<Py<PyString>>,
}

impl RustView {
    pub(crate) fn data(&self) -> &ViewData {
        &self.data
    }

    pub(crate) fn key<'py>(&self, py: Python<'py>, index: usize) -> &Bound<'py, PyString> {
        self.keys[index].bind(py)
    }
}

fn tuple_of<'py>(py: Python<'py>, items: &[Arc<str>]) -> PyResult<Bound<'py, PyAny>> {
    if items.is_empty() {
        return Ok(PyTuple::empty(py).into_any());
    }
    Ok(PyTuple::new(py, items.iter().map(|item| &**item))?.into_any())
}

fn option_text<'py>(py: Python<'py>, text: &Option<Arc<str>>) -> Bound<'py, PyAny> {
    match text {
        Some(text) => PyString::new(py, text).into_any(),
        None => py.None().into_bound(py),
    }
}

fn option_int<'py>(py: Python<'py>, value: Option<i64>) -> PyResult<Bound<'py, PyAny>> {
    match value {
        Some(value) => value.into_bound_py_any(py),
        None => Ok(py.None().into_bound(py)),
    }
}

fn new_object<'py>(helpers: &PythonHelpers, kind: &Py<PyType>, fields: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyAny>> {
    let py = fields.py();
    let object = helpers.new.bind(py).call1((kind.bind(py),))?;
    raw_setattr(&object, intern!(py, "__dict__"), fields.as_ptr())?;
    Ok(object)
}

/// What every person of a zone shares, in the order `from_world` gives their fields.
fn zone_python<'py>(helpers: &PythonHelpers, py: Python<'py>, zone: &ViewZone) -> PyResult<Bound<'py, PyAny>> {
    let dict = PyDict::new(py);
    let any = dict.as_any();
    macro_rules! set {
        ($name:literal, $value:expr) => {
            raw_dict_set(any, intern!(py, $name).as_any(), &$value)?
        };
    }
    let float = |value: f64| PyFloat::new(py, value).into_any();
    set!("environmental_stress_per_minute", float(zone.environmental_stress_per_minute));
    set!("environmental_stressor_event_ids", tuple_of(py, &zone.environmental_stressor_event_ids)?);
    set!("environmental_assumption_source_id", option_text(py, &zone.environmental_assumption_source_id));
    set!("ambient_noise_level", float(zone.ambient_noise_level));
    set!("rest_efficiency_multiplier", float(zone.rest_efficiency_multiplier));
    set!("zone_environment_modifier_event_ids", tuple_of(py, &zone.zone_environment_modifier_event_ids)?);
    set!("zone_environment_assumption_ids", tuple_of(py, &zone.zone_environment_assumption_ids)?);
    set!("ventilation_effectiveness", float(zone.ventilation_effectiveness));
    set!("lighting_level", float(zone.lighting_level));
    set!("privacy_level", float(zone.privacy_level));
    set!("temperature_c", float(zone.temperature_c));
    set!("ventilation_hygiene_growth_per_minute", float(zone.ventilation_hygiene_growth_per_minute));
    set!("privacy_discontent_per_minute", float(zone.privacy_discontent_per_minute));
    set!("condition_modifier_event_ids", tuple_of(py, &zone.condition_modifier_event_ids)?);
    set!("condition_assumption_ids", tuple_of(py, &zone.condition_assumption_ids)?);
    let mut demand = Vec::with_capacity(zone.demand_modifiers.len());
    for modifier in &zone.demand_modifiers {
        let fields = PyDict::new(py);
        fields.set_item(intern!(py, "action"), &*modifier.action)?;
        fields.set_item(intern!(py, "probability_multiplier"), modifier.probability_multiplier)?;
        fields.set_item(intern!(py, "duration_multiplier"), modifier.duration_multiplier)?;
        fields.set_item(intern!(py, "modifier_event_ids"), tuple_of(py, &modifier.modifier_event_ids)?)?;
        fields.set_item(intern!(py, "assumption_ids"), tuple_of(py, &modifier.assumption_ids)?)?;
        demand.push(new_object(helpers, &helpers.demand_view, &fields)?);
    }
    set!("demand_modifiers", PyTuple::new(py, demand)?.into_any());
    set!("min_rest_decision_minutes", zone.min_rest_decision_minutes.into_bound_py_any(py)?);
    set!("max_rest_decision_minutes", zone.max_rest_decision_minutes.into_bound_py_any(py)?);
    set!("sanitation_level", float(zone.sanitation_level));
    Ok(dict.into_any())
}

fn copy_dict<'py>(dict: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    // SAFETY: dict is a live dict; the call returns a new reference or sets an error.
    unsafe {
        let copy = pyo3::ffi::PyDict_Copy(dict.as_ptr());
        Bound::from_owned_ptr_or_err(dict.py(), copy)
    }
}

fn members_python<'py>(
    py: Python<'py>,
    members: Vec<(&Arc<str>, Vec<usize>)>,
    keys: &[Py<PyString>],
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    for (name, people) in members {
        let tuple = PyTuple::new(py, people.iter().map(|index| keys[*index].bind(py)))?;
        dict.set_item(&**name, tuple)?;
    }
    Ok(dict)
}

#[pymethods]
impl RustView {
    /// (agents, facilities, zone_members, group_members) as `WorldView` holds them, each as a
    /// plain dict for it to wrap. Each call makes new objects.
    fn python_fields<'py>(
        &self,
        py: Python<'py>,
    ) -> PyResult<(Bound<'py, PyDict>, Bound<'py, PyDict>, Bound<'py, PyDict>, Bound<'py, PyDict>)> {
        let helpers = helpers(py)?;
        let data = &self.data;
        let mut zones = Vec::with_capacity(data.zones.len());
        for zone in &data.zones {
            zones.push(zone_python(helpers, py, zone)?);
        }
        let zone_ids: Vec<Bound<'py, PyString>> = data.zones.iter().map(|zone| PyString::new(py, &zone.id)).collect();
        let float = |value: f64| PyFloat::new(py, value).into_any();
        let enabled = data.resource_service_enabled.into_bound_py_any(py)?;
        let agents = PyDict::new(py);
        for (agent, key) in data.agents.iter().zip(&self.keys) {
            let fields = copy_dict(&zones[agent.zone])?;
            let any = &fields;
            macro_rules! set {
                ($name:literal, $value:expr) => {
                    raw_dict_set(any, intern!(py, $name).as_any(), &$value)?
                };
            }
            let person = &agent.person;
            set!("caregiver_ids", tuple_of(py, &person.caregiver_ids)?);
            set!("care_for_ids", tuple_of(py, &person.care_for_ids)?);
            set!("parent_ids", tuple_of(py, &person.parent_ids)?);
            set!("roles", tuple_of(py, &person.roles)?);
            set!("usual_wake_minute", option_int(py, person.usual_wake_minute)?);
            set!("usual_sleep_minute", option_int(py, person.usual_sleep_minute)?);
            set!("required_sleep_minutes", option_int(py, person.required_sleep_minutes)?);
            set!("personal_context_present", person.personal_context_present.into_bound_py_any(py)?);
            let key = key.bind(py);
            set!("id", key.as_any());
            set!("group_id", option_text(py, &agent.group_id));
            set!("zone_id", zone_ids[agent.zone].as_any());
            set!("mobility", agent.mobility.member(py)?);
            set!("support_need", float(agent.support_need));
            set!("stress_sensitivity", float(agent.stress_sensitivity));
            set!("rng_key", PyString::new(py, &agent.rng_key).into_any());
            set!("activity", agent.activity.member(py)?);
            set!("target_id", option_text(py, &agent.target_id));
            let needs = PyDict::new(py);
            let needs_any = needs.as_any();
            raw_dict_set(needs_any, intern!(py, "toilet_urgency").as_any(), &float(agent.needs.toilet_urgency))?;
            raw_dict_set(needs_any, intern!(py, "hunger").as_any(), &float(agent.needs.hunger))?;
            raw_dict_set(needs_any, intern!(py, "fatigue").as_any(), &float(agent.needs.fatigue))?;
            raw_dict_set(needs_any, intern!(py, "hydration_need").as_any(), &float(agent.needs.hydration_need))?;
            set!("needs", new_object(helpers, &helpers.need_view, &needs)?);
            set!("stress", float(agent.stress));
            set!("chronic_distress", float(agent.chronic_distress));
            set!("trust", float(agent.trust));
            set!("next_decision_at", agent.next_decision_at.into_bound_py_any(py)?);
            set!("resource_service_enabled", enabled);
            set!("queue_task_id", option_text(py, &agent.queue_task_id));
            set!("queue_kind", option_text(py, &agent.queue_kind));
            set!("queue_target_id", option_text(py, &agent.queue_target_id));
            set!("queue_wait_minutes", agent.queue_wait_minutes.into_bound_py_any(py)?);
            set!("support_request_id", option_text(py, &agent.support_request_id));
            set!(
                "support_request_status",
                match agent.support_request_status {
                    Some(status) => PyString::new(py, status_text(status)).into_any(),
                    None => py.None().into_bound(py),
                }
            );
            set!("support_assigned_staff_id", option_text(py, &agent.support_assigned_staff_id));
            set!("support_wait_minutes", agent.support_wait_minutes.into_bound_py_any(py)?);
            set!("support_cooldown_remaining_minutes", agent.support_cooldown_remaining_minutes.into_bound_py_any(py)?);
            set!("information_query_pressure", float(agent.information_query_pressure));
            set!("misinformation_belief_pressure", float(agent.misinformation_belief_pressure));
            let sources = if agent.misinformation_effect_sources.is_empty() {
                PyTuple::empty(py).into_any()
            } else {
                let mut items = Vec::with_capacity(agent.misinformation_effect_sources.len());
                for source in &agent.misinformation_effect_sources {
                    items.push((
                        &*source.message_id,
                        &*source.origin_event_id,
                        &*source.last_event_id,
                        &*source.source_id,
                        source.time,
                    ));
                }
                PyTuple::new(py, items)?.into_any()
            };
            set!("misinformation_effect_sources", sources);
            let body = &agent.body;
            set!("sleep_debt_minutes", float(body.sleep_debt_minutes));
            set!("uncertainty", float(body.uncertainty));
            set!("reassurance", float(body.reassurance));
            set!("discontent", float(body.discontent));
            set!("positive_affect", float(body.positive_affect));
            set!("negative_affect", float(body.negative_affect));
            set!("hygiene_need", float(body.hygiene_need));
            let Ok(fields) = fields.downcast::<PyDict>() else {
                return unsupported("a copy of a dict is not a dict");
            };
            let view = new_object(helpers, &helpers.agent_view, fields)?;
            raw_dict_set(agents.as_any(), key.as_any(), &view)?;
        }
        let facilities = PyDict::new(py);
        for facility in &data.facilities {
            let fields = PyDict::new(py);
            let any = fields.as_any();
            macro_rules! set {
                ($name:literal, $value:expr) => {
                    raw_dict_set(any, intern!(py, $name).as_any(), &$value)?
                };
            }
            let id = PyString::new(py, &facility.id);
            set!("id", id.as_any());
            set!("kind", PyString::new(py, &facility.kind).into_any());
            set!("zone_id", PyString::new(py, &facility.zone_id).into_any());
            set!("capacity", facility.capacity.into_bound_py_any(py)?);
            set!("working_units", facility.working_units.into_bound_py_any(py)?);
            set!("active_agent_ids", tuple_of(py, &facility.active_agent_ids)?);
            set!("queue_agent_ids", tuple_of(py, &facility.queue_agent_ids)?);
            set!("resource_active_agent_ids", tuple_of(py, &facility.resource_active_agent_ids)?);
            set!("resource_queue_agent_ids", tuple_of(py, &facility.resource_queue_agent_ids)?);
            set!("facility_event_context_ids", tuple_of(py, &facility.facility_event_context_ids)?);
            let view = new_object(helpers, &helpers.facility_view, &fields)?;
            raw_dict_set(facilities.as_any(), id.as_any(), &view)?;
        }
        let zone_members = members_python(py, data.zone_members(), &self.keys)?;
        let group_members = members_python(py, data.group_members(), &self.keys)?;
        Ok((agents, facilities, zone_members, group_members))
    }
}

/// A `WorldView` over a snapshot, whose Python parts are made when a Python reader asks.
fn new_view<'py>(
    py: Python<'py>,
    data: ViewData,
    keys: Vec<Py<PyString>>,
    hash: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let helpers = helpers(py)?;
    let fields = PyDict::new(py);
    fields.set_item(intern!(py, "minute_index"), data.minute_index)?;
    fields.set_item(intern!(py, "state_hash"), hash)?;
    fields.set_item(intern!(py, "resource_service_enabled"), data.resource_service_enabled)?;
    fields.set_item(intern!(py, "autonomy_enabled"), data.autonomy_enabled)?;
    fields.set_item(intern!(py, "_has_population_context"), data.has_population_context)?;
    fields.set_item(intern!(py, "_rust_view"), Py::new(py, RustView { data, keys })?)?;
    new_object(helpers, &helpers.world_view, &fields)
}

/// What the kernels read of a view Python made (a Rust view was refused, or a test made it). Its
/// `__dict__` is read with the checks of a typed field. Only what the update kernels read is filled:
/// `ViewData::partial` says so, and a rule that reads more says Unsupported on it.
fn from_python_view<'py>(view: &Bound<'py, PyAny>) -> PyResult<Bound<'py, RustView>> {
    let py = view.py();
    let items = view.getattr(intern!(py, "agents"))?.call_method0(intern!(py, "items"))?;
    let mut agents = Vec::with_capacity(items.len()?);
    let mut zones = Vec::with_capacity(agents.capacity());
    let mut keys: Vec<Py<PyString>> = Vec::with_capacity(agents.capacity());
    for item in items.try_iter()? {
        let item = item?;
        let item = item.downcast_exact::<PyTuple>()?;
        let key = item.get_item(0)?;
        let Ok(key) = key.downcast_into_exact::<PyString>() else {
            return unsupported("an id is not a str");
        };
        // A lone surrogate has no UTF-8.
        let id: Arc<str> = Arc::from(key.to_str().map_err(|_| Unsupported::new_err("an id is not UTF-8"))?);
        if keys.last().is_some_and(|last| last.bind(py).to_str().is_ok_and(|last| last >= &*id)) {
            return unsupported("the view is not in ID order");
        }
        let fields = item.get_item(1)?.getattr(intern!(py, "__dict__"))?;
        let Some(fields) = as_readable_dict(&fields) else {
            return unsupported("a person's fields are not a dict");
        };
        let float = |name: &Bound<'py, PyString>| -> PyResult<f64> { exact_float(&field(fields, name)?) };
        zones.push(Arc::new(ViewZone {
            id: exact_text(&field(fields, intern!(py, "zone_id"))?)?,
            environmental_stress_per_minute: float(intern!(py, "environmental_stress_per_minute"))?,
            environmental_stressor_event_ids: Vec::new(),
            environmental_assumption_source_id: None,
            ambient_noise_level: 0.0,
            rest_efficiency_multiplier: float(intern!(py, "rest_efficiency_multiplier"))?,
            zone_environment_modifier_event_ids: Vec::new(),
            zone_environment_assumption_ids: Vec::new(),
            ventilation_effectiveness: 0.0,
            lighting_level: 0.0,
            privacy_level: 0.0,
            temperature_c: 0.0,
            ventilation_hygiene_growth_per_minute: float(intern!(py, "ventilation_hygiene_growth_per_minute"))?,
            privacy_discontent_per_minute: float(intern!(py, "privacy_discontent_per_minute"))?,
            condition_modifier_event_ids: Vec::new(),
            condition_assumption_ids: Vec::new(),
            demand_modifiers: Vec::new(),
            min_rest_decision_minutes: 0,
            max_rest_decision_minutes: 0,
            sanitation_level: float(intern!(py, "sanitation_level"))?,
        }));
        agents.push(ViewAgent {
            zone: agents.len(),
            group_id: optional_text(&field(fields, intern!(py, "group_id"))?)?,
            mobility: Mobility::Independent,
            support_need: 0.0,
            stress_sensitivity: 0.0,
            rng_key: id.clone(),
            activity: Activity::Idle,
            target_id: None,
            needs: NeedRow { toilet_urgency: 0.0, hunger: 0.0, fatigue: 0.0, hydration_need: 0.0 },
            stress: 0.0,
            chronic_distress: 0.0,
            trust: 0.0,
            next_decision_at: 0,
            queue_task_id: None,
            queue_kind: None,
            queue_target_id: None,
            queue_wait_minutes: 0,
            support_request_id: None,
            support_request_status: None,
            support_assigned_staff_id: None,
            support_wait_minutes: 0,
            support_cooldown_remaining_minutes: 0,
            information_query_pressure: float(intern!(py, "information_query_pressure"))?,
            misinformation_belief_pressure: float(intern!(py, "misinformation_belief_pressure"))?,
            misinformation_effect_sources: Vec::new(),
            body: BodyFields {
                sleep_debt_minutes: float(intern!(py, "sleep_debt_minutes"))?,
                uncertainty: float(intern!(py, "uncertainty"))?,
                reassurance: float(intern!(py, "reassurance"))?,
                discontent: float(intern!(py, "discontent"))?,
                positive_affect: float(intern!(py, "positive_affect"))?,
                negative_affect: float(intern!(py, "negative_affect"))?,
                hygiene_need: float(intern!(py, "hygiene_need"))?,
            },
            person: Arc::new(PersonFields {
                required_sleep_minutes: optional_i64(&field(fields, intern!(py, "required_sleep_minutes"))?)?,
                ..PersonFields::default()
            }),
            id,
        });
        keys.push(key.unbind());
    }
    let data = ViewData {
        minute_index: 0,
        resource_service_enabled: false,
        autonomy_enabled: false,
        has_population_context: false,
        partial: true,
        zones,
        agents,
        facilities: Vec::new(),
    };
    Bound::new(py, RustView { data, keys })
}

/// The snapshot of a view, for the update kernels. They read the same tick's rows as well.
pub(crate) fn view_of<'py>(view: &Bound<'py, PyAny>) -> PyResult<Bound<'py, RustView>> {
    let py = view.py();
    let fields = view.getattr(intern!(py, "__dict__"))?;
    match raw_dict_get(&fields, intern!(py, "_rust_view").as_any())? {
        Some(found) => Ok(found.downcast_into::<RustView>()?),
        None => from_python_view(view),
    }
}

/// `_view_after_movement` of the update phase (experiments/phases/segments.py).
#[pyfunction]
fn view_after_movement<'py>(controller: &Bound<'py, PyAny>, world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let (data, keys) = build(controller, world)?;
    new_view(world.py(), data, keys, None)
}

/// `_view_for_proposals` of the propose phase.
#[pyfunction]
fn view_for_proposals<'py>(
    controller: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let slot = intern!(py, "_view_before_proposals");
    let prior = controller.getattr(slot)?;
    let (data, keys) = if !prior.is_none() && prior.getattr(intern!(py, "minute_index"))?.eq(minute)? {
        let fields = prior.getattr(intern!(py, "__dict__"))?;
        let Some(rust) = raw_dict_get(&fields, intern!(py, "_rust_view").as_any())? else {
            return unsupported("the prior view is Python's");
        };
        let rust = rust.downcast_into::<RustView>()?;
        refresh(rust.get(), world)?
    } else {
        build(controller, world)?
    };
    let hash = state_hash(&data, world)?;
    let view = new_view(py, data, keys, Some(hash))?;
    controller.setattr(slot, py.None())?;
    Ok(view)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<RustView>()?;
    m.add_class::<ViewCache>()?;
    m.add_function(wrap_pyfunction!(view_after_movement, m)?)?;
    m.add_function(wrap_pyfunction!(view_for_proposals, m)?)?;
    Ok(())
}

// Rows of a view stay shareable: a rule reads them without Python.
#[allow(dead_code)]
fn assert_views_are_shareable() {
    fn check<T: Send + Sync>() {}
    check::<ViewData>();
}
