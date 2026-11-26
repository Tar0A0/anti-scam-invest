"""資深全端／系統工程師強化測試：涵蓋資料庫健康檢查、排程防重入、驗證容錯、CSV 防注入與 HTTP 安全防護。"""

from __future__ import annotations

import csv
import io
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.adapters.base import Quote
from app.core.broker import execute_order, preview_order
from app.core.risk_guard import OrderRequest
from app.core.scheduler import TaskScheduler
from app.core.validator import validate_quotes
from app.db.database import CORE_TABLES, db_health_check, get_connection, init_db
from app.web import AppService, _sanitize_csv_cell, create_server


class DatabaseReliabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "eng_db.db"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_connection_pragmas_and_health_check(self) -> None:
        conn = get_connection(self.db_path)
        try:
            busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
            journal_mode = str(conn.execute("PRAGMA journal_mode;").fetchone()[0]).lower()
            foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
            self.assertEqual(busy_timeout, 5000)
            self.assertEqual(journal_mode, "wal")
            self.assertEqual(foreign_keys, 1)
        finally:
            conn.close()

        health = db_health_check(self.db_path)
        self.assertTrue(health["ok"])
        self.assertTrue(health["exists"])
        self.assertGreater(health["file_size_bytes"], 0)
        self.assertEqual(health["journal_mode"].lower(), "wal")
        self.assertIsNone(health["error"])
        for tbl in CORE_TABLES:
            self.assertIn(tbl, health["table_counts"])
        self.assertEqual(health["table_counts"]["schedule_configs"], 6)
        self.assertEqual(health["table_counts"]["watchlist"], 5)

    def test_health_check_missing_file_does_not_create_db(self) -> None:
        missing_path = Path(self.tmp_dir.name) / "does_not_exist.db"
        health = db_health_check(missing_path)
        self.assertFalse(health["ok"])
        self.assertFalse(health["exists"])
        self.assertEqual(health["file_size_bytes"], 0)
        self.assertFalse(missing_path.exists())

    def test_concurrent_writes_succeed_with_busy_timeout(self) -> None:
        errors: list[Exception] = []

        def writer(worker_id: int) -> None:
            try:
                conn = get_connection(self.db_path)
                try:
                    for i in range(15):
                        with conn:
                            conn.execute(
                                "INSERT OR IGNORE INTO watchlist (symbol, name) VALUES (?, ?);",
                                (f"T{worker_id}_{i}.TW", f"測試{worker_id}_{i}"),
                            )
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(idx,)) for idx in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        health = db_health_check(self.db_path)
        self.assertEqual(health["table_counts"]["watchlist"], 5 + 4 * 15)


class SchedulerReentrancyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "eng_sched.db"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_reentrant_job_is_safely_skipped_and_cleaned_up(self) -> None:
        scheduler = TaskScheduler(self.db_path)
        entered = threading.Event()
        release = threading.Event()
        call_count = {"n": 0}

        def slow_job() -> int:
            call_count["n"] += 1
            entered.set()
            release.wait(timeout=5)
            return 7

        scheduler.register_job("slow_sync", slow_job, job_type="sync_classes", cron_expr="*/15 * * * *", max_retries=0)

        first_holder: dict[str, object] = {}

        def run_first() -> None:
            first_holder["res"] = scheduler.run_job("slow_sync", trigger_type="scheduled")

        t1 = threading.Thread(target=run_first)
        t1.start()
        self.assertTrue(entered.wait(timeout=3), "首次任務未於預期時間進入執行")

        # 重入呼叫應立即安全跳過並回傳 PARTIAL，不重複進入 slow_job
        second = scheduler.run_job("slow_sync", trigger_type="scheduled")
        self.assertEqual(second.status, "PARTIAL")
        self.assertEqual(second.records_processed, 0)
        self.assertIn("正在執行中", second.error_message or "")
        self.assertEqual(call_count["n"], 1)

        release.set()
        t1.join(timeout=5)
        first = first_holder["res"]
        self.assertEqual(first.status, "SUCCESS")  # type: ignore[attr-defined]
        self.assertEqual(first.records_processed, 7)  # type: ignore[attr-defined]

        # 首次完成後，防重入集合已清空，可正常再次執行
        third = scheduler.run_job("slow_sync", trigger_type="manual")
        self.assertEqual(third.status, "SUCCESS")
        self.assertEqual(call_count["n"], 2)

    def test_running_jobs_cleaned_up_after_failure(self) -> None:
        scheduler = TaskScheduler(self.db_path)
        should_fail = {"flag": True}

        def flaky() -> int:
            if should_fail["flag"]:
                raise RuntimeError("boom")
            return 3

        scheduler.register_job("fail_then_ok", flaky, job_type="strategy", cron_expr="0 14 * * 1-5", max_retries=0)
        res_fail = scheduler.run_job("fail_then_ok")
        self.assertEqual(res_fail.status, "FAILED")
        self.assertNotIn("fail_then_ok", scheduler._running_jobs)

        should_fail["flag"] = False
        res_ok = scheduler.run_job("fail_then_ok")
        self.assertEqual(res_ok.status, "SUCCESS")
        self.assertEqual(res_ok.records_processed, 3)


