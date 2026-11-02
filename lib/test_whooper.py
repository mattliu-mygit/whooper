import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile

from whooper import bot_list, classify, conversation, fill, main, pr_link, poll_items, read_asks, select_work, summary_text, write_record

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
bots = [{ name = "pr_digest_bot", label = "PR digests" }, { name = "Slackbot", label = "Slackbot" }]

[models]
poll = "poll-model"
review = "review-model"

[review]
budget_usd = 15
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
        items = poll_items(LOGIN, [pr(requested=[LOGIN])], [], [])
        self.assertEqual(kinds(items), [(1, "first_review", "github_request")])
        self.assertEqual(items[0]["head_sha"], "head2")
        self.assertEqual(items[0]["base_ref"], "master")
        self.assertIsNone(items[0]["slack"])

    def test_team_request_is_not_work(self):
        self.assertEqual(poll_items(LOGIN, [pr(requested=[("team", "example-team")])], [], []), [])

    def test_pending_review_does_not_count_as_reviewed(self):
        github = [pr(requested=[LOGIN], reviews=[(LOGIN, "PENDING", None, "head2")])]
        self.assertEqual(kinds(poll_items(LOGIN, github, [], [])), [(1, "first_review", "github_request")])

    def test_request_by_name_after_a_review_is_recheck(self):
        # The thread has no reply, which would block a recheck without the re-request.
        github = [pr(
            requested=[LOGIN],
            reviews=[(LOGIN, "COMMENTED", "2031-07-27T01:02:46Z", "head1")],
            threads=[(False, [(LOGIN, "2031-07-27T01:02:46Z")])],
        )]
        self.assertEqual(kinds(poll_items(LOGIN, github, [], [])), [(1, "recheck", "github_request")])

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
                self.assertEqual(kinds(poll_items(LOGIN, github, [], [])), want)

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
                self.assertEqual(poll_items(LOGIN, [pr(reviews=reviews)], [], []), [])

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
                self.assertEqual(kinds(poll_items(LOGIN, [pr(reviews=reviews)], [], [])), [(1, "recheck", "reviewed_by_user")])

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
                items = poll_items(LOGIN, [pr(head=head, reviews=review, threads=threads)], [], [])
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
        items = poll_items(LOGIN, github, [], [])
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
        self.assertEqual(poll_items(LOGIN, github, [], []), [])

    def test_latest_review_is_by_submission_time(self):
        # The API lists reviews by creation. A review started earlier can be submitted later.
        github = [pr(
            head="head2",
            reviews=[
                (LOGIN, "COMMENTED", "2031-07-28T09:00:00Z", "head2"),
                (LOGIN, "COMMENTED", "2031-07-27T09:00:00Z", "head1"),
            ],
        )]
        self.assertEqual(poll_items(LOGIN, github, [], []), [])

    def test_pending_review_is_not_the_latest_review(self):
        github = [pr(
            head="head2",
            reviews=[
                (LOGIN, "COMMENTED", "2031-07-27T01:02:46Z", "head1"),
                (LOGIN, "PENDING", None, "head2"),
            ],
        )]
        items = poll_items(LOGIN, github, [], [])
        self.assertEqual(kinds(items), [(1, "recheck", "reviewed_by_user")])
        self.assertIn("head1", items[0]["reason"])

    def test_truncated_connection_fails(self):
        github = [pr(reviews=[(LOGIN, "COMMENTED", "2031-07-27T01:02:46Z", "head1")])]
        github[0]["reviews"]["totalCount"] = 101
        with self.assertRaises(ValueError):
            poll_items(LOGIN, github, [], [])


