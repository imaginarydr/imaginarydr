#!/usr/bin/env python3
"""Local control panel for a live Binance Agent OS funding-aware hedge."""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_os_login import AgentOsLoginError, begin_connect as connect_agent_os, connection_status, disconnect as disconnect_agent_os
from binance_agent_os import AgentOsError, fetch_market
from binance_mcp_bridge import account_snapshot, hedge_account_snapshot, submit_market_order
from engine import analyze, number, plan_reconcile, walk_book


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("FUNDING_HEDGE_DATA_DIR", Path.home() / ".binance-funding-hedge-agent"))
CONFIG_PATH = DATA_DIR / "config.json"
EVENTS_PATH = DATA_DIR / "events.jsonl"
HOST = os.environ.get("FUNDING_HEDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("FUNDING_HEDGE_PORT", "8815"))
MAX_BODY = 64 * 1024
CONFIG_FIELDS = {
    "symbol", "exposureQuantity", "hedgeRatioPct", "maxNotionalUsdt", "maxSlippageBps",
    "maxBasisBps", "maxFundingCostAnnualPct", "minQuoteVolumeUsdt", "monitorIntervalSeconds",
    "followSpotBalance",
}
DEFAULT_CONFIG: dict[str, Any] = {
    "symbol": "BTCUSDT",
    "exposureQuantity": "0.001",
    "hedgeRatioPct": "100",
    "maxNotionalUsdt": "10000",
    "maxSlippageBps": "10",
    "maxBasisBps": "100",
    "maxFundingCostAnnualPct": "20",
    "minQuoteVolumeUsdt": "10000000",
    "monitorIntervalSeconds": 60,
    "followSpotBalance": True,
}
TICKET_TTL_MS = 180_000
ACCOUNT_MONITOR_INTERVAL_SECONDS = 900
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
PENDING_TICKETS: dict[str, dict[str, Any]] = {}
LOCK = threading.RLock()
RUN_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
STATE: dict[str, Any] = {
    "monitorEnabled": False,
    "phase": "待机",
    "lastError": "",
    "lastReport": None,
    "account": None,
    "spotQuantity": None,
    "autoTicket": None,
    "lastOrder": None,
    "events": [],
    "updatedAt": 0,
}


def now_ms() -> int:
    return int(time.time() * 1000)


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    ensure_data_dir()
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def load_config() -> dict[str, Any]:
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    except (OSError, json.JSONDecodeError):
        saved = {}
    return {**DEFAULT_CONFIG, **{key: value for key, value in saved.items() if key in CONFIG_FIELDS}}


def clean_config(raw: dict[str, Any]) -> dict[str, Any]:
    current = {**load_config(), **{key: value for key, value in raw.items() if key in CONFIG_FIELDS}}
    current["symbol"] = str(current["symbol"]).strip().upper()
    interval = int(current.get("monitorIntervalSeconds") or 60)
    if not 15 <= interval <= 3600:
        raise ValueError("监控间隔必须为 15–3600 秒")
    current["monitorIntervalSeconds"] = interval
    follow = current.get("followSpotBalance", True)
    if isinstance(follow, str):
        follow = follow.strip().lower() in {"1", "true", "yes", "on"}
    current["followSpotBalance"] = bool(follow)
    return current


def save_config(raw: dict[str, Any]) -> dict[str, Any]:
    value = clean_config(raw)
    atomic_json(CONFIG_PATH, value)
    return value


def event(message: str, kind: str = "info") -> None:
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, "message": str(message)[:500]}
    with LOCK:
        STATE["events"] = (STATE.get("events") or [])[-119:] + [row]
        STATE["updatedAt"] = now_ms()
    try:
        ensure_data_dir()
        with EVENTS_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.chmod(EVENTS_PATH, 0o600)
    except OSError:
        pass


def set_phase(value: str) -> None:
    with LOCK:
        STATE["phase"] = value
        STATE["updatedAt"] = now_ms()


def public_state() -> dict[str, Any]:
    with LOCK:
        state = json.loads(json.dumps(STATE))
    return {
        "config": load_config(),
        "state": state,
        "agentOs": {"adapter": "Binance MCP OAuth", "productionOrders": True},
    }


def agent_os_status() -> dict[str, Any]:
    status = connection_status()
    with LOCK:
        status["verified"] = bool(STATE.get("account"))
    return status


