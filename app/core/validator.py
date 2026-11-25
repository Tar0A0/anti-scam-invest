"""數據驗證與入庫模組：攔截缺漏值、異常價格、極端漲跌幅與錯亂時間戳，避免髒資料污染分析。"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.adapters.base import Quote


def _to_utc(dt: datetime) -> datetime:
    """將 datetime 統一正規化為 UTC 時區（無時區資訊者視為 UTC）。"""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


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
        symbol = (quote.symbol or "").strip()
        ts_str = quote.quote_time.isoformat() if isinstance(quote.quote_time, datetime) else str(quote.quote_time)

        def reject(reason: str) -> None:
            result.issues.append(ValidationIssue(symbol=symbol or "<EMPTY>", quote_time=ts_str, reason=reason))

        # 1. 檢查必要欄位與數值有限性（擋掉 None、NaN、Inf）
        if not symbol:
            reject("股票代碼為空")
            continue
        prices = (quote.open, quote.high, quote.low, quote.close)
        if any(p is None or not math.isfinite(p) for p in prices) or quote.volume is None:
            reject("價格或成交量包含缺漏或非有限數值")
            continue

        # 2. 檢查價格與成交量基本合理性（嚴禁 0 元或負數）
        if any(p <= 0 for p in prices):
            reject("開高低收價格必須大於 0")
            continue
        if quote.volume < 0:
            reject("成交量不可為負數")
            continue
        if quote.high < max(quote.open, quote.close) or quote.low > min(quote.open, quote.close) or quote.high < quote.low:
            reject("K 線高低價邏輯矛盾（最高價需 >= 開收盤，最低價需 <= 開收盤）")
            continue

        # 3. 檢查時間戳記是否超前未來或順序錯亂
        if not isinstance(quote.quote_time, datetime):
            reject("報價時間格式不正確")
            continue
        q_time = _to_utc(quote.quote_time)
        if q_time > max_future_time:
            reject("報價時間超過系統當前時間，疑似未來異常資料")
            continue
        prev_time = last_time_by_symbol.get(symbol)
        if prev_time is not None and q_time <= prev_time:
            reject("時間戳記重複或倒退（早於前一筆報價）")
            continue

        # 4. 檢查單日異常極端漲跌幅
        ref_close = last_close_by_symbol.get(symbol)
        if ref_close is not None and ref_close > 0:
            change_pct = abs(quote.close - ref_close) / ref_close
            if change_pct > max_daily_change_pct:
                reject(f"單筆漲跌幅達 {change_pct:.1%}，超過安全門檻 {max_daily_change_pct:.0%}")
                continue

        # 通過所有檢查：更新基準並加入有效清單
        last_time_by_symbol[symbol] = q_time
        last_close_by_symbol[symbol] = quote.close
        result.valid_quotes.append(
            Quote(
                symbol=symbol,
                quote_time=q_time,
                open=float(quote.open),
                high=float(quote.high),
                low=float(quote.low),
                close=float(quote.close),
                volume=int(quote.volume),
                source=(quote.source or "mock").strip(),
            )
        )

    return result


def save_validated_quotes(conn: sqlite3.Connection, result: ValidationResult) -> int:
    """僅將通過驗證的報價寫入 market_quotes 資料表，回傳實際新增筆數。"""
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
            INSERT OR IGNORE INTO market_quotes
                (symbol, source, quote_time, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            rows,
        )
    return cursor.rowcount
