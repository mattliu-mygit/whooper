//! Black-box tests of whooper ask on a finished Claude review of example-org/example-repo#12, with a
//! fake claude that records its arguments, working directory, and prompt, and answers with the file
//! answer; and of the claude review run's arguments.

mod common;

use std::fs;
use std::path::PathBuf;
use std::process::Output;

use common::*;
use serde_json::{Value, json};

const RUN_ID: &str = "20310727T010246Z-12";
const QUESTION: &str = "Why is it bad to have invisible characters?";

fn finding_of(id: Value, title: &str) -> Value {
    json!({"id": id, "path": "README.md", "line": 3, "side": "RIGHT", "title": title,
           "draft": "**nit:** Line 3 has a zero-width space.", "kind": "nit"})
}

struct Ask {
    w: Whooper,
    run: PathBuf,
}

impl Ask {
    fn new() -> Ask {
        let w = Whooper::new();
        fs::write(
            w.fakes.join("answer"),
            "They hide a change from the reader.",
        )
        .unwrap();
        w.fake(
            "claude",
            r#"fakes="${0%/*}"
printf '%s\n' "$@" > "$fakes/args"
pwd > "$fakes/cwd"
ls -A > "$fakes/files"
answer=$(cat "$fakes/answer")
printf '{"type": "result", "subtype": "success", "result": "%s"}\n' "$answer""#,
        );
        let run = w.state.join("runs").join(RUN_ID);
        fs::create_dir_all(&run).unwrap();
        fs::write(
            run.join("run.json"),
            json!({"repo": "example-org/example-repo", "pr": 12, "head_sha": "h1",
            "run_id": RUN_ID, "agent": "claude"})
            .to_string(),
        )
        .unwrap();
        fs::write(
            run.join("findings.json"),
            json!({"status": "reviewed", "skip_reason": "",
            "approval": {"verdict": "look_first", "reason": "Finding 1 needs the author."},
            "findings": [finding_of(json!("1"), "Bad JSON now returns 500"),
                         finding_of(json!("2"), "Invisible characters in the setup steps")]})
            .to_string(),
        )
        .unwrap();
        for name in [
            "log.md",
            "github.json",
            "diff.patch",
            "transcript.jsonl",
            "prompt.md",
            "stderr.log",
        ] {
            fs::write(run.join(name), format!("the review's {name}")).unwrap();
        }
        Ask { w, run }
    }

    fn ask(&self, args: &[&str]) -> Output {
        let mut all = vec!["ask"];
        all.extend(args);
        self.w.run(&all)
    }

    fn prompt(&self, n: usize) -> String {
        fs::read_to_string(self.run.join(format!("asks/{n}.prompt.md"))).unwrap()
    }

    fn recorded(&self, name: &str) -> Vec<String> {
        fs::read_to_string(self.w.fakes.join(name))
            .unwrap()
            .lines()
            .map(String::from)
            .collect()
    }
}

fn text(o: &Output) -> (Option<i32>, String, String) {
    (
        o.status.code(),
        String::from_utf8_lossy(&o.stdout).into_owned(),
        String::from_utf8_lossy(&o.stderr).into_owned(),
    )
}

#[test]
fn the_prompt_names_the_question_the_finding_and_each_record_file() {
    let a = Ask::new();
    for name in [
        "drafts.json",
        "overview.json",
        "overview.invalid.json",
        "connections.json",
        "test_gaps.json",
        "examples.json",
    ] {
        fs::write(a.run.join(name), "{}").unwrap();
    }
    fs::create_dir_all(a.run.join("asks")).unwrap();
    fs::write(a.run.join("asks/1.json"), "{}").unwrap();
    fs::write(a.run.join("asks/1.prompt.md"), "an earlier ask's prompt").unwrap();
    // Braces in a question stay as written instead of reading as placeholders.
    let question = format!("{QUESTION} Is {{{{this}}}} one?");
    let out = a.ask(&[RUN_ID, &question, "--finding", "2"]);
    assert_eq!(text(&out).0, Some(0), "{}", text(&out).2);
    let prompt = a.prompt(2);
    for name in [
        "log.md",
        "findings.json",
        "overview.json",
        "overview.invalid.json",
        "diff.patch",
        "connections.json",
        "test_gaps.json",
        "examples.json",
        "github.json",
        "transcript.jsonl",
        "run.json",
        "prompt.md",
        "drafts.json",
        "asks/1.json",
    ] {
        assert!(
            prompt.contains(&format!("{}: ", a.run.join(name).display())),
            "{name}"
        );
    }
    for name in [
        "transcript.json",
        "submit.json",
        "stderr.log",
        "asks/1.prompt.md",
    ] {
        assert!(
            !prompt.contains(&format!("{}: ", a.run.join(name).display())),
            "{name}"
        );
    }
    assert!(prompt.contains("finding 2, Invisible characters in the setup steps (README.md:3)"));
    assert!(prompt.ends_with(&format!("\n{question}\n")));
    for want in [
        "sam-example",
        "`review-skill`, `language-skill`",
        "the Skill tool",
    ] {
        assert!(prompt.contains(want), "{want}");
    }
    assert!(!prompt.replace(&question, "").contains("{{"));
}

