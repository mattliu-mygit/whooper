// The messages between Whooper's page and the program that shows it, Prompting Time, which hosts the
// page in a sandboxed iframe (sandbox="allow-scripts"). The page reaches nothing else: no network, no
// Tauri. Every message is a JSON object with `version`, the number of this message format, and `type`.
// Prompting Time drops a message that does not fit, and so does the page.
//
// Page -> host: requests. Each carries an `id` that the host's answer carries back as `reply_to`.
//   ready                               The page listens. The host answers with status, and sends the
//                                       review the user opened, if any.
//   open-review {run_id}                The host answers with review, read from the run's directory:
//                                       run.json's repo and PR, github.json's title and author,
//                                       findings.json, diff.patch, and the drafts
//                                       `bin/whooper-draft <run_id>` prints.
//   change-draft {run_id, change}       The host runs `bin/whooper-draft <run_id> <op> [<finding>] [<text>]`
//                                       and answers with drafts, the JSON it prints.
//   submit {run_id}                     The host runs `bin/whooper-submit <run_id> --print`, shows the
//                                       request it wrote in its confirmation (the manifest's submit
//                                       action has confirm: true), and only after the user confirms runs
//                                       it again with `--expect <fingerprint>`. It answers with
//                                       submit-result.
//   ask {run_id, question, finding}     The host runs `bin/whooper-ask <run_id> <question> [--finding <id>]`
//                                       and answers with answer.
// Host -> page: answers, with reply_to, and updates, without it.
//   status {status, queue}              `bin/whooper-status --json`, and the reviews in Whooper's queue.
//   review {review}                     One review run, as above.
//   drafts {run_id, drafts}             Also sent unasked when the run's drafts.json changes, such as
//                                       after a whooper-draft from a terminal.
//   submit-result {run_id, posted, message}
//                                       posted is false when the user cancelled the confirmation.
//   answer {run_id, finding, question, answer}
//   error {message}                     The request failed, with Whooper's message, such as
//                                       whooper-draft's or whooper-submit's refusal.

export const PROTOCOL_VERSION = 1;

/** `bin/whooper-status --json`, format version 1; its header describes each field. */
export interface WhooperStatus {
  version: 1;
  loop: { running: boolean; pid: number | null };
  paused: boolean;
  next_round: string | null;
  round: { running: boolean; started_at: string | null; reviewing: string | null };
  last_round: {
    started_at: string;
    ended_at: string;
    result: "done" | "poll_failed" | "interrupted";
    selected: number | null;
    reviews: { pr: string; outcome: "reviewed" | "failed" | "interrupted" }[];
  } | null;
}

/**
 * One review in Whooper's queue. decide: the review finished and waits for the user; reviewing: a
 * review runs now; failed: the review stopped without findings; submitted: the user submitted it.
 * The host builds these from the reviews `bin/whooper-status --json` lists (see README.md).
 */
export interface QueueItem {
  /** The review's run, null while no run directory holds its record yet. */
  run_id: string | null;
  /** <owner>/<repo>#<number> */
  pr: string;
  title: string | null;
  state: "decide" | "reviewing" | "failed" | "submitted";
  verdict: Verdict | null;
  /** How many findings the review has. */
  findings: number;
}

export type Verdict = "approve_once_ci_passes" | "look_first";

/** A finding in findings.json (schemas/findings.json), with the fields the page reads. */
export interface Finding {
  id: string;
  path: string;
  line: number;
  side: "LEFT" | "RIGHT";
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
}

/** One whooper-draft change: include <id>, leave-out <id>, text <id> <text>, approve, comment, or message <text>. */
export type DraftChange =
  | { op: "include" | "leave-out"; finding: string }
  | { op: "text"; finding: string; text: string }
  | { op: "approve" | "comment" }
  | { op: "message"; text: string };

export type PageRequest =
  | { type: "ready" }
  | { type: "open-review"; run_id: string }
  | { type: "change-draft"; run_id: string; change: DraftChange }
  | { type: "submit"; run_id: string }
  | { type: "ask"; run_id: string; question: string; finding: string | null };

export type PageMessage = PageRequest & { version: typeof PROTOCOL_VERSION; id: number };

export type HostUpdate =
  | { type: "status"; status: WhooperStatus; queue: QueueItem[] }
  | { type: "review"; review: Review }
  | { type: "drafts"; run_id: string; drafts: Drafts }
  | { type: "submit-result"; run_id: string; posted: boolean; message: string }
  | { type: "answer"; run_id: string; finding: string | null; question: string; answer: string }
  | { type: "error"; message: string };

export type HostMessage = HostUpdate & { version: typeof PROTOCOL_VERSION; reply_to?: number };

type Shape = Record<string, "string" | "number" | "boolean" | "object" | "string|null">;

function fits(value: Record<string, unknown>, shape: Shape): boolean {
  return Object.entries(shape).every(([key, kind]) => {
    const field = value[key];
    if (kind === "string|null") return field === null || typeof field === "string";
    if (kind === "object") return typeof field === "object" && field !== null;
    return typeof field === kind;
  });
}

function envelope(data: unknown): Record<string, unknown> | null {
  if (typeof data !== "object" || data === null) return null;
  const message = data as Record<string, unknown>;
  return message.version === PROTOCOL_VERSION && typeof message.type === "string" ? message : null;
}

// The fields each host message must have, by type. The page trusts its host for the rest, such as
// the inside of a review, which comes from Whooper's own files.
const HOST_FIELDS: Record<HostUpdate["type"], Shape> = {
  status: { status: "object", queue: "object" },
  review: { review: "object" },
  drafts: { run_id: "string", drafts: "object" },
  "submit-result": { run_id: "string", posted: "boolean", message: "string" },
  answer: { run_id: "string", finding: "string|null", question: "string", answer: "string" },
  error: { message: "string" },
};

/** The host message in data, or null when data is not one: another version, an unknown type, or a
 * missing field. */
export function readHostMessage(data: unknown): HostMessage | null {
  const message = envelope(data);
  const shape = message && HOST_FIELDS[message.type as HostUpdate["type"]];
  if (!message || !shape || !fits(message, shape)) return null;
  if (message.reply_to !== undefined && typeof message.reply_to !== "number") return null;
  return message as unknown as HostMessage;
}

const CHANGE_FIELDS: Record<DraftChange["op"], Shape> = {
  include: { finding: "string" },
  "leave-out": { finding: "string" },
  text: { finding: "string", text: "string" },
  approve: {},
  comment: {},
  message: { text: "string" },
};

const PAGE_FIELDS: Record<PageRequest["type"], Shape> = {
  ready: {},
  "open-review": { run_id: "string" },
  "change-draft": { run_id: "string", change: "object" },
  submit: { run_id: "string" },
  ask: { run_id: "string", question: "string", finding: "string|null" },
};

/** The page message in data, or null when data is not one. This is the check a host runs on what
 * the page sends before it runs anything. */
export function readPageMessage(data: unknown): PageMessage | null {
  const message = envelope(data);
  const shape = message && PAGE_FIELDS[message.type as PageRequest["type"]];
  if (!message || !shape || typeof message.id !== "number" || !fits(message, shape)) return null;
  if (message.type === "change-draft") {
    const change = message.change as Record<string, unknown>;
    const fields = CHANGE_FIELDS[change.op as DraftChange["op"]];
    if (!fields || !fits(change, fields)) return null;
  }
  return message as unknown as PageMessage;
}
