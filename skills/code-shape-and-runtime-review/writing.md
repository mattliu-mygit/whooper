# Writing code, PRs, and replies

Read this with `SKILL.md` when you write code, prepare a PR, check your own change before a push, or reply to review. The checklist in `SKILL.md` is the standard your code must pass, so this file does not repeat it.

## Style by example

[examples.md](examples.md) holds invented examples of the style the checklist in `SKILL.md` describes:

- **A closed variant type**: a JSON union whose wire forms carry only the ID their kind selects, and whose decoder rejects a missing or unknown kind.
- **Bookkeeping that must outlive the caller**: `context.WithoutCancel` with a one-line reason, a logger scoped with IDs, and `errors.Join` for a secondary failure.
- **A deliberate duplicate, a contract docstring, and pinned settings**: a drift warning at the declaration, a query ID operators can search for, and a pinned database setting with its reason.
- **A spy that embeds the store**: a test double that records one method's input and delegates, plus one test run against the fake and the real stores.

## PRs

- Keep PRs around 200 changed lines where the work allows. Land mechanical changes and behavior changes separately.
- Sequence the work: tests first, then the refactor. A move-only PR, then the behavior change. A rename as a prep PR before the feature. Before a feature grows a long function, extract its named steps in a behavior-preserving refactor PR. Add, migrate, then delete, as three PRs. A flag, then the feature, then activation. One PR per resource or per service.
- Deleting code is progress. Follow a feature with the PRs that remove what it made redundant. Prove from the writer side that a state cannot occur, then delete the code that defended against it.
- Title type follows reachability: `chore` for code nothing reaches yet, `feat` for turning it on.
- A split that produces a prep PR can leave a helper with one caller at that PR's own head, which fails the one-use rule where it is reviewed. Say in the prep PR's body and the helper's comment which child grows it and why the move is worth its own review, or leave the move in the child. After a restack moves code into an earlier PR, ask again whether each file and helper still has a reason.
- PR body: What, Why, How, Testing. State what changes on merge and what does not. Give production evidence with counts and a dashboard link, measured alternatives with the condition that would reopen the choice, and `file:line` proof of any invariant the change relies on. Name each test with its exact command and pass count. Say what an operator must do after a failure.
- When a review point is easier to show than describe, open a closed example PR against the reviewed branch, one finding each, and say it is not meant to merge.

## Before pushing your own change

The goal is a diff that a reviewer holding these opinions has nothing to comment on, nits included. Rules fail when they are applied to categories instead of instances, so work per instance.

1. **Run the Shape worksheets on your own diff:** the identifier sweep, the caller sketch, and the hand-written machinery list from "Pre-reviewing a diff". Every row gets a verdict.
2. **Walk the checklist per instance.** For each changed file, go through every checklist heading and name each instance it applies to in that file: each new name, helper, type, field, docstring, operation description, SQL builder, test, error path, loop, stored shape, and number. Fix every instance that fails, including the ones that look minor. A reviewer who comments on nits will comment on the one you skipped.
3. **Run the Runtime pass** on your own diff, as in "Pre-reviewing a diff".
4. **Read your own PR body** against the code at the pushed head. Every claim must be true of that head.
5. **Walk test tables, loops, and test files line by line.** Sweeps that read tests for correctness, or leaned on scripts, have missed one-line table cases, in-package test files, and long loop names. Read each table case, loop, and test file's package line against the Tests and Go idiom headings, and check every added test for its phases, names, assertions that can fail, and why-comments.
6. **Run the mechanical checks last, as a backstop**, where the repository has them. Steps 1 to 5 are judgment and come first: the scripts are narrow and miss most of what a careful reader catches. Treat each flag as a question for that instance:
   - A repeat-check script for semicolons in comments, docstrings that open with their own name, API helpers that return `*Response`, deleted base tests, stale references to removed routes, JSON written or matched by hand, inline struct types with JSON tags, and test helpers that only return one literal.
   - A PR-body check for identifiers a title or body names that the head no longer has.
   - `golangci-lint run --new-from-rev BASE` with only `revive`'s function-result-limit rule enabled, for functions that return more than two values plus an error.
7. **Check your own reply before posting it** against "Replying to review" below.

## Replying to review

- Answer the question before proposing a change. An existing name or shape may be fine.
- When you keep the code, give a concrete reason: the interface it implements, the non-HTTP caller, consistency with nearby code, or a measured cost. Answer with evidence where possible: a Go playground link, docs, a measured timing.
- When you concede, change the code, link the commit, and name the test that pins the change. When the reviewer's question showed the code was unclear, put the explanation into a code comment, not only the thread.
- When you defer, link the ticket from the code and the ticket from the PR.
- When the ask is vague, ask what specifically is unclear.
- A second reviewer raising a point you skipped is a reason to change it.
- Before saying a fix is pushed, check that it is.
