//! whooper review: reviews one work item with a run of the agent the config's [jobs] review names, and
//! prints the run's directory, <state>/runs/<run-id>/, as soon as it makes it:
//!
//!   whooper review <work-item.json> [--as-of <ISO 8601 time with a zone>]
//!
//! The PR's repo must have a table in the config. Before the run starts, the review fetches with the
//! user's login everything the run reads: the head and merge-base commits, checked out as the worktrees
//! head/ and base/ in the run directory, and the PR's conversation as github.json, which with --as-of
//! holds only what people could read before that time, for replaying a past review. The diff between
//! the two commits stays as diff.patch after the worktrees go, a plain patch whatever the user's git
//! config says. Then the repo's setup script, if the config names one, prepares anything else the run
//! needs, and the test-gaps step saves test_gaps.json. The run itself has no GitHub login, and its agent
//! denies the Slack writes, so it can post nothing. Its findings.json must fit schemas/findings.json,
//! and a run that passes [review] timeout_minutes is stopped. Once the run ends: its overview is checked
//! (overview.json, or overview.invalid.json with why), and the connections, examples, and mutation steps
//! run. Whatever each step finds, and even if it fails, the review goes on as it would without its file.
//! A review that ends with findings that fit marks its <state>/prs record done, or stale when the PR's
//! head moved during the review, which also puts the PR on the recheck list.
//!
//! While it runs, the review holds the PR's lock, <state>/locks/<owner>-<repo>-<pr>/, with its process
//! ID in pid and the run ID in run_id. On exit, a run that did not finish first counts as a failure in
//! its record and leaves why as failure.txt, with the cause in failure.json; then the repo's teardown
//! script runs, and the worktrees and the lock go away.

use std::collections::BTreeMap;
use std::fs;
use std::os::fd::AsFd;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use serde_json::{Value, json};

use crate::config::{self, Whooper};
use crate::fsx::{self, Exit, Object, Result, read_object, str_of};
use crate::proc::{self, Ended};
use crate::status::failure_cause;
use crate::work::{mark_new_commits, write_record};
use crate::{agent, lock, prompt};

/// What failure.txt says when the review stops at a command that failed, without its own reason.
const UNEXPECTED: &str =
    "the review stopped at an unexpected error; see whooper-review's error output";

/// The environment every program a review runs gets: no GitHub login, as gh sends the token "disabled"
/// and git's credential helper (gh) hands git the same, and temporary files in the run directory.
fn sealed(run: &Path) -> Vec<(String, String)> {
    vec![
        ("GH_TOKEN".into(), "disabled".into()),
        ("GIT_SSH_COMMAND".into(), "false".into()),
        ("GIT_TERMINAL_PROMPT".into(), "0".into()),
        ("TMPDIR".into(), run.join("tmp").display().to_string()),
    ]
}

/// This process's error output, for a child's output.
pub fn to_stderr() -> Stdio {
    std::io::stderr()
        .as_fd()
        .try_clone_to_owned()
        .map(Stdio::from)
        .unwrap_or_else(|_| Stdio::inherit())
}

/// The arguments that print diff.patch, a plain patch as git's defaults print it whatever the user's
/// git config says: no external diff tool, textconv filter, or color; a/ and b/ before the paths and a
/// submodule's commit lines; hunks of 3 unchanged lines around each change and a new one wherever more
/// than 6 separate two changes, by myers with the indent heuristic, and a blank context line printed
/// with its leading space; renames among at most 1,000 files but not copies, and no order file; quoted
/// unusual paths, each file up to 512 MiB compared as text, and only the repo's .gitattributes; each
/// changed submodule shown; and object names of 12 digits. It runs in the head worktree, so git applies
/// the .gitattributes it finds there.
const PLAIN_DIFF: [&str; 24] = [
    "-c",
    "diff.suppressBlankEmpty=false",
    "-c",
    "core.quotePath=true",
    "-c",
    "core.bigFileThreshold=512m",
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "diff.ignoreSubmodules=none",
    "diff",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--submodule=short",
    "--unified=3",
    "--inter-hunk-context=0",
    "--diff-algorithm=myers",
    "--indent-heuristic",
    "--find-renames",
    "-l1000",
    "-O/dev/null",
];

/// A git command in folder.
fn git(folder: &Path) -> Command {
    let mut command = Command::new("git");
    command.arg("-C").arg(folder);
    command
}

