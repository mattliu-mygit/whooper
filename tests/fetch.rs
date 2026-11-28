//! Black-box tests of whooper fetch with a fake gh: the recheck list's PRs its searches miss, which it
//! looks up and select then drops when merged or closed, and the 1,000 PRs a search returns at most.

mod common;

use std::fs;

use common::*;
use serde_json::{Value, json};

/// A fake gh whose search for the user's review requests in example-org prints the URLs in the file
/// requested, at most --limit of them, and whose graphql lookup of a URL prints the PR saved for it, or
/// fails when there is none.
fn fake_gh(w: &Whooper) {
    w.fake(
        "gh",
        r#"case "$*" in
  "search prs user-review-requested:sam-example --owner example-org "*)
    limit=30 previous=""
    for arg; do [ "$previous" = --limit ] && limit="$arg"; previous="$arg"; done
    head -n "$limit" "${0%/*}/requested" ;;
  "search prs "*) ;;
  "api graphql "*) for arg; do case "$arg" in url=https://github.com/*) cat "${0%/*}/$(echo "${arg#url=https://github.com/}" | tr / -).json"; exit ;; esac; done; exit 1 ;;
  *) exit 1 ;;
esac"#,
    );
}

fn save_pr(w: &Whooper, number: i64, state: &str, requested: &[&str]) -> Value {
    let mut p = pr(number, requested, vec![]);
    p["state"] = json!(state);
    fs::write(
        w.fakes
            .join(format!("example-org-example-repo-pull-{number}.json")),
        p.to_string(),
    )
    .unwrap();
    p
}

fn list_for_recheck(w: &Whooper, numbers: &[i64]) {
    let rechecks = w.state.join("rechecks");
    fs::create_dir_all(&rechecks).unwrap();
    for &n in numbers {
        let item = work_item("example-org/example-repo", n, &format!("{n:0>40}"));
        fs::write(
            rechecks.join(format!("example-org-example-repo-{n}.json")),
            item.to_string(),
        )
        .unwrap();
    }
}

fn numbers(path: &std::path::Path) -> Vec<(i64, String)> {
    read_json(path)
        .as_array()
        .unwrap()
        .iter()
        .map(|p| {
            (
                p["number"].as_i64().unwrap(),
                p["state"].as_str().unwrap().to_string(),
            )
        })
        .collect()
}

fn select(w: &Whooper) -> (Vec<i64>, String) {
    fs::write(
        w.state.join("poll/20310727T020000Z.json"),
        r#"{"items": []}"#,
    )
    .unwrap();
    let out = w.run(&["select"]);
    assert!(out.status.success());
    let prs = String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(|l| {
            serde_json::from_str::<Value>(l).unwrap()["pr"]
                .as_i64()
                .unwrap()
        })
        .collect();
    (prs, String::from_utf8_lossy(&out.stderr).into_owned())
}

fn listed(w: &Whooper) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(w.state.join("rechecks"))
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

#[test]
fn the_fetch_looks_up_the_recheck_lists_prs_its_searches_miss_and_select_drops_merged_and_closed_ones()
 {
    let w = Whooper::new();
    fake_gh(&w);
    // The searches find PR 12, whose review request stands. The recheck list holds PRs 12 to 15, and
    // GitHub reports PR 13 merged, PR 14 closed, and PR 15 open.
    let twelve = save_pr(&w, 12, "OPEN", &[LOGIN]);
    fs::write(
        w.fakes.join("requested"),
        format!("{}\n", twelve["url"].as_str().unwrap()),
    )
    .unwrap();
    for (n, state) in [(13, "MERGED"), (14, "CLOSED"), (15, "OPEN")] {
        save_pr(&w, n, state, &[]);
    }
    list_for_recheck(&w, &[12, 13, 14, 15]);
    w.ok(&["fetch"]);
    let poll = w.state.join("poll");
    assert_eq!(numbers(&poll.join("github.json")), [(12, "OPEN".into())]);
    // The searches list open PRs only, so only the PRs they did not find are looked up.
    assert_eq!(
        numbers(&poll.join("recheck-prs.json")),
        [
            (13, "MERGED".into()),
            (14, "CLOSED".into()),
            (15, "OPEN".into())
        ]
    );
    let (prs, err) = select(&w);
    assert_eq!(prs, [12, 15], "{err}");
    assert_eq!(
        err,
        "whooper-select: dropped example-org/example-repo#13 first_review: merged on GitHub\nwhooper-select: dropped example-org/example-repo#14 first_review: closed on GitHub\n"
    );
    assert_eq!(
        listed(&w),
        [
            "example-org-example-repo-12.json",
            "example-org-example-repo-15.json"
        ]
    );
    // With the list empty, the fetch looks up nothing.
    fs::remove_dir_all(w.state.join("rechecks")).unwrap();
    w.ok(&["fetch"]);
    assert_eq!(read_json(&poll.join("recheck-prs.json")), json!([]));
}

