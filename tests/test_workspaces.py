from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_capacity import cli, workspaces
from agent_capacity.artifact_transport import (
    SOURCE_EXCLUDE_NAMES,
    SOURCE_EXCLUDE_SUFFIXES,
    SOURCE_EXCLUDES,
    package_source,
)
from agent_capacity.providers.sprites import (
    SpriteError,
    SpritesProvider,
    completed_stream,
    segment,
)
from agent_capacity.results import inspect_result_bundle
from agent_capacity.workspace_contract import (
    Capabilities,
    choose_workspace_provider,
    fingerprint,
    validate_request,
)


def request():
    return {
        "intent": "isolated",
        "derivation": "snapshot",
        "retention": "delete",
        "retention_seconds": 0,
        "network_policy": {"rules": [{"domain": "*", "action": "deny"}]},
        "connectors": [],
        "resources": SpritesProvider().resources(),
    }


class ContractTests(unittest.TestCase):
    def test_native_fork_never_substitutes_snapshot(self):
        value = request()
        value["derivation"] = "native-fork"
        with self.assertRaisesRegex(ValueError, "substitution is forbidden"):
            choose_workspace_provider(value, [SpritesProvider()])

    def test_another_cloud_can_satisfy_contract(self):
        class OtherCloud:
            name = "other-cloud"
            capabilities = Capabilities(True, True, True, True, True, True, ("delete",))

            def resources(self):
                return {"cpu": 2, "memory": "fixed"}

        value = request()
        value["resources"] = OtherCloud().resources()
        self.assertEqual(
            choose_workspace_provider(value, [SpritesProvider(), OtherCloud()]).name,
            "other-cloud",
        )

    def test_project_and_retention_guards(self):
        for mutation in (
            {"intent": "project"},
            {"retention_seconds": -1},
            {"connectors": ["unapproved"]},
            {"unexpected": True},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_request(
                    {**request(), **mutation}, SpritesProvider.capabilities
                )
        value = {
            **request(),
            "intent": "project",
            "project": {"name": "example", "id": "immutable"},
        }
        with self.assertRaisesRegex(ValueError, "shared project"):
            validate_request(value, SpritesProvider.capabilities)

    def test_exact_trust_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                provider="sprites",
                source_path=tmp,
                git_url=None,
                git_ref=None,
                image=None,
                max_runtime_seconds=60,
                estimated_cost_usd=0.1,
                workload_command="true",
                workload="test",
                result_path=[],
                approval_receipt=None,
            )
            config = cli.default_config()
            config["providers"]["sprites"] = {
                "enabled": True,
                "organization": "example",
            }
            config["trust"].update(
                approved=True,
                expires_at="2099-01-01T00:00:00+00:00",
                source={
                    "allow_private": True,
                    "allow_uncommitted": True,
                    "allowed_roots": [tmp],
                },
                providers={
                    "sprites": {
                        "identity": SpritesProvider().identity(
                            config["providers"]["sprites"]
                        ),
                        "regions": [],
                    }
                },
                artifact_store=cli.artifact_store_identity(config),
                limits={
                    "max_cpu": 8,
                    "max_memory_mb": 8192,
                    "max_runtime_seconds": 3600,
                    "max_estimated_cost_usd": 1,
                },
            )
            job, plan = workspaces.plan(cli, request(), args, config)
            self.assertFalse(plan["trust"]["allowed"])
            config["trust"]["workspace_boundaries"] = [plan["workspace_boundary"]]
            self.assertTrue(cli.evaluate_trust(job, plan, config)["allowed"])
            changed = copy.deepcopy(job)
            changed["workspace"]["retention"] = "keep"
            self.assertFalse(cli.evaluate_trust(changed, plan, config)["allowed"])
            self.assertFalse(
                cli.evaluate_trust(job, plan, config, "stale-receipt")["allowed"]
            )
            changed = copy.deepcopy(job)
            changed["command"] = "different command"
            self.assertFalse(cli.evaluate_trust(changed, plan, config)["allowed"])

    def test_public_projection_hides_workspace_identity(self):
        result = cli.public_job_view(
            {
                "id": "task",
                "workspace_id": "private-id",
                "workspace_name": "private-name",
                "session_id": "private-session",
                "lineage": {"parent": "private"},
                "execution_contract": "workspace",
            }
        )
        self.assertNotIn("private", json.dumps(result))