/// Runs command, with its error output passed on, and returns what it printed, or the unexpected
/// failure with its exit code.
fn checked(command: &mut Command) -> Result<String> {
    command.stdin(Stdio::null()).stderr(to_stderr());
    let out = command.output()?;
    proc::check()?;
    if !out.status.success() {
        return Err(Exit::new(proc::code(out.status), ""));
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// A content hash of every skill folder's files by relative path and content, so a finding traces to the
/// skill text behind it. The same files give the same value wherever they sit.
pub fn skill_hashes(skills: &Path) -> Result<Object> {
    use sha2::{Digest, Sha256};
    fn walk(folder: &Path, top: &Path, hash: &mut Sha256) -> std::io::Result<()> {
        let mut files = vec![];
        let mut folders = vec![];
        for entry in fsx::entries(folder) {
            let meta = fs::symlink_metadata(&entry)?;
            if meta.is_dir() {
                folders.push(entry);
            } else if meta.file_type().is_symlink() && entry.is_dir() {
                // os.walk lists a link to a folder among the folders and does not follow it.
            } else {
                files.push(entry);
            }
        }
        for file in files {
            let relative = file
                .strip_prefix(top)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            hash.update(relative.as_bytes());
            hash.update(b"\0");
            hash.update(hex(&Sha256::digest(fs::read(&file)?)).as_bytes());
        }
        for sub in folders {
            walk(&sub, top, hash)?;
        }
        Ok(())
    }
    let mut hashes = Object::new();
    for skill in fsx::entries(skills) {
        if !skill.is_dir() {
            continue;
        }
        let mut hash = Sha256::new();
        walk(&skill, &skill, &mut hash)?;
        hashes.insert(fsx::name_of(&skill), json!(hex(&hash.finalize())));
    }
    Ok(hashes)
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn author_login(actor: &Value) -> Value {
    match actor["login"].as_str() {
        Some(login) if actor.is_object() => json!(login),
        _ => json!("ghost"),
    }
}

/// A review run's github.json: the PR's text, its submitted reviews with their comments, and its
/// conversation comments. With as_of, a replay sees only what people could read before that time:
/// reviews submitted before it, comments created and last edited before it, and the body as it read
/// then. A review's comments go with their review, because GitHub dates a comment from when it was
/// drafted in a pending review. GitHub keeps no title history, so the title is today's.
pub fn conversation(pr: &Value, as_of: Option<&str>) -> std::result::Result<Value, String> {
    let cutoff = as_of.and_then(fsx::parse_time);
    let readable = |time: &Value| match (cutoff, time.as_str().and_then(fsx::parse_time)) {
        (None, _) => true,
        (Some(cut), Some(t)) => t < cut,
        (Some(_), None) => false,
    };
    let nodes = crate::poll::nodes;
    let mut body = pr["body"].clone();
    if cutoff.is_some() && pr["userContentEdits"]["totalCount"].as_u64().unwrap_or(0) > 0 {
        let versions: Vec<Value> = nodes(&pr["userContentEdits"])?
            .into_iter()
            .filter(|v| readable(&v["editedAt"]))
            .collect();
        body = versions
            .iter()
            .max_by_key(|v| v["editedAt"].as_str().and_then(fsx::parse_time))
            .map(|v| v["diff"].clone())
            .unwrap_or(Value::Null);
    }
    let mut reviews = vec![];
    for r in nodes(&pr["reviews"])? {
        if r["state"] == "PENDING" || !readable(&r["submittedAt"]) {
            continue;
        }
        let mut comments = vec![];
        for c in nodes(&r["comments"])? {
            let edited = if c["lastEditedAt"].is_null() {
                &c["createdAt"]
            } else {
                &c["lastEditedAt"]
            };
            if !readable(edited) {
                continue;
            }
            comments.push(json!({
                "id": c["databaseId"],
                "reply_to": if c["replyTo"].is_null() { Value::Null } else { c["replyTo"]["databaseId"].clone() },
                "author": author_login(&c["author"]),
                "created_at": c["createdAt"],
                "path": c["path"],
                "line": c["originalLine"],
                "start_line": c["originalStartLine"],
                "commit": c["originalCommit"]["oid"],
                "diff_hunk": c["diffHunk"],
                "body": c["body"],
            }));
        }
        reviews.push(json!({
            "author": author_login(&r["author"]),
            "state": r["state"],
            "submitted_at": r["submittedAt"],
            "commit": r["commit"]["oid"],
            "body": r["body"],
            "comments": comments,
        }));
    }
    let mut comments = vec![];
    for c in nodes(&pr["comments"])? {
        let edited = if c["lastEditedAt"].is_null() {
            &c["createdAt"]
        } else {
            &c["lastEditedAt"]
        };
        if readable(edited) {
            comments.push(json!({"author": author_login(&c["author"]), "created_at": c["createdAt"], "body": c["body"]}));
        }
    }
    Ok(json!({
        "pr": {"url": pr["url"], "number": pr["number"], "title": pr["title"], "author": author_login(&pr["author"]), "body": body},
        "as_of": match cutoff {
            Some(t) => json!(python_iso(as_of.unwrap(), t)),
            None => Value::Null,
        },
        "reviews": reviews,
        "comments": comments,
    }))
}

/// as_of as Python's isoformat writes the time it parsed: with its own offset, and +00:00 for Z.
fn python_iso(text: &str, _: jiff::Timestamp) -> String {
    if let Some(stripped) = text.strip_suffix('Z').or_else(|| text.strip_suffix('z')) {
        return format!("{stripped}+00:00");
    }
    text.to_string()
}

/// Moves an overview the run wrote inside findings.json, which the schema has no room for, to
/// overview.json, unless the run wrote that file too, and then moves an overview.json that cannot be
/// read, is not JSON, or breaks a rule of prompts/review.md's overview aside to overview.invalid.json,
/// with why there and on the error output.
fn check_overview(run: &Path) -> Result<()> {
    let path = run.join("findings.json");
    if let Some(mut findings) = read_object(&path)
        && let Some(stray) = findings.remove("overview")
    {
        // findings.json keeps the time the run wrote it, which whooper status gives as the review's end.
        let written = fs::metadata(&path)?.modified()?;
        fsx::write_file(&path, &fsx::pretty(&Value::Object(findings)), false)?;
        fs::File::options()
            .write(true)
            .open(&path)?
            .set_modified(written)?;
        match fsx::write_file(&run.join("overview.json"), &fsx::pretty(&stray), true) {
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => eprintln!(
                "whooper-review: discarded the overview in the run's findings.json, since the run also wrote overview.json"
            ),
            Err(e) => return Err(e.into()),
            Ok(()) => eprintln!(
                "whooper-review: moved the overview in the run's findings.json to overview.json"
            ),
        }
    }
    let patch = fsx::read_text(&run.join("diff.patch")).unwrap_or_default();
    let (overview, dropped) = crate::diff::read_overview(run, &patch);
    if let Some(reason) = dropped {
        fsx::write_file(
            &run.join("overview.invalid.json"),
            &fsx::pretty(&json!({"reason": reason, "overview": overview})),
            false,
        )?;
        fs::remove_file(run.join("overview.json"))?;
        eprintln!("whooper-review: dropped the run's overview: {reason}");
    }
    Ok(())
}

/// Reads KEY=value lines a setup script appended to WHOOPER_SETUP_ENV, as a shell's source would set
/// them: a value in single or double quotes loses its quotes.
fn setup_env(text: &str) -> Vec<(String, String)> {
    let mut vars = vec![];
    for line in text.lines() {
        let line = line.trim();
        let line = line.strip_prefix("export ").unwrap_or(line);
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let value = value.trim();
        let unquoted = if value.len() >= 2
            && ((value.starts_with('"') && value.ends_with('"'))
                || (value.starts_with('\'') && value.ends_with('\'')))
        {
            &value[1..value.len() - 1]
        } else {
            value
        };
        vars.push((key.trim().to_string(), unquoted.to_string()));
    }
    vars
}

/// The review's progress, which its cleanup reads.
struct Review {
    whooper: Whooper,
    item: Object,
    item_path: PathBuf,
    repo: config::Repo,
    run_id: String,
    run: PathBuf,
    lock: PathBuf,
    work: tempfile::TempDir,
    /// Whether the run directory exists, so a stop saves its reason there.
    has_run: bool,
    /// The step the review is at, for failure.json's cause, and the file holding its error output.
    step: String,
    step_log: Option<PathBuf>,
    /// The environment of the setup and teardown scripts, the steps, and the run, once set.
    env: Vec<(String, String)>,
}

/// A die: the reason the review stopped, which failure.txt keeps once the run directory exists.
fn die(message: impl Into<String>) -> Exit {
    Exit::new(1, format!("whooper-review: {}", message.into()))
}

/// whooper review.
pub fn review(item_path: &Path, as_of: Option<&str>) -> Result<()> {
    // git takes an external diff tool from GIT_EXTERNAL_DIFF and the context lines from GIT_DIFF_OPTS,
    // whatever its config says, so the review and everything it starts go without them.
    unsafe {
        std::env::remove_var("GIT_EXTERNAL_DIFF");
        std::env::remove_var("GIT_DIFF_OPTS");
    }
    let whooper = Whooper::load()?;
    let usage = || die("usage: whooper review <work-item.json> [--as-of <time>]");
    if !item_path.is_file() {
        return Err(usage());
    }
    if let Some(as_of) = as_of
        && fsx::parse_time(as_of).is_none()
    {
        return Err(die(
            "--as-of takes an ISO 8601 time with a zone, such as 2031-01-02T03:04:05Z",
        ));
    }
    let Some(item) = read_object(item_path) else {
        return Err(usage());
    };
    let repo_name = str_of(&item, "repo").unwrap_or("").to_string();
    let repo = whooper.repo(&repo_name)?;
    let pr = item["pr"].as_i64().filter(|&n| n > 0);
    let head = str_of(&item, "head_sha").unwrap_or("");
    let full_sha = head.len() == 40 && head.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'));
    let (Some(pr), true) = (pr, full_sha) else {
        return Err(die(format!(
            "{} needs a PR number and a full head_sha",
            item_path.display()
        )));
    };
    let base_ref = str_of(&item, "base_ref").unwrap_or("").to_string();
    // base_ref names a branch, or for a replay the commit the base branch pointed at then.
    let valid = Command::new("git")
        .args(["check-ref-format", "--branch", &base_ref])
        .stdout(Stdio::null())
        .status()?
        .success();
    if !valid {
        return Err(die(format!(
            "base_ref {base_ref} is not a branch or commit"
        )));
    }
    let run_id = format!("{}-{pr}", jiff::Timestamp::now().strftime("%Y%m%dT%H%M%SZ"));
    let state = whooper.state.clone();
    let run = state.join("runs").join(&run_id);
    let lock_dir = state
        .join("locks")
        .join(format!("{}-{pr}", repo_name.replace('/', "-")));
    let work = tempfile::tempdir()?;
    match lock::take(&state, &lock_dir, "whooper-review", |_| Ok(()))? {
        lock::Taken::Mine => {}
        lock::Taken::HeldBy(pid) => {
            return Err(die(format!(
                "process {pid} is already reviewing {repo_name}#{pr}"
            )));
        }
    }
    let mut review = Review {
        whooper,
        item,
        item_path: item_path.to_path_buf(),
        repo,
        run_id,
        run,
        lock: lock_dir,
        work,
        has_run: false,
        step: String::new(),
        step_log: None,
        env: vec![],
    };
    let result = review.go(as_of);
    review.cleanup(result)
}

impl Review {
    fn save_failure(&self, reason: &str) {
        let _ = fs::write(self.run.join("failure.txt"), format!("{reason}\n"));
        let text = self
            .step_log
            .as_ref()
            .and_then(|p| fsx::read_text(p))
            .unwrap_or_default();
        let _ = fsx::write_file(
            &self.run.join("failure.json"),
            &failure_cause(&self.step, &text).to_string(),
            false,
        );
    }

    /// Runs a program in a session of its own with the review's environment, and waits for it; a stop
    /// stops it and the review.
    fn run_script(&self, mut command: Command) -> Result<i32> {
        command.envs(self.env.iter().map(|(k, v)| (k, v)));
        command.stdin(Stdio::null());
        let mut child = proc::spawn(&mut command)?;
        match proc::wait(&mut child, None) {
            Ended::Exited(code) => Ok(code),
            _ => Err(proc::stopped()),
        }
    }

    /// Runs whooper step <name> on the run, which saves one file for the review screen and stops at a
    /// time limit of its own, in a session of its own, with the run's environment, since a step may run
    /// the PR's tests. Whatever it finds, and even if it fails, the review goes on.
    fn run_step(&self, name: &str, what: &str) -> Result<()> {
        let mut command = Command::new(crate::fsx::me()?);
        command
            .args(["step", name])
            .arg(&self.run)
            .envs(sealed(&self.run))
            .stdout(to_stderr());
        if self.run_script(command)? != 0 {
            eprintln!("whooper-review: the {name} step failed, so the review has no {what}");
        }
        Ok(())
    }

    fn go(&mut self, as_of: Option<&str>) -> Result<()> {
        let (run, work) = (self.run.clone(), self.work.path().to_path_buf());
        fs::write(self.lock.join("run_id"), format!("{}\n", self.run_id))?;
        fs::create_dir_all(run.join("tmp"))?;
        self.has_run = true;
        // whooper round reads why a review that fails stopped from its run directory.
        println!("{}", run.display());
        let state = self.whooper.state.clone();
        write_record(&state, &self.item, "running")?;
        proc::check()?;

        // Checked only now, so a review of a repo without a clone leaves its run directory and failure.
        self.step = "clone".into();
        let core = self.repo.clone.clone();
        let repo = str_of(&self.item, "repo").unwrap_or("").to_string();
        let pr = self.item["pr"].clone();
        let head_sha = str_of(&self.item, "head_sha").unwrap_or("").to_string();
        let base_ref = str_of(&self.item, "base_ref").unwrap_or("").to_string();
        if !core.join(".git").is_dir() {
            return Err(die(format!(
                "no clone of {repo} at {}; see README.md",
                core.display()
            )));
        }
        // The fetch is the review's first use of the user's GitHub login, so its error output shows when
        // gh is signed out.
        self.step = "fetch".into();
        let log = work.join("fetch.log");
        self.step_log = Some(log.clone());
        let fetched = git(&core)
            .args(["fetch", "--quiet", "--no-write-fetch-head", "origin"])
            .arg(format!("{head_sha}:refs/whooper/{}/head", self.run_id))
            .arg(format!("{base_ref}:refs/whooper/{}/base", self.run_id))
            .stdin(Stdio::null())
            .stderr(fs::File::create(&log)?)
            .status()?;
        eprint!("{}", fsx::read_text(&log).unwrap_or_default());
        proc::check()?;
        if !fetched.success() {
            return Err(Exit::new(proc::code(fetched), ""));
        }
        self.step = "prepare".into();
        self.step_log = None;
        let merge_base = checked(git(&core).args([
            "merge-base",
            &format!("refs/whooper/{}/head", self.run_id),
            &format!("refs/whooper/{}/base", self.run_id),
        ]))?
        .trim()
        .to_string();
        // Checking out both trees fetches every file in them, so the run can read both without a login.
        checked(
            git(&core)
                .args(["worktree", "add", "--quiet", "--detach"])
                .arg(run.join("head"))
                .arg(&head_sha),
        )?;
        checked(
            git(&core)
                .args(["worktree", "add", "--quiet", "--detach"])
                .arg(run.join("base"))
                .arg(&merge_base),
        )?;
        let patch = checked(git(&run.join("head")).args(PLAIN_DIFF).args([
            "--abbrev=12",
            &merge_base,
            "HEAD",
        ]))?;
        fs::write(run.join("diff.patch"), patch)?;
        let agent_name = self.whooper.agent("review");
        let mut ran = self.item.clone();
        ran.insert("run_id".into(), json!(self.run_id));
        ran.insert("merge_base".into(), json!(merge_base));
        ran.insert("as_of".into(), json!(as_of));
        ran.insert("agent".into(), json!(agent_name));
        let home = PathBuf::from(std::env::var_os("HOME").unwrap_or_default());
        ran.insert(
            "skills".into(),
            Value::Object(skill_hashes(&home.join(".claude/skills"))?),
        );
        fs::write(
            run.join("run.json"),
            fsx::pretty(&Value::Object(ran)) + "\n",
        )?;

        let (owner, name) = repo.split_once('/').unwrap_or((&repo, ""));
        let fetched = Command::new("gh")
            .args([
                "api",
                "graphql",
                "-F",
                &format!(
                    "query=@{}",
                    config::root()
                        .join("queries/conversation.graphql")
                        .display()
                ),
                "-f",
                &format!("owner={owner}"),
                "-f",
                &format!("name={name}"),
                "-F",
                &format!("number={pr}"),
            ])
            .stdin(Stdio::null())
            .stderr(to_stderr())
            .output()?;
        proc::check()?;
        let github = fetched
            .status
            .success()
            .then(|| serde_json::from_slice::<Value>(&fetched.stdout).ok())
            .flatten()
            .and_then(|result| {
                match conversation(&result["data"]["repository"]["pullRequest"], as_of) {
                    Ok(c) => Some(c),
                    Err(e) => {
                        eprintln!("whooper: {e}");
                        None
                    }
                }
            });
        let github = match github {
            Some(conversation) => {
                fsx::write_file(&run.join("github.json"), &fsx::pretty(&conversation), false)?;
                match as_of {
                    Some(t) => format!(
                        "{}, which holds only what existed before {t}",
                        run.join("github.json").display()
                    ),
                    None => run.join("github.json").display().to_string(),
                }
            }
            None => {
                eprintln!("whooper-review: the GitHub fetch failed, so the run has no github.json");
                "missing, because the GitHub fetch failed".into()
            }
        };

        // The setup script gets the run's paths and changed files, may append KEY=value lines for the
        // run's environment to WHOOPER_SETUP_ENV, and prints what the run can use for the prompt. The list
        // of changed files takes those of diff.patch's settings that change which files git lists.
        let changed = checked(
            git(&core)
                .args([
                    "-c",
                    "core.quotePath=true",
                    "-c",
                    "diff.ignoreSubmodules=none",
                    "diff",
                    "--name-only",
                    "--find-renames",
                    "-l1000",
                    "-O/dev/null",
                ])
                .args([&merge_base, &head_sha]),
        )?;
        fs::write(work.join("changed.txt"), changed)?;
        fs::write(work.join("setup.env"), "")?;
        let shown = |p: PathBuf| p.display().to_string();
        self.env = vec![
            ("WHOOPER_RUN_ID".into(), self.run_id.clone()),
            ("WHOOPER_RUN".into(), shown(run.clone())),
            ("WHOOPER_HEAD".into(), shown(run.join("head"))),
            ("WHOOPER_BASE".into(), shown(run.join("base"))),
            ("WHOOPER_CHANGED".into(), shown(work.join("changed.txt"))),
            ("WHOOPER_SETUP_ENV".into(), shown(work.join("setup.env"))),
        ];
        let mut setup = "none".to_string();
        if let Some(script) = self.repo.setup.clone() {
            // What the script leaves running, such as a database the run uses, stays until the teardown
            // script removes it. When it fails, its last line ends the reason.
            self.step = "setup".into();
            let log = work.join("setup.log");
            self.step_log = Some(log.clone());
            let mut command = Command::new(&script);
            command
                .stdout(fs::File::create(work.join("setup.out"))?)
                .stderr(fs::File::create(&log)?);
            let code = self.run_script(command)?;
            setup = fsx::read_text(&work.join("setup.out"))
                .unwrap_or_default()
                .trim_end_matches('\n')
                .to_string();
            let text = fsx::read_text(&log).unwrap_or_default();
            eprint!("{text}");
            if code != 0 {
                let last = text.lines().rev().find(|l| !l.trim().is_empty());
                return Err(die(format!(
                    "{} failed{}",
                    script.display(),
                    last.map(|l| format!(": {l}")).unwrap_or_default()
                )));
            }
            let extra = setup_env(&fsx::read_text(&work.join("setup.env")).unwrap_or_default());
            self.env.extend(extra);
        }
        self.step = "review".into();
        self.step_log = None;

        // Before the run starts, while the head's worktree holds the head as committed. The run may change
        // test_gaps.json, so a copy outside the run directory replaces it once the run ends.
        self.run_step("test-gaps", "test_gaps.json")?;
        let mut gaps = String::new();
        if run.join("test_gaps.json").is_file()
            && fs::copy(run.join("test_gaps.json"), work.join("test_gaps.json")).is_ok()
        {
            gaps = format!(
                "\n- test_gaps.json: {}, the changed Go, JavaScript, and TypeScript lines no test ran, which Whooper measured before this run started",
                run.join("test_gaps.json").display()
            );
        }
        let untagged = if str_of(&self.item, "source") == Some("slack_ask")
            && self.item["slack"]["tagged_user"] == false
        {
            "\n- The work came from an untagged team ask: decide first whether the PR is small or mechanical."
        } else {
            ""
        };
        let text = format!(
            "{}\n## This run\n\n- Pull request: https://github.com/{repo}/pull/{pr}, base {base_ref}. Why it is {}'s work: {}{untagged}\n- Head: {head_sha}, checked out in {}, the working directory.\n- Merge base: {merge_base}, checked out in {}.\n- Run directory: {}. Write log.md and overview.json there.\n- github.json: {github}\n- Repository setup: {setup}{gaps}\n",
            prompt::review(&self.whooper, &repo, &agent_name)?,
            self.whooper.login,
            str_of(&self.item, "reason").unwrap_or(""),
            run.join("head").display(),
            run.join("base").display(),
            run.display(),
        );
        fs::write(run.join("prompt.md"), text)?;

        let minutes = self.whooper.limits.review;
        let env: Vec<(String, String)> = sealed(&run).into_iter().chain(self.env.clone()).collect();
        let ended = agent::run(
            &self.whooper,
            &agent_name,
            &agent::Job::Review {
                prompt: &run.join("prompt.md"),
                run: &run,
            },
            Duration::from_secs(minutes as u64 * 60),
            Some(&run.join("head")),
            &env,
        )?;
        if ended == Ended::Stopped {
            return Err(proc::stopped());
        }
        // Only Whooper's own steps fill test_gaps.json and examples.json: the copy from before the run
        // replaces the first, and the examples the run chose move to examples.request.json.
        let _ = remove_any(&run.join("test_gaps.json"));
        if work.join("test_gaps.json").exists() {
            let _ =
                fs::rename(work.join("test_gaps.json"), run.join("test_gaps.json")).or_else(|_| {
                    fs::copy(work.join("test_gaps.json"), run.join("test_gaps.json")).map(|_| ())
                });
        }
        if run.join("examples.json").exists()
            && fs::rename(run.join("examples.json"), run.join("examples.request.json")).is_err()
        {
            let _ = remove_any(&run.join("examples.json"));
        }
        // Checked as soon as the run ends, even one past its time limit, so an overview in findings.json
        // cannot fail the findings' check below.
        if let Err(e) = check_overview(&run) {
            eprintln!("whooper-review: the overview check failed: {}", e.message);
        }
        proc::check()?;
        self.run_step("connections", "connections.json")?;
        self.run_step("examples", "examples.json")?;
        self.run_step("mutation", "survived or caught lines in test_gaps.json")?;

        self.step = "time-limit".into();
        if ended == Ended::TimedOut {
            return Err(die(format!(
                "the review run passed its {minutes}-minute limit; see {}/stderr.log and the transcript in {}",
                run.display(),
                run.display()
            )));
        }
        let Ended::Exited(code) = ended else {
            unreachable!()
        };
        self.step = "findings".into();
        let findings = run.join("findings.json");
        if fs::metadata(&findings).map(|m| m.len()).unwrap_or(0) == 0 {
            return Err(die(format!(
                "the review run exited {code} without findings; see {}/stderr.log and the transcript in {}",
                run.display(),
                run.display()
            )));
        }
        let schema_path = config::root().join("schemas/findings.json");
        let fits = match fsx::read_json(&findings) {
            Some(value) => {
                crate::schema::check(&value, &crate::status::findings_schema()?, "the value")
            }
            None => Err("it is not JSON".to_string()),
        };
        if let Err(e) = fits {
            eprintln!(
                "whooper: {} does not fit {}: {e}",
                findings.display(),
                schema_path.display()
            );
            fs::rename(&findings, run.join("findings.invalid.json"))?;
            return Err(die(
                "the review run's findings.json does not fit the schema",
            ));
        }
        if !run.join("log.md").is_file() {
            eprintln!("whooper-review: the run wrote no log.md");
        }
        // Without the PR's head now, as when gh fails, the record says done at the reviewed head.
        let head_now = Command::new("gh")
            .args([
                "api",
                &format!("repos/{repo}/pulls/{pr}"),
                "--jq",
                ".head.sha",
            ])
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .unwrap_or_default();
        proc::check()?;
        let item = read_object(&run.join("run.json")).unwrap_or_else(|| self.item.clone());
        write_record(&state, &item, "done")?;
        if !head_now.is_empty() && head_now != head_sha {
            mark_new_commits(&state, &item, &head_now)?;
            eprintln!(
                "whooper-review: the PR's head is now {head_now}; the next round reviews the new commits"
            );
        }
        Ok(())
    }

    /// Cleans up however the review ended: a review that did not finish saves why and counts as a
    /// failure first, so that holds even when the cleanup is cut short; then the teardown script runs,
    /// and the worktrees, the work folder, and the lock go.
    fn cleanup(self, result: Result<()>) -> Result<()> {
        let result = match result {
            Err(e) if proc::stopping() && e.code != 130 => Err(proc::stopped()),
            other => other,
        };
        if let Err(e) = &result {
            if !e.message.is_empty() {
                eprintln!("{}", e.message);
            }
            let reason = if e.code == 130 {
                "the review was interrupted".to_string()
            } else if let Some(reason) = e.message.strip_prefix("whooper-review: ") {
                reason.to_string()
            } else {
                UNEXPECTED.to_string()
            };
            let mut this = self;
            if e.code == 130 {
                this.step = "interrupted".into();
                this.step_log = None;
            }
            if this.has_run && !this.run.join("failure.txt").exists() {
                this.save_failure(&reason);
            }
            let _ = write_record(&this.whooper.state, &this.item, "failed");
            this.finish();
            return Err(Exit::new(e.code, ""));
        }
        self.finish();
        result
    }

    fn finish(self) {
        // From now on a signal stops only the teardown script, so it cannot cut the cleanup short.
        proc::clear();
        if let Some(teardown) = &self.repo.teardown
            && !self.env.is_empty()
        {
            let mut command = Command::new(teardown);
            command.stdout(to_stderr());
            match self.run_script(command) {
                Ok(0) => {}
                _ => eprintln!("whooper-review: {} failed", teardown.display()),
            }
            proc::clear();
        }
        let core = &self.repo.clone;
        for tree in ["head", "base"] {
            if self.run.join(tree).is_dir() {
                let _ = git(core)
                    .args(["worktree", "remove", "--force"])
                    .arg(self.run.join(tree))
                    .stdin(Stdio::null())
                    .status();
            }
            let _ = git(core)
                .args([
                    "update-ref",
                    "-d",
                    &format!("refs/whooper/{}/{tree}", self.run_id),
                ])
                .stdin(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        let _ = fs::remove_dir_all(self.run.join("tmp"));
        let _ = &self.item_path;
        lock::release(&self.lock);
        drop(self.work);
    }
}

fn remove_any(path: &Path) -> std::io::Result<()> {
    match fs::symlink_metadata(path) {
        Ok(m) if m.is_dir() => fs::remove_dir_all(path),
        Ok(_) => fs::remove_file(path),
        Err(e) => Err(e),
    }
}

/// A step's function, which saves its file within a time limit in seconds.
type Step = fn(&Path, f64) -> Result<()>;

/// The names of the review's steps that whooper step runs, each with the function that saves its file
/// and its time limit in seconds.
pub fn steps() -> BTreeMap<&'static str, (Step, f64)> {
    BTreeMap::from([
        (
            "connections",
            (
                crate::connections::connections_within as Step,
                crate::connections::SECONDS,
            ),
        ),
        (
            "test-gaps",
            (
                crate::gaps::test_gaps_within as Step,
                crate::gaps::TEST_GAPS_SECONDS,
            ),
        ),
        (
            "examples",
            (
                crate::examples::examples_within as Step,
                crate::examples::SECONDS,
            ),
        ),
        (
            "mutation",
            (
                crate::gaps::mutation_within as Step,
                crate::gaps::MUTATION_SECONDS,
            ),
        ),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_replay_sees_only_what_was_readable_before_its_time() {
        let pr = json!({"url": "u", "number": 1, "title": "t", "author": null, "body": "now",
            "userContentEdits": {"totalCount": 2, "nodes": [{"editedAt": "2031-01-01T00:00:00Z", "diff": "first"},
                                                             {"editedAt": "2031-01-03T00:00:00Z", "diff": "later"}]},
            "reviews": {"totalCount": 2, "nodes": [
                {"author": {"login": "bob"}, "state": "COMMENTED", "submittedAt": "2031-01-01T00:00:00Z", "commit": {"oid": "c"}, "body": "b",
                 "comments": {"totalCount": 0, "nodes": []}},
                {"author": {"login": "bob"}, "state": "APPROVED", "submittedAt": "2031-01-04T00:00:00Z", "commit": {"oid": "c"}, "body": "",
                 "comments": {"totalCount": 0, "nodes": []}}]},
            "comments": {"totalCount": 0, "nodes": []}});
        let replay = conversation(&pr, Some("2031-01-02T00:00:00Z")).unwrap();
        assert_eq!(replay["pr"]["body"], "first");
        assert_eq!(replay["pr"]["author"], "ghost");
        assert_eq!(replay["as_of"], "2031-01-02T00:00:00+00:00");
        assert_eq!(replay["reviews"].as_array().unwrap().len(), 1);
        assert_eq!(
            conversation(&pr, None).unwrap()["reviews"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
    }

    #[test]
    fn skill_hashes_change_only_with_a_skills_files() {
        let one = tempfile::tempdir().unwrap();
        let two = tempfile::tempdir().unwrap();
        for dir in [one.path(), two.path()] {
            fs::create_dir_all(dir.join("a/sub")).unwrap();
            fs::write(dir.join("a/SKILL.md"), "x").unwrap();
            fs::write(dir.join("a/sub/more.md"), "y").unwrap();
            fs::create_dir_all(dir.join("b")).unwrap();
            fs::write(dir.join("b/SKILL.md"), "z").unwrap();
        }
        assert_eq!(
            skill_hashes(one.path()).unwrap(),
            skill_hashes(two.path()).unwrap()
        );
        fs::write(two.path().join("a/sub/more.md"), "changed").unwrap();
        let (a, b) = (
            skill_hashes(one.path()).unwrap(),
            skill_hashes(two.path()).unwrap(),
        );
        assert_ne!(a["a"], b["a"]);
        assert_eq!(a["b"], b["b"]);
    }

    #[test]
    fn setup_env_reads_key_value_lines() {
        assert_eq!(
            setup_env("A=1\n# c\nexport B='two words'\n\nC=\"x\"\n"),
            [
                ("A".into(), "1".into()),
                ("B".into(), "two words".into()),
                ("C".into(), "x".into())
            ]
        );
    }

    const T: &str = "2031-07-19T21:35:04Z";

    /// A review comment as queries/conversation.graphql returns it.
    fn gql_comment(author: &str, created: &str, body: &str, edited: Option<&str>) -> Value {
        json!({"databaseId": 1, "author": {"login": author}, "createdAt": created, "lastEditedAt": edited,
               "path": "services/orders/internal/api/settings.go", "originalLine": 88, "originalStartLine": null,
               "diffHunk": "@@ -80,6 +80,9 @@", "body": body, "replyTo": null, "originalCommit": {"oid": "head1"}})
    }

    fn connection(nodes: Vec<Value>) -> Value {
        json!({"totalCount": nodes.len(), "nodes": nodes})
    }

    fn gql_review(author: &str, state: &str, at: Option<&str>, comments: Vec<Value>) -> Value {
        json!({"author": {"login": author}, "state": state, "submittedAt": at, "body": "",
               "commit": {"oid": "head1"}, "comments": connection(comments)})
    }

    /// A pull request as queries/conversation.graphql returns it; comments are (author, createdAt,
    /// lastEditedAt, body), edits (editedAt, body of that version), newest first.
    fn gql_pr(
        reviews: Vec<Value>,
        comments: &[(&str, &str, Option<&str>, &str)],
        body: &str,
        edits: &[(&str, &str)],
    ) -> Value {
        json!({"url": "https://github.com/example-org/example-repo/pull/42", "number": 42,
               "title": "feat(app): add a settings page", "body": body, "author": {"login": "dave"},
               "userContentEdits": connection(edits.iter().map(|(t, d)| json!({"editedAt": t, "diff": d})).collect()),
               "reviews": connection(reviews),
               "comments": connection(comments.iter().map(|(a, c, e, b)|
                   json!({"author": {"login": a}, "createdAt": c, "lastEditedAt": e, "body": b})).collect())})
    }

    type Summary = (Vec<(String, String, Vec<String>)>, Vec<String>);

    fn summary(github: &Value) -> Summary {
        let s = |v: &Value| v.as_str().unwrap().to_string();
        (
            github["reviews"]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| {
                    let bodies = r["comments"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|c| s(&c["body"]))
                        .collect();
                    (s(&r["author"]), s(&r["state"]), bodies)
                })
                .collect(),
            github["comments"]
                .as_array()
                .unwrap()
                .iter()
                .map(|c| s(&c["body"]))
                .collect(),
        )
    }

    fn reviewed(author: &str, state: &str, bodies: &[&str]) -> (String, String, Vec<String>) {
        (
            author.into(),
            state.into(),
            bodies.iter().map(|b| b.to_string()).collect(),
        )
    }

    #[test]
    fn without_a_cutoff_the_conversation_keeps_every_submitted_review_but_not_a_pending_one() {
        let github = conversation(
            &gql_pr(
                vec![
                    gql_review(
                        "alice",
                        "APPROVED",
                        Some("2031-07-16T02:43:19Z"),
                        vec![gql_comment(
                            "alice",
                            "2031-07-16T02:38:42Z",
                            "rename this",
                            None,
                        )],
                    ),
                    gql_review(
                        "sam-example",
                        "PENDING",
                        None,
                        vec![gql_comment(
                            "sam-example",
                            "2031-07-19T21:09:55Z",
                            "draft",
                            None,
                        )],
                    ),
                ],
                &[],
                "Compares rows.",
                &[],
            ),
            None,
        )
        .unwrap();
        assert_eq!(
            summary(&github),
            (
                vec![reviewed("alice", "APPROVED", &["rename this"])],
                vec![]
            )
        );
        assert_eq!(github["pr"]["body"], "Compares rows.");
        assert!(github["as_of"].is_null());
    }

    #[test]
    fn a_cutoff_leaves_out_the_replayed_review_and_everything_written_after_it() {
        // The user drafted comments before the cutoff and submitted the review at it, so the comments are
        // dated before the cutoff although nobody could read them then.
        let pr = gql_pr(
            vec![
                gql_review(
                    "alice",
                    "APPROVED",
                    Some("2031-07-16T02:43:19Z"),
                    vec![
                        gql_comment("alice", "2031-07-16T02:38:42Z", "rename this", None),
                        gql_comment(
                            "alice",
                            "2031-07-16T02:40:13Z",
                            "edited later",
                            Some("2031-07-27T05:00:00Z"),
                        ),
                    ],
                ),
                gql_review(
                    "sam-example",
                    "COMMENTED",
                    Some("2031-07-17T10:00:00Z"),
                    vec![gql_comment(
                        "sam-example",
                        "2031-07-17T10:00:00Z",
                        "an earlier point",
                        None,
                    )],
                ),
                gql_review(
                    "sam-example",
                    "APPROVED",
                    Some(T),
                    vec![gql_comment(
                        "sam-example",
                        "2031-07-19T21:09:55Z",
                        "the answer",
                        None,
                    )],
                ),
                gql_review(
                    "dave",
                    "COMMENTED",
                    Some("2031-07-27T05:29:47Z"),
                    vec![gql_comment("dave", "2031-07-27T05:29:47Z", "fixed", None)],
                ),
            ],
            &[
                (
                    "codeowners-bot",
                    "2031-07-14T05:42:44Z",
                    None,
                    "code owners",
                ),
                (
                    "ci-review-bot",
                    "2031-07-08T02:13:26Z",
                    Some("2031-07-27T15:51:22Z"),
                    "deployment, rewritten later",
                ),
                ("dave", "2031-07-20T09:00:00Z", None, "after the cutoff"),
            ],
            "Compares rows.",
            &[],
        );
        let github = conversation(&pr, Some(T)).unwrap();
        assert_eq!(
            summary(&github),
            (
                vec![
                    reviewed("alice", "APPROVED", &["rename this"]),
                    reviewed("sam-example", "COMMENTED", &["an earlier point"]),
                ],
                vec!["code owners".to_string()]
            )
        );
        assert!(!github.to_string().contains("the answer"));
        assert_eq!(github["as_of"], "2031-07-19T21:35:04+00:00");
    }

    #[test]
    fn a_cutoff_reads_the_body_as_it_was_then() {
        // GitHub lists body versions newest first, the first version included, once a body is edited.
        let edits = [
            ("2031-07-27T00:32:19Z", "v3, after the restack"),
            ("2031-07-16T01:00:00Z", "v2"),
            ("2031-07-08T02:03:58Z", "v1"),
        ];
        let pr = gql_pr(vec![], &[], "v3, after the restack", &edits);
        assert_eq!(conversation(&pr, Some(T)).unwrap()["pr"]["body"], "v2");
        assert_eq!(
            conversation(&pr, None).unwrap()["pr"]["body"],
            "v3, after the restack"
        );
        let never = gql_pr(vec![], &[], "never edited", &[]);
        assert_eq!(
            conversation(&never, Some(T)).unwrap()["pr"]["body"],
            "never edited"
        );
    }

    #[test]
    fn a_conversation_list_longer_than_one_page_fails() {
        let mut pr = gql_pr(
            vec![gql_review(
                "alice",
                "APPROVED",
                Some("2031-07-16T02:43:19Z"),
                vec![],
            )],
            &[],
            "b",
            &[],
        );
        pr["reviews"]["totalCount"] = json!(101);
        assert!(conversation(&pr, None).is_err());
    }

    #[test]
    fn check_overview_keeps_a_fitting_overview_and_moves_any_other_aside() {
        let run = tempfile::tempdir().unwrap();
        let run = run.path();
        let patch = "diff --git a/app.go b/app.go\n--- a/app.go\n+++ b/app.go\n@@ -1,1 +1,2 @@\n one\n+two\n";
        fs::write(run.join("diff.patch"), patch).unwrap();
        let findings = r#"{"status": "reviewed", "skip_reason": "", "findings": [], "approval": {"verdict": "look_first", "reason": "r"}}"#;
        fs::write(run.join("findings.json"), findings).unwrap();
        // Without overview.json it changes nothing.
        check_overview(run).unwrap();
        let names = || {
            let mut n: Vec<String> = fsx::entries(run).iter().map(|p| fsx::name_of(p)).collect();
            n.sort();
            n
        };
        assert_eq!(names(), ["diff.patch", "findings.json"]);
        let kept = json!({"summary": "s", "verdict_reason": "v", "before_after": [], "places": [],
            "layers": [{"id": "l1", "title": "t", "intent": "i", "read_hint": "read",
                        "ranges": [{"path": "app.go", "start_line": 1, "end_line": 2}]}],
            "flow": null, "checked": [], "not_checked": []});
        fs::write(run.join("overview.json"), kept.to_string()).unwrap();
        check_overview(run).unwrap();
        assert_eq!(
            fs::read_to_string(run.join("overview.json")).unwrap(),
            kept.to_string()
        );
        assert!(!run.join("overview.invalid.json").exists());
        // An overview.json that breaks a rule, is not JSON, or cannot be read moves to
        // overview.invalid.json with why and what it held; findings.json stays as it was.
        let mut broken = kept.clone();
        broken["layers"][0]["ranges"][0]["end_line"] = json!(3);
        for (content, reason, held) in [
            (
                broken.to_string().into_bytes(),
                "overview.layers[0].ranges[0], lines 1 to 3 of app.go, is not inside one hunk of the diff".to_string(),
                broken.clone(),
            ),
            (b"\xff\xfe{}".to_vec(), "overview.json cannot be read: it is not UTF-8".to_string(), Value::Null),
        ] {
            fs::write(run.join("overview.json"), content).unwrap();
            check_overview(run).unwrap();
            assert!(!run.join("overview.json").exists());
            assert_eq!(
                fsx::read_json(&run.join("overview.invalid.json")).unwrap(),
                json!({"reason": reason, "overview": held})
            );
            assert_eq!(fs::read_to_string(run.join("findings.json")).unwrap(), findings);
        }
        fs::write(run.join("overview.json"), r#"{"summary": "Pages GET"#).unwrap();
        check_overview(run).unwrap();
        let invalid = fsx::read_json(&run.join("overview.invalid.json")).unwrap();
        assert_eq!(invalid["overview"], r#"{"summary": "Pages GET"#);
        assert!(
            invalid["reason"]
                .as_str()
                .unwrap()
                .starts_with("overview.json is not JSON: ")
        );
    }
}
