"""Mutation tests for the parity gate.

Every check gets the same treatment: build a repo the gate PASSES, then plant
the one desynchronisation that check exists to catch, and require the gate to go
red — plus the synchronised twin of the same edit, which must stay green. A test
that only ever sees a clean repo proves nothing: it would still pass if the
check were deleted, and `tests/mutation_check.py` deletes them to prove it.

    python3 -m pytest tests/ -q

The gate is invoked as a subprocess rather than imported, because the exit code
is part of the contract the whole conduct-harness family shares (0 clean,
1 findings, 2 the gate itself broke). Importing would test the functions and
leave the contract untested.

Every repo here is built from scratch in a tmp dir and given its own empty
hooks path, so a global hook on the developer's machine cannot change what the
suite measures.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "gate" / "nobody_left.py"

COHORTS = """\
[[cohort]]
name = "locales"
members = ["src/i18n/de.json", "src/i18n/en.json", "src/i18n/es.json"]

[[cohort]]
name = "locale-pages"
pattern = "src/pages/{locale}/**"
values = ["de", "en", "es"]
"""

GREETING = {"de": "Hallo", "en": "Hello", "es": "Hola"}

TOOLS = '''\
"""A module with one exported function and one private helper."""


def render(text, width):
    return _pad(text, width)


def _pad(text, width):
    return text.ljust(width)
'''

CALLER = '''\
from tools import render


def main():
    return render("hallo", 12)
'''


# --- plumbing ----------------------------------------------------------------

def sh(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, text=True, check=True)
    return r.stdout


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def commit_all(root: Path) -> None:
    """Seed the repo with one commit, which every test then diffs against."""
    hooks = root.parent / "nohooks"
    hooks.mkdir(exist_ok=True)
    sh(root, "init", "-q", "-b", "main")
    sh(root, "config", "core.hooksPath", str(hooks))
    sh(root, "config", "user.email", "gate@example.invalid")
    sh(root, "config", "user.name", "Parity Gate Test")
    sh(root, "config", "commit.gpgsign", "false")
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", "base")


def run(root: Path, *args: str, base: str = "HEAD") -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HARNESS_ROOT": str(root)}
    return subprocess.run([sys.executable, str(GATE), "--base", base, *args],
                          capture_output=True, text=True, env=env, check=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with a declared locale cohort, a pattern cohort, and Python."""
    root = tmp_path / "repo"
    write(root, "README.md", "# A site\n")
    write(root, ".conduct/cohorts.toml", COHORTS)
    for loc, word in GREETING.items():
        write(root, f"src/i18n/{loc}.json",
              json.dumps({"greeting": {"title": word}}, indent=2) + "\n")
        write(root, f"src/pages/{loc}/index.astro", f"<h1>{word}</h1>\n")
    write(root, "lib/tools.py", TOOLS)
    write(root, "app/main.py", CALLER)
    commit_all(root)
    return root


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    """No .conduct/ at all: the gate has to infer the sets from the tree."""
    root = tmp_path / "bare"
    write(root, "README.md", "# A site\n")
    for loc in ("de", "en"):
        write(root, f"src/i18n/{loc}.json", json.dumps({"a": 1}) + "\n")
    write(root, "solo/de.json", json.dumps({"a": 1}) + "\n")
    commit_all(root)
    return root


def touch_locales(root: Path, *locales: str, keys: dict[str, dict] | None = None) -> None:
    """Rewrite the named locale files, optionally with different key sets."""
    for loc in locales:
        payload = (keys or {}).get(loc, {"greeting": {"title": GREETING[loc]}})
        write(root, f"src/i18n/{loc}.json", json.dumps(payload, indent=2) + "\n")


# --- the control -------------------------------------------------------------

def test_clean_repo_passes(repo: Path) -> None:
    """Without this, every test below could pass because the gate always fails."""
    r = run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "moved together" in r.stdout


def test_a_file_in_no_cohort_is_not_the_gate_s_business(repo: Path) -> None:
    write(repo, "README.md", "# A site\n\nA new paragraph.\n")
    r = run(repo)
    assert r.returncode == 0, r.stdout


