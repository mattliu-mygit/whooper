import { screen, waitFor, within } from "@testing-library/dom";
import { describe, expect, it, vi } from "vitest";
import { nameConnections, NAME_RUN, RETRY_RUN, typeScriptConnections } from "../fixtures";
import type { Shown } from "../protocol";
import { fireEvent, follows, openReview, press, SLOW, viewsButton, walkthrough } from "../test/review";
import { layoutConnections } from "./Connections";

const summary = () => screen.queryByRole("region", { name: "How the changed code connects" });
const node = (name: RegExp) => screen.getByRole("button", { name });
const panel = () => screen.getByRole("complementary");
const legend = () => screen.getByRole("list", { name: "What the map's marks mean" });
/** A selected node's links in one of its panel's groups, "Called or used by" or "Calls or uses". */
const links = (group: string) => within(within(panel()).getByRole("region", { name: group })).getAllByRole("listitem");
const open = () => fireEvent.click(screen.getByRole("button", { name: "Open the map" }));

/** Opens the name review, with `shown` replacing show-review's fields, and its map. */
async function openMap(shown: Partial<Shown> = {}) {
  openReview(NAME_RUN, shown);
  await walkthrough();
  open();
  return screen.getByRole("group", { name: "Map of the changed code" });
}

/** Opens the name review with `connections` as its map, and returns its summary of the map. */
async function renderMap(connections: Shown["connections"]) {
  openReview(NAME_RUN, { connections });
  await walkthrough();
  return summary()!;
}

