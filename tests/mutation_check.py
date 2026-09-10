#!/usr/bin/env python3
"""Prove the parity gate's tests actually defend it.

    python3 tests/mutation_check.py

For each check in gate/nobody_left.py: delete it, run the suite, and require the
suite to go RED. A test that still passes with the mechanism removed is not
testing the mechanism — it is decoration that reports green forever.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run (the same contract as the gate).

The file is restored from an in-memory copy in a `finally`, never with
`git checkout`: this repo may hold uncommitted work, and a checkout to undo a
mutation would take that work with it. The restore is then verified, because a
mutation runner that leaves the gate mutated has done more harm than the bug it
was hunting.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "gate" / "nobody_left.py"

# (name, the call in main() to neuter, what to leave in its place)
MUTANTS = [
    ("CHECK 1 cohort-drift", "groups = check_cohorts(cohorts, changed, findings)",
     "groups = []"),
    ("CHECK 2 key-parity", "check_key_parity(groups, findings)", ""),
    ("CHECK 3 signature-drift", "check_signatures(base_rev, changed, findings)", ""),
    ("autodetection", "cohorts += autodetect(changed)", ""),
    ("the allowlist", "findings = apply_allowlist(findings, allow, suppressed)", ""),
]


def run_suite() -> bool:
    """True when the suite is green."""
    r = subprocess.run([sys.executable, "-m", "pytest", str(ROOT / "tests"), "-q",
                        "-x", "--no-header"],
                       capture_output=True, text=True, cwd=ROOT, check=False)
    return r.returncode == 0


def main() -> int:
    original = GATE.read_text(encoding="utf-8")

    if not run_suite():
        print("the suite is RED before any mutation — fix that first", file=sys.stderr)
        return 2

    survivors: list[str] = []
    try:
        for name, call, replacement in MUTANTS:
            if call not in original:
                print(f"  ?? {name}: call not found in gate — mutation list is stale")
                survivors.append(f"{name} (stale)")
                continue
            GATE.write_text(original.replace(call, replacement or "pass", 1),
                            encoding="utf-8")
            if run_suite():
                print(f"  SURVIVED  {name} — removed it and the suite stayed green")
                survivors.append(name)
            else:
                print(f"  killed    {name}")
    finally:
        GATE.write_text(original, encoding="utf-8")

    if GATE.read_text(encoding="utf-8") != original:
        print("gate file was NOT restored cleanly", file=sys.stderr)
        return 2

    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        return 1
    print(f"\nall {len(MUTANTS)} mutants killed; gate restored and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
