import { fireEvent as dom, screen, waitFor, within } from "@testing-library/dom";
import { flush } from "solid-js";
import { describe, expect, it } from "vitest";
import { matches } from "./Reviews";
import { HOLD_MS } from "./store";
import { renderWithFakeHost } from "./test/connect";

// Solid applies updates in a microtask, so each event is followed by flush() to see its effect.
const fireEvent = {
  click: (element: Element) => (dom.click(element), flush()),
  input: (element: Element, init: object) => (dom.input(element, init), flush()),
};
/** A key pressed where the focus is, or on the page when nothing has it. */
const press = (key: string) => (dom.keyDown(document.activeElement ?? document.body, { key }), flush());

const RUN = "20310727T010246Z-101";
const EARLIER = "20310726T120000Z-107";
const TITLE = "refactor(cache): separate the cache configuration";
const CACHE = /^(?!Actions for ).*refactor\(cache\)/;

const tab = (name: string) => within(screen.getByRole("navigation", { name: "Whooper" })).getByRole("button", { name: new RegExp(`^${name}`) });
const rail = () => screen.getByRole("button", { name: /^Whooper/, expanded: false });
const list = (name = "Reviews") => screen.findByRole("list", { name });
const rowTexts = (element: HTMLElement) => within(element).queryAllByRole("listitem").map((row) => row.textContent);
const statusWords = (element: HTMLElement) =>
  [...element.querySelectorAll(".w-row-status")].map((status) => status.textContent);
const search = (name = "Search reviews") => screen.getByRole("searchbox", { name });
const chip = (name: RegExp) => within(screen.getByRole("group", { name: "Show reviews" })).getByRole("button", { name });
/** Opens a row's ⋯ menu and chooses item. */
async function choose(title: string, item: string) {
  fireEvent.click(screen.getByRole("button", { name: `Actions for ${title}` }));
  const choice = await screen.findByRole("menuitem", { name: item });
  await waitFor(() => expect(choice).not.toHaveAttribute("data-disabled"), { timeout: 2_000 });
  fireEvent.click(choice);
}

