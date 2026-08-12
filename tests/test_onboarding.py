import tempfile
import unittest
from pathlib import Path

from agent_capacity.cli import default_config, save_config
from agent_capacity.onboarding import (
    doctor_report,
    initial_config,
    required_init_values,
)


class OnboardingTests(unittest.TestCase):
    def test_fly_setup_uses_tigris_without_enabling_azure(self):
        config = initial_config(
            default_config(),
            provider="fly",
            fly_app="elsewhere-runner",
            fly_region="iad",
            fly_region_fallbacks=["ord", "iad"],
            tigris_bucket="elsewhere-artifacts",
        )
        self.assertEqual(config["routing"], {"default": "fly", "fallbacks": [], "workloads": {}})
        self.assertTrue(config["providers"]["fly"]["enabled"])
        self.assertFalse(config["providers"]["azure"]["enabled"])
        self.assertEqual(config["providers"]["fly"]["region_fallbacks"], ["ord", "iad"])
        self.assertEqual(config["artifact_store"]["provider"], "tigris")
        self.assertEqual(config["artifact_store"]["bucket"], "elsewhere-artifacts")
        self.assertEqual(required_init_values(config), [])

    def test_missing_setup_values_are_actionable(self):
        config = initial_config(default_config(), provider="fly")
        self.assertEqual(required_init_values(config), ["Fly app", "Tigris bucket"])

    def test_doctor_separates_planning_from_execution_readiness(self):
        config = initial_config(
            default_config(), provider="fly", fly_app="runner", tigris_bucket="artifacts"
        )
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / ".elsewhere.json"
            selected.write_text("{}")
            report = doctor_report(
                config,
                selected,
                lambda provider: (True, "ready"),
                lambda: (True, "ready"),
                {
                    "valid": False,
                    "reasons": ["artifact-store destination differs from the approved destination"],
                    "source": {},
                },
                {"sensing_available": True},
            )
        self.assertTrue(report["ready_for_planning"])
        self.assertFalse(report["ready_for_execution"])
        self.assertEqual(report["warnings"], 1)
        trust = next(item for item in report["checks"] if item["name"] == "trust")
        self.assertEqual(trust["status"], "warn")
        self.assertIn("artifact-store destination differs", trust["message"])

    def test_doctor_keeps_preapproval_source_check_nonblocking(self):
        config = initial_config(
            default_config(), provider="fly", fly_app="runner", tigris_bucket="artifacts"
        )
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / ".elsewhere.json"
            selected.write_text("{}")
            report = doctor_report(
                config,
                selected,
                lambda provider: (True, "ready"),
                lambda: (True, "ready"),
                {"configured": False, "valid": False, "source": {}},
                {"sensing_available": True},
                source_path=directory,
                source_allowed=lambda source, roots: False,
                source_inspect=lambda source: {"kind": "git", "dirty": False},
            )
        source = next(item for item in report["checks"] if item["name"] == "source boundary")
        self.assertEqual(source["status"], "warn")
        self.assertTrue(report["ready_for_planning"])
        self.assertFalse(report["ready_for_execution"])

    def test_project_config_write_does_not_make_project_directory_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            path = root / ".elsewhere.json"
            save_config(default_config(), path)
            self.assertEqual(root.stat().st_mode & 0o777, 0o755)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_doctor_rejects_dirty_or_unapproved_private_source(self):
        config = initial_config(
            default_config(), provider="fly", fly_app="runner", tigris_bucket="artifacts"
        )
        trust = {
            "configured": True,
            "valid": True,
            "source": {
                "allowed_roots": ["/approved"],
                "allow_private": True,
                "allow_uncommitted": False,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / ".elsewhere.json"
            selected.write_text("{}")
            report = doctor_report(
                config,
                selected,
                lambda provider: (True, "ready"),
                lambda: (True, "ready"),
                trust,
                {"sensing_available": True},
                source_path="/approved/project",
                source_allowed=lambda source, roots: True,
                source_inspect=lambda source: {"kind": "git", "dirty": True},
            )
        source = next(item for item in report["checks"] if item["name"] == "source boundary")
        self.assertEqual(source["status"], "fail")
        self.assertIn("uncommitted", source["message"])
        self.assertFalse(report["ready_for_execution"])

        trust["source"].update({"allow_private": False, "allow_uncommitted": True})
        report = doctor_report(
            config,
            selected,
            lambda provider: (True, "ready"),
            lambda: (True, "ready"),
            trust,
            {"sensing_available": True},
            source_path="/approved/project",
            source_allowed=lambda source, roots: True,
            source_inspect=lambda source: {"kind": "git", "dirty": False},
        )
        source = next(item for item in report["checks"] if item["name"] == "source boundary")
        self.assertIn("private source export is not approved", source["message"])


if __name__ == "__main__":
    unittest.main()
