//! Finding the user's review work: the GitHub fetch (fetch), the Slack poll (poll), and the work items
//! they make (poll_items).
//!
//! The fetch writes <state>/poll/github.json, every open PR in the configured orgs whose review
//! requests name the user, as queries/pr.graphql returns it, and <state>/poll/recheck-prs.json, the
//! recheck list's PRs that search did not find, which select drops when merged or closed.
//!
//! The poll reads the ask channel with slackdump through the user's Slack web login, from
//! <state>/poll/cursor (the last 24 hours without one), and the agent the config's [jobs] poll names
//! judges which posts ask the user for a review. Its answer is checked against the posts, the PRs they
//! link are fetched, and the items go to <state>/poll/<UTC start time>.json. The cursor moves to the
//! poll's start only after the output is written. Read-only toward GitHub and Slack.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;

use jiff::{Timestamp, tz::TimeZone};
use regex::Regex;
use serde_json::{Value, json};

use crate::config::{self, Whooper, table};
use crate::fail;
use crate::fsx::{self, Exit, Object, Result, str_of};
use crate::proc::{self, Ended};

/// An untagged team ask leaves a PR to whoever else reviewed it in the last 8 hours.
const CLAIM_HOURS: i64 = 8;
/// The most PRs GitHub returns for one search, and the highest --limit gh takes.
const SEARCH_LIMIT: usize = 1000;

fn is_ts(text: &str) -> bool {
    text.len() == 17
        && text.as_bytes()[10] == b'.'
        && text
            .bytes()
            .enumerate()
            .all(|(i, b)| i == 10 || b.is_ascii_digit())
}

/// The posts and thread replies of a channel slackdump dumped that are new since oldest, a Unix time:
/// posted or last edited at or after it. The dump starts a day before oldest, so replies and edits under
/// older posts come along. Each keeps its ts, the ts of the post it replies to (thread_ts), its sender,
/// whether a bot wrote it, its text with the text of any message it shares, and its reactions' names.
pub fn slack_posts(dump: &Value, oldest: i64) -> Vec<Value> {
    let new = |m: &Value| {
        let edited = m["edited"]["ts"].as_str().unwrap_or("");
        [m["ts"].as_str().unwrap_or(""), edited]
            .iter()
            .any(|t| is_ts(t) && t[..10].parse::<i64>().unwrap_or(0) >= oldest)
    };
    let post = |m: &Value, thread: Option<&str>| {
        let mut p = Object::new();
        p.insert("ts".into(), m["ts"].clone());
        if let Some(thread) = thread {
            p.insert("thread_ts".into(), json!(thread));
        }
        let user = m["user"].as_str().unwrap_or("");
        p.insert("user".into(), json!(user));
        let bot = m["bot_id"].as_str().is_some_and(|b| !b.is_empty())
            || m["subtype"] == "bot_message"
            || user == "USLACKBOT";
        p.insert("bot".into(), json!(bot));
        let mut texts = vec![m["text"].as_str().unwrap_or("").to_string()];
        for a in m["attachments"].as_array().into_iter().flatten() {
            if let Some(t) = a["text"].as_str().filter(|t| !t.is_empty()) {
                texts.push(t.to_string());
            }
        }
        p.insert("text".into(), json!(texts.join("\n")));
        let reactions: Vec<Value> = m["reactions"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|r| r["name"].clone())
            .collect();
        p.insert("reactions".into(), Value::Array(reactions));
        Value::Object(p)
    };
    let mut posts = vec![];
    for m in dump["messages"].as_array().into_iter().flatten() {
        let Some(ts) = m["ts"].as_str().filter(|t| is_ts(t)) else {
            continue;
        };
        if new(m) {
            posts.push(post(m, None));
        }
        for r in m["slackdump_thread_replies"]
            .as_array()
            .into_iter()
            .flatten()
        {
            let rts = r["ts"].as_str().unwrap_or("");
            if rts != ts && is_ts(rts) && new(r) {
                posts.push(post(r, Some(ts)));
            }
        }
    }
    posts
}

/// Matches a plain pull request URL in one of orgs, with the link without its scheme as group 1. Slack
/// text is untrusted, so only these URLs reach a gh command. The number has at most 9 digits.
pub struct PrLink(Regex);

impl PrLink {
    pub fn new(orgs: &[String]) -> PrLink {
        let names: Vec<String> = orgs.iter().map(|o| regex::escape(o)).collect();
        PrLink(
            Regex::new(&format!(
                r"https?://(github\.com/(?:{})/[A-Za-z0-9._-]+/pull/([1-9][0-9]*))",
                names.join("|")
            ))
            .unwrap(),
        )
    }

    /// The links in text, without their scheme.
    pub fn find<'t>(&self, text: &'t str) -> Vec<&'t str> {
        self.0
            .captures_iter(text)
            .filter(|c| c[2].len() <= 9)
            .map(|c| c.get(1).unwrap().as_str())
            .collect()
    }
}

