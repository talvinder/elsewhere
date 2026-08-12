import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent_capacity.cli import (
    approve_trust,
    default_config,
    evaluate_trust,
    load_config,
    main,
    make_parser,
    revoke_trust,
    trust_status,
)

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src/agent_capacity/cli.py"


class CliOnboardingTests(unittest.TestCase):
    def test_top_level_help_guides_first_run_without_leaking_internal_commands(self):
        parser = make_parser()
        help_text = parser.format_help()

        self.assertIn("New here? Run `elsewhere status --human`", help_text)
        self.assertIn("route", help_text)
        self.assertIn(
            "plan or execute a local-versus-remote placement decision", help_text
        )
        self.assertNotIn("sample-memory", help_text)
        self.assertNotIn("_local-worker", help_text)
        self.assertNotIn("==SUPPRESS==", help_text)

    def test_noninteractive_fly_init_creates_a_private_tigris_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".elsewhere.json"
            result = subprocess.run(
                [
                    sys.executable, str(CLI), "init", "--json", "--path", str(config),
                    "--provider", "fly", "--fly-app", "example-runner",
                    "--fly-region", "iad", "--fly-region-fallback", "ord",
                    "--tigris-bucket", "example-artifacts", "--source-root", str(root),
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            saved = json.loads(config.read_text())
            self.assertEqual(output["artifact_store"], "tigris")
            self.assertEqual(saved["artifact_store"]["bucket"], "example-artifacts")
            self.assertFalse(saved["providers"]["azure"]["enabled"])
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertIn("--allow-private", output["next"][1])
            self.assertIn("--estimated-cost-usd 0.05", output["next"][2])

    def test_init_without_required_values_fails_with_a_creation_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable, str(CLI), "init", "--path",
                    str(Path(directory) / ".elsewhere.json"),
                ],
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--fly-app", result.stderr)

    def test_doctor_proves_planning_readiness_without_approving_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".elsewhere.json"
            loaded = {
                "routing": {"default": "fly", "fallbacks": [], "workloads": {}},
                "providers": {
                    "fly": {
                        "enabled": True, "app": "example-runner", "org": "",
                        "region": "iad", "region_fallbacks": ["ord"], "cpu_kind": "shared",
                    }
                },
                "artifact_store": {
                    "provider": "tigris", "bucket": "example-artifacts",
                    "endpoint": "https://t3.storage.dev", "region": "auto",
                },
                "trust": {"approved": False},
            }
            config.write_text(json.dumps(loaded))
            metrics = {
                "version": 1, "sampled_at": time.time(), "total_mb": 16384,
                "memory_level": 80, "swap_known": True, "swap_total_mb": 0,
                "swap_used_mb": 0, "swap_free_mb": 0, "swap_utilization_percent": 0,
                "pageouts_per_second": 0, "swapins_per_second": 0, "swapouts_per_second": 0,
            }
            stdout = StringIO()
            with (
                patch.object(sys, "argv", ["elsewhere", "doctor", "--json"]),
                patch("agent_capacity.cli.config_path", return_value=config),
                patch("agent_capacity.cli.load_config", return_value=loaded),
                patch("agent_capacity.cli.provider_ready", return_value=(True, "ready")),
                patch("agent_capacity.cli.artifact_store_ready", return_value=(True, "ready")),
                patch("agent_capacity.cli.trust_status", return_value={"valid": False, "source": {}}),
                patch("agent_capacity.cli.system_metrics", return_value={**metrics, "sensing_available": True}),
                redirect_stdout(stdout),
            ):
                code = main()
            self.assertEqual(code, 0)
            output = json.loads(stdout.getvalue())
            self.assertTrue(output["ready_for_planning"])
            self.assertFalse(output["ready_for_execution"])

    def test_trust_status_rejects_destination_drift_before_dispatch(self):
        config = default_config()
        config["providers"]["fly"].update({
            "enabled": True,
            "app": "approved-runner",
            "org": "approved-org",
            "region": "iad",
            "region_fallbacks": [],
        })
        config["artifact_store"].update({
            "provider": "tigris",
            "bucket": "approved-bucket",
            "endpoint": "https://t3.storage.dev",
            "region": "auto",
        })
        config["trust"] = {
            "approved": True,
            "approved_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "providers": {
                "fly": {
                    "identity": {"app": "approved-runner", "org": "approved-org"},
                    "regions": ["iad"],
                }
            },
            "artifact_store": {
                "provider": "tigris",
                "bucket": "approved-bucket",
                "endpoint": "https://t3.storage.dev",
                "region": "auto",
            },
            "source": {"allowed_roots": []},
            "limits": {},
        }
        self.assertTrue(trust_status(config)["valid"])

        config["providers"]["fly"]["app"] = "different-runner"
        drifted_provider = trust_status(config)
        self.assertFalse(drifted_provider["valid"])
        self.assertIn("configured fly account differs", drifted_provider["reasons"][0])

        config["providers"]["fly"]["app"] = "approved-runner"
        config["artifact_store"]["bucket"] = "different-bucket"
        drifted_store = trust_status(config)
        self.assertFalse(drifted_store["valid"])
        self.assertTrue(
            any(
                "artifact-store destination differs" in reason
                for reason in drifted_store["reasons"]
            )
        )

        config["artifact_store"]["endpoint"] = "https://credentials.example"
        malformed_store = trust_status(config)
        self.assertFalse(malformed_store["valid"])
        self.assertTrue(
            any(
                "artifact-store destination differs" in reason
                for reason in malformed_store["reasons"]
            )
        )

    def test_trust_status_fails_closed_for_malformed_boundary(self):
        status = trust_status({"trust": "not-an-object"})

        self.assertFalse(status["valid"])
        self.assertIn("trust contract is malformed", status["reasons"])

    def test_dispatch_trust_fails_closed_for_malformed_boundary(self):
        plan = {"provider": "fly", "provider_config": {"region": "iad"}}
        job = {
            "cpu": 1,
            "memory_mb": 512,
            "max_runtime_seconds": 300,
            "estimated_cost_usd": 0.02,
        }

        decision = evaluate_trust(job, plan, {"trust": "not-an-object"})

        self.assertFalse(decision["allowed"])
        self.assertIn("trust contract is malformed", decision["reasons"])

    def test_dispatch_trust_fails_closed_for_malformed_nested_limits(self):
        config = default_config()
        config["providers"]["fly"].update({"app": "runner", "region": "iad"})
        config["artifact_store"].update({
            "provider": "tigris",
            "bucket": "bucket",
            "endpoint": "https://t3.storage.dev",
            "region": "auto",
        })
        config["trust"] = {
            "approved": True,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "providers": {"fly": {
                "identity": {"app": "runner", "org": ""},
                "regions": ["iad"],
            }},
            "artifact_store": {
                "provider": "tigris",
                "bucket": "bucket",
                "endpoint": "https://t3.storage.dev",
                "region": "auto",
            },
            "source": {},
            "limits": "unbounded",
        }

        decision = evaluate_trust(
            {
                "cpu": 1,
                "memory_mb": 512,
                "max_runtime_seconds": 300,
                "estimated_cost_usd": 0.02,
            },
            {"provider": "fly", "provider_config": {"region": "iad"}},
            config,
        )

        self.assertFalse(decision["allowed"])
        self.assertIn("approved execution limits are malformed", decision["reasons"])

    def test_approve_trust_persists_exact_provider_source_and_cost_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".elsewhere.json"
            source = root / "source"
            source.mkdir()
            config = default_config()
            config["providers"]["fly"].update({
                "enabled": True,
                "app": "approved-runner",
                "org": "approved-org",
                "region": "iad",
                "region_fallbacks": ["ord"],
            })
            config["artifact_store"].update({
                "provider": "tigris",
                "bucket": "approved-bucket",
                "endpoint": "https://t3.storage.dev",
                "region": "auto",
            })
            path.write_text(json.dumps(config))

            with (
                patch("agent_capacity.cli.config_path", return_value=path),
                patch("agent_capacity.cli.provider_ready", return_value=(True, "ready")),
            ):
                approved = approve_trust(
                    path,
                    ["fly"],
                    [str(source)],
                    True,
                    True,
                    2,
                    2048,
                    600,
                    0.05,
                    7,
                )

            saved = json.loads(path.read_text())
            saved_mode = path.stat().st_mode & 0o777

        self.assertTrue(approved["valid"], approved)
        self.assertEqual(saved["trust"]["providers"]["fly"]["regions"], ["iad", "ord"])
        self.assertEqual(saved["trust"]["source"]["allowed_roots"], [str(source.resolve())])
        self.assertTrue(saved["trust"]["source"]["allow_uncommitted"])
        self.assertEqual(saved["trust"]["limits"]["max_estimated_cost_usd"], 0.05)
        self.assertEqual(saved_mode, 0o600)

    def test_approve_trust_rejects_unready_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".elsewhere.json"
            with patch(
                "agent_capacity.cli.provider_ready",
                return_value=(False, "missing provider credentials"),
            ):
                with self.assertRaisesRegex(SystemExit, "missing provider credentials"):
                    approve_trust(path, ["fly"], [], False, False, 1, 512, 300, 0.02, 1)

    def test_explicit_project_denial_does_not_inherit_global_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            global_config = root / "global.json"
            global_config.write_text(json.dumps({
                "trust": {
                    "version": 1, "approved": True,
                    "approved_at": "2026-01-01T00:00:00+00:00",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "providers": {}, "artifact_store": {},
                    "source": {"allowed_roots": []}, "limits": {},
                }
            }))
            config = root / ".elsewhere.json"
            config.write_text(json.dumps({
                "trust": {"approved": False, "inherit_global": False}
            }))
            with (
                patch.dict(os.environ, {"AGENT_CAPACITY_CONFIG": str(config)}),
                patch("agent_capacity.cli.global_config_path", return_value=global_config),
            ):
                loaded = load_config()
            self.assertFalse(loaded["trust"]["approved"])

    def test_legacy_project_config_still_inherits_global_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            global_config = root / "global.json"
            global_config.write_text(json.dumps({
                "trust": {
                    "version": 1, "approved": True,
                    "approved_at": "2026-01-01T00:00:00+00:00",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "providers": {}, "artifact_store": {},
                    "source": {"allowed_roots": []}, "limits": {},
                }
            }))
            config = root / ".elsewhere.json"
            config.write_text(json.dumps({"trust": {"approved": False}}))
            with (
                patch.dict(os.environ, {"AGENT_CAPACITY_CONFIG": str(config)}),
                patch("agent_capacity.cli.global_config_path", return_value=global_config),
            ):
                loaded = load_config()
            self.assertTrue(loaded["trust"]["approved"])

    def test_project_revocation_cannot_reinherit_global_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            global_config = root / "global.json"
            global_config.write_text(json.dumps({
                "trust": {
                    "version": 1, "approved": True,
                    "approved_at": "2026-01-01T00:00:00+00:00",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "providers": {}, "artifact_store": {},
                    "source": {"allowed_roots": []}, "limits": {},
                }
            }))
            config = root / ".elsewhere.json"
            config.write_text(json.dumps({"trust": {"approved": True}}))
            revoke_trust(config)
            with (
                patch.dict(os.environ, {"AGENT_CAPACITY_CONFIG": str(config)}),
                patch("agent_capacity.cli.global_config_path", return_value=global_config),
            ):
                loaded = load_config()
            self.assertFalse(loaded["trust"]["approved"])
            self.assertFalse(loaded["trust"]["inherit_global"])


if __name__ == "__main__":
    unittest.main()