describe("the page's tabs", () => {
  it("opens on Reviews at /: Whooper's status, the tabs with their counts, and the reviews that wait on you first", async () => {
    const host = renderWithFakeHost({ openOnReady: "/" });
    expect(await screen.findByRole("heading", { name: "Reviews" })).toBeInTheDocument();
    const reviews = await list();
    const nav = screen.getByRole("navigation", { name: "Whooper" });
    expect(within(nav).getByText("Can't review · Docker isn't running")).toBeInTheDocument();
    // The tab counts the reviews waiting on you; the header also says how many the list holds.
    expect(tab("Reviews")).toHaveTextContent("Reviews5");
    expect(tab("Reviews")).toHaveAttribute("aria-current", "page");
    expect(tab("Causes")).toHaveTextContent("Causes1");
    expect(tab("Past reviews")).not.toHaveAttribute("aria-current");
    expect(screen.getByText("5 waiting on you · 7 listed")).toBeInTheDocument();
    expect(statusWords(reviews)).toEqual([
      "Ready to approve",
      "Ready to approve",
      "Ready to approve",
      "Needs a look",
      "Review failed",
      "Reviewing",
      "Waiting",
    ]);
    expect(within(reviews).getByRole("button", { name: CACHE })).toHaveTextContent("widgets#101 · @alex · 2 findings");
    // Prompting Time lists the reviews, so the page reads only the status.
    expect(host.calls("status")).toEqual([[]]);
    expect(host.calls("show-review")).toEqual([]);
  });

  it("opens each tab at its route, and marks the tab whose list holds the review or cause Prompting Time opens", async () => {
    const host = renderWithFakeHost({ openOnReady: "/" });
    await list();
    fireEvent.click(tab("Past reviews"));
    expect(await screen.findByRole("heading", { name: "Past reviews" })).toBeInTheDocument();
    // The first review of #107, which a newer review replaced.
    expect(statusWords(await list("Past reviews"))).toEqual(["Earlier review"]);

    fireEvent.click(tab("Causes"));
    expect(await screen.findByRole("heading", { name: "Docker isn't running" })).toBeInTheDocument();
    expect(screen.getByText(/^Stops every review · since .* · 1 review waiting$/)).toBeInTheDocument();
    expect(tab("Causes")).toHaveAttribute("aria-current", "page");

    host.open(`/reviews/${RUN}`);
    fireEvent.click(await screen.findByRole("button", { name: "Back to the list" }));
    expect(await screen.findByRole("heading", { name: "Reviews" })).toBeInTheDocument();

    host.open(`/reviews/${EARLIER}`);
    await screen.findByText(/Review 1 of 2, read-only/, undefined, { timeout: 15_000 });
    fireEvent.click(rail());
    expect(tab("Past reviews")).toHaveAttribute("aria-current", "page");
    fireEvent.click(screen.getByRole("button", { name: "Back to the list" }));
    expect(await screen.findByRole("heading", { name: "Past reviews" })).toBeInTheDocument();

    host.open("/causes/docker_unavailable");
    expect(await screen.findByRole("heading", { name: "Causes" })).toBeInTheDocument();
    expect(tab("Causes")).toHaveAttribute("aria-current", "page");
    fireEvent.click(within(screen.getByRole("list", { name: "Reviews it holds back" })).getByRole("button", { name: /example-org\/widgets#115/ }));
    expect(await screen.findByRole("heading", { name: "Waiting · Docker isn't running" })).toBeInTheDocument();
  }, 30_000);

  it("searches by title, repo, PR number, or author, and filters by group with counts that follow the search", async () => {
    const host = renderWithFakeHost({ openOnReady: "/" });
    const reviews = await list();
    fireEvent.input(search(), { target: { value: "gadgets" } });
    expect(statusWords(reviews)).toEqual(["Review failed"]);
    fireEvent.input(search(), { target: { value: "#133" } });
    expect(rowTexts(reviews)).toEqual([expect.stringMatching(/check names/)]);
    fireEvent.input(search(), { target: { value: "@ALEX" } });
    expect(rowTexts(reviews)).toEqual([expect.stringMatching(CACHE)]);
    fireEvent.input(search(), { target: { value: "widgets upload" } });
    expect(rowTexts(reviews)).toEqual([expect.stringMatching(/retry uploads/)]);
    fireEvent.input(search(), { target: { value: "nothing like this" } });
    expect(screen.getByText("No reviews match.")).toBeInTheDocument();

    fireEvent.input(search(), { target: { value: "widgets" } });
    expect(chip(/^All/)).toHaveTextContent("All · 6");
    expect(chip(/^Ready to approve/)).toHaveTextContent("Ready to approve · 3");
    // A group with nothing that matches offers no chip.
    expect(within(screen.getByRole("group", { name: "Show reviews" })).queryByRole("button", { name: /^Review failed/ })).not.toBeInTheDocument();
    fireEvent.click(chip(/^Ready to approve/));
    expect(chip(/^Ready to approve/)).toHaveAttribute("aria-pressed", "true");
    expect(statusWords(await list())).toEqual(["Ready to approve", "Ready to approve", "Ready to approve"]);

    // The search and the filter wait while a review shows, for Back to the list.
    fireEvent.click(within(await list()).getByRole("button", { name: CACHE }));
    fireEvent.click(await screen.findByRole("button", { name: "Back to the list" }));
    expect(await screen.findByRole("searchbox", { name: "Search reviews" })).toHaveValue("widgets");
    expect(chip(/^Ready to approve/)).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(chip(/^Ready to approve/));
    expect(chip(/^All/)).toHaveAttribute("aria-pressed", "true");
    expect(host.calls("show-review")).toEqual([[RUN]]);
  });

  it("skips a review from its row with Undo, removes it to Past reviews, which restores it, and retries it", async () => {
    const host = renderWithFakeHost({ openOnReady: "/" });
    await list();
    fireEvent.click(screen.getByRole("button", { name: `Actions for ${TITLE}` }));
    expect((await screen.findAllByRole("menuitem")).map((item) => item.textContent)).toEqual(["Open", "Skip", "Remove from the list", "Retry"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "Skip" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Skipped “refactor(cache): separate the cache configuration”.");
    await waitFor(() => expect(within(screen.getByRole("button", { name: CACHE })).getByText("Done")).toBeInTheDocument());
    await waitFor(() => expect(within(screen.getByRole("status")).getByRole("button", { name: "Undo" })).toBeEnabled(), { timeout: 2_000 });
    fireEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(within(screen.getByRole("button", { name: CACHE })).getByText("Ready to approve")).toBeInTheDocument());
    expect(host.calls("skip")).toEqual([[RUN], [RUN, "--undo"]]);

    await choose(TITLE, "Remove from the list");
    expect(await screen.findByRole("status")).toHaveTextContent("Removed “refactor(cache): separate the cache configuration” from the list.");
    await waitFor(() => expect(screen.queryByRole("button", { name: CACHE })).not.toBeInTheDocument());
    expect(tab("Reviews")).toHaveTextContent("Reviews4");
    fireEvent.click(tab("Past reviews"));
    const past = await list("Past reviews");
    expect(within(past).getByRole("button", { name: CACHE })).toHaveTextContent("Removed");
    await choose(TITLE, "Restore to the list");
    expect(await screen.findByRole("status")).toHaveTextContent("Restored “refactor(cache): separate the cache configuration” to the list.");
    await waitFor(() => expect(within(past).queryByRole("button", { name: CACHE })).not.toBeInTheDocument());
    expect(host.calls("remove")).toEqual([[RUN], [RUN, "--undo"]]);

    fireEvent.click(tab("Reviews"));
    await list();
    await choose(TITLE, "Retry");
    expect(await screen.findByRole("status")).toHaveTextContent("queued another review of example-org/widgets#101");
    expect(host.calls("retry")).toEqual([[RUN]]);

    await choose(TITLE, "Open");
    expect(await screen.findByRole("button", { name: "Back to the list" })).toBeInTheDocument();
    expect(host.calls("show-review")).toEqual([[RUN]]);
  }, 30_000);

  it("offers only the row actions whooper-status lists for the review, such as Skip alone while it runs", async () => {
    renderWithFakeHost({ openOnReady: "/" });
    await list();
    fireEvent.click(screen.getByRole("button", { name: /^Actions for feat\(api\)/ }));
    expect((await screen.findAllByRole("menuitem")).map((item) => item.textContent)).toEqual(["Open", "Skip"]);
  });

  it("pauses and resumes Whooper, and polls now, from any tab's header", async () => {
    const host = renderWithFakeHost({ openOnReady: "/causes" });
    expect(await screen.findByRole("heading", { name: "Causes" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Pause" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    // A cause that stops every review outranks the pause in whooper-status's words, so the page adds it.
    expect(await screen.findByText("Paused · Can't review · Docker isn't running")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Resume" })).toBeEnabled(), { timeout: 2_000 });
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(await screen.findByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(host.calls("pause")).toEqual([[]]);
    expect(host.calls("resume")).toEqual([[]]);

    fireEvent.click(tab("Reviews"));
    await list();
    await waitFor(() => expect(screen.getByRole("button", { name: "Pause" })).toBeEnabled(), { timeout: 2_000 });
    fireEvent.click(screen.getByRole("button", { name: "Poll now" }));
    expect(screen.getByRole("button", { name: "Polling…" })).toBeDisabled();
    // A round, which can take minutes, holds only Poll now.
    expect(screen.getByRole("button", { name: "Pause" })).toBeEnabled();
    expect(await screen.findByRole("button", { name: "Poll now" })).toBeEnabled();
    expect(host.calls("round")).toEqual([[]]);
  });

  it("selects rows with j and k, around the ends, and opens the selected one with Enter, also after Back to the list", async () => {
    const host = renderWithFakeHost({ openOnReady: "/" });
    const reviews = await list();
    const selected = () => reviews.querySelector("[data-selected]")?.querySelector(".w-row-title")?.textContent;
    const titles = [...reviews.querySelectorAll(".w-row-title")].map((title) => title.textContent);
    press("j");
    expect(selected()).toBe(titles[0]);
    expect(document.activeElement).toHaveClass("w-row-open");
    press("j");
    expect(selected()).toBe(titles[1]);
    press("k");
    press("k");
    expect(selected()).toBe(titles.at(-1));
    // Not while typing in the search.
    search().focus();
    press("j");
    expect(selected()).toBe(titles.at(-1));

    fireEvent.input(search(), { target: { value: "cache" } });
    (document.activeElement as HTMLElement).blur();
    press("j");
    expect(selected()).toMatch(/refactor\(cache\)/);
    // The focused row's button takes Enter as a click.
    dom.click(document.activeElement!);
    flush();
    fireEvent.click(await screen.findByRole("button", { name: "Back to the list" }));
    await list();
    expect(selected()).toMatch(/refactor\(cache\)/);
    expect(document.activeElement).toBe(document.body);
    press("Enter");
    expect(await screen.findByRole("button", { name: "Back to the list" })).toBeInTheDocument();
    expect(host.calls("show-review")).toEqual([[RUN], [RUN]]);
  });

  it("moves through Past reviews with j and k, and leaves the list alone while a row's menu is open, which Escape closes from anywhere", async () => {
    renderWithFakeHost({ openOnReady: "/" });
    await list();
    fireEvent.click(screen.getByRole("button", { name: `Actions for ${TITLE}` }));
    const menu = await screen.findByRole("menu");
    await waitFor(() => expect(menu).toBeVisible());
    press("j");
    expect(document.querySelector("[data-selected]")).toBeNull();
    // Also when the focus left the menu.
    (document.activeElement as HTMLElement).blur();
    press("Escape");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());

    fireEvent.click(tab("Past reviews"));
    const past = await list("Past reviews");
    press("k");
    expect(past.querySelector("[data-selected]")).toHaveTextContent("Earlier review");
  });

  it("folds the tabs to a rail while a review shows, which unfolds them with the review open, « folds again, and Back unfolds", async () => {
    renderWithFakeHost({ openOnReady: `/reviews/${RUN}` });
    expect(await screen.findByRole("button", { name: "Back to the list" })).toBeInTheDocument();
    await waitFor(() => expect(rail()).toHaveAccessibleName("Whooper · 5 waiting on you"));
    expect(screen.queryByRole("navigation", { name: "Whooper" })).not.toBeInTheDocument();
    fireEvent.click(rail());
    expect(tab("Reviews")).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "Back to the list" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Fold the tabs" }));
    expect(screen.queryByRole("navigation", { name: "Whooper" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to the list" }));
    expect(await screen.findByRole("heading", { name: "Reviews" })).toBeInTheDocument();
    expect(tab("Reviews")).toBeInTheDocument();
    // The lists have nothing to fold to.
    expect(screen.queryByRole("button", { name: "Fold the tabs" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Whooper/, expanded: false })).not.toBeInTheDocument();
  });

  it("names only Whooper on the rail when nothing waits on you, and opens each review folded", async () => {
    const host = renderWithFakeHost({ openOnReady: `/reviews/${RUN}` });
    await screen.findByRole("button", { name: "Back to the list" });
    await waitFor(() => expect(rail()).toHaveAccessibleName("Whooper · 5 waiting on you"));
    fireEvent.click(rail());
    host.open(`/reviews/${EARLIER}`);
    await screen.findByText(/Review 1 of 2, read-only/, undefined, { timeout: 15_000 });
    expect(rail()).toBeInTheDocument();
    // Skip each review that waits on you, from a list.
    host.open("/");
    for (const title of await waitFor(() => {
      const waiting = [...document.querySelectorAll(".w-row")].filter((row) => /Ready to approve|Needs a look|Review failed/.test(row.textContent ?? ""));
      expect(waiting).toHaveLength(5);
      return waiting.map((row) => row.querySelector(".w-row-title")!.textContent!);
    }))
    {
      await choose(title, "Skip");
      // Each skip holds the row actions for HOLD_MS after its reply.
      await screen.findByText(`Skipped “${title}”.`);
      await new Promise((resolve) => setTimeout(resolve, HOLD_MS + 50));
    }
    await waitFor(() => expect(tab("Reviews")).toHaveTextContent(/^Reviews$/));
    host.open(`/reviews/${RUN}`);
    await waitFor(() => expect(rail()).toHaveAccessibleName("Whooper"));
  }, 30_000);

  it("finds a review only when every word of the search is in its text", () => {
    expect(matches("", "anything")).toBe(true);
    expect(matches("Widgets  #101", "refactor example-org/widgets #101 @alex")).toBe(true);
    expect(matches("widgets #102", "refactor example-org/widgets #101 @alex")).toBe(false);
  });
});