#[test]
fn the_claude_ask_run_gets_only_tools_that_read_files_and_every_slack_write_denied() {
    let a = Ask::new();
    let out = a.ask(&[RUN_ID, QUESTION]);
    assert_eq!(
        text(&out),
        (
            Some(0),
            "They hide a change from the reader.\n".into(),
            String::new()
        )
    );
    let args = a.recorded("args");
    let after = |flag: &str| args[args.iter().position(|a| a == flag).unwrap() + 1].clone();
    assert_eq!(after("--tools"), "Read,Grep,Glob,Skill");
    assert_eq!(after("--permission-mode"), "dontAsk");
    let add = args.iter().position(|a| a == "--add-dir").unwrap();
    assert_eq!(
        fs::canonicalize(&args[add + 1]).unwrap(),
        fs::canonicalize(&a.run).unwrap()
    );
    assert!(args[add + 2].ends_with("/.claude/skills"));
    assert!(after("--disallowedTools").contains("mcp__claude_ai_Slack__slack_send_message_draft"));
    assert!(args.contains(&"--no-session-persistence".to_string()));
    assert!(!args.contains(&"--allowedTools".to_string()));
    // It runs in an empty folder, so no project's settings load.
    assert_eq!(a.recorded("files"), Vec::<String>::new());
    let saved = read_json(&a.run.join("asks/1.json"));
    assert_eq!(
        (
            saved["question"].clone(),
            saved["answer"].clone(),
            saved["agent"].clone()
        ),
        (
            json!(QUESTION),
            json!("They hide a change from the reader."),
            json!("claude")
        )
    );
}

#[test]
fn ask_refuses_a_run_or_finding_it_cannot_ask_about() {
    let a = Ask::new();
    for (args, why) in [
        (
            vec!["20310101T000000Z-1", QUESTION],
            "no run 20310101T000000Z-1",
        ),
        (vec![RUN_ID], "usage: whooper ask"),
        (
            vec![RUN_ID, QUESTION, "--finding", "9"],
            "has no finding 9; its findings are 1, 2",
        ),
    ] {
        let out = a.ask(&args);
        assert_eq!(out.status.code(), Some(1));
        assert!(text(&out).2.contains(why), "{args:?}: {}", text(&out).2);
    }
    assert!(!a.run.join("asks").exists());
}

#[test]
fn ask_refuses_a_finding_when_findings_json_holds_none_it_can_read_or_gives_ids_as_numbers() {
    let a = Ask::new();
    for (content, why) in [
        (
            "".to_string(),
            "its findings.json holds no findings Whooper can read",
        ),
        (
            "{}".to_string(),
            "its findings.json holds no findings Whooper can read",
        ),
        (json!({"findings": []}).to_string(), "its findings are none"),
        (
            json!({"findings": [finding_of(json!(1), "a"), finding_of(json!(2), "b")]}).to_string(),
            "its findings are 1, 2",
        ),
    ] {
        fs::write(a.run.join("findings.json"), &content).unwrap();
        let out = a.ask(&[RUN_ID, QUESTION, "--finding", "1"]);
        let (code, stdout, stderr) = text(&out);
        assert_eq!((code, stdout), (Some(1), String::new()));
        assert!(
            stderr.contains(&format!("whooper-ask: the run has no finding 1; {why}\n")),
            "{content}: {stderr}"
        );
    }
    assert!(!a.run.join("asks").exists());
}

