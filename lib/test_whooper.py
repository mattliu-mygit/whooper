import unittest
from contextlib import redirect_stderr, redirect_stdout, suppress
from datetime import datetime, time, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from time import monotonic, sleep
from unittest import mock

from whooper import (REVIEW_WORDING, bot_list, check_schema, claude_shell_calls, classify, conversation,
                     devin_shell_calls, fill, main, next_round, pr_link, poll_items, read_asks, read_drafts,
                     read_schedule, select_work, skill_hashes, slack_posts, summary_text, write_record)

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


def setUpModule():
    config = Path(tempfile.mkdtemp()) / "config.toml"
    config.write_text(CONFIG)
    (config.parent / "notes.md").write_text("Run `make test` from the service's directory.")
    os.environ["WHOOPER_CONFIG"] = str(config)
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


def work_item(kind="recheck", head="head3", number=12):
    return {"kind": kind, "repo": "example-org/example-repo", "pr": number, "head_sha": head, "base_ref": "master",
            "source": "reviewed_by_user", "slack": None, "reason": "r"}



class SelectWorkTest(unittest.TestCase):
    # 10:00 on 2031-08-01 in a UTC-7 timezone.
    now = datetime(2031, 8, 1, 10, 0, tzinfo=timezone(timedelta(hours=-7)))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.prs = Path(tmp.name)

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
        return {**work_item(kind="first_review", head=head), "source": "slack_ask", "slack": slack,
                "reason": "Alex asked for a review."}

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
                self.record("head3.recheck.json", status, failures)
                self.assertEqual(self.selected(work_item()), want)

    def test_record_for_another_head_or_kind_does_not_match(self):
        self.record("head2.recheck.json", "done")
        self.record("head3.first_review.json", "done")
        self.record("head3.recheck.json", "done", pr_dir="example-org-example-repo-1")
        self.assertTrue(self.selected(work_item()))

    def test_three_rechecks_today_stop_a_fourth(self):
        for head in ["head0", "head1", "head2"]:
            self.record(f"{head}.recheck.json", "done", started_at="2031-08-01T16:00:00Z")
        self.assertFalse(self.selected(work_item()))
        self.assertTrue(self.selected(work_item(kind="first_review")))

    def test_failed_third_recheck_today_is_retried(self):
        self.record("head1.recheck.json", "done", started_at="2031-08-01T16:00:00Z")
        self.record("head2.recheck.json", "done", started_at="2031-08-01T16:20:00Z")
        self.record("head3.recheck.json", "failed", failures=1, started_at="2031-08-01T16:40:00Z")
        self.assertTrue(self.selected(work_item()))

    def test_rechecks_count_by_local_day(self):
        self.record("head1.recheck.json", "done", started_at="2031-08-01T16:00:00Z")
        self.record("head2.recheck.json", "done", started_at="2031-08-01T16:30:00Z")
        # 23:30 on 2031-07-31 at UTC-7, though 2031-08-01 in UTC.
        self.record("head0.recheck.json", "done", started_at="2031-08-01T06:30:00Z")
        self.assertTrue(self.selected(work_item()))

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
        polled, listed = work_item(kind="first_review", head="head4"), self.listed_again()
        self.assertEqual(select_work([polled], self.prs, self.now, REPOS, [listed]), ([polled], [], [listed]))

    def test_the_recheck_limit_holds_a_recheck_list_item_for_the_next_day(self):
        for head in ["head0", "head1", "head2"]:
            self.record(f"{head}.first_review.json", "stale", started_at="2031-08-01T16:00:00Z")
        listed = self.listed_again()
        self.assertEqual(select_work([], self.prs, self.now, REPOS, [listed]),
                         ([], [(listed, "3 re-checks already today")], []))
        # The poll's own first reviews have no daily limit.
        self.assertTrue(self.selected(work_item(kind="first_review")))
        tomorrow = self.now + timedelta(days=1)
        self.assertEqual(select_work([], self.prs, tomorrow, REPOS, [listed]), ([listed], [], []))


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


