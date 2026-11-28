//! Black-box tests of whooper retry and remove, and of select after them, on review runs of
//! example-org/example-repo#12 and #13. gh is a fake that reports PR 12's head from the file head.

mod common;

use std::fs;
use std::path::PathBuf;
use std::process::Output;

use common::*;
use serde_json::{Value, json};

const RUN_ID: &str = "20310727T010246Z-12";

struct Retry {
    w: Whooper,
    run: PathBuf,
    work: Value,
    ask: Value,
    asked_again: Value,
}

fn failed() -> Value {
    json!({"status": "failed", "failures": 3, "started_at": "2031-07-27T01:02:46+00:00"})
}

impl Retry {
    fn new() -> Retry {
        let w = Whooper::new();
        w.fake(
            "gh",
            r#"case "$*" in
  "api repos/example-org/example-repo/pulls/12 --jq .head.sha") cat "${0%/*}/head" ;;
  *) echo "fake gh: unexpected call: gh $*" >&2; exit 1 ;;
esac"#,
        );
        fs::write(w.fakes.join("head"), "h1\n").unwrap();
        let work = json!({"kind": "first_review", "repo": "example-org/example-repo", "pr": 12, "head_sha": "h1",
            "base_ref": "master", "source": "github_request", "slack": null,
            "reason": "A GitHub review request names sam-example, who has not reviewed this PR."});
        let run = w.state.join("runs").join(RUN_ID);
        fs::create_dir_all(&run).unwrap();
        let slack = json!({"ts": "1943194837.000100", "tagged_user": true,
            "permalink": "https://example.slack.com/archives/C0000000001/p1943194837000100"});
        let mut ask = work.clone();
        ask["pr"] = json!(13);
        ask["head_sha"] = json!("h7");
        ask["source"] = json!("slack_ask");
        ask["slack"] = slack;
        ask["reason"] = json!("Alex asked for a review.");
        let mut asked_again = ask.clone();
        asked_again["slack"]["ts"] = json!("1943198437.000200");
        asked_again["slack"]["permalink"] =
            json!("https://example.slack.com/archives/C0000000001/p1943198437000200");
        let r = Retry {
            w,
            run,
            work,
            ask,
            asked_again,
        };
        r.write_run(&r.work.clone());
        // The review of h1 failed at the setup script, its third failure at h1.
        fs::write(r.run.join("failure.txt"), "/config/setup.sh failed\n").unwrap();
        r.write_record("h1", &failed());
        // The newest poll lists the PR at h1, since its review request stands.
        r.poll(vec![r.work.clone()]);
        r
    }

    fn write_run(&self, work: &Value) {
        let mut ran = work.clone();
        ran["run_id"] = json!(RUN_ID);
        ran["merge_base"] = json!("m1");
        ran["agent"] = json!("claude");
        fs::write(self.run.join("run.json"), ran.to_string()).unwrap();
    }

    fn record(&self, head: &str) -> PathBuf {
        self.w.state.join(format!(
            "prs/example-org-example-repo-12/{head}.first_review.json"
        ))
    }

    fn write_record(&self, head: &str, record: &Value) {
        let path = self.record(head);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, record.to_string()).unwrap();
    }

    fn poll(&self, items: Vec<Value>) {
        fs::create_dir_all(self.w.state.join("poll")).unwrap();
        fs::write(
            self.w.state.join("poll/20310727T020000Z.json"),
            json!({"items": items}).to_string(),
        )
        .unwrap();
    }

    fn retry(&self, args: &[&str]) -> Output {
        let mut all = vec!["retry"];
        all.extend(args);
        self.w.run(&all)
    }

    fn remove(&self, args: &[&str]) -> Output {
        let mut all = vec!["remove"];
        all.extend(args);
        self.w.run(&all)
    }

    fn select(&self) -> Vec<Value> {
        self.w
            .ok(&["select"])
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    }

    /// Leaves item waiting for the next round, as a round does after an earlier review could not reach
    /// Docker: a run with run.json and waiting.json, and the item on the recheck list.
    fn wait(&self, item: &Value, run_id: &str) -> PathBuf {
        let run = self.w.state.join("runs").join(run_id);
        fs::create_dir_all(&run).unwrap();
        let mut ran = item.clone();
        ran["run_id"] = json!(run_id);
        ran["agent"] = json!("claude");
        fs::write(run.join("run.json"), ran.to_string()).unwrap();
        fs::write(
            run.join("waiting.json"),
            r#"{"id": "docker_unavailable", "summary": "Docker isn't running", "scope": "all"}"#,
        )
        .unwrap();
        let list = self.w.state.join("rechecks");
        fs::create_dir_all(&list).unwrap();
        let name = format!(
            "{}-{}.json",
            item["repo"].as_str().unwrap().replace('/', "-"),
            item["pr"]
        );
        fs::write(list.join(name), item.to_string()).unwrap();
        run
    }
}

