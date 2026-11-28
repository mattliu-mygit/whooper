//! Review work: the PR records under prs/, the recheck list under rechecks/, and the commands that
//! change what a run means: retry, skip, remove, and a review that waits.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Value, json};

use crate::fail;
use crate::fsx::{self, Object, Result, read_object, str_of};
use crate::status::{REMOVED_BY_USER, SKIPPED_BY_USER, parse_run_id, run_dir, running_runs};

/// Records fail at most this many times at one head before select stops retrying it.
pub const MAX_FAILURES: i64 = 3;

/// The record of work on one head and kind: <owner>-<repo>-<pr>/<head>.<kind>.json under prs/.
pub fn record_path(state: &Path, item: &Object) -> PathBuf {
    state
        .join("prs")
        .join(format!(
            "{}-{}",
            str_of(item, "repo").unwrap_or_default().replace('/', "-"),
            item["pr"]
        ))
        .join(format!(
            "{}.{}.json",
            str_of(item, "head_sha").unwrap_or_default(),
            str_of(item, "kind").unwrap_or_default()
        ))
}

/// Records a review run's status for the item's head and kind: running starts a run now, failed
/// counts one more failure, and done, retry, and stale keep the count. It writes under the PR folder's
/// lock, so two writers cannot drop each other's change. A record that cannot be read starts over.
pub fn write_record(state: &Path, item: &Object, status: &str) -> Result<()> {
    let path = record_path(state, item);
    let started_at = fsx::iso(&fsx::now_utc());
    let folder = path.parent().unwrap();
    fs::create_dir_all(folder)?;
    let _lock = fsx::folder_lock(folder)?;
    let old = read_object(&path).unwrap_or_default();
    let failures =
        old.get("failures").and_then(Value::as_i64).unwrap_or(0) + i64::from(status == "failed");
    let started = if status == "running" || !old.contains_key("started_at") {
        json!(started_at)
    } else {
        old["started_at"].clone()
    };
    let record = json!({"status": status, "failures": failures, "started_at": started});
    fsx::write_file(&path, &serde_json::to_string(&record)?, false)?;
    Ok(())
}

/// The recheck list's file for the item's PR: rechecks/<owner>-<repo>-<pr>.json.
pub fn recheck_path(state: &Path, item: &Object) -> PathBuf {
    state.join("rechecks").join(format!(
        "{}-{}.json",
        str_of(item, "repo").unwrap_or_default().replace('/', "-"),
        item["pr"]
    ))
}

/// Puts a run's work item on the recheck list at head, replacing the list's item for the PR, under the
/// list folder's lock, which select holds from its read of the list to its deletes.
pub fn list_again(state: &Path, item: &Object, head: &str) -> Result<()> {
    let path = recheck_path(state, item);
    let folder = path.parent().unwrap();
    fs::create_dir_all(folder)?;
    let mut work = Object::new();
    for key in [
        "kind", "repo", "pr", "head_sha", "base_ref", "source", "slack", "reason",
    ] {
        work.insert(key.into(), item.get(key).cloned().unwrap_or(Value::Null));
    }
    work.insert("head_sha".into(), json!(head));
    let _lock = fsx::folder_lock(folder)?;
    fsx::write_file(&path, &serde_json::to_string(&work)?, false)?;
    Ok(())
}

/// Records that a run's PR has new commits since the run: the reviewed head's record says stale, and
/// the recheck list holds the run's work item at head_now, so the next round reviews the new commits
/// whatever brought the PR in.
pub fn mark_new_commits(state: &Path, item: &Object, head_now: &str) -> Result<()> {
    write_record(state, item, "stale")?;
    list_again(state, item, head_now)
}

/// The newest review run of repo's PR pr under runs/, with its run.json, or None. Only run.json names
/// a run's repo.
pub fn newest_run(state: &Path, repo: &str, pr: i64) -> Option<(PathBuf, Object)> {
    let suffix = format!("-{pr}");
    for run in fsx::entries(&state.join("runs")).into_iter().rev() {
        let name = fsx::name_of(&run);
        if !name.ends_with(&suffix) || parse_run_id(&name).is_none() {
            continue;
        }
        if let Some(ran) = read_object(&run.join("run.json"))
            && str_of(&ran, "repo") == Some(repo)
        {
            return Some((run, ran));
        }
    }
    None
}

