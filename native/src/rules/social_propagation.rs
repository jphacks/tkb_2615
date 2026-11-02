//! The social model's step of the update phase: `_social_propagation` in
//! `experiments/phases/update.py`, with the rules of `StressPropagationPolicy` and
//! `ExpressivenessChannelOpennessPolicy` (`policies/social.py`) and `DeGrootContactPolicy`
//! (`policies/alternatives.py`), which stay as the comparison base.
//!
//! The kernel changes nothing until it knows the Python rule would pass: `plan` computes everything,
//! and any value the Python rule turns down, or that this kernel does not handle, gives Unsupported,
//! so the Python step runs and raises its own text. A trace, an RNG other than `CounterRNG` and a
//! subclass of a policy go to Python as well. Then the plan goes to the row and the tree, whose
//! dicts have the keys in the order Python's new dicts have them, without replacing the namespace.
//! Every float operation keeps the order of the Python expression.
//!
//! Stress, zones and sensitivities come from the entity rows. The group comes from the snapshot of
//! `rules/world_view.rs`: a person's household in `population_presence_v1` overrides
//! `profile.group_id` there.

use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyInt, PyList, PyString, PyTuple, PyType};
use pyo3::IntoPyObjectExt;
use rustc_hash::FxBuildHasher;

use crate::contacts::{
    exact_float, exact_int, key_head, nearby_buckets, pair_key, quoted_ids, select_contacts, zone_members,
    Basis, Bucket,
};
use crate::core::mathx::{py_fsum, py_max, py_min, py_sum};
use crate::core::row::{Field, IdMap};
use crate::core::rng::{start_draw_log, take_draw_log, KeyedDraws};
use crate::core::tracked::{as_readable_dict, as_readable_list};
use crate::core::world_core::{mirror_of, NamespaceMirror};
use crate::rules::social_network::record_observed_contacts;
use crate::rules::world_view::view_of;
use crate::state::entities::{entities_of, EntityStore};
use crate::state::social_propagation::{ContactRecord, SocialRow, NAME, SCHEMA_VERSION};
use crate::Unsupported;

#[track_caller]
fn unsupported<T>(reason: &'static str) -> PyResult<T> {
    Err(Unsupported::new_err(reason))
}

#[derive(Clone, Copy, PartialEq)]
enum Policy {
    Sparse,
    Channel,
    DeGroot,
}

/// `SocialPropagationParameters`, each of its exact type.
struct Settings {
    policy: Policy,
    maximum: i64,
    minimum_contact_minutes: i64,
    cooldown_minutes: i64,
    spread: f64,
    recovery: f64,
    pair_cap: f64,
    agent_cap: f64,
    family_strength: f64,
    zone_strength: f64,
    nearby_strength: f64,
}

/// One person's values of this tick. The people of a plan are sorted by id, and differ in id.
struct Person<'a> {
    id: &'a str,
    group: Option<&'a str>,
    zone: &'a str,
    stress: f64,
    sensitivity: f64,
    expressiveness: f64,
    /// The stress of the psychology delta.
    base: f64,
}

struct Plan {
    /// The new stress of each person.
    stresses: Vec<f64>,
    /// The new contacts, in the order of Python's new dict.
    contacts: IdMap<ContactRecord>,
    /// The new cooldowns, sorted, or None if the row has these already.
    cooldowns: Option<IdMap<i64>>,
    /// (recipient, source) of what the step records in `social_network_v1`, in its order.
    observed: Vec<(usize, usize)>,
}

fn checked<T>(value: Option<T>) -> PyResult<T> {
    value.ok_or_else(|| Unsupported::new_err("a minute does not fit"))
}

