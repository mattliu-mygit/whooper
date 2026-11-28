import type { Cause, Connections, Drafts, Examples, FindingsResult, Overview, Shown, TestGaps, WhooperStatus } from "./protocol";

// Invented reviews for the fake host: made-up repos, people, and code, never a real PR.

/** The record of one review run, as the fake host reads it from the run's directory. */
export interface RunRecord {
  run_id: string;
  repo: string;
  pr: number;
  title: string | null;
  author: string | null;
  head_sha: string;
  outcome: Shown["outcome"];
  /** findings.json, null for a review that runs, waits, or failed without findings. */
  findings: FindingsResult | null;
  /** drafts.json, when the user changed something; whooper-draft computes them otherwise. */
  drafts?: Drafts;
  /** diff.patch, null for a review that waits. */
  patch: string | null;
  failure: string | null;
  cause: Cause | null;
  asks: Shown["asks"];
  /** What whooper-show adds for a review that wrote an overview, and its other views; each part left out
   * prints as missing (null, or [] for co_change). */
  extras?: Partial<Pick<Shown, "overview" | "commits_since_review" | "co_change" | "connections" | "test_gaps" | "examples">>;
}

const PATCH_101 = `diff --git a/internal/cacheconfig/config.go b/internal/cacheconfig/config.go
new file mode 100644
index 0000000..328d4e1
--- /dev/null
+++ b/internal/cacheconfig/config.go
@@ -0,0 +1,25 @@
+package cacheconfig
+
+import (
+	"errors"
+	"time"
+)
+
+// Config is how the server reaches its cache.
+type Config struct {
+	Addr       string
+	TLSMode    string
+	TTL        time.Duration
+	MaxEntries int
+}
+
+// Validate reports the first setting Open cannot use.
+func (c Config) Validate() error {
+	if c.TLSMode == "" && c.Addr != "localhost" {
+		return errors.New("TLSMode must be set for a remote cache")
+	}
+	if c.TTL <= 0 {
+		return errors.New("TTL must be positive")
+	}
+	return nil
+}
diff --git a/internal/cacheconfig/config_test.go b/internal/cacheconfig/config_test.go
index 4558913..4012ccf 100644
--- a/internal/cacheconfig/config_test.go
+++ b/internal/cacheconfig/config_test.go
@@ -9,11 +9,13 @@ import (
 func TestOpenRejectsInvalidConfig(t *testing.T) {
 	cases := []struct {
 		name string
-		cfg  store.Config
+		cfg  Config
 		want string
 	}{
-		{"remote plaintext", remotePlain(), "TLSMode"},
+		{"remote plaintext", remotePlaintext(), "TLSMode"},
 		{"zero ttl", zeroTTL(), "TTL"},
+		// Validate must run before Open dials the cache.
+		{"pool bound", poolBound(), "MaxEntries"},
 	}
 	for _, tc := range cases {
 		t.Run(tc.name, func(t *testing.T) {
@@ -34,3 +36,15 @@ func TestDefaults(t *testing.T) {
 		t.Fatalf("MaxEntries = %d, want 10000", cfg.MaxEntries)
 	}
 }
+
+// TestProductionDoesNotImportStore keeps the store package out of production code.
+//
+//nolint:paralleltest // reads the module graph once
+func TestProductionDoesNotImportStore(t *testing.T) {
+	pkgs := loadPackages(t, "./cmd/...", "./internal/...")
+	for _, p := range pkgs {
+		if imports(p, "example.com/widgets/internal/store") {
+			t.Errorf("%s imports the store package", p.PkgPath)
+		}
+	}
+}
diff --git a/internal/server/server.go b/internal/server/server.go
index 9897331..922830f 100644
--- a/internal/server/server.go
+++ b/internal/server/server.go
@@ -1,11 +1,15 @@
 package server
 
 import (
+	"example.com/widgets/internal/cacheconfig"
 	"example.com/widgets/internal/store"
 )
 
 // New returns a server that reads through the cache.
-func New(cfg store.Config) (*Server, error) {
+func New(cfg cacheconfig.Config) (*Server, error) {
+	if err := cfg.Validate(); err != nil {
+		return nil, err
+	}
 	c, err := store.OpenCache(cfg)
 	if err != nil {
 		return nil, err
`;

