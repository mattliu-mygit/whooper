//! The user's drafts for a review, and whooper-submit, the only command that writes to GitHub.

use std::fs;
use std::path::Path;

use regex::Regex;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::fail;
use crate::fsx::{self, Exit, Object, Result, read_object, str_of};
use crate::status::{findings_schema, run_dir, running_runs};
use crate::{gh, work};

/// whooper-submit's exit codes for the refusals Prompting Time handles each its own way.
pub const NEW_COMMITS_EXIT: i32 = 10;
pub const NOT_SAFE_EXIT: i32 = 11;
pub const MENTIONS_EXIT: i32 = 12;
pub const CHANGED_EXIT: i32 = 13;
pub const OUTSIDE_DIFF_EXIT: i32 = 14;
/// Each comment whooper-submit posts starts with this line and a blank line: an AI wrote the comment,
/// and the user read it and chose to post it.
const VETTED: &str = "AI comment (vetted)";
/// The message of an approval the user left without one, when it includes comments.
const PRE_APPROVAL: &str = "Pre-approving to unblock";
pub const DRAFT_USAGE: &str = "usage: whooper-draft <run-id> [include <finding-id> | leave-out <finding-id> | text <finding-id> <text> | approve | comment | message <text>]";

/// The @-mentions in text, such as @alice or the team @example-org/reviewers. A handle preceded by a
/// word character, a dot, an @, or a slash, as in an email address or a path, is none.
pub fn mentions(text: &str) -> Vec<String> {
    let re = Regex::new(r"(?:^|[^\w.@/])(@[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9_-]+)?)").unwrap();
    let mut found = Vec::new();
    let mut at = 0;
    while let Some(m) = re.captures_at(text, at) {
        let handle = m.get(1).unwrap();
        found.push(handle.as_str().to_string());
        at = handle.end();
    }
    found
}

/// The findings.json of the review in run, for whooper-<command>, draft or submit, which read its
/// fields without checking each one. Refuses a review that did not finish, which left no findings.json
/// or one that does not fit the schema.
pub fn read_valid_findings(run: &Path, command: &str) -> Result<Value> {
    let Ok(bytes) = fs::read(run.join("findings.json")) else {
        fail!(
            "whooper-{command}: {} has no findings.json, so its review did not finish",
            run.display()
        )
    };
    let unfit = |e: String| {
        Exit::new(
            1,
            format!(
                "whooper-{command}: {} has a findings.json that does not fit schemas/findings.json, so its review did not finish: {e}",
                run.display()
            ),
        )
    };
    let result: Value = serde_json::from_slice(&bytes).map_err(|e| unfit(e.to_string()))?;
    crate::schema::check(&result, &findings_schema()?, "findings.json").map_err(unfit)?;
    Ok(result)
}

/// The user's choices for a review run, from drafts.json: the review's event, APPROVE or COMMENT, its
/// message, and per finding ID whether its comment is included and its edited text. Until the user
/// changes anything, a run that run_concerns finds nothing against starts as an approval with every
/// finding included, and any other as a comment with nothing included.
pub fn read_drafts(run: &Path) -> Result<Value> {
    if let Some(drafts) = fsx::read_json(&run.join("drafts.json")) {
        return Ok(drafts);
    }
    let result: Value = serde_json::from_slice(&fs::read(run.join("findings.json"))?)?;
    if !run_concerns(run, &result).is_empty() {
        return Ok(json!({"event": "COMMENT", "message": "", "findings": {}}));
    }
    let included: Object = result["findings"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|f| {
            (
                f["id"].as_str().unwrap_or_default().to_string(),
                json!({"included": true}),
            )
        })
        .collect();
    Ok(json!({"event": "APPROVE", "message": "", "findings": included}))
}

/// What argues against approving the PR: a review that failed, with failure.txt's reason; a review
/// the run skipped; a verdict other than approve_once_ci_passes; and each finding that is not a nit.
pub fn run_concerns(run: &Path, result: &Value) -> Vec<String> {
    let mut concerns = Vec::new();
    if let Some(text) = fsx::read_text(&run.join("failure.txt")) {
        concerns.push(format!("the review failed: {}", text.trim()));
    }
    let approval = &result["approval"];
    if result["status"] == "skipped" {
        concerns.push(format!(
            "the review was skipped: {}",
            result["skip_reason"].as_str().unwrap_or_default()
        ));
    }
    if approval["verdict"] != "approve_once_ci_passes" {
        concerns.push(format!(
            "the run's verdict is {}: {}",
            approval["verdict"].as_str().unwrap_or_default(),
            approval["reason"].as_str().unwrap_or_default()
        ));
    }
    for f in result["findings"].as_array().into_iter().flatten() {
        if f["kind"] != "nit" {
            concerns.push(format!(
                "finding {} is a {}: {} ({}:{})",
                f["id"].as_str().unwrap_or_default(),
                f["kind"].as_str().unwrap_or_default(),
                f["title"].as_str().unwrap_or_default(),
                f["path"].as_str().unwrap_or_default(),
                f["line"]
            ));
        }
    }
    concerns
}

