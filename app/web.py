"""MVP 網站後端服務：支援真實市場 (Yahoo Finance) 與模擬市場雙軌切換、驗證、風控與回測。"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from app.adapters.mock import DEFAULT_WATCHLIST, MockMarketAdapter
from app.adapters.yahoo import YahooFinanceAdapter
from app.core.broker import check_stop_triggers, execute_order, get_latest_prices, get_portfolio, preview_order
from app.core.reality_check import evaluate_strategy_accuracy
from app.core.risk_guard import OrderRequest
from app.core.scheduler import TaskScheduler
from app.core.strategy import (
    analyze_quotes,
    build_daily_report,
    run_strategy_and_save,
    simulate_conditional_forecast,
)
from app.core.validator import save_validated_quotes, validate_quotes
from app.db.database import get_connection, init_db

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "stockmate.db"
SYMBOL_NAMES: dict[str, str] = {sym: meta[0] for sym, meta in DEFAULT_WATCHLIST.items()}
SOURCE_LABELS: dict[str, str] = {
    "yahoo": "真實市場 (Yahoo Finance)",
    "mock": "模擬市場 (Mock)",
}

STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


class AppService:
    """網站應用服務核心：協調雙軌資料源、排程器與模擬交易風控。"""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        *,
        adapter_seed: int | None = None,
        default_source: str = "yahoo",
        yahoo_adapter: YahooFinanceAdapter | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.mock_adapter = MockMarketAdapter(seed=adapter_seed)
        self.yahoo_adapter = yahoo_adapter or YahooFinanceAdapter()
        self.scheduler = TaskScheduler(self.db_path)
        self._lock = threading.Lock()
        self.active_source = default_source if default_source in SOURCE_LABELS else "mock"
        self.last_source_notice: str | None = None

        init_db(self.db_path)
        self._register_jobs()
        self._initialize_data()

    def _register_jobs(self) -> None:
        self.scheduler.register_job(
            "fetch_pre_market", self.job_fetch_market,
            job_type="fetch_pre", cron_expr="30 8 * * 1-5", max_retries=2, description="盤前市場資料抓取",
        )
        self.scheduler.register_job(
            "fetch_intraday", self.job_fetch_market,
            job_type="fetch_intraday", cron_expr="*/15 9-13 * * 1-5", max_retries=1, description="盤中定時輪詢報價",
        )
        self.scheduler.register_job(
            "fetch_post_market", self.job_fetch_market,
            job_type="fetch_post", cron_expr="0 14 * * 1-5", max_retries=2, description="盤後完整日線資料抓取",
        )
        self.scheduler.register_job(
            "run_strategy_analysis", self.job_run_strategy,
            job_type="strategy", cron_expr="30 14 * * 1-5", max_retries=2, description="每日數據驗證與白話策略評分",
        )
        self.scheduler.register_job(
            "generate_daily_report", self.job_generate_report,
            job_type="daily_report", cron_expr="0 15 * * 1-5", max_retries=1, description="產出每日盤後小白白話日報",
        )

    def _seed_mock_if_empty(self, conn: Any) -> None:
        cnt = conn.execute("SELECT COUNT(*) AS cnt FROM market_quotes WHERE source = 'mock';").fetchone()["cnt"]
        if cnt == 0:
            quotes = self.mock_adapter.seed_history(list(SYMBOL_NAMES.keys()), days=30)
            result = validate_quotes(quotes)
            save_validated_quotes(conn, result)
        run_strategy_and_save(conn, SYMBOL_NAMES, source="mock")

    def _fetch_yahoo_batch(self, conn: Any) -> tuple[int, list[str]]:
        quotes = self.yahoo_adapter.fetch_quotes(list(SYMBOL_NAMES.keys()))
        validation = validate_quotes(quotes, max_daily_change_pct=0.25)
        saved = save_validated_quotes(conn, validation)
        issues = [f"{sym}: {err}" for sym, err in self.yahoo_adapter.last_errors.items()]
        issues.extend(f"{i.symbol}: {i.reason}" for i in validation.issues)
        return saved, issues

    def _initialize_data(self) -> None:
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                if self.active_source == "yahoo":
                    existing = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM market_quotes WHERE source = 'yahoo';"
                    ).fetchone()["cnt"]
                    if existing == 0:
                        _, issues = self._fetch_yahoo_batch(conn)
                        existing = conn.execute(
                            "SELECT COUNT(*) AS cnt FROM market_quotes WHERE source = 'yahoo';"
                        ).fetchone()["cnt"]
                        if existing > 0:
                            run_strategy_and_save(conn, SYMBOL_NAMES, source="yahoo")
                            if issues:
                                self.last_source_notice = f"真實行情已載入，部分標的異常：{issues[0]}"
                            return
                        # 無法連線或查無有效真實報價時安全降級為模擬市場
                        self.active_source = "mock"
                        self.last_source_notice = "目前無法連線至 Yahoo Finance 真實行情，已自動切換為模擬市場模式。"
                        self._seed_mock_if_empty(conn)
                    else:
                        run_strategy_and_save(conn, SYMBOL_NAMES, source="yahoo")
                else:
                    self._seed_mock_if_empty(conn)
            finally:
                conn.close()

    def switch_source(self, new_source: str) -> dict[str, Any]:
        """切換資料源（真實市場 Yahoo Finance <-> 模擬市場）。"""
        if new_source not in SOURCE_LABELS:
            raise ValueError("資料源必須為 'yahoo' 或 'mock'")

        with self._lock:
            conn = get_connection(self.db_path)
            try:
                if new_source == "yahoo":
                    _, issues = self._fetch_yahoo_batch(conn)
                    cnt = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM market_quotes WHERE source = 'yahoo';"
                    ).fetchone()["cnt"]
                    if cnt == 0:
                        raise RuntimeError("無法取得 Yahoo Finance 真實行情，請檢查網路連線後重試")
                    self.active_source = "yahoo"
                    self.last_source_notice = f"部分標的更新異常：{issues[0]}" if issues else None
                    run_strategy_and_save(conn, SYMBOL_NAMES, source="yahoo")
                else:
                    self.active_source = "mock"
                    self.last_source_notice = None
                    self._seed_mock_if_empty(conn)
            finally:
                conn.close()

        return self.get_overview()

    def job_fetch_market(self) -> tuple[str, int, str | None]:
        """依目前啟用的資料源抓取行情 -> 驗證 -> 入庫 -> 重算策略並檢查停損停利。"""
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                now_utc = datetime.now(timezone.utc).replace(microsecond=0)
                if self.active_source == "yahoo":
                    saved, issues = self._fetch_yahoo_batch(conn)
                    cnt = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM market_quotes WHERE source = 'yahoo';"
                    ).fetchone()["cnt"]
                    run_strategy_and_save(conn, SYMBOL_NAMES, source="yahoo")
                    check_stop_triggers(conn, source="yahoo", now=now_utc)
                    err_msg = "；".join(issues) if issues else None
                    status = "SUCCESS" if not issues else ("PARTIAL" if cnt > 0 else "FAILED")
                    return (status, saved, err_msg)

                last_closes = get_latest_prices(conn, source="mock")
                max_time_row = conn.execute(
                    "SELECT MAX(quote_time) AS max_t FROM market_quotes WHERE source = 'mock';"
                ).fetchone()
                fetch_time = now_utc
                if max_time_row and max_time_row["max_t"]:
                    latest_dt = datetime.fromisoformat(max_time_row["max_t"].replace("Z", "+00:00"))
                    if fetch_time <= latest_dt:
                        fetch_time = min(latest_dt + timedelta(seconds=1), now_utc + timedelta(minutes=4))

                quotes = self.mock_adapter.fetch_quotes(
                    list(SYMBOL_NAMES.keys()),
                    last_closes=last_closes,
                    quote_time=fetch_time,
                )
                validation = validate_quotes(quotes, prev_closes=last_closes, now=now_utc)
                saved = save_validated_quotes(conn, validation)
                run_strategy_and_save(conn, SYMBOL_NAMES, source="mock")
                check_stop_triggers(conn, source="mock", now=now_utc)

                err_msg = "；".join(f"{i.symbol}: {i.reason}" for i in validation.issues) if validation.issues else None
                return (validation.status, saved, err_msg)
            finally:
                conn.close()

    def job_run_strategy(self) -> int:
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                return run_strategy_and_save(conn, SYMBOL_NAMES, source=self.active_source)
            finally:
                conn.close()

    def job_generate_report(self) -> int:
        overview = self.get_overview()
        return len(overview["stocks"])

    def get_overview(self) -> dict[str, Any]:
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                source = self.active_source
                stocks: list[dict[str, Any]] = []
                for symbol, name in SYMBOL_NAMES.items():
                    rows = conn.execute(
                        """
                        SELECT quote_time, close, volume
                        FROM market_quotes
                        WHERE symbol = ? AND source = ?
                        ORDER BY quote_time ASC;
                        """,
                        (symbol, source),
                    ).fetchall()
                    if rows:
                        stocks.append(analyze_quotes(symbol, name, rows))

                max_row = conn.execute(
                    "SELECT MAX(quote_time) AS max_t FROM market_quotes WHERE source = ?;",
                    (source,),
                ).fetchone()
                return {
                    "active_source": source,
                    "source_label": SOURCE_LABELS[source],
                    "source_notice": self.last_source_notice,
                    "latest_quote_time": max_row["max_t"] if max_row else None,
                    "stocks": stocks,
                    "daily_report": build_daily_report(stocks),
                    "portfolio": get_portfolio(conn, SYMBOL_NAMES, source=source),
                }
            finally:
                conn.close()

    def get_reality_check(self) -> dict[str, Any]:
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                return evaluate_strategy_accuracy(conn, SYMBOL_NAMES, source=self.active_source, horizon_days=5)
            finally:
                conn.close()

    def get_schedules(self) -> dict[str, Any]:
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                configs = conn.execute(
                    "SELECT job_name, job_type, cron_expr, enabled, max_retries, description, updated_at FROM schedule_configs ORDER BY job_name;"
                ).fetchall()
                logs = self.scheduler.get_recent_logs(limit=30)
                return {
                    "schedules": [dict(c) for c in configs],
                    "logs": logs,
                }
            finally:
                conn.close()

    def simulate_forecast(
        self,
        symbol: str,
        *,
        market_change_pct: float,
        volume_multiplier: float,
        news_sentiment: str,
    ) -> dict[str, Any]:
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT quote_time, close, volume
                    FROM market_quotes
                    WHERE symbol = ? AND source = ?
                    ORDER BY quote_time ASC;
                    """,
                    (symbol, self.active_source),
                ).fetchall()
                if not rows:
                    raise KeyError(f"找不到標的報價：{symbol}")
                name = SYMBOL_NAMES.get(symbol, symbol)
                analysis = analyze_quotes(symbol, name, rows)
                closes = [float(r["close"]) for r in rows[-20:]]
                sim = simulate_conditional_forecast(
                    analysis["latest_close"],
                    analysis["ma20"],
                    closes,
                    market_change_pct=market_change_pct,
                    volume_multiplier=volume_multiplier,
                    news_sentiment=news_sentiment,
                )
                sim["symbol"] = symbol
                sim["name"] = name
                return sim
            finally:
                conn.close()


