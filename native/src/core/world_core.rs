//! What a world's Rust state hangs on: `WorldCore`, its `NamespaceMirror`s and the `Row` trait
//! (docs/rust-port/lanes/p4-world-core.md).
//!
//! A namespace is owned when Rust holds it in a typed row. Unported Python keeps reading the same
//! name in `world.model_state`, which is then a tree of the tracked containers of
//! `core/state_mirror.py`. A write from Python comes here with the node's number and is applied
//! to the row; a write from Rust goes to the row and then to the tree through the C API, which
//! does not run the containers' overrides. So the row and the tree hold the same values, and
//! `verify` compares their canonical JSON.
//!
//! A write that does not fit the row taints the namespace: the row is dropped, kernels get
//! `Unsupported`, and the Python rules run on the tree, which stays right whatever was written.
//!
//! Rows hold no Python handles, so they can be read without the GIL; the handles are here, in the
//! node table. No `PyRefMut` of a mirror is held across anything that may run Python code: each
//! borrow below covers the table or the row only. A world can move to another core between two
//! calls (`world.__dict__.update` in a copy-and-swap), so a kernel asks `core_of(world)` again
//! after every Python callback.

use std::any::Any;
use std::ptr::null_mut;
use std::sync::{Arc, OnceLock};

use indexmap::IndexMap;
use pyo3::exceptions::PyRuntimeError;
use pyo3::ffi;
use pyo3::gc::PyVisit;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple};
use pyo3::PyTraverseError;
use smallvec::SmallVec;

use super::row::Scalar;
use super::tracked::{tracked_dict_type, tracked_list_type};
use super::value::V;
use crate::{encode, Encoder, Unsupported, MAX_DEPTH};

const NO_PARENT: u32 = u32::MAX;

/// One step of a path from the namespace's root.
#[derive(Clone, Copy, Debug)]
pub(crate) enum Part<'a> {
    Key(&'a str),
    Index(usize),
}