# --- CHECK 1: cohort drift ---------------------------------------------------

def test_touching_one_locale_names_the_siblings_left_behind(repo: Path) -> None:
    touch_locales(repo, "de", keys={"de": {"greeting": {"title": "Guten Tag"}}})
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "cohort-drift" in r.stdout
    # Naming the missing files is the whole product. "Something is out of sync"
    # sends the reader back to the diff; this sends them to the two files.
    assert "src/i18n/en.json" in r.stdout
    assert "src/i18n/es.json" in r.stdout


def test_touching_all_three_passes(repo: Path) -> None:
    touch_locales(repo, "de", "en", "es", keys={
        loc: {"greeting": {"title": w + "!"}} for loc, w in GREETING.items()})
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_deleting_one_member_of_a_cohort_is_caught(repo: Path) -> None:
    """Removal is desynchronisation too: two locales now answer, one does not."""
    (repo / "src/i18n/de.json").unlink()
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "cohort-drift" in r.stdout


def test_an_untracked_new_file_still_counts_as_touched(repo: Path) -> None:
    """The commonest shape: a new locale file added on disk and never staged."""
    write(repo, "src/pages/de/preise.astro", "<h1>Preise</h1>\n")
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "src/pages/en/preise.astro" in r.stdout
    assert "src/pages/es/preise.astro" in r.stdout


def test_pattern_cohort_pairs_file_by_file(repo: Path) -> None:
    write(repo, "src/pages/de/index.astro", "<h1>Hallo Welt</h1>\n")
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "src/pages/en/index.astro" in r.stdout


def test_pattern_cohort_passes_when_all_three_pages_move(repo: Path) -> None:
    for loc, word in GREETING.items():
        write(repo, f"src/pages/{loc}/index.astro", f"<h1>{word} World</h1>\n")
    r = run(repo)
    assert r.returncode == 0, r.stdout


# --- CHECK 2: key parity -----------------------------------------------------

def test_all_three_touched_but_a_key_only_in_one_is_caught(repo: Path) -> None:
    """Touching the files is not syncing them — the point of this whole check."""
    touch_locales(repo, "de", "en", "es", keys={
        "de": {"greeting": {"title": "Hallo", "subtitle": "Willkommen"}},
        "en": {"greeting": {"title": "Hello"}},
        "es": {"greeting": {"title": "Hola"}},
    })
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "key-parity" in r.stdout
    assert "greeting.subtitle" in r.stdout
    assert "src/i18n/en.json" in r.stdout and "src/i18n/es.json" in r.stdout


