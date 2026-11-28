import { fireEvent as dom, screen, waitFor, within } from "@testing-library/dom";
import { flush } from "solid-js";
import { describe, expect, it } from "vitest";
import type { FakeHostOptions } from "./fakeHost";
import { renderWithFakeHost } from "./test/connect";
import { usePageCss } from "./test/layout";

// Solid applies updates in a microtask, so each event is followed by flush() to see its effect.
const click = (element: Element) => (dom.click(element), flush());
/** A key pressed where the focus is, or on the page when nothing has it. */
const press = (key: string) => (dom.keyDown(document.activeElement ?? document.body, { key }), flush());

const HEADING = "Rounds per day, last 15 days";
const HINT = "Click a day for its counts, or move between days with j and k and press Enter.";
/** A day of the fixture's July 2031 as the page writes it, such as "Thu, Jul 17", from its local midnight. */
const date = (day: number) => new Date(2031, 6, day).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });

const strip = () => screen.getByRole("group", { name: "Days, oldest first" });
const bars = () => within(strip()).getAllByRole("button");
/** The bar of a day in July 2031; the strip starts on Jul 13. */
const bar = (day: number) => bars()[day - 13];
const legend = () => screen.getByRole("list", { name: "What each color means, with its total" });
/** The line below the strip. */
const line = () => document.querySelector(".w-rounds-day");

/** Opens the Causes tab, which the chart shares with the cause that stops every review. */
async function openCauses(options: FakeHostOptions = {}) {
  const host = renderWithFakeHost({ openOnReady: "/causes", ...options });
  await screen.findByRole("heading", { name: "Docker isn't running" });
  return host;
}

