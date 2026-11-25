"""模擬市場資料產生器（實作 MarketDataAdapter 協定），供初期無須真實券商 API 即可完整演練。"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from app.adapters.base import Quote

# 預設新手觀察名單：代碼 -> (白話名稱, 基準參考價)
DEFAULT_WATCHLIST: dict[str, tuple[str, float]] = {
    "0050.TW": ("台灣 50 ETF", 195.0),
    "0056.TW": ("高股息 ETF", 38.0),
    "2330.TW": ("台積電", 1020.0),
    "2317.TW": ("鴻海", 198.0),
    "2454.TW": ("聯發科", 1280.0),
}


class MockMarketAdapter:
    """模擬市場資料轉接器：產生符合真實 K 線邏輯的 OHLCV 報價。"""

    source_name = "mock"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def _next_bar(self, symbol: str, prev_close: float, quote_time: datetime) -> Quote:
        """從前一收盤價推演單根合理 K 線（日內波動控制在 ±4.5% 內，確保通過風控驗證）。"""
        open_gap = self._rng.uniform(-0.012, 0.012)
        open_p = max(1.0, round(prev_close * (1 + open_gap), 2))
        body_pct = self._rng.uniform(-0.032, 0.035)
        close_p = max(1.0, round(open_p * (1 + body_pct), 2))

        upper_wick = self._rng.uniform(0.001, 0.015)
        lower_wick = self._rng.uniform(0.001, 0.015)
        high_p = round(max(open_p, close_p) * (1 + upper_wick), 2)
        low_p = max(0.5, round(min(open_p, close_p) * (1 - lower_wick), 2))

        base_vol = 12_000 if symbol.startswith("00") else 25_000
        volume = int(base_vol * self._rng.uniform(0.6, 2.2))

        return Quote(
            symbol=symbol,
            quote_time=quote_time,
            open=open_p,
            high=high_p,
            low=low_p,
            close=close_p,
            volume=volume,
            source=self.source_name,
        )

    def seed_history(
        self,
        symbols: list[str] | None = None,
        *,
        days: int = 30,
        end_time: datetime | None = None,
    ) -> list[Quote]:
        """產生過去 N 個交易日的模擬歷史報價，讓均線與勝率指標開箱即可計算。"""
        target_symbols = symbols or list(DEFAULT_WATCHLIST.keys())
        end_utc = (end_time or datetime.now(timezone.utc)).astimezone(timezone.utc)
        # 對齊到前一個小時整點之前，確保後續即時刷新不會與歷史時間重疊
        anchor = end_utc.replace(minute=0, second=0, microsecond=0) - timedelta(days=days)

        quotes: list[Quote] = []
        for symbol in target_symbols:
            _, base_price = DEFAULT_WATCHLIST.get(symbol, (symbol, 100.0))
            prev_close = base_price
            for offset in range(days):
                q_time = anchor + timedelta(days=offset)
                bar = self._next_bar(symbol, prev_close, q_time)
                prev_close = bar.close
                quotes.append(bar)
        return quotes

    def fetch_quotes(
        self,
        symbols: list[str],
        *,
        last_closes: dict[str, float] | None = None,
        quote_time: datetime | None = None,
    ) -> list[Quote]:
        """抓取指定標的清單的最新模擬報價（供排程任務定期呼叫）。"""
        now_utc = (quote_time or datetime.now(timezone.utc)).astimezone(timezone.utc)
        closes = last_closes or {}
        quotes: list[Quote] = []
        for symbol in symbols:
            _, default_price = DEFAULT_WATCHLIST.get(symbol, (symbol, 100.0))
            prev_close = closes.get(symbol, default_price)
            quotes.append(self._next_bar(symbol, prev_close, now_utc))
        return quotes
