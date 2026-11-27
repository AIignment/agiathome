#!/usr/bin/env python3
"""
AGI@home distributed compute & verification runner.
100% Python 3 standard library — zero external dependencies.
"""

import argparse
import base64
import datetime
import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid

DEFAULT_BASE = os.environ.get("AGI_BASE", "https://agi.alignment.id")
DEFAULT_CONFIG_PATH = os.environ.get(
    "AGI_CONFIG", os.path.expanduser("~/.config/agihome/credentials.json")
)
USER_AGENT = "agihome-runner/0.2"
BUILTIN_TASK_TYPES = frozenset({"prime_range", "monte_carlo", "faers_disprop", "hash_search"})
MASK64 = 0xFFFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------
# Hardware auto-detection
# ---------------------------------------------------------------------------


def _detect_ram_gb() -> float:
    try:
        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024.0 * 1024.0), 2)
    except Exception:
        pass
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if proc.returncode == 0 and proc.stdout.strip().isdigit():
                return round(int(proc.stdout.strip()) / (1024.0 ** 3), 2)
        except Exception:
            pass
    return 4.0


def _detect_docker() -> tuple[bool, str]:
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return False, "MISSING"
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return True, proc.stdout.strip() or "running"
        return False, "UNREACHABLE"
    except Exception:
        return False, "UNREACHABLE"


def _detect_gpu() -> tuple[str, float]:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return "", 0.0
    try:
        proc = subprocess.run(
            [
                smi,
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split(",")]
                name = parts[0] if parts else ""
                vram_gb = 0.0
                if len(parts) >= 2:
                    try:
                        vram_gb = round(float(parts[1]) / 1024.0, 2)
                    except ValueError:
                        vram_gb = 0.0
                return name, vram_gb
    except Exception:
        pass
    return "", 0.0


def detect_hardware() -> dict:
    cpu_cores = int(os.cpu_count() or 1)
    ram_gb = _detect_ram_gb()
    arch = platform.machine() or "unknown"
    has_docker, _ = _detect_docker()
    gpu_name, vram_gb = _detect_gpu()
    return {
        "cpu_cores": cpu_cores,
        "ram_gb": ram_gb,
        "gpu_name": gpu_name,
        "vram_gb": vram_gb,
        "arch": arch,
        "has_docker": has_docker,
    }


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------


def canonical_json_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
            if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
                redirected.remove_header("Authorization")
        return redirected


HTTP_OPENER = urllib.request.build_opener(SafeRedirectHandler())


def api_request(base, path, method="GET", token=None, payload=None, headers=None, timeout=30):
    req_headers = {"User-Agent": USER_AGENT}
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    url = f"{base.rstrip('/')}{path}"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with HTTP_OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.status == 204 or not raw:
                return resp.status, None
            return resp.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw}
    except urllib.error.URLError as e:
        return 0, {"error": str(e)}


def api_download_bytes(base, path, token=None, timeout=60) -> bytes:
    req_headers = {"User-Agent": USER_AGENT}
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    url = f"{base.rstrip('/')}{path}"
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    with HTTP_OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


def api_submit_multipart(
    base,
    path,
    token,
    meta: dict,
    artifact_bytes: bytes,
    filename: str = "artifact.json",
    idempotency_key: str | None = None,
    timeout: int = 60,
):
    boundary = f"----AgiHome{uuid.uuid4().hex}"
    meta_bytes = canonical_json_bytes(meta)
    parts = [
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="meta"\r\n'
            f"Content-Type: application/json\r\n\r\n"
        ).encode("utf-8")
        + meta_bytes
        + b"\r\n",
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="artifact"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        + artifact_bytes
        + b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(parts)
    req_headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
    }
    url = f"{base.rstrip('/')}{path}"
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with HTTP_OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, None
            return resp.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw}
    except urllib.error.URLError as e:
        return 0, {"error": str(e)}


# ---------------------------------------------------------------------------
# Registration & credentials
# ---------------------------------------------------------------------------