const PATCH_107 = `diff --git a/internal/store/writer.go b/internal/store/writer.go
index 252ae41..f1abd4c 100644
--- a/internal/store/writer.go
+++ b/internal/store/writer.go
@@ -2,8 +2,9 @@ package store
 
 // Write stores rows for one table version.
 func (w *Writer) Write(version int, rows []Row) error {
-	if version < w.minVersion {
+	if version <= w.minVersion {
 		return ErrStale
 	}
+	w.minVersion = version
 	return w.db.Insert(rows)
 }
`;

const FINDINGS_101: FindingsResult = {
  status: "reviewed",
  skip_reason: "",
  approval: {
    verdict: "approve_once_ci_passes",
    reason:
      "ran the build, vet, and the changed packages' tests · skipped the integration tests, which need a cache server",
  },
  findings: [
    {
      id: "1",
      path: "internal/cacheconfig/config_test.go",
      line: 18,
      side: "RIGHT",
      title: "The pool bound case repeats a rule TestValidate covers",
      draft:
        "**nit:** The table is here to show that `Open` runs `Config.Validate` before it dials the cache, and one bad config shows that. \"pool bound\" re-checks a rule that `TestValidate` already covers. Could we drop it?",
      kind: "nit",
    },
    {
      id: "2",
      path: "internal/cacheconfig/config_test.go",
      line: 43,
      side: "RIGHT",
      title: "The import test could be a depguard rule",
      draft:
        "**nit:** `TestProductionDoesNotImportStore` could be a `depguard` rule in `.golangci.yaml`, which catches the import at lint time for every package. Keeping the test is fine too.",
      kind: "nit",
    },
  ],
};

const FINDINGS_107: FindingsResult = {
  status: "reviewed",
  skip_reason: "",
  approval: { verdict: "look_first", reason: "ran the store tests · finding 1 needs the author" },
  findings: [
    {
      id: "1",
      path: "internal/store/writer.go",
      line: 5,
      side: "RIGHT",
      title: "A retried write of the same version now fails",
      draft:
        "**issue:** With `<=`, a client that retries a write of the version it just wrote gets `ErrStale`, and the retry loop in `sync.go` gives up after it. `TestWriteRetry` fails at this commit and passes at the merge base.",
      kind: "caller_visible_bug",
    },
    {
      id: "2",
      path: "internal/store/writer.go",
      line: 8,
      side: "RIGHT",
      title: "minVersion changes without the writer's lock",
      draft: "**question:** Two writers share this `Writer` in `server.go`. Should this hold `w.mu` like `Flush` does?",
      kind: "question",
    },
  ],
};

/** An invented two-file patch: a new retry policy in src/retry.rs and the client that starts using it. */
export const RETRY_PATCH = [
  "diff --git a/src/retry.rs b/src/retry.rs",
  "new file mode 100644",
  "--- /dev/null",
  "+++ b/src/retry.rs",
  "@@ -0,0 +1,8 @@",
  "+pub struct RetryPolicy {",
  "+    pub attempts: u32,",
  "+}",
  "+",
  "+impl RetryPolicy {",
  "+    pub fn delay(&self, n: u32) -> u64 {",
  "+        250 * 2u64.pow(n)",
  "+    }",
  "diff --git a/src/client.rs b/src/client.rs",
  "--- a/src/client.rs",
  "+++ b/src/client.rs",
  "@@ -1,4 +1,5 @@",
  " fn send(body: &[u8]) {",
  "-    for _ in 0..3 { try_once(body); }",
  "+    let policy = RetryPolicy { attempts: 5 };",
  "+    retry(&policy, body);",
  " }",
  " ",
  "",
].join("\n");

const RETRY_FINDINGS: FindingsResult = {
  status: "reviewed",
  skip_reason: "",
  approval: { verdict: "approve_once_ci_passes", reason: "Small and tested." },
  findings: [
    { id: "1", path: "src/retry.rs", line: 7, side: "RIGHT", title: "The delay overflows", draft: "**nit:** `2u64.pow` overflows after 64 attempts.", kind: "nit" },
  ],
};

