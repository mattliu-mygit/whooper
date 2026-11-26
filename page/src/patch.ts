import { parsePatchFiles, type FileDiffMetadata } from "@pierre/diffs";
import type { Finding } from "./protocol";

/** One file of diff.patch, as the file list and the diff show it. */
export interface PatchFile {
  path: string;
  /** A added, D deleted, R renamed, M changed, as in git's status letters. */
  status: "A" | "D" | "R" | "M";
  additions: number;
  deletions: number;
  diff: FileDiffMetadata;
}

const STATUS: Record<FileDiffMetadata["type"], PatchFile["status"]> = {
  new: "A",
  deleted: "D",
  "rename-pure": "R",
  "rename-changed": "R",
  change: "M",
};

/** The files of a review's diff.patch, in the patch's order. */
export function readPatch(patch: string): PatchFile[] {
  return parsePatchFiles(patch)
    .flatMap((parsed) => parsed.files)
    .map((diff) => ({
      path: diff.name,
      status: STATUS[diff.type],
      additions: diff.hunks.reduce((sum, hunk) => sum + hunk.additionLines, 0),
      deletions: diff.hunks.reduce((sum, hunk) => sum + hunk.deletionLines, 0),
      diff,
    }));
}

/**
 * Whether the finding's line shows in the file's diff, so its comment can sit under that line. A
 * patch holds only its hunks, so a line outside them has no row; GitHub takes inline comments only
 * on lines of the diff too. RIGHT lines count in the new file, LEFT lines in the old one.
 */
export function showsLine(file: PatchFile, finding: Finding): boolean {
  return file.diff.hunks.some((hunk) =>
    finding.side === "LEFT"
      ? finding.line >= hunk.deletionStart && finding.line < hunk.deletionStart + hunk.deletionCount
      : finding.line >= hunk.additionStart && finding.line < hunk.additionStart + hunk.additionCount,
  );
}
