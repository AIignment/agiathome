#!/usr/bin/env bash
set -euo pipefail

SUBMISSION="/work/submission.dat"
if [ ! -s "$SUBMISSION" ]; then
  echo "Missing or empty $SUBMISSION" >&2
  exit 1
fi

TARGET_IMPORTS=""
export LEAN_PATH="/tmp"

mkdir -p /tmp/sub /tmp/target
cp "$SUBMISSION" /tmp/sub/Submission.lean
cd /tmp

BUILD_LOG=$(mktemp /tmp/lean-build.XXXXXX)
if ! lean -o /tmp/Submission.olean /tmp/sub/Submission.lean >"$BUILD_LOG" 2>&1; then
  cat "$BUILD_LOG" >&2
  exit 1
fi
cat "$BUILD_LOG"

if grep -qE "declaration uses 'sorry'|sorryAx" "$BUILD_LOG"; then
  echo "Rejected: submission uses sorry" >&2
  exit 1
fi

TARGET_THEOREM="${TARGET_THEOREM:-}"
TARGET_SIGNATURE="${TARGET_SIGNATURE:-}"
if [ -z "$TARGET_THEOREM" ] && [ -f /work/target.json ]; then
  TARGET_THEOREM=$(python3 -c 'import json;print(str(json.load(open("/work/target.json")).get("theorem","")).strip())')
  TARGET_SIGNATURE=$(python3 -c 'import json;print(str(json.load(open("/work/target.json")).get("signature","")).strip())')
fi

if [ -n "$TARGET_THEOREM" ]; then
  if [ -z "$TARGET_SIGNATURE" ]; then
    echo "Rejected: target theorem without a signature" >&2
    exit 1
  fi
  if printf '%s' "$TARGET_SIGNATURE" | grep -qE '(\r|\n|#eval|#print|#check|import[[:space:]]|axiom[[:space:]]|unsafe[[:space:]]|initialize)'; then
    echo "Rejected: unsafe target signature" >&2
    exit 1
  fi
  printf '%s\naxiom agihomeTarget %s\n' "$TARGET_IMPORTS" "$TARGET_SIGNATURE" > /tmp/target/AgihomeTarget.lean
  if ! lean -o /tmp/AgihomeTarget.olean /tmp/target/AgihomeTarget.lean >&2; then
    echo "Rejected: target statement failed to elaborate" >&2
    exit 1
  fi
fi

cat > /tmp/target/Audit.lean <<'LEAN'
import Lean
open Lean

def allowedAxioms : List Name := [``propext, ``Classical.choice, ``Quot.sound]

def loadReplayed (m : Name) : IO (Environment × ModuleData) := do
  let (mod, _) ← readModuleData (← findOLean m)
  let (_, s) ← (importModulesCore mod.imports).run {}
  let env ← finalizeImport s mod.imports {} 0 (leakEnv := true)
  let mut consts : Std.HashMap Name ConstantInfo := {}
  for n in mod.constNames, ci in mod.constants do
    consts := consts.insert n ci
  return (← env.replay consts, mod)

def main (args : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  let (env, mod) ← loadReplayed `Submission
  let mut theorems := 0
  for n in mod.constNames do
    match env.find? n with
    | some (.axiomInfo _) =>
      IO.eprintln s!"Rejected: submission declares axiom {n}"
      return 1
    | some (.thmInfo _) => theorems := theorems + 1
    | _ => pure ()
    let (_, st) := ((CollectAxioms.collect n).run env).run {}
    for a in st.axioms do
      unless allowedAxioms.contains a do
        IO.eprintln s!"Rejected: {n} depends on disallowed axiom {a}"
        return 1
  if theorems == 0 then
    IO.eprintln "Rejected: no theorems found"
    return 1
  match args with
  | [thm] =>
    let tenv ← importModules #[{ module := `AgihomeTarget }] {} 0 (leakEnv := true)
    let some tgt := tenv.find? `agihomeTarget | do IO.eprintln "Rejected: target missing"; return 1
    let some sub := env.find? (thm.toName) | do IO.eprintln s!"Rejected: target theorem {thm} not found"; return 1
    unless sub matches .thmInfo _ do
      IO.eprintln s!"Rejected: {thm} is not a theorem"
      return 1
    if sub.levelParams.length != tgt.levelParams.length then
      IO.eprintln "Rejected: universe parameters differ from the target statement"
      return 1
    let subTy := sub.type.instantiateLevelParams sub.levelParams (tgt.levelParams.map Level.param)
    match Kernel.isDefEq env {} tgt.type subTy with
    | .ok true => IO.println s!"Target statement matches {thm}"
    | _ =>
      IO.eprintln s!"Rejected: {thm} does not prove the exact target statement"
      return 1
  | _ => pure ()
  IO.println s!"Kernel replay and axiom audit passed for {mod.constNames.size} constants"
  return 0
LEAN

if [ -n "$TARGET_THEOREM" ]; then
  lean --run /tmp/target/Audit.lean "$TARGET_THEOREM"
elif [ "${ALLOW_UNTARGETED_AUDIT:-0}" = "1" ]; then
  lean --run /tmp/target/Audit.lean
else
  echo "Rejected: missing required target theorem binding" >&2
  exit 1
fi
