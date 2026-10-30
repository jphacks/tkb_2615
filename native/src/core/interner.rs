//! IDs and enum members as small numbers in rows, and one Python object for each, so that
//! `agent.activity is Activity.IDLE` holds and a read allocates nothing. `Interner` holds no
//! Python objects, so rows stay `Send + Sync`; the objects live in side tables.

use std::collections::HashMap;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::PyString;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) struct Sym(u32);

#[derive(Default)]
pub(crate) struct Interner {
    symbols: HashMap<Arc<str>, Sym>,
    texts: Vec<Arc<str>>,
}

impl Interner {
    pub(crate) fn intern(&mut self, text: &str) -> Sym {
        if let Some(&symbol) = self.symbols.get(text) {
            return symbol;
        }
        let symbol = Sym(u32::try_from(self.texts.len()).expect("under 2**32 distinct IDs"));
        let text: Arc<str> = Arc::from(text);
        self.texts.push(text.clone());
        self.symbols.insert(text, symbol);
        symbol
    }

    pub(crate) fn text(&self, symbol: Sym) -> &str {
        &self.texts[symbol.0 as usize]
    }
}

/// The one Python str of each symbol, made when first asked for.
#[derive(Default)]
pub(crate) struct PyStrings {
    objects: Vec<Option<Py<PyString>>>,
}

impl PyStrings {
    pub(crate) fn get<'py>(
        &mut self,
        py: Python<'py>,
        interner: &Interner,
        symbol: Sym,
    ) -> Bound<'py, PyString> {
        let index = symbol.0 as usize;
        if index >= self.objects.len() {
            self.objects.resize_with(index + 1, || None);
        }
        self.objects[index]
            .get_or_insert_with(|| PyString::intern(py, interner.text(symbol)).unbind())
            .bind(py)
            .clone()
    }
}

/// The members of a Python enum, in definition order. Rows hold the index.
pub(crate) struct EnumTable {
    members: Vec<Py<PyAny>>,
}

impl EnumTable {
    pub(crate) fn from_class(class: &Bound<'_, PyAny>) -> PyResult<Self> {
        let members = class
            .try_iter()?
            .map(|item| Ok(item?.unbind()))
            .collect::<PyResult<_>>()?;
        Ok(Self { members })
    }

    pub(crate) fn member<'py>(&self, py: Python<'py>, index: u8) -> Bound<'py, PyAny> {
        self.members[index as usize].bind(py).clone()
    }

    /// The index of this very object; a value equal to a member but not it is `None`, and the
    /// caller leaves it to Python.
    pub(crate) fn index_of(&self, value: &Bound<'_, PyAny>) -> Option<u8> {
        self.members
            .iter()
            .position(|member| member.as_ptr() == value.as_ptr())
            .map(|index| index as u8)
    }
}
