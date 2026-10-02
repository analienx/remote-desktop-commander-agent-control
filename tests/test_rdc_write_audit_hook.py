import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "rdc_write_audit_hook.py"
SPEC = importlib.util.spec_from_file_location("rdc_write_audit_hook", MODULE_PATH)
hook = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(hook)


class WriteAuditHookTests(unittest.TestCase):
    def test_kill_process_patch_is_exact_and_idempotent(self):
        source = """import { KillProcessArgsSchema } from './schemas.js';
export async function killProcess(args) {
    const parsed = KillProcessArgsSchema.safeParse(args);
    if (!parsed.success) {
        return {
            content: [{ type: "text", text: `Error: Invalid arguments for kill_process: ${parsed.error}` }],
            isError: true,
        };
    }
    try {
        process.kill(parsed.data.pid);
        return {
            content: [{ type: "text", text: `Successfully terminated process ${parsed.data.pid}` }],
        };
    }
    catch (error) {
        return {
            content: [{ type: "text", text: `Error: Failed to kill process: ${error instanceof Error ? error.message : String(error)}` }],
            isError: true,
        };
    }
}
"""
        patched = hook._patch_process(source)
        self.assertIn(hook.PROCESS_IMPORT, patched)
        self.assertIn(hook.PROCESS_MARKER, patched)
        self.assertIn("beginNativeControl('process_terminate'", patched)
        self.assertIn("termination_kind: 'kill_process'", patched)
        self.assertEqual(hook._patch_process(patched), patched)

    def test_partial_process_patch_fails_closed(self):
        source = hook.PROCESS_IMPORT + "\n" + "export async function killProcess(args) {}\n"
        with self.assertRaises(hook.HookError):
            hook._patch_process(source)


if __name__ == "__main__":
    unittest.main()
