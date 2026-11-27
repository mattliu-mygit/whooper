import { For } from "solid-js";
import manifest from "../../prompting-time-plugin.json";
import type { Finding, Verdict } from "./protocol";

/** The page's name for Whooper in its sentences, from one place: the manifest's title. */
export const TITLE = manifest.title;

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

/** "1 file" or "2 files". */
export const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/** "a", "a and b", "a, b, and c"; or "a or b" with "or" for `joiner`. */
export const series = (items: readonly string[], joiner = "and") =>
  items.length > 2 ? `${items.slice(0, -1).join(", ")}, ${joiner} ${items.at(-1)}` : items.join(` ${joiner} `);

/** Words in a sentence, "a, b, and c", the names among them in code type. */
export function Series(props: { items: ReadonlyArray<{ text: string; code: boolean }> }) {
  const parts = () =>
    props.items.flatMap((item, index) => [
      ...(index === 0 ? [] : [{ text: index < props.items.length - 1 ? ", " : props.items.length > 2 ? ", and " : " and ", code: false }]),
      item,
    ]);
  return <For each={parts()}>{(part) => (part.code ? <code class="walk-code">{part.text}</code> : part.text)}</For>;
}

/** Text with `code` spans, as a review writes identifiers in its overview. */
export function Inline(props: { text: string }) {
  const parts = () => props.text.split("`").map((text, index) => ({ text, code: index % 2 === 1 }));
  return <For each={parts()}>{(part) => (part.code ? <code class="walk-code">{part.text}</code> : part.text)}</For>;
}

/** "at 3:12 PM" today, "on Nov 24" before. */
export const whenText = (iso: string, now = new Date()) => `${shortDate(iso, now).includes(":") ? "at" : "on"} ${shortDate(iso, now)}`;

/** What the user submitted: "approved at 3:12 PM" or "commented on Nov 24". */
export const submittedText = (submitted: { event: "APPROVE" | "COMMENT"; at: string }, now = new Date()) =>
  `${submitted.event === "APPROVE" ? "approved" : "commented"} ${whenText(submitted.at, now)}`;

/** The kinds of finding other than a nit, in the order the reasons name them, with the words for one and for several. */
const CONCERNS: ReadonlyArray<readonly [kind: Finding["kind"], one: string, several: string]> = [
  ["caller_visible_bug", "a bug", "bugs"],
  ["ci_failure", "a CI failure", "CI failures"],
  ["question", "a question", "questions"],
  ["other", "a suggestion", "suggestions"],
];

/**
 * Why Whooper judges a review that says approve once CI passes not safe to approve, as whooper-submit
 * does: the review skipped the change, so it didn't read the whole change, or it found more than
 * nits. A run with a nit and a question "has a question"; one with two questions and a bug "has a bug
 * and 2 questions".
 */
export function unsafeReasons(skipped: boolean, kinds: readonly Finding["kind"][]): string {
  const reasons = skipped ? ["didn't read the whole change"] : [];
  const words = CONCERNS.flatMap(([kind, one, several]) => {
    const count = kinds.filter((item) => item === kind).length;
    return count === 0 ? [] : [count === 1 ? one : `${count} ${several}`];
  });
  if (words.length > 0) reasons.push(`has ${series(words)}`);
  return reasons.length > 0 ? reasons.join(", ") : "not safe to approve";
}
