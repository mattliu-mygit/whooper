import { screen, waitFor, within } from "@testing-library/dom";
import { afterEach, beforeEach, describe, it } from "vitest";
import { NAME_RUN, RETRY_RUN } from "../fixtures";
import { expectCodeFits, usePageCss } from "../test/layout";
import { fireEvent, openReview, SLOW, walkthrough } from "../test/review";

// A long line of code once widened the walkthrough's column past the page in Prompting Time, with the
// prose centered far off to the right and clipped: these check that every grid around a diff keeps to
// its own width, so such a line scrolls in its diff.
let removeCss: () => void;
beforeEach(() => {
  removeCss = usePageCss();
});
afterEach(() => removeCss());

const diffs = () => waitFor(() => {
  if (!document.querySelector(".pt-diff")) throw new Error("no diff yet");
}, SLOW);

describe("the page's layout around code", () => {
  it("keeps the walkthrough's column and its layers to the column's width", async () => {
    openReview(RETRY_RUN);
    await walkthrough();
    await diffs();
    expectCodeFits(document.querySelector(".walk-column")!);
  }, 30_000);

  it("keeps changes no test would catch, in full, to its column's width", async () => {
    openReview(NAME_RUN);
    await walkthrough();
    const block = screen.getByRole("region", { name: "Changes no test would catch" });
    fireEvent.click(within(block).getByRole("button", { name: "Open all lines" }));
    await diffs();
    expectCodeFits(screen.getByRole("region", { name: "Changes no test would catch" }));
  }, 30_000);

  it("keeps the Diff view's files to the pane's width", async () => {
    openReview(RETRY_RUN);
    fireEvent.click(await screen.findByRole("button", { name: "Diff" }, SLOW));
    await diffs();
    expectCodeFits(document);
  }, 30_000);
});
