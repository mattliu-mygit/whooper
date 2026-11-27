import * as fixtures from "./fixtures";
import { MENTION } from "./text";
import { PROTOCOL_VERSION, readPageMessage, type Drafts, type FindingsResult, type HostMessage, type HostUpdate, type PageMessage, type Shown } from "./protocol";

// A stand-in for Prompting Time, for development (dev/index.html) and the page's tests. It runs the
// page's calls of the manifest's actions on the invented reviews in fixtures.ts the way Whooper's
// commands would: drafts change by whooper-draft's rules and refusals, a submit builds
// whooper-submit's request and asks confirmSubmit first, or is refused as whooper-submit refuses an
// unsafe approval or an @-mention, an ask answers at once, and skip, remove,
// and retry change what status and show-review print, after which it says which files changed. It
// refuses an action the manifest does not declare, as Prompting Time does. It reaches no Whooper,
// GitHub, or model.

export interface FakeHostOptions {
  /** Prompting Time's confirmation: gets the text it would show for the request whooper-submit
   * --print wrote, and returns whether the user confirmed. Without it every submit is cancelled. A
   * request whooper-submit --print refuses never reaches it. */
  confirmSubmit?: (text: string) => boolean | Promise<boolean>;
  /** The route the host opens once the page is ready, as when the user opened a row in Prompting Time. */
  openOnReady?: string;
}

/** The actions prompting-time-plugin.json declares. */
const DECLARED = new Set([
  "status", "round", "pause", "resume", "get-settings", "save-settings", "show-review", "draft", "ask", "skip", "remove", "retry", "submit",
]);

/** Whooper's comment marker and the message of an approval left without one (lib/whooper.py). */
const VETTED = "AI comment (vetted)";
const PRE_APPROVAL = "Pre-approving to unblock";
const LABELS: Record<string, string> = {
  caller_visible_bug: "**issue:**",
  ci_failure: "**issue:**",
  nit: "**nit:**",
  question: "**question:**",
  other: "**suggestion:**",
};

/** What argues against approving a run, as run_concerns in lib/whooper.py lists it. */
function concerns(result: FindingsResult): string[] {
  const lines: string[] = [];
  if (result.status === "skipped") lines.push(`the review was skipped: ${result.skip_reason}`);
  if (result.approval.verdict !== "approve_once_ci_passes")
    lines.push(`the run's verdict is ${result.approval.verdict}: ${result.approval.reason}`);
  for (const f of result.findings) if (f.kind !== "nit") lines.push(`finding ${f.id} is a ${f.kind}: ${f.title}`);
  return lines;
}

/** The drafts whooper-draft prints before any change (read_drafts in lib/whooper.py). */
function firstDrafts(result: FindingsResult): Drafts {
  if (concerns(result).length > 0) return { event: "COMMENT", message: "", findings: {} };
  return { event: "APPROVE", message: "", findings: Object.fromEntries(result.findings.map((f) => [f.id, { included: true }])) };
}

