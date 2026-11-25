"""Phase 1 核心模組單元測試：涵蓋資料庫初始化、數據驗證、排程重試與防呆風控。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.adapters.base import Quote
from app.core.risk_guard import OrderRequest, RiskGuard
from app.core.scheduler import TaskScheduler
from app.core.validator import save_validated_quotes, validate_quotes
from app.db.database import get_connection, init_db


class CoreModulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_app.db"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_init_db_seeds_defaults(self) -> None:
        conn = get_connection(self.db_path)
        try:
            risk = conn.execute("SELECT * FROM risk_settings WHERE id = 1;").fetchone()
            schedules = conn.execute("SELECT COUNT(*) AS cnt FROM schedule_configs;").fetchone()
            self.assertIsNotNone(risk)
            self.assertAlmostEqual(risk["max_single_position_pct"], 0.20)
            self.assertEqual(risk["max_orders_per_hour"], 3)
            self.assertEqual(schedules["cnt"], 6)
        finally:
            conn.close()

    def test_validator_filters_bad_quotes_and_saves_only_valid(self) -> None:
        base_time = datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc)
        quotes = [
            # 有效報價
            Quote("2330.TW", base_time, 1000.0, 1010.0, 995.0, 1005.0, 15000),
            # 價格為 0（異常）
            Quote("2330.TW", base_time + timedelta(minutes=5), 0.0, 1010.0, 995.0, 1005.0, 1000),
            # 高低價倒掛（異常）
            Quote("2330.TW", base_time + timedelta(minutes=10), 1005.0, 1000.0, 1010.0, 1005.0, 1000),
            # 單日暴漲超過 20%（異常）
            Quote("2330.TW", base_time + timedelta(minutes=15), 1005.0, 1300.0, 1005.0, 1300.0, 2000),
            # 未來時間戳（異常）
            Quote("2330.TW", base_time + timedelta(hours=2), 1005.0, 1015.0, 1000.0, 1010.0, 1000),
            # 正常第二筆（時間在有效第一筆之後）
            Quote("2330.TW", base_time + timedelta(minutes=20), 1005.0, 1015.0, 1002.0, 1012.0, 12000),
            # 時間倒退（晚出現但時間早於 1012 那筆）
            Quote("2330.TW", base_time + timedelta(minutes=18), 1005.0, 1010.0, 1002.0, 1008.0, 9000),
        ]

        result = validate_quotes(
            quotes,
            prev_closes={"2330.TW": 1000.0},
            max_daily_change_pct=0.20,
            now=base_time + timedelta(minutes=30),
        )
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(len(result.valid_quotes), 2)
        self.assertEqual(len(result.issues), 5)

        conn = get_connection(self.db_path)
        try:
            inserted = save_validated_quotes(conn, result)
            self.assertEqual(inserted, 2)
            count = conn.execute("SELECT COUNT(*) AS cnt FROM market_quotes;").fetchone()["cnt"]
            self.assertEqual(count, 2)
        finally:
            conn.close()

    def test_scheduler_retry_partial_and_manual_rerun(self) -> None:
        scheduler = TaskScheduler(self.db_path)
        attempts = {"count": 0}

        def flaky_fetch() -> int:
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise ConnectionError("模擬 API 暫時斷線")
            return 12

        def always_fail() -> int:
            raise RuntimeError("模擬上游完全失效")

        def partial_job() -> tuple[str, int, str]:
            return ("PARTIAL", 8, "有 2 檔標的驗證失敗已略過")

        scheduler.register_job("fetch_post_market", flaky_fetch, job_type="fetch_post", cron_expr="0 14 * * 1-5", max_retries=2)
        scheduler.register_job("broken_job", always_fail, job_type="strategy", cron_expr="30 14 * * 1-5", max_retries=1)
        scheduler.register_job("partial_job", partial_job, job_type="daily_report", cron_expr="0 15 * * 1-5", max_retries=0)

        res1 = scheduler.run_job("fetch_post_market")
        self.assertEqual(res1.status, "SUCCESS")
        self.assertEqual(res1.retry_count, 1)
        self.assertEqual(res1.records_processed, 12)

        res2 = scheduler.run_job("broken_job")
        self.assertEqual(res2.status, "FAILED")
        self.assertEqual(res2.retry_count, 1)
        self.assertIn("RuntimeError", res2.error_message or "")

        res3 = scheduler.rerun_job("partial_job")
        self.assertEqual(res3.status, "PARTIAL")
        self.assertEqual(res3.trigger_type, "manual")

        logs = scheduler.get_recent_logs(limit=10)
        self.assertEqual(len(logs), 3)

        scheduler.update_schedule("fetch_post_market", cron_expr="15 14 * * 1-5", enabled=False)
        with self.assertRaises(ValueError):
            scheduler.update_schedule("fetch_post_market", cron_expr="invalid-cron")

    def test_risk_guard_position_limit_confirmation_and_anti_fomo(self) -> None:
        guard = RiskGuard()
        now = datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc)
        conn = get_connection(self.db_path)
        try:
            # 1. 超過單一標的 20% 資金上限（總資金 100,000，買 25,000）
            oversized = OrderRequest("2330.TW", "BUY", 25, 1000.0, stop_loss_price=950.0, confirmed_by_user=True)
            res_size = guard.evaluate_order(
                conn, oversized, total_equity=100_000, current_symbol_value=0, daily_realized_pnl=0, now=now
            )
            self.assertFalse(res_size.allowed)
            self.assertIn("20%", res_size.reasons[0])

            # 2. 未勾選二次確認：必須攔截並計算最大可能虧損 (1000 - 950) * 10 = 500
            unconfirmed = OrderRequest("2330.TW", "BUY", 10, 1000.0, stop_loss_price=950.0, confirmed_by_user=False)
            res_confirm = guard.evaluate_order(
                conn, unconfirmed, total_equity=100_000, current_symbol_value=0, daily_realized_pnl=0, now=now
            )
            self.assertFalse(res_confirm.allowed)
            self.assertTrue(res_confirm.requires_confirmation)
            self.assertAlmostEqual(res_confirm.max_loss_estimate, 500.0)

            # 3. 確認後成功下 3 筆單，第 4 筆應觸發 1 小時內頻繁交易鎖定
            ok_order = OrderRequest("2330.TW", "BUY", 5, 1000.0, stop_loss_price=950.0, confirmed_by_user=True)
            for idx in range(3):
                res_ok = guard.evaluate_order(
                    conn, ok_order, total_equity=100_000, current_symbol_value=0, daily_realized_pnl=0,
                    now=now + timedelta(minutes=idx * 5),
                )
                self.assertTrue(res_ok.allowed)
                guard.record_order(
                    conn, ok_order, status="FILLED", max_loss_estimate=res_ok.max_loss_estimate,
                    now=now + timedelta(minutes=idx * 5),
                )

            res_freq = guard.evaluate_order(
                conn, ok_order, total_equity=100_000, current_symbol_value=0, daily_realized_pnl=0,
                now=now + timedelta(minutes=20),
            )
            self.assertFalse(res_freq.allowed)
            self.assertIsNotNone(res_freq.locked_until)

            # 4. 冷靜鎖定期內仍允許 SELL 停損/撤退，不可把使用者鎖死在虧損部位
            sell_order = OrderRequest("2330.TW", "SELL", 5, 940.0, confirmed_by_user=True)
            res_sell = guard.evaluate_order(
                conn, sell_order, total_equity=100_000, current_symbol_value=5_000, daily_realized_pnl=-6_000,
                now=now + timedelta(minutes=25),
            )
            self.assertTrue(res_sell.allowed)
        finally:
            conn.close()

    def test_risk_guard_daily_loss_lockout(self) -> None:
        guard = RiskGuard()
        now = datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc)
        conn = get_connection(self.db_path)
        try:
            buy_order = OrderRequest("0050.TW", "BUY", 10, 100.0, stop_loss_price=95.0, confirmed_by_user=True)
            res = guard.evaluate_order(
                conn, buy_order, total_equity=100_000, current_symbol_value=0, daily_realized_pnl=-5_000, now=now
            )
            self.assertFalse(res.allowed)
            self.assertIsNotNone(res.locked_until)
            self.assertIn("5%", res.reasons[0])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
