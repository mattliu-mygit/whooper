//! What whooper-status and whooper-show print: the reviews of the last 30 days, the round history,
//! what blocks every review, and one review in full, read from the state folder without calling
//! GitHub, Slack, or a model.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use jiff::{Timestamp, Zoned, civil::Date, tz::TimeZone};
use regex::Regex;
use serde_json::{Value, json};

use crate::diff;
use crate::fail;
use crate::fsx::{self, Object, Result, local_time, read_object, str_of};
use crate::lock;
use crate::submit;

/// whooper-status lists the review runs that started in the last 30 days.
const REVIEW_DAYS: i64 = 30;
/// whooper-status counts the rounds that started on each of the last 15 local days, by result.
pub const ROUND_DAYS: i64 = 15;
const ROUND_RESULTS: [&str; 4] = ["done", "poll_failed", "interrupted", "skipped"];
/// Status format 2 lists at most this many items.
const STATUS_ITEMS: usize = 50;
/// The empty file whooper-skip leaves in the folder of a run the user marked skipped.
pub const SKIPPED_BY_USER: &str = "skipped-by-user";
/// The empty file whooper-remove leaves in the folder of a run the user removed from their lists.
pub const REMOVED_BY_USER: &str = "removed-by-user";

/// The UTC start time and the PR number in a review run's ID, such as 20310727T010246Z-12, or None
/// for a name that is not a run ID.
pub fn parse_run_id(run_id: &str) -> Option<(Timestamp, i64)> {
    let re = Regex::new(r"^([0-9]{8})T([0-9]{6})Z-([0-9]+)$").unwrap();
    let m = re.captures(run_id)?;
    let (d, t) = (&m[1], &m[2]);
    let date = Date::new(
        d[..4].parse().ok()?,
        d[4..6].parse().ok()?,
        d[6..].parse().ok()?,
    )
    .ok()?;
    let time = jiff::civil::Time::new(
        t[..2].parse().ok()?,
        t[2..4].parse().ok()?,
        t[4..].parse().ok()?,
        0,
    )
    .ok()?;
    let when = date
        .to_datetime(time)
        .to_zoned(TimeZone::UTC)
        .ok()?
        .timestamp();
    Some((when, m[3].parse().ok()?))
}

/// The folder of the review run run_id under the state folder, for command, such as whooper-skip:
/// refuses a run ID that names no run folder, so a path such as ../x reads nothing outside runs/.
pub fn run_dir(state: &Path, run_id: &str, command: &str) -> Result<PathBuf> {
    let run = state.join("runs").join(run_id);
    if parse_run_id(run_id).is_none() || !run.is_dir() {
        fail!(
            "{command}: no run {run_id} in {}",
            state.join("runs").display()
        );
    }
    Ok(run)
}

/// The IDs of the review runs a live whooper-review still runs, each with the process ID its lock
/// names: the PR's lock names the run in run_id, and its process runs whooper-review. A lock whose
/// process is gone, or now runs another program, is stale.
pub fn running_runs(state: &Path) -> HashMap<String, i64> {
    let mut running = HashMap::new();
    for lock in fsx::entries(&state.join("locks")) {
        let (Some(pid), Some(run_id)) = (
            lock::pid_in(&lock),
            fs::read_to_string(lock.join("run_id")).ok(),
        ) else {
            continue;
        };
        if lock::runs(pid, "whooper-review") {
            running.insert(run_id.trim().to_string(), pid);
        }
    }
    running
}

/// What stops a review, by the cause's ID: a summary, and whether it stops every review ("all") or
/// only its PR's ("pr").
const CAUSES: [(&str, &str, &str); 7] = [
    ("docker_unavailable", "Docker isn't running", "all"),
    ("setup_failed", "The repo's setup script failed", "all"),
    ("gh_signed_out", "gh isn't signed in to GitHub", "all"),
    (
        "repo_not_configured",
        "Whooper isn't set up for this repo",
        "pr",
    ),
    (
        "agent_no_findings",
        "The review agent left no usable findings",
        "pr",
    ),
    ("time_limit", "The review ran past its time limit", "pr"),
    ("unknown", "The review stopped unexpectedly", "pr"),
];

/// What stopped a review, as {id, summary, scope}, from the step of whooper-review it stopped at and
/// that step's error output: the setup script's failure is docker_unavailable when it could not reach
/// Docker and setup_failed otherwise, and the fetch's is gh_signed_out when GitHub refused it for want
/// of a login. Any other step, such as an interrupt, is unknown.
pub fn failure_cause(step: &str, output: &str) -> Value {
    let docker = Regex::new(r"(?i)Cannot connect to the Docker daemon|failed to connect to the docker API|Is the docker daemon running").unwrap();
    let signed_out = Regex::new(
        r"(?i)could not read Username for 'https://github\.com'|Invalid username or token|Authentication failed for 'https://github\.com/|gh auth login|Bad credentials",
    )
    .unwrap();
    let id = match step {
        "setup" if docker.is_match(output) => "docker_unavailable",
        "setup" => "setup_failed",
        "fetch" if signed_out.is_match(output) => "gh_signed_out",
        "clone" => "repo_not_configured",
        "time-limit" => "time_limit",
        "findings" => "agent_no_findings",
        _ => "unknown",
    };
    let (_, summary, scope) = CAUSES.iter().find(|(c, ..)| *c == id).unwrap();
    json!({"id": id, "summary": summary, "scope": scope})
}

