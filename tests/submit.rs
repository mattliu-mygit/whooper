//! Black-box tests of whooper draft and whooper submit, on a review run of example-org/example-repo#12
//! at head h1. gh is a fake: it records each call in calls.log, reports the PR's head from the file
//! head, saves each review it is asked to post as posted-<n>.json, and fails any other call, so no test
//! reaches GitHub. Unless a test changes it, the run is not safe to approve: its verdict is look_first,
//! and of its findings BUG, QUESTION, and NIT, only NIT is a nit.

mod common;

use std::fs;
use std::path::PathBuf;
use std::process::Output;

use common::*;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

const RUN_ID: &str = "20310727T010246Z-12";
const PATCH: &str = concat!(
    "diff --git a/services/orders/internal/api/orders.go b/services/orders/internal/api/orders.go\n",
    "--- a/services/orders/internal/api/orders.go\n+++ b/services/orders/internal/api/orders.go\n",
    "@@ -30,70 +30,71 @@ func Create()\n",
    "diff --git a/services/orders/README.md b/services/orders/README.md\n",
    "--- a/services/orders/README.md\n+++ b/services/orders/README.md\n@@ -1,5 +1,4 @@\n",
);
const RESPONSE: &str = r#"{"id": 7001, "html_url": "https://github.com/example-org/example-repo/pull/12#pullrequestreview-7001"}"#;
const UNFIT: &str =
    "has a findings.json that does not fit schemas/findings.json, so its review did not finish: ";

fn finding(fields: Value) -> Value {
    let mut f = json!({"id": "1", "path": "services/orders/internal/api/orders.go", "line": 88, "side": "RIGHT",
        "in_diff": true, "title": "Bad JSON now returns 500", "draft": "**issue:** ...", "kind": "caller_visible_bug"});
    for (k, v) in fields.as_object().unwrap() {
        f[k] = v.clone();
    }
    f
}

fn bug() -> Value {
    finding(json!({"draft": "**issue (blocking):** Bad JSON now returns 500 instead of 422."}))
}

fn question() -> Value {
    finding(
        json!({"id": "2", "kind": "question", "line": 40, "title": "Why retry twice?",
                   "draft": "**question:** Why does the client retry a call that succeeded?"}),
    )
}

/// The review skills open each draft with its label; this one lacks it.
fn nit() -> Value {
    finding(
        json!({"id": "3", "kind": "nit", "nit_kind": "wording", "path": "services/orders/README.md", "line": 3,
                   "side": "LEFT", "title": "Old flag name", "draft": "This line still names the old --fast flag."}),
    )
}

fn other_nit() -> Value {
    finding(
        json!({"id": "4", "kind": "nit", "nit_kind": "redundant_code", "line": 95, "title": "Unused count",
                   "draft": "**nit:** `count` is never read."}),
    )
}

fn reason(verdict: &str) -> &'static str {
    if verdict == "approve_once_ci_passes" {
        "Ran the orders tests; nothing needs the author."
    } else {
        "The question needs the author's answer."
    }
}

struct Submit {
    w: Whooper,
    run: PathBuf,
    work: Value,
}