class CheckSchemaTest(unittest.TestCase):
    SCHEMA = json.loads((ROOT / "schemas" / "findings.json").read_text())

    def result(self, **overrides):
        out = {"status": "reviewed", "skip_reason": "", "findings": [finding()],
               "approval": {"verdict": "look_first", "reason": "Ran the api tests; no database path."}}
        out.update(overrides)
        return out

    def test_a_result_that_fits_passes(self):
        check_schema(self.result(), self.SCHEMA)

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
            ("an integer where a boolean belongs", self.result(findings=[finding(in_diff=1)])),
        ]:
            with self.subTest(name), self.assertRaises(ValueError):
                check_schema(value, self.SCHEMA)

    def test_the_check_command_fails_on_a_file_that_does_not_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "findings.json"
            for text in [json.dumps(self.result(status="done")), "not JSON"]:
                path.write_text(text)
                with self.subTest(text), self.assertRaises(SystemExit):
                    main("check", str(ROOT / "schemas" / "findings.json"), str(path))


def review_comment(path="services/orders/internal/api/orders.go", line=88, start_line=None, side="RIGHT",
                   created_at="2031-07-27T01:30:00Z"):
    """A review comment as GitHub's REST API lists it, on the head's lines start_line to line."""
    return {"path": path, "line": line, "original_line": line, "start_line": start_line,
            "original_start_line": start_line, "side": side, "created_at": created_at}


class PostTest(unittest.TestCase):
    """whooper.py post for a run started 2031-07-27T01:02:46Z at head h1, whose verdicts are one
    auto_post finding on orders.go:88 and one digest finding."""

    def setUp(self):
        self.data = Path(tempfile.mkdtemp())
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


class SubmitTest(unittest.TestCase):
    """bin/whooper-submit, and whooper.py draft, which bin/whooper-draft runs, on a review run of
    example-org/example-repo#12 at head h1 in a throwaway state folder. gh is a fake: it reports the
    PR's head from the file head and the CI checks on it from checks.json, lists a teammate's comment
    on finding 1's line written after the review started, records the body of each review it is asked
    to post in posted.jsonl, and fails any other call, so no test can reach GitHub. Unless a test
    changes it, the run is not safe to approve: its verdict is look_first, and of its findings BUG,
    QUESTION, and NIT, only NIT is a nit."""

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

    def shown(self, *flags):
        """Runs whooper-submit --print with flags, as Prompting Time's confirmation does, and returns
        the fingerprint it prints."""
        printed = self.submit("--print", *flags)
        self.assertEqual(printed.returncode, 0, printed.stderr)
        return printed.stdout.splitlines()[-1].removeprefix("fingerprint: ")

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

    def test_a_review_without_comments_posts_only_as_an_approval(self):
        # The run is not safe to approve, so its review starts as a comment with nothing included; a
        # message alone does not count.
        for change in [None, ("message", "Looks fine.")]:
            with self.subTest(change=change):
                if change:
                    self.draft(*change)
                empty = self.submit()
                self.assertEqual(empty.returncode, 1)
                self.assertIn("nothing to submit", empty.stderr)
        self.draft("approve")
        result = self.submit("--approve-anyway")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.posted(), [{"commit_id": "h1", "event": "APPROVE", "body": "Looks fine.", "comments": []}])

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
        # A review that did not finish has no drafts to print.
        (self.run / "findings.json").unlink()
        with self.assertRaises(SystemExit) as caught:
            self.draft()
        self.assertIn("did not finish", str(caught.exception))


