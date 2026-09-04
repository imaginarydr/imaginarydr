# 币安资金费率对冲 Agent

用于对冲已经持有、并准备转到链上参与 LP、质押、任务或其他活动的现货敞口。Agent 通过 Binance Agent OS 读取现货价格、USDⓈ-M 永续合约盘口、资金费率、基差、交易规则和真实合约仓位，再把目标敞口转换为可审计的空仓调整计划。

## 功能

- 填写币种、现货数量与套保比例。
- 实时计算目标空仓、名义金额、资金费率年化、基差和预计滑点。
- 检查最小数量、最小名义金额、最大仓位、盘口深度、成交量和资金费率成本。
- 持续监控市场变化，并比较外部现货敞口与真实空仓。
- 可跟随 Binance 现货账户的可用与锁定余额；检测加减仓后自动生成同步空仓票据。
- 通过 Binance Agent OS OAuth 读取真实 USDⓈ-M 仓位与可用保证金。
- 查看订单票据并点击“确认执行”后，增加、减少或关闭空仓。
- 每次执行前重新读取市场、交易规则和真实仓位。
- 下单结果不确定时按唯一 client order ID 查询，不会盲目重发。

## Binance Agent OS

应用使用 Binance Agent OS 的两条官方接入路径：公共行情通过 `@binance/binance-cli`，账户与交易通过 Binance MCP OAuth。

公共行情：

- `spot.ticker-book-ticker`
- `spot.ticker24hr`
- `futures-usds.mark-price`
- `futures-usds.order-book`
- `futures-usds.get-funding-rate-info`
- `futures-usds.exchange-information`

账户与交易：

- `spot.getAccount`
- `futures_usds.positionInformationV2`
- `futures_usds.accountInformationV3`
- `futures_usds.newOrder`
- `futures_usds.queryOrder`

应用不包含 Binance REST 签名代码，也不接收或保存 API Key、Secret。

## 安装与运行

要求 Node.js 20+、Python 3.9+，以及已经登录的 Codex CLI。Codex 用于连接 Binance MCP OAuth；应用不要求用户提供 API Key。

```bash
npm install
npm run connect
npm test
npm run verify
npm start
```

打开 `http://hedge.localhost:7331`。

## 独立账户

默认使用 Binance Agent OS 官方 MCP OAuth。运行 `npm run connect` 或在页面点击“连接 Binance”后，Codex 会打开 Binance 授权页；用户选择 Agentic 子账户并授予 Market、Account、Trade 与 USDⓈ-M 权限。页面可验证当前授权状态并退出登录。退出登录只撤销本机保存的 MCP OAuth 会话，不影响账户资金或仓位。设备上不保存 Binance API Key。

官方 MCP 地址：`https://agent.binance.com/mcp/agentic`

如果当前环境不是 Codex，可按 [Binance MCP 官方接入文档](https://developers.binance.com/en/docs/agent-native/mcp-server/agentic) 在受支持的 AI 客户端完成授权。

### 账户与交易

交易功能沿用同一 Binance Agent OS OAuth 授权，不要求用户在页面填写 API Key 或 Secret。点击“验证 Agent OS”会读取真实 USDⓈ-M 仓位；每次仓位调整都会先生成带有效期的精确订单票据，用户查看票据并点击“确认执行”后，系统刷新行情与仓位并再次检查，只有参数完全一致时才提交一次订单。

账户与交易 MCP 调用默认使用 `gpt-5.6-luna`。如需切换，可在启动服务时设置 `FUNDING_HEDGE_CODEX_MODEL`；模型额度耗尽与 OAuth 失效会分别显示，不会再把额度错误误报为需要重新登录。

## 外部敞口更新

链上仓位或理财脚本可以在本机推送最新数量。服务只监听 `127.0.0.1`，收到数量后立即重新读取 Binance Agent OS 市场数据并计算目标空仓。

```bash
curl -sS http://hedge.localhost:7331/api/exposure \
  -H 'content-type: application/json' \
  --data '{"symbol":"ETHUSDT","exposureQuantity":"0.25"}'
```

## 执行边界

- `PASS` 才能建立或增加空仓。
- `WARN` 和 `BLOCK` 不会建立或增加仓位。
- 减少和关闭已有空仓始终使用 `reduceOnly`。
- 当前存在合约多仓时拒绝自动反向穿仓。
- 每次生产订单都要求查看新生成的票据并点击“确认执行”。
- 自动跟随只负责检测、计算和出票；每笔真实订单仍需当次点击“确认执行”。
- 服务默认只监听 `127.0.0.1`，并拒绝非本地 Host、跨站 Origin 与非 JSON 写请求。
