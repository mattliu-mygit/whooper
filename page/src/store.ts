import { createSignal, createStore } from "solid-js";
import {
  CallError,
  PROTOCOL_VERSION,
  readHostMessage,
  type Drafts,
  type DraftChange,
  type Review,
  type Shown,
  type StatusReview,
  type WhooperStatus,
} from "./protocol";

/** How the page reaches its host: in Prompting Time, the parent window; in tests, the fake host. */
export interface HostLink {
  post(message: unknown): void;
  /** Calls receive with each message the host sends, until the returned function is called. */
  listen(receive: (data: unknown) => void): () => void;
}

/** The parent window. Only messages from it count, and the page posts only to it. The page's
 * sandbox gives it an opaque origin and no say over its parent, so it posts to any origin. */
export function parentLink(): HostLink {
  return {
    post: (message) => window.parent.postMessage(message, "*"),
    listen: (receive) => {
      const onMessage = (event: MessageEvent) => {
        if (event.source === window.parent) receive(event.data);
      };
      window.addEventListener("message", onMessage);
      return () => window.removeEventListener("message", onMessage);
    },
  };
}

/** What a route shows: the list of reviews, the past reviews, the causes that stop every review, the
 * settings, one review, or one cause. */
export type Place =
  | { kind: "home" }
  | { kind: "past" }
  | { kind: "causes" }
  | { kind: "settings" }
  | { kind: "review"; runId: string }
  | { kind: "cause"; cause: string }
  | null;

/** The place a route names: / for the reviews, which Prompting Time opens from Whooper's name, /past,
 * /causes, and /settings for the page's other tabs, and the routes of whooper-status's items and
 * blockers; null for any other route. */
export function placeOf(route: string | null): Place {
  if (route === "/") return { kind: "home" };
  if (route === "/past") return { kind: "past" };
  if (route === "/causes") return { kind: "causes" };
  if (route === "/settings") return { kind: "settings" };
  const match = route ? /^\/(reviews|causes)\/([^/]+)$/.exec(route) : null;
  if (!match) return null;
  const id = decodeURIComponent(match[2]);
  return match[1] === "reviews" ? { kind: "review", runId: id } : { kind: "cause", cause: id };
}

export interface PageState {
  /** The route Prompting Time opened, or the page moved to since, or null until it opens one. */
  route: string | null;
  /** What whooper-status printed last, or null before the first read. */
  status: WhooperStatus | null;
  /** What whooper-show printed for the open review, or null while it is read. */
  shown: Shown | null;
  /** The open review, when it finished with findings, which the diff draws. */
  review: Review | null;
  /** Why the last read failed, or null. */
  loadError: string | null;
  /** This session's answers to Ask about this, by finding ID; "" holds questions about the whole review. */
  answers: Record<string, { question: string; answer: string }[]>;
  /** Findings the user discarded: left out of the review and folded to one line. */
  discarded: Record<string, boolean>;
  /** What whooper-submit printed after it posted the review on screen. */
  submitted: string | null;
}

export type PageStore = ReturnType<typeof createPageStore>;

/** The review whose findings the diff draws, from what whooper-show printed, or null for a review that
 * runs, waits, or failed, which has none. */
function reviewOf(shown: Shown): Review | null {
  if (shown.outcome !== "reviewed" || !shown.findings || !shown.drafts || !shown.run) return null;
  return {
    run_id: shown.run_id,
    repo: shown.run.repo,
    pr: shown.run.pr,
    title: shown.title,
    author: shown.author,
    findings: shown.findings,
    drafts: shown.drafts,
    patch: shown.diff ?? "",
    overview: shown.overview,
    commits_since_review: shown.commits_since_review,
    co_change: shown.co_change,
    connections: shown.connections,
    test_gaps: shown.test_gaps,
    examples: shown.examples,
  };
}

/** How long the action buttons stay disabled after an action's reply, or after an open that arrives
 * in that time: long enough for Prompting Time to open the next item the action moved past. */
export const HOLD_MS = 600;

const message = (error: unknown) => (error instanceof Error ? error.message : String(error));

/**
 * The page's state, read from Whooper through the host with page protocol 2: each read and change
 * calls one of the manifest's actions and returns a promise that settles with what it printed, or
 * rejects with the host's error message. The page shows the route Prompting Time opens, and reads
 * again when files in Whooper's state folder change.
 */
