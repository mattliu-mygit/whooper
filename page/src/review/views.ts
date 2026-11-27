import { createSignal } from "solid-js";
import type { Review } from "../protocol";

/**
 * The views of a review beyond its walkthrough. Each adds a short summary where its question comes up
 * in the walkthrough, and only when Whooper printed data for it in this review. The Views menu lists
 * every view, saying when one has nothing for this review, except one marked `onlyWithData`, which it
 * lists only for a review that has its data.
 */
export type ViewId = "since" | "calls" | "co-change" | "connections" | "test-gaps" | "inputs";
export type ReviewView = { id: ViewId; name: string; description: string; onlyWithData?: boolean };
export const REVIEW_VIEWS: readonly ReviewView[] = [
  { id: "since", name: "Changes since you last looked", description: "New commits since the review, and what you last read." },
  { id: "inputs", name: "Example inputs, before and after", description: "What the old and new code give for the same inputs, from running both.", onlyWithData: true },
  { id: "test-gaps", name: "Changes no test would catch", description: "Changed lines no test ran, and changes made on purpose that no test noticed.", onlyWithData: true },
  { id: "calls", name: "Calls before and after", description: "Old and new calls for one input, overlaid." },
  { id: "co-change", name: "Files that usually change with these", description: "Files that change with these in history, left alone here." },
  { id: "connections", name: "How the changed code connects", description: "Changed functions and types, and their callers.", onlyWithData: true },
];

/** The views that show in full in the walkthrough's place, opened from their summary. */
export type FullViewId = Extract<ViewId, "connections" | "test-gaps" | "inputs">;

export const viewName = (id: ViewId) => REVIEW_VIEWS.find((view) => view.id === id)!.name;

/** Whether the review has something for the view to show. */
export function viewHasData(id: ViewId, review: Review): boolean {
  switch (id) {
    case "since":
      return (review.commits_since_review?.length ?? 0) > 0;
    case "calls":
      return review.overview?.flow != null;
    case "co-change":
      return review.co_change.length > 0;
    case "connections":
      return review.connections !== null;
    case "test-gaps":
      return review.test_gaps !== null;
    case "inputs":
      return review.examples !== null;
  }
}

/** Whether the review's Views menu lists the view. */
export const viewListed = (view: ReviewView, review: Review) => !view.onlyWithData || viewHasData(view.id, review);

export type ReviewViews = ReturnType<typeof createReviewViews>;

/** The views the user turned on or off, for every review, in memory for as long as the page stays
 * loaded, for the reason createReviewMemory gives. */
export function createReviewViews() {
  const [off, setOff] = createSignal<readonly ViewId[]>([]);
  return {
    isOn: (id: ViewId) => !off().includes(id),
    setOn(id: ViewId, on: boolean) {
      // From the pending value, not a read: Solid applies writes at the end of the batch.
      setOff((current) => (on ? current.filter((item) => item !== id) : [...current.filter((item) => item !== id), id]));
    },
  };
}