class SlackItemsTest(unittest.TestCase):
    human_review = ("bob", "COMMENTED", "2031-07-27T01:00:00Z", "head1")
    # Review bots and the PR author's own thread replies are not someone reviewing.
    non_human_reviews = [
        ("ci-review-bot", "COMMENTED", "2031-07-26T18:31:52Z", "head1"),
        ("assistant-bot", "COMMENTED", "2031-07-26T18:40:00Z", "head1"),
        ("alice", "COMMENTED", "2031-07-26T18:59:38Z", "head1", "reply"),
    ]

    def test_tagged_ask_is_first_review_with_its_post(self):
        items = poll_items(LOGIN, [], [ask(tagged=True)], [pr()])
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
            ("a human reviewed", False, [self.human_review], False),
        ]
        for name, eyes, reviews, taken in cases:
            with self.subTest(name):
                items = poll_items(LOGIN, [], [ask(tagged=False, eyes=eyes)], [pr(reviews=reviews)])
                self.assertEqual(kinds(items), [(1, "first_review", "slack_ask")] if taken else [])

    def test_tagged_ask_ignores_eyes_and_other_reviews(self):
        items = poll_items(LOGIN, [], [ask(tagged=True, eyes=True)], [pr(reviews=[self.human_review])])
        self.assertEqual(kinds(items), [(1, "first_review", "slack_ask")])

    def test_tagged_ask_after_the_users_review_is_recheck(self):
        reviewed = pr(reviews=[(LOGIN, "COMMENTED", "2031-07-27T01:02:46Z", "head1")])
        self.assertEqual(kinds(poll_items(LOGIN, [], [ask()], [reviewed])), [(1, "recheck", "slack_ask")])

    def test_closed_merged_and_own_prs_are_dropped(self):
        for name, data in {
            "closed": pr(state="CLOSED"),
            "merged": pr(state="MERGED"),
            "the user's own PR": pr(author=LOGIN),
        }.items():
            with self.subTest(name):
                self.assertEqual(poll_items(LOGIN, [], [ask()], [data]), [])

    def test_ask_for_a_pr_without_data_is_skipped(self):
        self.assertEqual(poll_items(LOGIN, [], [ask(urls=["https://github.com/example-org/example-repo/pull/2"])], [pr()]), [])

    def test_one_item_per_pr_and_kind_preferring_tagged_slack_asks(self):
        github = [pr(requested=[LOGIN])]
        asks = [ask(ts="1943194600.000001", tagged=False), ask(ts="1943194837.000100", tagged=True)]
        items = poll_items(LOGIN, github, asks, [pr()])
        self.assertEqual(kinds(items), [(1, "first_review", "slack_ask")])
        self.assertEqual(items[0]["slack"]["ts"], "1943194837.000100")

    def test_request_by_name_wins_over_an_untagged_ask(self):
        # An untagged ask lets the review run skip a PR that is not small. A request by name does not.
        items = poll_items(LOGIN, [pr(requested=[LOGIN])], [ask(tagged=False)], [pr()])
        self.assertEqual(kinds(items), [(1, "first_review", "github_request")])

    def test_each_linked_pr_is_its_own_item(self):
        urls = ["https://github.com/example-org/example-repo/pull/1", "https://github.com/example-org/example-repo/pull/2"]
        items = poll_items(LOGIN, [], [ask(urls=urls)], [pr(number=1), pr(number=2)])
        self.assertEqual(kinds(items), [(1, "first_review", "slack_ask"), (2, "first_review", "slack_ask")])


CHANNEL = "C0000000001"
USER = "U0000000000"
SLACK_URL = "https://example.slack.com"
ORGS = ["example-org", "sample-org"]
READ = "mcp__claude_ai_Slack__slack_read_channel"
OLDEST = "1943117600"
ASKER = "Alex Asker <alex@example.com> (U0000000001)"
TEAMMATE = "Bob Teammate <bob@example.com> (U0000000002)"
SELF = "Sam Reviewer <sam@example.com> (U0000000000)"
PR_BOT = "pr_bot <botuser-T0000000000-A0000000000@slack-bots.com> (U07ABCDEF12)"


def post(ts, text, sender=ASKER):
    """One post as slack_read_channel prints it."""
    return f"=== Message from {sender} at 2031-07-30 09:20:37 PDT === \nMessage TS: {ts}\n{text}\n\n\n"


