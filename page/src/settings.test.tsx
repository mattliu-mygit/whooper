import { fireEvent as dom, screen, waitFor, within } from "@testing-library/dom";
import { flush } from "solid-js";
import { describe, expect, it } from "vitest";
import * as fixtures from "./fixtures";
import { appliesWhen } from "./Settings";
import { boundsProblem } from "./settingsForm";
import { renderWithFakeHost } from "./test/connect";

// Solid applies updates in a microtask, so each event is followed by flush() to see its effect.
const fireEvent = {
  click: (element: Element) => (dom.click(element), flush()),
  input: (element: Element, value: string) => (dom.input(element, { target: { value } }), flush()),
  change: (element: Element, init: object) => (dom.change(element, init), flush()),
};

const field = (name: string | RegExp, group?: string) =>
  (group ? within(screen.getByRole("group", { name: group })) : screen).getByLabelText(name) as HTMLInputElement;
const save = () => screen.getByRole("button", { name: "Save" });

async function openSettings(settings?: Record<string, unknown>) {
  const host = renderWithFakeHost({ openOnReady: "/", settings });
  await screen.findByRole("list", { name: "Reviews" });
  fireEvent.click(within(screen.getByRole("navigation", { name: "Whooper" })).getByRole("button", { name: "Settings" }));
  expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
  await screen.findByRole("form", { name: "Whooper settings" });
  await waitFor(() => expect(field("Poll every")).toHaveValue(15));
  return host;
}

