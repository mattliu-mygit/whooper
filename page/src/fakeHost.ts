import settingsSchema from "../../schemas/settings.json";
import * as fixtures from "./fixtures";
import { CallError, PROTOCOL_VERSION, readPageMessage, type Drafts, type FindingsResult, type HostMessage, type HostUpdate, type PageMessage, type Shown } from "./protocol";

// A stand-in for Prompting Time, for development (dev/index.html) and the page's tests. It runs the
// page's calls of the manifest's actions on the invented reviews in fixtures.ts the way Whooper's
// commands would: drafts change by whooper-draft's rules and refusals, a submit builds
// whooper-submit's request and asks confirmSubmit first, or is refused as whooper-submit refuses a
// comment outside the diff, an unsafe approval, or an @-mention, an ask answers at once, settings
// save by whooper-settings set's checks and refusals, and skip, remove, retry, pause, and resume
// change what status and show-review print, after which it says which files changed. It
// refuses an action the manifest does not declare, as Prompting Time does. It reaches no Whooper,
// GitHub, or model.

export interface FakeHostOptions {
  /** Prompting Time's confirmation: gets the text it would show for the request whooper-submit
   * --print wrote, and returns whether the user confirmed. Without it every submit is cancelled. A
   * request whooper-submit --print refuses never reaches it. */
  confirmSubmit?: (text: string) => boolean | Promise<boolean>;
  /** The route the host opens once the page is ready, as when the user opened a row in Prompting Time. */
  openOnReady?: string;
  /** Top-level fields of what show-review prints, by run ID, that replace the fixture's, as a test's
   * variation of a review, such as one without connections. */
  shown?: Record<string, Partial<Shown>>;
  /** What whooper-settings get prints, in place of the fixture's settings. */
  settings?: Record<string, unknown>;
  /** The error a call of action with args fails with instead of running, as whooper-retry's when gh
   * can't reach GitHub; null runs it. */
  refuse?: (action: string, args: string[]) => string | null;
}

/** The actions prompting-time-plugin.json declares. */
const DECLARED = new Set([
  "status", "round", "pause", "resume", "get-settings", "save-settings", "show-review", "draft", "ask", "skip", "remove", "retry", "submit",
]);

/** Whooper's comment marker and the message of an approval left without one (lib/whooper.py). */
const VETTED = "AI comment (vetted)";
const PRE_APPROVAL = "Pre-approving to unblock";
/** Stands in for whooper-submit's @-mention rule (MENTION in lib/whooper.py), close enough for the
 * fixtures; the page itself never looks for mentions. */
const MENTION = /(?<![\w.@/])@[A-Za-z0-9]/;
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

interface SchemaNode {
  type?: string;
  properties?: Record<string, SchemaNode>;
  enum?: unknown[];
  items?: SchemaNode;
  minimum?: number;
}

/** check_schema in lib/whooper.py, for the keywords schemas/settings.json uses: the first place value
 * breaks node, in its words. */
function schemaProblem(value: unknown, node: SchemaNode, where: string): string | null {
  const types: Record<string, (v: unknown) => boolean> = {
    object: (v) => typeof v === "object" && v !== null && !Array.isArray(v),
    array: Array.isArray,
    string: (v) => typeof v === "string",
    integer: Number.isInteger,
    number: (v) => typeof v === "number",
  };
  if (node.type && !types[node.type](value)) return `${where} is not of type ${node.type}`;
  if (node.enum && !node.enum.includes(value)) return `${where} is ${JSON.stringify(value)}, not one of ${JSON.stringify(node.enum)}`;
  if (node.minimum !== undefined && typeof value === "number" && value < node.minimum) return `${where} is below ${node.minimum}`;
  if (node.properties && types.object(value)) {
    const extra = Object.keys(value as object).filter((key) => !(key in node.properties!));
    if (extra.length > 0) return `${where} has unexpected ${extra.sort().join(", ")}`;
    for (const [key, sub] of Object.entries(node.properties)) {
      const found = key in (value as object) ? schemaProblem((value as Record<string, unknown>)[key], sub, `${where}.${key}`) : null;
      if (found) return found;
    }
  }
  if (node.items && Array.isArray(value))
    for (const [i, item] of value.entries()) {
      const found = schemaProblem(item, node.items, `${where}[${i}]`);
      if (found) return found;
    }
  return null;
}

/** write_settings in lib/whooper.py: the settings with the given JSON's merged in, or its refusal:
 * values that break the schema, or a schedule whooper-run could not follow. */
function saveSettings(current: Record<string, unknown>, json: string): Record<string, unknown> {
  let values: unknown;
  try {
    values = JSON.parse(json);
  } catch (e) {
    throw new Error(`whooper-settings: the values are not JSON: ${(e as Error).message}`);
  }
  const problem = schemaProblem(values, settingsSchema as SchemaNode, "settings");
  if (problem) throw new Error(`whooper-settings: ${problem}`);
  const merge = (table: Record<string, unknown>, changes: Record<string, unknown>) => {
    for (const [key, value] of Object.entries(changes))
      if (typeof value === "object" && value !== null && !Array.isArray(value)) merge((table[key] ??= {}) as Record<string, unknown>, value as Record<string, unknown>);
      else table[key] = value;
  };
  const merged = structuredClone(current);
  merge(merged, values as Record<string, unknown>);
  const schedule = merged.schedule as { start: string; end: string; days: string[] };
  const minutes = (time: string) => (/^\d{1,2}:\d{1,2}$/.test(time) ? Number(time.split(":")[0]) * 60 + Number(time.split(":")[1]) : NaN);
  if (Number.isNaN(minutes(schedule.start)) || Number.isNaN(minutes(schedule.end)))
    throw new Error('whooper: [schedule] start and end take a 24-hour time such as "18:00"; see config.toml');
  if (schedule.days.length === 0 || minutes(schedule.start) >= minutes(schedule.end))
    throw new Error("whooper: [schedule] needs at least one day and an end after its start; see config.toml");
  return merged;
}

