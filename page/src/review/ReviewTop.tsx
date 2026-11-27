import { createSignal, For, Show, untrack } from "solid-js";
import { Button, ChevronDownIcon, ChevronRightIcon, CloseIcon, IconButton, StatusDot, type Status } from "@prompting-time/ui";
import type { CommitSinceReview, Review, Shown, StatusReview } from "../protocol";
import type { PageStore } from "../store";
import { plural, shortDate, shortSha, submittedText, unsafeReasons, verdictText, whenText } from "../text";
import type { LastLook, ReviewMemory } from "./memory";
import { viewHasData, viewName, type ReviewViews } from "./views";

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/**
 * The verdict banner's color, the page's one color mapping: red for a review that failed or waits on
 * a cause, green for one Whooper judges safe to approve, orange for the rest, which need a look first.
 */
export function verdictTone(shown: Pick<Shown, "outcome" | "safe_to_approve">): Status {
  if (shown.outcome === "failed" || shown.outcome === "waiting") return "error";
  return shown.safe_to_approve ? "success" : "attention";
}

/**
 * The verdict first in the review, as a banner tinted with its color: the verdict in words, such as
 * "Approve once CI passes" or, for one Whooper judges not safe to approve, why ("· has a question"),
 * then the reason, then the version, its commit, and its draft comments.
 */
export function VerdictBanner(props: { review: Review; shown: Shown; entry: StatusReview | null; number: number }) {
  const findings = () => props.review.findings;
  const skipped = () => findings().status === "skipped";
  const words = () => {
    const verdict = findings().approval.verdict;
    if (verdict === "approve_once_ci_passes" && !props.shown.safe_to_approve)
      return capital(`${verdictText(verdict)} · ${unsafeReasons(skipped(), findings().findings.map((finding) => finding.kind).filter((kind) => kind !== "nit"))}`);
    return capital(skipped() ? "skipped, look first" : verdictText(verdict));
  };
  const reason = () => props.review.overview?.verdict_reason || findings().approval.reason;
  const meta = () =>
    [
      `Review ${props.number}`,
      shortSha(props.shown.head_sha),
      plural(findings().findings.length, "draft comment"),
      props.entry?.submitted ? submittedText(props.entry.submitted) : null,
    ]
      .filter(Boolean)
      .join(" · ");
  return (
    <section class="verdict-banner" data-tone={verdictTone(props.shown)} aria-label="Verdict">
      <p class="verdict-banner-head">
        <StatusDot status={verdictTone(props.shown)} />
        <span class="verdict-banner-words">{words()}</span>
        <span class="verdict-banner-meta">{meta()}</span>
      </p>
      <Show when={reason()}>{(text) => <p class="verdict-banner-reason">{text()}</p>}</Show>
    </section>
  );
}

/** The commits after `head` in the list, or every one when `head` isn't in it. */
function commitsAfter(commits: readonly CommitSinceReview[], head: string | null): CommitSinceReview[] {
  const index = head ? commits.findIndex((commit) => commit.sha.startsWith(head) || head.startsWith(commit.sha)) : -1;
  return index < 0 ? [...commits] : commits.slice(index + 1);
}

type NoticeProps = {
  entry: StatusReview;
  /** Every version of the review's pull request, oldest first. */
  versions: readonly StatusReview[];
  commits: readonly CommitSinceReview[];
  /** What the user opened of this pull request before this time, if anything. */
  look: LastLook | null;
  onHide(): void;
};

/**
 * "Changes since you last looked" as one line under the verdict: how many commits the pull request
 * got after the reviewed one (and how many of them since you last opened it), and ×, which hides the
 * line for this version of the pull request. It starts folded; the line opens what you last read and
 * the commits. Rerun review is the header's, which turns primary when the pull request moved.
 */