#[test]
fn a_recheck_list_pr_the_fetch_cannot_look_up_stays_listed_and_fails_no_round() {
    let w = Whooper::new();
    fake_gh(&w);
    let twelve = save_pr(&w, 12, "OPEN", &[LOGIN]);
    fs::write(
        w.fakes.join("requested"),
        format!("{}\n", twelve["url"].as_str().unwrap()),
    )
    .unwrap();
    // GitHub reports PR 13 merged, and the lookup of PR 16 fails, as for a PR in an org the token may
    // no longer read.
    save_pr(&w, 13, "MERGED", &[]);
    list_for_recheck(&w, &[13, 16]);
    let out = w.run(&["fetch"]);
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(out.status.success(), "{err}");
    assert!(
        err.ends_with("whooper-fetch: could not look up https://github.com/example-org/example-repo/pull/16 on the recheck list, so it stays listed\n"),
        "{err}"
    );
    let poll = w.state.join("poll");
    assert_eq!(numbers(&poll.join("github.json")), [(12, "OPEN".into())]);
    assert_eq!(
        numbers(&poll.join("recheck-prs.json")),
        [(13, "MERGED".into())]
    );
    // select drops the merged PR and keeps PR 16, whose state nobody knows, for a review.
    let (prs, err) = select(&w);
    assert_eq!(prs, [16], "{err}");
    assert_eq!(listed(&w), ["example-org-example-repo-16.json"]);
}

#[test]
fn the_fetch_reads_every_pr_its_searches_find_up_to_the_1000_github_returns() {
    let w = Whooper::new();
    // The fake logs each PR the fetch reads and prints nothing for it. It runs in bash itself, which
    // starts no other program for each of the 1,150 lookups.
    let read = w.fakes.join("read");
    fs::write(
        w.fakes.join("gh"),
        r#"#!/bin/bash
case "$*" in
  "search prs user-review-requested:sam-example --owner example-org "*)
    limit=30 previous=""
    for arg; do [ "$previous" = --limit ] && limit="$arg"; previous="$arg"; done
    head -n "$limit" "${0%/*}/requested" ;;
  "search prs "*) ;;
  "api graphql "*) for arg; do case "$arg" in url=*) echo "${arg#url=}" >> "${0%/*}/read" ;; esac; done ;;
  *) exit 1 ;;
esac
"#,
    )
    .unwrap();
    // fs::write keeps the mode of the fake gh Whooper::new made.
    let urls: Vec<String> = (1..=1200)
        .map(|n| format!("https://github.com/example-org/example-repo/pull/{n}"))
        .collect();
    let write_requested = |n: usize| {
        fs::write(
            w.fakes.join("requested"),
            urls[..n]
                .iter()
                .map(|u| format!("{u}\n"))
                .collect::<String>(),
        )
        .unwrap()
    };
    let read_urls = || {
        let mut found: Vec<String> = fs::read_to_string(&read)
            .unwrap()
            .lines()
            .map(String::from)
            .collect();
        found.sort();
        found
    };
    let sorted = |n: usize| {
        let mut want = urls[..n].to_vec();
        want.sort();
        want
    };
    write_requested(150);
    let out = w.run(&["fetch"]);
    assert_eq!(
        (
            out.status.code(),
            String::from_utf8_lossy(&out.stderr).into_owned()
        ),
        (Some(0), String::new())
    );
    assert_eq!(read_urls(), sorted(150));
    // 1,000 is the most GitHub returns for one search, so a search that reaches it says it may have
    // left PRs out.
    write_requested(1200);
    fs::remove_file(&read).unwrap();
    let out = w.run(&["fetch"]);
    assert_eq!(
        (out.status.code(), String::from_utf8_lossy(&out.stderr).into_owned()),
        (Some(0), "whooper-fetch: the search for PRs that request sam-example's review in example-org found 1,000 PRs, the most GitHub returns for one search, so it may have left some out\n".to_string())
    );
    assert_eq!(read_urls(), sorted(1000));
}
