//! `zone_environment_view` (`domain/environment.py`) and the formulas it selects
//! (`ZoneEnvironmentFormula` in `policies/registry.py`): a zone's conditions, read from
//! `model_configs` and the modifiers of `zone_environment_v1`.
//!
//! `rules/world_view.rs` keeps what this makes while its inputs stay the same. Values and formula
//! meaning are the Python rule's; the coefficients here are the listed defaults of its contracts and
//! the constants of its formulas (docs/rust-port/lanes/l5-read-rules.md).
//!
//! `read_config` is `ScenarioZoneEnvironmentConfig.model_validate` of a document that validation
//! accepts: anything it does not accept says `Unsupported`, and the Python rule raises its own text.
//! The same goes for a record the Python rule would raise on. The strictness of `exact_float` over
//! Python's arithmetic is what `rules/world_view.rs` has always asked of a view: an int coordinate
//! could not stay an int in a typed field, and `1` and `1.0` would hash differently.

use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyString, PyTuple};
use rustc_hash::FxHashMap;

use crate::core::mathx::{py_max, py_min};
use crate::core::tracked::as_readable_dict;
use crate::core::value::V;
use crate::rules::world_view::DemandModifier;
use crate::state::events::is_stable_id;
use crate::Unsupported;

#[track_caller]
fn unsupported<T>(reason: &'static str) -> PyResult<T> {
    Err(Unsupported::new_err(reason))
}

const SOURCE_ID: &str = "ASM-H19-ZONE-ENVIRONMENT-V2";
const LEGACY_SOURCE_ID: &str = "ASM-H19-ZONE-ENVIRONMENT-V1";

// ---- `ZoneEnvironmentView` -------------------------------------------------------------------------

pub(crate) struct ZoneConditions {
    pub(crate) ambient_noise_level: f64,
    pub(crate) rest_efficiency_multiplier: f64,
    pub(crate) noise_modifier_event_ids: Vec<Arc<str>>,
    pub(crate) noise_assumption_ids: Vec<Arc<str>>,
    pub(crate) demand_modifiers: Vec<DemandModifier>,
    pub(crate) min_rest_decision_minutes: i64,
    pub(crate) max_rest_decision_minutes: i64,
    pub(crate) ventilation_effectiveness: f64,
    pub(crate) lighting_level: f64,
    pub(crate) privacy_level: f64,
    pub(crate) temperature_c: f64,
    pub(crate) ventilation_hygiene_growth_per_minute: f64,
    pub(crate) privacy_discontent_per_minute: f64,
    pub(crate) temperature_stress_per_minute: f64,
    pub(crate) condition_modifier_event_ids: Vec<Arc<str>>,
    pub(crate) condition_assumption_ids: Vec<Arc<str>>,
}

impl ZoneConditions {
    /// `ZoneEnvironmentView()`: a world without the opt-in policy.
    fn neutral() -> Self {
        Self {
            ambient_noise_level: 0.0,
            rest_efficiency_multiplier: 1.0,
            noise_modifier_event_ids: Vec::new(),
            noise_assumption_ids: Vec::new(),
            demand_modifiers: Vec::new(),
            min_rest_decision_minutes: 1,
            max_rest_decision_minutes: 60,
            ventilation_effectiveness: 0.7,
            lighting_level: 0.5,
            privacy_level: 0.5,
            temperature_c: 20.0,
            ventilation_hygiene_growth_per_minute: 0.0001,
            privacy_discontent_per_minute: 0.0001,
            temperature_stress_per_minute: 0.0,
            condition_modifier_event_ids: Vec::new(),
            condition_assumption_ids: Vec::new(),
        }
    }
}

// ---- `ScenarioZoneEnvironmentConfig` ---------------------------------------------------------------

/// `ZoneEnvironmentBaseline`.
#[derive(Clone, Copy)]
struct Baseline {
    ventilation_effectiveness: f64,
    lighting_level: f64,
    privacy_level: f64,
    temperature_c: f64,
}