/// The cause whooper-review saved in failure.json as it stopped a failed review, or the one in
/// waiting.json for a review that waits; unknown without one, as after a kill -9; None for any other
/// outcome.
fn read_cause(run: &Path, outcome: &str) -> Value {
    if outcome != "failed" && outcome != "waiting" {
        return Value::Null;
    }
    let saved = read_object(&run.join(if outcome == "waiting" {
        "waiting.json"
    } else {
        "failure.json"
    }))
    .unwrap_or_default();
    let fits = saved.get("id").is_some_and(Value::is_string)
        && saved.get("summary").is_some_and(Value::is_string)
        && matches!(str_of(&saved, "scope"), Some("all" | "pr"));
    if fits {
        return json!({"id": saved["id"], "summary": saved["summary"], "scope": saved["scope"]});
    }
    failure_cause("", "")
}

/// The JSON Schema a review's findings.json must fit.
pub fn findings_schema() -> Result<Value> {
    Ok(serde_json::from_str(&fs::read_to_string(
        crate::config::root().join("schemas").join("findings.json"),
    )?)?)
}

/// How the review in run went, as whooper-status and whooper-show report it.
pub struct Review {
    pub item: Option<Object>,
    pub outcome: &'static str,
    pub findings: Option<Value>,
    pub posted: Option<Object>,
    pub cause: Value,
    pub skipped: bool,
    pub removed: bool,
    pub safe: bool,
}

/// Reads the review in the run folder: running while a live whooper-review runs it (running holds
/// those runs, running_runs); waiting for one whooper-round did not start; reviewed once it ended with
/// findings that fit the schema and without failure.txt; and failed otherwise. A file that is missing,
/// cut short, or does not fit reads as None, so a run folder Whooper cannot read gives a failed review
/// rather than an error. The posted review is submit.json once it says posted. It is safe to approve
/// once reviewed with nothing run_concerns argues against approving.
pub fn read_review(run: &Path, running: &HashMap<String, i64>, schema: &Value) -> Review {
    let (outcome, findings) = if running.contains_key(&fsx::name_of(run)) {
        ("running", None)
    } else if run.join("waiting.json").exists() {
        ("waiting", None)
    } else {
        let result = fsx::read_json(&run.join("findings.json"))
            .filter(|r| r.is_object() && crate::schema::check(r, schema, "findings.json").is_ok());
        let outcome = if result.is_some() && !run.join("failure.txt").exists() {
            "reviewed"
        } else {
            "failed"
        };
        (outcome, result)
    };
    let posted =
        read_object(&run.join("submit.json")).filter(|s| str_of(s, "status") == Some("posted"));
    let safe = outcome == "reviewed"
        && findings
            .as_ref()
            .is_some_and(|f| submit::run_concerns(run, f).is_empty());
    Review {
        item: read_object(&run.join("run.json")),
        outcome,
        findings,
        posted,
        cause: read_cause(run, outcome),
        skipped: run.join(SKIPPED_BY_USER).exists(),
        removed: run.join(REMOVED_BY_USER).exists(),
        safe,
    }
}

/// The PR's title and its author's login in the run's github.json, each None when missing.
fn title_and_author(run: &Path) -> (Value, Value) {
    let github = read_object(&run.join("github.json")).unwrap_or_default();
    let pr = github
        .get("pr")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let text = |key: &str| {
        pr.get(key)
            .filter(|v| v.is_string())
            .cloned()
            .unwrap_or(Value::Null)
    };
    (text("title"), text("author"))
}

type Heads = HashMap<(String, i64), (f64, String)>;

