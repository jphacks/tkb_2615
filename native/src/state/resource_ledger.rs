//! `resource_ledger_v1`: the two accounts, the reservations and the audit trail
//! (`domain/resources.py`). `rules/resources.rs` writes it in place: a Python rule that replaced the
//! whole namespace at each operation would forward all of it (docs/rust-port/lanes/m4-resources.md).
//!
//! The audit trail is the canonical JSON of each record. Rules read only the newest record of each
//! resource, which the row keeps typed. A record joins the row only if it passes every rule of
//! `_validate_audit_record` that reads the record alone; any other record, or a write into a record
//! already there, taints the namespace, and the Python rules and their errors run on the tree.

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyInt, PyList, PyString};
use rustc_hash::{FxBuildHasher, FxHashSet};

use crate::core::row::{attribute, load_row, namespace_is_absent, state_row, Field, IdMap};
use crate::core::tracked::{as_readable_dict, as_readable_list};
use crate::core::world_core::{mirror_of, raw_dict_get, Misfit, NamespaceMirror, Part, RowType, Write};
use crate::{Encoder, Unsupported};

pub(crate) const NAME: &str = "resource_ledger_v1";
pub(crate) const RESOURCE_IDS: [&str; 2] = ["water_ml", "food_meal_units"];
const SCHEMA_VERSION: i64 = 1;
pub(crate) const AUDIT_SCHEMA_VERSION: i64 = 1;

state_row! {
    #[derive(Clone)]
    pub(crate) struct Reservation {
        reservation_id: Arc<str>,
        resource_id: Arc<str>,
        agent_id: Arc<str>,
        quantity: i64 => set_quantity,
    }
}

state_row! {
    pub(crate) struct Account {
        initial: i64,
        delivered: i64 => set_delivered,
        served: i64 => set_served,
        losses: i64 => set_losses,
        remaining: i64 => set_remaining,
        reserved: i64 => set_reserved,
        source_ids: Vec<Arc<str>>,
        reservations: IdMap<Reservation>,
    }
}

state_row! {
    pub(crate) struct Accounts {
        water_ml: Account,
        food_meal_units: Account,
    }
}

impl Accounts {
    pub(crate) fn of(&self, index: usize) -> &Account {
        if index == 0 {
            &self.water_ml
        } else {
            &self.food_meal_units
        }
    }

    pub(crate) fn of_mut(&mut self, index: usize) -> &mut Account {
        if index == 0 {
            &mut self.water_ml
        } else {
            &mut self.food_meal_units
        }
    }
}

state_row! {
    #[derive(Clone)]
    pub(crate) struct AuditRecord {
        schema_version: i64,
        minute: i64,
        event_id: Option<Arc<str>>,
        operation: Arc<str>,
        resource_id: Arc<str>,
        quantity: i64,
        reservation_id: Option<Arc<str>>,
        agent_id: Option<Arc<str>>,
        source_id: Option<Arc<str>>,
        source_ids: Vec<Arc<str>>,
        warehouse_id: Arc<str>,
        initial: i64,
        delivered: i64,
        served: i64,
        losses: i64,
        remaining: i64,
        reserved: i64,
    }
}

pub(crate) use crate::state::events::is_stable_id as is_valid_id;

fn no_repeats(ids: &[Arc<str>]) -> bool {
    // A handful of sources: a set would cost more.
    ids.iter().enumerate().all(|(index, id)| !ids[..index].contains(id))
}

pub(crate) fn resource_index(id: &str) -> Option<usize> {
    RESOURCE_IDS.iter().position(|known| *known == id)
}

impl AuditRecord {
    /// The rules of `_validate_audit_record` that read the record alone.
    fn is_valid(&self) -> bool {
        let id_is_valid = |id: &Option<Arc<str>>| id.as_ref().is_none_or(|id| is_valid_id(id));
        self.schema_version == AUDIT_SCHEMA_VERSION
            && self.minute >= 0
            && resource_index(&self.resource_id).is_some()
            && matches!(&*self.operation, "initial" | "delivery" | "loss" | "reserve" | "release" | "consume")
            && self.quantity >= 0
            && id_is_valid(&self.event_id)
            && id_is_valid(&self.reservation_id)
            && id_is_valid(&self.agent_id)
            && id_is_valid(&self.source_id)
            && self.source_ids.iter().all(|id| is_valid_id(id))
            && no_repeats(&self.source_ids)
            && self.initial >= 0
            && self.delivered >= 0
            && self.served >= 0
            && self.losses >= 0
            && self.remaining >= 0
            && self.reserved >= 0
    }
}

