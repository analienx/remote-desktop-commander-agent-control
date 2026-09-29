import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "rdc_runner_hook.py"
SPEC = importlib.util.spec_from_file_location("rdc_runner_hook", MODULE_PATH)
hook = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(hook)

class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.control = root / "control"
        self.dc_root = root / "desktop-commander"
        self.target = self.dc_root / "dist" / "tools" / "improved-process-tools.js"
        self.policy_target = self.dc_root / "dist" / "tools" / "analienx-runner-policy.js"
        self.policy_source = self.control / "analienx-runner-policy.js"
        self.state = self.control / "state.json"
        self.backups = self.control / "backups"
        self.requests = root / "Workspace" / ".analienx" / "rdc-requests"
        self.runner_marker = root / "runner-client-ready"
        self.target.parent.mkdir(parents=True)
        self.control.mkdir(parents=True)
        self.runner_marker.write_text("ready", encoding="ascii")
        (self.dc_root / "package.json").write_text('{"version":"0.2.51"}', encoding="utf-8")
        self.policy_source.write_text("export function enforceAnalienxRunnerRouting() {}\n", encoding="utf-8")
        self.original = (
            "import { fileURLToPath } from 'url';\n"
            "export async function startProcess(args) {\n"
            "    const parsed = StartProcessArgsSchema.safeParse(args);\n"
            "    if (!parsed.success) { return { isError: true }; }\n"
            "    try {\n"
            "        const commands = commandManager.extractCommands(parsed.data.command).join(', ');\n"
            "    } catch (error) {}\n"
            "}\n"
        )
        self.target.write_text(self.original, encoding="utf-8")
        values = {
            "CONTROL_ROOT": self.control,
            "DC_ROOT": self.dc_root,
            "TARGET": self.target,
            "POLICY_TARGET": self.policy_target,
            "POLICY_SOURCE": self.policy_source,
            "STATE_FILE": self.state,
            "BACKUP_ROOT": self.backups,
            "REQUEST_ROOT": self.requests,
            "RUNNER_TOKEN": self.runner_marker,
        }
        self.patchers = [mock.patch.object(hook, key, value) for key, value in values.items()]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def test_install_idempotence_and_exact_restore(self):
        current = self.target.read_text(encoding="utf-8")
        self.assertEqual(current.count(hook.IMPORT_ANCHOR), 1)
        self.assertEqual(current.count("const commands = commandManager.extractCommands(parsed.data.command).join(\', \');"), 1)
        first = hook.ensure()
        self.assertTrue(first["healthy"])
        self.assertTrue(first["changed"])
        patched = self.target.read_text(encoding="utf-8")
        self.assertIn(hook.IMPORT_LINE, patched)
        self.assertIn(hook.CALL_MARKER, patched)
        second = hook.ensure()
        self.assertFalse(second["changed"])
        hook.uninstall()
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.original)

    def test_partial_hook_refuses_patch(self):
        self.target.write_text(hook.IMPORT_LINE + "\n" + self.original, encoding="utf-8")
        with self.assertRaises(hook.HookError):
            hook.ensure()

if __name__ == "__main__":
    unittest.main()
