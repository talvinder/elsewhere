"""The completion receipt must not lose evidence it has already established.

Each test drives the real code path and asserts the receipt a developer would be
shown, because these regressions are invisible in the job state alone: the job
stays terminal while the receipt quietly downgrades to unverified.
"""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_capacity import cli
from agent_capacity.journey import completion_receipt


def terminal_fly_job(**overrides) -> dict:
    now = int(time.time())
    job = {
        "id": "job-r1", "name": "ac-receipt-1", "provider": "fly", "owner": "test",
        "workload": "test", "state": "cleaned", "provider_absent": True,
        "provider_id": "machine-1", "submitted_at": now - 300, "started_at": now - 290,
        "completed_at": now - 100, "cleaned_at": now - 50,
        "cpu": 1, "memory_mb": 1024, "max_runtime_seconds": 600,
        "estimated_cost_usd": 0.05,
        "source_artifact": {"manifest": {
            "content_sha256": "a" * 64, "file_count": 3, "skipped_count": 0,
        }},
        "result": {"state": "collected", "exit_code": 0, "local_path": "/tmp/absent"},
        "transitions": [],
        "plan": {"provider_config": {"app": "example-app", "region": "bom"}},
    }
    job.update(overrides)
    return job


class ReceiptIntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        for name, value in (
            ("AGENT_CAPACITY_JOBS", root / "jobs.json"),
            ("AGENT_CAPACITY_STATE", root / "leases.json"),
        ):
            patcher = patch.dict(os.environ, {name: str(value)})
            patcher.start()
            self.addCleanup(patcher.stop)

    def store(self, job: dict) -> None:
        Path(os.environ["AGENT_CAPACITY_JOBS"]).write_text(
            json.dumps({"version": 1, "jobs": [job]})
        )

    def test_failed_status_probe_keeps_verified_cleanup(self) -> None:
        """A network timeout is not evidence that destroyed compute came back."""
        self.store(terminal_fly_job())
        self.assertTrue(completion_receipt(cli.find_job("job-r1"))["cleanup_verified"])

        timeout = subprocess.CompletedProcess(
            ["fly"], 1, "", "Error: dial tcp 1.2.3.4:443: i/o timeout"
        )
        with patch.object(cli.subprocess, "run", return_value=timeout):
            cli.refresh_remote_job(cli.find_job("job-r1"))

        refreshed = cli.find_job("job-r1")
        self.assertEqual(refreshed["state"], "cleaned")
        self.assertIs(refreshed["provider_absent"], True)
        self.assertTrue(completion_receipt(refreshed)["cleanup_verified"])

    def test_authoritative_presence_still_recorded(self) -> None:
        """A successful probe that finds a live machine must still be believed."""
        self.store(terminal_fly_job(state="running", provider_absent=False,
                                    completed_at=None, cleaned_at=None))
        running = subprocess.CompletedProcess(["fly"], 0, "State: started\n", "")
        with patch.object(cli.subprocess, "run", return_value=running):
            cli.refresh_remote_job(cli.find_job("job-r1"))
        self.assertIs(cli.find_job("job-r1")["provider_absent"], False)

    def test_failed_cancel_keeps_completion_time(self) -> None:
        """update_job refuses the rejected state change but applies other keys."""
        self.store(terminal_fly_job(id="job-r1", state="succeeded",
                                    provider_absent=False, cleaned_at=None))
        before = completion_receipt(cli.find_job("job-r1"))
        self.assertIsNotNone(before["elapsed_seconds"])

        def provider_call(command, *args, **kwargs):
            text = " ".join(map(str, command))
            # Cancel succeeds, but the machine never disappears, so absence
            # verification reports the job as not cancelled.
            if "status" in text:
                return subprocess.CompletedProcess(command, 0, "State: stopped\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(cli.subprocess, "run", side_effect=provider_call):
            cli.run_job_action("job-r1", "cancel")

        after = completion_receipt(cli.find_job("job-r1"))
        self.assertEqual(after["state"], "succeeded")
        self.assertEqual(after["elapsed_seconds"], before["elapsed_seconds"])
        self.assertIsNotNone(cli.find_job("job-r1")["completed_at"])

    def test_job_wait_is_not_advertised_as_read_only(self) -> None:
        """Waiting reconciles and persists provider state, so it is not read-only."""
        tool = next(t for t in cli.mcp_tools() if t["name"] == "elsewhere_job_wait")
        self.assertIs(tool["annotations"]["readOnlyHint"], False)
        self.assertIs(tool["annotations"]["idempotentHint"], False)


if __name__ == "__main__":
    unittest.main()
