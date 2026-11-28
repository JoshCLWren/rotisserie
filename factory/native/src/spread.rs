use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyFloatMethods, PyList, PyTuple};

fn extract_floats<'py>(
    values: impl ExactSizeIterator<Item = Bound<'py, PyAny>>,
) -> Option<Vec<f64>> {
    let mut result = Vec::with_capacity(values.len());
    for value in values {
        let value = value.cast_exact::<PyFloat>().ok()?.value();
        if value.is_nan() {
            return None;
        }
        result.push(value);
    }
    Some(result)
}

fn position(size: usize, tenth: usize) -> (usize, usize) {
    let rank = (size - 1) * tenth;
    (rank / 10, rank % 10)
}

fn float_spread(result: &Bound<'_, PyDict>, mut sorted: Vec<f64>) -> PyResult<()> {
    sorted.sort_by(|a, b| {
        a.partial_cmp(b)
            .expect("NaNs are handled by Python sorting")
    });
    let n = sorted.len();
    let quantile = |tenth| {
        let (i, remainder) = position(n, tenth);
        let delta = remainder as f64;
        (sorted[i] * (10.0 - delta) + sorted[i + 1] * delta) / 10.0
    };
    let median = if n % 2 == 0 {
        (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0
    } else {
        sorted[n / 2]
    };
    result.set_item("median", median)?;
    result.set_item("low", quantile(1))?;
    result.set_item("high", quantile(9))
}

fn statistics_error(py: Python<'_>, message: &str) -> PyResult<PyErr> {
    let class = py.import("statistics")?.getattr("StatisticsError")?;
    Ok(PyErr::from_value(class.call1((message,))?))
}

fn object_spread<'py>(result: &Bound<'py, PyDict>, values: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = values.py();
    let sort = py.import("builtins")?.getattr("sorted")?;
    let sorted = sort.call1((values,))?.cast_into::<PyList>()?;
    let size = sorted.len();
    if size == 0 {
        return Err(statistics_error(py, "must have at least one data point")?);
    }
    if size == 1 {
        let value = sorted.get_item(0)?;
        result.set_item("low", &value)?;
        result.set_item("high", &value)?;
    } else {
        // Execute all nine operations in order for custom numeric types,
        // including errors and side effects in intermediate deciles.
        for tenth in 1..10 {
            let (i, delta) = position(size, tenth);
            let left = sorted.get_item(i)?.mul(10 - delta)?;
            let right = sorted.get_item(i + 1)?.mul(delta)?;
            let interpolated = left.add(right)?.div(10)?;
            if tenth == 1 {
                result.set_item("low", interpolated)?;
            } else if tenth == 9 {
                result.set_item("high", interpolated)?;
            }
        }
    }
    // The published function sorts and iterates twice. Preserve ordering of
    // comparisons for NaNs and stateful sequences, and Python number semantics.
    let sorted = sort.call1((values,))?.cast_into::<PyList>()?;
    let n = sorted.len();
    if n == 0 {
        return Err(statistics_error(py, "no median for empty data")?);
    }
    let middle = sorted.get_item(n / 2)?;
    let median = if n % 2 == 0 {
        sorted.get_item(n / 2 - 1)?.add(middle)?.div(2)?
    } else {
        middle
    };
    result.set_item("median", median)
}

#[pyfunction]
pub fn spread<'py>(values: &Bound<'py, PyAny>, minimum: usize) -> PyResult<Py<PyAny>> {
    let py = values.py();
    if values.len()? < minimum {
        return Ok(py.None());
    }
    let floats = if let Ok(list) = values.cast_exact::<PyList>() {
        extract_floats(list.iter())
    } else if let Ok(tuple) = values.cast_exact::<PyTuple>() {
        extract_floats(tuple.iter())
    } else {
        None
    };
    let measurements = PyDict::new(py);
    if let Some(floats) = floats {
        float_spread(&measurements, floats)?;
    } else {
        object_spread(&measurements, values)?;
    }
    let result = PyDict::new(py);
    for key in ["median", "low", "high"] {
        result.set_item(
            key,
            measurements.get_item(key)?.expect("computed measurement"),
        )?;
    }
    result.set_item("n", values.len()?)?;
    Ok(result.into_any().unbind())
}
