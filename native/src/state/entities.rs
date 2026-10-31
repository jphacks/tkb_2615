//! The entities of a world as Rust rows: agents with their needs, facilities, staff, support
//! requests and active tasks (docs/rust-port/lanes/p6-entity-windows.md).
//!
//! The Python objects stay the dataclasses of `domain/models.py`: a read is a plain attribute
//! read of the instance, and each `__dict__` is the mirror of a row, as a tracked tree is the
//! mirror of a namespace's row. An object in a world's `IdMap` is attached: its slots `_store` and
//! `_slot` say where its row is (they stay out of `__dict__`), and its `__setattr__` comes here. `IdMap` reports what is inserted and removed, and
//! `TrackedList` what happens to the ID lists of a facility or a staff member. Rust rules read
//! the rows through `with_rows`, and `write_field` writes a row and then its object.
//!
//! The store holds no reference to an entity or a world, so it makes no cycle and the rows stay
//! `Send + Sync` (`assert_rows_are_shareable`). Rust finds an object through the world's `IdMap`.
//!
//! A value that does not fit a typed field taints the whole store. The Python object keeps what
//! was written, so the Python rules see it and fail with their own text; `with_rows` then says
//! `Unsupported` until `refit` reads every object again. A typed field takes only its exact
//! Python type, and an ID list only exact `str` items.
//!
//! No `PyRefMut` of the store is held across anything that may run Python code: a property
//! setter (`acute_distress`) writes back through here while `set_field` is storing.

use std::ptr::null_mut;
use std::sync::atomic::{AtomicPtr, Ordering};
use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::ffi;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple, PyType};
use pyo3::IntoPyObjectExt;
use rustc_hash::{FxBuildHasher, FxHashSet};

use crate::core::tracked::{as_readable_list, id_map_type};
use crate::core::value::V;
use crate::core::world_core::{
    core_of, new_tracked_list, raw_dict_get, raw_dict_set, raw_list_append, Misfit,
};
use crate::{write_json_string, Encoder, Unsupported};

// ---- What Python hands over ---------------------------------------------------------------

const AGENT: u8 = 0;
const NEEDS: u8 = 1;
const FACILITY: u8 = 2;
const STAFF: u8 = 3;
const REQUEST: u8 = 4;
const TASK: u8 = 5;

struct Installed {
    agent: Py<PyType>,
    needs: Py<PyType>,
    facility: Py<PyType>,
    staff: Py<PyType>,
    request: Py<PyType>,
    task: Py<PyType>,
    activity: Vec<Py<PyAny>>,
    mobility: Vec<Py<PyAny>>,
    status: Vec<Py<PyAny>>,
    // The `.value` of each member, which is what a document holds.
    activity_values: Vec<String>,
    mobility_values: Vec<String>,
    status_values: Vec<String>,
}

static INSTALLED: AtomicPtr<Installed> = AtomicPtr::new(null_mut());

fn installed() -> Result<&'static Installed, Misfit> {
    // SAFETY: the pointer comes from Box::leak in install and is never freed.
    unsafe { INSTALLED.load(Ordering::Acquire).as_ref() }
        .ok_or_else(|| Misfit::new("domain/models.py is not imported"))
}

/// Remember the classes and the members of the enums, in the order of `ACTIVITIES` and the
/// others below. They stay alive for the life of the process.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn install(
    py: Python<'_>,
    agent: Py<PyType>,
    needs: Py<PyType>,
    facility: Py<PyType>,
    staff: Py<PyType>,
    request: Py<PyType>,
    task: Py<PyType>,
    activity: Vec<Py<PyAny>>,
    mobility: Vec<Py<PyAny>>,
    status: Vec<Py<PyAny>>,
) -> PyResult<()> {
    if activity.len() != ACTIVITIES.len()
        || mobility.len() != MOBILITIES.len()
        || status.len() != STATUSES.len()
    {
        return Err(PyValueError::new_err("the enums of domain/models.py do not match the rows"));
    }
    let values = |members: &[Py<PyAny>]| -> PyResult<Vec<String>> {
        members.iter().map(|member| member.bind(py).getattr("value")?.extract()).collect()
    };
    let (activity_values, mobility_values, status_values) =
        (values(&activity)?, values(&mobility)?, values(&status)?);
    let new = Box::leak(Box::new(Installed {
        agent,
        needs,
        facility,
        staff,
        request,
        task,
        activity,
        mobility,
        status,
        activity_values,
        mobility_values,
        status_values,
    }));
    INSTALLED.store(new, Ordering::Release);
    Ok(())
}

// ---- Typed fields ----------------------------------------------------------------------------

/// A field of a row. `from_py` runs no Python code and takes only the exact type.
trait Field: Sized {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit>;
    fn to_py(&self, py: Python<'_>) -> PyResult<Py<PyAny>>;
    fn same(&self, other: &Self) -> bool;
    /// The bytes `canonical_json` writes for this value in a WorldState document of this version.
    fn write_json(&self, encoder: &mut Encoder, version: i64) -> PyResult<()>;
}

fn write_strings(encoder: &mut Encoder, items: &[Arc<str>]) {
    encoder.out.push(b'[');
    for (index, item) in items.iter().enumerate() {
        if index > 0 {
            encoder.out.push(b',');
        }
        write_json_string(&mut encoder.out, item);
    }
    encoder.out.push(b']');
}

impl Field for f64 {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        Ok(value.downcast_exact::<PyFloat>().map_err(|_| Misfit::new("not a float"))?.value())
    }
    fn to_py(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.into_py_any(py)
    }
    fn same(&self, other: &Self) -> bool {
        self.to_bits() == other.to_bits()
    }
    fn write_json(&self, encoder: &mut Encoder, _version: i64) -> PyResult<()> {
        encoder.float(*self)
    }
}

impl Field for i64 {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        // A bool's exact type is not int.
        Ok(value.downcast_exact::<PyInt>().map_err(|_| Misfit::new("not an int"))?.extract()?)
    }
    fn to_py(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.into_py_any(py)
    }
    fn same(&self, other: &Self) -> bool {
        self == other
    }
    fn write_json(&self, encoder: &mut Encoder, _version: i64) -> PyResult<()> {
        encoder.out.extend_from_slice(itoa::Buffer::new().format(*self).as_bytes());
        Ok(())
    }
}

impl Field for Arc<str> {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        let text = value.downcast_exact::<PyString>().map_err(|_| Misfit::new("not a str"))?;
        Ok(Arc::from(text.to_str()?))
    }
    fn to_py(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        (&**self).into_py_any(py)
    }
    fn same(&self, other: &Self) -> bool {
        self == other
    }
    fn write_json(&self, encoder: &mut Encoder, _version: i64) -> PyResult<()> {
        write_json_string(&mut encoder.out, self);
        Ok(())
    }
}