class AdapterTests(unittest.TestCase):
    def test_sleep_is_not_task_completion(self):
        provider = SpritesProvider({"organization": "example"})
        with patch.object(
            provider,
            "request",
            return_value=b'{"name":"task","id":"id1","organization":"example","status":"cold"}',
        ):
            self.assertEqual(provider.get("task")["status"], "cold")

    def test_identity_mismatch(self):
        provider = SpritesProvider({"organization": "example"})
        with patch.object(
            provider,
            "request",
            return_value=b'{"name":"task","id":"id1","organization":"another"}',
        ):
            with self.assertRaises(SpriteError):
                provider.get("task")
        for value in ("../other", "name?token=secret", "", "a/b"):
            with self.assertRaises(ValueError):
                segment(value)

    def test_stream_requires_completion(self):
        for value in (
            b"",
            b'{"type":"info"}',
            b'{"type":"error"}\n{"type":"complete"}',
        ):
            with self.assertRaises(SpriteError):
                completed_stream(value)
        self.assertEqual(
            completed_stream(b'{"type":"complete"}')[0]["type"], "complete"
        )

    def test_delete_is_idempotent_only_for_absence(self):
        provider = SpritesProvider()
        with patch.object(provider, "request", side_effect=SpriteError("absent", 404)):
            provider.delete("task")
        with patch.object(
            provider, "request", side_effect=SpriteError("unauthorized", 403)
        ):
            with self.assertRaises(SpriteError):
                provider.delete("task")

    def test_disconnected_session_has_bounded_lifetime(self):
        provider = SpritesProvider()

        class Connection:
            messages = iter(['{"type":"session_info","session_id":"123"}', b'\x01ELSEWHERE_WORKSPACE_READY\n'])

            def recv(self):
                return next(self.messages)

            def close(self):
                pass

        with (
            patch.dict(os.environ, {"SPRITES_TOKEN": "test-secret"}),
            patch("websocket.create_connection", return_value=Connection()) as connect,
        ):
            self.assertEqual(provider.start("task", ["echo", "ok"], 90), "123")
            self.assertIn("max_run_after_disconnect=90s", connect.call_args.args[0])
            self.assertNotIn("test-secret", connect.call_args.args[0])

    def test_session_identity_without_runner_ready_is_not_detachable(self):
        connection = unittest.mock.Mock()
        connection.recv.side_effect = ['{"type":"session_info","session_id":"123"}', b'']
        with patch.dict(os.environ, {"SPRITES_TOKEN": "test-secret"}), patch("websocket.create_connection", return_value=connection):
            with self.assertRaisesRegex(SpriteError, "uncertain"):
                SpritesProvider().start("task", ["python3", "runner.py"], 90)
        connection.close.assert_called_once()


