import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "rdc_runner_bridge.py"
SPEC = importlib.util.spec_from_file_location("rdc_runner_bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workspace = root / "Workspace"
        self.request_root = self.workspace / ".analienx" / "rdc-requests"
        self.cwd = self.workspace / "worktrees" / "cinema"
        self.request_root.mkdir(parents=True)
        self.cwd.mkdir(parents=True)
        self.token = root / "client-token.txt"
        self.token.write_text("x" * 64, encoding="ascii")
        self.patches = [
            mock.patch.object(bridge, "REQUEST_ROOT", self.request_root),
            mock.patch.object(bridge, "WORKSPACE_ROOT", self.workspace),
            mock.patch.object(bridge, "TOKEN_FILE", self.token),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def request(self, **overrides):
        payload = {
            "schema": bridge.SCHEMA,
            "cwd": str(self.cwd),
            "argv": ["python", "tool.py", "--check"],
            "category": "FERAL",
            "repository": "cinema",
            "worktree": "feral/test",
            "timeout_seconds": 120,
            "wait_seconds": 10,
        }
        payload.update(overrides)
        path = self.request_root / "11111111-1111-1111-1111-111111111111.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_valid_request_submits_workspace_exec_and_waits(self):
        path = self.request()
        calls = []
        def fake_call(method, route, body=None, timeout=15):
            calls.append((method, route, body))
            if method == "POST":
                return {"ok": True, "job_id": "22222222-2222-2222-2222-222222222222"}
            return {"ok": True, "job_id": "22222222-2222-2222-2222-222222222222", "result": "ok"}
        with mock.patch.object(bridge, "_call", side_effect=fake_call):
            result = bridge.submit(path)
        self.assertTrue(result["ok"])
        submitted = calls[0][2]
        self.assertEqual(submitted["origin"], "rdc")
        self.assertEqual(submitted["operation"], "workspace_exec")
        self.assertEqual(submitted["cwd"], str(self.cwd.resolve()))
        self.assertEqual(submitted["command"], ["python", "tool.py", "--check"])
        self.assertEqual(submitted["label"], "[RDC][FERAL][cinema]")

    def test_shell_wrapper_is_rejected(self):
        path = self.request(argv=["powershell.exe", "-File", "x.ps1"])
        with self.assertRaisesRegex(bridge.BridgeError, "shell wrappers"):
            bridge._load_request(path)

    def test_secret_like_argv_is_rejected(self):
        path = self.request(argv=["python", "tool.py", "--token", "abc"])
        with self.assertRaisesRegex(bridge.BridgeError, "secret-like"):
            bridge._load_request(path)

    def test_request_outside_fixed_root_is_rejected(self):
        outside = self.workspace / "outside.json"
        outside.write_text(json.dumps({
            "schema": bridge.SCHEMA,
            "cwd": str(self.cwd),
            "argv": ["python", "tool.py"],
        }), encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "fixed RDC request root"):
            bridge._load_request(outside)

    def test_missing_runner_token_fails_closed(self):
        self.token.unlink()
        with self.assertRaisesRegex(bridge.BridgeError, "not installed"):
            bridge._token()


if __name__ == "__main__":
    unittest.main()
