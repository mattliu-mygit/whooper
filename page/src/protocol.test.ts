import { describe, expect, it } from "vitest";
import { status } from "./fixtures";
import { readHostMessage, readPageMessage } from "./protocol";

describe("the page's messages", () => {
  it("reads a host message only at version 1 with its type's fields", () => {
    const drafts = { version: 1, type: "drafts", run_id: "r", drafts: { event: "COMMENT", message: "", findings: {} }, reply_to: 3 };
    expect(readHostMessage(drafts)).toEqual(drafts);
    for (const bad of [
      { ...drafts, version: 2 },
      { ...drafts, type: "run-command" },
      { version: 1, type: "drafts", drafts: drafts.drafts },
      { ...drafts, reply_to: "3" },
      "not an object",
      null,
    ]) {
      expect(readHostMessage(bad)).toBeNull();
    }
  });

  it("reads a status message without queue, which the page does not read and Prompting Time may leave out", () => {
    const answer = { version: 1, type: "status", status: status(), reply_to: 1 };
    expect(readHostMessage(answer)).toEqual(answer);
    expect(readHostMessage({ ...answer, queue: [] })).toEqual({ ...answer, queue: [] });
    expect(readHostMessage({ version: 1, type: "status", queue: [], reply_to: 1 })).toBeNull();
  });

  it("reads a page request only with an id and a change whooper-draft takes", () => {
    const include = { version: 1, id: 1, type: "change-draft", run_id: "r", change: { op: "include", finding: "2" } };
    expect(readPageMessage(include)).toEqual(include);
    expect(readPageMessage({ version: 1, id: 2, type: "ask", run_id: "r", question: "Why?", finding: null })).not.toBeNull();
    for (const bad of [
      { ...include, id: undefined },
      { ...include, change: { op: "delete", finding: "2" } },
      { ...include, change: { op: "text", finding: "2" } },
      { version: 1, id: 3, type: "submit" },
      { version: 1, id: 4, type: "run", command: "rm" },
    ]) {
      expect(readPageMessage(bad)).toBeNull();
    }
  });
});