def load_or_register(
    base,
    handle=None,
    caps=None,
    enroll_code=None,
    label=None,
    hardware=None,
    token_override=None,
    config_path=None,
):
    if token_override:
        return {
            "base": base,
            "agent_id": "token-override",
            "handle": handle or "runner",
            "token": token_override,
        }

    cfg_path = config_path or DEFAULT_CONFIG_PATH
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = None
        if (
            isinstance(cfg, dict)
            and cfg.get("base") == base
            and cfg.get("token")
            and (enroll_code or not handle or cfg.get("handle") == handle)
        ):
            if enroll_code:
                print(
                    f"Using saved credentials for {cfg.get('handle')} in {cfg_path}; ignoring --enroll.",
                    file=sys.stderr,
                )
            return cfg

    hw = hardware or detect_hardware()
    cap_list = list(dict.fromkeys((caps or ["verify", "crunch"]) + (["docker"] if hw.get("has_docker") else ["builtin"])))
    payload = {
        "handle": handle or f"runner-{uuid.uuid4().hex[:6]}",
        "caps": cap_list[:8],
        "model": "compute-runner",
        "harness": "agihome.py",
        "hardware": hw,
    }
    if enroll_code:
        payload["enroll_code"] = enroll_code
    if label:
        payload["label"] = label

    status, data = api_request(base, "/api/v1/agents", method="POST", payload=payload)
    if status not in (200, 201) or not data or "token" not in data:
        print(f"Registration failed ({status}): {data}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(cfg_path) or ".", mode=0o700, exist_ok=True)
    cfg = {
        "base": base,
        "agent_id": data["agent_id"],
        "handle": data["handle"],
        "token": data["token"],
    }
    fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass
    print(f"Registered agent {cfg['handle']} ({cfg['agent_id']})")
    return cfg


# ---------------------------------------------------------------------------
# Background Heartbeat Thread (cancels immediately on HTTP 410)
# ---------------------------------------------------------------------------


class HeartbeatThread(threading.Thread):
    def __init__(self, base: str, token: str, lease_id: str, interval: float = 60.0):
        super().__init__(daemon=True)
        self.base = base
        self.token = token
        self.lease_id = lease_id
        self.interval = max(2.0, float(interval))
        self.start_time = time.monotonic()
        self.stop_event = threading.Event()
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._container_name: str | None = None

    def set_process(self, proc: subprocess.Popen | None):
        with self._lock:
            self._proc = proc
        if self.cancelled.is_set() and proc is not None:
            self._terminate_active()

    def set_container(self, cname: str | None):
        with self._lock:
            self._container_name = cname
        if self.cancelled.is_set() and cname:
            self._terminate_active()

    def elapsed_sec(self) -> int:
        return max(1, int(round(time.monotonic() - self.start_time)))

    def _terminate_active(self):
        with self._lock:
            cname = self._container_name
            proc = self._proc
        if cname:
            try:
                subprocess.run(
                    ["docker", "kill", cname],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass

    def stop(self):
        self.stop_event.set()

    def run(self):
        while not self.stop_event.wait(self.interval):
            status, resp = api_request(
                self.base,
                "/api/v1/desk/beat",
                method="POST",
                token=self.token,
                payload={
                    "lease_id": self.lease_id,
                    "cpu_sec": self.elapsed_sec(),
                    "model": "compute-runner",
                    "harness": "agihome.py",
                },
                timeout=15,
            )
            if status == 410:
                print(
                    f"[heartbeat] Lease {self.lease_id} returned 410 ({resp}); aborting active job.",
                    file=sys.stderr,
                )
                self.cancelled.set()
                self._terminate_active()
                break


# ---------------------------------------------------------------------------
# Whitelisted Deterministic Built-In Compute & Verify Engine
# ---------------------------------------------------------------------------


def _segmented_primes(range_start: int, range_end: int) -> tuple[int, int, int, str]:
    lo = max(2, int(range_start))
    hi = int(range_end)
    if hi <= lo:
        return 0, 0, 0, sha256_hex(b"")

    limit = math.isqrt(hi - 1)
    small = bytearray(b"\x01") * (limit + 1)
    if limit >= 0:
        small[0] = 0
    if limit >= 1:
        small[1] = 0
    for p in range(2, math.isqrt(limit) + 1):
        if small[p]:
            small[p * p : limit + 1 : p] = b"\x00" * ((limit - p * p) // p + 1)
    base_primes = [p for p in range(2, limit + 1) if small[p]]

    size = hi - lo
    seg = bytearray(b"\x01") * size
    for p in base_primes:
        start = max(p * p, ((lo + p - 1) // p) * p)
        if start < hi:
            seg[start - lo : size : p] = b"\x00" * ((hi - 1 - start) // p + 1)

    count = 0
    total = 0
    last_prime = 0
    h = hashlib.sha256()
    for idx, flag in enumerate(seg):
        if flag:
            val = lo + idx
            count += 1
            total += val
            last_prime = val
            h.update(f"{val}\n".encode("ascii"))
    return count, total, last_prime, h.hexdigest()


def _splitmix64_next(state: int) -> tuple[int, int]:
    state = (state + 0x9E3779B97F4A7C15) & MASK64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    z = z ^ (z >> 31)
    return state, z


def _run_prime_range(params: dict) -> tuple[dict, dict]:
    range_start = int(params.get("range_start", params.get("start", 2)))
    range_end = int(params.get("range_end", params.get("end", 10000)))
    prime_count, prime_sum, last_prime, checksum = _segmented_primes(range_start, range_end)
    artifact = {
        "task_type": "prime_range",
        "range_start": range_start,
        "range_end": range_end,
        "prime_count": prime_count,
        "prime_sum": prime_sum,
        "last_prime": last_prime,
        "checksum": checksum,
    }
    metrics = {"prime_count": prime_count, "prime_sum": prime_sum}
    return artifact, metrics


def _run_monte_carlo(params: dict) -> tuple[dict, dict]:
    range_start = int(params.get("range_start", params.get("start", 0)))
    default_samples = int(params.get("samples", 10000))
    range_end = int(params.get("range_end", params.get("end", range_start + default_samples)))
    samples = max(1, int(params.get("samples", max(1, range_end - range_start))))
    seed = int(params.get("seed", 42))

    state = ((seed & MASK64) ^ ((range_start * 0x9E3779B97F4A7C15) & MASK64)) & MASK64
    inv_2_53 = 1.0 / float(1 << 53)
    mean = 0.0
    m2 = 0.0
    for i in range(1, samples + 1):
        state, z = _splitmix64_next(state)
        x = (z >> 11) * inv_2_53
        val = 4.0 / (1.0 + x * x)
        delta = val - mean
        mean += delta / i
        m2 += delta * (val - mean)

    variance = (m2 / (samples - 1)) if samples > 1 else 0.0
    estimate_r = round(mean, 8)
    variance_r = round(variance, 8)
    artifact = {
        "task_type": "monte_carlo",
        "seed": seed,
        "range_start": range_start,
        "range_end": range_end,
        "samples": samples,
        "estimate": estimate_r,
        "variance": variance_r,
    }
    metrics = {"estimate": estimate_r, "variance": variance_r}
    return artifact, metrics


def _extract_faers_counts(params: dict, input_blobs: list[bytes] | None = None) -> tuple[float, float, float, float]:
    src = params
    if all(type(src.get(k)) is int and src[k] >= 0 for k in ("a", "b", "c", "d")):
        return float(src["a"]), float(src["b"]), float(src["c"]), float(src["d"])

    if isinstance(src.get("contingency"), dict):
        ct = src["contingency"]
        if all(type(ct.get(k)) is int and ct[k] >= 0 for k in ("a", "b", "c", "d")):
            return float(ct["a"]), float(ct["b"]), float(ct["c"]), float(ct["d"])

    if input_blobs:
        a_sum = b_sum = c_sum = d_sum = 0.0
        found = False
        for raw in input_blobs:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            rows = parsed if isinstance(parsed, list) else parsed.get("records", [parsed]) if isinstance(parsed, dict) else []
            for row in rows:
                if isinstance(row, dict) and all(type(row.get(k)) is int and row[k] >= 0 for k in ("a", "b", "c", "d")):
                    a_sum += float(row["a"])
                    b_sum += float(row["b"])
                    c_sum += float(row["c"])
                    d_sum += float(row["d"])
                    found = True
        if found:
            return a_sum, b_sum, c_sum, d_sum

    raise ValueError(
        "faers_disprop step carries no real 2x2 counts: params have no integer a, b, c, d "
        "(directly or under 'contingency') and no input blob supplies them. Refusing to invent numbers."
    )


def _run_faers_disprop(params: dict, input_blobs: list[bytes] | None = None) -> tuple[dict, dict]:
    a_raw, b_raw, c_raw, d_raw = _extract_faers_counts(params, input_blobs)
    corr = 0.5 if min(a_raw, b_raw, c_raw, d_raw) <= 0 else 0.0
    a = a_raw + corr
    b = b_raw + corr
    c = c_raw + corr
    d = d_raw + corr
    n = a + b + c + d

    prr = (a / (a + b)) / (c / (c + d))
    ror = (a * d) / (b * c)
    expected = ((a + b) * (a + c)) / n
    ic = math.log2((a + 0.5) / (expected + 0.5))
    ic_sd = math.sqrt(1.0 / (a + 0.5) + 1.0 / (expected + 0.5)) / math.log(2.0)
    ic025 = ic - 1.96 * ic_sd

    prr_r = round(prr, 6)
    ror_r = round(ror, 6)
    ic025_r = round(ic025, 6)
    n_cases = int(round(a_raw))

    artifact = {
        "task_type": "faers_disprop",
        "a": int(round(a_raw)),
        "b": int(round(b_raw)),
        "c": int(round(c_raw)),
        "d": int(round(d_raw)),
        "prr": prr_r,
        "ror": ror_r,
        "ic025": ic025_r,
        "n_cases": n_cases,
    }
    metrics = {"prr": prr_r, "ror": ror_r, "ic025": ic025_r}
    return artifact, metrics


def _run_hash_search(params: dict) -> tuple[dict, dict]:
    range_start = int(params.get("range_start", params.get("start", 0)))
    range_end = int(params.get("range_end", params.get("end", range_start + 5000)))
    prefix = str(params.get("prefix", "agihome:"))
    target_zeros = int(params.get("target_zeros", 4))

    best_zeros = -1
    best_nonce = range_start
    best_hash = ""
    checked = 0
    for nonce in range(range_start, max(range_start, range_end)):
        digest = hashlib.sha256(f"{prefix}{nonce}".encode("utf-8")).hexdigest()
        zeros = len(digest) - len(digest.lstrip("0"))
        checked += 1
        if zeros > best_zeros:
            best_zeros = zeros
            best_nonce = nonce
            best_hash = digest

    if best_zeros < 0:
        best_zeros = 0
    artifact = {
        "task_type": "hash_search",
        "prefix": prefix,
        "target_zeros": target_zeros,
        "range_start": range_start,
        "range_end": range_end,
        "checked": checked,
        "best_zeros": best_zeros,
        "best_nonce": best_nonce,
        "best_hash": best_hash,
    }
    metrics = {"checked": checked, "best_zeros": best_zeros}
    return artifact, metrics


BUILTIN_WORKER_ARG = "_builtin_worker"
RAM_UNIT_BYTES = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def parse_ram_bytes(spec) -> int:
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)\s*i?b?\s*", str(spec or "").lower())
    if not m:
        raise ValueError(f"Unrecognised --max-ram value {spec!r}; use for example 4g, 2048m, or 512m.")
    return int(float(m.group(1)) * RAM_UNIT_BYTES[m.group(2)])


def _limits_supported() -> bool:
    try:
        import resource
    except Exception:
        return False
    return os.name == "posix"


def _builtin_worker_main() -> int:
    import resource

    payload = json.loads(sys.stdin.read())
    max_cpu = float(payload.get("max_cpu") or 1.0)
    max_ram_bytes = int(payload.get("max_ram_bytes") or 0)
    budget_sec = max(1, int(payload.get("budget_sec") or 900))
    cpu_sec = max(1, int(math.ceil(max_cpu * budget_sec)))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec))
    if max_ram_bytes > 0:
        for name in ("RLIMIT_AS", "RLIMIT_DATA"):
            res = getattr(resource, name, None)
            if res is None:
                continue
            try:
                resource.setrlimit(res, (max_ram_bytes, max_ram_bytes))
                break
            except (ValueError, OSError):
                continue
        else:
            raise RuntimeError("Cannot enforce builtin memory limit on this platform")
    blobs = [base64.b64decode(b) for b in payload.get("blobs") or []]
    artifact, metrics = run_builtin_task(payload["task_type"], payload.get("params") or {}, blobs)
    sys.stdout.write(json.dumps({"artifact": artifact, "metrics": metrics}))
    return 0


def run_builtin_limited(
    task_type: str,
    params: dict,
    input_blobs: list[bytes] | None = None,
    max_cpu: float | None = None,
    max_ram: str | None = None,
    budget_sec: int = 900,
) -> tuple[dict, dict]:
    if max_cpu is None and max_ram is None:
        return run_builtin_task(task_type, params, input_blobs)
    if not _limits_supported():
        return run_builtin_task(task_type, params, input_blobs)

    cpu = float(max_cpu) if max_cpu is not None else float(os.cpu_count() or 1)
    ram_bytes = parse_ram_bytes(max_ram or "4g")
    payload = {
        "task_type": task_type,
        "params": params,
        "blobs": [base64.b64encode(b).decode("ascii") for b in (input_blobs or [])],
        "max_cpu": cpu,
        "max_ram_bytes": ram_bytes,
        "budget_sec": max(1, int(budget_sec)),
    }
    cpu_sec = max(1, int(math.ceil(cpu * max(1, int(budget_sec)))))
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), BUILTIN_WORKER_ARG],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=cpu_sec + 60,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-400:]
        if proc.returncode < 0:
            sig = -proc.returncode
            raise RuntimeError(
                f"Builtin worker killed by signal {sig} under --max-cpu={cpu} --max-ram={max_ram}: {detail}"
            )
        raise RuntimeError(f"Builtin worker failed (rc={proc.returncode}) under --max-cpu={cpu} --max-ram={max_ram}: {detail}")
    parsed = json.loads(proc.stdout)
    return parsed["artifact"], parsed["metrics"]


