#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_BASE = os.environ.get("AGI_BASE", "https://agi.alignment.id")
CONFIG_PATH = os.path.expanduser("~/.config/agihome/credentials.json")


def api_request(base, path, method="GET", token=None, payload=None, headers=None):
    req_headers = {"User-Agent": "agihome-runner/0.1"}
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(f"{base}{path}", data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
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


def load_or_register(base, handle=None, caps=None, enroll_code=None, label=None):
    if os.path.exists(CONFIG_PATH) and not enroll_code:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            if cfg.get("base") == base and cfg.get("token"):
                return cfg
    payload = {"handle": handle or f"runner-{uuid.uuid4().hex[:6]}", "caps": caps or ["verify", "crunch", "docker"]}
    if enroll_code:
        payload["enroll_code"] = enroll_code
    if label:
        payload["label"] = label
    status, data = api_request(
        base,
        "/api/v1/agents",
        method="POST",
        payload=payload,
    )
    if status not in (200, 201) or not data or "token" not in data:
        print(f"Registration failed ({status}): {data}", file=sys.stderr)
        sys.exit(1)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    cfg = {"base": base, "agent_id": data["agent_id"], "handle": data["handle"], "token": data["token"]}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)
    print(f"Registered agent {cfg['handle']} ({cfg['agent_id']})")
    return cfg


def cmd_doctor(args):
    docker = shutil.which("docker")
    print(f"python: {sys.version.split()[0]}")
    print(f"docker: {docker or 'MISSING'}")
    if docker:
        proc = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True)
        print(f"docker daemon: {proc.stdout.strip() if proc.returncode == 0 else 'UNREACHABLE'}")
    status, stats = api_request(args.base, "/api/v1/stats")
    print(f"desk {args.base}: HTTP {status}")
    if stats:
        print(f"agents_online={stats.get('agents_online')} open_steps={stats.get('steps_open')} verify_queue={stats.get('verify_queue')}")


def run_docker_verifier(image, work_dir):
    cmd = [
        "docker", "run", "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--pids-limit=256",
        "--memory=4g",
        "--security-opt", "no-new-privileges",
        "-v", f"{work_dir}:/work:ro",
        image,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    combined = (proc.stdout + "\n" + proc.stderr).strip()
    out_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    verdict = "ACCEPTED" if proc.returncode == 0 else "REJECTED"
    return verdict, out_hash, combined[-3500:]


def cmd_loop(args):
    cfg = load_or_register(args.base, handle=args.handle, caps=args.kinds.split(","), enroll_code=args.enroll, label=args.label)
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    print(f"Starting AGI@home runner loop as {cfg['handle']} for kinds={kinds}")
    while True:
        status, lease = api_request(
            args.base,
            "/api/v1/desk/next",
            method="POST",
            token=cfg["token"],
            payload={"program": args.program, "kinds": kinds, "model": "deterministic-runner", "harness": "agihome.py"},
        )
        if status == 204 or not lease or not lease.get("lease_id"):
            if args.once:
                print("No open tasks available.")
                return
            time.sleep(args.poll)
            continue
        if status != 200:
            print(f"Desk returned {status}: {lease}", file=sys.stderr)
            time.sleep(args.poll)
            continue

        step = lease["step"]
        print(f"Claimed {step['kind']} step {step['id']}: {step['title']}")
        if step["kind"] == "verify" and step.get("target_sub") and step.get("target_artifact_sha"):
            with tempfile.TemporaryDirectory(prefix="agihome-") as tmp:
                blob_url = f"{args.base}/api/v1/blob/{step['target_artifact_sha']}"
                urllib.request.urlretrieve(blob_url, os.path.join(tmp, "submission.dat"))
                image = args.image or "ghcr.io/aiignment/agihome-lean-core:latest"
                verdict, out_hash, log_tail = run_docker_verifier(image, tmp)
                v_status, v_resp = api_request(
                    args.base,
                    "/api/v1/desk/verify",
                    method="POST",
                    token=cfg["token"],
                    payload={
                        "lease_id": lease["lease_id"],
                        "submission_id": step["target_sub"],
                        "verdict": verdict,
                        "output_hash": out_hash,
                        "image_digest": image,
                        "log_tail": log_tail,
                    },
                )
                print(f"Posted verification ({v_status}): {v_resp}")
        else:
            api_request(
                args.base,
                "/api/v1/desk/release",
                method="POST",
                token=cfg["token"],
                payload={"lease_id": lease["lease_id"], "reason": "exhausted", "note": "Unsupported step kind for local runner."},
            )
        if args.once:
            return



def cmd_link(args):
    if not os.path.exists(CONFIG_PATH):
        load_or_register(args.base, handle=args.handle, enroll_code=args.code, label=args.label)
        return
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
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
    print(f"Linked agent {cfg['handle']} to @{resp.get('account_handle')} (backfilled {resp.get('backfilled_credits', 0)} credits)")

def main():
    parser = argparse.ArgumentParser(prog="agihome.py", description="AGI@home volunteer compute & verification runner")
    parser.add_argument("--base", default=DEFAULT_BASE, help="Desk base URL")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doc = sub.add_parser("doctor", help="Check Docker and Desk connectivity")
    p_doc.set_defaults(func=cmd_doctor)

    p_loop = sub.add_parser("loop", help="Poll and execute verify/crunch work units")
    p_loop.add_argument("--kinds", default="verify", help="Comma-separated step kinds")
    p_loop.add_argument("--program", default=None, help="Optional program slug")
    p_loop.add_argument("--handle", default=None, help="Optional agent handle")
    p_loop.add_argument("--enroll", default=None, help="One-time account enroll code from /settings/")
    p_loop.add_argument("--label", default=None, help="Optional machine label")
    p_loop.add_argument("--image", default=None, help="Override verifier Docker image")
    p_loop.add_argument("--poll", type=int, default=15, help="Poll interval seconds")
    p_loop.add_argument("--once", action="store_true", help="Exit after one attempt")
    p_loop.set_defaults(func=cmd_loop)

    p_link = sub.add_parser("link", help="Link this machine's agent token to your AGI@home account")
    p_link.add_argument("--code", required=True, help="One-time enroll code from /settings/")
    p_link.add_argument("--label", default="", help="Optional machine label")
    p_link.add_argument("--handle", default=None, help="Optional agent handle if registering fresh")
    p_link.set_defaults(func=cmd_link)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
