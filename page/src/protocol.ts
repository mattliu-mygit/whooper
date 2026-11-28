// The messages between Whooper's page and the program that shows it, Prompting Time, page protocol
// 2, which prompting-time-plugin.json names as page.protocol. Prompting Time hosts the page in an
// iframe sandboxed to scripts only (sandbox="allow-scripts"), so the page reaches nothing else: no
// network, no Tauri, no local server. Every message is a JSON object with `version` 2, `type`, and
// only the fields its type lists. Prompting Time drops a message that does not fit, and so does the
// page.
//
// Page -> host:
//   ready                      The page listens. The host then sends the route the user opened, if any.
//   call {id, action, args}    Runs one of the manifest's actions with args after the action's own
//                              command. The host answers with result or error, with the call's id as
//                              reply_to. Prompting Time runs only an action the manifest declares, and
//                              one marked confirm only after the user confirms it: it runs the command
//                              with --print, shows what it printed, and runs it again with --expect
//                              <fingerprint>, so the page never passes either flag.
// Host -> page:
//   result {reply_to, json}    What the action printed: JSON when it printed JSON, else the text.
//   error {reply_to?, message, stage?, exit_code?, stderr?}
//                              The action failed or was refused, with its message to show, such as
//                              whooper-draft's refusal, or the user cancelled the confirmation. stage
//                              says how far the call got: cancelled (the user cancelled the
//                              confirmation), preview (submit's --print refused, which the
//                              confirmation showed), or run; absent when Prompting Time refused the
//                              call itself. When Whooper's command refused, exit_code (null on a
//                              signal) and stderr say how, such as whooper-submit's exit 11 or 12.
//   open {route}               The user opened an item or blocker of whooper-status's status format 2,
//                              from a row or Command-J: /reviews/<run-id> or /causes/<cause-id>, or
//                              Whooper's name, which opens the home route /.
//   changed {files}            Files changed in Whooper's state folder, relative to it, such as
//                              runs/<run-id>/drafts.json, or null when more changed than one event
//                              lists. The page reads again with call.
//
// The actions the page calls, each with the arguments it adds:
//   status                     bin/whooper-status --json: the reviews, for the tabs and the open
//                              review's header
//   show-review <run-id>       bin/whooper-show --json <run-id>: the review the page shows
//   draft <run-id> [<op> [<finding>] [<text>]]
//                              bin/whooper-draft: changes the drafts and prints them
//   ask <run-id> <question> [--finding <id>]
//                              bin/whooper-ask: prints the answer
//   submit <run-id> [--approve-anyway] [--allow-mentions]
//                              bin/whooper-submit, after Prompting Time's confirmation
//   skip <run-id> [--undo], remove <run-id> [--undo], retry <run-id>
//                              bin/whooper-skip, bin/whooper-remove, bin/whooper-retry
//   round                      bin/whooper-round: reviews the waiting PRs again now
//   pause, resume              bin/whooper-pause, bin/whooper-resume
//   get-settings               bin/whooper-settings get: the settings, for the Settings tab
//   save-settings <json>       bin/whooper-settings set: saves the changed settings, or refuses why

export const PROTOCOL_VERSION = 2;

/** What stopped a review: scope all for a cause that stops every review, such as Docker not running,
 * and pr for one that stops only its PR's. */
export interface Cause {
  id: string;
  summary: string;
  scope: "all" | "pr";
}

/** `bin/whooper-status`; its header describes each field. */
export interface WhooperStatus {
  paused: boolean;
  next_round: string | null;
  round: { running: boolean; started_at: string | null; reviewing: string | null };
  last_round: {
    started_at: string;
    ended_at: string;
    result: "done" | "poll_failed" | "interrupted";
    selected: number | null;
    reviews: { pr: string; outcome: "reviewed" | "failed" | "interrupted" | "waiting" }[];
  } | null;
  /** What stops every review now, or null. */
  blocked: (Cause & { scope: "all"; since: string; waiting: number }) | null;
  /** The review runs that started in the last 30 days, newest first. */
  reviews: {
    run_id: string;
    /** <owner>/<repo> */
    repo: string | null;
    pr: number;
    title: string | null;
    author: string | null;
    url: string | null;
    started_at: string;
    ended_at: string | null;
    outcome: "running" | "waiting" | "reviewed" | "failed";
    verdict: Verdict | null;
    skipped: boolean;
    findings: number;
    safe_to_approve: boolean;
    submitted: { event: "APPROVE" | "COMMENT"; at: string } | null;
    needs_you: boolean;
    cause: Cause | null;
    head_sha: string | null;
    pr_head_sha: string | null;
    skipped_by_user: boolean;
    removed_by_user: boolean;
  }[];
  /** How many rounds started on each of the last 15 local days, oldest first and today last, by how
   * they ended: date is the local day, such as 2031-07-30. An older Whooper leaves it out. */
  rounds_by_day?: { date: string; done: number; poll_failed: number; interrupted: number; skipped: number }[];
  // Prompting Time's status format 2 (schemas/prompting-time-status.json), beside format 1.
  format: 2;
  /** Whooper's row in Prompting Time, such as "Running · next poll 3:45 PM". */
  agent: { words: string; dot: Dot };
  /** Each cause that stops every review. */
  blockers: { id: string; title: string; dot: Dot; route: string; action?: string }[];
  /** The newest review of each PR the user did not remove, at most 50, current before finished. */
  items: StatusItem[];
  truncated: boolean;
}

