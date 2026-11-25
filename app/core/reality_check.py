"""真實市場對比回測模組（Reality Check）：驗證歷史策略建議與後續實際走勢之吻合度。"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.core.strategy import analyze_quotes


def evaluate_strategy_accuracy(
    conn: sqlite3.Connection,
    symbol_names: dict[str, str],
    *,
    source: str = "yahoo",
    horizon_days: int = 5,
) -> dict[str, Any]:
    """用歷史滾動視窗對比策略訊號與未來 N 日真實漲跌，輸出準確率與微調建議。"""
    if horizon_days < 1:
        raise ValueError("horizon_days 必須大於 0")

    per_symbol: list[dict[str, Any]] = []
    total_samples = 0
    directional_signals = 0
    directional_hits = 0
    signal_returns: list[float] = []
    abs_moves: list[float] = []

    for symbol, name in symbol_names.items():
        rows = conn.execute(
            """
            SELECT quote_time, close, volume
            FROM market_quotes
            WHERE symbol = ? AND source = ?
            ORDER BY quote_time ASC;
            """,
            (symbol, source),
        ).fetchall()

        if len(rows) <= 20 + horizon_days:
            continue

        sym_dir = 0
        sym_hits = 0
        sym_rets: list[float] = []

        for idx in range(20, len(rows) - horizon_days):
            curr_close = float(rows[idx]["close"])
            fut_close = float(rows[idx + horizon_days]["close"])
            if curr_close <= 0:
                continue

            fwd_ret_pct = (fut_close - curr_close) / curr_close * 100.0
            abs_moves.append(abs(fwd_ret_pct))
            total_samples += 1

            sig = analyze_quotes(symbol, name, rows[: idx + 1])
            action = sig["action"]
            if action in ("HEAVY_BUY", "ADD"):
                directional_signals += 1
                sym_dir += 1
                signal_returns.append(fwd_ret_pct)
                sym_rets.append(fwd_ret_pct)
                if fwd_ret_pct > 0:
                    directional_hits += 1
                    sym_hits += 1
            elif action == "SELL":
                directional_signals += 1
                sym_dir += 1
                aligned_ret = -fwd_ret_pct
                signal_returns.append(aligned_ret)
                sym_rets.append(aligned_ret)
                if fwd_ret_pct < 0:
                    directional_hits += 1
                    sym_hits += 1

        sym_acc = round((sym_hits / sym_dir * 100.0), 1) if sym_dir > 0 else None
        sym_avg_ret = round((sum(sym_rets) / len(sym_rets)), 2) if sym_rets else None
        per_symbol.append(
            {
                "symbol": symbol,
                "name": name,
                "directional_signals": sym_dir,
                "accuracy_pct": sym_acc,
                "avg_signal_return_pct": sym_avg_ret,
            }
        )

    accuracy_pct = round((directional_hits / directional_signals * 100.0), 1) if directional_signals > 0 else 0.0
    avg_signal_return_pct = round((sum(signal_returns) / len(signal_returns)), 2) if signal_returns else 0.0
    mae_pct = round((sum(abs_moves) / len(abs_moves)), 2) if abs_moves else 0.0

    if directional_signals == 0:
        calibration_hint = "近期歷史資料多為「繼續蹲」觀望訊號，方向性樣本尚不足，建議先維持保守門檻。"
    elif accuracy_pct >= 60.0 and avg_signal_return_pct > 0:
        calibration_hint = f"近 {horizon_days} 日方向命中率 {accuracy_pct}% 且平均訊號正報酬，目前均線與量能門檻表現穩健。"
    elif accuracy_pct >= 45.0:
        calibration_hint = f"命中率 {accuracy_pct}% 接近震盪盤水準，建議將「重壓」的量能門檻微調提高（例如 >= 1.3 倍）過濾假突破。"
    else:
        calibration_hint = f"命中率僅 {accuracy_pct}%（平均波動 {mae_pct}%），近期市場反覆洗盤，建議縮小單筆投入比例並嚴格設停損。"

    return {
        "source": source,
        "horizon_days": horizon_days,
        "total_samples": total_samples,
        "directional_signals": directional_signals,
        "accuracy_pct": accuracy_pct,
        "avg_signal_return_pct": avg_signal_return_pct,
        "mae_pct": mae_pct,
        "calibration_hint": calibration_hint,
        "per_symbol": per_symbol,
    }
