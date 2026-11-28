use pyo3::prelude::*;

macro_rules! patterns {
    ($($name:ident = $pattern:literal;)*) => {
        $(
            static $name: std::sync::LazyLock<regex::Regex> =
                std::sync::LazyLock::new(|| crate::text::compile($pattern));
        )*
    };
}

mod checks;
mod closing;
mod dependencies;
mod manual;
mod spread;
mod text;

#[pymodule]
fn _rotisserie_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(checks::summarize_log, module)?)?;
    module.add_function(wrap_pyfunction!(closing::explicit_closing_issue, module)?)?;
    module.add_function(wrap_pyfunction!(closing::nonclosing_issue_numbers, module)?)?;
    module.add_function(wrap_pyfunction!(
        dependencies::parse_declared_dependencies,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        dependencies::parse_heading_dependency_numbers,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        dependencies::parse_inline_depends_on_numbers,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(manual::is_manual_only, module)?)?;
    module.add_function(wrap_pyfunction!(manual::strip_code_segments, module)?)?;
    module.add_function(wrap_pyfunction!(spread::spread, module)?)?;
    Ok(())
}
