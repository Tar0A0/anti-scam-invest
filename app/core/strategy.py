"""新手友善統計指標、四選一白話策略建議、盤後日報與條件式預測模擬引擎。"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timezone
from typing import Any

ACTION_LABELS: dict[str, str] = {
    "HEAVY_BUY": "重壓",
    "ADD": "補倉",
    "SELL": "拋出",
    "HOLD": "繼續蹲",
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _extract_optional_positive_float(row: sqlite3.Row | dict[str, Any], key: str) -> float | None:
    if isinstance(row, dict):
        val = row.get(key)
    else:
        val = row[key] if key in row.keys() else None
    if val is None:
        return None
    try:
        num = float(val)
        return num if num > 0 else None
    except (TypeError, ValueError):
        return None


def _compute_rsi14(closes: list[float]) -> float:
    """計算 14 日 Wilder RSI（樣本不足時以可用天數計算，範圍 0~100）。"""
    if len(closes) < 2:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    period = min(14, len(deltas))
    avg_gain = sum(max(d, 0.0) for d in deltas[:period]) / period
    avg_loss = sum(max(-d, 0.0) for d in deltas[:period]) / period
    for delta in deltas[period:]:
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return _clamp(100.0 - (100.0 / (1.0 + rs)), 0.0, 100.0)


def _classify_rsi(rsi14: float) -> str:
    """將 RSI 數值轉譯為新手易懂的白話體溫標籤。"""
    if rsi14 > 70.0:
        return "過熱(>70)"
    if rsi14 >= 55.0:
        return "偏強(55~70)"
    if rsi14 >= 45.0:
        return "中性(45~55)"
    if rsi14 >= 30.0:
        return "偏弱(30~45)"
    return "超賣(<30)"


def _compute_atr14_pct(
    quotes: list[sqlite3.Row | dict[str, Any]],
    closes: list[float],
    latest_close: float,
) -> float:
    """計算近 14 日平均真實／收盤振幅百分比（%），衡量個股日常波動度。"""
    if latest_close <= 0 or not closes:
        return 0.0
    true_ranges: list[float] = []
    for i, q in enumerate(quotes):
        high = _extract_optional_positive_float(q, "high")
        low = _extract_optional_positive_float(q, "low")
        if high is not None and low is not None and high >= low:
            if i > 0:
                prev_c = closes[i - 1]
                tr = max(high - low, abs(high - prev_c), abs(low - prev_c))
            else:
                tr = high - low
            true_ranges.append(tr)
        elif i > 0:
            true_ranges.append(abs(closes[i] - closes[i - 1]))
    if not true_ranges:
        return 0.0
    window = true_ranges[-14:]
    atr14 = sum(window) / len(window)
    return max(0.0, atr14 / latest_close * 100.0)


def _compute_support_resistance(
    quotes: list[sqlite3.Row | dict[str, Any]],
    closes: list[float],
) -> tuple[float, float]:
    """計算近 20 日支撐與壓力參考價（有高低價優先參考高低點，否則以收盤價區間估算）。"""
    recent_quotes = quotes[-20:]
    recent_closes = closes[-20:]
    lows: list[float] = []
    highs: list[float] = []
    for q, c in zip(recent_quotes, recent_closes):
        low = _extract_optional_positive_float(q, "low")
        high = _extract_optional_positive_float(q, "high")
        lows.append(min(low, c) if low is not None else c)
        highs.append(max(high, c) if high is not None else c)
    return min(lows), max(highs)


def analyze_quotes(symbol: str, display_name: str, quotes: list[sqlite3.Row | dict[str, Any]]) -> dict[str, Any]:
    """將原始 K 線轉為新手看得懂的健康度評分（0~100）與四選一操作建議。"""
    if not quotes:
        raise ValueError(f"標的 {symbol} 尚無報價資料可分析")

    closes = [float(q["close"]) for q in quotes]
    volumes = [int(q["volume"]) for q in quotes]
    latest_close = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else latest_close
    change_pct = ((latest_close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0

    ma5 = sum(closes[-5:]) / min(len(closes), 5)
    ma20 = sum(closes[-20:]) / min(len(closes), 20)
    bias_pct = ((latest_close - ma20) / ma20 * 100.0) if ma20 > 0 else 0.0

    avg_vol = (sum(volumes[-20:]) / min(len(volumes), 20)) or 1.0
    volume_ratio = volumes[-1] / avg_vol

    # 計算近 N 日（最多 10 日）上漲天數比例作為白話勝率參考
    recent = closes[-11:]
    if len(recent) >= 2:
        up_days = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
        win_rate_nd = up_days / (len(recent) - 1) * 100.0
    else:
        win_rate_nd = 50.0

    # 進階量化指標：14 日 RSI、14 日波動率、近 20 日支撐／壓力、建議停損／停利價
    rsi14 = round(_compute_rsi14(closes), 2)
    rsi_label = _classify_rsi(rsi14)
    atr14_pct = round(_compute_atr14_pct(quotes, closes, latest_close), 2)
    raw_support, raw_resistance = _compute_support_resistance(quotes, closes)
    support_price = round(raw_support, 2)
    resistance_price = round(raw_resistance, 2)

    latest_close_rounded = round(latest_close, 2)
    vol_pct = max(atr14_pct, 1.5)
    stop_dist_pct = _clamp(vol_pct * 2.0, 3.0, 10.0)
    atr_stop = latest_close * (1.0 - stop_dist_pct / 100.0)
    support_stop = raw_support * 0.99
    candidate_stop = min(atr_stop, support_stop) if support_stop >= latest_close * 0.88 else atr_stop
    raw_stop = _clamp(candidate_stop, latest_close * 0.88, latest_close * 0.97)
    suggested_stop_loss = round(raw_stop, 2)
    if suggested_stop_loss >= latest_close_rounded:
        suggested_stop_loss = round(max(0.01, latest_close_rounded - 0.01), 2)

    risk_amount = max(latest_close - suggested_stop_loss, latest_close * 0.03)
    rr_target = latest_close + risk_amount * 1.8
    if latest_close * 1.04 <= raw_resistance <= latest_close * 1.25:
        raw_tp = max(rr_target, raw_resistance)
    else:
        raw_tp = rr_target
    raw_tp = _clamp(raw_tp, latest_close * 1.04, latest_close * 1.30)
    suggested_take_profit = round(raw_tp, 2)
    if suggested_take_profit <= latest_close_rounded:
        suggested_take_profit = round(latest_close_rounded + 0.01, 2)

    # 計算健康度評分（基礎 50 分，依均線、乖離、勝率、RSI 與支撐壓力加減）
    score = 50.0
    if latest_close >= ma20:
        score += 18.0
    else:
        score -= 12.0
    if ma5 >= ma20:
        score += 12.0
    else:
        score -= 8.0
    if -4.0 <= bias_pct <= 5.0:
        score += 10.0
    elif bias_pct > 8.0 or bias_pct < -8.0:
        score -= 10.0
    score += (win_rate_nd - 50.0) * 0.25

    if rsi14 > 70.0:
        score -= 6.0
    elif rsi14 >= 55.0:
        score += 4.0
    elif rsi14 < 30.0:
        score -= 4.0

    dist_support_pct = ((latest_close - raw_support) / latest_close * 100.0) if latest_close > 0 else 0.0
    dist_resistance_pct = ((raw_resistance - latest_close) / latest_close * 100.0) if latest_close > 0 else 0.0
    if latest_close >= ma20 and dist_support_pct <= 3.0:
        score += 3.0
    if dist_resistance_pct <= 1.5 and rsi14 > 65.0:
        score -= 3.0

    health_score = int(round(_clamp(score, 0.0, 100.0)))

    # 決定四選一白話策略建議
    if latest_close > ma20 and ma5 >= ma20 and 0.0 <= bias_pct <= 4.5 and volume_ratio >= 1.1:
        action = "HEAVY_BUY"
        ma_trend = "站穩月線且短線轉強"
        plain_reason = (
            f"股價站上近 20 日平均價（乖離 {bias_pct:+.1f}%）且成交量放大到平時的 "
            f"{volume_ratio:.1f} 倍，屬於風險相對可控的順勢起漲點。"
        )
    elif latest_close >= ma20 * 0.98 and bias_pct < 0 and ma5 >= ma20 * 0.99:
        action = "ADD"
        ma_trend = "多頭格局小幅回檔"
        plain_reason = (
            f"大方向還沒走壞，只是短線稍微拉回休息（偏離月線 {bias_pct:+.1f}%），"
            "適合分批小額補倉攤平，不建議一次梭哈。"
        )
    elif bias_pct > 8.0 or (latest_close < ma20 * 0.96 and ma5 < ma20):
        action = "SELL"
        if bias_pct > 8.0:
            ma_trend = "短線漲多過熱"
            plain_reason = (
                f"短期跑得太快，已經偏離月線 +{bias_pct:.1f}%，隨時可能獲利回吐，"
                "建議分批拋出把獲利放進口袋。"
            )
        else:
            ma_trend = "跌破月線轉弱"
            plain_reason = (
                f"股價跌破近 20 日支撐（乖離 {bias_pct:+.1f}%）且短期均線向下，"
                "建議先減碼或停損撤退，保留現金更安全。"
            )
    else:
        action = "HOLD"
        if abs(bias_pct) <= 3.5:
            ma_trend = "區間盤整觀望"
        elif volume_ratio < 1.0:
            ma_trend = "跌破月線但量縮"
        else:
            ma_trend = "跌破月線帶量震盪"
        plain_reason = (
            f"目前方向還不明朗（近 10 日上漲機率 {win_rate_nd:.0f}%、成交量為平時 "
            f"{volume_ratio:.1f} 倍），先繼續蹲著觀察，別急著亂動。"
        )

    if rsi14 > 70.0:
        quant_note = (
            f" 目前 RSI 達 {rsi14:.1f}（過熱），短線追高風險偏大，"
            f"參考壓力價 {resistance_price:,.2f} 元、防守支撐 {support_price:,.2f} 元。"
        )
    elif rsi14 < 30.0:
        quant_note = (
            f" 目前 RSI 僅 {rsi14:.1f}（超賣），雖有機會醞釀短線反彈但趨勢仍弱，"
            f"先觀察近 20 日支撐 {support_price:,.2f} 元是否守穩。"
        )
    else:
        quant_note = (
            f" 目前 RSI 為 {rsi14:.1f}（{rsi_label}），"
            f"近 20 日參考支撐 {support_price:,.2f} 元、壓力 {resistance_price:,.2f} 元。"
        )
    plain_reason = f"{plain_reason}{quant_note}"

    return {
        "symbol": symbol,
        "name": display_name,
        "latest_close": latest_close_rounded,
        "change_pct": round(change_pct, 2),
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "bias_pct": round(bias_pct, 2),
        "volume_ratio": round(volume_ratio, 2),
        "win_rate_nd": round(win_rate_nd, 1),
        "rsi14": rsi14,
        "rsi_label": rsi_label,
        "atr14_pct": atr14_pct,
        "support_price": support_price,
        "resistance_price": resistance_price,
        "suggested_stop_loss": suggested_stop_loss,
        "suggested_take_profit": suggested_take_profit,
        "health_score": health_score,
        "action": action,
        "action_label": ACTION_LABELS[action],
        "ma_trend": ma_trend,
        "plain_reason": plain_reason,
        "sparkline": [round(c, 2) for c in closes[-20:]],
    }


def run_strategy_and_save(
    conn: sqlite3.Connection,
    symbol_names: dict[str, str],
    *,
    source: str | None = None,
) -> int:
    """重新計算所有追蹤標的的指標與建議，並寫入 strategy_signals 資料表。"""
    signal_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    processed = 0

    with conn:
        for symbol, name in symbol_names.items():
            if source:
                rows = conn.execute(
                    """
                    SELECT quote_time, high, low, close, volume
                    FROM market_quotes
                    WHERE symbol = ? AND source = ?
                    ORDER BY quote_time ASC;
                    """,
                    (symbol, source),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT quote_time, high, low, close, volume
                    FROM market_quotes
                    WHERE symbol = ?
                    ORDER BY quote_time ASC;
                    """,
                    (symbol,),
                ).fetchall()
            if not rows:
                continue

            analysis = analyze_quotes(symbol, name, rows)
            conn.execute(
                """
                INSERT INTO strategy_signals
                    (symbol, signal_date, action, health_score, ma_trend,
                     bias_pct, volume_ratio, win_rate_nd, plain_reason, model_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1')
                ON CONFLICT(symbol, signal_date, model_version) DO UPDATE SET
                    action = excluded.action,
                    health_score = excluded.health_score,
                    ma_trend = excluded.ma_trend,
                    bias_pct = excluded.bias_pct,
                    volume_ratio = excluded.volume_ratio,
                    win_rate_nd = excluded.win_rate_nd,
                    plain_reason = excluded.plain_reason;
                """,
                (
                    symbol,
                    signal_date,
                    analysis["action"],
                    analysis["health_score"],
                    analysis["ma_trend"],
                    analysis["bias_pct"],
                    analysis["volume_ratio"],
                    analysis["win_rate_nd"],
                    analysis["plain_reason"],
                ),
            )
            processed += 1
    return processed


