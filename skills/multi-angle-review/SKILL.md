---
name: multi-angle-review
description: Use when reviewing a diff, pull request, branch, stack, or design document in any repository or language, including your own changes before a commit or push, a colleague's PR, a return round after the author responded, fixes written in response to review, and work handed to a reviewer subagent.
---

# Multi-angle review

Find with recall, then report with judgment. A missed bug ships, so the finder angles run wide and never suppress each other. A comment on a colleague's PR costs their attention, so every candidate is qualified, ranked, and given a disposition before anyone reads it.

## Hard rules

- Never push, commit, force-push, rebase, or amend on the author's branch.
- Never approve, request changes, submit a review, merge, or close a PR. The user makes the final call.
- Never post a comment, reply, pending review, or example PR until the user approves that item in chat. Approval covers only the items it names.
- Never edit a comment the user already submitted. Check the review state first. If it is not `PENDING`, the words are the user's and are already in the author's inbox.
- Never commit in the primary working tree. Use one worktree per example PR.
- Do not manufacture findings. Finding nothing is a valid result. Say so and stop.

## Step 0: Pick the mode

| Mode | Use when | Reporting bar |
| --- | --- | --- |
| **Self** | Your own diff, before a commit or push, or fixes you wrote for review | Report every candidate that has a failure scenario or a concrete cost. Fix cheap items instead of dropping them.
| **Peer** | Another author's PR or stack | Qualify every candidate (Step 4). Drop what is cheap to change later and does no harm now.
| **Co-review** | The user is walking through a PR with you | Peer bar. Present one finding per message and wait (Step 8).
| **Document** | A design doc, plan, or PR split | Cite document lines and relevant code. Separate contradictions and deductions from observed failures. Never claim unimplemented behavior was reproduced.

When you delegate a review to a subagent, its prompt names the mode and says whether the user approved posting.

## Step 1: Scope and readiness

- Review the target you were given (PR, branch, path, document). Otherwise review the committed range against upstream (`git diff @{upstream}...HEAD`, or the merge base with the default branch), plus `git diff HEAD` when there are uncommitted changes.
- Pin the head SHA. Every later step reads code at that SHA. Confirm line numbers with `git show "${SHA}:path"`.
- Scope is the diff plus the whole body of every function it touches. A bug on an unchanged line of a touched function counts. The change either exposes it or failed to fix it.
- Test files get every angle, the same as production code.
- In a stack, rebuild it from refs. Walk down through `baseRefName` and up through PRs whose base is this PR's `headRefName`. Review each PR's own diff against its parent, not the tip.
- When a PR's branch predates the squash-merge of its parent, GitHub shows the parent's files. Compute the net change with `T=$(git merge-tree --write-tree origin/master origin/<head>)` and `git diff --stat origin/master "${T}"`, and say which files to skip.
- In every mode, check that the head still merges with its current base: `git fetch` the base, then `git merge-tree --write-tree origin/<base> <head>`. A base that moved since the head was cut can delete what the diff calls or already contain what it adds.
- Mark every draft PR with a `-d` suffix (`#123-d`) in everything you show the user. Keep the suffix out of posted comments.

### Readiness gate (peer and co-review)

Answer "is it ready for review, or is it still churning?" in one line first, then give the evidence.

1. **State.** `isDraft`, `mergeable`, `mergeStateStatus`, `reviewDecision`, head SHA.
2. **Alive.** A draft with no commits in a week, `CONFLICTING`, and no requested reviewer is probably abandoned, especially when a merged PR solved the problem another way. Verify supersession before you stop. Age, draft state, and conflicts alone do not prove it.
3. **Churn.** Count pushes in the last 24 hours and since the user's last review. A force-push count overstates churn, because a restack is also a force-push. Measure the PR's own patch change with `git range-diff <old>^..<old> <new>^..<new>` after fetching both full 40-character SHAs. A push in the last hour means the author is still working. Say so and review at the pinned head anyway.
4. **Other reviewers' threads.** List unresolved threads with the GraphQL `reviewThreads` query. An outdated thread is not an addressed thread. Read the code at head and classify each one as addressed, not addressed, or answered in the PR body. Name every thread and `CHANGES_REQUESTED` review that would stop approval.
5. **Overlap.** When another reviewer already raised what you would raise, say so, and fold yours into theirs.
6. **CI.** Report the service's own test and lint jobs. Read the first error in a failed job's log before calling it a test failure. A job that failed in seconds is often setup, not a test.