/// whooper-draft: prints a run's drafts, or makes one change to them and prints the result.
pub fn draft(state: &Path, args: &[String]) -> Result<()> {
    let Some(run_id) = args.first() else {
        fail!("{DRAFT_USAGE}")
    };
    let run = run_dir(state, run_id, "whooper-draft")?;
    if !run.join("run.json").exists() {
        fail!(
            "whooper-draft: no run {run_id} in {}",
            run.parent().unwrap().display()
        );
    }
    read_valid_findings(&run, "draft")?;
    let drafts = if args.len() > 1 {
        change_drafts(&run, &args[1], &args[2..])?
    } else {
        read_drafts(&run)?
    };
    println!("{}", fsx::pretty(&drafts));
    Ok(())
}

/// Makes one change to a run's drafts and saves them under the run folder's lock, since the page,
/// Whooper's agent, and a terminal can each change one run's drafts at the same moment. After the
/// review is submitted a change would not reach GitHub, so it refuses.
fn change_drafts(run: &Path, change: &str, args: &[String]) -> Result<Value> {
    let takes = match change {
        "include" | "leave-out" | "message" => 1,
        "text" => 2,
        "approve" | "comment" => 0,
        _ => usize::MAX,
    };
    if takes != args.len() {
        fail!("{DRAFT_USAGE}");
    }
    if run.join("submit.json").exists() {
        fail!(
            "whooper-draft: {} was already submitted; see its submit.json",
            run.display()
        );
    }
    if change == "text" && args[1].trim().is_empty() {
        fail!("whooper-draft: the text is empty; to drop the comment, leave the finding out");
    }
    let _lock = fsx::folder_lock(run)?;
    let mut drafts = read_drafts(run)?;
    match change {
        "include" | "leave-out" | "text" => {
            let findings: Value = serde_json::from_slice(&fs::read(run.join("findings.json"))?)?;
            let ids: Vec<&str> = findings["findings"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|f| f["id"].as_str())
                .collect();
            if !ids.contains(&args[0].as_str()) {
                let listed = if ids.is_empty() {
                    "none".to_string()
                } else {
                    ids.join(", ")
                };
                fail!(
                    "whooper-draft: the run has no finding {}; its findings are {listed}",
                    args[0]
                );
            }
            let choices = drafts["findings"]
                .as_object_mut()
                .expect("drafts hold findings");
            let choice = choices
                .entry(args[0].clone())
                .or_insert_with(|| json!({"included": false}));
            if change == "text" {
                choice["text"] = json!(args[1]);
            } else {
                choice["included"] = json!(change == "include");
            }
        }
        "message" => drafts["message"] = json!(args[0]),
        _ => drafts["event"] = json!(change.to_uppercase()),
    }
    fsx::write_file(&run.join("drafts.json"), &fsx::pretty(&drafts), false)?;
    Ok(drafts)
}

/// The comments whooper-submit posts, as (finding, body) pairs in findings.json's order: one for each
/// finding the drafts include. A body is VETTED, a blank line, and the user's text or else the
/// finding's draft, opened with its Conventional Comments label, or its kind's when it has none.
fn vetted_comments(findings: &Value, drafts: &Value) -> Vec<(Value, String)> {
    let label = Regex::new(r"^\*\*[A-Za-z]+(?: \([^)]*\))?:\*\*").unwrap();
    let mut comments = Vec::new();
    for f in findings.as_array().into_iter().flatten() {
        let choice = &drafts["findings"][f["id"].as_str().unwrap_or_default()];
        if choice["included"] != true {
            continue;
        }
        let mut text = choice["text"]
            .as_str()
            .or(f["draft"].as_str())
            .unwrap_or_default()
            .trim()
            .to_string();
        if !label.is_match(&text) {
            let kind = match f["kind"].as_str() {
                Some("caller_visible_bug" | "ci_failure") => "**issue:**",
                Some("nit") => "**nit:**",
                Some("question") => "**question:**",
                _ => "**suggestion:**",
            };
            text = format!("{kind} {text}");
        }
        comments.push((f.clone(), format!("{VETTED}\n\n{text}")));
    }
    comments
}