const DEFAULT_BASELINE: Baseline =
    Baseline { ventilation_effectiveness: 0.7, lighting_level: 0.5, privacy_level: 0.5, temperature_c: 20.0 };

struct Config {
    is_v2: bool,
    source_id: Arc<str>,
    noise_rest_efficiency_loss: f64,
    min_probability_multiplier: f64,
    max_probability_multiplier: f64,
    min_rest_decision_minutes: i64,
    max_rest_decision_minutes: i64,
    lighting_sleep_efficiency_loss: f64,
    ventilation_hygiene_growth_per_minute: f64,
    privacy_discontent_per_minute: f64,
    temperature_stress_per_degree_minute: f64,
    comfortable_min_temperature_c: f64,
    comfortable_max_temperature_c: f64,
    baseline_by_zone: FxHashMap<Arc<str>, Baseline>,
}

const CONFIG_REFUSED: &str = "the zone environment config does not parse";

/// A field of `Field(strict=True, ge=low, le=high, allow_inf_nan=False)`, which takes an int as its
/// float and not a bool.
fn bounded_float(value: &Bound<'_, PyAny>, low: f64, high: f64) -> PyResult<f64> {
    let number = if let Ok(number) = value.downcast_exact::<PyFloat>() {
        number.value()
    } else if let Ok(number) = value.downcast_exact::<PyInt>() {
        match number.extract::<i64>() {
            Ok(number) => number as f64,
            Err(_) => return unsupported(CONFIG_REFUSED),
        }
    } else {
        return unsupported(CONFIG_REFUSED);
    };
    if number.is_finite() && number >= low && number <= high {
        Ok(number)
    } else {
        unsupported(CONFIG_REFUSED)
    }
}

fn bounded_int(value: &Bound<'_, PyAny>, low: i64, high: i64) -> PyResult<i64> {
    match value.downcast_exact::<PyInt>().map(|number| number.extract::<i64>()) {
        Ok(Ok(number)) if number >= low && number <= high => Ok(number),
        _ => unsupported(CONFIG_REFUSED),
    }
}

fn read_baseline(value: &Bound<'_, PyAny>) -> PyResult<Baseline> {
    let Some(record) = as_readable_dict(value) else {
        return unsupported(CONFIG_REFUSED);
    };
    let mut baseline = DEFAULT_BASELINE;
    for (name, item) in record.iter() {
        let Ok(name) = name.downcast_exact::<PyString>() else {
            return unsupported(CONFIG_REFUSED);
        };
        match name.to_str()? {
            "ventilation_effectiveness" => baseline.ventilation_effectiveness = bounded_float(&item, 0.0, 1.0)?,
            "lighting_level" => baseline.lighting_level = bounded_float(&item, 0.0, 1.0)?,
            "privacy_level" => baseline.privacy_level = bounded_float(&item, 0.0, 1.0)?,
            "temperature_c" => baseline.temperature_c = bounded_float(&item, -40.0, 60.0)?,
            _ => return unsupported(CONFIG_REFUSED),
        }
    }
    Ok(baseline)
}

