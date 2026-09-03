#!/usr/bin/env python3
"""Private Binance Agent OS operations through the authenticated Codex MCP client."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from agent_os_login import codex_path
from binance_agent_os import AgentOsError


ROOT = Path(__file__).resolve().parent
MODEL = os.environ.get("FUNDING_HEDGE_CODEX_MODEL", "gpt-5.6-luna")
_LOCK = threading.Lock()


def _json_line(output: str) -> dict[str, Any]:
    for raw in reversed(output.splitlines()):
        line = raw.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise AgentOsError("Binance MCP 没有返回可解析的结果")


def _run_agent(prompt: str, timeout: int = 150) -> dict[str, Any]:
    command = [
        codex_path(), "exec", "--ephemeral", "--sandbox", "read-only",
        "--skip-git-repo-check", "--color", "never", "-m", MODEL,
        "-c", "model_reasoning_effort='low'", prompt,
    ]
    try:
        with _LOCK:
            result = subprocess.run(
                command, cwd=str(ROOT), capture_output=True, text=True,
                timeout=timeout, check=False,
            )
    except subprocess.TimeoutExpired as error:
        raise AgentOsError("Binance MCP 调用超时") from error
    if result.returncode != 0:
        diagnostic = f"{result.stdout}\n{result.stderr}".lower()
        if "usage limit" in diagnostic:
            raise AgentOsError(f"{MODEL} 使用额度已耗尽，请更换 FUNDING_HEDGE_CODEX_MODEL")
        if "auth required" in diagnostic or "logged in" in diagnostic:
            raise AgentOsError("Binance MCP 授权已失效，请重新连接 Binance")
        raise AgentOsError("Binance MCP 调用失败")
    value = _json_line(result.stdout)
    if not value.get("ok"):
        raise AgentOsError(str(value.get("error") or "Binance MCP 返回失败"))
    return value


def _symbol(value: str) -> str:
    symbol = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,20}USDT", symbol):
        raise AgentOsError("交易对格式无效")
    return symbol


def _quantity(value: str) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise AgentOsError("订单数量无效") from error
    if not parsed.is_finite() or parsed <= 0:
        raise AgentOsError("订单数量必须大于 0")
    return format(parsed, "f")


def account_snapshot(symbol: str) -> dict[str, Any]:
    market = _symbol(symbol)
    prompt = f"""
只使用 Binance MCP 的只读工具，禁止下单、撤单、转账、调杠杆或修改账户。
并行读取 USDⓈ-M 的 futures_usds.positionInformationV2（只查 {market}）和 futures_usds.accountInformationV3。
从账户信息读取可用保证金和总保证金，从仓位信息读取该交易对仓位；不要调用其他工具。
双向持仓时只选择 positionSide=SHORT 的腿，并把 positionAmount 规范为负数或 0；不要把 LONG 腿混入。
只返回一行 JSON，不要 Markdown：
{{"ok":true,"symbol":"{market}","positionMode":"ONE_WAY或HEDGE","positionAmount":"字符串","entryPrice":"字符串","markPrice":"字符串","liquidationPrice":"字符串","unrealizedProfit":"字符串","availableBalance":"字符串","totalMarginBalance":"字符串","leverage":"字符串","marginType":"字符串","error":""}}
不要输出 UID、完整资产列表或其他隐私标识。失败时返回 ok=false 和简短 error。
""".strip()
    value = _run_agent(prompt)
    mode = str(value.get("positionMode") or "").upper()
    if mode not in {"ONE_WAY", "HEDGE"}:
        raise AgentOsError("Binance MCP 返回了无效持仓模式")
    return {
        "symbol": market,
        "positionAmount": str(value.get("positionAmount") or "0"),
        "entryPrice": str(value.get("entryPrice") or "0"),
        "markPrice": str(value.get("markPrice") or "0"),
        "liquidationPrice": str(value.get("liquidationPrice") or "0"),
        "unrealizedProfit": str(value.get("unrealizedProfit") or "0"),
        "availableBalance": str(value.get("availableBalance") or "0"),
        "totalMarginBalance": str(value.get("totalMarginBalance") or "0"),
        "leverage": str(value.get("leverage") or ""),
        "marginType": str(value.get("marginType") or ""),
        "positionMode": mode,
        "oneWayMode": mode == "ONE_WAY",
        "source": {
            "provider": "Binance Agent OS",
            "adapter": "Binance MCP OAuth",
            "toolCalls": [
                "futures_usds.positionInformationV2",
                "futures_usds.accountInformationV3",
            ],
        },
    }


def hedge_account_snapshot(symbol: str) -> dict[str, Any]:
    market = _symbol(symbol)
    asset = market[:-4]
    prompt = f"""
