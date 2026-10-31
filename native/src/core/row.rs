//! Typed rows: `Field`, what a value of a row can do, and `state_row!`, which writes it for a
//! record from its list of fields (docs/rust-port/lanes/p5-template-namespace.md).
//!
//! ```ignore
//! state_row! {
//!     #[derive(Clone, Copy)]
//!     pub(crate) struct AgentRecord {
//!         level: f64 => set_level,   // a scalar field with a mirrored setter
//!         visits: i64,               // a scalar field Rust does not write
//!     }
//! }
//! ```
//!
//! Any `Field` is a `Row`, so a namespace is a `state_row!` struct or a map of them, and its loader
//! is `load_row::<ThatType>`. The macro writes, for the struct:
//! - `from_py`: the fields and nothing else, each of its exact type; anything else is a `Misfit`,
//!   which taints the namespace.
//! - `to_py`: a new plain dict, keys in the order they are listed. List them in the tree's order.
//! - `write_json`: the bytes of `canonical_json` of the tree, keys sorted by bytes.
//! - `apply`: a forwarded write. One to a field reads that field again, and one below a field goes
//!   to the field's own `apply`. Deleting a field, or writing a field the struct does not have,
//!   is a `Misfit`.
//! - `set_level(&mut self, tree, value)`: the mirrored setter a rule uses: the row and the tree, or
//!   neither when the value has the same bits. Only a `Scalar` field can have one, because storing
//!   a scalar over a scalar leaves the tree's table of nodes as it is, and so the setter can run
//!   inside `NamespaceMirror::with_row_tree`.

use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};
use pyo3::IntoPyObjectExt;
use rustc_hash::FxBuildHasher;

use super::tracked::{as_readable_dict, as_readable_list};
use super::value::V;
use super::world_core::{apply_to_value, Misfit, Part, Row, Tree, Write};
use crate::{write_json_string, Encoder};

/// A map keyed by id: insertion order like a dict's, so what iterates it is deterministic.
pub(crate) type IdMap<T> = IndexMap<Arc<str>, T, FxBuildHasher>;

/// A value that can be part of a row.
pub(crate) trait Field: Sized + Send + Sync + 'static {
    /// The value, if it is exactly of this type. Runs no Python code.
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit>;
    /// A new plain value for the tree.
    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>>;
    /// The bytes of `canonical_json`.
    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()>;
    /// A forwarded write inside this value. A scalar has no inside.
    fn apply(&mut self, _path: &[Part<'_>], _write: &Write<'_, '_>) -> Result<(), Misfit> {
        Err(Misfit::new("the write has no field in the row"))
    }
}

impl<T: Field> Row for T {
    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        Field::apply(self, path, write)
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Field::to_py(self, py)
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        Field::write_json(self, encoder)
    }

    fn as_any_mut(&mut self) -> &mut dyn std::any::Any {
        self
    }
}

/// The loader of a namespace whose row is this type: `RowType::new("name", load_row::<ThatType>)`.
pub(crate) fn load_row<T: Field>(tree: &Bound<'_, PyAny>) -> Result<Box<dyn Row>, Misfit> {
    Ok(Box::new(T::from_py(tree)?))
}

/// A field a mirrored setter can store in the tree as it is: never a container.
pub(crate) trait Scalar: Field {
    /// Whether writing other would change nothing in the canonical JSON.
    fn same(&self, other: &Self) -> bool;
}

impl Field for f64 {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        Ok(value
            .downcast_exact::<PyFloat>()
            .map_err(|_| Misfit::new("not a float"))?
            .value())
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(PyFloat::new(py, *self).into_any())
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        encoder.float(*self)
    }
}

impl Scalar for f64 {
    fn same(&self, other: &Self) -> bool {
        self.to_bits() == other.to_bits()
    }
}

impl Field for i64 {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        // bool is an int subclass, so the exact check turns it down too.
        Ok(value
            .downcast_exact::<PyInt>()
            .map_err(|_| Misfit::new("not an int"))?
            .extract()?)
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.into_bound_py_any(py)
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        encoder
            .out
            .extend_from_slice(itoa::Buffer::new().format(*self).as_bytes());
        Ok(())
    }
}

