# /// script
# requires-python = ">=3.11"
# dependencies = ["tomlkit==0.15.2"]
# ///
# uv builds the environment lib/python runs Whooper's Python in from this block, with the versions
# whooper.py.lock pins. Pin only releases at least a week old.
"""Whooper's exact rules: which PRs are the user's review work, which of it is already done, what a
review run may read about a PR, which of a run's findings pass rule v1, what a review the user
submits posts, and when whooper-run polls.

Every identifiable value, such as the user, the GitHub orgs, the Slack channels, and the bots, comes
from the user's config file (config_path), never from this code. The bin/ scripts call it as:
  whooper.py env                                                                   -> shell variables
  whooper.py repo <owner/repo>                                                     -> the repo's shell variables
  whooper.py prompt poll | review <owner/repo> <agent>                             -> a prompt, filled in
                    | ask <agent> <run dir> <question> [<finding-id>]              -> an ask run's prompt, with the
                                                                                      record files it may read
  whooper.py check <schema.json> <value.json>                                      -> fails unless value fits schema
  whooper.py posts <slackdump channel.json> <oldest>                               -> the channel's posts JSON
  whooper.py asks <posts.json> <answer.json> <oldest>                              -> asks JSON
  whooper.py items <github.json> <asks.json> <slack-prs.json>                      -> {"items": [...]}
  whooper.py select <data dir>                                                     -> one work item per line, and
                                                                                      prunes the recheck list
  whooper.py conversation <conversation.graphql result> [<as-of time>]             -> a run's github.json
  whooper.py record <data dir> <work-item.json> running|failed                     -> writes the PR record
  whooper.py classify <data dir> <run dir>                                         -> verdicts, digest, record
  whooper.py post <data dir> <run dir> <head now> <review comments.json>          -> post.json, digest, record
  whooper.py draft <data dir> <run-id> [<change>]                                  -> the run's drafts JSON
  whooper.py submit <data dir> <run dir> <head now> [--print] [--allow-mentions] [--approve-anyway]
                    [--expect=<fingerprint>]                                       -> submit-request.json, submit.json
  whooper.py summary <run dir>                                                     -> the summary channel's text
  whooper.py skills <skills dir>                                                   -> {skill: content hash}
  whooper.py next-round [<Unix time the last round ended>]                         -> the next round's Unix time
  whooper.py status <data dir>                                                     -> whooper-status's JSON
  whooper.py settings get | set <json>                                             -> settings JSON | saves the given ones
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib

import tomlkit

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
# The lookbehind keeps email addresses and paths from counting as mentions. A match is the whole
# handle, such as @alice or the team @example-org/reviewers, so whooper-submit can list it.
MENTION = re.compile(r"(?<![\w.@/])@[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9_-]+)?")
# Each comment whooper-submit posts starts with this line and a blank line: an AI wrote the comment,
# and the user read it and chose to post it.
VETTED = "AI comment (vetted)"
# The message of an approval the user left without one, when it includes comments: the author need
# not wait for the user, and the comments still ask for a look.
PRE_APPROVAL = "Pre-approving to unblock"
# whooper-submit's exit codes for the refusals Prompting Time handles each its own way: the PR has
# new commits since the review, the review approves a run that is not safe to approve, it
# @-mentions someone, and its request is not the one --print showed. Any other failure exits 1, or
# with gh's or jq's own code, and neither uses 10 to 13.
NEW_COMMITS_EXIT, NOT_SAFE_EXIT, MENTIONS_EXIT, CHANGED_EXIT = 10, 11, 12, 13
# The review skills open each draft with its Conventional Comments label, such as **nit:** or
# **suggestion (non-blocking):**, and whooper-submit keeps the label a comment opens with. A comment
# that opens with none, such as one the user rewrote, gets the label of its finding's kind.
LABEL = re.compile(r"\*\*[A-Za-z]+(?: \([^)]*\))?:\*\*")
LABELS = {"caller_visible_bug": "**issue:**", "ci_failure": "**issue:**", "nit": "**nit:**",
          "question": "**question:**", "other": "**suggestion:**"}
LINK = re.compile(r"(?:https?://|www\.)[^\s)>\]]*")
# How prompts/review.md names each agent's tools, limits, and way of returning the findings.
# prompts/ask.md uses skill_tool too.
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


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# whooper-run's schedule for each key the config's [schedule] leaves out.
SCHEDULE_DEFAULTS = {"interval_minutes": 15, "start": "10:00", "end": "18:00", "days": ["mon", "tue", "wed", "thu", "fri"]}
# whooper-poll's time limit for a poll run when the config's [poll] leaves out timeout_minutes.
POLL_TIMEOUT_MINUTES = 10
# whooper-ask's time limit for a run that answers a question about a review, when the config's [ask]
# leaves out timeout_minutes.
ASK_TIMEOUT_MINUTES = 10


def read_schedule(config):
    """The config's [schedule], with SCHEDULE_DEFAULTS for what it leaves out and start and end as
    times of day. Fails on a schedule that next_round cannot follow."""
    values = {**SCHEDULE_DEFAULTS, **config.get("schedule", {})}
    try:
        start, end = (datetime.strptime(values[key], "%H:%M").time() for key in ("start", "end"))
    except (TypeError, ValueError):
        raise SystemExit(f'whooper: [schedule] start and end take a 24-hour time such as "18:00"; see {config_path()}')
    if unknown := [day for day in values["days"] if day not in DAYS]:
        raise SystemExit(f"whooper: [schedule] days takes names from {', '.join(DAYS)}, not {unknown}")
    # Each poll runs a model, so 0 or less would poll nonstop. bool is an int in Python, so true would
    # otherwise pass as 1.
    if type(values["interval_minutes"]) is not int or values["interval_minutes"] < 1:
        raise SystemExit(f"whooper: [schedule] interval_minutes takes a whole number of minutes, at least 1; see {config_path()}")
    if not values["days"] or start >= end:
        raise SystemExit(f"whooper: [schedule] needs at least one day and an end after its start; see {config_path()}")
    return {**values, "start": start, "end": end}


def next_round(schedule, earliest):
    """The first time at or after earliest when whooper-run may start a round: on one of the
    schedule's days, at or after its start and before its end. A round that starts before the end
    may run past it.

    Times are local and carry no zone, so after a daylight saving change the rounds still start at
    the configured hour; the caller turns the result into a Unix time with the zone's rules.
    """
    for offset in range(8):
        day = earliest.date() + timedelta(days=offset)
        # Today counts only while earliest is before today's end.
        if DAYS[day.weekday()] in schedule["days"] and (offset > 0 or earliest.time() < schedule["end"]):
            return max(earliest, datetime.combine(day, schedule["start"]))


def read_status(data):
    """Whooper's status for whooper-status --json, from the files under data, its state directory:
    whether whooper-run and a round are running, whether Whooper is paused, when the next scheduled
    round starts, and how the last round ended. Times are local, in ISO 8601 with the offset. It
    reads files and asks ps about the two lock holders, and never calls GitHub, Slack, or a model."""
    def read(path):
        # Whooper renames each of these files into place, so a read gets a whole file or none.
        try:
            return path.read_text()
        except FileNotFoundError:
            return None

    def holder(lock, name):
        # The rule whooper-run and whooper-round apply to a lock they find: a lock whose process is
        # gone, or is now some other program, holds nothing.
        try:
            pid = int(read(lock / "pid") or "")
        except ValueError:
            return None
        ps = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
        return pid if name in ps.stdout else None

    def local(unix):
        return datetime.fromtimestamp(unix, timezone.utc).astimezone().isoformat(timespec="seconds")

    loop, current = holder(data / "run.lock", "whooper-run"), holder(data / "round.lock", "whooper-round")
    # whooper-run writes next-round before each wait and deletes it when the round is due.
    next_round = read(data / "next-round") if loop else None
    record = json.loads(read(data / "round.lock" / "round.json") or "null") if current else None
    last = json.loads(read(data / "last-round.json") or "null")
    return {
        "version": 1,
        "loop": {"running": bool(loop), "pid": loop},
        "paused": (data / "paused").exists(),
        "next_round": local(int(next_round)) if next_round else None,
        "round": {"running": bool(current), "started_at": local(record["started_at"]) if record else None,
                  "reviewing": record["reviewing"] if record else None},
        "last_round": {**last, "started_at": local(last["started_at"]), "ended_at": local(last["ended_at"])}
                      if last else None,
    }


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
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    # Python's True is also an int, but JSON's true is not a number.
    if kind and (not isinstance(value, types[kind]) or (kind in ("integer", "number") and isinstance(value, bool))):
        raise ValueError(f"{where} is not of type {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{where} is {value!r}, not one of {schema['enum']}")
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool) and value < schema["minimum"]:
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


def check_jobs(config):
    """Fails unless each job in the config's [jobs] names an agent with a launcher for it,
    agents/<agent>/<job>."""
    for job, agent in config["jobs"].items():
        if not (ROOT / "agents" / agent / job).exists():
            raise SystemExit(f"whooper: [jobs] {job} names {agent!r}, which has no agents/{agent}/{job}")


# The settings a settings form may edit, as a JSON Schema.
SETTINGS_SCHEMA = ROOT / "schemas" / "settings.json"


def read_settings(config):
    """The config's values for the settings in SETTINGS_SCHEMA, with Whooper's defaults for what the
    config leaves out, such as SCHEDULE_DEFAULTS. A setting with neither is left out."""
    values = {**config, "schedule": {**SCHEDULE_DEFAULTS, **config.get("schedule", {})},
              "poll": {"timeout_minutes": POLL_TIMEOUT_MINUTES, **config.get("poll", {})},
              "ask": {"timeout_minutes": ASK_TIMEOUT_MINUTES, **config.get("ask", {})}}

    def pick(value, schema):
        # Only what the schema names, at every level.
        if "properties" not in schema:
            return value
        return {key: pick(value[key], sub) for key, sub in schema["properties"].items() if key in value}

    return pick(values, json.loads(SETTINGS_SCHEMA.read_text()))


def write_settings(values):
    """Saves values, part of the settings in SETTINGS_SCHEMA such as {"schedule": {"interval_minutes": 30}},
    in the config file. Only the lines of those keys change, keeping their comments, and a table the file
    lacks is added. Nothing is written unless the values fit the schema and the config as it would be
    saved passes the checks Whooper runs when it starts."""
    try:
        check_schema(values, json.loads(SETTINGS_SCHEMA.read_text()), "settings")
    except ValueError as e:
        raise SystemExit(f"whooper-settings: {e}")

    def merge(table, changes):
        for key, value in changes.items():
            if isinstance(value, dict):
                if key not in table:
                    table[key] = tomlkit.table()
                merge(table[key], value)
            # Setting a key rewrites its line, which drops comments inside a list, so a value that did
            # not change keeps its line as it is.
            elif table.get(key) != value:
                table[key] = value

    # A config that links into a dotfiles folder stays a link, and the file it names changes.
    path = config_path().resolve()
    if not path.exists():
        raise SystemExit(f"whooper-settings: no config at {path}")
    document = tomlkit.parse(path.read_text())
    merge(document, values)
    text = tomlkit.dumps(document)
    merged = tomllib.loads(text)
    read_schedule(merged)
    check_jobs(merged)
    # Nothing locks the file, so of two saves at the same moment the later one drops the other's
    # change; add a lock if a running whooper-run starts saving settings too.
    write_file(path, text)


def write_file(path, text, exclusive=False):
    """Writes text to path through a temporary file beside it, which then replaces it, so a failure
    leaves the old file whole and a program watching the folder, such as Prompting Time, never reads
    half a file. A file that already exists keeps its mode. With exclusive, the write raises
    FileExistsError when path exists, so of two writers only the first succeeds."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(text)
        if exclusive:
            # Unlike a rename, a link fails when the name is taken.
            os.link(temporary, path)
            os.unlink(temporary)
        else:
            if path.exists():
                shutil.copymode(path, temporary)
            os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


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
            # A team ask does not ask the user again about a PR the user already reviewed.
            if not ask["tagged_user"] and user_reviews(pr, login):
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


