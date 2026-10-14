#!/bin/sh
# Second agent for the comparison: Claude Code, non-interactive, with shell tools limited to HTTP and text filters.
exec claude -p --model sonnet --strict-mcp-config --allowedTools "Bash(curl:*)" "Bash(jq:*)" "Bash(grep:*)" "Bash(head:*)" "Bash(tail:*)" "Bash(wc:*)" "Bash(cat:*)" "Bash(sed:*)" "Bash(tr:*)" "Bash(sort:*)" "Bash(cut:*)" "Bash(echo:*)" "Bash(printf:*)" "Bash(date:*)"
