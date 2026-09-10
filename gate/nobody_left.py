#!/usr/bin/env python3
"""The Umuntu Harness gate: nobody gets left behind.

    python3 gate/nobody_left.py                      # diff against origin/main
    python3 gate/nobody_left.py --base HEAD~1
    python3 gate/nobody_left.py --sarif parity.sarif

Exit codes are the contract shared by the conduct-harness family:

    0   no findings
    1   findings — this diff left one of its siblings behind
    2   the gate itself could not run

The third one is not decoration. A checker that returns 1 when it crashed reads
as "I found something"; one that returns 0 reads as "clean" and fails OPEN,
which would let the gate lie on the one day it matters. When this gate cannot
measure — no git, no base ref — it says so and returns 2. It never guesses
"clean" from an absence.

WHAT THIS GATE IS FOR
---------------------
HARAMBEE rule 2, *nothing half-done*: "suite green, every case and every locale
synced, the files left consistent with one another." Its falsifier is written
into CODEX.md: *one locale, case, or file was updated and its siblings left to
drift.* That is a falsifier a machine can check, and until now nothing did.

The failure it catches is the most ordinary one in a trilingual site: an agent
edits `de.json`, ships, and `en.json` and `es.json` quietly fall a key behind.
Nobody notices, because nothing is broken — the English reader just sees a
German string, or a blank.

Three ways a sibling gets left behind, three checks:

  1 cohort-drift   a declared set was touched in part and not in whole
  2 key-parity     the whole set was touched but the KEYS still disagree —
                   touching three files is not the same as syncing them
  3 signature-drift  an exported function changed shape and its callers did not
                   move with it (a heuristic; see check_signatures)

Only what the diff touches is judged. A gate that reported every pre-existing
drift in the tree would be ignored by the second week, and deserve to be.

*Umuntu ngumuntu ngabantu* — a person is a person through other persons. A file
is a file through the files that must agree with it.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The root is overridable so the tests can point the gate at a scratch repo. A
# checker that can only ever run on itself cannot be shown to work: the only way
# to prove a check has teeth is to hand it a repo with the defect planted and
# watch it go red.
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent)

CONDUCT = ROOT / ".conduct"
COHORT_FILES = (CONDUCT / "cohorts.toml", CONDUCT / "cohorts.json")
ALLOW_FILE = CONDUCT / "parity-allow.txt"

# Autodetection only ever recognises these as locale codes. A wider list buys
# nothing and starts matching real directories: `no` (Norwegian) would claim
# every `src/no/` and `is` (Icelandic) every `lib/is/`.
LOCALES = ("de", "en", "es", "fr", "it", "pt")

# How many missing keys to print before summarising. A wall of 400 keys is not a
# report, it is a reason to stop reading reports.
MAX_LISTED = 8


class GateFailure(RuntimeError):
    """The gate could not measure. Exit 2 — never 1, never 0."""


@dataclass
class Finding:
    check: str
    message: str
    path: str = ""
    line: int = 0


@dataclass
class Cohort:
    """A set of files that travel together."""

    name: str
    kind: str                                   # "members" | "pattern"
    members: tuple[str, ...] = ()
    pattern: str = ""
    values: tuple[str, ...] = ()
    origin: str = "declared"                    # "declared" | "autodetected"
    rx: Any = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.kind == "pattern" and self.rx is None:
            m = re.search(r"\{(\w+)\}", self.pattern)
            if m is None:                       # validated in load_cohorts
                raise GateFailure(f"cohort {self.name!r}: pattern has no placeholder")
            alt = "|".join(re.escape(v) for v in self.values)
            self.rx = re.compile(
                glob_re(self.pattern[:m.start()]) + "(" + alt + ")"
                + glob_re(self.pattern[m.end():]))

    def siblings(self, path: str) -> tuple[str, ...] | None:
        """The whole set this path belongs to, or None if it does not belong."""
        if self.kind == "members":
            return self.members if path in self.members else None
        m = self.rx.fullmatch(path)
        if m is None:
            return None
        # The capture group tells us exactly where the locale sits in the path,
        # so the siblings are the same path with the other values spliced in.
        # Deriving them by string search would rewrite `de` inside `moderne/`.
        return tuple(path[:m.start(1)] + v + path[m.end(1):] for v in self.values)


def glob_re(pat: str) -> str:
    """Translate a glob to a regex where `**` crosses `/` and `*` does not.

    `fnmatch` maps `*` to `.*`, which crosses directory boundaries — so
    `src/*.json` would match `src/a/b/c.json` and a cohort would silently
    swallow half the tree. The distinction is the whole reason for hand-rolling
    twenty lines instead of importing one.
    """
    out: list[str] = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                if pat[i + 2:i + 3] == "/":     # `**/` may match nothing at all
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


# --- git ---------------------------------------------------------------------

def git(*args: str, check: bool = True) -> str:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args],
                           capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:            # git itself is missing
        raise GateFailure(f"git is not available: {exc}") from exc
    if r.returncode != 0:
        if check:
            raise GateFailure(f"git {' '.join(args)} failed: {r.stderr.strip()[:200]}")
        return ""
    return r.stdout


def repo_prefix() -> str:
    """'' when ROOT is the repo top level, else ROOT's path inside the repo."""
    top = git("rev-parse", "--show-toplevel").strip()
    if not top:
        raise GateFailure(f"{ROOT} is not inside a git repository")
    try:
        rel = Path(ROOT).resolve().relative_to(Path(top).resolve())
    except ValueError as exc:
        raise GateFailure(f"{ROOT} is not inside {top}") from exc
    return "" if str(rel) == "." else str(rel) + "/"