impl Submit {
    fn new() -> Submit {
        let w = Whooper::new();
        fs::write(w.fakes.join("head"), "h1\n").unwrap();
        w.fake(
            "gh",
            &format!(
                r#"fakes="${{0%/*}}"
echo "$*" >> "$fakes/calls.log"
case "$*" in
  "api repos/example-org/example-repo/pulls/12 --jq .head.sha")
    [ -e "$fakes/on-head" ] && sh "$fakes/on-head"
    cat "$fakes/head" ;;
  "api --method POST repos/example-org/example-repo/pulls/12/reviews --input "*)
    for arg; do [ "$previous" = --input ] && input="$arg"; previous="$arg"; done
    if [ -e "$fakes/refuse" ]; then
      echo '{{"message": "Unprocessable Entity", "errors": ["Line could not be resolved"], "status": "422"}}'
      echo 'gh: Unprocessable Entity (HTTP 422)' >&2
      exit 1
    fi
    n=$(ls "$fakes" | grep -c '^posted-')
    cp "$input" "$fakes/posted-$n.json"
    echo '{RESPONSE}' ;;
  *) echo "fake gh: unexpected call: gh $*" >&2; exit 1 ;;
esac"#
            ),
        );
        let run = w.state.join("runs").join(RUN_ID);
        fs::create_dir_all(&run).unwrap();
        let work = json!({"kind": "first_review", "repo": "example-org/example-repo", "pr": 12, "head_sha": "h1",
            "base_ref": "master", "source": "github_request", "slack": null,
            "reason": "A GitHub review request names sam-example, who has not reviewed this PR."});
        let s = Submit { w, run, work };
        s.write_run(&s.work.clone());
        s.write_findings("look_first", vec![bug(), question(), nit()], "");
        s
    }

    fn write_run(&self, work: &Value) {
        let mut ran = work.clone();
        ran["run_id"] = json!(RUN_ID);
        ran["agent"] = json!("claude");
        ran["as_of"] = Value::Null;
        fs::write(self.run.join("run.json"), ran.to_string()).unwrap();
        fs::write(self.run.join("diff.patch"), PATCH).unwrap();
    }

    fn write_findings(&self, verdict: &str, findings: Vec<Value>, skip_reason: &str) {
        let status = if skip_reason.is_empty() {
            "reviewed"
        } else {
            "skipped"
        };
        let result = json!({"status": status, "skip_reason": skip_reason,
            "approval": {"verdict": verdict, "reason": reason(verdict)}, "findings": findings});
        fs::write(self.run.join("findings.json"), result.to_string()).unwrap();
    }

    fn draft(&self, change: &[&str]) -> Value {
        let mut args = vec!["draft", RUN_ID];
        args.extend(change);
        serde_json::from_str(&self.w.ok(&args)).unwrap()
    }

    fn submit(&self, flags: &[&str]) -> Output {
        let mut args = vec!["submit", RUN_ID];
        args.extend(flags);
        self.w.run(&args)
    }

    fn posted(&self) -> Vec<Value> {
        (0..)
            .map(|n| self.w.fakes.join(format!("posted-{n}.json")))
            .take_while(|p| p.exists())
            .map(|p| read_json(&p))
            .collect()
    }

    fn gh_calls(&self) -> Vec<String> {
        fs::read_to_string(self.w.fakes.join("calls.log"))
            .map(|t| t.lines().map(String::from).collect())
            .unwrap_or_default()
    }

    /// Runs submit --print with flags, as Prompting Time's confirmation does, and returns the
    /// fingerprint it prints.
    fn shown(&self, flags: &[&str]) -> String {
        let mut all = vec!["--print"];
        all.extend(flags);
        let printed = self.submit(&all);
        assert_eq!(printed.status.code(), Some(0), "{}", err(&printed));
        out(&printed)
            .lines()
            .last()
            .unwrap()
            .strip_prefix("fingerprint: ")
            .unwrap()
            .to_string()
    }

    fn files(&self) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(&self.run)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }

    /// Has the lock of the run's PR name the run and a live stand-in for whooper review, as review's
    /// lock does while it reviews; returns the lock and the stand-in.
    fn hold(&self) -> (PathBuf, std::process::Child) {
        let lock = self.w.state.join("locks/example-org-example-repo-12");
        let holder = self.w.holder("whooper-review");
        self.w.hold(&lock, holder.id());
        fs::write(lock.join("run_id"), format!("{RUN_ID}\n")).unwrap();
        (lock, holder)
    }

    fn request(&self) -> Value {
        read_json(&self.run.join("submit-request.json"))
    }
}

fn out(o: &Output) -> String {
    String::from_utf8_lossy(&o.stdout).into_owned()
}

fn err(o: &Output) -> String {
    String::from_utf8_lossy(&o.stderr).into_owned()
}

/// The lines of a refusal that list its reasons, which start with two spaces.
fn listed(o: &Output) -> Vec<String> {
    err(o)
        .lines()
        .filter(|l| l.starts_with("  "))
        .map(|l| l.trim().to_string())
        .collect()
}

fn code(o: &Output) -> Option<i32> {
    o.status.code()
}

const VETTED: &str = "AI comment (vetted)\n\n";

fn nit_comment() -> Value {
    json!({"path": "services/orders/README.md", "line": 3, "side": "LEFT",
           "body": format!("{VETTED}**nit:** This line still names the old --fast flag.")})
}

#[test]
fn posts_the_included_drafts_as_one_review_at_the_reviewed_head() {
    let s = Submit::new();
    for change in [
        &["include", "1"][..],
        &["include", "2"],
        &["include", "3"],
        &["approve"],
        &[
            "text",
            "2",
            "Why retry a call that succeeded? It duplicates the order.",
        ],
        &["message", "Pre-approving to unblock"],
    ] {
        s.draft(change);
    }
    // The run is not safe to approve, and the user chose to approve it anyway.
    let result = s.submit(&["--approve-anyway"]);
    assert_eq!(code(&result), Some(0), "{}", err(&result));
    // A comment keeps the label its text opens with, and a text without one gets its finding kind's.
    let request = json!({"commit_id": "h1", "event": "APPROVE", "body": "Pre-approving to unblock", "comments": [
        {"path": "services/orders/internal/api/orders.go", "line": 88, "side": "RIGHT",
         "body": format!("{VETTED}**issue (blocking):** Bad JSON now returns 500 instead of 422.")},
        {"path": "services/orders/internal/api/orders.go", "line": 40, "side": "RIGHT",
         "body": format!("{VETTED}**question:** Why retry a call that succeeded? It duplicates the order.")},
        nit_comment(),
    ]});
    assert_eq!(s.posted(), std::slice::from_ref(&request));
    let response: Value = serde_json::from_str(RESPONSE).unwrap();
    assert_eq!(
        read_json(&s.run.join("submit.json")),
        json!({"status": "posted", "request": request, "response": response})
    );
    assert!(out(&result).contains(response["html_url"].as_str().unwrap()));
}

