import { createMemo, createSignal, For, Show, untrack } from "solid-js";
import { Button, Diff, type DiffLineAnnotation } from "@prompting-time/ui";
import { DraftThread } from "./DraftThread";
import { readPatch, showsLine, type PatchFile } from "./patch";
import type { Finding, Review } from "./protocol";
import type { PageStore } from "./store";
import { verdictText } from "./text";

/**
 * A review run as the agreed mock draws it: the PR's diff, one file at a time, with Whooper's
 * findings as draft comments under their lines, and one bar that submits them as one GitHub review.
 */
export function ReviewScreen(props: { store: PageStore }) {
  const store = props.store;
  const review = () => store.state.review as Review;
  // A new review message, even for the same run, parses the patch again, and the diff rebuilds.
  const files = createMemo(() => readPatch(review().patch));
  const findings = () => review().findings.findings;
  const [layout, setLayout] = createSignal<"split" | "unified">("split");
  const [chosen, setChosen] = createSignal<string | null>(null);
  // The file chosen in the list, or else the first with a finding, or else the first.
  const file = createMemo(() => {
    const all = files();
    const firstWithFinding = all.find((f) => findings().some((finding) => finding.path === f.path));
    return all.find((f) => f.path === chosen()) ?? firstWithFinding ?? all[0];
  });
  const findingsIn = (path: string) => findings().filter((f) => f.path === path);
  // Comments whose file the patch does not have, which no file of the diff can show.
  const elsewhere = () => findings().filter((f) => !files().some((file) => file.path === f.path));
  const additions = () => files().reduce((sum, f) => sum + f.additions, 0);
  const deletions = () => files().reduce((sum, f) => sum + f.deletions, 0);

  return (
    <div class="w-review">
      <header class="w-header">
        <div class="w-title">
          <h1>{review().title ?? `${review().repo}#${review().pr}`}</h1>
          <p class="w-meta">
            {review().repo}#{review().pr}
            {review().author ? ` · ${review().author}` : ""} · +{additions()} −{deletions()} · Whooper suggests:{" "}
            {verdictText(review().findings.approval.verdict)}
          </p>
        </div>
        <div class="w-segmented" role="group" aria-label="Diff layout">
          <Button aria-pressed={layout() === "split" ? "true" : "false"} onClick={() => setLayout("split")}>
            Split
          </Button>
          <Button aria-pressed={layout() === "unified" ? "true" : "false"} onClick={() => setLayout("unified")}>
            Unified
          </Button>
        </div>
      </header>
      <p class="w-summary">
        <strong>Whooper</strong>{" "}
        {review().findings.status === "skipped"
          ? `skipped this review: ${review().findings.skip_reason}`
          : review().findings.approval.reason}
      </p>
      <div class="w-body">
        <FileList files={files()} selected={file()?.path} findingsIn={findingsIn} onSelect={setChosen} />
        <div class="w-files-pane">
          <Show when={elsewhere().length > 0}>
            <section class="w-card" aria-label="Comments on files outside the diff">
              <header class="w-card-header">Comments on files the diff does not change</header>
              <For each={elsewhere()}>{(f) => <DraftThread store={store} findingId={f.id} />}</For>
            </section>
          </Show>
          <Show when={file()}>{(f) => <FileCard store={store} file={f()} findings={findingsIn(f().path)} layout={layout()} />}</Show>
        </div>
      </div>
      <SubmitBar store={store} />
    </div>
  );
}

function FileList(props: {
  files: PatchFile[];
  selected: string | undefined;
  findingsIn: (path: string) => Finding[];
  onSelect: (path: string) => void;
}) {
  const comments = () => props.files.reduce((sum, f) => sum + props.findingsIn(f.path).length, 0);
  // Files under a heading for their folder, folders in the order the patch first names them.
  const folders = createMemo(() => {
    const byFolder = new Map<string, PatchFile[]>();
    for (const f of props.files) {
      const folder = f.path.slice(0, f.path.lastIndexOf("/") + 1);
      byFolder.set(folder, [...(byFolder.get(folder) ?? []), f]);
    }
    return [...byFolder].map(([folder, files]) => ({ folder, files }));
  });
  return (
    <nav class="w-file-list" aria-label="Changed files">
      <p class="w-file-list-head">
        <span>{props.files.length} files</span>
        <span>{comments()} comments</span>
      </p>
      <For each={folders()}>
        {(group) => (
          <>
            <Show when={group.folder}>
              <p class="w-file-dir">{group.folder}</p>
            </Show>
            <ul>
              <For each={group.files}>
                {(f) => (
                  <li>
                    <button
                      type="button"
                      class="w-file"
                      aria-current={props.selected === f.path ? "true" : undefined}
                      title={f.path}
                      onClick={() => props.onSelect(f.path)}
                    >
                      <span class="w-file-status">{f.status}</span>
                      <span class="w-file-name">{f.path.slice(group.folder.length)}</span>
                      <span class="w-file-counts">
                        <Show when={props.findingsIn(f.path).length > 0}>
                          <span class="w-file-comments">{props.findingsIn(f.path).length}</span>
                        </Show>
                        +{f.additions} −{f.deletions}
                      </span>
                    </button>
                  </li>
                )}
              </For>
            </ul>
          </>
        )}
      </For>
    </nav>
  );
}