/// The model's fields as `keep_legacy_environment_assumptions_neutral` and the contract's defaults
/// leave them, then the checks of `multipliers_are_ordered`. A stable ID with spaces around it, which
/// the contract would strip, is refused: it could not be the canonical document validation asks for.
fn read_config(raw: &Bound<'_, PyDict>) -> PyResult<Config> {
    let py = raw.py();
    let version = raw.get_item(intern!(py, "policy_version"))?;
    let legacy = match &version {
        Some(version) => match version.downcast_exact::<PyString>().map(|text| text.to_str()) {
            Ok(Ok("zone-environment-v1")) => true,
            Ok(Ok("zone-environment-v2")) => false,
            _ => return unsupported(CONFIG_REFUSED),
        },
        None => {
            let mut none_of_the_new_fields = true;
            for name in [
                intern!(py, "lighting_sleep_efficiency_loss"),
                intern!(py, "ventilation_hygiene_growth_per_minute"),
                intern!(py, "privacy_discontent_per_minute"),
                intern!(py, "temperature_stress_per_degree_minute"),
                intern!(py, "baseline_by_zone"),
            ] {
                none_of_the_new_fields &= !raw.contains(name)?;
            }
            none_of_the_new_fields
        }
    };
    let neutral_or = |coefficient: f64| if legacy { 0.0 } else { coefficient };
    let mut config = Config {
        is_v2: !legacy,
        source_id: Arc::from(if legacy { LEGACY_SOURCE_ID } else { SOURCE_ID }),
        noise_rest_efficiency_loss: 0.5,
        min_probability_multiplier: 0.5,
        max_probability_multiplier: 2.0,
        min_rest_decision_minutes: 1,
        max_rest_decision_minutes: 60,
        lighting_sleep_efficiency_loss: neutral_or(0.2),
        ventilation_hygiene_growth_per_minute: neutral_or(0.0001),
        privacy_discontent_per_minute: neutral_or(0.0001),
        temperature_stress_per_degree_minute: neutral_or(0.0001),
        comfortable_min_temperature_c: 18.0,
        comfortable_max_temperature_c: 26.0,
        baseline_by_zone: FxHashMap::default(),
    };
    for (name, value) in raw.iter() {
        let Ok(name) = name.downcast_exact::<PyString>() else {
            return unsupported(CONFIG_REFUSED);
        };
        match name.to_str()? {
            // `Literal[1]` looks the value up, so what equals 1 passes.
            "schema_version" => {
                let is_one = value.is_exact_instance_of::<PyBool>() && value.is_truthy()?
                    || value.downcast_exact::<PyFloat>().is_ok_and(|number| number.value() == 1.0)
                    || value.downcast_exact::<PyInt>().is_ok_and(|number| number.extract::<i64>().is_ok_and(|number| number == 1));
                if !is_one {
                    return unsupported(CONFIG_REFUSED);
                }
            }
            "policy_version" => {}
            "source_type" => {
                if !value.downcast_exact::<PyString>().is_ok_and(|text| text.to_str().is_ok_and(|text| text == "assumption")) {
                    return unsupported(CONFIG_REFUSED);
                }
            }
            "source_id" => {
                let Ok(text) = value.downcast_exact::<PyString>() else {
                    return unsupported(CONFIG_REFUSED);
                };
                let text = text.to_str()?;
                if !is_stable_id(text) {
                    return unsupported(CONFIG_REFUSED);
                }
                config.source_id = Arc::from(text);
            }
            "noise_rest_efficiency_loss" => config.noise_rest_efficiency_loss = bounded_float(&value, 0.0, 0.9)?,
            "min_probability_multiplier" => config.min_probability_multiplier = bounded_float(&value, 0.1, 1.0)?,
            "max_probability_multiplier" => config.max_probability_multiplier = bounded_float(&value, 1.0, 2.0)?,
            "min_rest_decision_minutes" => config.min_rest_decision_minutes = bounded_int(&value, 1, 60)?,
            "max_rest_decision_minutes" => config.max_rest_decision_minutes = bounded_int(&value, 1, 240)?,
            "max_active_modifiers_per_zone" => {
                bounded_int(&value, 1, 16)?;
            }
            "lighting_sleep_efficiency_loss" => config.lighting_sleep_efficiency_loss = bounded_float(&value, 0.0, 0.9)?,
            "ventilation_hygiene_growth_per_minute" => {
                config.ventilation_hygiene_growth_per_minute = bounded_float(&value, 0.0, 0.01)?;
            }
            "privacy_discontent_per_minute" => config.privacy_discontent_per_minute = bounded_float(&value, 0.0, 0.01)?,
            "temperature_stress_per_degree_minute" => {
                config.temperature_stress_per_degree_minute = bounded_float(&value, 0.0, 0.01)?;
            }
            "comfortable_min_temperature_c" => config.comfortable_min_temperature_c = bounded_float(&value, -10.0, 40.0)?,
            "comfortable_max_temperature_c" => config.comfortable_max_temperature_c = bounded_float(&value, -10.0, 50.0)?,
            "baseline_by_zone" => {
                let Some(baselines) = as_readable_dict(&value) else {
                    return unsupported(CONFIG_REFUSED);
                };
                for (zone, baseline) in baselines.iter() {
                    let Ok(zone) = zone.downcast_exact::<PyString>() else {
                        return unsupported(CONFIG_REFUSED);
                    };
                    let zone = zone.to_str()?;
                    if !is_stable_id(zone) {
                        return unsupported(CONFIG_REFUSED);
                    }
                    config.baseline_by_zone.insert(Arc::from(zone), read_baseline(&baseline)?);
                }
            }
            _ => return unsupported(CONFIG_REFUSED),
        }
    }
    if (legacy
        && (config.lighting_sleep_efficiency_loss != 0.0
            || config.ventilation_hygiene_growth_per_minute != 0.0
            || config.privacy_discontent_per_minute != 0.0
            || config.temperature_stress_per_degree_minute != 0.0
            || !config.baseline_by_zone.is_empty()))
        || config.min_probability_multiplier > config.max_probability_multiplier
        || config.min_rest_decision_minutes > config.max_rest_decision_minutes
        || config.comfortable_min_temperature_c >= config.comfortable_max_temperature_c
    {
        return unsupported(CONFIG_REFUSED);
    }
    Ok(config)
}

