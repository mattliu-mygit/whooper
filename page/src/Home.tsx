import { For, Show } from "solid-js";
import { ListRow, PullRequestIcon, StatusDot } from "@prompting-time/ui";
import manifest from "../../prompting-time-plugin.json";
import type { StatusItem } from "./protocol";
import type { PageStore } from "./store";

/** The groups prompting-time-plugin.json declares for status format 2, the ones that wait on the user
 * first, then by their order. */
const GROUPS = [...manifest.status.groups].sort((a, b) => Number(b.needs_you) - Number(a.needs_you) || a.order - b.order);

/** The group whose section starts closed, since it holds the reviews nothing waits on any more. */
const FOLDED_GROUP = "done";

/**
 * Whooper's home, which Prompting Time opens at / when the user clicks Whooper's name: Whooper's agent
 * row, such as "Paused" or "Running · next poll 3:45 PM", each cause that stops every review, and the
 * reviews in the groups whooper-status's status format 2 puts them in, with a folded group, Done,
 * closed until the user opens it. Each row opens its review's route, and each cause its own.
 */
export function Home(props: { store: PageStore }) {
  const store = props.store;
  const status = () => store.state.status;
  const itemsIn = (group: string): StatusItem[] => status()?.items.filter((item) => item.group === group) ?? [];
  return (
    <Show
      when={status()}
      fallback={
        <Show when={store.state.loadError} fallback={<p class="w-hint w-column" role="status">Reading Whooper's status…</p>}>
          {(error) => (
            <p class="w-error w-column" role="alert">
              {error()}
            </p>
          )}
        </Show>
      }
    >
      {(current) => (
        <section class="w-column" aria-labelledby="home-title">
          <header>
            <h1 id="home-title">Whooper</h1>
            <p class="w-home-agent">
              <StatusDot status={current().agent.dot} />
              {current().agent.words}
            </p>
          </header>
          <Show when={current().blockers.length > 0}>
            <ul class="w-home-list" aria-label="Stops every review">
              <For each={current().blockers}>
                {(blocker) => (
                  <li>
                    <button type="button" class="w-cause-review" onClick={() => store.open(blocker.route)}>
                      <span class="w-home-blocker">
                        <StatusDot status={blocker.dot} />
                        {blocker.title}
                      </span>
                      <span class="w-hint">Stops every review</span>
                    </button>
                  </li>
                )}
              </For>
            </ul>
          </Show>
          <For each={GROUPS.filter((group) => itemsIn(group.id).length > 0)}>
            {(group) => {
              const rows = () => (
                <ul class="w-home-list" aria-label={group.title}>
                  <For each={itemsIn(group.id)}>
                    {(item) => (
                      <li>
                        <ListRow
                          icon={<PullRequestIcon />}
                          title={item.title}
                          meta={item.detail ?? undefined}
                          status={item.dot}
                          onClick={() => store.open(item.route)}
                        />
                      </li>
                    )}
                  </For>
                </ul>
              );
              const heading = () => `${group.title} · ${itemsIn(group.id).length}`;
              return group.id === FOLDED_GROUP ? (
                <details class="w-home-group">
                  <summary>{heading()}</summary>
                  {rows()}
                </details>
              ) : (
                <section class="w-home-group" aria-label={group.title}>
                  <h2>{heading()}</h2>
                  {rows()}
                </section>
              );
            }}
          </For>
          <Show when={current().items.length === 0}>
            <p class="w-hint">No reviews in the last 30 days.</p>
          </Show>
          <Show when={current().truncated}>
            <p class="w-hint">Whooper lists its newest 50 reviews.</p>
          </Show>
        </section>
      )}
    </Show>
  );
}