describe("the Settings tab", () => {
  it("shows the settings get-settings prints, in the schema's groups, at /settings", async () => {
    const host = await openSettings();
    expect(within(screen.getByRole("navigation", { name: "Whooper" })).getByRole("button", { name: "Settings" })).toHaveAttribute("aria-current", "page");
    expect(field("Start")).toHaveValue("10:00");
    expect(field("Mon")).toBeChecked();
    expect(field("Sat")).not.toBeChecked();
    expect(field("Review agent")).toHaveValue("claude");
    expect(field("Review model", "Devin")).toHaveValue("claude-opus-4-high");
    expect(field("Review skills")).toHaveValue("code-review");
    // Left out of config.toml with no default, so it shows empty and saves nothing.
    expect(field("Most one review may spend")).toHaveValue(null);
    expect(save()).toBeDisabled();
    expect(host.calls("get-settings")).toEqual([[]]);
  });

  it("says inline why a value would be refused, and saves nothing until none is", async () => {
    const host = await openSettings();
    fireEvent.input(field("Poll every"), "0");
    expect(screen.getByText("Enter 1 or more.")).toBeInTheDocument();
    expect(field("Poll every")).toHaveAttribute("aria-invalid", "true");
    expect(save()).toBeDisabled();
    fireEvent.input(field("Poll every"), "");
    expect(screen.getByText("Enter a value. To leave it out, delete its line in config.toml.")).toBeInTheDocument();
    fireEvent.input(field("Poll every"), "7.5");
    expect(screen.getByText("Enter a whole number.")).toBeInTheDocument();
    fireEvent.input(field("Poll every"), "20");

    fireEvent.input(field("End"), "9:30");
    expect(screen.getByText("Enter a time after the start.")).toBeInTheDocument();
    fireEvent.input(field("End"), "6pm");
    expect(screen.getByText("Enter a 24-hour time such as 18:00.")).toBeInTheDocument();
    fireEvent.input(field("End"), "18:30");

    for (const day of ["Mon", "Tue", "Wed", "Thu", "Fri"]) fireEvent.click(field(day));
    expect(screen.getByText("Choose at least one day.")).toBeInTheDocument();
    expect(save()).toBeDisabled();
    fireEvent.click(field("Sat"));
    expect(screen.queryByText("Choose at least one day.")).not.toBeInTheDocument();
    expect(save()).toBeEnabled();
    expect(host.calls("save-settings")).toEqual([]);
  });

  it("refuses an answer limit of 0 or less inline, in whooper-settings set's words, and saves one above 0", async () => {
    // schemas/settings.json gives [ask] timeout_minutes exclusiveMinimum 0, which whooper-settings set
    // refuses 0 by with "settings.ask.timeout_minutes is not above 0".
    const host = await openSettings();
    for (const value of ["0", "-1"]) {
      fireEvent.input(field("Stop an answer after"), value);
      expect(screen.getByText("Enter a number above 0.")).toBeInTheDocument();
      expect(field("Stop an answer after")).toHaveAttribute("aria-invalid", "true");
      expect(save()).toBeDisabled();
    }
    fireEvent.input(field("Stop an answer after"), "0.5");
    expect(screen.queryByText("Enter a number above 0.")).not.toBeInTheDocument();
    expect(host.calls("save-settings")).toEqual([]);
    fireEvent.click(save());
    await screen.findByText(/^Saved\./);
    expect(host.calls("save-settings")).toEqual([[JSON.stringify({ ask: { timeout_minutes: 0.5 } })]]);
  });

  it("words each bound a number setting may have as whooper-settings set refuses it", () => {
    expect(boundsProblem(0, { minimum: 1 })).toBe("Enter 1 or more.");
    expect(boundsProblem(0, { exclusiveMinimum: 0 })).toBe("Enter a number above 0.");
    // The schema sets no maximum yet.
    expect(boundsProblem(61, { maximum: 60 })).toBe("Enter 60 or less.");
    expect(boundsProblem(60, { exclusiveMaximum: 60 })).toBe("Enter a number below 60.");
    expect(boundsProblem(1, { minimum: 1, exclusiveMinimum: 0, maximum: 1, exclusiveMaximum: 2 })).toBeNull();
  });

  it("saves only the changed settings with save-settings, reads them again, and says when they apply", async () => {
    const host = await openSettings();
    fireEvent.input(field("Poll every"), "30");
    fireEvent.change(field("Review agent"), { target: { value: "devin" } });
    fireEvent.input(field("Review skills"), "code-review\nsecurity\n");
    fireEvent.click(save());
    const note = await screen.findByText(/^Saved\./);
    expect(host.calls("save-settings")).toEqual([
      [JSON.stringify({ schedule: { interval_minutes: 30 }, jobs: { review: "devin" }, review: { skills: ["code-review", "security"] } })],
    ]);
    expect(note.textContent).toMatch(/^Saved\. The round already set for .+ still starts then; the new schedule times the rounds after it\. Each poll and review that starts from now on uses the new settings; one already running keeps the old ones\.$/);
    expect(host.calls("get-settings")).toEqual([[], []]);
    expect(field("Poll every")).toHaveValue(30);
    expect(field("Review agent")).toHaveValue("devin");
    expect(save()).toBeDisabled();
  });

  it("keeps each choice shown as edits redraw its options, and drops Not set once a choice is made", async () => {
    const settings = fixtures.settings();
    settings.jobs = { review: "devin" };
    await openSettings(settings);
    const options = (name: string) => [...field(name).querySelectorAll("option")].map((option) => option.textContent);
    expect(field("Review agent")).toHaveValue("devin");
    expect(field("Poll agent")).toHaveValue("");
    expect(options("Poll agent")).toEqual(["Not set", "Claude", "Devin"]);
    fireEvent.input(field("Poll every"), "30");
    expect(field("Review agent")).toHaveValue("devin");
    expect(field("Poll agent")).toHaveValue("");
    fireEvent.change(field("Poll agent"), { target: { value: "devin" } });
    expect(options("Poll agent")).toEqual(["Claude", "Devin"]);
    expect(field("Poll agent")).toHaveValue("devin");
    expect(field("Review agent")).toHaveValue("devin");
  });

  it("reverts unsaved changes", async () => {
    await openSettings();
    fireEvent.input(field("Stop an answer after"), "3");
    fireEvent.click(screen.getByRole("button", { name: "Revert" }));
    expect(field("Stop an answer after")).toHaveValue(10);
    expect(save()).toBeDisabled();
  });

  it("words when each kind of change applies", () => {
    expect(appliesWhen({ ask: { timeout_minutes: 5 } }, null)).toBe("Saved. The next question you ask uses the new time limit.");
    expect(appliesWhen({ schedule: { start: "09:00" } }, null)).toBe("Saved. The new schedule times the next round whooper-run sets.");
  });
});
