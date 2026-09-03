# Architecture

```text
Binance spot balance or external spot exposure
          ↓
Binance Agent OS live market + account tools
          ↓
deterministic hedge and funding controls
          ↓
PASS / WARN / BLOCK + continuous monitor
          ↓
confirmation button → exact CONFIRM internally → one USDⓈ-M order → status readback
```

The local engine never signs a Binance request and never stores API credentials. `binance_agent_os.py` reads public market data through the official Binance CLI without a shell. `binance_mcp_bridge.py` uses the authorized Binance MCP OAuth connection for Spot balance, USDⓈ-M position, order submission, and status readback.