impl<T: Field> Field for Option<T> {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        if value.is_none() {
            Ok(None)
        } else {
            T::from_py(value).map(Some)
        }
    }
    fn to_py(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match self {
            Some(value) => value.to_py(py),
            None => Ok(py.None()),
        }
    }
    fn same(&self, other: &Self) -> bool {
        match (self, other) {
            (Some(mine), Some(theirs)) => mine.same(theirs),
            (None, None) => true,
            _ => false,
        }
    }
    fn write_json(&self, encoder: &mut Encoder, version: i64) -> PyResult<()> {
        match self {
            Some(value) => value.write_json(encoder, version),
            None => {
                encoder.out.extend_from_slice(b"null");
                Ok(())
            }
        }
    }
}

/// An ID list: a list of a facility or a staff member. Its node is a `TrackedList`.
impl Field for Vec<Arc<str>> {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        let list = as_readable_list(value).ok_or_else(|| Misfit::new("not a list"))?;
        list.iter().map(|item| <Arc<str>>::from_py(&item)).collect()
    }
    fn to_py(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(PyList::new(py, self.iter().map(|item| &**item))?.into_any().unbind())
    }
    fn same(&self, other: &Self) -> bool {
        self == other
    }
    fn write_json(&self, encoder: &mut Encoder, _version: i64) -> PyResult<()> {
        write_strings(encoder, self);
        Ok(())
    }
}

/// `StaffState.skills`: a tuple of str.
#[derive(Clone, PartialEq)]
pub(crate) struct Skills(pub(crate) Vec<Arc<str>>);

impl Field for Skills {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        let items = value.downcast_exact::<PyTuple>().map_err(|_| Misfit::new("not a tuple"))?;
        items.iter().map(|item| <Arc<str>>::from_py(&item)).collect::<Result<_, _>>().map(Skills)
    }
    fn to_py(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(PyTuple::new(py, self.0.iter().map(|item| &**item))?.into_any().unbind())
    }
    fn same(&self, other: &Self) -> bool {
        self == other
    }
    fn write_json(&self, encoder: &mut Encoder, _version: i64) -> PyResult<()> {
        write_strings(encoder, &self.0);
        Ok(())
    }
}

impl Field for V {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        Ok(V::from_py(value)?)
    }
    fn to_py(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(V::to_py(self, py)?.unbind())
    }
    fn same(&self, other: &Self) -> bool {
        self == other
    }
    fn write_json(&self, encoder: &mut Encoder, _version: i64) -> PyResult<()> {
        V::write_json(self, encoder)
    }
}

/// An enum field: a member in the row is its number, and the member object is `Installed`'s, so
/// `agent.activity is Activity.IDLE` holds for anything Rust writes.
macro_rules! member_field {
    ($name:ident, $all:ident, $table:ident, $values:ident, [$($member:ident),* $(,)?]) => {
        #[derive(Clone, Copy, Debug, Eq, PartialEq)]
        pub(crate) enum $name { $($member),* }

        const $all: &[$name] = &[$($name::$member),*];

        impl Field for $name {
            fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
                let members = &installed()?.$table;
                members
                    .iter()
                    .position(|member| value.is(member))
                    .map(|index| $all[index])
                    .ok_or_else(|| Misfit::new(concat!("not a ", stringify!($name))))
            }
            fn to_py(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
                let members = &installed().map_err(|misfit| PyRuntimeError::new_err(misfit.0))?.$table;
                Ok(members[*self as usize].clone_ref(py))
            }
            fn same(&self, other: &Self) -> bool {
                self == other
            }
            fn write_json(&self, encoder: &mut Encoder, _version: i64) -> PyResult<()> {
                let values = &installed().map_err(|misfit| PyRuntimeError::new_err(misfit.0))?.$values;
                write_json_string(&mut encoder.out, &values[*self as usize]);
                Ok(())
            }
        }
    };
}

member_field!(Activity, ACTIVITIES, activity, activity_values, [Idle, Moving, Queued, InService, SeekingHelp, Sleeping, Resting]);
member_field!(Mobility, MOBILITIES, mobility, mobility_values, [Independent, Assisted, Wheelchair]);
member_field!(RequestStatus, STATUSES, status, status_values, [Pending, Assigned, Resolved, Unresolved]);

// ---- Rows ------------------------------------------------------------------------------------

/// A document holds this field only in WorldState v6.
fn v6<T>(_value: &T, version: i64) -> bool {
    version == 6
}

/// A document leaves this field out while it is None.
fn set<T>(value: &Option<T>, _version: i64) -> bool {
    value.is_some()
}

macro_rules! keeps_field {
    ($value:expr, $version:expr) => {
        true
    };
    ($value:expr, $version:expr, $keep:ident) => {
        $keep($value, $version)
    };
}

/// A struct of fields, each named as the attribute of the Python object. `name[rule]` says when a
/// document holds the field (`v6`, `set`).
macro_rules! row {
    ($name:ident { $($field:ident $([$keep:ident])?: $kind:ty),* $(,)? }) => {
        // Rules read these fields as they move to Rust.
        #[allow(dead_code)]
        #[derive(Clone)]
        pub(crate) struct $name {
            $(pub(crate) $field: $kind,)*
        }

        impl Field for $name {
            fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
                let py = value.py();
                Ok(Self {
                    $($field: <$kind as Field>::from_py(
                        &value.getattr(intern!(py, stringify!($field)))?,
                    )?,)*
                })
            }
            fn to_py(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
                let document = PyDict::new(py);
                $(document.set_item(intern!(py, stringify!($field)), self.$field.to_py(py)?)?;)*
                Ok(document.into_any().unbind())
            }
            fn same(&self, other: &Self) -> bool {
                true $(&& self.$field.same(&other.$field))*
            }
            fn write_json(&self, encoder: &mut Encoder, version: i64) -> PyResult<()> {
                const NAMES: &[&str] = &[$(stringify!($field)),*];
                const ORDER: [usize; NAMES.len()] = crate::core::row::sorted_order(NAMES);
                type Writer = fn(&$name, &mut Encoder, i64, &mut bool) -> PyResult<()>;
                const WRITERS: &[Writer] = &[$(|row, encoder, version, first| {
                    if !keeps_field!(&row.$field, version $(, $keep)?) {
                        return Ok(());
                    }
                    if !*first {
                        encoder.out.push(b',');
                    }
                    *first = false;
                    write_json_string(&mut encoder.out, stringify!($field));
                    encoder.out.push(b':');
                    Field::write_json(&row.$field, encoder, version)
                }),*];
                let mut first = true;
                encoder.out.push(b'{');
                for index in ORDER {
                    WRITERS[index](self, encoder, version, &mut first)?;
                }
                encoder.out.push(b'}');
                Ok(())
            }
        }

        impl $name {
            /// Take a forwarded attribute write. A name that is no field (a property) is nothing
            /// to do here. ProfileRow's whole value is replaced instead.
            #[allow(dead_code)]
            fn set(&mut self, name: &str, value: &Bound<'_, PyAny>) -> Result<(), Misfit> {
                match name {
                    $(stringify!($field) => self.$field = <$kind as Field>::from_py(value)?,)*
                    _ => {}
                }
                Ok(())
            }
        }
    };
}