// ---- `ZoneEnvironmentFormula` ----------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq)]
enum Temperature {
    ComfortBand,
    Midpoint,
}

#[derive(Clone, Copy, PartialEq)]
enum Rest {
    Multiplicative,
    Limiting,
}

#[derive(Clone, Copy, PartialEq)]
enum Hygiene {
    Linear,
    Quadratic,
}

#[derive(Clone, Copy, PartialEq)]
enum Privacy {
    Threshold,
    Proportional,
}

fn temperature_deviation(model: Temperature, temperature_c: f64, config: &Config) -> f64 {
    let minimum = config.comfortable_min_temperature_c;
    let maximum = config.comfortable_max_temperature_c;
    match model {
        Temperature::Midpoint => (temperature_c - (minimum + maximum) / 2.0).abs(),
        Temperature::ComfortBand => py_max(py_max(0.0, minimum - temperature_c), temperature_c - maximum),
    }
}

fn rest_efficiency(model: Rest, noise_level: f64, lighting_level: f64, config: &Config) -> f64 {
    let noise_loss = noise_level * config.noise_rest_efficiency_loss;
    let lighting_loss = lighting_level * config.lighting_sleep_efficiency_loss;
    match model {
        Rest::Limiting => py_max(0.1, 1.0 - py_max(noise_loss, lighting_loss)),
        Rest::Multiplicative => py_max(0.1, (1.0 - noise_loss) * (1.0 - lighting_loss)),
    }
}

fn ventilation_hygiene_growth(model: Hygiene, ventilation: f64, config: &Config) -> f64 {
    let mut deficit = 1.0 - ventilation;
    if model == Hygiene::Quadratic {
        deficit *= deficit;
    }
    deficit * config.ventilation_hygiene_growth_per_minute
}

fn privacy_discontent(model: Privacy, privacy: f64, config: &Config) -> f64 {
    let deficit = match model {
        Privacy::Proportional => 1.0 - privacy,
        Privacy::Threshold => py_max(0.0, 0.4 - privacy),
    };
    deficit * config.privacy_discontent_per_minute
}

/// The selected formulas, and the source IDs a selection adds. `create_zone_*_formula` of an ID the
/// registry does not hold raises; a formula another module registered is not one of these.
struct Formulas {
    temperature: Temperature,
    rest: Rest,
    hygiene: Hygiene,
    privacy: Privacy,
    sources: Vec<Arc<str>>,
}