class SlackGuardTest(unittest.TestCase):
    """hooks/slack-guard.sh, fed the PreToolUse input Claude Code sends it."""

    HOOK = ROOT / "hooks" / "slack-guard.sh"
    SLACK = "mcp__claude_ai_Slack__slack"
    # The channels of the test config, which the hook reads through WHOOPER_CONFIG.
    ASK_CHANNEL, SUMMARY_CHANNEL = "C0000000001", "C0000000002"
    ASK = {"channel_id": ASK_CHANNEL, "message_ts": "1761700000.000100", "emoji": "eyes"}

    def setUp(self):
        self.summary = Path(tempfile.mkdtemp()) / "summary.txt"
        self.summary.write_text("Whooper reviewed https://github.com/example-org/example-repo/pull/12 at cb54aa8.\nLog: log.md")
        self.schedule = {"channel_id": self.SUMMARY_CHANNEL, "message": self.summary.read_text(), "post_at": 1761700180}

    def run_hook(self, tool, tool_input, **env):
        """Runs the hook with the variables a summary run sets, changed by env, where None unsets one."""
        env = {"PATH": os.environ["PATH"], "WHOOPER_CONFIG": os.environ["WHOOPER_CONFIG"], "WHOOPER_ASK_TS": "1761700000.000100", "WHOOPER_SUMMARY_FILE": str(self.summary),
               "WHOOPER_SUMMARY_POST_AT": "1761700180", **env}
        env = {k: v for k, v in env.items() if v is not None}
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": f"{self.SLACK}_{tool}", "tool_input": tool_input})
        return subprocess.run([str(self.HOOK)], input=payload, env=env, capture_output=True, text=True).returncode

    def test_allows_eyes_on_the_ask_and_the_composed_summary(self):
        self.assertEqual(self.run_hook("add_reaction", self.ASK), 0)
        self.assertEqual(self.run_hook("schedule_message", self.schedule), 0)

    def test_blocks_everything_else(self):
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
            # An unset variable makes bash exit 1, which Claude Code would treat as allowing the call.
            ("a run with no summary time", "schedule_message", self.schedule, {"WHOOPER_SUMMARY_POST_AT": None}),
        ]
        for name, tool, tool_input, env in cases:
            with self.subTest(name):
                self.assertEqual(self.run_hook(tool, tool_input, **env), 2)

    def test_second_summary_is_blocked(self):
        self.assertEqual(self.run_hook("schedule_message", self.schedule), 0)
        self.assertEqual(self.run_hook("schedule_message", self.schedule), 2)