row!(NeedRow { toilet_urgency: f64, hunger: f64, fatigue: f64, hydration_need[v6]: f64 });

row!(ProfileRow {
    id: Arc<str>,
    group_id: Option<Arc<str>>,
    initial_zone_id: Arc<str>,
    mobility: Mobility,
    support_need: f64,
    stress_sensitivity: f64,
    rng_key: Arc<str>,
    initial_needs: NeedRow,
    provenance: V,
});

row!(AgentRow {
    profile: ProfileRow,
    zone_id: Arc<str>,
    activity: Activity,
    target_id: Option<Arc<str>>,
    needs: NeedRow,
    stress: f64,
    trust: f64,
    next_decision_at: i64,
    support_request_id: Option<Arc<str>>,
    support_cooldown_until: i64,
});

row!(FacilityRow {
    id: Arc<str>,
    kind: Arc<str>,
    zone_id: Arc<str>,
    capacity: i64,
    working_units: i64,
    active_agent_ids: Vec<Arc<str>>,
    queue_agent_ids: Vec<Arc<str>>,
    resource_active_agent_ids[v6]: Vec<Arc<str>>,
    resource_queue_agent_ids[v6]: Vec<Arc<str>>,
});

row!(StaffRow {
    id: Arc<str>,
    zone_id: Arc<str>,
    skills: Skills,
    work_queue: Vec<Arc<str>>,
    busy_until: i64,
    active_task_id: Option<Arc<str>>,
});

row!(RequestRow {
    id: Arc<str>,
    agent_id: Arc<str>,
    requested_minute: i64,
    timeout_minute: i64,
    status: RequestStatus,
    assigned_staff_id: Option<Arc<str>>,
    task_id: Option<Arc<str>>,
    resolved_minute: Option<i64>,
    cooldown_until: i64,
    last_assignment_attempt_minute[set]: Option<i64>,
    last_assignment_wait_reason_code[set]: Option<Arc<str>>,
});

row!(TaskRow {
    id: Arc<str>,
    kind: Arc<str>,
    agent_id: Option<Arc<str>>,
    resource_id: Option<Arc<str>>,
    start_minute: i64,
    due_minute: i64,
    staff_id: Option<Arc<str>>,
});

/// The rows of one `IdMap`: a stable slot per row, and the keys in the dict's order. `changed` says
/// which slots may differ from the document bytes kept for them (`EntityStore::rows_json`): anything
/// that can reach a row for writing sets it, so a clear flag means the row is the one written.
pub(crate) struct Table<R> {
    rows: Vec<Option<R>>,
    free: Vec<u32>,
    ids: IndexMap<Arc<str>, u32, FxBuildHasher>,
    changed: Vec<bool>,
}

impl<R> Default for Table<R> {
    fn default() -> Self {
        Self { rows: Vec::new(), free: Vec::new(), ids: IndexMap::default(), changed: Vec::new() }
    }
}

impl<R> Table<R> {
    fn reserve_slot(&mut self) -> u32 {
        let slot = self.free.pop().unwrap_or_else(|| {
            self.rows.push(None);
            self.changed.push(true);
            u32::try_from(self.rows.len() - 1).expect("under 2**32 entities")
        });
        self.changed[slot as usize] = true;
        slot
    }

    /// The row in a slot, for writing.
    fn slot_mut(&mut self, slot: u32) -> Option<&mut R> {
        let row = self.rows.get_mut(slot as usize)?.as_mut()?;
        self.changed[slot as usize] = true;
        Some(row)
    }

    fn release_slot(&mut self, slot: u32) {
        if let Some(row) = self.rows.get_mut(slot as usize) {
            if row.take().is_some() {
                self.changed[slot as usize] = true;
                self.free.push(slot);
            }
        }
    }

    pub(crate) fn len(&self) -> usize {
        self.ids.len()
    }

    /// The row of a key.
    pub(crate) fn get(&self, id: &str) -> Option<&R> {
        self.rows.get(*self.ids.get(id)? as usize)?.as_ref()
    }

    pub(crate) fn get_mut(&mut self, id: &str) -> Option<&mut R> {
        let slot = *self.ids.get(id)?;
        self.slot_mut(slot)
    }

    /// The keys and rows in the order of the dict.
    pub(crate) fn iter(&self) -> impl Iterator<Item = (&Arc<str>, &R)> {
        self.ids.iter().filter_map(|(id, slot)| Some((id, self.rows[*slot as usize].as_ref()?)))
    }
}

/// All the rows of a world.
#[derive(Default)]
pub(crate) struct Entities {
    pub(crate) agents: Table<AgentRow>,
    pub(crate) facilities: Table<FacilityRow>,
    pub(crate) staff: Table<StaffRow>,
    pub(crate) support_requests: Table<RequestRow>,
    pub(crate) active_tasks: Table<TaskRow>,
}

#[allow(dead_code)]
fn assert_rows_are_shareable() {
    fn check<T: Send + Sync>() {}
    check::<Entities>();
}

// ---- Which attributes hold what -----------------------------------------------------------

// The number after the slot in a `TrackedList`'s `_node`, which says which field the list is.
const LIST_FIELDS: [(&str, u8); 5] = [
    ("active_agent_ids", FACILITY),
    ("queue_agent_ids", FACILITY),
    ("resource_active_agent_ids", FACILITY),
    ("resource_queue_agent_ids", FACILITY),
    ("work_queue", STAFF),
];
const LIST_KINDS: u32 = LIST_FIELDS.len() as u32;

fn list_node(slot: u32, field: usize) -> u32 {
    slot * LIST_KINDS + field as u32
}

fn list_field_of(kind: u8, name: &str) -> Option<usize> {
    LIST_FIELDS.iter().position(|(field, owner)| *owner == kind && *field == name)
}

fn class_of(installed: &Installed, kind: u8) -> &Py<PyType> {
    match kind {
        AGENT => &installed.agent,
        NEEDS => &installed.needs,
        FACILITY => &installed.facility,
        STAFF => &installed.staff,
        REQUEST => &installed.request,
        _ => &installed.task,
    }
}

/// Store an attribute of an entity without its `__setattr__`.
fn raw_setattr(
    object: &Bound<'_, PyAny>,
    name: &Bound<'_, PyString>,
    value: *mut ffi::PyObject,
) -> PyResult<()> {
    // SAFETY: object and name are live; the call takes its own reference to value.
    if unsafe { ffi::PyObject_GenericSetAttr(object.as_ptr(), name.as_ptr(), value) } != 0 {
        return Err(PyErr::fetch(object.py()));
    }
    Ok(())
}

fn exact_type_is(value: &Bound<'_, PyAny>, class: &Py<PyType>) -> bool {
    value.get_type().is(class)
}

