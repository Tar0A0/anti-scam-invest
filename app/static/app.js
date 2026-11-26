"use strict";

const state = {
  stocks: [],
  portfolio: null,
  selectedSymbol: null,
  pendingOrder: null,
  classGroup: "",
  selectedCategoryKey: "",
  allClasses: [],
  orderClassQuotes: [],
  syncTimer: null,
  lastDataVersion: null,
  searchTimer: null,
};

const fmtMoney = (n) => Number(n).toLocaleString("zh-TW", { maximumFractionDigits: 2 });
const isFiniteNum = (v) => v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));
const GUIDE_STORAGE_KEY = "stockmate-guide-dismissed";

const GLOSSARY_TERMS = [
  {
    key: "ma20",
    title: "月線 (MA20)",
    code: "MA20",
    metaphor: "生活比喻：過去一個月買進這檔股票的「大家平均成本線」。",
    desc: "把最近 20 個交易日的收盤價加起來平均。股價站在月線上方，代表近一個月買的人多數賺錢、籌碼相對安定；跌破月線則代表近期進場者開始虧損，容易遇到解套賣壓。",
    tip: "新手口訣：先看股價在月線之上還是之下，順著大方向比較省力。",
  },
  {
    key: "bias",
    title: "乖離率 (Bias)",
    code: "BIAS %",
    metaphor: "生活比喻：股價與月線之間的「橡皮筋拉扯程度」。",
    desc: "衡量目前股價離月線有多遠。正乖離太大（例如超過 +8%），代表短線漲太急、像橡皮筋拉太緊隨時可能回彈修正；負乖離過大則代表短線急跌。",
    tip: "新手口訣：看到正乖離偏高先忍住不追高，等股價拉回靠近月線更安全。",
  },
  {
    key: "volume_ratio",
    title: "成交量倍數",
    code: "VOL RATIO",
    metaphor: "生活比喻：店門口今天的「排隊人潮和平常比多了幾倍」。",
    desc: "今日成交量除以近 5 日平均量。1.0x 代表平常熱度；1.5x 以上代表市場突然聚焦。若「價漲量增」代表買氣有跟上；若「爆量長黑下跌」則要提防大戶急著撤退。",
    tip: "新手口訣：價漲搭配溫和放量較健康，無量上漲或爆量下殺都要提高警覺。",
  },
  {
    key: "rsi",
    title: "RSI 體溫計 (14 日)",
    code: "RSI14",
    metaphor: "生活比喻：測量市場買賣情緒的「體溫計（0～100 度）」。",
    desc: "比較近 14 天漲幅與跌幅的力道。70 以上像發燒「過熱」，短線追高容易燙傷；45～70 屬於健康或偏強區間；30 以下像體溫過低「超賣」，代表賣壓宣洩過度、可能醞釀反彈，但仍要等止跌訊號。",
    tip: "新手口訣：過熱(>70)不急著追，超賣(<30)不急著一把接刀，分批最安心。",
  },
  {
    key: "atr",
    title: "平均波動幅度 (ATR 14 日)",
    code: "ATR14 %",
    metaphor: "生活比喻：這檔股票平常一天會「搖晃多大」的避震器讀數。",
    desc: "統計近 14 個交易日平均每日價格震盪百分比。數值低（1%～2%）像搭公車般平穩；數值高（3.5% 以上）像搭雲霄飛車，單日上下劇烈，部位應該縮小、停損空間也要留得更合理。",
    tip: "新手口訣：波動越大的股票，投入金額越要縮小，才不會被正常震盪嚇出場。",
  },
  {
    key: "support_resistance",
    title: "支撐價 / 壓力價",
    code: "SUPPORT / RESISTANCE",
    metaphor: "生活比喻：股價近期的「地板（防守價）」與「天花板（壓力價）」。",
    desc: "支撐價是近 20 日跌到附近就常有買盤承接的參考地板；壓力價是漲到附近常有人想先獲利了結的參考天花板。離地板近時防守容易算，逼近天花板時則要觀察能不能放量突破。",
    tip: "新手口訣：參考防守價可用來思考停損點，買在接近天花板位置則報酬空間較窄。",
  },
  {
    key: "stop_loss_profit",
    title: "停損價 / 停利價",
    code: "STOP LOSS / TAKE PROFIT",
    metaphor: "生活比喻：投資的「安全氣囊」與「到站下車鬧鐘」。",
    desc: "停損價是下單前就先決定好「看錯最多願意賠多少就自動賣出」，避免小虧滾成大套牢；停利價是漲到目標就先落袋為安，防止帳面獲利坐過山車又吐回去。",
    tip: "新手口訣：進場前先設好停損價（常見買價 -5% 或參考防守價下方），再決定買幾股。",
  },
  {
    key: "max_drawdown",
    title: "歷史最大回撤與獲利因子",
    code: "MDD / PROFIT FACTOR",
    metaphor: "生活比喻：「從山頂摔到谷底最痛的一次」與「賺的錢是賠的錢的幾倍」。",
    desc: "歷史最大連續回檔（最大回撤）顯示回測期間訊號最慘曾連續回吐多少百分比，用來預先評估心理承受度；獲利因子＝總獲利÷總虧損，大於 1 代表長期賺多賠少，數值越高策略越穩健。",
    tip: "新手口訣：不能只看命中率，同時看最大回檔與賺賠比，才知道策略扛不扛得住逆風。",
  },
  {
    key: "risk_guard",
    title: "單標上限 20% 與冷靜鎖",
    code: "RISK GUARD",
    metaphor: "生活比喻：「雞蛋不放同一個籃子」加上「衝動暫停鍵」。",
    desc: "本系統規定單一股票最多只佔模擬總資產 20%，避免重壓一檔踩雷傷筋動骨；當日已實現虧損達 -5% 或短時間內連續頻繁下單時，系統會自動鎖住買進功能，強迫先冷靜再做決定。",
    tip: "新手口訣：風控不是為了限制獲利，而是確保你在最衝動的時候不會把本金輸光。",
  },
];

function helpTipBtn(termKey, ariaLabel) {
  return el("button", {
    type: "button",
    class: "help-tip-btn",
    "data-glossary": termKey,
    "aria-label": ariaLabel || "查看名詞白話解釋",
    text: "?",
  });
}

function syncGuideRestoreButton() {
  const restoreBtn = document.getElementById("btn-restore-guide");
  if (!restoreBtn) return;
  const dismissed = localStorage.getItem(GUIDE_STORAGE_KEY) === "1";
  restoreBtn.textContent = dismissed ? "重新顯示 3 步驟導引" : "3 步驟導引已顯示";
  restoreBtn.disabled = !dismissed;
}

function applyGuideVisibility() {
  const guide = document.getElementById("beginner-guide");
  if (!guide) return;
  const dismissed = localStorage.getItem(GUIDE_STORAGE_KEY) === "1";
  guide.classList.toggle("hidden", dismissed);
  syncGuideRestoreButton();
}

function dismissGuide() {
  localStorage.setItem(GUIDE_STORAGE_KEY, "1");
  applyGuideVisibility();
}

function restoreGuide() {
  localStorage.removeItem(GUIDE_STORAGE_KEY);
  applyGuideVisibility();
  switchTab("tab-market");
  closeGlossary();
  const guide = document.getElementById("beginner-guide");
  if (guide) guide.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderGlossary(highlightKey = null) {
  const body = document.getElementById("glossary-body");
  if (!body) return;
  body.replaceChildren(
    ...GLOSSARY_TERMS.map((item) =>
      el(
        "article",
        {
          class: `glossary-item${highlightKey === item.key ? " highlighted" : ""}`,
          "data-term-key": item.key,
        },
        [
          el("div", { class: "glossary-item-head" }, [
            el("h4", { text: item.title }),
            el("span", { class: "glossary-term-code", text: item.code }),
          ]),
          el("p", { class: "glossary-metaphor", text: item.metaphor }),
          el("p", { class: "glossary-desc", text: item.desc }),
          el("p", { class: "glossary-tip", text: item.tip }),
        ],
      ),
    ),
  );
}

function openGlossary(termKey = null) {
  const modal = document.getElementById("glossary-modal");
  if (!modal) return;
  renderGlossary(termKey);
  syncGuideRestoreButton();
  modal.classList.remove("hidden");
  if (termKey) {
    const target = modal.querySelector(`[data-term-key="${termKey}"]`);
    if (target) {
      requestAnimationFrame(() => target.scrollIntoView({ behavior: "smooth", block: "center" }));
    }
  }
  const closeBtn = document.getElementById("glossary-close");
  if (closeBtn) closeBtn.focus();
}

function closeGlossary() {
  const modal = document.getElementById("glossary-modal");
  if (modal) modal.classList.add("hidden");
}

function rsiTagClass(rsiLabel, rsi14) {
  const label = String(rsiLabel || "");
  const val = isFiniteNum(rsi14) ? Number(rsi14) : null;
  if (label.includes("過熱") || (val !== null && val > 70)) return "tag-HEAVY_BUY";
  if (label.includes("偏強") || (val !== null && val >= 55)) return "tag-ADD";
  if (label.includes("超賣") || (val !== null && val < 30)) return "tag-SELL";
  if (label.includes("偏弱") || (val !== null && val < 45)) return "tag-HOLD";
  return "tag-neutral";
}

function atrPlainDesc(atrPct) {
  const val = Number(atrPct);
  if (val >= 3.5) return "波動偏大・部位宜縮小";
  if (val >= 2.0) return "波動適中";
  return "相對平穩";
}

/** 安全建立 DOM 節點，全部動態文字皆透過 textContent 寫入以避免 XSS。 */
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children) {
    if (typeof child === "string") node.appendChild(document.createTextNode(child));
    else if (child) node.appendChild(child);
  }
  return node;
}

