#!/usr/bin/env python3
"""Cross-platform local-capacity sensing: Linux /proc parsing, PSI-driven bands,
and honest reporting when sensing is unavailable. These run on the macOS CI host by
parsing fixture text and mocking /proc reads, so they never depend on the runner OS.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from agent_capacity.cli import (  # noqa: E402
    capacity_band,
    linux_direct_metrics,
    parse_meminfo,
    parse_pressure_stall,
    recommend_count,
    system_metrics,
    unavailable_metrics,
)

MEMINFO = """MemTotal:       16384000 kB
MemFree:         2000000 kB
MemAvailable:    8192000 kB
Buffers:          100000 kB
Cached:          4000000 kB
SwapTotal:       2097152 kB
SwapFree:        1887436 kB
"""

PSI = """some avg10=12.34 avg60=5.00 avg300=1.00 total=123456789
full avg10=6.50 avg60=2.00 avg300=0.50 total=98765432
"""


class MeminfoTests(unittest.TestCase):
    def test_reports_total_headroom_and_swap(self):
        m = parse_meminfo(MEMINFO)
        self.assertEqual(m["total_mb"], 16000)
        self.assertEqual(m["memory_level"], 50)  # MemAvailable / MemTotal
        self.assertTrue(m["swap_known"])
        self.assertEqual(m["swap_total_mb"], 2048)
        self.assertEqual(m["swap_utilization_percent"], 10.0)

    def test_missing_swap_is_reported_as_unknown(self):
        m = parse_meminfo("MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\n")
        self.assertFalse(m["swap_known"])
        self.assertEqual(m["memory_level"], 50)

    def test_falls_back_to_memfree_without_memavailable(self):
        m = parse_meminfo("MemTotal: 8000000 kB\nMemFree: 1600000 kB\n")
        self.assertEqual(m["memory_level"], 20)

    def test_empty_meminfo_is_zeroed(self):
        m = parse_meminfo("")
        self.assertEqual(m["total_mb"], 0)
        self.assertEqual(m["memory_level"], 0)


class PressureStallTests(unittest.TestCase):
    def test_reads_full_and_some_avg10(self):
        p = parse_pressure_stall(PSI)
        self.assertEqual(p["memory_stall_percent"], 6.5)
        self.assertEqual(p["memory_some_stall_percent"], 12.34)

    def test_absent_psi_is_zero(self):
        p = parse_pressure_stall("")
        self.assertEqual(p["memory_stall_percent"], 0.0)


class PsiBandTests(unittest.TestCase):
    def _metrics(self, stall):
        return {
            "total_mb": 16000, "memory_level": 70, "swap_known": False,
            "swap_utilization_percent": 0.0, "pageouts_per_second": 0.0,
            "swapouts_per_second": 0.0, "memory_stall_percent": stall,
        }

    def test_high_stall_forces_critical_despite_healthy_headroom(self):
        self.assertEqual(capacity_band(self._metrics(25.0))["name"], "critical")

    def test_moderate_stall_is_constrained(self):
        self.assertEqual(capacity_band(self._metrics(6.5))["name"], "constrained")

    def test_mild_stall_is_guarded(self):
        self.assertEqual(capacity_band(self._metrics(1.5))["name"], "guarded")

    def test_no_stall_stays_healthy(self):
        self.assertEqual(capacity_band(self._metrics(0.0))["name"], "healthy")

    def test_macos_style_metrics_without_stall_field_are_unaffected(self):
        # No memory_stall_percent key at all — the macOS path never sets it.
        band = capacity_band({"memory_level": 70, "swap_known": False})
        self.assertEqual(band["name"], "healthy")

    def test_thrashing_linux_host_admits_no_bursty_work(self):
        self.assertEqual(recommend_count("build", 4, self._metrics(25.0), []), 0)


class LinuxCollectorTests(unittest.TestCase):
    def test_emits_the_standard_metric_keys(self):
        def fake_read(path):
            return MEMINFO if "meminfo" in path else PSI
        with patch("agent_capacity.cli.read_proc", side_effect=fake_read):
            m = linux_direct_metrics()
        self.assertEqual(m["total_mb"], 16000)
        self.assertEqual(m["memory_level"], 50)
        self.assertTrue(m["sensing_available"])
        self.assertEqual(m["telemetry_source"], "proc")
        self.assertEqual(m["memory_stall_percent"], 6.5)
        # The keys the shared decision logic consumes must all be present.
        for key in ("swapouts_per_second", "pageouts_per_second", "swap_known"):
            self.assertIn(key, m)

    def test_missing_proc_degrades_to_unavailable(self):
        with patch("agent_capacity.cli.read_proc", return_value=""):
            m = linux_direct_metrics()
        self.assertFalse(m["sensing_available"])
        self.assertEqual(m["telemetry_source"], "unavailable")
        self.assertEqual(m["total_mb"], 0)


class UnavailableSensingTests(unittest.TestCase):
    def test_unavailable_metrics_are_honest_not_zero_ram_lies(self):
        m = unavailable_metrics()
        self.assertFalse(m["sensing_available"])
        self.assertEqual(m["telemetry_source"], "unavailable")

    def test_system_metrics_uses_linux_branch(self):
        def fake_read(path):
            return MEMINFO if "meminfo" in path else PSI
        with patch("agent_capacity.cli.host_platform", return_value="linux"), \
                patch("agent_capacity.cli.read_proc", side_effect=fake_read), \
                patch.dict("os.environ", {}, clear=False) as _:
            import os
            os.environ.pop("AGENT_CAPACITY_TOTAL_MB", None)
            os.environ.pop("AGENT_CAPACITY_MEMORY_LEVEL", None)
            os.environ.pop("AGENT_CAPACITY_HOST_METRICS", None)
            m = system_metrics()
        self.assertEqual(m["telemetry_source"], "proc")
        self.assertEqual(m["total_mb"], 16000)

    def test_explicit_host_sample_overrides_platform_collection(self):
        sample = {
            "total_mb": 18432, "memory_level": 41, "swap_known": True,
            "swap_total_mb": 8192, "swap_used_mb": 7440,
            "swap_free_mb": 752, "swap_utilization_percent": 90.8,
            "pageouts_per_second": 0, "swapins_per_second": 0,
            "swapouts_per_second": 0, "memory_stall_percent": 0,
        }
        with patch("agent_capacity.cli.host_platform", return_value="linux"), \
                patch("agent_capacity.cli.read_host_sample", return_value=sample), \
                patch.dict("os.environ", {"AGENT_CAPACITY_HOST_METRICS": "/tmp/fixture.json"}):
            m = system_metrics()
        self.assertEqual(m["swap_utilization_percent"], 90.8)
        self.assertEqual(m["swap_used_mb"], 7440)

    def test_macos_automatically_consumes_its_installed_sampler(self):
        import os
        sample = {
            "total_mb": 18432, "memory_level": 43, "swap_known": True,
            "swap_total_mb": 7168, "swap_used_mb": 5725,
            "swap_free_mb": 1443, "swap_utilization_percent": 79.9,
            "pageouts_per_second": 0.18, "swapins_per_second": 0,
            "swapouts_per_second": 0, "memory_stall_percent": 0,
            "telemetry_source": "host-sampler",
        }
        with patch("agent_capacity.cli.host_platform", return_value="darwin"), \
                patch("agent_capacity.cli.read_host_sample", return_value=sample), \
                patch.dict("os.environ", {}, clear=False):
            os.environ.pop("AGENT_CAPACITY_HOST_METRICS", None)
            m = system_metrics()
        self.assertEqual(m["telemetry_source"], "host-sampler")
        self.assertEqual(m["swap_utilization_percent"], 79.9)

    def test_unsupported_platform_reports_unavailable(self):
        import os
        with patch("agent_capacity.cli.host_platform", return_value="unsupported"):
            saved = {k: os.environ.pop(k, None) for k in
                     ("AGENT_CAPACITY_TOTAL_MB", "AGENT_CAPACITY_MEMORY_LEVEL",
                      "AGENT_CAPACITY_HOST_METRICS")}
            try:
                m = system_metrics()
            finally:
                for k, v in saved.items():
                    if v is not None:
                        os.environ[k] = v
        self.assertFalse(m["sensing_available"])


if __name__ == "__main__":
    unittest.main()