/// The head of each PR that Whooper's latest poll saw, in poll/github.json and the newest poll's
/// items, or the recheck list holds, each dated by when its file was written, and of two for one PR
/// the newer.
fn polled_heads(state: &Path) -> Heads {
    let mut heads: Heads = HashMap::new();
    let mut note = |when: f64, repo: Option<&str>, pr: Option<i64>, head: Option<&str>| {
        if let (Some(repo), Some(pr), Some(head)) = (repo, pr, head) {
            let key = (repo.to_string(), pr);
            if heads.get(&key).is_none_or(|(w, _)| when >= *w) {
                heads.insert(key, (when, head.to_string()));
            }
        }
    };
    let stamped = |path: &Path| -> (f64, Option<Value>) {
        let when = fs::metadata(path)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs_f64());
        match (when, fsx::read_json(path)) {
            (Some(w), Some(v)) => (w, Some(v)),
            _ => (0.0, None),
        }
    };
    let poll = state.join("poll");
    let (when, prs) = stamped(&poll.join("github.json"));
    for pr in prs.as_ref().and_then(Value::as_array).into_iter().flatten() {
        note(
            when,
            pr["repository"]["nameWithOwner"].as_str(),
            pr["number"].as_i64(),
            pr["headRefOid"].as_str(),
        );
    }
    let mut items = Vec::new();
    if let Some(newest) = newest_poll(&poll) {
        let (when, out) = stamped(&newest);
        for item in out
            .and_then(|o| o.get("items").and_then(Value::as_array).cloned())
            .unwrap_or_default()
        {
            items.push((when, item));
        }
    }
    for path in fsx::entries(&state.join("rechecks"))
        .into_iter()
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
    {
        if let (when, Some(item)) = stamped(&path) {
            items.push((when, item));
        }
    }
    for (when, item) in items {
        note(
            when,
            item["repo"].as_str(),
            item["pr"].as_i64(),
            item["head_sha"].as_str(),
        );
    }
    heads
}

/// The newest poll output, <state>/poll/<UTC start time>.json, whose names sort by time.
pub fn newest_poll(poll: &Path) -> Option<PathBuf> {
    let re = Regex::new(r"^[0-9].*Z\.json$").unwrap();
    fsx::entries(poll)
        .into_iter()
        .rfind(|p| re.is_match(&fsx::name_of(p)))
}

/// The newest head Whooper knows of repo's PR: of its polled head and the heads its records under
/// prs/ name, the one Whooper learned last. A record dates its head by its started_at.
fn pr_head(state: &Path, polled: &Heads, repo: &str, pr: i64) -> Value {
    let mut known: Vec<(f64, String)> = polled
        .get(&(repo.to_string(), pr))
        .cloned()
        .into_iter()
        .collect();
    for path in fsx::entries(
        &state
            .join("prs")
            .join(format!("{}-{pr}", repo.replace('/', "-"))),
    ) {
        if path.extension().is_none_or(|e| e != "json") {
            continue;
        }
        let record = read_object(&path).unwrap_or_default();
        if let Some(when) = str_of(&record, "started_at").and_then(fsx::parse_time) {
            let head = fsx::name_of(&path)
                .split('.')
                .next()
                .unwrap_or_default()
                .to_string();
            known.push((when.as_second() as f64, head));
        }
    }
    known
        .into_iter()
        .max_by(|a, b| a.partial_cmp(b).unwrap())
        .map(|(_, h)| json!(h))
        .unwrap_or(Value::Null)
}

/// The review runs that started in the 30 days before now, newest first.
pub fn read_reviews(state: &Path, now: Timestamp) -> Result<Vec<Object>> {
    let schema = findings_schema()?;
    let mut runs = Vec::new();
    for run in fsx::entries(&state.join("runs")).into_iter().rev() {
        if let Some((started, pr)) = parse_run_id(&fsx::name_of(&run))
            && started >= now - jiff::SignedDuration::from_hours(24 * REVIEW_DAYS)
            && run.is_dir()
        {
            runs.push((run, started, pr));
        }
    }
    // whooper-review takes the PR's lock before it makes the run folder, so the locks, read after the
    // runs are listed, name every listed run whose review still runs.
    let running = running_runs(state);
    let polled = polled_heads(state);
    let mut reviews = Vec::new();
    let mut newer: HashSet<(Option<String>, i64)> = HashSet::new();
    for (run, started, pr) in runs {
        let review = read_review(&run, &running, &schema);
        let (title, author) = title_and_author(&run);
        let repo = review
            .item
            .as_ref()
            .and_then(|i| str_of(i, "repo"))
            .map(String::from);
        let head = review
            .item
            .as_ref()
            .and_then(|i| i.get("head_sha"))
            .filter(|h| h.is_string())
            .cloned()
            .unwrap_or(Value::Null);
        let ended = if review.outcome == "running" {
            None
        } else {
            fsx::modified(&run.join("failure.txt"))
                .or_else(|| fsx::modified(&run.join("findings.json")))
        };
        let f = review.findings.as_ref();
        let key = (repo.clone(), pr);
        let needs_you = !newer.contains(&key)
            && !matches!(review.outcome, "running" | "waiting")
            && review.posted.is_none()
            && !review.skipped
            && !review.removed;
        reviews.push(json!({
            "run_id": fsx::name_of(&run),
            "repo": repo,
            "pr": pr,
            "title": title,
            "author": author,
            "url": repo.as_ref().map(|r| format!("https://github.com/{r}/pull/{pr}")),
            "started_at": local_time(started.as_second()),
            "ended_at": ended,
            "outcome": review.outcome,
            "verdict": f.map(|f| f["approval"]["verdict"].clone()),
            "skipped": f.is_some_and(|f| f["status"] == "skipped"),
            "findings": f.map(|f| f["findings"].as_array().map_or(0, Vec::len)).unwrap_or(0),
            "safe_to_approve": review.safe,
            "submitted": review.posted.as_ref().map(|s| json!({"event": s["request"]["event"], "at": fsx::modified(&run.join("submit.json"))})),
            "needs_you": needs_you,
            "cause": review.cause,
            "head_sha": head,
            "pr_head_sha": repo.as_ref().map(|r| pr_head(state, &polled, r, pr)).unwrap_or(Value::Null),
            "skipped_by_user": review.skipped,
            "removed_by_user": review.removed,
        }).as_object().cloned().unwrap());
        newer.insert(key);
    }
    Ok(reviews)
}