def select_work(items, prs_dir, now, repos, rechecks=()):
    """Splits work items into work to start and (item, reason) for items already handled or
    unreviewable, and returns the recheck-list items that no longer wait, for the caller to delete.

    items are the newest poll's, and rechecks the recheck list's (mark_new_commits). prs_dir holds the
    records write_record writes, and repos is the config's [repos] table. An item is dropped when
    repos has no table for its repo, since a review needs the repo's settings, or when its record is
    running or done or has failed MAX_FAILURES times. A re-check is dropped when its PR already has
    MAX_RECHECKS_PER_DAY re-check records for other heads started on now's local day.

    The limit holds a recheck-list item too, counting the records of the item's own kind, and an item
    it drops stays on the list for the next day. A recheck-list item no longer waits once items list
    its PR, since they carry the head the PR has now, or once its record is done or has failed
    MAX_FAILURES times. Nothing here reads GitHub, so a listed PR merged, closed, or reviewed on
    GitHub since the mark still gets its review, at the head the mark saw; have whooper-poll fetch
    the listed PRs if such reviews come to waste runs.
    """
    listed = {(i["repo"], i["pr"]) for i in items}
    finished = [r for r in rechecks if (r["repo"], r["pr"]) in listed]
    waiting = [r for r in rechecks if (r["repo"], r["pr"]) not in listed]
    work, drops = [], []
    for item, from_list in [(i, False) for i in items] + [(r, True) for r in waiting]:
        path = record_path(prs_dir, item)
        pr_dir = path.parent
        record = json.loads(path.read_text()) if path.exists() else None
        reason = None
        if item["repo"] not in repos:
            reason = f"the config has no [repos.\"{item['repo']}\"] table"
        elif record and record["status"] == "running":
            reason = "running at this head"
        elif record and (record["status"] == "done" or record["failures"] >= MAX_FAILURES):
            reason = "done at this head" if record["status"] == "done" else f"failed {record['failures']} times at this head"
            # No review starts at this head again, so the list has nothing left to bring back.
            if from_list:
                finished.append(item)
        elif item["kind"] == "recheck" or from_list:
            started = [
                datetime.fromisoformat(json.loads(p.read_text())["started_at"]).astimezone(now.tzinfo).date()
                for p in pr_dir.glob(f"*.{item['kind']}.json") if p != path
            ]
            if started.count(now.date()) >= MAX_RECHECKS_PER_DAY:
                reason = f"{MAX_RECHECKS_PER_DAY} re-checks already today"
        if reason:
            drops.append((item, reason))
        else:
            work.append(item)
    return work, drops, finished


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