// ---- The store -------------------------------------------------------------------------------

/// The canonical JSON of each row of a table as `canonical_json` writes it, in the order
/// `WorldState._canonical_data` gives: by the entity's own ID, stably from the dict's order. A row
/// nothing reached for writing keeps its bytes object, so the same object means the same row.
fn table_json<'py, R: Field>(
    py: Python<'py>,
    table: &mut Table<R>,
    kept: &mut Vec<Option<Py<PyBytes>>>,
    version: i64,
    id_of: fn(&R) -> &str,
) -> PyResult<Bound<'py, PyList>> {
    let Table { rows, ids, changed, .. } = table;
    let mut order: Vec<(&str, u32)> = ids
        .values()
        .filter_map(|slot| Some((id_of(rows[*slot as usize].as_ref()?), *slot)))
        .collect();
    order.sort_by(|first, second| first.0.as_bytes().cmp(second.0.as_bytes()));
    kept.resize_with(rows.len(), || None);
    let mut encoder = Encoder { out: Vec::with_capacity(512) };
    let list = PyList::empty(py);
    for (_, slot) in order {
        let slot = slot as usize;
        let current = match &kept[slot] {
            Some(raw) if !changed[slot] => raw.clone_ref(py),
            _ => {
                encoder.out.clear();
                let Some(row) = rows[slot].as_ref() else { continue };
                row.write_json(&mut encoder, version)?;
                let raw = PyBytes::new(py, &encoder.out).unbind();
                kept[slot] = Some(raw.clone_ref(py));
                changed[slot] = false;
                raw
            }
        };
        list.append(current)?;
    }
    Ok(list)
}

/// The rows of one world, and the sink of its attached objects, `IdMap`s and ID lists.
#[pyclass(module = "sheltersim_native")]
#[derive(Default)]
pub struct EntityStore {
    rows: Entities,
    // The document bytes of each table's rows by slot, and the WorldState version they are for.
    row_json: [Vec<Option<Py<PyBytes>>>; 5],
    json_version: i64,
    taint: Option<String>,
    forwards: u64,
    attaches: u64,
    detaches: u64,
    taints: u64,
}

impl EntityStore {
    fn taint(&mut self, why: String) {
        self.rows = Entities::default();
        self.taints += 1;
        self.taint.get_or_insert(why);
    }

    fn table_len(&self, kind: u8) -> usize {
        match kind {
            AGENT => self.rows.agents.len(),
            FACILITY => self.rows.facilities.len(),
            STAFF => self.rows.staff.len(),
            REQUEST => self.rows.support_requests.len(),
            _ => self.rows.active_tasks.len(),
        }
    }

    /// Whether the object is attached to this store, and its slot if so.
    fn attached_here(slf: &Bound<'_, Self>, object: &Bound<'_, PyAny>) -> PyResult<Option<u32>> {
        let py = slf.py();
        let store = object.getattr(intern!(py, "_store"))?;
        if store.is(slf) {
            Ok(Some(object.getattr(intern!(py, "_slot"))?.extract()?))
        } else {
            Ok(None)
        }
    }

    fn mark_attached(
        slf: &Bound<'_, Self>,
        object: &Bound<'_, PyAny>,
        slot: u32,
    ) -> PyResult<()> {
        let py = slf.py();
        raw_setattr(object, intern!(py, "_store"), slf.as_ptr())?;
        let slot = slot.into_pyobject(py)?;
        raw_setattr(object, intern!(py, "_slot"), slot.as_ptr())
    }

    fn mark_detached(object: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = object.py();
        // SAFETY: None is a live object for as long as the GIL is held.
        raw_setattr(object, intern!(py, "_store"), py.None().as_ptr())
    }

