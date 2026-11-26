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
  whooper.py cause <run dir> <step> [<step's error output>]                         -> failure.json, the cause
  whooper.py overview <run dir>                                                    -> overview.json, for an overview
                                                                                      findings.json holds, and
                                                                                      overview.invalid.json, for an
                                                                                      overview.json read_overview drops
  whooper.py retry <data dir> <run-id> <head now>                                  -> queues another review
  whooper.py wait <data dir> <work-item.json> <failure.json>                      -> a waiting run, the recheck list
  whooper.py skip <data dir> <run-id> [--undo]                                    -> marks the run skipped, or not
  whooper.py remove <data dir> <run-id> [--undo]                                  -> marks the run removed, or not
  whooper.py classify <data dir> <run dir>                                         -> verdicts, digest, record
  whooper.py post <data dir> <run dir> <head now> <review comments.json>          -> post.json, digest, record
  whooper.py draft <data dir> <run-id> [<change>]                                  -> the run's drafts JSON
  whooper.py submit <data dir> <run dir> <head now> [--print] [--allow-mentions] [--approve-anyway]
                    [--expect=<fingerprint>]                                       -> submit-request.json, submit.json
  whooper.py summary <run dir>                                                     -> the summary channel's text
  whooper.py skills <skills dir>                                                   -> {skill: content hash}
  whooper.py next-round [<Unix time the last round ended>]                         -> the next round's Unix time
  whooper.py add-round <data dir> <round JSON>                                     -> adds it to the round history
  whooper.py status <data dir>                                                     -> whooper-status's JSON
  whooper.py show <data dir> <run-id>                                              -> whooper-show's JSON
  whooper.py settings get | set <json>                                             -> settings JSON | saves the given ones
"""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
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


def local_time(unix):
    """A Unix time as whooper-status and whooper-show print times: local, in ISO 8601 with the offset."""
    return datetime.fromtimestamp(unix, timezone.utc).astimezone().isoformat(timespec="seconds")


def read_status(data):
    """Whooper's status for whooper-status --json, from the files under data, its state directory:
    whether whooper-run and a round are running, whether Whooper is paused, when the next scheduled
    round starts, how the last round ended, what blocks every review (read_blocked), the reviews of
    the last REVIEW_DAYS days (read_reviews), and the last ROUNDS_KEPT rounds (add_round), newest first. Times are local, in ISO 8601 with the
    offset. It reads files, asks ps about the two
    lock holders, and checks that the process of each review's lock exists, and never calls GitHub,
    Slack, or a model."""
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

    loop, current = holder(data / "run.lock", "whooper-run"), holder(data / "round.lock", "whooper-round")
    # whooper-run writes next-round before each wait and deletes it when the round is due.
    next_round = read(data / "next-round") if loop else None
    record = json.loads(read(data / "round.lock" / "round.json") or "null") if current else None
    last = json.loads(read(data / "last-round.json") or "null")
    rounds = [json.loads(line) for line in (read(data / "rounds.jsonl") or "").splitlines()]
    reviews = read_reviews(data, datetime.now(timezone.utc))
    return {
        "version": 1,
        "loop": {"running": bool(loop), "pid": loop},
        "paused": (data / "paused").exists(),
        "next_round": local_time(int(next_round)) if next_round else None,
        "round": {"running": bool(current), "started_at": local_time(record["started_at"]) if record else None,
                  "reviewing": record["reviewing"] if record else None},
        "last_round": {**last, "started_at": local_time(last["started_at"]), "ended_at": local_time(last["ended_at"])}
                      if last else None,
        "blocked": read_blocked(reviews),
        "reviews": reviews,
        "rounds": [{**r, "started_at": local_time(r["started_at"]),
                    "ended_at": local_time(r["ended_at"]) if r["ended_at"] is not None else None}
                   for r in reversed(rounds)],
    }


def read_blocked(reviews):
    """What stops every review, for whooper-status's blocked, from reviews, newest first, as
    read_reviews lists them: while the newest review that ended or waits stopped, or waits, at a cause
    whose scope is all, that cause; since, when the first of the reviews just before it that stopped
    or wait at the same cause ended, or started for one that waits; and waiting, how many PRs' newest
    review waits for the next round, which one the user removed does not, since the round holds it
    back (select_work). None otherwise, as once a review succeeds, or stops at a cause of its PR's own,
    which shows that the cause no longer holds. A review that runs has not shown either yet, so it is
    passed over."""
    ended = [r for r in reviews if r["outcome"] != "running"]
    cause = ended[0]["cause"] if ended else None
    if cause is None or cause["scope"] != "all":
        return None
    first = ended[0]
    for review in ended:
        if review["cause"] is None or review["cause"]["id"] != cause["id"]:
            break
        first = review
    newest, waiting = set(), 0
    for review in reviews:
        if ((review["repo"], review["pr"]) not in newest and review["outcome"] == "waiting"
                and not review["removed_by_user"]):
            waiting += 1
        newest.add((review["repo"], review["pr"]))
    return {**cause, "since": first["ended_at"] or first["started_at"], "waiting": waiting}


# whooper-status lists the last 50 rounds, which is all rounds.jsonl keeps.
ROUNDS_KEPT = 50


def add_round(data, entry):
    """Adds entry, a round that ended or one whooper-run skipped, to data/rounds.jsonl, the history
    whooper-status prints as rounds: one JSON object a line, oldest first, the last ROUNDS_KEPT. The
    file is rewritten through a rename, so a program watching the folder never reads half of it, and
    under the state folder's lock, so of a round run by hand and a round whooper-run skips that end at
    the same moment, neither drops the other's line."""
    path = data / "rounds.jsonl"
    with folder_lock(data):
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        write_file(path, "".join(f"{line}\n" for line in [*lines, json.dumps(entry)][-ROUNDS_KEPT:]))


# whooper-status lists the review runs that started in the last 30 days.
REVIEW_DAYS = 30


def parse_run_id(run_id):
    """The UTC start time and the PR number in a review run's ID, which whooper-review makes of the
    two, as in 20310727T010246Z-12, or None for a name that is not a run ID."""
    m = re.fullmatch(r"([0-9]{8}T[0-9]{6}Z)-([0-9]+)", run_id)
    if not m:
        return None
    try:
        started = datetime.strptime(m[1], "%Y%m%dT%H%M%SZ")
    except ValueError:
        # Digits that make no date, such as month 13.
        return None
    return started.replace(tzinfo=timezone.utc), int(m[2])


def running_runs(data):
    """The IDs of the review runs that a live whooper-review still runs, by whooper-ask's rule: the PR's
    lock under data/locks names the run in run_id, and the process in its pid exists. A lock whose
    process is gone, as after a kill -9, is stale. The locks are read for the runs they name, so a
    review counts as running before it writes run.json, which names its PR and so its lock."""
    running = set()
    for lock in (data / "locks").glob("*"):
        try:
            pid, run_id = int((lock / "pid").read_text()), (lock / "run_id").read_text().strip()
            # Signal 0 only checks that the process exists, as whooper-ask's kill -0 does.
            os.kill(pid, 0)
        except (OSError, ValueError):
            continue
        running.add(run_id)
    return running


def read_object(path):
    """The JSON object in the file at path, or None when the file is missing or holds no JSON object,
    such as a run.json that whooper-review left empty or one cut short."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# What stops a review, by the cause's ID: a summary for Prompting Time, and whether the cause stops
# every review ("all"), as Docker not running does for the setup scripts that start containers, or
# only its PR's ("pr").
CAUSES = {
    "docker_unavailable": ("Docker isn't running", "all"),
    "setup_failed": ("The repo's setup script failed", "all"),
    "gh_signed_out": ("gh isn't signed in to GitHub", "all"),
    "repo_not_configured": ("Whooper isn't set up for this repo", "pr"),
    "agent_no_findings": ("The review agent left no usable findings", "pr"),
    "time_limit": ("The review ran past its time limit", "pr"),
    "unknown": ("The review stopped unexpectedly", "pr"),
}
# What the docker CLI prints when it cannot reach the Docker daemon: Docker Desktop's and colima's
# "Cannot connect to the Docker daemon at unix://... Is the docker daemon running?", and newer
# clients' "failed to connect to the docker API at unix://...", as with OrbStack.
DOCKER_DOWN = re.compile(r"Cannot connect to the Docker daemon|failed to connect to the docker API"
                         r"|Is the docker daemon running", re.IGNORECASE)
# What git and gh print when the user's GitHub login is missing or no longer works: git's messages
# when the credential helper, gh, gave no login and there is no terminal to ask on, or GitHub refused
# the token, gh's advice to run gh auth login, and the API's Bad credentials.
GH_SIGNED_OUT = re.compile(r"could not read Username for 'https://github\.com'|Invalid username or token"
                           r"|Authentication failed for 'https://github\.com/|gh auth login|Bad credentials",
                           re.IGNORECASE)
# The cause of each step of whooper-review whose cause does not depend on what the step printed.
STEP_CAUSES = {"clone": "repo_not_configured", "time-limit": "time_limit", "findings": "agent_no_findings"}


def failure_cause(step, output=""):
    """What stopped a review, as {id, summary, scope} (CAUSES), from step, the step of whooper-review
    it stopped at, and output, that step's error output: the setup script's failure is
    docker_unavailable when it could not reach Docker and setup_failed otherwise, and the fetch's
    failure is gh_signed_out when GitHub refused it for want of a login. The checkout missing from the
    cache, the time limit, and an agent that left no findings that fit name their own causes, and any
    other step, such as an interrupt, is unknown."""
    if step == "setup":
        cause = "docker_unavailable" if DOCKER_DOWN.search(output) else "setup_failed"
    elif step == "fetch":
        cause = "gh_signed_out" if GH_SIGNED_OUT.search(output) else "unknown"
    else:
        cause = STEP_CAUSES.get(step, "unknown")
    summary, scope = CAUSES[cause]
    return {"id": cause, "summary": summary, "scope": scope}


# How whooper-review worded the first line of failure.txt at each step it stops at with its own
# reason, which names the step of a review from before it saved failure.json. The setup script's
# reason, "<script> failed", now ends with the last line the script printed on stderr.
FAILURE_TEXTS = (
    ("clone", re.compile(r"^no clone of ")),
    ("time-limit", re.compile(r"^the review run passed its \d+-minute limit")),
    ("findings", re.compile(r"^the review run exited \S+ without findings|^the review run's findings\.json does not fit")),
    ("setup", re.compile(r"^\S.* failed(: |$)")),
)


def read_cause(run, outcome):
    """What kept the review in run from finishing, as failure_cause gives it, for a failed or waiting
    review, and None for any other outcome: the cause whooper-review saved in failure.json as it
    stopped the review, or for one that waits, the cause in waiting.json (wait_review). Without that
    file, as for a review from before whooper-review saved it, failure.txt names the step by its
    wording (FAILURE_TEXTS), and its text is the step's output, so a review with neither, as after a
    kill -9, is unknown."""
    if outcome not in ("failed", "waiting"):
        return None
    saved = read_object(run / ("waiting.json" if outcome == "waiting" else "failure.json")) or {}
    if isinstance(saved.get("id"), str) and isinstance(saved.get("summary"), str) and saved.get("scope") in ("all", "pr"):
        return {key: saved[key] for key in ("id", "summary", "scope")}
    try:
        text = (run / "failure.txt").read_text(errors="replace")
    except OSError:
        text = ""
    first = text.partition("\n")[0]
    return failure_cause(next((step for step, wording in FAILURE_TEXTS if wording.search(first)), None), text)


# The empty file whooper-skip leaves in the directory of a run the user marked skipped: the review
# is done without being submitted, and needs the user no more.
SKIPPED_BY_USER = "skipped-by-user"
# The empty file whooper-remove leaves in the directory of a run the user removed from their lists:
# the review needs the user no more, and Whooper does not retry it on its own (remove_review).
REMOVED_BY_USER = "removed-by-user"


def read_review(run, running):
    """How the review in the run directory run went, as whooper-status and whooper-show report it:
    run.json's work item, the outcome, the findings, the review whooper-submit posted, for a failed or
    waiting review, the cause (read_cause), whether the user marked it skipped (skip_review), whether
    the user removed it (remove_review), and whether it is safe to approve: reviewed, with nothing
    run_concerns argues against approving, as whooper-submit then approves it without
    --approve-anyway. running holds the IDs of the runs a live whooper-review still runs
    (running_runs). A file that is missing, empty, cut short, or does not fit reads as None, so a run
    directory Whooper cannot read gives a failed review rather than an error.

    The outcome is running while a live whooper-review runs the review; waiting for a review
    whooper-round did not start, because an earlier review in its round stopped at a cause that stops
    every review (wait_review); reviewed once the review ended with findings that fit FINDINGS_SCHEMA
    and without failure.txt, the reason whooper-review saves when it stops a review; and failed
    otherwise. The findings are None while the review runs, since a
    Devin review writes findings.json itself and may change it until it ends, and when they do not
    fit, as whooper-draft then shows no drafts. The posted review is submit.json once it says posted;
    a submit.json that says posting may or may not be on GitHub, and whooper-submit asks the user to
    check the PR."""
    if run.name in running:
        outcome, result = "running", None
    elif (run / "waiting.json").exists():
        outcome, result = "waiting", None
    else:
        result = read_object(run / "findings.json")
        try:
            # A findings.json that is missing or holds no object reads as None, which does not fit either.
            check_schema(result, json.loads(FINDINGS_SCHEMA.read_text()))
        except ValueError:
            result = None
        outcome = "reviewed" if result is not None and not (run / "failure.txt").exists() else "failed"
    submit = read_object(run / "submit.json")
    posted = submit if submit and submit.get("status") == "posted" else None
    # whooper-submit refuses a review that runs, which may still change its findings.json, and
    # run_concerns lists a failure, as at the time limit, whatever findings the review left, so this is
    # true exactly when whooper-submit approves the review without --approve-anyway.
    safe = outcome == "reviewed" and not run_concerns(run, result)
    return (read_object(run / "run.json"), outcome, result, posted, read_cause(run, outcome),
            (run / SKIPPED_BY_USER).exists(), (run / REMOVED_BY_USER).exists(), safe)


def read_title_and_author(run):
    """The PR's title and its author's login in the run's github.json, the PR conversation the review
    read, since run.json has neither. Each is None when the file is missing, as when the review's
    GitHub fetch failed, or does not hold it as text, as when the file is cut short."""
    github = read_object(run / "github.json") or {}
    pr = github["pr"] if isinstance(github.get("pr"), dict) else {}
    return [pr.get(key) if isinstance(pr.get(key), str) else None for key in ("title", "author")]


def polled_heads(data):
    """The head of each PR, by (repo, number), that Whooper's latest poll saw, whooper-fetch's
    poll/github.json and the newest poll's items, or the recheck list holds, as {(repo, number): (time,
    head)}, each dated by when its file was written, and of two for one PR the newer. A file that is
    missing or does not read as JSON gives no heads."""
    heads = {}

    def note(when, repo, pr, head):
        if isinstance(repo, str) and isinstance(pr, int) and isinstance(head, str):
            if (repo, pr) not in heads or when >= heads[(repo, pr)][0]:
                heads[(repo, pr)] = (when, head)

    def stamped(path):
        try:
            return path.stat().st_mtime, json.loads(path.read_text())
        except (OSError, ValueError):
            return 0, None

    when, prs = stamped(data / "poll" / "github.json")
    for pr in prs if isinstance(prs, list) else []:
        try:
            note(when, pr["repository"]["nameWithOwner"], pr["number"], pr["headRefOid"])
        except (KeyError, TypeError):
            continue
    items = []
    # Poll outputs are named by their UTC start time, so the last name is the newest, as in select.
    if newest := max((data / "poll").glob("[0-9]*Z.json"), default=None):
        when, poll = stamped(newest)
        items += [(when, item) for item in (poll.get("items") if isinstance(poll, dict) else None) or []]
    items += [stamped(path) for path in (data / "rechecks").glob("*.json")]
    for when, item in items:
        if isinstance(item, dict):
            note(when, item.get("repo"), item.get("pr"), item.get("head_sha"))
    return heads


def pr_head(data, polled, repo, pr):
    """The newest head Whooper knows of repo's PR pr: of its head in polled (polled_heads) and the heads
    its records under data/prs name, the one Whooper learned last, or None when none names it. A record
    dates its head by its started_at, since a later status, such as stale, keeps the head it started
    with."""
    known = [polled[(repo, pr)]] if (repo, pr) in polled else []
    for path in (data / "prs" / f"{repo.replace('/', '-')}-{pr}").glob("*.json"):
        record = read_object(path) or {}
        try:
            known.append((datetime.fromisoformat(record["started_at"]).timestamp(), path.name.split(".")[0]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(known)[1] if known else None


def read_reviews(data, now):
    """The review runs under data/runs that started in the REVIEW_DAYS days before now, newest first, as
    whooper-status lists them. A review's PR number and start come from its run ID, its repo from
    run.json, its title and author from github.json, and the rest from read_review. Only the newest
    run of each PR can need the user, since a newer run supersedes the older ones, even while it runs
    or waits, and one that runs or waits needs no one yet, nor one the user marked skipped or removed.
    A name in runs that is not a run ID, such as Finder's .DS_Store, is no run. Each review also gets
    the head it reviewed, from run.json, and the newest head Whooper knows of its PR (pr_head), which
    differ once the PR has new commits since the review."""
    def modified(path):
        try:
            return local_time(path.stat().st_mtime)
        except OSError:
            return None

    runs = []
    for run in sorted((data / "runs").glob("*"), reverse=True):
        parsed = parse_run_id(run.name)
        if parsed and parsed[0] >= now - timedelta(days=REVIEW_DAYS) and run.is_dir():
            runs.append((run, *parsed))
    # whooper-review takes the PR's lock before it makes the run directory, so the locks, read after
    # the runs are listed, name every listed run whose review still runs.
    running = running_runs(data)
    polled = polled_heads(data)
    # runs is newest first, so a PR, by repo and number, is in newer once its newest run is listed, and
    # its older runs no longer need the user. A run without a readable run.json has repo None, which
    # matches only other runs without one.
    reviews, newer = [], set()
    for run, started, pr in runs:
        item, outcome, result, submit, cause, skipped, removed, safe = read_review(run, running)
        title, author = read_title_and_author(run)
        repo = item["repo"] if item else None
        head = item.get("head_sha") if item and isinstance(item.get("head_sha"), str) else None
        # No file records when a review ended, so it ended when whooper-review saved failure.txt as it
        # stopped the review, or else when the run wrote findings.json as it ended.
        ended = None if outcome == "running" else modified(run / "failure.txt") or modified(run / "findings.json")
        reviews.append({
            "run_id": run.name,
            "repo": repo,
            "pr": pr,
            "title": title,
            "author": author,
            "url": f"https://github.com/{repo}/pull/{pr}" if repo else None,
            "started_at": local_time(started.timestamp()),
            "ended_at": ended,
            "outcome": outcome,
            "verdict": result["approval"]["verdict"] if result else None,
            "skipped": result is not None and result["status"] == "skipped",
            "findings": len(result["findings"]) if result else 0,
            "safe_to_approve": safe,
            # whooper-submit renames submit.json saying posted into place once GitHub took the review.
            "submitted": {"event": submit["request"]["event"], "at": modified(run / "submit.json")}
                         if submit else None,
            "needs_you": ((repo, pr) not in newer and outcome not in ("running", "waiting") and submit is None
                          and not skipped and not removed),
            "cause": cause,
            "head_sha": head,
            "pr_head_sha": pr_head(data, polled, repo, pr) if repo else None,
            "skipped_by_user": skipped,
            "removed_by_user": removed,
        })
        newer.add((repo, pr))
    return reviews


# The flags of the git diff that writes a review's diff.patch in bin/whooper-review, which keep it a
# plain patch whatever the user's git config says: no external diff tool, textconv filter, or color,
# a/ and b/ before the paths, and a submodule's commit lines rather than its log. rebuilt_diff passes
# the same ones, so keep the two lists the same.
PLAIN_DIFF = ("--no-ext-diff", "--no-textconv", "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--submodule=short")


def repo_clone(repo):
    """The partial clone of repo that reviews check out from, WHOOPER_REPO_CLONE:
    whooper/repos/<owner>-<name> under $XDG_CACHE_HOME (default ~/.cache)."""
    return xdg_dir("XDG_CACHE_HOME", ".cache") / "repos" / repo.replace("/", "-")


def read_clone(repo, *args):
    """What git <args> prints in repo's clone (repo_clone), or None when git fails, as when the clone
    lacks an object args need or does not exist. --no-lazy-fetch keeps git from fetching an object the
    partial clone lacks, so this only reads the clone. As in diff.patch, a file in an encoding other
    than UTF-8 leaves bytes that read as U+FFFD."""
    git = subprocess.run(["git", "--git-dir", str(repo_clone(repo) / ".git"), "--no-lazy-fetch", *args],
                         capture_output=True)
    return git.stdout.decode(errors="replace") if git.returncode == 0 else None


def rebuilt_diff(item):
    """The diff a review read, for a run from before whooper-review kept it as diff.patch: git diff
    between the merge base and the head that item, the run's run.json, records, in the repo's clone
    (read_clone), with whooper-review's PLAIN_DIFF flags, or None without run.json or its merge base,
    as for a review that waits, and when the clone lacks either commit or one of their files. The clone
    has no checkout, so git reads .gitattributes from the head commit, which whooper-review's diff read
    from the head worktree: a file marked -diff shows as binary."""
    if item is None or not item.get("merge_base"):
        return None
    base, head = item["merge_base"], item["head_sha"]
    return read_clone(item["repo"], f"--attr-source={head}", "diff", *PLAIN_DIFF, base, head)


# A hunk header of a patch git diff wrote: the hunk's first line in the old file and how many lines it
# spans there, then the same in the new file. A count git leaves out is 1.
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# The first line of each file's part of such a patch, with the file's two paths, each quoted when git
# quotes it, read as the page's diff reads it.
DIFF_HEADER = re.compile(r'diff --git ("a/(?:[^"\\]|\\.)*"|a/.+?) ("b/(?:[^"\\]|\\.)*"|b/.+?)$')


def diff_path(text, prefix=""):
    """A path as a patch git diff wrote names it, without prefix, a/ or b/, and without the quotes and C
    escapes git gives a path with unusual characters, as in "a/caf\\303\\251.md" for a/café.md; None
    for /dev/null, the side an added or deleted file lacks."""
    if text.startswith('"'):
        text = text[1:-1].encode().decode("unicode_escape").encode("latin-1").decode(errors="replace")
    return None if text == "/dev/null" else text.removeprefix(prefix)


def diff_files(patch):
    """The files a patch git diff wrote changes, such as a run's diff.patch, as Whooper's page reads
    them: each as (path, old path, lines), where path is the file's path in the head, or for a deleted
    file at the merge base, old path its path at the merge base, None for an added file, and lines the
    first and last line of each hunk, counted in the head, or for a deleted file at the merge base. A
    file whose diff shows no lines, such as a binary file, a pure rename, or a mode change, has none.
    The ---, +++, rename, and copy lines name a file's paths; the first line names them only for a file
    without those, since a path with a space and " b/" in it reads two ways there."""
    files = []
    for part in re.split(r"^(?=diff --git )", patch, flags=re.M)[1:]:
        first, *lines = part.splitlines()
        names = DIFF_HEADER.match(first)
        old, new = (diff_path(names[1], "a/"), diff_path(names[2], "b/")) if names else (None, None)
        added = deleted = False
        hunks = []
        # git ends the path on a --- or +++ line with a tab when the path has a space.
        for line in lines:
            if hunk := HUNK.match(line):
                hunks.append([int(n) if n is not None else 1 for n in hunk.groups()])
            elif hunks:
                # A line of a hunk, which may itself start with --- or +++.
                continue
            elif line.startswith("--- "):
                old = diff_path(line[4:].split("\t")[0], "a/")
            elif line.startswith("+++ "):
                new = diff_path(line[4:].split("\t")[0], "b/")
            elif line.startswith(("rename from ", "copy from ")):
                old = diff_path(line.split(" from ", 1)[1])
            elif line.startswith(("rename to ", "copy to ")):
                new = diff_path(line.split(" to ", 1)[1])
            elif line.startswith("new file mode"):
                added = True
            elif line.startswith("deleted file mode"):
                deleted = True
        spans = []
        for old_start, old_count, new_start, new_count in hunks:
            # A deleted file's lines count at the merge base, every other file's in the head.
            start, count = (old_start, old_count) if deleted else (new_start, new_count)
            if count:
                spans.append((start, start + count - 1))
        files.append((old if deleted else new, None if added else old, spans))
    return files


# The JSON Schema the overview.json a review run writes must fit, the first rule check_overview checks.
OVERVIEW_SCHEMA = ROOT / "schemas" / "overview.json"


def check_overview(overview, patch):
    """Raises ValueError at the first rule of prompts/review.md's overview that the overview a review
    run wrote breaks, checked against patch, the run's diff.patch: it fits OVERVIEW_SCHEMA, which allows
    at most 5 places and one flow; each place's lines and each layer range lie inside one hunk of a file
    the patch changes (diff_files); and each file the patch shows lines of is in exactly one layer. A
    file whose diff shows no lines, such as a binary file, has no lines a range could name, so it is in
    no layer."""
    check_schema(overview, json.loads(OVERVIEW_SCHEMA.read_text()), "overview")
    hunks = {path: spans for path, _, spans in diff_files(patch)}
    ranges = [(f"overview.places[{i}]", place) for i, place in enumerate(overview["places"])]
    ranges += [(f"overview.layers[{i}].ranges[{j}]", r) for i, layer in enumerate(overview["layers"])
               for j, r in enumerate(layer["ranges"])]
    for where, r in ranges:
        if r["path"] not in hunks:
            raise ValueError(f"{where} names {r['path']}, which the diff does not change")
        if not any(first <= r["start_line"] <= r["end_line"] <= last for first, last in hunks[r["path"]]):
            raise ValueError(f"{where}, lines {r['start_line']} to {r['end_line']} of {r['path']}, is not inside"
                             " one hunk of the diff")
    for path, spans in hunks.items():
        layers = [f"overview.layers[{i}]" for i, layer in enumerate(overview["layers"])
                  if any(r["path"] == path for r in layer["ranges"])]
        if spans and len(layers) != 1:
            raise ValueError(f"{path} is in {' and '.join(layers) or 'no layer'}")


def read_overview(run_dir, patch):
    """Reads the overview the review run in run_dir wrote in overview.json, and returns it with why
    Whooper drops it, or with None when the overview keeps every rule check_overview checks against
    patch, the run's diff. A file that is not JSON gives its text, and one that cannot be read, such as
    one that is not UTF-8, gives None. Without the file, both are None."""
    try:
        text = (run_dir / "overview.json").read_text()
    except FileNotFoundError:
        return None, None
    # read_text raises a ValueError for text that is not UTF-8.
    except (OSError, ValueError) as e:
        return None, f"overview.json cannot be read: {e}"
    try:
        overview = json.loads(text)
    except ValueError as e:
        return text, f"overview.json is not JSON: {e}"
    try:
        check_overview(overview, patch)
    except ValueError as e:
        return overview, str(e)
    return overview, None


def clone_log(repo, *args):
    """The commits git log <args> lists in repo's clone (read_clone), newest first, each as its sha,
    title, author's name, time (local_time), and the files it changed, or None when git fails, as when
    the clone lacks a commit args name. A rename lists as a delete and an add, since finding renames
    takes file contents that a partial clone lacks."""
    log = read_clone(repo, "log", "-z", "--no-renames", "--name-only", "--format=//%H%x00%s%x00%an%x00%at", *args)
    if log is None:
        return None
    # -z ends each field and each file's path with a NUL, and puts a newline before a commit's first path.
    # No path starts with /, so // marks where a commit starts, and its other fields come after it.
    commits, fields = [], iter(log.split("\0"))
    for field in fields:
        if field.startswith("//"):
            commits.append({"sha": field[2:], "title": next(fields), "author": next(fields),
                            "at": local_time(int(next(fields))), "files": []})
        elif field:
            commits[-1]["files"].append(field.removeprefix("\n"))
    return commits


# whooper-show's co_change: a file outside the PR counts when it changed in at least CO_CHANGE_SHARE of
# the commits of one of the PR's files in the CO_CHANGE_DAYS days before the review started, and in at
# least CO_CHANGE_COMMITS of them; whooper-show lists at most CO_CHANGE_MAX.
CO_CHANGE_DAYS, CO_CHANGE_SHARE, CO_CHANGE_COMMITS, CO_CHANGE_MAX = 90, 0.6, 3, 5


def co_change(commits, paths):
    """The files outside paths, the PR's files, that changed together with one of them in commits, the
    set of files each commit changed: each with the PR's file it changed with most (partner), the share
    of that file's commits it changed in too (ratio), and in how many (commits). A file counts from a
    ratio of CO_CHANGE_SHARE and CO_CHANGE_COMMITS commits; at most CO_CHANGE_MAX of them, highest ratio
    first, then most commits, then by path."""
    changes, together = Counter(), Counter()
    for files in commits:
        for partner in files & paths:
            changes[partner] += 1
            for path in files - paths:
                together[path, partner] += 1
    rows = [{"path": path, "partner": partner, "ratio": n / changes[partner], "commits": n}
            for (path, partner), n in together.items()]
    best = {}
    # In this order each file's first row is its best, and the files come as whooper-show lists them.
    for row in sorted(rows, key=lambda row: (-row["ratio"], -row["commits"], row["path"], row["partner"])):
        if row["commits"] >= CO_CHANGE_COMMITS and row["ratio"] >= CO_CHANGE_SHARE:
            best.setdefault(row["path"], row)
    return list(best.values())[:CO_CHANGE_MAX]


def show_review(data, run_id):
    """One review run for Prompting Time's review screen, as whooper-show --json prints it:
    run.json, the outcome, the findings, whether it is safe to approve, and the posted review as
    read_review reads them, the PR's title and author in github.json, the drafts whooper-draft prints,
    which only a review whose findings fit has, diff.patch, or for a run from before reviews kept it,
    the diff rebuilt_diff rebuilds, failure.txt, and whooper-ask's answers. The overview the run wrote
    in overview.json comes once the review ended, and only when read_overview keeps it against that
    diff, as whooper-review did. From the repo's clone, without fetching (clone_log): the commits
    between the head the review read and the PR's newest head Whooper knows (pr_head), and the files
    that changed together with the PR's in the CO_CHANGE_DAYS days before the review started, in the
    history the PR's merge base ends (co_change). Refuses a run ID that names no run directory."""
    run, parsed = data / "runs" / run_id, parse_run_id(run_id)
    # A run ID names a directory right under runs, so a path such as ../x reads nothing outside it.
    if parsed is None or not run.is_dir():
        raise SystemExit(f"whooper-show: no run {run_id} in {data / 'runs'}")
    started, pr = parsed
    item, outcome, result, submit, cause, skipped, removed, safe = read_review(run, running_runs(data))
    title, author = read_title_and_author(run)

    def text(name):
        # git copies a file's bytes into diff.patch as they are, so a file in an encoding other than
        # UTF-8 leaves bytes that do not decode, which read as U+FFFD.
        try:
            return (run / name).read_text(errors="replace")
        except FileNotFoundError:
            return None

    diff = text("diff.patch")
    if diff is None:
        diff = rebuilt_diff(item)
    # whooper-review drops an overview.json that breaks a rule as soon as the run ends, but a run that
    # still runs may still change the file, and a review stopped before then, as at an interrupt, can
    # leave one it never checked.
    overview, dropped = read_overview(run, diff or "") if outcome != "running" else (None, None)
    repo = item["repo"] if item else None
    head = item.get("head_sha") if item and isinstance(item.get("head_sha"), str) else None
    newest = pr_head(data, polled_heads(data), repo, pr) if repo else None
    if head is None or newest is None:
        since = None
    elif newest == head:
        since = []
    else:
        # A merge, such as of the base branch into the PR, lists as one commit, with the files it brought.
        since = clone_log(repo, "--first-parent", f"{head}..{newest}")
    # The PR's files, by their paths in the head and at the merge base, so a renamed file's history counts.
    paths = {path for new, old, _ in diff_files(diff or "") for path in (new, old) if path}
    history = (clone_log(repo, f"--since={(started - timedelta(days=CO_CHANGE_DAYS)).isoformat()}", "--no-merges",
                         item["merge_base"]) if paths and item and item.get("merge_base") else None)
    return {
        "version": 1,
        "run_id": run_id,
        "run": item,
        "title": title,
        "author": author,
        "outcome": outcome,
        "findings": result,
        "safe_to_approve": safe,
        "drafts": read_drafts(run) if result else None,
        "diff": diff,
        "failure": text("failure.txt"),
        "asks": [json.loads(path.read_text()) for path in ask_files(run)],
        "submitted": submit,
        "cause": cause,
        "head_sha": head,
        "skipped_by_user": skipped,
        "removed_by_user": removed,
        "overview": None if dropped else overview,
        "commits_since_review": since,
        "co_change": co_change([set(commit["files"]) for commit in history or []], paths),
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
    Whooper's schemas use: type, one or a list of them, enum, minimum, pattern, properties, required,
    additionalProperties, items, minItems, and maxItems."""
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool,
             "null": type(None)}
    # Python's True is also an int, but JSON's true is not a number.
    if kinds and (not any(isinstance(value, types[kind]) for kind in kinds)
                  or isinstance(value, bool) and "boolean" not in kinds):
        raise ValueError(f"{where} is not of type {' or '.join(kinds)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{where} is {value!r}, not one of {schema['enum']}")
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool) and value < schema["minimum"]:
        raise ValueError(f"{where} is below {schema['minimum']}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        raise ValueError(f"{where} is {value!r}, which does not match {schema['pattern']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if missing := [key for key in schema.get("required", []) if key not in value]:
            raise ValueError(f"{where} lacks {', '.join(missing)}")
        if schema.get("additionalProperties") is False and (extra := sorted(value.keys() - properties.keys())):
            raise ValueError(f"{where} has unexpected {', '.join(extra)}")
        for key, sub in properties.items():
            if key in value:
                check_schema(value[key], sub, f"{where}.{key}")
    if isinstance(value, list) and len(value) < schema.get("minItems", 0):
        raise ValueError(f"{where} has fewer than {schema['minItems']} items")
    if isinstance(value, list) and len(value) > schema.get("maxItems", len(value)):
        raise ValueError(f"{where} has more than {schema['maxItems']} items")
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
    # Prompting Time's settings form and a terminal can each save at the same moment, so each save
    # reads the file and rewrites it under its folder's lock, and neither drops the other's change.
    with folder_lock(path.parent):
        document = tomlkit.parse(path.read_text())
        merge(document, values)
        text = tomlkit.dumps(document)
        merged = tomllib.loads(text)
        read_schedule(merged)
        check_jobs(merged)
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


@contextmanager
def folder_lock(folder):
    """Holds an exclusive lock on folder for the with block, for code that reads a file in folder and
    writes it back: of two such writers at once, in any of Whooper's processes, the second waits for
    the first and then reads what it wrote, so neither drops the other's change. The lock is flock's
    on the folder itself, which adds no file to the folder, and it ends with the block or the process."""
    fd = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


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


def select_work(items, prs_dir, now, repos, rechecks=(), runs_dir=None):
    """Splits work items into work to start and (item, reason) for items already handled or
    unreviewable, and returns the recheck-list items that no longer wait, for the caller to delete.

    items are the newest poll's, and rechecks the recheck list's (mark_new_commits). prs_dir holds the
    records write_record writes, runs_dir the review runs, and repos is the config's [repos] table. An
    item is dropped when repos has no table for its repo, since a review needs the repo's settings, or
    when its record is running or done or has failed MAX_FAILURES times. A re-check is dropped when its
    PR already has MAX_RECHECKS_PER_DAY re-check records for other heads started on now's local day. A
    record that says retry, which whooper-retry writes when the user asks for another review (retry),
    lets its item through whatever earlier reviews at its head did, and the daily limit does not hold
    it either.

    Whooper does not retry on its own a review the user removed (remove_review): while the newest run
    of an item's PR in runs_dir is marked removed, an item that asks for the same review again
    (removed_request) is dropped, unless its record says retry.

    The limit holds a recheck-list item too, counting the records of the item's own kind, and an item
    it drops stays on the list for the next day. A recheck-list item no longer waits once items list
    its PR, since they carry the head the PR has now, once its record is done or has failed
    MAX_FAILURES times, or once the removal of its PR's review drops it. Nothing here reads GitHub, so
    a listed PR merged, closed, or reviewed on GitHub since the mark still gets its review, at the head
    the mark saw; have whooper-poll fetch the listed PRs if such reviews come to waste runs.
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
        elif record and record["status"] == "retry":
            # The user asked for this review, so neither limit holds it. The review's own record
            # replaces this one when it starts.
            pass
        elif record and (record["status"] == "done" or record["failures"] >= MAX_FAILURES):
            reason = "done at this head" if record["status"] == "done" else f"failed {record['failures']} times at this head"
            # No review starts at this head again, so the list has nothing left to bring back.
            if from_list:
                finished.append(item)
        elif runs_dir and removed_request(runs_dir, item):
            reason = "removed by the user"
            # Whooper does not retry the review on its own, so the PR leaves the list.
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


def removed_request(runs, item):
    """Whether work item item asks again for a review the user removed (remove_review): the newest run
    of its PR under runs is marked removed, and item has that run's head and kind and no Slack ask but
    the run's own. A GitHub review request reads the same in every poll while it stands, so it is no
    new request, and neither is a Slack post the poll reads again, as after an edit. A new Slack post,
    new commits, or another kind, as when the user reviewed the PR and is requested again, is new. A
    request on GitHub made again at the same head reads like the one before, so it does not count."""
    newest = newest_run(runs, item["repo"], item["pr"])
    if newest is None or not (newest[0] / REMOVED_BY_USER).exists():
        return False
    ran = newest[1]
    return ((ran.get("head_sha"), ran.get("kind")) == (item["head_sha"], item["kind"])
            and (item["slack"] is None or item["slack"]["ts"] == (ran.get("slack") or {}).get("ts")))


def newest_run(runs, repo, pr):
    """The newest review run of repo's PR pr under runs, as (its directory, its run.json), or None when
    there is none. Only run.json names a run's repo, so a run without one Whooper can read, as when
    whooper-review stopped before it wrote the file, is no PR's."""
    # Run IDs start with the time and end with the PR number, so the first of them whose run.json names
    # the repo is the PR's newest run.
    for run in sorted(runs.glob(f"*-{pr}"), reverse=True):
        ran = read_object(run / "run.json") if parse_run_id(run.name) else None
        if ran is not None and ran.get("repo") == repo:
            return run, ran
    return None


def record_path(prs_dir, item):
    """The record of work on one head and kind: <owner>-<repo>-<pr>/<head>.<kind>.json under prs_dir."""
    return prs_dir / f"{item['repo'].replace('/', '-')}-{item['pr']}" / f"{item['head_sha']}.{item['kind']}.json"


def write_record(prs_dir, item, status, now, approval=None):
    """Records a review run's status for the item's head and kind.

    running starts a run at now. failed counts one more failure at this head and done keeps the
    count, so whooper-select stops retrying a head after MAX_FAILURES. retry keeps the count too, and
    select_work lets its item through anyway. classify passes the run's approval with done, and later
    statuses for the same head keep it.
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
    list_again(data, item, head_now)


def list_again(data, item, head):
    """Puts the work item of a review run, its run.json item, on the recheck list under data at head,
    replacing the list's item for the PR, if any. It writes under the list folder's lock, which
    whooper-select holds from its read of the list to its deletes, so a mark written meanwhile is not
    deleted with the item it replaces."""
    path = recheck_path(data, item)
    path.parent.mkdir(parents=True, exist_ok=True)
    work = {key: item[key] for key in ("kind", "repo", "pr", "head_sha", "base_ref", "source", "slack", "reason")}
    with folder_lock(path.parent):
        write_file(path, json.dumps(work | {"head_sha": head}))


def run_to_change(data, run_id, command, verb):
    """The directory of the review run run_id under data, for command, such as whooper-skip, which
    changes what the run means: exits with command's message for a run ID that names no run directory,
    and for a review a live whooper-review still runs, as verb, such as skip, says."""
    run = data / "runs" / run_id
    # A run ID names a directory right under runs, so a path such as ../x reads nothing outside it.
    if parse_run_id(run_id) is None or not run.is_dir():
        raise SystemExit(f"{command}: no run {run_id} in {data / 'runs'}")
    if run_id in running_runs(data):
        raise SystemExit(f"{command}: the review is still running; {verb} it once it ends")
    return run


def retry(data, run_id, head_now, now):
    """Queues another review of the PR of the review run run_id under data, for whooper-retry, at
    head_now, the head the PR has now: the run's work item at that head goes on the recheck list, and
    the record at that head says retry, which select_work lets through whatever earlier reviews at
    the head did, since the user asked for this review. A run the user removed (remove_review) is
    unmarked, since the user wants its review back. Refuses a run ID that names no run directory and a
    review a live whooper-review still runs. Returns the line whooper-retry prints."""
    run = run_to_change(data, run_id, "whooper-retry", "retry")
    item = json.loads((run / "run.json").read_text())
    write_record(data / "prs", {**item, "head_sha": head_now}, "retry", now)
    list_again(data, item, head_now)
    queued = (f"queued another review of {item['repo']}#{item['pr']} at {head_now[:12]} for the next round;"
              " bin/whooper-round runs that round now")
    mark = run / REMOVED_BY_USER
    if not mark.exists():
        return queued
    mark.unlink()
    return f"unmarked {run_id} as removed, and {queued}"


def skip_review(data, run_id, undo=False):
    """Marks the review run run_id under data skipped by the user, for whooper-skip, or with undo
    unmarks it: the empty file SKIPPED_BY_USER in its directory, which makes whooper-status count the
    review as done without its being submitted, so it needs the user no more. Nothing else changes,
    the drafts and records included. Refuses a run ID that names no run directory and a review a live
    whooper-review still runs. Returns the line whooper-skip prints."""
    run = run_to_change(data, run_id, "whooper-skip", "skip")
    item = read_object(run / "run.json") or {}
    name = f"{run_id} ({item['repo']}#{item['pr']})" if "repo" in item and "pr" in item else run_id
    mark = run / SKIPPED_BY_USER
    if undo:
        if not mark.exists():
            return f"{name} is not marked as skipped"
        mark.unlink()
        return f"unmarked {name} as skipped"
    if mark.exists():
        return f"{name} is already marked as skipped"
    # The file stays empty, so a program watching the folder has nothing to read half of.
    mark.touch()
    return f"marked {name} as skipped, without submitting it; bin/whooper-skip {run_id} --undo unmarks it"


def remove_review(data, run_id, undo=False):
    """Marks the review run run_id under data removed by the user, for whooper-remove, or with undo
    unmarks it: the empty file REMOVED_BY_USER in its directory. While it is there, whooper-status
    lists the review with needs_you false, so Prompting Time leaves it out of its lists, and Whooper
    does not retry the review on its own: while the run is its PR's newest, select_work drops what asks
    for the same review again and takes the PR off the recheck list, and wait_review leaves the run as
    it is. The run's files and the records stay. Undone, a review that waits and is still its PR's
    newest run goes back on the recheck list unless the list holds its PR: select_work may have taken
    it off, and only the list brings it to a round, as for a Slack ask, which a poll reads once.
    Refuses a run ID that names no run directory and a review a live whooper-review still runs.
    Returns the line whooper-remove prints."""
    run = run_to_change(data, run_id, "whooper-remove", "remove")
    item = read_object(run / "run.json") or {}
    name = f"{run_id} ({item['repo']}#{item['pr']})" if "repo" in item and "pr" in item else run_id
    mark = run / REMOVED_BY_USER
    if undo:
        if not mark.exists():
            return f"{name} is not marked as removed"
        mark.unlink()
        # The run names its PR, so newest_run finds it, or a newer run of the PR, which supersedes it.
        if ((run / "waiting.json").exists() and "repo" in item and not recheck_path(data, item).exists()
                and newest_run(data / "runs", item["repo"], item["pr"])[0] == run):
            list_again(data, item, item["head_sha"])
            return f"unmarked {name} as removed, and put it back on the recheck list for the next round"
        return f"unmarked {name} as removed"
    if mark.exists():
        return f"{name} is already marked as removed"
    # The file stays empty, so a program watching the folder has nothing to read half of.
    mark.touch()
    return f"marked {name} as removed; its files stay, and bin/whooper-remove {run_id} --undo unmarks it"


def wait_review(data, item, cause, agent, now):
    """Records, for whooper-round, that it did not start the review of item, a work item, because an
    earlier review in its round stopped at cause, which stops every review: a run directory under
    data/runs whose waiting.json holds the cause, and whose run.json is the item with its run ID and
    agent, the agent [jobs] review names, which an ask about the run runs on. When the PR's newest run
    already waits at the item's head and kind, as after an earlier round that waited too, it gets the
    cause instead of another directory, unless the user removed that run (remove_review): the item is
    then a new request, which select_work let through, and shows as a review of its own. The item goes
    on the recheck list unless the list holds the PR already, so the next round reviews it even when
    the poll does not list it again, as for a Slack ask, which a poll reads once. No record changes, so
    the item is work as it was. Returns the run directory."""
    runs = data / "runs"
    if not recheck_path(data, item).exists():
        list_again(data, item, item["head_sha"])
    if newest := newest_run(runs, item["repo"], item["pr"]):
        run, ran = newest
        if ((run / "waiting.json").exists() and not (run / REMOVED_BY_USER).exists()
                and (ran.get("head_sha"), ran.get("kind")) == (item["head_sha"], item["kind"])):
            write_file(run / "waiting.json", json.dumps(cause))
            return run
    runs.mkdir(parents=True, exist_ok=True)
    # Two items of one PR, of two kinds, can wait in the same second.
    while (run := runs / f"{now:%Y%m%dT%H%M%SZ}-{item['pr']}").exists():
        now += timedelta(seconds=1)
    # The directory appears whole, through a rename, so whooper-status never lists it without its files,
    # which would read as a failed review that needs the user.
    staging = Path(tempfile.mkdtemp(dir=runs, prefix=f".{run.name}."))
    (staging / "run.json").write_text(json.dumps({**item, "run_id": run.name, "agent": agent}, indent=2))
    (staging / "waiting.json").write_text(json.dumps(cause))
    staging.rename(run)
    return run


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


def read_findings(run_dir):
    """The findings.json of the review in run_dir as whooper-ask reads it, or None when the review left
    no findings.json Whooper can read: one that holds a list of findings, each with an ID, an empty
    list included. whooper-review leaves findings.json as the run wrote it when it stops the review
    before its schema check, such as at the time limit or for an empty file, so only a file that reads
    as findings shows that the review finished. The file need not fit the schema, so that an ask can
    answer about an older review, or one whooper-review stopped after the run wrote part of the file;
    whooper-draft, whooper-submit, and whooper-classify read it with read_valid_findings instead."""
    try:
        result = json.loads((run_dir / "findings.json").read_text())
        # whooper-ask --finding looks findings up by ID.
        {f["id"] for f in result["findings"]}
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None
    return result


# The JSON Schema a review's findings.json must fit, which whooper-review checks when the run ends.
FINDINGS_SCHEMA = ROOT / "schemas" / "findings.json"


def read_valid_findings(run_dir, command):
    """The findings.json of the review in run_dir, for whooper-<command>: draft, submit, or classify,
    which read its fields without checking for each one. Refuses a review that did not finish, which
    left no findings.json or one that does not fit FINDINGS_SCHEMA, naming the first part that does not
    fit. whooper-review leaves findings.json as the run wrote it when it stops the review before its
    schema check, so a Devin review stopped at its time limit can leave findings and little else."""
    schema = json.loads(FINDINGS_SCHEMA.read_text())
    try:
        result = json.loads((run_dir / "findings.json").read_text())
        check_schema(result, schema, "findings.json")
    except FileNotFoundError:
        raise SystemExit(f"whooper-{command}: {run_dir} has no findings.json, so its review did not finish")
    except ValueError as e:
        raise SystemExit(f"whooper-{command}: {run_dir} has a findings.json that does not fit schemas/findings.json,"
                         f" so its review did not finish: {e}")
    return result


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
    if run_concerns(run_dir, result):
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
    # Prompting Time's page, Whooper's agent, and a terminal can each change one run's drafts at the
    # same moment, so each change reads drafts.json and writes it back under the run folder's lock.
    with folder_lock(run_dir):
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


def run_concerns(run_dir, result):
    """What in the review run in run_dir argues against approving the PR, as lines for whooper-submit
    to list: a review that failed, with the reason whooper-review saved in failure.txt as it stopped
    the review; and from result, the run's findings.json, a review the run skipped, with the run's
    reason, a verdict other than approve_once_ci_passes, with its reason, and each finding that is not
    a nit. A run with none is safe to approve: its drafts start as an approval (read_drafts),
    whooper-submit approves it without --approve-anyway, and whooper-status and whooper-show give
    safe_to_approve true once its review is reviewed (read_review)."""
    concerns = []
    # A Devin review writes findings.json itself, so one that passed its time limit or was interrupted
    # can leave findings that fit, though it stopped before it finished.
    try:
        concerns.append(f"the review failed: {(run_dir / 'failure.txt').read_text(errors='replace').strip()}")
    except FileNotFoundError:
        pass
    approval = result["approval"]
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
# failure.txt, a Claude review leaves transcript.jsonl and a Devin review transcript.json,
# drafts.json, submit.json, and summary.md come only from the user's choices or a Devin summary,
# overview.json only from a run that wrote an overview, which overview.invalid.json holds instead when
# whooper-review dropped it, and waiting.json only from a review whooper-round did not start.
# failure.txt is here rather than in ASK_FAILURE_RECORD because a Devin review writes findings.json
# itself, and can pass its time limit after writing it.
ASK_RECORD = {
    "failure.txt": "the reason whooper-review gave for stopping the review before it finished",
    "log.md": "the review's log: what the change does, the evidence for each finding, and the candidates it dropped",
    "findings.json": "the review's findings, each with its draft comment, and its approval verdict with the reason",
    "overview.json": "the review's overview of the change: what it does, where to read closely, and what the review"
                     " checked and did not",
    "overview.invalid.json": "the overview the review wrote that Whooper dropped, and why",
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
    "waiting.json": "why the review has not run yet: the cause that stopped an earlier review in its round",
}
# The files an ask run's prompt also lists for a review that did not finish, one that left no
# findings.json Whooper can read: its agent's output, which may say why, and the findings
# whooper-review set aside because they did not fit the schema. Only a Devin review leaves
# stdout.log.
ASK_FAILURE_RECORD = {
    "stderr.log": "the error output of the review's agent, which may say why the review did not finish",
    "stdout.log": "the rest of the review agent's output",
    "findings.invalid.json": "the findings the review wrote, which did not fit Whooper's findings schema",
}


def ask_files(run_dir):
    """whooper-ask's saved answers about the review in run_dir, asks/1.json, asks/2.json, and so on, in
    number order. A Devin ask's config sits beside them as asks/<n>.devin-config.json."""
    return sorted((p for p in (run_dir / "asks").glob("*.json") if p.stem.isdigit()), key=lambda p: int(p.stem))


def ask_facts(run_dir, question, finding_id=None):
    """The end of the prompt of a run that answers question about the review in run_dir: the files of
    the review's record it may read, each with its path, earlier asks about the review among them, and
    for a review that did not finish, its agent's output and the findings that did not fit the schema
    too; the finding the question is about when finding_id names one; and the question. None of it
    goes through fill, so braces in the question stay as written."""
    result = read_findings(run_dir)
    finished = result is not None
    record = ASK_RECORD if finished else {**ASK_RECORD, **ASK_FAILURE_RECORD}
    # whooper-review fills run.json with one jq, and the file stays empty when that jq fails, as when
    # the skills cannot be hashed. Such a file holds nothing to answer from, and bin/whooper-ask
    # counts it as missing, so the list leaves it out.
    try:
        json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        record = {name: what for name, what in record.items() if name != "run.json"}
    lines = ["", "", "## The review's record", "", f"The review's run directory is {run_dir}. You may read these files:", ""]
    lines += [f"- {run_dir / name}: {what}" for name, what in record.items() if (run_dir / name).exists()]
    lines += [f"- {p}: an earlier question about this review, with its answer" for p in ask_files(run_dir)]
    if finding_id is not None:
        if not finished and (run_dir / "findings.json").exists():
            raise SystemExit(f"whooper-ask: the run has no finding {finding_id}; its findings.json holds no"
                             " findings Whooper can read")
        findings = {f["id"]: f for f in result["findings"]} if finished else {}
        if finding_id not in findings:
            # finding_id is a string, and an ID that does not fit the schema, such as the number 1,
            # never equals it; str lists such IDs too.
            raise SystemExit(f"whooper-ask: the run has no finding {finding_id}; its findings are"
                             f" {', '.join(map(str, findings)) or 'none'}")
        f = findings[finding_id]
        # read_findings takes findings that do not fit the schema, such as those of a review
        # whooper-review stopped before its schema check, so a finding may lack its title, path, or
        # line. The prompt names it with the ones it has, and gives the line only with its path, since
        # a line number alone points nowhere.
        name = f"finding {f['id']}"
        if f.get("title"):
            name += f", {f['title']}"
        if f.get("path"):
            name += f" ({f['path']}:{f['line']})" if f.get("line") else f" ({f['path']})"
        lines += ["", "## The finding", "", f"The question is about {name}, in findings.json."]
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
            "WHOOPER_REPO_CLONE": repo_clone(repo),
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
            values["overview_schema"] = str(OVERVIEW_SCHEMA)
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
        recheck_list = data / "rechecks"
        recheck_list.mkdir(exist_ok=True)
        # list_again writes the list under this lock too, so a mark of a PR that whooper-submit or
        # whooper-post writes while the selection runs, as for new commits, waits for the deletes below
        # instead of being deleted with the item it replaces.
        with folder_lock(recheck_list):
            rechecks = [load(path) for path in sorted(recheck_list.glob("*.json"))]
            work, drops, finished = select_work(load(newest)["items"], data / "prs", datetime.now().astimezone(),
                                                load_config().get("repos", {}), rechecks, data / "runs")
            for item, reason in drops:
                print(f"whooper-select: dropped {item['repo']}#{item['pr']} {item['kind']}: {reason}", file=sys.stderr)
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
    elif command == "cause":
        # whooper-review writes failure.txt first, so a reader that comes between the two still finds
        # the review failed, and names its cause from failure.txt meanwhile.
        run, step, *output = args
        try:
            text = Path(output[0]).read_text(errors="replace") if output else ""
        except FileNotFoundError:
            text = ""
        write_file(Path(run) / "failure.json", json.dumps(failure_cause(step, text)))
    elif command == "overview":
        # whooper-review runs this as soon as the run ends, and the review then goes on as it would without
        # an overview.json, whatever this finds.
        run = Path(args[0])
        # A run that writes its overview inside findings.json, though prompts/review.md asks for overview.json,
        # would fail the findings' schema check, since schemas/findings.json has no overview. So the overview
        # leaves findings.json and moves to overview.json, for the check below, unless the run wrote that file
        # too, which then stays as it is. findings.json is rewritten first, so a failed write of overview.json
        # loses the overview rather than failing the review. It keeps the time the run wrote it, which
        # whooper-status gives as when the review ended.
        path = run / "findings.json"
        findings = read_object(path)
        if findings is not None and "overview" in findings:
            stray = findings.pop("overview")
            written = path.stat()
            write_file(path, json.dumps(findings, indent=2))
            os.utime(path, ns=(written.st_atime_ns, written.st_mtime_ns))
            try:
                write_file(run / "overview.json", json.dumps(stray, indent=2), exclusive=True)
            except FileExistsError:
                print("whooper-review: discarded the overview in the run's findings.json, since the run also wrote"
                      " overview.json", file=sys.stderr)
            else:
                print("whooper-review: moved the overview in the run's findings.json to overview.json", file=sys.stderr)
        overview, dropped = read_overview(run, (run / "diff.patch").read_text(errors="replace"))
        if dropped:
            write_file(run / "overview.invalid.json", json.dumps({"reason": dropped, "overview": overview}, indent=2))
            (run / "overview.json").unlink()
            print(f"whooper-review: dropped the run's overview: {dropped}", file=sys.stderr)
    elif command == "retry":
        data, run_id, head_now = args
        print(retry(Path(data), run_id, head_now, datetime.now(timezone.utc)))
    elif command == "skip":
        data, run_id, *flags = args
        print(skip_review(Path(data), run_id, undo=flags == ["--undo"]))
    elif command == "remove":
        data, run_id, *flags = args
        print(remove_review(Path(data), run_id, undo=flags == ["--undo"]))
    elif command == "wait":
        data, item, failure = Path(args[0]), load(args[1]), load(args[2])
        cause = {key: failure[key] for key in ("id", "summary", "scope")}
        wait_review(data, item, cause, load_config()["jobs"]["review"], datetime.now(timezone.utc))
    elif command == "classify":
        # Not resolve(): the run's commands name its directory as whooper-review printed it, and on
        # macOS a directory under $TMPDIR resolves to another path through /private.
        data, run = Path(args[0]), Path(os.path.abspath(args[1]))
        result = read_valid_findings(run, command)
        # verdicts.json is written last, so a run that has it already added its findings to the digest.
        if (run / "verdicts.json").exists():
            raise SystemExit(f"whooper-classify: {run} is already classified")
        item = load(run / "run.json")
        # A run from before run.json recorded its agent was a claude run.
        agent = item.get("agent", "claude")
        if agent not in SHELL_CALLS:
            raise SystemExit(f"whooper-classify: {run} ran on {agent}, which has no transcript reader")
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
        started, _ = parse_run_id(item["run_id"])
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
        read_valid_findings(run, command)
        print(json.dumps(change_drafts(run, *change) if change else read_drafts(run), indent=2))
    elif command == "submit":
        data, run, head_now, flags = Path(args[0]), Path(args[1]).resolve(), args[2], args[3:]
        # A Devin review writes findings.json itself and may change it until it ends, so nothing of a
        # review a live whooper-review still runs posts, neither an approval nor a comment.
        if run.name in running_runs(data):
            raise SystemExit("whooper-submit: the review is still running; submit it once it ends")
        # bin/whooper-submit writes submit.json saying posting before it posts, and adds GitHub's
        # response after, so a run that has the file may have its review on GitHub.
        if (run / "submit.json").exists():
            if load(run / "submit.json")["status"] == "posting":
                raise SystemExit(f"whooper-submit: another submit of {run} is posting or did not finish, so its review"
                                 " may be on GitHub; check the PR, then delete submit.json to submit again")
            raise SystemExit(f"whooper-submit: {run} was already submitted; see its submit.json")
        result = read_valid_findings(run, command)
        item = load(run / "run.json")
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
            if concerns := run_concerns(run, result):
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
    elif command == "add-round":
        data, entry = args
        add_round(Path(data), json.loads(entry))
    elif command == "status":
        print(json.dumps(read_status(Path(args[0])), indent=2))
    elif command == "show":
        print(json.dumps(show_review(Path(args[0]), args[1]), indent=2))
    else:
        raise SystemExit(f"whooper.py: unknown command {command!r}")


if __name__ == "__main__":
    main(*sys.argv[1:])
