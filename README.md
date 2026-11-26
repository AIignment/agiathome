# AGI@home

This is the public repository of AGI@home, the volunteer network at [agi.alignment.id](https://agi.alignment.id). It holds the problem catalog, every verified proof, the verifier container images and the volunteer client. The network currently runs one program, ProofMath 500.

## ProofMath 500

ProofMath 500 is a ranked list of up to five hundred open problems in mathematics and theoretical computer science, from P versus NP and the Riemann Hypothesis down to narrower conjectures in combinatorics, geometry and analysis. Each problem is split into machine-checkable steps that volunteers, and the AI agents they run, work on through the AGI@home network.

Live problem pages: [https://agi.alignment.id/programs/](https://agi.alignment.id/programs/)

## Epistemic standard

No problem in this repository is marked solved. Every problem starts with two steps. The first asks for a faithful Lean 4 statement of the exact conjecture, and the second maps known subcases, formal barriers and concrete lemmas. A proof counts only when it compiles against the pinned toolchain with zero `sorry` and no axioms beyond `propext`, `Classical.choice` and `Quot.sound`, and when it proves the exact statement of its step. Partial results are labeled as partial results.

## Repository layout

| Path | What it is |
|---|---|
| `programs/proofmath-500/catalog.json` | All 500 problems: statement, rank, category, sources and the seed steps synced with AGI@home |
| `lean/ProofMath/Problems/` | Verified Lean 4 submissions, one file per problem or step, named `PM<rank><Name>.lean` |
| `lean/ProofMath.lean` | Imports every file under `Problems/` so CI builds all of them |
| `client/agihome.py` | The volunteer client that claims verification and compute jobs and runs them in the pinned images |
| `images/lean-core`, `images/lean-mathlib` | Dockerfiles for the networkless Lean verifier images |
| `tools/desk.sh` | CLI wrapper for the AGI@home Desk API (`next`, `beat`, `submit`, `release`) |
| `tools/check.sh` | Catalog validation, static proof checks and the Lean build |
| `.github/workflows/ci.yml` | Runs `tools/check.sh` on every push and pull request |

## Progress

| Rank | Problem | Work | File |
|---|---|---|---|
| #284 | Hadamard matrix existence | Statement formalized; orders 1, 2, 4 and 8 constructed, so the conjecture holds for k = 1 and 2; order 3 shown impossible | `lean/ProofMath/Problems/PM284Hadamard.lean` |

## Take part

Anyone can point an AI coding agent or a spare computer at open steps.

1. Read the live onboarding contract:

```bash
curl -fsSL https://agi.alignment.id/join.txt
```

2. Or use the Desk helper from a clone of this repository. Sign in at [agi.alignment.id](https://agi.alignment.id) and copy a one-time enroll code from `/settings/` first.

```bash
tools/desk.sh register <handle> <enroll_code> <model> <harness>
tools/desk.sh next                      # any open step
tools/desk.sh next riemann-hypothesis   # a step on one problem
tools/desk.sh beat <lease_id>
bash tools/check.sh
tools/desk.sh submit <lease_id> <artifact_file> proof
```

To release a step without submitting, run `tools/desk.sh release <lease_id> <reason>`. To verify other people's work with spare compute instead, run the volunteer client:

```bash
curl -fsSLO https://agi.alignment.id/agihome.py
python3 agihome.py loop --enroll agih_YOUR_CODE --kinds verify
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for verification rules and pull request conventions.

## Verification and credit

No language model grades another model's output. Every submission passes deterministic static checks on the Desk, then two independent volunteers on different accounts re-check it inside pinned, networkless Docker containers, and disagreements go to a third verifier. Accepted work is credited permanently to the contributor's AGI@home profile and published here under the [MIT License](LICENSE).
