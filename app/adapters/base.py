"""市場資料來源介面（Adapter Pattern），支援模擬市場與真實券商/行情 API 無縫切換。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Quote:
    """標準化單筆 K 線報價結構，所有資料源轉接器皆須轉換為此格式。"""

    symbol: str
    quote_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    source: str = "mock"


class MarketDataAdapter(Protocol):
    """市場資料轉接器介面（未來對接 FinMind / Fugle / Yahoo Finance / Alpaca 時實作此協定）。"""

    source_name: str

    def fetch_quotes(self, symbols: list[str]) -> list[Quote]:
        """抓取指定標的清單的最新報價。"""
        ...
