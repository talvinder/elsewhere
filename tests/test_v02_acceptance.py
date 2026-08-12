import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts/v02_acceptance.py"
SPEC = importlib.util.spec_from_file_location("v02_acceptance", SCRIPT)
assert SPEC and SPEC.loader
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


class AcceptanceEvidenceTests(unittest.TestCase):
    def test_live_runner_returns_ledger_derived_public_evidence(self):
        planned_job = {"id": "private-id", "provider": "fly"}
        exact_result = {
            "state": "collected",
            "local_path": "/tmp/result",
            "exit_code": 0,
            "stdout": "elsewhere-fly-1-stdout",
        }
        cleaned_job = {"id": "private-id", "state": "cleaned"}
        public_record = {
            "id": "run-public",
            "participant_id": "maintainer-1",
            "capture_method": "elsewhere-job-store-v1",
        }

        with (
            patch.object(
                acceptance,
                "build_dispatch_plan",
                return_value=(planned_job, {"provider": "fly"}),
            ),
            patch.object(
                acceptance,
                "attach_execution_artifacts",
                return_value=(planned_job, {"provider": "fly"}),
            ),
            patch.object(acceptance, "load_config", return_value={}),
            patch.object(acceptance, "execute_dispatch", return_value=(0, {})),
            patch.object(
                acceptance,
                "refresh_remote_job",
                return_value=({**planned_job, "state": "succeeded", "result": exact_result}, {}),
            ),
            patch.object(acceptance, "cleanup", return_value=True),
            patch.object(acceptance, "find_job", return_value=cleaned_job),
            patch.object(acceptance, "build_record", return_value=public_record) as exporter,
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "read_text", return_value="elsewhere-fly-1-file"),
        ):
            value = acceptance.run_once(
                "fly", 1, "receipt", "maintainer-1", ROOT
            )

        self.assertEqual(value["id"], "run-public")
        self.assertEqual(value["capture_method"], "elsewhere-job-store-v1")
        exporter.assert_called_once_with(cleaned_job, "maintainer-1", "success")


if __name__ == "__main__":
    unittest.main()