/// A write from Python, at the node a path leads to. Values are the tree's stored values.
pub(crate) enum Write<'a, 'py> {
    Set { key: Part<'a>, value: &'a Bound<'py, PyAny> },
    Delete { key: Part<'a> },
    /// `del items[start:stop]`, of a list.
    DeleteRange { start: usize, stop: usize },
    Clear,
    Append { value: &'a Bound<'py, PyAny> },
    Extend { values: &'a Bound<'py, PyList> },
    Insert { index: usize, value: &'a Bound<'py, PyAny> },
    Reverse,
    /// The list now holds these items: a slice assignment, a sort or `*=`.
    ReplaceAll { items: &'a Bound<'py, PyList> },
}

/// Why a value or a write does not fit a row.
pub(crate) struct Misfit(pub(crate) String);

impl Misfit {
    pub(crate) fn new(reason: impl Into<String>) -> Self {
        Self(reason.into())
    }
}

impl From<PyErr> for Misfit {
    fn from(error: PyErr) -> Self {
        Self(error.to_string())
    }
}

/// A namespace's state as Rust holds it. A lane implements this in `state/<name>.rs`;
/// `state/sample_namespace.rs` is the template.
pub(crate) trait Row: Send + Sync + 'static {
    /// Apply a write from Python. Every part of a row is checked before the first change is made
    /// or the misfit drops the whole row, so a half-applied write can't be read.
    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit>;
    /// New plain containers holding the state, in the order of the tree.
    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>>;
    /// The canonical JSON of the state, the same bytes as `canonical_json` of the tree.
    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()>;
    fn as_any_mut(&mut self) -> &mut dyn Any;
}

/// Reads a whole namespace, or says it does not fit.
pub(crate) type RowLoader = fn(&Bound<'_, PyAny>) -> Result<Box<dyn Row>, Misfit>;

/// A row's loader as a Python object: `native.state("name").ROW`.
#[pyclass(frozen, module = "sheltersim_native")]
pub struct RowType {
    name: &'static str,
    load: RowLoader,
}

impl RowType {
    pub(crate) fn new(name: &'static str, load: RowLoader) -> Self {
        Self { name, load }
    }
}

#[pymethods]
impl RowType {
    fn __repr__(&self) -> String {
        format!("<RowType {}>", self.name)
    }
}

/// A row that is a `V`, for a namespace without typed fields: any JSON fits.
pub(crate) struct ValueRow(pub(crate) V);

pub(crate) fn load_value_row(tree: &Bound<'_, PyAny>) -> Result<Box<dyn Row>, Misfit> {
    Ok(Box::new(ValueRow(V::from_py(tree)?)))
}

fn value_at<'v>(root: &'v mut V, path: &[Part<'_>]) -> Result<&'v mut V, Misfit> {
    let mut target = root;
    for part in path {
        target = match (target, part) {
            (V::Map(map), Part::Key(key)) => map
                .get_mut(*key)
                .ok_or_else(|| Misfit::new(format!("no key {key:?}")))?,
            (V::List(items), Part::Index(index)) => items
                .get_mut(*index)
                .ok_or_else(|| Misfit::new(format!("no index {index}")))?,
            _ => return Err(Misfit::new("the path does not lead to a container")),
        };
    }
    Ok(target)
}

/// A forwarded write to a value: `ValueRow`'s, and the free-form part of a typed row's.
pub(crate) fn apply_to_value(
    root: &mut V,
    path: &[Part<'_>],
    write: &Write<'_, '_>,
) -> Result<(), Misfit> {
    let target = value_at(root, path)?;
    match (target, write) {
        (V::Map(map), Write::Set { key: Part::Key(key), value }) => {
            let value = V::from_py(value)?;
            match map.get_mut(*key) {
                Some(slot) => *slot = value,
                None => {
                    map.insert(Arc::from(*key), value);
                }
            }
        }
        (V::Map(map), Write::Delete { key: Part::Key(key) }) => {
            map.shift_remove(*key)
                .ok_or_else(|| Misfit::new(format!("no key {key:?}")))?;
        }
        (V::Map(map), Write::Clear) => map.clear(),
        (V::List(items), Write::Set { key: Part::Index(index), value }) => {
            *items
                .get_mut(*index)
                .ok_or_else(|| Misfit::new(format!("no index {index}")))? = V::from_py(value)?;
        }
        (V::List(items), Write::Delete { key: Part::Index(index) }) if *index < items.len() => {
            items.remove(*index);
        }
        (V::List(items), Write::DeleteRange { start, stop })
            if start <= stop && *stop <= items.len() =>
        {
            items.drain(*start..*stop);
        }
        (V::List(items), Write::Clear) => items.clear(),
        (V::List(items), Write::Append { value }) => items.push(V::from_py(value)?),
        (V::List(items), Write::Extend { values }) => {
            for value in values.iter() {
                items.push(V::from_py(&value)?);
            }
        }
        (V::List(items), Write::Insert { index, value }) if *index <= items.len() => {
            items.insert(*index, V::from_py(value)?);
        }
        (V::List(items), Write::Reverse) => items.reverse(),
        (V::List(items), Write::ReplaceAll { items: new_items }) => {
            *items = new_items
                .iter()
                .map(|value| V::from_py(&value))
                .collect::<PyResult<Vec<V>>>()?;
        }
        _ => return Err(Misfit::new("the write does not fit the value at its path")),
    }
    Ok(())
}

impl Row for ValueRow {
    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        apply_to_value(&mut self.0, path, write)
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.0.to_py(py)
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        self.0.write_json(encoder)
    }

    fn as_any_mut(&mut self) -> &mut dyn Any {
        self
    }
}

enum NodeKey {
    Root,
    Key(Py<PyString>),
    /// Found in the parent list by identity: indexes shift.
    ListItem,
    /// A dict key that is not an exact str. No path reaches below it, so a write there misfits.
    Other,
}

struct Node {
    parent: u32,
    key: NodeKey,
    /// The node, for a list only, whose items are searched. A dict node is reached by its keys.
    list: Option<Py<PyAny>>,
}

#[derive(Default)]
struct NodeTable {
    nodes: Vec<Option<Node>>,
    free: Vec<u32>,
    live: usize,
}

impl NodeTable {
    fn add(&mut self, node: Node) -> u32 {
        self.live += 1;
        if let Some(id) = self.free.pop() {
            self.nodes[id as usize] = Some(node);
            id
        } else {
            self.nodes.push(Some(node));
            (self.nodes.len() - 1) as u32
        }
    }

    fn remove(&mut self, id: u32) {
        if let Some(slot) = self.nodes.get_mut(id as usize) {
            if slot.take().is_some() {
                self.live -= 1;
                self.free.push(id);
            }
        }
    }

    /// The path from the root to a node, or a misfit if none can be written.
    fn path<'a>(&'a self, py: Python<'a>, id: u32) -> Result<SmallVec<[Part<'a>; 8]>, Misfit> {
        let mut path: SmallVec<[Part<'a>; 8]> = SmallVec::new();
        let mut current = id;
        loop {
            let node = self
                .nodes
                .get(current as usize)
                .and_then(Option::as_ref)
                .ok_or_else(|| Misfit::new("a node left the table"))?;
            match &node.key {
                NodeKey::Root => break,
                NodeKey::Key(key) => path.push(Part::Key(key.bind(py).to_str()?)),
                NodeKey::ListItem => path.push(Part::Index(self.index_in_parent(current, node)?)),
                NodeKey::Other => return Err(Misfit::new("a key is not a str")),
            }
            current = node.parent;
        }
        path.reverse();
        Ok(path)
    }

    fn index_in_parent(&self, own: u32, node: &Node) -> Result<usize, Misfit> {
        let parent = self
            .nodes
            .get(node.parent as usize)
            .and_then(Option::as_ref)
            .and_then(|parent| parent.list.as_ref())
            .ok_or_else(|| Misfit::new("a list item without its list"))?;
        // SAFETY: the table holds the list, the GIL is held and nothing here runs Python code.
        unsafe {
            let list = parent.as_ptr();
            let mut index = ffi::PyList_Size(list);
            while index > 0 {
                index -= 1;
                let item = ffi::PyList_GetItem(list, index);
                if node_number_is(item, own) {
                    return Ok(index as usize);
                }
            }
        }
        Err(Misfit::new("a list item is no longer in its list"))
    }
}

/// Whether an item of a tracked list is the node with this number.
///
/// # Safety
/// `item` is a live object, the GIL is held, and reading the slot runs no Python code.
unsafe fn node_number_is(item: *mut ffi::PyObject, id: u32) -> bool {
    if ffi::Py_TYPE(item) != tracked_dict_type() && ffi::Py_TYPE(item) != tracked_list_type() {
        return false;
    }
    let name = node_slot_name();
    let number = ffi::PyObject_GetAttr(item, name);
    if number.is_null() {
        ffi::PyErr_Clear();
        return false;
    }
    let found = ffi::PyLong_AsLong(number);
    ffi::Py_DecRef(number);
    if found == -1 && !ffi::PyErr_Occurred().is_null() {
        ffi::PyErr_Clear();
        return false;
    }
    found == id as std::os::raw::c_long
}

/// The interned name of the node slot, for code with only raw pointers. Never released.
fn node_slot_name() -> *mut ffi::PyObject {
    static NAME: OnceLock<usize> = OnceLock::new();
    // SAFETY: called with the GIL held; a text with no NUL makes a new interned str.
    *NAME.get_or_init(|| unsafe {
        ffi::PyUnicode_InternFromString(c"_node".as_ptr()) as usize
    }) as *mut ffi::PyObject
}

/// A dict of a namespace's tree, for a rule's writes: `with_row_tree` gives the root, `child` goes
/// down, and the setters `state_row!` writes store scalars here. The C API is used, so no override
/// runs and nothing is forwarded. A detached namespace has no tree, and these do nothing.
pub(crate) struct Tree<'py> {
    py: Python<'py>,
    node: Option<Bound<'py, PyAny>>,
}

impl<'py> Tree<'py> {
    pub(crate) fn py(&self) -> Python<'py> {
        self.py
    }

    pub(crate) fn child(&self, key: &Bound<'py, PyString>) -> PyResult<Tree<'py>> {
        let node = match &self.node {
            Some(node) => Some(
                raw_dict_get(node, key.as_any())?
                    .ok_or_else(|| PyRuntimeError::new_err("the tree does not have the row's key"))?,
            ),
            None => None,
        };
        Ok(Tree { py: self.py, node })
    }

    pub(crate) fn child_str(&self, key: &str) -> PyResult<Tree<'py>> {
        self.child(&PyString::new(self.py, key))
    }

    /// d[key] = value. The row's type says the old value and the new one are not containers, so
    /// the table of nodes stays right and no Python code runs: this is allowed inside `with_row`.
    pub(crate) fn set<T: Scalar>(&self, key: &Bound<'py, PyString>, value: &T) -> PyResult<()> {
        match &self.node {
            Some(node) => raw_dict_set(node, key.as_any(), &value.to_py(self.py)?),
            None => Ok(()),
        }
    }

    pub(crate) fn set_str<T: Scalar>(&self, key: &str, value: &T) -> PyResult<()> {
        match &self.node {
            Some(node) => raw_dict_set(node, PyString::new(self.py, key).as_any(), &value.to_py(self.py)?),
            None => Ok(()),
        }
    }
}

/// The state of one owned namespace in one world, and the sink of its tracked nodes.
#[pyclass(module = "sheltersim_native")]
pub struct NamespaceMirror {
    name: Arc<str>,
    row_type: Py<RowType>,
    row: Option<Box<dyn Row>>,
    taint: Option<String>,
    root: Option<Py<PyAny>>,
    table: NodeTable,
    detached: bool,
    forwards: u64,
    whole_writes: u64,
    rebuilds: u64,
}

/// A new empty tracked container of the class given, without its Python `__new__`.
fn new_container<'py>(py: Python<'py>, list: bool) -> PyResult<Bound<'py, PyAny>> {
    type NewFunction = unsafe extern "C" fn(
        *mut ffi::PyTypeObject,
        *mut ffi::PyObject,
        *mut ffi::PyObject,
    ) -> *mut ffi::PyObject;
    static NEW: OnceLock<[usize; 2]> = OnceLock::new();
    let functions = NEW.get_or_init(|| {
        // SAFETY: both built-in types live forever, and tp_new is always set on them.
        unsafe {
            [
                ffi::PyType_GetSlot(std::ptr::addr_of_mut!(ffi::PyDict_Type), ffi::Py_tp_new) as usize,
                ffi::PyType_GetSlot(std::ptr::addr_of_mut!(ffi::PyList_Type), ffi::Py_tp_new) as usize,
            ]
        }
    });
    let kind = if list { tracked_list_type() } else { tracked_dict_type() };
    if kind.is_null() {
        return Err(PyRuntimeError::new_err("core/state_mirror.py is not imported"));
    }
    let arguments = PyTuple::empty(py);
    // SAFETY: the address is tp_new of dict or list, kind is a subclass of that built-in type
    // that adds only slots, and those functions run no Python code.
    unsafe {
        let new = std::mem::transmute::<usize, NewFunction>(functions[list as usize]);
        Bound::from_owned_ptr_or_err(py, new(kind, arguments.as_ptr(), null_mut()))
    }
}

