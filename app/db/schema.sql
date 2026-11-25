-- =============================================================================
-- 面向股市小白的股市分析與模擬交易系統 - SQLite 資料庫 Schema
-- =============================================================================

-- 1. 市場報價表：儲存通過數據驗證的 K 線報價（支援模擬市場與真實 API 雙軌來源）
CREATE TABLE IF NOT EXISTS market_quotes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT    NOT NULL,                    -- 股票代碼（如 2330.TW, AAPL）
    source     TEXT    NOT NULL,                    -- 資料來源（mock, finmind, fugle, yfinance, alpaca）
    quote_time TEXT    NOT NULL,                    -- 報價時間（ISO-8601 UTC 字串）
    open       REAL    NOT NULL CHECK (open > 0),   -- 開盤價（嚴禁 0 或負數）
    high       REAL    NOT NULL CHECK (high > 0),   -- 最高價
    low        REAL    NOT NULL CHECK (low > 0),    -- 最低價
    close      REAL    NOT NULL CHECK (close > 0),  -- 收盤價
    volume     INTEGER NOT NULL CHECK (volume >= 0),-- 成交量（不可為負）
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (high >= low),
    UNIQUE (symbol, source, quote_time)             -- 防止重複寫入同一時點報價
);

CREATE INDEX IF NOT EXISTS idx_market_quotes_symbol_time
    ON market_quotes (symbol, quote_time DESC);

-- 2. 排程設定表：讓使用者透過介面自訂每日盤前/盤中/盤後/策略/日報執行時間
CREATE TABLE IF NOT EXISTS schedule_configs (
    job_name    TEXT    PRIMARY KEY,                -- 任務唯一識別名稱
    job_type    TEXT    NOT NULL CHECK (
        job_type IN ('fetch_pre', 'fetch_intraday', 'fetch_post', 'strategy', 'daily_report', 'sync_classes')
    ),
    cron_expr   TEXT    NOT NULL,                   -- 標準 5 欄 Cron 表達式（分 時 日 月 週）
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    max_retries INTEGER NOT NULL DEFAULT 2 CHECK (max_retries >= 0),
    description TEXT    NOT NULL DEFAULT '',        -- 任務白話說明
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- 3. 排程執行日誌表：完整記錄每次定時或手動重跑的狀態、耗時與錯誤訊息
CREATE TABLE IF NOT EXISTS job_logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name          TEXT    NOT NULL,             -- 對應 schedule_configs.job_name
    trigger_type      TEXT    NOT NULL CHECK (trigger_type IN ('scheduled', 'manual')),
    started_at        TEXT    NOT NULL,             -- 開始時間（ISO-8601 UTC）
    finished_at       TEXT    NOT NULL,             -- 結束時間（ISO-8601 UTC）
    duration_ms       INTEGER NOT NULL CHECK (duration_ms >= 0),
    status            TEXT    NOT NULL CHECK (status IN ('SUCCESS', 'FAILED', 'PARTIAL')),
    retry_count       INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    records_processed INTEGER NOT NULL DEFAULT 0 CHECK (records_processed >= 0),
    error_message     TEXT,                         -- 失敗或部分異常時的白話/技術錯誤摘要
    FOREIGN KEY (job_name) REFERENCES schedule_configs (job_name) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_job_logs_name_started
    ON job_logs (job_name, started_at DESC);

-- 4. 策略訊號表：儲存白話健康度評分與「四選一」新手操作建議
CREATE TABLE IF NOT EXISTS strategy_signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL,
    signal_date   TEXT    NOT NULL,                 -- 訊號日期（YYYY-MM-DD）
    action        TEXT    NOT NULL CHECK (action IN ('HEAVY_BUY', 'ADD', 'SELL', 'HOLD')),
    health_score  INTEGER NOT NULL CHECK (health_score BETWEEN 0 AND 100), -- 0~100 白話健康度
    ma_trend      TEXT    NOT NULL,                 -- 均線趨勢摘要（如「站上月線」）
    bias_pct      REAL    NOT NULL,                 -- 乖離率（%）
    volume_ratio  REAL    NOT NULL CHECK (volume_ratio >= 0), -- 成交量異常倍數
    win_rate_nd   REAL    NOT NULL CHECK (win_rate_nd BETWEEN 0 AND 100), -- 近 N 日勝率（%）
    plain_reason  TEXT    NOT NULL,                 -- 給新手看的白話操作理由
    model_version TEXT    NOT NULL DEFAULT 'v1',
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (symbol, signal_date, model_version)
);