fn text(o: &Output) -> (Option<i32>, String, String) {
    (
        o.status.code(),
        String::from_utf8_lossy(&o.stdout).into_owned(),
        String::from_utf8_lossy(&o.stderr).into_owned(),
    )
}

const QUEUED: &str = "queued another review of example-org/example-repo#12 at h1 for the next round; bin/whooper round runs that round now\n";

#[test]
fn a_review_that_failed_three_times_is_reviewed_again_in_the_next_round() {
    let r = Retry::new();
    assert_eq!(r.select(), Vec::<Value>::new());
    let result = r.retry(&[RUN_ID]);
    assert_eq!(
        (result.status.code(), text(&result).1),
        (Some(0), QUEUED.into())
    );
    let mut retry = failed();
    retry["status"] = json!("retry");
    assert_eq!(read_json(&r.record("h1")), retry);
    // The poll lists the PR, so the round reviews the poll's item, and the list's item is done.
    assert_eq!(r.select(), std::slice::from_ref(&r.work));
    assert!(
        !r.w.state
            .join("rechecks/example-org-example-repo-12.json")
            .exists()
    );
    assert_eq!(r.select(), std::slice::from_ref(&r.work));
}

#[test]
fn a_rows_menu_retries_a_review_with_item() {
    let r = Retry::new();
    let result = r.retry(&["--item", RUN_ID]);
    assert_eq!(
        (result.status.code(), text(&result).1),
        (Some(0), QUEUED.into())
    );
    assert_eq!(read_json(&r.record("h1"))["status"], "retry");
}

#[test]
fn a_review_that_left_its_record_running_is_reviewed_again_once_no_live_review_holds_its_lock() {
    let r = Retry::new();
    let mut running = failed();
    running["status"] = json!("running");
    running["failures"] = json!(1);
    r.write_record("h1", &running);
    let lock = r.w.state.join("locks/example-org-example-repo-12");
    let mut holder = r.w.holder("whooper-review");
    r.w.hold(&lock, holder.id());
    assert_eq!(r.select(), Vec::<Value>::new());
    assert_eq!(read_json(&r.record("h1")), running);
    holder.kill().unwrap();
    holder.wait().unwrap();
    let mut gone = std::process::Command::new("true").spawn().unwrap();
    gone.wait().unwrap();
    r.w.hold(&lock, gone.id());
    assert_eq!(r.select(), std::slice::from_ref(&r.work));
    running["status"] = json!("failed");
    running["failures"] = json!(2);
    assert_eq!(read_json(&r.record("h1")), running);
}

#[test]
fn a_retry_reviews_the_head_the_pr_has_now_whatever_brought_it_in() {
    let r = Retry::new();
    let mut work = r.work.clone();
    work["source"] = json!("slack_ask");
    work["slack"] = r.ask["slack"].clone();
    work["reason"] = json!("Alex asked for a review.");
    r.write_run(&work);
    fs::remove_file(r.run.join("failure.txt")).unwrap();
    r.write_record(
        "h1",
        &json!({"status": "done", "failures": 0, "started_at": "2031-07-27T01:02:46+00:00"}),
    );
    r.poll(vec![]);
    assert_eq!(r.select(), Vec::<Value>::new());
    assert_eq!(r.retry(&[RUN_ID]).status.code(), Some(0));
    assert_eq!(r.select(), [work.clone()]);
    fs::write(r.w.fakes.join("head"), "h2\n").unwrap();
    let result = r.retry(&[RUN_ID]);
    assert_eq!(result.status.code(), Some(0));
    assert!(
        text(&result)
            .1
            .contains("example-org/example-repo#12 at h2")
    );
    let mut moved = work.clone();
    moved["head_sha"] = json!("h2");
    assert_eq!(r.select(), [moved]);
    assert_eq!(read_json(&r.record("h2"))["status"], "retry");
}

