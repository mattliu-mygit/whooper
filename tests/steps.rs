//! Black-box tests of the review steps whooper step runs on a run directory: connections, test-gaps,
//! examples, and mutation, on a throwaway origin's PR with its head and base worktrees.

mod common;

use std::path::PathBuf;

use common::*;
use serde_json::Value;

fn run_dir(w: &Whooper, base: &[(&str, &str)], head: &[(&str, &str)]) -> PathBuf {
    w.run_dir(base, head)
}

fn step(w: &Whooper, name: &str, run: &std::path::Path, file: &str) -> Value {
    w.step_file(name, run, file, None, &[]).unwrap()
}

const CLI_BASE: &str =
    "from report import summarize\n\n\ndef main(rows):\n    print(summarize(rows))\n";

#[test]
fn connections_link_python_files_by_name_search() {
    let w = Whooper::new();
    let run = run_dir(
        &w,
        &[
            (
                "tools/report.py",
                "def summarize(rows):\n    return len(rows)\n",
            ),
            ("tools/cli.py", CLI_BASE),
        ],
        &[
            (
                "tools/report.py",
                "def summarize(rows):\n    return len([r for r in rows if r])\n",
            ),
            ("tools/cli.py", CLI_BASE),
        ],
    );
    let graph = step(&w, "connections", &run, "connections.json");
    let ids: Vec<&str> = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| n["id"].as_str().unwrap())
        .collect();
    assert_eq!(ids, ["tools/report.py:summarize", "tools/cli.py:main"]);
    assert_eq!(graph["nodes"][0]["change"], "changed");
    assert_eq!(graph["nodes"][1]["change"], "unchanged");
    let edge = &graph["edges"][0];
    assert_eq!(
        (
            edge["from"].as_str(),
            edge["to"].as_str(),
            edge["found_by"].as_str()
        ),
        (
            Some("tools/cli.py:main"),
            Some("tools/report.py:summarize"),
            Some("name_search")
        )
    );
}

#[test]
fn a_pr_that_changes_no_go_or_node_code_has_no_test_gaps_and_no_examples() {
    let w = Whooper::new();
    let run = run_dir(&w, &[("README.md", "old\n")], &[("README.md", "new\n")]);
    let out = w.run(&["step", "test-gaps", run.to_str().unwrap()]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!run.join("test_gaps.json").exists());
    // The run chose no examples, so the examples step writes nothing to compare.
    let out = w.run(&["step", "examples", run.to_str().unwrap()]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !run.join("examples.json").exists()
            || read_json(&run.join("examples.json"))["rows"] == serde_json::json!([])
    );
}

#[test]
fn an_unknown_step_fails() {
    let w = Whooper::new();
    let out = w.run(&["step", "nothing", "/tmp"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stderr).contains("no step nothing"));
}