/**
 * The overview of the retry review: two layers (the policy, then the client, to skim), two places to
 * look, and the calls one failed upload makes before and after.
 */
export function retryOverview(): Overview {
  return {
    summary: "Uploads retry with a `RetryPolicy` instead of a fixed loop.",
    verdict_reason: "The policy is small and its tests pass.",
    before_after: [{ label: "A failed upload", before: "tries 3 times at once", after: "tries 5 times, waiting longer each time" }],
    places: [
      { id: "p1", title: "The delay doubles without a cap", reason: "After 20 attempts the delay is hours.", evidence: { kind: "read", detail: "read `delay`" }, path: "src/retry.rs", start_line: 6, end_line: 8 },
      { id: "p2", title: "Five attempts, not three", reason: "Uploads now try twice more.", evidence: { kind: "ran", detail: "ran the client tests" }, path: "src/client.rs", start_line: 2, end_line: 3 },
    ],
    layers: [
      { id: "l1", title: "The retry policy", intent: "A new type that says how often to retry.", read_hint: "read", ranges: [{ path: "src/retry.rs", start_line: 1, end_line: 8 }] },
      { id: "l2", title: "The client uses it", intent: "The loop gives way to the policy.", read_hint: "skim", ranges: [{ path: "src/client.rs", start_line: 1, end_line: 4 }] },
    ],
    flow: {
      title: "A failed upload",
      input: 'send(b"x") while the server answers 503',
      calls: [
        { from: "client", to: "server", label: "try_once", status: "removed", old_label: null, path: null, line: null },
        { from: "client", to: "policy", label: "retry", status: "added", old_label: null, path: "src/client.rs", line: 3 },
        { from: "policy", to: "policy", label: "delay(n)", status: "changed", old_label: "sleep(0)", path: "src/retry.rs", line: 7 },
      ],
    },
    checked: ["Ran the client tests at head and base"],
    not_checked: ["Didn't run the upload integration suite"],
  };
}

/** An invented Go patch: a new name rule in internal/projects/name.go, and Create, which checks it instead of slugify, removed. */
export const NAME_PATCH = [
  "diff --git a/internal/projects/name.go b/internal/projects/name.go",
  "new file mode 100644",
  "--- /dev/null",
  "+++ b/internal/projects/name.go",
  "@@ -0,0 +1,11 @@",
  "+package projects",
  "+",
  '+import "regexp"',
  "+",
  "+// namePattern is what a bucket name allows.",
  "+var namePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$`)",
  "+",
  "+// ValidateName says why a project name can't name a bucket.",
  "+func ValidateName(name string) error {",
  "+\treturn check(namePattern.MatchString(name))",
  "+}",
  "diff --git a/internal/projects/create.go b/internal/projects/create.go",
  "--- a/internal/projects/create.go",
  "+++ b/internal/projects/create.go",
  "@@ -10,8 +10,6 @@",
  " func Create(name string) (*Project, error) {",
  "-\tbucket := slugify(name)",
  "-\treturn newProject(name, bucket)",
  "+\tif err := ValidateName(name); err != nil {",
  "+\t\treturn nil, err",
  "+\t}",
  "+\treturn newProject(name, name)",
  " }",
  "-",
  "-func slugify(name string) string {",
  "-\treturn strings.ToLower(name)",
  "-}",
  "",
].join("\n");

const NAME_FINDINGS: FindingsResult = {
  status: "reviewed",
  skip_reason: "",
  approval: { verdict: "approve_once_ci_passes", reason: "Small and tested." },
  findings: [{ id: "1", path: "internal/projects/name.go", line: 6, side: "RIGHT", title: "Two dashes pass", draft: "**nit:** `namePattern` lets `a--b` through.", kind: "nit" }],
};