/// The audit trail: the bytes of each record, and what the checks of a tick read.
pub(crate) struct Audit {
    items: Vec<Box<[u8]>>,
    newest: [Option<AuditRecord>; 2],
    pub(crate) last_minute: i64,
    /// Every minute is at least the one before. The Python check of a tick reads only the newest
    /// records, so a false here says nothing, and the Python rule then decides.
    ordered: bool,
    /// The warehouse of every record, or None without records. A second warehouse is a misfit.
    warehouse: Option<Arc<str>>,
}

impl Audit {
    /// Add a record a rule built, or one read from the tree.
    pub(crate) fn push(&mut self, record: AuditRecord) -> PyResult<()> {
        let mut encoder = Encoder { out: Vec::with_capacity(384) };
        Field::write_json(&record, &mut encoder)?;
        self.items.push(encoder.out.into_boxed_slice());
        if record.minute < self.last_minute {
            self.ordered = false;
        }
        self.last_minute = record.minute;
        if self.warehouse.is_none() {
            self.warehouse = Some(record.warehouse_id.clone());
        }
        if let Some(index) = resource_index(&record.resource_id) {
            self.newest[index] = Some(record);
        }
        Ok(())
    }

    fn take(&mut self, value: &Bound<'_, PyAny>) -> Result<(), Misfit> {
        let record = AuditRecord::from_py(value)?;
        if !record.is_valid() {
            return Err(Misfit::new("an audit record the rules turn down"));
        }
        if self.warehouse.as_ref().is_some_and(|warehouse| *warehouse != record.warehouse_id) {
            return Err(Misfit::new("audit records of two warehouses"));
        }
        self.push(record)?;
        Ok(())
    }
}

impl Field for Audit {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        let list = as_readable_list(value).ok_or_else(|| Misfit::new("not a list"))?;
        let mut audit = Self {
            items: Vec::with_capacity(list.len()),
            newest: [None, None],
            last_minute: 0,
            ordered: true,
            warehouse: None,
        };
        for item in list.iter() {
            audit.take(&item)?;
        }
        Ok(audit)
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let loads = py.import(intern!(py, "json"))?.getattr(intern!(py, "loads"))?;
        let list = PyList::empty(py);
        for item in &self.items {
            list.append(loads.call1((std::str::from_utf8(item)?,))?)?;
        }
        Ok(list.into_any())
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        encoder.out.push(b'[');
        for (index, item) in self.items.iter().enumerate() {
            if index > 0 {
                encoder.out.push(b',');
            }
            encoder.out.extend_from_slice(item);
        }
        encoder.out.push(b']');
        Ok(())
    }

    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        match (path, write) {
            ([], Write::Append { value }) => self.take(value),
            ([], Write::Extend { values }) => values.iter().try_for_each(|value| self.take(&value)),
            _ => Err(Misfit::new("the write does not fit the audit trail")),
        }
    }
}

/// `used_reservation_ids`, with a set: a rule asks whether an ID was used, four times per service.
pub(crate) struct UsedIds {
    ids: Vec<Arc<str>>,
    set: FxHashSet<Arc<str>>,
}

impl UsedIds {
    pub(crate) fn contains(&self, id: &str) -> bool {
        self.set.contains(id)
    }

    pub(crate) fn push(&mut self, id: Arc<str>) {
        self.set.insert(id.clone());
        self.ids.push(id);
    }

    fn are_unique(&self) -> bool {
        self.set.len() == self.ids.len()
    }
}

impl Field for UsedIds {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        let ids: Vec<Arc<str>> = Field::from_py(value)?;
        let mut set = FxHashSet::with_capacity_and_hasher(ids.len(), FxBuildHasher);
        set.extend(ids.iter().cloned());
        Ok(Self { ids, set })
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Field::to_py(&self.ids, py)
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        Field::write_json(&self.ids, encoder)
    }

    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        Field::apply(&mut self.ids, path, write)?;
        self.set.clear();
        self.set.extend(self.ids.iter().cloned());
        Ok(())
    }
}

state_row! {
    pub(crate) struct LedgerRow {
        schema_version: i64,
        warehouse_id: Arc<str>,
        zone_id: Option<Arc<str>>,
        accounts: Accounts,
        used_reservation_ids: UsedIds,
        audit: Audit,
    }
}

fn balances_match(record: &AuditRecord, account: &Account) -> bool {
    record.initial == account.initial
        && record.delivered == account.delivered
        && record.served == account.served
        && record.losses == account.losses
        && record.remaining == account.remaining
        && record.reserved == account.reserved
        && record.source_ids == account.source_ids
}

fn int_is<'py>(value: Option<Bound<'py, PyAny>>, expected: i64) -> PyResult<bool> {
    let Some(value) = value else {
        return Ok(false);
    };
    let Ok(number) = value.downcast_exact::<PyInt>() else {
        return Ok(false);
    };
    Ok(number.extract::<i64>().is_ok_and(|number| number == expected))
}

