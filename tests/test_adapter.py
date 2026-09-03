import json
import subprocess
import unittest
from unittest.mock import patch

import binance_agent_os as adapter


class AdapterTests(unittest.TestCase):
    @patch.object(adapter, "cli_path", return_value="/tmp/binance-cli")
    @patch.object(subprocess, "run")
    def test_cli_uses_argument_array_and_profile(self, run, _path):
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"ok": True}), "")
        result = adapter.run_cli("futures-usds", "mark-price", {"symbol": "BTCUSDT"}, profile="hedge-3")
        self.assertTrue(result["ok"])
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["/tmp/binance-cli", "futures-usds", "mark-price"])
        self.assertIn("--profile", argv)
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_profile_name_is_bounded(self):
        self.assertEqual(adapter.clean_profile("hedge-agent.3"), "hedge-agent.3")
        with self.assertRaises(adapter.AgentOsError):
            adapter.clean_profile("bad profile; rm")

    @patch.object(adapter, "cli_path", return_value="/tmp/binance-cli")
    @patch.object(subprocess, "run")
    def test_private_api_rejection_has_actionable_message(self, run, _path):
        run.return_value = subprocess.CompletedProcess([], 0, "Invalid API-key, IP, or permissions for action\n", "")
        with self.assertRaisesRegex(adapter.AgentOsError, "IP 白名单"):
            adapter.run_cli("futures-usds", "position-information-v3", {"symbol": "BTCUSDT"}, profile="hedger")

    @patch.object(adapter, "cli_path", return_value="/tmp/binance-cli")
    @patch.object(subprocess, "run")
    def test_profile_list_returns_metadata_without_credentials(self, run, _path):
        run.return_value = subprocess.CompletedProcess([], 0, "hedger (prod) *\nreview (demo)\n", "")
        self.assertEqual(adapter.list_profiles(), [
            {"name": "hedger", "environment": "prod", "active": True},
            {"name": "review", "environment": "demo", "active": False},
        ])

    @patch.object(adapter, "run_cli")
    def test_unknown_order_result_queries_same_client_id(self, run_cli):
        run_cli.side_effect = [adapter.AgentOsError("timeout"), {"status": "FILLED", "clientOrderId": "cid-1"}]
        result = adapter.submit_market_order("BTCUSDT", "SELL", "0.001", False, "cid-1", "hedge-3")
        self.assertTrue(result["recoveredByClientId"])
        query = run_cli.call_args_list[1]
        self.assertEqual(query.args[1], "query-order")
        self.assertEqual(query.args[2]["orig-client-order-id"], "cid-1")


if __name__ == "__main__":
    unittest.main()
