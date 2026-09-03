#!/usr/bin/env python3
"""Deterministic exposure and funding-aware hedge calculations."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Iterable


ZERO = Decimal("0")
ONE = Decimal("1")
TEN_THOUSAND = Decimal("10000")


def number(value: Any, field: str, *, minimum: Decimal | None = None) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} 必须是数字") from error
    if not parsed.is_finite():
        raise ValueError(f"{field} 必须是有限数字")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field} 不能小于 {minimum}")
    return parsed


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        raise ValueError("数量步长必须大于 0")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized else "0"


def display(value: Decimal, places: int = 4) -> str:
    quantum = ONE.scaleb(-places)
    return text(value.quantize(quantum))


def funding_annual_pct(rate: Any, interval_hours: Any = 8) -> Decimal:
    funding = number(rate, "资金费率")
    interval = number(interval_hours, "资金费率周期", minimum=Decimal("1"))
    return funding * (Decimal("24") / interval) * Decimal("36500")


def walk_book(levels: Iterable[Iterable[Any]], quantity: Any, side: str) -> dict[str, Any]:
    requested = number(quantity, "对冲数量", minimum=ZERO)
    remaining = requested
    quote = ZERO
    best = ZERO
    used = 0
    for raw in levels:
        row = list(raw)
        if len(row) < 2:
            continue
        price = number(row[0], "盘口价格", minimum=ZERO)
        available = number(row[1], "盘口数量", minimum=ZERO)
        if price <= ZERO or available <= ZERO:
            continue
        if best == ZERO:
            best = price
        take = min(remaining, available)
        quote += take * price
        remaining -= take
        used += 1
        if remaining <= ZERO:
            break
    filled = requested > ZERO and remaining <= ZERO
    average = quote / requested if filled else ZERO
    normalized_side = side.upper()
    if not filled or best == ZERO:
        slippage = ZERO
    elif normalized_side == "SELL":
        slippage = max(ZERO, (best - average) / best * TEN_THOUSAND)
    elif normalized_side == "BUY":
        slippage = max(ZERO, (average - best) / best * TEN_THOUSAND)
    else:
        raise ValueError("方向必须是 BUY 或 SELL")
    return {
        "filled": filled,
        "requestedQuantity": text(requested),
        "filledQuantity": text(requested - max(remaining, ZERO)),
        "averagePrice": text(average),
        "notionalUsdt": text(quote),
        "slippageBps": text(slippage),
        "levelsUsed": used,
    }


def _control(control_id: str, label: str, passed: bool, severity: str, actual: str) -> dict[str, Any]:
    return {
        "id": control_id,
        "label": label,
        "passed": bool(passed),
        "severity": severity,
        "actual": actual,
    }


def analyze(snapshot: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    symbol = str(intent.get("symbol") or "").strip().upper()
    if not symbol.endswith("USDT") or len(symbol) < 7:
        raise ValueError("交易对必须是 USDT 永续合约，例如 BTCUSDT")

    exposure = number(intent.get("exposureQuantity"), "现货敞口数量", minimum=ZERO)
    if exposure < ZERO:
        raise ValueError("现货敞口数量不能小于 0")
    hedge_ratio = number(intent.get("hedgeRatioPct", 100), "套保比例", minimum=ZERO)
    if hedge_ratio > Decimal("100"):
        raise ValueError("套保比例不能超过 100%")

    max_notional = number(intent.get("maxNotionalUsdt", 10000), "最大套保名义金额", minimum=ZERO)
    max_slippage = number(intent.get("maxSlippageBps", 10), "最大滑点", minimum=ZERO)
    max_basis = number(intent.get("maxBasisBps", 100), "最大基差", minimum=ZERO)
    max_funding_cost = number(intent.get("maxFundingCostAnnualPct", 20), "最大年化资金费率成本", minimum=ZERO)
    min_volume = number(intent.get("minQuoteVolumeUsdt", 10000000), "最低成交额", minimum=ZERO)

    rules = snapshot.get("rules") or {}
    step = number(rules.get("stepSize"), "数量步长", minimum=ZERO)
    min_qty = number(rules.get("minQty"), "最小数量", minimum=ZERO)
    min_notional = number(rules.get("minNotional"), "最小名义金额", minimum=ZERO)
    target = floor_step(exposure * hedge_ratio / Decimal("100"), step)

    spot_bid = number(snapshot.get("spotBid"), "现货买一", minimum=ZERO)
    spot_ask = number(snapshot.get("spotAsk"), "现货卖一", minimum=ZERO)
    futures_bid = number(snapshot.get("futuresBid"), "合约买一", minimum=ZERO)
    futures_ask = number(snapshot.get("futuresAsk"), "合约卖一", minimum=ZERO)
    mark = number(snapshot.get("markPrice"), "标记价格", minimum=ZERO)
    if min(spot_bid, spot_ask, futures_bid, futures_ask, mark) <= ZERO:
        raise ValueError("市场价格无效")

    spot_mid = (spot_bid + spot_ask) / Decimal("2")
    futures_mid = (futures_bid + futures_ask) / Decimal("2")
    basis_bps = (futures_mid - spot_mid) / spot_mid * TEN_THOUSAND
    funding_rate = number(snapshot.get("lastFundingRate"), "资金费率")
    funding_interval = number(snapshot.get("fundingIntervalHours", 8), "资金费率周期", minimum=Decimal("1"))
    annual_funding = funding_annual_pct(funding_rate, funding_interval)
    short_funding_cost = max(ZERO, -annual_funding)
    notional = target * mark
    book = walk_book(snapshot.get("futuresBids") or [], target, "SELL")
    slippage = number(book["slippageBps"], "预计滑点", minimum=ZERO)
    spot_volume = number(snapshot.get("spotQuoteVolume24h", 0), "现货成交额", minimum=ZERO)
    futures_volume = number(snapshot.get("futuresQuoteVolume24h", 0), "合约成交额", minimum=ZERO)

    controls = [
        _control("quantity", "满足合约最小数量", target == ZERO or target >= min_qty, "BLOCK", f"{text(target)} / 最低 {text(min_qty)}"),
        _control("minimumNotional", "满足合约最小名义金额", target == ZERO or notional >= min_notional, "BLOCK", f"{display(notional)} / 最低 {text(min_notional)} USDT"),
        _control("positionLimit", "不超过最大套保金额", notional <= max_notional, "BLOCK", f"{display(notional)} / 上限 {text(max_notional)} USDT"),
        _control("orderbookFill", "盘口可覆盖目标数量", target == ZERO or bool(book["filled"]), "BLOCK", f"已覆盖 {book['filledQuantity']}"),
        _control("slippage", "预计滑点不超过限制", target == ZERO or (bool(book["filled"]) and slippage <= max_slippage), "BLOCK", f"{display(slippage)} / 上限 {text(max_slippage)} bps"),
        _control("basis", "现货合约基差不超过限制", abs(basis_bps) <= max_basis, "WARN", f"{display(basis_bps)} / 上限 ±{text(max_basis)} bps"),
        _control("fundingCost", "空仓年化资金费率成本不超过限制", short_funding_cost <= max_funding_cost, "WARN", f"{display(short_funding_cost)}% / 上限 {text(max_funding_cost)}%"),
        _control("spotLiquidity", "现货24小时成交额达到要求", spot_volume >= min_volume, "WARN", f"{display(spot_volume, 2)} USDT"),
        _control("futuresLiquidity", "合约24小时成交额达到要求", futures_volume >= min_volume, "WARN", f"{display(futures_volume, 2)} USDT"),
    ]
    hard_failed = [row for row in controls if not row["passed"] and row["severity"] == "BLOCK"]
    soft_failed = [row for row in controls if not row["passed"] and row["severity"] == "WARN"]
    decision = "BLOCK" if hard_failed else "WARN" if soft_failed else "PASS"

    return {
        "symbol": symbol,
        "baseAsset": symbol[:-4],
        "decision": decision,
        "exposure": {
            "quantity": text(exposure),
            "hedgeRatioPct": text(hedge_ratio),
            "targetShortQuantity": text(target),
            "targetNotionalUsdt": text(notional),
        },
        "market": {
            "spotMid": text(spot_mid),
            "futuresMid": text(futures_mid),
            "markPrice": text(mark),
            "basisBps": text(basis_bps),
            "lastFundingRatePct": text(funding_rate * Decimal("100")),
            "fundingIntervalHours": text(funding_interval),
            "shortFundingAnnualPct": text(annual_funding),
            "nextFundingTime": snapshot.get("nextFundingTime"),
            "spotQuoteVolume24h": text(spot_volume),
            "futuresQuoteVolume24h": text(futures_volume),
        },
        "executionEstimate": book,
        "controls": controls,
        "failedControls": [row for row in controls if not row["passed"]],
        "rules": {
            "stepSize": text(step),
            "minQty": text(min_qty),
            "minNotional": text(min_notional),
        },
        "source": snapshot.get("source") or {},
        "generatedAt": snapshot.get("fetchedAt"),
    }


def plan_reconcile(current_position: Any, target_short_quantity: Any, step_size: Any, min_qty: Any) -> dict[str, Any]:
    current = number(current_position, "当前合约仓位")
    target_abs = number(target_short_quantity, "目标空仓数量", minimum=ZERO)
    step = number(step_size, "数量步长", minimum=ZERO)
    minimum = number(min_qty, "最小数量", minimum=ZERO)
    if current > ZERO:
        return {"action": "BLOCK", "reason": "当前存在多仓，拒绝自动反向穿仓", "currentPosition": text(current)}
    target = -floor_step(target_abs, step)
    delta = target - current
    quantity = floor_step(abs(delta), step)
    if quantity < minimum or quantity <= ZERO:
        return {
            "action": "NONE",
            "reason": "当前仓位已在最小调整范围内",
            "currentPosition": text(current),
            "targetPosition": text(target),
            "delta": text(delta),
        }
    if delta < ZERO:
        return {
            "action": "ORDER",
            "side": "SELL",
            "quantity": text(quantity),
            "reduceOnly": False,
            "currentPosition": text(current),
            "targetPosition": text(target),
            "delta": text(delta),
        }
    reduce_quantity = min(quantity, abs(current))
    return {
        "action": "ORDER",
        "side": "BUY",
        "quantity": text(reduce_quantity),
        "reduceOnly": True,
        "currentPosition": text(current),
        "targetPosition": text(target),
        "delta": text(delta),
    }