Stop after the gate only when the author says the PR is under redesign, or requested changes will reshape it. Say what event makes the review worth doing.

## Step 2: Understand before you judge

- Gather metadata with `gh pr view <n> --json title,body,baseRefName,headRefName,files,commits,additions,deletions` and the diff with `gh pr diff <n>`.
- Explain the change in chat: what it is for without naming a symbol, how it works with each local term introduced at first use, and what lands on merge.
- Is it needed now? Name the problem it fixes and the evidence class: observed in production, observed in tests or CI, reported experiments, or theoretical. Optional speculative work may be deferred. A required correctness property of an approved feature needs no prior incident.
- When the diff adds code that no production path calls at the head, find the first caller before you judge its API. Look in later commits, local branches, and the stack (`git log --all -S'<symbol>'`). Read that caller. A commit message or PR body that names the caller is a claim to check, not the caller. Judge the new code's failure behavior (panic, cancel, retry, shutdown) against how that caller uses it.
- Classify the PR and set depth:

| PR type | Focus | Depth |
| --- | --- | --- |
| Bug fix | Root cause, regression risk, test coverage, minimal change | Deep on affected code
| Refactor | Behavior preservation | Verify equivalence
| New feature | Design, edge cases, tests, docs | Comprehensive
| Dependency update | Breaking changes, security advisories | Changelog review
| Config or infra | Side effects, environment differences | Deployment impact
| Suspected over-engineering | Necessity of each abstraction, simpler alternatives | Challenge every addition

- **What is frozen?** Before weighing a contract or schema change, find out whether the service is deployed, whether real data exists, and whether any client depends on the current shape. When nothing is frozen, judge whether the change is principled, not whether it is compatible. Answer it from the deploy configuration or runbook, which says where the service runs today, not from an architecture overview.
- Read every instruction file that governs the changed paths: the user-level instructions, the repo-root `AGENTS.md` or `CLAUDE.md`, and any nested one above a changed file. Load the skills they require. For a backend Go repository, load `code-shape-and-runtime-review`, and a Go language skill for Go.
- A pattern that matches its sibling modules is a convention, not a finding. Name the siblings, and check that they agree (Step 4).

## Step 3: Find (recall)

Run every angle over the same diff. Each angle surfaces up to eight candidates or an explicit "none". An angle never suppresses another angle's candidate. If two angles flag the same line for different reasons, keep both.

**Correctness**

1. **Line scan.** For each changed line, find the input, state, timing, or platform that makes it wrong: inverted condition, off-by-one, nil dereference, missing await, zero read as "unset", a copy-pasted line using the wrong variable, a swallowed error, an unescaped regex character, a race.
2. **Removed behavior.** For each deleted or replaced line, name what it guaranteed, then find where the new code guarantees it. If nothing does, that is a candidate: a dropped guard or error path, narrowed validation, or a deleted test that covered a real case. A removed bound (an end key, a row-count check, a `LIMIT`) may also have capped the work. Check what stops the read on the last page and on a cursor past the end.
3. **Callers and callees.** Search for every caller of each changed function. Look for a new precondition, a changed return shape, a new error, or a new ordering dependency. Then check callees: does another change in the same diff make an existing call unsafe?
4. **Language pitfalls.** Check the classic traps of the diff's language and load the matching language skill. Go: writing to a nil map, a typed nil in an interface, `defer` inside a loop. JS: falsy zero, `==` coercion. Python: mutable default arguments, late-binding closures. Anywhere: SQL built by concatenation, timezone or DST drift, float equality.
5. **Wrappers.** For every type that wraps another (cache, proxy, decorator, adapter, fake), each method calls the wrapped value directly and does not go back through a registry or global that re-enters the wrapper. The wrapper implements every method its callers use.
6. **Security.** Input validation, injection, authentication and authorization, secrets in code, unsafe deserialization. Security code fails closed: an empty, ambiguous, or mixed answer is a rejection.

**Durability**