def _parse_order_request(payload: dict[str, Any]) -> tuple[OrderRequest, float | None]:
    symbol = str(payload.get("symbol", "")).strip()
    side = str(payload.get("side", "")).strip().upper()
    if not symbol:
        raise ValueError("請選擇或輸入股票代碼")
    try:
        quantity = int(payload["quantity"])
        price = float(payload["price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("下單數量須為整數且價格須為數字") from exc

    raw_sl = payload.get("stop_loss_price")
    raw_tp = payload.get("take_profit_price")
    sl = float(raw_sl) if raw_sl not in (None, "") else None
    tp = float(raw_tp) if raw_tp not in (None, "") else None
    confirmed = bool(payload.get("confirmed_by_user", False))
    return OrderRequest(symbol=symbol, side=side, quantity=quantity, price=price, stop_loss_price=sl, confirmed_by_user=confirmed), tp


def create_server(
    db_path: str | Path = DEFAULT_DB_PATH,
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    adapter_seed: int | None = None,
    default_source: str = "yahoo",
    yahoo_adapter: YahooFinanceAdapter | None = None,
) -> ThreadingHTTPServer:
    """建立可立即啟動的 MVP HTTP 伺服器。"""
    # shortcut: 使用 Python 內建 ThreadingHTTPServer 零依賴啟動 MVP 網站；未來若需高併發、JWT 或 OpenAPI 文件再升級為 FastAPI
    service = AppService(
        db_path=db_path,
        adapter_seed=adapter_seed,
        default_source=default_source,
        yahoo_adapter=yahoo_adapter,
    )
    static_root = STATIC_DIR.resolve()

    class AppHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_json(self, status: int, data: Any) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("請求內容長度無效")
            raw = self.rfile.read(length).decode("utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("JSON 根節點必須為物件")
            return parsed

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in STATIC_FILES:
                filename, ctype = STATIC_FILES[path]
                target = (static_root / filename).resolve()
                if target.parent != static_root or not target.is_file():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "找不到靜態檔案"})
                    return
                content = target.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

            if path == "/api/overview":
                self._send_json(HTTPStatus.OK, service.get_overview())
                return
            if path == "/api/reality-check":
                self._send_json(HTTPStatus.OK, service.get_reality_check())
                return
            if path == "/api/schedules":
                self._send_json(HTTPStatus.OK, service.get_schedules())
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "找不到指定路徑"})

        def do_PUT(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            prefix = "/api/schedules/"
            if not path.startswith(prefix):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "找不到指定路徑"})
                return

            job_name = unquote(path[len(prefix):]).strip("/")
            try:
                body = self._read_json()
                service.scheduler.update_schedule(
                    job_name,
                    cron_expr=body.get("cron_expr"),
                    enabled=body.get("enabled"),
                    max_retries=int(body["max_retries"]) if "max_retries" in body else None,
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "schedules": service.get_schedules()["schedules"]})
            except KeyError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path

            if path == "/api/source":
                try:
                    body = self._read_json()
                    overview = service.switch_source(str(body.get("source", "")).strip())
                    self._send_json(HTTPStatus.OK, overview)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except RuntimeError as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return

            if path.startswith("/api/schedules/") and path.endswith("/run"):
                job_name = unquote(path[len("/api/schedules/"):-len("/run")]).strip("/")
                try:
                    res = service.scheduler.rerun_job(job_name)
                    self._send_json(HTTPStatus.OK, asdict(res))
                except KeyError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return

            if path == "/api/orders/preview":
                try:
                    req, _ = _parse_order_request(self._read_json())
                    with service._lock:
                        conn = get_connection(service.db_path)
                        try:
                            check = preview_order(conn, req, source=service.active_source)
                        finally:
                            conn.close()
                    self._send_json(HTTPStatus.OK, asdict(check))
                except (ValueError, json.JSONDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if path == "/api/orders":
                try:
                    req, tp = _parse_order_request(self._read_json())
                    with service._lock:
                        conn = get_connection(service.db_path)
                        try:
                            ok, check, order_info = execute_order(
                                conn, req, take_profit_price=tp, source=service.active_source
                            )
                        finally:
                            conn.close()
                    status = HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST
                    self._send_json(status, {"ok": ok, "check": asdict(check), "order": order_info})
                except (ValueError, json.JSONDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if path == "/api/positions/check-triggers":
                with service._lock:
                    conn = get_connection(service.db_path)
                    try:
                        triggered = check_stop_triggers(conn, source=service.active_source)
                    finally:
                        conn.close()
                self._send_json(HTTPStatus.OK, {"triggered": triggered})
                return

            if path == "/api/forecast/simulate":
                try:
                    body = self._read_json()
                    res = service.simulate_forecast(
                        str(body.get("symbol", "")).strip(),
                        market_change_pct=float(body.get("market_change_pct", 0.0)),
                        volume_multiplier=float(body.get("volume_multiplier", 1.0)),
                        news_sentiment=str(body.get("news_sentiment", "neutral")),
                    )
                    self._send_json(HTTPStatus.OK, res)
                except KeyError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "找不到指定路徑"})

    return ThreadingHTTPServer((host, port), AppHandler)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = create_server(port=port, default_source="yahoo")
    print(f"安心股伴 StockMate 網站已啟動：http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
