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
