//! Black-box tests of the commands that run agents and git: review, ask, fetch, poll, round, and run,
//! with fake gh, slackdump, and claude, and a local origin for the PR.

mod common;

use std::fs;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::{Duration, Instant};

use common::*;
use serde_json::{Value, json};

fn review(w: &Whooper, item: &std::path::Path) -> (std::process::Output, PathBuf) {
    let out = w.run(&["review", item.to_str().unwrap()]);
    let run = PathBuf::from(String::from_utf8_lossy(&out.stdout).trim());
    (out, run)
}

fn err(out: &std::process::Output) -> String {
    String::from_utf8_lossy(&out.stderr).into_owned()
}

#[test]
fn a_review_keeps_its_diff_findings_and_connections_and_cleans_up() {
    let w = Whooper::new();
    let (item, merge_base) = w.pull_request(
        &[(
            "app.py",
            "def total(cart):\n    return 1\n\n\ndef caller():\n    return total(None)\n",
        )],
        &[(
            "app.py",
            "def total(cart):\n    return 2\n\n\ndef caller():\n    return total(None)\n",
        )],
    );
    w.fake_claude("echo '# log' > \"$WHOOPER_RUN/log.md\"; [ \"$GH_TOKEN\" = disabled ] && pwd > \"$WHOOPER_RUN/cwd.txt\";");
    let (out, run) = review(&w, &item);
    assert!(out.status.success(), "{}", err(&out));
    assert!(
        fs::read_to_string(run.join("diff.patch"))
            .unwrap()
            .contains("+    return 2")
    );
    let ran = read_json(&run.join("run.json"));
    assert_eq!(
        (
            ran["merge_base"].as_str().unwrap(),
            ran["agent"].as_str().unwrap()
        ),
        (merge_base.as_str(), "claude")
    );
    assert_eq!(read_json(&run.join("findings.json"))["status"], "reviewed");
    assert!(
        fs::read_to_string(run.join("cwd.txt"))
            .unwrap()
            .trim()
            .ends_with("/head")
    );
    let prompt = fs::read_to_string(run.join("prompt.md")).unwrap();
    assert!(
        prompt.contains(
            "- Pull request: https://github.com/sample-org/plain-repo/pull/12, base main."
        )
    );
    assert!(prompt.contains("Run `make test`.") || prompt.contains("None."));
    let connections = read_json(&run.join("connections.json"));
    let edges = connections["edges"].as_array().unwrap();
    assert_eq!(edges[0]["from"], "app.py:caller");
    assert_eq!(edges[0]["to"], "app.py:total");
    assert_eq!(edges[0]["found_by"], "name_search");
    assert!(!run.join("head").exists() && !run.join("base").exists() && !run.join("tmp").exists());
    assert!(!w.state.join("locks/sample-org-plain-repo-12").exists());
    let record = fs::read_dir(w.state.join("prs/sample-org-plain-repo-12"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    assert_eq!(read_json(&record)["status"], "done");
    let shown: Value =
        serde_json::from_str(&w.ok(&["show", run.file_name().unwrap().to_str().unwrap()])).unwrap();
    assert_eq!(shown["outcome"], "reviewed");
}

#[test]
fn a_setup_script_that_cannot_reach_docker_stops_the_review_names_the_cause_and_the_teardown_runs()
{
    let w = Whooper::new();
    let (item, _) = w.pull_request(&[("a.txt", "1\n")], &[("a.txt", "2\n")]);
    w.set_config(
        "[repos.\"sample-org/plain-repo\"]\n",
        "[repos.\"sample-org/plain-repo\"]\nsetup = \"setup.sh\"\nteardown = \"teardown.sh\"\n",
    );
    script(
        &w.config.parent().unwrap().join("setup.sh"),
        "echo 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock' >&2\nexit 1",
    );
    let torn = w.root().join("torn");
    script(
        &w.config.parent().unwrap().join("teardown.sh"),
        &format!("echo \"$WHOOPER_RUN_ID\" > '{}'", torn.display()),
    );
    let (out, run) = review(&w, &item);
    assert_eq!(out.status.code(), Some(1));
    assert!(
        fs::read_to_string(run.join("failure.txt"))
            .unwrap()
            .contains("setup.sh failed: Cannot connect to the Docker daemon")
    );
    assert_eq!(
        read_json(&run.join("failure.json"))["id"],
        "docker_unavailable"
    );
    assert_eq!(
        fs::read_to_string(&torn).unwrap().trim(),
        run.file_name().unwrap().to_str().unwrap()
    );
    let status = w.status();
    assert_eq!(status["blocked"]["id"], "docker_unavailable");
}

#[test]
fn a_review_of_a_repo_without_a_clone_or_table_fails_with_its_cause() {
    let w = Whooper::new();
    let item = w.root().join("item.json");
    fs::write(
        &item,
        work_item("sample-org/plain-repo", 12, &"a".repeat(40)).to_string(),
    )
    .unwrap();
    let (out, run) = review(&w, &item);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(
        read_json(&run.join("failure.json"))["id"],
        "repo_not_configured"
    );
    fs::write(
        &item,
        work_item("other-org/repo", 12, &"a".repeat(40)).to_string(),
    )
    .unwrap();
    let out = w.run(&["review", item.to_str().unwrap()]);
    assert_eq!(out.status.code(), Some(1));
    assert!(err(&out).contains("other-org/repo has no [repos.\"other-org/repo\"] table"));
    assert!(out.stdout.is_empty());
}

#[test]
fn findings_that_do_not_fit_move_aside_and_an_overview_in_findings_moves_to_overview_json() {
    let w = Whooper::new();
    let (item, _) = w.pull_request(&[("a.txt", "1\n")], &[("a.txt", "2\n")]);
    w.fake_claude("");
    fs::write(
        w.fakes.join("result.json"),
        json!({"type": "result", "subtype": "success",
        "structured_output": {"status": "nope", "overview": {"summary": "x"}}})
        .to_string(),
    )
    .unwrap();
    let (out, run) = review(&w, &item);
    assert_eq!(out.status.code(), Some(1));
    assert!(run.join("findings.invalid.json").exists());
    assert!(err(&out).contains("moved the overview in the run's findings.json to overview.json"));
    assert_eq!(
        read_json(&run.join("failure.json"))["id"],
        "agent_no_findings"
    );
    assert!(run.join("overview.invalid.json").exists());
}

#[test]
fn a_review_refuses_a_pr_a_live_review_holds_and_takes_over_a_stale_lock() {
    let w = Whooper::new();
    let (item, _) = w.pull_request(&[("a.txt", "1\n")], &[("a.txt", "2\n")]);
    let lock = w.state.join("locks/sample-org-plain-repo-12");
    let mut holder = w.holder("whooper-review");
    w.hold(&lock, holder.id());
    let out = w.run(&["review", item.to_str().unwrap()]);
    assert!(err(&out).contains(&format!(
        "process {} is already reviewing sample-org/plain-repo#12",
        holder.id()
    )));
    assert_eq!(
        fs::read_to_string(lock.join("pid")).unwrap(),
        format!("{}\n", holder.id())
    );
    holder.kill().unwrap();
    holder.wait().unwrap();
    let mut other = w.holder("sleeper");
    fs::write(lock.join("pid"), format!("{}\n", other.id())).unwrap();
    w.fake_claude("");
    let (out, _) = review(&w, &item);
    assert!(out.status.success(), "{}", err(&out));
    assert!(!lock.exists());
    other.kill().unwrap();
    other.wait().unwrap();
}

#[test]
fn a_go_review_measures_test_gaps_before_its_run() {
    let w = Whooper::new();
    let base = [
        ("go.mod", "module example.com/p\n\ngo 1.21\n"),
        (
            "p/p.go",
            "package p\n\nfunc Clamp(n int) int {\n\treturn n\n}\n",
        ),
        (
            "p/p_test.go",
            "package p\n\nimport \"testing\"\n\nfunc TestClamp(t *testing.T) { Clamp(1) }\n",
        ),
    ];
    let head = [(
        "p/p.go",
        "package p\n\nfunc Clamp(n int) int {\n\tif n > 100 {\n\t\treturn 100\n\t}\n\treturn n\n}\n",
    )];
    let (item, _) = w.pull_request(&base, &head);
    w.fake_claude("cp \"$WHOOPER_RUN/test_gaps.json\" \"$WHOOPER_RUN/seen.json\"; echo changed > \"$WHOOPER_RUN/test_gaps.json\";");
    let (out, run) = review(&w, &item);
    assert!(out.status.success(), "{}", err(&out));
    let gaps = read_json(&run.join("seen.json"));
    assert_eq!(
        read_json(&run.join("test_gaps.json"))["lines"],
        gaps["lines"]
    );
    assert_eq!(gaps["measured"], true, "{gaps}");
    assert_eq!(
        gaps["lines"],
        json!([{"path": "p/p.go", "line": 5, "status": "not_run"}])
    );
    assert_eq!(
        gaps["summary"],
        json!({"changed_lines": 2, "run": 1, "not_run": 1})
    );
}

#[test]
fn an_ask_saves_each_answer_numbered_and_refuses_what_it_cannot_ask_about() {
    let w = Whooper::new();
    let id = "20310727T010246Z-12";
    w.finished_run(
        id,
        "sample-org/plain-repo",
        12,
        findings(vec![finding("1", "**nit:** x")], "look_first"),
    );
    w.fake_claude("");
    assert_eq!(w.ok(&["ask", id, "Why?"]).trim(), "It stopped.");
    w.ok(&["ask", id, "And {this}?", "--finding", "1"]);
    let run = w.state.join("runs").join(id);
    let second = read_json(&run.join("asks/2.json"));
    assert_eq!(
        (second["question"].as_str(), second["finding"].as_str()),
        (Some("And {this}?"), Some("1"))
    );
    assert!(
        fs::read_to_string(run.join("asks/2.prompt.md"))
            .unwrap()
            .contains("The question is about finding 1, A title (app.go:2)")
    );
    let out = w.run(&["ask", id, "Q", "--finding", "9"]);
    assert!(err(&out).contains("the run has no finding 9; its findings are 1"));
    let mut holder = w.holder("whooper-review");
    let lock = w.state.join("locks/sample-org-plain-repo-12");
    w.hold(&lock, holder.id());
    fs::write(lock.join("run_id"), id).unwrap();
    assert!(err(&w.run(&["ask", id, "Q"])).contains("the review is still running"));
    holder.kill().unwrap();
    holder.wait().unwrap();
}

#[test]
fn an_ask_past_its_time_limit_is_stopped_and_saves_nothing() {
    let w = Whooper::new();
    let id = "20310727T010246Z-12";
    w.finished_run(
        id,
        "sample-org/plain-repo",
        12,
        findings(vec![], "look_first"),
    );
    fs::write(
        &w.config,
        fs::read_to_string(&w.config).unwrap() + "\n[ask]\ntimeout_minutes = 0.02\n",
    )
    .unwrap();
    w.fake("claude", "sleep 30");
    let started = Instant::now();
    let out = w.run(&["ask", id, "Q"]);
    assert!(started.elapsed() < Duration::from_secs(15));
    assert!(err(&out).contains("the ask run passed its 0.02-minute limit"));
    assert!(!w.state.join("runs").join(id).join("asks/1.json").exists());
}

fn fake_github_and_slack(w: &Whooper, prs: &[Value], posts: Value, answer: Value) {
    for p in prs {
        let name = p["url"]
            .as_str()
            .unwrap()
            .trim_start_matches("https://github.com/")
            .replace('/', "-");
        fs::write(w.fakes.join(format!("{name}.json")), p.to_string()).unwrap();
    }
    let requested: Vec<&str> = prs
        .iter()
        .filter(|p| p["reviewRequests"]["totalCount"] != 0)
        .map(|p| p["url"].as_str().unwrap())
        .collect();
    fs::write(w.fakes.join("requested"), requested.join("\n") + "\n").unwrap();
    w.fake("gh", "case \"$*\" in\n  \"search prs user-review-requested:sam-example --owner example-org\"*) cat \"${0%/*}/requested\" ;;\n  \"search prs \"*) ;;\n  \"api graphql \"*) for arg; do case \"$arg\" in url=https://github.com/*) cat \"${0%/*}/$(echo \"${arg#url=https://github.com/}\" | tr / -).json\"; exit ;; esac; done; exit 1 ;;\n  *) exit 1 ;;\nesac");
    fs::write(
        w.fakes.join("dump.json"),
        json!({"messages": posts}).to_string(),
    )
    .unwrap();
    w.fake("slackdump", "while [ $# -gt 1 ]; do [ \"$1\" = -o ] && out=\"$2\"; shift; done\nmkdir -p \"$out\" && cp \"${0%/*}/dump.json\" \"$out/$1.json\"");
    fs::write(
        w.fakes.join("answer.json"),
        json!({"type": "result", "subtype": "success", "structured_output": answer}).to_string(),
    )
    .unwrap();
    w.fake("claude", "cat \"${0%/*}/answer.json\"");
}

#[test]
fn fetch_and_poll_list_review_requests_and_slack_asks() {
    let w = Whooper::new();
    let ts = format!("{}.000100", jiff::Timestamp::now().as_second());
    let posts = json!([{"ts": ts, "user": "U1", "text": "<@U0000000000> please look at https://github.com/example-org/example-repo/pull/7"}]);
    let answer = json!({"asks": [{"ts": ts, "pr_urls": ["https://github.com/example-org/example-repo/pull/7"], "reason": "asked"}], "not_asks": []});
    fake_github_and_slack(
        &w,
        &[pr(3, &[LOGIN], vec![]), pr(7, &[], vec![])],
        posts,
        answer,
    );
    w.ok(&["fetch"]);
    assert_eq!(
        read_json(&w.state.join("poll/github.json"))
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let path = w.ok(&["poll"]);
    let items = read_json(std::path::Path::new(path.trim()));
    let found: Vec<(i64, &str)> = items["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| (i["pr"].as_i64().unwrap(), i["source"].as_str().unwrap()))
        .collect();
    assert_eq!(found, [(7, "slack_ask"), (3, "github_request")]);
    assert!(w.state.join("poll/cursor").exists());
    let prompt = fs::read_dir(w.state.join("poll"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| p.to_string_lossy().ends_with(".prompt.md"))
        .unwrap();
    assert!(
        fs::read_to_string(prompt)
            .unwrap()
            .contains("user_id: U0000000000")
    );
}

#[test]
fn a_poll_answer_that_leaves_out_a_post_asking_the_user_fails() {
    let w = Whooper::new();
    let ts = format!("{}.000100", jiff::Timestamp::now().as_second());
    let posts = json!([{"ts": ts, "user": "U1", "text": "<@U0000000000> https://github.com/example-org/example-repo/pull/7"}]);
    fake_github_and_slack(&w, &[], posts, json!({"asks": [], "not_asks": []}));
    w.ok(&["fetch"]);
    let out = w.run(&["poll"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(
        err(&out)
            .contains("the poll run left out posts that mention U0000000000 next to a PR link")
    );
    assert!(!w.state.join("poll/cursor").exists());
}

#[test]
fn a_round_reviews_each_selected_item_waits_after_a_cause_that_stops_every_review_and_reports() {
    let w = Whooper::new();
    let (item, _) = w.pull_request(&[("a.txt", "1\n")], &[("a.txt", "2\n")]);
    let head = read_json(&item)["head_sha"].clone();
    w.set_config(
        "[repos.\"sample-org/plain-repo\"]\n",
        "[repos.\"sample-org/plain-repo\"]\nsetup = \"setup.sh\"\n",
    );
    script(
        &w.config.parent().unwrap().join("setup.sh"),
        "echo 'Cannot connect to the Docker daemon' >&2; exit 1",
    );
    let mut first = pr(12, &[LOGIN], vec![]);
    first["url"] = json!("https://github.com/sample-org/plain-repo/pull/12");
    first["repository"]["nameWithOwner"] = json!("sample-org/plain-repo");
    let mut second = pr(13, &[LOGIN], vec![]);
    second["url"] = json!("https://github.com/sample-org/plain-repo/pull/13");
    second["repository"]["nameWithOwner"] = json!("sample-org/plain-repo");
    first["headRefOid"] = head.clone();
    second["headRefOid"] = head;
    fake_github_and_slack(&w, &[], json!([]), json!({"asks": [], "not_asks": []}));
    for p in [&first, &second] {
        let name = p["url"]
            .as_str()
            .unwrap()
            .trim_start_matches("https://github.com/")
            .replace('/', "-");
        fs::write(w.fakes.join(format!("{name}.json")), p.to_string()).unwrap();
    }
    fs::write(w.fakes.join("requested"), "").unwrap();
    w.fake("gh", "case \"$*\" in\n  \"search prs user-review-requested:sam-example --owner sample-org\"*) printf 'https://github.com/sample-org/plain-repo/pull/12\\nhttps://github.com/sample-org/plain-repo/pull/13\\n' ;;\n  \"search prs \"*) ;;\n  \"api graphql \"*) for arg; do case \"$arg\" in url=https://github.com/*) cat \"${0%/*}/$(echo \"${arg#url=https://github.com/}\" | tr / -).json\"; exit ;; esac; done; exit 1 ;;\n  *) exit 1 ;;\nesac");
    let out = w.run(&["round"]);
    assert!(out.status.success(), "{}", err(&out));
    let last = read_json(&w.state.join("last-round.json"));
    assert_eq!(last["result"], "done");
    assert_eq!(
        last["reviews"],
        json!([{"pr": "sample-org/plain-repo#12", "outcome": "failed"}, {"pr": "sample-org/plain-repo#13", "outcome": "waiting"}])
    );
    let history = fs::read_to_string(w.state.join("rounds.jsonl")).unwrap();
    assert!(
        history.contains("so 1 review(s) wait for the next round"),
        "{history}"
    );
    assert!(
        fs::read_to_string(w.root().join("notifications.log"))
            .unwrap()
            .contains("Whooper: sample-org/plain-repo#12 review failed")
    );
    assert!(
        w.state
            .join("rechecks/sample-org-plain-repo-13.json")
            .exists()
    );
    assert!(!w.state.join("round.lock").exists());
}

#[test]
fn a_round_or_run_refuses_while_a_live_copy_holds_its_lock() {
    let w = Whooper::new();
    for (command, lock, says) in [
        (
            "round",
            "round.lock",
            "a round is already running, in process",
        ),
        ("run", "run.lock", "is already running Whooper"),
    ] {
        let mut holder = w.holder(&format!("whooper-{command}"));
        w.hold(&w.state.join(lock), holder.id());
        let out = w.run(&[command]);
        assert_eq!(out.status.code(), Some(1));
        assert!(err(&out).contains(says), "{}", err(&out));
        holder.kill().unwrap();
        let _ = holder.wait();
    }
}

#[test]
fn a_paused_run_skips_rounds_and_a_stop_ends_it_and_removes_its_lock() {
    let w = Whooper::new();
    let mut run = w
        .command(&["run"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(30);
    while !w.state.join("run.lock/pid").exists() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(50));
    }
    w.ok(&["pause"]);
    while !w.state.join("next-round").exists() || !w.state.join("rounds.jsonl").exists() {
        assert!(
            Instant::now() < deadline + Duration::from_secs(90),
            "no round"
        );
        std::thread::sleep(Duration::from_millis(100));
    }
    unsafe { libc_kill(run.id() as i32, 15) };
    let status = run.wait().unwrap();
    assert_eq!(status.code(), Some(130));
    assert!(!w.state.join("run.lock").exists());
    let history = fs::read_to_string(w.state.join("rounds.jsonl")).unwrap();
    assert!(
        history
            .lines()
            .all(|l| l.contains("\"result\":\"skipped\"") || l.contains("poll_failed")),
        "{history}"
    );
}

unsafe extern "C" {
    #[link_name = "kill"]
    fn libc_kill(pid: i32, signal: i32) -> i32;
}