#[test]
fn a_finding_left_out_stays_out() {
    let s = Submit::new();
    for change in [
        &["include", "1"][..],
        &["include", "2"],
        &["leave-out", "1"],
    ] {
        s.draft(change);
    }
    assert_eq!(code(&s.submit(&[])), Some(0));
    let [request] = &s.posted()[..] else { panic!() };
    assert_eq!(
        (&request["event"], &request["body"]),
        (&json!("COMMENT"), &json!(""))
    );
    let lines: Vec<&Value> = request["comments"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| &c["line"])
        .collect();
    assert_eq!(lines, [&json!(40)]);
}

#[test]
fn new_commits_post_nothing_and_leave_the_pr_to_the_next_round() {
    let s = Submit::new();
    s.draft(&["include", "1"]);
    fs::write(s.w.fakes.join("head"), "h2\n").unwrap();
    for flags in [&["--print"][..], &[]] {
        let result = s.submit(flags);
        assert_eq!(code(&result), Some(10));
        assert!(err(&result).contains("new commits"));
        assert!(err(&result).contains("bin/whooper round"));
    }
    assert!(s.posted().is_empty());
    assert!(!s.run.join("submit.json").exists());
    assert!(!s.run.join("submit-request.json").exists());
    // The reviewed head's record says stale, and the recheck list holds the work item at the new head.
    let record =
        s.w.state
            .join("prs/example-org-example-repo-12/h1.first_review.json");
    assert_eq!(read_json(&record)["status"], "stale");
    let mut moved = s.work.clone();
    moved["head_sha"] = json!("h2");
    assert_eq!(
        read_json(&s.w.state.join("rechecks/example-org-example-repo-12.json")),
        moved
    );
}

#[test]
fn new_commits_bring_back_a_pr_only_a_slack_ask_brought_in() {
    let s = Submit::new();
    let mut work = s.work.clone();
    work["source"] = json!("slack_ask");
    work["slack"] = json!({"ts": "1943194837.000100", "tagged_user": true,
                           "permalink": "https://example.slack.com/archives/C0000000001/p1943194837000100"});
    work["reason"] = json!("Alex asked for a review.");
    s.write_run(&work);
    fs::write(s.w.fakes.join("head"), "h2\n").unwrap();
    assert_eq!(code(&s.submit(&[])), Some(10));
    // A poll reads only the Slack posts new since the last poll, so the next round's poll lists other
    // work only.
    let other = work_item("sample-org/plain-repo", 7, "h7");
    fs::create_dir_all(s.w.state.join("poll")).unwrap();
    fs::write(
        s.w.state.join("poll/20310727T020000Z.json"),
        json!({"items": [other]}).to_string(),
    )
    .unwrap();
    let select = || -> Vec<Value> {
        s.w.ok(&["select"])
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    };
    let mut moved = work.clone();
    moved["head_sha"] = json!("h2");
    assert_eq!(select(), [other.clone(), moved]);
    // Once the review of the new head is done, select takes the PR off the list.
    fs::write(
        s.w.state
            .join("prs/example-org-example-repo-12/h2.first_review.json"),
        r#"{"status": "done", "failures": 0, "started_at": "2031-07-27T02:05:00+00:00"}"#,
    )
    .unwrap();
    assert_eq!(select(), [other]);
    assert!(
        !s.w.state
            .join("rechecks/example-org-example-repo-12.json")
            .exists()
    );
}

#[test]
fn an_at_mention_posts_only_with_allow_mentions() {
    let s = Submit::new();
    s.draft(&["include", "1"]);
    s.draft(&[
        "text",
        "1",
        "**issue:** @alice, bad JSON now returns 500; alice@example.com has the trace.",
    ]);
    s.draft(&["message", "cc @example-org/orders-team"]);
    for flags in [&[][..], &["--print"]] {
        let refused = s.submit(flags);
        assert_eq!(code(&refused), Some(12));
        for text in [
            "@alice in the comment on services/orders/internal/api/orders.go:88 (finding 1)",
            "@example-org/orders-team in the message",
            "--allow-mentions",
        ] {
            assert!(err(&refused).contains(text), "{text}");
        }
        // An email address mentions nobody.
        assert!(!err(&refused).contains("@example.com"));
    }
    assert!(s.posted().is_empty());
    assert!(!s.run.join("submit-request.json").exists());
    assert_eq!(code(&s.submit(&["--allow-mentions"])), Some(0));
    let [request] = &s.posted()[..] else { panic!() };
    assert_eq!(request["body"], "cc @example-org/orders-team");
    assert_eq!(
        request["comments"][0]["body"],
        format!(
            "{VETTED}**issue:** @alice, bad JSON now returns 500; alice@example.com has the trace."
        )
    );
}

#[test]
fn an_outside_link_does_not_stop_it() {
    let s = Submit::new();
    s.draft(&["include", "1"]);
    s.draft(&[
        "text",
        "1",
        "**issue:** Bad JSON now returns 500; https://example.com/rfc says 422.",
    ]);
    assert_eq!(code(&s.submit(&[])), Some(0));
    assert_eq!(
        s.posted()[0]["comments"][0]["body"],
        format!("{VETTED}**issue:** Bad JSON now returns 500; https://example.com/rfc says 422.")
    );
}

