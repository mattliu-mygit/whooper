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
failure it prints why and leaves the file as it was. A change applies from the next round.

## See reviews

`bin/whooper-status --json` lists, as `reviews`, the review runs in `<state>/runs` that started in
the last 30 days, newest first: each one's PR, the PR's title and author, and times, how it went, its
verdict and number of findings, whether it is safe to approve, what whooper-submit posted, and
whether it needs you. Prompting Time builds its "Needs you" list and its history of past reviews
from them.

```sh
bin/whooper-show <run-id> --json   # one review in full, for its review screen
```

whooper-show prints the run's `run.json`, the PR's title and author, its outcome, `findings.json`,
the drafts whooper-draft prints for it, `diff.patch`, `failure.txt`, each of whooper-ask's answers in
number order, and the review whooper-submit posted. It reads files only, the run directory and the
review locks, and refuses a run ID that names no run directory. Reviews kept no `diff.patch` before
whooper-review saved one, and for such a run whooper-show has git rebuild the diff from the repo's
clone in the cache: `git diff` between the merge base and the head `run.json` records, with the flags
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

`safe_to_approve` is true when a review's outcome is `reviewed` and whooper-submit would approve it
without `--approve-anyway` ([Submit a review](#submit-a-review)): its verdict is
`approve_once_ci_passes`, every finding is a nit, and it did not skip the PR. It is false for any
other review, as for one that runs, waits, or failed, even with findings that fit, such as a Devin
review that passed its time limit after writing them. whooper-show gives it too.

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

A run is safe to approve when it reviewed the PR, its verdict is `approve_once_ci_passes`, and
every finding it has is a nit. A run that skipped the PR, as the review of a large PR from an
untagged ask does, read too little to vouch for it, so it is not safe whatever its verdict says.
Until the user changes anything, a safe run starts as an approval with every nit included, and any
other run as a comment with nothing included, so the user decides. A review that did not finish,
whose `findings.json` is missing or does not fit `schemas/findings.json`, such as an empty file or
findings without a verdict, has no drafts; whooper-draft names the first part that does not fit.

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
stop it. It refuses a review that did not finish, a run whose `run.json` it cannot read, a review
that includes no comment and does not approve, and a run it already submitted, so a review never
posts twice.

## Skip a review

`bin/whooper-skip <run-id>` marks a review done without submitting it, and prints what it did: it
leaves the empty file `skipped-by-user` in the run directory, so whooper-status lists the review with
`skipped_by_user` true and `needs_you` false, and whooper-show gives `skipped_by_user` true.
`bin/whooper-skip <run-id> --undo` deletes the file. Nothing else changes: nothing posts to GitHub,
and the drafts and records stay, so a skipped review can still be submitted. It refuses a run ID that
names no run directory and a review that is still running. Prompting Time runs it as the manifest's
`skip` action, adding the run ID, and `--undo` after it to undo.

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
runs it as the manifest's `remove` action, adding the run ID, and `--undo` after it to undo.

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
the manifest's `retry` action.

## Whooper in Prompting Time

Prompting Time reads `prompting-time-plugin.json`, which fits `schemas/prompting-time-plugin.json`:
Whooper's name and version, the command it starts (`bin/whooper-run`), the programs it checks for
first (`gh`, slackdump, `jq`, uv, and the CLI of each agent the settings' `[jobs]` name), the page,
the settings schema (`schemas/settings.json`), and the commands it runs as actions. An action with
`confirm: true` waits for the user's yes in Prompting Time; only `submit` has it, since only
whooper-submit writes outside this Mac. The tests check the manifest against the schema and the
checkout.

### Whooper's page

`page/` is the page Prompting Time shows for one of Whooper's reviews: the review's diff with
Whooper's findings as draft comments under their lines, Include in review, Edit, Ask about this,
Discard, and one bar that submits the review. It is a Solid 2.0 app built on Prompting Time's shared
UI library, `@prompting-time/ui`. Prompting Time shows it in an iframe sandboxed to scripts only, so
the page reaches nothing but its host: it sends Prompting Time the messages in
`page/src/protocol.ts`, which also says which Whooper command each one runs, and Prompting Time
confirms a submit before `whooper-submit` posts. The page shows the review Prompting Time opens as
soon as it arrives, and nothing before: Prompting Time's sidebar lists the reviews whooper-status
lists, so the page has no list of its own, and whooper-show prints the review the page shows
([See reviews](#see-reviews)).

The page needs Node 22.12 or later. In `page/`:

```sh
npm ci          # exact versions from package-lock.json, as the registry stood on .npmrc's date
npm test        # the page's tests, against the fake host in page/src/fakeHost.ts
npm run dev     # http://localhost:5175/dev/ shows the page in a sandboxed iframe with the fake host
npm run build   # page/dist, which the repo does not track
```

The fake host answers from invented reviews in `page/src/fixtures.ts` the way Whooper's commands
would, and reaches no Whooper, GitHub, or model; `/dev/` opens the first of its reviews, and
`/dev/?review=<run id>` another.
Until `@prompting-time/ui`'s first npm release, the page installs it from a Prompting Time checkout
beside this one, `../../repos/prompting-time-actions/packages/ui` from `page/`, which `npm ci`
copies in; it then switches to a pinned npm version. The checkout must be on branch `solid-actions`,
whose `Diff` draws one file of a parsed patch. Versions are exact and resolved as of `.npmrc`'s
`before` date, at least a week before they were pinned, and no install script runs.

### Releases

The repo tracks no build output. Pushing a version tag, `v` and the manifest's version, runs
`.github/workflows/release.yml`, which builds the release archive with `scripts/build-release` and
attaches it and its SHA-256 to the tag's GitHub release; Prompting Time installs that archive,
pinned by the tag and the checksum. `scripts/build-release [<tag>]` builds the same archive locally:
`release/whooper-<version>.tar.gz`, HEAD's files plus `page/dist`, and its `.sha256`. Until
`@prompting-time/ui` is on npm, the workflow stops at its first check, since the runner has no
Prompting Time checkout to install it from.

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
run directory as `failure.txt`; the reason a failed setup script gets ends with the last line the
script printed on stderr. A review stopped any other way gets a fixed reason there:
`the review was interrupted` after Ctrl-C or a `kill`, and, after a command that failed,
`the review stopped at an unexpected error; see whooper-review's error output`, where the
command's own message went. Beside it, `failure.json` names the cause, from the step the review
stopped at and what that step printed on stderr ([See reviews](#see-reviews) lists the causes). A
repo the config lists but the cache has no clone of fails the review once its run directory exists,
so the failure shows in whooper-status.

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
`asks/<n>.json`, and whooper-post, whooper-submit, whooper-retry, and whooper-remove write the recheck
list's files the same way too.

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
  whooper-select drops an item in a repo the config has no `[repos."<owner>/<repo>"]` table for,
  an item whose record is done or running or has failed 3 times, a re-check of a PR that already
  has 3 re-check records for other heads started today (local time), and an item that asks again for
  a review you removed ([Remove a review](#remove-a-review)). A recheck-list item counts under that
  limit too, by the records of its own kind; dropped for it, the item stays on the list for the next
  day. Of these, only the missing table drops an item whose record says retry.
- `rechecks/<owner>-<repo>-<pr>.json`: the recheck list: a PR whose head whooper-post or
  whooper-submit found moved since its review, as the work item for the new head, or whose review
  you asked for again with whooper-retry, as the run's work item at the head the PR has now, or whose
  review waits for the next round, as its work item, with the kind, source, and Slack ask it had. A
  later mark of the PR replaces it. whooper-select deletes it once
  the newest poll lists the PR, which then comes with the head the PR has now, once the record at
  the listed head is done or has failed 3 times, or once it asks again for a review you removed.
- `locks/<owner>-<repo>-<pr>/`: the lock of the review that holds the PR, with its process ID in
  `pid` and its run ID in `run_id`. A lock whose process is gone is stale: the next review takes
  it, and whooper-ask, whooper-status, and whooper-show no longer count its review as running.
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
  (`findings.invalid.json` when it did not fit the schema), `failure.txt` the reason whooper-review
  gave when it stopped the run and `failure.json` its cause, `log.md` its evidence for each finding
  and the candidates it dropped,
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
