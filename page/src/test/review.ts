import { fireEvent as dom, screen, waitFor } from "@testing-library/dom";
import { flush } from "solid-js";
import { expect } from "vitest";
import type { Shown } from "../protocol";
import { renderWithFakeHost } from "./connect";
import type { FakeHostOptions } from "../fakeHost";

/** For the walkthrough's tests: Solid applies updates in a microtask, so each event is followed by
 * flush() to see its effect. */
export const fireEvent = {
  click: (element: Element) => (dom.click(element), flush()),
  keyDown: (element: Element | Document | Window, init: object) => (dom.keyDown(element, init), flush()),
  mouseMove: (element: Element | Window, init: object) => (dom.mouseMove(element, init), flush()),
  scroll: (element: Element) => (dom.scroll(element), flush()),
};

/** Pierre loads Shiki and a grammar before it draws a diff. */
export const SLOW = { timeout: 15_000 };

/** Opens review `runId` on the fake host, with show-review's fields `shown` replacing the fixture's. */
export function openReview(runId: string, shown: Partial<Shown> = {}, options: FakeHostOptions = {}) {
  return renderWithFakeHost({ openOnReady: `/reviews/${runId}`, shown: { [runId]: shown }, ...options });
}

export const walkthrough = () => screen.findByRole("navigation", { name: "Walkthrough" }, SLOW);
/** The Views menu's button, which shows once whooper-status lists the review. */
export const viewsButton = () => screen.findByRole("button", { name: /^Views/ }, SLOW);
export const press = (key: string) => fireEvent.keyDown(document.body, { key });
export const follows = (first: Node, second: Node) => Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);
/** After an action, the page holds its buttons for HOLD_MS. */
export const enabled = (find: () => HTMLElement) =>
  waitFor(
    () => {
      const button = find();
      expect(button).toBeEnabled();
      return button;
    },
    { timeout: 2_000 },
  );