async function api(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json();
  return { ok: res.ok, status: res.status, data };
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("stockmate-theme", theme);
  const label = document.getElementById("theme-label");
  if (label) label.textContent = theme === "dark" ? "淺色紙面模式" : "深色終端模式";
}

function buildSparkline(values, isUp) {
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 120 32");
  svg.setAttribute("class", "sparkline");
  svg.setAttribute("aria-hidden", "true");
  if (!values || values.length < 2) return svg;

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const coords = values.map((v, i) => {
    const x = (i / (values.length - 1)) * 116 + 2;
    const y = 28 - ((v - min) / span) * 24;
    return [Number(x.toFixed(1)), Number(y.toFixed(1))];
  });
  const pts = coords.map(([x, y]) => `${x},${y}`).join(" ");
  const color = isUp ? "var(--up)" : "var(--down)";

  const area = document.createElementNS(svgNS, "polygon");
  area.setAttribute("points", `2,30 ${pts} 118,30`);
  area.setAttribute("fill", color);
  area.setAttribute("fill-opacity", "0.10");

  const poly = document.createElementNS(svgNS, "polyline");
  poly.setAttribute("fill", "none");
  poly.setAttribute("stroke", color);
  poly.setAttribute("stroke-width", "1.6");
  poly.setAttribute("points", pts);

  svg.append(area, poly);
  return svg;
}

function renderInspector(stock) {
  const panel = document.getElementById("stock-inspector");
  if (!stock) {
    panel.replaceChildren(el("p", { class: "muted-text", text: "請選擇左側標的檢視詳細診斷。" }));
    return;
  }

  const chgCls = stock.change_pct >= 0 ? "pos" : "neg";
  const closePrice = isFiniteNum(stock.latest_close) ? Number(stock.latest_close) : null;
  const hasRsi = isFiniteNum(stock.rsi14);
  const hasAtr = isFiniteNum(stock.atr14_pct);
  const hasSupport = isFiniteNum(stock.support_price);
  const hasResistance = isFiniteNum(stock.resistance_price);
  const hasSuggestedSl = isFiniteNum(stock.suggested_stop_loss);
  const hasSuggestedTp = isFiniteNum(stock.suggested_take_profit);
  const hasLevels = hasSupport || hasResistance || hasSuggestedSl || hasSuggestedTp;

  const pctFromClose = (target) => {
    if (!closePrice || closePrice <= 0 || !isFiniteNum(target)) return "";
    const diff = ((Number(target) - closePrice) / closePrice) * 100;
    return `距現價 ${diff >= 0 ? "+" : ""}${diff.toFixed(1)}%`;
  };

  const rsiValueChildren = hasRsi
    ? [
        el("strong", { class: "mono", text: `${Number(stock.rsi14).toFixed(2)}` }),
        stock.rsi_label
          ? el("span", {
              class: `tag metric-inline-tag ${rsiTagClass(stock.rsi_label, stock.rsi14)}`,
              text: stock.rsi_label,
            })
          : null,
      ]
    : [el("strong", { class: "mono", text: "--" })];

  const atrBoxChildren = [
    el("div", { class: "metric-label-row" }, [
      el("span", { text: "近 14 日平均波動" }),
      helpTipBtn("atr", "了解近 14 日平均波動幅度 ATR"),
    ]),
    el("strong", {
      class: "mono",
      text: hasAtr ? `${Number(stock.atr14_pct).toFixed(2)}%` : "--",
    }),
    hasAtr ? el("span", { class: "metric-sub", text: atrPlainDesc(stock.atr14_pct) }) : null,
  ];

  const levelsCard = hasLevels
    ? el("div", { class: "levels-card" }, [
        el("div", { class: "levels-head" }, [
          el("span", { text: "參考價位與建議停損／停利" }),
          helpTipBtn("support_resistance", "了解支撐價與壓力價"),
        ]),
        el("div", { class: "levels-grid" }, [
          el("div", { class: "level-item" }, [
            el("span", { class: "level-label", text: "參考防守價（支撐）" }),
            el("strong", {
              class: "level-val mono",
              text: hasSupport ? `${fmtMoney(stock.support_price)} 元` : "--",
            }),
            hasSupport ? el("span", { class: "level-meta mono", text: pctFromClose(stock.support_price) }) : null,
          ]),
          el("div", { class: "level-item" }, [
            el("span", { class: "level-label", text: "參考壓力價" }),
            el("strong", {
              class: "level-val mono",
              text: hasResistance ? `${fmtMoney(stock.resistance_price)} 元` : "--",
            }),
            hasResistance ? el("span", { class: "level-meta mono", text: pctFromClose(stock.resistance_price) }) : null,
          ]),
          el("div", { class: "level-item" }, [
            el("span", { class: "level-label" }, [
              el("span", { text: "建議停損價" }),
              helpTipBtn("stop_loss_profit", "了解停損價與停利價"),
            ]),
            el("strong", {
              class: "level-val mono neg",
              text: hasSuggestedSl ? `${fmtMoney(stock.suggested_stop_loss)} 元` : "--",
            }),
            hasSuggestedSl ? el("span", { class: "level-meta mono", text: pctFromClose(stock.suggested_stop_loss) }) : null,
          ]),
          el("div", { class: "level-item" }, [
            el("span", { class: "level-label", text: "建議停利價" }),
            el("strong", {
              class: "level-val mono pos",
              text: hasSuggestedTp ? `${fmtMoney(stock.suggested_take_profit)} 元` : "--",
            }),
            hasSuggestedTp ? el("span", { class: "level-meta mono", text: pctFromClose(stock.suggested_take_profit) }) : null,
          ]),
        ]),
      ])
    : null;

  const orderBtnLabel = hasSuggestedSl || hasSuggestedTp
    ? "依建議停損／停利前往模擬下單"
    : "前往模擬下單";

  panel.replaceChildren(
    el("div", { class: "inspector-header" }, [
      el("div", {}, [
        el("span", { class: "eyebrow", text: `${stock.symbol} · ${stock.ma_trend}` }),
        el("h2", { style: "font-size: 1.2rem; margin-top: 2px;", text: stock.name }),
        el("div", { style: "margin-top: 6px;" }, [
          el("span", { class: `tag tag-${stock.action}`, text: `建議：${stock.action_label}` }),
        ]),
      ]),
      el("div", { class: `inspector-price mono ${chgCls}` }, [
        el("div", { text: `${fmtMoney(stock.latest_close)}` }),
        el("div", {
          style: "font-size: 0.84rem; margin-top: 2px;",
          text: `${stock.change_pct >= 0 ? "+" : ""}${stock.change_pct}%`,
        }),
      ]),
    ]),
    el("div", { class: "metric-grid" }, [
      el("div", { class: "metric-box" }, [
        el("div", { class: "metric-label-row" }, [el("span", { text: "健康度評分" })]),
        el("strong", { class: "mono", text: `${stock.health_score} / 100` }),
      ]),
      el("div", { class: "metric-box" }, [
        el("div", { class: "metric-label-row" }, [
          el("span", { text: "月線 MA20 / 乖離" }),
          helpTipBtn("ma20", "了解月線 MA20 與乖離率"),
        ]),
        el("strong", { class: "mono", text: `${fmtMoney(stock.ma20)} (${stock.bias_pct >= 0 ? "+" : ""}${stock.bias_pct}%)` }),
      ]),
      el("div", { class: "metric-box" }, [
        el("div", { class: "metric-label-row" }, [
          el("span", { text: "成交量能倍數" }),
          helpTipBtn("volume_ratio", "了解成交量倍數"),
        ]),
        el("strong", { class: "mono", text: `${stock.volume_ratio}x` }),
      ]),
      el("div", { class: "metric-box" }, [
        el("div", { class: "metric-label-row" }, [el("span", { text: "近 10 日上漲勝率" })]),
        el("strong", { class: "mono", text: `${stock.win_rate_nd}%` }),
      ]),
      el("div", { class: "metric-box" }, [
        el("div", { class: "metric-label-row" }, [
          el("span", { text: "RSI 體溫狀態" }),
          helpTipBtn("rsi", "了解 RSI 體溫計"),
        ]),
        el("div", {}, rsiValueChildren),
      ]),
      el("div", { class: "metric-box" }, atrBoxChildren),
    ]),
    levelsCard,
    el("p", { class: "rationale-box", text: stock.plain_reason }),
    el("div", { class: "btn-group inspector-actions" }, [
      el("button", {
        type: "button",
        class: "btn btn-primary",
        text: orderBtnLabel,
        onclick: () => prefillOrder(stock),
      }),
      el("button", {
        type: "button",
        class: "btn btn-ghost",
        text: "情境壓力測試",
        onclick: () => {
          document.getElementById("forecast-symbol").value = stock.symbol;
          switchTab("tab-forecast");
        },
      }),
      el("button", {
        type: "button",
        class: "btn btn-ghost",
        text: "移除追蹤",
        onclick: () => removeWatchlistSymbol(stock.symbol),
      }),
    ]),
  );
}

