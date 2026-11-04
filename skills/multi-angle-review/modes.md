# Multi-angle review: publishing, co-review, later rounds, and stacks

Read this with `SKILL.md` before publishing approved comments, in co-review mode, when the author has responded, and when the target is a stack.

## Publish, only after approval

1. Present findings in chat before any external work, so the user can read while you prepare.
2. Split them. An obvious fix gets a suggestion block or an example PR. A finding that needs a design decision gets no PR: give the options, the evidence class, whether it is hard to undo, and the trigger that makes it urgent.
3. After the user approves specific items, post them as a pending review. Never submit it. Give the user each comment's discussion URL.
4. An approved example PR holds one idea. Create it from its own worktree as a draft, close it with a comment saying it is an illustration, then list your open PRs and confirm none of this review's PRs is open.
5. When a permission check denies a write, retry once. If it repeats, put the exact denial at the top of the report and do not call the review done.

## Co-review mode

After the orientation, send one finding per message, labeled "finding N of M". Quote the lines the claim rests on and show any change as a snippet against the existing code. Say whether it should hold the merge or whether another item covers it. End the message and wait. Between items, reread the PR for new input. Answer a new comment at once when it lands on the current or an earlier item, and otherwise hold it for its item.

## Later rounds

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