def run_builtin_task(task_type: str, params: dict, input_blobs: list[bytes] | None = None) -> tuple[dict, dict]:
    if task_type == "prime_range":
        return _run_prime_range(params)
    if task_type == "monte_carlo":
        return _run_monte_carlo(params)
    if task_type == "faers_disprop":
        return _run_faers_disprop(params, input_blobs)
    if task_type == "hash_search":
        return _run_hash_search(params)
    raise ValueError(f"Unsupported builtin task_type: {task_type}")


def verify_builtin_artifact(
    task_type: str,
    target_params: dict,
    submitted_bytes: bytes,
    input_blobs: list[bytes] | None = None,
    tol: float = 1e-4,
    max_cpu: float | None = None,
    max_ram: str | None = None,
    budget_sec: int = 900,
) -> tuple[str, str, dict, str]:
    expected_artifact, expected_metrics = run_builtin_limited(
        task_type, target_params, input_blobs, max_cpu, max_ram, budget_sec
    )
    expected_canonical = canonical_json_bytes(expected_artifact)
    own_hash = sha256_hex(expected_canonical)
    submitted_hash = sha256_hex(submitted_bytes)
    out_hash = own_hash

    try:
        submitted = json.loads(submitted_bytes.decode("utf-8"))
    except Exception as e:
        return "REJECTED", out_hash, expected_metrics, f"Submitted artifact is not valid JSON: {e}"

    if not isinstance(submitted, dict):
        return "REJECTED", out_hash, expected_metrics, "Submitted artifact must be a JSON object."

    if submitted.get("task_type") and submitted.get("task_type") != task_type:
        return (
            "REJECTED",
            out_hash,
            expected_metrics,
            f"task_type mismatch: got {submitted.get('task_type')}, expected {task_type}",
        )

    mismatches = []
    sub_metrics = submitted.get("metrics") if isinstance(submitted.get("metrics"), dict) else {}
    for k, exp_val in expected_metrics.items():
        raw_val = submitted.get(k, sub_metrics.get(k))
        if raw_val is None or not isinstance(raw_val, (int, float)):
            mismatches.append(f"missing numeric metric {k}")
            continue
        diff = abs(float(raw_val) - float(exp_val))
        allowed = max(tol, tol * abs(float(exp_val)))
        if diff > allowed:
            mismatches.append(f"{k}: got {raw_val}, expected {exp_val} (diff={diff:.6g})")

    for exact_key in ("best_hash", "checksum"):
        if exact_key in expected_artifact and exact_key in submitted:
            if submitted[exact_key] != expected_artifact[exact_key]:
                mismatches.append(f"{exact_key} mismatch")

    if task_type == "faers_disprop":
        for key in ("a", "b", "c", "d"):
            want = expected_artifact[key]
            got = submitted.get(key)
            if not isinstance(got, int) or isinstance(got, bool):
                mismatches.append(f"{key}: submitted artifact has no integer {key}")
            elif got != want:
                mismatches.append(f"{key}: got {got}, expected {want}")

    if mismatches:
        return "REJECTED", own_hash, expected_metrics, "Verification mismatch: " + "; ".join(mismatches)

    return (
        "ACCEPTED",
        submitted_hash,
        expected_metrics,
        f"Verified builtin {task_type} deterministically (submitted sha={submitted_hash[:12]})",
    )