7. **Hard to undo.** Anything stored rows, generated SDKs, or customer data will freeze: a durable format that a later reader cannot tell apart from its successor (a missing version marker is reversible when a reader can treat its absence as the current version), an idempotency key derived from internal code shapes, a schema no read query has exercised, a client-visible error code that conflates a transient state with one that needs a human, a wire value read from a global instead of passed in.
   - Ask of every new type that models a result, a joined view, or a stored row: which fields are present in every case, and which only in one? Name the cases you believe exist and the shape that holds them: a base type for what is common, plus one arm per case, discriminated by an explicit tag field. Say what the flat version costs: a caller cannot tell which fields it may read. If the type is persisted, a later split must decode the flat shape forever.
   - Two symptoms make it concrete. An optional field whose presence encodes which case occurred is a tagged union spelled badly. A field named after one implementation, on a type named after the general thing, sits one level too high.

**Cleanup**

8. **Reuse.** New code that reimplements an existing helper, a sibling module's abstraction, a standard-library interface, or an installed dependency. Search shared packages and neighboring files. The test is whether the existing thing could have been called. Name it with its path. A newer module may carry a fix that the older shared base never got.
9. **Simplification and necessity.** State derivable from other state, near-duplicate copies, deep nesting, dead code left behind. Sketch the minimal solution, compare it with the PR, and challenge each new class, file, layer, and future-proofing hook. Agent-written or cargo-culted signs: structure that looks right with subtle semantic gaps, abstractions that do not map to the domain, comments that say what instead of why, defensive code with no named threat, and two voices in one PR. Name the simpler form, and say so when the complexity is warranted.
10. **Efficiency.** Repeated work or I/O, N+1 queries, independent calls run in sequence, unbounded growth, missing indexes, blocking work on startup or a hot path, a long-lived closure that keeps a large scope alive. When the gain depends on data size, write the measurement that decides, not a fix.

**Design**

11. **Altitude.** A symptom patch where the root cause sits deeper. A special case added to shared code usually means the fix is too shallow. Name the general change to the shared function every caller goes through. For a bug fix, also flag unrelated refactoring mixed into the fix.
12. **Conventions.** Flag a violation of an instruction file or loaded skill only when you can quote both the rule and the line that breaks it.
13. **Outside the diff.** Nothing in the diff prompts these, so ask each one:
    - Should this be two PRs? A pure move mixed with a risky behavior change holds the safe half hostage and invites a rubber stamp on the risky half. Name the split.
    - What alternatives exist? When the change picks a design without saying why, write the alternatives with one-line pros and cons and name the one you would take. Include any option smaller in code but wider in blast radius, and say both.
    - Would the tests catch this if it broke? Say whether the suite cannot catch it for a structural reason or because nobody wrote the assertion. A rule enforced by a test someone must remember to extend is weaker than one the compiler enforces.
      Answer it by mutation. For each hunk that changes behavior, put the base version back, and run the tests of the packages that import the changed code. A revert that passes every test is a test gap. A mutation that passes the new test but fails another one is not a gap, so name the test that already pins it.
    - Does it collide with work in flight? Search open PRs for every shared constant, gate, base class, and call site it changes, including the user's own (see Stacks).
    - What did the author measure, and could the harness see the failure? A zero-stale-reads run against one local database cannot show replica lag. State which condition the measurement covers and which it cannot.
    - Is each TODO a plan? A plan has a mechanism, an owner, and a ticket. Say what deferred work must deliver, and whether a fast follow is enough.
14. **Proposed code in the thread.** Every suggestion block and proposed change in the review thread, including the user's own, is code under review. Run it or otherwise verify it against the installed dependencies before endorsing it. An author's "makes sense!" is not verification.

### Gap sweep

Merge candidates that describe the same defect at the same place for the same reason. Do not drop a candidate because you are unsure of it. Then reread the diff as a fresh reviewer who already has the list, looking only for what it lacks. First passes usually miss:

- code moved or extracted that lost a guard on the way
- a default value evaluated once and then shared
- iteration order or hashing that is not deterministic
- a lock whose scope shrank
- a predicate or getter with a side effect
- setup and teardown that do not mirror each other
- a test that cannot fail
- a config default that flipped
- lines written to answer an earlier review comment (review them as new code)

