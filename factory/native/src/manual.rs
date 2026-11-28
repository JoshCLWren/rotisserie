use crate::text::{Text, to_python};
use pyo3::prelude::*;

patterns! {
    FENCED = r"(?ms)^[ \t]*```[^\n]*\n.*?^[ \t]*```[ \t]*$";
    INLINE = r"`[^`\n]+`";
    DIRECTIVE = r"(?m)^[ \t]*(?:<!--[ \t]*factory-execution:manual-only[ \t]*-->|manual-only)[ \t]*$";
}

#[pyfunction]
pub fn strip_code_segments<'py>(text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let text_value = Text::new(text)?;
    let unfenced = text_value.erase(&FENCED);
    let prose = unfenced.erase(&INLINE);
    to_python(text.py(), prose.bytes(&prose.utf8))
}

#[pyfunction]
pub fn is_manual_only(body: &Bound<'_, PyAny>) -> PyResult<bool> {
    if !body.is_truthy()? {
        return Ok(false);
    }
    let text = Text::new(body)?;
    if !text.utf8.contains("manual") && !text.utf8.contains('`') {
        return Ok(false);
    }
    let unfenced = text.erase(&FENCED);
    let prose = unfenced.erase(&INLINE);
    Ok(DIRECTIVE.is_match(&prose.utf8))
}
