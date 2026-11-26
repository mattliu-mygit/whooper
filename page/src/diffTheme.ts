import { registerCustomTheme } from "@pierre/diffs";

// A copy of @prompting-time/ui's diff theme (src/diffTheme.ts there), which the package does not
// export. Delete this file with PatchDiff.tsx once the package's Diff draws a parsed patch.

/** The name PatchDiff gives Pierre for the Shiki theme below. */
export const DIFF_THEME = "whooper-sigil";

const sigil = (name: string) => `var(--s-${name})`;

/** A Shiki theme whose colors are Sigil's CSS variables, which reach into Pierre's Shadow DOM. */
const diffTheme = {
  name: DIFF_THEME,
  type: "dark" as const,
  colors: {
    "editor.background": sigil("code-bg"),
    "editor.foreground": sigil("text"),
    "gitDecoration.addedResourceForeground": sigil("success"),
    "gitDecoration.deletedResourceForeground": sigil("error"),
  },
  tokenColors: [
    { scope: ["comment", "punctuation.definition.comment"], settings: { foreground: sigil("code-comment-color") } },
    { scope: ["string", "punctuation.definition.string"], settings: { foreground: sigil("code-string-color") } },
    { scope: ["constant.numeric"], settings: { foreground: sigil("code-number-color") } },
    { scope: ["entity.name.type", "support.type"], settings: { foreground: sigil("code-number-color") } },
    { scope: ["keyword", "storage", "storage.type", "storage.modifier"], settings: { foreground: sigil("code-keyword-color") } },
    { scope: ["keyword.operator", "punctuation"], settings: { foreground: sigil("text") } },
    { scope: ["entity.name.function", "support.function"], settings: { foreground: sigil("code-function-color") } },
  ],
};

let registered = false;

/** Registers the theme with Pierre once; Pierre logs an error when a name is registered twice. */
export function registerDiffTheme() {
  if (registered) return;
  registerCustomTheme(DIFF_THEME, async () => diffTheme);
  registered = true;
}
