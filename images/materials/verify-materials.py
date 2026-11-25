#!/usr/bin/env python3
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys

import ase.data
import ase.io
import numpy as np
from pymatgen.io.cif import CifParser
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer


def fail(msg):
    print(f"Rejected: {msg}", file=sys.stderr)
    sys.exit(1)


def load_submission():
    sub_path = "/work/submission.dat"
    if not os.path.isfile(sub_path) or os.path.getsize(sub_path) == 0:
        fail("Missing or empty /work/submission.dat")
    with open(sub_path, "r", encoding="utf-8") as f:
        raw = f.read()
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(raw)
        except Exception as exc:
            fail(f"Invalid JSON submission: {exc}")
        if not isinstance(payload, dict) or not isinstance(payload.get("cif"), str) or not payload["cif"].strip():
            fail("JSON submission must include non-empty 'cif' string")
        return payload["cif"], payload
    return raw, {}


def validate_cif_and_bonds(cif_text):
    try:
        parser = CifParser.from_str(cif_text)
        structures = parser.parse_structures(primitive=False)
    except Exception as exc:
        fail(f"pymatgen CIF parse error: {exc}")

    if not structures:
        fail("No crystal structures found in CIF")

    structure = structures[0]
    if len(structure) == 0:
        fail("Structure has zero atomic sites")

    a, b, c = structure.lattice.abc
    alpha, beta, gamma = structure.lattice.angles
    vol = structure.lattice.volume

    for dim in (a, b, c):
        if not math.isfinite(dim) or dim < 1.5 or dim > 80.0:
            fail(f"Unit cell length {dim:.4f} A outside physical range [1.5, 80.0]")
    for ang in (alpha, beta, gamma):
        if not math.isfinite(ang) or ang < 20.0 or ang > 160.0:
            fail(f"Unit cell angle {ang:.4f} deg outside physical range [20.0, 160.0]")
    if not math.isfinite(vol) or vol <= 1.0:
        fail(f"Invalid cell volume: {vol}")

    try:
        atoms = ase.io.read(io.StringIO(cif_text), format="cif")
    except Exception as exc:
        fail(f"ASE CIF parse error: {exc}")

    if len(atoms) != len(structure):
        fail("Atom count mismatch between pymatgen and ASE parsers")

    positions = atoms.get_scaled_positions(wrap=False)
    if not np.all(np.isfinite(positions)):
        fail("Non-finite fractional atomic coordinates")

    min_dist = float("inf")
    n_atoms = len(atoms)
    numbers = atoms.get_atomic_numbers()
    if n_atoms > 1:
        dmat = atoms.get_all_distances(mic=True)
        for i in range(n_atoms):
            nearest_i = float("inf")
            for j in range(n_atoms):
                if i == j:
                    continue
                d = float(dmat[i, j])
                if not math.isfinite(d):
                    fail("Non-finite interatomic distance")
                if d < min_dist:
                    min_dist = d
                if d < nearest_i:
                    nearest_i = d
                rc_i = float(ase.data.covalent_radii[numbers[i]])
                rc_j = float(ase.data.covalent_radii[numbers[j]])
                threshold = max(0.55, 0.45 * (rc_i + rc_j))
                if d < threshold:
                    fail(f"Unphysical interatomic overlap ({d:.4f} A < {threshold:.4f} A) between sites {i} and {j}")
            if nearest_i > 6.0:
                fail(f"Isolated atomic site {i} with nearest neighbor {nearest_i:.4f} A > 6.0 A")
    else:
        min_dist = float(min(a, b, c))

    sga = SpacegroupAnalyzer(structure, symprec=1e-3)
    sg_symbol = sga.get_space_group_symbol()
    sg_num = sga.get_space_group_number()
    reduced = structure.get_reduced_structure()
    canonical_repr = json.dumps({
        "formula": structure.composition.reduced_formula,
        "lattice": np.round(reduced.lattice.matrix, 5).tolist(),
        "sites": sorted(
            [str(s.specie), np.round(s.frac_coords % 1.0, 5).tolist()]
            for s in reduced.sites
        ),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    struct_hash = hashlib.sha256(canonical_repr).hexdigest()

    return {
        "formula": structure.composition.reduced_formula,
        "num_sites": len(structure),
        "spacegroup": sg_symbol,
        "spacegroup_number": sg_num,
        "volume_a3": round(float(vol), 6),
        "min_bond_distance_a": round(float(min_dist), 6),
        "structure_hash": struct_hash,
    }


def run_qe_and_metric_checks(payload):
    pw_bin = shutil.which("pw.x")
    if not pw_bin:
        fail("Quantum ESPRESSO pw.x binary missing from image")

    qe_ran = False
    pw_input = payload.get("pw_in")
    if not pw_input and os.path.isfile("/work/pw.in"):
        with open("/work/pw.in", "r", encoding="utf-8") as f:
            pw_input = f.read()

    if pw_input:
        pw_path = "/tmp/pw.in"
        with open(pw_path, "w", encoding="utf-8") as f:
            f.write(pw_input)
        env = os.environ.copy()
        if os.path.isdir("/work/pseudo") and "ESPRESSO_PSEUDO" not in env:
            env["ESPRESSO_PSEUDO"] = "/work/pseudo"
        proc = subprocess.run(
            [pw_bin, "-in", pw_path],
            cwd="/tmp",
            env=env,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if proc.returncode != 0 or "JOB DONE" not in proc.stdout:
            tail = (proc.stdout + "\n" + proc.stderr)[-1500:]
            fail(f"Quantum ESPRESSO pw.x failed: {tail}")
        if "convergence NOT achieved" in proc.stdout:
            fail("Quantum ESPRESSO SCF convergence not achieved")
        qe_ran = True

    if "phonon_frequencies_thz" in payload:
        freqs = np.asarray(payload["phonon_frequencies_thz"], dtype=np.float64)
        if freqs.size == 0 or not np.all(np.isfinite(freqs)):
            fail("Invalid phonon_frequencies_thz array")
        if float(np.min(freqs)) < -0.1:
            fail(f"Dynamic instability: imaginary phonon mode {float(np.min(freqs)):.4f} THz < -0.1 THz")

    if "tc_k" in payload:
        tc = payload["tc_k"]
        if not isinstance(tc, (int, float)) or not math.isfinite(tc) or tc < 0.0 or tc > 400.0:
            fail(f"Unphysical claimed Tc: {tc}")

    if "e_hull_ev" in payload:
        e_hull = payload["e_hull_ev"]
        if not isinstance(e_hull, (int, float)) or not math.isfinite(e_hull) or e_hull < -1e-4 or e_hull > 2.0:
            fail(f"Invalid energy above hull: {e_hull}")

    return {"qe_binary": pw_bin, "qe_ran": qe_ran}


def main():
    cif_text, payload = load_submission()
    cif_metrics = validate_cif_and_bonds(cif_text)
    qe_metrics = run_qe_and_metric_checks(payload)
    print(json.dumps({"ok": True, **cif_metrics, **qe_metrics}, sort_keys=True))


if __name__ == "__main__":
    main()