/// The review asks a poll run reported, and a note for each ask or URL it dropped. posts are the ask
/// channel's posts new since oldest, and answer the run's {asks, not_asks}. Fails when the run left out
/// a post, not the user's, that mentions the user next to a link to a PR in orgs. An ask keeps only the
/// URLs of PRs in orgs that its post links; whether the post mentions the user and has :eyes: is read
/// from the post.
pub fn read_asks(
    posts: &[Value],
    answer: &Value,
    oldest: i64,
    channel: &str,
    user: &str,
    slack_url: &str,
    orgs: &[String],
) -> std::result::Result<(Vec<Value>, Vec<String>), String> {
    let by_ts: HashMap<&str, &Value> = posts
        .iter()
        .map(|p| (p["ts"].as_str().unwrap_or(""), p))
        .collect();
    let mention = Regex::new(&format!(r"<@{}[|>]", regex::escape(user))).unwrap();
    let link = PrLink::new(orgs);
    let list = |key: &str| answer[key].as_array().cloned().unwrap_or_default();
    let answered: HashSet<String> = list("asks")
        .iter()
        .chain(list("not_asks").iter())
        .filter_map(|a| a["ts"].as_str().map(str::to_string))
        .collect();
    let missed: Vec<&str> = posts
        .iter()
        .filter(|p| {
            let text = p["text"].as_str().unwrap_or("");
            let ts = p["ts"].as_str().unwrap_or("");
            !answered.contains(ts)
                && mention.is_match(text)
                && !link.find(text).is_empty()
                && p["user"].as_str() != Some(user)
        })
        .map(|p| p["ts"].as_str().unwrap_or(""))
        .collect();
    if !missed.is_empty() {
        return Err(format!(
            "the poll run left out posts that mention {user} next to a PR link: {}",
            missed.join(", ")
        ));
    }
    let (mut asks, mut problems) = (vec![], vec![]);
    for ask in list("asks") {
        let ts = ask["ts"].as_str().unwrap_or("");
        let Some(post) = by_ts.get(ts) else {
            problems.push(format!(
                "dropped ask '{ts}': not a post in the channel since {oldest}"
            ));
            continue;
        };
        let text = post["text"].as_str().unwrap_or("");
        let linked: HashSet<String> = link
            .find(text)
            .into_iter()
            .map(|l| format!("https://{l}"))
            .collect();
        let wanted: Vec<&str> = ask["pr_urls"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect();
        let urls: Vec<&str> = wanted
            .iter()
            .copied()
            .filter(|u| linked.contains(*u))
            .collect();
        for url in wanted.iter().filter(|u| !linked.contains(**u)) {
            problems.push(format!("dropped PR URL '{url}' from ask {ts}"));
        }
        if urls.is_empty() {
            problems.push(format!("dropped ask {ts}: no PR URL left"));
            continue;
        }
        let mut permalink = format!("{slack_url}/archives/{channel}/p{}", ts.replace('.', ""));
        if let Some(thread) = post["thread_ts"].as_str() {
            permalink.push_str(&format!("?thread_ts={thread}&cid={channel}"));
        }
        let reactions = post["reactions"].as_array().cloned().unwrap_or_default();
        asks.push(json!({
            "ts": ts,
            "permalink": permalink,
            "tagged_user": mention.is_match(text),
            "pr_urls": urls,
            "has_eyes": reactions.iter().any(|r| r == "eyes"),
            "reason": ask["reason"],
        }));
    }
    Ok((asks, problems))
}

/// A GraphQL connection's nodes. Fails when the query's page size cut some off.
pub fn nodes(connection: &Value) -> std::result::Result<Vec<Value>, String> {
    let list = connection["nodes"].as_array().cloned().unwrap_or_default();
    let total = connection["totalCount"].as_u64().unwrap_or(0) as usize;
    if total > list.len() {
        return Err(format!("{total} nodes exceed one page of {}", list.len()));
    }
    Ok(list)
}

/// The user's submitted reviews of pr, ordered by submission. A pending review is not submitted, and a
/// thread reply, which GitHub records as a COMMENTED review with no body and one comment that replies
/// to another, is no review.
pub fn user_reviews(pr: &Value, login: &str) -> std::result::Result<Vec<Value>, String> {
    let mut mine: Vec<Value> = nodes(&pr["reviews"])?
        .into_iter()
        .filter(|r| {
            let comments = &r["comments"];
            let reply = r["state"] == "COMMENTED"
                && r["body"].as_str().unwrap_or("").is_empty()
                && comments["totalCount"] == 1
                && !comments["nodes"][0]["replyTo"].is_null();
            r["author"]["login"] == login && r["state"] != "PENDING" && !reply
        })
        .collect();
    mine.sort_by(|a, b| {
        a["submittedAt"]
            .as_str()
            .unwrap_or("")
            .cmp(b["submittedAt"].as_str().unwrap_or(""))
    });
    Ok(mine)
}

/// Whether someone other than the PR's author and bot accounts submitted a review of pr at or after
/// since.
fn human_reviewed_since(pr: &Value, since: Timestamp) -> std::result::Result<bool, String> {
    let author = &pr["author"]["login"];
    Ok(nodes(&pr["reviews"])?.iter().any(|r| {
        r["state"] != "PENDING"
            && r["author"]["__typename"] != "Bot"
            && &r["author"]["login"] != author
            && r["submittedAt"]
                .as_str()
                .and_then(fsx::parse_time)
                .is_some_and(|t| t >= since)
    }))
}

fn work_item(pr: &Value, source: &str, reason: &Value, slack: Value) -> Value {
    json!({
        "kind": "first_review",
        "repo": pr["repository"]["nameWithOwner"],
        "pr": pr["number"],
        "head_sha": pr["headRefOid"],
        "base_ref": pr["baseRefName"],
        "source": source,
        "slack": slack,
        "reason": reason,
    })
}

/// The review work in GitHub's review requests and the Slack asks, one item per PR. Whooper reviews a
/// PR the user has not reviewed yet: a request by name for a PR the user already reviewed, or an ask
/// about one, makes no item. github_prs come from the fetch's search, slack_prs are the PRs the asks
/// link, and now dates the window in which an untagged team ask leaves a PR to whoever else reviewed it.
/// When sources overlap, a tagged ask wins because it carries the post, then a request by name, which
/// lets the review run skip a large PR, then an untagged ask.
pub fn poll_items(
    login: &str,
    github_prs: &[Value],
    asks: &[Value],
    slack_prs: &[Value],
    now: Timestamp,
) -> std::result::Result<Vec<Value>, String> {
    let mut found = vec![];
    for pr in github_prs {
        let requested = nodes(&pr["reviewRequests"])?
            .iter()
            .any(|r| r["requestedReviewer"]["login"] == login);
        if requested && user_reviews(pr, login)?.is_empty() {
            let reason =
                format!("A GitHub review request names {login}, who has not reviewed this PR.");
            found.push(work_item(pr, "github_request", &json!(reason), Value::Null));
        }
    }
    let by_url: HashMap<&str, &Value> = slack_prs
        .iter()
        .map(|p| (p["url"].as_str().unwrap_or(""), p))
        .collect();
    let since = now - jiff::SignedDuration::from_hours(CLAIM_HOURS);
    for ask in asks {
        for url in ask["pr_urls"].as_array().into_iter().flatten() {
            let Some(pr) = by_url.get(url.as_str().unwrap_or("")) else {
                continue;
            };
            if pr["state"] != "OPEN" || pr["author"]["login"] == login {
                continue;
            }
            let tagged = ask["tagged_user"] == true;
            if !tagged && (ask["has_eyes"] == true || human_reviewed_since(pr, since)?) {
                continue;
            }
            if !user_reviews(pr, login)?.is_empty() {
                continue;
            }
            let slack = json!({"ts": ask["ts"], "tagged_user": ask["tagged_user"], "permalink": ask["permalink"]});
            found.push(work_item(pr, "slack_ask", &ask["reason"], slack));
        }
    }
    let rank = |item: &Value| match &item["slack"] {
        Value::Null => {
            if item["source"] == "github_request" {
                1
            } else {
                3
            }
        }
        slack => {
            if slack["tagged_user"] == true {
                0
            } else {
                2
            }
        }
    };
    found.sort_by_key(rank);
    let mut seen = HashSet::new();
    Ok(found
        .into_iter()
        .filter(|i| {
            seen.insert((
                i["repo"].to_string(),
                i["pr"].to_string(),
                i["kind"].to_string(),
            ))
        })
        .collect())
}

/// The pull requests at urls as queries/pr.graphql returns them; a URL that names no PR is left out.
/// Fails when gh does.
pub fn pull_requests<'a>(urls: impl IntoIterator<Item = &'a str>) -> Result<Vec<Value>> {
    let mut prs = vec![];
    for url in urls {
        if let Some(pr) = crate::gh::pull_request(url)? {
            prs.push(pr);
        }
    }
    Ok(prs)
}