/// A new empty `TrackedList`, out of any tree.
pub(crate) fn new_tracked_list(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    new_container(py, true)
}

fn exact_str_key<'py>(key: &Bound<'py, PyAny>) -> NodeKey {
    match key.downcast_exact::<PyString>() {
        Ok(text) => NodeKey::Key(text.clone().unbind()),
        Err(_) => NodeKey::Other,
    }
}

fn is_atom(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<pyo3::types::PyFloat>()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<pyo3::types::PyInt>()
        || value.is_exact_instance_of::<pyo3::types::PyBool>()
}

pub(crate) fn raw_dict_set(dict: &Bound<'_, PyAny>, key: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<()> {
    // SAFETY: all are live objects; PyDict_SetItem takes its own references, and the type is a
    // dict subclass whose override this call means not to run.
    if unsafe { ffi::PyDict_SetItem(dict.as_ptr(), key.as_ptr(), value.as_ptr()) } != 0 {
        return Err(PyErr::fetch(dict.py()));
    }
    Ok(())
}

pub(crate) fn raw_list_append(list: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<()> {
    // SAFETY: as above.
    if unsafe { ffi::PyList_Append(list.as_ptr(), value.as_ptr()) } != 0 {
        return Err(PyErr::fetch(list.py()));
    }
    Ok(())
}

/// A strong reference to a dict's value, or None; a failed hash is an error.
pub(crate) fn raw_dict_get<'py>(
    dict: &Bound<'py, PyAny>,
    key: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = dict.py();
    // SAFETY: dict is a live dict; the value is borrowed until the new reference below.
    unsafe {
        let value = ffi::PyDict_GetItemWithError(dict.as_ptr(), key.as_ptr());
        if value.is_null() {
            return match PyErr::take(py) {
                Some(error) => Err(error),
                None => Ok(None),
            };
        }
        Ok(Some(Bound::from_borrowed_ptr(py, value)))
    }
}

