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

#[cfg(test)]
mod tests {
    use super::*;

    /// A run's diff.patch with each kind of file git diff writes: a changed file with two hunks, an added
    /// file, a deleted file, a binary file, a pure rename, a path git quotes, and a path with a space,
    /// whose --- and +++ lines git ends with a tab.
    const PATCH: &str = concat!(
        "diff --git a/api/runs.go b/api/runs.go\nindex 1111111..2222222 100644\n--- a/api/runs.go\n+++ b/api/runs.go\n",
        "@@ -10,7 +10,8 @@ func List() {\n a\n b\n c\n-d\n+D\n+E\n f\n g\n h\n",
        "@@ -40,6 +41,7 @@ func Page() {\n p\n q\n r\n+s\n t\n u\n v\n",
        "diff --git a/api/cursor.go b/api/cursor.go\nnew file mode 100644\nindex 0000000..3333333\n--- /dev/null\n",
        "+++ b/api/cursor.go\n@@ -0,0 +1,3 @@\n+package api\n+\n+type Cursor string\n",
        "diff --git a/api/old.go b/api/old.go\ndeleted file mode 100644\nindex 4444444..0000000\n--- a/api/old.go\n",
        "+++ /dev/null\n@@ -1,2 +0,0 @@\n-package api\n-// Old paging.\n",
        "diff --git a/web/logo.png b/web/logo.png\nindex 5555555..6666666 100644\n",
        "Binary files a/web/logo.png and b/web/logo.png differ\n",
        "diff --git a/docs/a.md b/docs/b.md\nsimilarity index 100%\nrename from docs/a.md\nrename to docs/b.md\n",
        "diff --git \"a/docs/caf\\303\\251.md\" \"b/docs/caf\\303\\251.md\"\nindex 7777777..8888888 100644\n",
        "--- \"a/docs/caf\\303\\251.md\"\n+++ \"b/docs/caf\\303\\251.md\"\n@@ -1 +1 @@\n-old\n+new\n",
        "diff --git a/docs/read me.md b/docs/read me.md\nindex 9999999..aaaaaaa 100644\n",
        "--- a/docs/read me.md\t\n+++ b/docs/read me.md\t\n@@ -2,0 +3 @@ intro\n+added\n",
    );

    /// An overview of PATCH that keeps every rule of prompts/review.md's overview.
    fn overview() -> Value {
        json!({
            "summary": "Pages GET /v1/runs by a cursor, so exports of large projects finish.",
            "verdict_reason": "The cursor skips runs created in the same millisecond.",
            "before_after": [{"label": "API callers", "before": "GET /v1/runs returns at most 100 runs.",
                              "after": "It returns up to 500 runs and a next_cursor."}],
            "places": [{"id": "p1", "title": "Runs with equal timestamps", "reason": "Page orders by created_at only.",
                        "evidence": {"kind": "ran", "detail": "TestPage at head and base"},
                        "path": "api/runs.go", "start_line": 41, "end_line": 47}],
            "layers": [
                {"id": "l1", "title": "The cursor", "intent": "A cursor type, and List and Page take it.", "read_hint": "read",
                 "ranges": [{"path": "api/cursor.go", "start_line": 1, "end_line": 3},
                            {"path": "api/runs.go", "start_line": 12, "end_line": 14},
                            {"path": "api/runs.go", "start_line": 41, "end_line": 47}]},
                {"id": "l2", "title": "The old paging", "intent": "Removed.", "read_hint": "skim",
                 "ranges": [{"path": "api/old.go", "start_line": 1, "end_line": 2}]},
                {"id": "l3", "title": "Docs", "intent": "Wording only.", "read_hint": "skip",
                 "ranges": [{"path": "docs/café.md", "start_line": 1, "end_line": 1},
                            {"path": "docs/read me.md", "start_line": 3, "end_line": 3}]},
            ],
            "flow": {"title": "Paging an export", "input": "GET /v1/runs?cursor=c1", "calls": [
                {"from": "handler", "to": "store", "label": "Page(c1)", "status": "changed", "old_label": "List()",
                 "path": "api/runs.go", "line": 44},
                {"from": "store", "to": "MySQL", "label": "SELECT ... LIMIT 500", "status": "same", "old_label": null,
                 "path": null, "line": null}]},
            "checked": ["Ran TestPage at head and base."],
            "not_checked": ["The query plan: the sandbox has no database."],
        })
    }

    fn range(path: &str, start: i64, end: i64) -> Value {
        json!({"path": path, "start_line": start, "end_line": end})
    }

    #[test]
    fn an_overview_that_keeps_every_rule_passes() {
        check_overview(&overview(), PATCH).unwrap();
        // A PR that changes no flow between components has none.
        let mut no_flow = overview();
        no_flow["flow"] = Value::Null;
        check_overview(&no_flow, PATCH).unwrap();
    }

