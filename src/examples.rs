//! The examples step, examples.json: what the Go functions a review run chose return for the inputs it
//! chose, at the merge base and at the head. The run wrote them as examples.json, which whooper review
//! moved to examples.request.json as soon as the run ended, and whooper_examples_test.go, a test that
//! prints one JSON line per input. The test runs in the package's folder once in the head's worktree and
//! twice in the merge base's, with go's -overlay leaving out the Go files the run edited or added there,
//! so it runs the reviewed commits. A row is same or different when both runs at the merge base printed
//! one result for its input, flaky when they printed two, and error when a run printed none; a function
//! the merge base lacks runs at the head only. Running stops after SECONDS. A run that chose no examples
//! gets no file, and examples Whooper cannot read get one with no rows and why.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use regex::Regex;
use serde_json::{Value, json};

use crate::config;
use crate::connections::{committed, definitions, edited_files, which};
use crate::diff::{diff_files, json_lines};
use crate::fsx::{self, Result, limit_text, listed, plural, read_object};
use crate::gaps::{Limit, TimeLimit, build_error, first_panic, normalize};

pub const SECONDS: f64 = 300.0;
const EXAMPLE_TIMEOUT: &str = "60s";
const EXAMPLE_TEST: &str = "whooper_examples_test.go";
/// A row shows at most this many characters of a result.
const EXAMPLE_CHARS: usize = 4000;

/// A Go function's name without a receiver's pointer or type parameters, such as Cart.Total for
/// (*Cart).Total.
pub fn go_name(name: &str) -> String {
    Regex::new(r"\[[^\]]*\]|[()*]")
        .unwrap()
        .replace_all(name, "")
        .into_owned()
}

/// The result a line of the examples test printed, as a row shows it: error: and the error's text, or
/// else the result as compact JSON.
pub fn example_value(line: &Value) -> String {
    match &line["error"] {
        Value::Null => {}
        Value::String(s) if s.is_empty() => {}
        Value::String(s) => return format!("error: {s}"),
        other => return format!("error: {}", python_json(other)),
    }
    line["result"].to_string()
}

/// JSON as Python's json.dumps writes it by default: ", " and ": " between items.
fn python_json(value: &Value) -> String {
    match value {
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(python_json).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}: {}", Value::String(k.clone()), python_json(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        other => other.to_string(),
    }
}

/// How one run of the test went.
#[derive(PartialEq, Clone, Copy)]
enum Ran {
    Finished,
    Broken,
    Late,
}

type Results = HashMap<(String, String), String>;