impl LedgerRow {
    /// Every rule `validate_resource_ledger_state` has, given a row of the right types, for a world
    /// holding these resources, people and zones. True says the Python rule accepts; false decides
    /// nothing. Reads dicts with exact str keys only, so it runs no Python code.
    pub(crate) fn passes_rules<'py>(
        &self,
        resources: &Bound<'py, PyAny>,
        agents: &Bound<'py, PyAny>,
        zones: &Bound<'py, PyAny>,
    ) -> PyResult<bool> {
        let py = resources.py();
        if self.schema_version != SCHEMA_VERSION
            || !is_valid_id(&self.warehouse_id)
            || !self.used_reservation_ids.are_unique()
            || self.audit.items.len() < RESOURCE_IDS.len()
            || !self.audit.ordered
            || self.audit.warehouse.as_ref() != Some(&self.warehouse_id)
        {
            return Ok(false);
        }
        if let Some(zone) = &self.zone_id {
            if !is_valid_id(zone) || raw_dict_get(zones, PyString::new(py, zone).as_any())?.is_none() {
                return Ok(false);
            }
        }
        let Some(resources) = as_readable_dict(resources) else {
            return Ok(false);
        };
        if resources.len() != RESOURCE_IDS.len() {
            return Ok(false);
        }
        for (index, resource_id) in RESOURCE_IDS.iter().enumerate() {
            let account = self.accounts.of(index);
            if account.initial < 0
                || account.delivered < 0
                || account.served < 0
                || account.losses < 0
                || account.remaining < 0
                || account.reserved < 0
            {
                return Ok(false);
            }
            let expected = i128::from(account.initial) + i128::from(account.delivered)
                - i128::from(account.served)
                - i128::from(account.losses);
            if expected != i128::from(account.remaining)
                || account.reserved > account.remaining
                || !int_is(
                    raw_dict_get(resources.as_any(), PyString::new(py, resource_id).as_any())?,
                    account.remaining,
                )?
            {
                return Ok(false);
            }
            let mut total = 0_i128;
            for (id, record) in &account.reservations {
                if !is_valid_id(id)
                    || !self.used_reservation_ids.contains(id)
                    || record.reservation_id != *id
                    || &*record.resource_id != *resource_id
                    || !is_valid_id(&record.agent_id)
                    || raw_dict_get(agents, PyString::new(py, &record.agent_id).as_any())?.is_none()
                    || record.quantity <= 0
                {
                    return Ok(false);
                }
                total += i128::from(record.quantity);
            }
            if total != i128::from(account.reserved)
                || !account.source_ids.iter().all(|id| is_valid_id(id))
                || !no_repeats(&account.source_ids)
                || !self.audit.newest[index].as_ref().is_some_and(|newest| balances_match(newest, account))
            {
                return Ok(false);
            }
        }
        Ok(true)
    }
}

/// The mirror of the ledger the world really holds: a row left behind by a write past the table
/// answers for nothing, so the kernels say Unsupported and the Python rules read what is there.
pub(crate) fn held_mirror<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, NamespaceMirror>> {
    let py = world.py();
    let mirror = mirror_of(world, NAME)?;
    let table = attribute(world, intern!(py, "model_state"))?;
    if as_readable_dict(&table).is_none() {
        return Err(Unsupported::new_err("model_state is another kind of mapping"));
    }
    let held = raw_dict_get(&table, intern!(py, "resource_ledger_v1").as_any())?;
    if !held.is_some_and(|held| mirror.try_borrow().is_ok_and(|mirror| mirror.root_is(&held))) {
        return Err(Unsupported::new_err("the world holds another ledger"));
    }
    Ok(mirror)
}

/// The dicts a check reads, in the order `passes_rules` takes them.
pub(crate) fn world_inputs<'py>(
    world: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let py = world.py();
    let agents = attribute(world, intern!(py, "agents"))?;
    let zones = attribute(world, intern!(py, "zones"))?;
    if as_readable_dict(&agents).is_none() || as_readable_dict(&zones).is_none() {
        return Err(Unsupported::new_err("the world's maps are another kind of mapping"));
    }
    Ok((attribute(world, intern!(py, "resources"))?, agents, zones))
}

/// `validate_resource_ledger_state`: accepts, or leaves it to the Python rule and its error text.
#[pyfunction]
fn validate(world: &Bound<'_, PyAny>) -> PyResult<bool> {
    if namespace_is_absent(world, intern!(world.py(), NAME))? {
        return Ok(false);
    }
    let mirror = held_mirror(world)?;
    let (resources, agents, zones) = world_inputs(world)?;
    NamespaceMirror::with_row::<LedgerRow, _>(&mirror, |row| row.passes_rules(&resources, &agents, &zones))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("ROW", Py::new(py, RowType::new("resource_ledger", load_row::<LedgerRow>))?)?;
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    Ok(())
}
