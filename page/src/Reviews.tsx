import { createSignal, For, onSettled, Show, untrack } from "solid-js";
import { Button, EllipsisIcon, Menu, SearchIcon, StatusDot, type MenuItem } from "@prompting-time/ui";
import manifest from "../../prompting-time-plugin.json";
import type { Dot, StatusItem, StatusReview } from "./protocol";
import type { Outcome, PageStore } from "./store";
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
  menu: RowAction[];
}

type RowAction = "skip" | "unskip" | "remove" | "restore" | "retry";

/** What each row action calls, once per review, and how its notice words what it did: `what` is the
 * review's quoted title, or "3 reviews". Skip and Remove offer Undo, which is Undo skip and Restore. */
const ACTIONS: Record<
  RowAction,
  { label: string; command: "skip" | "remove" | "retry"; flags: string[]; done(what: string, outcomes: Outcome[]): string; undo?: RowAction }
> = {
  skip: { label: "Skip", command: "skip", flags: [], done: (what) => `Skipped ${what}.`, undo: "unskip" },
  unskip: {
    label: "Undo skip",
    command: "skip",
    flags: ["--undo"],
    done: (what, outcomes) => `${what} ${outcomes.length === 1 ? "is" : "are"} no longer skipped.`,
  },
  remove: { label: "Remove from the list", command: "remove", flags: [], done: (what) => `Removed ${what} from the list.`, undo: "restore" },
  restore: { label: "Restore to the list", command: "remove", flags: ["--undo"], done: (what) => `Restored ${what} to the list.` },
  // One retry says what whooper-retry printed: which PR at which head the next round reviews.
  retry: {
    label: "Retry",
    command: "retry",
    flags: [],
    done: (_, outcomes) => (outcomes.length === 1 ? outcomes[0].printed! : `Queued another review of ${outcomes.length} pull requests.`),
  },
};
const ORDER = Object.keys(ACTIONS) as RowAction[];

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
    const menu: RowAction[] = [];
    if (actions.includes("skip")) menu.push("skip");
    else if (review?.skipped_by_user) menu.push("unskip");
    if (actions.includes("remove")) menu.push("remove");
    if (actions.includes("retry")) menu.push("retry");
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
      menu: review.removed_by_user ? ["restore"] : [],
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

/** Whether the key goes to text the user types in; a checkbox takes no text, so the list's keys work there. */
const typing = (event: KeyboardEvent) =>
  event.target instanceof HTMLElement &&
  (event.target.isContentEditable ||
    ["TEXTAREA", "SELECT"].includes(event.target.tagName) ||
    (event.target instanceof HTMLInputElement && event.target.type !== "checkbox"));

/** Whether a menu or dialog is open, which takes the keys, Escape among them, for itself. */
const covered = () =>
  [...document.querySelectorAll('[role="dialog"], [role="alertdialog"], [role="menu"]:not([hidden])')].some((element) => element.checkVisibility?.() ?? true);

/**
 * The rows, and what the last row action did, with Undo for a skip or a removal. j and k select the
 * next and previous row, as they step through the walkthrough's places, from the first or the last
 * and around, and move the focus to it; Enter opens it, also once the focus left the rows for the
 * page itself, as after Back to the list. Not while typing, with a modifier, or while a menu or dialog
 * is open, where Escape closes it.
 *
 * Each row has a checkbox, shown on the selected row and on every row once one is checked: x checks or
 * unchecks the selected row, Shift and a click checks or unchecks every row from the last one checked
 * or unchecked, ⌘A checks every row the search and group show, and Escape unchecks them all. A bar
 * then says how many are selected and offers each action every checked row's ⋯ menu offers, which
 * runs once per review, one after another, as the row's menu runs it for one. The bar counts and acts
 * on only the checked rows the list shows; a checked row the search hides stays checked, unseen,
 * until an action or Escape. The rows whose call failed stay checked, beside why.
 */