fn dict_values<'py>(dict: &Bound<'py, PyAny>) -> Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let py = dict.py();
    // SAFETY: dict is a live dict. Keys and values are borrowed for one step, each kept by the
    // new references made in that step, and nothing here runs Python code.
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

fn list_values<'py>(list: &Bound<'py, PyAny>) -> Vec<Bound<'py, PyAny>> {
    let py = list.py();
    // SAFETY: list is a live list, and nothing here runs Python code.
    unsafe {
        let count = ffi::PyList_Size(list.as_ptr());
        (0..count)
            .map(|index| Bound::from_borrowed_ptr(py, ffi::PyList_GetItem(list.as_ptr(), index)))
            .collect()
    }
}

/// Whether value is a tracked container, and of which kind: Some(true) for a list.
#[inline]
fn tracked_kind(value: &Bound<'_, PyAny>) -> Option<bool> {
    // SAFETY: the type pointer is only compared.
    let kind = unsafe { ffi::Py_TYPE(value.as_ptr()) };
    if kind == tracked_dict_type() {
        Some(false)
    } else if kind == tracked_list_type() {
        Some(true)
    } else {
        None
    }
}

impl NamespaceMirror {
    fn add_node(
        slf: &Bound<'_, Self>,
        node: &Bound<'_, PyAny>,
        parent: u32,
        key: NodeKey,
        list: bool,
    ) -> PyResult<()> {
        let py = slf.py();
        let handle = list.then(|| node.clone().unbind());
        let id = slf.try_borrow_mut()?.table.add(Node { parent, key, list: handle });
        node.setattr(intern!(py, "_sink"), slf)?;
        node.setattr(intern!(py, "_node"), id)?;
        Ok(())
    }

