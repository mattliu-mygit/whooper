//! A throwaway Whooper for black-box tests: a made-up config, a state folder, and fake gh, slackdump,
//! claude, and devin on PATH that fail unless a test says otherwise, so no test reaches GitHub, Slack,
//! or a model.
#![allow(dead_code)]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::{Value, json};

pub const LOGIN: &str = "sam-example";

pub const CONFIG: &str = r#"
[user]
name = "Sam"
github_login = "sam-example"

[github]
orgs = ["example-org", "sample-org"]
review_bots = [{ login = "ci-review-bot", label = "the CI review bot" }]

[slack]
workspace_url = "https://example.slack.com"
user_id = "U0000000000"
ask_channel = "C0000000001"
slackdump_workspace = "example"

[jobs]
review = "claude"
poll = "claude"

[agents.claude]
review_model = "review-model"
poll_model = "poll-model"
budget_usd = 15

[agents.devin]
review_model = "devin-review-model"
poll_model = "devin-poll-model"

[review]
timeout_minutes = 45
skills = ["review-skill", "language-skill"]

[schedule]
interval_minutes = 1
start = "00:00"
end = "23:59"
days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
notify = "notify.sh"

[repos."example-org/example-repo"]
notes = "notes.md"

[repos."sample-org/plain-repo"]
"#;

pub struct Whooper {
    pub tmp: tempfile::TempDir,
    pub fakes: PathBuf,
    pub config: PathBuf,
    pub state: PathBuf,
    pub home: PathBuf,
}

pub fn script(path: &Path, body: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, format!("#!/bin/sh\n{body}\n")).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}

pub fn read_json(path: &Path) -> Value {
    serde_json::from_str(
        &fs::read_to_string(path).unwrap_or_else(|e| panic!("{}: {e}", path.display())),
    )
    .unwrap()
}

impl Whooper {
    pub fn new() -> Whooper {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        let fakes = root.join("bin");
        for name in ["gh", "slackdump", "devin", "claude"] {
            script(&fakes.join(name), "exit 1");
        }
        let config = root.join("config/config.toml");
        fs::create_dir_all(config.parent().unwrap()).unwrap();
        fs::write(&config, CONFIG).unwrap();
        fs::write(root.join("config/notes.md"), "Run `make test`.").unwrap();
        script(
            &root.join("config/notify.sh"),
            &format!(
                "printf '%s | %s\\n' \"$1\" \"$2\" >> '{}'",
                root.join("notifications.log").display()
            ),
        );
        let home = root.join("home");
        fs::create_dir_all(home.join(".claude/skills")).unwrap();
        Whooper {
            state: root.join("whooper"),
            tmp,
            fakes,
            config,
            home,
        }
    }

    pub fn root(&self) -> PathBuf {
        self.state.parent().unwrap().to_path_buf()
    }

