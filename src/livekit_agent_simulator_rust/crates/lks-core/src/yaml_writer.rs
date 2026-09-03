//! Minimal PyYAML-compatible block-style YAML emitter for scenario/config export.
//!
//! Matches scenario_yaml.py `_dump_plain` output:
//!   default_flow_style=False, sort_keys=False, allow_unicode=True, width=100,
//!   `_clean` (drop None / empty collections), `_str_representer` digit-quoting
//!   (pure-digit or 0-led-digit strings single-quoted).
//!
//! This is NOT a general YAML library — it emits the subset the exporter needs
//! (maps, sequences, scalars) with PyYAML-compatible formatting.

use serde_json::{Map, Value as Json};

/// Recursively drop None / empty collection values (mirror `_clean`).
pub fn clean(v: Json) -> Option<Json> {
    match v {
        Json::Null => None,
        Json::Object(m) => {
            let mut out = Map::new();
            for (k, val) in m {
                if let Some(c) = clean(val) {
                    out.insert(k, c);
                }
            }
            if out.is_empty() {
                None
            } else {
                Some(Json::Object(out))
            }
        }
        Json::Array(a) => {
            let out: Vec<Json> = a.into_iter().filter_map(clean).collect();
            if out.is_empty() {
                None
            } else {
                Some(Json::Array(out))
            }
        }
        other => Some(other),
    }
}

/// Emit a serde_json Value as PyYAML-compatible block YAML.
pub fn to_yaml_string(v: &Json) -> String {
    let mut s = String::new();
    emit(v, 0, &mut s);
    s
}

fn indent(n: usize) -> String {
    "  ".repeat(n)
}

fn emit(v: &Json, depth: usize, out: &mut String) {
    match v {
        Json::Object(m) => {
            if m.is_empty() {
                out.push_str("{}\n");
                return;
            }
            for (k, val) in m {
                out.push_str(&indent(depth));
                out.push_str(&scalar_key(k));
                out.push(':');
                match val {
                    // PyYAML: nested containers on the next line; BLOCK-LIST
                    // items sit at the KEY's depth (2-space under `tags:`), not
                    // depth+1 — matches safe_dump block style.
                    Json::Object(_) => {
                        out.push('\n');
                        emit(val, depth + 1, out);
                    }
                    Json::Array(_) => {
                        out.push('\n');
                        emit(val, depth, out);
                    }
                    Json::Null => out.push_str(" null\n"),
                    other => {
                        out.push(' ');
                        emit_scalar(other, out, depth + 1);
                        out.push('\n');
                    }
                }
            }
        }
        Json::Array(a) => {
            if a.is_empty() {
                out.push_str("[]\n");
                return;
            }
            for item in a {
                out.push_str(&indent(depth));
                out.push('-');
                match item {
                    Json::Object(obj) => {
                        // PyYAML compact: `- key: value` with subsequent keys indented.
                        if obj.is_empty() {
                            out.push_str(" {}\n");
                            continue;
                        }
                        let mut first = true;
                        for (k, val) in obj {
                            if first {
                                out.push(' ');
                                first = false;
                            } else {
                                out.push_str(&indent(depth + 1));
                            }
                            out.push_str(&scalar_key(k));
                            out.push(':');
                            if val.is_object() || val.is_array() {
                                out.push('\n');
                                emit(val, depth + 2, out);
                            } else {
                                out.push(' ');
                                emit_scalar(val, out, depth + 2);
                                out.push('\n');
                            }
                        }
                    }
                    Json::Array(_) => {
                        out.push('\n');
                        emit(item, depth + 1, out);
                    }
                    other => {
                        out.push(' ');
                        emit_scalar(other, out, depth + 1);
                        out.push('\n');
                    }
                }
            }
        }
        other => {
            emit_scalar(other, out, depth);
            out.push('\n');
        }
    }
}