function FileCard(props: { store: PageStore; file: PatchFile; findings: Finding[]; layout: "split" | "unified" }) {
  // Each annotation's metadata is the finding's ID, a string, which Pierre compares by value, so
  // a drafts change does not rebuild a thread; the thread reads the finding from the store.
  const annotations = createMemo((): DiffLineAnnotation<string>[] =>
    props.findings
      .filter((f) => showsLine(props.file, f))
      .map((f) => ({ side: f.side === "LEFT" ? "deletions" : "additions", lineNumber: f.line, metadata: f.id })),
  );
  const offLine = () => props.findings.filter((f) => !showsLine(props.file, f));
  return (
    // Pierre's own header names the file and counts its lines.
    <article class="w-card" aria-label={props.file.path}>
      <Show when={offLine().length > 0}>
        <div class="w-off-line">
          <p class="w-hint">Comments on lines the diff does not show:</p>
          <For each={offLine()}>{(f) => <DraftThread store={props.store} findingId={f.id} />}</For>
        </div>
      </Show>
      <Diff
        file={props.file.diff}
        layout={props.layout}
        annotations={annotations()}
        annotationKey={(annotation) => annotation.metadata}
        renderAnnotation={(annotation) => <DraftThread store={props.store} findingId={untrack(annotation).metadata} />}
      />
    </article>
  );
}

function SubmitBar(props: { store: PageStore }) {
  const store = props.store;
  const drafts = () => store.state.review!.drafts;
  const comments = () => store.state.review!.findings.findings.filter((f) => drafts().findings[f.id]?.included).length;
  // The message box shows the user's typing until it is saved, then whatever the drafts say.
  const [message, setMessage] = createSignal(() => drafts().message);
  const [pendingEvent, setPendingEvent] = createSignal<"APPROVE" | "COMMENT" | null>(null);
  const event = () => pendingEvent() ?? drafts().event;
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal("");
  const locked = () => store.locked();
  const nothing = () => comments() === 0 && event() === "COMMENT";

  async function run(work: () => Promise<unknown>) {
    setBusy(true);
    try {
      await work();
      setError("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function choose(next: "APPROVE" | "COMMENT") {
    setPendingEvent(next);
    await run(() => store.changeDraft({ op: next === "APPROVE" ? "approve" : "comment" }));
    setPendingEvent(null);
  }

  // Prompting Time confirms first; a cancelled confirmation comes back as the host's error.
  const submit = () => run(() => store.submit());

  // What posts when the message stays empty (whooper-submit): an approval with comments says
  // "Pre-approving to unblock".
  const placeholder = () =>
    event() === "APPROVE" ? (comments() > 0 ? "Pre-approving to unblock" : "No message") : "Message (optional)";

  return (
    <section class="w-submit" aria-label="Submit review">
      <span>
        {comments()} {comments() === 1 ? "comment" : "comments"}
      </span>
      <span class="w-radios" role="radiogroup" aria-label="Review">
        <label>
          <input type="radio" name="event" checked={event() === "COMMENT"} disabled={busy() || locked()} onChange={() => choose("COMMENT")} />
          Comment
        </label>
        <label>
          <input type="radio" name="event" checked={event() === "APPROVE"} disabled={busy() || locked()} onChange={() => choose("APPROVE")} />
          Approve
        </label>
      </span>
      <input
        class="w-message"
        aria-label="Review message"
        placeholder={placeholder()}
        value={message()}
        disabled={locked()}
        onInput={(e) => setMessage(e.currentTarget.value)}
        onChange={(e) => {
          const text = e.currentTarget.value;
          if (text !== drafts().message) void run(() => store.changeDraft({ op: "message", text }));
        }}
      />
      <Button variant="primary" disabled={busy() || locked() || nothing()} onClick={submit}>
        {busy() ? "Submitting…" : "Submit review"}
      </Button>
      <Show when={store.state.submitted}>{(text) => <p class="w-hint w-submit-line">Submitted. Whooper says: {text()}</p>}</Show>
      <Show when={error()}>
        <p class="w-error w-submit-line" role="alert">
          {error()}
        </p>
      </Show>
    </section>
  );
}
