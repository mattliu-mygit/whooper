import { createUniqueId, Show } from "solid-js";
import { Button, Textarea } from "@prompting-time/ui";
import { createDecision, SubmitRefusal } from "../decision";
import type { PageStore } from "../store";
import { plural } from "../text";

/**
 * The review's decision in the walkthrough: Comment or Approve, which whooper-draft saves at once, the
 * review's message, which it saves when the box loses focus, and Submit review, which Prompting Time
 * confirms first, as the diff's submit bar does; a refusal shows why, and one that a flag lets through
 * offers that flag's button. It waits as one Decide… button in the corner until you click it or reach
 * the end of the walkthrough.
 */
export function DecideBar(props: { store: PageStore; open: boolean; onOpen(): void; onEditDrafts(): void }) {
  const store = props.store;
  const decision = createDecision(store);
  const { busy, error, event } = decision;
  const total = () => store.state.review!.findings.findings.length;
  const name = () => `decide-${store.state.review!.run_id}`;
  const nothingId = createUniqueId();
  let bar: HTMLDivElement | undefined;
  return (
    <Show
      when={props.open}
      fallback={
        <div class="decide-corner">
          <Button
            variant="primary"
            onClick={() => {
              props.onOpen();
              // The choice takes the focus once the bar shows, so the keyboard carries on there.
              requestAnimationFrame(() => bar?.querySelector<HTMLInputElement>("input:checked")?.focus());
            }}
          >
            Decide…
          </Button>
        </div>
      }
    >
      <div class="decide-bar" role="group" aria-label="Your decision" ref={(element: HTMLDivElement) => (bar = element)}>
        <span class="decide-count">
          {plural(total(), "draft comment")}, {decision.comments()} included
        </span>
        <label class="decide-choice">
          <input type="radio" name={name()} checked={event() === "COMMENT"} disabled={busy()} onChange={() => decision.choose("COMMENT")} /> Comment
        </label>
        <label class="decide-choice">
          <input type="radio" name={name()} checked={event() === "APPROVE"} disabled={busy()} onChange={() => decision.choose("APPROVE")} /> Approve
        </label>
        <button type="button" class="decide-edit" onClick={() => props.onEditDrafts()}>
          Edit drafts in Diff
        </button>
        <Textarea
          label="Review message"
          rows={2}
          placeholder={decision.placeholder()}
          value={decision.message()}
          onInput={(e) => decision.typeMessage(e.currentTarget.value)}
          onChange={() => decision.saveMessage()}
        />
        <span class="decide-spacer" />
        <Show when={error()}>
          {(text) => (
            <span role="alert" class="w-error">
              {text()}
            </span>
          )}
        </Show>
        <SubmitRefusal store={store} decision={decision} />
        <Show when={decision.nothing()}>
          <span id={nothingId} class="w-hint">
            Add a comment or a message to submit a comment review
          </span>
        </Show>
        <Button
          variant="primary"
          disabled={busy() || store.held() || decision.nothing()}
          aria-describedby={decision.nothing() ? nothingId : undefined}
          onClick={() => decision.submit()}
        >
          {busy() ? "Submitting…" : "Submit review"}
        </Button>
      </div>
    </Show>
  );
}
