import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

from agent_capacity.economics import compare


def fixture():
    base = dict(runtime_seconds=3600, setup_seconds=0, queue_seconds=0,
                average_cpus=1, average_memory_gb=2, fixed_hour_usd=0,
                cpu_hour_usd=0, gb_hour_usd=0, transfer_usd=0,
                retained_storage_usd=0, ineligible_reasons=[])
    return dict(version=1, deadline_seconds=7200, budget_usd=10, candidates=[
        dict(base, id="fixed", fixed_hour_usd=0.2),
        dict(base, id="usage", cpu_hour_usd=0.05, gb_hour_usd=0.025)])


class EconomicsTests(unittest.TestCase):
    def test_cli_example(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-m", "agent_capacity.cli", "compare",
                                 str(root / "examples/cost-comparison.json")],
                                capture_output=True, text=True, check=True)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["recommendation"], "usage")
        self.assertFalse(receipt["executed"])

    def test_utilization_changes_choice(self):
        spec = fixture()
        self.assertEqual(compare(spec)["recommendation"], "usage")
        spec["candidates"][1].update(average_cpus=4, average_memory_gb=8)
        self.assertEqual(compare(spec)["recommendation"], "fixed")

    def test_total_cost_and_deadline(self):
        spec = fixture()
        spec["candidates"][1]["transfer_usd"] = 0.2
        self.assertEqual(compare(spec)["recommendation"], "fixed")
        spec["candidates"][0]["queue_seconds"] = 5000
        self.assertEqual(compare(spec)["recommendation"], "usage")

    def test_constraints_fail_closed_and_do_not_mutate(self):
        spec = fixture()
        for row in spec["candidates"]:
            row["ineligible_reasons"] = ["source export not approved"]
        before = copy.deepcopy(spec)
        result = compare(spec)
        self.assertIsNone(result["recommendation"])
        self.assertFalse(result["authorization_granted"])
        self.assertEqual(spec, before)

    def test_reject_invalid_and_missing_estimates(self):
        for value in [float("nan"), float("inf"), -1, True, "0"]:
            spec = fixture()
            spec["candidates"][0]["fixed_hour_usd"] = value
            with self.assertRaises(ValueError):
                compare(spec)
        spec = fixture()
        del spec["candidates"][0]["transfer_usd"]
        with self.assertRaises(KeyError):
            compare(spec)

    def test_setup_and_retention_are_charged(self):
        spec = fixture()
        spec["candidates"][1].update(setup_seconds=3600, retained_storage_usd=0.05)
        result = compare(spec)
        self.assertEqual(result["recommendation"], "fixed")
        self.assertAlmostEqual(result["candidates"][1]["estimated_cost_usd"], 0.25)