class RunnerJourneyTests(unittest.TestCase):
    def test_real_process_source_results_and_duplicate_prevention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "hello.txt").write_text("before")
            (source / ".env").write_text("EXCLUDED_SECRET=yes")
            bundle, manifest = package_source(str(source), "test")
            task = root / "task"
            task.mkdir()
            shutil.move(bundle, task / "source.tar.gz")
            spec = {
                "id": "test",
                "source_fingerprint": manifest["content_sha256"],
                "lineage": {"derivation": "repository-snapshot"},
                "command": "printf after > hello.txt; printf done",
                "result_paths": ["hello.txt"],
                "max_runtime_seconds": 5,
                "exclusions": {
                    "paths": list(SOURCE_EXCLUDES),
                    "names": list(SOURCE_EXCLUDE_NAMES),
                    "suffixes": list(SOURCE_EXCLUDE_SUFFIXES),
                },
                "activity_commands": {"start": ["true"], "stop": ["true"]},
            }
            (task / "spec.json").write_text(json.dumps(spec))
            runner = Path(workspaces.__file__).with_name("workspace_runner.py")
            command = [sys.executable, str(runner), str(task / "spec.json")]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            result = inspect_result_bundle(task / "result.tar.gz", root / "recovered")
            self.assertEqual(result["stdout"], "done")
            self.assertEqual((root / "recovered/files/hello.txt").read_text(), "after")
            self.assertFalse((root / "recovered/files/.env").exists())
            self.assertEqual(
                subprocess.run(command, capture_output=True).returncode, 73
            )
            self.assertEqual((source / "hello.txt").read_text(), "before")
            self.assertEqual(
                fingerprint(spec["lineage"]),
                fingerprint(
                    json.loads((root / "recovered/manifest.json").read_text())[
                        "lineage"
                    ]
                ),
            )


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = {
            "id": "task",
            "provider": "sprites",
            "workspace_id": "immutable",
            "workspace_name": "example",
            "approval_receipt": "receipt",
            "plan": {},
            "state": "succeeded",
            "workspace": request(),
            "result": {"state": "collected", "activity_released": True},
            "session_id": "session",
        }
        self.provider = unittest.mock.Mock()
        self.provider.get.return_value = {"id": "immutable"}
        self.require = unittest.mock.Mock()

        def update(_id, **values):
            self.job.update(values)

        self.facade = SimpleNamespace(
            require_trust=self.require,
            update_job=update,
            find_job=lambda _: self.job,
            result_cache_path=lambda _: Path(self.tmp.name),
        )
        self.patch = patch.object(workspaces, "adapter", return_value=self.provider)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_repeated_cleanup_requires_verified_absence(self):
        self.provider.get.side_effect = [{"id": "immutable"}, None, None]
        self.assertEqual(
            workspaces.action(self.facade, self.job, "cleanup", {})["retention_state"],
            "deleted",
        )
        workspaces.action(self.facade, self.job, "cleanup", {})
        self.provider.delete.assert_called_once_with("example")

    def test_cleanup_preserves_unrecovered_results(self):
        self.job["result"] = {}
        with self.assertRaisesRegex(ValueError, "recover verified"):
            workspaces.action(self.facade, self.job, "cleanup", {})
        self.provider.delete.assert_not_called()

    def test_preparation_failure_can_clean_without_inventing_results(self):
        self.job.update(
            result={}, submission_phase="preparing", state="submission_uncertain"
        )
        self.provider.get.side_effect = [{"id": "immutable"}, None]
        result = workspaces.action(self.facade, self.job, "cleanup", {})
        self.assertEqual(result["retention_state"], "deleted")
        self.assertEqual(result["result"]["state"], "not-started")

    def test_discard_requires_cancellation_of_active_work(self):
        self.job.update(
            result={}, submission_phase="session_identified", state="running"
        )
        with self.assertRaisesRegex(ValueError, "cancel active work"):
            workspaces.action(
                self.facade, self.job, "cleanup", {}, discard_results=True
            )
        self.provider.delete.assert_not_called()

    def test_project_never_deleted(self):
        self.job["workspace"]["intent"] = "project"
        with self.assertRaisesRegex(ValueError, "cannot delete project"):
            workspaces.action(self.facade, self.job, "cleanup", {})
        self.provider.delete.assert_not_called()

    def test_reused_name_cannot_be_deleted(self):
        self.provider.get.return_value = {"id": "replacement"}
        with self.assertRaisesRegex(ValueError, "different immutable"):
            workspaces.action(self.facade, self.job, "cleanup", {})
        self.provider.delete.assert_not_called()

    def test_expired_task_retention_deletes_only_after_verified_recovery(self):
        self.job["workspace"]["retention"] = "keep"
        self.job["retention_expires_at"] = 1
        self.job["plan"] = {"workspace_boundary": {"retention_expiry": "delete-task-workspace-after-recovery"}}
        self.provider.get.side_effect = [{"id": "immutable"}, None]
        result = workspaces.action(self.facade, self.job, "cleanup", {})
        self.assertEqual(result["retention_state"], "deleted")
        self.provider.delete.assert_called_once_with("example")

    def test_transfer_to_project_revokes_task_deletion(self):
        self.job["workspace_transferred"] = True
        with self.assertRaisesRegex(ValueError, "transferred"):
            workspaces.action(self.facade, self.job, "cleanup", {})
        self.provider.delete.assert_not_called()

    def test_sleep_and_keep_do_not_claim_deletion(self):
        for retention, expected in (
            ("sleep", "idle-pause-allowed"),
            ("keep", "retained"),
        ):
            self.job["workspace"]["retention"] = retention
            self.assertEqual(
                workspaces.action(self.facade, self.job, "cleanup", {})[
                    "retention_state"
                ],
                expected,
            )
        self.provider.delete.assert_not_called()

    def test_uncertain_checkpoint_is_not_repeated(self):
        self.job["workspace"]["retention"] = "checkpoint"
        self.job["retention_state"] = "checkpointing"
        with self.assertRaisesRegex(ValueError, "uncertain"):
            workspaces.action(self.facade, self.job, "cleanup", {})
        self.provider.checkpoint.assert_not_called()

    def test_unreleased_activity_blocks_sleep(self):
        self.job["workspace"]["retention"] = "sleep"
        self.job["result"]["activity_released"] = False
        with self.assertRaisesRegex(ValueError, "activity release"):
            workspaces.action(self.facade, self.job, "cleanup", {})

    def test_cancel_targets_only_session(self):
        self.job["result"] = {}
        workspaces.action(self.facade, self.job, "cancel", {})
        self.provider.cancel.assert_called_once_with("example", "session")
        self.provider.delete.assert_not_called()
        self.assertEqual(self.job["state"], "cancelled")

    def test_absence_never_becomes_success(self):
        self.provider.get.return_value = None
        self.job["state"] = "running"
        self.job["result"] = {}
        with self.assertRaisesRegex(ValueError, "not completion"):
            workspaces.action(self.facade, self.job, "status", {})
        self.assertEqual(self.job["state"], "running")

    def test_missing_result_after_deadline_is_unknown_not_success(self):
        self.job.update(
            result={},
            remote_root="/task",
            created_at=1,
            max_runtime_seconds=60,
            state="running",
        )
        self.provider.read.side_effect = SpriteError("missing", 404)
        workspaces.action(self.facade, self.job, "status", {})
        self.assertEqual(self.job["state"], "outcome_unknown")

    def test_verified_results_survive_provider_deletion(self):
        self.job["retention_state"] = "deleted"
        self.require.side_effect = SystemExit("expired")
        self.assertIs(workspaces.action(self.facade, self.job, "results", {}), self.job)
        self.provider.get.assert_not_called()

    def test_revoked_trust_blocks_remote_access(self):
        self.require.side_effect = SystemExit("trust mismatch")
        with self.assertRaises(SystemExit):
            workspaces.action(self.facade, self.job, "cleanup", {})
        self.provider.get.assert_not_called()

    def test_connector_policy_drift_rejected(self):
        provider = SpritesProvider()
        with patch.object(
            provider,
            "request",
            return_value=b'[{"id":"example","provider":"github","access_policy":{"allow_all":true},"credential":"never-project"}]',
        ):
            with self.assertRaisesRegex(SpriteError, "differ"):
                provider.verify_connectors([])
            provider.verify_connectors(
                [
                    {
                        "id": "example",
                        "provider": "github",
                        "access_policy": {"allow_all": True},
                    }
                ]
            )

    def test_live_connector_inventory_envelope(self):
        provider = SpritesProvider()
        with patch.object(provider, "request", return_value=b'{"connections": []}'):
            provider.verify_connectors([])
        with patch.object(provider, "request", return_value=b'{"connections": [], "has_more": true}'):
            with self.assertRaisesRegex(SpriteError, "schema"):
                provider.verify_connectors([])


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "file.txt").write_text("reviewed source")
        self.config = cli.default_config()
        self.config["providers"]["sprites"] = {
            "enabled": True,
            "organization": "example",
        }
        self.config["trust"].update(
            approved=True,
            expires_at="2099-01-01T00:00:00+00:00",
            source={
                "allow_private": True,
                "allow_uncommitted": True,
                "allowed_roots": [str(self.source)],
            },
            providers={
                "sprites": {
                    "identity": SpritesProvider().identity(
                        self.config["providers"]["sprites"]
                    ),
                    "regions": [],
                }
            },
            artifact_store=cli.artifact_store_identity(self.config),
            limits={
                "max_cpu": 8,
                "max_memory_mb": 8192,
                "max_runtime_seconds": 3600,
                "max_estimated_cost_usd": 1,
            },
        )
        self.args = SimpleNamespace(
            provider="sprites",
            source_path=str(self.source),
            git_url=None,
            git_ref=None,
            image=None,
            max_runtime_seconds=60,
            estimated_cost_usd=0.1,
            workload_command="true",
            workload="test",
            result_path=[],
            approval_receipt=None,
        )
        self.provider = SpritesProvider(self.config["providers"]["sprites"])
        self.inventory = {}

        def create(name):
            value = {"id": "immutable-" + name, "name": name}
            self.inventory[name] = value
            return value

        self.provider.get = unittest.mock.Mock(
            side_effect=lambda name: self.inventory.get(name)
        )
        self.provider.create = unittest.mock.Mock(side_effect=create)
        self.provider.policy = unittest.mock.Mock(
            return_value=request()["network_policy"]
        )
        self.provider.set_policy = unittest.mock.Mock()
        self.provider.privileges = unittest.mock.Mock(return_value={})
        self.provider.verify_connectors = unittest.mock.Mock()
        self.provider.write = unittest.mock.Mock()
        self.provider.start = unittest.mock.Mock(return_value="session")
        for context in (
            patch.object(workspaces, "adapter", return_value=self.provider),
            patch.object(cli, "load_config", return_value=self.config),
            patch.dict(
                os.environ, {"AGENT_CAPACITY_JOBS": str(self.root / "jobs.json")}
            ),
        ):
            context.start()
            self.addCleanup(context.stop)

    def planned(self, spec=None):
        job, plan = workspaces.plan(cli, spec or request(), self.args, self.config)
        self.config["trust"]["workspace_boundaries"] = [plan["workspace_boundary"]]
        return job

    def test_corrupt_ownership_ledger_blocks_new_workspace(self):
        (self.root / "jobs.json").write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "ownership ledger"):
            workspaces.execute(cli, self.planned(), self.config, None)
        self.provider.create.assert_not_called()
        self.assertEqual((self.root / "jobs.json").read_text(), "corrupt")

    def test_unapproved_dispatch_creates_nothing(self):
        job = self.planned()
        self.config["trust"]["workspace_boundaries"] = []
        with self.assertRaises(SystemExit):
            workspaces.execute(cli, job, self.config, None)
        self.provider.create.assert_not_called()
        self.provider.write.assert_not_called()

    def test_changed_source_requires_new_approval_before_creation(self):
        job = self.planned()
        (self.source / "file.txt").write_text("changed after approval")
        with self.assertRaisesRegex(ValueError, "approved fingerprint"):
            workspaces.execute(cli, job, self.config, None)
        self.provider.create.assert_not_called()
        self.provider.write.assert_not_called()

    def test_privilege_drift_blocks_source_transfer(self):
        self.provider.privileges.return_value = {"unexpected": True}
        with self.assertRaisesRegex(ValueError, "privilege policy"):
            workspaces.execute(cli, self.planned(), self.config, None)
        self.provider.write.assert_not_called()

    def test_snapshot_tasks_have_distinct_workspaces_and_same_lineage(self):
        first = workspaces.execute(cli, self.planned(), self.config, None)
        second = workspaces.execute(cli, self.planned(), self.config, None)
        self.assertNotEqual(first["workspace_name"], second["workspace_name"])
        self.assertNotEqual(first["remote_root"], second["remote_root"])
        self.assertEqual(
            first["lineage"]["source_fingerprint"],
            second["lineage"]["source_fingerprint"],
        )
        self.assertEqual(first["lineage"]["derivation"], "repository-snapshot")
        self.assertEqual(first["state"], "running")
        self.assertNotIn("result", first)

    def test_existing_project_claim_prevents_concurrent_mutation(self):
        self.inventory["project"] = {"id": "project-id", "name": "project"}
        spec = {
            **request(),
            "intent": "project",
            "project": {"name": "project", "id": "project-id"},
            "retention": "keep",
            "retention_seconds": 600,
        }
        workspaces.execute(cli, self.planned(spec), self.config, None)
        with self.assertRaisesRegex(ValueError, "unresolved task"):
            workspaces.execute(cli, self.planned(spec), self.config, None)
        self.provider.create.assert_not_called()
        self.provider.start.assert_called_once()

    def test_ambiguous_submission_is_durable_without_retry(self):
        self.provider.start.side_effect = SpriteError("uncertain")
        job = self.planned()
        with self.assertRaises(SpriteError):
            workspaces.execute(cli, job, self.config, None)
        retained = cli.find_job(job["id"])
        self.assertEqual(retained["state"], "submission_uncertain")
        self.assertTrue(retained["workspace_id"])
        self.provider.start.assert_called_once()

    def test_project_policy_drift_stops_source_transfer(self):
        self.inventory["project"] = {"id": "project-id", "name": "project"}
        self.provider.policy.return_value = {
            "rules": [{"domain": "*", "action": "allow"}]
        }
        spec = {
            **request(),
            "intent": "project",
            "project": {"name": "project", "id": "project-id"},
            "retention": "sleep",
            "retention_seconds": 600,
        }
        with self.assertRaisesRegex(ValueError, "policy differs"):
            workspaces.execute(cli, self.planned(spec), self.config, None)
        self.provider.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
