//! `V`: a JSON value owned by Rust, for state without typed rows. It holds exactly what
//! `json.dumps` tells apart: `1`, `1.0` and `True` differ, and a map keeps Python's insertion
//! order, so a value read from Python writes the same bytes and goes back the same.

use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};
use pyo3::IntoPyObjectExt;

use crate::core::tracked::{as_readable_dict, as_readable_list};
use crate::{write_json_string, Encoder, Unsupported, MAX_DEPTH};

#[derive(Clone, Debug, PartialEq)]
pub(crate) enum V {
    Null,
    Bool(bool),
    Int(i64),
    /// An int outside i64, as `int.__repr__` writes it.
    Big(Box<str>),
    Float(f64),
    Str(Arc<str>),
    List(Vec<V>),
    Map(IndexMap<Arc<str>, V>),
}

// Rows and values must be usable without the GIL.
const _: fn() = || {
    fn send_and_sync<T: Send + Sync>() {}
    send_and_sync::<V>();
};

impl V {
    /// Exact scalar types only: a str or int subclass, a tuple or a key that is not a str raises
    /// `Unsupported`, because it would not come back the same. So does a dict or list subclass
    /// other than the tracked containers (`core/tracked.rs`), which are their items.
    pub(crate) fn from_py(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        Self::read(value, 0)
    }

    fn read(value: &Bound<'_, PyAny>, depth: usize) -> PyResult<Self> {
        if depth > MAX_DEPTH {
            return Err(Unsupported::new_err("value nests too deeply"));
        }
        if value.is_none() {
            return Ok(V::Null);
        }
        if let Ok(number) = value.downcast_exact::<PyFloat>() {
            return Ok(V::Float(number.value()));
        }
        if let Ok(text) = value.downcast_exact::<PyString>() {
            return Ok(V::Str(Arc::from(text.to_str()?)));
        }
        if let Ok(flag) = value.downcast_exact::<PyBool>() {
            return Ok(V::Bool(flag.is_true()));
        }
        if let Ok(number) = value.downcast_exact::<PyInt>() {
            return Ok(match number.extract::<i64>() {
                Ok(small) => V::Int(small),
                Err(_) => V::Big(number.repr()?.to_str()?.into()),
            });
        }
        if let Some(object) = as_readable_dict(value) {
            let mut map = IndexMap::with_capacity(object.len());
            for (key, item) in object.iter() {
                let key = key
                    .downcast_exact::<PyString>()
                    .map_err(|_| Unsupported::new_err("object key is not a str"))?;
                map.insert(Arc::from(key.to_str()?), Self::read(&item, depth + 1)?);
            }
            return Ok(V::Map(map));
        }
        if let Some(array) = as_readable_list(value) {
            let mut items = Vec::with_capacity(array.len());
            for item in array.iter() {
                items.push(Self::read(&item, depth + 1)?);
            }
            return Ok(V::List(items));
        }
        Err(Unsupported::new_err("value is not a plain JSON value"))
    }

    /// New plain dicts and lists, in the order of the map.
    pub(crate) fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        match self {
            V::Null => Ok(py.None().into_bound(py)),
            V::Bool(flag) => flag.into_bound_py_any(py),
            V::Int(number) => number.into_bound_py_any(py),
            V::Big(text) => py.get_type::<PyInt>().call1((&**text,)),
            V::Float(number) => number.into_bound_py_any(py),
            V::Str(text) => (&**text).into_bound_py_any(py),
            V::List(items) => {
                let list = PyList::empty(py);
                for item in items {
                    list.append(item.to_py(py)?)?;
                }
                Ok(list.into_any())
            }
            V::Map(map) => {
                let dict = PyDict::new(py);
                for (key, item) in map {
                    dict.set_item(&**key, item.to_py(py)?)?;
                }
                Ok(dict.into_any())
            }
        }
    }

    /// The canonical JSON of the value (`canonical_json` in `lib.rs`), keys sorted by bytes.
    /// NaN and infinity raise `Unsupported`, as there.
    pub(crate) fn write_json(&self, encoder: &mut Encoder) -> PyResult<()> {
        match self {
            V::Null => encoder.out.extend_from_slice(b"null"),
            V::Bool(flag) => {
                encoder
                    .out
                    .extend_from_slice(if *flag { b"true" } else { b"false" });
            }
            V::Int(number) => {
                encoder
                    .out
                    .extend_from_slice(itoa::Buffer::new().format(*number).as_bytes());
            }
            V::Big(text) => encoder.out.extend_from_slice(text.as_bytes()),
            V::Float(number) => encoder.float(*number)?,
            V::Str(text) => write_json_string(&mut encoder.out, text),
            V::List(items) => {
                encoder.out.push(b'[');
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        encoder.out.push(b',');
                    }
                    item.write_json(encoder)?;
                }
                encoder.out.push(b']');
            }
            V::Map(map) => {
                let mut entries: Vec<(&Arc<str>, &V)> = map.iter().collect();
                entries.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
                encoder.out.push(b'{');
                for (index, (key, item)) in entries.into_iter().enumerate() {
                    if index > 0 {
                        encoder.out.push(b',');
                    }
                    write_json_string(&mut encoder.out, key);
                    encoder.out.push(b':');
                    item.write_json(encoder)?;
                }
                encoder.out.push(b'}');
            }
        }
        Ok(())
    }
}
