---
name: code-shape-and-runtime-review
description: Use when writing, pre-reviewing, or reviewing backend Go changes, when writing PR review comments, or when replying to review feedback.
---

# Code shape and runtime review

Review every diff through two lenses, in two separate passes.

- **Code shape**: names, which layer owns each piece of logic, duplicate types and helpers, framework features over hand-written code, public API ergonomics, docstrings, and test readability.
- **Running system**: error paths and their side effects, what an operator can see, unbounded loops and stuck states, formats and fields that cannot change later, tests that cannot fail, docs that contradict code, and PR scope.

A single combined pass drifts toward correctness findings and misses shape findings, which are small and local: one identifier, one helper, one docstring, one tag. General principles live in `engineering-principles` and `go-engineering`. This skill holds the opinions behind both lenses, the concrete checklist, the coding style that passes it, and how to write and answer review comments.

The examples below come from an invented inventory service: a Go HTTP service using an OpenAPI framework with validator tags and resolver methods (Huma, for example), MySQL for rows, and an analytical database for reports. Longer code examples are in [examples.md](examples.md).

## Opinions

These positions drive the checklist. When a case is not covered below, argue from them.

- **Consistency with nearby code is the default.** The first question about any new pattern is what the surrounding code already does. Deviate only with a reason stated in the code or the PR.
- **Code is a precedent.** People and agents copy the nearest example, so a workaround teaches the next change to work around. Fix a flawed pattern while it is cheap, and never write its second instance.
- **Readability beats unmeasured speed.** A slightly slower `lo.Filter` beats a hand-rolled loop until a measurement says otherwise. When a measurement justifies the faster form, write the measured win beside the code. A measured win justifies replacing a standard mechanism only when a customer or operator would notice it, and when it beats the best standard form, not the worst one (a hand-written JSON reader that beat `encoding/json` on allocations is not enough).
- **Do it now if it is free now.** A version marker, a constraint, or a hidden field costs nothing before customer rows or SDK users depend on it, and a migration or a deprecation cycle afterwards.
- **Omit rather than add.** A field, route, option, or index left out can be added later without breaking anyone. One put in cannot be taken back.
- **Deleting code is progress.** Remove dead code, spent gates, and redundant calls in follow-up PRs. Prove from the writer side that a state cannot occur, then delete the code that defended against it.
- **Name compromises in the code.** A `TODO` or `FIXME` that says what is awkward and when it goes away is better than a compromise nobody can see.
- **Review attention is the scarce resource.** Mechanical and behavioral changes go in separate PRs, so the risky half does not ride in on the obvious half's approval, and the routine half does not wait on the risky half's questions.
- **A failure nobody can see is not handled.** Every failure mode needs a counter, a span attribute, or a log line an operator would find.
- **Operators need levers without a deploy.** A setting someone would change during an incident, or a stored answer someone would stop serving, gets a runtime switch.
- **Fakes are as simple as the database.** A fake that validates or orchestrates hides production bugs. If a test needs a workaround, production has the same problem.
- **Few tests, each able to fail.** Coverage for its own sake is not a goal. A test that passes while its bug is present gives false confidence, which is worse than no test.
- **Pin the external behavior you depend on,** in code with a reason and in a test, even when it is already the default.
- **Say what the evidence covers.** State what was measured, at what scale, on which commit, and what was only read.
- **Unblock rather than hold for perfect.** Approve when the remaining items can follow in tracked PRs. Block the next change that entrenches a pattern already flagged.
- **Framework and standard library before in-house machinery.** The HTTP framework, `lo`, `slices`, `maps`, and `sync` already solve most of what gets hand-written.
- **Read agent output with suspicion.** Agents add low-value helpers, duplicate instead of reusing, handle errors over-eagerly, invent terms, and write docstrings that restate names or use semicolons. Agents also optimize the number they are shown: given an allocation profile, they write a custom parser to beat it. Check every instance.
- **Security code fails closed.** An empty, ambiguous, or mixed answer is a rejection.

## Writing rules

- **Comments:**
  - Default to no comment. Capture the single non-obvious idea, in one line where possible.
  - Don't glue a claim to its reason with a mid-sentence colon.
  - No file paths, line numbers, distant callers, transient context such as "for this PR", or symbol names that a rename would break. Before finishing, scan each comment for identifiers and ask whether it survives a rename.
  - Keep existing why-comments when you rewrite code. When in doubt, keep the comment.
- **Comments and prose:** Simplified Technical English. Lead with the outcome. Sentences of 25 words or fewer. Write contractions in full in comments and PR bodies. Active voice. No em dash, semicolon, or RST double backticks.
- **Tests:**
  - Each test gets `t.Run` cases with descriptive names, and covers one behavior (split, or use named cases).
  - A table test must pay for its abstraction.
  - Before review, check every added test for phases, names, assertions that can fail, and why-comments.
  - Prefer real constructors and existing fakes over bespoke scaffolding.
  - Name new comparison values `expected` and `actual`. Existing tests may be mixed, so follow the file you're in.
- **Design:**
  - Need before mechanism: state the evidence, whether skipping the mechanism is a one-way door, and what would reopen the decision. When the evidence is theoretical and the door is two-way, don't build.
  - Precedent needs a reason: an existing pattern alone doesn't justify a choice.
  - No guards for impossible states.
  - Challenge hand-rolled mechanisms.
- **Diffs:** no side-effect renames, moves, or rewording of existing code. No unused helpers or temporary hacks left behind.
- **Claims:** no latency or other numbers unless measured or sourced.

Where these conflict with the repository's own rules, the repository wins:

