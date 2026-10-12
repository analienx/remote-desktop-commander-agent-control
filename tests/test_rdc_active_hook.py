"""Regression tests for the version-pinned RDC launcher/hook package boundary."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "rdc_active_hook.py"
spec = importlib.util.spec_from_file_location("rdc_active_hook_test", SCRIPT)
assert spec and spec.loader
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class ManagedInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctrl = self.root / "RDC-Control"
        self.ctrl.mkdir()
        self.cache = self.root / "npm-cache"
        self.patches = [
            patch.object(hook, "CONTROL", self.ctrl),
            patch.object(hook, "NPM_CACHE", self.cache),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.version = "0.2.52"
        self.folder = self.ctrl / "managed" / ("desktop-commander-" + self.version)
        self.pkg = self.folder / "node_modules" / "@wonderwhy-er" / "desktop-commander"
        (self.pkg / "dist").mkdir(parents=True)
        (self.pkg / "dist" / "index.js").write_text("// fake entrypoint\n")
        self.write_meta(self.version)
        self.write_lock(self.version)
        self.cfg = self.ctrl / "rdc-active-install.json"
        self.data = {
            "schema": 1,
            "package_root": str(self.pkg),
            "version": self.version,
            "installation_id": "managed-0.2.52",
        }
        self.write_config()

    def write_meta(self, version):
        (self.pkg / "package.json").write_text(
            json.dumps({"name": "@wonderwhy-er/desktop-commander", "version": version})
        )

    def write_lock(self, version):
        (self.folder / "package-lock.json").write_text(json.dumps({
            "packages": {"node_modules/@wonderwhy-er/desktop-commander": {"version": version}}
        }))

    def write_config(self):
        self.cfg.write_text(json.dumps(self.data), encoding="utf-8")

    def test_valid_pinned_managed_package(self):
        result, path = hook.checked_config(self.cfg)
        self.assertEqual(result["installation_id"], "managed-0.2.52")
        self.assertEqual(path, self.pkg)

    def test_rejects_unpinned_lockfile(self):
        self.write_lock("0.2.53")
        with self.assertRaisesRegex(hook.CanonicalInstallError, "lockfile"):
            hook.checked_config(self.cfg)

    def test_rejects_package_version_mismatch(self):
        self.write_meta("0.2.53")
        with self.assertRaisesRegex(hook.CanonicalInstallError, "version mismatch"):
            hook.checked_config(self.cfg)

    def test_rejects_untrusted_package_root(self):
        self.data["package_root"] = str(self.root / "other-install")
        self.write_config()
        with self.assertRaisesRegex(hook.CanonicalInstallError, "does not match pinned"):
            hook.checked_config(self.cfg)

    def test_rejects_extra_config_fields(self):
        self.data["untrusted"] = "ignored?"
        self.write_config()
        with self.assertRaisesRegex(hook.CanonicalInstallError, "schema/fields"):
            hook.checked_config(self.cfg)

    def test_rejects_unknown_install_identity(self):
        self.data["installation_id"] = "managed-0.2.53"
        self.write_config()
        with self.assertRaisesRegex(hook.CanonicalInstallError, "not an authorized"):
            hook.checked_config(self.cfg)

    def test_rejects_config_outside_control_root(self):
        external = self.root / "rdc-active-install.json"
        external.write_text(json.dumps(self.data))
        with self.assertRaisesRegex(hook.CanonicalInstallError, "config must live"):
            hook.checked_config(external)

    def test_pinned_npx_rollback_is_possible(self):
        self.data["installation_id"] = "npx-4b4c857f6efdfb61"
        cache_package = self.cache / "_npx" / "4b4c857f6efdfb61" / "node_modules" / "@wonderwhy-er" / "desktop-commander"
        (cache_package / "dist").mkdir(parents=True)
        (cache_package / "dist" / "index.js").write_text("// rollback entrypoint\n")
        (cache_package / "package.json").write_text(json.dumps({
            "name": "@wonderwhy-er/desktop-commander", "version": "0.2.52"
        }))
        self.data["package_root"] = str(cache_package)
        self.write_config()
        self.assertEqual(hook.checked_config(self.cfg)[1], cache_package)


if __name__ == "__main__":
    unittest.main()