-- 5. 模擬倉位表：記錄目前持股數量、平均成本與自動停損停利設定
CREATE TABLE IF NOT EXISTS positions (
    symbol            TEXT    PRIMARY KEY,
    quantity          INTEGER NOT NULL CHECK (quantity >= 0),
    avg_cost          REAL    NOT NULL CHECK (avg_cost >= 0),
    stop_loss_price   REAL    CHECK (stop_loss_price IS NULL OR stop_loss_price > 0),
    take_profit_price REAL    CHECK (take_profit_price IS NULL OR take_profit_price > 0),
    updated_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- 6. 模擬交易紀錄表：記錄每一筆下單、防呆確認狀態與最大虧損估算
CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT    NOT NULL,
    side              TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type        TEXT    NOT NULL CHECK (order_type IN ('MANUAL', 'STOP_LOSS', 'TAKE_PROFIT')),
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    price             REAL    NOT NULL CHECK (price > 0),
    max_loss_estimate REAL    NOT NULL DEFAULT 0 CHECK (max_loss_estimate >= 0), -- 下單前估算最大可能虧損
    confirmed_by_user INTEGER NOT NULL DEFAULT 0 CHECK (confirmed_by_user IN (0, 1)),
    status            TEXT    NOT NULL CHECK (status IN ('FILLED', 'REJECTED', 'LOCKED')),
    reject_reason     TEXT,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_orders_created_at
    ON orders (created_at DESC);

-- 7. 防呆風控設定表（單例模式 id = 1）：控制單一標的上限與防上頭冷靜機制
CREATE TABLE IF NOT EXISTS risk_settings (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    max_single_position_pct REAL    NOT NULL DEFAULT 0.20 CHECK (max_single_position_pct > 0 AND max_single_position_pct <= 1.0),
    max_orders_per_hour     INTEGER NOT NULL DEFAULT 3 CHECK (max_orders_per_hour >= 1),
    max_daily_loss_pct      REAL    NOT NULL DEFAULT 0.05 CHECK (max_daily_loss_pct > 0 AND max_daily_loss_pct <= 1.0),
    cooldown_minutes        INTEGER NOT NULL DEFAULT 60 CHECK (cooldown_minutes >= 1),
    updated_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- 8. 交易冷靜鎖定紀錄表：觸發防上頭條件後強制暫停買進至 unlock_at
CREATE TABLE IF NOT EXISTS risk_lockouts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    reason    TEXT    NOT NULL,
    locked_at TEXT    NOT NULL,
    unlock_at TEXT    NOT NULL
);

-- 9. 模擬帳戶資金表（單例模式 id = 1）：記錄初始資金、可用現金與當日已實現損益
CREATE TABLE IF NOT EXISTS account_state (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    initial_cash       REAL    NOT NULL DEFAULT 1000000.0 CHECK (initial_cash > 0),
    cash_balance       REAL    NOT NULL DEFAULT 1000000.0 CHECK (cash_balance >= 0),
    daily_realized_pnl REAL    NOT NULL DEFAULT 0.0,
    updated_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- 10. Yahoo 台股全類別目錄與統計摘要表（上市/上櫃/興櫃/概念股/集團股/電子產業）
CREATE TABLE IF NOT EXISTS yahoo_categories (
    category_key   TEXT    PRIMARY KEY,
    group_type     TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    sector_id      TEXT,
    exchange       TEXT,
    category_param TEXT,
    category_label TEXT,
    stock_count    INTEGER NOT NULL DEFAULT 0,
    avg_change_pct REAL,
    up_count       INTEGER NOT NULL DEFAULT 0,
    down_count     INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- 11. Yahoo 台股各類別即時成分股報價表
CREATE TABLE IF NOT EXISTS yahoo_category_quotes (
    category_key  TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    symbol_name   TEXT    NOT NULL,
    exchange      TEXT,
    price         REAL,
    change        REAL,
    change_pct    REAL,
    change_status TEXT,
    open          REAL,
    high          REAL,
    low           REAL,
    prev_close    REAL,
    volume        INTEGER NOT NULL DEFAULT 0,
    turnover_m    REAL,
    limit_up      INTEGER NOT NULL DEFAULT 0 CHECK (limit_up IN (0, 1)),
    limit_down    INTEGER NOT NULL DEFAULT 0 CHECK (limit_down IN (0, 1)),
    market_status TEXT,
    quote_time    TEXT,
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (category_key, symbol),
    FOREIGN KEY (category_key) REFERENCES yahoo_categories (category_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_yahoo_cat_quotes_key ON yahoo_category_quotes (category_key);
CREATE INDEX IF NOT EXISTS idx_yahoo_cat_quotes_sym ON yahoo_category_quotes (symbol);

-- 12. Yahoo 類別每日收盤快照表：累積每日類股強弱與成交金額變化
CREATE TABLE IF NOT EXISTS yahoo_category_daily_snapshots (
    trade_date       TEXT    NOT NULL,
    category_key     TEXT    NOT NULL,
    stock_count      INTEGER NOT NULL DEFAULT 0,
    avg_change_pct   REAL,
    up_count         INTEGER NOT NULL DEFAULT 0,
    down_count       INTEGER NOT NULL DEFAULT 0,
    total_turnover_m REAL,
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (trade_date, category_key),
    FOREIGN KEY (category_key) REFERENCES yahoo_categories (category_key) ON DELETE CASCADE
);

-- 13. 使用者自選核心追蹤清單（盤勢總覽動態新增／移除）
CREATE TABLE IF NOT EXISTS watchlist (
    symbol   TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
