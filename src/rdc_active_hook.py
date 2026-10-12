#!/usr/bin/env python3
"""Version-pinned RDC hook resolver. Fail closed on stale or unexpected package paths.

This is the local wrapper used by Guardian; the installed hook modules enforce
the existing SLRunner / TaskRouter execution and native-write policies.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path

CONTROL = Path(__file__).resolve().parent
CONFIG = CONTROL / "rdc-active-install.json"
NPM_CACHE = Path.home() / "AppData" / "Local" / "npm-cache"
PACKAGE_SEGMENTS = ("node_modules", "@wonderwhy-er", "desktop-commander")


class CanonicalInstallError(RuntimeError):
    pass


def checked_config(filename: Path) -> tuple[dict, Path]:
    filename = filename.resolve()
    if filename.parent != CONTROL.resolve() or not filename.name.startswith("rdc-active-install"):
        raise CanonicalInstallError("config must live in RDC-Control and have approved basename")
    c = json.loads(filename.read_text(encoding="utf-8"))
    if set(c) != {"schema", "package_root", "version", "installation_id"} or c["schema"] != 1:
        raise CanonicalInstallError("invalid active installation schema/fields")
    version = c["version"]
    install_id = c["installation_id"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise CanonicalInstallError("invalid pinned RDC version")
    if not isinstance(install_id, str):
        raise CanonicalInstallError("invalid installation identity")
    if install_id == f"managed-{version}":
        expected = CONTROL / "managed" / f"desktop-commander-{version}"
        package = expected.joinpath(*PACKAGE_SEGMENTS)
    elif install_id == "npx-4b4c857f6efdfb61" and version == "0.2.52":
        expected = NPM_CACHE / "_npx" / "4b4c857f6efdfb61"
        package = expected.joinpath(*PACKAGE_SEGMENTS)
    else:
        raise CanonicalInstallError("installation ID is not an authorized managed or rollback installation")
    target = Path(c["package_root"])
    if not target.is_absolute() or target.resolve() != package.resolve():
        raise CanonicalInstallError("active installation package_root does not match pinned identity")
    if not package.is_dir():
        raise CanonicalInstallError("pinned RDC installation missing")
    meta = json.loads((package / "package.json").read_text(encoding="utf-8"))
    if meta.get("name") != "@wonderwhy-er/desktop-commander" or meta.get("version") != version:
        raise CanonicalInstallError("installed RDC package name/version mismatch")
    if not (package / "dist" / "index.js").is_file():
        raise CanonicalInstallError("RDC package entry point missing")
    if install_id.startswith("managed-"):
        lock = json.loads((expected / "package-lock.json").read_text(encoding="utf-8"))
        locked = lock.get("packages", {}).get("node_modules/@wonderwhy-er/desktop-commander", {})
        if locked.get("version") != version:
            raise CanonicalInstallError("managed lockfile does not pin the installed version")
    return c, package


def configure_hook(install: dict, root: Path):
    spec = importlib.util.spec_from_file_location(
        "rdc_managed_runner_hook", CONTROL / "rdc_runner_hook.py"
    )
    if spec is None or spec.loader is None:
        raise CanonicalInstallError("RDC runner hook module not importable")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.DC_ROOT = root
    runner.TARGET = root / "dist" / "tools" / "improved-process-tools.js"
    runner.SCHEMA_TARGET = root / "dist" / "tools" / "schemas.js"
    runner.POLICY_TARGET = root / "dist" / "tools" / "analienx-runner-policy.js"
    identity = install["installation_id"]
    runner.STATE_FILE = CONTROL / f"rdc-runner-hook-state-{identity}.json"
    runner.BACKUP_ROOT = CONTROL / "backups" / identity
    audit = runner.WRITE_AUDIT_HOOK
    audit.DC_ROOT = root
    audit.FS_TARGET = root / "dist" / "handlers" / "filesystem-handlers.js"
    audit.EDIT_TARGET = root / "dist" / "tools" / "edit.js"
    audit.PROCESS_TARGET = root / "dist" / "tools" / "process.js"
    audit.HELPER_TARGET = root / "dist" / "tools" / "analienx-write-audit.js"
    audit.STATE_FILE = CONTROL / f"rdc-write-audit-hook-state-{identity}.json"
    audit.BACKUP_ROOT = CONTROL / "backups" / identity
    return runner


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("ensure", "status", "package"))
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args(argv)
    try:
        install, root = checked_config(args.config)
        if args.action == "package":
            result = {"healthy": True}
        else:
            hook = configure_hook(install, root)
            result = hook.ensure() if args.action == "ensure" else hook.status()
        result.update(
            active_package_root=str(root),
            active_package_version=install["version"],
            active_installation_id=install["installation_id"],
        )
    except (CanonicalInstallError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        result = {"healthy": False, "error": str(exc)}
    print(json.dumps(result, indent=2))
    return 0 if result["healthy"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
