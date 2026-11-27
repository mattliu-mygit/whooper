import { createEffect, Match, Show, snapshot, Switch, untrack } from "solid-js";
import type { Overview, Review } from "../protocol";
import { ReviewScreen } from "../Review";
import type { PageStore } from "../store";
import { ConnectionsView } from "./Connections";
import { ExamplesView } from "./Examples";
import type { LastLook, ReviewMemory } from "./memory";
import { ReviewTop } from "./ReviewTop";
import { TestGapsView } from "./TestGaps";
import { viewHasData, type FullViewId, type ReviewViews } from "./views";
import { Walkthrough } from "./Walkthrough";

/** What a review with an overview shows: its walkthrough, its diff, or a view in full in the walkthrough's place, such as the map of how the changed code connects. */
export type ReviewMode = "walkthrough" | "diff" | FullViewId;

/**
 * A finished review that wrote an overview: it opens on its walkthrough, headed by the verdict banner
 * and the since notice, which head its Diff too. A view's summary in the walkthrough opens the view
 * in full in its place, which keeps the walkthrough as it was to come back to; turning that view off,
 * or a new read without its data, goes back to the walkthrough. Walkthrough | Diff and the Views
 * menu sit in the review's header.
 */
export function OverviewReview(props: {
  store: PageStore;
  memory: ReviewMemory;
  views: ReviewViews;
  look: LastLook | null;
  mode: ReviewMode;
  onMode(mode: ReviewMode): void;
}) {
  const store = props.store;
  const review = () => store.state.review as Review & { overview: Overview };
  /** The view that shows in full in the walkthrough's place, while it is on and the review has its data. */
  const full = (): FullViewId | null => {
    const mode = props.mode;
    return mode !== "walkthrough" && mode !== "diff" && props.views.isOn(mode) && viewHasData(mode, review()) ? mode : null;
  };
  createEffect(
    () => props.mode !== "walkthrough" && props.mode !== "diff" && full() === null,
    (gone) => {
      if (gone) untrack(() => props.onMode("walkthrough"));
    },
  );
  const removed = () => !store.earlier() && store.entry()?.removed_by_user === true;
  const top = () => <ReviewTop store={store} memory={props.memory} views={props.views} look={props.look} />;
  const back = () => props.onMode("walkthrough");
  return (
    <Show when={props.mode !== "diff"} fallback={<ReviewScreen store={store} top={top()} />}>
      <div class="review-screen">
        <Walkthrough
          store={store}
          memory={props.memory}
          views={props.views}
          top={top()}
          active={full() === null}
          canDecide={!store.locked() && !removed()}
          onShowDiff={() => props.onMode("diff")}
          onOpenView={(id) => props.onMode(id)}
        />
        <Switch>
          {/* Plain copies of the store's data, which the views read in keyed lists and the diff's callbacks. */}
          <Match when={full() === "connections" && review().connections}>
            {(connections) => <ConnectionsView connections={snapshot(connections())} patch={review().patch} onBack={back} />}
          </Match>
          <Match when={full() === "test-gaps" && review().test_gaps}>
            {(testGaps) => <TestGapsView testGaps={snapshot(testGaps())} patch={review().patch} onBack={back} />}
          </Match>
          <Match when={full() === "inputs" && review().examples}>
            {(examples) => <ExamplesView examples={snapshot(examples())} onBack={back} />}
          </Match>
        </Switch>
      </div>
    </Show>
  );
}
