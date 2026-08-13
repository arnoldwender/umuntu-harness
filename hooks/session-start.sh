#!/usr/bin/env sh
# The Ubuntu Harness — session-start hook.
#
# Opens every session with the maxim + a rotating proverb, and keeps the four
# disciplines present. Its stdout is meant to be injected into the agent's context
# at the start of a session (e.g. a Claude Code `SessionStart` hook), and it is also
# just readable output for any harness that can run a startup command.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# 1) The maxim + the proverb of the day (bin/proverb).
"$ROOT/bin/proverb"

# 2) The four disciplines — kept present in context, every session.
echo ""
cat "$ROOT/codex-block.md"
