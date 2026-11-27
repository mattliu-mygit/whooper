import { screen, waitFor, within } from "@testing-library/dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RETRY_RUN } from "../fixtures";
import type { FindingsResult } from "../protocol";
import { enabled, fireEvent, openReview, press, SLOW, viewsButton, walkthrough } from "../test/review";

const RUN = RETRY_RUN;
const placeCard = () => document.querySelector<HTMLElement>(".place-card");
/** The retry review's findings with `kind` for its one finding. */
const findingsOf = (kind: FindingsResult["findings"][number]["kind"], verdict: FindingsResult["approval"]["verdict"] = "approve_once_ci_passes"): FindingsResult => ({
  status: "reviewed",
  skip_reason: "",
  approval: { verdict, reason: "Small and tested." },
  findings: [{ id: "1", path: "src/retry.rs", line: 7, side: "RIGHT", title: "The delay overflows", draft: "**question:** Does `2u64.pow` overflow?", kind }],
});

afterEach(() => {
  document.body.classList.remove("review-dimming");
});

describe("a review's walkthrough", () => {
  it("opens on the walkthrough with its navigator, headed by the verdict, and Diff shows the diff under the same top", async () => {
    renderReview();
    await walkthrough();
    const verdict = screen.getByRole("region", { name: "Verdict" });
    expect(verdict).toHaveAttribute("data-tone", "success");
    expect(verdict).toHaveTextContent("Approve once CI passes");
    expect(verdict).toHaveTextContent("The policy is small and its tests pass.");
    expect(verdict).toHaveTextContent("Review 1 · 2252c0f · 1 draft comment");
    expect(await screen.findByRole("button", { name: "Walkthrough" }, SLOW)).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("region", { name: "Submit review" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Diff" }));
    expect(screen.queryByRole("navigation", { name: "Walkthrough" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Diff" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("region", { name: "Submit review" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Verdict" })).toHaveTextContent("Approve once CI passes");
    // The banner says Whooper's reason, so the diff's own line for it goes.
    expect(document.querySelector(".w-summary")).toBeNull();
  }, 30_000);

  it("tints the verdict orange for a review that needs a look, and says why one that says approve isn't safe to approve", async () => {
    renderReview({ findings: findingsOf("question", "look_first") });
    await walkthrough();
    expect(screen.getByRole("region", { name: "Verdict" })).toHaveAttribute("data-tone", "attention");
    expect(screen.getByRole("region", { name: "Verdict" })).toHaveTextContent("Look first");
  }, 30_000);

  it("says why a review that says approve once CI passes isn't safe to approve", async () => {
    renderReview({ findings: findingsOf("question") });
    await walkthrough();
    expect(screen.getByRole("region", { name: "Verdict" })).toHaveAttribute("data-tone", "attention");
    expect(screen.getByRole("region", { name: "Verdict" })).toHaveTextContent("Approve once CI passes · has a question");
  }, 30_000);

  it("says the commits since the review in one folded line, and × hides it for this version of the pull request, with Undo", async () => {
    renderReview();
    await walkthrough();
    const line = await screen.findByRole("button", { name: /^2 new commits since this review/ }, SLOW);
    // The since notice says it, so the header's banner doesn't.
    expect(screen.queryByText("New commits since this review")).not.toBeInTheDocument();
    expect(line).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("client: log each retry")).not.toBeInTheDocument();
    fireEvent.click(line);
    expect(line).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("client: log each retry")).toBeInTheDocument();
    expect(screen.getByText(/You haven't opened this pull request's review before\. The review read 2252c0f; none of these commits\./)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Hide until the pull request gets new commits" }));
    expect(screen.queryByRole("region", { name: "Changes since you last looked" })).not.toBeInTheDocument();
    const hidden = screen.getByRole("status", { name: "" });
    expect(screen.getByText("Hidden until the pull request gets new commits.")).toBeInTheDocument();
    fireEvent.click(within(hidden).getByRole("button", { name: "Undo" }));
    expect(screen.getByRole("region", { name: "Changes since you last looked" })).toBeInTheDocument();
  }, 30_000);

  it("keeps the since notice hidden for the same version of the pull request while the page stays loaded", async () => {
    const host = renderReview();
    await walkthrough();
    fireEvent.click(await screen.findByRole("button", { name: "Hide until the pull request gets new commits" }, SLOW));
    host.open("/");
    await screen.findByRole("heading", { name: "Reviews" });
    host.open(`/reviews/${RUN}`);
    await walkthrough();
    await screen.findByRole("button", { name: "Walkthrough" }, SLOW);
    expect(screen.queryByRole("region", { name: "Changes since you last looked" })).not.toBeInTheDocument();
    expect(screen.queryByText("Hidden until the pull request gets new commits.")).not.toBeInTheDocument();
  }, 30_000);

  it("steps through the places to look with j and k, dims the rest until the mouse moves, and opens each place's reason", async () => {
    renderReview();
    const nav = await walkthrough();
    press("j");
    expect(within(nav).getByRole("button", { name: /^1The delay doubles without a cap/ })).toHaveAttribute("aria-current", "true");
    expect(document.querySelector(".walkthrough")).toHaveAttribute("data-dim");
    expect(document.body).toHaveClass("review-dimming");
    await waitFor(() => expect(placeCard()).toHaveTextContent("After 20 attempts the delay is hours."), SLOW);
    expect(placeCard()).toHaveTextContent("Read, not run · read delay");

    fireEvent.mouseMove(window, { clientX: 40, clientY: 40 });
    expect(document.querySelector(".walkthrough")).not.toHaveAttribute("data-dim");
    press("j");
    expect(within(nav).getByRole("button", { name: /^2Five attempts, not three/ })).toHaveAttribute("aria-current", "true");
    press("k");
    expect(within(nav).getByRole("button", { name: /^1The delay doubles/ })).toHaveAttribute("aria-current", "true");
    press("Escape");
    expect(document.querySelector(".walkthrough")).not.toHaveAttribute("data-dim");
    expect(document.body).not.toHaveClass("review-dimming");
  }, 30_000);

  it("draws the calls before and after, struck where removed, and a call opens its line in the code", async () => {
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled(this.getAttribute("data-column-number"));
    };
    try {
      renderReview();
      await walkthrough();
      expect(screen.getByRole("group", { name: "A failed upload: calls before and after this change" })).toBeInTheDocument();
      expect(screen.getByRole("img", { name: "client to server: try_once, removed" })).toHaveAttribute("data-status", "removed");
      expect(screen.getByRole("button", { name: "policy to policy: sleep(0), now delay(n), changed" })).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "client to policy: retry, new" }));
      await waitFor(() => expect(scrolled).toHaveBeenCalledWith("3"), SLOW);
    } finally {
      // @ts-expect-error jsdom has no scrollIntoView; the test put one in for itself.
      delete Element.prototype.scrollIntoView;
    }
  }, 30_000);

  it("folds a layer you mark read to one line, and opens it again on a click", async () => {
    renderReview();
    await walkthrough();
    const layer = screen.getByRole("region", { name: "Layer 1: The retry policy" });
    fireEvent.click(within(layer).getByRole("button", { name: "Mark layer 1 read" }));
    const folded = within(layer).getByRole("button", { expanded: false });
    expect(folded).toHaveTextContent("The retry policy");
    expect(folded).toHaveTextContent("1 file · read");
    expect(screen.getByRole("button", { name: "Layer 1 read" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(folded);
    expect(within(layer).getByRole("button", { name: "Layer 1 read · undo" })).toBeInTheDocument();
  }, 30_000);

  it("starts a layer Whooper says to skip folded", async () => {
    const { retryOverview } = await import("../fixtures");
    const overview = retryOverview();
    overview.layers[1].read_hint = "skip";
    renderReview({ overview });
    await walkthrough();
    const layer = screen.getByRole("region", { name: "Layer 2: The client uses it" });
    expect(within(layer).getByRole("button", { expanded: false })).toHaveTextContent("1 file · skip");
  }, 30_000);

  it("lists what Whooper checked and didn't, then the files that usually change with these", async () => {
    renderReview();
    await walkthrough();
    expect(screen.getByRole("heading", { name: "What Whooper checked and didn't" })).toBeInTheDocument();
    expect(screen.getByText("Ran the client tests at head and base")).toBeInTheDocument();
    expect(screen.getByText("Didn't run the upload integration suite")).toBeInTheDocument();
    expect(screen.getByLabelText("Files that usually change with these")).toHaveTextContent("src/server.rs changed with client.rs in 80% of 10 commits");
  }, 30_000);

  it("shows Whooper's draft comment under its line, saying whether it is included", async () => {
    renderReview();
    await walkthrough();
    await waitFor(() => expect(document.querySelector(".draft-card-head")).toHaveTextContent("Whooper's draft comment · included"), SLOW);
  }, 30_000);

  it("turns a view off for every review from the Views menu, and shows no summary for a view without data", async () => {
    const host = renderReview({ co_change: [] });
    await walkthrough();
    expect(screen.queryByLabelText("Files that usually change with these")).not.toBeInTheDocument();
    fireEvent.click(await viewsButton());
    const calls = await screen.findByRole("menuitemcheckbox", { name: /^Calls before and after/ });
    expect(screen.getByRole("menuitemcheckbox", { name: /^Files that usually change with these/ })).toHaveTextContent("Nothing for this review.");
    expect(screen.queryByRole("menuitemcheckbox", { name: /How the changed code connects/ })).not.toBeInTheDocument();
    fireEvent.click(calls);
    await waitFor(() => expect(screen.queryByRole("group", { name: /calls before and after this change/ })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: /^Views · 1 off/ })).toBeInTheDocument();

    // Off for every review the page opens next, too.
    fireEvent.click(screen.getByRole("button", { name: /^Views · 1 off/ }));
    host.open("/reviews/20310727T021000Z-133");
    await screen.findByRole("button", { name: "Open the map" }, SLOW);
    await waitFor(() => expect(screen.getByRole("button", { name: /^Views · 1 off/ })).toBeInTheDocument(), SLOW);
  }, 30_000);

  it("waits as Decide… until clicked, then submits through the host, which confirms first", async () => {
    const confirmSubmit = vi.fn().mockResolvedValue(true);
    const host = renderReview({}, { confirmSubmit });
    await walkthrough();
    expect(screen.queryByRole("group", { name: "Your decision" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Decide…" }));
    const bar = screen.getByRole("group", { name: "Your decision" });
    expect(bar).toHaveTextContent("1 draft comment, 1 included");
    expect(within(bar).getByRole("radio", { name: "Approve" })).toBeChecked();
    fireEvent.click(await enabled(() => within(bar).getByRole("button", { name: "Submit review" })));
    expect(await screen.findByText(/Submitted\. Whooper says: posted the review/)).toBeInTheDocument();
    expect(confirmSubmit).toHaveBeenCalledOnce();
    expect(host.calls("submit")).toEqual([[RUN]]);
    expect(screen.queryByRole("group", { name: "Your decision" })).not.toBeInTheDocument();
  }, 30_000);

  it("saves Comment or Approve with whooper-draft, and lets an approval whooper-submit refused through with Approve anyway", async () => {
    const confirmSubmit = vi.fn().mockResolvedValue(true);
    const host = renderReview({ findings: findingsOf("question") }, { confirmSubmit });
    await walkthrough();
    fireEvent.click(screen.getByRole("button", { name: "Decide…" }));
    const bar = screen.getByRole("group", { name: "Your decision" });
    // A run not safe to approve starts as a comment with nothing included, so there is nothing to submit.
    expect(bar).toHaveTextContent("1 draft comment, 0 included");
    expect(within(bar).getByRole("button", { name: "Submit review" })).toBeDisabled();
    fireEvent.click(within(bar).getByRole("radio", { name: "Approve" }));
    await waitFor(() => expect(host.calls("draft").filter((args) => args.length > 1)).toEqual([[RUN, "approve"]]));
    fireEvent.click(await enabled(() => within(bar).getByRole("button", { name: "Submit review" })));
    const anyway = await screen.findByRole("button", { name: "Approve anyway" });
    expect(bar).toHaveTextContent("the review approves, and the run is not safe to approve");
    expect(confirmSubmit).not.toHaveBeenCalled();
    fireEvent.click(await enabled(() => anyway));
    expect(await screen.findByText(/Submitted\. Whooper says:/)).toBeInTheDocument();
    expect(host.calls("submit")).toEqual([[RUN], [RUN, "--approve-anyway"]]);
  }, 30_000);

  it("opens the decision bar once you scroll to the end", async () => {
    renderReview();
    await walkthrough();
    const scroller = document.querySelector<HTMLElement>(".walk-scroll")!;
    Object.defineProperties(scroller, { scrollHeight: { value: 2000 }, clientHeight: { value: 800 }, scrollTop: { value: 1200, writable: true } });
    fireEvent.scroll(scroller);
    expect(screen.getByRole("group", { name: "Your decision" })).toBeInTheDocument();
  }, 30_000);

  it("shows the diff as before when the review wrote no overview", async () => {
    renderReview({ overview: null });
    expect(await screen.findByRole("region", { name: "Submit review" }, SLOW)).toBeInTheDocument();
    await screen.findByRole("button", { name: "Rerun review" }, SLOW);
    expect(screen.queryByRole("navigation", { name: "Walkthrough" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Show the review as" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Verdict" })).not.toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("New commits since this review");
  }, 30_000);

  it("tints a failed review's first section red, as the verdict banner would be", async () => {
    openReview("20310727T005510Z-109");
    const heading = await screen.findByRole("heading", { name: "Review failed" }, SLOW);
    expect(heading.closest("section")).toHaveAttribute("data-tone", "error");
    expect(heading.closest("section")).toHaveClass("verdict-banner");
  }, 30_000);
});

function renderReview(...args: Parameters<typeof openReview> extends [string, ...infer Rest] ? Rest : never) {
  return openReview(RUN, ...args);
}