/// What stops every review: while the newest review that ended or waits stopped at a cause whose
/// scope is all, that cause; since, when the first of the reviews just before it at the same cause
/// ended, or started for one that waits; and waiting, how many PRs' newest review waits, leaving out
/// those the user removed or skipped. None otherwise.
fn read_blocked(reviews: &[Object]) -> Value {
    let ended: Vec<&Object> = reviews
        .iter()
        .filter(|r| r["outcome"] != "running")
        .collect();
    let Some(cause) = ended
        .first()
        .map(|r| &r["cause"])
        .filter(|c| c["scope"] == "all")
    else {
        return Value::Null;
    };
    let mut first = ended[0];
    for review in &ended {
        if review["cause"].is_null() || review["cause"]["id"] != cause["id"] {
            break;
        }
        first = review;
    }
    let (mut newest, mut waiting) = (HashSet::new(), 0);
    for review in reviews {
        let key = (review["repo"].to_string(), review["pr"].to_string());
        if !newest.contains(&key)
            && review["outcome"] == "waiting"
            && review["removed_by_user"] == false
            && review["skipped_by_user"] == false
        {
            waiting += 1;
        }
        newest.insert(key);
    }
    let mut out = cause.as_object().cloned().unwrap_or_default();
    out.insert(
        "since".into(),
        if first["ended_at"].is_null() {
            first["started_at"].clone()
        } else {
            first["ended_at"].clone()
        },
    );
    out.insert("waiting".into(), json!(waiting));
    Value::Object(out)
}

fn zoned(iso: &str, now: &Zoned) -> Zoned {
    fsx::parse_time(iso)
        .unwrap_or(Timestamp::UNIX_EPOCH)
        .to_zoned(now.time_zone().clone())
}

/// A time of day as Prompting Time's rows show it, such as 3:45 PM.
fn clock(when: &Zoned) -> String {
    let hour = when.hour();
    format!(
        "{}:{:02} {}",
        if hour % 12 == 0 { 12 } else { hour % 12 },
        when.minute(),
        if hour < 12 { "AM" } else { "PM" }
    )
}

/// 3:12 PM today, Nov 24 this year, and Nov 24, 2025 before.
pub fn short_date(iso: &str, now: &Zoned) -> String {
    let when = zoned(iso, now);
    if when.date() == now.date() {
        return clock(&when);
    }
    let day = format!("{} {}", when.strftime("%b"), when.day());
    if when.year() != now.year() {
        format!("{day}, {}", when.year())
    } else {
        day
    }
}

/// 3:45 PM today, and Mon 10:00 AM on another day.
pub fn short_time(iso: &str, now: &Zoned) -> String {
    let when = zoned(iso, now);
    if when.date() == now.date() {
        clock(&when)
    } else {
        format!("{} {}", when.strftime("%a"), clock(&when))
    }
}

/// The status group of a PR's newest review: None once the user removed it; ready or look while it
/// waits on the user's decision; done once the user is finished with it; reviewing while it runs or
/// waits without a cause that stops every review; waiting while it waits or failed at such a cause;
/// and failed when it failed at a cause of its own PR's.
fn review_group(review: &Object) -> Option<&'static str> {
    if review["removed_by_user"] == true {
        return None;
    }
    let reviewed = review["outcome"] == "reviewed";
    let finished = !review["submitted"].is_null()
        || review["skipped_by_user"] == true
        || reviewed && review["needs_you"] == false;
    if reviewed && review["needs_you"] == true && !finished {
        return Some(if review["safe_to_approve"] == true {
            "ready"
        } else {
            "look"
        });
    }
    if finished {
        return Some("done");
    }
    if review["outcome"] == "running" {
        return Some("reviewing");
    }
    let blocking = review["cause"]["scope"] == "all";
    if review["outcome"] == "waiting" {
        return Some(if blocking { "waiting" } else { "reviewing" });
    }
    Some(if blocking { "waiting" } else { "failed" })
}

