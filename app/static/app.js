"use strict";

const state = {
  stocks: [],
  pendingOrder: null,
};

const fmtMoney = (n) => Number(n).toLocaleString("zh-TW", { maximumFractionDigits: 2 });

/** 安全建立 DOM 節點，全部文字皆透過 textContent 寫入以避免 XSS。 */
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

function buildSparkline(values, isUp) {
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 200 48");
  svg.setAttribute("class", "sparkline");
  if (!values || values.length < 2) return svg;

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pts = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * 196 + 2;
      const y = 44 - ((v - min) / span) * 40;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const poly = document.createElementNS(svgNS, "polyline");
  poly.setAttribute("fill", "none");
  poly.setAttribute("stroke", isUp ? "#dc2626" : "#059669");
  poly.setAttribute("stroke-width", "2.2");
  poly.setAttribute("points", pts);
  svg.appendChild(poly);
  return svg;
}

function renderOverview(data) {
  const { portfolio, daily_report: report, stocks } = data;
  state.stocks = stocks;

  // 資料源切換與降級提示
  const sourceSel = document.getElementById("source-select");
  if (data.active_source) sourceSel.value = data.active_source;

  const noticeBanner = document.getElementById("source-notice-banner");
  if (data.source_notice) {
    noticeBanner.textContent = data.source_notice;
    noticeBanner.classList.remove("hidden");
  } else {
    noticeBanner.classList.add("hidden");
  }

  // 冷靜鎖定橫幅
  const banner = document.getElementById("lockout-banner");
  if (portfolio.active_lockout) {
    banner.textContent = `冷靜保護中（暫停買進至 ${portfolio.active_lockout.unlock_at}）：${portfolio.active_lockout.reason}`;
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }

  // KPI
  document.getElementById("kpi-equity").textContent = `${fmtMoney(portfolio.total_equity)} 元`;
  document.getElementById("kpi-return").textContent = `累計報酬 ${portfolio.total_return_pct >= 0 ? "+" : ""}${portfolio.total_return_pct}%`;
  document.getElementById("kpi-cash").textContent = `${fmtMoney(portfolio.cash_balance)} 元`;
  const pnlEl = document.getElementById("kpi-pnl");
  pnlEl.textContent = `${portfolio.daily_realized_pnl >= 0 ? "+" : ""}${fmtMoney(portfolio.daily_realized_pnl)} 元`;
  document.getElementById("kpi-health").textContent = `${report.avg_health_score} / 100`;
  document.getElementById("kpi-health-note").textContent = data.source_label || report.report_date;

  // 白話日報
  document.getElementById("report-headline").textContent = report.headline;
  document.getElementById("report-date").textContent = data.latest_quote_time
    ? `最新報價 ${data.latest_quote_time}`
    : report.report_date;
  document.getElementById("report-summary").textContent = report.summary;
  const chips = document.getElementById("report-counts");
  chips.replaceChildren(
    el("span", { class: "badge badge-HEAVY_BUY", text: `重壓 ${report.counts.HEAVY_BUY || 0}` }),
    el("span", { class: "badge badge-ADD", text: `補倉 ${report.counts.ADD || 0}` }),
    el("span", { class: "badge badge-HOLD", text: `繼續蹲 ${report.counts.HOLD || 0}` }),
    el("span", { class: "badge badge-SELL", text: `拋出 ${report.counts.SELL || 0}` }),
  );

  // 標的卡片
  const grid = document.getElementById("stock-grid");
  grid.replaceChildren(
    ...stocks.map((s) => {
      const fill = el("div", { class: "health-bar-fill" });
      fill.style.width = `${s.health_score}%`;

      return el("article", { class: "stock-card" }, [
        el("div", { class: "stock-top" }, [
          el("div", {}, [
            el("h3", { class: "stock-name", text: s.name }),
            el("span", { class: "stock-code", text: `${s.symbol} · ${s.ma_trend}` }),
          ]),
          el("div", { class: "stock-price" }, [
            el("div", { text: `${fmtMoney(s.latest_close)} 元` }),
            el("div", {
              class: `stock-change ${s.change_pct >= 0 ? "pos" : "neg"}`,
              text: `${s.change_pct >= 0 ? "+" : ""}${s.change_pct}%`,
            }),
          ]),
        ]),
        buildSparkline(s.sparkline, s.change_pct >= 0),
        el("div", { class: "stock-meta" }, [
          el("span", { text: `健康度 ${s.health_score} 分` }),
          el("span", { class: `badge badge-${s.action}`, text: `建議：${s.action_label}` }),
        ]),
        el("div", { class: "health-bar-wrap" }, [fill]),
        el("div", { class: "stock-meta" }, [
          el("span", { text: `月線乖離 ${s.bias_pct >= 0 ? "+" : ""}${s.bias_pct}%` }),
          el("span", { text: `量比 ${s.volume_ratio}x · 勝率 ${s.win_rate_nd}%` }),
        ]),
        el("p", { class: "stock-reason", text: s.plain_reason }),
        el("button", {
          class: "btn btn-outline btn-sm",
          type: "button",
          text: "帶入模擬下單",
          onclick: () => prefillOrder(s),
        }),
      ]);
    }),
  );

  syncSymbolSelects(stocks);
  renderPortfolioTables(portfolio);
}

