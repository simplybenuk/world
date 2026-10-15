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
exec opencode run --pure --model "${WORLD_OPENCODE_MODEL:-opencode/nemotron-3-ultra-free}" "$(cat)"
