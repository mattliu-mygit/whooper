import * as fixtures from "./fixtures";
import {
  PROTOCOL_VERSION,
  readPageMessage,
  type DraftChange,
  type Drafts,
  type FindingsResult,
  type HostMessage,
  type HostUpdate,
  type PageMessage,
  type QueueItem,
  type Review,
} from "./protocol";

// A stand-in for Prompting Time, for development (dev/index.html) and the page's tests. It answers
// the page's messages from the invented reviews in fixtures.ts the way Whooper's commands would:
// drafts change by whooper-draft's rules and refusals, a submit builds whooper-submit's request and
// asks confirmSubmit first, and an ask answers at once. It reaches no Whooper, GitHub, or model.

export interface FakeHostOptions {
  /** Prompting Time's confirmation: gets the text it would show for the request whooper-submit
   * --print wrote, and returns whether the user confirmed. Without it every submit is cancelled. */
  confirmSubmit?: (text: string) => boolean | Promise<boolean>;
  /** A run whose review the host sends after ready, as when the user opened it in Prompting Time. */
  openOnReady?: string;
}

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
  const queue: QueueItem[] = fixtures.queue();
  const submitted = new Set<string>();
  /** Every message the page sent that fits the protocol, in order. */
  const received: PageMessage[] = [];

  const reply = (to: number | undefined, update: HostUpdate) =>
    send({ ...update, version: PROTOCOL_VERSION, ...(to === undefined ? {} : { reply_to: to }) });

  function record(runId: string) {
    const found = records.get(runId);
    if (!found) throw new Error(`whooper-draft: no run ${runId} in the state folder's runs`);
    return found;
  }

  function draftsOf(runId: string): Drafts {
    if (!drafts.has(runId)) drafts.set(runId, structuredClone(record(runId).drafts ?? firstDrafts(record(runId).findings)));
    return drafts.get(runId)!;
  }

  function review(runId: string): Review {
    const { drafts: _saved, ...rest } = structuredClone(record(runId));
    return { ...rest, drafts: structuredClone(draftsOf(runId)) };
  }

  /** change_drafts in lib/whooper.py: one change, with its refusals. */
  function changeDrafts(runId: string, change: DraftChange): Drafts {
    if (submitted.has(runId)) throw new Error(`whooper-draft: ${runId} was already submitted; see its submit.json`);
    if (change.op === "text" && !change.text.trim())
      throw new Error("whooper-draft: the text is empty; to drop the comment, leave the finding out");
    const current = draftsOf(runId);
    if (change.op === "include" || change.op === "leave-out" || change.op === "text") {
      const ids = record(runId).findings.findings.map((f) => f.id);
      if (!ids.includes(change.finding))
        throw new Error(`whooper-draft: the run has no finding ${change.finding}; its findings are ${ids.join(", ") || "none"}`);
      const choice = (current.findings[change.finding] ??= { included: false });
      if (change.op === "text") choice.text = change.text;
      else choice.included = change.op === "include";
    } else if (change.op === "message") current.message = change.text;
    else current.event = change.op === "approve" ? "APPROVE" : "COMMENT";
    return structuredClone(current);
  }

  /** whooper-submit --print's request, and the text Prompting Time's confirmation would show for it. */
  function submitRequest(runId: string): string {
    if (submitted.has(runId)) throw new Error(`whooper-submit: ${runId} was already submitted; see its submit.json`);
    const { findings, repo, pr } = record(runId);
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
    const request = { commit_id: "0123456789abcdef0123456789abcdef01234567", event: choices.event, body: message, comments };
    const unsafe = choices.event === "APPROVE" ? concerns(findings) : [];
    return [
      `Submit this review to ${repo}#${pr}?`,
      ...(unsafe.length > 0 ? ["Whooper says the run is not safe to approve:", ...unsafe.map((c) => `  ${c}`)] : []),
      JSON.stringify(request, null, 2),
    ].join("\n");
  }

  async function handle(message: PageMessage) {
    switch (message.type) {
      case "ready":
        reply(message.id, { type: "status", status: fixtures.status(), queue: structuredClone(queue) });
        if (options.openOnReady) reply(undefined, { type: "review", review: review(options.openOnReady) });
        break;
      case "open-review":
        reply(message.id, { type: "review", review: review(message.run_id) });
        break;
      case "change-draft":
        reply(message.id, { type: "drafts", run_id: message.run_id, drafts: changeDrafts(message.run_id, message.change) });
        break;
      case "submit": {
        const text = submitRequest(message.run_id);
        if (!(await options.confirmSubmit?.(text))) {
          reply(message.id, { type: "submit-result", run_id: message.run_id, posted: false, message: "cancelled" });
          break;
        }
        const { repo, pr } = record(message.run_id);
        submitted.add(message.run_id);
        for (const item of queue) if (item.run_id === message.run_id) item.state = "submitted";
        reply(message.id, {
          type: "submit-result",
          run_id: message.run_id,
          posted: true,
          message: `posted the review: https://github.com/${repo}/pull/${pr}#pullrequestreview-1`,
        });
        reply(undefined, { type: "status", status: fixtures.status(), queue: structuredClone(queue) });
        break;
      }
      case "ask": {
        const finding = record(message.run_id).findings.findings.find((f) => f.id === message.finding);
        const about = finding ? `finding ${finding.id}, ${finding.title}` : "the review";
        reply(message.id, {
          type: "answer",
          run_id: message.run_id,
          finding: message.finding,
          question: message.question,
          answer: `The fake host stands in for whooper-ask here, which would run the review's agent on ${about} and answer from the review's record.`,
        });
        break;
      }
    }
  }

  return {
    received,
    /** Takes one message from the page. One that does not fit the protocol is dropped, as Prompting Time drops it. */
    receive(data: unknown) {
      const message = readPageMessage(data);
      if (!message) return;
      received.push(message);
      handle(message).catch((error: Error) => reply(message.id, { type: "error", message: error.message }));
    },
    /** Sends the run's review again, as the host does when the run's files change. */
    resendReview(runId: string) {
      reply(undefined, { type: "review", review: review(runId) });
    },
    /** Changes the run's drafts as whooper-draft from a terminal would, and tells the page. */
    changeFromElsewhere(runId: string, change: DraftChange) {
      reply(undefined, { type: "drafts", run_id: runId, drafts: changeDrafts(runId, change) });
    },
  };
}