export function createFakeHost(send: (message: HostMessage) => void, options: FakeHostOptions = {}) {
  // A test's findings stand for the run's own, which drafts and submits read too.
  const records = new Map(
    fixtures.reviewRecords().map((record) => {
      const findings = options.shown?.[record.run_id]?.findings;
      return [record.run_id, findings ? { ...record, findings } : record];
    }),
  );
  const drafts = new Map<string, Drafts>();
  const submitted = new Map<string, string>();
  const skipped = new Set<string>();
  const removed = new Set<string>();
  let paused = false;
  let settings = options.settings ?? fixtures.settings();
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
    // In status format 2, a removed review leaves the items, and a skipped or submitted one is done,
    // with no dot, and can't be skipped again.
    base.items = base.items.filter((item) => !removed.has(item.id));
    for (const item of base.items)
      if (skipped.has(item.id) || submitted.has(item.id)) {
        item.group = "done";
        item.dot = "idle";
        item.actions = item.actions?.filter((action) => action !== "skip");
      }
    // As status_v2 in lib/whooper.py words it, a cause that stops every review outranks a pause.
    base.paused = paused;
    if (paused && !base.blocked) base.agent = { words: "Paused", dot: "idle" };
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
      ...structuredClone(r.extras),
      ...structuredClone(options.shown?.[runId]),
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
   * or its refusal of an included comment on a line outside the PR's diff (exit 14), and without the
   * flag that allows it, of an approval of a run that is not safe to approve (exit 11) or of an
   * @-mention (exit 12), which it checks in that order. */
  function submitRequest(runId: string, flags: string[]): { text: string } | { refused: 11 | 12 | 14; stderr: string } {
    if (submitted.has(runId)) throw new Error(`whooper-submit: ${runId} was already submitted; see its submit.json`);
    const { repo, pr } = record(runId, "whooper-submit");
    const findings = findingsOf(runId, "whooper-submit");
    const choices = draftsOf(runId);
    const included = findings.findings.filter((f) => choices.findings[f.id]?.included);
    const outside = included.filter((f) => !f.in_diff);
    if (outside.length > 0)
      return {
        refused: 14,
        stderr: `whooper-submit: nothing posted: these included comments are on lines outside the PR's diff, where GitHub takes no comment, so it would refuse the whole review:\n${outside.map((f) => `  finding ${f.id}: ${f.title} (${f.path}:${f.line})\n`).join("")}Leave them out with bin/whooper-draft, or make their point in the review's message.\n`,
      };
    const comments = included.map((f) => {
      const text = (choices.findings[f.id].text ?? f.draft).trim();
      const body = /^\*\*[A-Za-z]+(?: \([^)]*\))?:\*\*/.test(text) ? text : `${LABELS[f.kind]} ${text}`;
      return { path: f.path, line: f.line, side: f.side, body: `${VETTED}\n\n${body}` };
    });
    if (comments.length === 0 && choices.event === "COMMENT" && !choices.message.trim())
      throw new Error(
        "whooper-submit: nothing to submit: the drafts include no comment, have no message, and do not approve; change them with bin/whooper-draft",
      );
    const message =
      choices.event === "APPROVE" && !choices.message.trim() ? (comments.length > 0 ? PRE_APPROVAL : "") : choices.message;
    const unsafe = concerns(findings);
    if (choices.event === "APPROVE" && unsafe.length > 0 && !flags.includes("--approve-anyway"))
      return {
        refused: 11,
        stderr: `whooper-submit: nothing posted: the review approves, and the run is not safe to approve:\n${unsafe.map((c) => `  ${c}\n`).join("")}Pass --approve-anyway to approve it anyway, or make the review only comment with bin/whooper-draft.\n`,
      };
    if ([message, ...comments.map((c) => c.body)].some((text) => MENTION.test(text)) && !flags.includes("--allow-mentions"))
      return {
        refused: 12,
        stderr: "whooper-submit: nothing posted: the review @-mentions someone, and posting would notify them.\nPass --allow-mentions to post it anyway.\n",
      };
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
    const refusal = options.refuse?.(action, args);
    if (refusal) throw new Error(refusal);
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
        // Prompting Time shows a refusal in its confirmation, which the user can only close, and then
        // tells the page the preview's exit code and stderr.
        if ("refused" in preview) {
          refusals.push(preview.refused);
          throw new CallError(preview.stderr.trim(), { stage: "preview", exit_code: preview.refused, stderr: preview.stderr });
        }
        if (!(await options.confirmSubmit?.(preview.text))) throw new CallError("Nothing ran: the confirmation was cancelled.", { stage: "cancelled" });
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
      case "get-settings":
        return structuredClone(settings);
      case "save-settings":
        settings = saveSettings(settings, runId);
        return "";
      case "round":
        changed(null);
        return "";
      case "pause":
      case "resume":
        paused = action === "pause";
        changed(["paused"]);
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
        (error: Error) =>
          post({ type: "error", reply_to: message.id, message: error.message, ...(error instanceof CallError ? error.failure : {}) }),
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