fn review_item(review: &Object, group: &str, now: &Zoned) -> Value {
    let (repo, pr, cause) = (review["repo"].as_str(), &review["pr"], &review["cause"]);
    let name = match repo {
        Some(r) => format!("{}#{pr}", r.rsplit('/').next().unwrap_or(r)),
        None => format!("#{pr}"),
    };
    let outcome = review["outcome"].as_str().unwrap_or_default();
    let standing = if let Some(submitted) = review["submitted"].as_object() {
        let verb = if submitted["event"] == "APPROVE" {
            "approved"
        } else {
            "commented"
        };
        match submitted["at"].as_str() {
            Some(at) => format!("{verb} {}", short_date(at, now)),
            None => verb.to_string(),
        }
    } else if review["skipped_by_user"] == true {
        "skipped".into()
    } else if outcome == "running" {
        format!(
            "reviewing since {}",
            short_date(review["started_at"].as_str().unwrap_or_default(), now)
        )
    } else if outcome == "waiting" {
        if cause.is_null() {
            "waiting for the next round".into()
        } else {
            format!(
                "waiting · {}",
                cause["summary"].as_str().unwrap_or_default()
            )
        }
    } else if outcome == "failed" {
        cause["summary"]
            .as_str()
            .unwrap_or("review failed")
            .to_string()
    } else if review["needs_you"] == false {
        "not submitted".into()
    } else if review["skipped"] == true {
        "skipped, look first".into()
    } else {
        match review["findings"].as_i64().unwrap_or(0) {
            0 => "no findings".into(),
            1 => "1 finding".into(),
            n => format!("{n} findings"),
        }
    };
    let dot = if !review["submitted"].is_null() || review["skipped_by_user"] == true {
        "idle"
    } else {
        match outcome {
            "running" => "running",
            "waiting" => "idle",
            "failed" => "error",
            _ => match group {
                "ready" => "success",
                "look" => "attention",
                _ => "idle",
            },
        }
    };
    let running = outcome == "running";
    let mut actions = Vec::new();
    if !running && repo.is_some() {
        actions.push("retry");
    }
    if review["submitted"].is_null() && review["skipped_by_user"] == false {
        actions.push("skip");
    }
    if !running {
        actions.push("remove");
    }
    let mut detail = vec![name];
    if let Some(author) = review["author"].as_str() {
        detail.push(format!("@{author}"));
    }
    detail.push(standing);
    let title = match (review["title"].as_str(), repo) {
        (Some(t), _) => t.to_string(),
        (None, Some(r)) => format!("{r}#{pr}"),
        (None, None) => format!("PR #{pr}"),
    };
    let updated = review["submitted"]["at"]
        .as_str()
        .map(|s| json!(s))
        .filter(|_| true)
        .or_else(|| {
            if review["ended_at"].is_null() {
                None
            } else {
                Some(review["ended_at"].clone())
            }
        });
    json!({
        "id": review["run_id"],
        "group": group,
        "title": title,
        "detail": detail.join(" · "),
        "dot": dot,
        "project": null,
        "updated_at": updated.unwrap_or_else(|| review["started_at"].clone()),
        "route": format!("/reviews/{}", review["run_id"].as_str().unwrap_or_default()),
        "actions": actions,
    })
}

/// The fields of Prompting Time's status format 2: Whooper's agent row, the causes that stop every
/// review as blockers, and the newest review of each PR the user did not remove as items, at most 50,
/// those still current before the finished ones and each part newest first.
fn status_v2(status: &Object, now: &Zoned) -> Object {
    let (blocked, last, round) = (&status["blocked"], &status["last_round"], &status["round"]);
    let agent = if !blocked.is_null() {
        json!({"words": format!("Can't review · {}", blocked["summary"].as_str().unwrap_or_default()), "dot": "error"})
    } else if round["running"] == true {
        let words = match round["reviewing"].as_str() {
            Some(pr) => format!("Reviewing {pr}"),
            None => "Polling now".into(),
        };
        json!({"words": words, "dot": "running"})
    } else if status["paused"] == true {
        json!({"words": "Paused", "dot": "idle"})
    } else {
        let upcoming = status["next_round"].as_str().map(|n| short_time(n, now));
        if last["result"] == "poll_failed" {
            let mut words = format!(
                "Poll failed {}",
                short_time(last["ended_at"].as_str().unwrap_or_default(), now)
            );
            if let Some(u) = &upcoming {
                words.push_str(&format!(" · next try {u}"));
            }
            json!({"words": words, "dot": "error"})
        } else {
            json!({"words": upcoming.map_or("Running".into(), |u| format!("Running · next poll {u}")), "dot": "idle"})
        }
    };
    let mut seen = HashSet::new();
    let mut items = Vec::new();
    for review in status["reviews"].as_array().into_iter().flatten() {
        let review = review.as_object().unwrap();
        if !seen.insert((review["repo"].to_string(), review["pr"].to_string())) {
            continue;
        }
        if let Some(group) = review_group(review) {
            items.push((review, review_item(review, group, now)));
        }
    }
    let mut causes: Vec<(String, String)> = Vec::new();
    if !blocked.is_null() {
        causes.push((
            blocked["id"].as_str().unwrap_or_default().into(),
            blocked["summary"].as_str().unwrap_or_default().into(),
        ));
    }
    for (review, item) in &items {
        if item["group"] == "waiting" && !review["cause"].is_null() {
            let id = review["cause"]["id"]
                .as_str()
                .unwrap_or_default()
                .to_string();
            if !causes.iter().any(|(c, _)| *c == id) {
                causes.push((
                    id,
                    review["cause"]["summary"]
                        .as_str()
                        .unwrap_or_default()
                        .into(),
                ));
            }
        }
    }
    let mut ordered: Vec<Value> = items.into_iter().map(|(_, item)| item).collect();
    let time = |item: &Value| {
        fsx::parse_time(item["updated_at"].as_str().unwrap_or_default())
            .unwrap_or(Timestamp::UNIX_EPOCH)
    };
    // Stable sorts keep equal keys in order, so each part stays newest first.
    ordered.sort_by_key(|item| std::cmp::Reverse(time(item)));
    ordered.sort_by_key(|item| item["group"] == "done");
    let truncated = ordered.len() > STATUS_ITEMS;
    ordered.truncate(STATUS_ITEMS);
    json!({
        "format": 2,
        "agent": agent,
        "blockers": causes.iter().map(|(id, title)| json!({"id": id, "title": title, "dot": "error", "route": format!("/causes/{id}"), "action": "round"})).collect::<Vec<_>>(),
        "items": ordered,
        "truncated": truncated,
    })
    .as_object()
    .cloned()
    .unwrap()
}

