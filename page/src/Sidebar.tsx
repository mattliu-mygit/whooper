import { For, Show } from "solid-js";
import { StatusDot } from "@prompting-time/ui";
import type { PageStore } from "./store";
import { waitingOnYou } from "./Reviews";
import { TITLE } from "./text";

/** The page's tabs, each at its route. */
export type Tab = "reviews" | "past" | "causes" | "settings";

/** Whooper's row as whooper-status words it, which names a pause that a cause outranks there too. */
export function agentWords(store: PageStore): string {
  const status = store.state.status;
  if (!status) return "";
  return status.paused && !status.agent.words.startsWith("Paused") ? `Paused · ${status.agent.words}` : status.agent.words;
}

/**
 * The page's own sidebar, which stands in for Prompting Time's while it folds to a rail: Whooper's name
 * and status, then its tabs, Reviews with how many of its reviews wait on you, Past reviews, Causes
 * with how many stop every review, and Settings. Each opens its route; the open one is marked, also while one of its reviews or
 * causes shows. While a review shows, « at its top folds it back to the rail.
 */
export function Sidebar(props: { store: PageStore; tab: Tab; onFold?: () => void }) {
  const store = props.store;
  const status = () => store.state.status;
  const tabs = (): { id: Tab; label: string; route: string; count?: number }[] => [
    { id: "reviews", label: "Reviews", route: "/", count: status() ? waitingOnYou(status()!.items) : undefined },
    { id: "past", label: "Past reviews", route: "/past" },
    { id: "causes", label: "Causes", route: "/causes", count: status()?.blockers.length },
    { id: "settings", label: "Settings", route: "/settings" },
  ];
  return (
    <nav id="w-tabs" class="w-nav" aria-label={TITLE}>
      <div class="w-nav-head">
        <div class="w-nav-title">
          <p class="w-nav-name">{TITLE}</p>
          <Show when={props.onFold}>
            {(fold) => (
              <button type="button" class="w-nav-fold" aria-label="Fold the tabs" aria-expanded="true" title="Fold the tabs" onClick={() => fold()()}>
                «
              </button>
            )}
          </Show>
        </div>
        <Show when={status()}>
          {(current) => (
            <p class="w-nav-status">
              <StatusDot status={current().agent.dot} />
              <span>{agentWords(store)}</span>
            </p>
          )}
        </Show>
      </div>
      <ul class="w-nav-tabs">
        <For each={tabs()} keyed={(tab) => tab.id}>
          {(tab) => (
            <li>
              <button
                type="button"
                class="w-nav-tab"
                aria-current={props.tab === tab().id ? "page" : undefined}
                onClick={() => store.open(tab().route)}
              >
                <span>{tab().label}</span>
                <Show when={tab().count}>{(count) => <span class="w-nav-count">{count()}</span>}</Show>
              </button>
            </li>
          )}
        </For>
      </ul>
    </nav>
  );
}
