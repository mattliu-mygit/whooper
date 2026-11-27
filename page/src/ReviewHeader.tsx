import { createSignal, For, Show } from "solid-js";
import { Button, Menu, type MenuItem } from "@prompting-time/ui";
import type { Review, StatusReview } from "./protocol";
import type { ReviewMode } from "./review/OverviewReview";
import { REVIEW_VIEWS, viewHasData, viewListed, type ReviewViews } from "./review/views";
import type { PageStore } from "./store";
import { shortDate, shortSha } from "./text";

/** "Review 2 · dc0c0bf · 3:12 PM": the version, the commit it reviewed, and when it ended or started. */
export function versionLabel(review: StatusReview, number: number): string {
  return [`Review ${number}`, shortSha(review.head_sha), shortDate(review.ended_at ?? review.started_at)].filter(Boolean).join(" · ");
}

/**
 * The open review's header, which Prompting Time drew itself before page protocol 2: its version and
 * the commit it reviewed, the PR's other versions (an earlier one only shows, since only the newest
 * can be submitted), Skip with Undo, which also stops a review that runs, Remove, Rerun review (Retry
 * review after a failure), and banners for a removed review, an earlier version, and new commits since
 * the review. Each button runs the manifest's action for the run, and a refusal shows under the bar.
 * The store disables the buttons while one runs and briefly after (PageStore.held).
 * It reads the review as whooper-status lists it, so it shows nothing until the status arrives or for
 * a review older than the 30 days whooper-status lists. For a review with an overview, `modes` adds
 * Walkthrough | Diff and the Views menu, and the since notice under the verdict says what the
 * new-commits banner would.
 */