fn run_to_change(state: &Path, run_id: &str, command: &str, verb: &str) -> Result<PathBuf> {
    let run = run_dir(state, run_id, command)?;
    if running_runs(state).contains_key(run_id) {
        fail!("{command}: the review is still running; {verb} it once it ends");
    }
    Ok(run)
}

fn named(run_id: &str, item: &Object) -> String {
    match (str_of(item, "repo"), item.get("pr")) {
        (Some(repo), Some(pr)) => format!("{run_id} ({repo}#{pr})"),
        _ => run_id.to_string(),
    }
}

/// whooper-retry: queues another review of the run's PR at the head it has now: the work item at that
/// head goes on the recheck list, and the record at that head says retry, which select lets through
/// whatever earlier reviews did there. A removed run is unmarked.
pub fn retry(state: &Path, run_id: &str) -> Result<String> {
    let run = run_dir(state, run_id, "whooper-retry")?;
    let Some(item) = read_object(&run.join("run.json"))
        .filter(|i| i.contains_key("repo") && i.contains_key("pr"))
    else {
        fail!(
            "whooper-retry: cannot read {}, which names the PR to review again",
            run.join("run.json").display()
        )
    };
    let head_now = crate::gh::head(
        str_of(&item, "repo").unwrap_or_default(),
        &item["pr"].to_string(),
    )?;
    let run = run_to_change(state, run_id, "whooper-retry", "retry")?;
    let mut at_head = item.clone();
    at_head.insert("head_sha".into(), json!(head_now));
    write_record(state, &at_head, "retry")?;
    list_again(state, &item, &head_now)?;
    let queued = format!(
        "queued another review of {}#{} at {} for the next round; bin/whooper round runs that round now",
        str_of(&item, "repo").unwrap_or_default(),
        item["pr"],
        &head_now[..head_now.len().min(12)]
    );
    let mark = run.join(REMOVED_BY_USER);
    if !mark.exists() {
        return Ok(queued);
    }
    fs::remove_file(mark)?;
    Ok(format!("unmarked {run_id} as removed, and {queued}"))
}

/// whooper-skip: marks the run skipped by the user, or with undo unmarks it. A review a live
/// whooper-review still runs is stopped with a TERM to the process its lock names.
pub fn skip(state: &Path, run_id: &str, undo: bool) -> Result<String> {
    let run = run_dir(state, run_id, "whooper-skip")?;
    let item = read_object(&run.join("run.json")).unwrap_or_default();
    let name = named(run_id, &item);
    let mark = run.join(SKIPPED_BY_USER);
    if undo {
        if !mark.exists() {
            return Ok(format!("{name} is not marked as skipped"));
        }
        fs::remove_file(mark)?;
        return Ok(format!("unmarked {name} as skipped"));
    }
    let pid = running_runs(state).get(run_id).copied();
    if mark.exists() && pid.is_none() {
        return Ok(format!("{name} is already marked as skipped"));
    }
    // The mark comes first, so whooper-status never lists the stopped review as a failure.
    fs::write(&mark, "")?;
    let stopped = pid.is_some_and(|pid| unsafe { libc::kill(pid as i32, libc::SIGTERM) } == 0);
    let newest = match (
        str_of(&item, "repo"),
        item.get("pr").and_then(Value::as_i64),
    ) {
        (Some(repo), Some(pr)) => newest_run(state, repo, pr).is_some_and(|(r, _)| r == run),
        _ => false,
    };
    Ok(format!(
        "marked {name} as skipped, without submitting it{}{} bin/whooper skip {run_id} --undo unmarks it",
        if stopped {
            ", and stopped its review"
        } else {
            ""
        },
        if newest {
            "; Whooper starts no other review of the PR until"
        } else {
            ";"
        }
    ))
}

/// whooper-remove: marks the run removed from the user's lists, or with undo unmarks it. Undone, a
/// review that waits and is still its PR's newest run goes back on the recheck list unless the list
/// holds its PR.
pub fn remove(state: &Path, run_id: &str, undo: bool) -> Result<String> {
    let run = run_to_change(state, run_id, "whooper-remove", "remove")?;
    let item = read_object(&run.join("run.json")).unwrap_or_default();
    let name = named(run_id, &item);
    let mark = run.join(REMOVED_BY_USER);
    if undo {
        if !mark.exists() {
            return Ok(format!("{name} is not marked as removed"));
        }
        fs::remove_file(mark)?;
        if run.join("waiting.json").exists()
            && item.contains_key("repo")
            && !recheck_path(state, &item).exists()
        {
            let newest = newest_run(
                state,
                str_of(&item, "repo").unwrap_or_default(),
                item["pr"].as_i64().unwrap_or(0),
            );
            if newest.is_some_and(|(r, _)| r == run) {
                list_again(state, &item, str_of(&item, "head_sha").unwrap_or_default())?;
                return Ok(format!(
                    "unmarked {name} as removed, and put it back on the recheck list for the next round"
                ));
            }
        }
        return Ok(format!("unmarked {name} as removed"));
    }
    if mark.exists() {
        return Ok(format!("{name} is already marked as removed"));
    }
    fs::write(&mark, "")?;
    Ok(format!(
        "marked {name} as removed; its files stay, and bin/whooper remove {run_id} --undo unmarks it"
    ))
}