fn read_formulas(configs: &Bound<'_, PyDict>) -> PyResult<Formulas> {
    let py = configs.py();
    let registry = configs.get_item(intern!(py, "policy_registry"))?;
    let registry = match &registry {
        Some(registry) => match as_readable_dict(registry) {
            Some(registry) => Some(registry),
            None => return unsupported("the policy registry is not a plain dict"),
        },
        None => None,
    };
    let mut sources = Vec::new();
    let select = |domain: &Bound<'_, PyString>| -> PyResult<Option<String>> {
        let Some(registry) = registry else { return Ok(None) };
        let Some(selected) = registry.get_item(domain)? else { return Ok(None) };
        match selected.downcast_exact::<PyString>() {
            Ok(text) => Ok(Some(text.to_str()?.to_owned())),
            Err(_) => unsupported("a zone formula's ID is not a str"),
        }
    };
    const UNKNOWN: &str = "a zone formula of another module";
    let temperature = match select(intern!(py, "zone_environment"))?.as_deref() {
        None => Temperature::ComfortBand,
        Some("zone-temperature-comfort-band-v1") => {
            sources.push(Arc::from("ASM-ZONE-TEMP-COMFORT-BAND-V1"));
            Temperature::ComfortBand
        }
        Some("zone-temperature-midpoint-v1") => {
            sources.push(Arc::from("ASM-ZONE-TEMP-MIDPOINT-V1"));
            Temperature::Midpoint
        }
        Some(_) => return unsupported(UNKNOWN),
    };
    let rest = match select(intern!(py, "zone_rest"))?.as_deref() {
        None => Rest::Multiplicative,
        Some("zone-rest-multiplicative-v1") => {
            sources.push(Arc::from("ASM-ZONE-REST-MULTIPLICATIVE-V1"));
            Rest::Multiplicative
        }
        Some("zone-rest-limiting-v1") => {
            sources.push(Arc::from("ASM-ZONE-REST-LIMITING-V1"));
            Rest::Limiting
        }
        Some(_) => return unsupported(UNKNOWN),
    };
    let hygiene = match select(intern!(py, "zone_hygiene"))?.as_deref() {
        None => Hygiene::Linear,
        Some("zone-hygiene-linear-v1") => {
            sources.push(Arc::from("ASM-ZONE-HYGIENE-LINEAR-V1"));
            Hygiene::Linear
        }
        Some("zone-hygiene-quadratic-v1") => {
            sources.push(Arc::from("ASM-ZONE-HYGIENE-QUADRATIC-V1"));
            Hygiene::Quadratic
        }
        Some(_) => return unsupported(UNKNOWN),
    };
    let privacy = match select(intern!(py, "zone_privacy"))?.as_deref() {
        None => Privacy::Threshold,
        Some("zone-privacy-threshold-v1") => {
            sources.push(Arc::from("ASM-ZONE-PRIVACY-THRESHOLD-V1"));
            Privacy::Threshold
        }
        Some("zone-privacy-proportional-v1") => {
            sources.push(Arc::from("ASM-ZONE-PRIVACY-PROPORTIONAL-V1"));
            Privacy::Proportional
        }
        Some(_) => return unsupported(UNKNOWN),
    };
    Ok(Formulas { temperature, rest, hygiene, privacy, sources })
}

// ---- The modifiers ---------------------------------------------------------------------------------

const RECORD_REFUSED: &str = "a zone modifier is not what the rule reads";

/// A modifier record: its ID, and the document in `zone_environment_v1.modifiers`.
struct Modifier {
    id: Arc<str>,
    zone_id: Option<Arc<str>>,
    record: IndexMap<Arc<str>, V>,
}

impl Modifier {
    fn field(&self, name: &str) -> PyResult<&V> {
        self.record.get(name).ok_or_else(|| Unsupported::new_err(RECORD_REFUSED))
    }

    fn text(&self, name: &str) -> PyResult<Arc<str>> {
        match self.field(name)? {
            V::Str(text) => Ok(text.clone()),
            _ => unsupported(RECORD_REFUSED),
        }
    }

    fn parameter(&self, name: &str) -> PyResult<&V> {
        match self.field("parameters")? {
            V::Map(parameters) => parameters.get(name).ok_or_else(|| Unsupported::new_err(RECORD_REFUSED)),
            _ => unsupported(RECORD_REFUSED),
        }
    }