export function createPageStore(link: HostLink) {
  const [state, setState] = createStore<PageState>({
    route: null,
    status: null,
    shown: null,
    review: null,
    loadError: null,
    answers: {},
    discarded: {},
    submitted: null,
  });
  const waiting = new Map<number, { resolve: (json: unknown) => void; reject: (error: Error) => void }>();
  let lastId = 0;

  function call(action: string, args: string[] = []): Promise<unknown> {
    const id = ++lastId;
    return new Promise((resolve, reject) => {
      waiting.set(id, { resolve, reject });
      link.post({ version: PROTOCOL_VERSION, type: "call", id, action, args });
    });
  }

  // The open route, kept beside the store, which applies a change only at its next flush, so the
  // reads and calls that follow a change see the new route.
  let route: string | null = null;
  const place = () => placeOf(state.route);
  /** The run the page shows, read from the store, for what the page draws. */
  const shownRun = () => {
    const current = place();
    return current?.kind === "review" ? current.runId : null;
  };
  const openRun = () => {
    const current = placeOf(route);
    return current?.kind === "review" ? current.runId : null;
  };
  const runId = () => {
    const id = openRun();
    if (!id) throw new Error("no review is open");
    return id;
  };

  async function readStatus() {
    try {
      const status = (await call("status")) as WhooperStatus;
      setState((s) => {
        s.status = status;
      });
    } catch (e) {
      setState((s) => {
        s.loadError = message(e);
      });
    }
  }

  /** Reads the open review again; what arrives after the user opened another route is dropped. */
  async function readReview(id: string) {
    try {
      const shown = (await call("show-review", [id])) as Shown;
      if (openRun() !== id) return;
      setState((s) => {
        s.shown = shown;
        s.review = reviewOf(shown);
        s.loadError = null;
      });
    } catch (e) {
      if (openRun() === id)
        setState((s) => {
          s.loadError = message(e);
        });
    }
  }

  async function readDrafts(id: string) {
    try {
      const drafts = (await call("draft", [id])) as Drafts;
      setDrafts(id, drafts);
    } catch {
      // whooper-show reads the drafts again on the next change.
    }
  }

  function setDrafts(id: string, drafts: Drafts) {
    setState((s) => {
      if (s.review?.run_id === id) s.review.drafts = drafts;
    });
  }

  // While an action the user pressed runs, and for HOLD_MS after its reply, the page holds every
  // action button disabled. Prompting Time may open the next item in that time, and an open in that
  // time holds them HOLD_MS more, so a second click can't act on the item it opened. Ask, which can
  // take Whooper's agent a long time, holds only the Ask buttons while it runs, and every button for
  // HOLD_MS after its reply. The plain variables decide; the signals only draw, since the store
  // applies a write at its next flush.
  const [held, setHeld] = createSignal(false);
  const [asking, setAsking] = createSignal(false);
  let running = 0;
  let asks = 0;
  let release: ReturnType<typeof setTimeout> | undefined;
  function holdFor(ms: number) {
    clearTimeout(release);
    release = setTimeout(() => {
      release = undefined;
      setHeld(false);
    }, ms);
  }
  async function holding<T>(work: () => Promise<T>): Promise<T> {
    running++;
    clearTimeout(release);
    release = undefined;
    setHeld(true);
    try {
      return await work();
    } finally {
      if (--running === 0) holdFor(HOLD_MS);
    }
  }
  /** Runs work holding only the Ask buttons, then holds every button HOLD_MS after its reply. */
  async function holdingAsk<T>(work: () => Promise<T>): Promise<T> {
    asks++;
    setAsking(true);
    try {
      return await work();
    } finally {
      if (--asks === 0) setAsking(false);
      // An action still running starts the hold when it ends.
      if (running === 0) {
        setHeld(true);
        holdFor(HOLD_MS);
      }
    }
  }

  /** Shows route: a review starts clean, with nothing this session added, and reads its record. The
   * route already open, as when the user opens the same row again, only reads again, which keeps what
   * the user typed. */
  function open(next: string) {
    if (release !== undefined) holdFor(HOLD_MS);
    if (next === route) {
      filesChanged(null);
      return;
    }
    route = next;
    setState((s) => {
      s.route = next;
      s.shown = null;
      s.review = null;
      s.loadError = null;
      s.answers = {};
      s.discarded = {};
      s.submitted = null;
    });
    const id = openRun();
    void readStatus();
    if (id) void readReview(id);
  }

  /** Reads again what the changed files bear on: the status always, and the open review when one of
   * its run's files changed, only its drafts when they alone did, which keeps the diff as it is. */
  function filesChanged(files: string[] | null) {
    void readStatus();
    const id = openRun();
    if (!id) return;
    const mine = files === null ? null : files.filter((file) => file.startsWith(`runs/${id}/`));
    if (mine !== null && mine.length === 0) return;
    if (mine !== null && mine.every((file) => file === `runs/${id}/drafts.json`)) void readDrafts(id);
    else void readReview(id);
  }

  const stop = link.listen((data) => {
    const host = readHostMessage(data);
    if (!host) return;
    switch (host.type) {
      case "open":
        open(host.route);
        break;
      case "changed":
        filesChanged(host.files);
        break;
      case "result":
      case "error": {
        const request = host.reply_to === undefined ? undefined : waiting.get(host.reply_to);
        if (!request) return;
        waiting.delete(host.reply_to!);
        if (host.type === "error") request.reject(new CallError(host.message, { stage: host.stage, exit_code: host.exit_code, stderr: host.stderr }));
        else request.resolve(host.json);
        break;
      }
    }
  });

  /** The open review as whooper-status lists it, or null before the status arrives or once the review
   * is older than the reviews it lists. */
  const entry = (): StatusReview | null => state.status?.reviews.find((review) => review.run_id === shownRun()) ?? null;
  /** Every run of the open review's PR that whooper-status lists, oldest first, so the first is Review 1. */
  const versions = (): StatusReview[] => {
    const open = entry();
    if (!open) return [];
    return state.status!.reviews.filter((review) => review.repo === open.repo && review.pr === open.pr).reverse();
  };
  /** Whether a newer run of the PR supersedes the open one, which then only shows. */
  const earlier = () => {
    const all = versions();
    return all.length > 0 && all[all.length - 1].run_id !== shownRun();
  };

  const printedText = (printed: unknown) => (typeof printed === "string" ? printed.trim() : JSON.stringify(printed));

  /** Runs an action, holding the action buttons, and reads the status again; resolves with what it printed. */
  async function act(action: string, args: string[]): Promise<string> {
    const printed = await holding(() => call(action, args));
    void readStatus();
    return printedText(printed);
  }

  // A round can take minutes, since it reviews what it finds, and opens nothing in Prompting Time, so
  // it holds no button but the Poll now buttons, which say so.
  const [rounds, setRounds] = createSignal(0);
  async function round(): Promise<string> {
    setRounds((n) => n + 1);
    try {
      const printed = await call("round");
      void readStatus();
      return printedText(printed);
    } finally {
      setRounds((n) => n - 1);
    }
  }

  return {
    state,
    place,
    entry,
    versions,
    earlier,
    /** Whether the action buttons are disabled: while an action other than Ask or a round runs, and for HOLD_MS
     * after any action's reply. */
    held,
    /** Whether an Ask runs, which disables only the Ask buttons. */
    asking,
    /** Whether the open review takes no more changes: submitted, or superseded by a newer run. */
    locked: () => state.submitted !== null || state.shown?.submitted != null || earlier(),
    ready: () => link.post({ version: PROTOCOL_VERSION, type: "ready" }),
    /** Shows another route, as a link inside the page does. */
    open,
    changeDraft: async (change: DraftChange) => {
      const id = runId();
      const args = [id, change.op];
      if ("finding" in change) args.push(change.finding);
      if ("text" in change) args.push(change.text);
      const drafts = (await call("draft", args)) as Drafts;
      setDrafts(id, drafts);
      return drafts;
    },
    /** Asks Prompting Time to run whooper-submit, which it does only after the user confirms, with
     * flags such as --approve-anyway that the user chose after a refusal. */
    submit: async (flags: string[] = []) => {
      const id = runId();
      const printed = String(await holding(() => call("submit", [id, ...flags]))).trim();
      setState((s) => {
        if (openRun() === id) s.submitted = printed;
      });
      return printed;
    },
    ask: async (question: string, finding: string | null) => {
      const id = runId();
      const answer = String(await holdingAsk(() => call("ask", [id, question, ...(finding ? ["--finding", finding] : [])]))).trim();
      setState((s) => {
        if (openRun() === id) (s.answers[finding ?? ""] ??= []).push({ question, answer });
      });
      return answer;
    },
    /** Skip, Remove, and Retry act on the open review, or on run `id`, as a list row's menu does. */
    skip: (undo = false, id = runId()) => act("skip", [id, ...(undo ? ["--undo"] : [])]),
    remove: (undo = false, id = runId()) => act("remove", [id, ...(undo ? ["--undo"] : [])]),
    retry: (id = runId()) => act("retry", [id]),
    /** The settings whooper-settings get prints, with Whooper's defaults for what config.toml leaves out. */
    readSettings: async () => (await call("get-settings")) as Record<string, unknown>,
    /** Saves changed settings with whooper-settings set, which checks them and rejects with its reason. */
    saveSettings: (changes: Record<string, unknown>) => call("save-settings", [JSON.stringify(changes)]),
    pause: () => act("pause", []),
    resume: () => act("resume", []),
    /** Runs a round now, which reviews the PRs on the recheck list, such as those that wait. */
    round,
    /** Whether a round runs: one the page asked for, or one whooper-status says runs. */
    polling: () => rounds() > 0 || state.status?.round.running === true,
    /** Leaves the finding out of the review and folds its comment to one line. */
    discard: async (finding: string) => {
      const id = runId();
      setDrafts(id, (await call("draft", [id, "leave-out", finding])) as Drafts);
      setState((s) => {
        s.discarded[finding] = true;
      });
    },
    unfold: (finding: string) =>
      setState((s) => {
        s.discarded[finding] = false;
      }),
    dispose: () => {
      stop();
      clearTimeout(release);
    },
  };
}
