import { createSignal } from "solid-js";
import type { StatusReview } from "../protocol";

/** The review version the user last opened, the pull request's head then, and when. */
export type LastLook = { run: string; head: string | null; at: string };

/** A review as whooper-status lists it, with what the memory keys it by. */
type Reviewed = Pick<StatusReview, "run_id" | "repo" | "pr" | "pr_head_sha">;

/** The pull request a review belongs to, such as "example-org/widgets#101". */
const pullRequestKey = (review: Reviewed) => `${review.repo}#${review.pr}`;
/** The version of the pull request that × hides the notice for: the review and the pull request's head, so a new commit or review brings it back. */
const sinceVersion = (review: Reviewed) => `${review.run_id}@${review.pr_head_sha ?? ""}`;

export type ReviewMemory = ReturnType<typeof createReviewMemory>;

/**
 * What the page remembers about the user's reviews while Prompting Time keeps it loaded: the version
 * of each pull request's review they last opened, the versions whose "Changes since you last looked"
 * notice they hid, and, per run, the walkthrough's layers they marked read and whether its decision
 * bar opened; and whether the walkthrough's navigator is folded.
 *
 * Only in memory, so a reload, such as when Prompting Time restarts, forgets it all. Prompting Time
 * sandboxes the page to scripts only, which gives it an opaque origin, and an opaque origin has no
 * localStorage: reading it throws a SecurityError. Keeping it across reloads would take a Whooper
 * action that saves it in the state folder.
 */
export function createReviewMemory() {
  const lastOpened = new Map<string, LastLook>();
  const hidden = new Map<string, string>();
  const [navCollapsed, setNavCollapsed] = createSignal(false);
  return {
    /** Remembers that the user opened this version of the review now, and returns what they opened before, or null. */
    rememberLook(review: Reviewed, now = new Date()): LastLook | null {
      const key = pullRequestKey(review);
      const before = lastOpened.get(key) ?? null;
      lastOpened.set(key, { run: review.run_id, head: review.pr_head_sha, at: now.toISOString() });
      return before;
    },
    sinceHidden: (review: Reviewed) => hidden.get(pullRequestKey(review)) === sinceVersion(review),
    /** Hides the notice for this version of the pull request, or shows it again. */
    hideSince(review: Reviewed, hide: boolean) {
      if (hide) hidden.set(pullRequestKey(review), sinceVersion(review));
      else hidden.delete(pullRequestKey(review));
    },
    /** The layers marked read, by run. */
    readLayers: new Map<string, ReadonlySet<string>>(),
    /** The runs whose decision bar opened. */
    decided: new Set<string>(),
    navCollapsed,
    setNavCollapsed,
  };
}
