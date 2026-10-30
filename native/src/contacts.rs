//! The per-tick contact models: sampling keyed partners and, for the social model,
//! the bounded stress synchrony (`ExpressivenessChannelOpennessPolicy.update`,
//! `InformationSpreadPolicy.update` while no message exists). Each returns the same
//! floats, draws and JSON keys as the Python code: every float operation keeps its
//! order, and anything the Python code would reject, or any value of another type,
//! raises `Unsupported` so the caller runs that code instead.

use std::collections::{BTreeMap, HashMap};

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PyString};
use sha2::{Digest, Sha256};

use crate::psychology::{py_max, py_min};
use crate::{write_json_string, Unsupported};

/// `CounterRNG.random`: SHA-256 of the seed and the JSON key, the first eight bytes
/// as a fraction of 2**64.
pub(crate) struct KeyedDraws {
    seeded: Sha256,
}

impl KeyedDraws {
    pub(crate) fn new(seed: &str) -> Self {
        let mut seeded = Sha256::new();
        seeded.update(b"sheltersim:sha256-keyed-v1\0");
        seeded.update(seed.as_bytes());
        seeded.update(b"\0");
        Self { seeded }
    }

    pub(crate) fn draw(&self, key: &[u8]) -> f64 {
        let digest = self.seeded.clone().chain_update(key).finalize();
        let mut first = [0u8; 8];
        first.copy_from_slice(&digest[..8]);
        // u64 to f64 and int / int both round to nearest, ties to even, and the
        // division by a power of two is exact.
        u64::from_be_bytes(first) as f64 / 18_446_744_073_709_551_616.0
    }
}

/// math.fsum as CPython 3.12 computes it (Shewchuk's partials with its half-even
/// correction of the last bit), for finite values whose partial sums stay finite.
pub(crate) fn py_fsum(values: &[f64]) -> Option<f64> {
    let mut partials: Vec<f64> = Vec::with_capacity(8);
    for &value in values {
        if !value.is_finite() {
            return None;
        }
        let mut x = value;
        let mut kept = 0;
        for index in 0..partials.len() {
            let mut y = partials[index];
            if x.abs() < y.abs() {
                std::mem::swap(&mut x, &mut y);
            }
            let hi = x + y;
            let yr = hi - x;
            let lo = y - yr;
            if lo != 0.0 {
                partials[kept] = lo;
                kept += 1;
            }
            x = hi;
        }
        partials.truncate(kept);
        if x != 0.0 {
            if !x.is_finite() {
                return None;
            }
            partials.push(x);
        }
    }
    let mut n = partials.len();
    let mut hi = 0.0;
    if n > 0 {
        n -= 1;
        hi = partials[n];
        let mut lo = 0.0;
        while n > 0 {
            let x = hi;
            n -= 1;
            let y = partials[n];
            hi = x + y;
            let yr = hi - x;
            lo = y - yr;
            if lo != 0.0 {
                break;
            }
        }
        if n > 0 && ((lo < 0.0 && partials[n - 1] < 0.0) || (lo > 0.0 && partials[n - 1] > 0.0)) {
            let y = lo * 2.0;
            let x = hi + y;
            let yr = x - hi;
            if y == yr {
                hi = x;
            }
        }
    }
    Some(hi)
}

/// math.fsum of finite values, for tests.
#[pyfunction]
pub fn fsum(values: Vec<f64>) -> PyResult<f64> {
    py_fsum(&values).ok_or_else(|| Unsupported::new_err("values are not finite"))
}

fn exact_float(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    value
        .downcast_exact::<PyFloat>()
        .map(|number| number.value())
        .map_err(|_| Unsupported::new_err("not a float"))
}

fn exact_int(value: &Bound<'_, PyAny>) -> PyResult<i64> {
    value
        .downcast_exact::<PyInt>()
        .map_err(|_| Unsupported::new_err("not an int"))?
        .extract()
        .map_err(|_| Unsupported::new_err("int out of range"))
}

fn exact_dict<'a, 'py>(value: &'a Bound<'py, PyAny>) -> PyResult<&'a Bound<'py, PyDict>> {
    value
        .downcast_exact::<PyDict>()
        .map_err(|_| Unsupported::new_err("not a dict"))
}