def build_daily_report(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    """根據目前各標的解析結果，組合小白看得懂的盤後白話日報。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not analyses:
        return {
            "report_date": today,
            "avg_health_score": 0,
            "counts": {k: 0 for k in ACTION_LABELS},
            "headline": "目前尚無市場資料，請先點選更新模擬行情。",
            "summary": "系統準備完成後，將在此自動整理每日白話重點。",
        }

    avg_score = round(sum(a["health_score"] for a in analyses) / len(analyses))
    counts = {k: sum(1 for a in analyses if a["action"] == k) for k in ACTION_LABELS}
    best = max(analyses, key=lambda a: a["health_score"])
    risky = [a for a in analyses if a["action"] == "SELL"]

    if avg_score >= 68:
        mood = "整體偏多健康，可依紀律尋找順勢機會"
    elif avg_score >= 48:
        mood = "整體盤整震盪，保持耐心與分批節奏"
    else:
        mood = "整體氣溫偏冷，守住現金、嚴設停損最優先"

    risk_text = (
        f"需留意「{'、'.join(r['name'] for r in risky)}」出現過熱或轉弱訊號，記得檢視停利停損。"
        if risky
        else "目前追蹤標的皆無急迫撤退訊號，持續觀察量能變化即可。"
    )
    summary = (
        f"今日市場平均健康度 {avg_score} 分（{mood}）。"
        f"表現最穩健的是 {best['name']}（{best['symbol']}，健康度 {best['health_score']} 分，建議「{best['action_label']}」）。"
        f"{risk_text}"
    )

    return {
        "report_date": today,
        "avg_health_score": avg_score,
        "counts": counts,
        "headline": f"今日健康度 {avg_score} 分 · {mood}",
        "summary": summary,
    }


def simulate_conditional_forecast(
    latest_close: float,
    ma20: float,
    recent_closes: list[float],
    *,
    market_change_pct: float,
    volume_multiplier: float,
    news_sentiment: str = "neutral",
) -> dict[str, Any]:
    """條件式預測模擬器：根據大盤漲跌、成交量與消息面推估未來 5 日可能價格區間。"""
    # shortcut: MVP 使用線性敏感度 + 歷史波動率區間估算；Phase 3 可替換為統計迴歸或蒙地卡羅模擬
    if latest_close <= 0:
        raise ValueError("目前股價必須大於 0")

    mkt = _clamp(market_change_pct, -15.0, 15.0)
    vol_mult = _clamp(volume_multiplier, 0.2, 5.0)
    sentiment = news_sentiment if news_sentiment in ("positive", "neutral", "negative") else "neutral"

    # 計算歷史日報酬波動度（若樣本不足則以預設 1.8% 估算）
    if len(recent_closes) >= 3:
        returns = [
            (recent_closes[i] - recent_closes[i - 1]) / recent_closes[i - 1]
            for i in range(1, len(recent_closes))
            if recent_closes[i - 1] > 0
        ]
        daily_vol = statistics.pstdev(returns) if len(returns) >= 2 else 0.018
    else:
        daily_vol = 0.018
    daily_vol = _clamp(daily_vol, 0.008, 0.06)

    # 推估預期報酬：大盤帶動 (beta≈0.9) + 消息面影響 + 均線引力微調
    sentiment_shift = {"positive": 0.025, "neutral": 0.0, "negative": -0.028}[sentiment]
    ma_anchor_pull = ((ma20 - latest_close) / latest_close) * 0.15 if ma20 > 0 else 0.0
    volume_amplifier = 1.0 + max(0.0, vol_mult - 1.0) * 0.12

    expected_return = ((mkt / 100.0) * 0.9 + sentiment_shift + ma_anchor_pull) * volume_amplifier
    expected_return = _clamp(expected_return, -0.25, 0.25)

    # 5 日波動區間寬度（成交量放大時區間會更寬）
    band_half_width = daily_vol * (5 ** 0.5) * (0.85 + 0.25 * vol_mult)
    expected_price = round(latest_close * (1.0 + expected_return), 2)
    low_price = max(0.5, round(expected_price * (1.0 - band_half_width), 2))
    high_price = round(expected_price * (1.0 + band_half_width), 2)

    if band_half_width >= 0.08 or expected_return <= -0.05:
        risk_level = "高"
    elif band_half_width >= 0.045 or abs(expected_return) >= 0.03:
        risk_level = "中"
    else:
        risk_level = "低"

    sentiment_zh = {"positive": "利多消息", "neutral": "消息平穩", "negative": "利空消息"}[sentiment]
    plain_explanation = (
        f"在「大盤 {mkt:+.1f}%、成交量 {vol_mult:.1f} 倍、{sentiment_zh}」情境下，"
        f"未來 5 個交易日預估可能落在 {low_price:,.2f} ~ {high_price:,.2f} 元之間"
        f"（中位推估 {expected_price:,.2f} 元，{expected_return * 100:+.1f}%）。"
        f"目前情境波動風險等級為「{risk_level}」，請依此預留安全空間。"
    )

    return {
        "latest_close": round(latest_close, 2),
        "expected_price": expected_price,
        "low_price": low_price,
        "high_price": high_price,
        "expected_return_pct": round(expected_return * 100, 2),
        "risk_level": risk_level,
        "plain_explanation": plain_explanation,
    }