/** The overview of the name review: two layers and two places to look, and no calls before and after. */
export function nameOverview(): Overview {
  return {
    summary: "Projects check their names before they get a bucket, instead of `slugify` turning any name into one.",
    verdict_reason: "Small and tested.",
    before_after: [{ label: "A name with a space", before: "gets a bucket with a dash", after: "fails with a message" }],
    places: [
      { id: "p1", title: "Names with spaces now fail", reason: "`slugify` used to fix them.", evidence: { kind: "read", detail: "read `Create`" }, path: "internal/projects/create.go", start_line: 11, end_line: 13 },
      { id: "p2", title: "Two dashes pass", reason: "`namePattern` lets `a--b` through.", evidence: { kind: "ran", detail: "ran `TestValidateName`" }, path: "internal/projects/name.go", start_line: 6, end_line: 6 },
    ],
    layers: [
      { id: "rule", title: "The name rule", intent: "What a bucket name allows.", read_hint: "read", ranges: [{ path: "internal/projects/name.go", start_line: 1, end_line: 11 }] },
      { id: "create", title: "Create checks names", intent: "The check replaces slugify.", read_hint: "read", ranges: [{ path: "internal/projects/create.go", start_line: 10, end_line: 15 }] },
    ],
    flow: null,
    checked: ["Ran go test ./internal/projects/..."],
    not_checked: ["Didn't create a real bucket"],
  };
}

/**
 * How the changed code of NAME_PATCH connects: ValidateName and namePattern added, Create changed,
 * slugify removed, and two unchanged callers, one of which only a search for ValidateName's name
 * found, so it may call a different ValidateName.
 */
export function nameConnections(): Connections {
  return {
    nodes: [
      { id: "validate", name: "projects.ValidateName", kind: "func", path: "internal/projects/name.go", line: 9, change: "added", lines_changed: 3 },
      { id: "pattern", name: "projects.namePattern", kind: "var", path: "internal/projects/name.go", line: 6, change: "added", lines_changed: 1 },
      { id: "create", name: "projects.Create", kind: "func", path: "internal/projects/create.go", line: 10, change: "changed", lines_changed: 6 },
      { id: "slugify", name: "projects.slugify", kind: "func", path: "internal/projects/create.go", line: 15, change: "removed", lines_changed: 4 },
      { id: "handle", name: "api.handleCreate", kind: "func", path: "internal/api/projects.go", line: 41, change: "unchanged", lines_changed: 0 },
      { id: "signup", name: "api.signup", kind: "func", path: "internal/api/users.go", line: 30, change: "unchanged", lines_changed: 0 },
    ],
    edges: [
      { from: "handle", to: "create", kind: "calls", found_by: "gopls" },
      { from: "create", to: "validate", kind: "calls", found_by: "gopls" },
      { from: "validate", to: "pattern", kind: "uses", found_by: "gopls" },
      { from: "signup", to: "validate", kind: "calls", found_by: "name_search" },
    ],
    limits: ["Callers outside example-org/widgets aren't searched.", "gopls couldn't load internal/legacy, so its callers come from name search."],
  };
}

/**
 * How a TypeScript change connects: the trace sidebar shows token counts from a new formatTokens.
 * TypeScript's language service found every link but exportReport's, in plain JavaScript outside
 * TypeScript's project, which only a search for formatTokens found.
 */
export function typeScriptConnections(): Connections {
  return {
    nodes: [
      { id: "format", name: "formatTokens", kind: "func", path: "web/src/trace/tokens.ts", line: 9, change: "added", lines_changed: 3 },
      { id: "sidebar", name: "TraceSidebar", kind: "func", path: "web/src/trace/TraceSidebar.tsx", line: 8, change: "changed", lines_changed: 2 },
      { id: "page", name: "TracePage", kind: "func", path: "web/src/trace/TracePage.tsx", line: 31, change: "unchanged", lines_changed: 0 },
      { id: "report", name: "exportReport", kind: "func", path: "web/scripts/report.js", line: 14, change: "unchanged", lines_changed: 0 },
    ],
    edges: [
      { from: "page", to: "sidebar", kind: "calls", found_by: "typescript" },
      { from: "sidebar", to: "format", kind: "calls", found_by: "typescript" },
      { from: "report", to: "format", kind: "calls", found_by: "name_search" },
    ],
    limits: ["TypeScript's project leaves out web/scripts, which is plain JavaScript, so its callers come from name search."],
  };
}

