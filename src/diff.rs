//! A review's diff.patch as Whooper's page reads it, the overview a review run writes about it, and
//! the commit history in the repo's clone.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::Path;
use std::process::Command;

use regex::Regex;
use serde_json::{Value, json};

use crate::config::repo_clone;
use crate::fsx::local_time;

/// One file a patch git diff wrote changes.
pub struct FileDiff {
    /// The file's path in the head, or for a deleted file at the merge base.
    pub path: String,
    /// Its path at the merge base; None for an added file.
    pub old: Option<String>,
    /// The first and last line of each hunk, counted in the head, or for a deleted file at the base.
    pub spans: Vec<(i64, i64)>,
    /// The numbers of the lines the patch adds, counted in the head.
    pub added: BTreeSet<i64>,
    /// The numbers of the lines the patch removes, counted at the merge base.
    pub removed: BTreeSet<i64>,
    /// Each hunk's first line and line count at the merge base and in the head.
    pub hunks: Vec<[i64; 4]>,
}

/// A path as a patch names it, without prefix and without the quotes and C escapes git gives a path
/// with unusual characters, as in "a/caf\303\251.md"; None for /dev/null.
fn diff_path(text: &str, prefix: &str) -> Option<String> {
    let text = if text.starts_with('"') && text.len() >= 2 {
        unescape(&text[1..text.len() - 1])
    } else {
        text.to_string()
    };
    if text == "/dev/null" {
        None
    } else {
        Some(text.strip_prefix(prefix).unwrap_or(&text).to_string())
    }
}

fn unescape(text: &str) -> String {
    let (mut bytes, mut chars) = (Vec::new(), text.bytes().peekable());
    while let Some(b) = chars.next() {
        if b != b'\\' {
            bytes.push(b);
            continue;
        }
        match chars.next() {
            Some(d @ b'0'..=b'7') => {
                let mut n = (d - b'0') as u32;
                for _ in 0..2 {
                    if let Some(&c @ b'0'..=b'7') = chars.peek() {
                        n = n * 8 + (c - b'0') as u32;
                        chars.next();
                    }
                }
                bytes.push(n as u8);
            }
            Some(b'n') => bytes.push(b'\n'),
            Some(b't') => bytes.push(b'\t'),
            Some(b'a') => bytes.push(7),
            Some(b'b') => bytes.push(8),
            Some(b'f') => bytes.push(12),
            Some(b'v') => bytes.push(11),
            Some(b'r') => bytes.push(b'\r'),
            Some(other) => bytes.push(other),
            None => bytes.push(b'\\'),
        }
    }
    String::from_utf8_lossy(&bytes).into_owned()
}