#[test]
fn print_writes_the_request_that_would_post_and_posts_nothing() {
    let s = Submit::new();
    s.draft(&["include", "2"]);
    let printed = s.submit(&["--print"]);
    assert_eq!(code(&printed), Some(0), "{}", err(&printed));
    assert!(out(&printed).contains("submit-request.json"));
    assert!(s.posted().is_empty());
    assert!(!s.run.join("submit.json").exists());
    // Its last line is the request's fingerprint: the SHA-256 of the file it wrote.
    let text = fs::read(s.run.join("submit-request.json")).unwrap();
    assert_eq!(
        out(&printed).lines().last().unwrap(),
        format!("fingerprint: {:x}", Sha256::digest(&text))
    );
    assert_eq!(code(&s.submit(&[])), Some(0));
    assert_eq!(
        s.posted(),
        [serde_json::from_slice::<Value>(&text).unwrap()]
    );
}

#[test]
fn expect_posts_only_the_request_print_showed() {
    let s = Submit::new();
    let missing = s.submit(&["--expect"]);
    assert_eq!(code(&missing), Some(1));
    assert!(err(&missing).contains("usage"));
    s.draft(&["include", "2"]);
    let confirmed = s.shown(&[]);
    // Whooper's agent edits the comment after the user confirmed the review.
    s.draft(&["text", "2", "**question:** Why retry at all?"]);
    let changed = s.submit(&["--expect", &confirmed]);
    assert_eq!(code(&changed), Some(13), "{}", err(&changed));
    for text in ["the drafts changed", "--print"] {
        assert!(err(&changed).contains(text));
    }
    assert!(s.posted().is_empty());
    assert!(!s.run.join("submit.json").exists());
    // The file still holds the request the confirmation showed.
    let text = fs::read(s.run.join("submit-request.json")).unwrap();
    assert_eq!(format!("{:x}", Sha256::digest(&text)), confirmed);
    let again = s.shown(&[]);
    assert_ne!(again, confirmed);
    assert_eq!(code(&s.submit(&["--expect", &again])), Some(0));
    assert_eq!(
        s.posted()[0]["comments"][0]["body"],
        format!("{VETTED}**question:** Why retry at all?")
    );
}

#[test]
fn a_review_posts_once() {
    let s = Submit::new();
    s.draft(&["include", "1"]);
    assert_eq!(code(&s.submit(&[])), Some(0));
    for flags in [&[][..], &["--print"]] {
        let again = s.submit(flags);
        assert_eq!(code(&again), Some(1));
        assert!(err(&again).contains("already submitted"));
    }
    assert_eq!(s.posted().len(), 1);
    // A change now would not reach GitHub.
    let out = s.w.run(&["draft", RUN_ID, "include", "2"]);
    assert_eq!(code(&out), Some(1));
    assert!(err(&out).contains("already submitted"));
}

#[test]
fn a_submit_that_did_not_finish_blocks_the_next() {
    // A submit stopped while gh posted leaves submit.json saying posting, and the review may be on GitHub.
    let s = Submit::new();
    s.draft(&["include", "1"]);
    fs::write(
        s.run.join("submit.json"),
        r#"{"status": "posting", "request": {}}"#,
    )
    .unwrap();
    let result = s.submit(&[]);
    assert_eq!(code(&result), Some(1));
    assert!(err(&result).contains("did not finish"));
    assert!(s.posted().is_empty());
}

#[test]
fn a_submit_json_that_cannot_be_read_blocks_the_next() {
    let s = Submit::new();
    s.draft(&["include", "1"]);
    for text in ["", r#"{"status": "post"#, "[]"] {
        fs::write(s.run.join("submit.json"), text).unwrap();
        let result = s.submit(&[]);
        assert_eq!(
            (code(&result), out(&result), err(&result)),
            (
                Some(1),
                String::new(),
                format!(
                    "whooper-submit: {} has a submit.json that cannot be read, so its review may be on GitHub; check the PR, then delete submit.json to submit again\n",
                    fs::canonicalize(&s.run).unwrap().display()
                )
            )
        );
    }
    assert!(s.posted().is_empty());
    assert!(s.gh_calls().is_empty());
}

#[test]
fn a_submit_that_finds_another_claimed_the_run_meanwhile_posts_nothing() {
    // Another submit claims the run after this one checked it, while this one reads the PR's head.
    let s = Submit::new();
    s.draft(&["include", "1"]);
    let claim = r#"{"status": "posting", "request": {}}"#;
    fs::write(
        s.w.fakes.join("on-head"),
        format!(
            "printf '%s' '{claim}' > '{}'",
            s.run.join("submit.json").display()
        ),
    )
    .unwrap();
    let result = s.submit(&[]);
    assert_eq!(code(&result), Some(1));
    assert!(
        err(&result).contains("is posting or did not finish"),
        "{}",
        err(&result)
    );
    assert_eq!(
        fs::read_to_string(s.run.join("submit.json")).unwrap(),
        claim
    );
    assert!(s.posted().is_empty());
    assert_eq!(
        s.files(),
        [
            "diff.patch",
            "drafts.json",
            "findings.json",
            "run.json",
            "submit.json"
        ]
    );
}