POST_TS = "1943194837.000100"
POST = post(POST_TS, (
    "<@U0000000000|Sam Reviewer> can you take a look?\n"
    "• <https://github.com/example-org/example-repo/pull/21|feat(app): add a settings page #21>\n"
    "• <http://github.com/example-org/example-repo/pull/22|#22>\n"
    "Reactions: eyes (1)"
))
LAST_PAGE = "There are no more messages available.\n"
MORE = "There are more messages available. To view the next page, use cursor: `bmV4dF90czox`"


def page(posts=POST, pagination=LAST_PAGE):
    """A slack_read_channel result as the connector returns it: JSON text that escapes "/" as "\\/"."""
    text = json.dumps({"messages": f"Channel: #pr-reviews ({CHANNEL})\n\n{posts}",
                       "pagination_info": pagination})
    return [{"type": "text", "text": text.replace("/", "\\/")}]


def read(content=None, is_error=None, **input_overrides):
    """One slack_read_channel call and its result. content is the result's content."""
    return {"channel_id": CHANNEL, "oldest": OLDEST, "limit": 20, **input_overrides}, content or page(), is_error


def transcript(asks, reads=None, result_subtype="success", not_asks=()):
    """Stream-json events of a poll run that loads the read tool, makes the reads, and returns asks."""
    reads = [read()] if reads is None else reads
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_0", "name": "ToolSearch", "input": {"query": f"select:{READ}"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_0", "content": [{"type": "tool_reference", "tool_name": READ}]}]}},
    ]
    for n, (call_input, content, is_error) in enumerate(reads, 1):
        events += [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"toolu_{n}", "name": READ, "input": call_input}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"toolu_{n}", "is_error": is_error, "content": content}]}},
        ]
    output = {"asks": asks, "not_asks": list(not_asks)} if result_subtype == "success" else None
    events.append({"type": "result", "subtype": result_subtype, "is_error": result_subtype != "success",
                   "structured_output": output})
    return events


def model_ask(**overrides):
    out = {
        "ts": POST_TS,
        "pr_urls": ["https://github.com/example-org/example-repo/pull/21"],
        "reason": "Alex asked the user to review #21.",
    }
    out.update(overrides)
    return out


# Alex's post, reported as not an ask, for tests about other posts.
ASKER_NOT_ASK = {"ts": POST_TS, "reason": "Not an ask."}


