import { For } from "solid-js";
import type { Finding, Verdict } from "./protocol";

/** The Conventional Comments word for a finding's kind, as whooper-submit labels a comment without one. */
export function kindLabel(kind: Finding["kind"]): string {
  return { caller_visible_bug: "issue", ci_failure: "issue", nit: "nit", question: "question", other: "suggestion" }[kind];
}

export function verdictText(verdict: Verdict): string {
  return verdict === "approve_once_ci_passes" ? "approve once CI passes" : "look first";
}

/**
 * A comment's text with its **bold** and `code` spans drawn, the two a draft uses most. The rest
 * shows as written, as GitHub's Markdown would mostly show it, until the shared library has a
 * Markdown component.
 */
export function CommentText(props: { text: string }) {
  // Objects, not the strings themselves, so a part that repeats is still its own item.
  const parts = () =>
    props.text
      .split(/(\*\*[^*]+\*\*|`[^`]+`)/)
      .filter(Boolean)
      .map((text) => ({ text }));
  return (
    <For each={parts()}>
      {({ text }) =>
        text.startsWith("**") && text.endsWith("**") && text.length > 4 ? (
          <strong>{text.slice(2, -2)}</strong>
        ) : text.startsWith("`") && text.endsWith("`") && text.length > 2 ? (
          <code>{text.slice(1, -1)}</code>
        ) : (
          text
        )
      }
    </For>
  );
}
