import { createSignal, For, Show } from "solid-js";
import { ListRow, StatusDot, type Status } from "@prompting-time/ui";
import { PullRequestIcon } from "./icons";
import type { QueueItem, WhooperStatus } from "./protocol";
import type { PageStore } from "./store";
import { verdictText } from "./text";

const GROUPS: { state: QueueItem["state"]; title: string; status: Status }[] = [
  { state: "decide", title: "Needs you", status: "attention" },
  { state: "reviewing", title: "Reviewing", status: "running" },
  { state: "failed", title: "Failed", status: "error" },
  { state: "submitted", title: "Submitted", status: "idle" },
];

function clock(iso: string) {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** One line on what Whooper's loop does, with a dot as in the agreed sidebar's Whooper row. */
function loopLine(status: WhooperStatus): { status: Status; text: string } {
  const next = status.next_round ? ` · next round ${clock(status.next_round)}` : "";
  if (status.round.running)
    return { status: "running", text: status.round.reviewing ? `reviewing ${status.round.reviewing}` : "polling" };
  if (status.paused) return { status: "idle", text: "paused" };
  if (!status.loop.running) return { status: "idle", text: "not running" };
  if (status.last_round?.result === "poll_failed") return { status: "error", text: `the last poll failed${next}` };
  return { status: "success", text: `running${next}` };
}

function meta(item: QueueItem): string {
  const what = {
    decide: item.verdict ? verdictText(item.verdict) : "waiting for you",
    reviewing: "Whooper is reviewing",
    failed: "review failed",
    submitted: "submitted",
  }[item.state];
  return `${item.pr} · ${what}`;
}

/** Whooper's queue: the reviews waiting on the user first. A review that waits opens on a click. */
export function Queue(props: { store: PageStore }) {
  const [error, setError] = createSignal("");
  const open = async (item: QueueItem) => {
    if (item.state !== "decide" || !item.run_id) return;
    try {
      await props.store.openReview(item.run_id);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <main class="w-queue" aria-label="Whooper's reviews">
      <header class="w-queue-header">
        <h1>Whooper</h1>
        <Show when={props.store.state.status}>
          {(status) => (
            <p class="w-loop">
              <StatusDot status={loopLine(status()).status} />
              {loopLine(status()).text}
            </p>
          )}
        </Show>
      </header>
      <Show when={error()}>
        <p class="w-error" role="alert">
          {error()}
        </p>
      </Show>
      <For each={GROUPS}>
        {(group) => (
          <Show when={props.store.state.queue.some((item) => item.state === group.state)}>
            <section class="w-group" aria-label={group.title}>
              <h2>{group.title}</h2>
              <ul>
                <For each={props.store.state.queue.filter((item) => item.state === group.state)}>
                  {(item) => (
                    <li>
                      <ListRow
                        icon={<PullRequestIcon />}
                        title={item.title ?? item.pr}
                        count={item.findings > 0 ? item.findings : undefined}
                        status={group.status}
                        meta={meta(item)}
                        onClick={() => open(item)}
                      />
                    </li>
                  )}
                </For>
              </ul>
            </section>
          </Show>
        )}
      </For>
      <Show when={props.store.state.status && props.store.state.queue.length === 0}>
        <p class="w-hint">Nothing waits for you.</p>
      </Show>
    </main>
  );
}