def resolve_base(base: str) -> str:
    """The revision to diff from: the merge base with `base` when there is one.

    Diffing straight against a branch tip reports every commit that landed on
    main since this branch forked as if this branch had touched it. The merge
    base is what a reviewer actually sees.
    """
    sha = git("rev-parse", "--verify", "--quiet", base + "^{commit}", check=False).strip()
    if not sha:
        raise GateFailure(
            f"base revision {base!r} does not resolve — pass --base <ref> "
            f"(e.g. --base HEAD~1), or fetch it first. The gate will not "
            f"report 'clean' for a diff it could not compute.")
    merge_base = git("merge-base", sha, "HEAD", check=False).strip()
    return merge_base or sha


def changed_files(base_rev: str, prefix: str) -> set[str]:
    """Every path the diff touches, plus untracked files, repo-relative.

    Untracked files count. The commonest shape of this failure is a NEW locale
    file that was never added: it is present on disk, absent from the index, and
    invisible to `git diff`. Skipping it would let the gate pass a diff that
    adds `de.json` alone.
    """
    out: set[str] = set()
    tokens = [t for t in git("diff", "--name-status", "-M", "-z", base_rev, "--").split("\0") if t]
    i = 0
    while i < len(tokens):
        status = tokens[i]
        n = 2 if status[:1] in ("R", "C") else 1     # rename/copy carry old AND new
        for p in tokens[i + 1:i + 1 + n]:
            out.add(p)
        i += 1 + n
    # --full-name: without it `ls-files` prints paths relative to the working
    # directory while `diff` prints them relative to the repo root, and the two
    # halves of this set stop lining up the moment ROOT is a subdirectory.
    out.update(t for t in git("ls-files", "--others", "--exclude-standard",
                              "--full-name", "-z").split("\0") if t)

    if not prefix:
        return out
    return {p[len(prefix):] for p in out if p.startswith(prefix)}


# --- cohorts -----------------------------------------------------------------

