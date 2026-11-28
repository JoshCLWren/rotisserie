use crate::text::{Text, is_word, issue_int};
use pyo3::prelude::*;
use pyo3::types::PySet;
use regex::{Captures, Regex};
use std::borrow::Cow;

patterns! {
    CLOSING = r"(?i)(?:closes|closed|close|f[iİı]xes|f[iİı]xed|f[iİı]x|resolves|resolved|resolve)\s+#(\d+)";
    NEGATED = r"(?i)(?:(?:(?:do|does|d[iİı]d|w[iİı]ll|would|should|must|can|[iİı]s|are)\s+not)|doesn't|don't|d[iİı]dn't|won't|wouldn't|shouldn't|mustn't|can't|cannot|never|not)\s+(?:close|closes|closed|clos[iİı]ng|f[iİı]x|f[iİı]xes|f[iİı]xed|resolve|resolves|resolved)\s+#(\d+)";
    PARTIAL = r"(?i)part\s+of\s+#(\d+)";
}

fn boundaries<'a>(
    pattern: &'a Regex,
    text: &'a str,
    end_boundary: bool,
) -> impl Iterator<Item = Captures<'a>> {
    let mut start = 0;
    std::iter::from_fn(move || {
        while let Some(captures) = pattern.captures_at(text, start) {
            let item = captures.get(0).expect("full regex match");
            let before = text[..item.start()].chars().next_back();
            let after = text[item.end()..].chars().next();
            if !before.is_some_and(is_word) && !(end_boundary && after.is_some_and(is_word)) {
                start = item.end();
                return Some(captures);
            }
            // A rejected prefix can contain a valid (shorter) negation.
            start = item.start() + item.as_str().chars().next().unwrap().len_utf8();
        }
        None
    })
}

fn collect_numbers<'py>(
    py: Python<'py>,
    text: &str,
) -> PyResult<(Bound<'py, PySet>, Vec<std::ops::Range<usize>>)> {
    let numbers = PySet::empty(py)?;
    let mut negatives = Vec::new();
    for captures in boundaries(&NEGATED, text, true) {
        numbers.add(issue_int(py, &captures[1])?)?;
        negatives.push(captures.get(0).unwrap().range());
    }
    for captures in boundaries(&PARTIAL, text, true) {
        numbers.add(issue_int(py, &captures[1])?)?;
    }
    Ok((numbers, negatives))
}

#[pyfunction]
pub fn nonclosing_issue_numbers<'py>(body: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PySet>> {
    if !body.is_truthy()? {
        return PySet::empty(body.py());
    }
    let text = Text::new(body)?;
    Ok(collect_numbers(body.py(), &text.utf8)?.0)
}

#[pyfunction]
pub fn explicit_closing_issue<'py>(body: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
    let py = body.py();
    if !body.is_truthy()? {
        return Ok(py.None());
    }
    let text = Text::new(body)?;
    let (denied, removed) = collect_numbers(py, &text.utf8)?;
    let prose = if removed.is_empty() {
        Cow::Borrowed(text.utf8.as_ref())
    } else {
        let mut output = String::with_capacity(text.utf8.len());
        let mut offset = 0;
        for range in removed {
            output.push_str(&text.utf8[offset..range.start]);
            offset = range.end;
        }
        output.push_str(&text.utf8[offset..]);
        Cow::Owned(output)
    };
    for captures in boundaries(&CLOSING, &prose, false) {
        let number = issue_int(py, &captures[1])?;
        if !denied.contains(&number)? {
            return Ok(number.unbind());
        }
    }
    Ok(py.None())
}