def mark_new_commits(data, item, head_now, now):
    """Records that a review run's PR has new commits since the run, for whooper-post and
    whooper-submit, which post nothing then: the reviewed head's record says stale, and the recheck
    list under data holds the run's work item at head_now, so the next round's whooper-select reviews
    the new commits whatever brought the PR in. Without the list only a PR GitHub still lists would
    come back, since the poll reads each Slack post once. item is the run's run.json. A later mark of
    the same PR replaces this one, so the list holds the newest head either command saw."""
    write_record(data / "prs", item, "stale", now)
    path = recheck_path(data, item)
    path.parent.mkdir(parents=True, exist_ok=True)
    work = {key: item[key] for key in ("kind", "repo", "pr", "head_sha", "base_ref", "source", "slack", "reason")}
    write_file(path, json.dumps(work | {"head_sha": head_now}))


def recheck_path(data, item):
    """The recheck list's file for the item's PR: rechecks/<owner>-<repo>-<pr>.json under data."""
    return data / "rechecks" / f"{item['repo'].replace('/', '-')}-{item['pr']}.json"


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


def review_request(head_sha, event, body, comments):
    """The body of the one POST /repos/<owner>/<repo>/pulls/<pr>/reviews call that posts a review at
    head_sha: event is APPROVE or COMMENT, body is the review's message, and comments are (finding,
    comment body) pairs, each posted on its finding's line."""
    return {
        "commit_id": head_sha,
        "event": event,
        "body": body,
        "comments": [{"path": f["path"], "line": f["line"], "side": f["side"], "body": text} for f, text in comments],
    }


