import { createMemo, createSignal, For, Show } from "solid-js";
import { Button, Checkbox } from "@prompting-time/ui";
import type { PageStore } from "./store";
import { CommentText, kindLabel } from "./text";

/**
 * One finding as a draft comment: Include in review, Edit, Ask about this, and Discard. Each change
 * goes to the host as a request, and the drafts the host answers with are what it shows. The editor
 * and question box keep their text for as long as the thread lives, which the diff keeps across its
 * rebuilds.
 */
export function DraftThread(props: { store: PageStore; findingId: string }) {
  const store = props.store;
  const finding = createMemo(() => store.state.review?.findings.findings.find((f) => f.id === props.findingId));
  const choice = () => store.state.review?.drafts.findings[props.findingId];
  const locked = () => store.locked();
  // While the host has not answered, Include shows the user's click.
  const [pendingInclude, setPendingInclude] = createSignal<boolean | null>(null);
  const included = () => pendingInclude() ?? choice()?.included ?? false;
  const text = () => choice()?.text ?? finding()?.draft ?? "";
  const [editing, setEditing] = createSignal(false);
  const [edited, setEdited] = createSignal("");
  const [asking, setAsking] = createSignal(false);
  const [question, setQuestion] = createSignal("");
  const [busy, setBusy] = createSignal(false);
  // Ask runs apart from the other changes, which stay usable while Whooper's agent answers.
  const [askBusy, setAskBusy] = createSignal(false);
  const [error, setError] = createSignal("");

  async function run(work: () => Promise<unknown>, setRunning = setBusy) {
    setRunning(true);
    try {
      await work();
      setError("");
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setRunning(false);
    }
  }

  async function include(checked: boolean) {
    setPendingInclude(checked);
    await run(() => store.changeDraft({ op: checked ? "include" : "leave-out", finding: props.findingId }));
    setPendingInclude(null);
  }

  async function save() {
    if (await run(() => store.changeDraft({ op: "text", finding: props.findingId, text: edited() }))) setEditing(false);
  }

  async function ask() {
    if (await run(() => store.ask(question(), props.findingId), setAskBusy)) {
      setQuestion("");
      setAsking(false);
    }
  }

  const place = () => `${finding()?.path.split("/").pop()}:${finding()?.line}`;

  return (
    <Show when={finding()}>
      {(f) => (
        <Show
          when={!store.state.discarded[props.findingId]}
          fallback={
            <div class="w-thread w-thread-folded">
              <span>
                Discarded · {f().title} · {place()}
              </span>
              <Button onClick={() => store.unfold(props.findingId)}>Show</Button>
            </div>
          }
        >
          <section class="w-thread" aria-label={`Draft comment on ${place()}`} data-included={included() ? "yes" : "no"}>
            <p class="w-thread-meta">
              Whooper · {kindLabel(f().kind)} · {place()} · {included() ? "in the review" : "left out"}
            </p>
            <Show
              when={editing()}
              fallback={
                <p class="w-thread-body">
                  <CommentText text={text()} />
                </p>
              }
            >
              <textarea class="w-textarea" aria-label="Comment" rows={4} value={edited()} onInput={(e) => setEdited(e.currentTarget.value)} />
            </Show>
            <div class="w-thread-actions">
              <Show
                when={editing()}
                fallback={
                  <>
                    <Checkbox
                      label="Include in review"
                      checked={included()}
                      disabled={busy() || locked()}
                      onChange={(e) => include(e.currentTarget.checked)}
                    />
                    <Button
                      disabled={busy() || locked()}
                      onClick={() => {
                        setEdited(text());
                        setEditing(true);
                      }}
                    >
                      Edit
                    </Button>
                    <Button disabled={busy()} onClick={() => setAsking(true)}>
                      Ask about this
                    </Button>
                    <Button disabled={busy() || locked()} onClick={() => run(() => store.discard(props.findingId))}>
                      Discard
                    </Button>
                  </>
                }
              >
                <Button variant="primary" disabled={busy()} onClick={save}>
                  {busy() ? "Saving…" : "Save"}
                </Button>
                <Button disabled={busy()} onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              </Show>
            </div>
            <Show when={asking()}>
              <div class="w-ask">
                <textarea
                  class="w-textarea"
                  aria-label="Question about this finding"
                  placeholder="Ask Whooper about this finding"
                  rows={2}
                  value={question()}
                  onInput={(e) => setQuestion(e.currentTarget.value)}
                />
                <div class="w-thread-actions">
                  <Button variant="primary" disabled={askBusy() || store.held() || store.asking() || !question().trim()} onClick={ask}>
                    {askBusy() ? "Asking…" : "Ask"}
                  </Button>
                  <Button disabled={askBusy()} onClick={() => setAsking(false)}>
                    Cancel
                  </Button>
                  <span class="w-hint">Whooper's agent answers from the review's record. Nothing posts.</span>
                </div>
              </div>
            </Show>
            <For each={store.state.answers[props.findingId] ?? []}>
              {(entry) => (
                <div class="w-answer">
                  <p class="w-answer-question">{entry.question}</p>
                  <p class="w-answer-text">{entry.answer}</p>
                </div>
              )}
            </For>
            <Show when={error()}>
              <p class="w-error" role="alert">
                {error()}
              </p>
            </Show>
          </section>
        </Show>
      )}
    </Show>
  );
}
