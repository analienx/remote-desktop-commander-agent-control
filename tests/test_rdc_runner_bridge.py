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
        self.runner = root / "SLRunner" / "slrunner.py"
        self.runner.parent.mkdir()
        self.runner.write_text("# test runner\n", encoding="utf-8")
        self.patchers = [
            mock.patch.object(bridge, "REQUEST_ROOT", self.request_root),
            mock.patch.object(bridge, "WORKSPACE_ROOT", self.workspace),
            mock.patch.object(bridge, "SLRUNNER_ENTRY", self.runner),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def request(self, **overrides):
        payload = {
            "schema": bridge.SCHEMA,
            "cwd": str(self.cwd),
            "command": "node tool.mjs --check",
            "shell": "cmd.exe",
            "initiative_id": "feral-60s-trailer",
            "activity_type": "keyframe-generation",
            "project": "cinema",
            "stream": "feral",
            "category": "FERAL",
            "repository": "analienx/cinema",
            "worktree": str(self.cwd),
            "timeout_seconds": 120,
            "heartbeat_seconds": 15,
        }
        payload.update(overrides)
        path = self.request_root / "11111111-1111-1111-1111-111111111111.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_valid_request_executes_installed_slrunner_and_preserves_exit(self):
        path = self.request()
        observed = {}
        process = mock.Mock()
        process.poll.return_value = 7
        process.wait.return_value = 7
        def fake_popen(argv):
            translated = Path(argv[-1])
            observed["argv"] = argv
            observed["request"] = json.loads(translated.read_text(encoding="utf-8"))
            return process
        job = mock.Mock()
        job.__enter__ = mock.Mock(return_value=job)
        job.__exit__ = mock.Mock(return_value=False)
        parent = mock.Mock()
        parent.__enter__ = mock.Mock(return_value=parent)
        parent.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(bridge.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(bridge, "BridgeJob", return_value=job), \
             mock.patch.object(bridge, "ParentMonitor", return_value=parent):
            code = bridge.execute(path)
        self.assertEqual(code, 7)
        job.assign.assert_called_once_with(process)
        self.assertEqual(observed["argv"][:2], [bridge.sys.executable, str(self.runner)])
        self.assertEqual(observed["request"]["origin"], "rdc")
        self.assertEqual(observed["request"]["initiative_id"], "feral-60s-trailer")
        self.assertEqual(observed["request"]["activity_type"], "keyframe-generation")
        self.assertEqual(observed["request"]["project"], "cinema")
        self.assertEqual(observed["request"]["command"], "node tool.mjs --check")

    def test_bridge_job_assignment_failure_kills_slrunner(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = -9
        job = mock.Mock()
        job.__enter__ = mock.Mock(return_value=job)
        job.__exit__ = mock.Mock(return_value=False)
        job.assign.side_effect = bridge.BridgeError("cannot attach")
        parent = mock.Mock()
        parent.__enter__ = mock.Mock(return_value=parent)
        parent.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=process), \
             mock.patch.object(bridge, "BridgeJob", return_value=job), \
             mock.patch.object(bridge, "ParentMonitor", return_value=parent):
            with self.assertRaisesRegex(bridge.BridgeError, "cannot attach"):
                bridge.execute(self.request())
        process.kill.assert_called_once()

    def test_parent_exit_closes_job_and_returns_cancelled(self):
        process = mock.Mock()
        process.poll.side_effect = [None]
        process.wait.return_value = -9
        job = mock.Mock()
        job.__enter__ = mock.Mock(return_value=job)
        job.__exit__ = mock.Mock(return_value=False)
        parent = mock.Mock()
        parent.__enter__ = mock.Mock(return_value=parent)
        parent.__exit__ = mock.Mock(return_value=False)
        parent.exited.return_value = True
        with mock.patch.object(bridge.subprocess, "Popen", return_value=process), \
             mock.patch.object(bridge, "BridgeJob", return_value=job), \
             mock.patch.object(bridge, "ParentMonitor", return_value=parent):
            code = bridge.execute(self.request())
        self.assertEqual(code, 130)
        job.close.assert_called_once()
        parent.exited.assert_called()

    def test_shell_commands_are_allowed_and_delegated_to_slrunner_guard(self):
        req = bridge._load_request(self.request(
            command="powershell.exe -NoProfile -File x.ps1",
            shell="cmd.exe",
        ))
        self.assertIn("powershell.exe", req["command"])

    def test_argv_form_is_supported(self):
        req = bridge._load_request(self.request(command=None, argv=["python", "tool.py"]))
        self.assertEqual(req["argv"], ["python", "tool.py"])
        self.assertIsNone(req["command"])

    def test_typed_capability_form_is_supported_and_strict(self):
        req = bridge._load_request(self.request(
            command=None,
            capability={
                "schema": 1,
                "action": "search",
                "roots": ["."],
                "query": "needle",
            },
        ))
        self.assertIsNone(req["command"])
        self.assertEqual(req["capability"]["action"], "search")
        with self.assertRaisesRegex(bridge.BridgeError, "unknown capability fields"):
            bridge._load_request(self.request(
                command=None,
                capability={
                    "schema": 1,
                    "action": "system",
                    "command": "whoami",
                },
            ))

    def test_missing_initiative_is_rejected(self):
        with self.assertRaisesRegex(bridge.BridgeError, "initiative_id"):
            bridge._load_request(self.request(initiative_id=None))

    def test_request_outside_fixed_root_is_rejected(self):
        outside = self.workspace / "outside.json"
        outside.write_text(json.dumps({
            "schema": bridge.SCHEMA,
            "cwd": str(self.cwd),
            "command": "python tool.py",
        }), encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "fixed RDC request root"):
            bridge._load_request(outside)

    def test_missing_slrunner_fails_closed(self):
        self.runner.unlink()
        with self.assertRaisesRegex(bridge.BridgeError, "not installed"):
            bridge.execute(self.request())


if __name__ == "__main__":
    unittest.main()
