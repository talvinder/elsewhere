"""Exercise the plugin's permission, project, and result boundaries."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "herdr"
spec = importlib.util.spec_from_file_location("elsewhere_herdr", PLUGIN / "elsewhere_herdr.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class HerdrPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "project with spaces"
        self.source.mkdir()
        self.environment = patch.dict(os.environ, {
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"focused_pane_cwd": str(self.source)}),
            "HERDR_PLUGIN_STATE_DIR": str(Path(self.temp.name) / "state"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.work = {"source": self.source, "command": "printf 'hello world'", "workload": "light"}

    def test_manifest_has_no_automatic_execution(self):
        manifest = tomllib.loads((PLUGIN / "herdr-plugin.toml").read_text())
        self.assertEqual(manifest["id"], bridge.PLUGIN_ID)
        self.assertEqual(manifest["min_herdr_version"], "0.9.0")
        self.assertNotIn("startup", manifest)
        self.assertNotIn("events", manifest)
        self.assertNotIn("build", manifest)
        for entry in manifest["actions"] + manifest["panes"]:
            self.assertTrue((PLUGIN / entry["command"][1]).is_file())
            self.assertNotIn("--execute", entry["command"])

    def test_project_comes_from_focused_pane_not_plugin_cwd(self):
        self.assertEqual(bridge.context_source(), self.source.resolve())
        with patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": "{}"}):
            with self.assertRaises(bridge.WorkflowError):
                bridge.context_source()
        with self.assertRaises(bridge.WorkflowError):
            bridge.context_source(str(PLUGIN))

    def test_explicit_source_wins_and_missing_context_fails_closed(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        self.assertEqual(bridge.context_source(str(other)), other.resolve())
        with self.assertRaises(bridge.WorkflowError):
            bridge.context_source("/")

    def test_argv_preserves_command_as_single_argument_and_never_executes_plan(self):
        command = "printf '%s' '$HOME; $(touch should-not-exist)'"
        args = bridge.route_args({**self.work, "command": command})
        self.assertEqual(args[args.index("--command") + 1], command)
        self.assertNotIn("--execute", args)
        self.assertIn("--no-queue", args)

    def test_call_uses_project_cwd_and_no_shell(self):
        with patch.object(bridge, "executable", return_value="/bin/elsewhere"), patch.object(bridge.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"ok":true}', "")
            self.assertEqual(bridge.call(["status"], self.source), (0, {"ok": True}))
            self.assertEqual(run.call_args.kwargs["cwd"], self.source)
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_bad_cli_output_does_not_leak_provider_details(self):
        with patch.object(bridge, "executable", return_value="elsewhere"), patch.object(bridge.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "secret", "https://storage.example/?sig=secret")
            with self.assertRaises(bridge.WorkflowError) as caught:
                bridge.call(["status"], self.source)
            self.assertNotIn("secret", str(caught.exception))

    def test_remote_trust_denial_never_dispatches(self):
        plan = {"decision": {"placement": "remote"}, "plan": {"trust": {"allowed": False}}}
        with patch.object(bridge, "call") as call:
            with self.assertRaises(bridge.WorkflowError):
                bridge.execute(self.work, plan)
            call.assert_not_called()

    def test_local_review_cannot_become_remote_when_capacity_changes(self):
        with patch.object(bridge, "call", return_value=(3, {"executed": False})) as call:
            _, record = bridge.execute(self.work, {"decision": {"placement": "local"}})
            args = call.call_args.args[0]
            self.assertEqual(args[args.index("--execution") + 1], "local")
            self.assertIn("--execute", args)
            self.assertIn("--no-queue", args)
            self.assertEqual(record["state"], "not_started")

    def test_remote_dispatch_binds_receipt_and_retains_job_without_raw_plan(self):
        job_id = "a" * 32
        plan = {"decision": {"placement": "remote"}, "plan": {"trust": {"allowed": True, "receipt": "approved"}}}
        with patch.object(bridge, "call", return_value=(0, {"job": {"id": job_id, "state": "submitted"}})) as call:
            _, record = bridge.execute(self.work, plan)
            args = call.call_args.args[0]
            self.assertEqual(args[args.index("--approval-receipt") + 1], "approved")
            path = bridge.state_dir(self.source) / (record["id"] + ".json")
            self.assertEqual(json.loads(path.read_text())["job_id"], job_id)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("command", path.read_text())
            self.assertNotIn("approved", path.read_text())

    def test_cannot_dispatch_if_receipt_storage_fails(self):
        with patch.object(bridge, "save_record", side_effect=OSError("read only")), patch.object(bridge, "call") as call:
            with self.assertRaises(OSError):
                bridge.execute(self.work, {"decision": {"placement": "local"}})
            call.assert_not_called()

    def test_cleanup_requires_verified_recovery_and_terminal_state(self):
        for record in ({"placement": "remote", "state": "running"},
                       {"placement": "remote", "state": "failed", "receipt": {"result_verified": False}}):
            with patch.object(bridge, "inspect_record", return_value=record), patch.object(bridge, "call") as call:
                with self.assertRaises(bridge.WorkflowError):
                    bridge.cleanup(self.source, record)
                call.assert_not_called()

    def test_failed_workload_with_verified_results_can_be_cleaned(self):
        record = {"id": "b" * 32, "job_id": "a" * 32, "placement": "remote", "state": "failed",
                  "receipt": {"result_verified": True, "exit_code": 1}}
        receipt = {**record["receipt"], "cleanup_verified": True, "result_path": "/retained/results"}
        with patch.object(bridge, "inspect_record", return_value=record), patch.object(bridge, "call", return_value=(0, {"state": "cleaned", "receipt": receipt})) as call:
            bridge.cleanup(self.source, record)
            self.assertEqual(call.call_args.args[0], ["job-cleanup", "a" * 32])
            self.assertEqual(record["receipt"]["exit_code"], 1)
            self.assertEqual(record["receipt"]["result_path"], "/retained/results")

    def test_cleanup_failure_is_retained_for_retry(self):
        record = {"id": "b" * 32, "job_id": "a" * 32, "placement": "remote", "state": "failed",
                  "receipt": {"result_verified": True}}
        with patch.object(bridge, "inspect_record", return_value=record), patch.object(bridge, "call", return_value=(1, {"state": "cleanup_failed"})):
            with self.assertRaises(bridge.WorkflowError):
                bridge.cleanup(self.source, record)
            self.assertEqual(record["state"], "cleanup_failed")

    def test_cleaned_receipt_does_not_requery_deleted_transport(self):
        record = {"job_id": "a" * 32, "state": "cleaned", "receipt": {"cleanup_verified": True}}
        with patch.object(bridge, "call") as call:
            bridge.inspect_record(self.source, record)
            call.assert_not_called()

    def test_follow_stops_at_deadline_without_cancelling_or_cleaning(self):
        record = {"job_id": "a" * 32, "state": "running"}
        with patch.object(bridge, "inspect_record"), patch.object(bridge, "call") as call:
            bridge.follow_record(self.source, record, timeout_seconds=0)
            call.assert_not_called()

    def test_records_are_scoped_to_project(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        self.assertNotEqual(bridge.state_dir(self.source), bridge.state_dir(other))

    def test_run_without_execute_only_plans(self):
        path = Path(self.temp.name) / "spec.json"
        path.write_text(json.dumps({"command": "echo hello"}))
        with patch.object(sys, "argv", ["plugin", "run", "--spec", str(path)]), patch.object(bridge, "preview"), patch.object(bridge, "execute") as execute:
            self.assertEqual(bridge.main(), 1)
            execute.assert_not_called()

    def test_real_cli_dry_plan_creates_no_job_or_source_archive(self):
        environment = {**os.environ, "AGENT_CAPACITY_STATE": str(Path(self.temp.name) / "leases.json"),
                       "AGENT_CAPACITY_JOBS": str(Path(self.temp.name) / "jobs.json"),
                       "AGENT_CAPACITY_CONFIG": str(Path(self.temp.name) / "config.json")}
        Path(environment["AGENT_CAPACITY_CONFIG"]).write_text(json.dumps({"trust": {"approved": False, "inherit_global": False}}))
        args = bridge.route_args({**self.work, "execution": "local"})
        result = subprocess.run([sys.executable, str(ROOT / "src/agent_capacity/cli.py"), *args],
                                cwd=self.source, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["executed"])
        self.assertFalse(Path(environment["AGENT_CAPACITY_JOBS"]).exists())


if __name__ == "__main__":
    unittest.main()