fn refuse(code: i32, message: String) -> Exit {
    Exit::new(code, message)
}

/// The review whooper-submit would post from run, after the checks that need nothing from GitHub. It
/// refuses a review a live whooper-review still runs, a run whose review was submitted or is posting,
/// a review that did not finish, and, with OUTSIDE_DIFF_EXIT, one that includes a comment on a line
/// outside the PR's diff, where GitHub takes no comment, judged from the run's diff.patch.
/// A review's findings, drafts, and the comments it would post.
type ToSubmit = (Value, Value, Vec<(Value, String)>);

fn review_to_submit(state: &Path, run: &Path) -> Result<ToSubmit> {
    if running_runs(state).contains_key(&fsx::name_of(run)) {
        fail!("whooper-submit: the review is still running; submit it once it ends");
    }
    if run.join("submit.json").exists() {
        let Some(submit) = read_object(&run.join("submit.json")) else {
            fail!(
                "whooper-submit: {} has a submit.json that cannot be read, so its review may be on GitHub; check the PR, then delete submit.json to submit again",
                run.display()
            )
        };
        if str_of(&submit, "status") == Some("posting") {
            fail!(
                "whooper-submit: another submit of {} is posting or did not finish, so its review may be on GitHub; check the PR, then delete submit.json to submit again",
                run.display()
            );
        }
        fail!(
            "whooper-submit: {} was already submitted; see its submit.json",
            run.display()
        );
    }
    let result = read_valid_findings(run, "submit")?;
    let drafts = read_drafts(run)?;
    let comments = vetted_comments(&result["findings"], &drafts);
    let files =
        crate::diff::diff_files(&fsx::read_text(&run.join("diff.patch")).unwrap_or_default());
    let outside: Vec<&Value> = comments
        .iter()
        .map(|(f, _)| f)
        .filter(|f| {
            !crate::diff::in_diff(
                &files,
                f["path"].as_str().unwrap_or_default(),
                f["side"].as_str().unwrap_or("RIGHT"),
                f["line"].as_i64().unwrap_or(0),
            )
        })
        .collect();
    if !outside.is_empty() {
        let listed: String = outside
            .iter()
            .map(|f| {
                format!(
                    "  finding {}: {} ({}:{})\n",
                    f["id"].as_str().unwrap_or_default(),
                    f["title"].as_str().unwrap_or_default(),
                    f["path"].as_str().unwrap_or_default(),
                    f["line"]
                )
            })
            .collect();
        return Err(refuse(
            OUTSIDE_DIFF_EXIT,
            format!(
                "whooper-submit: nothing posted: these included comments are on lines outside the PR's diff, where GitHub takes no comment, so it would refuse the whole review:\n{listed}Leave them out with bin/whooper-draft, or make their point in the review's message."
            ),
        ));
    }
    Ok((result, drafts, comments))
}

/// The flags whooper-submit takes.
pub struct SubmitFlags {
    pub print: bool,
    pub allow_mentions: bool,
    pub approve_anyway: bool,
    pub expect: Option<String>,
}

