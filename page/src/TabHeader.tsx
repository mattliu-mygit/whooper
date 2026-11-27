import { createSignal, Show } from "solid-js";
import { Button } from "@prompting-time/ui";
import type { PageStore } from "./store";

/**
 * The header of one of the page's tabs: its title, one line about it, and Whooper's own controls,
 * Pause or Resume and Poll now, the manifest's pause, resume, and round actions. Start and Stop stay
 * in Prompting Time, which starts and stops bin/whooper-run; no action of Whooper's does either.
 * Poll now says Polling… while a round runs, which can take minutes, and holds no other button.
 */
export function TabHeader(props: { store: PageStore; title: string; words: string }) {
  const store = props.store;
  const [error, setError] = createSignal<string | null>(null);
  const paused = () => store.state.status?.paused === true;

  async function run(task: () => Promise<string>) {
    setError(null);
    try {
      await task();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <header class="w-tab-header">
      <div class="w-tab-header-bar">
        <h1 id="tab-title">{props.title}</h1>
        <span class="w-tab-words">{props.words}</span>
        <span class="w-spacer" />
        <Button
          disabled={store.held() || store.state.status === null}
          onClick={() => run(() => (paused() ? store.resume() : store.pause()))}
        >
          {paused() ? "Resume" : "Pause"}
        </Button>
        <Button variant="primary" disabled={store.polling()} onClick={() => run(() => store.round())}>
          {store.polling() ? "Polling…" : "Poll now"}
        </Button>
      </div>
      <Show when={error()}>
        {(text) => (
          <p class="w-error" role="alert">
            {text()}
          </p>
        )}
      </Show>
    </header>
  );
}
