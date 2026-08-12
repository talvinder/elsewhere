import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src/agent_capacity/cli.py"


def request(message):
    return json.dumps(message, allow_nan=True)


class McpProtocolTests(unittest.TestCase):
    def run_server(self, lines):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                **os.environ,
                "AGENT_CAPACITY_STATE": str(Path(directory) / "leases.json"),
                "AGENT_CAPACITY_JOBS": str(Path(directory) / "jobs.json"),
                "AGENT_CAPACITY_TOTAL_MB": "18432",
                "AGENT_CAPACITY_MEMORY_LEVEL": "80",
            }
            result = subprocess.run(
                [sys.executable, str(CLI), "mcp-server"],
                input="\n".join(lines) + "\n",
                text=True, capture_output=True, env=env, check=True,
            )
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_parse_invalid_request_and_initialization_errors_are_protocol_errors(self):
        responses = self.run_server([
            "{bad-json",
            request({"jsonrpc": "1.0", "id": 1, "method": "ping"}),
            request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            request({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}}),
        ])
        self.assertEqual([item["error"]["code"] for item in responses], [-32700, -32600, -32002, -32602])

    def test_notifications_do_not_receive_json_rpc_responses(self):
        responses = self.run_server([
            request({"jsonrpc": "2.0", "method": "ping"}),
            request({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            request({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        ])
        self.assertEqual(responses, [{"jsonrpc": "2.0", "id": 1, "result": {}}])

    def test_initialized_notification_cannot_bypass_initialize_exchange(self):
        responses = self.run_server([
            request({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        ])
        self.assertEqual(responses[0]["error"]["code"], -32002)

    def test_tool_arguments_reject_missing_extra_nonfinite_and_out_of_range_values(self):
        initialize = request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        invalid_calls = [
            {"name": "elsewhere_queue", "arguments": {"extra": True}},
            {"name": "elsewhere_queue", "arguments": {"history_limit": -1}},
            {"name": "elsewhere_plan", "arguments": {"estimated_cost_usd": float("nan")}},
            {"name": "missing_tool", "arguments": {}},
        ]
        responses = self.run_server([
            initialize,
            *[
                request({"jsonrpc": "2.0", "id": index + 2, "method": "tools/call", "params": params})
                for index, params in enumerate(invalid_calls)
            ],
        ])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertTrue(all(item["error"]["code"] == -32602 for item in responses[1:]))

    def test_plan_and_dispatch_keep_distinct_read_only_annotations(self):
        responses = self.run_server([
            request({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }),
            request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ])
        tools = {item["name"]: item for item in responses[1]["result"]["tools"]}
        self.assertTrue(tools["elsewhere_plan"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["elsewhere_dispatch"]["annotations"]["readOnlyHint"])
        self.assertIn("approval_receipt", tools["elsewhere_dispatch"]["inputSchema"]["required"])


if __name__ == "__main__":
    unittest.main()
