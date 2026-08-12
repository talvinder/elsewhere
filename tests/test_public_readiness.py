import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/public_readiness.py"
SPEC = importlib.util.spec_from_file_location("public_readiness", SCRIPT)
assert SPEC and SPEC.loader
public_readiness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(public_readiness)


class PublicReadinessTests(unittest.TestCase):
    def test_evidence_gate_requires_current_multi_user_cross_platform_proof(self):
        participants = [
            {"id": "p1", "platform": "macos", "role": "maintainer"},
            {"id": "p2", "platform": "linux", "role": "external"},
            {"id": "p3", "platform": "macos", "role": "external"},
        ]
        runs = []
        for index in range(25):
            runs.append({
                "id": f"run-{index + 1}",
                "participant_id": participants[index % 3]["id"],
                "provider": "fly" if index % 2 else "azure",
                "artifact_provider": "tigris" if index % 2 else "azure-blob",
                "scenario": "expected_failure" if index < 3 else "success",
                "completed_at": "2026-08-11T12:00:00+00:00",
                "region": "iad",
                "estimated_cost_usd": 0.05,
                "result_verified": True,
                "cleanup_verified": True,
                "source_transport_verified": True,
                "result_bundle_sha256": f"{index + 100:064x}",
                "job_evidence_sha256": f"{index:064x}",
                "capture_method": "elsewhere-job-store-v1",
                "evidence_exporter_revision": "b" * 40,
                "elsewhere_version": "0.2.0a1",
            })
        journey = {
            "participant_id": "p2",
            "run_id": "run-2",
            "completed_at": "2026-08-11T13:00:00+00:00",
            "platform": "linux",
            "capture_method": "elsewhere-journey-v1",
            "installed_version": "0.2.0a1",
            "installed_command_sha256": "d" * 64,
            "journey_capture_revision": "b" * 40,
            "dry_plan_provider": "fly",
            "dry_plan_trust_allowed": True,
            "doctor_ready_for_execution": True,
            "result_bundle_sha256": f"{101:064x}",
            "job_evidence_sha256": f"{1:064x}",
            "steps": {
                "install": "command-and-version-verified",
                "doctor": "execution-ready",
                "dry_plan": "remote-trust-approved",
                "live_run": "linked-ledger-run",
                "result_recovery": "checksum-verified",
                "cleanup": "compute-and-artifacts-verified-absent",
            },
        }
        journey["journey_evidence_sha256"] = hashlib.sha256(
            json.dumps(journey, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps({
                "generated_at": "2026-08-12T00:00:00+00:00",
                "participants": participants,
                "runs": runs,
                "journeys": [journey],
            }))
            checks = public_readiness.validate_evidence(
                path, now=datetime(2026, 8, 12, 12, tzinfo=UTC)
            )
        self.assertTrue(all(item["passed"] for item in checks), checks)

    def test_evidence_gate_rejects_unlinked_manual_journey_checkboxes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps({
                "generated_at": "2026-08-12T00:00:00+00:00",
                "participants": [{
                    "id": "p1", "platform": "linux", "role": "external"
                }],
                "runs": [],
                "journeys": [{
                    "participant_id": "p1",
                    "steps": {
                        "install": True, "doctor": True, "dry_plan": True,
                        "live_run": True, "result_recovery": True, "cleanup": True,
                    },
                }],
            }))
            checks = public_readiness.validate_evidence(
                path, now=datetime(2026, 8, 12, 12, tzinfo=UTC)
            )
        journey = next(item for item in checks if item["name"] == "stranger journey")
        self.assertFalse(journey["passed"])

    def test_evidence_gate_rejects_unverified_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps({
                "generated_at": "2026-08-12T00:00:00+00:00",
                "participants": [{"id": "p1", "platform": "macos"}],
                "runs": [{
                    "id": "run-1", "provider": "fly", "scenario": "success",
                    "participant_id": "p1", "completed_at": "2026-08-11T12:00:00+00:00",
                    "region": "iad", "estimated_cost_usd": 0.05,
                    "result_verified": True, "cleanup_verified": False,
                    "source_transport_verified": True,
                    "result_bundle_sha256": "a" * 64,
                    "job_evidence_sha256": "b" * 64,
                    "capture_method": "elsewhere-job-store-v1",
                    "evidence_exporter_revision": "c" * 40,
                    "elsewhere_version": "0.2.0a1",
                }],
            }))
            checks = public_readiness.validate_evidence(
                path, now=datetime(2026, 8, 12, 12, tzinfo=UTC)
            )
        cleanup = next(item for item in checks if item["name"] == "result and cleanup proof")
        self.assertFalse(cleanup["passed"])

    def test_evidence_gate_rejects_hand_authored_assertions_without_ledger_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps({
                "generated_at": "2026-08-12T00:00:00+00:00",
                "participants": [{"id": "p1", "platform": "macos"}],
                "runs": [{
                    "id": "run-1", "participant_id": "p1", "provider": "fly",
                    "artifact_provider": "tigris", "scenario": "success",
                    "completed_at": "2026-08-11T12:00:00+00:00",
                    "region": "iad", "estimated_cost_usd": 0.05,
                    "result_verified": True, "cleanup_verified": True,
                    "source_transport_verified": True,
                }],
            }))
            checks = public_readiness.validate_evidence(
                path, now=datetime(2026, 8, 12, 12, tzinfo=UTC)
            )
        captured = next(item for item in checks if item["name"] == "ledger-captured proof")
        self.assertFalse(captured["passed"])

    def test_named_participants_do_not_count_until_they_run_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps({
                "generated_at": "2026-08-12T00:00:00+00:00",
                "participants": [
                    {"id": "p1", "platform": "macos"},
                    {"id": "p2", "platform": "linux"},
                    {"id": "p3", "platform": "macos"},
                ],
                "runs": [{
                    "id": f"run-{index}", "participant_id": "p1", "provider": "fly",
                    "completed_at": "2026-08-11T12:00:00+00:00",
                    "result_verified": True, "cleanup_verified": True,
                    "source_transport_verified": True,
                    "result_bundle_sha256": "a" * 64,
                    "job_evidence_sha256": "b" * 64,
                    "capture_method": "elsewhere-job-store-v1",
                    "evidence_exporter_revision": "c" * 40,
                    "elsewhere_version": "0.2.0a1",
                } for index in range(25)],
            }))
            checks = public_readiness.validate_evidence(
                path, now=datetime(2026, 8, 12, 12, tzinfo=UTC)
            )
        participants = next(item for item in checks if item["name"] == "dogfood participants")
        self.assertFalse(participants["passed"])

    def test_duplicate_runs_and_unsupported_providers_do_not_inflate_proof(self):
        base = {
            "id": "run-1",
            "participant_id": "p1",
            "provider": "fly",
            "artifact_provider": "tigris",
            "scenario": "success",
            "completed_at": "2026-08-11T12:00:00+00:00",
            "region": "iad",
            "estimated_cost_usd": 0.05,
            "result_verified": True,
            "cleanup_verified": True,
            "source_transport_verified": True,
            "result_bundle_sha256": "a" * 64,
            "job_evidence_sha256": "b" * 64,
            "capture_method": "elsewhere-job-store-v1",
            "evidence_exporter_revision": "c" * 40,
            "elsewhere_version": "0.2.0a1",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps({
                "generated_at": "2026-08-12T00:00:00+00:00",
                "participants": [
                    {"id": "p1", "platform": "macos", "role": "maintainer"}
                ],
                "runs": [base, dict(base), {**base, "id": "run-2", "provider": "fake"}],
                "journeys": [],
            }))
            checks = public_readiness.validate_evidence(
                path, now=datetime(2026, 8, 12, 12, tzinfo=UTC)
            )
        schema = next(item for item in checks if item["name"] == "evidence schema")
        count = next(item for item in checks if item["name"] == "dogfood run count")
        providers = next(item for item in checks if item["name"] == "provider neutrality proof")
        self.assertFalse(schema["passed"])
        self.assertEqual(count["message"], "0 of 25 unique qualifying runs")
        self.assertFalse(providers["passed"])

    def test_future_dated_runs_do_not_count_as_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps({
                "generated_at": "2027-08-12T00:00:00+00:00",
                "participants": [{"id": "p1", "platform": "macos"}],
                "runs": [{
                    "id": "run-1", "participant_id": "p1", "provider": "fly",
                    "completed_at": "2027-08-12T00:00:00+00:00",
                    "result_verified": True, "cleanup_verified": True,
                    "result_bundle_sha256": "a" * 64,
                    "job_evidence_sha256": "b" * 64,
                    "capture_method": "elsewhere-job-store-v1",
                    "evidence_exporter_revision": "c" * 40,
                    "elsewhere_version": "0.2.0a1",
                }],
            }))
            checks = public_readiness.validate_evidence(
                path, now=datetime(2026, 8, 12, 12, tzinfo=UTC)
            )
        freshness = next(item for item in checks if item["name"] == "evidence freshness")
        self.assertFalse(freshness["passed"])

    def test_relative_link_check_reports_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("[missing](docs/missing.md)\n")
            broken = public_readiness.check_links(root, ["README.md"])
        self.assertEqual(broken, ["README.md -> docs/missing.md"])

    def test_static_gate_rejects_moving_action_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/ci.yml").write_text(
                "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n"
                "# macos-latest ubuntu-latest \"3.11\" \"3.12\" \"3.13\"\n"
            )
            (root / ".github/CODEOWNERS").write_text(
                "/.github/ @talvinder\n"
                "/scripts/check-no-internal.sh @talvinder\n"
                "/scripts/public_readiness.py @talvinder\n"
                "/src/agent_capacity/artifact_transport.py @talvinder\n"
            )
            with (
                mock.patch.object(public_readiness, "tracked_files", return_value=[]),
                mock.patch.object(public_readiness, "check_links", return_value=[]),
                mock.patch.object(
                    public_readiness,
                    "run",
                    return_value=public_readiness.subprocess.CompletedProcess(
                        [], 0, stdout="", stderr=""
                    ),
                ),
            ):
                checks = public_readiness.static_checks(root)

        pinning = next(item for item in checks if item["name"] == "immutable CI dependencies")
        self.assertFalse(pinning["passed"])
        self.assertIn("actions/checkout@v4", pinning["message"])

    def test_static_gate_accepts_immutable_action_shas_and_owned_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/ci.yml").write_text(
                "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@" + "a" * 40 + "\n"
                "# macos-latest ubuntu-latest \"3.11\" \"3.12\" \"3.13\"\n"
            )
            (root / ".github/CODEOWNERS").write_text(
                "/.github/ @talvinder\n"
                "/scripts/check-no-internal.sh @talvinder\n"
                "/scripts/public_readiness.py @talvinder\n"
                "/src/agent_capacity/artifact_transport.py @talvinder\n"
            )
            with (
                mock.patch.object(public_readiness, "tracked_files", return_value=[]),
                mock.patch.object(public_readiness, "check_links", return_value=[]),
                mock.patch.object(
                    public_readiness,
                    "run",
                    return_value=public_readiness.subprocess.CompletedProcess(
                        [], 0, stdout="init doctor", stderr=""
                    ),
                ),
            ):
                checks = public_readiness.static_checks(root)

        pinning = next(item for item in checks if item["name"] == "immutable CI dependencies")
        ownership = next(item for item in checks if item["name"] == "trust-boundary ownership")
        self.assertTrue(pinning["passed"])
        self.assertTrue(ownership["passed"])

    def test_online_journey_requires_all_four_logged_out_surfaces(self):
        responses = [
            (
                200,
                b"Your work doesn't need your laptop github.com/talvinder/elsewhere",
                public_readiness.LANDING_URL,
            ),
            (404, b"Not Found", public_readiness.REPOSITORY_URL),
            (200, b"refs/heads/main", public_readiness.INSTALL_PROBE_URL),
            (200, b"# Dogfood elsewhere", public_readiness.DOGFOOD_URL),
        ]
        with mock.patch.object(
            public_readiness, "fetch_public_url", side_effect=responses
        ):
            checks = public_readiness.online_journey_checks()

        self.assertEqual(len(checks), 4)
        self.assertFalse(checks[1]["passed"])
        self.assertIn("HTTP 404", checks[1]["message"])
        self.assertTrue(checks[0]["passed"])
        self.assertTrue(checks[2]["passed"])
        self.assertTrue(checks[3]["passed"])

    def test_online_journey_rejects_a_public_but_wrong_landing_page(self):
        responses = [
            (200, b"generic page", public_readiness.LANDING_URL),
            (200, b"talvinder/elsewhere", public_readiness.REPOSITORY_URL),
            (200, b"refs/heads/main", public_readiness.INSTALL_PROBE_URL),
            (200, b"# Dogfood elsewhere", public_readiness.DOGFOOD_URL),
        ]
        with mock.patch.object(
            public_readiness, "fetch_public_url", side_effect=responses
        ):
            checks = public_readiness.online_journey_checks()

        self.assertFalse(checks[0]["passed"])
        self.assertIn("missing expected content", checks[0]["message"])

    def test_live_proof_revision_rejects_runtime_changes_after_proven_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps({"runs": [{
                "provider": "fly",
                "artifact_provider": "tigris",
                "completed_at": "2026-08-12T00:00:00+00:00",
                "source_transport_verified": True,
                "result_verified": True,
                "cleanup_verified": True,
                "evidence_exporter_revision": "a" * 40,
            }]}))
            completed = lambda code: public_readiness.subprocess.CompletedProcess([], code)
            with mock.patch.object(
                public_readiness,
                "run",
                side_effect=[completed(0), completed(0), completed(1)],
            ):
                check = public_readiness.live_proof_revision_check(path, Path(directory))
        self.assertFalse(check["passed"])
        self.assertIn("runtime code changed", check["message"])

    def test_live_proof_revision_allows_documentation_only_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps({"runs": [{
                "provider": "fly",
                "artifact_provider": "tigris",
                "completed_at": "2026-08-12T00:00:00+00:00",
                "source_transport_verified": True,
                "result_verified": True,
                "cleanup_verified": True,
                "evidence_exporter_revision": "a" * 40,
            }]}))
            completed = lambda code: public_readiness.subprocess.CompletedProcess([], code)
            with mock.patch.object(
                public_readiness,
                "run",
                side_effect=[completed(0), completed(0), completed(0)],
            ):
                check = public_readiness.live_proof_revision_check(path, Path(directory))
        self.assertTrue(check["passed"])

    def test_history_check_summarizes_scanner_findings_without_secret_values(self):
        report = {
            "clean": False,
            "findings": [{
                "category": "credential assignment",
                "location": "old-config.txt",
                "commit": "a" * 40,
            }],
        }
        completed = public_readiness.subprocess.CompletedProcess(
            [], 1, stdout=json.dumps(report), stderr=""
        )
        with mock.patch.object(public_readiness, "run", return_value=completed):
            check = public_readiness.history_check(Path("/tmp/example"))

        self.assertFalse(check["passed"])
        self.assertIn("credential assignment: old-config.txt", check["message"])

    def test_history_check_accepts_a_clean_complete_scan(self):
        report = {"clean": True, "commits_scanned": 12, "text_blobs_scanned": 34}
        completed = public_readiness.subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(report), stderr=""
        )
        with mock.patch.object(public_readiness, "run", return_value=completed):
            check = public_readiness.history_check(Path("/tmp/example"))

        self.assertTrue(check["passed"])
        self.assertIn("12 commits and 34 unique text blobs", check["message"])


if __name__ == "__main__":
    unittest.main()
