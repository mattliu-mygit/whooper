import { createStore } from "solid-js";
import {
  PROTOCOL_VERSION,
  readHostMessage,
  type DraftChange,
  type HostMessage,
  type PageRequest,
  type QueueItem,
  type Review,
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

export interface PageState {
  status: WhooperStatus | null;
  queue: QueueItem[];
  /** The review on screen, or null for the queue. */
  review: Review | null;
  /** This session's answers to Ask about this, by finding ID; "" holds questions about the whole review. */
  answers: Record<string, { question: string; answer: string }[]>;
  /** Findings the user discarded: left out of the review and folded to one line. */
  discarded: Record<string, boolean>;
  /** What whooper-submit printed after it posted the review on screen. */
  submitted: string | null;
}

export type PageStore = ReturnType<typeof createPageStore>;

/**
 * The page's state, changed only by what the host sends. Each request returns a promise that the
 * host's answer settles: it resolves with the answer, after the answer is in the state, and rejects
 * with the host's error message.
 */
export function createPageStore(link: HostLink) {
  const [state, setState] = createStore<PageState>({
    status: null,
    queue: [],
    review: null,
    answers: {},
    discarded: {},
    submitted: null,
  });
  const waiting = new Map<number, { resolve: (message: HostMessage) => void; reject: (error: Error) => void }>();
  let lastId = 0;

  function apply(message: HostMessage) {
    switch (message.type) {
      case "status":
        setState((s) => {
          s.status = message.status;
          s.queue = message.queue;
        });
        break;
      case "review":
        setState((s) => {
          // Another run starts clean; the same run keeps what this session added.
          if (s.review?.run_id !== message.review.run_id) {
            s.answers = {};
            s.discarded = {};
            s.submitted = null;
          }
          s.review = message.review;
        });
        break;
      case "drafts":
        setState((s) => {
          if (s.review?.run_id === message.run_id) s.review.drafts = message.drafts;
        });
        break;
      case "submit-result":
        setState((s) => {
          if (message.posted && s.review?.run_id === message.run_id) s.submitted = message.message;
        });
        break;
      case "answer":
        setState((s) => {
          if (s.review?.run_id !== message.run_id) return;
          (s.answers[message.finding ?? ""] ??= []).push({ question: message.question, answer: message.answer });
        });
        break;
      case "error":
        break;
    }
  }

  const stop = link.listen((data) => {
    const message = readHostMessage(data);
    if (!message) return;
    apply(message);
    const request = message.reply_to === undefined ? undefined : waiting.get(message.reply_to);
    if (!request) return;
    waiting.delete(message.reply_to!);
    if (message.type === "error") request.reject(new Error(message.message));
    else request.resolve(message);
  });

  function request(body: PageRequest): Promise<HostMessage> {
    const id = ++lastId;
    return new Promise((resolve, reject) => {
      waiting.set(id, { resolve, reject });
      link.post({ ...body, version: PROTOCOL_VERSION, id });
    });
  }

  const runId = () => {
    if (!state.review) throw new Error("no review is open");
    return state.review.run_id;
  };

  return {
    state,
    ready: () => request({ type: "ready" }),
    openReview: (run_id: string) => request({ type: "open-review", run_id }),
    changeDraft: (change: DraftChange) => request({ type: "change-draft", run_id: runId(), change }),
    submit: () => request({ type: "submit", run_id: runId() }),
    ask: (question: string, finding: string | null) => request({ type: "ask", run_id: runId(), question, finding }),
    /** Leaves the finding out of the review and folds its comment to one line. */
    discard: async (finding: string) => {
      await request({ type: "change-draft", run_id: runId(), change: { op: "leave-out", finding } });
      setState((s) => {
        s.discarded[finding] = true;
      });
    },
    unfold: (finding: string) =>
      setState((s) => {
        s.discarded[finding] = false;
      }),
    showQueue: () =>
      setState((s) => {
        s.review = null;
      }),
    dispose: stop,
  };
}