class ReadAsksTest(unittest.TestCase):
    def test_ask_gets_a_permalink_built_from_its_ts(self):
        asks, problems = read_asks(transcript([model_ask()]), OLDEST, CHANNEL, USER, SLACK_URL, ORGS)
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
        asks, problems = read_asks(transcript([model_ask(pr_urls=["https://github.com/example-org/example-repo/pull/22"])]),
                                   OLDEST, CHANNEL, USER, SLACK_URL, ORGS)
        self.assertEqual(problems, [])
        self.assertEqual(asks[0]["pr_urls"], ["https://github.com/example-org/example-repo/pull/22"])

    def test_asks_from_every_page_count_once_the_last_page_is_read(self):
        reads = [
            read(content=page(pagination=MORE)),
            # detailed is the format that shows reactions, and the default.
            read(content=page(posts=post("1943207600.000001", "<https://github.com/example-org/example-repo/pull/7|#7>")),
                 cursor="bmV4dF90czox", response_format="detailed"),
        ]
        asks, problems = read_asks(
            transcript([model_ask(), model_ask(ts="1943207600.000001", pr_urls=["https://github.com/example-org/example-repo/pull/7"])], reads),
            OLDEST, CHANNEL, USER, SLACK_URL, ORGS)
        self.assertEqual(problems, [])
        self.assertEqual([a["ts"] for a in asks], [POST_TS, "1943207600.000001"])

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
                content = page(posts=f"{POST}\n{url}" if in_post else POST)
                asks, problems = read_asks(transcript([model_ask(pr_urls=[url])], [read(content=content)]),
                                           OLDEST, CHANNEL, USER, SLACK_URL, ORGS)
                self.assertEqual(asks, [])
                self.assertTrue(problems)

    def test_pr_url_must_be_linked_by_the_post_that_asks(self):
        # The bot's digest links #34, but Alex's post does not.
        digest = post("1943197600.000001", "• <https://github.com/example-org/example-repo/pull/34|#34>", sender=PR_BOT)
        urls = ["https://github.com/example-org/example-repo/pull/21", "https://github.com/example-org/example-repo/pull/34"]
        asks, problems = read_asks(transcript([model_ask(pr_urls=urls)], [read(content=page(POST + digest))]),
                                   OLDEST, CHANNEL, USER, SLACK_URL, ORGS)
        self.assertEqual([a["pr_urls"] for a in asks], [["https://github.com/example-org/example-repo/pull/21"]])
        self.assertEqual(problems, [f"dropped PR URL 'https://github.com/example-org/example-repo/pull/34' from ask {POST_TS}"])

    def test_ask_must_be_a_post_the_run_read_since_oldest(self):
        for name, ts, posts in [
            ("not in the read output", "1943195599.000001", POST),
            ("malformed", "1943194837", POST + post("1943194837", "<https://github.com/example-org/example-repo/pull/21|#21>")),
            ("only in a post's text", "1943207600.123456", POST + "\nsee 1943207600.123456"),
            # A real post, so only the time check can drop it.
            ("before oldest", "1943017600.000001",
             POST + post("1943017600.000001", "<https://github.com/example-org/example-repo/pull/21|#21>")),
        ]:
            with self.subTest(name):
                events = transcript([model_ask(ts=ts)], [read(content=page(posts))], not_asks=[ASKER_NOT_ASK])
                asks, problems = read_asks(events, OLDEST, CHANNEL, USER, SLACK_URL, ORGS)
                self.assertEqual(asks, [])
                self.assertTrue(problems)

    def test_mention_and_eyes_are_read_from_the_post(self):
        eyes_no_mention = post("1943197600.000001",
                               "<https://github.com/example-org/example-repo/pull/50|#50>\nReactions: squirrel (1), eyes (2)", sender=TEAMMATE)
        mention_no_eyes = post("1943197601.000001",
                               "<@U0000000000> <https://github.com/example-org/example-repo/pull/51|#51>\nReactions: eyes-closed (1)")
        model_asks = [
            model_ask(ts="1943197600.000001", pr_urls=["https://github.com/example-org/example-repo/pull/50"]),
            model_ask(ts="1943197601.000001", pr_urls=["https://github.com/example-org/example-repo/pull/51"]),
        ]
        posts = POST + eyes_no_mention + mention_no_eyes
        asks, problems = read_asks(transcript(model_asks, [read(content=page(posts))], not_asks=[ASKER_NOT_ASK]),
                                   OLDEST, CHANNEL, USER, SLACK_URL, ORGS)
        self.assertEqual(problems, [])
        self.assertEqual([(a["ts"], a["tagged_user"], a["has_eyes"]) for a in asks],
                         [("1943197600.000001", False, True), ("1943197601.000001", True, False)])

    def test_ask_from_a_bot_is_dropped(self):
        digest = post("1943197600.000001", "• <https://github.com/example-org/example-repo/pull/34|#34>", sender=PR_BOT)
        bot_ask = model_ask(ts="1943197600.000001", pr_urls=["https://github.com/example-org/example-repo/pull/34"])
        asks, problems = read_asks(transcript([bot_ask], [read(content=page(POST + digest))], not_asks=[ASKER_NOT_ASK]),
                                   OLDEST, CHANNEL, USER, SLACK_URL, ORGS)
        self.assertEqual(asks, [])
        self.assertEqual(problems, ["dropped ask 1943197600.000001: a bot wrote it"])

    def test_every_post_that_mentions_the_user_next_to_a_pr_link_is_answered(self):
        link = "<@U0000000000|Sam Reviewer> <https://github.com/example-org/example-repo/pull/50|#50>"
        for name, posts, not_asks, raises in [
            ("left out", POST, [], True),
            ("reported as not an ask", POST, [ASKER_NOT_ASK], False),
            ("written by the user", post("1943197600.000001", link, sender=SELF), [], False),
            ("written by a bot", post("1943197600.000001", link, sender=PR_BOT), [], False),
            ("no PR link", post("1943197600.000001", "<@U0000000000|Sam Reviewer> lunch?"), [], False),
        ]:
            with self.subTest(name):
                events = transcript([], [read(content=page(posts))], not_asks=not_asks)
                if raises:
                    with self.assertRaises(ValueError):
                        read_asks(events, OLDEST, CHANNEL, USER, SLACK_URL, ORGS)
                else:
                    self.assertEqual(read_asks(events, OLDEST, CHANNEL, USER, SLACK_URL, ORGS), ([], []))

    def test_failed_run_raises(self):
        # The CLI's reply when a read is too large: a string, not marked as an error.
        too_large = "Error: result (103,864 characters across 1 line) exceeds maximum allowed tokens."
        for name, reads, subtype in [
            ("result is an error", [read()], "error_during_execution"),
            ("never read", [], "success"),
            ("read failed", [read(is_error=True)], "success"),
            ("read result too large", [read(content=too_large)], "success"),
            ("stopped before the last page", [read(content=page(pagination=MORE))], "success"),
            ("read the wrong channel", [read(channel_id="C000")], "success"),
            ("read from a later time", [read(oldest="1943217600")], "success"),
            ("also read the user's DMs", [read(), read(channel_id=USER)], "success"),
            ("read up to an end time", [read(latest="1943197600")], "success"),
            ("read without reactions", [read(response_format="concise")], "success"),
            ("followed a cursor no page gave", [read(content=page(pagination=MORE)), read(cursor="b3RoZXI=")], "success"),
        ]:
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    read_asks(transcript([model_ask()], reads, subtype), OLDEST, CHANNEL, USER, SLACK_URL, ORGS)