# ---------------------------------------------------------------------------
# Blob download & Docker sandbox execution
# ---------------------------------------------------------------------------


def download_input_blobs(base: str, token: str, blob_specs: list, dest_dir: str | None = None) -> list[bytes]:
    results: list[bytes] = []
    for idx, entry in enumerate(blob_specs or []):
        if isinstance(entry, str):
            sha = entry.strip()
            name = sha
        elif isinstance(entry, dict):
            sha = str(entry.get("sha") or entry.get("hash") or "").strip()
            name = os.path.basename(str(entry.get("name") or sha))
        else:
            continue
        if not sha:
            continue
        raw = api_download_bytes(base, f"/api/v1/blob/{sha}", token=token)
        actual = sha256_hex(raw)
        if actual != sha.lower():
            raise ValueError(f"Input blob SHA-256 mismatch for {name}: expected {sha}, got {actual}")
        results.append(raw)
        if dest_dir:
            out_path = os.path.join(dest_dir, name or f"blob-{idx}.bin")
            with open(out_path, "wb") as f:
                f.write(raw)
    return results


def parse_lean_target(statement: str) -> dict | None:
    import re
    m = re.match(
        r"^\s*(?:theorem|lemma)\s+([A-Za-z_][A-Za-z0-9_.']*)\s*(.*?)\s*:=\s*(?:by\s+sorry|sorry)\s*$",
        statement or "",
        re.S,
    )
    if not m or not m.group(2).strip():
        return None
    return {"theorem": m.group(1), "signature": m.group(2).strip()}


def _safe_step_dirname(step_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in step_id)[:80] or "step"


MAX_STREAM_BYTES = 4 * 1024 * 1024
APPROVED_IMAGES = frozenset({
    "ghcr.io/aiignment/agihome-lean-core",
    "ghcr.io/aiignment/agihome-lean-mathlib",
    "ghcr.io/aiignment/agihome-ci-verifier",
})


def _validate_runner_image(image: str) -> None:
    if not isinstance(image, str):
        raise ValueError("Missing container image")
    repo, separator, digest = image.partition("@sha256:")
    if repo not in APPROVED_IMAGES or not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"Refusing unapproved or unpinned runner image '{image}'.")


def _read_stream_capped(stream, limit: int, out_bucket: list):
    buf = bytearray()
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            rem = limit - len(buf)
            if rem > 0:
                buf.extend(chunk[:rem])
    except Exception:
        pass
    finally:
        out_bucket.append(bytes(buf))


