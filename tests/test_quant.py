"""量化分析與風控專屬單元測試：驗證 RSI、ATR、支撐壓力、建議停損停利、回測風險指標與寬停損提醒。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.adapters.base import Quote
from app.core.reality_check import _compute_max_drawdown_pct, evaluate_strategy_accuracy
from app.core.risk_guard import OrderRequest, RiskGuard
from app.core.strategy import _classify_rsi, analyze_quotes
from app.core.validator import save_validated_quotes, validate_quotes
from app.db.database import get_connection, init_db


def _make_close_only_quotes(closes: list[float], volumes: list[int] | None = None) -> list[dict]:
    vols = volumes or [10000 for _ in closes]
    return [{"close": c, "volume": v} for c, v in zip(closes, vols)]


def _make_ohlc_quotes(closes: list[float], spread_pct: float = 0.015) -> list[dict]:
    return [
        {
            "high": round(c * (1.0 + spread_pct), 2),
            "low": round(c * (1.0 - spread_pct), 2),
            "close": c,
            "volume": 15000 + i * 200,
        }
        for i, c in enumerate(closes)
    ]


class QuantMetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "quant_test.db"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_rsi_boundaries_and_labels(self) -> None:
        # 1. 連續上漲 -> RSI = 100, 過熱(>70)
        up = analyze_quotes("2330.TW", "台積電", _make_close_only_quotes([100.0 + i * 2.0 for i in range(20)]))
        self.assertEqual(up["rsi14"], 100.0)
        self.assertEqual(up["rsi_label"], "過熱(>70)")
        self.assertIn("過熱", up["plain_reason"])

        # 2. 連續下跌 -> RSI = 0, 超賣(<30)
        down = analyze_quotes("2330.TW", "台積電", _make_close_only_quotes([200.0 - i * 3.0 for i in range(20)]))
        self.assertEqual(down["rsi14"], 0.0)
        self.assertEqual(down["rsi_label"], "超賣(<30)")
        self.assertIn("超賣", down["plain_reason"])

        # 3. 持平不動 -> RSI = 50, 中性(45~55)
        flat = analyze_quotes("0050.TW", "台灣50", _make_close_only_quotes([100.0] * 10))
        self.assertEqual(flat["rsi14"], 50.0)
        self.assertEqual(flat["rsi_label"], "中性(45~55)")

        # 4. 單筆與極短樣本（不足 14 日）亦能正常計算且落在 0~100
        single = analyze_quotes("0056.TW", "高股息", _make_close_only_quotes([35.0]))
        self.assertEqual(single["rsi14"], 50.0)
        self.assertEqual(single["rsi_label"], "中性(45~55)")

        short_up = analyze_quotes("0056.TW", "高股息", _make_close_only_quotes([35.0, 36.0, 35.5, 36.5, 37.0]))
        self.assertGreaterEqual(short_up["rsi14"], 0.0)
        self.assertLessEqual(short_up["rsi14"], 100.0)
        self.assertIn(short_up["rsi_label"], ("過熱(>70)", "偏強(55~70)", "中性(45~55)", "偏弱(30~45)", "超賣(<30)"))

        # 5. 五段白話體溫邊界完整驗證
        self.assertEqual(_classify_rsi(70.01), "過熱(>70)")
        self.assertEqual(_classify_rsi(70.0), "偏強(55~70)")
        self.assertEqual(_classify_rsi(55.0), "偏強(55~70)")
        self.assertEqual(_classify_rsi(54.99), "中性(45~55)")
        self.assertEqual(_classify_rsi(45.0), "中性(45~55)")
        self.assertEqual(_classify_rsi(44.99), "偏弱(30~45)")
        self.assertEqual(_classify_rsi(30.0), "偏弱(30~45)")
        self.assertEqual(_classify_rsi(29.99), "超賣(<30)")

        for res in (up, down, flat, single, short_up):
            self.assertGreaterEqual(res["health_score"], 0)
            self.assertLessEqual(res["health_score"], 100)

    def test_support_resistance_atr_and_suggested_stops(self) -> None:
        closes = [
            100.0, 102.0, 101.0, 103.5, 105.0, 104.0, 106.0, 108.0, 107.0, 105.5,
            104.0, 103.0, 105.0, 107.5, 109.0, 110.0, 108.5, 107.0, 106.0, 107.5,
        ]
        # 同時測試 close-only（模擬 web.py get_overview 查詢）與完整 OHLC
        for quotes in (_make_close_only_quotes(closes), _make_ohlc_quotes(closes, spread_pct=0.02)):
            res = analyze_quotes("2317.TW", "鴻海", quotes)
            self.assertGreater(res["atr14_pct"], 0.0)
            self.assertLessEqual(res["support_price"], res["latest_close"])
            self.assertGreaterEqual(res["resistance_price"], res["latest_close"])
            self.assertGreater(res["suggested_stop_loss"], 0.0)
            self.assertLess(res["suggested_stop_loss"], res["latest_close"])
            self.assertGreater(res["suggested_take_profit"], res["latest_close"])
            # 建議停損不應過寬（避免觸發 >15% 寬停損警示）
            stop_dist = (res["latest_close"] - res["suggested_stop_loss"]) / res["latest_close"]
            self.assertLessEqual(stop_dist, 0.15)
            # 白話理由應包含支撐或壓力參考
            self.assertIn("支撐", res["plain_reason"])

        # 極端情境：收盤正好位於 20 日低點或 20 日高點，仍需滿足 suggested_stop_loss < latest_close < suggested_take_profit
        at_low = analyze_quotes("2454.TW", "聯發科", _make_close_only_quotes([120.0 - i * 1.0 for i in range(20)]))
        self.assertLess(at_low["suggested_stop_loss"], at_low["latest_close"])
        self.assertGreater(at_low["suggested_take_profit"], at_low["latest_close"])

        at_high = analyze_quotes("2454.TW", "聯發科", _make_close_only_quotes([100.0 + i * 1.5 for i in range(20)]))
        self.assertLess(at_high["suggested_stop_loss"], at_high["latest_close"])
        self.assertGreater(at_high["suggested_take_profit"], at_high["latest_close"])

    def test_reality_check_extended_metrics(self) -> None:
        # 驗證最大回撤計算邏輯：+10% -> -20% -> +5% 應為 20.0%
        self.assertAlmostEqual(_compute_max_drawdown_pct([10.0, -20.0, 5.0]), 20.0, places=2)
        self.assertEqual(_compute_max_drawdown_pct([5.0, 3.0, 4.0]), 0.0)
        self.assertEqual(_compute_max_drawdown_pct([]), 0.0)

        conn = get_connection(self.db_path)
        try:
            base = datetime(2026, 8, 1, tzinfo=timezone.utc)
            # 構造包含先漲、過熱回檔、再震盪反彈的 50 日序列，同時觸發正向與負向訊號結果
            prices = [
                100.0, 100.5, 101.0, 101.2, 101.8, 102.0, 102.5, 103.0, 103.2, 103.8,
                104.0, 104.5, 105.0, 105.2, 105.8, 106.0, 106.5, 107.0, 107.5, 108.0,
                109.5, 111.0, 113.0, 116.0, 120.0, 118.0, 114.0, 110.0, 106.0, 103.0,
                104.0, 106.0, 109.0, 112.0, 115.0, 111.0, 107.0, 104.0, 101.0, 102.0,
                104.0, 106.5, 109.0, 112.0, 116.0, 119.0, 115.0, 112.0, 114.0, 117.0,
            ]
            quotes = [
                Quote(
                    "2330.TW",
                    base + timedelta(days=i),
                    p,
                    round(p * 1.015, 2),
                    round(p * 0.985, 2),
                    p,
                    15000 + (8000 if i % 5 == 0 else 0),
                    "yahoo",
                )
                for i, p in enumerate(prices)
            ]
            res_val = validate_quotes(quotes, max_daily_change_pct=0.25, now=base + timedelta(days=60))
            save_validated_quotes(conn, res_val)

            rc = evaluate_strategy_accuracy(conn, {"2330.TW": "台積電"}, source="yahoo", horizon_days=5)
            self.assertIn("max_drawdown_pct", rc)
            self.assertIn("profit_factor", rc)
            self.assertIn("win_loss_ratio", rc)
            self.assertGreater(rc["directional_signals"], 0)
            self.assertGreater(rc["max_drawdown_pct"], 0.0)
            self.assertIsNotNone(rc["profit_factor"])
            self.assertIsNotNone(rc["win_loss_ratio"])
            self.assertGreater(rc["profit_factor"], 0.0)
            self.assertGreater(rc["win_loss_ratio"], 0.0)
            self.assertIn("最大回撤", rc["calibration_hint"])
            self.assertIn("盈虧比", rc["calibration_hint"])
        finally:
            conn.close()

    def test_reality_check_no_losses_returns_null_profit_factor(self) -> None:
        conn = get_connection(self.db_path)
        try:
            base = datetime(2026, 8, 1, tzinfo=timezone.utc)
            # 穩步緩漲且量能微放，所有順勢買進訊號 5 日後皆為正報酬（無虧損）
            prices = [round(100.0 * (1.006 ** i), 2) for i in range(40)]
            quotes = [
                Quote(
                    "0050.TW",
                    base + timedelta(days=i),
                    p,
                    round(p * 1.005, 2),
                    round(p * 0.995, 2),
                    p,
                    10000 + i * 600,
                    "yahoo",
                )
                for i, p in enumerate(prices)
            ]
            save_validated_quotes(conn, validate_quotes(quotes, now=base + timedelta(days=50)))
            rc = evaluate_strategy_accuracy(conn, {"0050.TW": "台灣50"}, source="yahoo", horizon_days=5)
            self.assertEqual(rc["max_drawdown_pct"], 0.0)
            self.assertIsNone(rc["profit_factor"])
            self.assertIsNone(rc["win_loss_ratio"])
        finally:
            conn.close()

    def test_risk_guard_wide_stop_loss_warning_without_blocking(self) -> None:
        guard = RiskGuard()
        now = datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc)
        conn = get_connection(self.db_path)
        try:
            # 1. 停損距離 20%（> 15%）：未確認預覽時附帶提醒
            wide_preview = OrderRequest(
                "2330.TW", "BUY", 10, 1000.0, stop_loss_price=800.0, confirmed_by_user=False
            )
            res_prev = guard.evaluate_order(
                conn, wide_preview, total_equity=100_000, current_symbol_value=0, daily_realized_pnl=0, now=now
            )
            self.assertFalse(res_prev.allowed)
            self.assertTrue(res_prev.requires_confirmation)
            self.assertTrue(any("15%" in r for r in res_prev.reasons))

            # 2. 停損距離 20%（> 15%）：已確認下單不阻擋，但仍在 reasons 中溫馨提醒
            wide_confirmed = OrderRequest(
                "2330.TW", "BUY", 10, 1000.0, stop_loss_price=800.0, confirmed_by_user=True
            )
            res_ok = guard.evaluate_order(
                conn, wide_confirmed, total_equity=100_000, current_symbol_value=0, daily_realized_pnl=0, now=now
            )
            self.assertTrue(res_ok.allowed)
            self.assertFalse(res_ok.requires_confirmation)
            self.assertTrue(any("15%" in r for r in res_ok.reasons))

            # 3. 停損距離剛好 15%（未超過 15%）：不觸發寬停損警示
            exact_15 = OrderRequest(
                "2330.TW", "BUY", 10, 1000.0, stop_loss_price=850.0, confirmed_by_user=True
            )
            res_15 = guard.evaluate_order(
                conn, exact_15, total_equity=100_000, current_symbol_value=0, daily_realized_pnl=0, now=now
            )
            self.assertTrue(res_15.allowed)
            self.assertEqual(res_15.reasons, [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