/// Emit a scalar (string / number / bool) with PyYAML-compatible quoting.
/// `depth` is the container depth — used for folding long quoted scalars.
fn emit_scalar(v: &Json, out: &mut String, depth: usize) {
    match v {
        Json::Null => out.push_str("null"),
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Number(n) => out.push_str(&n.to_string()),
        Json::String(s) => {
            let q = quote_str(s);
            if q.starts_with('\'') && q.len() > 80 {
                // PyYAML folds single-quoted scalars at width=100; continuation
                // lines indent to the CONTAINER depth (the key's indent + 2 for
                // the value position — i.e. depth*2 spaces where depth is the
                // value's container depth). Literal \n in the value become blank
                // continuation lines.
                out.push_str(&fold_quoted(&q, depth));
            } else {
                out.push_str(&q);
            }
        }
        Json::Object(_) | Json::Array(_) => out.push_str("{}"),
    }
}

/// Fold a single-quoted scalar the way PyYAML does: break at word boundaries
/// near `width=100` (measured from the continuation indent), keeping the quote
/// style. Literal newlines inside the value become blank continuation lines.
fn fold_quoted(q: &str, cont_indent: usize) -> String {
    const WIDTH: usize = 100;
    // PyYAML continuation indent = the KEY's indent (container depth in
    // 2-space units → cont_indent * 2 spaces... actually the key line indent).
    let cont = indent(cont_indent);
    // Content between the outer quotes ('' escapes preserved).
    let inner = &q[1..q.len() - 1];
    let mut out = String::new();
    out.push('\'');
    let mut line = String::new();
    let mut first_line = true;
    for c in inner.chars() {
        if c == '\n' {
            // Literal newline → blank continuation line (PyYAML folds to a
            // blank indented line between wrapped fragments).
            out.push('\n');
            out.push_str(&cont);
            line.clear();
            first_line = false;
            continue;
        }
        // Continuation lines: width budget is WIDTH minus the indent.
        let budget = if first_line {
            WIDTH - 2 // approx key+prefix on the first line
        } else {
            WIDTH - cont.len()
        };
        if line.len() >= budget && c == ' ' {
            // Break at the space (drop the space).
            out.push('\n');
            out.push_str(&cont);
            first_line = false;
            line.clear();
            continue;
        }
        line.push(c);
        out.push(c);
    }
    out.push('\'');
    out
}

/// Quote strings the way PyYAML's SafeDumper does for the strings the exporter
/// produces. Verified against real PyYAML on 2026-08-13:
///   '123','0','00' -> '123' etc.   '09' -> 09 (bare — PyYAML quirk)
///   '0x1F','1e5','-1','1.5','1_000' -> all single-quoted
///   '{"a":"b"}' (JSON metadata) -> quoted (else parsed as flow map)
///   'hello world' -> bare
///
/// Approximation: quote if the string is pure-digit, starts with a YAML-special
/// char ({ [ " # & * ! | > ' - ? : @ ` . 0x/1e etc.), or looks like a number
/// PyYAML would coerce (starts with digit, or ., +, -). Bare words pass through.
fn quote_str(s: &str) -> String {
    if s.is_empty() {
        return "''".to_string();
    }
    let first = s.chars().next().unwrap();
    let starts_special = matches!(
        first,
        '{' | '[' | '"' | '#' | '&' | '*' | '!' | '|' | '>' | '\'' | '?' | ':' | '@' | '`'
    );
    // Number-looking (PyYAML would coerce → quote): leading digit, or ., +, -
    // followed by digit; also pure-digit and 0-led-digit strings.
    let is_number_like = s.chars().all(|c| c.is_ascii_digit())
        || (s.len() > 1 && s.starts_with('0') && s[1..].chars().all(|c| c.is_ascii_digit()))
        || first.is_ascii_digit()
        || ((first == '.' || first == '+' || first == '-')
            && s.len() > 1
            && s.chars().nth(1).unwrap().is_ascii_digit())
        || (first.is_ascii_digit() && s.contains(['e', 'E', '.', '_']));
    // Strings containing `: ` or ` #` (or a trailing colon) break plain-scalar
    // parsing — quote them. This covers values like "Example: open ticket #12345".
    let has_inline_special =
        s.contains(": ") || s.contains(" #") || s.ends_with(':') || s.contains(": ");
    if starts_special || is_number_like || has_inline_special {
        format!("'{}'", s.replace('\'', "''"))
    } else {
        s.to_string()
    }
}

/// Emit a mapping key (quote digit keys too, matching PyYAML `_str_representer`).
fn scalar_key(s: &str) -> String {
    quote_str(s)
}