export function ReviewHeader(props: {
  store: PageStore;
  modes?: { mode: ReviewMode; onMode(mode: ReviewMode): void; views: ReviewViews };
}) {
  const store = props.store;
  const [queued, setQueued] = createSignal<string | null>(null);
  const [error, setError] = createSignal<string | null>(null);

  async function run(task: () => Promise<string>, done?: (printed: string) => void) {
    setError(null);
    try {
      const printed = await task();
      done?.(printed);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <Show when={store.entry()}>
      {(review) => {
        const versions = () => store.versions();
        const number = () => versions().findIndex((r) => r.run_id === review().run_id) + 1;
        const newest = () => versions()[versions().length - 1] ?? review();
        const earlier = () => store.earlier();
        const running = () => review().outcome === "running";
        const problem = () => review().outcome === "failed" || review().outcome === "waiting";
        const removed = () => !earlier() && review().removed_by_user;
        // whooper-retry reads the PR from run.json, which a review stopped before it wrote one lacks.
        const canRerun = () => !earlier() && !running() && review().repo !== null;
        // whooper-skip stops a review that runs, and Whooper starts no new review of the PR until Undo.
        const canSkip = () => !earlier() && !removed() && review().submitted === null;
        const canRemove = () => !earlier() && !running() && !removed();
        const newCommits = () =>
          !earlier() && !running() && review().head_sha !== null && review().pr_head_sha !== null && review().head_sha !== review().pr_head_sha;
        /** The Views menu: each view this review lists, on or off, and what it shows. */
        const viewItems = (views: ReviewViews): MenuItem[] => {
          const shown = store.state.review as Review;
          return REVIEW_VIEWS.filter((view) => viewListed(view, shown)).map((view) => ({
            value: view.id,
            label: view.name,
            description: viewHasData(view.id, shown) ? view.description : `${view.description} Nothing for this review.`,
            type: "checkbox" as const,
            checked: views.isOn(view.id),
          }));
        };
        const rerun = () => run(() => store.retry(), (printed) => setQueued(printed));
        return (
          <header class="w-review-header">
            <div class="w-review-header-bar">
              <Show when={versions().length > 1} fallback={<span class="w-hint">{versionLabel(review(), Math.max(number(), 1))}</span>}>
                <select
                  class="w-versions"
                  aria-label="Versions of this review"
                  value={review().run_id}
                  onChange={(e) => store.open(`/reviews/${e.currentTarget.value}`)}
                >
                  <For each={[...versions()].reverse()}>
                    {(version) => (
                      <option value={version.run_id} selected={version.run_id === review().run_id}>
                        {versionLabel(version, versions().indexOf(version) + 1)}
                      </option>
                    )}
                  </For>
                </select>
              </Show>
              <span class="w-spacer" />
              <Show when={running() && !earlier()}>
                <span class="w-hint">Reviewing… started {shortDate(review().started_at)}</span>
              </Show>
              <Show when={canSkip()}>
                <Show when={review().skipped_by_user} fallback={<Button disabled={store.held()} onClick={() => run(() => store.skip())}>Skip</Button>}>
                  <span class="w-hint">Skipped</span>
                  <Button disabled={store.held()} onClick={() => run(() => store.skip(true))}>
                    Undo
                  </Button>
                </Show>
              </Show>
              <Show when={canRemove()}>
                <Button disabled={store.held()} onClick={() => run(() => store.remove())}>
                  Remove
                </Button>
              </Show>
              <Show when={canRerun()}>
                <Button variant={problem() || newCommits() ? "primary" : "plain"} disabled={store.held()} onClick={rerun}>
                  {problem() ? "Retry review" : "Rerun review"}
                </Button>
              </Show>
              <Show when={props.modes}>
                {(modes) => {
                  const off = () => viewItems(modes().views).filter((item) => !item.checked).length;
                  return (
                    <>
                      <div class="w-segmented review-modes" role="group" aria-label="Show the review as">
                        <Button aria-pressed={modes().mode === "diff" ? "false" : "true"} onClick={() => modes().onMode("walkthrough")}>
                          Walkthrough
                        </Button>
                        <Button aria-pressed={modes().mode === "diff" ? "true" : "false"} onClick={() => modes().onMode("diff")}>
                          Diff
                        </Button>
                      </div>
                      <Menu
                        label="Views in reviews"
                        text={off() > 0 ? `Views · ${off()} off` : "Views"}
                        items={viewItems(modes().views)}
                        closeOnSelect={false}
                        onSelect={(id) => {
                          const view = REVIEW_VIEWS.find((item) => item.id === id);
                          if (view) modes().views.setOn(view.id, !modes().views.isOn(view.id));
                        }}
                      />
                    </>
                  );
                }}
              </Show>
            </div>
            <Show when={removed()}>
              <div class="w-banner" role="note">
                <span>
                  <strong>Removed from your lists</strong> · its files stay on disk.
                </span>
                <Button disabled={store.held()} onClick={() => run(() => store.remove(true))}>
                  Restore
                </Button>
              </div>
            </Show>
            <Show when={earlier()}>
              <div class="w-banner" role="note">
                <span>
                  Review {number()} of {versions().length}, read-only: only the newest review can be submitted.
                </span>
                <Button onClick={() => store.open(`/reviews/${newest().run_id}`)}>Open review {versions().length}</Button>
              </div>
            </Show>
            <Show when={newCommits() && !props.modes}>
              <div class="w-banner" role="note">
                <span>
                  <strong>New commits since this review</strong> · it reviewed {shortSha(review().head_sha)}; the pull request is at{" "}
                  {shortSha(review().pr_head_sha)}.
                </span>
              </div>
            </Show>
            <Show when={queued()}>
              {(text) => (
                <div class="w-banner" role="status">
                  <span>{text()}</span>
                  <Button disabled={store.held()} onClick={() => run(() => store.round(), () => setQueued(null))}>
                    Poll now
                  </Button>
                </div>
              )}
            </Show>
            <Show when={error()}>
              {(text) => (
                <p class="w-error" role="alert">
                  {text()}
                </p>
              )}
            </Show>
          </header>
        );
      }}
    </Show>
  );
}
