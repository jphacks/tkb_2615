//! The rules of `domain/resources.py` that run in a tick: one inventory operation, the KPI columns,
//! and whether a reservation ID was used. Each reads the event and the row, checks what the Python
//! rule checks, and then writes the row and the tree in place. It calls no Python.
//!
//! Anything the Python rule would turn down, or that this rule isn't certain about, says
//! Unsupported before any change, and the Python rule runs and raises its text. Its whole write of
//! the namespace then reads the row again.
//!
//! The Python rule checks the state after an operation too. Here that follows from the checks
//! before it: the new record's IDs are checked below, its sources were checked with the account,
//! each operation keeps `remaining - reserved >= 0` and the reservation total, and the row's minute
//! order is kept by turning down an older minute (docs/rust-port/lanes/m4-resources.md).

use std::sync::Arc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

use crate::core::row::{attribute, exact_int, exact_str, namespace_is_absent, unsupported, Field};
use crate::core::tracked::as_readable_dict;
use crate::core::world_core::{raw_dict_get, NamespaceMirror};
use crate::rules::social_network::mapping_get;
use crate::state::entities::raw_setattr;
use crate::state::resource_ledger::{
    held_mirror, is_valid_id, resource_index, world_inputs, Account, AuditRecord, LedgerRow, Reservation,
    AUDIT_SCHEMA_VERSION, RESOURCE_IDS,
};

/// What an operation does to the reservations, once the balances are known.
enum ReservationChange {
    None,
    Insert(Reservation),
    Remove,
    Reduce(i64),
}

#[derive(Clone, Copy)]
struct Balances {
    delivered: i64,
    served: i64,
    losses: i64,
    remaining: i64,
    reserved: i64,
}

fn added(first: i64, second: i64) -> PyResult<i64> {
    first.checked_add(second).map_or_else(|| unsupported("a quantity past 64 bits"), Ok)
}

fn taken(first: i64, second: i64) -> PyResult<i64> {
    first.checked_sub(second).map_or_else(|| unsupported("a quantity past 64 bits"), Ok)
}

fn required_field<'py>(payload: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    mapping_get(payload, name)?.map_or_else(|| unsupported("the payload lacks a field"), Ok)
}

fn valid_id<'a>(value: &'a Bound<'_, PyAny>) -> PyResult<&'a str> {
    let id = exact_str(value)?;
    if is_valid_id(id) {
        Ok(id)
    } else {
        unsupported("not a stable ID")
    }
}

/// One operation's values, read from its event or given by a rule that hasn't made the event.
pub(crate) struct Operation<'a> {
    pub(crate) operation: &'a str,
    pub(crate) warehouse_id: &'a str,
    pub(crate) resource_id: &'a str,
    pub(crate) index: usize,
    pub(crate) quantity: i64,
    pub(crate) reservation_id: Option<&'a str>,
    pub(crate) agent_id: Option<&'a str>,
    pub(crate) source_id: &'a str,
    pub(crate) event_id: &'a str,
}

/// `apply_resource_operation`.
#[pyfunction]
pub(crate) fn apply_operation<'py>(
    world: &Bound<'py, PyAny>,
    event: &Bound<'py, PyAny>,
    operation: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let mirror = held_mirror(world)?;
    let operation_text = exact_str(operation)?;
    let (has_reservation, has_quantity) = match operation_text {
        "delivery" | "loss" => (false, true),
        "reserve" | "consume" => (true, true),
        "release" => (true, false),
        _ => return unsupported("an unknown operation"),
    };

    // set(payload) is exactly the required fields.
    let payload = attribute(event, intern!(py, "payload"))?;
    let field_count = 2 + usize::from(has_quantity) + if has_reservation { 2 } else { 0 };
    if payload.len().map_or(true, |count| count != field_count) {
        return unsupported("the payload has other fields");
    }
    let warehouse = required_field(&payload, intern!(py, "warehouse_id"))?;
    let warehouse_id = valid_id(&warehouse)?;
    let resource = required_field(&payload, intern!(py, "resource_id"))?;
    let resource_id = exact_str(&resource)?;
    let Some(index) = resource_index(resource_id) else {
        return unsupported("an unknown resource");
    };
    let quantity = if has_quantity {
        let quantity = exact_int(&required_field(&payload, intern!(py, "quantity"))?)?;
        if quantity <= 0 {
            return unsupported("a quantity that is not positive");
        }
        quantity
    } else {
        0
    };

    let (reservation_object, agent_object) = if has_reservation {
        (
            Some(required_field(&payload, intern!(py, "reservation_id"))?),
            Some(required_field(&payload, intern!(py, "agent_id"))?),
        )
    } else {
        (None, None)
    };
    let reservation_id = reservation_object.as_ref().map(valid_id).transpose()?;
    let agent_id = agent_object.as_ref().map(valid_id).transpose()?;

    let source_object = attribute(event, intern!(py, "source_id"))?;
    let source_id = valid_id(&source_object)?;
    let event_object = attribute(event, intern!(py, "event_id"))?;
    let event_id = valid_id(&event_object)?;
    apply_planned(
        world,
        &mirror,
        &Operation {
            operation: operation_text,
            warehouse_id,
            resource_id,
            index,
            quantity,
            reservation_id,
            agent_id,
            source_id,
            event_id,
        },
    )
}