    fn number(&self, name: &str) -> PyResult<f64> {
        match self.parameter(name)? {
            V::Float(number) => Ok(*number),
            _ => unsupported(RECORD_REFUSED),
        }
    }

    fn start_minute(&self) -> PyResult<i64> {
        match self.field("start_minute")? {
            V::Int(minute) => Ok(*minute),
            _ => unsupported(RECORD_REFUSED),
        }
    }

    /// `record["op"]`; only a str is one of the operations the rule compares it with.
    fn op(&self) -> PyResult<Option<&str>> {
        Ok(match self.field("op")? {
            V::Str(text) => Some(text),
            _ => None,
        })
    }
}

fn sorted_set(mut items: Vec<Arc<str>>) -> Vec<Arc<str>> {
    items.sort_unstable();
    items.dedup();
    items
}

/// What every zone's conditions read: None when the world has no zone environment.
pub(crate) struct ZoneInputs {
    parts: Option<(Config, Formulas, Vec<Modifier>)>,
}

impl ZoneInputs {
    /// The head of `zone_environment_view`: `config` and `state` as it finds them, and what it would
    /// raise on for any zone.
    pub(crate) fn read(configs: &Bound<'_, PyDict>, state: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = configs.py();
        let config = configs.get_item(intern!(py, "zone_environment"))?;
        let is_a_dict = |value: &Bound<'_, PyAny>| value.is_instance_of::<PyDict>();
        let (Some(config), true) = (config.filter(|config| is_a_dict(config)), is_a_dict(state)) else {
            return Ok(Self { parts: None });
        };
        let Some(config) = as_readable_dict(&config) else {
            return unsupported("the zone environment config is another kind of dict");
        };
        let config = read_config(config)?;
        let formulas = read_formulas(configs)?;
        let modifiers = state
            .get_item(intern!(py, "modifiers"))
            .map_err(|_| Unsupported::new_err("the zone environment state has no modifiers"))?;
        let V::Map(documents) = V::from_py(&modifiers)? else {
            return unsupported("the zone modifiers are not a mapping");
        };
        let mut modifiers = Vec::with_capacity(documents.len());
        for (id, document) in documents {
            let V::Map(record) = document else {
                return unsupported(RECORD_REFUSED);
            };
            let zone_id = match record.get("zone_id") {
                Some(V::Str(zone)) => Some(zone.clone()),
                Some(_) => None,
                None => return unsupported(RECORD_REFUSED),
            };
            modifiers.push(Modifier { id, zone_id, record });
        }
        Ok(Self { parts: Some((config, formulas, modifiers)) })
    }