    #[test]
    fn each_break_of_an_overview_is_caught_and_named() {
        type Change = fn(&mut Value);
        let cases: Vec<(Change, &str)> = vec![
            (
                |o| o["layers"][0]["ranges"][1] = range("api/runs.go", 20, 22),
                "overview.layers[0].ranges[1], lines 20 to 22 of api/runs.go, is not inside one hunk of the diff",
            ),
            (
                |o| o["layers"][0]["ranges"][1] = range("api/runs.go", 12, 45),
                "overview.layers[0].ranges[1], lines 12 to 45 of api/runs.go, is not inside one hunk of the diff",
            ),
            (
                |o| o["layers"][0]["ranges"][1] = range("api/runs.go", 14, 12),
                "overview.layers[0].ranges[1], lines 14 to 12 of api/runs.go, is not inside one hunk of the diff",
            ),
            (
                |o| o["layers"][1]["ranges"][0]["end_line"] = json!(3),
                "overview.layers[1].ranges[0], lines 1 to 3 of api/old.go, is not inside one hunk of the diff",
            ),
            (
                |o| {
                    o["layers"][2]["ranges"].as_array_mut().unwrap().push(range(
                        "web/logo.png",
                        1,
                        1,
                    ))
                },
                "overview.layers[2].ranges[2], lines 1 to 1 of web/logo.png, is not inside one hunk of the diff",
            ),
            (
                |o| {
                    o["places"][0]["start_line"] = json!(30);
                    o["places"][0]["end_line"] = json!(31);
                },
                "overview.places[0], lines 30 to 31 of api/runs.go, is not inside one hunk of the diff",
            ),
            (
                |o| o["places"][0]["path"] = json!("api/other.go"),
                "overview.places[0] names api/other.go, which the diff does not change",
            ),
            (
                |o| {
                    o["layers"].as_array_mut().unwrap().pop();
                },
                "docs/café.md is in no layer",
            ),
            (
                |o| {
                    o["layers"][1]["ranges"].as_array_mut().unwrap().push(range(
                        "api/cursor.go",
                        1,
                        3,
                    ))
                },
                "api/cursor.go is in overview.layers[0] and overview.layers[1]",
            ),
            (
                |o| o["places"] = json!(vec![o["places"][0].clone(); 6]),
                "overview.places has more than 5 items",
            ),
            (
                |o| o["flow"] = json!([o["flow"], o["flow"]]),
                "overview.flow is not of type null or object",
            ),
            (
                |o| o["places"][0]["evidence"]["kind"] = json!("guessed"),
                r#"overview.places[0].evidence.kind is "guessed", not one of ["ran","read","searched"]"#,
            ),
            (
                |o| o["layers"][0]["read_hint"] = json!("deep"),
                r#"overview.layers[0].read_hint is "deep", not one of ["read","skim","skip"]"#,
            ),
            (
                |o| o["flow"]["calls"][0]["status"] = json!("moved"),
                r#"overview.flow.calls[0].status is "moved", not one of ["same","added","removed","changed"]"#,
            ),
            (
                |o| o["flow"]["calls"][0]["line"] = json!(0),
                "overview.flow.calls[0].line is below 1",
            ),
            (
                |o| o["places"][0]["start_line"] = json!("41"),
                "overview.places[0].start_line is not of type integer",
            ),
            (
                |o| {
                    o.as_object_mut().unwrap().remove("not_checked");
                },
                "overview lacks not_checked",
            ),
        ];
        for (change, reason) in cases {
            let mut value = overview();
            change(&mut value);
            assert_eq!(check_overview(&value, PATCH), Err(reason.to_string()));
        }
    }

    type Shown = (String, Option<String>, Vec<(i64, i64)>, Vec<i64>, Vec<i64>);

    fn shown(patch: &str) -> Vec<Shown> {
        diff_files(patch)
            .into_iter()
            .map(|f| {
                (
                    f.path,
                    f.old,
                    f.spans,
                    f.added.into_iter().collect(),
                    f.removed.into_iter().collect(),
                )
            })
            .collect()
    }

    fn file(
        path: &str,
        old: Option<&str>,
        spans: &[(i64, i64)],
        added: &[i64],
        removed: &[i64],
    ) -> Shown {
        (
            path.into(),
            old.map(String::from),
            spans.to_vec(),
            added.to_vec(),
            removed.to_vec(),
        )
    }