impl Scalar for i64 {
    fn same(&self, other: &Self) -> bool {
        self == other
    }
}

impl Field for bool {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        Ok(value
            .downcast_exact::<PyBool>()
            .map_err(|_| Misfit::new("not a bool"))?
            .is_true())
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.into_bound_py_any(py)
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        encoder
            .out
            .extend_from_slice(if *self { b"true" } else { b"false" });
        Ok(())
    }
}

impl Scalar for bool {
    fn same(&self, other: &Self) -> bool {
        self == other
    }
}

impl Field for Arc<str> {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        Ok(Arc::from(
            value
                .downcast_exact::<PyString>()
                .map_err(|_| Misfit::new("not a str"))?
                .to_str()?,
        ))
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(PyString::new(py, self).into_any())
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        write_json_string(&mut encoder.out, self);
        Ok(())
    }
}

impl Scalar for Arc<str> {
    fn same(&self, other: &Self) -> bool {
        self == other
    }
}

impl<T: Field> Field for Option<T> {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        if value.is_none() {
            return Ok(None);
        }
        Ok(Some(T::from_py(value)?))
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        match self {
            Some(value) => value.to_py(py),
            None => Ok(py.None().into_bound(py)),
        }
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        match self {
            Some(value) => value.write_json(encoder),
            None => {
                encoder.out.extend_from_slice(b"null");
                Ok(())
            }
        }
    }

    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        match self {
            Some(value) => value.apply(path, write),
            None => Err(Misfit::new("the write is inside a None")),
        }
    }
}

impl<T: Scalar> Scalar for Option<T> {
    fn same(&self, other: &Self) -> bool {
        match (self, other) {
            (Some(first), Some(second)) => first.same(second),
            (None, None) => true,
            _ => false,
        }
    }
}

/// The free-form part of a row: takes any JSON, and any write that fits it.
impl Field for V {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        Ok(V::from_py(value)?)
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        V::to_py(self, py)
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        V::write_json(self, encoder)
    }

    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        apply_to_value(self, path, write)
    }
}

impl<T: Field> Field for IdMap<T> {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        let dict = as_readable_dict(value).ok_or_else(|| Misfit::new("not a dict"))?;
        let mut map = IdMap::with_capacity_and_hasher(dict.len(), FxBuildHasher);
        for (key, item) in dict.iter() {
            let key = key
                .downcast_exact::<PyString>()
                .map_err(|_| Misfit::new("not a str key"))?;
            map.insert(Arc::from(key.to_str()?), T::from_py(&item)?);
        }
        Ok(map)
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let dict = PyDict::new(py);
        for (key, value) in self {
            dict.set_item(&**key, value.to_py(py)?)?;
        }
        Ok(dict.into_any())
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        let mut entries: Vec<(&Arc<str>, &T)> = self.iter().collect();
        // Python orders str by code point, the same as UTF-8 bytes.
        entries.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
        encoder.out.push(b'{');
        for (index, (key, value)) in entries.into_iter().enumerate() {
            if index > 0 {
                encoder.out.push(b',');
            }
            write_json_string(&mut encoder.out, key);
            encoder.out.push(b':');
            value.write_json(encoder)?;
        }
        encoder.out.push(b'}');
        Ok(())
    }

    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        match (path, write) {
            ([], Write::Set { key: Part::Key(key), value }) => {
                let value = T::from_py(value)?;
                match self.get_mut(*key) {
                    Some(slot) => *slot = value,
                    None => {
                        self.insert(Arc::from(*key), value);
                    }
                }
                Ok(())
            }
            ([], Write::Delete { key: Part::Key(key) }) => {
                self.shift_remove(*key)
                    .map(|_| ())
                    .ok_or_else(|| Misfit::new(format!("no key {key:?}")))
            }
            ([], Write::Clear) => {
                self.clear();
                Ok(())
            }
            ([Part::Key(key), rest @ ..], write) => self
                .get_mut(*key)
                .ok_or_else(|| Misfit::new(format!("no key {key:?}")))?
                .apply(rest, write),
            _ => Err(Misfit::new("the write does not fit a map")),
        }
    }
}

