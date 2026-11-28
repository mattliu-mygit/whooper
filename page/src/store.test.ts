import { createRoot, flush } from "solid-js";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CallError, type PageMessage } from "./protocol";
import { createPageStore, HOLD_MS } from "./store";

/** A store wired to a host the test answers by hand. */
function storeWithHost() {
  const posted: PageMessage[] = [];
  let toPage: (data: unknown) => void = () => {};
  const store = createRoot(() =>
    createPageStore({
      post: (message) => posted.push(message as PageMessage),
      listen: (receive) => {
        toPage = receive;
        return () => {};
      },
    }),
  );
  const send = (message: object) => toPage({ version: 2, ...message });
  /** The id of the last call of action. */
  const lastCall = (action: string) => posted.flatMap((m) => (m.type === "call" && m.action === action ? [m.id] : [])).at(-1)!;
  const held = () => (flush(), store.held());
  const asking = () => (flush(), store.asking());
  /** The arguments of each call of action. */
  const calls = (action: string) => posted.flatMap((m) => (m.type === "call" && m.action === action ? [m.args] : []));
  return { store, send, lastCall, held, asking, calls };
}

describe("the page store", () => {
  beforeEach(() => vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] }));
  afterEach(() => vi.useRealTimers());

  it("holds the action buttons while an action runs, for HOLD_MS after its reply, and HOLD_MS after an open in that time", async () => {
    const { store, send, lastCall, held } = storeWithHost();
    send({ type: "open", route: "/reviews/r1" });
    expect(held()).toBe(false);

    const skipped = store.skip();
    expect(held()).toBe(true);
    send({ type: "result", reply_to: lastCall("skip"), json: "marked r1 as skipped\n" });
    await skipped;
    expect(held()).toBe(true);

    // Prompting Time opens the next item 400 ms after the reply, which holds the buttons 600 ms more.
    vi.advanceTimersByTime(400);
    send({ type: "open", route: "/reviews/r2" });
    vi.advanceTimersByTime(HOLD_MS - 1);
    expect(held()).toBe(true);
    vi.advanceTimersByTime(1);
    expect(held()).toBe(false);

    // An open after the hold ended holds nothing.
    send({ type: "open", route: "/reviews/r3" });
    expect(held()).toBe(false);
  });

  it("holds after a failed action too, and rejects with the host's stage, exit code, and stderr", async () => {
    const { store, send, lastCall, held } = storeWithHost();
    send({ type: "open", route: "/reviews/r1" });
    const submitted = store.submit();
    const stderr = "whooper-submit: nothing posted: the review @-mentions someone\n";
    send({ type: "error", reply_to: lastCall("submit"), message: stderr.trim(), stage: "preview", exit_code: 12, stderr });
    const error = await submitted.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(CallError);
    expect((error as CallError).message).toBe(stderr.trim());
    expect((error as CallError).failure).toEqual({ stage: "preview", exit_code: 12, stderr });
    expect(held()).toBe(true);
    vi.advanceTimersByTime(HOLD_MS);
    expect(held()).toBe(false);
  });

  it("runs an action once per run, each after the last replied, holding throughout, and reads the status once at the end", async () => {
    const { store, send, lastCall, held, calls } = storeWithHost();
    // A reply settles in a few microtasks.
    const settle = async () => {
      for (let i = 0; i < 5; i++) await Promise.resolve();
    };
    const each = store.actOnEach("remove", ["r1", "r2"], ["--undo"]);
    expect(calls("remove")).toEqual([["r1", "--undo"]]);
    expect(held()).toBe(true);
    send({ type: "result", reply_to: lastCall("remove"), json: "unmarked r1 as removed\n" });
    await settle();
    expect(calls("remove")).toEqual([["r1", "--undo"], ["r2", "--undo"]]);
    expect(calls("status")).toEqual([]);
    expect(held()).toBe(true);
    send({ type: "error", reply_to: lastCall("remove"), message: "whooper-remove: no run r2 in the state folder's runs" });
    expect(await each).toEqual([
      { id: "r1", printed: "unmarked r1 as removed" },
      { id: "r2", error: "whooper-remove: no run r2 in the state folder's runs" },
    ]);
    expect(calls("status")).toEqual([[]]);
    vi.advanceTimersByTime(HOLD_MS);
    expect(held()).toBe(false);
  });

  it("holds only the Ask buttons while an ask runs, so other actions stay usable, and every button for HOLD_MS after its reply", async () => {
    const { store, send, lastCall, held, asking } = storeWithHost();
    send({ type: "open", route: "/reviews/r1" });
    const asked = store.ask("Why?", "1");
    expect(asking()).toBe(true);
    expect(held()).toBe(false);

    // Skip runs and ends while the ask still runs, and holds the buttons HOLD_MS after its reply.
    const skipped = store.skip();
    expect(held()).toBe(true);
    send({ type: "result", reply_to: lastCall("skip"), json: "marked r1 as skipped\n" });
    await skipped;
    vi.advanceTimersByTime(HOLD_MS);
    expect(held()).toBe(false);
    expect(asking()).toBe(true);

    send({ type: "result", reply_to: lastCall("ask"), json: "It timed out.\n" });
    expect(await asked).toBe("It timed out.");
    expect(asking()).toBe(false);
    expect(held()).toBe(true);
    expect(store.state.answers["1"]).toEqual([{ question: "Why?", answer: "It timed out." }]);
    vi.advanceTimersByTime(HOLD_MS - 1);
    expect(held()).toBe(true);
    vi.advanceTimersByTime(1);
    expect(held()).toBe(false);
  });

  it("leaves the hold to an action that still runs when an ask's reply arrives", async () => {
    const { store, send, lastCall, held, asking } = storeWithHost();
    send({ type: "open", route: "/reviews/r1" });
    const asked = store.ask("Why?", null);
    const submitted = store.submit();
    send({ type: "error", reply_to: lastCall("ask"), message: "whooper-ask: the review is still running" });
    await expect(asked).rejects.toThrow("still running");
    expect(asking()).toBe(false);
    vi.advanceTimersByTime(HOLD_MS);
    expect(held()).toBe(true);
    send({ type: "result", reply_to: lastCall("submit"), json: "posted\n" });
    await submitted;
    vi.advanceTimersByTime(HOLD_MS - 1);
    expect(held()).toBe(true);
    vi.advanceTimersByTime(1);
    expect(held()).toBe(false);
  });
});
