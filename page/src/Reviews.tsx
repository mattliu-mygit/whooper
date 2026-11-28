import { createSignal, For, onSettled, Show, untrack } from "solid-js";
import { EllipsisIcon, Menu, SearchIcon, StatusDot, type MenuItem } from "@prompting-time/ui";
import manifest from "../../prompting-time-plugin.json";
import type { Dot, StatusItem, StatusReview } from "./protocol";
import type { PageStore } from "./store";
import { TabHeader } from "./TabHeader";
import { shortDate } from "./text";

/** The groups prompting-time-plugin.json declares for status format 2, the ones that wait on the user
 * first, then by their order. */
const GROUPS = [...manifest.status.groups].sort((a, b) => Number(b.needs_you) - Number(a.needs_you) || a.order - b.order);

/** How many of the items are in a group the manifest marks needs_you: the reviews waiting on the user. */
export function waitingOnYou(items: readonly StatusItem[]): number {
  return items.filter((item) => GROUPS.find((g) => g.id === item.group)?.needs_you).length;
}

/** What the Reviews and Past reviews tabs keep while a review is open, so Back finds the list as it
 * was: the search, the group, and the row j and k selected. */
export function createListFilters() {
  const [query, setQuery] = createSignal("");
  const [group, setGroup] = createSignal<string | null>(null);
  const [selected, setSelected] = createSignal<string | null>(null);
  const [pastQuery, setPastQuery] = createSignal("");
  const [pastSelected, setPastSelected] = createSignal<string | null>(null);
  return { query, setQuery, group, setGroup, selected, setSelected, pastQuery, setPastQuery, pastSelected, setPastSelected };
}
export type ListFilters = ReturnType<typeof createListFilters>;

/** One row of a list: a review, its status as a dot and a word, and the actions its ⋯ menu offers. */
interface Row {
  id: string;
  route: string;
  title: string;
  detail: string;
  dot: Dot;
  word: string;
  /** What the search looks in: the title, the repo, the PR number, and the author. */
  text: string;
  menu: { value: "skip" | "unskip" | "remove" | "restore" | "retry"; label: string }[];
}

/** Whether every word of query is in text, ignoring case; "#133" and "133" both find PR 133. */
export function matches(query: string, text: string): boolean {
  const haystack = text.toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => haystack.includes(word));
}

const searchText = (title: string, detail: string, review: StatusReview | undefined) =>
  [title, detail, review?.repo, review ? `#${review.pr}` : null, review?.author ? `@${review.author}` : null].filter(Boolean).join(" ");

const quoted = (title: string) => `“${title}”`;

/**
 * The Reviews tab at /, which Prompting Time opens from Whooper's name: the newest review of each pull
 * request whooper-status's status format 2 lists, those that wait on the user first, with a search by
 * title, repo, PR number, or author, and one chip per group with its count. Each row opens its review,
 * and its ⋯ menu skips it or undoes the skip, removes it from the list, or retries it. Removed reviews
 * move to Past reviews, which restores them.
 */
export function ReviewsTab(props: { store: PageStore; filters: ListFilters }) {
  const store = props.store;
  const f = props.filters;
  const reviewOf = (id: string) => store.state.status?.reviews.find((review) => review.run_id === id);
  const rowOf = (item: StatusItem): Row => {
    const review = reviewOf(item.id);
    const group = GROUPS.find((g) => g.id === item.group);
    const actions = item.actions ?? [];
    const menu: Row["menu"] = [];
    if (actions.includes("skip")) menu.push({ value: "skip", label: "Skip" });
    else if (review?.skipped_by_user) menu.push({ value: "unskip", label: "Undo skip" });
    if (actions.includes("remove")) menu.push({ value: "remove", label: "Remove from the list" });
    if (actions.includes("retry")) menu.push({ value: "retry", label: "Retry" });
    return {
      id: item.id,
      route: item.route,
      title: item.title,
      detail: item.detail ?? "",
      dot: item.dot,
      word: group?.title ?? item.group,
      text: searchText(item.title, item.detail ?? "", review),
      menu,
    };
  };
  const items = () => store.state.status?.items ?? [];
  const found = () => items().filter((item) => matches(f.query(), rowOf(item).text));
  const countIn = (group: string) => found().filter((item) => item.group === group).length;
  const rows = () =>
    GROUPS.flatMap((group) => (f.group() === null || f.group() === group.id ? found().filter((item) => item.group === group.id) : [])).map(rowOf);
  const words = () => {
    const waiting = waitingOnYou(items());
    const listed = items().length;
    return `${waiting === 0 ? "Nothing waiting on you" : `${waiting} waiting on you`} · ${listed} listed`;
  };
  return (
    <>
      <TabHeader store={store} title="Reviews" words={words()} />
      <div class="w-tab-body">
        <Loaded store={store}>
          <div class="w-toolbar">
            <SearchBox label="Search reviews" value={f.query()} onInput={f.setQuery} />
            <div class="w-chips" role="group" aria-label="Show reviews">
              <button type="button" class="w-chip" aria-pressed={f.group() === null ? "true" : "false"} onClick={() => f.setGroup(null)}>
                All · {found().length}
              </button>
              <For each={GROUPS.filter((group) => countIn(group.id) > 0 || f.group() === group.id)}>
                {(group) => (
                  <button
                    type="button"
                    class="w-chip"
                    aria-pressed={f.group() === group.id ? "true" : "false"}
                    onClick={() => f.setGroup(f.group() === group.id ? null : group.id)}
                  >
                    {group.title} · {countIn(group.id)}
                  </button>
                )}
              </For>
            </div>
          </div>
          <RowList
            store={store}
            label="Reviews"
            rows={rows()}
            selected={f.selected()}
            onSelected={f.setSelected}
            empty={items().length === 0 ? "No reviews in the last 30 days." : "No reviews match."}
          />
          <Show when={store.state.status?.truncated}>
            <p class="w-tab-hint">Whooper lists its newest 50 reviews.</p>
          </Show>
        </Loaded>
      </div>
    </>
  );
}