class ValidatorResilienceTest(unittest.TestCase):
    def test_malformed_types_and_missing_fields_become_validation_issues(self) -> None:
        base_time = datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc)
        quotes: list[Quote] = [
            # 正常報價
            Quote("2330.TW", base_time, 1000.0, 1010.0, 995.0, 1005.0, 15000, "yahoo"),
            # volume 為浮點數
            Quote("2330.TW", base_time + timedelta(minutes=1), 1005.0, 1010.0, 1000.0, 1006.0, 12.5, "yahoo"),  # type: ignore[arg-type]
            # volume 為字串
            Quote("2330.TW", base_time + timedelta(minutes=2), 1005.0, 1010.0, 1000.0, 1006.0, "1000", "yahoo"),  # type: ignore[arg-type]
            # volume 為布林
            Quote("2330.TW", base_time + timedelta(minutes=3), 1005.0, 1010.0, 1000.0, 1006.0, True, "yahoo"),  # type: ignore[arg-type]
            # quote_time 缺漏 (None)
            Quote("2330.TW", None, 1005.0, 1010.0, 1000.0, 1006.0, 1000, "yahoo"),  # type: ignore[arg-type]
            # quote_time 為字串
            Quote("2330.TW", "2026-11-25T01:05:00Z", 1005.0, 1010.0, 1000.0, 1006.0, 1000, "yahoo"),  # type: ignore[arg-type]
            # 價格為字串
            Quote("2330.TW", base_time + timedelta(minutes=4), "1005", 1010.0, 1000.0, 1006.0, 1000, "yahoo"),  # type: ignore[arg-type]
            # 價格為 NaN
            Quote("2330.TW", base_time + timedelta(minutes=5), float("nan"), 1010.0, 1000.0, 1006.0, 1000, "yahoo"),
            # 價格為布林
            Quote("2330.TW", base_time + timedelta(minutes=6), True, 1010.0, 1000.0, 1006.0, 1000, "yahoo"),  # type: ignore[arg-type]
            # symbol 為非字串
            Quote(2330, base_time + timedelta(minutes=7), 1005.0, 1010.0, 1000.0, 1006.0, 1000, "yahoo"),  # type: ignore[arg-type]
            # None 物件本身
            None,  # type: ignore[list-item]
        ]

        res = validate_quotes(quotes, now=base_time + timedelta(minutes=30))
        self.assertEqual(res.status, "PARTIAL")
        self.assertEqual(len(res.valid_quotes), 1)
        self.assertEqual(len(res.issues), 10)


class ServiceAndCsvSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "eng_service.db"
        self.service = AppService(db_path=self.db_path, adapter_seed=7, default_source="mock")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_csv_formula_injection_sanitization_preserves_numbers(self) -> None:
        self.assertEqual(_sanitize_csv_cell("=CMD|' /C calc'!A0"), "'=CMD|' /C calc'!A0")
        self.assertEqual(_sanitize_csv_cell("+SUM(A1:A2)"), "'+SUM(A1:A2)")
        self.assertEqual(_sanitize_csv_cell("-2+3"), "'-2+3")
        self.assertEqual(_sanitize_csv_cell("@SUM(1,2)"), "'@SUM(1,2)")
        self.assertEqual(_sanitize_csv_cell(" =1+1"), "' =1+1")
        self.assertEqual(_sanitize_csv_cell("台積電"), "台積電")
        self.assertEqual(_sanitize_csv_cell(-3.17), -3.17)
        self.assertEqual(_sanitize_csv_cell(123), 123)
        self.assertIsNone(_sanitize_csv_cell(None))

        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO yahoo_categories (category_key, group_type, name) VALUES ('sec:1', '=上市', '+半導體');"
                )
                conn.execute(
                    """
                    INSERT INTO yahoo_category_quotes
                        (category_key, symbol, symbol_name, exchange, price, change, change_pct, volume, quote_time)
                    VALUES ('sec:1', '-EVIL.TW', '@HYPERLINK(\"http://x\")', 'TAI', 100.5, -3.5, -3.37, 50000, '2026-11-25T05:30:00Z');
                    """
                )
        finally:
            conn.close()

        raw_csv = self.service.export_classes_csv().decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(raw_csv)))
        self.assertEqual(len(rows), 2)
        data_row = rows[1]
        self.assertEqual(data_row[0], "'=上市")
        self.assertEqual(data_row[1], "'+半導體")
        self.assertEqual(data_row[2], "'-EVIL.TW")
        self.assertEqual(data_row[3], "'@HYPERLINK(\"http://x\")")
        # 數值欄位保持原樣（不可加單引號）
        self.assertEqual(data_row[5], "100.5")
        self.assertEqual(data_row[6], "-3.5")
        self.assertEqual(data_row[7], "-3.37")
        self.assertEqual(data_row[12], "50000")

    def test_get_overview_passes_through_all_analyze_quotes_fields_and_ohlcv(self) -> None:
        captured_keys: list[list[str]] = []
        from app.core import strategy as strategy_mod

        orig_analyze = strategy_mod.analyze_quotes

        def wrapped_analyze(symbol: str, name: str, rows: list) -> dict:
            captured_keys.append(list(rows[0].keys()))
            out = orig_analyze(symbol, name, rows)
            out["custom_quant_metric"] = 42.25
            out["rsi14_mock_passthrough"] = 58.1
            return out

        with mock.patch("app.web.analyze_quotes", side_effect=wrapped_analyze):
            overview = self.service.get_overview()

        self.assertGreaterEqual(len(overview["stocks"]), 1)
        self.assertIn("high", captured_keys[0])
        self.assertIn("low", captured_keys[0])
        for st in overview["stocks"]:
            self.assertEqual(st["custom_quant_metric"], 42.25)
            self.assertEqual(st["rsi14_mock_passthrough"], 58.1)

    def test_service_get_health_payload(self) -> None:
        health = self.service.get_health()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["active_source"], "mock")
        self.assertTrue(health["database"]["ok"])
        self.assertEqual(health["schedule_count"], 6)
        self.assertIn("enabled", health["live_updater"])
        self.assertIn("running", health["live_updater"])

    def test_broker_rejects_nan_take_profit_safely(self) -> None:
        conn = get_connection(self.db_path)
        try:
            req = OrderRequest("2330.TW", "BUY", 10, 100.0, stop_loss_price=95.0, confirmed_by_user=True)
            ok, check, filled = execute_order(conn, req, take_profit_price=float("nan"), source="mock")
            self.assertFalse(ok)
            self.assertIsNone(filled)
            self.assertTrue(check.reasons)

            preview = preview_order(conn, OrderRequest("2330.TW", "", 10, 100.0, confirmed_by_user=False), source="mock")
            self.assertFalse(preview.allowed)
        finally:
            conn.close()


class _FakeSocket:
    """記憶體內模擬通訊端，可無須 localhost 連線直接完整測試 BaseHTTPRequestHandler。"""

    def __init__(self, raw_request: bytes) -> None:
        self._in = io.BytesIO(raw_request)
        self._out = io.BytesIO()

    def makefile(self, mode: str, *_args: object, **_kwargs: object) -> io.BytesIO:
        return self._in if "r" in mode else self._out

    def sendall(self, data: bytes) -> None:
        self._out.write(data)

    def close(self) -> None:
        return

    def get_response_bytes(self) -> bytes:
        return self._out.getvalue()


