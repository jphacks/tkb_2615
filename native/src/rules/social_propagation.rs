//! The social model's bounded stress synchrony (`ExpressivenessChannelOpennessPolicy.update`),
//! under the same rules as `contacts.rs`.

use std::collections::{BTreeMap, HashMap};

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};

use crate::contacts::{
    contact_entry, exact_dict, exact_float, exact_int, key_head, nearby_buckets, pair_key, previous_contacts,
    quoted_ids, select_contacts, texts, zone_members, Basis, Bucket,
};
use crate::core::mathx::{py_fsum, py_max, py_min};
use crate::core::rng::KeyedDraws;
use crate::Unsupported;

/// One tick of `ExpressivenessChannelOpennessPolicy.update`. `ids` are sorted and
/// `rows` hold `(group or None, zone, stress, stress_sensitivity, expressiveness,
/// base stress)` for each. `floats` are the stress spread, the pair cap, the agent
/// cap and the family, same-zone and nearby channel strengths. Returns the new
/// stresses, the contact records, the cooldowns, the contact traces (recipient,
/// source, basis, minutes, cooldown left and six floats), the contributing count and
/// the total shift.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn social_channel_update<'py>(
    py: Python<'py>,
    seed: &str,
    minute: i64,
    maximum: i64,
    minimum_contact_minutes: i64,
    cooldown_minutes: i64,
    floats: [f64; 6],
    ids: Vec<Bound<'py, PyString>>,
    rows: &Bound<'py, PyList>,
    neighbors: HashMap<String, Vec<String>>,
    previous: &Bound<'py, PyDict>,
    previous_cooldowns: &Bound<'py, PyDict>,
) -> PyResult<(
    Vec<f64>,
    Bound<'py, PyDict>,
    Bound<'py, PyDict>,
    Vec<(usize, usize, &'static str, i64, i64, [f64; 7])>,
    i64,
    f64,
)> {
    let [spread, pair_cap, agent_cap, family_strength, zone_strength, nearby_strength] = floats;
    let id_texts = texts(&ids)?;
    if rows.len() != id_texts.len() {
        return Err(Unsupported::new_err("people and rows differ in length"));
    }
    let mut group_values = Vec::with_capacity(rows.len());
    let mut zone_values = Vec::with_capacity(rows.len());
    let mut stress = Vec::with_capacity(rows.len());
    let mut sensitivity = Vec::with_capacity(rows.len());
    let mut expressiveness = Vec::with_capacity(rows.len());
    let mut base = Vec::with_capacity(rows.len());
    for row in rows.iter() {
        let (group, zone, a, b, c, d): (
            Option<Bound<'py, PyString>>,
            Bound<'py, PyString>,
            Bound<'py, PyAny>,
            Bound<'py, PyAny>,
            Bound<'py, PyAny>,
            Bound<'py, PyAny>,
        ) = row.extract()?;
        group_values.push(group);
        zone_values.push(zone);
        stress.push(exact_float(&a)?);
        sensitivity.push(exact_float(&b)?);
        expressiveness.push(exact_float(&c)?);
        base.push(exact_float(&d)?);
    }
    let zone_texts = texts(&zone_values)?;
    let mut group_members: HashMap<&str, Vec<usize>> = HashMap::new();
    let mut group_texts = Vec::with_capacity(rows.len());
    for (index, group) in group_values.iter().enumerate() {
        let text = match group {
            Some(group) => Some(
                group
                    .to_str()
                    .map_err(|_| Unsupported::new_err("not UTF-8"))?,
            ),
            None => None,
        };
        if let Some(text) = text {
            group_members.entry(text).or_default().push(index);
        }
        group_texts.push(text);
    }
    let previous = previous_contacts(previous)?;
    let mut cooldown_before: HashMap<String, i64> =
        HashMap::with_capacity(previous_cooldowns.len());
    for (key, value) in exact_dict(previous_cooldowns.as_any())?.iter() {
        let key = key
            .downcast_exact::<PyString>()
            .map_err(|_| Unsupported::new_err("cooldown key is not a str"))?
            .to_str()
            .map_err(|_| Unsupported::new_err("not UTF-8"))?
            .to_owned();
        let due = exact_int(&value)?;
        if due < 0 {
            return Err(Unsupported::new_err("cooldown is negative"));
        }
        cooldown_before.insert(key, due);
    }
    let mut cooldowns: BTreeMap<&str, i64> = cooldown_before
        .iter()
        .filter(|(_, &due)| due > minute)
        .map(|(id, &due)| (id.as_str(), due))
        .collect();

    let draws = KeyedDraws::new(seed);
    let quoted = quoted_ids(&id_texts);
    let members = zone_members(&zone_texts);
    let current = PyDict::new(py);
    let mut output = Vec::with_capacity(id_texts.len());
    let mut traces = Vec::new();
    let mut total_shift = 0.0;
    let mut contributing = 0;
    let mut selected = Vec::new();
    for (me, &zone) in zone_texts.iter().enumerate() {
        let mut buckets = Vec::new();
        if let Some(group) = group_texts[me] {
            let family = &group_members[group];
            if family.len() > 1 {
                buckets.push(Bucket {
                    basis: Basis::Family,
                    members: family,
                    includes_self: true,
                });
            }
        }
        let same_zone = &members[zone];
        if same_zone.len() > 1 {
            buckets.push(Bucket {
                basis: Basis::SameZone,
                members: same_zone,
                includes_self: true,
            });
        }
        for people in nearby_buckets(zone, &neighbors, &members) {
            buckets.push(Bucket {
                basis: Basis::Nearby,
                members: people,
                includes_self: false,
            });
        }
        let mut head = key_head("social-contact-bucket-v1", minute, &quoted[me]);
        select_contacts(&draws, &mut head, me, &buckets, maximum, true, &mut selected);

        // Contacts of at least the minimum duration, by source ID (people are indexed
        // in ID order, and a person is a source once).
        let mut contacts: Vec<(usize, Basis, i64)> = Vec::with_capacity(selected.len());
        for &(source, basis) in &selected {
            let key = pair_key(&quoted[me], &quoted[source])?;
            let duration = match previous.get(&key) {
                Some(&(seen, count)) if seen == minute - 1 => count + 1,
                _ => 1,
            };
            current.set_item(key, contact_entry(py, minute, duration)?)?;
            if duration >= minimum_contact_minutes {
                contacts.push((source, basis, duration));
            }
        }
        contacts.sort_unstable_by_key(|contact| contact.0);

        let delta_stress = base[me];
        let can_contribute = minute >= cooldown_before.get(id_texts[me]).copied().unwrap_or(0);
        let eligible: &[(usize, Basis, i64)] = if can_contribute { &contacts } else { &[] };
        let weights: Vec<f64> = eligible
            .iter()
            .map(|&(source, basis, _)| {
                let channel = match basis {
                    Basis::Family => family_strength,
                    Basis::SameZone => zone_strength,
                    Basis::Nearby => nearby_strength,
                };
                expressiveness[source] * channel * sensitivity[me]
            })
            .collect();
        let weight_total =
            py_fsum(&weights).ok_or_else(|| Unsupported::new_err("weights are not finite"))?;
        // The Python code raises for contacts without any weight (its strict zip).
        if !eligible.is_empty() && !(weight_total > 0.0) {
            return Err(Unsupported::new_err("eligible contacts have no weight"));
        }
        let mut shift = 0.0;
        let mut pair_shifts: Vec<f64> = Vec::new();
        let mut raw_pair_shifts: Vec<f64> = Vec::new();
        let mut aggregate_rate = 0.0;
        let mut pair_cap_scale = 1.0;
        if weight_total > 0.0 {
            let gamma = py_min(1.0, spread * weight_total);
            aggregate_rate = gamma;
            let own = stress[me];
            raw_pair_shifts = eligible
                .iter()
                .zip(&weights)
                .map(|(&(source, _, _), &weight)| {
                    gamma * weight / weight_total * (stress[source] - own)
                })
                .collect();
            pair_shifts = raw_pair_shifts
                .iter()
                .map(|&value| py_max(-pair_cap, py_min(pair_cap, value)))
                .collect();
            let raw_shift = py_fsum(&pair_shifts)
                .ok_or_else(|| Unsupported::new_err("shifts are not finite"))?;
            shift = py_max(-agent_cap, py_min(agent_cap, raw_shift));
            shift = py_max(-delta_stress, py_min(1.0 - delta_stress, shift));
            pair_cap_scale = if raw_shift != 0.0 {
                shift / raw_shift
            } else {
                0.0
            };
            for value in &mut pair_shifts {
                *value *= pair_cap_scale;
            }
        }
        output.push(py_min(1.0, py_max(0.0, delta_stress + shift)));
        total_shift += shift;
        if shift != 0.0 {
            contributing += 1;
            cooldowns.insert(id_texts[me], minute + cooldown_minutes);
        }
        let cooldown_left = 0.max(cooldowns.get(id_texts[me]).copied().unwrap_or(0) - minute);
        for (index, &(source, basis, duration)) in contacts.iter().enumerate() {
            let basis_name = match basis {
                Basis::Family => "family",
                Basis::SameZone => "same_zone",
                Basis::Nearby => "nearby_zone",
            };
            let difference = stress[source] - stress[me];
            let values = if index < pair_shifts.len() {
                [
                    difference,
                    pair_shifts[index],
                    weights[index],
                    aggregate_rate,
                    raw_pair_shifts[index],
                    py_max(-pair_cap, py_min(pair_cap, raw_pair_shifts[index])),
                    pair_cap_scale,
                ]
            } else {
                [difference, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]
            };
            traces.push((me, source, basis_name, duration, cooldown_left, values));
        }
    }
    let cooldown_dict = PyDict::new(py);
    for (id, due) in cooldowns {
        cooldown_dict.set_item(id, due)?;
    }
    Ok((
        output,
        current,
        cooldown_dict,
        traces,
        contributing,
        total_shift,
    ))
}

// lib.rs also registers this at the top of the module, where sheltersim/native.py calls it.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(social_channel_update, m)?)?;
    Ok(())
}