/**
 * Which changed lines of NAME_PATCH the tests ran: no test ran lines 11 and 12 of create.go, in the
 * first place to look, nor line 14 after it. TestValidateName ran name.go's lines 6 and 10: it
 * caught the change Whooper made to line 10 on purpose, but no test noticed the one to line 6, the
 * second place to look.
 */
export function nameTestGaps(): TestGaps {
  return {
    measured: true,
    reason: null,
    tool: "go test -coverprofile",
    lines: [
      { path: "internal/projects/create.go", line: 11, status: "not_run" },
      { path: "internal/projects/create.go", line: 12, status: "not_run" },
      { path: "internal/projects/create.go", line: 14, status: "not_run" },
      { path: "internal/projects/name.go", line: 6, status: "survived", change: "`{1,61}` to `{1,62}`" },
      { path: "internal/projects/name.go", line: 10, status: "caught", change: "`namePattern.MatchString(name)` to `!namePattern.MatchString(name)`" },
    ],
    summary: { changed_lines: 15, run: 2, not_run: 3, survived: 1, caught: 1 },
    limits: ["Whooper changed only lines a test ran, one change on each.", "Tests of other packages that call Create didn't run."],
  };
}

/** Example inputs run through NAME_PATCH's functions: Create at base and head, where "a b" now fails, and ValidateName, which only the head has. */
export function nameExamples(): Examples {
  return {
    functions: [
      { name: "projects.Create", path: "internal/projects/create.go", line: 10 },
      { name: "projects.ValidateName", path: "internal/projects/name.go", line: 9 },
    ],
    rows: [
      { function: "projects.Create", input: '"abc"', before: '&Project{Name: "abc", Bucket: "abc"}', after: '&Project{Name: "abc", Bucket: "abc"}', status: "same", label: "ran_base_and_head", note: null },
      { function: "projects.Create", input: '"a b"', before: '&Project{Name: "a b", Bucket: "a b"}', after: 'error: name "a b" can\'t name a bucket', status: "different", label: "ran_base_and_head", note: null },
      { function: "projects.Create", input: '"x"', before: '&Project{Name: "x"}', after: '&Project{Name: "x"}', status: "flaky", label: "ran_base_and_head", note: "the two runs at base gave different results" },
      { function: "projects.Create", input: '""', before: null, after: null, status: "error", label: "ran_base_and_head", note: "the run at base timed out" },
      { function: "projects.ValidateName", input: '"a--b"', before: null, after: "nil", status: "different", label: "ran_head_only", note: "new in this PR" },
      { function: "projects.ValidateName", input: '"Q3"', before: null, after: null, status: "same", label: "not_compared", note: "the test didn't build" },
    ],
    limits: ["Create ran with an in-memory store, not a real bucket."],
  };
}

/** The retry review, which wrote an overview, and the name review, which also saved its connections, test gaps, and examples. */
export const RETRY_RUN = "20310727T020000Z-131";
export const NAME_RUN = "20310727T021000Z-133";

const DOCKER: Cause = { id: "docker_unavailable", summary: "Docker isn't running", scope: "all" };
const NO_FINDINGS: Cause = { id: "agent_no_findings", summary: "The review agent left no usable findings", scope: "pr" };
const NOTHING_ELSE = { failure: null, cause: null, asks: [] };