impl<T: Field> Field for Vec<T> {
    fn from_py(value: &Bound<'_, PyAny>) -> Result<Self, Misfit> {
        let list = as_readable_list(value).ok_or_else(|| Misfit::new("not a list"))?;
        list.iter().map(|item| T::from_py(&item)).collect()
    }

    fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let list = PyList::empty(py);
        for item in self {
            list.append(item.to_py(py)?)?;
        }
        Ok(list.into_any())
    }

    fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        encoder.out.push(b'[');
        for (index, item) in self.iter().enumerate() {
            if index > 0 {
                encoder.out.push(b',');
            }
            item.write_json(encoder)?;
        }
        encoder.out.push(b']');
        Ok(())
    }

    fn apply(&mut self, path: &[Part<'_>], write: &Write<'_, '_>) -> Result<(), Misfit> {
        match (path, write) {
            ([], Write::Set { key: Part::Index(index), value }) if *index < self.len() => {
                self[*index] = T::from_py(value)?;
            }
            ([], Write::Delete { key: Part::Index(index) }) if *index < self.len() => {
                self.remove(*index);
            }
            ([], Write::DeleteRange { start, stop }) if start <= stop && *stop <= self.len() => {
                self.drain(*start..*stop);
            }
            ([], Write::Clear) => self.clear(),
            ([], Write::Append { value }) => self.push(T::from_py(value)?),
            ([], Write::Extend { values }) => {
                for value in values.iter() {
                    self.push(T::from_py(&value)?);
                }
            }
            ([], Write::Insert { index, value }) if *index <= self.len() => {
                self.insert(*index, T::from_py(value)?);
            }
            ([], Write::Reverse) => self.reverse(),
            ([], Write::ReplaceAll { items }) => {
                *self = items
                    .iter()
                    .map(|item| T::from_py(&item))
                    .collect::<Result<Vec<T>, Misfit>>()?;
            }
            ([Part::Index(index), rest @ ..], write) => {
                return self
                    .get_mut(*index)
                    .ok_or_else(|| Misfit::new(format!("no index {index}")))?
                    .apply(rest, write);
            }
            _ => return Err(Misfit::new("the write does not fit a list")),
        }
        Ok(())
    }
}

/// A mirrored write into a map of scalars: the row and the tree, or neither for the same bits.
pub(crate) fn map_set<'py, T: Scalar>(
    map: &mut IdMap<T>,
    tree: &Tree<'py>,
    key: &str,
    value: T,
) -> PyResult<()> {
    match map.get_mut(key) {
        Some(slot) if slot.same(&value) => {}
        Some(slot) => {
            tree.set_str(key, &value)?;
            *slot = value;
        }
        None => {
            tree.set_str(key, &value)?;
            map.insert(Arc::from(key), value);
        }
    }
    Ok(())
}

/// The order that sorts names by their bytes, computed at compile time for `write_json`.
pub(crate) const fn sorted_order<const N: usize>(names: &[&str]) -> [usize; N] {
    const fn before(first: &[u8], second: &[u8]) -> bool {
        let mut index = 0;
        while index < first.len() && index < second.len() {
            if first[index] != second[index] {
                return first[index] < second[index];
            }
            index += 1;
        }
        first.len() < second.len()
    }
    let mut order = [0; N];
    let mut index = 0;
    while index < N {
        order[index] = index;
        index += 1;
    }
    let mut done = 1;
    while done < N {
        let mut place = done;
        while place > 0 && before(names[order[place]].as_bytes(), names[order[place - 1]].as_bytes()) {
            let moved = order[place];
            order[place] = order[place - 1];
            order[place - 1] = moved;
            place -= 1;
        }
        done += 1;
    }
    order
}