/// whooper fetch: writes poll/github.json and poll/recheck-prs.json.
pub fn fetch(whooper: &Whooper) -> Result<()> {
    let poll = whooper.state.join("poll");
    fs::create_dir_all(&poll)?;
    let mut urls = BTreeSet::new();
    for org in &whooper.orgs {
        let out = Command::new("gh")
            .args([
                "search",
                "prs",
                &format!("user-review-requested:{}", whooper.login),
                "--owner",
                org,
                "--state",
                "open",
                "--limit",
                &SEARCH_LIMIT.to_string(),
                "--json",
                "url",
                "--jq",
                ".[].url",
            ])
            .stderr(Stdio::inherit())
            .output()?;
        proc::check()?;
        if !out.status.success() {
            return Err(Exit::new(out.status.code().unwrap_or(1), ""));
        }
        let found: Vec<String> = String::from_utf8_lossy(&out.stdout)
            .lines()
            .map(str::to_string)
            .collect();
        if found.len() >= SEARCH_LIMIT {
            eprintln!(
                "whooper-fetch: the search for PRs that request {}'s review in {org} found 1,000 PRs, the most GitHub returns for one search, so it may have left some out",
                whooper.login
            );
        }
        urls.extend(found.into_iter().filter(|u| !u.is_empty()));
    }
    let github = pull_requests(urls.iter().map(String::as_str))?;
    // The search lists open PRs only, so a listed PR it found is open, and only the others need a look.
    // A failed lookup fails nothing: the PR stays listed, and the next round looks again.
    let found: HashSet<&str> = github.iter().filter_map(|p| p["url"].as_str()).collect();
    let listed: BTreeSet<String> = fsx::entries(&whooper.state.join("rechecks"))
        .iter()
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
        .filter_map(|p| fsx::read_object(p))
        .map(|i| {
            format!(
                "https://github.com/{}/pull/{}",
                str_of(&i, "repo").unwrap_or(""),
                i.get("pr").cloned().unwrap_or(Value::Null)
            )
        })
        .collect();
    let mut looked_up = vec![];
    for url in listed.iter().filter(|u| !found.contains(u.as_str())) {
        match crate::gh::pull_request(url) {
            Ok(pr) => looked_up.extend(pr),
            Err(_) => {
                proc::check()?;
                eprintln!(
                    "whooper-fetch: could not look up {url} on the recheck list, so it stays listed"
                )
            }
        }
    }
    fsx::write_file(
        &poll.join("github.json"),
        &fsx::pretty(&json!(github)),
        false,
    )?;
    fsx::write_file(
        &poll.join("recheck-prs.json"),
        &fsx::pretty(&json!(looked_up)),
        false,
    )?;
    Ok(())
}

fn load(path: &Path) -> Result<Value> {
    let text = fs::read_to_string(path)
        .map_err(|e| Exit::new(1, format!("whooper: cannot read {}: {e}", path.display())))?;
    serde_json::from_str(&text)
        .map_err(|e| Exit::new(1, format!("whooper: {} is not JSON: {e}", path.display())))
}