def _read_trusted_output_file(out_dir: str, filename: str, max_bytes: int) -> bytes | None:
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        return None
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    dir_fd = -1
    try:
        dir_fd = os.open(out_dir, dir_flags)
        fd = os.open(filename, file_flags, dir_fd=dir_fd)
    except OSError:
        return None
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0 or st.st_size > max_bytes:
            os.close(fd)
            return None
        with os.fdopen(fd, "rb") as f:
            data = f.read(max_bytes + 1)
        if len(data) == 0 or len(data) > max_bytes:
            return None
        return data
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def run_docker_job(
    image: str,
    mode: str,
    step: dict,
    execution: dict,
    base: str,
    token: str,
    max_cpu: float,
    max_ram: str,
    hb: HeartbeatThread,
    submission_bytes: bytes | None = None,
) -> tuple[int, bytes | None, dict, str, str]:
    """
    Runs a hardened Docker sandbox container for either `crunch` or `verify`.
    Returns (returncode, artifact_bytes_or_none, parsed_metrics, output_hash, log_tail).
    """
    timeout_sec = int((execution.get("resources") or {}).get("timeout_sec") or 600)
    ckpt_dir = os.path.expanduser(
        f"~/.cache/agihome/checkpoints/{_safe_step_dirname(str(step.get('id', 'step')))}"
    )
    os.makedirs(ckpt_dir, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="agihome-work-") as tmp:
        in_dir = os.path.join(tmp, "in")
        out_dir = os.path.join(tmp, "out")
        os.makedirs(in_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)
        os.chmod(in_dir, 0o755)
        os.chmod(out_dir, 0o733)

        download_input_blobs(base, token, execution.get("input_blobs") or [], dest_dir=in_dir)

        sub_path = None
        if submission_bytes is not None:
            sub_path = os.path.join(in_dir, "submission.dat")
            with open(sub_path, "wb") as f:
                f.write(submission_bytes)

        target_path = None
        target = parse_lean_target(str((step.get("params") or {}).get("target_statement") or "")) if mode == "verify" else None
        if target:
            target_path = os.path.join(tmp, "target.json")
            with open(target_path, "wb") as f:
                f.write(canonical_json_bytes(target))

        params_path = os.path.join(tmp, "params.json")
        with open(params_path, "wb") as f:
            f.write(canonical_json_bytes(step.get("params") or {}))

        step_path = os.path.join(tmp, "step.json")
        with open(step_path, "wb") as f:
            f.write(canonical_json_bytes({"step": step, "execution": execution}))

        for name in os.listdir(in_dir):
            os.chmod(os.path.join(in_dir, name), 0o644)
        for path in (params_path, step_path, target_path):
            if path:
                os.chmod(path, 0o644)

        cname = f"agihome-{uuid.uuid4().hex[:12]}"
        cmd = [
            "docker", "run", "--rm",
            "--name", cname,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--pids-limit=256",
            "--user", "65534:65534",
            "--ulimit", "nofile=1024:1024",
            "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            f"--cpus={max_cpu}",
            f"--memory={max_ram}",
            "-e", f"AGI_MODE={mode}",
            "-v", f"{in_dir}:/work/in:ro",
            "-v", f"{params_path}:/work/params.json:ro",
            "-v", f"{step_path}:/work/step.json:ro",
            "-v", f"{ckpt_dir}:/work/ckpt:ro",
            "-v", f"{out_dir}:/work/out:rw",
        ]
        if sub_path is not None:
            cmd.extend(["-v", f"{sub_path}:/work/submission.dat:ro"])
        if target_path is not None:
            cmd.extend(["-v", f"{target_path}:/work/target.json:ro"])
        cmd.append(image)

        _validate_runner_image(image)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        hb.set_container(cname)
        hb.set_process(proc)
        out_bucket: list[bytes] = []
        err_bucket: list[bytes] = []
        t_out = threading.Thread(target=_read_stream_capped, args=(proc.stdout, MAX_STREAM_BYTES, out_bucket), daemon=True)
        t_err = threading.Thread(target=_read_stream_capped, args=(proc.stderr, MAX_STREAM_BYTES, err_bucket), daemon=True)
        t_out.start()
        t_err.start()
        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", cname], capture_output=True, timeout=10)
            proc.kill()
            proc.wait(timeout=10)
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            return 124, None, {}, sha256_hex(b"timeout"), "Container timed out."
        finally:
            hb.set_process(None)
            hb.set_container(None)
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        stdout_b = out_bucket[0] if out_bucket else b""
        stderr_b = err_bucket[0] if err_bucket else b""

        stdout_s = stdout_b.decode("utf-8", errors="replace")
        stderr_s = stderr_b.decode("utf-8", errors="replace")
        combined = (stdout_s + "\n" + stderr_s).strip()

        artifact_bytes = _read_trusted_output_file(out_dir, "artifact.json", 16 * 1024 * 1024)
        metrics: dict = {}
        if artifact_bytes is None and stdout_b.strip():
            artifact_bytes = stdout_b.strip()
        out_hash = sha256_hex(submission_bytes if submission_bytes is not None else artifact_bytes or b"")

        result_bytes = _read_trusted_output_file(out_dir, "result.json", 1024 * 1024)
        if result_bytes is not None:
            try:
                res_obj = json.loads(result_bytes.decode("utf-8"))
                if isinstance(res_obj, dict) and isinstance(res_obj.get("metrics"), dict):
                    metrics = {
                        k: v for k, v in res_obj["metrics"].items()
                        if isinstance(k, str) and "hash" not in k.lower() and isinstance(v, (int, float))
                    }
            except Exception:
                pass

        return proc.returncode, artifact_bytes, metrics, out_hash, combined[-3500:]


# ---------------------------------------------------------------------------
# Time window helper (--until HH:MM)
# ---------------------------------------------------------------------------


