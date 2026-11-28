import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout, suppress
import copy
from datetime import datetime, time, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from time import monotonic, sleep
from unittest import mock
from zoneinfo import ZoneInfo

import whooper
from whooper import (REVIEW_WORDING, SETTINGS_SCHEMA, add_round, bot_list, change_drafts, check_overview, check_schema,
                     claude_shell_calls, classify, co_change, conversation, devin_shell_calls, diff_files, failure_cause,
                     fill, main, mark_new_commits, next_round, pr_link, poll_items, read_asks, read_drafts, read_schedule,
                     select_work, short_date, short_time, skill_hashes, slack_posts, status_v2, summary_text,
                     wait_review, write_record, write_settings)

LOGIN = "sam-example"
# Every test runs on this made-up config, never on the user's own.
CONFIG = """
[user]
name = "Sam"
github_login = "sam-example"

[github]
orgs = ["example-org", "sample-org"]
review_bots = [{ login = "ci-review-bot", label = "the CI review bot" }]

[slack]
workspace_url = "https://example.slack.com"
user_id = "U0000000000"
ask_channel = "C0000000001"
summary_channel = "C0000000002"
summary_channel_name = "#whooper"
slackdump_workspace = "example"

[jobs]
review = "claude"
poll = "claude"
summary = "claude"

[agents.claude]
review_model = "review-model"
poll_model = "poll-model"
budget_usd = 15

[agents.devin]
review_model = "devin-review-model"
poll_model = "devin-poll-model"

[review]
timeout_minutes = 45
skills = ["review-skill", "language-skill"]

[repos."example-org/example-repo"]
notes = "notes.md"

[repos."sample-org/plain-repo"]
"""
ROOT = Path(__file__).resolve().parent.parent
# What whooper-status and whooper-show print as the cause of a review that failed or waits: what
# stopped it, and whether that stops every review or only its PR's.
CAUSES = {
    "docker_unavailable": {"id": "docker_unavailable", "summary": "Docker isn't running", "scope": "all"},
    "setup_failed": {"id": "setup_failed", "summary": "The repo's setup script failed", "scope": "all"},
    "gh_signed_out": {"id": "gh_signed_out", "summary": "gh isn't signed in to GitHub", "scope": "all"},
    "repo_not_configured": {"id": "repo_not_configured", "summary": "Whooper isn't set up for this repo", "scope": "pr"},
    "agent_no_findings": {"id": "agent_no_findings", "summary": "The review agent left no usable findings",
                          "scope": "pr"},
    "time_limit": {"id": "time_limit", "summary": "The review ran past its time limit", "scope": "pr"},
    "unknown": {"id": "unknown", "summary": "The review stopped unexpectedly", "scope": "pr"},
}


def setUpModule():
    # The made-up config and the build cache live in one folder, which goes once the module's tests end.
    folder = Path(tempfile.mkdtemp())
    unittest.addModuleCleanup(shutil.rmtree, folder, ignore_errors=True)
    config = folder / "config.toml"
    config.write_text(CONFIG)
    (folder / "notes.md").write_text("Run `make test` from the service's directory.")
    os.environ["WHOOPER_CONFIG"] = str(config)
    # Every test that runs go shares one build cache, so only the first compiles the standard library.
    os.environ["GOCACHE"] = str(folder / "go-build")


# The first line of each fake claude that a command which runs agents may call: it answers --version as Claude
# Code 2.1.314 does, which the command asks as it starts (lib/whooper.py's check_claude).
CLAUDE_VERSION = 'if [ "$1" = --version ]; then echo "2.1.314 (Claude Code)"; exit; fi'
# What those commands print when a job runs on claude and claude --version names a version before 2.1.295.
CLAUDE_TOO_OLD = ("whooper: [jobs] review, poll, and summary run on claude, which needs Claude Code 2.1.295 or later,"
                  " the first that blocks a Slack call when its guard hook fails, but `claude --version` says {}; update"
                  " Claude Code, or move those jobs to devin\n")


# How the review bots appear as GraphQL review authors.
BOTS = {"ci-review-bot", "assistant-bot"}


def actor(login):
    return {"__typename": "Bot" if login in BOTS else "User", "login": login}


def connection(nodes):
    return {"totalCount": len(nodes), "nodes": nodes}


# When the poll runs: after every review in these tests, and 4 hours after SlackItemsTest's human_review.
NOW = datetime(2031, 7, 27, 5, 0, tzinfo=timezone.utc)


def pr(number=1, author="alice", head="head2", state="OPEN", requested=(), reviews=(), threads=()):
    """A pull request as queries/pr.graphql returns it.

    requested: user logins, or ("team", slug).
    reviews: (author, state, submittedAt, commit) in API order, with "reply" as a fifth element
      for a thread reply, which GitHub records as a review.
    threads: (isResolved, [(author, createdAt), ...]).
    """
    def reviewer(r):
        # A team matches no fragment in the query, so it comes back empty.
        return {} if isinstance(r, tuple) else {"login": r}

    def review(author, state, submitted_at, commit, kind="review"):
        # Both have no body and one comment. A reply's comment answers another comment.
        reply_to = {"id": "PRRC_1"} if kind == "reply" else None
        return {"author": actor(author), "state": state, "submittedAt": submitted_at, "body": "",
                "commit": {"oid": commit}, "comments": connection([{"replyTo": reply_to}])}

    return {
        "url": f"https://github.com/example-org/example-repo/pull/{number}",
        "number": number,
        "repository": {"nameWithOwner": "example-org/example-repo"},
        "state": state,
        "headRefOid": head,
        "baseRefName": "master",
        "author": {"login": author},
        "reviewRequests": connection([{"requestedReviewer": reviewer(r)} for r in requested]),
        "reviews": connection([review(*r) for r in reviews]),
        "reviewThreads": connection([
            {"isResolved": resolved,
             "comments": connection([{"author": {"login": a}, "createdAt": t} for a, t in comments])}
            for resolved, comments in threads
        ]),
    }


def ask(ts="1943194837.000100", tagged=True, eyes=False, urls=("https://github.com/example-org/example-repo/pull/1",)):
    return {
        "ts": ts,
        "permalink": f"https://example.slack.com/archives/C0000000001/p{ts.replace('.', '')}",
        "tagged_user": tagged,
        "pr_urls": list(urls),
        "has_eyes": eyes,
        "reason": "Alex asked for a review.",
    }


def kinds(items):
    return [(i["pr"], i["kind"], i["source"]) for i in items]


class GitHubItemsTest(unittest.TestCase):
    def test_request_by_name_before_any_review_is_first_review(self):
        items = poll_items(LOGIN, [pr(requested=[LOGIN])], [], [], NOW)
        self.assertEqual(kinds(items), [(1, "first_review", "github_request")])
        self.assertEqual(items[0]["head_sha"], "head2")
        self.assertEqual(items[0]["base_ref"], "master")
        self.assertIsNone(items[0]["slack"])

    def test_team_request_is_not_work(self):
        self.assertEqual(poll_items(LOGIN, [pr(requested=[("team", "example-team")])], [], [], NOW), [])

    def test_pending_review_does_not_count_as_reviewed(self):
        github = [pr(requested=[LOGIN], reviews=[(LOGIN, "PENDING", None, "head2")])]
        self.assertEqual(kinds(poll_items(LOGIN, github, [], [], NOW)), [(1, "first_review", "github_request")])

    def test_request_by_name_after_a_review_is_recheck(self):
        # The thread has no reply, which would block a recheck without the re-request.
        github = [pr(
            requested=[LOGIN],
            reviews=[(LOGIN, "COMMENTED", "2031-07-27T01:02:46Z", "head1")],
            threads=[(False, [(LOGIN, "2031-07-27T01:02:46Z")])],
        )]
        self.assertEqual(kinds(poll_items(LOGIN, github, [], [], NOW)), [(1, "recheck", "github_request")])

    def test_only_a_thread_reply_is_not_a_review(self):
        # The user only replied "+1" in a thread Carol started on Bob's PR.
        reviews = [
            ("carol", "COMMENTED", "2031-07-27T01:00:00Z", "head1"),
            (LOGIN, "COMMENTED", "2031-07-27T02:00:00Z", "head1", "reply"),
        ]
        threads = [(False, [("carol", "2031-07-27T01:00:00Z"), (LOGIN, "2031-07-27T02:00:00Z")])]
        cases = [
            ("requested by name", [LOGIN], [(7, "first_review", "github_request")]),
            ("not requested, head moved", [], []),
        ]
        for name, requested, want in cases:
            with self.subTest(name):
                github = [pr(number=7, author="bob", requested=requested, reviews=reviews, threads=threads)]
                self.assertEqual(kinds(poll_items(LOGIN, github, [], [], NOW)), want)

    def test_latest_opinion_approved_is_not_rechecked(self):
        cases = {
            "approved": [(LOGIN, "APPROVED", "2031-07-28T19:08:52Z", "head1")],
            # A later COMMENTED review does not withdraw the approval.
            "approved then commented": [
                (LOGIN, "APPROVED", "2031-07-28T19:08:52Z", "head1"),
                (LOGIN, "COMMENTED", "2031-07-29T10:00:00Z", "head1"),
            ],
        }
        for name, reviews in cases.items():
            with self.subTest(name):
                self.assertEqual(poll_items(LOGIN, [pr(reviews=reviews)], [], [], NOW), [])

    def test_withdrawn_approval_can_be_rechecked(self):
        cases = {
            "approval dismissed": [
                (LOGIN, "COMMENTED", "2031-07-27T01:00:00Z", "head1"),
                (LOGIN, "DISMISSED", "2031-07-28T19:08:52Z", "head1"),
            ],
            "changes requested after approving": [
                (LOGIN, "APPROVED", "2031-07-27T01:00:00Z", "head1"),
                (LOGIN, "CHANGES_REQUESTED", "2031-07-28T19:08:52Z", "head1"),
            ],
        }
        for name, reviews in cases.items():
            with self.subTest(name):
                self.assertEqual(kinds(poll_items(LOGIN, [pr(reviews=reviews)], [], [], NOW)), [(1, "recheck", "reviewed_by_user")])

    def test_recheck_triggers(self):
        review = [(LOGIN, "COMMENTED", "2031-07-27T01:02:46Z", "head1")]
        mine = (LOGIN, "2031-07-27T01:02:46Z")
        cases = [
            # (name, head, threads, is recheck)
            ("push, thread resolved without reply", "head2", [(True, [mine])], True),
            ("push, author replied", "head2", [(False, [mine, ("alice", "2031-07-27T16:12:48Z")])], True),
            ("reply with no push", "head1", [(False, [mine, ("alice", "2031-07-27T16:12:48Z")])], True),
            ("push, no threads of the user's", "head2", [], True),
            ("push, one thread unanswered", "head2",
             [(False, [mine, ("alice", "2031-07-27T16:12:48Z")]), (False, [mine])], False),
            ("push, only another reviewer replied", "head2", [(False, [mine, ("bob", "2031-07-27T16:12:48Z")])], False),
            ("no push, no reply", "head1", [(True, [mine])], False),
            ("an unanswered bot thread does not block", "head2",
             [(False, [("ci-review-bot", "2031-07-26T18:31:52Z")]), (True, [mine])], True),
        ]
        for name, head, threads, want in cases:
            with self.subTest(name):
                items = poll_items(LOGIN, [pr(head=head, reviews=review, threads=threads)], [], [], NOW)
                self.assertEqual(kinds(items), [(1, "recheck", "reviewed_by_user")] if want else [])

    def test_users_thread_reply_after_a_push_does_not_hide_the_push(self):
        # The user reviewed at head1, and the author replied at head2 and pushed head3. The
        # user's "thanks" reply is recorded as a review at head3, but the user never reviewed head3.
        github = [pr(
            head="head3",
            reviews=[
                (LOGIN, "COMMENTED", "2031-07-27T01:02:46Z", "head1"),
                ("alice", "COMMENTED", "2031-07-27T16:12:48Z", "head2", "reply"),
                (LOGIN, "COMMENTED", "2031-07-27T18:00:00Z", "head3", "reply"),
            ],
            threads=[(False, [
                (LOGIN, "2031-07-27T01:02:46Z"), ("alice", "2031-07-27T16:12:48Z"), (LOGIN, "2031-07-27T18:00:00Z"),
            ])],
        )]
        items = poll_items(LOGIN, github, [], [], NOW)
        self.assertEqual(kinds(items), [(1, "recheck", "reviewed_by_user")])
        self.assertIn("from head1 to head3", items[0]["reason"])

    def test_reply_older_than_the_users_latest_review_does_not_trigger(self):
        github = [pr(
            head="head1",
            reviews=[
                (LOGIN, "COMMENTED", "2031-07-27T01:02:46Z", "head1"),
                (LOGIN, "COMMENTED", "2031-07-27T18:00:00Z", "head1"),
            ],
            threads=[(False, [(LOGIN, "2031-07-27T01:02:46Z"), ("alice", "2031-07-27T16:12:48Z")])],
        )]
        self.assertEqual(poll_items(LOGIN, github, [], [], NOW), [])

    def test_latest_review_is_by_submission_time(self):
        # The API lists reviews by creation. A review started earlier can be submitted later.
        github = [pr(
            head="head2",
            reviews=[
                (LOGIN, "COMMENTED", "2031-07-28T09:00:00Z", "head2"),
                (LOGIN, "COMMENTED", "2031-07-27T09:00:00Z", "head1"),
            ],
        )]
        self.assertEqual(poll_items(LOGIN, github, [], [], NOW), [])

    def test_pending_review_is_not_the_latest_review(self):
        github = [pr(
            head="head2",
            reviews=[
                (LOGIN, "COMMENTED", "2031-07-27T01:02:46Z", "head1"),
                (LOGIN, "PENDING", None, "head2"),
            ],
        )]
        items = poll_items(LOGIN, github, [], [], NOW)
        self.assertEqual(kinds(items), [(1, "recheck", "reviewed_by_user")])
        self.assertIn("head1", items[0]["reason"])

    def test_truncated_connection_fails(self):
        github = [pr(reviews=[(LOGIN, "COMMENTED", "2031-07-27T01:02:46Z", "head1")])]
        github[0]["reviews"]["totalCount"] = 101
        with self.assertRaises(ValueError):
            poll_items(LOGIN, github, [], [], NOW)


class SlackItemsTest(unittest.TestCase):
    human_review = ("bob", "COMMENTED", "2031-07-27T01:00:00Z", "head1")
    # Review bots and the PR author's own thread replies are not someone reviewing.
    non_human_reviews = [
        ("ci-review-bot", "COMMENTED", "2031-07-26T18:31:52Z", "head1"),
        ("assistant-bot", "COMMENTED", "2031-07-26T18:40:00Z", "head1"),
        ("alice", "COMMENTED", "2031-07-26T18:59:38Z", "head1", "reply"),
    ]

    def test_tagged_ask_is_first_review_with_its_post(self):
        items = poll_items(LOGIN, [], [ask(tagged=True)], [pr()], NOW)
        self.assertEqual(kinds(items), [(1, "first_review", "slack_ask")])
        self.assertEqual(items[0]["slack"], {
            "ts": "1943194837.000100",
            "tagged_user": True,
            "permalink": "https://example.slack.com/archives/C0000000001/p1943194837000100",
        })
        self.assertEqual(items[0]["reason"], "Alex asked for a review.")

    def test_untagged_ask_conditions(self):
        cases = [
            # (name, has :eyes:, reviews, taken)
            ("nobody looking", False, [], True),
            ("only bots and the author", False, self.non_human_reviews, True),
            ("only the user's pending review", False, [(LOGIN, "PENDING", None, "head2")], True),
            ("a human put :eyes:", True, [], False),
            ("a human reviewed in the last 8 hours", False, [self.human_review], False),
            # More than 8 hours before NOW, so the PR is fair game again.
            ("a human reviewed earlier", False, [("bob", "COMMENTED", "2031-07-26T20:59:59Z", "head1")], True),
        ]
        for name, eyes, reviews, taken in cases:
            with self.subTest(name):
                items = poll_items(LOGIN, [], [ask(tagged=False, eyes=eyes)], [pr(reviews=reviews)], NOW)
                self.assertEqual(kinds(items), [(1, "first_review", "slack_ask")] if taken else [])

    def test_tagged_ask_ignores_eyes_and_other_reviews(self):
        items = poll_items(LOGIN, [], [ask(tagged=True, eyes=True)], [pr(reviews=[self.human_review])], NOW)
        self.assertEqual(kinds(items), [(1, "first_review", "slack_ask")])

    def test_tagged_ask_after_the_users_review_is_recheck(self):
        reviewed = pr(reviews=[(LOGIN, "COMMENTED", "2031-07-27T01:02:46Z", "head1")])
        self.assertEqual(kinds(poll_items(LOGIN, [], [ask()], [reviewed], NOW)), [(1, "recheck", "slack_ask")])

    def test_untagged_ask_skips_a_pr_the_user_already_reviewed(self):
        # Such as a daily digest that still lists a PR the user approved.
        reviewed = pr(reviews=[(LOGIN, "APPROVED", "2031-07-27T01:02:46Z", "head2")])
        self.assertEqual(poll_items(LOGIN, [], [ask(tagged=False)], [reviewed], NOW), [])

    def test_closed_merged_and_own_prs_are_dropped(self):
        for name, data in {
            "closed": pr(state="CLOSED"),
            "merged": pr(state="MERGED"),
            "the user's own PR": pr(author=LOGIN),
        }.items():
            with self.subTest(name):
                self.assertEqual(poll_items(LOGIN, [], [ask()], [data], NOW), [])

    def test_ask_for_a_pr_without_data_is_skipped(self):
        self.assertEqual(poll_items(LOGIN, [], [ask(urls=["https://github.com/example-org/example-repo/pull/2"])], [pr()], NOW), [])

    def test_one_item_per_pr_and_kind_preferring_tagged_slack_asks(self):
        github = [pr(requested=[LOGIN])]
        asks = [ask(ts="1943194600.000001", tagged=False), ask(ts="1943194837.000100", tagged=True)]
        items = poll_items(LOGIN, github, asks, [pr()], NOW)
        self.assertEqual(kinds(items), [(1, "first_review", "slack_ask")])
        self.assertEqual(items[0]["slack"]["ts"], "1943194837.000100")

    def test_request_by_name_wins_over_an_untagged_ask(self):
        # An untagged ask lets the review run skip a PR that is not small. A request by name does not.
        items = poll_items(LOGIN, [pr(requested=[LOGIN])], [ask(tagged=False)], [pr()], NOW)
        self.assertEqual(kinds(items), [(1, "first_review", "github_request")])

    def test_each_linked_pr_is_its_own_item(self):
        urls = ["https://github.com/example-org/example-repo/pull/1", "https://github.com/example-org/example-repo/pull/2"]
        items = poll_items(LOGIN, [], [ask(urls=urls)], [pr(number=1), pr(number=2)], NOW)
        self.assertEqual(kinds(items), [(1, "first_review", "slack_ask"), (2, "first_review", "slack_ask")])


CHANNEL = "C0000000001"
USER = "U0000000000"
SLACK_URL = "https://example.slack.com"
ORGS = ["example-org", "sample-org"]
OLDEST = "1943117600"
ASKER, TEAMMATE = "U0000000001", "U0000000002"


def message(ts, text, user=ASKER, reactions=(), **fields):
    """One message as slackdump dumps it."""
    out = {"type": "message", "ts": ts, "user": user, "text": text, **fields}
    if reactions:
        out["reactions"] = [{"name": name, "count": 1, "users": [TEAMMATE]} for name in reactions]
    return out


POST_TS = "1943194837.000100"
POST = message(POST_TS, (
    "<@U0000000000|Sam Reviewer> can you take a look?\n"
    "• <https://github.com/example-org/example-repo/pull/21|feat(app): add a settings page #21>\n"
    "• <http://github.com/example-org/example-repo/pull/22|#22>"
), reactions=["eyes"])
DIGEST_TS = "1943197600.000001"
BOT_DIGEST = message(DIGEST_TS, "• <https://github.com/example-org/example-repo/pull/34|#34>", user="U07ABCDEF12",
                     bot_id="B0000000001", bot_profile={"name": "pr_bot"})


def posts(*messages):
    """The ask channel's posts, as whooper-poll gives them to the run and to read_asks."""
    return slack_posts({"channel_id": CHANNEL, "name": "pr-reviews", "messages": list(messages or [POST])}, OLDEST)


def model_ask(**overrides):
    out = {
        "ts": POST_TS,
        "pr_urls": ["https://github.com/example-org/example-repo/pull/21"],
        "reason": "Alex asked the user to review #21.",
    }
    out.update(overrides)
    return out


def asks_of(channel_posts, asks, not_asks=()):
    return read_asks(channel_posts, {"asks": asks, "not_asks": list(not_asks)}, OLDEST, CHANNEL, USER, SLACK_URL, ORGS)


# Alex's post, reported as not an ask, for tests about other posts.
ASKER_NOT_ASK = {"ts": POST_TS, "reason": "Not an ask."}


class SlackPostsTest(unittest.TestCase):
    def test_keeps_each_post_and_reply_since_oldest(self):
        reply = message("1943194900.000200", "<@U0000000000> also #23", thread_ts=POST_TS)
        old = message("1943017600.000001", "<https://github.com/example-org/example-repo/pull/9|#9>")
        malformed = message("1943194837", "<https://github.com/example-org/example-repo/pull/9|#9>")
        # slackdump lists the thread's post first among its replies.
        self.assertEqual(posts({**POST, "slackdump_thread_replies": [POST, reply]}, old, malformed), [
            {"ts": POST_TS, "user": ASKER, "bot": False, "text": POST["text"], "reactions": ["eyes"]},
            {"ts": "1943194900.000200", "thread_ts": POST_TS, "user": ASKER, "bot": False,
             "text": "<@U0000000000> also #23", "reactions": []},
        ])

    def test_an_older_post_counts_again_for_a_new_reply_or_edit(self):
        older = message("1943017600.000001", "Ongoing PR stack, see the thread")
        reply = message("1943194900.000200", "<https://github.com/example-org/example-repo/pull/9|#9>",
                        thread_ts=older["ts"])
        edited = message("1943017700.000001", "EDIT: see the thread", edited={"ts": "1943194950.000000"})
        self.assertEqual([(p["ts"], p.get("thread_ts")) for p in posts({**older, "slackdump_thread_replies": [older, reply]}, edited)],
                         [("1943194900.000200", older["ts"]), ("1943017700.000001", None)])

    def test_a_shared_message_counts_as_part_of_the_post(self):
        bump = message(DIGEST_TS, "bump on this one", attachments=[{
            "from_url": "https://example.slack.com/archives/C0000000009/p1943190000000001",
            "text": "alex requested review on <https://github.com/example-org/example-repo/pull/60|#60>"}])
        asks, problems = asks_of(posts(bump), [model_ask(ts=DIGEST_TS, pr_urls=[
            "https://github.com/example-org/example-repo/pull/60"])])
        self.assertEqual((problems, [a["pr_urls"] for a in asks]),
                         ([], [["https://github.com/example-org/example-repo/pull/60"]]))

    def test_marks_what_bots_wrote(self):
        for name, m in [
            ("an app or integration", BOT_DIGEST),
            ("a bot message", message(DIGEST_TS, "digest", subtype="bot_message")),
            ("Slackbot", message(DIGEST_TS, "reminder", user="USLACKBOT")),
        ]:
            with self.subTest(name):
                self.assertTrue(posts(m)[0]["bot"])


class ReadAsksTest(unittest.TestCase):
    def test_ask_gets_a_permalink_built_from_its_ts(self):
        asks, problems = asks_of(posts(), [model_ask()])
        self.assertEqual(problems, [])
        self.assertEqual(asks, [{
            "ts": POST_TS,
            "permalink": "https://example.slack.com/archives/C0000000001/p1943194837000100",
            "tagged_user": True,
            "pr_urls": ["https://github.com/example-org/example-repo/pull/21"],
            "has_eyes": True,
            "reason": "Alex asked the user to review #21.",
        }])

    def test_http_link_in_the_post_grounds_an_https_url(self):
        asks, problems = asks_of(posts(), [model_ask(pr_urls=["https://github.com/example-org/example-repo/pull/22"])])
        self.assertEqual(problems, [])
        self.assertEqual(asks[0]["pr_urls"], ["https://github.com/example-org/example-repo/pull/22"])

    def test_untrusted_pr_urls_are_dropped(self):
        for url, in_post in [
            ("https://github.com/other-org/example-repo/pull/21", True),
            ("https://github.com/example-org/example-repo/pull/21/files", True),
            ("https://github.com/example-org/example-repo/pull/21; gh pr merge", True),
            ("https://github.com/example-org/example-repo/issues/21", True),
            ("https://github.com/example-org/example-repo/pull/0", True),
            # Well formed, but the post links #21, not #2.
            ("https://github.com/example-org/example-repo/pull/2", False),
        ]:
            with self.subTest(url):
                post = {**POST, "text": f"{POST['text']}\n{url}"} if in_post else POST
                asks, problems = asks_of(posts(post), [model_ask(pr_urls=[url])])
                self.assertEqual(asks, [])
                self.assertTrue(problems)

    def test_pr_url_must_be_linked_by_the_post_that_asks(self):
        # The bot's digest links #34, but Alex's post does not.
        urls = ["https://github.com/example-org/example-repo/pull/21", "https://github.com/example-org/example-repo/pull/34"]
        asks, problems = asks_of(posts(POST, BOT_DIGEST), [model_ask(pr_urls=urls)])
        self.assertEqual([a["pr_urls"] for a in asks], [["https://github.com/example-org/example-repo/pull/21"]])
        self.assertEqual(problems, [f"dropped PR URL 'https://github.com/example-org/example-repo/pull/34' from ask {POST_TS}"])

    def test_ask_must_be_one_of_the_channels_posts_since_oldest(self):
        link = "<https://github.com/example-org/example-repo/pull/21|#21>"
        for name, ts, extra in [
            ("not a post", "1943195599.000001", []),
            ("malformed", "1943194837", [message("1943194837", link)]),
            ("before oldest", "1943017600.000001", [message("1943017600.000001", link)]),
        ]:
            with self.subTest(name):
                asks, problems = asks_of(posts(POST, *extra), [model_ask(ts=ts)], not_asks=[ASKER_NOT_ASK])
                self.assertEqual(asks, [])
                self.assertTrue(problems)

    def test_mention_and_eyes_are_read_from_the_post(self):
        eyes_no_mention = message("1943197600.000001", "<https://github.com/example-org/example-repo/pull/50|#50>",
                                  user=TEAMMATE, reactions=["squirrel", "eyes"])
        mention_no_eyes = message("1943197601.000001", "<@U0000000000> <https://github.com/example-org/example-repo/pull/51|#51>",
                                  reactions=["eyes-closed"])
        model_asks = [
            model_ask(ts="1943197600.000001", pr_urls=["https://github.com/example-org/example-repo/pull/50"]),
            model_ask(ts="1943197601.000001", pr_urls=["https://github.com/example-org/example-repo/pull/51"]),
        ]
        asks, problems = asks_of(posts(POST, eyes_no_mention, mention_no_eyes), model_asks, not_asks=[ASKER_NOT_ASK])
        self.assertEqual(problems, [])
        self.assertEqual([(a["ts"], a["tagged_user"], a["has_eyes"]) for a in asks],
                         [("1943197600.000001", False, True), ("1943197601.000001", True, False)])

    def test_ask_from_a_bot_counts(self):
        # Such as a digest of PRs waiting for the team's review.
        bot_ask = model_ask(ts=DIGEST_TS, pr_urls=["https://github.com/example-org/example-repo/pull/34"])
        asks, problems = asks_of(posts(POST, BOT_DIGEST), [bot_ask], not_asks=[ASKER_NOT_ASK])
        self.assertEqual((problems, [a["pr_urls"] for a in asks]), ([], [["https://github.com/example-org/example-repo/pull/34"]]))

    def test_an_ask_in_a_thread_links_to_the_reply(self):
        reply = message("1943194900.000200", "<https://github.com/example-org/example-repo/pull/9|#9>", thread_ts=POST_TS)
        asks, problems = asks_of(posts({**POST, "slackdump_thread_replies": [POST, reply]}),
                                 [model_ask(ts="1943194900.000200", pr_urls=["https://github.com/example-org/example-repo/pull/9"])],
                                 not_asks=[ASKER_NOT_ASK])
        self.assertEqual(problems, [])
        self.assertEqual(asks[0]["permalink"], "https://example.slack.com/archives/C0000000001/p1943194900000200"
                                               f"?thread_ts={POST_TS}&cid=C0000000001")

    def test_every_post_that_mentions_the_user_next_to_a_pr_link_is_answered(self):
        link = "<@U0000000000|Sam Reviewer> <https://github.com/example-org/example-repo/pull/50|#50>"
        for name, channel_posts, not_asks, raises in [
            ("left out", posts(), [], True),
            ("reported as not an ask", posts(), [ASKER_NOT_ASK], False),
            ("written by the user", posts(message(DIGEST_TS, link, user=USER)), [], False),
            ("written by a bot and left out", posts(message(DIGEST_TS, link, bot_id="B0000000001")), [], True),
            ("no PR link", posts(message(DIGEST_TS, "<@U0000000000|Sam Reviewer> lunch?")), [], False),
        ]:
            with self.subTest(name):
                if raises:
                    with self.assertRaises(ValueError):
                        asks_of(channel_posts, [], not_asks)
                else:
                    self.assertEqual(asks_of(channel_posts, [], not_asks), ([], []))


# The repos the test config lists, as select_work gets them.
REPOS = {"example-org/example-repo": {}, "sample-org/plain-repo": {}}


def work_item(kind="first_review", head="head3", number=12):
    return {"kind": kind, "repo": "example-org/example-repo", "pr": number, "head_sha": head, "base_ref": "master",
            "source": "reviewed_by_user", "slack": None, "reason": "r"}



class SelectWorkTest(unittest.TestCase):
    # 10:00 on 2031-08-01 in a UTC-7 timezone.
    now = datetime(2031, 8, 1, 10, 0, tzinfo=timezone(timedelta(hours=-7)))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.prs, self.runs = Path(tmp.name) / "prs", Path(tmp.name) / "runs"

    def record(self, name, status, failures=0, started_at="2031-08-01T16:00:00Z", pr_dir="example-org-example-repo-12"):
        path = self.prs / pr_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": status, "failures": failures, "started_at": started_at}))

    def selected(self, item):
        work, drops, _ = select_work([item], self.prs, self.now, REPOS)
        self.assertEqual(len(work) + len(drops), 1)
        return bool(work)

    def listed_again(self, head="head3"):
        """A recheck-list item: PR 12, which only a Slack ask brought in, as whooper-submit listed it on
        finding its head moved to head."""
        slack = {"ts": "1943194837.000100", "tagged_user": True,
                 "permalink": "https://example.slack.com/archives/C0000000001/p1943194837000100"}
        return {**work_item(head=head), "source": "slack_ask", "slack": slack,
                "reason": "Alex asked for a review."}

    def make_run(self, run_id, item, removed=False, skipped=False):
        """Makes the review run run_id of item, whose run.json is item with the run's own fields, and marks it
        removed by the user, as bin/whooper-remove does, when removed is true, and skipped, as
        bin/whooper-skip does, when skipped is true; returns its directory."""
        run = self.runs / run_id
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({**item, "run_id": run_id, "agent": "claude"}))
        if removed:
            (run / "removed-by-user").touch()
        if skipped:
            (run / "skipped-by-user").touch()
        return run

    def test_no_record_is_work(self):
        self.assertTrue(self.selected(work_item()))

    def test_repo_the_config_does_not_list_is_dropped(self):
        item = {**work_item(), "repo": "example-org/unlisted-repo"}
        work, drops, _ = select_work([item], self.prs, self.now, REPOS)
        self.assertEqual(work, [])
        self.assertEqual(drops, [(item, 'the config has no [repos."example-org/unlisted-repo"] table')])

    def test_record_at_this_head_and_kind(self):
        cases = [
            # (status, failures, selected)
            ("done", 0, False),
            ("running", 0, False),
            ("failed", 2, True),
            ("failed", 3, False),
            ("stale", 0, True),
        ]
        for status, failures, want in cases:
            with self.subTest(f"{status} {failures}"):
                self.record("head3.first_review.json", status, failures)
                self.assertEqual(self.selected(work_item()), want)

    def test_record_for_another_head_or_kind_does_not_match(self):
        self.record("head2.first_review.json", "done")
        self.record("head3.recheck.json", "done")
        self.record("head3.first_review.json", "done", pr_dir="example-org-example-repo-1")
        self.assertTrue(self.selected(work_item()))

    def test_an_unreadable_record_counts_as_none(self):
        # Such as one cut short by a crash before write_record wrote through a temporary file. The review
        # writes the record again when it starts.
        for text in ["", '{"status": "do', "[]"]:
            with self.subTest(text):
                path = self.prs / "example-org-example-repo-12" / "head3.first_review.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                self.assertEqual(select_work([work_item()], self.prs, self.now, REPOS), ([work_item()], [], []))
                # An unreadable record of another head does not count toward a recheck-list item's daily limit.
                path.rename(path.with_name("head2.first_review.json"))
                for head in ["head0", "head1"]:
                    self.record(f"{head}.first_review.json", "stale", started_at="2031-08-01T16:00:00Z")
                listed = self.listed_again()
                self.assertEqual(select_work([], self.prs, self.now, REPOS, [listed]), ([listed], [], []))
                shutil.rmtree(self.prs)

    def test_a_recheck_is_dropped_until_whooper_review_rechecks(self):
        # whooper-review reviews first reviews only, so a re-check the poll finds would fail every round.
        recheck = {**work_item(kind="recheck"), "source": "reviewed_by_user"}
        why = "whooper-review does not re-check a PR yet"
        self.assertEqual(select_work([recheck], self.prs, self.now, REPOS), ([], [(recheck, why)], []))
        # Not even a retry lets it through.
        self.record("head3.recheck.json", "retry")
        self.assertEqual(select_work([recheck], self.prs, self.now, REPOS), ([], [(recheck, why)], []))
        # A re-check on the recheck list, as a round that waited left before this rule, leaves the list.
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [recheck]), ([], [(recheck, why)], [recheck]))
        # The poll listing the PR as a re-check does not finish the PR's first review on the list, such as
        # for new commits since the review whooper-submit posted, which then still gets its review.
        listed = self.listed_again()
        self.assertEqual(select_work([recheck], self.prs, self.now, REPOS, [listed]),
                         ([listed], [(recheck, why)], []))

    def test_a_recheck_list_item_whose_pr_github_reports_merged_or_closed_leaves_the_list(self):
        # whooper-fetch looked up the list's PRs its searches did not find: GitHub reports PR 13 merged, PR 14
        # closed, and PR 15 open.
        merged, closed, still_open = ({**self.listed_again(), "pr": number} for number in (13, 14, 15))
        states = {"https://github.com/example-org/example-repo/pull/13": "MERGED",
                  "https://github.com/example-org/example-repo/pull/14": "CLOSED"}
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [merged, closed, still_open], closed=states),
                         ([still_open], [(merged, "merged on GitHub"), (closed, "closed on GitHub")], [merged, closed]))
        # Not even a retry reviews a merged PR.
        self.record("head3.first_review.json", "retry", pr_dir="example-org-example-repo-13")
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [merged], closed=states),
                         ([], [(merged, "merged on GitHub")], [merged]))

    def test_three_reviews_today_hold_a_fourth_recheck_list_item(self):
        for head in ["head0", "head1", "head2"]:
            self.record(f"{head}.first_review.json", "done", started_at="2031-08-01T16:00:00Z")
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [self.listed_again()])[0], [])
        # The poll's own first reviews have no daily limit.
        self.assertTrue(self.selected(work_item()))

    def test_failed_third_review_today_is_retried(self):
        self.record("head1.first_review.json", "done", started_at="2031-08-01T16:00:00Z")
        self.record("head2.first_review.json", "done", started_at="2031-08-01T16:20:00Z")
        self.record("head3.first_review.json", "failed", failures=1, started_at="2031-08-01T16:40:00Z")
        listed = self.listed_again()
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [listed])[0], [listed])

    def test_reviews_count_by_local_day(self):
        self.record("head1.first_review.json", "done", started_at="2031-08-01T16:00:00Z")
        self.record("head2.first_review.json", "done", started_at="2031-08-01T16:30:00Z")
        # 23:30 on 2031-07-31 at UTC-7, though 2031-08-01 in UTC.
        self.record("head0.first_review.json", "done", started_at="2031-08-01T06:30:00Z")
        listed = self.listed_again()
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [listed])[0], [listed])

    def test_a_recheck_list_item_is_work_until_a_review_at_its_head_is_done_or_gives_up(self):
        listed = self.listed_again()
        cases = [
            # (the record at its head as (status, failures), selected, finished)
            (None, True, False),
            (("running", 0), False, False),
            (("failed", 2), True, False),
            (("failed", 3), False, True),
            (("done", 0), False, True),
        ]
        for record, want_selected, want_finished in cases:
            with self.subTest(record):
                if record:
                    self.record("head3.first_review.json", *record)
                work, _, finished = select_work([], self.prs, self.now, REPOS, [listed])
                self.assertEqual(work, [listed] if want_selected else [])
                self.assertEqual(finished, [listed] if want_finished else [])

    def test_the_poll_listing_the_pr_finishes_its_recheck_list_item(self):
        # Such as a new ask for the PR, which the poll lists at the head the PR has now.
        polled, listed = work_item(head="head4"), self.listed_again()
        self.assertEqual(select_work([polled], self.prs, self.now, REPOS, [listed]), ([polled], [], [listed]))

    def test_the_recheck_limit_holds_a_recheck_list_item_for_the_next_day(self):
        for head in ["head0", "head1", "head2"]:
            self.record(f"{head}.first_review.json", "stale", started_at="2031-08-01T16:00:00Z")
        listed = self.listed_again()
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [listed]),
                         ([], [(listed, "3 reviews of other heads already today")], []))
        # The poll's own first reviews have no daily limit.
        self.assertTrue(self.selected(work_item(kind="first_review")))
        tomorrow = self.now + timedelta(days=1)
        self.assertEqual(select_work([], self.prs, tomorrow, REPOS, [listed]), ([listed], [], []))

    def test_a_retry_is_work_whatever_earlier_reviews_at_its_head_did(self):
        # The head failed 3 times before bin/whooper-retry marked its record retry and listed the PR
        # again, and the PR has 3 first-review records for other heads today, which would hold any
        # other recheck-list item for the next day.
        for head in ["head0", "head1", "head2"]:
            self.record(f"{head}.first_review.json", "stale", started_at="2031-08-01T16:00:00Z")
        self.record("head3.first_review.json", "retry", failures=3)
        listed = self.listed_again()
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [listed]), ([listed], [], []))
        # A poll that lists the PR finishes the list's item, and the poll's item at that head is the work.
        polled = work_item(kind="first_review")
        self.assertEqual(select_work([polled], self.prs, self.now, REPOS, [listed]), ([polled], [], [listed]))
        # Once the review starts, its record counts as any other: here it failed a fourth time.
        self.record("head3.first_review.json", "failed", failures=4)
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [listed]),
                         ([], [(listed, "failed 4 times at this head")], [listed]))

    def test_a_removed_review_holds_back_the_request_it_had_until_a_new_one(self):
        # The PR's review of head3, which a Slack ask brought in, failed once, and the user removed it.
        listed = self.listed_again()
        removed = self.make_run("20310801T150000Z-12", listed, removed=True)
        self.record("head3.first_review.json", "failed", failures=1)
        standing = {**work_item(kind="first_review"), "source": "github_request"}
        new_ask = {**listed, "slack": {**listed["slack"], "ts": "1943198437.000200"}}
        cases = [
            # (what the poll lists, selected)
            # A GitHub review request reads the same in every poll while it stands, and a Slack post the
            # poll reads again, as after an edit, is the same ask.
            (standing, False),
            (listed, False),
            # A new Slack post asks again, or the PR has new commits.
            (new_ask, True),
            ({**standing, "head_sha": "head4"}, True),
        ]
        for item, want in cases:
            with self.subTest(item=item):
                self.assertEqual(select_work([item], self.prs, self.now, REPOS, runs_dir=self.runs),
                                 ([item], [], []) if want else ([], [(item, "removed by the user")], []))
        # The recheck list's item for the PR leaves the list.
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [listed], runs_dir=self.runs),
                         ([], [(listed, "removed by the user")], [listed]))
        # bin/whooper-retry marks the record at the head retry: the user asked for the review.
        self.record("head3.first_review.json", "retry", failures=1)
        self.assertEqual(select_work([standing], self.prs, self.now, REPOS, runs_dir=self.runs), ([standing], [], []))
        self.record("head3.first_review.json", "failed", failures=1)
        # Only the PR's newest run counts, and a PR of another repo with the same number is another PR.
        self.make_run("20310801T170000Z-12", {**listed, "repo": "sample-org/plain-repo"}, removed=True)
        newer = self.make_run("20310801T160000Z-12", standing)
        self.assertEqual(select_work([standing], self.prs, self.now, REPOS, runs_dir=self.runs), ([standing], [], []))
        # Unmarked, as bin/whooper-remove --undo leaves it, the newest run holds nothing back either.
        shutil.rmtree(newer)
        (removed / "removed-by-user").unlink()
        self.assertEqual(select_work([standing], self.prs, self.now, REPOS, runs_dir=self.runs), ([standing], [], []))

    def test_a_skipped_review_holds_back_every_review_of_its_pr_until_undone(self):
        # The user skipped the PR's review of head3, which a Slack ask brought in.
        listed = self.listed_again()
        skipped = self.make_run("20310801T150000Z-12", listed, skipped=True)
        standing = {**work_item(kind="first_review"), "source": "github_request"}
        new_ask = {**listed, "slack": {**listed["slack"], "ts": "1943198437.000200"}}
        # Unlike a removal, a new request holds back too: new commits or a new Slack post.
        for item in [standing, listed, new_ask, {**standing, "head_sha": "head4"}]:
            with self.subTest(item=item):
                self.assertEqual(select_work([item], self.prs, self.now, REPOS, runs_dir=self.runs),
                                 ([], [(item, "skipped by the user")], []))
        # The recheck list's item for the PR stays on the list for after the undo.
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [listed], runs_dir=self.runs),
                         ([], [(listed, "skipped by the user")], []))
        # bin/whooper-retry marks the record at the head retry: the user asked for the review.
        self.record("head3.first_review.json", "retry")
        self.assertEqual(select_work([standing], self.prs, self.now, REPOS, runs_dir=self.runs), ([standing], [], []))
        self.record("head3.first_review.json", "failed", failures=1)
        # Undone, as bin/whooper-skip --undo leaves it, the run holds nothing back.
        (skipped / "skipped-by-user").unlink()
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [listed], runs_dir=self.runs), ([listed], [], []))
        # Only the PR's newest run counts: a skip of an earlier review holds nothing back.
        self.make_run("20310801T140000Z-12", listed, skipped=True)
        self.assertEqual(select_work([standing], self.prs, self.now, REPOS, runs_dir=self.runs), ([standing], [], []))


class WriteRecordTest(unittest.TestCase):
    def test_failures_add_up_across_runs_and_done_keeps_the_run_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            prs = Path(tmp)
            item = work_item(kind="first_review")
            path = prs / "example-org-example-repo-12" / "head3.first_review.json"
            first, second = datetime(2031, 8, 1, 17, 0, tzinfo=timezone.utc), datetime(2031, 8, 1, 18, 0, tzinfo=timezone.utc)
            steps = [
                ("running", first, {"status": "running", "failures": 0, "started_at": "2031-08-01T17:00:00+00:00"}),
                ("failed", first + timedelta(minutes=30), {"status": "failed", "failures": 1, "started_at": "2031-08-01T17:00:00+00:00"}),
                ("running", second, {"status": "running", "failures": 1, "started_at": "2031-08-01T18:00:00+00:00"}),
                ("done", second + timedelta(minutes=40), {"status": "done", "failures": 1, "started_at": "2031-08-01T18:00:00+00:00"}),
            ]
            for status, now, want in steps:
                write_record(prs, item, status, now)
                self.assertEqual(json.loads(path.read_text()), want)
            # whooper-select reads the same record.
            self.assertEqual(select_work([item], prs, second, REPOS)[0], [])

    def test_stale_keeps_the_approval_done_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            prs, now = Path(tmp), datetime(2031, 8, 1, 17, 0, tzinfo=timezone.utc)
            item = work_item(kind="first_review")
            approval = {"verdict": "approve_once_ci_passes", "reason": "Ran the package tests."}
            write_record(prs, item, "running", now)
            write_record(prs, item, "done", now, approval)
            write_record(prs, item, "stale", now)
            path = prs / "example-org-example-repo-12" / "head3.first_review.json"
            self.assertEqual(json.loads(path.read_text())["approval"], approval)

    def test_waits_for_the_pr_folders_lock_and_writes_through_a_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            prs, now = Path(tmp), datetime(2031, 8, 1, 17, 0, tzinfo=timezone.utc)
            folder = prs / "example-org-example-repo-12"
            path = folder / "head3.first_review.json"
            folder.mkdir()
            written, write_file = [], whooper.write_file

            def spy(target, text, **flags):
                written.append(target)
                write_file(target, text, **flags)

            with mock.patch.object(whooper, "write_file", spy):
                # Another writer, such as whooper-post marking the head stale, holds the folder's lock and
                # writes the record meanwhile; this write waits, then counts from what it wrote.
                with whooper.folder_lock(folder):
                    writer = threading.Thread(target=write_record, args=(prs, work_item(kind="first_review"), "failed", now))
                    writer.start()
                    writer.join(0.5)
                    self.assertTrue(writer.is_alive())
                    path.write_text(json.dumps({"status": "running", "failures": 1, "started_at": "2031-08-01T16:00:00+00:00"}))
                writer.join(10)
            self.assertEqual(written, [path])
            self.assertEqual(json.loads(path.read_text()),
                             {"status": "failed", "failures": 2, "started_at": "2031-08-01T16:00:00+00:00"})

    def test_an_unreadable_record_starts_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            prs, now = Path(tmp), datetime(2031, 8, 1, 17, 0, tzinfo=timezone.utc)
            path = prs / "example-org-example-repo-12" / "head3.first_review.json"
            path.parent.mkdir()
            path.write_text('{"status": "runn')
            write_record(prs, work_item(kind="first_review"), "failed", now)
            self.assertEqual(json.loads(path.read_text()),
                             {"status": "failed", "failures": 1, "started_at": "2031-08-01T17:00:00+00:00"})


T = datetime(2031, 7, 19, 21, 35, 4, tzinfo=timezone.utc)


def gql_comment(author, created, body, edited=None):
    """A review comment as queries/conversation.graphql returns it."""
    return {"databaseId": 1, "author": {"login": author}, "createdAt": created, "lastEditedAt": edited,
            "path": "services/orders/internal/api/settings.go", "originalLine": 88, "originalStartLine": None,
            "diffHunk": "@@ -80,6 +80,9 @@", "body": body, "replyTo": None, "originalCommit": {"oid": "head1"}}


def gql_review(author, state, submitted_at, comments=(), body=""):
    return {"author": {"login": author}, "state": state, "submittedAt": submitted_at, "body": body,
            "commit": {"oid": "head1"}, "comments": connection(list(comments))}


def gql_pr(reviews=(), comments=(), body="Compares rows.", edits=()):
    """A pull request as queries/conversation.graphql returns it.

    comments: (author, createdAt, lastEditedAt, body). edits: (editedAt, body of that version), newest first.
    """
    return {
        "url": "https://github.com/example-org/example-repo/pull/42", "number": 42, "title": "feat(app): add a settings page",
        "body": body, "author": {"login": "dave"},
        "userContentEdits": connection([{"editedAt": t, "diff": text} for t, text in edits]),
        "reviews": connection(list(reviews)),
        "comments": connection([{"author": {"login": a}, "createdAt": c, "lastEditedAt": e, "body": b} for a, c, e, b in comments]),
    }


def summary(github):
    return ([(r["author"], r["state"], [c["body"] for c in r["comments"]]) for r in github["reviews"]],
            [c["body"] for c in github["comments"]])


class ConversationTest(unittest.TestCase):
    def test_without_a_cutoff_keeps_every_submitted_review_but_not_a_pending_one(self):
        github = conversation(gql_pr(reviews=[
            gql_review("alice", "APPROVED", "2031-07-16T02:43:19Z", [gql_comment("alice", "2031-07-16T02:38:42Z", "rename this")]),
            gql_review(LOGIN, "PENDING", None, [gql_comment(LOGIN, "2031-07-19T21:09:55Z", "draft")]),
        ]))
        self.assertEqual(summary(github), ([("alice", "APPROVED", ["rename this"])], []))
        self.assertEqual(github["pr"]["body"], "Compares rows.")
        self.assertIsNone(github["as_of"])

    def test_cutoff_leaves_out_the_replayed_review_and_everything_written_after_it(self):
        # The user drafted comments before the cutoff and submitted the review at it,
        # so the comments are dated before the cutoff although nobody could read them then.
        pr = gql_pr(
            reviews=[
                gql_review("alice", "APPROVED", "2031-07-16T02:43:19Z", [
                    gql_comment("alice", "2031-07-16T02:38:42Z", "rename this"),
                    gql_comment("alice", "2031-07-16T02:40:13Z", "edited later", edited="2031-07-27T05:00:00Z"),
                ]),
                gql_review(LOGIN, "COMMENTED", "2031-07-17T10:00:00Z", [gql_comment(LOGIN, "2031-07-17T10:00:00Z", "an earlier point")]),
                gql_review(LOGIN, "APPROVED", "2031-07-19T21:35:04Z", [gql_comment(LOGIN, "2031-07-19T21:09:55Z", "the answer")]),
                gql_review("dave", "COMMENTED", "2031-07-27T05:29:47Z", [gql_comment("dave", "2031-07-27T05:29:47Z", "fixed")]),
            ],
            comments=[
                ("codeowners-bot", "2031-07-14T05:42:44Z", None, "code owners"),
                ("ci-review-bot", "2031-07-08T02:13:26Z", "2031-07-27T15:51:22Z", "deployment, rewritten later"),
                ("dave", "2031-07-20T09:00:00Z", None, "after the cutoff"),
            ],
        )
        github = conversation(pr, T)
        self.assertEqual(summary(github), (
            [("alice", "APPROVED", ["rename this"]), (LOGIN, "COMMENTED", ["an earlier point"])],
            ["code owners"],
        ))
        self.assertNotIn("the answer", json.dumps(github))
        self.assertEqual(github["as_of"], "2031-07-19T21:35:04+00:00")

    def test_cutoff_reads_the_body_as_it_was_then(self):
        # GitHub lists body versions newest first, the first version included, once a body is edited.
        edits = [("2031-07-27T00:32:19Z", "v3, after the restack"), ("2031-07-16T01:00:00Z", "v2"), ("2031-07-08T02:03:58Z", "v1")]
        pr = gql_pr(body="v3, after the restack", edits=edits)
        self.assertEqual(conversation(pr, T)["pr"]["body"], "v2")
        self.assertEqual(conversation(pr)["pr"]["body"], "v3, after the restack")
        self.assertEqual(conversation(gql_pr(body="never edited"), T)["pr"]["body"], "never edited")

    def test_a_list_longer_than_one_page_fails(self):
        pr = gql_pr(reviews=[gql_review("alice", "APPROVED", "2031-07-16T02:43:19Z")])
        pr["reviews"]["totalCount"] = 101
        with self.assertRaises(ValueError):
            conversation(pr)


class FailureCauseTest(unittest.TestCase):
    """failure_cause, which names what stopped a review from the step of bin/whooper-review that failed
    and that step's error output, on output the real commands print."""

    def test_a_setup_script_that_cannot_reach_docker_stops_every_review(self):
        for output in [
            # OrbStack with a newer docker CLI, after a warning of the script's own.
            "! You are not logged in to FloxHub. Run 'flox auth login' to log in.\n"
            "failed to connect to the docker API at unix:///Users/sam/.orbstack/run/docker.sock; check if the path is"
            " correct and if the daemon is running: dial unix /Users/sam/.orbstack/run/docker.sock: connect: no such"
            " file or directory\n",
            # Docker Desktop, from docker run, and colima.
            "docker: Cannot connect to the Docker daemon at unix:///Users/sam/.docker/run/docker.sock. Is the docker"
            " daemon running?\nRun 'docker run --help' for more information\n",
            "Cannot connect to the Docker daemon at unix:///Users/sam/.colima/default/docker.sock. Is the docker daemon"
            " running?\n",
        ]:
            with self.subTest(output):
                self.assertEqual(failure_cause("setup", output), CAUSES["docker_unavailable"])

    def test_any_other_setup_failure_stops_every_review_too(self):
        for output in ["", "make: *** [migrate] Error 1\n", "docker: Error response from daemon: Conflict. The container"
                       ' name "/whooper-postgres" is already in use.\n']:
            with self.subTest(output):
                self.assertEqual(failure_cause("setup", output), CAUSES["setup_failed"])

    def test_a_fetch_github_refuses_for_want_of_a_login_stops_every_review(self):
        for output in [
            # git with gh as its credential helper, signed out, and no terminal to ask on.
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n",
            "fatal: could not read Username for 'https://github.com': Device not configured\n",
            # git with a token GitHub no longer takes.
            "remote: Invalid username or token. Password authentication is not supported for Git operations.\n"
            "fatal: Authentication failed for 'https://github.com/example-org/example-repo.git/'\n",
            # gh itself.
            "To get started with GitHub CLI, please run:  gh auth login\n"
            "Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.\n",
            "HTTP 401: Bad credentials (https://api.github.com/graphql)\nTry authenticating with:  gh auth login\n",
        ]:
            with self.subTest(output):
                self.assertEqual(failure_cause("fetch", output), CAUSES["gh_signed_out"])

    def test_any_other_fetch_failure_is_unknown(self):
        for output in ["fatal: couldn't find remote ref no-such-branch\n",
                       "fatal: unable to access 'https://github.com/example-org/example-repo.git/': Could not resolve"
                       " host: github.com\n"]:
            with self.subTest(output):
                self.assertEqual(failure_cause("fetch", output), CAUSES["unknown"])

    def test_the_other_steps_name_their_cause_whatever_they_printed(self):
        for step, cause in [("clone", "repo_not_configured"), ("time-limit", "time_limit"),
                            ("findings", "agent_no_findings"), ("prepare", "unknown"), ("review", "unknown"),
                            ("interrupted", "unknown"), (None, "unknown")]:
            with self.subTest(step):
                # Docker's and gh's messages count only for the steps that run them.
                self.assertEqual(failure_cause(step, "Cannot connect to the Docker daemon. gh auth login"),
                                 CAUSES[cause])


def tool_call(tool_use_id, name, output, command="", is_error=False, subagent=None):
    """Stream-json events for one tool call and its result. A failed Bash call's output starts with its
    exit code. subagent is the ID of the Task call whose subagent made the call."""
    return [
        {"type": "assistant", "parent_tool_use_id": subagent, "message": {"content": [
            {"type": "tool_use", "id": tool_use_id, "name": name, "input": {"command": command}}]}},
        {"type": "user", "parent_tool_use_id": subagent, "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": output, "is_error": is_error}]}},
    ]


RUN = "/tmp/whooper/data/runs/20310727T010246Z-12"
API = "github.com/example-org/example-repo/services/orders/internal/api"
# A probe as prompts/review.md asks for it: it starts by entering one worktree.
PROBE = "cd {}/services/orders && go test -run TestCreateOrder ./internal/api"
HEAD_FAILS = f"Exit code 1\n--- FAIL: TestCreateOrder\n    got status 500, want 422\nFAIL\t{API}\t0.5s"

# The CLI gives a tool result's content either as a string or as a list of text blocks.
EVENTS = [
    {"type": "user", "message": {"content": "the review prompt"}},
    # The head probe fails, and the same probe passes at the merge base.
    *tool_call("toolu_head", "Bash", HEAD_FAILS, PROBE.format(f"{RUN}/head"), is_error=True),
    *tool_call("toolu_base", "Bash", [{"type": "text", "text": f"ok  \t{API}\t0.4s"}], PROBE.format(f"{RUN}/base")),
    *tool_call("toolu_read", "Read", "got status 500, want 422"),
    # Copying the PR's test into the base worktree prints nothing.
    *tool_call("toolu_cp", "Bash", "(Bash completed with no output)",
               f"cp {RUN}/head/services/orders/internal/api/orders_test.go {RUN}/base/services/orders/internal/api/"),
    # The copied test calls a helper the PR adds, so at the merge base it does not compile and no base code runs.
    *tool_call("toolu_base_build", "Bash",
               f"Exit code 1\n# {API} [{API}.test]\ninternal/api/orders_test.go:40:9: undefined: newOrderRequest\n"
               f"FAIL\t{API} [build failed]", PROBE.format(f"{RUN}/base"), is_error=True),
    # A base output over 30,000 characters, of which the transcript keeps only a preview.
    *tool_call("toolu_base_long", "Bash",
               "<persisted-output>\nOutput too large (48.2KB). Full output saved to: /tmp/b1.txt\n\n"
               "Preview (first 2KB):\n=== RUN   TestCreateOrder\n...\n</persisted-output>", PROBE.format(f"{RUN}/base")),
    # The same probe in both trees, where an earlier cd that the commands do not show picked the tree.
    *tool_call("toolu_head_cwd", "Bash", HEAD_FAILS, "go test -run TestCreateOrder ./internal/api", is_error=True),
    *tool_call("toolu_base_cwd", "Bash", f"ok  \t{API}\t0.4s", "go test -run TestCreateOrder ./internal/api"),
    # A subagent's head probe, inside the main session's Task call.
    *tool_call("toolu_sub_head", "Bash", HEAD_FAILS, PROBE.format(f"{RUN}/head"), is_error=True, subagent="toolu_task"),
    *tool_call("toolu_task", "Task", "The test fails at the head; toolu_sub_head shows it."),
]


def finding(**overrides):
    out = {
        "id": "1", "path": "services/orders/internal/api/orders.go", "line": 88, "side": "RIGHT", "in_diff": True,
        "title": "Bad JSON now returns 500", "draft": "**issue:** ...", "kind": "caller_visible_bug",
        "rule_v1": {"ran_and_saw_failure": True, "caller_or_ci_visible": True, "introduced_by_pr": True,
                    "not_already_raised": True, "not_pr_text": True},
        "head_run": "toolu_head", "base_run": "toolu_base", "observed": "got status 500, want 422",
    }
    out.update(overrides)
    return out


class ClaudeShellCallsTest(unittest.TestCase):
    def test_lists_only_the_main_sessions_bash_calls(self):
        self.assertEqual(sorted(claude_shell_calls(EVENTS)), sorted([
            "toolu_head", "toolu_base", "toolu_cp", "toolu_base_build", "toolu_base_long", "toolu_head_cwd",
            "toolu_base_cwd"]))

    def test_reads_each_calls_command_output_and_outcome(self):
        calls = claude_shell_calls(EVENTS)
        self.assertEqual(calls["toolu_head"], {"command": PROBE.format(f"{RUN}/head"), "output": HEAD_FAILS,
                                               "ok": False, "cut": False, "finished": True})
        # Text blocks join into one output.
        self.assertEqual(calls["toolu_base"]["output"], f"ok  \t{API}\t0.4s")
        self.assertTrue(calls["toolu_base"]["ok"])
        self.assertTrue(calls["toolu_base_long"]["cut"])


def exec_step(call_id, output, command="", function="exec", workdir=None):
    """One agent step of a Devin ATIF export: one tool call and its result."""
    arguments = {"command": command, **({"workdir": workdir} if workdir else {})}
    return {"source": "agent", "tool_calls": [{"tool_call_id": call_id, "function_name": function, "arguments": arguments}],
            "observation": {"results": [{"source_call_id": call_id, "content": output}]}}


def exec_output(output, exit_code):
    """What exec reports for a command that finished."""
    return f"Output from command in shell 5fae99:\n{output}\n\n\nExit code: {exit_code}"


HEAD_FAILS_EXEC = exec_output(f"--- FAIL: TestCreateOrder\n    got status 500, want 422\nFAIL\t{API}\t0.5s", 1)
# The same calls as EVENTS, as a Devin run's export records them. A subagent's calls are never in it.
EXPORT = {"schema_version": "ATIF-v1.7", "steps": [
    {"source": "user", "message": "the review prompt"},
    exec_step("toolu_head", HEAD_FAILS_EXEC, PROBE.format(f"{RUN}/head")),
    exec_step("toolu_base", exec_output(f"ok  \t{API}\t0.4s", 0), PROBE.format(f"{RUN}/base")),
    exec_step("toolu_read", "got status 500, want 422", function="read"),
    exec_step("toolu_cp", exec_output("", 0),
              f"cp {RUN}/head/services/orders/internal/api/orders_test.go {RUN}/base/services/orders/internal/api/"),
    exec_step("toolu_base_build", exec_output(
        f"# {API} [{API}.test]\ninternal/api/orders_test.go:40:9: undefined: newOrderRequest\nFAIL\t{API} [build failed]", 1),
        PROBE.format(f"{RUN}/base")),
    # exec keeps the start and end of a long output and names the file with the rest.
    exec_step("toolu_base_long", "Output from command in shell 5fae99. The full terminal output is being written live to "
              "/tmp/devin-overflows-502/shell-5fae99/content.txt; read or grep it for anything omitted below. Preview:\n"
              "<file-view>\n    1|=== RUN   TestCreateOrder\n</file-view>\n\nExit code: 0", PROBE.format(f"{RUN}/base")),
    exec_step("toolu_head_cwd", HEAD_FAILS_EXEC, "go test -run TestCreateOrder ./internal/api"),
    exec_step("toolu_base_cwd", exec_output(f"ok  \t{API}\t0.4s", 0), "go test -run TestCreateOrder ./internal/api"),
    # A probe silent for 10 seconds, which exec moved to the background.
    exec_step("toolu_base_bg", "No output produced after 10.0 seconds. Command running in background with ID: 21d1ae. "
              "Run get_output with timeout if you need to wait.", PROBE.format(f"{RUN}/base")),
    exec_step("toolu_task", "Subagent agent_id=b147292a completed successfully: the test fails at the head.",
              function="run_subagent"),
]}
# Each agent's shell calls for the same review, from its own transcript reader.
CALLS = {"claude": claude_shell_calls(EVENTS), "devin": devin_shell_calls(EXPORT)}


class DevinShellCallsTest(unittest.TestCase):
    def test_lists_only_exec_calls(self):
        self.assertEqual(sorted(devin_shell_calls(EXPORT)), sorted([
            "toolu_head", "toolu_base", "toolu_cp", "toolu_base_build", "toolu_base_long", "toolu_head_cwd",
            "toolu_base_cwd", "toolu_base_bg"]))

    def test_reads_each_calls_outcome_from_execs_report(self):
        calls = devin_shell_calls(EXPORT)
        self.assertEqual(calls["toolu_head"], {"command": PROBE.format(f"{RUN}/head"), "output": HEAD_FAILS_EXEC,
                                               "ok": False, "cut": False, "finished": True})
        self.assertTrue(calls["toolu_base"]["ok"])
        self.assertEqual((calls["toolu_base_long"]["ok"], calls["toolu_base_long"]["cut"]), (True, True))
        self.assertEqual((calls["toolu_base_bg"]["ok"], calls["toolu_base_bg"]["finished"]), (False, False))

    def test_a_result_too_large_for_the_transcript_finished_but_is_cut(self):
        output = ("Output from command in shell 11f977:\nPROBE status=422 body=...\n... (1422 lines shown).\n"
                  "<truncation_notice>\nFull output written to: /tmp/devin-overflows-502/4c5282b9/content.txt\n"
                  "</truncation_notice>")
        call = devin_shell_calls({"steps": [exec_step("call_1", output, PROBE.format(f"{RUN}/head"))]})["call_1"]
        self.assertEqual((call["finished"], call["cut"], call["ok"]), (True, True, False))

    def test_a_workdir_reads_as_a_cd(self):
        export = {"steps": [exec_step("call_1", exec_output("ok", 0), "go test ./internal/api", workdir=f"{RUN}/head")]}
        self.assertEqual(devin_shell_calls(export)["call_1"]["command"], f"cd {RUN}/head && go test ./internal/api")


# What whooper-review leaves of findings.json when it stops a review before its schema check, such as
# at the time limit or for an empty file, each with the end of the refusal that whooper-draft,
# whooper-submit, and whooper-classify give after the run's directory. A Devin review writes the file
# itself, so one stopped at its time limit can leave findings and little else.
UNFIT = "has a findings.json that does not fit schemas/findings.json, so its review did not finish: "
UNFINISHED = [
    ("no findings.json", None, "has no findings.json, so its review did not finish"),
    ("empty", "", UNFIT + "Expecting value: line 1 column 1 (char 0)"),
    ("not JSON", '{"status": "revi', UNFIT + "Unterminated string starting at: line 1 column 12 (char 11)"),
    ("no list of findings", "{}", UNFIT + "findings.json lacks status, skip_reason, approval, findings"),
    ("findings and nothing else", json.dumps({"findings": [finding()]}),
     UNFIT + "findings.json lacks status, skip_reason, approval"),
    ("a finding without its rule v1 answers", json.dumps({
        "status": "reviewed", "skip_reason": "", "approval": {"verdict": "look_first", "reason": "Ran no probe."},
        "findings": [{key: value for key, value in finding().items() if key != "rule_v1"}]}),
     UNFIT + "findings.json.findings[0] lacks rule_v1"),
]


class ClassifyTest(unittest.TestCase):
    def verdicts(self, f, fetched=True):
        """The finding's verdict and failed checks under each agent's reader."""
        result = {"status": "reviewed", "skip_reason": "", "findings": [f]}
        return {agent: (out["verdict"], out["checks_failed"])
                for agent, calls in CALLS.items() for [out] in [classify(result, calls, fetched, RUN)]}

    def test_all_checks_pass_is_auto_post(self):
        self.assertEqual(self.verdicts(finding()), {agent: ("auto_post", []) for agent in CALLS})

    def test_any_failed_check_sends_the_finding_to_the_digest(self):
        cases = [
            (name, finding(rule_v1={**finding()["rule_v1"], name: False}), True)
            for name in ["ran_and_saw_failure", "caller_or_ci_visible", "introduced_by_pr", "not_already_raised", "not_pr_text"]
        ] + [
            ("line outside the diff", finding(in_diff=False), True),
            ("head_run names no tool call", finding(head_run="toolu_missing"), True),
            ("head_run names a Read call", finding(head_run="toolu_read"), True),
            ("head_run names a subagent's call", finding(head_run="toolu_sub_head"), True),
            ("base_run names no tool call", finding(base_run=""), True),
            ("base_run names a Read call", finding(base_run="toolu_read"), True),
            ("base_run names another command", finding(base_run="toolu_cp"), True),
            ("the probe names no worktree", finding(head_run="toolu_head_cwd", base_run="toolu_base_cwd"), True),
            ("base_run failed", finding(base_run="toolu_base_build"), True),
            ("base_run's output was cut", finding(base_run="toolu_base_long"), True),
            ("base_run went to the background", finding(base_run="toolu_base_bg"), True),
            ("observed is not in the head output", finding(observed="got status 400"), True),
            ("observed is also in the base output", finding(observed="services/orders/internal/api"), True),
            ("the GitHub fetch failed", finding(), False),
        ]
        for name, f, fetched in cases:
            for agent, (verdict, failed) in self.verdicts(f, fetched).items():
                with self.subTest(name, agent=agent):
                    self.assertEqual(verdict, "digest")
                    self.assertEqual(len(failed), 1, failed)

    def test_every_nit_is_a_shadow_nit(self):
        for name, f in [
            ("passes every check", finding(kind="nit", nit_kind="codeowners_no_effect")),
            ("fails a check", finding(kind="nit", nit_kind="wording", in_diff=False)),
        ]:
            with self.subTest(name):
                self.assertEqual({v for v, _ in self.verdicts(f).values()}, {"shadow_nit"})

    def test_skipped_run_has_no_verdicts(self):
        # The schema allows findings on a skipped run, and a skipped PR gets no comments.
        result = {"status": "skipped", "skip_reason": "not small", "findings": [finding()]}
        for agent, calls in CALLS.items():
            with self.subTest(agent):
                self.assertEqual(classify(result, calls, True, RUN), [])

    def test_a_review_that_did_not_finish_is_not_classified(self):
        with tempfile.TemporaryDirectory() as data:
            run = Path(data) / "runs" / "20310727T010246Z-12"
            run.mkdir(parents=True)
            (run / "run.json").write_text(json.dumps({
                "kind": "first_review", "repo": "example-org/example-repo", "pr": 12, "head_sha": "h1",
                "base_ref": "master", "source": "github_request", "slack": None, "reason": "r",
                "run_id": run.name, "dry_run": True, "as_of": None, "agent": "claude"}))
            (run / "transcript.jsonl").write_text("")
            for name, content, why in UNFINISHED:
                with self.subTest(name):
                    if content is None:
                        (run / "findings.json").unlink(missing_ok=True)
                    else:
                        (run / "findings.json").write_text(content)
                    with self.assertRaises(SystemExit) as caught:
                        main("classify", data, str(run))
                    self.assertEqual(str(caught.exception), f"whooper-classify: {run} {why}")
            # Nothing went to the digest or the PR's record.
            self.assertEqual(sorted(p.name for p in Path(data).iterdir()), ["runs"])
            self.assertFalse((run / "verdicts.json").exists())


class CheckSchemaTest(unittest.TestCase):
    SCHEMA = json.loads((ROOT / "schemas" / "findings.json").read_text())

    def result(self, **overrides):
        out = {"status": "reviewed", "skip_reason": "", "findings": [finding()],
               "approval": {"verdict": "look_first", "reason": "Ran the api tests; no database path."}}
        out.update(overrides)
        return out

    def test_a_result_that_fits_passes(self):
        check_schema(self.result(), self.SCHEMA)

    def test_the_findings_hold_no_overview(self):
        # The overview is a file of its own, overview.json, which cannot fail the review (OverviewTest).
        self.assertNotIn("overview", self.SCHEMA["properties"])

    def test_each_break_is_caught(self):
        no_approval = self.result()
        del no_approval["approval"]
        for name, value in [
            ("a required key is missing", no_approval),
            ("a value is outside its enum", self.result(status="done")),
            ("an unexpected key", self.result(extra="x")),
            ("a string where an integer belongs", self.result(findings=[finding(line="88")])),
            ("true where an integer belongs", self.result(findings=[finding(line=True)])),
            ("an integer below its minimum", self.result(findings=[finding(line=0)])),
            # JSON Schema counts 12.0 as an integer, but the code that reads a finding's line takes an int.
            ("a float with no fraction where an integer belongs", self.result(findings=[finding(line=12.0)])),
            ("an integer where a boolean belongs", self.result(findings=[finding(in_diff=1)])),
        ]:
            with self.subTest(name), self.assertRaises(ValueError):
                check_schema(value, self.SCHEMA)

    def test_it_checks_the_keywords_of_prompting_times_status_schema(self):
        schema = json.loads((ROOT / "schemas" / "prompting-time-status.json").read_text())
        item = {"id": "r1", "group": "look", "title": "T", "detail": None, "dot": "attention", "project": None,
                "updated_at": "2031-07-30T15:00:00-07:00", "route": "/reviews/r1", "actions": ["skip"]}
        status = {"format": 2, "agent": {"words": "Running", "dot": "idle"}, "blockers": [], "items": [item],
                  "truncated": False}
        check_schema(status, schema)
        for name, changes in [
            ("another format, by const", {"format": 1}),
            ("true for the format, which is 1 to Python", {"format": True}),
            ("a dot outside the $defs enum", {"agent": {"words": "Running", "dot": "red"}}),
            ("an empty id, below minLength", {"items": [{**item, "id": ""}]}),
            ("a route that is not a path, by the $defs pattern", {"items": [{**item, "route": "reviews/r1"}]}),
            ("a row action twice, against uniqueItems", {"items": [{**item, "actions": ["skip", "skip"]}]}),
            ("more than 50 items", {"items": [item] * 51}),
        ]:
            with self.subTest(name), self.assertRaises(ValueError):
                check_schema({**status, **changes}, schema)

    def test_an_exclusive_minimum_takes_only_numbers_above_it(self):
        # schemas/settings.json takes an [ask] limit of any number of minutes above 0.
        schema = {"type": "number", "exclusiveMinimum": 0}
        for value in [0.05, 2.5, 10]:
            with self.subTest(value):
                check_schema(value, schema)
        for value in [0, 0.0, -1]:
            with self.subTest(value), self.assertRaises(ValueError) as refused:
                check_schema(value, schema, "settings.ask.timeout_minutes")
            self.assertEqual(str(refused.exception), "settings.ask.timeout_minutes is not above 0")

    def test_the_check_command_fails_on_a_file_that_does_not_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "findings.json"
            for text in [json.dumps(self.result(status="done")), "not JSON"]:
                path.write_text(text)
                with self.subTest(text), self.assertRaises(SystemExit):
                    main("check", str(ROOT / "schemas" / "findings.json"), str(path))


# A run's diff.patch with each kind of file git diff writes: a changed file with two hunks, an added
# file, a deleted file, a binary file, a pure rename, a path git quotes, and a path with a space, whose
# --- and +++ lines git ends with a tab.
OVERVIEW_PATCH = "".join([
    "diff --git a/api/runs.go b/api/runs.go\nindex 1111111..2222222 100644\n--- a/api/runs.go\n+++ b/api/runs.go\n",
    "@@ -10,7 +10,8 @@ func List() {\n a\n b\n c\n-d\n+D\n+E\n f\n g\n h\n",
    "@@ -40,6 +41,7 @@ func Page() {\n p\n q\n r\n+s\n t\n u\n v\n",
    "diff --git a/api/cursor.go b/api/cursor.go\nnew file mode 100644\nindex 0000000..3333333\n--- /dev/null\n",
    "+++ b/api/cursor.go\n@@ -0,0 +1,3 @@\n+package api\n+\n+type Cursor string\n",
    "diff --git a/api/old.go b/api/old.go\ndeleted file mode 100644\nindex 4444444..0000000\n--- a/api/old.go\n",
    "+++ /dev/null\n@@ -1,2 +0,0 @@\n-package api\n-// Old paging.\n",
    "diff --git a/web/logo.png b/web/logo.png\nindex 5555555..6666666 100644\n",
    "Binary files a/web/logo.png and b/web/logo.png differ\n",
    "diff --git a/docs/a.md b/docs/b.md\nsimilarity index 100%\nrename from docs/a.md\nrename to docs/b.md\n",
    'diff --git "a/docs/caf\\303\\251.md" "b/docs/caf\\303\\251.md"\nindex 7777777..8888888 100644\n',
    '--- "a/docs/caf\\303\\251.md"\n+++ "b/docs/caf\\303\\251.md"\n@@ -1 +1 @@\n-old\n+new\n',
    "diff --git a/docs/read me.md b/docs/read me.md\nindex 9999999..aaaaaaa 100644\n",
    "--- a/docs/read me.md\t\n+++ b/docs/read me.md\t\n@@ -2,0 +3 @@ intro\n+added\n",
])


def overview(**overrides):
    """An overview of OVERVIEW_PATCH that keeps every rule of prompts/review.md's overview."""
    out = {
        "summary": "Pages GET /v1/runs by a cursor, so exports of large projects finish.",
        "verdict_reason": "The cursor skips runs created in the same millisecond.",
        "before_after": [{"label": "API callers", "before": "GET /v1/runs returns at most 100 runs.",
                          "after": "It returns up to 500 runs and a next_cursor."}],
        "places": [{"id": "p1", "title": "Runs with equal timestamps", "reason": "Page orders by created_at only.",
                    "evidence": {"kind": "ran", "detail": "TestPage at head and base"},
                    "path": "api/runs.go", "start_line": 41, "end_line": 47}],
        "layers": [
            {"id": "l1", "title": "The cursor", "intent": "A cursor type, and List and Page take it.", "read_hint": "read",
             "ranges": [{"path": "api/cursor.go", "start_line": 1, "end_line": 3},
                        {"path": "api/runs.go", "start_line": 12, "end_line": 14},
                        {"path": "api/runs.go", "start_line": 41, "end_line": 47}]},
            {"id": "l2", "title": "The old paging", "intent": "Removed.", "read_hint": "skim",
             "ranges": [{"path": "api/old.go", "start_line": 1, "end_line": 2}]},
            {"id": "l3", "title": "Docs", "intent": "Wording only.", "read_hint": "skip",
             "ranges": [{"path": "docs/café.md", "start_line": 1, "end_line": 1},
                        {"path": "docs/read me.md", "start_line": 3, "end_line": 3}]},
        ],
        "flow": {"title": "Paging an export", "input": "GET /v1/runs?cursor=c1", "calls": [
            {"from": "handler", "to": "store", "label": "Page(c1)", "status": "changed", "old_label": "List()",
             "path": "api/runs.go", "line": 44},
            {"from": "store", "to": "MySQL", "label": "SELECT ... LIMIT 500", "status": "same", "old_label": None,
             "path": None, "line": None}]},
        "checked": ["Ran TestPage at head and base."],
        "not_checked": ["The query plan: the sandbox has no database."],
    }
    out.update(overrides)
    return out


class OverviewTest(unittest.TestCase):
    """check_overview, the checks whooper-review runs on the overview.json a review run wrote, and
    whooper.py overview, which moves an overview.json that fails them aside."""

    FINDINGS = {"status": "reviewed", "skip_reason": "", "findings": [finding()],
                "approval": {"verdict": "look_first", "reason": "Ran the api tests; no database path."}}

    def test_an_overview_that_keeps_every_rule_passes(self):
        check_overview(overview(), OVERVIEW_PATCH)
        # A PR that changes no flow between components has none.
        check_overview(overview(flow=None), OVERVIEW_PATCH)

    def test_each_break_is_caught_and_named(self):
        def outside(start, end, path="api/runs.go"):
            return {"path": path, "start_line": start, "end_line": end}

        for name, change, reason in [
            ("a range between two hunks", lambda o: o["layers"][0]["ranges"][1].update(start_line=20, end_line=22),
             "overview.layers[0].ranges[1], lines 20 to 22 of api/runs.go, is not inside one hunk of the diff"),
            ("a range across two hunks", lambda o: o["layers"][0]["ranges"][1].update(start_line=12, end_line=45),
             "overview.layers[0].ranges[1], lines 12 to 45 of api/runs.go, is not inside one hunk of the diff"),
            ("a range that ends before it starts", lambda o: o["layers"][0]["ranges"][1].update(start_line=14, end_line=12),
             "overview.layers[0].ranges[1], lines 14 to 12 of api/runs.go, is not inside one hunk of the diff"),
            ("a deleted file's range past its last line", lambda o: o["layers"][1]["ranges"][0].update(end_line=3),
             "overview.layers[1].ranges[0], lines 1 to 3 of api/old.go, is not inside one hunk of the diff"),
            ("a range in a file whose diff shows no lines", lambda o: o["layers"][2]["ranges"].append(outside(1, 1, "web/logo.png")),
             "overview.layers[2].ranges[2], lines 1 to 1 of web/logo.png, is not inside one hunk of the diff"),
            ("a place outside the hunks", lambda o: o["places"][0].update(start_line=30, end_line=31),
             "overview.places[0], lines 30 to 31 of api/runs.go, is not inside one hunk of the diff"),
            ("a place in a file the diff does not change", lambda o: o["places"][0].update(path="api/other.go"),
             "overview.places[0] names api/other.go, which the diff does not change"),
            ("a changed file in no layer", lambda o: o["layers"].pop(2),
             "docs/café.md is in no layer"),
            ("a changed file in two layers", lambda o: o["layers"][1]["ranges"].append(outside(1, 3, "api/cursor.go")),
             "api/cursor.go is in overview.layers[0] and overview.layers[1]"),
            ("six places", lambda o: o.update(places=o["places"] * 6),
             "overview.places has more than 5 items"),
            ("two flows", lambda o: o.update(flow=[o["flow"], o["flow"]]),
             "overview.flow is not of type object or null"),
            ("evidence of another kind", lambda o: o["places"][0]["evidence"].update(kind="guessed"),
             "overview.places[0].evidence.kind is 'guessed', not one of ['ran', 'read', 'searched']"),
            ("another read hint", lambda o: o["layers"][0].update(read_hint="deep"),
             "overview.layers[0].read_hint is 'deep', not one of ['read', 'skim', 'skip']"),
            ("another call status", lambda o: o["flow"]["calls"][0].update(status="moved"),
             "overview.flow.calls[0].status is 'moved', not one of ['same', 'added', 'removed', 'changed']"),
            ("a call on line 0", lambda o: o["flow"]["calls"][0].update(line=0),
             "overview.flow.calls[0].line is below 1"),
            ("a line given as text", lambda o: o["places"][0].update(start_line="41"),
             "overview.places[0].start_line is not of type integer"),
            ("a missing part", lambda o: o.pop("not_checked"),
             "overview lacks not_checked"),
        ]:
            value = copy.deepcopy(overview())
            change(value)
            with self.subTest(name), self.assertRaises(ValueError) as caught:
                check_overview(value, OVERVIEW_PATCH)
            self.assertEqual(str(caught.exception), reason, name)

    def test_names_each_file_and_its_hunks_as_the_page_does(self):
        # Each file by its path in the head, or a deleted file's at the merge base, with its path at the merge
        # base, the first and last line of each hunk, counted in the head, or a deleted file's at the merge base,
        # and the lines the patch adds, counted in the head, and removes, counted at the merge base. The binary
        # file and the pure rename show no lines.
        self.assertEqual(diff_files(OVERVIEW_PATCH), [
            ("api/runs.go", "api/runs.go", [(10, 17), (41, 47)], {13, 14, 44}, {13}),
            ("api/cursor.go", None, [(1, 3)], {1, 2, 3}, set()),
            ("api/old.go", "api/old.go", [(1, 2)], set(), {1, 2}),
            ("web/logo.png", "web/logo.png", [], set(), set()),
            ("docs/b.md", "docs/a.md", [], set(), set()),
            ("docs/café.md", "docs/café.md", [(1, 1)], {1}, {1}),
            ("docs/read me.md", "docs/read me.md", [(3, 3)], {3}, set()),
        ])
        # A form feed in a line of code ends no line, and "\ No newline at end of file" is none of the file's.
        patch = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-b\x0cc\n+b\n d\n"
                 "\\ No newline at end of file\n")
        self.assertEqual(diff_files(patch), [("x.py", "x.py", [(1, 3)], {2}, {2})])

    def test_the_overview_command_keeps_an_overview_that_keeps_every_rule_and_moves_any_other_aside(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "diff.patch").write_text(OVERVIEW_PATCH)
            findings = json.dumps(self.FINDINGS)
            (run / "findings.json").write_text(findings)
            # Without overview.json it changes nothing, and it keeps one that keeps every rule. Either way it says
            # nothing.
            err = io.StringIO()
            with redirect_stderr(err):
                main("overview", str(run))
            self.assertEqual(sorted(path.name for path in run.iterdir()), ["diff.patch", "findings.json"])
            kept = json.dumps(overview())
            (run / "overview.json").write_text(kept)
            with redirect_stderr(err):
                main("overview", str(run))
            self.assertEqual(err.getvalue(), "")
            self.assertEqual((run / "overview.json").read_text(), kept)
            self.assertFalse((run / "overview.invalid.json").exists())
            # An overview.json that breaks a rule, is not JSON, as when the run stopped while writing it, or cannot
            # be read moves to overview.invalid.json, with why and what it held, and why goes to the error output.
            # findings.json stays as it was.
            broken = overview(places=[{**overview()["places"][0], "start_line": 30, "end_line": 31}])
            for content, reason, held in [
                (json.dumps(broken).encode(),
                 "overview.places[0], lines 30 to 31 of api/runs.go, is not inside one hunk of the diff", broken),
                (b'{"summary": "Pages GET',
                 "overview.json is not JSON: Unterminated string starting at: line 1 column 13 (char 12)",
                 '{"summary": "Pages GET'),
                (b"\xff\xfe{}",
                 "overview.json cannot be read: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
                 None),
            ]:
                with self.subTest(reason):
                    (run / "overview.json").write_bytes(content)
                    err = io.StringIO()
                    with redirect_stderr(err):
                        main("overview", str(run))
                    self.assertEqual(err.getvalue(), f"whooper-review: dropped the run's overview: {reason}\n")
                    self.assertFalse((run / "overview.json").exists())
                    self.assertEqual(json.loads((run / "overview.invalid.json").read_text()),
                                     {"reason": reason, "overview": held})
                    self.assertEqual((run / "findings.json").read_text(), findings)


class CoChangeTest(unittest.TestCase):
    """co_change, which picks the files whooper-show lists as changing together with the PR's files, from
    the files each commit changed in the 90 days before the review."""

    def test_a_file_counts_from_60_percent_of_a_pr_files_commits_and_3_commits(self):
        # p changed in 3 of x's 5 commits, 60%; in 3 of 6, in both of y's 2, or as one of the PR's files, it
        # does not count.
        self.assertEqual(co_change([{"x", "p"}] * 3 + [{"x"}] * 2, {"x"}),
                         [{"path": "p", "partner": "x", "ratio": 0.6, "commits": 3}])
        self.assertEqual(co_change([{"x", "p"}] * 3 + [{"x"}] * 3, {"x"}), [])
        self.assertEqual(co_change([{"y", "p"}] * 2, {"y"}), [])
        self.assertEqual(co_change([{"x", "p"}] * 3, {"x", "p"}), [])

    def test_lists_at_most_5_files_highest_ratio_first_each_with_the_pr_file_it_changes_with_most(self):
        commits = [
            {"app", "c1", "c2", "c3", "d1", "d2"},
            {"app", "c1", "c2", "c3", "d1", "d2"},
            {"app", "c1", "c2", "c3", "d1", "d2"},
            {"app", "c1", "c2"},
            {"app", "c1"},
            {"lib", "d1", "e1"},
            {"lib", "d1", "e1"},
            {"lib", "d1", "e1"},
            {"lib", "d1"},
        ]
        # d1 changed in 3 of app's 5 commits but in all 4 of lib's, so it counts once, with lib. d2 ties c3 at 3
        # of app's 5 commits, and comes sixth by its path.
        self.assertEqual(co_change(commits, {"app", "lib"}), [
            {"path": "c1", "partner": "app", "ratio": 1.0, "commits": 5},
            {"path": "d1", "partner": "lib", "ratio": 1.0, "commits": 4},
            {"path": "c2", "partner": "app", "ratio": 0.8, "commits": 4},
            {"path": "e1", "partner": "lib", "ratio": 0.75, "commits": 3},
            {"path": "c3", "partner": "app", "ratio": 0.6, "commits": 3},
        ])


# PR 12 of a shop: in its Go module, Total changes, Discount goes away, and Checkout, which called both at the
# merge base, now calls only Total; in its Python tools, summarize changes. Report calls Total, main calls
# summarize, and neither changes, and count names summarize only in a comment.
SHOP = {
    "go.mod": "module example.com/shop\n\ngo 1.21\n",
    "prices/prices.go": ("package prices\n\n// Total adds up the prices.\nfunc Total(prices []int) int {\n\tsum := 0\n"
                         "\tfor _, p := range prices {\n\t\tsum += p\n\t}\n\treturn sum\n}\n\n"
                         "// Discount takes a tenth off.\nfunc Discount(total int) int {\n\treturn total - total/10\n}\n"),
    "shop/shop.go": ('package shop\n\nimport "example.com/shop/prices"\n\n// Checkout charges for the cart.\n'
                     "func Checkout(cart []int) int {\n\treturn prices.Discount(prices.Total(cart))\n}\n"),
    "shop/report.go": ('package shop\n\nimport (\n\t"fmt"\n\n\t"example.com/shop/prices"\n)\n\n'
                       "// Report prints the cart's total.\nfunc Report(cart []int) string {\n"
                       "\treturn fmt.Sprint(prices.Total(cart))\n}\n"),
    "tools/report.py": "def summarize(rows):\n    return len(rows)\n",
    "tools/cli.py": ("from report import summarize\n\n\ndef main(rows):\n    print(summarize(rows))\n\n\n"
                     "def count(rows):\n    # Unlike summarize, this counts empty rows too.\n    return len(rows)\n"),
}
SHOP_PR = {
    **SHOP,
    "prices/prices.go": ("package prices\n\n// Total adds up the prices, leaving out refunds.\n"
                         "func Total(prices []int) int {\n\tsum := 0\n\tfor _, p := range prices {\n\t\tif p > 0 {\n"
                         "\t\t\tsum += p\n\t\t}\n\t}\n\treturn sum\n}\n"),
    "shop/shop.go": SHOP["shop/shop.go"].replace("prices.Discount(prices.Total(cart))", "prices.Total(cart)"),
    "tools/report.py": "def summarize(rows):\n    return len([row for row in rows if row])\n",
}
# The limits connections.json can give in plain words.
ONE_LEVEL = "Callers are one level up only: the code that calls those callers is not shown."
NO_GOPLS = ("Whooper found no gopls on PATH or where go install puts it, go env GOBIN or GOPATH/bin, so the Go links"
            " come from name search.")
GOPLS_MISSES = "gopls leaves out dynamic calls, such as calls through an interface, a function value, or reflection."
NAME_SEARCH = ("Name search links every whole-word match of a name, so a link can join code that only shares the name,"
               " and it misses calls that do not spell the name out.")
PATTERNS = ("Where neither gopls nor TypeScript read a file, Whooper finds its definitions by a pattern per language and"
            " counts each changed line toward the nearest definition above it, so a change between two definitions can"
            " count toward the wrong one.")
REMOVED = "Removed code shows the callers it had at the merge base."
TYPESCRIPT_MISSES = ("TypeScript leaves out dynamic calls, such as calls through a function value, a value typed any, or a"
                     " module import() loads at run time, and links a function only to code that calls it, not to code that"
                     " only names it, such as a list of handlers.")
JS_METHODS = ("In the JavaScript and TypeScript files TypeScript did not read, a change to a method of a class counts toward"
              " the class.")
NO_TYPESCRIPT = ("Whooper found no node_modules/typescript in its package or a folder above it, which the repo's setup"
                 " script would install")
# A TypeScript package's PR: total leaves out refunds, Cart's items become readonly, and Cart.add leaves out items that
# are not above 0. checkout calls total and Cart.add and uses Cart, report calls total by another name, an import's alias,
# and handlers names it without calling it, and useIt calls a total of its own, which shares only the name. node_modules
# stays out of git, as a package's .gitignore keeps it.
TS_SHOP = {
    "shop/package.json": '{"name": "shop", "private": true, "devDependencies": {"typescript": "5.9.3"}}\n',
    "shop/.gitignore": "node_modules/\n",
    "shop/tsconfig.json": ('{"compilerOptions": {"strict": true, "module": "esnext", "moduleResolution": "bundler",'
                           ' "target": "es2022", "noEmit": true}, "include": ["src"]}\n'),
    "shop/src/prices.ts": ("/** Totals the prices. */\nexport function total(prices: number[]): number {\n"
                           "  return prices.reduce((sum, p) => sum + p, 0);\n}\n\nexport class Cart {\n"
                           "  items: number[] = [];\n\n  add(item: number) {\n    this.items.push(item);\n  }\n}\n"),
    "shop/src/checkout.ts": ('import { Cart, total } from "./prices";\n\nexport function checkout(cart: number[]): number {\n'
                             "  const c = new Cart();\n  c.add(1);\n  return total(cart);\n}\n"),
    "shop/src/report.ts": ('import { total as sum } from "./prices";\n\n'
                           "export const report = (rows: number[]) => `Total: ${sum(rows)}`;\n\n"
                           "export const handlers = [sum];\n"),
    "shop/src/unrelated.ts": ("// Another total, which shares only the name.\nfunction total(n: number): number {\n"
                              "  return n;\n}\n\nexport function useIt(): number {\n  return total(1);\n}\n"),
}
TS_SHOP_PR = {
    **TS_SHOP,
    "shop/src/prices.ts": ("/** Totals the prices, leaving out refunds. */\nexport function total(prices: number[]): number {\n"
                           "  return prices.filter((p) => p > 0).reduce((sum, p) => sum + p, 0);\n}\n\nexport class Cart {\n"
                           "  readonly items: number[] = [];\n\n  add(item: number) {\n    if (item > 0) {\n"
                           "      this.items.push(item);\n    }\n  }\n}\n"),
}

# A React package's PR, which TypeScript 7's language server reads as JSX only when it opens each .tsx file as one: Row,
# which Table renders in the same file and Single in another, doubles its number, and the file's default export, which
# has no name and which Page renders, wraps its table.
JSX = {
    "web/package.json": '{"name": "web", "private": true, "devDependencies": {"typescript": "7.1.0-beta"}}\n',
    "web/.gitignore": "node_modules/\n",
    "web/tsconfig.json": ('{"compilerOptions": {"strict": true, "jsx": "preserve", "module": "esnext", "moduleResolution":'
                          ' "bundler", "target": "es2022", "noEmit": true}, "include": ["src"]}\n'),
    "web/src/rows.tsx": ("export function Row({ n }: { n: number }) {\n  return <li>{n}</li>;\n}\n\nexport function Table() {\n"
                         "  return <ul><Row n={1} /></ul>;\n}\n\nexport default function () {\n  return <Table />;\n}\n"),
    "web/src/page.tsx": ('import Rows, { Row } from "./rows";\n\nexport const Page = () => <Rows />;\n\n'
                         "export const Single = () => <Row n={2} />;\n"),
}
JSX_PR = {**JSX, "web/src/rows.tsx": JSX["web/src/rows.tsx"].replace("{n}</li>", "{n * 2}</li>").replace(
    "return <Table />", "return <section><Table /></section>")}


def ts_node(path, name, kind, line, change="unchanged", lines_changed=0):
    """A node of connections.json."""
    return {"id": f"{path}:{name}", "name": name, "kind": kind, "path": path, "line": line, "change": change,
            "lines_changed": lines_changed}


def ts_shop_by_name_search(why):
    """The connections.json of TS_SHOP's PR when name search finds its links and patterns its definitions, since
    TypeScript did not run, for why. The patterns find no method, so Cart.add's change counts toward Cart too, and name
    search links useIt and the other total, which only share total's name, and misses report, which calls total by
    another name."""
    def link(caller, called, kind="calls"):
        return {"from": caller, "to": called, "kind": kind, "found_by": "name_search"}

    return {
        "nodes": [ts_node("shop/src/prices.ts", "total", "func", 2, "changed", 4),
                  ts_node("shop/src/prices.ts", "Cart", "type", 6, "changed", 6),
                  ts_node("shop/src/checkout.ts", "checkout", "func", 3),
                  ts_node("shop/src/unrelated.ts", "total", "func", 2),
                  ts_node("shop/src/unrelated.ts", "useIt", "func", 6)],
        "edges": [link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:total"),
                  link("shop/src/unrelated.ts:total", "shop/src/prices.ts:total"),
                  link("shop/src/unrelated.ts:useIt", "shop/src/prices.ts:total"),
                  link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:Cart", "uses")],
        "limits": [ONE_LEVEL, f"The links of 1 changed file come from name search, since {why}: shop/src/prices.ts.",
                   NAME_SEARCH, PATTERNS, JS_METHODS],
    }


def ts_shop_by_typescript():
    """The connections.json of TS_SHOP's PR when TypeScript finds its links and definitions. TypeScript sees Cart.add as a
    method of its own, so Cart's own change is the line of its items. total's callers are checkout and report, which calls
    it by its alias, and not handlers, which only names it, or useIt, whose total is another function."""
    def link(caller, called, kind="calls"):
        return {"from": caller, "to": called, "kind": kind, "found_by": "typescript"}

    return {
        "nodes": [ts_node("shop/src/prices.ts", "total", "func", 2, "changed", 4),
                  ts_node("shop/src/prices.ts", "Cart", "type", 6, "changed", 2),
                  ts_node("shop/src/prices.ts", "Cart.add", "method", 9, "changed", 4),
                  ts_node("shop/src/checkout.ts", "checkout", "func", 3),
                  ts_node("shop/src/report.ts", "report", "func", 3)],
        "edges": [link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:total"),
                  link("shop/src/report.ts:report", "shop/src/prices.ts:total"),
                  link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:Cart", "uses"),
                  link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:Cart.add")],
        "limits": [ONE_LEVEL, TYPESCRIPT_MISSES],
    }


def leave_a_probe_and_an_edit(head):
    """Leaves in head, the head's checkout of TS_SHOP's PR, what a review run might: a probe that calls total, and a
    function it added to checkout.ts that calls total too. TypeScript reads neither, so checkout is where checkout.ts has it
    in the head."""
    (head / "shop" / "src" / "probe.ts").write_text(
        'import { total } from "./prices";\n\nexport const probe = () => total([1]);\n')
    checkout = head / "shop" / "src" / "checkout.ts"
    checkout.write_text("// probe\n\n" + checkout.read_text() + "\nexport const extra = () => total([]);\n")


def install_typescript(package):
    """Installs TypeScript in package's node_modules, as the repo's setup script would: a link to the one in
    PAGE_MODULES."""
    (package / "node_modules").mkdir(parents=True, exist_ok=True)
    (package / "node_modules" / "typescript").symlink_to(PAGE_MODULES / "typescript")


def fake_typescript(package, body, version="5.9.3"):
    """Installs in package's node_modules a typescript package of version whose compiler, lib/typescript.js, runs body,
    JavaScript, as node loads it, or which has none, as TypeScript 7 has none, when body is None."""
    folder = package / "node_modules" / "typescript"
    (folder / "lib").mkdir(parents=True)
    (folder / "package.json").write_text(json.dumps({"name": "typescript", "version": version}))
    if body is not None:
        (folder / "lib" / "typescript.js").write_text(body)


def fake_typescript_7(package, server, version="7.1.0-beta"):
    """Installs in package's node_modules a TypeScript 7 of version, a typescript package without lib/typescript.js, and
    beside it the package of its language server for this machine, whose tsc runs server, lines of sh."""
    fake_typescript(package, None, version)
    fake(package / "node_modules" / whooper.TYPESCRIPT_SERVER_PACKAGE / "lib" / "tsc", server)


def typescript_7_from_bun(folder):
    """Installs in folder's node_modules the newest TypeScript 7 in bun's cache, where bun leaves each package it installs,
    as bun would: a copy of the typescript package and, beside it, a link to the package of its language server for this
    machine, where node looks for it from the copy. Returns whether bun's cache held both."""
    cache = Path.home() / ".bun" / "install" / "cache"
    for server in sorted(cache.glob(f"{whooper.TYPESCRIPT_SERVER_PACKAGE}@7.*"), reverse=True):
        typescript = cache / f"typescript@{server.name.partition('@')[2]}"
        if (server / "lib" / "tsc").is_file() and (typescript / "package.json").is_file():
            shutil.copytree(typescript, folder / "node_modules" / "typescript", symlinks=True)
            (folder / "node_modules" / whooper.TYPESCRIPT_SERVER_PACKAGE).parent.mkdir()
            (folder / "node_modules" / whooper.TYPESCRIPT_SERVER_PACKAGE).symlink_to(server)
            return True
    return False


def block_once(pid):
    """JavaScript that writes its process ID to the file pid and then waits for ever."""
    return (f'require("fs").writeFileSync({json.dumps(str(pid))}, String(process.pid));\n'
            "Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);\n")


def shop_connections(found_by):
    """The connections.json of SHOP's PR 12, with its Go links found by found_by, gopls or name_search. gopls
    spans Total from its comment to its closing brace, and name search to the line before Discount's comment,
    a removed blank line, so it counts one more of Total's changed lines. Discount is where it was at the merge
    base, and its caller is the Checkout of the merge base. Report, Total's first caller, comes before main,
    summarize's, as each changed declaration's first caller comes before any one's second."""
    def node(path, name, line, change, lines_changed):
        return {"id": f"{path}:{name}", "name": name, "kind": "func", "path": path, "line": line, "change": change,
                "lines_changed": lines_changed}

    def link(caller, called, by=found_by):
        return {"from": caller, "to": called, "kind": "calls", "found_by": by}

    return {
        "nodes": [node("prices/prices.go", "Total", 4, "changed", 6 if found_by == "gopls" else 7),
                  node("prices/prices.go", "Discount", 13, "removed", 4),
                  node("shop/shop.go", "Checkout", 6, "changed", 2),
                  node("tools/report.py", "summarize", 1, "changed", 2),
                  node("shop/report.go", "Report", 10, "unchanged", 0),
                  node("tools/cli.py", "main", 4, "unchanged", 0)],
        "edges": [link("shop/report.go:Report", "prices/prices.go:Total"),
                  link("shop/shop.go:Checkout", "prices/prices.go:Total"),
                  link("shop/shop.go:Checkout", "prices/prices.go:Discount"),
                  link("tools/cli.py:main", "tools/report.py:summarize", "name_search")],
        "limits": [ONE_LEVEL, GOPLS_MISSES if found_by == "gopls" else NO_GOPLS, NAME_SEARCH, PATTERNS, REMOVED],
    }


def commit_pr(origin, env, base, head):
    """Makes origin a repo whose branch main holds base's files, by path, and whose branch pr-12 changes them to
    head's, and returns the merge base and the head."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=origin, env=env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def write(files):
        for path in set(base) | set(head):
            (origin / path).unlink(missing_ok=True)
        for path, content in files.items():
            (origin / path).parent.mkdir(parents=True, exist_ok=True)
            (origin / path).write_text(content)
        git("add", "--all")

    origin.mkdir()
    git("init", "--quiet", "--initial-branch=main")
    write(base)
    git("commit", "--quiet", "-m", "base")
    git("checkout", "--quiet", "-b", "pr-12")
    write(head)
    git("commit", "--quiet", "-m", "the PR")
    return git("rev-parse", "main"), git("rev-parse", "HEAD")


def without_gopls(env, gobin):
    """env with no gopls Whooper can find: none of PATH's folders holds one, and go install puts tools in gobin."""
    path = os.pathsep.join(folder for folder in env["PATH"].split(os.pathsep)
                           if not os.access(Path(folder) / "gopls", os.X_OK))
    return {**env, "PATH": path, "GOBIN": str(gobin)}


def without_go(env):
    """env with no go command on PATH."""
    return {**env, "PATH": os.pathsep.join(folder for folder in env["PATH"].split(os.pathsep)
                                           if not os.access(Path(folder) / "go", os.X_OK))}


@contextmanager
def limit_once_the_test_runs(pid, command=("go", "test")):
    """Has each command that a step waits for with a time limit, as run_command does, whose program and first arguments
    are command, go test unless given, run out of time as soon as the test it runs has written its process ID to the
    file pid, however long the command takes to build or start it, rather than when the seconds pass: Popen.wait with a
    timeout waits for the file, or for the command to end, and then times out at once. Yields, for each such command, the
    seconds the step gave it and when they ran out, by monotonic."""
    wait, waits = subprocess.Popen.wait, []

    def wait_for_the_test(process, timeout=None):
        if timeout is not None and [Path(process.args[0]).name, *process.args[1:len(command)]] == list(command):
            while process.poll() is None and not (pid.exists() and pid.read_text()):
                sleep(0.01)
            waits.append((timeout, monotonic()))
            timeout = 0
        return wait(process, timeout)

    with mock.patch.object(subprocess.Popen, "wait", wait_for_the_test):
        yield waits


class RunDirTest(unittest.TestCase):
    """A step of whooper-review, run on a throwaway origin's PR and a run directory with its head and base
    worktrees, with HOME and TMPDIR in a throwaway folder, so git reads only the config a test writes there, and go
    and gopls keep their caches and temporary files there, but for the build cache the tests share."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        home = self.tmp / "home"
        home.mkdir()
        # git config that changes what git grep prints, which name search must read all the same.
        (home / ".gitconfig").write_text("[grep]\n\tcolumn = true\n\tpatternType = perl\n[color]\n\tui = always\n")
        # Temporary files go in the throwaway folder too, as whooper-review puts a step's in the run directory, so
        # those of a go the step kills at its time limit go with the folder.
        (self.tmp / "tmp").mkdir()
        self.env = {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Sam", "GIT_AUTHOR_EMAIL": "sam@example.com",
                    "GIT_COMMITTER_NAME": "Sam", "GIT_COMMITTER_EMAIL": "sam@example.com", "TMPDIR": str(self.tmp / "tmp")}

    def run_dir(self, base, head):
        """A review run's directory for the PR that changes base's files, by path, to head's, as whooper-review
        leaves it when the run ends: run.json, diff.patch, and the worktrees head and base."""
        merge_base, head_sha = commit_pr(self.tmp / "origin", self.env, base, head)
        run = self.tmp / "run"
        for tree, commit in [("head", head_sha), ("base", merge_base)]:
            subprocess.run(["git", "worktree", "add", "--quiet", "--detach", str(run / tree), commit],
                           cwd=self.tmp / "origin", env=self.env, check=True)
        (run / "run.json").write_text(json.dumps({"head_sha": head_sha, "merge_base": merge_base}))
        (run / "diff.patch").write_text(subprocess.run(["git", *whooper.PLAIN_DIFF, merge_base, "HEAD"],
                                                       cwd=run / "head", env=self.env, check=True, capture_output=True,
                                                       text=True).stdout)
        return run


class ConnectionsTest(RunDirTest):
    """connections, the step whooper-review runs as soon as a review run ends."""

    @classmethod
    def setUpClass(cls):
        # Found in the user's own environment, before a test moves HOME.
        cls.gopls, _ = whooper.find_go_tool("gopls")

    def connections(self, run, env, **options):
        """What connections writes for run in the environment env, given options such as seconds."""
        with mock.patch.dict(os.environ, env, clear=True):
            whooper.connections(run, **options)
        return json.loads((run / "connections.json").read_text())

    def test_go_files_link_by_gopls_and_other_files_by_name_search(self):
        if not self.gopls:
            self.skipTest("gopls is not installed; go install golang.org/x/tools/gopls@latest to run this test")
        run = self.run_dir(SHOP, SHOP_PR)
        # The review run left a probe's test in each worktree that calls the code it probes, and lines it added
        # to report.go, which the diff does not change. gopls reads neither, so Report is where report.go has it.
        (run / "head" / "shop" / "probe_test.go").write_text(
            'package shop\n\nimport "example.com/shop/prices"\n\nfunc Probe() int { return prices.Total(nil) }\n')
        (run / "base" / "shop" / "probe_test.go").write_text(
            'package shop\n\nimport "example.com/shop/prices"\n\nfunc Probe() int { return prices.Discount(1) }\n')
        report = run / "head" / "shop" / "report.go"
        report.write_text("// probe\n\n\n" + report.read_text())
        env = {**self.env, "PATH": f"{Path(self.gopls).parent}{os.pathsep}{self.env['PATH']}"}
        self.assertEqual(self.connections(run, env), shop_connections("gopls"))

    def test_without_gopls_go_files_link_by_name_search_too(self):
        run = self.run_dir(SHOP, SHOP_PR)
        (self.tmp / "gobin").mkdir()
        self.assertEqual(self.connections(run, without_gopls(self.env, self.tmp / "gobin")),
                         shop_connections("name_search"))

    def test_moved_code_is_removed_and_added_and_code_the_diff_leaves_alone_shows_where_it_is_in_the_head(self):
        (self.tmp / "gobin").mkdir()
        # move goes from a.py to b.py, and x, which calls it, moves down 2 lines as w above it grows.
        base = {"a.py": "def move():\n    return 2\n\n\ndef keep():\n    return 1\n",
                "c.py": "def w():\n    return 1\n\n\ndef x():\n    return move()\n"}
        head = {"a.py": "def keep():\n    return 1\n", "b.py": "def move():\n    return 2\n",
                "c.py": "def w():\n    total = 1\n    total += 1\n    return total\n\n\ndef x():\n    return move()\n"}
        graph = self.connections(self.run_dir(base, head), without_gopls(self.env, self.tmp / "gobin"))
        self.assertEqual(graph, {
            "nodes": [
                {"id": "a.py:move", "name": "move", "kind": "func", "path": "a.py", "line": 1, "change": "removed",
                 "lines_changed": 4},
                {"id": "b.py:move", "name": "move", "kind": "func", "path": "b.py", "line": 1, "change": "added",
                 "lines_changed": 2},
                {"id": "c.py:w", "name": "w", "kind": "func", "path": "c.py", "line": 1, "change": "changed",
                 "lines_changed": 4},
                # Found at the merge base, where it is on line 5, calling the removed move.
                {"id": "c.py:x", "name": "x", "kind": "func", "path": "c.py", "line": 7, "change": "unchanged",
                 "lines_changed": 0},
            ],
            "edges": [{"from": "c.py:x", "to": "a.py:move", "kind": "calls", "found_by": "name_search"},
                      {"from": "c.py:x", "to": "b.py:move", "kind": "calls", "found_by": "name_search"}],
            "limits": [ONE_LEVEL, NAME_SEARCH, PATTERNS, REMOVED],
        })

    def test_patterns_find_each_languages_definitions(self):
        definitions = whooper.definitions
        go = ("package p\n\n// A counts.\nvar A = 1\n\nconst (\n\tB = iota\n\tC\n)\n\ntype (\n\tD int\n)\n\n"
              "func (l *List[T]) Push(v T) {\n\tvar local int\n}\n\nfunc E() {}\n")
        self.assertEqual([(d["name"], d["kind"], d["line"], d["start"], d["end"]) for d in definitions(go, "go")], [
            ("A", "var", 4, 3, 6), ("B", "const", 7, 7, 7), ("C", "const", 8, 8, 11), ("D", "type", 12, 12, 14),
            ("(*List[T]).Push", "method", 15, 15, 18), ("E", "func", 19, 19, 20)])
        python = ("LIMIT = 3\n\n\nclass Cart:\n    @property\n    def total(self):\n        def inner():\n"
                  "            pass\n\n\nasync def load():\n    else_ = 1\nelse: x = 2\n")
        self.assertEqual([(d["name"], d["kind"], d["line"], d["start"]) for d in definitions(python, "python")], [
            ("LIMIT", "const", 1, 1), ("Cart", "type", 4, 4), ("Cart.total", "method", 6, 5), ("inner", "func", 7, 7),
            ("load", "func", 11, 11)])
        script = ("export const LIMIT = 3\nlet count = 0\n/** Totals. */\nexport async function total() {}\n"
                  "export const Row = ({a}) => a\nconst handler = async (e) => {}\nexport default class Cart {\n"
                  "  add(item) {\n  }\n}\nexport interface Props {}\ntype Id = string\n")
        self.assertEqual([(d["name"], d["kind"], d["line"], d["start"]) for d in definitions(script, "javascript")], [
            ("LIMIT", "const", 1, 1), ("count", "var", 2, 2), ("total", "func", 4, 3), ("Row", "func", 5, 5),
            ("handler", "func", 6, 6), ("Cart", "type", 7, 7), ("Props", "type", 11, 11), ("Id", "type", 12, 12)])

    def test_the_graph_holds_at_most_40_nodes_the_changed_code_first_then_each_ones_callers_in_turn(self):
        (self.tmp / "gobin").mkdir()
        env = without_gopls(self.env, self.tmp / "gobin")
        # a has 50 callers and b 2. After a and b come a's first caller, b's first, a's second, b's second, and
        # then a's next ones, up to 40 nodes. Name search stops at a's 40th caller, so a has more than it lists.
        callers = "".join(f"def use_a_{i}():\n    return a()\n\n\n" for i in range(50))
        callers += "def use_b_0():\n    return b()\n\n\ndef use_b_1():\n    return b()\n"
        base = {"lib.py": "def a():\n    return 1\n\n\ndef b():\n    return 2\n", "users.py": callers}
        head = {**base, "lib.py": "def a():\n    return 10\n\n\ndef b():\n    return 20\n"}
        graph = self.connections(self.run_dir(base, head), env)
        ids = [node["id"] for node in graph["nodes"]]
        self.assertEqual(ids[:6], ["lib.py:a", "lib.py:b", "users.py:use_a_0", "users.py:use_b_0", "users.py:use_a_1",
                                   "users.py:use_b_1"])
        self.assertEqual(ids[6:], [f"users.py:use_a_{i}" for i in range(2, 36)])
        self.assertTrue(all(edge["from"] in ids and edge["to"] in ids for edge in graph["edges"]))
        self.assertEqual(len(graph["edges"]), 38)
        self.assertIn("Whooper shows 40 of the 44 or more declarations it found, the changed code first, and leaves"
                      " out the rest and their links.", graph["limits"])

    def test_more_than_40_changed_declarations_show_the_first_40_without_callers(self):
        (self.tmp / "gobin").mkdir()
        head = {"lib.py": "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(45)),
                "users.py": "def use():\n    return f0()\n"}
        graph = self.connections(self.run_dir({"users.py": head["users.py"]}, head),
                                 without_gopls(self.env, self.tmp / "gobin"))
        self.assertEqual([node["id"] for node in graph["nodes"]], [f"lib.py:f{i}" for i in range(40)])
        self.assertEqual({node["change"] for node in graph["nodes"]}, {"added"})
        self.assertEqual(graph["edges"], [])
        self.assertIn("Whooper shows 40 of the 46 declarations it found, the changed code first, and leaves out the"
                      " rest and their links.", graph["limits"])

    def test_the_step_stops_at_its_time_limit_and_keeps_what_it_found(self):
        # A gopls that never answers. Once it reads the step's first request, when the step has started it and waits
        # for its answer, it writes its process ID and sends the step SIGALRM, as the step's timer would at its limit:
        # the limit finds the step waiting for gopls however slow the machine is, and the timer itself never runs
        # out. report.py comes first in the diff, so the step has read it by then.
        fakes = self.tmp / "fakes"
        fake(fakes / "gopls", f"read -r request\necho $$ > '{self.tmp}/gopls.pid'\nkill -ALRM $PPID\nexec sleep 30")
        base = {"a/report.py": "def summarize(rows):\n    return len(rows)\n",
                "b/total.go": "package b\n\nfunc Total() int {\n\treturn 1\n}\n"}
        head = {"a/report.py": "def summarize(rows):\n    return 0\n",
                "b/total.go": "package b\n\nfunc Total() int {\n\treturn 2\n}\n"}
        run = self.run_dir(base, head)
        started = monotonic()
        with mock.patch("signal.setitimer", wraps=signal.setitimer) as timer:
            graph = self.connections(run, {**self.env, "PATH": f"{fakes}{os.pathsep}{self.env['PATH']}"}, seconds=60)
        self.assertLess(monotonic() - started, 10)
        # The step set its timer to the limit, and stopped it as it ended.
        self.assertEqual(timer.call_args_list, [mock.call(signal.ITIMER_REAL, 60), mock.call(signal.ITIMER_REAL, 0)])
        self.assertEqual(graph, {
            "nodes": [{"id": "a/report.py:summarize", "name": "summarize", "kind": "func", "path": "a/report.py",
                       "line": 1, "change": "changed", "lines_changed": 2}],
            "edges": [],
            "limits": [ONE_LEVEL, PATTERNS, "Whooper stopped looking at its 60-second limit, so changed code and"
                       " callers may be missing."],
        })
        # The step stopped the gopls it started.
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.tmp / "gopls.pid").read_text()), 0)

    def need_typescript(self):
        if not shutil.which("node"):
            self.skipTest("node is not on PATH, so no TypeScript can run")
        if not (PAGE_MODULES / "typescript" / "lib" / "typescript.js").is_file():
            self.skipTest("TypeScript is not installed offline; npm ci in page/ installs it")

    def install_typescript_7(self, package):
        """Installs TypeScript 7 in package's node_modules, as the repo's setup script would: a link to the typescript
        package of a folder that holds TypeScript 7 from bun's cache (typescript_7_from_bun), made once for the class. The
        test skips when bun's cache has none."""
        cls = type(self)
        if not hasattr(cls, "typescript_7"):
            folder = Path(tempfile.mkdtemp())
            cls.addClassCleanup(shutil.rmtree, folder, ignore_errors=True)
            cls.typescript_7 = folder if typescript_7_from_bun(folder) else None
        if not cls.typescript_7:
            self.skipTest(f"TypeScript 7 is not installed offline: bun's cache, ~/.bun/install/cache, has no"
                          f" {whooper.TYPESCRIPT_SERVER_PACKAGE}@7; bun add typescript@7 in any folder puts one there")
        (package / "node_modules").mkdir(parents=True, exist_ok=True)
        (package / "node_modules" / "typescript").symlink_to(cls.typescript_7 / "node_modules" / "typescript")

    def test_typescript_5_and_6_link_through_ts_callers_to_the_code_that_calls_them(self):
        self.need_typescript()
        run = self.run_dir(TS_SHOP, TS_SHOP_PR)
        install_typescript(run / "head" / "shop")
        leave_a_probe_and_an_edit(run / "head")
        with mock.patch("whooper.run_command", wraps=whooper.run_command) as ran:
            graph = self.connections(run, self.env)
        # page/'s TypeScript 5.9.3 has lib/typescript.js, so node runs lib/ts-callers.mjs with it, at the head and at the
        # merge base, whose checkout has no TypeScript installed and reads with the head's.
        self.assertEqual({tuple(call.args[0][:2]) for call in ran.call_args_list}, {("node", str(whooper.TS_CALLERS))})
        self.assertEqual(graph, ts_shop_by_typescript())

    def test_typescript_7_links_through_its_language_server_to_the_code_that_calls_them(self):
        # TS_SHOP's PR with type acquisition on, with which TypeScript 7's language server would download type declarations
        # for the package's dependencies with npm, here a fake that notes each time it runs.
        acquiring = {"shop/tsconfig.json": TS_SHOP["shop/tsconfig.json"].replace(
            ', "include"', ', "typeAcquisition": {"enable": true}, "include"')}
        run = self.run_dir({**TS_SHOP, **acquiring}, {**TS_SHOP_PR, **acquiring})
        self.install_typescript_7(run / "head" / "shop")
        leave_a_probe_and_an_edit(run / "head")
        fake(self.tmp / "fakes" / "npm", f'echo "$*" >> "{self.tmp}/npm.log"\nexit 1')
        env = {**self.env, "PATH": f"{self.tmp / 'fakes'}{os.pathsep}{self.env['PATH']}"}
        with mock.patch("whooper.run_command", wraps=whooper.run_command) as ran:
            graph = self.connections(run, env)
        # TypeScript 7 has no lib/typescript.js, so its language server answers instead of lib/ts-callers.mjs, at the head
        # and at the merge base, and finds the same links. It downloads nothing, and the step stops it as it ends.
        self.assertEqual(ran.call_args_list, [])
        self.assertEqual(graph, ts_shop_by_typescript())
        self.assertFalse((self.tmp / "npm.log").exists())
        self.assertEqual(subprocess.run(["pgrep", "-f", type(self).typescript_7.name]).returncode, 1)

    def test_typescript_7_reads_jsx_and_finds_the_callers_of_a_default_export_without_a_name(self):
        run = self.run_dir(JSX, JSX_PR)
        self.install_typescript_7(run / "head" / "web")

        def link(caller, called):
            return {"from": caller, "to": called, "kind": "calls", "found_by": "typescript"}

        # A JSX tag that renders a component calls it.
        self.assertEqual(self.connections(run, self.env), {
            "nodes": [ts_node("web/src/rows.tsx", "Row", "func", 1, "changed", 2),
                      ts_node("web/src/rows.tsx", "default", "func", 9, "changed", 2),
                      ts_node("web/src/page.tsx", "Single", "func", 5),
                      ts_node("web/src/page.tsx", "Page", "func", 3),
                      ts_node("web/src/rows.tsx", "Table", "func", 5)],
            "edges": [link("web/src/page.tsx:Single", "web/src/rows.tsx:Row"),
                      link("web/src/rows.tsx:Table", "web/src/rows.tsx:Row"),
                      link("web/src/page.tsx:Page", "web/src/rows.tsx:default")],
            "limits": [ONE_LEVEL, TYPESCRIPT_MISSES],
        })

    def test_a_package_without_typescript_links_by_name_search_and_says_so(self):
        self.assertEqual(self.connections(self.run_dir(TS_SHOP, TS_SHOP_PR), self.env),
                         ts_shop_by_name_search(NO_TYPESCRIPT))

    def test_typescript_7_without_its_language_server_links_by_name_search_and_says_why(self):
        run = self.run_dir(TS_SHOP, TS_SHOP_PR)
        fake_typescript(run / "head" / "shop", None, "7.1.0-beta")
        self.assertEqual(self.connections(run, self.env), ts_shop_by_name_search(
            f"Whooper found no {whooper.TYPESCRIPT_SERVER_PACKAGE}, the package of TypeScript 7's language server, beside"
            " its TypeScript"))

    def test_a_typescript_7_server_that_fails_to_start_links_by_name_search_and_says_why(self):
        run = self.run_dir(TS_SHOP, TS_SHOP_PR)
        fake_typescript_7(run / "head" / "shop", "exit 3")
        self.assertEqual(self.connections(run, self.env), ts_shop_by_name_search(
            "TypeScript failed in the head's checkout (tsc --lsp exited with code 3)"))

    def test_a_typescript_7_server_that_never_answers_stops_at_typescripts_share_and_name_search_finds_the_links(self):
        run = self.run_dir(TS_SHOP, TS_SHOP_PR)
        # A language server that writes its process ID and then never answers: the limit finds it waiting, however slow
        # the machine is.
        pid = self.tmp / "server.pid"
        fake_typescript_7(run / "head" / "shop", f"echo $$ > '{pid}'\nexec sleep 60")
        with limit_once_the_test_runs(pid, ("tsc", "--lsp")) as limited:
            graph = self.connections(run, self.env, seconds=60)
        # The head's server and the merge base's, which runs the head's TypeScript, each had at most three quarters of the
        # step's 60 seconds.
        self.assertEqual(len(limited), 2)
        self.assertTrue(all(0 < left <= 45 for left, _ in limited), limited)
        self.assertEqual(graph, ts_shop_by_name_search(
            "TypeScript's 45-second share of the step's 1-minute limit ran out"))
        # The step stopped the servers it started.
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid.read_text()), 0)

    def check_moved_code(self, install):
        """Checks connections.json for a PR that moves a function, with TypeScript installed in the head's checkout by
        install, a function of the package's folder."""
        # discount moves from prices.ts to deals.ts, so checkout.ts imports it from there, and checkout, which calls it
        # and does not change, moves down a line. unrelated.ts has a discount of its own. The package's tsconfig.json
        # only references the project of its code, as a solution does.
        discount = "/** Takes a tenth off. */\nexport function discount(amount: number): number {\n  return amount - amount / 10;\n}\n"
        base = {**TS_SHOP, "shop/tsconfig.json": '{"files": [], "references": [{"path": "./tsconfig.app.json"}]}\n',
                "shop/tsconfig.app.json": TS_SHOP["shop/tsconfig.json"],
                "shop/src/prices.ts": TS_SHOP["shop/src/prices.ts"] + "\n" + discount,
                "shop/src/checkout.ts": ('import { Cart, discount, total } from "./prices";\n\n'
                                         "export function checkout(cart: number[]): number {\n  const c = new Cart();\n"
                                         "  c.add(1);\n  return discount(total(cart));\n}\n"),
                "shop/src/unrelated.ts": (TS_SHOP["shop/src/unrelated.ts"] + "\nfunction discount(n: number): number {\n"
                                          "  return n;\n}\n\nexport const useDiscount = () => discount(2);\n")}
        head = {**base, "shop/src/prices.ts": TS_SHOP["shop/src/prices.ts"], "shop/src/deals.ts": discount,
                "shop/src/checkout.ts": base["shop/src/checkout.ts"].replace(
                    'import { Cart, discount, total } from "./prices";',
                    'import { discount } from "./deals";\nimport { Cart, total } from "./prices";')}
        run = self.run_dir(base, head)
        install(run / "head" / "shop")
        self.assertEqual(self.connections(run, self.env), {
            "nodes": [ts_node("shop/src/deals.ts", "discount", "func", 2, "added", 4),
                      ts_node("shop/src/prices.ts", "discount", "func", 15, "removed", 4),
                      # Found at the merge base, where it is on line 3, calling the removed discount.
                      ts_node("shop/src/checkout.ts", "checkout", "func", 4)],
            "edges": [{"from": "shop/src/checkout.ts:checkout", "to": "shop/src/deals.ts:discount", "kind": "calls",
                       "found_by": "typescript"},
                      {"from": "shop/src/checkout.ts:checkout", "to": "shop/src/prices.ts:discount", "kind": "calls",
                       "found_by": "typescript"}],
            "limits": [ONE_LEVEL, TYPESCRIPT_MISSES, REMOVED],
        })

    def test_moved_typescript_code_shows_the_callers_typescript_finds_at_each_commit(self):
        self.need_typescript()
        self.check_moved_code(install_typescript)

    def test_moved_code_shows_the_callers_typescript_7_finds_at_each_commit(self):
        self.check_moved_code(self.install_typescript_7)

    def test_typescript_stops_at_its_share_of_the_time_limit_and_name_search_finds_the_rest(self):
        self.need_typescript()
        run = self.run_dir(TS_SHOP, TS_SHOP_PR)
        # A TypeScript that lists declarations, and that waits for ever once asked to load a project, after it writes
        # its process ID: the limit finds it waiting, however slow the machine is.
        pid = self.tmp / "typescript.pid"
        fake_typescript(run / "head" / "shop", "\n".join([
            f"const ts = require({json.dumps(str(PAGE_MODULES / 'typescript'))});",
            "module.exports = new Proxy(ts, {get(target, key) {",
            f"  if (key === 'getParsedCommandLineOfConfigFile') {{ {block_once(pid)} }}",
            "  return target[key];",
            "}});"]))
        with limit_once_the_test_runs(pid, ("node", str(whooper.TS_CALLERS))) as limited:
            graph = self.connections(run, self.env, seconds=60)
        # Two runs list the changed file's declarations, at the head and at the merge base, and the third, which finds
        # callers, had at most three quarters of the step's 60 seconds.
        self.assertEqual(len(limited), 3)
        (left, ran_out) = limited[-1]
        self.assertTrue(0 < left <= 45, left)
        self.assertLess(monotonic() - ran_out, 10)
        # TypeScript listed prices.ts's declarations, so Cart.add is a method of its own, and name search found the
        # callers of each.
        def link(caller, called, kind="calls"):
            return {"from": caller, "to": called, "kind": kind, "found_by": "name_search"}

        self.assertEqual(graph, {
            "nodes": [ts_node("shop/src/prices.ts", "total", "func", 2, "changed", 4),
                      ts_node("shop/src/prices.ts", "Cart", "type", 6, "changed", 2),
                      ts_node("shop/src/prices.ts", "Cart.add", "method", 9, "changed", 4),
                      ts_node("shop/src/checkout.ts", "checkout", "func", 3),
                      ts_node("shop/src/unrelated.ts", "total", "func", 2),
                      ts_node("shop/src/unrelated.ts", "useIt", "func", 6)],
            "edges": [link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:total"),
                      link("shop/src/unrelated.ts:total", "shop/src/prices.ts:total"),
                      link("shop/src/unrelated.ts:useIt", "shop/src/prices.ts:total"),
                      link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:Cart", "uses"),
                      link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:Cart.add")],
            "limits": [ONE_LEVEL, "The links of 1 changed file come from name search, since TypeScript's 45-second share"
                       " of the step's 1-minute limit ran out: shop/src/prices.ts.", NAME_SEARCH, PATTERNS, JS_METHODS],
        })
        # The step stopped the TypeScript it started.
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid.read_text()), 0)

    def test_a_stopped_step_leaves_no_typescript_running(self):
        self.need_typescript()
        run = self.run_dir(TS_SHOP, TS_SHOP_PR)
        pid = self.tmp / "typescript.pid"
        fake_typescript(run / "head" / "shop", block_once(pid))
        # whooper-review stops a step with a TERM to the step's process group, which TypeScript, in a group of its
        # own, is not in.
        step = subprocess.Popen([str(ROOT / "lib" / "python"), str(ROOT / "lib" / "whooper.py"), "connections", str(run)],
                                env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        def stop_step():
            if step.poll() is None:
                step.kill()
            step.wait()

        self.addCleanup(stop_step)
        deadline = monotonic() + 30
        while not (pid.exists() and pid.read_text()) and monotonic() < deadline:
            sleep(0.05)
        typescript = int(pid.read_text())

        def stop_typescript():
            with suppress(ProcessLookupError):
                os.kill(typescript, signal.SIGKILL)

        self.addCleanup(stop_typescript)
        os.killpg(step.pid, signal.SIGTERM)
        step.wait(timeout=30)
        deadline = monotonic() + 5
        with self.assertRaises(ProcessLookupError):
            while monotonic() < deadline:
                os.kill(typescript, 0)
                sleep(0.05)

# The limits test_gaps.json gives whenever it measures, in plain words.
AND_LINES = "Go counts a line as run when any part of it ran: in `a && b`, the line counts as run even when b never ran."
CHECKED = "A line that ran is not always checked: a test can run a line without checking what it does."
OWN_TESTS = ("Only the tests of the changed packages ran, so a test in another package that runs these lines does not"
             " count.")
# PR 7 of a Go module: in calc, Clamp gains a cap its test never reaches, Allowed gains `&& quota > 0`, which its
# test never reaches either, and Untested is new, which only app's test runs, through app's Shout. calc's test
# sleeps a second, so its test binary ends after app's, and go test writes the counts each binary gives calc's
# code in that order: Clamp's first line 0, then 1, and Untested's 1, then 0. The generated gen.go, two files the
# build on this Mac leaves out, a new test file, and a Python file change too.
GAPS = {
    "go.mod": "module example.com/gaps\n\ngo 1.21\n",
    "calc/calc.go": ("package calc\n\n// Clamp keeps n at or under 100.\nfunc Clamp(n int) int {\n\treturn n\n}\n\n"
                     "// Allowed says whether a user may act.\nfunc Allowed(admin bool, quota int) bool {\n\treturn admin\n}\n"),
    "calc/calc_test.go": ('package calc\n\nimport (\n\t"testing"\n\t"time"\n)\n\n'
                          "func TestClamp(t *testing.T) {\n\ttime.Sleep(time.Second)\n\tClamp(50)\n}\n\n"
                          "func TestAllowed(t *testing.T) {\n\tAllowed(false, 5)\n}\n"),
    "calc/calc_windows.go": "//go:build windows\n\npackage calc\n\nfunc Windows() int {\n\treturn 1\n}\n",
    "app/app.go": "package app\n\nfunc Shout(s string) string {\n\treturn s\n}\n",
    "app/app_test.go": 'package app\n\nimport "testing"\n\nfunc TestShout(t *testing.T) {\n\tShout("a")\n}\n',
    "gen/gen.go": '// Code generated by stringer; DO NOT EDIT.\n\npackage gen\n\nfunc Name() string {\n\treturn "a"\n}\n',
    "win/win_windows.go": "//go:build windows\n\npackage win\n\nfunc W() int {\n\treturn 1\n}\n",
    "tools/report.py": "def summarize(rows):\n    return len(rows)\n",
}
GAPS_PR = {
    **GAPS,
    "calc/calc.go": ("package calc\n\n// Clamp keeps n at or under 100.\nfunc Clamp(n int) int {\n\tif n > 100 {\n"
                     "\t\t// The cap.\n\t\tn = 100\n\t}\n\n\treturn n\n}\n\n// Allowed says whether a user may act.\n"
                     "func Allowed(admin bool, quota int) bool {\n\treturn admin && quota > 0\n}\n\n"
                     '// Untested adds an exclamation mark.\nfunc Untested(s string) string {\n\tparts := []string{\n'
                     '\t\ts,\n\t\t// Shout.\n\t\t"!",\n\t}\n\treturn parts[0] + parts[1]\n}\n'),
    "calc/calc_windows.go": GAPS["calc/calc_windows.go"].replace("return 1", "return 2"),
    "calc/more_test.go": 'package calc\n\nimport "testing"\n\nfunc TestMore(t *testing.T) {}\n',
    "app/app.go": ('package app\n\nimport "example.com/gaps/calc"\n\nfunc Shout(s string) string {\n'
                   "\treturn calc.Untested(s)\n}\n"),
    "gen/gen.go": GAPS["gen/gen.go"].replace('"a"', '"b"'),
    "win/win_windows.go": GAPS["win/win_windows.go"].replace("return 1", "return 2"),
    "tools/report.py": "def summarize(rows):\n    return len([row for row in rows if row])\n",
}
LEFT_OUT = ("The build on this Mac leaves out 2 changed Go files, so Whooper did not measure them: calc/calc_windows.go,"
            " win/win_windows.go.")
OTHER_LANGUAGE = ("Whooper measures only Go, JavaScript, and TypeScript code so far, so it left out 1 changed file in"
                  " another language: tools/report.py.")


def gaps_tests(allowed):
    """GAPS' calc_test.go with allowed, lines of Go, as the end of TestAllowed."""
    return GAPS["calc/calc_test.go"].replace("\tAllowed(false, 5)\n}", f"\tAllowed(false, 5)\n{allowed}\n}}")


class TestGapsTest(RunDirTest):
    """test_gaps, the step whooper-review runs before a review run starts, on GAPS' PR."""

    def gaps(self, run, env=None, **options):
        """What test_gaps writes for run in the environment env, given options such as seconds, or None when it
        writes nothing."""
        with mock.patch.dict(os.environ, env or self.env, clear=True):
            whooper.test_gaps(run, **options)
        path = run / "test_gaps.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_lists_the_changed_go_lines_no_test_ran_and_counts_those_a_test_ran(self):
        # Of calc.go's changed lines, Go's coverage counts only statements: the if's condition (5), the cap (7),
        # Allowed's return (15), and Untested's two statements, the first of which spans lines 20 to 23, the comment
        # line among them (22) aside, and the second line 25. The other blank and comment lines, the closing braces,
        # and Untested's first line hold none. The cap is the one no test ran. Of app.go's, the return (6) ran.
        self.assertEqual(self.gaps(self.run_dir(GAPS, GAPS_PR)), {
            "measured": True,
            "reason": None,
            "tool": "GOPROXY=off GOTOOLCHAIN=local go test -count=1 -coverpkg=./app,./calc ./app ./calc",
            "lines": [{"path": "calc/calc.go", "line": 7, "status": "not_run"}],
            "summary": {"changed_lines": 8, "run": 7, "not_run": 1},
            "limits": [AND_LINES, CHECKED, OWN_TESTS, LEFT_OUT, OTHER_LANGUAGE],
        })

    def test_a_block_counts_as_run_when_any_test_binary_ran_it(self):
        # go test writes a block once for each test binary that covers its package, in the order they end.
        block = "example.com/gaps/calc/calc.go:25.2,26.1 1 "
        for counts in [("1", "0"), ("0", "1")]:
            with self.subTest(counts=counts):
                profile = "mode: set\n" + "".join(f"{block}{count}\n" for count in counts)
                self.assertEqual(whooper.coverage_counts(profile), {("example.com/gaps/calc/calc.go", 25, 2, 26, 1): 1})

    def test_a_failing_test_still_measures_and_says_so(self):
        gaps = self.gaps(self.run_dir(GAPS, {**GAPS_PR, "calc/calc_test.go": gaps_tests('\tt.Error("fails on purpose")')}))
        self.assertEqual({key: gaps[key] for key in ["measured", "reason", "lines", "summary"]}, {
            "measured": True, "reason": None, "lines": [{"path": "calc/calc.go", "line": 7, "status": "not_run"}],
            "summary": {"changed_lines": 8, "run": 7, "not_run": 1}})
        self.assertEqual(gaps["limits"], [AND_LINES, CHECKED, OWN_TESTS,
                                          "1 test failed: TestAllowed in example.com/gaps/calc. Go recorded the lines it"
                                          " ran before it failed, so they count as run.", LEFT_OUT, OTHER_LANGUAGE])

    def test_nothing_is_measured_when_a_test_binary_crashes_without_recording_what_it_ran(self):
        # A test binary that panics writes no coverage, so the lines only calc's tests ran would read as not run.
        gaps = self.gaps(self.run_dir(GAPS, {**GAPS_PR, "calc/calc_test.go": gaps_tests('\tpanic("fails on purpose")')}))
        self.assertEqual({key: gaps[key] for key in ["measured", "lines", "summary", "limits"]}, {
            "measured": False, "lines": [], "summary": {"changed_lines": 0, "run": 0, "not_run": 0},
            "limits": [OTHER_LANGUAGE]})
        self.assertRegex(gaps["reason"], r"^The tests of example\.com/gaps/calc stopped before Go recorded what they ran,"
                                         r" as when a test panics: panic: fails on purpose")

    def test_nothing_is_measured_when_the_head_does_not_build(self):
        gaps = self.gaps(self.run_dir(GAPS, {**GAPS_PR, "calc/calc.go": GAPS_PR["calc/calc.go"].replace("parts[0] + parts[1]", "parts[0] + 1")}))
        self.assertEqual({key: gaps[key] for key in ["measured", "tool", "lines", "summary", "limits"]}, {
            "measured": False, "tool": "GOPROXY=off GOTOOLCHAIN=local go test -count=1 -coverpkg=./app,./calc ./app ./calc",
            "lines": [], "summary": {"changed_lines": 0, "run": 0, "not_run": 0}, "limits": [OTHER_LANGUAGE]})
        self.assertRegex(gaps["reason"], r"^The Go code at the head does not build: calc/calc\.go:25:\d+: ")

    def test_nothing_is_measured_without_the_go_command(self):
        self.assertEqual(self.gaps(self.run_dir(GAPS, GAPS_PR), without_go(self.env)), {
            "measured": False, "reason": "The go command is not on PATH.", "tool": "go test", "lines": [],
            "summary": {"changed_lines": 0, "run": 0, "not_run": 0}, "limits": [OTHER_LANGUAGE]})

    def test_the_step_stops_at_its_time_limit_and_leaves_no_test_running(self):
        pid = self.tmp / "test.pid"
        wait = f'\tos.WriteFile("{pid}", []byte(strconv.Itoa(os.Getpid())), 0o644)\n\ttime.Sleep(time.Minute)'
        tests = gaps_tests(wait).replace('\t"testing"', '\t"os"\n\t"strconv"\n\t"testing"')
        run = self.run_dir(GAPS, {**GAPS_PR, "calc/calc_test.go": tests})
        # The limit comes once the test binary runs, however long go takes to build it, so it finds the test running.
        with limit_once_the_test_runs(pid) as waits:
            gaps = self.gaps(run, seconds=10)
        [(left, ran_out)] = waits
        # go test had what was left of the step's 10 seconds, and the step ended soon after they ran out.
        self.assertTrue(0 < left <= 10, left)
        self.assertLess(monotonic() - ran_out, 10)
        self.assertEqual({key: gaps[key] for key in ["measured", "reason", "lines"]},
                         {"measured": False, "reason": "The tests ran past Whooper's 10-second limit.", "lines": []})
        # The test binary started, and the step stopped it.
        deadline = monotonic() + 5
        with self.assertRaises(ProcessLookupError):
            while monotonic() < deadline:
                os.kill(int(pid.read_text()), 0)
                sleep(0.1)

    def test_a_pr_that_changes_no_go_code_has_no_test_gaps(self):
        head = {**GAPS, "tools/report.py": GAPS_PR["tools/report.py"], "gen/gen.go": GAPS_PR["gen/gen.go"],
                "calc/more_test.go": GAPS_PR["calc/more_test.go"],
                "calc/calc.go": GAPS["calc/calc.go"].replace("// Clamp keeps", "// Clamp still keeps")}
        self.assertIsNone(self.gaps(self.run_dir(GAPS, head)))


# The limits test_gaps.json gives whenever it measures JavaScript or TypeScript lines, in plain words.
NODE_LINES = ("Vitest and Jest count a line as run when a statement that starts on it ran: in `a && b`, the line counts as"
              " run even when b never ran, and the other lines of a statement that spans several lines do not count.")
# PR 7 of a repository with two Node packages: web's tests run with Vitest, whose test calls clamp with 50 only, so of
# clamp's new lines its cap is the one no test ran; untested.ts, which no test imports, and a page under a folder whose
# name is a glob, [id], are new or changed, and a test file and a declaration file change too. other's package.json names
# no test runner. A Python file changes as well.
WEB = {
    "web/package.json": ('{"name": "web", "private": true, "scripts": {"test": "vitest run"},'
                         ' "devDependencies": {"vitest": "4.1.11", "@vitest/coverage-v8": "4.1.11"}}\n'),
    "web/src/calc.ts": "export function clamp(n: number): number {\n  return n;\n}\n",
    "web/src/calc.test.ts": ('import { expect, test } from "vitest";\nimport { clamp } from "./calc";\n\n'
                             'test("clamp", () => {\n  expect(clamp(50)).toBe(50);\n});\n'),
    "web/src/routes/[id]/page.ts": 'export const title = (id: string) => "Item " + id;\n',
    "other/package.json": '{"name": "other", "private": true}\n',
    "other/index.js": "module.exports = 1;\n",
    "tools/report.py": "def summarize(rows):\n    return len(rows)\n",
}
WEB_PR = {
    **WEB,
    "web/src/calc.ts": ("export function clamp(n: number): number {\n  if (n > 100) {\n    return 100;\n  }\n"
                        "  // Below the cap.\n  return n;\n}\n"),
    "web/src/calc.test.ts": WEB["web/src/calc.test.ts"].replace("toBe(50);\n", "toBe(50);\n  expect(clamp(7)).toBe(7);\n"),
    "web/src/untested.ts": 'export function shout(s: string): string {\n  return s + "!";\n}\n',
    "web/src/routes/[id]/page.ts": 'export const title = (id: string) =>\n  "Item " + id.trim();\n',
    "web/src/types.d.ts": "export type Cap = number;\n",
    "other/index.js": "module.exports = 2;\n",
    "tools/report.py": "def summarize(rows):\n    return len([row for row in rows if row])\n",
}
NO_RUNNER = "Whooper did not measure 1 changed file in other, since its package.json names neither Vitest nor Jest: other/index.js."
WEB_TOOL = ("(cd web && CI=true npm_config_offline=true node_modules/.bin/vitest run --coverage --coverage.reporter=lcov"
            " --coverage.include=src/calc.ts '--coverage.include=src/routes/\\[id\\]/page.ts'"
            " --coverage.include=src/untested.ts)")
# Where npm ci in page/ installs Vitest and its V8 coverage, which the tests run as a PR's own would.
PAGE_MODULES = ROOT / "page" / "node_modules"


def install_vitest(package):
    """Installs Vitest and its V8 coverage in package's node_modules, as the repo's setup script would: links to the ones
    in PAGE_MODULES, in a folder of package's own, so Vitest keeps its cache there."""
    modules = package / "node_modules"
    (modules / ".bin").mkdir(parents=True)
    for name in ["vitest", "@vitest"]:
        (modules / name).symlink_to(PAGE_MODULES / name)
    (modules / ".bin" / "vitest").symlink_to("../vitest/vitest.mjs")


class NodeGapsTest(RunDirTest):
    """test_gaps on JavaScript and TypeScript changes, on WEB's PR and others."""

    def gaps(self, run, env=None, **options):
        with mock.patch.dict(os.environ, env or self.env, clear=True):
            whooper.test_gaps(run, **options)
        path = run / "test_gaps.json"
        return json.loads(path.read_text()) if path.exists() else None

    def need_vitest(self):
        if not shutil.which("node"):
            self.skipTest("node is not on PATH, so no Vitest can run")
        if not ((PAGE_MODULES / ".bin" / "vitest").exists() and (PAGE_MODULES / "@vitest" / "coverage-v8").is_dir()):
            self.skipTest("Vitest and @vitest/coverage-v8 are not installed offline; npm ci in page/ installs both")

    def test_lists_the_changed_lines_no_vitest_test_ran_in_each_package_whose_tests_run_with_it(self):
        self.need_vitest()
        run = self.run_dir(WEB, WEB_PR)
        install_vitest(run / "head" / "web")
        # In calc.ts, the if (2) ran and the cap (3) did not, and the brace (4) and the comment (5) hold no statement.
        # page.ts's two statements and untested.ts's one ran in no test, since no test loads either file. The test and
        # the declaration file are not measured.
        self.assertEqual(self.gaps(run), {
            "measured": True,
            "reason": None,
            "tool": WEB_TOOL,
            "lines": [{"path": path, "line": n, "status": "not_run"} for path, n in [
                ("web/src/calc.ts", 3), ("web/src/routes/[id]/page.ts", 1), ("web/src/routes/[id]/page.ts", 2),
                ("web/src/untested.ts", 2)]],
            "summary": {"changed_lines": 5, "run": 1, "not_run": 4},
            "limits": [NODE_LINES, CHECKED, OWN_TESTS, NO_RUNNER, OTHER_LANGUAGE],
        })
        # Vitest wrote nothing in the worktree but its cache in node_modules.
        status = subprocess.run(["git", "status", "--porcelain", "--ignored", "--untracked-files=all"], cwd=run / "head",
                                env=self.env, capture_output=True, text=True, check=True).stdout
        self.assertEqual({line.split("/node_modules/")[0] for line in status.splitlines()}, {"?? web"})

    def test_a_jest_package_and_go_merge_into_one_file_with_each_runs_command(self):
        # A Go module at the root and a Node package, app, whose tests run with Jest. The fake jest saves its arguments
        # and environment and writes an LCov report that names calc.js by its absolute path, in two records, as a merged
        # report can, and exits 1, as Jest does when a test fails. A fake node stands for the one Jest's script needs.
        base = {"go.mod": GAPS["go.mod"], "calc/calc.go": GAPS["calc/calc.go"], "calc/calc_test.go": GAPS["calc/calc_test.go"],
                "app/package.json": '{"name": "app", "scripts": {"test": "jest --silent"}}\n',
                "app/src/calc.js": "exports.cap = (n) => n;\n"}
        head = {**base, "calc/calc.go": GAPS_PR["calc/calc.go"].split("\n\n// Untested")[0] + "\n",
                "app/src/calc.js": "exports.cap = (n) => {\n  if (n > 9) {\n    return 9;\n  }\n  return n;\n};\n"}
        run = self.run_dir(base, head)
        app = run.resolve() / "head" / "app"
        fake(app / "node_modules" / ".bin" / "jest", "\n".join([
            f'printf "%s\\n" "$PWD" "$CI $npm_config_offline" "$@" > "{self.tmp}/jest-args"',
            'for arg; do case "$arg" in --coverageDirectory=*) out="${arg#*=}" ;; esac; done',
            f'printf "TN:\\nSF:{app}/src/calc.js\\nDA:1,1\\nDA:2,1\\nDA:3,0\\nend_of_record\\n'
            f'SF:{app}/src/calc.js\\nDA:3,0\\nDA:5,2\\nend_of_record\\n" > "$out/lcov.info"',
            "exit 1"]))
        fake(self.tmp / "fakes" / "node", "exit 0")
        gaps = self.gaps(run, {**self.env, "PATH": f"{self.tmp / 'fakes'}{os.pathsep}{self.env['PATH']}"})
        self.assertEqual(gaps["tool"], "GOPROXY=off GOTOOLCHAIN=local go test -count=1 -coverpkg=./calc ./calc; (cd app &&"
                                       " CI=true npm_config_offline=true node_modules/.bin/jest --coverage"
                                       " --coverageReporters=lcov --collectCoverageFrom=src/calc.js)")
        # calc.js's cap (3) and calc.go's cap (7) no test ran, in the diff's order.
        self.assertEqual({key: gaps[key] for key in ["measured", "reason", "lines", "summary"]}, {
            "measured": True, "reason": None,
            "lines": [{"path": "app/src/calc.js", "line": 3, "status": "not_run"},
                      {"path": "calc/calc.go", "line": 7, "status": "not_run"}],
            "summary": {"changed_lines": 7, "run": 5, "not_run": 2}})
        self.assertEqual(gaps["limits"], [AND_LINES, CHECKED, OWN_TESTS, NODE_LINES, "Jest exited with code 1 in app, as it"
                                          " does when a test fails; the lines the tests ran count as run."])
        folder, env, *args = (self.tmp / "jest-args").read_text().splitlines()
        self.assertEqual((Path(folder).resolve(), env), (app, "true true"))
        self.assertEqual(args[:3] + args[4:], ["--coverage", "--coverageReporters=lcov", "--collectCoverageFrom=src/calc.js",
                                               "--no-watchman"])
        self.assertTrue(args[3].startswith("--coverageDirectory="), args[3])

    def test_lcov_counts_give_each_files_lines_with_the_highest_count(self):
        report = ("TN:\nDA:1,5\nSF:src/a b:c.ts\r\nFN:1,f\nDA:1,0\nDA:2,3,abc123\nBRDA:2,0,0,1\nDA:x,1\nDA:4,-1\nend_of_record\n"
                  "SF:src/empty.ts\nend_of_record\nSF:src/a b:c.ts\nDA:1,2\nDA:2,1\nDA:3,0\nend_of_record\nSF:src/last.ts\nDA:7,1")
        self.assertEqual(whooper.lcov_counts(report), {"src/a b:c.ts": {1: 2, 2: 3, 3: 0}, "src/empty.ts": {},
                                                       "src/last.ts": {7: 1}})
        self.assertEqual(whooper.lcov_counts(""), {})

    def test_nothing_is_measured_without_a_runner_installed_or_a_package(self):
        js = {"web/src/a.ts": "export const a = 1;\n"}
        for name, files, why in [
            ("no package.json", {}, "Whooper did not measure 1 changed file, since no package.json is above it:"
                                          " web/src/a.ts."),
            ("no runner", {"web/package.json": '{"scripts": {"test": "node test.js"}}'},
             "Whooper did not measure 1 changed file in web, since its package.json names neither Vitest nor Jest:"
             " web/src/a.ts."),
            ("not installed", {"web/package.json": '{"devDependencies": {"jest": "30.0.0"}}'},
             "Whooper did not measure 1 changed file in web, since its dependencies are not installed: Whooper found no"
             " node_modules/.bin/jest there or in a folder above it, which the repo's setup script would install:"
             " web/src/a.ts."),
            ("unreadable", {"web/package.json": "{"}, "Whooper did not measure 1 changed file in web, since its"
                                                      " package.json cannot be read: "),
        ]:
            with self.subTest(name):
                shutil.rmtree(self.tmp / "run", ignore_errors=True)
                shutil.rmtree(self.tmp / "origin", ignore_errors=True)
                run = self.run_dir({**files, "web/src/a.ts": "export const a = 0;\n"}, {**files, **js})
                gaps = self.gaps(run)
                self.assertEqual({key: gaps[key] for key in ["measured", "tool", "lines", "summary", "limits"]}, {
                    "measured": False, "tool": "", "lines": [], "summary": {"changed_lines": 0, "run": 0, "not_run": 0},
                    "limits": []})
                self.assertTrue(gaps["reason"].startswith(why), gaps["reason"])

    def test_the_tests_of_a_package_stop_at_the_steps_time_limit_and_the_go_lines_stay_measured(self):
        self.need_vitest()
        pid = self.tmp / "test.pid"
        waits = ('import { writeFileSync } from "node:fs";\nimport { test } from "vitest";\n\n'
                 f'test("waits", async () => {{\n  writeFileSync({json.dumps(str(pid))}, String(process.pid));\n'
                 "  await new Promise((resolve) => setTimeout(resolve, 60000));\n}, 120000);\n")
        base = {"go.mod": GAPS["go.mod"], "calc/calc.go": GAPS["calc/calc.go"], "calc/calc_test.go": GAPS["calc/calc_test.go"],
                "web/package.json": WEB["web/package.json"], "web/src/calc.ts": WEB["web/src/calc.ts"],
                "web/src/calc.test.ts": waits}
        run = self.run_dir(base, {**base, "calc/calc.go": GAPS_PR["calc/calc.go"].split("\n\n// Untested")[0] + "\n",
                                  "web/src/calc.ts": WEB_PR["web/src/calc.ts"]})
        install_vitest(run / "head" / "web")
        # The limit comes once the test runs, however long Vitest takes to start, so it finds the test waiting.
        with limit_once_the_test_runs(pid, ("vitest", "run")) as limited:
            gaps = self.gaps(run, seconds=30)
        [(left, ran_out)] = limited
        self.assertTrue(0 < left <= 30, left)
        self.assertLess(monotonic() - ran_out, 10)
        self.assertEqual({key: gaps[key] for key in ["measured", "lines"]},
                         {"measured": True, "lines": [{"path": "calc/calc.go", "line": 7, "status": "not_run"}]})
        self.assertEqual(gaps["limits"][-1], "Whooper did not measure 1 changed file in web, since its tests ran past"
                                             " Whooper's 30-second limit: web/src/calc.ts.")
        # The test's process started, and the step stopped it.
        deadline = monotonic() + 5
        with self.assertRaises(ProcessLookupError):
            while monotonic() < deadline:
                os.kill(int(pid.read_text()), 0)
                sleep(0.1)


# The limits test_gaps.json gives once the mutation step ran, in plain words.
MUTATED = ("Whooper changed each changed Go line that tests ran with gremlins, one small change at a time, such as `>` to"
           " `>=`, and ran the tests of the line's package again: survived when every test still passed, caught when one"
           " failed. gremlins changes only arithmetic, comparisons, increments, and negative signs, so other lines, and"
           " lines only another package's tests run, show neither.")
NO_GREMLINS = ("Whooper found no gremlins on PATH or where go install puts it, go env GOBIN or GOPATH/bin, so Whooper did"
               " not change the changed Go lines on purpose to see whether a test catches the change (mutation testing).")
# PR 5 of a Go module: Clamp gains a cap, whose test checks 50 and 150 but not 100, so gremlins' `>` to `>=` survives
# while its `>` to `<=` is caught; F subtracts where it added and adds 2, which its test catches either way, and F's middle
# line stays as it was between two changed lines. The test sleeps a little, so gremlins gives each change's tests time.
MUT = {
    "go.mod": "module example.com/mut\n\ngo 1.21\n",
    "calc/calc.go": ("package calc\n\n// Clamp keeps n at or under 100.\nfunc Clamp(n int) int {\n\treturn n\n}\n\n"
                     "// F adds and doubles.\nfunc F(n int) int {\n\ta := n + 1\n\tb := a * 2\n\treturn b\n}\n"),
    "calc/calc_test.go": ('package calc\n\nimport (\n\t"testing"\n\t"time"\n)\n\nfunc TestClamp(t *testing.T) {\n'
                          "\ttime.Sleep(300 * time.Millisecond)\n\tif Clamp(50) != 50 || Clamp(150) != 100 {\n"
                          '\t\tt.Fatal("clamp")\n\t}\n}\n\nfunc TestF(t *testing.T) {\n\tif F(1) != 2 {\n\t\tt.Fatal(F(1))\n'
                          "\t}\n}\n"),
}
MUT_PR = {**MUT, "calc/calc.go": ("package calc\n\n// Clamp keeps n at or under 100.\nfunc Clamp(n int) int {\n"
                                  "\tif n > 100 {\n\t\treturn 100\n\t}\n\treturn n\n}\n\n// F adds and doubles.\n"
                                  "func F(n int) int {\n\ta := n - 1\n\tb := a * 2\n\treturn b + 2\n}\n")}


class MutationTest(RunDirTest):
    """mutation, the step whooper-review runs once a review run ends, after the examples step."""

    @classmethod
    def setUpClass(cls):
        # Found in the user's own environment, before a test moves HOME.
        cls.gremlins, _ = whooper.find_go_tool("gremlins")

    def with_gremlins(self):
        if not self.gremlins:
            self.skipTest("gremlins is not installed; go install github.com/go-gremlins/gremlins/cmd/gremlins@v0.6.0")
        return {**self.env, "PATH": f"{Path(self.gremlins).parent}{os.pathsep}{self.env['PATH']}"}

    def mutation(self, run, env, **options):
        with mock.patch.dict(os.environ, env, clear=True):
            whooper.mutation(run, **options)
        return json.loads((run / "test_gaps.json").read_text())

    def test_marks_each_changed_line_tests_ran_survived_or_caught_with_its_change(self):
        env = self.with_gremlins()
        run = self.run_dir(MUT, MUT_PR)
        with mock.patch.dict(os.environ, env, clear=True):
            whooper.test_gaps(run)
        # The review run then left an edit to Clamp and a probe that does not compile in the head's worktree, which the
        # step takes back, so gremlins changes the reviewed commit.
        calc = run / "head" / "calc" / "calc.go"
        calc.write_text(calc.read_text().replace("n > 100", "n >= 100"))
        (run / "head" / "calc" / "probe_test.go").write_text("package calc\n\nfunc broken( {\n")
        merge_base = json.loads((run / "run.json").read_text())["merge_base"]
        self.assertEqual(self.mutation(run, env), {
            "measured": True,
            "reason": None,
            "tool": "GOPROXY=off GOTOOLCHAIN=local go test -count=1 -coverpkg=./calc ./calc; (cd calc && GOPROXY=off"
                    f" GOTOOLCHAIN=local gremlins unleash --diff {merge_base} --workers 2 --timeout-coefficient 10)",
            # F's middle line, which the PR leaves alone, shows nothing.
            "lines": [{"path": "calc/calc.go", "line": 5, "status": "survived", "change": "`>` to `>=`"},
                      {"path": "calc/calc.go", "line": 13, "status": "caught", "change": "`-` to `+`"},
                      {"path": "calc/calc.go", "line": 15, "status": "caught", "change": "`+` to `-`"}],
            "summary": {"changed_lines": 4, "run": 4, "not_run": 0, "survived": 1, "caught": 2},
            "limits": [AND_LINES, CHECKED, OWN_TESTS, MUTATED],
        })
        self.assertIn("n > 100", calc.read_text())
        self.assertFalse((run / "head" / "calc" / "probe_test.go").exists())

    def test_keeps_lines_the_diff_adds_one_change_a_line_and_7_lines_a_file_survived_first(self):
        # A fake gremlins reports a caught change on each of 9 changed lines, a surviving one on a tenth, one on a line
        # the PR leaves alone, one that ran out of time, and one that did not compile.
        base = {"go.mod": MUT["go.mod"], "calc/sum.go": "package calc\n\nfunc Sum(n int) int {\n\treturn n\n}\n",
                "calc/sum_test.go": 'package calc\n\nimport "testing"\n\nfunc TestSum(t *testing.T) {\n\tSum(1)\n}\n'}
        run = self.run_dir(base, {**base, "calc/sum.go": "package calc\n\nfunc Sum(n int) int {\n\ts := n\n"
                                  + "\ts = s + 1\n" * 10 + "\treturn s\n}\n"})
        with mock.patch.dict(os.environ, self.env, clear=True):
            whooper.test_gaps(run)

        def mutant(line, status, kind="ARITHMETIC_BASE"):
            return {"type": kind, "status": status, "line": line, "column": 8}

        report = [mutant(n, "KILLED") for n in range(5, 14)] + [
            mutant(14, "LIVED", "CONDITIONALS_BOUNDARY"), mutant(3, "LIVED"), mutant(4, "TIMED OUT"),
            mutant(15, "NOT VIABLE")]
        (self.tmp / "report.json").write_text(json.dumps({"files": [{"file_name": "sum.go", "mutations": report}]}))
        fake(self.tmp / "fakes" / "gremlins", "\n".join([
            f'printf "%s\\n" "$PWD" "$GOPROXY $GOTOOLCHAIN $GIT_CONFIG_COUNT $GIT_CONFIG_KEY_0=$GIT_CONFIG_VALUE_0'
            f' $GIT_CONFIG_KEY_1=$GIT_CONFIG_VALUE_1 $GIT_CONFIG_KEY_2=$GIT_CONFIG_VALUE_2" "$@" > "{self.tmp}/args"',
            'while [ $# -gt 1 ]; do [ "$1" = --output ] && out="$2"; shift; done',
            f'cp "{self.tmp}/report.json" "$out"']))
        gaps = self.mutation(run, {**self.env, "PATH": f"{self.tmp / 'fakes'}{os.pathsep}{self.env['PATH']}"})
        # The surviving change's operator is not one its kind changes, so it shows by its kind.
        self.assertEqual(gaps["lines"], [
            *({"path": "calc/sum.go", "line": n, "status": "caught", "change": "`+` to `-`"} for n in range(5, 11)),
            {"path": "calc/sum.go", "line": 14, "status": "survived",
             "change": "gremlins's CONDITIONALS_BOUNDARY change at column 8"}])
        self.assertEqual(gaps["summary"], {"changed_lines": 12, "run": 12, "not_run": 0, "survived": 1, "caught": 6})
        self.assertEqual(gaps["limits"][-1], "Whooper shows at most 7 survived or caught lines a file, survived first, so"
                                             " it left out 3 lines of calc/sum.go.")
        folder, settings, *args = (self.tmp / "args").read_text().splitlines()
        merge_base = json.loads((run / "run.json").read_text())["merge_base"]
        self.assertEqual((Path(folder).resolve(), settings), (run.resolve() / "head" / "calc",
                                                             "off local 3 diff.relative=true diff.context=0 color.diff=false"))
        self.assertEqual([args[:3], args[5:]], [["unleash", "--diff", merge_base],
                                                ["--workers", "2", "--timeout-coefficient", "10"]])

    def test_without_gremlins_limits_say_so_and_nothing_else_changes(self):
        run = self.run_dir(MUT, MUT_PR)
        with mock.patch.dict(os.environ, self.env, clear=True):
            whooper.test_gaps(run)
        before = json.loads((run / "test_gaps.json").read_text())
        gaps = self.mutation(run, {**without_gopls(self.env, self.tmp / "gobin"), "PATH": os.pathsep.join(
            folder for folder in self.env["PATH"].split(os.pathsep) if not os.access(Path(folder) / "gremlins", os.X_OK))})
        self.assertEqual(gaps, {**before, "summary": {**before["summary"], "survived": 0, "caught": 0},
                                "limits": [*before["limits"], NO_GREMLINS]})

    def test_the_step_stops_at_its_time_limit_and_leaves_no_test_running(self):
        env = self.with_gremlins()
        pid = self.tmp / "test.pid"
        tests = MUT["calc/calc_test.go"].replace('\t"testing"', '\t"os"\n\t"strconv"\n\t"testing"').replace(
            "\ttime.Sleep(300 * time.Millisecond)\n",
            f'\tos.WriteFile("{pid}", []byte(strconv.Itoa(os.Getpid())), 0o644)\n\ttime.Sleep(time.Minute)\n')
        run = self.run_dir(MUT, {**MUT_PR, "calc/calc_test.go": tests})
        # The test-gaps step would wait for the test too, so the file stands for what it measured.
        (run / "test_gaps.json").write_text(json.dumps({"measured": True, "reason": None, "tool": "go test", "lines": [],
                                                        "summary": {"changed_lines": 4, "run": 4, "not_run": 0},
                                                        "limits": []}))
        # The limit comes once gremlins's coverage run started the test, however long go takes to build it.
        with limit_once_the_test_runs(pid, ("gremlins", "unleash")) as waits:
            gaps = self.mutation(run, env, seconds=20)
        [(left, ran_out)] = waits
        self.assertTrue(0 < left <= 20, left)
        self.assertLess(monotonic() - ran_out, 10)
        self.assertEqual({key: gaps[key] for key in ["lines", "summary"]}, {
            "lines": [], "summary": {"changed_lines": 4, "run": 4, "not_run": 0, "survived": 0, "caught": 0}})
        self.assertEqual(gaps["limits"], ["gremlins ran past Whooper's 20-second limit, so Whooper has no survived or"
                                          " caught lines in calc."])
        deadline = monotonic() + 5
        with self.assertRaises(ProcessLookupError):
            while monotonic() < deadline:
                os.kill(int(pid.read_text()), 0)
                sleep(0.1)


# PR 9 of a Go module: Clamp gains a cap at 100, and Half is new. Token, which the PR leaves alone, names each
# request with its process's ID, so two runs of the same input print different results.
PRICES = {
    "go.mod": "module example.com/shop\n\ngo 1.21\n",
    "prices/prices.go": ('package prices\n\nimport (\n\t"fmt"\n\t"os"\n)\n\n// Clamp keeps n at or under 100.\n'
                         "func Clamp(n int) int {\n\treturn n\n}\n\n// Token names a request.\n"
                         'func Token(s string) string {\n\treturn s + "-" + fmt.Sprint(os.Getpid())\n}\n'),
}
PRICES_PR = {
    **PRICES,
    "prices/prices.go": ('package prices\n\nimport (\n\t"fmt"\n\t"os"\n)\n\n// Clamp keeps n at or under 100.\n'
                         "func Clamp(n int) int {\n\tif n > 100 {\n\t\treturn 100\n\t}\n\treturn n\n}\n\n"
                         '// Token names a request.\nfunc Token(s string) string {\n\treturn s + "-" + fmt.Sprint(os.Getpid())\n}\n\n'
                         "// Half halves n.\nfunc Half(n int) int {\n\treturn n / 2\n}\n"),
}
# The limits examples.json gives whenever Whooper runs the test.
CHOSE = "The review run chose the functions and inputs, so inputs that would show other changes may be missing."
ONCE_AT_HEAD = "Whooper runs each input once at the PR head, so a result that varies from run to run there can read as a change."


def examples_test(*calls):
    """A whooper_examples_test.go for package prices whose TestWhooperExamples prints a line for each of calls, given
    as (function, input, Go expression), as prompts/review.md asks."""
    lines = "".join(f"\tshow({json.dumps(function)}, {json.dumps(text)}, {expression})\n" for function, text, expression in calls)
    return ('package prices\n\nimport (\n\t"encoding/json"\n\t"fmt"\n\t"testing"\n)\n\n'
            "func TestWhooperExamples(t *testing.T) {\n\tshow := func(function, input string, result any) {\n"
            '\t\tline, _ := json.Marshal(map[string]any{"function": function, "input": input, "result": result})\n'
            "\t\tfmt.Println(string(line))\n\t}\n" + lines + "}\n")


def examples_request(functions, inputs):
    """The examples.json a review run writes for package prices: functions as (name, line), inputs as (function, input)."""
    return {"package": "prices", "functions": [{"name": name, "path": "prices/prices.go", "line": line} for name, line in functions],
            "inputs": [{"function": function, "input": text} for function, text in inputs]}


def example_row(function, text, before, after, status, label, note=None):
    return {"function": function, "input": text, "before": before, "after": after, "status": status, "label": label,
            "note": note}


class ExamplesTest(RunDirTest):
    """examples, the step whooper-review runs once a review run ends, on PRICES' PR."""

    def examples(self, run, request, test, env=None, **options):
        """What examples writes for run, given options such as seconds, after the review run wrote request, JSON
        unless it is a string, as examples.json, which whooper-review moved to examples.request.json, and test as
        whooper_examples_test.go; None leaves either out. None when it writes nothing."""
        if request is not None:
            (run / "examples.request.json").write_text(request if isinstance(request, str) else json.dumps(request))
        if test is not None:
            (run / "whooper_examples_test.go").write_text(test)
        with mock.patch.dict(os.environ, env or self.env, clear=True):
            whooper.examples(run, **options)
        path = run / "examples.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_compares_each_inputs_result_at_the_head_with_two_runs_at_the_merge_base(self):
        run = self.run_dir(PRICES, PRICES_PR)
        # The review run left a change to Clamp in the head's worktree and a probe that does not compile in the
        # merge base's. Neither reaches the runs.
        prices = run / "head" / "prices" / "prices.go"
        prices.write_text(prices.read_text().replace("return 100", "return 99"))
        (run / "base" / "prices" / "probe_test.go").write_text("package prices\n\nfunc broken( {\n")
        # The run gave Token's line wrong, and its test prints nothing for Clamp(7).
        result = self.examples(run, examples_request(
            [("Clamp", 9), ("Token", 2)],
            [("Clamp", "Clamp(50)"), ("Clamp", "Clamp(101)"), ("Token", 'Token("a")'), ("Clamp", "Clamp(7)")]),
            examples_test(("Clamp", "Clamp(50)", "Clamp(50)"), ("Clamp", "Clamp(101)", "Clamp(101)"),
                          ("Token", 'Token("a")', 'Token("a")')))
        self.assertEqual(result["functions"], [{"name": "Clamp", "path": "prices/prices.go", "line": 9},
                                               {"name": "Token", "path": "prices/prices.go", "line": 17}])
        same, different, flaky, missing = result["rows"]
        self.assertEqual(same, example_row("Clamp", "Clamp(50)", "50", "50", "same", "ran_base_and_head"))
        self.assertEqual(different, example_row("Clamp", "Clamp(101)", "101", "100", "different", "ran_base_and_head"))
        self.assertEqual({key: flaky[key] for key in ["function", "input", "before", "status", "label"]},
                         {"function": "Token", "input": 'Token("a")', "before": None, "status": "flaky",
                          "label": "not_compared"})
        self.assertRegex(flaky["after"], r'^"a-\d+"$')
        self.assertRegex(flaky["note"], r'^The two runs at the merge base printed "a-\d+" and "a-\d+"\.$')
        self.assertEqual(missing, example_row("Clamp", "Clamp(7)", None, None, "error", "not_compared",
                                              "The test printed no result for this input at the PR head."))
        self.assertEqual(result["limits"], [CHOSE, ONCE_AT_HEAD])
        # The worktrees are as the review run left them.
        self.assertIn("return 99", prices.read_text())
        self.assertFalse((run / "head" / "prices" / "whooper_examples_test.go").exists())

    def test_a_function_new_in_the_pr_runs_at_the_head_only(self):
        run = self.run_dir(PRICES, PRICES_PR)
        result = self.examples(run, examples_request([("Half", 22), ("Clamp", 9)], [("Half", "Half(4)"), ("Clamp", "Clamp(101)")]),
                               examples_test(("Half", "Half(4)", "Half(4)"), ("Clamp", "Clamp(101)", "Clamp(101)")))
        half, clamp = result["rows"]
        self.assertEqual(half, example_row("Half", "Half(4)", None, "2", "different", "ran_head_only",
                                           "New in this PR: the merge base has no Half."))
        # The test does not compile at the merge base, which has no Half, so Clamp is not compared either.
        self.assertEqual({key: clamp[key] for key in ["before", "after", "status", "label"]},
                         {"before": None, "after": "100", "status": "error", "label": "not_compared"})
        self.assertRegex(clamp["note"], r"^The test does not compile at the merge base: .*undefined: Half")

    def test_a_function_the_merge_base_defines_in_a_file_whose_name_git_quotes_runs_there_too(self):
        # git quotes a path with a character outside ASCII, as prices/größe.go, unless it lists paths ended by NUL.
        base = {"go.mod": PRICES["go.mod"], "prices/größe.go": PRICES["prices/prices.go"]}
        head = {"go.mod": PRICES["go.mod"], "prices/größe.go": PRICES_PR["prices/prices.go"]}
        run = self.run_dir(base, head)
        request = {"package": "prices", "functions": [{"name": "Clamp", "path": "prices/größe.go", "line": 9}],
                   "inputs": [{"function": "Clamp", "input": "Clamp(101)"}]}
        result = self.examples(run, request, examples_test(("Clamp", "Clamp(101)", "Clamp(101)")))
        self.assertEqual(result["rows"], [example_row("Clamp", "Clamp(101)", "101", "100", "different", "ran_base_and_head")])

    def test_a_test_that_does_not_compile_at_the_head_gives_errors(self):
        run = self.run_dir(PRICES, PRICES_PR)
        result = self.examples(run, examples_request([("Clamp", 9)], [("Clamp", "Clamp(50)")]),
                               examples_test(("Clamp", "Clamp(50)", 'Clamp("50")')))
        [row] = result["rows"]
        self.assertEqual({key: row[key] for key in ["before", "after", "status", "label"]},
                         {"before": None, "after": None, "status": "error", "label": "not_compared"})
        self.assertRegex(row["note"], r"^The test does not compile at the PR head: prices/whooper_examples_test\.go:\d+:\d+: ")

    def test_without_the_test_or_the_go_command_nothing_runs(self):
        request = examples_request([("Clamp", 9)], [("Clamp", "Clamp(50)")])
        for name, test, env, why in [
            ("no test", None, self.env, "the review run wrote no whooper_examples_test.go"),
            ("no go", examples_test(("Clamp", "Clamp(50)", "Clamp(50)")), without_go(self.env),
             "the go command is not on PATH"),
        ]:
            with self.subTest(name):
                run = self.run_dir(PRICES, PRICES_PR)
                self.assertEqual(self.examples(run, request, test, env), {
                    "functions": [{"name": "Clamp", "path": "prices/prices.go", "line": 9}],
                    "rows": [example_row("Clamp", "Clamp(50)", None, None, "error", "not_compared",
                                         f"Whooper did not run the test: {why}.")],
                    "limits": [CHOSE, ONCE_AT_HEAD],
                })
                shutil.rmtree(run)
                shutil.rmtree(self.tmp / "origin")

    def test_examples_whooper_cannot_read_run_nothing(self):
        for name, request, why in [
            ("not JSON", '{"package": ', "examples.json is not JSON"),
            ("a result instead", {"functions": [], "rows": [{"before": "1", "after": "2"}], "limits": []},
             "examples.json lacks package, inputs"),
            ("a package outside the checkout", {**examples_request([("Clamp", 9)], [("Clamp", "Clamp(50)")]),
                                                "package": "../prices"},
             "examples.json names the package ../prices, which is not a folder of the PR's head"),
            ("an input of another function", examples_request([("Clamp", 9)], [("Half", "Half(4)")]),
             "examples.json's inputs[0] names Half, which its functions do not"),
        ]:
            with self.subTest(name):
                run = self.run_dir(PRICES, PRICES_PR)
                result = self.examples(run, request, examples_test(("Clamp", "Clamp(50)", "Clamp(50)")))
                self.assertEqual({key: result[key] for key in ["functions", "rows"]}, {"functions": [], "rows": []})
                [limit] = result["limits"]
                self.assertTrue(limit.startswith(f"Whooper could not read the examples the review run chose, so it ran"
                                                 f" none: {why}"), limit)
                shutil.rmtree(run)
                shutil.rmtree(self.tmp / "origin")

    def test_a_run_that_chose_no_examples_has_none(self):
        self.assertIsNone(self.examples(self.run_dir(PRICES, PRICES_PR), None, None))

    def test_the_step_stops_at_its_time_limit_and_leaves_no_test_running(self):
        pid = self.tmp / "test.pid"
        test = ('package prices\n\nimport (\n\t"os"\n\t"strconv"\n\t"testing"\n\t"time"\n)\n\n'
                f'func TestWhooperExamples(t *testing.T) {{\n\tos.WriteFile("{pid}", []byte(strconv.Itoa(os.Getpid())), 0o644)\n'
                "\ttime.Sleep(time.Minute)\n}\n")
        run = self.run_dir(PRICES, PRICES_PR)
        # The limit comes once the test binary runs, however long go takes to build it, so it finds the test running.
        with limit_once_the_test_runs(pid) as waits:
            result = self.examples(run, examples_request([("Clamp", 9)], [("Clamp", "Clamp(50)")]), test, seconds=8)
        [(left, ran_out)] = waits
        # go test had what was left of the step's 8 seconds, and the step ended soon after they ran out.
        self.assertTrue(0 < left <= 8, left)
        self.assertLess(monotonic() - ran_out, 10)
        self.assertEqual(result["rows"], [example_row("Clamp", "Clamp(50)", None, None, "error", "not_compared",
                                                      "The test ran past Whooper's 8-second limit at the PR head.")])
        deadline = monotonic() + 5
        with self.assertRaises(ProcessLookupError):
            while monotonic() < deadline:
                os.kill(int(pid.read_text()), 0)
                sleep(0.1)


def review_comment(path="services/orders/internal/api/orders.go", line=88, start_line=None, side="RIGHT",
                   created_at="2031-07-27T01:30:00Z"):
    """A review comment as GitHub's REST API lists it, on the head's lines start_line to line."""
    return {"path": path, "line": line, "original_line": line, "start_line": start_line,
            "original_start_line": start_line, "side": side, "created_at": created_at}


class PostTest(unittest.TestCase):
    """whooper.py post for a run started 2031-07-27T01:02:46Z at head h1, whose verdicts are one
    auto_post finding on orders.go:88 and one digest finding."""

    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run = self.data / "runs" / "20310727T010246Z-12"
        self.run.mkdir(parents=True)
        # run.json is the work item the review got, with the run's own fields.
        self.work = {"kind": "first_review", "repo": "example-org/example-repo", "pr": 12, "head_sha": "h1", "base_ref": "master",
                     "source": "github_request", "slack": None, "reason": "A GitHub review request names sam-example."}
        self.item = {**self.work, "run_id": self.run.name, "dry_run": True, "as_of": None}
        (self.run / "run.json").write_text(json.dumps(self.item))
        approval = {"verdict": "look_first", "reason": "The question needs the author's answer."}
        (self.run / "findings.json").write_text(json.dumps({"status": "reviewed", "skip_reason": "", "approval": approval, "findings": []}))
        self.write_verdicts(finding())

    def write_verdicts(self, auto_post):
        verdicts = [{**auto_post, "verdict": "auto_post", "checks_failed": []},
                    {**finding(id="2", title="A question"), "verdict": "digest", "checks_failed": ["rule_v1.ran_and_saw_failure is false"]}]
        (self.run / "verdicts.json").write_text(json.dumps(verdicts))

    def post(self, head_now="h1", comments=()):
        path = self.data / "comments.json"
        path.write_text(json.dumps(list(comments)))
        with redirect_stdout(io.StringIO()):
            main("post", str(self.data), str(self.run), head_now, str(path))
        return json.loads((self.run / "post.json").read_text())

    def digest(self):
        path = self.data / "digest.jsonl"
        return [json.loads(line)["finding"] for line in path.read_text().splitlines()] if path.exists() else []

    def test_request_holds_only_the_auto_post_finding_as_a_comment_review_marked_whooper_ai(self):
        post = self.post()
        request = json.loads((self.run / "post-request.json").read_text())
        self.assertEqual(post["status"], "printed")
        self.assertEqual(request["event"], "COMMENT")
        self.assertEqual(request["commit_id"], "h1")
        self.assertTrue(request["body"].startswith("Whooper AI"))
        self.assertEqual(request["comments"], [{"path": "services/orders/internal/api/orders.go", "line": 88,
                                                "side": "RIGHT", "body": "Whooper AI\n\n**issue:** ..."}])

    def test_moved_head_posts_nothing_marks_the_record_stale_and_lists_the_pr_again(self):
        post = self.post(head_now="h2")
        self.assertEqual(post["status"], "stale")
        self.assertFalse((self.run / "post-request.json").exists())
        record = self.data / "prs" / "example-org-example-repo-12" / "h1.first_review.json"
        self.assertEqual(json.loads(record.read_text())["status"], "stale")
        # The recheck list holds the work item at the new head, for the next round's whooper-select.
        listed = self.data / "rechecks" / "example-org-example-repo-12.json"
        self.assertEqual(json.loads(listed.read_text()), {**self.work, "head_sha": "h2"})

    def test_finding_is_held_back_into_the_digest(self):
        cases = [
            ("a new comment on its line", finding(), [review_comment()]),
            ("a new comment on a range that holds its line", finding(), [review_comment(line=90, start_line=85)]),
            ("an @-mention in the draft", finding(draft="**issue:** @alice, this returns 500."), []),
            ("a link outside the configured orgs", finding(draft="**issue:** see https://example.com/422."), []),
            ("a www link", finding(draft="**issue:** see www.example.com."), []),
        ]
        for name, f, comments in cases:
            with self.subTest(name):
                self.setUp()
                self.write_verdicts(f)
                post = self.post(comments=comments)
                self.assertEqual(post["status"], "nothing to post")
                self.assertFalse((self.run / "post-request.json").exists())
                [held] = self.digest()
                self.assertEqual(held["verdict"], "digest")
                self.assertEqual(len(held["checks_failed"]), 1, held["checks_failed"])

    def test_finding_still_posts(self):
        cases = [
            ("a comment written before the run started", finding(), [review_comment(created_at="2031-07-27T00:59:00Z")]),
            ("a new comment on another line", finding(), [review_comment(line=87)]),
            ("a new comment on the deleted line with the same number", finding(), [review_comment(side="LEFT")]),
            ("a new comment on another file", finding(), [review_comment(path="services/orders/README.md")]),
            ("an email address and a link to an org's PR", finding(
                draft="**issue:** ask alice@example.com; https://github.com/example-org/example-repo/pull/12 has the trace."), []),
        ]
        for name, f, comments in cases:
            with self.subTest(name):
                self.setUp()
                self.write_verdicts(f)
                self.assertEqual(self.post(comments=comments)["status"], "printed")
                self.assertEqual(self.digest(), [])

    def test_summary_counts_the_findings_the_digest_got(self):
        # classify sent the digest finding there; the auto_post one reaches it only when held back.
        cases = [
            ("posted", {}, ["Would post these comments", "- Bad JSON now returns 500 (", "Findings in the digest: 1"]),
            ("held back", {"comments": [review_comment()]}, ["Nothing to post.", "Findings in the digest: 2"]),
            ("stale", {"head_now": "h2"}, ["the PR's head moved to h2", "Findings in the digest: 1"]),
        ]
        for name, post_args, want in cases:
            with self.subTest(name):
                self.setUp()
                self.post(**post_args)
                text = summary_text(self.run)
                self.assertTrue(text.startswith("Whooper reviewed https://github.com/example-org/example-repo/pull/12 at h1 (dry run)."), text)
                self.assertIn("\nLook first: The question needs the author's answer.\n", text)
                for line in want:
                    self.assertIn(line, text)

    def test_second_call_refuses(self):
        self.post()
        with self.assertRaises(SystemExit):
            self.post()


@contextmanager
def at_first_call(name, other):
    """Patches lib/whooper.py's function name so that its first call first runs other() in another
    thread, as another of Whooper's processes would at that moment, and waits half a second for it,
    time enough unless other waits for a lock the caller holds. The block's end waits for other to end.
    flock's locks, unlike POSIX record locks, hold between two threads of one process."""
    function, threads = getattr(whooper, name), []

    def call(*args, **kwargs):
        if not threads:
            threads.append(threading.Thread(target=other))
            threads[0].start()
            threads[0].join(0.5)
        return function(*args, **kwargs)

    with mock.patch(f"whooper.{name}", call):
        try:
            yield
        finally:
            for thread in threads:
                thread.join()


class SubmitTest(unittest.TestCase):
    """bin/whooper-submit, and whooper.py draft, which bin/whooper-draft runs, on a review run of
    example-org/example-repo#12 at head h1 in a throwaway state folder; a few tests run
    bin/whooper-classify and bin/whooper-post on it too, and one runs whooper.py status and show. gh
    is a fake: it records each call in calls.log, reports the PR's head from the file head and the CI
    checks on it from checks.json, lists a teammate's comment on finding 1's line written after the
    review started, records the body of each review it is asked to post in posted.jsonl, and fails any
    other call, so no test can reach GitHub. Unless a test changes it, the run is not safe to approve:
    its verdict is look_first, and of its findings BUG, QUESTION, and NIT, only NIT is a nit."""

    # What GitHub answers a review it created with, in part.
    RESPONSE = {"id": 7001, "html_url": "https://github.com/example-org/example-repo/pull/12#pullrequestreview-7001"}
    BUG = finding(draft="**issue (blocking):** Bad JSON now returns 500 instead of 422.")
    QUESTION = finding(id="2", kind="question", line=40, title="Why retry twice?",
                       draft="**question:** Why does the client retry a call that succeeded?")
    # The review skills open each draft with its label; this one lacks it.
    NIT = finding(id="3", kind="nit", nit_kind="wording", path="services/orders/README.md", line=3, side="LEFT",
                  title="Old flag name", draft="This line still names the old --fast flag.")
    OTHER_NIT = finding(id="4", kind="nit", nit_kind="redundant_code", line=95, title="Unused count",
                        draft="**nit:** `count` is never read.")
    REASONS = {"approve_once_ci_passes": "Ran the orders tests; nothing needs the author.",
               "look_first": "The question needs the author's answer."}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.fakes = tmp / "bin"
        self.fakes.mkdir()
        (self.fakes / "head").write_text("h1\n")
        (self.fakes / "comments.json").write_text(json.dumps([{**review_comment(), "user": {"login": "teammate"}}]))
        gh = self.fakes / "gh"
        gh.write_text("\n".join([
            "#!/bin/sh",
            'fakes="${0%/*}"',
            'echo "$*" >> "$fakes/calls.log"',
            'case "$*" in',
            '  "api repos/example-org/example-repo/pulls/12 --jq .head.sha") cat "$fakes/head" ;;',
            '  "pr view 12 --repo example-org/example-repo --json headRefOid,statusCheckRollup")',
            '    jq -n --arg head "$(cat "$fakes/head")" --slurpfile checks "$fakes/checks.json"'
            " '{headRefOid: $head, statusCheckRollup: $checks[0]}' ;;",
            '  "api --paginate repos/example-org/example-repo/pulls/12/comments?per_page=100") cat "$fakes/comments.json" ;;',
            '  "api --method POST repos/example-org/example-repo/pulls/12/reviews --input "*)',
            '    for arg; do [ "$previous" = --input ] && input="$arg"; previous="$arg"; done',
            # GitHub answers a comment on a line outside the diff with 422, and takes nothing.
            '    if [ -e "$fakes/refuse" ]; then',
            """      echo '{"message": "Unprocessable Entity", "errors": ["Line could not be resolved"], "status": "422"}'""",
            "      echo 'gh: Unprocessable Entity (HTTP 422)' >&2",
            "      exit 1",
            "    fi",
            '    jq -c . "$input" >> "$fakes/posted.jsonl"',
            f"    echo '{json.dumps(self.RESPONSE)}' ;;",
            '  *) echo "fake gh: unexpected call: gh $*" >&2; exit 1 ;;',
            "esac",
        ]) + "\n")
        gh.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{self.fakes}:{os.environ['PATH']}", "XDG_STATE_HOME": str(tmp),
                    "XDG_CACHE_HOME": str(tmp / "cache")}
        self.state = tmp / "whooper"
        self.run = self.state / "runs" / "20310727T010246Z-12"
        self.run.mkdir(parents=True)
        self.work = {"kind": "first_review", "repo": "example-org/example-repo", "pr": 12, "head_sha": "h1",
                     "base_ref": "master", "source": "github_request", "slack": None,
                     "reason": "A GitHub review request names sam-example, who has not reviewed this PR."}
        self.write_run(self.work)
        self.write_findings("look_first", [self.BUG, self.QUESTION, self.NIT])

    def write_run(self, work):
        """Writes the run's run.json: the work item the review got, with the run's own fields."""
        (self.run / "run.json").write_text(json.dumps({**work, "run_id": self.run.name, "dry_run": True, "as_of": None}))

    def write_findings(self, verdict, findings, skip_reason=""):
        """Gives the run the approval verdict, with its reason in REASONS, and the findings. With a
        skip_reason, the review skipped the PR, as it does a large PR from an untagged ask."""
        approval = {"verdict": verdict, "reason": self.REASONS[verdict]}
        status = "skipped" if skip_reason else "reviewed"
        (self.run / "findings.json").write_text(json.dumps(
            {"status": status, "skip_reason": skip_reason, "approval": approval, "findings": findings}))

    def draft(self, *change):
        """whooper.py draft on the run, as bin/whooper-draft runs it; returns the drafts it prints."""
        out = io.StringIO()
        with redirect_stdout(out):
            main("draft", str(self.state), self.run.name, *change)
        return json.loads(out.getvalue())

    def submit(self, *flags):
        return subprocess.run([str(ROOT / "bin" / "whooper-submit"), self.run.name, *flags], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def posted(self):
        """The body of each review the fake gh was asked to post."""
        path = self.fakes / "posted.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def gh_calls(self):
        """The arguments of each call of the fake gh, joined by spaces."""
        path = self.fakes / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def shown(self, *flags):
        """Runs whooper-submit --print with flags, as Prompting Time's confirmation does, and returns
        the fingerprint it prints."""
        printed = self.submit("--print", *flags)
        self.assertEqual(printed.returncode, 0, printed.stderr)
        return printed.stdout.splitlines()[-1].removeprefix("fingerprint: ")

    def hold(self):
        """Has the lock of the run's PR name the run and a stand-in for a live whooper-review, as
        whooper-review's lock does while it reviews; returns the lock."""
        lock = self.state / "locks" / "example-org-example-repo-12"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{review_stand_in(self, Path(self.tmp.name) / 'review')}\n")
        (lock / "run_id").write_text(f"{self.run.name}\n")
        return lock

    def test_posts_the_included_drafts_as_one_review_at_the_reviewed_head(self):
        for change in [("include", "1"), ("include", "2"), ("include", "3"), ("approve",),
                       ("text", "2", "Why retry a call that succeeded? It duplicates the order."),
                       ("message", "Pre-approving to unblock")]:
            self.draft(*change)
        # The run is not safe to approve, and the user chose to approve it anyway.
        result = self.submit("--approve-anyway")
        self.assertEqual(result.returncode, 0, result.stderr)
        # A comment keeps the label its text opens with, and a text without one, such as finding 2's edit
        # or finding 3's draft, gets its finding kind's.
        request = {"commit_id": "h1", "event": "APPROVE", "body": "Pre-approving to unblock", "comments": [
            {"path": "services/orders/internal/api/orders.go", "line": 88, "side": "RIGHT",
             "body": "AI comment (vetted)\n\n**issue (blocking):** Bad JSON now returns 500 instead of 422."},
            {"path": "services/orders/internal/api/orders.go", "line": 40, "side": "RIGHT",
             "body": "AI comment (vetted)\n\n**question:** Why retry a call that succeeded? It duplicates the order."},
            {"path": "services/orders/README.md", "line": 3, "side": "LEFT",
             "body": "AI comment (vetted)\n\n**nit:** This line still names the old --fast flag."},
        ]}
        self.assertEqual(self.posted(), [request])
        self.assertEqual(json.loads((self.run / "submit.json").read_text()),
                         {"status": "posted", "request": request, "response": self.RESPONSE})
        self.assertIn(self.RESPONSE["html_url"], result.stdout)

    def test_a_finding_left_out_stays_out(self):
        for change in [("include", "1"), ("include", "2"), ("leave-out", "1")]:
            self.draft(*change)
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        [request] = self.posted()
        self.assertEqual((request["event"], request["body"]), ("COMMENT", ""))
        self.assertEqual([c["line"] for c in request["comments"]], [40])

    def test_new_commits_post_nothing_and_leave_the_pr_to_the_next_round(self):
        self.draft("include", "1")
        (self.fakes / "head").write_text("h2\n")
        for flags in [["--print"], []]:
            with self.subTest(flags=flags):
                result = self.submit(*flags)
                self.assertEqual(result.returncode, 10)
                self.assertIn("new commits", result.stderr)
                self.assertIn("bin/whooper-round", result.stderr)
                self.assertNotIn("requests your review", result.stderr)
        self.assertEqual(self.posted(), [])
        self.assertFalse((self.run / "submit.json").exists())
        self.assertFalse((self.run / "submit-request.json").exists())
        # The mark whooper-post leaves: the reviewed head's record says stale, and the recheck list
        # holds the work item at the new head.
        record = self.state / "prs" / "example-org-example-repo-12" / "h1.first_review.json"
        self.assertEqual(json.loads(record.read_text())["status"], "stale")
        listed = self.state / "rechecks" / "example-org-example-repo-12.json"
        self.assertEqual(json.loads(listed.read_text()), {**self.work, "head_sha": "h2"})

    def test_new_commits_bring_back_a_pr_only_a_slack_ask_brought_in(self):
        slack = {"ts": "1943194837.000100", "tagged_user": True,
                 "permalink": "https://example.slack.com/archives/C0000000001/p1943194837000100"}
        work = {**self.work, "source": "slack_ask", "slack": slack, "reason": "Alex asked for a review."}
        self.write_run(work)
        (self.fakes / "head").write_text("h2\n")
        self.assertEqual(self.submit().returncode, 10)
        # A poll reads only the Slack posts new since the last poll, and GitHub requests no review of
        # this PR, so the next round's poll lists other work only.
        other = {**work_item(kind="first_review", head="h7", number=7), "repo": "sample-org/plain-repo"}
        (self.state / "poll").mkdir()
        (self.state / "poll" / "20310727T020000Z.json").write_text(json.dumps({"items": [other]}))

        def select():
            result = subprocess.run([str(ROOT / "bin" / "whooper-select")], env=self.env, capture_output=True,
                                    text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            return [json.loads(line) for line in result.stdout.splitlines()]

        self.assertEqual(select(), [other, {**work, "head_sha": "h2"}])
        # Once the review of the new head is done, whooper-select takes the PR off the list.
        done = {"status": "done", "failures": 0, "started_at": "2031-07-27T02:05:00+00:00"}
        (self.state / "prs" / "example-org-example-repo-12" / "h2.first_review.json").write_text(json.dumps(done))
        self.assertEqual(select(), [other])
        self.assertFalse((self.state / "rechecks" / "example-org-example-repo-12.json").exists())

    def test_an_at_mention_posts_only_with_allow_mentions(self):
        self.draft("include", "1")
        self.draft("text", "1", "**issue:** @alice, bad JSON now returns 500; alice@example.com has the trace.")
        self.draft("message", "cc @example-org/orders-team")
        for flags in [[], ["--print"]]:
            with self.subTest(flags=flags):
                refused = self.submit(*flags)
                self.assertEqual(refused.returncode, 12)
                for text in ["@alice in the comment on services/orders/internal/api/orders.go:88 (finding 1)",
                             "@example-org/orders-team in the message", "--allow-mentions"]:
                    self.assertIn(text, refused.stderr)
                # An email address mentions nobody.
                self.assertNotIn("@example.com", refused.stderr)
        self.assertEqual(self.posted(), [])
        self.assertFalse((self.run / "submit-request.json").exists())
        allowed = self.submit("--allow-mentions")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        [request] = self.posted()
        self.assertEqual(request["body"], "cc @example-org/orders-team")
        self.assertEqual(request["comments"][0]["body"],
                         "AI comment (vetted)\n\n**issue:** @alice, bad JSON now returns 500; alice@example.com has the trace.")

    def test_a_teammates_comment_on_the_line_and_an_outside_link_do_not_stop_it(self):
        # The fake gh lists a teammate's comment on finding 1's line, written after the review started.
        self.draft("include", "1")
        self.draft("text", "1", "**issue:** Bad JSON now returns 500; https://example.com/rfc says 422.")
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        [request] = self.posted()
        self.assertEqual(request["comments"][0]["body"],
                         "AI comment (vetted)\n\n**issue:** Bad JSON now returns 500; https://example.com/rfc says 422.")

    def test_print_writes_the_request_that_would_post_and_posts_nothing(self):
        self.draft("include", "2")
        printed = self.submit("--print")
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertIn("submit-request.json", printed.stdout)
        self.assertEqual(self.posted(), [])
        self.assertFalse((self.run / "submit.json").exists())
        # Its last line is the request's fingerprint: the SHA-256 of the file it wrote.
        text = (self.run / "submit-request.json").read_bytes()
        self.assertEqual(printed.stdout.splitlines()[-1], f"fingerprint: {hashlib.sha256(text).hexdigest()}")
        self.assertEqual(self.submit().returncode, 0)
        self.assertEqual(self.posted(), [json.loads(text)])

    def test_expect_posts_only_the_request_print_showed(self):
        missing = self.submit("--expect")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("usage", missing.stderr)
        self.draft("include", "2")
        confirmed = self.shown()
        # Whooper's agent edits the comment after the user confirmed the review.
        self.draft("text", "2", "**question:** Why retry at all?")
        changed = self.submit("--expect", confirmed)
        self.assertEqual(changed.returncode, 13, changed.stderr)
        for text in ["the drafts changed", "--print"]:
            self.assertIn(text, changed.stderr)
        self.assertEqual(self.posted(), [])
        self.assertFalse((self.run / "submit.json").exists())
        # The file still holds the request the confirmation showed.
        self.assertEqual(hashlib.sha256((self.run / "submit-request.json").read_bytes()).hexdigest(), confirmed)
        # Shown and confirmed again, the new request posts.
        again = self.shown()
        self.assertNotEqual(again, confirmed)
        result = self.submit("--expect", again)
        self.assertEqual(result.returncode, 0, result.stderr)
        [request] = self.posted()
        self.assertEqual(request["comments"][0]["body"], "AI comment (vetted)\n\n**question:** Why retry at all?")

    def test_a_review_posts_once(self):
        self.draft("include", "1")
        self.assertEqual(self.submit().returncode, 0)
        for flags in [[], ["--print"]]:
            with self.subTest(flags=flags):
                again = self.submit(*flags)
                self.assertEqual(again.returncode, 1)
                self.assertIn("already submitted", again.stderr)
        self.assertEqual(len(self.posted()), 1)
        # A change now would not reach GitHub.
        with self.assertRaises(SystemExit):
            self.draft("include", "2")

    def test_a_submit_that_did_not_finish_blocks_the_next(self):
        # A submit stopped while gh posted leaves submit.json saying posting, and the review may be on GitHub.
        self.draft("include", "1")
        (self.run / "submit.json").write_text(json.dumps({"status": "posting", "request": {}}))
        result = self.submit()
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not finish", result.stderr)
        self.assertEqual(self.posted(), [])

    def test_a_submit_json_that_cannot_be_read_blocks_the_next_without_a_traceback(self):
        # Such as one cut short or edited by hand: the review it records may be on GitHub.
        self.draft("include", "1")
        for text in ["", '{"status": "post', "[]"]:
            with self.subTest(text):
                (self.run / "submit.json").write_text(text)
                result = self.submit()
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", f"whooper-submit: {self.run.resolve()} has a submit.json that cannot be read, so"
                                  " its review may be on GitHub; check the PR, then delete submit.json to submit again\n"))
        self.assertEqual((self.posted(), self.gh_calls()), ([], []))

    def test_of_two_submits_at_once_only_one_posts(self):
        # Another submit claims the run after this one found no submit.json, while it reads the drafts.
        self.draft("include", "1")
        claim = {"status": "posting", "request": {}}

        def claimed_meanwhile(run_dir):
            (self.run / "submit.json").write_text(json.dumps(claim))
            return read_drafts(run_dir)

        with mock.patch("whooper.read_drafts", claimed_meanwhile), self.assertRaises(SystemExit) as caught:
            main("submit", str(self.state), str(self.run), "h1")
        self.assertIn("already submitted", str(caught.exception))
        self.assertEqual(json.loads((self.run / "submit.json").read_text()), claim)
        # The losing claim's temporary file is gone too.
        self.assertEqual(sorted(p.name for p in self.run.iterdir()),
                         ["drafts.json", "findings.json", "run.json", "submit-request.json", "submit.json"])

    def test_a_review_github_refuses_can_be_submitted_again(self):
        self.draft("include", "1")
        (self.fakes / "refuse").touch()
        refused = self.submit()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("Line could not be resolved", refused.stderr)
        self.assertFalse((self.run / "submit.json").exists())
        (self.fakes / "refuse").unlink()
        self.assertEqual(self.submit().returncode, 0)
        self.assertEqual(len(self.posted()), 1)

    def test_a_review_without_comments_posts_its_message(self):
        # A run with no finding is safe to approve, so its review starts as an approval, and the run setUp
        # writes is not, so its review starts as a comment. Neither includes a comment.
        cases = [
            # (name, the run's verdict, its findings, the message the user set, the event that posts)
            ("an approval", "approve_once_ci_passes", [], "lgtm", "APPROVE"),
            ("a comment", "look_first", [self.BUG, self.QUESTION, self.NIT], "Thanks, nothing from me.", "COMMENT"),
        ]
        for name, verdict, findings, message, event in cases:
            with self.subTest(name):
                self.setUp()
                self.write_findings(verdict, findings)
                self.draft("message", message)
                result = self.submit()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.posted(), [{"commit_id": "h1", "event": event, "body": message, "comments": []}])

    def test_a_comment_without_comments_or_a_message_has_nothing_to_submit(self):
        # The run is not safe to approve, so its review starts as a comment with nothing included, and a
        # message of blanks says nothing either.
        for name, message in [("no message", None), ("a blank message", " \n")]:
            with self.subTest(name):
                if message is not None:
                    self.draft("message", message)
                for flags in [[], ["--print"]]:
                    refused = self.submit(*flags)
                    self.assertEqual(refused.returncode, 1, refused.stderr)
                    self.assertIn("nothing to submit", refused.stderr)
        self.assertEqual(self.posted(), [])
        self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["drafts.json", "findings.json", "run.json"])

    def test_print_shows_a_review_of_only_a_message_and_its_fingerprint(self):
        # The run is not safe to approve, so its review starts as a comment with nothing included.
        self.draft("message", "Thanks, nothing from me.")
        confirmed = self.shown()
        request = {"commit_id": "h1", "event": "COMMENT", "body": "Thanks, nothing from me.", "comments": []}
        text = (self.run / "submit-request.json").read_bytes()
        self.assertEqual((json.loads(text), hashlib.sha256(text).hexdigest()), (request, confirmed))
        self.assertEqual(self.posted(), [])
        result = self.submit("--expect", confirmed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.posted(), [request])

    def test_a_comment_outside_the_diff_stops_the_review_before_it_reads_github(self):
        # GitHub takes an inline comment only on a line inside a hunk of the PR's diff, and refuses the
        # whole review for one comment elsewhere. The review run says which lines are inside with in_diff.
        outside = finding(id="5", kind="question", path="services/orders/internal/api/client.go", line=12,
                          in_diff=False, title="Does the gateway retry too?",
                          draft="**question:** Does the gateway retry this call too?")
        self.write_findings("look_first", [self.BUG, self.QUESTION, {**self.NIT, "in_diff": False}, outside])
        for finding_id in ["1", "3", "5"]:
            self.draft("include", finding_id)
        for flags in [[], ["--print"]]:
            with self.subTest(flags=flags):
                refused = self.submit(*flags)
                self.assertEqual(refused.returncode, 14, refused.stderr)
                self.assertEqual([line.strip() for line in refused.stderr.splitlines() if line.startswith("  ")],
                                 ["finding 3: Old flag name (services/orders/README.md:3)",
                                  "finding 5: Does the gateway retry too? (services/orders/internal/api/client.go:12)"])
                self.assertIn("bin/whooper-draft", refused.stderr)
        # It refused before any call to GitHub, even the one that reads the PR's head.
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual(self.posted(), [])
        self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["drafts.json", "findings.json", "run.json"])
        # A comment outside the diff that the review leaves out stops nothing.
        for finding_id in ["3", "5"]:
            self.draft("leave-out", finding_id)
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        [request] = self.posted()
        self.assertEqual([c["line"] for c in request["comments"]], [88])

    def test_a_safe_run_starts_as_an_approval_of_its_nits_and_posts_it(self):
        self.write_findings("approve_once_ci_passes", [self.NIT, self.OTHER_NIT])
        shown = subprocess.run([str(ROOT / "bin" / "whooper-draft"), self.run.name], env=self.env,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(shown.stdout), {"event": "APPROVE", "message": "", "findings": {
            "3": {"included": True}, "4": {"included": True}}})
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.posted(), [{"commit_id": "h1", "event": "APPROVE", "body": "Pre-approving to unblock",
                                          "comments": [
            {"path": "services/orders/README.md", "line": 3, "side": "LEFT",
             "body": "AI comment (vetted)\n\n**nit:** This line still names the old --fast flag."},
            {"path": "services/orders/internal/api/orders.go", "line": 95, "side": "RIGHT",
             "body": "AI comment (vetted)\n\n**nit:** `count` is never read."},
        ]}])

    def test_a_safe_run_approves_whatever_ci_reports(self):
        # The PR's author sees a failed check and fixes it, so CI does not bear on the approval: a safe
        # run approves while a check fails or before any has reported, and the request the
        # confirmation shows, which is the one that posts, says nothing about CI.
        failed = {"__typename": "CheckRun", "name": "test", "workflowName": "CI", "status": "COMPLETED",
                  "conclusion": "FAILURE"}
        request = {"commit_id": "h1", "event": "APPROVE", "body": "Pre-approving to unblock", "comments": [
            {"path": "services/orders/README.md", "line": 3, "side": "LEFT",
             "body": "AI comment (vetted)\n\n**nit:** This line still names the old --fast flag."}]}
        for name, checks in [("a check failed", [failed]), ("no check has reported", [])]:
            with self.subTest(name):
                self.setUp()
                self.write_findings("approve_once_ci_passes", [self.NIT])
                (self.fakes / "checks.json").write_text(json.dumps(checks))
                confirmed = self.shown()
                self.assertEqual(json.loads((self.run / "submit-request.json").read_text()), request)
                result = self.submit("--expect", confirmed)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.posted(), [request])

    def test_any_other_run_starts_as_a_comment_with_nothing_included(self):
        for name, verdict, findings in [
            ("the verdict is look_first", "look_first", [self.NIT]),
            ("a finding is not a nit", "approve_once_ci_passes", [self.NIT, self.QUESTION]),
        ]:
            with self.subTest(name):
                self.write_findings(verdict, findings)
                self.assertEqual(self.draft(), {"event": "COMMENT", "message": "", "findings": {}})

    def test_approving_a_run_that_is_not_safe_waits_for_the_users_decision(self):
        cases = [
            # (name, verdict, findings, the reasons whooper-submit lists)
            ("the verdict is look_first", "look_first", [self.NIT],
             ["the run's verdict is look_first: The question needs the author's answer."]),
            ("findings that are not nits", "approve_once_ci_passes", [self.BUG, self.NIT, self.QUESTION],
             ["finding 1 is a caller_visible_bug: Bad JSON now returns 500 (services/orders/internal/api/orders.go:88)",
              "finding 2 is a question: Why retry twice? (services/orders/internal/api/orders.go:40)"]),
        ]
        for name, verdict, findings, reasons in cases:
            with self.subTest(name):
                self.setUp()
                self.write_findings(verdict, findings)
                self.draft("approve")
                for flags in [[], ["--print"]]:
                    refused = self.submit(*flags)
                    self.assertEqual(refused.returncode, 11, refused.stderr)
                    self.assertEqual([line.strip() for line in refused.stderr.splitlines() if line.startswith("  ")],
                                     reasons)
                    self.assertIn("--approve-anyway", refused.stderr)
                self.assertEqual(self.posted(), [])
                self.assertFalse((self.run / "submit-request.json").exists())
                # The user looked, and approves anyway.
                approved = self.submit("--approve-anyway")
                self.assertEqual(approved.returncode, 0, approved.stderr)
                self.assertEqual([request["event"] for request in self.posted()], ["APPROVE"])

    def test_a_review_that_only_comments_needs_no_decision(self):
        # The run is not safe to approve: its verdict is look_first, and two of its findings are not nits.
        self.draft("include", "1")
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([request["event"] for request in self.posted()], ["COMMENT"])

    def test_a_skipped_review_is_not_safe_to_approve_whatever_its_verdict(self):
        # The review skipped a large PR from an untagged ask, so it read too little to vouch for the PR,
        # though its verdict says approve and it has no finding.
        why = "The PR changes 40 files across four services."
        self.write_findings("approve_once_ci_passes", [], skip_reason=why)
        self.assertEqual(self.draft(), {"event": "COMMENT", "message": "", "findings": {}})
        self.draft("approve")
        for flags in [[], ["--print"]]:
            with self.subTest(flags=flags):
                refused = self.submit(*flags)
                self.assertEqual(refused.returncode, 11, refused.stderr)
                self.assertEqual([line.strip() for line in refused.stderr.splitlines() if line.startswith("  ")],
                                 [f"the review was skipped: {why}"])
                self.assertIn("--approve-anyway", refused.stderr)
        self.assertEqual(self.posted(), [])
        # The user looked, and approves anyway.
        approved = self.submit("--approve-anyway")
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.assertEqual(self.posted(), [{"commit_id": "h1", "event": "APPROVE", "body": "", "comments": []}])

    def test_a_review_that_failed_is_not_safe_to_approve_whatever_its_findings(self):
        # A Devin review writes findings.json itself. This one wrote a safe run's findings and then passed its
        # time limit, so whooper-review stopped it before it finished.
        why = f"the review run passed its 45-minute limit; see {self.run}/stderr.log and the transcript in {self.run}"

        def fail():
            self.write_findings("approve_once_ci_passes", [self.NIT])
            (self.run / "failure.txt").write_text(f"{why}\n")

        fail()
        self.assertEqual(self.draft(), {"event": "COMMENT", "message": "", "findings": {}})
        self.draft("approve")
        for flags in [[], ["--print"]]:
            with self.subTest(flags=flags):
                refused = self.submit(*flags)
                self.assertEqual(refused.returncode, 11, refused.stderr)
                self.assertEqual([line.strip() for line in refused.stderr.splitlines() if line.startswith("  ")],
                                 [f"the review failed: {why}"])
                self.assertIn("--approve-anyway", refused.stderr)
        self.assertEqual(self.posted(), [])
        # The user looked, and approves anyway.
        approved = self.submit("--approve-anyway")
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.assertEqual(self.posted(), [{"commit_id": "h1", "event": "APPROVE", "body": "", "comments": []}])
        # Only an approval waits for the user's look: a failed review's comments post as they are.
        self.setUp()
        fail()
        self.draft("include", "3")
        commented = self.submit()
        self.assertEqual(commented.returncode, 0, commented.stderr)
        self.assertEqual([request["event"] for request in self.posted()], ["COMMENT"])

    def test_a_review_still_running_cannot_be_submitted(self):
        # A Devin review writes findings.json itself and may change it until it ends, so while its whooper-review
        # holds the PR's lock for the run, nothing of it posts, not even a safe run's findings as a comment.
        self.write_findings("approve_once_ci_passes", [self.NIT])
        lock = self.hold()
        comment = {"event": "COMMENT", "message": "", "findings": {"3": {"included": True}}}
        for name, drafts in [("an approval of its nit", None), ("a comment", comment)]:
            if drafts:
                (self.run / "drafts.json").write_text(json.dumps(drafts))
            for flags in [[], ["--print"], ["--approve-anyway"]]:
                with self.subTest(name, flags=flags):
                    refused = self.submit(*flags)
                    self.assertEqual((refused.returncode, refused.stdout, refused.stderr),
                                     (1, "", "whooper-submit: the review is still running; submit it once it ends\n"))
        self.assertEqual(self.posted(), [])
        self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["drafts.json", "findings.json", "run.json"])
        # whooper-review deletes the lock when the review ends.
        shutil.rmtree(lock)
        submitted = self.submit()
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        self.assertEqual([request["event"] for request in self.posted()], ["COMMENT"])

    def test_status_and_show_call_a_run_safe_to_approve_exactly_when_submit_approves_it_without_approve_anyway(self):
        # Prompting Time reads safe_to_approve from whooper-status and whooper-show, so it must match what
        # whooper-submit does with an approval.
        why = "The PR changes 40 files across four services."

        def failed():
            # As a Devin review that passed its time limit after it wrote its findings.
            (self.run / "failure.txt").write_text("the review run passed its 45-minute limit\n")

        cases = {
            "only nits": ("approve_once_ci_passes", [self.NIT, self.OTHER_NIT], "", None),
            "no finding": ("approve_once_ci_passes", [], "", None),
            "the verdict is look_first": ("look_first", [self.NIT], "", None),
            "a finding that is not a nit": ("approve_once_ci_passes", [self.NIT, self.QUESTION], "", None),
            "the review skipped the PR": ("approve_once_ci_passes", [], why, None),
            # Only a nit, in a review that did not finish: it failed after its run wrote the findings, or it
            # still runs.
            "the review failed": ("approve_once_ci_passes", [self.NIT], "", failed),
            "the review still runs": ("approve_once_ci_passes", [self.NIT], "", self.hold),
        }
        seen = {}
        for name, (verdict, findings, skip_reason, unfinished) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.write_findings(verdict, findings, skip_reason)
                if unfinished:
                    unfinished()
                self.draft("approve")
                # --print runs every check whooper-submit runs before it posts, and posts nothing.
                submitted = self.submit("--print")
                status, show = io.StringIO(), io.StringIO()
                with redirect_stdout(status):
                    main("status", str(self.state))
                with redirect_stdout(show):
                    main("show", str(self.state), self.run.name)
                [review] = json.loads(status.getvalue())["reviews"]
                seen[name] = (submitted.returncode, review["safe_to_approve"],
                              json.loads(show.getvalue())["safe_to_approve"])
        # Each case as (whooper-submit's exit code, whooper-status's safe_to_approve, whooper-show's): 0 when
        # whooper-submit would approve, 11 when the run is not safe to approve, and 1 when it refuses the review.
        self.assertEqual(seen, {"only nits": (0, True, True), "no finding": (0, True, True),
                                "the verdict is look_first": (11, False, False),
                                "a finding that is not a nit": (11, False, False),
                                "the review skipped the PR": (11, False, False),
                                "the review failed": (11, False, False),
                                "the review still runs": (1, False, False)})

    def test_an_approval_without_a_message_says_pre_approving_only_when_it_comments(self):
        self.write_findings("approve_once_ci_passes", [self.NIT, self.OTHER_NIT])
        cases = [
            # (name, changes to the drafts the safe run starts with, the message that would post)
            ("with its nits", [], "Pre-approving to unblock"),
            ("with no comment", [("leave-out", "3"), ("leave-out", "4")], ""),
            ("with a message the user set", [("message", "Thanks, looks good.")], "Thanks, looks good."),
            ("with no comment and a message the user set", [("leave-out", "3"), ("leave-out", "4"), ("message", "LGTM")],
             "LGTM"),
            ("only commenting", [("comment",)], ""),
        ]
        for name, changes, message in cases:
            with self.subTest(name):
                (self.run / "drafts.json").unlink(missing_ok=True)
                for change in changes:
                    self.draft(*change)
                self.shown()
                self.assertEqual(json.loads((self.run / "submit-request.json").read_text())["body"], message)

    def test_whooper_draft_records_each_choice_and_prints_the_drafts(self):
        command = [str(ROOT / "bin" / "whooper-draft"), self.run.name]
        # The run is not safe to approve, so before any change it only comments and includes nothing.
        shown = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual((shown.returncode, json.loads(shown.stdout)), (0, {"event": "COMMENT", "message": "", "findings": {}}))
        self.assertFalse((self.run / "drafts.json").exists())
        edited = subprocess.run(command + ["text", "2", "**question:** Why retry?"], env=self.env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(json.loads(edited.stdout)["findings"], {"2": {"included": False, "text": "**question:** Why retry?"}})
        for change in [("include", "2"), ("include", "3"), ("leave-out", "3"), ("approve",),
                       ("message", "Pre-approving to unblock")]:
            drafts = self.draft(*change)
        self.assertEqual(drafts, {"event": "APPROVE", "message": "Pre-approving to unblock", "findings": {
            "2": {"included": True, "text": "**question:** Why retry?"}, "3": {"included": False}}})
        self.assertEqual(self.draft("comment"), {**drafts, "event": "COMMENT"})
        # Each change went to a temporary file renamed into place, so only drafts.json is new.
        self.assertEqual(json.loads((self.run / "drafts.json").read_text()), {**drafts, "event": "COMMENT"})
        self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["drafts.json", "findings.json", "run.json"])

    def test_two_changes_to_the_drafts_at_once_both_stay(self):
        # Prompting Time's page includes finding 2 while Whooper's agent sets the message: the agent's change
        # comes between the page's read of the drafts and its write.
        with at_first_call("write_file", lambda: change_drafts(self.run, "message", "Thanks!")):
            change_drafts(self.run, "include", "2")
        self.assertEqual(self.draft(), {"event": "COMMENT", "message": "Thanks!", "findings": {"2": {"included": True}}})

    def test_whooper_draft_refuses_a_change_it_cannot_record(self):
        for name, change, why in [
            ("a finding the run does not have", ["include", "9"], "no finding 9"),
            ("an empty text", ["text", "1", " \n"], "empty"),
            ("an unknown change", ["reject", "1"], "usage"),
            ("a change missing its text", ["text", "1"], "usage"),
        ]:
            with self.subTest(name), self.assertRaises(SystemExit) as caught:
                self.draft(*change)
            self.assertIn(why, str(caught.exception))
        self.assertFalse((self.run / "drafts.json").exists())
        with self.assertRaises(SystemExit) as caught:
            main("draft", str(self.state))
        self.assertIn("usage", str(caught.exception))
        missing = subprocess.run([str(ROOT / "bin" / "whooper-draft"), "20310101T000000Z-1"], env=self.env,
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no run 20310101T000000Z-1", missing.stderr)

    def test_a_review_that_did_not_finish_has_no_drafts_and_cannot_be_submitted(self):
        for name, content, why in UNFINISHED:
            if content is None:
                (self.run / "findings.json").unlink(missing_ok=True)
            else:
                (self.run / "findings.json").write_text(content)
            # whooper-submit names the run directory resolved, and on macOS a folder under $TMPDIR
            # resolves through /private.
            for command, args, run in [("draft", [self.run.name], self.run),
                                       ("draft", [self.run.name, "approve"], self.run),
                                       ("submit", [str(self.run), "h1"], self.run.resolve())]:
                with self.subTest(name, command=command, args=args[1:]):
                    with self.assertRaises(SystemExit) as caught:
                        main(command, str(self.state), *args)
                    self.assertEqual(str(caught.exception), f"whooper-{command}: {run} {why}")
        self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["findings.json", "run.json"])

    def test_draft_submit_and_classify_refuse_findings_that_do_not_fit_the_schema(self):
        # Findings and little else, as a Devin review stopped at its time limit can leave. An ask still
        # answers about such a review (AskTest), but these three read its fields without checking for
        # each one.
        (self.run / "findings.json").write_text(json.dumps({"findings": [self.BUG, self.QUESTION]}))
        for command, argument, run in [("whooper-draft", self.run.name, self.run),
                                       ("whooper-submit", self.run.name, self.run.resolve()),
                                       ("whooper-classify", str(self.run), self.run)]:
            with self.subTest(command):
                result = subprocess.run([str(ROOT / "bin" / command), argument], env=self.env, capture_output=True,
                                        text=True, timeout=60)
                # The refusal alone, with no traceback.
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", f"{command}: {run} {UNFIT}findings.json lacks status, skip_reason, approval\n"))
        # Nothing was drafted, posted, classified, or recorded.
        self.assertEqual(self.posted(), [])
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["runs"])
        self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["findings.json", "run.json"])

    def test_submit_and_post_refuse_a_run_json_they_cannot_read(self):
        # whooper-review fills run.json with one jq, and the file stays empty when that jq fails, as when
        # the skills cannot be hashed.
        for name, content in [("empty", ""), ("cut short", '{"repo": "example-org/exa')]:
            (self.run / "run.json").write_text(content)
            for command, flags in [("whooper-submit", []), ("whooper-post", ["--print"])]:
                with self.subTest(name, command=command):
                    result = subprocess.run([str(ROOT / "bin" / command), self.run.name, *flags], env=self.env,
                                            capture_output=True, text=True, timeout=60)
                    self.assertEqual((result.returncode, result.stdout, result.stderr),
                                     (1, "", f"{command}: cannot read {self.run / 'run.json'}\n"))
        self.assertEqual(self.posted(), [])


# The bashes a script's #!/usr/bin/env bash line may start, each file once whatever links lead to it:
# /bin/bash, macOS's bash 3.2, which comes first on the PATH a Prompting Time opened from the Dock gives
# Whooper, and each bash on PATH, since env starts the first one. The tests of a script whose exit code
# must hold under any bash run it under each.
BASHES = list(dict.fromkeys(
    path.resolve()
    for path in [Path("/bin/bash"), *(Path(entry) / "bash" for entry in os.environ["PATH"].split(os.pathsep))]
    if os.access(path, os.X_OK)))


def bash_folder(bash, folder):
    """Makes folder, with a link to bash named bash in it, and returns it. While folder comes first on
    PATH, a script's #!/usr/bin/env bash line starts that bash."""
    folder.mkdir()
    (folder / "bash").symlink_to(bash)
    return folder


def running(pid):
    """Whether the process pid runs: it exists and has not yet exited, as a zombie its parent has not reaped has."""
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


class SlackGuardTest(unittest.TestCase):
    """hooks/slack-guard.sh, fed the PreToolUse input Claude Code sends it, under each bash in BASHES."""

    HOOK = ROOT / "hooks" / "slack-guard.sh"
    SLACK = "mcp__claude_ai_Slack__slack"
    # The channels of the test config, which the hook reads through WHOOPER_CONFIG.
    ASK_CHANNEL, SUMMARY_CHANNEL = "C0000000001", "C0000000002"
    ASK = {"channel_id": ASK_CHANNEL, "message_ts": "1761700000.000100", "emoji": "eyes"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        text = "Whooper reviewed https://github.com/example-org/example-repo/pull/12 at cb54aa8.\nLog: log.md"
        self.schedule = {"channel_id": self.SUMMARY_CHANNEL, "message": text, "post_at": 1761700180}
        # Each bash gets a folder that puts it first on PATH, and a summary file of its own, since the hook
        # lets each file's summary through once.
        self.folders, self.summaries = {}, {}
        for n, bash in enumerate(BASHES):
            self.folders[bash] = bash_folder(bash, self.tmp / f"bash-{n}")
            self.summaries[bash] = self.tmp / f"summary-{n}.txt"
            self.summaries[bash].write_text(text)

    def run_hook(self, bash, tool, tool_input, **env):
        """Runs the hook as Claude Code does, by its path, with bash first on PATH, and with the variables a
        summary run sets, changed by env, where None unsets one."""
        env = {"PATH": os.environ["PATH"], "WHOOPER_CONFIG": os.environ["WHOOPER_CONFIG"],
               "WHOOPER_ASK_TS": "1761700000.000100", "WHOOPER_SUMMARY_FILE": str(self.summaries[bash]),
               "WHOOPER_SUMMARY_POST_AT": "1761700180", **env}
        env = {k: v for k, v in env.items() if v is not None}
        env["PATH"] = f"{self.folders[bash]}{os.pathsep}{env['PATH']}"
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": f"{self.SLACK}_{tool}", "tool_input": tool_input})
        return subprocess.run([str(self.HOOK)], input=payload, env=env, capture_output=True, text=True).returncode

    def test_allows_eyes_on_the_ask_and_the_composed_summary(self):
        for bash in BASHES:
            with self.subTest(bash=str(bash)):
                self.assertEqual(self.run_hook(bash, "add_reaction", self.ASK), 0)
                self.assertEqual(self.run_hook(bash, "schedule_message", self.schedule), 0)

    def test_blocks_everything_else(self):
        # With no config, lib/whooper.py env prints nothing, so the channels it sets stay unset.
        no_config = {"WHOOPER_CONFIG": str(self.tmp / "missing.toml")}
        # A jq that fails stands in for any command in the hook that fails where it should not.
        fakes = self.tmp / "fakes"
        fake(fakes / "jq", "exit 1")
        failing_jq = {"PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}"}
        cases = [
            ("wrong channel", "add_reaction", {**self.ASK, "channel_id": self.SUMMARY_CHANNEL}, {}),
            ("wrong ts", "add_reaction", {**self.ASK, "message_ts": "1761699999.000100"}, {}),
            ("wrong emoji", "add_reaction", {**self.ASK, "emoji": "white_check_mark"}, {}),
            ("a run that took no ask", "add_reaction", self.ASK, {"WHOOPER_ASK_TS": ""}),
            ("a canvas", "create_canvas", {"title": "x", "content": "y"}, {}),
            ("a read", "read_channel", {"channel_id": self.ASK_CHANNEL}, {}),
            ("a draft", "send_message_draft", {"channel_id": self.SUMMARY_CHANNEL, "message": "hi"}, {}),
            ("summary to another channel", "schedule_message", {**self.schedule, "channel_id": self.ASK_CHANNEL}, {}),
            ("summary in a thread", "schedule_message", {**self.schedule, "thread_ts": "1761700000.000100"}, {}),
            ("changed summary text", "schedule_message", {**self.schedule, "message": "Whooper approved this PR."}, {}),
            ("another time", "schedule_message", {**self.schedule, "post_at": 1771700180}, {}),
            # Each allowed call with a variable it reads unset, one its run sets or a channel. set -u stops the
            # hook at one it does not check first: bash then exits 1, and bash 3.2 exits 0 when an EXIT trap
            # is set, and Claude Code lets the call through after either.
            ("eyes with WHOOPER_ASK_TS unset", "add_reaction", self.ASK, {"WHOOPER_ASK_TS": None}),
            ("eyes with no config", "add_reaction", self.ASK, no_config),
            ("the summary with WHOOPER_SUMMARY_FILE unset", "schedule_message", self.schedule,
             {"WHOOPER_SUMMARY_FILE": None}),
            ("the summary with WHOOPER_SUMMARY_POST_AT unset", "schedule_message", self.schedule,
             {"WHOOPER_SUMMARY_POST_AT": None}),
            ("the summary with no config", "schedule_message", self.schedule, no_config),
            # Each allowed call when a command in the hook fails.
            ("eyes when a command fails", "add_reaction", self.ASK, failing_jq),
            ("the summary when a command fails", "schedule_message", self.schedule, failing_jq),
        ]
        for bash in BASHES:
            for name, tool, tool_input, env in cases:
                with self.subTest(name, bash=str(bash)):
                    self.assertEqual(self.run_hook(bash, tool, tool_input, **env), 2)

    def test_second_summary_is_blocked(self):
        for bash in BASHES:
            with self.subTest(bash=str(bash)):
                self.assertEqual(self.run_hook(bash, "schedule_message", self.schedule), 0)
                self.assertEqual(self.run_hook(bash, "schedule_message", self.schedule), 2)


class LauncherHooksTest(unittest.TestCase):
    """The hooks the launchers set in the settings or config they give their CLI: agents/claude/summary and
    agents/claude/review in claude's settings, and agents/devin/review in devin's config. Each launcher runs
    from a Whooper whose root path has a space, as the Application Support folder Prompting Time installs it
    in has, and a single quote and a $, which a shell would read as quoting and a variable. The root links
    to the real agents, lib, and schemas, and its hooks are fakes that save their input. A fake claude and a
    fake devin save the settings and the config, and each test runs their hooks as the CLI runs a command
    hook."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.root = tmp / "Application Support" / "Sam's $whooper"
        self.root.mkdir(parents=True)
        for name in ["agents", "lib", "schemas"]:
            (self.root / name).symlink_to(ROOT / name)
        for name in ["slack-guard.sh", "tool-use-id.sh"]:
            fake(self.root / "hooks" / name, 'cat > "$0.input"')
        # Each fake saves what its launcher gave it, claude its --settings and devin its --config file, as
        # <fakes>/<CLI>.json.
        self.fakes = tmp / "bin"
        fake(self.fakes / "claude", "\n".join([
            "while [ $# -gt 0 ]; do",
            '  if [ "$1" = --settings ]; then printf %s "$2" > "${0%/*}/claude.json"; fi',
            "  shift",
            "done",
        ]))
        fake(self.fakes / "devin", "\n".join([
            "while [ $# -gt 0 ]; do",
            '  if [ "$1" = --config ]; then cp "$2" "${0%/*}/devin.json"; fi',
            "  shift",
            "done",
        ]))
        self.env = {**os.environ, "PATH": f"{self.fakes}:{os.environ['PATH']}", "XDG_STATE_HOME": str(tmp / "state"),
                    "XDG_CACHE_HOME": str(tmp / "cache")}

    def launch(self, cli, launcher, *args, **env):
        """Runs agents/<cli>/<launcher> in the root with args and env, and returns the settings or config it gave
        the CLI, whatever the launcher does once the CLI exits."""
        result = subprocess.run([str(self.root / "agents" / cli / launcher), *args], env={**self.env, **env},
                                capture_output=True, text=True, timeout=60)
        given = self.fakes / f"{cli}.json"
        self.assertTrue(given.exists(), result.stderr)
        return json.loads(given.read_text())

    def ran(self, cli, hook, payload, name):
        """Runs a command hook from the settings or config as the CLI does, with payload and a newline on stdin,
        and returns the input the fake hooks/<name> saved, or None when it did not run. Claude Code 2.1.314
        starts a hook with args by its path, with them, and runs one without through /bin/sh -c. Devin CLI
        3000.11.234 runs every hook through bash -c, with the first bash on PATH."""
        saved = self.root / "hooks" / f"{name}.input"
        saved.unlink(missing_ok=True)
        if cli == "devin":
            command = ["bash", "-c", hook["command"]]
        elif "args" in hook:
            command = [hook["command"], *hook["args"]]
        else:
            command = ["/bin/sh", "-c", hook["command"]]
        subprocess.run(command, input=f"{payload}\n", capture_output=True, text=True, timeout=60)
        return saved.read_text() if saved.exists() else None

    def test_the_summary_runs_slack_guard_and_blocks_the_call_when_slack_guard_fails(self):
        summary, run = Path(self.tmp.name) / "summary.txt", Path(self.tmp.name) / "run"
        summary.write_text("Whooper reviewed https://github.com/example-org/example-repo/pull/12 at cb54aa8.")
        run.mkdir()
        [group] = self.launch("claude", "summary", str(summary), str(run))["hooks"]["PreToolUse"]
        [hook] = group["hooks"]
        payload = json.dumps({"hook_event_name": "PreToolUse",
                              "tool_name": "mcp__claude_ai_Slack__slack_schedule_message",
                              "tool_input": {"channel_id": "C0000000002", "message": summary.read_text(),
                                             "post_at": 1761700180}})
        self.assertEqual(self.ran("claude", hook, payload, "slack-guard.sh"), f"{payload}\n")
        # Without it, Claude Code lets the call through when slack-guard cannot start, crashes, or times out.
        self.assertEqual(hook.get("onFailure"), "block")

    def test_a_claude_review_runs_tool_use_id_after_each_bash_call(self):
        prompt, run = Path(self.tmp.name) / "prompt.md", Path(self.tmp.name) / "run"
        prompt.write_text("Review this PR.")
        run.mkdir()
        hooks = self.launch("claude", "review", str(prompt), WHOOPER_RUN=str(run),
                            WHOOPER_CLAUDE_REVIEW_MODEL="review-model")["hooks"]
        for event in ["PostToolUse", "PostToolUseFailure"]:
            with self.subTest(event):
                [group] = hooks[event]
                [hook] = group["hooks"]
                payload = json.dumps({"hook_event_name": event, "tool_name": "Bash", "tool_use_id": "toolu_01",
                                      "tool_input": {"command": "go test ./internal/api"}})
                self.assertEqual(self.ran("claude", hook, payload, "tool-use-id.sh"), f"{payload}\n")

    def test_a_devin_review_runs_tool_use_id_after_each_exec_call(self):
        prompt, run = Path(self.tmp.name) / "prompt.md", Path(self.tmp.name) / "run"
        prompt.write_text("Review this PR.")
        run.mkdir()
        # The home has no Devin config, so the launcher adds its hook to an empty config, not the user's.
        config = self.launch("devin", "review", str(prompt), HOME=str(Path(self.tmp.name) / "home"),
                             WHOOPER_RUN=str(run), WHOOPER_DEVIN_REVIEW_MODEL="review-model")
        [group] = config["hooks"]["PostToolUse"]
        [hook] = group["hooks"]
        payload = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "exec", "tool_use_id": "call_1",
                              "tool_input": {"command": "go test ./internal/api"}})
        self.assertEqual(self.ran("devin", hook, payload, "tool-use-id.sh"), f"{payload}\n")


class RunTest(unittest.TestCase):
    """bin/whooper-run, whooper-round, whooper-review, whooper-ask, whooper-status, whooper-pause, and
    whooper-resume on a copy of the test config and a throwaway state folder, with gh, slackdump, and
    the agent CLIs replaced by commands that fail unless a test says otherwise, so even a broken check
    cannot reach GitHub, Slack, or a model. Each command runs in a process group of its own, which the
    test stops whole when it ends."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.fakes = tmp / "bin"
        for name in ["gh", "slackdump", "devin"]:
            self.script(self.fakes / name, "exit 1")
        self.claude_script("exit 1")
        # Rounds may start at any time on any day, a minute apart. notify.sh notes each review's
        # notification in notifications.log.
        config = tmp / "config" / "config.toml"
        config.parent.mkdir()
        config.write_text(CONFIG + '\n[schedule]\ninterval_minutes = 1\nstart = "00:00"\nend = "23:59"\n'
                          'days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]\nnotify = "notify.sh"\n')
        self.notify, self.notifications = config.parent / "notify.sh", tmp / "notifications.log"
        self.script(self.notify, f"printf '%s | %s\\n' \"$1\" \"$2\" >> '{self.notifications}'")
        self.env = {**os.environ, "PATH": f"{self.fakes}:{os.environ['PATH']}", "WHOOPER_CONFIG": str(config),
                    "XDG_STATE_HOME": self.tmp.name, "XDG_CACHE_HOME": str(tmp / "cache")}
        self.state = tmp / "whooper"
        self.lock = self.state / "run.lock"

    def script(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def claude_script(self, body):
        """Writes the fake claude, which answers --version as Claude Code 2.1.314 does and runs body, a few lines
        of sh, for any other call."""
        self.script(self.fakes / "claude", f"{CLAUDE_VERSION}\n{body}")

    def start(self, path, out=subprocess.DEVNULL):
        process = subprocess.Popen([str(path)], env=self.env, stdout=out, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        self.addCleanup(self.stop, process)
        return process

    def stop(self, process):
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=30)

    def command(self, name, *args, env=None):
        """Runs bin/<name> with args to its end."""
        return self.run_script(ROOT / "bin" / name, *args, env=env)

    def run_script(self, path, *args, env=None):
        """Runs the script at path with args to its end."""
        process = subprocess.Popen([str(path), *args], env={**self.env, **(env or {})},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(self.stop, process)
        out, err = process.communicate(timeout=60)
        return subprocess.CompletedProcess(process.args, process.returncode, out, err)

    def status(self):
        """whooper-status --json, with times in Los Angeles."""
        result = self.command("whooper-status", "--json", env={"TZ": "America/Los_Angeles"})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def holder(self, name):
        """Starts a script named name that waits, standing in for a running copy of that command, and
        returns its process ID."""
        script = Path(self.tmp.name) / "holders" / name
        self.script(script, "sleep 30")
        return self.start(script).pid

    def hold(self, lock, pid):
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{pid}\n")

    def read(self, path):
        """The file's text, or None while it does not exist."""
        try:
            return path.read_text()
        except FileNotFoundError:
            return None

    def wait_for(self, condition, what):
        # A test that starts in the day's last minute waits until 00:00 for its first round.
        deadline = monotonic() + 90
        while not condition():
            if monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            sleep(0.1)

    def lock_takers(self, item):
        """Each command that takes a lock: its name, the arguments that bring it to the lock, with item as
        whooper-review's work item, the lock, and the refusal it prints while the process whose ID fills
        the braces holds the lock."""
        round_lock, review_lock = self.state / "round.lock", self.state / "locks" / "sample-org-plain-repo-12"
        return [("whooper-run", [], self.lock,
                 f"whooper-run: process {{}} is already running Whooper; see {self.lock}/pid"),
                ("whooper-round", [], round_lock,
                 f"whooper-round: a round is already running, in process {{}}; see {round_lock}/pid"),
                ("whooper-review", [str(item), "--dry-run"], review_lock,
                 "whooper-review: process {} is already reviewing sample-org/plain-repo#12")]

    def start_command(self, name, *args, env):
        """Starts bin/<name> with args in the environment env, keeping its error output for the test."""
        process = subprocess.Popen([str(ROOT / "bin" / name), *args], env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        # Cleanups run last first, so the command stops before the pipe of its error output closes. A test that reads
        # the pipe with communicate has closed it already; one that never reads it would otherwise leave it open.
        self.addCleanup(process.stderr.close)
        self.addCleanup(self.stop, process)
        return process

    def gh_waits(self):
        """Has gh stand in for a GitHub fetch that lasts until the test stops it, so a command that comes to it after
        taking its lock holds the lock until then: gh notes that it started, in gh-<process ID> in the test's folder for
        itself and each process it descends from, and waits (stop_in_gh). whooper-run's round runs in a process group of
        its own, so whooper-run is the round's parent but not in its group."""
        self.script(self.fakes / "gh", "\n".join([
            "pid=$$",
            f'while [ "$pid" -gt 1 ]; do touch "{self.tmp.name}/gh-$pid"; pid="$(ps -o ppid= -p "$pid" | tr -d " ")"; done',
            "exec sleep 600",
        ]))

    def stop_in_gh(self, process):
        """Stops process, a command started in a process group of its own, once it waits in gh (gh_waits), or has
        ended. A TERM to the group misses a process that is being forked at that moment, and each shell that waits
        for that process runs its TERM trap only once the process ends, so a command stopped while it starts one may
        go on through gh; one that waits in gh starts nothing until the TERM comes."""
        self.wait_for(lambda: (Path(self.tmp.name) / f"gh-{process.pid}").exists() or process.poll() is not None,
                      "the command to wait in gh")
        self.stop(process)

    def two_copies(self, name, args, env, lock, paused):
        """Starts two copies of bin/<name> with args in the environment env. A fake command stops the first
        as it takes lock: it makes the file paused/stopped and waits for paused/go. Once the second copy
        waits for the first, or has taken the lock, the first goes on, and once the second stops, or
        holds the lock, the test stops both, the first once it waits in gh (gh_waits). Returns the first
        copy's process ID, the second's exit code and error output, and the process ID in lock by then."""
        def waits(process):
            # Each copy takes its lock while it holds flock on <state>/locks, which lib/python takes with
            # Python's fcntl.flock, so a copy that waits for the first runs fcntl.flock in its process group.
            return subprocess.run(["pgrep", "-g", str(process.pid), "-f", "fcntl.flock"],
                                  stdout=subprocess.DEVNULL).returncode == 0

        first = self.start_command(name, *args, env=env)
        self.wait_for((paused / "stopped").exists, f"the first {name} to stop as it takes the lock")
        second = self.start_command(name, *args, env=env)
        self.wait_for(lambda: waits(second) or self.read(lock / "pid") == f"{second.pid}\n",
                      f"the second {name} to wait for the first or take the lock")
        (paused / "go").touch()
        self.wait_for(lambda: second.poll() is not None or self.read(lock / "pid") == f"{second.pid}\n",
                      f"the second {name} to stop or take the lock")
        holder = self.read(lock / "pid")
        self.stop_in_gh(first)
        self.stop(second)
        first.communicate()
        return first.pid, second.returncode, second.communicate()[1], holder

    def fake_one_review_request(self):
        """Has gh report one review request, for example-org/example-repo#12, as fake_review_requests
        does. The review then fails, since the cache holds no clone of the repo."""
        self.fake_review_requests(pr(number=12, requested=[LOGIN]))

    def fake_review_requests(self, *prs):
        """Has gh report a review request for each of prs, PRs as queries/pr.graphql returns them, and
        fail any other call, slackdump an ask channel with no posts, and the Claude poll run no asks."""
        for p in prs:
            path = p["url"].removeprefix("https://github.com/").replace("/", "-")
            (self.fakes / f"{path}.json").write_text(json.dumps(p))
            org = p["repository"]["nameWithOwner"].split("/")[0]
            with (self.fakes / f"requested-{org}").open("a") as requested:
                requested.write(p["url"] + "\n")
        self.script(self.fakes / "gh", "\n".join([
            'case "$*" in',
            '  "search prs user-review-requested:sam-example --owner "*)',
            '    all="$*"; org="${all#*--owner }"; cat "${0%/*}/requested-${org%% *}" 2>/dev/null; exit 0 ;;',
            '  "search prs "*) ;;',
            '  "api graphql "*)',
            '    for arg; do case "$arg" in url=https://github.com/*)',
            '      cat "${0%/*}/$(echo "${arg#url=https://github.com/}" | tr / -).json"; exit ;; esac; done',
            "    exit 1 ;;",
            "  *) exit 1 ;;",
            "esac",
        ]))
        # slackdump dumps the channel, its last argument, to <channel>.json in the folder after -o.
        self.script(self.fakes / "slackdump", "\n".join([
            'while [ $# -gt 1 ]; do [ "$1" = -o ] && out="$2"; shift; done',
            """mkdir -p "$out" && echo '{"messages": []}' > "$out/$1.json\"""",
        ]))
        # The poll's answer is the result at the end of Claude's stream-json output.
        self.claude_script(
            """echo '{"type": "result", "subtype": "success", "structured_output": {"asks": [], "not_asks": []}}'""")

    def git_env(self):
        """The environment for git and the review, with Sam as each commit's author. Its home has no
        skills, and git reads only home/.gitconfig, which does not exist until a test writes it, so
        neither the user's git config nor the system's reaches a test."""
        home = Path(self.tmp.name) / "home"
        (home / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
        return {**self.env, "HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "Sam", "GIT_AUTHOR_EMAIL": "sam@example.com",
                "GIT_COMMITTER_NAME": "Sam", "GIT_COMMITTER_EMAIL": "sam@example.com"}

    def pull_request(self, app=("one\n", "one\ntwo\n"), more=None):
        """Makes PR 12 of sample-org/plain-repo in a local origin, the cache's clone of it, and its work
        item, and returns the environment for git and the review (git_env), the work item's path, and
        the PR's merge base. app is app.txt at the merge base and at the head, and more maps each other
        file the PR changes to its text at the merge base and at the head, None where it is missing."""
        tmp = Path(self.tmp.name)
        origin = tmp / "origin"
        env = self.git_env()

        def git(*args):
            return subprocess.run(["git", *args], cwd=origin, env=env, check=True, capture_output=True,
                                  text=True).stdout.strip()

        def write(files):
            for name, content in files.items():
                if content is None:
                    (origin / name).unlink(missing_ok=True)
                else:
                    (origin / name).parent.mkdir(parents=True, exist_ok=True)
                    (origin / name).write_text(content)

        # PR 12 branches off main, which moves on after it, and adds the submodule vendor/lib. The
        # repo's .gitattributes marks generated.txt -diff, which git reads from the worktree it runs in.
        origin.mkdir()
        git("init", "--quiet", "--initial-branch=main")
        write({"app.txt": app[0], "generated.txt": "g1\n", ".gitattributes": "generated.txt -diff\n",
               **{name: texts[0] for name, texts in (more or {}).items()}})
        git("add", ".")
        git("commit", "--quiet", "-m", "base")
        merge_base = git("rev-parse", "HEAD")
        git("checkout", "--quiet", "-b", "pr-12")
        write({"app.txt": app[1], "generated.txt": "g2\n", **{name: texts[1] for name, texts in (more or {}).items()}})
        git("add", "--all")
        git("update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/lib")
        git("commit", "--quiet", "-m", "the PR")
        head = git("rev-parse", "HEAD")
        git("checkout", "--quiet", "main")
        (origin / "app.txt").write_text("one\nlater on main\n")
        git("commit", "--quiet", "-am", "later on main")
        git("clone", "--quiet", "--no-checkout", str(origin),
            str(tmp / "cache" / "whooper" / "repos" / "sample-org-plain-repo"))
        item = tmp / "item.json"
        item.write_text(json.dumps({"kind": "first_review", "repo": "sample-org/plain-repo", "pr": 12, "head_sha": head,
                                    "base_ref": "main", "source": "github_request", "slack": None, "reason": "r"}))
        return env, item, merge_base

    def overview_of_the_pr(self, **overrides):
        """An overview of pull_request's PR 12 that keeps every rule of prompts/review.md's overview: the
        one hunk of app.txt and of the submodule vendor/lib in a layer each, and generated.txt, which
        .gitattributes shows as binary, in none."""
        out = {
            "summary": "Adds a second line to app.txt and vendors lib as a submodule.",
            "verdict_reason": "Nothing needs the author: the change only adds a line and a submodule.",
            "before_after": [{"label": "Readers of app.txt", "before": "app.txt holds one.", "after": "It holds one and two."}],
            "places": [{"id": "p1", "title": "The new line", "reason": "Nothing reads the second line yet.",
                        "evidence": {"kind": "searched", "detail": "grep -rn app.txt"},
                        "path": "app.txt", "start_line": 2, "end_line": 2}],
            "layers": [{"id": "l1", "title": "app.txt", "intent": "One more line.", "read_hint": "read",
                        "ranges": [{"path": "app.txt", "start_line": 1, "end_line": 2}]},
                       {"id": "l2", "title": "The submodule", "intent": "Vendors lib.", "read_hint": "skip",
                        "ranges": [{"path": "vendor/lib", "start_line": 1, "end_line": 1}]}],
            "flow": None,
            "checked": ["Read the diff."],
            "not_checked": ["Nothing runs app.txt, so nothing ran."],
        }
        out.update(overrides)
        return out

    def setup_script(self, body):
        """Names setup.sh, which runs body, a few lines of sh, as sample-org/plain-repo's setup script in
        the config, and returns its path."""
        config = Path(self.env["WHOOPER_CONFIG"])
        table = '[repos."sample-org/plain-repo"]\n'
        if f'{table}setup = "setup.sh"\n' not in config.read_text():
            config.write_text(config.read_text().replace(table, f'{table}setup = "setup.sh"\n'))
        self.script(config.parent / "setup.sh", body)
        return config.parent / "setup.sh"

    def teardown_script(self, body):
        """Names teardown.sh, which runs body, a few lines of sh, as sample-org/plain-repo's teardown script in the
        config, as setup_script names setup.sh, and returns its path."""
        config = Path(self.env["WHOOPER_CONFIG"])
        table = '[repos."sample-org/plain-repo"]\n'
        if f'{table}teardown = "teardown.sh"\n' not in config.read_text():
            config.write_text(config.read_text().replace(table, f'{table}teardown = "teardown.sh"\n'))
        self.script(config.parent / "teardown.sh", body)
        return config.parent / "teardown.sh"

    def kill_groups(self, pids):
        """Sends SIGKILL to each process in pids that is still there and to its process group, for a test that leaves
        processes behind when a stop misses them."""
        for pid in pids:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)

    def fake_claude(self, review):
        """Has claude run review, a line of sh, in a review run, which alone passes --json-schema, and
        answer an ask run with "It stopped."."""
        self.claude_script("\n".join([
            'case "$*" in',
            f"  *--json-schema*) {review} ;;",
            """  *) echo '{"type": "result", "subtype": "success", "result": "It stopped."}' ;;""",
            "esac",
        ]))

    def test_refuses_to_start_while_another_whooper_run_holds_the_lock(self):
        # A script named whooper-run stands in for a copy that is already polling, and paused.
        holder = self.holder("whooper-run")
        self.hold(self.lock, holder)
        (self.state / "paused").touch()
        result = self.command("whooper-run")
        self.assertEqual((result.returncode, (self.lock / "pid").read_text()), (1, f"{holder}\n"))
        self.assertIn(f"process {holder} is already running", result.stderr)
        # Only the copy that runs ends the pause when it starts.
        self.assertTrue((self.state / "paused").exists())

    def test_takes_over_a_lock_whose_process_is_some_other_program(self):
        # After a kill -9 or a restart, the lock's process ID can belong to anything, here this test.
        self.hold(self.lock, os.getpid())
        process = self.start(ROOT / "bin" / "whooper-run")
        self.wait_for(lambda: self.read(self.lock / "pid") == f"{process.pid}\n", "whooper-run to take the lock")
        process.terminate()
        process.wait(timeout=10)
        self.assertFalse(self.lock.exists())

    def test_a_signal_the_moment_a_command_makes_its_lock_leaves_no_lock_behind(self):
        # mkdir sends TERM to the command's whole process group, as Ctrl-C or kill -- -<pid> does, the
        # moment it makes the lock, before the command has written its process ID there.
        env, item, _ = self.pull_request()
        mkdir = shutil.which("mkdir")
        for name, args, lock in [("whooper-run", [], self.lock), ("whooper-round", [], self.state / "round.lock"),
                                 ("whooper-review", [str(item), "--dry-run"],
                                  self.state / "locks" / "sample-org-plain-repo-12")]:
            with self.subTest(name):
                self.script(self.fakes / "mkdir", f'{mkdir} "$@" || exit\nif [ "$1" = "{lock}" ]; then kill -TERM 0; fi')
                result = self.command(name, *args, env=env)
                self.assertEqual((result.returncode, lock.exists()), (130, False), result.stderr)

    def test_a_copy_that_comes_while_another_makes_the_lock_waits_for_it_and_refuses(self):
        # The first copy of each command stops in the mkdir that makes the lock, before it writes its process
        # ID there, as if it lost the processor there, until the second copy has come to the lock. The first
        # then waits in its GitHub fetch, or its round's, so it holds the lock until the test stops it.
        env, item, _ = self.pull_request()
        mkdir = shutil.which("mkdir")
        self.gh_waits()
        for name, args, lock, refusal in self.lock_takers(item):
            with self.subTest(name):
                paused = Path(self.tmp.name) / f"{name}.paused"
                self.script(self.fakes / "mkdir", "\n".join([
                    f'{mkdir} "$@" || exit',
                    f'if [ "$1" = "{lock}" ] && {mkdir} "{paused}" 2>/dev/null; then',
                    f'  touch "{paused}/stopped"',
                    f'  until [ -e "{paused}/go" ]; do sleep 0.1; done',
                    "fi",
                ]))
                first, returncode, stderr, holder = self.two_copies(name, args, env, lock, paused)
                # The second copy waited until the first had written its process ID, then refused, and the
                # lock names the first.
                self.assertEqual((returncode, stderr, holder), (1, f"{refusal.format(first)}\n", f"{first}\n"))

    def test_status_reads_a_lock_another_command_is_making_once_it_is_made(self):
        # whooper-run and whooper-round each stop for a second in the mkdir that makes their lock, before they write
        # their process ID there, as if they lost the processor there, while whooper-status reads the status. Then
        # whooper-run's first round, and whooper-round, wait in their GitHub fetch, so each holds its lock until the
        # test stops it.
        mkdir = shutil.which("mkdir")
        self.gh_waits()
        for name, lock in [("whooper-run", self.lock), ("whooper-round", self.state / "round.lock")]:
            with self.subTest(name):
                paused = Path(self.tmp.name) / f"{name}.paused"
                self.script(self.fakes / "mkdir", "\n".join([
                    f'{mkdir} "$@" || exit',
                    f'if [ "$1" = "{lock}" ] && {mkdir} "{paused}" 2>/dev/null; then',
                    f'  touch "{paused}/stopped"',
                    "  sleep 1",
                    "fi",
                ]))
                process = self.start_command(name, env=self.env)
                self.wait_for((paused / "stopped").exists, f"{name} to stop as it makes its lock")
                status = self.status()
                self.stop_in_gh(process)
                if name == "whooper-run":
                    self.assertEqual(status["loop"], {"running": True, "pid": process.pid})
                else:
                    self.assertEqual((status["round"]["running"], status["round"]["reviewing"]), (True, None))
                    self.assertIsNotNone(status["round"]["started_at"])

    def test_of_two_copies_that_find_the_same_stale_lock_one_takes_it_over_and_the_other_refuses(self):
        # Two copies of each command come to a lock whose process is gone. The first stops in the cat that
        # reads the lock's process ID, as if it lost the processor there, until the second has come to the
        # lock too. The first then takes the lock over and waits in its GitHub fetch, or its round's, so it
        # holds the lock until the test stops it.
        env, item, _ = self.pull_request()
        gone = subprocess.Popen(["true"])
        gone.wait()
        cat = shutil.which("cat")
        self.gh_waits()
        for name, args, lock, refusal in self.lock_takers(item):
            with self.subTest(name):
                paused = Path(self.tmp.name) / f"{name}.paused"
                self.hold(lock, gone.pid)
                self.script(self.fakes / "cat", "\n".join([
                    f'if [ "$*" = "{lock}/pid" ] && mkdir "{paused}" 2>/dev/null; then',
                    f'  {cat} "$@" > "{paused}/pid"',
                    f'  touch "{paused}/stopped"',
                    f'  until [ -e "{paused}/go" ]; do sleep 0.1; done',
                    f'  exec {cat} "{paused}/pid"',
                    "fi",
                    f'exec {cat} "$@"',
                ]))
                first, returncode, stderr, holder = self.two_copies(name, args, env, lock, paused)
                # The second copy waited until the first had taken the lock over, then refused, and the lock
                # names the first.
                self.assertEqual((returncode, stderr, holder), (1, f"{refusal.format(first)}\n", f"{first}\n"))

    def test_takes_over_a_lock_without_a_process_id_that_no_copy_is_making(self):
        # A copy killed with kill -9 after the mkdir that makes its lock, before it wrote its process ID
        # there, leaves the lock without one, while the flock on <state>/locks that it held as it made the
        # lock ends with it. Each command takes such a lock over at once, then waits in its GitHub fetch,
        # or its round's, so it holds the lock until the test stops it.
        env, item, _ = self.pull_request()
        self.gh_waits()
        for name, args, lock, _ in self.lock_takers(item):
            with self.subTest(name):
                lock.mkdir(parents=True)
                process = self.start_command(name, *args, env=env)
                self.wait_for(lambda: process.poll() is not None or self.read(lock / "pid") == f"{process.pid}\n",
                              f"{name} to stop or take the lock")
                holder = self.read(lock / "pid")
                self.stop_in_gh(process)
                self.assertEqual(holder, f"{process.pid}\n", process.communicate()[1])

    def test_a_review_takes_over_a_pr_lock_whose_process_is_some_other_program(self):
        # After a kill -9 or a restart, the lock's process ID can belong to anything, here this test. The
        # review takes the lock over, then waits in its GitHub fetch, so it holds the lock until the test
        # stops it.
        env, item, _ = self.pull_request()
        self.gh_waits()
        lock = self.state / "locks" / "sample-org-plain-repo-12"
        self.hold(lock, os.getpid())
        review = self.start_command("whooper-review", str(item), "--dry-run", env=env)
        self.wait_for(lambda: review.poll() is not None or self.read(lock / "pid") == f"{review.pid}\n",
                      "whooper-review to stop or take the lock")
        holder = self.read(lock / "pid")
        self.stop_in_gh(review)
        self.assertEqual(holder, f"{review.pid}\n", review.communicate()[1])

    def test_commands_that_take_a_run_id_refuse_a_path_outside_runs(self):
        # A run ID names a folder right under runs/, so a path such as ../x reads nothing outside it, even
        # a folder that holds what a run does.
        runs = self.state / "runs"
        run, outside = runs / "20310727T010246Z-12", self.state / "elsewhere"
        for folder in (run, outside):
            folder.mkdir(parents=True)
            (folder / "run.json").write_text(json.dumps({"repo": "sample-org/plain-repo", "pr": 12, "head_sha": "1" * 40,
                                                         "agent": "claude", "run_id": run.name}))
            (folder / "findings.json").write_text(json.dumps({"status": "reviewed", "skip_reason": "", "findings": [],
                                                              "approval": {"verdict": "look_first", "reason": "r"}}))
        for name, extra in [("whooper-draft", []), ("whooper-submit", []), ("whooper-post", ["--print"]),
                            ("whooper-ask", ["Why?"]), ("whooper-retry", [])]:
            for run_id in [f"../runs/{run.name}", "../elsewhere", ".."]:
                with self.subTest(name, run_id=run_id):
                    result = self.command(name, run_id, *extra)
                    self.assertEqual((result.returncode, result.stdout, result.stderr),
                                     (1, "", f"{name}: no run {run_id} in {runs}\n"))

    def test_a_signal_the_moment_a_command_makes_its_temporary_folder_leaves_no_folder_behind(self):
        # mktemp makes the folder, inside the test's folder rather than the user's temporary folder, then
        # sends TERM to the command's whole process group, as Ctrl-C or kill -- -<pid> does, before it
        # prints the folder's name. Each command stops there, so the setup is only what each needs to
        # reach its mktemp.
        mktemp = shutil.which("mktemp")
        run = self.state / "runs" / "20310727T010246Z-12"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"repo": "sample-org/plain-repo", "pr": 12, "agent": "claude"}))
        (run / "post.json").write_text("{}")
        item, prompt = Path(self.tmp.name) / "item.json", Path(self.tmp.name) / "prompt.md"
        item.write_text(json.dumps({"kind": "first_review", "repo": "sample-org/plain-repo", "pr": 12,
                                    "head_sha": "1" * 40, "base_ref": "main", "source": "github_request",
                                    "slack": None, "reason": "r"}))
        prompt.write_text("Why?\n")
        launcher = [str(prompt), str(Path(self.tmp.name) / "answer"), str(run / "record")]
        for path, args in [("bin/whooper-review", [str(item), "--dry-run"]), ("bin/whooper-poll", []),
                           ("bin/whooper-ask", [run.name, "Why?"]), ("bin/whooper-summary", [run.name]),
                           ("bin/whooper-post", [run.name, "--print"]), ("bin/whooper-submit", [run.name]),
                           ("agents/claude/ask", launcher), ("agents/devin/ask", launcher),
                           ("agents/devin/poll", launcher)]:
            with self.subTest(path):
                temporary = Path(self.tmp.name) / "temporary" / path.replace("/", "-")
                temporary.mkdir(parents=True)
                self.script(self.fakes / "mktemp", "\n".join([
                    f'folder="$({mktemp} -d "{temporary}/folder.XXXXXX")" || exit', "kill -TERM 0", 'echo "$folder"']))
                result = self.run_script(ROOT / path, *args, env=self.git_env())
                self.assertEqual((result.returncode, list(temporary.iterdir())), (130, []), result.stderr)

    def test_a_background_command_that_cannot_expand_its_words_leaves_the_work_folder_to_the_command(self):
        # bash runs a script's EXIT trap in the process it forks for a background command too, when that process cannot
        # expand the command's words, as `sleep "$unset" &` under set -u, in bash 3.2 and 5 alike, and $$ there is still
        # the script's process ID. None of these commands' own background commands fails so, so BASH_ENV, which bash
        # runs before a script's first line, gives each a function named after a command it runs in its own process
        # once it made its work folder: whooper-poll's slackdump, whooper-ask's mkdir, and whooper-review's gh, which
        # comes once it holds the PR's lock and has its worktrees. The function starts such a background command, waits
        # for it, notes what is left of the command's work folder, lock, and head worktree, and ends the command. mktemp
        # makes the work folder inside the test's folder, so the test sees it.
        env, item, _ = self.pull_request()
        mktemp, tmp = shutil.which("mktemp"), Path(self.tmp.name)
        run = self.state / "runs" / "20310727T010246Z-12"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"repo": "sample-org/plain-repo", "pr": 12, "agent": "claude"}))
        lock = self.state / "locks" / "sample-org-plain-repo-12"
        for path, args, command, kept in [("bin/whooper-poll", [], "slackdump", "folder\n"),
                                          ("bin/whooper-ask", [run.name, "Why?"], "mkdir", "folder\n"),
                                          ("bin/whooper-review", [str(item), "--dry-run"], "gh", "folder\nlock\nhead\n")]:
            with self.subTest(path):
                temporary, left = tmp / "temporary" / path.replace("/", "-"), tmp / f"{command}.left"
                temporary.mkdir(parents=True)
                self.script(self.fakes / "mktemp", f'exec {mktemp} -d "{temporary}/folder.XXXXXX"')
                bash_env = tmp / f"{command}.bash"
                bash_env.write_text("\n".join([
                    f"{command}() {{",
                    '  sleep "$whooper_test_unset" &',
                    '  wait "$!" || true',
                    f'  {{ [ -n "$(ls -A "{temporary}")" ] && echo folder; [ -d "{lock}" ] && echo lock;',
                    f'    ls -d "{self.state}"/runs/*/head > /dev/null 2>&1 && echo head; }} > "{left}" || true',
                    "  exit 7",
                    "}",
                ]) + "\n")
                result = self.run_script(ROOT / path, *args, env={**env, "BASH_ENV": str(bash_env)})
                # The command's own process cleaned up as it ended.
                self.assertEqual((result.returncode, left.read_text(), list(temporary.iterdir()), lock.exists()),
                                 (7, kept, [], False), result.stderr)

    def test_a_round_refuses_to_start_while_another_round_runs(self):
        holder = self.holder("whooper-round")
        self.hold(self.state / "round.lock", holder)
        result = self.command("whooper-round")
        self.assertEqual((result.returncode, (self.state / "round.lock" / "pid").read_text()), (1, f"{holder}\n"))
        self.assertIn("a round is already running", result.stderr)
        self.assertFalse((self.state / "last-round.json").exists())

    def test_a_review_of_a_pr_another_live_review_holds_stops_and_leaves_that_reviews_lock(self):
        env, item, _ = self.pull_request()
        holder = self.holder("whooper-review")
        lock = self.state / "locks" / "sample-org-plain-repo-12"
        self.hold(lock, holder)
        result = self.command("whooper-review", str(item), "--dry-run", env=env)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", f"whooper-review: process {holder} is already reviewing sample-org/plain-repo#12\n"))
        # The other review's lock stays, and this review made no run and counted no failure.
        self.assertEqual(((lock / "pid").read_text(), [p.name for p in self.state.iterdir()]), (f"{holder}\n", ["locks"]))

    def test_the_fetch_looks_up_the_recheck_lists_prs_its_searches_miss_and_select_drops_merged_and_closed_ones(self):
        # The searches find PR 12, whose review request stands. The recheck list holds PRs 12 to 15, and GitHub
        # reports PR 13 merged, PR 14 closed, and PR 15, which a Slack ask brought in, open.
        self.fake_review_requests(pr(number=12, requested=[LOGIN]))
        for number, state in [(13, "MERGED"), (14, "CLOSED"), (15, "OPEN")]:
            (self.fakes / f"example-org-example-repo-pull-{number}.json").write_text(
                json.dumps(pr(number=number, state=state)))
        rechecks = self.state / "rechecks"
        rechecks.mkdir(parents=True)
        listed = {number: work_item(number=number) for number in (12, 13, 14, 15)}
        for number, item in listed.items():
            (rechecks / f"example-org-example-repo-{number}.json").write_text(json.dumps(item))
        fetch = self.command("whooper-fetch")
        self.assertEqual(fetch.returncode, 0, fetch.stderr)
        poll = self.state / "poll"
        self.assertEqual([p["number"] for p in json.loads((poll / "github.json").read_text())], [12])
        # The searches list open PRs only, so only the PRs they did not find are looked up.
        self.assertEqual([(p["number"], p["state"]) for p in json.loads((poll / "recheck-prs.json").read_text())],
                         [(13, "MERGED"), (14, "CLOSED"), (15, "OPEN")])
        (poll / "20310727T020000Z.json").write_text(json.dumps({"items": []}))
        select = self.command("whooper-select")
        self.assertEqual([json.loads(line) for line in select.stdout.splitlines()], [listed[12], listed[15]],
                         select.stderr)
        self.assertEqual(select.stderr,
                         "whooper-select: dropped example-org/example-repo#13 first_review: merged on GitHub\n"
                         "whooper-select: dropped example-org/example-repo#14 first_review: closed on GitHub\n")
        self.assertEqual(sorted(path.name for path in rechecks.iterdir()),
                         ["example-org-example-repo-12.json", "example-org-example-repo-15.json"])
        # With the list empty, the fetch looks up nothing.
        for path in rechecks.iterdir():
            path.unlink()
        self.assertEqual(self.command("whooper-fetch").returncode, 0)
        self.assertEqual(json.loads((poll / "recheck-prs.json").read_text()), [])

    def test_a_recheck_list_pr_the_fetch_cannot_look_up_stays_listed_and_fails_no_round(self):
        # The searches find PR 12. The recheck list holds PRs 13 and 16: GitHub reports PR 13 merged, and the
        # lookup of PR 16 fails, as for a PR in an org the token may no longer read.
        self.fake_review_requests(pr(number=12, requested=[LOGIN]))
        (self.fakes / "example-org-example-repo-pull-13.json").write_text(json.dumps(pr(number=13, state="MERGED")))
        rechecks = self.state / "rechecks"
        rechecks.mkdir(parents=True)
        listed = {number: work_item(number=number) for number in (13, 16)}
        for number, item in listed.items():
            (rechecks / f"example-org-example-repo-{number}.json").write_text(json.dumps(item))
        fetch = self.command("whooper-fetch")
        # gh's own error comes first.
        self.assertEqual(fetch.returncode, 0, fetch.stderr)
        self.assertTrue(fetch.stderr.endswith(
            "whooper-fetch: could not look up https://github.com/example-org/example-repo/pull/16 on the recheck"
            " list, so it stays listed\n"), fetch.stderr)
        poll = self.state / "poll"
        self.assertEqual([p["number"] for p in json.loads((poll / "github.json").read_text())], [12])
        self.assertEqual([(p["number"], p["state"]) for p in json.loads((poll / "recheck-prs.json").read_text())],
                         [(13, "MERGED")])
        # select drops the merged PR and keeps PR 16, whose state nobody knows, for a review.
        (poll / "20310727T020000Z.json").write_text(json.dumps({"items": []}))
        select = self.command("whooper-select")
        self.assertEqual([json.loads(line) for line in select.stdout.splitlines()], [listed[16]], select.stderr)
        self.assertEqual(sorted(path.name for path in rechecks.iterdir()), ["example-org-example-repo-16.json"])

    def test_the_fetch_reads_every_pr_its_searches_find_up_to_the_1000_github_returns(self):
        # Open PRs that request Sam's review in example-org. gh returns at most --limit of a search's results, which
        # it pages through, and the fake logs each PR the fetch then reads.
        requested, read = self.fakes / "requested", self.fakes / "read"
        self.script(self.fakes / "gh", "\n".join([
            'case "$*" in',
            '  "search prs user-review-requested:sam-example --owner example-org "*)',
            '    limit=30 previous=""',
            '    for arg; do [ "$previous" = --limit ] && limit="$arg"; previous="$arg"; done',
            '    head -n "$limit" "${0%/*}/requested" ;;',
            '  "search prs "*) ;;',
            '  "api graphql "*) for arg; do case "$arg" in url=*) echo "${arg#url=}" >> "${0%/*}/read" ;; esac; done ;;',
            "  *) exit 1 ;;",
            "esac",
        ]))
        urls = [f"https://github.com/example-org/example-repo/pull/{number}" for number in range(1, 1201)]
        requested.write_text("".join(f"{url}\n" for url in urls[:150]))
        fetch = self.command("whooper-fetch")
        self.assertEqual((fetch.returncode, fetch.stderr), (0, ""))
        self.assertEqual(sorted(read.read_text().splitlines()), sorted(urls[:150]))
        # 1,000 is the most GitHub returns for one search, so a search that reaches it says it may have left PRs out.
        requested.write_text("".join(f"{url}\n" for url in urls))
        read.unlink()
        fetch = self.command("whooper-fetch")
        self.assertEqual((fetch.returncode, fetch.stderr), (
            0, "whooper-fetch: the search for PRs that request sam-example's review in example-org found 1,000 PRs, the"
               " most GitHub returns for one search, so it may have left some out\n"))
        self.assertEqual(sorted(read.read_text().splitlines()), sorted(urls[:1000]))

    def test_with_a_job_on_claude_each_command_that_runs_agents_refuses_claude_code_before_2_1_295(self):
        # claude --version answers as Claude Code 2.1.294 does, and the fake logs any other call.
        log = self.fakes / "claude.log"
        self.script(self.fakes / "claude", 'if [ "$1" = --version ]; then echo "2.1.294 (Claude Code)"; exit; fi\n'
                                           'echo "$*" >> "${0%/*}/claude.log"\nexit 1')
        message = CLAUDE_TOO_OLD.format("2.1.294 (Claude Code)")
        run = self.state / "runs" / "20310727T010246Z-12"
        run.mkdir(parents=True)
        (run / "failure.txt").write_text("the review stopped at an unexpected error\n")
        for command, args in [("whooper-poll", []), ("whooper-review", [str(run / "item.json"), "--dry-run"]),
                              ("whooper-summary", [run.name]), ("whooper-ask", [run.name, "Why did it stop?"])]:
            with self.subTest(command):
                result = self.command(command, *args)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", message))
        # A round records the refusal as a failed poll, which Prompting Time and the page show.
        round_ = self.command("whooper-round")
        self.assertEqual((round_.returncode, round_.stderr), (1, message))
        status = self.status()
        self.assertEqual((status["last_round"]["result"], status["rounds"][0]["reason"]),
                         ("poll_failed", "The Claude Code version check failed"))
        self.assertFalse(log.exists())

    def test_a_round_reviews_each_selected_item_and_reports_how_it_ended(self):
        self.fake_one_review_request()
        # A lock left by a round that was killed, whose process ID now belongs to this test.
        self.hold(self.state / "round.lock", os.getpid())
        before = datetime.now(timezone.utc).replace(microsecond=0)
        result = self.command("whooper-round")
        after = datetime.now(timezone.utc)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self.status()
        last = status["last_round"]
        started, ended = (datetime.fromisoformat(last[key]) for key in ["started_at", "ended_at"])
        self.assertTrue(before <= started <= ended <= after, last)
        self.assertEqual({key: last[key] for key in ["result", "selected", "reviews"]},
                         {"result": "done", "selected": 1,
                          "reviews": [{"pr": "example-org/example-repo#12", "outcome": "failed"}]})
        self.assertEqual(status["round"], {"running": False, "started_at": None, "reviewing": None})
        self.assertEqual(status["rounds"], [{"started_at": last["started_at"], "ended_at": last["ended_at"],
                                             "result": "done", "selected": 1, "reason": None}])
        self.assertEqual(self.notifications.read_text(),
                         "Whooper: example-org/example-repo#12 review failed | See whooper-round's output.\n")
        self.assertFalse((self.state / "round.lock").exists())

    def test_after_a_review_stops_at_a_cause_that_stops_every_review_the_rest_of_the_round_waits(self):
        env, item, _ = self.pull_request()
        head = json.loads(item.read_text())["head_sha"]
        line = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
        self.setup_script(f"echo '{line}' >&2\nexit 1")
        # Review requests for PRs 12 and 13 of sample-org/plain-repo, which the round reviews in that order.
        plain = {"repository": {"nameWithOwner": "sample-org/plain-repo"}, "baseRefName": "main"}
        self.fake_review_requests(*[{**pr(number=number, head=sha, requested=[LOGIN]), **plain,
                                     "url": f"https://github.com/sample-org/plain-repo/pull/{number}"}
                                    for number, sha in [(12, head), (13, "3" * 40)]])
        result = self.command("whooper-round", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--- sample-org/plain-repo#13 first_review waits for the next round: Docker isn't running\n",
                      result.stdout)
        status = self.status()
        self.assertEqual(status["last_round"]["reviews"], [{"pr": "sample-org/plain-repo#12", "outcome": "failed"},
                                                           {"pr": "sample-org/plain-repo#13", "outcome": "waiting"}])
        self.assertEqual(status["rounds"][0]["reason"], "Docker isn't running, so 1 review(s) wait for the next round")
        # PR 13's review did not start: its run holds the work item and the cause it waits on, and needs no
        # one. Only the failed review sent a notification.
        waits, failed = status["reviews"]
        self.assertEqual((failed["pr"], failed["outcome"], failed["cause"], failed["needs_you"]),
                         (12, "failed", CAUSES["docker_unavailable"], True))
        self.assertEqual({key: waits[key] for key in ["pr", "outcome", "cause", "needs_you", "ended_at", "findings"]},
                         {"pr": 13, "outcome": "waiting", "cause": CAUSES["docker_unavailable"], "needs_you": False,
                          "ended_at": None, "findings": 0})
        work = json.loads((self.state / "items" / "sample-org-plain-repo-13-first_review.json").read_text())
        run = self.state / "runs" / waits["run_id"]
        self.assertEqual(json.loads((run / "run.json").read_text()), {**work, "run_id": run.name, "agent": "claude"})
        self.assertEqual(json.loads((run / "waiting.json").read_text()), CAUSES["docker_unavailable"])
        self.assertEqual(self.notifications.read_text(),
                         "Whooper: sample-org/plain-repo#12 review failed | See whooper-round's output.\n")
        # Whooper is blocked since the failure, with one review waiting.
        self.assertEqual(status["blocked"], {**CAUSES["docker_unavailable"], "since": failed["ended_at"], "waiting": 1})
        # The next round, with Docker still down, fails PR 12's review again, and PR 13 still waits in the
        # same run.
        self.assertEqual(self.command("whooper-round", env=env).returncode, 0)
        status = self.status()
        self.assertEqual([(r["pr"], r["outcome"]) for r in status["reviews"]],
                         [(12, "failed"), (13, "waiting"), (12, "failed")])
        self.assertEqual(status["reviews"][1]["run_id"], waits["run_id"])
        self.assertEqual(status["blocked"], {**CAUSES["docker_unavailable"], "since": failed["ended_at"], "waiting": 1})
        # The round after reviews both again, PR 13 from the recheck list even once the poll no longer
        # lists it, as for a Slack ask, which a poll reads once.
        [poll] = sorted((self.state / "poll").glob("*Z.json"))[-1:]
        items = json.loads(poll.read_text())["items"]
        poll.write_text(json.dumps({"items": [i for i in items if i["pr"] == 12]}))
        select = self.command("whooper-select", env=env)
        self.assertEqual([json.loads(line) for line in select.stdout.splitlines()],
                         [i for i in items if i["pr"] == 12] + [work])

    def test_a_round_stopped_during_a_review_reports_that_review_as_interrupted(self):
        self.fake_one_review_request()
        # notify.sh notes the failed review's notification, then holds the round there.
        self.script(self.notify, f"echo \"$1\" >> '{self.notifications}'\nsleep 30")
        loop = self.start(ROOT / "bin" / "whooper-run")
        self.wait_for(self.notifications.exists, "the review's notification")
        status = self.status()
        running = status["round"]
        self.assertEqual((status["loop"], status["next_round"]), ({"running": True, "pid": loop.pid}, None))
        self.assertEqual((running["running"], running["reviewing"]), (True, "example-org/example-repo#12"))
        self.stop(loop)
        status = self.status()
        last = status["last_round"]
        self.assertEqual((loop.returncode, last["started_at"]), (130, running["started_at"]))
        self.assertEqual({key: last[key] for key in ["result", "selected", "reviews"]},
                         {"result": "interrupted", "selected": 1,
                          "reviews": [{"pr": "example-org/example-repo#12", "outcome": "interrupted"}]})
        self.assertEqual(status["rounds"], [{"started_at": last["started_at"], "ended_at": last["ended_at"],
                                             "result": "interrupted", "selected": 1, "reason": None}])
        self.assertFalse((self.state / "round.lock").exists())

    def test_a_review_keeps_the_diff_it_read_after_its_worktrees_go(self):
        env, item, merge_base = self.pull_request()
        run_diff = Path(self.tmp.name) / "run.diff"
        # The run starts in the head worktree, reads the change as prompts/review.md tells it to, writes its
        # overview, and finds nothing.
        (self.fakes / "result.json").write_text(json.dumps({
            "type": "result", "subtype": "success", "total_cost_usd": 0, "duration_ms": 0, "num_turns": 1,
            "structured_output": {"status": "reviewed", "skip_reason": "", "findings": [],
                                  "approval": {"verdict": "approve_once_ci_passes", "reason": "Read the diff."}}}))
        (self.fakes / "overview.json").write_text(json.dumps(self.overview_of_the_pr()))
        self.claude_script("\n".join([
            f"git diff --no-ext-diff --no-textconv {merge_base} HEAD > '{run_diff}'",
            'cp "${0%/*}/overview.json" "$WHOOPER_RUN/"',
            'cat "${0%/*}/result.json"',
        ]))
        result = self.command("whooper-review", str(item), "--dry-run", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run = Path(result.stdout.strip())
        self.assertEqual(run.parent, self.state / "runs")
        diff = (run / "diff.patch").read_text()
        self.assertEqual(diff, run_diff.read_text())
        self.assertIn("\n+two\n", diff)
        self.assertIn("Binary files a/generated.txt and b/generated.txt differ", diff)
        self.assertFalse((run / "head").exists() or (run / "base").exists())
        # A past review shows the diff it kept.
        shown = self.command("whooper-show", run.name, "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual({key: json.loads(shown.stdout)[key] for key in ["outcome", "diff"]},
                         {"outcome": "reviewed", "diff": diff})

    # app.txt at the merge base and at the head of a PR that changes lines 13 and 22 of its 34, which git diff's
    # defaults show as two hunks, lines 10 to 16 and 19 to 25: 3 unchanged lines around each change, and the 2
    # lines between the hunks left out.
    APP_IN_TWO_HUNKS = ("".join(f"line {n}\n" for n in range(1, 35)),
                        "".join(f"changed {n}\n" if n in (13, 22) else f"line {n}\n" for n in range(1, 35)))
    # report.c at the merge base and at the head, which adds count above report and drops sum: myers, git's
    # default algorithm, shows the change otherwise than patience does, around a blank line.
    REPORT_C = tuple("".join(f"{line}\n" for line in lines) for lines in (
        ["// Prints the total of the prices.", "void report(int total)", "{", "    if (total > 0)", "    {",
         '        printf("Total: ");', '        printf("%d\\n", total);', "    }", "}", "",
         "int sum(int n)", "{", "    if (n > 0)", "    {", "        return sum(n - 1) + n;", "    }", "    return 0;", "}"],
        ["int count(int n)", "{", "    if (n > 0)", "    {", "        return count(n - 1) + 1;", "    }", "    return 0;",
         "}", "", "// Prints the total of the prices.", "void report(int total)", "{", "    if (total > 0)", "    {",
         '        printf("%d\\n", total);', "    }", "}"],
    ))
    # The PR's other files, for diff_settings: report.c; notes.txt, which moves to docs/notes.txt; app-copy.txt, a copy
    # of app.txt as it was, which git's default rename detection shows as a new file; greeting.txt, which moves to
    # hello.txt with its last line changed, a rename git finds only by comparing greeting.txt with each new file,
    # app-copy.txt and hello.txt; café.txt, whose name git quotes; and sliders.txt, which gains 3 lines that git could
    # show in more than one place, and its indent heuristic picks one.
    GREETING = "".join(f"{word}\n" for word in ["hello", "there", "one", "two", "three", "four", "five", "six", "seven",
                                                 "eight"])
    MORE_FILES = {"report.c": REPORT_C, "notes.txt": ("first\nsecond\nthird\n", None),
                  "docs/notes.txt": (None, "first\nsecond\nthird\n"), "app-copy.txt": (None, APP_IN_TWO_HUNKS[0]),
                  "greeting.txt": (GREETING, None), "hello.txt": (None, GREETING.replace("eight", "EIGHT")),
                  "café.txt": ("one\n", "one\ntwo\n"),
                  "sliders.txt": ("1\n2\na\n\nb\n3\n4\n", "1\n2\na\n\nb\na\n\nb\n3\n4\n")}

    def diff_settings(self):
        """The git settings, each as the config entries it takes, by name, each of which changes the patch git diff
        prints of the PR that pull_request makes with APP_IN_TWO_HUNKS and MORE_FILES, and which whooper-review pins,
        to git's defaults but for 12-digit object names, in diff.patch, in its run's environment, but for the external
        diff tool, and in the setup script's list of changed files where they change it: 10 unchanged lines around each
        change instead of 3, hunks at most 5 lines apart joined into one, the patience algorithm, an order file that
        puts the submodule and report.c first, no rename detection, a blank line of context printed without its leading
        space, object names of at least 20 digits, café.txt's name unquoted, the submodule left out, rename detection
        that compares at most 1 file with 1 other, a hunk-header pattern and a textconv filter that the user's
        attributes file gives hello.txt and app.txt, an external diff tool, color, set through color.ui and through
        color.diff, paths without a/ and b/, and with others before them, a submodule's log instead of its commit
        lines, each file over 10 bytes shown as binary, and no indent heuristic."""
        tmp = Path(self.tmp.name)
        order, attributes = tmp / "order", tmp / "attributes"
        order.write_text("vendor/*\nreport.c\n")
        attributes.write_text("app.txt diff=shout\nhello.txt diff=hello\n")
        self.script(tmp / "external", 'echo "external diff of $1"')
        self.script(tmp / "shout", 'tr a-z A-Z < "$1"')
        return [{"diff.context": "10"}, {"diff.interHunkContext": "5"}, {"diff.algorithm": "patience"},
                {"diff.orderFile": str(order)}, {"diff.renames": "false"}, {"diff.suppressBlankEmpty": "true"},
                {"core.abbrev": "20"}, {"core.quotePath": "false"}, {"diff.ignoreSubmodules": "all"},
                {"diff.renameLimit": "1"}, {"core.attributesFile": str(attributes), "diff.hello.xfuncname": "^hello$"},
                {"core.attributesFile": str(attributes), "diff.shout.textconv": str(tmp / "shout")},
                {"diff.external": str(tmp / "external")}, {"color.ui": "always"}, {"color.diff": "always"},
                {"diff.noprefix": "true"}, {"diff.srcPrefix": "SRC/"}, {"diff.dstPrefix": "DST/"},
                {"diff.submodule": "log"}, {"core.bigFileThreshold": "10"}, {"diff.indentHeuristic": "false"}]

    def write_git_config(self, env, settings):
        """Writes settings, each as diff_settings gives it, as the git config of env's home."""
        lines = []
        for key, value in {key: value for setting in settings for key, value in setting.items()}.items():
            section, _, name = key.rpartition(".")
            top, _, subsection = section.partition(".")
            lines += [f'[{top} "{subsection}"]' if subsection else f"[{top}]", f"\t{name} = {value}"]
        Path(env["GIT_CONFIG_GLOBAL"]).write_text("".join(f"{line}\n" for line in lines))

    def plain_patch(self, env, merge_base, setting=None, names=False):
        """git's own patch of the PR, with whooper-review's 12-digit object names, or with names its list of the files
        the PR changes, under setting, as diff_settings gives it, but no config from env's home, which tests write only
        after this."""
        config = [arg for key, value in (setting or {}).items() for arg in ("-c", f"{key}={value}")]
        return subprocess.run(["git", "-c", "core.abbrev=12", *config, "diff", *(["--name-only"] if names else []),
                               merge_base, "pr-12"], cwd=Path(self.tmp.name) / "origin", env=env, check=True,
                              capture_output=True, text=True).stdout

    def test_each_diff_setting_changes_the_patch_of_the_pr(self):
        # Without this, the tests below of diff.patch, its rebuild, the run's own git diff, gremlins' git diff, and the
        # setup script's list of changed files could pass with a setting left as git's default.
        env, _, merge_base = self.pull_request(app=self.APP_IN_TWO_HUNKS, more=self.MORE_FILES)
        plain, names = self.plain_patch(env, merge_base), self.plain_patch(env, merge_base, names=True)
        listed = set()
        for setting in self.diff_settings():
            with self.subTest(setting=", ".join(setting)):
                self.assertNotEqual(self.plain_patch(env, merge_base, setting), plain)
            if self.plain_patch(env, merge_base, setting, names=True) != names:
                listed.update(setting)
        # The ones that change which files git diff --name-only lists, in what order, or how it writes their names.
        self.assertEqual(listed, {"diff.orderFile", "diff.renames", "core.quotePath", "diff.ignoreSubmodules",
                                  "diff.renameLimit"})

    def test_the_kept_diff_is_a_plain_patch_whatever_the_git_config_says(self):
        env, item, merge_base = self.pull_request(app=self.APP_IN_TWO_HUNKS, more=self.MORE_FILES)
        plain, names = self.plain_patch(env, merge_base), self.plain_patch(env, merge_base, names=True)
        self.write_git_config(env, self.diff_settings())
        # The setup script keeps the list of changed files it gets.
        tmp = Path(self.tmp.name)
        self.setup_script(f'cp "$WHOOPER_CHANGED" "{tmp / "changed.txt"}"')
        # The review itself fails, since claude is the fake that exits 1, after it saved the diff.
        self.command("whooper-review", str(item), "--dry-run", env=env)
        [run] = (self.state / "runs").iterdir()
        self.assertEqual(((run / "diff.patch").read_text(), (tmp / "changed.txt").read_text()), (plain, names))

    def test_the_run_reads_its_own_git_diff_as_the_kept_diff_whatever_the_git_config_says(self):
        # The repo's own .gitattributes names app.txt's diff driver, so the textconv filter the user's config gives that
        # driver reaches a git diff without --no-textconv, as diff.external reaches one without --no-ext-diff: no git
        # setting turns either off, and an empty diff.external makes git fail to start it.
        attributes = "generated.txt -diff\napp.txt diff=shout\n"
        env, item, merge_base = self.pull_request(app=self.APP_IN_TWO_HUNKS,
                                                  more={**self.MORE_FILES, ".gitattributes": (attributes, attributes)})
        plain = self.plain_patch(env, merge_base)
        self.write_git_config(env, self.diff_settings())
        tmp = Path(self.tmp.name)
        # The run reads the change with the git diff prompts/review.md names, and stops.
        self.claude_script("\n".join([
            "command=\"$(sed -n 's/.*Read the change with `\\([^`]*\\)`.*/\\1/p' \"$WHOOPER_RUN/prompt.md\")\"",
            f"eval \"$(echo \"$command\" | sed 's/<merge base>/{merge_base}/')\" > '{tmp / 'run.diff'}'",
            f"git config whooper.kept > '{tmp / 'kept.txt'}'"]))
        # The environment Whooper starts in already holds git config: a setting the review sets again, which
        # the review's own wins over, and one the run keeps.
        self.command("whooper-review", str(item), "--dry-run",
                     env={**env, "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "diff.context", "GIT_CONFIG_VALUE_0": "10",
                          "GIT_CONFIG_KEY_1": "whooper.kept", "GIT_CONFIG_VALUE_1": "yes"})
        [run] = (self.state / "runs").iterdir()
        self.assertEqual(((tmp / "run.diff").read_text(), (run / "diff.patch").read_text(), (tmp / "kept.txt").read_text()),
                         (plain, plain, "yes\n"))

    def test_the_run_reads_the_whole_change_from_a_subfolder_whatever_diff_relative_says(self):
        env, item, merge_base = self.pull_request(app=self.APP_IN_TWO_HUNKS, more=self.MORE_FILES)
        plain = self.plain_patch(env, merge_base)
        # diff.relative=true shows a git diff run in a subfolder only that folder's changes, with paths relative to it.
        self.write_git_config(env, [{"diff.relative": "true"}])
        tmp = Path(self.tmp.name)
        # The run reads the change with git diff in docs/, as after a probe's cd there, and stops.
        self.claude_script(f"cd docs && git diff {merge_base} HEAD > '{tmp / 'run.diff'}'")
        self.command("whooper-review", str(item), "--dry-run", env=env)
        self.assertEqual((tmp / "run.diff").read_text(), plain)

    def test_the_kept_diff_the_runs_own_git_diff_and_the_rebuild_leave_out_gits_diff_variables(self):
        # git takes an external diff tool from GIT_EXTERNAL_DIFF, and how many unchanged lines go around each change
        # from GIT_DIFF_OPTS, whatever its config says, and GIT_DIFF_OPTS outranks even --unified.
        env, item, merge_base = self.pull_request(app=self.APP_IN_TWO_HUNKS)
        plain = self.plain_patch(env, merge_base)
        tmp = Path(self.tmp.name)
        self.script(tmp / "external", 'echo "external diff of $1"')
        variables = {"GIT_EXTERNAL_DIFF": str(tmp / "external"), "GIT_DIFF_OPTS": "--unified=10"}
        # The run reads the change with a plain git diff and stops.
        self.claude_script(f"git diff {merge_base} HEAD > '{tmp / 'run.diff'}'")
        self.command("whooper-review", str(item), "--dry-run", env={**env, **variables})
        [run] = (self.state / "runs").iterdir()
        self.assertEqual(((tmp / "run.diff").read_text(), (run / "diff.patch").read_text()), (plain, plain))
        # whooper-show rebuilds the diff of a review from before reviews kept it the same way.
        (run / "diff.patch").unlink()
        shown = self.command("whooper-show", run.name, "--json", env={**env, **variables})
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["diff"], plain)

    def test_show_rebuilds_the_diff_of_a_review_from_before_reviews_kept_it(self):
        env, item, merge_base = self.pull_request(app=self.APP_IN_TWO_HUNKS, more=self.MORE_FILES)
        self.command("whooper-review", str(item), "--dry-run", env=env)
        [run] = (self.state / "runs").iterdir()
        kept = (run / "diff.patch").read_text()
        # Reviews kept no diff.patch before whooper-review saved one.
        (run / "diff.patch").unlink()
        self.write_git_config(env, self.diff_settings())
        clone = Path(self.tmp.name) / "cache" / "whooper" / "repos" / "sample-org-plain-repo"
        before = files(clone)
        shown = self.command("whooper-show", run.name, "--json", env=env)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        # The same patch the review read, generated.txt shown as binary by the head's .gitattributes.
        self.assertEqual(json.loads(shown.stdout)["diff"], kept)
        self.assertIn("Binary files a/generated.txt and b/generated.txt differ", kept)
        self.assertEqual(files(clone), before)

    def test_show_gives_no_diff_when_the_clone_lacks_a_reviewed_commit_or_file(self):
        env, item, _ = self.pull_request()
        self.command("whooper-review", str(item), "--dry-run", env=env)
        [run] = (self.state / "runs").iterdir()
        (run / "diff.patch").unlink()
        tmp = Path(self.tmp.name)
        clone = tmp / "cache" / "whooper" / "repos" / "sample-org-plain-repo"

        def git(*args, cwd=tmp / "origin"):
            return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                                  text=True).stdout.strip()

        def diff():
            shown = self.command("whooper-show", run.name, "--json", env=env)
            self.assertEqual(shown.returncode, 0, shown.stderr)
            return json.loads(shown.stdout)["diff"]

        # The clone made again as README.md makes it, a partial clone, which fetches a file only when
        # something needs it: it has both commits but neither one's files. origin would hand them over,
        # so a fetch would succeed.
        git("config", "uploadpack.allowFilter", "true")
        git("config", "uploadpack.allowAnySHA1InWant", "true")
        shutil.rmtree(clone)
        git("clone", "--quiet", "--filter=blob:none", "--no-checkout", f"file://{tmp / 'origin'}", str(clone))
        before = files(clone)
        self.assertEqual(diff(), None)
        self.assertEqual(files(clone), before)
        # A head the clone never fetched.
        git("commit", "--quiet", "--allow-empty", "-m", "after the clone")
        record = json.loads((run / "run.json").read_text())
        (run / "run.json").write_text(json.dumps({**record, "head_sha": git("rev-parse", "HEAD")}))
        self.assertEqual(diff(), None)
        # No clone at all.
        shutil.rmtree(clone)
        self.assertEqual(diff(), None)

    def test_a_review_keeps_the_overview_json_its_run_wrote_and_moves_one_that_breaks_a_rule_aside(self):
        env, item, _ = self.pull_request()
        tmp = Path(self.tmp.name)
        findings = {"status": "reviewed", "skip_reason": "", "findings": [],
                    "approval": {"verdict": "approve_once_ci_passes", "reason": "Read the diff."}}

        def review(overview=None):
            """Reviews PR 12 with a Claude run that saves the prompt, the schema, and the directory it may write
            in that it got, writes overview, bytes, as overview.json in the run directory unless it is None, and
            returns findings, and returns whooper-review's result and the run's directory. A run ID names its
            start to the second, so each review starts in a second of its own."""
            sleep(1 - datetime.now().microsecond / 1e6)
            (self.fakes / "result.json").write_text(json.dumps({
                "type": "result", "subtype": "success", "total_cost_usd": 0, "duration_ms": 0, "num_turns": 1,
                "structured_output": findings}))
            (self.fakes / "overview.json").unlink(missing_ok=True)
            if overview is not None:
                (self.fakes / "overview.json").write_bytes(overview)
            self.claude_script("\n".join([
                'fakes="${0%/*}"',
                f"""printf '%s' "$2" > '{tmp}/prompt.md'""",
                "while [ $# -gt 0 ]; do",
                f"""  [ "$1" = --json-schema ] && printf '%s' "$2" > '{tmp}/schema.json'""",
                f"""  [ "$1" = --add-dir ] && printf '%s' "$2" > '{tmp}/add-dir'""",
                "  shift",
                "done",
                'if [ -f "$fakes/overview.json" ]; then cp "$fakes/overview.json" "$WHOOPER_RUN/"; fi',
                'cat "$fakes/result.json"',
            ]))
            result = self.command("whooper-review", str(item), "--dry-run", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result, Path(result.stdout.strip())

        def shown(run):
            """What whooper-show gives of the review's outcome, findings, and approval, and its overview."""
            result = self.command("whooper-show", run.name, "--json", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            return {key: json.loads(result.stdout)[key]
                    for key in ["outcome", "findings", "safe_to_approve", "drafts", "failure", "cause", "overview"]}

        # The review of a run that writes no overview.json.
        result, run = review()
        without = shown(run)
        self.assertEqual(without, {"outcome": "reviewed", "findings": findings, "safe_to_approve": True,
                                   "drafts": {"event": "APPROVE", "message": "", "findings": {}}, "failure": None,
                                   "cause": None, "overview": None})
        self.assertNotIn("overview", result.stderr)
        self.assertFalse((run / "overview.invalid.json").exists())
        # The prompt asks for overview.json in the run directory, which the launcher lets the run write in, as
        # JSON that fits schemas/overview.json, and the schema of the findings the run returns has no overview.
        prompt = (tmp / "prompt.md").read_text()
        self.assertIn("\n## The overview\n", prompt)
        self.assertIn(f"`{ROOT / 'schemas' / 'overview.json'}`", prompt)
        self.assertIn(f"\n- Run directory: {run}. Write log.md and overview.json there.\n", prompt)
        self.assertEqual((tmp / "add-dir").read_text(), str(run))
        self.assertNotIn("overview", json.loads((tmp / "schema.json").read_text())["properties"])
        # An overview.json that keeps every rule stays, and whooper-show gives it.
        kept = self.overview_of_the_pr()
        result, run = review(json.dumps(kept).encode())
        self.assertNotIn("overview", result.stderr)
        self.assertEqual(json.loads((run / "overview.json").read_text()), kept)
        self.assertFalse((run / "overview.invalid.json").exists())
        self.assertEqual(shown(run), {**without, "overview": kept})
        # One with lines outside the diff's hunks, one that does not fit the schema, one that is no overview at
        # all, one cut short, as when the run stopped while writing it, and one that is not UTF-8 moves to
        # overview.invalid.json, with why and what it held, and the review is as it is without one.
        place = {**kept["places"][0], "start_line": 3, "end_line": 3}
        layers = [{**kept["layers"][0], "read_hint": "deep"}, kept["layers"][1]]
        for held, content, reason in [
            (self.overview_of_the_pr(places=[place]), None,
             "overview.places[0], lines 3 to 3 of app.txt, is not inside one hunk of the diff"),
            (self.overview_of_the_pr(layers=layers), None,
             "overview.layers[0].read_hint is 'deep', not one of ['read', 'skim', 'skip']"),
            ("Adds a line.", None, "overview is not of type object"),
            ('{"summary": "Adds a second line', b'{"summary": "Adds a second line',
             "overview.json is not JSON: Unterminated string starting at: line 1 column 13 (char 12)"),
            (None, b'{"summary": "caf\xe9"}',
             "overview.json cannot be read: 'utf-8' codec can't decode byte 0xe9 in position 16: invalid"
             " continuation byte"),
        ]:
            with self.subTest(reason):
                result, run = review(content or json.dumps(held).encode())
                self.assertIn(f"whooper-review: dropped the run's overview: {reason}\n", result.stderr)
                self.assertFalse((run / "overview.json").exists())
                self.assertEqual(json.loads((run / "overview.invalid.json").read_text()),
                                 {"reason": reason, "overview": held})
                self.assertEqual(shown(run), without)

    def test_a_review_goes_on_when_the_overview_check_itself_fails(self):
        env, item, _ = self.pull_request()
        findings = {"status": "reviewed", "skip_reason": "", "findings": [],
                    "approval": {"verdict": "approve_once_ci_passes", "reason": "Read the diff."}}
        (self.fakes / "result.json").write_text(json.dumps({
            "type": "result", "subtype": "success", "total_cost_usd": 0, "duration_ms": 0, "num_turns": 1,
            "structured_output": findings}))
        # A directory named overview.json, which the check cannot read and then cannot remove, makes the check
        # itself fail.
        self.claude_script('mkdir "$WHOOPER_RUN/overview.json"\ncat "${0%/*}/result.json"')
        result = self.command("whooper-review", str(item), "--dry-run", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        shown = self.command("whooper-show", Path(result.stdout.strip()).name, "--json", env=env)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual({key: json.loads(shown.stdout)[key] for key in ["outcome", "findings", "failure", "overview"]},
                         {"outcome": "reviewed", "findings": findings, "failure": None, "overview": None})

    def test_each_agents_run_writes_overview_json_in_the_run_directory_and_show_gives_it(self):
        env, item, _ = self.pull_request()
        config = Path(env["WHOOPER_CONFIG"])
        findings = {"status": "reviewed", "skip_reason": "", "findings": [],
                    "approval": {"verdict": "approve_once_ci_passes", "reason": "Read the diff."}}
        kept = self.overview_of_the_pr()
        (self.fakes / "findings.json").write_text(json.dumps(findings))
        (self.fakes / "overview.json").write_text(json.dumps(kept))
        (self.fakes / "result.json").write_text(json.dumps({
            "type": "result", "subtype": "success", "total_cost_usd": 0, "duration_ms": 0, "num_turns": 1,
            "structured_output": findings}))
        # Each fake writes overview.json in the run directory, as its agent's run does with its file-writing
        # tool: Claude's run, which --add-dir lets write there, returns the findings as structured output, and
        # Devin's, in dangerous mode, writes findings.json there too, and its export where --export says. A
        # second call, devin rm, deletes the Devin run's session.
        self.claude_script("\n".join([
            'cp "${0%/*}/overview.json" "$WHOOPER_RUN/"',
            'cat "${0%/*}/result.json"',
        ]))
        self.script(self.fakes / "devin", "\n".join([
            'if [ "$1" = rm ]; then exit 0; fi',
            'while [ $# -gt 1 ]; do [ "$1" = --export ] && export="$2"; shift; done',
            'cp "${0%/*}/findings.json" "${0%/*}/overview.json" "$WHOOPER_RUN/"',
            """echo '{"session_id": "session-7", "steps": []}' > "$export\"""",
        ]))
        for agent in ["claude", "devin"]:
            with self.subTest(agent):
                # A run ID names its start to the second, so each review starts in a second of its own.
                sleep(1 - datetime.now().microsecond / 1e6)
                config.write_text(re.sub(r'(?m)^review = "\w+"$', f'review = "{agent}"', config.read_text()))
                result = self.command("whooper-review", str(item), "--dry-run", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                run = Path(result.stdout.strip())
                self.assertEqual(json.loads((run / "run.json").read_text())["agent"], agent)
                shown = self.command("whooper-show", run.name, "--json", env=env)
                self.assertEqual(shown.returncode, 0, shown.stderr)
                self.assertEqual({key: json.loads(shown.stdout)[key] for key in ["outcome", "findings", "overview"]},
                                 {"outcome": "reviewed", "findings": findings, "overview": kept})

    def test_an_overview_the_run_wrote_in_findings_json_moves_to_overview_json_and_cannot_fail_the_review(self):
        env, item, _ = self.pull_request()
        config = Path(env["WHOOPER_CONFIG"])
        config.write_text(re.sub(r'(?m)^review = "\w+"$', 'review = "devin"', config.read_text()))
        findings = {"status": "reviewed", "skip_reason": "", "findings": [],
                    "approval": {"verdict": "approve_once_ci_passes", "reason": "Read the diff."}}
        # A Devin run that writes its overview inside findings.json, where schemas/findings.json has no room for
        # it, and overview.json too when the test leaves one beside it. cp -p keeps the time the test gives
        # findings.json, as the time the run wrote it. A second call, devin rm, deletes the run's session.
        self.script(self.fakes / "devin", "\n".join([
            'if [ "$1" = rm ]; then exit 0; fi',
            'while [ $# -gt 1 ]; do [ "$1" = --export ] && export="$2"; shift; done',
            'fakes="${0%/*}"',
            'cp -p "$fakes/findings.json" "$WHOOPER_RUN/"',
            'if [ -f "$fakes/overview.json" ]; then cp "$fakes/overview.json" "$WHOOPER_RUN/"; fi',
            """echo '{"session_id": "session-7", "steps": []}' > "$export\"""",
        ]))

        def review(stray, own=None):
            """Reviews PR 12 with the run writing stray as the overview in findings.json, and own, text, as
            overview.json unless it is None, and returns whooper-review's error output, the run's directory, and
            what whooper-show gives of the review. findings.json keeps the time the run wrote it, here a minute
            before the review, which whooper-status gives as when the review ended. A run ID names its start to
            the second, so each review starts in a second of its own."""
            sleep(1 - datetime.now().microsecond / 1e6)
            (self.fakes / "findings.json").write_text(json.dumps({**findings, "overview": stray}))
            written = int(datetime.now().timestamp()) - 60
            os.utime(self.fakes / "findings.json", (written, written))
            (self.fakes / "overview.json").unlink(missing_ok=True)
            if own is not None:
                (self.fakes / "overview.json").write_text(own)
            result = self.command("whooper-review", str(item), "--dry-run", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            run = Path(result.stdout.strip())
            self.assertEqual((run / "findings.json").stat().st_mtime, written)
            shown = self.command("whooper-show", run.name, "--json", env=env)
            self.assertEqual(shown.returncode, 0, shown.stderr)
            keys = ["outcome", "findings", "safe_to_approve", "drafts", "failure", "cause", "overview"]
            return result.stderr, run, {key: json.loads(shown.stdout)[key] for key in keys}

        # Each review goes on as one whose run wrote only the findings, which lose only the overview.
        reviewed = {"outcome": "reviewed", "findings": findings, "safe_to_approve": True,
                    "drafts": {"event": "APPROVE", "message": "", "findings": {}}, "failure": None, "cause": None}
        moved = "whooper-review: moved the overview in the run's findings.json to overview.json\n"
        kept = self.overview_of_the_pr()
        with self.subTest("an overview that keeps every rule moves to overview.json, and whooper-show gives it"):
            err, run, shown = review(kept)
            self.assertIn(moved, err)
            self.assertEqual(json.loads((run / "findings.json").read_text()), findings)
            self.assertEqual(json.loads((run / "overview.json").read_text()), kept)
            self.assertFalse((run / "overview.invalid.json").exists())
            self.assertEqual(shown, {**reviewed, "overview": kept})
        with self.subTest("one that breaks a rule moves to overview.json, and then aside to overview.invalid.json"):
            broken = self.overview_of_the_pr(places=[{**kept["places"][0], "start_line": 3, "end_line": 3}])
            reason = "overview.places[0], lines 3 to 3 of app.txt, is not inside one hunk of the diff"
            err, run, shown = review(broken)
            self.assertIn(f"{moved}whooper-review: dropped the run's overview: {reason}\n", err)
            self.assertEqual(json.loads((run / "findings.json").read_text()), findings)
            self.assertFalse((run / "overview.json").exists())
            self.assertEqual(json.loads((run / "overview.invalid.json").read_text()),
                             {"reason": reason, "overview": broken})
            self.assertEqual(shown, {**reviewed, "overview": None})
        with self.subTest("an overview.json the run also wrote stays as it is, and the one in findings.json goes"):
            own = json.dumps(kept)
            err, run, shown = review(self.overview_of_the_pr(summary="Another summary."), own)
            self.assertIn("whooper-review: discarded the overview in the run's findings.json, since the run also wrote"
                          " overview.json\n", err)
            self.assertNotIn(moved, err)
            self.assertEqual(json.loads((run / "findings.json").read_text()), findings)
            self.assertEqual((run / "overview.json").read_text(), own)
            self.assertFalse((run / "overview.invalid.json").exists())
            self.assertEqual(shown, {**reviewed, "overview": kept})

    def shop_pull_request(self, base=SHOP, head=SHOP_PR):
        """Makes PR 12 of sample-org/plain-repo the PR that changes base's files to head's, SHOP's unless given, in a
        local origin, the cache's clone of it, and its work item, has claude's review run find nothing, and returns the
        environment for git and the review (git_env), with no gopls Whooper can find, and the work item's path."""
        tmp = Path(self.tmp.name)
        env = self.git_env()
        _, head = commit_pr(tmp / "origin", env, base, head)
        subprocess.run(["git", "clone", "--quiet", "--no-checkout", str(tmp / "origin"),
                        str(tmp / "cache" / "whooper" / "repos" / "sample-org-plain-repo")], env=env, check=True)
        item = tmp / "item.json"
        item.write_text(json.dumps({"kind": "first_review", "repo": "sample-org/plain-repo", "pr": 12, "head_sha": head,
                                    "base_ref": "main", "source": "github_request", "slack": None, "reason": "r"}))
        (self.fakes / "result.json").write_text(json.dumps({
            "type": "result", "subtype": "success", "total_cost_usd": 0, "duration_ms": 0, "num_turns": 1,
            "structured_output": {"status": "reviewed", "skip_reason": "", "findings": [],
                                  "approval": {"verdict": "approve_once_ci_passes", "reason": "Read the diff."}}}))
        self.claude_script('cat "${0%/*}/result.json"')
        (tmp / "gobin").mkdir()
        return without_gopls(env, tmp / "gobin"), item

    def test_a_review_saves_how_the_changed_code_connects_before_its_worktrees_go_and_show_prints_it(self):
        env, item = self.shop_pull_request()
        gopls, _ = whooper.find_go_tool("gopls")
        # Without gopls, and with it when it is installed. A run ID names its start to the second, so each review
        # starts in a second of its own.
        for found_by, path in [("name_search", env["PATH"]),
                               *([("gopls", f"{Path(gopls).parent}{os.pathsep}{env['PATH']}")] if gopls else [])]:
            with self.subTest(found_by):
                sleep(1 - datetime.now().microsecond / 1e6)
                result = self.command("whooper-review", str(item), "--dry-run", env={**env, "PATH": path})
                self.assertEqual(result.returncode, 0, result.stderr)
                run = Path(result.stdout.strip())
                self.assertEqual(json.loads((run / "connections.json").read_text()), shop_connections(found_by))
                self.assertFalse((run / "head").exists() or (run / "base").exists())
                shown = self.command("whooper-show", run.name, "--json", env=env)
                self.assertEqual(shown.returncode, 0, shown.stderr)
                self.assertEqual({key: json.loads(shown.stdout)[key] for key in ["outcome", "connections"]},
                                 {"outcome": "reviewed", "connections": shop_connections(found_by)})

    def test_a_review_succeeds_with_name_search_links_when_typescript_crashes(self):
        if not shutil.which("node"):
            self.skipTest("node is not on PATH, so no TypeScript can run")
        env, item = self.shop_pull_request(TS_SHOP, TS_SHOP_PR)
        # The review run installs in the head's checkout, where the repo's setup script would, a TypeScript that kills
        # its own process as node loads it.
        self.claude_script("\n".join([
            'ts="$WHOOPER_HEAD/shop/node_modules/typescript"', 'mkdir -p "$ts/lib"',
            """echo '{"name": "typescript", "version": "5.9.3"}' > "$ts/package.json\"""",
            """echo 'process.kill(process.pid, "SIGKILL");' > "$ts/lib/typescript.js\"""",
            'cat "${0%/*}/result.json"']))
        result = self.command("whooper-review", str(item), "--dry-run", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run = Path(result.stdout.strip())
        self.assertEqual(json.loads((run / "connections.json").read_text()), ts_shop_by_name_search(
            "TypeScript failed in the head's checkout (node was stopped by signal 9)"))
        shown = self.command("whooper-show", run.name, "--json", env=env)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["outcome"], "reviewed")

    def test_a_review_succeeds_with_name_search_links_when_typescript_7s_server_crashes(self):
        env, item = self.shop_pull_request(TS_SHOP, TS_SHOP_PR)
        # The review run installs in the head's checkout, where the repo's setup script would, a TypeScript 7 whose language
        # server kills itself once it reads the step's first request.
        self.claude_script("\n".join([
            'modules="$WHOOPER_HEAD/shop/node_modules"', f'server="$modules/{whooper.TYPESCRIPT_SERVER_PACKAGE}/lib"',
            'mkdir -p "$modules/typescript" "$server"',
            """echo '{"name": "typescript", "version": "7.1.0-beta"}' > "$modules/typescript/package.json\"""",
            """printf '#!/bin/sh\\nread -r header\\nkill -9 $$\\n' > "$server/tsc\"""", 'chmod +x "$server/tsc"',
            'cat "${0%/*}/result.json"']))
        result = self.command("whooper-review", str(item), "--dry-run", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run = Path(result.stdout.strip())
        self.assertEqual(json.loads((run / "connections.json").read_text()), ts_shop_by_name_search(
            "TypeScript failed in the head's checkout (tsc --lsp was stopped by signal 9)"))
        shown = self.command("whooper-show", run.name, "--json", env=env)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["outcome"], "reviewed")

    def test_a_review_goes_on_when_the_connections_step_fails(self):
        env, item = self.shop_pull_request()
        # A directory named connections.json, which the step cannot replace with its file, makes the step fail.
        self.claude_script('mkdir "$WHOOPER_RUN/connections.json"\ncat "${0%/*}/result.json"')
        result = self.command("whooper-review", str(item), "--dry-run", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("whooper-review: the connections step failed, so the review has no connections.json\n",
                      result.stderr)
        shown = self.command("whooper-show", Path(result.stdout.strip()).name, "--json", env=env)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual({key: json.loads(shown.stdout)[key] for key in ["outcome", "failure", "connections"]},
                         {"outcome": "reviewed", "failure": None, "connections": None})

    # What the test-gaps step measures on PRICES' PR, whose module has no tests: the if's condition and the cap
    # in Clamp, and Half's return.
    PRICES_GAPS = {
        "measured": True, "reason": None,
        "tool": "GOPROXY=off GOTOOLCHAIN=local go test -count=1 -coverpkg=./prices ./prices",
        "lines": [{"path": "prices/prices.go", "line": line, "status": "not_run"} for line in (10, 11, 23)],
        "summary": {"changed_lines": 3, "run": 0, "not_run": 3},
        "limits": [AND_LINES, CHECKED, OWN_TESTS],
    }
    # The same once the mutation step ran without gremlins, which a review finds nowhere in these tests.
    PRICES_GAPS_MUTATED = {**PRICES_GAPS, "summary": {**PRICES_GAPS["summary"], "survived": 0, "caught": 0},
                           "limits": [*PRICES_GAPS["limits"], NO_GREMLINS]}

    def prices_review(self, claude, env=None):
        """Makes PR 12 of sample-org/plain-repo PRICES' PR in a local origin and the cache's clone of it, reviews it
        with a claude whose review run runs claude, lines of sh, and finds nothing, with no gopls Whooper can find,
        and returns whooper-review's result and the run directory."""
        tmp = Path(self.tmp.name)
        git_env = self.git_env()
        _, head = commit_pr(tmp / "origin", git_env, PRICES, PRICES_PR)
        subprocess.run(["git", "clone", "--quiet", "--no-checkout", str(tmp / "origin"),
                        str(tmp / "cache" / "whooper" / "repos" / "sample-org-plain-repo")], env=git_env, check=True)
        item = tmp / "item.json"
        item.write_text(json.dumps({"kind": "first_review", "repo": "sample-org/plain-repo", "pr": 12, "head_sha": head,
                                    "base_ref": "main", "source": "github_request", "slack": None, "reason": "r"}))
        (self.fakes / "result.json").write_text(json.dumps({
            "type": "result", "subtype": "success", "total_cost_usd": 0, "duration_ms": 0, "num_turns": 1,
            "structured_output": {"status": "reviewed", "skip_reason": "", "findings": [],
                                  "approval": {"verdict": "approve_once_ci_passes", "reason": "Read the diff."}}}))
        self.claude_script(f'{claude}\ncat "${{0%/*}}/result.json"')
        (tmp / "gobin").mkdir(exist_ok=True)
        result = self.command("whooper-review", str(item), "--dry-run",
                              env={**without_gopls(git_env, tmp / "gobin"), **(env or {})})
        return result, Path(result.stdout.strip())

    def shown_fields(self, run, *keys):
        shown = self.command("whooper-show", run.name, "--json", env=self.git_env())
        self.assertEqual(shown.returncode, 0, shown.stderr)
        return {key: json.loads(shown.stdout)[key] for key in keys}

    def test_a_review_measures_test_gaps_before_its_run_and_runs_its_examples_after_and_show_prints_both(self):
        request = examples_request([("Clamp", 9)], [("Clamp", "Clamp(50)"), ("Clamp", "Clamp(101)")])
        (self.fakes / "examples.json").write_text(json.dumps(request))
        (self.fakes / "whooper_examples_test.go").write_text(
            examples_test(("Clamp", "Clamp(50)", "Clamp(50)"), ("Clamp", "Clamp(101)", "Clamp(101)")))
        # The run saves test_gaps.json as it found it, and writes its examples.
        result, run = self.prices_review('cp "$WHOOPER_RUN/test_gaps.json" "${0%/*}/seen.json"\n'
                                         'cp "${0%/*}/examples.json" "${0%/*}/whooper_examples_test.go" "$WHOOPER_RUN/"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.fakes / "seen.json").read_text()), self.PRICES_GAPS)
        self.assertIn(f"\n- test_gaps.json: {run}/test_gaps.json, the changed Go, JavaScript, and TypeScript lines no test"
                      " ran, which Whooper measured before this run started\n", (run / "prompt.md").read_text())
        examples = {
            "functions": [{"name": "Clamp", "path": "prices/prices.go", "line": 9}],
            "rows": [example_row("Clamp", "Clamp(50)", "50", "50", "same", "ran_base_and_head"),
                     example_row("Clamp", "Clamp(101)", "101", "100", "different", "ran_base_and_head")],
            "limits": [CHOSE, ONCE_AT_HEAD],
        }
        self.assertEqual(json.loads((run / "examples.json").read_text()), examples)
        self.assertEqual(json.loads((run / "examples.request.json").read_text()), request)
        self.assertFalse((run / "head").exists() or (run / "base").exists())
        self.assertEqual(self.shown_fields(run, "outcome", "test_gaps", "examples"),
                         {"outcome": "reviewed", "test_gaps": self.PRICES_GAPS_MUTATED, "examples": examples})

    def test_the_run_can_neither_change_its_test_gaps_nor_write_its_examples_results(self):
        forged = {"functions": [], "limits": [],
                  "rows": [example_row("Clamp", "Clamp(101)", "101", "100", "different", "ran_base_and_head")]}
        (self.fakes / "forged.json").write_text(json.dumps(forged))
        result, run = self.prices_review('echo "{}" > "$WHOOPER_RUN/test_gaps.json"\n'
                                         'cp "${0%/*}/forged.json" "$WHOOPER_RUN/examples.json"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((run / "test_gaps.json").read_text()), self.PRICES_GAPS_MUTATED)
        self.assertEqual(json.loads((run / "examples.json").read_text()), {
            "functions": [], "rows": [], "limits": ["Whooper could not read the examples the review run chose, so it ran"
                                                    " none: examples.json lacks package, inputs."]})
        self.assertEqual(json.loads((run / "examples.request.json").read_text()), forged)

    def test_a_review_goes_on_when_its_examples_test_is_broken_or_missing(self):
        request = examples_request([("Clamp", 9)], [("Clamp", "Clamp(50)")])
        (self.fakes / "examples.json").write_text(json.dumps(request))
        for name, test, note in [
            ("broken", examples_test(("Clamp", "Clamp(50)", 'Clamp("50")')), "The test does not compile at the PR head: "),
            ("missing", None, "Whooper did not run the test: the review run wrote no whooper_examples_test.go."),
        ]:
            with self.subTest(name):
                shutil.rmtree(Path(self.tmp.name) / "origin", ignore_errors=True)
                shutil.rmtree(Path(self.tmp.name) / "cache", ignore_errors=True)
                sleep(1 - datetime.now().microsecond / 1e6)
                copy = 'cp "${0%/*}/examples.json" "$WHOOPER_RUN/"'
                if test is not None:
                    (self.fakes / "whooper_examples_test.go").write_text(test)
                    copy += '\ncp "${0%/*}/whooper_examples_test.go" "$WHOOPER_RUN/"'
                result, run = self.prices_review(copy)
                self.assertEqual(result.returncode, 0, result.stderr)
                shown = self.shown_fields(run, "outcome", "failure", "examples")
                self.assertEqual({key: shown[key] for key in ["outcome", "failure"]}, {"outcome": "reviewed", "failure": None})
                [row] = shown["examples"]["rows"]
                self.assertEqual((row["status"], row["label"]), ("error", "not_compared"))
                self.assertTrue(row["note"].startswith(note), row["note"])

    def test_a_review_goes_on_when_the_test_gaps_step_fails(self):
        # A directory named test_gaps.json, which the step cannot replace with its file, makes the step fail.
        self.setup_script('mkdir "$WHOOPER_RUN/test_gaps.json"')
        # The run writes a test_gaps.json of its own, which goes as the review ends.
        result, run = self.prices_review('rm -r "$WHOOPER_RUN/test_gaps.json"\necho "{}" > "$WHOOPER_RUN/test_gaps.json"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("whooper-review: the test-gaps step failed, so the review has no test_gaps.json\n", result.stderr)
        self.assertNotIn("\n- test_gaps.json:", (run / "prompt.md").read_text())
        self.assertEqual(self.shown_fields(run, "outcome", "failure", "test_gaps"),
                         {"outcome": "reviewed", "failure": None, "test_gaps": None})
        self.assertFalse((run / "test_gaps.json").exists())

    def test_a_review_goes_on_when_the_mutation_step_fails(self):
        # A gremlins that kills the step that started it, which then writes nothing.
        self.script(self.fakes / "gremlins", "kill -9 $PPID")
        result, run = self.prices_review("true", env={"PATH": self.env["PATH"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("whooper-review: the mutation step failed, so the review has no survived or caught lines in"
                      " test_gaps.json\n", result.stderr)
        self.assertEqual(self.shown_fields(run, "outcome", "failure", "test_gaps"),
                         {"outcome": "reviewed", "failure": None, "test_gaps": self.PRICES_GAPS})

    def test_gremlins_reads_the_lines_the_diff_adds_whatever_the_git_config_says(self):
        # Every setting but diff.external, which no git setting turns off.
        self.write_git_config(self.git_env(), [setting for setting in self.diff_settings()
                                               if "diff.external" not in setting])
        tmp = Path(self.tmp.name)
        # gremlins finds the changed lines with git diff --merge-base in the folder it starts in, and finds nothing to
        # change. The fake also has git print the same diff with no config but the mutation step's own, and with only
        # the step's own over the user's config.
        step = "-c diff.relative=true -c diff.context=0 -c color.diff=false"
        self.script(self.fakes / "gremlins", "\n".join([
            f'git diff --merge-base "$3" > "{tmp}/gremlins.diff"',
            f'GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_COUNT=0 git -c core.abbrev=12 {step} diff --merge-base "$3"'
            f' > "{tmp}/plain.diff"',
            f'GIT_CONFIG_COUNT=0 git {step} diff --merge-base "$3" > "{tmp}/config.diff"']))
        result, _ = self.prices_review("true")
        self.assertEqual(result.returncode, 0, result.stderr)
        plain = (tmp / "plain.diff").read_text()
        self.assertIn("\n+func Half(n int) int {\n", plain)
        self.assertNotEqual((tmp / "config.diff").read_text(), plain)
        self.assertEqual((tmp / "gremlins.diff").read_text(), plain)

    def test_gremlins_reads_the_lines_the_diff_adds_whatever_gits_diff_variables_say(self):
        # git takes an external diff tool from GIT_EXTERNAL_DIFF, and how many unchanged lines go around each change
        # from GIT_DIFF_OPTS, whatever its config says. The fake gremlins also has git print the same diff without
        # them, with no config but the mutation step's own.
        tmp = Path(self.tmp.name)
        self.script(tmp / "external", 'echo "external diff of $1"')
        step = "-c diff.relative=true -c diff.context=0 -c color.diff=false"
        self.script(self.fakes / "gremlins", "\n".join([
            f'git diff --merge-base "$3" > "{tmp}/gremlins.diff"',
            f'env -u GIT_EXTERNAL_DIFF -u GIT_DIFF_OPTS GIT_CONFIG_COUNT=0 git -c core.abbrev=12 {step} diff --merge-base "$3"'
            f' > "{tmp}/plain.diff"']))
        result, _ = self.prices_review("true", env={"GIT_EXTERNAL_DIFF": str(tmp / "external"), "GIT_DIFF_OPTS": "--unified=3"})
        self.assertEqual(result.returncode, 0, result.stderr)
        plain = (tmp / "plain.diff").read_text()
        self.assertIn("\n+func Half(n int) int {\n", plain)
        self.assertEqual((tmp / "gremlins.diff").read_text(), plain)

    def test_show_gives_the_commits_since_the_review_and_the_files_that_change_with_the_prs_from_the_clone(self):
        env = {**self.git_env(), "TZ": "America/Los_Angeles"}
        tmp = Path(self.tmp.name)
        origin, clone = tmp / "origin", tmp / "cache" / "whooper" / "repos" / "sample-org-plain-repo"
        now = datetime.now(timezone.utc).replace(microsecond=0)

        def git(*args, days_ago=0):
            when = (now - timedelta(days=days_ago)).isoformat()
            return subprocess.run(["git", *args], cwd=origin, env={**env, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
                                  check=True, capture_output=True, text=True).stdout.strip()

        def commit(message, *names, days_ago=0):
            """Commits a line added to each file of names, days_ago days before now, and returns its SHA."""
            for name in names:
                with (origin / name).open("a") as file:
                    file.write(f"{message}\n")
            git("add", *names)
            git("commit", "--quiet", "-m", message, days_ago=days_ago)
            return git("rev-parse", "HEAD")

        def at(days_ago):
            return (now - timedelta(days=days_ago)).astimezone(ZoneInfo("America/Los_Angeles")).isoformat()

        def newest_head(head):
            """Has the latest poll see PR 12 at head, after every record of its reviews."""
            (self.state / "poll").mkdir(exist_ok=True)
            (self.state / "poll" / "github.json").write_text(json.dumps([
                {"repository": {"nameWithOwner": "sample-org/plain-repo"}, "number": 12, "headRefOid": head}]))

        def shown():
            result = self.command("whooper-show", run.name, "--json", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            return {key: json.loads(result.stdout)[key] for key in ["commits_since_review", "co_change"]}

        origin.mkdir()
        git("init", "--quiet", "--initial-branch=main")
        # More than 90 days before the review, app.txt changed three times with old.txt. In the 90 days,
        # config.txt changed in 3 of app.txt's 4 commits, and docs.md in 2.
        for n in range(3):
            commit(f"old {n}", "app.txt", "old.txt", days_ago=100 - n)
        commit("a1", "app.txt", "config.txt", "docs.md", days_ago=80)
        commit("a2", "app.txt", "config.txt", "docs.md", days_ago=60)
        commit("a3", "app.txt", "config.txt", days_ago=40)
        commit("a4", "app.txt", days_ago=20)
        # PR 12 changes app.txt. After the commit its review reads, main changes app.txt and config.txt once
        # more, and the PR merges main, keeping its own app.txt, and adds a commit.
        git("checkout", "--quiet", "-b", "pr-12")
        head = commit("the PR", "app.txt", days_ago=3)
        git("checkout", "--quiet", "main")
        commit("later on main", "app.txt", "config.txt", days_ago=2)
        git("checkout", "--quiet", "pr-12")
        git("merge", "--quiet", "--no-edit", "--strategy-option=ours", "main", days_ago=1)
        merge = git("rev-parse", "HEAD")
        newest = commit("pr: tidy up", "app.txt", "notes.txt")
        git("clone", "--quiet", "--no-checkout", str(origin), str(clone))
        item = tmp / "item.json"
        item.write_text(json.dumps({"kind": "first_review", "repo": "sample-org/plain-repo", "pr": 12, "head_sha": head,
                                    "base_ref": "main", "source": "github_request", "slack": None, "reason": "r"}))
        # The review fails, since claude is the fake that exits 1, after it saved run.json and the diff.
        self.command("whooper-review", str(item), "--dry-run", env=env)
        [run] = (self.state / "runs").iterdir()
        # The newest head Whooper knows is the one the review read, from its record. config.txt's 3 of 4
        # commits count, read from the merge base back: the 3 commits before the 90 days would make them
        # 3 of 7, and main's later commit 4 of 5.
        co_changed = [{"path": "config.txt", "partner": "app.txt", "ratio": 0.75, "commits": 3}]
        self.assertEqual(shown(), {"commits_since_review": [], "co_change": co_changed})
        # Once the poll sees the PR's newest head, the commits since come newest first, the merge as one
        # commit with the files it brought from main.
        newest_head(newest)
        self.assertEqual(shown(), {"commits_since_review": [
            {"sha": newest, "title": "pr: tidy up", "author": "Sam", "at": at(0), "files": ["app.txt", "notes.txt"]},
            {"sha": merge, "title": "Merge branch 'main' into pr-12", "author": "Sam", "at": at(1),
             "files": ["config.txt"]},
        ], "co_change": co_changed})
        # The clone made again as README.md makes it, a partial clone, which fetches a file only when
        # something needs it, and a newest head it never fetched, which origin would hand over: the commits
        # since are unknown, git fetches nothing and writes nothing there, and the clone still lacks it.
        git("config", "uploadpack.allowFilter", "true")
        git("config", "uploadpack.allowAnySHA1InWant", "true")
        shutil.rmtree(clone)
        git("clone", "--quiet", "--filter=blob:none", "--no-checkout", f"file://{origin}", str(clone))
        unfetched = commit("after the clone", "app.txt")
        newest_head(unfetched)
        before = files(clone)
        self.assertEqual(shown(), {"commits_since_review": None, "co_change": co_changed})
        self.assertEqual(files(clone), before)
        lacks = subprocess.run(["git", "--git-dir", str(clone / ".git"), "--no-lazy-fetch", "cat-file", "-e", unfetched],
                               env=env, capture_output=True)
        self.assertNotEqual(lacks.returncode, 0)

    def test_a_failed_review_keeps_its_reason_and_an_ask_lists_it_first(self):
        env, item, _ = self.pull_request()
        self.fake_claude(review="exit 1")
        result = self.command("whooper-review", str(item), "--dry-run", env=env)
        [run] = (self.state / "runs").iterdir()
        reason = f"the review run exited 1 without findings; see {run}/stderr.log and the transcript in {run}"
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"whooper-review: {reason}\n", result.stderr)
        # The cleanup keeps die's reason rather than writing its own fixed one.
        self.assertEqual((run / "failure.txt").read_text(), f"{reason}\n")
        self.assertEqual(json.loads((run / "failure.json").read_text()), CAUSES["agent_no_findings"])
        ask = self.command("whooper-ask", run.name, "Why did this review fail?", env=env)
        self.assertEqual((ask.returncode, ask.stdout), (0, "It stopped.\n"), ask.stderr)
        prompt = (run / "asks" / "1.prompt.md").read_text()
        listed = [line for line in prompt.splitlines() if line.startswith(f"- {run}/")]
        self.assertTrue(listed[0].startswith(f"- {run / 'failure.txt'}: "), listed)

    def test_an_interrupted_review_saves_that_it_was_interrupted(self):
        env, item, _ = self.pull_request()
        agent_pid = Path(self.tmp.name) / "agent.pid"
        # The review run notes its process ID and waits.
        self.fake_claude(review=f"echo $$ > '{agent_pid}'; exec sleep 30")
        review = subprocess.Popen([str(ROOT / "bin" / "whooper-review"), str(item), "--dry-run"], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.stop, review)
        self.wait_for(lambda: self.read(agent_pid), "the review run to start")
        # README.md stops a review with a TERM to whooper-review alone.
        review.terminate()
        review.wait(timeout=30)
        [run] = (self.state / "runs").iterdir()
        self.assertEqual((review.returncode, self.read(run / "failure.txt")), (130, "the review was interrupted\n"))
        self.assertEqual(json.loads((run / "failure.json").read_text()), CAUSES["unknown"])

    def test_a_command_stopped_before_its_run_makes_its_process_group_leaves_none_of_the_run_running(self):
        # lib/python runs uv before Python, which starts a review, poll, or ask run, or a step of a review, in a session
        # of its own, its process group. A fake uv stops the lib/python that starts it there, before it has made the
        # group, as if it lost the processor there, and the test meanwhile stops the command with a TERM to it alone,
        # as whooper-skip stops a review. The starting process goes on only once the command's cleanup has stopped
        # whatever group the command knew of and comes to the rm that removes its work folder, which a fake rm holds
        # until the starting process ends. The command exits without waiting for the run, and the run never starts,
        # under bash 3.2 and 5 alike: neither claude, for a run, nor the test-gaps step's own lib/python ran.
        env, item, _ = self.pull_request()
        self.fake_review_requests()
        run = self.state / "runs" / "20310727T010246Z-12"
        run.mkdir(parents=True)
        (run / "failure.txt").write_text("the review stopped at an unexpected error\n")
        uv, rm, tmp = shutil.which("uv"), shutil.which("rm"), Path(self.tmp.name)

        def alive(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        # (the command and its arguments, what the command line of the lib/python that starts the run names, and the
        # start of the command line of the step's own lib/python, for the step)
        for case, (name, args, starts, step) in enumerate([
            ("whooper-review", [str(item), "--dry-run"], "/agents/claude/review ", None),
            ("whooper-review", [str(item), "--dry-run"], "/lib/whooper.py test-gaps ",
             f"bash {ROOT}/lib/python {ROOT}/lib/whooper.py test-gaps "),
            ("whooper-poll", [], "/agents/claude/poll ", None),
            ("whooper-ask", [run.name, "Why did it stop?"], "/agents/claude/ask ", None),
        ]):
            for n, bash in enumerate(BASHES):
                with self.subTest(name, starts=starts, bash=str(bash)):
                    paused, ran = tmp / f"paused-{case}-{n}", tmp / f"ran-{case}-{n}"
                    self.claude_script(f"touch '{ran}'\nexec sleep 30")
                    self.script(self.fakes / "uv", "\n".join([
                        'parent="$(ps -ww -o command= -p "$PPID")"',
                        f'case "$parent" in *"{starts}"*) if mkdir "{paused}" 2>/dev/null; then',
                        f'  echo "$PPID" > "{paused}/pid.tmp" && mv "{paused}/pid.tmp" "{paused}/pid"',
                        f'  until [ -e "{paused}/go" ]; do sleep 0.1; done',
                        "fi ;; esac",
                        *([f'case "$parent" in "{step}"*) touch "{ran}" ;; esac'] if step else []),
                        f'exec "{uv}" "$@"',
                    ]))
                    self.script(self.fakes / "rm", "\n".join([
                        f'if [ -e "{paused}/pid" ] && mkdir "{paused}/released" 2>/dev/null; then',
                        f'  touch "{paused}/go"',
                        f'  while kill -0 "$(cat "{paused}/pid")" 2>/dev/null; do sleep 0.1; done',
                        "fi",
                        f'exec "{rm}" "$@"',
                    ]))
                    folder = bash_folder(bash, tmp / f"bash-{case}-{n}")
                    command = self.start_command(name, *args, env={**env, "PATH": f"{folder}{os.pathsep}{env['PATH']}"})
                    self.wait_for(lambda: (paused / "pid").exists() or command.poll() is not None,
                                  f"{name} to start the run")
                    starter = int((paused / "pid").read_text())
                    os.kill(command.pid, signal.SIGTERM)
                    with suppress(subprocess.TimeoutExpired):
                        command.wait(timeout=30)
                    exited = command.returncode
                    # A cleanup that waits for the run never comes to the rm.
                    (paused / "go").touch()
                    self.wait_for(lambda: not alive(starter), "the process that starts the run to end")
                    self.assertEqual((exited, ran.exists()), (130, False), command.communicate()[1])

    def test_a_stop_that_misses_the_review_or_round_being_started_stops_it_and_its_parent_at_once(self):
        # A Ctrl-C or kill -- -<pid> that lands while whooper-round forks whooper-review, or whooper-run forks
        # whooper-round, reaches the parent but can miss the child being forked, and the parent then stopped only once
        # that child had run to its end: a whole review, or a whole round. A fake uv stops the child's first lib/python
        # as it starts, and the test meanwhile sends TERM to the parent alone, standing in for the stop the child
        # missed. The child goes on only once the parent has exited, or after 30 seconds. Under bash 3.2 and 5 alike,
        # the parent exits 130 at once, and the child never runs: after the pause, the child itself, whooper-review or
        # whooper-round, starts no lib/python of its own, which the fake uv notes in ran.
        self.fake_one_review_request()
        uv, tmp = shutil.which("uv"), Path(self.tmp.name)

        def alive(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        # (the parent, and what the command line of the child, or of the lib/python that starts it, names)
        for case, (name, child) in enumerate([("whooper-round", "/bin/whooper-review "), ("whooper-run", "/bin/whooper-round")]):
            for n, bash in enumerate(BASHES):
                with self.subTest(name, bash=str(bash)):
                    paused, ran, home = tmp / f"paused-{case}-{n}", tmp / f"ran-{case}-{n}", tmp / f"state-{case}-{n}"
                    # The fake uv's parent is a lib/python, and the lib/python's parent the script that runs it.
                    self.script(self.fakes / "uv", "\n".join([
                        'parent="$(ps -ww -o command= -p "$PPID")"',
                        'script="$(ps -ww -o command= -p "$(ps -o ppid= -p "$PPID" | tr -d " ")")"',
                        f'case "$parent $script" in *"{child}"*)',
                        f'  if mkdir "{paused}" 2>/dev/null; then',
                        f'    echo "$PPID" > "{paused}/pid.tmp" && mv "{paused}/pid.tmp" "{paused}/pid"',
                        f'    until [ -e "{paused}/go" ]; do sleep 0.1; done',
                        f'  else case "$script" in *"{child}"*) touch "{ran}" ;; esac; fi ;;',
                        "esac",
                        f'exec "{uv}" "$@"',
                    ]))
                    folder = bash_folder(bash, tmp / f"bash-{case}-{n}")
                    command = self.start_command(name, env={**self.env, "XDG_STATE_HOME": str(home),
                                                            "PATH": f"{folder}{os.pathsep}{self.env['PATH']}"})
                    self.wait_for(lambda: (paused / "pid").exists() or command.poll() is not None,
                                  f"{name} to start {child.strip()}")
                    starter = int((paused / "pid").read_text())
                    os.kill(command.pid, signal.SIGTERM)
                    with suppress(subprocess.TimeoutExpired):
                        command.wait(timeout=30)
                    exited = command.returncode
                    (paused / "go").touch()
                    self.wait_for(lambda: not alive(starter), "the process that starts the child to end")
                    self.assertEqual((exited, ran.exists()), (130, False), command.communicate()[1])
                    if name == "whooper-round":
                        # The round, which stopped the review before it started, lists it as interrupted.
                        last = json.loads((home / "whooper" / "last-round.json").read_text())
                        self.assertEqual((last["result"], last["reviews"]),
                                         ("interrupted", [{"pr": "example-org/example-repo#12", "outcome": "interrupted"}]))

    def test_a_stop_that_misses_the_fetch_poll_or_summary_being_started_stops_it_and_the_round_at_once(self):
        # As for a review, a Ctrl-C or kill -- -<pid> that lands while whooper-round forks whooper-fetch, whooper-poll,
        # or whooper-summary reaches the round but can miss the child being forked, and the round then stopped only
        # once that child had run to its end, which for the summary's agent has no time limit. A fake uv stops the
        # child's first lib/python as it starts, and the test meanwhile sends TERM to the round alone, standing in for
        # the stop the child missed. The child goes on only once the round has exited, or after 10 seconds. Under bash
        # 3.2 and 5 alike, the round exits 130 at once, and the child never runs: after the pause, the child itself
        # starts no lib/python of its own, which the fake uv notes in ran. The round reaches the summary once its
        # review succeeds.
        env, item, _ = self.pull_request()
        head = json.loads(item.read_text())["head_sha"]
        uv, tmp = shutil.which("uv"), Path(self.tmp.name)
        self.fake_review_requests({**pr(number=12, head=head, requested=[LOGIN]),
                                   "repository": {"nameWithOwner": "sample-org/plain-repo"}, "baseRefName": "main",
                                   "url": "https://github.com/sample-org/plain-repo/pull/12"})
        # The review run, which starts in the head worktree, finds nothing; the poll's run finds no asks.
        (self.fakes / "result.json").write_text(json.dumps({
            "type": "result", "subtype": "success", "total_cost_usd": 0, "duration_ms": 0, "num_turns": 1,
            "structured_output": {"status": "reviewed", "skip_reason": "", "findings": [],
                                  "approval": {"verdict": "approve_once_ci_passes", "reason": "Read the diff."}}}))
        poll = (self.fakes / "claude").read_text().split("\n", 1)[1]
        self.claude_script(f'case "$PWD" in */head) cat "${{0%/*}}/result.json"; exit ;; esac\n{poll}')

        def alive(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        for case, child in enumerate(["/bin/whooper-fetch", "/bin/whooper-poll", "/bin/whooper-summary "]):
            for n, bash in enumerate(BASHES):
                with self.subTest(child.strip(), bash=str(bash)):
                    paused, ran, home = tmp / f"paused-{case}-{n}", tmp / f"ran-{case}-{n}", tmp / f"state-{case}-{n}"
                    # The fake uv's parent is a lib/python, and the lib/python's parent the script that runs it.
                    self.script(self.fakes / "uv", "\n".join([
                        'parent="$(ps -ww -o command= -p "$PPID")"',
                        'script="$(ps -ww -o command= -p "$(ps -o ppid= -p "$PPID" | tr -d " ")")"',
                        f'case "$parent $script" in *"{child}"*)',
                        f'  if mkdir "{paused}" 2>/dev/null; then',
                        f'    echo "$PPID" > "{paused}/pid.tmp" && mv "{paused}/pid.tmp" "{paused}/pid"',
                        f'    until [ -e "{paused}/go" ]; do sleep 0.1; done',
                        f'  else case "$script" in *"{child}"*) touch "{ran}" ;; esac; fi ;;',
                        "esac",
                        f'exec "{uv}" "$@"',
                    ]))
                    folder = bash_folder(bash, tmp / f"bash-{case}-{n}")
                    command = self.start_command("whooper-round", env={**env, "XDG_STATE_HOME": str(home),
                                                                       "PATH": f"{folder}{os.pathsep}{env['PATH']}"})
                    self.wait_for(lambda: (paused / "pid").exists() or command.poll() is not None,
                                  f"whooper-round to start {child.strip()}")
                    starter = int((paused / "pid").read_text())
                    os.kill(command.pid, signal.SIGTERM)
                    with suppress(subprocess.TimeoutExpired):
                        command.wait(timeout=10)
                    exited = command.returncode
                    (paused / "go").touch()
                    self.wait_for(lambda: not alive(starter), "the process that starts the child to end")
                    self.assertEqual((exited, ran.exists()), (130, False), command.communicate()[1])
                    last = json.loads((home / "whooper" / "last-round.json").read_text())
                    self.assertEqual(last["result"], "interrupted")

    def test_a_stop_reaches_the_setup_or_teardown_script_and_kills_it_4_seconds_later_if_it_ignores_term(self):
        # whooper-review ran the repo's setup and teardown scripts in the foreground, so a stop that missed the script
        # as it was forked, standing in here for a TERM to whooper-review alone, waited for the script to end, and a
        # stop that came while the cleanup ran the teardown script never reached it. Here the script ignores TERM and
        # waits 10 minutes. Under bash 3.2 and 5 alike, whooper-review stops it, with a SIGKILL once it ignores the
        # TERM for 4 seconds, and exits within 8 seconds: 130 when the stop came during the setup script, whose review
        # it interrupted, and 1 when it came during the teardown script of a review that had already failed, which
        # then removes its worktrees and lock as usual.
        env, item, _ = self.pull_request()
        tmp = Path(self.tmp.name)
        self.fake_claude(review="exit 1")
        for name, setup in [("setup", self.setup_script), ("teardown", self.teardown_script)]:
            setup("\n".join([
                f'if [ "$WHOOPER_TEST_HANG" = {name} ]; then',
                f'  echo $$ > "$WHOOPER_TEST_PIDS/{name}"',
                '  trap "" TERM',
                "  exec sleep 600",
                "fi",
            ]))
        lock = self.state / "locks" / "sample-org-plain-repo-12"
        for case, (script, code) in enumerate([("setup", 130), ("teardown", 1)]):
            for n, bash in enumerate(BASHES):
                with self.subTest(script, bash=str(bash)):
                    pids = tmp / f"pids-{case}-{n}"
                    pids.mkdir()
                    folder = bash_folder(bash, tmp / f"bash-{case}-{n}")
                    # The output goes to a file, which a script the stop missed cannot hold open as it can a pipe.
                    log = tmp / f"review-{case}-{n}.log"
                    with log.open("w") as out:
                        review = subprocess.Popen(
                            [str(ROOT / "bin" / "whooper-review"), str(item), "--dry-run"], stdout=out,
                            stderr=subprocess.STDOUT, start_new_session=True,
                            env={**env, "WHOOPER_TEST_HANG": script, "WHOOPER_TEST_PIDS": str(pids),
                                 "PATH": f"{folder}{os.pathsep}{env['PATH']}"})
                    self.addCleanup(self.stop, review)
                    self.wait_for(lambda: (pids / script).exists() or review.poll() is not None,
                                  f"the {script} script to start")
                    hung = int((pids / script).read_text())
                    self.addCleanup(self.kill_groups, [hung])
                    review.terminate()
                    with suppress(subprocess.TimeoutExpired):
                        review.wait(timeout=8)
                    deadline = monotonic() + 2
                    while running(hung) and monotonic() < deadline:
                        sleep(0.1)
                    [run] = (self.state / "runs").iterdir()
                    ended = (review.returncode, running(hung), lock.exists(), (run / "head").exists(),
                             (run / "base").exists())
                    # A stop that misses the script leaves it and the review running, which the next case must not
                    # find.
                    self.kill_groups([hung, review.pid])
                    review.wait()
                    shutil.rmtree(run)
                    shutil.rmtree(lock, ignore_errors=True)
                    self.assertEqual(ended, (code, False, False, False, False), log.read_text())

    def test_a_stop_as_a_command_records_its_watchdog_leaves_no_watchdog_running_under_each_bash(self):
        # bash 3.2 can run the INT, TERM, and HUP trap between watchdog ... & and the line after it, which records
        # watchdog_pid, so the cleanup found no watchdog to stop, and the watchdog slept to the time limit, holding the
        # command's output open. BASH_ENV, which bash runs before a script, sets a DEBUG trap that sends the script a
        # TERM just before that line, which bash 3.2 and 5 alike then trap before they run the line. whooper-review,
        # whooper-poll, and whooper-ask each exit 130, and nothing is left in the command's process group, the
        # watchdog's included.
        env, item, _ = self.pull_request()
        self.fake_review_requests()
        self.claude_script("exec sleep 30")
        run = self.state / "runs" / "20310727T010246Z-12"
        run.mkdir(parents=True)
        (run / "failure.txt").write_text("the review stopped at an unexpected error\n")
        tmp = Path(self.tmp.name)
        bash_env = tmp / "debug.bash"
        bash_env.write_text("""trap '[[ "$BASH_COMMAND" != "watchdog_pid=\\$!" ]] || kill -TERM $$' DEBUG\n""")
        for case, (name, args) in enumerate([("whooper-review", [str(item), "--dry-run"]), ("whooper-poll", []),
                                             ("whooper-ask", [run.name, "Why did it stop?"])]):
            for n, bash in enumerate(BASHES):
                with self.subTest(name, bash=str(bash)):
                    folder = bash_folder(bash, tmp / f"bash-{case}-{n}")
                    # The output goes to a file, which a watchdog left running cannot hold open as it can a pipe.
                    log = tmp / f"{name}-{n}.log"
                    with log.open("w") as out:
                        command = subprocess.Popen(
                            [str(ROOT / "bin" / name), *args], stdout=out, stderr=subprocess.STDOUT,
                            start_new_session=True, env={**env, "BASH_ENV": str(bash_env),
                                                         "PATH": f"{folder}{os.pathsep}{env['PATH']}"})
                    self.addCleanup(self.stop, command)
                    command.wait(timeout=60)

                    def left():
                        return subprocess.run(["pgrep", "-g", str(command.pid)], capture_output=True,
                                              text=True).stdout.split()

                    deadline = monotonic() + 3
                    while left() and monotonic() < deadline:
                        sleep(0.1)
                    self.assertEqual((command.returncode, left()), (130, []), log.read_text())

    def test_a_second_signal_while_whooper_run_or_a_round_stops_its_child_leaves_no_lock_behind(self):
        # whooper-run's EXIT trap stops its round, then removes run.lock, and whooper-round's stops its review, then
        # writes last-round.json and removes round.lock. A second INT, TERM, or HUP, as a second Ctrl-C, ran the trap
        # that exits, which ended the EXIT trap before the lock went. Here the review run ignores TERM, so each stop
        # of its child takes 4 seconds, and the test sends the second TERM to the command's group once it waits in
        # lib/stop-group. Under bash 3.2 and 5 alike, the command exits 130 once its child has ended, and its lock is
        # gone; the round also records itself as interrupted.
        env, item, _ = self.pull_request()
        head = json.loads(item.read_text())["head_sha"]
        tmp = Path(self.tmp.name)
        self.fake_review_requests({**pr(number=12, head=head, requested=[LOGIN]),
                                   "repository": {"nameWithOwner": "sample-org/plain-repo"}, "baseRefName": "main",
                                   "url": "https://github.com/sample-org/plain-repo/pull/12"})
        poll = (self.fakes / "claude").read_text().split("\n", 1)[1]
        self.claude_script("\n".join([
            'case "$PWD" in */head) echo $$ > "$WHOOPER_TEST_PIDS/run"; trap "" TERM; exec sleep 600 ;; esac',
            poll,
        ]))
        for case, (name, lock) in enumerate([("whooper-run", "run.lock"), ("whooper-round", "round.lock")]):
            for n, bash in enumerate(BASHES):
                with self.subTest(name, bash=str(bash)):
                    home, pids = tmp / f"state-{case}-{n}", tmp / f"pids-{case}-{n}"
                    pids.mkdir()
                    folder = bash_folder(bash, tmp / f"bash-{case}-{n}")
                    log = tmp / f"{name}-{n}.log"
                    with log.open("w") as out:
                        command = subprocess.Popen(
                            [str(ROOT / "bin" / name)], stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                            env={**env, "XDG_STATE_HOME": str(home), "WHOOPER_TEST_PIDS": str(pids),
                                 "PATH": f"{folder}{os.pathsep}{env['PATH']}"})
                    self.addCleanup(self.stop, command)
                    self.wait_for(lambda: (pids / "run").exists() or command.poll() is not None,
                                  "the review run to start")
                    self.addCleanup(self.kill_groups, [int((pids / "run").read_text())])
                    os.killpg(command.pid, signal.SIGTERM)
                    self.wait_for(lambda: command.poll() is not None or subprocess.run(
                        ["pgrep", "-P", str(command.pid), "-f", "lib/stop-group"], capture_output=True).returncode == 0,
                                  f"{name} to stop its child")
                    with suppress(ProcessLookupError):
                        os.killpg(command.pid, signal.SIGTERM)
                    with suppress(subprocess.TimeoutExpired):
                        command.wait(timeout=10)
                    state = home / "whooper"
                    self.assertEqual((command.returncode, (state / lock).exists()), (130, False), log.read_text())
                    if name == "whooper-round":
                        self.assertEqual(json.loads((state / "last-round.json").read_text())["result"], "interrupted")

    def test_a_stop_of_whooper_run_ends_its_round_review_and_run_before_prompting_times_sigkill_whatever_ignores_term(self):
        # Prompting Time stops whooper-run with a TERM to its process group and, 5 seconds later, a SIGKILL to it. The
        # round and the review run in process groups of their own, which neither signal reaches, so each parent's
        # cleanup stops its child's group, and the review's cleanup stops the run's and then runs the repo's teardown
        # script. Here either the review run ignores TERM, or the teardown script does and waits 10 minutes, a cleanup
        # that hangs. Under bash 3.2 and 5 alike, whooper-run exits 130 on its own before the SIGKILL, and nothing of the
        # round is left: whooper-round, whooper-review, the run, and the teardown script have all ended.
        env, item, _ = self.pull_request()
        head = json.loads(item.read_text())["head_sha"]
        tmp = Path(self.tmp.name)
        self.fake_review_requests({**pr(number=12, head=head, requested=[LOGIN]),
                                   "repository": {"nameWithOwner": "sample-org/plain-repo"}, "baseRefName": "main",
                                   "url": "https://github.com/sample-org/plain-repo/pull/12"})
        # The review run, which starts in the head worktree, notes its process ID in the folder WHOOPER_TEST_PIDS names
        # and waits, ignoring TERM when WHOOPER_TEST_RUN_IGNORES_TERM is set; the poll's run finds no asks. The teardown
        # script notes its own and returns, or ignores TERM and waits when WHOOPER_TEST_TEARDOWN_HANGS is set.
        poll = (self.fakes / "claude").read_text().split("\n", 1)[1]
        self.claude_script("\n".join([
            'case "$PWD" in */head)',
            '  echo $$ > "$WHOOPER_TEST_PIDS/run"',
            '  [ -z "$WHOOPER_TEST_RUN_IGNORES_TERM" ] || trap "" TERM',
            "  exec sleep 600 ;;",
            "esac",
            poll,
        ]))
        self.teardown_script("\n".join([
            'echo $$ > "$WHOOPER_TEST_PIDS/teardown"',
            '[ -n "$WHOOPER_TEST_TEARDOWN_HANGS" ] || exit 0',
            'trap "" TERM',
            "exec sleep 600",
        ]))
        for case, (what, hang) in enumerate([("the run ignores TERM", {"WHOOPER_TEST_RUN_IGNORES_TERM": "1"}),
                                             ("the teardown hangs", {"WHOOPER_TEST_TEARDOWN_HANGS": "1"})]):
            for n, bash in enumerate(BASHES):
                with self.subTest(what, bash=str(bash)):
                    home, pids = tmp / f"state-{case}-{n}", tmp / f"pids-{case}-{n}"
                    pids.mkdir()
                    folder = bash_folder(bash, tmp / f"bash-{case}-{n}")
                    # Its output goes to a file, which a process the stop missed cannot hold open as it can a pipe.
                    log = tmp / f"run-{case}-{n}.log"
                    with log.open("w") as out:
                        loop = subprocess.Popen([str(ROOT / "bin" / "whooper-run")], stdout=out, stderr=subprocess.STDOUT,
                                                start_new_session=True, env={
                                                    **env, **hang, "XDG_STATE_HOME": str(home),
                                                    "WHOOPER_TEST_PIDS": str(pids),
                                                    "PATH": f"{folder}{os.pathsep}{env['PATH']}"})
                    self.addCleanup(self.stop, loop)
                    self.wait_for(lambda: (pids / "run").exists() or loop.poll() is not None, "the review run to start")
                    state = home / "whooper"
                    started = {"whooper-round": int((state / "run.lock" / "round.group").read_text()),
                               "whooper-review": int((state / "locks" / "sample-org-plain-repo-12" / "pid").read_text()),
                               "the run": int((pids / "run").read_text())}
                    self.addCleanup(self.kill_groups, list(started.values()))
                    os.killpg(loop.pid, signal.SIGTERM)
                    try:
                        loop.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(loop.pid, signal.SIGKILL)
                        loop.wait()
                    if (pids / "teardown").exists():
                        started["the teardown script"] = int((pids / "teardown").read_text())
                        self.addCleanup(self.kill_groups, [started["the teardown script"]])
                    if hang.get("WHOOPER_TEST_TEARDOWN_HANGS"):
                        self.assertIn("the teardown script", started)
                    deadline = monotonic() + 3
                    while (left := [name for name, pid in started.items() if running(pid)]) and monotonic() < deadline:
                        sleep(0.1)
                    self.assertEqual((loop.returncode, left), (130, []), log.read_text())

    def test_a_time_limit_that_passes_before_the_run_makes_its_process_group_keeps_the_run_from_starting(self):
        # A fake uv stops the lib/python that starts a review, poll, or ask run, before it has made the run's process
        # group, and lets it go on only once the group file the run would write its group's ID to exists, or after 30
        # seconds. Meanwhile the run's time limit passes: [ask] timeout_minutes takes a fraction of a minute, so the ask
        # runs a 0.01-minute limit, while a review's and a poll's limits are whole minutes, of at least 1 and 5, so for
        # them a fake sleep stands in for the limit passing: the watchdog's sleep, which the command itself starts,
        # ends at once. Under bash 3.2 and 5 alike, the watchdog stops the run through its group file, as the cleanup
        # does, so the run never starts, claude never runs, and the command stops at the limit and says so.
        env, item, _ = self.pull_request()
        self.fake_review_requests()
        run = self.state / "runs" / "20310727T010246Z-12"
        run.mkdir(parents=True)
        (run / "failure.txt").write_text("the review stopped at an unexpected error\n")
        config = Path(self.env["WHOOPER_CONFIG"])
        config.write_text(config.read_text() + "\n[ask]\ntimeout_minutes = 0.01\n")
        uv, sleep, tmp = shutil.which("uv"), shutil.which("sleep"), Path(self.tmp.name)

        def alive(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        # (the command and its arguments, what the command line of the lib/python that starts the run names, whose
        # sleeps the fake sleep ends at once, if any, and what the command says at the limit)
        for case, (name, args, starts, quick, limit) in enumerate([
            ("whooper-review", [str(item), "--dry-run"], "/agents/claude/review ", "/bin/whooper-review ",
             "the review run passed its 45-minute limit"),
            ("whooper-poll", [], "/agents/claude/poll ", "/bin/whooper-poll", "the claude poll run passed its 10-minute limit"),
            ("whooper-ask", [run.name, "Why did it stop?"], "/agents/claude/ask ", None,
             "the ask run passed its 0.01-minute limit"),
        ]):
            for n, bash in enumerate(BASHES):
                with self.subTest(name, bash=str(bash)):
                    paused, ran = tmp / f"limit-paused-{case}-{n}", tmp / f"limit-ran-{case}-{n}"
                    self.claude_script(f"touch '{ran}'\nexec sleep 30")
                    # The lib/python that starts the run is the fake uv's parent, and its command line names the group
                    # file right after session.
                    self.script(self.fakes / "uv", "\n".join([
                        'parent="$(ps -ww -o command= -p "$PPID")"',
                        f'case "$parent" in *"{starts}"*) if mkdir "{paused}" 2>/dev/null; then',
                        f'  echo "$PPID" > "{paused}/pid.tmp" && mv "{paused}/pid.tmp" "{paused}/pid"',
                        '  group="${parent#* session }"',
                        f'  until [ -e "${{group%% *}}" ] || [ -e "{paused}/go" ]; do sleep 0.1; done',
                        "fi ;; esac",
                        f'exec "{uv}" "$@"',
                    ]))
                    self.script(self.fakes / "sleep", "\n".join([
                        *([f'case "$(ps -ww -o command= -p "$PPID")" in *"{quick}"*) exit 0 ;; esac'] if quick else []),
                        f'exec "{sleep}" "$@"',
                    ]))
                    folder = bash_folder(bash, tmp / f"limit-bash-{case}-{n}")
                    command = self.start_command(name, *args, env={**env, "PATH": f"{folder}{os.pathsep}{env['PATH']}"})
                    self.wait_for(lambda: (paused / "pid").exists() or command.poll() is not None,
                                  f"{name} to start the run")
                    starter = int((paused / "pid").read_text())
                    with suppress(subprocess.TimeoutExpired):
                        command.wait(timeout=30)
                    exited = command.returncode
                    (paused / "go").touch()
                    self.wait_for(lambda: not alive(starter), "the process that starts the run to end")
                    stderr = command.communicate()[1]
                    self.assertEqual((exited, ran.exists()), (1, False), stderr)
                    self.assertIn(limit, stderr)

    def test_skipping_a_review_a_round_runs_stops_it_quietly_and_holds_back_its_pr_until_undone(self):
        env, item, _ = self.pull_request()
        head = json.loads(item.read_text())["head_sha"]
        agent_pid = Path(self.tmp.name) / "agent.pid"
        self.fake_review_requests({**pr(number=12, head=head, requested=[LOGIN]),
                                   "repository": {"nameWithOwner": "sample-org/plain-repo"}, "baseRefName": "main",
                                   "url": "https://github.com/sample-org/plain-repo/pull/12"})
        # The review run, which starts in the head worktree, notes its process ID and waits; the poll's
        # run finds no asks.
        poll = (self.fakes / "claude").read_text().split("\n", 1)[1]
        self.claude_script(f"case \"$PWD\" in */head) echo $$ > '{agent_pid}'; exec sleep 30 ;; esac\n{poll}")
        round_ = subprocess.Popen([str(ROOT / "bin" / "whooper-round")], env={**self.env, **env}, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.addCleanup(self.stop, round_)
        self.wait_for(lambda: self.read(agent_pid), "the review run to start")
        [run] = (self.state / "runs").iterdir()
        skipped = self.command("whooper-skip", run.name, env=env)
        self.assertEqual((skipped.returncode, skipped.stdout), (
            0, f"marked {run.name} (sample-org/plain-repo#12) as skipped, without submitting it, and stopped its review;"
               f" Whooper starts no other review of the PR until bin/whooper-skip {run.name} --undo unmarks it\n"),
            skipped.stderr)
        out, _ = round_.communicate(timeout=60)
        # The round goes on, and sends no notice of a review the user stopped.
        self.assertEqual(round_.returncode, 0, out)
        self.assertIn("review stopped: skipped by the user\n", out)
        self.assertFalse(self.notifications.exists())
        self.assertEqual(self.read(run / "failure.txt"), "the review was interrupted\n")
        [review] = self.status()["reviews"]
        self.assertEqual((review["outcome"], review["skipped_by_user"], review["needs_you"]), ("failed", True, False))
        self.assertEqual(self.status()["items"][0]["group"], "done")
        # The poll still lists the PR, and whooper-select drops it until the skip is undone.
        select = self.command("whooper-select", env=env)
        self.assertEqual(select.stdout, "")
        self.assertIn("dropped sample-org/plain-repo#12 first_review: skipped by the user", select.stderr)
        self.assertEqual(self.command("whooper-skip", run.name, "--undo", env=env).returncode, 0)
        select = self.command("whooper-select", env=env)
        self.assertEqual([json.loads(line)["pr"] for line in select.stdout.splitlines()], [12], select.stderr)

    def test_a_review_stopped_by_a_failed_command_saves_a_fixed_reason(self):
        env, item, _ = self.pull_request()
        # origin has no such branch, so the fetch fails once the run directory exists.
        item.write_text(json.dumps({**json.loads(item.read_text()), "base_ref": "no-such-branch"}))
        result = self.command("whooper-review", str(item), "--dry-run", env=env)
        [run] = (self.state / "runs").iterdir()
        self.assertEqual(result.returncode, 128)
        # git's own message is in whooper-review's error output, where failure.txt points.
        self.assertIn("couldn't find remote ref no-such-branch", result.stderr)
        self.assertEqual(self.read(run / "failure.txt"),
                         "the review stopped at an unexpected error; see whooper-review's error output\n")
        self.assertEqual(json.loads((run / "failure.json").read_text()), CAUSES["unknown"])

    def test_a_review_stopped_by_an_unset_variable_fails_under_each_bash(self):
        # The repo's setup script hands the run a line that reads a variable nothing sets, so whooper-review
        # stops at an unset variable once its run directory exists, and bash 3.2 then runs the cleanup with $?
        # at 0.
        env, item, _ = self.pull_request()
        self.setup_script("echo 'EXAMPLE_HOST=$EXAMPLE_UNSET' >> \"$WHOOPER_SETUP_ENV\"")
        for n, bash in enumerate(BASHES):
            with self.subTest(bash=str(bash)):
                folder = bash_folder(bash, Path(self.tmp.name) / f"bash-{n}")
                result = self.command("whooper-review", str(item), "--dry-run",
                                      env={**env, "PATH": f"{folder}{os.pathsep}{env['PATH']}"})
                [run] = (self.state / "runs").iterdir()
                failure = self.read(run / "failure.txt")
                shutil.rmtree(run)
                self.assertIn("EXAMPLE_UNSET: unbound variable", result.stderr)
                # whooper-round counts a review that exits 0 as reviewed.
                self.assertEqual((result.returncode, failure),
                                 (1, "the review stopped at an unexpected error; see whooper-review's error output\n"))

    def test_a_review_limit_typed_as_a_float_gives_the_watchdog_that_many_minutes_under_each_bash(self):
        # whooper-review's watchdog sleeps the limit times 60, which bash multiplies, and bash 3.2 and 5 both stop the
        # review at a syntax error on 45.0. With [review] timeout_minutes = 45.0, edited by hand, the watchdog sleeps
        # 2700 seconds under each bash, as with 45. The test reads them from the watchdog's sleep, the only sleep in
        # whooper-review's process group, while the review run, whose fake claude sleeps in a group of its own, waits.
        config = Path(self.env["WHOOPER_CONFIG"])
        config.write_text(config.read_text().replace("timeout_minutes = 45\n", "timeout_minutes = 45.0\n"))
        env, item, _ = self.pull_request()
        self.fake_claude(review="exec sleep 30")

        def watchdog_seconds(review):
            found = subprocess.run(["pgrep", "-g", str(review.pid), "-x", "sleep"], capture_output=True,
                                   text=True).stdout.split()
            commands = [subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout
                        for pid in found]
            return next((m[1] for c in commands if (m := re.fullmatch(r"sleep (\S+)", c.strip()))), None)

        for n, bash in enumerate(BASHES):
            with self.subTest(bash=str(bash)):
                folder = bash_folder(bash, Path(self.tmp.name) / f"bash-{n}")
                review = self.start_command("whooper-review", str(item), "--dry-run",
                                            env={**env, "PATH": f"{folder}{os.pathsep}{env['PATH']}"})
                self.wait_for(lambda: review.poll() is not None or watchdog_seconds(review),
                              "whooper-review to stop or start its watchdog")
                seconds = watchdog_seconds(review)
                self.stop(review)
                shutil.rmtree(self.state / "runs", ignore_errors=True)
                self.assertEqual(seconds, "2700", review.communicate()[1])

    def test_a_poll_or_an_ask_stopped_by_an_unset_variable_fails_under_each_bash(self):
        # BASH_ENV, which bash reads before a script, gives the script a command that reads a variable nothing sets,
        # standing in for any line that does: cat, which whooper-poll first runs itself as it writes the poll's
        # prompt, and mkdir, which makes whooper-ask's asks folder. set -u stops the script there, once its cleanup
        # trap is set, and bash 3.2 then runs the cleanup with $? at 0.
        self.fake_review_requests()
        run = self.state / "runs" / "20310727T010246Z-12"
        run.mkdir(parents=True)
        (run / "failure.txt").write_text("the review stopped at an unexpected error\n")
        for command, args, function in [("whooper-poll", [], "cat"), ("whooper-ask", [run.name, "Why did it stop?"], "mkdir")]:
            functions = Path(self.tmp.name) / f"{function}.sh"
            functions.write_text(f'{function}() {{ : "$EXAMPLE_UNSET"; }}\n')
            for n, bash in enumerate(BASHES):
                with self.subTest(command, bash=str(bash)):
                    folder = bash_folder(bash, Path(self.tmp.name) / f"{command}-bash-{n}")
                    result = self.command(command, *args, env={"PATH": f"{folder}{os.pathsep}{self.env['PATH']}",
                                                               "BASH_ENV": str(functions)})
                    self.assertIn("EXAMPLE_UNSET: unbound variable", result.stderr)
                    # whooper-round counts a poll that exits 0 as done, and goes on to select the work.
                    self.assertEqual(result.returncode, 1, result.stderr)
        # Neither saved anything: the poll moved no cursor, and the ask saved no answer.
        self.assertFalse((self.state / "poll" / "cursor").exists())
        self.assertFalse((run / "asks").exists())

    def test_a_fetch_github_refuses_for_want_of_a_login_names_that_cause(self):
        env, item, _ = self.pull_request()
        # A transport of git's stands in for GitHub refusing the fetch, as it does when gh, git's
        # credential helper, is signed out.
        self.script(self.fakes / "git-remote-whoopertest",
                    "echo \"fatal: could not read Username for 'https://github.com': terminal prompts disabled\" >&2\n"
                    "exit 128")
        clone = Path(self.tmp.name) / "cache" / "whooper" / "repos" / "sample-org-plain-repo"
        subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin", "whoopertest::github"], env=env,
                       check=True)
        result = self.command("whooper-review", str(item), "--dry-run", env=env)
        [run] = (self.state / "runs").iterdir()
        # git's message still goes to whooper-review's error output, where failure.txt points.
        self.assertEqual(result.returncode, 128)
        self.assertIn("fatal: could not read Username for 'https://github.com'", result.stderr)
        self.assertEqual(self.read(run / "failure.txt"),
                         "the review stopped at an unexpected error; see whooper-review's error output\n")
        self.assertEqual(json.loads((run / "failure.json").read_text()), CAUSES["gh_signed_out"])

    def test_a_setup_script_that_cannot_reach_docker_stops_the_review_and_names_that_cause(self):
        env, item, _ = self.pull_request()
        line = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
        setup = self.setup_script(f"echo 'Starting PostgreSQL' >&2\necho '{line}' >&2\necho\nexit 1")
        result = self.command("whooper-review", str(item), "--dry-run", env=env)
        [run] = (self.state / "runs").iterdir()
        # The script's error output still goes to whooper-review's, and failure.txt adds its last line.
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"Starting PostgreSQL\n{line}\n", result.stderr)
        self.assertEqual((run / "failure.txt").read_text(), f"{setup} failed: {line}\n")
        self.assertEqual(json.loads((run / "failure.json").read_text()), CAUSES["docker_unavailable"])
        # Any other failure of the script is setup_failed, and one that printed nothing gets the bare reason.
        shutil.rmtree(run)
        self.setup_script("exit 3")
        self.assertEqual(self.command("whooper-review", str(item), "--dry-run", env=env).returncode, 1)
        [run] = (self.state / "runs").iterdir()
        self.assertEqual((run / "failure.txt").read_text(), f"{setup} failed\n")
        self.assertEqual(json.loads((run / "failure.json").read_text()), CAUSES["setup_failed"])

    def test_a_review_of_a_repo_without_a_clone_fails_and_names_that_cause(self):
        # The config lists example-org/example-repo, and the cache holds no clone of it.
        item = Path(self.tmp.name) / "item.json"
        item.write_text(json.dumps({"kind": "first_review", "repo": "example-org/example-repo", "pr": 12,
                                    "head_sha": "1" * 40, "base_ref": "main", "source": "github_request",
                                    "slack": None, "reason": "r"}))
        result = self.command("whooper-review", str(item), "--dry-run")
        [run] = (self.state / "runs").iterdir()
        clone = Path(self.tmp.name) / "cache" / "whooper" / "repos" / "example-org-example-repo"
        reason = f"no clone of example-org/example-repo at {clone}; see README.md"
        # It prints the run directory as soon as it has one, whether or not the review then finishes.
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, f"{run}\n", f"whooper-review: {reason}\n"))
        self.assertEqual((run / "failure.txt").read_text(), f"{reason}\n")
        self.assertEqual(json.loads((run / "failure.json").read_text()), CAUSES["repo_not_configured"])
        # Like any review that does not finish, it counts as a failure at its head.
        record = self.state / "prs" / "example-org-example-repo-12" / f"{'1' * 40}.first_review.json"
        self.assertEqual(json.loads(record.read_text())["failures"], 1)

    def test_a_repo_the_config_does_not_list_stops_the_review_with_the_configs_message(self):
        # whooper-select drops such an item, so only a review run by hand gets here.
        item = Path(self.tmp.name) / "item.json"
        item.write_text(json.dumps({"kind": "first_review", "repo": "example-org/unlisted-repo", "pr": 12,
                                    "head_sha": "1" * 40, "base_ref": "main", "source": "github_request",
                                    "slack": None, "reason": "r"}))
        result = self.command("whooper-review", str(item), "--dry-run")
        config = self.env["WHOOPER_CONFIG"]
        self.assertEqual((result.returncode, result.stdout, result.stderr), (
            1, "", f'whooper: example-org/unlisted-repo has no [repos."example-org/unlisted-repo"] table in {config}\n'))
        # It stopped before it took the PR's lock or made the run's directory and record.
        self.assertFalse(self.state.exists())

    def test_an_ask_about_a_review_still_running_is_refused_until_its_whooper_review_is_gone(self):
        env, item, _ = self.pull_request()
        agent_pid = Path(self.tmp.name) / "agent.pid"
        # The review run notes its process ID and waits.
        self.fake_claude(review=f"echo $$ > '{agent_pid}'; exec sleep 30")
        review = subprocess.Popen([str(ROOT / "bin" / "whooper-review"), str(item), "--dry-run"], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.stop, review)
        self.wait_for(lambda: self.read(agent_pid), "the review run to start")
        # The run has a session of its own, which outlives a kill -9 of whooper-review.
        agent = os.getpgid(int(agent_pid.read_text()))

        def stop_agent():
            with suppress(ProcessLookupError):
                os.killpg(agent, signal.SIGKILL)

        self.addCleanup(stop_agent)
        [run] = (self.state / "runs").iterdir()
        # whooper-status applies the same rule to the review's lock.
        self.assertEqual([(r["run_id"], r["outcome"]) for r in self.status()["reviews"]], [(run.name, "running")])
        refused = self.command("whooper-ask", run.name, "Why did this review fail?", env=env)
        self.assertEqual((refused.returncode, refused.stdout), (1, ""))
        self.assertIn("the review is still running", refused.stderr)
        self.assertFalse((run / "asks").exists())
        # An earlier review of the same PR has ended, so the lock this one holds does not stop an ask
        # about it.
        earlier = run.parent / "20310727T010246Z-12"
        earlier.mkdir()
        (earlier / "run.json").write_text(json.dumps({"repo": "sample-org/plain-repo", "pr": 12, "run_id": earlier.name,
                                                      "agent": "claude"}))
        meanwhile = self.command("whooper-ask", earlier.name, "What did it find?", env=env)
        self.assertEqual(meanwhile.returncode, 0, meanwhile.stderr)
        # A kill -9 leaves the lock and the run's tmp folder behind. The lock's process is gone, so the
        # lock is stale and no longer stops an ask.
        os.kill(review.pid, signal.SIGKILL)
        review.wait()
        stop_agent()
        lock = self.state / "locks" / "sample-org-plain-repo-12"
        self.assertEqual(((lock / "pid").read_text(), (run / "tmp").is_dir()), (f"{review.pid}\n", True))
        self.assertEqual({r["run_id"]: r["outcome"] for r in self.status()["reviews"]}[run.name], "failed")
        answered = self.command("whooper-ask", run.name, "Why did this review fail?", env=env)
        self.assertEqual((answered.returncode, answered.stdout), (0, "It stopped.\n"), answered.stderr)

    def test_status_reports_the_loop_pause_next_round_and_rounds_from_the_state_folder(self):
        # Status format 2's fields come beside format 1's, which they leave as they were.
        nothing = {"version": 1, "loop": {"running": False, "pid": None}, "paused": False, "next_round": None,
                   "round": {"running": False, "started_at": None, "reviewing": None}, "last_round": None,
                   "blocked": None, "reviews": [], "rounds": [], "rounds_by_day": self.rounds_by_day({}),
                   "format": 2, "agent": {"words": "Running", "dot": "idle"}, "blockers": [], "items": [],
                   "truncated": False}
        self.assertEqual(self.status(), nothing)
        # Unix times on 2031-07-30: the next round at 10:15:00 in Los Angeles, the running round's
        # start at 10:00:02, and the last round from 09:45:01 to 09:52:40.
        loop, current = self.holder("whooper-run"), self.holder("whooper-round")
        self.hold(self.lock, loop)
        self.hold(self.state / "round.lock", current)
        reviews = [{"pr": "sample-org/plain-repo#7", "outcome": "reviewed"}]
        (self.state / "round.lock" / "round.json").write_text(json.dumps(
            {"started_at": 1943197202, "selected": 2, "reviewing": "example-org/example-repo#12", "reviews": reviews}))
        (self.state / "next-round").write_text("1943198100\n")
        (self.state / "paused").touch()
        (self.state / "last-round.json").write_text(json.dumps(
            {"started_at": 1943196301, "ended_at": 1943196760, "result": "done", "selected": 1, "reviews": reviews}))
        last_round = {"started_at": "2031-07-30T09:45:01-07:00", "ended_at": "2031-07-30T09:52:40-07:00",
                      "result": "done", "selected": 1, "reviews": reviews}
        # The history, oldest first: a round whose poll failed at 09:15:00, one whooper-run skipped at
        # 09:30:00 while paused, and the last round.
        (self.state / "rounds.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            {"started_at": 1943194500, "ended_at": 1943194507, "result": "poll_failed", "selected": 0,
             "reason": "The GitHub fetch failed"},
            {"started_at": 1943195400, "ended_at": None, "result": "skipped", "selected": 0,
             "reason": "Whooper is paused"},
            {"started_at": 1943196301, "ended_at": 1943196760, "result": "done", "selected": 1, "reason": None},
        ]))
        rounds = [
            {"started_at": "2031-07-30T09:45:01-07:00", "ended_at": "2031-07-30T09:52:40-07:00", "result": "done",
             "selected": 1, "reason": None},
            {"started_at": "2031-07-30T09:30:00-07:00", "ended_at": None, "result": "skipped", "selected": 0,
             "reason": "Whooper is paused"},
            {"started_at": "2031-07-30T09:15:00-07:00", "ended_at": "2031-07-30T09:15:07-07:00",
             "result": "poll_failed", "selected": 0, "reason": "The GitHub fetch failed"},
        ]
        self.assertEqual(self.status(), {
            "version": 1,
            "loop": {"running": True, "pid": loop},
            "paused": True,
            "next_round": "2031-07-30T10:15:00-07:00",
            "round": {"running": True, "started_at": "2031-07-30T10:00:02-07:00",
                      "reviewing": "example-org/example-repo#12"},
            "last_round": last_round,
            "blocked": None,
            "reviews": [],
            "rounds": rounds,
            # The rounds above are in 2031, after the last 15 days.
            "rounds_by_day": self.rounds_by_day({}),
            "format": 2,
            "agent": {"words": "Reviewing example-org/example-repo#12", "dot": "running"},
            "blockers": [],
            "items": [],
            "truncated": False,
        })
        # Locks left by a whooper-run that is gone and by a round whose process ID now belongs to this
        # test: nothing runs, so no round is next either.
        gone = subprocess.Popen(["true"])
        gone.wait()
        (self.lock / "pid").write_text(f"{gone.pid}\n")
        (self.state / "round.lock" / "pid").write_text(f"{os.getpid()}\n")
        (self.state / "paused").unlink()
        self.assertEqual(self.status(), {**nothing, "last_round": last_round, "rounds": rounds})
        usage = self.command("whooper-status")
        self.assertEqual((usage.returncode, usage.stderr), (1, "usage: whooper-status --json\n"))

    def rounds_by_day(self, counts):
        """whooper-status's rounds_by_day with times in Los Angeles: each of the last 15 days, oldest first, with
        counts, which maps a number of days before today to that day's counts by result, and 0 for the rest."""
        today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
        return [{"date": (today - timedelta(days=back)).isoformat(),
                 **{result: counts.get(back, {}).get(result, 0) for result in ["done", "poll_failed", "interrupted", "skipped"]}}
                for back in range(14, -1, -1)]

    def test_status_counts_the_rounds_of_each_of_the_last_15_days_and_lists_the_last_50(self):
        zone = ZoneInfo("America/Los_Angeles")
        today = datetime.now(zone).date()

        def noon(back, minute=0):
            """The Unix time of noon in Los Angeles back days before today, and minute minutes."""
            return int(datetime.combine(today - timedelta(days=back), time(12, minute), zone).timestamp())

        def ran(started, result):
            return {"started_at": started, "ended_at": None if result == "skipped" else started + 60, "result": result,
                    "selected": 0, "reason": None}

        # Oldest first: a round 15 days back, before the window, one 14 days back, two yesterday, and 55 today.
        history = [ran(noon(15), "done"), ran(noon(14), "poll_failed"), ran(noon(1), "interrupted"),
                   ran(noon(1, 5), "skipped"), *(ran(noon(0, minute), "poll_failed" if minute < 2 else "done")
                                                  for minute in range(55))]
        self.state.mkdir(parents=True)
        (self.state / "rounds.jsonl").write_text("".join(json.dumps(r) + "\n" for r in history))
        status = self.status()
        self.assertEqual(status["rounds_by_day"], self.rounds_by_day({
            14: {"poll_failed": 1}, 1: {"interrupted": 1, "skipped": 1}, 0: {"done": 53, "poll_failed": 2}}))
        # rounds lists the newest 50, newest first.
        self.assertEqual([datetime.fromisoformat(r["started_at"]).timestamp() for r in status["rounds"]],
                         [noon(0, minute) for minute in range(54, 4, -1)])

    def test_a_pause_lasts_until_whooper_run_restarts_and_skips_each_round_meanwhile(self):
        # A pause does not survive a restart: a new whooper-run deletes paused and runs its first round at
        # once, and the fakes fail its poll.
        self.assertEqual(self.command("whooper-pause").returncode, 0)
        log = Path(self.tmp.name) / "run.log"
        with log.open("w") as out:
            loop = self.start(ROOT / "bin" / "whooper-run", out)
        self.wait_for((self.state / "last-round.json").exists, "the first round")
        self.assertFalse((self.state / "paused").exists())
        self.assertEqual(self.status()["last_round"]["result"], "poll_failed")
        # Paused while it waits, whooper-run skips the next round, due a minute after the first ended.
        self.assertEqual(self.command("whooper-pause").returncode, 0)
        self.wait_for(lambda: "skipped this round" in log.read_text(), "whooper-run to skip a round")
        skipped = datetime.now(timezone.utc)
        self.wait_for(lambda: self.status()["next_round"], "the next round")
        status = self.status()
        self.assertGreater(datetime.fromisoformat(status["next_round"]), skipped)
        # The skipped round ran nothing, so the last round is still the first, and the history lists both.
        self.assertEqual({key: status[key] for key in ["loop", "paused", "round"]},
                         {"loop": {"running": True, "pid": loop.pid}, "paused": True,
                          "round": {"running": False, "started_at": None, "reviewing": None}})
        last = status["last_round"]
        self.assertEqual(last["result"], "poll_failed")
        [skip, first] = status["rounds"]
        self.assertEqual(first, {"started_at": last["started_at"], "ended_at": last["ended_at"],
                                 "result": "poll_failed", "selected": 0, "reason": "The GitHub fetch failed"})
        self.assertEqual({key: skip[key] for key in ["ended_at", "result", "selected", "reason"]},
                         {"ended_at": None, "result": "skipped", "selected": 0, "reason": "Whooper is paused"})
        self.assertTrue(datetime.fromisoformat(last["ended_at"]) < datetime.fromisoformat(skip["started_at"])
                        <= skipped)
        self.assertEqual(self.command("whooper-resume").returncode, 0)
        self.assertFalse((self.state / "paused").exists())


class RoundHistoryTest(unittest.TestCase):
    """whooper.py add-round, which whooper-round runs as each round ends and whooper-run as it skips
    one, on a throwaway state folder."""

    def test_the_history_keeps_the_rounds_of_the_last_15_days_and_at_least_the_last_50_oldest_first(self):
        # 51 rounds 20 days ago, a minute apart, then 60 that start now, a minute apart.
        old = int((datetime.now() - timedelta(days=20)).timestamp())
        now = int(datetime.now().timestamp())

        def skipped(started):
            return json.dumps({"started_at": started, "ended_at": None, "result": "skipped", "selected": 0,
                               "reason": "Whooper is paused"})

        with tempfile.TemporaryDirectory() as tmp:
            for n in range(51):
                main("add-round", tmp, skipped(old + 60 * n))
            # Written through a rename, which leaves no temporary file behind.
            self.assertEqual(os.listdir(tmp), ["rounds.jsonl"])
            starts = [json.loads(line)["started_at"] for line in (Path(tmp) / "rounds.jsonl").read_text().splitlines()]
            # The old rounds are before the last 15 days, and the history keeps the last 50 of them.
            self.assertEqual(starts, [old + 60 * n for n in range(1, 51)])
            for n in range(60):
                main("add-round", tmp, skipped(now + 60 * n))
            starts = [json.loads(line)["started_at"] for line in (Path(tmp) / "rounds.jsonl").read_text().splitlines()]
        # Once 50 newer rounds came, the old ones went, and every round of the last 15 days stays.
        self.assertEqual(starts, [now + 60 * n for n in range(60)])

    def test_two_rounds_that_end_at_once_both_stay_in_the_history(self):
        # whooper-run adds a round it skips while a round run by hand ends: its line comes between the
        # round's read of rounds.jsonl and its write.
        ran = {"started_at": 1943194500, "ended_at": 1943194620, "result": "done", "selected": 0, "reason": None}
        skipped = {"started_at": 1943194560, "ended_at": None, "result": "skipped", "selected": 0,
                   "reason": "Whooper is paused"}
        with tempfile.TemporaryDirectory() as tmp:
            with at_first_call("write_file", lambda: add_round(Path(tmp), skipped)):
                add_round(Path(tmp), ran)
            history = [json.loads(line) for line in (Path(tmp) / "rounds.jsonl").read_text().splitlines()]
        self.assertEqual(history, [ran, skipped])


DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun", "next mon"]


def at(day, clock):
    """A local time in the week of Monday 2031-07-28, such as at("wed", "12:00"). "next mon" is 2031-08-04."""
    hours, minutes = clock.split(":")
    return datetime(2031, 7, 28, int(hours), int(minutes)) + timedelta(days=DAY_NAMES.index(day))


class ScheduleTest(unittest.TestCase):
    """When whooper-run starts a round. Most cases use the default [schedule], which the test config
    also gets by having none: 15 minutes after the last round, from 10:00 to 18:00, Monday to Friday."""

    weekdays = read_schedule({})

    def test_defaults(self):
        self.assertEqual(self.weekdays, {"interval_minutes": 15, "start": time(10, 0), "end": time(18, 0),
                                         "days": ["mon", "tue", "wed", "thu", "fri"]})

    def test_inside_the_hours_a_round_starts_at_once(self):
        for when in [at("mon", "10:00"), at("wed", "12:34"), at("fri", "17:59")]:
            with self.subTest(when):
                self.assertEqual(next_round(self.weekdays, when), when)

    def test_outside_the_hours_a_round_waits_for_the_next_start(self):
        for when, want in [
            (at("wed", "09:05"), at("wed", "10:00")),
            (at("wed", "18:00"), at("thu", "10:00")),
            (at("wed", "23:30"), at("thu", "10:00")),
            (at("fri", "18:00"), at("next mon", "10:00")),
            (at("sat", "12:00"), at("next mon", "10:00")),
            (at("sun", "09:00"), at("next mon", "10:00")),
        ]:
            with self.subTest(when):
                self.assertEqual(next_round(self.weekdays, when), want)

    def test_hours_and_days_come_from_the_config(self):
        schedule = read_schedule({"schedule": {"start": "08:30", "end": "12:00", "days": ["tue", "sat"]}})
        for when, want in [
            (at("mon", "11:00"), at("tue", "08:30")),
            (at("tue", "11:59"), at("tue", "11:59")),
            (at("tue", "12:00"), at("sat", "08:30")),
        ]:
            with self.subTest(when):
                self.assertEqual(next_round(schedule, when), want)

    def test_the_next_round_starts_interval_minutes_after_the_last_one_ended(self):
        # (case, when the last round ended, the next round, the line whooper-run logs when it waits for the next start)
        for name, ended, want, note in [
            ("a morning round", at("wed", "10:06"), at("wed", "10:21"), ""),
            # A round may start until 18:00 and finish after it.
            ("a round that leaves time for one more", at("wed", "17:44"), at("wed", "17:59"), ""),
            ("the day's last round", at("wed", "17:50"), at("thu", "10:00"),
             "whooper-run: outside the [schedule] hours; next round at Thu 2031-07-31 10:00\n"),
            ("Friday's last round", at("fri", "17:46"), at("next mon", "10:00"),
             "whooper-run: outside the [schedule] hours; next round at Mon 2031-08-04 10:00\n"),
        ]:
            with self.subTest(name):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    main("next-round", str(int(ended.timestamp())))
                self.assertEqual((int(out.getvalue()), err.getvalue()), (int(want.timestamp()), note))

    def test_an_interval_typed_as_a_float_with_no_fraction_is_that_many_minutes(self):
        # TOML reads interval_minutes = 15.0, edited by hand, as a float, which a settings form shows as 15, so typing 15
        # there sends nothing. read_schedule takes it as 15, and so does next-round, which whooper-run runs before each
        # wait.
        interval = read_schedule({"schedule": {"interval_minutes": 15.0}})["interval_minutes"]
        self.assertEqual((interval, type(interval)), (15, int))
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(CONFIG + "\n[schedule]\ninterval_minutes = 15.0\n")
            out = io.StringIO()
            with mock.patch.dict(os.environ, WHOOPER_CONFIG=str(config)), redirect_stdout(out):
                main("next-round", str(int(at("wed", "10:06").timestamp())))
        self.assertEqual(int(out.getvalue()), int(at("wed", "10:21").timestamp()))

    def test_a_schedule_whooper_run_cannot_follow_fails(self):
        for name, values in [
            ("an unknown day", {"days": ["mon", "tues"]}),
            ("no days", {"days": []}),
            ("a time that is not 24-hour", {"start": "9:30am"}),
            ("an end before the start", {"start": "18:00", "end": "10:00"}),
            ("an end at the start", {"end": "10:00"}),
            # Each poll runs a model, so no wait between rounds would spend money nonstop.
            ("no wait between rounds", {"interval_minutes": 0}),
            ("a negative interval", {"interval_minutes": -30}),
            ("an interval in quotes", {"interval_minutes": "15"}),
            ("part of a minute", {"interval_minutes": 1.5}),
            # bool is an int in Python, but true is no number of minutes.
            ("an interval of true", {"interval_minutes": True}),
        ]:
            with self.subTest(name), self.assertRaises(SystemExit):
                read_schedule({"schedule": values})


class ConfigTest(unittest.TestCase):
    """What the config feeds: shell variables, PR links, and the prompts."""

    def command(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            main(*args)
        return out.getvalue()

    def test_example_config_has_every_value_the_scripts_read(self):
        os.environ["WHOOPER_CONFIG"], test_config = str(ROOT / "examples" / "config.example.toml"), os.environ["WHOOPER_CONFIG"]
        try:
            env = self.command("env")
            repo = self.command("repo", "example-org/example-repo")
            round_at = self.command("next-round", str(int(at("wed", "10:06").timestamp())))
        finally:
            os.environ["WHOOPER_CONFIG"] = test_config
        self.assertIn("WHOOPER_ORGS=example-org\n", env)
        self.assertIn("WHOOPER_REVIEW_AGENT=devin\n", env)
        self.assertIn("WHOOPER_ASK_TIMEOUT_MINUTES=10\n", env)
        self.assertIn(f"WHOOPER_NOTIFY={ROOT / 'examples' / 'notify.sh'}\n", env)
        self.assertIn(f"WHOOPER_REPO_SETUP={ROOT / 'examples' / 'setup.sh'}\n", repo)
        self.assertEqual(int(round_at), int(at("wed", "10:21").timestamp()))

    def test_notify_is_a_path_in_the_configs_directory_or_empty(self):
        self.assertIn("WHOOPER_NOTIFY=''\n", self.command("env"))
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(CONFIG + '\n[schedule]\nnotify = "notify.sh"\n')
            os.environ["WHOOPER_CONFIG"], test_config = str(config), os.environ["WHOOPER_CONFIG"]
            try:
                env = self.command("env")
            finally:
                os.environ["WHOOPER_CONFIG"] = test_config
        self.assertIn(f"WHOOPER_NOTIFY={Path(tmp) / 'notify.sh'}\n", env)

    def test_each_agents_settings_are_exported_for_its_launcher(self):
        env = self.command("env")
        for line in ["export WHOOPER_CLAUDE_REVIEW_MODEL=review-model\n", "export WHOOPER_CLAUDE_BUDGET_USD=15\n",
                     "export WHOOPER_DEVIN_REVIEW_MODEL=devin-review-model\n"]:
            self.assertIn(line, env)

    def test_a_poll_limit_that_is_not_a_whole_number_of_at_least_five_minutes_fails(self):
        # whooper-poll's watchdog multiplies the limit by 60 in bash, as whooper-review's does, and a poll takes 2 to 3
        # minutes. A float with no fraction, such as 7.0, is that whole number, and env prints it as one.
        # (value in the config, what env prints for it, or None where env fails)
        for value, printed in [("7.0", "7"), ("3", None), ("4.0", None), ("7.5", None), ('"7"', None), ("true", None)]:
            with self.subTest(value), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "config.toml"
                config.write_text(CONFIG + f"\n[poll]\ntimeout_minutes = {value}\n")
                with mock.patch.dict(os.environ, WHOOPER_CONFIG=str(config)):
                    if printed is None:
                        with self.assertRaises(SystemExit) as stop:
                            self.command("env")
                        self.assertEqual(str(stop.exception), "whooper: [poll] timeout_minutes takes a whole number of"
                                                              f" minutes, at least 5; see {config}")
                    else:
                        self.assertIn(f"WHOOPER_POLL_TIMEOUT_MINUTES={printed}\n", self.command("env"))

    def test_polls_stop_after_ten_minutes_unless_the_config_says_otherwise(self):
        self.assertIn("WHOOPER_POLL_TIMEOUT_MINUTES=10\n", self.command("env"))
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(CONFIG + "\n[poll]\ntimeout_minutes = 7\n")
            os.environ["WHOOPER_CONFIG"], test_config = str(config), os.environ["WHOOPER_CONFIG"]
            try:
                env = self.command("env")
            finally:
                os.environ["WHOOPER_CONFIG"] = test_config
        self.assertIn("WHOOPER_POLL_TIMEOUT_MINUTES=7\n", env)

    def test_asks_stop_after_ten_minutes_unless_the_config_says_otherwise(self):
        self.assertIn("WHOOPER_ASK_TIMEOUT_MINUTES=10\n", self.command("env"))
        # (value in the config, whether it is a limit whooper-ask can keep)
        for value, works in [("7", True), ("2.5", True), ("0", False), ('"7"', False), ("true", False)]:
            with self.subTest(value), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "config.toml"
                config.write_text(CONFIG + f"\n[ask]\ntimeout_minutes = {value}\n")
                with mock.patch.dict(os.environ, WHOOPER_CONFIG=str(config)):
                    if works:
                        self.assertIn(f"WHOOPER_ASK_TIMEOUT_MINUTES={value}\n", self.command("env"))
                    else:
                        with self.assertRaises(SystemExit):
                            self.command("env")

    def test_a_review_limit_that_is_not_a_whole_number_of_minutes_fails(self):
        # whooper-review's watchdog multiplies the limit by 60 in bash, which knows whole numbers only, so 1.5 would
        # leave the review with no time limit. A float with no fraction, such as 45.0, which a settings form shows as
        # 45, is that whole number, and env prints it as one, so bash never reads 45.0.
        # (value in the config, what env prints for it, or None where env fails)
        for value, printed in [("45", "45"), ("1", "1"), ("45.0", "45"), ("1.5", None), ("0", None), ("0.0", None),
                               ("-5", None), ('"45"', None), ("true", None), (None, None)]:
            with self.subTest(value), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "config.toml"
                config.write_text(CONFIG.replace("timeout_minutes = 45\n",
                                                 "" if value is None else f"timeout_minutes = {value}\n"))
                with mock.patch.dict(os.environ, WHOOPER_CONFIG=str(config)):
                    if printed is None:
                        with self.assertRaises(SystemExit) as stop:
                            self.command("env")
                        self.assertEqual(str(stop.exception), "whooper: [review] timeout_minutes takes a whole number of"
                                                              f" minutes, at least 1; see {config}")
                    else:
                        self.assertIn(f"WHOOPER_REVIEW_TIMEOUT_MINUTES={printed}\n", self.command("env"))

    def test_a_job_on_an_agent_without_a_launcher_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(CONFIG.replace('[jobs]\nreview = "claude"', '[jobs]\nreview = "nobody"'))
            os.environ["WHOOPER_CONFIG"], test_config = str(config), os.environ["WHOOPER_CONFIG"]
            try:
                with self.assertRaises(SystemExit):
                    self.command("env")
            finally:
                os.environ["WHOOPER_CONFIG"] = test_config

    def test_a_config_without_each_of_the_three_jobs_fails_naming_the_missing_one(self):
        jobs = 'review = "claude"\npoll = "claude"\nsummary = "claude"\n'
        for missing, text in [("review", CONFIG.replace('review = "claude"\n', "")),
                              ("summary", CONFIG.replace('summary = "claude"\n', "")),
                              ("review, poll, or summary", CONFIG.replace(f"[jobs]\n{jobs}", ""))]:
            with self.subTest(missing), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "config.toml"
                config.write_text(text)
                with mock.patch.dict(os.environ, WHOOPER_CONFIG=str(config)), self.assertRaises(SystemExit) as stop:
                    self.command("env")
                self.assertEqual(str(stop.exception), f"whooper: [jobs] names no agent for {missing}; each of review,"
                                                      f" poll, and summary names the agent it runs on; see {config}")

    def test_a_repo_missing_from_the_config_fails(self):
        with self.assertRaises(SystemExit):
            self.command("repo", "other-org/example-repo")

    def test_prompts_are_filled_from_the_config(self):
        review = self.command("prompt", "review", "example-org/example-repo", "claude")
        for text in ["sam-example (Sam)", "`review-skill`, `language-skill`", "the CI review bot (ci-review-bot)",
                     "Run `make test` from the service's directory.", "as Sam would post it",
                     f"`{ROOT / 'schemas' / 'examples-request.json'}`", "`TestWhooperExamples`"]:
            self.assertIn(text, review)
        self.assertIn("Notes for this repository:\n\nNone.",
                      self.command("prompt", "review", "sample-org/plain-repo", "claude"))
        self.assertNotIn("{{", self.command("prompt", "poll"))

    def test_the_review_prompt_names_each_agents_tools(self):
        for agent, wording in REVIEW_WORDING.items():
            with self.subTest(agent):
                review = self.command("prompt", "review", "example-org/example-repo", agent)
                self.assertNotIn("{{", review)
                self.assertIn(f'"{wording["shell"]} tool_use_id <id> ran: <command>"', review)

    def test_a_placeholder_without_a_value_fails(self):
        with self.assertRaises(SystemExit):
            fill("for {{user_name}} and {{missing}}", {"user_name": "Sam"})

    def test_empty_bot_list_reads_none(self):
        self.assertEqual(bot_list([], "login"), "none")

    def test_pr_links_count_in_every_configured_org_only(self):
        link = pr_link(ORGS)
        for url, matches in [
            ("https://github.com/example-org/app/pull/3", True),
            ("http://github.com/sample-org/app/pull/3", True),
            ("https://github.com/other-org/app/pull/3", False),
            # The org name must match whole, not as a prefix.
            ("https://github.com/example-org-fork/app/pull/3", False),
        ]:
            with self.subTest(url):
                self.assertEqual(bool(link.search(url)), matches)


class ClaudeVersionTest(unittest.TestCase):
    """whooper.py claude-version, which the commands that run agents run as they start, with a fake claude that
    answers --version, alone on PATH."""

    def check(self, version=None, config=None, body=None):
        """Runs the check with claude --version answering version, or with no claude on PATH for None, or with a
        claude that runs body, a few lines of sh, on config's text, or the test config; returns the message it stops
        with, or None."""
        with tempfile.TemporaryDirectory() as tmp:
            fakes = Path(tmp) / "bin"
            fakes.mkdir()
            if body is not None:
                fake(fakes / "claude", body)
            elif version is not None:
                fake(fakes / "claude", f'[ "$1" = --version ] && echo "{version}"\necho "$*" >> "{tmp}/calls"')
            path = Path(tmp) / "config.toml"
            path.write_text(config or CONFIG)
            with mock.patch.dict(os.environ, PATH=str(fakes), WHOOPER_CONFIG=str(path)):
                try:
                    main("claude-version")
                except SystemExit as stop:
                    return f"{stop}\n"
                finally:
                    self.calls = (Path(tmp) / "calls").read_text() if (Path(tmp) / "calls").exists() else ""
        return None

    def test_claude_code_2_1_295_and_later_runs(self):
        for version in ["2.1.295 (Claude Code)", "2.1.314 (Claude Code)", "2.10.0 (Claude Code)", "3.0.0"]:
            with self.subTest(version):
                self.assertIsNone(self.check(version))
                self.assertEqual(self.calls, "--version\n")

    def test_an_older_or_unreadable_version_or_no_claude_refuses(self):
        for version, said in [("2.1.294 (Claude Code)", "`claude --version` says 2.1.294 (Claude Code)"),
                              ("1.9.999", "`claude --version` says 1.9.999"),
                              ("Claude Code", "`claude --version` says Claude Code"),
                              (None, "no claude command is on PATH")]:
            with self.subTest(version):
                fix = "install" if version is None else "update"
                self.assertEqual(self.check(version), CLAUDE_TOO_OLD.split(" but ")[0] + f" but {said}; {fix} Claude"
                                 " Code, or move those jobs to devin\n")

    def test_a_claude_that_does_not_answer_in_time_refuses_and_one_that_reads_its_input_gets_none(self):
        # This test's input is a pipe nobody closes, as a terminal would be, so a claude that read it would wait.
        read, write = os.pipe()
        saved = os.dup(0)
        os.dup2(read, 0)
        try:
            with mock.patch.object(whooper, "CLAUDE_VERSION_SECONDS", 2):
                self.assertEqual(self.check(body="exec /bin/sleep 30"), CLAUDE_TOO_OLD.split(" but ")[0] + " but"
                                 " `claude --version` did not answer within 2 seconds; update Claude Code, or move those"
                                 " jobs to devin\n")
                # A claude that reads its input to the end, as one waiting at a prompt would, gets an empty input.
                self.assertIsNone(self.check(body='/bin/cat >/dev/null\necho "2.1.314 (Claude Code)"'))
        finally:
            os.dup2(saved, 0)
            for fd in (saved, read, write):
                os.close(fd)

    def test_the_message_names_only_the_jobs_on_claude(self):
        config = CONFIG.replace('review = "claude"', 'review = "devin"').replace('poll = "claude"', 'poll = "devin"')
        self.assertEqual(self.check("2.1.0", config),
                         "whooper: [jobs] summary runs on claude, which needs Claude Code 2.1.295 or later, the first"
                         " that blocks a Slack call when its guard hook fails, but `claude --version` says 2.1.0; update"
                         " Claude Code, or move that job to devin\n")

    def test_no_job_on_claude_leaves_claude_alone(self):
        self.assertIsNone(self.check("2.1.0", CONFIG.replace('= "claude"', '= "devin"')))
        self.assertEqual(self.calls, "")


EXAMPLE = (ROOT / "examples" / "config.example.toml").read_text()


class ClaudeLauncherTest(unittest.TestCase):
    """agents/claude/review with a fake claude on PATH that records its arguments."""

    def arguments(self, **env):
        with tempfile.TemporaryDirectory() as tmp:
            fakes = Path(tmp) / "bin"
            fakes.mkdir()
            (fakes / "claude").write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_FILE"\n')
            (fakes / "claude").chmod(0o755)
            prompt, run, args = Path(tmp) / "prompt.md", Path(tmp) / "run", Path(tmp) / "args"
            prompt.write_text("Review this PR.")
            run.mkdir()
            base = {key: value for key, value in os.environ.items() if key != "WHOOPER_CLAUDE_BUDGET_USD"}
            subprocess.run([str(ROOT / "agents" / "claude" / "review"), str(prompt)], cwd=tmp, capture_output=True,
                           timeout=60, env={**base, "PATH": f"{fakes}:{os.environ['PATH']}", "ARGS_FILE": str(args),
                                            "WHOOPER_RUN": str(run), "WHOOPER_CLAUDE_REVIEW_MODEL": "review-model", **env})
            return args.read_text().splitlines()

    def test_a_review_has_no_spending_limit_unless_the_config_sets_one(self):
        self.assertNotIn("--max-budget-usd", self.arguments())
        args = self.arguments(WHOOPER_CLAUDE_BUDGET_USD="15")
        self.assertEqual(args[args.index("--max-budget-usd") + 1], "15")


def files(folder):
    """Each path under folder with its size and modification time, which change when anything writes there."""
    return {str(path.relative_to(folder)): (path.stat().st_size, path.stat().st_mtime_ns) for path in folder.rglob("*")}


def fake(path, body):
    """Writes an executable sh script at path, standing in for a command such as claude."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def review_stand_in(test, folder):
    """Starts a stand-in for a running whooper-review, a script named whooper-review in folder that waits until the
    test ends, so the command of its process names whooper-review, as the check of whether a review still runs wants.
    Returns its process ID once ps shows that command."""
    path = folder / "whooper-review"
    fake(path, "while :; do sleep 0.1; done")
    process = subprocess.Popen([str(path)])
    test.addCleanup(lambda: (process.kill(), process.wait()))
    deadline = monotonic() + 10
    while "whooper-review" not in subprocess.run(["ps", "-o", "command=", "-p", str(process.pid)],
                                                 capture_output=True, text=True).stdout:
        if monotonic() > deadline:
            test.fail("the stand-in for whooper-review did not start")
        sleep(0.05)
    return process.pid


class AskTest(unittest.TestCase):
    """bin/whooper-ask, and whooper.py prompt ask, which it runs, on a finished Claude review of
    example-org/example-repo#12 in a throwaway state folder. claude is a fake that records its
    prompt, arguments, and environment and answers with the text in the file answer; gh, slackdump,
    and devin fail, so no test can reach GitHub, Slack, or a model."""

    QUESTION = "Why is it bad to have invisible characters?"
    NIT = finding(id="2", kind="nit", nit_kind="wording", path="README.md", line=3,
                  title="Invisible characters in the setup steps", draft="**nit:** Line 3 has a zero-width space.")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.fakes = tmp / "bin"
        for name in ["gh", "slackdump", "devin"]:
            fake(self.fakes / name, "exit 1")
        # The launcher passes the prompt right after -p.
        fake(self.fakes / "claude", "\n".join([
            CLAUDE_VERSION,
            'fakes="${0%/*}"',
            'printf "%s" "$2" > "$fakes/prompt"',
            'printf "%s\\n" "$@" > "$fakes/args"',
            'env > "$fakes/env"',
            """jq -n -c --rawfile answer "$fakes/answer" '{type: "result", subtype: "success", result: $answer}'""",
        ]))
        self.env = {**os.environ, "PATH": f"{self.fakes}:{os.environ['PATH']}", "XDG_STATE_HOME": str(tmp),
                    "XDG_CACHE_HOME": str(tmp / "cache")}
        self.run = tmp / "whooper" / "runs" / "20310727T010246Z-12"
        self.run.mkdir(parents=True)
        (self.run / "run.json").write_text(json.dumps({"repo": "example-org/example-repo", "pr": 12, "head_sha": "h1",
                                                       "run_id": self.run.name, "agent": "claude"}))
        (self.run / "findings.json").write_text(json.dumps({
            "status": "reviewed", "skip_reason": "",
            "approval": {"verdict": "look_first", "reason": "Finding 1 needs the author."},
            "findings": [finding(), self.NIT]}))
        for name in ["log.md", "github.json", "diff.patch", "transcript.jsonl", "prompt.md", "stderr.log"]:
            (self.run / name).write_text(f"the review's {name}")

    def prompt(self, *args):
        """whooper.py prompt ask with args, as bin/whooper-ask runs it."""
        out = io.StringIO()
        with redirect_stdout(out):
            main("prompt", "ask", *args)
        return out.getvalue()

    def ask(self, *args, env=None):
        return subprocess.run([str(ROOT / "bin" / "whooper-ask"), *args], env={**self.env, **(env or {})},
                              capture_output=True, text=True, timeout=60)

    def test_the_prompt_names_the_question_the_finding_and_each_record_file(self):
        for name in ["drafts.json", "overview.json", "overview.invalid.json", "connections.json", "test_gaps.json",
                     "examples.json"]:
            (self.run / name).write_text("{}")
        (self.run / "asks").mkdir()
        (self.run / "asks" / "1.json").write_text("{}")
        (self.run / "asks" / "1.prompt.md").write_text("an earlier ask's prompt")
        # Braces in a question stay as written instead of reading as placeholders.
        question = f"{self.QUESTION} Is {{{{this}}}} one?"
        prompt = self.prompt("claude", str(self.run), question, "2")
        for name in ["log.md", "findings.json", "overview.json", "overview.invalid.json", "diff.patch", "connections.json",
                     "test_gaps.json", "examples.json", "github.json", "transcript.jsonl", "run.json", "prompt.md",
                     "drafts.json", "asks/1.json"]:
            self.assertIn(f"{self.run / name}: ", prompt)
        # Files the run does not have, and files that hold nothing to answer from.
        for name in ["transcript.json", "submit.json", "summary.md", "stderr.log", "asks/1.prompt.md"]:
            self.assertNotIn(f"{self.run / name}: ", prompt)
        self.assertIn("finding 2, Invisible characters in the setup steps (README.md:3)", prompt)
        self.assertTrue(prompt.endswith(f"\n{question}\n"))
        for text in ["sam-example", "`review-skill`, `language-skill`", "the Skill tool"]:
            self.assertIn(text, prompt)
        self.assertNotIn("{{", prompt.replace(question, ""))
        self.assertIn("the skill tool", self.prompt("devin", str(self.run), self.QUESTION))

    def test_refuses_a_run_it_cannot_ask_about(self):
        # (case, arguments, part of the reason it prints)
        for name, args, why in [
            ("no such run", ["20310101T000000Z-1", self.QUESTION], "no run 20310101T000000Z-1"),
            ("no question", [self.run.name], "usage: whooper-ask"),
            ("a finding the run lacks", [self.run.name, self.QUESTION, "--finding", "9"],
             "has no finding 9; its findings are 1, 2"),
        ]:
            with self.subTest(name):
                result = self.ask(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn(why, result.stderr)
        self.assertFalse((self.run / "asks").exists())

    def test_refuses_a_finding_when_findings_json_is_empty_or_holds_no_findings(self):
        # A review whooper-review stopped before its schema check leaves findings.json as the run wrote it.
        for name, content, why in [
            ("empty", "", "its findings.json holds no findings Whooper can read"),
            ("no list of findings", "{}", "its findings.json holds no findings Whooper can read"),
            ("an empty list of findings", json.dumps({"findings": []}), "its findings are none"),
        ]:
            with self.subTest(name):
                (self.run / "findings.json").write_text(content)
                result = self.ask(self.run.name, self.QUESTION, "--finding", "1")
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertIn(f"whooper-ask: the run has no finding 1; {why}\n", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.run / "asks").exists())

    def lock(self, pid):
        """Has the lock of the run's PR name the run and the process pid, as whooper-review's lock does while it
        reviews."""
        lock = Path(self.tmp.name) / "whooper" / "locks" / "example-org-example-repo-12"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{pid}\n")
        (lock / "run_id").write_text(f"{self.run.name}\n")

    def test_refuses_a_review_a_live_whooper_review_still_runs(self):
        pid = review_stand_in(self, Path(self.tmp.name) / "review")
        self.lock(pid)
        result = self.ask(self.run.name, self.QUESTION)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn(f"the review is still running, in process {pid}; ask again once it ends", result.stderr)

    def test_a_lock_whose_process_is_now_another_program_does_not_stop_an_ask(self):
        # The lock of a whooper-review killed with kill -9 stays, and the system may give its process ID to
        # another program, here this test.
        self.lock(os.getpid())
        (self.fakes / "answer").write_text("They hide a change from the reader.\n")
        result = self.ask(self.run.name, self.QUESTION)
        self.assertEqual((result.returncode, result.stdout), (0, "They hide a change from the reader.\n"),
                         result.stderr)

    def test_a_review_that_did_not_finish_can_be_asked_why(self):
        # The review's findings did not fit the schema, so whooper-review set them aside as
        # findings.invalid.json and saved why as failure.txt. The run left stdout.log, as a Devin
        # review does.
        (self.run / "findings.json").rename(self.run / "findings.invalid.json")
        (self.run / "failure.txt").write_text("the review run's findings.json does not fit the schema\n")
        (self.run / "stdout.log").write_text("the review's stdout.log")
        (self.fakes / "answer").write_text("Its findings did not fit the schema.\n")
        result = self.ask(self.run.name, "Why did this review fail?")
        self.assertEqual((result.returncode, result.stdout), (0, "Its findings did not fit the schema.\n"), result.stderr)
        # whooper-review's reason comes first. The agent's output may say more, so the prompt lists it
        # with the findings that did not fit and the rest of the record.
        prompt = (self.run / "asks" / "1.prompt.md").read_text()
        listed = [line for line in prompt.splitlines() if line.startswith(f"- {self.run}/")]
        self.assertTrue(listed[0].startswith(f"- {self.run / 'failure.txt'}: "), listed)
        for name in ["findings.invalid.json", "stderr.log", "stdout.log", "log.md", "diff.patch", "transcript.jsonl"]:
            self.assertIn(f"{self.run / name}: ", prompt)
        self.assertNotIn(f"{self.run / 'findings.json'}: ", prompt)
        no_finding = self.ask(self.run.name, self.QUESTION, "--finding", "1")
        self.assertEqual(no_finding.returncode, 1)
        self.assertIn("has no finding 1; its findings are none", no_finding.stderr)

    def test_a_review_stopped_before_it_wrote_run_json_is_asked_about_on_the_configs_review_agent(self):
        stopped = self.run.parent / "20310727T020000Z-12"
        # While whooper-review fetches, the run directory has neither run.json nor failure.txt.
        stopped.mkdir()
        fetching = self.ask(stopped.name, "Why did this review fail?")
        self.assertEqual(fetching.returncode, 1)
        self.assertIn(f"no run {stopped.name}", fetching.stderr)
        # The fetch failed, so whooper-review saved why before it wrote run.json, and no agent ran.
        # The agent the config's [jobs] review names answers: Devin, whose fake writes the answer.
        (stopped / "failure.txt").write_text(
            "the review stopped at an unexpected error; see whooper-review's error output\n")
        config = Path(self.tmp.name) / "config.toml"
        config.write_text(CONFIG.replace('review = "claude"', 'review = "devin"'))
        fake(self.fakes / "devin", 'cp "${0%/*}/answer" answer.md')
        (self.fakes / "answer").write_text("The fetch failed before the review started.\n")
        # A home of its own keeps the user's Devin config out of the run.
        env = {"WHOOPER_CONFIG": str(config), "HOME": str(Path(self.tmp.name) / "home")}
        result = self.ask(stopped.name, "Why did this review fail?", env=env)
        self.assertEqual((result.returncode, result.stdout), (0, "The fetch failed before the review started.\n"),
                         result.stderr)
        self.assertEqual(json.loads((stopped / "asks" / "1.json").read_text())["agent"], "devin")
        prompt = (stopped / "asks" / "1.prompt.md").read_text()
        listed = [line for line in prompt.splitlines() if line.startswith(f"- {stopped}/")]
        self.assertTrue(listed[0].startswith(f"- {stopped / 'failure.txt'}: "), listed)
        # whooper-review writes run.json with one jq, which leaves the file empty when it fails, as when
        # the skills cannot be hashed. Such a run.json, like one cut short, records no agent either, and
        # holds nothing to answer from, so the prompt leaves it out.
        (self.fakes / "answer").write_text("The skills could not be hashed.\n")
        for n, (name, run_json) in enumerate([("empty", ""), ("cut short", '{"repo": "example-org/exa')], start=2):
            with self.subTest(run_json=name):
                (stopped / "run.json").write_text(run_json)
                result = self.ask(stopped.name, "Why did this review fail?", env=env)
                self.assertEqual((result.returncode, result.stdout), (0, "The skills could not be hashed.\n"),
                                 result.stderr)
                self.assertEqual(json.loads((stopped / "asks" / f"{n}.json").read_text())["agent"], "devin")
                self.assertNotIn(f"{stopped / 'run.json'}: ", (stopped / "asks" / f"{n}.prompt.md").read_text())

    def test_a_findings_json_without_findings_whooper_can_read_counts_as_a_review_that_did_not_finish(self):
        # whooper-review leaves findings.json as the run wrote it when it stops the review before its
        # schema check, such as at the time limit or for an empty file. The prompt then lists what may
        # say why, as for any review that did not finish.
        (self.run / "findings.invalid.json").write_text("{}")
        for name, content in [("empty", ""), ("not JSON", '{"status": "revi'), ("no list of findings", "{}")]:
            with self.subTest(name):
                (self.run / "findings.json").write_text(content)
                prompt = self.prompt("claude", str(self.run), "Why did this review fail?")
                for record in ["stderr.log", "findings.invalid.json"]:
                    self.assertIn(f"{self.run / record}: ", prompt)
        # An empty list of findings is a finished review that found nothing.
        (self.run / "findings.json").write_text(json.dumps({"findings": []}))
        self.assertNotIn(f"{self.run / 'stderr.log'}: ", self.prompt("claude", str(self.run), self.QUESTION))

    def test_findings_that_do_not_fit_the_schema_can_still_be_asked_about(self):
        # whooper-draft, whooper-submit, and whooper-classify refuse findings and little else, as a
        # Devin review stopped at its time limit can leave (SubmitTest), but an ask answers from what
        # the file holds.
        (self.run / "findings.json").write_text(json.dumps({"findings": [finding(), self.NIT]}))
        (self.fakes / "answer").write_text("Line 3 has a zero-width space.\n")
        result = self.ask(self.run.name, self.QUESTION, "--finding", "2")
        self.assertEqual((result.returncode, result.stdout), (0, "Line 3 has a zero-width space.\n"), result.stderr)
        self.assertIn("finding 2, Invisible characters in the setup steps (README.md:3)",
                      (self.run / "asks" / "1.prompt.md").read_text())

    def test_refuses_a_finding_when_findings_json_gives_ids_as_numbers(self):
        # The schema makes each ID a string, and --finding passes a string, so the number 1 in
        # findings.json is not finding "1"; the refusal lists the IDs the file holds.
        (self.run / "findings.json").write_text(json.dumps({"findings": [finding(id=1), finding(id=2)]}))
        result = self.ask(self.run.name, self.QUESTION, "--finding", "1")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("whooper-ask: the run has no finding 1; its findings are 1, 2\n", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.run / "asks").exists())

    def test_a_finding_without_its_title_path_or_line_is_named_by_the_fields_it_has(self):
        # whooper-review leaves findings.json as the run wrote it when it stops the review before its
        # schema check, such as at the time limit, so a finding can lack any field but its ID.
        # (the finding, how the prompt names it)
        for f, named in [
            ({"id": "1", "title": "Bad JSON now returns 500"}, "finding 1, Bad JSON now returns 500"),
            ({"id": "1"}, "finding 1"),
            ({"id": "1", "path": "api/orders.go", "line": 88}, "finding 1 (api/orders.go:88)"),
            ({"id": "1", "path": "api/orders.go"}, "finding 1 (api/orders.go)"),
            # A line number without its file points nowhere.
            ({"id": "1", "title": "Bad JSON now returns 500", "line": 88}, "finding 1, Bad JSON now returns 500"),
        ]:
            with self.subTest(finding=f):
                (self.run / "findings.json").write_text(json.dumps({"findings": [f]}))
                prompt = self.prompt("claude", str(self.run), self.QUESTION, "1")
                self.assertIn(f"The question is about {named}, in findings.json.", prompt)

    def test_saves_each_answer_numbered_in_order(self):
        asks = self.run / "asks"
        before = datetime.now(timezone.utc).replace(microsecond=0)
        (self.fakes / "answer").write_text("Zero-width spaces hide a change from the reader.\n")
        first = self.ask(self.run.name, self.QUESTION)
        self.assertEqual((first.returncode, first.stdout), (0, "Zero-width spaces hide a change from the reader.\n"),
                         first.stderr)
        env = dict(line.split("=", 1) for line in (self.fakes / "env").read_text().splitlines() if "=" in line)
        self.assertEqual((env["GH_TOKEN"], env["WHOOPER_RUN"]), ("disabled", str(self.run)))
        (self.fakes / "answer").write_text("Nothing breaks; it reads as a nit.")
        second = self.ask(self.run.name, "Does anything break?", "--finding", "2")
        after = datetime.now(timezone.utc)
        self.assertEqual((second.returncode, second.stdout), (0, "Nothing breaks; it reads as a nit.\n"), second.stderr)
        saved = [json.loads((asks / f"{n}.json").read_text()) for n in (1, 2)]
        for record in saved:
            started, ended = (datetime.fromisoformat(record.pop(key)) for key in ["started_at", "ended_at"])
            self.assertTrue(before <= started <= ended <= after, (started, ended))
        self.assertEqual(saved, [
            {"question": self.QUESTION, "finding": None, "answer": "Zero-width spaces hide a change from the reader.",
             "agent": "claude", "transcript": str(asks / "1.transcript.jsonl")},
            {"question": "Does anything break?", "finding": "2", "answer": "Nothing breaks; it reads as a nit.",
             "agent": "claude", "transcript": str(asks / "2.transcript.jsonl")},
        ])
        # Each run got the prompt saved beside its answer, and the second may read the first ask.
        prompt = (asks / "2.prompt.md").read_text()
        self.assertEqual((self.fakes / "prompt").read_text(), prompt.rstrip("\n"))
        self.assertIn(f"{asks / '1.json'}: ", prompt)

    def test_a_run_past_its_time_limit_is_stopped_and_saves_nothing(self):
        # A limit of 3 seconds, and a run that would take 30. The fake runs sleep rather than exec it,
        # so a process whose command line names the fake lasts the whole run.
        config = Path(self.tmp.name) / "config.toml"
        config.write_text(CONFIG + "\n[ask]\ntimeout_minutes = 0.05\n")
        fake(self.fakes / "claude", f"{CLAUDE_VERSION}\nsleep 30")
        self.addCleanup(subprocess.run, ["pkill", "-KILL", "-f", self.tmp.name])
        started = monotonic()
        result = self.ask(self.run.name, self.QUESTION, env={"WHOOPER_CONFIG": str(config)})
        took = monotonic() - started
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("passed its 0.05-minute limit", result.stderr)
        self.assertFalse((self.run / "asks" / "1.json").exists())
        # Stopped at the limit, not when the run would have ended.
        self.assertLess(took, 20)
        # The limit counts from when whooper-ask starts the launcher, so on a busy machine it can pass
        # before the launcher has started claude, and claude never runs. Either way nothing of the run
        # is left: the launcher's command line names the ask's files in this test's folder, and the
        # fake's names the fake.
        deadline = monotonic() + 10
        while subprocess.run(["pgrep", "-f", self.tmp.name], capture_output=True).returncode == 0:
            if monotonic() > deadline:
                self.fail("a process of the run is still running")
            sleep(0.1)


class AskLauncherTest(unittest.TestCase):
    """agents/claude/ask and agents/devin/ask, each with a fake CLI on PATH that records how it was
    started and then answers, in a throwaway folder that also stands in for the user's home."""

    # The claude.ai Slack connector's writes, which review runs deny too.
    SLACK_WRITES = [f"mcp__claude_ai_Slack__slack_{tool}" for tool in
                    ["add_reaction", "create_canvas", "update_canvas", "schedule_message", "send_message_draft"]]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.fakes, self.home, self.run = tmp / "bin", tmp / "home", tmp / "run"
        (self.run / "asks").mkdir(parents=True)
        self.prompt, self.answer, self.prefix = tmp / "prompt.md", tmp / "answer.md", self.run / "asks" / "1"
        self.prompt.write_text("Why is it bad to have invisible characters?\n")
        # Devin Desktop sets the last two in the shells it starts.
        self.env = {**os.environ, "PATH": f"{self.fakes}:{os.environ['PATH']}", "HOME": str(self.home),
                    "WHOOPER_RUN": str(self.run), "WHOOPER_CLAUDE_REVIEW_MODEL": "review-model",
                    "WHOOPER_DEVIN_REVIEW_MODEL": "devin-review-model", "ACP_BACKEND": "windsurf",
                    "WINDSURF_EXT_HOST_PID": "1"}

    def launch(self, agent):
        result = subprocess.run([str(ROOT / "agents" / agent / "ask"), str(self.prompt), str(self.answer),
                                 str(self.prefix)], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.answer.read_text()

    def recorded(self, name):
        return (self.fakes / name).read_text().splitlines()

    def test_the_claude_run_gets_only_tools_that_read_files_and_every_slack_write_denied(self):
        fake(self.fakes / "claude", "\n".join([
            'fakes="${0%/*}"',
            'printf "%s\\n" "$@" > "$fakes/args"',
            'pwd > "$fakes/cwd"',
            'ls -A > "$fakes/files"',
            """echo '{"type": "result", "subtype": "success", "result": "They hide a change."}'""",
        ]))
        self.assertEqual(self.launch("claude"), "They hide a change.\n")
        args = self.recorded("args")
        self.assertEqual(args[args.index("--tools") + 1].split(","), ["Read", "Grep", "Glob", "Skill"])
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        add_dir = args.index("--add-dir")
        self.assertEqual(args[add_dir + 1:add_dir + 3], [str(self.run), f"{self.home}/.claude/skills"])
        self.assertEqual(args[args.index("--disallowedTools") + 1].split(), self.SLACK_WRITES)
        self.assertIn("--no-session-persistence", args)
        self.assertNotIn("--allowedTools", args)
        # It starts in an empty directory, not the run's.
        [cwd] = self.recorded("cwd")
        self.assertNotIn(str(self.run), cwd)
        self.assertEqual(self.recorded("files"), [])

    def test_the_devin_run_starts_read_only_in_an_empty_directory(self):
        config = self.home / ".config" / "devin" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"agent": {"model": "user-model"},
                                      "permissions": {"allow": ["Exec(git)", "Write(/**)"], "deny": ["Exec(rm)"]}}))
        (self.fakes / "answer").parent.mkdir(parents=True)
        (self.fakes / "answer").write_text("They hide a change.\n")
        # The run writes answer.md in its working directory and its export where --export says. A
        # second call, devin rm, deletes the run's session.
        fake(self.fakes / "devin", "\n".join([
            'fakes="${0%/*}"',
            'if [ "$1" = rm ]; then echo "$@" > "$fakes/removed"; exit 0; fi',
            'printf "%s\\n" "$@" > "$fakes/args"',
            'env > "$fakes/env"',
            'pwd > "$fakes/cwd"',
            'ls -A > "$fakes/files"',
            'cp prompt.md "$fakes/prompt"',
            'while [ $# -gt 1 ]; do',
            '  case "$1" in --config) cp "$2" "$fakes/config.json" ;; --export) export="$2" ;; esac',
            '  shift',
            'done',
            'cp "$fakes/answer" answer.md',
            """echo '{"session_id": "session-7", "steps": []}' > "$export\"""",
        ]))
        self.assertEqual(self.launch("devin"), "They hide a change.\n")
        args = self.recorded("args")
        for flag, value in [("--permission-mode", "accept-edits"), ("--model", "devin-review-model"),
                            ("--config", f"{self.prefix}.devin-config.json"),
                            ("--export", f"{self.prefix}.transcript.json")]:
            self.assertEqual(args[args.index(flag) + 1], value)
        # The user's config without its allow rules, which could approve a command or a write
        # outside the working directory, and with the shell and Slack denied.
        used = json.loads((self.fakes / "config.json").read_text())
        self.assertEqual(used["agent"], {"model": "user-model"})
        self.assertEqual(used["permissions"]["allow"], [])
        self.assertEqual(sorted(used["permissions"]["deny"]),
                         sorted(["Exec(rm)", "exec", "mcp__slack-remote__*", "mcp__slack__*"]))
        env = [line.split("=", 1)[0] for line in self.recorded("env")]
        self.assertNotIn("ACP_BACKEND", env)
        self.assertNotIn("WINDSURF_EXT_HOST_PID", env)
        # It starts in an empty directory, not the run's, with only its prompt, which asks for the
        # answer in answer.md through the file-writing tool.
        [cwd] = self.recorded("cwd")
        self.assertNotIn(str(self.run), cwd)
        self.assertEqual(self.recorded("files"), ["prompt.md"])
        prompt = (self.fakes / "prompt").read_text()
        self.assertTrue(prompt.startswith(self.prompt.read_text()))
        self.assertIn("answer.md in the working directory with your file-writing tool", prompt)
        self.assertEqual(self.recorded("removed"), ["rm session-7 --force"])


class ReviewsTest(unittest.TestCase):
    """The reviews bin/whooper-status --json lists and the review bin/whooper-show --json prints, read
    from review runs of example-org/example-repo in a throwaway state folder, with times in Los
    Angeles. Both read files only, and gh, slackdump, claude, and devin are fakes that fail, so no test
    can reach GitHub, Slack, or a model."""

    LA = ZoneInfo("America/Los_Angeles")
    TITLE = "Return 422 for bad order JSON"
    NIT = finding(id="2", kind="nit", nit_kind="wording", path="README.md", line=3, title="Old flag name",
                  draft="**nit:** This line still names the old --fast flag.")
    DIFF = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -3 +3 @@\n-fast\n+quick\n"
    REQUEST = {"commit_id": "h1", "event": "APPROVE", "body": "", "comments": []}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        for name in ["gh", "slackdump", "claude", "devin"]:
            fake(tmp / "bin" / name, "exit 1")
        self.env = {**os.environ, "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "XDG_STATE_HOME": str(tmp),
                    "XDG_CACHE_HOME": str(tmp / "cache"), "TZ": "America/Los_Angeles"}
        self.state = tmp / "whooper"
        self.reviewer = None
        # A run ID holds its run's start time to the second.
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def local(self, when):
        """A time as whooper-status and whooper-show print it here: in Los Angeles, in ISO 8601 with the
        offset."""
        return when.astimezone(self.LA).isoformat(timespec="seconds")

    def started(self, run):
        """When the run started, which its ID says."""
        return datetime.strptime(run.name.split("-")[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)

    def make_run(self, ago, pr=12, run_json=True, repo="example-org/example-repo"):
        """Makes the directory of a review run of <repo>#<pr> that started ago before now, with the
        run.json whooper-review writes unless run_json is False, and returns it."""
        started = self.now - ago
        run = self.state / "runs" / f"{started:%Y%m%dT%H%M%SZ}-{pr}"
        run.mkdir(parents=True)
        if run_json:
            (run / "run.json").write_text(json.dumps({
                "kind": "first_review", "repo": repo, "pr": pr, "head_sha": "h1",
                "base_ref": "master", "source": "github_request", "slack": None, "reason": "r", "run_id": run.name,
                "merge_base": "m1", "as_of": None, "dry_run": True, "agent": "claude", "skills": {}}))
        return run

    def write(self, run, name, content, minutes):
        """Writes the run's file name, content as JSON unless it is a string, as written minutes after the
        run started."""
        path = run / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content))
        when = (self.started(run) + timedelta(minutes=minutes)).timestamp()
        os.utime(path, (when, when))

    def fetched(self, run):
        """Gives the run the diff and the PR's conversation whooper-review saves before the review starts."""
        pr = int(run.name.split("-")[1])
        self.write(run, "diff.patch", self.DIFF, 1)
        self.write(run, "github.json", {"pr": {"url": f"https://github.com/example-org/example-repo/pull/{pr}",
                                               "number": pr, "title": self.TITLE, "author": "alice", "body": ""},
                                        "as_of": None, "reviews": [], "comments": []}, 1)

    def findings(self, verdict="look_first", findings=(), skip_reason=""):
        """A findings.json that fits schemas/findings.json. With a skip_reason, the review skipped the PR,
        as it does a large PR from an untagged ask."""
        return {"status": "skipped" if skip_reason else "reviewed", "skip_reason": skip_reason,
                "approval": {"verdict": verdict, "reason": "Ran the orders tests."}, "findings": list(findings)}

    def finish(self, run, *args, **kwargs):
        """Fetches the run and ends it as a review that wrote its findings 20 minutes after it started."""
        self.fetched(run)
        self.write(run, "findings.json", self.findings(*args, **kwargs), 20)

    def hold(self, run, pid=None):
        """Has the lock of the run's PR name the run and the process pid, by default a stand-in for a live
        whooper-review, which one test shares, as whooper-review's lock does while it reviews."""
        if pid is None:
            if self.reviewer is None:
                self.reviewer = review_stand_in(self, Path(self.tmp.name) / "review")
            pid = self.reviewer
        lock = self.state / "locks" / f"example-org-example-repo-{run.name.split('-')[1]}"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{pid}\n")
        (lock / "run_id").write_text(f"{run.name}\n")

    def record(self, pr, head, started, status="done"):
        """Writes the record of first reviews of example-org/example-repo#<pr> at head, started at started."""
        path = self.state / "prs" / f"example-org-example-repo-{pr}" / f"{head}.first_review.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": status, "failures": 0, "started_at": started.isoformat()}))

    def stopped(self, ago, pr, cause):
        """Makes a run of example-org/example-repo#<pr> that started ago before now and that whooper-review
        stopped at cause 2 minutes later, and returns it."""
        run = self.make_run(ago, pr=pr)
        self.fetched(run)
        self.write(run, "failure.txt", "the reason whooper-review gave\n", 2)
        self.write(run, "failure.json", CAUSES[cause], 2)
        return run

    def waiting(self, ago, pr, cause="docker_unavailable"):
        """Makes the run of example-org/example-repo#<pr> that whooper-round did not start, ago before now,
        because an earlier review in its round stopped at cause, as wait_review leaves it, and returns it."""
        run = self.make_run(ago, pr=pr, run_json=False)
        (run / "run.json").write_text(json.dumps({
            "kind": "first_review", "repo": "example-org/example-repo", "pr": pr, "head_sha": "h1", "base_ref": "master",
            "source": "github_request", "slack": None, "reason": "r", "run_id": run.name, "agent": "claude"}))
        (run / "waiting.json").write_text(json.dumps(CAUSES[cause]))
        return run

    def status(self):
        result = subprocess.run([str(ROOT / "bin" / "whooper-status"), "--json"], env=self.env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def reviews(self):
        return self.status()["reviews"]

    def review(self, run, **fields):
        """What whooper-status lists for the run, given the fields that differ from a review that ended
        with a look_first verdict and no findings 20 minutes after it started, read its PR's title and
        author, and waits for the user."""
        started, pr = self.started(run), int(run.name.split("-")[1])
        return {"run_id": run.name, "repo": "example-org/example-repo", "pr": pr, "title": self.TITLE,
                "author": "alice",
                "url": f"https://github.com/example-org/example-repo/pull/{pr}", "started_at": self.local(started),
                "ended_at": self.local(started + timedelta(minutes=20)), "outcome": "reviewed",
                "verdict": "look_first", "skipped": False, "findings": 0, "safe_to_approve": False, "submitted": None,
                "needs_you": True, "cause": None, "head_sha": "h1", "pr_head_sha": None, "skipped_by_user": False,
                "removed_by_user": False, **fields}

    def show(self, *args):
        return subprocess.run([str(ROOT / "bin" / "whooper-show"), *args], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def shown(self, run):
        """What whooper-show prints for the run."""
        result = self.show(run.name, "--json")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return json.loads(result.stdout)

    def test_status_lists_the_reviews_of_the_last_30_days_newest_first(self):
        reviewed = self.make_run(timedelta(hours=1))
        self.finish(reviewed, findings=[finding(), self.NIT])
        submitted = self.make_run(timedelta(hours=3), pr=13)
        self.finish(submitted, "approve_once_ci_passes", [self.NIT])
        self.write(submitted, "submit.json", {"status": "posted", "request": self.REQUEST,
                                              "response": {"id": 7001}}, 30)
        # A submit stopped while gh posted leaves submit.json saying posting. Its review may or may not be
        # on GitHub, so it is not submitted, and whooper-submit asks the user to check the PR.
        posting = self.make_run(timedelta(hours=4), pr=19)
        self.finish(posting, "approve_once_ci_passes", [self.NIT])
        self.write(posting, "submit.json", {"status": "posting", "request": self.REQUEST}, 30)
        skipped = self.make_run(timedelta(hours=5), pr=14)
        self.finish(skipped, skip_reason="Not small or mechanical.")
        # Its findings did not fit the schema, so whooper-review set them aside and saved why.
        failed = self.make_run(timedelta(hours=7), pr=15)
        self.fetched(failed)
        self.write(failed, "findings.invalid.json", {"findings": [finding()]}, 20)
        self.write(failed, "failure.txt", "the review run's findings.json does not fit the schema\n", 21)
        # A Devin review writes findings.json itself, so it can pass its time limit after writing findings
        # that fit. whooper-review counts it as failed, and the findings stay for the user to read.
        timed_out = self.make_run(timedelta(hours=9), pr=18)
        self.finish(timed_out, findings=[finding()])
        self.write(timed_out, "failure.txt", "the review run passed its 45-minute limit\n", 45)
        month = self.make_run(timedelta(days=29), pr=16)
        self.finish(month)
        self.finish(self.make_run(timedelta(days=31), pr=17))
        self.assertEqual(self.reviews(), [
            self.review(reviewed, findings=2),
            self.review(submitted, verdict="approve_once_ci_passes", findings=1, safe_to_approve=True, needs_you=False,
                        submitted={"event": "APPROVE",
                                   "at": self.local(self.started(submitted) + timedelta(minutes=30))}),
            self.review(posting, verdict="approve_once_ci_passes", findings=1, safe_to_approve=True),
            self.review(skipped, skipped=True),
            self.review(failed, outcome="failed", verdict=None, cause=CAUSES["agent_no_findings"],
                        ended_at=self.local(self.started(failed) + timedelta(minutes=21))),
            self.review(timed_out, outcome="failed", findings=1, cause=CAUSES["time_limit"],
                        ended_at=self.local(self.started(timed_out) + timedelta(minutes=45))),
            self.review(month),
        ])

    def test_status_lists_each_prs_newest_review_as_an_item_of_status_format_2(self):
        now = datetime.now(timezone.utc).astimezone(self.LA)
        ready = self.make_run(timedelta(hours=1))
        self.finish(ready, "approve_once_ci_passes", [self.NIT])
        # An older run of the same PR, which the newer one supersedes, lists as no item.
        self.finish(self.make_run(timedelta(hours=5)), findings=[finding()])
        look = self.make_run(timedelta(hours=2), pr=13)
        self.finish(look)
        failed = self.stopped(timedelta(hours=3), 14, "time_limit")
        submitted = self.make_run(timedelta(hours=4), pr=17)
        self.finish(submitted, "approve_once_ci_passes", [self.NIT])
        self.write(submitted, "submit.json", {"status": "posted", "request": self.REQUEST, "response": {"id": 7001}}, 30)
        removed = self.make_run(timedelta(hours=6), pr=18)
        self.finish(removed)
        (removed / "removed-by-user").touch()
        reviewing = self.make_run(timedelta(minutes=10), pr=15)
        self.fetched(reviewing)
        self.hold(reviewing)
        # A review that waits read no github.json yet, so its title is its PR.
        waits = self.waiting(timedelta(minutes=30), 16)
        status = self.status()
        schema = json.loads((ROOT / "schemas" / "prompting-time-status.json").read_text())
        # The schema allows other top-level fields, such as format 1's, which stay beside format 2's
        # until Prompting Time reads format 2 alone.
        check_schema(status, schema, "status")

        def item(run, group, dot, detail, actions=("retry", "skip", "remove"), title=self.TITLE, updated=None):
            return {"id": run.name, "group": group, "title": title, "detail": detail, "dot": dot, "project": None,
                    "updated_at": self.local(updated or self.started(run) + timedelta(minutes=20)),
                    "route": f"/reviews/{run.name}", "actions": list(actions)}

        self.assertEqual(status["items"], [
            item(reviewing, "reviewing", "running",
                 f"example-repo#15 · @alice · reviewing since {short_date(self.local(self.started(reviewing)), now)}",
                 actions=("skip",), updated=self.started(reviewing)),
            item(waits, "waiting", "idle", "example-repo#16 · waiting · Docker isn't running",
                 title="example-org/example-repo#16", updated=self.started(waits)),
            item(ready, "ready", "success", "example-repo#12 · @alice · 1 finding"),
            item(look, "look", "attention", "example-repo#13 · @alice · no findings"),
            item(failed, "failed", "error", "example-repo#14 · @alice · The review ran past its time limit",
                 updated=self.started(failed) + timedelta(minutes=2)),
            # Finished items come after the current ones.
            item(submitted, "done", "idle",
                 f"example-repo#17 · @alice · approved {short_date(self.local(self.started(submitted) + timedelta(minutes=30)), now)}",
                 actions=("retry", "remove"), updated=self.started(submitted) + timedelta(minutes=30)),
        ])
        self.assertFalse(status["truncated"])
        # The newest review that ended waits at a cause that stops every review.
        self.assertEqual(status["blockers"], [{"id": "docker_unavailable", "title": "Docker isn't running",
                                               "dot": "error", "route": "/causes/docker_unavailable",
                                               "action": "round"}])
        self.assertEqual(status["agent"], {"words": "Can't review · Docker isn't running", "dot": "error"})

    def test_a_review_runs_while_a_live_whooper_review_holds_its_prs_lock_for_it(self):
        earlier = self.make_run(timedelta(hours=2))
        self.finish(earlier)
        # A Devin review writes findings.json itself, and may change it until it ends, so its findings do
        # not count yet. The lock names a live whooper-review.
        current = self.make_run(timedelta(minutes=30))
        self.fetched(current)
        self.write(current, "findings.json", self.findings(findings=[finding()]), 25)
        self.hold(current)
        # whooper-review takes the lock before it makes the run directory, and writes run.json, which
        # names the repo, after it fetches.
        fetching = self.make_run(timedelta(minutes=1), pr=20, run_json=False)
        self.hold(fetching)
        # A whooper-review killed with kill -9 leaves its lock, whose process is gone, and no failure.txt.
        gone = subprocess.Popen(["true"])
        gone.wait()
        killed = self.make_run(timedelta(hours=3), pr=21)
        self.fetched(killed)
        self.hold(killed, gone.pid)
        running = {"outcome": "running", "ended_at": None, "verdict": None, "needs_you": False}
        self.assertEqual(self.reviews(), [
            self.review(fetching, **running, repo=None, url=None, title=None, author=None, head_sha=None),
            self.review(current, **running),
            # The run the lock names is newer, so the earlier run of its PR no longer needs the user.
            self.review(earlier, needs_you=False),
            self.review(killed, outcome="failed", verdict=None, ended_at=None, cause=CAUSES["unknown"]),
        ])

    def test_a_lock_whose_process_is_now_another_program_is_stale(self):
        # The lock of a whooper-review killed with kill -9 stays, and the system may give its process ID to
        # another program, here this test, so the review ended as one whose process is gone did.
        reused = self.make_run(timedelta(hours=3))
        self.fetched(reused)
        self.hold(reused, os.getpid())
        self.assertEqual(self.reviews(), [
            self.review(reused, outcome="failed", verdict=None, ended_at=None, cause=CAUSES["unknown"]),
        ])

    def test_a_newer_run_of_a_pr_supersedes_its_older_runs(self):
        # Only the newest run of a PR can need the user, as when whooper-submit found new commits after the
        # older review and the next round reviewed them.
        older = self.make_run(timedelta(hours=5))
        self.finish(older, findings=[finding()])
        newer = self.make_run(timedelta(hours=1))
        self.finish(newer)
        # The same number in another repo is another PR.
        elsewhere = self.make_run(timedelta(hours=3), repo="sample-org/plain-repo")
        self.finish(elsewhere)
        # A newer run supersedes a failed one too, from the time it starts.
        failed = self.make_run(timedelta(hours=6), pr=13)
        self.fetched(failed)
        self.write(failed, "failure.txt", "the review run passed its 45-minute limit\n", 45)
        running = self.make_run(timedelta(minutes=10), pr=13)
        self.fetched(running)
        self.hold(running)
        self.assertEqual(self.reviews(), [
            self.review(running, outcome="running", ended_at=None, verdict=None, needs_you=False),
            self.review(newer),
            self.review(elsewhere, repo="sample-org/plain-repo", url="https://github.com/sample-org/plain-repo/pull/12"),
            self.review(older, findings=1, needs_you=False),
            self.review(failed, outcome="failed", verdict=None, needs_you=False, cause=CAUSES["time_limit"],
                        ended_at=self.local(self.started(failed) + timedelta(minutes=45))),
        ])

    def test_a_run_directory_whooper_cannot_read_lists_as_a_failed_review(self):
        # whooper-review stopped these before run.json was whole: its fetch failed, so it wrote none, or
        # the skills could not be hashed, which leaves the file empty. One cut short stands for any
        # run.json that does not read as JSON. Nothing names their repo.
        unreadable = []
        for hours, run_json in [(1, None), (2, ""), (3, '{"repo": "example-org/exa')]:
            run = self.make_run(timedelta(hours=hours), pr=30 + hours, run_json=False)
            if run_json is not None:
                (run / "run.json").write_text(run_json)
            self.write(run, "failure.txt",
                       "the review stopped at an unexpected error; see whooper-review's error output\n", 1)
            unreadable.append(run)
        # The review run exited with findings.json empty.
        empty = self.make_run(timedelta(hours=4), pr=40)
        self.fetched(empty)
        self.write(empty, "findings.json", "", 20)
        self.write(empty, "failure.txt", "the review run exited 0 without findings; see its stderr.log\n", 21)
        # A Devin review killed with kill -9 while it wrote findings.json left one that does not fit.
        unfit = self.make_run(timedelta(hours=5), pr=41)
        self.fetched(unfit)
        self.write(unfit, "findings.json", {"findings": [finding()]}, 20)
        # Finder's .DS_Store, and a folder not named as whooper-review names runs, are not runs.
        (self.state / "runs" / ".DS_Store").write_text("")
        (self.state / "runs" / "notes").mkdir()
        failed = {"outcome": "failed", "verdict": None}
        self.assertEqual(self.reviews(), [
            *[self.review(run, **failed, repo=None, url=None, title=None, author=None, cause=CAUSES["unknown"],
                          head_sha=None, ended_at=self.local(self.started(run) + timedelta(minutes=1)))
              for run in unreadable],
            self.review(empty, **failed, cause=CAUSES["agent_no_findings"],
                        ended_at=self.local(self.started(empty) + timedelta(minutes=21))),
            self.review(unfit, **failed, cause=CAUSES["unknown"]),
        ])

    def test_status_and_show_take_the_title_and_author_from_github_json(self):
        # run.json has neither, so both come from github.json, the PR conversation the review read.
        run = self.make_run(timedelta(hours=1))
        self.finish(run)
        # whooper-review saves no github.json when the review's GitHub fetch fails. The other files are not
        # the github.json it writes: one cut short, one without the PR, one whose PR is text, and one whose
        # title and author are not text.
        unread = []
        for hours, github in [(2, None), (3, '{"pr": {"title": "Return 4'), (4, {"reviews": [], "comments": []}),
                              (5, {"pr": self.TITLE}), (6, {"pr": {"title": 422, "author": {"login": "alice"}}})]:
            other = self.make_run(timedelta(hours=hours), pr=20 + hours)
            if github is not None:
                self.write(other, "github.json", github, 1)
            self.write(other, "findings.json", self.findings(), 20)
            unread.append(other)
        self.assertEqual(self.reviews(), [self.review(run),
                                          *[self.review(other, title=None, author=None) for other in unread]])
        shown = [self.shown(r) for r in [run, *unread]]
        self.assertEqual([(s["title"], s["author"]) for s in shown],
                         [(self.TITLE, "alice")] + [(None, None)] * len(unread))

    def test_status_and_show_name_what_stopped_each_failed_review(self):
        # whooper-review saves the cause in failure.json beside failure.txt as it stops a review.
        saved = self.make_run(timedelta(hours=1))
        self.fetched(saved)
        self.write(saved, "failure.txt", "/config/setup.sh failed: Cannot connect to the Docker daemon at"
                   " unix:///var/run/docker.sock. Is the docker daemon running?\n", 2)
        self.write(saved, "failure.json", CAUSES["docker_unavailable"], 2)
        # Reviews from before failure.json get the cause that whooper-review's wording of failure.txt at
        # each step names, and unknown for any other.
        older = []
        for hours, (text, cause) in enumerate([
            ("the review run passed its 45-minute limit; see /state/runs/x/stderr.log and the transcript in"
             " /state/runs/x\n", "time_limit"),
            ("the review run exited 1 without findings; see /state/runs/x/stderr.log and the transcript in"
             " /state/runs/x\n", "agent_no_findings"),
            ("the review run's findings.json does not fit the schema\n", "agent_no_findings"),
            ("/Users/sam/.config/whooper/setup.sh failed\n", "setup_failed"),
            ("no clone of example-org/example-repo at /cache/repos/example-org-example-repo; see README.md\n",
             "repo_not_configured"),
            ("the review was interrupted\n", "unknown"),
            ("the review stopped at an unexpected error; see whooper-review's error output\n", "unknown"),
        ], start=2):
            run = self.make_run(timedelta(hours=hours), pr=20 + hours)
            self.fetched(run)
            self.write(run, "failure.txt", text, 2)
            older.append((run, cause))
        # A failure.json cut short reads as missing, so failure.txt names the cause.
        cut = self.make_run(timedelta(hours=10), pr=30)
        self.fetched(cut)
        self.write(cut, "failure.txt", "/config/setup.sh failed\n", 2)
        self.write(cut, "failure.json", '{"id": "docker_unav', 2)
        expected = {saved.name: CAUSES["docker_unavailable"], cut.name: CAUSES["setup_failed"],
                    **{run.name: CAUSES[cause] for run, cause in older}}
        self.assertEqual({r["run_id"]: r["cause"] for r in self.reviews()}, expected)
        self.assertEqual({run_id: self.shown(self.state / "runs" / run_id)["cause"] for run_id in expected}, expected)

    def test_a_review_that_waits_needs_no_one_and_names_the_cause_it_waits_on(self):
        older = self.make_run(timedelta(hours=5), pr=13)
        self.finish(older)
        # A round's review of PR 12 could not reach Docker, so its review of PR 13 waits.
        failed = self.stopped(timedelta(hours=2), 12, "docker_unavailable")
        waits = self.waiting(timedelta(hours=2) - timedelta(minutes=3), 13)
        self.assertEqual(self.reviews(), [
            self.review(waits, outcome="waiting", cause=CAUSES["docker_unavailable"], needs_you=False, ended_at=None,
                        verdict=None, title=None, author=None),
            self.review(failed, outcome="failed", cause=CAUSES["docker_unavailable"], verdict=None,
                        ended_at=self.local(self.started(failed) + timedelta(minutes=2))),
            # PR 13's newer run supersedes this one, though it waits.
            self.review(older, needs_you=False),
        ])
        # It has no findings, drafts, or diff yet, since its review has not fetched the merge base.
        self.assertEqual(self.shown(waits), {
            "version": 1, "run_id": waits.name, "run": json.loads((waits / "run.json").read_text()), "title": None,
            "author": None, "outcome": "waiting", "findings": None, "safe_to_approve": False, "drafts": None,
            "diff": None, "failure": None, "asks": [], "submitted": None, "cause": CAUSES["docker_unavailable"],
            "head_sha": "h1", "skipped_by_user": False, "removed_by_user": False, "overview": None,
            "commits_since_review": None, "co_change": [], "connections": None, "test_gaps": None, "examples": None})
        # An ask about it may read why it waits.
        prompt = io.StringIO()
        with redirect_stdout(prompt):
            main("prompt", "ask", "claude", str(waits), "Why does this review wait?")
        self.assertIn(f"- {waits / 'waiting.json'}: ", prompt.getvalue())

    def test_whooper_is_blocked_from_a_failure_that_stops_every_review_until_a_review_does_not_stop_there(self):
        self.assertIsNone(self.status()["blocked"])
        self.finish(self.make_run(timedelta(hours=9), pr=11))
        self.assertIsNone(self.status()["blocked"])
        # A review whose setup script could not reach Docker blocks Whooper from when it stopped.
        first = self.stopped(timedelta(hours=8), 12, "docker_unavailable")
        since = self.local(self.started(first) + timedelta(minutes=2))
        self.assertEqual(self.status()["blocked"], {**CAUSES["docker_unavailable"], "since": since, "waiting": 0})
        # The rest of its round waits, the next round's review stops there again, and a review runs now,
        # which has not shown yet whether Docker is back. Two PRs' newest reviews wait.
        self.waiting(timedelta(hours=8) - timedelta(minutes=3), 13)
        self.waiting(timedelta(hours=8) - timedelta(minutes=4), 14)
        self.stopped(timedelta(hours=7), 12, "docker_unavailable")
        self.hold(self.make_run(timedelta(minutes=5), pr=15))
        self.assertEqual(self.status()["blocked"], {**CAUSES["docker_unavailable"], "since": since, "waiting": 2})
        # A review that stops at a cause of its PR's own got past the setup script, so Docker runs again.
        self.stopped(timedelta(hours=6), 16, "time_limit")
        self.assertIsNone(self.status()["blocked"])
        # Another cause that stops every review blocks Whooper again, from its own failure.
        setup = self.stopped(timedelta(hours=5), 17, "setup_failed")
        self.assertEqual(self.status()["blocked"], {**CAUSES["setup_failed"], "waiting": 2,
                                                    "since": self.local(self.started(setup) + timedelta(minutes=2))})
        # A review that succeeds ends it.
        self.finish(self.make_run(timedelta(hours=4), pr=18))
        self.assertIsNone(self.status()["blocked"])

    def test_status_gives_the_head_each_review_read_and_the_newest_head_whooper_knows_of_its_pr(self):
        # Each review read h1, as its run.json says, and whooper-review's record dates h1 by its start.
        runs = {pr: self.make_run(timedelta(hours=pr - 10), pr=pr) for pr in [11, 12, 13, 14, 15]}
        for pr, run in runs.items():
            self.finish(run)
            if pr != 15:
                self.record(pr, "h1", self.started(run))
        # whooper-review stopped this one before it wrote run.json, which names the repo and head.
        stopped = self.make_run(timedelta(hours=6), pr=16, run_json=False)
        self.write(stopped, "failure.txt", "the review stopped at an unexpected error\n", 1)
        # Half an hour ago the latest poll saw PR 11 at h2 and PR 13 still at h1, and listed a Slack ask
        # for PR 14 at h4; then whooper-retry queued PR 13 at h3, and whooper-submit found PR 12 at h5.
        half_hour_ago = (self.now - timedelta(minutes=30)).timestamp()
        poll = self.state / "poll"
        poll.mkdir()
        (poll / "github.json").write_text(json.dumps([
            {"repository": {"nameWithOwner": "example-org/example-repo"}, "number": number, "headRefOid": head}
            for number, head in [(11, "h2"), (13, "h1")]]))
        (poll / "20310727T020000Z.json").write_text(json.dumps({"items": [
            {"repo": "example-org/example-repo", "pr": 14, "head_sha": "h4", "kind": "first_review"}]}))
        for path in poll.iterdir():
            os.utime(path, (half_hour_ago, half_hour_ago))
        self.record(13, "h3", self.now - timedelta(minutes=10), status="retry")
        (self.state / "rechecks").mkdir()
        (self.state / "rechecks" / "example-org-example-repo-12.json").write_text(json.dumps(
            {"repo": "example-org/example-repo", "pr": 12, "head_sha": "h5", "kind": "first_review"}))
        self.assertEqual([(r["pr"], r["head_sha"], r["pr_head_sha"]) for r in self.reviews()],
                         [(11, "h1", "h2"), (12, "h1", "h5"), (13, "h1", "h3"), (14, "h1", "h4"), (15, "h1", None),
                          (16, None, None)])
        self.assertEqual((self.shown(runs[11])["head_sha"], self.shown(stopped)["head_sha"]), ("h1", None))

    def test_status_and_show_call_a_review_safe_to_approve_only_once_it_is_reviewed(self):
        # whooper-submit approves a run without --approve-anyway when its verdict is approve_once_ci_passes,
        # every finding is a nit, and it did not skip the PR (SubmitTest), as for the first run.
        safe = self.make_run(timedelta(hours=1))
        self.finish(safe, "approve_once_ci_passes", [self.NIT])
        look_first = self.make_run(timedelta(hours=2), pr=13)
        self.finish(look_first, findings=[self.NIT])
        # The next two have the first run's findings, but neither review finished: a Devin review that
        # still runs may change its findings.json, and one that passed its time limit after writing
        # findings that fit failed.
        running = self.make_run(timedelta(minutes=10), pr=14)
        self.finish(running, "approve_once_ci_passes", [self.NIT])
        self.hold(running)
        timed_out = self.make_run(timedelta(hours=3), pr=15)
        self.finish(timed_out, "approve_once_ci_passes", [self.NIT])
        self.write(timed_out, "failure.txt", "the review run passed its 45-minute limit\n", 45)
        # A review that waits has no findings yet, and a run directory Whooper cannot read has none, as when
        # whooper-review stopped before it wrote run.json.
        waits = self.waiting(timedelta(hours=4), 16)
        unreadable = self.make_run(timedelta(hours=5), pr=17, run_json=False)
        self.write(unreadable, "failure.txt", "the review stopped at an unexpected error\n", 1)
        expected = {safe.name: True, look_first.name: False, running.name: False, timed_out.name: False,
                    waits.name: False, unreadable.name: False}
        self.assertEqual({r["run_id"]: r["safe_to_approve"] for r in self.reviews()}, expected)
        self.assertEqual({run_id: self.shown(self.state / "runs" / run_id)["safe_to_approve"] for run_id in expected},
                         expected)

    def test_show_prints_a_finished_review(self):
        run = self.make_run(timedelta(hours=1))
        self.finish(run, findings=[finding(), self.NIT])
        shown = self.shown(run)
        self.assertEqual(shown, {
            "version": 1, "run_id": run.name, "run": json.loads((run / "run.json").read_text()),
            "title": self.TITLE, "author": "alice",
            "outcome": "reviewed", "findings": self.findings(findings=[finding(), self.NIT]),
            # The run is not safe to approve, so until the user changes anything the review only comments
            # and includes nothing.
            "safe_to_approve": False, "drafts": {"event": "COMMENT", "message": "", "findings": {}},
            "diff": self.DIFF, "failure": None, "asks": [], "submitted": None, "cause": None, "head_sha": "h1",
            "skipped_by_user": False, "removed_by_user": False,
            # The run wrote no overview, nothing names a newer head of the PR, the cache has no clone, and the
            # review kept no connections.json.
            "overview": None, "commits_since_review": None, "co_change": [], "connections": None,
            "test_gaps": None, "examples": None,
        })
        drafts = subprocess.run([str(ROOT / "bin" / "whooper-draft"), run.name], env=self.env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(json.loads(drafts.stdout), shown["drafts"])
        # Prompting Time runs the manifest's show-review action with the run ID after its fixed arguments.
        [action] = [a for a in json.loads((ROOT / "prompting-time-plugin.json").read_text())["actions"]
                    if a["name"] == "show-review"]
        command = action["command"]
        result = subprocess.run([str(ROOT / command[0]), *command[1:], run.name], env=self.env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, shown), result.stderr)

    def test_show_prints_a_review_that_failed_or_still_runs(self):
        failed = self.make_run(timedelta(hours=1))
        self.fetched(failed)
        reason = f"the review run passed its 45-minute limit; see {failed}/stderr.log and the transcript in {failed}\n"
        self.write(failed, "failure.txt", reason, 45)
        nothing = {"version": 1, "findings": None, "safe_to_approve": False, "drafts": None, "asks": [],
                   "submitted": None, "overview": None, "commits_since_review": None, "co_change": [],
                   "connections": None, "test_gaps": None, "examples": None}
        self.assertEqual(self.shown(failed), {
            **nothing, "run_id": failed.name, "run": json.loads((failed / "run.json").read_text()),
            "title": self.TITLE, "author": "alice", "outcome": "failed", "diff": self.DIFF, "failure": reason,
            "cause": CAUSES["time_limit"], "head_sha": "h1", "skipped_by_user": False, "removed_by_user": False,
        })
        # A Devin review that wrote findings that fit before its time limit has them and whooper-draft's
        # drafts, though it failed.
        self.write(failed, "findings.json", self.findings(findings=[finding()]), 40)
        self.assertEqual({key: self.shown(failed)[key] for key in ["outcome", "findings", "drafts", "failure"]},
                         {"outcome": "failed", "findings": self.findings(findings=[finding()]),
                          "drafts": {"event": "COMMENT", "message": "", "findings": {}}, "failure": reason})
        # whooper-review stopped this review before it wrote run.json.
        stopped = self.make_run(timedelta(hours=2), pr=13, run_json=False)
        self.write(stopped, "failure.txt", "the review stopped at an unexpected error\n", 1)
        self.assertEqual(self.shown(stopped), {
            **nothing, "run_id": stopped.name, "run": None, "title": None, "author": None, "outcome": "failed",
            "diff": None, "failure": "the review stopped at an unexpected error\n", "cause": CAUSES["unknown"],
            "head_sha": None, "skipped_by_user": False, "removed_by_user": False,
        })
        # A Devin review that is still running may still change the findings.json it wrote, so it shows
        # no findings yet.
        running = self.make_run(timedelta(minutes=5), pr=14)
        self.fetched(running)
        self.write(running, "findings.json", self.findings(findings=[finding()]), 4)
        self.hold(running)
        self.assertEqual(self.shown(running), {
            **nothing, "run_id": running.name, "run": json.loads((running / "run.json").read_text()),
            "title": self.TITLE, "author": "alice", "outcome": "running", "diff": self.DIFF, "failure": None,
            "cause": None, "head_sha": "h1", "skipped_by_user": False, "removed_by_user": False,
        })

    def test_show_gives_overview_json_once_the_review_ended_and_only_when_it_keeps_every_rule(self):
        # An overview of DIFF, which changes line 3 of README.md.
        kept = {"summary": "Renames the --fast flag to --quick in the README.",
                "verdict_reason": "Only a word changes.", "before_after": [], "places": [], "flow": None,
                "checked": ["Read the diff."], "not_checked": [],
                "layers": [{"id": "l1", "title": "README", "intent": "The flag's new name.", "read_hint": "read",
                            "ranges": [{"path": "README.md", "start_line": 3, "end_line": 3}]}]}
        run = self.make_run(timedelta(hours=1))
        self.finish(run)
        self.write(run, "overview.json", kept, 19)
        self.assertEqual(self.shown(run)["overview"], kept)
        # whooper-review moves an overview.json that breaks a rule aside as soon as the run ends, but a review it
        # stopped before then, as at an interrupt, can leave one, which is no overview either. One that keeps
        # every rule still comes, though the review failed.
        interrupted = self.stopped(timedelta(hours=2), 13, "unknown")
        for content, shown in [({**kept, "layers": []}, None), ('{"summary": "Renames', None), (kept, kept)]:
            with self.subTest(content=content):
                self.write(interrupted, "overview.json", content, 1)
                self.assertEqual({key: self.shown(interrupted)[key] for key in ["outcome", "overview"]},
                                 {"outcome": "failed", "overview": shown})
        # While the review runs, the run may still change the file.
        running = self.make_run(timedelta(minutes=5), pr=14)
        self.fetched(running)
        self.write(running, "overview.json", kept, 4)
        self.hold(running)
        self.assertEqual({key: self.shown(running)[key] for key in ["outcome", "overview"]},
                         {"outcome": "running", "overview": None})

    def test_show_gives_connections_json_once_the_review_ended(self):
        graph = {"nodes": [{"id": "README.md:x", "name": "x", "kind": "var", "path": "README.md", "line": 3,
                            "change": "changed", "lines_changed": 2}], "edges": [], "limits": [ONE_LEVEL]}
        run = self.make_run(timedelta(hours=1))
        self.finish(run)
        self.write(run, "connections.json", graph, 19)
        self.assertEqual(self.shown(run)["connections"], graph)
        # One that is not JSON, which whooper-review's step never leaves, since it writes the file whole, shows none.
        self.write(run, "connections.json", '{"nodes": [', 19)
        self.assertEqual(self.shown(run)["connections"], None)
        # While the review runs, whooper-show shows none, as it shows no overview.
        running = self.make_run(timedelta(minutes=5), pr=14)
        self.fetched(running)
        self.write(running, "connections.json", graph, 4)
        self.hold(running)
        self.assertEqual({key: self.shown(running)[key] for key in ["outcome", "connections"]},
                         {"outcome": "running", "connections": None})

    def test_show_gives_test_gaps_json_and_examples_json_once_the_review_ended(self):
        gaps = {"measured": True, "reason": None, "tool": "GOPROXY=off GOTOOLCHAIN=local go test -count=1 -coverpkg=./a ./a",
                "lines": [{"path": "a/a.go", "line": 4, "status": "not_run"}],
                "summary": {"changed_lines": 2, "run": 1, "not_run": 1}, "limits": [AND_LINES, CHECKED, OWN_TESTS]}
        examples = {"functions": [{"name": "A", "path": "a/a.go", "line": 3}],
                    "rows": [example_row("A", "A(1)", "1", "2", "different", "ran_base_and_head")],
                    "limits": [CHOSE, ONCE_AT_HEAD]}
        run = self.make_run(timedelta(hours=1))
        self.finish(run)
        self.write(run, "test_gaps.json", gaps, 1)
        self.write(run, "examples.json", examples, 19)
        self.assertEqual({key: self.shown(run)[key] for key in ["test_gaps", "examples"]},
                         {"test_gaps": gaps, "examples": examples})
        # While the review runs, the run may still change either file.
        running = self.make_run(timedelta(minutes=5), pr=14)
        self.fetched(running)
        self.write(running, "test_gaps.json", gaps, 1)
        self.write(running, "examples.json", examples, 4)
        self.hold(running)
        self.assertEqual({key: self.shown(running)[key] for key in ["outcome", "test_gaps", "examples"]},
                         {"outcome": "running", "test_gaps": None, "examples": None})

    def test_show_prints_the_drafts_and_the_review_whooper_submit_posted(self):
        run = self.make_run(timedelta(hours=1))
        self.finish(run, "approve_once_ci_passes", [self.NIT])
        drafts = {"event": "APPROVE", "message": "Thanks!",
                  "findings": {"2": {"included": True, "text": "**nit:** --quick"}}}
        self.write(run, "drafts.json", drafts, 25)
        # A submit stopped while gh posted leaves submit.json saying posting, which does not count as
        # submitted, as in whooper-status.
        self.write(run, "submit.json", {"status": "posting", "request": self.REQUEST}, 30)
        self.assertEqual({key: self.shown(run)[key] for key in ["outcome", "drafts", "submitted"]},
                         {"outcome": "reviewed", "drafts": drafts, "submitted": None})
        posted = {"status": "posted", "request": self.REQUEST, "response": {
            "id": 7001, "html_url": "https://github.com/example-org/example-repo/pull/12#pullrequestreview-7001"}}
        self.write(run, "submit.json", posted, 31)
        self.assertEqual({key: self.shown(run)[key] for key in ["outcome", "drafts", "submitted"]},
                         {"outcome": "reviewed", "drafts": drafts, "submitted": posted})

    def test_show_lists_the_asks_in_number_order(self):
        run = self.make_run(timedelta(hours=1))
        self.finish(run, findings=[finding()])
        asks = {n: {"question": f"Question {n}?", "finding": "1" if n == 2 else None, "answer": f"Answer {n}.",
                    "agent": "claude", "started_at": "2031-07-27T02:00:00Z", "ended_at": "2031-07-27T02:01:00Z",
                    "transcript": str(run / "asks" / f"{n}.transcript.jsonl")} for n in (2, 10, 1)}
        for n, ask in asks.items():
            self.write(run, f"asks/{n}.json", ask, 30 + n)
        # Beside the answers: each ask's prompt and transcript, a Devin ask's config, an answer being
        # written, and the prompt of an ask that failed and saved no answer.
        for name in ["1.prompt.md", "1.transcript.jsonl", "10.devin-config.json", "11.json.tmp", "3.prompt.md"]:
            (run / "asks" / name).write_text("{}")
        self.assertEqual(self.shown(run)["asks"], [asks[1], asks[2], asks[10]])

    def skip(self, *args):
        """Runs the manifest's skip action, to which Prompting Time adds the run ID and, to undo, --undo."""
        [action] = [a for a in json.loads((ROOT / "prompting-time-plugin.json").read_text())["actions"]
                    if a["name"] == "skip"]
        self.assertFalse(action["confirm"])
        command = action["command"]
        return subprocess.run([str(ROOT / command[0]), *command[1:], *args], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def test_skipping_a_review_marks_it_done_without_submitting_it_until_undone(self):
        run = self.make_run(timedelta(hours=1))
        self.finish(run, findings=[finding()])
        # The run is its PR's newest, so Whooper starts no other review of the PR until the undo.
        undo = f"Whooper starts no other review of the PR until bin/whooper-skip {run.name} --undo unmarks it"
        skipped = self.skip(run.name)
        self.assertEqual((skipped.returncode, skipped.stdout, skipped.stderr), (
            0, f"marked {run.name} (example-org/example-repo#12) as skipped, without submitting it; {undo}\n", ""))
        self.assertEqual(self.reviews(), [self.review(run, findings=1, skipped_by_user=True, needs_you=False)])
        self.assertEqual(self.shown(run)["skipped_by_user"], True)
        # Nothing went to GitHub, and drafts stay as they were.
        self.assertFalse((run / "submit.json").exists() or (run / "drafts.json").exists())
        self.assertEqual(self.skip(run.name).stdout, f"{run.name} (example-org/example-repo#12) is already marked as"
                                                     " skipped\n")
        # Prompting Time adds --undo after the run ID.
        undone = self.skip(run.name, "--undo")
        self.assertEqual((undone.returncode, undone.stdout),
                         (0, f"unmarked {run.name} (example-org/example-repo#12) as skipped\n"))
        self.assertEqual(self.reviews(), [self.review(run, findings=1)])
        self.assertEqual(self.skip("--undo", run.name).stdout,
                         f"{run.name} (example-org/example-repo#12) is not marked as skipped\n")
        # A row's menu in Prompting Time passes the run ID after --item, as for each of status.item_actions.
        self.assertEqual(self.skip("--item", run.name).stdout, f"marked {run.name} (example-org/example-repo#12) as"
                                                               f" skipped, without submitting it; {undo}\n")
        self.assertEqual(self.skip("--item", run.name, "--undo").stdout,
                         f"unmarked {run.name} (example-org/example-repo#12) as skipped\n")
        # A review whooper-review stopped before it wrote run.json, which names the PR, can be skipped too,
        # though no PR's next review waits on it.
        stopped = self.make_run(timedelta(hours=2), pr=13, run_json=False)
        self.write(stopped, "failure.txt", "the review stopped at an unexpected error\n", 1)
        self.assertEqual(self.skip(stopped.name).stdout, f"marked {stopped.name} as skipped, without submitting it;"
                                                         f" bin/whooper-skip {stopped.name} --undo unmarks it\n")
        self.assertEqual({r["run_id"]: (r["skipped_by_user"], r["needs_you"]) for r in self.reviews()},
                         {run.name: (False, True), stopped.name: (True, False)})

    def test_skipping_a_review_still_running_stops_its_whooper_review(self):
        run = self.make_run(timedelta(minutes=5))
        self.fetched(run)
        name = f"{run.name} (example-org/example-repo#12)"
        then = f"Whooper starts no other review of the PR until bin/whooper-skip {run.name} --undo unmarks it"
        # A stand-in for whooper-review, named so, which notes the TERM a kill sends and stops at it, as
        # whooper-review's trap does.
        tmp, term = Path(self.tmp.name), Path(self.tmp.name) / "term"
        fake(tmp / "review" / "whooper-review",
             f"trap 'echo TERM > {term}; exit 130' TERM\ntouch {tmp / 'ready'}\nwhile :; do sleep 0.1; done")
        review = subprocess.Popen([str(tmp / "review" / "whooper-review")])
        self.addCleanup(lambda: review.poll() is None and review.kill())
        deadline = monotonic() + 10
        while not (tmp / "ready").exists() and monotonic() < deadline:
            sleep(0.05)
        self.hold(run, review.pid)
        result = self.skip(run.name)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, f"marked {name} as skipped, without submitting it, and stopped its review; {then}\n", ""))
        self.assertEqual(review.wait(timeout=10), 130)
        self.assertEqual(term.read_text(), "TERM\n")
        self.assertTrue((run / "skipped-by-user").exists())

    def test_skipping_a_review_that_ends_before_its_signal_marks_it_and_says_nothing_was_stopped(self):
        run = self.make_run(timedelta(minutes=5))
        self.fetched(run)
        # running_runs saw the review's whooper-review, which has ended since, as one may between the two.
        ended = subprocess.Popen(["true"])
        ended.wait()
        with mock.patch.object(whooper, "running_runs", return_value={run.name: ended.pid}):
            said = whooper.skip_review(self.state, run.name)
        self.assertEqual(said, f"marked {run.name} (example-org/example-repo#12) as skipped, without submitting it;"
                               f" Whooper starts no other review of the PR until bin/whooper-skip {run.name} --undo"
                               " unmarks it")
        self.assertTrue((run / "skipped-by-user").exists())

    def test_skip_refuses_an_unknown_run_and_signals_no_other_program(self):
        run = self.make_run(timedelta(minutes=5))
        self.fetched(run)
        for run_id in ["20310101T000000Z-1", "..", "../whooper", f"../runs/{run.name}"]:
            with self.subTest(run_id):
                result = self.skip(run_id)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", f"whooper-skip: no run {run_id} in {self.state / 'runs'}\n"))
        # The PR's lock names the run and a process that exists but is no whooper-review, here this test, as
        # when the system gave the ID of a killed whooper-review to another program. The mark goes on, and
        # the process gets no signal, or this test would stop here.
        self.hold(run, os.getpid())
        name = f"{run.name} (example-org/example-repo#12)"
        then = f"Whooper starts no other review of the PR until bin/whooper-skip {run.name} --undo unmarks it"
        result = self.skip(run.name)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, f"marked {name} as skipped, without submitting it; {then}\n", ""))
        self.assertEqual(self.skip(run.name, "--undo").stdout, f"unmarked {name} as skipped\n")
        for args in [[], ["--undo"], [run.name, run.name], [run.name, "--undo", "--undo"], ["--item"],
                     ["--item", run.name, run.name], [run.name, "--item", run.name]]:
            with self.subTest(args=args):
                result = self.skip(*args)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", "usage: whooper-skip [--item] <run-id> [--undo]\n"))
        self.assertEqual(list(run.glob("skipped*")), [])

    def remove(self, *args):
        """Runs the manifest's remove action, to which Prompting Time adds the run ID and, to undo, --undo."""
        [action] = [a for a in json.loads((ROOT / "prompting-time-plugin.json").read_text())["actions"]
                    if a["name"] == "remove"]
        self.assertFalse(action["confirm"])
        command = action["command"]
        return subprocess.run([str(ROOT / command[0]), *command[1:], *args], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def test_removing_a_review_takes_it_out_of_the_lists_until_undone(self):
        run = self.make_run(timedelta(hours=1))
        self.finish(run, findings=[finding()])
        files = sorted(run.iterdir())
        name = f"{run.name} (example-org/example-repo#12)"
        removed = self.remove(run.name)
        self.assertEqual((removed.returncode, removed.stdout, removed.stderr), (
            0, f"marked {name} as removed; its files stay, and bin/whooper-remove {run.name} --undo unmarks it\n", ""))
        # whooper-status still lists it, marked removed, and it needs no one.
        self.assertEqual(self.reviews(), [self.review(run, findings=1, removed_by_user=True, needs_you=False)])
        self.assertEqual(self.shown(run)["removed_by_user"], True)
        # Its files stay, beside the empty mark.
        self.assertEqual(sorted(run.iterdir()), sorted([*files, run / "removed-by-user"]))
        self.assertEqual((run / "removed-by-user").read_text(), "")
        self.assertEqual(self.remove(run.name).stdout, f"{name} is already marked as removed\n")
        # Prompting Time adds --undo after the run ID.
        undone = self.remove(run.name, "--undo")
        self.assertEqual((undone.returncode, undone.stdout), (0, f"unmarked {name} as removed\n"))
        self.assertEqual(self.reviews(), [self.review(run, findings=1)])
        self.assertEqual(self.remove("--undo", run.name).stdout, f"{name} is not marked as removed\n")
        # A row's menu in Prompting Time passes the run ID after --item, as for each of status.item_actions.
        self.assertEqual(self.remove("--item", run.name).stdout, f"marked {name} as removed; its files stay, and"
                                                                 f" bin/whooper-remove {run.name} --undo unmarks it\n")
        self.assertEqual(self.remove("--undo", "--item", run.name).stdout, f"unmarked {name} as removed\n")
        # A review whooper-review stopped before it wrote run.json, which names the PR, can be removed too.
        stopped = self.make_run(timedelta(hours=2), pr=13, run_json=False)
        self.write(stopped, "failure.txt", "the review stopped at an unexpected error\n", 1)
        self.assertEqual(self.remove(stopped.name).stdout, f"marked {stopped.name} as removed; its files stay, and"
                                                           f" bin/whooper-remove {stopped.name} --undo unmarks it\n")
        self.assertEqual({r["run_id"]: (r["removed_by_user"], r["needs_you"]) for r in self.reviews()},
                         {run.name: (False, True), stopped.name: (True, False)})

    def test_remove_refuses_an_unknown_run_and_a_review_still_running(self):
        run = self.make_run(timedelta(minutes=5))
        self.fetched(run)
        for run_id in ["20310101T000000Z-1", "..", "../whooper", f"../runs/{run.name}"]:
            with self.subTest(run_id):
                result = self.remove(run_id)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", f"whooper-remove: no run {run_id} in {self.state / 'runs'}\n"))
        # A live whooper-review holds the PR's lock for the run.
        self.hold(run)
        for args in [[run.name], [run.name, "--undo"]]:
            with self.subTest(args=args):
                result = self.remove(*args)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", "whooper-remove: the review is still running; remove it once it ends\n"))
        for args in [[], ["--undo"], [run.name, run.name], [run.name, "--undo", "--undo"], ["--item"],
                     ["--item", run.name, run.name], [run.name, "--item", run.name]]:
            with self.subTest(args=args):
                result = self.remove(*args)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", "usage: whooper-remove [--item] <run-id> [--undo]\n"))
        self.assertEqual(list(run.glob("removed*")), [])

    def test_a_removed_review_that_waits_no_longer_counts_as_waiting(self):
        failed = self.stopped(timedelta(hours=2), 12, "docker_unavailable")
        waits = [self.waiting(timedelta(hours=2) - timedelta(minutes=m), pr) for m, pr in [(3, 13), (4, 14)]]
        blocked = {**CAUSES["docker_unavailable"], "since": self.local(self.started(failed) + timedelta(minutes=2))}
        self.assertEqual(self.status()["blocked"], {**blocked, "waiting": 2})
        # The next round does not review a removed review, so it no longer waits, though Docker is still down.
        self.assertEqual(self.remove(waits[0].name).returncode, 0)
        self.assertEqual(self.status()["blocked"], {**blocked, "waiting": 1})
        # Nor a skipped one.
        self.assertEqual(self.skip(waits[1].name).returncode, 0)
        self.assertEqual(self.status()["blocked"], {**blocked, "waiting": 0})

    def test_show_refuses_an_unknown_run(self):
        run = self.make_run(timedelta(hours=1))
        self.finish(run)
        # A run ID names a folder right under runs/, so a path reads nothing outside it.
        for run_id in ["20310101T000000Z-1", "..", "../whooper", f"../runs/{run.name}"]:
            with self.subTest(run_id):
                result = self.show(run_id, "--json")
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", f"whooper-show: no run {run_id} in {self.state / 'runs'}\n"))
        for args in [[], ["--json"], [run.name], [run.name, run.name, "--json"]]:
            with self.subTest(args=args):
                result = self.show(*args)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", "usage: whooper-show <run-id> --json\n"))


class StatusFormat2Test(unittest.TestCase):
    """status_v2, the fields of Prompting Time's status format 2 in whooper-status's JSON, on reviews
    as read_reviews lists them, at a fixed time in Los Angeles."""

    LA = ZoneInfo("America/Los_Angeles")
    NOW = datetime(2031, 7, 30, 15, 0, tzinfo=LA)

    def review(self, minutes_ago, pr=12, **fields):
        """A review of example-org/example-repo#<pr> that started minutes_ago before NOW and ended a minute
        later with a look_first verdict, and waits for the user."""
        started = (self.NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        ended = (self.NOW - timedelta(minutes=minutes_ago - 1)).isoformat(timespec="seconds")
        return {"run_id": f"run-{pr}-{minutes_ago}", "repo": "example-org/example-repo", "pr": pr, "title": "T",
                "author": None, "url": None, "started_at": started, "ended_at": ended, "outcome": "reviewed",
                "verdict": "look_first", "skipped": False, "findings": 0, "safe_to_approve": False, "submitted": None,
                "needs_you": True, "cause": None, "head_sha": "h1", "pr_head_sha": None, "skipped_by_user": False,
                "removed_by_user": False, **fields}

    def status(self, reviews, **fields):
        return status_v2({"blocked": None, "last_round": None, "round": {"running": False, "reviewing": None},
                          "paused": False, "next_round": None, "reviews": reviews, **fields}, self.NOW)

    def test_each_review_goes_in_the_group_prompting_time_placed_it_in_before_format_2(self):
        failed = {"outcome": "failed", "verdict": None}
        cases = [
            ({}, "look", "attention"),
            ({"safe_to_approve": True, "verdict": "approve_once_ci_passes"}, "ready", "success"),
            # A review the user skipped is done, even one that failed.
            ({"skipped_by_user": True, "needs_you": False}, "done", "idle"),
            ({**failed, "skipped_by_user": True, "needs_you": False}, "done", "idle"),
            ({**failed, "cause": CAUSES["time_limit"]}, "failed", "error"),
            # A failure at a cause that stops every review waits with the others it stopped, still red.
            ({**failed, "cause": CAUSES["docker_unavailable"]}, "waiting", "error"),
            ({"outcome": "waiting", "verdict": None, "needs_you": False, "cause": CAUSES["docker_unavailable"]},
             "waiting", "idle"),
            ({"outcome": "waiting", "verdict": None, "needs_you": False, "cause": CAUSES["unknown"]}, "reviewing", "idle"),
            ({"outcome": "running", "verdict": None, "needs_you": False, "ended_at": None}, "reviewing", "running"),
            # bin/whooper-skip stopped the review, which ends as an interrupted failure.
            ({"outcome": "running", "verdict": None, "needs_you": False, "ended_at": None, "skipped_by_user": True},
             "done", "idle"),
        ]
        for fields, group, dot in cases:
            with self.subTest(fields):
                [item] = self.status([self.review(5, **fields)])["items"]
                self.assertEqual((item["group"], item["dot"]), (group, dot))

    def test_a_review_that_runs_offers_only_skip_which_stops_it(self):
        running = {"outcome": "running", "verdict": None, "needs_you": False, "ended_at": None}
        cases = [
            (running, ["skip"]),
            ({}, ["retry", "skip", "remove"]),
            ({"skipped_by_user": True, "needs_you": False}, ["retry", "remove"]),
            ({**running, "skipped_by_user": True}, []),
        ]
        for fields, actions in cases:
            with self.subTest(fields):
                [item] = self.status([self.review(5, **fields)])["items"]
                self.assertEqual(item["actions"], actions)
        # A review the user removed is in no group.
        self.assertEqual(self.status([self.review(5, removed_by_user=True, needs_you=False)])["items"], [])

    def test_it_lists_the_current_items_before_the_finished_ones_and_at_most_50(self):
        done = self.review(1, pr=1, skipped_by_user=True, needs_you=False)
        current = [self.review(10 + i, pr=100 + i) for i in range(50)]
        status = self.status([done, *current])
        self.assertEqual([item["id"] for item in status["items"]], [r["run_id"] for r in current])
        self.assertTrue(status["truncated"])
        status = self.status([done, *current[:49]])
        self.assertEqual(status["items"][-1]["id"], done["run_id"])
        self.assertFalse(status["truncated"])

    def test_blockers_name_the_blocked_cause_then_each_other_one_a_newest_review_waits_at(self):
        blocked = {**CAUSES["gh_signed_out"], "since": self.NOW.isoformat(), "waiting": 0}
        reviews = [self.review(5, pr=1, outcome="failed", cause=CAUSES["setup_failed"]),
                   self.review(6, pr=2, outcome="waiting", needs_you=False, cause=CAUSES["setup_failed"]),
                   # A failure of the PR's own stops no other review.
                   self.review(7, pr=3, outcome="failed", cause=CAUSES["time_limit"])]
        self.assertEqual([(b["id"], b["title"], b["route"]) for b in self.status(reviews, blocked=blocked)["blockers"]], [
            ("gh_signed_out", "gh isn't signed in to GitHub", "/causes/gh_signed_out"),
            ("setup_failed", "The repo's setup script failed", "/causes/setup_failed"),
        ])

    def test_the_agent_row_says_what_whooper_does_as_prompting_time_worded_it_before_format_2(self):
        today, monday = "2031-07-30T15:45:00-07:00", "2031-07-28T10:00:00-07:00"
        self.assertEqual((short_time(today, self.NOW), short_time(monday, self.NOW)), ("3:45 PM", "Mon 10:00 AM"))
        self.assertEqual((short_date(today, self.NOW), short_date(monday, self.NOW),
                          short_date("2030-11-24T09:05:00-08:00", self.NOW)), ("3:45 PM", "Jul 28", "Nov 24, 2030"))
        failed = {"started_at": monday, "ended_at": "2031-07-30T14:50:00-07:00", "result": "poll_failed",
                  "selected": 0, "reviews": []}
        for fields, agent in [
            ({}, {"words": "Running", "dot": "idle"}),
            ({"next_round": today}, {"words": "Running · next poll 3:45 PM", "dot": "idle"}),
            ({"last_round": failed, "next_round": today},
             {"words": "Poll failed 2:50 PM · next try 3:45 PM", "dot": "error"}),
            ({"paused": True, "next_round": today}, {"words": "Paused", "dot": "idle"}),
            ({"round": {"running": True, "reviewing": None}}, {"words": "Polling now", "dot": "running"}),
            ({"round": {"running": True, "reviewing": "example-org/example-repo#12"}, "paused": True},
             {"words": "Reviewing example-org/example-repo#12", "dot": "running"}),
            ({"blocked": {**CAUSES["docker_unavailable"], "since": today, "waiting": 2},
              "round": {"running": True, "reviewing": None}},
             {"words": "Can't review · Docker isn't running", "dot": "error"}),
        ]:
            with self.subTest(fields):
                self.assertEqual(self.status([], **fields)["agent"], agent)


class RetryTest(unittest.TestCase):
    """bin/whooper-retry and bin/whooper-remove, run as the manifest's retry and remove actions, and
    bin/whooper-select after them, on review runs of example-org/example-repo#12 and #13 in a throwaway
    state folder. gh is a fake that reports PR 12's head from the file head and fails any other call,
    and slackdump, claude, and devin fail, so no test can reach GitHub, Slack, or a model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.fakes = tmp / "bin"
        for name in ["slackdump", "claude", "devin"]:
            fake(self.fakes / name, "exit 1")
        fake(self.fakes / "gh", "\n".join([
            'case "$*" in',
            '  "api repos/example-org/example-repo/pulls/12 --jq .head.sha") cat "${0%/*}/head" ;;',
            '  *) echo "fake gh: unexpected call: gh $*" >&2; exit 1 ;;',
            "esac",
        ]))
        (self.fakes / "head").write_text("h1\n")
        self.env = {**os.environ, "PATH": f"{self.fakes}:{os.environ['PATH']}", "XDG_STATE_HOME": str(tmp),
                    "XDG_CACHE_HOME": str(tmp / "cache")}
        self.state = tmp / "whooper"
        # The review of the PR's head h1 failed at the repo's setup script, its third failure at h1.
        self.work = {"kind": "first_review", "repo": "example-org/example-repo", "pr": 12, "head_sha": "h1",
                     "base_ref": "master", "source": "github_request", "slack": None,
                     "reason": "A GitHub review request names sam-example, who has not reviewed this PR."}
        self.run = self.state / "runs" / "20310727T010246Z-12"
        self.run.mkdir(parents=True)
        self.write_run(self.work)
        (self.run / "failure.txt").write_text("/config/setup.sh failed\n")
        self.failed = {"status": "failed", "failures": 3, "started_at": "2031-07-27T01:02:46+00:00"}
        self.record("h1").parent.mkdir(parents=True)
        self.record("h1").write_text(json.dumps(self.failed))
        # The newest poll lists the PR at h1, since its review request stands.
        self.poll([self.work])
        # A tagged Slack ask for PR 13 at h7, which a poll reads once, and a later post that asks again.
        slack = {"ts": "1943194837.000100", "tagged_user": True,
                 "permalink": "https://example.slack.com/archives/C0000000001/p1943194837000100"}
        self.ask = {**self.work, "pr": 13, "head_sha": "h7", "source": "slack_ask", "slack": slack,
                    "reason": "Alex asked for a review."}
        again = "1943198437.000200"
        self.asked_again = {**self.ask, "slack": {
            **slack, "ts": again, "permalink": f"https://example.slack.com/archives/C0000000001/p{again.replace('.', '')}"}}

    def write_run(self, work):
        """Writes the run's run.json: the work item the review got, with the run's own fields."""
        (self.run / "run.json").write_text(json.dumps({**work, "run_id": self.run.name, "merge_base": "m1",
                                                       "as_of": None, "dry_run": True, "agent": "claude", "skills": {}}))

    def record(self, head):
        """The path of the record of first reviews of the PR at head."""
        return self.state / "prs" / "example-org-example-repo-12" / f"{head}.first_review.json"

    def poll(self, items):
        """Makes items the newest poll's output."""
        (self.state / "poll").mkdir(exist_ok=True)
        (self.state / "poll" / "20310727T020000Z.json").write_text(json.dumps({"items": items}))

    def retry(self, *args):
        """Runs the manifest's retry action, to which Prompting Time adds the run ID, with args."""
        [action] = [a for a in json.loads((ROOT / "prompting-time-plugin.json").read_text())["actions"]
                    if a["name"] == "retry"]
        command = action["command"]
        return subprocess.run([str(ROOT / command[0]), *command[1:], *args], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def select(self):
        """The work items bin/whooper-select prints, as the next round's selection."""
        result = subprocess.run([str(ROOT / "bin" / "whooper-select")], env=self.env, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def remove(self, *args):
        """Runs the manifest's remove action, to which Prompting Time adds the run ID and, to undo, --undo."""
        [action] = [a for a in json.loads((ROOT / "prompting-time-plugin.json").read_text())["actions"]
                    if a["name"] == "remove"]
        command = action["command"]
        return subprocess.run([str(ROOT / command[0]), *command[1:], *args], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def wait(self, item):
        """Leaves item waiting for the next round, as whooper-round does after an earlier review in its round
        could not reach Docker, and returns the run directory that waits."""
        return wait_review(self.state, item, CAUSES["docker_unavailable"], "claude", datetime.now(timezone.utc))

    def test_a_review_that_failed_three_times_is_reviewed_again_in_the_next_round(self):
        # Three failures at h1 stop the rounds from reviewing h1 again on their own.
        self.assertEqual(self.select(), [])
        result = self.retry(self.run.name)
        self.assertEqual((result.returncode, result.stdout),
                         (0, "queued another review of example-org/example-repo#12 at h1 for the next round;"
                             " bin/whooper-round runs that round now\n"), result.stderr)
        self.assertEqual(json.loads(self.record("h1").read_text()), {**self.failed, "status": "retry"})
        # The poll lists the PR, so the round reviews the poll's item at h1, once, and the recheck list's
        # item for it is done.
        self.assertEqual(self.select(), [self.work])
        self.assertFalse((self.state / "rechecks" / "example-org-example-repo-12.json").exists())
        self.assertEqual(self.select(), [self.work])

    def test_a_rows_menu_retries_a_review_with_item(self):
        # Prompting Time runs each of status.item_actions as the action's command, --item, and the run ID.
        result = self.retry("--item", self.run.name)
        self.assertEqual((result.returncode, result.stdout),
                         (0, "queued another review of example-org/example-repo#12 at h1 for the next round;"
                             " bin/whooper-round runs that round now\n"), result.stderr)
        self.assertEqual(json.loads(self.record("h1").read_text()), {**self.failed, "status": "retry"})

    def test_a_retry_reviews_the_head_the_pr_has_now_whatever_brought_it_in(self):
        # A review of a PR only a Slack ask brought in, which a poll reads once, finished at h1.
        slack = {"ts": "1943194837.000100", "tagged_user": True,
                 "permalink": "https://example.slack.com/archives/C0000000001/p1943194837000100"}
        work = {**self.work, "source": "slack_ask", "slack": slack, "reason": "Alex asked for a review."}
        self.write_run(work)
        (self.run / "failure.txt").unlink()
        done = {"status": "done", "failures": 0, "started_at": "2031-07-27T01:02:46+00:00",
                "approval": {"verdict": "look_first", "reason": "Ran the orders tests."}}
        self.record("h1").write_text(json.dumps(done))
        self.poll([])
        self.assertEqual(self.select(), [])
        # The user wants the review done again.
        self.assertEqual(self.retry(self.run.name).returncode, 0)
        self.assertEqual(self.select(), [work])
        # Then the PR moves to h2, and the user asks again: the list now holds the PR at h2.
        (self.fakes / "head").write_text("h2\n")
        result = self.retry(self.run.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("example-org/example-repo#12 at h2", result.stdout)
        self.assertEqual(self.select(), [{**work, "head_sha": "h2"}])
        self.assertEqual(json.loads(self.record("h2").read_text())["status"], "retry")

    def test_refuses_an_unknown_run_a_review_still_running_and_a_run_without_run_json(self):
        runs = self.state / "runs"
        # A run ID names a folder right under runs/, as in whooper-show.
        for run_id in ["20310101T000000Z-1", "../whooper", f"../runs/{self.run.name}"]:
            with self.subTest(run_id):
                result = self.retry(run_id)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", f"whooper-retry: no run {run_id} in {runs}\n"))
        # A live whooper-review holds the PR's lock for the run.
        lock = self.state / "locks" / "example-org-example-repo-12"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{review_stand_in(self, Path(self.tmp.name) / 'review')}\n")
        (lock / "run_id").write_text(f"{self.run.name}\n")
        result = self.retry(self.run.name)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", "whooper-retry: the review is still running; retry it once it ends\n"))
        # whooper-review stopped this review before it wrote run.json, which alone names the PR's repo.
        stopped = runs / "20310727T000000Z-13"
        stopped.mkdir()
        (stopped / "failure.txt").write_text("the review stopped at an unexpected error\n")
        result = self.retry(stopped.name)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", f"whooper-retry: cannot read {stopped}/run.json, which names the PR to review again\n"))
        for args in [[], [self.run.name, stopped.name], ["--item"], ["--item", "--item"],
                     ["--item", self.run.name, stopped.name], [self.run.name, "--item"]]:
            with self.subTest(args=args):
                result = self.retry(*args)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", "usage: whooper-retry [--item] <run-id>\n"))
        # Nothing was queued.
        self.assertFalse((self.state / "rechecks").exists())
        self.assertEqual(json.loads(self.record("h1").read_text()), self.failed)

    def test_a_removed_review_that_failed_or_waits_is_not_reviewed_again_until_a_new_request_for_its_pr(self):
        # PR 12's review at h1 failed once, and its review request stands, so every poll lists the PR, and
        # each round would review it again until it failed 3 times. PR 13's review waits for the next round
        # on the recheck list.
        self.record("h1").write_text(json.dumps({**self.failed, "failures": 1}))
        waits = self.wait(self.ask)
        self.assertEqual(self.select(), [self.work, self.ask])
        for run in [self.run, waits]:
            self.assertEqual(self.remove(run.name).returncode, 0)
        result = subprocess.run([str(ROOT / "bin" / "whooper-select")], env=self.env, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (
            0, "", "whooper-select: dropped example-org/example-repo#12 first_review: removed by the user\n"
                   "whooper-select: dropped example-org/example-repo#13 first_review: removed by the user\n"))
        # PR 13 left the recheck list, and the files of both runs stay.
        self.assertFalse((self.state / "rechecks" / "example-org-example-repo-13.json").exists())
        self.assertTrue((self.run / "failure.txt").exists() and (waits / "waiting.json").exists())
        self.assertEqual(self.select(), [])
        # A new Slack post asks for PR 13 again, and PR 12 gets new commits: the next round reviews both.
        moved = {**self.work, "head_sha": "h2"}
        self.poll([moved, self.asked_again])
        self.assertEqual(self.select(), [moved, self.asked_again])

    def test_a_mark_written_while_a_round_selects_stays_on_the_recheck_list(self):
        # The recheck list holds PR 12 at h1, and the poll lists the PR, so whooper-select takes it off the
        # list. Meanwhile whooper-submit finds that the PR moved to h2 and marks it, between the selection's
        # read of the list and its delete. The mark stays, so the next round reviews h2 whatever brought the
        # PR in.
        listed = self.state / "rechecks" / "example-org-example-repo-12.json"
        listed.parent.mkdir()
        listed.write_text(json.dumps(self.work))

        def moved():
            mark_new_commits(self.state, self.work, "h2", datetime.now(timezone.utc))

        with at_first_call("select_work", moved), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main("select", str(self.state))
        self.assertEqual(json.loads(listed.read_text()), {**self.work, "head_sha": "h2"})

    def test_retrying_a_removed_review_unmarks_it(self):
        self.assertEqual(self.remove(self.run.name).returncode, 0)
        result = self.retry(self.run.name)
        self.assertEqual((result.returncode, result.stdout),
                         (0, f"unmarked {self.run.name} as removed, and queued another review of"
                             " example-org/example-repo#12 at h1 for the next round; bin/whooper-round runs that"
                             " round now\n"), result.stderr)
        self.assertFalse((self.run / "removed-by-user").exists())
        self.assertEqual(self.select(), [self.work])

    def test_undoing_the_removal_of_a_review_that_waits_puts_it_back_on_the_recheck_list(self):
        self.poll([])
        waits = self.wait(self.ask)
        listed = self.state / "rechecks" / "example-org-example-repo-13.json"
        # Undone before a round's selection, the review is still on the list.
        self.assertEqual(self.remove(waits.name).returncode, 0)
        self.assertEqual(self.remove(waits.name, "--undo").stdout,
                         f"unmarked {waits.name} (example-org/example-repo#13) as removed\n")
        self.assertEqual(json.loads(listed.read_text()), self.ask)
        # Undone after a round's selection took it off the list, the review goes back on it.
        self.assertEqual(self.remove(waits.name).returncode, 0)
        self.assertEqual(self.select(), [])
        self.assertFalse(listed.exists())
        self.assertEqual(self.remove(waits.name, "--undo").stdout,
                         f"unmarked {waits.name} (example-org/example-repo#13) as removed, and put it back on the"
                         " recheck list for the next round\n")
        self.assertEqual(self.select(), [self.ask])

    def test_a_new_request_for_a_removed_review_that_waits_waits_in_a_run_of_its_own(self):
        waits = self.wait(self.ask)
        self.assertEqual(self.remove(waits.name).returncode, 0)
        # A new Slack post asks for PR 13 again, and its round stops at Docker as well.
        self.poll([self.asked_again])
        self.assertEqual(self.select(), [self.asked_again])
        again = self.wait(self.asked_again)
        self.assertNotEqual(again, waits)
        self.assertEqual(json.loads((again / "run.json").read_text())["slack"], self.asked_again["slack"])
        self.assertTrue((waits / "removed-by-user").exists())
        # The new run is not removed, so a round that waits again reuses it.
        self.assertEqual(self.wait(self.asked_again), again)


class SettingsTest(unittest.TestCase):
    """whooper.py settings, which bin/whooper-settings runs, on a config file in a throwaway folder."""

    def use_config(self, text):
        """Points WHOOPER_CONFIG at a new file holding text for the rest of the test."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "config.toml"
        path.write_text(text)
        self.enterContext(mock.patch.dict(os.environ, WHOOPER_CONFIG=str(path)))
        return path

    def settings(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            main("settings", *args)
        return out.getvalue()

    def test_get_fills_in_whoopers_defaults(self):
        # The test config has no [schedule], [poll], or [ask].
        self.assertEqual(json.loads(self.settings("get")), {
            "schedule": {"interval_minutes": 15, "start": "10:00", "end": "18:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
            "poll": {"timeout_minutes": 10},
            "jobs": {"review": "claude", "poll": "claude", "summary": "claude"},
            "agents": {"claude": {"review_model": "review-model", "poll_model": "poll-model", "budget_usd": 15},
                       "devin": {"review_model": "devin-review-model", "poll_model": "devin-poll-model"}},
            "review": {"timeout_minutes": 45, "skills": ["review-skill", "language-skill"]},
            "ask": {"timeout_minutes": 10},
        })

    def test_each_job_offers_the_agents_with_a_launcher_for_it(self):
        jobs = json.loads((ROOT / "schemas" / "settings.json").read_text())["properties"]["jobs"]["properties"]
        for job, schema in jobs.items():
            with self.subTest(job):
                self.assertEqual(sorted(schema["enum"]), sorted(p.parent.name for p in (ROOT / "agents").glob(f"*/{job}")))

    def test_set_refuses_a_save_that_leaves_a_job_without_an_agent(self):
        path = self.use_config(EXAMPLE.replace('summary = "devin"\n', ""))
        before = path.read_text()
        with self.assertRaises(SystemExit) as stop:
            self.settings("set", json.dumps({"schedule": {"interval_minutes": 30}}))
        self.assertEqual(str(stop.exception), "whooper: [jobs] names no agent for summary; each of review, poll, and"
                                              f" summary names the agent it runs on; see {path}")
        self.assertEqual(path.read_text(), before)
        # A save that gives the job an agent goes through.
        self.settings("set", json.dumps({"schedule": {"interval_minutes": 30}, "jobs": {"summary": "claude"}}))
        self.assertEqual(json.loads(self.settings("get"))["jobs"], {"review": "devin", "poll": "devin", "summary": "claude"})

    def test_set_refuses_a_save_that_leaves_a_time_limit_whooper_refuses_to_start_on(self):
        # whooper.py env, which each script starts with, stops on a time limit edited by hand into config.toml, so a
        # save of another setting that would keep it is refused with env's message.
        # (case, the config's line, the line edited by hand, why env and the save refuse it)
        review = "[review] timeout_minutes takes a whole number of minutes, at least 1"
        for name, old, new, why in [
            ("a review limit with part of a minute", "timeout_minutes = 45 ", "timeout_minutes = 1.5 ", review),
            ("a review limit of 0 minutes", "timeout_minutes = 45 ", "timeout_minutes = 0 ", review),
            ("a review limit in quotes", "timeout_minutes = 45 ", 'timeout_minutes = "45" ', review),
            ("a review limit of true", "timeout_minutes = 45 ", "timeout_minutes = true ", review),
            ("a poll limit under 5 minutes", "timeout_minutes = 10 ", "timeout_minutes = 3 ",
             "[poll] timeout_minutes takes a whole number of minutes, at least 5"),
            ("an ask limit of 0 minutes", "timeout_minutes = 10               # whooper-ask",
             "timeout_minutes = 0               # whooper-ask", "[ask] timeout_minutes takes a number of minutes above 0"),
        ]:
            with self.subTest(name):
                path = self.use_config(EXAMPLE.replace(old, new, 1))
                with self.assertRaises(SystemExit) as save:
                    self.settings("set", json.dumps({"schedule": {"interval_minutes": 30}}))
                with self.assertRaises(SystemExit) as env, redirect_stdout(io.StringIO()):
                    main("env")
                self.assertEqual(str(save.exception), f"whooper: {why}; see {path}")
                self.assertEqual(str(env.exception), str(save.exception))
                self.assertEqual(path.read_text(), EXAMPLE.replace(old, new, 1))
                self.assertEqual([p.name for p in path.parent.iterdir()], ["config.toml"])

    def test_get_reads_a_review_limit_whooper_refuses_and_a_save_that_fixes_it_goes_through(self):
        # A settings form reads the limit to show it, and saves the fix.
        for bad, read in [("1.5", 1.5), ("0", 0), ('"45"', "45")]:
            with self.subTest(bad):
                path = self.use_config(EXAMPLE.replace("timeout_minutes = 45 ", f"timeout_minutes = {bad} ", 1))
                self.assertEqual(json.loads(self.settings("get"))["review"]["timeout_minutes"], read)
                self.settings("set", json.dumps({"review": {"timeout_minutes": 30}}))
                self.assertEqual(path.read_text(), EXAMPLE.replace("timeout_minutes = 45 ", "timeout_minutes = 30 ", 1))

    def test_a_whole_number_typed_as_a_float_reads_as_that_number_and_a_save_of_another_setting_keeps_it(self):
        # TOML reads interval_minutes = 15.0 or timeout_minutes = 45.0, edited by hand, as a float, which a settings
        # form shows as 15 or 45, so typing 15 or 45 there sends nothing. The checks take it as that whole number, so
        # get prints it as one, and a save of another setting goes through and keeps the line as it is.
        edited = (EXAMPLE.replace("interval_minutes = 15\n", "interval_minutes = 15.0\n", 1)
                  .replace("timeout_minutes = 45 ", "timeout_minutes = 45.0 ", 1)
                  .replace("timeout_minutes = 10 ", "timeout_minutes = 10.0 ", 1))
        path = self.use_config(edited)
        got = json.loads(self.settings("get"))
        numbers = [got["schedule"]["interval_minutes"], got["poll"]["timeout_minutes"], got["review"]["timeout_minutes"]]
        self.assertEqual([(number, type(number)) for number in numbers], [(15, int), (10, int), (45, int)])
        self.settings("set", json.dumps({"jobs": {"review": "claude"}}))
        self.assertEqual(path.read_text(), edited.replace('[jobs]\nreview = "devin"', '[jobs]\nreview = "claude"', 1))

    def test_set_rewrites_a_line_whose_value_equals_the_saved_one_in_python_but_has_another_type(self):
        # Python's == calls true equal to 1, and read_schedule refuses interval_minutes = true and the config check a
        # limit of true, so a save of 1 over a line edited by hand to true rewrites the line instead of keeping it and
        # refusing.
        # (the line, the line edited by hand, the values set is given, the line it saves)
        for line, edited, values, saved in [
            ("interval_minutes = 15\n", "interval_minutes = true\n", {"schedule": {"interval_minutes": 1}},
             "interval_minutes = 1\n"),
            ("timeout_minutes = 45 ", "timeout_minutes = true ", {"review": {"timeout_minutes": 1}},
             "timeout_minutes = 1 "),
        ]:
            with self.subTest(edited):
                path = self.use_config(EXAMPLE.replace(line, edited, 1))
                self.settings("set", json.dumps(values))
                self.assertEqual(path.read_text(), EXAMPLE.replace(line, saved, 1))

    def test_a_whole_number_sent_as_a_float_saves_as_that_number(self):
        # JSON Schema counts 45.0 as an integer, so a save of {"review": {"timeout_minutes": 45.0}}, as Python's
        # json.dumps writes 45, goes through and writes 45, which the checks read as they read 45.
        for values, line, saved in [
            ({"review": {"timeout_minutes": 45.0}}, "timeout_minutes = 30 ", "timeout_minutes = 45 "),
            ({"schedule": {"interval_minutes": 20.0}}, "interval_minutes = 15\n", "interval_minutes = 20\n"),
            ({"poll": {"timeout_minutes": 5.0}}, "timeout_minutes = 10 ", "timeout_minutes = 5 "),
        ]:
            with self.subTest(values):
                edited = EXAMPLE.replace("timeout_minutes = 45 ", "timeout_minutes = 30 ", 1)
                path = self.use_config(edited)
                self.settings("set", json.dumps(values))
                self.assertEqual(path.read_text(), edited.replace(line, saved, 1))

    def test_a_save_takes_the_ask_limits_whooper_ask_starts_on_and_refuses_the_rest(self):
        # whooper-ask's watchdog sleeps the limit times 60 in Python, so any number of minutes above 0 works, such as
        # 0.05 for 3 seconds, and the schema and the config check both take exactly those.
        # (the value, in JSON and in TOML alike, whether whooper-ask starts on it)
        line = "timeout_minutes = 10               # whooper-ask"
        for text, works in [("2.5", True), ("0.05", True), ("15", True), ("0", False), ("-1", False), ("true", False),
                            ('"7"', False)]:
            with self.subTest(text):
                path = self.use_config(EXAMPLE)
                if works:
                    self.settings("set", f'{{"ask": {{"timeout_minutes": {text}}}}}')
                    self.assertEqual(path.read_text(), EXAMPLE.replace(line, line.replace("10 ", f"{text} "), 1))
                    with redirect_stdout(io.StringIO()) as env:
                        main("env")
                    self.assertIn(f"WHOOPER_ASK_TIMEOUT_MINUTES={text}\n", env.getvalue())
                else:
                    with self.assertRaises(SystemExit):
                        self.settings("set", f'{{"ask": {{"timeout_minutes": {text}}}}}')
                    self.assertEqual(path.read_text(), EXAMPLE)
                    self.use_config(EXAMPLE.replace(line, line.replace("10 ", f"{text} "), 1))
                    with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
                        main("env")

    def test_set_changes_only_the_given_line_and_keeps_every_comment(self):
        for values, old, new in [
            ({"schedule": {"interval_minutes": 30}}, "interval_minutes = 15\n", "interval_minutes = 30\n"),
            # The changed line keeps its own comment too.
            ({"poll": {"timeout_minutes": 5}}, "timeout_minutes = 10 ", "timeout_minutes = 5 "),
            ({"ask": {"timeout_minutes": 15}}, "timeout_minutes = 10               # whooper-ask",
             "timeout_minutes = 15               # whooper-ask"),
        ]:
            with self.subTest(values):
                path = self.use_config(EXAMPLE)
                self.settings("set", json.dumps(values))
                # [poll] and [ask] each have the line timeout_minutes = 10, and a change to one keeps the other.
                self.assertEqual(path.read_text(), EXAMPLE.replace(old, new, 1))

    def test_a_claude_budget_is_optional_and_can_be_set(self):
        # With no budget_usd, as in the example config, a Claude review has no spending limit.
        self.use_config(EXAMPLE)
        self.assertNotIn("budget_usd", json.loads(self.settings("get"))["agents"]["claude"])
        self.settings("set", json.dumps({"agents": {"claude": {"budget_usd": 7.5}}}))
        self.assertEqual(json.loads(self.settings("get"))["agents"]["claude"]["budget_usd"], 7.5)

    def test_two_saves_at_once_both_change_the_file(self):
        # Prompting Time's settings form saves the schedule while a terminal saves the ask limit: the
        # terminal's save comes between the form's read of the config and its write.
        path = self.use_config(EXAMPLE)
        with at_first_call("write_file", lambda: write_settings({"ask": {"timeout_minutes": 15}})):
            write_settings({"schedule": {"interval_minutes": 30}})
        saved = EXAMPLE.replace("interval_minutes = 15\n", "interval_minutes = 30\n", 1)
        saved = saved.replace("timeout_minutes = 10               # whooper-ask",
                              "timeout_minutes = 15               # whooper-ask", 1)
        self.assertEqual(path.read_text(), saved)

    def test_set_adds_a_missing_table(self):
        path = self.use_config(CONFIG)
        self.settings("set", '{"schedule": {"interval_minutes": 30}}')
        self.assertEqual(path.read_text(), CONFIG + "\n[schedule]\ninterval_minutes = 30\n")
        self.assertEqual(json.loads(self.settings("get"))["schedule"],
                         {"interval_minutes": 30, "start": "10:00", "end": "18:00", "days": ["mon", "tue", "wed", "thu", "fri"]})

    def test_saving_every_value_get_printed_changes_nothing(self):
        # A form may send back every field, and rewriting an unchanged value could reformat its line.
        path = self.use_config(EXAMPLE.replace('days = ["mon", "tue", "wed", "thu", "fri"]',
                                               'days = [\n  "mon",  # standup\n  "tue", "wed", "thu", "fri",\n]'))
        before = path.read_bytes()
        values = self.settings("get")
        check_schema(json.loads(values), json.loads((ROOT / "schemas" / "settings.json").read_text()))
        self.settings("set", values)
        self.assertEqual(path.read_bytes(), before)

    def test_bad_values_are_rejected_and_leave_the_file_unchanged(self):
        # (case, the values set is given, part of the reason it prints)
        for name, text, why in [
            ("no wait between rounds", '{"schedule": {"interval_minutes": 0}}', "interval_minutes is below 1"),
            ("an interval in quotes", '{"schedule": {"interval_minutes": "30"}}', "interval_minutes is not of type integer"),
            # A poll takes 2 to 3 minutes, so a shorter limit would stop every poll.
            ("a poll limit under 5 minutes", '{"poll": {"timeout_minutes": 4}}', "timeout_minutes is below 5"),
            ("an ask limit of 0 minutes", '{"ask": {"timeout_minutes": 0}}', "settings.ask.timeout_minutes is not above 0"),
            ("a review limit with part of a minute", '{"review": {"timeout_minutes": 1.5}}',
             "settings.review.timeout_minutes is not of type integer"),
            ("a budget of nothing", '{"agents": {"claude": {"budget_usd": 0}}}', "budget_usd is below 1"),
            ("an unknown key", '{"schedule": {"interval": 30}}', "unexpected interval"),
            ("a setting the form does not edit", '{"slack": {"ask_channel": "C0000000009"}}', "unexpected slack"),
            ("an unknown day", '{"schedule": {"days": ["mon", "tues"]}}', "'tues'"),
            ("an unknown agent", '{"jobs": {"review": "nobody"}}', "'nobody'"),
            ("an end before the start", '{"schedule": {"start": "18:00", "end": "10:00"}}', "an end after its start"),
            # The config's start is 10:00, and the check reads the config as it would be saved.
            ("an end before the config's start", '{"schedule": {"end": "09:00"}}', "an end after its start"),
            ("a time that is not 24-hour", '{"schedule": {"start": "9:30am"}}', "24-hour time"),
            ("not JSON", '{"schedule": {interval_minutes: 30}}', "not JSON"),
        ]:
            with self.subTest(name):
                path = self.use_config(EXAMPLE)
                with self.assertRaises(SystemExit) as caught:
                    self.settings("set", text)
                self.assertIn(why, str(caught.exception))
                self.assertEqual(path.read_bytes(), EXAMPLE.encode())
                self.assertEqual([p.name for p in path.parent.iterdir()], ["config.toml"])

    def test_set_runs_the_jobs_check_on_the_config_as_it_would_be_saved(self):
        broken = EXAMPLE.replace('[jobs]\nreview = "devin"', '[jobs]\nreview = "nobody"')
        path = self.use_config(broken)
        with self.assertRaises(SystemExit) as caught:
            self.settings("set", '{"schedule": {"interval_minutes": 30}}')
        self.assertIn("agents/nobody/review", str(caught.exception))
        self.assertEqual(path.read_text(), broken)
        self.settings("set", '{"jobs": {"review": "claude"}}')
        self.assertEqual(path.read_text(), EXAMPLE.replace('[jobs]\nreview = "devin"', '[jobs]\nreview = "claude"'))

    def test_set_writes_through_a_link_and_keeps_the_files_mode(self):
        # A config kept in a dotfiles folder is often a link to it.
        path = self.use_config(EXAMPLE)
        target = path.with_name("dotfiles-config.toml")
        path.rename(target)
        path.symlink_to(target)
        target.chmod(0o640)
        self.settings("set", '{"schedule": {"interval_minutes": 30}}')
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_text(), EXAMPLE.replace("interval_minutes = 15\n", "interval_minutes = 30\n"))
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_a_failed_write_leaves_the_old_file_and_no_temporary_file(self):
        path = self.use_config(EXAMPLE)
        with mock.patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.settings("set", '{"schedule": {"interval_minutes": 30}}')
        self.assertEqual(path.read_text(), EXAMPLE)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["config.toml"])

    def test_bin_whooper_settings_runs_the_command(self):
        path = self.use_config(EXAMPLE)
        command = [str(ROOT / "bin" / "whooper-settings")]
        got = subprocess.run(command + ["get"], capture_output=True, text=True, timeout=60)
        self.assertEqual((got.returncode, json.loads(got.stdout)["schedule"]["notify"]), (0, "notify.sh"))
        bad = subprocess.run(command + ["set", '{"poll": {"timeout_minutes": 0}}'], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("timeout_minutes is below 5", bad.stderr)
        self.assertEqual(path.read_text(), EXAMPLE)


class ManifestTest(unittest.TestCase):
    """prompting-time-plugin.json, which Prompting Time reads to install, check, start, and show
    Whooper, against schemas/prompting-time-plugin.json and the checkout."""

    MANIFEST = json.loads((ROOT / "prompting-time-plugin.json").read_text())
    SCHEMA = json.loads((ROOT / "schemas" / "prompting-time-plugin.json").read_text())

    def test_the_manifest_fits_its_schema(self):
        check_schema(self.MANIFEST, self.SCHEMA, "manifest")

    def test_the_schema_catches_each_break(self):
        no_page = {key: value for key, value in self.MANIFEST.items() if key != "page"}
        unflagged = [{key: value for key, value in a.items() if key != "confirm"} for a in self.MANIFEST["actions"]]
        for name, changes in [
            ("a name a pt-plugin:// address cannot hold", {"name": "Whooper Reviews"}),
            ("a version that is not <major>.<minor>.<patch>", {"version": "v0.1"}),
            ("an empty start command", {"start": []}),
            ("actions without their confirm flag", {"actions": unflagged}),
            ("a page protocol the format does not know", {"page": {"entry": "page/dist/index.html", "protocol": 3}}),
            ("a status format Prompting Time does not read", {"status": {**self.MANIFEST["status"], "format": 1}}),
            ("a status group whose id is not lowercase", {"status": {**self.MANIFEST["status"], "groups": [
                {"id": "Ready", "title": "Ready", "needs_you": True, "order": 1}]}}),
            ("an unexpected key", {"socket": "whooper.sock"}),
        ]:
            with self.subTest(name), self.assertRaises(ValueError):
                check_schema({**self.MANIFEST, **changes}, self.SCHEMA, "manifest")
        with self.subTest("no page"), self.assertRaises(ValueError):
            check_schema(no_page, self.SCHEMA, "manifest")

    def test_each_command_and_file_it_names_is_in_the_checkout(self):
        for command in [self.MANIFEST["start"]] + [a["command"] for a in self.MANIFEST["actions"]]:
            with self.subTest(command[0]):
                self.assertTrue(os.access(ROOT / command[0], os.X_OK), command)
        # The form edits the settings whooper-settings checks and saves.
        self.assertEqual(ROOT / self.MANIFEST["settings"], SETTINGS_SCHEMA)
        # page/dist is built from page/, which speaks the protocol version the manifest names.
        self.assertTrue((ROOT / "page" / "index.html").exists())
        protocol = re.search(r"PROTOCOL_VERSION = (\d+);", (ROOT / "page" / "src" / "protocol.ts").read_text())
        self.assertEqual(int(protocol[1]), self.MANIFEST["page"]["protocol"])

    def test_status_names_the_groups_whooper_status_puts_items_in_and_row_actions_it_declares(self):
        status = self.MANIFEST["status"]
        # Prompting Time's sections for Whooper before status format 2, in their order.
        self.assertEqual([(g["id"], g["needs_you"]) for g in sorted(status["groups"], key=lambda g: g["order"])], [
            ("ready", True), ("look", True), ("failed", True), ("reviewing", False), ("waiting", False),
            ("done", False)])
        names = {a["name"]: a for a in self.MANIFEST["actions"]}
        for name in status["item_actions"]:
            with self.subTest(name):
                # Prompting Time runs a row action without its confirmation flow only when it needs none.
                self.assertFalse(names[name]["confirm"])
        # Each blocker's action is one the manifest declares.
        self.assertIn("round", names)

    def test_only_submitting_a_review_waits_for_a_confirmation(self):
        # As README.md's Approvals has it, only whooper-submit writes outside this Mac, to GitHub.
        names = [a["name"] for a in self.MANIFEST["actions"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual([a["name"] for a in self.MANIFEST["actions"] if a["confirm"]], ["submit"])

    def test_each_agents_cli_is_needed_when_a_job_runs_on_it(self):
        agents = {path.name for path in (ROOT / "agents").iterdir() if path.is_dir()}
        conditional = {p["command"]: p["only_when"] for p in self.MANIFEST["prerequisites"] if "only_when" in p}
        self.assertEqual(conditional, {agent: {"setting": "jobs", "value": agent} for agent in agents})
        self.assertIn("jobs", json.loads(SETTINGS_SCHEMA.read_text())["properties"])


class SkillHashesTest(unittest.TestCase):
    def write(self, root, files):
        for name, text in files.items():
            path = Path(root) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def test_any_file_edit_changes_only_its_own_skill_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, {"a/SKILL.md": "rule one", "b/SKILL.md": "rule two", "b/examples.md": "example"})
            before = skill_hashes(tmp)
            self.write(tmp, {"b/examples.md": "another example"})
            after = skill_hashes(tmp)
        self.assertEqual(sorted(before), ["a", "b"])
        self.assertEqual(before["a"], after["a"])
        self.assertNotEqual(before["b"], after["b"])

    def test_same_files_in_another_folder_hash_the_same(self):
        with tempfile.TemporaryDirectory() as live, tempfile.TemporaryDirectory() as saved:
            files = {"a/SKILL.md": "rule one", "a/notes/extra.md": "detail"}
            self.write(live, files)
            self.write(Path(saved) / "v2", files)
            self.assertEqual(skill_hashes(live)["a"], skill_hashes(Path(saved) / "v2")["a"])


if __name__ == "__main__":
    unittest.main()
