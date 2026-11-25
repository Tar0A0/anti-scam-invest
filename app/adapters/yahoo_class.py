"""Yahoo 奇摩股市全類別爬蟲與每日同步模組（https://tw.stock.yahoo.com/class）。"""

from __future__ import annotations

import argparse
import html
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote as url_quote, urlencode, urlparse
from urllib.request import Request, urlopen

from app.db.database import DEFAULT_SCHEDULES, get_connection, init_db  # noqa: F401

CLASS_PAGE_URL = "https://tw.stock.yahoo.com/class"
CLASS_API_BASE = "https://tw.stock.yahoo.com/_td-stock/api/resource/StockServices.getClassQuotes"
DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StockMate/1.0", "Accept": "application/json"}
TAIPEI_TZ = timezone(timedelta(hours=8))

EXCHANGE_GROUP_MAP = {
    "TAI": "上市",
    "TWO": "上櫃",
    "OES": "興櫃",
}


@dataclass(frozen=True)
class CategoryDef:
    """單一 Yahoo 台股分類定義（涵蓋上市/上櫃/興櫃類股與概念/集團/電子產業）。"""

    category_key: str
    group_type: str
    name: str
    sector_id: str | None = None
    exchange: str | None = None
    category_param: str | None = None
    category_label: str | None = None


class _ClassLinkParser(HTMLParser):
    """從 Yahoo 台股分類總覽 HTML 抽取所有 `/class-quote?...` 連結與文字。"""

    def __init__(self) -> None:
        super().__init__()
        self._href: str | None = None
        self._buf: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for k, v in attrs:
            if k.lower() == "href" and v and "/class-quote?" in v:
                self._href = html.unescape(v)
                self._buf = []
                break

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self.links.append((self._href, text))
            self._href = None
            self._buf = []


def parse_class_page_categories(html_text: str) -> list[CategoryDef]:
    """解析 https://tw.stock.yahoo.com/class 頁面中的所有類股與題材分類。"""
    parser = _ClassLinkParser()
    parser.feed(html_text)

    categories: dict[str, CategoryDef] = {}
    for href, link_text in parser.links:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)

        if "sectorId" in qs and "exchange" in qs:
            sector_id = qs["sectorId"][0].strip()
            exchange = qs["exchange"][0].strip().upper()
            if not sector_id or exchange not in EXCHANGE_GROUP_MAP:
                continue
            key = f"sector:{exchange}:{sector_id}"
            name = link_text or f"類股 {sector_id}"
            categories.setdefault(
                key,
                CategoryDef(
                    category_key=key,
                    group_type=EXCHANGE_GROUP_MAP[exchange],
                    name=name,
                    sector_id=sector_id,
                    exchange=exchange,
                ),
            )
        elif "category" in qs and "categoryLabel" in qs:
            cat_param = qs["category"][0].strip()
            cat_label = qs["categoryLabel"][0].strip()
            if not cat_param or not cat_label:
                continue
            key = f"theme:{cat_label}:{cat_param}"
            name = link_text or cat_param
            categories.setdefault(
                key,
                CategoryDef(
                    category_key=key,
                    group_type=cat_label,
                    name=name,
                    category_param=cat_param,
                    category_label=cat_label,
                ),
            )

    return list(categories.values())


