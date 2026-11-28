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

/// The names of the review's steps that whooper step runs, each with the function that saves its file.
pub fn steps() -> BTreeMap<&'static str, fn(&Path) -> Result<()>> {
    BTreeMap::from([
        (
            "connections",
            crate::connections::connections as fn(&Path) -> Result<()>,
        ),
        ("test-gaps", crate::gaps::test_gaps),
        ("examples", crate::examples::examples),
        ("mutation", crate::gaps::mutation),
    ])
}
