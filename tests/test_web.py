"""MVP 網站前後端整合測試：透過真實 ThreadingHTTPServer 與 SQLite 驗證所有 API 流程。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.adapters.base import Quote
from app.db.database import get_connection
from app.web import SYMBOL_NAMES, create_server


class _FakeYahooAdapter:
    source_name = "yahoo"

    def __init__(self) -> None:
        self.last_errors: dict[str, str] = {}

    def fetch_quotes(self, symbols: list[str]) -> list[Quote]:
        base = datetime(2026, 10, 1, tzinfo=timezone.utc)
        out: list[Quote] = []
        for sym in symbols:
            for i in range(30):
                p = round(200.0 + i * 1.5, 2)
                out.append(Quote(sym, base + timedelta(days=i), p, p + 2, p - 2, p, 15000 + i * 300, "yahoo"))
        return out


class WebAppIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "web_test.db"
        self.server = create_server(
            db_path=self.db_path,
            host="127.0.0.1",
            port=0,
            adapter_seed=42,
            default_source="mock",
            yahoo_adapter=_FakeYahooAdapter(),  # type: ignore[arg-type]
        )
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp_dir.cleanup()

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                ctype = resp.headers.get("Content-Type", "")
                return resp.status, (json.loads(raw) if "application/json" in ctype else raw)
        except HTTPError as err:
            raw = err.read().decode("utf-8")
            return err.code, json.loads(raw)

    def test_index_and_overview(self) -> None:
        status_html, html = self._request("GET", "/")
        self.assertEqual(status_html, 200)
        self.assertIn("StockMate", html)

        status_api, data = self._request("GET", "/api/overview")
        self.assertEqual(status_api, 200)
        self.assertEqual(len(data["stocks"]), 5)
        self.assertIn("headline", data["daily_report"])
        self.assertEqual(data["portfolio"]["cash_balance"], 1_000_000.0)
        for stock in data["stocks"]:
            self.assertIn(stock["action"], ("HEAVY_BUY", "ADD", "SELL", "HOLD"))
            self.assertGreaterEqual(len(stock["sparkline"]), 20)

    def test_schedule_run_and_update(self) -> None:
        status, run_res = self._request("POST", "/api/schedules/fetch_post_market/run", {})
        self.assertEqual(status, 200)
        self.assertEqual(run_res["status"], "SUCCESS")
        self.assertEqual(run_res["trigger_type"], "manual")
        self.assertEqual(run_res["records_processed"], 5)

        status_get, sched_data = self._request("GET", "/api/schedules")
        self.assertEqual(status_get, 200)
        self.assertGreaterEqual(len(sched_data["logs"]), 1)

        bad_status, _ = self._request("PUT", "/api/schedules/fetch_post_market", {"cron_expr": "bad"})
        self.assertEqual(bad_status, 400)

        ok_status, _ = self._request("PUT", "/api/schedules/fetch_post_market", {"cron_expr": "15 14 * * 1-5", "enabled": True})
        self.assertEqual(ok_status, 200)

    def test_order_preview_confirm_and_stop_loss_trigger(self) -> None:
        _, overview = self._request("GET", "/api/overview")
        target = overview["stocks"][0]
        symbol = target["symbol"]
        price = float(target["latest_close"])
        sl_price = round(price * 0.95, 2)

        # 1. 預覽應要求二次確認並估算最大虧損
        p_status, preview = self._request(
            "POST", "/api/orders/preview",
            {"symbol": symbol, "side": "BUY", "quantity": 100, "price": price, "stop_loss_price": sl_price},
        )
        self.assertEqual(p_status, 200)
        self.assertTrue(preview["requires_confirmation"])
        self.assertAlmostEqual(preview["max_loss_estimate"], (price - sl_price) * 100, places=1)

        # 2. 未確認直接送單必須被拒絕
        unconf_status, unconf_res = self._request(
            "POST", "/api/orders",
            {"symbol": symbol, "side": "BUY", "quantity": 100, "price": price, "stop_loss_price": sl_price, "confirmed_by_user": False},
        )
        self.assertEqual(unconf_status, 400)
        self.assertFalse(unconf_res["ok"])

        # 3. 確認後下單成功並扣減現金與新增持倉
        ok_status, ok_res = self._request(
            "POST", "/api/orders",
            {"symbol": symbol, "side": "BUY", "quantity": 100, "price": price, "stop_loss_price": sl_price, "confirmed_by_user": True},
        )
        self.assertEqual(ok_status, 200)
        self.assertTrue(ok_res["ok"])

        # 4. 模擬跌破停損價後觸發自動停損掃描
        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute("UPDATE positions SET stop_loss_price = ? WHERE symbol = ?;", (price + 10.0, symbol))
        finally:
            conn.close()

        trig_status, trig_res = self._request("POST", "/api/positions/check-triggers", {})
        self.assertEqual(trig_status, 200)
        self.assertEqual(len(trig_res["triggered"]), 1)
        self.assertEqual(trig_res["triggered"][0]["order_type"], "STOP_LOSS")

    def test_conditional_forecast(self) -> None:
        status, res = self._request(
            "POST", "/api/forecast/simulate",
            {"symbol": "2330.TW", "market_change_pct": -3.0, "volume_multiplier": 2.0, "news_sentiment": "negative"},
        )
        self.assertEqual(status, 200)
        self.assertLessEqual(res["low_price"], res["expected_price"])
        self.assertLessEqual(res["expected_price"], res["high_price"])
        self.assertIn(res["risk_level"], ("低", "中", "高"))

    def test_source_switch_and_reality_check(self) -> None:
        s_switch, switched = self._request("POST", "/api/source", {"source": "yahoo"})
        self.assertEqual(s_switch, 200)
        self.assertEqual(switched["active_source"], "yahoo")
        self.assertEqual(len(switched["stocks"]), len(SYMBOL_NAMES))

        s_rc, rc = self._request("GET", "/api/reality-check")
        self.assertEqual(s_rc, 200)
        self.assertEqual(rc["source"], "yahoo")
        self.assertGreater(rc["total_samples"], 0)
        self.assertTrue(bool(rc["calibration_hint"]))


if __name__ == "__main__":
    unittest.main()