    /// What the tree stores for value: an atom as it is and a container as nodes. Any other value
    /// is stored as it is; a row can't read it, so a namespace holding one is tainted.
    fn adopt_value<'py>(
        slf: &Bound<'py, Self>,
        parent: u32,
        key: NodeKey,
        value: &Bound<'py, PyAny>,
        depth: usize,
    ) -> PyResult<Bound<'py, PyAny>> {
        if is_atom(value) || depth > MAX_DEPTH {
            return Ok(value.clone());
        }
        let py = value.py();
        let tracked = tracked_kind(value);
        // SAFETY: the type pointer is only compared.
        let kind = unsafe { ffi::Py_TYPE(value.as_ptr()) };
        let is_list = tracked == Some(true) || kind == std::ptr::addr_of_mut!(ffi::PyList_Type);
        let is_dict = tracked == Some(false) || kind == std::ptr::addr_of_mut!(ffi::PyDict_Type);
        if !is_list && !is_dict {
            return Ok(value.clone());
        }
        // A free node (out of any tree) moves in as itself. A node already in a tree, of this
        // namespace or another, is copied, so a node has one place: two names of one object can
        // not outlive a JSON copy-and-swap anyway, so no code can depend on them.
        let moved = tracked.is_some() && value.getattr(intern!(py, "_sink"))?.is_none();
        let node = if moved { value.clone() } else { new_container(py, is_list)? };
        Self::add_node(slf, &node, parent, key, is_list)?;
        let id: u32 = node.getattr(intern!(py, "_node"))?.extract()?;
        if is_dict {
            for (child_key, child) in dict_values(value) {
                let stored = Self::adopt_value(slf, id, exact_str_key(&child_key), &child, depth + 1)?;
                if !moved || !stored.is(&child) {
                    raw_dict_set(&node, &child_key, &stored)?;
                }
            }
        } else {
            for (index, child) in list_values(value).into_iter().enumerate() {
                let stored = Self::adopt_value(slf, id, NodeKey::ListItem, &child, depth + 1)?;
                if !moved {
                    raw_list_append(&node, &stored)?;
                } else if !stored.is(&child) {
                    // SAFETY: index is within the list; PyList_SetItem steals the new reference.
                    if unsafe { ffi::PyList_SetItem(node.as_ptr(), index as _, stored.into_ptr()) } != 0 {
                        return Err(PyErr::fetch(py));
                    }
                }
            }
        }
        Ok(node)
    }

    /// Take value and everything under it out of the table. It stays a working container: its
    /// later writes are forwarded to `next_sink`, or to nobody if that is None, like the plain
    /// dict it would be.
    fn release_value(
        slf: &Bound<'_, Self>,
        value: &Bound<'_, PyAny>,
        next_sink: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let Some(is_list) = tracked_kind(value) else {
            return Ok(());
        };
        let py = value.py();
        if !value.getattr(intern!(py, "_sink"))?.is(slf) {
            return Ok(());
        }
        let id: u32 = value.getattr(intern!(py, "_node"))?.extract()?;
        slf.try_borrow_mut()?.table.remove(id);
        value.setattr(intern!(py, "_sink"), next_sink)?;
        value.setattr(intern!(py, "_node"), py.None())?;
        if is_list {
            for child in list_values(value) {
                Self::release_value(slf, &child, next_sink)?;
            }
        } else {
            for (_, child) in dict_values(value) {
                Self::release_value(slf, &child, next_sink)?;
            }
        }
        Ok(())
    }

    fn apply(slf: &Bound<'_, Self>, id: u32, write: &Write<'_, '_>) -> PyResult<()> {
        let py = slf.py();
        let mut this = slf.try_borrow_mut()?;
        let this = &mut *this;
        this.forwards += 1;
        let Some(row) = this.row.as_mut() else {
            return Ok(());
        };
        let result = this.table.path(py, id).and_then(|path| row.apply(&path, write));
        if let Err(misfit) = result {
            this.row = None;
            this.taint = Some(misfit.0);
        }
        Ok(())
    }

    fn set_root(slf: &Bound<'_, Self>, value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let root = Self::adopt_value(slf, NO_PARENT, NodeKey::Root, value, 0)?;
        let load = slf.try_borrow()?.row_type.get().load;
        let row = load(&root);
        let mut this = slf.try_borrow_mut()?;
        match row {
            Ok(row) => {
                this.row = Some(row);
                this.taint = None;
            }
            Err(misfit) => {
                this.row = None;
                this.taint = Some(misfit.0);
            }
        }
        this.detached = false;
        this.root = Some(root.clone().unbind());
        drop(this);
        Ok(root.unbind())
    }

    fn drop_root(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        let old = slf.try_borrow_mut()?.root.take();
        if let Some(old) = old {
            Self::release_value(slf, old.bind(py), py.None().bind(py))?;
        }
        Ok(())
    }

    /// Write a value into the tree from Rust, after the row took it: no override runs and nothing
    /// is forwarded. A detached namespace has no tree to write. `path` leads to a dict.
    pub(crate) fn write_through<'py>(
        slf: &Bound<'py, Self>,
        path: &[&str],
        key: &str,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        let Some(mut node) = slf.try_borrow()?.root.as_ref().map(|root| root.bind(py).clone()) else {
            return Ok(());
        };
        for part in path {
            let part = PyString::new(py, part);
            node = raw_dict_get(&node, part.as_any())?
                .ok_or_else(|| Unsupported::new_err("the tree has no such path"))?;
        }
        let key = PyString::new(py, key);
        Self::store(slf, &node, key.as_any(), value)
    }

    /// d[key] = value in the tree, keeping the table right, without touching the row.
    fn store<'py>(
        slf: &Bound<'py, Self>,
        node: &Bound<'py, PyAny>,
        key: &Bound<'py, PyAny>,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        let old = raw_dict_get(node, key)?;
        if old.as_ref().is_some_and(|old| old.is(value)) {
            return Ok(());
        }
        let id: u32 = node.getattr(intern!(py, "_node"))?.extract()?;
        let stored = Self::adopt_value(slf, id, exact_str_key(key), value, 0)?;
        raw_dict_set(node, key, &stored)?;
        if let Some(old) = old {
            Self::release_value(slf, &old, py.None().bind(py))?;
        }
        Ok(())
    }

    /// Run f on the row of type T. f must not run Python code; kernels get Unsupported from a
    /// tainted or absent namespace, and the Python rules run on the tree.
    pub(crate) fn with_row<T: Row, R>(
        slf: &Bound<'_, Self>,
        f: impl FnOnce(&mut T) -> PyResult<R>,
    ) -> PyResult<R> {
        let mut this = slf.try_borrow_mut()?;
        let row = this
            .row
            .as_mut()
            .ok_or_else(|| Unsupported::new_err("the namespace has no row"))?;
        let row = row
            .as_any_mut()
            .downcast_mut::<T>()
            .ok_or_else(|| Unsupported::new_err("the namespace has another row"))?;
        f(row)
    }

    /// with_row, with the root of the tree for mirrored setters. See `Tree::set`.
    pub(crate) fn with_row_tree<'py, T: Row, R>(
        slf: &Bound<'py, Self>,
        f: impl FnOnce(&mut T, &Tree<'py>) -> PyResult<R>,
    ) -> PyResult<R> {
        let py = slf.py();
        let mut this = slf.try_borrow_mut()?;
        let tree = Tree { py, node: this.root.as_ref().map(|root| root.bind(py).clone()) };
        let row = this
            .row
            .as_mut()
            .ok_or_else(|| Unsupported::new_err("the namespace has no row"))?;
        let row = row
            .as_any_mut()
            .downcast_mut::<T>()
            .ok_or_else(|| Unsupported::new_err("the namespace has another row"))?;
        f(row, &tree)
    }

    /// Whether value is the root of this mirror's tree.
    pub(crate) fn root_is(&self, value: &Bound<'_, PyAny>) -> bool {
        self.root.as_ref().is_some_and(|root| root.as_ptr() == value.as_ptr())
    }
}