function renderOverview(data) {
  const { portfolio, daily_report: report, stocks } = data;
  state.stocks = stocks;
  state.portfolio = portfolio || null;

  // 資料源指示與降級提示
  const sourceSel = document.getElementById("source-select");
  if (data.active_source) sourceSel.value = data.active_source;
  const dot = document.getElementById("source-dot");
  dot.classList.toggle("mock", data.active_source === "mock");

  document.getElementById("latest-quote-pill").textContent = data.latest_quote_time
    ? `報價 ${data.latest_quote_time}`
    : report.report_date;

  const noticeBanner = document.getElementById("source-notice-banner");
  if (data.source_notice) {
    noticeBanner.textContent = data.source_notice;
    noticeBanner.classList.remove("hidden");
  } else {
    noticeBanner.classList.add("hidden");
  }

  const banner = document.getElementById("lockout-banner");
  if (portfolio.active_lockout) {
    banner.textContent = `冷靜保護中（暫停買進至 ${portfolio.active_lockout.unlock_at}）：${portfolio.active_lockout.reason}`;
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }

  // 帳戶摘要條
  document.getElementById("kpi-equity").textContent = `${fmtMoney(portfolio.total_equity)} 元`;
  document.getElementById("kpi-return").textContent = `累計報酬 ${portfolio.total_return_pct >= 0 ? "+" : ""}${portfolio.total_return_pct}%`;
  document.getElementById("kpi-cash").textContent = `${fmtMoney(portfolio.cash_balance)} 元`;
  const pnlEl = document.getElementById("kpi-pnl");
  pnlEl.textContent = `${portfolio.daily_realized_pnl >= 0 ? "+" : ""}${fmtMoney(portfolio.daily_realized_pnl)} 元`;
  pnlEl.className = `strip-value mono ${portfolio.daily_realized_pnl >= 0 ? "pos" : "neg"}`;
  document.getElementById("kpi-health").textContent = `${report.avg_health_score} / 100`;
  document.getElementById("kpi-health-note").textContent = data.source_label || report.report_date;

  // 報社盤後摘要
  document.getElementById("report-headline").textContent = report.headline;
  document.getElementById("report-date").textContent = report.report_date;
  document.getElementById("report-summary").textContent = report.summary;
  document.getElementById("report-counts").replaceChildren(
    el("span", { class: "tag tag-HEAVY_BUY", text: `重壓 ${report.counts.HEAVY_BUY || 0}` }),
    el("span", { class: "tag tag-ADD", text: `補倉 ${report.counts.ADD || 0}` }),
    el("span", { class: "tag tag-HOLD", text: `繼續蹲 ${report.counts.HOLD || 0}` }),
    el("span", { class: "tag tag-SELL", text: `拋出 ${report.counts.SELL || 0}` }),
  );

  // 確保選中有效標的
  if (!state.selectedSymbol || !stocks.some((s) => s.symbol === state.selectedSymbol)) {
    state.selectedSymbol = stocks.length ? stocks[0].symbol : null;
  }

  const tbody = document.getElementById("watchlist-body");
  tbody.replaceChildren(
    ...stocks.map((s) => {
      const chgCls = s.change_pct >= 0 ? "pos" : "neg";
      const fill = el("div", { class: "health-fill" });
      fill.style.width = `${s.health_score}%`;

      return el("tr", {
        class: `watch-row ${s.symbol === state.selectedSymbol ? "selected" : ""}`,
        tabindex: "0",
        onclick: () => {
          state.selectedSymbol = s.symbol;
          renderOverview(data);
        },
        onkeydown: (ev) => {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            state.selectedSymbol = s.symbol;
            renderOverview(data);
          }
        },
      }, [
        el("td", {}, [
          el("strong", { text: s.name }),
          el("div", { class: "mono muted-text", style: "font-size: 0.74rem;", text: s.symbol }),
        ]),
        el("td", {}, [buildSparkline(s.sparkline, s.change_pct >= 0)]),
        el("td", { class: `num ${chgCls}` }, [
          el("div", { style: "font-weight: 600;", text: fmtMoney(s.latest_close) }),
          el("div", { style: "font-size: 0.76rem;", text: `${s.change_pct >= 0 ? "+" : ""}${s.change_pct}%` }),
        ]),
        el("td", { class: "num" }, [
          el("div", { class: "health-mini" }, [
            el("div", { class: "health-track" }, [fill]),
            el("span", { text: `${s.health_score}` }),
          ]),
        ]),
        el("td", {}, [el("span", { class: `tag tag-${s.action}`, text: s.action_label })]),
        el("td", {}, [
          el("button", {
            type: "button",
            class: "btn btn-ghost btn-sm",
            text: "移除",
            onclick: (ev) => {
              ev.stopPropagation();
              removeWatchlistSymbol(s.symbol);
            },
          }),
        ]),
      ]);
    }),
  );

  renderInspector(stocks.find((s) => s.symbol === state.selectedSymbol) || null);
  syncSymbolSelects(stocks);
  renderPortfolioTables(portfolio);
  updateOrderRiskPreview();
}

function syncSymbolSelects(stocks) {
  const forecastSel = document.getElementById("forecast-symbol");
  const curForecast = forecastSel.value;
  forecastSel.replaceChildren(
    ...stocks.map((s) => el("option", { value: s.symbol, text: `${s.name} (${s.symbol}) · ${s.latest_close} 元` })),
  );
  if (curForecast && stocks.some((s) => s.symbol === curForecast)) {
    forecastSel.value = curForecast;
  }

  // 若目前模擬交易下拉為核心自選清單，同步更新選項
  const catSel = document.getElementById("order-category");
  if (!catSel.options.length) {
    populateOrderCategories();
  } else if (catSel.value === "__watchlist__") {
    renderOrderSymbolOptionsFromWatchlist();
  }
}

function renderOrderSymbolOptionsFromWatchlist(preferredSymbol = null) {
  const orderSel = document.getElementById("order-symbol");
  const curOrder = preferredSymbol || orderSel.value;
  orderSel.replaceChildren(
    ...state.stocks.map((s) => el("option", { value: s.symbol, text: `${s.symbol} · ${s.name} (${s.latest_close} 元)` })),
  );
  if (curOrder && state.stocks.some((s) => s.symbol === curOrder)) {
    orderSel.value = curOrder;
  }
  if (!document.getElementById("order-price").value || preferredSymbol) {
    prefillPriceForSelected();
  }
}

function populateOrderCategories(preferredCategoryKey = null) {
  const groupVal = document.getElementById("order-group").value;
  const catSel = document.getElementById("order-category");
  const prevCat = preferredCategoryKey || catSel.value;
  catSel.replaceChildren();

  if (groupVal === "" || groupVal === "__watchlist__") {
    catSel.appendChild(el("option", { value: "__watchlist__", text: `核心自選清單 (${state.stocks.length} 檔)` }));
  }

  if (groupVal !== "__watchlist__") {
    const filtered = groupVal
      ? state.allClasses.filter((c) => c.group_type === groupVal)
      : state.allClasses;

    const byGroup = new Map();
    for (const c of filtered) {
      if (!byGroup.has(c.group_type)) byGroup.set(c.group_type, []);
      byGroup.get(c.group_type).push(c);
    }

    for (const [grpName, items] of byGroup.entries()) {
      const og = el("optgroup", { label: grpName });
      for (const c of items) {
        og.appendChild(el("option", { value: c.category_key, text: `${c.name} (${c.stock_count} 檔)` }));
      }
      catSel.appendChild(og);
    }
  }

  if (prevCat && [...catSel.options].some((o) => o.value === prevCat)) {
    catSel.value = prevCat;
  }
  loadOrderSymbolsForCategory();
}

async function loadOrderSymbolsForCategory(preferredSymbol = null) {
  const catKey = document.getElementById("order-category").value;
  if (!catKey || catKey === "__watchlist__") {
    state.orderClassQuotes = [];
    renderOrderSymbolOptionsFromWatchlist(preferredSymbol);
    return;
  }

  const res = await api(`/api/classes/${encodeURIComponent(catKey)}/quotes`);
  if (!res.ok) return;
  const quotes = (res.data.quotes || []).filter((q) => q.price !== null && q.price > 0);
  state.orderClassQuotes = quotes;

  const orderSel = document.getElementById("order-symbol");
  const prevSym = preferredSymbol || orderSel.value;
  if (!quotes.length) {
    orderSel.replaceChildren(el("option", { value: "", text: "此類別尚無有效報價" }));
    return;
  }

  orderSel.replaceChildren(
    ...quotes.map((q) =>
      el("option", {
        value: q.symbol,
        text: `${q.symbol} · ${q.symbol_name} (${fmtMoney(q.price)} 元)`,
      }),
    ),
  );
  if (prevSym && quotes.some((q) => q.symbol === prevSym)) {
    orderSel.value = prevSym;
  }
  prefillPriceForSelected();
}

function setOrderSide(side) {
  const normalized = side === "SELL" ? "SELL" : "BUY";
  document.getElementById("order-side").value = normalized;
  document.getElementById("side-buy-btn").classList.toggle("active-buy", normalized === "BUY");
  document.getElementById("side-sell-btn").classList.toggle("active-sell", normalized === "SELL");
  updateOrderRiskPreview();
}

