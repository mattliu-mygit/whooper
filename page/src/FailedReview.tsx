import { createSignal, For, Show, untrack } from "solid-js";
import { Button, Markdown, StatusDot, Textarea } from "@prompting-time/ui";
import type { Shown } from "./protocol";
import type { PageStore } from "./store";
import { shortDate, shortSha, waitingText } from "./text";

/** The PR as a heading names it: its title, or <owner>/<repo>#<number> without one. */
function prTitle(shown: Shown): string {
  return shown.title ?? (shown.run ? `${shown.run.repo}#${shown.run.pr}` : shown.run_id);
}

/**
 * A review that failed or waits, which has no findings to draw, as Prompting Time drew it itself
 * before page protocol 2: the PR, the cause whooper-show names, why the review failed as
 * whooper-review recorded it, and the answers to earlier questions. Ask why runs whooper-ask with the
 * user's question and adds the answer; the header retries. A review that waits on a cause that stops
 * every review links to that cause's screen.
 */
export function FailedReview(props: { store: PageStore; shown: Shown }) {
  const store = props.store;
  const waiting = () => props.shown.outcome === "waiting";
  const [asking, setAsking] = createSignal(false);
  // The first question suits where the review stood when the screen opened.
  const [question, setQuestion] = createSignal(untrack(waiting) ? "Why is this review waiting?" : "Why did this review fail?");
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal<string | null>(null);
  const answers = () => [...props.shown.asks, ...(store.state.answers[""] ?? [])];
  const blocking = () => (props.shown.cause?.scope === "all" ? props.shown.cause : null);
  // The PRs whose newest review waits or failed at the same cause, which the cause's screen lists.
  const sameCause = () => {
    const cause = blocking();
    return cause ? newestAtCause(store, cause.id).length : 0;
  };

  async function ask(event: SubmitEvent) {
    event.preventDefault();
    const text = question().trim();
    if (!text || busy() || store.held() || store.asking()) return;
    setBusy(true);
    setError(null);
    try {
      await store.ask(text, null);
      setQuestion("");
      setAsking(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section class="w-column" aria-labelledby="failed-review-title">
      <header>
        <h1 id="failed-review-title">{prTitle(props.shown)}</h1>
        <Show when={props.shown.run}>
          {(run) => (
            <p class="w-meta">
              {run().repo}#{run().pr}
              {props.shown.author ? ` · ${props.shown.author}` : ""}
            </p>
          )}
        </Show>
      </header>
      {/* Tinted red like the verdict banner a finished review opens on: failed, or blocked by a cause. */}
      <section class="w-card w-failed verdict-banner" data-tone="error" aria-labelledby="failed-review-heading">
        <h2 id="failed-review-heading">
          <StatusDot status="error" />
          {waiting() ? `Waiting · ${props.shown.cause?.summary ?? "blocked"}` : "Review failed"}
        </h2>
        <Show when={!waiting() && props.shown.cause}>{(cause) => <p>{cause().summary}</p>}</Show>
        <Show when={waiting()}>
          <p>Whooper stopped its round when this cause stopped every review, so this review waits for the next round.</p>
        </Show>
        <Show
          when={props.shown.failure?.trim()}
          fallback={
            <Show when={!waiting() && !props.shown.cause}>
              <p>Whooper didn't record why this review failed.</p>
            </Show>
          }
        >
          {(failure) => (
            <pre class="w-log" tabindex="0" aria-label="Why it failed">
              {failure()}
            </pre>
          )}
        </Show>
        <div class="w-thread-actions">
          <Button disabled={asking()} onClick={() => setAsking(true)}>
            Ask why
          </Button>
          <Show when={blocking()}>
            {(cause) => <Button onClick={() => store.open(`/causes/${cause().id}`)}>See all {waitingText(sameCause())}</Button>}
          </Show>
        </div>
        <Show when={asking()}>
          <form class="w-ask" onSubmit={(event) => void ask(event)}>
            <Textarea label="Question" rows={2} value={question()} disabled={busy()} onInput={(e) => setQuestion(e.currentTarget.value)} />
            <div class="w-thread-actions">
              <Button type="submit" variant="primary" disabled={busy() || store.held() || store.asking() || !question().trim()}>
                {busy() ? "Asking…" : "Ask"}
              </Button>
              <Button disabled={busy()} onClick={() => setAsking(false)}>
                Cancel
              </Button>
              <span class="w-hint">Whooper's agent answers from the review's record. Nothing posts.</span>
            </div>
          </form>
        </Show>
        <Show when={error()}>
          {(text) => (
            <p class="w-error" role="alert">
              {text()}
            </p>
          )}
        </Show>
      </section>
      <Show when={answers().length > 0}>
        <section class="w-card" aria-labelledby="failed-review-answers">
          <h2 id="failed-review-answers">Answers</h2>
          <ol class="w-answers" aria-labelledby="failed-review-answers">
            <For each={answers()}>
              {(entry) => (
                <li class="w-answer">
                  <p class="w-answer-question">{entry.question}</p>
                  <Markdown content={entry.answer} />
                </li>
              )}
            </For>
          </ol>
        </section>
      </Show>
    </section>
  );
}

/** The review that runs: its findings open here once it finishes. */
export function RunningReview(props: { shown: Shown }) {
  return (
    <section class="w-column" aria-labelledby="running-review-heading">
      <h1>{prTitle(props.shown)}</h1>
      <section class="w-card w-failed">
        <h2 id="running-review-heading">
          <StatusDot status="running" />
          Reviewing…
        </h2>
        <p>
          Whooper is reviewing {props.shown.run ? `${props.shown.run.repo}#${props.shown.run.pr}` : "this pull request"}
          {props.shown.head_sha ? ` at ${shortSha(props.shown.head_sha)}` : ""}. Its findings open here when it finishes.
        </p>
      </section>
    </section>
  );
}

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
 * A cause that stops every review, which Prompting Time lists as a blocker of whooper-status's status
 * format 2 and opens at /causes/<id>: what it is, since when, the reviews it holds back, each of which
 * opens here, and Poll now, which runs a round that reviews them again.
 */
export function CauseScreen(props: { store: PageStore; cause: string }) {
  const store = props.store;
  const [busy, setBusy] = createSignal(false);
  const [note, setNote] = createSignal<string | null>(null);
  const [error, setError] = createSignal<string | null>(null);
  const reviews = () => newestAtCause(store, props.cause);
  const blocked = () => (store.state.status?.blocked?.id === props.cause ? store.state.status.blocked : null);
  const summary = () => blocked()?.summary ?? reviews()[0]?.cause?.summary ?? props.cause;

  async function poll() {
    setBusy(true);
    setError(null);
    try {
      setNote((await store.round()) || "Whooper ran a round.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section class="w-column" aria-labelledby="cause-title">
      <header>
        <h1 id="cause-title">
          <StatusDot status="error" />
          {summary()}
        </h1>
        <p class="w-meta">
          Stops every review{blocked()?.since ? ` · since ${shortDate(blocked()!.since)}` : ""} · {waitingText(reviews().length)}
        </p>
      </header>
      <div class="w-thread-actions">
        <Button variant="primary" disabled={busy() || store.held()} onClick={poll}>
          {busy() ? "Polling…" : "Poll now"}
        </Button>
        <span class="w-hint">A round reviews the waiting pull requests again once the cause is fixed.</span>
      </div>
      <Show when={note()}>{(text) => <p role="status">{text()}</p>}</Show>
      <Show when={error()}>
        {(text) => (
          <p class="w-error" role="alert">
            {text()}
          </p>
        )}
      </Show>
      <ul class="w-cause-reviews" aria-label="Reviews it holds back">
        <For each={reviews()}>
          {(review) => (
            <li>
              <button type="button" class="w-cause-review" onClick={() => store.open(`/reviews/${review.run_id}`)}>
                <span>{review.title ?? `${review.repo}#${review.pr}`}</span>
                <span class="w-hint">
                  {review.repo}#{review.pr}
                </span>
              </button>
            </li>
          )}
        </For>
      </ul>
    </section>
  );
}