    /// `zone_environment_view(world, zone_id)`.
    pub(crate) fn conditions(&self, zone_id: &str) -> PyResult<ZoneConditions> {
        let Some((config, formulas, modifiers)) = &self.parts else {
            return Ok(ZoneConditions::neutral());
        };
        let mut entries: Vec<(i64, &Modifier)> = Vec::new();
        for modifier in modifiers {
            if modifier.zone_id.as_deref() == Some(zone_id) {
                entries.push((modifier.start_minute()?, modifier));
            }
        }
        entries.sort_unstable_by(|first, second| (first.0, &first.1.id).cmp(&(second.0, &second.1.id)));
        let of_op = |op: &str| -> PyResult<Vec<&Modifier>> {
            let mut found = Vec::new();
            for (_, modifier) in &entries {
                if modifier.op()? == Some(op) {
                    found.push(*modifier);
                }
            }
            Ok(found)
        };
        let baseline = config.baseline_by_zone.get(zone_id).copied().unwrap_or(DEFAULT_BASELINE);
        let ventilation_records = of_op("set_ventilation")?;
        let lighting_records = of_op("set_lighting")?;
        let privacy_records = of_op("set_privacy")?;
        let temperature_records = of_op("set_temperature")?;
        let ventilation = match ventilation_records.last() {
            Some(record) => record.number("effectiveness")?,
            None => baseline.ventilation_effectiveness,
        };
        let lighting = match lighting_records.last() {
            Some(record) => record.number("level")?,
            None => baseline.lighting_level,
        };
        let privacy = match privacy_records.last() {
            Some(record) => record.number("level")?,
            None => baseline.privacy_level,
        };
        let temperature = match temperature_records.last() {
            Some(record) => record.number("temperature_c")?,
            None => baseline.temperature_c,
        };
        let mut condition_event_ids = Vec::new();
        let mut condition_sources = vec![config.source_id.clone()];
        let mut has_condition_records = false;
        for records in [&ventilation_records, &lighting_records, &privacy_records, &temperature_records] {
            for record in records {
                has_condition_records = true;
                condition_event_ids.push(record.text("start_event_id")?);
                condition_sources.push(record.text("source_id")?);
            }
        }
        // Not a set in the Python rule: an event that started two records shows twice.
        condition_event_ids.sort_unstable();
        condition_sources.extend(formulas.sources.iter().cloned());

        let degree_deviation = temperature_deviation(formulas.temperature, temperature, config);
        let noise_entries = of_op("set_ambient_noise")?;
        let mut noise_level: Option<f64> = None;
        for record in &noise_entries {
            let level = record.number("level")?;
            noise_level = Some(noise_level.map_or(level, |largest| py_max(largest, level)));
        }
        let noise_level = noise_level.unwrap_or(0.0);
        let rest = rest_efficiency(formulas.rest, noise_level, lighting, config);

        let mut by_action: Vec<(Arc<str>, Vec<&Modifier>)> = Vec::new();
        for modifier in of_op("modify_demand_rate")? {
            let V::Str(action) = modifier.parameter("action")? else {
                return unsupported(RECORD_REFUSED);
            };
            match by_action.iter_mut().find(|(known, _)| known == action) {
                Some((_, records)) => records.push(modifier),
                None => by_action.push((action.clone(), vec![modifier])),
            }
        }
        by_action.sort_unstable_by(|first, second| first.0.cmp(&second.0));
        let mut demand_modifiers = Vec::with_capacity(by_action.len());
        for (action, records) in by_action {
            // `math.prod`: in order, from 1.
            let (mut probability, mut duration) = (1.0_f64, 1.0_f64);
            for record in &records {
                probability *= record.number("probability_multiplier")?;
            }
            for record in &records {
                duration *= record.number("duration_multiplier")?;
            }
            let mut sources = vec![config.source_id.clone()];
            let mut event_ids = Vec::with_capacity(records.len());
            for record in &records {
                event_ids.push(record.text("start_event_id")?);
                sources.extend(assumption_ids(record)?);
                sources.push(record.text("source_id")?);
            }
            let duration_multiplier = if matches!(&*action, "rest" | "sleep") {
                py_min(2.0, py_max(0.5, duration))
            } else {
                1.0
            };
            demand_modifiers.push(DemandModifier {
                action,
                probability_multiplier: py_min(
                    config.max_probability_multiplier,
                    py_max(config.min_probability_multiplier, probability),
                ),
                duration_multiplier,
                modifier_event_ids: sorted_set(event_ids),
                assumption_ids: sorted_set(sources),
            });
        }

        let mut noise_sources = vec![config.source_id.clone()];
        let mut noise_event_ids = Vec::with_capacity(noise_entries.len());
        for record in &noise_entries {
            noise_event_ids.push(record.text("start_event_id")?);
            noise_sources.push(record.text("source_id")?);
            noise_sources.extend(assumption_ids(record)?);
        }
        Ok(ZoneConditions {
            ambient_noise_level: noise_level,
            rest_efficiency_multiplier: rest,
            noise_modifier_event_ids: sorted_set(noise_event_ids),
            noise_assumption_ids: if noise_entries.is_empty() { Vec::new() } else { sorted_set(noise_sources) },
            demand_modifiers,
            min_rest_decision_minutes: config.min_rest_decision_minutes,
            max_rest_decision_minutes: config.max_rest_decision_minutes,
            ventilation_effectiveness: ventilation,
            lighting_level: lighting,
            privacy_level: privacy,
            temperature_c: temperature,
            ventilation_hygiene_growth_per_minute: ventilation_hygiene_growth(formulas.hygiene, ventilation, config),
            privacy_discontent_per_minute: privacy_discontent(formulas.privacy, privacy, config),
            temperature_stress_per_minute: degree_deviation * config.temperature_stress_per_degree_minute,
            condition_modifier_event_ids: condition_event_ids,
            condition_assumption_ids: if has_condition_records || config.is_v2 {
                sorted_set(condition_sources)
            } else {
                Vec::new()
            },
        })
    }
}

