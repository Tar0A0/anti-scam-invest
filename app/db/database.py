"""SQLite 資料庫連線與初始化工具模組。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# 預設排程種子資料：對應「盤前、盤中、盤後、策略分析、白話日報」五個日常任務
DEFAULT_SCHEDULES = (
    ("fetch_pre_market", "fetch_pre", "30 8 * * 1-5", 1, 2, "盤前市場與新聞資料抓取"),
    ("fetch_intraday", "fetch_intraday", "*/15 9-13 * * 1-5", 1, 1, "盤中定時輪詢報價"),
    ("fetch_post_market", "fetch_post", "0 14 * * 1-5", 1, 2, "盤後完整日線資料抓取"),
    ("run_strategy_analysis", "strategy", "30 14 * * 1-5", 1, 2, "每日數據驗證與白話策略評分"),
    ("generate_daily_report", "daily_report", "0 15 * * 1-5", 1, 1, "產出每日盤後小白白話日報"),
    ("sync_yahoo_classes", "sync_classes", "*/15 9-14 * * 1-5", 1, 2, "每日盤中與盤後同步 Yahoo 台股全類別與即時報價"),
)

DEFAULT_WATCHLIST_SEEDS = (
    ("0050.TW", "台灣 50 ETF"),
    ("0056.TW", "高股息 ETF"),
    ("2330.TW", "台積電"),
    ("2317.TW", "鴻海"),
    ("2454.TW", "聯發科"),
)


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """建立啟用 WAL 模式與外鍵約束的 SQLite 連線。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: str | Path) -> None:
    """建立資料表並寫入預設風控參數與排程設定（可重複安全執行，含舊版排程表平滑升級）。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    conn = get_connection(path)
    try:
        conn.executescript(schema_sql)

        # 若既有資料庫的 schedule_configs 尚未包含 sync_classes 約束，進行無痛欄位約束升級
        tbl_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'schedule_configs';"
        ).fetchone()
        if tbl_row and "sync_classes" not in (tbl_row["sql"] or ""):
            conn.execute("PRAGMA foreign_keys=OFF;")
            with conn:
                conn.execute("ALTER TABLE schedule_configs RENAME TO _schedule_configs_old;")
                conn.execute(
                    """
                    CREATE TABLE schedule_configs (
                        job_name    TEXT    PRIMARY KEY,
                        job_type    TEXT    NOT NULL CHECK (
                            job_type IN ('fetch_pre', 'fetch_intraday', 'fetch_post', 'strategy', 'daily_report', 'sync_classes')
                        ),
                        cron_expr   TEXT    NOT NULL,
                        enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                        max_retries INTEGER NOT NULL DEFAULT 2 CHECK (max_retries >= 0),
                        description TEXT    NOT NULL DEFAULT '',
                        updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                    );
                    """
                )
                conn.execute("INSERT INTO schedule_configs SELECT * FROM _schedule_configs_old;")
                conn.execute("DROP TABLE _schedule_configs_old;")
            conn.execute("PRAGMA foreign_keys=ON;")

        with conn:
            # 寫入預設防呆風控設定（單一標的上限 20%、每小時最多 3 單、當日虧損上限 5%、冷靜 60 分鐘）
            conn.execute("INSERT OR IGNORE INTO risk_settings (id) VALUES (1);")
            conn.execute("INSERT OR IGNORE INTO account_state (id) VALUES (1);")
            conn.executemany(
                """
                INSERT OR IGNORE INTO schedule_configs
                    (job_name, job_type, cron_expr, enabled, max_retries, description)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                DEFAULT_SCHEDULES,
            )
            # 僅於 watchlist 全空時寫入預設 5 檔，讓使用者刪除後重啟不會自動復活
            wl_cnt = conn.execute("SELECT COUNT(*) AS cnt FROM watchlist;").fetchone()["cnt"]
            if wl_cnt == 0:
                conn.executemany(
                    "INSERT INTO watchlist (symbol, name) VALUES (?, ?);",
                    DEFAULT_WATCHLIST_SEEDS,
                )
    finally:
        conn.close()
