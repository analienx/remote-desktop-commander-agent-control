#!/usr/bin/env python3
"""Signature-anchored Desktop Commander native-write audit hook."""
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
FS_TARGET = DC_ROOT / "dist" / "handlers" / "filesystem-handlers.js"
EDIT_TARGET = DC_ROOT / "dist" / "tools" / "edit.js"
HELPER_TARGET = DC_ROOT / "dist" / "tools" / "analienx-write-audit.js"
HELPER_SOURCE = Path(__file__).resolve().with_name("analienx-write-audit.js")
STATE_FILE = CONTROL_ROOT / "rdc-write-audit-hook-state.json"
BACKUP_ROOT = CONTROL_ROOT / "backups"
FS_IMPORT = "import { beginNativeWrite, finishNativeWrite, failNativeWrite } from '../tools/analienx-write-audit.js';"
EDIT_IMPORT = "import { beginNativeWrite, finishNativeWrite, failNativeWrite } from './analienx-write-audit.js';"
FS_MARKER = "// ANALienx native write audit"
EDIT_MARKER = "// ANALienx edit audit"

class HookError(RuntimeError):
    pass

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _write_atomic(path: Path, data: bytes, *, allow_locked: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temp, path)
        except PermissionError:
            if not allow_locked or not path.is_file():
                raise
            with path.open("r+b") as handle:
                handle.seek(0)
                handle.write(data)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            if path.read_bytes() != data:
                raise HookError(f"locked-target write verification failed: {path}")
    finally:
        Path(temp).unlink(missing_ok=True)

def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise HookError(f"{label} signature changed or ambiguous (matches={count})")
    return source.replace(old, new, 1)