/**
 * The Past reviews tab at /past: each review of the last 30 days that the Reviews tab doesn't list,
 * newest first: the ones the user removed, which its ⋯ menu restores, and earlier reviews of a pull
 * request that a newer review replaced.
 */
export function PastTab(props: { store: PageStore; filters: ListFilters }) {
  const store = props.store;
  const f = props.filters;
  const rowOf = (review: StatusReview, newer: boolean): Row => {
    const title = review.title ?? `${review.repo ?? ""}#${review.pr}`;
    const author = review.author ? `@${review.author}` : null;
    const detail = [review.repo ? `${review.repo}#${review.pr}` : `#${review.pr}`, author, shortDate(review.ended_at ?? review.started_at)]
      .filter(Boolean)
      .join(" · ");
    return {
      id: review.run_id,
      route: `/reviews/${review.run_id}`,
      title,
      detail,
      dot: "idle",
      word: review.removed_by_user ? "Removed" : newer ? "Earlier review" : "Older",
      text: searchText(title, detail, review),
      menu: review.removed_by_user ? [{ value: "restore", label: "Restore to the list" }] : [],
    };
  };
  const rows = () => {
    const status = store.state.status;
    if (!status) return [];
    const listed = new Set(status.items.map((item) => item.id));
    const seen = new Set<string>();
    return status.reviews.flatMap((review) => {
      const pr = `${review.repo}#${review.pr}`;
      const newer = seen.has(pr);
      seen.add(pr);
      return listed.has(review.run_id) ? [] : [rowOf(review, newer)];
    });
  };
  const found = () => rows().filter((row) => matches(f.pastQuery(), row.text));
  return (
    <>
      <TabHeader store={store} title="Past reviews" words="Removed and earlier reviews of the last 30 days" />
      <div class="w-tab-body">
        <Loaded store={store}>
          <div class="w-toolbar">
            <SearchBox label="Search past reviews" value={f.pastQuery()} onInput={f.setPastQuery} />
          </div>
          <RowList
            store={store}
            label="Past reviews"
            rows={found()}
            selected={f.pastSelected()}
            onSelected={f.setPastSelected}
            empty={rows().length === 0 ? "No past reviews." : "No past reviews match."} />
        </Loaded>
      </div>
    </>
  );
}

/** Its children once whooper-status answered, else that it reads, or why the read failed. */
function Loaded(props: { store: PageStore; children: unknown }) {
  return (
    <Show
      when={props.store.state.status}
      fallback={
        <Show when={props.store.state.loadError} fallback={<p class="w-empty" role="status">Reading Whooper's status…</p>}>
          {(error) => (
            <p class="w-error" role="alert">
              {error()}
            </p>
          )}
        </Show>
      }
    >
      {props.children as never}
    </Show>
  );
}

function SearchBox(props: { label: string; value: string; onInput: (value: string) => void }) {
  return (
    <label class="w-search">
      <SearchIcon />
      <input
        class="pt-input"
        type="search"
        aria-label={props.label}
        placeholder="Title, repo, PR number, or author"
        value={props.value}
        onInput={(e) => props.onInput(e.currentTarget.value)}
      />
    </label>
  );
}

const typing = (event: KeyboardEvent) =>
  event.target instanceof HTMLElement && (event.target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName));