class RunTest(unittest.TestCase):
    """bin/whooper-run, whooper-round, whooper-status, whooper-pause, and whooper-resume on a copy of
    the test config and a throwaway state folder, with gh, slackdump, and the agent CLIs replaced by
    commands that fail unless a test says otherwise, so even a broken check cannot reach GitHub,
    Slack, or a model. Each command runs in a process group of its own, which the test stops whole
    when it ends."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.fakes = tmp / "bin"
        for name in ["gh", "slackdump", "claude", "devin"]:
            self.script(self.fakes / name, "exit 1")
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
        process = subprocess.Popen([str(ROOT / "bin" / name), *args], env={**self.env, **(env or {})},
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

    def fake_one_review_request(self):
        """Has gh report one review request, for example-org/example-repo#12, slackdump an ask channel
        with no posts, and the Claude poll run no asks. The review then fails, since the cache holds no
        clone of the repo."""
        (self.fakes / "pr.json").write_text(json.dumps(pr(number=12, requested=[LOGIN])))
        self.script(self.fakes / "gh", "\n".join([
            'case "$*" in',
            '  "search prs user-review-requested:sam-example --owner example-org "*)',
            "    echo https://github.com/example-org/example-repo/pull/12 ;;",
            '  "search prs "*) ;;',
            '  "api graphql "*) cat "${0%/*}/pr.json" ;;',
            "  *) exit 1 ;;",
            "esac",
        ]))
        # slackdump dumps the channel, its last argument, to <channel>.json in the folder after -o.
        self.script(self.fakes / "slackdump", "\n".join([
            'while [ $# -gt 1 ]; do [ "$1" = -o ] && out="$2"; shift; done',
            """mkdir -p "$out" && echo '{"messages": []}' > "$out/$1.json\"""",
        ]))
        # The poll's answer is the result at the end of Claude's stream-json output.
        self.script(self.fakes / "claude",
                    """echo '{"type": "result", "subtype": "success", "structured_output": {"asks": [], "not_asks": []}}'""")

    def test_refuses_to_start_while_another_whooper_run_holds_the_lock(self):
        # A script named whooper-run stands in for a copy that is already polling.
        holder = self.holder("whooper-run")
        self.hold(self.lock, holder)
        result = self.command("whooper-run")
        self.assertEqual((result.returncode, (self.lock / "pid").read_text()), (1, f"{holder}\n"))
        self.assertIn(f"process {holder} is already running", result.stderr)

    def test_takes_over_a_lock_whose_process_is_some_other_program(self):
        # After a kill -9 or a restart, the lock's process ID can belong to anything, here this test.
        self.hold(self.lock, os.getpid())
        process = self.start(ROOT / "bin" / "whooper-run")
        self.wait_for(lambda: self.read(self.lock / "pid") == f"{process.pid}\n", "whooper-run to take the lock")
        process.terminate()
        process.wait(timeout=10)
        self.assertFalse(self.lock.exists())

    def test_a_round_refuses_to_start_while_another_round_runs(self):
        holder = self.holder("whooper-round")
        self.hold(self.state / "round.lock", holder)
        result = self.command("whooper-round")
        self.assertEqual((result.returncode, (self.state / "round.lock" / "pid").read_text()), (1, f"{holder}\n"))
        self.assertIn("a round is already running", result.stderr)
        self.assertFalse((self.state / "last-round.json").exists())

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
        self.assertEqual(self.notifications.read_text(),
                         "Whooper: example-org/example-repo#12 review failed | See whooper-round's output.\n")
        self.assertFalse((self.state / "round.lock").exists())

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
        last = self.status()["last_round"]
        self.assertEqual((loop.returncode, last["started_at"]), (130, running["started_at"]))
        self.assertEqual({key: last[key] for key in ["result", "selected", "reviews"]},
                         {"result": "interrupted", "selected": 1,
                          "reviews": [{"pr": "example-org/example-repo#12", "outcome": "interrupted"}]})
        self.assertFalse((self.state / "round.lock").exists())

    def test_status_reports_the_loop_pause_next_round_and_rounds_from_the_state_folder(self):
        nothing = {"version": 1, "loop": {"running": False, "pid": None}, "paused": False, "next_round": None,
                   "round": {"running": False, "started_at": None, "reviewing": None}, "last_round": None}
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
        self.assertEqual(self.status(), {
            "version": 1,
            "loop": {"running": True, "pid": loop},
            "paused": True,
            "next_round": "2031-07-30T10:15:00-07:00",
            "round": {"running": True, "started_at": "2031-07-30T10:00:02-07:00",
                      "reviewing": "example-org/example-repo#12"},
            "last_round": last_round,
        })
        # Locks left by a whooper-run that is gone and by a round whose process ID now belongs to this
        # test: nothing runs, so no round is next either.
        gone = subprocess.Popen(["true"])
        gone.wait()
        (self.lock / "pid").write_text(f"{gone.pid}\n")
        (self.state / "round.lock" / "pid").write_text(f"{os.getpid()}\n")
        (self.state / "paused").unlink()
        self.assertEqual(self.status(), {**nothing, "last_round": last_round})
        usage = self.command("whooper-status")
        self.assertEqual((usage.returncode, usage.stderr), (1, "usage: whooper-status --json\n"))

    def test_whooper_run_skips_rounds_while_paused(self):
        self.assertEqual(self.command("whooper-pause").returncode, 0)
        log = Path(self.tmp.name) / "run.log"
        with log.open("w") as out:
            loop = self.start(ROOT / "bin" / "whooper-run", out)
        # The first round is due at once.
        self.wait_for(lambda: "skipped this round" in log.read_text(), "whooper-run to skip a round")
        skipped = datetime.now(timezone.utc)
        self.wait_for(lambda: self.status()["next_round"], "the next round")
        status = self.status()
        self.assertGreater(datetime.fromisoformat(status["next_round"]), skipped)
        self.assertEqual({key: status[key] for key in ["loop", "paused", "round", "last_round"]},
                         {"loop": {"running": True, "pid": loop.pid}, "paused": True,
                          "round": {"running": False, "started_at": None, "reviewing": None}, "last_round": None})
        self.stop(loop)
        # Resumed, a new whooper-run runs its first round at once, and the fakes fail its poll.
        self.assertEqual(self.command("whooper-resume").returncode, 0)
        self.assertFalse((self.state / "paused").exists())
        self.start(ROOT / "bin" / "whooper-run")
        self.wait_for((self.state / "last-round.json").exists, "a round")
        self.assertEqual(self.status()["last_round"]["result"], "poll_failed")


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

    def test_a_poll_limit_under_five_minutes_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(CONFIG + "\n[poll]\ntimeout_minutes = 3\n")
            os.environ["WHOOPER_CONFIG"], test_config = str(config), os.environ["WHOOPER_CONFIG"]
            try:
                with self.assertRaises(SystemExit):
                    self.command("env")
            finally:
                os.environ["WHOOPER_CONFIG"] = test_config

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

    def test_a_repo_missing_from_the_config_fails(self):
        with self.assertRaises(SystemExit):
            self.command("repo", "other-org/example-repo")

    def test_prompts_are_filled_from_the_config(self):
        review = self.command("prompt", "review", "example-org/example-repo", "claude")
        for text in ["sam-example (Sam)", "`review-skill`, `language-skill`", "the CI review bot (ci-review-bot)",
                     "Run `make test` from the service's directory.", "as Sam would post it"]:
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