#[test]
fn of_two_submits_at_once_only_one_posts() {
    let s = Submit::new();
    s.draft(&["include", "1"]);
    let one = s.w.command(&["submit", RUN_ID]).spawn().unwrap();
    let two = s.w.command(&["submit", RUN_ID]).spawn().unwrap();
    let ended = [
        one.wait_with_output().unwrap(),
        two.wait_with_output().unwrap(),
    ];
    let codes: Vec<Option<i32>> = ended.iter().map(|o| o.status.code()).collect();
    assert!(codes.contains(&Some(0)), "{codes:?}");
    assert_eq!(s.posted().len(), 1);
    assert_eq!(read_json(&s.run.join("submit.json"))["status"], "posted");
}

#[test]
fn a_review_github_refuses_can_be_submitted_again() {
    let s = Submit::new();
    s.draft(&["include", "1"]);
    fs::write(s.w.fakes.join("refuse"), "").unwrap();
    let refused = s.submit(&[]);
    assert_eq!(code(&refused), Some(1));
    assert!(err(&refused).contains("Line could not be resolved"));
    assert!(!s.run.join("submit.json").exists());
    fs::remove_file(s.w.fakes.join("refuse")).unwrap();
    assert_eq!(code(&s.submit(&[])), Some(0));
    assert_eq!(s.posted().len(), 1);
}

#[test]
fn a_review_without_comments_posts_its_message() {
    for (verdict, findings, message, event) in [
        ("approve_once_ci_passes", vec![], "lgtm", "APPROVE"),
        (
            "look_first",
            vec![bug(), question(), nit()],
            "Thanks, nothing from me.",
            "COMMENT",
        ),
    ] {
        let s = Submit::new();
        s.write_findings(verdict, findings, "");
        s.draft(&["message", message]);
        let result = s.submit(&[]);
        assert_eq!(code(&result), Some(0), "{}", err(&result));
        assert_eq!(
            s.posted(),
            [json!({"commit_id": "h1", "event": event, "body": message, "comments": []})]
        );
    }
}

#[test]
fn a_comment_without_comments_or_a_message_has_nothing_to_submit() {
    for message in [None, Some(" \n")] {
        let s = Submit::new();
        if let Some(message) = message {
            s.draft(&["message", message]);
        } else {
            s.draft(&["comment"]);
        }
        for flags in [&[][..], &["--print"]] {
            let refused = s.submit(flags);
            assert_eq!(code(&refused), Some(1), "{}", err(&refused));
            assert!(err(&refused).contains("nothing to submit"));
        }
        assert!(s.posted().is_empty());
        assert_eq!(
            s.files(),
            ["diff.patch", "drafts.json", "findings.json", "run.json"]
        );
    }
}

#[test]
fn print_shows_a_review_of_only_a_message_and_its_fingerprint() {
    let s = Submit::new();
    s.draft(&["message", "Thanks, nothing from me."]);
    let confirmed = s.shown(&[]);
    let request = json!({"commit_id": "h1", "event": "COMMENT", "body": "Thanks, nothing from me.", "comments": []});
    let text = fs::read(s.run.join("submit-request.json")).unwrap();
    assert_eq!(serde_json::from_slice::<Value>(&text).unwrap(), request);
    assert_eq!(format!("{:x}", Sha256::digest(&text)), confirmed);
    assert!(s.posted().is_empty());
    assert_eq!(code(&s.submit(&["--expect", &confirmed])), Some(0));
    assert_eq!(s.posted(), [request]);
}

#[test]
fn a_comment_outside_the_diff_stops_the_review_before_it_reads_github() {
    // GitHub takes an inline comment only on a line inside a hunk of the PR's diff, and refuses the
    // whole review for one comment elsewhere.
    let s = Submit::new();
    let outside = finding(
        json!({"id": "5", "kind": "question", "path": "services/orders/internal/api/client.go",
        "line": 12, "title": "Does the gateway retry too?", "draft": "**question:** Does the gateway retry this call too?"}),
    );
    let mut nit_9 = nit();
    nit_9["line"] = json!(9);
    s.write_findings("look_first", vec![bug(), question(), nit_9, outside], "");
    for id in ["1", "3", "5"] {
        s.draft(&["include", id]);
    }
    for flags in [&[][..], &["--print"]] {
        let refused = s.submit(flags);
        assert_eq!(code(&refused), Some(14), "{}", err(&refused));
        assert_eq!(
            listed(&refused),
            [
                "finding 3: Old flag name (services/orders/README.md:9)",
                "finding 5: Does the gateway retry too? (services/orders/internal/api/client.go:12)"
            ]
        );
        assert!(err(&refused).contains("bin/whooper draft"));
    }
    // It refused before any call to GitHub, even the one that reads the PR's head.
    assert!(s.gh_calls().is_empty());
    assert!(s.posted().is_empty());
    assert_eq!(
        s.files(),
        ["diff.patch", "drafts.json", "findings.json", "run.json"]
    );
    // A comment outside the diff that the review leaves out stops nothing.
    for id in ["3", "5"] {
        s.draft(&["leave-out", id]);
    }
    assert_eq!(code(&s.submit(&[])), Some(0));
    let lines: Vec<Value> = s.posted()[0]["comments"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["line"].clone())
        .collect();
    assert_eq!(lines, [json!(88)]);
}

