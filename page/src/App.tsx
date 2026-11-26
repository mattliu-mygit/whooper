import { Show } from "solid-js";
import { Queue } from "./Queue";
import { ReviewScreen } from "./Review";
import type { PageStore } from "./store";

/** The queue, or the open review. A review stays mounted while its run stays the same, so a
 * review message for the same run updates it in place. */
export function App(props: { store: PageStore }) {
  return (
    <Show when={props.store.state.review?.run_id} keyed fallback={<Queue store={props.store} />}>
      {(_runId) => <ReviewScreen store={props.store} />}
    </Show>
  );
}
