import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_capacity import __version__
from agent_capacity.artifact_transport import prepare_source_artifact
from agent_capacity.cli import (
    append_job,
    attach_execution_artifacts,
    cleanup_source_artifact,
    execute_dispatch,
    find_job,
    public_job_view,
    public_provider_result,
    redact_value,
    refresh_remote_job,
    remote_command,
    run_job_action,
    sanitize_persisted_value,
    update_job,
)
from agent_capacity.models import should_accept_remote_transition
from agent_capacity.providers import get_provider


def job(provider: str) -> dict:
    config = (
        {"app": "example-app", "region": "bom", "region_fallbacks": []}
        if provider == "fly"
        else {"resource_group": "example-rg", "location": "centralindia", "subscription": "sub"}
    )
    return {
        "id": "job-1",
        "name": "ac-test-123",
        "provider": provider,
        "image": "example/image:latest",
        "cpu": 1,
        "memory_mb": 512,
        "remote_command": "echo ok",
        "plan": {"provider_config": config},
    }


class ProviderContractTests(unittest.TestCase):
    def test_approved_runtime_bounds_source_workload_and_result_finalization(self):
        command = remote_command(
            "printf ok",
            None,
            None,
            source_url="https://example.test/source?sig=secret",
            max_runtime_seconds=300,
            result_url="https://example.test/result?sig=secret",
            result_paths=["output/result.txt"],
            job_id="job-1",
        )
        self.assertTrue(command.startswith("command -v timeout"))
        self.assertIn("exec timeout -s TERM 300 /bin/sh -lc", command)
        self.assertIn("source?sig=secret", command)
        self.assertIn("result?sig=secret", command)
        self.assertLess(command.index("source?sig=secret"), command.index("result?sig=secret"))

    def test_public_lifecycle_views_omit_provider_account_and_command_details(self):
        value = job("azure")
        value.update({
            "provider_id": "/subscriptions/private/resourceGroups/personal/job",
            "remote_command": "curl https://example.test/result?sig=secret",
            "plan": {"provider_config": {"subscription": "private"}},
            "state": "running",
        })
        serialized = json.dumps(public_job_view(value))
        self.assertNotIn("subscriptions", serialized)
        self.assertNotIn("remote_command", serialized)
        provider_value = public_provider_result({
            "returncode": 0,
            "observation": {"state": "running", "provider_id": value["provider_id"]},
        })
        self.assertNotIn("provider_id", json.dumps(provider_value))

    def test_signed_source_url_is_removed_from_nested_persisted_evidence(self):
        secret = "https://storage.example/source.tgz?sig=secret"
        value = {
            "command": ["curl", secret],
            "attempts": [{"stdout": f"fetching {secret}", "stderr": secret}],
            "submission": {"stdout": secret},
        }
        redacted = redact_value(value, secret, "<redacted-source-url>")
        serialized = json.dumps(redacted)
        self.assertNotIn(secret, serialized)
        self.assertGreaterEqual(serialized.count("<redacted-source-url>"), 4)

    def test_credential_shaped_provider_errors_are_sanitized(self):
        value = sanitize_persisted_value({
            "stderr": "Bearer abcdefghijklmnopqrstuvwxyz AWS_SECRET_ACCESS_KEY=supersecret",
            "url": "https://storage.example/result.tgz?sp=cw&se=tomorrow&sig=secret",
        })
        serialized = json.dumps(value)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", serialized)
        self.assertNotIn("supersecret", serialized)
        self.assertNotIn("sig=secret", serialized)

    def test_s3_presigned_urls_are_sanitized_from_generic_evidence(self):
        secret = (
            "https://bucket.t3.storage.dev/results/job.tar.gz?"
            "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=supersecret"
        )
        serialized = json.dumps(sanitize_persisted_value({"stderr": f"upload failed: {secret}"}))
        self.assertNotIn("supersecret", serialized)
        self.assertIn("<redacted-signed-url>", serialized)

    def test_repository_versions_match_the_package_source(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual((root / "VERSION").read_text().strip(), __version__)
        project = (root / "pyproject.toml").read_text()
        self.assertIn('dynamic = ["version"]', project)
        self.assertIn('path = "src/agent_capacity/version.py"', project)
        plugin = json.loads((root / "plugins/elsewhere/.codex-plugin/plugin.json").read_text())
        self.assertEqual(plugin["version"], __version__)

    def test_every_provider_implements_the_lifecycle_contract(self):
        for name in ("fly", "azure"):
            provider = get_provider(name)
            for method in (
                "ready", "identity", "regions", "build_plan", "parse_submission",
                "status_command", "parse_status", "logs_command", "cancel_command",
                "cleanup_command", "classify_failure", "result_strategy",
            ):
                self.assertTrue(callable(getattr(provider, method)), f"{name}.{method}")

    def test_azure_source_failure_reports_unverified_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "README.md").write_text("safe\n")
            value = {"id": "job-azure", "source_path": str(source)}
            config = {
                "artifact_store": {
                    "provider": "azure-blob", "account": "example",
                    "container": "elsewhere-artifacts",
                }
            }
            uploaded = subprocess.CompletedProcess([], 0, "", "")
            with (
                patch("agent_capacity.artifact_transport.shutil.which", return_value="/usr/bin/az"),
                patch("agent_capacity.artifact_transport.subprocess.run", side_effect=[uploaded, uploaded]),
                patch(
                    "agent_capacity.artifact_transport.subprocess.check_output",
                    side_effect=RuntimeError("sas failed"),
                ),
                patch(
                    "agent_capacity.artifact_transport.cleanup_artifact",
                    return_value={"deleted": False, "verified_absent": False},
                ),
                self.assertRaisesRegex(RuntimeError, "cleanup could not be verified"),
            ):
                prepare_source_artifact(value, config)

    def test_ambiguous_azure_upload_failure_still_attempts_verified_rollback(self):
        value = job("azure")
        value["source_path"] = tempfile.gettempdir()
        config = {
            "artifact_store": {
                "provider": "azure-blob",
                "account": "example",
                "container": "sources",
                "subscription": "sub",
            }
        }
        upload_failure = subprocess.CalledProcessError(1, ["az", "storage", "blob", "upload"])
        with (
            patch("agent_capacity.artifact_transport.package_source") as package,
            patch(
                "agent_capacity.artifact_transport.subprocess.run",
                side_effect=[subprocess.CompletedProcess([], 0), upload_failure],
            ),
            patch(
                "agent_capacity.artifact_transport.cleanup_artifact",
                return_value={"verified_absent": True},
            ) as cleanup,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            package.return_value = (
                Path(tempfile.gettempdir()) / "missing-source-bundle.tar.gz",
                {"files": [], "skipped": [], "total_bytes": 0},
            )
            prepare_source_artifact(value, config)
        cleanup.assert_called_once()

    def test_fly_plan_retains_machine_until_explicit_cleanup(self):
        provider = get_provider("fly")
        value = job("fly")
        plan = provider.build_plan(value, value["plan"]["provider_config"])
        self.assertNotIn("--rm", plan["command"])
        self.assertIn("explicitly destroys", plan["cleanup"])

    def test_fly_status_and_logs_are_scoped_to_the_dispatched_machine(self):
        provider = get_provider("fly")
        value = job("fly")
        machine_id = "9080deadbeef12"
        value["provider_id"] = machine_id
        observation = provider.parse_status(
            '[{"id":"9080deadbeef12","name":"ac-test-123","state":"started","region":"bom"},'
            '{"id":"1111deadbeef22","name":"somebody-else","state":"started"}]',
            "", 0, value,
        )
        self.assertEqual(observation.state, "running")
        self.assertEqual(observation.provider_id, machine_id)
        self.assertEqual(provider.logs_command(value)[-4:-2], ["--machine", machine_id])

    def test_fly_absence_is_explicit_but_not_reported_as_success(self):
        observation = get_provider("fly").parse_status("[]", "", 0, job("fly"))
        self.assertTrue(observation.absent)
        self.assertIsNone(observation.state)

    def test_fly_status_ignores_a_stale_same_named_machine_and_matches_by_id(self):
        provider = get_provider("fly")
        value = job("fly")
        value["provider_id"] = "9080deadbeef12"
        # A leftover machine from a prior run shares the job name but has a different ID
        # and appears first in the app-wide list. Strict ID matching must skip it.
        stale_first = (
            '[{"id":"deadbeefdead11","name":"ac-test-123","state":"stopped","region":"bom"},'
            '{"id":"9080deadbeef12","name":"ac-test-123","state":"started","region":"bom"}]'
        )
        observation = provider.parse_status(stale_first, "", 0, value)
        self.assertEqual(observation.provider_id, "9080deadbeef12")
        self.assertEqual(observation.state, "running")

    def test_fly_status_reports_absent_when_the_dispatched_id_is_gone(self):
        provider = get_provider("fly")
        value = job("fly")
        value["provider_id"] = "9080deadbeef12"
        # Only a different, same-named machine remains; the dispatched one was destroyed.
        others = '[{"id":"deadbeefdead11","name":"ac-test-123","state":"started"}]'
        observation = provider.parse_status(others, "", 0, value)
        self.assertTrue(observation.absent)
        self.assertIsNone(observation.state)

    def test_fly_submission_prefers_structured_json_machine_id(self):
        machine_id = get_provider("fly").parse_submission(
            '{"id":"9080deadbeef12","name":"ac-test-123","state":"created"}', "", job("fly")
        )
        self.assertEqual(machine_id, "9080deadbeef12")

    def test_fly_submission_falls_back_to_text_scraping(self):
        machine_id = get_provider("fly").parse_submission(
            "Success! A Machine has been launched\nMachine ID: 9080deadbeef12\n", "", job("fly")
        )
        self.assertEqual(machine_id, "9080deadbeef12")

    def test_fly_job_specific_status_uses_exit_evidence_without_retaining_machine_config(self):
        provider = get_provider("fly")
        value = job("fly")
        value["provider_id"] = "9080deadbeef12"
        output = (
            "Machine ID: 9080deadbeef12\nState: stopped\nHostStatus: ok\n"
            " Region │ bom\n stopped │ exit │ flyd │ now │ exit_code=0,oom_killed=false\n"
        )
        observation = provider.parse_status(output, "", 0, value)
        self.assertEqual(observation.state, "succeeded")
        self.assertEqual(observation.evidence["exit_code"], 0)
        self.assertIn("9080deadbeef12", provider.status_command(value))
        destroyed = provider.parse_status(output.replace("stopped", "destroyed"), "", 0, value)
        self.assertTrue(destroyed.absent)


class PersistedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.jobs_path = Path(self.directory.name) / "jobs.json"
        self.old_jobs = os.environ.get("AGENT_CAPACITY_JOBS")
        os.environ["AGENT_CAPACITY_JOBS"] = str(self.jobs_path)

    def tearDown(self):
        if self.old_jobs is None:
            os.environ.pop("AGENT_CAPACITY_JOBS", None)
        else:
            os.environ["AGENT_CAPACITY_JOBS"] = self.old_jobs
        self.directory.cleanup()

    @staticmethod
    def completed(command, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def test_status_refresh_persists_provider_identity_and_normalized_state(self):
        value = job("fly")
        value["state"] = "submitted"
        append_job(value)
        listing = '[{"id":"9080deadbeef12","name":"ac-test-123","state":"started"}]'
        with patch(
            "agent_capacity.cli.subprocess.run",
            return_value=self.completed([], stdout=listing),
        ):
            current, _ = refresh_remote_job(value)
        self.assertEqual(current["state"], "running")
        self.assertEqual(current["provider_id"], "9080deadbeef12")
        persisted = json.loads(self.jobs_path.read_text())["jobs"][0]
        self.assertEqual(persisted["state"], "running")
        self.assertEqual(persisted["provider_id"], "9080deadbeef12")
        self.assertEqual(persisted["transitions"][-1]["to"], "running")

    def test_atomic_state_update_rejects_a_stale_backwards_transition(self):
        value = job("fly")
        value["state"] = "submitted"
        append_job(value)
        update_job(value["id"], state="running")
        update_job(value["id"], state="queued")
        self.assertEqual(find_job(value["id"])["state"], "running")

    def test_submission_redacts_signed_urls_and_credential_echoes_everywhere(self):
        secret = "https://storage.example/result.tgz?sp=cw&sig=secret"
        value = job("fly")
        value.update({
            "state": "planned", "created_at": 1, "result_artifact": {
                "provider": "azure-blob", "account": "example", "container": "results",
                "blob": "results/job-1.tar.gz", "url": secret,
            },
            "remote_command": f"curl {secret}", "fallback_providers": [],
        })
        plan = {
            "provider": "fly", "provider_config": value["plan"]["provider_config"],
            "command": ["fly", "machine", "run", f"curl {secret}"],
        }
        completed = self.completed(
            plan["command"], stdout="Machine 9080deadbeef12 created",
            stderr=f"Bearer abcdefghijklmnopqrstuvwxyz {secret}",
        )
        with (
            patch("agent_capacity.cli.load_config", return_value={"providers": {"fly": {}}}),
            patch("agent_capacity.cli.evaluate_trust", return_value={"allowed": True}),
            patch("agent_capacity.cli.subprocess.run", return_value=completed),
        ):
            code, _ = execute_dispatch(value, plan, "receipt")
        self.assertEqual(code, 0)
        serialized = self.jobs_path.read_text()
        self.assertNotIn("sig=secret", serialized)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", serialized)
        self.assertIn("<redacted-result-url>", serialized)

    def test_retryable_submission_is_reconciled_before_any_fallback(self):
        value = job("fly")
        value.update({"state": "planned", "created_at": 1, "fallback_providers": []})
        plan = {
            "provider": "fly", "provider_config": value["plan"]["provider_config"],
            "command": ["fly", "machine", "run"],
        }
        responses = [
            self.completed(plan["command"], returncode=1, stderr="request timeout"),
            self.completed([], stdout='[{"id":"9080deadbeef12","name":"ac-test-123","state":"started"}]'),
        ]
        with (
            patch("agent_capacity.cli.load_config", return_value={"providers": {"fly": {}}}),
            patch("agent_capacity.cli.evaluate_trust", return_value={"allowed": True}),
            patch("agent_capacity.cli.subprocess.run", side_effect=responses) as runner,
        ):
            code, _ = execute_dispatch(value, plan, "receipt")
        self.assertEqual(code, 0)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(find_job(value["id"])["state"], "running")
        self.assertEqual(find_job(value["id"])["provider_id"], "9080deadbeef12")
        self.assertTrue(find_job(value["id"])["submission_reconciled"])

    def test_final_submission_failure_rolls_back_prepared_artifacts(self):
        value = job("fly")
        value.update({
            "state": "planned", "created_at": 1, "fallback_providers": [],
            "result_artifact": {
                "provider": "azure-blob", "account": "example", "container": "results",
                "blob": "results/job-1.tar.gz", "url": "https://example.test/?sig=secret",
            },
        })
        plan = {
            "provider": "fly", "provider_config": value["plan"]["provider_config"],
            "command": ["fly", "machine", "run"],
        }
        with (
            patch("agent_capacity.cli.load_config", return_value={"providers": {"fly": {}}}),
            patch("agent_capacity.cli.evaluate_trust", return_value={"allowed": True}),
            patch(
                "agent_capacity.cli.cleanup_source_artifact",
                return_value={"deleted": True, "verified_absent": True},
            ) as cleanup,
            patch(
                "agent_capacity.cli.subprocess.run",
                return_value=self.completed(plan["command"], returncode=1, stderr="invalid image"),
            ),
        ):
            code, _ = execute_dispatch(value, plan, "receipt")
        self.assertEqual(code, 1)
        self.assertEqual(cleanup.call_count, 1)
        self.assertTrue(find_job(value["id"])["submission_cleanup"]["result_artifact"]["deleted"])

    def test_artifact_preparation_failure_reports_unverified_rollback(self):
        value = job("fly")
        value.update({"source_path": "/tmp/source", "result_paths": []})
        config = {"providers": {"fly": {}}}
        source = {"provider": "tigris", "bucket": "bucket", "key": "sources/job-1.tar.gz"}
        with (
            patch("agent_capacity.cli.prepare_source_artifact", return_value=source),
            patch("agent_capacity.cli.prepare_result_artifact", side_effect=RuntimeError("presign failed")),
            patch(
                "agent_capacity.cli.cleanup_source_artifact",
                return_value={"deleted": False, "verified_absent": False},
            ),
            self.assertRaisesRegex(RuntimeError, "rollback could not be verified"),
        ):
            attach_execution_artifacts(value, "echo ok", 600, config, None)

    def test_cleanup_is_idempotent_and_only_finishes_after_absence_is_verified(self):
        value = job("fly")
        value.update({"state": "completed", "provider_id": "9080deadbeef12"})
        append_job(value)
        present = '[{"id":"9080deadbeef12","name":"ac-test-123","state":"stopped"}]'
        responses = [
            self.completed([], stdout=present),
            self.completed([], stdout="Machine destroyed"),
            self.completed([], stdout="[]"),
        ]
        with patch("agent_capacity.artifact_transport.subprocess.run", side_effect=responses) as runner:
            with contextlib.redirect_stdout(io.StringIO()):
                code = run_job_action(value["id"], "cleanup")
        self.assertEqual(code, 0)
        self.assertEqual(find_job(value["id"])["state"], "cleaned")
        self.assertTrue(find_job(value["id"])["cleanup"]["compute"]["verified_absent"])
        self.assertIn("destroy", runner.call_args_list[1].args[0])

        with patch(
            "agent_capacity.cli.subprocess.run",
            return_value=self.completed([], stdout="[]"),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                repeated_code = run_job_action(value["id"], "cleanup")
        self.assertEqual(repeated_code, 0)
        self.assertEqual(find_job(value["id"])["state"], "cleaned")

    def test_cleanup_does_not_delete_compute_before_terminal_results_are_collected(self):
        value = job("fly")
        value.update({
            "state": "completed", "provider_id": "9080deadbeef12",
            "result_artifact": {"provider": "azure-blob", "blob": "results/job-1.tar.gz"},
        })
        append_job(value)
        present = '[{"id":"9080deadbeef12","name":"ac-test-123","state":"stopped"}]'
        with (
            patch("agent_capacity.cli.subprocess.run", return_value=self.completed([], stdout=present)) as runner,
            patch(
                "agent_capacity.cli.collect_result_artifact",
                return_value=(False, {"state": "pending", "error": "not uploaded yet"}),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = run_job_action(value["id"], "cleanup")
        self.assertEqual(code, 1)
        self.assertEqual(find_job(value["id"])["state"], "completed")
        self.assertIn("cleanup_blocked", find_job(value["id"]))
        self.assertEqual(runner.call_count, 1)

    def test_cleanup_can_explicitly_discard_unavailable_results(self):
        value = job("fly")
        value.update({
            "state": "completed", "provider_id": "9080deadbeef12",
            "result_artifact": {
                "provider": "azure-blob", "account": "example", "container": "results",
                "blob": "results/job-1.tar.gz",
            },
        })
        append_job(value)
        present = '[{"id":"9080deadbeef12","name":"ac-test-123","state":"stopped"}]'
        responses = [
            self.completed([], stdout=present), self.completed([], stdout="Machine destroyed"),
            self.completed([], stdout="[]"),
        ]
        with (
            patch("agent_capacity.cli.subprocess.run", side_effect=responses),
            patch("agent_capacity.cli.collect_result_artifact", return_value=(False, {"state": "pending"})),
            patch(
                "agent_capacity.cli.cleanup_source_artifact",
                return_value={"deleted": True, "verified_absent": True},
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = run_job_action(value["id"], "cleanup", discard_results=True)
        self.assertEqual(code, 0)
        self.assertEqual(find_job(value["id"])["state"], "cleaned")
        self.assertEqual(find_job(value["id"])["result"]["state"], "discarded")

    def test_cleanup_refuses_to_terminalize_a_running_job(self):
        value = job("fly")
        value.update({"state": "running", "provider_id": "9080deadbeef12"})
        append_job(value)
        running = '[{"id":"9080deadbeef12","name":"ac-test-123","state":"started"}]'
        with (
            patch("agent_capacity.cli.subprocess.run", return_value=self.completed([], stdout=running)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = run_job_action(value["id"], "cleanup")
        self.assertEqual(code, 1)
        self.assertEqual(find_job(value["id"])["state"], "running")

    def test_cleanup_does_not_trust_unverified_artifact_deletion(self):
        value = job("fly")
        value.update({
            "state": "completed",
            "provider_id": "9080deadbeef12",
            "source_artifact": {
                "provider": "tigris", "bucket": "example", "key": "sources/job.tar.gz"
            },
        })
        append_job(value)
        absent = self.completed([], returncode=1, stderr="machine not found")
        with (
            patch("agent_capacity.cli.subprocess.run", return_value=absent),
            patch(
                "agent_capacity.cli.cleanup_source_artifact",
                return_value={"deleted": True, "verified_absent": False},
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = run_job_action(value["id"], "cleanup")
        self.assertEqual(code, 1)
        self.assertEqual(find_job(value["id"])["state"], "cleanup_failed")

    def test_source_artifact_cleanup_requires_a_verified_absent_blob(self):
        artifact = {
            "provider": "azure-blob", "account": "example", "container": "sources",
            "blob": "sources/job-1.tar.gz",
        }
        config = {"artifact_store": {"subscription": "sub"}}
        responses = [
            self.completed([], stdout=""),
            self.completed([], stdout="false\n"),
        ]
        with patch("agent_capacity.cli.subprocess.run", side_effect=responses) as runner:
            result = cleanup_source_artifact(artifact, config)
        self.assertTrue(result["deleted"])
        self.assertTrue(result["verified_absent"])
        self.assertIn("delete", runner.call_args_list[0].args[0])
        self.assertIn("exists", runner.call_args_list[1].args[0])

        responses = [
            self.completed([], stdout=""),
            self.completed([], stdout="true\n"),
        ]
        with patch("agent_capacity.artifact_transport.subprocess.run", side_effect=responses):
            unverified = cleanup_source_artifact(artifact, config)
        self.assertFalse(unverified["deleted"])
        self.assertFalse(unverified["verified_absent"])

        responses = [
            self.completed([], returncode=3, stderr="ErrorCode:BlobNotFound"),
            self.completed([], stdout="false\n"),
        ]
        with patch("agent_capacity.artifact_transport.subprocess.run", side_effect=responses):
            repeated = cleanup_source_artifact(artifact, config)
        self.assertTrue(repeated["deleted"])
        self.assertTrue(repeated["already_absent"])

    def test_azure_exit_code_controls_terminal_outcome(self):
        provider = get_provider("azure")
        value = job("azure")
        succeeded = provider.parse_status(
            '{"id":"azure-id","containers":[{"instanceView":{"currentState":'
            '{"state":"Terminated","exitCode":0}}}]}', "", 0, value,
        )
        failed = provider.parse_status(
            '{"id":"azure-id","containers":[{"instanceView":{"currentState":'
            '{"state":"Terminated","exitCode":17}}}]}', "", 0, value,
        )
        self.assertEqual(succeeded.state, "succeeded")
        self.assertEqual(failed.state, "failed")

    def test_azure_provisioning_accepts_null_instance_view(self):
        observation = get_provider("azure").parse_status(
            '{"containers":[{"instanceView":null}],"provisioningState":"Creating"}',
            "", 0, job("azure"),
        )
        self.assertEqual(observation.state, "queued")
        provisioned = get_provider("azure").parse_status(
            '{"containers":[{"instanceView":null}],"provisioningState":"Succeeded"}',
            "", 0, job("azure"),
        )
        self.assertEqual(provisioned.state, "queued")

    def test_azure_not_found_is_cleanup_evidence(self):
        observation = get_provider("azure").parse_status(
            "", "ResourceNotFound: container group could not be found", 3, job("azure")
        )
        self.assertTrue(observation.absent)

    def test_failure_classification_is_provider_specific_and_normalized(self):
        self.assertEqual(get_provider("fly").classify_failure("no capacity available"), "retryable")
        self.assertEqual(get_provider("fly").classify_failure("permission denied"), "terminal")
        self.assertEqual(get_provider("fly").classify_failure(
            "Region xyz is deprecated and cannot have new resources provisioned."
        ), "retryable")
        self.assertEqual(get_provider("fly").classify_failure("deprecated API; permission denied"), "terminal")
        self.assertEqual(get_provider("azure").classify_failure("AllocationFailed"), "retryable")
        self.assertEqual(get_provider("azure").classify_failure("RegistryErrorResponse; retry later"), "retryable")
        self.assertEqual(get_provider("azure").classify_failure("InvalidImage"), "terminal")

    def test_all_adapters_scope_control_and_use_verified_remote_results(self):
        fly = job("fly")
        fly["provider_id"] = "9080deadbeef12"
        azure = job("azure")
        for value in (fly, azure):
            provider = get_provider(value["provider"])
            self.assertEqual(provider.result_strategy(value), "remote-upload")
            self.assertEqual(provider.classify_failure("request timeout"), "retryable")
            identity = value.get("provider_id") or value["name"]
            self.assertIn(identity, " ".join(provider.logs_command(value)))
            self.assertIn(identity, " ".join(provider.cancel_command(value)))
            self.assertIn(identity, " ".join(provider.cleanup_command(value)))

    def test_stale_provider_observations_cannot_move_a_job_backwards(self):
        self.assertFalse(should_accept_remote_transition("running", "queued"))
        self.assertFalse(should_accept_remote_transition("succeeded", "running"))
        self.assertTrue(should_accept_remote_transition("submitted", "running"))


if __name__ == "__main__":
    unittest.main()
