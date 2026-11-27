import { screen, waitFor, within } from "@testing-library/dom";
import { describe, expect, it } from "vitest";
import { NAME_RUN, nameExamples } from "../fixtures";
import type { Examples } from "../protocol";
import { fireEvent, follows, openReview, press, viewsButton, walkthrough } from "../test/review";

const NAME = "Example inputs, before and after";
const line = () => screen.queryByRole("group", { name: NAME });
/** Each row of a table as its cells' text. */
const cells = (table: HTMLElement) =>
  within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent));

async function renderReview(examples?: Examples | null) {
  openReview(NAME_RUN, examples === undefined ? {} : { examples });
  return walkthrough();
}

async function openAllInputs() {
  await renderReview();
  fireEvent.click(within(line()!).getByRole("button", { name: "Open all inputs" }));
  return screen.getByRole("region", { name: NAME });
}

describe("example inputs, before and after", () => {
  it("stays hidden when show-review prints no examples: no line and no item in Views", async () => {
    await renderReview(null);
    expect(line()).not.toBeInTheDocument();
    fireEvent.click(await viewsButton());
    await screen.findByRole("menuitemcheckbox", { name: /^How the changed code connects/ });
    expect(screen.queryByRole("menuitemcheckbox", { name: new RegExp(NAME) })).not.toBeInTheDocument();
  }, 30_000);

  it("names the inputs whose result changed in one line under What this PR does, from rows run at base and head only", async () => {
    await renderReview();
    const found = line()!;
    const what = screen.getByRole("region", { name: "What this PR does" });
    expect(what).toContainElement(found);
    expect(follows(what.querySelector("table")!, found)).toBe(true);
    // ValidateName's "a--b" differs too, but only the head ran it, so nothing was compared.
    expect(found).toHaveTextContent(`${NAME}projects.Create gives a different result for "a b". Ran at base and head.`);
    expect(within(found).getByRole("button", { name: "Open all inputs" })).toBeInTheDocument();
  }, 30_000);

  it("names each function's changed inputs, and says when no result changed or nothing could be compared", async () => {
    const { functions, rows } = nameExamples();
    const clamp: Examples["rows"][number] = { function: "limits.Clamp", input: "101", before: "101", after: "100", status: "different", label: "ran_base_and_head", note: null };
    await renderReview({ functions, rows: [...rows, { ...rows[1], input: '"ab"' }, clamp], limits: [] });
    expect(line()).toHaveTextContent('projects.Create gives a different result for "a b" and "ab", and limits.Clamp for 101.');
  }, 30_000);

  it("says no result changed for the inputs run at base and head", async () => {
    await renderReview({ ...nameExamples(), rows: nameExamples().rows.filter((row) => row.status !== "different") });
    expect(line()).toHaveTextContent(`${NAME}No result changed for the 3 inputs run at base and head.`);
  }, 30_000);

  it("says when no input ran at both base and head", async () => {
    await renderReview({ ...nameExamples(), rows: nameExamples().rows.filter((row) => row.label !== "ran_base_and_head") });
    expect(line()).toHaveTextContent(`${NAME}No input ran at both base and head, so none could be compared.`);
  }, 30_000);

  it("opens a table per function in the walkthrough's place: changed first, each result compared in words, and how each row ran", async () => {
    const view = await openAllInputs();
    expect(document.querySelector(".walkthrough")).toHaveAttribute("hidden");
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: NAME }));
    const create = within(view).getByRole("region", { name: "projects.Create internal/projects/create.go:10" });
    expect(within(create).getAllByRole("columnheader").map((header) => header.textContent)).toEqual(["Input", "Before", "After", "Compared", "How it ran"]);
    expect(cells(within(create).getByRole("table"))).toEqual([
      ['"a b"', '&Project{Name: "a b", Bucket: "a b"}', 'error: name "a b" can\'t name a bucket', "changed", "Ran at base and head"],
      ['"abc"', '&Project{Name: "abc", Bucket: "abc"}', '&Project{Name: "abc", Bucket: "abc"}', "same", "Ran at base and head"],
      ['"x"', '&Project{Name: "x"}', '&Project{Name: "x"}', "varies between runs", "Ran at base and head · the two runs at base gave different results"],
      ['""', "–none", "–none", "run failed", "Ran at base and head · the run at base timed out"],
    ]);
    // Only a result that changed between base and head is orange.
    expect(within(create).getByText("changed")).toHaveAttribute("data-changed");
    expect(within(create).getByText("same")).not.toHaveAttribute("data-changed");

    const validate = within(view).getByRole("region", { name: "projects.ValidateName internal/projects/name.go:9" });
    // Same and different compare two runs, so a row that ran at the head only, or neither, has none.
    expect(cells(within(validate).getByRole("table"))).toEqual([
      ['"a--b"', "–none", "nil", "–nothing to compare", "Ran at head only · new in this PR"],
      ['"Q3"', "–none", "–none", "–nothing to compare", "Not compared · the test didn't build"],
    ]);
    const limits = within(view).getByRole("region", { name: "What these runs may miss" });
    expect(within(limits).getAllByRole("listitem").map((item) => item.textContent)).toEqual(nameExamples().limits);
  }, 30_000);

  it("steps through the inputs with j and k, and Escape clears the current one, then goes back with the focus on Open all inputs", async () => {
    const view = await openAllInputs();
    const current = () => [...view.querySelectorAll('tr[aria-current="true"]')].map((row) => row.querySelector("td")!.textContent);
    press("j");
    expect(current()).toEqual(['"a b"']);
    press("j");
    expect(current()).toEqual(['"abc"']);
    press("k");
    press("k");
    // Around the start to the last input, in the second table.
    expect(current()).toEqual(['"Q3"']);

    press("Escape");
    expect(current()).toEqual([]);
    expect(screen.getByRole("region", { name: NAME })).toBe(view);
    press("Escape");
    expect(document.querySelector(".walkthrough")).not.toHaveAttribute("hidden");
    expect(document.activeElement).toBe(within(line()!).getByRole("button", { name: "Open all inputs" }));
  }, 30_000);

  it("goes back to the walkthrough when Views turns the view off while it shows", async () => {
    await openAllInputs();
    fireEvent.click(await viewsButton());
    fireEvent.click(await screen.findByRole("menuitemcheckbox", { name: new RegExp(`^${NAME}`) }));
    await waitFor(() => expect(document.querySelector(".walkthrough")).not.toHaveAttribute("hidden"));
    expect(line()).not.toBeInTheDocument();
  }, 30_000);
});
