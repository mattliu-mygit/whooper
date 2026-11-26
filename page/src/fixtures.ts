import type { Drafts, FindingsResult, QueueItem, Review, WhooperStatus } from "./protocol";

// Invented reviews for the fake host: made-up repos, people, and code, never a real PR.

/** The record of one review run, as the host reads it from the run's directory. */
export interface ReviewRecord extends Omit<Review, "drafts"> {
  /** drafts.json, when the user changed something; whooper-draft computes them otherwise. */
  drafts?: Drafts;
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

export function reviewRecords(): ReviewRecord[] {
  return [
    {
      run_id: "20310727T010246Z-101",
      repo: "example-org/widgets",
      pr: 101,
      title: "refactor(cache): separate the cache configuration",
      author: "alex",
      findings: FINDINGS_101,
      patch: PATCH_101,
    },
    {
      run_id: "20310727T011502Z-107",
      repo: "example-org/widgets",
      pr: 107,
      title: "fix(store): make writers honor version constraints",
      author: "jordan",
      findings: FINDINGS_107,
      patch: PATCH_107,
    },
  ];
}

export function queue(): QueueItem[] {
  return [
    { run_id: "20310727T010246Z-101", pr: "example-org/widgets#101", title: "refactor(cache): separate the cache configuration", state: "decide", verdict: "approve_once_ci_passes", findings: 2 },
    { run_id: "20310727T011502Z-107", pr: "example-org/widgets#107", title: "fix(store): make writers honor version constraints", state: "decide", verdict: "look_first", findings: 2 },
    { run_id: null, pr: "example-org/widgets#112", title: "add tag store methods", state: "reviewing", verdict: null, findings: 0 },
    { run_id: "20310727T005510Z-109", pr: "sample-org/gadgets#109", title: "keep only shared types in domain", state: "failed", verdict: null, findings: 0 },
    { run_id: "20310726T201133Z-99", pr: "example-org/widgets#99", title: "move the score projection to domain", state: "submitted", verdict: "approve_once_ci_passes", findings: 1 },
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
    round: { running: true, started_at: "2031-07-27T15:30:02-07:00", reviewing: "example-org/widgets#112" },
    last_round: {
      started_at: "2031-07-27T15:15:01-07:00",
      ended_at: "2031-07-27T15:22:40-07:00",
      result: "done",
      selected: 2,
      reviews: [
        { pr: "example-org/widgets#101", outcome: "reviewed" },
        { pr: "example-org/widgets#107", outcome: "reviewed" },
      ],
    },
    blocked: null,
    // The runs the queue lists; #112's review has no run directory yet.
    reviews: [
      { run_id: "20310727T011502Z-107", repo: "example-org/widgets", pr: 107, title: "fix(store): make writers honor version constraints",
        author: "jordan", url: "https://github.com/example-org/widgets/pull/107", started_at: "2031-07-26T18:15:02-07:00",
        ended_at: "2031-07-26T18:31:27-07:00", outcome: "reviewed", verdict: "look_first", skipped: false, findings: 2,
        safe_to_approve: false, submitted: null, needs_you: true, cause: null, head_sha: "f1abd4c90b2e",
        pr_head_sha: "f1abd4c90b2e", ...UNMARKED },
      { run_id: "20310727T010246Z-101", repo: "example-org/widgets", pr: 101, title: "refactor(cache): separate the cache configuration",
        author: "alex", url: "https://github.com/example-org/widgets/pull/101", started_at: "2031-07-26T18:02:46-07:00",
        ended_at: "2031-07-26T18:14:58-07:00", outcome: "reviewed", verdict: "approve_once_ci_passes", skipped: false, findings: 2,
        safe_to_approve: true, submitted: null, needs_you: true, cause: null, head_sha: "4012ccf3a9d1",
        pr_head_sha: "4012ccf3a9d1", ...UNMARKED },
      { run_id: "20310727T005510Z-109", repo: "sample-org/gadgets", pr: 109, title: "keep only shared types in domain",
        author: "riley", url: "https://github.com/sample-org/gadgets/pull/109", started_at: "2031-07-26T17:55:10-07:00",
        ended_at: "2031-07-26T18:02:31-07:00", outcome: "failed", verdict: null, skipped: false, findings: 0, safe_to_approve: false,
        submitted: null, needs_you: true,
        cause: { id: "agent_no_findings", summary: "The review agent left no usable findings", scope: "pr" },
        head_sha: "9c2e71d04b5a", pr_head_sha: "9c2e71d04b5a", ...UNMARKED },
      { run_id: "20310726T201133Z-99", repo: "example-org/widgets", pr: 99, title: "move the score projection to domain",
        author: "alex", url: "https://github.com/example-org/widgets/pull/99", started_at: "2031-07-26T13:11:33-07:00",
        ended_at: "2031-07-26T13:29:40-07:00", outcome: "reviewed", verdict: "approve_once_ci_passes", skipped: false, findings: 1,
        safe_to_approve: true, submitted: { event: "APPROVE", at: "2031-07-26T14:05:12-07:00" }, needs_you: false, cause: null,
        head_sha: "77d0e5b1c3f2", pr_head_sha: "77d0e5b1c3f2", ...UNMARKED },
    ],
    rounds: [
      { started_at: "2031-07-27T15:15:01-07:00", ended_at: "2031-07-27T15:22:40-07:00", result: "done", selected: 2, reason: null },
    ],
  };
}