/** A dot of status format 2: error red, attention orange, success green, running uncolored, idle none. */
export type Dot = "error" | "attention" | "success" | "running" | "idle";

/** One review in status format 2, in one of the groups prompting-time-plugin.json declares. */
export interface StatusItem {
  id: string;
  group: string;
  title: string;
  detail?: string | null;
  dot: Dot;
  project?: string | null;
  updated_at: string;
  /** /reviews/<run-id> */
  route: string;
  actions?: string[];
}

/** One review in the reviews of `bin/whooper-status --json`. */
export type StatusReview = WhooperStatus["reviews"][number];

/** One day in the rounds_by_day of `bin/whooper-status --json`. */
export type RoundsOfDay = NonNullable<WhooperStatus["rounds_by_day"]>[number];

/** `bin/whooper-show <run-id>`; its header describes each field. */
export interface Shown {
  run_id: string;
  /** run.json, or null when whooper-review stopped before it wrote the file. */
  run: { repo: string; pr: number; head_sha?: string } | null;
  title: string | null;
  author: string | null;
  outcome: "running" | "waiting" | "reviewed" | "failed";
  findings: FindingsResult | null;
  safe_to_approve: boolean;
  drafts: Drafts | null;
  diff: string | null;
  /** failure.txt, the reason whooper-review gave when it stopped the review. */
  failure: string | null;
  /** whooper-ask's answers, oldest first. */
  asks: { question: string; finding: string | null; answer: string }[];
  submitted: object | null;
  cause: Cause | null;
  head_sha: string | null;
  skipped_by_user: boolean;
  removed_by_user: boolean;
  overview: Overview | null;
  commits_since_review: CommitSinceReview[] | null;
  co_change: CoChange[];
  connections: Connections | null;
  test_gaps: TestGaps | null;
  examples: Examples | null;
}

export type Verdict = "approve_once_ci_passes" | "look_first";

/** A finding in findings.json (schemas/findings.json), with the fields the page and its fake host read. */
export interface Finding {
  id: string;
  path: string;
  line: number;
  side: "LEFT" | "RIGHT";
  /** Whether the line is inside a hunk of the PR's diff, where GitHub takes an inline comment;
   * whooper-submit refuses a review that includes a comment elsewhere (exit 14). */
  in_diff: boolean;
  title: string;
  draft: string;
  kind: "caller_visible_bug" | "ci_failure" | "nit" | "question" | "other";
}

/** findings.json, which fits schemas/findings.json. */
export interface FindingsResult {
  status: "reviewed" | "skipped";
  skip_reason: string;
  approval: { verdict: Verdict; reason: string };
  findings: Finding[];
}

/** What `bin/whooper-draft <run_id>` prints: the review's event and message, and per finding ID
 * whether its comment is included and, once edited, its text. */
export interface Drafts {
  event: "APPROVE" | "COMMENT";
  message: string;
  findings: Record<string, { included: boolean; text?: string }>;
}

/** Lines inside one hunk of a file of diff.patch, counted in the head, or for a deleted file at the
 * merge base. */
export interface LineRange {
  path: string;
  start_line: number;
  end_line: number;
}

/** The overview a review wrote in overview.json (schemas/overview.json), which whooper-review checked
 * against diff.patch. */