function syncSymbolSelects(stocks) {
  const orderSel = document.getElementById("order-symbol");
  const forecastSel = document.getElementById("forecast-symbol");
  const curOrder = orderSel.value;
  const curForecast = forecastSel.value;

  const makeOpts = () => stocks.map((s) => el("option", { value: s.symbol, text: `${s.name} (${s.symbol}) · ${s.latest_close} 元` }));
  orderSel.replaceChildren(...makeOpts());
  forecastSel.replaceChildren(...makeOpts());

  if (curOrder) orderSel.value = curOrder;
  if (curForecast) forecastSel.value = curForecast;

  if (!document.getElementById("order-price").value && stocks.length) {
    prefillPriceForSelected();
  }
}

function prefillPriceForSelected() {
  const sym = document.getElementById("order-symbol").value;
  const found = state.stocks.find((s) => s.symbol === sym);
  if (found) {
    document.getElementById("order-price").value = found.latest_close;
    document.getElementById("order-sl").value = (found.latest_close * 0.95).toFixed(2);
    document.getElementById("order-tp").value = (found.latest_close * 1.1).toFixed(2);
  }
}

function prefillOrder(stock) {
  document.getElementById("order-symbol").value = stock.symbol;
  document.getElementById("order-side").value = stock.action === "SELL" ? "SELL" : "BUY";
  prefillPriceForSelected();
  switchTab("tab-trade");
}

