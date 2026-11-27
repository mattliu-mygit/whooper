import { expect } from "vitest";
import appCss from "../app.css?raw";
import reviewCss from "../review/review.css?raw";

/** Puts the page's own CSS in the document, so getComputedStyle answers as the page's rules say. */
export function usePageCss() {
  const style = document.createElement("style");
  style.textContent = `${appCss}\n${reviewCss}`;
  document.head.append(style);
  return () => style.remove();
}

/** Splits a grid-template-columns value at its top-level spaces: "16rem minmax(0, 1fr)" has two tracks. */
function tracks(value: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let current = "";
  for (const char of value) {
    if (char === "(") depth++;
    if (char === ")") depth--;
    if (char === " " && depth === 0) {
      if (current) out.push(current);
      current = "";
    } else current += char;
  }
  if (current) out.push(current);
  return out;
}

/**
 * Checks that no grid between each diff in `root` and the area that scrolls it can grow wider than its
 * own box. jsdom lays nothing out, so this checks the rule that keeps the layout in bounds: a grid
 * with no column template, or with an auto or bare fr track, sizes that track to at least its widest
 * child's min-content, such as a diff's longest line of code, and so overflows and pushes centered
 * children off to the side. Each track must instead have a fixed width or a fixed minimum, such as 0.
 */
export function expectCodeFits(root: ParentNode) {
  const diffs = [...root.querySelectorAll(".pt-diff")];
  expect(diffs.length).toBeGreaterThan(0);
  for (const diff of diffs) {
    for (let element = diff.parentElement; element; element = element.parentElement) {
      const style = getComputedStyle(element);
      if (style.display === "grid" || style.display === "inline-grid") {
        const where = `.${[...element.classList].join(".")}`;
        expect(style.gridTemplateColumns, `${where} has no column template`).not.toBe("none");
        for (const track of tracks(style.gridTemplateColumns))
          expect(track, `${where}'s track ${track}`).toMatch(/^(minmax\((0|[\d.]+(px|rem)), .+\)|[\d.]+(px|rem))$/);
      }
      if (style.overflowY === "auto" || style.overflowY === "scroll") break;
    }
  }
}