def fake(path, body):
    """Writes an executable sh script at path, standing in for a command such as claude."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


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
        for name in ["log.md", "github.json", "transcript.jsonl", "prompt.md", "stderr.log"]:
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
        (self.run / "drafts.json").write_text("{}")
        (self.run / "asks").mkdir()
        (self.run / "asks" / "1.json").write_text("{}")
        (self.run / "asks" / "1.prompt.md").write_text("an earlier ask's prompt")
        # Braces in a question stay as written instead of reading as placeholders.
        question = f"{self.QUESTION} Is {{{{this}}}} one?"
        prompt = self.prompt("claude", str(self.run), question, "2")
        for name in ["log.md", "findings.json", "github.json", "transcript.jsonl", "run.json", "prompt.md",
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
        (self.run / "findings.json").unlink()
        unfinished = self.ask(self.run.name, self.QUESTION)
        self.assertEqual(unfinished.returncode, 1)
        self.assertIn("its review did not finish", unfinished.stderr)
        self.assertFalse((self.run / "asks").exists())

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
        # A limit of 3 seconds, and a run that would take 30.
        config = Path(self.tmp.name) / "config.toml"
        config.write_text(CONFIG + "\n[ask]\ntimeout_minutes = 0.05\n")
        fake(self.fakes / "claude", 'echo $$ > "${0%/*}/pid"\nexec sleep 30')
        started = monotonic()
        result = self.ask(self.run.name, self.QUESTION, env={"WHOOPER_CONFIG": str(config)})
        took = monotonic() - started
        pid = int((self.fakes / "pid").read_text())

        def kill():
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

        self.addCleanup(kill)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("passed its 0.05-minute limit", result.stderr)
        self.assertFalse((self.run / "asks" / "1.json").exists())
        # Stopped at the limit, not when the run would have ended.
        self.assertLess(took, 20)
        deadline = monotonic() + 10
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            if monotonic() > deadline:
                self.fail(f"the run's process {pid} is still running")
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
        # The test config has no [schedule] and no [poll].
        self.assertEqual(json.loads(self.settings("get")), {
            "schedule": {"interval_minutes": 15, "start": "10:00", "end": "18:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
            "poll": {"timeout_minutes": 10},
            "jobs": {"review": "claude", "poll": "claude", "summary": "claude"},
            "agents": {"claude": {"review_model": "review-model", "poll_model": "poll-model", "budget_usd": 15},
                       "devin": {"review_model": "devin-review-model", "poll_model": "devin-poll-model"}},
            "review": {"timeout_minutes": 45, "skills": ["review-skill", "language-skill"]},
        })

    def test_each_job_offers_the_agents_with_a_launcher_for_it(self):
        jobs = json.loads((ROOT / "schemas" / "settings.json").read_text())["properties"]["jobs"]["properties"]
        for job, schema in jobs.items():
            with self.subTest(job):
                self.assertEqual(sorted(schema["enum"]), sorted(p.parent.name for p in (ROOT / "agents").glob(f"*/{job}")))

    def test_set_changes_only_the_given_line_and_keeps_every_comment(self):
        for values, old, new in [
            ({"schedule": {"interval_minutes": 30}}, "interval_minutes = 15\n", "interval_minutes = 30\n"),
            # The changed line keeps its own comment too.
            ({"poll": {"timeout_minutes": 5}}, "timeout_minutes = 10 ", "timeout_minutes = 5 "),
        ]:
            with self.subTest(values):
                path = self.use_config(EXAMPLE)
                self.settings("set", json.dumps(values))
                # [ask] has the same timeout_minutes line as [poll], below it, and keeps it.
                self.assertEqual(path.read_text(), EXAMPLE.replace(old, new, 1))

    def test_a_claude_budget_is_optional_and_can_be_set(self):
        # With no budget_usd, as in the example config, a Claude review has no spending limit.
        self.use_config(EXAMPLE)
        self.assertNotIn("budget_usd", json.loads(self.settings("get"))["agents"]["claude"])
        self.settings("set", json.dumps({"agents": {"claude": {"budget_usd": 7.5}}}))
        self.assertEqual(json.loads(self.settings("get"))["agents"]["claude"]["budget_usd"], 7.5)

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
