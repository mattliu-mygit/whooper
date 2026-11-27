import { Match, Show, Switch } from "solid-js";
import { CauseScreen, FailedReview, RunningReview } from "./FailedReview";
import { Home } from "./Home";
import { ReviewScreen } from "./Review";
import { ReviewHeader } from "./ReviewHeader";
import type { PageStore } from "./store";

/**
 * The route Prompting Time opened, and nothing until it opens one: Whooper's home at /, which
 * Prompting Time opens from Whooper's name, a review, or a cause that stops every review. Home lists
 * the reviews and causes whooper-status lists. A review shows its header, then its diff when it
 * finished with findings, or the screen for a review that runs, waits, or failed; a cause that stops
 * every review shows the reviews it holds back. Each review stays
 * mounted while its run stays the same, so a new read of the same run updates it in place.
 */
export function App(props: { store: PageStore }) {
  const store = props.store;
  const review = () => {
    const place = store.place();
    return place?.kind === "review" ? place.runId : null;
  };
  const cause = () => {
    const place = store.place();
    return place?.kind === "cause" ? place.cause : null;
  };
  return (
    <Switch>
      <Match when={store.place()?.kind === "home"}>
        <div class="w-page-body">
          <Home store={store} />
        </div>
      </Match>
      <Match when={review()} keyed>
        {(runId: string) => (
          <div class="w-page">
            <ReviewHeader store={store} />
            <div class="w-page-body">
              <Show
                when={store.state.shown?.run_id === runId ? store.state.shown : null}
                fallback={
                  <Show when={store.state.loadError} fallback={<p class="w-hint w-column" role="status">Reading the review…</p>}>
                    {(error) => (
                      <p class="w-error w-column" role="alert">
                        {error()}
                      </p>
                    )}
                  </Show>
                }
              >
                {(shown) => (
                  <Switch>
                    <Match when={store.state.review?.run_id === runId}>
                      <ReviewScreen store={store} />
                    </Match>
                    <Match when={shown().outcome === "running"}>
                      <RunningReview shown={shown()} />
                    </Match>
                    <Match when={true}>
                      <FailedReview store={store} shown={shown()} />
                    </Match>
                  </Switch>
                )}
              </Show>
            </div>
          </div>
        )}
      </Match>
      <Match when={cause()} keyed>
        {(id: string) => (
          <div class="w-page-body">
            <CauseScreen store={store} cause={id} />
          </div>
        )}
      </Match>
    </Switch>
  );
}