    /// What a field stores for an ID list: a `TrackedList` of this store. A free one moves in as
    /// itself and anything else readable is copied, so a list has one place: a write can only
    /// mean one row. Not a list: an error for the caller to turn into a taint.
    fn adopt_list<'py>(
        slf: &Bound<'py, Self>,
        value: &Bound<'py, PyAny>,
        node: u32,
    ) -> Result<Bound<'py, PyAny>, Misfit> {
        let py = slf.py();
        let list = as_readable_list(value).ok_or_else(|| Misfit::new("not a list"))?;
        let tracked = value.is_instance_of::<PyList>() && !value.is_exact_instance_of::<PyList>();
        let reuse = tracked && value.getattr(intern!(py, "_sink"))?.is_none();
        let stored = if reuse {
            value.clone()
        } else {
            let new = new_tracked_list(py)?;
            for item in list.iter() {
                raw_list_append(&new, &item)?;
            }
            new
        };
        stored.setattr(intern!(py, "_sink"), slf)?;
        stored.setattr(intern!(py, "_node"), node)?;
        Ok(stored)
    }

    fn free_list(slf: &Bound<'_, Self>, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        let tracked = value.is_instance_of::<PyList>() && !value.is_exact_instance_of::<PyList>();
        if tracked && as_readable_list(value).is_some() && value.getattr(intern!(py, "_sink"))?.is(slf) {
            value.setattr(intern!(py, "_sink"), py.None())?;
            value.setattr(intern!(py, "_node"), py.None())?;
        }
        Ok(())
    }

    /// Give an object its links: its ID lists become nodes, and an agent's needs are attached.
    fn link_children(
        slf: &Bound<'_, Self>,
        kind: u8,
        object: &Bound<'_, PyAny>,
        slot: u32,
    ) -> Result<(), Misfit> {
        let py = slf.py();
        for (index, (name, owner)) in LIST_FIELDS.iter().enumerate() {
            if *owner == kind {
                let name = PyString::intern(py, name);
                let current = object.getattr(&name)?;
                let stored = Self::adopt_list(slf, &current, list_node(slot, index))?;
                if !stored.is(&current) {
                    raw_setattr(object, &name, stored.as_ptr())?;
                }
            }
        }
        if kind == AGENT {
            Self::link_needs(slf, &object.getattr(intern!(py, "needs"))?, slot)?;
        }
        Ok(())
    }

    fn link_needs(slf: &Bound<'_, Self>, needs: &Bound<'_, PyAny>, slot: u32) -> Result<(), Misfit> {
        if !exact_type_is(needs, &installed()?.needs) {
            return Err(Misfit::new("an agent's needs are not a NeedState"));
        }
        if !needs.getattr(intern!(slf.py(), "_store"))?.is_none() {
            return Err(Misfit::new("a NeedState has two places"));
        }
        Self::mark_attached(slf, needs, slot)?;
        Ok(())
    }

    fn unlink_children(slf: &Bound<'_, Self>, kind: u8, object: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        for (name, owner) in LIST_FIELDS.iter() {
            if *owner == kind {
                let name = PyString::intern(py, name);
                Self::free_list(slf, &object.getattr(&name)?)?;
            }
        }
        if kind == AGENT {
            let needs = object.getattr(intern!(py, "needs"))?;
            if exact_type_is(&needs, class_of(installed().map_err(misfit_error)?, NEEDS))
                && Self::attached_here(slf, &needs)?.is_some()
            {
                Self::mark_detached(&needs)?;
            }
        }
        Ok(())
    }

    /// Read an object into a free slot. The caller knows it has the right class.
    fn load_into_table(
        slf: &Bound<'_, Self>,
        kind: u8,
        object: &Bound<'_, PyAny>,
    ) -> Result<u32, Misfit> {
        let slot = {
            let mut this = slf.try_borrow_mut().map_err(pyo3_misfit)?;
            match kind {
                AGENT => this.rows.agents.reserve_slot(),
                FACILITY => this.rows.facilities.reserve_slot(),
                STAFF => this.rows.staff.reserve_slot(),
                REQUEST => this.rows.support_requests.reserve_slot(),
                _ => this.rows.active_tasks.reserve_slot(),
            }
        };
        Self::link_children(slf, kind, object, slot)?;
        // Getting these attributes runs no Python code: the classes define no such properties.
        let mut this = slf.try_borrow_mut().map_err(pyo3_misfit)?;
        match kind {
            AGENT => this.rows.agents.rows[slot as usize] = Some(AgentRow::from_py(object)?),
            FACILITY => this.rows.facilities.rows[slot as usize] = Some(FacilityRow::from_py(object)?),
            STAFF => this.rows.staff.rows[slot as usize] = Some(StaffRow::from_py(object)?),
            REQUEST => this.rows.support_requests.rows[slot as usize] = Some(RequestRow::from_py(object)?),
            _ => this.rows.active_tasks.rows[slot as usize] = Some(TaskRow::from_py(object)?),
        }
        Ok(slot)
    }

    fn set_slot_of(&mut self, kind: u8, key: Arc<str>, slot: u32) -> Option<u32> {
        match kind {
            AGENT => self.rows.agents.ids.insert(key, slot),
            FACILITY => self.rows.facilities.ids.insert(key, slot),
            STAFF => self.rows.staff.ids.insert(key, slot),
            REQUEST => self.rows.support_requests.ids.insert(key, slot),
            _ => self.rows.active_tasks.ids.insert(key, slot),
        }
    }

    fn drop_key(&mut self, kind: u8, key: &str) -> Option<u32> {
        // shift_remove: the order of the keys is the order of the dict.
        let slot = match kind {
            AGENT => self.rows.agents.ids.shift_remove(key),
            FACILITY => self.rows.facilities.ids.shift_remove(key),
            STAFF => self.rows.staff.ids.shift_remove(key),
            REQUEST => self.rows.support_requests.ids.shift_remove(key),
            _ => self.rows.active_tasks.ids.shift_remove(key),
        };
        if let Some(slot) = slot {
            self.drop_slot(kind, slot);
        }
        slot
    }

    fn drop_slot(&mut self, kind: u8, slot: u32) {
        match kind {
            AGENT => self.rows.agents.release_slot(slot),
            FACILITY => self.rows.facilities.release_slot(slot),
            STAFF => self.rows.staff.release_slot(slot),
            REQUEST => self.rows.support_requests.release_slot(slot),
            _ => self.rows.active_tasks.release_slot(slot),
        }
    }

    /// Attach an object under a key, or taint. Never raises for a value that does not fit.
    fn attach(
        slf: &Bound<'_, Self>,
        kind: u8,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        if slf.try_borrow()?.taint.is_some() {
            return Ok(());
        }
        match Self::attach_fitting(slf, kind, key, value) {
            Ok(()) => Ok(()),
            Err(misfit) => {
                slf.try_borrow_mut()?.taint(misfit.0);
                Ok(())
            }
        }
    }

    fn attach_fitting(
        slf: &Bound<'_, Self>,
        kind: u8,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
    ) -> Result<(), Misfit> {
        let py = slf.py();
        let key = <Arc<str>>::from_py(key)?;
        if !exact_type_is(value, class_of(installed()?, kind)) {
            return Err(Misfit::new("not the entity class of this map"));
        }
        if !value.getattr(intern!(py, "_store"))?.is_none() {
            return Err(Misfit::new("an entity has two places"));
        }
        let slot = match Self::load_into_table(slf, kind, value) {
            Ok(slot) => slot,
            Err(misfit) => {
                // Half linked, it would hold a list or needs of a row that does not exist.
                Self::unlink_children(slf, kind, value)?;
                return Err(misfit);
            }
        };
        Self::mark_attached(slf, value, slot)?;
        let mut this = slf.try_borrow_mut().map_err(pyo3_misfit)?;
        this.attaches += 1;
        if let Some(old) = this.set_slot_of(kind, key, slot) {
            this.drop_slot(kind, old);
        }
        Ok(())
    }

    /// The object left the map, or is going to be replaced. It works on as a plain object.
    fn detach(
        slf: &Bound<'_, Self>,
        kind: u8,
        key: Option<&Bound<'_, PyAny>>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        if let Some(key) = key {
            if let Ok(key) = key.downcast_exact::<PyString>() {
                slf.try_borrow_mut()?.drop_key(kind, key.to_str()?);
            }
        }
        let py = slf.py();
        // Anything but the map's class was never attached: it may not have these attributes.
        if value.hasattr(intern!(py, "_store"))? && Self::attached_here(slf, value)?.is_some() {
            Self::unlink_children(slf, kind, value)?;
            Self::mark_detached(value)?;
            slf.try_borrow_mut()?.detaches += 1;
        }
        Ok(())
    }

    fn write_row(
        slf: &Bound<'_, Self>,
        kind: u8,
        slot: u32,
        name: &str,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let mut this = slf.try_borrow_mut()?;
        let this = &mut *this;
        this.forwards += 1;
        if this.taint.is_some() {
            return Ok(());
        }
        fn set_in<R>(
            table: &mut Table<R>,
            slot: u32,
            apply: impl FnOnce(&mut R) -> Result<(), Misfit>,
        ) -> Result<(), Misfit> {
            match table.slot_mut(slot) {
                Some(row) => apply(row),
                None => Err(Misfit::new("an attached object has no row")),
            }
        }
        let result = match kind {
            AGENT => set_in(&mut this.rows.agents, slot, |row| row.set(name, value)),
            NEEDS => set_in(&mut this.rows.agents, slot, |row| row.needs.set(name, value)),
            FACILITY => set_in(&mut this.rows.facilities, slot, |row| row.set(name, value)),
            STAFF => set_in(&mut this.rows.staff, slot, |row| row.set(name, value)),
            REQUEST => set_in(&mut this.rows.support_requests, slot, |row| row.set(name, value)),
            _ => set_in(&mut this.rows.active_tasks, slot, |row| row.set(name, value)),
        };
        if let Err(misfit) = result {
            this.taint(misfit.0);
        }
        Ok(())
    }

    /// `facility.queue_agent_ids = value`.
    fn set_list(
        slf: &Bound<'_, Self>,
        kind: u8,
        object: &Bound<'_, PyAny>,
        slot: u32,
        field: usize,
        name: &Bound<'_, PyString>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let old = object.getattr(name)?;
        if old.is(value) {
            return Ok(());
        }
        // Whatever comes next, the old list leaves: nothing may write to a row by its stale node.
        Self::free_list(slf, &old)?;
        if slf.try_borrow()?.taint.is_some() {
            return raw_setattr(object, name, value.as_ptr());
        }
        match Self::adopt_list(slf, value, list_node(slot, field)) {
            Ok(stored) => {
                raw_setattr(object, name, stored.as_ptr())?;
                Self::write_row(slf, kind, slot, name.to_str()?, &stored)
            }
            Err(misfit) => {
                raw_setattr(object, name, value.as_ptr())?;
                slf.try_borrow_mut()?.taint(misfit.0);
                Ok(())
            }
        }
    }

    /// `agent.needs = value`.
    fn set_needs(
        slf: &Bound<'_, Self>,
        object: &Bound<'_, PyAny>,
        slot: u32,
        name: &Bound<'_, PyString>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let old = object.getattr(name)?;
        if old.is(value) {
            return Ok(());
        }
        raw_setattr(object, name, value.as_ptr())?;
        // Whatever comes next, the old object leaves: its slot may be another agent's later.
        if exact_type_is(&old, class_of(installed().map_err(misfit_error)?, NEEDS))
            && Self::attached_here(slf, &old)?.is_some()
        {
            Self::mark_detached(&old)?;
        }
        if slf.try_borrow()?.taint.is_some() {
            return Ok(());
        }
        if let Err(misfit) = Self::link_needs(slf, value, slot) {
            slf.try_borrow_mut()?.taint(misfit.0);
            return Ok(());
        }
        Self::write_row(slf, AGENT, slot, name.to_str()?, value)
    }
}