function prefillPriceForSelected() {
  const sym = document.getElementById("order-symbol").value;
  const catKey = document.getElementById("order-category").value;
  let price = null;
  let stockMeta = null;
  if (catKey && catKey !== "__watchlist__") {
    const foundCls = state.orderClassQuotes.find((q) => q.symbol === sym);
    if (foundCls) price = Number(foundCls.price);
  }
  const foundWatch = state.stocks.find((s) => s.symbol === sym);
  if (foundWatch) {
    stockMeta = foundWatch;
    if (price === null) price = Number(foundWatch.latest_close);
  }
  if (price !== null && price > 0) {
    document.getElementById("order-price").value = price;
    const slVal = stockMeta && isFiniteNum(stockMeta.suggested_stop_loss) && Number(stockMeta.suggested_stop_loss) > 0
      ? Number(stockMeta.suggested_stop_loss)
      : price * 0.95;
    const tpVal = stockMeta && isFiniteNum(stockMeta.suggested_take_profit) && Number(stockMeta.suggested_take_profit) > 0
      ? Number(stockMeta.suggested_take_profit)
      : price * 1.1;
    document.getElementById("order-sl").value = slVal.toFixed(2);
    document.getElementById("order-tp").value = tpVal.toFixed(2);
  }
  updateOrderRiskPreview();
}

function prefillOrder(stock) {
  document.getElementById("order-group").value = "__watchlist__";
  populateOrderCategories("__watchlist__");
  document.getElementById("order-symbol").value = stock.symbol;
  setOrderSide(stock.action === "SELL" ? "SELL" : "BUY");
  prefillPriceForSelected();
  if (isFiniteNum(stock.latest_close) && Number(stock.latest_close) > 0) {
    const price = Number(stock.latest_close);
    document.getElementById("order-price").value = price;
    document.getElementById("order-sl").value = (
      isFiniteNum(stock.suggested_stop_loss) && Number(stock.suggested_stop_loss) > 0
        ? Number(stock.suggested_stop_loss)
        : price * 0.95
    ).toFixed(2);
    document.getElementById("order-tp").value = (
      isFiniteNum(stock.suggested_take_profit) && Number(stock.suggested_take_profit) > 0
        ? Number(stock.suggested_take_profit)
        : price * 1.1
    ).toFixed(2);
  }
  updateOrderRiskPreview();
  switchTab("tab-trade");
}

function updateOrderRiskPreview() {
  const box = document.getElementById("order-risk-preview");
  if (!box) return;

  const side = document.getElementById("order-side").value || "BUY";
  const symbol = document.getElementById("order-symbol").value || "";
  const qty = Number(document.getElementById("order-qty").value);
  const price = Number(document.getElementById("order-price").value);
  const slRaw = document.getElementById("order-sl").value.trim();
  const tpRaw = document.getElementById("order-tp").value.trim();
  const sl = slRaw !== "" ? Number(slRaw) : null;
  const tp = tpRaw !== "" ? Number(tpRaw) : null;

  const validOrder = Number.isFinite(qty) && qty > 0 && Number.isFinite(price) && price > 0;
  const orderValue = validOrder ? qty * price : 0;
  const totalEquity = state.portfolio && isFiniteNum(state.portfolio.total_equity)
    ? Number(state.portfolio.total_equity)
    : 0;
  const cashBalance = state.portfolio && isFiniteNum(state.portfolio.cash_balance)
    ? Number(state.portfolio.cash_balance)
    : null;
  const allocPct = totalEquity > 0 && validOrder ? (orderValue / totalEquity) * 100 : null;

  let existingValue = 0;
  if (state.portfolio && Array.isArray(state.portfolio.positions) && symbol) {
    const pos = state.portfolio.positions.find((p) => p.symbol === symbol);
    if (pos && isFiniteNum(pos.quantity) && isFiniteNum(pos.current_price)) {
      existingValue = Number(pos.quantity) * Number(pos.current_price);
    }
  }
  const combinedPct = totalEquity > 0 && validOrder ? ((existingValue + orderValue) / totalEquity) * 100 : null;

  const notes = [];
  let stateClass = "";

  if (!validOrder) {
    box.className = "risk-preview";
    box.replaceChildren(
      el("div", { class: "risk-preview-head" }, [
        el("span", { text: "下單前即時風險試算" }),
        helpTipBtn("risk_guard", "了解單標 20% 上限與停損風險"),
      ]),
      el("p", { class: "risk-note risk-note-ok", text: "請輸入有效的委託股數與價格，系統會即時試算佔比與停損風險。" }),
    );
    return;
  }

  let lossDisplay = "--";
  let lossSubCls = "";

  if (side === "BUY") {
    const overSingle = allocPct !== null && allocPct > 20;
    const overCombined = !overSingle && existingValue > 0 && combinedPct !== null && combinedPct > 20;
    if (overSingle) {
      stateClass = "risk-state-danger";
      notes.push({
        cls: "risk-note-danger",
        text: "超過單標 20% 安全線，送單時會被風控攔截",
      });
    } else if (overCombined) {
      stateClass = "risk-state-danger";
      notes.push({
        cls: "risk-note-danger",
        text: `加上既有持倉後達 ${combinedPct.toFixed(1)}%，超過單標 20% 安全線，送單時會被風控攔截`,
      });
    }

    if (cashBalance !== null && orderValue > cashBalance) {
      stateClass = "risk-state-danger";
      notes.push({
        cls: "risk-note-danger",
        text: `委託總額超過可用模擬現金（剩餘 ${fmtMoney(cashBalance)} 元）`,
      });
    }

    if (sl === null || !Number.isFinite(sl) || sl <= 0) {
      lossDisplay = "未設定";
      lossSubCls = "risk-note-warn";
      if (!stateClass) stateClass = "risk-state-warn";
      notes.push({
        cls: "risk-note-warn",
        text: `尚未設定停損價，建議至少設在買價 -5%（約 ${(price * 0.95).toFixed(2)} 元）`,
      });
    } else if (sl >= price) {
      lossDisplay = "設定無效";
      lossSubCls = "risk-note-warn";
      if (!stateClass) stateClass = "risk-state-warn";
      notes.push({
        cls: "risk-note-warn",
        text: `停損價需低於買進委託價（${fmtMoney(price)} 元）才能發揮止損保護`,
      });
    } else {
      const estLoss = (price - sl) * qty;
      const slDistPct = ((price - sl) / price) * 100;
      lossDisplay = `-${fmtMoney(estLoss)} 元 (-${slDistPct.toFixed(1)}%)`;
      if (slDistPct > 15) {
        if (!stateClass) stateClass = "risk-state-warn";
        notes.push({
          cls: "risk-note-warn",
          text: `停損距離達 ${slDistPct.toFixed(1)}%（超過 15%），空間偏寬恐失去風險保護意義`,
        });
      }
      if (tp !== null && Number.isFinite(tp) && tp > price) {
        const estGain = (tp - price) * qty;
        const rr = (tp - price) / (price - sl);
        notes.push({
          cls: "risk-note-ok",
          text: `若達停利預估獲利 +${fmtMoney(estGain)} 元（預估賺賠比 ${rr.toFixed(1)} : 1）`,
        });
      }
    }

    if (!notes.length) {
      notes.push({
        cls: "risk-note-ok",
        text: "單標配置在 20% 安全線內且已設停損，可點擊下方按鈕進行風控預覽。",
      });
    }
  } else {
    lossDisplay = "賣出減碼";
    notes.push({
      cls: "risk-note-ok",
      text: "賣出委託會減少持倉並釋放模擬現金（不受單標 20% 上限與冷靜鎖限制）。",
    });
  }

  const barFill = el("div", {
    class: `risk-bar-fill${side === "BUY" && ((allocPct !== null && allocPct > 20) || (combinedPct !== null && combinedPct > 20)) ? " over-limit" : ""}`,
  });
  const barPct = allocPct !== null ? Math.min(100, (allocPct / 25) * 100) : 0;
  barFill.style.width = `${barPct.toFixed(1)}%`;

  box.className = `risk-preview${stateClass ? ` ${stateClass}` : ""}`;
  box.replaceChildren(
    el("div", { class: "risk-preview-head" }, [
      el("span", { text: "下單前即時風險試算" }),
      helpTipBtn("risk_guard", "了解單標 20% 上限與停損試算"),
    ]),
    el("div", { class: "risk-preview-grid" }, [
      el("div", { class: "risk-metric" }, [
        el("span", { text: "本次委託總金額 / 佔總資產" }),
        el("strong", {
          class: `mono${side === "BUY" && allocPct !== null && allocPct > 20 ? " pos" : ""}`,
          text: allocPct !== null
            ? `${fmtMoney(orderValue)} 元 (${allocPct.toFixed(1)}%)`
            : `${fmtMoney(orderValue)} 元`,
        }),
      ]),
      el("div", { class: "risk-metric" }, [
        el("span", { text: side === "BUY" ? "若觸發停損預估虧損" : "預估動作" }),
        el("strong", { class: `mono ${lossSubCls}`, text: lossDisplay }),
      ]),
    ]),
    side === "BUY" && allocPct !== null ? el("div", { class: "risk-bar-track" }, [barFill]) : null,
    el(
      "div",
      { class: "risk-notes" },
      notes.map((n) => el("p", { class: `risk-note ${n.cls}`, text: n.text })),
    ),
  );
}