fn assumption_ids(record: &Modifier) -> PyResult<Vec<Arc<str>>> {
    let V::List(items) = record.field("assumption_ids")? else {
        return unsupported(RECORD_REFUSED);
    };
    items
        .iter()
        .map(|item| match item {
            V::Str(text) => Ok(text.clone()),
            _ => unsupported(RECORD_REFUSED),
        })
        .collect()
}

// ---- For the Python readers outside a tick, the shadow and the tests -------------------------------

fn texts<'py>(py: Python<'py>, items: &[Arc<str>]) -> PyResult<Bound<'py, PyTuple>> {
    PyTuple::new(py, items.iter().map(|item| &**item))
}

/// `zone_environment_view(world, zone_id)` as the Python dataclass. Says Unsupported for what the
/// Python rule raises on, and for what a view could not hold.
#[pyfunction]
fn view_of<'py>(world: &Bound<'py, PyAny>, zone_id: &str) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let configs = world.getattr(intern!(py, "model_configs"))?;
    let Some(configs) = as_readable_dict(&configs) else {
        return unsupported("the configs are not a plain dict");
    };
    let state = world
        .getattr(intern!(py, "model_state"))?
        .call_method1(intern!(py, "get"), (intern!(py, "zone_environment_v1"),))?;
    let conditions = ZoneInputs::read(configs, &state)?.conditions(zone_id)?;
    let module = py.import("sheltersim.domain.environment")?;
    let demand_view = module.getattr("ZoneDemandView")?;
    let mut demand = Vec::with_capacity(conditions.demand_modifiers.len());
    for modifier in &conditions.demand_modifiers {
        demand.push(demand_view.call1((
            &*modifier.action,
            modifier.probability_multiplier,
            modifier.duration_multiplier,
            texts(py, &modifier.modifier_event_ids)?,
            texts(py, &modifier.assumption_ids)?,
        ))?);
    }
    let fields = PyDict::new(py);
    fields.set_item("ambient_noise_level", conditions.ambient_noise_level)?;
    fields.set_item("rest_efficiency_multiplier", conditions.rest_efficiency_multiplier)?;
    fields.set_item("noise_modifier_event_ids", texts(py, &conditions.noise_modifier_event_ids)?)?;
    fields.set_item("noise_assumption_ids", texts(py, &conditions.noise_assumption_ids)?)?;
    fields.set_item("demand_modifiers", PyTuple::new(py, demand)?)?;
    fields.set_item("min_rest_decision_minutes", conditions.min_rest_decision_minutes)?;
    fields.set_item("max_rest_decision_minutes", conditions.max_rest_decision_minutes)?;
    fields.set_item("ventilation_effectiveness", conditions.ventilation_effectiveness)?;
    fields.set_item("lighting_level", conditions.lighting_level)?;
    fields.set_item("privacy_level", conditions.privacy_level)?;
    fields.set_item("temperature_c", conditions.temperature_c)?;
    fields.set_item("ventilation_hygiene_growth_per_minute", conditions.ventilation_hygiene_growth_per_minute)?;
    fields.set_item("privacy_discontent_per_minute", conditions.privacy_discontent_per_minute)?;
    fields.set_item("temperature_stress_per_minute", conditions.temperature_stress_per_minute)?;
    fields.set_item("condition_modifier_event_ids", texts(py, &conditions.condition_modifier_event_ids)?)?;
    fields.set_item("condition_assumption_ids", texts(py, &conditions.condition_assumption_ids)?)?;
    module.getattr("ZoneEnvironmentView")?.call((), Some(&fields))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(view_of, m)?)?;
    Ok(())
}