fn pyo3_misfit<E: std::fmt::Display>(error: E) -> Misfit {
    Misfit::new(error.to_string())
}

fn misfit_error(misfit: Misfit) -> PyErr {
    PyRuntimeError::new_err(misfit.0)
}

fn dict_items<'py>(dict: &Bound<'py, PyDict>) -> Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let py = dict.py();
    // SAFETY: as in world_core::dict_values: nothing here runs Python code, and each step takes
    // new references to what it borrowed.
    unsafe {
        let mut rows = Vec::with_capacity(ffi::PyDict_Size(dict.as_ptr()).max(0) as usize);
        let mut position = 0;
        let mut key = null_mut();
        let mut value = null_mut();
        while ffi::PyDict_Next(dict.as_ptr(), &mut position, &mut key, &mut value) != 0 {
            rows.push((Bound::from_borrowed_ptr(py, key), Bound::from_borrowed_ptr(py, value)));
        }
        rows
    }
}

fn new_id_map<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
    let kind = id_map_type();
    if kind.is_null() {
        return Err(PyRuntimeError::new_err("core/state_mirror.py is not imported"));
    }
    // SAFETY: kind is the class handed to install_id_map_type, alive for the life of the process.
    let class = unsafe { Bound::from_borrowed_ptr(py, kind.cast()) };
    class.call0()
}

#[pymethods]
impl EntityStore {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    /// `object.name = value` on an attached object, from its `__setattr__`.
    fn write(
        slf: &Bound<'_, Self>,
        object: &Bound<'_, PyAny>,
        kind: u8,
        slot: u32,
        name: &Bound<'_, PyString>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let text = name.to_str()?;
        if let Some(field) = list_field_of(kind, text) {
            return Self::set_list(slf, kind, object, slot, field, name, value);
        }
        if kind == AGENT && text == "needs" {
            return Self::set_needs(slf, object, slot, name, value);
        }
        // Before the row, and with no borrow: a property's setter comes back through here.
        raw_setattr(object, name, value.as_ptr())?;
        Self::write_row(slf, kind, slot, text, value)
    }

    /// Something was done to an object that no row can follow, such as deleting a field.
    fn taint_now(&mut self, why: String) {
        self.taint(why);
    }

    /// `map[key] = value` on a bound `IdMap`: what stores it also attaches it.
    fn map_set(
        slf: &Bound<'_, Self>,
        map: &Bound<'_, PyAny>,
        kind: u8,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        // A key that can't be hashed fails here, with dict's own error, before any change.
        let old = raw_dict_get(map, key)?;
        if old.as_ref().is_some_and(|old| old.is(value)) {
            return Ok(());
        }
        if let Some(old) = &old {
            // Not by key: the new row takes the old key's place in the order.
            Self::detach(slf, kind, None, old)?;
        }
        Self::attach(slf, kind, key, value)?;
        raw_dict_set(map, key, value)
    }

    /// The value left a bound `IdMap`.
    fn map_removed(
        slf: &Bound<'_, Self>,
        kind: u8,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        Self::detach(slf, kind, Some(key), value)
    }

