#!/usr/bin/env python3
"""Install/verify the mandatory Desktop Commander -> Analienx Runner hook.

The patch is intentionally tiny and signature-anchored. It inserts one policy
import and one enforcement call into Desktop Commander's start_process path.
If the installed package no longer matches the expected anchors, installation
fails closed rather than guessing across an upstream upgrade.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

CONTROL_ROOT = Path(os.environ.get("LOCALAPPDATA", "")) / "RDC-Control"
DC_ROOT = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@wonderwhy-er" / "desktop-commander"
TARGET = DC_ROOT / "dist" / "tools" / "improved-process-tools.js"
POLICY_TARGET = DC_ROOT / "dist" / "tools" / "analienx-runner-policy.js"
POLICY_SOURCE = Path(__file__).resolve().with_name("analienx-runner-policy.js")
STATE_FILE = CONTROL_ROOT / "rdc-runner-hook-state.json"
BACKUP_ROOT = CONTROL_ROOT / "backups"
REQUEST_ROOT = Path(r"C:\Workspace\.analienx\rdc-requests")
RUNNER_TOKEN = Path(r"C:\ProgramData\Analienx\runner-client\client-token.txt")

IMPORT_LINE = "import { enforceAnalienxRunnerRouting } from './analienx-runner-policy.js';"
IMPORT_ANCHOR = "import { fileURLToPath } from 'url';"
CALL_MARKER = "enforceAnalienxRunnerRouting(parsed.data);"
CALL_ANCHOR = "    try {\n        const commands = commandManager.extractCommands(parsed.data.command).join(', ');"
CALL_BLOCK = """    try {
        enforceAnalienxRunnerRouting(parsed.data);
    }
    catch (error) {
        capture('server_start_process_runner_required');
        return {
            content: [{
                    type: "text",
                    text: "Error: " + (error instanceof Error ? error.message : String(error))
                }],
            isError: true,
        };
    }