def read_drafts(run_dir):
    """The user's choices for a review run, from <run dir>/drafts.json, which bin/whooper-draft
    writes: the review's event, APPROVE or COMMENT, and message, and per finding ID whether its
    comment is included and, when the user edited it, the comment's text.

    Until the user changes anything, a run that run_concerns finds nothing against starts as an
    approval with every finding, each a nit, included, and any other run as a comment with nothing
    included, so the user decides. Either way the message starts empty."""
    path = run_dir / "drafts.json"
    if path.exists():
        return json.loads(path.read_text())
    result = json.loads((run_dir / "findings.json").read_text())
    if run_concerns(result):
        return {"event": "COMMENT", "message": "", "findings": {}}
    return {"event": "APPROVE", "message": "", "findings": {f["id"]: {"included": True} for f in result["findings"]}}


# Each change bin/whooper-draft makes, with the number of arguments it takes.
DRAFT_CHANGES = {"include": 1, "leave-out": 1, "text": 2, "approve": 0, "comment": 0, "message": 1}
DRAFT_USAGE = ("usage: whooper-draft <run-id> [include <finding-id> | leave-out <finding-id> | text <finding-id> <text>"
               " | approve | comment | message <text>]")


def change_drafts(run_dir, change, *args):
    """Makes one change to a run's drafts and saves them: includes a finding's comment or leaves it
    out, replaces its text, makes the review approve or only comment, or sets its message. Returns
    the drafts. After the run's review is submitted a change would not reach GitHub, so it refuses."""
    if DRAFT_CHANGES.get(change) != len(args):
        raise SystemExit(DRAFT_USAGE)
    if (run_dir / "submit.json").exists():
        raise SystemExit(f"whooper-draft: {run_dir} was already submitted; see its submit.json")
    if change == "text" and not args[1].strip():
        raise SystemExit("whooper-draft: the text is empty; to drop the comment, leave the finding out")
    drafts = read_drafts(run_dir)
    if change in ("include", "leave-out", "text"):
        ids = [f["id"] for f in json.loads((run_dir / "findings.json").read_text())["findings"]]
        if args[0] not in ids:
            raise SystemExit(f"whooper-draft: the run has no finding {args[0]}; its findings are"
                             f" {', '.join(ids) or 'none'}")
        choice = drafts["findings"].setdefault(args[0], {"included": False})
        if change == "text":
            choice["text"] = args[1]
        else:
            choice["included"] = change == "include"
    elif change == "message":
        drafts["message"] = args[0]
    else:
        drafts["event"] = change.upper()
    # Nothing locks drafts.json, so of two changes at the same moment the later one drops the other's;
    # add a lock if Prompting Time and the agent come to change one run's drafts at once.
    write_file(run_dir / "drafts.json", json.dumps(drafts, indent=2))
    return drafts


