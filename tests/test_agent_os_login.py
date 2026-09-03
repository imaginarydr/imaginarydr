import json
import subprocess
import unittest
from unittest.mock import patch

import agent_os_login as login


class AgentOsLoginTests(unittest.TestCase):
    @patch.object(login, "codex_path", return_value="/tmp/codex")
    @patch.object(subprocess, "run")
    def test_detects_configured_binance_mcp_without_credentials(self, run, _path):
        payload = [{
            "name": "binance-mcp-server", "enabled": True, "auth_status": "unknown",
            "transport": {"type": "streamable_http", "url": login.MCP_ENDPOINT},
        }]
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        self.assertEqual(login.connection_status()["name"], "binance-mcp-server")

    @patch.object(login, "connection_status", return_value={
        "configured": True, "name": "binance", "enabled": True, "authStatus": "unknown",
    })
    @patch.object(login, "codex_path", return_value="/tmp/codex")
    @patch.object(subprocess, "run")
    def test_connect_uses_official_oauth_login_without_api_keys(self, run, _path, _status):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        result = login.connect()
        self.assertEqual(result["authStatus"], "authorized")
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/tmp/codex", "mcp", "login", "binance"])

    @patch.object(login, "connection_status", return_value={
        "configured": True, "name": "binance", "enabled": True, "authStatus": "authorized",
    })
    @patch.object(login, "codex_path", return_value="/tmp/codex")
    @patch.object(subprocess, "run")
    def test_disconnect_uses_official_oauth_logout(self, run, _path, _status):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        result = login.disconnect()
        self.assertEqual(result["authStatus"], "logged_out")
        self.assertEqual(run.call_args.args[0], ["/tmp/codex", "mcp", "logout", "binance"])

    @patch.object(login, "codex_path", return_value="/tmp/codex")
    @patch.object(subprocess, "run")
    def test_normalizes_oauth_status(self, run, _path):
        payload = [{
            "name": "binance", "enabled": True, "auth_status": "o_auth",
            "transport": {"type": "streamable_http", "url": login.MCP_ENDPOINT},
        }]
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        self.assertEqual(login.connection_status()["authStatus"], "o_auth")


if __name__ == "__main__":
    unittest.main()