def load_cohorts(findings: list[Finding]) -> tuple[list[Cohort], bool]:
    """Read .conduct/cohorts.{toml,json}. Returns (cohorts, a file was present).

    TOML is the documented form and needs no dependency: `tomllib` has been in
    the standard library since 3.11, which is this gate's floor. JSON is
    accepted for hosts that would rather not add a second config language.

    A config file that exists but is broken returns `True` for "present": a
    malformed declaration must NOT silently fall back to autodetection, or the
    author's typo turns into a gate that quietly checks something else.
    """
    path = next((p for p in COHORT_FILES if p.is_file()), None)
    if path is None:
        return [], False
    where = str(path.relative_to(ROOT))
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".toml":
            import tomllib
            raw: Any = tomllib.loads(text)
        else:
            raw = json.loads(text)
    except Exception as exc:                    # noqa: BLE001 - reported, not swallowed
        findings.append(Finding("cohort-config", f"{where}: unreadable ({exc})", where))
        return [], True

    if isinstance(raw, list):                   # a bare JSON list of cohorts
        entries: Any = raw
    elif isinstance(raw, dict):
        entries = raw.get("cohort") or raw.get("cohorts") or []
    else:
        entries = None
    if not isinstance(entries, list):
        findings.append(Finding(
            "cohort-config", f"{where}: expected a list of [[cohort]] tables", where))
        return [], True

    out: list[Cohort] = []
    for i, e in enumerate(entries, 1):
        tag = f"{where}: cohort #{i}"
        if not isinstance(e, dict):
            findings.append(Finding("cohort-config", f"{tag} is not a table", where))
            continue
        name = str(e.get("name") or f"#{i}")
        members, pattern = e.get("members"), e.get("pattern")
        if members is not None and pattern is not None:
            findings.append(Finding(
                "cohort-config",
                f'{tag} ("{name}"): declares both `members` and `pattern` — pick one',
                where))
            continue
        if members is not None:
            if (not isinstance(members, list) or len(members) < 2
                    or not all(isinstance(m, str) and m for m in members)):
                findings.append(Finding(
                    "cohort-config",
                    f'{tag} ("{name}"): `members` must list at least two paths', where))
                continue
            out.append(Cohort(name, "members", members=tuple(members)))
        elif pattern is not None:
            values = e.get("values")
            if (not isinstance(pattern, str) or not isinstance(values, list)
                    or len(values) < 2 or not all(isinstance(v, str) and v for v in values)):
                findings.append(Finding(
                    "cohort-config",
                    f'{tag} ("{name}"): `pattern` needs `values` with at least two entries',
                    where))
                continue
            holes = re.findall(r"\{(\w+)\}", pattern)
            if len(holes) != 1:
                findings.append(Finding(
                    "cohort-config",
                    f'{tag} ("{name}"): `pattern` needs exactly one placeholder, '
                    f"found {len(holes)}", where))
                continue
            out.append(Cohort(name, "pattern", pattern=pattern, values=tuple(values)))
        else:
            findings.append(Finding(
                "cohort-config",
                f'{tag} ("{name}"): needs `members`, or `pattern` + `values`', where))
    return out, True


def autodetect(changed: set[str]) -> list[Cohort]:
    """Infer locale cohorts from the tree when nothing is declared.

    THE RULE THAT KEEPS THIS HONEST: a sibling is only a cohort member if it can
    be SEEN — it exists on disk or the diff touches it. Never invent the set.
    Touching `de.json` in a repo that has no `es.json` is not a finding; it is a
    repo with one locale, and a gate that demanded a Spanish file nobody ever
    wrote would be deleted the same afternoon.
    """
    found: dict[frozenset[str], Cohort] = {}
    for path in sorted(changed):
        for members in _locale_variants(path):
            real = tuple(m for m in members if m in changed or (ROOT / m).is_file())
            if len(real) < 2:
                continue
            key = frozenset(real)
            if key in found:
                continue
            found[key] = Cohort(
                _shorthand(real), "members", members=real, origin="autodetected")
    return list(found.values())


def _locale_variants(path: str) -> list[tuple[str, ...]]:
    """Candidate sibling sets for a path, from its locale stem or directory."""
    out: list[tuple[str, ...]] = []
    parts = path.split("/")
    stem, dot, ext = parts[-1].partition(".")
    if dot and stem in LOCALES:                        # src/i18n/de.json
        head = "/".join(parts[:-1])
        head = head + "/" if head else ""
        out.append(tuple(f"{head}{loc}.{ext}" for loc in LOCALES))
    for i, seg in enumerate(parts[:-1]):               # src/pages/de/index.astro
        if seg in LOCALES:
            out.append(tuple("/".join(parts[:i] + [loc] + parts[i + 1:]) for loc in LOCALES))
    return out


def _shorthand(members: tuple[str, ...]) -> str:
    """A readable name for an inferred set: src/i18n/{de,en}.json."""
    first = members[0].split("/")
    for i in range(len(first)):
        variants = {m.split("/")[i] for m in members if len(m.split("/")) == len(first)}
        if len(variants) == len(members) > 1:
            return "/".join(first[:i] + ["{" + ",".join(sorted(variants)) + "}"] + first[i + 1:])
    return members[0]


