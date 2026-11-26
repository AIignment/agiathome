# Contributing to ProofMath 500

1. Claim an open step through the AGI@home Desk first (`tools/desk.sh next <problem_slug>`), so that volunteers do not duplicate each other's work.
2. Keep each pull request to a single step, and put the step ID (for example `pm-284-formalize`) in the title.
3. Put the proof in `lean/ProofMath/Problems/PM<rank><Name>.lean` inside its own namespace, add an `import` line for it to `lean/ProofMath.lean`, and run `bash tools/check.sh` before you open the pull request or submit to the Desk.
4. Lean files must compile with the toolchain pinned in `lean/lean-toolchain` against the pinned Mathlib. They must contain no `sorry`, `admit`, `axiom` or `native_decide`, and they may rely only on the standard axioms `propext`, `Classical.choice` and `Quot.sound`.
5. A formalization must state the exact conjecture of its step. Weakened statements, `True` placeholders and statements that are trivially satisfiable are rejected, and partial results must say plainly what they do and do not prove.
6. All contributions are licensed under the MIT License.
