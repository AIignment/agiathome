#!/usr/bin/env bash
set -euo pipefail

python3 - <<'PYEOF'
import json, pathlib, re, sys

catalog = json.loads(pathlib.Path("programs/proofmath-500/catalog.json").read_text(encoding="utf-8"))
slugs = [e["slug"] for e in catalog]
refs = [e["parent_ref"] for e in catalog]
step_ids = [s["id"] for e in catalog for s in e["steps"]]
errors = []
if not 0 < len(catalog) <= 500:
    errors.append(f"catalog has {len(catalog)} problems, expected between 1 and 500")
for name, items in (("slug", slugs), ("rank", refs), ("step id", step_ids)):
    dupes = sorted({x for x in items if items.count(x) > 1})
    if dupes:
        errors.append(f"duplicate {name}: {dupes[:5]}")
known = set(step_ids)
for e in catalog:
    for s in e["steps"]:
        for dep in s.get("requires", []):
            if dep not in known:
                errors.append(f"{s['id']} requires unknown step {dep}")

forbidden = re.compile(r"\b(sorry|admit|axiom|native_decide)\b")
root = pathlib.Path("lean/ProofMath/Problems")
imports = pathlib.Path("lean/ProofMath.lean").read_text(encoding="utf-8")
files = sorted(root.rglob("*.lean"))
for p in files:
    m = forbidden.search(p.read_text(encoding="utf-8"))
    if m:
        errors.append(f"{p}: forbidden token '{m.group(1)}'")
    module = "ProofMath." + ".".join(p.relative_to("lean/ProofMath").with_suffix("").parts)
    if f"import {module}\n" not in imports + "\n":
        errors.append(f"{p}: not imported by lean/ProofMath.lean (add 'import {module}')")

if errors:
    print("REJECTED:\n  " + "\n  ".join(errors), file=sys.stderr)
    sys.exit(1)
print(f"ACCEPTED (static check): {len(catalog)} problems, {len(step_ids)} steps, {len(files)} Lean files clean")
PYEOF

if command -v lake >/dev/null 2>&1 && find lean/ProofMath/Problems -name '*.lean' | grep -q .; then
  (cd lean && lake exe cache get && lake build)
fi
