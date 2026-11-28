//! Black-box tests of the reviews whooper status lists and whooper show prints, read from review runs
//! of example-org/example-repo in a throwaway state folder, with times in Los Angeles, and of skip and
//! remove, which mark them.

mod common;

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Output;
use std::time::{Duration, SystemTime};

use common::*;
use jiff::{Timestamp, tz::TimeZone};
use serde_json::{Value, json};

const TITLE: &str = "Return 422 for bad order JSON";
const DIFF: &str = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -3 +3 @@\n-fast\n+quick\n";
const HOUR: i64 = 3600;
const MIN: i64 = 60;

fn cause(id: &str) -> Value {
    let (summary, scope) = match id {
        "docker_unavailable" => ("Docker isn't running", "all"),
        "setup_failed" => ("The repo's setup script failed", "all"),
        "agent_no_findings" => ("The review agent left no usable findings", "pr"),
        "time_limit" => ("The review ran past its time limit", "pr"),
        _ => ("The review stopped unexpectedly", "pr"),
    };
    json!({"id": id, "summary": summary, "scope": scope})
}

fn finding_of(id: &str, kind: &str) -> Value {
    json!({"id": id, "path": "README.md", "line": 3, "side": "RIGHT", "title": "Old flag name",
           "draft": "**nit:** This line still names the old --fast flag.", "kind": kind})
}

fn nit() -> Value {
    finding_of("2", "nit")
}

fn bug() -> Value {
    finding_of("1", "caller_visible_bug")
}

fn findings_json(verdict: &str, findings: Vec<Value>, skip_reason: &str) -> Value {
    json!({"status": if skip_reason.is_empty() { "reviewed" } else { "skipped" }, "skip_reason": skip_reason,
           "approval": {"verdict": verdict, "reason": "Ran the orders tests."}, "findings": findings})
}

struct Reviews {
    w: Whooper,
    now: i64,
    holders: std::cell::RefCell<Vec<std::process::Child>>,
}

impl Drop for Reviews {
    fn drop(&mut self) {
        for h in self.holders.borrow_mut().iter_mut() {
            let _ = h.kill();
            let _ = h.wait();
        }
    }
}

fn run_id_at(t: i64, pr: i64) -> String {
    let ts = Timestamp::from_second(t).unwrap();
    format!("{}-{pr}", ts.strftime("%Y%m%dT%H%M%SZ"))
}

fn started(run: &Path) -> i64 {
    let name = run.file_name().unwrap().to_string_lossy().into_owned();
    let stamp = name.split('-').next().unwrap();
    let dt = jiff::civil::DateTime::strptime("%Y%m%dT%H%M%SZ", stamp).unwrap();
    dt.to_zoned(TimeZone::UTC).unwrap().timestamp().as_second()
}

fn local(t: i64) -> String {
    let zone = TimeZone::get("America/Los_Angeles").unwrap();
    Timestamp::from_second(t)
        .unwrap()
        .to_zoned(zone)
        .strftime("%Y-%m-%dT%H:%M:%S%:z")
        .to_string()
}

fn pr_of(run: &Path) -> i64 {
    let name = run.file_name().unwrap().to_string_lossy().into_owned();
    name.rsplit('-').next().unwrap().parse().unwrap()
}

impl Reviews {
    fn new() -> Reviews {
        Reviews {
            w: Whooper::new(),
            now: Timestamp::now().as_second(),
            holders: Default::default(),
        }
    }

    fn command(&self, args: &[&str]) -> Output {
        self.w
            .command(args)
            .env("TZ", "America/Los_Angeles")
            .output()
            .unwrap()
    }

    fn make_run(&self, ago: i64, pr: i64, run_json: bool, repo: &str) -> PathBuf {
        let run = self
            .w
            .state
            .join("runs")
            .join(run_id_at(self.now - ago, pr));
        fs::create_dir_all(&run).unwrap();
        if run_json {
            let name = run.file_name().unwrap().to_string_lossy().into_owned();
            fs::write(run.join("run.json"), json!({"kind": "first_review", "repo": repo, "pr": pr, "head_sha": "h1",
                "base_ref": "master", "source": "github_request", "slack": null, "reason": "r", "run_id": name,
                "merge_base": "m1", "as_of": null, "agent": "claude", "skills": {}}).to_string()).unwrap();
        }
        run
    }

    fn run(&self, ago: i64, pr: i64) -> PathBuf {
        self.make_run(ago, pr, true, "example-org/example-repo")
    }

    /// Writes the run's file name, as written minutes after the run started.
    fn write(&self, run: &Path, name: &str, content: &str, minutes: i64) {
        let path = run.join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, content).unwrap();
        let when =
            SystemTime::UNIX_EPOCH + Duration::from_secs((started(run) + minutes * MIN) as u64);
        fs::File::options()
            .write(true)
            .open(&path)
            .unwrap()
            .set_modified(when)
            .unwrap();
    }

    fn fetched(&self, run: &Path) {
        let pr = pr_of(run);
        self.write(run, "diff.patch", DIFF, 1);
        self.write(run, "github.json", &json!({"pr": {"url": format!("https://github.com/example-org/example-repo/pull/{pr}"),
            "number": pr, "title": TITLE, "author": "alice", "body": ""}, "as_of": null, "reviews": [], "comments": []}).to_string(), 1);
    }

    fn finish(&self, run: &Path, verdict: &str, findings: Vec<Value>, skip_reason: &str) {
        self.fetched(run);
        self.write(
            run,
            "findings.json",
            &findings_json(verdict, findings, skip_reason).to_string(),
            20,
        );
    }

    fn hold(&self, run: &Path, pid: Option<u32>) {
        let pid = pid.unwrap_or_else(|| {
            let h = self.w.holder("whooper-review");
            let id = h.id();
            self.holders.borrow_mut().push(h);
            id
        });
        let lock = self
            .w
            .state
            .join(format!("locks/example-org-example-repo-{}", pr_of(run)));
        self.w.hold(&lock, pid);
        fs::write(
            lock.join("run_id"),
            format!("{}\n", run.file_name().unwrap().to_string_lossy()),
        )
        .unwrap();
    }

    fn stopped(&self, ago: i64, pr: i64, id: &str) -> PathBuf {
        let run = self.run(ago, pr);
        self.fetched(&run);
        self.write(&run, "failure.txt", "the reason whooper review gave\n", 2);
        self.write(&run, "failure.json", &cause(id).to_string(), 2);
        run
    }

    fn waiting(&self, ago: i64, pr: i64, id: &str) -> PathBuf {
        let run = self.make_run(ago, pr, false, "");
        let name = run.file_name().unwrap().to_string_lossy().into_owned();
        fs::write(run.join("run.json"), json!({"kind": "first_review", "repo": "example-org/example-repo", "pr": pr,
            "head_sha": "h1", "base_ref": "master", "source": "github_request", "slack": null, "reason": "r",
            "run_id": name, "agent": "claude"}).to_string()).unwrap();
        fs::write(run.join("waiting.json"), cause(id).to_string()).unwrap();
        run
    }

    fn status(&self) -> Value {
        let out = self.command(&["status"]);
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        serde_json::from_slice(&out.stdout).unwrap()
    }

    fn reviews(&self) -> Value {
        self.status()["reviews"].clone()
    }

    /// What status lists for the run, given the fields that differ from a review that ended with a
    /// look_first verdict and no findings 20 minutes after it started and waits for the user.
    fn review(&self, run: &Path, fields: Value) -> Value {
        let (start, pr) = (started(run), pr_of(run));
        let mut r = json!({"run_id": run.file_name().unwrap().to_string_lossy(), "repo": "example-org/example-repo",
            "pr": pr, "title": TITLE, "author": "alice",
            "url": format!("https://github.com/example-org/example-repo/pull/{pr}"), "started_at": local(start),
            "ended_at": local(start + 20 * MIN), "outcome": "reviewed", "verdict": "look_first", "skipped": false,
            "findings": 0, "safe_to_approve": false, "submitted": null, "needs_you": true, "cause": null,
            "head_sha": "h1", "pr_head_sha": null, "skipped_by_user": false, "removed_by_user": false});
        for (k, v) in fields.as_object().unwrap() {
            r[k] = v.clone();
        }
        r
    }

    fn shown(&self, run: &Path) -> Value {
        let out = self.command(&["show", &run.file_name().unwrap().to_string_lossy()]);
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        serde_json::from_slice(&out.stdout).unwrap()
    }

    fn name(run: &Path) -> String {
        run.file_name().unwrap().to_string_lossy().into_owned()
    }
}

