//! Black-box tests of the review steps whooper step runs on a run directory: connections, test-gaps,
//! examples, and mutation, on a throwaway origin's PR with its head and base worktrees.

mod common;

use std::fs;
use std::path::PathBuf;
use std::process::Command;

use common::*;
use serde_json::Value;

/// A run directory for PR 12, which changes base's files to head's: run.json, diff.patch, and the
/// worktrees head and base, as review leaves it when the run ends.
fn run_dir(w: &Whooper, base: &[(&str, &str)], head: &[(&str, &str)]) -> PathBuf {
    let (_, merge_base) = w.pull_request(base, head);
    let origin = w.root().join("origin");
    let git = |dir: &std::path::Path, args: &[&str]| {
        let mut c = Command::new("git");
        c.args(args).current_dir(dir);
        for (k, v) in w.command(&[]).get_envs() {
            match v {
                Some(v) => c.env(k, v),
                None => c.env_remove(k),
            };
        }
        let out = c.output().unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).into_owned()
    };
    let head_sha = git(&origin, &["rev-parse", "pr-12"]).trim().to_string();
    let run = w.root().join("run");
    for (tree, commit) in [("head", head_sha.as_str()), ("base", merge_base.as_str())] {
        let path = run.join(tree);
        git(
            &origin,
            &[
                "worktree",
                "add",
                "--quiet",
                "--detach",
                path.to_str().unwrap(),
                commit,
            ],
        );
    }
    fs::write(
        run.join("run.json"),
        serde_json::json!({"head_sha": head_sha, "merge_base": merge_base}).to_string(),
    )
    .unwrap();
    let patch = git(
        &run.join("head"),
        &["diff", "--no-color", "--no-ext-diff", &merge_base, "HEAD"],
    );
    fs::write(run.join("diff.patch"), patch).unwrap();
    run
}

fn step(w: &Whooper, name: &str, run: &std::path::Path, file: &str) -> Value {
    let out = w.run(&["step", name, run.to_str().unwrap()]);
    assert!(
        out.status.success(),
        "{name}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    read_json(&run.join(file))
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