/// Whooper's status for whooper-status: whether whooper-run's round runs and what it reviews, whether
/// Whooper is paused, when the next scheduled round starts, how the last round ended, what blocks
/// every review, the reviews of the last 30 days, how many rounds started on each of the last 15 local
/// days by result, and beside them the same status in Prompting Time's status format 2.
pub fn read_status(state: &Path) -> Result<Value> {
    let read = |name: &str| fsx::read_text(&state.join(name));
    let (looping, current, record) = {
        let _held = lock::reading(state);
        let looping = lock::holder(&state.join("run.lock"), "whooper-run");
        let current = lock::holder(&state.join("round.lock"), "whooper-round");
        let record =
            current.and_then(|_| fsx::read_json(&state.join("round.lock").join("round.json")));
        (looping, current, record)
    };
    // whooper-run writes next-round before each wait and deletes it when the round is due.
    let next_round = looping
        .and_then(|_| read("next-round"))
        .and_then(|t| t.trim().parse::<i64>().ok());
    let last = read("last-round.json")
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .filter(Value::is_object);
    let rounds: Vec<Value> = read("rounds.jsonl")
        .unwrap_or_default()
        .lines()
        .filter_map(|l| serde_json::from_str(l).ok())
        .collect();
    let now = fsx::now();
    let today = now.date();
    let days: Vec<Date> = (0..ROUND_DAYS)
        .rev()
        .map(|back| today.checked_sub(jiff::Span::new().days(back)).unwrap())
        .collect();
    let mut by_day: HashMap<Date, HashMap<String, i64>> =
        days.iter().map(|d| (*d, HashMap::new())).collect();
    for r in &rounds {
        if let Some(started) = r["started_at"].as_i64() {
            let day = Timestamp::from_second(started)
                .unwrap()
                .to_zoned(TimeZone::system())
                .date();
            if let Some(counts) = by_day.get_mut(&day) {
                *counts
                    .entry(r["result"].as_str().unwrap_or_default().to_string())
                    .or_default() += 1;
            }
        }
    }
    let reviews = read_reviews(state, now.timestamp())?;
    let local = |v: &Value| v.as_i64().map(local_time);
    let last_round = last.map(|mut l| {
        let started = local(&l["started_at"]);
        let ended = local(&l["ended_at"]);
        l["started_at"] = json!(started);
        l["ended_at"] = json!(ended);
        l
    });
    let mut status = json!({
        "paused": state.join("paused").exists(),
        "next_round": next_round.map(local_time),
        "round": {
            "running": current.is_some(),
            "started_at": record.as_ref().and_then(|r| local(&r["started_at"])),
            "reviewing": record.as_ref().map(|r| r["reviewing"].clone()),
        },
        "last_round": last_round,
        "blocked": read_blocked(&reviews),
        "reviews": reviews,
        "rounds_by_day": days.iter().map(|day| {
            let mut entry = serde_json::Map::new();
            entry.insert("date".into(), json!(day.to_string()));
            for result in ROUND_RESULTS {
                entry.insert(result.into(), json!(by_day[day].get(result).copied().unwrap_or(0)));
            }
            Value::Object(entry)
        }).collect::<Vec<_>>(),
    })
    .as_object()
    .cloned()
    .unwrap();
    let v2 = status_v2(&status, &now);
    status.extend(v2);
    Ok(Value::Object(status))
}