#[test]
fn a_safe_run_starts_as_an_approval_of_its_nits_and_posts_it() {
    let s = Submit::new();
    s.write_findings("approve_once_ci_passes", vec![nit(), other_nit()], "");
    assert_eq!(
        s.draft(&[]),
        json!({"event": "APPROVE", "message": "", "findings": {"3": {"included": true}, "4": {"included": true}}})
    );
    assert_eq!(code(&s.submit(&[])), Some(0));
    assert_eq!(
        s.posted(),
        [
            json!({"commit_id": "h1", "event": "APPROVE", "body": "Pre-approving to unblock", "comments": [
                nit_comment(),
                {"path": "services/orders/internal/api/orders.go", "line": 95, "side": "RIGHT",
                 "body": format!("{VETTED}**nit:** `count` is never read.")},
            ]})
        ]
    ); // A safe run approves whatever CI reports: submit reads only the PR's head before it posts.
    let calls = s.gh_calls();
    assert_eq!(calls.len(), 2);
    assert_eq!(
        calls[0],
        "api repos/example-org/example-repo/pulls/12 --jq .head.sha"
    );
    assert!(
        calls[1].starts_with(
            "api --method POST repos/example-org/example-repo/pulls/12/reviews --input "
        )
    );
}

#[test]
fn any_other_run_starts_as_a_comment_with_nothing_included() {
    let s = Submit::new();
    for (verdict, findings) in [
        ("look_first", vec![nit()]),
        ("approve_once_ci_passes", vec![nit(), question()]),
    ] {
        s.write_findings(verdict, findings, "");
        assert_eq!(
            s.draft(&[]),
            json!({"event": "COMMENT", "message": "", "findings": {}})
        );
    }
}

#[test]
fn approving_a_run_that_is_not_safe_waits_for_the_users_decision() {
    for (verdict, findings, reasons) in [
        (
            "look_first",
            vec![nit()],
            vec!["the run's verdict is look_first: The question needs the author's answer."],
        ),
        (
            "approve_once_ci_passes",
            vec![bug(), nit(), question()],
            vec![
                "finding 1 is a caller_visible_bug: Bad JSON now returns 500 (services/orders/internal/api/orders.go:88)",
                "finding 2 is a question: Why retry twice? (services/orders/internal/api/orders.go:40)",
            ],
        ),
    ] {
        let s = Submit::new();
        s.write_findings(verdict, findings, "");
        s.draft(&["approve"]);
        for flags in [&[][..], &["--print"]] {
            let refused = s.submit(flags);
            assert_eq!(code(&refused), Some(11), "{}", err(&refused));
            assert_eq!(listed(&refused), reasons);
            assert!(err(&refused).contains("--approve-anyway"));
        }
        assert!(s.posted().is_empty());
        assert!(!s.run.join("submit-request.json").exists());
        assert_eq!(code(&s.submit(&["--approve-anyway"])), Some(0));
        assert_eq!(s.posted()[0]["event"], "APPROVE");
    }
}

#[test]
fn a_review_that_only_comments_needs_no_decision() {
    let s = Submit::new();
    s.draft(&["include", "1"]);
    assert_eq!(code(&s.submit(&[])), Some(0));
    assert_eq!(s.posted()[0]["event"], "COMMENT");
}

#[test]
fn a_skipped_or_failed_review_is_not_safe_to_approve_whatever_its_verdict() {
    let skipped = "The PR changes 40 files across four services.";
    let failed = "the review run passed its 45-minute limit";
    for (findings, skip_reason, failure, reason) in [
        (
            vec![],
            skipped,
            None,
            format!("the review was skipped: {skipped}"),
        ),
        (
            vec![nit()],
            "",
            Some(failed),
            format!("the review failed: {failed}"),
        ),
    ] {
        let s = Submit::new();
        s.write_findings("approve_once_ci_passes", findings, skip_reason);
        if let Some(failure) = failure {
            fs::write(s.run.join("failure.txt"), format!("{failure}\n")).unwrap();
        }
        assert_eq!(
            s.draft(&[]),
            json!({"event": "COMMENT", "message": "", "findings": {}})
        );
        s.draft(&["approve"]);
        for flags in [&[][..], &["--print"]] {
            let refused = s.submit(flags);
            assert_eq!(code(&refused), Some(11), "{}", err(&refused));
            assert_eq!(listed(&refused), std::slice::from_ref(&reason));
            assert!(err(&refused).contains("--approve-anyway"));
        }
        assert!(s.posted().is_empty());
        assert_eq!(code(&s.submit(&["--approve-anyway"])), Some(0));
        assert_eq!(
            s.posted(),
            [json!({"commit_id": "h1", "event": "APPROVE", "body": "", "comments": []})]
        );
    }
    // Only an approval waits for the user's look: a failed review's comments post as they are.
    let s = Submit::new();
    s.write_findings("approve_once_ci_passes", vec![nit()], "");
    fs::write(s.run.join("failure.txt"), format!("{failed}\n")).unwrap();
    s.draft(&["include", "3"]);
    assert_eq!(code(&s.submit(&[])), Some(0));
    assert_eq!(s.posted()[0]["event"], "COMMENT");
}

