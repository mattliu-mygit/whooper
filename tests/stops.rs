//! Black-box tests of how Whooper's commands stop: an interrupted review, a setup or teardown script
//! that ignores TERM, a stop of whooper run before Prompting Time's SIGKILL, a second signal, a round
//! stopped during a review, a skip while a round runs the review, and a failed command's fixed reason.

mod common;

use std::fs;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::time::{Duration, Instant};

use common::*;
use serde_json::{Value, json};

fn pid_in(path: &Path) -> i32 {
    fs::read_to_string(path).unwrap().trim().parse().unwrap()
}

fn kill_groups(pids: &[i32]) {
    for &pid in pids {
        unsafe {
            libc::kill(-pid, libc::SIGKILL);
            libc::kill(pid, libc::SIGKILL);
        }
    }
}

/// Starts whooper with args in a process group of its own, its output in the file log.
fn start(w: &Whooper, args: &[&str], log: &Path, env: &[(&str, &str)]) -> Child {
    let out = fs::File::create(log).unwrap();
    w.command(args)
        .envs(env.iter().copied())
        .process_group(0)
        .stdout(out.try_clone().unwrap())
        .stderr(out)
        .spawn()
        .unwrap()
}

/// Waits up to seconds for child to end and returns its exit code, or None.
fn ends(child: &mut Child, seconds: u64) -> Option<i32> {
    let deadline = Instant::now() + Duration::from_secs(seconds);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            return status.code();
        }
        if Instant::now() > deadline {
            return None;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

fn set_script(w: &Whooper, name: &str, body: &str) {
    let table = "[repos.\"sample-org/plain-repo\"]\n";
    let line = format!("{name} = \"{name}.sh\"\n");
    let text = fs::read_to_string(&w.config).unwrap();
    if !text.contains(&format!("{table}{line}")) && !text.contains(&line) {
        w.set_config(table, &format!("{table}{line}"));
    }
    script(&w.config.parent().unwrap().join(format!("{name}.sh")), body);
}

/// Has gh report a review request for PR 12 of sample-org/plain-repo at head, slackdump an ask
/// channel with no posts, and claude run review, lines of sh, in a review run, which starts in the
/// head's worktree, and find no asks in a poll run.
fn fake_round(w: &Whooper, head: &str, review: &str) {
    let mut p = pr(12, &[LOGIN], vec![]);
    p["url"] = json!("https://github.com/sample-org/plain-repo/pull/12");
    p["repository"]["nameWithOwner"] = json!("sample-org/plain-repo");
    p["headRefOid"] = json!(head);
    fs::write(
        w.fakes.join("sample-org-plain-repo-pull-12.json"),
        p.to_string(),
    )
    .unwrap();
    w.fake("gh", "case \"$*\" in\n  \"search prs user-review-requested:sam-example --owner sample-org\"*) echo https://github.com/sample-org/plain-repo/pull/12 ;;\n  \"search prs \"*) ;;\n  \"api graphql \"*) for arg; do case \"$arg\" in url=https://github.com/*) cat \"${0%/*}/$(echo \"${arg#url=https://github.com/}\" | tr / -).json\"; exit ;; esac; done; exit 1 ;;\n  \"api repos/sample-org/plain-repo/pulls/12 \"*) cat \"${0%/*}/head\" ;;\n  *) exit 1 ;;\nesac");
    fs::write(w.fakes.join("head"), format!("{head}\n")).unwrap();
    fs::write(w.fakes.join("dump.json"), r#"{"messages": []}"#).unwrap();
    w.fake("slackdump", "while [ $# -gt 1 ]; do [ \"$1\" = -o ] && out=\"$2\"; shift; done\nmkdir -p \"$out\" && cp \"${0%/*}/dump.json\" \"$out/$1.json\"");
    fs::write(
        w.fakes.join("answer.json"),
        json!({"type": "result", "subtype": "success", "structured_output": {"asks": [], "not_asks": []}})
            .to_string(),
    )
    .unwrap();
    w.fake(
        "claude",
        &format!("case \"$PWD\" in\n  */head)\n{review}\n  ;;\n  *) cat \"${{0%/*}}/answer.json\" ;;\nesac"),
    );
}

fn head_of(item: &Path) -> String {
    read_json(item)["head_sha"].as_str().unwrap().to_string()
}

fn only_run(w: &Whooper) -> PathBuf {
    let runs: Vec<PathBuf> = fs::read_dir(w.state.join("runs"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    assert_eq!(runs.len(), 1, "{runs:?}");
    runs[0].clone()
}

#[test]
fn an_interrupted_review_saves_that_it_was_interrupted() {
    let w = Whooper::new();
    let (item, _) = w.pull_request(&[("a.txt", "1\n")], &[("a.txt", "2\n")]);
    let agent = w.root().join("agent.pid");
    w.fake_claude(&format!("echo $$ > '{}'; exec sleep 30;", agent.display()));
    // The teardown script copies the PR's record as it finds it.
    let record = w.state.join(format!(
        "prs/sample-org-plain-repo-12/{}.first_review.json",
        head_of(&item)
    ));
    let at_teardown = w.root().join("record-at-teardown.json");
    set_script(
        &w,
        "teardown",
        &format!("cp '{}' '{}'", record.display(), at_teardown.display()),
    );
    let mut review = start(
        &w,
        &["review", item.to_str().unwrap()],
        &w.root().join("log"),
        &[],
    );
    wait_for("the review run to start", || agent.exists());
    // README.md stops a review with a TERM to whooper review alone.
    unsafe { libc::kill(review.id() as i32, libc::SIGTERM) };
    assert_eq!(ends(&mut review, 30), Some(130));
    let run = only_run(&w);
    assert_eq!(
        fs::read_to_string(run.join("failure.txt")).unwrap(),
        "the review was interrupted\n"
    );
    assert_eq!(read_json(&run.join("failure.json"))["id"], "unknown");
    // The record counted the failure before the teardown script ran, so a SIGKILL that cuts the
    // cleanup short there leaves it right.
    let seen = read_json(&at_teardown);
    assert_eq!(
        (&seen["status"], &seen["failures"]),
        (&json!("failed"), &json!(1))
    );
    assert!(ends_within(pid_in(&agent), 5));
}

#[test]
fn a_stop_reaches_the_setup_or_teardown_script_and_kills_it_4_seconds_later_if_it_ignores_term() {
    // Here the script ignores TERM and waits 10 minutes. whooper review stops it, with a SIGKILL once
    // it ignores the TERM for 4 seconds, and exits within 8 seconds: 130 when the stop came during the
    // setup script, whose review it interrupted, and 1 when it came during the teardown script of a
    // review that had already failed, which then removes its worktrees and lock as usual.
    for (hung, code) in [("setup", 130), ("teardown", 1)] {
        let w = Whooper::new();
        let (item, _) = w.pull_request(&[("a.txt", "1\n")], &[("a.txt", "2\n")]);
        w.fake_claude("exit 1;");
        let pid = w.root().join("hung.pid");
        for name in ["setup", "teardown"] {
            let body = if name == hung {
                format!(
                    "echo $$ > '{}'\ntrap '' TERM\nexec sleep 600",
                    pid.display()
                )
            } else {
                "exit 0".into()
            };
            set_script(&w, name, &body);
        }
        let log = w.root().join("log");
        let mut review = start(&w, &["review", item.to_str().unwrap()], &log, &[]);
        wait_for("the script to start", || {
            pid.exists() || review.try_wait().unwrap().is_some()
        });
        let script = pid_in(&pid);
        unsafe { libc::kill(review.id() as i32, libc::SIGTERM) };
        let exited = ends(&mut review, 8);
        let script_ended = ends_within(script, 2);
        let run = only_run(&w);
        let lock = w.state.join("locks/sample-org-plain-repo-12");
        let result = (
            exited,
            script_ended,
            lock.exists(),
            run.join("head").exists(),
            run.join("base").exists(),
        );
        kill_groups(&[script, review.id() as i32]);
        let _ = review.wait();
        assert_eq!(
            result,
            (Some(code), true, false, false, false),
            "{hung}: {}",
            fs::read_to_string(&log).unwrap()
        );
    }
}

#[test]
fn a_stop_of_whooper_run_ends_its_round_review_and_run_before_prompting_times_sigkill() {
    // Prompting Time stops whooper run with a TERM to its process group and, 5 seconds later, a SIGKILL.
    // The round and the review run in process groups of their own, which neither signal reaches. Here
    // either the review run ignores TERM, or the teardown script does and waits 10 minutes.
    for (what, run_ignores, teardown_hangs) in [
        ("the run ignores TERM", true, false),
        ("the teardown hangs", false, true),
    ] {
        let w = Whooper::new();
        let (item, _) = w.pull_request(&[("a.txt", "1\n")], &[("a.txt", "2\n")]);
        let pids = w.root().join("pids");
        fs::create_dir_all(&pids).unwrap();
        fake_round(
            &w,
            &head_of(&item),
            &format!(
                "echo $$ > '{}/run'\n{}exec sleep 600",
                pids.display(),
                if run_ignores { "trap '' TERM\n" } else { "" }
            ),
        );
        set_script(
            &w,
            "teardown",
            &format!(
                "echo $$ > '{}/teardown'\n{}",
                pids.display(),
                if teardown_hangs {
                    "trap '' TERM\nexec sleep 600"
                } else {
                    "exit 0"
                }
            ),
        );
        let log = w.root().join("log");
        let mut run = start(&w, &["run"], &log, &[]);
        wait_for("the review run to start", || {
            pids.join("run").exists() || run.try_wait().unwrap().is_some()
        });
        let mut started = vec![
            ("round", pid_in(&w.state.join("round.lock/pid"))),
            (
                "review",
                pid_in(&w.state.join("locks/sample-org-plain-repo-12/pid")),
            ),
            ("the run", pid_in(&pids.join("run"))),
        ];
        unsafe { libc::killpg(run.id() as i32, libc::SIGTERM) };
        let exited = ends(&mut run, 5);
        if exited.is_none() {
            unsafe { libc::killpg(run.id() as i32, libc::SIGKILL) };
            let _ = run.wait();
        }
        if pids.join("teardown").exists() {
            started.push(("the teardown script", pid_in(&pids.join("teardown"))));
        }
        if teardown_hangs {
            assert!(
                started.iter().any(|(n, _)| *n == "the teardown script"),
                "{what}"
            );
        }
        let deadline = Instant::now() + Duration::from_secs(3);
        let mut left: Vec<&str>;
        loop {
            left = started
                .iter()
                .filter(|(_, p)| alive(*p))
                .map(|(n, _)| *n)
                .collect();
            if left.is_empty() || Instant::now() > deadline {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        kill_groups(&started.iter().map(|(_, p)| *p).collect::<Vec<_>>());
        assert_eq!(
            (exited, left),
            (Some(130), vec![]),
            "{what}: {}",
            fs::read_to_string(&log).unwrap()
        );
    }
}

#[test]
fn a_second_signal_while_whooper_run_or_a_round_stops_its_child_leaves_no_lock_behind() {
    // The review run ignores TERM, so each stop of the command's child takes 4 seconds, and the test
    // sends a second TERM to the command's group meanwhile.
    for (command, lock) in [("run", "run.lock"), ("round", "round.lock")] {
        let w = Whooper::new();
        let (item, _) = w.pull_request(&[("a.txt", "1\n")], &[("a.txt", "2\n")]);
        let agent = w.root().join("agent.pid");
        fake_round(
            &w,
            &head_of(&item),
            &format!(
                "echo $$ > '{}'\ntrap '' TERM\nexec sleep 600",
                agent.display()
            ),
        );
        let log = w.root().join("log");
        let mut child = start(&w, &[command], &log, &[]);
        wait_for("the review run to start", || {
            agent.exists() || child.try_wait().unwrap().is_some()
        });
        let agent = pid_in(&agent);
        unsafe { libc::killpg(child.id() as i32, libc::SIGTERM) };
        std::thread::sleep(Duration::from_millis(1000));
        unsafe { libc::killpg(child.id() as i32, libc::SIGTERM) };
        let exited = ends(&mut child, 12);
        kill_groups(&[agent, child.id() as i32]);
        let _ = child.wait();
        assert_eq!(
            (exited, w.state.join(lock).exists()),
            (Some(130), false),
            "{command}: {}",
            fs::read_to_string(&log).unwrap()
        );
        if command == "round" {
            assert_eq!(
                read_json(&w.state.join("last-round.json"))["result"],
                "interrupted"
            );
        }
    }
}

#[test]
fn a_round_stopped_during_a_review_reports_that_review_as_interrupted() {
    let w = Whooper::new();
    let (item, _) = w.pull_request(&[("a.txt", "1\n")], &[("a.txt", "2\n")]);
    let agent = w.root().join("agent.pid");
    fake_round(
        &w,
        &head_of(&item),
        &format!("echo $$ > '{}'\nexec sleep 600", agent.display()),
    );
    let log = w.root().join("log");
    let mut run = start(&w, &["run"], &log, &[]);
    wait_for("the review run to start", || agent.exists());
    let status = w.status();
    let running = status["round"].clone();
    assert_eq!(
        (&running["running"], &running["reviewing"]),
        (&json!(true), &json!("sample-org/plain-repo#12")),
        "{status}"
    );
    unsafe { libc::killpg(run.id() as i32, libc::SIGTERM) };
    assert_eq!(
        ends(&mut run, 10),
        Some(130),
        "{}",
        fs::read_to_string(&log).unwrap()
    );
    let last = read_json(&w.state.join("last-round.json"));
    assert_eq!(
        w.status()["last_round"]["started_at"],
        running["started_at"]
    );
    assert_eq!(
        (&last["result"], &last["selected"], &last["reviews"]),
        (
            &json!("interrupted"),
            &json!(1),
            &json!([{"pr": "sample-org/plain-repo#12", "outcome": "interrupted"}])
        )
    );
    let history: Vec<Value> = fs::read_to_string(w.state.join("rounds.jsonl"))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(history.last().unwrap()["result"], "interrupted");
    assert!(!w.state.join("round.lock").exists());
    assert!(ends_within(pid_in(&agent), 5));
}

#[test]
fn skipping_a_review_a_round_runs_stops_it_quietly_and_holds_back_its_pr_until_undone() {
    let w = Whooper::new();
    let (item, _) = w.pull_request(&[("a.txt", "1\n")], &[("a.txt", "2\n")]);
    let agent = w.root().join("agent.pid");
    fake_round(
        &w,
        &head_of(&item),
        &format!("echo $$ > '{}'\nexec sleep 30", agent.display()),
    );
    let log = w.root().join("log");
    let mut round = start(&w, &["round"], &log, &[]);
    wait_for("the review run to start", || agent.exists());
    let run = only_run(&w);
    let id = run.file_name().unwrap().to_str().unwrap();
    let skipped = w.ok(&["skip", id]);
    assert!(skipped.contains("stopped its review"), "{skipped}");
    // The round goes on, and sends no notice of a review the user stopped.
    assert_eq!(
        ends(&mut round, 60),
        Some(0),
        "{}",
        fs::read_to_string(&log).unwrap()
    );
    let out = fs::read_to_string(&log).unwrap();
    assert!(
        out.contains("review stopped: skipped by the user\n"),
        "{out}"
    );
    assert!(!w.root().join("notifications.log").exists());
    assert_eq!(
        fs::read_to_string(run.join("failure.txt")).unwrap(),
        "the review was interrupted\n"
    );
    let status = w.status();
    let review = &status["reviews"][0];
    assert_eq!(
        (
            &review["outcome"],
            &review["skipped_by_user"],
            &review["needs_you"]
        ),
        (&json!("failed"), &json!(true), &json!(false))
    );
    // The poll still lists the PR, and select drops it until the skip is undone.
    let out = w.run(&["select"]);
    assert_eq!(String::from_utf8_lossy(&out.stdout), "");
    assert!(
        String::from_utf8_lossy(&out.stderr)
            .contains("dropped sample-org/plain-repo#12 first_review: skipped by the user")
    );
    w.ok(&["skip", id, "--undo"]);
    let selected = w.ok(&["select"]);
    let prs: Vec<i64> = selected
        .lines()
        .map(|l| {
            serde_json::from_str::<Value>(l).unwrap()["pr"]
                .as_i64()
                .unwrap()
        })
        .collect();
    assert_eq!(prs, [12]);
}

#[test]
fn a_review_stopped_by_a_failed_command_saves_a_fixed_reason() {
    let w = Whooper::new();
    let (item, _) = w.pull_request(&[("a.txt", "1\n")], &[("a.txt", "2\n")]);
    // origin has no such branch, so the fetch fails once the run directory exists.
    let mut work = read_json(&item);
    work["base_ref"] = json!("no-such-branch");
    fs::write(&item, work.to_string()).unwrap();
    let out = w.run(&["review", item.to_str().unwrap()]);
    let run = only_run(&w);
    assert_eq!(out.status.code(), Some(128));
    // git's own message is in the review's error output, where failure.txt points.
    assert!(
        String::from_utf8_lossy(&out.stderr).contains("couldn't find remote ref no-such-branch")
    );
    assert_eq!(
        fs::read_to_string(run.join("failure.txt")).unwrap(),
        "the review stopped at an unexpected error; see whooper-review's error output\n"
    );
    assert_eq!(read_json(&run.join("failure.json"))["id"], "unknown");
}
