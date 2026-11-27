import { createSignal } from "solid-js";
import { CallError } from "./protocol";
import type { PageStore } from "./store";

/** What lets a submit through that whooper-submit refused with exit 11 (the review approves a run
 * that is not safe to approve) or exit 12 (the review @-mentions someone). */
const OVERRIDES: Record<number, { flag: string; label: string }> = {
  11: { flag: "--approve-anyway", label: "Approve anyway" },
  12: { flag: "--allow-mentions", label: "Send with the mentions" },
};

/**
 * The open review's decision, which the diff's submit bar and the walkthrough's decision bar both
 * offer: how many comments go out, Comment or Approve, which whooper-draft saves at once, and Submit
 * review, which Prompting Time confirms first. After a refusal that a flag lets through, `override`
 * names the button that sends the review again with that flag, with whooper-submit's stderr as the
 * reason.
 */
export function createDecision(store: PageStore) {
  const drafts = () => store.state.review!.drafts;
  const comments = () => store.state.review!.findings.findings.filter((f) => drafts().findings[f.id]?.included).length;
  const [pendingEvent, setPendingEvent] = createSignal<"APPROVE" | "COMMENT" | null>(null);
  const event = () => pendingEvent() ?? drafts().event;
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal("");
  // The flags the user added by pressing Approve anyway or Send with the mentions, which the next of
  // those buttons keeps, and the button that lets the last submit's refusal through.
  const [flags, setFlags] = createSignal<string[]>([]);
  const [refusal, setRefusal] = createSignal<{ flag: string; label: string; why: string } | null>(null);

  /** Runs work, shows its error, if any, and resolves with whether it succeeded. */
  async function run(work: () => Promise<unknown>): Promise<boolean> {
    setBusy(true);
    try {
      await work();
      setError("");
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  return {
    comments,
    event,
    busy,
    error,
    run,
    /** Whether there is nothing to submit: no comment, no message, and no approval. */
    nothing: () => comments() === 0 && event() === "COMMENT" && !drafts().message.trim(),
    override: () => (store.locked() ? null : refusal()),
    async choose(next: "APPROVE" | "COMMENT") {
      setPendingEvent(next);
      await run(() => store.changeDraft({ op: next === "APPROVE" ? "approve" : "comment" }));
      setPendingEvent(null);
    },
    /**
     * Prompting Time confirms first; a cancelled or refused confirmation comes back as the host's
     * error, with whooper-submit's exit code when it refused. Submit review sends no flags; each
     * override button adds its flag to those the user added before.
     */
    async submit(flag?: string) {
      const next = flag ? [...flags(), flag] : [];
      setFlags(next);
      setRefusal(null);
      await run(async () => {
        try {
          await store.submit(next);
        } catch (e) {
          const code = e instanceof CallError ? e.failure.exit_code : undefined;
          const allow = code == null ? undefined : OVERRIDES[code];
          // A refusal the next button lets through shows as that button's reason, not as an error.
          if (!(e instanceof CallError) || !allow) throw e;
          setRefusal({ ...allow, why: e.failure.stderr?.trim() || e.message });
        }
      });
    },
  };
}