def _patch_filesystem(source: str) -> str:
    if FS_MARKER in source:
        if FS_IMPORT not in source:
            raise HookError("partial filesystem audit hook detected")
        return source
    anchor = "import { withTimeout } from '../utils/withTimeout.js';"
    source = _replace_once(source, anchor, anchor + "\n" + FS_IMPORT, "filesystem import")
    source = _replace_once(
        source,
        "export async function handleWriteFile(args) {\n    try {",
        "export async function handleWriteFile(args) {\n    let nativeAudit = null;\n    try {",
        "write_file function",
    )
    source = _replace_once(
        source,
        "        // Pass the mode parameter to writeFile\n        await writeFile(parsed.path, parsed.content, parsed.mode);",
        "        // " + FS_MARKER + "\n"
        "        nativeAudit = beginNativeWrite('write_file', [resolveAbsolutePath(parsed.path)], {\n"
        "            bytes: Buffer.byteLength(parsed.content, 'utf8'), mode: parsed.mode, line_count: lineCount\n"
        "        });\n"
        "        await writeFile(parsed.path, parsed.content, parsed.mode);\n"
        "        finishNativeWrite(nativeAudit);",
        "write_file mutation",
    )
    source = _replace_once(
        source,
        "    catch (error) {\n        const errorMessage = error instanceof Error ? error.message : String(error);\n        return createErrorResponse(errorMessage);\n    }\n}\n/**\n * Handle create_directory command",
        "    catch (error) {\n        failNativeWrite(nativeAudit, error);\n"
        "        const errorMessage = error instanceof Error ? error.message : String(error);\n"
        "        return createErrorResponse(errorMessage);\n    }\n}\n/**\n * Handle create_directory command",
        "write_file catch",
    )

    source = _replace_once(
        source,
        "export async function handleCreateDirectory(args) {\n    try {\n        const parsed = CreateDirectoryArgsSchema.parse(args);\n        await createDirectory(parsed.path);",
        "export async function handleCreateDirectory(args) {\n    let nativeAudit = null;\n    try {\n"
        "        const parsed = CreateDirectoryArgsSchema.parse(args);\n"
        "        nativeAudit = beginNativeWrite('create_directory', [resolveAbsolutePath(parsed.path)]);\n"
        "        await createDirectory(parsed.path);\n        finishNativeWrite(nativeAudit);",
        "create_directory mutation",
    )
    source = _replace_once(
        source,
        "    catch (error) {\n        const errorMessage = error instanceof Error ? error.message : String(error);\n        return createErrorResponse(errorMessage);\n    }\n}\n/**\n * Handle list_directory command",
        "    catch (error) {\n        failNativeWrite(nativeAudit, error);\n"
        "        const errorMessage = error instanceof Error ? error.message : String(error);\n"
        "        return createErrorResponse(errorMessage);\n    }\n}\n/**\n * Handle list_directory command",
        "create_directory catch",
    )
    source = _replace_once(
        source,
        "export async function handleMoveFile(args) {\n    try {\n        const parsed = MoveFileArgsSchema.parse(args);\n        await moveFile(parsed.source, parsed.destination);",
        "export async function handleMoveFile(args) {\n    let nativeAudit = null;\n    try {\n"
        "        const parsed = MoveFileArgsSchema.parse(args);\n"
        "        nativeAudit = beginNativeWrite('move_file', [resolveAbsolutePath(parsed.source), resolveAbsolutePath(parsed.destination)]);\n"
        "        await moveFile(parsed.source, parsed.destination);\n        finishNativeWrite(nativeAudit);",
        "move_file mutation",
    )

    source = _replace_once(
        source,
        "    catch (error) {\n        const errorMessage = error instanceof Error ? error.message : String(error);\n        return createErrorResponse(errorMessage);\n    }\n}\n/**\n * Format a value for display",
        "    catch (error) {\n        failNativeWrite(nativeAudit, error);\n"
        "        const errorMessage = error instanceof Error ? error.message : String(error);\n"
        "        return createErrorResponse(errorMessage);\n    }\n}\n/**\n * Format a value for display",
        "move_file catch",
    )
    source = _replace_once(
        source,
        "export async function handleWritePdf(args) {\n    try {\n        const parsed = WritePdfArgsSchema.parse(args);\n        await writePdf(parsed.path, parsed.content, parsed.outputPath, parsed.options);\n        const targetPath = parsed.outputPath || parsed.path;",
        "export async function handleWritePdf(args) {\n    let nativeAudit = null;\n    try {\n"
        "        const parsed = WritePdfArgsSchema.parse(args);\n"
        "        const targetPath = parsed.outputPath || parsed.path;\n"
        "        nativeAudit = beginNativeWrite('write_pdf', [resolveAbsolutePath(targetPath)]);\n"
        "        await writePdf(parsed.path, parsed.content, parsed.outputPath, parsed.options);\n"
        "        finishNativeWrite(nativeAudit);",
        "write_pdf mutation",
    )
    tail = "    catch (error) {\n        const errorMessage = error instanceof Error ? error.message : String(error);\n        return createErrorResponse(errorMessage);\n    }\n}\n"
    if source.count(tail) < 1:
        raise HookError("write_pdf catch signature changed")
    pos = source.rfind(tail)
    source = source[:pos] + tail.replace(
        "    catch (error) {\n",
        "    catch (error) {\n        failNativeWrite(nativeAudit, error);\n",
        1,
    ) + source[pos + len(tail):]
    return source