async function removeWatchlistSymbol(symbol) {
  const fb = document.getElementById("watchlist-feedback");
  const res = await api(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" });
  if (res.ok) {
    fb.textContent = `已從核心追蹤移除 ${symbol}。`;
    renderOverview(res.data);
    await loadRealityCheck();
  } else {
    fb.textContent = res.data.error || "移除失敗";
  }
}

function renderPortfolioTables(portfolio) {
  const posBody = document.getElementById("positions-body");
  if (!portfolio.positions.length) {
    posBody.replaceChildren(
      el("tr", {}, [el("td", { colspan: "5", class: "muted-text", text: "目前尚無模擬持倉，可於左側試著買進第一筆。" })]),
    );
  } else {
    posBody.replaceChildren(
      ...portfolio.positions.map((p) =>
        el("tr", {}, [
          el("td", { text: `${p.name} (${p.symbol})` }),
          el("td", { class: "num", text: `${p.quantity}` }),
          el("td", { class: "num", text: `${p.avg_cost} / ${p.current_price}` }),
          el("td", {
            class: `num ${p.unrealized_pnl >= 0 ? "pos" : "neg"}`,
            text: `${p.unrealized_pnl >= 0 ? "+" : ""}${fmtMoney(p.unrealized_pnl)} (${p.unrealized_pct}%)`,
          }),
          el("td", { class: "num", text: `${p.stop_loss_price ?? "--"} / ${p.take_profit_price ?? "--"}` }),
        ]),
      ),
    );
  }

  const ordBody = document.getElementById("orders-body");
  if (!portfolio.recent_orders.length) {
    ordBody.replaceChildren(
      el("tr", {}, [el("td", { colspan: "6", class: "muted-text", text: "尚無下單紀錄。" })]),
    );
  } else {
    ordBody.replaceChildren(
      ...portfolio.recent_orders.map((o) =>
        el("tr", {}, [
          el("td", { class: "mono", text: o.created_at }),
          el("td", { class: "mono", text: o.symbol }),
          el("td", { text: `${o.side} (${o.order_type})` }),
          el("td", { class: "num", text: `${o.quantity} × ${o.price}` }),
          el("td", { class: "num", text: `${fmtMoney(o.max_loss_estimate)} 元` }),
          el("td", { class: "wrap-cell" }, [
            el("span", { class: `tag tag-${o.status}`, text: o.status }),
            o.reject_reason ? el("span", { text: ` ${o.reject_reason}` }) : null,
          ]),
        ]),
      ),
    );
  }
}

const JOB_LABELS = {
  fetch_pre_market: "盤前行情抓取",
  fetch_intraday: "盤中定時更新",
  fetch_post_market: "盤後完整抓取",
  run_strategy_analysis: "策略健康度評分",
  generate_daily_report: "產出小白日報",
  sync_yahoo_classes: "台股全類別同步",
};

const STATUS_LABELS = {
  SUCCESS: "成功",
  PARTIAL: "部分異常",
  FAILED: "失敗",
};

const pad2 = (n) => String(n).padStart(2, "0");

function parseCronToSimple(expr) {
  const s = String(expr || "").trim();
  let m = s.match(/^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+(1-5|\*)$/);
  if (m) {
    return { mode: "fixed", minute: Number(m[1]), hour: Number(m[2]), days: m[3] };
  }
  m = s.match(/^\*\/(\d{1,2})\s+(\d{1,2})-(\d{1,2})\s+\*\s+\*\s+(1-5|\*)$/);
  if (m) {
    return { mode: "interval", every: Number(m[1]), startHour: Number(m[2]), endHour: Number(m[3]), days: m[4] };
  }
  m = s.match(/^0\s+(\d{1,2})-(\d{1,2})\s+\*\s+\*\s+(1-5|\*)$/);
  if (m) {
    return { mode: "interval", every: 60, startHour: Number(m[1]), endHour: Number(m[2]), days: m[3] };
  }
  return { mode: "custom", raw: s };
}

function simpleToCron(cfg) {
  if (cfg.mode === "fixed") {
    return `${Number(cfg.minute)} ${Number(cfg.hour)} * * ${cfg.days}`;
  }
  if (cfg.mode === "interval") {
    const start = Number(cfg.startHour);
    const end = Math.max(start, Number(cfg.endHour));
    const every = Number(cfg.every);
    if (every === 60) return `0 ${start}-${end} * * ${cfg.days}`;
    return `*/${every} ${start}-${end} * * ${cfg.days}`;
  }
  return String(cfg.raw || "").trim();
}

function describeSchedule(cfg) {
  const daysText = cfg.days === "1-5" ? "週一至週五（開盤日）" : "每天";
  if (cfg.mode === "fixed") {
    return `${daysText} ${pad2(cfg.hour)}:${pad2(cfg.minute)} 執行一次`;
  }
  if (cfg.mode === "interval") {
    const start = Number(cfg.startHour);
    const end = Math.max(start, Number(cfg.endHour));
    return `${daysText} ${pad2(start)}:00–${pad2(end)}:59 每 ${cfg.every} 分鐘輪詢`;
  }
  return `進階設定（${cfg.raw}）`;
}

function buildScheduleEditor(cronExpr) {
  let cfg = parseCronToSimple(cronExpr);
  if (cfg.mode === "custom") {
    cfg = { mode: "custom", raw: cronExpr, hour: 9, minute: 0, every: 15, startHour: 9, endHour: 13, days: "1-5" };
  } else {
    cfg = { hour: 9, minute: 0, every: 15, startHour: 9, endHour: 13, days: "1-5", ...cfg };
  }

  const modeSel = el("select", { "aria-label": "執行方式" }, [
    el("option", { value: "fixed", text: "固定時間" }),
    el("option", { value: "interval", text: "時段輪詢" }),
    ...(cfg.mode === "custom" ? [el("option", { value: "custom", text: "進階格式" })] : []),
  ]);
  modeSel.value = cfg.mode;

  const daysSel = el("select", { "aria-label": "執行星期" }, [
    el("option", { value: "1-5", text: "週一至週五（開盤日）" }),
    el("option", { value: "*", text: "每天" }),
  ]);
  daysSel.value = cfg.days;

  const timeInput = el("input", {
    type: "time",
    class: "mono",
    "aria-label": "執行時間",
    value: `${pad2(cfg.hour)}:${pad2(cfg.minute)}`,
  });

  const everySel = el("select", { "aria-label": "輪詢間隔" }, [
    ...[5, 10, 15, 30, 60].map((m) => el("option", { value: String(m), text: `每 ${m} 分鐘` })),
  ]);
  if (![5, 10, 15, 30, 60].includes(cfg.every)) {
    everySel.appendChild(el("option", { value: String(cfg.every), text: `每 ${cfg.every} 分鐘` }));
  }
  everySel.value = String(cfg.every);

  const hourOpts = () =>
    Array.from({ length: 24 }, (_, h) => el("option", { value: String(h), text: `${pad2(h)}:00` }));
  const startSel = el("select", { "aria-label": "開始小時" }, hourOpts());
  startSel.value = String(cfg.startHour);
  const endSel = el("select", { "aria-label": "結束小時" }, hourOpts());
  endSel.value = String(cfg.endHour);

  const customInput = el("input", { type: "text", class: "mono input-compact", value: cfg.raw || cronExpr });
  const previewEl = el("div", { class: "sched-preview" });
  const controlsRow = el("div", { class: "sched-controls" });

  function readCfg() {
    const mode = modeSel.value;
    if (mode === "fixed") {
      const [hh, mm] = (timeInput.value || "09:00").split(":").map(Number);
      return { mode, hour: hh || 0, minute: mm || 0, days: daysSel.value };
    }
    if (mode === "interval") {
      const startHour = Number(startSel.value);
      const endHour = Math.max(startHour, Number(endSel.value));
      endSel.value = String(endHour);
      return { mode, every: Number(everySel.value), startHour, endHour, days: daysSel.value };
    }
    return { mode: "custom", raw: customInput.value };
  }

  function syncLayout() {
    const mode = modeSel.value;
    if (mode === "fixed") {
      controlsRow.replaceChildren(daysSel, modeSel, timeInput);
    } else if (mode === "interval") {
      controlsRow.replaceChildren(
        daysSel,
        modeSel,
        startSel,
        el("span", { class: "sched-sep", text: "到" }),
        endSel,
        everySel,
      );
    } else {
      controlsRow.replaceChildren(modeSel, customInput);
    }
    previewEl.textContent = describeSchedule(readCfg());
  }

  [modeSel, daysSel, timeInput, everySel, startSel, endSel, customInput].forEach((ctrl) => {
    ctrl.addEventListener("change", syncLayout);
  });
  syncLayout();

  return {
    root: el("div", { class: "sched-editor" }, [controlsRow, previewEl]),
    getCron: () => simpleToCron(readCfg()),
  };
}

function renderSchedules(data) {
  const schedBody = document.getElementById("schedules-body");
  schedBody.replaceChildren(
    ...data.schedules.map((s) => {
      const editor = buildScheduleEditor(s.cron_expr);
      const enabledChk = el("input", { type: "checkbox" });
      enabledChk.checked = Boolean(s.enabled);
      const friendlyName = JOB_LABELS[s.job_name] || s.job_name;

      return el("tr", {}, [
        el("td", { class: "wrap-cell" }, [
          el("strong", { text: friendlyName }),
          el("div", { class: "muted-text", style: "font-size: 0.76rem;", text: s.description }),
        ]),
        el("td", {}, [editor.root]),
        el("td", {}, [enabledChk]),
        el("td", {}, [
          el("div", { class: "btn-group" }, [
            el("button", {
              class: "btn btn-ghost btn-sm",
              type: "button",
              text: "儲存",
              onclick: async () => {
                const fb = document.getElementById("schedule-feedback");
                const res = await api(`/api/schedules/${encodeURIComponent(s.job_name)}`, {
                  method: "PUT",
                  body: JSON.stringify({ cron_expr: editor.getCron(), enabled: enabledChk.checked }),
                });
                fb.textContent = res.ok ? `已更新「${friendlyName}」排程設定。` : `更新失敗：${res.data.error}`;
                if (res.ok) await loadSchedules();
              },
            }),
            el("button", {
              class: "btn btn-primary btn-sm",
              type: "button",
              text: "立即執行",
              onclick: async () => {
                const fb = document.getElementById("schedule-feedback");
                const res = await api(`/api/schedules/${encodeURIComponent(s.job_name)}/run`, { method: "POST" });
                fb.textContent = res.ok
                  ? `已手動執行「${friendlyName}」：${STATUS_LABELS[res.data.status] || res.data.status}，處理 ${res.data.records_processed} 筆 (${res.data.duration_ms} ms)`
                  : `執行失敗：${res.data.error}`;
                await Promise.all([loadOverview(), loadSchedules()]);
              },
            }),
          ]),
        ]),
      ]);
    }),
  );

  const logsBody = document.getElementById("logs-body");
  if (!data.logs.length) {
    logsBody.replaceChildren(el("tr", {}, [el("td", { colspan: "6", class: "muted-text", text: "尚無排程執行紀錄。" })]));
  } else {
    logsBody.replaceChildren(
      ...data.logs.map((l) =>
        el("tr", {}, [
          el("td", { class: "mono", text: l.started_at }),
          el("td", { text: JOB_LABELS[l.job_name] || l.job_name }),
          el("td", { text: l.trigger_type === "manual" ? "手動重跑" : "定時排程" }),
          el("td", {}, [el("span", { class: `tag tag-${l.status}`, text: STATUS_LABELS[l.status] || l.status })]),
          el("td", { class: "num", text: `${l.duration_ms} ms / 重試 ${l.retry_count}` }),
          el("td", { class: "wrap-cell", text: l.error_message || `處理 ${l.records_processed} 筆` }),
        ]),
      ),
    );
  }
}

async function loadOverview() {
  const res = await api("/api/overview");
  if (res.ok) renderOverview(res.data);
}

async function loadSchedules() {
  const res = await api("/api/schedules");
  if (res.ok) renderSchedules(res.data);
}

async function loadRealityCheck() {
  const res = await api("/api/reality-check");
  if (!res.ok) return;
  const d = res.data;
  const summaryEl = document.getElementById("reality-summary");

  const extraChips = [];
  if (isFiniteNum(d.max_drawdown_pct)) {
    const mddVal = Math.abs(Number(d.max_drawdown_pct)).toFixed(2);
    extraChips.push(
      el("span", { class: "reality-chip" }, [
        el("span", { text: "歷史最大連續回檔" }),
        el("strong", { class: Number(mddVal) > 0 ? "neg" : "", text: `-${mddVal}%` }),
        helpTipBtn("max_drawdown", "了解歷史最大連續回檔與獲利因子"),
      ]),
    );
  }
  if (isFiniteNum(d.profit_factor)) {
    extraChips.push(
      el("span", { class: "reality-chip" }, [
        el("span", { text: "獲利因子（總賺÷總賠）" }),
        el("strong", { text: `${Number(d.profit_factor).toFixed(2)}x` }),
      ]),
    );
  } else if (d.profit_factor === null && Number(d.directional_signals || 0) > 0) {
    extraChips.push(
      el("span", { class: "reality-chip" }, [
        el("span", { text: "獲利因子" }),
        el("strong", { text: "尚無虧損樣本" }),
      ]),
    );
  }
  if (isFiniteNum(d.win_loss_ratio)) {
    extraChips.push(
      el("span", { class: "reality-chip" }, [
        el("span", { text: "平均賺賠比" }),
        el("strong", { text: `${Number(d.win_loss_ratio).toFixed(2)}` }),
      ]),
    );
  }

  summaryEl.replaceChildren(
    el("strong", {
      class: "mono",
      text: `資料源 ${d.source} ｜ 方向命中率 ${d.accuracy_pct}%（${d.directional_signals} 筆訊號）｜ 平均報酬 ${d.avg_signal_return_pct >= 0 ? "+" : ""}${d.avg_signal_return_pct}% ｜ 平均 ${d.horizon_days} 日波動 ${d.mae_pct}%`,
    }),
    extraChips.length ? el("div", { class: "reality-metrics" }, extraChips) : null,
    el("p", { style: "margin: 8px 0 0;", text: d.calibration_hint }),
  );

  const body = document.getElementById("reality-body");
  if (!d.per_symbol.length) {
    body.replaceChildren(el("tr", {}, [el("td", { colspan: "4", class: "muted-text", text: "歷史資料筆數尚不足 25 筆，暫無法計算回測。" })]));
    return;
  }
  body.replaceChildren(
    ...d.per_symbol.map((r) =>
      el("tr", {}, [
        el("td", { text: `${r.name} (${r.symbol})` }),
        el("td", { class: "num", text: `${r.directional_signals}` }),
        el("td", { class: "num", text: r.accuracy_pct !== null ? `${r.accuracy_pct}%` : "無方向訊號" }),
        el("td", {
          class: `num ${(r.avg_signal_return_pct || 0) >= 0 ? "pos" : "neg"}`,
          text: r.avg_signal_return_pct !== null ? `${r.avg_signal_return_pct >= 0 ? "+" : ""}${r.avg_signal_return_pct}%` : "--",
        }),
      ]),
    ),
  );
}

function switchTab(tabId) {
  document.querySelectorAll(".nav-btn").forEach((b) => {
    const active = b.dataset.tab === tabId;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
    if (active && b.dataset.title) {
      document.getElementById("page-title").textContent = b.dataset.title;
    }
  });
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === tabId));
}

