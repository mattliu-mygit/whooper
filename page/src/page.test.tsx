import { fireEvent as dom, screen, waitFor, within } from "@testing-library/dom";
import { flush } from "solid-js";
import { describe, expect, it, vi } from "vitest";
import { renderWithFakeHost } from "./test/connect";

const RUN = "20310727T010246Z-101";

// Solid applies updates in a microtask, so each event is followed by flush() to see its effect.
const fireEvent = {
  click: (element: Element) => (dom.click(element), flush()),
  input: (element: Element, init: object) => (dom.input(element, init), flush()),
  change: (element: Element, init: object) => (dom.change(element, init), flush()),
};
// Pierre loads Shiki and the Go grammar before it draws the threads.
const SLOW = { timeout: 15_000 };

const OPEN = { openOnReady: `/reviews/${RUN}` };

const thread = (place: string) => screen.findByRole("region", { name: `Draft comment on ${place}` }, SLOW);
// The changes the page asked whooper-draft for; a call with the run ID alone only reads the drafts.
const changes = (host: ReturnType<typeof renderWithFakeHost>) => host.calls("draft").filter((args) => args.length > 1);

describe("Whooper's page with the fake host", () => {
  it("shows the review Prompting Time opened, read with calls of Whooper's actions", async () => {
    const host = renderWithFakeHost(OPEN);
    expect(await screen.findByRole("heading", { name: "refactor(cache): separate the cache configuration" })).toBeInTheDocument();
    // Prompting Time lists the reviews, so the page reads only the status and the review it opened.
    expect(host.received.filter((m) => m.type === "ready")).toHaveLength(1);
    expect(host.calls("show-review")).toEqual([[RUN]]);
    expect(host.calls("status")).toEqual([[]]);
    expect(screen.queryByRole("button", { name: "Back to the queue" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Needs you" })).not.toBeInTheDocument();
    // The first file with a finding shows, with each finding under its line.
    expect(await thread("config_test.go:18")).toBeInTheDocument();
    expect(await thread("config_test.go:43")).toBeInTheDocument();
  }, 30_000);

  it("shows nothing and reads nothing before Prompting Time opens a route", async () => {
    const host = renderWithFakeHost();
    await waitFor(() => expect(host.received.map((m) => m.type)).toEqual(["ready"]));
    await new Promise((resolve) => setTimeout(resolve, 0));
    flush();
    expect(screen.queryByRole("main", { name: "Whooper's reviews" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Needs you" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("changes drafts only through the host and shows the drafts it answers with", async () => {
    const host = renderWithFakeHost(OPEN);
    const nit = await thread("config_test.go:43");
    const bar = screen.getByRole("region", { name: "Submit review" });
    // A safe run starts as an approval with every nit included.
    expect(within(bar).getByText("2 comments")).toBeInTheDocument();
    expect(within(bar).getByRole("radio", { name: "Approve" })).toBeChecked();
    expect(within(bar).getByRole("textbox", { name: "Review message" })).toHaveAttribute("placeholder", "Pre-approving to unblock");

    fireEvent.click(within(nit).getByRole("checkbox", { name: "Include in review" }));
    await waitFor(() => expect(within(bar).getByText("1 comment")).toBeInTheDocument());
    expect(within(nit).getByText(/left out/)).toBeInTheDocument();

    fireEvent.click(within(nit).getByRole("button", { name: "Edit" }));
    fireEvent.input(within(nit).getByRole("textbox", { name: "Comment" }), { target: { value: "**nit:** A `depguard` rule would do." } });
    fireEvent.click(within(nit).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(nit).queryByRole("textbox", { name: "Comment" })).not.toBeInTheDocument());
    expect(within(nit).getByText("depguard", { selector: "code" })).toBeInTheDocument();

    fireEvent.click(within(bar).getByRole("radio", { name: "Comment" }));
    await waitFor(() => expect(within(bar).getByRole("textbox", { name: "Review message" })).toHaveAttribute("placeholder", "Message (optional)"));
    const message = within(bar).getByRole("textbox", { name: "Review message" });
    fireEvent.input(message, { target: { value: "Thanks, this reads well." } });
    fireEvent.change(message, { target: { value: "Thanks, this reads well." } });

    await waitFor(() =>
      expect(changes(host)).toEqual([
        [RUN, "leave-out", "2"],
        [RUN, "text", "2", "**nit:** A `depguard` rule would do."],
        [RUN, "comment"],
        [RUN, "message", "Thanks, this reads well."],
      ]),
    );
  }, 30_000);

  it("shows whooper-draft's refusal in the thread and keeps the editor open", async () => {
    renderWithFakeHost(OPEN);
    const nit = await thread("config_test.go:18");
    fireEvent.click(within(nit).getByRole("button", { name: "Edit" }));
    fireEvent.input(within(nit).getByRole("textbox", { name: "Comment" }), { target: { value: "  " } });
    fireEvent.click(within(nit).getByRole("button", { name: "Save" }));
    expect(await within(nit).findByRole("alert")).toHaveTextContent(
      "whooper-draft: the text is empty; to drop the comment, leave the finding out",
    );
    expect(within(nit).getByRole("textbox", { name: "Comment" })).toHaveValue("  ");
  }, 30_000);

  it("asks the host to submit, which confirms first, and locks the review once it posted", async () => {
    const confirmSubmit = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const host = renderWithFakeHost({ ...OPEN, confirmSubmit });
    await thread("config_test.go:18");
    const bar = screen.getByRole("region", { name: "Submit review" });
    fireEvent.click(within(bar).getByRole("button", { name: "Submit review" }));
    expect(await within(bar).findByRole("alert")).toHaveTextContent("Nothing ran: the confirmation was cancelled.");

    fireEvent.click(await within(bar).findByRole("button", { name: "Submit review" }));
    expect(await within(bar).findByText(/posted the review: https:\/\/github.com\/example-org\/widgets\/pull\/101/)).toBeInTheDocument();
    // The page passes the run ID alone; Prompting Time adds --print and --expect for its confirmation.
    expect(host.calls("submit")).toEqual([[RUN], [RUN]]);
    // The confirmation showed whooper-submit's request: both nits, as one approval.
    const shown = confirmSubmit.mock.calls[1][0] as string;
    const request = JSON.parse(shown.slice(shown.indexOf("{")));
    expect(request.event).toBe("APPROVE");
    expect(request.body).toBe("Pre-approving to unblock");
    expect(request.comments.map((c: { line: number }) => c.line)).toEqual([18, 43]);
    expect(request.comments[0].body).toMatch(/^AI comment \(vetted\)\n\n\*\*nit:\*\* /);
    expect(await within(bar).findByRole("button", { name: "Submit review" })).toBeDisabled();
    expect(within(await thread("config_test.go:18")).getByRole("checkbox", { name: "Include in review" })).toBeDisabled();
  }, 30_000);

  it("sends the @-mentions whooper-submit refused once the user presses Send with the mentions", async () => {
    const confirmSubmit = vi.fn().mockResolvedValue(true);
    const host = renderWithFakeHost({ ...OPEN, confirmSubmit });
    const nit = await thread("config_test.go:18");
    fireEvent.click(within(nit).getByRole("button", { name: "Edit" }));
    fireEvent.input(within(nit).getByRole("textbox", { name: "Comment" }), { target: { value: "**nit:** @sam owns this test." } });
    fireEvent.click(within(nit).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(nit).queryByRole("textbox", { name: "Comment" })).not.toBeInTheDocument());
    const bar = screen.getByRole("region", { name: "Submit review" });
    // Nothing shows until a submit comes back refused.
    expect(within(bar).queryByRole("button", { name: "Send with the mentions" })).not.toBeInTheDocument();

    fireEvent.click(within(bar).getByRole("button", { name: "Submit review" }));
    // whooper-submit's stderr is the reason, and the refusal is not repeated as an error.
    expect(await within(bar).findByText(/the review @-mentions someone, and posting would notify them/)).toBeInTheDocument();
    expect(within(bar).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(bar).queryByRole("button", { name: "Approve anyway" })).not.toBeInTheDocument();
    fireEvent.click(within(bar).getByRole("button", { name: "Send with the mentions" }));
    expect(await within(bar).findByText(/posted the review/)).toBeInTheDocument();
    expect(host.refusals).toEqual([12]);
    expect(host.calls("submit")).toEqual([[RUN], [RUN, "--allow-mentions"]]);
    expect(confirmSubmit).toHaveBeenCalledTimes(1);
    expect(within(bar).queryByRole("button", { name: "Send with the mentions" })).not.toBeInTheDocument();
  }, 30_000);

  it("approves anyway, then sends the mentions, each after its own refusal", async () => {
    const newest = "20310727T011502Z-107";
    const host = renderWithFakeHost({ openOnReady: `/reviews/${newest}`, confirmSubmit: () => true });
    const bar = await screen.findByRole("region", { name: "Submit review" }, SLOW);
    // The run has a question, so it is not safe to approve, and its drafts start as a comment.
    fireEvent.click(within(bar).getByRole("radio", { name: "Approve" }));
    await waitFor(() => expect(within(bar).getByRole("radio", { name: "Approve" })).toBeChecked());
    const message = within(bar).getByRole("textbox", { name: "Review message" });
    fireEvent.input(message, { target: { value: "Thanks @jordan" } });
    fireEvent.change(message, { target: { value: "Thanks @jordan" } });
    await waitFor(() => expect(changes(host)).toContainEqual([newest, "message", "Thanks @jordan"]));

    fireEvent.click(within(bar).getByRole("button", { name: "Submit review" }));
    expect(await within(bar).findByText(/the run is not safe to approve/)).toBeInTheDocument();
    fireEvent.click(within(bar).getByRole("button", { name: "Approve anyway" }));
    fireEvent.click(await within(bar).findByRole("button", { name: "Send with the mentions" }));
    expect(await within(bar).findByText(/posted the review/)).toBeInTheDocument();
    expect(host.refusals).toEqual([11, 12]);
    expect(host.calls("submit")).toEqual([[newest], [newest, "--approve-anyway"], [newest, "--approve-anyway", "--allow-mentions"]]);
  }, 30_000);

  it("keeps a half-typed draft when the diff rebuilds", async () => {
    const host = renderWithFakeHost(OPEN);
    const first = await thread("config_test.go:18");
    fireEvent.click(within(first).getByRole("button", { name: "Edit" }));
    const editor = within(first).getByRole("textbox", { name: "Comment" }) as HTMLTextAreaElement;
    fireEvent.input(editor, { target: { value: "**nit:** half-typed" } });
    const slot = first.closest("[data-annotation-slot]");

    // A file of the run changed, so the page reads the review again: the patch is parsed again, and
    // Pierre builds every thread's element anew.
    host.changed([`runs/${RUN}/findings.json`]);
    await waitFor(() => expect(slot).not.toBeInTheDocument(), SLOW);
    // A change from a terminal, which the page reads as the drafts alone, and a switch to one column.
    host.changeFromElsewhere(RUN, "leave-out", "2");
    fireEvent.click(screen.getByRole("button", { name: "Unified" }));
    await waitFor(() => expect(within(screen.getByRole("region", { name: "Submit review" })).getByText("1 comment")).toBeInTheDocument());

    const again = await thread("config_test.go:18");
    expect(again).toBe(first);
    expect(within(again).getByRole("textbox", { name: "Comment" })).toBe(editor);
    expect(editor.value).toBe("**nit:** half-typed");
    expect(again.closest("[data-annotation-slot]")).not.toBe(slot);
  }, 30_000);

  it("asks the host about a finding and shows the answer under it", async () => {
    const host = renderWithFakeHost(OPEN);
    const nit = await thread("config_test.go:18");
    fireEvent.click(within(nit).getByRole("button", { name: "Ask about this" }));
    fireEvent.input(within(nit).getByRole("textbox", { name: "Question about this finding" }), {
      target: { value: "Does TestValidate cover the same bound?" },
    });
    fireEvent.click(within(nit).getByRole("button", { name: "Ask" }));
    expect(await within(nit).findByText(/stands in for whooper-ask/)).toBeInTheDocument();
    expect(within(nit).getByText("Does TestValidate cover the same bound?")).toBeInTheDocument();
    expect(host.calls("ask")).toEqual([[RUN, "Does TestValidate cover the same bound?", "--finding", "1"]]);
  }, 30_000);

  it("skips and removes the review from its header, and undoes either", async () => {
    const host = renderWithFakeHost(OPEN);
    await thread("config_test.go:18");
    fireEvent.click(await screen.findByRole("button", { name: "Skip" }));
    expect(await screen.findByText("Skipped")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(await screen.findByRole("button", { name: "Skip" })).toBeInTheDocument();
    expect(host.calls("skip")).toEqual([[RUN], [RUN, "--undo"]]);

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    const banner = await screen.findByRole("note");
    expect(banner).toHaveTextContent("Removed from your lists");
    // A removed review can't be skipped or removed again, only restored.
    expect(screen.queryByRole("button", { name: "Skip" })).not.toBeInTheDocument();
    fireEvent.click(within(banner).getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(screen.queryByRole("note")).not.toBeInTheDocument());
    expect(host.calls("remove")).toEqual([[RUN], [RUN, "--undo"]]);
  }, 30_000);

  it("skips a review that still runs, which stops it", async () => {
    const running = "20310727T014000Z-120";
    const host = renderWithFakeHost({ openOnReady: `/reviews/${running}` });
    expect(await screen.findByRole("heading", { name: "Reviewing…" })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Skip" }));
    expect(await screen.findByText("Skipped")).toBeInTheDocument();
    expect(host.calls("skip")).toEqual([[running]]);
    // Remove still waits for the review to end.
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
  });

  it("shows Whooper's home at /: the agent, what stops every review, and the reviews by group", async () => {
    const host = renderWithFakeHost({ openOnReady: "/" });
    expect(await screen.findByRole("heading", { name: "Whooper" })).toBeInTheDocument();
    expect(screen.getByText("Can't review · Docker isn't running")).toBeInTheDocument();
    expect(host.calls("status")).toEqual([[]]);
    expect(host.calls("show-review")).toEqual([]);
    // What stops every review first, then the groups that need the user, then the rest; Done has nothing yet.
    expect(screen.getAllByRole("list").map((list) => list.getAttribute("aria-label"))).toEqual([
      "Stops every review",
      "Ready to approve",
      "Needs a look",
      "Review failed",
      "Reviewing",
      "Waiting",
    ]);
    expect(screen.getByRole("heading", { name: "Needs a look · 1" })).toBeInTheDocument();
    expect(screen.queryByText(/^Done/)).not.toBeInTheDocument();

    fireEvent.click(within(screen.getByRole("list", { name: "Ready to approve" })).getByRole("button", { name: /refactor\(cache\)/ }));
    await thread("config_test.go:18");
    fireEvent.click(await screen.findByRole("button", { name: "Skip" }));
    expect(await screen.findByText("Skipped")).toBeInTheDocument();

    host.open("/");
    const done = (await screen.findByText("Done · 1")).closest("details") as HTMLDetailsElement;
    // Done stays folded until the user opens it.
    expect(done.open).toBe(false);
    expect(within(done).getByRole("button", { name: /refactor\(cache\)/ })).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Ready to approve" })).not.toBeInTheDocument();

    fireEvent.click(within(screen.getByRole("list", { name: "Stops every review" })).getByRole("button", { name: /Docker isn't running/ }));
    expect(await screen.findByRole("list", { name: "Reviews it holds back" })).toBeInTheDocument();
  }, 30_000);

  it("shows a failed review: why it failed, earlier answers, Ask why, and Retry review", async () => {
    const failed = "20310727T005510Z-109";
    const host = renderWithFakeHost({ openOnReady: `/reviews/${failed}` });
    expect(await screen.findByRole("heading", { name: "Review failed" })).toBeInTheDocument();
    expect(screen.getByText("The review agent left no usable findings")).toBeInTheDocument();
    expect(screen.getByLabelText("Why it failed")).toHaveTextContent("the review run exited 0 without findings");
    expect(screen.getByText("Why did this review fail?")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Ask why" }));
    fireEvent.input(screen.getByRole("textbox", { name: "Question" }), { target: { value: "Did it run out of time?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText(/about: Did it run out of time\?/)).toBeInTheDocument();
    expect(host.calls("ask")).toEqual([[failed, "Did it run out of time?"]]);

    fireEvent.click(screen.getByRole("button", { name: "Retry review" }));
    const queued = await screen.findByRole("status");
    expect(queued).toHaveTextContent("queued another review of sample-org/gadgets#109");
    fireEvent.click(within(queued).getByRole("button", { name: "Poll now" }));
    await waitFor(() => expect(host.calls("round")).toEqual([[]]));
    expect(host.calls("retry")).toEqual([[failed]]);
  });

  it("links a review that waits to the cause that stops every review, and the cause back to it", async () => {
    const waits = "20310727T013000Z-115";
    renderWithFakeHost({ openOnReady: `/reviews/${waits}` });
    expect(await screen.findByRole("heading", { name: "Waiting · Docker isn't running" })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "See all 1 review waiting" }));
    expect(await screen.findByRole("heading", { name: "Docker isn't running" })).toBeInTheDocument();
    const held = screen.getByRole("list", { name: "Reviews it holds back" });
    fireEvent.click(within(held).getByRole("button", { name: /example-org\/widgets#115/ }));
    expect(await screen.findByRole("heading", { name: "Waiting · Docker isn't running" })).toBeInTheDocument();
  });

  it("opens each route Prompting Time sends, such as a blocker's", async () => {
    const host = renderWithFakeHost(OPEN);
    await thread("config_test.go:18");
    host.open("/causes/docker_unavailable");
    expect(await screen.findByRole("heading", { name: "Docker isn't running" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Poll now" }));
    await waitFor(() => expect(host.calls("round")).toEqual([[]]));
  });

  it("shows an earlier version read-only, and new commits since the newest", async () => {
    renderWithFakeHost({ openOnReady: "/reviews/20310726T120000Z-107" });
    const note = await screen.findByText(/Review 1 of 2, read-only/);
    expect(screen.queryByRole("button", { name: "Skip" })).not.toBeInTheDocument();
    expect(await within(screen.getByRole("region", { name: "Submit review" })).findByRole("button", { name: "Submit review" })).toBeDisabled();
    fireEvent.click(within(note.closest("[role=note]") as HTMLElement).getByRole("button", { name: "Open review 2" }));
    const commits = (await screen.findByText("New commits since this review")).closest("[role=note]");
    expect(commits).toHaveTextContent("it reviewed f1abd4c; the pull request is at c83d20e.");
    expect(screen.getByRole("button", { name: "Rerun review" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Versions of this review" })).toHaveValue("20310727T011502Z-107");
  }, 30_000);

  it("reads the review again only when one of its run's files changed", async () => {
    const host = renderWithFakeHost(OPEN);
    await thread("config_test.go:18");
    host.changed(["poll/github.json"]);
    await waitFor(() => expect(host.calls("status")).toHaveLength(2));
    // Nothing of the run changed, so the page reads neither the review nor its drafts.
    expect([host.calls("show-review").length, host.calls("draft").length]).toEqual([1, 0]);
    host.changed(null);
    await waitFor(() => expect(host.calls("show-review")).toHaveLength(2));
  });
});
