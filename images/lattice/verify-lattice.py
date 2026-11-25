#!/usr/bin/env python3
import hashlib
import json
import math
import os
import re
import sys

import numpy as np

HEX64_RE = re.compile(r"^[a-f0-9]{64}$")


def fail(msg):
    print(f"Rejected: {msg}", file=sys.stderr)
    sys.exit(1)


def sha256_hex(raw_bytes):
    return hashlib.sha256(raw_bytes).hexdigest()


def merkle_pair(left_hex, right_hex):
    return sha256_hex(bytes.fromhex(left_hex) + bytes.fromhex(right_hex))


def compute_merkle_root(leaves):
    if not leaves:
        fail("Merkle leaf list is empty")
    layer = list(leaves)
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        next_layer = []
        for i in range(0, len(layer), 2):
            next_layer.append(merkle_pair(layer[i], layer[i + 1]))
        layer = next_layer
    return layer[0]


def verify_merkle_proof(leaf_hash, proof, expected_root):
    cur = leaf_hash
    for step in proof:
        if not isinstance(step, dict):
            fail("Invalid merkle_proof entry")
        sibling = step.get("sibling")
        pos = step.get("position")
        if not isinstance(sibling, str) or not HEX64_RE.match(sibling):
            fail("Invalid sibling hash in merkle_proof")
        if pos == "left":
            cur = merkle_pair(sibling, cur)
        elif pos == "right":
            cur = merkle_pair(cur, sibling)
        else:
            fail("Invalid sibling position in merkle_proof")
    if cur != expected_root:
        fail(f"Merkle proof root mismatch: computed {cur}, expected {expected_root}")


def compute_segment_hash(data):
    if "segment_bytes_hex" in data:
        raw = bytes.fromhex(str(data["segment_bytes_hex"]))
        return sha256_hex(raw)
    if "segment_state" in data:
        arr = np.asarray(data["segment_state"], dtype=np.float64)
        return sha256_hex(arr.tobytes(order="C"))
    if "segment" in data:
        canonical = json.dumps(data["segment"], sort_keys=True, separators=(",", ":")).encode("utf-8")
        return sha256_hex(canonical)
    return None


def verify_su_matrices(links, group):
    n = 2 if group == "SU(2)" else 3
    arr = np.asarray(links, dtype=np.float64)
    if arr.ndim < 3 or arr.shape[-3:] != (n, n, 2):
        fail(f"Invalid link tensor shape {arr.shape} for {group}")
    c_links = arr[..., 0] + 1j * arr[..., 1]
    flat = c_links.reshape(-1, n, n)
    eye = np.eye(n, dtype=np.complex128)
    for idx in range(flat.shape[0]):
        mat = flat[idx]
        unitary_err = np.max(np.abs(mat.conj().T @ mat - eye))
        if not math.isfinite(unitary_err) or unitary_err > 1e-8:
            fail(f"Non-unitary link matrix at index {idx}: err={unitary_err}")
        det_err = abs(np.linalg.det(mat) - 1.0)
        if not math.isfinite(det_err) or det_err > 1e-8:
            fail(f"Invalid determinant at index {idx}: err={det_err}")


def compute_plaquette_from_lattice(links, group):
    n = 2 if group == "SU(2)" else 3
    arr = np.asarray(links, dtype=np.float64)
    if arr.ndim != 7 or arr.shape[4:] != (4, n, n, 2):
        return None
    verify_su_matrices(arr, group)
    u = arr[..., 0] + 1j * arr[..., 1]
    total = np.float64(0.0)
    count = 0
    for mu in range(4):
        for nu in range(mu + 1, 4):
            u_mu_x = u[:, :, :, :, mu]
            u_nu_xmu = np.roll(u[:, :, :, :, nu], shift=-1, axis=mu)
            u_mu_xnu = np.roll(u[:, :, :, :, mu], shift=-1, axis=nu)
            u_nu_x = u[:, :, :, :, nu]
            plaq = u_mu_x @ u_nu_xmu @ np.swapaxes(u_mu_xnu.conj(), -1, -2) @ np.swapaxes(u_nu_x.conj(), -1, -2)
            tr = np.trace(plaq, axis1=-2, axis2=-1).real / np.float64(n)
            total += np.sum(tr, dtype=np.float64)
            count += tr.size
    return float(total / np.float64(count))