def _patch_edit(source: str) -> str:
    if EDIT_MARKER in source:
        if EDIT_IMPORT not in source:
            raise HookError("partial edit audit hook detected")
        return source
    anchor = "import { getDefaultEditorMetadata, writeFile, readFileInternal, validatePath } from './filesystem.js';"
    source = _replace_once(source, anchor, anchor + "\n" + EDIT_IMPORT, "edit import")
    source = _replace_once(
        source,
        "        await writeFile(filePath, newContent);",
        "        // " + EDIT_MARKER + "\n"
        "        const nativeAudit = beginNativeWrite('edit_block', [resolveAbsolutePath(filePath)], {\n"
        "            expected_replacements: expectedReplacements, replacement_count: count\n"
        "        });\n"
        "        try {\n            await writeFile(filePath, newContent);\n"
        "            finishNativeWrite(nativeAudit, { replacement_count: count });\n"
        "        }\n        catch (error) {\n            failNativeWrite(nativeAudit, error);\n            throw error;\n        }",
        "plain text edit mutation",
    )
    source = _replace_once(
        source,
        "        if (hasEditRange) {\n            try {\n                // parsed.range is guaranteed non-empty string by hasRange check above\n                await handler.editRange(validatedPath, parsed.range, content, parsed.options);",
        "        if (hasEditRange) {\n            let nativeAudit = null;\n            try {\n"
        "                // parsed.range is guaranteed non-empty string by hasRange check above\n"
        "                nativeAudit = beginNativeWrite('edit_block', [resolveAbsolutePath(parsed.file_path)]);\n"
        "                await handler.editRange(validatedPath, parsed.range, content, parsed.options);\n"
        "                finishNativeWrite(nativeAudit);",
        "range edit mutation",
    )

    range_catch = "            catch (error) {\n                const errorMessage = error instanceof Error ? error.message : String(error);\n                return createErrorResponse(errorMessage);\n            }\n        }\n        return createErrorResponse("
    source = _replace_once(
        source,
        range_catch,
        "            catch (error) {\n                failNativeWrite(nativeAudit, error);\n"
        "                const errorMessage = error instanceof Error ? error.message : String(error);\n"
        "                return createErrorResponse(errorMessage);\n            }\n        }\n"
        "        return createErrorResponse(",
        "range edit catch",
    )
    source = _replace_once(
        source,
        "    if (hasEditRange) {\n        try {\n            const result = await handler.editRange(validatedPath, '', {",
        "    if (hasEditRange) {\n        let nativeAudit = null;\n        try {\n"
        "            nativeAudit = beginNativeWrite('edit_block', [resolveAbsolutePath(parsed.file_path)], {\n"
        "                expected_replacements: parsed.expected_replacements\n"
        "            });\n"
        "            const result = await handler.editRange(validatedPath, '', {",
        "structured text edit mutation",
    )
    source = _replace_once(
        source,
        "            if (result.success) {\n                const resolvedEditRangePath = resolveAbsolutePath(parsed.file_path);",
        "            if (result.success) {\n"
        "                finishNativeWrite(nativeAudit, { replacement_count: result.editsApplied });\n"
        "                const resolvedEditRangePath = resolveAbsolutePath(parsed.file_path);",
        "structured text edit success",
    )
    source = _replace_once(
        source,
        "            const errorMsg = result.errors?.map(e => e.error).join('; ') || 'Unknown error';\n            return createErrorResponse(errorMsg);",
        "            const errorMsg = result.errors?.map(e => e.error).join('; ') || 'Unknown error';\n"
        "            failNativeWrite(nativeAudit, new Error(errorMsg));\n"
        "            return createErrorResponse(errorMsg);",
        "structured text edit rejection",
    )

    source = _replace_once(
        source,
        "        catch (error) {\n            const errorMessage = error instanceof Error ? error.message : String(error);\n            return createErrorResponse(errorMessage);\n        }\n    }\n    return performSearchReplace",
        "        catch (error) {\n            failNativeWrite(nativeAudit, error);\n"
        "            const errorMessage = error instanceof Error ? error.message : String(error);\n"
        "            return createErrorResponse(errorMessage);\n        }\n    }\n"
        "    return performSearchReplace",
        "structured text edit catch",
    )
    return source

def preflight() -> None:
    for path in (FS_TARGET, EDIT_TARGET, HELPER_SOURCE):
        if not path.is_file():
            raise HookError(f"required hook file is missing: {path}")
    _patch_filesystem(FS_TARGET.read_text(encoding="utf-8"))
    _patch_edit(EDIT_TARGET.read_text(encoding="utf-8"))


def status() -> dict[str, Any]:
    fs = FS_TARGET.read_text(encoding="utf-8") if FS_TARGET.is_file() else ""
    edit = EDIT_TARGET.read_text(encoding="utf-8") if EDIT_TARGET.is_file() else ""
    helper_matches = (
        HELPER_TARGET.is_file() and HELPER_SOURCE.is_file()
        and HELPER_TARGET.read_bytes() == HELPER_SOURCE.read_bytes()
    )
    healthy = (
        FS_IMPORT in fs and FS_MARKER in fs
        and EDIT_IMPORT in edit and EDIT_MARKER in edit
        and helper_matches
    )
    return {
        "schema": "analienx.rdc-write-audit-hook/v1",
        "healthy": healthy,
        "filesystem_hook": FS_IMPORT in fs and FS_MARKER in fs,
        "edit_hook": EDIT_IMPORT in edit and EDIT_MARKER in edit,
        "helper_matches": helper_matches,
        "filesystem_target": str(FS_TARGET),
        "edit_target": str(EDIT_TARGET),
        "helper_target": str(HELPER_TARGET),
    }

