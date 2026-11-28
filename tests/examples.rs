//! Black-box tests of whooper step examples on a throwaway origin's Go PR: comparing each input's
//! result at the head with two runs at the merge base, functions new in the PR, file names git quotes,
//! tests that do not compile, runs without the test or go, requests Whooper cannot read, and the time
//! limit.

mod common;

use std::fs;
use std::path::Path;
use std::time::{Duration, Instant};

use common::*;
use serde_json::{Value, json};

// PR 9 of a Go module: Clamp gains a cap at 100, and Half is new. Token, which the PR leaves alone,
// names each request with its process's ID, so two runs of the same input print different results.
const GO_MOD: &str = "module example.com/shop\n\ngo 1.21\n";
const PRICES: &str = "package prices\n\nimport (\n\t\"fmt\"\n\t\"os\"\n)\n\n// Clamp keeps n at or under 100.\nfunc Clamp(n int) int {\n\treturn n\n}\n\n// Token names a request.\nfunc Token(s string) string {\n\treturn s + \"-\" + fmt.Sprint(os.Getpid())\n}\n";
const PRICES_PR: &str = "package prices\n\nimport (\n\t\"fmt\"\n\t\"os\"\n)\n\n// Clamp keeps n at or under 100.\nfunc Clamp(n int) int {\n\tif n > 100 {\n\t\treturn 100\n\t}\n\treturn n\n}\n\n// Token names a request.\nfunc Token(s string) string {\n\treturn s + \"-\" + fmt.Sprint(os.Getpid())\n}\n\n// Half halves n.\nfunc Half(n int) int {\n\treturn n / 2\n}\n";
const CHOSE: &str = "The review run chose the functions and inputs, so inputs that would show other changes may be missing.";
const ONCE_AT_HEAD: &str = "Whooper runs each input once at the PR head, so a result that varies from run to run there can read as a change.";

fn prices_run(w: &Whooper) -> std::path::PathBuf {
    w.run_dir(
        &[("go.mod", GO_MOD), ("prices/prices.go", PRICES)],
        &[("go.mod", GO_MOD), ("prices/prices.go", PRICES_PR)],
    )
}

/// A whooper_examples_test.go for package prices whose TestWhooperExamples prints a line for each of
/// calls, given as (function, input, Go expression).
fn examples_test(calls: &[(&str, &str, &str)]) -> String {
    let lines: String = calls
        .iter()
        .map(|(f, t, e)| format!("\tshow({}, {}, {e})\n", json!(f), json!(t)))
        .collect();
    format!(
        "package prices\n\nimport (\n\t\"encoding/json\"\n\t\"fmt\"\n\t\"testing\"\n)\n\nfunc TestWhooperExamples(t *testing.T) {{\n\tshow := func(function, input string, result any) {{\n\t\tline, _ := json.Marshal(map[string]any{{\"function\": function, \"input\": input, \"result\": result}})\n\t\tfmt.Println(string(line))\n\t}}\n{lines}}}\n"
    )
}

/// The examples.json a review run writes for package prices.
fn request(functions: &[(&str, i64)], inputs: &[(&str, &str)]) -> Value {
    json!({"package": "prices",
           "functions": functions.iter().map(|(n, l)| json!({"name": n, "path": "prices/prices.go", "line": l})).collect::<Vec<_>>(),
           "inputs": inputs.iter().map(|(f, t)| json!({"function": f, "input": t})).collect::<Vec<_>>()})
}

fn row(
    function: &str,
    input: &str,
    before: Option<&str>,
    after: Option<&str>,
    status: &str,
    label: &str,
    note: Option<&str>,
) -> Value {
    json!({"function": function, "input": input, "before": before, "after": after, "status": status,
           "label": label, "note": note})
}