def main():
    sub_path = "/work/submission.dat"
    if not os.path.isfile(sub_path) or os.path.getsize(sub_path) == 0:
        fail("Missing or empty /work/submission.dat")

    try:
        with open(sub_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        fail(f"Invalid JSON payload: {exc}")

    if not isinstance(data, dict):
        fail("Submission JSON must be an object")

    beta = data.get("beta")
    plaquette = data.get("plaquette")
    merkle_root = data.get("merkle_root")
    sweeps = data.get("sweeps")
    group = data.get("group", "SU(3)")

    if group not in ("SU(2)", "SU(3)"):
        fail(f"Unsupported gauge group: {group}")
    if not isinstance(beta, (int, float)) or not math.isfinite(beta) or beta <= 0.0 or beta > 20.0:
        fail("Missing or out-of-range coupling beta")
    if not isinstance(plaquette, (int, float)) or not math.isfinite(plaquette) or plaquette <= 0.0 or plaquette > 1.0:
        fail("Missing or unphysical plaquette value")
    if not isinstance(merkle_root, str) or not HEX64_RE.match(merkle_root):
        fail("Missing or invalid 64-hex merkle_root")
    if not isinstance(sweeps, int) or isinstance(sweeps, bool) or sweeps < 100:
        fail("Sweeps must be an integer >= 100")

    recomputed_seg_hash = compute_segment_hash(data)
    claimed_seg_hash = data.get("segment_hash")
    if claimed_seg_hash is not None:
        if not isinstance(claimed_seg_hash, str) or not HEX64_RE.match(claimed_seg_hash):
            fail("Invalid segment_hash format")
        if recomputed_seg_hash is not None and recomputed_seg_hash != claimed_seg_hash:
            fail(f"Segment hash mismatch: computed {recomputed_seg_hash}, claimed {claimed_seg_hash}")

    active_seg_hash = recomputed_seg_hash or claimed_seg_hash

    if "checkpoint_hashes" in data:
        leaves = data["checkpoint_hashes"]
        if not isinstance(leaves, list) or not leaves:
            fail("checkpoint_hashes must be a non-empty list")
        for h in leaves:
            if not isinstance(h, str) or not HEX64_RE.match(h):
                fail("Invalid leaf hash in checkpoint_hashes")
        seg_idx = data.get("segment_index")
        if seg_idx is not None:
            if not isinstance(seg_idx, int) or seg_idx < 0 or seg_idx >= len(leaves):
                fail("segment_index out of range")
            if active_seg_hash is not None and leaves[seg_idx] != active_seg_hash:
                fail("Segment hash does not match checkpoint_hashes[segment_index]")
        computed_root = compute_merkle_root(leaves)
        if computed_root != merkle_root:
            fail(f"Merkle root mismatch: computed {computed_root}, claimed {merkle_root}")
    elif "merkle_proof" in data:
        if active_seg_hash is None:
            fail("merkle_proof requires segment_hash or segment data")
        verify_merkle_proof(active_seg_hash, data["merkle_proof"], merkle_root)
    elif active_seg_hash is not None:
        if active_seg_hash != merkle_root:
            fail(f"Single-segment hash {active_seg_hash} does not match merkle_root {merkle_root}")
    else:
        fail("Missing checkpoint_hashes, merkle_proof, or segment_hash to verify merkle_root")

    computed_plaq = None
    if "segment_state" in data:
        computed_plaq = compute_plaquette_from_lattice(data["segment_state"], group)
        if computed_plaq is not None:
            target_plaq = data.get("segment_plaquette", plaquette)
            if abs(computed_plaq - float(target_plaq)) > 1e-8:
                fail(f"Plaquette mismatch: computed {computed_plaq:.12f}, claimed {target_plaq}")

    if "checkpoint_plaquettes" in data:
        vals = np.asarray(data["checkpoint_plaquettes"], dtype=np.float64)
        if vals.size == 0 or not np.all(np.isfinite(vals)):
            fail("Invalid checkpoint_plaquettes array")
        mean_plaq = float(np.mean(vals, dtype=np.float64))
        if abs(mean_plaq - float(plaquette)) > 1e-8:
            fail(f"Mean plaquette mismatch: computed {mean_plaq:.12f}, claimed {plaquette}")

    print(json.dumps({
        "ok": True,
        "group": group,
        "beta": float(beta),
        "plaquette": float(plaquette),
        "computed_plaquette": computed_plaq,
        "merkle_root": merkle_root,
        "segment_hash": active_seg_hash,
        "sweeps": sweeps,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
