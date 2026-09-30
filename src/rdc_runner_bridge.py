#!/usr/bin/env python3
"""Strict RDC -> thin SLRunner bridge.

The bridge is intentionally boring: validate that the request lives under the fixed
workspace request root, translate it to the SLRunner schema, and execute the installed
SLRunner as a child with inherited stdin/stdout/stderr. There is no HTTP service,
scheduler, token, or second execution policy here.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

REQUEST_ROOT = Path(r"C:\Workspace\.analienx\rdc-requests")
WORKSPACE_ROOT = Path(r"C:\Workspace")
SLRUNNER_ENTRY = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Analienx" / "SLRunner" / "slrunner.py"
SCHEMA = "analienx.rdc-slrunner-request/v1"
MAX_ARGV = 256
MAX_ARG = 32768
MAX_COMMAND = 131072


class BridgeError(RuntimeError):
    pass


def _contains(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=True).relative_to(parent.resolve(strict=True))
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
        "schema", "cwd", "command", "argv", "shell", "project", "stream", "category",
        "initiative_id", "activity_type", "repository", "worktree", "label",
        "timeout_seconds", "heartbeat_seconds",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise BridgeError(f"unknown request fields: {sorted(unknown)}")
    if payload.get("schema") != SCHEMA:
        raise BridgeError("unsupported RDC SLRunner request schema")

    cwd_raw = payload.get("cwd")
    if not isinstance(cwd_raw, str) or not cwd_raw:
        raise BridgeError("cwd is required")
    cwd = Path(cwd_raw)
    if not cwd.is_dir() or not _contains(cwd, WORKSPACE_ROOT):
        raise BridgeError("cwd must be an existing directory below C:\\Workspace")

    command = payload.get("command")
    argv = payload.get("argv")
    if bool(command) == bool(argv):
        raise BridgeError("provide exactly one of command or argv")
    if command is not None and (
        not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND
    ):
        raise BridgeError(f"command must be a non-empty string up to {MAX_COMMAND} characters")
    if argv is not None and (
        not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV
        or not all(isinstance(value, str) and 0 < len(value) <= MAX_ARG for value in argv)
    ):
        raise BridgeError(f"argv must contain 1..{MAX_ARGV} bounded non-empty strings")

    def bounded(name: str, default, limit: int):
        value = payload.get(name, default)
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= limit):
            raise BridgeError(f"{name} must be a string up to {limit} characters")
        return value

    timeout = payload.get("timeout_seconds", 3600)
    heartbeat = payload.get("heartbeat_seconds", 15)
    if not isinstance(timeout, int) or not 1 <= timeout <= 86400:
        raise BridgeError("timeout_seconds must be 1..86400")
    if not isinstance(heartbeat, int) or not 5 <= heartbeat <= 300:
        raise BridgeError("heartbeat_seconds must be 5..300")

    initiative_id = bounded("initiative_id", None, 120)
    if not initiative_id:
        raise BridgeError("initiative_id is required for RDC execution")
    activity_type = bounded("activity_type", "rdc-command", 120)
    project = bounded("project", None, 120)
    category = bounded("category", "RDC", 64)
    repository = bounded("repository", None, 160)
    worktree = bounded("worktree", str(cwd.resolve(strict=True)), 512)
    stream = bounded("stream", None, 160)
    label = bounded("label", f"[RDC][{category}][{project or repository or cwd.name}]", 200)
    shell = bounded("shell", "cmd.exe", 260)

    return {
        "schema": 1,
        "job_id": str(uuid.uuid4()),
        "cwd": str(cwd.resolve(strict=True)),
        "command": command,
        "argv": argv,
        "shell": shell,
        "initiative_id": initiative_id,
        "activity_type": activity_type,
        "project": project,
        "stream": stream,
        "category": category,
        "origin": "rdc",
        "repository": repository,
        "worktree": worktree,
        "label": label,
        "timeout_seconds": timeout,
        "heartbeat_seconds": heartbeat,
    }


def execute(request_file: Path) -> int:
    request = _load_request(request_file)
    if not SLRUNNER_ENTRY.is_file():
        raise BridgeError(f"thin SLRunner is not installed: {SLRUNNER_ENTRY}")
    REQUEST_ROOT.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{request['job_id']}.", suffix=".slrunner.json", dir=str(REQUEST_ROOT))
    translated = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(request, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        print(json.dumps({
            "event": "RDC_SLRUNNER_START",
            "job_id": request["job_id"],
            "initiative_id": request.get("initiative_id"),
            "project": request.get("project"),
            "activity_type": request.get("activity_type"),
            "category": request.get("category"),
        }, ensure_ascii=False), flush=True)
        # No stdio redirection: RDC interaction is transparently forwarded through
        # bridge -> SLRunner -> project child.
        return subprocess.call([
            sys.executable, str(SLRUNNER_ENTRY), "--request", str(translated)
        ])
    finally:
        translated.unlink(missing_ok=True)
        request_file.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strict RDC -> thin SLRunner bridge")
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return execute(args.request)
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