/// whooper poll: lists the user's review work in poll/<UTC start time>.json and prints its path.
pub fn poll(whooper: &Whooper) -> Result<()> {
    let poll = whooper.state.join("poll");
    fs::create_dir_all(&poll)?;
    let now = Timestamp::now();
    let started = now.as_second();
    let stamp = now
        .to_zoned(TimeZone::UTC)
        .strftime("%Y%m%dT%H%M%SZ")
        .to_string();
    let oldest = match fsx::read_text(&poll.join("cursor")) {
        Some(text) => match text.trim().parse::<i64>() {
            Ok(n) => n,
            Err(_) => fail!(
                "whooper-poll: {} holds no Unix time",
                poll.join("cursor").display()
            ),
        },
        None => started - 86400,
    };
    let work = tempfile::tempdir()?;
    let channel = whooper.get("slack", "ask_channel")?;
    let user = whooper.get("slack", "user_id")?;
    // slackdump reads from a day before the cursor, so the replies and edits that came since under older
    // posts come along. It takes its start time in UTC.
    let from = Timestamp::from_second(oldest - 86400)?
        .to_zoned(TimeZone::UTC)
        .strftime("%Y-%m-%dT%H:%M:%S")
        .to_string();
    let log = work.path().join("slackdump.log");
    let mut dump = Command::new("slackdump");
    dump.args([
        "dump",
        "-workspace",
        &whooper.get("slack", "slackdump_workspace")?,
        "-files=false",
        "-time-from",
        &from,
        "-o",
    ])
    .arg(work.path().join("slack"))
    .arg(&channel)
    .stdin(Stdio::null())
    .stdout(fs::File::create(&log)?)
    .stderr(fs::File::create(&log)?.try_clone()?);
    let ok = dump.status()?.success();
    proc::check()?;
    if !ok {
        let text = fsx::read_text(&log).unwrap_or_default();
        let lines: Vec<&str> = text.lines().collect();
        fail!(
            "whooper-poll: slackdump failed: {}",
            lines[lines.len().saturating_sub(3)..].join("\n")
        );
    }
    let posts = slack_posts(
        &load(&work.path().join("slack").join(format!("{channel}.json")))?,
        oldest,
    );
    let posts_text = fsx::pretty(&json!(posts));
    // The prompt, the answer, and the run's transcript stay next to the poll's output as its record.
    let prefix = poll.join(&stamp);
    let prompt = poll.join(format!("{stamp}.prompt.md"));
    fs::write(
        &prompt,
        format!(
            "{}\nuser_id: {user}\n\nposts:\n\n{posts_text}\n",
            crate::prompt::poll(whooper)?
        ),
    )?;
    let answer = poll.join(format!("{stamp}.answer.json"));
    let agent = whooper.agent("poll");
    let limit = whooper.limits.poll;
    match crate::agent::run(
        whooper,
        &agent,
        &crate::agent::Job::Poll {
            prompt: &prompt,
            answer: &answer,
            prefix: &prefix,
        },
        Duration::from_secs(limit as u64 * 60),
        None,
        &[],
    )? {
        Ended::Stopped => return Err(proc::stopped()),
        Ended::TimedOut => fail!(
            "whooper-poll: the {agent} poll run passed its {limit}-minute limit; see {}.*",
            prefix.display()
        ),
        Ended::Exited(0) => {}
        Ended::Exited(_) => fail!(
            "whooper-poll: the {agent} poll run failed; see {}.*",
            prefix.display()
        ),
    }
    let schema_path = config::root().join("schemas").join("slack-asks.json");
    let answered = load(&answer)?;
    if let Err(e) = crate::schema::check(&answered, &load(&schema_path)?, "the value") {
        fail!(
            "whooper: {} does not fit {}: {e}",
            answer.display(),
            schema_path.display()
        );
    }
    let slack = table(&whooper.values, "slack");
    let (asks, problems) = read_asks(
        &posts,
        &answered,
        oldest,
        &channel,
        &user,
        slack
            .get("workspace_url")
            .and_then(Value::as_str)
            .unwrap_or(""),
        &whooper.orgs,
    )
    .map_err(|e| Exit::new(1, format!("whooper-poll: {e}; see {}", answer.display())))?;
    for problem in problems {
        eprintln!("whooper-poll: {problem}");
    }
    let urls: BTreeSet<String> = asks
        .iter()
        .flat_map(|a| a["pr_urls"].as_array().cloned().unwrap_or_default())
        .filter_map(|u| u.as_str().map(str::to_string))
        .collect();
    let slack_prs = pull_requests(urls.iter().map(String::as_str))?;
    let github = load(&poll.join("github.json"))?;
    let items = poll_items(
        &whooper.login,
        github.as_array().map(Vec::as_slice).unwrap_or(&[]),
        &asks,
        &slack_prs,
        now,
    )
    .map_err(|e| Exit::new(1, format!("whooper: {e}")))?;
    let out = poll.join(format!("{stamp}.json"));
    fsx::write_file(&out, &fsx::pretty(&json!({"items": items})), false)?;
    fs::write(poll.join("cursor"), format!("{started}\n"))?;
    println!("{}", out.display());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pr(requested: &[&str], reviews: Value) -> Value {
        let requests: Vec<Value> = requested
            .iter()
            .map(|l| json!({"requestedReviewer": {"login": l}}))
            .collect();
        json!({"url": "https://github.com/o/r/pull/1", "number": 1, "state": "OPEN", "author": {"login": "alice"},
               "headRefOid": "h", "baseRefName": "main", "repository": {"nameWithOwner": "o/r"},
               "reviewRequests": {"totalCount": requests.len(), "nodes": requests}, "reviews": reviews})
    }

    #[test]
    fn slack_posts_keep_new_posts_replies_and_shared_text() {
        let dump = json!({"messages": [
            {"ts": "1000000000.000001", "user": "U1", "text": "old"},
            {"ts": "2000000000.000001", "user": "U2", "bot_id": "B", "text": "new", "attachments": [{"text": "shared"}],
             "reactions": [{"name": "eyes"}],
             "slackdump_thread_replies": [{"ts": "2000000000.000001"}, {"ts": "2000000001.000001", "user": "U3", "text": "reply"}]},
            {"ts": "bad"}]});
        let posts = slack_posts(&dump, 1500000000);
        assert_eq!(posts.len(), 2);
        assert_eq!(posts[0]["text"], "new\nshared");
        assert_eq!(posts[0]["bot"], true);
        assert_eq!(posts[0]["reactions"], json!(["eyes"]));
        assert_eq!(posts[1]["thread_ts"], "2000000000.000001");
    }

    #[test]
    fn pr_links_count_in_the_configured_orgs_only_and_at_most_nine_digits() {
        let link = PrLink::new(&["example-org".into()]);
        assert_eq!(
            link.find(
                "see https://github.com/example-org/r/pull/12 and http://github.com/other/r/pull/3"
            ),
            ["github.com/example-org/r/pull/12"]
        );
        assert!(
            link.find("https://github.com/example-org/r/pull/1234567890")
                .is_empty()
        );
    }

    #[test]
    fn read_asks_keeps_only_linked_urls_and_builds_permalinks() {
        let posts = vec![
            json!({"ts": "2000000000.000001", "thread_ts": "1999999999.000001", "user": "U1",
                                "text": "<@ME> https://github.com/o/r/pull/1", "reactions": []}),
        ];
        let answer = json!({"asks": [{"ts": "2000000000.000001", "pr_urls": ["https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"], "reason": "r"},
                                     {"ts": "1.2", "pr_urls": [], "reason": "r"}], "not_asks": []});
        let (asks, problems) = read_asks(
            &posts,
            &answer,
            0,
            "C1",
            "ME",
            "https://w.slack.com",
            &["o".into()],
        )
        .unwrap();
        assert_eq!(asks[0]["pr_urls"], json!(["https://github.com/o/r/pull/1"]));
        assert_eq!(
            asks[0]["permalink"],
            "https://w.slack.com/archives/C1/p2000000000000001?thread_ts=1999999999.000001&cid=C1"
        );
        assert_eq!(asks[0]["tagged_user"], true);
        assert_eq!(problems.len(), 2);
        let missed = read_asks(
            &posts,
            &json!({"asks": [], "not_asks": []}),
            0,
            "C1",
            "ME",
            "u",
            &["o".into()],
        );
        assert!(missed.is_err());
    }

    #[test]
    fn poll_items_take_requests_the_user_has_not_answered_and_tagged_asks_first() {
        let none = json!({"totalCount": 0, "nodes": []});
        let reviewed = json!({"totalCount": 1, "nodes": [{"author": {"login": "me"}, "state": "APPROVED", "body": "",
            "submittedAt": "2031-01-01T00:00:00Z", "comments": {"totalCount": 0, "nodes": []}}]});
        let now = "2031-01-02T00:00:00Z".parse().unwrap();
        let items = poll_items(
            "me",
            &[pr(&["me"], none.clone()), pr(&["me"], reviewed)],
            &[],
            &[],
            now,
        )
        .unwrap();
        assert_eq!(items.len(), 1);
        let ask = json!({"ts": "1.1", "permalink": "p", "tagged_user": true, "pr_urls": ["https://github.com/o/r/pull/1"], "has_eyes": true, "reason": "asked"});
        let items = poll_items(
            "me",
            &[pr(&["me"], none.clone())],
            &[ask],
            &[pr(&[], none)],
            now,
        )
        .unwrap();
        assert_eq!(items.len(), 1);
        assert_eq!(items[0]["source"], "slack_ask");
        let cut = json!({"totalCount": 5, "nodes": []});
        assert!(poll_items("me", &[pr(&["me"], cut)], &[], &[], now).is_err());
    }

    // Builders after the old Python suite's: PRs as queries/pr.graphql returns them, asks as read_asks
    // gives them, and Slack messages as slackdump dumps them.
    const LOGIN: &str = "sam-example";
    const NOW: &str = "2031-07-27T05:00:00Z";
    const BOTS: [&str; 2] = ["ci-review-bot", "assistant-bot"];

    fn now() -> Timestamp {
        NOW.parse().unwrap()
    }

    fn connection(nodes: Vec<Value>) -> Value {
        json!({"totalCount": nodes.len(), "nodes": nodes})
    }

    /// A review: (author, state, submittedAt, commit, is a thread reply).
    type R<'a> = (&'a str, &'a str, Option<&'a str>, &'a str, bool);

    struct Pr<'a> {
        number: i64,
        author: &'a str,
        state: &'a str,
        requested: Vec<Option<&'a str>>,
        reviews: Vec<R<'a>>,
    }

    impl Default for Pr<'_> {
        fn default() -> Self {
            Pr {
                number: 1,
                author: "alice",
                state: "OPEN",
                requested: vec![],
                reviews: vec![],
            }
        }
    }

    impl Pr<'_> {
        fn json(&self) -> Value {
            let requests = self
                .requested
                .iter()
                .map(|r| match r {
                    Some(login) => json!({"requestedReviewer": {"login": login}}),
                    // A team matches no fragment in the query, so it comes back empty.
                    None => json!({"requestedReviewer": {}}),
                })
                .collect();
            let reviews = self
                .reviews
                .iter()
                .map(|(author, state, at, commit, reply)| {
                    let reply_to = if *reply {
                        json!({"id": "PRRC_1"})
                    } else {
                        Value::Null
                    };
                    let kind = if BOTS.contains(author) { "Bot" } else { "User" };
                    json!({"author": {"__typename": kind, "login": author}, "state": state,
                           "submittedAt": at, "body": "", "commit": {"oid": commit},
                           "comments": connection(vec![json!({"replyTo": reply_to})])})
                })
                .collect();
            json!({"url": format!("https://github.com/example-org/example-repo/pull/{}", self.number),
                   "number": self.number, "repository": {"nameWithOwner": "example-org/example-repo"},
                   "state": self.state, "headRefOid": "head2", "baseRefName": "master",
                   "author": {"login": self.author}, "reviewRequests": connection(requests),
                   "reviews": connection(reviews), "reviewThreads": connection(vec![])})
        }
    }

    fn requested_pr() -> Value {
        Pr {
            requested: vec![Some(LOGIN)],
            ..Pr::default()
        }
        .json()
    }

    fn reviewed_pr(reviews: Vec<R>) -> Value {
        Pr {
            reviews,
            ..Pr::default()
        }
        .json()
    }

    fn slack_ask(ts: &str, tagged: bool, eyes: bool, urls: &[&str]) -> Value {
        json!({"ts": ts, "permalink": format!("https://example.slack.com/archives/C0000000001/p{}", ts.replace('.', "")),
               "tagged_user": tagged, "pr_urls": urls, "has_eyes": eyes, "reason": "Alex asked for a review."})
    }

    const PR1: &str = "https://github.com/example-org/example-repo/pull/1";

    fn ask_for(tagged: bool, eyes: bool) -> Value {
        slack_ask("1943194837.000100", tagged, eyes, &[PR1])
    }

    fn kinds(items: &[Value]) -> Vec<(i64, String, String)> {
        items
            .iter()
            .map(|i| {
                (
                    i["pr"].as_i64().unwrap(),
                    i["kind"].as_str().unwrap().to_string(),
                    i["source"].as_str().unwrap().to_string(),
                )
            })
            .collect()
    }

    fn one(number: i64, source: &str) -> Vec<(i64, String, String)> {
        vec![(number, "first_review".into(), source.into())]
    }

    #[test]
    fn a_request_by_name_before_any_review_is_a_first_review() {
        let items = poll_items(LOGIN, &[requested_pr()], &[], &[], now()).unwrap();
        assert_eq!(kinds(&items), one(1, "github_request"));
        assert_eq!(items[0]["head_sha"], "head2");
        assert_eq!(items[0]["base_ref"], "master");
        assert!(items[0]["slack"].is_null());
    }

    #[test]
    fn a_team_request_is_not_work() {
        let team = Pr {
            requested: vec![None],
            ..Pr::default()
        };
        assert!(
            poll_items(LOGIN, &[team.json()], &[], &[], now())
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn a_pending_review_does_not_count_as_reviewed() {
        let pr = Pr {
            requested: vec![Some(LOGIN)],
            reviews: vec![(LOGIN, "PENDING", None, "head2", false)],
            ..Pr::default()
        };
        let items = poll_items(LOGIN, &[pr.json()], &[], &[], now()).unwrap();
        assert_eq!(kinds(&items), one(1, "github_request"));
    }

    #[test]
    fn a_request_by_name_after_the_users_review_makes_no_item() {
        let pr = Pr {
            requested: vec![Some(LOGIN)],
            reviews: vec![(
                LOGIN,
                "COMMENTED",
                Some("2031-07-27T01:02:46Z"),
                "head1",
                false,
            )],
            ..Pr::default()
        };
        assert!(
            poll_items(LOGIN, &[pr.json()], &[], &[], now())
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn only_a_thread_reply_is_not_a_review() {
        // The user only replied "+1" in a thread Carol started on Bob's PR.
        let reviews = vec![
            (
                "carol",
                "COMMENTED",
                Some("2031-07-27T01:00:00Z"),
                "head1",
                false,
            ),
            (
                LOGIN,
                "COMMENTED",
                Some("2031-07-27T02:00:00Z"),
                "head1",
                true,
            ),
        ];
        for (requested, want) in [
            (vec![Some(LOGIN)], one(7, "github_request")),
            (vec![], vec![]),
        ] {
            let pr = Pr {
                number: 7,
                author: "bob",
                requested,
                reviews: reviews.clone(),
                ..Pr::default()
            };
            let items = poll_items(LOGIN, &[pr.json()], &[], &[], now()).unwrap();
            assert_eq!(kinds(&items), want);
        }
    }

    #[test]
    fn a_truncated_connection_fails() {
        let mut pr = requested_pr();
        pr["reviews"]["totalCount"] = json!(101);
        assert!(poll_items(LOGIN, &[pr], &[], &[], now()).is_err());
    }

    const HUMAN_REVIEW: R = (
        "bob",
        "COMMENTED",
        Some("2031-07-27T01:00:00Z"),
        "head1",
        false,
    );

    #[test]
    fn a_tagged_ask_is_a_first_review_with_its_post() {
        let items = poll_items(
            LOGIN,
            &[],
            &[ask_for(true, false)],
            &[reviewed_pr(vec![])],
            now(),
        )
        .unwrap();
        assert_eq!(kinds(&items), one(1, "slack_ask"));
        assert_eq!(
            items[0]["slack"],
            json!({"ts": "1943194837.000100", "tagged_user": true,
                   "permalink": "https://example.slack.com/archives/C0000000001/p1943194837000100"})
        );
        assert_eq!(items[0]["reason"], "Alex asked for a review.");
    }

    #[test]
    fn an_untagged_ask_is_work_while_no_human_is_looking() {
        // Review bots and the PR author's own thread replies are not someone reviewing.
        let non_human = vec![
            (
                "ci-review-bot",
                "COMMENTED",
                Some("2031-07-26T18:31:52Z"),
                "head1",
                false,
            ),
            (
                "assistant-bot",
                "COMMENTED",
                Some("2031-07-26T18:40:00Z"),
                "head1",
                false,
            ),
            (
                "alice",
                "COMMENTED",
                Some("2031-07-26T18:59:38Z"),
                "head1",
                true,
            ),
        ];
        let cases: Vec<(&str, bool, Vec<R>, bool)> = vec![
            ("nobody looking", false, vec![], true),
            ("only bots and the author", false, non_human, true),
            (
                "only the user's pending review",
                false,
                vec![(LOGIN, "PENDING", None, "head2", false)],
                true,
            ),
            ("a human put :eyes:", true, vec![], false),
            (
                "a human reviewed in the last 8 hours",
                false,
                vec![HUMAN_REVIEW],
                false,
            ),
            // More than 8 hours before NOW, so the PR is fair game again.
            (
                "a human reviewed earlier",
                false,
                vec![(
                    "bob",
                    "COMMENTED",
                    Some("2031-07-26T20:59:59Z"),
                    "head1",
                    false,
                )],
                true,
            ),
        ];
        for (name, eyes, reviews, taken) in cases {
            let items = poll_items(
                LOGIN,
                &[],
                &[ask_for(false, eyes)],
                &[reviewed_pr(reviews)],
                now(),
            )
            .unwrap();
            let want = if taken { one(1, "slack_ask") } else { vec![] };
            assert_eq!(kinds(&items), want, "{name}");
        }
    }

    #[test]
    fn a_tagged_ask_ignores_eyes_and_other_reviews() {
        let items = poll_items(
            LOGIN,
            &[],
            &[ask_for(true, true)],
            &[reviewed_pr(vec![HUMAN_REVIEW])],
            now(),
        )
        .unwrap();
        assert_eq!(kinds(&items), one(1, "slack_ask"));
    }

    #[test]
    fn an_ask_about_a_pr_the_user_already_reviewed_makes_no_item() {
        for (tagged, state) in [(true, "COMMENTED"), (false, "APPROVED")] {
            let reviewed = reviewed_pr(vec![(
                LOGIN,
                state,
                Some("2031-07-27T01:02:46Z"),
                "head1",
                false,
            )]);
            assert!(
                poll_items(LOGIN, &[], &[ask_for(tagged, false)], &[reviewed], now())
                    .unwrap()
                    .is_empty()
            );
        }
    }

    #[test]
    fn closed_merged_and_own_prs_are_dropped() {
        for pr in [
            Pr {
                state: "CLOSED",
                ..Pr::default()
            },
            Pr {
                state: "MERGED",
                ..Pr::default()
            },
            Pr {
                author: LOGIN,
                ..Pr::default()
            },
        ] {
            assert!(
                poll_items(LOGIN, &[], &[ask_for(true, false)], &[pr.json()], now())
                    .unwrap()
                    .is_empty()
            );
        }
    }

    #[test]
    fn an_ask_for_a_pr_without_data_is_skipped() {
        let ask = slack_ask(
            "1943194837.000100",
            true,
            false,
            &["https://github.com/example-org/example-repo/pull/2"],
        );
        assert!(
            poll_items(LOGIN, &[], &[ask], &[reviewed_pr(vec![])], now())
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn one_item_per_pr_and_kind_preferring_tagged_slack_asks() {
        let asks = [
            slack_ask("1943194600.000001", false, false, &[PR1]),
            slack_ask("1943194837.000100", true, false, &[PR1]),
        ];
        let items = poll_items(
            LOGIN,
            &[requested_pr()],
            &asks,
            &[reviewed_pr(vec![])],
            now(),
        )
        .unwrap();
        assert_eq!(kinds(&items), one(1, "slack_ask"));
        assert_eq!(items[0]["slack"]["ts"], "1943194837.000100");
    }

    #[test]
    fn a_request_by_name_wins_over_an_untagged_ask() {
        // An untagged ask lets the review run skip a PR that is not small. A request by name does not.
        let items = poll_items(
            LOGIN,
            &[requested_pr()],
            &[ask_for(false, false)],
            &[reviewed_pr(vec![])],
            now(),
        )
        .unwrap();
        assert_eq!(kinds(&items), one(1, "github_request"));
    }

    #[test]
    fn each_linked_pr_is_its_own_item() {
        let ask = slack_ask(
            "1943194837.000100",
            true,
            false,
            &[PR1, "https://github.com/example-org/example-repo/pull/2"],
        );
        let prs = [
            Pr {
                number: 1,
                ..Pr::default()
            }
            .json(),
            Pr {
                number: 2,
                ..Pr::default()
            }
            .json(),
        ];
        let items = poll_items(LOGIN, &[], &[ask], &prs, now()).unwrap();
        let mut want = one(1, "slack_ask");
        want.extend(one(2, "slack_ask"));
        assert_eq!(kinds(&items), want);
    }

    const CHANNEL: &str = "C0000000001";
    const USER: &str = "U0000000000";
    const OLDEST: i64 = 1943117600;
    const ASKER: &str = "U0000000001";
    const TEAMMATE: &str = "U0000000002";
    const POST_TS: &str = "1943194837.000100";
    const DIGEST_TS: &str = "1943197600.000001";

    fn message(ts: &str, text: &str, user: &str, reactions: &[&str]) -> Value {
        let mut m = json!({"type": "message", "ts": ts, "user": user, "text": text});
        if !reactions.is_empty() {
            m["reactions"] = reactions
                .iter()
                .map(|n| json!({"name": n, "count": 1, "users": [TEAMMATE]}))
                .collect();
        }
        m
    }

    fn with(mut m: Value, fields: Value) -> Value {
        for (k, v) in fields.as_object().unwrap() {
            m[k] = v.clone();
        }
        m
    }

    fn post() -> Value {
        message(
            POST_TS,
            "<@U0000000000|Sam Reviewer> can you take a look?\n\
             • <https://github.com/example-org/example-repo/pull/21|feat(app): add a settings page #21>\n\
             • <http://github.com/example-org/example-repo/pull/22|#22>",
            ASKER,
            &["eyes"],
        )
    }

    fn bot_digest() -> Value {
        with(
            message(
                DIGEST_TS,
                "• <https://github.com/example-org/example-repo/pull/34|#34>",
                "U07ABCDEF12",
                &[],
            ),
            json!({"bot_id": "B0000000001", "bot_profile": {"name": "pr_bot"}}),
        )
    }

    fn posts(messages: Vec<Value>) -> Vec<Value> {
        let messages = if messages.is_empty() {
            vec![post()]
        } else {
            messages
        };
        slack_posts(
            &json!({"channel_id": CHANNEL, "name": "pr-reviews", "messages": messages}),
            OLDEST,
        )
    }

    fn model_ask(ts: &str, urls: &[&str]) -> Value {
        json!({"ts": ts, "pr_urls": urls, "reason": "Alex asked the user to review #21."})
    }

    fn pr21() -> Value {
        model_ask(
            POST_TS,
            &["https://github.com/example-org/example-repo/pull/21"],
        )
    }

    // Alex's post, reported as not an ask, for tests about other posts.
    fn asker_not_ask() -> Value {
        json!({"ts": POST_TS, "reason": "Not an ask."})
    }

    fn asks_of(
        posts: &[Value],
        asks: Vec<Value>,
        not_asks: Vec<Value>,
    ) -> std::result::Result<(Vec<Value>, Vec<String>), String> {
        read_asks(
            posts,
            &json!({"asks": asks, "not_asks": not_asks}),
            OLDEST,
            CHANNEL,
            USER,
            "https://example.slack.com",
            &["example-org".into(), "sample-org".into()],
        )
    }

    fn urls_of(asks: &[Value]) -> Vec<Value> {
        asks.iter().map(|a| a["pr_urls"].clone()).collect()
    }

    #[test]
    fn slack_posts_keep_each_post_and_reply_since_oldest() {
        let reply = with(
            message("1943194900.000200", "<@U0000000000> also #23", ASKER, &[]),
            json!({"thread_ts": POST_TS}),
        );
        let old = message(
            "1943017600.000001",
            "<https://github.com/example-org/example-repo/pull/9|#9>",
            ASKER,
            &[],
        );
        let malformed = message(
            "1943194837",
            "<https://github.com/example-org/example-repo/pull/9|#9>",
            ASKER,
            &[],
        );
        // slackdump lists the thread's post first among its replies.
        let threaded = with(post(), json!({"slackdump_thread_replies": [post(), reply]}));
        assert_eq!(
            posts(vec![threaded, old, malformed]),
            vec![
                json!({"ts": POST_TS, "user": ASKER, "bot": false, "text": post()["text"], "reactions": ["eyes"]}),
                json!({"ts": "1943194900.000200", "thread_ts": POST_TS, "user": ASKER, "bot": false,
                       "text": "<@U0000000000> also #23", "reactions": []}),
            ]
        );
    }

    #[test]
    fn an_older_post_counts_again_for_a_new_reply_or_edit() {
        let older = message(
            "1943017600.000001",
            "Ongoing PR stack, see the thread",
            ASKER,
            &[],
        );
        let reply = with(
            message(
                "1943194900.000200",
                "<https://github.com/example-org/example-repo/pull/9|#9>",
                ASKER,
                &[],
            ),
            json!({"thread_ts": older["ts"]}),
        );
        let edited = with(
            message("1943017700.000001", "EDIT: see the thread", ASKER, &[]),
            json!({"edited": {"ts": "1943194950.000000"}}),
        );
        let threaded = with(
            older.clone(),
            json!({"slackdump_thread_replies": [older, reply]}),
        );
        let got: Vec<(Value, Value)> = posts(vec![threaded, edited])
            .iter()
            .map(|p| (p["ts"].clone(), p["thread_ts"].clone()))
            .collect();
        assert_eq!(
            got,
            vec![
                (json!("1943194900.000200"), json!("1943017600.000001")),
                (json!("1943017700.000001"), Value::Null),
            ]
        );
    }

    #[test]
    fn a_shared_message_counts_as_part_of_the_post() {
        let bump = with(
            message(DIGEST_TS, "bump on this one", ASKER, &[]),
            json!({"attachments": [{"from_url": "https://example.slack.com/archives/C0000000009/p1943190000000001",
                   "text": "alex requested review on <https://github.com/example-org/example-repo/pull/60|#60>"}]}),
        );
        let url = "https://github.com/example-org/example-repo/pull/60";
        let (asks, problems) = asks_of(
            &posts(vec![bump]),
            vec![model_ask(DIGEST_TS, &[url])],
            vec![],
        )
        .unwrap();
        assert!(problems.is_empty());
        assert_eq!(urls_of(&asks), vec![json!([url])]);
    }

    #[test]
    fn slack_posts_mark_what_bots_wrote() {
        for m in [
            bot_digest(),
            with(
                message(DIGEST_TS, "digest", ASKER, &[]),
                json!({"subtype": "bot_message"}),
            ),
            message(DIGEST_TS, "reminder", "USLACKBOT", &[]),
        ] {
            assert_eq!(posts(vec![m])[0]["bot"], true);
        }
    }

    #[test]
    fn an_ask_gets_a_permalink_built_from_its_ts() {
        let (asks, problems) = asks_of(&posts(vec![]), vec![pr21()], vec![]).unwrap();
        assert!(problems.is_empty());
        assert_eq!(
            asks,
            vec![json!({"ts": POST_TS,
                "permalink": "https://example.slack.com/archives/C0000000001/p1943194837000100",
                "tagged_user": true, "pr_urls": ["https://github.com/example-org/example-repo/pull/21"],
                "has_eyes": true, "reason": "Alex asked the user to review #21."})]
        );
    }

    #[test]
    fn an_http_link_in_the_post_grounds_an_https_url() {
        let url = "https://github.com/example-org/example-repo/pull/22";
        let (asks, problems) =
            asks_of(&posts(vec![]), vec![model_ask(POST_TS, &[url])], vec![]).unwrap();
        assert!(problems.is_empty());
        assert_eq!(asks[0]["pr_urls"], json!([url]));
    }

    #[test]
    fn untrusted_pr_urls_are_dropped() {
        for (url, in_post) in [
            ("https://github.com/other-org/example-repo/pull/21", true),
            (
                "https://github.com/example-org/example-repo/pull/21/files",
                true,
            ),
            (
                "https://github.com/example-org/example-repo/pull/21; gh pr merge",
                true,
            ),
            (
                "https://github.com/example-org/example-repo/issues/21",
                true,
            ),
            ("https://github.com/example-org/example-repo/pull/0", true),
            // Well formed, but the post links #21, not #2.
            ("https://github.com/example-org/example-repo/pull/2", false),
        ] {
            let mut p = post();
            if in_post {
                p["text"] = json!(format!("{}\n{url}", p["text"].as_str().unwrap()));
            }
            let (asks, problems) =
                asks_of(&posts(vec![p]), vec![model_ask(POST_TS, &[url])], vec![]).unwrap();
            assert!(asks.is_empty(), "{url}");
            assert!(!problems.is_empty(), "{url}");
        }
    }

    #[test]
    fn a_pr_url_must_be_linked_by_the_post_that_asks() {
        // The bot's digest links #34, but Alex's post does not.
        let urls = [
            "https://github.com/example-org/example-repo/pull/21",
            "https://github.com/example-org/example-repo/pull/34",
        ];
        let (asks, problems) = asks_of(
            &posts(vec![post(), bot_digest()]),
            vec![model_ask(POST_TS, &urls)],
            vec![],
        )
        .unwrap();
        assert_eq!(urls_of(&asks), vec![json!([urls[0]])]);
        assert_eq!(
            problems,
            [format!("dropped PR URL '{}' from ask {POST_TS}", urls[1])]
        );
    }

    #[test]
    fn an_ask_must_be_one_of_the_channels_posts_since_oldest() {
        let link = "<https://github.com/example-org/example-repo/pull/21|#21>";
        for (ts, extra) in [
            ("1943195599.000001", vec![]),
            ("1943194837", vec![message("1943194837", link, ASKER, &[])]),
            (
                "1943017600.000001",
                vec![message("1943017600.000001", link, ASKER, &[])],
            ),
        ] {
            let mut all = vec![post()];
            all.extend(extra);
            let ask = model_ask(ts, &["https://github.com/example-org/example-repo/pull/21"]);
            let (asks, problems) = asks_of(&posts(all), vec![ask], vec![asker_not_ask()]).unwrap();
            assert!(asks.is_empty(), "{ts}");
            assert!(!problems.is_empty(), "{ts}");
        }
    }

    #[test]
    fn mention_and_eyes_are_read_from_the_post() {
        let eyes_no_mention = message(
            "1943197600.000001",
            "<https://github.com/example-org/example-repo/pull/50|#50>",
            TEAMMATE,
            &["squirrel", "eyes"],
        );
        let mention_no_eyes = message(
            "1943197601.000001",
            "<@U0000000000> <https://github.com/example-org/example-repo/pull/51|#51>",
            ASKER,
            &["eyes-closed"],
        );
        let model_asks = vec![
            model_ask(
                "1943197600.000001",
                &["https://github.com/example-org/example-repo/pull/50"],
            ),
            model_ask(
                "1943197601.000001",
                &["https://github.com/example-org/example-repo/pull/51"],
            ),
        ];
        let (asks, problems) = asks_of(
            &posts(vec![post(), eyes_no_mention, mention_no_eyes]),
            model_asks,
            vec![asker_not_ask()],
        )
        .unwrap();
        assert!(problems.is_empty());
        let got: Vec<Value> = asks
            .iter()
            .map(|a| json!([a["ts"], a["tagged_user"], a["has_eyes"]]))
            .collect();
        assert_eq!(
            got,
            vec![
                json!(["1943197600.000001", false, true]),
                json!(["1943197601.000001", true, false])
            ]
        );
    }

    #[test]
    fn an_ask_from_a_bot_counts() {
        // Such as a digest of PRs waiting for the team's review.
        let url = "https://github.com/example-org/example-repo/pull/34";
        let (asks, problems) = asks_of(
            &posts(vec![post(), bot_digest()]),
            vec![model_ask(DIGEST_TS, &[url])],
            vec![asker_not_ask()],
        )
        .unwrap();
        assert!(problems.is_empty());
        assert_eq!(urls_of(&asks), vec![json!([url])]);
    }

    #[test]
    fn an_ask_in_a_thread_links_to_the_reply() {
        let reply = with(
            message(
                "1943194900.000200",
                "<https://github.com/example-org/example-repo/pull/9|#9>",
                ASKER,
                &[],
            ),
            json!({"thread_ts": POST_TS}),
        );
        let threaded = with(post(), json!({"slackdump_thread_replies": [post(), reply]}));
        let (asks, problems) = asks_of(
            &posts(vec![threaded]),
            vec![model_ask(
                "1943194900.000200",
                &["https://github.com/example-org/example-repo/pull/9"],
            )],
            vec![asker_not_ask()],
        )
        .unwrap();
        assert!(problems.is_empty());
        assert_eq!(
            asks[0]["permalink"],
            format!(
                "https://example.slack.com/archives/C0000000001/p1943194900000200?thread_ts={POST_TS}&cid=C0000000001"
            )
        );
    }

    #[test]
    fn every_post_that_mentions_the_user_next_to_a_pr_link_is_answered() {
        let link =
            "<@U0000000000|Sam Reviewer> <https://github.com/example-org/example-repo/pull/50|#50>";
        let cases = vec![
            ("left out", posts(vec![]), vec![], true),
            (
                "reported as not an ask",
                posts(vec![]),
                vec![asker_not_ask()],
                false,
            ),
            (
                "written by the user",
                posts(vec![message(DIGEST_TS, link, USER, &[])]),
                vec![],
                false,
            ),
            (
                "written by a bot and left out",
                posts(vec![with(
                    message(DIGEST_TS, link, ASKER, &[]),
                    json!({"bot_id": "B0000000001"}),
                )]),
                vec![],
                true,
            ),
            (
                "no PR link",
                posts(vec![message(
                    DIGEST_TS,
                    "<@U0000000000|Sam Reviewer> lunch?",
                    ASKER,
                    &[],
                )]),
                vec![],
                false,
            ),
        ];
        for (name, channel_posts, not_asks, fails) in cases {
            let got = asks_of(&channel_posts, vec![], not_asks);
            if fails {
                assert!(got.is_err(), "{name}");
            } else {
                assert_eq!(got.unwrap(), (vec![], vec![]), "{name}");
            }
        }
    }

    #[test]
    fn pr_links_count_in_every_configured_org_only() {
        let link = PrLink::new(&["example-org".into(), "sample-org".into()]);
        for (url, matches) in [
            ("https://github.com/example-org/app/pull/3", true),
            ("http://github.com/sample-org/app/pull/3", true),
            ("https://github.com/other-org/app/pull/3", false),
            // The org name must match whole, not as a prefix.
            ("https://github.com/example-org-fork/app/pull/3", false),
        ] {
            assert_eq!(!link.find(url).is_empty(), matches, "{url}");
        }
    }
}
