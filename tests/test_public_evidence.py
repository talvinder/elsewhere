import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/export_public_run_evidence.py"
SPEC = importlib.util.spec_from_file_location("export_public_run_evidence", SCRIPT)
assert SPEC and SPEC.loader
public_evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(public_evidence)


def cleaned_job():
    return {
        "id": "private-job-id",
        "provider": "fly",
        "state": "cleaned",
        "provider_absent": True,
        "estimated_cost_usd": 0.02,
        "cleaned_at": 1786492800,
        "plan": {"provider_config": {"region": "iad", "app": "private-app"}},
        "provider_id": "private-machine-id",
        "command": "private command",
        "result_artifact": {
            "provider": "tigris", "bucket": "private-bucket", "key": "private-key"
        },
        "result": {
            "state": "collected", "exit_code": 0, "bundle_sha256": "a" * 64,
            "stdout": "private output", "local_path": "/private/path",
        },
        "cleanup": {
            "compute": {"verified_absent": True},
            "source_artifact": {"verified_absent": True},
            "result_artifact": {"verified_absent": True},
        },
    }


class PublicEvidenceTests(unittest.TestCase):
    @patch.object(public_evidence, "_revision", return_value="b" * 40)
    def test_export_contains_proof_without_private_job_data(self, _revision):
        record = public_evidence.build_record(cleaned_job(), "tester-1", "success")
        encoded = str(record)
        self.assertEqual(record["provider"], "fly")
        self.assertEqual(record["artifact_provider"], "tigris")
        self.assertEqual(record["capture_method"], "elsewhere-job-store-v1")
        self.assertTrue(record["source_transport_verified"])
        self.assertNotIn("private-job-id", encoded)
        self.assertNotIn("private-app", encoded)
        self.assertNotIn("private-machine-id", encoded)
        self.assertNotIn("private-bucket", encoded)
        self.assertNotIn("private command", encoded)
        self.assertNotIn("private output", encoded)

    def test_export_rejects_cleanup_without_artifact_absence(self):
        job = cleaned_job()
        job["cleanup"]["result_artifact"] = {"deleted": True}
        with self.assertRaisesRegex(ValueError, "result artifact absence was not verified"):
            public_evidence.build_record(job, "tester-1", "success")

    def test_expected_failure_requires_nonzero_verified_exit(self):
        with self.assertRaisesRegex(ValueError, "non-zero exit code"):
            public_evidence.build_record(cleaned_job(), "tester-1", "expected_failure")

    def test_export_distinguishes_unused_source_transport(self):
        job = cleaned_job()
        job["cleanup"]["source_artifact"] = {"reason": "no source artifact"}
        record = public_evidence.build_record(job, "tester-1", "success")
        self.assertFalse(record["source_transport_verified"])

    def test_export_rejects_non_finite_cost(self):
        job = cleaned_job()
        job["estimated_cost_usd"] = float("nan")
        with self.assertRaisesRegex(ValueError, "positive cost estimate"):
            public_evidence.build_record(job, "tester-1", "success")


if __name__ == "__main__":
    unittest.main()