只使用 Binance MCP 的只读工具，禁止下单、撤单、转账、调杠杆或修改账户。
先用 Binance MCP tool_search 找到并调用 spot.getAccount（omitZeroBalances=true），只提取 {asset} 的 free 和 locked；
同时读取 USDⓈ-M 的 futures_usds.positionInformationV2（只查 {market}）和 futures_usds.accountInformationV3。
双向持仓时只选择 positionSide=SHORT 的腿，并把 positionAmount 规范为负数或 0；不要把 LONG 腿混入。
只返回一行 JSON，不要 Markdown：
{{"ok":true,"symbol":"{market}","spotAsset":"{asset}","spotFree":"字符串","spotLocked":"字符串","positionMode":"ONE_WAY或HEDGE","positionAmount":"字符串","entryPrice":"字符串","markPrice":"字符串","liquidationPrice":"字符串","unrealizedProfit":"字符串","availableBalance":"字符串","totalMarginBalance":"字符串","leverage":"字符串","marginType":"字符串","error":""}}
不要输出 UID、其他资产、完整资产列表或其他隐私标识。失败时返回 ok=false 和简短 error。
""".strip()
    value = _run_agent(prompt)
    mode = str(value.get("positionMode") or "").upper()
    if mode not in {"ONE_WAY", "HEDGE"}:
        raise AgentOsError("Binance MCP 返回了无效持仓模式")
    try:
        spot_free = Decimal(str(value.get("spotFree") or "0"))
        spot_locked = Decimal(str(value.get("spotLocked") or "0"))
    except InvalidOperation as error:
        raise AgentOsError("Binance MCP 返回了无效现货余额") from error
    if not spot_free.is_finite() or not spot_locked.is_finite() or min(spot_free, spot_locked) < 0:
        raise AgentOsError("Binance MCP 返回了无效现货余额")
    return {
        "symbol": market,
        "spotAsset": asset,
        "spotFree": format(spot_free, "f"),
        "spotLocked": format(spot_locked, "f"),
        "spotQuantity": format(spot_free + spot_locked, "f"),
        "positionAmount": str(value.get("positionAmount") or "0"),
        "entryPrice": str(value.get("entryPrice") or "0"),
        "markPrice": str(value.get("markPrice") or "0"),
        "liquidationPrice": str(value.get("liquidationPrice") or "0"),
        "unrealizedProfit": str(value.get("unrealizedProfit") or "0"),
        "availableBalance": str(value.get("availableBalance") or "0"),
        "totalMarginBalance": str(value.get("totalMarginBalance") or "0"),
        "leverage": str(value.get("leverage") or ""),
        "marginType": str(value.get("marginType") or ""),
        "positionMode": mode,
        "oneWayMode": mode == "ONE_WAY",
        "source": {
            "provider": "Binance Agent OS",
            "adapter": "Binance MCP OAuth",
            "toolCalls": [
                "spot.getAccount",
                "futures_usds.positionInformationV2",
                "futures_usds.accountInformationV3",
            ],
        },
    }


def submit_market_order(symbol: str, side: str, quantity: str, reduce_only: bool,
                        client_order_id: str, position_mode: str) -> dict[str, Any]:
    market = _symbol(symbol)
    order_side = str(side).upper()
    if order_side not in {"BUY", "SELL"}:
        raise AgentOsError("订单方向无效")
    order_quantity = _quantity(quantity)
    mode = str(position_mode).upper()
    if mode not in {"ONE_WAY", "HEDGE"}:
        raise AgentOsError("持仓模式无效")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,36}", client_order_id):
        raise AgentOsError("订单客户端 ID 无效")
    parameters: dict[str, Any] = {
        "symbol": market,
        "side": order_side,
        "type": "MARKET",
        "quantity": order_quantity,
        "positionSide": "SHORT" if mode == "HEDGE" else "BOTH",
        "newClientOrderId": client_order_id,
        "newOrderRespType": "RESULT",
    }
    if mode == "ONE_WAY" and reduce_only:
        parameters["reduceOnly"] = True
    prompt = f"""
用户刚刚在网页中针对以下精确订单票据输入了 CONFIRM。只使用 Binance MCP。
调用 futures_usds.newOrder 一次，参数为：{json.dumps(parameters, ensure_ascii=False, separators=(',', ':'))}
不得修改参数，不得调杠杆、转账或提交第二笔订单。提交后用相同 symbol 和 origClientOrderId 查询一次订单状态。
如果提交结果不确定，只查询同一 client ID，绝对不要重发。
只返回一行 JSON，不要 Markdown：
{{"ok":true,"submitted":true,"recoveredByClientId":false,"order":{{"status":"字符串","executedQty":"字符串","avgPrice":"字符串","clientOrderId":"字符串"}},"error":""}}
失败时返回 ok=false 和简短 error。
""".strip()
    value = _run_agent(prompt, timeout=180)
    order = value.get("order") if isinstance(value.get("order"), dict) else {}
    if str(order.get("clientOrderId") or "") != client_order_id:
        raise AgentOsError("Binance MCP 返回的订单 ID 不匹配")
    return {
        "submitted": bool(value.get("submitted")),
        "recoveredByClientId": bool(value.get("recoveredByClientId")),
        "order": order,
    }
