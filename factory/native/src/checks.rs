use crate::text::{Text, is_space, to_python};
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use std::collections::HashSet;

patterns! {
    ANSI = r"\x1b\[[0-9;]*m";
    FMT = r"(?m)^(?:\S+ )?Diff in (?P<path>\S+?):\d+:";
    ROOT = r"^.*?/work/[^/]+/[^/]+/";
    TSC = r"(?m)^(?:\S+ )?(?P<text>(?P<path>[\p{L}\p{N}_./-]+\.tsx?)\(\d+,\d+\): error TS\d+: .+)$";
    RUST_TEST = r"(?m)^(?:\S+ )?test (\S+) \.\.\. FAILED$";
    PYTEST = r"(?m)^(?:\S+ )?FAILED (\S+)";
    COMPILE = r"(?m)^(?:\S+ )?(error(?:\[E\d+\])?: .+)$";
    ERROR = r"(?m)^(?:\S+ )?(?:##\[error\]|::error::)(.+)$";
    GENERIC = r"^Process completed with exit code \d+\.?$";
}

fn append_more(output: &mut Vec<u8>, count: usize, limit: usize) {
    if count > limit {
        output.extend_from_slice(format!(" and {} more", count - limit).as_bytes());
    }
}

fn shortened(text: &str) -> &str {
    &text[..text.char_indices().nth(200).map_or(text.len(), |(i, _)| i)]
}

fn report<'py, 'text>(
    py: Python<'py>,
    kind: &'static str,
    summary: &[u8],
    files: impl ExactSizeIterator<Item = &'text [u8]>,
) -> PyResult<(&'static str, Bound<'py, PyAny>, Bound<'py, PyTuple>)> {
    let paths = files
        .map(|file| to_python(py, file))
        .collect::<PyResult<Vec<_>>>()?;
    Ok((kind, to_python(py, summary)?, PyTuple::new(py, paths)?))
}

#[pyfunction]
pub fn summarize_log<'py>(
    log: &Bound<'py, PyAny>,
) -> PyResult<(&'static str, Bound<'py, PyAny>, Bound<'py, PyTuple>)> {
    let py = log.py();
    let input = Text::new(log)?;
    let clean = input.erase(&ANSI);
    let text = &clean.utf8;
    if text.contains("Diff in ") {
        let mut seen = HashSet::new();
        let files: Vec<_> = FMT
            .captures_iter(text)
            .map(|m| m.name("path").unwrap().as_str())
            .filter(|path| seen.insert(clean.bytes(path)))
            .map(|path| ROOT.find(path).map_or(path, |root| &path[root.end()..]))
            .collect();
        if !files.is_empty() {
            let mut summary = b"cargo fmt --check wants changes in ".to_vec();
            for (i, name) in files.iter().take(3).enumerate() {
                if i > 0 {
                    summary.extend_from_slice(b", ");
                }
                summary.extend_from_slice(clean.bytes(name.rsplit('/').next().unwrap()));
            }
            append_more(&mut summary, files.len(), 3);
            return report(
                py,
                "formatting",
                &summary,
                files.iter().map(|path| clean.bytes(path)),
            );
        }
    }
    if text.contains(": error TS") {
        if let Some(m) = TSC.captures(text) {
            return report(
                py,
                "compile",
                clean.bytes(shortened(m.name("text").unwrap().as_str())),
                std::iter::once(clean.bytes(m.name("path").unwrap().as_str())),
            );
        }
    }
    if text.contains("FAILED") {
        let mut seen = HashSet::new();
        let tests: Vec<_> = RUST_TEST
            .captures_iter(text)
            .chain(PYTEST.captures_iter(text))
            .map(|m| clean.bytes(m.get(1).unwrap().as_str()))
            .filter(|name| seen.insert(*name))
            .collect();
        if !tests.is_empty() {
            let mut summary = b"failing tests: ".to_vec();
            summary.extend_from_slice(&tests[..tests.len().min(2)].join(&b", "[..]));
            append_more(&mut summary, tests.len(), 2);
            if tests
                .iter()
                .any(|name| name.ends_with(b"checked_in_contract_is_current"))
            {
                summary.extend_from_slice(
                    b" (the checked-in generated contract is stale; regenerate it)",
                );
            }
            return report(py, "tests", &summary, std::iter::empty());
        }
    }
    if text.contains("error") {
        for m in COMPILE.captures_iter(text) {
            let message = m.get(1).unwrap().as_str();
            if !message.starts_with("error: test failed") {
                return report(
                    py,
                    "compile",
                    clean.bytes(shortened(message.trim_matches(is_space))),
                    std::iter::empty(),
                );
            }
        }
        for m in ERROR.captures_iter(text) {
            let message = m.get(1).unwrap().as_str().trim_matches(is_space);
            if !GENERIC.is_match(message) {
                return report(
                    py,
                    "other",
                    clean.bytes(shortened(message)),
                    std::iter::empty(),
                );
            }
        }
    }
    report(
        py,
        "other",
        b"the check failed; see its log",
        std::iter::empty(),
    )
}