def parse_until_deadline(until_str: str | None) -> float | None:
    if not until_str:
        return None
    parts = until_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError("--until must be in HH:MM 24-hour local time format")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("--until hour must be 00..23 and minute 00..59")
    now_dt = datetime.datetime.now()
    target = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now_dt:
        target += datetime.timedelta(days=1)
    return target.timestamp()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_doctor(args):
    hw = detect_hardware()
    has_docker, docker_status = _detect_docker()
    print(f"python: {sys.version.split()[0]}")
    print(
        f"hardware: cpu_cores={hw['cpu_cores']} ram_gb={hw['ram_gb']} arch={hw['arch']} "
        f"gpu={hw['gpu_name'] or 'none'} vram_gb={hw['vram_gb']}"
    )
    print(f"docker: {shutil.which('docker') or 'MISSING'} ({docker_status if has_docker else docker_status})")
    if _limits_supported():
        print("builtin limits: enforced by a worker subprocess with resource.setrlimit (RLIMIT_CPU, RLIMIT_AS)")
    else:
        print("builtin limits: NOT enforced on this platform; builtin tasks may use unbounded CPU and RAM")
    print("  Docker uses --cpus/--memory; builtin --max-cpu limits total CPU time, not CPU rate or cores.")
    status, stats = api_request(args.base, "/api/v1/stats")
    print(f"desk {args.base}: HTTP {status}")
    if isinstance(stats, dict) and status == 200:
        print(
            f"agents_online={stats.get('agents_online')} "
            f"open_steps={stats.get('steps_open')} "
            f"verify_queue={stats.get('verify_queue')} "
            f"cpu_hours_donated={stats.get('cpu_hours_donated', 0)}"
        )


def _resolve_execution(step: dict, lease: dict) -> tuple[dict, str, str]:
    execution = lease.get("execution") if isinstance(lease.get("execution"), dict) else {}
    params = step.get("params") if isinstance(step.get("params"), dict) else {}
    target_params = params.get("target_params") if isinstance(params.get("target_params"), dict) else {}

    task_type = str(
        execution.get("task_type")
        or params.get("task_type")
        or target_params.get("task_type")
        or ""
    ).strip()

    mode = str(execution.get("mode") or params.get("mode") or "").strip()
    if not mode:
        if task_type in BUILTIN_TASK_TYPES:
            mode = "builtin"
        elif step.get("kind") == "think":
            mode = "agent"
        else:
            mode = "docker"
    return execution, mode, task_type


def _release_lease(base: str, token: str, lease_id: str, reason: str, note: str):
    api_request(
        base,
        "/api/v1/desk/release",
        method="POST",
        token=token,
        payload={"lease_id": lease_id, "reason": reason, "note": note},
    )


