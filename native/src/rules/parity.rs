//! The `core` modules as Python functions, for `tests/integration/test_native_parity.py`,
//! which compares them with CPython. No rule uses these.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyInt, PyList, PyString};

use crate::core::interner::{EnumTable, Interner, PyStrings};
use crate::core::mathx::{self, Num};
use crate::core::rng::{Key, KeyedDraws};
use crate::core::value::V;
use crate::{Encoder, Unsupported};

#[pyfunction]
fn exp(values: Vec<f64>) -> Vec<Option<f64>> {
    values.into_iter().map(mathx::exp).collect()
}

#[pyfunction]
fn expm1(values: Vec<f64>) -> Vec<Option<f64>> {
    values.into_iter().map(mathx::expm1).collect()
}

#[pyfunction]
fn pow(pairs: Vec<(f64, f64)>) -> Vec<Option<f64>> {
    pairs
        .into_iter()
        .map(|(base, exponent)| mathx::pow(base, exponent))
        .collect()
}

#[pyfunction]
fn hypot(rows: Vec<Vec<f64>>) -> Vec<f64> {
    rows.iter().map(|row| mathx::hypot(row)).collect()
}

#[pyfunction]
fn fsum(rows: Vec<Vec<f64>>) -> Vec<Option<f64>> {
    rows.iter().map(|row| mathx::py_fsum(row)).collect()
}

#[pyfunction]
fn round_digits(values: Vec<f64>, ndigits: i32) -> Vec<Option<f64>> {
    values
        .into_iter()
        .map(|value| mathx::py_round(value, ndigits))
        .collect()
}

#[pyfunction]
fn round_int(values: Vec<f64>) -> Vec<Option<i64>> {
    values.into_iter().map(mathx::py_round_int).collect()
}

#[pyfunction]
fn max_floats(pairs: Vec<(f64, f64)>) -> Vec<f64> {
    pairs
        .into_iter()
        .map(|(first, second)| mathx::py_max(first, second))
        .collect()
}

#[pyfunction]
fn min_floats(pairs: Vec<(f64, f64)>) -> Vec<f64> {
    pairs
        .into_iter()
        .map(|(first, second)| mathx::py_min(first, second))
        .collect()
}

#[pyfunction]
fn sum_floats(rows: Vec<Vec<f64>>) -> Vec<f64> {
    rows.iter().map(|row| mathx::py_sum(row)).collect()
}

fn num(value: &Bound<'_, PyAny>) -> PyResult<Num> {
    Num::from_py(value).ok_or_else(|| Unsupported::new_err("not an exact int or float"))
}

#[pyfunction]
fn max_num<'py>(
    first: &Bound<'py, PyAny>,
    second: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    mathx::max_num(num(first)?, num(second)?).to_py(first.py())
}

#[pyfunction]
fn min_num<'py>(
    first: &Bound<'py, PyAny>,
    second: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    mathx::min_num(num(first)?, num(second)?).to_py(first.py())
}

#[pyfunction]
fn sum_nums<'py>(py: Python<'py>, values: Vec<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    let numbers = values.iter().map(num).collect::<PyResult<Vec<_>>>()?;
    mathx::sum_nums(&numbers)
        .ok_or_else(|| Unsupported::new_err("left i64"))?
        .to_py(py)
}

/// The `CounterRNG(int(seed)).random(*parts)` of each list of str and int parts.
#[pyfunction]
fn draws(seed: &str, keys: Vec<Bound<'_, PyList>>) -> PyResult<Vec<f64>> {
    let draws = KeyedDraws::new(seed);
    let mut out = Vec::with_capacity(keys.len());
    for parts in keys {
        let mut key = Key::new();
        for part in parts.iter() {
            if let Ok(text) = part.downcast_exact::<PyString>() {
                key.text(text.to_str()?);
            } else {
                key.int(part.downcast_exact::<PyInt>()?.extract()?);
            }
        }
        out.push(key.draw(&draws));
    }
    Ok(out)
}

/// The draws of `key_head + (slot, attempt)` written with `mark` and `rewind`.
#[pyfunction]
fn draws_from_mark(seed: &str, label: &str, minute: i64, count: i64) -> Vec<f64> {
    let draws = KeyedDraws::new(seed);
    let mut key = Key::labelled(label);
    key.int(minute);
    let mark = key.mark();
    (0..count)
        .map(|index| {
            key.int(index);
            let value = key.draw(&draws);
            key.rewind(mark);
            value
        })
        .collect()
}

#[pyfunction]
fn value_json<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    let mut encoder = Encoder {
        out: Vec::with_capacity(256),
    };
    V::from_py(value)?.write_json(&mut encoder)?;
    Ok(PyBytes::new(value.py(), &encoder.out))
}

#[pyfunction]
fn value_round_trip<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    V::from_py(value)?.to_py(value.py())
}

/// The singleton of each text, from one interner.
#[pyfunction]
fn interned<'py>(py: Python<'py>, texts: Vec<String>) -> Vec<Bound<'py, PyString>> {
    let mut interner = Interner::default();
    let mut objects = PyStrings::default();
    texts
        .iter()
        .map(|text| {
            let symbol = interner.intern(text);
            objects.get(py, &interner, symbol)
        })
        .collect()
}

/// `(index, member)` for each value, by identity with the members of `class`.
#[pyfunction]
fn enum_members<'py>(
    class: &Bound<'py, PyAny>,
    values: Vec<Bound<'py, PyAny>>,
) -> PyResult<Vec<Option<(u8, Bound<'py, PyAny>)>>> {
    let table = EnumTable::from_class(class)?;
    Ok(values
        .iter()
        .map(|value| {
            table
                .index_of(value)
                .map(|index| (index, table.member(class.py(), index)))
        })
        .collect())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(exp, m)?)?;
    m.add_function(wrap_pyfunction!(expm1, m)?)?;
    m.add_function(wrap_pyfunction!(pow, m)?)?;
    m.add_function(wrap_pyfunction!(hypot, m)?)?;
    m.add_function(wrap_pyfunction!(fsum, m)?)?;
    m.add_function(wrap_pyfunction!(round_digits, m)?)?;
    m.add_function(wrap_pyfunction!(round_int, m)?)?;
    m.add_function(wrap_pyfunction!(max_floats, m)?)?;
    m.add_function(wrap_pyfunction!(min_floats, m)?)?;
    m.add_function(wrap_pyfunction!(sum_floats, m)?)?;
    m.add_function(wrap_pyfunction!(max_num, m)?)?;
    m.add_function(wrap_pyfunction!(min_num, m)?)?;
    m.add_function(wrap_pyfunction!(sum_nums, m)?)?;
    m.add_function(wrap_pyfunction!(draws, m)?)?;
    m.add_function(wrap_pyfunction!(draws_from_mark, m)?)?;
    m.add_function(wrap_pyfunction!(value_json, m)?)?;
    m.add_function(wrap_pyfunction!(value_round_trip, m)?)?;
    m.add_function(wrap_pyfunction!(interned, m)?)?;
    m.add_function(wrap_pyfunction!(enum_members, m)?)?;
    Ok(())
}
