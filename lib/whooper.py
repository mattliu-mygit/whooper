"""Whooper's exact rules: which PRs are the user's review work, which of it is already done, what a
review run may read about a PR, and which of a run's findings pass rule v1.

Every identifiable value, such as the user, the GitHub orgs, the Slack channels, and the bots, comes
from the user's config file (config_path), never from this code. The bin/ scripts call it as:
  whooper.py env                                                                   -> shell variables
  whooper.py repo <owner/repo>                                                     -> the repo's shell variables
  whooper.py prompt poll | review <owner/repo> <agent>                             -> a prompt, filled in
  whooper.py check <schema.json> <value.json>                                      -> fails unless value fits schema
  whooper.py posts <slackdump channel.json> <oldest>                               -> the channel's posts JSON
  whooper.py asks <posts.json> <answer.json> <oldest>                              -> asks JSON
  whooper.py items <github.json> <asks.json> <slack-prs.json>                      -> {"items": [...]}
  whooper.py select <data dir>                                                     -> one work item per line
  whooper.py conversation <conversation.graphql result> [<as-of time>]             -> a run's github.json
  whooper.py record <data dir> <work-item.json> running|failed                     -> writes the PR record
  whooper.py classify <data dir> <run dir>                                         -> verdicts, digest, record
  whooper.py post <data dir> <run dir> <head now> <review comments.json>          -> post.json, digest, record
  whooper.py summary <run dir>                                                     -> the summary channel's text
  whooper.py skills <skills dir>                                                   -> {skill: content hash}
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tomllib

ROOT = Path(__file__).resolve().parent.parent
MAX_FAILURES = 3
MAX_RECHECKS_PER_DAY = 3
# An untagged team ask leaves a PR to whoever else reviewed it in the last 8 hours. After that,
# the PR is fair game again.
CLAIM_HOURS = 8
# Auto-post rule v1 (design question 3): a finding posts without the user only when the run answers
# all of these true and the transcript backs the first and third.
RULE_V1 = ("ran_and_saw_failure", "caller_or_ci_visible", "introduced_by_pr", "not_already_raised", "not_pr_text")
# Whooper's review body and every comment it posts start with this.
MARKER = "Whooper AI"
# A draft posts under the user's name, so it may not ping anyone or link outside the org's GitHub.
# The lookbehind keeps email addresses and paths from counting as mentions.
MENTION = re.compile(r"(?<![\w.@/])@[A-Za-z0-9]")
LINK = re.compile(r"(?:https?://|www\.)[^\s)>\]]*")
# How prompts/review.md names each agent's tools, limits, and way of returning the findings.
REVIEW_WORDING = {
    "claude": {
        "shell": "Bash",
        "skill_tool": "the Skill tool",
        "output_limit": "the transcript keeps only a 2 KB preview of an output over 30,000 characters",
        "probe_rule": "",
        "answer": "Return the findings as your structured output, in the JSON format this run requires.",
    },
    "devin": {
        "shell": "exec",
        "skill_tool": "the skill tool",
        "output_limit": "exec shows only the start and end of a long output and saves the rest to a file",
        "probe_rule": (" Pass `timeout: 280000`, exec's longest, on every probe's exec call, so the probe finishes"
                       " inside that call: a call that exec moves to the background does not count."),
        "answer": (f"Then write the findings to `findings.json` in the run directory, as JSON that fits the JSON Schema"
                   f" in `{ROOT / 'schemas' / 'findings.json'}`. A file that does not fit fails the run."),
    },
}


def config_path():
    """$WHOOPER_CONFIG, else whooper/config.toml under $XDG_CONFIG_HOME (default ~/.config)."""
    if os.environ.get("WHOOPER_CONFIG"):
        return Path(os.environ["WHOOPER_CONFIG"])
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "whooper" / "config.toml"


def load_config():
    path = config_path()
    if not path.exists():
        raise SystemExit(f"whooper: no config at {path}; copy examples/config.example.toml there and fill it in")
    config = tomllib.loads(path.read_text())
    config["dir"] = path.parent
    return config


def xdg_dir(variable, default):
    """Whooper's directory under an XDG base directory, such as $XDG_STATE_HOME/whooper."""
    return Path(os.environ.get(variable) or Path.home() / default) / "whooper"


def pr_link(orgs):
    """Matches a plain pull request URL in one of orgs. Slack text is untrusted, so only these URLs
    reach a gh command. Posts link with http:// or https://, and group 1 is the link without its
    scheme."""
    names = "|".join(re.escape(org) for org in orgs)
    return re.compile(rf"https?://(github\.com/(?:{names})/[A-Za-z0-9._-]+/pull/[1-9][0-9]{{0,8}})(?![0-9])")


def fill(template, values):
    """Replaces each {{name}} in a prompt template. A placeholder with no value is an error, so a
    prompt never reaches a run half filled."""
    def value(m):
        if m[1] not in values:
            raise SystemExit(f"whooper: no value for {{{{{m[1]}}}}} in the prompt template")
        return values[m[1]]
    return re.sub(r"\{\{(\w+)\}\}", value, template)