"""


class HookError(RuntimeError):
    pass


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HookError(f"cannot read hook state: {exc}") from exc
    if not isinstance(value, dict):
        raise HookError("hook state must be a JSON object")
    return value


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _package_version() -> str | None:
    package = DC_ROOT / "package.json"
    if not package.is_file():
        return None
    try:
        value = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = value.get("version") if isinstance(value, dict) else None
    return version if isinstance(version, str) else None


def _patched_source(original: str) -> str:
    has_import = IMPORT_LINE in original
    has_call = CALL_MARKER in original
    if has_import != has_call:
        raise HookError("partial Runner hook detected; refusing to patch ambiguous Desktop Commander source")
    if has_import and has_call:
        return original
    if original.count(IMPORT_ANCHOR) != 1:
        raise HookError("Desktop Commander import anchor changed; review upstream before installing hook")
    if original.count(CALL_ANCHOR) != 1:
        raise HookError("Desktop Commander start_process anchor changed; review upstream before installing hook")
    updated = original.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + "\n" + IMPORT_LINE, 1)
    updated = updated.replace(CALL_ANCHOR, CALL_BLOCK + CALL_ANCHOR, 1)
    return updated


def status() -> dict[str, Any]:
    target_exists = TARGET.is_file()
    policy_exists = POLICY_TARGET.is_file()
    source_exists = POLICY_SOURCE.is_file()
    target_text = TARGET.read_text(encoding="utf-8") if target_exists else ""
    import_present = IMPORT_LINE in target_text
    call_present = CALL_MARKER in target_text
    policy_matches = False
    if policy_exists and source_exists:
        policy_matches = POLICY_TARGET.read_bytes() == POLICY_SOURCE.read_bytes()
    runner_ready = RUNNER_TOKEN.is_file()
    healthy = (
        target_exists
        and policy_exists
        and import_present
        and call_present
        and policy_matches
        and runner_ready
    )
    return {
        "schema": "analienx.rdc-runner-hook/v1",
        "healthy": healthy,
        "runner_ready": runner_ready,
        "desktop_commander_version": _package_version(),
        "target": str(TARGET),
        "target_exists": target_exists,
        "policy_exists": policy_exists,
        "import_present": import_present,
        "call_present": call_present,
        "policy_matches": policy_matches,
        "request_root": str(REQUEST_ROOT),
    }


def ensure() -> dict[str, Any]:
    if not RUNNER_TOKEN.is_file():
        raise HookError(
            "hardened Windows Runner client is not installed; refusing to activate mandatory RDC process routing"
        )
    if not TARGET.is_file():
        raise HookError(f"Desktop Commander start_process implementation is missing: {TARGET}")
    if not POLICY_SOURCE.is_file():
        raise HookError(f"Runner policy source is missing: {POLICY_SOURCE}")

    CONTROL_ROOT.mkdir(parents=True, exist_ok=True)
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    REQUEST_ROOT.mkdir(parents=True, exist_ok=True)

    original_bytes = TARGET.read_bytes()
    original_text = original_bytes.decode("utf-8")
    original_sha = _sha_bytes(original_bytes)
    patched_text = _patched_source(original_text)
    changed = patched_text != original_text

    backup: Path | None = None
    if changed:
        backup = BACKUP_ROOT / f"improved-process-tools.{original_sha}.js"
        if not backup.exists():
            _write_atomic(backup, original_bytes)
        _write_atomic(TARGET, patched_text.encode("utf-8"))
    else:
        state = _read_json(STATE_FILE) if STATE_FILE.is_file() else {}
        backup_raw = state.get("backup_path")
        backup = Path(backup_raw) if isinstance(backup_raw, str) and backup_raw else None

    _write_atomic(POLICY_TARGET, POLICY_SOURCE.read_bytes())
    patched_bytes = TARGET.read_bytes()
    patched_sha = _sha_bytes(patched_bytes)
    patched_text_check = patched_bytes.decode("utf-8")
    if IMPORT_LINE not in patched_text_check or CALL_MARKER not in patched_text_check:
        raise HookError("Runner hook verification failed after patch")
    if POLICY_TARGET.read_bytes() != POLICY_SOURCE.read_bytes():
        raise HookError("Runner policy module verification failed after copy")

    prior = _read_json(STATE_FILE) if STATE_FILE.is_file() else {}
    backup_path = str(backup) if backup else prior.get("backup_path")
    state = {
        "schema": "analienx.rdc-runner-hook-state/v1",
        "desktop_commander_version": _package_version(),
        "target": str(TARGET),
        "policy_target": str(POLICY_TARGET),
        "original_sha256": original_sha if changed else prior.get("original_sha256"),
        "patched_sha256": patched_sha,
        "backup_path": backup_path,
        "changed": changed,
    }
    _write_atomic(STATE_FILE, (json.dumps(state, indent=2) + "\n").encode("utf-8"))
    result = status()
    result["changed"] = changed
    result["patched_sha256"] = patched_sha
    if not result["healthy"]:
        raise HookError(f"hook is not healthy after ensure: {result}")
    return result


def uninstall() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return {"ok": True, "changed": False, "reason": "no hook state"}
    state = _read_json(STATE_FILE)
    expected = state.get("patched_sha256")
    backup_raw = state.get("backup_path")
    if not isinstance(expected, str) or not isinstance(backup_raw, str):
        raise HookError("hook state is missing patched hash or backup path")
    backup = Path(backup_raw)
    if not TARGET.is_file() or _sha_bytes(TARGET.read_bytes()) != expected:
        raise HookError("Desktop Commander source changed since hook install; refusing destructive restore")
    if not backup.is_file():
        raise HookError(f"hook backup is missing: {backup}")
    _write_atomic(TARGET, backup.read_bytes())
    try:
        POLICY_TARGET.unlink(missing_ok=True)
    except OSError as exc:
        raise HookError(f"could not remove policy module: {exc}") from exc
    STATE_FILE.unlink(missing_ok=True)
    return {"ok": True, "changed": True, "restored_sha256": _sha_bytes(TARGET.read_bytes())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install/verify mandatory RDC Runner hook")
    parser.add_argument("action", choices=("status", "ensure", "uninstall"))
    args = parser.parse_args(argv)
    try:
        if args.action == "status":
            result = status()
            code = 0 if result["healthy"] else 3
        elif args.action == "ensure":
            result = ensure()
            code = 0
        else:
            result = uninstall()
            code = 0
    except HookError as exc:
        result = {"ok": False, "error": str(exc)}
        code = 2
    print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
