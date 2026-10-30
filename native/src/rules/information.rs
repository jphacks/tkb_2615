//! The information model's contacts while no message exists (`InformationSpreadPolicy.update`),
//! under the same rules as `contacts.rs`.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

use crate::contacts::{
    contact_entry, exact_float, key_head, nearby_buckets, pair_key, previous_contacts, quoted_ids,
    select_contacts, texts, zone_members, Basis, Bucket,
};
use crate::core::mathx::py_max;
use crate::core::rng::KeyedDraws;
use crate::Unsupported;

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
        let mut head = key_head("information-contact-partner-v1", minute, &quoted[me]);
        select_contacts(&draws, &mut head, me, &buckets, maximum, false, &mut selected);
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

// lib.rs also registers this at the top of the module, where sheltersim/native.py calls it.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(information_contacts_without_messages, m)?)?;
    Ok(())
}