# --- CHECK 1 -----------------------------------------------------------------

def check_cohorts(cohorts: list[Cohort], changed: set[str],
                  findings: list[Finding]) -> list[tuple[Cohort, tuple[str, ...]]]:
    """CHECK 1 — a set touched in part is a set left behind.

    Returns every touched set, so CHECK 2 can go on to ask the harder question:
    the files all moved, but did they move to the same place?
    """
    groups: list[tuple[Cohort, tuple[str, ...]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for cohort in cohorts:
        for path in sorted(changed):
            members = cohort.siblings(path)
            if members is None:
                continue
            key = (cohort.name, members)
            if key in seen:
                continue
            seen.add(key)
            groups.append((cohort, members))
            missing = [m for m in members if m not in changed]
            if not missing:
                continue
            touched = [m for m in members if m in changed]
            findings.append(Finding(
                "cohort-drift",
                f'cohort "{cohort.name}" ({cohort.origin}): '
                f"{', '.join(touched)} changed, but {', '.join(missing)} did not — "
                f"nobody gets left behind",
                touched[0]))
    return groups


# --- CHECK 2 -----------------------------------------------------------------

def check_key_parity(groups: list[tuple[Cohort, tuple[str, ...]]],
                     findings: list[Finding]) -> None:
    """CHECK 2 — touching the three files is not the same as syncing them.

    CHECK 1 is satisfied by a commit that opens all three locale files and edits
    one. This one reads them: every key present in any member of the set must be
    present in all of them.

    Invalid JSON is REPORTED and the gate carries on. A parse error is a finding
    about that file, not a reason to abandon the other checks — the diff that
    broke `de.json` is exactly the diff whose `en.json` also needs looking at.
    """
    for cohort, members in groups:
        files = [m for m in members if m.lower().endswith(".json")]
        if len(files) < 2:
            continue
        keys: dict[str, set[tuple[str, ...]]] = {}
        broken = False
        for name in files:
            path = ROOT / name
            if not path.is_file():
                continue                        # absent: CHECK 1's business, not ours
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                findings.append(Finding(
                    "invalid-json", f"{name}: cannot be read as JSON ({exc}) — "
                    f"key parity for this set could not be checked", name))
                broken = True
                continue
            if not isinstance(data, dict):
                findings.append(Finding(
                    "invalid-json", f"{name}: top level is not a JSON object — "
                    f"key parity for this set could not be checked", name))
                broken = True
                continue
            keys[name] = flatten(data)
        if broken or len(keys) < 2:
            continue
        union: set[tuple[str, ...]] = set().union(*keys.values())
        for name, own in keys.items():
            missing = sorted(".".join(k) for k in union - own)
            if not missing:
                continue
            shown = ", ".join(missing[:MAX_LISTED])
            if len(missing) > MAX_LISTED:
                shown += f", +{len(missing) - MAX_LISTED} more"
            findings.append(Finding(
                "key-parity",
                f'cohort "{cohort.name}": {name} is missing {len(missing)} key(s) '
                f"its siblings carry: {shown}", name))


def flatten(obj: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Every leaf path in a nested object. A list is a leaf, an empty dict too."""
    out: set[tuple[str, ...]] = set()
    for k, v in obj.items():
        key = prefix + (str(k),)
        if isinstance(v, dict) and v:
            out |= flatten(v, key)
        else:
            out.add(key)
    return out


# --- CHECK 3 -----------------------------------------------------------------

@dataclass(frozen=True)
class Param:
    kind: str                                   # positional | kwonly | vararg | kwarg
    name: str
    optional: bool


def check_signatures(base_rev: str, changed: set[str], findings: list[Finding]) -> None:
    """CHECK 3 — an exported function changed shape; did its callers move too?

    HEURISTIC, and deliberately a quiet one. It reports only a change that could
    break an existing call:

      * a positional parameter removed, renamed, or reordered
      * a new parameter with no default
      * a keyword-only parameter removed
      * `*args` or `**kwargs` taken away

    Appending an optional parameter is NOT reported: every existing call site
    still works, and firing on it would make the check noise. Callers are found
    by scanning `.py` files for `name(` — a grep, with a grep's blind spots:
    getattr dispatch, re-exports and dynamic calls are invisible to it, and a
    same-named method on an unrelated class is a false positive. Hence the
    "(heuristic)" in the message: this one asks you to look, it does not claim
    to know.
    """
    for path in sorted(p for p in changed if p.endswith(".py")):
        before = git("show", f"{base_rev}:{path}", check=False)
        if not before:
            continue                            # added file: nothing to drift from
        current = ROOT / path
        if not current.is_file():
            continue                            # deleted: not this check's business
        try:
            old = signatures(before)
            new = signatures(current.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            findings.append(Finding(
                "unparseable-python", f"{path}: cannot be parsed ({exc}) — "
                f"signature drift for this file could not be checked", path))
            continue
        for qualname, params in new.items():
            was = old.get(qualname)
            if was is None or was == params or not breaking(was, params):
                continue
            simple = qualname.rsplit(".", 1)[-1]
            stale = sorted(callers(simple, exclude=path) - changed)
            if not stale:
                continue
            findings.append(Finding(
                "signature-drift",
                f"{qualname}() changed shape ({render(was)} -> {render(params)}) "
                f"but {', '.join(stale)} still call(s) it and did not change "
                f"(heuristic)", path))


def signatures(src: str) -> dict[str, tuple[Param, ...]]:
    """Public functions and methods, by qualified name.

    Public means "does not start with an underscore". A leading underscore is
    the language's own way of saying "no promises made", and a name nobody was
    promised cannot leave anybody behind.
    """
    tree = ast.parse(src)
    out: dict[str, tuple[Param, ...]] = {}

    def fn(node: ast.AST, prefix: str = "") -> None:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        if node.name.startswith("_"):
            return
        out[prefix + node.name] = params_of(node.args)

    for node in tree.body:
        fn(node)
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            for child in node.body:
                fn(child, node.name + ".")
    return out


def params_of(a: ast.arguments) -> tuple[Param, ...]:
    positional = list(a.posonlyargs) + list(a.args)
    # Defaults bind to the RIGHTMOST positional parameters, so the first
    # `len(positional) - len(defaults)` are the required ones.
    required = len(positional) - len(a.defaults)
    out = [Param("positional", p.arg, i >= required) for i, p in enumerate(positional)]
    if a.vararg:
        out.append(Param("vararg", a.vararg.arg, True))
    out += [Param("kwonly", p.arg, d is not None)
            for p, d in zip(a.kwonlyargs, a.kw_defaults)]
    if a.kwarg:
        out.append(Param("kwarg", a.kwarg.arg, True))
    return tuple(out)


def breaking(old: tuple[Param, ...], new: tuple[Param, ...]) -> bool:
    """True when an existing call site could stop working."""
    o_pos = [p for p in old if p.kind == "positional"]
    n_pos = [p for p in new if p.kind == "positional"]
    if [p.name for p in n_pos][:len(o_pos)] != [p.name for p in o_pos]:
        return True                                     # removed, renamed or reordered
    if any(not p.optional for p in n_pos[len(o_pos):]):
        return True                                     # new required parameter
    o_kw = {p.name for p in old if p.kind == "kwonly"}
    n_kw = {p.name for p in new if p.kind == "kwonly"}
    if o_kw - n_kw:
        return True
    if any(p.kind == "kwonly" and p.name not in o_kw and not p.optional for p in new):
        return True
    for kind in ("vararg", "kwarg"):
        if any(p.kind == kind for p in old) and not any(p.kind == kind for p in new):
            return True
    return False


def render(params: tuple[Param, ...]) -> str:
    bits = []
    for p in params:
        star = {"vararg": "*", "kwarg": "**"}.get(p.kind, "")
        bits.append(f"{star}{p.name}" + ("=..." if p.optional and not star else ""))
    return "(" + ", ".join(bits) + ")"


CALL_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache"}


def callers(name: str, exclude: str) -> set[str]:
    """Repo-relative `.py` files that appear to call `name`."""
    rx = re.compile(rf"\b{re.escape(name)}\s*\(")
    out: set[str] = set()
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in CALL_SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            rel = str(Path(base, fname).relative_to(ROOT))
            if rel == exclude:
                continue
            try:
                text = Path(base, fname).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line in text.splitlines():
                stripped = line.lstrip()
                if stripped.startswith(("def ", "async def ", "#")):
                    continue                            # a definition is not a call
                if rx.search(line):
                    out.add(rel)
                    break
    return out


# --- allowlist ---------------------------------------------------------------

def load_allow() -> list[tuple[str, str]]:
    """(check-id or '*', glob) pairs from .conduct/parity-allow.txt."""
    if not ALLOW_FILE.is_file():
        return []
    out: list[tuple[str, str]] = []
    for raw in ALLOW_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        check, sep, glob = line.partition(":")
        out.append((check.strip(), glob.strip()) if sep and glob.strip() else ("*", line))
    return out


def apply_allowlist(findings: list[Finding], allow: list[tuple[str, str]],
                    suppressed: list[Finding]) -> list[Finding]:
    """Drop allowlisted findings — and hand back what was dropped.

    The suppressed count is printed. An allowlist that hides its own size is how
    a gate ends up green with forty exemptions nobody remembers granting.
    """
    kept: list[Finding] = []
    for f in findings:
        for check, glob in allow:
            if check not in ("*", f.check):
                continue
            if re.fullmatch(glob_re(glob), f.path or ""):
                suppressed.append(f)
                break
        else:
            kept.append(f)
    return kept


# --- output ------------------------------------------------------------------

def to_sarif(findings: list[Finding]) -> dict[str, Any]:
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "umuntu-harness-nobody-left",
                "informationUri": "https://github.com/arnoldwender/umuntu-harness",
                "rules": [{"id": r} for r in sorted({f.check for f in findings})],
            }},
            "results": [{
                "ruleId": f.check,
                "level": "error",
                "message": {"text": f.message},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": f.path or ".conduct/"},
                    "region": {"startLine": max(f.line, 1)},
                }}],
            } for f in findings],
        }],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="origin/main",
                    help="revision to diff against (default: origin/main)")
    ap.add_argument("--sarif", metavar="PATH", help="write SARIF 2.1.0 to PATH")
    args = ap.parse_args(argv)

    findings: list[Finding] = []
    suppressed: list[Finding] = []
    try:
        prefix = repo_prefix()
        base_rev = resolve_base(args.base)
        changed = changed_files(base_rev, prefix)
        allow = load_allow()
        cohorts, declared = load_cohorts(findings)
        if not declared:
            cohorts += autodetect(changed)
        groups = check_cohorts(cohorts, changed, findings)
        check_key_parity(groups, findings)
        check_signatures(base_rev, changed, findings)
        findings = apply_allowlist(findings, allow, suppressed)
    except GateFailure as exc:
        print(f"gate failure: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:                    # noqa: BLE001
        # Exit 2, never 1 and never 0: the gate broke, it did not judge.
        print(f"gate failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.sarif:
        Path(args.sarif).write_text(json.dumps(to_sarif(findings), indent=2), encoding="utf-8")

    inferred = [c for c in cohorts if c.origin == "autodetected"]
    print(f"nobody-left: {len(changed)} changed file(s) vs {args.base} "
          f"({base_rev[:9]}), {len(cohorts)} cohort(s) "
          f"[{len(cohorts) - len(inferred)} declared, {len(inferred)} autodetected]")
    if not declared:
        print(f"  no {CONDUCT.name}/cohorts.toml — sets inferred from the tree, "
              f"never invented:")
        for c in inferred:
            print(f"    autodetected {c.name}: {', '.join(c.members)}")
        if not inferred:
            print("    none found among the changed files")
    for f in findings:
        where = f"{f.path}:{f.line}" if f.line else (f.path or ".conduct/")
        print(f"  FAIL [{f.check}] {where}: {f.message}")
    if suppressed:
        print(f"  {len(suppressed)} finding(s) suppressed by "
              f"{ALLOW_FILE.relative_to(ROOT)}: "
              f"{', '.join(sorted({f.check for f in suppressed}))}")
    if findings:
        print(f"\n{len(findings)} finding(s)")
        return 1
    print("  every set this diff touched moved together")
    return 0


if __name__ == "__main__":
    sys.exit(main())
