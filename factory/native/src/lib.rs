use pyo3::prelude::*;

#[pymodule]
fn _rotisserie_native(_module: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