def cmd_loop(args):
    hw = detect_hardware()
    deadline = parse_until_deadline(args.until)
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    if not kinds:
        kinds = ["verify", "crunch"]

    builtin_allowed = not args.docker_only
    if args.docker_only and not hw["has_docker"]:
        print("Error: --docker-only specified, but Docker daemon is unavailable.", file=sys.stderr)
        sys.exit(1)

    max_cpu = float(args.max_cpu) if args.max_cpu is not None else float(min(2, hw["cpu_cores"]))
    max_ram = args.max_ram or "4g"

    programs_list: list[str] = []
    if args.programs:
        programs_list.extend([p.strip() for p in args.programs.split(",") if p.strip()])
    if args.program and args.program.strip() and args.program.strip() not in programs_list:
        programs_list.append(args.program.strip())

    cfg = load_or_register(
        args.base,
        handle=args.handle,
        caps=kinds,
        enroll_code=args.enroll,
        label=args.label,
        hardware=hw,
        token_override=args.token or os.environ.get("AGI_TOKEN"),
        config_path=args.config,
    )
    print(
        f"[connected] AGI@home runner as {cfg['handle']} kinds={kinds} "
        f"programs={programs_list or 'all'} docker={hw['has_docker']} builtin={builtin_allowed}"
    )

    while True:
        if deadline is not None and time.time() >= deadline:
            print(f"Reached --until stop window ({args.until}); exiting cleanly.")
            return

        next_payload: dict = {
            "kinds": kinds,
            "model": "compute-runner",
            "harness": "agihome.py",
            "hardware": hw,
        }
        if len(programs_list) == 1:
            next_payload["program"] = programs_list[0]
            next_payload["programs"] = programs_list
        elif len(programs_list) > 1:
            next_payload["programs"] = programs_list

        if args.docker_only:
            next_payload["modes"] = ["docker"]
        elif not hw["has_docker"] and builtin_allowed:
            next_payload["modes"] = ["builtin"]

        status, lease = api_request(
            args.base,
            "/api/v1/desk/next",
            method="POST",
            token=cfg["token"],
            payload=next_payload,
        )
        if status not in (200, 204):
            err_detail = lease.get("error") if isinstance(lease, dict) else lease
            print(f"[error] Desk returned HTTP {status}: {err_detail}", file=sys.stderr)
            if args.once:
                sys.exit(1)
            time.sleep(args.poll)
            continue
        if status == 204 or not lease or not lease.get("lease_id"):
            if args.once:
                print("[waiting] No open tasks available.")
                return
            time.sleep(args.poll)
            continue

        step = lease["step"]
        lease_id = lease["lease_id"]
        execution, mode, task_type = _resolve_execution(step, lease)
        print(
            f"Claimed {step['kind']} step {step['id']} ({step['program']}): "
            f"{step['title']} [mode={mode}, task_type={task_type or '-'}]"
        )

        hb = HeartbeatThread(args.base, cfg["token"], lease_id, interval=args.heartbeat_interval)
        hb.start()
        t0 = time.monotonic()

        try:
            use_builtin = builtin_allowed and (
                mode == "builtin"
                or (task_type in BUILTIN_TASK_TYPES and (not hw["has_docker"] or mode != "docker"))
            )

            if step["kind"] == "crunch" and use_builtin and task_type in BUILTIN_TASK_TYPES:
                blobs = download_input_blobs(
                    args.base, cfg["token"], execution.get("input_blobs") or []
                )
                params = step.get("params") if isinstance(step.get("params"), dict) else {}
                artifact, metrics = run_builtin_limited(
                    task_type, params, blobs, max_cpu, max_ram, max(60, int(args.heartbeat_interval * 20))
                )
                if hb.cancelled.is_set():
                    print(f"Step {step['id']} cancelled by Desk (410); skipping submit.")
                else:
                    duration_sec = max(1, int(round(time.monotonic() - t0)))
                    artifact_bytes = canonical_json_bytes(artifact)
                    meta = {
                        "lease_id": lease_id,
                        "claim": "result",
                        "model": "compute-runner",
                        "harness": "agihome.py",
                        "duration_sec": duration_sec,
                        "cpu_sec": duration_sec,
                        "metrics": metrics,
                    }
                    s_status, s_resp = api_submit_multipart(
                        args.base,
                        "/api/v1/desk/submit",
                        cfg["token"],
                        meta,
                        artifact_bytes,
                        filename=f"{task_type}.json",
                    )
                    print(f"Submitted builtin {task_type} ({s_status}): {s_resp}")

            elif step["kind"] == "verify" and use_builtin and (
                step.get("target_sub") or (step.get("params") or {}).get("submission_id")
            ):
                params = step.get("params") if isinstance(step.get("params"), dict) else {}
                target_sub = step.get("target_sub") or params.get("submission_id")
                target_sha = step.get("target_artifact_sha") or params.get("artifact_sha")
                if not target_sub or not target_sha:
                    _release_lease(
                        args.base, cfg["token"], lease_id, "exhausted", "Missing target submission metadata."
                    )
                else:
                    sub_bytes = api_download_bytes(
                        args.base, f"/api/v1/blob/{target_sha}", token=cfg["token"]
                    )
                    blobs = download_input_blobs(
                        args.base, cfg["token"], execution.get("input_blobs") or []
                    )
                    target_params = (
                        params.get("target_params")
                        if isinstance(params.get("target_params"), dict) and params.get("target_params")
                        else params
                    )
                    eff_task_type = task_type
                    if not eff_task_type:
                        try:
                            eff_task_type = str(json.loads(sub_bytes.decode("utf-8")).get("task_type") or "")
                        except Exception:
                            eff_task_type = ""

                    if eff_task_type not in BUILTIN_TASK_TYPES:
                        _release_lease(
                            args.base,
                            cfg["token"],
                            lease_id,
                            "exhausted",
                            f"Unsupported builtin verify task_type={eff_task_type}",
                        )
                    else:
                        verdict, out_hash, metrics, log_tail = verify_builtin_artifact(
                            eff_task_type,
                            target_params,
                            sub_bytes,
                            blobs,
                            max_cpu=max_cpu,
                            max_ram=max_ram,
                            budget_sec=max(60, int(args.heartbeat_interval * 20)),
                        )
                        if hb.cancelled.is_set():
                            print(f"Verify step {step['id']} cancelled by Desk (410); skipping post.")
                        else:
                            cpu_sec = max(1, int(round(time.monotonic() - t0)))
                            v_status, v_resp = api_request(
                                args.base,
                                "/api/v1/desk/verify",
                                method="POST",
                                token=cfg["token"],
                                payload={
                                    "lease_id": lease_id,
                                    "submission_id": target_sub,
                                    "verdict": verdict,
                                    "output_hash": out_hash,
                                    "image_digest": f"builtin:{eff_task_type}:v1",
                                    "metrics": metrics,
                                    "cpu_sec": cpu_sec,
                                    "log_tail": log_tail,
                                },
                            )
                            print(f"Posted builtin verification ({v_status}): {v_resp}")

            elif mode == "docker" and hw["has_docker"] and step["kind"] in ("crunch", "verify"):
                image = args.image or str(execution.get("runner_image") or "").strip()
                if not image:
                    _release_lease(
                        args.base,
                        cfg["token"],
                        lease_id,
                        "exhausted",
                        "No runner_image specified for docker step.",
                    )
                elif step["kind"] == "crunch":
                    rc, art_bytes, metrics, _out_hash, log_tail = run_docker_job(
                        image=image,
                        mode="crunch",
                        step=step,
                        execution=execution,
                        base=args.base,
                        token=cfg["token"],
                        max_cpu=max_cpu,
                        max_ram=max_ram,
                        hb=hb,
                    )
                    if hb.cancelled.is_set():
                        print(f"Docker crunch {step['id']} cancelled by Desk (410).")
                    elif rc == 0 and art_bytes:
                        duration_sec = max(1, int(round(time.monotonic() - t0)))
                        meta = {
                            "lease_id": lease_id,
                            "claim": "result",
                            "model": "compute-runner",
                            "harness": "agihome.py",
                            "duration_sec": duration_sec,
                            "cpu_sec": duration_sec,
                            "metrics": metrics,
                        }
                        s_status, s_resp = api_submit_multipart(
                            args.base,
                            "/api/v1/desk/submit",
                            cfg["token"],
                            meta,
                            art_bytes,
                            filename="artifact.json",
                        )
                        print(f"Submitted docker crunch ({s_status}): {s_resp}")
                    else:
                        _release_lease(
                            args.base,
                            cfg["token"],
                            lease_id,
                            "exhausted",
                            f"Docker crunch exited rc={rc}: {log_tail[-300:]}",
                        )
                else:
                    params = step.get("params") if isinstance(step.get("params"), dict) else {}
                    target_sub = step.get("target_sub") or params.get("submission_id")
                    target_sha = step.get("target_artifact_sha") or params.get("artifact_sha")
                    if not target_sub or not target_sha:
                        _release_lease(
                            args.base, cfg["token"], lease_id, "exhausted", "Missing target submission metadata."
                        )
                    else:
                        sub_bytes = api_download_bytes(
                            args.base, f"/api/v1/blob/{target_sha}", token=cfg["token"]
                        )
                        rc, _art_bytes, metrics, out_hash, log_tail = run_docker_job(
                            image=image,
                            mode="verify",
                            step=step,
                            execution=execution,
                            base=args.base,
                            token=cfg["token"],
                            max_cpu=max_cpu,
                            max_ram=max_ram,
                            hb=hb,
                            submission_bytes=sub_bytes,
                        )
                        if hb.cancelled.is_set():
                            print(f"Docker verify {step['id']} cancelled by Desk (410).")
                        elif rc in (124, 125, 126, 127):
                            _release_lease(
                                args.base,
                                cfg["token"],
                                lease_id,
                                "exhausted",
                                f"Verifier could not run (rc={rc}), no verdict posted: {log_tail[-300:]}",
                            )
                            print(f"Docker verify {step['id']} could not run (rc={rc}); released without a verdict.")
                        else:
                            verdict = "ACCEPTED" if rc == 0 else "REJECTED"
                            cpu_sec = max(1, int(round(time.monotonic() - t0)))
                            v_status, v_resp = api_request(
                                args.base,
                                "/api/v1/desk/verify",
                                method="POST",
                                token=cfg["token"],
                                payload={
                                    "lease_id": lease_id,
                                    "submission_id": target_sub,
                                    "verdict": verdict,
                                    "output_hash": out_hash,
                                    "image_digest": image,
                                    "metrics": metrics,
                                    "cpu_sec": cpu_sec,
                                    "log_tail": log_tail,
                                },
                            )
                            print(f"Posted docker verification ({v_status}): {v_resp}")
            else:
                _release_lease(
                    args.base,
                    cfg["token"],
                    lease_id,
                    "exhausted",
                    f"Unsupported execution mode={mode} kind={step['kind']} docker={hw['has_docker']}",
                )
        except Exception as exc:
            print(f"Job failed on step {step['id']}: {exc}", file=sys.stderr)
            if not hb.cancelled.is_set():
                _release_lease(args.base, cfg["token"], lease_id, "exhausted", f"Runner error: {exc}"[:480])
        finally:
            hb.stop()

        if args.once:
            return