/// The operation on the row and the tree; Unsupported, with nothing changed, for what the Python
/// rule turns down. Every ID is checked by the caller to be a stable ID.
pub(crate) fn apply_planned<'py>(
    world: &Bound<'py, PyAny>,
    mirror: &Bound<'py, NamespaceMirror>,
    planned: &Operation<'_>,
) -> PyResult<()> {
    let py = world.py();
    let &Operation {
        operation: operation_text,
        warehouse_id,
        resource_id,
        index,
        quantity,
        reservation_id,
        agent_id,
        source_id,
        event_id,
    } = planned;
    let (resources, agents, zones) = world_inputs(world)?;
    if let Some(agent) = agent_id {
        if raw_dict_get(&agents, PyString::new(py, agent).as_any())?.is_none() {
            return unsupported("an unknown person");
        }
    }
    let minute = exact_int(&attribute(world, intern!(py, "minute_index"))?)?;
    if minute < 0 {
        return unsupported("a minute before the Run");
    }

    let (record, change) = NamespaceMirror::with_row_tree::<LedgerRow, _>(mirror, |row, tree| {
        // Plan: nothing changes until the record is known.
        if &*row.warehouse_id != warehouse_id
            || minute < row.audit.last_minute
            || !row.passes_rules(&resources, &agents, &zones)?
        {
            return unsupported("the state or the warehouse is for the Python rule to judge");
        }
        let account = row.accounts.of(index);
        let mut next = Balances {
            delivered: account.delivered,
            served: account.served,
            losses: account.losses,
            remaining: account.remaining,
            reserved: account.reserved,
        };
        let mut change = ReservationChange::None;
        let mut record_quantity = quantity;
        match operation_text {
            "delivery" => {
                next.delivered = added(next.delivered, quantity)?;
                next.remaining = added(next.remaining, quantity)?;
            }
            "loss" => {
                if quantity > taken(next.remaining, next.reserved)? {
                    return unsupported("the loss exceeds the inventory");
                }
                next.losses = added(next.losses, quantity)?;
                next.remaining = taken(next.remaining, quantity)?;
            }
            "reserve" => {
                let id = reservation_id.unwrap_or_default();
                if row.used_reservation_ids.contains(id) {
                    return unsupported("the reservation ID was used");
                }
                if quantity > taken(next.remaining, next.reserved)? {
                    return unsupported("the reservation exceeds the inventory");
                }
                next.reserved = added(next.reserved, quantity)?;
                change = ReservationChange::Insert(Reservation {
                    reservation_id: Arc::from(id),
                    resource_id: Arc::from(resource_id),
                    agent_id: Arc::from(agent_id.unwrap_or_default()),
                    quantity,
                });
            }
            _ => {
                let Some(found) = account.reservations.get(reservation_id.unwrap_or_default()) else {
                    return unsupported("an unknown reservation");
                };
                if Some(&*found.agent_id) != agent_id {
                    return unsupported("the reservation is someone else's");
                }
                if operation_text == "release" {
                    record_quantity = found.quantity;
                    next.reserved = taken(next.reserved, found.quantity)?;
                    change = ReservationChange::Remove;
                } else {
                    if quantity > found.quantity {
                        return unsupported("the consume exceeds the reservation");
                    }
                    next.served = added(next.served, quantity)?;
                    next.remaining = taken(next.remaining, quantity)?;
                    next.reserved = taken(next.reserved, quantity)?;
                    change = if quantity == found.quantity {
                        ReservationChange::Remove
                    } else {
                        ReservationChange::Reduce(found.quantity - quantity)
                    };
                }
            }
        }
        let source_is_new = !account.source_ids.iter().any(|id| &**id == source_id);

        // Write the row and the tree. These cannot be turned down.
        let account_tree = tree.child(intern!(py, "accounts"))?.child_str(RESOURCE_IDS[index])?;
        let shared_source: Arc<str> = Arc::from(source_id);
        let reservations_tree = account_tree.child(intern!(py, "reservations"))?;
        let account: &mut Account = row.accounts.of_mut(index);
        account.set_delivered(&account_tree, next.delivered)?;
        account.set_served(&account_tree, next.served)?;
        account.set_losses(&account_tree, next.losses)?;
        account.set_remaining(&account_tree, next.remaining)?;
        account.set_reserved(&account_tree, next.reserved)?;
        if source_is_new {
            account_tree.child(intern!(py, "source_ids"))?.list_append(&shared_source)?;
            account.source_ids.push(shared_source.clone());
        }
        match &change {
            ReservationChange::Insert(new) => {
                account.reservations.insert(new.reservation_id.clone(), new.clone());
            }
            ReservationChange::Remove => {
                account.reservations.shift_remove(reservation_id.unwrap_or_default());
            }
            ReservationChange::Reduce(left) => {
                let id = reservation_id.unwrap_or_default();
                if let Some(found) = account.reservations.get_mut(id) {
                    found.set_quantity(&reservations_tree.child_str(id)?, *left)?;
                }
            }
            ReservationChange::None => {}
        }
        let record = AuditRecord {
            schema_version: AUDIT_SCHEMA_VERSION,
            minute,
            event_id: Some(Arc::from(event_id)),
            operation: Arc::from(operation_text),
            resource_id: Arc::from(resource_id),
            quantity: record_quantity,
            reservation_id: reservation_id.map(Arc::from),
            agent_id: agent_id.map(Arc::from),
            source_id: Some(shared_source),
            source_ids: account.source_ids.clone(),
            warehouse_id: row.warehouse_id.clone(),
            initial: account.initial,
            delivered: account.delivered,
            served: account.served,
            losses: account.losses,
            remaining: account.remaining,
            reserved: account.reserved,
        };
        if operation_text == "reserve" {
            let id: Arc<str> = Arc::from(reservation_id.unwrap_or_default());
            tree.child(intern!(py, "used_reservation_ids"))?.list_append(&id)?;
            row.used_reservation_ids.push(id);
        }
        row.audit.push(record.clone())?;
        Ok((record, change))
    })?;

    // What adds or leaves nodes of the tree runs outside the row's borrow.
    let path = ["accounts", RESOURCE_IDS[index], "reservations"];
    match &change {
        ReservationChange::Insert(new) => {
            NamespaceMirror::write_through(mirror, &path, &new.reservation_id, &Field::to_py(new, py)?)?;
        }
        ReservationChange::Remove => {
            NamespaceMirror::remove_through(mirror, &path, reservation_id.unwrap_or_default())?;
        }
        ReservationChange::Reduce(_) | ReservationChange::None => {}
    }
    NamespaceMirror::append_through(mirror, &["audit"], &Field::to_py(&record, py)?)?;

    // A new dict, like the Python rule: whoever holds the old one keeps the old values.
    let next_resources = as_readable_dict(&resources).map_or_else(
        || unsupported("the resources are not a dict"),
        |resources| Ok(resources.copy()?),
    )?;
    next_resources.set_item(RESOURCE_IDS[index], record.remaining)?;
    raw_setattr(world, intern!(py, "resources"), next_resources.as_ptr())
}

