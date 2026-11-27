import { createEffect, createSignal, Match, Show, Switch, untrack } from "solid-js";
import { CauseScreen, FailedReview, RunningReview } from "./FailedReview";
import { Home } from "./Home";
import { ReviewScreen } from "./Review";
import { ReviewHeader } from "./ReviewHeader";
import { createReviewMemory, type LastLook } from "./review/memory";
import { OverviewReview, type ReviewMode } from "./review/OverviewReview";
import { createReviewViews } from "./review/views";
import type { PageStore } from "./store";

/**
 * The route Prompting Time opened, and nothing until it opens one: Whooper's home at /, which
 * Prompting Time opens from Whooper's name, a review, or a cause that stops every review. Home lists
 * the reviews and causes whooper-status lists. A review shows its header, then its diff when it
 * finished with findings, or the screen for a review that runs, waits, or failed; a cause that stops
 * every review shows the reviews it holds back. Each review stays
 * mounted while its run stays the same, so a new read of the same run updates it in place. A finished
 * review that wrote an overview opens on its walkthrough, and the header switches it to its diff.
 * What the page remembers about the reviews, and the views turned off, last as long as the page.
 */
export function App(props: { store: PageStore }) {
  const store = props.store;
  const memory = createReviewMemory();
  const views = createReviewViews();
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
        {(runId: string) => {
          const [mode, setMode] = createSignal<ReviewMode>("walkthrough");
          // What the user opened of this pull request before, remembered once whooper-status lists the run.
          const [look, setLook] = createSignal<LastLook | null | undefined>(undefined);
          createEffect(
            () => {
              const entry = store.entry();
              return entry ? { run_id: entry.run_id, repo: entry.repo, pr: entry.pr, pr_head_sha: entry.pr_head_sha } : null;
            },
            (entry) => {
              if (entry && untrack(look) === undefined) setLook(memory.rememberLook(entry));
            },
          );
          const withOverview = () => store.state.review?.run_id === runId && store.state.review.overview !== null;
          return (
          <div class="w-page">
            <ReviewHeader store={store} modes={withOverview() ? { mode: mode(), onMode: setMode, views } : undefined} />
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
                    <Match when={withOverview()}>
                      <OverviewReview store={store} memory={memory} views={views} look={look() ?? null} mode={mode()} onMode={setMode} />
                    </Match>
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
          );
        }}
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
