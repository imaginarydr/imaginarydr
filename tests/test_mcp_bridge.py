import json
import subprocess
import unittest
from unittest.mock import patch

import binance_mcp_bridge as bridge


class McpBridgeTests(unittest.TestCase):
    @patch.object(bridge, "codex_path", return_value="/tmp/codex")
    @patch.object(subprocess, "run")
    def test_usage_limit_is_not_reported_as_oauth_failure(self, run, _path):
        run.return_value = subprocess.CompletedProcess([], 1, "", "You've hit your usage limit")
        with self.assertRaisesRegex(bridge.AgentOsError, "使用额度已耗尽"):
            bridge._run_agent("read only")

    @patch.object(bridge, "codex_path", return_value="/tmp/codex")
    @patch.object(subprocess, "run")
    def test_auth_failure_is_reported_separately(self, run, _path):
        run.return_value = subprocess.CompletedProcess([], 1, "", "Auth required")
        with self.assertRaisesRegex(bridge.AgentOsError, "授权已失效"):
            bridge._run_agent("read only")

    @patch.object(bridge, "codex_path", return_value="/tmp/codex")
    @patch.object(subprocess, "run")
    def test_account_snapshot_uses_short_leg_summary(self, run, _path):
        payload = {
            "ok": True, "symbol": "BTCUSDT", "positionMode": "HEDGE",
            "positionAmount": "-0.001", "entryPrice": "80000", "markPrice": "79900",
            "liquidationPrice": "0", "unrealizedProfit": "0.1", "availableBalance": "5",
            "totalMarginBalance": "5.1", "leverage": "20", "marginType": "cross",
            "error": "",
        }
        run.return_value = subprocess.CompletedProcess([], 0, "log\n" + json.dumps(payload), "")
        result = bridge.account_snapshot("btcusdt")
        self.assertEqual(result["positionMode"], "HEDGE")
        self.assertEqual(result["positionAmount"], "-0.001")
        self.assertFalse(result["oneWayMode"])

    @patch.object(bridge, "_run_agent")
    def test_hedge_mode_order_uses_short_position_side(self, run_agent):
        run_agent.return_value = {
            "ok": True, "submitted": True, "recoveredByClientId": False,
            "order": {"status": "FILLED", "executedQty": "0.001", "avgPrice": "80000", "clientOrderId": "cid-1"},
        }
        result = bridge.submit_market_order("BTCUSDT", "SELL", "0.001", False, "cid-1", "HEDGE")
        self.assertEqual(result["order"]["status"], "FILLED")
        prompt = run_agent.call_args.args[0]
        self.assertIn('"positionSide":"SHORT"', prompt)
        self.assertNotIn('"reduceOnly"', prompt)

    @patch.object(bridge, "_run_agent")
    def test_one_way_reduce_order_keeps_reduce_only(self, run_agent):
        run_agent.return_value = {
            "ok": True, "submitted": True, "recoveredByClientId": False,
            "order": {"status": "FILLED", "executedQty": "0.001", "avgPrice": "80000", "clientOrderId": "cid-2"},
        }
        bridge.submit_market_order("BTCUSDT", "BUY", "0.001", True, "cid-2", "ONE_WAY")
        prompt = run_agent.call_args.args[0]
        self.assertIn('"positionSide":"BOTH"', prompt)
        self.assertIn('"reduceOnly":true', prompt)

    @patch.object(bridge, "_run_agent")
    def test_hedge_account_snapshot_combines_spot_and_short_leg(self, run_agent):
        run_agent.return_value = {
            "ok": True, "symbol": "BTCUSDT", "spotAsset": "BTC",
            "spotFree": "0.001", "spotLocked": "0.002",
            "positionMode": "HEDGE", "positionAmount": "-0.002",
            "entryPrice": "80000", "markPrice": "80100", "liquidationPrice": "0",
            "unrealizedProfit": "-0.2", "availableBalance": "5",
            "totalMarginBalance": "4.8", "leverage": "20", "marginType": "cross",
        }
        result = bridge.hedge_account_snapshot("btcusdt")
        self.assertEqual(result["spotQuantity"], "0.003")
        self.assertEqual(result["positionAmount"], "-0.002")
        self.assertIn("spot.getAccount", result["source"]["toolCalls"])
        self.assertIn("omitZeroBalances=true", run_agent.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
