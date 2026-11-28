import { createSignal, For, Show } from "solid-js";
import { Button } from "@prompting-time/ui";
import { CallError } from "./protocol";
import type { PageStore } from "./store";

/** What lets a submit through that whooper-submit refused with exit 11 (the review approves a run that
 * is not safe to approve) or exit 12 (the review @-mentions someone). */
const OVERRIDES: Record<number, { flag: string; label: string }> = {
  11: { flag: "--approve-anyway", label: "Approve anyway" },
  12: { flag: "--allow-mentions", label: "Send with the mentions" },
};
/** whooper-submit's exit code when an included comment sits on a line outside the PR's diff, where
 * GitHub takes no comment (OUTSIDE_DIFF_EXIT in lib/whooper.py). No flag lets it through: the user
 * leaves those comments out, or makes their point in the review's message. */
const OUTSIDE_DIFF = 14;

/** A submit whooper-submit refused for a reason the page knows: why, in words, the comments it named,
 * and the flag and button that let it through, if one does. */
interface Refusal {
  why: string;
  named: string[];
  override: { flag: string; label: string } | null;
}

/** The refusal a submit's error is, or null for any other failure, which shows as an error. */
function refusalOf(e: unknown): Refusal | null {
  if (!(e instanceof CallError)) return null;
  const { exit_code: code, stderr = "" } = e.failure;
  if (code === OUTSIDE_DIFF)
    return {
      why: "Some included comments sit on lines outside this PR's change. Leave them out or make the point in the review's message.",
      // whooper-submit names each on a line of its own: "  finding 2: <title> (<path>:<line>)".
      named: stderr
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => line.startsWith("finding ")),
      override: null,
    };
  const override = code == null ? undefined : OVERRIDES[code];
  // A refusal the next button lets through shows whooper-submit's stderr as that button's reason.
  return override ? { why: stderr.trim() || e.message, named: [], override } : null;
}

export type Decision = ReturnType<typeof createDecision>;

/**
 * The open review's decision, which the diff's submit bar and the walkthrough's decision bar both
 * offer: how many comments go out, Comment or Approve, which whooper-draft saves at once, the review's
 * message, which it saves when the box loses focus, and Submit review, which Prompting Time confirms
 * first. After a refusal the page knows, `refusal` says why, and names the button that sends the
 * review again with the flag that lets it through, if one does.
 */
export function createDecision(store: PageStore) {
  const drafts = () => store.state.review!.drafts;
  const comments = () => store.state.review!.findings.findings.filter((f) => drafts().findings[f.id]?.included).length;
  const [pendingEvent, setPendingEvent] = createSignal<"APPROVE" | "COMMENT" | null>(null);
  const event = () => pendingEvent() ?? drafts().event;
  // What the user typed in the message box since the drafts last held it, or null; otherwise the box
  // shows the drafts' message.
  const [typed, setTyped] = createSignal<string | null>(null);
  const message = () => typed() ?? drafts().message;
  // The message save that the box's blur started. The blur comes from the very click that submits, so
  // the save disables no button, which would swallow that click; the submit waits for it instead.
  let saving: Promise<unknown> = Promise.resolve();
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal("");
  // The flags the user added by pressing Approve anyway or Send with the mentions, which the next of
  // those buttons keeps, and the last submit's refusal.
  const [flags, setFlags] = createSignal<string[]>([]);
  const [refusal, setRefusal] = createSignal<Refusal | null>(null);

  /** Runs work, shows its error, if any, and resolves with whether it succeeded. */
  async function attempt(work: () => Promise<unknown>): Promise<boolean> {
    try {
      await work();
      setError("");
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    }
  }

  /** attempt, with the decision's buttons disabled while work runs. */
  async function run(work: () => Promise<unknown>): Promise<boolean> {
    setBusy(true);
    try {
      return await attempt(work);
    } finally {
      setBusy(false);
    }
  }

  /** Saves what the user typed with whooper-draft, unless the drafts hold it already. */
  async function saveTyped() {
    const text = typed();
    if (text === null) return;
    if (text !== drafts().message) await store.changeDraft({ op: "message", text });
    // What the user typed since the save started stays in the box.
    if (typed() === text) setTyped(null);
  }

  return {
    comments,
    event,
    message,
    busy,
    error,
    /** What posts when the message stays empty (whooper-submit): an approval with comments says
     * "Pre-approving to unblock". */
    placeholder: () => (event() === "APPROVE" ? (comments() > 0 ? "Pre-approving to unblock" : "No message") : "Message (optional)"),
    /** Whether there is nothing to submit: no comment, no message, and no approval. */
    nothing: () => comments() === 0 && event() === "COMMENT" && !message().trim(),
    refusal: () => (store.locked() ? null : refusal()),
    async choose(next: "APPROVE" | "COMMENT") {
      setPendingEvent(next);
      await run(() => store.changeDraft({ op: next === "APPROVE" ? "approve" : "comment" }));
      setPendingEvent(null);
    },
    /** Shows the box's text as the user types it. */
    typeMessage: (text: string) => setTyped(text),
    /** Saves the box's text, as its blur does. */
    saveMessage() {
      saving = attempt(saveTyped);
    },
    /**
     * Prompting Time confirms first; a cancelled or refused confirmation comes back as the host's
     * error, with whooper-submit's exit code when it refused. The message the box holds is saved
     * first, so the confirmation shows it. Submit review sends no flags; each override button adds its
     * flag to those the user added before.
     */
    async submit(flag?: string) {
      const next = flag ? [...flags(), flag] : [];
      setFlags(next);
      setRefusal(null);
      await run(async () => {
        // The save the box's blur started, then whatever it did not save, such as after it failed.
        await saving;
        await saveTyped();
        try {
          await store.submit(next);
        } catch (e) {
          // A refusal the page knows shows as the decision's refusal, not as an error.
          const known = refusalOf(e);
          if (!known) throw e;
          setRefusal(known);
        }
      });
    },
  };
}

/**
 * whooper-submit's refusal of the last submit, as both bars show it: why, each comment it named, and
 * the button that sends the review again with the flag that lets it through, if one does.
 */
export function SubmitRefusal(props: { store: PageStore; decision: Decision; class?: string }) {
  return (
    <Show when={props.decision.refusal()}>
      {(refusal) => (
        <>
          <div class={`w-hint w-refusal${props.class ? ` ${props.class}` : ""}`}>
            {refusal().why}
            <Show when={refusal().named.length > 0}>
              <ul class="w-refused">
                <For each={refusal().named}>{(name) => <li>{name}</li>}</For>
              </ul>
            </Show>
          </div>
          <Show when={refusal().override}>
            {(override) => (
              <Button disabled={props.decision.busy() || props.store.held()} onClick={() => props.decision.submit(override().flag)}>
                {override().label}
              </Button>
            )}
          </Show>
        </>
      )}
    </Show>
  );
}