/// Adds entry, a round that ended or one whooper-run skipped, to rounds.jsonl, the history
/// whooper-status counts by day: one JSON object a line, oldest first, each round that started on one
/// of the last 15 local days. The file is rewritten through a rename under the state folder's lock,
/// so of two rounds that end at once neither drops the other's line.
pub fn add_round(state: &Path, entry: &Value) -> Result<()> {
    let path = state.join("rounds.jsonl");
    let first_day = fsx::now()
        .date()
        .checked_sub(jiff::Span::new().days(ROUND_DAYS - 1))
        .unwrap();
    let _lock = fsx::folder_lock(state)?;
    let mut lines: Vec<String> = fsx::read_text(&path)
        .unwrap_or_default()
        .lines()
        .map(String::from)
        .collect();
    lines.push(serde_json::to_string(entry)?);
    let kept: String = lines
        .into_iter()
        .filter(|line| {
            serde_json::from_str::<Value>(line)
                .ok()
                .and_then(|v| v["started_at"].as_i64())
                .is_some_and(|s| {
                    Timestamp::from_second(s)
                        .unwrap()
                        .to_zoned(TimeZone::system())
                        .date()
                        >= first_day
                })
        })
        .map(|line| format!("{line}\n"))
        .collect();
    fsx::write_file(&path, &kept, false)?;
    Ok(())
}

/// whooper-show's co_change: a file outside the PR counts when it changed in at least 60% of the
/// commits of one of the PR's files in the 90 days before the review started, and in at least 3 of
/// them; at most 5 show.
const CO_CHANGE_DAYS: i64 = 90;

