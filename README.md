# 股伴 StockMate · 面向股市小白的台股分析與模擬交易工作站

一套「高安全性、低門檻、白話直覺」的台股分析與模擬交易網站。初期可於模擬市場演練，底層採用 **Adapter Pattern** 雙軌架構，已整合 **Yahoo Finance 真實行情** 與 **Yahoo 奇摩股市全類別即時報價**，未來可無縫擴充對接 FinMind、Fugle 或 Alpaca 等券商／行情 API。

---

## 核心特色

- **零外部依賴、開箱即跑**：後端以 Python 3.12 標準函式庫（`ThreadingHTTPServer` + `SQLite WAL`）運作，無須安裝 Node.js 或複雜套件即可啟動完整網站。
- **雙軌即時行情**：預設串接真實市場（Yahoo Finance 日線與台股全類別即時報價），斷網時自動安全降級為模擬市場（`MockMarketAdapter`），支援右上角一鍵切換。
- **數據驗證閘門（Data Validation）**：自動攔截缺漏值、0 元或負數價格、高低價倒掛、未來／倒退時間戳與單日異常極端漲跌幅，確認安全才寫入資料庫。
- **小白友善白話建議**：將均線（MA5 / MA20）、乖離率、成交量比與近 N 日勝率轉換為 **0–100 健康度評分**與**四選一白話操作建議**（`重壓` / `補倉` / `拋出` / `繼續蹲`），搭配盤後白話日報。
- **防呆與防上頭風控（Anti-FOMO）**：
  - 單一標的最大投入比例限制（預設總資金 **20%**）。
  - 下單前強制跳出 **二次確認視窗**，明確顯示「最大可能虧損金額」。
  - 1 小時內下單超過 3 次或當日已實現虧損達 **-5%**，自動觸發冷靜鎖定期暫停買進（仍允許賣出停損保命）。
  - 支援停損／停利條件單自動掃描平倉。
- **台股全類別即時同步**：完整收錄 `tw.stock.yahoo.com/class` 6 大分組（上市、上櫃、興櫃、概念股、集團股、電子產業）共 305 個分類與成分股報價，支援類別篩選、模擬下單帶入與 UTF-8-SIG CSV 匯出。
- **全自動排程與即時更新**：內建台北時間（UTC+8）Cron 排程迴圈與 60 秒背景自動更新機制，前端自動輪詢資料版本號無痛刷新畫面。

---

## 快速啟動

### 1. 環境需求
- **Python 3.12+**（核心功能與網站伺服器僅需標準函式庫，無須額外 `pip install`）

### 2. 啟動網站
```powershell
python -m app.web
```
啟動後開啟瀏覽器前往：**`http://127.0.0.1:8000`**

> 若需指定其他 Port（例如 `8080`）：`python -m app.web 8080`

### 3. 獨立同步台股全類別行情（選用）
網站啟動時已內建週一至週五盤中自動同步；若想單獨手動抓取或搭配 Windows 工作排程器執行：
```powershell
python -m app.adapters.yahoo_class
```

---

## 功能分頁總覽

1. **盤勢總覽**：每日盤後白話重點、自選核心追蹤清單（支援輸入代號／股名模糊搜尋新增與移除）、20 日走勢微圖與右側個股白話診斷面板。
2. **全市場類股**：瀏覽 Yahoo 台股 305 個分類強弱表現、即時同步進度條、個股明細篩選、一鍵帶入模擬下單與匯出 CSV。
3. **模擬交易**：三層連動選單（`類股分組` → `選擇類別` → `股票代碼`）、防呆下單單據、二次確認風險視窗、持倉損益監控、停損／停利掃描與委託紀錄（Blotter）。
4. **情境與回測**：
   - **條件式情境壓力測試**：手動調整大盤漲跌幅、成交量倍數與消息面，即時繪製未來 5 日價格區間量尺。
   - **真實走勢回測驗證（Reality Check）**：以歷史滾動視窗檢驗策略訊號 5 日方向命中率，並提供參數微調建議。
5. **排程紀錄**：白話時間選擇器（週一至週五／每天、固定時間／時段輪詢）、一鍵手動重跑與完整稽核日誌。

---

## 專案結構

```text
d:\anti-scam-invest
├── requirements.txt             # 進階部署選配套件清單（FastAPI / APScheduler 等）
├── data/
│   └── stockmate.db             # SQLite 資料庫（啟動後自動建立）
├── app/
│   ├── web.py                   # ThreadingHTTPServer 網站後端與 REST API
│   ├── adapters/
│   │   ├── base.py              # Quote 標準結構與 MarketDataAdapter 介面
│   │   ├── mock.py              # 模擬市場 K 線產生器
│   │   ├── yahoo.py             # Yahoo Finance 日線轉接器
│   │   └── yahoo_class.py       # Yahoo 台股全類別與成分股爬蟲
│   ├── core/
│   │   ├── validator.py         # 報價數據驗證與入庫
│   │   ├── strategy.py          # 白話健康度評分、四選一建議、日報與情境模擬
│   │   ├── reality_check.py     # 歷史走勢回測與準確率驗證
│   │   ├── risk_guard.py        # 資金上限、二次確認與防上頭冷靜鎖定
│   │   ├── broker.py            # 模擬撮合、倉位帳務與停損／停利觸發
│   │   └── scheduler.py         # 排程設定、日誌稽核與內建 Cron 迴圈
│   ├── db/
│   │   ├── schema.sql           # SQLite 資料表結構定義
│   │   └── database.py          # 連線設定（WAL / FK）與預設種子初始化
│   └── static/
│       ├── index.html           # 網站工作站介面
│       ├── styles.css           # Warm Editorial Paper / Slate Terminal 雙主題樣式
│       └── app.js               # 前端互動、XSS 安全 DOM 渲染與自動輪詢
└── tests/
    ├── test_core.py             # 資料庫、驗證、排程與風控核心測試
    ├── test_real_market.py      # Yahoo Adapter、資料源隔離與回測測試
    ├── test_web.py              # 網站 API、自選股、類股下單與即時更新整合測試
    └── test_yahoo_class.py      # 全類別爬蟲、Cron 匹配與匯出測試
```

---

## 執行測試

```powershell
python -m unittest discover -s tests -v
```

---

## 免責聲明與限制說明

- 本系統為**模擬交易與投資教育分析工具**，所有策略評分、健康度指標與條件預測僅供參考，不構成任何真實買賣建議。
- Yahoo Finance 與 Yahoo 奇摩股市端點屬於公開資訊介面，盤中報價可能存在延遲，且受限於網站結構與連線頻率限制。
- 內建自動更新與排程迴圈僅於 `python -m app.web` 執行期間運作。
