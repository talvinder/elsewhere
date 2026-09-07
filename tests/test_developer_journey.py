import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_capacity import cli
from agent_capacity.artifact_transport import package_source
from agent_capacity.journey import completion_receipt, source_fingerprint


class DeveloperJourneyTests(unittest.TestCase):
    def test_fingerprint_tracks_transported_changes_not_secrets_or_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "main.py"
            source.write_text("print(1)")
            (root / ".env").write_text("harmless-secret-marker")
            bundle, first = package_source(str(root), "first")
            bundle.unlink()
            os.utime(source, (1, 1))
            (root / ".env").write_text("changed-secret-marker")
            bundle, same = package_source(str(root), "same")
            bundle.unlink()
            self.assertEqual(first["content_sha256"], same["content_sha256"])
            self.assertEqual(first["content_sha256"], source_fingerprint(first["files"]))
            self.assertEqual(first["skipped"][0]["path"], ".env")
            source.write_text("print(2)")
            bundle, changed = package_source(str(root), "changed")
            bundle.unlink()
            self.assertNotEqual(first["content_sha256"], changed["content_sha256"])

    def test_receipt_retains_failed_result_after_cleanup_without_account_details(self):
        receipt = completion_receipt({
            "id": "test-job", "provider": "fly", "state": "cleaned", "provider_absent": True,
            "submitted_at": 100, "completed_at": 112,
            "result": {"state": "collected", "exit_code": 1, "local_path": "/tmp/result"},
            "source_artifact": {"url": "secret", "manifest": {"content_sha256": "abc"}},
            "estimated_cost_usd": 0.02,
        })
        self.assertEqual(receipt["exit_code"], 1)
        self.assertEqual(receipt["elapsed_seconds"], 12)
        self.assertTrue(receipt["cleanup_verified"])
        self.assertTrue(receipt["result_verified"])
        self.assertNotIn("secret", str(receipt))

    def test_denial_advises_placement_without_relaxing_admission(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "AGENT_CAPACITY_STATE": str(Path(directory) / "state.json"),
            "AGENT_CAPACITY_MEMORY_LEVEL": "10", "AGENT_CAPACITY_TOTAL_MB": "18432",
        }):
            code, value = cli.acquire("build", 1, "test", 60)
            self.assertEqual(code, 2)
            self.assertFalse(value["allowed"])
            self.assertEqual(value["placement_advice"]["action"], "assess_remote_placement")
            self.assertFalse(value["placement_advice"]["automatic_dispatch"])

    def test_wait_suppresses_unchanged_receipt_and_does_not_dispatch(self):
        job = {"id": "test-job", "provider": "local", "state": "running"}
        with patch.object(cli, "find_job", return_value=job), patch.object(cli, "execute_dispatch") as dispatch:
            first = cli.wait_for_job("test-job", 0)
            same = cli.wait_for_job("test-job", 0, first["cursor"])
            self.assertTrue(first["changed"])
            self.assertFalse(same["changed"])
            job["state"] = "failed"
            done = cli.wait_for_job("test-job", 0, same["cursor"])
            self.assertTrue(done["terminal"])
            self.assertTrue(done["changed"])
            dispatch.assert_not_called()

    def test_wait_rejects_unknown_job_and_unbounded_wait(self):
        with self.assertRaises(ValueError):
            cli.wait_for_job("test-job", 31)
        with patch.object(cli, "find_job", return_value=None), self.assertRaises(ValueError):
            cli.wait_for_job("missing", 0)


if __name__ == "__main__":
    unittest.main()
