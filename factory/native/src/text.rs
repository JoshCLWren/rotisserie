use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};
use regex::Regex;
use std::borrow::Cow;

patterns! {
    WORD = r"^[\p{L}\p{N}_]$";
}

pub fn compile(pattern: &str) -> Regex {
    // Python additionally considers the four ASCII information separators whitespace.
    Regex::new(
        &pattern
            .replace(r"\s", r"[\p{White_Space}\x1c-\x1f]")
            .replace(r"\S", r"[^\p{White_Space}\x1c-\x1f]"),
    )
    .expect("static Rotisserie regular expression")
}

pub fn is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\x1c'..='\x1f')
}

pub fn is_word(c: char) -> bool {
    if c.is_ascii() {
        c.is_ascii_alphanumeric() || c == '_'
    } else {
        WORD.is_match(c.encode_utf8(&mut [0; 4]))
    }
}

// CPython permits lone surrogates. Their three-byte encodings are mapped to
// private-use characters for matching, retaining the original bytes at the
// same offsets for returned snippets. Both character classes are non-words.
pub struct Text<'a> {
    pub utf8: Cow<'a, str>,
    raw: Option<Cow<'a, [u8]>>,
}

impl<'a> Text<'a> {
    pub fn new(value: &'a Bound<'_, PyAny>) -> PyResult<Self> {
        let value = match value.cast::<PyString>() {
            Ok(value) => value,
            Err(_) => {
                // Ask the original regexp engine for its own buffer/type error.
                value
                    .py()
                    .import("re")?
                    .call_method1("search", ("", value))?;
                unreachable!("re.search accepted a non-string with a string pattern");
            }
        };
        if let Ok(text) = value.to_str() {
            return Ok(Self {
                utf8: Cow::Borrowed(text),
                raw: None,
            });
        }
        let encoded = value
            .py()
            .get_type::<PyString>()
            .call_method1("encode", (value, "utf-8", "surrogatepass"))?;
        let bytes = encoded.cast::<PyBytes>()?.as_bytes();
        let mut utf8 = bytes.to_vec();
        for index in 0..bytes.len().saturating_sub(2) {
            if bytes[index] == 0xed && (0xa0..=0xbf).contains(&bytes[index + 1]) {
                utf8[index] = 0xee;
                utf8[index + 1] -= 0x20;
            }
        }
        Ok(Self {
            utf8: Cow::Owned(String::from_utf8(utf8).expect("surrogates were translated")),
            raw: Some(Cow::Owned(bytes.to_vec())),
        })
    }

    pub fn bytes<'text>(&'text self, part: &'text str) -> &'text [u8] {
        if let Some(raw) = &self.raw {
            let start = part.as_ptr() as usize - self.utf8.as_ptr() as usize;
            &raw[start..start + part.len()]
        } else {
            part.as_bytes()
        }
    }

    pub fn erase(&self, pattern: &Regex) -> Text<'_> {
        let mut matches = pattern.find_iter(&self.utf8).peekable();
        if matches.peek().is_none() {
            return Text {
                utf8: Cow::Borrowed(&self.utf8),
                raw: self.raw.as_deref().map(Cow::Borrowed),
            };
        }
        let mut output = String::with_capacity(self.utf8.len());
        let mut raw = self.raw.as_ref().map(|raw| Vec::with_capacity(raw.len()));
        let mut start = 0;
        for matched in matches {
            let part = &self.utf8[start..matched.start()];
            output.push_str(part);
            if let Some(raw) = &mut raw {
                raw.extend_from_slice(self.bytes(part));
            }
            start = matched.end();
        }
        let tail = &self.utf8[start..];
        output.push_str(tail);
        if let Some(raw) = &mut raw {
            raw.extend_from_slice(self.bytes(tail));
        }
        Text {
            utf8: Cow::Owned(output),
            raw: raw.map(Cow::Owned),
        }
    }
}

pub fn to_python<'py>(py: Python<'py>, bytes: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    if let Ok(text) = std::str::from_utf8(bytes) {
        Ok(PyString::new(py, text).into_any())
    } else {
        PyBytes::new(py, bytes).call_method1("decode", ("utf-8", "surrogatepass"))
    }
}

pub fn issue_int<'py>(py: Python<'py>, digits: &str) -> PyResult<Bound<'py, PyAny>> {
    use pyo3::IntoPyObjectExt;
    if digits.len() <= 640 {
        if let Ok(number) = digits.parse::<u64>() {
            return number.into_bound_py_any(py);
        }
    }
    py.import("builtins")?.getattr("int")?.call1((digits,))
}