Add up to eight more. Do not pad.

In self mode, also run two breadth checks. A self review is the last pass before reviewers see the change, so it covers what sits next to the diff, not only the diff:

- **Docs that the change makes false.** Grep the repository's docs, runbooks, and doc comments for each status, error, limit, setting, and name the diff changes or relies on. Check that each sentence still describes the code at the head. A doc the change makes false is a finding.
- **Pre-existing defects in touched code.** For each function or query the diff touches, look for defects that predate the change, such as a read that can miss recent writes, a loop with no bound, or a missing setting. Raise each one as pre-existing. It does not block the push.

## Step 4: Qualify every candidate

A candidate becomes a finding only when it passes these checks. In self mode, run the first three and keep everything that survives.

- **Who reads it?** For a header, field, verb, or error message, find what reads it today. With no reader, downgrade it to a note.
- **What breaks?** Say in one sentence what breaks in the running service. When nothing breaks yet, say so and raise it as a question about intent. For a design concern, say what becomes harder to understand or change and why the fix helps.
- **Is the premise true?** A claim that a route should be a POST needs evidence that the route writes something.
- **Could you check the state it rests on?** A drafted comment whose premise depends on stack or GitHub state you could not check goes under what did not run, not into a comment. Examples: whether an unrelated commit is already its own PR, or which PR a commit message says calls the code. The exception is a problem that would leave stored data wrong for good, such as rows written with a value nothing rewrites, when it happens only under one merge or deploy order. Raise it as a conditional comment: "If A deploys before this PR, ...", with what to do in each case (merge them together, or move the change into A).
- **Does the author already know?** Grep their TODOs and their own review threads. A known item is folded in as agreement, not raised as news.
- **Defect or deliberate choice?** When the behavior looks chosen, say whether you found evidence that it was. A bug report about a deliberate decision costs more trust than the finding is worth.
- **Introduced or pre-existing?** For an inherited problem, say how much this change makes it worse. A new test, comment, or doc line that asserts an inherited behavior turns it into a contract. Treat that as introduced here, and ask whether the behavior is intended, citing the doc or sibling path it disagrees with.

Then earn the claim:

- Cite file, function, and line, with a permalink pinned to a commit SHA.
- Label each finding's support with one word in the report: `measured` (you ran a probe, test, or mutation), `read` (you followed the path in code at the pinned SHA), `inferred` (from docs, commit messages, or the PR body), or `untraced`. Run a probe or mutation test when feasible. The comment carries no support label. It says in one plain sentence what you ran, when you ran something ("I reverted the write and got a nil pointer dereference").
- Check every number. Label a worst case as a worst case.
- A clear or a drop is a claim too. Its reason must answer the candidate's own question. Test coverage does not answer a readability concern. Check its facts the way you check a finding's: count the instances you cite, read the dependency's source before relying on what it returns, and run the probe when the clear rests on behavior.
- A probe must use a state production can reach. Doctoring one of two reads of the same row proves nothing about base versus head.
- A clear that rests on sibling precedent names the siblings it compared. When they split, say which siblings follow which rule, and raise the choice as a question. Public names (SDK methods, routes, error codes) always get this check.
- Before you report a check as not run because a tool is missing, search for it: the repository's version file (such as `.go-version`), then Nix (`ls -d /nix/store/*-go-*/bin`), mise, and asdf.

## Step 5: Rank and dispose

Rank by how costly the problem gets, not by line number:

1. Hard to undo (angle 7). Rank a finding here only after you trace the write to a stored row, a published SDK or API name, or a client-visible contract, and find that a later reader cannot accept both shapes. An untraced candidate ranks by its other effects.
2. Blocks parallel development: a struct, constructor, or `main.go` every new route must edit, or two branches conflicting in five places.
3. Misleads future agents: two rival abstractions where the dead one is easier to find, dead names shadowing live ones, docs describing a deleted design, an assertion that no longer asserts.
4. Correctness and operability: unbounded retries, consumers that stop reading, permanent parks, broken metric invariants, shutdown stalls, telemetry that silently vanished.
5. Missing signals and unjustified settings: a stall with no metric, two limits where only one can bind. A new setting needs a named operator action, such as turning off a cache that serves wrong results. Otherwise prefer a constant.
6. Cleanup and conventions.

