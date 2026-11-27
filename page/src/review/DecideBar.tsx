import { Show } from "solid-js";
import { Button } from "@prompting-time/ui";
import { createDecision } from "../decision";
import type { PageStore } from "../store";
import { plural } from "../text";

/**
 * The review's decision in the walkthrough: Comment or Approve, which whooper-draft saves at once, and
 * Submit review, which Prompting Time confirms first, as the diff's submit bar does; a refusal that a
 * flag lets through offers that flag's button. It waits as one Decide… button in the corner until
 * you click it or reach the end of the walkthrough.
 */
export function DecideBar(props: { store: PageStore; open: boolean; onOpen(): void; onEditDrafts(): void }) {
  const store = props.store;
  const decision = createDecision(store);
  const { busy, error, event } = decision;
  const total = () => store.state.review!.findings.findings.length;
  const name = () => `decide-${store.state.review!.run_id}`;
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
        <span class="decide-spacer" />
        <Show when={error()}>
          {(text) => (
            <span role="alert" class="w-error">
              {text()}
            </span>
          )}
        </Show>
        <Show when={decision.override()}>
          {(next) => (
            <>
              <span class="w-hint w-refusal">{next().why}</span>
              <Button disabled={busy() || store.held()} onClick={() => decision.submit(next().flag)}>
                {next().label}
              </Button>
            </>
          )}
        </Show>
        <Button variant="primary" disabled={busy() || store.held() || decision.nothing()} onClick={() => decision.submit()}>
          {busy() ? "Submitting…" : "Submit review"}
        </Button>
      </div>
    </Show>
  );
}
