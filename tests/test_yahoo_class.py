"""Yahoo 台股全類別爬蟲、Cron 排程匹配與網站端點測試。"""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from app.adapters.yahoo_class import (
    YahooClassScraper,
    parse_class_page_categories,
    parse_class_quote_item,
)
from app.core.scheduler import cron_matches
from app.db.database import get_connection, init_db
from app.web import create_server

SAMPLE_CLASS_HTML = """
<html><body>
  <a href="/class-quote?sectorId=1&amp;exchange=TAI">水泥</a>
  <a href="/class-quote?sectorId=141&amp;exchange=TWO">櫃電子</a>
  <a href="/class-quote?sectorId=99300&amp;exchange=OES">興櫃半導體</a>
  <a href="/class-quote?category=AI%E4%BA%BA%E5%B7%A5%E6%99%BA%E6%85%A7&amp;categoryLabel=%E6%A6%82%E5%BF%B5%E8%82%A1">AI人工智慧</a>
  <a href="/class-quote?category=%E5%8F%B0%E7%A9%8D%E9%9B%BB&amp;categoryLabel=%E9%9B%86%E5%9C%98%E8%82%A1">台積電</a>
  <a href="/class-quote?category=PCB&amp;categoryLabel=%E9%9B%BB%E5%AD%90%E7%94%A2%E6%A5%AD">PCB</a>
</body></html>
"""


class YahooClassTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "class_test.db"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_parse_categories_and_quote_item(self) -> None:
        cats = parse_class_page_categories(SAMPLE_CLASS_HTML)
        groups = {c.group_type for c in cats}
        self.assertEqual(groups, {"上市", "上櫃", "興櫃", "概念股", "集團股", "電子產業"})
        self.assertEqual(len(cats), 6)

        item = parse_class_quote_item(
            {
                "symbol": "2330.TW",
                "symbolName": "台積電",
                "exchange": "TAI",
                "price": {"raw": "2,150"},
                "change": {"raw": "-80"},
                "changePercent": "-3.59%",
                "changeStatus": "down",
                "regularMarketOpen": {"raw": "2200"},
                "regularMarketDayHigh": {"raw": "2210"},
                "regularMarketDayLow": {"raw": "2140"},
                "regularMarketPreviousClose": {"raw": "2230"},
                "volume": "52,000,000",
                "turnoverM": "111800.5",
                "limitUp": False,
                "limitDown": False,
                "marketStatus": "close",
                "regularMarketTime": "2026-11-25T05:30:00Z",
            }
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["price"], 2150.0)
        self.assertEqual(item["change_pct"], -3.59)
        self.assertEqual(item["volume"], 52_000_000)

        # 空值或 '-' 不可崩潰
        dash_item = parse_class_quote_item({"symbol": "9999.TW", "price": "-", "volume": None})
        self.assertIsNotNone(dash_item)
        assert dash_item is not None
        self.assertIsNone(dash_item["price"])
        self.assertEqual(dash_item["volume"], 0)

    def test_sync_all_with_pagination_and_partial_failure(self) -> None:
        def fake_opener(req, timeout=12.0):  # noqa: ARG001
            url = req.full_url
            if url.endswith("/class"):
                return io.BytesIO(SAMPLE_CLASS_HTML.encode("utf-8"))
            if "exchange=TWO" in url:
                raise ConnectionError("模擬上櫃分類暫時失敗")
            if "offset=0" in url and "sectorId=1" in url:
                payload = {
                    "data": {
                        "list": [{"symbol": "1101.TW", "symbolName": "台泥", "price": {"raw": "24.4"}, "changePercent": "-3.17%", "volume": "29040000", "turnoverM": "710"}],
                        "pagination": {"resultsTotal": 2, "nextOffset": "30"},
                    }
                }
                return io.BytesIO(json.dumps(payload).encode("utf-8"))
            if "offset=30" in url and "sectorId=1" in url:
                payload = {
                    "data": {
                        "list": [{"symbol": "1102.TW", "symbolName": "亞泥", "price": {"raw": "40.1"}, "changePercent": "+1.25%", "volume": "8000000", "turnoverM": "320"}],
                        "pagination": {"resultsTotal": 2, "nextOffset": None},
                    }
                }
                return io.BytesIO(json.dumps(payload).encode("utf-8"))

            payload = {
                "data": {
                    "list": [{"symbol": "2330.TW", "symbolName": "台積電", "price": {"raw": "2150"}, "changePercent": "+2.00%", "volume": "1000000", "turnoverM": "2150"}],
                    "pagination": {"resultsTotal": 1, "nextOffset": None},
                }
            }
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        scraper = YahooClassScraper(opener=fake_opener)
        summary = scraper.sync_all_categories(self.db_path, max_workers=2)
        self.assertEqual(summary["total_categories"], 6)
        self.assertEqual(summary["synced_categories"], 5)
        self.assertEqual(summary["failed_categories"], 1)
        self.assertGreaterEqual(summary["total_quote_rows"], 6)

        conn = get_connection(self.db_path)
        try:
            cement_quotes = conn.execute(
                "SELECT symbol FROM yahoo_category_quotes WHERE category_key = 'sector:TAI:1' ORDER BY symbol;"
            ).fetchall()
            self.assertEqual([r["symbol"] for r in cement_quotes], ["1101.TW", "1102.TW"])
            snaps = conn.execute("SELECT COUNT(*) AS cnt FROM yahoo_category_daily_snapshots;").fetchone()["cnt"]
            self.assertEqual(snaps, 5)
        finally:
            conn.close()

    def test_cron_matches(self) -> None:
        tpe = timezone(timedelta(hours=8))
        # 2026-11-25 為星期三 (weekday=2, cron_dow=3)
        wed_0915 = datetime(2026, 11, 25, 9, 15, tzinfo=tpe)
        wed_0917 = datetime(2026, 11, 25, 9, 17, tzinfo=tpe)
        sat_0915 = datetime(2026, 11, 28, 9, 15, tzinfo=tpe)
        self.assertTrue(cron_matches("*/15 9-14 * * 1-5", wed_0915))
        self.assertFalse(cron_matches("*/15 9-14 * * 1-5", wed_0917))
        self.assertFalse(cron_matches("*/15 9-14 * * 1-5", sat_0915))

    def test_web_classes_endpoints(self) -> None:
        def fake_opener(req, timeout=12.0):  # noqa: ARG001
            if req.full_url.endswith("/class"):
                return io.BytesIO(SAMPLE_CLASS_HTML.encode("utf-8"))
            payload = {
                "data": {
                    "list": [{"symbol": "1101.TW", "symbolName": "台泥", "price": {"raw": "24.4"}, "changePercent": "-3.17%", "volume": "1000000", "turnoverM": "24.4"}],
                    "pagination": {"resultsTotal": 1, "nextOffset": None},
                }
            }
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        scraper = YahooClassScraper(opener=fake_opener)
        scraper.sync_all_categories(self.db_path, max_workers=2)

        server = create_server(
            db_path=self.db_path,
            host="127.0.0.1",
            port=0,
            adapter_seed=42,
            default_source="mock",
            class_scraper=scraper,
        )
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(Request(f"{base_url}/api/classes?group=%E4%B8%8A%E5%B8%82"), timeout=5) as r1:
                d1 = json.loads(r1.read().decode("utf-8"))
                self.assertEqual(len(d1["categories"]), 1)
                self.assertEqual(d1["categories"][0]["name"], "水泥")

            with urlopen(Request(f"{base_url}/api/classes/sector%3ATAI%3A1/quotes"), timeout=5) as r2:
                d2 = json.loads(r2.read().decode("utf-8"))
                self.assertEqual(d2["quotes"][0]["symbol"], "1101.TW")

            with urlopen(Request(f"{base_url}/api/classes/sector%3ATAI%3A1/quotes?refresh=1"), timeout=5) as r2r:
                d2r = json.loads(r2r.read().decode("utf-8"))
                self.assertEqual(d2r["quotes"][0]["symbol"], "1101.TW")

            with urlopen(Request(f"{base_url}/api/classes/export.csv?category_key=sector%3ATAI%3A1"), timeout=5) as r3:
                csv_text = r3.read().decode("utf-8-sig")
                self.assertIn("1101.TW", csv_text)
                self.assertIn("台泥", csv_text)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