macro_rules! state_row {
    (
        $(#[$meta:meta])*
        $vis:vis struct $name:ident {
            $( $field:ident : $kind:ty $(=> $setter:ident)? ),* $(,)?
        }
    ) => {
        $(#[$meta])*
        $vis struct $name {
            $( pub(crate) $field: $kind, )*
        }

        impl $name {
            const NAMES: &'static [&'static str] = &[$(stringify!($field)),*];
            const JSON_ORDER: [usize; $name::NAMES.len()] =
                $crate::core::row::sorted_order($name::NAMES);
            const JSON_WRITERS: &'static [fn(&Self, &mut $crate::Encoder) -> pyo3::PyResult<()>] =
                &[$(|row, encoder| $crate::core::row::Field::write_json(&row.$field, encoder)),*];

            $($(
                /// The row and the tree, or neither when the value has the same bits.
                pub(crate) fn $setter<'py>(
                    &mut self,
                    tree: &$crate::core::world_core::Tree<'py>,
                    value: $kind,
                ) -> pyo3::PyResult<()> {
                    if !$crate::core::row::Scalar::same(&self.$field, &value) {
                        tree.set(pyo3::intern!(tree.py(), stringify!($field)), &value)?;
                        self.$field = value;
                    }
                    Ok(())
                }
            )?)*
        }

        impl $crate::core::row::Field for $name {
            fn from_py(
                value: &pyo3::Bound<'_, pyo3::PyAny>,
            ) -> Result<Self, $crate::core::world_core::Misfit> {
                use pyo3::types::PyDictMethods;
                let record = $crate::core::tracked::as_readable_dict(value)
                    .ok_or_else(|| $crate::core::world_core::Misfit::new("not a dict"))?;
                if record.len() != $name::NAMES.len() {
                    return Err($crate::core::world_core::Misfit::new("other fields than the row's"));
                }
                Ok(Self {
                    $(
                        $field: $crate::core::row::Field::from_py(
                            &record.get_item(pyo3::intern!(value.py(), stringify!($field)))?
                                .ok_or_else(|| $crate::core::world_core::Misfit::new(
                                    concat!("no field ", stringify!($field)),
                                ))?,
                        )?,
                    )*
                })
            }

            fn to_py<'py>(
                &self,
                py: pyo3::Python<'py>,
            ) -> pyo3::PyResult<pyo3::Bound<'py, pyo3::PyAny>> {
                use pyo3::types::PyDictMethods;
                let record = pyo3::types::PyDict::new(py);
                $(
                    record.set_item(
                        pyo3::intern!(py, stringify!($field)),
                        $crate::core::row::Field::to_py(&self.$field, py)?,
                    )?;
                )*
                Ok(record.into_any())
            }

            fn write_json(&self, encoder: &mut $crate::Encoder) -> pyo3::PyResult<()> {
                encoder.out.push(b'{');
                for (place, index) in $name::JSON_ORDER.iter().enumerate() {
                    if place > 0 {
                        encoder.out.push(b',');
                    }
                    $crate::write_json_string(&mut encoder.out, $name::NAMES[*index]);
                    encoder.out.push(b':');
                    $name::JSON_WRITERS[*index](self, encoder)?;
                }
                encoder.out.push(b'}');
                Ok(())
            }

            fn apply(
                &mut self,
                path: &[$crate::core::world_core::Part<'_>],
                write: &$crate::core::world_core::Write<'_, '_>,
            ) -> Result<(), $crate::core::world_core::Misfit> {
                use $crate::core::world_core::{Misfit, Part, Write};
                match (path, write) {
                    ([], Write::Set { key: Part::Key(key), value }) => {
                        $(
                            if *key == stringify!($field) {
                                self.$field = $crate::core::row::Field::from_py(value)?;
                                return Ok(());
                            }
                        )*
                        let _ = value;
                        Err(Misfit::new(format!("no field {key:?}")))
                    }
                    ([Part::Key(key), rest @ ..], write) => {
                        $(
                            if *key == stringify!($field) {
                                return $crate::core::row::Field::apply(&mut self.$field, rest, write);
                            }
                        )*
                        let _ = (rest, write);
                        Err(Misfit::new(format!("no field {key:?}")))
                    }
                    _ => Err(Misfit::new("the write changes which fields a record has")),
                }
            }
        }
    };
}

pub(crate) use state_row;