def work_item(kind="recheck", head="head3", number=12):
    return {"kind": kind, "repo": "example-org/example-repo", "pr": number, "head_sha": head, "base_ref": "master",
            "source": "reviewed_by_user", "slack": None, "reason": "r"}



class PollGuardTest(unittest.TestCase):
    """whooper.py poll-guard as the poll run's hooks call it: each read is recorded after it
    returns, and the run's answer is blocked with exit code 2 until the reads reach the last page."""

    def setUp(self):
        self.reads = Path(tempfile.mkdtemp()) / "reads.jsonl"

    def hook(self, payload):
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(["python3", "-I", str(ROOT / "lib" / "whooper.py"), "poll-guard", str(self.reads), OLDEST, CHANNEL],
                              input=stdin, capture_output=True, text=True).returncode

    def after_read(self, call_input, content):
        return self.hook({"hook_event_name": "PostToolUse", "tool_name": READ, "tool_input": call_input, "tool_response": content})

    def answer(self):
        return self.hook({"hook_event_name": "PreToolUse", "tool_name": "StructuredOutput", "tool_input": {"asks": [], "not_asks": []}})

    def test_answer_waits_for_the_last_page(self):
        first, _, _ = read()
        # The first page points at a second page with a cursor; the answer is blocked until it is read.
        self.assertEqual(self.after_read(first, page(pagination=MORE)), 0)
        self.assertEqual(self.answer(), 2)
        self.assertEqual(self.after_read({**first, "cursor": "bmV4dF90czox"}, page()), 0)
        self.assertEqual(self.answer(), 0)

    def test_answer_is_blocked(self):
        call, content, _ = read()
        cases = [
            ("no read at all", []),
            ("a read of another channel", [({**call, "channel_id": "C0000000009"}, content)]),
            ("a read with a cursor no page gave", [({**call, "cursor": "made-up"}, content)]),
        ]
        for name, reads in cases:
            with self.subTest(name):
                self.setUp()
                for call_input, result in reads:
                    self.after_read(call_input, result)
                self.assertEqual(self.answer(), 2)

    def test_input_it_cannot_parse_blocks(self):
        # Claude Code would let the answer through if the hook failed with exit code 1.
        self.assertEqual(self.hook("not json"), 2)

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


