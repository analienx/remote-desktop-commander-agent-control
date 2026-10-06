#!/usr/bin/env python3
"""Strict RDC -> thin SLRunner bridge.

The bridge is intentionally boring: validate that the request lives under the fixed
workspace request root, translate it to the SLRunner schema, and execute the installed
SLRunner as a child with inherited stdin/stdout/stderr. There is no HTTP service,
scheduler, token, or second execution policy here.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

REQUEST_ROOT = Path(r"C:\Workspace\.analienx\rdc-requests")
WORKSPACE_ROOT = Path(r"C:\Workspace")
SLRUNNER_ENTRY = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Analienx" / "SLRunner" / "slrunner.py"
TASK_ROUTER_ENTRY = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Analienx" / "TaskRouter" / "task_router.py"
INITIATIVE_REGISTRY = WORKSPACE_ROOT / ".analienx" / "runner" / "initiatives.json"
LEGACY_SCHEMA = "analienx.rdc-slrunner-request/v1"
EXPLICIT_SCHEMA = "analienx.rdc-slrunner-request/v2"
SCHEMAS = {LEGACY_SCHEMA, EXPLICIT_SCHEMA}
SCHEMA = LEGACY_SCHEMA  # compatibility alias for legacy tests/callers
MAX_EXECUTION_BYTES = 256 * 1024
SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
MAX_ARGV = 256
MAX_ARG = 32768
MAX_COMMAND = 131072
MAX_CAPABILITY_BYTES = 256 * 1024
CAPABILITY_ACTIONS = {
    "read_many", "list", "search", "repo_status",
    "snapshot", "delta", "system", "processes",
}
CAPABILITY_FIELDS = {
    "schema", "action", "paths", "roots", "query", "max_results", "max_bytes",
    "max_files", "max_depth", "snapshot_id", "excludes", "include_content",
}


class BridgeError(RuntimeError):
    pass




class ParentMonitor:
    """Stable handle to the RDC terminal parent; PID reuse cannot fool this watcher."""
    def __init__(self):
        self.handle = None
        self.kernel32 = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x00100000, False, os.getppid())  # SYNCHRONIZE
        if not handle:
            raise BridgeError("cannot monitor owning RDC terminal process")
        self.handle = handle
        self.kernel32 = kernel32

    def exited(self, timeout_ms: int = 250) -> bool:
        if os.name != "nt":
            return False
        result = int(self.kernel32.WaitForSingleObject(self.handle, timeout_ms))
        if result == 0x00000000:  # WAIT_OBJECT_0
            return True
        if result == 0x00000102:  # WAIT_TIMEOUT
            return False
        raise BridgeError(f"RDC parent wait failed: 0x{result:08x}")

    def close(self) -> None:
        if self.handle and self.kernel32:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class BridgeJob:
    """Own the SLRunner subtree so killing the RDC bridge cannot orphan execution."""
    def __init__(self):
        self.handle = None
        self.kernel32 = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise BridgeError("cannot create RDC bridge Job Object")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            raise BridgeError("cannot configure RDC bridge Job Object")
        self.handle = handle
        self.kernel32 = kernel32

    def assign(self, process: subprocess.Popen) -> None:
        if os.name != "nt":
            return
        from ctypes import wintypes
        if not self.handle or not self.kernel32 or not self.kernel32.AssignProcessToJobObject(
            self.handle, wintypes.HANDLE(int(process._handle))
        ):
            raise BridgeError("cannot attach SLRunner to RDC bridge Job Object")

    def close(self) -> None:
        if self.handle and self.kernel32:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _contains(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=True).relative_to(parent.resolve(strict=True))
        return True
    except ValueError:
        return False


def _load_task_router():
    if not TASK_ROUTER_ENTRY.is_file():
        raise BridgeError(f"TaskRouter is not installed: {TASK_ROUTER_ENTRY}")
    parent = str(TASK_ROUTER_ENTRY.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec = importlib.util.spec_from_file_location(
        "analienx_rdc_task_router", TASK_ROUTER_ENTRY
    )
    if spec is None or spec.loader is None:
        raise BridgeError(f"cannot load TaskRouter: {TASK_ROUTER_ENTRY}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry = TASK_ROUTER_ENTRY.with_name("projects.yaml")
    if not registry.is_file():
        raise BridgeError(f"TaskRouter registry is missing: {registry}")
    return module, registry


def _task_text(request: dict) -> str:
    command = request.get("command")
    if isinstance(command, str) and command.strip():
        return command
    argv = request.get("argv")
    if isinstance(argv, list) and argv:
        return subprocess.list2cmdline([str(value) for value in argv])
    return ""


def _bounded_string(value, name: str, limit: int, *, optional: bool = False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise BridgeError(f"{name} must be a non-empty string up to {limit} characters")
    return value


def _validate_execution_contract(value: object) -> dict:
    if not isinstance(value, dict):
        raise BridgeError("execution must be an object")
    allowed = {"schema", "identity", "operation", "authorization"}
    unknown = set(value) - allowed
    if unknown:
        raise BridgeError(f"unknown execution fields: {sorted(unknown)}")
    if value.get("schema") != 1:
        raise BridgeError("execution schema must be 1")

    identity = value.get("identity")
    if not isinstance(identity, dict):
        raise BridgeError("execution.identity must be an object")
    allowed_identity = {"project_id", "worktree", "repository", "stream_id"}
    unknown = set(identity) - allowed_identity
    if unknown:
        raise BridgeError(f"unknown execution.identity fields: {sorted(unknown)}")
    project_id = _bounded_string(identity.get("project_id"), "execution.identity.project_id", 160)
    worktree_raw = _bounded_string(identity.get("worktree"), "execution.identity.worktree", 512)
    worktree = Path(worktree_raw)
    if not worktree.is_absolute() or not worktree.is_dir() or not _contains(worktree, WORKSPACE_ROOT):
        raise BridgeError("execution.identity.worktree must be an existing absolute workspace directory")
    repository = _bounded_string(
        identity.get("repository"), "execution.identity.repository", 160, optional=True
    )
    stream_id = _bounded_string(
        identity.get("stream_id"), "execution.identity.stream_id", 160, optional=True
    )

    operation_out = None
    operation = value.get("operation")
    if operation is not None:
        if not isinstance(operation, dict):
            raise BridgeError("execution.operation must be an object")
        unknown = set(operation) - {"name", "parameters"}
        if unknown:
            raise BridgeError(f"unknown execution.operation fields: {sorted(unknown)}")
        name = _bounded_string(operation.get("name"), "execution.operation.name", 120)
        parameters = operation.get("parameters", {})
        if not isinstance(parameters, dict):
            raise BridgeError("execution.operation.parameters must be an object")
        operation_out = {"name": name, "parameters": parameters}

    authorization_out = None
    authorization = value.get("authorization")
    if authorization is not None:
        if not isinstance(authorization, dict):
            raise BridgeError("execution.authorization must be an object")
        allowed_auth = {
            "schema", "kind", "authorization_id", "expires_at", "max_attempts",
            "target", "artifact_sha256", "helper_sha256",
        }
        unknown = set(authorization) - allowed_auth
        if unknown:
            raise BridgeError(f"unknown execution.authorization fields: {sorted(unknown)}")
        if authorization.get("schema") != 1:
            raise BridgeError("execution.authorization schema must be 1")
        if authorization.get("kind") not in {"user", "supervisor", "delegation"}:
            raise BridgeError("execution.authorization.kind is invalid")
        auth_id = _bounded_string(
            authorization.get("authorization_id"),
            "execution.authorization.authorization_id", 128,
        )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", auth_id):
            raise BridgeError("execution.authorization.authorization_id has invalid characters")
        expires_at = _bounded_string(
            authorization.get("expires_at"), "execution.authorization.expires_at", 64
        )
        attempts = authorization.get("max_attempts")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 100:
            raise BridgeError("execution.authorization.max_attempts must be 1..100")
        authorization_out = {
            "schema": 1,
            "kind": authorization["kind"],
            "authorization_id": auth_id,
            "expires_at": expires_at,
            "max_attempts": attempts,
        }
        for field, limit in (("target", 512),):
            if authorization.get(field) is not None:
                authorization_out[field] = _bounded_string(
                    authorization[field], f"execution.authorization.{field}", limit
                )
        for field in ("artifact_sha256", "helper_sha256"):
            digest = authorization.get(field)
            if digest is not None:
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise BridgeError(f"execution.authorization.{field} must be lowercase SHA-256")
                authorization_out[field] = digest

    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_EXECUTION_BYTES:
        raise BridgeError("execution contract exceeds size limit")
    out = {
        "schema": 1,
        "identity": {
            "project_id": project_id,
            "worktree": str(worktree.resolve(strict=True)),
        },
    }
    if repository is not None:
        out["identity"]["repository"] = repository
    if stream_id is not None:
        out["identity"]["stream_id"] = stream_id
    if operation_out is not None:
        out["operation"] = operation_out
    if authorization_out is not None:
        out["authorization"] = authorization_out
    return out


def _explicit_designation(request: dict) -> dict:
    execution = request.get("execution")
    if execution is None:
        return request
    identity = execution["identity"]
    module, registry = _load_task_router()
    try:
        result = module.route_task(
            "explicit execution identity",
            registry,
            hint_project=identity["project_id"],
            hint_stream=identity.get("stream_id"),
            current_cwd=identity["worktree"],
        )
    except Exception as exc:
        raise BridgeError(f"explicit execution identity validation failed: {exc}") from exc
    if not isinstance(result, dict):
        raise BridgeError("TaskRouter returned an invalid explicit identity designation")
    clarification = result.get("clarification")
    if result.get("routing") != "single" or (
        isinstance(clarification, dict) and clarification.get("required") is True
    ):
        raise BridgeError(
            "ANALIENX_RDC_EXECUTION_IDENTITY_REJECTED "
            + json.dumps(
                {
                    "routing": result.get("routing"),
                    "clarification": clarification,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    if result.get("project_id") != identity["project_id"]:
        raise BridgeError(
            "ANALIENX_RDC_EXECUTION_IDENTITY_REJECTED: registered project does not match explicit project_id"
        )
    if identity.get("repository") is not None and result.get("repository") != identity["repository"]:
        raise BridgeError(
            "ANALIENX_RDC_EXECUTION_IDENTITY_REJECTED: registered repository does not match explicit repository"
        )
    if identity.get("stream_id") is not None and result.get("stream_id") != identity["stream_id"]:
        raise BridgeError(
            "ANALIENX_RDC_EXECUTION_IDENTITY_REJECTED: registered stream does not match explicit stream_id"
        )
    location = result.get("worktree")
    if not isinstance(location, dict):
        raise BridgeError("ANALIENX_RDC_EXECUTION_IDENTITY_REJECTED: TaskRouter omitted worktree")
    resolved_root = location.get("worktree_root")
    resolved_cwd = location.get("cwd")
    if not isinstance(resolved_root, str) or not isinstance(resolved_cwd, str):
        raise BridgeError("ANALIENX_RDC_EXECUTION_IDENTITY_REJECTED: TaskRouter worktree is incomplete")
    expected = Path(identity["worktree"]).resolve(strict=True)
    actual = Path(resolved_root).resolve(strict=True)
    if expected != actual:
        raise BridgeError(
            "ANALIENX_RDC_EXECUTION_IDENTITY_REJECTED: registered worktree does not match explicit worktree"
        )
    cwd = Path(request["cwd"]).resolve(strict=True)
    if not _contains(cwd, expected):
        raise BridgeError(
            "ANALIENX_RDC_EXECUTION_IDENTITY_REJECTED: execution cwd is outside explicit worktree"
        )

    designated = dict(request)
    designated["cwd"] = str(cwd)
    designated["worktree"] = str(expected)
    designated["project"] = identity["project_id"]
    designated["repository"] = identity.get("repository") or result.get("repository")
    designated["stream"] = identity.get("stream_id") or result.get("stream_id")
    designated["initiative_id"] = _initiative_for(
        designated["project"], designated["stream"], cwd
    )
    designated["category"] = "RDC"
    designated["label"] = (
        f"[RDC][{designated['project']}]"
        + (f"[{designated['stream']}]" if designated["stream"] else "")
    )[:200]
    designated["routing_basis"] = "explicit_execution_contract"
    return designated


def _initiative_for(project: str, stream: str | None, cwd: Path) -> str:
    matches: set[str] = set()
    try:
        payload = json.loads(INITIATIVE_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    rows = payload.get("bindings") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict) or row.get("project") != project:
                continue
            if stream and row.get("stream") not in {None, stream}:
                continue
            root_raw = row.get("root")
            initiative = row.get("initiative_id")
            if not isinstance(root_raw, str) or not isinstance(initiative, str):
                continue
            try:
                root = Path(root_raw)
                if _contains(cwd, root) or _contains(root, cwd):
                    matches.add(initiative)
            except OSError:
                continue
    if len(matches) == 1:
        return next(iter(matches))
    slug = SAFE_SEGMENT.sub("-", project).strip("-") or "project"
    if stream:
        stream_slug = SAFE_SEGMENT.sub("-", stream).strip("-")
        if stream_slug:
            slug += "-" + stream_slug
    return ("adhoc-" + slug)[:120]


def _preflight_designation(request: dict) -> dict:
    if request.get("execution") is not None:
        return _explicit_designation(request)
    if request.get("capability") is not None:
        return request
    task = _task_text(request)
    if not task:
        raise BridgeError("execution request has no routable task text")

    module, registry = _load_task_router()
    project = request.get("project")
    stream = request.get("stream")
    hint_project = project if isinstance(project, str) and project.strip() else None
    hint_stream = stream if isinstance(stream, str) and stream.strip() else None
    try:
        result = module.route_task(
            task,
            registry,
            hint_project=hint_project,
            hint_stream=hint_stream,
            current_cwd=request.get("cwd"),
        )
    except Exception as exc:
        raise BridgeError(f"TaskRouter preflight failed: {exc}") from exc
    if not isinstance(result, dict):
        raise BridgeError("TaskRouter returned an invalid designation")

    routing = str(result.get("routing") or "")
    clarification = result.get("clarification")
    needs_clarification = isinstance(clarification, dict) and clarification.get("required") is True
    if routing != "single" or needs_clarification:
        if needs_clarification:
            raise BridgeError(
                "ROUTING_CLARIFICATION_REQUIRED "
                + json.dumps({
                    "event": "ROUTING_CLARIFICATION_REQUIRED",
                    "routing": routing or "unknown",
                    "clarification": clarification,
                }, ensure_ascii=False, separators=(",", ":"))
            )
        if routing == "multi_repo":
            projects = [
                str(row.get("project_id"))
                for row in result.get("projects", [])
                if isinstance(row, dict) and row.get("project_id")
            ]
            detail = ", ".join(projects) or "multiple projects"
            raise BridgeError(
                f"execution requires one explicit project/worktree; TaskRouter found: {detail}"
            )
        if routing == "project_only":
            alternatives = result.get("stream", {}).get("alternatives", [])
            raise BridgeError(
                "execution project is known but workstream is ambiguous; "
                f"choose an explicit stream before execution: {alternatives}"
            )
        if routing == "worktree_ambiguous":
            location = result.get("worktree") or {}
            candidates = [
                str(row.get("path"))
                for row in location.get("candidates", [])
                if isinstance(row, dict) and row.get("path")
            ]
            raise BridgeError(
                "execution project/workstream is known but worktree is ambiguous; "
                f"choose an explicit cwd/worktree before execution: {candidates}"
            )
        raise BridgeError(
            "execution has no single canonical designation; "
            f"TaskRouter routing={routing or 'unknown'}"
        )

    project_id = result.get("project_id")
    repository = result.get("repository")
    stream_id = result.get("stream_id")
    location = result.get("worktree")
    if not isinstance(project_id, str) or not project_id.strip():
        raise BridgeError("TaskRouter single route omitted project_id")
    if not isinstance(location, dict):
        raise BridgeError("TaskRouter single route omitted worktree selection")
    cwd_raw = location.get("cwd")
    root_raw = location.get("worktree_root")
    if not isinstance(cwd_raw, str) or not isinstance(root_raw, str):
        raise BridgeError("TaskRouter worktree selection is incomplete")
    cwd = Path(cwd_raw)
    worktree = Path(root_raw)
    if not cwd.is_absolute() or not worktree.is_absolute():
        raise BridgeError("TaskRouter worktree selection must use absolute directories")
    if (
        not cwd.is_dir()
        or not worktree.is_dir()
        or not _contains(cwd, worktree)
        or not _contains(worktree, WORKSPACE_ROOT)
    ):
        raise BridgeError("TaskRouter worktree selection escaped the workspace")

    designated = dict(request)
    designated["cwd"] = str(cwd.resolve(strict=True))
    designated["worktree"] = str(worktree.resolve(strict=True))
    designated["project"] = project_id
    designated["repository"] = (
        repository if isinstance(repository, str) and repository.strip() else None
    )
    designated["stream"] = (
        stream_id if isinstance(stream_id, str) and stream_id.strip() else None
    )
    designated["initiative_id"] = _initiative_for(
        project_id, designated["stream"], cwd
    )
    designated["category"] = "RDC"
    designated["label"] = (
        f"[RDC][{project_id}]"
        + (f"[{designated['stream']}]" if designated["stream"] else "")
    )[:200]
    return designated


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
        "schema", "cwd", "command", "argv", "capability", "execution", "shell", "project", "stream",
        "category", "initiative_id", "activity_type", "repository", "worktree", "label",
        "timeout_seconds", "heartbeat_seconds",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise BridgeError(f"unknown request fields: {sorted(unknown)}")
    request_schema = payload.get("schema")
    if request_schema not in SCHEMAS:
        raise BridgeError("unsupported RDC SLRunner request schema")
    if request_schema == EXPLICIT_SCHEMA and payload.get("execution") is None:
        raise BridgeError("v2 RDC request requires execution")
    if request_schema == LEGACY_SCHEMA and payload.get("execution") is not None:
        raise BridgeError("v1 RDC request cannot carry execution")

    cwd_raw = payload.get("cwd")
    if not isinstance(cwd_raw, str) or not cwd_raw:
        raise BridgeError("cwd is required")
    cwd = Path(cwd_raw)
    if not cwd.is_dir() or not _contains(cwd, WORKSPACE_ROOT):
        raise BridgeError("cwd must be an existing directory below C:\\Workspace")

    command = payload.get("command")
    argv = payload.get("argv")
    capability = payload.get("capability")
    execution = (
        _validate_execution_contract(payload.get("execution"))
        if payload.get("execution") is not None else None
    )
    operation = execution.get("operation") if execution is not None else None
    modes = int(bool(command)) + int(bool(argv)) + int(capability is not None) + int(operation is not None)
    if modes != 1:
        raise BridgeError("provide exactly one of command, argv, capability or named operation")
    if command is not None and (
        not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND
    ):
        raise BridgeError(f"command must be a non-empty string up to {MAX_COMMAND} characters")
    if argv is not None and (
        not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV
        or not all(isinstance(value, str) and 0 < len(value) <= MAX_ARG for value in argv)
    ):
        raise BridgeError(f"argv must contain 1..{MAX_ARGV} bounded non-empty strings")
    if capability is not None:
        if not isinstance(capability, dict):
            raise BridgeError("capability must be an object")
        unknown = set(capability) - CAPABILITY_FIELDS
        if unknown:
            raise BridgeError(f"unknown capability fields: {sorted(unknown)}")
        if capability.get("schema") != 1:
            raise BridgeError("capability schema must be 1")
        if capability.get("action") not in CAPABILITY_ACTIONS:
            raise BridgeError("unsupported capability action")
        encoded = json.dumps(capability, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_CAPABILITY_BYTES:
            raise BridgeError("capability request exceeds size limit")

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
        "capability": dict(capability) if isinstance(capability, dict) else None,
        "execution": execution,
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
    request = _preflight_designation(_load_request(request_file))
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
        # bridge -> SLRunner -> project child. The bridge owns the whole subtree via a
        # KILL_ON_JOB_CLOSE Job Object so terminating the RDC session cannot orphan it.
        with ParentMonitor() as parent, BridgeJob() as job:
            process = subprocess.Popen([
                sys.executable, str(SLRUNNER_ENTRY), "--request", str(translated)
            ])
            try:
                job.assign(process)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
                raise
            while True:
                code = process.poll()
                if code is not None:
                    return int(code)
                if parent.exited(250):
                    print(json.dumps({
                        "event": "RDC_SLRUNNER_PARENT_EXIT",
                        "job_id": request["job_id"],
                    }), file=sys.stderr, flush=True)
                    job.close()  # KILL_ON_JOB_CLOSE tears down SLRunner + project child.
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    return 130
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