#[test]
fn a_review_still_running_cannot_be_submitted() {
    // A Devin review writes findings.json itself and may change it until it ends.
    let s = Submit::new();
    s.write_findings("approve_once_ci_passes", vec![nit()], "");
    let (lock, mut holder) = s.hold();
    for drafts in [
        None,
        Some(json!({"event": "COMMENT", "message": "", "findings": {"3": {"included": true}}})),
    ] {
        if let Some(drafts) = drafts {
            fs::write(s.run.join("drafts.json"), drafts.to_string()).unwrap();
        }
        for flags in [&[][..], &["--print"], &["--approve-anyway"]] {
            let refused = s.submit(flags);
            assert_eq!(
                (code(&refused), out(&refused), err(&refused)),
                (
                    Some(1),
                    String::new(),
                    "whooper-submit: the review is still running; submit it once it ends\n".into()
                )
            );
        }
    }
    assert!(s.posted().is_empty());
    assert_eq!(
        s.files(),
        ["diff.patch", "drafts.json", "findings.json", "run.json"]
    );
    // whooper review deletes the lock when the review ends.
    holder.kill().unwrap();
    holder.wait().unwrap();
    fs::remove_dir_all(lock).unwrap();
    assert_eq!(code(&s.submit(&[])), Some(0));
    assert_eq!(s.posted()[0]["event"], "COMMENT");
}

#[test]
fn status_and_show_call_a_run_safe_to_approve_exactly_when_submit_approves_it_without_approve_anyway()
 {
    let why = "The PR changes 40 files across four services.";
    type Case<'a> = (&'a str, &'a str, Vec<Value>, &'a str, Option<i32>, bool);
    let cases: Vec<Case> = vec![
        (
            "only nits",
            "approve_once_ci_passes",
            vec![nit(), other_nit()],
            "",
            Some(0),
            true,
        ),
        (
            "no finding",
            "approve_once_ci_passes",
            vec![],
            "",
            Some(0),
            true,
        ),
        (
            "the verdict is look_first",
            "look_first",
            vec![nit()],
            "",
            Some(11),
            false,
        ),
        (
            "a finding that is not a nit",
            "approve_once_ci_passes",
            vec![nit(), question()],
            "",
            Some(11),
            false,
        ),
        (
            "the review skipped the PR",
            "approve_once_ci_passes",
            vec![],
            why,
            Some(11),
            false,
        ),
        (
            "the review failed",
            "approve_once_ci_passes",
            vec![nit()],
            "",
            Some(11),
            false,
        ),
        (
            "the review still runs",
            "approve_once_ci_passes",
            vec![nit()],
            "",
            Some(1),
            false,
        ),
    ];
    for (name, verdict, findings, skip_reason, exit, safe) in cases {
        let s = Submit::new();
        s.write_findings(verdict, findings, skip_reason);
        let mut held = None;
        if name == "the review failed" {
            fs::write(
                s.run.join("failure.txt"),
                "the review run passed its 45-minute limit\n",
            )
            .unwrap();
        }
        if name == "the review still runs" {
            held = Some(s.hold());
        }
        s.draft(&["approve"]);
        // --print runs every check submit runs before it posts, and posts nothing.
        let submitted = s.submit(&["--print"]);
        let status = s.w.status();
        let shown: Value = serde_json::from_str(&s.w.ok(&["show", RUN_ID])).unwrap();
        if let Some((_, mut holder)) = held {
            holder.kill().unwrap();
            holder.wait().unwrap();
        }
        assert_eq!(
            (
                code(&submitted),
                status["reviews"][0]["safe_to_approve"].clone(),
                shown["safe_to_approve"].clone()
            ),
            (exit, json!(safe), json!(safe)),
            "{name}"
        );
    }
}

#[test]
fn an_approval_without_a_message_says_pre_approving_only_when_it_comments() {
    let s = Submit::new();
    s.write_findings("approve_once_ci_passes", vec![nit(), other_nit()], "");
    for (changes, message) in [
        (vec![], "Pre-approving to unblock"),
        (vec![vec!["leave-out", "3"], vec!["leave-out", "4"]], ""),
        (
            vec![vec!["message", "Thanks, looks good."]],
            "Thanks, looks good.",
        ),
        (
            vec![
                vec!["leave-out", "3"],
                vec!["leave-out", "4"],
                vec!["message", "LGTM"],
            ],
            "LGTM",
        ),
        (vec![vec!["comment"]], ""),
    ] {
        let _ = fs::remove_file(s.run.join("drafts.json"));
        for change in changes {
            s.draft(&change);
        }
        s.shown(&[]);
        assert_eq!(s.request()["body"], message);
    }
}

