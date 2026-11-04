import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile

from whooper import (REVIEW_WORDING, bot_list, check_schema, claude_shell_calls, classify, conversation,
                     devin_shell_calls, fill, main, pr_link, poll_items, read_asks, select_work, skill_hashes,
                     slack_posts, summary_text, write_record)

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
        work, drops = select_work([item], self.prs, self.now)
        self.assertEqual(len(work) + len(drops), 1)
        return bool(work)

    def test_no_record_is_work(self):
        self.assertTrue(self.selected(work_item()))

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
            self.assertEqual(select_work([item], prs, second)[0], [])

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
        self.item = {"run_id": self.run.name, "repo": "example-org/example-repo", "pr": 12, "head_sha": "h1", "kind": "first_review",
                     "dry_run": True, "as_of": None}
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

    def test_moved_head_posts_nothing_and_marks_the_record_stale(self):
        post = self.post(head_now="h2")
        self.assertEqual(post["status"], "stale")
        self.assertFalse((self.run / "post-request.json").exists())
        record = self.data / "prs" / "example-org-example-repo-12" / "h1.first_review.json"
        self.assertEqual(json.loads(record.read_text())["status"], "stale")

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
        finally:
            os.environ["WHOOPER_CONFIG"] = test_config
        self.assertIn("WHOOPER_ORGS=example-org\n", env)
        self.assertIn("WHOOPER_REVIEW_AGENT=devin\n", env)
        self.assertIn(f"WHOOPER_REPO_SETUP={ROOT / 'examples' / 'setup.sh'}\n", repo)

    def test_each_agents_settings_are_exported_for_its_launcher(self):
        env = self.command("env")
        for line in ["export WHOOPER_CLAUDE_REVIEW_MODEL=review-model\n", "export WHOOPER_CLAUDE_BUDGET_USD=15\n",
                     "export WHOOPER_DEVIN_REVIEW_MODEL=devin-review-model\n"]:
            self.assertIn(line, env)

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