#[test]
fn a_review_that_did_not_finish_can_be_asked_why_and_unfit_findings_can_still_be_asked_about() {
    let a = Ask::new();
    let findings = fs::read_to_string(a.run.join("findings.json")).unwrap();
    fs::rename(
        a.run.join("findings.json"),
        a.run.join("findings.invalid.json"),
    )
    .unwrap();
    fs::write(
        a.run.join("failure.txt"),
        "the review run's findings.json does not fit the schema\n",
    )
    .unwrap();
    fs::write(a.run.join("stdout.log"), "the review's stdout.log").unwrap();
    let out = a.ask(&[RUN_ID, "Why did this review fail?"]);
    assert_eq!(out.status.code(), Some(0), "{}", text(&out).2);
    let prompt = a.prompt(1);
    let run = a.run.display().to_string();
    let listed: Vec<&str> = prompt
        .lines()
        .filter(|l| l.starts_with(&format!("- {run}/")))
        .collect();
    assert!(
        listed[0].starts_with(&format!("- {run}/failure.txt: ")),
        "{listed:?}"
    );
    for name in [
        "findings.invalid.json",
        "stderr.log",
        "stdout.log",
        "log.md",
        "diff.patch",
        "transcript.jsonl",
    ] {
        assert!(prompt.contains(&format!("{run}/{name}: ")), "{name}");
    }
    assert!(!prompt.contains(&format!("{run}/findings.json: ")));
    assert!(
        text(&a.ask(&[RUN_ID, QUESTION, "--finding", "1"]))
            .2
            .contains("has no finding 1; its findings are none")
    );
    // Findings that do not fit the schema, as a Devin review stopped at its limit leaves, can be asked about.
    let only: Value = serde_json::from_str(&findings).unwrap();
    fs::write(
        a.run.join("findings.json"),
        json!({"findings": only["findings"]}).to_string(),
    )
    .unwrap();
    let out = a.ask(&[RUN_ID, QUESTION, "--finding", "2"]);
    assert_eq!(out.status.code(), Some(0), "{}", text(&out).2);
    assert!(
        a.prompt(2)
            .contains("finding 2, Invisible characters in the setup steps (README.md:3)")
    );
}

#[test]
fn a_lock_whose_process_is_another_program_does_not_stop_an_ask_but_a_live_review_does() {
    let a = Ask::new();
    let lock = a.w.state.join("locks/example-org-example-repo-12");
    let mut other = std::process::Command::new("sleep")
        .arg("30")
        .spawn()
        .unwrap();
    a.w.hold(&lock, other.id());
    fs::write(lock.join("run_id"), format!("{RUN_ID}\n")).unwrap();
    assert_eq!(a.ask(&[RUN_ID, QUESTION]).status.code(), Some(0));
    other.kill().unwrap();
    other.wait().unwrap();
    let mut holder = a.w.holder("whooper-review");
    a.w.hold(&lock, holder.id());
    let out = a.ask(&[RUN_ID, QUESTION]);
    holder.kill().unwrap();
    holder.wait().unwrap();
    assert_eq!((text(&out).0, text(&out).1), (Some(1), String::new()));
    assert!(text(&out).2.contains(&format!(
        "the review is still running, in process {}; ask again once it ends",
        holder.id()
    )));
}

#[test]
fn a_review_has_no_spending_limit_unless_the_config_sets_one() {
    for (budget, want) in [(true, Some("15")), (false, None)] {
        let w = Whooper::new();
        if !budget {
            w.set_config("budget_usd = 15\n", "");
        }
        let (item, _) = w.pull_request(
            &[("app.go", "package app\n")],
            &[("app.go", "package app\n\nvar X = 1\n")],
        );
        w.fake_claude(&format!(
            "printf '%s\\n' \"$@\" > '{}'",
            w.root().join("args").display()
        ));
        let out = w.run(&["review", item.to_str().unwrap()]);
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        let args: Vec<String> = fs::read_to_string(w.root().join("args"))
            .unwrap()
            .lines()
            .map(String::from)
            .collect();
        let at = args.iter().position(|a| a == "--max-budget-usd");
        assert_eq!(at.map(|i| args[i + 1].as_str()), want);
        assert_eq!(
            args[args.iter().position(|a| a == "--model").unwrap() + 1],
            "review-model"
        );
    }
}
