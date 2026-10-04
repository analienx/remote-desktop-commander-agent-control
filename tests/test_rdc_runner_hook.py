import importlib.util
import json
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
        self.schema_target = self.dc_root / "dist" / "tools" / "schemas.js"
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
        self.policy_source.write_text("export async function routeAnalienxRunner() { return {command: 'x', shell: 'cmd.exe'}; }\n", encoding="utf-8")
        self.original = (
            "import { fileURLToPath } from 'url';\n"
            "export async function startProcess(args) {\n"
            "    const parsed = StartProcessArgsSchema.safeParse(args);\n"
            "    if (!parsed.success) { return { isError: true }; }\n"
            "    try {\n"
            "        const commands = commandManager.extractCommands(parsed.data.command).join(', ');\n"
            "    } catch (error) {}\n"
            "    const isAllowed = await commandManager.validateCommand(parsed.data.command);\n"
            "    if (!isAllowed) { return { isError: true }; }\n"
            "}\n"
            "export async function interactWithProcess(args) {\n"
            "    const parsed = InteractWithProcessArgsSchema.safeParse(args);\n"
            "    if (!parsed.success) { return { isError: true }; }\n"
            "    const { pid, input, timeout_ms = 8000, wait_for_prompt = true, verbose_timing = false } = parsed.data;\n"
            "    if (virtualNodeSessions.has(pid)) {\n"
            "        const session = virtualNodeSessions.get(pid);\n"
            "        const effectiveTimeout = timeout_ms ?? session.timeout_ms;\n"
            "        return executeNodeCode(input, effectiveTimeout);\n"
            "    }\n"
            "    let exitReason = 'timeout';\n"
            "    try {\n"
            "        const success = terminalManager.sendInputToProcess(pid, input);\n"
            "        if (!success) {\n"
            "            return {\n"
            "                content: [],\n"
            "                isError: true,\n"
            "            };\n"
            "        }\n"
            "        if (!wait_for_prompt) {\n"
            "            exitReason = 'no_wait';\n"
            "            return { content: [] };\n"
            "        }\n"
            "        const waitForResponse = async () => {};\n"
            "        await waitForResponse();\n"
            "        // Clean and format output\n"
            "        return { content: [] };\n"
            "    }\n"
            "    catch (error) {\n"
            "        const errorMessage = error instanceof Error ? error.message : String(error);\n"
            "        capture('server_interact_with_process_error', {\n"
            "            error: errorMessage\n"
            "        });\n"
            "        return { isError: true };\n"
            "    }\n"
            "}\n"
            "export async function forceTerminate(args) {\n"
            "    const parsed = ForceTerminateArgsSchema.safeParse(args);\n"
            "    if (!parsed.success) { return { isError: true }; }\n"
            "    const pid = parsed.data.pid;\n"
            "    // Handle virtual Node.js sessions (node:local)\n"
            "    if (virtualNodeSessions.has(pid)) {\n"
            "        virtualNodeSessions.delete(pid);\n"
            "        return { content: [] };\n"
            "    }\n"
            "    const success = terminalManager.forceTerminate(pid);\n"
            "    return { content: [], success };\n"
            "}\n"
        )
        self.target.write_bytes(self.original.encode("utf-8"))
        self.schema_original = (
            "import { z } from \"zod\";\n"
            "export const StartProcessArgsSchema = z.object({\n"
            "    command: z.string(),\n"
            "    timeout_ms: z.number(),\n"
            "    shell: z.string().optional(),\n"
            "    verbose_timing: z.boolean().optional(),\n"
            "    origin: z.enum(['ui', 'llm']).optional(),\n"
            "});\n"
        )
        self.schema_target.write_bytes(self.schema_original.encode("utf-8"))
        class FakeWriteAuditHook:
            @staticmethod
            def preflight():
                return None

            @staticmethod
            def status():
                return {"healthy": True}

            @staticmethod
            def ensure():
                return {"healthy": True, "changed": False}

            @staticmethod
            def uninstall():
                return {"ok": True, "changed": False}

        values = {
            "WRITE_AUDIT_HOOK": FakeWriteAuditHook(),
            "CONTROL_ROOT": self.control,
            "DC_ROOT": self.dc_root,
            "TARGET": self.target,
            "SCHEMA_TARGET": self.schema_target,
            "POLICY_TARGET": self.policy_target,
            "POLICY_SOURCE": self.policy_source,
            "STATE_FILE": self.state,
            "BACKUP_ROOT": self.backups,
            "REQUEST_ROOT": self.requests,
            "SLRUNNER_ENTRY": self.runner_marker,
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
        self.assertEqual(current.count("const isAllowed = await commandManager.validateCommand(parsed.data.command);"), 1)
        first = hook.ensure()
        self.assertTrue(first["healthy"])
        self.assertTrue(first["changed"])
        patched = self.target.read_text(encoding="utf-8")
        self.assertIn(hook.IMPORT_LINE, patched)
        self.assertIn(hook.CALL_MARKER, patched)
        self.assertIn(hook.CONTROL_AUDIT_IMPORT, patched)
        self.assertIn(hook.INTERACT_AUDIT_MARKER, patched)
        self.assertIn(hook.FORCE_AUDIT_MARKER, patched)
        self.assertIn("parsed.data.command = routed.command;", patched)
        self.assertLess(patched.index(hook.CALL_MARKER), patched.index("const isAllowed = await commandManager.validateCommand(parsed.data.command);"))
        schema_patched = self.schema_target.read_text(encoding="utf-8")
        self.assertIn(hook.SCHEMA_MARKER, schema_patched)
        self.assertIn("options: z.object({", schema_patched)
        self.assertIn("'read_many'", schema_patched)
        self.assertIn(hook.SCHEMA_CONTEXT_MARKER, schema_patched)
        self.assertIn("context_id: z.string().min(1).max(256).optional()", schema_patched)
        self.assertIn("task_context: z.object({", schema_patched)
        self.assertIn("objective: z.string().min(1).max(512).optional()", schema_patched)
        second = hook.ensure()
        self.assertFalse(second["changed"])
        self.policy_source.write_text(
            "export async function routeAnalienxRunner() { return {command: 'updated', shell: 'cmd.exe'}; }\n",
            encoding="utf-8",
        )
        policy_update = hook.ensure()
        self.assertTrue(policy_update["changed"])
        self.assertEqual(self.policy_target.read_bytes(), self.policy_source.read_bytes())
        self.assertFalse(hook.ensure()["changed"])
        hook.uninstall()
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.original)
        self.assertEqual(self.schema_target.read_text(encoding="utf-8"), self.schema_original)

    def test_upgrade_preserves_original_restore_backup(self):
        original_backup = self.backups / "original.js"
        schema_backup = self.backups / "schema-original.js"
        self.backups.mkdir(parents=True, exist_ok=True)
        original_backup.write_bytes(self.original.encode("utf-8"))
        schema_backup.write_bytes(self.schema_original.encode("utf-8"))

        route_only = hook._patched_source(self.original)
        schema_patched = hook._patched_schema(self.schema_original)
        self.target.write_bytes(route_only.encode("utf-8"))
        self.schema_target.write_bytes(schema_patched.encode("utf-8"))
        self.state.write_text(json.dumps({
            "schema": "analienx.rdc-runner-hook-state/v1",
            "original_sha256": hook._sha_bytes(self.original.encode("utf-8")),
            "patched_sha256": hook._sha_bytes(route_only.encode("utf-8")),
            "backup_path": str(original_backup),
            "schema_original_sha256": hook._sha_bytes(self.schema_original.encode("utf-8")),
            "schema_patched_sha256": hook._sha_bytes(schema_patched.encode("utf-8")),
            "schema_backup_path": str(schema_backup),
        }), encoding="utf-8")

        result = hook.ensure()
        self.assertTrue(result["healthy"])
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["backup_path"], str(original_backup))
        self.assertEqual(state["schema_backup_path"], str(schema_backup))
        self.assertEqual(
            state["original_sha256"],
            hook._sha_bytes(self.original.encode("utf-8")),
        )
        hook.uninstall()
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.original)
        self.assertEqual(self.schema_target.read_text(encoding="utf-8"), self.schema_original)

    def test_upgrade_adds_task_context_to_options_only_install(self):
        legacy = self.schema_original.replace(
            hook.SCHEMA_ANCHOR,
            hook.SCHEMA_ANCHOR + "\n" + hook.SCHEMA_OPTIONS_BLOCK.rstrip("\n"),
            1,
        )
        self.schema_target.write_bytes(legacy.encode("utf-8"))
        result = hook.ensure()
        self.assertTrue(result["healthy"])
        patched = self.schema_target.read_text(encoding="utf-8")
        self.assertIn(hook.SCHEMA_MARKER, patched)
        self.assertIn(hook.SCHEMA_CONTEXT_MARKER, patched)
        self.assertFalse(hook.ensure()["changed"])

    def test_upgrade_refuses_unknown_process_source_drift(self):
        first = hook.ensure()
        self.assertTrue(first["healthy"])
        self.target.write_bytes(
            self.target.read_bytes() + b"// unknown drift\n"
        )
        with self.assertRaisesRegex(hook.HookError, "drifted"):
            hook.ensure()

    def test_partial_hook_refuses_patch(self):
        self.target.write_bytes((hook.IMPORT_LINE + "\n" + self.original).encode("utf-8"))
        with self.assertRaises(hook.HookError):
            hook.ensure()

if __name__ == "__main__":
    unittest.main()
