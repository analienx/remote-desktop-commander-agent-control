#!/usr/bin/env python3
"""Submit one strictly validated RDC process request to Analienx Windows Runner.

This bridge never executes the requested argv itself. It only submits a durable
workspace_exec job to the loopback Runner service and optionally waits for the
durable result. The caller must provide a request JSON under the fixed workspace
request root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

REQUEST_ROOT = Path(r"C:\Workspace\.analienx\rdc-requests")
WORKSPACE_ROOT = Path(r"C:\Workspace")
TOKEN_FILE = Path(r"C:\ProgramData\Analienx\runner-client\client-token.txt")
RUNNER_BASE = "http://127.0.0.1:8765"
SCHEMA = "analienx.rdc-runner-request/v1"
MAX_ARGV = 128
MAX_ARG = 8192
MAX_WAIT = 3600
SHELL_WRAPPERS = {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}
SECRET_RE = re.compile(r"(?i)(?:password|secret|api[_-]?key|authorization|bearer|token)")


class BridgeError(RuntimeError):
    pass


def _contains(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=True)
    parent_resolved = parent.resolve(strict=True)
    try:
        child_resolved.relative_to(parent_resolved)
        return True
    except ValueError:
        return False


def _load_request(path: Path) -> dict:
    if not REQUEST_ROOT.is_dir():
        raise BridgeError(f"request root is missing: {REQUEST_ROOT}")
    if not path.is_file() or not _contains(path, REQUEST_ROOT):
        raise BridgeError("request file must be a regular file below the fixed RDC request root")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"request JSON is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise BridgeError("request JSON must be an object")
    allowed = {
        "schema", "cwd", "argv", "category", "repository", "worktree",
        "label", "timeout_seconds", "wait_seconds",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise BridgeError(f"unknown request fields: {sorted(unknown)}")
    if payload.get("schema") != SCHEMA:
        raise BridgeError("unsupported RDC runner request schema")

    cwd_raw = payload.get("cwd")
    if not isinstance(cwd_raw, str) or not cwd_raw:
        raise BridgeError("cwd is required")
    cwd = Path(cwd_raw)
    if not cwd.is_dir() or not _contains(cwd, WORKSPACE_ROOT):
        raise BridgeError("cwd must be an existing directory below C:\\Workspace")

    argv = payload.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > MAX_ARGV
        or not all(isinstance(value, str) and 0 < len(value) <= MAX_ARG for value in argv)
    ):
        raise BridgeError(f"argv must contain 1..{MAX_ARGV} non-empty bounded strings")
    if any(SECRET_RE.search(value) for value in argv):
        raise BridgeError("argv contains a secret-like token; use an approved typed helper instead")
    executable = Path(argv[0]).name.lower()
    if executable in SHELL_WRAPPERS:
        raise BridgeError("shell wrappers are not accepted by the RDC Runner bridge; submit direct argv")

    category = payload.get("category", "RDC")
    repository = payload.get("repository")
    worktree = payload.get("worktree")
    label = payload.get("label") or f"[RDC][{category}][{repository or cwd.name}]"
    for field, value, limit in (
        ("category", category, 64),
        ("repository", repository, 120),
        ("worktree", worktree, 160),
        ("label", label, 160),
    ):
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= limit):
            raise BridgeError(f"{field} must be a string up to {limit} characters")

    timeout_seconds = payload.get("timeout_seconds", 300)
    wait_seconds = payload.get("wait_seconds", min(timeout_seconds + 30, MAX_WAIT))
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 3600:
        raise BridgeError("timeout_seconds must be 1..3600")
    if not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= MAX_WAIT:
        raise BridgeError(f"wait_seconds must be 0..{MAX_WAIT}")

    return {
        "cwd": str(cwd.resolve(strict=True)),
        "argv": argv,
        "category": category,
        "repository": repository,
        "worktree": worktree,
        "label": label,
        "timeout_seconds": timeout_seconds,
        "wait_seconds": wait_seconds,
    }


def _token() -> str:
    try:
        value = TOKEN_FILE.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise BridgeError(
            "Windows Runner client is not installed; hardened runner activation is required"
        ) from exc
    if len(value) < 32:
        raise BridgeError("Windows Runner client token is invalid")
    return value


def _call(method: str, path: str, body: dict | None = None, timeout: int = 15) -> dict:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        RUNNER_BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + _token(),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BridgeError(f"Runner HTTP {exc.code}: {detail[:600]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BridgeError(f"Windows Runner 127.0.0.1:8765 is unavailable: {exc}") from exc
    if not isinstance(payload, dict):
        raise BridgeError("Runner response is not a JSON object")
    return payload


def submit(request_file: Path) -> dict:
    request = _load_request(request_file)
    request_digest = hashlib.sha256(request_file.read_bytes()).hexdigest()
    runner_request = {
        "schema": 1,
        "job_id": str(uuid.uuid4()),
        "idempotency_key": f"rdc-{request_digest[:48]}",
        "origin": "rdc",
        "operation": "workspace_exec",
        "label": request["label"],
        "repository": request["repository"],
        "worktree": request["worktree"],
        "cwd": request["cwd"],
        "command": request["argv"],
        "timeout_seconds": request["timeout_seconds"],
    }
    submitted = _call("POST", "/v1/submit", runner_request)
    if not submitted.get("ok") or not submitted.get("job_id"):
        raise BridgeError(f"Runner rejected submission: {submitted}")
    job_id = str(submitted["job_id"])
    print(json.dumps({
        "event": "RDC_RUNNER_SUBMITTED",
        "job_id": job_id,
        "category": request["category"],
        "label": request["label"],
    }, ensure_ascii=False), flush=True)

    wait_seconds = request["wait_seconds"]
    if wait_seconds == 0:
        return {"ok": True, "job_id": job_id, "submitted": True, "pending": True}

    deadline = time.monotonic() + wait_seconds
    while True:
        remaining = max(0, int(deadline - time.monotonic()))
        chunk = min(10, remaining)
        result = _call(
            "GET",
            f"/v1/jobs/{urllib.parse.quote(job_id)}/result?wait={chunk}",
            timeout=max(15, chunk + 5),
        )
        if not result.get("pending"):
            return result
        if remaining <= 0:
            return {"ok": True, "job_id": job_id, "pending": True, "wait_expired": True}
        print(json.dumps({
            "event": "RDC_RUNNER_WAIT",
            "job_id": job_id,
            "remaining_seconds": remaining,
        }), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strict RDC -> Analienx Runner bridge")
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = submit(args.request)
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