def vetted_comments(findings, drafts):
    """The comments whooper-submit posts, as (finding, comment body) pairs in findings.json's order:
    one for each finding the drafts include. A body is VETTED, a blank line, and the user's text or
    else the finding's draft, opened with its label."""
    comments = []
    for f in findings:
        choice = drafts["findings"].get(f["id"], {})
        if not choice.get("included"):
            continue
        # Leading blanks would hide the label, and four spaces would make the first line code.
        text = choice.get("text", f["draft"]).strip()
        if not LABEL.match(text):
            text = f"{LABELS[f['kind']]} {text}"
        comments.append((f, f"{VETTED}\n\n{text}"))
    return comments


def run_concerns(result):
    """What in a review run's findings.json argues against approving the PR, as lines for
    whooper-submit to list: a review the run skipped, with the run's reason; a verdict other than
    approve_once_ci_passes, with its reason; and each finding that is not a nit. A run with none is
    safe to approve: its drafts start as an approval (read_drafts), and whooper-submit approves it
    without --approve-anyway."""
    approval, concerns = result["approval"], []
    # A run that skipped the PR, as prompts/review.md has it skip a large PR from an untagged ask,
    # read too little to vouch for it, whatever its verdict says.
    if result["status"] == "skipped":
        concerns.append(f"the review was skipped: {result['skip_reason']}")
    if approval["verdict"] != "approve_once_ci_passes":
        concerns.append(f"the run's verdict is {approval['verdict']}: {approval['reason']}")
    return concerns + [f"finding {f['id']} is a {f['kind']}: {f['title']} ({f['path']}:{f['line']})"
                       for f in result["findings"] if f["kind"] != "nit"]


def refuse(code, message):
    """Stops whooper-submit with message on stderr and code, one of the exit codes Prompting Time
    tells apart, such as NOT_SAFE_EXIT."""
    print(message, file=sys.stderr)
    raise SystemExit(code)


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


