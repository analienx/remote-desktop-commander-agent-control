import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
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
        self.real_preflight = bridge._preflight_designation
        self.patchers = [
            mock.patch.object(bridge, "REQUEST_ROOT", self.request_root),
            mock.patch.object(bridge, "WORKSPACE_ROOT", self.workspace),
            mock.patch.object(bridge, "SLRUNNER_ENTRY", self.runner),
            mock.patch.object(
                bridge, "_preflight_designation", side_effect=lambda request: request
            ),
        ]
        started = [patcher.start() for patcher in self.patchers]
        self.preflight_mock = started[-1]

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

    def test_preflight_single_route_rewrites_to_canonical_worktree(self):
        canonical = self.workspace / "worktrees" / "cinema-canonical"
        canonical.mkdir(parents=True)
        request = bridge._load_request(self.request(
            cwd=str(self.workspace),
            worktree=str(self.workspace),
            project=None,
            stream=None,
            initiative_id="unclassified-workspace",
            category="RDC-UNCLASSIFIED",
            command="launch FERAL keyframe generation",
        ))
        router = SimpleNamespace(route_task=lambda *args, **kwargs: {
            "routing": "single",
            "project_id": "cinema",
            "repository": "analienx/cinema",
            "stream_id": "autonomous-production",
            "worktree": {
                "cwd": str(canonical),
                "worktree_root": str(canonical),
                "basis": "tiny_jev_location",
            },
        })
        with mock.patch.object(bridge, "_load_task_router", return_value=(router, Path("registry"))), \
             mock.patch.object(bridge, "INITIATIVE_REGISTRY", self.workspace / "missing.json"):
            result = self.real_preflight(request)
        self.assertEqual(result["project"], "cinema")
        self.assertEqual(result["stream"], "autonomous-production")
        self.assertEqual(Path(result["cwd"]), canonical.resolve())
        self.assertEqual(Path(result["worktree"]), canonical.resolve())
        self.assertEqual(result["initiative_id"], "adhoc-cinema-autonomous-production")
        self.assertEqual(result["category"], "RDC")

    def test_preflight_blocks_multi_repo_before_execution(self):
        request = bridge._load_request(self.request(
            cwd=str(self.workspace),
            worktree=str(self.workspace),
            project=None,
            stream=None,
            initiative_id="unclassified-workspace",
            command="inspect bseed PM role matrix",
        ))
        router = SimpleNamespace(route_task=lambda *args, **kwargs: {
            "routing": "multi_repo",
            "projects": [
                {"project_id": "bseed"},
                {"project_id": "bseed-ts0726-dimmer"},
            ],
        })
        with mock.patch.object(bridge, "_load_task_router", return_value=(router, Path("registry"))):
            with self.assertRaisesRegex(bridge.BridgeError, "one explicit project/worktree"):
                self.real_preflight(request)

    def test_required_clarification_survives_bridge_and_never_spawns_runner(self):
        path = self.request(
            cwd=str(self.workspace), worktree=str(self.workspace),
            project=None, stream=None, initiative_id="unclassified-workspace",
        )
        clarification = {
            "required": True,
            "kind": "registration_or_mapping",
            "requested_task": "inspect demo",
            "question": "Map this repo to an existing project or register it?",
            "candidates": [{"repository": "analienx/demo"}],
            "required_fields": ["decision", "project_id"],
            "answer_contract": {"decision": ["map", "register"]},
            "resume_instruction": "Rerun routing with the selected project.",
        }
        router = SimpleNamespace(route_task=lambda *args, **kwargs: {
            "routing": "needs_registration", "clarification": clarification,
        })
        self.preflight_mock.side_effect = self.real_preflight
        with mock.patch.object(bridge, "_load_task_router", return_value=(router, Path("registry"))):
            with mock.patch.object(bridge.subprocess, "Popen") as popen:
                with self.assertRaises(bridge.BridgeError) as ctx:
                    bridge.execute(path)
        prefix, encoded = str(ctx.exception).split(" ", 1)
        self.assertEqual(prefix, "ROUTING_CLARIFICATION_REQUIRED")
        self.assertEqual(json.loads(encoded), {
            "event": "ROUTING_CLARIFICATION_REQUIRED",
            "routing": "needs_registration",
            "clarification": clarification,
        })
        popen.assert_not_called()

    def test_single_route_with_required_clarification_never_spawns_runner(self):
        path = self.request(project=None, stream=None, initiative_id="unclassified-workspace")
        router = SimpleNamespace(route_task=lambda *args, **kwargs: {
            "routing": "single", "project_id": "cinema",
            "worktree": {"cwd": str(self.cwd), "worktree_root": str(self.cwd)},
            "clarification": {"required": True, "question": "Choose a worktree"},
        })
        self.preflight_mock.side_effect = self.real_preflight
        with mock.patch.object(bridge, "_load_task_router", return_value=(router, Path("registry"))):
            with mock.patch.object(bridge.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(bridge.BridgeError, "ROUTING_CLARIFICATION_REQUIRED"):
                    bridge.execute(path)
        popen.assert_not_called()

    def test_router_selection_rejects_relative_and_file_paths(self):
        selected_file = self.workspace / "not-a-directory"
        selected_file.write_text("fixture")
        for cwd, root in [
            (".", str(self.cwd)), (str(self.cwd), "."),
            (str(selected_file), str(self.workspace)),
            (str(selected_file), str(selected_file)),
        ]:
            with self.subTest(cwd=cwd, root=root):
                path = self.request(project=None, stream=None, initiative_id="unclassified-workspace")
                router = SimpleNamespace(route_task=lambda *args, **kwargs: {
                    "routing": "single", "project_id": "cinema",
                    "worktree": {"cwd": cwd, "worktree_root": root},
                })
                self.preflight_mock.side_effect = self.real_preflight
                with mock.patch.object(bridge, "_load_task_router", return_value=(router, Path("registry"))):
                    with mock.patch.object(bridge.subprocess, "Popen") as popen:
                        with self.assertRaises(bridge.BridgeError):
                            bridge.execute(path)
                popen.assert_not_called()

    def test_preflight_blocks_ambiguous_stream(self):
        request = bridge._load_request(self.request(
            project=None,
            stream=None,
            initiative_id="unclassified-workspace",
        ))
        router = SimpleNamespace(route_task=lambda *args, **kwargs: {
            "routing": "project_only",
            "stream": {"alternatives": ["one", "two"]},
        })
        with mock.patch.object(bridge, "_load_task_router", return_value=(router, Path("registry"))):
            with self.assertRaisesRegex(bridge.BridgeError, "workstream is ambiguous"):
                self.real_preflight(request)

    def test_preflight_blocks_ambiguous_worktree(self):
        request = bridge._load_request(self.request(
            project=None,
            stream=None,
            initiative_id="unclassified-workspace",
        ))
        router = SimpleNamespace(route_task=lambda *args, **kwargs: {
            "routing": "worktree_ambiguous",
            "worktree": {
                "candidates": [
                    {"path": r"C:\Workspace\repos\cinema"},
                    {"path": r"C:\Workspace\worktrees\cinema-feral"},
                ]
            },
        })
        with mock.patch.object(bridge, "_load_task_router", return_value=(router, Path("registry"))):
            with self.assertRaisesRegex(bridge.BridgeError, "worktree is ambiguous"):
                self.real_preflight(request)

    def test_typed_read_capability_skips_execution_designation(self):
        request = bridge._load_request(self.request(
            command=None,
            capability={"schema": 1, "action": "processes"},
            project=None,
            stream=None,
            initiative_id="unclassified-workspace",
        ))
        with mock.patch.object(
            bridge, "_load_task_router", side_effect=AssertionError("read must not route")
        ):
            result = self.real_preflight(request)
        self.assertEqual(result["capability"]["action"], "processes")

    def test_failed_preflight_never_spawns_slrunner(self):
        path = self.request(
            cwd=str(self.workspace),
            worktree=str(self.workspace),
            project=None,
            stream=None,
            initiative_id="unclassified-workspace",
        )
        self.preflight_mock.side_effect = bridge.BridgeError("designation required")
        with mock.patch.object(bridge.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(bridge.BridgeError, "designation required"):
                bridge.execute(path)
        popen.assert_not_called()

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
