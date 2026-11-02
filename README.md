# Whooper

Whooper finds the pull requests one GitHub user is expected to review or re-check, and reviews
them with Claude Code runs. It reads the user's review requests on GitHub and review asks in one
Slack channel, reviews each PR in a sandboxed run, sorts the findings by a strict rule, writes the
GitHub review it would post, and sends a summary to a private Slack channel. Live posting to GitHub
is not turned on yet, and nothing runs on a schedule.

## Set it up

Whooper needs `gh` and `claude` logged in (the claude.ai Slack connector must be connected), `jq`,
`git`, `docker`, and Python 3.11 or later as `python3`.

1. Copy `examples/config.example.toml` to `~/.config/whooper/config.toml` and fill it in. Everything
   that identifies you, your company, or your repos lives there: your GitHub login and name, the
   GitHub orgs, the Slack workspace and channels, the bots on both sides, the models, the skills a
   review loads, and one table per repo.
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

```sh
bin/whooper-fetch   # GitHub reads -> <state>/poll/github.json
bin/whooper-poll    # a Haiku run reads Slack -> <state>/poll/<UTC start time>.json, and prints that path
bin/whooper-select  # prints the newest poll's items that no record marks as handled, one per line
```

To review one work item, save it as a file and run:

```sh
bin/whooper-review <work-item.json> --dry-run [--as-of <ISO 8601 time>]   # prints the run directory
bin/whooper-classify <run directory>                                       # verdicts, digest, record
bin/whooper-post <run id> --print     # writes the review it would post to post-request.json
bin/whooper-summary <run id> --print  # prints the summary; without --print, sends it to the summary channel
```

`--as-of` replays a past review: `github.json` then holds only what people could read on the PR
before that time.

whooper-post re-reads the PR first. It posts nothing when the head moved, and it moves to the
digest any finding whose line got a new review comment since the run started, or whose draft
@-mentions someone or links outside the configured orgs. Live posting is not built yet.
whooper-summary sends through a Haiku run whose only Slack call, `slack_schedule_message`,
`hooks/slack-guard.sh` pins to the composed text, channel, and time.

Tests: `python3 -I -m unittest discover -s lib`. They run on a made-up config and never read yours.

## What counts as work

`lib/whooper.py` applies the rules. `prompts/poll.md` is the only judgment, and it covers Slack
posts only. Only PRs in the configured GitHub orgs count. Each item is one PR and one kind:

- **first_review**: a GitHub review request names the user and the user has not reviewed the PR,
  or a post in the ask channel asks the user for a review. A post that does not mention the user
  counts only while it has no :eyes: and no one but bots and the PR author has reviewed the PR.
- **recheck**: the user is requested by name again after reviewing, a post that mentions the user
  asks about a PR the user already reviewed, or the user reviewed the PR without approving, every
  thread the user started has an author reply or is resolved, and the head moved or the author
  replied since the user's latest review.

A PENDING review or a thread reply does not count as reviewing. Slack text is untrusted: an ask
must name a post the Haiku run actually read that no bot wrote, and only plain
`https://github.com/<org>/<repo>/pull/<n>` URLs in a configured org that appear in that post reach
`gh`. `lib/whooper.py` reads whether a post mentions the user and has :eyes: from the post itself.
It fails the poll, so the cursor stays, when the run read the channel with any argument besides
`channel_id`, `oldest`, `limit`, a `cursor` from an earlier page, and the default
`response_format`, or when the run left out a post by someone else that mentions the user next to
a PR link.

## How a review run works

whooper-review takes the PR's lock, then fetches with the user's login everything the run will
read: the head and the merge base of the head and `base_ref`, checked out as two worktrees, and the
PR's conversation as `github.json`. Then it runs the repo's setup script, if the config names one,
which may add variables to the run's environment and prints a paragraph for the prompt. Then a
Claude run follows `prompts/review.md`, filled in from the config, from the head worktree and
returns `schemas/findings.json`.

The run gets `GH_TOKEN=disabled`, so `gh` and git's `gh` credential helper fail, and no MCP
servers, so it has no Slack tools. It loads user settings only (`--setting-sources user`), so the
PR's `.claude/` settings, hooks, and skills do not load. `hooks/tool-use-id.sh` shows it the
tool-use ID of each Bash call, which a finding cites as proof. A run can still write outside its
directory, for example to language package caches.

whooper-classify marks a finding `auto_post` only when every rule v1 answer is true, its line is in
the diff, `github.json` was fetched, and the run's main session, not a subagent, backs it with two
Bash calls: `head_run` printed `observed`, and `base_run` ran the same command with the head
worktree's path replaced by the base worktree's, succeeded, and did not print `observed` in an
output the transcript holds in full. Every nit is `shadow_nit`, and everything else is `digest`. A
skipped run gets no verdicts.

## Approvals

The poll's Haiku run sees only ToolSearch and the Slack connector's tools, may call only ToolSearch
and `slack_read_channel`, and has every Slack write tool denied by name. Its hooks (`whooper.py
poll-guard`) record each read and block its answer until it has read the channel to the last
page, because a run can otherwise answer without reading. The user's own hooks run in it too. Review runs have no GitHub
login and no Slack tools, and whooper-review refuses to run without `--dry-run`. The summary run
may call only `slack_schedule_message`, and `hooks/slack-guard.sh` blocks any other Slack call,
any other channel, a thread, a changed text or time, and a second summary.

## Stop it

Nothing is scheduled. Interrupt a running script with Ctrl-C, or stop a review with
`kill $(cat ~/.local/state/whooper/locks/*/pid)`. An interrupted review runs the teardown script,
removes its worktrees and lock, and counts as a failure in its record. The Slack cursor moves only
after a poll writes its output, so an interrupted poll is simply repeated.

## Files under the state directory

- `poll/github.json`: whooper-fetch's latest output, one entry per PR as `queries/pr.graphql`
  returns it.
- `poll/<time>.json`: a poll's work items. `poll/<time>.transcript.jsonl` is its Haiku run.
- `poll/cursor`: the Unix time the next poll reads Slack from, which is the last successful poll's
  start time. Without it a poll reads the last 24 hours.
- `prs/<owner>-<repo>-<pr>/<head>.<kind>.json`: a record of work on one head:
  `{"status": "running" | "done" | "failed" | "stale", "failures": <n>, "started_at": "<ISO 8601 time>"}`.
  whooper-review writes running when it starts and failed, with one more failure, when it stops
  without findings. whooper-classify writes done, and whooper-post writes stale when the head moved.
  whooper-select drops an item whose record is done or running or has failed 3 times, and a
  re-check of a PR that already has 3 re-check records for other heads started today (local time).
- `locks/<owner>-<repo>-<pr>/pid`: the process ID of the review that holds the PR. A lock whose
  process is gone is stale, and the next review takes it.
- `runs/<run-id>/`: one review run, named by its UTC start time and PR number. `run.json` is the
  work item with the run ID, merge base, and cutoff. `github.json` is the PR conversation the run
  read, missing when the fetch failed. `transcript.jsonl` and `stderr.log` are the run,
  `findings.json` its result, `log.md` its evidence for each finding and the candidates it dropped,
  `verdicts.json` whooper-classify's output, and `post.json` and `post-request.json` whooper-post's.
  The worktrees `head/` and `base/` exist only while the run does.
- `digest.jsonl`: one line per finding that waits for the user, with its run.
