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

const thread = (place: string) => screen.findByRole("region", { name: `Draft comment on ${place}` }, SLOW);
const changes = (host: ReturnType<typeof renderWithFakeHost>) =>
  host.received.filter((m) => m.type === "change-draft").map((m) => (m.type === "change-draft" ? m.change : null));

describe("Whooper's page with the fake host", () => {
  it("lists the queue and opens a review that waits for the user", async () => {
    const host = renderWithFakeHost();
    const needsYou = await screen.findByRole("region", { name: "Needs you" });
    fireEvent.click(within(needsYou).getByRole("button", { name: /separate the cache configuration/ }));
    expect(await screen.findByRole("heading", { name: "refactor(cache): separate the cache configuration" })).toBeInTheDocument();
    expect(host.received.map((m) => m.type)).toEqual(["ready", "open-review"]);
    // The first file with a finding shows, with each finding under its line.
    expect(await thread("config_test.go:18")).toBeInTheDocument();
    expect(await thread("config_test.go:43")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to the queue" }));
    expect(await screen.findByRole("region", { name: "Needs you" })).toBeInTheDocument();
  }, 30_000);

  it("changes drafts only through the host and shows the drafts it answers with", async () => {
    const host = renderWithFakeHost({ openOnReady: RUN });
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
        { op: "leave-out", finding: "2" },
        { op: "text", finding: "2", text: "**nit:** A `depguard` rule would do." },
        { op: "comment" },
        { op: "message", text: "Thanks, this reads well." },
      ]),
    );
  }, 30_000);

  it("shows whooper-draft's refusal in the thread and keeps the editor open", async () => {
    renderWithFakeHost({ openOnReady: RUN });
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
    const host = renderWithFakeHost({ openOnReady: RUN, confirmSubmit });
    await thread("config_test.go:18");
    const bar = screen.getByRole("region", { name: "Submit review" });
    fireEvent.click(within(bar).getByRole("button", { name: "Submit review" }));
    expect(await within(bar).findByText("Not submitted: you cancelled the confirmation.")).toBeInTheDocument();

    fireEvent.click(await within(bar).findByRole("button", { name: "Submit review" }));
    expect(await within(bar).findByText(/posted the review: https:\/\/github.com\/example-org\/widgets\/pull\/101/)).toBeInTheDocument();
    expect(host.received.filter((m) => m.type === "submit")).toEqual([
      { type: "submit", run_id: RUN, version: 1, id: expect.any(Number) },
      { type: "submit", run_id: RUN, version: 1, id: expect.any(Number) },
    ]);
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

  it("keeps a half-typed draft when the diff rebuilds", async () => {
    const host = renderWithFakeHost({ openOnReady: RUN });
    const first = await thread("config_test.go:18");
    fireEvent.click(within(first).getByRole("button", { name: "Edit" }));
    const editor = within(first).getByRole("textbox", { name: "Comment" }) as HTMLTextAreaElement;
    fireEvent.input(editor, { target: { value: "**nit:** half-typed" } });
    const slot = first.closest("[data-annotation-slot]");

    // The host sends the review again, as when the run's files change: the patch is parsed again,
    // and Pierre builds every thread's element anew.
    host.resendReview(RUN);
    await waitFor(() => expect(slot).not.toBeInTheDocument(), SLOW);
    // A change from a terminal and a switch to one column rebuild it too.
    host.changeFromElsewhere(RUN, { op: "leave-out", finding: "2" });
    fireEvent.click(screen.getByRole("button", { name: "Unified" }));
    await waitFor(() => expect(within(screen.getByRole("region", { name: "Submit review" })).getByText("1 comment")).toBeInTheDocument());

    const again = await thread("config_test.go:18");
    expect(again).toBe(first);
    expect(within(again).getByRole("textbox", { name: "Comment" })).toBe(editor);
    expect(editor.value).toBe("**nit:** half-typed");
    expect(again.closest("[data-annotation-slot]")).not.toBe(slot);
  }, 30_000);

  it("asks the host about a finding and shows the answer under it", async () => {
    const host = renderWithFakeHost({ openOnReady: RUN });
    const nit = await thread("config_test.go:18");
    fireEvent.click(within(nit).getByRole("button", { name: "Ask about this" }));
    fireEvent.input(within(nit).getByRole("textbox", { name: "Question about this finding" }), {
      target: { value: "Does TestValidate cover the same bound?" },
    });
    fireEvent.click(within(nit).getByRole("button", { name: "Ask" }));
    expect(await within(nit).findByText(/stands in for whooper-ask/)).toBeInTheDocument();
    expect(within(nit).getByText("Does TestValidate cover the same bound?")).toBeInTheDocument();
    expect(host.received.find((m) => m.type === "ask")).toMatchObject({
      run_id: RUN,
      finding: "1",
      question: "Does TestValidate cover the same bound?",
    });
  }, 30_000);
});