describe("the chart of rounds per day on the Causes tab", () => {
  it("draws a bar per day of the last 15 days, oldest first, each named by its date and counts, under a legend with the totals", async () => {
    await openCauses();
    expect(screen.getByRole("heading", { name: HEADING })).toBeInTheDocument();
    expect(bars().map((element) => element.getAttribute("aria-label"))).toEqual([
      `${date(13)}: no rounds`,
      `${date(14)}: 28 rounds, 28 done`,
      `${date(15)}: 28 rounds, 1 interrupted, 27 done`,
      `${date(16)}: 28 rounds, 19 poll failed, 9 done`,
      `${date(17)}: 29 rounds, 29 poll failed`,
      `${date(18)}: 27 rounds, 1 poll failed, 26 done`,
      `${date(19)}: no rounds`,
      `${date(20)}: no rounds`,
      `${date(21)}: 29 rounds, 29 done`,
      `${date(22)}: 28 rounds, 13 done, 15 skipped while paused`,
      `${date(23)}: 28 rounds, 28 done`,
      `${date(24)}: 29 rounds, 2 interrupted, 27 done`,
      `${date(25)}: 28 rounds, 28 done`,
      `${date(26)}: 3 rounds, 3 done`,
      `${date(27)}: 1 round, 1 done`,
    ]);
    expect(within(legend()).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Poll failed · 49",
      "Interrupted · 3",
      "Done · 219",
      "Skipped while paused · 15",
    ]);
    // Each part of a bar is its share of the busiest day, 29 rounds, from the baseline up; a day without rounds
    // keeps an empty slot.
    const parts = (day: number) => [...bar(day).querySelectorAll<HTMLElement>(".w-rounds-part")].map((part) => [part.dataset.result, parseFloat(part.style.flexBasis)]);
    expect(parts(13)).toEqual([]);
    expect(parts(16)).toEqual([["poll_failed", (19 / 29) * 100], ["done", (9 / 29) * 100]]);
    expect(parts(17)).toEqual([["poll_failed", 100]]);
    expect(parts(22)).toEqual([["done", (13 / 29) * 100], ["skipped", (15 / 29) * 100]]);
    // The first and last dates sit under the strip for the eye; the bars' labels already say them.
    const short = (day: number) => new Date(2031, 6, day).toLocaleDateString([], { month: "short", day: "numeric" });
    const axis = document.querySelector(".w-rounds-axis")!;
    expect(axis).toHaveAttribute("aria-hidden", "true");
    expect([...axis.children].map((end) => end.textContent)).toEqual([short(13), short(27)]);
    expect(line()).toHaveTextContent(HINT);
  });

  it("shows a day's counts in one line below the strip on a click on its bar, and hides them on a second click; a hover opens nothing", async () => {
    await openCauses();
    dom.mouseOver(bar(17));
    dom.mouseEnter(bar(17));
    flush();
    expect(line()).toHaveTextContent(HINT);
    expect(strip().querySelector("[title]")).toBeNull();

    click(bar(17));
    expect(line()).toHaveTextContent(`${date(17)} · 29 rounds · 29 poll failed`);
    expect(bars().filter((element) => element.getAttribute("aria-pressed") === "true")).toEqual([bar(17)]);
    // The clicked bar takes the focus, so j and k move on from it.
    expect(document.activeElement).toBe(bar(17));
    click(bar(22));
    expect(line()).toHaveTextContent(`${date(22)} · 28 rounds · 13 done · 15 skipped while paused`);
    click(bar(13));
    expect(line()).toHaveTextContent(`${date(13)} · no rounds`);
    expect(bars().filter((element) => element.getAttribute("aria-pressed") === "true")).toEqual([bar(13)]);
    click(bar(13));
    expect(line()).toHaveTextContent(HINT);
    expect(bars().filter((element) => element.getAttribute("aria-pressed") === "true")).toEqual([]);
  });

  it("moves between days with j and k once the strip has the focus, around the ends, shows the focused day on Enter, and hides it on Escape", async () => {
    const host = await openCauses();
    // The strip is one stop for Tab, on today's bar.
    expect(bars().filter((element) => element.tabIndex === 0)).toEqual([bar(27)]);
    // j and k move nothing while the focus is elsewhere.
    press("k");
    expect(document.activeElement).toBe(document.body);

    bar(27).focus();
    press("k");
    expect(document.activeElement).toBe(bar(26));
    press("j");
    press("j");
    expect(document.activeElement).toBe(bar(13));
    press("k");
    expect(document.activeElement).toBe(bar(27));
    for (let step = 0; step < 4; step++) press("j");
    expect(document.activeElement).toBe(bar(16));
    // Moving shows nothing yet; the focused bar takes Enter as a click.
    expect(line()).toHaveTextContent(HINT);
    press("Enter");
    dom.click(document.activeElement!);
    flush();
    expect(line()).toHaveTextContent(`${date(16)} · 28 rounds · 19 poll failed · 9 done`);
    expect(bars().filter((element) => element.tabIndex === 0)).toEqual([bar(16)]);

    // A new read of the status keeps the focused bar and the chosen day.
    const focused = document.activeElement;
    host.changed(null);
    await waitFor(() => expect(host.calls("status")).toHaveLength(2));
    await new Promise((resolve) => setTimeout(resolve, 0));
    flush();
    expect(document.activeElement).toBe(focused);
    expect(line()).toHaveTextContent(`${date(16)} · 28 rounds · 19 poll failed · 9 done`);

    press("Escape");
    expect(line()).toHaveTextContent(HINT);
    expect(document.activeElement).toBe(bar(16));
  });

  it("leaves the chart out for an older Whooper's status, which has no rounds_by_day", async () => {
    await openCauses({ status: { rounds_by_day: undefined } });
    expect(screen.queryByRole("heading", { name: HEADING })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Days, oldest first" })).not.toBeInTheDocument();
  });

  it("pairs each color with its word in the legend and paints the bars the same: poll failed red, interrupted orange, done gray, skipped an outline", async () => {
    const removeCss = usePageCss();
    try {
      await openCauses();
      const paint = (element: Element) => {
        const style = getComputedStyle(element);
        return { fill: style.backgroundColor, outline: style.boxShadow };
      };
      const items = within(legend()).getAllByRole("listitem");
      // --s-error is the theme's red and --s-warning its orange, which StatusDot gives error and attention.
      expect(items.map((item) => [item.textContent, paint(item.querySelector(".w-rounds-key")!)])).toEqual([
        ["Poll failed · 49", { fill: "var(--s-error)", outline: "" }],
        ["Interrupted · 3", { fill: "var(--s-warning)", outline: "" }],
        ["Done · 219", { fill: "var(--s-text-subtle)", outline: "" }],
        ["Skipped while paused · 15", { fill: "rgba(0, 0, 0, 0)", outline: "inset 0 0 0 1px var(--s-text-subtle)" }],
      ]);
      const keys = new Map(items.map((item) => {
        const key = item.querySelector<HTMLElement>(".w-rounds-key")!;
        return [key.dataset.result, paint(key)];
      }));
      const parts = [...strip().querySelectorAll<HTMLElement>(".w-rounds-part")];
      expect(new Set(parts.map((part) => part.dataset.result))).toEqual(new Set(keys.keys()));
      for (const part of parts) expect(paint(part)).toEqual(keys.get(part.dataset.result));
    } finally {
      removeCss();
    }
  });
});