def ensure() -> dict[str, Any]:
    for path in (FS_TARGET, EDIT_TARGET, HELPER_SOURCE):
        if not path.is_file():
            raise HookError(f"required hook file is missing: {path}")
    CONTROL_ROOT.mkdir(parents=True, exist_ok=True)
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    prior = {}
    if STATE_FILE.is_file():
        try:
            prior = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = {}

    fs_original = FS_TARGET.read_bytes()
    edit_original = EDIT_TARGET.read_bytes()
    fs_patched = _patch_filesystem(fs_original.decode("utf-8")).encode("utf-8")
    edit_patched = _patch_edit(edit_original.decode("utf-8")).encode("utf-8")
    backups = {}
    for label, target, original, patched in (
        ("filesystem", FS_TARGET, fs_original, fs_patched),
        ("edit", EDIT_TARGET, edit_original, edit_patched),
    ):
        if original != patched:
            backup = BACKUP_ROOT / f"{target.name}.{_sha(original)}.native-write-audit.bak"
            if not backup.exists():
                _write_atomic(backup, original)
            backups[label] = str(backup)
            _write_atomic(target, patched, allow_locked=True)
        else:
            backups[label] = prior.get(f"{label}_backup")

    _write_atomic(HELPER_TARGET, HELPER_SOURCE.read_bytes(), allow_locked=True)
    state = {
        "schema": "analienx.rdc-write-audit-hook-state/v1",
        "filesystem_sha256": _sha(FS_TARGET.read_bytes()),
        "edit_sha256": _sha(EDIT_TARGET.read_bytes()),
        "filesystem_backup": backups["filesystem"],
        "edit_backup": backups["edit"],
    }
    _write_atomic(STATE_FILE, (json.dumps(state, indent=2) + "\n").encode("utf-8"))
    result = status()
    result["changed"] = fs_original != fs_patched or edit_original != edit_patched
    if not result["healthy"]:
        raise HookError("native write audit hook is not healthy after ensure")
    return result


def uninstall() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return {"ok": True, "changed": False, "reason": "no native-write hook state"}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HookError(f"cannot read native-write hook state: {exc}") from exc
    pairs = (
        ("filesystem", FS_TARGET, state.get("filesystem_sha256"), state.get("filesystem_backup")),
        ("edit", EDIT_TARGET, state.get("edit_sha256"), state.get("edit_backup")),
    )
    for label, target, expected, backup_raw in pairs:
        if not isinstance(expected, str) or not isinstance(backup_raw, str):
            raise HookError(f"native-write {label} restore metadata is missing")
        backup = Path(backup_raw)
        if not target.is_file() or _sha(target.read_bytes()) != expected:
            raise HookError(f"Desktop Commander {label} handler changed since hook install")
        if not backup.is_file():
            raise HookError(f"native-write {label} backup is missing")
    for _, target, _, backup_raw in pairs:
        _write_atomic(target, Path(backup_raw).read_bytes(), allow_locked=True)
    if HELPER_TARGET.is_file():
        if not HELPER_SOURCE.is_file() or HELPER_TARGET.read_bytes() != HELPER_SOURCE.read_bytes():
            raise HookError("native-write helper changed since hook install")
        HELPER_TARGET.unlink()
    STATE_FILE.unlink(missing_ok=True)
    return {"ok": True, "changed": True}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install/verify RDC native-write audit hook")
    parser.add_argument("action", choices=("status", "ensure", "uninstall"))
    args = parser.parse_args(argv)
    try:
        if args.action == "status":
            result = status()
            code = 0 if result.get("healthy") else 3
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