/// whooper step examples, within seconds.
pub fn examples_within(run: &Path, seconds: f64) -> Result<()> {
    let request_path = run.join("examples.request.json");
    if !request_path.exists() {
        return Ok(());
    }
    let item = read_object(&run.join("run.json")).unwrap_or_default();
    let root = fs::canonicalize(run)?;
    let sides: HashMap<&str, (PathBuf, String)> = HashMap::from([
        (
            "head",
            (
                root.join("head"),
                fsx::str_of(&item, "head_sha").unwrap_or("").to_string(),
            ),
        ),
        (
            "base",
            (
                root.join("base"),
                fsx::str_of(&item, "merge_base").unwrap_or("").to_string(),
            ),
        ),
    ]);
    let write = |functions: Value, rows: Value, limits: Vec<String>| -> Result<()> {
        fsx::write_file(
            &run.join("examples.json"),
            &fsx::pretty(&json!({"functions": functions, "rows": rows, "limits": limits})),
            false,
        )?;
        Ok(())
    };
    let unreadable = |why: String| {
        write(
            json!([]),
            json!([]),
            vec![format!(
                "Whooper could not read the examples the review run chose, so it ran none: {why}."
            )],
        )
    };
    let text = match fs::read_to_string(&request_path) {
        Ok(t) => t,
        Err(e) => return unreadable(format!("examples.json cannot be read: {e}")),
    };
    let request: Value = match serde_json::from_str(&text) {
        Ok(v) => v,
        Err(e) => return unreadable(format!("examples.json is not JSON: {e}")),
    };
    let schema: Value = serde_json::from_str(&fs::read_to_string(
        config::root().join("schemas/examples-request.json"),
    )?)?;
    if let Err(e) = crate::schema::check(&request, &schema, "examples.json") {
        return unreadable(e);
    }
    let head_tree = sides["head"].0.clone();
    let package = normalize(request["package"].as_str().unwrap_or(""));
    let folder = head_tree.join(&package);
    if !fs::canonicalize(&folder).is_ok_and(|f| f.starts_with(&head_tree)) || !folder.is_dir() {
        return unreadable(format!(
            "examples.json names the package {}, which is not a folder of the PR's head",
            request["package"].as_str().unwrap_or("")
        ));
    }
    let functions_in = request["functions"].as_array().cloned().unwrap_or_default();
    let inputs = request["inputs"].as_array().cloned().unwrap_or_default();
    let names: HashSet<String> = functions_in
        .iter()
        .map(|f| go_name(f["name"].as_str().unwrap_or("")))
        .collect();
    for (i, entry) in inputs.iter().enumerate() {
        let function = entry["function"].as_str().unwrap_or("");
        if !names.contains(&go_name(function)) {
            return unreadable(format!(
                "examples.json's inputs[{i}] names {function}, which its functions do not list"
            ));
        }
    }
    let defined = |side: &str, path: &str| -> HashMap<String, i64> {
        let (tree, commit) = &sides[side];
        committed(tree, commit, path)
            .filter(|t| !t.is_empty())
            .map(|t| {
                definitions(&t, "go")
                    .into_iter()
                    .filter(|d| d.kind == "func" || d.kind == "method")
                    .map(|d| (go_name(&d.name), d.line))
                    .collect()
            })
            .unwrap_or_default()
    };
    let functions: Vec<Value> = functions_in
        .iter()
        .map(|f| {
            let mut f = f.clone();
            let line = defined("head", f["path"].as_str().unwrap_or(""))
                .get(&go_name(f["name"].as_str().unwrap_or("")))
                .copied();
            if let Some(line) = line {
                f["line"] = json!(line);
            }
            f
        })
        .collect();
    let (base_tree, base_commit) = sides["base"].clone();
    let listing = Command::new("git")
        .arg("-C")
        .arg(&base_tree)
        .args([
            "--no-lazy-fetch",
            "ls-tree",
            "-z",
            "--name-only",
            &base_commit,
            "--",
        ])
        .arg(if package != "." {
            format!("{package}/")
        } else {
            ".".into()
        })
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        .unwrap_or_default();
    let mut at_base = HashSet::new();
    for path in listing.split('\0') {
        if path.ends_with(".go") && !path.ends_with("_test.go") {
            at_base.extend(defined("base", path).into_keys());
        }
    }
    let patch = fsx::read_text(&run.join("diff.patch")).unwrap_or_default();
    let others: Vec<String> = diff_files(&patch)
        .into_iter()
        .filter(|f| {
            !f.added.is_empty()
                && !matches!(crate::connections::language_of(&f.path), None | Some("go"))
        })
        .map(|f| f.path)
        .collect();
    let mut limits = vec![
        "The review run chose the functions and inputs, so inputs that would show other changes may be missing.".to_string(),
        "Whooper runs each input once at the PR head, so a result that varies from run to run there can read as a change.".into(),
    ];
    let limit = Limit::new(seconds);
    let scratch = tempfile::tempdir()?;
    let test = scratch.path().join(EXAMPLE_TEST);
    let mut not_run = match fs::copy(run.join(EXAMPLE_TEST), &test) {
        Ok(_) => None,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            Some(format!("the review run wrote no {EXAMPLE_TEST}"))
        }
        Err(e) => Some(format!("{EXAMPLE_TEST} cannot be read: {e}")),
    };
    let go = which("go");
    if not_run.is_none() && go.is_none() {
        not_run = Some("the go command is not on PATH".into());
    }
    let run_test = |side: &str, place: &str| -> (Ran, Results, String) {
        let (tree, commit) = &sides[side];
        let mut replace = serde_json::Map::new();
        let mut names: Vec<(String, String)> = vec![(
            test.display().to_string(),
            format!("{package}/{EXAMPLE_TEST}"),
        )];
        for path in edited_files(tree, &[".go"]).unwrap_or_default() {
            match committed(tree, commit, &path) {
                Some(text) => {
                    let backing = scratch.path().join(format!("{side}-{}.go", replace.len()));
                    let _ = fs::write(&backing, text);
                    names.push((backing.display().to_string(), path.clone()));
                    replace.insert(
                        tree.join(&path).display().to_string(),
                        json!(backing.display().to_string()),
                    );
                }
                None => {
                    replace.insert(tree.join(&path).display().to_string(), json!(""));
                }
            }
        }
        replace.insert(
            tree.join(&package).join(EXAMPLE_TEST).display().to_string(),
            json!(test.display().to_string()),
        );
        let overlay = scratch.path().join(format!("{side}-overlay.json"));
        let _ = fs::write(&overlay, json!({"Replace": replace}).to_string());
        let args: Vec<String> = [
            "test",
            "-json",
            "-count=1",
            "-run",
            "^TestWhooperExamples$",
            &format!("-timeout={EXAMPLE_TIMEOUT}"),
            "-vet=off",
            &format!("-overlay={}", overlay.display()),
            ".",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        let env = [("GOPROXY", "off"), ("GOTOOLCHAIN", "local")];
        let (code, out, err) =
            match limit.run(go.as_deref().unwrap(), &args, &tree.join(&package), &env) {
                Ok((c, o, e)) => (Some(c), o, e),
                Err(TimeLimit) => (None, String::new(), String::new()),
            };
        let events = json_lines(&out);
        let printed: String = events
            .iter()
            .filter(|e| {
                e["Action"] == "output"
                    && e["Test"] == "TestWhooperExamples"
                    && e["OutputType"] != "frame"
            })
            .map(|e| e["Output"].as_str().unwrap_or(""))
            .collect();
        let mut results = Results::new();
        for line in printed.split('\n') {
            if let Ok(value) = serde_json::from_str::<Value>(line)
                && let (Some(f), Some(i)) = (value["function"].as_str(), value["input"].as_str())
            {
                results
                    .entry((go_name(f), i.to_string()))
                    .or_insert_with(|| example_value(&value));
            }
        }
        if code.is_none() {
            return (
                Ran::Late,
                results,
                format!(
                    "The test ran past Whooper's {} limit at {place}.",
                    limit_text(seconds)
                ),
            );
        }
        if !events
            .iter()
            .any(|e| e["Action"] == "run" && e["Test"] == "TestWhooperExamples")
        {
            let mut error = build_error(&events, &err);
            for (backing, path) in &names {
                error = error.replace(backing.as_str(), path);
            }
            return (
                Ran::Broken,
                results,
                format!("The test does not compile at {place}: {error}."),
            );
        }
        let package_name = events
            .iter()
            .find(|e| e["Package"].is_string())
            .map(|e| e["Package"].clone())
            .unwrap_or(Value::Null);
        let problem = match first_panic(&events, &package_name) {
            Some(panic) => format!(
                "The test stopped at {place} before it printed this input's result: {panic}."
            ),
            None => format!("The test printed no result for this input at {place}."),
        };
        (Ran::Finished, results, problem)
    };
    let mut head_run: Option<(Ran, Results, String)> = None;
    let mut base: Vec<(Ran, Results, String)> = vec![];
    if not_run.is_none() {
        let first = run_test("head", "the PR head");
        // The merge base has no use for a test that did not run at the head.
        if first.0 == Ran::Finished && base_tree.join(&package).is_dir() {
            base = (0..2).map(|_| run_test("base", "the merge base")).collect();
        }
        head_run = Some(first);
    }
    let mut cut = false;
    let mut shown = |value: Option<&String>, most: usize| -> Option<String> {
        let value = value?;
        if value.chars().count() > most {
            cut |= most == EXAMPLE_CHARS;
            return Some(value.chars().take(most).collect::<String>() + "…");
        }
        Some(value.clone())
    };
    let mut rows = vec![];
    for entry in &inputs {
        let function = entry["function"].as_str().unwrap_or("").to_string();
        let input = entry["input"].as_str().unwrap_or("").to_string();
        let key = (go_name(&function), input.clone());
        let mut row = json!({"function": function, "input": input, "before": null, "after": null,
                             "status": "error", "label": "not_compared", "note": null});
        if let Some(why) = &not_run {
            row["note"] = json!(format!("Whooper did not run the test: {why}."));
            rows.push(row);
            continue;
        }
        let (_, head, head_problem) = head_run.as_ref().unwrap();
        let after = shown(head.get(&key), EXAMPLE_CHARS);
        row["after"] = json!(after);
        if after.is_none() {
            row["note"] = json!(head_problem);
        } else if !at_base.contains(&key.0) {
            row["status"] = json!("different");
            row["label"] = json!("ran_head_only");
            row["note"] = json!(format!("New in this PR: the merge base has no {function}."));
        } else if base.is_empty() {
            row["note"] = json!(head_problem);
        } else {
            let (first_state, first, first_problem) = &base[0];
            let (second_state, second, second_problem) = &base[1];
            let (first, second) = (first.get(&key), second.get(&key));
            if *first_state == Ran::Late && first.is_none()
                || *second_state == Ran::Late && second.is_none()
            {
                row["note"] = json!(if *first_state == Ran::Late && first.is_none() {
                    first_problem
                } else {
                    second_problem
                });
            } else if first != second {
                let a = shown(first, 200).unwrap_or_else(|| "no result".into());
                let b = shown(second, 200).unwrap_or_else(|| "no result".into());
                row["status"] = json!("flaky");
                row["note"] = json!(format!(
                    "The two runs at the merge base printed {a} and {b}."
                ));
            } else if first.is_none() {
                row["note"] = json!(first_problem);
            } else {
                row["before"] = json!(shown(first, EXAMPLE_CHARS));
                row["status"] = json!(if first == head.get(&key) {
                    "same"
                } else {
                    "different"
                });
                row["label"] = json!("ran_base_and_head");
            }
        }
        rows.push(row);
    }
    if cut {
        limits.push(format!(
            "Whooper cut results longer than {} characters, and compared them whole.",
            "4,000"
        ));
    }
    if !others.is_empty() {
        limits.push(format!(
            "Whooper runs examples only of Go functions so far, so it ran none for {}: {}.",
            plural(
                others.len(),
                "changed file in another language",
                "changed files in other languages"
            ),
            listed(&others, 5)
        ));
    }
    write(json!(functions), json!(rows), limits)
}