/// One review run for Prompting Time's review screen, as whooper-show prints it.
pub fn show_review(state: &Path, run_id: &str) -> Result<Value> {
    let run = state.join("runs").join(run_id);
    let Some((started, pr)) = parse_run_id(run_id).filter(|_| run.is_dir()) else {
        fail!(
            "whooper-show: no run {run_id} in {}",
            state.join("runs").display()
        )
    };
    let schema = findings_schema()?;
    let review = read_review(&run, &running_runs(state), &schema);
    let (title, author) = title_and_author(&run);
    let text = |name: &str| fsx::read_text(&run.join(name));
    let patch = text("diff.patch");
    let ended = review.outcome != "running";
    let (overview, dropped) = if ended {
        diff::read_overview(&run, patch.as_deref().unwrap_or(""))
    } else {
        (None, None)
    };
    let item = review.item.clone();
    let repo = item
        .as_ref()
        .and_then(|i| str_of(i, "repo"))
        .map(String::from);
    let head = item
        .as_ref()
        .and_then(|i| str_of(i, "head_sha"))
        .map(String::from);
    let newest = repo.as_ref().and_then(|r| {
        pr_head(state, &polled_heads(state), r, pr)
            .as_str()
            .map(String::from)
    });
    let since = match (&head, &newest, &repo) {
        (Some(h), Some(n), _) if h == n => json!([]),
        (Some(h), Some(n), Some(r)) => {
            diff::clone_log(r, &["--first-parent", &format!("{h}..{n}")])
                .map_or(Value::Null, Value::Array)
        }
        _ => Value::Null,
    };
    let files = diff::diff_files(patch.as_deref().unwrap_or(""));
    let paths: HashSet<String> = files
        .iter()
        .flat_map(|f| [Some(f.path.clone()), f.old.clone()])
        .flatten()
        .collect();
    let merge_base = item
        .as_ref()
        .and_then(|i| str_of(i, "merge_base"))
        .map(String::from);
    let history = match (&repo, &merge_base) {
        (Some(r), Some(base)) if !paths.is_empty() => {
            let since = (started - jiff::SignedDuration::from_hours(24 * CO_CHANGE_DAYS))
                .to_zoned(TimeZone::UTC);
            diff::clone_log(
                r,
                &[
                    &format!("--since={}", fsx::iso(&since)),
                    "--no-merges",
                    base,
                ],
            )
        }
        _ => None,
    };
    let commits: Vec<HashSet<String>> = history
        .unwrap_or_default()
        .iter()
        .map(|c| {
            c["files"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|f| f.as_str().map(String::from))
                .collect()
        })
        .collect();
    let asks: Vec<Value> = crate::ask::ask_files(&run)
        .iter()
        .filter_map(|p| fsx::read_json(p))
        .collect();
    let after = |name: &str| {
        if ended {
            read_object(&run.join(name))
                .map(Value::Object)
                .unwrap_or(Value::Null)
        } else {
            Value::Null
        }
    };
    Ok(json!({
        "run_id": run_id,
        "run": item,
        "title": title,
        "author": author,
        "outcome": review.outcome,
        "findings": review.findings,
        "safe_to_approve": review.safe,
        "drafts": if review.findings.is_some() { submit::read_drafts(&run).ok() } else { None },
        "diff": patch,
        "failure": text("failure.txt"),
        "asks": asks,
        "submitted": review.posted,
        "cause": review.cause,
        "head_sha": head,
        "skipped_by_user": review.skipped,
        "removed_by_user": review.removed,
        "overview": if dropped.is_some() { Value::Null } else { overview.unwrap_or(Value::Null) },
        "commits_since_review": since,
        "co_change": diff::co_change(&commits, &paths),
        "connections": after("connections.json"),
        "test_gaps": after("test_gaps.json"),
        "examples": after("examples.json"),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cause(id: &str) -> Value {
        let (_, summary, scope) = CAUSES.iter().find(|(c, ..)| *c == id).unwrap();
        json!({"id": id, "summary": summary, "scope": scope})
    }

    #[test]
    fn a_setup_script_that_cannot_reach_docker_stops_every_review() {
        for output in [
            // OrbStack with a newer docker CLI, after a warning of the script's own.
            "! You are not logged in to FloxHub. Run 'flox auth login' to log in.\nfailed to connect to the docker API at unix:///Users/sam/.orbstack/run/docker.sock; check if the path is correct and if the daemon is running: dial unix /Users/sam/.orbstack/run/docker.sock: connect: no such file or directory\n",
            // Docker Desktop, from docker run, and colima.
            "docker: Cannot connect to the Docker daemon at unix:///Users/sam/.docker/run/docker.sock. Is the docker daemon running?\nRun 'docker run --help' for more information\n",
            "Cannot connect to the Docker daemon at unix:///Users/sam/.colima/default/docker.sock. Is the docker daemon running?\n",
        ] {
            assert_eq!(failure_cause("setup", output), cause("docker_unavailable"));
        }
        assert_eq!(cause("docker_unavailable")["scope"], "all");
    }

    #[test]
    fn any_other_setup_failure_stops_every_review_too() {
        for output in [
            "",
            "make: *** [migrate] Error 1\n",
            "docker: Error response from daemon: Conflict. The container name \"/whooper-postgres\" is already in use.\n",
        ] {
            assert_eq!(failure_cause("setup", output), cause("setup_failed"));
        }
        assert_eq!(cause("setup_failed")["scope"], "all");
    }

    #[test]
    fn a_fetch_github_refuses_for_want_of_a_login_stops_every_review() {
        for output in [
            // git with gh as its credential helper, signed out, and no terminal to ask on.
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n",
            "fatal: could not read Username for 'https://github.com': Device not configured\n",
            // git with a token GitHub no longer takes.
            "remote: Invalid username or token. Password authentication is not supported for Git operations.\nfatal: Authentication failed for 'https://github.com/example-org/example-repo.git/'\n",
            // gh itself.
            "To get started with GitHub CLI, please run:  gh auth login\nAlternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.\n",
            "HTTP 401: Bad credentials (https://api.github.com/graphql)\nTry authenticating with:  gh auth login\n",
        ] {
            assert_eq!(failure_cause("fetch", output), cause("gh_signed_out"));
        }
        assert_eq!(cause("gh_signed_out")["scope"], "all");
    }

    #[test]
    fn any_other_fetch_failure_is_unknown() {
        for output in [
            "fatal: couldn't find remote ref no-such-branch\n",
            "fatal: unable to access 'https://github.com/example-org/example-repo.git/': Could not resolve host: github.com\n",
        ] {
            assert_eq!(failure_cause("fetch", output), cause("unknown"));
        }
    }

    #[test]
    fn the_other_steps_name_their_cause_whatever_they_printed() {
        for (step, id, scope) in [
            ("clone", "repo_not_configured", "pr"),
            ("time-limit", "time_limit", "pr"),
            ("findings", "agent_no_findings", "pr"),
            ("prepare", "unknown", "pr"),
            ("review", "unknown", "pr"),
            ("interrupted", "unknown", "pr"),
            ("", "unknown", "pr"),
        ] {
            // Docker's and gh's messages count only for the steps that run them.
            let got = failure_cause(step, "Cannot connect to the Docker daemon. gh auth login");
            assert_eq!(got, cause(id), "{step}");
            assert_eq!(got["scope"], scope);
        }
    }

    #[test]
    fn each_cause_has_the_summary_and_scope_the_page_shows() {
        let all: Vec<Value> = CAUSES.iter().map(|(id, ..)| cause(id)).collect();
        assert_eq!(
            Value::Array(all),
            json!([
                {"id": "docker_unavailable", "summary": "Docker isn't running", "scope": "all"},
                {"id": "setup_failed", "summary": "The repo's setup script failed", "scope": "all"},
                {"id": "gh_signed_out", "summary": "gh isn't signed in to GitHub", "scope": "all"},
                {"id": "repo_not_configured", "summary": "Whooper isn't set up for this repo", "scope": "pr"},
                {"id": "agent_no_findings", "summary": "The review agent left no usable findings", "scope": "pr"},
                {"id": "time_limit", "summary": "The review ran past its time limit", "scope": "pr"},
                {"id": "unknown", "summary": "The review stopped unexpectedly", "scope": "pr"},
            ])
        );
    }
}