def _extract_num(val: Any) -> float | None:
    """安全解析 Yahoo 回應中可能為 dict(raw/fmt)、帶逗號字串或 '-' 的數值。"""
    if val is None:
        return None
    if isinstance(val, dict):
        val = val.get("raw") if val.get("raw") is not None else val.get("sort")
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    text = str(val).strip().replace(",", "").replace("%", "")
    if not text or text in ("-", "--", "N/A"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_class_quote_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """將單筆 `StockServices.getClassQuotes` 項目轉為標準化資料列。"""
    symbol = str(item.get("symbol") or "").strip()
    symbol_name = str(item.get("symbolName") or symbol).strip()
    if not symbol:
        return None

    vol_num = _extract_num(item.get("volume"))
    return {
        "symbol": symbol,
        "symbol_name": symbol_name,
        "exchange": str(item.get("exchange") or "").strip(),
        "price": _extract_num(item.get("price")),
        "change": _extract_num(item.get("change")),
        "change_pct": _extract_num(item.get("changePercent")),
        "change_status": str(item.get("changeStatus") or "").strip(),
        "open": _extract_num(item.get("regularMarketOpen")),
        "high": _extract_num(item.get("regularMarketDayHigh")),
        "low": _extract_num(item.get("regularMarketDayLow")),
        "prev_close": _extract_num(item.get("regularMarketPreviousClose")),
        "volume": max(0, int(vol_num)) if vol_num is not None else 0,
        "turnover_m": _extract_num(item.get("turnoverM")),
        "limit_up": 1 if item.get("limitUp") else 0,
        "limit_down": 1 if item.get("limitDown") else 0,
        "market_status": str(item.get("marketStatus") or "").strip(),
        "quote_time": str(item.get("regularMarketTime") or "").strip() or None,
    }


def _build_quotes_url(cat: CategoryDef, offset: int) -> str:
    qs = urlencode(
        {
            "intl": "tw",
            "lang": "zh-Hant-TW",
            "region": "TW",
            "site": "finance",
            "tz": "Asia/Taipei",
            "returnMeta": "true",
        }
    )
    if cat.sector_id and cat.exchange:
        return f"{CLASS_API_BASE};exchange={cat.exchange};offset={offset};sectorId={cat.sector_id}?{qs}"
    cat_enc = url_quote(cat.category_param or "", safe="")
    lbl_enc = url_quote(cat.category_label or "", safe="")
    return f"{CLASS_API_BASE};category={cat_enc};categoryLabel={lbl_enc};categoryName={cat_enc};offset={offset}?{qs}"


class YahooClassScraper:
    """抓取 Yahoo 台股全類別目錄與成分股即時報價並寫入 SQLite。"""

    # shortcut: 使用 Yahoo 台股公開網頁與 _td-stock API；若遭限流或網頁結構改版需調整解析規則
    def __init__(self, *, timeout_sec: float = 12.0, opener: Callable[..., Any] | None = None) -> None:
        self.timeout_sec = timeout_sec
        self._opener = opener or urlopen

    def fetch_categories(self) -> list[CategoryDef]:
        req = Request(CLASS_PAGE_URL, headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]})
        with self._opener(req, timeout=self.timeout_sec) as resp:
            html_text = resp.read().decode("utf-8", "ignore")
        cats = parse_class_page_categories(html_text)
        if not cats:
            raise RuntimeError("無法從 Yahoo 台股分類頁解析出任何類別")
        return cats

    def fetch_category_quotes(self, cat: CategoryDef, *, max_pages: int = 30) -> list[dict[str, Any]]:
        quotes_by_symbol: dict[str, dict[str, Any]] = {}
        offset = 0
        seen_offsets: set[int] = set()

        for _ in range(max_pages):
            if offset in seen_offsets:
                break
            seen_offsets.add(offset)

            url = _build_quotes_url(cat, offset)
            req = Request(url, headers=DEFAULT_HEADERS)
            with self._opener(req, timeout=self.timeout_sec) as resp:
                payload = json.loads(resp.read().decode("utf-8"))

            data = payload.get("data") or {}
            for item in data.get("list") or []:
                parsed = parse_class_quote_item(item)
                if parsed is not None:
                    quotes_by_symbol[parsed["symbol"]] = parsed

            next_offset = (data.get("pagination") or {}).get("nextOffset")
            if next_offset in (None, ""):
                break
            offset = int(next_offset)

        return list(quotes_by_symbol.values())

    def sync_all_categories(
        self,
        db_path: str | Path,
        *,
        group_filter: str | None = None,
        max_workers: int = 6,
        progress_cb: Callable[[int, int, int], None] | None = None,
    ) -> dict[str, Any]:
        """抓取全部分類與所有成分股報價並更新資料庫與當日快照。"""
        init_db(db_path)
        categories = self.fetch_categories()
        if group_filter:
            categories = [c for c in categories if c.group_type == group_filter]

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        trade_date = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")

        # 先註冊類別中繼資料，確保外鍵與列表存在
        conn = get_connection(db_path)
        try:
            with conn:
                conn.executemany(
                    """
                    INSERT INTO yahoo_categories
                        (category_key, group_type, name, sector_id, exchange, category_param, category_label, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(category_key) DO UPDATE SET
                        group_type = excluded.group_type,
                        name = excluded.name,
                        sector_id = excluded.sector_id,
                        exchange = excluded.exchange,
                        category_param = excluded.category_param,
                        category_label = excluded.category_label;
                    """,
                    [
                        (c.category_key, c.group_type, c.name, c.sector_id, c.exchange, c.category_param, c.category_label, now_utc)
                        for c in categories
                    ],
                )
        finally:
            conn.close()

        total = len(categories)
        synced = 0
        failed = 0
        total_rows = 0
        unique_symbols: set[str] = set()
        errors: list[str] = []
        db_lock = threading.Lock()

        def worker(cat: CategoryDef) -> tuple[CategoryDef, list[dict[str, Any]]]:
            return cat, self.fetch_category_quotes(cat)

        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futures = {pool.submit(worker, cat): cat for cat in categories}
            for fut in as_completed(futures):
                cat = futures[fut]
                try:
                    _, quotes = fut.result()
                    changes = [q["change_pct"] for q in quotes if q["change_pct"] is not None]
                    avg_chg = round(sum(changes) / len(changes), 2) if changes else None
                    up_cnt = sum(1 for q in quotes if (q["change_pct"] or 0) > 0)
                    down_cnt = sum(1 for q in quotes if (q["change_pct"] or 0) < 0)
                    turnovers = [q["turnover_m"] for q in quotes if q["turnover_m"] is not None]
                    total_turnover = round(sum(turnovers), 2) if turnovers else None
                    cat_updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                    with db_lock:
                        c_conn = get_connection(db_path)
                        try:
                            with c_conn:
                                c_conn.execute(
                                    "DELETE FROM yahoo_category_quotes WHERE category_key = ?;",
                                    (cat.category_key,),
                                )
                                c_conn.executemany(
                                    """
                                    INSERT INTO yahoo_category_quotes
                                        (category_key, symbol, symbol_name, exchange, price, change, change_pct,
                                         change_status, open, high, low, prev_close, volume, turnover_m,
                                         limit_up, limit_down, market_status, quote_time, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                                    """,
                                    [
                                        (
                                            cat.category_key, q["symbol"], q["symbol_name"], q["exchange"],
                                            q["price"], q["change"], q["change_pct"], q["change_status"],
                                            q["open"], q["high"], q["low"], q["prev_close"], q["volume"],
                                            q["turnover_m"], q["limit_up"], q["limit_down"], q["market_status"],
                                            q["quote_time"], cat_updated,
                                        )
                                        for q in quotes
                                    ],
                                )
                                c_conn.execute(
                                    """
                                    UPDATE yahoo_categories
                                    SET stock_count = ?, avg_change_pct = ?, up_count = ?, down_count = ?, updated_at = ?
                                    WHERE category_key = ?;
                                    """,
                                    (len(quotes), avg_chg, up_cnt, down_cnt, cat_updated, cat.category_key),
                                )
                                c_conn.execute(
                                    """
                                    INSERT INTO yahoo_category_daily_snapshots
                                        (trade_date, category_key, stock_count, avg_change_pct, up_count, down_count, total_turnover_m, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    ON CONFLICT(trade_date, category_key) DO UPDATE SET
                                        stock_count = excluded.stock_count,
                                        avg_change_pct = excluded.avg_change_pct,
                                        up_count = excluded.up_count,
                                        down_count = excluded.down_count,
                                        total_turnover_m = excluded.total_turnover_m,
                                        updated_at = excluded.updated_at;
                                    """,
                                    (trade_date, cat.category_key, len(quotes), avg_chg, up_cnt, down_cnt, total_turnover, cat_updated),
                                )
                        finally:
                            c_conn.close()

                    synced += 1
                    total_rows += len(quotes)
                    unique_symbols.update(q["symbol"] for q in quotes)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    if len(errors) < 10:
                        errors.append(f"{cat.name}({cat.category_key}): {type(exc).__name__}: {exc}")

                if progress_cb:
                    progress_cb(synced + failed, total, total_rows)

        return {
            "total_categories": total,
            "synced_categories": synced,
            "failed_categories": failed,
            "total_quote_rows": total_rows,
            "unique_symbols": len(unique_symbols),
            "errors": errors,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="同步 Yahoo 奇摩台股全類別與即時報價")
    parser.add_argument("--db", default=str(Path(__file__).resolve().parents[2] / "data" / "stockmate.db"))
    parser.add_argument("--group", default=None, help="只同步指定分組（如 上市、上櫃、興櫃、概念股、集團股、電子產業）")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    summary = YahooClassScraper().sync_all_categories(args.db, group_filter=args.group, max_workers=args.workers)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
