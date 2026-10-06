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
import importlib.util
import json
import os
import re
from pathlib import Path
import tempfile
from typing import Any

CONTROL_ROOT = Path(os.environ.get("LOCALAPPDATA", "")) / "RDC-Control"
DC_ROOT = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@wonderwhy-er" / "desktop-commander"
TARGET = DC_ROOT / "dist" / "tools" / "improved-process-tools.js"
SCHEMA_TARGET = DC_ROOT / "dist" / "tools" / "schemas.js"
POLICY_TARGET = DC_ROOT / "dist" / "tools" / "analienx-runner-policy.js"
POLICY_SOURCE = Path(__file__).resolve().with_name("analienx-runner-policy.js")
STATE_FILE = CONTROL_ROOT / "rdc-runner-hook-state.json"
BACKUP_ROOT = CONTROL_ROOT / "backups"
REQUEST_ROOT = Path(r"C:\Workspace\.analienx\rdc-requests")
SLRUNNER_ENTRY = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Analienx" / "SLRunner" / "slrunner.py"


def _load_write_audit_hook():
    path = Path(__file__).resolve().with_name("rdc_write_audit_hook.py")
    if not path.is_file():
        raise RuntimeError(f"native-write audit hook module is missing: {path}")
    spec = importlib.util.spec_from_file_location("analienx_rdc_write_audit_hook", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load native-write audit hook: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WRITE_AUDIT_HOOK = _load_write_audit_hook()

IMPORT_LINE = "import { routeAnalienxRunner } from './analienx-runner-policy.js';"
CONTROL_AUDIT_IMPORT = "import { beginNativeControl, finishNativeControl, failNativeControl } from './analienx-write-audit.js';"
IMPORT_ANCHOR = "import { fileURLToPath } from 'url';"
INTERACT_AUDIT_MARKER = "// ANALienx process interaction audit"
FORCE_AUDIT_MARKER = "// ANALienx process termination audit"
CALL_MARKER = "const routed = await routeAnalienxRunner(parsed.data, runnerOriginalShell);"
CALL_SIGNATURE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)const isAllowed = await commandManager\.validateCommand"
    r"\(parsed\.data\.command\);"
)
SCHEMA_START = "export const StartProcessArgsSchema = z.object({"
SCHEMA_ANCHOR = "    verbose_timing: z.boolean().optional(),"
SCHEMA_MARKER = "    // ANALienx typed Runner capability options"
SCHEMA_OPTIONS_BLOCK = """    // ANALienx typed Runner capability options
    options: z.object({
        capability: z.object({
            schema: z.literal(1),
            action: z.enum([
                'read_many', 'list', 'search', 'repo_status',
                'snapshot', 'delta', 'system', 'processes',
            ]),
            paths: z.array(z.string()).optional(),
            roots: z.array(z.string()).optional(),
            query: z.string().optional(),
            max_results: z.number().int().optional(),
            max_bytes: z.number().int().optional(),
            max_files: z.number().int().optional(),
            max_depth: z.number().int().optional(),
            snapshot_id: z.string().optional(),
            excludes: z.array(z.string()).optional(),
            include_content: z.boolean().optional(),
        }).strict().optional(),
        execution: z.object({
            schema: z.literal(1),
            identity: z.object({
                project_id: z.string().min(1).max(160),
                worktree: z.string().min(1).max(512),
                repository: z.string().min(1).max(160).optional(),
                stream_id: z.string().min(1).max(160).optional(),
            }).strict(),
            operation: z.object({
                name: z.string().min(1).max(120),
                parameters: z.record(z.unknown()).optional(),
            }).strict().optional(),
            authorization: z.object({
                schema: z.literal(1),
                kind: z.enum(['user', 'supervisor', 'delegation']),
                authorization_id: z.string().min(1).max(128),
                expires_at: z.string().min(1).max(64),
                max_attempts: z.number().int().min(1).max(100),
                target: z.string().max(512).optional(),
                artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/).optional(),
                helper_sha256: z.string().regex(/^[0-9a-f]{64}$/).optional(),
            }).strict().optional(),
        }).strict().optional(),
    }).strict().optional(),
"""
CALL_BLOCK = """    try {
        let runnerOriginalShell = parsed.data.shell;
        if (!runnerOriginalShell) {
            const runnerConfig = await configManager.getConfig();
            if (runnerConfig.defaultShell) {
                runnerOriginalShell = runnerConfig.defaultShell;
            }
            else {
                const runnerIsWindows = os.platform() === 'win32';
                runnerOriginalShell = runnerIsWindows
                    ? (process.env.COMSPEC || 'cmd.exe')
                    : (process.env.SHELL || '/bin/sh');
            }
        }
        const routed = await routeAnalienxRunner(parsed.data, runnerOriginalShell);
        parsed.data.command = routed.command;
        parsed.data.shell = routed.shell;
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


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise HookError(f"{label} signature changed or ambiguous (matches={count})")
    return source.replace(old, new, 1)


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


def _write_atomic(path: Path, data: bytes, *, allow_locked_target_fallback: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temp_name, path)
        except PermissionError:
            if (
                not allow_locked_target_fallback
                or path not in {TARGET, SCHEMA_TARGET}
                or not path.is_file()
            ):
                raise
            # A running Windows Node process may keep the loaded module open without
            # FILE_SHARE_DELETE, which blocks rename/replace while still permitting
            # ordinary writes. The caller has already created a content-addressed
            # backup before enabling this narrow fallback.
            with path.open("r+b") as handle:
                handle.seek(0)
                handle.write(data)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            if path.read_bytes() != data:
                raise HookError("locked-target in-place write verification failed")
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
    matches = list(CALL_SIGNATURE.finditer(original))
    if len(matches) != 1:
        raise HookError(
            "Desktop Commander start_process signature changed or became ambiguous; review upstream before installing hook"
        )
    updated = original.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + "\n" + IMPORT_LINE, 1)
    # Import insertion changes source offsets. Re-resolve the unique call anchor
    # against the updated source before inserting the routing block.
    updated_matches = list(CALL_SIGNATURE.finditer(updated))
    if len(updated_matches) != 1:
        raise HookError(
            "Desktop Commander start_process signature changed after import insertion"
        )
    match = updated_matches[0]
    updated = updated[:match.start()] + CALL_BLOCK + updated[match.start():]
    return updated


def _patched_control_audit(original: str) -> str:
    markers = (
        INTERACT_AUDIT_MARKER in original,
        FORCE_AUDIT_MARKER in original,
        CONTROL_AUDIT_IMPORT in original,
    )
    if any(markers) and not all(markers):
        raise HookError("partial process-control audit hook detected")
    if all(markers):
        return original
    if IMPORT_LINE not in original:
        raise HookError("Runner import must be installed before process-control audit")
    source = original.replace(
        IMPORT_LINE, IMPORT_LINE + "\n" + CONTROL_AUDIT_IMPORT, 1
    )
    source = _replace_once(
        source,
        "    const { pid, input, timeout_ms = 8000, wait_for_prompt = true, verbose_timing = false } = parsed.data;",
        "    const { pid, input, timeout_ms = 8000, wait_for_prompt = true, verbose_timing = false } = parsed.data;\n"
        "    " + INTERACT_AUDIT_MARKER + "\n"
        "    const nativeAudit = beginNativeControl('process_input', { pid, input });",
        "interact audit begin",
    )
    source = _replace_once(
        source,
        "        return executeNodeCode(input, effectiveTimeout);",
        "        try {\n"
        "            const result = await executeNodeCode(input, effectiveTimeout);\n"
        "            finishNativeControl(nativeAudit);\n"
        "            return result;\n"
        "        }\n"
        "        catch (error) {\n"
        "            failNativeControl(nativeAudit, error);\n"
        "            throw error;\n"
        "        }",
        "virtual node interaction audit",
    )
    source = _replace_once(
        source,
        "        if (!success) {\n            return {",
        "        if (!success) {\n"
        "            failNativeControl(nativeAudit, new Error('process rejected input'));\n"
        "            return {",
        "process input rejection audit",
    )
    source = _replace_once(
        source,
        "        if (!wait_for_prompt) {\n            exitReason = 'no_wait';",
        "        if (!wait_for_prompt) {\n"
        "            exitReason = 'no_wait';\n"
        "            finishNativeControl(nativeAudit);",
        "no-wait interaction audit",
    )
    source = _replace_once(
        source,
        "        await waitForResponse();\n        // Clean and format output",
        "        await waitForResponse();\n"
        "        finishNativeControl(nativeAudit);\n"
        "        // Clean and format output",
        "interaction completion audit",
    )
    source = _replace_once(
        source,
        "    catch (error) {\n        const errorMessage = error instanceof Error ? error.message : String(error);\n        capture('server_interact_with_process_error', {",
        "    catch (error) {\n"
        "        failNativeControl(nativeAudit, error);\n"
        "        const errorMessage = error instanceof Error ? error.message : String(error);\n"
        "        capture('server_interact_with_process_error', {",
        "interaction error audit",
    )
    source = _replace_once(
        source,
        "    const pid = parsed.data.pid;\n    // Handle virtual Node.js sessions (node:local)",
        "    const pid = parsed.data.pid;\n"
        "    " + FORCE_AUDIT_MARKER + "\n"
        "    const nativeAudit = beginNativeControl('process_terminate', {\n"
        "        pid, termination_kind: 'force_terminate'\n"
        "    });\n"
        "    // Handle virtual Node.js sessions (node:local)",
        "force terminate audit begin",
    )
    source = _replace_once(
        source,
        "    if (virtualNodeSessions.has(pid)) {\n        virtualNodeSessions.delete(pid);\n        return {",
        "    if (virtualNodeSessions.has(pid)) {\n"
        "        virtualNodeSessions.delete(pid);\n"
        "        finishNativeControl(nativeAudit);\n"
        "        return {",
        "virtual terminate audit",
    )
    source = _replace_once(
        source,
        "    const success = terminalManager.forceTerminate(pid);\n    return {",
        "    let success = false;\n"
        "    try {\n"
        "        success = terminalManager.forceTerminate(pid);\n"
        "        if (success) finishNativeControl(nativeAudit);\n"
        "        else failNativeControl(nativeAudit, new Error('no active session'));\n"
        "    }\n"
        "    catch (error) {\n"
        "        failNativeControl(nativeAudit, error);\n"
        "        throw error;\n"
        "    }\n"
        "    return {",
        "force terminate completion audit",
    )
    return source


def _patched_schema(original: str) -> str:
    if SCHEMA_MARKER in original:
        return original
    start = original.find(SCHEMA_START)
    if start < 0:
        raise HookError(
            "Desktop Commander StartProcessArgsSchema is missing; review upstream"
        )
    end = original.find("\n});", start)
    if end < 0:
        raise HookError(
            "Desktop Commander StartProcessArgsSchema terminator changed; review upstream"
        )
    block = original[start:end]
    if block.count(SCHEMA_ANCHOR) != 1:
        raise HookError(
            "Desktop Commander start_process schema changed; review upstream before installing options"
        )
    patched_block = block.replace(
        SCHEMA_ANCHOR,
        SCHEMA_ANCHOR + "\n" + SCHEMA_OPTIONS_BLOCK.rstrip("\n"),
        1,
    )
    return original[:start] + patched_block + original[end:]


def status() -> dict[str, Any]:
    try:
        write_audit_status = WRITE_AUDIT_HOOK.status()
    except Exception as exc:
        write_audit_status = {"healthy": False, "error": str(exc)}
    target_exists = TARGET.is_file()
    schema_exists = SCHEMA_TARGET.is_file()
    policy_exists = POLICY_TARGET.is_file()
    source_exists = POLICY_SOURCE.is_file()
    target_text = TARGET.read_text(encoding="utf-8") if target_exists else ""
    schema_text = SCHEMA_TARGET.read_text(encoding="utf-8") if schema_exists else ""
    import_present = IMPORT_LINE in target_text
    call_present = CALL_MARKER in target_text
    control_audit_present = (
        CONTROL_AUDIT_IMPORT in target_text
        and INTERACT_AUDIT_MARKER in target_text
        and FORCE_AUDIT_MARKER in target_text
    )
    options_present = SCHEMA_MARKER in schema_text
    policy_matches = False
    if policy_exists and source_exists:
        policy_matches = POLICY_TARGET.read_bytes() == POLICY_SOURCE.read_bytes()
    slrunner_ready = SLRUNNER_ENTRY.is_file()
    healthy = (
        target_exists
        and schema_exists
        and policy_exists
        and import_present
        and call_present
        and control_audit_present
        and options_present
        and policy_matches
        and slrunner_ready
        and write_audit_status.get("healthy") is True
    )
    return {
        "schema": "analienx.rdc-runner-hook/v1",
        "healthy": healthy,
        "slrunner_ready": slrunner_ready,
        "slrunner_entry": str(SLRUNNER_ENTRY),
        "desktop_commander_version": _package_version(),
        "target": str(TARGET),
        "target_exists": target_exists,
        "schema_target": str(SCHEMA_TARGET),
        "schema_exists": schema_exists,
        "options_present": options_present,
        "policy_exists": policy_exists,
        "import_present": import_present,
        "call_present": call_present,
        "control_audit_present": control_audit_present,
        "policy_matches": policy_matches,
        "request_root": str(REQUEST_ROOT),
        "native_write_audit": write_audit_status,
    }

def ensure() -> dict[str, Any]:
    if not SLRUNNER_ENTRY.is_file():
        raise HookError(
            "thin SLRunner is not installed; refusing to activate mandatory RDC process routing"
        )
    if not TARGET.is_file():
        raise HookError(f"Desktop Commander start_process implementation is missing: {TARGET}")
    if not SCHEMA_TARGET.is_file():
        raise HookError(f"Desktop Commander tool schema is missing: {SCHEMA_TARGET}")
    if not POLICY_SOURCE.is_file():
        raise HookError(f"Runner policy source is missing: {POLICY_SOURCE}")
    try:
        WRITE_AUDIT_HOOK.preflight()
    except Exception as exc:
        raise HookError(f"native-write audit preflight failed: {exc}") from exc

    CONTROL_ROOT.mkdir(parents=True, exist_ok=True)
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    REQUEST_ROOT.mkdir(parents=True, exist_ok=True)
    prior = _read_json(STATE_FILE) if STATE_FILE.is_file() else {}

    original_bytes = TARGET.read_bytes()
    original_text = original_bytes.decode("utf-8")
    original_sha = _sha_bytes(original_bytes)
    prior_patched_sha = prior.get("patched_sha256")
    if isinstance(prior_patched_sha, str) and prior_patched_sha != original_sha:
        raise HookError(
            "Desktop Commander process source drifted since hook install; "
            "refusing to patch unknown state"
        )
    patched_text = _patched_control_audit(_patched_source(original_text))
    source_changed = patched_text != original_text

    schema_original_bytes = SCHEMA_TARGET.read_bytes()
    schema_original_text = schema_original_bytes.decode("utf-8")
    schema_original_sha = _sha_bytes(schema_original_bytes)
    prior_schema_patched_sha = prior.get("schema_patched_sha256")
    if (
        isinstance(prior_schema_patched_sha, str)
        and prior_schema_patched_sha != schema_original_sha
    ):
        raise HookError(
            "Desktop Commander schema drifted since hook install; "
            "refusing to patch unknown state"
        )
    schema_patched_text = _patched_schema(schema_original_text)
    schema_changed = schema_patched_text != schema_original_text

    policy_bytes = POLICY_SOURCE.read_bytes()
    policy_changed = (
        not POLICY_TARGET.is_file() or POLICY_TARGET.read_bytes() != policy_bytes
    )
    changed = source_changed or schema_changed or policy_changed

    prior_backup_raw = prior.get("backup_path")
    prior_backup = (
        Path(prior_backup_raw)
        if isinstance(prior_backup_raw, str) and prior_backup_raw
        else None
    )
    prior_patched_sha = prior.get("patched_sha256")
    backup: Path | None = prior_backup
    source_original_sha = prior.get("original_sha256")
    if source_changed:
        if isinstance(prior_patched_sha, str):
            if prior_patched_sha != original_sha:
                raise HookError(
                    "Desktop Commander process source drifted since hook install; "
                    "refusing to redefine restore origin"
                )
            if prior_backup is None or not prior_backup.is_file():
                raise HookError("original process-hook backup is missing")
        else:
            backup = BACKUP_ROOT / f"improved-process-tools.{original_sha}.js"
            if not backup.exists():
                _write_atomic(backup, original_bytes)
            source_original_sha = original_sha
        _write_atomic(
            TARGET, patched_text.encode("utf-8"),
            allow_locked_target_fallback=True,
        )

    prior_schema_backup_raw = prior.get("schema_backup_path")
    prior_schema_backup = (
        Path(prior_schema_backup_raw)
        if isinstance(prior_schema_backup_raw, str) and prior_schema_backup_raw
        else None
    )
    prior_schema_patched_sha = prior.get("schema_patched_sha256")
    schema_backup: Path | None = prior_schema_backup
    schema_origin_sha = prior.get("schema_original_sha256")
    if schema_changed:
        if isinstance(prior_schema_patched_sha, str):
            if prior_schema_patched_sha != schema_original_sha:
                raise HookError(
                    "Desktop Commander schema drifted since hook install; "
                    "refusing to redefine restore origin"
                )
            if prior_schema_backup is None or not prior_schema_backup.is_file():
                raise HookError("original schema-hook backup is missing")
        else:
            schema_backup = BACKUP_ROOT / f"schemas.{schema_original_sha}.js"
            if not schema_backup.exists():
                _write_atomic(schema_backup, schema_original_bytes)
            schema_origin_sha = schema_original_sha
        _write_atomic(
            SCHEMA_TARGET, schema_patched_text.encode("utf-8"),
            allow_locked_target_fallback=True,
        )

    _write_atomic(POLICY_TARGET, policy_bytes)

    patched_bytes = TARGET.read_bytes()
    patched_sha = _sha_bytes(patched_bytes)
    patched_text_check = patched_bytes.decode("utf-8")
    schema_patched_bytes = SCHEMA_TARGET.read_bytes()
    schema_patched_sha = _sha_bytes(schema_patched_bytes)
    schema_text_check = schema_patched_bytes.decode("utf-8")
    if IMPORT_LINE not in patched_text_check or CALL_MARKER not in patched_text_check:
        raise HookError("Runner hook verification failed after patch")
    if not (
        CONTROL_AUDIT_IMPORT in patched_text_check
        and INTERACT_AUDIT_MARKER in patched_text_check
        and FORCE_AUDIT_MARKER in patched_text_check
    ):
        raise HookError("process-control audit verification failed after patch")
    if SCHEMA_MARKER not in schema_text_check:
        raise HookError("Runner options schema verification failed after patch")
    if POLICY_TARGET.read_bytes() != POLICY_SOURCE.read_bytes():
        raise HookError("Runner policy module verification failed after copy")
    try:
        write_audit_result = WRITE_AUDIT_HOOK.ensure()
    except Exception as exc:
        raise HookError(f"native-write audit install failed: {exc}") from exc
    changed = changed or bool(write_audit_result.get("changed"))

    backup_path = str(backup) if backup else prior.get("backup_path")
    schema_backup_path = (
        str(schema_backup) if schema_backup else prior.get("schema_backup_path")
    )
    state = {
        "schema": "analienx.rdc-runner-hook-state/v1",
        "desktop_commander_version": _package_version(),
        "target": str(TARGET),
        "schema_target": str(SCHEMA_TARGET),
        "policy_target": str(POLICY_TARGET),
        "original_sha256": source_original_sha,
        "patched_sha256": patched_sha,
        "backup_path": backup_path,
        "schema_original_sha256": schema_origin_sha,
        "schema_patched_sha256": schema_patched_sha,
        "schema_backup_path": schema_backup_path,
        "changed": changed,
    }
    _write_atomic(STATE_FILE, (json.dumps(state, indent=2) + "\n").encode("utf-8"))
    result = status()
    result["changed"] = changed
    result["patched_sha256"] = patched_sha
    result["schema_patched_sha256"] = schema_patched_sha
    if not result["healthy"]:
        raise HookError(f"hook is not healthy after ensure: {result}")
    return result


def uninstall() -> dict[str, Any]:
    try:
        write_audit_result = WRITE_AUDIT_HOOK.uninstall()
    except Exception as exc:
        raise HookError(f"native-write audit uninstall failed: {exc}") from exc
    if not STATE_FILE.is_file():
        return {
            "ok": True,
            "changed": bool(write_audit_result.get("changed")),
            "reason": "no process hook state",
        }
    state = _read_json(STATE_FILE)
    expected = state.get("patched_sha256")
    backup_raw = state.get("backup_path")
    schema_expected = state.get("schema_patched_sha256")
    schema_backup_raw = state.get("schema_backup_path")
    if not all(isinstance(value, str) and value for value in (
        expected, backup_raw, schema_expected, schema_backup_raw
    )):
        raise HookError("hook state is missing process/schema restore metadata")
    backup = Path(backup_raw)
    schema_backup = Path(schema_backup_raw)
    if not TARGET.is_file() or _sha_bytes(TARGET.read_bytes()) != expected:
        raise HookError(
            "Desktop Commander process source changed since hook install; "
            "refusing destructive restore"
        )
    if (
        not SCHEMA_TARGET.is_file()
        or _sha_bytes(SCHEMA_TARGET.read_bytes()) != schema_expected
    ):
        raise HookError(
            "Desktop Commander schema changed since hook install; "
            "refusing destructive restore"
        )
    if not backup.is_file() or not schema_backup.is_file():
        raise HookError("hook restore backup is missing")
    _write_atomic(TARGET, backup.read_bytes(), allow_locked_target_fallback=True)
    _write_atomic(
        SCHEMA_TARGET, schema_backup.read_bytes(),
        allow_locked_target_fallback=True,
    )
    try:
        POLICY_TARGET.unlink(missing_ok=True)
    except OSError as exc:
        raise HookError(f"could not remove policy module: {exc}") from exc
    STATE_FILE.unlink(missing_ok=True)
    return {
        "ok": True,
        "changed": True,
        "restored_sha256": _sha_bytes(TARGET.read_bytes()),
        "schema_restored_sha256": _sha_bytes(SCHEMA_TARGET.read_bytes()),
    }


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
