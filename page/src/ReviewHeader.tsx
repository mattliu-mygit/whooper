import { createSignal, For, Show } from "solid-js";
import { Button } from "@prompting-time/ui";
import type { StatusReview } from "./protocol";
import type { PageStore } from "./store";
import { shortDate, shortSha } from "./text";

/** "Review 2 · dc0c0bf · 3:12 PM": the version, the commit it reviewed, and when it ended or started. */
export function versionLabel(review: StatusReview, number: number): string {
  return [`Review ${number}`, shortSha(review.head_sha), shortDate(review.ended_at ?? review.started_at)].filter(Boolean).join(" · ");
}

/**
 * The open review's header, which Prompting Time drew itself before page protocol 2: its version and
 * the commit it reviewed, the PR's other versions (an earlier one only shows, since only the newest
 * can be submitted), Skip with Undo, Remove, Rerun review (Retry review after a failure), and
 * banners for a removed review, an earlier version, and new commits since the review. Each button
 * runs the manifest's action for the run, and a refusal shows under the bar. It reads the review as
 * whooper-status lists it, so it shows nothing until the status arrives or for a review older than
 * the 30 days whooper-status lists.
 */
export function ReviewHeader(props: { store: PageStore }) {
  const store = props.store;
  const [busy, setBusy] = createSignal(false);
  const [queued, setQueued] = createSignal<string | null>(null);
  const [error, setError] = createSignal<string | null>(null);

  async function run(task: () => Promise<string>, done?: (printed: string) => void) {
    setBusy(true);
    setError(null);
    try {
      const printed = await task();
      done?.(printed);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
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
        const canSkip = () => !earlier() && !running() && !removed() && review().submitted === null;
        const canRemove = () => !earlier() && !running() && !removed();
        const newCommits = () =>
          !earlier() && !running() && review().head_sha !== null && review().pr_head_sha !== null && review().head_sha !== review().pr_head_sha;
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
                <Show when={review().skipped_by_user} fallback={<Button disabled={busy()} onClick={() => run(() => store.skip())}>Skip</Button>}>
                  <span class="w-hint">Skipped</span>
                  <Button disabled={busy()} onClick={() => run(() => store.skip(true))}>
                    Undo
                  </Button>
                </Show>
              </Show>
              <Show when={canRemove()}>
                <Button disabled={busy()} onClick={() => run(() => store.remove())}>
                  Remove
                </Button>
              </Show>
              <Show when={canRerun()}>
                <Button variant={problem() || newCommits() ? "primary" : "plain"} disabled={busy()} onClick={rerun}>
                  {problem() ? "Retry review" : "Rerun review"}
                </Button>
              </Show>
            </div>
            <Show when={removed()}>
              <div class="w-banner" role="note">
                <span>
                  <strong>Removed from your lists</strong> · its files stay on disk.
                </span>
                <Button disabled={busy()} onClick={() => run(() => store.remove(true))}>
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
            <Show when={newCommits()}>
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
                  <Button disabled={busy()} onClick={() => run(() => store.round(), () => setQueued(null))}>
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