def test_the_same_key_added_everywhere_passes(repo: Path) -> None:
    touch_locales(repo, "de", "en", "es", keys={
        loc: {"greeting": {"title": w, "subtitle": w + "!"}}
        for loc, w in GREETING.items()})
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_invalid_json_is_reported_and_the_gate_survives(repo: Path) -> None:
    """A parse error is a finding about that file, never a crash and never a pass."""
    touch_locales(repo, "en", "es", keys={
        "en": {"greeting": {"title": "Hi"}}, "es": {"greeting": {"title": "Buenas"}}})
    write(repo, "src/i18n/de.json", '{"greeting": {"title": "Hallo"\n')
    r = run(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.returncode != 2, "a bad JSON file must not read as a gate failure"
    assert "invalid-json" in r.stdout
    assert "Traceback" not in r.stderr


def test_a_json_file_that_is_not_an_object_is_reported(repo: Path) -> None:
    touch_locales(repo, "en", "es", keys={
        "en": {"greeting": {"title": "Hi"}}, "es": {"greeting": {"title": "Buenas"}}})
    write(repo, "src/i18n/de.json", "[1, 2, 3]\n")
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "invalid-json" in r.stdout


# --- CHECK 3: signature drift ------------------------------------------------

def test_a_breaking_signature_change_with_a_stale_caller_is_caught(repo: Path) -> None:
    write(repo, "lib/tools.py", TOOLS.replace("def render(text, width):",
                                              "def render(text, width, prefix):"))
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "signature-drift" in r.stdout
    assert "app/main.py" in r.stdout


def test_the_same_change_with_the_caller_updated_passes(repo: Path) -> None:
    write(repo, "lib/tools.py", TOOLS.replace("def render(text, width):",
                                              "def render(text, width, prefix):"))
    write(repo, "app/main.py", CALLER.replace('render("hallo", 12)',
                                              'render("hallo", 12, ">")'))
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_appending_an_optional_parameter_is_not_reported(repo: Path) -> None:
    """Deliberately quiet: every existing call still works, so firing is noise.

    A gate with false positives is uninstalled in a week, and deserves to be.
    """
    write(repo, "lib/tools.py", TOOLS.replace('def render(text, width):',
                                              'def render(text, width, prefix=""):'))
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_renaming_a_private_helper_is_not_reported(repo: Path) -> None:
    """A leading underscore is the language saying 'no promises made'."""
    write(repo, "lib/tools.py", TOOLS.replace("def _pad(text, width):",
                                              "def _pad(text, width, fill=' '):"))
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_unparseable_python_is_reported_not_skipped(repo: Path) -> None:
    write(repo, "lib/tools.py", TOOLS + "\ndef broken(:\n")
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "unparseable-python" in r.stdout


# --- autodetection -----------------------------------------------------------

def test_locale_siblings_are_inferred_when_nothing_is_declared(bare: Path) -> None:
    write(bare, "src/i18n/de.json", json.dumps({"a": 2}) + "\n")
    r = run(bare)
    assert r.returncode == 1, r.stdout
    assert "(autodetected)" in r.stdout, "the report must say the set was inferred"
    assert "src/i18n/en.json" in r.stdout


def test_autodetection_never_invents_a_sibling_that_is_not_there(bare: Path) -> None:
    """`solo/de.json` has no siblings on disk. A repo with one locale is not a bug."""
    write(bare, "solo/de.json", json.dumps({"a": 2}) + "\n")
    r = run(bare)
    assert r.returncode == 0, r.stdout
    assert "solo/es.json" not in r.stdout


def test_a_broken_config_does_not_fall_back_to_guessing(repo: Path) -> None:
    """A typo in the declaration must not silently become a different gate."""
    write(repo, ".conduct/cohorts.toml", '[[cohort]]\nname = "locales"\n')
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "cohort-config" in r.stdout
    assert "sets inferred from the tree" not in r.stdout


# --- the allowlist -----------------------------------------------------------

def test_the_allowlist_suppresses_and_says_it_did(repo: Path) -> None:
    write(repo, ".conduct/parity-allow.txt", "src/i18n/*\n")
    touch_locales(repo, "de", keys={"de": {"greeting": {"title": "Guten Tag"}}})
    r = run(repo)
    assert r.returncode == 0, r.stdout
    assert "suppressed" in r.stdout


def test_a_scoped_allowlist_entry_only_suppresses_its_own_check(repo: Path) -> None:
    write(repo, ".conduct/parity-allow.txt", "key-parity:src/i18n/*\n")
    touch_locales(repo, "de", keys={"de": {"greeting": {"title": "Guten Tag"}}})
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "cohort-drift" in r.stdout


# --- the contract: exit 2 is the gate's own failure --------------------------

def test_an_unresolvable_base_is_exit_2_not_a_pass(repo: Path) -> None:
    """When the diff cannot be computed the answer is never 'clean'."""
    r = run(repo, base="no/such/ref")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "gate failure" in r.stderr


def test_outside_a_git_repository_is_exit_2(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    r = run(plain)
    assert r.returncode == 2, r.stdout + r.stderr


# --- SARIF output ------------------------------------------------------------

def test_sarif_is_written_and_well_formed(repo: Path, tmp_path: Path) -> None:
    touch_locales(repo, "de", keys={"de": {"greeting": {"title": "Guten Tag"}}})
    out = tmp_path / "out.sarif"
    r = run(repo, "--sarif", str(out))
    assert r.returncode == 1
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["version"] == "2.1.0"
    assert doc["runs"][0]["results"], "SARIF carries no results for a failing run"
    assert doc["runs"][0]["results"][0]["ruleId"] == "cohort-drift"