# The files of a review run's record that an ask run's prompt lists, in this order, with what each
# holds. A file the run directory lacks is left out: only a review whooper-review stopped leaves
# failure.txt, a Claude review leaves transcript.jsonl and a Devin review transcript.json, and
# drafts.json, submit.json, and summary.md come only from the user's choices or a Devin summary.
# failure.txt is here rather than in ASK_FAILURE_RECORD because a Devin review writes findings.json
# itself, and can pass its time limit after writing it.
ASK_RECORD = {
    "failure.txt": "the reason whooper-review gave for stopping the review before it finished",
    "log.md": "the review's log: what the change does, the evidence for each finding, and the candidates it dropped",
    "findings.json": "the review's findings, each with its draft comment, and its approval verdict with the reason",
    "diff.patch": "the pull request's change, git diff <merge base> HEAD",
    "github.json": "the PR's title, body, reviews, and comments, as the review read them",
    "transcript.jsonl": "the review's whole conversation, with every command it ran and that command's output",
    "transcript.json": "the review's whole conversation, with every command it ran and that command's output",
    "run.json": "the work item: the PR, why it is the user's work, and the head and merge-base commits",
    "prompt.md": "the instructions the review followed",
    "verdicts.json": "which findings Whooper would post on its own, and the checks each other finding failed",
    "post.json": "what Whooper's own posting would post, and what it held back and why",
    "drafts.json": "the user's choices for the review: which comments go in, edited texts, approve or comment, and the message",
    "submit.json": "the review the user submitted to GitHub",
    "summary.md": "Whooper's summary of the review",
}
# The files an ask run's prompt also lists for a review that did not finish, and so left no
# findings.json: its agent's output, which may say why, and the findings whooper-review set aside
# because they did not fit the schema. Only a Devin review leaves stdout.log.
ASK_FAILURE_RECORD = {
    "stderr.log": "the error output of the review's agent, which may say why the review did not finish",
    "stdout.log": "the rest of the review agent's output",
    "findings.invalid.json": "the findings the review wrote, which did not fit Whooper's findings schema",
}