    pub fn command(&self, args: &[&str]) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_whooper"));
        command
            .args(args)
            .env(
                "PATH",
                format!(
                    "{}:{}",
                    self.fakes.display(),
                    std::env::var("PATH").unwrap()
                ),
            )
            .env("WHOOPER_CONFIG", &self.config)
            .env("XDG_STATE_HOME", self.root())
            .env("XDG_CACHE_HOME", self.root().join("cache"))
            .env("HOME", &self.home)
            .env("GIT_CONFIG_GLOBAL", self.home.join(".gitconfig"))
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_AUTHOR_NAME", "Sam")
            .env("GIT_AUTHOR_EMAIL", "sam@example.com")
            .env("GIT_COMMITTER_NAME", "Sam")
            .env("GIT_COMMITTER_EMAIL", "sam@example.com")
            .env("GOTELEMETRY", "off")
            .env_remove("WHOOPER_RUN");
        command
    }

    pub fn run(&self, args: &[&str]) -> Output {
        self.command(args).output().unwrap()
    }

    /// Runs args and returns stdout, failing the test unless it exits 0.
    pub fn ok(&self, args: &[&str]) -> String {
        let out = self.run(args);
        assert!(
            out.status.success(),
            "{args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).into_owned()
    }

    pub fn status(&self) -> Value {
        serde_json::from_str(&self.ok(&["status"])).unwrap()
    }

    pub fn fake(&self, name: &str, body: &str) {
        script(&self.fakes.join(name), body);
    }

    pub fn set_config(&self, from: &str, to: &str) {
        let text = fs::read_to_string(&self.config).unwrap();
        assert!(text.contains(from));
        fs::write(&self.config, text.replacen(from, to, 1)).unwrap();
    }

    /// Has claude answer a review run, which alone passes --json-schema, by running review, a line of
    /// sh, then printing a result with findings; and any other run with "It stopped.".
    pub fn fake_claude(&self, review: &str) {
        let result = json!({"type": "result", "subtype": "success", "total_cost_usd": 0, "duration_ms": 0,
            "num_turns": 1, "structured_output": {"status": "reviewed", "skip_reason": "", "findings": [],
            "approval": {"verdict": "approve_once_ci_passes", "reason": "Read the diff."}}});
        fs::write(self.fakes.join("result.json"), result.to_string()).unwrap();
        self.fake(
            "claude",
            &format!(
                "case \"$*\" in\n  *--json-schema*) {review}\n    cat \"${{0%/*}}/result.json\" ;;\n  *) echo '{{\"type\": \"result\", \"subtype\": \"success\", \"result\": \"It stopped.\"}}' ;;\nesac"
            ),
        );
    }

    fn git(&self, folder: &Path, args: &[&str]) -> String {
        let out = self.command(&[]).get_program().to_owned();
        let _ = out;
        let mut command = Command::new("git");
        command.args(args).current_dir(folder);
        for (k, v) in self.command(&[]).get_envs() {
            match v {
                Some(v) => command.env(k, v),
                None => command.env_remove(k),
            };
        }
        let out = command.output().unwrap();
        assert!(
            out.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).trim().to_string()
    }

    /// Makes PR 12 of sample-org/plain-repo in a local origin, which changes files from base to head,
    /// and the cache's clone of it, and returns the work item's path and the merge base.
    pub fn pull_request(&self, base: &[(&str, &str)], head: &[(&str, &str)]) -> (PathBuf, String) {
        let origin = self.root().join("origin");
        fs::create_dir_all(&origin).unwrap();
        let write = |files: &[(&str, &str)]| {
            for (name, text) in files {
                let path = origin.join(name);
                fs::create_dir_all(path.parent().unwrap()).unwrap();
                fs::write(path, text).unwrap();
            }
        };
        self.git(&origin, &["init", "--quiet", "--initial-branch=main"]);
        write(base);
        self.git(&origin, &["add", "."]);
        self.git(&origin, &["commit", "--quiet", "-m", "base"]);
        let merge_base = self.git(&origin, &["rev-parse", "HEAD"]);
        self.git(&origin, &["checkout", "--quiet", "-b", "pr-12"]);
        write(head);
        self.git(&origin, &["add", "--all"]);
        self.git(&origin, &["commit", "--quiet", "-m", "the PR"]);
        let sha = self.git(&origin, &["rev-parse", "HEAD"]);
        self.git(&origin, &["checkout", "--quiet", "main"]);
        let clone = self
            .root()
            .join("cache/whooper/repos/sample-org-plain-repo");
        fs::create_dir_all(clone.parent().unwrap()).unwrap();
        self.git(
            &self.root(),
            &[
                "clone",
                "--quiet",
                "--no-checkout",
                origin.to_str().unwrap(),
                clone.to_str().unwrap(),
            ],
        );
        let item = self.root().join("item.json");
        fs::write(
            &item,
            work_item("sample-org/plain-repo", 12, &sha).to_string(),
        )
        .unwrap();
        (item, merge_base)
    }

    /// Starts a process whose command line names command, standing in for a live copy of it.
    pub fn holder(&self, command: &str) -> std::process::Child {
        let path = self.root().join("holders").join(command);
        script(&path, "sleep 30");
        Command::new(&path).spawn().unwrap()
    }

    pub fn hold(&self, lock: &Path, pid: u32) {
        fs::create_dir_all(lock).unwrap();
        fs::write(lock.join("pid"), format!("{pid}\n")).unwrap();
    }

    /// A finished review run of PR pr in repo, with run.json, findings.json, and diff.patch, by run ID.
    pub fn finished_run(&self, run_id: &str, repo: &str, pr: i64, findings: Value) -> PathBuf {
        let run = self.state.join("runs").join(run_id);
        fs::create_dir_all(&run).unwrap();
        let mut item = work_item(repo, pr, &"a".repeat(40));
        item["run_id"] = json!(run_id);
        item["agent"] = json!("claude");
        item["merge_base"] = json!("b".repeat(40));
        fs::write(run.join("run.json"), item.to_string()).unwrap();
        fs::write(run.join("findings.json"), findings.to_string()).unwrap();
        fs::write(
            run.join("diff.patch"),
            "diff --git a/app.go b/app.go\n--- a/app.go\n+++ b/app.go\n@@ -1,1 +1,2 @@\n one\n+two\n",
        )
        .unwrap();
        run
    }
}

pub fn work_item(repo: &str, pr: i64, head: &str) -> Value {
    json!({"kind": "first_review", "repo": repo, "pr": pr, "head_sha": head, "base_ref": "main",
           "source": "github_request", "slack": null, "reason": "r"})
}

pub fn finding(id: &str, draft: &str) -> Value {
    json!({"id": id, "path": "app.go", "line": 2, "side": "RIGHT", "title": "A title", "draft": draft, "kind": "nit"})
}

pub fn findings(list: Vec<Value>, verdict: &str) -> Value {
    json!({"status": "reviewed", "skip_reason": "", "findings": list,
           "approval": {"verdict": verdict, "reason": "Because."}})
}

/// A pull request as queries/pr.graphql returns it.
pub fn pr(number: i64, requested: &[&str], reviews: Vec<Value>) -> Value {
    let requests: Vec<Value> = requested
        .iter()
        .map(|l| json!({"requestedReviewer": {"login": l}}))
        .collect();
    json!({"url": format!("https://github.com/example-org/example-repo/pull/{number}"), "number": number,
           "state": "OPEN", "author": {"login": "alice"}, "headRefOid": format!("{:0>40}", number),
           "baseRefName": "main", "repository": {"nameWithOwner": "example-org/example-repo"},
           "reviewRequests": {"totalCount": requests.len(), "nodes": requests},
           "reviews": {"totalCount": reviews.len(), "nodes": reviews},
           "reviewThreads": {"totalCount": 0, "nodes": []}})
}
