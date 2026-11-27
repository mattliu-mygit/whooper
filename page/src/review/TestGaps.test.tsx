import { screen, waitFor, within } from "@testing-library/dom";
import { describe, expect, it, vi } from "vitest";
import { NAME_RUN, nameTestGaps } from "../fixtures";
import type { TestGaps } from "../protocol";
import { render } from "../test/render";
import { fireEvent, follows, openReview, press, SLOW, viewsButton, walkthrough } from "../test/review";
import { countsText, lineList, TestGapsView } from "./TestGaps";

const NAME = "Changes no test would catch";
const block = () => screen.queryByRole("region", { name: NAME });
/** The first place to look, create.go's lines 11 to 13, or place `number` counting from 1; the second is name.go's line 6. */
const place = (number = 1) => document.querySelectorAll<HTMLElement>(".walk-places li")[number - 1];
/** test_gaps as Whooper printed it before it changed lines on purpose: only the lines no test ran, and no counts of changes. */
const notRunOnly = (): TestGaps => ({
  ...nameTestGaps(),
  lines: [
    { path: "internal/projects/create.go", line: 11, status: "not_run" },
    { path: "internal/projects/create.go", line: 12, status: "not_run" },
    { path: "internal/projects/create.go", line: 14, status: "not_run" },
    { path: "internal/projects/name.go", line: 10, status: "not_run" },
  ],
  summary: { changed_lines: 15, run: 2, not_run: 4 },
});
const codeIn = (element: Element) => [...element.querySelectorAll("code")].map((code) => code.textContent);

async function renderReview(testGaps?: TestGaps | null) {
  openReview(NAME_RUN, testGaps === undefined ? {} : { test_gaps: testGaps });
  return walkthrough();
}

async function openAllLines(testGaps?: TestGaps) {
  await renderReview(testGaps);
  fireEvent.click(within(block()!).getByRole("button", { name: "Open all lines" }));
  return screen.getByRole("region", { name: NAME });
}

