# Whooper

Whooper finds the pull requests one GitHub user is expected to review or re-check, and reviews
them with coding-agent runs; the config picks the agent, and `agents/` has a folder per agent. It reads the user's review requests on GitHub and review asks in one
Slack channel, reviews each PR in a sandboxed run, sorts the findings by a strict rule, writes the
GitHub review it would post, and sends a summary to a private Slack channel. It posts nothing to
GitHub on its own: `bin/whooper-submit` posts one review built from the findings the user picked.
`bin/whooper-run` repeats all of this on the schedule the config sets.

## Set it up

Whooper needs `gh` logged in, [slackdump](https://github.com/rusq/slackdump) logged in to the
Slack workspace (`slackdump workspace new <name>`, through a browser), the CLI of each agent the
config's `[jobs]` names logged in (`devin auth login`; `claude`, whose summary uses the claude.ai
Slack connector), `jq`, `git`, `docker`, and [uv](https://docs.astral.sh/uv/). The scripts run Python
through `lib/python`, which has uv supply Python 3.11 or later and the packages `lib/whooper.py`
names, at the versions `lib/whooper.py.lock` pins; the first run downloads what is missing.

1. Copy `examples/config.example.toml` to `~/.config/whooper/config.toml` and fill it in. Everything
   that identifies you, your company, or your repos lives there: your GitHub login and name, the
   GitHub orgs, the Slack workspace and channels, the bots on both sides, the agent each job runs
   on and each agent's models, the skills a review loads, and one table per repo.
2. For each repo, optionally copy `examples/setup.example.sh`, `teardown.example.sh`, and
   `repo-notes.example.md` next to the config and name them in the repo's table. Setup prepares
   what a review needs beyond the two checkouts, such as dependencies or a database; teardown
   removes it; the notes go into the review prompt.
3. Make a partial clone of each repo, which fetches file contents only when a checkout needs them:

   ```sh
   git clone --filter=blob:none --no-checkout https://github.com/<owner>/<repo>.git ~/.cache/whooper/repos/<owner>-<repo>
   ```

Whooper follows the XDG Base Directory spec: the config is under `$XDG_CONFIG_HOME/whooper`
(default `~/.config/whooper`, or set `WHOOPER_CONFIG` to the file), runs and records under
`$XDG_STATE_HOME/whooper` (default `~/.local/state/whooper`), and clones under
`$XDG_CACHE_HOME/whooper` (default `~/.cache/whooper`). The checkout holds no private data.

`skills/` holds public versions of the two review skills a review run loads. A run loads skills
from `~/.claude/skills`, so copy them there or point the config at your own.

## Run it

`bin/whooper-run` runs Whooper until you stop it. Each round is `bin/whooper-round`, which runs the
steps below: fetch, poll, and select, then for each selected item a dry-run review, classify,
`whooper-post --print`, and the summary, and it prints what each step prints. The config's
`[schedule]` sets when: a round starts `interval_minutes` after the last one ended, the first at
once, but only between `start` and `end` on the listed `days`, and otherwise whooper-run waits for
the next start. The defaults are 15 minutes, 10:00 to 18:00, Monday to Friday. When a review
finishes or fails, whooper-round runs `[schedule] notify`, if the config names one, as
`<notify> <title> <message>`. Only one copy runs at a time: whooper-run holds the lock
`<state>/run.lock` and refuses to start while another whooper-run holds it. To run it in the
background with a log:

```sh
bin/whooper-run >> ~/.local/state/whooper/run.log 2>&1 &
```

Four commands work from a terminal and from Prompting Time, which runs them and watches the state
directory for changes:

```sh
bin/whooper-round          # runs a round now, even while paused
bin/whooper-pause          # whooper-run skips each round it comes to, and logs that it did
bin/whooper-resume         # ends the pause
bin/whooper-status --json  # what runs, whether Whooper is paused, the next round, and the last round
```

Only one round runs at a time: whooper-round holds the lock `<state>/round.lock`, and another
whooper-round prints "a round is already running" and exits 1. whooper-status makes no network
calls, and its header lists the fields it prints.

To run the steps by hand:

```sh
bin/whooper-fetch   # GitHub reads -> <state>/poll/github.json
bin/whooper-poll    # slackdump reads Slack, an agent picks the asks -> <state>/poll/<UTC start time>.json
bin/whooper-select  # prints the newest poll's items and the recheck list's that no record marks as handled, one per line
```

To review one work item, save it as a file and run:

```sh
bin/whooper-review <work-item.json> --dry-run [--as-of <ISO 8601 time>]   # prints the run directory
bin/whooper-classify <run directory>                                       # verdicts, digest, record
bin/whooper-post <run id> --print     # writes the review it would post to post-request.json
bin/whooper-summary <run id> --print  # prints the summary; without --print, the summary agent delivers it
```

`--as-of` replays a past review: `github.json` then holds only what people could read on the PR
before that time.

whooper-post re-reads the PR first. When the head moved, it posts nothing and puts the PR on the
recheck list, so the next round reviews the new commits (see [What counts as work](#what-counts-as-work)).
Otherwise it moves to the digest any finding whose line got a new review comment since the run
started, or whose draft @-mentions someone or links outside the configured orgs. Its live posting is
not built yet.
whooper-summary delivers through the agent `[jobs] summary` names. On Claude, a Haiku run sends
it, and its only Slack call, `slack_schedule_message`, is pinned by `hooks/slack-guard.sh` to the
composed text, channel, and time. Devin cannot reach Slack, so it writes `summary.md` in the run
directory instead.

Tests: `lib/python -m unittest discover -s lib`. They run on a made-up config and never read yours.

## Change settings

`bin/whooper-settings get` prints the settings `schemas/settings.json` describes as JSON, with
Whooper's defaults for what the config leaves out. They are the ones a settings form may edit:
`[schedule]`, `[poll] timeout_minutes`, `[jobs]`, each agent's models, `[review]`
`timeout_minutes` and `skills`, and `[ask] timeout_minutes`.
`bin/whooper-settings set '{"schedule": {"interval_minutes": 30}}'` saves part of them. It checks
the values against the schema, and the config as it would be saved with the checks Whooper runs
when it starts, and only then rewrites the lines of those keys, keeping the file's comments. On a
failure it prints why and leaves the file as it was. A change applies from the next round.

## Submit a review

Each review run keeps the user's choices for its review in `drafts.json`: which findings' comments
go in, each one's text when the user edited it, whether the review approves or only comments, and
its message. Prompting Time, Whooper's agent, and a terminal all change them with
`bin/whooper-draft`, which prints the drafts as JSON after each change:

```sh
bin/whooper-draft <run-id>                                  # prints the drafts
bin/whooper-draft <run-id> include 2                        # or leave-out 2
bin/whooper-draft <run-id> text 2 '**question:** Why ...?'  # replaces finding 2's draft, label included
bin/whooper-draft <run-id> approve                          # or comment
bin/whooper-draft <run-id> message 'Thanks, this reads well.'
```

A run is safe to approve when it reviewed the PR, its verdict is `approve_once_ci_passes`, and
every finding it has is a nit. A run that skipped the PR, as the review of a large PR from an
untagged ask does, read too little to vouch for it, so it is not safe whatever its verdict says.
Until the user changes anything, a safe run starts as an approval with every nit included, and any
other run as a comment with nothing included, so the user decides.

`bin/whooper-submit <run-id>` posts the review with the user's own gh login, as one GitHub review
at the head the run reviewed. Each comment is `AI comment (vetted)`, a blank line, and the text,
which keeps the Conventional Comments label it opens with, such as `**nit:**`; a text without one
gets its finding kind's. An approval without a message says `Pre-approving to unblock` when it
includes comments and nothing when it includes none; a message the user set posts as written.

Prompting Time's Submit button and Whooper's agent both run whooper-submit after Prompting Time's
confirmation, which shows the exact request that `whooper-submit <run-id> --print` writes to
`submit-request.json` without posting. `--print` also prints the request's fingerprint, the
SHA-256 of that file, as its last line, `fingerprint: <hex>`, and the submit after the
confirmation passes it back as `--expect <hex>`, so drafts changed after the confirmation post
nothing the user did not see.

whooper-submit re-reads the PR's head first. Each refusal posts nothing and exits with its own
code, so Prompting Time can tell them apart:

| Exit | Why | What then |
| --- | --- | --- |
| 10 | The PR has new commits since the review. | The reviewed head's record says stale and the PR goes on the recheck list, as whooper-post does, so the next round reviews the new commits whatever brought the PR in; `bin/whooper-round` runs that round at once. |
| 11 | The review approves, and the run is not safe to approve. | It lists why: a review the run skipped and why, the verdict and its reason, and the findings that are not nits. `--approve-anyway` approves after the user has looked. |
| 12 | An included comment or the message @-mentions someone. | It lists each mention, and `--allow-mentions` posts them. |
| 13 | The request is not the one whose fingerprint `--expect` gives, because the drafts changed since `--print` showed it. | `--print` shows the new request for another confirmation. |

Any other failure exits 1, or with gh's or jq's own code, none of which is 10 to 13. A comment
someone wrote on the same line since the review, and a link outside the configured orgs, do not
stop it. It refuses a review that includes no comment and does not approve, and a run it already
submitted, so a review never posts twice.

## Ask about a review

`bin/whooper-ask <run-id> '<question>' [--finding <finding-id>]` answers a question about a review
run, such as "Why is it bad to have invisible characters?", or "Why did this review fail?" about one
that did not finish, and prints the answer. Prompting Time's "Ask about this" runs it with the
finding. A new run of the agent that did the review answers, with the review's skills, since Whooper
ends each review's session. A review whooper-review stopped before it wrote `run.json`, such as
when the fetch failed, recorded no agent, so the agent `[jobs] review` names answers about it. Its
prompt, `prompts/ask.md` filled in, names the question, the finding, and the path of each file of
the review's record the run directory has: first `failure.txt`, the reason whooper-review gave when
it stopped the review, then `log.md`, `findings.json`, `diff.patch`, the transcript, `github.json`,
`run.json`, `prompt.md`, `verdicts.json`, `post.json`, `drafts.json`, `submit.json`, `summary.md`,
and earlier asks. For a review that did not finish, whose `findings.json` is missing or holds no
findings Whooper can read, such as an empty file, it also names the review agent's `stderr.log` and
`stdout.log`, which may say why, and `findings.invalid.json`, the findings that did not fit the
schema. The PR's code is gone by then, since whooper-review removes the worktrees, so the run
answers from the record, where `diff.patch` holds the PR's change. [Approvals](#approvals) says what
the run may do.

whooper-ask refuses a review that is still running, and says so: one whose whooper-review holds the
PR's lock with the run's ID in it. A lock whose process is gone, as after a `kill -9`, does not
count, so a review killed that way can be asked about.

whooper-ask saves each answer in the run directory as `asks/<n>.json`, numbered in order from 1:
`question`, `finding` (its ID or null), `answer`, `agent`, `started_at` and `ended_at` in UTC, and
`transcript`, the transcript's path. `asks/<n>.prompt.md` is the run's prompt. A run that passes
`[ask] timeout_minutes` (10 by default) is stopped with everything it started and, like a run that
fails, saves no answer; its number is not used again.

## What counts as work

`lib/whooper.py` applies the rules. `prompts/poll.md` is the only judgment, and it covers Slack
posts only. Only PRs in the configured GitHub orgs count. Each item is one PR and one kind:

- **first_review**: a GitHub review request names the user and the user has not reviewed the PR,
  or a post in the ask channel asks the user for a review. A post that does not mention the user
  counts only while it has no :eyes: and no one but bots and the PR author has reviewed the PR in
  the last 8 hours, and only while the user has not reviewed it.
- **recheck**: the user is requested by name again after reviewing, a post that mentions the user
  asks about a PR the user already reviewed, or the user reviewed the PR without approving, every
  thread the user started has an author reply or is resolved, and the head moved or the author
  replied since the user's latest review.

When whooper-post or whooper-submit finds that a PR's head moved since its review, it posts
nothing and puts the PR on the recheck list. The next round's whooper-select adds it to the work at
the new head, with the kind and source it had, so the new commits get a review even when only a
Slack ask brought the PR in, which a poll reads once. Like a re-check, it waits for the next day
once the PR has 3 records of its kind for other heads started today.

A PENDING review or a thread reply does not count as reviewing. Slack text is untrusted: an ask
must name a post or thread reply slackdump read, and only plain
`https://github.com/<org>/<repo>/pull/<n>` URLs in a configured org that appear in that post reach
`gh`. `lib/whooper.py` reads whether a post mentions the user and has :eyes: from the post itself.
It fails the poll, so the cursor stays, when slackdump or the run fails, when the run's answer does
not fit `schemas/slack-asks.json`, or when the run left out a post by someone else that mentions
the user next to a PR link.

Each poll reads the channel from a day before its cursor and keeps what is new since the cursor:
new posts, new thread replies, and posts edited since, so a stack listed in a thread under an
older post still counts. Posts by bots count too, such as a list of PRs waiting for the team's
review.

## How a review run works

whooper-review takes the PR's lock, then fetches with the user's login everything the run will
read: the head and the merge base of the head and `base_ref`, checked out as two worktrees, and the
PR's conversation as `github.json`. It saves the diff the run reads, `git diff <merge base> HEAD`
in the head worktree, as `diff.patch`, which stays when the worktrees go. `diff.patch` is a plain
patch whatever your git config says: no external diff tool, textconv filter, or color, `a/` and
`b/` before the paths, and a submodule's commit lines rather than its log. Then it runs the repo's
setup script, if the config names one, which may add variables to the run's environment and prints
a paragraph for the prompt. Then the agent that `[jobs] review` names runs from the head worktree
through its launcher, `agents/<agent>/review`. The run follows `prompts/review.md`, filled in from
the config with that agent's tool names and saved as `prompt.md` in the run directory, and leaves
`findings.json`, which must fit `schemas/findings.json`. A run that passes
`[review] timeout_minutes` is stopped, and when whooper-review exits it stops the run and every
process the run started. When whooper-review stops a review for a reason it names, such as the time
limit, a failed setup script, or findings that do not fit the schema, it saves that reason in the
run directory as `failure.txt`. A review stopped any other way gets a fixed reason there:
`the review was interrupted` after Ctrl-C or a `kill`, and, after a command that failed,
`the review stopped at an unexpected error; see whooper-review's error output`, where the
command's own message went.

`agents/claude/review` runs `claude -p`, which returns the findings as structured output, and
reports the run's cost in dollars, capped by `[agents.claude] budget_usd` when the config sets one. `agents/devin/review`
runs `devin -p` in `dangerous` permission mode, so no tool call waits for an approval; the run
writes `findings.json` itself, and the cost line is in ACUs, Devin's billing unit. Its config for
the run, saved as `devin-config.json`, is your `~/.config/devin/config.json` plus Whooper's hook and
deny rules for every Slack tool, which hold in that mode. When the run ends, the launcher deletes its
Devin session with `devin rm`, so runs don't fill Devin Desktop's session list; `transcript.json`
keeps the conversation. `agents/devin/poll` does the same for each poll.

The run gets `GH_TOKEN=disabled`, so `gh` and git's `gh` credential helper fail. Otherwise it loads
what a normal session in the head worktree loads: user settings, and the PR's own settings, hooks,
rules, skills, and MCP servers, so a PR's hook runs on this machine with your logins. Each launcher
denies the Slack writes by name, so the run can post nothing. `hooks/tool-use-id.sh` shows it the
tool-use ID of each shell call, which a finding cites as proof. A run can still write outside its
directory, for example to language package caches.

whooper-classify marks a finding `auto_post` only when every rule v1 answer is true, its line is in
the diff, `github.json` was fetched, and the run's main session, not a subagent, backs it with two
shell calls that each finished inside the call: `head_run` printed `observed`, and `base_run` ran
the same command with the head worktree's path replaced by the base worktree's, succeeded, and did
not print `observed` in an output the transcript holds in full. Every nit is `shadow_nit`, and everything else is `digest`. A
skipped run gets no verdicts.

Each run also returns `approval`: `approve_once_ci_passes` when the run found nothing that needs the
author and checked enough to trust the change, otherwise `look_first`, with a reason that says what
the run did and did not check. The run cannot see CI, so no verdict covers it. The approval goes
into the PR record and the summary. It is the run's judgment: Whooper approves on GitHub only when
the user chooses to, through whooper-submit.

## Approvals

The poll's run gets the channel's posts in its prompt and no way to act: Claude gets no tools, and
Devin starts in an empty directory in accept-edits mode, where anything but writing its answer
needs an approval nobody gives. A poll run that passes `[poll] timeout_minutes` (10 by default) is
stopped with everything it started, and the cursor stays put, so the next poll reads the same posts.
slackdump only reads. Review runs have no GitHub
login and every Slack write denied, and whooper-review refuses to run without `--dry-run`. An ask run
has no GitHub login either, and reads files but writes nothing except its answer. Claude gets only
its Read, Grep, Glob, and Skill tools, can read the run directory and `~/.claude/skills`, runs in
`dontAsk` mode, which denies any call that would wait for an approval, and has the Slack writes
denied by name. Devin starts in an empty directory in accept-edits mode, where it reads any file but
writes only there, with your Devin config minus its allow rules and plus deny rules for the shell
and every Slack tool. The summary run
may call only `slack_schedule_message`, and `hooks/slack-guard.sh` blocks any other Slack call,
any other channel, a thread, a changed text or time, and a second summary. Only whooper-submit writes
to GitHub, with the user's own login: the one review the user chose in a run's `drafts.json`, and an
approval of a run that is not safe to approve only after the user looked and passed `--approve-anyway`.

## Stop it

Stop whooper-run with Ctrl-C, or with `kill -- -$(cat ~/.local/state/whooper/run.lock/pid)` when a
shell started it in the background; either way the round it is running stops too. A `kill` of
whooper-run's process alone waits for that round to end. Interrupt any other script with Ctrl-C,
or stop a review with `kill $(cat ~/.local/state/whooper/locks/*/pid)`. An interrupted review runs
the teardown script, removes its worktrees and lock, saves `the review was interrupted` as its
`failure.txt`, and counts as a failure in its record. The
Slack cursor moves only after a poll writes its output, so an interrupted poll is simply repeated.
A whooper-submit interrupted while gh posts leaves the run's `submit.json` saying `posting`, and
whooper-submit refuses that run until you check the PR on GitHub and delete the file.

## Files under the state directory

whooper-run and whooper-round write each of their files through a temporary file and a rename, so a
program watching the directory never reads half of one, whooper-draft and whooper-submit write a
run's `drafts.json`, `submit-request.json`, and `submit.json` the same way, whooper-ask its
`asks/<n>.json`, and whooper-post and whooper-submit write the recheck list's files the same way too.

- `poll/github.json`: whooper-fetch's latest output, one entry per PR as `queries/pr.graphql`
  returns it.
- `poll/<time>.json`: a poll's work items. `poll/<time>.prompt.md`, `.answer.json`, and
  `.transcript.jsonl` (Claude) or `.transcript.json` (Devin) are its run.
- `poll/cursor`: the Unix time the next poll reads Slack from, which is the last successful poll's
  start time. Without it a poll reads the last 24 hours.
- `prs/<owner>-<repo>-<pr>/<head>.<kind>.json`: a record of work on one head:
  `{"status": "running" | "done" | "failed" | "stale", "failures": <n>, "started_at": "<ISO 8601 time>"}`.
  whooper-review writes running when it starts and failed, with one more failure, when it stops
  without findings. whooper-classify writes done with the run's `approval`, and whooper-post and
  whooper-submit write stale when the head moved.
  whooper-select drops an item in a repo the config has no `[repos."<owner>/<repo>"]` table for,
  an item whose record is done or running or has failed 3 times, and a re-check of a PR that
  already has 3 re-check records for other heads started today (local time). A recheck-list item
  counts under that limit too, by the records of its own kind; dropped for it, the item stays on the
  list for the next day.
- `rechecks/<owner>-<repo>-<pr>.json`: the recheck list: a PR whose head whooper-post or
  whooper-submit found moved since its review, as the work item for the new head, with the kind,
  source, and Slack ask it had. A later mark of the PR replaces it. whooper-select deletes it once
  the newest poll lists the PR, which then comes with the head the PR has now, or once the record at
  the listed head is done or has failed 3 times.
- `locks/<owner>-<repo>-<pr>/`: the lock of the review that holds the PR, with its process ID in
  `pid` and its run ID in `run_id`. A lock whose process is gone is stale: the next review takes
  it, and whooper-ask no longer counts its review as running.
- `run.lock/`: the running whooper-run's lock, with its process ID in `pid`. A lock whose process is
  gone, or is now some other program, is stale, and the next whooper-run takes it over.
- `next-round`: the Unix time of the round whooper-run waits for, written before each wait and
  deleted when the round is due.
- `paused`: an empty file that whooper-pause creates and whooper-resume deletes. While it exists,
  whooper-run skips its rounds.
- `round.lock/`: the running whooper-round's lock, with its process ID in `pid` and the same stale
  rule as `run.lock/`. `round.json` holds the round's start as a Unix time, how many items it
  selected, the PR it is reviewing as `<owner>/<repo>#<number>`, and each finished review's PR and
  outcome: `reviewed` or `failed`.
- `last-round.json`: the last round that ended: its start and end as Unix times, its result
  (`done`, `poll_failed`, or `interrupted`), how many items it selected, and each review's PR and
  outcome, where a review the round stopped in is `interrupted`.
- `items/`: whooper-round's work items: `selected.jsonl`, the latest round's selection, and
  `<owner>-<repo>-<pr>-<kind>.json`, the item a review got.
- `runs/<run-id>/`: one review run, named by its UTC start time and PR number. `run.json` is the
  work item with the run ID, merge base, cutoff, agent, and a content hash of each skill in
  `~/.claude/skills`, so a finding traces to the skill text behind it. `github.json` is the PR
  conversation the run read, missing when the fetch failed. `diff.patch` is the PR's change,
  `git diff <merge base> HEAD`, as a plain patch. `prompt.md` is the prompt the run got.
  The transcript (`transcript.jsonl` for Claude, the ATIF export `transcript.json` for Devin, with
  `devin-config.json` and `stdout.log`) and `stderr.log` are the run, `findings.json` its result
  (`findings.invalid.json` when it did not fit the schema), `failure.txt` the reason whooper-review
  gave when it stopped the run, `log.md` its evidence for each finding and the candidates it dropped,
  `verdicts.json` whooper-classify's output, and `post.json` and `post-request.json` whooper-post's.
  `drafts.json` holds the user's choices for the review: `event` (`APPROVE` or `COMMENT`),
  `message`, and `findings`, which maps a finding ID to `included` and, once edited, `text`.
  `submit-request.json` is the exact request whooper-submit last showed with `--print` or set out
  to post, and its SHA-256 is the fingerprint `--print` prints. `submit.json` is the review it
  posted: the request and GitHub's `response`, with `status` `posted`, or `posting` while gh posts.
  `asks/` holds whooper-ask's answers, `<n>.json`, each beside its run's `<n>.prompt.md`,
  transcript, and `<n>.stderr.log`, and for Devin `<n>.devin-config.json`.
  The worktrees `head/` and `base/` exist only while the run does.
- `digest.jsonl`: one line per finding that waits for the user, with its run.