def ask_facts(run_dir, question, finding_id=None):
    """The end of the prompt of a run that answers question about the review in run_dir: the files of
    the review's record it may read, each with its path, earlier asks about the review among them, and
    for a review that did not finish, its agent's output and the findings that did not fit the schema
    too; the finding the question is about when finding_id names one; and the question. None of it
    goes through fill, so braces in the question stay as written."""
    finished = (run_dir / "findings.json").exists()
    record = ASK_RECORD if finished else {**ASK_RECORD, **ASK_FAILURE_RECORD}
    lines = ["", "", "## The review's record", "", f"The review's run directory is {run_dir}. You may read these files:", ""]
    lines += [f"- {run_dir / name}: {what}" for name, what in record.items() if (run_dir / name).exists()]
    # bin/whooper-ask numbers its answers asks/1.json, asks/2.json, and so on; a Devin ask's config
    # sits beside them as asks/<n>.devin-config.json.
    asks = sorted((p for p in (run_dir / "asks").glob("*.json") if p.stem.isdigit()), key=lambda p: int(p.stem))
    lines += [f"- {p}: an earlier question about this review, with its answer" for p in asks]
    if finding_id is not None:
        # A review whooper-review stopped before its schema check, such as at the time limit or for an
        # empty file, leaves findings.json as the run wrote it.
        try:
            findings = ({f["id"]: f for f in json.loads((run_dir / "findings.json").read_text())["findings"]}
                        if finished else {})
        except (ValueError, KeyError, TypeError):
            raise SystemExit(f"whooper-ask: the run has no finding {finding_id}; its findings.json holds no"
                             " findings Whooper can read")
        if finding_id not in findings:
            raise SystemExit(f"whooper-ask: the run has no finding {finding_id}; its findings are"
                             f" {', '.join(findings) or 'none'}")
        f = findings[finding_id]
        lines += ["", "## The finding", "",
                  f"The question is about finding {f['id']}, {f['title']} ({f['path']}:{f['line']}), in findings.json."]
    lines += ["", "## The question", "", question]
    return "\n".join(lines) + "\n"


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
        check_jobs(config)
        poll_timeout = config.get("poll", {}).get("timeout_minutes", POLL_TIMEOUT_MINUTES)
        # A poll takes 2 to 3 minutes, so a shorter limit would stop every poll.
        if type(poll_timeout) is not int or poll_timeout < 5:
            raise SystemExit(f"whooper: [poll] timeout_minutes takes a whole number of minutes, at least 5; see {config_path()}")
        # whooper-ask's watchdog multiplies the limit in Python, so a fraction of a minute works too.
        ask_timeout = config.get("ask", {}).get("timeout_minutes", ASK_TIMEOUT_MINUTES)
        if type(ask_timeout) not in (int, float) or ask_timeout <= 0:
            raise SystemExit(f"whooper: [ask] timeout_minutes takes a number of minutes above 0; see {config_path()}")
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
            # A poll run that hangs, such as on a request the network dropped, holds up the loop.
            "WHOOPER_POLL_TIMEOUT_MINUTES": poll_timeout,
            "WHOOPER_ASK_TIMEOUT_MINUTES": ask_timeout,
            # The script whooper-round runs after each review, relative to the config's directory.
            "WHOOPER_NOTIFY": config["dir"] / config["schedule"]["notify"] if "notify" in config.get("schedule", {}) else "",
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
        facts = ""
        if name == "review":
            repo, agent = rest
            notes = config["repos"][repo].get("notes")
            values["repo_notes"] = (config["dir"] / notes).read_text().strip() if notes else "None."
            values.update(REVIEW_WORDING[agent])
        elif name == "ask":
            agent, run, question, *finding = rest
            values["skill_tool"] = REVIEW_WORDING[agent]["skill_tool"]
            facts = ask_facts(Path(run), question, *finding)
        print(fill((ROOT / "prompts" / f"{name}.md").read_text(), values) + facts, end="")
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
        rechecks = [load(path) for path in sorted((data / "rechecks").glob("*.json"))]
        work, drops, finished = select_work(load(newest)["items"], data / "prs", datetime.now().astimezone(),
                                            load_config().get("repos", {}), rechecks)
        for item, reason in drops:
            print(f"whooper-select: dropped {item['repo']}#{item['pr']} {item['kind']}: {reason}", file=sys.stderr)
        # Nothing locks the list, so a mark of the same PR written between the read above and this
        # delete is lost; add a lock if submits during a round's selection come to lose reviews.
        for item in finished:
            recheck_path(data, item).unlink(missing_ok=True)
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
            mark_new_commits(data, item, head_now, datetime.now(timezone.utc))
            (run / "post.json").write_text(json.dumps({"status": "stale", "head_now": head_now, "posted": [], "held": []}))
            print(f"stale: the PR's head is now {head_now}, so nothing posts; the next round reviews the new commits")
            return
        # A run ID is the run's UTC start time and the PR number.
        started = datetime.strptime(item["run_id"].split("-")[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        posts, held = hold_back(load(run / "verdicts.json"), comments, started, load_config()["github"]["orgs"])
        append_digest(data, run, item, held)
        request_file = run / "post-request.json"
        if posts:
            # Whooper's own review only comments: it never approves or requests changes.
            body = f"{MARKER} review. Each comment reports a failure Whooper reproduced at this commit and not at the merge base."
            request = review_request(item["head_sha"], "COMMENT", body, [(p, f"{MARKER}\n\n{p['draft']}") for p in posts])
            request_file.write_text(json.dumps(request, indent=2))
        (run / "post.json").write_text(json.dumps({
            "status": "printed" if posts else "nothing to post",
            "request": str(request_file) if posts else None,
            "posted": [{"id": p["id"], "title": p["title"], "path": p["path"], "line": p["line"]} for p in posts],
            "held": [{"id": h["id"], "title": h["title"], "reasons": h["checks_failed"]} for h in held],
        }, indent=2))
        for h in held:
            print(f"held: finding {h['id']} at {h['path']}:{h['line']}: {', '.join(h['checks_failed'])}")
        print(f"printed the request to {request_file}" if posts else "nothing to post")
    elif command == "draft":
        if len(args) < 2:
            raise SystemExit(DRAFT_USAGE)
        run, change = Path(args[0]) / "runs" / args[1], args[2:]
        if not (run / "run.json").exists():
            raise SystemExit(f"whooper-draft: no run {args[1]} in {run.parent}")
        # Drafts start from the run's findings (read_drafts), which only a finished review has.
        if not (run / "findings.json").exists():
            raise SystemExit(f"whooper-draft: {run} has no findings.json, so its review did not finish")
        print(json.dumps(change_drafts(run, *change) if change else read_drafts(run), indent=2))
    elif command == "submit":
        data, run, head_now, flags = Path(args[0]), Path(args[1]).resolve(), args[2], args[3:]
        # bin/whooper-submit writes submit.json saying posting before it posts, and adds GitHub's
        # response after, so a run that has the file may have its review on GitHub.
        if (run / "submit.json").exists():
            if load(run / "submit.json")["status"] == "posting":
                raise SystemExit(f"whooper-submit: another submit of {run} is posting or did not finish, so its review"
                                 " may be on GitHub; check the PR, then delete submit.json to submit again")
            raise SystemExit(f"whooper-submit: {run} was already submitted; see its submit.json")
        if not (run / "findings.json").exists():
            raise SystemExit(f"whooper-submit: {run} has no findings.json, so its review did not finish")
        item, result = load(run / "run.json"), load(run / "findings.json")
        if head_now != item["head_sha"]:
            mark_new_commits(data, item, head_now, datetime.now(timezone.utc))
            refuse(NEW_COMMITS_EXIT, f"whooper-submit: nothing posted: the PR has new commits since the review (head"
                   f" {item['head_sha'][:12]}, now {head_now[:12]}). Whooper reviews them in its next round;"
                   " bin/whooper-round runs that round now.")
        drafts = read_drafts(run)
        comments = vetted_comments(result["findings"], drafts)
        if not comments and drafts["event"] == "COMMENT":
            raise SystemExit("whooper-submit: nothing to submit: the drafts include no comment and do not approve;"
                             " change them with bin/whooper-draft")
        message = drafts["message"]
        if drafts["event"] == "APPROVE" and not message.strip():
            message = PRE_APPROVAL if comments else ""
        # An approval lets the PR merge under the user's name, so whatever in the run argues against
        # it waits for the user's own look.
        if drafts["event"] == "APPROVE" and "--approve-anyway" not in flags:
            if concerns := run_concerns(result):
                refuse(NOT_SAFE_EXIT, "whooper-submit: nothing posted: the review approves, and the run is not safe to"
                       " approve:\n" + "".join(f"  {c}\n" for c in concerns)
                       + "Pass --approve-anyway to approve it anyway, or make the review only comment with"
                       " bin/whooper-draft.")
        # A mention notifies someone under the user's name, so each one waits for the user's approval.
        mentions = [f"{m} in the comment on {f['path']}:{f['line']} (finding {f['id']})"
                    for f, body in comments for m in MENTION.findall(body)]
        mentions += [f"{m} in the message" for m in MENTION.findall(message)]
        if mentions and "--allow-mentions" not in flags:
            refuse(MENTIONS_EXIT, "whooper-submit: nothing posted: the review @-mentions someone, and posting would"
                   " notify them:\n" + "".join(f"  {m}\n" for m in mentions) + "Pass --allow-mentions to post it anyway.")
        request = review_request(item["head_sha"], drafts["event"], message, comments)
        text = json.dumps(request, indent=2)
        # The fingerprint names this exact request, so the submit Prompting Time runs after its
        # confirmation, with --expect, posts nothing the user did not see.
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        request_file = run / "submit-request.json"
        expect = next((flag.removeprefix("--expect=") for flag in flags if flag.startswith("--expect=")), None)
        if expect is not None and expect != fingerprint:
            refuse(CHANGED_EXIT, f"whooper-submit: nothing posted: the drafts changed since --print showed the review,"
                   f" so its fingerprint is now {fingerprint}, not {expect}. Run with --print to show it again.")
        # Only now, so a submit refused above leaves the file as the confirmation showed it.
        write_file(request_file, text)
        if "--print" in flags:
            print(f"printed the request to {request_file}; nothing posted")
            print(f"fingerprint: {fingerprint}")
            return
        # bin/whooper-submit posts the request next. Only one submit can create submit.json, so of two
        # at once only one posts.
        claim = json.dumps({"status": "posting", "request": request}, indent=2)
        try:
            write_file(run / "submit.json", claim, exclusive=True)
        except FileExistsError:
            raise SystemExit(f"whooper-submit: {run} was already submitted; see its submit.json")
    elif command == "summary":
        sys.stdout.write(summary_text(Path(args[0]).resolve()))
    elif command == "skills":
        print(json.dumps(skill_hashes(args[0])))
    elif command == "settings":
        if args == ("get",):
            print(json.dumps(read_settings(load_config()), indent=2))
        elif len(args) == 2 and args[0] == "set":
            try:
                values = json.loads(args[1])
            except json.JSONDecodeError as e:
                raise SystemExit(f"whooper-settings: the values are not JSON: {e}")
            write_settings(values)
        else:
            raise SystemExit("usage: whooper-settings get | set <json>")
    elif command == "next-round":
        schedule = read_schedule(load_config())
        # A round starts interval_minutes after the last one ended, or at once when whooper-run starts.
        earliest = (datetime.fromtimestamp(int(args[0])) + timedelta(minutes=schedule["interval_minutes"])
                    if args else datetime.now())
        when = next_round(schedule, earliest)
        if when > earliest:
            print(f"whooper-run: outside the [schedule] hours; next round at {when:%a %Y-%m-%d %H:%M}", file=sys.stderr)
        print(int(when.timestamp()))
    elif command == "status":
        print(json.dumps(read_status(Path(args[0])), indent=2))
    else:
        raise SystemExit(f"whooper.py: unknown command {command!r}")


if __name__ == "__main__":
    main(*sys.argv[1:])