/// What the step writes for run, after the review run wrote request, JSON unless it is a string, as
/// examples.request.json, and test as whooper_examples_test.go; None leaves either out.
fn examples(
    w: &Whooper,
    run: &Path,
    request: Option<String>,
    test: Option<&str>,
    seconds: Option<f64>,
    env: &[(&str, &str)],
) -> Option<Value> {
    if let Some(r) = request {
        fs::write(run.join("examples.request.json"), r).unwrap();
    }
    if let Some(t) = test {
        fs::write(run.join("whooper_examples_test.go"), t).unwrap();
    }
    w.step_file("examples", run, "examples.json", seconds, env)
}

fn pick(value: &Value, keys: &[&str]) -> Value {
    Value::Object(
        keys.iter()
            .map(|k| (k.to_string(), value[k].clone()))
            .collect(),
    )
}

#[test]
fn compares_each_inputs_result_at_the_head_with_two_runs_at_the_merge_base() {
    let w = Whooper::new();
    let run = prices_run(&w);
    // The review run left a change to Clamp in the head's worktree and a probe that does not compile in
    // the merge base's. Neither reaches the runs.
    let prices = run.join("head/prices/prices.go");
    fs::write(&prices, PRICES_PR.replace("return 100", "return 99")).unwrap();
    fs::write(
        run.join("base/prices/probe_test.go"),
        "package prices\n\nfunc broken( {\n",
    )
    .unwrap();
    // The run gave Token's line wrong, and its test prints nothing for Clamp(7).
    let result = examples(
        &w,
        &run,
        Some(
            request(
                &[("Clamp", 9), ("Token", 2)],
                &[
                    ("Clamp", "Clamp(50)"),
                    ("Clamp", "Clamp(101)"),
                    ("Token", "Token(\"a\")"),
                    ("Clamp", "Clamp(7)"),
                ],
            )
            .to_string(),
        ),
        Some(&examples_test(&[
            ("Clamp", "Clamp(50)", "Clamp(50)"),
            ("Clamp", "Clamp(101)", "Clamp(101)"),
            ("Token", "Token(\"a\")", "Token(\"a\")"),
        ])),
        None,
        &[],
    )
    .unwrap();
    assert_eq!(
        result["functions"],
        json!([{"name": "Clamp", "path": "prices/prices.go", "line": 9},
               {"name": "Token", "path": "prices/prices.go", "line": 17}])
    );
    let rows = result["rows"].as_array().unwrap();
    assert_eq!(
        rows[0],
        row(
            "Clamp",
            "Clamp(50)",
            Some("50"),
            Some("50"),
            "same",
            "ran_base_and_head",
            None
        )
    );
    assert_eq!(
        rows[1],
        row(
            "Clamp",
            "Clamp(101)",
            Some("101"),
            Some("100"),
            "different",
            "ran_base_and_head",
            None
        )
    );
    let flaky = &rows[2];
    assert_eq!(
        pick(flaky, &["function", "input", "before", "status", "label"]),
        json!({"function": "Token", "input": "Token(\"a\")", "before": null, "status": "flaky",
               "label": "not_compared"})
    );
    let pid_result = regex::Regex::new(r#"^"a-\d+"$"#).unwrap();
    assert!(pid_result.is_match(flaky["after"].as_str().unwrap()));
    let note =
        regex::Regex::new(r#"^The two runs at the merge base printed "a-\d+" and "a-\d+"\.$"#)
            .unwrap();
    assert!(
        note.is_match(flaky["note"].as_str().unwrap()),
        "{}",
        flaky["note"]
    );
    assert_eq!(
        rows[3],
        row(
            "Clamp",
            "Clamp(7)",
            None,
            None,
            "error",
            "not_compared",
            Some("The test printed no result for this input at the PR head.")
        )
    );
    assert_eq!(result["limits"], json!([CHOSE, ONCE_AT_HEAD]));
    // The worktrees are as the review run left them.
    assert!(fs::read_to_string(&prices).unwrap().contains("return 99"));
    assert!(!run.join("head/prices/whooper_examples_test.go").exists());
}

#[test]
fn a_function_new_in_the_pr_runs_at_the_head_only() {
    let w = Whooper::new();
    let run = prices_run(&w);
    let result = examples(
        &w,
        &run,
        Some(
            request(
                &[("Half", 22), ("Clamp", 9)],
                &[("Half", "Half(4)"), ("Clamp", "Clamp(101)")],
            )
            .to_string(),
        ),
        Some(&examples_test(&[
            ("Half", "Half(4)", "Half(4)"),
            ("Clamp", "Clamp(101)", "Clamp(101)"),
        ])),
        None,
        &[],
    )
    .unwrap();
    let rows = result["rows"].as_array().unwrap();
    assert_eq!(
        rows[0],
        row(
            "Half",
            "Half(4)",
            None,
            Some("2"),
            "different",
            "ran_head_only",
            Some("New in this PR: the merge base has no Half.")
        )
    );
    // The test does not compile at the merge base, which has no Half, so Clamp is not compared either.
    assert_eq!(
        pick(&rows[1], &["before", "after", "status", "label"]),
        json!({"before": null, "after": "100", "status": "error", "label": "not_compared"})
    );
    let note = rows[1]["note"].as_str().unwrap();
    assert!(
        note.starts_with("The test does not compile at the merge base: ")
            && note.contains("undefined: Half"),
        "{note}"
    );
}

#[test]
fn a_function_the_merge_base_defines_in_a_file_whose_name_git_quotes_runs_there_too() {
    let w = Whooper::new();
    // git quotes a path with a character outside ASCII, as prices/größe.go, unless it lists paths ended
    // by NUL.
    let run = w.run_dir(
        &[("go.mod", GO_MOD), ("prices/größe.go", PRICES)],
        &[("go.mod", GO_MOD), ("prices/größe.go", PRICES_PR)],
    );
    let request = json!({"package": "prices", "functions": [{"name": "Clamp", "path": "prices/größe.go", "line": 9}],
                         "inputs": [{"function": "Clamp", "input": "Clamp(101)"}]});
    let result = examples(
        &w,
        &run,
        Some(request.to_string()),
        Some(&examples_test(&[("Clamp", "Clamp(101)", "Clamp(101)")])),
        None,
        &[],
    )
    .unwrap();
    assert_eq!(
        result["rows"],
        json!([row(
            "Clamp",
            "Clamp(101)",
            Some("101"),
            Some("100"),
            "different",
            "ran_base_and_head",
            None
        )])
    );
}

#[test]
fn a_test_that_does_not_compile_at_the_head_gives_errors() {
    let w = Whooper::new();
    let run = prices_run(&w);
    let result = examples(
        &w,
        &run,
        Some(request(&[("Clamp", 9)], &[("Clamp", "Clamp(50)")]).to_string()),
        Some(&examples_test(&[("Clamp", "Clamp(50)", "Clamp(\"50\")")])),
        None,
        &[],
    )
    .unwrap();
    let row = &result["rows"][0];
    assert_eq!(
        pick(row, &["before", "after", "status", "label"]),
        json!({"before": null, "after": null, "status": "error", "label": "not_compared"})
    );
    let note = regex::Regex::new(
        r"^The test does not compile at the PR head: prices/whooper_examples_test\.go:\d+:\d+: ",
    )
    .unwrap();
    assert!(
        note.is_match(row["note"].as_str().unwrap()),
        "{}",
        row["note"]
    );
}

#[test]
fn without_the_test_or_the_go_command_nothing_runs() {
    let no_go = path_without(&["go"]);
    let test = examples_test(&[("Clamp", "Clamp(50)", "Clamp(50)")]);
    for (name, test, env, why) in [
        (
            "no test",
            None,
            vec![],
            "the review run wrote no whooper_examples_test.go",
        ),
        (
            "no go",
            Some(test.as_str()),
            vec![("PATH", no_go.as_str())],
            "the go command is not on PATH",
        ),
    ] {
        let w = Whooper::new();
        let run = prices_run(&w);
        let result = examples(
            &w,
            &run,
            Some(request(&[("Clamp", 9)], &[("Clamp", "Clamp(50)")]).to_string()),
            test,
            None,
            &env,
        );
        assert_eq!(
            result.unwrap(),
            json!({"functions": [{"name": "Clamp", "path": "prices/prices.go", "line": 9}],
                   "rows": [row("Clamp", "Clamp(50)", None, None, "error", "not_compared",
                                Some(&format!("Whooper did not run the test: {why}.")))],
                   "limits": [CHOSE, ONCE_AT_HEAD]}),
            "{name}"
        );
    }
}

#[test]
fn examples_whooper_cannot_read_run_nothing() {
    let mut outside = request(&[("Clamp", 9)], &[("Clamp", "Clamp(50)")]);
    outside["package"] = json!("../prices");
    for (name, request, why) in [
        (
            "not JSON",
            "{\"package\": ".to_string(),
            "examples.json is not JSON",
        ),
        (
            "a result instead",
            json!({"functions": [], "rows": [{"before": "1", "after": "2"}], "limits": []})
                .to_string(),
            "examples.json lacks package, inputs",
        ),
        (
            "a package outside the checkout",
            outside.to_string(),
            "examples.json names the package ../prices, which is not a folder of the PR's head",
        ),
        (
            "an input of another function",
            request(&[("Clamp", 9)], &[("Half", "Half(4)")]).to_string(),
            "examples.json's inputs[0] names Half, which its functions do not",
        ),
    ] {
        let w = Whooper::new();
        let run = prices_run(&w);
        let result = examples(
            &w,
            &run,
            Some(request),
            Some(&examples_test(&[("Clamp", "Clamp(50)", "Clamp(50)")])),
            None,
            &[],
        )
        .unwrap();
        assert_eq!(
            pick(&result, &["functions", "rows"]),
            json!({"functions": [], "rows": []}),
            "{name}"
        );
        let limits = result["limits"].as_array().unwrap();
        assert_eq!(limits.len(), 1, "{name}");
        let limit = limits[0].as_str().unwrap();
        assert!(
            limit.starts_with(&format!(
                "Whooper could not read the examples the review run chose, so it ran none: {why}"
            )),
            "{name}: {limit}"
        );
    }
}

#[test]
fn the_examples_step_stops_at_its_time_limit_and_leaves_no_test_running() {
    let w = Whooper::new();
    let pid = w.root().join("test.pid");
    let test = format!(
        "package prices\n\nimport (\n\t\"os\"\n\t\"strconv\"\n\t\"testing\"\n\t\"time\"\n)\n\nfunc TestWhooperExamples(t *testing.T) {{\n\tos.WriteFile(\"{}\", []byte(strconv.Itoa(os.Getpid())), 0o644)\n\ttime.Sleep(time.Minute)\n}}\n",
        pid.display()
    );
    let run = prices_run(&w);
    let started = Instant::now();
    let result = examples(
        &w,
        &run,
        Some(request(&[("Clamp", 9)], &[("Clamp", "Clamp(50)")]).to_string()),
        Some(&test),
        Some(15.0),
        &[],
    )
    .unwrap();
    assert!(started.elapsed() < Duration::from_secs(30));
    assert_eq!(
        result["rows"],
        json!([row(
            "Clamp",
            "Clamp(50)",
            None,
            None,
            "error",
            "not_compared",
            Some("The test ran past Whooper's 15-second limit at the PR head.")
        )])
    );
    let pid: i32 = fs::read_to_string(&pid).unwrap().trim().parse().unwrap();
    let ended = ends_within(pid, 5);
    if !ended {
        unsafe { libc::kill(pid, libc::SIGKILL) };
    }
    assert!(ended, "the test still runs");
}