export interface Overview {
  summary: string;
  verdict_reason: string;
  before_after: { label: string; before: string; after: string }[];
  /** At most 5, most important first. */
  places: (LineRange & {
    id: string;
    title: string;
    reason: string;
    evidence: { kind: "ran" | "read" | "searched"; detail: string };
  })[];
  /** The change in reading order: each file diff.patch shows lines of is in exactly one layer. */
  layers: { id: string; title: string; intent: string; read_hint: "read" | "skim" | "skip"; ranges: LineRange[] }[];
  flow: {
    title: string;
    input: string;
    calls: {
      from: string;
      to: string;
      label: string;
      status: "same" | "added" | "removed" | "changed";
      old_label: string | null;
      path: string | null;
      line: number | null;
    }[];
  } | null;
  checked: string[];
  not_checked: string[];
}

/** A commit of the PR after the one the review read, from the repo's clone. */
export interface CommitSinceReview {
  sha: string;
  title: string;
  author: string;
  at: string;
  files: string[];
}

/** A file outside the PR that changed together with partner, one of the PR's files: in `commits` of
 * partner's commits in the 90 days before the review, `ratio` of them. */
export interface CoChange {
  path: string;
  partner: string;
  ratio: number;
  commits: number;
}

/** How the code a review's diff changes connects (connections.json): the declarations the diff adds,
 * changes, or removes, the code that calls or uses them, one level up, and how each link was found. */
export interface Connections {
  nodes: {
    /** <path>:<name>, which edges name. */
    id: string;
    /** As the language names it, such as Total, (*Cart).Total for a Go method, or Cart.total in Python. */
    name: string;
    kind: "func" | "method" | "type" | "var" | "const";
    /** In the head, or for removed code at the merge base. */
    path: string;
    line: number;
    /** unchanged for code the diff leaves alone, which calls or uses changed code. */
    change: "added" | "changed" | "removed" | "unchanged";
    lines_changed: number;
  }[];
  /** From the code that calls or uses a node to that node. */
  edges: { from: string; to: string; kind: "calls" | "uses"; found_by: "gopls" | "typescript" | "name_search" }[];
  /** What the graph can miss, in plain words, such as that it keeps at most 40 nodes. */
  limits: string[];
}

/** Which of the lines a review's diff adds to Go, JavaScript, and TypeScript code no test ran (test_gaps.json),
 * from go test with coverage of the changed packages and one Vitest or Jest run with coverage in each changed
 * package.json's folder, run before the review run started. */
export interface TestGaps {
  /** false when Whooper could measure nothing, such as when the head does not build; reason then says why. */
  measured: boolean;
  reason: string | null;
  /** The commands Whooper ran, joined by "; ", each that runs in another folder as (cd <folder> && <command>). */
  tool: string;
  /** Each changed line no test ran, in the head, and each changed Go line tests ran that Whooper changed with
   * gremlins: survived when every test of its package still passed, caught when one failed, with change saying what
   * changed, such as "`>` to `>=`". At most 7 survived or caught lines a file; gremlins names no test. */
  lines: { path: string; line: number; status: "not_run" | "survived" | "caught"; change?: string }[];
  /** The changed lines that hold statements, and how many of them a test ran; all 0 when nothing was measured.
   * survived and caught count those lines, once the mutation step ran. */
  summary: { changed_lines: number; run: number; not_run: number; survived?: number; caught?: number };
  /** What the measure can miss, in plain words, such as that a test can run a line without checking it. */
  limits: string[];
}

/** What the Go functions a review run chose return for the inputs it chose (examples.json), from the test it
 * wrote, which Whooper ran once at the head and twice at the merge base. Only what the test printed fills a row. */
export interface Examples {
  /** In the head. */
  functions: { name: string; path: string; line: number }[];
  rows: {
    function: string;
    input: string;
    /** What the test printed at the merge base and at the head, or null. */
    before: string | null;
    after: string | null;
    /** flaky when the two runs at the merge base printed different results, error when a run printed none. */
    status: "same" | "different" | "flaky" | "error";
    /** ran_head_only for a function the PR adds. */
    label: "ran_base_and_head" | "ran_head_only" | "not_compared";
    /** Why the row is not compared or is new, or null. */
    note: string | null;
  }[];
  /** What the table can miss, in plain words. */
  limits: string[];
}