    #[test]
    fn diff_files_name_each_file_and_its_hunks_as_the_page_does() {
        // Each file by its path in the head, or a deleted file's at the merge base, with its path at the
        // merge base, each hunk's first and last line, and the lines the patch adds and removes. The
        // binary file and the pure rename show no lines.
        assert_eq!(
            shown(PATCH),
            vec![
                file(
                    "api/runs.go",
                    Some("api/runs.go"),
                    &[(10, 17), (41, 47)],
                    &[13, 14, 44],
                    &[13]
                ),
                file("api/cursor.go", None, &[(1, 3)], &[1, 2, 3], &[]),
                file("api/old.go", Some("api/old.go"), &[(1, 2)], &[], &[1, 2]),
                file("web/logo.png", Some("web/logo.png"), &[], &[], &[]),
                file("docs/b.md", Some("docs/a.md"), &[], &[], &[]),
                file("docs/café.md", Some("docs/café.md"), &[(1, 1)], &[1], &[1]),
                file(
                    "docs/read me.md",
                    Some("docs/read me.md"),
                    &[(3, 3)],
                    &[3],
                    &[]
                ),
            ]
        );
        // A form feed in a line of code ends no line, and "\ No newline at end of file" is none of the
        // file's.
        let patch = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-b\x0cc\n+b\n d\n\\ No newline at end of file\n";
        assert_eq!(
            shown(patch),
            vec![file("x.py", Some("x.py"), &[(1, 3)], &[2], &[2])]
        );
    }

    #[test]
    fn read_overview_keeps_a_fitting_overview_and_says_why_it_drops_any_other() {
        let run = tempfile::tempdir().unwrap();
        assert_eq!(read_overview(run.path(), PATCH), (None, None));
        fs::write(run.path().join("overview.json"), overview().to_string()).unwrap();
        assert_eq!(read_overview(run.path(), PATCH), (Some(overview()), None));
        fs::write(
            run.path().join("overview.json"),
            r#"{"summary": "Pages GET"#,
        )
        .unwrap();
        let (held, why) = read_overview(run.path(), PATCH);
        assert_eq!(held, Some(json!(r#"{"summary": "Pages GET"#)));
        assert!(why.unwrap().starts_with("overview.json is not JSON: "));
        fs::write(run.path().join("overview.json"), b"\xff\xfe{}").unwrap();
        assert_eq!(
            read_overview(run.path(), PATCH),
            (
                None,
                Some("overview.json cannot be read: it is not UTF-8".into())
            )
        );
    }

    fn commits(list: &[(&[&str], usize)]) -> Vec<HashSet<String>> {
        list.iter()
            .flat_map(|(files, n)| {
                std::iter::repeat_n(files.iter().map(|f| f.to_string()).collect(), *n)
            })
            .collect()
    }

    fn set(files: &[&str]) -> HashSet<String> {
        files.iter().map(|f| f.to_string()).collect()
    }

    #[test]
    fn a_file_co_changes_from_60_percent_of_a_pr_files_commits_and_3_commits() {
        // p changed in 3 of x's 5 commits, 60%; in 3 of 6, in both of y's 2, or as one of the PR's
        // files, it does not count.
        assert_eq!(
            co_change(&commits(&[(&["x", "p"], 3), (&["x"], 2)]), &set(&["x"])),
            json!([{"path": "p", "partner": "x", "ratio": 0.6, "commits": 3}])
        );
        assert_eq!(
            co_change(&commits(&[(&["x", "p"], 3), (&["x"], 3)]), &set(&["x"])),
            json!([])
        );
        assert_eq!(
            co_change(&commits(&[(&["y", "p"], 2)]), &set(&["y"])),
            json!([])
        );
        assert_eq!(
            co_change(&commits(&[(&["x", "p"], 3)]), &set(&["x", "p"])),
            json!([])
        );
    }

    #[test]
    fn co_change_lists_at_most_5_files_highest_ratio_first_each_with_its_closest_pr_file() {
        let list = commits(&[
            (&["app", "c1", "c2", "c3", "d1", "d2"], 3),
            (&["app", "c1", "c2"], 1),
            (&["app", "c1"], 1),
            (&["lib", "d1", "e1"], 3),
            (&["lib", "d1"], 1),
        ]);
        // d1 changed in 3 of app's 5 commits but in all 4 of lib's, so it counts once, with lib. d2 ties
        // c3 at 3 of app's 5 commits, and comes sixth by its path.
        assert_eq!(
            co_change(&list, &set(&["app", "lib"])),
            json!([
                {"path": "c1", "partner": "app", "ratio": 1.0, "commits": 5},
                {"path": "d1", "partner": "lib", "ratio": 1.0, "commits": 4},
                {"path": "c2", "partner": "app", "ratio": 0.8, "commits": 4},
                {"path": "e1", "partner": "lib", "ratio": 0.75, "commits": 3},
                {"path": "c3", "partner": "app", "ratio": 0.6, "commits": 3},
            ])
        );
    }
}
