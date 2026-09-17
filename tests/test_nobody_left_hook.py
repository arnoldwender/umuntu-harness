"""Tests for the live hook, hooks/nobody-left-at-stop.py.

Same pair as the gate's own suite: a set moved in part must be handed back to
the agent at the end of the turn, and the synchronised twin of the same edit
must let the turn end in silence. The hook is run as a Stop subprocess with the
payload on stdin, inside a small git repo with a declared locale cohort; the
exit code, the stdout JSON and the receipt are what is asserted.

    python3 -m pytest tests/test_nobody_left_hook.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# The mutation runner points this at a mutated COPY; the real hook is never rewritten.
HOOK = Path(os.environ.get("NOBODY_LEFT_HOOK_UNDER_TEST")
            or Path(__file__).resolve().parent.parent / "hooks" / "nobody-left-at-stop.py")

COHORTS = """\
[[cohort]]
name = "locales"
members = ["src/i18n/de.json", "src/i18n/en.json", "src/i18n/es.json"]
"""
GREETING = {"de": "Hallo", "en": "Hello", "es": "Hola"}


def sh(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    write(root, "README.md", "# A site\n")
    write(root, ".conduct/cohorts.toml", COHORTS)
    for loc, word in GREETING.items():
        write(root, f"src/i18n/{loc}.json", json.dumps({"greeting": word}, indent=2) + "\n")
    hooks = tmp_path / "nohooks"
    hooks.mkdir()
    sh(root, "init", "-q", "-b", "main")
    sh(root, "config", "core.hooksPath", str(hooks))
    sh(root, "config", "user.email", "gate@example.invalid")
    sh(root, "config", "user.name", "Parity Hook Test")
    sh(root, "config", "commit.gpgsign", "false")
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", "base")
    return root


def touch(root: Path, *locales: str) -> None:
    for loc in locales:
        write(root, f"src/i18n/{loc}.json",
              json.dumps({"greeting": GREETING[loc] + "!"}, indent=2) + "\n")


def stop(root: Path, receipts: Path, active: bool = False, env: dict[str, str] | None = None,
         session: str = "test-session") -> tuple[int, dict | None, str, dict | None]:
    payload = {"session_id": session, "cwd": str(root), "hook_event_name": "Stop",
               "stop_hook_active": active, "last_assistant_message": "done",
               "transcript_path": str(root / "session.jsonl")}
    run_env = {**os.environ, "NOBODY_LEFT_RECEIPTS": str(receipts)}
    run_env.pop("NOBODY_LEFT_HOOK_MODE", None)
    run_env.update(env or {})
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, env=run_env, cwd=str(root),
                       check=False, timeout=90)
    out = json.loads(r.stdout) if r.stdout.strip() else None
    rec = None
    if receipts.is_file():
        rec = json.loads(receipts.read_text(encoding="utf-8").strip().split("\n")[-1])
    return r.returncode, out, r.stderr, rec


def feedback(out: dict | None) -> str:
    return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


# --- the control -------------------------------------------------------------

def test_a_clean_turn_ends_in_silence(repo: Path, tmp_path: Path) -> None:
    """Without this, every test below could pass because the hook always speaks."""
    rc, out, _, rec = stop(repo, tmp_path / "r.jsonl")
    assert rc == 0 and out is None, (rc, out)
    assert rec["verdict"] == "ok" and rec["base"] == "HEAD"


def test_all_three_locales_moved_together_ends_in_silence(repo: Path, tmp_path: Path) -> None:
    touch(repo, "de", "en", "es")
    rc, out, _, rec = stop(repo, tmp_path / "r.jsonl")
    assert rc == 0 and out is None and rec["verdict"] == "ok"


# --- the finding -------------------------------------------------------------

def test_one_locale_moved_alone_is_handed_back_naming_the_siblings(repo: Path, tmp_path: Path) -> None:
    touch(repo, "de")
    rc, out, _, rec = stop(repo, tmp_path / "r.jsonl")
    assert rc == 0
    text = feedback(out)
    assert "cohort-drift" in text and "src/i18n/en.json" in text and "src/i18n/es.json" in text
    assert "nothing half-done" in text
    assert out["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert rec["verdict"] == "finding" and rec["nudged"] is True and rec["checks"] == ["cohort-drift"]


def test_a_key_only_one_locale_carries_is_handed_back(repo: Path, tmp_path: Path) -> None:
    touch(repo, "de", "en", "es")
    write(repo, "src/i18n/de.json", json.dumps({"greeting": "Hallo!", "farewell": "Tschüss"}) + "\n")
    _, out, _, rec = stop(repo, tmp_path / "r.jsonl")
    assert "key-parity" in feedback(out) and "farewell" in feedback(out)


# --- the turn, not the working tree ------------------------------------------

def test_a_drift_committed_during_the_turn_is_still_judged(repo: Path, tmp_path: Path) -> None:
    """The base is where the tree stood when the hook last ran, not HEAD: a locale file
    edited and committed alone during the turn is a drift `--base HEAD` would never see."""
    receipts = tmp_path / "r.jsonl"
    _, out, _, rec = stop(repo, receipts)                 # turn 1: clean, records HEAD
    assert out is None and rec["verdict"] == "ok"
    touch(repo, "de")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "de only")
    _, out, _, rec = stop(repo, receipts)                 # turn 2: base = turn 1's head
    assert "src/i18n/en.json" in feedback(out), (out, rec)
    assert rec["base"] != "HEAD" and len(rec["base"]) == 40


def test_a_base_that_no_longer_exists_falls_back_to_head(repo: Path, tmp_path: Path) -> None:
    receipts = tmp_path / "r.jsonl"
    receipts.write_text(json.dumps({"ts": "2026-01-01T00:00:00Z", "session": "test-ses",
                                    "cwd": str(repo), "head": "0" * 40, "verdict": "ok"}) + "\n",
                        encoding="utf-8")
    touch(repo, "de")
    _, out, _, rec = stop(repo, receipts)
    assert feedback(out) and rec["base"] == "HEAD"


# --- the loop guard ----------------------------------------------------------

def test_when_the_turn_is_already_a_continuation_the_hook_stays_silent(repo: Path, tmp_path: Path) -> None:
    """`stop_hook_active`: the runtime says a stop hook already sent the agent round once."""
    touch(repo, "de")
    rc, out, _, rec = stop(repo, tmp_path / "r.jsonl", active=True)
    assert rc == 0 and out is None, out
    assert rec["verdict"] == "finding" and rec["nudged"] is False and rec["why"] == "stop_hook_active"


def test_the_same_drift_is_not_fed_back_twice(repo: Path, tmp_path: Path) -> None:
    """Second stop, same finding signature: the human is told, the agent is not sent round."""
    receipts = tmp_path / "r.jsonl"
    touch(repo, "de")
    _, out1, _, rec1 = stop(repo, receipts)
    assert feedback(out1) and rec1["nudged"] is True
    _, out2, _, rec2 = stop(repo, receipts)
    assert not feedback(out2) and "systemMessage" in out2, out2
    assert rec2["nudged"] is False and rec2["signature"] == rec1["signature"]


def test_a_different_drift_after_a_first_one_is_fed_back_again(repo: Path, tmp_path: Path) -> None:
    receipts = tmp_path / "r.jsonl"
    touch(repo, "de")
    stop(repo, receipts)
    touch(repo, "en")                                     # now es.json alone is behind
    _, out, _, rec = stop(repo, receipts)
    assert "src/i18n/es.json" in feedback(out) and rec["nudged"] is True


# --- modes -------------------------------------------------------------------

def test_notify_mode_tells_the_human_only(repo: Path, tmp_path: Path) -> None:
    touch(repo, "de")
    _, out, _, rec = stop(repo, tmp_path / "r.jsonl", env={"NOBODY_LEFT_HOOK_MODE": "notify"})
    assert "systemMessage" in out and "hookSpecificOutput" not in out
    assert rec["nudged"] is False


def test_block_mode_uses_the_runtime_s_block_decision(repo: Path, tmp_path: Path) -> None:
    touch(repo, "de")
    _, out, _, rec = stop(repo, tmp_path / "r.jsonl", env={"NOBODY_LEFT_HOOK_MODE": "block"})
    assert out["decision"] == "block" and "src/i18n/en.json" in out["reason"]
    assert rec["mode"] == "block"


# --- fail-open ---------------------------------------------------------------

def test_outside_a_git_repository_the_hook_records_and_ends(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    rc, out, _, rec = stop(plain, tmp_path / "r.jsonl")
    assert rc == 0 and out is None and rec["verdict"] == "not-a-repo"


def test_a_gate_that_cannot_judge_is_a_receipt_not_a_pass(repo: Path, tmp_path: Path) -> None:
    receipts = tmp_path / "r.jsonl"
    receipts.write_text(json.dumps({"ts": "2026-01-01T00:00:00Z", "session": "test-ses",
                                    "cwd": str(repo), "head": "HEAD~999", "verdict": "ok"}) + "\n",
                        encoding="utf-8")
    touch(repo, "de")
    rc, out, _, rec = stop(repo, receipts)
    assert rc == 0
    assert rec["verdict"] in ("finding", "gate-failure")   # never "ok" over a drift


def test_a_missing_gate_fails_open_with_a_receipt(repo: Path, tmp_path: Path) -> None:
    touch(repo, "de")
    rc, out, _, rec = stop(repo, tmp_path / "r.jsonl", env={"NOBODY_LEFT_GATE": str(tmp_path / "none.py")})
    assert rc == 0 and out is None and rec["verdict"] in ("error", "gate-failure")


def test_receipts_can_be_switched_off(repo: Path, tmp_path: Path) -> None:
    touch(repo, "de")
    rc, out, _, _ = stop(repo, tmp_path / "r.jsonl", env={"NOBODY_LEFT_RECEIPTS": "off"})
    assert rc == 0 and feedback(out)
    assert not (tmp_path / "r.jsonl").exists()


def test_the_allowlist_of_the_working_repository_is_honoured(repo: Path, tmp_path: Path) -> None:
    write(repo, ".conduct/parity-allow.txt", "src/i18n/*\n")
    touch(repo, "de")
    _, out, _, rec = stop(repo, tmp_path / "r.jsonl")
    assert out is None and rec["verdict"] == "ok"


def test_other_events_are_ignored(repo: Path, tmp_path: Path) -> None:
    receipts = tmp_path / "r.jsonl"
    payload = {"session_id": "s", "cwd": str(repo), "hook_event_name": "PreToolUse", "tool_name": "Bash"}
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                       text=True, env={**os.environ, "NOBODY_LEFT_RECEIPTS": str(receipts)}, check=False)
    assert r.returncode == 0 and not r.stdout.strip() and not receipts.exists()


def test_the_feedback_stays_far_below_the_runtime_cap(repo: Path, tmp_path: Path) -> None:
    for i in range(12):
        write(repo, f"src/i18n/de.json", json.dumps({f"k{j}": j for j in range(i + 1)}) + "\n")
    touch(repo, "de")
    write(repo, "src/i18n/de.json", json.dumps({f"key{j}": j for j in range(40)}) + "\n")
    _, out, _, _ = stop(repo, tmp_path / "r.jsonl")
    assert 0 < len(feedback(out)) < 3000