function renderPortfolioTables(portfolio) {
  const posBody = document.getElementById("positions-body");
  if (!portfolio.positions.length) {
    posBody.replaceChildren(
      el("tr", {}, [el("td", { colspan: "5", class: "muted", text: "目前尚無模擬持倉，可於左側試著買進第一筆。" })]),
    );
  } else {
    posBody.replaceChildren(
      ...portfolio.positions.map((p) =>
        el("tr", {}, [
          el("td", { text: `${p.name} (${p.symbol})` }),
          el("td", { text: `${p.quantity}` }),
          el("td", { text: `${p.avg_cost} / ${p.current_price}` }),
          el("td", {
            class: p.unrealized_pnl >= 0 ? "pos" : "neg",
            text: `${p.unrealized_pnl >= 0 ? "+" : ""}${fmtMoney(p.unrealized_pnl)} (${p.unrealized_pct}%)`,
          }),
          el("td", { text: `${p.stop_loss_price ?? "--"} / ${p.take_profit_price ?? "--"}` }),
        ]),
      ),
    );
  }

  const ordBody = document.getElementById("orders-body");
  if (!portfolio.recent_orders.length) {
    ordBody.replaceChildren(
      el("tr", {}, [el("td", { colspan: "6", class: "muted", text: "尚無下單紀錄。" })]),
    );
  } else {
    ordBody.replaceChildren(
      ...portfolio.recent_orders.map((o) =>
        el("tr", {}, [
          el("td", { text: o.created_at }),
          el("td", { text: o.symbol }),
          el("td", { text: `${o.side} (${o.order_type})` }),
          el("td", { text: `${o.quantity} × ${o.price}` }),
          el("td", { text: `${fmtMoney(o.max_loss_estimate)} 元` }),
          el("td", { class: "wrap-cell" }, [
            el("span", { class: `badge badge-${o.status}`, text: o.status }),
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
      const cronInput = el("input", { type: "text", value: s.cron_expr });
      const enabledChk = el("input", { type: "checkbox" });
      enabledChk.checked = Boolean(s.enabled);

      return el("tr", {}, [
        el("td", { text: s.job_name }),
        el("td", { class: "wrap-cell", text: s.description }),
        el("td", {}, [cronInput]),
        el("td", {}, [enabledChk]),
        el("td", {}, [
          el("button", {
            class: "btn btn-outline btn-sm",
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
          " ",
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
      ]);
    }),
  );

  const logsBody = document.getElementById("logs-body");
  if (!data.logs.length) {
    logsBody.replaceChildren(el("tr", {}, [el("td", { colspan: "6", class: "muted", text: "尚無排程執行紀錄。" })]));
  } else {
    logsBody.replaceChildren(
      ...data.logs.map((l) =>
        el("tr", {}, [
          el("td", { text: l.started_at }),
          el("td", { text: l.job_name }),
          el("td", { text: l.trigger_type === "manual" ? "手動重跑" : "定時排程" }),
          el("td", {}, [el("span", { class: `badge badge-${l.status}`, text: l.status })]),
          el("td", { text: `${l.duration_ms} ms / 重試 ${l.retry_count}` }),
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
      text: `資料源：${d.source} ｜ 方向命中率：${d.accuracy_pct}%（${d.directional_signals} 次方向訊號）｜ 平均訊號報酬：${d.avg_signal_return_pct >= 0 ? "+" : ""}${d.avg_signal_return_pct}% ｜ 平均 ${d.horizon_days} 日波動幅度：${d.mae_pct}%`,
    }),
    el("p", { style: "margin: 6px 0 0; color: #334155;", text: d.calibration_hint }),
  );

  const body = document.getElementById("reality-body");
  if (!d.per_symbol.length) {
    body.replaceChildren(el("tr", {}, [el("td", { colspan: "4", class: "muted", text: "歷史資料筆數尚不足 25 筆，暫無法計算回測。" })]));
    return;
  }
  body.replaceChildren(
    ...d.per_symbol.map((r) =>
      el("tr", {}, [
        el("td", { text: `${r.name} (${r.symbol})` }),
        el("td", { text: `${r.directional_signals}` }),
        el("td", { text: r.accuracy_pct !== null ? `${r.accuracy_pct}%` : "無方向訊號" }),
        el("td", {
          class: (r.avg_signal_return_pct || 0) >= 0 ? "pos" : "neg",
          text: r.avg_signal_return_pct !== null ? `${r.avg_signal_return_pct >= 0 ? "+" : ""}${r.avg_signal_return_pct}%` : "--",
        }),
      ]),
    ),
  );
}

function switchTab(tabId) {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabId));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === tabId));
}

function openConfirmModal(orderPayload, check) {
  state.pendingOrder = orderPayload;
  const sideZh = orderPayload.side === "BUY" ? "買進" : "賣出";
  document.getElementById("modal-summary").textContent =
    `準備${sideZh} ${orderPayload.symbol} 共 ${orderPayload.quantity} 股，單價 ${orderPayload.price} 元（總金額 ${fmtMoney(orderPayload.quantity * orderPayload.price)} 元）。`;
  document.getElementById("modal-max-loss").textContent = `${fmtMoney(check.max_loss_estimate)} 元`;

  const list = document.getElementById("modal-reasons");
  list.replaceChildren(...(check.reasons || []).map((r) => el("li", { text: r })));
  document.getElementById("confirm-modal").classList.remove("hidden");
}

function closeConfirmModal() {
  state.pendingOrder = null;
  document.getElementById("confirm-modal").classList.add("hidden");
}

function wireEvents() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  document.getElementById("order-symbol").addEventListener("change", prefillPriceForSelected);

  document.getElementById("source-select").addEventListener("change", async (e) => {
    const target = e.target.value;
    const res = await api("/api/source", {
      method: "POST",
      body: JSON.stringify({ source: target }),
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

  document.getElementById("btn-run-reality-check").addEventListener("click", loadRealityCheck);

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

  document.getElementById("modal-cancel").addEventListener("click", closeConfirmModal);

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

    const box = document.getElementById("forecast-result");
    if (!res.ok) {
      box.replaceChildren(el("p", { class: "feedback-msg", text: `試算失敗：${res.data.error}` }));
      return;
    }
    const d = res.data;
    box.replaceChildren(
      el("div", { class: "stock-meta" }, [
        el("strong", { text: `${d.name} (${d.symbol}) · 現價 ${fmtMoney(d.latest_close)} 元` }),
        el("span", { class: `badge badge-${d.risk_level === "高" ? "SELL" : d.risk_level === "中" ? "HOLD" : "HEAVY_BUY"}`, text: `波動風險：${d.risk_level}` }),
      ]),
      el("div", { class: "forecast-range" }, [
        el("div", {}, [el("span", { class: "muted", text: "保守低標" }), el("strong", { text: `${fmtMoney(d.low_price)} 元` })]),
        el("div", {}, [el("span", { class: "muted", text: "中位推估" }), el("strong", { text: `${fmtMoney(d.expected_price)} 元 (${d.expected_return_pct >= 0 ? "+" : ""}${d.expected_return_pct}%)` })]),
        el("div", {}, [el("span", { class: "muted", text: "樂觀高標" }), el("strong", { text: `${fmtMoney(d.high_price)} 元` })]),
      ]),
      el("p", { class: "stock-reason", text: d.plain_explanation }),
    );
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  wireEvents();
  await Promise.all([loadOverview(), loadSchedules(), loadRealityCheck()]);
});
