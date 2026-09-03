import unittest
from pathlib import Path
from unittest.mock import patch

import server


class ServerTests(unittest.TestCase):
    def test_monitor_delay_is_measured_from_cycle_start(self):
        self.assertEqual(server.monitor_delay(60, 100, 135), 25)
        self.assertEqual(server.monitor_delay(60, 100, 170), 1)

    def test_local_request_validation_blocks_remote_web_origins(self):
        self.assertTrue(server.is_local_host("127.0.0.1:8815"))
        self.assertTrue(server.is_local_host("localhost:8815"))
        self.assertFalse(server.is_local_host("attacker.example:8815"))
        self.assertTrue(server.is_trusted_origin("http://127.0.0.1:8815"))
        self.assertTrue(server.is_trusted_origin(""))
        self.assertFalse(server.is_trusted_origin("https://attacker.example"))

    @patch.object(server, "connection_status")
    def test_successful_account_read_marks_agent_os_verified(self, connection):
        connection.return_value = {"configured": True, "enabled": True, "authStatus": "o_auth"}
        previous = server.STATE.get("account")
        try:
            server.STATE["account"] = {"symbol": "BTCUSDT"}
            self.assertTrue(server.agent_os_status()["verified"])
        finally:
            server.STATE["account"] = previous

    def test_logout_state_cleanup_stops_monitor_and_removes_private_snapshot(self):
        previous_state = dict(server.STATE)
        previous_tickets = dict(server.PENDING_TICKETS)
        try:
            server.STATE.update({"monitorEnabled": True, "account": {"symbol": "BTCUSDT"}, "spotQuantity": "1", "autoTicket": {"ticketId": "x"}, "lastError": "old"})
            server.PENDING_TICKETS["x"] = {"ticketId": "x"}
            server.clear_private_state()
            self.assertFalse(server.STATE["monitorEnabled"])
            self.assertIsNone(server.STATE["account"])
            self.assertIsNone(server.STATE["spotQuantity"])
            self.assertEqual(server.STATE["phase"], "已退出")
            self.assertEqual(server.PENDING_TICKETS, {})
        finally:
            server.STATE.clear()
            server.STATE.update(previous_state)
            server.PENDING_TICKETS.clear()
            server.PENDING_TICKETS.update(previous_tickets)

    def test_execution_requires_exact_confirmation(self):
        with self.assertRaisesRegex(ValueError, "CONFIRM"):
            server.execute_ticket({"confirmation": "confirm"})

    def test_close_requires_exact_confirmation(self):
        with self.assertRaisesRegex(ValueError, "CONFIRM"):
            server.execute_ticket({"confirmation": "确认"})

    def test_dashboard_has_live_controls_without_secret_api_fields(self):
        html = (Path(server.ROOT) / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("实时分析", html)
        self.assertIn("对齐空仓", html)
        self.assertIn("连接 Binance", html)
        self.assertIn("验证 Agent OS", html)
        self.assertIn("退出登录", html)
        self.assertIn("跟随币安现货", html)
        self.assertIn("币安现货余额", html)
        self.assertNotIn("CLI API（可选）", html)
        self.assertNotIn("验证 CLI API", html)
        self.assertNotIn("CLI Profile", html)
        self.assertNotIn("确认指令", html)
        self.assertNotIn("输入 CONFIRM", html)
        self.assertIn("confirmModal", html)
        self.assertIn("确认执行", html)
        self.assertIn("busyPanel", html)
        self.assertNotIn("API Secret", html)
        self.assertNotIn("API Key", html)

    @patch.object(server, "submit_market_order")
    def test_invalid_confirmation_never_reaches_order_tool(self, submit):
        with self.assertRaises(ValueError):
            server.execute_ticket({"confirmation": "CONFIRM "})
        submit.assert_not_called()

    @patch.object(server, "account_snapshot")
    @patch.object(server, "analyze")
    @patch.object(server, "fetch_market")
    @patch.object(server, "save_config")
    def test_preview_blocks_new_short_without_margin(self, save, fetch, analyze, account):
        config = dict(server.DEFAULT_CONFIG)
        config["followSpotBalance"] = False
        save.return_value = config
        fetch.return_value = {"futuresBids": [["80000", "1"]], "futuresAsks": [["80001", "1"]]}
        analyze.return_value = {
            "decision": "PASS", "exposure": {"targetShortQuantity": "0.001"},
            "rules": {"stepSize": "0.001", "minQty": "0.001"},
        }
        account.return_value = {"positionAmount": "0", "positionMode": "HEDGE", "availableBalance": "0"}
        with self.assertRaisesRegex(ValueError, "没有可用保证金"):
            server.prepare_ticket(config, "reconcile")

    @patch.object(server, "account_snapshot")
    @patch.object(server, "analyze")
    @patch.object(server, "fetch_market")
    @patch.object(server, "save_config")
    def test_preview_returns_exact_ticket(self, save, fetch, analyze, account):
        config = dict(server.DEFAULT_CONFIG)
        config["followSpotBalance"] = False
        save.return_value = config
        fetch.return_value = {"futuresBids": [["80000", "1"]], "futuresAsks": [["80001", "1"]]}
        analyze.return_value = {
            "decision": "PASS", "exposure": {"targetShortQuantity": "0.001"},
            "rules": {"stepSize": "0.001", "minQty": "0.001"},
        }
        account.return_value = {"positionAmount": "0", "positionMode": "HEDGE", "availableBalance": "5"}
        result = server.prepare_ticket(config, "reconcile")
        self.assertEqual(result["ticket"]["side"], "SELL")
        self.assertEqual(result["ticket"]["positionSide"], "SHORT")
        self.assertEqual(result["ticket"]["tool"], "futures_usds.newOrder")
        self.assertNotIn("config", result["ticket"])

    def test_follow_mode_uses_live_spot_quantity(self):
        config = {**server.DEFAULT_CONFIG, "exposureQuantity": "9", "followSpotBalance": True}
        result = server.effective_config(config, {"spotQuantity": "0.125"})
        self.assertEqual(result["exposureQuantity"], "0.125")
        self.assertEqual(config["exposureQuantity"], "9")

    def test_auto_follow_creates_visible_confirmation_ticket(self):
        config = {**server.DEFAULT_CONFIG, "exposureQuantity": "0.002", "followSpotBalance": True}
        report = {
            "decision": "PASS", "exposure": {"targetShortQuantity": "0.002"},
            "rules": {"stepSize": "0.001", "minQty": "0.001"},
        }
        snapshot = {"futuresBids": [["80000", "1"]], "futuresAsks": [["80001", "1"]]}
        account = {"positionAmount": "0", "availableBalance": "5", "positionMode": "HEDGE", "spotQuantity": "0.002"}
        server.PENDING_TICKETS.clear()
        server.STATE["autoTicket"] = None
        result = server.create_ticket(config, report, snapshot, account, "reconcile", automatic=True)
        self.assertIsNotNone(result["ticket"])
        self.assertEqual(server.STATE["autoTicket"]["ticketId"], result["ticket"]["ticketId"])
        self.assertNotIn("config", server.STATE["autoTicket"])
        server.discard_ticket(result["ticket"]["ticketId"])


if __name__ == "__main__":
    unittest.main()