function RowList(props: { store: PageStore; label: string; rows: Row[]; selected: string | null; onSelected(id: string): void; empty: string }) {
  const store = props.store;
  let list: HTMLUListElement | undefined;
  const buttonOf = (id: string) => list?.querySelector<HTMLButtonElement>(`[data-row="${CSS.escape(id)}"] .w-row-open`) ?? null;

  const [checked, setChecked] = createSignal<ReadonlySet<string>>(new Set());
  // The row last checked or unchecked, where a Shift-click's range starts.
  let anchor: string | null = null;
  const checkedRows = () => props.rows.filter((row) => checked().has(row.id));
  const bulkActions = () => {
    const rows = checkedRows();
    return ORDER.filter((action) => rows.every((row) => row.menu.includes(action)));
  };

  /** Checks or unchecks row, or with range every row from the anchor to it, and selects it. */
  function check(row: Row, on: boolean, range: boolean) {
    const rows = props.rows;
    const to = rows.findIndex((r) => r.id === row.id);
    const from = range && anchor !== null ? rows.findIndex((r) => r.id === anchor) : -1;
    const ids = from < 0 ? [row.id] : rows.slice(Math.min(from, to), Math.max(from, to) + 1).map((r) => r.id);
    const next = new Set(checked());
    for (const id of ids) {
      if (on) next.add(id);
      else next.delete(id);
    }
    setChecked(next);
    anchor = row.id;
    props.onSelected(row.id);
  }

  onSettled(() => {
    const keys = (event: KeyboardEvent) => {
      if (event.defaultPrevented || typing(event) || event.altKey) return;
      const all = (event.metaKey || event.ctrlKey) && !event.shiftKey && event.key.toLowerCase() === "a";
      if (!all && (event.metaKey || event.ctrlKey)) return;
      if (!all && !["j", "k", "Enter", "x", "Escape"].includes(event.key)) return;
      if (covered()) return;
      const rows = untrack(() => props.rows);
      const now = rows.findIndex((row) => row.id === untrack(() => props.selected));
      if (all) {
        event.preventDefault();
        setChecked(new Set(rows.map((row) => row.id)));
        anchor = null;
        return;
      }
      if (event.key === "Escape") {
        if (untrack(checked).size === 0) return;
        event.preventDefault();
        setChecked(new Set<string>());
        anchor = null;
        return;
      }
      if (event.key === "x") {
        if (now < 0) return;
        event.preventDefault();
        check(rows[now], !untrack(checked).has(rows[now].id), false);
        return;
      }
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
  const [notice, setNotice] = createSignal<{ text: string; undo?: () => void } | null>(null);
  const [error, setError] = createSignal<string | null>(null);

  /** Runs action once for each row, and says what it did, with Undo for a skip or a removal, unless it
   * is that Undo, and which calls failed and why; returns the rows that failed. */
  async function apply(rows: Row[], action: RowAction, say = true): Promise<Row[]> {
    const { command, flags, done, undo } = ACTIONS[action];
    setError(null);
    setNotice(null);
    const outcomes = await store.actOnEach(command, rows.map((row) => row.id), flags);
    const worked = rows.filter((_, i) => outcomes[i].error === undefined);
    const failed = rows.filter((_, i) => outcomes[i].error !== undefined);
    if (say && worked.length > 0) {
      const what = worked.length === 1 ? quoted(worked[0].title) : `${worked.length} reviews`;
      const text = done(what, outcomes.filter((outcome) => outcome.error === undefined));
      setNotice({ text, undo: undo && (() => void apply(worked, undo, false)) });
    }
    // One review's failure reads as before: the command's own words.
    if (failed.length > 0)
      setError(rows.length === 1 ? outcomes[0].error! : failed.map((row) => `${quoted(row.title)}: ${outcomes[rows.indexOf(row)].error}`).join("\n"));
    return failed;
  }

  async function applyChecked(action: RowAction) {
    const failed = await apply(checkedRows(), action);
    setChecked(new Set(failed.map((row) => row.id)));
    anchor = null;
  }

  const menuItems = (row: Row): MenuItem[] => [
    { value: "open", label: "Open" },
    ...row.menu.map((action) => ({ value: action, label: ACTIONS[action].label, disabled: store.held() })),
  ];

  return (
    <>
      <Show when={checkedRows().length > 0}>
        <div class="w-bulk" role="region" aria-label="Selected reviews">
          <span class="w-bulk-count">{checkedRows().length} selected</span>
          <For each={bulkActions()} fallback={<span class="w-bulk-none">No action fits every selected review.</span>}>
            {(action) => (
              <Button disabled={store.held()} onClick={() => void applyChecked(action)}>
                {ACTIONS[action].label}
              </Button>
            )}
          </For>
          <span class="w-spacer" />
          <span class="w-bulk-keys">x selects · ⌘A selects all · Esc clears</span>
          <Button
            onClick={() => {
              setChecked(new Set<string>());
              anchor = null;
            }}
          >
            Clear
          </Button>
        </div>
      </Show>
      <Show when={notice()}>
        {(current) => (
          <div class="w-notice" role="status">
            <span>{current().text}</span>
            <Show when={current().undo}>
              {(undo) => (
                <button type="button" class="w-link" disabled={store.held()} onClick={() => undo()()}>
                  Undo
                </button>
              )}
            </Show>
          </div>
        )}
      </Show>
      <Show when={error()}>
        {(text) => (
          <p class="w-error w-error-lines" role="alert">
            {text()}
          </p>
        )}
      </Show>
      <Show when={props.rows.length > 0} fallback={<p class="w-empty">{props.empty}</p>}>
        <ul
          class="w-rows"
          aria-label={props.label}
          data-checking={checkedRows().length > 0 ? "" : undefined}
          ref={(element: HTMLUListElement) => {
            list = element;
          }}
        >
          {/* Keyed by run, so a new read of the status keeps an open ⋯ menu open. */}
          <For each={props.rows} keyed={(row) => row.id}>
            {(row) => (
              <li class="w-row" data-row={row().id} data-selected={props.selected === row().id ? "" : undefined}>
                <input
                  type="checkbox"
                  class="w-row-check"
                  aria-label={`Select ${row().title}`}
                  checked={checked().has(row().id)}
                  onFocus={() => props.onSelected(row().id)}
                  onClick={(e) => check(row(), e.currentTarget.checked, e.shiftKey)}
                />
                <button type="button" class="w-row-open" onFocus={() => props.onSelected(row().id)} onClick={() => store.open(row().route)}>
                  <span class="w-row-status">
                    <StatusDot status={row().dot} />
                    {row().word}
                  </span>
                  <span class="w-row-title">{row().title}</span>
                  <span class="w-row-detail">{row().detail}</span>
                </button>
                <Menu
                  label={`Actions for ${row().title}`}
                  icon={<EllipsisIcon />}
                  items={menuItems(row())}
                  onSelect={(value) => (value === "open" ? store.open(row().route) : void apply([row()], value as RowAction))}
                />
              </li>
            )}
          </For>
        </ul>
      </Show>
    </>
  );
}