fn texts<'a>(values: &'a [Bound<'_, PyString>]) -> PyResult<Vec<&'a str>> {
    values
        .iter()
        .map(|value| {
            value
                .to_str()
                .map_err(|_| Unsupported::new_err("not UTF-8"))
        })
        .collect()
}

/// The {pair key: {last_seen_minute, consecutive_minutes}} of the last tick, as
/// non-negative ints.
fn previous_contacts(contacts: &Bound<'_, PyDict>) -> PyResult<HashMap<String, (i64, i64)>> {
    let py = contacts.py();
    let last_seen = intern!(py, "last_seen_minute");
    let consecutive = intern!(py, "consecutive_minutes");
    let mut previous = HashMap::with_capacity(contacts.len());
    for (key, value) in contacts.iter() {
        let key = key
            .downcast_exact::<PyString>()
            .map_err(|_| Unsupported::new_err("contact key is not a str"))?
            .to_str()
            .map_err(|_| Unsupported::new_err("not UTF-8"))?
            .to_owned();
        let value = exact_dict(&value)?;
        let minute = value.get_item(last_seen)?;
        let minutes = value.get_item(consecutive)?;
        let (Some(minute), Some(minutes)) = (minute, minutes) else {
            return Err(Unsupported::new_err("contact field is missing"));
        };
        let numbers = (exact_int(&minute)?, exact_int(&minutes)?);
        if numbers.0 < 0 || numbers.1 < 0 {
            return Err(Unsupported::new_err("contact field is negative"));
        }
        previous.insert(key, numbers);
    }
    Ok(previous)
}

fn contact_entry<'py>(py: Python<'py>, minute: i64, minutes: i64) -> PyResult<Bound<'py, PyDict>> {
    let entry = PyDict::new(py);
    entry.set_item(intern!(py, "last_seen_minute"), minute)?;
    entry.set_item(intern!(py, "consecutive_minutes"), minutes)?;
    Ok(entry)
}

#[derive(Clone, Copy)]
enum Basis {
    Family,
    SameZone,
    Nearby,
}

struct Bucket<'a> {
    basis: Basis,
    members: &'a [usize],
    includes_self: bool,
}

impl Bucket<'_> {
    fn eligible(&self) -> i64 {
        self.members.len() as i64 - i64::from(self.includes_self)
    }
}

/// Sorted person indexes of each zone (people are indexed in ID order).
fn zone_members<'a>(zones: &[&'a str]) -> HashMap<&'a str, Vec<usize>> {
    let mut members: HashMap<&str, Vec<usize>> = HashMap::new();
    for (index, &zone) in zones.iter().enumerate() {
        members.entry(zone).or_default().push(index);
    }
    members
}

/// The adjacent zones of `zone` that have people, in sorted order.
fn nearby_buckets<'a>(
    zone: &str,
    neighbors: &'a HashMap<String, Vec<String>>,
    members: &'a HashMap<&str, Vec<usize>>,
) -> Vec<&'a [usize]> {
    let mut names: Vec<&str> = neighbors
        .get(zone)
        .map(|names| names.iter().map(String::as_str).collect())
        .unwrap_or_default();
    names.sort_unstable();
    names
        .into_iter()
        .filter_map(|name| members.get(name))
        .filter(|people| !people.is_empty())
        .map(Vec::as_slice)
        .collect()
}

