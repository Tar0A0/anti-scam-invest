"""數據驗證與入庫模組：攔截缺漏值、異常價格、極端漲跌幅與錯亂時間戳，避免髒資料污染分析。"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.adapters.base import Quote


def _to_utc(dt: datetime) -> datetime:
    """將 datetime 統一正規化為 UTC 時區（無時區資訊者視為 UTC）。"""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _parse_finite_float(value: Any) -> float | None:
    """安全解析有限浮點數，攔截 None、bool、非數值型別與 NaN/Inf。"""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    val = float(value)
    return val if math.isfinite(val) else None


def _parse_non_negative_int(value: Any) -> int | None:
    """安全解析非負整數成交量，拒絕 bool、浮點數與字串型別異常。"""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    return value


@dataclass(frozen=True)
class ValidationIssue:
    """單筆報價驗證失敗的紀錄。"""

    symbol: str
    quote_time: str
    reason: str


@dataclass
class ValidationResult:
    """批次驗證結果摘要，同時提供排程日誌需要的狀態判別（SUCCESS / PARTIAL / FAILED）。"""

    valid_quotes: list[Quote] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def status(self) -> str:
        """根據有效筆數與異常筆數回傳標準狀態。"""
        if not self.issues:
            return "SUCCESS"
        return "PARTIAL" if self.valid_quotes else "FAILED"


def validate_quotes(
    quotes: list[Quote],
    prev_closes: dict[str, float] | None = None,
    *,
    max_daily_change_pct: float = 0.20,
    now: datetime | None = None,
) -> ValidationResult:
    """驗證批次報價的完整性、合理性與時間順序，僅保留安全資料。

    Args:
        quotes: 待檢查的標準化報價清單。
        prev_closes: 各標的前一筆已知有效收盤價，用於偵測單日異常暴漲暴跌。
        max_daily_change_pct: 單日最大允許漲跌幅（預設 20%，台股為 10% 可依市場調整）。
        now: 檢查未來時間戳的基準時間（預設為目前 UTC 時間）。
    """
    result = ValidationResult()
    if not quotes:
        return result

    ref_now = _to_utc(now or datetime.now(timezone.utc))
    max_future_time = ref_now + timedelta(minutes=5)
    last_time_by_symbol: dict[str, datetime] = {}
    last_close_by_symbol: dict[str, float] = dict(prev_closes or {})

    for quote in quotes:
        if quote is None or not all(
            hasattr(quote, attr) for attr in ("symbol", "quote_time", "open", "high", "low", "close", "volume")
        ):
            result.issues.append(ValidationIssue(symbol="<INVALID>", quote_time="", reason="報價物件缺漏必要欄位"))
            continue

        raw_symbol = quote.symbol
        if raw_symbol is None:
            symbol = ""
        elif isinstance(raw_symbol, str):
            symbol = raw_symbol.strip()
        else:
            symbol = str(raw_symbol).strip()

        raw_time = quote.quote_time
        if raw_time is None:
            ts_str = ""
        elif isinstance(raw_time, datetime):
            ts_str = raw_time.isoformat()
        else:
            ts_str = str(raw_time)

        def reject(reason: str) -> None:
            result.issues.append(ValidationIssue(symbol=symbol or "<EMPTY>", quote_time=ts_str, reason=reason))

        # 1. 檢查必要欄位與數值型別／有限性（擋掉 None、bool、字串、浮點成交量、NaN、Inf）
        if raw_symbol is not None and not isinstance(raw_symbol, str):
            reject("股票代碼型別不正確")
            continue
        if not symbol:
            reject("股票代碼為空")
            continue

        parsed_prices = tuple(_parse_finite_float(p) for p in (quote.open, quote.high, quote.low, quote.close))
        if any(p is None for p in parsed_prices):
            reject("價格包含缺漏、非數值或非有限數值")
            continue
        open_p, high_p, low_p, close_p = (
            parsed_prices[0],
            parsed_prices[1],
            parsed_prices[2],
            parsed_prices[3],
        )
        assert open_p is not None and high_p is not None and low_p is not None and close_p is not None

        parsed_vol = _parse_non_negative_int(quote.volume)
        if parsed_vol is None:
            reject("成交量缺漏或型別異常（須為整數）")
            continue

        # 2. 檢查價格與成交量基本合理性（嚴禁 0 元或負數）
        if any(p <= 0 for p in (open_p, high_p, low_p, close_p)):
            reject("開高低收價格必須大於 0")
            continue
        if parsed_vol < 0:
            reject("成交量不可為負數")
            continue
        if high_p < max(open_p, close_p) or low_p > min(open_p, close_p) or high_p < low_p:
            reject("K 線高低價邏輯矛盾（最高價需 >= 開收盤，最低價需 <= 開收盤）")
            continue

        # 3. 檢查時間戳記是否缺漏、格式異常、超前未來或順序錯亂
        if not isinstance(raw_time, datetime):
            reject("報價時間缺漏或格式不正確")
            continue
        q_time = _to_utc(raw_time)
        if q_time > max_future_time:
            reject("報價時間超過系統當前時間，疑似未來異常資料")
            continue
        prev_time = last_time_by_symbol.get(symbol)
        if prev_time is not None and q_time <= prev_time:
            reject("時間戳記重複或倒退（早於前一筆報價）")
            continue

        # 4. 檢查單日異常極端漲跌幅
        ref_close = _parse_finite_float(last_close_by_symbol.get(symbol))
        if ref_close is not None and ref_close > 0:
            change_pct = abs(close_p - ref_close) / ref_close
            if change_pct > max_daily_change_pct:
                reject(f"單筆漲跌幅達 {change_pct:.1%}，超過安全門檻 {max_daily_change_pct:.0%}")
                continue

        # 通過所有檢查：更新基準並加入有效清單
        raw_source = getattr(quote, "source", "mock")
        safe_source = raw_source.strip() if isinstance(raw_source, str) and raw_source.strip() else "mock"
        last_time_by_symbol[symbol] = q_time
        last_close_by_symbol[symbol] = close_p
        result.valid_quotes.append(
            Quote(
                symbol=symbol,
                quote_time=q_time,
                open=open_p,
                high=high_p,
                low=low_p,
                close=close_p,
                volume=parsed_vol,
                source=safe_source,
            )
        )

    return result


def save_validated_quotes(conn: sqlite3.Connection, result: ValidationResult) -> int:
    """將通過驗證的報價寫入或更新至 market_quotes 資料表，回傳寫入／更新筆數。"""
    if not result.valid_quotes:
        return 0

    rows = [
        (
            q.symbol,
            q.source,
            _to_utc(q.quote_time).strftime("%Y-%m-%dT%H:%M:%SZ"),
            q.open,
            q.high,
            q.low,
            q.close,
            q.volume,
        )
        for q in result.valid_quotes
    ]
    with conn:
        cursor = conn.executemany(
            """
            INSERT INTO market_quotes
                (symbol, source, quote_time, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, source, quote_time) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume;
            """,
            rows,
        )
    return cursor.rowcount
