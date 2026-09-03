#!/usr/bin/env python3
"""Verify the live public Binance Agent OS path without account access."""
from __future__ import annotations

import json

from binance_agent_os import fetch_market
from engine import analyze


def main() -> None:
    intent = {
        "symbol": "BTCUSDT",
        "exposureQuantity": "0.001",
        "hedgeRatioPct": "100",
        "maxNotionalUsdt": "10000",
        "maxSlippageBps": "10",
        "maxBasisBps": "100",
        "maxFundingCostAnnualPct": "20",
        "minQuoteVolumeUsdt": "10000000",
    }
    report = analyze(fetch_market(intent["symbol"]), intent)
    print(json.dumps({
        "ok": True,
        "symbol": report["symbol"],
        "decision": report["decision"],
        "targetShortQuantity": report["exposure"]["targetShortQuantity"],
        "fundingAnnualPct": report["market"]["shortFundingAnnualPct"],
        "basisBps": report["market"]["basisBps"],
        "provider": report["source"].get("provider"),
        "toolCalls": report["source"].get("toolCalls"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