const COLUMNS: [[&str; 6]; 2] = [
    [
        "water_ml_initial",
        "water_ml_delivered",
        "water_ml_served",
        "water_ml_losses",
        "water_ml_remaining",
        "water_ml_reserved",
    ],
    [
        "food_meal_units_initial",
        "food_meal_units_delivered",
        "food_meal_units_served",
        "food_meal_units_losses",
        "food_meal_units_remaining",
        "food_meal_units_reserved",
    ],
];

/// `resource_metric_values`.
#[pyfunction]
#[pyo3(signature = (world, *, tick_memo=None))]
pub(crate) fn metric_values<'py>(
    world: &Bound<'py, PyAny>,
    tick_memo: Option<&Bound<'py, PyAny>>,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    let _ = tick_memo;
    let py = world.py();
    if namespace_is_absent(world, intern!(py, "resource_ledger_v1"))? {
        return Ok(None);
    }
    let mirror = held_mirror(world)?;
    let (resources, agents, zones) = world_inputs(world)?;
    let balances = NamespaceMirror::with_row::<LedgerRow, _>(&mirror, |row| {
        if !row.passes_rules(&resources, &agents, &zones)? {
            return unsupported("the state is for the Python rule to judge");
        }
        let of = |account: &Account| {
            [account.initial, account.delivered, account.served, account.losses, account.remaining, account.reserved]
        };
        Ok([of(row.accounts.of(0)), of(row.accounts.of(1))])
    })?;
    let result = PyDict::new(py);
    for (names, values) in COLUMNS.iter().zip(&balances) {
        for (name, value) in names.iter().zip(values) {
            result.set_item(*name, *value)?;
        }
    }
    Ok(Some(result))
}

/// `reservation_id in ledger["used_reservation_ids"]`, without reading the list.
#[pyfunction]
fn reservation_id_was_used(world: &Bound<'_, PyAny>, reservation_id: &Bound<'_, PyAny>) -> PyResult<bool> {
    let mirror = held_mirror(world)?;
    let id = exact_str(reservation_id)?;
    NamespaceMirror::with_row::<LedgerRow, _>(&mirror, |row| Ok(row.used_reservation_ids.contains(id)))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(apply_operation, m)?)?;
    m.add_function(wrap_pyfunction!(metric_values, m)?)?;
    m.add_function(wrap_pyfunction!(reservation_id_was_used, m)?)?;
    Ok(())
}