#[test]
fn draft_records_each_choice_and_prints_the_drafts() {
    let s = Submit::new();
    // The run is not safe to approve, so before any change it only comments and includes nothing.
    assert_eq!(
        s.draft(&[]),
        json!({"event": "COMMENT", "message": "", "findings": {}})
    );
    assert!(!s.run.join("drafts.json").exists());
    assert_eq!(
        s.draft(&["text", "2", "**question:** Why retry?"])["findings"],
        json!({"2": {"included": false, "text": "**question:** Why retry?"}})
    );
    let mut drafts = Value::Null;
    for change in [
        &["include", "2"][..],
        &["include", "3"],
        &["leave-out", "3"],
        &["approve"],
        &["message", "Pre-approving to unblock"],
    ] {
        drafts = s.draft(change);
    }
    assert_eq!(
        drafts,
        json!({"event": "APPROVE", "message": "Pre-approving to unblock", "findings": {
            "2": {"included": true, "text": "**question:** Why retry?"}, "3": {"included": false}}})
    );
    let mut commented = drafts.clone();
    commented["event"] = json!("COMMENT");
    assert_eq!(s.draft(&["comment"]), commented);
    // Each change went to a temporary file renamed into place, so only drafts.json is new.
    assert_eq!(read_json(&s.run.join("drafts.json")), commented);
    assert_eq!(
        s.files(),
        ["diff.patch", "drafts.json", "findings.json", "run.json"]
    );
}

#[test]
fn two_changes_to_the_drafts_at_once_both_stay() {
    // Prompting Time's page includes finding 2 while Whooper's agent sets the message.
    for _ in 0..5 {
        let s = Submit::new();
        let page =
            s.w.command(&["draft", RUN_ID, "include", "2"])
                .spawn()
                .unwrap();
        let agent =
            s.w.command(&["draft", RUN_ID, "message", "Thanks!"])
                .spawn()
                .unwrap();
        assert!(page.wait_with_output().unwrap().status.success());
        assert!(agent.wait_with_output().unwrap().status.success());
        assert_eq!(
            s.draft(&[]),
            json!({"event": "COMMENT", "message": "Thanks!", "findings": {"2": {"included": true}}})
        );
    }
}

#[test]
fn draft_refuses_a_change_it_cannot_record() {
    let s = Submit::new();
    for (change, why) in [
        (&["include", "9"][..], "no finding 9"),
        (&["text", "1", " \n"], "empty"),
        (&["reject", "1"], "usage"),
        (&["text", "1"], "usage"),
    ] {
        let mut args = vec!["draft", RUN_ID];
        args.extend(change);
        let out = s.w.run(&args);
        assert_eq!(code(&out), Some(1));
        assert!(err(&out).contains(why), "{change:?}: {}", err(&out));
    }
    assert!(!s.run.join("drafts.json").exists());
    assert!(err(&s.w.run(&["draft"])).contains("usage"));
    let missing = s.w.run(&["draft", "20310101T000000Z-1"]);
    assert_eq!(code(&missing), Some(1));
    assert!(err(&missing).contains("no run 20310101T000000Z-1"));
}

#[test]
fn a_review_that_did_not_finish_has_no_drafts_and_cannot_be_submitted() {
    let s = Submit::new();
    let no_title = {
        let mut f = bug();
        f.as_object_mut().unwrap().remove("title");
        json!({"status": "reviewed", "skip_reason": "", "approval": {"verdict": "look_first", "reason": "Ran no probe."},
               "findings": [f]})
    };
    let cases: Vec<(Option<String>, String)> = vec![
        (
            None,
            "has no findings.json, so its review did not finish".into(),
        ),
        (
            Some(String::new()),
            format!("{UNFIT}EOF while parsing a value at line 1 column 0"),
        ),
        (
            Some(r#"{"status": "revi"#.into()),
            format!("{UNFIT}EOF while parsing a string at line 1 column 16"),
        ),
        (
            Some("{}".into()),
            format!("{UNFIT}findings.json lacks status, skip_reason, approval, findings"),
        ),
        (
            Some(json!({"findings": [bug()]}).to_string()),
            format!("{UNFIT}findings.json lacks status, skip_reason, approval"),
        ),
        (
            Some(no_title.to_string()),
            format!("{UNFIT}findings.json.findings[0] lacks title"),
        ),
    ];
    let resolved = fs::canonicalize(&s.run).unwrap();
    for (content, why) in cases {
        match &content {
            None => {
                let _ = fs::remove_file(s.run.join("findings.json"));
            }
            Some(text) => fs::write(s.run.join("findings.json"), text).unwrap(),
        }
        for (args, command, run) in [
            (vec!["draft", RUN_ID], "draft", &s.run),
            (vec!["draft", RUN_ID, "approve"], "draft", &s.run),
            (vec!["submit", RUN_ID], "submit", &resolved),
        ] {
            let out = s.w.run(&args);
            assert_eq!(
                (code(&out), err(&out)),
                (
                    Some(1),
                    format!("whooper-{command}: {} {why}\n", run.display())
                ),
                "{args:?}"
            );
        }
    }
    assert_eq!(s.files(), ["diff.patch", "findings.json", "run.json"]);
    assert!(s.gh_calls().is_empty());
}

#[test]
fn submit_refuses_a_run_json_it_cannot_read() {
    // run.json that is empty or cut short, as a review stopped while writing it leaves.
    let s = Submit::new();
    for content in ["", r#"{"repo": "example-org/exa"#] {
        fs::write(s.run.join("run.json"), content).unwrap();
        let result = s.submit(&[]);
        assert_eq!(
            (code(&result), out(&result), err(&result)),
            (
                Some(1),
                String::new(),
                format!(
                    "whooper-submit: cannot read {}\n",
                    s.run.join("run.json").display()
                )
            )
        );
    }
    assert!(s.posted().is_empty());
}