fn text(o: &Output) -> (Option<i32>, String, String) {
    (
        o.status.code(),
        String::from_utf8_lossy(&o.stdout).into_owned(),
        String::from_utf8_lossy(&o.stderr).into_owned(),
    )
}

fn keys(v: &Value, keys: &[&str]) -> Value {
    Value::Object(
        keys.iter()
            .map(|k| (k.to_string(), v[*k].clone()))
            .collect(),
    )
}

const REQUEST: &str = r#"{"commit_id": "h1", "event": "APPROVE", "body": "", "comments": []}"#;

#[test]
fn status_lists_the_reviews_of_the_last_30_days_newest_first() {
    let r = Reviews::new();
    let reviewed = r.run(HOUR, 12);
    r.finish(&reviewed, "look_first", vec![bug(), nit()], "");
    let submitted = r.run(3 * HOUR, 13);
    r.finish(&submitted, "approve_once_ci_passes", vec![nit()], "");
    r.write(
        &submitted,
        "submit.json",
        &format!(r#"{{"status": "posted", "request": {REQUEST}, "response": {{"id": 7001}}}}"#),
        30,
    );
    // A submit stopped while gh posted is not submitted.
    let posting = r.run(4 * HOUR, 19);
    r.finish(&posting, "approve_once_ci_passes", vec![nit()], "");
    r.write(
        &posting,
        "submit.json",
        &format!(r#"{{"status": "posting", "request": {REQUEST}}}"#),
        30,
    );
    let skipped = r.run(5 * HOUR, 14);
    r.finish(&skipped, "look_first", vec![], "Not small or mechanical.");
    let failed = r.run(7 * HOUR, 15);
    r.fetched(&failed);
    r.write(
        &failed,
        "findings.invalid.json",
        &json!({"findings": [bug()]}).to_string(),
        20,
    );
    r.write(
        &failed,
        "failure.txt",
        "the review run's findings.json does not fit the schema\n",
        21,
    );
    r.write(
        &failed,
        "failure.json",
        &cause("agent_no_findings").to_string(),
        21,
    );
    // A Devin review can pass its time limit after writing findings that fit.
    let timed_out = r.run(9 * HOUR, 18);
    r.finish(&timed_out, "look_first", vec![bug()], "");
    r.write(
        &timed_out,
        "failure.txt",
        "the review run passed its 45-minute limit\n",
        45,
    );
    r.write(
        &timed_out,
        "failure.json",
        &cause("time_limit").to_string(),
        45,
    );
    let month = r.run(29 * 24 * HOUR, 16);
    r.finish(&month, "look_first", vec![], "");
    r.finish(&r.run(31 * 24 * HOUR, 17), "look_first", vec![], "");
    assert_eq!(
        r.reviews(),
        json!([
            r.review(&reviewed, json!({"findings": 2})),
            r.review(&submitted, json!({"verdict": "approve_once_ci_passes", "findings": 1, "safe_to_approve": true,
                "needs_you": false, "submitted": {"event": "APPROVE", "at": local(started(&submitted) + 30 * MIN)}})),
            r.review(&posting, json!({"verdict": "approve_once_ci_passes", "findings": 1, "safe_to_approve": true})),
            r.review(&skipped, json!({"skipped": true})),
            r.review(&failed, json!({"outcome": "failed", "verdict": null, "cause": cause("agent_no_findings"),
                "ended_at": local(started(&failed) + 21 * MIN)})),
            r.review(&timed_out, json!({"outcome": "failed", "findings": 1, "cause": cause("time_limit"),
                "ended_at": local(started(&timed_out) + 45 * MIN)})),
            r.review(&month, json!({})),
        ])
    );
}

#[test]
fn status_lists_each_prs_newest_review_as_an_item_of_status_format_2() {
    let r = Reviews::new();
    let ready = r.run(HOUR, 12);
    r.finish(&ready, "approve_once_ci_passes", vec![nit()], "");
    // An older run of the same PR, which the newer one supersedes, lists as no item.
    r.finish(&r.run(5 * HOUR, 12), "look_first", vec![bug()], "");
    let look = r.run(2 * HOUR, 13);
    r.finish(&look, "look_first", vec![], "");
    let failed = r.stopped(3 * HOUR, 14, "time_limit");
    let submitted = r.run(4 * HOUR, 17);
    r.finish(&submitted, "approve_once_ci_passes", vec![nit()], "");
    r.write(
        &submitted,
        "submit.json",
        &format!(r#"{{"status": "posted", "request": {REQUEST}, "response": {{"id": 7001}}}}"#),
        30,
    );
    let removed = r.run(6 * HOUR, 18);
    r.finish(&removed, "look_first", vec![], "");
    fs::write(removed.join("removed-by-user"), "").unwrap();
    let reviewing = r.run(10 * MIN, 15);
    r.fetched(&reviewing);
    r.hold(&reviewing, None);
    // A review that waits read no github.json yet, so its title is its PR.
    let waits = r.waiting(30 * MIN, 16, "docker_unavailable");
    let status = r.status();
    let ids: Vec<(Value, Value, Value)> = status["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| (i["id"].clone(), i["group"].clone(), i["dot"].clone()))
        .collect();
    let want: Vec<(Value, Value, Value)> = [
        (&reviewing, "reviewing", "running"),
        (&waits, "waiting", "idle"),
        (&ready, "ready", "success"),
        (&look, "look", "attention"),
        (&failed, "failed", "error"),
        // Finished items come after the current ones.
        (&submitted, "done", "idle"),
    ]
    .iter()
    .map(|(run, g, d)| (json!(Reviews::name(run)), json!(g), json!(d)))
    .collect();
    assert_eq!(ids, want);
    let items = status["items"].as_array().unwrap();
    assert_eq!(items[0]["actions"], json!(["skip"]));
    assert_eq!(items[1]["title"], "example-org/example-repo#16");
    assert_eq!(
        items[1]["detail"],
        "example-repo#16 · waiting · Docker isn't running"
    );
    assert_eq!(items[2]["detail"], "example-repo#12 · @alice · 1 finding");
    assert_eq!(items[2]["actions"], json!(["retry", "skip", "remove"]));
    assert_eq!(
        items[2]["route"],
        format!("/reviews/{}", Reviews::name(&ready))
    );
    assert_eq!(items[2]["updated_at"], local(started(&ready) + 20 * MIN));
    assert_eq!(items[3]["detail"], "example-repo#13 · @alice · no findings");
    assert_eq!(
        items[4]["detail"],
        "example-repo#14 · @alice · The review ran past its time limit"
    );
    assert_eq!(items[4]["updated_at"], local(started(&failed) + 2 * MIN));
    assert_eq!(items[5]["actions"], json!(["retry", "remove"]));
    assert!(
        items[5]["detail"]
            .as_str()
            .unwrap()
            .starts_with("example-repo#17 · @alice · approved ")
    );
    assert_eq!(status["truncated"], false);
    assert_eq!(
        status["blockers"],
        json!([{"id": "docker_unavailable", "title": "Docker isn't running", "dot": "error",
                "route": "/causes/docker_unavailable", "action": "round"}])
    );
    assert_eq!(
        status["agent"],
        json!({"words": "Can't review · Docker isn't running", "dot": "error"})
    );
}

#[test]
fn a_review_runs_while_a_live_review_holds_its_prs_lock_for_it() {
    let r = Reviews::new();
    let earlier = r.run(2 * HOUR, 12);
    r.finish(&earlier, "look_first", vec![], "");
    // A Devin review writes findings.json itself, so its findings do not count yet.
    let current = r.run(30 * MIN, 12);
    r.fetched(&current);
    r.write(
        &current,
        "findings.json",
        &findings_json("look_first", vec![bug()], "").to_string(),
        25,
    );
    r.hold(&current, None);
    // review takes the lock before it makes the run directory, and writes run.json after it fetches.
    let fetching = r.make_run(MIN, 20, false, "");
    r.hold(&fetching, None);
    // A review killed with kill -9 leaves its lock, whose process is gone, and no failure.txt.
    let mut gone = std::process::Command::new("true").spawn().unwrap();
    gone.wait().unwrap();
    let killed = r.run(3 * HOUR, 21);
    r.fetched(&killed);
    r.hold(&killed, Some(gone.id()));
    let running =
        json!({"outcome": "running", "ended_at": null, "verdict": null, "needs_you": false});
    let mut fetching_fields = running.clone();
    for k in ["repo", "url", "title", "author", "head_sha"] {
        fetching_fields[k] = Value::Null;
    }
    assert_eq!(
        r.reviews(),
        json!([
            r.review(&fetching, fetching_fields),
            r.review(&current, running),
            // The run the lock names is newer, so the earlier run of its PR no longer needs the user.
            r.review(&earlier, json!({"needs_you": false})),
            r.review(&killed, json!({"outcome": "failed", "verdict": null, "ended_at": null, "cause": cause("unknown")})),
        ])
    );
}

#[test]
fn a_lock_whose_process_is_now_another_program_is_stale() {
    let r = Reviews::new();
    let reused = r.run(3 * HOUR, 12);
    r.fetched(&reused);
    let mut other = std::process::Command::new("sleep")
        .arg("30")
        .spawn()
        .unwrap();
    r.hold(&reused, Some(other.id()));
    let reviews = r.reviews();
    other.kill().unwrap();
    other.wait().unwrap();
    assert_eq!(
        reviews,
        json!([r.review(&reused, json!({"outcome": "failed", "verdict": null, "ended_at": null, "cause": cause("unknown")}))])
    );
}

#[test]
fn a_newer_run_of_a_pr_supersedes_its_older_runs() {
    let r = Reviews::new();
    let older = r.run(5 * HOUR, 12);
    r.finish(&older, "look_first", vec![bug()], "");
    let newer = r.run(HOUR, 12);
    r.finish(&newer, "look_first", vec![], "");
    // The same number in another repo is another PR.
    let elsewhere = r.make_run(3 * HOUR, 12, true, "sample-org/plain-repo");
    r.finish(&elsewhere, "look_first", vec![], "");
    // A newer run supersedes a failed one too, from the time it starts.
    let failed = r.run(6 * HOUR, 13);
    r.fetched(&failed);
    r.write(
        &failed,
        "failure.txt",
        "the review run passed its 45-minute limit\n",
        45,
    );
    r.write(
        &failed,
        "failure.json",
        &cause("time_limit").to_string(),
        45,
    );
    let running = r.run(10 * MIN, 13);
    r.fetched(&running);
    r.hold(&running, None);
    assert_eq!(
        r.reviews(),
        json!([
            r.review(&running, json!({"outcome": "running", "ended_at": null, "verdict": null, "needs_you": false})),
            r.review(&newer, json!({})),
            r.review(&elsewhere, json!({"repo": "sample-org/plain-repo", "url": "https://github.com/sample-org/plain-repo/pull/12"})),
            r.review(&older, json!({"findings": 1, "needs_you": false})),
            r.review(&failed, json!({"outcome": "failed", "verdict": null, "needs_you": false, "cause": cause("time_limit"),
                "ended_at": local(started(&failed) + 45 * MIN)})),
        ])
    );
}

#[test]
fn a_run_directory_whooper_cannot_read_lists_as_a_failed_review() {
    let r = Reviews::new();
    let mut unreadable = vec![];
    for (hours, run_json) in [
        (1, None),
        (2, Some("")),
        (3, Some(r#"{"repo": "example-org/exa"#)),
    ] {
        let run = r.make_run(hours * HOUR, 30 + hours, false, "");
        if let Some(text) = run_json {
            fs::write(run.join("run.json"), text).unwrap();
        }
        r.write(
            &run,
            "failure.txt",
            "the review stopped at an unexpected error\n",
            1,
        );
        unreadable.push(run);
    }
    let empty = r.run(4 * HOUR, 40);
    r.fetched(&empty);
    r.write(&empty, "findings.json", "", 20);
    r.write(
        &empty,
        "failure.txt",
        "the review run exited 0 without findings; see its stderr.log\n",
        21,
    );
    r.write(
        &empty,
        "failure.json",
        &cause("agent_no_findings").to_string(),
        21,
    );
    // A Devin review killed while it wrote findings.json left one that does not fit.
    let unfit = r.run(5 * HOUR, 41);
    r.fetched(&unfit);
    r.write(
        &unfit,
        "findings.json",
        &json!({"findings": [bug()]}).to_string(),
        20,
    );
    // Finder's .DS_Store, and a folder not named as review names runs, are not runs.
    fs::write(r.w.state.join("runs/.DS_Store"), "").unwrap();
    fs::create_dir(r.w.state.join("runs/notes")).unwrap();
    let mut want: Vec<Value> = unreadable
        .iter()
        .map(|run| {
            r.review(run, json!({"outcome": "failed", "verdict": null, "repo": null, "url": null, "title": null,
                "author": null, "cause": cause("unknown"), "head_sha": null, "ended_at": local(started(run) + MIN)}))
        })
        .collect();
    want.push(r.review(
        &empty,
        json!({"outcome": "failed", "verdict": null, "cause": cause("agent_no_findings"),
        "ended_at": local(started(&empty) + 21 * MIN)}),
    ));
    want.push(r.review(
        &unfit,
        json!({"outcome": "failed", "verdict": null, "cause": cause("unknown")}),
    ));
    assert_eq!(r.reviews(), Value::Array(want));
}

#[test]
fn status_and_show_take_the_title_and_author_from_github_json() {
    let r = Reviews::new();
    let run = r.run(HOUR, 12);
    r.finish(&run, "look_first", vec![], "");
    let mut unread = vec![];
    for (hours, github) in [
        (2, None),
        (3, Some(r#"{"pr": {"title": "Return 4"#.to_string())),
        (4, Some(json!({"reviews": [], "comments": []}).to_string())),
        (5, Some(json!({"pr": TITLE}).to_string())),
        (
            6,
            Some(json!({"pr": {"title": 422, "author": {"login": "alice"}}}).to_string()),
        ),
    ] {
        let other = r.run(hours * HOUR, 20 + hours);
        if let Some(github) = github {
            r.write(&other, "github.json", &github, 1);
        }
        r.write(
            &other,
            "findings.json",
            &findings_json("look_first", vec![], "").to_string(),
            20,
        );
        unread.push(other);
    }
    let mut want = vec![r.review(&run, json!({}))];
    want.extend(
        unread
            .iter()
            .map(|o| r.review(o, json!({"title": null, "author": null}))),
    );
    assert_eq!(r.reviews(), Value::Array(want));
    let shown = r.shown(&run);
    assert_eq!(
        (shown["title"].clone(), shown["author"].clone()),
        (json!(TITLE), json!("alice"))
    );
    for other in &unread {
        let shown = r.shown(other);
        assert_eq!(
            (shown["title"].clone(), shown["author"].clone()),
            (Value::Null, Value::Null)
        );
    }
}

#[test]
fn status_and_show_name_what_stopped_each_failed_review_and_why_one_waits() {
    let r = Reviews::new();
    let saved = r.stopped(HOUR, 12, "docker_unavailable");
    // A review without failure.json, as after a kill -9, or with one cut short, is unknown.
    let older = r.run(2 * HOUR, 22);
    r.fetched(&older);
    r.write(
        &older,
        "failure.txt",
        "/Users/sam/.config/whooper/setup.sh failed\n",
        2,
    );
    let cut = r.run(10 * HOUR, 30);
    r.fetched(&cut);
    r.write(&cut, "failure.txt", "/config/setup.sh failed\n", 2);
    r.write(&cut, "failure.json", r#"{"id": "docker_unav"#, 2);
    let waits = r.waiting(30 * MIN, 13, "docker_unavailable");
    let causes: Vec<(Value, Value)> = r
        .reviews()
        .as_array()
        .unwrap()
        .iter()
        .map(|v| (v["run_id"].clone(), v["cause"].clone()))
        .collect();
    assert!(causes.contains(&(json!(Reviews::name(&saved)), cause("docker_unavailable"))));
    assert!(causes.contains(&(json!(Reviews::name(&older)), cause("unknown"))));
    assert!(causes.contains(&(json!(Reviews::name(&cut)), cause("unknown"))));
    assert!(causes.contains(&(json!(Reviews::name(&waits)), cause("docker_unavailable"))));
    assert_eq!(r.shown(&saved)["cause"], cause("docker_unavailable"));
    // It has no findings, drafts, or diff yet, since its review has not fetched the merge base.
    let shown = r.shown(&waits);
    assert_eq!(
        keys(
            &shown,
            &[
                "outcome",
                "findings",
                "drafts",
                "diff",
                "failure",
                "cause",
                "head_sha",
                "co_change"
            ]
        ),
        json!({"outcome": "waiting", "findings": null, "drafts": null, "diff": null, "failure": null,
               "cause": cause("docker_unavailable"), "head_sha": "h1", "co_change": []})
    );
    let waiting_review = r
        .reviews()
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["run_id"] == json!(Reviews::name(&waits)))
        .cloned()
        .unwrap();
    assert_eq!(waiting_review["outcome"], "waiting");
    assert_eq!(waiting_review["needs_you"], false);
}

#[test]
fn whooper_is_blocked_by_a_failure_that_stops_every_review_until_a_review_does_not_stop_there() {
    let r = Reviews::new();
    assert!(r.status()["blocked"].is_null());
    r.finish(&r.run(9 * HOUR, 11), "look_first", vec![], "");
    assert!(r.status()["blocked"].is_null());
    let first = r.stopped(8 * HOUR, 12, "docker_unavailable");
    let since = local(started(&first) + 2 * MIN);
    let blocked = |id: &str, since: &str, waiting: i64| {
        let mut b = cause(id);
        b["since"] = json!(since);
        b["waiting"] = json!(waiting);
        b
    };
    assert_eq!(
        r.status()["blocked"],
        blocked("docker_unavailable", &since, 0)
    );
    // The rest of its round waits, the next round's review stops there again, and a review runs now.
    let waits = [
        r.waiting(8 * HOUR - 3 * MIN, 13, "docker_unavailable"),
        r.waiting(8 * HOUR - 4 * MIN, 14, "docker_unavailable"),
    ];
    r.stopped(7 * HOUR, 12, "docker_unavailable");
    r.hold(&r.run(5 * MIN, 15), None);
    assert_eq!(
        r.status()["blocked"],
        blocked("docker_unavailable", &since, 2)
    );
    // A removed or skipped review no longer counts as waiting.
    let _ = &waits;
    // A review that stops at a cause of its PR's own got past the setup script.
    r.stopped(6 * HOUR, 16, "time_limit");
    assert!(r.status()["blocked"].is_null());
    let setup = r.stopped(5 * HOUR, 17, "setup_failed");
    assert_eq!(
        r.status()["blocked"],
        blocked("setup_failed", &local(started(&setup) + 2 * MIN), 2)
    );
    r.finish(&r.run(4 * HOUR, 18), "look_first", vec![], "");
    assert!(r.status()["blocked"].is_null());
}

#[test]
fn a_removed_or_skipped_review_that_waits_no_longer_counts_as_waiting() {
    let r = Reviews::new();
    let failed = r.stopped(2 * HOUR, 12, "docker_unavailable");
    let waits = [
        r.waiting(2 * HOUR - 3 * MIN, 13, "docker_unavailable"),
        r.waiting(2 * HOUR - 4 * MIN, 14, "docker_unavailable"),
    ];
    let mut blocked = cause("docker_unavailable");
    blocked["since"] = json!(local(started(&failed) + 2 * MIN));
    blocked["waiting"] = json!(2);
    assert_eq!(r.status()["blocked"], blocked);
    assert!(
        r.command(&["remove", &Reviews::name(&waits[0])])
            .status
            .success()
    );
    blocked["waiting"] = json!(1);
    assert_eq!(r.status()["blocked"], blocked);
    assert!(
        r.command(&["skip", &Reviews::name(&waits[1])])
            .status
            .success()
    );
    blocked["waiting"] = json!(0);
    assert_eq!(r.status()["blocked"], blocked);
}

#[test]
fn status_gives_the_head_each_review_read_and_the_newest_head_whooper_knows_of_its_pr() {
    let r = Reviews::new();
    let record = |pr: i64, head: &str, at: i64, status: &str| {
        let path = r.w.state.join(format!(
            "prs/example-org-example-repo-{pr}/{head}.first_review.json"
        ));
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let started_at = Timestamp::from_second(at).unwrap().to_string();
        fs::write(
            path,
            json!({"status": status, "failures": 0, "started_at": started_at}).to_string(),
        )
        .unwrap();
    };
    let mut runs = std::collections::BTreeMap::new();
    for pr in [11, 12, 13, 14, 15] {
        let run = r.run((pr - 10) * HOUR, pr);
        r.finish(&run, "look_first", vec![], "");
        if pr != 15 {
            record(pr, "h1", started(&run), "done");
        }
        runs.insert(pr, run);
    }
    let stopped = r.make_run(6 * HOUR, 16, false, "");
    r.write(
        &stopped,
        "failure.txt",
        "the review stopped at an unexpected error\n",
        1,
    );
    // Half an hour ago the latest poll saw PR 11 at h2 and PR 13 still at h1, and listed a Slack ask for
    // PR 14 at h4; then retry queued PR 13 at h3, and submit found PR 12 at h5.
    let poll = r.w.state.join("poll");
    fs::create_dir_all(&poll).unwrap();
    fs::write(poll.join("github.json"), json!([
        {"repository": {"nameWithOwner": "example-org/example-repo"}, "number": 11, "headRefOid": "h2"},
        {"repository": {"nameWithOwner": "example-org/example-repo"}, "number": 13, "headRefOid": "h1"}]).to_string()).unwrap();
    fs::write(
        poll.join("20310727T020000Z.json"),
        json!({"items": [
        {"repo": "example-org/example-repo", "pr": 14, "head_sha": "h4", "kind": "first_review"}]})
        .to_string(),
    )
    .unwrap();
    let half_hour_ago = SystemTime::UNIX_EPOCH + Duration::from_secs((r.now - 30 * MIN) as u64);
    for name in ["github.json", "20310727T020000Z.json"] {
        fs::File::options()
            .write(true)
            .open(poll.join(name))
            .unwrap()
            .set_modified(half_hour_ago)
            .unwrap();
    }
    record(13, "h3", r.now - 10 * MIN, "retry");
    fs::create_dir_all(r.w.state.join("rechecks")).unwrap();
    fs::write(r.w.state.join("rechecks/example-org-example-repo-12.json"),
        json!({"repo": "example-org/example-repo", "pr": 12, "head_sha": "h5", "kind": "first_review"}).to_string()).unwrap();
    let got: Vec<Value> = r
        .reviews()
        .as_array()
        .unwrap()
        .iter()
        .map(|v| json!([v["pr"], v["head_sha"], v["pr_head_sha"]]))
        .collect();
    assert_eq!(
        got,
        vec![
            json!([11, "h1", "h2"]),
            json!([12, "h1", "h5"]),
            json!([13, "h1", "h3"]),
            json!([14, "h1", "h4"]),
            json!([15, "h1", null]),
            json!([16, null, null])
        ]
    );
    assert_eq!(r.shown(&runs[&11])["head_sha"], "h1");
    assert!(r.shown(&stopped)["head_sha"].is_null());
}

#[test]
fn status_and_show_call_a_review_safe_to_approve_only_once_it_is_reviewed() {
    let r = Reviews::new();
    let safe = r.run(HOUR, 12);
    r.finish(&safe, "approve_once_ci_passes", vec![nit()], "");
    let look_first = r.run(2 * HOUR, 13);
    r.finish(&look_first, "look_first", vec![nit()], "");
    let running = r.run(10 * MIN, 14);
    r.finish(&running, "approve_once_ci_passes", vec![nit()], "");
    r.hold(&running, None);
    let timed_out = r.run(3 * HOUR, 15);
    r.finish(&timed_out, "approve_once_ci_passes", vec![nit()], "");
    r.write(
        &timed_out,
        "failure.txt",
        "the review run passed its 45-minute limit\n",
        45,
    );
    let waits = r.waiting(4 * HOUR, 16, "docker_unavailable");
    let unreadable = r.make_run(5 * HOUR, 17, false, "");
    r.write(
        &unreadable,
        "failure.txt",
        "the review stopped at an unexpected error\n",
        1,
    );
    let expected: Vec<(String, bool)> = [
        (&safe, true),
        (&look_first, false),
        (&running, false),
        (&timed_out, false),
        (&waits, false),
        (&unreadable, false),
    ]
    .iter()
    .map(|(run, s)| (Reviews::name(run), *s))
    .collect();
    let listed = r.reviews();
    for (id, safe) in &expected {
        let review = listed
            .as_array()
            .unwrap()
            .iter()
            .find(|v| v["run_id"] == json!(id))
            .unwrap();
        assert_eq!(review["safe_to_approve"], json!(safe), "{id}");
        assert_eq!(
            r.shown(&r.w.state.join("runs").join(id))["safe_to_approve"],
            json!(safe),
            "{id}"
        );
    }
}

#[test]
fn show_prints_a_finished_review() {
    let r = Reviews::new();
    let run = r.run(HOUR, 12);
    r.finish(&run, "look_first", vec![bug(), nit()], "");
    let shown = r.shown(&run);
    assert_eq!(
        shown,
        json!({"run_id": Reviews::name(&run), "run": read_json(&run.join("run.json")), "title": TITLE, "author": "alice",
            "outcome": "reviewed", "findings": findings_json("look_first", vec![bug(), nit()], ""),
            "safe_to_approve": false, "drafts": {"event": "COMMENT", "message": "", "findings": {}},
            "diff": DIFF, "failure": null, "asks": [], "submitted": null, "cause": null, "head_sha": "h1",
            "skipped_by_user": false, "removed_by_user": false, "overview": null, "commits_since_review": null,
            "co_change": [], "connections": null, "test_gaps": null, "examples": null})
    );
    let drafts: Value =
        serde_json::from_slice(&r.command(&["draft", &Reviews::name(&run)]).stdout).unwrap();
    assert_eq!(drafts, shown["drafts"]);
}

#[test]
fn show_prints_a_review_that_failed_stopped_early_or_still_runs() {
    let r = Reviews::new();
    let nothing = json!({"findings": null, "safe_to_approve": false, "drafts": null, "asks": [], "submitted": null,
        "overview": null, "commits_since_review": null, "co_change": [], "connections": null, "test_gaps": null,
        "examples": null});
    let with = |extra: Value| {
        let mut v = nothing.clone();
        for (k, x) in extra.as_object().unwrap() {
            v[k] = x.clone();
        }
        v
    };
    let failed = r.run(HOUR, 12);
    r.fetched(&failed);
    let reason = format!(
        "the review run passed its 45-minute limit; see {}/stderr.log\n",
        failed.display()
    );
    r.write(&failed, "failure.txt", &reason, 45);
    r.write(
        &failed,
        "failure.json",
        &cause("time_limit").to_string(),
        45,
    );
    assert_eq!(
        r.shown(&failed),
        with(
            json!({"run_id": Reviews::name(&failed), "run": read_json(&failed.join("run.json")),
        "title": TITLE, "author": "alice", "outcome": "failed", "diff": DIFF, "failure": reason,
        "cause": cause("time_limit"), "head_sha": "h1", "skipped_by_user": false, "removed_by_user": false})
        )
    );
    // A Devin review that wrote findings that fit before its time limit has them and drafts, though it failed.
    r.write(
        &failed,
        "findings.json",
        &findings_json("look_first", vec![bug()], "").to_string(),
        40,
    );
    assert_eq!(
        keys(
            &r.shown(&failed),
            &["outcome", "findings", "drafts", "failure"]
        ),
        json!({"outcome": "failed", "findings": findings_json("look_first", vec![bug()], ""),
               "drafts": {"event": "COMMENT", "message": "", "findings": {}}, "failure": reason})
    );
    let stopped = r.make_run(2 * HOUR, 13, false, "");
    r.write(
        &stopped,
        "failure.txt",
        "the review stopped at an unexpected error\n",
        1,
    );
    assert_eq!(
        r.shown(&stopped),
        with(
            json!({"run_id": Reviews::name(&stopped), "run": null, "title": null,
        "author": null, "outcome": "failed", "diff": null, "failure": "the review stopped at an unexpected error\n",
        "cause": cause("unknown"), "head_sha": null, "skipped_by_user": false, "removed_by_user": false})
        )
    );
    // A Devin review that is still running may still change its findings.json, so it shows none yet.
    let running = r.run(5 * MIN, 14);
    r.fetched(&running);
    r.write(
        &running,
        "findings.json",
        &findings_json("look_first", vec![bug()], "").to_string(),
        4,
    );
    r.hold(&running, None);
    assert_eq!(
        r.shown(&running),
        with(
            json!({"run_id": Reviews::name(&running), "run": read_json(&running.join("run.json")),
        "title": TITLE, "author": "alice", "outcome": "running", "diff": DIFF, "failure": null, "cause": null,
        "head_sha": "h1", "skipped_by_user": false, "removed_by_user": false})
        )
    );
}

#[test]
fn show_gives_the_overview_connections_test_gaps_and_examples_once_the_review_ended() {
    let r = Reviews::new();
    let kept = json!({"summary": "Renames the --fast flag to --quick in the README.", "verdict_reason": "Only a word changes.",
        "before_after": [], "places": [], "flow": null, "checked": ["Read the diff."], "not_checked": [],
        "layers": [{"id": "l1", "title": "README", "intent": "The flag's new name.", "read_hint": "read",
                    "ranges": [{"path": "README.md", "start_line": 3, "end_line": 3}]}]});
    let graph = json!({"nodes": [], "edges": [], "limits": ["one level"]});
    let gaps = json!({"measured": true, "lines": []});
    let examples = json!({"functions": [], "rows": []});
    let run = r.run(HOUR, 12);
    r.finish(&run, "look_first", vec![], "");
    r.write(&run, "overview.json", &kept.to_string(), 19);
    r.write(&run, "connections.json", &graph.to_string(), 19);
    r.write(&run, "test_gaps.json", &gaps.to_string(), 1);
    r.write(&run, "examples.json", &examples.to_string(), 19);
    assert_eq!(
        keys(
            &r.shown(&run),
            &["overview", "connections", "test_gaps", "examples"]
        ),
        json!({"overview": kept, "connections": graph, "test_gaps": gaps, "examples": examples})
    );
    // A connections.json that is not JSON shows none.
    r.write(&run, "connections.json", r#"{"nodes": ["#, 19);
    assert!(r.shown(&run)["connections"].is_null());
    // An overview.json that breaks a rule, as an interrupted review can leave, is no overview; one that
    // keeps every rule still comes, though the review failed.
    let interrupted = r.stopped(2 * HOUR, 13, "unknown");
    let mut no_layers = kept.clone();
    no_layers["layers"] = json!([]);
    for (content, shown) in [
        (no_layers.to_string(), Value::Null),
        (r#"{"summary": "Renames"#.to_string(), Value::Null),
        (kept.to_string(), kept.clone()),
    ] {
        r.write(&interrupted, "overview.json", &content, 1);
        assert_eq!(
            keys(&r.shown(&interrupted), &["outcome", "overview"]),
            json!({"outcome": "failed", "overview": shown})
        );
    }
    // While the review runs, the run may still change each file.
    let running = r.run(5 * MIN, 14);
    r.fetched(&running);
    for name in [
        "overview.json",
        "connections.json",
        "test_gaps.json",
        "examples.json",
    ] {
        r.write(&running, name, &kept.to_string(), 4);
    }
    r.hold(&running, None);
    assert_eq!(
        keys(
            &r.shown(&running),
            &[
                "outcome",
                "overview",
                "connections",
                "test_gaps",
                "examples"
            ]
        ),
        json!({"outcome": "running", "overview": null, "connections": null, "test_gaps": null, "examples": null})
    );
}

#[test]
fn show_prints_the_drafts_and_the_review_submit_posted() {
    let r = Reviews::new();
    let run = r.run(HOUR, 12);
    r.finish(&run, "approve_once_ci_passes", vec![nit()], "");
    let drafts = json!({"event": "APPROVE", "message": "Thanks!", "findings": {"2": {"included": true, "text": "**nit:** --quick"}}});
    r.write(&run, "drafts.json", &drafts.to_string(), 25);
    r.write(
        &run,
        "submit.json",
        &format!(r#"{{"status": "posting", "request": {REQUEST}}}"#),
        30,
    );
    assert_eq!(
        keys(&r.shown(&run), &["outcome", "drafts", "submitted"]),
        json!({"outcome": "reviewed", "drafts": drafts, "submitted": null})
    );
    let posted = json!({"status": "posted", "request": serde_json::from_str::<Value>(REQUEST).unwrap(),
        "response": {"id": 7001, "html_url": "https://github.com/example-org/example-repo/pull/12#pullrequestreview-7001"}});
    r.write(&run, "submit.json", &posted.to_string(), 31);
    assert_eq!(
        keys(&r.shown(&run), &["outcome", "drafts", "submitted"]),
        json!({"outcome": "reviewed", "drafts": drafts, "submitted": posted})
    );
}

#[test]
fn show_lists_the_asks_in_number_order() {
    let r = Reviews::new();
    let run = r.run(HOUR, 12);
    r.finish(&run, "look_first", vec![bug()], "");
    let ask = |n: i64| {
        json!({"question": format!("Question {n}?"), "finding": if n == 2 { json!("1") } else { Value::Null },
        "answer": format!("Answer {n}."), "agent": "claude", "started_at": "2031-07-27T02:00:00Z",
        "ended_at": "2031-07-27T02:01:00Z", "transcript": run.join(format!("asks/{n}.transcript.jsonl")).display().to_string()})
    };
    for n in [2, 10, 1] {
        r.write(&run, &format!("asks/{n}.json"), &ask(n).to_string(), 30 + n);
    }
    for name in [
        "1.prompt.md",
        "1.transcript.jsonl",
        "10.devin-config.json",
        "11.json.tmp",
        "3.prompt.md",
    ] {
        fs::write(run.join("asks").join(name), "{}").unwrap();
    }
    assert_eq!(r.shown(&run)["asks"], json!([ask(1), ask(2), ask(10)]));
}

#[test]
fn skipping_a_review_marks_it_done_without_submitting_it_until_undone() {
    let r = Reviews::new();
    let run = r.run(HOUR, 12);
    r.finish(&run, "look_first", vec![bug()], "");
    let id = Reviews::name(&run);
    let name = format!("{id} (example-org/example-repo#12)");
    let undo = format!(
        "Whooper starts no other review of the PR until bin/whooper skip {id} --undo unmarks it"
    );
    assert_eq!(
        text(&r.command(&["skip", &id])),
        (
            Some(0),
            format!("marked {name} as skipped, without submitting it; {undo}\n"),
            String::new()
        )
    );
    assert_eq!(
        r.reviews(),
        json!([r.review(
            &run,
            json!({"findings": 1, "skipped_by_user": true, "needs_you": false})
        )])
    );
    assert_eq!(r.shown(&run)["skipped_by_user"], true);
    assert!(!run.join("submit.json").exists() && !run.join("drafts.json").exists());
    assert_eq!(
        text(&r.command(&["skip", &id])).1,
        format!("{name} is already marked as skipped\n")
    );
    assert_eq!(
        text(&r.command(&["skip", &id, "--undo"])),
        (
            Some(0),
            format!("unmarked {name} as skipped\n"),
            String::new()
        )
    );
    assert_eq!(r.reviews(), json!([r.review(&run, json!({"findings": 1}))]));
    assert_eq!(
        text(&r.command(&["skip", "--undo", &id])).1,
        format!("{name} is not marked as skipped\n")
    );
    assert_eq!(
        text(&r.command(&["skip", "--item", &id])).1,
        format!("marked {name} as skipped, without submitting it; {undo}\n")
    );
    assert_eq!(
        text(&r.command(&["skip", "--item", &id, "--undo"])).1,
        format!("unmarked {name} as skipped\n")
    );
    // A review stopped before it wrote run.json, which names the PR, can be skipped too.
    let stopped = r.make_run(2 * HOUR, 13, false, "");
    r.write(
        &stopped,
        "failure.txt",
        "the review stopped at an unexpected error\n",
        1,
    );
    let sid = Reviews::name(&stopped);
    assert_eq!(
        text(&r.command(&["skip", &sid])).1,
        format!(
            "marked {sid} as skipped, without submitting it; bin/whooper skip {sid} --undo unmarks it\n"
        )
    );
    let marks: Vec<Value> = r
        .reviews()
        .as_array()
        .unwrap()
        .iter()
        .map(|v| json!([v["run_id"], v["skipped_by_user"], v["needs_you"]]))
        .collect();
    assert_eq!(
        marks,
        vec![json!([id, false, true]), json!([sid, true, false])]
    );
}

#[test]
fn skipping_a_review_still_running_stops_its_review() {
    let r = Reviews::new();
    let run = r.run(5 * MIN, 12);
    r.fetched(&run);
    let id = Reviews::name(&run);
    let root = r.w.root();
    let term = root.join("term");
    let stand_in = root.join("review/whooper-review");
    script(
        &stand_in,
        &format!(
            "trap 'echo TERM > {}; exit 130' TERM\ntouch {}\nwhile :; do sleep 0.1; done",
            term.display(),
            root.join("ready").display()
        ),
    );
    let mut review = std::process::Command::new(&stand_in).spawn().unwrap();
    for _ in 0..200 {
        if root.join("ready").exists() {
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    r.hold(&run, Some(review.id()));
    let then = format!(
        "Whooper starts no other review of the PR until bin/whooper skip {id} --undo unmarks it"
    );
    assert_eq!(
        text(&r.command(&["skip", &id])),
        (
            Some(0),
            format!(
                "marked {id} (example-org/example-repo#12) as skipped, without submitting it, and stopped its review; {then}\n"
            ),
            String::new()
        )
    );
    assert_eq!(review.wait().unwrap().code(), Some(130));
    assert_eq!(fs::read_to_string(term).unwrap(), "TERM\n");
    assert!(run.join("skipped-by-user").exists());
}

#[test]
fn skip_refuses_an_unknown_run_and_signals_no_other_program() {
    let r = Reviews::new();
    let run = r.run(5 * MIN, 12);
    r.fetched(&run);
    let id = Reviews::name(&run);
    let runs = r.w.state.join("runs");
    for bad in [
        "20310101T000000Z-1".to_string(),
        "..".into(),
        "../whooper".into(),
        format!("../runs/{id}"),
    ] {
        assert_eq!(
            text(&r.command(&["skip", &bad])),
            (
                Some(1),
                String::new(),
                format!("whooper-skip: no run {bad} in {}\n", runs.display())
            )
        );
    }
    // The lock names the run and a process that is no whooper review: the mark goes on, and it gets no signal.
    let mut other = std::process::Command::new("sleep")
        .arg("30")
        .spawn()
        .unwrap();
    r.hold(&run, Some(other.id()));
    let name = format!("{id} (example-org/example-repo#12)");
    let then = format!(
        "Whooper starts no other review of the PR until bin/whooper skip {id} --undo unmarks it"
    );
    assert_eq!(
        text(&r.command(&["skip", &id])),
        (
            Some(0),
            format!("marked {name} as skipped, without submitting it; {then}\n"),
            String::new()
        )
    );
    assert!(other.try_wait().unwrap().is_none());
    other.kill().unwrap();
    other.wait().unwrap();
    assert_eq!(
        text(&r.command(&["skip", &id, "--undo"])).1,
        format!("unmarked {name} as skipped\n")
    );
    for args in [
        vec![],
        vec!["--undo"],
        vec![&id, &id],
        vec!["--item"],
        vec!["--item", &id, &id],
        vec![&id, "--item", &id],
    ] {
        let mut all = vec!["skip"];
        all.extend(args.iter().map(|s| -> &str { s }));
        assert_eq!(
            text(&r.command(&all)),
            (
                Some(1),
                String::new(),
                "usage: whooper skip [--item] <run-id> [--undo]\n".into()
            ),
            "{all:?}"
        );
    }
    assert!(!run.join("skipped-by-user").exists());
}

#[test]
fn removing_a_review_takes_it_out_of_the_lists_until_undone() {
    let r = Reviews::new();
    let run = r.run(HOUR, 12);
    r.finish(&run, "look_first", vec![bug()], "");
    let id = Reviews::name(&run);
    let name = format!("{id} (example-org/example-repo#12)");
    let marked = format!(
        "marked {name} as removed; its files stay, and bin/whooper remove {id} --undo unmarks it\n"
    );
    assert_eq!(
        text(&r.command(&["remove", &id])),
        (Some(0), marked.clone(), String::new())
    );
    assert_eq!(
        r.reviews(),
        json!([r.review(
            &run,
            json!({"findings": 1, "removed_by_user": true, "needs_you": false})
        )])
    );
    assert_eq!(r.shown(&run)["removed_by_user"], true);
    assert_eq!(fs::read_to_string(run.join("removed-by-user")).unwrap(), "");
    assert_eq!(
        text(&r.command(&["remove", &id])).1,
        format!("{name} is already marked as removed\n")
    );
    assert_eq!(
        text(&r.command(&["remove", &id, "--undo"])),
        (
            Some(0),
            format!("unmarked {name} as removed\n"),
            String::new()
        )
    );
    assert_eq!(r.reviews(), json!([r.review(&run, json!({"findings": 1}))]));
    assert_eq!(
        text(&r.command(&["remove", "--undo", &id])).1,
        format!("{name} is not marked as removed\n")
    );
    assert_eq!(text(&r.command(&["remove", "--item", &id])).1, marked);
    assert_eq!(
        text(&r.command(&["remove", "--undo", "--item", &id])).1,
        format!("unmarked {name} as removed\n")
    );
}

#[test]
fn remove_refuses_an_unknown_run_and_a_review_still_running() {
    let r = Reviews::new();
    let run = r.run(5 * MIN, 12);
    r.fetched(&run);
    let id = Reviews::name(&run);
    let runs = r.w.state.join("runs");
    for bad in [
        "20310101T000000Z-1".to_string(),
        "..".into(),
        "../whooper".into(),
        format!("../runs/{id}"),
    ] {
        assert_eq!(
            text(&r.command(&["remove", &bad])),
            (
                Some(1),
                String::new(),
                format!("whooper-remove: no run {bad} in {}\n", runs.display())
            )
        );
    }
    r.hold(&run, None);
    for args in [vec![id.as_str()], vec![id.as_str(), "--undo"]] {
        let mut all = vec!["remove"];
        all.extend(args);
        assert_eq!(
            text(&r.command(&all)),
            (
                Some(1),
                String::new(),
                "whooper-remove: the review is still running; remove it once it ends\n".into()
            )
        );
    }
    for args in [
        vec![],
        vec!["--undo"],
        vec![&id, &id],
        vec!["--item"],
        vec!["--item", &id, &id],
    ] {
        let mut all = vec!["remove"];
        all.extend(args.iter().map(|s| -> &str { s }));
        assert_eq!(
            text(&r.command(&all)),
            (
                Some(1),
                String::new(),
                "usage: whooper remove [--item] <run-id> [--undo]\n".into()
            ),
            "{all:?}"
        );
    }
    assert!(!run.join("removed-by-user").exists());
}

#[test]
fn show_refuses_an_unknown_run() {
    let r = Reviews::new();
    let run = r.run(HOUR, 12);
    r.finish(&run, "look_first", vec![], "");
    let id = Reviews::name(&run);
    let runs = r.w.state.join("runs");
    for bad in [
        "20310101T000000Z-1".to_string(),
        "..".into(),
        "../whooper".into(),
        format!("../runs/{id}"),
    ] {
        assert_eq!(
            text(&r.command(&["show", &bad])),
            (
                Some(1),
                String::new(),
                format!("whooper-show: no run {bad} in {}\n", runs.display())
            )
        );
    }
    for args in [vec![], vec![id.as_str(), id.as_str()]] {
        let mut all = vec!["show"];
        all.extend(args);
        assert_eq!(
            text(&r.command(&all)),
            (
                Some(1),
                String::new(),
                "usage: whooper show <run-id>\n".into()
            )
        );
    }
}