function openConfirmModal(orderPayload, check) {
  state.pendingOrder = orderPayload;
  const sideZh = orderPayload.side === "BUY" ? "買進" : "賣出";
  document.getElementById("modal-summary").textContent =
    `準備${sideZh} ${orderPayload.symbol} 共 ${orderPayload.quantity} 股，單價 ${orderPayload.price} 元（委託總額 ${fmtMoney(orderPayload.quantity * orderPayload.price)} 元）。`;
  document.getElementById("modal-max-loss").textContent = `${fmtMoney(check.max_loss_estimate)} 元`;

  const list = document.getElementById("modal-reasons");
  list.replaceChildren(...(check.reasons || []).map((r) => el("li", { text: r })));
  document.getElementById("confirm-modal").classList.remove("hidden");
  document.getElementById("modal-confirm").focus();
}

function closeConfirmModal() {
  state.pendingOrder = null;
  document.getElementById("confirm-modal").classList.add("hidden");
}

function renderForecastResult(d) {
  const box = document.getElementById("forecast-result");
  const minVal = Math.min(d.low_price, d.latest_close) * 0.99;
  const maxVal = Math.max(d.high_price, d.latest_close) * 1.01;
  const span = maxVal - minVal || 1;
  const pct = (v) => `${Math.max(0, Math.min(100, ((v - minVal) / span) * 100)).toFixed(1)}%`;

  const band = el("div", { class: "gauge-band" });
  band.style.left = pct(d.low_price);
  band.style.width = `${Math.max(4, ((d.high_price - d.low_price) / span) * 100).toFixed(1)}%`;

  const nowMark = el("div", { class: "gauge-marker gauge-marker-now", title: `現價 ${d.latest_close}` });
  nowMark.style.left = pct(d.latest_close);

  const expMark = el("div", { class: "gauge-marker gauge-marker-exp", title: `中位推估 ${d.expected_price}` });
  expMark.style.left = pct(d.expected_price);

  const riskCls = d.risk_level === "高" ? "tag-HEAVY_BUY" : (d.risk_level === "中" ? "tag-HOLD" : "tag-ADD");

  box.replaceChildren(
    el("div", { class: "panel-head", style: "margin-bottom: 0;" }, [
      el("strong", { text: `${d.name} (${d.symbol}) · 現價 ${fmtMoney(d.latest_close)} 元` }),
      el("span", { class: `tag ${riskCls}`, text: `波動風險：${d.risk_level}` }),
    ]),
    el("div", { class: "gauge-wrap" }, [
      el("div", { class: "gauge-legend" }, [
        el("span", { class: "mono", text: `低標 ${fmtMoney(d.low_price)}` }),
        el("span", { class: "mono", text: `中位 ${fmtMoney(d.expected_price)}` }),
        el("span", { class: "mono", text: `高標 ${fmtMoney(d.high_price)}` }),
      ]),
      el("div", { class: "gauge-bar" }, [band, nowMark, expMark]),
      el("div", { class: "gauge-legend" }, [
        el("span", { text: "灰標：目前股價" }),
        el("span", { text: "藍標：情境預期中位" }),
      ]),
    ]),
    el("div", { class: "forecast-range" }, [
      el("div", {}, [el("span", { class: "muted-text", text: "保守低標" }), el("strong", { class: "mono", text: `${fmtMoney(d.low_price)} 元` })]),
      el("div", {}, [el("span", { class: "muted-text", text: "中位推估" }), el("strong", { class: "mono", text: `${fmtMoney(d.expected_price)} (${d.expected_return_pct >= 0 ? "+" : ""}${d.expected_return_pct}%)` })]),
      el("div", {}, [el("span", { class: "muted-text", text: "樂觀高標" }), el("strong", { class: "mono", text: `${fmtMoney(d.high_price)} 元` })]),
    ]),
    el("p", { class: "rationale-box", style: "margin-bottom: 0;", text: d.plain_explanation }),
  );
}

