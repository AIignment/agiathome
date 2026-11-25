#!/usr/bin/env bash
set -euo pipefail

SUBMISSION="/work/submission.dat"
if [ ! -s "$SUBMISSION" ]; then
  echo "Missing or empty $SUBMISSION" >&2
  exit 1
fi

cp "$SUBMISSION" /tmp/Submission.lean
export LEAN_PATH="/tmp"

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

CHECK_FILE="/tmp/Check.lean"
printf "import Submission\n" > "$CHECK_FILE"

if [ -n "${TARGET_THEOREM:-}" ]; then
  printf "#print axioms %s\n" "$TARGET_THEOREM" >> "$CHECK_FILE"
  DECL_COUNT=1
else
  DECLS=$(awk '
    BEGIN { depth = 0 }
    /^[[:space:]]*namespace[[:space:]]+[A-Za-z_][A-Za-z0-9_.]*/ {
      line = $0
      sub(/^[[:space:]]*namespace[[:space:]]+/, "", line)
      sub(/[[:space:]].*$/, "", line)
      depth++
      ns[depth] = line
      next
    }
    /^[[:space:]]*end[[:space:]]+[A-Za-z_][A-Za-z0-9_.]*/ {
      if (depth > 0) depth--
      next
    }
    /^[[:space:]]*(private[[:space:]]+|protected[[:space:]]+)?(theorem|lemma|def)[[:space:]]+[A-Za-z_][A-Za-z0-9_.\x27]*/ {
      line = $0
      sub(/^[[:space:]]*(private[[:space:]]+|protected[[:space:]]+)?(theorem|lemma|def)[[:space:]]+/, "", line)
      sub(/[[:space:]:({\[].*$/, "", line)
      prefix = ""
      for (i = 1; i <= depth; i++) {
        prefix = (prefix == "" ? ns[i] : prefix "." ns[i])
      }
      if (prefix != "") print prefix "." line
      else print line
    }
  ' /tmp/Submission.lean)

  DECL_COUNT=0
  for decl in $DECLS; do
    printf "#print axioms %s\n" "$decl" >> "$CHECK_FILE"
    DECL_COUNT=$((DECL_COUNT + 1))
  done
fi

if [ "$DECL_COUNT" -eq 0 ]; then
  echo "Rejected: no verifiable theorem or definition found" >&2
  exit 1
fi

AXIOM_LOG=$(mktemp /tmp/lean-axioms.XXXXXX)
if ! lean "$CHECK_FILE" >"$AXIOM_LOG" 2>&1; then
  cat "$AXIOM_LOG" >&2
  exit 1
fi
cat "$AXIOM_LOG"

if grep -qE "sorryAx|Lean\.ofReduceBool|Lean\.trustCompiler" "$AXIOM_LOG"; then
  echo "Rejected: forbidden axiom detected" >&2
  exit 1
fi

BAD_AXIOMS=$(awk '
  /depends on axioms:/ {
    line = $0
    sub(/^.*depends on axioms: \[/, "", line)
    sub(/\].*$/, "", line)
    n = split(line, arr, /,[[:space:]]*/)
    for (i = 1; i <= n; i++) {
      ax = arr[i]
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", ax)
      if (ax != "" && ax != "propext" && ax != "Classical.choice" && ax != "Quot.sound") {
        print ax
      }
    }
  }
' "$AXIOM_LOG")

if [ -n "$BAD_AXIOMS" ]; then
  echo "Rejected: non-standard axioms detected: $BAD_AXIOMS" >&2
  exit 1
fi