fn part_of<'a>(key: &'a Bound<'_, PyAny>) -> Result<Part<'a>, Misfit> {
    let text = key
        .downcast_exact::<PyString>()
        .map_err(|_| Misfit::new("a key is not a str"))?;
    Ok(Part::Key(text.to_str()?))
}

#[pymethods]
impl NamespaceMirror {
    /// d[key] = value on a bound TrackedDict: what stores it also forwards it.
    fn dict_set(
        slf: &Bound<'_, Self>,
        node: &Bound<'_, PyAny>,
        id: u32,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        // A key that can't be hashed fails here, with dict's own error, before any change.
        let old = raw_dict_get(node, key)?;
        if old.as_ref().is_some_and(|old| old.is(value)) {
            // Reassigning what a key holds changes nothing, and is common after an edit in place.
            return Ok(());
        }
        let stored = Self::adopt_value(slf, id, exact_str_key(key), value, 0)?;
        raw_dict_set(node, key, &stored)?;
        if let Some(old) = old {
            Self::release_value(slf, &old, py.None().bind(py))?;
        }
        match part_of(key) {
            Ok(part) => Self::apply(slf, id, &Write::Set { key: part, value: &stored }),
            Err(misfit) => {
                let mut this = slf.try_borrow_mut()?;
                this.row = None;
                this.taint = Some(misfit.0);
                Ok(())
            }
        }
    }

    /// items.append(value) on a bound TrackedList.
    fn list_append(
        slf: &Bound<'_, Self>,
        node: &Bound<'_, PyAny>,
        id: u32,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let stored = Self::adopt_value(slf, id, NodeKey::ListItem, value, 0)?;
        raw_list_append(node, &stored)?;
        Self::apply(slf, id, &Write::Append { value: &stored })
    }