/// The files patch changes, as Whooper's page reads them. A file whose diff shows no lines, such as
/// a binary file, a pure rename, or a mode change, has none. The ---, +++, rename, and copy lines name
/// a file's paths; the first line names them only for a file without those, since a path with a space
/// and " b/" in it reads two ways there. Only a newline ends a line.
pub fn diff_files(patch: &str) -> Vec<FileDiff> {
    let hunk = Regex::new(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@").unwrap();
    let header =
        Regex::new(r#"^diff --git ("a/(?:[^"\\]|\\.)*"|a/.+?) ("b/(?:[^"\\]|\\.)*"|b/.+?)$"#)
            .unwrap();
    let mut starts: Vec<usize> = Vec::new();
    if patch.starts_with("diff --git ") {
        starts.push(0);
    }
    starts.extend(patch.match_indices("\ndiff --git ").map(|(i, _)| i + 1));
    let mut files = Vec::new();
    for (n, &start) in starts.iter().enumerate() {
        let end = starts.get(n + 1).copied().unwrap_or(patch.len());
        let part = &patch[start..end];
        let mut lines = part.split('\n');
        let first = lines.next().unwrap_or_default();
        let (mut old, mut new) = match header.captures(first) {
            Some(m) => (diff_path(&m[1], "a/"), diff_path(&m[2], "b/")),
            None => (None, None),
        };
        let (mut created, mut deleted) = (false, false);
        let (mut hunks, mut added, mut removed) =
            (Vec::<[i64; 4]>::new(), BTreeSet::new(), BTreeSet::new());
        let (mut old_line, mut new_line) = (0, 0);
        for line in lines {
            if let Some(m) = hunk.captures(line) {
                let get = |i: usize| m.get(i).map_or(1, |g| g.as_str().parse().unwrap_or(1));
                hunks.push([get(1), get(2), get(3), get(4)]);
                old_line = hunks.last().unwrap()[0];
                new_line = hunks.last().unwrap()[2];
            } else if !hunks.is_empty() {
                if line.starts_with('+') {
                    added.insert(new_line);
                    new_line += 1;
                } else if line.starts_with('-') {
                    removed.insert(old_line);
                    old_line += 1;
                } else if !line.starts_with('\\') {
                    old_line += 1;
                    new_line += 1;
                }
            } else if let Some(rest) = line.strip_prefix("--- ") {
                old = diff_path(rest.split('\t').next().unwrap_or_default(), "a/");
            } else if let Some(rest) = line.strip_prefix("+++ ") {
                new = diff_path(rest.split('\t').next().unwrap_or_default(), "b/");
            } else if line.starts_with("rename from ") || line.starts_with("copy from ") {
                old = diff_path(line.split_once(" from ").unwrap().1, "");
            } else if line.starts_with("rename to ") || line.starts_with("copy to ") {
                new = diff_path(line.split_once(" to ").unwrap().1, "");
            } else if line.starts_with("new file mode") {
                created = true;
            } else if line.starts_with("deleted file mode") {
                deleted = true;
            }
        }
        let spans = hunks
            .iter()
            .filter_map(|&[os, oc, ns, nc]| {
                let (start, count) = if deleted { (os, oc) } else { (ns, nc) };
                (count > 0).then_some((start, start + count - 1))
            })
            .collect();
        files.push(FileDiff {
            path: (if deleted { old.clone() } else { new }).unwrap_or_default(),
            old: if created { None } else { old },
            spans,
            added,
            removed,
            hunks,
        });
    }
    files
}

/// Whether line, on side RIGHT (the head) or LEFT (the merge base) of path, is inside a hunk of patch,
/// where GitHub takes an inline comment.
pub fn in_diff(files: &[FileDiff], path: &str, side: &str, line: i64) -> bool {
    files.iter().any(|f| {
        let named = if side == "LEFT" {
            f.old.as_deref() == Some(path)
        } else {
            f.path == path
        };
        named
            && f.hunks.iter().any(|&[os, oc, ns, nc]| {
                let (start, count) = if side == "LEFT" { (os, oc) } else { (ns, nc) };
                count > 0 && start <= line && line < start + count
            })
    })
}

/// Checks the overview a review run wrote against patch: it fits schemas/overview.json; each place's
/// lines and each layer range lie inside one hunk of a file the patch changes; and each file the patch
/// shows lines of is in exactly one layer.
fn check_overview(overview: &Value, patch: &str) -> Result<(), String> {
    let schema: Value =
        fs::read_to_string(crate::config::root().join("schemas").join("overview.json"))
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .ok_or("schemas/overview.json cannot be read")?;
    crate::schema::check(overview, &schema, "overview")?;
    let files = diff_files(patch);
    let hunks: HashMap<&str, &Vec<(i64, i64)>> =
        files.iter().map(|f| (f.path.as_str(), &f.spans)).collect();
    let mut ranges = Vec::new();
    for (i, place) in overview["places"]
        .as_array()
        .into_iter()
        .flatten()
        .enumerate()
    {
        ranges.push((format!("overview.places[{i}]"), place));
    }
    let layers = overview["layers"].as_array().cloned().unwrap_or_default();
    for (i, layer) in layers.iter().enumerate() {
        for (j, r) in layer["ranges"].as_array().into_iter().flatten().enumerate() {
            ranges.push((format!("overview.layers[{i}].ranges[{j}]"), r));
        }
    }
    for (place, r) in ranges {
        let path = r["path"].as_str().unwrap_or_default();
        let (start, end) = (
            r["start_line"].as_i64().unwrap_or(0),
            r["end_line"].as_i64().unwrap_or(0),
        );
        let Some(spans) = hunks.get(path) else {
            return Err(format!(
                "{place} names {path}, which the diff does not change"
            ));
        };
        if !spans
            .iter()
            .any(|&(first, last)| first <= start && start <= end && end <= last)
        {
            return Err(format!(
                "{place}, lines {start} to {end} of {path}, is not inside one hunk of the diff"
            ));
        }
    }
    for f in &files {
        let named: Vec<String> = layers
            .iter()
            .enumerate()
            .filter(|(_, layer)| {
                layer["ranges"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .any(|r| r["path"] == f.path.as_str())
            })
            .map(|(i, _)| format!("overview.layers[{i}]"))
            .collect();
        if !f.spans.is_empty() && named.len() != 1 {
            let which = if named.is_empty() {
                "no layer".to_string()
            } else {
                named.join(" and ")
            };
            return Err(format!("{} is in {which}", f.path));
        }
    }
    Ok(())
}

/// The overview the review run in run wrote in overview.json, with why Whooper drops it, or with None
/// when it keeps every rule check_overview checks. A file that is not JSON gives its text, and one
/// that cannot be read gives None. Without the file, both are None.
pub fn read_overview(run: &Path, patch: &str) -> (Option<Value>, Option<String>) {
    let bytes = match fs::read(run.join("overview.json")) {
        Ok(bytes) => bytes,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return (None, None),
        Err(e) => return (None, Some(format!("overview.json cannot be read: {e}"))),
    };
    let Ok(text) = String::from_utf8(bytes) else {
        return (
            None,
            Some("overview.json cannot be read: it is not UTF-8".into()),
        );
    };
    let overview: Value = match serde_json::from_str(&text) {
        Ok(v) => v,
        Err(e) => {
            return (
                Some(json!(text)),
                Some(format!("overview.json is not JSON: {e}")),
            );
        }
    };
    match check_overview(&overview, patch) {
        Ok(()) => (Some(overview), None),
        Err(e) => (Some(overview), Some(e)),
    }
}

/// What git <args> prints in repo's clone, or None when git fails. --no-lazy-fetch keeps git from
/// fetching an object the partial clone lacks, so this only reads the clone.
fn read_clone(repo: &str, args: &[&str]) -> Option<String> {
    let out = Command::new("git")
        .arg("--git-dir")
        .arg(repo_clone(repo).join(".git"))
        .arg("--no-lazy-fetch")
        .args(args)
        .env_remove("GIT_EXTERNAL_DIFF")
        .env_remove("GIT_DIFF_OPTS")
        .output()
        .ok()?;
    out.status
        .success()
        .then(|| String::from_utf8_lossy(&out.stdout).into_owned())
}

/// The commits git log <args> lists in repo's clone, newest first, each with its sha, title, author's
/// name, time, and the files it changed, or None when git fails. A rename lists as a delete and an add,
/// since finding renames takes file contents that a partial clone lacks.
pub fn clone_log(repo: &str, args: &[&str]) -> Option<Vec<Value>> {
    let mut all = vec![
        "log",
        "-z",
        "--no-renames",
        "--name-only",
        "--format=//%H%x00%s%x00%an%x00%at",
    ];
    all.extend_from_slice(args);
    let log = read_clone(repo, &all)?;
    let mut commits: Vec<Value> = Vec::new();
    let mut fields = log.split('\0');
    while let Some(field) = fields.next() {
        if let Some(sha) = field.strip_prefix("//") {
            let title = fields.next().unwrap_or_default();
            let author = fields.next().unwrap_or_default();
            let at = fields.next().and_then(|t| t.parse().ok()).unwrap_or(0);
            commits.push(json!({"sha": sha, "title": title, "author": author, "at": local_time(at), "files": []}));
        } else if !field.is_empty()
            && let Some(last) = commits.last_mut()
        {
            last["files"]
                .as_array_mut()
                .unwrap()
                .push(json!(field.strip_prefix('\n').unwrap_or(field)));
        }
    }
    Some(commits)
}

/// The files outside paths, the PR's files, that changed together with one of them in commits: each
/// with the PR's file it changed with most (partner), the share of that file's commits it changed in
/// too (ratio), and in how many (commits). A file counts from a ratio of 0.6 and 3 commits; at most 5,
/// highest ratio first, then most commits, then by path.
pub fn co_change(commits: &[HashSet<String>], paths: &HashSet<String>) -> Value {
    let (mut changes, mut together) = (
        HashMap::<&str, i64>::new(),
        HashMap::<(&str, &str), i64>::new(),
    );
    for files in commits {
        for partner in files.intersection(paths) {
            *changes.entry(partner).or_default() += 1;
            for path in files.difference(paths) {
                *together.entry((path, partner)).or_default() += 1;
            }
        }
    }
    let mut rows: Vec<(f64, i64, &str, &str)> = together
        .iter()
        .map(|(&(path, partner), &n)| (n as f64 / changes[partner] as f64, n, path, partner))
        .collect();
    rows.sort_by(|a, b| {
        b.0.partial_cmp(&a.0)
            .unwrap()
            .then(b.1.cmp(&a.1))
            .then(a.2.cmp(b.2))
            .then(a.3.cmp(b.3))
    });
    let mut seen = HashSet::new();
    let mut best = Vec::new();
    for (ratio, n, path, partner) in rows {
        if n >= 3 && ratio >= 0.6 && seen.insert(path) {
            best.push(json!({"path": path, "partner": partner, "ratio": ratio, "commits": n}));
        }
    }
    best.truncate(5);
    Value::Array(best)
}

/// The JSON objects output holds, one a line, such as the events go test -json prints.
pub fn json_lines(output: &str) -> Vec<Value> {
    output
        .lines()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .filter(Value::is_object)
        .collect()
}
