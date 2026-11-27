import { For, Show } from "solid-js";
import { StatusDot } from "@prompting-time/ui";
import type { PageStore } from "./store";
import { TabHeader } from "./TabHeader";
import { shortDate, waitingText } from "./text";

/** The newest review of each PR that waits or failed at cause, which stops every review, newest first,
 * leaving out one the user removed: the reviews the cause holds back. */
export function newestAtCause(store: PageStore, cause: string) {
  const seen = new Set<string>();
  return (store.state.status?.reviews ?? []).filter((review) => {
    const pr = `${review.repo}#${review.pr}`;
    if (seen.has(pr)) return false;
    seen.add(pr);
    return (
      !review.removed_by_user &&
      review.submitted === null &&
      !review.skipped_by_user &&
      (review.outcome === "waiting" || review.outcome === "failed") &&
      review.cause?.id === cause
    );
  });
}

/**
 * The Causes tab, at /causes and at a cause's own route, /causes/<id>, which Prompting Time opens from
 * a blocker of whooper-status's status format 2: each cause that stops every review, the one the route
 * names first, with since when and the reviews it holds back, each of which opens here. The header's
 * Poll now runs a round, which reviews them again.
 */
export function CausesTab(props: { store: PageStore; cause: string | null }) {
  const store = props.store;
  const causes = () => {
    const ids = (store.state.status?.blockers ?? []).map((blocker) => blocker.id);
    return props.cause && !ids.includes(props.cause) ? [props.cause, ...ids] : ids;
  };
  return (
    <>
      <TabHeader store={store} title="Causes" words="What stops every review" />
      <div class="w-tab-body">
        <Show when={store.state.status}>
          <Show when={causes().length > 0} fallback={<p class="w-empty">Nothing stops reviews now.</p>}>
            <p class="w-tab-hint">A round reviews the waiting pull requests again once the cause is fixed.</p>
            <For each={causes()}>{(id) => <Cause store={store} cause={id} />}</For>
          </Show>
        </Show>
      </div>
    </>
  );
}

/** One cause that stops every review: what it is, since when, and the reviews it holds back. */
function Cause(props: { store: PageStore; cause: string }) {
  const store = props.store;
  const reviews = () => newestAtCause(store, props.cause);
  const blocked = () => (store.state.status?.blocked?.id === props.cause ? store.state.status.blocked : null);
  const blocker = () => store.state.status?.blockers.find((b) => b.id === props.cause);
  const summary = () => blocked()?.summary ?? blocker()?.title ?? reviews()[0]?.cause?.summary ?? props.cause;
  const headingId = () => `cause-${props.cause}`;
  return (
    <section class="w-cause" aria-labelledby={headingId()}>
      <h2 id={headingId()}>
        <StatusDot status="error" />
        {summary()}
      </h2>
      <p class="w-row-detail">
        Stops every review{blocked()?.since ? ` · since ${shortDate(blocked()!.since)}` : ""} · {waitingText(reviews().length)}
      </p>
      <Show when={reviews().length > 0}>
        <ul class="w-cause-reviews" aria-label="Reviews it holds back">
          <For each={reviews()}>
            {(review) => (
              <li>
                <button type="button" class="w-cause-review" onClick={() => store.open(`/reviews/${review.run_id}`)}>
                  <span>{review.title ?? `${review.repo}#${review.pr}`}</span>
                  <Show when={review.title}>
                    <span class="w-row-detail">
                      {review.repo}#{review.pr}
                    </span>
                  </Show>
                </button>
              </li>
            )}
          </For>
        </ul>
      </Show>
    </section>
  );
}
