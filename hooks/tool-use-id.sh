#!/usr/bin/env bash
# PostToolUse hook for the shell tool in review runs: Bash in Claude Code (also its
# PostToolUseFailure), exec in Devin. Neither CLI shows the model its tool-use IDs, and a finding
# must cite the IDs of the shell calls that prove it (see prompts/review.md), so this adds each
# call's ID and the start of its command to the model's context.
set -euo pipefail

exec jq -c '{hookSpecificOutput: {hookEventName: .hook_event_name,
  additionalContext: "\(.tool_name) tool_use_id \(.tool_use_id) ran: \(.tool_input.command | .[0:200])"}}'