    /// What a container stores for value, for the mutators written in Python. key is None for a
    /// list item.
    fn adopt<'py>(
        slf: &Bound<'py, Self>,
        id: u32,
        key: &Bound<'py, PyAny>,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let key = if key.is_none() { NodeKey::ListItem } else { exact_str_key(key) };
        Self::adopt_value(slf, id, key, value, 0)
    }

    /// Value left the tree and is an ordinary container now.
    fn release(slf: &Bound<'_, Self>, value: &Bound<'_, PyAny>) -> PyResult<()> {
        Self::release_value(slf, value, slf.py().None().bind(slf.py()))
    }

    /// A write the container made itself: `set` and `del` take the key or index, `del_range`
    /// start and stop, `extend` the stored items, `insert` index and stored item. `slice_set` and
    /// `replace_all` say the list's items changed, and read them from node.
    #[pyo3(signature = (node, id, op, *arguments))]
    fn forward(
        slf: &Bound<'_, Self>,
        node: &Bound<'_, PyAny>,
        id: u32,
        op: &str,
        arguments: &Bound<'_, PyTuple>,
    ) -> PyResult<()> {
        let argument = |index: usize| arguments.get_item(index);
        match op {
            "set" | "del" => {
                let key_object = argument(0)?;
                let part = if node.is_instance_of::<PyList>() {
                    key_object
                        .extract::<usize>()
                        .map(Part::Index)
                        .map_err(|_| Misfit::new("not an index"))
                } else {
                    part_of(&key_object)
                };
                match part {
                    Ok(key) if op == "set" => {
                        Self::apply(slf, id, &Write::Set { key, value: &argument(1)? })
                    }
                    Ok(key) => Self::apply(slf, id, &Write::Delete { key }),
                    Err(misfit) => {
                        let mut this = slf.try_borrow_mut()?;
                        this.row = None;
                        this.taint = Some(misfit.0);
                        Ok(())
                    }
                }
            }
            "del_range" => {
                let (start, stop) = (argument(0)?.extract()?, argument(1)?.extract()?);
                Self::apply(slf, id, &Write::DeleteRange { start, stop })
            }
            "clear" => Self::apply(slf, id, &Write::Clear),
            "extend" => {
                let values = argument(0)?;
                Self::apply(slf, id, &Write::Extend { values: values.downcast::<PyList>()? })
            }
            "insert" => {
                let index = argument(0)?.extract()?;
                Self::apply(slf, id, &Write::Insert { index, value: &argument(1)? })
            }
            "reverse" => Self::apply(slf, id, &Write::Reverse),
            "slice_set" | "replace_all" => {
                let items = node.downcast::<PyList>()?;
                Self::apply(slf, id, &Write::ReplaceAll { items })
            }
            other => Err(pyo3::exceptions::PyValueError::new_err(format!("unknown write {other:?}"))),
        }
    }

    /// Own value as the namespace's new tree and return that tree: at attach, and for a write of
    /// the whole namespace. A tree that does not fit is stored and the namespace is tainted.
    fn replace_root(slf: &Bound<'_, Self>, value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        Self::drop_root(slf)?;
        let root = Self::set_root(slf, value)?;
        slf.try_borrow_mut()?.whole_writes += 1;
        Ok(root)
    }

    /// The namespace left model_state.
    fn clear_root(slf: &Bound<'_, Self>) -> PyResult<()> {
        Self::drop_root(slf)?;
        let mut this = slf.try_borrow_mut()?;
        this.row = None;
        this.taint = None;
        this.detached = false;
        Ok(())
    }

    /// Drop the tree: the row alone holds the state, and a later Python read calls rebuild. Nodes
    /// still named somewhere get dead_sink, which raises on a write, because a write to one could
    /// reach neither the row nor a new tree. Not possible for a tainted namespace, whose only
    /// state is the tree. Returns whether it detached.
    fn detach(slf: &Bound<'_, Self>, dead_sink: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py = slf.py();
        if slf.try_borrow()?.row.is_none() {
            return Ok(false);
        }
        let root = slf.try_borrow_mut()?.root.take();
        if let Some(root) = root {
            Self::release_value(slf, root.bind(py), dead_sink)?;
        }
        slf.try_borrow_mut()?.detached = true;
        Ok(true)
    }

    /// A new tree from the row, after a detach.
    fn rebuild(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let plain = {
            let this = slf.try_borrow()?;
            let Some(row) = this.row.as_ref() else {
                return Err(PyRuntimeError::new_err("a detached namespace always has a row"));
            };
            row.to_py(py)?
        };
        let root = Self::adopt_value(slf, NO_PARENT, NodeKey::Root, &plain, 0)?;
        let mut this = slf.try_borrow_mut()?;
        this.root = Some(root.clone().unbind());
        this.detached = false;
        this.rebuilds += 1;
        Ok(root.unbind())
    }

    /// Drop the row, as a write that does not fit does.
    fn taint(&mut self, reason: String) {
        self.row = None;
        self.taint = Some(reason);
    }

    /// Why the namespace has no row, or None.
    #[getter]
    fn tainted(&self) -> Option<String> {
        self.taint.clone()
    }

    #[getter]
    fn has_row(&self) -> bool {
        self.row.is_some()
    }

    #[getter]
    fn is_detached(&self) -> bool {
        self.detached
    }

    /// Read the tree into a new row, for a namespace whose misfit is gone. Returns whether it fits.
    fn refit(slf: &Bound<'_, Self>) -> PyResult<bool> {
        let py = slf.py();
        let (root, load) = {
            let this = slf.try_borrow()?;
            (this.root.as_ref().map(|root| root.clone_ref(py)), this.row_type.get().load)
        };
        let Some(root) = root else {
            return Ok(false);
        };
        let row = load(root.bind(py));
        let mut this = slf.try_borrow_mut()?;
        match row {
            Ok(row) => {
                this.row = Some(row);
                this.taint = None;
                Ok(true)
            }
            Err(misfit) => {
                this.taint = Some(misfit.0);
                Ok(false)
            }
        }
    }

    /// The canonical JSON of the row, or None without one.
    fn row_json<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyBytes>>> {
        let Some(row) = self.row.as_ref() else {
            return Ok(None);
        };
        let mut encoder = Encoder { out: Vec::with_capacity(256) };
        row.write_json(&mut encoder)?;
        Ok(Some(PyBytes::new(py, &encoder.out)))
    }

    /// Whether the row and the tree have the same canonical JSON; true when one of them is
    /// missing, since there is nothing to differ.
    fn verify(slf: &Bound<'_, Self>) -> PyResult<bool> {
        let py = slf.py();
        let (row_bytes, root) = {
            let this = slf.try_borrow()?;
            let Some(row) = this.row.as_ref() else {
                return Ok(true);
            };
            let mut encoder = Encoder { out: Vec::with_capacity(256) };
            row.write_json(&mut encoder)?;
            (encoder.out, this.root.as_ref().map(|root| root.clone_ref(py)))
        };
        let Some(root) = root else {
            return Ok(true);
        };
        Ok(encode(root.bind(py))? == row_bytes)
    }

    /// Counters for the budget of a lane.
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let stats = PyDict::new(py);
        stats.set_item("namespace", &*self.name)?;
        stats.set_item("forwards", self.forwards)?;
        stats.set_item("whole_writes", self.whole_writes)?;
        stats.set_item("rebuilds", self.rebuilds)?;
        stats.set_item("live_nodes", self.table.live)?;
        Ok(stats)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.row_type)?;
        visit.call(&self.root)?;
        for node in self.table.nodes.iter().flatten() {
            visit.call(&node.list)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.root = None;
        self.row = None;
        self.table = NodeTable::default();
    }
}