describe("changes no test would catch", () => {
  it("stays hidden when show-review prints no test_gaps: nothing at the places, no block, and no item in Views", async () => {
    await renderReview(null);
    expect(block()).not.toBeInTheDocument();
    expect(place()).not.toHaveTextContent("No test ran");
    fireEvent.click(await viewsButton());
    await screen.findByRole("menuitemcheckbox", { name: /^How the changed code connects/ });
    expect(screen.queryByRole("menuitemcheckbox", { name: new RegExp(NAME) })).not.toBeInTheDocument();
  }, 30_000);

  it("names a place's changed lines no test ran under it, labeled as measured, and the lines elsewhere after the places", async () => {
    await renderReview(notRunOnly());
    expect(place()).toHaveTextContent("No test ran 2 changed lines here: 11, 12");
    expect(place()).toHaveTextContent("Measured: no test ran these lines · go test -coverprofile");

    const found = block()!;
    expect(follows(place(), found)).toBe(true);
    expect(follows(found, screen.getByRole("heading", { name: "What Whooper checked and didn't" }))).toBe(true);
    expect(found).toHaveTextContent("Tests ran 2 of 15 changed lines, and no test ran 4. The run counted the other 9 as neither, such as comments.");
    expect(found).toHaveTextContent("Outside the places to look, no test ran 2 changed lines:");
    expect(within(found).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["internal/projects/create.go 14", "internal/projects/name.go 10"]);
    expect(found).toHaveTextContent("Measured: no test ran these lines · go test -coverprofile");

    // The place's reason under its lines says it too.
    press("j");
    await waitFor(() => expect(document.querySelector(".place-card")).toHaveTextContent("No test ran 2 changed lines here: 11, 12"), SLOW);
    expect(document.querySelector(".place-card")).toHaveTextContent("Measured: no test ran these lines");
  }, 30_000);

  it("says one line, labeled this line, for a place with one, counting only lines of the place's own file", async () => {
    const lines: TestGaps["lines"] = [
      { path: "internal/projects/create.go", line: 12, status: "not_run" },
      { path: "internal/projects/name.go", line: 13, status: "not_run" },
    ];
    await renderReview({ ...nameTestGaps(), lines, summary: { changed_lines: 15, run: 5, not_run: 2 } });
    // name.go's line 13 has the number of a line in the place, create.go's 11 to 13, but not its file.
    expect(place()).toHaveTextContent("No test ran 1 changed line here: 12");
    expect(place()).toHaveTextContent("Measured: no test ran this line");
    expect(within(block()!).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["internal/projects/name.go 13"]);
    expect(block()).toHaveTextContent("Measured: no test ran this line");
  }, 30_000);

  it("says only the counts when no line is outside the places", async () => {
    await renderReview({ ...nameTestGaps(), lines: [{ path: "internal/projects/create.go", line: 12, status: "not_run" }], summary: { changed_lines: 15, run: 5, not_run: 1 } });
    expect(block()).toHaveTextContent("Tests ran 5 of 15 changed lines, and no test ran 1.");
    expect(block()).toHaveTextContent("Measured · go test -coverprofile");
    expect(within(block()!).queryByRole("listitem")).not.toBeInTheDocument();
  }, 30_000);

  it("names each change no test noticed at its place after the place's lines no test ran, labeled as measured, and opens both with j", async () => {
    const lines: TestGaps["lines"] = [
      { path: "internal/projects/create.go", line: 12, status: "not_run" },
      { path: "internal/projects/create.go", line: 11, status: "survived", change: "`err != nil` to `err == nil`" },
      { path: "internal/projects/create.go", line: 14, status: "not_run" },
      { path: "internal/projects/name.go", line: 6, status: "survived", change: "`{1,61}` to `{1,62}`" },
    ];
    await renderReview({ ...nameTestGaps(), lines, summary: { changed_lines: 15, run: 3, not_run: 2, survived: 2, caught: 0 } });
    const [notRun, unnoticed] = [...place().querySelectorAll(".place-gaps > div")];
    expect(notRun).toHaveTextContent("No test ran 1 changed line here: 12Measured: no test ran this line · go test -coverprofile");
    expect(unnoticed).toHaveTextContent("Tests ran line 11 but none noticed when Whooper changed err != nil to err == nilMeasured: no test noticed this change");
    // The change keeps Whooper's code in code type.
    expect(codeIn(unnoticed)).toEqual(["err != nil", "err == nil"]);
    expect(place(2)).toHaveTextContent("Tests ran line 6 but none noticed when Whooper changed {1,61} to {1,62}Measured: no test noticed this change");
    expect(place(2)).not.toHaveTextContent("No test ran");
    expect(block()).toHaveTextContent("Tests caught 0 of 2 changes Whooper made on purpose.");
    expect(within(block()!).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["internal/projects/create.go 14"]);

    press("j");
    await waitFor(() => expect(document.querySelector(".place-card")).toHaveTextContent("Tests ran line 11 but none noticed when Whooper changed err != nil to err == nil"), SLOW);
    expect(document.querySelector(".place-card")).toHaveTextContent("No test ran 1 changed line here: 12");
    press("j");
    await waitFor(() => expect(document.querySelector(".place-card")).toHaveTextContent("Tests ran line 6 but none noticed when Whooper changed {1,61} to {1,62}"), SLOW);
  }, 30_000);

  it("counts the changes tests caught after the places, without listing any of them", async () => {
    const lines: TestGaps["lines"] = [
      ...notRunOnly().lines.slice(0, 3),
      { path: "internal/projects/name.go", line: 6, status: "caught", change: "`{1,61}` to `{0,61}`" },
      { path: "internal/projects/name.go", line: 10, status: "caught", change: "`namePattern.MatchString(name)` to `!namePattern.MatchString(name)`" },
    ];
    await renderReview({ ...nameTestGaps(), lines, summary: { changed_lines: 15, run: 2, not_run: 3, survived: 0, caught: 2 } });
    expect(block()).toHaveTextContent(
      "Tests ran 2 of 15 changed lines, and no test ran 3. The run counted the other 10 as neither, such as comments. Tests caught 2 of 2 changes Whooper made on purpose.",
    );
    expect(place(2).querySelector(".place-gaps")).toBeNull();
    const said = [...document.querySelectorAll(".place-gaps"), block()!].map((element) => element.textContent).join(" ");
    for (const word of ["line 6", "line 10", "{0,61}", "!namePattern"]) expect(said).not.toContain(word);
    expect(within(block()!).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["internal/projects/create.go 14"]);
  }, 30_000);

  it("names each run of the tests in code type when Whooper measured with several", async () => {
    const tool = "go test -coverprofile ./internal/projects/...; npx vitest run --coverage src/projects";
    const runs = ["go test -coverprofile ./internal/projects/...", "npx vitest run --coverage src/projects"];
    const view = await openAllLines({ ...nameTestGaps(), tool });
    const measured = view.querySelector(".gaps-counts")!;
    expect(measured).toHaveTextContent(`Measured with ${runs[0]} and ${runs[1]}. Tests ran 2 of 15 changed lines`);
    expect(codeIn(measured)).toEqual(runs);

    press("Escape");
    expect(document.querySelector(".walkthrough")).not.toHaveAttribute("hidden");
    const label = place().querySelector(".place-evidence:last-child")!;
    expect(label).toHaveTextContent(`Measured: no test ran these lines · ${runs[0]} and ${runs[1]}`);
    expect(codeIn(label)).toEqual(runs);
  }, 30_000);

  it("says why in one quiet line when the run couldn't measure, with nothing at the places and nothing to open", async () => {
    await renderReview({ ...nameTestGaps(), measured: false, reason: "go isn't on the PATH" });
    const line = screen.getByText("Not measured: go isn't on the PATH");
    expect(line).toHaveClass("walk-view-quiet");
    expect(line).toHaveTextContent(`${NAME}Not measured: go isn't on the PATH`);
    expect(block()).not.toBeInTheDocument();
    expect(place()).not.toHaveTextContent("No test ran");
    expect(place(2)).not.toHaveTextContent("noticed");
    expect(screen.queryByRole("button", { name: "Open all lines" })).not.toBeInTheDocument();
  }, 30_000);

  it("opens every line by file in the walkthrough's place: the counts in words, each file's lines with how that is known and its diff, and what the measure may miss", async () => {
    const view = await openAllLines(notRunOnly());
    expect(document.querySelector(".walkthrough")).toHaveAttribute("hidden");
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: NAME }));
    expect(view).toHaveTextContent("Measured with go test -coverprofile. Tests ran 2 of 15 changed lines, and no test ran 4.");
    const create = within(view).getByRole("region", { name: "internal/projects/create.go" });
    expect(create).toHaveTextContent("No test ran 3 changed lines: 11, 12, 14");
    expect(create.querySelector(".pt-diff")).not.toBeNull();
    const name = within(view).getByRole("region", { name: "internal/projects/name.go" });
    expect(name).toHaveTextContent("No test ran 1 changed line: 10");
    expect(name).toHaveTextContent("Measured: no test ran this line");
    const limits = within(view).getByRole("region", { name: "What this measure may miss" });
    expect(within(limits).getAllByRole("listitem").map((item) => item.textContent)).toEqual(nameTestGaps().limits);
  }, 30_000);

  it("groups each file's lines in full: no test ran, then no test noticed, then caught", async () => {
    // Listed out of order: the view groups them by what the runs found.
    const lines: TestGaps["lines"] = [
      { path: "internal/projects/create.go", line: 14, status: "caught", change: "`newProject(name, name)` to `nil, nil`" },
      { path: "internal/projects/create.go", line: 12, status: "not_run" },
      { path: "internal/projects/create.go", line: 11, status: "survived", change: "`err != nil` to `err == nil`" },
      { path: "internal/projects/name.go", line: 6, status: "survived", change: "`{1,61}` to `{1,62}`" },
    ];
    const view = await openAllLines({ ...nameTestGaps(), lines, summary: { changed_lines: 15, run: 4, not_run: 1, survived: 2, caught: 1 } });
    expect(view.querySelector(".gaps-counts")).toHaveTextContent(
      "Measured with go test -coverprofile. Tests ran 4 of 15 changed lines, and no test ran 1. The run counted the other 10 as neither, such as comments. Tests caught 1 of 3 changes Whooper made on purpose.",
    );
    const create = within(view).getByRole("region", { name: "internal/projects/create.go" });
    const groups = [...create.querySelectorAll(".gaps-group")];
    expect(groups.map((group) => group.textContent)).toEqual([
      "No test ran 1 changed line: 12Measured: no test ran this line · go test -coverprofile",
      "Tests ran line 11 but none noticed when Whooper changed err != nil to err == nilMeasured: no test noticed this change",
      "Tests caught this change Whooper made on purpose:Line 14: newProject(name, name) to nil, nil",
    ]);
    // The diff comes after the words that name its lines.
    expect(follows(groups[2], create.querySelector(".pt-diff")!)).toBe(true);
    const name = within(view).getByRole("region", { name: "internal/projects/name.go" });
    expect(name).not.toHaveTextContent("No test ran");
    expect(name).not.toHaveTextContent("Tests caught");
  }, 30_000);

  it("shows why, and no lines, in full for a run that couldn't measure", () => {
    render(() => <TestGapsView testGaps={{ ...nameTestGaps(), measured: false, reason: "the tests didn't build" }} patch={null} onBack={vi.fn()} />);
    const view = screen.getByRole("region", { name: NAME });
    expect(view).toHaveTextContent("Not measured: the tests didn't build");
    expect(view.querySelector(".full-view-column")).not.toHaveTextContent("noticed");
    expect(view.querySelector(".gaps-file")).toBeNull();
    press("j");
    expect(view.querySelector("[data-current]")).toBeNull();
  });

  it("steps through the files with j and k, and Escape clears the current file, then goes back with the focus on Open all lines", async () => {
    const view = await openAllLines();
    const current = () => [...view.querySelectorAll(".gaps-file[data-current]")].map((element) => element.querySelector("h4")!.textContent);
    press("j");
    expect(current()).toEqual(["internal/projects/create.go"]);
    press("j");
    expect(current()).toEqual(["internal/projects/name.go"]);
    press("j");
    expect(current()).toEqual(["internal/projects/create.go"]);
    press("k");
    expect(current()).toEqual(["internal/projects/name.go"]);

    press("Escape");
    expect(current()).toEqual([]);
    expect(screen.getByRole("region", { name: NAME })).toBe(view);
    press("Escape");
    expect(document.querySelector(".walkthrough")).not.toHaveAttribute("hidden");
    expect(document.activeElement).toBe(within(block()!).getByRole("button", { name: "Open all lines" }));
  }, 30_000);

  it("goes back to the walkthrough when Views turns the view off while it shows", async () => {
    await openAllLines();
    fireEvent.click(await viewsButton());
    fireEvent.click(await screen.findByRole("menuitemcheckbox", { name: new RegExp(`^${NAME}`) }));
    await waitFor(() => expect(document.querySelector(".walkthrough")).not.toHaveAttribute("hidden"));
    expect(block()).not.toBeInTheDocument();
    expect(place()).not.toHaveTextContent("No test ran");
    expect(place(2)).not.toHaveTextContent("noticed");
  }, 30_000);

  it("writes line numbers in order, a run of three or more as a range, and the counts in words", () => {
    expect(lineList([50, 39, 40, 41, 42])).toBe("39–42, 50");
    expect(lineList([40, 39])).toBe("39, 40");
    expect(countsText({ changed_lines: 21, run: 3, not_run: 4, survived: 2, caught: 5 })).toBe(
      "Tests ran 3 of 21 changed lines, and no test ran 4. The run counted the other 14 as neither, such as comments. Tests caught 5 of 7 changes Whooper made on purpose.",
    );
  });
});