function wireEvents() {
  const savedTheme = localStorage.getItem("stockmate-theme") || "light";
  applyTheme(savedTheme);
  document.getElementById("btn-theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    applyTheme(cur);
  });

  applyGuideVisibility();
  const dismissGuideBtn = document.getElementById("btn-dismiss-guide");
  if (dismissGuideBtn) dismissGuideBtn.addEventListener("click", dismissGuide);
  const restoreGuideBtn = document.getElementById("btn-restore-guide");
  if (restoreGuideBtn) restoreGuideBtn.addEventListener("click", restoreGuide);
  const guideGlossaryBtn = document.getElementById("btn-guide-glossary");
  if (guideGlossaryBtn) guideGlossaryBtn.addEventListener("click", () => openGlossary());
  const openGlossaryBtn = document.getElementById("btn-open-glossary");
  if (openGlossaryBtn) openGlossaryBtn.addEventListener("click", () => openGlossary());
  const glossaryCloseBtn = document.getElementById("glossary-close");
  if (glossaryCloseBtn) glossaryCloseBtn.addEventListener("click", closeGlossary);
  const glossaryModal = document.getElementById("glossary-modal");
  if (glossaryModal) {
    glossaryModal.addEventListener("click", (ev) => {
      if (ev.target === glossaryModal) closeGlossary();
    });
  }

  const guideStep1 = document.getElementById("guide-step-1");
  if (guideStep1) {
    guideStep1.addEventListener("click", () => {
      const inspector = document.getElementById("stock-inspector");
      if (inspector) inspector.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  }
  const guideStep2 = document.getElementById("guide-step-2");
  if (guideStep2) guideStep2.addEventListener("click", () => switchTab("tab-trade"));
  const guideStep3 = document.getElementById("guide-step-3");
  if (guideStep3) guideStep3.addEventListener("click", () => switchTab("tab-forecast"));

  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".help-tip-btn[data-glossary]");
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    openGlossary(btn.dataset.glossary);
  });

  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  document.getElementById("order-group").addEventListener("change", () => populateOrderCategories());
  document.getElementById("order-category").addEventListener("change", () => loadOrderSymbolsForCategory());
  document.getElementById("order-symbol").addEventListener("change", prefillPriceForSelected);
  document.getElementById("side-buy-btn").addEventListener("click", () => setOrderSide("BUY"));
  document.getElementById("side-sell-btn").addEventListener("click", () => setOrderSide("SELL"));
  ["order-qty", "order-price", "order-sl", "order-tp"].forEach((id) => {
    const input = document.getElementById(id);
    if (input) {
      input.addEventListener("input", updateOrderRiskPreview);
      input.addEventListener("change", updateOrderRiskPreview);
    }
  });
  updateOrderRiskPreview();

  const wlInput = document.getElementById("watchlist-input");
  wlInput.addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    const q = wlInput.value.trim();
    if (!q) return;
    state.searchTimer = setTimeout(async () => {
      const res = await api(`/api/stocks/search?q=${encodeURIComponent(q)}`);
      if (!res.ok) return;
      document.getElementById("watchlist-suggestions").replaceChildren(
        ...(res.data.items || []).map((item) =>
          el("option", { value: `${item.symbol} - ${item.symbol_name}` }),
        ),
      );
    }, 180);
  });

  document.getElementById("watchlist-add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fb = document.getElementById("watchlist-feedback");
    const raw = wlInput.value.trim();
    if (!raw) return;
    fb.textContent = "正在抓取並驗證報價...";
    const res = await api("/api/watchlist", {
      method: "POST",
      body: JSON.stringify({ symbol: raw }),
    });
    if (res.ok) {
      wlInput.value = "";
      fb.textContent = "已新增至核心追蹤清單！";
      const stocks = res.data.stocks || [];
      if (stocks.length) state.selectedSymbol = stocks[stocks.length - 1].symbol;
      renderOverview(res.data);
      await loadRealityCheck();
    } else {
      fb.textContent = res.data.error || "新增失敗";
    }
  });

  document.getElementById("source-select").addEventListener("change", async (e) => {
    const res = await api("/api/source", {
      method: "POST",
      body: JSON.stringify({ source: e.target.value }),
    });
    if (res.ok) {
      renderOverview(res.data);
      await loadRealityCheck();
    } else {
      const notice = document.getElementById("source-notice-banner");
      notice.textContent = res.data.error || "切換資料源失敗";
      notice.classList.remove("hidden");
      await loadOverview();
    }
  });

  document.getElementById("btn-refresh-market").addEventListener("click", async () => {
    await api("/api/schedules/fetch_post_market/run", { method: "POST" });
    await Promise.all([loadOverview(), loadSchedules(), loadRealityCheck()]);
  });

  document.getElementById("live-interval-select").addEventListener("change", async (e) => {
    const val = Number(e.target.value);
    const body = val === 0 ? { enabled: false } : { enabled: true, interval_sec: val };
    const res = await api("/api/live", { method: "POST", body: JSON.stringify(body) });
    if (res.ok) updateLiveBadge(res.data);
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollLiveStatus();
  });

  document.getElementById("btn-run-reality-check").addEventListener("click", loadRealityCheck);

  document.getElementById("btn-sync-classes").addEventListener("click", async () => {
    const msgEl = document.getElementById("classes-sync-msg");
    const res = await api("/api/classes/sync", { method: "POST" });
    if (res.ok) {
      msgEl.textContent = res.data.started ? "已啟動全類別背景同步，正在抓取最新成分股報價..." : "全類別同步已在執行中...";
      startSyncPolling();
    }
  });

  document.getElementById("class-search").addEventListener("input", () => loadClasses());
  document.getElementById("quote-search").addEventListener("input", () => {
    if (state.selectedCategoryKey) loadCategoryQuotes(state.selectedCategoryKey);
  });

  document.getElementById("order-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fb = document.getElementById("order-feedback");
    const payload = {
      symbol: document.getElementById("order-symbol").value,
      side: document.getElementById("order-side").value,
      quantity: Number(document.getElementById("order-qty").value),
      price: Number(document.getElementById("order-price").value),
      stop_loss_price: document.getElementById("order-sl").value || null,
      take_profit_price: document.getElementById("order-tp").value || null,
    };

    const res = await api("/api/orders/preview", { method: "POST", body: JSON.stringify(payload) });
    if (!res.ok) {
      fb.textContent = `輸入格式錯誤：${res.data.error}`;
      return;
    }
    if (res.data.requires_confirmation) {
      fb.textContent = "";
      openConfirmModal(payload, res.data);
    } else {
      fb.textContent = `風控攔截：${(res.data.reasons || []).join("；")}`;
      await loadOverview();
    }
  });

  const modal = document.getElementById("confirm-modal");
  document.getElementById("modal-cancel").addEventListener("click", closeConfirmModal);
  modal.addEventListener("click", (ev) => {
    if (ev.target === modal) closeConfirmModal();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    const gModal = document.getElementById("glossary-modal");
    if (gModal && !gModal.classList.contains("hidden")) {
      closeGlossary();
      return;
    }
    if (!modal.classList.contains("hidden")) closeConfirmModal();
  });

  document.getElementById("modal-confirm").addEventListener("click", async () => {
    if (!state.pendingOrder) return;
    const payload = { ...state.pendingOrder, confirmed_by_user: true };
    closeConfirmModal();

    const fb = document.getElementById("order-feedback");
    const res = await api("/api/orders", { method: "POST", body: JSON.stringify(payload) });
    if (res.ok && res.data.ok) {
      fb.textContent = `模擬下單成功（單號 #${res.data.order.order_id}）！`;
    } else {
      const reasons = res.data.check?.reasons?.join("；") || res.data.error || "下單未通過";
      fb.textContent = `無法下單：${reasons}`;
    }
    await loadOverview();
  });

  document.getElementById("btn-check-triggers").addEventListener("click", async () => {
    const fb = document.getElementById("order-feedback");
    const res = await api("/api/positions/check-triggers", { method: "POST" });
    if (res.ok) {
      fb.textContent = res.data.triggered.length
        ? `已自動觸發 ${res.data.triggered.length} 筆停損/停利單！`
        : "目前持倉尚未觸及停損或停利價格。";
      await loadOverview();
    }
  });

  const mktInput = document.getElementById("forecast-mkt");
  const volInput = document.getElementById("forecast-vol");
  mktInput.addEventListener("input", () => {
    document.getElementById("val-mkt").textContent = `${Number(mktInput.value) >= 0 ? "+" : ""}${Number(mktInput.value).toFixed(1)}%`;
  });
  volInput.addEventListener("input", () => {
    document.getElementById("val-vol").textContent = `${Number(volInput.value).toFixed(1)} 倍`;
  });

  document.getElementById("forecast-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const res = await api("/api/forecast/simulate", {
      method: "POST",
      body: JSON.stringify({
        symbol: document.getElementById("forecast-symbol").value,
        market_change_pct: Number(mktInput.value),
        volume_multiplier: Number(volInput.value),
        news_sentiment: document.getElementById("forecast-news").value,
      }),
    });

    if (!res.ok) {
      document.getElementById("forecast-result").replaceChildren(
        el("p", { class: "feedback-msg", text: `試算失敗：${res.data.error}` }),
      );
      return;
    }
    renderForecastResult(res.data);
  });
}