function SinceNotice(props: NoticeProps) {
  const [open, setOpen] = createSignal(false);
  const sinceLook = () => (props.look ? commitsAfter(props.commits, props.look.head).length : props.commits.length);
  const fresh = (commit: CommitSinceReview) => props.look !== null && commitsAfter(props.commits, props.look.head).includes(commit);
  const earlier = () => (props.look && props.look.run !== props.entry.run_id ? (props.versions.find((version) => version.run_id === props.look!.run) ?? null) : null);
  const looked = () => {
    const look = props.look;
    if (!look) return "You haven't opened this pull request's review before.";
    if (look.run === props.entry.run_id) return `You last opened this review ${whenText(look.at)}.`;
    const before = earlier();
    if (!before) return `You last opened an earlier review ${whenText(look.at)}.`;
    const said = before.verdict ? verdictText(before.verdict) : "nothing yet";
    return `You last opened review ${props.versions.indexOf(before) + 1} (${shortSha(before.head_sha)}) ${whenText(look.at)}, which said ${said} with ${plural(before.findings, "finding")}.`;
  };
  return (
    <section class="since-notice" data-open={open() ? "" : undefined} aria-label={viewName("since")}>
      <div class="since-notice-line">
        <button
          type="button"
          class="since-notice-toggle"
          aria-expanded={open() ? "true" : "false"}
          aria-controls={`since-${props.entry.run_id}`}
          onClick={() => setOpen(!open())}
        >
          {open() ? <ChevronDownIcon /> : <ChevronRightIcon />}
          <span>
            {plural(props.commits.length, "new commit")} since this review
            <Show when={sinceLook() > 0 && sinceLook() < props.commits.length}> · {sinceLook()} since you last looked</Show>
          </span>
        </button>
        <IconButton label="Hide until the pull request gets new commits" onClick={() => props.onHide()}>
          <CloseIcon />
        </IconButton>
      </div>
      <Show when={open()}>
        <div class="since-notice-detail" id={`since-${props.entry.run_id}`}>
          <p>
            {looked()} The review read {shortSha(props.entry.head_sha) ?? "an earlier commit"}; none of these commits.
          </p>
          <ul class="since-commits">
            <For each={props.commits}>
              {(commit) => (
                <li>
                  <code>{commit.sha.slice(0, 7)}</code>
                  <span class="since-commit-title">{commit.title}</span>
                  <span class="since-commit-meta">
                    {[commit.author ? `@${commit.author}` : null, shortDate(commit.at), plural(commit.files.length, "file")].filter(Boolean).join(" · ")}
                    <Show when={fresh(commit)}> · new since you last looked</Show>
                  </span>
                </li>
              )}
            </For>
          </ul>
        </div>
      </Show>
    </section>
  );
}

/**
 * The top of a review with an overview, above the walkthrough and the diff alike: the verdict banner,
 * then the since notice, which × folds to "Hidden until the pull request gets new commits" with Undo.
 * The notice needs the review as whooper-status lists it, for the pull request's head.
 */
export function ReviewTop(props: { store: PageStore; memory: ReviewMemory; views: ReviewViews; look: LastLook | null }) {
  const store = props.store;
  const review = () => store.state.review as Review;
  const shown = () => store.state.shown as Shown;
  const number = () => Math.max(store.versions().findIndex((version) => version.run_id === review().run_id) + 1, 1);
  const [hidden, setHidden] = createSignal(false);
  const [undo, setUndo] = createSignal(false);
  // Read once the status lists the review; until then the notice waits.
  const isHidden = (entry: StatusReview) => hidden() || untrack(() => props.memory.sinceHidden(entry));
  const commits = (entry: StatusReview) =>
    props.views.isOn("since") && viewHasData("since", review()) && !isHidden(entry) ? (review().commits_since_review ?? []) : [];
  const hide = (entry: StatusReview, value: boolean) => {
    props.memory.hideSince(entry, value);
    setHidden(value);
    setUndo(value);
  };
  return (
    <div class="review-top">
      <VerdictBanner review={review()} shown={shown()} entry={store.entry()} number={number()} />
      <Show when={store.entry()}>
        {(entry) => (
          <>
            <Show when={commits(entry()).length > 0}>
              <SinceNotice entry={entry()} versions={store.versions()} commits={commits(entry())} look={props.look} onHide={() => hide(entry(), true)} />
            </Show>
            <Show when={undo()}>
              <p class="since-hidden" role="status">
                Hidden until the pull request gets new commits.
                <Button onClick={() => hide(entry(), false)}>Undo</Button>
              </p>
            </Show>
          </>
        )}
      </Show>
    </div>
  );
}