/// whooper-submit: checks the review, reads the PR's head, refuses with the exit codes Prompting Time
/// tells apart, writes submit-request.json, and unless --print, claims submit.json saying posting,
/// posts the request with gh, and marks submit.json posted with GitHub's response.
pub fn submit(state: &Path, run_id: &str, flags: &SubmitFlags) -> Result<()> {
    let given = run_dir(state, run_id, "whooper-submit")?;
    let run = fs::canonicalize(&given).unwrap_or(given.clone());
    if !run.join("run.json").exists() {
        fail!(
            "whooper-submit: no run {run_id} in {}",
            state.join("runs").display()
        );
    }
    let Some(item) = read_object(&run.join("run.json"))
        .filter(|i| i.contains_key("repo") && i.contains_key("pr"))
    else {
        fail!(
            "whooper-submit: cannot read {}",
            given.join("run.json").display()
        )
    };
    let (repo, pr) = (
        str_of(&item, "repo").unwrap_or_default().to_string(),
        item["pr"].to_string(),
    );
    review_to_submit(state, &run)?;
    let head_now = gh::head(&repo, &pr)?;
    let (result, drafts, comments) = review_to_submit(state, &run)?;
    let head = str_of(&item, "head_sha").unwrap_or_default();
    if head_now != head {
        work::mark_new_commits(state, &item, &head_now)?;
        return Err(refuse(
            NEW_COMMITS_EXIT,
            format!(
                "whooper-submit: nothing posted: the PR has new commits since the review (head {}, now {}). Whooper reviews them in its next round; bin/whooper-round runs that round now.",
                &head[..head.len().min(12)],
                &head_now[..head_now.len().min(12)]
            ),
        ));
    }
    let event = drafts["event"].as_str().unwrap_or("COMMENT");
    let mut message = drafts["message"].as_str().unwrap_or_default().to_string();
    if comments.is_empty() && event == "COMMENT" && message.trim().is_empty() {
        fail!(
            "whooper-submit: nothing to submit: the drafts include no comment, have no message, and do not approve; change them with bin/whooper-draft"
        );
    }
    if event == "APPROVE" && message.trim().is_empty() {
        message = if comments.is_empty() {
            String::new()
        } else {
            PRE_APPROVAL.into()
        };
    }
    if event == "APPROVE" && !flags.approve_anyway {
        let concerns = run_concerns(&run, &result);
        if !concerns.is_empty() {
            let listed: String = concerns.iter().map(|c| format!("  {c}\n")).collect();
            return Err(refuse(
                NOT_SAFE_EXIT,
                format!(
                    "whooper-submit: nothing posted: the review approves, and the run is not safe to approve:\n{listed}Pass --approve-anyway to approve it anyway, or make the review only comment with bin/whooper-draft."
                ),
            ));
        }
    }
    let mut found = Vec::new();
    for (f, body) in &comments {
        for m in mentions(body) {
            found.push(format!(
                "{m} in the comment on {}:{} (finding {})",
                f["path"].as_str().unwrap_or_default(),
                f["line"],
                f["id"].as_str().unwrap_or_default()
            ));
        }
    }
    found.extend(
        mentions(&message)
            .into_iter()
            .map(|m| format!("{m} in the message")),
    );
    if !found.is_empty() && !flags.allow_mentions {
        let listed: String = found.iter().map(|m| format!("  {m}\n")).collect();
        return Err(refuse(
            MENTIONS_EXIT,
            format!(
                "whooper-submit: nothing posted: the review @-mentions someone, and posting would notify them:\n{listed}Pass --allow-mentions to post it anyway."
            ),
        ));
    }
    let request = json!({
        "commit_id": head,
        "event": event,
        "body": message,
        "comments": comments.iter().map(|(f, text)| json!({"path": f["path"], "line": f["line"], "side": f["side"], "body": text})).collect::<Vec<_>>(),
    });
    // The fingerprint names these exact bytes, so the submit Prompting Time runs after its
    // confirmation, with --expect, posts nothing the user did not see.
    let text = fsx::pretty(&request);
    let fingerprint = format!("{:x}", Sha256::digest(text.as_bytes()));
    if let Some(expect) = &flags.expect
        && *expect != fingerprint
    {
        return Err(refuse(
            CHANGED_EXIT,
            format!(
                "whooper-submit: nothing posted: the drafts changed since --print showed the review, so its fingerprint is now {fingerprint}, not {expect}. Run with --print to show it again."
            ),
        ));
    }
    let request_file = run.join("submit-request.json");
    fsx::write_file(&request_file, &text, false)?;
    if flags.print {
        println!(
            "printed the request to {}; nothing posted",
            request_file.display()
        );
        println!("fingerprint: {fingerprint}");
        return Ok(());
    }
    // Only one submit can create submit.json, so of two at once only one posts.
    let claim = json!({"status": "posting", "request": request});
    if fsx::write_file(&run.join("submit.json"), &fsx::pretty(&claim), true).is_err() {
        fail!(
            "whooper-submit: {} was already submitted; see its submit.json",
            run.display()
        );
    }
    match gh::post_review(&repo, &pr, &request) {
        Ok(response) => {
            let posted = json!({"status": "posted", "request": request, "response": response});
            fsx::write_file(&run.join("submit.json"), &fsx::pretty(&posted), false)?;
            println!(
                "posted the review: {}",
                response["html_url"].as_str().unwrap_or_default()
            );
            Ok(())
        }
        Err(printed) => {
            let _ = fs::remove_file(run.join("submit.json"));
            fail!(
                "whooper-submit: posting failed; unless GitHub answered with an error, check the PR before submitting again:\n{printed}"
            )
        }
    }
}