/// `_select_contacts`: at most `maximum` partners of `me`, with the draws of
/// `{key}` written as `["<label>",minute,"<me>",slot,attempt]`. The social model
/// stops at a slot that used all its attempts; the information model goes on.
fn select_contacts(
    draws: &KeyedDraws,
    key_head: &[u8],
    me: usize,
    buckets: &[Bucket<'_>],
    maximum: i64,
    stop_at_exhausted_slot: bool,
    selected: &mut Vec<(usize, Basis)>,
) {
    selected.clear();
    if maximum == 0 || buckets.is_empty() {
        return;
    }
    let total: i64 = buckets.iter().map(Bucket::eligible).sum();
    if total <= 0 {
        return;
    }
    let mut key = Vec::with_capacity(key_head.len() + 24);
    for slot in 0..maximum {
        let mut exhausted = true;
        for attempt in 0..maximum * 4 {
            key.clear();
            key.extend_from_slice(key_head);
            key.extend_from_slice(format!(",{slot},{attempt}]").as_bytes());
            let draw = draws.draw(&key);
            let mut offset = (total - 1).min((draw * total as f64) as i64);
            let mut chosen = None;
            for bucket in buckets {
                if offset < bucket.eligible() {
                    chosen = Some(bucket);
                    break;
                }
                offset -= bucket.eligible();
            }
            let Some(bucket) = chosen else {
                exhausted = false;
                break;
            };
            let mut local = offset as usize;
            if bucket.includes_self && local >= bucket.members.partition_point(|&other| other < me)
            {
                local += 1;
            }
            let source = bucket.members[local];
            if source == me || selected.iter().any(|&(other, _)| other == source) {
                continue;
            }
            selected.push((source, bucket.basis));
            exhausted = false;
            break;
        }
        if exhausted && stop_at_exhausted_slot {
            break;
        }
    }
}

/// `"<id>"` as JSON for each person.
fn quoted_ids(ids: &[&str]) -> Vec<Vec<u8>> {
    ids.iter()
        .map(|id| {
            let mut quoted = Vec::with_capacity(id.len() + 2);
            write_json_string(&mut quoted, id);
            quoted
        })
        .collect()
}

fn pair_key(first: &[u8], second: &[u8]) -> PyResult<String> {
    let mut key = String::with_capacity(first.len() + second.len() + 3);
    key.push('[');
    key.push_str(std::str::from_utf8(first).map_err(|_| Unsupported::new_err("not UTF-8"))?);
    key.push(',');
    key.push_str(std::str::from_utf8(second).map_err(|_| Unsupported::new_err("not UTF-8"))?);
    key.push(']');
    Ok(key)
}

fn key_head(label: &str, minute: i64, quoted: &[u8]) -> Vec<u8> {
    let mut head = Vec::with_capacity(label.len() + quoted.len() + 32);
    head.extend_from_slice(b"[\"");
    head.extend_from_slice(label.as_bytes());
    head.extend_from_slice(b"\",");
    head.extend_from_slice(minute.to_string().as_bytes());
    head.push(b',');
    head.extend_from_slice(quoted);
    head
}

/// One tick of `InformationSpreadPolicy.update` while no person holds a belief: the
/// decayed query pressure (a copy of `query_pressure`), the new contact records and
/// the number of contacts. `ids` are sorted; `zones` are their zones.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn information_contacts_without_messages<'py>(
    py: Python<'py>,
    seed: &str,
    minute: i64,
    maximum: i64,
    ids: Vec<Bound<'py, PyString>>,
    zones: Vec<Bound<'py, PyString>>,
    neighbors: HashMap<String, Vec<String>>,
    previous: &Bound<'py, PyDict>,
    query_pressure: &Bound<'py, PyDict>,
    decay: f64,
) -> PyResult<(Bound<'py, PyDict>, Bound<'py, PyDict>, i64)> {
    let id_texts = texts(&ids)?;
    let zone_texts = texts(&zones)?;
    if id_texts.len() != zone_texts.len() {
        return Err(Unsupported::new_err("people and zones differ in length"));
    }
    let pressure = query_pressure.copy()?;
    for id in &ids {
        let Some(value) = pressure.get_item(id)? else {
            return Err(Unsupported::new_err("a person has no query pressure"));
        };
        pressure.set_item(id, py_max(0.0, exact_float(&value)? - decay))?;
    }

    let previous = previous_contacts(previous)?;
    let draws = KeyedDraws::new(seed);
    let quoted = quoted_ids(&id_texts);
    let members = zone_members(&zone_texts);
    let current = PyDict::new(py);
    let mut attempts = 0;
    let mut selected = Vec::new();
    for (me, &zone) in zone_texts.iter().enumerate() {
        let mut buckets = Vec::new();
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
        let head = key_head("information-contact-partner-v1", minute, &quoted[me]);
        select_contacts(&draws, &head, me, &buckets, maximum, false, &mut selected);
        for &(source, _) in &selected {
            let key = pair_key(&quoted[me], &quoted[source])?;
            let minutes = match previous.get(&key) {
                Some(&(seen, count)) if seen == minute - 1 => count + 1,
                _ => 1,
            };
            current.set_item(key, contact_entry(py, minute, minutes)?)?;
            attempts += 1;
        }
    }
    Ok((pressure, current, attempts))
}

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
    for (key, value) in previous_cooldowns.iter() {
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
        let head = key_head("social-contact-bucket-v1", minute, &quoted[me]);
        select_contacts(&draws, &head, me, &buckets, maximum, true, &mut selected);

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