def _dispatch_http(server: object, method: str, path: str, body: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    header_lines = [
        f"{method} {path} HTTP/1.1",
        "Host: 127.0.0.1",
        "Connection: close",
    ]
    if body is not None:
        header_lines.append("Content-Type: application/json")
        header_lines.append(f"Content-Length: {len(payload)}")
    raw_req = ("\r\n".join(header_lines) + "\r\n\r\n").encode("utf-8") + payload
    sock = _FakeSocket(raw_req)
    server.RequestHandlerClass(sock, ("127.0.0.1", 12345), server)  # type: ignore[attr-defined]
    raw_resp = sock.get_response_bytes()
    head_bytes, _, body_bytes = raw_resp.partition(b"\r\n\r\n")
    lines = head_bytes.decode("iso-8859-1").split("\r\n")
    status_code = int(lines[0].split(" ", 2)[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status_code, headers, body_bytes


class HttpHardeningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "eng_http.db"
        self.server = create_server(
            db_path=self.db_path,
            host="127.0.0.1",
            port=0,
            adapter_seed=11,
            default_source="mock",
        )

    def tearDown(self) -> None:
        self.server.server_close()
        self.tmp_dir.cleanup()

    def _assert_security_headers(self, headers: dict[str, str]) -> None:
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")

    def test_security_headers_favicon_and_health_endpoint(self) -> None:
        # 1. GET /favicon.ico -> 204 No Content + 安全標頭
        status_fav, headers_fav, body_fav = _dispatch_http(self.server, "GET", "/favicon.ico")
        self.assertEqual(status_fav, 204)
        self.assertEqual(body_fav, b"")
        self._assert_security_headers(headers_fav)

        # 2. GET /api/health -> 200 OK + 健康資訊 + 安全標頭
        status_h, headers_h, body_h = _dispatch_http(self.server, "GET", "/api/health")
        self.assertEqual(status_h, 200)
        self._assert_security_headers(headers_h)
        data = json.loads(body_h.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["active_source"], "mock")
        self.assertTrue(data["database"]["ok"])
        self.assertEqual(data["schedule_count"], 6)
        self.assertIn("live_updater", data)

        # 3. 靜態頁、404 與 CSV 匯出也必須包含安全標頭
        status_idx, headers_idx, body_idx = _dispatch_http(self.server, "GET", "/")
        self.assertEqual(status_idx, 200)
        self.assertIn(b"StockMate", body_idx)
        self._assert_security_headers(headers_idx)

        status_csv, headers_csv, _ = _dispatch_http(self.server, "GET", "/api/classes/export.csv")
        self.assertEqual(status_csv, 200)
        self._assert_security_headers(headers_csv)

        status_404, headers_404, _ = _dispatch_http(self.server, "GET", "/api/not-found")
        self.assertEqual(status_404, 404)
        self._assert_security_headers(headers_404)

    def test_top_level_exception_guard_hides_traceback_across_methods(self) -> None:
        service: AppService = self.server.app_service  # type: ignore[attr-defined]

        # GET 內部未預期例外 -> 500 且不洩漏 Traceback
        with mock.patch.object(service, "get_overview", side_effect=RuntimeError("SECRET_DB_TOKEN_123")):
            status, headers, raw = _dispatch_http(self.server, "GET", "/api/overview")
            self.assertEqual(status, 500)
            self._assert_security_headers(headers)
            body = json.loads(raw.decode("utf-8"))
            self.assertEqual(body, {"error": "伺服器內部錯誤，請稍後再試"})
            self.assertNotIn("SECRET_DB_TOKEN_123", raw.decode("utf-8"))

        # POST 內部未預期例外 -> 500
        with mock.patch.object(service, "start_async_class_sync", side_effect=RuntimeError("POST_SECRET_TRACE")):
            status, headers, raw = _dispatch_http(self.server, "POST", "/api/classes/sync", {})
            self.assertEqual(status, 500)
            self._assert_security_headers(headers)
            self.assertEqual(json.loads(raw.decode("utf-8")), {"error": "伺服器內部錯誤，請稍後再試"})
            self.assertNotIn("POST_SECRET_TRACE", raw.decode("utf-8"))

        # PUT 內部未預期例外 -> 500
        with mock.patch.object(service.scheduler, "update_schedule", side_effect=RuntimeError("PUT_SECRET_TRACE")):
            status, headers, raw = _dispatch_http(
                self.server, "PUT", "/api/schedules/fetch_post_market", {"enabled": True}
            )
            self.assertEqual(status, 500)
            self._assert_security_headers(headers)
            self.assertEqual(json.loads(raw.decode("utf-8")), {"error": "伺服器內部錯誤，請稍後再試"})
            self.assertNotIn("PUT_SECRET_TRACE", raw.decode("utf-8"))

        # DELETE 內部未預期例外 -> 500
        with mock.patch.object(service, "remove_watchlist_symbol", side_effect=RuntimeError("DEL_SECRET_TRACE")):
            status, headers, raw = _dispatch_http(self.server, "DELETE", "/api/watchlist/2330.TW")
            self.assertEqual(status, 500)
            self._assert_security_headers(headers)
            self.assertEqual(json.loads(raw.decode("utf-8")), {"error": "伺服器內部錯誤，請稍後再試"})
            self.assertNotIn("DEL_SECRET_TRACE", raw.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
