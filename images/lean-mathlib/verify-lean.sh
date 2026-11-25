#!/usr/bin/env bash
set -euo pipefail

SUBMISSION="/work/submission.dat"
if [ ! -s "$SUBMISSION" ]; then
  echo "Missing or empty $SUBMISSION" >&2
  exit 1
fi

cp "$SUBMISSION" /tmp/Submission.lean

MATHLIB_PATHS=$(find /opt/mathlib-workspace/.lake -type d -name lean -path '*/build/lib/lean' | tr '\n' ':')
export LEAN_PATH="/tmp:${MATHLIB_PATHS}"

BUILD_LOG=$(mktemp /tmp/lean-build.XXXXXX)
if ! lean -o /tmp/Submission.olean /tmp/Submission.lean >"$BUILD_LOG" 2>&1; then
  cat "$BUILD_LOG" >&2
  exit 1
fi
cat "$BUILD_LOG"

if grep -qE "declaration uses 'sorry'|sorryAx" "$BUILD_LOG"; then
  echo "Rejected: submission uses sorry" >&2
  exit 1
fi

python3 - <<'PY'
import os
import re
import subprocess
import sys

with open("/tmp/Submission.lean", "r", encoding="utf-8") as f:
    src = f.read()

target_theorem = os.environ.get("TARGET_THEOREM", "").strip()
target_signature = os.environ.get("TARGET_SIGNATURE", "").strip()

if not target_theorem and os.path.exists("/work/target.json"):
    import json
    with open("/work/target.json", "r", encoding="utf-8") as tf:
        tdata = json.load(tf)
        target_theorem = str(tdata.get("theorem", "")).strip()
        target_signature = str(tdata.get("signature", "")).strip()

decls = []
if target_theorem:
    decls = [target_theorem]
else:
    ns_stack = []
    for raw_line in src.splitlines():
        m_ns = re.match(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_.]*)", raw_line)
        if m_ns:
            ns_stack.append(m_ns.group(1))
            continue
        if re.match(r"^\s*end\s+[A-Za-z_][A-Za-z0-9_.]*", raw_line):
            if ns_stack:
                ns_stack.pop()
            continue
        m_decl = re.match(
            r"^\s*(?:private\s+|protected\s+)?(?:theorem|lemma|def)\s+([A-Za-z_][A-Za-z0-9_.']*)",
            raw_line,
        )
        if m_decl:
            name = m_decl.group(1)
            full = ".".join(ns_stack + [name]) if ns_stack else name
            decls.append(full)

if not decls:
    print("Rejected: no verifiable declarations found", file=sys.stderr)
    sys.exit(1)

lines = ["import Mathlib", "import Submission"]
if target_theorem and target_signature:
    lines.append(f"example : {target_signature} := {target_theorem}")
for d in decls:
    lines.append(f"#print axioms {d}")

check_path = "/tmp/Check.lean"
with open(check_path, "w", encoding="utf-8") as cf:
    cf.write("\n".join(lines) + "\n")

proc = subprocess.run(["lean", check_path], capture_output=True, text=True)
combined = (proc.stdout + "\n" + proc.stderr).strip()
if combined:
    print(combined)
if proc.returncode != 0:
    sys.exit(proc.returncode)

allowed = {"propext", "Classical.choice", "Quot.sound"}
checked = 0
for line in combined.splitlines():
    if "does not depend on any axioms" in line:
        checked += 1
        continue
    m_ax = re.search(r"depends on axioms:\s*\[([^\]]*)\]", line)
    if m_ax:
        checked += 1
        axioms = [a.strip() for a in m_ax.group(1).split(",") if a.strip()]
        bad = [a for a in axioms if a not in allowed]
        if bad:
            print(f"Rejected: disallowed axioms {bad}", file=sys.stderr)
            sys.exit(1)

if checked < len(decls):
    print("Rejected: incomplete axiom audit", file=sys.stderr)
    sys.exit(1)
PY
