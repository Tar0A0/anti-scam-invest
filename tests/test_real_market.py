"""真實市場 Adapter、資料源隔離與 Reality Check 回測單元測試（離線模擬回應）。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.adapters.base import Quote
from app.adapters.yahoo import YahooFinanceAdapter, parse_yahoo_chart_payload
from app.core.broker import get_latest_prices
from app.core.reality_check import evaluate_strategy_accuracy
from app.core.strategy import run_strategy_and_save
from app.core.validator import save_validated_quotes, validate_quotes
from app.db.database import get_connection, init_db


def _make_chart_payload(closes: list[float], start_ts: int = 1760000000) -> dict:
    timestamps = [start_ts + i * 86400 for i in range(len(closes))]
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": closes,
                                "high": [round(c * 1.01, 2) for c in closes],
                                "low": [round(c * 0.99, 2) for c in closes],
                                "close": closes,
                                "volume": [10000 + i * 500 for i in range(len(closes))],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


class RealMarketTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "real_market_test.db"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_parse_yahoo_skips_none_deduplicates_and_saves(self) -> None:
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1760000000, 1760086400, 1760086400, 1760172800],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [100.0, None, 102.0, 103.0],
                                    "high": [102.0, None, 104.0, 105.0],
                                    "low": [99.0, None, 101.0, 102.0],
                                    "close": [101.0, None, 103.0, 104.0],
                                    "volume": [5000, None, 7000, None],
                                }
                            ]
                        },
                    }
                ],
                "error": None,
            }
        }
        quotes = parse_yahoo_chart_payload("2330.TW", payload)
        self.assertEqual(len(quotes), 3)
        self.assertEqual(quotes[-1].volume, 0)
        self.assertEqual(quotes[0].source, "yahoo")

        res = validate_quotes(quotes, now=datetime(2026, 11, 25, tzinfo=timezone.utc))
        self.assertEqual(res.status, "SUCCESS")
        conn = get_connection(self.db_path)
        try:
            saved = save_validated_quotes(conn, res)
            self.assertEqual(saved, 3)
        finally:
            conn.close()

    def test_yahoo_adapter_partial_failure(self) -> None:
        def fake_opener(req, timeout=10.0):  # noqa: ARG001
            if "BAD" in req.full_url:
                raise ConnectionError("network error")
            data = json.dumps(_make_chart_payload([100.0, 101.0, 102.0])).encode("utf-8")
            return io.BytesIO(data)

        adapter = YahooFinanceAdapter(opener=fake_opener)
        quotes = adapter.fetch_quotes(["2330.TW", "BAD.TW"])
        self.assertEqual(len(quotes), 3)
        self.assertIn("BAD.TW", adapter.last_errors)

    def test_source_isolation_and_reality_check(self) -> None:
        conn = get_connection(self.db_path)
        try:
            base = datetime(2026, 9, 1, tzinfo=timezone.utc)
            # 寫入 35 筆緩漲後回檔的 yahoo 報價與價格截然不同的 mock 報價
            yahoo_quotes = []
            price = 500.0
            for i in range(35):
                price = round(price * (1.012 if i % 4 != 3 else 0.985), 2)
                yahoo_quotes.append(
                    Quote("2330.TW", base + timedelta(days=i), price, price * 1.01, price * 0.99, price, 20000 + i * 1000, "yahoo")
                )
            mock_quotes = [
                Quote("2330.TW", base + timedelta(days=34), 999.0, 1005.0, 995.0, 999.0, 10000, "mock")
            ]

            save_validated_quotes(conn, validate_quotes(yahoo_quotes, now=base + timedelta(days=40)))
            save_validated_quotes(conn, validate_quotes(mock_quotes, now=base + timedelta(days=40)))

            latest_yahoo = get_latest_prices(conn, source="yahoo")
            latest_mock = get_latest_prices(conn, source="mock")
            self.assertAlmostEqual(latest_yahoo["2330.TW"], yahoo_quotes[-1].close)
            self.assertAlmostEqual(latest_mock["2330.TW"], 999.0)

            processed = run_strategy_and_save(conn, {"2330.TW": "台積電"}, source="yahoo")
            self.assertEqual(processed, 1)

            rc = evaluate_strategy_accuracy(conn, {"2330.TW": "台積電"}, source="yahoo", horizon_days=5)
            self.assertEqual(rc["source"], "yahoo")
            self.assertGreater(rc["total_samples"], 0)
            self.assertGreaterEqual(rc["accuracy_pct"], 0.0)
            self.assertLessEqual(rc["accuracy_pct"], 100.0)
            self.assertTrue(bool(rc["calibration_hint"]))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