describe("how the changed code connects", () => {
  it("stays hidden when show-review prints no connections: no summary, no part in the navigator, and no item in Views", async () => {
    const nav = await (openReview(RETRY_RUN), walkthrough());
    expect(summary()).not.toBeInTheDocument();
    expect(within(nav).queryByRole("button", { name: "How the changed code connects" })).not.toBeInTheDocument();
    fireEvent.click(await viewsButton());
    await screen.findByRole("menuitemcheckbox", { name: /^Calls before and after/ });
    expect(screen.queryByRole("menuitemcheckbox", { name: /How the changed code connects/ })).not.toBeInTheDocument();
  }, 30_000);

  it("sums the map up where the layers start, and turns off from the Views menu, which counts it as off", async () => {
    openReview(NAME_RUN);
    const nav = await walkthrough();
    const found = summary()!;
    const divider = screen.getByText("The change, layer by layer").closest("p")!;
    expect(follows(divider, found)).toBe(true);
    expect(follows(found, screen.getByRole("region", { name: "Layer 1: The name rule" }))).toBe(true);
    expect(found).toHaveTextContent("4 functions and variables change here: 2 added, 1 changed, and 1 removed.");
    expect(found).toHaveTextContent("2 unchanged callers: api.handleCreate and api.signup.");
    expect(found).toHaveTextContent("1 of 4 links was found by name search, so it may point at a different function or type with the same name; gopls found the rest.");
    expect(within(nav).getByRole("button", { name: "How the changed code connects" })).toBeInTheDocument();

    fireEvent.click(await viewsButton());
    const item = await screen.findByRole("menuitemcheckbox", { name: /^How the changed code connects/ });
    expect(item).toHaveAttribute("aria-checked", "true");
    fireEvent.click(item);
    await waitFor(() => expect(summary()).not.toBeInTheDocument());
    // Only the map is off; Calls before and after has nothing for this review but stays on.
    expect(screen.getByRole("button", { name: /^Views · 1 off/ })).toBeInTheDocument();
  }, 30_000);

  it("opens the map in the walkthrough's place, and Back finds the walkthrough with the focus on Open the map", async () => {
    await openMap();
    expect(document.querySelector(".walkthrough")).toHaveAttribute("hidden");
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "How the changed code connects" }));
    expect(await screen.findByRole("button", { name: "Walkthrough" }, SLOW)).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: "Back to the walkthrough" }));
    expect(screen.queryByRole("group", { name: "Map of the changed code" })).not.toBeInTheDocument();
    expect(document.querySelector(".walkthrough")).not.toHaveAttribute("hidden");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Open the map" }));
  }, 30_000);

  it("says each node's change in words, and a click selects it: its panel shows where it is and its links, and a link selects the other end", async () => {
    await openMap();
    expect(node(/^projects\.slugify, function, removed, 4 lines$/)).toHaveTextContent("removed · 4 lines");
    expect(node(/^api\.handleCreate, function, unchanged$/)).toHaveTextContent("unchanged");
    fireEvent.click(node(/^projects\.ValidateName,/));
    expect(node(/^projects\.ValidateName,/)).toHaveAttribute("aria-pressed", "true");
    expect(panel()).toHaveAccessibleName("projects.ValidateName: where it is and its links");
    expect(panel()).toHaveTextContent("function · added · 3 lines changed");
    expect(within(panel()).getByRole("button", { name: /^internal\/projects\/name\.go:9/ })).toHaveAttribute("aria-expanded", "false");
    const callers = within(panel()).getByRole("region", { name: "Called or used by" });
    const [create, signup] = within(callers).getAllByRole("listitem");
    expect(create).toHaveTextContent("projects.Create calls projects.ValidateName");
    expect(create).toHaveTextContent("found by gopls");
    expect(signup).toHaveTextContent("api.signup calls projects.ValidateName");
    expect(within(panel()).getByRole("region", { name: "Calls or uses" })).toHaveTextContent("projects.ValidateName uses projects.namePattern");

    fireEvent.click(within(signup).getByRole("button", { name: "api.signup" }));
    expect(node(/^api\.signup,/)).toHaveAttribute("aria-pressed", "true");
    expect(panel()).toHaveTextContent("internal/api/users.go:30 · unchanged, so the diff has none of its lines");
    expect(within(panel()).queryByRole("button", { name: /Open the diff/ })).not.toBeInTheDocument();
  }, 30_000);

  it("draws what only a search for a name found dashed, and says found by name search in the legend and the links", async () => {
    await openMap();
    expect(document.querySelectorAll('.map-edge[data-found-by="name_search"]')).toHaveLength(1);
    expect(document.querySelectorAll('.map-edge[data-found-by="gopls"]')).toHaveLength(3);
    expect(legend()).toHaveTextContent("calls or uses, found by gopls");
    expect(legend()).toHaveTextContent("found by name search: may be a different function or type with the same name");
    expect(legend().querySelector('.map-line[data-found-by="name_search"]')).not.toBeNull();

    fireEvent.click(node(/^api\.signup,/));
    const [link] = links("Calls or uses");
    expect(link).toHaveAttribute("data-found-by", "name_search");
    expect(link).toHaveTextContent("found by name search, so api.signup may call a different function named projects.ValidateName");
  }, 30_000);

  it("draws what TypeScript found solid, as it does what gopls found, and says found by TypeScript in the summary, the legend, and the links", async () => {
    const found = await renderMap(typeScriptConnections());
    expect(found).toHaveTextContent("1 of 3 links was found by name search, so it may point at a different function or type with the same name; TypeScript found the rest.");
    open();
    expect(document.querySelectorAll('.map-edge[data-found-by="typescript"]')).toHaveLength(2);
    expect(document.querySelectorAll('.map-edge[data-found-by="name_search"]')).toHaveLength(1);
    expect(legend()).toHaveTextContent("calls or uses, found by TypeScript");

    fireEvent.click(node(/^formatTokens,/));
    const [sidebar, report] = links("Called or used by");
    expect(sidebar).toHaveTextContent("TraceSidebar calls formatTokens");
    expect(sidebar).toHaveAttribute("data-found-by", "typescript");
    expect(sidebar.querySelector(".map-found")).toHaveTextContent(/^found by TypeScript$/);
    expect(report).toHaveAttribute("data-found-by", "name_search");
    expect(report).toHaveTextContent("found by name search, so exportReport may call a different function named formatTokens");
  }, 30_000);

  it("names no tool where no language tool found a link", async () => {
    const connections = nameConnections();
    connections.edges = connections.edges.map((edge) => ({ ...edge, found_by: "name_search" }));
    const found = await renderMap(connections);
    expect(found).toHaveTextContent(/4 of 4 links were found by name search, so each may point at a different function or type with the same name\.$/);
    open();
    expect(within(legend()).getAllByRole("listitem").map((item) => item.textContent)).toContain("calls or uses");
  }, 30_000);

  it("lists what the map may miss in Whooper's words", async () => {
    await openMap();
    const limits = screen.getByRole("region", { name: "What this map may miss" });
    expect(within(limits).getAllByRole("listitem").map((item) => item.textContent)).toEqual(nameConnections().limits);
  }, 30_000);

  it("steps through the changed nodes with j and k, selects any node with Enter, and Escape clears the selection, then goes back", async () => {
    await openMap();
    const pressed = () =>
      screen
        .getAllByRole("button", { pressed: true })
        .filter((element) => element.classList.contains("map-node"))
        .map((element) => element.getAttribute("data-node"));
    press("j");
    expect(pressed()).toEqual(["slugify"]);
    press("j");
    expect(pressed()).toEqual(["create"]);
    press("k");
    press("k");
    // Around the start to the last changed node; the unchanged callers between them are skipped.
    expect(pressed()).toEqual(["pattern"]);

    const caller = node(/^api\.handleCreate,/);
    expect(caller).toHaveAttribute("tabindex", "0");
    caller.focus();
    fireEvent.keyDown(caller, { key: "Enter" });
    expect(pressed()).toEqual(["handle"]);

    press("Escape");
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Map of the changed code" })).toBeInTheDocument();
    press("Escape");
    expect(screen.queryByRole("group", { name: "Map of the changed code" })).not.toBeInTheDocument();
    expect(document.querySelector(".walkthrough")).not.toHaveAttribute("hidden");
  }, 30_000);

  it("opens the diff at a node's line from its location, on the old side for a removed function, and says when the diff starts later", async () => {
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled(this.getAttribute("data-column-number"), this.getAttribute("data-line-type"));
    };
    try {
      const connections = nameConnections();
      // Create's first line is outside the patch, which starts at line 10.
      connections.nodes[2] = { ...connections.nodes[2], line: 4 };
      await openMap({ connections });
      press("j");
      const location = within(panel()).getByRole("button", { name: /^internal\/projects\/create\.go:15/ });
      fireEvent.click(location);
      expect(location).toHaveAttribute("aria-expanded", "true");
      await waitFor(() => expect(scrolled).toHaveBeenCalledWith("15", "change-deletion"), SLOW);

      // The diff stays open as j moves the selection to Create.
      press("j");
      expect(panel()).toHaveTextContent("The diff doesn't show line 4; it opens at line 10, the next line it shows.");
      await waitFor(() => expect(scrolled).toHaveBeenLastCalledWith("10", expect.anything()), SLOW);
    } finally {
      // @ts-expect-error jsdom has no scrollIntoView; the test put one in for itself.
      delete Element.prototype.scrollIntoView;
    }
  }, 30_000);

  it("places callers left of what they call, and a cycle doesn't stop the layout", () => {
    const layout = layoutConnections(nameConnections());
    const x = (id: string) => layout.nodes.find((item) => item.id === id)!.x;
    expect(x("handle")).toBeLessThan(x("create"));
    expect(x("create")).toBeLessThan(x("validate"));
    expect(x("validate")).toBeLessThan(x("pattern"));
    const cycle = nameConnections();
    cycle.edges.push({ from: "pattern", to: "handle", kind: "uses", found_by: "gopls" });
    expect(layoutConnections(cycle).nodes).toHaveLength(6);
  });
});