fn plan(
    seed: &str,
    minute: i64,
    settings: &Settings,
    people: &[Person<'_>],
    neighbors: &HashMap<String, Vec<String>>,
    previous: &IdMap<ContactRecord>,
    cooldowns_before: &IdMap<i64>,
) -> PyResult<Plan> {
    if previous.values().any(|record| record.last_seen_minute < 0 || record.consecutive_minutes < 0) {
        return unsupported("a contact field is negative");
    }
    if cooldowns_before.values().any(|&due| due < 0) {
        return unsupported("a cooldown is negative");
    }
    let last_minute = checked(minute.checked_sub(1))?;
    let cooldown_after = checked(minute.checked_add(settings.cooldown_minutes))?;

    let ids: Vec<&str> = people.iter().map(|person| person.id).collect();
    let zones: Vec<&str> = people.iter().map(|person| person.zone).collect();
    let quoted = quoted_ids(&ids);
    let members = zone_members(&zones);
    let mut group_members: HashMap<&str, Vec<usize>> = HashMap::new();
    for (index, person) in people.iter().enumerate() {
        if let Some(group) = person.group {
            group_members.entry(group).or_default().push(index);
        }
    }
    let nearby_of_zone: HashMap<&str, Vec<&[usize]>> = members
        .keys()
        .map(|&zone| (zone, nearby_buckets(zone, neighbors, &members)))
        .collect();

    let keep_all = settings.policy == Policy::Sparse;
    let mut cooldowns: BTreeMap<&str, i64> = cooldowns_before
        .iter()
        .filter(|(_, &due)| keep_all || due > minute)
        .map(|(id, &due)| (&**id, due))
        .collect();
    if settings.policy == Policy::DeGroot {
        cooldowns.clear();
    }

    let draws = KeyedDraws::new(seed);
    let mut stresses = Vec::with_capacity(people.len());
    let mut contacts = IdMap::with_capacity_and_hasher(people.len() * 3, FxBuildHasher);
    let mut observed = Vec::with_capacity(people.len() * 3);
    let mut selected = Vec::new();
    let mut buckets = Vec::new();
    let mut lasting: Vec<(usize, Basis)> = Vec::new();
    for (me, person) in people.iter().enumerate() {
        buckets.clear();
        if let Some(group) = person.group {
            let family = &group_members[group];
            if family.len() > 1 {
                buckets.push(Bucket { basis: Basis::Family, members: family, includes_self: true });
            }
        }
        let same_zone = &members[person.zone];
        if same_zone.len() > 1 {
            buckets.push(Bucket { basis: Basis::SameZone, members: same_zone, includes_self: true });
        }
        for &nearby in &nearby_of_zone[person.zone] {
            buckets.push(Bucket { basis: Basis::Nearby, members: nearby, includes_self: false });
        }
        let mut head = key_head("social-contact-bucket-v1", minute, &quoted[me]);
        select_contacts(&draws, &mut head, me, &buckets, settings.maximum, true, &mut selected);

        lasting.clear();
        let mut additions = 0.0;
        for &(source, basis) in &selected {
            let key = pair_key(&quoted[me], &quoted[source])?;
            let duration = match previous.get(key.as_str()) {
                Some(record) if record.last_seen_minute == last_minute => {
                    checked(record.consecutive_minutes.checked_add(1))?
                }
                _ => 1,
            };
            contacts.insert(
                Arc::from(key),
                ContactRecord { last_seen_minute: minute, consecutive_minutes: duration },
            );
            if duration >= settings.minimum_contact_minutes {
                lasting.push((source, basis));
                if settings.policy == Policy::Sparse
                    && minute >= cooldowns.get(person.id).copied().unwrap_or(0)
                {
                    let difference = py_max(0.0, people[source].stress - person.stress);
                    let contribution =
                        py_min(settings.pair_cap, difference * settings.spread * duration as f64);
                    let remaining = py_max(
                        0.0,
                        py_min(
                            settings.agent_cap - additions,
                            1.0 - py_max(0.0, person.base - settings.recovery) - additions,
                        ),
                    );
                    let contribution = py_min(contribution, remaining);
                    if contribution > 0.0 {
                        cooldowns.insert(person.id, cooldown_after);
                        additions += contribution;
                    }
                }
            }
        }

        let stress = match settings.policy {
            Policy::Sparse => {
                // Every selected contact, by source (people are indexed in ID order).
                let start = observed.len();
                observed.extend(selected.iter().map(|&(source, _)| (me, source)));
                observed[start..].sort_unstable();
                py_min(1.0, py_max(0.0, person.base - settings.recovery + additions))
            }
            Policy::Channel => {
                lasting.sort_unstable_by_key(|contact| contact.0);
                observed.extend(lasting.iter().map(|&(source, _)| (me, source)));
                let can_contribute = minute >= cooldowns_before.get(person.id).copied().unwrap_or(0);
                let (stress, shift) = channel_stress(
                    settings,
                    people,
                    person,
                    if can_contribute { &lasting } else { &[] },
                )?;
                if shift != 0.0 {
                    cooldowns.insert(person.id, cooldown_after);
                }
                stress
            }
            Policy::DeGroot => {
                lasting.sort_unstable_by_key(|contact| contact.0);
                observed.extend(lasting.iter().map(|&(source, _)| (me, source)));
                degroot_stress(settings, people, person, &lasting)
            }
        };
        // `replace` of the Python rule checks the value it stores.
        if !(0.0..=1.0).contains(&stress) {
            return unsupported("a new stress is outside 0..1");
        }
        stresses.push(stress);
    }

    let new_cooldowns: Vec<(&str, i64)> =
        cooldowns.into_iter().filter(|&(_, due)| due > minute).collect();
    let unchanged = cooldowns_before.len() == new_cooldowns.len()
        && cooldowns_before
            .iter()
            .zip(&new_cooldowns)
            .all(|((id, due), (new_id, new_due))| &**id == *new_id && due == new_due);
    let cooldowns = (!unchanged).then(|| {
        new_cooldowns.into_iter().map(|(id, due)| (Arc::from(id), due)).collect::<IdMap<i64>>()
    });
    Ok(Plan { stresses, contacts, cooldowns, observed })
}

/// The new stress and the shift of `ExpressivenessChannelOpennessPolicy.update`. `eligible` are by
/// source.
fn channel_stress(
    settings: &Settings,
    people: &[Person<'_>],
    person: &Person<'_>,
    eligible: &[(usize, Basis)],
) -> PyResult<(f64, f64)> {
    let weights: Vec<f64> = eligible
        .iter()
        .map(|&(source, basis)| {
            let channel = match basis {
                Basis::Family => settings.family_strength,
                Basis::SameZone => settings.zone_strength,
                Basis::Nearby => settings.nearby_strength,
            };
            people[source].expressiveness * channel * person.sensitivity
        })
        .collect();
    let weight_total =
        py_fsum(&weights).ok_or_else(|| Unsupported::new_err("weights are not finite"))?;
    // The Python code raises for contacts without any weight (its strict zip).
    if !eligible.is_empty() && !(weight_total > 0.0) {
        return unsupported("eligible contacts have no weight");
    }
    let mut shift = 0.0;
    if weight_total > 0.0 {
        let gamma = py_min(1.0, settings.spread * weight_total);
        let pair_shifts: Vec<f64> = eligible
            .iter()
            .zip(&weights)
            .map(|(&(source, _), &weight)| {
                let value = gamma * weight / weight_total * (people[source].stress - person.stress);
                py_max(-settings.pair_cap, py_min(settings.pair_cap, value))
            })
            .collect();
        let raw_shift =
            py_fsum(&pair_shifts).ok_or_else(|| Unsupported::new_err("shifts are not finite"))?;
        shift = py_max(-settings.agent_cap, py_min(settings.agent_cap, raw_shift));
        shift = py_max(-person.base, py_min(1.0 - person.base, shift));
    }
    Ok((py_min(1.0, py_max(0.0, person.base + shift)), shift))
}

/// The new stress of `DeGrootContactPolicy.update`. `neighbors` are by source.
fn degroot_stress(
    settings: &Settings,
    people: &[Person<'_>],
    person: &Person<'_>,
    neighbors: &[(usize, Basis)],
) -> f64 {
    let alpha = py_min(1.0, settings.spread);
    let count = neighbors.len().max(1) as f64;
    let contributions: Vec<f64> = neighbors
        .iter()
        .map(|&(source, _)| {
            let value = alpha * (people[source].stress - person.stress) / count;
            py_max(-settings.pair_cap, py_min(settings.pair_cap, value))
        })
        .collect();
    // An empty sum is the int 0 in Python, which every use below treats as 0.0.
    let raw_shift = py_sum(&contributions);
    let shift = py_max(-settings.agent_cap, py_min(settings.agent_cap, raw_shift));
    let recovered = py_max(0.0, person.base - settings.recovery);
    let applied = py_min(1.0, py_max(0.0, recovered + shift)) - recovered;
    recovered + applied
}

// ---- The step ----------------------------------------------------------------------------------

struct Classes {
    sparse: Py<PyType>,
    channel: Py<PyType>,
    degroot: Py<PyType>,
    counter_rng: Py<PyType>,
    delta: Py<PyType>,
}

static CLASSES: GILOnceCell<Classes> = GILOnceCell::new();

fn classes(py: Python<'_>) -> PyResult<&'static Classes> {
    CLASSES.get_or_try_init(py, || {
        let class = |module: &str, name: &str| -> PyResult<Py<PyType>> {
            Ok(py.import(module)?.getattr(name)?.downcast_into::<PyType>()?.unbind())
        };
        Ok(Classes {
            sparse: class("sheltersim.policies.social", "StressPropagationPolicy")?,
            channel: class("sheltersim.policies.social", "ExpressivenessChannelOpennessPolicy")?,
            degroot: class("sheltersim.policies.alternatives", "DeGrootContactPolicy")?,
            counter_rng: class("sheltersim.core.rng", "CounterRNG")?,
            delta: class("sheltersim.policies.psychology", "AgentStateDelta")?,
        })
    })
}

fn settings_of(policy: &Bound<'_, PyAny>, parameters: &Bound<'_, PyAny>, known: &Classes) -> PyResult<Settings> {
    let py = policy.py();
    let kind = policy.get_type();
    let policy = if kind.is(&known.channel) {
        Policy::Channel
    } else if kind.is(&known.sparse) {
        Policy::Sparse
    } else if kind.is(&known.degroot) {
        Policy::DeGroot
    } else {
        return unsupported("a policy this kernel does not know");
    };
    let int = |name: &Bound<'_, PyString>| exact_int(&parameters.getattr(name)?);
    let float = |name: &Bound<'_, PyString>| exact_float(&parameters.getattr(name)?);
    Ok(Settings {
        policy,
        maximum: int(intern!(py, "max_contacts_per_agent"))?,
        minimum_contact_minutes: int(intern!(py, "minimum_contact_minutes"))?,
        cooldown_minutes: int(intern!(py, "cooldown_minutes"))?,
        spread: float(intern!(py, "stress_spread_per_minute"))?,
        recovery: float(intern!(py, "natural_recovery_per_minute"))?,
        pair_cap: float(intern!(py, "max_pair_stress_increase"))?,
        agent_cap: float(intern!(py, "max_agent_stress_increase_per_minute"))?,
        family_strength: float(intern!(py, "family_channel_strength"))?,
        zone_strength: float(intern!(py, "same_zone_channel_strength"))?,
        nearby_strength: float(intern!(py, "nearby_zone_channel_strength"))?,
    })
}

fn unit_stress(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    let value = exact_float(value)?;
    if !(0.0..=1.0).contains(&value) {
        return unsupported("a stress is outside 0..1");
    }
    Ok(value)
}

/// `sorted(zone_neighbors.get(zone, ()))` needs these as strs.
fn neighbors_of(zone_neighbors: &Bound<'_, PyAny>, zones: &[&str]) -> PyResult<HashMap<String, Vec<String>>> {
    let py = zone_neighbors.py();
    let Some(table) = as_readable_dict(zone_neighbors) else {
        return unsupported("the zone neighbors are not a dict");
    };
    let mut found = HashMap::with_capacity(zones.len());
    for &zone in zones {
        if found.contains_key(zone) {
            continue;
        }
        let Some(names) = table.get_item(PyString::new(py, zone))? else {
            continue;
        };
        let mut texts = Vec::with_capacity(4);
        let mut add = |name: Bound<'_, PyAny>| -> PyResult<()> {
            let Ok(name) = name.downcast_exact::<PyString>() else {
                return unsupported("a neighbor is not a str");
            };
            texts.push(name.to_str().map_err(|_| Unsupported::new_err("not UTF-8"))?.to_owned());
            Ok(())
        };
        if let Ok(names) = names.downcast_exact::<PyTuple>() {
            names.iter().try_for_each(&mut add)?;
        } else if let Some(names) = as_readable_list(&names) {
            names.iter().try_for_each(&mut add)?;
        } else {
            return unsupported("the neighbors of a zone are not a tuple or a list");
        }
        found.insert(zone.to_owned(), texts);
    }
    Ok(found)
}

/// One person's delta and what the view says of them.
struct Input<'py> {
    delta: Bound<'py, PyAny>,
    id: Bound<'py, PyString>,
    group: Option<Arc<str>>,
    base: f64,
    expressiveness: f64,
}

/// Python orders str by code point, the same as UTF-8 bytes. Ids were checked to have UTF-8.
fn id_bytes<'a>(input: &'a Input<'_>) -> &'a [u8] {
    input.id.to_str().unwrap_or("").as_bytes()
}

/// `_social_propagation` of the update phase (experiments/phases/segments.py).
#[pyfunction]
pub(crate) fn update_social_propagation<'py>(
    controller: &Bound<'py, PyAny>,
    world: &Bound<'py, PyAny>,
    view: &Bound<'py, PyAny>,
    base_deltas: &Bound<'py, PyAny>,
    minute: &Bound<'py, PyAny>,
    rng: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = world.py();
    let policy = controller.getattr(intern!(py, "stress_policy"))?;
    let parameters = policy.getattr(intern!(py, "parameters"))?;
    if !parameters.getattr(intern!(py, "enabled"))?.is_truthy()? {
        return Ok(base_deltas.clone());
    }
    let trace = controller.getattr(intern!(py, "artifacts"))?.getattr(intern!(py, "trace_level"))?;
    if !trace.getattr(intern!(py, "value"))?.eq(intern!(py, "off"))? {
        return unsupported("a trace needs the Python contact records");
    }
    let known = classes(py)?;
    if !rng.get_type().is(&known.counter_rng) {
        return unsupported("the RNG is not a CounterRNG");
    }
    let settings = settings_of(&policy, &parameters, known)?;
    let minute_value = exact_int(minute)?;
    if minute_value < 0 || exact_int(&view.getattr(intern!(py, "minute_index"))?)? != minute_value {
        return unsupported("the view is at another minute");
    }
    let Ok(deltas) = base_deltas.downcast_exact::<PyTuple>() else {
        return unsupported("the deltas are not a tuple");
    };

    // Who is here, with what only Python holds.
    let rust_view = view_of(view)?;
    let view_data = rust_view.get().data();
    if view_data.agents.len() != deltas.len() {
        return unsupported("the view and the deltas have different people");
    }
    let expressiveness = parameters.getattr(intern!(py, "source_expressiveness_by_agent"))?;
    let Some(expressiveness) = as_readable_dict(&expressiveness) else {
        return unsupported("the expressiveness is not a dict");
    };
    let mut inputs = Vec::with_capacity(deltas.len());
    for delta in deltas.iter() {
        if !delta.get_type().is(&known.delta) {
            return unsupported("a delta of another class");
        }
        let id = delta.getattr(intern!(py, "agent_id"))?;
        let Ok(id) = id.downcast_into_exact::<PyString>() else {
            return unsupported("an id is not a str");
        };
        if id.to_str().is_err() {
            return unsupported("an id is not UTF-8");
        }
        if exact_int(&delta.getattr(intern!(py, "minute_index"))?)? != minute_value {
            return unsupported("a delta is at another minute");
        }
        let Some(agent) = view_data.index_of(id.to_str()?) else {
            return unsupported("the view and the deltas have different people");
        };
        let group = view_data.agents[agent].group_id.clone();
        let source_value = if settings.policy == Policy::Channel && !expressiveness.is_empty() {
            match expressiveness.get_item(&id)? {
                Some(value) => exact_float(&value)?,
                None => 1.0,
            }
        } else {
            1.0
        };
        inputs.push(Input {
            base: unit_stress(&delta.getattr(intern!(py, "stress"))?)?,
            delta,
            id,
            group,
            expressiveness: source_value,
        });
    }
    if !inputs.windows(2).all(|pair| id_bytes(&pair[0]) < id_bytes(&pair[1])) {
        // Deltas come sorted. The Python rules sort them, except DeGroot's, which keeps their order.
        if settings.policy == Policy::DeGroot {
            return unsupported("the deltas are not in id order");
        }
        inputs.sort_unstable_by(|first, second| id_bytes(first).cmp(id_bytes(second)));
        if inputs.windows(2).any(|pair| id_bytes(&pair[0]) == id_bytes(&pair[1])) {
            return unsupported("two deltas for one person");
        }
    }

    // The entity rows.
    let rows = EntityStore::with_rows(&entities_of(world)?, |rows| {
        if rows.agents.len() != inputs.len() {
            return unsupported("the view and the world have different people");
        }
        let mut found = Vec::with_capacity(inputs.len());
        for input in &inputs {
            let id = input.id.to_str()?;
            let Some(row) = rows.agents.get(id) else {
                return unsupported("the view and the world have different people");
            };
            if &*row.profile.id != id {
                return unsupported("a person's key is not their id");
            }
            if !(0.0..=1.0).contains(&row.stress) || !row.profile.stress_sensitivity.is_finite() {
                return unsupported("a stress is outside 0..1");
            }
            found.push((row.zone_id.clone(), row.stress, row.profile.stress_sensitivity));
        }
        Ok(found)
    })?;
    let zones: Vec<&str> = rows.iter().map(|(zone, _, _)| &**zone).collect();
    let neighbors = neighbors_of(&controller.getattr(intern!(py, "zone_neighbors"))?, &zones)?;
    let mut people = Vec::with_capacity(inputs.len());
    for (input, (zone, stress, sensitivity)) in inputs.iter().zip(&rows) {
        people.push(Person {
            id: input.id.to_str()?,
            group: input.group.as_deref(),
            zone,
            stress: *stress,
            sensitivity: *sensitivity,
            expressiveness: input.expressiveness,
            base: input.base,
        });
    }

    let seed = rng.getattr(intern!(py, "seed"))?;
    if !seed.is_exact_instance_of::<PyInt>() {
        return unsupported("the seed is not an int");
    }
    let seed = seed.str()?;
    let mirror = mirror_of(world, NAME)?;
    let Plan { stresses, contacts, cooldowns, observed } =
        NamespaceMirror::with_row::<SocialRow, _>(&mirror, |row| {
            if row.schema_version != SCHEMA_VERSION {
                return unsupported("another schema version");
            }
            plan(
                seed.to_str()?,
                minute_value,
                &settings,
                &people,
                &neighbors,
                &row.contacts,
                &row.agent_cooldown_until,
            )
        })?;

    // The deltas first: they run a little Python, and nothing is written yet. A delta that keeps
    // its stress is itself, since a delta is frozen. The people are in id order, as the Python
    // rules sort them; DeGroot's keeps the order of the deltas, which was checked to be the same.
    let mut final_deltas = Vec::with_capacity(inputs.len());
    for (input, stress) in inputs.iter().zip(&stresses) {
        final_deltas.push(if stress.to_bits() == input.base.to_bits() {
            input.delta.clone()
        } else {
            input.delta.call_method1(intern!(py, "with_stress"), (*stress,))?
        });
    }
    let final_deltas = PyTuple::new(py, final_deltas)?;

    // The row and the tree. The namespace's dict gets the order of the Python rule's new one too.
    NamespaceMirror::with_row_tree::<SocialRow, _>(&mirror, |row, tree| {
        tree.order_keys([
            intern!(py, "schema_version"),
            intern!(py, "contacts"),
            intern!(py, "agent_cooldown_until"),
        ])?;
        if let Some(cooldowns) = cooldowns {
            let node = tree.child(intern!(py, "agent_cooldown_until"))?;
            node.clear()?;
            for (id, due) in &cooldowns {
                node.set_str(id, due)?;
            }
            row.agent_cooldown_until = cooldowns;
        }
        Ok(())
    })?;
    // Python's dicts share the minute's one int, and so do these.
    let minute_object = minute.clone();
    NamespaceMirror::refill_record_map(
        &mirror,
        intern!(py, "contacts"),
        [intern!(py, "last_seen_minute"), intern!(py, "consecutive_minutes")],
        contacts.iter().map(|(key, record)| {
            Ok((
                PyString::new(py, key),
                [minute_object.clone(), record.consecutive_minutes.into_bound_py_any(py)?],
            ))
        }),
    )?;
    NamespaceMirror::with_row::<SocialRow, _>(&mirror, |row| {
        row.contacts = contacts;
        Ok(())
    })?;

    // What it records in `social_network_v1`, after every write, as the Python step does.
    if world.getattr(intern!(py, "model_state"))?.contains(intern!(py, "social_network_v1"))? {
        let mut ids = Vec::with_capacity(observed.len());
        for &(recipient, source) in &observed {
            ids.push((inputs[recipient].id.to_str()?, inputs[source].id.to_str()?));
        }
        match record_observed_contacts(&world, &ids, minute_value) {
            Err(error) if error.is_instance_of::<Unsupported>(py) => {
                // That namespace's own rules decide, in their own words; this one is written.
                let pairs = PyList::empty(py);
                for &(recipient, source) in &observed {
                    pairs.append((&inputs[recipient].id, &inputs[source].id))?;
                }
                py.import(intern!(py, "sheltersim.domain.social_network"))?
                    .getattr(intern!(py, "record_observed_contacts"))?
                    .call1((world, pairs, minute))?;
            }
            other => other?,
        }
    }
    Ok(final_deltas.into_any())
}

// ---- For the bit comparisons of tests/integration/test_social_propagation_row.py ---------------

/// `plan` from plain values: `ids` sorted, `rows` of (group or None, zone, stress, sensitivity,
/// expressiveness, base stress), ints (max contacts, minimum minutes, cooldown) and floats (spread,
/// recovery, pair cap, agent cap, family, same zone, nearby). Returns the stresses, the contacts
/// and cooldowns as lists in order, and the observed pairs. It reads the state as a row does.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
fn plan_of_values(
    policy: &str,
    seed: &str,
    minute: i64,
    ints: [i64; 3],
    floats: [f64; 7],
    ids: Vec<String>,
    rows: Vec<(Option<String>, String, f64, f64, f64, f64)>,
    neighbors: HashMap<String, Vec<String>>,
    previous: &Bound<'_, PyAny>,
    cooldowns: &Bound<'_, PyAny>,
) -> PyResult<(Vec<f64>, Vec<(String, i64, i64)>, Vec<(String, i64)>, Vec<(usize, usize)>)> {
    let misfit = |error: crate::core::world_core::Misfit| Unsupported::new_err(error.0);
    let previous = IdMap::<ContactRecord>::from_py(previous).map_err(misfit)?;
    let cooldowns = IdMap::<i64>::from_py(cooldowns).map_err(misfit)?;
    let settings = Settings {
        policy: match policy {
            "sparse" => Policy::Sparse,
            "channel" => Policy::Channel,
            "degroot" => Policy::DeGroot,
            _ => return unsupported("a policy this kernel does not know"),
        },
        maximum: ints[0],
        minimum_contact_minutes: ints[1],
        cooldown_minutes: ints[2],
        spread: floats[0],
        recovery: floats[1],
        pair_cap: floats[2],
        agent_cap: floats[3],
        family_strength: floats[4],
        zone_strength: floats[5],
        nearby_strength: floats[6],
    };
    let people: Vec<Person<'_>> = ids
        .iter()
        .zip(&rows)
        .map(|(id, (group, zone, stress, sensitivity, expressiveness, base))| Person {
            id,
            group: group.as_deref(),
            zone,
            stress: *stress,
            sensitivity: *sensitivity,
            expressiveness: *expressiveness,
            base: *base,
        })
        .collect();
    let plan = plan(seed, minute, &settings, &people, &neighbors, &previous, &cooldowns)?;
    let new_cooldowns = plan.cooldowns.as_ref().unwrap_or(&cooldowns);
    Ok((
        plan.stresses,
        plan.contacts
            .iter()
            .map(|(key, record)| (key.to_string(), record.last_seen_minute, record.consecutive_minutes))
            .collect(),
        new_cooldowns.iter().map(|(id, due)| (id.to_string(), *due)).collect(),
        plan.observed,
    ))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(update_social_propagation, m)?)?;
    m.add_function(wrap_pyfunction!(plan_of_values, m)?)?;
    m.add_function(wrap_pyfunction!(start_draw_log, m)?)?;
    m.add_function(wrap_pyfunction!(take_draw_log, m)?)?;
    Ok(())
}
