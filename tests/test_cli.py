#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent
CLI = ROOT.parent / "src/agent_capacity/cli.py"
sys.path.insert(0, str(ROOT.parent / "src"))
from agent_capacity.artifact_transport import package_source  # noqa: E402
from agent_capacity.cli import (  # noqa: E402
    acquire,
    dashboard_html,
    keep_lease_alive,
    parse_swap_usage,
    parse_vm_stat,
    release,
)


def call(
    state: Path, *args: str, level: int = 80, check: bool = True,
    config: Path | None = None, host_metrics: Path | None = None,
):
    env = {
        **os.environ,
        "AGENT_CAPACITY_STATE": str(state),
        "AGENT_CAPACITY_TOTAL_MB": "18432",
        "AGENT_CAPACITY_MEMORY_LEVEL": str(level),
        "AGENT_CAPACITY_JOBS": str(state.with_name("jobs.json")),
        "AGENT_CAPACITY_HOST_METRICS": str(host_metrics or state.with_name("host-memory.json")),
        "AGENT_CAPACITY_FLY_APP": "test-agent-capacity",
        "AGENT_CAPACITY_AZURE_RESOURCE_GROUP": "example-unit-test-rg",
    }
    if config:
        env["AGENT_CAPACITY_CONFIG"] = str(config)
    result = subprocess.run(
        [sys.executable, str(CLI), *args], env=env, text=True, capture_output=True
    )
    if check and result.returncode:
        raise AssertionError(f"command failed: {result.args}\n{result.stdout}\n{result.stderr}")
    return result, json.loads(result.stdout) if result.stdout else None


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        fake_bin = Path(directory) / "bin"
        fake_bin.mkdir()
        for command in ("fly", "az"):
            executable = fake_bin / command
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"

        version = subprocess.run(
            [sys.executable, str(CLI), "--version"],
            text=True, capture_output=True, check=True,
        )
        assert version.stdout.strip().startswith("elsewhere ")

        state = Path(directory) / "leases.json"
        config = Path(directory) / "config.json"
        config.write_text(json.dumps({
            "version": 1,
            "routing": {"default": "fly", "fallbacks": ["azure"], "workloads": {}},
            "providers": {
                "fly": {
                    "enabled": True, "app": "test-agent-capacity", "org": "test-org",
                    "region": "bom", "region_fallbacks": ["sin"], "cpu_kind": "shared",
                },
                "azure": {
                    "enabled": True, "subscription": "test-subscription",
                    "resource_group": "example-unit-test-rg", "location": "centralindia",
                },
            },
            "artifact_store": {
                "provider": "azure-blob", "account": "teststorage", "container": "sources",
                "subscription": "test-subscription", "sas_ttl_minutes": 60,
            },
            "trust": {"approved": False},
        }))

        _, status = call(state, "status")
        assert status["recommendations"]["service"] == 4
        assert status["recommendations"]["parallel-agent"] == 2
        assert status["recommendations"]["build"] == 1

        _, lease = call(state, "acquire", "--workload", "parallel-agent", "--count", "2", "--owner", "codex:test")
        assert lease["allowed"] is True

        denied_result, denied = call(
            state, "acquire", "--workload", "parallel-agent", "--count", "1", "--owner", "claude:test", check=False
        )
        assert denied_result.returncode == 2
        assert denied["allowed"] is False
        assert denied["recommended_count"] == 0

        _, released = call(state, "release", lease["token"])
        assert released["released"] is True

        critical_result, critical = call(
            state, "acquire", "--workload", "build", "--owner", "codex:critical", level=20, check=False
        )
        assert critical_result.returncode == 2
        assert critical["recommended_count"] == 0

        fail_fast_result, fail_fast = call(
            state,
            "run",
            "--workload",
            "build",
            "--owner",
            "codex:fail-fast",
            "--no-queue",
            "--",
            "/usr/bin/true",
            level=20,
            check=False,
        )
        assert fail_fast_result.returncode == 2
        assert fail_fast["allowed"] is False

        run_result, _ = call(
            state, "run", "--workload", "light", "--owner", "test:run", "--", "/usr/bin/true", check=False
        )
        assert run_result.returncode == 0
        _, final_status = call(state, "status")
        assert final_status["active_leases"] == []

        _, blocker = call(
            state, "acquire", "--workload", "light", "--count", "4", "--owner", "test:blocker"
        )
        marker = Path(directory) / "queued-ran"
        queued_result, queued = call(
            state,
            "run",
            "--workload",
            "light",
            "--owner",
            "test:queued",
            "--poll-seconds",
            "0.05",
            "--",
            "/bin/sh",
            "-lc",
            f"printf queued-ok; touch {marker}",
            check=False,
        )
        assert queued_result.returncode == 0
        assert queued["queued"] is True
        assert queued["job"]["state"] == "waiting_for_capacity"
        queued_job_id = queued["job"]["id"]

        _, queue = call(state, "queue", "--json")
        assert any(item["id"] == queued_job_id for item in queue["active_jobs"])
        assert any(item["owner"] == "test:blocker" for item in queue["leases"])
        assert queue["counts"]["waiting"] == 1
        assert queue["counts"]["local_running"] == 0

        _, queued_status = call(state, "job-status", queued_job_id)
        assert queued_status["job"]["state"] == "waiting_for_capacity"
        _, _ = call(state, "release", blocker["token"])

        deadline = time.time() + 3
        while time.time() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.exists(), "queued command did not start after capacity returned"

        deadline = time.time() + 3
        while time.time() < deadline:
            _, queued_status = call(state, "job-status", queued_job_id)
            if queued_status["job"]["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.05)
        assert queued_status["job"]["state"] == "succeeded"
        _, queued_logs = call(state, "job-logs", queued_job_id)
        assert "queued-ok" in queued_logs["stdout"]
        _, queued_cleanup = call(state, "job-cleanup", queued_job_id)
        assert queued_cleanup["log_deleted"] is True

        host_metrics = Path(directory) / "adaptive-host-memory.json"
        host_metrics.write_text(json.dumps({
            "version": 1,
            "sampled_at": time.time(),
            "total_mb": 18432,
            "memory_level": 41,
            "swap_known": True,
            "swap_total_mb": 8192,
            "swap_used_mb": 7440,
            "swap_free_mb": 752,
            "swap_utilization_percent": 90.8,
            "pageouts_per_second": 0,
            "swapins_per_second": 0,
            "swapouts_per_second": 0,
        }))
        _, guarded = call(state, "status", level=41, host_metrics=host_metrics)
        assert guarded["capacity_band"]["name"] == "guarded"
        assert guarded["recommendations"]["service"] == 4
        assert guarded["recommendations"]["light"] >= 1
        assert guarded["recommendations"]["build"] == 0
        assert guarded["budget"]["swap_penalty_mb"] == 512
        denied_result, actionable = call(
            state, "acquire", "--workload", "build", "--owner", "test:guarded",
            level=41, host_metrics=host_metrics, check=False,
        )
        assert denied_result.returncode == 2
        assert actionable["denied_by"] == "insufficient_burst_headroom"
        assert actionable["placement_advice"]["action"] == "assess_remote_placement"
        assert actionable["placement_advice"]["automatic_dispatch"] is False
        assert actionable["privacy"].endswith("are hidden")

        quiet_guarded_sample = json.loads(host_metrics.read_text())
        quiet_guarded_sample.update({
            "sampled_at": time.time(), "memory_level": 49,
            "swap_used_mb": 6881, "swap_free_mb": 1311,
            "swap_utilization_percent": 84.0,
        })
        host_metrics.write_text(json.dumps(quiet_guarded_sample))
        _, quiet_guarded = call(state, "status", level=49, host_metrics=host_metrics)
        assert quiet_guarded["capacity_band"]["name"] == "guarded"
        assert quiet_guarded["recommendations"]["test"] >= 1

        retained_sample = json.loads(host_metrics.read_text())
        retained_sample.update({
            "sampled_at": time.time(), "memory_level": 70,
            "swap_used_mb": 7782, "swap_free_mb": 410,
            "swap_utilization_percent": 95.0,
            "pageouts_per_second": 0, "swapouts_per_second": 0,
        })
        host_metrics.write_text(json.dumps(retained_sample))
        _, retained = call(state, "status", level=70, host_metrics=host_metrics)
        assert retained["capacity_band"]["name"] == "healthy"
        assert retained["recommendations"]["build"] == 1

        active_sample = json.loads(host_metrics.read_text())
        active_sample["sampled_at"] = time.time()
        active_sample["page_size_bytes"] = 16384
        active_sample["swapouts_per_second"] = 150
        host_metrics.write_text(json.dumps(active_sample))
        _, constrained = call(state, "status", level=41, host_metrics=host_metrics)
        assert constrained["capacity_band"]["name"] == "constrained"
        assert constrained["recommendations"]["service"] >= 1
        assert constrained["recommendations"]["parallel-agent"] == 0

        active_sample["sampled_at"] = time.time()
        active_sample["memory_level"] = 17
        active_sample["swapouts_per_second"] = 0
        host_metrics.write_text(json.dumps(active_sample))
        _, critical_capacity = call(state, "status", level=17, host_metrics=host_metrics)
        assert critical_capacity["capacity_band"]["name"] == "critical"
        assert critical_capacity["recommendations"]["service"] == 0

        old_lease_state = Path(directory) / "old-leases.json"
        old_lease_state.write_text(json.dumps({
            "version": 1,
            "leases": [{
                "token": "old", "owner": "test:settled", "workload": "build", "count": 1,
                "reserved_mb": 4200, "created_at": int(time.time()) - 600,
                "expires_at": int(time.time()) + 600,
            }],
        }))
        healthy_sample = dict(active_sample)
        healthy_sample.update({
            "sampled_at": time.time(), "memory_level": 80, "swap_utilization_percent": 0,
            "swap_used_mb": 0, "swap_free_mb": 8192,
        })
        host_metrics.write_text(json.dumps(healthy_sample))
        _, settled = call(old_lease_state, "status", level=80, host_metrics=host_metrics)
        assert settled["budget"]["leased_mb"] == 4200
        assert settled["budget"]["effective_leased_mb"] == 840

        orphan_state = Path(directory) / "orphan-leases.json"
        orphan_state.write_text(json.dumps({
            "version": 1, "leases": [{
                "token": "orphan", "owner": "test:orphan", "owner_pid": 99999999,
                "workload": "build", "count": 1, "reserved_mb": 4200,
                "created_at": int(time.time()) - 300, "expires_at": int(time.time()) + 600,
            }],
        }))
        _, cleanup = call(orphan_state, "cleanup", "--stale", level=80, host_metrics=host_metrics)
        assert cleanup["released_reservations"] == ["orphan"]
        assert cleanup["privacy"].endswith("are hidden")
        _, readmitted = call(
            orphan_state, "acquire", "--workload", "build", "--owner", "test:after-cleanup",
            level=80, host_metrics=host_metrics,
        )
        assert readmitted["allowed"] is True

        stale_job_state = Path(directory) / "stale-job-leases.json"
        stale_jobs = stale_job_state.with_name("jobs.json")
        stale_job_state.write_text(json.dumps({
            "version": 1, "leases": [{
                "token": "stale-job-token", "owner": "test:stale-job", "owner_pid": 99999999,
                "workload": "test", "count": 1, "reserved_mb": 2500,
                "created_at": int(time.time()) - 120, "expires_at": int(time.time()) + 600,
            }],
        }))
        stale_jobs.write_text(json.dumps({
            "version": 1, "jobs": [{
                "id": "stale-job", "provider": "local", "state": "running",
                "owner": "test:stale-job", "workload": "test", "count": 1,
                "worker_pid": 99999998, "process_pid": 99999999,
                "lease_token": "stale-job-token", "created_at": int(time.time()) - 120,
            }],
        }))
        _, stale_cleanup = call(
            stale_job_state, "cleanup", "--stale", level=80, host_metrics=host_metrics
        )
        assert stale_cleanup["jobs"][0]["id"] == "stale-job"
        assert stale_cleanup["jobs"][0]["memory_mb"] == 2500
        assert stale_cleanup["released_reservations"] == ["stale-job-token"]

        parsed_swap = parse_swap_usage("vm.swapusage: total = 8.00G  used = 7.27G  free = 747.12M")
        assert parsed_swap["swap_known"] is True
        assert parsed_swap["swap_total_mb"] == 8192
        assert parsed_swap["swap_used_mb"] == 7444
        parsed_vm = parse_vm_stat(
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pageouts: 524179.\nSwapins: 12795604.\nSwapouts: 32747967.\n"
        )
        assert parsed_vm["page_size_bytes"] == 16384
        assert parsed_vm["swapouts"] == 32747967

        _, local_route = call(
            state, "route", "--workload", "light", "--command", "printf local-ok"
        )
        assert local_route["decision"]["placement"] == "local"
        assert local_route["executed"] is False

        _, local_execution = call(
            state, "route", "--workload", "light", "--command", "printf local-ok", "--execute"
        )
        assert local_execution["stdout"] == "local-ok"

        _, remote_route = call(
            state, "route", "--workload", "build", "--execution", "remote",
            "--provider", "fly", "--image", "alpine:3.20", "--command", "echo remote-ok",
        )
        assert remote_route["decision"]["placement"] == "remote"
        assert remote_route["decision"]["automatic_placement"] == "local"
        assert remote_route["decision"]["reason"] == "caller explicitly selected remote execution"
        assert remote_route["plan"]["provider"] == "fly"

        _, fly_plan = call(
            state, "dispatch", "--workload", "build", "--provider", "fly",
            "--image", "alpine:3.20", "--command", "echo fly-ok",
            "--cpu", "1", "--memory-mb", "512",
        )
        assert fly_plan["executed"] is False
        assert fly_plan["plan"]["provider"] == "fly"
        assert "--rm" not in fly_plan["plan"]["command"]
        assert "explicitly destroys" in fly_plan["plan"]["cleanup"]
        assert "echo fly-ok" in fly_plan["plan"]["shell_preview"]

        _, azure_plan = call(
            state, "dispatch", "--workload", "test", "--provider", "azure",
            "--image", "alpine:3.20", "--command", "echo azure-ok",
            "--cpu", "1", "--memory-mb", "512",
            config=config,
        )
        assert azure_plan["executed"] is False
        assert azure_plan["plan"]["provider"] == "azure"
        assert "Never" in azure_plan["plan"]["command"]

        trust_source = Path(directory) / "trusted-source"
        trust_source.mkdir()
        (trust_source / "work.txt").write_text("uncommitted work\n")
        _, approved = call(
            state, "trust-approve", "--path", str(config),
            "--provider", "fly", "--provider", "azure",
            "--source-root", str(trust_source), "--allow-uncommitted", "--allow-private",
            "--max-cpu", "4", "--max-memory-mb", "8192",
            "--max-runtime-seconds", "3600", "--max-estimated-cost-usd", "5",
            config=config,
        )
        assert approved["valid"] is True
        assert approved["receipt"].startswith("ew1_")
        assert set(approved["providers"]) == {"fly", "azure"}

        _, trusted_plan = call(
            state, "dispatch", "--workload", "build", "--provider", "fly",
            "--image", "alpine:3.20", "--command", "echo trusted",
            "--cpu", "2", "--memory-mb", "4096", "--source-path", str(trust_source),
            "--max-runtime-seconds", "1200", "--estimated-cost-usd", "0.25",
            config=config,
        )
        assert trusted_plan["plan"]["trust"]["allowed"] is True
        assert trusted_plan["plan"]["trust"]["receipt"] == approved["receipt"]

        outside = Path(directory) / "outside"
        outside.mkdir()
        _, denied_plan = call(
            state, "dispatch", "--workload", "build", "--provider", "fly",
            "--image", "alpine:3.20", "--command", "echo denied",
            "--cpu", "2", "--memory-mb", "4096", "--source-path", str(outside),
            "--max-runtime-seconds", "1200", "--estimated-cost-usd", "0.25",
            config=config,
        )
        assert denied_plan["plan"]["trust"]["allowed"] is False
        assert "source path is outside the approved roots" in denied_plan["plan"]["trust"]["reasons"]

        nan_cost_result, _ = call(
            state, "dispatch", "--workload", "build", "--provider", "fly",
            "--image", "alpine:3.20", "--command", "echo denied",
            "--cpu", "2", "--memory-mb", "4096", "--source-path", str(trust_source),
            "--max-runtime-seconds", "1200", "--estimated-cost-usd", "nan",
            "--execute", config=config, check=False,
        )
        assert nan_cost_result.returncode != 0
        assert "estimated cost" in nan_cost_result.stderr

        bad_receipt_result, _ = call(
            state, "dispatch", "--workload", "build", "--provider", "fly",
            "--image", "alpine:3.20", "--command", "echo denied",
            "--cpu", "2", "--memory-mb", "4096", "--source-path", str(trust_source),
            "--max-runtime-seconds", "1200", "--estimated-cost-usd", "0.25",
            "--approval-receipt", "ew1_00000000000000000000000000000000", "--execute",
            config=config, check=False,
        )
        assert bad_receipt_result.returncode != 0
        assert "approval receipt does not match" in bad_receipt_result.stderr

        mcp_input = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]) + "\n"
        mcp_env = {
            **os.environ,
            "AGENT_CAPACITY_CONFIG": str(config),
            "AGENT_CAPACITY_STATE": str(state),
            "AGENT_CAPACITY_JOBS": str(state.with_name("jobs.json")),
            "AGENT_CAPACITY_TOTAL_MB": "18432",
            "AGENT_CAPACITY_MEMORY_LEVEL": "80",
        }
        mcp = subprocess.run(
            [sys.executable, str(CLI), "mcp-server"], input=mcp_input, env=mcp_env,
            text=True, capture_output=True, check=True,
        )
        responses = [json.loads(line) for line in mcp.stdout.splitlines()]
        assert responses[0]["result"]["serverInfo"]["name"] == "elsewhere"
        tool_names = {item["name"] for item in responses[1]["result"]["tools"]}
        assert tool_names == {
            "elsewhere_trust_status", "elsewhere_queue", "elsewhere_plan",
            "elsewhere_dispatch", "elsewhere_job_status", "elsewhere_job_control", "elsewhere_job_wait",
        }
        assert "Work in motion" in dashboard_html("test-token")
        assert "test-token" in dashboard_html("test-token")

        renewal_state = Path(directory) / "renewal-leases.json"
        renewal_host = Path(directory) / "renewal-host.json"
        renewal_host.write_text(json.dumps({
            "version": 1, "sampled_at": time.time(), "total_mb": 18432,
            "memory_level": 80, "swap_known": True, "swap_total_mb": 8192,
            "swap_used_mb": 0, "swap_free_mb": 8192,
            "swap_utilization_percent": 0, "pageouts_per_second": 0,
            "swapins_per_second": 0, "swapouts_per_second": 0,
        }))
        renewal_env = {
            "AGENT_CAPACITY_STATE": str(renewal_state),
            "AGENT_CAPACITY_HOST_METRICS": str(renewal_host),
            "AGENT_CAPACITY_TOTAL_MB": "18432",
            "AGENT_CAPACITY_MEMORY_LEVEL": "80",
            "AGENT_CAPACITY_RENEW_INTERVAL_SECONDS": "0.02",
        }
        previous_env = {name: os.environ.get(name) for name in renewal_env}
        os.environ.update(renewal_env)
        try:
            code, managed = acquire("service", 1, "test:managed-service", 60)
            assert code == 0
            renewal_data = json.loads(renewal_state.read_text())
            # Prove that the renewal thread changes the stored deadline. Avoid
            # comparing it with the wall clock after an arbitrary sleep: a busy CI
            # runner can legitimately consume several seconds between those reads.
            forced_expiry = int(time.time()) + 5
            renewal_data["leases"][0]["expires_at"] = forced_expiry
            renewal_state.write_text(json.dumps(renewal_data))
            with keep_lease_alive(managed["token"], 60):
                deadline = time.monotonic() + 2
                renewed_expiry = forced_expiry
                while time.monotonic() < deadline and renewed_expiry <= forced_expiry:
                    renewed_data = json.loads(renewal_state.read_text())
                    renewed_expiry = renewed_data["leases"][0]["expires_at"]
                    if renewed_expiry <= forced_expiry:
                        time.sleep(0.01)
            assert renewed_expiry > forced_expiry
            release(managed["token"])
        finally:
            for name, old_value in previous_env.items():
                if old_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old_value

        source = Path(directory) / "source"
        source.mkdir()
        (source / "app.py").write_text("print('ok')\n")
        (source / ".env").write_text("SECRET=nope\n")
        (source / ".npmrc").write_text("//registry.example/:_authToken=nope\n")
        (source / "id_rsa").write_text("private\n")
        (source / ".agent-capacity-manifest.json").write_text("stale\n")
        (source / ".DS_Store").write_text("metadata\n")
        (source / ".aws").mkdir()
        (source / ".aws/credentials").write_text("secret\n")
        (source / ".SSH").mkdir()
        (source / ".SSH/config").write_text("secret\n")
        (source / "node_modules").mkdir()
        (source / "node_modules/ignored.js").write_text("ignored\n")
        bundle, manifest = package_source(str(source), "test")
        try:
            assert [item["path"] for item in manifest["files"]] == ["app.py"]
            assert any(item["path"] == ".env" for item in manifest["skipped"])
            assert any(item["path"] == ".npmrc" for item in manifest["skipped"])
            assert any(item["path"] == "id_rsa" for item in manifest["skipped"])
            assert not any(item["path"] == ".SSH/config" for item in manifest["files"])
            assert not any(
                item["path"] == ".agent-capacity-manifest.json"
                for item in manifest["files"]
            )
            assert not any(item["path"] == ".DS_Store" for item in manifest["files"])
        finally:
            bundle.unlink(missing_ok=True)

    print("PASS: leases, routing, source redaction, and provider-neutral Fly/Azure plans")


if __name__ == "__main__":
    main()