class ClassifyTest(unittest.TestCase):
    def verdict(self, f, fetched=True):
        [out] = classify({"status": "reviewed", "skip_reason": "", "findings": [f]}, EVENTS, fetched, RUN)
        return out["verdict"], out["checks_failed"]

    def test_all_checks_pass_is_auto_post(self):
        self.assertEqual(self.verdict(finding()), ("auto_post", []))

    def test_any_failed_check_sends_the_finding_to_the_digest(self):
        cases = [
            (name, finding(rule_v1={**finding()["rule_v1"], name: False}), True)
            for name in ["ran_and_saw_failure", "caller_or_ci_visible", "introduced_by_pr", "not_already_raised", "not_pr_text"]
        ] + [
            ("line outside the diff", finding(in_diff=False), True),
            ("head_run names no tool call", finding(head_run="toolu_missing"), True),
            ("head_run names a Read call", finding(head_run="toolu_read"), True),
            ("head_run names a subagent's Bash call", finding(head_run="toolu_sub_head"), True),
            ("base_run names no tool call", finding(base_run=""), True),
            ("base_run names a Read call", finding(base_run="toolu_read"), True),
            ("base_run names another command", finding(base_run="toolu_cp"), True),
            ("the probe names no worktree", finding(head_run="toolu_head_cwd", base_run="toolu_base_cwd"), True),
            ("base_run failed", finding(base_run="toolu_base_build"), True),
            ("base_run's output was cut", finding(base_run="toolu_base_long"), True),
            ("observed is not in the head output", finding(observed="got status 400"), True),
            ("observed is also in the base output", finding(observed="services/orders/internal/api"), True),
            ("the GitHub fetch failed", finding(), False),
        ]
        for name, f, fetched in cases:
            with self.subTest(name):
                verdict, failed = self.verdict(f, fetched)
                self.assertEqual(verdict, "digest")
                self.assertEqual(len(failed), 1, failed)

    def test_every_nit_is_a_shadow_nit(self):
        for name, f in [
            ("passes every check", finding(kind="nit", nit_kind="codeowners_no_effect")),
            ("fails a check", finding(kind="nit", nit_kind="wording", in_diff=False)),
        ]:
            with self.subTest(name):
                self.assertEqual(self.verdict(f)[0], "shadow_nit")

    def test_skipped_run_has_no_verdicts(self):
        # The schema allows findings on a skipped run, and a skipped PR gets no comments.
        result = {"status": "skipped", "skip_reason": "not small", "findings": [finding()]}
        self.assertEqual(classify(result, EVENTS, True, RUN), [])


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
        (self.run / "findings.json").write_text(json.dumps({"status": "reviewed", "skip_reason": "", "findings": []}))
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
        self.assertIn(f"WHOOPER_REPO_SETUP={ROOT / 'examples' / 'setup.sh'}\n", repo)

    def test_a_repo_missing_from_the_config_fails(self):
        with self.assertRaises(SystemExit):
            self.command("repo", "other-org/example-repo")

    def test_prompts_are_filled_from_the_config(self):
        review = self.command("prompt", "review", "example-org/example-repo")
        for text in ["sam-example (Sam)", "`review-skill`, `language-skill`", "the CI review bot (ci-review-bot)",
                     "Run `make test` from the service's directory.", "as Sam would post it"]:
            self.assertIn(text, review)
        self.assertIn("Notes for this repository:\n\nNone.", self.command("prompt", "review", "sample-org/plain-repo"))
        self.assertIn("PR digests (pr_digest_bot), Slackbot (Slackbot)", self.command("prompt", "poll"))
        for name in ["review", "poll"]:
            with self.subTest(name):
                self.assertNotIn("{{", self.command("prompt", name, "example-org/example-repo"))

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


if __name__ == "__main__":
    unittest.main()
