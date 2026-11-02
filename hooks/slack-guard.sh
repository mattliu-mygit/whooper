#!/usr/bin/env bash
# PreToolUse hook for the Slack connector's tools in Whooper's runs. It lets through only the two
# Slack writes the user approved, with the arguments the calling script pinned, and blocks every
# other Slack tool:
# - slack_add_reaction: eyes on the review ask the run took, in $WHOOPER_ASK_CHANNEL at
#   $WHOOPER_ASK_TS;
# - slack_schedule_message: to $WHOOPER_SUMMARY_CHANNEL, not in a thread, with exactly the text in
#   $WHOOPER_SUMMARY_FILE at $WHOOPER_SUMMARY_POST_AT, once per file.
# Exit code 2 blocks the call and shows the run the reason.
set -euo pipefail
# Claude Code lets a call through when its hook fails with any code but 2, such as an unset
# variable's 1, so every failure here blocks.
trap 'status=$?; ((status == 0)) || exit 2' EXIT

eval "$(python3 -I "$(dirname "$0")/../lib/whooper.py" env)"
input="$(cat)"

deny() {
  echo "slack-guard: $*" >&2
  exit 2
}

tool="$(jq -r .tool_name <<<"$input")"
case "$tool" in
  mcp__claude_ai_Slack__slack_add_reaction)
    [[ -n "${WHOOPER_ASK_TS:-}" ]] || deny "this run took no Slack ask"
    jq -e --arg channel "$WHOOPER_ASK_CHANNEL" --arg ts "$WHOOPER_ASK_TS" \
      '.tool_input == {channel_id: $channel, message_ts: $ts, emoji: "eyes"}' <<<"$input" >/dev/null ||
      deny "only eyes on message $WHOOPER_ASK_TS in $WHOOPER_ASK_CHANNEL"
    ;;
  mcp__claude_ai_Slack__slack_schedule_message)
    [[ -n "${WHOOPER_SUMMARY_FILE:-}" ]] || deny "this run has no summary to post"
    jq -e --arg channel "$WHOOPER_SUMMARY_CHANNEL" --rawfile message "$WHOOPER_SUMMARY_FILE" \
      --argjson post_at "$WHOOPER_SUMMARY_POST_AT" \
      '.tool_input == {channel_id: $channel, message: $message, post_at: $post_at}' <<<"$input" >/dev/null ||
      deny "only the composed summary, to $WHOOPER_SUMMARY_CHANNEL at $WHOOPER_SUMMARY_POST_AT, with no thread"
    # noclobber makes the redirect fail when the file exists, so a second identical call is blocked
    # and the channel never gets the summary twice.
    (set -o noclobber && : > "$WHOOPER_SUMMARY_FILE.scheduled") 2>/dev/null || deny "the summary was already scheduled"
    ;;
  *) deny "$tool is not a Slack call Whooper may make" ;;
esac
