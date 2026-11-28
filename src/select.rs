//! Selecting the work a round starts: the newest poll's items and the recheck list's, without the work
//! already handled.

use std::fs;
use std::path::Path;

use jiff::Zoned;
use serde_json::Value;

use crate::fail;
use crate::fsx::{self, Object, Result, read_object, str_of};
use crate::lock;
use crate::status::{REMOVED_BY_USER, SKIPPED_BY_USER};
use crate::work::{MAX_FAILURES, newest_run, recheck_path, record_path, write_record};

/// A recheck-list item waits for the next day once its PR had this many reviews of other heads today.
pub const MAX_RECHECKS_PER_DAY: usize = 3;

/// Whether item asks again for a review the user removed: the newest run of its PR is marked removed,
/// and item has that run's head and kind and no Slack ask but the run's own. A GitHub request reads the
/// same in every poll while it stands, so it is no new request, and neither is a post the poll reads
/// again. A new post, new commits, or another kind is new.
fn removed_request(state: &Path, item: &Object) -> bool {
    let Some((run, ran)) = newest_run(
        state,
        str_of(item, "repo").unwrap_or(""),
        item["pr"].as_i64().unwrap_or(0),
    ) else {
        return false;
    };
    if !run.join(REMOVED_BY_USER).exists() {
        return false;
    }
    ran.get("head_sha") == item.get("head_sha")
        && ran.get("kind") == item.get("kind")
        && (item["slack"].is_null()
            || item["slack"]["ts"]
                == ran
                    .get("slack")
                    .map(|s| s["ts"].clone())
                    .unwrap_or(Value::Null))
}

/// What select_work decides: the work to start, each dropped item with why, and the recheck-list items
/// that no longer wait.
pub struct Selection {
    pub work: Vec<Object>,
    pub drops: Vec<(Object, String)>,
    pub finished: Vec<Object>,
}

/// Splits the poll's items and the recheck list's into work to start and items already handled.
///
/// An item is dropped when the config's [repos] has no table for its repo, or its record is done, has
/// failed MAX_FAILURES times, or is running while a live whooper review holds the PR's lock. A record
/// that says running while none does is that of a review that ended without writing how, as after a
/// SIGKILL: it becomes failed, with one more failure. A record that says retry, which whooper retry
/// writes, lets its item through whatever earlier reviews did, and the daily limit does not hold it.
///
/// While the newest run of the PR is marked removed, an item that asks for the same review again is
/// dropped, unless its record says retry; while it is marked skipped, every item of the PR is, and a
/// recheck-list item stays listed, so the review comes back once the user undoes the skip.
///
/// A recheck-list item is dropped when its PR already had MAX_RECHECKS_PER_DAY reviews of other heads
/// started on now's local day, and stays listed for the next day. It no longer waits once the poll lists
/// its PR, since the poll carries the head the PR has now, once its record is done or has failed
/// MAX_FAILURES times, once its PR is merged or closed (closed maps such PRs' URLs to their state, from
/// the fetch's look), or once the removal of its review drops it.
pub fn select_work(
    state: &Path,
    items: &[Object],
    rechecks: &[Object],
    repos: &Object,
    closed: &std::collections::HashMap<String, String>,
    now: &Zoned,
) -> Result<Selection> {
    let key = |i: &Object| (i.get("repo").cloned(), i.get("pr").cloned());
    let listed: Vec<_> = items.iter().map(key).collect();
    let (mut finished, waiting): (Vec<Object>, Vec<Object>) = rechecks
        .iter()
        .cloned()
        .partition(|r| listed.contains(&key(r)));
    let (mut work, mut drops) = (vec![], vec![]);
    let all = items
        .iter()
        .map(|i| (i, false))
        .chain(waiting.iter().map(|r| (r, true)));
    for (item, from_list) in all {
        let path = record_path(state, item);
        let pr_dir = path.parent().unwrap();
        let mut record = read_object(&path);
        if record
            .as_ref()
            .is_some_and(|r| str_of(r, "status") == Some("running"))
            && lock::holder(
                &state.join("locks").join(fsx::name_of(pr_dir)),
                "whooper-review",
            )
            .is_none()
        {
            write_record(state, item, "failed")?;
            record = read_object(&path);
        }
        let status = record.as_ref().and_then(|r| str_of(r, "status"));
        let failures = record
            .as_ref()
            .and_then(|r| r.get("failures"))
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let repo = str_of(item, "repo").unwrap_or("");
        let url = format!("https://github.com/{repo}/pull/{}", item["pr"]);
        let mut reason = None;
        if let Some(state_on_github) = closed.get(&url).filter(|_| from_list) {
            reason = Some(format!("{} on GitHub", state_on_github.to_lowercase()));
            finished.push(item.clone());
        } else if !repos.contains_key(repo) {
            reason = Some(format!("the config has no [repos.\"{repo}\"] table"));
        } else if status == Some("running") {
            reason = Some("running at this head".into());
        } else if status == Some("retry") {
            // The user asked for this review, so neither limit holds it.
        } else if record.is_some() && (status == Some("done") || failures >= MAX_FAILURES) {
            reason = Some(if status == Some("done") {
                "done at this head".into()
            } else {
                format!("failed {failures} times at this head")
            });
            if from_list {
                finished.push(item.clone());
            }
        } else if removed_request(state, item) {
            reason = Some("removed by the user".into());
            if from_list {
                finished.push(item.clone());
            }
        } else if newest_run(state, repo, item["pr"].as_i64().unwrap_or(0))
            .is_some_and(|(run, _)| run.join(SKIPPED_BY_USER).exists())
        {
            reason = Some("skipped by the user".into());
        } else if from_list {
            let kind = str_of(item, "kind").unwrap_or("");
            let today = now.date();
            let started = fsx::entries(pr_dir)
                .iter()
                .filter(|p| *p != &path && fsx::name_of(p).ends_with(&format!(".{kind}.json")))
                .filter_map(|p| read_object(p))
                .filter_map(|r| str_of(&r, "started_at").and_then(fsx::parse_time))
                .filter(|t| t.to_zoned(now.time_zone().clone()).date() == today)
                .count();
            if started >= MAX_RECHECKS_PER_DAY {
                reason = Some(format!(
                    "{MAX_RECHECKS_PER_DAY} reviews of other heads already today"
                ));
            }
        }
        match reason {
            Some(reason) => drops.push((item.clone(), reason)),
            None => work.push(item.clone()),
        }
    }
    Ok(Selection {
        work,
        drops,
        finished,
    })
}

