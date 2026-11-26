"""Yahoo Finance 真實市場資料轉接器（實作 MarketDataAdapter 協定，免 API Key）。"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote as url_quote
from urllib.request import Request, urlopen

from app.adapters.base import Quote

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StockMate/1.0"
TAIPEI_TZ = timezone(timedelta(hours=8))


def _finite_positive_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    val = float(value)
    return val if math.isfinite(val) and val > 0 else None


def _finite_nonneg_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    val = float(value)
    return int(val) if math.isfinite(val) and val >= 0 else None


def parse_yahoo_chart_payload(symbol: str, payload: dict[str, Any]) -> list[Quote]:
    """解析 Yahoo Finance `/v8/finance/chart` JSON 回應並轉為標準化 Quote 清單（含 meta 最新報價補齊）。"""
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
    # 記錄各台北交易日原始 timestamp 與非空 open，供 meta 補齊／更新同日 K 棒使用
    raw_day_ts: dict[date, int] = {}
    raw_day_open: dict[date, float] = {}
    for idx, ts in enumerate(timestamps):
        if ts is None:
            continue
        ts_int = int(ts)
        q_time = datetime.fromtimestamp(ts_int, tz=timezone.utc)
        tpe_day = q_time.astimezone(TAIPEI_TZ).date()
        raw_day_ts.setdefault(tpe_day, ts_int)

        o = opens[idx] if idx < len(opens) else None
        h = highs[idx] if idx < len(highs) else None
        l_val = lows[idx] if idx < len(lows) else None
        c = closes[idx] if idx < len(closes) else None
        v = volumes[idx] if idx < len(volumes) else None

        parsed_o = _finite_positive_float(o)
        if parsed_o is not None:
            raw_day_open.setdefault(tpe_day, parsed_o)

        # 跳過休市或尚未成交完成的殘缺 K 棒
        if any(x is None for x in (o, h, l_val, c)):
            continue

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

    quotes = [by_ts[k] for k in sorted(by_ts.keys())]

    # 補齊或更新 meta.regularMarketPrice / regularMarketTime（解決日線陣列落後一天或盤中尚未更新問題）
    meta = first.get("meta") or {}
    rm_ts = _finite_nonneg_int(meta.get("regularMarketTime"))
    rm_price = _finite_positive_float(meta.get("regularMarketPrice"))
    if rm_ts and rm_price is not None:
        rm_dt = datetime.fromtimestamp(rm_ts, tz=timezone.utc)
        rm_tpe_day = rm_dt.astimezone(TAIPEI_TZ).date()
        rm_high = _finite_positive_float(meta.get("regularMarketDayHigh"))
        rm_low = _finite_positive_float(meta.get("regularMarketDayLow"))
        rm_vol = _finite_nonneg_int(meta.get("regularMarketVolume"))

        # 查找最後一筆同台北交易日 K 棒
        same_day_idx: int | None = None
        for idx in range(len(quotes) - 1, -1, -1):
            if quotes[idx].quote_time.astimezone(TAIPEI_TZ).date() == rm_tpe_day:
                same_day_idx = idx
                break

        if same_day_idx is not None:
            # 同一台北交易日：保留原 quote_time 與 open，原地更新最新 OHLCV（避免資料庫同日產生兩筆重複日線）
            existing = quotes[same_day_idx]
            open_p = existing.open
            high_candidates = [existing.high, open_p, rm_price]
            low_candidates = [existing.low, open_p, rm_price]
            if rm_high is not None:
                high_candidates.append(rm_high)
            if rm_low is not None:
                low_candidates.append(rm_low)
            high_p = round(max(high_candidates), 4)
            low_p = round(min(low_candidates), 4)
            close_p = round(rm_price, 4)
            vol = rm_vol if (rm_vol is not None and rm_vol > 0) else existing.volume
            quotes[same_day_idx] = Quote(
                symbol=symbol,
                quote_time=existing.quote_time,
                open=open_p,
                high=high_p,
                low=low_p,
                close=close_p,
                volume=vol,
                source="yahoo",
            )
        elif not quotes or rm_tpe_day > quotes[-1].quote_time.astimezone(TAIPEI_TZ).date():
            # 更新的台北交易日：補上今日 K 棒
            open_p = raw_day_open.get(rm_tpe_day, rm_price)
            high_candidates = [open_p, rm_price]
            low_candidates = [open_p, rm_price]
            if rm_high is not None:
                high_candidates.append(rm_high)
            if rm_low is not None:
                low_candidates.append(rm_low)
            high_p = round(max(high_candidates), 4)
            low_p = round(min(low_candidates), 4)
            bar_time = (
                datetime.fromtimestamp(raw_day_ts[rm_tpe_day], tz=timezone.utc)
                if rm_tpe_day in raw_day_ts
                else rm_dt
            )
            if not quotes or bar_time > quotes[-1].quote_time:
                quotes.append(
                    Quote(
                        symbol=symbol,
                        quote_time=bar_time,
                        open=round(open_p, 4),
                        high=high_p,
                        low=low_p,
                        close=round(rm_price, 4),
                        volume=rm_vol if rm_vol is not None else 0,
                        source="yahoo",
                    )
                )

    return quotes


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
