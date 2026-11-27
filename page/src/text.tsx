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

/** A commit as the review header shows it: its first 7 characters. */
export function shortSha(sha: string | null): string | null {
  return sha ? sha.slice(0, 7) : null;
}

/** A time from Whooper as the page shows it: "3:12 PM" today, "Nov 24" this year, "Nov 24, 2025" before. */
export function shortDate(iso: string, now = new Date()): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  if (date.toDateString() === now.toDateString()) return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return date.toLocaleDateString([], {
    month: "short",
    day: "numeric",
    ...(date.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  });
}

/** "36 reviews waiting", "1 review waiting", or "no reviews waiting". */
export function waitingText(count: number): string {
  return count === 0 ? "no reviews waiting" : `${count} ${count === 1 ? "review" : "reviews"} waiting`;
}

/**
 * An @-mention, by whooper-submit's rule (MENTION in lib/whooper.py), which refuses to post one
 * without --allow-mentions. JavaScript's \w knows only ASCII letters and Python's knows every letter,
 * so this finds each mention whooper-submit finds, and also one right after a letter such as é, which
 * whooper-submit lets through.
 */
export const MENTION = /(?<![\w.@/])@[A-Za-z0-9][A-Za-z0-9-]*(?:\/[A-Za-z0-9_-]+)?/;