/// A world's Rust state: `world.__dict__["_core"]`. It holds no reference to the world, so
/// `world.__dict__.clear()/update()` moves it with the state it belongs to.
#[pyclass(module = "sheltersim_native")]
pub struct WorldCore {
    namespaces: IndexMap<Arc<str>, Py<NamespaceMirror>>,
    entities: Option<Py<crate::state::entities::EntityStore>>,
}

#[pymethods]
impl WorldCore {
    #[new]
    fn new() -> Self {
        Self { namespaces: IndexMap::new(), entities: None }
    }

    /// The rows of the world's entities (state/entities.rs).
    pub(crate) fn entities(&mut self, py: Python<'_>) -> PyResult<Py<crate::state::entities::EntityStore>> {
        if let Some(store) = &self.entities {
            return Ok(store.clone_ref(py));
        }
        let store = Py::new(py, crate::state::entities::EntityStore::default())?;
        self.entities = Some(store.clone_ref(py));
        Ok(store)
    }

    /// Say the namespace is owned, with a row of this type, and return its mirror. It has no
    /// state until `replace_root`.
    fn own(&mut self, py: Python<'_>, name: &str, row_type: Py<RowType>) -> PyResult<Py<NamespaceMirror>> {
        if self.namespaces.contains_key(name) {
            return Err(pyo3::exceptions::PyValueError::new_err(format!("{name} is owned already")));
        }
        let name: Arc<str> = Arc::from(name);
        let mirror = Py::new(
            py,
            NamespaceMirror {
                name: name.clone(),
                row_type,
                row: None,
                taint: None,
                root: None,
                table: NodeTable::default(),
                detached: false,
                forwards: 0,
                whole_writes: 0,
                rebuilds: 0,
            },
        )?;
        self.namespaces.insert(name, mirror.clone_ref(py));
        Ok(mirror)
    }

    /// The mirror of an owned namespace, or None.
    fn mirror(&self, py: Python<'_>, name: &str) -> Option<Py<NamespaceMirror>> {
        self.namespaces.get(name).map(|mirror| mirror.clone_ref(py))
    }

    fn owned_names(&self) -> Vec<String> {
        self.namespaces.keys().map(|name| name.to_string()).collect()
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for mirror in self.namespaces.values() {
            visit.call(mirror)?;
        }
        visit.call(&self.entities)?;
        Ok(())
    }

    fn __clear__(&mut self) {
        self.namespaces.clear();
        self.entities = None;
    }
}

/// The core of a world, read now: call this again after any Python callback, because a
/// copy-and-swap in the callback moves the world to another core.
pub(crate) fn core_of<'py>(world: &Bound<'py, PyAny>) -> PyResult<Bound<'py, WorldCore>> {
    let core = world
        .getattr(intern!(world.py(), "_core"))
        .map_err(|_| Unsupported::new_err("the world has no core"))?;
    Ok(core.downcast::<WorldCore>()?.clone())
}

/// The mirror of a namespace of a world, read now, or Unsupported if it is not owned.
pub(crate) fn mirror_of<'py>(world: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, NamespaceMirror>> {
    let core = core_of(world)?;
    let found = core.try_borrow()?.namespaces.get(name).map(|mirror| mirror.clone_ref(world.py()));
    found
        .map(|mirror| mirror.into_bound(world.py()))
        .ok_or_else(|| Unsupported::new_err("the namespace is not owned"))
}

/// The mirror of an owned world field such as `metric_state`, or Unsupported if it is not owned or
/// the field holds something else now. `WorldState.__setattr__` catches a whole assignment to
/// `model_state` only; a field assigned whole simply stops being Rust's, and the Python rules run.
pub(crate) fn field_mirror_of<'py>(
    world: &Bound<'py, PyAny>,
    name: &Bound<'py, PyString>,
) -> PyResult<Bound<'py, NamespaceMirror>> {
    let mirror = mirror_of(world, name.to_str()?)?;
    let current = world.getattr(name)?;
    if !mirror.try_borrow()?.root_is(&current) {
        return Err(Unsupported::new_err("the field holds another dict"));
    }
    Ok(mirror)
}