def check_schema(value, schema, where="the value"):
    """Raises ValueError at the first place value breaks schema. Covers the JSON Schema keywords
    Whooper's schemas use: type, enum, minimum, properties, required, additionalProperties, and items."""
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    # Python's True is also an int, but JSON's true is not an integer.
    if kind and (not isinstance(value, types[kind]) or (kind == "integer" and isinstance(value, bool))):
        raise ValueError(f"{where} is not of type {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{where} is {value!r}, not one of {schema['enum']}")
    if "minimum" in schema and isinstance(value, int) and value < schema["minimum"]:
        raise ValueError(f"{where} is below {schema['minimum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if missing := [key for key in schema.get("required", []) if key not in value]:
            raise ValueError(f"{where} lacks {', '.join(missing)}")
        if schema.get("additionalProperties") is False and (extra := sorted(value.keys() - properties.keys())):
            raise ValueError(f"{where} has unexpected {', '.join(extra)}")
        for key, sub in properties.items():
            if key in value:
                check_schema(value[key], sub, f"{where}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            check_schema(item, schema["items"], f"{where}[{i}]")


def bot_list(bots, key):
    """Names a config bot list for a prompt: "the CI review bot (ci-bot), the assistant (helper-bot)"."""
    return ", ".join(f"{b['label']} ({b[key]})" for b in bots) or "none"


def slack_posts(dump, oldest):
    """Returns the posts and thread replies in a channel that slackdump dumped that are new since oldest.

    dump is slackdump's JSON for one channel, read from a day before oldest, so it also holds the
    replies and edits that came since oldest under older posts. A message is new when it was posted
    or last edited at or after oldest. Each one keeps its ts, the ts of the post it replies to
    (thread_ts, for a reply), its sender's user ID, whether a bot wrote it (a bot_id, a bot message,
    or Slackbot), its text in Slack's markup, and the names of its reactions.
    """
    def new(m):
        times = [m["ts"], (m.get("edited") or {}).get("ts", "")]
        return any(re.fullmatch(r"\d{10}\.\d{6}", t) and int(t.split(".")[0]) >= int(oldest) for t in times)

    def post(m, thread_ts=None):
        return {
            "ts": m["ts"],
            **({"thread_ts": thread_ts} if thread_ts else {}),
            "user": m.get("user", ""),
            "bot": bool(m.get("bot_id")) or m.get("subtype") == "bot_message" or m.get("user") == "USLACKBOT",
            # A post that shares another message, such as a bump of a review request, carries the
            # shared message's text, and its PR link, in an attachment.
            "text": "\n".join([m.get("text", "")] + [a["text"] for a in m.get("attachments") or [] if a.get("text")]),
            "reactions": [r["name"] for r in m.get("reactions") or []],
        }

    posts = []
    for m in dump["messages"]:
        if not re.fullmatch(r"\d{10}\.\d{6}", m.get("ts", "")):
            continue
        if new(m):
            posts.append(post(m))
        # slackdump lists a thread's post first among its replies.
        posts += [post(r, m["ts"]) for r in m.get("slackdump_thread_replies") or []
                  if r.get("ts") != m["ts"] and re.fullmatch(r"\d{10}\.\d{6}", r.get("ts", "")) and new(r)]
    return posts


def read_asks(posts, answer, oldest, channel, user, slack_url, orgs):
    """Returns the review asks a poll run reported, and a note for each ask or URL it dropped.

    posts are the ask channel's posts and thread replies new since oldest (slack_posts), and answer
    the run's {asks, not_asks}. Raises ValueError when the run left out a post, not written by the
    user, that mentions the user next to a link to a PR in orgs: each such post must be in asks or
    not_asks.

    An ask must name one of those posts. A bot's post counts, such as a list of PRs waiting for
    review. An ask keeps only the URLs of PRs in orgs that the post links. Whether the post mentions
    the user and has :eyes: is read from the post.
    """
    by_ts = {p["ts"]: p for p in posts}
    mention = re.compile(rf"<@{re.escape(user)}[|>]")
    link = pr_link(orgs)
    answered = {a["ts"] for a in answer["asks"] + answer["not_asks"]}
    missed = [
        p["ts"] for p in posts
        if p["ts"] not in answered and mention.search(p["text"]) and link.search(p["text"]) and p["user"] != user
    ]
    if missed:
        raise ValueError(f"the poll run left out posts that mention {user} next to a PR link: {', '.join(missed)}")

    asks, problems = [], []
    for ask in answer["asks"]:
        ts = ask["ts"]
        post = by_ts.get(ts)
        if post is None:
            problems.append(f"dropped ask {ts!r}: not a post in the channel since {oldest}")
            continue
        linked = {"https://" + m[1] for m in link.finditer(post["text"])}
        urls = [url for url in ask["pr_urls"] if url in linked]
        problems += [f"dropped PR URL {url!r} from ask {ts}" for url in ask["pr_urls"] if url not in linked]
        if not urls:
            problems.append(f"dropped ask {ts}: no PR URL left")
            continue
        permalink = f"{slack_url}/archives/{channel}/p{ts.replace('.', '')}"
        if "thread_ts" in post:
            permalink += f"?thread_ts={post['thread_ts']}&cid={channel}"
        asks.append({
            "ts": ts,
            "permalink": permalink,
            "tagged_user": bool(mention.search(post["text"])),
            "pr_urls": urls,
            "has_eyes": "eyes" in post["reactions"],
            "reason": ask["reason"],
        })
    return asks, problems


def poll_items(login, github_prs, asks, slack_prs, now):
    """Returns the review work found in GitHub data and Slack asks, one item per PR and kind.

    github_prs come from whooper-fetch's searches (open, not the user's). slack_prs are the PRs
    the asks link, in the same queries/pr.graphql shape. now dates the CLAIM_HOURS window.
    """
    found = []
    for pr in github_prs:
        reviews = user_reviews(pr, login)
        requested = any(r["requestedReviewer"].get("login") == login for r in nodes(pr["reviewRequests"]))
        if requested and reviews:
            sha = reviews[-1]["commit"]["oid"][:7]
            found.append(work_item(pr, "recheck", "github_request", f"{login} was re-requested by name after reviewing at {sha}."))
        elif requested:
            found.append(work_item(pr, "first_review", "github_request", f"A GitHub review request names {login}, who has not reviewed this PR."))
        elif reason := recheck_reason(pr, login, reviews):
            found.append(work_item(pr, "recheck", "reviewed_by_user", reason))

    prs_by_url = {pr["url"]: pr for pr in slack_prs}
    for ask in asks:
        for url in ask["pr_urls"]:
            pr = prs_by_url.get(url)
            if pr is None or pr["state"] != "OPEN" or pr["author"]["login"] == login:
                continue
            # An untagged team ask is taken only while nobody else is looking at the PR: no :eyes:,
            # and no review by someone else in the last CLAIM_HOURS.
            if not ask["tagged_user"] and (ask["has_eyes"] or human_reviewed_since(pr, now - timedelta(hours=CLAIM_HOURS))):
                continue
            kind = "recheck" if user_reviews(pr, login) else "first_review"
            slack = {"ts": ask["ts"], "tagged_user": ask["tagged_user"], "permalink": ask["permalink"]}
            found.append(work_item(pr, kind, "slack_ask", ask["reason"], slack))

    # When sources overlap, a tagged ask wins because it carries the post to claim with :eyes:.
    # A request by name wins over an untagged ask, which lets the review run skip a large PR.
    def rank(item):
        if item["slack"]:
            return 0 if item["slack"]["tagged_user"] else 2
        return 1 if item["source"] == "github_request" else 3

    items = {}
    for item in sorted(found, key=rank):
        items.setdefault((item["repo"], item["pr"], item["kind"]), item)
    return list(items.values())


def recheck_reason(pr, login, reviews):
    """Returns why a PR the user reviewed needs a re-check, or None.

    It does when the user's latest APPROVED, CHANGES_REQUESTED, or DISMISSED review is not
    APPROVED, every thread the user started has a PR author reply or is resolved, and the head
    moved from the commit of the user's latest review or an author reply is newer than it. A later
    COMMENTED review does not withdraw an approval.
    """
    opinions = [r["state"] for r in reviews if r["state"] != "COMMENTED"]
    if not reviews or (opinions and opinions[-1] == "APPROVED"):
        return None
    latest = reviews[-1]
    author = pr["author"]["login"]
    reply_times = []
    for thread in nodes(pr["reviewThreads"]):
        comments = nodes(thread["comments"])
        if comments[0]["author"]["login"] != login:
            continue
        thread_reply_times = [c["createdAt"] for c in comments[1:] if c["author"]["login"] == author]
        if not thread_reply_times and not thread["isResolved"]:
            return None
        reply_times += thread_reply_times

    answered = f"Every thread {login} started has an author reply or is resolved"
    reviewed_sha = latest["commit"]["oid"]
    if pr["headRefOid"] != reviewed_sha:
        return f"{answered}, and the head moved from {reviewed_sha[:7]} to {pr['headRefOid'][:7]} since {login}'s latest review."
    newer = [t for t in reply_times if t > latest["submittedAt"]]
    if newer:
        return f"{answered}, and the author replied at {max(newer)}, after {login}'s latest review at {latest['submittedAt']}."
    return None


def user_reviews(pr, login):
    """The user's submitted reviews, ordered by submission.

    A PENDING review is not submitted. A thread reply is not a review either, though GitHub records
    it as a COMMENTED review with no body and one comment that replies to another, at the head the
    PR had when the reply was written.
    """
    mine = []
    for r in nodes(pr["reviews"]):
        comments = r["comments"]
        reply = (r["state"] == "COMMENTED" and not r["body"] and comments["totalCount"] == 1
                 and comments["nodes"][0]["replyTo"] is not None)
        if r["author"]["login"] == login and r["state"] != "PENDING" and not reply:
            mine.append(r)
    # The API orders reviews by creation, and a review started earlier can be submitted later.
    return sorted(mine, key=lambda r: r["submittedAt"])


def human_reviewed_since(pr, since):
    """Someone other than the PR author and bot accounts submitted a review at or after since."""
    author = pr["author"]["login"]
    return any(
        r["state"] != "PENDING" and r["author"]["__typename"] != "Bot" and r["author"]["login"] != author
        and datetime.fromisoformat(r["submittedAt"]) >= since
        for r in nodes(pr["reviews"])
    )


def nodes(connection):
    """A GraphQL connection's nodes. Fails when the query's page size cut some off."""
    if connection["totalCount"] > len(connection["nodes"]):
        raise ValueError(f"{connection['totalCount']} nodes exceed one page of {len(connection['nodes'])}")
    return connection["nodes"]


def work_item(pr, kind, source, reason, slack=None):
    return {
        "kind": kind,
        "repo": pr["repository"]["nameWithOwner"],
        "pr": pr["number"],
        "head_sha": pr["headRefOid"],
        "base_ref": pr["baseRefName"],
        "source": source,
        "slack": slack,
        "reason": reason,
    }


def select_work(items, prs_dir, now):
    """Splits items into work to start and (item, reason) for items already handled.

    prs_dir holds the records write_record writes. An item is dropped when its record is done or
    running or has failed MAX_FAILURES times. A re-check is dropped when its PR already has
    MAX_RECHECKS_PER_DAY re-check records for other heads started on now's local day.
    """
    work, drops = [], []
    for item in items:
        path = record_path(prs_dir, item)
        pr_dir = path.parent
        record = json.loads(path.read_text()) if path.exists() else None
        reason = None
        if record and record["status"] in ("done", "running"):
            reason = f"{record['status']} at this head"
        elif record and record["failures"] >= MAX_FAILURES:
            reason = f"failed {record['failures']} times at this head"
        elif item["kind"] == "recheck":
            started = [
                datetime.fromisoformat(json.loads(p.read_text())["started_at"]).astimezone(now.tzinfo).date()
                for p in pr_dir.glob("*.recheck.json") if p != path
            ]
            if started.count(now.date()) >= MAX_RECHECKS_PER_DAY:
                reason = f"{MAX_RECHECKS_PER_DAY} re-checks already today"
        if reason:
            drops.append((item, reason))
        else:
            work.append(item)
    return work, drops


def record_path(prs_dir, item):
    """The record of work on one head and kind: <owner>-<repo>-<pr>/<head>.<kind>.json under prs_dir."""
    return prs_dir / f"{item['repo'].replace('/', '-')}-{item['pr']}" / f"{item['head_sha']}.{item['kind']}.json"


def write_record(prs_dir, item, status, now, approval=None):
    """Records a review run's status for the item's head and kind.

    running starts a run at now. failed counts one more failure at this head and done keeps the
    count, so whooper-select stops retrying a head after MAX_FAILURES. classify passes the run's
    approval with done, and later statuses for the same head keep it.
    """
    path = record_path(prs_dir, item)
    started_at = now.isoformat(timespec="seconds")
    old = json.loads(path.read_text()) if path.exists() else {"failures": 0, "started_at": started_at}
    record = {
        "status": status,
        "failures": old["failures"] + (status == "failed"),
        "started_at": started_at if status == "running" else old["started_at"],
    }
    if approval := approval or old.get("approval"):
        record["approval"] = approval
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


def conversation(pr, as_of=None):
    """Returns a review run's github.json: the PR's text, its submitted reviews with their comments,
    and its conversation comments.

    pr is queries/conversation.graphql's pullRequest. With as_of, a replay sees only what people
    could read before that time: reviews submitted before it, comments created and last edited
    before it, and the body as it read then. A review's comments go with their review, because
    GitHub dates a comment from when it was drafted in a pending review, before anyone could read
    it. GitHub keeps no title history, so the title is today's.
    """
    def readable(time):
        return as_of is None or datetime.fromisoformat(time) < as_of

    body = pr["body"]
    # Once a body is edited, GitHub lists every version as an edit, the first one included.
    if as_of is not None and pr["userContentEdits"]["totalCount"]:
        versions = [v for v in nodes(pr["userContentEdits"]) if readable(v["editedAt"])]
        body = max(versions, key=lambda v: v["editedAt"])["diff"] if versions else None

    reviews = [
        {
            "author": author_login(r["author"]),
            "state": r["state"],
            "submitted_at": r["submittedAt"],
            "commit": r["commit"]["oid"],
            "body": r["body"],
            "comments": [
                {
                    "id": c["databaseId"],
                    "reply_to": c["replyTo"] and c["replyTo"]["databaseId"],
                    "author": author_login(c["author"]),
                    "created_at": c["createdAt"],
                    "path": c["path"],
                    "line": c["originalLine"],
                    "start_line": c["originalStartLine"],
                    "commit": c["originalCommit"]["oid"],
                    "diff_hunk": c["diffHunk"],
                    "body": c["body"],
                }
                for c in nodes(r["comments"]) if readable(c["lastEditedAt"] or c["createdAt"])
            ],
        }
        for r in nodes(pr["reviews"]) if r["state"] != "PENDING" and readable(r["submittedAt"])
    ]
    comments = [
        {"author": author_login(c["author"]), "created_at": c["createdAt"], "body": c["body"]}
        for c in nodes(pr["comments"]) if readable(c["lastEditedAt"] or c["createdAt"])
    ]
    return {
        "pr": {"url": pr["url"], "number": pr["number"], "title": pr["title"], "author": author_login(pr["author"]), "body": body},
        "as_of": as_of.isoformat() if as_of else None,
        "reviews": reviews,
        "comments": comments,
    }


def author_login(actor):
    """An author's login. GitHub returns no author for a deleted account and shows it as ghost."""
    return actor["login"] if actor else "ghost"


def claude_shell_calls(events):
    """Returns the Bash calls of a Claude Code run's main session, keyed by tool_use_id.

    events is the run's stream-json transcript. Each call has its command and output, ok (the call
    did not fail), cut (the transcript kept only a preview of the output), and finished, always
    true, because Claude Code waits for a Bash call to end.
    """
    calls = {}
    for e in events:
        # A subagent's messages carry the ID of the Task call that started it.
        if e["type"] not in ("assistant", "user") or e.get("parent_tool_use_id"):
            continue
        content = e["message"]["content"]
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_use" and block["name"] == "Bash":
                calls[block["id"]] = {"command": block["input"]["command"], "output": "", "ok": True, "cut": False,
                                      "finished": True}
            elif block.get("type") == "tool_result" and block["tool_use_id"] in calls:
                output = block["content"]
                output = output if isinstance(output, str) else "".join(part.get("text", "") for part in output)
                # The CLI shows only a 2 KB preview of a successful output over 30,000 characters. It
                # also cuts a failed call's output, which is not ok anyway.
                calls[block["tool_use_id"]].update(output=output, ok=not block.get("is_error"),
                                                   cut="<persisted-output>" in output)
    return calls


def devin_shell_calls(export):
    """Returns the exec calls of a Devin run's main session, keyed by tool_call_id.

    export is the run's ATIF trajectory from devin --export, which holds the main session's calls
    and none of a subagent's. A call given a workdir reads as a command that starts with cd to it.
    exec ends a finished call's output with an "Exit code: <n>" line. A call without one was moved
    to the background or rejected, so it did not finish inside its call. A long output is cut two
    ways, each naming the file with the rest: exec keeps only its start and end, and the CLI keeps
    only the first lines of any tool result too large for the transcript. The second cuts off the
    exit code line, so the call finished but its exit code is unknown and it is not ok.
    """
    calls = {}
    for step in export["steps"]:
        outputs = {r.get("source_call_id"): r.get("content") for r in (step.get("observation") or {}).get("results", [])}
        for call in step.get("tool_calls") or []:
            if call.get("function_name") != "exec":
                continue
            args = call.get("arguments") or {}
            command = args.get("command", "")
            if args.get("workdir"):
                command = f"cd {args['workdir']} && {command}"
            output = outputs.get(call["tool_call_id"]) or ""
            if isinstance(output, list):
                output = "".join(part.get("text", "") for part in output)
            exit_code = re.search(r"\nExit code: (-?\d+)\s*\Z", output)
            too_large = "<truncation_notice>" in output
            calls[call["tool_call_id"]] = {
                "command": command, "output": output, "ok": bool(exit_code) and exit_code[1] == "0",
                "cut": too_large or "The full terminal output is being written live to" in output,
                "finished": bool(exit_code) or too_large,
            }
    return calls


# Each agent's reader: a review run's directory -> the shell calls of the run's main session.
SHELL_CALLS = {
    "claude": lambda run: claude_shell_calls(
        [json.loads(line) for line in (run / "transcript.jsonl").read_text().splitlines() if line]),
    "devin": lambda run: devin_shell_calls(json.loads((run / "transcript.json").read_text())),
}


def classify(result, calls, fetched, run_dir):
    """Returns each finding of a review run with its verdict and the checks it failed.

    result is the run's findings.json, calls the shell calls of its main session from its agent's
    reader in SHELL_CALLS, and run_dir the run directory, which held the worktrees head/ and base/.
    fetched says whether the run's github.json was written, without which nobody can tell a point
    was not already raised. A skipped run has no verdicts.

    A finding is auto_post only when every rule_v1 answer is true, its line is in the diff (GitHub
    takes inline comments only there), fetched is true, and two shell calls of the main session,
    not of a subagent, back it. head_run finished inside its call, and its output contains observed.
    base_run ran head_run's command, which names the head worktree, with the base worktree in its
    place; it finished inside its call and succeeded, and its whole output was kept and does not
    contain observed. Every nit is a shadow_nit, whatever its checks, and everything else goes to
    the digest.
    """
    if result["status"] == "skipped":
        return []
    head_tree, base_tree = f"{run_dir}/head", f"{run_dir}/base"

    verdicts = []
    for f in result["findings"]:
        failed = [f"rule_v1.{name} is false" for name in RULE_V1 if not f["rule_v1"][name]]
        if not f["in_diff"]:
            failed.append("the line is outside the diff")
        if not fetched:
            failed.append("the GitHub fetch failed")
        head, base = calls.get(f["head_run"]), calls.get(f["base_run"])
        if head is None:
            failed.append(f"head_run {f['head_run']!r} is not a shell call of the main session")
        elif not head["finished"]:
            failed.append("head_run did not finish inside its call")
        elif f["observed"] not in head["output"]:
            failed.append("observed is not in the head_run output")
        if base is None:
            failed.append(f"base_run {f['base_run']!r} is not a shell call of the main session")
        else:
            if head is not None and (head_tree not in head["command"]
                                     or base["command"] != head["command"].replace(head_tree, base_tree)):
                failed.append("base_run is not head_run's command run in the base worktree")
            if not base["finished"]:
                failed.append("base_run did not finish inside its call")
            elif not base["ok"]:
                failed.append("base_run failed")
            if base["cut"]:
                failed.append("base_run's output was cut to a preview")
            elif f["observed"] in base["output"]:
                failed.append("observed is also in the base_run output")
        verdict = "shadow_nit" if f["kind"] == "nit" else "digest" if failed else "auto_post"
        verdicts.append({**f, "verdict": verdict, "checks_failed": failed})
    return verdicts


def hold_back(verdicts, comments, started, orgs):
    """Splits a run's auto_post findings into those whooper-post sends and those it holds back,
    moved to the digest with the reasons.

    comments are the PR's review comments as GitHub's REST API lists them, and started is when the
    run started. A finding is held back when a comment created since then covers its line, because
    someone may have raised the point after the run read github.json, or when its draft @-mentions
    someone or links anywhere but the GitHub pages of orgs.
    """
    def covers(c, f):
        # line is null once a later push moves the comment off the diff; original_line remains.
        end = c["line"] or c["original_line"]
        start = c["start_line"] or c["original_start_line"] or end
        return c["path"] == f["path"] and c["side"] == f["side"] and start <= f["line"] <= end

    new = [c for c in comments if datetime.fromisoformat(c["created_at"]) >= started]
    posts, held = [], []
    for v in verdicts:
        if v["verdict"] != "auto_post":
            continue
        reasons = []
        if any(covers(c, v) for c in new):
            reasons.append("a review comment written since the run started covers this line")
        if MENTION.search(v["draft"]):
            reasons.append("the draft @-mentions someone")
        if any(not link.startswith(tuple(f"https://github.com/{org}/" for org in orgs)) for link in LINK.findall(v["draft"])):
            reasons.append("the draft links outside the configured GitHub orgs")
        if reasons:
            held.append({**v, "verdict": "digest", "checks_failed": reasons})
        else:
            posts.append(v)
    return posts, held


def review_request(head_sha, posts):
    """The body of the one POST /pulls/<pr>/reviews call for posts. The event is always COMMENT,
    never APPROVE or REQUEST_CHANGES."""
    return {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": f"{MARKER} review. Each comment reports a failure Whooper reproduced at this commit and not at the merge base.",
        "comments": [
            {"path": f["path"], "line": f["line"], "side": f["side"], "body": f"{MARKER}\n\n{f['draft']}"} for f in posts
        ],
    }


APPROVAL_TEXT = {"approve_once_ci_passes": "Approve once CI passes", "look_first": "Look first"}


def summary_text(run_dir):
    """The summary channel's message for a run that whooper-post has handled: the PR, what posted or
    would post, how many findings wait in the digest, and where the log is."""
    def load(name):
        return json.loads((run_dir / name).read_text())

    item, result, post = load("run.json"), load("findings.json"), load("post.json")
    lines = [
        f"Whooper reviewed https://github.com/{item['repo']}/pull/{item['pr']} at {item['head_sha'][:12]}"
        + (" (dry run)." if item["dry_run"] else "."),
        f"{APPROVAL_TEXT[result['approval']['verdict']]}: {result['approval']['reason']}",
    ]
    if result["status"] == "skipped":
        lines.append(f"Skipped: {result['skip_reason']}")
    elif post["status"] == "stale":
        lines.append(f"Posted nothing: the PR's head moved to {post['head_now'][:12]} during the review.")
    elif post["posted"]:
        lines.append(f"Would post these comments (print only, request in {post['request']}):")
        lines += [f"- {p['title']} ({p['path']}:{p['line']})" for p in post["posted"]]
    else:
        lines.append("Nothing to post.")
    # classify sent every finding but the auto_post ones to the digest, and whooper-post the held ones.
    waiting = sum(v["verdict"] != "auto_post" for v in load("verdicts.json")) + len(post["held"])
    if waiting:
        lines.append(f"Findings in the digest: {waiting}")
    lines.append(f"Log: {run_dir / 'log.md'}")
    return "\n".join(lines)


def append_digest(data, run_dir, item, verdicts):
    """Adds findings that wait for the user to data/digest.jsonl, with the run they came from."""
    with open(data / "digest.jsonl", "a") as digest:
        for v in verdicts:
            digest.write(json.dumps({
                "run_id": item["run_id"], "repo": item["repo"], "pr": item["pr"], "head_sha": item["head_sha"],
                "kind": item["kind"], "dry_run": item["dry_run"], "as_of": item["as_of"],
                "log": str(run_dir / "log.md"), "finding": v,
            }) + "\n")


def skill_hashes(skills_dir):
    """A hash of each skill folder's files by relative path and content, so a run's record names the skill
    text it loaded. The same files give the same value wherever they sit, so a run's hash can be matched
    against a saved copy of a skill version."""
    hashes = {}
    for skill in sorted(Path(skills_dir).iterdir()):
        if not skill.is_dir():
            continue
        h = hashlib.sha256()
        for dirpath, dirnames, filenames in os.walk(skill):
            dirnames.sort()
            for name in sorted(filenames):
                path = Path(dirpath) / name
                h.update(str(path.relative_to(skill)).encode() + b"\0")
                h.update(hashlib.sha256(path.read_bytes()).hexdigest().encode())
        hashes[skill.name] = h.hexdigest()
    return hashes


def main(command, *args):
    def load(path):
        return json.loads(Path(path).read_text())

    if command == "env":
        config = load_config()
        # Each job runs on the agent [jobs] names, through its launcher agents/<agent>/<job>.
        for job, agent in config["jobs"].items():
            if not (ROOT / "agents" / agent / job).exists():
                raise SystemExit(f"whooper: [jobs] {job} names {agent!r}, which has no agents/{agent}/{job}")
        values = {
            "WHOOPER_CONFIG_DIR": config["dir"],
            "WHOOPER_STATE": xdg_dir("XDG_STATE_HOME", ".local/state"),
            "WHOOPER_CACHE": xdg_dir("XDG_CACHE_HOME", ".cache"),
            "WHOOPER_GITHUB_LOGIN": config["user"]["github_login"],
            "WHOOPER_ORGS": " ".join(config["github"]["orgs"]),
            "WHOOPER_SLACK_USER": config["slack"]["user_id"],
            "WHOOPER_ASK_CHANNEL": config["slack"]["ask_channel"],
            "WHOOPER_SUMMARY_CHANNEL": config["slack"]["summary_channel"],
            "WHOOPER_SUMMARY_CHANNEL_NAME": config["slack"]["summary_channel_name"],
            "WHOOPER_SLACKDUMP_WORKSPACE": config["slack"]["slackdump_workspace"],
            "WHOOPER_REVIEW_TIMEOUT_MINUTES": config["review"]["timeout_minutes"],
            **{f"WHOOPER_{job.upper()}_AGENT": agent for job, agent in config["jobs"].items()},
        }
        for name, value in values.items():
            print(f"{name}={shlex.quote(str(value))}")
        # Each agent's settings, named by agent and key: [agents.claude] review_model becomes
        # WHOOPER_CLAUDE_REVIEW_MODEL. They are exported, because the launchers in agents/ run as
        # child processes and read their own agent's.
        for agent, settings in config["agents"].items():
            for key, value in settings.items():
                print(f"export WHOOPER_{agent.upper()}_{key.upper()}={shlex.quote(str(value))}")
    elif command == "repo":
        config, repo = load_config(), args[0]
        settings = config.get("repos", {}).get(repo)
        if settings is None:
            raise SystemExit(f"whooper: {repo} has no [repos.\"{repo}\"] table in {config_path()}")
        # Scripts named in the config are relative to its directory.
        values = {
            "WHOOPER_REPO_CLONE": xdg_dir("XDG_CACHE_HOME", ".cache") / "repos" / repo.replace("/", "-"),
            "WHOOPER_REPO_SETUP": config["dir"] / settings["setup"] if "setup" in settings else "",
            "WHOOPER_REPO_TEARDOWN": config["dir"] / settings["teardown"] if "teardown" in settings else "",
        }
        for name, value in values.items():
            print(f"{name}={shlex.quote(str(value))}")
    elif command == "prompt":
        config, (name, *rest) = load_config(), args
        values = {
            "user_name": config["user"]["name"],
            "github_login": config["user"]["github_login"],
            "review_bots": bot_list(config["github"]["review_bots"], "login"),
            "skills": ", ".join(f"`{skill}`" for skill in config["review"]["skills"]),
        }
        if name == "review":
            repo, agent = rest
            notes = config["repos"][repo].get("notes")
            values["repo_notes"] = (config["dir"] / notes).read_text().strip() if notes else "None."
            values.update(REVIEW_WORDING[agent])
        print(fill((ROOT / "prompts" / f"{name}.md").read_text(), values), end="")
    elif command == "check":
        schema, path = args
        try:
            check_schema(load(path), load(schema))
        except (ValueError, OSError) as e:
            raise SystemExit(f"whooper: {path} does not fit {schema}: {e}")
    elif command == "posts":
        dump, oldest = args
        print(json.dumps(slack_posts(load(dump), oldest), indent=2))
    elif command == "asks":
        config, (posts, answer, oldest) = load_config(), args
        slack = config["slack"]
        try:
            asks, problems = read_asks(load(posts), load(answer), oldest, slack["ask_channel"], slack["user_id"],
                                       slack["workspace_url"], config["github"]["orgs"])
        except ValueError as e:
            raise SystemExit(f"whooper-poll: {e}; see {answer}")
        for problem in problems:
            print(f"whooper-poll: {problem}", file=sys.stderr)
        print(json.dumps(asks, indent=2))
    elif command == "items":
        github, asks, slack_prs = args
        login = load_config()["user"]["github_login"]
        print(json.dumps({"items": poll_items(login, load(github), load(asks), load(slack_prs), datetime.now(timezone.utc))}, indent=2))
    elif command == "select":
        data = Path(args[0])
        # Poll outputs are named by their UTC start time, so the last name is the newest. The poll's
        # records beside them (<time>.answer.json, <time>.transcript.json) do not end in Z.json.
        newest = max((data / "poll").glob("[0-9]*Z.json"), default=None)
        if newest is None:
            raise SystemExit(f"whooper-select: no poll output in {data / 'poll'}")
        work, drops = select_work(load(newest)["items"], data / "prs", datetime.now().astimezone())
        for item, reason in drops:
            print(f"whooper-select: dropped {item['repo']}#{item['pr']} {item['kind']}: {reason}", file=sys.stderr)
        for item in work:
            print(json.dumps(item))
    elif command == "conversation":
        result, *as_of = args
        when = datetime.fromisoformat(as_of[0]) if as_of else None
        print(json.dumps(conversation(load(result)["data"]["repository"]["pullRequest"], when), indent=2))
    elif command == "record":
        data, item, status = args
        write_record(Path(data) / "prs", load(item), status, datetime.now(timezone.utc))
    elif command == "classify":
        # Not resolve(): the run's commands name its directory as whooper-review printed it, and on
        # macOS a directory under $TMPDIR resolves to another path through /private.
        data, run = Path(args[0]), Path(os.path.abspath(args[1]))
        if not (run / "findings.json").exists():
            raise SystemExit(f"whooper-classify: {run} has no findings.json, so its review did not finish")
        # verdicts.json is written last, so a run that has it already added its findings to the digest.
        if (run / "verdicts.json").exists():
            raise SystemExit(f"whooper-classify: {run} is already classified")
        item = load(run / "run.json")
        # A run from before run.json recorded its agent was a claude run.
        agent = item.get("agent", "claude")
        if agent not in SHELL_CALLS:
            raise SystemExit(f"whooper-classify: {run} ran on {agent}, which has no transcript reader")
        result = load(run / "findings.json")
        verdicts = classify(result, SHELL_CALLS[agent](run), (run / "github.json").exists(), run)
        append_digest(data, run, item, [v for v in verdicts if v["verdict"] != "auto_post"])
        write_record(data / "prs", item, "done", datetime.now(timezone.utc), result["approval"])
        (run / "verdicts.json").write_text(json.dumps(verdicts, indent=2))
        print(f"approval: {result['approval']['verdict']}: {result['approval']['reason']}")
        for v in verdicts:
            print(f"{v['verdict']}: finding {v['id']} at {v['path']}:{v['line']}: {v['title']}")
            for check in v["checks_failed"]:
                print(f"    {check}")
    elif command == "post":
        data, run, head_now, comments = Path(args[0]), Path(args[1]).resolve(), args[2], load(args[3])
        if not (run / "verdicts.json").exists():
            raise SystemExit(f"whooper-post: {run} is not classified; run bin/whooper-classify first")
        # post.json is written last, so a run that has it already moved its held findings to the digest.
        if (run / "post.json").exists():
            raise SystemExit(f"whooper-post: {run} was already handled; see its post.json")
        item = load(run / "run.json")
        if head_now != item["head_sha"]:
            write_record(data / "prs", item, "stale", datetime.now(timezone.utc))
            (run / "post.json").write_text(json.dumps({"status": "stale", "head_now": head_now, "posted": [], "held": []}))
            print(f"stale: the PR's head is now {head_now}, so nothing posts")
            return
        # A run ID is the run's UTC start time and the PR number.
        started = datetime.strptime(item["run_id"].split("-")[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        posts, held = hold_back(load(run / "verdicts.json"), comments, started, load_config()["github"]["orgs"])
        append_digest(data, run, item, held)
        request_file = run / "post-request.json"
        if posts:
            request_file.write_text(json.dumps(review_request(item["head_sha"], posts), indent=2))
        (run / "post.json").write_text(json.dumps({
            "status": "printed" if posts else "nothing to post",
            "request": str(request_file) if posts else None,
            "posted": [{"id": p["id"], "title": p["title"], "path": p["path"], "line": p["line"]} for p in posts],
            "held": [{"id": h["id"], "title": h["title"], "reasons": h["checks_failed"]} for h in held],
        }, indent=2))
        for h in held:
            print(f"held: finding {h['id']} at {h['path']}:{h['line']}: {', '.join(h['checks_failed'])}")
        print(f"printed the request to {request_file}" if posts else "nothing to post")
    elif command == "summary":
        sys.stdout.write(summary_text(Path(args[0]).resolve()))
    elif command == "skills":
        print(json.dumps(skill_hashes(args[0])))
    else:
        raise SystemExit(f"whooper.py: unknown command {command!r}")


if __name__ == "__main__":
    main(*sys.argv[1:])