export function createFakeHost(send: (message: HostMessage) => void, options: FakeHostOptions = {}) {
  const records = new Map(fixtures.reviewRecords().map((record) => [record.run_id, record]));
  const drafts = new Map<string, Drafts>();
  const submitted = new Map<string, string>();
  const skipped = new Set<string>();
  const removed = new Set<string>();
  /** The exit code of each submit whooper-submit --print refused, in order. */
  const refusals: number[] = [];
  /** Every message the page sent that fits the protocol, in order. */
  const received: PageMessage[] = [];

  const post = (update: HostUpdate) => send({ ...update, version: PROTOCOL_VERSION });

  function record(runId: string, command = "whooper-show") {
    const found = records.get(runId);
    if (!found) throw new Error(`${command}: no run ${runId} in the state folder's runs`);
    return found;
  }

  function findingsOf(runId: string, command: string): FindingsResult {
    const findings = record(runId, command).findings;
    if (!findings) throw new Error(`${command}: ${runId} has no findings.json, so its review did not finish`);
    return findings;
  }

  function draftsOf(runId: string, command = "whooper-draft"): Drafts {
    if (!drafts.has(runId)) drafts.set(runId, structuredClone(record(runId, command).drafts ?? firstDrafts(findingsOf(runId, command))));
    return drafts.get(runId)!;
  }

  /** What bin/whooper-status --json prints, with the user's marks and submissions since. */
  function status() {
    const base = fixtures.status();
    for (const review of base.reviews) {
      review.skipped_by_user = skipped.has(review.run_id);
      review.removed_by_user = removed.has(review.run_id);
      if (submitted.has(review.run_id)) review.submitted = { event: draftsOf(review.run_id).event, at: "2031-07-27T15:40:00-07:00" };
      if (review.skipped_by_user || review.removed_by_user || review.submitted) review.needs_you = false;
    }
    // In status format 2, a removed review leaves the items, and a skipped or submitted one is done.
    base.items = base.items.filter((item) => !removed.has(item.id));
    for (const item of base.items) if (skipped.has(item.id) || submitted.has(item.id)) item.group = "done";
    return base;
  }

  /** What bin/whooper-show --json <run-id> prints. */
  function show(runId: string): Shown {
    const r = record(runId);
    return {
      version: 1,
      run_id: runId,
      run: { repo: r.repo, pr: r.pr, head_sha: r.head_sha },
      title: r.title,
      author: r.author,
      outcome: r.outcome,
      findings: structuredClone(r.findings),
      safe_to_approve: r.outcome === "reviewed" && r.findings !== null && concerns(r.findings).length === 0,
      drafts: r.findings ? structuredClone(draftsOf(runId)) : null,
      diff: r.patch,
      failure: r.failure,
      asks: structuredClone(r.asks),
      submitted: submitted.has(runId) ? { status: "posted" } : null,
      cause: r.cause,
      head_sha: r.head_sha,
      skipped_by_user: skipped.has(runId),
      removed_by_user: removed.has(runId),
      overview: null,
      commits_since_review: null,
      co_change: [],
      connections: null,
      test_gaps: null,
      examples: null,
    };
  }

  /** change_drafts in lib/whooper.py: one change, with its refusals; no change prints the drafts. */
  function draft(runId: string, op?: string, ...rest: string[]): Drafts {
    const current = draftsOf(runId);
    if (op === undefined) return structuredClone(current);
    if (submitted.has(runId)) throw new Error(`whooper-draft: ${runId} was already submitted; see its submit.json`);
    if (op === "include" || op === "leave-out" || op === "text") {
      const [finding, text] = rest;
      if (op === "text" && !text?.trim()) throw new Error("whooper-draft: the text is empty; to drop the comment, leave the finding out");
      const ids = findingsOf(runId, "whooper-draft").findings.map((f) => f.id);
      if (!ids.includes(finding)) throw new Error(`whooper-draft: the run has no finding ${finding}; its findings are ${ids.join(", ") || "none"}`);
      const choice = (current.findings[finding] ??= { included: false });
      if (op === "text") choice.text = text;
      else choice.included = op === "include";
    } else if (op === "message") current.message = rest[0] ?? "";
    else if (op === "approve" || op === "comment") current.event = op === "approve" ? "APPROVE" : "COMMENT";
    else throw new Error(`whooper-draft: unknown change ${op}`);
    changed([`runs/${runId}/drafts.json`]);
    return structuredClone(current);
  }

  /** whooper-submit --print's request, and the text Prompting Time's confirmation would show for it,
   * or its refusal of an approval of a run that is not safe to approve (exit 11) or of an @-mention
   * (exit 12) without the flag that allows it, which it checks in that order. */
  function submitRequest(runId: string, flags: string[]): { text: string } | { refused: 11 | 12 } {
    if (submitted.has(runId)) throw new Error(`whooper-submit: ${runId} was already submitted; see its submit.json`);
    const { repo, pr } = record(runId, "whooper-submit");
    const findings = findingsOf(runId, "whooper-submit");
    const choices = draftsOf(runId);
    const comments = findings.findings
      .filter((f) => choices.findings[f.id]?.included)
      .map((f) => {
        const text = (choices.findings[f.id].text ?? f.draft).trim();
        const body = /^\*\*[A-Za-z]+(?: \([^)]*\))?:\*\*/.test(text) ? text : `${LABELS[f.kind]} ${text}`;
        return { path: f.path, line: f.line, side: f.side, body: `${VETTED}\n\n${body}` };
      });
    if (comments.length === 0 && choices.event === "COMMENT")
      throw new Error(
        "whooper-submit: nothing to submit: the drafts include no comment and do not approve; change them with bin/whooper-draft",
      );
    const message =
      choices.event === "APPROVE" && !choices.message.trim() ? (comments.length > 0 ? PRE_APPROVAL : "") : choices.message;
    if (choices.event === "APPROVE" && concerns(findings).length > 0 && !flags.includes("--approve-anyway")) return { refused: 11 };
    if ([message, ...comments.map((c) => c.body)].some((text) => MENTION.test(text)) && !flags.includes("--allow-mentions"))
      return { refused: 12 };
    const request = { commit_id: "0123456789abcdef0123456789abcdef01234567", event: choices.event, body: message, comments };
    return { text: [`Submit this review to ${repo}#${pr}?`, JSON.stringify(request, null, 2)].join("\n") };
  }

  /** Marks or unmarks a run as whooper-skip and whooper-remove do, and prints what it did. */
  function mark(marks: Set<string>, command: string, word: string, runId: string, ...flags: string[]): string {
    const r = record(runId, command);
    // whooper-skip stops a review that runs; the fake has no review to stop.
    if (r.outcome === "running" && command !== "whooper-skip")
      throw new Error(`${command}: the review is still running; ${command.slice(8)} it once it ends`);
    const name = `${runId} (${r.repo}#${r.pr})`;
    if (flags.includes("--undo")) {
      if (!marks.delete(runId)) return `${name} is not marked as ${word}`;
      changed([`runs/${runId}/${word}-by-user`]);
      return `unmarked ${name} as ${word}`;
    }
    if (marks.has(runId)) return `${name} is already marked as ${word}`;
    marks.add(runId);
    changed([`runs/${runId}/${word}-by-user`]);
    return `marked ${name} as ${word}`;
  }

  function changed(files: string[] | null) {
    // As Prompting Time's watch does, it tells the page after the command that wrote the files ends.
    queueMicrotask(() => post({ type: "changed", files }));
  }

  async function run(action: string, args: string[]): Promise<unknown> {
    if (!DECLARED.has(action)) throw new Error(`Whooper declares no ${action} action.`);
    const [runId, ...rest] = args;
    switch (action) {
      case "status":
        return status();
      case "show-review":
        return show(runId);
      case "draft":
        return draft(runId, ...rest);
      case "submit": {
        if (args.some((arg) => arg.startsWith("--print") || arg.startsWith("--expect")))
          throw new Error("submit asks to confirm first, so Prompting Time adds --print and --expect itself.");
        const preview = submitRequest(runId, rest);
        // Prompting Time shows a refusal in its confirmation, which the user can only close, and tells
        // the page what it tells it for a cancel.
        if ("refused" in preview) refusals.push(preview.refused);
        if ("refused" in preview || !(await options.confirmSubmit?.(preview.text)))
          throw new Error("Nothing ran: the confirmation was cancelled.");
        const { repo, pr } = record(runId);
        const printed = `posted the review: https://github.com/${repo}/pull/${pr}#pullrequestreview-1`;
        submitted.set(runId, printed);
        changed([`runs/${runId}/submit.json`]);
        return `${printed}\n`;
      }
      case "ask": {
        const [question, flag, finding] = rest;
        const found = flag === "--finding" ? findingsOf(runId, "whooper-ask").findings.find((f) => f.id === finding) : undefined;
        const about = found ? `finding ${found.id}, ${found.title}` : "the review";
        return `The fake host stands in for whooper-ask here, which would run the review's agent on ${about} and answer from the review's record, about: ${question}\n`;
      }
      case "skip":
        return `${mark(skipped, "whooper-skip", "skipped", runId, ...rest)}\n`;
      case "remove":
        return `${mark(removed, "whooper-remove", "removed", runId, ...rest)}\n`;
      case "retry": {
        const r = record(runId, "whooper-retry");
        removed.delete(runId);
        changed([`rechecks/${r.repo.replace("/", "-")}-${r.pr}.json`]);
        return `queued another review of ${r.repo}#${r.pr} at ${r.head_sha} for the next round; bin/whooper-round runs that round now\n`;
      }
      case "round":
        changed(null);
        return "";
      default:
        throw new Error(`the fake host does not run ${action}`);
    }
  }

  return {
    received,
    refusals,
    /** The calls of action the page made, each with its arguments. */
    calls: (action: string) => received.flatMap((m) => (m.type === "call" && m.action === action ? [m.args] : [])),
    /** Takes one message from the page. One that does not fit the protocol is dropped, as Prompting Time drops it. */
    receive(data: unknown) {
      const message = readPageMessage(data);
      if (!message) return;
      received.push(message);
      if (message.type === "ready") {
        if (options.openOnReady) post({ type: "open", route: options.openOnReady });
        return;
      }
      run(message.action, message.args).then(
        (json) => post({ type: "result", reply_to: message.id, json }),
        (error: Error) => post({ type: "error", reply_to: message.id, message: error.message }),
      );
    },
    /** Opens a route, as when the user opens a row or Command-J in Prompting Time. */
    open: (route: string) => post({ type: "open", route }),
    /** Says files changed, as Prompting Time's watch does, such as after a whooper-show's files changed. */
    changed: (files: string[] | null) => post({ type: "changed", files }),
    /** Changes the run's drafts as whooper-draft from a terminal would, and says drafts.json changed. */
    changeFromElsewhere(runId: string, op: string, ...rest: string[]) {
      draft(runId, op, ...rest);
    },
  };
}
