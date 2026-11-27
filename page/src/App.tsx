import { createEffect, createSignal, Match, Show, Switch, untrack } from "solid-js";
import { CausesTab } from "./Causes";
import { FailedReview, RunningReview } from "./FailedReview";
import { ReviewScreen } from "./Review";
import { ReviewHeader } from "./ReviewHeader";
import { createListFilters, PastTab, ReviewsTab } from "./Reviews";
import { createReviewMemory, type LastLook } from "./review/memory";
import { OverviewReview, type ReviewMode } from "./review/OverviewReview";
import { createReviewViews } from "./review/views";
import { Sidebar, type Tab } from "./Sidebar";
import type { PageStore } from "./store";

/**
 * The route Prompting Time opened, and nothing until it opens one, beside the page's own sidebar of
 * tabs: the Reviews tab at /, which Prompting Time opens from Whooper's name, Past reviews at /past,
 * and Causes at /causes and at a cause's route. A review shows Back to the list, its header, then its
 * diff when it finished with findings, or the screen for a review that runs, waits, or failed, with the
 * tab whose list holds it marked. The lists keep their search and filter while a review shows. Each review stays
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
  const filters = createListFilters();
  /** The list a review belongs to: Reviews while whooper-status lists it as an item, else Past reviews. */
  const listOf = (runId: string): Tab => {
    const status = store.state.status;
    return !status || status.items.some((item) => item.id === runId) ? "reviews" : "past";
  };
  const tab = (): Tab => {
    const place = store.place();
    if (place?.kind === "past") return "past";
    if (place?.kind === "causes" || place?.kind === "cause") return "causes";
    if (place?.kind === "review") return listOf(place.runId);
    return "reviews";
  };
  return (
    <Show when={store.place()}>
      <div class="w-app">
        <Sidebar store={store} tab={tab()} />
        <main class="w-main">
          <Switch>
            <Match when={store.place()?.kind === "home"}>
              <ReviewsTab store={store} filters={filters} />
            </Match>
            <Match when={store.place()?.kind === "past"}>
              <PastTab store={store} filters={filters} />
            </Match>
            <Match when={store.place()?.kind === "causes"}>
              <CausesTab store={store} cause={null} />
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
                  <ReviewHeader
                    store={store}
                    back={() => store.open(listOf(runId) === "past" ? "/past" : "/")}
                    modes={withOverview() ? { mode: mode(), onMode: setMode, views } : undefined}
                  />
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
              {(id: string) => <CausesTab store={store} cause={id} />}
            </Match>
          </Switch>
        </main>
      </div>
    </Show>
  );
}
