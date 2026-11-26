"""MVP 網站後端服務：支援真實市場 (Yahoo Finance)、台股全類別行情、雙軌切換與排程自動更新。"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import sys
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from app.adapters.mock import DEFAULT_WATCHLIST, MockMarketAdapter
from app.adapters.yahoo import YahooFinanceAdapter
from app.adapters.yahoo_class import TAIPEI_TZ, CategoryDef, YahooClassScraper
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
from app.db.database import db_health_check, get_connection, init_db

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

_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_cell(value: Any) -> Any:
    """防範 CSV 公式注入：字串若以 =, +, -, @ 等字元開頭則安全前綴單引號，數值欄位保留原樣。"""
    if not isinstance(value, str) or not value:
        return value
    stripped = value.lstrip(" \t\r")
    if value[0] in _CSV_FORMULA_PREFIXES or (stripped and stripped[0] in ("=", "+", "-", "@")):
        return f"'{value}"
    return value


class AppService:
    """網站應用服務核心：協調雙軌資料源、台股全類別爬蟲、排程器與模擬交易風控。"""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        *,
        adapter_seed: int | None = None,
        default_source: str = "yahoo",
        yahoo_adapter: YahooFinanceAdapter | None = None,
        class_scraper: YahooClassScraper | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.mock_adapter = MockMarketAdapter(seed=adapter_seed)
        self.yahoo_adapter = yahoo_adapter or YahooFinanceAdapter()
        self.class_scraper = class_scraper or YahooClassScraper()
        self.scheduler = TaskScheduler(self.db_path)
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._live_lock = threading.Lock()
        self._live_wake = threading.Event()
        self._live_stop = threading.Event()
        self._live_thread: threading.Thread | None = None
        self._live_state: dict[str, Any] = {
            "enabled": True,
            "interval_sec": 60,
            "data_version": 1,
            "tick_count": 0,
            "last_tick_at": None,
            "next_tick_at": None,
            "last_status": "READY",
            "last_error": None,
        }
        self._sync_state: dict[str, Any] = {
            "running": False,
            "synced_categories": 0,
            "total_categories": 0,
            "total_quote_rows": 0,
            "failed_categories": 0,
            "last_finished_at": None,
            "last_error": None,
        }
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
        self.scheduler.register_job(
            "sync_yahoo_classes", self.job_sync_classes,
            job_type="sync_classes", cron_expr="*/15 9-14 * * 1-5", max_retries=2,
            description="每日盤中與盤後同步 Yahoo 台股全類別與即時報價",
        )

    def _get_watchlist(self, conn: Any) -> dict[str, str]:
        """讀取使用者目前自選追蹤清單（symbol -> name）。"""
        rows = conn.execute("SELECT symbol, name FROM watchlist ORDER BY added_at ASC, symbol ASC;").fetchall()
        return {str(r["symbol"]): str(r["name"]) for r in rows}

    def _seed_mock_if_empty(self, conn: Any) -> None:
        watchlist = self._get_watchlist(conn)
        if not watchlist:
            return
        existing_syms = {
            r["symbol"]
            for r in conn.execute("SELECT DISTINCT symbol FROM market_quotes WHERE source = 'mock';").fetchall()
        }
        missing = [s for s in watchlist if s not in existing_syms]
        if missing:
            quotes = self.mock_adapter.seed_history(missing, days=30)
            result = validate_quotes(quotes)
            save_validated_quotes(conn, result)
        run_strategy_and_save(conn, watchlist, source="mock")

    def _fetch_yahoo_batch(self, conn: Any, symbols: list[str] | None = None) -> tuple[int, list[str]]:
        target_symbols = symbols if symbols is not None else list(self._get_watchlist(conn).keys())
        if not target_symbols:
            return 0, []
        quotes = self.yahoo_adapter.fetch_quotes(target_symbols)
        validation = validate_quotes(quotes, max_daily_change_pct=0.25)
        saved = save_validated_quotes(conn, validation)
        issues = [f"{sym}: {err}" for sym, err in self.yahoo_adapter.last_errors.items()]
        issues.extend(f"{i.symbol}: {i.reason}" for i in validation.issues)
        return saved, issues

    def _initialize_data(self) -> None:
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                watchlist = self._get_watchlist(conn)
                if self.active_source == "yahoo":
                    existing = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM market_quotes WHERE source = 'yahoo';"
                    ).fetchone()["cnt"]
                    if existing == 0:
                        _, issues = self._fetch_yahoo_batch(conn, list(watchlist.keys()))
                        existing = conn.execute(
                            "SELECT COUNT(*) AS cnt FROM market_quotes WHERE source = 'yahoo';"
                        ).fetchone()["cnt"]
                        if existing > 0:
                            run_strategy_and_save(conn, watchlist, source="yahoo")
                            if issues:
                                self.last_source_notice = f"真實行情已載入，部分標的異常：{issues[0]}"
                            return
                        self.active_source = "mock"
                        self.last_source_notice = "目前無法連線至 Yahoo Finance 真實行情，已自動切換為模擬市場模式。"
                        self._seed_mock_if_empty(conn)
                    else:
                        run_strategy_and_save(conn, watchlist, source="yahoo")
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
                watchlist = self._get_watchlist(conn)
                if new_source == "yahoo":
                    _, issues = self._fetch_yahoo_batch(conn, list(watchlist.keys()))
                    cnt = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM market_quotes WHERE source = 'yahoo';"
                    ).fetchone()["cnt"]
                    if cnt == 0:
                        raise RuntimeError("無法取得 Yahoo Finance 真實行情，請檢查網路連線後重試")
                    self.active_source = "yahoo"
                    self.last_source_notice = f"部分標的更新異常：{issues[0]}" if issues else None
                    run_strategy_and_save(conn, watchlist, source="yahoo")
                else:
                    self.active_source = "mock"
                    self.last_source_notice = None
                    self._seed_mock_if_empty(conn)
            finally:
                conn.close()

        self.bump_data_version()
        return self.get_overview()

    def search_stocks(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        """搜尋台股代碼與名稱（優先從已同步之 Yahoo 全類別資料查詢）。"""
        keyword = q.strip()
        if not keyword:
            return []
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                like_kw = f"%{keyword}%"
                rows = conn.execute(
                    """
                    SELECT symbol, symbol_name, MAX(exchange) AS exchange, MAX(price) AS price
                    FROM yahoo_category_quotes
                    WHERE symbol LIKE ? OR symbol_name LIKE ?
                    GROUP BY symbol, symbol_name
                    ORDER BY (symbol = ?) DESC, (symbol LIKE ?) DESC, symbol ASC
                    LIMIT ?;
                    """,
                    (like_kw, like_kw, keyword.upper(), f"{keyword.upper()}%", limit),
                ).fetchall()
                results = [dict(r) for r in rows]
                if not results:
                    wl_rows = conn.execute(
                        "SELECT symbol, name AS symbol_name FROM watchlist WHERE symbol LIKE ? OR name LIKE ? LIMIT ?;",
                        (like_kw, like_kw, limit),
                    ).fetchall()
                    results = [{**dict(r), "exchange": "", "price": None} for r in wl_rows]
                return results
            finally:
                conn.close()

    def add_watchlist_symbol(self, raw_symbol: str, custom_name: str = "") -> dict[str, Any]:
        """新增自選股票至盤勢總覽：自動補齊 .TW/.TWO、抓取驗證報價並寫入 watchlist。"""
        text = raw_symbol.strip()
        if not text:
            raise ValueError("請輸入股票代碼或名稱")

        # 支援從自動補全選擇的 "2303.TW - 聯電" 格式
        if " - " in text:
            text = text.split(" - ", 1)[0].strip()

        with self._lock:
            conn = get_connection(self.db_path)
            try:
                # 若輸入純數字（如 2303），先在類別報價找確切市場後綴（.TW / .TWO），找不到再預設 .TW
                if text.isdigit():
                    found = conn.execute(
                        "SELECT symbol, symbol_name FROM yahoo_category_quotes WHERE symbol IN (?, ?) LIMIT 1;",
                        (f"{text}.TW", f"{text}.TWO"),
                    ).fetchone()
                    symbol = str(found["symbol"]) if found else f"{text}.TW"
                else:
                    # 支援直接輸入中文股名（例如「聯電」）反查代碼
                    by_name = conn.execute(
                        "SELECT symbol, symbol_name FROM yahoo_category_quotes WHERE symbol_name = ? LIMIT 1;",
                        (text,),
                    ).fetchone()
                    symbol = str(by_name["symbol"]) if by_name else text.upper()

                if not re.fullmatch(r"[A-Z0-9._^-]{1,20}", symbol):
                    raise ValueError("股票代碼格式不正確（請輸入英數代碼如 2330.TW 或 2303）")

                exists = conn.execute("SELECT 1 FROM watchlist WHERE symbol = ?;", (symbol,)).fetchone()
                if exists:
                    raise ValueError(f"{symbol} 已在追蹤清單中")

                # 解析中文顯示名稱
                display_name = custom_name.strip()
                if not display_name:
                    cat_row = conn.execute(
                        "SELECT symbol_name FROM yahoo_category_quotes WHERE symbol = ? LIMIT 1;",
                        (symbol,),
                    ).fetchone()
                    if cat_row:
                        display_name = str(cat_row["symbol_name"])
                    elif symbol in SYMBOL_NAMES:
                        display_name = SYMBOL_NAMES[symbol]
                    else:
                        display_name = symbol

                source = self.active_source
            finally:
                conn.close()

        # 網路抓價不可佔用 DB 鎖
        if source == "yahoo":
            quotes = self.yahoo_adapter.fetch_quotes([symbol])
            validation = validate_quotes(quotes, max_daily_change_pct=0.25)
        else:
            quotes = self.mock_adapter.seed_history([symbol], days=30)
            validation = validate_quotes(quotes)

        with self._lock:
            conn = get_connection(self.db_path)
            try:
                if validation.valid_quotes:
                    save_validated_quotes(conn, validation)

                cnt = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM market_quotes WHERE symbol = ? AND source = ?;",
                    (symbol, source),
                ).fetchone()["cnt"]
                if cnt == 0:
                    raise ValueError(f"找不到 {symbol} 的有效報價，請確認代碼是否正確")

                with conn:
                    conn.execute(
                        "INSERT INTO watchlist (symbol, name) VALUES (?, ?);",
                        (symbol, display_name),
                    )
                run_strategy_and_save(conn, {symbol: display_name}, source=source)
            finally:
                conn.close()

        self.bump_data_version()
        return self.get_overview()

    def remove_watchlist_symbol(self, symbol: str) -> dict[str, Any]:
        """從盤勢總覽移除指定自選股票。"""
        sym = symbol.strip().upper()
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                with conn:
                    cur = conn.execute("DELETE FROM watchlist WHERE symbol = ?;", (sym,))
                    if cur.rowcount == 0:
                        raise KeyError(f"追蹤清單中找不到：{sym}")
            finally:
                conn.close()

        self.bump_data_version()
        return self.get_overview()

    def bump_data_version(self) -> int:
        """遞增全域資料版本號，通知前端自動無痛刷新最新動態。"""
        with self._live_lock:
            self._live_state["data_version"] += 1
            return int(self._live_state["data_version"])

    def get_live_state(self) -> dict[str, Any]:
        with self._live_lock:
            state = dict(self._live_state)
        state["sync_status"] = self.get_class_sync_status()
        return state

    def update_live_config(
        self,
        *,
        enabled: bool | None = None,
        interval_sec: int | None = None,
    ) -> dict[str, Any]:
        """動態調整自動即時更新的開關與更新間隔（15~3600 秒）。"""
        if interval_sec is not None and not (15 <= interval_sec <= 3600):
            raise ValueError("更新間隔必須介於 15 到 3600 秒之間")

        with self._live_lock:
            if enabled is not None:
                self._live_state["enabled"] = bool(enabled)
            if interval_sec is not None:
                self._live_state["interval_sec"] = int(interval_sec)

            if self._live_state["enabled"]:
                nxt = datetime.now(timezone.utc) + timedelta(seconds=int(self._live_state["interval_sec"]))
                self._live_state["next_tick_at"] = nxt.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                self._live_state["next_tick_at"] = None

        self._live_wake.set()
        return self.get_live_state()

    def run_live_tick(self) -> dict[str, Any]:
        """執行單次背景即時更新（抓價、重算白話策略與停損停利掃描）。"""
        try:
            status, _, err_msg = self.job_fetch_market()
            now_utc = datetime.now(timezone.utc)
            with self._live_lock:
                self._live_state["tick_count"] += 1
                self._live_state["last_tick_at"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                self._live_state["last_status"] = status
                self._live_state["last_error"] = err_msg
                if self._live_state["enabled"]:
                    nxt = now_utc + timedelta(seconds=int(self._live_state["interval_sec"]))
                    self._live_state["next_tick_at"] = nxt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception as exc:  # noqa: BLE001
            now_utc = datetime.now(timezone.utc)
            with self._live_lock:
                self._live_state["tick_count"] += 1
                self._live_state["last_tick_at"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                self._live_state["last_status"] = "FAILED"
                self._live_state["last_error"] = f"{type(exc).__name__}: {exc}"
        return self.get_live_state()

    def start_live_updater(self) -> None:
        """啟動背景即時更新執行緒，依設定間隔自動更新動態。"""
        # shortcut: 單機背景輪詢執行緒搭配前端輕量版本檢查達成自動更新；未來若擴展多實例再改為 Redis Pub/Sub + WebSocket/SSE
        if self._live_thread is not None and self._live_thread.is_alive():
            return

        self._live_stop.clear()
        self._live_wake.clear()
        with self._live_lock:
            if self._live_state["enabled"]:
                nxt = datetime.now(timezone.utc) + timedelta(seconds=int(self._live_state["interval_sec"]))
                self._live_state["next_tick_at"] = nxt.strftime("%Y-%m-%dT%H:%M:%SZ")

        def _worker() -> None:
            while not self._live_stop.is_set():
                with self._live_lock:
                    enabled = bool(self._live_state["enabled"])
                    interval = int(self._live_state["interval_sec"])

                if not enabled:
                    self._live_wake.wait(timeout=10.0)
                    self._live_wake.clear()
                    continue

                woken = self._live_wake.wait(timeout=float(interval))
                self._live_wake.clear()
                if self._live_stop.is_set():
                    break
                if woken:
                    # 使用者剛切換間隔或啟用狀態，依新間隔重新計時
                    continue
                self.run_live_tick()

        self._live_thread = threading.Thread(target=_worker, daemon=True)
        self._live_thread.start()

    def stop_live_updater(self) -> None:
        """安全停止背景即時更新執行緒。"""
        self._live_stop.set()
        self._live_wake.set()
        if self._live_thread is not None:
            self._live_thread.join(timeout=2)
            self._live_thread = None

    def job_fetch_market(self) -> tuple[str, int, str | None]:
        """依目前啟用的資料源抓取行情 -> 驗證 -> 入庫 -> 重算策略並檢查停損停利。"""
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                now_utc = datetime.now(timezone.utc).replace(microsecond=0)
                watchlist = self._get_watchlist(conn)
                if not watchlist:
                    return ("SUCCESS", 0, None)

                if self.active_source == "yahoo":
                    saved, issues = self._fetch_yahoo_batch(conn, list(watchlist.keys()))
                    cnt = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM market_quotes WHERE source = 'yahoo';"
                    ).fetchone()["cnt"]
                    run_strategy_and_save(conn, watchlist, source="yahoo")
                    check_stop_triggers(conn, source="yahoo", now=now_utc)
                    err_msg = "；".join(issues) if issues else None
                    status = "SUCCESS" if not issues else ("PARTIAL" if cnt > 0 else "FAILED")
                    self.bump_data_version()
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
                    list(watchlist.keys()),
                    last_closes=last_closes,
                    quote_time=fetch_time,
                )
                validation = validate_quotes(quotes, prev_closes=last_closes, now=now_utc)
                saved = save_validated_quotes(conn, validation)
                run_strategy_and_save(conn, watchlist, source="mock")
                check_stop_triggers(conn, source="mock", now=now_utc)

                err_msg = "；".join(f"{i.symbol}: {i.reason}" for i in validation.issues) if validation.issues else None
                self.bump_data_version()
                return (validation.status, saved, err_msg)
            finally:
                conn.close()

    def job_sync_classes(self) -> tuple[str, int, str | None]:
        """同步 Yahoo 台股全類別與即時報價並更新進度狀態。"""
        with self._sync_lock:
            if self._sync_state["running"]:
                return ("PARTIAL", 0, "已有全類別同步任務進行中")
            self._sync_state.update(
                {
                    "running": True,
                    "synced_categories": 0,
                    "total_categories": 0,
                    "total_quote_rows": 0,
                    "failed_categories": 0,
                    "last_error": None,
                }
            )

        def on_progress(done: int, total: int, rows: int) -> None:
            with self._sync_lock:
                self._sync_state["synced_categories"] = done
                self._sync_state["total_categories"] = total
                self._sync_state["total_quote_rows"] = rows

        try:
            summary = self.class_scraper.sync_all_categories(self.db_path, progress_cb=on_progress)
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            err_msg = "；".join(summary["errors"][:3]) if summary["errors"] else None
            with self._sync_lock:
                self._sync_state.update(
                    {
                        "running": False,
                        "synced_categories": summary["synced_categories"],
                        "total_categories": summary["total_categories"],
                        "total_quote_rows": summary["total_quote_rows"],
                        "failed_categories": summary["failed_categories"],
                        "last_finished_at": now_iso,
                        "last_error": err_msg,
                    }
                )
            status = "SUCCESS" if summary["failed_categories"] == 0 else ("PARTIAL" if summary["synced_categories"] > 0 else "FAILED")
            self.bump_data_version()
            return (status, int(summary["total_quote_rows"]), err_msg)
        except Exception as exc:
            with self._sync_lock:
                self._sync_state.update(
                    {
                        "running": False,
                        "last_error": f"{type(exc).__name__}: {exc}",
                    }
                )
            raise

    def start_async_class_sync(self) -> tuple[bool, dict[str, Any]]:
        """非同步觸發全類別同步（給網頁按鈕呼叫，不阻塞 HTTP 回應）。"""
        with self._sync_lock:
            if self._sync_state["running"]:
                return False, dict(self._sync_state)

        threading.Thread(target=self.scheduler.rerun_job, args=("sync_yahoo_classes",), daemon=True).start()
        return True, self.get_class_sync_status()

    def get_class_sync_status(self) -> dict[str, Any]:
        with self._sync_lock:
            return dict(self._sync_state)

    def get_classes(self, *, group: str = "", q: str = "") -> dict[str, Any]:
        """查詢台股全類別目錄、各分組計數與統計摘要。"""
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                clauses: list[str] = []
                params: list[Any] = []
                if group:
                    clauses.append("group_type = ?")
                    params.append(group)
                if q:
                    clauses.append("(name LIKE ? OR category_key LIKE ?)")
                    like_q = f"%{q}%"
                    params.extend([like_q, like_q])
                where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

                rows = conn.execute(
                    f"""
                    SELECT category_key, group_type, name, sector_id, exchange,
                           stock_count, avg_change_pct, up_count, down_count, updated_at
                    FROM yahoo_categories
                    {where}
                    ORDER BY group_type, stock_count DESC, name;
                    """,
                    params,
                ).fetchall()

                grp_rows = conn.execute(
                    "SELECT group_type, COUNT(*) AS cnt FROM yahoo_categories GROUP BY group_type;"
                ).fetchall()
                max_row = conn.execute("SELECT MAX(updated_at) AS max_u FROM yahoo_categories;").fetchone()
                quote_cnt = conn.execute("SELECT COUNT(DISTINCT symbol) AS u_sym, COUNT(*) AS total_rows FROM yahoo_category_quotes;").fetchone()

                return {
                    "categories": [dict(r) for r in rows],
                    "group_counts": {r["group_type"]: int(r["cnt"]) for r in grp_rows},
                    "unique_symbols": int(quote_cnt["u_sym"]) if quote_cnt else 0,
                    "total_quote_rows": int(quote_cnt["total_rows"]) if quote_cnt else 0,
                    "last_updated_at": max_row["max_u"] if max_row else None,
                    "sync_status": self.get_class_sync_status(),
                }
            finally:
                conn.close()

    def get_category_quotes(self, category_key: str, *, q: str = "", refresh: bool = False) -> dict[str, Any]:
        """查詢指定類別的成分股即時報價；refresh=True 時即時重抓該類別最新動態。"""
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                cat = conn.execute(
                    "SELECT * FROM yahoo_categories WHERE category_key = ?;",
                    (category_key,),
                ).fetchone()
                if cat is None:
                    raise KeyError(f"找不到類別：{category_key}")
                cat_dict = dict(cat)
            finally:
                conn.close()

        if refresh:
            cat_def = CategoryDef(
                category_key=cat_dict["category_key"],
                group_type=cat_dict["group_type"],
                name=cat_dict["name"],
                sector_id=cat_dict["sector_id"],
                exchange=cat_dict["exchange"],
                category_param=cat_dict["category_param"],
                category_label=cat_dict["category_label"],
            )
            try:
                fresh_quotes = self.class_scraper.fetch_category_quotes(cat_def)
                now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                trade_date = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")
                changes = [item["change_pct"] for item in fresh_quotes if item["change_pct"] is not None]
                avg_chg = round(sum(changes) / len(changes), 2) if changes else None
                up_cnt = sum(1 for item in fresh_quotes if (item["change_pct"] or 0) > 0)
                down_cnt = sum(1 for item in fresh_quotes if (item["change_pct"] or 0) < 0)
                turnovers = [item["turnover_m"] for item in fresh_quotes if item["turnover_m"] is not None]
                total_turnover = round(sum(turnovers), 2) if turnovers else None

                with self._lock:
                    u_conn = get_connection(self.db_path)
                    try:
                        with u_conn:
                            u_conn.execute("DELETE FROM yahoo_category_quotes WHERE category_key = ?;", (category_key,))
                            u_conn.executemany(
                                """
                                INSERT INTO yahoo_category_quotes
                                    (category_key, symbol, symbol_name, exchange, price, change, change_pct,
                                     change_status, open, high, low, prev_close, volume, turnover_m,
                                     limit_up, limit_down, market_status, quote_time, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                                """,
                                [
                                    (
                                        category_key, item["symbol"], item["symbol_name"], item["exchange"],
                                        item["price"], item["change"], item["change_pct"], item["change_status"],
                                        item["open"], item["high"], item["low"], item["prev_close"], item["volume"],
                                        item["turnover_m"], item["limit_up"], item["limit_down"], item["market_status"],
                                        item["quote_time"], now_iso,
                                    )
                                    for item in fresh_quotes
                                ],
                            )
                            u_conn.execute(
                                """
                                UPDATE yahoo_categories
                                SET stock_count = ?, avg_change_pct = ?, up_count = ?, down_count = ?, updated_at = ?
                                WHERE category_key = ?;
                                """,
                                (len(fresh_quotes), avg_chg, up_cnt, down_cnt, now_iso, category_key),
                            )
                            u_conn.execute(
                                """
                                INSERT INTO yahoo_category_daily_snapshots
                                    (trade_date, category_key, stock_count, avg_change_pct, up_count, down_count, total_turnover_m, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(trade_date, category_key) DO UPDATE SET
                                    stock_count = excluded.stock_count,
                                    avg_change_pct = excluded.avg_change_pct,
                                    up_count = excluded.up_count,
                                    down_count = excluded.down_count,
                                    total_turnover_m = excluded.total_turnover_m,
                                    updated_at = excluded.updated_at;
                                """,
                                (trade_date, category_key, len(fresh_quotes), avg_chg, up_cnt, down_cnt, total_turnover, now_iso),
                            )
                    finally:
                        u_conn.close()
            except Exception:  # noqa: BLE001 - 單類即時刷新若暫時斷線，仍安全回傳資料庫快取
                pass

        with self._lock:
            conn = get_connection(self.db_path)
            try:
                cat = conn.execute(
                    "SELECT * FROM yahoo_categories WHERE category_key = ?;",
                    (category_key,),
                ).fetchone()
                params: list[Any] = [category_key]
                filter_sql = ""
                if q:
                    filter_sql = "AND (symbol LIKE ? OR symbol_name LIKE ?)"
                    params.extend([f"%{q}%", f"%{q}%"])

                rows = conn.execute(
                    f"""
                    SELECT symbol, symbol_name, exchange, price, change, change_pct, change_status,
                           open, high, low, prev_close, volume, turnover_m, limit_up, limit_down,
                           market_status, quote_time
                    FROM yahoo_category_quotes
                    WHERE category_key = ? {filter_sql}
                    ORDER BY (change_pct IS NULL) ASC, change_pct DESC, turnover_m DESC;
                    """,
                    params,
                ).fetchall()
                return {
                    "category": dict(cat),
                    "quotes": [dict(r) for r in rows],
                }
            finally:
                conn.close()

    def export_classes_csv(self, *, category_key: str = "", group: str = "") -> bytes:
        """將類別與即時報價匯出為 UTF-8-SIG CSV（Excel 可直接開起不亂碼）。"""
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                clauses: list[str] = []
                params: list[Any] = []
                if category_key:
                    clauses.append("c.category_key = ?")
                    params.append(category_key)
                if group:
                    clauses.append("c.group_type = ?")
                    params.append(group)
                where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

                rows = conn.execute(
                    f"""
                    SELECT c.group_type, c.name AS category_name, q.symbol, q.symbol_name,
                           q.exchange, q.price, q.change, q.change_pct, q.open, q.high, q.low,
                           q.prev_close, q.volume, q.turnover_m, q.limit_up, q.limit_down, q.quote_time
                    FROM yahoo_category_quotes q
                    INNER JOIN yahoo_categories c ON q.category_key = c.category_key
                    {where}
                    ORDER BY c.group_type, c.name, q.change_pct DESC;
                    """,
                    params,
                ).fetchall()

                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow([
                    "分組", "類別名稱", "股票代碼", "股票名稱", "市場", "成交價", "漲跌",
                    "漲跌幅(%)", "開盤", "最高", "最低", "昨收", "成交量(股)", "成交值(百萬)", "漲停", "跌停", "報價時間(UTC)",
                ])
                for r in rows:
                    writer.writerow([
                        _sanitize_csv_cell(r["group_type"]),
                        _sanitize_csv_cell(r["category_name"]),
                        _sanitize_csv_cell(r["symbol"]),
                        _sanitize_csv_cell(r["symbol_name"]),
                        _sanitize_csv_cell(r["exchange"]),
                        _sanitize_csv_cell(r["price"]),
                        _sanitize_csv_cell(r["change"]),
                        _sanitize_csv_cell(r["change_pct"]),
                        _sanitize_csv_cell(r["open"]),
                        _sanitize_csv_cell(r["high"]),
                        _sanitize_csv_cell(r["low"]),
                        _sanitize_csv_cell(r["prev_close"]),
                        _sanitize_csv_cell(r["volume"]),
                        _sanitize_csv_cell(r["turnover_m"]),
                        _sanitize_csv_cell(r["limit_up"]),
                        _sanitize_csv_cell(r["limit_down"]),
                        _sanitize_csv_cell(r["quote_time"]),
                    ])
                return buf.getvalue().encode("utf-8-sig")
            finally:
                conn.close()

    def job_run_strategy(self) -> int:
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                watchlist = self._get_watchlist(conn)
                return run_strategy_and_save(conn, watchlist, source=self.active_source)
            finally:
                conn.close()

    def job_generate_report(self) -> int:
        overview = self.get_overview()
        return len(overview["stocks"])

    def get_health(self) -> dict[str, Any]:
        """回傳系統健康狀態：資料庫健康度、目前資料源、live updater 狀態與排程總數。"""
        db_health = db_health_check(self.db_path)
        live_state = self.get_live_state()
        live_running = bool(self._live_thread is not None and self._live_thread.is_alive())
        schedule_count = int(db_health.get("table_counts", {}).get("schedule_configs", 0))
        return {
            "status": "ok" if db_health.get("ok") else "degraded",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "active_source": self.active_source,
            "source_label": SOURCE_LABELS.get(self.active_source, self.active_source),
            "database": db_health,
            "live_updater": {
                **live_state,
                "running": live_running,
            },
            "schedule_count": schedule_count,
            "registered_jobs": len(self.scheduler._jobs),
        }

    def get_overview(self) -> dict[str, Any]:
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                source = self.active_source
                watchlist = self._get_watchlist(conn)
                stocks: list[dict[str, Any]] = []
                for symbol, name in watchlist.items():
                    rows = conn.execute(
                        """
                        SELECT quote_time, open, high, low, close, volume
                        FROM market_quotes
                        WHERE symbol = ? AND source = ?
                        ORDER BY quote_time ASC;
                        """,
                        (symbol, source),
                    ).fetchall()
                    if rows:
                        # 完整透傳 analyze_quotes 回傳的所有指標與診斷欄位
                        stocks.append(dict(analyze_quotes(symbol, name, rows)))

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
                    "portfolio": get_portfolio(conn, watchlist, source=source),
                }
            finally:
                conn.close()

    def get_reality_check(self) -> dict[str, Any]:
        with self._lock:
            conn = get_connection(self.db_path)
            try:
                watchlist = self._get_watchlist(conn)
                return evaluate_strategy_accuracy(conn, watchlist, source=self.active_source, horizon_days=5)
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
                    SELECT quote_time, open, high, low, close, volume
                    FROM market_quotes
                    WHERE symbol = ? AND source = ?
                    ORDER BY quote_time ASC;
                    """,
                    (symbol, self.active_source),
                ).fetchall()
                if not rows:
                    raise KeyError(f"找不到標的報價：{symbol}")
                watchlist = self._get_watchlist(conn)
                name = watchlist.get(symbol, SYMBOL_NAMES.get(symbol, symbol))
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
    if not math.isfinite(price) or (sl is not None and not math.isfinite(sl)) or (tp is not None and not math.isfinite(tp)):
        raise ValueError("價格、停損價與停利價須為有限數值")
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
    class_scraper: YahooClassScraper | None = None,
    start_scheduler: bool = False,
    start_live: bool = False,
) -> ThreadingHTTPServer:
    """建立可立即啟動的 MVP HTTP 伺服器。"""
    # shortcut: 使用 Python 內建 ThreadingHTTPServer 零依賴啟動 MVP 網站；未來若需高併發、JWT 或 OpenAPI 文件再升級為 FastAPI
    service = AppService(
        db_path=db_path,
        adapter_seed=adapter_seed,
        default_source=default_source,
        yahoo_adapter=yahoo_adapter,
        class_scraper=class_scraper,
    )
    if start_scheduler:
        service.scheduler.start_builtin_loop()
    if start_live:
        service.start_live_updater()
    static_root = STATIC_DIR.resolve()

    class AppHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def end_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            super().end_headers()

        def _safe_dispatch(self, handler_fn: Callable[[], None]) -> None:
            try:
                handler_fn()
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:  # noqa: BLE001 - 頂層例外防護，不洩漏內部 traceback 給前端
                try:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "伺服器內部錯誤，請稍後再試"})
                except OSError:
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
            self._safe_dispatch(self._handle_get)

        def _handle_get(self) -> None:
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            query = parse_qs(parsed_url.query)

            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return

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

            if path == "/api/health":
                health = service.get_health()
                status = HTTPStatus.OK if health.get("status") == "ok" else HTTPStatus.SERVICE_UNAVAILABLE
                self._send_json(status, health)
                return
            if path == "/api/stocks/search":
                q = (query.get("q") or [""])[0].strip()
                self._send_json(HTTPStatus.OK, {"items": service.search_stocks(q)})
                return
            if path == "/api/overview":
                self._send_json(HTTPStatus.OK, service.get_overview())
                return
            if path == "/api/live":
                self._send_json(HTTPStatus.OK, service.get_live_state())
                return
            if path == "/api/reality-check":
                self._send_json(HTTPStatus.OK, service.get_reality_check())
                return
            if path == "/api/schedules":
                self._send_json(HTTPStatus.OK, service.get_schedules())
                return
            if path == "/api/classes":
                group = (query.get("group") or [""])[0].strip()
                q = (query.get("q") or [""])[0].strip()
                self._send_json(HTTPStatus.OK, service.get_classes(group=group, q=q))
                return
            if path == "/api/classes/sync-status":
                self._send_json(HTTPStatus.OK, service.get_class_sync_status())
                return
            if path == "/api/classes/export.csv":
                cat_key = (query.get("category_key") or [""])[0].strip()
                group = (query.get("group") or [""])[0].strip()
                csv_bytes = service.export_classes_csv(category_key=cat_key, group=group)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="tw_stock_classes.csv"')
                self.send_header("Content-Length", str(len(csv_bytes)))
                self.end_headers()
                self.wfile.write(csv_bytes)
                return
            if path.startswith("/api/classes/") and path.endswith("/quotes"):
                cat_key = unquote(path[len("/api/classes/"):-len("/quotes")]).strip("/")
                q = (query.get("q") or [""])[0].strip()
                refresh = (query.get("refresh") or ["0"])[0].strip().lower() in ("1", "true")
                try:
                    self._send_json(HTTPStatus.OK, service.get_category_quotes(cat_key, q=q, refresh=refresh))
                except KeyError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "找不到指定路徑"})

        def do_PUT(self) -> None:  # noqa: N802
            self._safe_dispatch(self._handle_put)

        def _handle_put(self) -> None:
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
            self._safe_dispatch(self._handle_post)

        def _handle_post(self) -> None:
            path = urlparse(self.path).path

            if path == "/api/watchlist":
                try:
                    body = self._read_json()
                    overview = service.add_watchlist_symbol(
                        str(body.get("symbol", "")),
                        custom_name=str(body.get("name", "")),
                    )
                    self._send_json(HTTPStatus.OK, overview)
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if path == "/api/live":
                try:
                    body = self._read_json()
                    enabled = bool(body["enabled"]) if "enabled" in body else None
                    interval = int(body["interval_sec"]) if "interval_sec" in body else None
                    self._send_json(HTTPStatus.OK, service.update_live_config(enabled=enabled, interval_sec=interval))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if path == "/api/classes/sync":
                started, state = service.start_async_class_sync()
                self._send_json(HTTPStatus.OK, {"started": started, "sync_status": state})
                return

            if path == "/api/source":
                try:
                    body = self._read_json()
                    overview = service.switch_source(str(body.get("source", "")).strip())
                    self._send_json(HTTPStatus.OK, overview)
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
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
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
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
                    if ok:
                        service.bump_data_version()
                    self._send_json(status, {"ok": ok, "check": asdict(check), "order": order_info})
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if path == "/api/positions/check-triggers":
                with service._lock:
                    conn = get_connection(service.db_path)
                    try:
                        triggered = check_stop_triggers(conn, source=service.active_source)
                    finally:
                        conn.close()
                if triggered:
                    service.bump_data_version()
                self._send_json(HTTPStatus.OK, {"triggered": triggered})
                return

            if path == "/api/forecast/simulate":
                try:
                    body = self._read_json()
                    mkt = float(body.get("market_change_pct", 0.0))
                    vol_m = float(body.get("volume_multiplier", 1.0))
                    if not (math.isfinite(mkt) and math.isfinite(vol_m)):
                        raise ValueError("模擬參數必須為有限數值")
                    res = service.simulate_forecast(
                        str(body.get("symbol", "")).strip(),
                        market_change_pct=mkt,
                        volume_multiplier=vol_m,
                        news_sentiment=str(body.get("news_sentiment", "neutral")),
                    )
                    self._send_json(HTTPStatus.OK, res)
                except KeyError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "找不到指定路徑"})

        def do_DELETE(self) -> None:  # noqa: N802
            self._safe_dispatch(self._handle_delete)

        def _handle_delete(self) -> None:
            path = urlparse(self.path).path
            prefix = "/api/watchlist/"
            if not path.startswith(prefix):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "找不到指定路徑"})
                return
            symbol = unquote(path[len(prefix):]).strip("/")
            try:
                overview = service.remove_watchlist_symbol(symbol)
                self._send_json(HTTPStatus.OK, overview)
            except KeyError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})

    server = ThreadingHTTPServer((host, port), AppHandler)
    # 掛載 service 引用以便關閉時停止排程迴圈
    server.app_service = service  # type: ignore[attr-defined]
    return server


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = create_server(port=port, default_source="yahoo", start_scheduler=True, start_live=True)
    print(f"安心股伴 StockMate 網站已啟動：http://127.0.0.1:{port}（已啟用自動即時更新與 Cron 排程）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app_service.stop_live_updater()  # type: ignore[attr-defined]
        server.app_service.scheduler.stop_builtin_loop()  # type: ignore[attr-defined]
        server.server_close()


if __name__ == "__main__":
    main()