    /// A new `IdMap` bound to this store with the entries of value, which is what the world
    /// stores for one of its fields. `old` is the map it replaces, if any, whose entries are
    /// detached unless value holds them too, and which becomes a plain holder of what it held.
    /// Anything but a mapping is stored as it is, for validation to say what is wrong with it.
    fn assign<'py>(
        slf: &Bound<'py, Self>,
        kind: u8,
        old: &Bound<'py, PyAny>,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = slf.py();
        let old_map = old
            .downcast::<PyDict>()
            .ok()
            .filter(|map| map.get_type().as_ptr() == id_map_type().cast());
        let bound_here = match old_map {
            Some(map) => map.getattr(intern!(py, "_store"))?.is(slf),
            None => false,
        };
        let old_items = match old_map {
            Some(map) if bound_here => dict_items(map),
            _ => Vec::new(),
        };
        if bound_here {
            let map = old_map.expect("bound_here implies a map");
            map.setattr(intern!(py, "_store"), py.None())?;
        }
        // Keep the slots of what stays, so assigning a filtered dict costs one read per entry.
        let mut this_ids = IndexMap::<Arc<str>, u32, FxBuildHasher>::default();
        let mut kept = FxHashSet::default();
        let result = match value.downcast::<PyDict>() {
            Err(_) => {
                slf.try_borrow_mut()?.taint("a field of entities is not a mapping".to_owned());
                value.clone()
            }
            Ok(mapping) => {
                let items = dict_items(mapping);
                let map = new_id_map(py)?;
                let mut fresh = Vec::new();
                for (key, item) in &items {
                    raw_dict_set(&map, key, item)?;
                    let stays = bound_here
                        && slf.try_borrow()?.taint.is_none()
                        && item.get_type().is(class_of(installed().map_err(misfit_error)?, kind))
                        && key.is_exact_instance_of::<PyString>();
                    match stays.then(|| Self::attached_here(slf, item)).transpose()?.flatten() {
                        Some(slot) if kept.insert(slot) => {
                            this_ids.insert(Arc::from(key.downcast_exact::<PyString>()?.to_str()?), slot);
                        }
                        // The same object under two keys.
                        Some(_) => slf.try_borrow_mut()?.taint("an entity has two places".to_owned()),
                        None => fresh.push((key, item)),
                    }
                }
                {
                    let mut this = slf.try_borrow_mut()?;
                    if this.taint.is_none() {
                        match kind {
                            AGENT => this.rows.agents.ids = this_ids,
                            FACILITY => this.rows.facilities.ids = this_ids,
                            STAFF => this.rows.staff.ids = this_ids,
                            REQUEST => this.rows.support_requests.ids = this_ids,
                            _ => this.rows.active_tasks.ids = this_ids,
                        }
                    }
                }
                // New rows go to the end of the keys; then the keys take the dict's order.
                for (key, item) in &fresh {
                    Self::attach(slf, kind, key, item)?;
                }
                if !fresh.is_empty() && !kept.is_empty() {
                    Self::reorder_keys(slf, kind, &items)?;
                }
                map.setattr(intern!(py, "_store"), slf)?;
                map.setattr(intern!(py, "_table"), kind)?;
                map
            }
        };
        for (_, item) in &old_items {
            let attached = item.hasattr(intern!(py, "_store"))?
                && Self::attached_here(slf, item)?.is_some_and(|slot| !kept.contains(&slot));
            if attached {
                let slot = Self::attached_here(slf, item)?.expect("checked above");
                Self::detach(slf, kind, None, item)?;
                slf.try_borrow_mut()?.drop_slot(kind, slot);
            }
        }
        Ok(result)
    }

    /// The rows of a tainted store, read again from the objects of the world. True if they all
    /// fit now.
    fn refit(slf: &Bound<'_, Self>, world: &Bound<'_, PyAny>) -> PyResult<bool> {
        {
            let mut this = slf.try_borrow_mut()?;
            this.rows = Entities::default();
            this.taint = None;
        }
        for (name, kind) in FIELDS {
            let map = world.getattr(name)?;
            let Ok(map) = map.downcast::<PyDict>() else {
                slf.try_borrow_mut()?.taint("a field of entities is not a mapping".to_owned());
                return Ok(false);
            };
            let items = dict_items(map);
            for (_, item) in &items {
                // Attached before the taint: the rows those slots named are gone.
                let class = class_of(installed().map_err(misfit_error)?, kind);
                if exact_type_is(item, class) && Self::attached_here(slf, item)?.is_some() {
                    Self::unlink_children(slf, kind, item)?;
                    Self::mark_detached(item)?;
                }
            }
            for (key, item) in &items {
                Self::attach(slf, kind, key, item)?;
            }
        }
        Ok(slf.try_borrow()?.taint.is_none())
    }

    /// The first difference between the rows and the objects of the world, or None: the check
    /// behind the tests and the soak. A tainted store has no rows to differ.
    fn verify(slf: &Bound<'_, Self>, world: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
        if slf.try_borrow()?.taint.is_some() {
            return Ok(None);
        }
        for (name, kind) in FIELDS {
            let map = world.getattr(name)?;
            let map = map.downcast::<PyDict>()?;
            let items = dict_items(map);
            let this = slf.try_borrow()?;
            if this.table_len(kind) != items.len() {
                return Ok(Some(format!(
                    "{name}: {} rows, {} objects",
                    this.table_len(kind),
                    items.len()
                )));
            }
            fn differs<R: Field>(
                table: &Table<R>,
                name: &str,
                items: &[(Bound<'_, PyAny>, Bound<'_, PyAny>)],
            ) -> PyResult<Option<String>> {
                for ((id, row), (key, item)) in table.iter().zip(items) {
                    if key.downcast_exact::<PyString>()?.to_str()? != &**id {
                        return Ok(Some(format!("{name}: key {id}, in the dict {key}")));
                    }
                    let current = R::from_py(item).map_err(misfit_error)?;
                    if !current.same(row) {
                        return Ok(Some(format!("{name}.{id}: the row differs from the object")));
                    }
                }
                Ok(None)
            }
            let found = match kind {
                AGENT => differs(&this.rows.agents, name, &items)?,
                FACILITY => differs(&this.rows.facilities, name, &items)?,
                STAFF => differs(&this.rows.staff, name, &items)?,
                REQUEST => differs(&this.rows.support_requests, name, &items)?,
                _ => differs(&this.rows.active_tasks, name, &items)?,
            };
            if found.is_some() {
                return Ok(found);
            }
        }
        Ok(None)
    }

    /// A row as a new dict named like the dataclass's fields, or None.
    fn row(&self, py: Python<'_>, field: &str, id: &str) -> PyResult<Option<Py<PyAny>>> {
        fn document<R: Field>(py: Python<'_>, table: &Table<R>, id: &str) -> PyResult<Option<Py<PyAny>>> {
            table.get(id).map(|row| row.to_py(py)).transpose()
        }
        match field {
            "agents" => document(py, &self.rows.agents, id),
            "facilities" => document(py, &self.rows.facilities, id),
            "staff" => document(py, &self.rows.staff, id),
            "support_requests" => document(py, &self.rows.support_requests, id),
            "active_tasks" => document(py, &self.rows.active_tasks, id),
            _ => Err(PyValueError::new_err("not a field of entities")),
        }
    }

    /// `{field: [bytes of each row]}` for a WorldState document of this version: what
    /// `canonical_json` writes for the items `WorldState._canonical_data` makes of the entities.
    /// Unsupported for a tainted store and for a value canonical JSON refuses (NaN).
    fn rows_json<'py>(&mut self, py: Python<'py>, version: i64) -> PyResult<Bound<'py, PyDict>> {
        if self.taint.is_some() {
            return Err(Unsupported::new_err("the entity store is tainted"));
        }
        if self.json_version != version {
            self.row_json = Default::default();
            self.json_version = version;
        }
        let rows = &mut self.rows;
        let [agents, facilities, staff, requests, tasks] = &mut self.row_json;
        let documents = PyDict::new(py);
        documents.set_item("agents", table_json(py, &mut rows.agents, agents, version, |row| &row.profile.id)?)?;
        documents.set_item("facilities", table_json(py, &mut rows.facilities, facilities, version, |row| &row.id)?)?;
        documents.set_item("staff", table_json(py, &mut rows.staff, staff, version, |row| &row.id)?)?;
        documents.set_item("support_requests", table_json(py, &mut rows.support_requests, requests, version, |row| &row.id)?)?;
        documents.set_item("active_tasks", table_json(py, &mut rows.active_tasks, tasks, version, |row| &row.id)?)?;
        Ok(documents)
    }

    /// The keys of a table in the order of its dict.
    fn keys(&self, field: &str) -> PyResult<Vec<String>> {
        fn keys_of<R>(table: &Table<R>) -> Vec<String> {
            table.iter().map(|(id, _)| id.to_string()).collect()
        }
        match field {
            "agents" => Ok(keys_of(&self.rows.agents)),
            "facilities" => Ok(keys_of(&self.rows.facilities)),
            "staff" => Ok(keys_of(&self.rows.staff)),
            "support_requests" => Ok(keys_of(&self.rows.support_requests)),
            "active_tasks" => Ok(keys_of(&self.rows.active_tasks)),
            _ => Err(PyValueError::new_err("not a field of entities")),
        }
    }

    #[getter]
    fn tainted(&self) -> Option<String> {
        self.taint.clone()
    }

    /// Counters for the budget of a lane and the soak.
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let stats = PyDict::new(py);
        stats.set_item("forwards", self.forwards)?;
        stats.set_item("attaches", self.attaches)?;
        stats.set_item("detaches", self.detaches)?;
        stats.set_item("taints", self.taints)?;
        stats.set_item("agents", self.rows.agents.len())?;
        stats.set_item("support_requests", self.rows.support_requests.len())?;
        Ok(stats)
    }

    // ---- The sink of a `TrackedList`, which is an ID list of an entity ----------------------

    /// `list.append(value)`.
    fn list_append(
        slf: &Bound<'_, Self>,
        list: &Bound<'_, PyAny>,
        node: u32,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        raw_list_append(list, value)?;
        Self::list_changed(slf, list, node)
    }

    /// The value a list stores for value: an ID list holds str, not nodes.
    fn adopt(&self, _node: &Bound<'_, PyAny>, _key: &Bound<'_, PyAny>, value: Py<PyAny>) -> Py<PyAny> {
        value
    }

    fn release(&self, _value: &Bound<'_, PyAny>) {}

    /// The list changed somehow. An ID list is short, so its row is read again whatever it was.
    #[pyo3(signature = (list, node, _operation, *_arguments))]
    fn forward(
        slf: &Bound<'_, Self>,
        list: &Bound<'_, PyAny>,
        node: u32,
        _operation: &Bound<'_, PyAny>,
        _arguments: &Bound<'_, PyTuple>,
    ) -> PyResult<()> {
        Self::list_changed(slf, list, node)
    }
}

