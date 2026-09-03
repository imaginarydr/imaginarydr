#!/usr/bin/env python3
"""Connect Codex to Binance Agent OS through the official MCP OAuth flow."""
from __future__ import annotations

import json
import re
import select
import shutil
import subprocess
import threading
import time
from typing import Any


MCP_ENDPOINT = "https://agent.binance.com/mcp/agentic"
MCP_NAME = "binance-mcp-server"
LOGIN_URL = re.compile(r"https://accounts\.binance\.com/\S+")
_LOGIN_LOCK = threading.Lock()
_LOGIN_PROCESS: subprocess.Popen[str] | None = None


class AgentOsLoginError(RuntimeError):
    pass


def codex_path() -> str:
    value = shutil.which("codex")
    if not value:
        raise AgentOsLoginError("未找到 Codex CLI")
    return value


def _run(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise AgentOsLoginError("Binance Agent OS 授权超时，请重试") from error


def connection_status() -> dict[str, Any]:
    result = _run([codex_path(), "mcp", "list", "--json"], 15)
    if result.returncode != 0:
        raise AgentOsLoginError("无法读取 Codex MCP 配置")
    try:
        servers = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AgentOsLoginError("Codex MCP 配置响应无效") from error
    for server in servers if isinstance(servers, list) else []:
        transport = server.get("transport") if isinstance(server, dict) else {}
        if isinstance(transport, dict) and transport.get("url") == MCP_ENDPOINT:
            raw_auth_status = str(server.get("auth_status") or "unknown")
            if raw_auth_status in {"authenticated", "authorized"}:
                auth_status = "authorized"
            elif raw_auth_status in {"none", "not_logged_in", "unauthenticated"}:
                auth_status = "logged_out"
            else:
                auth_status = raw_auth_status
            return {
                "configured": True,
                "name": str(server.get("name") or MCP_NAME),
                "enabled": bool(server.get("enabled")),
                "authStatus": auth_status,
            }
    return {"configured": False, "name": MCP_NAME, "enabled": False, "authStatus": "none"}


def connect() -> dict[str, Any]:
    status = connection_status()
    name = str(status["name"])
    if not status["configured"]:
        added = _run([
            codex_path(), "mcp", "add", name, "--url", MCP_ENDPOINT,
            "--oauth-client-id", "codex",
        ], 60)
        if added.returncode != 0:
            raise AgentOsLoginError("添加 Binance MCP Server 失败")
    logged_in = _run([codex_path(), "mcp", "login", name], 360)
    if logged_in.returncode != 0:
        raise AgentOsLoginError("Binance 授权未完成")
    return {"configured": True, "name": name, "enabled": True, "authStatus": "authorized"}


def _finish_login(process: subprocess.Popen[str]) -> None:
    try:
        process.communicate(timeout=360)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
    finally:
        global _LOGIN_PROCESS
        with _LOGIN_LOCK:
            if _LOGIN_PROCESS is process:
                _LOGIN_PROCESS = None


def begin_connect() -> dict[str, Any]:
    """Start OAuth without blocking the dashboard request while the user signs in."""
    status = connection_status()
    name = str(status["name"])
    if not status["configured"]:
        added = _run([
            codex_path(), "mcp", "add", name, "--url", MCP_ENDPOINT,
            "--oauth-client-id", "codex",
        ], 60)
        if added.returncode != 0:
            raise AgentOsLoginError("添加 Binance MCP Server 失败")
    global _LOGIN_PROCESS
    with _LOGIN_LOCK:
        if _LOGIN_PROCESS and _LOGIN_PROCESS.poll() is None:
            return {"configured": True, "name": name, "enabled": True, "authStatus": "pending"}
        try:
            process = subprocess.Popen(
                [codex_path(), "mcp", "login", name], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except OSError as error:
            raise AgentOsLoginError("无法启动 Binance 授权") from error
        _LOGIN_PROCESS = process
    deadline = time.monotonic() + 15
    authorization_url = ""
    while process.poll() is None and time.monotonic() < deadline and process.stdout:
        ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        line = process.stdout.readline()
        match = LOGIN_URL.search(line)
        if match:
            authorization_url = match.group(0)
            break
    if process.poll() is not None:
        if process.returncode == 0:
            with _LOGIN_LOCK:
                _LOGIN_PROCESS = None
            return {"configured": True, "name": name, "enabled": True, "authStatus": "authorized"}
        with _LOGIN_LOCK:
            _LOGIN_PROCESS = None
        raise AgentOsLoginError("Binance 授权未能启动")
    threading.Thread(target=_finish_login, args=(process,), daemon=True).start()
    if not authorization_url:
        raise AgentOsLoginError("未能取得 Binance 授权链接")
    return {
        "configured": True, "name": name, "enabled": True,
        "authStatus": "pending", "authorizationUrl": authorization_url,
    }


def disconnect() -> dict[str, Any]:
    status = connection_status()
    if not status["configured"]:
        return {"configured": False, "name": MCP_NAME, "enabled": False, "authStatus": "logged_out"}
    name = str(status["name"])
    logged_out = _run([codex_path(), "mcp", "logout", name], 60)
    if logged_out.returncode != 0:
        raise AgentOsLoginError("退出 Binance Agent OS 失败")
    return {
        "configured": True,
        "name": name,
        "enabled": bool(status["enabled"]),
        "authStatus": "logged_out",
    }


if __name__ == "__main__":
    print(json.dumps(connect(), ensure_ascii=False))
