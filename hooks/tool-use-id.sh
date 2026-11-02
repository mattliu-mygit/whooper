#!/usr/bin/env bash
# PostToolUse and PostToolUseFailure hook for Bash in review runs. The CLI does not show the model
# its tool-use IDs, and a finding must cite the IDs of the Bash calls that prove it (see
# prompts/review.md), so this adds each call's ID and the start of its command to the model's context.
set -euo pipefail

exec jq -c '{hookSpecificOutput: {hookEventName: .hook_event_name,
  additionalContext: "Bash tool_use_id \(.tool_use_id) ran: \(.tool_input.command | .[0:200])"}}'