impl EntityStore {
    fn list_changed(slf: &Bound<'_, Self>, list: &Bound<'_, PyAny>, node: u32) -> PyResult<()> {
        let field = (node % LIST_KINDS) as usize;
        let slot = node / LIST_KINDS;
        let (name, kind) = LIST_FIELDS[field];
        Self::write_row(slf, kind, slot, name, list)
    }

    /// Put the keys in the order of the dict, after new rows went to the end.
    fn reorder_keys(
        slf: &Bound<'_, Self>,
        kind: u8,
        items: &[(Bound<'_, PyAny>, Bound<'_, PyAny>)],
    ) -> PyResult<()> {
        fn reorder<R>(
            table: &mut Table<R>,
            items: &[(Bound<'_, PyAny>, Bound<'_, PyAny>)],
        ) -> PyResult<()> {
            let mut ids = IndexMap::with_capacity_and_hasher(items.len(), FxBuildHasher);
            for (key, _) in items {
                let key = key.downcast_exact::<PyString>()?.to_str()?;
                if let Some((id, slot)) = table.ids.get_key_value(key) {
                    ids.insert(id.clone(), *slot);
                }
            }
            table.ids = ids;
            Ok(())
        }
        let mut this = slf.try_borrow_mut()?;
        if this.taint.is_some() {
            return Ok(());
        }
        match kind {
            AGENT => reorder(&mut this.rows.agents, items),
            FACILITY => reorder(&mut this.rows.facilities, items),
            STAFF => reorder(&mut this.rows.staff, items),
            REQUEST => reorder(&mut this.rows.support_requests, items),
            _ => reorder(&mut this.rows.active_tasks, items),
        }
    }

    /// Run f on the rows. f must not run Python code; kernels get Unsupported from a tainted
    /// store, and the Python rules run on the objects.
    pub(crate) fn with_rows<R>(
        slf: &Bound<'_, Self>,
        f: impl FnOnce(&mut Entities) -> PyResult<R>,
    ) -> PyResult<R> {
        let mut this = slf.try_borrow_mut()?;
        if this.taint.is_some() {
            return Err(Unsupported::new_err("the entity store is tainted"));
        }
        f(&mut this.rows)
    }
}

// `world.<name>`, and which table it is.
const FIELDS: [(&str, u8); 5] = [
    ("agents", AGENT),
    ("facilities", FACILITY),
    ("staff", STAFF),
    ("support_requests", REQUEST),
    ("active_tasks", TASK),
];

/// The store of a world, read now: call this again after any Python callback.
pub(crate) fn entities_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, EntityStore>> {
    core_of(world)?.borrow_mut().entities(world.py()).map(|store| store.into_bound(world.py()))
}

/// Store a value a row already took in the attribute of its object: no `__setattr__` runs and
/// nothing is forwarded. A rule writes the row inside `with_rows`, then calls this outside it. A
/// `SupportRequestState` also has writes to report (`_write_listener`): see the lane's document.
pub(crate) fn write_field<'py>(
    world: &Bound<'py, PyAny>,
    field: &str,
    id: &str,
    name: &str,
    value: &Bound<'py, PyAny>,
) -> PyResult<()> {
    let py = world.py();
    let map = world.getattr(field)?;
    let key = PyString::new(py, id);
    let object = raw_dict_get(&map, key.as_any())?
        .ok_or_else(|| Unsupported::new_err("the world's map has no such ID"))?;
    raw_setattr(&object, &PyString::intern(py, name), value.as_ptr())
}

/// The template of a rule that writes an entity: read and write the row, then the object. Used by
/// the tests. Says Unsupported, before any change, for a tainted store or an unknown ID.
#[pyfunction]
fn add_agent_stress(world: &Bound<'_, PyAny>, id: &str, change: f64) -> PyResult<f64> {
    let py = world.py();
    let new = EntityStore::with_rows(&entities_of(world)?, |rows| {
        let row = rows
            .agents
            .get_mut(id)
            .ok_or_else(|| Unsupported::new_err("no such agent"))?;
        row.stress += change;
        Ok(row.stress)
    })?;
    write_field(world, "agents", id, "stress", new.into_pyobject(py)?.as_any())?;
    Ok(new)
}

/// A need of each agent in dict order, read from the rows only.
#[pyfunction]
fn hunger_of_agents(world: &Bound<'_, PyAny>) -> PyResult<Vec<f64>> {
    EntityStore::with_rows(&entities_of(world)?, |rows| {
        Ok(rows.agents.iter().map(|(_, row)| row.needs.hunger).collect())
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<EntityStore>()?;
    m.add_function(wrap_pyfunction!(install, m)?)?;
    m.add_function(wrap_pyfunction!(add_agent_stress, m)?)?;
    m.add_function(wrap_pyfunction!(hunger_of_agents, m)?)?;
    Ok(())
}