def clear_private_state() -> None:
    with LOCK:
        STATE["monitorEnabled"] = False
        STATE["account"] = None
        STATE["spotQuantity"] = None
        STATE["autoTicket"] = None
        STATE["phase"] = "已退出"
        STATE["lastError"] = ""
        PENDING_TICKETS.clear()


def is_local_host(value: str | None) -> bool:
    try:
        return (urlparse(f"//{value or ''}").hostname or "").lower() in LOCAL_HOSTS
    except ValueError:
        return False


def is_trusted_origin(value: str | None) -> bool:
    if not value:
        return True
    try:
        parsed = urlparse(value)
        return parsed.scheme == "http" and (parsed.hostname or "").lower() in LOCAL_HOSTS and (parsed.port or 80) == PORT
    except ValueError:
        return False


def monitor_delay(interval: int, started_at: float, current_time: float | None = None) -> float:
    elapsed = max(0.0, (time.monotonic() if current_time is None else current_time) - started_at)
    return max(1.0, float(interval) - elapsed)


def analyze_live(intent: dict[str, Any], *, update: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    config = clean_config(intent)
    snapshot = fetch_market(config["symbol"])
    report = analyze(snapshot, config)
    if update:
        with LOCK:
            STATE["lastReport"] = report
            STATE["phase"] = "监控中" if STATE["monitorEnabled"] else "分析完成"
            STATE["lastError"] = ""
            STATE["updatedAt"] = now_ms()
    return report, snapshot


def effective_config(config: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    if not config.get("followSpotBalance"):
        return config
    return {**config, "exposureQuantity": str(account.get("spotQuantity") or "0")}


def live_account(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("followSpotBalance"):
        return hedge_account_snapshot(config["symbol"])
    return account_snapshot(config["symbol"])


def create_ticket(config: dict[str, Any], report: dict[str, Any], snapshot: dict[str, Any],
                  account: dict[str, Any], purpose: str, *, automatic: bool = False) -> dict[str, Any]:
    target = report["exposure"]["targetShortQuantity"] if purpose == "reconcile" else "0"
    minimum = report["rules"]["minQty"] if purpose == "reconcile" else "0"
    plan = plan_reconcile(account["positionAmount"], target, report["rules"]["stepSize"], minimum)
    if plan["action"] == "BLOCK":
        raise ValueError(plan["reason"])
    with LOCK:
        STATE["account"] = account
        STATE["spotQuantity"] = account.get("spotQuantity")
        STATE["lastReport"] = report
    if plan["action"] == "NONE":
        if automatic:
            with LOCK:
                STATE["autoTicket"] = None
        set_phase("仓位无需调整")
        return {"report": report, "account": account, "plan": plan, "ticket": None}
    if purpose == "reconcile" and not plan["reduceOnly"] and number(account["availableBalance"], "可用保证金") <= 0:
        raise ValueError("USDⓈ-M 合约账户没有可用保证金，无法建立空仓")
    estimate = estimate_order(snapshot, plan)
    max_slippage = number(config["maxSlippageBps"], "最大滑点")
    if not estimate["filled"] or number(estimate["slippageBps"], "预计滑点") > max_slippage:
        raise ValueError("当前盘口无法在滑点限制内完成订单")
    ticket_id = secrets.token_hex(16)
    ticket = {
        "ticketId": ticket_id,
        "purpose": purpose,
        "symbol": config["symbol"],
        "side": plan["side"],
        "orderType": "MARKET",
        "quantity": plan["quantity"],
        "reduceOnly": bool(plan["reduceOnly"]),
        "positionMode": account["positionMode"],
        "positionSide": "SHORT" if account["positionMode"] == "HEDGE" else "BOTH",
        "estimatedAveragePrice": estimate["averagePrice"],
        "estimatedSlippageBps": estimate["slippageBps"],
        "maxSlippageBps": str(config["maxSlippageBps"]),
        "tool": "futures_usds.newOrder",
        "expiresAt": now_ms() + TICKET_TTL_MS,
        "config": config,
    }
    public_ticket = {key: value for key, value in ticket.items() if key != "config"}
    with LOCK:
        expired = [key for key, value in PENDING_TICKETS.items() if now_ms() > int(value["expiresAt"])]
        for key in expired:
            PENDING_TICKETS.pop(key, None)
        if automatic and PENDING_TICKETS:
            return {"report": report, "account": account, "plan": plan, "ticket": None}
        PENDING_TICKETS.clear()
        PENDING_TICKETS[ticket_id] = ticket
        STATE["autoTicket"] = public_ticket if automatic else None
        STATE["phase"] = "等待确认执行"
    return {"report": report, "account": account, "plan": plan, "ticket": public_ticket}


def monitor_loop() -> None:
    last_auto_signature = ""
    last_account_check = time.time()
    while not STOP_EVENT.wait(1):
        with LOCK:
            enabled = bool(STATE["monitorEnabled"])
        if not enabled:
            continue
        cycle_started = time.monotonic()
        config = load_config()
        try:
            if config.get("followSpotBalance"):
                account = hedge_account_snapshot(config["symbol"])
                live_config = effective_config(config, account)
                report, snapshot = analyze_live(live_config)
                plan = plan_reconcile(
                    account["positionAmount"], report["exposure"]["targetShortQuantity"],
                    report["rules"]["stepSize"], report["rules"]["minQty"],
                )
                signature = "|".join([
                    config["symbol"], str(account.get("spotQuantity") or "0"),
                    str(account.get("positionAmount") or "0"), str(plan.get("side") or ""),
                    str(plan.get("quantity") or ""), report["decision"],
                ])
                with LOCK:
                    STATE["account"] = account
                    STATE["spotQuantity"] = account.get("spotQuantity")
                    STATE["lastReport"] = report
                with LOCK:
                    auto_ticket = STATE.get("autoTicket") or {}
                if auto_ticket and now_ms() > int(auto_ticket.get("expiresAt") or 0):
                    discard_ticket(str(auto_ticket.get("ticketId") or ""))
                    last_auto_signature = ""
                if report["decision"] == "PASS" and plan["action"] == "ORDER" and signature != last_auto_signature:
                    issued = create_ticket(live_config, report, snapshot, account, "reconcile", automatic=True)
                    if issued["ticket"]:
                        last_auto_signature = signature
                        event(f"检测到现货变化｜等待确认同步 {plan['side']} {plan['quantity']}")
                elif plan["action"] == "NONE":
                    last_auto_signature = ""
            else:
                report, _ = analyze_live(config)
            if not config.get("followSpotBalance") and time.time() - last_account_check >= ACCOUNT_MONITOR_INTERVAL_SECONDS:
                try:
                    account = account_snapshot(config["symbol"])
                    report["positionPlan"] = plan_reconcile(
                        account["positionAmount"],
                        report["exposure"]["targetShortQuantity"],
                        report["rules"]["stepSize"],
                        report["rules"]["minQty"],
                    )
                    with LOCK:
                        STATE["account"] = account
                        STATE["lastReport"] = report
                    last_account_check = time.time()
                except Exception as error:
                    event(f"真实仓位读取失败：{error}", "warning")
            event(f"{report['symbol']} {report['decision']}｜目标空仓 {report['exposure']['targetShortQuantity']}")
        except Exception as error:
            with LOCK:
                STATE["lastError"] = str(error)
                STATE["phase"] = "监控异常"
            event(str(error), "error")
        interval = int(config.get("monitorIntervalSeconds") or 60)
        if STOP_EVENT.wait(monitor_delay(interval, cycle_started)):
            return


def order_summary(result: dict[str, Any], plan: dict[str, Any], symbol: str) -> dict[str, Any]:
    order = result.get("order") if isinstance(result.get("order"), dict) else {}
    return {
        "symbol": symbol,
        "side": plan.get("side"),
        "quantity": plan.get("quantity"),
        "reduceOnly": plan.get("reduceOnly"),
        "status": order.get("status") or "UNKNOWN",
        "executedQuantity": str(order.get("executedQty") or "0"),
        "averagePrice": str(order.get("avgPrice") or "0"),
        "recoveredByClientId": bool(result.get("recoveredByClientId")),
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def estimate_order(snapshot: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    levels = snapshot["futuresAsks"] if plan["side"] == "BUY" else snapshot["futuresBids"]
    return walk_book(levels, plan["quantity"], plan["side"])


def discard_ticket(ticket_id: str) -> None:
    with LOCK:
        PENDING_TICKETS.pop(ticket_id, None)
        if (STATE.get("autoTicket") or {}).get("ticketId") == ticket_id:
            STATE["autoTicket"] = None


def prepare_ticket(raw: dict[str, Any], purpose: str) -> dict[str, Any]:
    set_phase("正在刷新实时行情")
    config = save_config(raw) if purpose == "reconcile" else clean_config(raw)
    snapshot = fetch_market(config["symbol"])
    set_phase("正在读取真实仓位")
    account = live_account(config)
    config = effective_config(config, account)
    report = analyze(snapshot, config)
    if purpose == "reconcile" and report["decision"] != "PASS":
        raise ValueError(f"当前结果为 {report['decision']}，禁止建立或增加空仓")
    return create_ticket(config, report, snapshot, account, purpose)


def execute_ticket(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("confirmation") != "CONFIRM":
        raise ValueError("请输入精确的 CONFIRM")
    ticket_id = str(raw.get("ticketId") or "")
    with LOCK:
        ticket = PENDING_TICKETS.get(ticket_id)
    if not ticket:
        raise ValueError("订单票据不存在，请重新生成")
    if now_ms() > int(ticket["expiresAt"]):
        discard_ticket(ticket_id)
        raise ValueError("订单票据已过期，请重新生成")
    if not RUN_LOCK.acquire(blocking=False):
        raise ValueError("已有仓位调整正在进行")
    try:
        config = ticket["config"]
        set_phase("正在刷新行情与仓位")
        snapshot = fetch_market(config["symbol"])
        account = live_account(config)
        config = effective_config(config, account)
        report = analyze(snapshot, config)
        with LOCK:
            STATE["lastReport"] = report
            STATE["account"] = account
            STATE["spotQuantity"] = account.get("spotQuantity")
        if ticket["purpose"] == "reconcile" and report["decision"] != "PASS":
            discard_ticket(ticket_id)
            raise ValueError(f"当前结果为 {report['decision']}，禁止建立或增加空仓")
        target = report["exposure"]["targetShortQuantity"] if ticket["purpose"] == "reconcile" else "0"
        minimum = report["rules"]["minQty"] if ticket["purpose"] == "reconcile" else "0"
        plan = plan_reconcile(
            account["positionAmount"],
            target,
            report["rules"]["stepSize"],
            minimum,
        )
        expected = (ticket["symbol"], ticket["side"], ticket["quantity"], ticket["reduceOnly"], ticket["positionMode"])
        actual = (config["symbol"], plan.get("side"), plan.get("quantity"), bool(plan.get("reduceOnly")), account["positionMode"])
        if plan["action"] != "ORDER" or actual != expected:
            discard_ticket(ticket_id)
            raise ValueError("行情或仓位已经变化，订单票据已取消")
        estimate = estimate_order(snapshot, plan)
        if not estimate["filled"] or number(estimate["slippageBps"], "预计滑点") > number(config["maxSlippageBps"], "最大滑点"):
            discard_ticket(ticket_id)
            raise ValueError("刷新后滑点超过限制，订单票据已取消")
        client_id = "funding-hedge-" + secrets.token_hex(8)
        discard_ticket(ticket_id)
        set_phase("正在提交订单")
        result = submit_market_order(
            config["symbol"], plan["side"], plan["quantity"], bool(plan["reduceOnly"]),
            client_id, account["positionMode"],
        )
        summary = order_summary(result, plan, config["symbol"])
        with LOCK:
            STATE["lastOrder"] = summary
            STATE["phase"] = "订单已返回，正在读取仓位"
            STATE["lastError"] = ""
        event(f"{config['symbol']} {plan['side']} {plan['quantity']}｜{summary['status']}", "trade")
        readback_error = ""
        try:
            refreshed = account_snapshot(config["symbol"])
        except AgentOsError as error:
            refreshed = account
            readback_error = str(error)
        with LOCK:
            STATE["account"] = refreshed
            if readback_error:
                STATE["phase"] = "订单已返回，仓位读回失败"
                STATE["lastError"] = readback_error
            else:
                STATE["phase"] = "仓位调整完成" if summary["status"] == "FILLED" else "订单状态待核对"
        return {"report": report, "account": refreshed, "plan": plan, "order": summary, "positionReadbackError": readback_error or None}
    finally:
        RUN_LOCK.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "FundingHedgeAgent/1.0"

    def log_message(self, *_args: Any) -> None:
        return

    def _json(self, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", content_type)
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("content-security-policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict[str, Any]:
        if self.headers.get_content_type().lower() != "application/json":
            raise ValueError("请求必须使用 application/json")
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError as error:
            raise ValueError("请求长度无效") from error
        if length <= 0 or length > MAX_BODY:
            raise ValueError("请求正文为空或过大")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("请求 JSON 无效") from error
        if not isinstance(value, dict):
            raise ValueError("请求必须是 JSON 对象")
        return value

    def do_GET(self) -> None:
        if not is_local_host(self.headers.get("host")):
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "拒绝非本地请求"})
            return
        path = urlparse(self.path).path
        if path == "/":
            self._file(ROOT / "static" / "index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self._file(ROOT / "static" / "app.js", "text/javascript; charset=utf-8")
        elif path == "/styles.css":
            self._file(ROOT / "static" / "styles.css", "text/css; charset=utf-8")
        elif path == "/api/state":
            self._json(HTTPStatus.OK, {"ok": True, **public_state()})
        elif path == "/api/agent-os/status":
            self._json(HTTPStatus.OK, {"ok": True, "connection": agent_os_status()})
        elif path == "/health":
            self._json(HTTPStatus.OK, {"ok": True})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not is_local_host(self.headers.get("host")) or not is_trusted_origin(self.headers.get("origin")):
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "拒绝非本地请求"})
            return
        path = urlparse(self.path).path
        try:
            raw = self._body()
            if path == "/api/config":
                result = save_config(raw)
            elif path == "/api/agent-os/connect":
                result = connect_agent_os()
                event("Binance Agent OS 授权完成")
            elif path == "/api/agent-os/logout":
                result = disconnect_agent_os()
                clear_private_state()
                event("Binance Agent OS 已退出")
            elif path == "/api/analyze":
                config = save_config(raw)
                set_phase("正在刷新实时行情")
                if config.get("followSpotBalance"):
                    account = live_account(config)
                    config = effective_config(config, account)
                    with LOCK:
                        STATE["account"] = account
                        STATE["spotQuantity"] = account.get("spotQuantity")
                result, _ = analyze_live(config)
                event(f"{result['symbol']} {result['decision']}｜资金费率年化 {result['market']['shortFundingAnnualPct']}%")
            elif path == "/api/account":
                config = clean_config(raw)
                set_phase("正在验证 Agent OS 账户")
                result = live_account(config)
                with LOCK:
                    STATE["account"] = result
                    STATE["spotQuantity"] = result.get("spotQuantity")
                    STATE["phase"] = "账户已读取"
                    STATE["lastError"] = ""
            elif path == "/api/monitor/start":
                save_config(raw)
                with LOCK:
                    STATE["monitorEnabled"] = True
                    STATE["phase"] = "监控中"
                event("持续监控已启动")
                result = {"monitorEnabled": True}
            elif path == "/api/monitor/stop":
                with LOCK:
                    STATE["monitorEnabled"] = False
                    STATE["phase"] = "已停止"
                    ticket_id = (STATE.get("autoTicket") or {}).get("ticketId")
                    if ticket_id:
                        PENDING_TICKETS.pop(ticket_id, None)
                    STATE["autoTicket"] = None
                event("持续监控已停止")
                result = {"monitorEnabled": False}
            elif path == "/api/exposure":
                config = save_config({
                    **raw,
                    "symbol": raw.get("symbol") or load_config()["symbol"],
                    "exposureQuantity": raw.get("exposureQuantity"),
                })
                result, _ = analyze_live(config)
                event(f"外部敞口已更新｜{config['symbol']} {config['exposureQuantity']}")
            elif path == "/api/reconcile/preview":
                result = prepare_ticket(raw, "reconcile")
            elif path == "/api/close/preview":
                result = prepare_ticket(raw, "close")
            elif path in {"/api/reconcile", "/api/close"}:
                result = execute_ticket(raw)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(HTTPStatus.OK, {"ok": True, "result": result})
        except (ValueError, AgentOsError, AgentOsLoginError, RuntimeError) as error:
            with LOCK:
                STATE["lastError"] = str(error)
                STATE["phase"] = "操作失败"
            event(str(error), "error")
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})


def main() -> None:
    ensure_data_dir()
    worker = threading.Thread(target=monitor_loop, name="funding-hedge-monitor", daemon=True)
    worker.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    event(f"服务已启动｜http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP_EVENT.set()
        server.server_close()


if __name__ == "__main__":
    main()
