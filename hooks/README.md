# Hooks — keeping the disciplines present

The Codex only works if it's *in context* when the agent acts. A one-time paste into
`AGENTS.md` works; a hook makes it automatic, every session, and opens each run with the
maxim and a proverb.

## `session-start.sh`

Emits, to stdout:

1. The **fixed maxim** (the heart of Ubuntu) + a rotating **proverb of the day**
   (`bin/proverb`, drawn from `proverbs.txt`).
2. The **conduct block** — the four disciplines, precedence, and the hard limit
   (`codex-block.md`).

It's harness-agnostic: any harness that can run a command at session start can use it, and
its stdout is plain readable text.

## Wiring it into Claude Code

Claude Code injects a `SessionStart` hook's stdout into the session context. Add to your
`settings.json` (use the **absolute** path, and check your Claude Code version's hook docs —
the schema evolves):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "/abs/path/to/umuntu-harness/hooks/session-start.sh" }
        ]
      }
    ]
  }
}
```

## Wiring it into any other harness

Run `hooks/session-start.sh` as the first step of your session bootstrap and prepend its
output to the system prompt. The maxim goes first, the disciplines stay present.

## `nobody-left-at-stop.py` — the parity gate, when the agent stops

A Claude Code `Stop` hook. When the agent finishes its turn, it runs
[`gate/nobody_left.py`](../gate/nobody_left.py) over what the turn changed and, if a set moved
in part — `de.json` edited, `en.json` and `es.json` left behind — hands the finding back so the
siblings come along before the turn is over:

> nobody-left: this turn moved a set in part. [cohort-drift] src/i18n/de.json: cohort "locales"
> (declared): src/i18n/de.json changed, but src/i18n/en.json, src/i18n/es.json did not — nobody
> gets left behind. HARAMBEE 2, nothing half-done: bring the siblings along before you finish …

Why at Stop and not before each edit: parity is a property of a set. The moment `de.json` is
edited its siblings are not yet behind — the agent may be about to open them. The end of the
turn is the first moment the question has an answer.

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command",
            "command": "python3 /abs/path/to/umuntu-harness/hooks/nobody-left-at-stop.py",
            "timeout": 30 }
        ]
      }
    ]
  }
}
```

It judges **the turn**, not the working tree: the base is the commit the tree stood at when
the hook last ran for this session and directory (kept in its receipts), so a locale file
edited and committed alone during the turn is still caught — `--base HEAD` alone would read a
committed drift as a clean tree. The gate runs with the working directory as its root, so
cohorts, autodetection and `.conduct/parity-allow.txt` are the working repository's.

A Stop hook that keeps the conversation going has to know when to stop itself. Three brakes:
`stop_hook_active` (the runtime says a stop hook already sent the agent round — this one then
records and stays silent, one nudge per turn); the same drift is never fed back twice (the
human is told instead); and the runtime's own cap of eight continuations. Modes:
`NOBODY_LEFT_HOOK_MODE=feedback` (default — `additionalContext`, the agent continues once),
`notify` (`systemMessage`, the human sees it, the agent is not steered), `block` (the
runtime's `decision: "block"`). Receipts in `~/.local/state/umuntu-harness/nobody-left-receipts.jsonl`
(`NOBODY_LEFT_RECEIPTS=…` to move, `off` to disable). Any error of its own is a receipt and
exit 0 — the hook is never the reason a session cannot end.

Tests: [`tests/test_nobody_left_hook.py`](../tests/test_nobody_left_hook.py) ·
mutants: [`tests/mutation_check_nobody_left_hook.py`](../tests/mutation_check_nobody_left_hook.py).

## Just want to see it?

```sh
./hooks/session-start.sh
```