export function reviewRecords(): RunRecord[] {
  return [
    {
      run_id: RETRY_RUN,
      repo: "example-org/widgets",
      pr: 131,
      title: "feat(upload): retry uploads with a policy",
      author: "sam",
      head_sha: "2252c0f0a1b2",
      outcome: "reviewed",
      findings: RETRY_FINDINGS,
      patch: RETRY_PATCH,
      ...NOTHING_ELSE,
      extras: {
        overview: retryOverview(),
        commits_since_review: [
          { sha: "aaaa1111bbbb", title: "client: log each retry", author: "sam", at: "2031-07-27T16:00:00-07:00", files: ["src/client.rs"] },
          { sha: "cccc2222dddd", title: "docs: retries", author: "sam", at: "2031-07-27T17:00:00-07:00", files: ["README.md"] },
        ],
        co_change: [{ path: "src/server.rs", partner: "src/client.rs", ratio: 0.8, commits: 10 }],
      },
    },
    {
      run_id: NAME_RUN,
      repo: "example-org/widgets",
      pr: 133,
      title: "feat(projects): check names before they name a bucket",
      author: "morgan",
      head_sha: "5e8f1a2b3c4d",
      outcome: "reviewed",
      findings: NAME_FINDINGS,
      patch: NAME_PATCH,
      ...NOTHING_ELSE,
      extras: { overview: nameOverview(), commits_since_review: [], connections: nameConnections(), test_gaps: nameTestGaps(), examples: nameExamples() },
    },
    {
      run_id: "20310727T010246Z-101",
      repo: "example-org/widgets",
      pr: 101,
      title: "refactor(cache): separate the cache configuration",
      author: "alex",
      head_sha: "4012ccf3a9d1",
      outcome: "reviewed",
      findings: FINDINGS_101,
      patch: PATCH_101,
      ...NOTHING_ELSE,
    },
    {
      run_id: "20310727T011502Z-107",
      repo: "example-org/widgets",
      pr: 107,
      title: "fix(store): make writers honor version constraints",
      author: "jordan",
      head_sha: "f1abd4c90b2e",
      outcome: "reviewed",
      findings: FINDINGS_107,
      patch: PATCH_107,
      ...NOTHING_ELSE,
    },
    // The first review of #107, which the one above supersedes.
    {
      run_id: "20310726T120000Z-107",
      repo: "example-org/widgets",
      pr: 107,
      title: "fix(store): make writers honor version constraints",
      author: "jordan",
      head_sha: "0b7e2d91aa40",
      outcome: "reviewed",
      findings: FINDINGS_107,
      patch: PATCH_107,
      ...NOTHING_ELSE,
    },
    {
      run_id: "20310727T005510Z-109",
      repo: "sample-org/gadgets",
      pr: 109,
      title: "keep only shared types in domain",
      author: "riley",
      head_sha: "9c2e71d04b5a",
      outcome: "failed",
      findings: null,
      patch: "",
      failure: "the review run exited 0 without findings; see its stderr.log\n",
      cause: NO_FINDINGS,
      asks: [{ question: "Why did this review fail?", finding: null, answer: "The agent stopped before it wrote **findings.json**." }],
    },
    {
      run_id: "20310727T014000Z-120",
      repo: "example-org/widgets",
      pr: 120,
      title: "feat(api): page the orders list",
      author: "casey",
      head_sha: "7a3f09c2d1e8",
      outcome: "running",
      findings: null,
      patch: null,
      ...NOTHING_ELSE,
    },
    {
      run_id: "20310727T013000Z-115",
      repo: "example-org/widgets",
      pr: 115,
      title: null,
      author: null,
      head_sha: "5d1c0a7e33b9",
      outcome: "waiting",
      findings: null,
      patch: null,
      failure: null,
      cause: DOCKER,
      asks: [],
    },
  ];
}

/** A review the user neither skipped nor removed. */
const UNMARKED = { skipped_by_user: false, removed_by_user: false };