- **Comment width.** Some reviewers wrap new comments at 80 columns (tab = 4), others at about 100. Lint (`golines`) allows 120, and many existing comment lines already exceed 100. Keep new comments short, so they rarely reach 100, and never reflow existing lines.
- **One-caller helpers.** Some reviewers split long functions into named phase helpers even with one caller. The repository's instructions say to inline helpers used once or twice "unless they clearly improve readability". Split a long function only into phases that each carry real logic, and inline everything else.
- **Embedding test spies.** A rule against embedding wrappers in test spies may hold in one service and not another. Where the existing tests use spies that embed the store interface, keep that pattern there.

## Pre-reviewing a diff

Produce these three sections, in this order.

1. **Shape pass**, built from three worksheets before any finding is written:
   - **Identifier sweep.** A table of every identifier the diff adds or renames: Go types, functions, methods, receivers, constants, variables at package level, struct fields, JSON keys, query parameters, route paths, and OpenAPI schema names. For each, a verdict: keep, or the checklist item it fails and a shorter or more conventional candidate. Also list what has no name: each inline struct type and each literal that spells out a format. A sweep of names alone once skipped two inline types that wrote and read one versioned document.
   - **Formats.** For each persisted or versioned format the diff writes or reads, the one named Go type both directions use. Two types for one document, or a format that exists only as string literals, is a finding.
   - **Caller sketch.** For each new or changed route, write the request a client sends and the response it reads, as JSON. Read it as an external SDK user: which keys are long, which structures are awkward to populate, which fields repeat what the resource already says, which shapes will be hard to extend.
   - **Hand-written machinery.** List every hand-written schema, validation, limit check, conversion, encoder, parser, SQL assembly, and helper in the diff. For each, name what already provides it: a framework validator tag, a framework resolver method, the framework's generated OpenAPI output, the standard library (`encoding/json` and the `json.Marshaler` and `json.Unmarshaler` interfaces for formats), `lo`, or an existing repo helper or type. If nothing does, write "none". "Measured faster" is not "none": write what the measurement compared and whether it meets the bar in engineering-principles' Performance section. A hand-written reader once passed three reviews on the strength of a bytes-per-value number.

   Then apply every **Shape** and **Both** item in the checklist below to each changed file, and record findings with `file:line`.
2. **Runtime pass.** Trace each runtime path the diff changes and apply every **Runtime** and **Both** item: each error path, loop, retry, stored or public shape, each test's failing condition, each doc and PR-body claim. Record findings with `file:line`.
3. **Merged findings.** Deduplicate, tag each with its lens, and write each one in the comment form under "Writing review comments".

When the request caps the number of findings, give at least a third of the slots to each pass.

## When to block

Block on: code that can panic, MySQL `ENUM` columns, an edited migration that has already been applied, a schema change that leaves existing rows unusable without a backfill, and internal details exposed in a public response. Object strongly to a backfill inside a migration.

Everything else can be a follow-up. Approve when the remaining items can land later, say which ones should, and link the ticket for each. Use Request Changes only to hold a PR while a review is still in progress, and say so.

## Review checklist

Items are tagged **Shape**, **Runtime**, or **Both**.

### Names and vocabulary (Shape)

- Does the name follow the area's convention: `Get`/`List`/`Find` verbs, `*Key` for key structs, `ListItemsByWarehouse` shape for storage methods?
- Does it use the term other tools use? Aggregation, not reducer. `created_at`, not `birth_time`.
- Is it short? Drop redundant suffixes (`LabelsList` becomes `Labels`). Receivers and parameters are short when the type already says what they are (`id` for a warehouse ID parameter). Idiomatic Go prefers much shorter variable names than most agents write. A reviewer flagged a long name on a one-statement loop (`column -> c`). When the existing one-statement range loops are split between long and short names, write short names in loops you add, and don't sweep existing ones.
- Does it imply the wrong shape? `*Option` reads as a functional option. A plural reads as a collection. `Find*` implies nil on a miss. "Condition" implies a predicate.
- Is it one name for one concept? Two words for the same thing (`Block` and `Batch`) make readers ask whether they differ. A name scoped more narrowly than what it holds is a finding.
- Is it invented jargon ("stock horizon", "ledger pass")? A reader should know what it means without context.
- Does each domain word in the name mean here what it means elsewhere in the package? Grep the word in the package before using it, including in test names. A test named for a "capacity failure" covered a report that returned too many rows, while the same package uses capacity for a busy worker slot (`ErrNoFreeWorker`). Read a test's name against the error or input its body sets up, and name it in that term (`TooLarge`).
- Search the whole module, not the package, and search identifiers, not prose. For every domain word in a name you add or propose, including names you suggest in chat or a reply, list every identifier and string in every package with that word in any case style, singular or plural. Compare the new name against each one, especially sibling `max`/`default`/limit constants, error messages, env variables, JSON keys, span attributes, and OpenAPI schema names. An exact-name collision check is not enough. The name to worry about is a similar one with another meaning. `MaxReportGroups` was proposed for "groups one sales report returns", but the API package already had `maxReportGroupFields = 8` for the fields a request may group by. A glossary of one package missed it.
- When every candidate word is taken, name the value after an established compound term that already has one meaning in the code ("pick-list cap" gave `PickListCap`), instead of trying new words one at a time.
- Does a function's name state what its body does and what the caller gets back? If a reader would guess different behavior from the name, rename it.
- Offer candidate names as a short list. A rename that ripples through many files is a follow-up PR.
- Rename only what the PR introduces. A name the PR only moves keeps its old name, and its rename goes to a follow-up PR (a reviewer asked to keep names for a value the PR only moved to a separate follow-up PR). A restructure that another review point requires is in scope even when it removes a moved name.
- Short names suit locals whose use fits on one screen. A struct field or package name needs to say what it holds. If a field carries an inline comment explaining what it holds, rename it to the comment: `picked []uint64 // each warehouse's picked order count` should be `pickedCounts`.

