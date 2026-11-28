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

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::path::PathBuf;

    use serde_json::json;

    use super::*;

    /// 10:00 on 2031-08-01 at UTC-7.
    fn now() -> Zoned {
        "2031-08-01T10:00:00-07:00[-07:00]".parse().unwrap()
    }

    fn repos() -> Object {
        json!({"example-org/example-repo": {}, "sample-org/plain-repo": {}})
            .as_object()
            .unwrap()
            .clone()
    }

    fn work_item(kind: &str, head: &str) -> Object {
        json!({"kind": kind, "repo": "example-org/example-repo", "pr": 12, "head_sha": head,
               "base_ref": "master", "source": "reviewed_by_user", "slack": null, "reason": "r"})
        .as_object()
        .unwrap()
        .clone()
    }

    fn item() -> Object {
        work_item("first_review", "head3")
    }

    fn with(item: &Object, fields: Value) -> Object {
        let mut out = item.clone();
        out.extend(fields.as_object().unwrap().clone());
        out
    }

    /// A recheck-list item: PR 12, which only a Slack ask brought in, as submit lists it on finding its
    /// head moved to head3.
    fn listed_again() -> Object {
        with(
            &item(),
            json!({"source": "slack_ask", "reason": "Alex asked for a review.",
                   "slack": {"ts": "1943194837.000100", "tagged_user": true,
                             "permalink": "https://example.slack.com/archives/C0000000001/p1943194837000100"}}),
        )
    }

    struct State(tempfile::TempDir);

    impl State {
        fn new() -> State {
            State(tempfile::tempdir().unwrap())
        }

        fn path(&self) -> &Path {
            self.0.path()
        }

        fn record_in(
            &self,
            pr_dir: &str,
            name: &str,
            status: &str,
            failures: i64,
            started_at: &str,
        ) {
            let path = self.path().join("prs").join(pr_dir).join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            let record = json!({"status": status, "failures": failures, "started_at": started_at});
            fs::write(path, record.to_string()).unwrap();
        }

        fn record(&self, name: &str, status: &str, failures: i64) {
            self.record_in(
                "example-org-example-repo-12",
                name,
                status,
                failures,
                "2031-08-01T16:00:00Z",
            );
        }

        fn record_at(&self, name: &str, status: &str, failures: i64, started_at: &str) {
            self.record_in(
                "example-org-example-repo-12",
                name,
                status,
                failures,
                started_at,
            );
        }

        /// Makes the run run_id of item, marked removed or skipped by the user as remove and skip mark it.
        fn make_run(&self, run_id: &str, item: &Object, mark: Option<&str>) -> PathBuf {
            let run = self.path().join("runs").join(run_id);
            fs::create_dir_all(&run).unwrap();
            let ran = with(item, json!({"run_id": run_id, "agent": "claude"}));
            fs::write(run.join("run.json"), Value::Object(ran).to_string()).unwrap();
            if let Some(mark) = mark {
                fs::write(run.join(mark), "").unwrap();
            }
            run
        }

        fn select(&self, items: &[Object], rechecks: &[Object]) -> Selection {
            self.select_at(items, rechecks, &HashMap::new(), &now())
        }

        fn select_at(
            &self,
            items: &[Object],
            rechecks: &[Object],
            closed: &HashMap<String, String>,
            now: &Zoned,
        ) -> Selection {
            select_work(self.path(), items, rechecks, &repos(), closed, now).unwrap()
        }

        fn selected(&self, item: &Object) -> bool {
            let s = self.select(std::slice::from_ref(item), &[]);
            assert_eq!(s.work.len() + s.drops.len(), 1);
            !s.work.is_empty()
        }
    }

    type Got = (Vec<Object>, Vec<(Object, String)>, Vec<Object>);

    fn got(s: Selection) -> Got {
        (s.work, s.drops, s.finished)
    }

    fn dropped(item: &Object, why: &str) -> Vec<(Object, String)> {
        vec![(item.clone(), why.to_string())]
    }

    #[test]
    fn an_item_without_a_record_is_work() {
        assert!(State::new().selected(&item()));
    }

    #[test]
    fn a_repo_the_config_does_not_list_is_dropped() {
        let unlisted = with(&item(), json!({"repo": "example-org/unlisted-repo"}));
        assert_eq!(
            got(State::new().select(std::slice::from_ref(&unlisted), &[])),
            (
                vec![],
                dropped(
                    &unlisted,
                    "the config has no [repos.\"example-org/unlisted-repo\"] table"
                ),
                vec![]
            )
        );
    }

    #[test]
    fn a_record_at_this_head_and_kind_decides() {
        for (status, failures, want) in [
            ("done", 0, false),
            // No live whooper review holds the PR's lock, so the review that wrote it is gone.
            ("running", 0, true),
            ("failed", 2, true),
            ("failed", 3, false),
            ("stale", 0, true),
        ] {
            let state = State::new();
            state.record("head3.first_review.json", status, failures);
            assert_eq!(state.selected(&item()), want, "{status} {failures}");
        }
    }

    #[test]
    fn a_running_record_whose_review_is_gone_counts_as_a_failure() {
        let state = State::new();
        let lock = state.path().join("locks/example-org-example-repo-12");
        let mut gone = std::process::Command::new("true").spawn().unwrap();
        gone.wait().unwrap();
        let mut other = std::process::Command::new("sleep")
            .arg("30")
            .spawn()
            .unwrap();
        let path = state
            .path()
            .join("prs/example-org-example-repo-12/head3.first_review.json");
        for holder in [None, Some(gone.id()), Some(other.id())] {
            let _ = fs::remove_dir_all(&lock);
            if let Some(pid) = holder {
                fs::create_dir_all(&lock).unwrap();
                fs::write(lock.join("pid"), format!("{pid}\n")).unwrap();
            }
            state.record("head3.first_review.json", "running", 1);
            assert_eq!(
                got(state.select(&[item()], &[])),
                (vec![item()], vec![], vec![])
            );
            let record = fsx::read_json(&path).unwrap();
            assert_eq!(record["status"], "failed", "{holder:?}");
            assert_eq!(record["failures"], 2);
            assert_eq!(record["started_at"], "2031-08-01T16:00:00Z");
        }
        other.kill().unwrap();
        other.wait().unwrap();
        // The failure counts toward the limit as any other: the head's third failure drops the item and
        // takes it off the recheck list.
        let listed = listed_again();
        state.record("head3.first_review.json", "running", 2);
        assert_eq!(
            got(state.select(&[], std::slice::from_ref(&listed))),
            (
                vec![],
                dropped(&listed, "failed 3 times at this head"),
                vec![listed]
            )
        );
    }

    #[test]
    fn a_running_record_a_live_review_holds_is_dropped() {
        let state = State::new();
        let holder = state.path().join("bin/whooper-review");
        fs::create_dir_all(holder.parent().unwrap()).unwrap();
        fs::write(&holder, "#!/bin/sh\nsleep 30\n").unwrap();
        std::os::unix::fs::PermissionsExt::set_mode(
            &mut fs::metadata(&holder).unwrap().permissions(),
            0o755,
        );
        fs::set_permissions(&holder, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        let mut live = std::process::Command::new(&holder).spawn().unwrap();
        let lock = state.path().join("locks/example-org-example-repo-12");
        fs::create_dir_all(&lock).unwrap();
        fs::write(lock.join("pid"), format!("{}\n", live.id())).unwrap();
        state.record("head3.first_review.json", "running", 1);
        let s = state.select(&[item()], &[]);
        live.kill().unwrap();
        live.wait().unwrap();
        assert_eq!(
            got(s),
            (vec![], dropped(&item(), "running at this head"), vec![])
        );
    }

    #[test]
    fn a_record_for_another_head_kind_or_pr_does_not_match() {
        let state = State::new();
        state.record("head2.first_review.json", "done", 0);
        state.record("head3.recheck.json", "done", 0);
        state.record_in(
            "example-org-example-repo-1",
            "head3.first_review.json",
            "done",
            0,
            "2031-08-01T16:00:00Z",
        );
        assert!(state.selected(&item()));
    }

    #[test]
    fn an_unreadable_record_counts_as_none() {
        // Such as one cut short by a crash before write_record wrote through a temporary file.
        for text in ["", "{\"status\": \"do", "[]"] {
            let state = State::new();
            let path = state
                .path()
                .join("prs/example-org-example-repo-12/head3.first_review.json");
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, text).unwrap();
            assert_eq!(
                got(state.select(&[item()], &[])),
                (vec![item()], vec![], vec![]),
                "{text}"
            );
            // An unreadable record of another head does not count toward a recheck-list item's daily
            // limit.
            fs::rename(&path, path.with_file_name("head2.first_review.json")).unwrap();
            for head in ["head0", "head1"] {
                state.record(&format!("{head}.first_review.json"), "stale", 0);
            }
            let listed = listed_again();
            assert_eq!(
                got(state.select(&[], std::slice::from_ref(&listed))),
                (vec![listed], vec![], vec![])
            );
        }
    }

    #[test]
    fn a_recheck_list_item_whose_pr_github_reports_merged_or_closed_leaves_the_list() {
        let state = State::new();
        let [merged, closed, still_open] =
            [13, 14, 15].map(|number| with(&listed_again(), json!({"pr": number})));
        let states: HashMap<String, String> = [
            (
                "https://github.com/example-org/example-repo/pull/13",
                "MERGED",
            ),
            (
                "https://github.com/example-org/example-repo/pull/14",
                "CLOSED",
            ),
        ]
        .into_iter()
        .map(|(u, s)| (u.to_string(), s.to_string()))
        .collect();
        let all = [merged.clone(), closed.clone(), still_open.clone()];
        assert_eq!(
            got(state.select_at(&[], &all, &states, &now())),
            (
                vec![still_open],
                vec![
                    (merged.clone(), "merged on GitHub".into()),
                    (closed.clone(), "closed on GitHub".into())
                ],
                vec![merged.clone(), closed]
            )
        );
        // Not even a retry reviews a merged PR.
        state.record_in(
            "example-org-example-repo-13",
            "head3.first_review.json",
            "retry",
            0,
            "2031-08-01T16:00:00Z",
        );
        assert_eq!(
            got(state.select_at(&[], std::slice::from_ref(&merged), &states, &now())),
            (vec![], dropped(&merged, "merged on GitHub"), vec![merged])
        );
    }

    #[test]
    fn three_reviews_today_hold_a_fourth_recheck_list_item() {
        let state = State::new();
        for head in ["head0", "head1", "head2"] {
            state.record(&format!("{head}.first_review.json"), "done", 0);
        }
        assert!(state.select(&[], &[listed_again()]).work.is_empty());
        // The poll's own first reviews have no daily limit.
        assert!(state.selected(&item()));
    }

    #[test]
    fn a_failed_third_review_today_is_retried() {
        let state = State::new();
        state.record_at("head1.first_review.json", "done", 0, "2031-08-01T16:00:00Z");
        state.record_at("head2.first_review.json", "done", 0, "2031-08-01T16:20:00Z");
        state.record_at(
            "head3.first_review.json",
            "failed",
            1,
            "2031-08-01T16:40:00Z",
        );
        let listed = listed_again();
        assert_eq!(
            state.select(&[], std::slice::from_ref(&listed)).work,
            vec![listed]
        );
    }

    #[test]
    fn reviews_count_by_local_day() {
        let state = State::new();
        state.record_at("head1.first_review.json", "done", 0, "2031-08-01T16:00:00Z");
        state.record_at("head2.first_review.json", "done", 0, "2031-08-01T16:30:00Z");
        // 23:30 on 2031-07-31 at UTC-7, though 2031-08-01 in UTC.
        state.record_at("head0.first_review.json", "done", 0, "2031-08-01T06:30:00Z");
        let listed = listed_again();
        assert_eq!(
            state.select(&[], std::slice::from_ref(&listed)).work,
            vec![listed]
        );
    }

    #[test]
    fn a_recheck_list_item_is_work_until_a_review_at_its_head_is_done_or_gives_up() {
        let listed = listed_again();
        for (record, selected, finished) in [
            (None, true, false),
            (Some(("running", 0)), true, false),
            (Some(("failed", 2)), true, false),
            (Some(("failed", 3)), false, true),
            (Some(("done", 0)), false, true),
        ] {
            let state = State::new();
            if let Some((status, failures)) = record {
                state.record("head3.first_review.json", status, failures);
            }
            let s = state.select(&[], std::slice::from_ref(&listed));
            let want = |yes: bool| if yes { vec![listed.clone()] } else { vec![] };
            assert_eq!(s.work, want(selected), "{record:?}");
            assert_eq!(s.finished, want(finished), "{record:?}");
        }
    }

    #[test]
    fn the_poll_listing_the_pr_finishes_its_recheck_list_item() {
        // Such as a new ask for the PR, which the poll lists at the head the PR has now.
        let (polled, listed) = (work_item("first_review", "head4"), listed_again());
        assert_eq!(
            got(State::new().select(std::slice::from_ref(&polled), std::slice::from_ref(&listed))),
            (vec![polled], vec![], vec![listed])
        );
    }

    #[test]
    fn the_recheck_limit_holds_a_recheck_list_item_for_the_next_day() {
        let state = State::new();
        for head in ["head0", "head1", "head2"] {
            state.record(&format!("{head}.first_review.json"), "stale", 0);
        }
        let listed = listed_again();
        assert_eq!(
            got(state.select(&[], std::slice::from_ref(&listed))),
            (
                vec![],
                dropped(&listed, "3 reviews of other heads already today"),
                vec![]
            )
        );
        assert!(state.selected(&item()));
        let tomorrow = now().checked_add(jiff::Span::new().days(1)).unwrap();
        assert_eq!(
            got(state.select_at(
                &[],
                std::slice::from_ref(&listed),
                &HashMap::new(),
                &tomorrow
            )),
            (vec![listed], vec![], vec![])
        );
    }

    #[test]
    fn a_retry_is_work_whatever_earlier_reviews_at_its_head_did() {
        let state = State::new();
        for head in ["head0", "head1", "head2"] {
            state.record(&format!("{head}.first_review.json"), "stale", 0);
        }
        state.record("head3.first_review.json", "retry", 3);
        let listed = listed_again();
        assert_eq!(
            got(state.select(&[], std::slice::from_ref(&listed))),
            (vec![listed.clone()], vec![], vec![])
        );
        // A poll that lists the PR finishes the list's item, and the poll's item at that head is the work.
        assert_eq!(
            got(state.select(&[item()], std::slice::from_ref(&listed))),
            (vec![item()], vec![], vec![listed.clone()])
        );
        // Once the review starts, its record counts as any other: here it failed a fourth time.
        state.record("head3.first_review.json", "failed", 4);
        assert_eq!(
            got(state.select(&[], std::slice::from_ref(&listed))),
            (
                vec![],
                dropped(&listed, "failed 4 times at this head"),
                vec![listed]
            )
        );
    }

    #[test]
    fn a_removed_review_holds_back_the_request_it_had_until_a_new_one() {
        let state = State::new();
        let listed = listed_again();
        let removed = state.make_run("20310801T150000Z-12", &listed, Some(REMOVED_BY_USER));
        state.record("head3.first_review.json", "failed", 1);
        let standing = with(&item(), json!({"source": "github_request"}));
        let mut new_ask = listed.clone();
        new_ask["slack"]["ts"] = json!("1943198437.000200");
        for (polled, want) in [
            // A GitHub request reads the same in every poll while it stands, and a Slack post the poll
            // reads again, as after an edit, is the same ask.
            (standing.clone(), false),
            (listed.clone(), false),
            // A new Slack post asks again, or the PR has new commits.
            (new_ask, true),
            (with(&standing, json!({"head_sha": "head4"})), true),
        ] {
            let s = got(state.select(std::slice::from_ref(&polled), &[]));
            if want {
                assert_eq!(s, (vec![polled], vec![], vec![]));
            } else {
                assert_eq!(s, (vec![], dropped(&polled, "removed by the user"), vec![]));
            }
        }
        // The recheck list's item for the PR leaves the list.
        assert_eq!(
            got(state.select(&[], std::slice::from_ref(&listed))),
            (
                vec![],
                dropped(&listed, "removed by the user"),
                vec![listed.clone()]
            )
        );
        // whooper retry marks the record at the head retry: the user asked for the review.
        state.record("head3.first_review.json", "retry", 1);
        let only = |s: &State| got(s.select(std::slice::from_ref(&standing), &[]));
        assert_eq!(only(&state), (vec![standing.clone()], vec![], vec![]));
        state.record("head3.first_review.json", "failed", 1);
        // Only the PR's newest run counts, and a PR of another repo with the same number is another PR.
        state.make_run(
            "20310801T170000Z-12",
            &with(&listed, json!({"repo": "sample-org/plain-repo"})),
            Some(REMOVED_BY_USER),
        );
        let newer = state.make_run("20310801T160000Z-12", &standing, None);
        assert_eq!(only(&state), (vec![standing.clone()], vec![], vec![]));
        // Unmarked, as remove --undo leaves it, the newest run holds nothing back either.
        fs::remove_dir_all(newer).unwrap();
        fs::remove_file(removed.join(REMOVED_BY_USER)).unwrap();
        assert_eq!(only(&state), (vec![standing.clone()], vec![], vec![]));
    }

    #[test]
    fn a_skipped_review_holds_back_every_review_of_its_pr_until_undone() {
        let state = State::new();
        let listed = listed_again();
        let skipped = state.make_run("20310801T150000Z-12", &listed, Some(SKIPPED_BY_USER));
        let standing = with(&item(), json!({"source": "github_request"}));
        let mut new_ask = listed.clone();
        new_ask["slack"]["ts"] = json!("1943198437.000200");
        // Unlike a removal, a new request holds back too: new commits or a new Slack post.
        for polled in [
            standing.clone(),
            listed.clone(),
            new_ask,
            with(&standing, json!({"head_sha": "head4"})),
        ] {
            assert_eq!(
                got(state.select(std::slice::from_ref(&polled), &[])),
                (vec![], dropped(&polled, "skipped by the user"), vec![])
            );
        }
        // The recheck list's item for the PR stays on the list for after the undo.
        assert_eq!(
            got(state.select(&[], std::slice::from_ref(&listed))),
            (vec![], dropped(&listed, "skipped by the user"), vec![])
        );
        state.record("head3.first_review.json", "retry", 0);
        assert_eq!(
            got(state.select(std::slice::from_ref(&standing), &[])),
            (vec![standing.clone()], vec![], vec![])
        );
        state.record("head3.first_review.json", "failed", 1);
        // Undone, as skip --undo leaves it, the run holds nothing back.
        fs::remove_file(skipped.join(SKIPPED_BY_USER)).unwrap();
        assert_eq!(
            got(state.select(&[], std::slice::from_ref(&listed))),
            (vec![listed.clone()], vec![], vec![])
        );
        // Only the PR's newest run counts: a skip of an earlier review holds nothing back.
        state.make_run("20310801T140000Z-12", &listed, Some(SKIPPED_BY_USER));
        assert_eq!(
            got(state.select(std::slice::from_ref(&standing), &[])),
            (vec![standing], vec![], vec![])
        );
    }

    #[test]
    fn failures_add_up_across_runs_and_done_keeps_the_run_start() {
        let state = State::new();
        let path = state
            .path()
            .join("prs/example-org-example-repo-12/head3.first_review.json");
        let read = || fsx::read_json(&path).unwrap();
        write_record(state.path(), &item(), "running").unwrap();
        let first = read()["started_at"].clone();
        assert_eq!(read()["failures"], 0);
        write_record(state.path(), &item(), "failed").unwrap();
        assert_eq!(
            read(),
            json!({"status": "failed", "failures": 1, "started_at": first})
        );
        std::thread::sleep(std::time::Duration::from_millis(1100));
        write_record(state.path(), &item(), "running").unwrap();
        let second = read()["started_at"].clone();
        assert_ne!(second, first);
        assert_eq!(read()["failures"], 1);
        write_record(state.path(), &item(), "done").unwrap();
        assert_eq!(
            read(),
            json!({"status": "done", "failures": 1, "started_at": second})
        );
        // select reads the same record.
        assert!(state.select(&[item()], &[]).work.is_empty());
    }

    #[test]
    fn write_record_waits_for_the_pr_folders_lock() {
        let state = State::new();
        let folder = state.path().join("prs/example-org-example-repo-12");
        fs::create_dir_all(&folder).unwrap();
        let path = folder.join("head3.first_review.json");
        let root = state.path().to_path_buf();
        let writer;
        {
            // Another writer, such as submit marking the head stale, holds the folder's lock and writes
            // the record meanwhile; this write waits, then counts from what it wrote.
            let _held = fsx::folder_lock(&folder).unwrap();
            writer = std::thread::spawn(move || write_record(&root, &item(), "failed").unwrap());
            std::thread::sleep(std::time::Duration::from_millis(500));
            assert!(!writer.is_finished());
            fs::write(
                &path,
                r#"{"status": "running", "failures": 1, "started_at": "2031-08-01T16:00:00+00:00"}"#,
            )
            .unwrap();
        }
        writer.join().unwrap();
        assert_eq!(
            fsx::read_json(&path).unwrap(),
            json!({"status": "failed", "failures": 2, "started_at": "2031-08-01T16:00:00+00:00"})
        );
        // Written through a temporary file, which leaves nothing beside the record.
        let names: Vec<String> = fsx::entries(&folder)
            .iter()
            .map(|p| fsx::name_of(p))
            .collect();
        assert_eq!(names, ["head3.first_review.json"]);
    }

    #[test]
    fn an_unreadable_record_starts_over() {
        let state = State::new();
        let path = state
            .path()
            .join("prs/example-org-example-repo-12/head3.first_review.json");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, r#"{"status": "runn"#).unwrap();
        write_record(state.path(), &item(), "failed").unwrap();
        let record = fsx::read_json(&path).unwrap();
        assert_eq!(record["status"], "failed");
        assert_eq!(record["failures"], 1);
        assert!(record["started_at"].as_str().is_some());
    }
}
