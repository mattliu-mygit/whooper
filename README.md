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
[gopls](https://go.dev/gopls/) is optional: with it, a review links changed Go code to its callers with
Go's own tools rather than by name search ([How a review run works](#how-a-review-run-works)). So is
[gremlins](https://github.com/go-gremlins/gremlins) (`go install
github.com/go-gremlins/gremlins/cmd/gremlins@v0.6.0`): with it, a review checks whether the tests notice a
small change to the changed Go lines they run. So is
the `go` command, with which a review measures which changed Go lines no test ran and runs the example
inputs its run chose, at the head and at the merge base, and `node`, with which it measures the changed
JavaScript and TypeScript lines of a package whose tests run with Vitest or Jest, and links changed
JavaScript and TypeScript code to its callers with the TypeScript 5 or 6 the package installed rather than by
name search. A package's TypeScript 7 links them without `node`, through its own language server.

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
finishes or fails, unless you skipped it ([Skip a review](#skip-a-review)), whooper-round runs
`[schedule] notify`, if the config names one, as
`<notify> <title> <message>`. When a review stops at a cause that stops every review, such as a
setup script that cannot reach Docker ([See reviews](#see-reviews)), the round starts none of its
other reviews: each waits for the next round, which reviews it again. Only one copy runs at a time:
whooper-run holds the lock `<state>/run.lock` and refuses to start while another whooper-run holds
it. To run it in the background with a log:

```sh
bin/whooper-run >> ~/.local/state/whooper/run.log 2>&1 &
```

Four commands work from a terminal and from Prompting Time, which runs them and watches the state
directory for changes:

```sh
bin/whooper-round          # runs a round now, even while paused
bin/whooper-pause          # whooper-run skips each round it comes to, and logs that it did
bin/whooper-resume         # ends the pause
bin/whooper-status --json  # what runs, whether Whooper is paused, the next and last rounds, the last 50 rounds, and the reviews
```

A pause lasts until whooper-resume ends it or whooper-run starts again: whooper-run deletes the
pause when it starts, so when Prompting Time opens and starts whooper-run, Whooper runs.

Only one round runs at a time: whooper-round holds the lock `<state>/round.lock`, and another
whooper-round prints "a round is already running" and exits 1. whooper-status makes no network
calls, and its header lists the fields it prints. [See reviews](#see-reviews) says how it lists the
reviews.

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
failure it prints why and leaves the file as it was. It reads and rewrites the file under a lock on
its folder, so of two saves at once neither drops the other's change. Nothing needs a restart:
whooper-run reads `[schedule]` each time it sets the next round, so a round already set keeps its
time, and whooper-poll, whooper-review, and whooper-ask read the config as each starts, so a run
already going keeps the old settings. Prompting Time's settings form and the Settings tab of
[Whooper's page](#whoopers-page) both edit these settings with this command.

## See reviews

`bin/whooper-status --json` lists, as `reviews`, the review runs in `<state>/runs` that started in
the last 30 days, newest first: each one's PR, the PR's title and author, and times, how it went, its
verdict and number of findings, whether it is safe to approve, what whooper-submit posted, and
whether it needs you.

Beside those fields, whooper-status prints the same status in Prompting Time's status format 2,
which `schemas/prompting-time-status.json` describes, a copy of Prompting Time's
`docs/plugin-status-v2.schema.json`: `agent`, Whooper's row in Prompting Time, such as `Running ·
next poll 3:45 PM`; `blockers`, each cause that stops every review; and `items`, the newest review of
each PR you did not remove, at most 50, with `truncated` saying whether any were left out. Each item
names its group among those `prompting-time-plugin.json` declares, the ones Prompting Time showed
Whooper's reviews in before: Ready to approve, Needs a look, and Review failed, which wait on you,
then Reviewing, Waiting, which holds the reviews a cause that stops every review stopped, and Done.
For example, a review that ended with an `approve_once_ci_passes` verdict and one nit is in group
`ready` with a green dot, the line `example-repo#12 · @alice · 1 finding`, the route
`/reviews/<run-id>`, where Whooper's page shows it, and the row actions retry, skip, and remove.
Prompting Time draws Whooper's row and its menu bar from `agent`, lists the blockers and the items of
the groups that wait on you in its Needs you inbox, and reaches every item through Whooper's page and
Command-K. The format 1 fields, which the schema allows beside format 2's, stay until Prompting Time
reads format 2 alone; whooper-status's header lists each field of both.

```sh
bin/whooper-show <run-id> --json   # one review in full, for its review screen
```

whooper-show prints the run's `run.json`, the PR's title and author, its outcome, `findings.json`,
the drafts whooper-draft prints for it, `diff.patch`, `failure.txt`, each of whooper-ask's answers in
number order, and the review whooper-submit posted. `overview` is the overview the run wrote in
`overview.json` ([How a review run works](#how-a-review-run-works)), null while the review runs and
when the file is missing, cannot be read, or breaks a rule whooper-review checks it against, as for a
review stopped before whooper-review checked it. It reads files only: the run directory, the review
locks, the files that name the PR's newest head, which whooper-status reads too, and the repo's
clone. It refuses a run ID that names no run directory. Reviews kept no `diff.patch` before
whooper-review saved one, and for such a run whooper-show has git rebuild the diff from the repo's
clone in the cache: `git diff` between the merge base and the head `run.json` records, with the arguments
whooper-review's diff uses and the head's `.gitattributes`. git fetches nothing and writes nothing
there, so the diff is null when the clone lacks either commit or one of their files. Prompting Time
runs it as the manifest's `show-review` action, which puts `--json` before the run ID; either order
works. The headers of both commands list each field.

A review's outcome is `running` while a live whooper-review holds the PR's lock for the run, the
rule whooper-ask follows; `waiting` for a review whooper-round did not start, because an earlier
review in its round stopped at a cause that stops every review; `reviewed` once the review left
findings that fit `schemas/findings.json` and no `failure.txt`; and `failed` otherwise, as for a run
directory Whooper cannot read. Only
`run.json` names the repo, so the repo is null for a review whooper-review stopped before that file
was whole, such as when its fetch failed, and in a review's first seconds, before it writes the
file; the PR number and start come from the run ID. The title and author come from `github.json`,
the PR conversation the review read, since `run.json` has neither, and are null when the review's
GitHub fetch failed or the file cannot be read. A review needs you once it ends, reviewed or failed,
until whooper-submit posts it, you skip it ([Skip a review](#skip-a-review)) or remove it
([Remove a review](#remove-a-review)), or a newer review of the same PR starts, which supersedes it,
even one that waits; a review that waits needs no one. A
`submit.json` that still says `posting` does not count as posted, since its review may or may not be
on GitHub; whooper-submit then asks you to check the PR. The verdict, the findings, and the drafts
come only from findings that fit the schema, and none while the review runs, since a Devin review
writes `findings.json` itself and may still change it. Nothing records when a review ended, so its
end is when whooper-review saved `failure.txt`, or else when the run wrote `findings.json`, and a
posted review's time is when whooper-submit wrote GitHub's answer to `submit.json`.

Each review also has `head_sha`, the commit it reviewed, from `run.json`, and `pr_head_sha`, the
newest head of its PR that Whooper knows: of the heads its latest poll saw, the recheck list holds,
and the PR's records name, the one it learned last. The two differ once the PR has new commits since
the review, and `pr_head_sha` is null when nothing names the PR. whooper-show gives `head_sha` too.

whooper-show also reads the PR's history from the repo's clone in the cache, where git fetches
nothing and writes nothing. `commits_since_review` lists the commits between `head_sha` and the
newest head Whooper knows, newest first, each with its SHA, title, author, time, and the files it
changed; a merge, such as of the base branch into the PR, is one commit with the files it brought.
It is `[]` when the review read the newest head, and null when either head is unknown or the clone
lacks either commit, as when no review fetched the new head yet. `co_change` lists the files outside
the PR that changed together with one of its files in at least 60% of that file's commits, and in at
least 3, in the 90 days before the review started, from the history the merge base ends, merges left
out: each with the PR's file as `partner`, that share as `ratio`, and how many as `commits`, at most
5, highest ratio first. For example, a file that changed in 7 of `load.go`'s 9 commits lists as ratio
0.78 and commits 7. It is `[]` without the diff, the merge base, or the clone.

`connections` is the `connections.json` whooper-review saved ([How a review run works](#how-a-review-run-works)):
`nodes`, each with its `id`, `name`, `kind` (`func`, `method`, `type`, `var`, or `const`), `path` and
`line`, in the head, or for removed code at the merge base, `change` (`added`, `changed`, `removed`, or
`unchanged`, for a caller the diff leaves alone), and `lines_changed`; `edges` from the code that calls
or uses a node to it, by node ID, each with `kind` (`calls` or `uses`) and `found_by` (`gopls`,
`typescript`, or `name_search`); and `limits`. For example, a PR that changes `Total` in `prices/prices.go`, which
`Report` in `shop/report.go` calls, has the nodes `prices/prices.go:Total`, changed, and
`shop/report.go:Report`, unchanged, and an edge from Report to Total that calls, found by gopls. It is
null while the review runs and when the file is missing or cannot be read, as for a review from before
whooper-review saved it.

`test_gaps` is the `test_gaps.json` whooper-review saved before the review run started
([How a review run works](#how-a-review-run-works)): which of the lines the diff adds to Go, JavaScript,
and TypeScript code no test ran. `measured` is false when Whooper could measure nothing, with `reason`
saying why in plain words, and `reason` is null otherwise; `tool` lists the commands it ran, go test and
each package's Vitest or Jest, joined by `; `, each that runs in a folder of its own in parentheses after
a `cd`; `lines` lists each changed line no test ran, as `path`, `line`, and `status` `not_run`, and, after the
review run, each changed Go line tests ran that Whooper changed with gremlins, with `status` `survived` when
every test of its package still passed and `caught` when one failed, and `change` saying what changed, such as
`` `>` to `>=` ``, at most 7 such lines a file, survived first; gremlins does not say which test caught a change; `summary` counts the changed lines that hold statements
(`changed_lines`) and how many of them a test ran (`run`) or not (`not_run`), all 0 when nothing was
measured, and, once the mutation step ran, how many lines it lists as `survived` and `caught`; and `limits` says what the measure can miss, such as that a test can run a line without checking
what it does. For example, a PR that adds `if n > 100 { n = 100 }` to `Clamp`, whose only test calls
`Clamp(50)`, lists the line `n = 100` as not run, and counts the `if` line as run. `examples` is the
`examples.json` whooper-review saved after the run: what the Go functions the run chose return for the
inputs it chose. `functions` lists each function as `name`, `path`, and `line`, in the head; each of `rows`
has the `function`, the `input`, `before` and `after`, what the run's test printed at the merge base and at
the head, or null, a `status` (`same`, `different`, `flaky` when the two runs at the merge base printed
different results, or `error` when a run printed none), a `label` (`ran_base_and_head`, `ran_head_only` for
a function the PR adds, or `not_compared`), and a `note` that says why, or null; `limits` says what the
table can miss. For the same PR, the input `Clamp(101)` reads before `101`, after `100`, `different`, and
`ran_base_and_head`. Only what the test printed fills a row, never what the run expected. Each is null
while the review runs and when its file is missing or cannot be read; `test_gaps` is also null for a PR
that changes no Go, JavaScript, or TypeScript code, and `examples` for a run that chose no examples.

`safe_to_approve` is true exactly when whooper-submit would approve a review without
`--approve-anyway` ([Submit a review](#submit-a-review)): its outcome is `reviewed`, its verdict is
`approve_once_ci_passes`, every finding is a nit, and it did not skip the PR. It is false for any
other review, as for one that runs, which whooper-submit refuses, waits, or failed, even with
findings that fit, such as a Devin review that passed its time limit after writing them.
whooper-show gives it too.

A failed review's `cause` says what stopped it, as `id`, a short `summary`, and `scope`: `all` when
the cause stops every review until you fix it, and `pr` when it stops only this PR's:

| `id` | `summary` | `scope` | When |
| --- | --- | --- | --- |
| `docker_unavailable` | Docker isn't running | `all` | The repo's setup script could not reach Docker, such as `Cannot connect to the Docker daemon`. |
| `setup_failed` | The repo's setup script failed | `all` | The setup script failed any other way. |
| `gh_signed_out` | gh isn't signed in to GitHub | `all` | GitHub refused the fetch of the PR's commits for want of a login. |
| `repo_not_configured` | Whooper isn't set up for this repo | `pr` | The cache has no clone of the repo. |
| `agent_no_findings` | The review agent left no usable findings | `pr` | The agent ended without `findings.json`, or with findings that do not fit the schema. |
| `time_limit` | The review ran past its time limit | `pr` | The run passed `[review] timeout_minutes`. |
| `unknown` | The review stopped unexpectedly | `pr` | Anything else, such as an interrupt or another failed command. |

whooper-review saves the cause in `failure.json` as it stops the review. A review from before then
gets the cause its `failure.txt` names by the wording whooper-review gave each step, and `unknown`
when it names none or the review has no `failure.txt`, as after a `kill -9`.

After a review stops at a cause whose scope is `all`, whooper-round starts none of the round's other
reviews: each gets a run directory of its own, outcome `waiting`, with that cause as its `cause`, and
goes on the recheck list, so the next round reviews it again whatever brought it in. A later round
that waits again reuses the run of a PR whose newest review already waits at the same head, unless
you removed that review. While the newest review that ended or waits stopped, or waits, at such a
cause, whooper-status's `blocked` names it, with `since`, when the first of the reviews in a row at
that cause ended, and `waiting`, how many PRs' newest review waits, leaving out one you removed,
which the next round does not review. It is null once a review succeeds, or stops at a cause of its
PR's own, which shows that the cause no longer holds; a review that runs has shown neither yet.

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

A run is safe to approve when it reviewed the PR, its verdict is `approve_once_ci_passes`, every
finding it has is a nit, and its review did not fail. A run that skipped the PR, as the review of a
large PR from an untagged ask does, read too little to vouch for it, so it is not safe whatever its
verdict says. Nor is a review that failed, which has a `failure.txt`, whatever findings it left,
such as a Devin review that passed its time limit after writing them. Until the user changes
anything, a safe run starts as an approval with every nit included, and any other run as a comment
with nothing included, so the user decides. A review that did not finish, whose `findings.json` is
missing or does not fit `schemas/findings.json`, such as an empty file or findings without a
verdict, has no drafts; whooper-draft names the first part that does not fit.

`bin/whooper-submit <run-id>` posts the review with the user's own gh login, as one GitHub review
at the head the run reviewed. Each comment is `AI comment (vetted)`, a blank line, and the text,
which keeps the Conventional Comments label it opens with, such as `**nit:**`; a text without one
gets its finding kind's. An approval without a message says `Pre-approving to unblock` when it
includes comments and nothing when it includes none; a message the user set posts as written. A
review that includes no comment posts its message alone: an approval with the message `lgtm` posts
an approval that says `lgtm`, and a comment review posts only what the user wrote.

Prompting Time's Submit button and Whooper's agent both run whooper-submit after Prompting Time's
confirmation, which shows the exact request that `whooper-submit <run-id> --print` writes to
`submit-request.json` without posting. `--print` also prints the request's fingerprint, the
SHA-256 of that file, as its last line, `fingerprint: <hex>`, and the submit after the
confirmation passes it back as `--expect <hex>`, so drafts changed after the confirmation post
nothing the user did not see.

whooper-submit first runs the checks that need nothing from GitHub, then re-reads the PR's head.
Each refusal posts nothing and exits with its own code, so Prompting Time can tell them apart:

| Exit | Why | What then |
| --- | --- | --- |
| 10 | The PR has new commits since the review. | The reviewed head's record says stale and the PR goes on the recheck list, as whooper-post does, so the next round reviews the new commits whatever brought the PR in; `bin/whooper-round` runs that round at once. |
| 11 | The review approves, and the run is not safe to approve. | It lists why: a review that failed and the reason in its `failure.txt`, a review the run skipped and why, the verdict and its reason, and the findings that are not nits. `--approve-anyway` approves after the user has looked. |
| 12 | An included comment or the message @-mentions someone. | It lists each mention, and `--allow-mentions` posts them. |
| 13 | The request is not the one whose fingerprint `--expect` gives, because the drafts changed since `--print` showed it. | `--print` shows the new request for another confirmation. |
| 14 | An included comment is on a line outside the PR's diff, as its finding's `in_diff` says, where GitHub takes no comment, so it would refuse the whole review. | It lists each such comment, and reads nothing from GitHub first. Leaving them out with `bin/whooper-draft` lets the review post, and the review's message can make their point. |

Any other failure exits 1, or with gh's or jq's own code, none of which is 10 to 14. A comment
someone wrote on the same line since the review, and a link outside the configured orgs, do not
stop it. It refuses a review that is still running, since a Devin review may still change its
`findings.json`, a review that did not finish, a run whose `run.json` it cannot read, a review that
includes no comment, has no message, and does not approve, and a run it already submitted, so a
review never posts twice.

## Skip a review

`bin/whooper-skip <run-id>` marks a review done without submitting it, and prints what it did: it
leaves the empty file `skipped-by-user` in the run directory, so whooper-status lists the review with
`skipped_by_user` true and `needs_you` false, and whooper-show gives `skipped_by_user` true.
`bin/whooper-skip <run-id> --undo` deletes the file. It also stops reviewing the PR. A review that
is still running stops as `kill` stops it ([Stop it](#stop-it)): whooper-review saves `the review
was interrupted`, and whooper-round sends no notice for it. While the run is its PR's newest,
whooper-select starts no other review of the PR, whatever asks for one, such as new commits or a new
Slack ask, until `--undo` or `bin/whooper-retry`; a recheck-list item for the PR stays on the list
meanwhile. For example, after you skip the review of #12 at head `abc1234`, the author pushes
`def5678`, and every round drops #12 until you undo the skip, after which the next round reviews
`def5678`. Nothing else changes: nothing posts to GitHub, and the drafts and records stay, so a
skipped review that finished can still be submitted. It refuses a run ID that names no run
directory. Prompting Time runs it as the manifest's `skip` action, adding the run ID, and `--undo`
after it to undo; a review's row menu there adds `--item` before the run ID, which works too.

## Remove a review

`bin/whooper-remove <run-id>` takes a review out of your lists, and prints what it did: it leaves the
empty file `removed-by-user` in the run directory, so whooper-status still lists the review, with
`removed_by_user` true and `needs_you` false, and whooper-show gives `removed_by_user` true. The run's
files and the records stay. `bin/whooper-remove <run-id> --undo` deletes the file.

Whooper does not retry a removed review on its own. While the run is its PR's newest, whooper-select
drops each item that asks for the same review again, saying `removed by the user`, and takes the PR
off the recheck list, so a review that waits or failed is not reviewed again. The same review means
the run's head and kind, and no Slack ask but the run's own. A new review request brings the PR back:
a new Slack post that asks for it, new commits, or another kind of work, as when you reviewed the PR
and are requested again. For example, after you remove a failed review of #12 at head `abc1234` that
a GitHub review request brought in, every poll still lists #12 at `abc1234` while the request stands,
and whooper-select drops it; once the author pushes `def5678`, the poll lists #12 at the new head and
the round reviews it. A request on GitHub made again at the same head reads like the one before, so
it does not count; `bin/whooper-retry` brings the PR back whatever brought it in, and unmarks the run
it retries. A new request that waits for the next round gets a run of its own rather than the
removed run that waits. `--undo` puts a review that waits back on the recheck list when whooper-select
took it off, so the next round reviews it again.

It refuses a run ID that names no run directory and a review that is still running. Prompting Time
runs it as the manifest's `remove` action, adding the run ID, and `--undo` after it to undo; a
review's row menu there adds `--item` before the run ID, which works too.

## Ask about a review

`bin/whooper-ask <run-id> '<question>' [--finding <finding-id>]` answers a question about a review
run, such as "Why is it bad to have invisible characters?", or "Why did this review fail?" about one
that did not finish, and prints the answer. Prompting Time's "Ask about this" runs it with the
finding. A new run of the agent that did the review answers, with the review's skills, since Whooper
ends each review's session. A review whooper-review stopped before it wrote `run.json`, such as
when the fetch failed or the skills could not be hashed, recorded no agent, so the agent
`[jobs] review` names answers about it; an empty or unreadable `run.json` counts as none. Its
prompt, `prompts/ask.md` filled in, names the question, the finding, and the path of each file of
the review's record the run directory has: first `failure.txt`, the reason whooper-review gave when
it stopped the review, then `log.md`, `findings.json`, `overview.json`, `overview.invalid.json`,
`diff.patch`, `connections.json`, `test_gaps.json`, `examples.json`, the transcript, `github.json`,
`run.json`, `prompt.md`, `verdicts.json`, `post.json`,
`drafts.json`, `submit.json`, `summary.md`, and earlier asks. For a review that did not finish,
whose `findings.json` is missing or holds no findings Whooper can read, such as an empty file, it
also names the review agent's `stderr.log` and `stdout.log`, which may say why, and
`findings.invalid.json`, the findings that did not fit the schema. The PR's code is gone by then,
since whooper-review removes the worktrees, so the run answers from the record, where `diff.patch`
holds the PR's change. [Approvals](#approvals) says what the run may do.

whooper-ask refuses a review that is still running, and says so: one whose whooper-review holds the
PR's lock with the run's ID in it. A lock whose process is gone, as after a `kill -9`, does not
count, so a review killed that way can be asked about.

whooper-ask saves each answer in the run directory as `asks/<n>.json`, numbered in order from 1:
`question`, `finding` (its ID or null), `answer`, `agent`, `started_at` and `ended_at` in UTC, and
`transcript`, the transcript's path. `asks/<n>.prompt.md` is the run's prompt. A run that passes
`[ask] timeout_minutes` (10 by default) is stopped with everything it started and, like a run that
fails, saves no answer; its number is not used again.

## Retry a review

`bin/whooper-retry <run-id>` queues another review of a run's PR, such as one whose setup script
failed while Docker was not running, and prints what it queued. It reads the PR's head with your gh
login, puts the run's work item at that head on the recheck list, and marks that head's record
`retry`. The next round reviews it whatever earlier reviews at that head did: you asked for it, so
neither the limit of 3 failures at a head nor the limit of 3 re-checks a day holds it back.
`bin/whooper-round` runs that round now. When the poll lists the PR at that head, as while its
review request stands, the round reviews the poll's item instead, once. Once the new review starts,
its record counts toward both limits as usual, and as the PR's newest run it supersedes the run you
retried, which no longer needs you. It unmarks a run you removed ([Remove a review](#remove-a-review)).
whooper-retry refuses a run ID that names no run directory, a review that is still running, and a run
whose `run.json` it cannot read, since only that file names the PR's repo. Prompting Time runs it as
the manifest's `retry` action, adding the run ID; a review's row menu there adds `--item` before it.

## Whooper in Prompting Time

Prompting Time reads `prompting-time-plugin.json`, which fits `schemas/prompting-time-plugin.json`:
Whooper's name and version, the command it starts (`bin/whooper-run`), the programs it checks for
first (`gh`, slackdump, `jq`, uv, and the CLI of each agent the settings' `[jobs]` name), the page,
the settings schema (`schemas/settings.json`), the groups whooper-status's status format 2 puts
its items in and the actions their rows offer ([See reviews](#see-reviews)), and the commands it runs
as actions. An action with `confirm: true` waits for the user's yes in Prompting Time; only `submit`
has it, since only whooper-submit writes outside this Mac. The tests check the manifest against the schema and the
checkout.

### Whooper's page

`page/` is the page Prompting Time shows for Whooper and for one of its reviews or blockers. While
it shows, Prompting Time folds its own sidebar to a rail, so the page draws its own: Whooper's name
and agent row, such as `Paused` or `Running · next poll 3:45 PM`, then four tabs, each at its route.
Reviews, at `/`, which Prompting Time opens when you click Whooper's name, lists the newest review of
each pull request in the groups status format 2 puts them in, those that wait on you first, with a
search by title, repo, PR number, or author, and a chip per group with its count; its tab counts the
reviews waiting on you, and its header says that and how many it lists. Each row opens its
review, and its ⋯ menu opens it, skips it or undoes the skip, removes it from the list, with Undo, or
retries it. Past reviews, at `/past`, lists the reviews you removed, which its ⋯ menu restores, and
earlier reviews of a pull request that a newer one replaced. Causes, at `/causes`, lists each cause
that stops every review with the reviews it holds back. In Reviews and Past reviews, j and k select
the next and previous row, around the ends, as they step through a walkthrough, and Enter opens it;
Escape closes any open menu. While a review shows, the tabs fold to a narrow rail, as Prompting
Time's All work folds while a plugin shows: » and "Whooper · 2 waiting on you", or only "Whooper"
when nothing waits on you, turned 90°, so only the walkthrough's own navigator stays beside the
review. A click on the rail unfolds the tabs with the review still open, « at their top folds them
again, and Back to the list, like any list, shows them; each review opens with them folded. Settings, at `/settings`, edits the settings `schemas/settings.json`
describes, drawn with the settings-form logic `@prompting-time/ui` exports for Prompting Time's own
form, read with the `get-settings` action and saved with `save-settings`, the same two Prompting
Time's settings form runs, so both edit the same lines of `config.toml` and Prompting
Time's check for each agent's CLI follows the saved `[jobs]`. Each field says why its value would be
refused as you type: what the schema says, start and end as 24-hour times with the end after the
start, at least one day, and a value for a setting the config has, since whooper-settings can't take
one out. Save sends only the changed settings, shows whooper-settings' reason when it refuses them,
and then says when the change applies ([Change settings](#change-settings)). Each tab's header has
Pause or Resume and Poll now; Start and Stop stay in Prompting Time, which runs `bin/whooper-run`. A review Prompting Time opens marks the tab whose list holds it. A review shows Back
to the list, which keeps the list's search and filter, then its header, with its versions, Skip, which also stops a review
that runs, and Undo, Remove and Restore, Rerun review, and a banner for new commits since the
review; then the review's diff with Whooper's findings as draft comments under their lines, Include
in review, Edit, Ask about this, Discard, and one bar that submits the review. When whooper-submit
refuses the submit because the run is not safe to approve (exit 11) or the review @-mentions someone
(exit 12), the bar offers Approve anyway or Send with the mentions, which submit again with
`--approve-anyway` or `--allow-mentions`, and Prompting Time confirms that submit too. The page
tells the refusals apart by the exit code Prompting Time passes back, and shows whooper-submit's
stderr as the reason. Only such a press sends a mention: whooper-post, which Whooper runs on its
own, still holds back each comment that @-mentions someone. When an included comment sits on a line
outside the PR's diff (exit 14), no flag helps: the bar says "Some included comments sit on lines
outside this PR's change. Leave them out or make the point in the review's message." and names each
such comment as whooper-submit does. After you press an action button (Skip,
Remove, Rerun review, Pause, Resume, Submit review, the two refusal buttons, or Ask), the page disables
the action buttons until 600 ms after the action's reply, and 600 ms more when Prompting Time opens
another item in that time, so a second click can't act on the item Prompting Time moved on to.
While Ask waits for Whooper's agent, which can take a long time, only the Ask buttons are disabled;
the 600 ms hold of every button starts at its reply. Poll now, whose round can take minutes,
disables only the Poll now buttons, which say Polling… while whooper-status says a round runs. A
review that runs, waits, or failed shows why instead, with whooper-review's reason, the answers to
earlier questions, and Ask why.

A finished review that wrote an overview opens on its walkthrough instead, and the header's
Walkthrough | Diff switches to the diff above. The verdict banner comes first, green when the run
is safe to approve, orange when it needs a look, red on a failed or waiting review's screen, then
"Changes since you last looked", one folded line with the commits since the review, which × hides
for that version of the pull request, with Undo. The walkthrough then reads top to bottom: what the
PR does, before and after; the places to look, each a numbered mark in the code that j and k step
through; the calls before and after; what Whooper checked and didn't, and the files that usually
change with these; then the change layer by layer, each folded once you mark it read. The decision
waits as Decide… in the corner until you click it or reach the end, and offers Comment or Approve,
the review's message, and Submit review, with the same refusals as the diff's bar. In both bars the
message saves with whooper-draft when its box loses focus, and Submit review saves it first, so
Prompting Time's confirmation shows it: an approval with no comments may say only `lgtm`, and a
comment review with no comments posts its message alone. A comment review with neither can't be
submitted, and the decision bar says why: "Add a comment or a message to submit a comment review".
The header's Views menu turns
each extra view off or on: how the changed code connects, changes no test would catch, and example
inputs before and after each show a summary where its question comes up and open in full in the
walkthrough's place. The page remembers what you last opened, hid, marked read, and turned off only
while Prompting Time keeps it loaded: its sandbox gives it no localStorage.

It is a Solid 2.0 app built on Prompting Time's shared UI library,
`@prompting-time/ui`. Prompting Time shows it in an iframe sandboxed to scripts only, so the page reaches nothing but its host, through page protocol 2
in `page/src/protocol.ts`: it says `ready`, Prompting Time sends `open` with the route the user
opened, `/`, or the route of an item or blocker, `/reviews/<run-id>` or `/causes/<cause-id>` ([See
reviews](#see-reviews)); `/past`, `/causes`, and `/settings` the page opens itself. The page reads and changes everything with `call`s of the manifest's
actions, such as `status`, `show-review`, `draft`, `ask`, `skip`, `retry`, `get-settings`,
`save-settings`, and `submit`, which
Prompting Time confirms before `whooper-submit` posts. When files in the state folder change,
Prompting Time sends `changed` with their paths, and the page reads the status again, and the open
review when one of its run's files changed: only its drafts when `drafts.json` alone did, which
keeps the diff as it is.

The page needs Node 22.12 or later. In `page/`:

```sh
npm ci          # exact versions from package-lock.json, as the registry stood on .npmrc's date
npm test        # the page's tests, against the fake host in page/src/fakeHost.ts
npm run dev     # http://localhost:5175/dev/ shows the page in a sandboxed iframe with the fake host
npm run build   # page/dist, which the repo does not track
```

The fake host runs the page's calls on invented reviews and settings in `page/src/fixtures.ts` the
way Whooper's commands would, keeping saved settings in memory, and reaches no Whooper, GitHub, or
model; `/dev/` opens the Reviews tab, `/dev/?review=<run id>` a review, and `/dev/?route=<route>`
any route, such as `/past`, `/settings`, or `/causes/docker_unavailable`.
The page installs `@prompting-time/ui` from its built tarball, `prompting-time-ui-0.1.0.tgz`, which
Prompting Time's `scripts/pack-ui.sh` writes. Until Prompting Time attaches that tarball to a GitHub
release, the page installs it from a Prompting Time checkout beside this one,
`../../repos/prompting-time-followups/packages/ui/prompting-time-ui-0.1.0.tgz` from `page/`; it then
switches to the release's URL. Either way `package-lock.json` pins the tarball's sha512, so `npm ci`
refuses a different build. The tarball bundles everything but Solid, which the page supplies; the
page also installs `@pierre/diffs`, at the tarball's version, only for the types of `Diff`'s props.
Versions are exact and resolved as of `.npmrc`'s `before` date, at least a week before they were
pinned, and no install script runs.

### Releases

The repo tracks no build output. Pushing a version tag, `v` and the manifest's version, runs
`.github/workflows/release.yml`, which builds the release archive with `scripts/build-release` and
attaches it and its SHA-256 to the tag's GitHub release; Prompting Time installs that archive,
pinned by the tag and the checksum. `scripts/build-release [<tag>]` builds the same archive locally:
`release/whooper-<version>.tar.gz`, HEAD's files plus `page/dist`, and its `.sha256`. Until
the page installs `@prompting-time/ui` from a release URL, the workflow stops at its first check,
since the runner has no Prompting Time checkout to install it from.

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
  replied since the user's latest review. whooper-review reviews first reviews only, so for now
  whooper-select drops each re-check, and a re-check does not take the PR off the recheck list.

When whooper-post or whooper-submit finds that a PR's head moved since its review, it posts
nothing and puts the PR on the recheck list. The next round's whooper-select adds it to the work at
the new head, with the kind and source it had, so the new commits get a review even when only a
Slack ask brought the PR in, which a poll reads once. Like a re-check, it waits for the next day
once the PR has 3 records of its kind for other heads started today. `bin/whooper-retry` puts a PR
on the list too, and no limit holds that review back ([Retry a review](#retry-a-review)). So does
whooper-round, for a review that waits for the next round after another review in the round stopped
at a cause that stops every review, unless the list holds the PR already. Once you remove a PR's
newest review, whooper-select takes the PR off the list and leaves it out of the work until a new
request for it comes in ([Remove a review](#remove-a-review)).

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
patch, as git's defaults print it whatever your git config says: no external diff tool, textconv
filter, or color, `a/` and `b/` before the paths, a submodule's commit lines rather than its log,
hunks of 3 unchanged lines around each change, and a new one wherever more than 6 unchanged lines
separate two changes, from the myers algorithm with the indent heuristic, a blank line of context
with its leading space, renames found but not copies, among at most 1,000 files, no order file,
paths with unusual characters quoted (`core.quotePath`), each file up to 512 MiB compared as text
(`core.bigFileThreshold`), nothing from your own attributes file (`core.attributesFile`), such as a
hunk-header pattern, each changed submodule shown (`diff.ignoreSubmodules`), and object names of at
least 12 digits. git's default for those, `core.abbrev=auto`, lengthens names as the cache's clone
grows, while 12 digits stay put. The run's own git diff, and the one gremlins runs in the mutation
step below, take the same settings from config entries their environment gives git
(`GIT_CONFIG_COUNT`), after any your environment already holds, with `color.diff=never` for no
color and `diff.relative=false`, which keeps a git diff the run starts in a subfolder, as after a
probe's `cd`, to the whole change, so the run sees the hunks `diff.patch` shows, and GitHub takes
comments in. git takes an external diff tool from `GIT_EXTERNAL_DIFF`, and the unchanged lines
around each change from `GIT_DIFF_OPTS`, whatever its config says, the second even over `--unified`,
so whooper-review runs everything without them, as whooper-show's rebuild does. The list of
changed files the repo's setup script gets (`WHOOPER_CHANGED`) takes the same rename detection,
order, quoting, and submodules. Some of your git config still reaches those diffs, since no setting
can pin it. git fails to start an empty `diff.external` rather than turning it off, so the run's own
git diff and gremlins' still run an external diff tool your config names. The reviewed repo's own
`.gitattributes` is part of the PR, so Whooper keeps it, and with it the settings your config gives a
diff driver it names: that driver's hunk-header pattern (`xfuncname`) and `binary` reach every diff,
and its `textconv` filter, `command`, and `algorithm` the run's and gremlins'. And a submodule's own
`ignore` setting, in the repo's `.gitmodules` or your config, hides that submodule's change from all of
them alike, which `--ignore-submodules` would override for `diff.patch` alone. Then it runs the repo's
setup script, if the config names one, which may add variables to the run's environment and prints
a paragraph for the prompt. Then, when the diff adds lines to Go, JavaScript, or TypeScript code, a step
saves `test_gaps.json`: which of those lines no test ran ([See reviews](#see-reviews)). For Go, it runs
`go test` once with coverage of the packages the PR changes, their own tests only, from their module's folder, or their workspace's when
`go` finds a `go.work`, with no downloads (`GOPROXY=off GOTOOLCHAIN=local`), and takes for each block of
statements the highest count any test package gave it, since `go test` writes a block once for each test
package that covers it. A line counts when such a block covers it, and blank and comment lines, test
files, generated files, files in `testdata`, and files the build on this Mac leaves out, such as files
for Windows, do not. A test that fails still measures, but nothing is measured, and `reason` says why,
without the `go` command, when the head does not build, when the step passes 10 minutes, and when a test
binary ends without recording what it ran, as one does when a test panics, since the lines only its tests
ran would read as not run. When the diff adds lines to JavaScript or TypeScript code, the same step,
within the same 10 minutes, runs the tests of each package such a file is in, the folder of the nearest
`package.json`, once: when its `package.json` names Vitest or Jest, in its test script first and then in
its dependencies, and the setup script installed its dependencies, so that `node_modules/.bin` there or in
a folder above it holds the runner. It runs that runner from the package's folder with coverage of the
package's changed files as an LCov report (Vitest: `vitest run --coverage --coverage.reporter=lcov
--coverage.include=<file>`; Jest: `jest --coverage --coverageReporters=lcov --collectCoverageFrom=<file>`),
which also covers a changed file no test loads, with `CI=true`, which keeps both from asking questions or
writing new snapshots, and `npm_config_offline=true`, so nothing installs. A changed line counts when the
report lists it for its file, as run when its count is above 0; Vitest and Jest list a statement on the
line it starts on. Test files (`.test.` or `.spec.` names, `__tests__` and `__mocks__` folders) and
declaration files do not count. Without a `package.json`, a runner, its installed dependencies, or `node`,
when the runner writes no report, or at the time limit, that package's files are not measured, and
`limits` says why; `measured` is false only when nothing was. The facts at the end of the prompt name the file. The run may change it, so
the copy from before the run replaces it once the run ends. Then the agent that `[jobs] review` names runs from the head worktree
through its launcher, `agents/<agent>/review`. The run follows `prompts/review.md`, filled in from
the config with that agent's tool names and saved as `prompt.md` in the run directory, and leaves
`findings.json`, which must fit `schemas/findings.json`. The run also writes `overview.json` in the
run directory, which says what the PR does and where to look first, for the review screen: a
summary, why the verdict, each change a caller sees before and after, at most 5 places to read
closely with how the run knows, the change in reading order as layers, at most one flow of calls,
and what the run checked and did not. As soon as the run ends, whooper-review checks it against
`diff.patch`: it fits `schemas/overview.json`, each place's lines and each layer range lie inside one
hunk of a file the diff changes, counted in the head, or for a deleted file at the merge base, and
each file the diff shows lines of is in exactly one layer; a file whose diff shows no lines, such as
a binary file, is in none. An `overview.json` that cannot be read, is not JSON, or breaks a rule
moves to `overview.invalid.json`, with why there and on whooper-review's error output. A run that
writes its overview inside `findings.json` instead, where `schemas/findings.json` has no room for
it, would fail the review, so whooper-review first moves that overview from `findings.json` to
`overview.json`, which it then checks, or discards it when the run wrote `overview.json` too, and
says which on its error output. The overview
never changes how a review ends: without one, or with one whooper-review drops, the review goes on
as it would anyway.

Then, while the worktrees still exist, whooper-review saves `connections.json` in the run directory, how
the code the diff changes connects, for the review screen: the declarations the diff adds, changes, or
removes, such as functions, methods, types, variables, and constants, each found in the head, or a
removed one at the merge base, and the code that calls or uses each of them, one level up, found on the
same side. For Go files it asks gopls, Go's language server, when Whooper finds it on PATH or where
`go install` puts it (`go env GOBIN`, or `GOPATH/bin`). For JavaScript and TypeScript files it asks
TypeScript, the one the file's package installed: `node_modules/typescript` in the folder of the nearest
`package.json` above the file or in a folder above that, which the repo's setup script installs; at the
merge base, where setup installs nothing, the head's. For TypeScript 5 or 6, `node` runs `lib/ts-callers.mjs`
with it. TypeScript 7, written in Go, has no code `node` can load, so Whooper runs its language server,
`tsc --lsp --stdio`, from the package its installer puts beside `typescript` for this machine, such as
`@typescript/typescript-darwin-arm64`, with its automatic type acquisition, which would download type
declarations with npm, turned off. Either way, TypeScript lists each file's declarations, those at its top
level and each class's methods, such as `Cart.add`, and finds, in the project of the nearest `tsconfig.json`
or `jsconfig.json`, the code that calls a changed function or method, as TypeScript's call hierarchy lists
it, or uses any other changed declaration, as its references list them. TypeScript gets the first three
quarters of the step's time. A file whose package installed no TypeScript, or a TypeScript 7 without its
language server, or whose project TypeScript cannot load or answer for in that time, gets name search, and
`limits` says which and why. For
any other file, and for Go files without gopls, it uses name search: the definitions a pattern per
language finds, in Go, Python, JavaScript, and TypeScript, and `git grep`'s whole-word matches of each
name. A changed line counts toward the innermost declaration around it, such as a method rather than its
class. Each link names which of the three found it, and `limits` says in plain words what the graph can
miss, such as calls through an interface, which gopls leaves out, or code that only shares a name, which
name search links. All three read the reviewed commits, not the edits or probes the run left in the
worktrees, and gopls downloads nothing (`GOPROXY=off`), nor does TypeScript. The graph keeps at most 40 nodes, the changed code
first, and the step stops looking after 2 minutes, and says so in `limits`. Like the overview, it never changes how a review ends: when it fails, the review has no
`connections.json`.

After the examples step below, while the worktrees still exist, a mutation step adds to `test_gaps.json`
which changed Go lines that tests ran still pass their tests when changed. It first puts back the committed
text of each Go file the run changed in the head's worktree, and removes those it added, so gremlins reads the
reviewed commit. Then it runs `gremlins unleash --diff <merge base> --workers 2 --timeout-coefficient 10` once
in the folder of each changed Go file, but for one inside another such folder, with `GOPROXY=off
GOTOOLCHAIN=local` and the run's git settings, so gremlins finds the lines `diff.patch` adds, then
`diff.relative=true`, `diff.context=0`, and `color.diff=false`, since gremlins compares paths relative to
its folder, takes each hunk's added lines as one range, and cannot read color. gremlins runs the folder's
tests with coverage, then for each changed line they run makes one small change at a time, such as `>` to
`>=`, in a copy of the module, and runs the line's package's tests again.
Whooper keeps only lines the diff adds and `test_gaps.json` does not list as not run, a surviving change first,
one change a line, and at most 7 lines a file, and leaves out changes that ran out of time or did not compile.
It stops after 15 minutes, keeping the folders gremlins finished, and `limits` says what it did not do: without
gremlins, at the time limit, and when gremlins stops, as when a test fails before it changes anything. Like the
other steps, it never changes how a review ends.

When the PR changes Go functions whose results a caller sees and that write no data, the run also
chooses at most 3 of them and at most 12 inputs, and writes `whooper_examples_test.go`, a test that prints
one JSON line per input with what the function returned or the error, and `examples.json`, which names
the package, the functions, and the inputs and must fit `schemas/examples-request.json`. As soon as the
run ends, whooper-review moves that file to `examples.request.json`, so that only its own runs fill
`examples.json`. Then, after the connections step, a step runs the test in the package's folder once at
the head and twice at the merge base, with `GOPROXY=off GOTOOLCHAIN=local`, `-run` limited to the test,
`-count=1`, and go test's `-timeout` at 60 seconds, with go's `-overlay` adding the test and leaving out
the Go files the run edited or added in the worktrees, so each run reads the reviewed commit. It stops
after 5 minutes and saves `examples.json` ([See reviews](#see-reviews)): two runs at the merge base that
print different results make a row flaky, and a test that does not compile at the merge base, as when it
calls a function the PR adds, runs at the head only. Examples Whooper cannot read give an `examples.json`
with no rows and why in `limits`. Each step runs, as the run does, in a process group of its own, with no
GitHub login and its temporary files in the run directory, and neither changes how a review ends: when
one fails, the review has no file from it.

A run that passes
`[review] timeout_minutes` is stopped, and when whooper-review exits it stops the run and every
process the run started. When whooper-review stops a review for a reason it names, such as the time
limit, a failed setup script, or findings that do not fit the schema, it saves that reason in the
run directory as `failure.txt`; the reason a failed setup script gets ends with the last line the
script printed on stderr. A review stopped any other way gets a fixed reason there:
`the review was interrupted` after Ctrl-C or a `kill`, and, after a command that failed,
`the review stopped at an unexpected error; see whooper-review's error output`, where the
command's own message went. Beside it, `failure.json` names the cause, from the step the review
stopped at and what that step printed on stderr ([See reviews](#see-reviews) lists the causes). A
repo the config lists but the cache has no clone of fails the review once its run directory exists,
so the failure shows in whooper-status.

`agents/claude/review` runs `claude -p`, which returns the findings as structured output and writes
`log.md` and `overview.json` in the run directory, which `--add-dir` opens to its file edits, and
reports the run's cost in dollars, capped by `[agents.claude] budget_usd` when the config sets one.
`agents/devin/review` runs `devin -p` in `dangerous` permission mode, so no tool call waits for an
approval; the run writes `findings.json` and `overview.json` itself, and the cost line is in ACUs,
Devin's billing unit. Its config for the run, saved as `devin-config.json`, is your
`~/.config/devin/config.json` plus Whooper's hook and deny rules for every Slack tool, which hold in
that mode. When the run ends, the launcher deletes its Devin session with `devin rm`, so runs don't
fill Devin Desktop's session list; `transcript.json` keeps the conversation. `agents/devin/poll`
does the same for each poll.

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
any other channel, a thread, a changed text or time, and a second summary; from version 2.1.295,
Claude Code also blocks the call when the hook cannot start, crashes, or times out. Only
whooper-submit writes to GitHub, with the user's own login: the one review the user chose in a run's
`drafts.json`, and an approval of a run that is not safe to approve only after the user looked and
passed `--approve-anyway`.

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
`asks/<n>.json`, and whooper-post, whooper-submit, whooper-retry, and whooper-remove write the recheck
list's files the same way too. Commands that read such a file and write it back hold a lock on its
folder meanwhile, so of two at once neither drops the other's change: whooper-round and whooper-run
adding to `rounds.jsonl`, whooper-draft changing `drafts.json`, and whooper-select taking items off
the recheck list while the commands above write to it.

- `poll/github.json`: whooper-fetch's latest output, one entry per PR as `queries/pr.graphql`
  returns it.
- `poll/<time>.json`: a poll's work items. `poll/<time>.prompt.md`, `.answer.json`, and
  `.transcript.jsonl` (Claude) or `.transcript.json` (Devin) are its run.
- `poll/cursor`: the Unix time the next poll reads Slack from, which is the last successful poll's
  start time. Without it a poll reads the last 24 hours.
- `prs/<owner>-<repo>-<pr>/<head>.<kind>.json`: a record of work on one head:
  `{"status": "running" | "done" | "failed" | "stale" | "retry", "failures": <n>, "started_at": "<ISO 8601 time>"}`.
  whooper-review writes running when it starts and failed, with one more failure, when it stops
  without findings. whooper-classify writes done with the run's `approval`, whooper-post and
  whooper-submit write stale when the head moved, and whooper-retry writes retry, keeping the
  count, at the head the PR has now.
  whooper-select drops a re-check, which whooper-review cannot review yet, an item in a repo the
  config has no `[repos."<owner>/<repo>"]` table for, an item whose record is done or running or has
  failed 3 times, a recheck-list item of a PR that already has 3 records of its kind for other heads
  started today (local time), an item that asks again for a review you removed
  ([Remove a review](#remove-a-review)), and any item of a PR whose newest review you skipped
  ([Skip a review](#skip-a-review)). A recheck-list item dropped for the daily limit stays on the
  list for the next day. Of these, only a re-check and the missing table drop an item whose record
  says retry. A record that cannot be read counts as none.
- `rechecks/<owner>-<repo>-<pr>.json`: the recheck list: a PR whose head whooper-post or
  whooper-submit found moved since its review, as the work item for the new head, or whose review
  you asked for again with whooper-retry, as the run's work item at the head the PR has now, or whose
  review waits for the next round, as its work item, with the kind, source, and Slack ask it had. A
  later mark of the PR replaces it. whooper-select deletes it once
  the newest poll lists the PR, which then comes with the head the PR has now, once the record at
  the listed head is done or has failed 3 times, or once it asks again for a review you removed.
- `locks/<owner>-<repo>-<pr>/`: the lock of the review that holds the PR, with its process ID in
  `pid` and its run ID in `run_id`. A lock whose process is gone is stale: the next review takes
  it, and whooper-ask, whooper-status, and whooper-show no longer count its review as running. The
  next review also takes over a lock whose process is now some other program.
- `run.lock/`: the running whooper-run's lock, with its process ID in `pid`. A lock whose process is
  gone, or is now some other program, is stale, and the next whooper-run takes it over.
- `next-round`: the Unix time of the round whooper-run waits for, written before each wait and
  deleted when the round is due.
- `paused`: an empty file that whooper-pause creates and whooper-resume deletes, and whooper-run
  deletes when it starts, so a pause does not survive a restart. While it exists, whooper-run skips
  its rounds.
- `round.lock/`: the running whooper-round's lock, with its process ID in `pid` and the same stale
  rule as `run.lock/`. `round.json` holds the round's start as a Unix time, how many items it
  selected, the PR it is reviewing as `<owner>/<repo>#<number>`, and each finished review's PR and
  outcome: `reviewed`, `failed`, or `waiting`.
- `last-round.json`: the last round that ended: its start and end as Unix times, its result
  (`done`, `poll_failed`, or `interrupted`), how many items it selected, and each review's PR and
  outcome, where a review the round stopped in is `interrupted`, and one it did not start after
  another stopped at a cause that stops every review is `waiting`.
- `rounds.jsonl`: the last 50 rounds, oldest first, one JSON object a line, which whooper-round adds
  as each round ends and whooper-run as it skips one while paused: the round's start and end as Unix
  times, the end null for a skipped round; its result (`done`, `poll_failed`, `interrupted`, or
  `skipped`); how many items it selected, 0 when it ended before selecting; and why it ended so, or
  null, such as `The GitHub fetch failed` or `Whooper is paused`. whooper-status prints them, newest
  first, as `rounds`.
- `items/`: whooper-round's work items: `selected.jsonl`, the latest round's selection, and
  `<owner>-<repo>-<pr>-<kind>.json`, the item a review got.
- `runs/<run-id>/`: one review run, named by its UTC start time and PR number. `run.json` is the
  work item with the run ID, merge base, cutoff, agent, and a content hash of each skill in
  `~/.claude/skills`, so a finding traces to the skill text behind it. `github.json` is the PR
  conversation the run read, missing when the fetch failed. `diff.patch` is the PR's change,
  `git diff <merge base> HEAD`, as a plain patch. `prompt.md` is the prompt the run got.
  The transcript (`transcript.jsonl` for Claude, the ATIF export `transcript.json` for Devin, with
  `devin-config.json` and `stdout.log`) and `stderr.log` are the run, `findings.json` its result
  (`findings.invalid.json` when it did not fit the schema), `overview.json` its overview of the PR
  (`overview.invalid.json`, with why, when whooper-review dropped it), `connections.json` how the
  code the diff changes connects, `test_gaps.json` the changed lines no test ran, `examples.json` the
  results of the example inputs the run chose, which it wrote with `whooper_examples_test.go` and which
  whooper-review moved to `examples.request.json`, `failure.txt` the reason
  whooper-review gave when it stopped the run and `failure.json` its cause, `log.md` its evidence
  for each finding and the candidates it dropped,
  `verdicts.json` whooper-classify's output, and `post.json` and `post-request.json` whooper-post's.
  `drafts.json` holds the user's choices for the review: `event` (`APPROVE` or `COMMENT`),
  `message`, and `findings`, which maps a finding ID to `included` and, once edited, `text`.
  `submit-request.json` is the exact request whooper-submit last showed with `--print` or set out
  to post, and its SHA-256 is the fingerprint `--print` prints. `submit.json` is the review it
  posted: the request and GitHub's `response`, with `status` `posted`, or `posting` while gh posts.
  `asks/` holds whooper-ask's answers, `<n>.json`, each beside its run's `<n>.prompt.md`,
  transcript, and `<n>.stderr.log`, and for Devin `<n>.devin-config.json`.
  `skipped-by-user` is the empty file whooper-skip leaves when you skip the review, and
  `removed-by-user` the one whooper-remove leaves when you remove it.
  The worktrees `head/` and `base/` exist only while the run does. The run of a review that waits
  holds only `run.json`, the work item with the run ID and the agent `[jobs] review` names, and
  `waiting.json`, the cause it waits on.
- `digest.jsonl`: one line per finding that waits for the user, with its run.
