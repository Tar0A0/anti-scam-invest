"""Yahoo Finance 真實市場資料轉接器（實作 MarketDataAdapter 協定，免 API Key）。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote as url_quote
from urllib.request import Request, urlopen

from app.adapters.base import Quote

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StockMate/1.0"


def parse_yahoo_chart_payload(symbol: str, payload: dict[str, Any]) -> list[Quote]:
    """解析 Yahoo Finance `/v8/finance/chart` JSON 回應並轉為標準化 Quote 清單。"""
    chart = payload.get("chart") or {}
    if chart.get("error"):
        err = chart["error"]
        raise ValueError(f"Yahoo Finance 回傳錯誤：{err.get('description') or err}")

    results = chart.get("result") or []
    if not results:
        return []

    first = results[0]
    timestamps = first.get("timestamp") or []
    quote_block = ((first.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote_block.get("open") or []
    highs = quote_block.get("high") or []
    lows = quote_block.get("low") or []
    closes = quote_block.get("close") or []
    volumes = quote_block.get("volume") or []

    # 依時間戳記去重（若同一交易日同時含收盤與盤中最新點，保留較後面那筆）
    by_ts: dict[int, Quote] = {}
    for idx, ts in enumerate(timestamps):
        if ts is None:
            continue
        o = opens[idx] if idx < len(opens) else None
        h = highs[idx] if idx < len(highs) else None
        l_val = lows[idx] if idx < len(lows) else None
        c = closes[idx] if idx < len(closes) else None
        v = volumes[idx] if idx < len(volumes) else None

        # 跳過休市或尚未成交完成的殘缺 K 棒
        if any(x is None for x in (o, h, l_val, c)):
            continue

        ts_int = int(ts)
        q_time = datetime.fromtimestamp(ts_int, tz=timezone.utc)
        by_ts[ts_int] = Quote(
            symbol=symbol,
            quote_time=q_time,
            open=round(float(o), 4),
            high=round(float(h), 4),
            low=round(float(l_val), 4),
            close=round(float(c), 4),
            volume=max(0, int(v)) if v is not None else 0,
            source="yahoo",
        )

    return [by_ts[k] for k in sorted(by_ts.keys())]


class YahooFinanceAdapter:
    """真實市場轉接器：透過 Yahoo Finance 公開圖表端點抓取歷史與最新日線報價。"""

    # shortcut: 使用 Yahoo Finance 公開圖表端點（免 API Key，盤中約延遲 15 分鐘）；未來需即時逐筆報價再新增 FugleAdapter / AlpacaAdapter
    source_name = "yahoo"

    def __init__(
        self,
        *,
        range_str: str = "3mo",
        timeout_sec: float = 10.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.range_str = range_str
        self.timeout_sec = timeout_sec
        self._opener = opener or urlopen
        self.last_errors: dict[str, str] = {}

    def fetch_symbol(self, symbol: str, *, range_str: str | None = None) -> list[Quote]:
        """抓取單一標的最近日線報價。"""
        rng = range_str or self.range_str
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{url_quote(symbol, safe='')}?range={rng}&interval=1d"
        req = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"})
        with self._opener(req, timeout=self.timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return parse_yahoo_chart_payload(symbol, payload)

    def fetch_quotes(self, symbols: list[str]) -> list[Quote]:
        """批次抓取多檔標的，單檔失敗時保留其餘成功標的並記錄 last_errors。"""
        self.last_errors = {}
        all_quotes: list[Quote] = []
        for symbol in symbols:
            try:
                quotes = self.fetch_symbol(symbol)
                if not quotes:
                    self.last_errors[symbol] = "未回傳任何有效報價"
                else:
                    all_quotes.extend(quotes)
            except Exception as exc:  # noqa: BLE001 - 單檔失敗不應拖垮整批查詢
                self.last_errors[symbol] = f"{type(exc).__name__}: {exc}"
        return all_quotes
