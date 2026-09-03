---
name: binance-funding-hedge-agent
description: Monitor an existing crypto spot exposure, evaluate Binance USDⓈ-M funding, basis, depth and risk limits, and reconcile a production short hedge through Binance Agent OS after exact confirmation.
---

# Binance Funding Hedge Agent

Use this skill when a user holds spot assets on-chain or elsewhere and wants to measure, monitor, increase, reduce, or close a Binance USDⓈ-M short hedge.

## Workflow

1. Collect the USDT perpetual symbol, spot exposure quantity, hedge ratio, and risk limits.
2. Run `npm install` if dependencies are absent, then run `python3 -B verify.py` for a live public-data check.
3. Start `python3 -B server.py` when the dashboard is useful.
4. Read public market context through the official Binance CLI and private account context through the authorized Binance MCP Server.
5. Run `engine.analyze` and show the target short, notional, funding annualization, basis, slippage, controls, and Agent OS tool trace.
6. Read private position data only from the Binance Agentic sub-account authorized by the user through MCP OAuth.
7. For any position-changing request, show the exact symbol, side, quantity, reduce-only state and current/target positions. Ask for exact `CONFIRM` immediately before execution.

## Invariants

- PASS is required to establish or increase a short.
- WARN and BLOCK stop establishment or increase.
- Reductions and closing orders use `reduceOnly`.
- Never submit a second order when the first result is uncertain; query by the same client order ID.
- Never accept or expose Binance credentials in chat, files, logs, reports, or the dashboard.
- For first use, run `npm run connect` or use the dashboard connection button; never ask the user to paste an API key.