/**
 * The rows, and what the last row action did, with Undo for a skip or a removal. j and k select the
 * next and previous row, as they step through the walkthrough's places, from the first or the last
 * and around, and move the focus to it; Enter opens it, also once the focus left the rows for the
 * page itself, as after Back to the list. Not while typing, with a modifier, or while a menu or dialog
 * is open, where Escape closes it.
 */
function RowList(props: { store: PageStore; label: string; rows: Row[]; selected: string | null; onSelected(id: string): void; empty: string }) {
  const store = props.store;
  let list: HTMLUListElement | undefined;
  const buttonOf = (id: string) => list?.querySelector<HTMLButtonElement>(`[data-row="${CSS.escape(id)}"] .w-row-open`) ?? null;

  onSettled(() => {
    const keys = (event: KeyboardEvent) => {
      if (event.defaultPrevented || typing(event) || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key !== "j" && event.key !== "k" && event.key !== "Enter") return;
      if ([...document.querySelectorAll('[role="dialog"], [role="alertdialog"], [role="menu"]:not([hidden])')].some((element) => element.checkVisibility?.() ?? true)) return;
      const rows = untrack(() => props.rows);
      const now = rows.findIndex((row) => row.id === untrack(() => props.selected));
      if (event.key === "Enter") {
        // A focused button, a selected row's among them, takes Enter itself.
        if (now < 0 || (document.activeElement && document.activeElement !== document.body)) return;
        event.preventDefault();
        store.open(rows[now].route);
        return;
      }
      if (rows.length === 0) return;
      event.preventDefault();
      const by = event.key === "j" ? 1 : -1;
      const next = rows[now < 0 ? (by > 0 ? 0 : rows.length - 1) : (now + by + rows.length) % rows.length];
      props.onSelected(next.id);
      const button = buttonOf(next.id);
      button?.focus();
      button?.scrollIntoView?.({ block: "nearest" });
    };
    document.addEventListener("keydown", keys);
    return () => document.removeEventListener("keydown", keys);
  });
  const [notice, setNotice] = createSignal<{ text: string; undo?: () => Promise<string> } | null>(null);
  const [error, setError] = createSignal<string | null>(null);

  async function run(task: () => Promise<string>, after: (printed: string) => { text: string; undo?: () => Promise<string> } | null) {
    setError(null);
    setNotice(null);
    try {
      setNotice(after(await task()));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function act(row: Row, action: string) {
    const name = quoted(row.title);
    switch (action) {
      case "open":
        return store.open(row.route);
      case "skip":
        return run(() => store.skip(false, row.id), () => ({ text: `Skipped ${name}.`, undo: () => store.skip(true, row.id) }));
      case "unskip":
        return run(() => store.skip(true, row.id), () => ({ text: `${name} is no longer skipped.` }));
      case "remove":
        return run(() => store.remove(false, row.id), () => ({ text: `Removed ${name} from the list.`, undo: () => store.remove(true, row.id) }));
      case "restore":
        return run(() => store.remove(true, row.id), () => ({ text: `Restored ${name} to the list.` }));
      case "retry":
        return run(() => store.retry(row.id), (printed) => ({ text: printed }));
    }
  }

  const menuItems = (row: Row): MenuItem[] => [
    { value: "open", label: "Open" },
    ...row.menu.map((item) => ({ ...item, disabled: store.held() })),
  ];

  return (
    <>
      <Show when={notice()}>
        {(current) => (
          <div class="w-notice" role="status">
            <span>{current().text}</span>
            <Show when={current().undo}>
              {(undo) => (
                <button type="button" class="w-link" disabled={store.held()} onClick={() => run(undo(), () => null)}>
                  Undo
                </button>
              )}
            </Show>
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
      <Show when={props.rows.length > 0} fallback={<p class="w-empty">{props.empty}</p>}>
        <ul class="w-rows" aria-label={props.label} ref={(element: HTMLUListElement) => { list = element; }}>
          {/* Keyed by run, so a new read of the status keeps an open ⋯ menu open. */}
          <For each={props.rows} keyed={(row) => row.id}>
            {(row) => (
              <li class="w-row" data-row={row().id} data-selected={props.selected === row().id ? "" : undefined}>
                <button type="button" class="w-row-open" onFocus={() => props.onSelected(row().id)} onClick={() => store.open(row().route)}>
                  <span class="w-row-status">
                    <StatusDot status={row().dot} />
                    {row().word}
                  </span>
                  <span class="w-row-title">{row().title}</span>
                  <span class="w-row-detail">{row().detail}</span>
                </button>
                <Menu label={`Actions for ${row().title}`} icon={<EllipsisIcon />} items={menuItems(row())} onSelect={(value) => act(row(), value)} />
              </li>
            )}
          </For>
        </ul>
      </Show>
    </>
  );
}