async function loadClasses() {
  const q = document.getElementById("class-search").value.trim();
  const params = new URLSearchParams();
  if (state.classGroup) params.set("group", state.classGroup);
  if (q) params.set("q", q);
  const res = await api(`/api/classes?${params.toString()}`);
  if (!res.ok) return;

  const d = res.data;
  if (!state.classGroup && !q) {
    state.allClasses = d.categories || [];
    populateOrderCategories();
  }
  const totalCats = Object.values(d.group_counts).reduce((a, b) => a + b, 0);
  document.getElementById("classes-summary").textContent = d.categories.length
    ? `共 ${totalCats} 個分類 · 收錄 ${d.unique_symbols} 檔不重複台股（${d.total_quote_rows} 筆）· 更新：${d.last_updated_at || "--"}`
    : "尚未同步台股全類別資料，請點擊右側「立即同步全類別」。";

  const exportLink = document.getElementById("btn-export-csv");
  const expParams = new URLSearchParams();
  if (state.classGroup) expParams.set("group", state.classGroup);
  else if (state.selectedCategoryKey) expParams.set("category_key", state.selectedCategoryKey);
  exportLink.href = `/api/classes/export.csv?${expParams.toString()}`;

  const groups = ["", "上市", "上櫃", "興櫃", "概念股", "集團股", "電子產業"];
  document.getElementById("class-group-pills").replaceChildren(
    ...groups.map((g) => {
      const label = g ? `${g} (${d.group_counts[g] || 0})` : "全部";
      return el("button", {
        type: "button",
        class: `pill-btn ${state.classGroup === g ? "active" : ""}`,
        text: label,
        onclick: () => {
          state.classGroup = g;
          state.selectedCategoryKey = "";
          loadClasses();
        },
      });
    }),
  );

  const listEl = document.getElementById("class-list");
  if (!d.categories.length) {
    listEl.replaceChildren(el("p", { class: "muted-text", text: "目前無符合條件的分類。" }));
    document.getElementById("class-quotes-body").replaceChildren();
    return;
  }

  if (!state.selectedCategoryKey || !d.categories.some((c) => c.category_key === state.selectedCategoryKey)) {
    state.selectedCategoryKey = d.categories[0].category_key;
  }

  listEl.replaceChildren(
    ...d.categories.map((c) => {
      const avgText = c.avg_change_pct !== null ? `${c.avg_change_pct >= 0 ? "+" : ""}${c.avg_change_pct}%` : "--";
      return el("button", {
        type: "button",
        class: `class-item ${c.category_key === state.selectedCategoryKey ? "active" : ""}`,
        onclick: () => {
          state.selectedCategoryKey = c.category_key;
          loadClasses();
        },
      }, [
        el("div", {}, [
          el("div", { class: "class-item-name", text: c.name }),
          el("div", { class: "class-item-meta", text: `${c.group_type} · ${c.stock_count} 檔 (漲${c.up_count}/跌${c.down_count})` }),
        ]),
        el("span", {
          class: `mono ${(c.avg_change_pct || 0) >= 0 ? "pos" : "neg"}`,
          style: "font-weight: 600; font-size: 0.82rem;",
          text: avgText,
        }),
      ]);
    }),
  );

  if (d.sync_status?.running) startSyncPolling();
  await loadCategoryQuotes(state.selectedCategoryKey);
}

async function loadCategoryQuotes(categoryKey, refresh = false) {
  const q = document.getElementById("quote-search").value.trim();
  const qs = new URLSearchParams();
  if (q) qs.set("q", q);
  if (refresh) qs.set("refresh", "1");
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await api(`/api/classes/${encodeURIComponent(categoryKey)}/quotes${suffix}`);
  if (!res.ok) return;

  const { category, quotes } = res.data;
  document.getElementById("selected-class-title").textContent = `${category.group_type} · ${category.name}（${quotes.length} 檔）`;

  const body = document.getElementById("class-quotes-body");
  if (!quotes.length) {
    body.replaceChildren(el("tr", {}, [el("td", { colspan: "6", class: "muted-text", text: "尚無成分股報價資料。" })]));
    return;
  }

  body.replaceChildren(
    ...quotes.map((item) => {
      const chgCls = (item.change_pct || 0) > 0 ? "pos" : ((item.change_pct || 0) < 0 ? "neg" : "");
      const chgText = item.change_pct !== null
        ? `${item.change >= 0 ? "+" : ""}${item.change} (${item.change_pct >= 0 ? "+" : ""}${item.change_pct}%)`
        : "--";
      const lots = Math.round((item.volume || 0) / 1000);

      return el("tr", {}, [
        el("td", {}, [
          el("div", { style: "display: flex; align-items: center; gap: 6px;" }, [
            el("strong", { text: item.symbol_name }),
            item.limit_up ? el("span", { class: "tag tag-HEAVY_BUY", text: "漲停" }) : null,
            item.limit_down ? el("span", { class: "tag tag-SELL", text: "跌停" }) : null,
          ]),
          el("div", { class: "mono muted-text", style: "font-size: 0.74rem;", text: item.symbol }),
        ]),
        el("td", { class: `num ${chgCls}`, style: "font-weight: 600;", text: item.price !== null ? `${fmtMoney(item.price)}` : "--" }),
        el("td", { class: `num ${chgCls}`, text: chgText }),
        el("td", { class: "num", text: `${item.open ?? "--"} / ${item.high ?? "--"} / ${item.low ?? "--"} / ${item.prev_close ?? "--"}` }),
        el("td", { class: "num", text: `${fmtMoney(lots)} 張 / ${item.turnover_m ?? "--"} M` }),
        el("td", {}, [
          item.price ? el("button", {
            type: "button",
            class: "btn btn-ghost btn-sm",
            text: "帶入下單",
            onclick: async () => {
              document.getElementById("order-group").value = category.group_type || "";
              populateOrderCategories(category.category_key);
              await loadOrderSymbolsForCategory(item.symbol);
              setOrderSide("BUY");
              switchTab("tab-trade");
            },
          }) : el("span", { class: "muted-text", text: "--" }),
        ]),
      ]);
    }),
  );
}

function startSyncPolling() {
  if (state.syncTimer) return;
  const track = document.getElementById("classes-sync-progress");
  const bar = document.getElementById("classes-sync-bar");
  track.classList.remove("hidden");

  state.syncTimer = setInterval(async () => {
    const res = await api("/api/classes/sync-status");
    if (!res.ok) return;
    const s = res.data;
    const msgEl = document.getElementById("classes-sync-msg");
    if (s.running) {
      const pct = s.total_categories ? Math.round((s.synced_categories / s.total_categories) * 100) : 10;
      bar.style.width = `${pct}%`;
      msgEl.textContent = `同步進行中：${s.synced_categories} / ${s.total_categories || "?"} 類別（已寫入 ${s.total_quote_rows} 筆報價）...`;
    } else {
      clearInterval(state.syncTimer);
      state.syncTimer = null;
      bar.style.width = "100%";
      setTimeout(() => track.classList.add("hidden"), 400);
      msgEl.textContent = s.last_error
        ? `同步完成（部分異常）：${s.synced_categories} 類別、${s.total_quote_rows} 筆，錯誤：${s.last_error}`
        : `同步完成：共 ${s.synced_categories} 類別、${s.total_quote_rows} 筆報價（${s.last_finished_at || ""}）`;
      await Promise.all([loadClasses(), loadSchedules()]);
    }
  }, 1500);
}

function updateLiveBadge(live) {
  const dot = document.getElementById("live-dot");
  const sel = document.getElementById("live-interval-select");
  dot.classList.toggle("paused", !live.enabled);
  sel.value = live.enabled ? String(live.interval_sec) : "0";
}

async function pollLiveStatus() {
  if (document.hidden) return;
  const res = await api("/api/live");
  if (!res.ok) return;
  const live = res.data;
  updateLiveBadge(live);

  if (state.lastDataVersion === null) {
    state.lastDataVersion = live.data_version;
    return;
  }

  if (live.data_version > state.lastDataVersion) {
    state.lastDataVersion = live.data_version;
    const pill = document.getElementById("latest-quote-pill");
    pill.classList.add("flash-updated");
    setTimeout(() => pill.classList.remove("flash-updated"), 1200);

    const tasks = [loadOverview(), loadSchedules(), loadRealityCheck()];
    if (document.getElementById("tab-classes").classList.contains("active") && state.selectedCategoryKey) {
      tasks.push(loadCategoryQuotes(state.selectedCategoryKey, true));
    }
    await Promise.all(tasks);
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  wireEvents();
  await Promise.all([loadOverview(), loadSchedules(), loadRealityCheck(), loadClasses(), pollLiveStatus()]);
  setInterval(pollLiveStatus, 5000);
});
