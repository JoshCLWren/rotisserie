use crate::text::{Text, is_space, issue_int};
use pyo3::prelude::*;
use pyo3::types::PySet;

patterns! {
    INLINE = r"(?:[Dd]epends?\s+on|[Bb]locked\s+by)\s+([^\n]+)";
    HEADING = r"(?i)^(?:#{1,6}\s*)?(?:dependenc[iİı]es|depends?\s+on|blocked\s+by|prerequ[iİı]s[iİı]tes?)\s*:?\s*$";
    NUMBERED = r"^[ \t]*\d+[.)][ \t]+";
    LINK = r"\[#(\d+)\]\([^)]*\)";
    REFERENCE = r"#(\d+)";
    TOKEN = r"^#(\d+)$";
}

fn normalize(text: &str) -> String {
    LINK.replace_all(text, "#${1}")
        .replace('`', "")
        .replace("**", "")
        .replace("__", "")
}

fn inline(text: &str, output: &Bound<'_, PySet>) -> PyResult<()> {
    for matched in INLINE.captures_iter(text) {
        for token in normalize(&matched[1])
            .split(is_space)
            .filter(|s| !s.is_empty())
        {
            let token = token.trim_end_matches([',', '.', ';', ':']);
            if let Some(reference) = TOKEN.captures(token) {
                output.add(issue_int(output.py(), &reference[1])?)?;
            } else if !(token.eq_ignore_ascii_case("and") || matches!(token, "&" | "+" | ",")) {
                break;
            }
        }
    }
    Ok(())
}

fn headings(text: &str, output: &Bound<'_, PySet>) -> PyResult<()> {
    let mut in_heading = false;
    // An empty line between the two characters of CRLF has the same role
    // as an ordinary blank line in a dependency section.
    for line in text.split([
        '\n', '\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\u{85}', '\u{2028}', '\u{2029}',
    ]) {
        if in_heading {
            let stripped = line.trim_matches(is_space);
            if stripped.is_empty() {
                continue;
            }
            let item = match stripped.as_bytes() {
                [b'-' | b'*' | b'+', b' ' | b'\t', ..] => Some(&stripped[1..]),
                _ => NUMBERED.find(line).map(|item| &line[item.end()..]),
            };
            if let Some(item) = item {
                for reference in REFERENCE.captures_iter(&normalize(item)) {
                    output.add(issue_int(output.py(), &reference[1])?)?;
                }
                continue;
            }
        }
        in_heading = HEADING.is_match(line);
    }
    Ok(())
}

fn parse<'py>(
    body: &Bound<'py, PyAny>,
    read_inline: bool,
    read_headings: bool,
) -> PyResult<Bound<'py, PySet>> {
    let output = PySet::empty(body.py())?;
    if body.is_truthy()? {
        let body = Text::new(body)?;
        if read_inline {
            inline(&body.utf8, &output)?;
        }
        if read_headings {
            headings(&body.utf8, &output)?;
        }
    }
    Ok(output)
}

#[pyfunction]
pub fn parse_declared_dependencies<'py>(body: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PySet>> {
    parse(body, true, true)
}

#[pyfunction]
pub fn parse_inline_depends_on_numbers<'py>(
    body: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PySet>> {
    parse(body, true, false)
}

#[pyfunction]
pub fn parse_heading_dependency_numbers<'py>(
    body: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PySet>> {
    parse(body, false, true)
}