export function status(): WhooperStatus {
  return {
    version: 1,
    loop: { running: true, pid: 4242 },
    paused: false,
    next_round: "2031-07-27T15:45:00-07:00",
    round: { running: false, started_at: null, reviewing: null },
    last_round: {
      started_at: "2031-07-27T15:15:01-07:00",
      ended_at: "2031-07-27T15:22:40-07:00",
      result: "done",
      selected: 2,
      reviews: [
        { pr: "example-org/widgets#101", outcome: "reviewed" },
        { pr: "example-org/widgets#115", outcome: "waiting" },
      ],
    },
    blocked: { ...DOCKER, scope: "all", since: "2031-07-27T15:22:40-07:00", waiting: 1 },
    reviews: [
      { run_id: NAME_RUN, repo: "example-org/widgets", pr: 133, title: "feat(projects): check names before they name a bucket",
        author: "morgan", url: "https://github.com/example-org/widgets/pull/133", started_at: "2031-07-26T19:10:00-07:00",
        ended_at: "2031-07-26T19:24:12-07:00", outcome: "reviewed", verdict: "approve_once_ci_passes", skipped: false, findings: 1,
        safe_to_approve: true, submitted: null, needs_you: true, cause: null, head_sha: "5e8f1a2b3c4d",
        pr_head_sha: "5e8f1a2b3c4d", ...UNMARKED },
      { run_id: RETRY_RUN, repo: "example-org/widgets", pr: 131, title: "feat(upload): retry uploads with a policy",
        author: "sam", url: "https://github.com/example-org/widgets/pull/131", started_at: "2031-07-26T19:00:00-07:00",
        ended_at: "2031-07-26T19:12:30-07:00", outcome: "reviewed", verdict: "approve_once_ci_passes", skipped: false, findings: 1,
        safe_to_approve: true, submitted: null, needs_you: true, cause: null, head_sha: "2252c0f0a1b2",
        pr_head_sha: "cccc2222dddd", ...UNMARKED },
      { run_id: "20310727T014000Z-120", repo: "example-org/widgets", pr: 120, title: "feat(api): page the orders list", author: "casey",
        url: "https://github.com/example-org/widgets/pull/120", started_at: "2031-07-26T18:40:00-07:00", ended_at: null,
        outcome: "running", verdict: null, skipped: false, findings: 0, safe_to_approve: false, submitted: null, needs_you: false,
        cause: null, head_sha: "7a3f09c2d1e8", pr_head_sha: "7a3f09c2d1e8", ...UNMARKED },
      { run_id: "20310727T013000Z-115", repo: "example-org/widgets", pr: 115, title: null, author: null, url: "https://github.com/example-org/widgets/pull/115",
        started_at: "2031-07-26T18:30:00-07:00", ended_at: null, outcome: "waiting", verdict: null, skipped: false, findings: 0,
        safe_to_approve: false, submitted: null, needs_you: false, cause: DOCKER, head_sha: "5d1c0a7e33b9",
        pr_head_sha: "5d1c0a7e33b9", ...UNMARKED },
      { run_id: "20310727T011502Z-107", repo: "example-org/widgets", pr: 107, title: "fix(store): make writers honor version constraints",
        author: "jordan", url: "https://github.com/example-org/widgets/pull/107", started_at: "2031-07-26T18:15:02-07:00",
        ended_at: "2031-07-26T18:31:27-07:00", outcome: "reviewed", verdict: "look_first", skipped: false, findings: 2,
        safe_to_approve: false, submitted: null, needs_you: true, cause: null, head_sha: "f1abd4c90b2e",
        pr_head_sha: "c83d20e7f6a1", ...UNMARKED },
      { run_id: "20310727T010246Z-101", repo: "example-org/widgets", pr: 101, title: "refactor(cache): separate the cache configuration",
        author: "alex", url: "https://github.com/example-org/widgets/pull/101", started_at: "2031-07-26T18:02:46-07:00",
        ended_at: "2031-07-26T18:14:58-07:00", outcome: "reviewed", verdict: "approve_once_ci_passes", skipped: false, findings: 2,
        safe_to_approve: true, submitted: null, needs_you: true, cause: null, head_sha: "4012ccf3a9d1",
        pr_head_sha: "4012ccf3a9d1", ...UNMARKED },
      { run_id: "20310727T005510Z-109", repo: "sample-org/gadgets", pr: 109, title: "keep only shared types in domain",
        author: "riley", url: "https://github.com/sample-org/gadgets/pull/109", started_at: "2031-07-26T17:55:10-07:00",
        ended_at: "2031-07-26T18:02:31-07:00", outcome: "failed", verdict: null, skipped: false, findings: 0, safe_to_approve: false,
        submitted: null, needs_you: true, cause: NO_FINDINGS, head_sha: "9c2e71d04b5a", pr_head_sha: "9c2e71d04b5a", ...UNMARKED },
      { run_id: "20310726T120000Z-107", repo: "example-org/widgets", pr: 107, title: "fix(store): make writers honor version constraints",
        author: "jordan", url: "https://github.com/example-org/widgets/pull/107", started_at: "2031-07-26T05:00:00-07:00",
        ended_at: "2031-07-26T05:16:10-07:00", outcome: "reviewed", verdict: "look_first", skipped: false, findings: 2,
        safe_to_approve: false, submitted: null, needs_you: false, cause: null, head_sha: "0b7e2d91aa40",
        pr_head_sha: "c83d20e7f6a1", ...UNMARKED },
    ],
    rounds: [
      { started_at: "2031-07-27T15:15:01-07:00", ended_at: "2031-07-27T15:22:40-07:00", result: "done", selected: 2, reason: null },
    ],
    // The same reviews in status format 2, as lib/whooper.py's status_v2 lists them.
    format: 2,
    agent: { words: "Can't review · Docker isn't running", dot: "error" },
    blockers: [{ id: "docker_unavailable", title: "Docker isn't running", dot: "error", route: "/causes/docker_unavailable", action: "round" }],
    items: [
      { id: NAME_RUN, group: "ready", title: "feat(projects): check names before they name a bucket",
        detail: "widgets#133 · @morgan · 1 finding", dot: "success", project: null,
        updated_at: "2031-07-26T19:24:12-07:00", route: `/reviews/${NAME_RUN}`, actions: ["retry", "skip", "remove"] },
      { id: RETRY_RUN, group: "ready", title: "feat(upload): retry uploads with a policy",
        detail: "widgets#131 · @sam · 1 finding", dot: "success", project: null,
        updated_at: "2031-07-26T19:12:30-07:00", route: `/reviews/${RETRY_RUN}`, actions: ["retry", "skip", "remove"] },
      { id: "20310727T014000Z-120", group: "reviewing", title: "feat(api): page the orders list",
        detail: "widgets#120 · @casey · reviewing since Jul 26", dot: "running", project: null,
        updated_at: "2031-07-26T18:40:00-07:00", route: "/reviews/20310727T014000Z-120", actions: ["skip"] },
      { id: "20310727T011502Z-107", group: "look", title: "fix(store): make writers honor version constraints",
        detail: "widgets#107 · @jordan · 2 findings", dot: "attention", project: null,
        updated_at: "2031-07-26T18:31:27-07:00", route: "/reviews/20310727T011502Z-107", actions: ["retry", "skip", "remove"] },
      { id: "20310727T013000Z-115", group: "waiting", title: "example-org/widgets#115",
        detail: "widgets#115 · waiting · Docker isn't running", dot: "idle", project: null,
        updated_at: "2031-07-26T18:30:00-07:00", route: "/reviews/20310727T013000Z-115", actions: ["retry", "skip", "remove"] },
      { id: "20310727T010246Z-101", group: "ready", title: "refactor(cache): separate the cache configuration",
        detail: "widgets#101 · @alex · 2 findings", dot: "success", project: null,
        updated_at: "2031-07-26T18:14:58-07:00", route: "/reviews/20310727T010246Z-101", actions: ["retry", "skip", "remove"] },
      { id: "20310727T005510Z-109", group: "failed", title: "keep only shared types in domain",
        detail: "gadgets#109 · @riley · The review agent left no usable findings", dot: "error", project: null,
        updated_at: "2031-07-26T18:02:31-07:00", route: "/reviews/20310727T005510Z-109", actions: ["retry", "skip", "remove"] },
    ],
    truncated: false,
  };
}

/** What bin/whooper-settings get prints for a config that sets its agents and models and leaves the
 * rest to Whooper's defaults. */
export function settings(): Record<string, unknown> {
  return {
    schedule: { interval_minutes: 15, start: "10:00", end: "18:00", days: ["mon", "tue", "wed", "thu", "fri"] },
    poll: { timeout_minutes: 10 },
    jobs: { review: "claude", poll: "claude", summary: "claude" },
    agents: {
      claude: { review_model: "opus", poll_model: "sonnet" },
      devin: { review_model: "claude-opus-4-high", poll_model: "claude-sonnet-4" },
    },
    review: { timeout_minutes: 60, skills: ["code-review"] },
    ask: { timeout_minutes: 10 },
  };
}
