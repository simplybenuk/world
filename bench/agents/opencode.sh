#!/bin/sh
# Third agent for the comparison: opencode, non-interactive, with a model from another family than Codex and Claude.
# As for Claude Code, the shell is limited to HTTP and text filters, and every other tool is denied.
# The default model is one of opencode's free ones, so these runs need no account. The free tier refuses
# requests without its read tool, so read stays, limited to the agent's empty workspace by external_directory.
# A home of its own inside the throwaway workspace, so that no user configuration or skills reach the agent.
export HOME="$PWD/.home" XDG_CONFIG_HOME="$PWD/.home/.config" XDG_DATA_HOME="$PWD/.home/.local/share" XDG_CACHE_HOME="$PWD/.home/.cache"
mkdir -p "$HOME"
export OPENCODE_CONFIG_CONTENT='{
  "$schema": "https://opencode.ai/config.json",
  "share": "disabled",
  "autoupdate": false,
  "permission": {
    "edit": "deny", "glob": "deny", "grep": "deny", "list": "deny", "task": "deny",
    "webfetch": "deny", "websearch": "deny", "codesearch": "deny", "external_directory": "deny",
    "bash": {
      "*": "deny",
      "curl *": "allow", "jq *": "allow", "grep *": "allow", "head *": "allow", "tail *": "allow", "wc *": "allow",
      "cat": "allow", "sed *": "allow", "tr *": "allow", "sort *": "allow", "cut *": "allow", "echo *": "allow",
      "printf *": "allow", "date": "allow", "date *": "allow"
    }
  }
}'
# The free tier's provider is sometimes overloaded or rate-limited, before or part way through a run. opencode
# then retries quietly, so its error log is watched, and the run is stopped at the first provider error.
# Exit 75 then, and the runner discards the run and repeats it from the start.
PROVIDER_ERROR='level=ERROR.*(providerID=|AI_APICallError|AI_RetryError)'
prompt="$(cat)"
opencode run --pure --print-logs --log-level ERROR --model "${WORLD_OPENCODE_MODEL:-opencode/mimo-v2.6-flash-free}" "$prompt" \
  >"$PWD/.home/stdout" 2>"$PWD/.home/stderr" &
agent=$!
while kill -0 "$agent" 2>/dev/null; do
  if grep -qE "$PROVIDER_ERROR" "$PWD/.home/stderr"; then
    kill "$agent"
    wait "$agent"
    cat "$PWD/.home/stderr" >&2
    exit 75
  fi
  sleep 2
done
wait "$agent"
status=$?
cat "$PWD/.home/stderr" >&2
cat "$PWD/.home/stdout"
if grep -qE "$PROVIDER_ERROR" "$PWD/.home/stderr"; then
  exit 75
fi
# opencode prints each tool call to standard error, marked "$ " or "✗ ". No answer and no tool call is a run that never started.
if [ ! -s "$PWD/.home/stdout" ] && ! grep -qE '(\$|✗) ' "$PWD/.home/stderr"; then
  exit 75
fi
exit $status
