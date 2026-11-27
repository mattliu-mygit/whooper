import { createRoot } from "solid-js";
import { describe, expect, it } from "vitest";
import { CallError, type PageMessage } from "./protocol";
import { createPageStore } from "./store";

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
  return { store, send, lastCall };
}

describe("the page store", () => {
  it("rejects a failed call with the host's stage, exit code, and stderr", async () => {
    const { store, send, lastCall } = storeWithHost();
    send({ type: "open", route: "/reviews/r1" });
    const submitted = store.submit();
    const stderr = "whooper-submit: nothing posted: the review @-mentions someone\n";
    send({ type: "error", reply_to: lastCall("submit"), message: stderr.trim(), stage: "preview", exit_code: 12, stderr });
    const error = await submitted.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(CallError);
    expect((error as CallError).message).toBe(stderr.trim());
    expect((error as CallError).failure).toEqual({ stage: "preview", exit_code: 12, stderr });
  });
});