### Helpers, abstractions, and duplication (Both)

- **Shape**: A one-use private helper is inlined. A test helper that wraps one struct literal is inlined. Agent-written code tends to add low-value helpers and noise abstractions, so check each one.
- **Shape**: Does a new type duplicate an existing type with the same or similar fields? Two near-duplicate queries or methods become one parameterized method.
- **Shape**: A new interface: can its methods go on an existing interface? A fake-only type that extends a runtime type is suspect.
- **Shape**: Is there an existing helper or constructor? Name it and link it (`lo.Map`, `slices.IndexFunc`, the package's storage error mapper).
- **Shape**: A new helper or value that is useful beyond its file goes in the shared package for that domain (for example a shared `jsonutil` package) and is exported there. A shared value that must not change is exposed through a function, such as one built with `sync.OnceValue`, not as an exported variable.
- **Shape**, counterweight: an abstraction that removes real duplication or adds type safety earns its place, such as a shared embedded struct, a generic marker type, functional options, or a logging wrapper `Store` instead of logging in every method.
- **Runtime**: Parallel implementations drift, because bug fixes land in only one of them. Two structs that must keep identical JSON tags with nothing enforcing it. One rule written in two places. A sibling worker or path that needs the same fix. Agent-written code tends to duplicate instead of reuse.
- **Runtime**: A long function or file is a finding. A helper that names a real step is fine (see "Balancing the two lenses").
- **Shape**: Does each new file have a reason beyond its topic? List the files its functions are called from and the files they call into. A file that holds one function, called from one file and calling into another, usually belongs with its callees (a reviewer will ask whether it needs a file of its own). Keep it when the package lays files out by entity or layer, or when the file holds a type with its methods or a separately tested unit. Ask again after a restack moves code into an earlier PR: a file sized for a later PR's logic can shrink to one small function.

### Which layer owns the logic (Shape)

- Storage methods run one SQL statement. No validation, conversion, orchestration, or transactions, in the real store or the fake. Existence and conflict checks go into the statement itself.
- Request constraints go on the API type as the framework's validator tags (`enum`, `minimum`, `maximum`, `maxLength`, `default`), which the framework also publishes in the OpenAPI spec. Limit checks should not be hand-written each time. What tags cannot express goes in a resolver method on the request type. A service-level check is right when a non-HTTP caller also needs it.
- API handlers stay thin. Parsing belongs in tags and resolvers, and reading or orchestrating belongs in a service method that other service methods can reuse. The service owns orchestration, transactions, and ID generation.
- Hand-written code that reproduces what the framework generates or checks (schema definitions, OpenAPI names, validation) is a finding. Several operations that share a path prefix, tags, or middleware are registered through one route group, not by repeating the settings on each operation. So is any hand-written machinery the standard library already provides, such as a concurrency limiter.
- A free function whose first argument is `*service`, `*Store`, or `Config` becomes a receiver method. Behavior about a type is a method on the type, which also prevents reversed-argument mistakes.
- Encoding belongs on the type through `json.Marshaler` and `json.Unmarshaler`. A standalone `encodeX` or `decodeX` beside the type needs a stated reason in its comment, such as decoding that depends on outside context the bytes do not carry. Even then, the standard library parses the bytes. The custom code is only the `switch` that picks the type.
- A method that takes an index only to reach into several parallel slices on its receiver hides its inputs. Pass the elements. When an error needs the index, the caller sets it, as the order validator sets `LineError.Line` after checking one order line.

### Public API and stored formats (Both)

- **Shape**: Read every request and response as the external user who must build and parse it. A structure that is verbose or awkward to populate in client code is an awkward API.
- **Shape**: JSON keys, query parameters, and paths are names too, and get the same brevity test as Go identifiers. Long route paths are a finding. Generic concepts get generic names: filters, sort, after, and limit are generic search parameters, and their types should not carry one resource's name.
- **Shape**: A structure that is awkward to compose or extend, or that locks an internal detail (a position, an ordering key) into the public contract, is a finding. Callers will depend on anything public. Model `not`, and later `and` and `or`, as expression nodes that wrap other expressions, never as a boolean field on a leaf filter.
- **Both**: A field has one meaning in every variant of its record and at every stage of the pipeline. A field that holds a sum at one stage and a mean at another, or a final value for some aggregations and an intermediate one for others, is a finding. Store the parts under their own names, or compute the derived value when the response is built. Do not add a field that an existing aggregation, such as count, already provides.
- **Shape**: It is much easier to omit a field now and add it later. A count is an aggregation, not a field. Client calls read `client.<resource>.<action>`, e.g. `inventory.items.get()`. Types follow JSON Schema semantics: `integer` vs `number`, no integer inferred from `7.0`, null vs missing. Closed sets are enums, not free strings. IDs are ID types.
- **Shape**, GraphQL: `@oneOf` inputs, `@deprecated` with client updates in separate PRs, nullability changes treated as breaking for the SDK and mobile app, and extending an existing field before adding a parallel one.
- **Runtime**: A persisted format gets a version marker, such as `{"version":1,"result":{...}}`. Adding one before customer rows exist is free.
- **Runtime**: A digest or idempotency key built from a Go struct encoding or a formatted URL breaks retries on a routine refactor.
- **Runtime**: A response field is a one-way door once the generated SDKs carry it. Taking a field away is a breaking change, and adding one is not. Internal operation IDs do not belong in public responses. A route a frontend or SDK still calls is deprecated before it is removed. Say so when a change is cheap to reverse, so it is not treated as a one-way door.

### Docs, docstrings, and comments (Both)

- **Shape**: Any semicolon in a docstring, comment, or doc is a finding.
- **Shape**: A sentinel error's name and message carry its meaning, so it needs no docstring. Before writing one, reword the message until it says what the docstring would: `var ErrStaleCount = errors.New("stock count changed after it was read")` replaced a docstring over "count is out of date". Keep a docstring only for what a message cannot say, such as which system produced the error (an error that means the upstream auth service rejected the request with HTTP 400).
- **Shape**: Would the docstring be useful to someone who sees only it and the signature? A cryptic docstring, one that restates the name, or one that describes details outside its scope is dropped or rewritten. A doc should be clear on first read.
- **Shape**: Operation descriptions are the public OpenAPI spec for external callers. Field rules, error-code details, and examples belong on the fields, their tags, or the response codes, not in the operation description. An overloaded operation description is a finding.
- **Shape**: A Markdown doc that lists operation or method names invites churn or drift. The source of truth is the Go docstring next to the code.
- **Runtime**: A setting, lock, or guarantee applied to some calls and not others says in its comment why the others go without it. A `readLatest` helper's comment explained what the database setting it turns on does, and a reviewer asked why every other read may miss recent rows. The answer, that a missed row on a list page lasts one request while a stored snapshot keeps it, lived only in a design doc.
- **Runtime**: Is the intent of each non-obvious branch written down? When an author explains a choice in a review thread, the explanation goes into the code comment.
- **Runtime**: Does every comment, doc, and PR-body claim match the code at this head? A doc comment that now describes the opposite of the code, or a stale PR body, is a finding. The squash commit inherits the PR body. Renames go stale in bodies without anyone noticing: after a rename of an internal helper, three PR bodies still named the old one a day later, when a reviewer asked why a helper existed. Check every backticked identifier in a title or body against the head.
- **Runtime**: Keep comments that record a persisted-format contract, the measurement that justifies a limit, or honest uncertainty about a default. The measurement is what justifies a limit. The arithmetic only describes it.

### Tests (Both)

- **Shape**: Can a reader paraphrase what the test does after a 10-second scan? One field per line in table cases with four or more fields, or three when one holds a call: a reviewer gave up on one-line `{name, left, right, want}` cases and praised the one case the formatter had wrapped (dense one-line cases read as code golf). A uniform list of two-field scenarios can stay one per line.
- **Shape**: `require` for preconditions, `assert` for the outcome under test. `assert.JSONEq`, `assert.ElementsMatch`, and `assert.Subset` where they fit.
- **Shape**: No assertions on verbatim error strings, migration file text, or unexported functions. A test of an unexported function points to a design to reconsider. A package whose tests commonly call unexported code does not excuse a new one: ask whether the behavior belongs on a standard interface (then test through `json.Marshal`) or should be observed through the public path (an unreadable stored snapshot falls back to a full recount).
- **Shape**: When many test helpers exist only to build one type's values (`intAttr`, `boolAttr`, `stringAttr`), the helpers are a symptom. Ask why the type is verbose to build: a tagged union such as `AttrValue` that needs `Type` plus a pointer per value, or a closed set whose states could share one generic type (`MinOf[T cmp.Ordered]`). Fix the type or inline the literals. Do not rename the helpers.
- **Shape**: Don't grow a spy one test at a time. Before adding a behavior field (canned results, waits, injected errors) or a new spy type, reuse an existing one as is, or consolidate the overlapping ones. Counters and recorded inputs are cheap; behavior knobs pile up (a reviewer counted the hacks piled onto one test double).
- **Shape**: No test-only setup that production cannot reach. Test logic that duplicates production logic means the test or the design is wrong. Fixtures elaborate enough to rival the code under test are overabstracted.
- **Shape**: Prefer fewer, clearer tests. Deleting a low-value test is often the right review ask.
- **Runtime**: Would the test fail on the bug it is meant to catch? Metrics for two conditions that could be swapped, rows paired by position, or a case named for something it never touches all pass while the bug is present.
- **Runtime**: One behavior per test, so a failing test's name says which behavior broke. Real data over mocks when the setup can be real. No flaky constants or sleeps. Cover the paths that skip or fail.

### Error paths, stuck states, and observability (Runtime)

- A parameter a helper uses only to label its error message belongs to the caller: the caller wraps the error with that context (`fmt.Errorf("stored snapshot: %w", err)`), and the helper drops the parameter (an `indexByWarehouse` helper took an `origin` string only for its error text). A check that is the only reason a helper takes an argument is likely in the wrong place.
- When an error path changes, compare what it logged, counted, or traced before and after. Dashboards and alerts may depend on it.
- Can an operator tell how often this happens? Each outcome gets a counter or a span attribute. The counters still add up (received equals processed plus skipped plus failed). Two series answering one question is a finding.
- Log noise counts: one traceback or one INFO line per message consumed.
- A failed write that leaves a resource frozen. A loop or retry with no attempt limit or no exit except shutdown. A state that nothing clears. A cleanup or slot release skipped when something above it raises. Side effects the old error path had that the new one drops. Partial failures handled inconsistently. User error vs system error, and what the user then sees.

### Runtime, deploys, and configuration (Runtime)

- Consumer health: Kafka `max.poll.interval.ms` eviction, synchronous `poll`, the shared default thread pool starving the consumer. Deploy shape: rolling vs Recreate, replica lag, worker count, graceful shutdown.
- Is each number arbitrary, or does it need to match something? Prefer an expression that derives it.
- A setting someone would want to change while production is misbehaving must not need a deploy.
- A stored or precomputed answer needs an off switch, and the switch must also stop the precompute.

### Schema and migrations (Both)

- **Shape**: No MySQL `ENUM` columns. ClickHouse `LowCardinality(String)` over `Enum`. No backfill inside a migration. No premature index. No dual-write table that doubles failure modes and sources of truth. `DEFAULT NULL` over a silent default.
- **Runtime**: Never edit an applied migration. golang-migrate never re-runs it, so existing databases go stale silently. Check migration numbers against other open PRs. A schema-only forward migration that leaves rows unusable needs its backfill. Initdb scripts run only when the volume is created.

### Go idiom and SQL (Shape)

- Two or more results plus an error suggest a function doing several things. Return a named struct.
- Never store a `context.Context` on a struct. `defer tx.Rollback()` directly, with no closure.
- Test a value against its zero with `lo.IsEmpty(x)` or `lo.IsNotEmpty(x)`, never `!lo.IsEmpty(x)` (a reviewer asked for this in place of `order.WarehouseID != (ids.Warehouse{})`). Where the codebase still has explicit `!= (T{})` comparisons, use the `lo` form in lines you add, and leave the existing ones.
- Bare loops that filter or map become `lo.Filter`, `lo.Map`, `lo/it`, or `slices`/`maps` calls. Readability beats the small speed win of an inline loop. A set type (such as `go-set`) over `map[T]struct{}` where the package already uses one. `switch` for enum dispatch.
- A method's name and receiver must agree. A verb (`Update`) on a pointer receiver changes the receiver. An `-ed` name (`Merged`, like `slices.Sorted`) on a value receiver returns a new value. `Update` on a value receiver that returns a copy is a finding. When choosing between them, prefer updating in place if the caller owns the value and no caller reads the old one afterwards. Trace the callers before claiming either way. A copy is justified by a caller that keeps the original, not by hypothetical aliasing.
- Handle a nil pointer argument with the zero value, not a stand-in object (an `emptyOrder` value): `lo.FromPtr(p)` for its fields and `lo.NthOrEmpty(s, i)` for a slot, when nil and zero mean the same thing. If nil handling takes more lines than the non-nil path, the design is off.
- SQL assembled in Go: whole `SELECT` statements, not CTE fragments, which compose poorly. Use one substitution mechanism, with values bound as native parameters. Do not mix `Sprintf` verbs, templates, and SQL parameters, do not store `%`-verb fragments away from the `Sprintf` that fills them, and do not mix `strings.Join`, `+`, and `Sprintf`. Fragments with unclosed parentheses are brittle. Interpolated SQL invites injection. Values go in as parameters.

### Scope, reviewability, and other PRs (Runtime)

- A refactor mixed with a behavior change is split, because different people spot problems in each. A part with an unmeasured cost gets its own review. Count the separate ideas in a large PR. Prefer more small PRs to bigger or longer-running ones.
- Diff churn with no semantic change, such as reindenting a block, is a finding.
- Search the other open PRs for every shared constant, gate, and call site the PR changes, and read their measurements and conflict notes. One PR raised a concurrency limit unmeasured while another open PR had already measured that limit and named the first one. A gate moved into a helper while another PR still acquires it on the same path takes two slots per request and waits for itself at a limit of one.
- Check how the PR interacts with other open PRs: merge order that decides what customers see, a newly required prop another PR does not pass, positional constructors plus a concurrent field insertion, two PRs defining the same make target.

### Performance and benchmarks (Both)

- **Shape**: Performance findings are non-blocking unless measured. Readability usually wins over an unmeasured speedup.
- **Runtime**: A benchmark must be reproducible from a pushed commit and exercise the changed layer. Watch for a benchmark that runs against the fake store (so a schema change measures as zero), bias from run order, a peak divided by the iteration count, setup inside the timed loop, and the Go test cache serving stale results.

### Correctness and precedent (Both)

- **Shape**: Code that can panic. NULL, nil, and boundary cases found by reading. `nil, nil` for not-found, instead of a not-found error. Leaking the existence of a restricted resource.
- **Both**: Future agents copy what they see. Accept one instance of a flawed pattern if you must, but block the next change that entrenches it.

## Writing code

### Balancing the two lenses

| Tension | Resolution |
| --- | --- |
| Inline one-use helpers, yet break up long functions. | Split a long function only into steps that each carry real logic and a name the caller reads as a sentence. Never add a helper that wraps one call or one literal. |
| Prune tests, yet catch the bug. | Fewer tests. Each checks one behavior and fails if that behavior regresses. |
| Delete restating docstrings, yet keep "why" comments. | Keep contracts, reasons, measured numbers, and stored-format warnings. Delete text that restates the name or the code. |
| Readability over speed, yet measure. | Write the readable version. Optimize only with a measurement from a pushed commit. |

### Go

- Typed closed sets: a named string type with constants, a `switch` whose `default` returns an error, `var _ I = (*T)(nil)` assertions, typed IDs (`ids.Warehouse`), and discriminated JSON unions with explicit `MarshalJSON`/`UnmarshalJSON` that reject a missing discriminator.
- Service layering: API types carry request constraints. The service owns transactions (a `WithTx` helper), ID generation, and orchestration. Storage runs single statements. The fake stores the same row types as MySQL and enforces nothing MySQL does not.
- Errors wrap a package sentinel with `%w` and context: `fmt.Errorf("%w: invalid shipment address: %w", ErrInvalidRequest, err)`, with short verb-phrase wraps ("generate order ID: %w"). Storage errors are translated once, by one mapping function. Use `errors.Join` for a secondary failure. Another service in the same repository may have its own error package: follow the one in the package you are in.
- `context.WithoutCancel` for bookkeeping that must finish after the caller cancels, with a comment saying why.
- One `...Input` struct per storage method. Narrow interfaces per capability, with compile-time assertions. Keep a method off a public interface until a later PR wires it in.
- SQL lives in embedded `.sql` files. Pin each database setting you depend on with a reason (ClickHouse `*_overflow_mode: "throw"` so a limit cannot commit a partial result). Set the query ID to the attempt ID so operators can find it in the database's query log. Scope the zap logger with IDs.
- Give every exported symbol a doc comment written as a contract, including the state left after an error. Sentinel errors are the exception: their message is the contract (see "Docs, docstrings, and comments"). Put "keep in sync" warnings at both ends of a deliberate duplicate.
- Each goroutine gets its own timeout context and panic capture. Tests prove the side effects still happen after a panic.
- Use the package's helper idiom: `lo` and `lo/it` where the package uses them, the local slices helper package where one exists, stdlib `slices`/`maps` everywhere.
- Comments give the reason and product context. Mark your own compromises in the code: `TODO(name): Remove this once ...`, `FIXME: This is awkward and not extensible`.
- Use generics to collapse repeated boilerplate, such as one `parseAs[T]` instead of a switch of decode calls, and reuse existing generic types (a `Page[T]`) before writing parallel ones.
- Carve a small interface out of a large one for each consumer (`StockReader`, `StockWriter`), and document its contract, e.g. `Returns (0, false) if the warehouse does not stock the item`.
- Prefer generated tracing, stats, and logging decorators over hand-written spans. Log with structured fields at Debug, capture real failures with context, and tag stats with every dimension a dashboard will split on.
- A fake panics on an input it does not support, instead of returning a plausible wrong answer.
- Go 1.26 `new(value)` replaces a local `ptr()` helper. `lo.Must` is fine in tests.
- Put performance fixes in the query: index hints, keyset pagination, `deleted_at IS NULL` in joins, deduplicated lookup IDs, skipping the query when the limit is zero. Avoid caching layers.

### Style by example

[examples.md](examples.md) holds invented examples of the style the rules above describe:

- **A closed variant type**: a JSON union whose wire forms carry only the ID their kind selects, and whose decoder rejects a missing or unknown kind.
- **Bookkeeping that must outlive the caller**: `context.WithoutCancel` with a one-line reason, a logger scoped with IDs, and `errors.Join` for a secondary failure.
- **A deliberate duplicate, a contract docstring, and pinned settings**: a drift warning at the declaration, a query ID operators can search for, and a pinned database setting with its reason.
- **A spy that embeds the store**: a test double that records one method's input and delegates, plus one test run against the fake and the real stores.

### Python and TypeScript

- Python: an error hierarchy per subsystem, mapped to a closed set of metric labels. Frozen dataclasses, `Protocol`, `Literal`, PEP 695 generics. Explicit `len(x) == 0` and `is None`. Security code fails closed. Metric attributes are closed enums and never carry customer identifiers.
- TypeScript: discriminated unions with an exhaustive `switch` and a `never` default. Pure functions pulled out of hooks, so they can be tested without rendering.

### Tests

- Run one contract suite against the fake and the real stores (one helper that runs a test function against each store). Prefer hand-written fakes over mocks. A spy embeds the store and records one method's input.
- Table tests with sentence names (`"replays the same request"`) or behavior-sentence test names (`TestRecountRecordsAFailureAfterTheCallerCancels`), one behavior each. Put a new test file in the external `_test` package, and test through the package's exported API or its callers. A test-only alias is a last resort: first check whether an exported caller shows the behavior, and if callers need guarantees on the function itself, export the real function. Add an alias only when neither keeps the coverage, and give the reason in the PR description. Aliases the package already has do not justify a new one.
- Wait on events, not sleeps. Root-cause flakes instead of retrying them. Turn a lesson into a repository check.
- Delete tests that pin error strings, and helpers that peek at the database with raw SQL.
- Test a schema by running the migration and querying the resulting schema (sort key, nullability, TTL), not by reading the migration file.
- A repository check's failure message names the exact error it prevents.

### Operations

- Add kill switches and caps before exposure.
- Merge dormant code behind `TEMP(ticket)` gates, then remove the gate in a small PR.
- Make redelivery harmless with deterministic IDs.
- Drain in-flight work on SIGTERM, with a grace period sized from production p99.
- Return 404 instead of 403 so a caller cannot learn what exists.

## PRs

- Keep PRs around 200 changed lines where the work allows. Land mechanical changes and behavior changes separately.
- Sequence the work: tests first, then the refactor. A move-only PR, then the behavior change. A rename as a "prep" PR before the feature. Add, migrate, then delete, as three PRs. A flag, then the feature, then activation. One PR per resource or per service.
- Deleting code is progress. Follow a feature with the PRs that remove what it made redundant.
- Title type follows reachability: `chore` for code nothing reaches yet, `feat` for turning it on.
- A split that produces a prep PR can leave a helper with one caller at that PR's own head, which fails the one-use rule where it is reviewed. Say in the prep PR's body and the helper's comment which child grows it and why the move is worth its own review, or leave the move in the child (a reviewer will ask why a helper with one caller exists).
- PR body: What, Why, How, Testing. State what changes on merge and what does not. Give production evidence with counts and a dashboard link, measured alternatives with the condition that would reopen the choice, and `file:line` proof of any invariant the change relies on. Name each test with its exact command and pass count. Say what an operator must do after a failure.
- When a review point is easier to show than describe, open a closed example PR against the reviewed branch, one finding each, and say it is not meant to merge.

## Before pushing your own change

The goal is a diff that a reviewer holding these opinions has nothing to comment on, nits included. Rules fail when they are applied to categories instead of instances, so work per instance.

1. **Run the Shape worksheets on your own diff:** the identifier sweep, the caller sketch, and the hand-written machinery list from "Pre-reviewing a diff". Every row gets a verdict.
2. **Walk the checklist per instance.** For each changed file, go through every checklist heading and name each instance it applies to in that file: each new name, helper, type, field, docstring, operation description, SQL builder, test, error path, loop, stored shape, and number. Fix every instance that fails, including the ones that look minor. A reviewer who comments on nits will comment on the one you skipped.
3. **Run the Runtime pass** on your own diff, as in "Pre-reviewing a diff".
4. **Read your own PR body** against the code at the pushed head. Every claim must be true of that head.
5. **Walk test tables and loops line by line.** In one round, three of a reviewer's four mechanical comments (one-line table cases, `package x_test`, a long loop name) quoted rules this skill already held. The stack had been swept several times, but every sweep read tests for correctness and leaned on scripts that did not check these rules. Read each table case, loop, and test file's package line against the Tests and Go idiom headings.
6. **Run the mechanical checks last, as a backstop**, where the repository has them. Steps 1 to 5 are judgment and come first: the scripts are narrow and miss most of what a careful reader catches (the user's own finding). Treat each flag as a question for that instance:
   - A repeat-check script for semicolons in comments, docstrings that open with their own name, API helpers that return `*Response`, deleted base tests, stale references to removed routes, JSON written or matched by hand, inline struct types with JSON tags, and test helpers that only return one literal.
   - A PR-body check for identifiers a title or body names that the head no longer has.
   - `golangci-lint run --new-from-rev BASE` with only `revive`'s function-result-limit rule enabled, for functions that return more than two values plus an error.
7. **Check your own reply before posting it** against "Replying to review".

## Choosing what to raise on a colleague's PR

The checklist above finds candidates. On someone else's PR, an approval with no comments is common. Offer every candidate the questions below raise, with the ones the user is most likely to post first, and list the rest under dropped, each with its reason, so the user can still pick one. When the questions drop every candidate, offer none and recommend approval.

Another reviewer's broader ask does not cover your narrower point. If they asked to move the tests to the public path and you found two inputs no request can produce, raise yours on its own lines: the author may not take theirs.

Ask these questions in order for each candidate. The user would rather delete an extra draft than miss a point, so when a question leaves you unsure, raise the candidate. A candidate on the "When to block" list above blocks, whatever the questions say.

1. **Can the author act on it in this PR, and has nobody made the point yet?** A comment can change the PR's own lines, lines the PR makes false, and places its change missed. If yes, go to question 2. If no, drop it as "already raised" or "not in this PR". Examples of no:
   - A reviewer or the review bot made the same point at the same place, in a comment you can read. A summary that mentions the topic does not count, and neither does a different fix for the same code or a thread you cannot read. In those cases draft the point, and say in the report what the summary claimed. When your point is narrower or adds a fact, raise it, or reply in their thread.
   - The PR body or title, a PR that already merged, or a gap that a later PR in the stack fills. When a probe shows a body claim is false, put that in the comment about the behavior.
2. **Would a caller, an operator, someone running a tool or test, or an agent following the text get a wrong result, a hang, or a cost someone measured?** These people act on the result without asking the author. If yes, go to question 3. If the harm needs a value the code rules out, or only a test fake breaks, drop it as "nothing produces it". If no, go to question 4. Examples of yes:
   - A fix tool that makes a call run twice.
   - A test that hangs, or that keeps passing when its code breaks.
   - A benchmark that adds minutes to every run, or that times the wrong work.
   - Text that is false after the PR, or that reads two ways.
3. **Did a request you ran show callers a wrong or changed response today?** Customers already rely on the old response. If yes, block, such as for a 422 that no longer names the bad field. If no, go to question 5.
4. **Will the next reader or editor pay for it?** That cost comes back with every later change, and the fix is cheap now. If yes, go to question 5. If no, drop it as "not worth the author's time". This covers naming opinions, one-line style or layout, rewording text that is true, asking for more docs or tests for code that works, speed nobody measured, and arguing against a choice the PR explains. Examples of yes:
   - The line does nothing: a check the PR adds that repeats one its only caller makes, a guard for an index nothing writes, a `CODEOWNERS` line that a broader line already covers, a new test of an input no request can produce, or machinery no caller asked for. For a check or guard the PR adds, say what already rules out the case it checks, and add an empty suggestion block that deletes it.
   - The code redoes or departs from what the repository has: an existing helper or library rule, two copies that must agree, the naming of its file, package, or the standard library, or an `AGENTS.md` rule.
   - The reader has to assemble it: an empty value that means success, one test that runs two paths, a long sentence.
5. **Does the fix depend on something only the author knows, such as whether a behavior is wanted?** The diff cannot answer it. If yes, ask a question, such as "Does the error need to name the first bad column in spec order?" If no, raise it with the fix, labeled as "Writing review comments" describes.

## Writing review comments

- **Small findings** (names, helpers, docstrings, idiom) take a bold Conventional Comments label: `**suggestion:**`, `**question:**`, `**nit:**`, `**note:**`, `**praise:**`, with qualifiers such as `(non-blocking)`, `(if-minor)`, `(follow-up)`, `(blocking)`. `note` means out of scope and recorded for later. Pick the label by what you expect the author to do. If the code behaves the same before and after your change, the label is `nit`. In detail: `nit` for a take-or-leave edit (a readability rewrite, the wording of a doc or rule, an idiom one-liner, a line or check with no effect), `suggestion` for a change you think should happen (a test that cannot fail, a reuse, a slow default), and `question` when the ask depends on the author's answer. Keep them to one to four sentences. Ask for intent when two readings lead to different fixes. Add a suggestion block for a rename or replacement docstring. Mark scope ("here and everywhere in this PR"), and mark pre-existing code as optional or a follow-up.
- **Substantive findings** (a failure you reproduced, a missing invariant) are self-contained, so a reviewer can evaluate them without private context. Recent comments the user posted write these as plain paragraphs after the label (what fails, how you showed it, the fix), with a base-versus-head table when the change is in responses. The bold headings below are optional:

  ```markdown
  <required prefix, if any> **Problem.** <the concrete failure, in one sentence>.

  <current mechanism and observable consequence: the functions, types, tables, statuses, HTTP responses, and tests involved>

  **Solution.** <smallest change that removes the cause>. <disposition>
  ```

  Add `**Impact.**`, `**Precondition.**`, `**What actually happens.**`, or `**Why it also costs operators.**` only when the sequence or a second-order effect would otherwise be unclear. Distinguish an existing defect from a regression risk.
- **Dispositions** end each substantive finding: "Fix in this PR.", "Fast follow.", "Can defer.", "Not blocking.", "Blocks approval.", "Pre-existing issue, consider follow-up PR.", "Pre-existing issue, but made worse here."
- **Evidence.** Verify against the current head and anchor the comment to the line that exposes the mechanism. Link sources pinned to a commit SHA. Run a probe when possible (a container, a script, a measured output block), and say plainly when you could not: "Verified by reading, not measured against a database." Say what you checked and found fine. Ground severity in real deployment conditions, and name when a deferred item becomes urgent.
- **Options.** In the report, give two or three fixes with their costs, including doing nothing or documenting, and name the one you prefer. The comment proposes the preferred fix, with a fallback only when the first may not fit. For a performance fix, name the measurement that decides it and the result that would change the answer, instead of prescribing the change.
- **Ask, don't order.** State each fact flatly once the evidence supports it. Then phrase the ask as a question: "Could this test use one spec per name?", "Can we drop it?", "Thoughts on including these in this PR as well?". Use a plain request ("Please ...", "Fix in this PR.") only for a bug you reproduced that the PR introduces. Do not hedge the facts themselves ("I wonder if").
- **Questions for intent.** When the right change depends on what the author meant, or on a choice you cannot check, open with `**question:**` and ask it: whether several copies should share one definition, which tool agents should default to, whether an input needs validating, whether a comment should exist at all. Offer at most one candidate, introduced with "If ..., something like ...". When the author made a choice you would not, ask what you are missing ("Was there a reason to prefer X that I'm missing?").
- **Taste in the first person.** For readability, naming, and test-structure points, say whose reading it is ("reads to me as database tables", "I feel like this test does a bit much"). When the current form also works, say so in one clause ("Same output, so fine to keep the current form"). A line or check with no effect is a fact, not taste: say what makes it dead and add the deletion block, with no question and no out.
- **Length.** A suggestion or nit holds the fact, its consequence, the ask, and the code, in about 40 to 120 words outside code blocks. Keep at most one sentence, inside the text, on what you ran ("A probe with a spread call and a plain call reports them as copies"). Rejected alternatives, caveats about tooling, and notes such as "the rename is mechanical" go in the report. For a problem that repeats, list up to five places and say how many there are. When a sweep or rename missed places, do not hold the PR description against the author. Give a likely reason ("that may have been added by other PRs recently") and ask whether to include them.
- **Smallest edit.** Ask for the smallest change that fixes what you showed, not the larger cleanup it hints at: drop the two impossible inputs, not "move every test to the public path". For wording, change only the words your point is about and keep the author's sentence. For code, rewrite only the lines the point needs, not the whole function.
- **One point per comment.** A smaller finding folded under a larger one stays in the report as a sub-item. The comment carries only its own point. A sub-item that stands on its own, at another place or with another ask, gets its own draft instead of riding inside a larger one. Name a missing test case in one sentence ("A spread and plain pair in the existing table would pin it"), and paste test code only when it is a few lines.
- **Report formats stay in the report.** A posted comment has no **Now:**/**Want:** pair (use a suggestion block), no support label, no "Measured:" or "Checked by reading" trailer, no citation of a skill, and no edit to the PR body. Quote a repository rule in one line only when breaking it is the point.
- **Standards.** One root cause per comment. When the same contract is duplicated across a stack, raise it once and scope the fix across the affected PRs. Connect the detail to customer, operator, or maintenance impact, because "cleaner" or "brittle" alone is not a reason. For a missing test, name the case and the regression it would catch. Mention another PR, TODO, or draft only when it changes the fix, and never invent a link. Keep length proportional. No praise, throat-clearing, severity theatre, or private-session history. If there is no actionable finding, post nothing. Preserve any required prefix exactly.
- **Review body.** Say what to read first. Post a partial review early rather than let comments sit unseen. Give conditional approvals ("Approved, assuming #N merges first"). A short thanks is enough when nothing needs flagging.

## Replying to review

- Answer the question before proposing a change. An existing name or shape may be fine.
- When you keep the code, give a concrete reason: the interface it implements, the non-HTTP caller, consistency with nearby code, or a measured cost. Answer with evidence where possible: a Go playground link, docs, a measured timing.
- When you concede, change the code, link the commit, and name the test that pins the change. When the reviewer's question showed the code was unclear, put the explanation into a code comment, not only the thread.
- When you defer, link the ticket from the code and the ticket from the PR.
- When the ask is vague, ask what specifically is unclear.
- A second reviewer raising a point you skipped is a reason to change it.
- Before saying a fix is pushed, check that it is.