/// whooper select: the work items to start, one JSON object per line, from the newest poll output and
/// the recheck list, which loses the entries that no longer wait.
pub fn select(state: &Path, repos: &Object) -> Result<Vec<Object>> {
    let poll = state.join("poll");
    // Poll outputs are named by their UTC start time, so the last name is the newest.
    let newest = fsx::entries(&poll).into_iter().rfind(|p| {
        let name = fsx::name_of(p);
        name.ends_with("Z.json") && name.starts_with(|c: char| c.is_ascii_digit())
    });
    let Some(newest) = newest else {
        fail!("whooper-select: no poll output in {}", poll.display())
    };
    let looked_up = fsx::read_json(&poll.join("recheck-prs.json")).unwrap_or(Value::Null);
    let closed = looked_up
        .as_array()
        .into_iter()
        .flatten()
        .filter(|p| p["state"] != "OPEN")
        .map(|p| {
            (
                p["url"].as_str().unwrap_or("").to_string(),
                p["state"].as_str().unwrap_or("").to_string(),
            )
        })
        .collect();
    let list = state.join("rechecks");
    fs::create_dir_all(&list)?;
    let items: Vec<Object> = match fsx::read_object(&newest) {
        Some(output) => output
            .get("items")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|i| i.as_object().cloned())
            .collect(),
        None => fail!("whooper-select: {} holds no poll output", newest.display()),
    };
    // list_again writes the list under this lock too, so a mark written while this selects waits for
    // the deletes below instead of being deleted with the item it replaces.
    let _lock = fsx::folder_lock(&list)?;
    let rechecks: Vec<Object> = fsx::entries(&list)
        .iter()
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
        .filter_map(|p| read_object(p))
        .collect();
    let selection = select_work(state, &items, &rechecks, repos, &closed, &fsx::now())?;
    for (item, reason) in &selection.drops {
        eprintln!(
            "whooper-select: dropped {}#{} {}: {reason}",
            str_of(item, "repo").unwrap_or(""),
            item["pr"],
            str_of(item, "kind").unwrap_or("")
        );
    }
    for item in &selection.finished {
        let _ = fs::remove_file(recheck_path(state, item));
    }
    Ok(selection.work)
}