def cmd_link(args):
    cfg_path = args.config or DEFAULT_CONFIG_PATH
    if not os.path.exists(cfg_path):
        load_or_register(
            args.base,
            handle=args.handle,
            enroll_code=args.code,
            label=args.label,
            hardware=detect_hardware(),
            config_path=cfg_path,
        )
        return
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    status, resp = api_request(
        args.base,
        "/api/v1/agents/link",
        method="POST",
        token=cfg["token"],
        payload={"enroll_code": args.code, "label": args.label or ""},
    )
    if status != 200:
        print(f"Link failed ({status}): {resp}", file=sys.stderr)
        sys.exit(1)
    print(
        f"Linked agent {cfg['handle']} to {resp.get('account_handle')} "
        f"(backfilled {resp.get('backfilled_credits', 0)} credits)"
    )


def _add_common_flags(subparser: argparse.ArgumentParser):
    subparser.add_argument("--base", dest="sub_base", default=None, help="Desk base URL")
    subparser.add_argument("--config", dest="sub_config", default=None, help="Credentials JSON file path")


def main():
    parser = argparse.ArgumentParser(
        prog="agihome.py",
        description="AGI@home volunteer distributed compute & verification runner",
    )
    parser.add_argument("--base", default=None, help="Desk base URL")
    parser.add_argument("--config", default=None, help="Credentials JSON file path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doc = sub.add_parser("doctor", help="Inspect hardware, Docker, and Desk connectivity")
    _add_common_flags(p_doc)
    p_doc.set_defaults(func=cmd_doctor)

    p_loop = sub.add_parser("loop", help="Poll and execute verify/crunch work units")
    _add_common_flags(p_loop)
    p_loop.add_argument("--kinds", default="verify,crunch", help="Comma-separated step kinds")
    p_loop.add_argument("--program", default=None, help="Optional single program slug")
    p_loop.add_argument("--programs", default=None, help="Optional comma-separated program slugs")
    p_loop.add_argument(
        "--max-cpu",
        type=float,
        default=None,
        help="Docker CPU cores; builtin workers limit total CPU time, not CPU rate or cores.",
    )
    p_loop.add_argument(
        "--max-ram",
        default="4g",
        help="Max RAM (e.g. 4g). Docker containers get --memory; builtin tasks run in a worker subprocess capped by RLIMIT_AS.",
    )
    p_loop.add_argument("--until", default=None, help="Stop cleanly at local time HH:MM")
    p_loop.add_argument(
        "--allow-builtin",
        action="store_true",
        help="Allow deterministic stdlib built-in compute/verify tasks (enabled by default unless --docker-only)",
    )
    p_loop.add_argument(
        "--docker-only",
        action="store_true",
        help="Restrict execution strictly to Docker sandbox containers",
    )
    p_loop.add_argument("--handle", default=None, help="Optional agent handle")
    p_loop.add_argument("--enroll", default=None, help="One-time account enroll code from /settings/")
    p_loop.add_argument("--token", default=None, help="Optional agent bearer token override")
    p_loop.add_argument("--label", default=None, help="Optional machine label")
    p_loop.add_argument("--image", default=None, help="Override runner/verifier Docker image")
    p_loop.add_argument("--poll", type=int, default=15, help="Poll interval seconds")
    p_loop.add_argument("--heartbeat-interval", type=float, default=60.0, help="Heartbeat interval seconds")
    p_loop.add_argument("--once", action="store_true", help="Exit after one attempt")
    p_loop.set_defaults(func=cmd_loop)

    p_link = sub.add_parser("link", help="Link this machine's agent token to your AGI@home account")
    _add_common_flags(p_link)
    p_link.add_argument("--code", required=True, help="One-time enroll code from /settings/")
    p_link.add_argument("--label", default="", help="Optional machine label")
    p_link.add_argument("--handle", default=None, help="Optional agent handle if registering fresh")
    p_link.set_defaults(func=cmd_link)

    if len(sys.argv) == 2 and sys.argv[1] == BUILTIN_WORKER_ARG:
        sys.exit(_builtin_worker_main())

    args = parser.parse_args()
    args.base = getattr(args, "sub_base", None) or args.base or DEFAULT_BASE
    args.config = getattr(args, "sub_config", None) or args.config or DEFAULT_CONFIG_PATH
    args.func(args)


if __name__ == "__main__":
    main()
