import importlib.util
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/capture_public_journey.py"
SPEC = importlib.util.spec_from_file_location("capture_public_journey", SCRIPT)
assert SPEC and SPEC.loader
journey = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(journey)


def run_record():
    return {
        "id": "run-public",
        "participant_id": "external-1",
        "provider": "fly",
        "artifact_provider": "tigris",
        "scenario": "success",
        "estimated_cost_usd": 0.02,
        "result_verified": True,
        "cleanup_verified": True,
        "result_bundle_sha256": "a" * 64,
        "job_evidence_sha256": "b" * 64,
        "capture_method": "elsewhere-job-store-v1",
        "elsewhere_version": "0.2.0a1",
    }


class JourneyCaptureTests(unittest.TestCase):
    def test_journey_is_linked_to_read_only_checks_and_run_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            command = Path(directory) / "elsewhere"
            command.write_text("installed command\n")
            responses = [
                journey.subprocess.CompletedProcess(
                    [], 0, stdout="elsewhere 0.2.0a1\n", stderr=""
                ),
                journey.subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps({"ready_for_execution": True}), stderr=""
                ),
                journey.subprocess.CompletedProcess([], 0, stdout=json.dumps({
                    "executed": False,
                    "decision": {"placement": "remote"},
                    "job": {"provider": "fly"},
                    "plan": {"trust": {"allowed": True}},
                }), stderr=""),
            ]
            with (
                patch.object(journey.subprocess, "run", side_effect=responses) as runner,
                patch.object(journey, "_revision", return_value="c" * 40),
                patch.object(journey, "_platform", return_value="linux"),
            ):
                value = journey.capture_journey(
                    "external-1", run_record(), source, command,
                    now=datetime(2026, 8, 12, 12, tzinfo=UTC),
                )

        self.assertEqual(value["run_id"], "run-public")
        self.assertEqual(value["platform"], "linux")
        self.assertEqual(value["steps"]["cleanup"], "compute-and-artifacts-verified-absent")
        self.assertEqual(len(value["journey_evidence_sha256"]), 64)
        self.assertNotIn("--execute", runner.call_args_list[2].args[0])

    def test_journey_rejects_run_from_another_participant(self):
        record = run_record()
        record["participant_id"] = "someone-else"
        with self.assertRaisesRegex(ValueError, "different participant"):
            journey.capture_journey(
                "external-1", record, Path.cwd(), Path(__file__)
            )

    def test_journey_rejects_expected_failure_as_the_live_run_step(self):
        record = run_record()
        record["scenario"] = "expected_failure"
        with self.assertRaisesRegex(ValueError, "successful live run"):
            journey.capture_journey(
                "external-1", record, Path.cwd(), Path(__file__)
            )


if __name__ == "__main__":
    unittest.main()
