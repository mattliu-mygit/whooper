import { Show } from "solid-js";
import { ReviewScreen } from "./Review";
import type { PageStore } from "./store";

/** The review Prompting Time opened, and nothing until it sends one: Prompting Time's sidebar lists
 * the reviews, so the page has no queue of its own. A review stays mounted while its run stays the
 * same, so a review message for the same run updates it in place. */
export function App(props: { store: PageStore }) {
  return (
    <Show when={props.store.state.review?.run_id} keyed>
      {(_runId) => <ReviewScreen store={props.store} />}
    </Show>
  );
}
