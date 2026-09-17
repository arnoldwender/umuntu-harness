#!/usr/bin/env python3
"""The Umuntu Harness — nobody left behind, checked when the agent stops.

A Claude Code `Stop` hook. When the agent finishes its turn, this runs the
parity gate (`gate/nobody_left.py`) over what the turn changed and, if a set
moved in part — `de.json` edited, `en.json` and `es.json` left behind — hands
the finding back to the agent so the siblings come along before the turn is
over. See hooks/README.md for the wiring and the README for why.

    "hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "python3 /abs/path/to/umuntu-harness/hooks/nobody-left-at-stop.py",
         "timeout": 30}]}]}

WHY AT STOP AND NOT BEFORE EACH EDIT
------------------------------------
A `PreToolUse` hook sees one file. Parity is a property of a SET: the moment
`de.json` is edited, its siblings are not yet behind — the agent may be about
to open them. Judging the single edit would fire on every well-ordered turn.
The turn's end is the first moment the question has an answer, and it is also
the moment HARAMBEE rule 2 names: *nothing half-done — every case and every
locale synced, the files left consistent with one another.*

WHAT IT DOES
------------
1. Reads the Stop payload from stdin: `cwd`, `session_id`, `stop_hook_active`.
2. Works out what THIS TURN changed. The base is the commit the tree was at
   when this hook last ran for this session and directory (kept in the
   receipts); the first time, `HEAD`. So a locale file the agent edited and
   COMMITTED alone during the turn is still judged — `--base HEAD` alone would
   read a committed drift as a clean tree.
3. Runs the gate as a subprocess, `HARNESS_ROOT` = the working directory, so
   cohorts, autodetection and the allowlist are the working repository's. The
   gate's exit contract is honoured as evidence: 0 clean, 1 findings, 2 the
   gate could not judge (no git, no base) — the hook then stays silent and
   says so in its receipt, never "clean".
4. On findings, in the default `feedback` mode, prints
   `{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "…"}}`.
   Claude Code keeps the conversation going once with that text, labelled as
   hook feedback, so the agent can bring the siblings along.
5. Appends one receipt per run to `NOBODY_LEFT_RECEIPTS` (default
   `~/.local/state/umuntu-harness/nobody-left-receipts.jsonl`; `off` disables):
   `{ts, session, cwd, head, verdict, checks, findings, ms}`.

THE LOOP GUARD, BECAUSE A STOP HOOK CAN TALK FOREVER
----------------------------------------------------
A Stop hook that keeps the conversation going has to know when to stop itself.
Three brakes, and each has a test:
* `stop_hook_active` — set by the runtime when the turn is already a
  continuation caused by a stop hook. This hook then records and stays silent:
  one nudge per turn, never a second.
* The same drift is not fed back twice. If the finding signature equals the
  one this session was already told about, the hook switches to `notify` —
  the human sees it, the agent is not sent round again.
* The runtime caps consecutive continuations at eight, whatever the hook says.

MODES
-----
`NOBODY_LEFT_HOOK_MODE=feedback` (default) — `additionalContext`: the agent is
told and continues once. `notify` — `systemMessage` only: the human sees the
finding, the agent is not steered. `block` — `decision: "block"` with the
finding as the reason, the runtime's stronger form of the same continuation.
Any error of the hook's own is a receipt with `verdict: error` and exit 0; the
hook is never the reason a session cannot end.

WHAT IT DOES NOT SEE
--------------------
Whatever the gate does not: a translation that arrived but is wrong, a set
nobody declared and autodetection cannot see, a caller reached through
`getattr`. And a turn that ends by user interrupt: Stop does not fire then.

Tests: tests/test_nobody_left_hook.py · mutants: tests/mutation_check_nobody_left_hook.py
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
GATE = pathlib.Path(os.environ.get("NOBODY_LEFT_GATE") or HERE.parent / "gate" / "nobody_left.py")


def _default_receipts() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "umuntu-harness", "nobody-left-receipts.jsonl")


RECEIPTS = os.environ.get("NOBODY_LEFT_RECEIPTS") or _default_receipts()
MODE = os.environ.get("NOBODY_LEFT_HOOK_MODE", "feedback")       # feedback | notify | block
RECEIPT_TAIL_BYTES = 262_144
MAX_SHOWN = 4


# --- receipts ----------------------------------------------------------------

def receipt(**row: object) -> None:
    """One JSON line per run. Paths, counts and a commit sha — never file contents."""
    if RECEIPTS == "off":
        return
    try:
        pathlib.Path(RECEIPTS).parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **row}
        with open(RECEIPTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — a receipt never brings the hook down
        pass


def last_receipt(session: str, cwd: str) -> dict | None:
    """This session's last receipt for this directory, from the tail of the file."""
    if RECEIPTS == "off":
        return None
    try:
        with open(RECEIPTS, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - RECEIPT_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for raw in reversed(tail.split("\n")):
        if session not in raw or cwd not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("session") == session and r.get("cwd") == cwd and "head" in r:
            return r
    return None


# --- git ---------------------------------------------------------------------

def git(cwd: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def turn_base(cwd: str, previous: dict | None) -> str:
    """The commit the tree was at when this hook last ran here — if it still exists.
    Otherwise HEAD: the hook will not pretend to know a history it cannot see."""
    prev = str((previous or {}).get("head") or "")
    if prev and subprocess.run(["git", "-C", cwd, "cat-file", "-e", f"{prev}^{{commit}}"],
                               capture_output=True, check=False).returncode == 0:
        return prev
    return "HEAD"
# mutation-anchor: turn_base


# --- the gate ----------------------------------------------------------------

def judge(cwd: str, base: str) -> tuple[int, str, str]:
    """Run the gate over the working tree of `cwd` against `base`. Returns
    (exit code, stdout, stderr) — the gate's contract is the evidence."""
    env = {**os.environ, "HARNESS_ROOT": cwd}
    r = subprocess.run([sys.executable, str(GATE), "--base", base],
                       capture_output=True, text=True, env=env, cwd=cwd, check=False, timeout=25)
    return r.returncode, r.stdout, r.stderr


def findings_of(stdout: str) -> list[str]:
    return [line.strip()[5:] for line in stdout.split("\n") if line.strip().startswith("FAIL ")]


def signature(findings: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(findings)).encode("utf-8")).hexdigest()[:16]


def message(findings: list[str]) -> str:
    shown = findings[:MAX_SHOWN]
    more = f" (+{len(findings) - MAX_SHOWN} more)" if len(findings) > MAX_SHOWN else ""
    return ("nobody-left: this turn moved a set in part. " + " ".join(shown) + more
            + " HARAMBEE 2, nothing half-done: bring the siblings along before you finish — "
            "the same keys in every locale, the callers moved with the signature. If the set "
            "is deliberately partial, say so and name it in .conduct/parity-allow.txt.")


# --- main --------------------------------------------------------------------

def main() -> int:
    t0 = time.time()
    payload = json.loads(sys.stdin.read() or "{}")
    if payload.get("hook_event_name") not in (None, "Stop", "SubagentStop"):
        return 0
    cwd = str(payload.get("cwd") or os.getcwd())
    session = str(payload.get("session_id") or "")[:8]
    common = {"session": session, "cwd": cwd, "mode": MODE}

    head = git(cwd, "rev-parse", "HEAD")
    if not head:
        receipt(verdict="not-a-repo", **common)
        return 0
    previous = last_receipt(session, cwd)
    base = turn_base(cwd, previous)
    code, out, err = judge(cwd, base)
    ms = int((time.time() - t0) * 1000)

    if code == 2:
        receipt(verdict="gate-failure", head=head, base=base, error=err.strip()[:200], ms=ms, **common)
        return 0
    findings = findings_of(out) if code == 1 else []
    if not findings:
        receipt(verdict="ok", head=head, base=base, ms=ms, **common)
        return 0

    sig = signature(findings)
    checks = sorted({f.split("]", 1)[0].lstrip("[") for f in findings if f.startswith("[")})
    already = bool(payload.get("stop_hook_active"))
    # mutation-anchor: stop_hook_active
    repeated = bool(previous) and previous.get("signature") == sig
    # mutation-anchor: repeated
    mode = MODE
    if already:
        receipt(verdict="finding", nudged=False, why="stop_hook_active", head=head, base=base,
                checks=checks, findings=len(findings), signature=sig, ms=ms, **common)
        return 0
    if repeated and mode != "notify":
        mode = "notify"

    receipt(verdict="finding", nudged=mode != "notify", head=head, base=base, checks=checks,
            findings=len(findings), signature=sig, ms=ms, **common)
    text = message(findings)
    if mode == "block":
        print(json.dumps({"decision": "block", "reason": text}, ensure_ascii=False))
    elif mode == "notify":
        print(json.dumps({"systemMessage": text}, ensure_ascii=False))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop",
                                                 "additionalContext": text}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open on purpose: the hook never keeps a session from ending
        receipt(verdict="error", error=f"{type(exc).__name__}: {exc}"[:200])
        sys.exit(0)
