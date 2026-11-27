import { describe, expect, it } from "vitest";
import { createReviewMemory } from "./memory";

const review = { run_id: "r2", repo: "example-org/widgets", pr: 101, pr_head_sha: "cccc2222dddd" };

describe("what the page remembers about reviews", () => {
  it("returns the version of a pull request's review the user opened before, by pull request", () => {
    const memory = createReviewMemory();
    expect(memory.rememberLook({ ...review, run_id: "r1", pr_head_sha: "aaaa1111bbbb" }, new Date("2031-07-24T10:00:00Z"))).toBeNull();
    expect(memory.rememberLook(review)).toEqual({ run: "r1", head: "aaaa1111bbbb", at: "2031-07-24T10:00:00.000Z" });
    expect(memory.rememberLook({ ...review, pr: 107 })).toBeNull();
  });

  it("hides the since notice for one version of the pull request, so a new commit or review brings it back", () => {
    const memory = createReviewMemory();
    memory.hideSince(review, true);
    expect(memory.sinceHidden(review)).toBe(true);
    expect(memory.sinceHidden({ ...review, pr_head_sha: "dddd3333eeee" })).toBe(false);
    expect(memory.sinceHidden({ ...review, run_id: "r3" })).toBe(false);
    memory.hideSince(review, false);
    expect(memory.sinceHidden(review)).toBe(false);
  });
});
