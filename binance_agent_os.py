#!/usr/bin/env python3
"""Binance Agent OS adapter backed by the official @binance/binance-cli."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
CLI_TIMEOUT_SECONDS = 35
_CACHE_LOCK = threading.RLock()
_CACHE: dict[str, tuple[float, Any]] = {}


class AgentOsError(RuntimeError):
    pass


def cli_path() -> str:
    configured = os.environ.get("BINANCE_CLI", "").strip()
    local = ROOT / "node_modules" / ".bin" / "binance-cli"
    for candidate in (configured, str(local) if local.is_file() else "", shutil.which("binance-cli") or ""):
        if candidate and Path(candidate).is_file():
            return candidate
    raise AgentOsError("Binance Agent OS CLI 未安装，请先运行 npm install")


def clean_profile(profile: str | None) -> str:
    value = str(profile or "").strip()
    if value and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        raise AgentOsError("Binance profile 名称格式无效")
    return value


def list_profiles() -> list[dict[str, Any]]:
    """Return CLI profile metadata without reading or exposing credentials."""
    try:
        result = subprocess.run(
            [cli_path(), "profile", "list"], cwd=str(ROOT), capture_output=True,
            text=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise AgentOsError("读取 Binance API 配置超时") from error
    if result.returncode != 0:
        message = (result.stderr.strip() or result.stdout.strip() or "调用失败").splitlines()[-1][:220]
        raise AgentOsError(f"读取 Binance API 配置失败：{message}")
    profiles: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"\s*([A-Za-z0-9_.-]{1,64})\s+\((prod|testnet|demo)\)(\s+\*)?\s*", line)
        if match:
            profiles.append({"name": match.group(1), "environment": match.group(2), "active": bool(match.group(3))})
    return profiles


def run_cli(product: str, command: str, arguments: dict[str, Any] | None = None,
            *, profile: str | None = None, timeout: int = CLI_TIMEOUT_SECONDS) -> Any:
    argv = [cli_path(), product, command]
    for key, raw in (arguments or {}).items():
        if raw is None:
            continue
        value = str(raw).lower() if isinstance(raw, bool) else str(raw)
        argv.extend([f"--{key}", value])
    selected = clean_profile(profile)
    if selected:
        argv.extend(["--profile", selected])
    try:
        result = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise AgentOsError(f"Agent OS {product}.{command} 超时") from error
    output = result.stdout.strip()
    if len(output.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise AgentOsError(f"Agent OS {product}.{command} 响应过大")
    if output == "Invalid API-key, IP, or permissions for action":
        raise AgentOsError("Binance API 连接被拒绝，请检查 Key、IP 白名单和合约交易权限")
    if output == "Request failed after 3 retries":
        raise AgentOsError(f"Agent OS {product}.{command} 网络请求失败")
    if result.returncode != 0:
        message = (result.stderr.strip() or output or "调用失败").splitlines()[-1][:220]
        raise AgentOsError(f"Agent OS {product}.{command}：{message}")
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise AgentOsError(f"Agent OS {product}.{command} 返回了无效 JSON") from error


def cached(key: str, seconds: int, loader) -> Any:
    with _CACHE_LOCK:
        row = _CACHE.get(key)
        if row and time.time() - row[0] < seconds:
            return row[1]
    value = loader()
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), value)
    return value


def _find_symbol(payload: dict[str, Any], symbol: str) -> dict[str, Any]:
    for row in payload.get("symbols", []):
        if row.get("symbol") == symbol and row.get("status") == "TRADING" and row.get("contractType") == "PERPETUAL":
            return row
    raise AgentOsError(f"{symbol} 不是可交易的 USDT 永续合约")


def _rules(symbol: str) -> dict[str, str]:
    payload = cached(
        "futures.exchange-information",
        900,
        lambda: run_cli("futures-usds", "exchange-information"),
    )
    row = _find_symbol(payload, symbol)
    filters = {item.get("filterType"): item for item in row.get("filters", [])}
    lot = filters.get("LOT_SIZE") or {}
    minimum = filters.get("MIN_NOTIONAL") or {}
    return {
        "stepSize": str(lot.get("stepSize") or "0"),
        "minQty": str(lot.get("minQty") or "0"),
        "maxQty": str(lot.get("maxQty") or "0"),
        "minNotional": str(minimum.get("notional") or "0"),
    }


def _funding_interval(symbol: str) -> int:
    payload = cached(
        "futures.get-funding-rate-info",
        900,
        lambda: run_cli("futures-usds", "get-funding-rate-info"),
    )
    rows = payload if isinstance(payload, list) else []
    for row in rows:
        if row.get("symbol") == symbol:
            try:
                return max(1, int(row.get("fundingIntervalHours") or 8))
            except (TypeError, ValueError):
                break
    return 8


def fetch_market(symbol: str) -> dict[str, Any]:
    market = str(symbol or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,20}USDT", market):
        raise AgentOsError("交易对格式无效")
    calls = [
        ("spot", "ticker-book-ticker", {"symbol": market}),
        ("spot", "ticker24hr", {"symbol": market}),
        ("futures-usds", "mark-price", {"symbol": market}),
        ("futures-usds", "order-book", {"symbol": market, "limit": 100}),
        ("futures-usds", "ticker24hr-price-change-statistics", {"symbol": market}),
    ]
    with ThreadPoolExecutor(max_workers=len(calls) + 2) as pool:
        futures = [pool.submit(run_cli, product, command, params) for product, command, params in calls]
        rules_future = pool.submit(_rules, market)
        interval_future = pool.submit(_funding_interval, market)
        spot_book, spot_ticker, mark, futures_book, futures_ticker = [future.result() for future in futures]
        rules = rules_future.result()
        interval = interval_future.result()
    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "symbol": market,
        "spotBid": spot_book.get("bidPrice"),
        "spotAsk": spot_book.get("askPrice"),
        "futuresBid": (futures_book.get("bids") or [[None]])[0][0],
        "futuresAsk": (futures_book.get("asks") or [[None]])[0][0],
        "futuresBids": futures_book.get("bids") or [],
        "futuresAsks": futures_book.get("asks") or [],
        "markPrice": mark.get("markPrice"),
        "lastFundingRate": mark.get("lastFundingRate"),
        "nextFundingTime": mark.get("nextFundingTime"),
        "fundingIntervalHours": interval,
        "spotQuoteVolume24h": spot_ticker.get("quoteVolume"),
        "futuresQuoteVolume24h": futures_ticker.get("quoteVolume"),
        "rules": rules,
        "fetchedAt": fetched_at,
        "source": {
            "provider": "Binance Agent OS",
            "adapter": "@binance/binance-cli",
            "mode": "live",
            "toolCalls": [
                "spot.ticker-book-ticker",
                "spot.ticker24hr",
                "futures-usds.mark-price",
                "futures-usds.order-book",
                "futures-usds.ticker24hr-price-change-statistics",
                "futures-usds.exchange-information",
                "futures-usds.get-funding-rate-info",
            ],
        },
    }


def account_snapshot(symbol: str, profile: str) -> dict[str, Any]:
    selected = clean_profile(profile)
    if not selected:
        raise AgentOsError("请输入独立的 Binance profile 名称")
    market = str(symbol).upper()
    calls = [
        ("position-information-v3", {"symbol": market}),
        ("symbol-configuration", {"symbol": market}),
        ("account-information-v3", {}),
        ("get-current-position-mode", {}),
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = [pool.submit(run_cli, "futures-usds", command, params, profile=selected) for command, params in calls]
        positions, configuration, account, mode = [future.result() for future in rows]
    position = next((row for row in positions if row.get("symbol") == market), {}) if isinstance(positions, list) else {}
    config_row = next((row for row in configuration if row.get("symbol") == market), {}) if isinstance(configuration, list) else configuration
    return {
        "symbol": market,
        "positionAmount": str(position.get("positionAmt") or "0"),
        "entryPrice": str(position.get("entryPrice") or "0"),
        "markPrice": str(position.get("markPrice") or "0"),
        "liquidationPrice": str(position.get("liquidationPrice") or "0"),
        "unrealizedProfit": str(position.get("unRealizedProfit") or position.get("unrealizedProfit") or "0"),
        "availableBalance": str(account.get("availableBalance") or "0") if isinstance(account, dict) else "0",
        "totalMarginBalance": str(account.get("totalMarginBalance") or "0") if isinstance(account, dict) else "0",
        "leverage": str(config_row.get("leverage") or position.get("leverage") or ""),
        "marginType": str(config_row.get("marginType") or position.get("marginType") or ""),
        "oneWayMode": not bool(mode.get("dualSidePosition")) if isinstance(mode, dict) else False,
        "source": {
            "provider": "Binance Agent OS",
            "adapter": "@binance/binance-cli",
            "toolCalls": [
                "futures-usds.position-information-v3",
                "futures-usds.symbol-configuration",
                "futures-usds.account-information-v3",
                "futures-usds.get-current-position-mode",
            ],
        },
    }


def submit_market_order(symbol: str, side: str, quantity: str, reduce_only: bool,
                        client_order_id: str, profile: str) -> dict[str, Any]:
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity,
        "new-client-order-id": client_order_id,
        "new-order-resp-type": "RESULT",
    }
    if reduce_only:
        params["reduce-only"] = True
    try:
        submitted = run_cli("futures-usds", "new-order", params, profile=profile, timeout=45)
    except AgentOsError as submit_error:
        try:
            queried = run_cli(
                "futures-usds",
                "query-order",
                {"symbol": symbol, "orig-client-order-id": client_order_id},
                profile=profile,
            )
        except AgentOsError as query_error:
            raise AgentOsError(f"订单结果未知；未重发。{submit_error}；查询失败：{query_error}") from query_error
        return {"submitted": None, "order": queried, "recoveredByClientId": True}
    return {"submitted": True, "order": submitted, "recoveredByClientId": False}