**Calibrate.** In peer and co-review mode, drop a finding that is reversible, cheap to change later, and does no harm now. Cheap to change later is not enough on its own to drop a small, concrete fix on a line the PR adds, such as deleting a line with no effect or a check for a state the code rules out. When the repository's skills say how to choose which findings the user raises, follow them. Self mode has no Dropped disposition. A real item is Fix here, and a candidate that is not a problem is cleared.

**Fold, do not clear.** When a larger fix would remove a smaller finding, list the smaller one inside the larger finding as a lettered sub-item ("5a. Also removes: ..."), so the user can answer "fix 5a" alone. Sub-items live in the report. The drafted comment carries only the main finding until the user asks to merge one in. "Chased and cleared" is only for candidates that turned out not to be problems.

**Dispose.** Every finding gets exactly one disposition, and the comment opens with the matching [Conventional Comments](https://conventionalcomments.org/) label. The disposition words themselves never appear in a posted comment.

| Disposition | Opening label | Closing sentence |
| --- | --- | --- |
| Blocks approval | `**issue (blocking):**` | What would have to change for you to approve
| Fix here (the norm) | `**suggestion:**`, or `**suggestion (if-minor):**` | None. Give the size of the fix ("two blocks swapped")
| Fast follow | `**suggestion (follow-up):**` | Why it needs its own PR (other module, pre-existing, behavior elsewhere) and what the follow-up must contain
| Can defer | `**suggestion (non-blocking):**` | What would make it urgent
| Open question that decides the ask | `**question:**` | None
| Fact that changes the author's next step | `**note:**` | None
| Take or leave: a small edit the author may skip, such as a readability rewrite, a wording fix, an idiom one-liner, or a line with no effect | `**nit:**` | For a readability or taste rewrite, say the current form also works ("fine to keep if you prefer the current form"). For a line with no effect, state why and stop
| Dropped (peer and co-review only), or chased and cleared | No comment | Listed in the chat report only

A pre-existing problem's first sentence after the label is "Pre-existing issue, consider follow-up PR."

Block only for data loss, a client-visible defect (a status or error message that differs from the base for the same request counts, even when the new one is one of several), or a hard-to-undo shape that customer data would freeze, plus the repository's own blocking list (for example, the "When to block" section of `code-shape-and-runtime-review`).

**Propose a shape only through two gates.** State both in the comment.

1. It creates a test seam now: something can be asserted that could not be before, without a new mock.
2. It absorbs a named expected change with less churn. "A second payment method" counts. "Future flexibility" does not. Say which files each shape would make that change touch.

Passing only gate 2 is speculative generality. Passing only gate 1 is a test-only refactor, priced as one. The shape that passes both most often is the **pure core**: in a unit that ends in I/O, make the largest part that touches nothing external a named function that takes and returns plain data, and leave the effect as a thin caller. Ask "what is the largest part that touches nothing external", not "what do the two callers share". When the file already has one small pure helper beside the effect, ask the author to continue their own pattern.

**Track recurring patterns** separately, including items too small for their own comment, and report the list once for the series.

## Step 6: Write the comments

- Anchor the comment to a line range that covers every line it discusses, starting at the line that exposes the mechanism.
- Name the symbol in the sentence: "the `MaxOrderLines` check", not "the total line check".
- After the label, the first sentence leads with the impact. Someone with no context must see what the comment asks for in the first two sentences. If you cannot name the ask, do not post it.
- Include the code. Use a ```suggestion block for a small local fix. Keep the snippet to the smallest edit that works and reuse what the file already has. Do not add helpers the author did not ask for.
- A substantive finding uses this shape, so a reader can judge it without private context:

  ```markdown
  **issue:** **Problem.** <the concrete failure, in one sentence>.

  <current mechanism and observable consequence: functions, types, tables, statuses, responses, tests>

  **Solution.** <smallest change that removes the cause>. <closing sentence from the disposition table, if any>
  ```

  Add `**Impact.**` or `**Precondition.**` only when the sequence would otherwise be unclear.
- When the code reads two ways, give each reading as a paraphrase of about a dozen words in the author's voice, then say what follows from each.
- Give two or three options with one-line pros and cons, including doing nothing, in the report, and name the one you prefer. The comment gives the preferred one, plus a fallback only when the first may not fit. For a performance fix, name the measurement that decides it.
- State facts declaratively once the evidence supports them. Soften the ask, not the finding: "this drops the retry when X" plus "could the check move into Y?". In peer mode, phrase the ask as a question unless the comment reports a bug you reproduced that the PR introduces. Those get a plain request ("Please ...", "Fix in this PR.").
- When the right change depends on what the author intended, or on a choice you cannot check, ask the question instead of prescribing the fix (see the repository's comment rules for examples).
- One root cause per comment. When the same contract is duplicated across a stack, raise it once and scope the fix across the PRs.
- Put the comment in the PR that introduces the behavior the fix depends on, preferring the later PR so earlier ones can merge. When a thread on the topic exists, reply in it.
- No praise, throat-clearing, or severity theatre in comments. Cleared items go in the chat report.
- Short paragraphs or bullets, one point each. Connect the detail to customer, operator, or maintenance impact. "Cleaner" alone is not a reason.
- When the repository has comment prose rules, such as those in `code-shape-and-runtime-review`, follow them.
- **Check the premise of a requested comment.** When the user asks for a comment and you know a fact that undercuts it, state the fact before you draft.

## Step 7: Publish, only after approval

1. Present findings in chat before any external work, so the user can read while you prepare.
2. Split them. An obvious fix gets a suggestion block or an example PR. A finding that needs a design decision gets no PR: give the options, the evidence class, whether it is hard to undo, and the trigger that makes it urgent.
3. After the user approves specific items, post them as a pending review. Never submit it. Give the user each comment's discussion URL.
4. An approved example PR holds one idea. Create it from its own worktree as a draft, close it with a comment saying it is an illustration, then list your open PRs and confirm none of this review's PRs is open.
5. When a permission check denies a write, retry once. If it repeats, put the exact denial at the top of the report and do not call the review done.

## Step 8: Report

1. One line: what blocks approval (or the push, in self mode) and what is a fast follow. This is always the first line. That is the question being asked, typed or not.
2. Corrections to anything you said earlier, only when there are some.
3. Orientation in two or three bullets: what the change does, before and after, and whether the user has approved it yet.
4. The findings table, numbered across the whole review so the user can answer "fix 2 and 5", ordered by Step 5's rank. Number only findings that ask for something (Blocks approval through Take or leave). A folded finding is a lettered sub-item.

   | # | Disposition | PR | `path:line` | Label and first sentence, verbatim | Replacement | Support |
   | --- | --- | --- | --- | --- | --- | --- |

5. Answers to the user's direct questions, each self-contained.
6. Findings that need a design decision, with options.
7. What you chased and cleared, or dropped, one line each with its reason, labeled d1, d2, ... so the user can still pick one. This is how the user calibrates the findings you kept.
8. Recurring patterns (Step 5).
9. What you verified and how, and which test suites did not run.
10. One next step.

Leave out what the user can already see, such as who approved and that checks pass. Report a test failure or what checks cannot cover.

Omit any section that would only say "None" or "no questions were asked". Keep angle-by-angle results and shape worksheets out of the report, and do not link into temporary worktrees. Check the success criteria in your working notes, and report one line ("all angles ran") unless the user asks for the detail.

**Co-review mode.** After the orientation, send one finding per message, labeled "finding N of M". Quote the lines the claim rests on and show any change as a snippet against the existing code. Say whether it should hold the merge or whether another item covers it. End the message and wait. Between items, reread the PR for new input. Answer a new comment at once when it lands on the current or an earlier item, and otherwise hold it for its item.

When the user returns and asks for a section again, repeat it in full. Do not say "scroll up".

## Step 9: Later rounds

When the author has responded:

1. Reread the diff at the new head. Do not trust your earlier read.
2. List the user's reviews and their state. A review created as pending may since have been submitted. Never tell the user to submit a review that is already sent.
3. For each earlier comment, read the code and classify it: addressed, addressed in spirit by a different change, deferred with a stated reason, or marked done when it was not. The last one goes first, loudly. Do not trust the resolved flag or the author's "done".
4. Say which threads the author answered and which have no reply. No reply does not mean refusal.
5. When a PR merged with an unaddressed comment, name the later open PR where the fix can land.
6. Drop any example PR the author's own change made unnecessary.
7. Look for problems the fixes introduced, especially a removed delay that was absorbing something, or a trimmed docstring that left a metric's dip without a documented meaning.
8. Answer the approval question again.
9. Sweep the conversation and confirm every concern raised reached a comment, a PR, or an explicit decision to drop it.
10. Say which pending comments are anchored to an old head and need re-anchoring before the user submits.

## Stacks

- Before presenting the first PR, review every PR against its base, trace how later PRs use or change each behavior, and synthesize across the stack. Attribute each finding to the PR that introduces it, and use the rest of the stack to explain its impact. Include effects of later PRs before their own walkthroughs.
- Frame the stack in three to five sentences: what it delivers, for whom, in what order. Then one table:

  | PR | What it does | Priority | First 3 files | Earlier feedback |
  | --- | --- | --- | --- | --- |

  - Priority is P1 to P3 with the reason in the cell. Rank by named risk (hard-to-undo and correctness first), by what the user learns about code they will work in, and by interaction with the user's work. Never by size, readiness, or position.
  - First 3 files carry the design, not the most lines. Skip tests and generated code unless a test is the contract.
  - Name the kind of risk. "Rework risk" and "merge risk" are different things to a reader.
- List prerequisites: which PRs a reader must understand before which, and anything that blocks review.
- **Conflicts with the user's own open PRs.** List them with `gh pr list --author @me`. For each one sharing files, run `git merge-tree` against the stack PRs. Conflicts git or the compiler reports are cheap: resolve them when the second stack merges. Mistakes that merge cleanly (a resource acquired twice, two options that must not be set together) go as a short checklist in the user's PR description. Do not build a guard ahead of time. The stack that merges second resolves the conflict.
- **Subagents.** Send independent PRs to parallel subagents in one message. Use `isolation: "worktree"` for any agent that runs code. Heads move while agents run, so re-verify each finding at the current head before it becomes a comment.

  | Work | Model | Reason |
  | --- | --- | --- |
  | Review of another author's PR, and re-verifying a finding at a new head | `opus` | It finds hard-to-undo shapes, runs probes, and writes comments other people read
  | Walkthrough and stack briefing | `sonnet` | It explains a diff under fixed ordering rules
  | Bulk metadata or history fetches | `sonnet` | Mechanical, fixed output shape
  | Readiness gate, thread lists, conflict checks | Main conversation | A few calls, and the result feeds every subagent prompt

  Each subagent prompt states the PR number, head SHA, base branch, the user's GitHub login (`gh api user -q .login`, which differs from the git author name), the mode, whether posting is approved, the skills to load, and the gate result so the agent does not repeat it.
- When a finding raises a file's rework risk, move that file earlier in the walkthrough and say why.

## Traps that have happened

- **zsh modifiers.** Write `"${REF}:path"` with braces. Without them zsh reads `:r` or `:s` as a history modifier.
- **Short SHAs.** `git fetch origin <sha>` needs the full 40 characters.
- **Inflated diffs.** A stale base made a PR look like +1010/-263 when its net change was +552/-262. Compute the net diff before picking files.
- **Thread state is not code state.** A note looked answered in its thread, but the merged code never got it.
- **Submitted reviews.** The user was once told to submit reviews that were already sent. Check state first.
- **Overbuilt conflict plans.** A guard proposed before either stack merged cost more than resolving at merge time.
- **"Next step: post the comment."** In report-only runs the review ended with an unposted correction and the user had to ask again. When the user approved posting, post. When they have not, ask once in the report's next step.

## Success criteria

- The mode was chosen and stated.
- All fourteen angles and the gap sweep ran, and each produced candidates or an explicit "none".
- Every touched function was read in full, test files included.
- Every reported finding passed Step 4, has a location, a failure scenario or cost, a replacement, a disposition, and a support label.
- Every conventions finding quotes its rule.
- Nothing was posted, pushed, or submitted without the user's approval for that item.

## Not this skill

- Answering or applying review comments someone else left: use `reconstructing-review-intent`.
- Repository-specific shape and runtime checklists: the conventions angle loads those skills, and this skill does not restate them.