/** A review that finished with findings, as the page draws it, from what whooper-show prints. */
export interface Review {
  run_id: string;
  /** <owner>/<repo> */
  repo: string;
  pr: number;
  /** github.json's PR title and author; null when the review's GitHub fetch failed. */
  title: string | null;
  author: string | null;
  findings: FindingsResult;
  drafts: Drafts;
  /** diff.patch: the PR's change, git diff <merge base> HEAD. */
  patch: string;
  /** The overview is null when the review wrote none that passed its checks, and the commits since are
   * null when Whooper cannot tell. */
  overview: Overview | null;
  commits_since_review: CommitSinceReview[] | null;
  /** At most 5, highest ratio first. */
  co_change: CoChange[];
  /** null when the review saved no connections.json. */
  connections: Connections | null;
  /** null when the PR changes no Go code or the review saved no test_gaps.json. */
  test_gaps: TestGaps | null;
  /** null when the review run chose no examples. */
  examples: Examples | null;
}

/** One whooper-draft change: include <id>, leave-out <id>, text <id> <text>, approve, comment, or message <text>. */
export type DraftChange =
  | { op: "include" | "leave-out"; finding: string }
  | { op: "text"; finding: string; text: string }
  | { op: "approve" | "comment" }
  | { op: "message"; text: string };

export type PageMessage =
  | { version: typeof PROTOCOL_VERSION; type: "ready" }
  | { version: typeof PROTOCOL_VERSION; type: "call"; id: number; action: string; args: string[] };

/** How far a failed call got, and when Whooper's command refused, its exit code and stderr. */
export type ActionFailure = { stage?: "cancelled" | "preview" | "run"; exit_code?: number | null; stderr?: string };

/** A failed call as the page's promises reject with it: the host's message, and what else it said. */
export class CallError extends Error {
  constructor(
    message: string,
    readonly failure: ActionFailure = {},
  ) {
    super(message);
  }
}

export type HostUpdate =
  | { type: "result"; reply_to: number; json: unknown }
  | ({ type: "error"; reply_to?: number; message: string } & ActionFailure)
  | { type: "open"; route: string }
  | { type: "changed"; files: string[] | null };

export type HostMessage = HostUpdate & { version: typeof PROTOCOL_VERSION };

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Whether message has exactly the keys required, plus any of optional. */
function hasKeys(message: Record<string, unknown>, required: string[], optional: string[] = []): boolean {
  const keys = Object.keys(message);
  return required.every((key) => keys.includes(key)) && keys.every((key) => required.includes(key) || optional.includes(key));
}

/** The host message in data, or null when data is not one: another version, an unknown type, or a
 * field missing, extra, or of the wrong kind. */
export function readHostMessage(data: unknown): HostMessage | null {
  if (!isRecord(data) || data.version !== PROTOCOL_VERSION) return null;
  switch (data.type) {
    case "result":
      return hasKeys(data, ["version", "type", "reply_to", "json"]) && Number.isSafeInteger(data.reply_to) ? (data as HostMessage) : null;
    case "error":
      return hasKeys(data, ["version", "type", "message"], ["reply_to", "stage", "exit_code", "stderr"]) &&
        typeof data.message === "string" &&
        (data.reply_to === undefined || Number.isSafeInteger(data.reply_to)) &&
        (data.stage === undefined || data.stage === "cancelled" || data.stage === "preview" || data.stage === "run") &&
        (data.exit_code === undefined || data.exit_code === null || Number.isSafeInteger(data.exit_code)) &&
        (data.stderr === undefined || typeof data.stderr === "string")
        ? (data as HostMessage)
        : null;
    case "open":
      return hasKeys(data, ["version", "type", "route"]) && typeof data.route === "string" && data.route.startsWith("/")
        ? (data as HostMessage)
        : null;
    case "changed":
      return hasKeys(data, ["version", "type", "files"]) &&
        (data.files === null || (Array.isArray(data.files) && data.files.every((file) => typeof file === "string")))
        ? (data as HostMessage)
        : null;
    default:
      return null;
  }
}

/** The page message in data, or null when data is not one: the check Prompting Time runs on what the
 * page sends before it runs anything, which the fake host runs too. */
export function readPageMessage(data: unknown): PageMessage | null {
  if (!isRecord(data) || data.version !== PROTOCOL_VERSION) return null;
  if (data.type === "ready") return hasKeys(data, ["version", "type"]) ? (data as PageMessage) : null;
  if (data.type !== "call" || !hasKeys(data, ["version", "type", "id", "action", "args"])) return null;
  const fits =
    Number.isSafeInteger(data.id) &&
    typeof data.action === "string" &&
    data.action !== "" &&
    Array.isArray(data.args) &&
    data.args.every((arg) => typeof arg === "string");
  return fits ? (data as PageMessage) : null;
}