/// Records, for whooper-round, that it did not start the review of item because an earlier review in
/// its round stopped at cause, which stops every review: a run folder whose waiting.json holds the
/// cause, or the PR's newest run when it already waits at the item's head and kind and the user did
/// not remove it. The item goes on the recheck list unless the list holds the PR already.
pub fn wait_review(state: &Path, item: &Object, cause: &Value, agent: &str) -> Result<PathBuf> {
    if !recheck_path(state, item).exists() {
        list_again(state, item, str_of(item, "head_sha").unwrap_or_default())?;
    }
    let cause_text = serde_json::to_string(cause)?;
    if let Some((run, ran)) = newest_run(
        state,
        str_of(item, "repo").unwrap_or_default(),
        item["pr"].as_i64().unwrap_or(0),
    ) && run.join("waiting.json").exists()
        && !run.join(REMOVED_BY_USER).exists()
        && ran.get("head_sha") == item.get("head_sha")
        && ran.get("kind") == item.get("kind")
    {
        fsx::write_file(&run.join("waiting.json"), &cause_text, false)?;
        return Ok(run);
    }
    let runs = state.join("runs");
    fs::create_dir_all(&runs)?;
    let mut now = jiff::Timestamp::now();
    let run = loop {
        let name = format!("{}-{}", now.strftime("%Y%m%dT%H%M%SZ"), item["pr"]);
        let run = runs.join(name);
        if !run.exists() {
            break run;
        }
        now += jiff::SignedDuration::from_secs(1);
    };
    // The folder appears whole, through a rename, so whooper-status never lists it without its files.
    let staging = tempfile::Builder::new()
        .prefix(&format!(".{}.", fsx::name_of(&run)))
        .tempdir_in(&runs)?;
    let mut ran = item.clone();
    ran.insert("run_id".into(), json!(fsx::name_of(&run)));
    ran.insert("agent".into(), json!(agent));
    fs::write(
        staging.path().join("run.json"),
        fsx::pretty(&Value::Object(ran)),
    )?;
    fs::write(staging.path().join("waiting.json"), cause_text)?;
    fs::rename(staging.keep(), &run)?;
    Ok(run)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ask(ts: &str) -> Object {
        json!({"kind": "first_review", "repo": "example-org/example-repo", "pr": 13, "head_sha": "h7",
               "base_ref": "master", "source": "slack_ask", "reason": "Alex asked for a review.",
               "slack": {"ts": ts, "tagged_user": true, "permalink": "p"}})
        .as_object()
        .unwrap()
        .clone()
    }

    #[test]
    fn a_new_request_for_a_removed_review_that_waits_waits_in_a_run_of_its_own() {
        let state = tempfile::tempdir().unwrap();
        let cause =
            json!({"id": "docker_unavailable", "summary": "Docker isn't running", "scope": "all"});
        let waits = wait_review(state.path(), &ask("1943194837.000100"), &cause, "claude").unwrap();
        assert!(recheck_path(state.path(), &ask("1")).exists());
        // A round that waits again reuses the run that waits.
        assert_eq!(
            wait_review(state.path(), &ask("1943194837.000100"), &cause, "claude").unwrap(),
            waits
        );
        fs::write(waits.join(REMOVED_BY_USER), "").unwrap();
        let again_ask = ask("1943198437.000200");
        let again = wait_review(state.path(), &again_ask, &cause, "claude").unwrap();
        assert_ne!(again, waits);
        assert_eq!(
            read_object(&again.join("run.json")).unwrap()["slack"],
            again_ask["slack"]
        );
        assert!(waits.join(REMOVED_BY_USER).exists());
        assert_eq!(
            wait_review(state.path(), &again_ask, &cause, "claude").unwrap(),
            again
        );
    }
}
