//! Black-box tests of the commands that read and change local state: status, show, pause, resume,
//! settings, draft, submit, retry, skip, remove, and select.

mod common;

use std::fs;

use common::*;
use serde_json::{Value, json};

#[test]
fn status_on_an_empty_state_folder_reports_no_reviews_and_no_round() {
    let w = Whooper::new();
    let status = w.status();
    assert_eq!(status["paused"], false);
    assert_eq!(status["reviews"], json!([]));
    assert!(status["next_round"].is_null());
}

#[test]
fn pause_and_resume_change_the_status() {
    let w = Whooper::new();
    w.ok(&["pause"]);
    assert_eq!(w.status()["paused"], true);
    w.ok(&["resume"]);
    assert_eq!(w.status()["paused"], false);
}

#[test]
fn a_config_whose_job_names_an_unknown_agent_fails() {
    let w = Whooper::new();
    w.set_config("poll = \"claude\"", "poll = \"gpt\"");
    let out = w.run(&["status"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stderr).contains("[jobs] poll names 'gpt'"));
}

#[test]
fn settings_get_fills_in_defaults_and_set_changes_only_its_line() {
    let w = Whooper::new();
    let got: Value = serde_json::from_str(&w.ok(&["settings", "get"])).unwrap();
    assert_eq!(got["poll"]["timeout_minutes"], 10);
    w.set_config("timeout_minutes = 45", "timeout_minutes = 45 # the limit");
    w.ok(&["settings", "set", r#"{"review": {"timeout_minutes": 30}}"#]);
    let text = fs::read_to_string(&w.config).unwrap();
    assert!(text.contains("timeout_minutes = 30 # the limit"), "{text}");
    let out = w.run(&["settings", "set", r#"{"review": {"timeout_minutes": 0}}"#]);
    assert_eq!(out.status.code(), Some(1));
    assert!(
        fs::read_to_string(&w.config)
            .unwrap()
            .contains("timeout_minutes = 30")
    );
}

#[test]
fn show_prints_a_finished_review_and_refuses_an_unknown_run() {
    let w = Whooper::new();
    w.finished_run(
        "20310727T010246Z-12",
        "sample-org/plain-repo",
        12,
        findings(vec![], "approve_once_ci_passes"),
    );
    let shown: Value = serde_json::from_str(&w.ok(&["show", "20310727T010246Z-12"])).unwrap();
    assert_eq!(shown["outcome"], "reviewed");
    let out = w.run(&["show", "../x"]);
    assert_eq!(out.status.code(), Some(1));
}

#[test]
fn draft_changes_are_recorded_and_submit_print_gives_a_fingerprint_expect_checks() {
    let w = Whooper::new();
    let id = "20310727T010246Z-12";
    w.finished_run(
        id,
        "sample-org/plain-repo",
        12,
        findings(vec![finding("1", "**nit:** Rename it.")], "look_first"),
    );
    w.ok(&["draft", id, "include", "1"]);
    let drafts: Value = serde_json::from_str(&w.ok(&["draft", id])).unwrap();
    assert_eq!(drafts["findings"]["1"]["included"], true);
    w.fake("gh", &format!("echo {}", "a".repeat(40)));
    let printed = w.ok(&["submit", id, "--print"]);
    let fingerprint = printed
        .lines()
        .last()
        .unwrap()
        .strip_prefix("fingerprint: ")
        .unwrap()
        .to_string();
    let request = read_json(&w.state.join("runs").join(id).join("submit-request.json"));
    assert_eq!(
        request["comments"][0]["body"],
        "AI comment (vetted)\n\n**nit:** Rename it."
    );
    let out = w.run(&["submit", id, "--expect", "0000"]);
    assert_eq!(out.status.code(), Some(13));
    assert!(fingerprint.len() == 64);
}

#[test]
fn submit_refuses_a_mention_and_new_commits() {
    let w = Whooper::new();
    let id = "20310727T010246Z-12";
    w.finished_run(
        id,
        "sample-org/plain-repo",
        12,
        findings(vec![finding("1", "**nit:** Ask @bob.")], "look_first"),
    );
    w.ok(&["draft", id, "include", "1"]);
    w.fake("gh", &format!("echo {}", "a".repeat(40)));
    assert_eq!(w.run(&["submit", id, "--print"]).status.code(), Some(12));
    w.fake("gh", &format!("echo {}", "c".repeat(40)));
    assert_eq!(
        w.run(&["submit", id, "--print", "--allow-mentions"])
            .status
            .code(),
        Some(10)
    );
    assert!(
        w.state
            .join("rechecks/sample-org-plain-repo-12.json")
            .exists()
    );
}

#[test]
fn skip_and_remove_mark_a_run_until_undone() {
    let w = Whooper::new();
    let id = "20310727T010246Z-12";
    let run = w.finished_run(
        id,
        "sample-org/plain-repo",
        12,
        findings(vec![], "look_first"),
    );
    w.ok(&["skip", "--item", id]);
    assert!(run.join("skipped-by-user").exists());
    w.ok(&["skip", id, "--undo"]);
    assert!(!run.join("skipped-by-user").exists());
    w.ok(&["remove", id]);
    assert!(run.join("removed-by-user").exists());
    w.ok(&["remove", id, "--undo"]);
    assert!(!run.join("removed-by-user").exists());
}

fn poll_output(w: &Whooper, items: Vec<Value>) {
    fs::create_dir_all(w.state.join("poll")).unwrap();
    fs::write(
        w.state.join("poll/20310727T010000Z.json"),
        json!({"items": items}).to_string(),
    )
    .unwrap();
}

fn selected(w: &Whooper) -> (Vec<Value>, String) {
    let out = w.run(&["select"]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let work = String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    (work, String::from_utf8_lossy(&out.stderr).into_owned())
}

#[test]
fn select_drops_handled_work_and_repos_without_a_table() {
    let w = Whooper::new();
    let done = work_item("sample-org/plain-repo", 1, &"1".repeat(40));
    let fresh = work_item("sample-org/plain-repo", 2, &"2".repeat(40));
    let unknown = work_item("other-org/repo", 3, &"3".repeat(40));
    let record = w.state.join("prs/sample-org-plain-repo-1");
    fs::create_dir_all(&record).unwrap();
    fs::write(
        record.join(format!("{}.first_review.json", "1".repeat(40))),
        r#"{"status": "done", "failures": 0, "started_at": "2031-07-27T01:00:00+00:00"}"#,
    )
    .unwrap();
    poll_output(&w, vec![done, fresh.clone(), unknown]);
    let (work, err) = selected(&w);
    assert_eq!(work, vec![fresh]);
    assert!(
        err.contains("dropped sample-org/plain-repo#1 first_review: done at this head"),
        "{err}"
    );
    assert!(
        err.contains("the config has no [repos.\"other-org/repo\"] table"),
        "{err}"
    );
}

#[test]
fn select_fails_a_running_record_no_live_review_holds_and_retries_it() {
    let w = Whooper::new();
    let item = work_item("sample-org/plain-repo", 1, &"1".repeat(40));
    let record = w
        .state
        .join("prs/sample-org-plain-repo-1")
        .join(format!("{}.first_review.json", "1".repeat(40)));
    fs::create_dir_all(record.parent().unwrap()).unwrap();
    fs::write(
        &record,
        r#"{"status": "running", "failures": 1, "started_at": "2031-07-27T01:00:00+00:00"}"#,
    )
    .unwrap();
    poll_output(&w, vec![item.clone()]);
    let (work, _) = selected(&w);
    assert_eq!(work, vec![item]);
    assert_eq!(read_json(&record)["status"], "failed");
    assert_eq!(read_json(&record)["failures"], 2);
}

#[test]
fn select_drops_a_listed_pr_github_reported_merged_and_takes_it_off_the_list() {
    let w = Whooper::new();
    poll_output(&w, vec![]);
    let listed = work_item("sample-org/plain-repo", 5, &"5".repeat(40));
    fs::create_dir_all(w.state.join("rechecks")).unwrap();
    fs::write(
        w.state.join("rechecks/sample-org-plain-repo-5.json"),
        listed.to_string(),
    )
    .unwrap();
    fs::write(
        w.state.join("poll/recheck-prs.json"),
        json!([{"url": "https://github.com/sample-org/plain-repo/pull/5", "state": "MERGED"}])
            .to_string(),
    )
    .unwrap();
    let (work, err) = selected(&w);
    assert!(work.is_empty());
    assert!(err.contains("merged on GitHub"), "{err}");
    assert!(
        !w.state
            .join("rechecks/sample-org-plain-repo-5.json")
            .exists()
    );
}

#[test]
fn retry_queues_the_pr_at_its_head_now() {
    let w = Whooper::new();
    let id = "20310727T010246Z-12";
    w.finished_run(
        id,
        "sample-org/plain-repo",
        12,
        findings(vec![], "look_first"),
    );
    w.fake("gh", &format!("echo {}", "d".repeat(40)));
    w.ok(&["retry", id]);
    let listed = read_json(&w.state.join("rechecks/sample-org-plain-repo-12.json"));
    assert_eq!(listed["head_sha"], "d".repeat(40));
    poll_output(&w, vec![]);
    let (work, _) = selected(&w);
    assert_eq!(work.len(), 1);
}

#[test]
fn the_manifest_names_only_bin_whooper_and_files_in_the_checkout() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let manifest = read_json(&root.join("prompting-time-plugin.json"));
    assert_eq!(manifest["start"], json!(["bin/whooper", "run"]));
    for action in manifest["actions"].as_array().unwrap() {
        assert_eq!(action["command"][0], "bin/whooper");
        assert_eq!(action["confirm"], action["name"] == "submit");
    }
    assert!(root.join(manifest["settings"].as_str().unwrap()).is_file());
    let commands: Vec<&str> = manifest["prerequisites"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p["command"].as_str().unwrap())
        .collect();
    assert_eq!(commands, ["gh", "slackdump", "claude", "devin"]);
}

#[test]
fn the_manifest_declares_the_groups_and_row_actions_whooper_status_uses() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let manifest = read_json(&root.join("prompting-time-plugin.json"));
    let status = &manifest["status"];
    let mut groups: Vec<&Value> = status["groups"].as_array().unwrap().iter().collect();
    groups.sort_by_key(|g| g["order"].as_i64());
    let got: Vec<(&str, bool)> = groups
        .iter()
        .map(|g| (g["id"].as_str().unwrap(), g["needs_you"].as_bool().unwrap()))
        .collect();
    assert_eq!(
        got,
        [
            ("ready", true),
            ("look", true),
            ("failed", true),
            ("reviewing", false),
            ("waiting", false),
            ("done", false)
        ]
    );
    let actions = manifest["actions"].as_array().unwrap();
    let names: Vec<&str> = actions
        .iter()
        .map(|a| a["name"].as_str().unwrap())
        .collect();
    let unique: std::collections::BTreeSet<&&str> = names.iter().collect();
    assert_eq!(unique.len(), names.len());
    // Prompting Time runs a row action without its confirmation flow only when it needs none.
    for name in status["item_actions"].as_array().unwrap() {
        let action = actions.iter().find(|a| &a["name"] == name).unwrap();
        assert_eq!(action["confirm"], false, "{name}");
    }
    // Each blocker's action is one the manifest declares.
    assert!(names.contains(&"round"));
    // Each action's command is one bin/whooper takes.
    let w = Whooper::new();
    for action in actions {
        let command: Vec<&str> = action["command"].as_array().unwrap()[1..]
            .iter()
            .map(|a| a.as_str().unwrap())
            .take(1)
            .collect();
        let mut help = command.clone();
        help.push("--help");
        w.ok(&help);
    }
}

#[test]
fn each_agents_cli_is_needed_only_when_a_job_runs_on_it_and_the_page_speaks_the_manifests_protocol()
{
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let manifest = read_json(&root.join("prompting-time-plugin.json"));
    let conditional: Vec<Value> = manifest["prerequisites"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|p| p.get("only_when").is_some())
        .map(|p| json!([p["command"], p["only_when"]]))
        .collect();
    assert_eq!(
        conditional,
        ["claude", "devin"]
            .map(|a| json!([a, {"setting": "jobs", "value": a}]))
            .to_vec()
    );
    let settings = read_json(&root.join("schemas/settings.json"));
    assert!(settings["properties"].get("jobs").is_some());
    assert!(root.join("page/index.html").exists());
    let protocol = fs::read_to_string(root.join("page/src/protocol.ts")).unwrap();
    assert!(protocol.contains(&format!(
        "export const PROTOCOL_VERSION = {};",
        manifest["page"]["protocol"]
    )));
}
