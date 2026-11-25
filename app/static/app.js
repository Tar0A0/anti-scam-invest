"use strict";

const state = {
  stocks: [],
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
        el("span", { text: "健康度評分" }),
        el("strong", { class: "mono", text: `${stock.health_score} / 100` }),
      ]),
      el("div", { class: "metric-box" }, [
        el("span", { text: "月線 MA20 / 乖離" }),
        el("strong", { class: "mono", text: `${fmtMoney(stock.ma20)} (${stock.bias_pct >= 0 ? "+" : ""}${stock.bias_pct}%)` }),
      ]),
      el("div", { class: "metric-box" }, [
        el("span", { text: "成交量能倍數" }),
        el("strong", { class: "mono", text: `${stock.volume_ratio}x` }),
      ]),
      el("div", { class: "metric-box" }, [
        el("span", { text: "近 10 日上漲勝率" }),
        el("strong", { class: "mono", text: `${stock.win_rate_nd}%` }),
      ]),
    ]),
    el("p", { class: "rationale-box", text: stock.plain_reason }),
    el("div", { class: "btn-group" }, [
      el("button", {
        type: "button",
        class: "btn btn-primary",
        text: "前往模擬下單",
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
}

function prefillPriceForSelected() {
  const sym = document.getElementById("order-symbol").value;
  const catKey = document.getElementById("order-category").value;
  let price = null;
  if (catKey && catKey !== "__watchlist__") {
    const foundCls = state.orderClassQuotes.find((q) => q.symbol === sym);
    if (foundCls) price = Number(foundCls.price);
  }
  if (price === null) {
    const found = state.stocks.find((s) => s.symbol === sym);
    if (found) price = Number(found.latest_close);
  }
  if (price !== null && price > 0) {
    document.getElementById("order-price").value = price;
    document.getElementById("order-sl").value = (price * 0.95).toFixed(2);
    document.getElementById("order-tp").value = (price * 1.1).toFixed(2);
  }
}

function prefillOrder(stock) {
  document.getElementById("order-group").value = "__watchlist__";
  populateOrderCategories("__watchlist__");
  document.getElementById("order-symbol").value = stock.symbol;
  setOrderSide(stock.action === "SELL" ? "SELL" : "BUY");
  prefillPriceForSelected();
  switchTab("tab-trade");
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

function renderSchedules(data) {
  const schedBody = document.getElementById("schedules-body");
  schedBody.replaceChildren(
    ...data.schedules.map((s) => {
      const cronInput = el("input", { type: "text", class: "mono input-compact", value: s.cron_expr });
      const enabledChk = el("input", { type: "checkbox" });
      enabledChk.checked = Boolean(s.enabled);

      return el("tr", {}, [
        el("td", { class: "mono", text: s.job_name }),
        el("td", { class: "wrap-cell", text: s.description }),
        el("td", {}, [cronInput]),
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
                  body: JSON.stringify({ cron_expr: cronInput.value, enabled: enabledChk.checked }),
                });
                fb.textContent = res.ok ? `已更新 ${s.job_name} 排程設定。` : `更新失敗：${res.data.error}`;
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
                  ? `已手動執行 ${s.job_name}：狀態 ${res.data.status}，處理 ${res.data.records_processed} 筆 (${res.data.duration_ms} ms)`
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
          el("td", { class: "mono", text: l.job_name }),
          el("td", { text: l.trigger_type === "manual" ? "手動重跑" : "定時排程" }),
          el("td", {}, [el("span", { class: `tag tag-${l.status}`, text: l.status })]),
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
  summaryEl.replaceChildren(
    el("strong", {
      class: "mono",
      text: `資料源 ${d.source} ｜ 方向命中率 ${d.accuracy_pct}%（${d.directional_signals} 筆訊號）｜ 平均報酬 ${d.avg_signal_return_pct >= 0 ? "+" : ""}${d.avg_signal_return_pct}% ｜ 平均 ${d.horizon_days} 日波動 ${d.mae_pct}%`,
    }),
    el("p", { style: "margin: 6px 0 0;", text: d.calibration_hint }),
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

  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  document.getElementById("order-group").addEventListener("change", () => populateOrderCategories());
  document.getElementById("order-category").addEventListener("change", () => loadOrderSymbolsForCategory());
  document.getElementById("order-symbol").addEventListener("change", prefillPriceForSelected);
  document.getElementById("side-buy-btn").addEventListener("click", () => setOrderSide("BUY"));
  document.getElementById("side-sell-btn").addEventListener("click", () => setOrderSide("SELL"));

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
    if (ev.key === "Escape" && !modal.classList.contains("hidden")) closeConfirmModal();
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