#[test]
fn retry_refuses_an_unknown_run_a_review_still_running_and_a_run_without_run_json() {
    let r = Retry::new();
    let runs = r.w.state.join("runs");
    for bad in [
        "20310101T000000Z-1".to_string(),
        "../whooper".into(),
        format!("../runs/{RUN_ID}"),
    ] {
        assert_eq!(
            text(&r.retry(&[&bad])),
            (
                Some(1),
                String::new(),
                format!("whooper-retry: no run {bad} in {}\n", runs.display())
            )
        );
    }
    let lock = r.w.state.join("locks/example-org-example-repo-12");
    let mut holder = r.w.holder("whooper-review");
    r.w.hold(&lock, holder.id());
    fs::write(lock.join("run_id"), format!("{RUN_ID}\n")).unwrap();
    assert_eq!(
        text(&r.retry(&[RUN_ID])),
        (
            Some(1),
            String::new(),
            "whooper-retry: the review is still running; retry it once it ends\n".into()
        )
    );
    holder.kill().unwrap();
    holder.wait().unwrap();
    let stopped = runs.join("20310727T000000Z-13");
    fs::create_dir_all(&stopped).unwrap();
    fs::write(
        stopped.join("failure.txt"),
        "the review stopped at an unexpected error\n",
    )
    .unwrap();
    assert_eq!(
        text(&r.retry(&["20310727T000000Z-13"])),
        (
            Some(1),
            String::new(),
            format!(
                "whooper-retry: cannot read {}/run.json, which names the PR to review again\n",
                stopped.display()
            )
        )
    );
    for args in [
        vec![],
        vec![RUN_ID, "20310727T000000Z-13"],
        vec!["--item"],
        vec!["--item", "--item"],
        vec!["--item", RUN_ID, "20310727T000000Z-13"],
        vec![RUN_ID, "--item"],
    ] {
        assert_eq!(
            text(&r.retry(&args)),
            (
                Some(1),
                String::new(),
                "usage: whooper retry [--item] <run-id>\n".into()
            ),
            "{args:?}"
        );
    }
    assert!(!r.w.state.join("rechecks").exists());
    assert_eq!(read_json(&r.record("h1")), failed());
}

#[test]
fn a_removed_review_that_failed_or_waits_is_not_reviewed_again_until_a_new_request_for_its_pr() {
    let r = Retry::new();
    let mut once = failed();
    once["failures"] = json!(1);
    r.write_record("h1", &once);
    let waits = r.wait(&r.ask, "20310727T030000Z-13");
    assert_eq!(r.select(), [r.work.clone(), r.ask.clone()]);
    for id in [RUN_ID, "20310727T030000Z-13"] {
        assert_eq!(r.remove(&[id]).status.code(), Some(0));
    }
    assert_eq!(text(&r.w.run(&["select"])), (Some(0), String::new(),
        "whooper-select: dropped example-org/example-repo#12 first_review: removed by the user\n\
         whooper-select: dropped example-org/example-repo#13 first_review: removed by the user\n".into()));
    assert!(
        !r.w.state
            .join("rechecks/example-org-example-repo-13.json")
            .exists()
    );
    assert!(r.run.join("failure.txt").exists() && waits.join("waiting.json").exists());
    assert_eq!(r.select(), Vec::<Value>::new());
    // A new Slack post asks for PR 13 again, and PR 12 gets new commits: the next round reviews both.
    let mut moved = r.work.clone();
    moved["head_sha"] = json!("h2");
    r.poll(vec![moved.clone(), r.asked_again.clone()]);
    assert_eq!(r.select(), [moved, r.asked_again.clone()]);
}

#[test]
fn retrying_a_removed_review_unmarks_it() {
    let r = Retry::new();
    assert_eq!(r.remove(&[RUN_ID]).status.code(), Some(0));
    let result = r.retry(&[RUN_ID]);
    assert_eq!(
        (result.status.code(), text(&result).1),
        (
            Some(0),
            format!("unmarked {RUN_ID} as removed, and {QUEUED}")
        )
    );
    assert!(!r.run.join("removed-by-user").exists());
    assert_eq!(r.select(), std::slice::from_ref(&r.work));
}

#[test]
fn undoing_the_removal_of_a_review_that_waits_puts_it_back_on_the_recheck_list() {
    let r = Retry::new();
    r.poll(vec![]);
    let id = "20310727T030000Z-13";
    r.wait(&r.ask, id);
    let listed = r.w.state.join("rechecks/example-org-example-repo-13.json");
    assert_eq!(r.remove(&[id]).status.code(), Some(0));
    assert_eq!(
        text(&r.remove(&[id, "--undo"])).1,
        format!("unmarked {id} (example-org/example-repo#13) as removed\n")
    );
    assert_eq!(read_json(&listed), r.ask);
    assert_eq!(r.remove(&[id]).status.code(), Some(0));
    assert_eq!(r.select(), Vec::<Value>::new());
    assert!(!listed.exists());
    assert_eq!(
        text(&r.remove(&[id, "--undo"])).1,
        format!(
            "unmarked {id} (example-org/example-repo#13) as removed, and put it back on the recheck list for the next round\n"
        )
    );
    assert_eq!(r.select(), std::slice::from_ref(&r.ask));
}
