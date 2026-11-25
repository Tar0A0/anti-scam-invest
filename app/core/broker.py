"""模擬撮合與倉位帳務模組：串接 RiskGuard 防呆檢查、資金扣款、持倉統計與自動停損停利。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.core.risk_guard import OrderRequest, RiskCheckResult, RiskGuard


def _iso_now(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_latest_prices(conn: sqlite3.Connection, *, source: str | None = None) -> dict[str, float]:
    """讀取每檔標的最新一筆已驗證收盤價（指定 source 時僅讀取該資料源）。"""
    if source:
        rows = conn.execute(
            """
            SELECT q.symbol, q.close
            FROM market_quotes q
            INNER JOIN (
                SELECT symbol, MAX(quote_time) AS max_time
                FROM market_quotes
                WHERE source = ?
                GROUP BY symbol
            ) latest
            ON q.symbol = latest.symbol AND q.quote_time = latest.max_time AND q.source = ?;
            """,
            (source, source),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT q.symbol, q.close
            FROM market_quotes q
            INNER JOIN (
                SELECT symbol, MAX(quote_time) AS max_time
                FROM market_quotes
                GROUP BY symbol
            ) latest
            ON q.symbol = latest.symbol AND q.quote_time = latest.max_time;
            """
        ).fetchall()
    return {r["symbol"]: float(r["close"]) for r in rows}


def get_portfolio(
    conn: sqlite3.Connection,
    symbol_names: dict[str, str] | None = None,
    *,
    source: str | None = None,
) -> dict[str, Any]:
    """彙整模擬帳戶總資產、現金、持倉浮動損益、冷靜鎖定狀態與最近交易紀錄。"""
    names = symbol_names or {}
    acct = conn.execute("SELECT * FROM account_state WHERE id = 1;").fetchone()
    if acct is None:
        raise RuntimeError("找不到 account_state（請先執行 init_db）")

    latest_prices = get_latest_prices(conn, source=source)
    pos_rows = conn.execute("SELECT * FROM positions WHERE quantity > 0 ORDER BY symbol ASC;").fetchall()

    positions: list[dict[str, Any]] = []
    positions_value = 0.0
    for row in pos_rows:
        symbol = row["symbol"]
        qty = int(row["quantity"])
        avg_cost = float(row["avg_cost"])
        current_price = latest_prices.get(symbol, avg_cost)
        market_value = round(qty * current_price, 2)
        unrealized_pnl = round((current_price - avg_cost) * qty, 2)
        unrealized_pct = round(((current_price - avg_cost) / avg_cost * 100.0) if avg_cost > 0 else 0.0, 2)
        positions_value += market_value
        positions.append(
            {
                "symbol": symbol,
                "name": names.get(symbol, symbol),
                "quantity": qty,
                "avg_cost": round(avg_cost, 2),
                "current_price": round(current_price, 2),
                "market_value": market_value,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pct": unrealized_pct,
                "stop_loss_price": row["stop_loss_price"],
                "take_profit_price": row["take_profit_price"],
            }
        )

    cash_balance = round(float(acct["cash_balance"]), 2)
    total_equity = round(cash_balance + positions_value, 2)
    initial_cash = round(float(acct["initial_cash"]), 2)

    now_iso = _iso_now()
    lock_row = conn.execute(
        "SELECT reason, unlock_at FROM risk_lockouts WHERE unlock_at > ? ORDER BY unlock_at DESC LIMIT 1;",
        (now_iso,),
    ).fetchone()
    risk_row = conn.execute("SELECT * FROM risk_settings WHERE id = 1;").fetchone()
    order_rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 20;").fetchall()

    return {
        "initial_cash": initial_cash,
        "cash_balance": cash_balance,
        "positions_value": round(positions_value, 2),
        "total_equity": total_equity,
        "total_return_pct": round(((total_equity - initial_cash) / initial_cash * 100.0) if initial_cash > 0 else 0.0, 2),
        "daily_realized_pnl": round(float(acct["daily_realized_pnl"]), 2),
        "active_lockout": dict(lock_row) if lock_row else None,
        "risk_settings": dict(risk_row) if risk_row else {},
        "positions": positions,
        "recent_orders": [dict(r) for r in order_rows],
    }


def _account_metrics_for_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    source: str | None = None,
) -> tuple[float, float, float, float]:
    """取得總資產、可用現金、該標的目前市值、當日已實現損益。"""
    portfolio = get_portfolio(conn, source=source)
    symbol_value = 0.0
    for p in portfolio["positions"]:
        if p["symbol"] == symbol:
            symbol_value = float(p["market_value"])
            break
    return (
        float(portfolio["total_equity"]),
        float(portfolio["cash_balance"]),
        symbol_value,
        float(portfolio["daily_realized_pnl"]),
    )


def preview_order(
    conn: sqlite3.Connection,
    order: OrderRequest,
    *,
    source: str | None = None,
    now: datetime | None = None,
) -> RiskCheckResult:
    """下單前預覽：強制回傳二次確認所需之最大可能虧損金額與風控檢核結果。"""
    total_equity, cash_balance, symbol_value, daily_pnl = _account_metrics_for_symbol(conn, order.symbol, source=source)
    if order.side.upper() == "BUY" and order.quantity * order.price > cash_balance:
        return RiskCheckResult(
            allowed=False,
            reasons=[f"可用模擬現金不足（需要 {order.quantity * order.price:,.0f} 元，僅剩 {cash_balance:,.0f} 元）"],
        )

    preview_req = OrderRequest(
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        price=order.price,
        stop_loss_price=order.stop_loss_price,
        confirmed_by_user=False,
    )
    return RiskGuard().evaluate_order(
        conn,
        preview_req,
        total_equity=total_equity,
        current_symbol_value=symbol_value,
        daily_realized_pnl=daily_pnl,
        now=now,
    )


def execute_order(
    conn: sqlite3.Connection,
    order: OrderRequest,
    *,
    order_type: str = "MANUAL",
    take_profit_price: float | None = None,
    source: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, RiskCheckResult, dict[str, Any] | None]:
    """執行模擬下單：伺服端重跑 RiskGuard 檢核，通過後以單一交易原子更新帳戶與倉位。"""
    guard = RiskGuard()
    total_equity, cash_balance, symbol_value, daily_pnl = _account_metrics_for_symbol(conn, order.symbol, source=source)
    side = (order.side or "").upper()

    if side == "BUY" and take_profit_price is not None and take_profit_price <= order.price:
        return (False, RiskCheckResult(allowed=False, reasons=["停利目標價必須高於買進價格"]), None)

    check = guard.evaluate_order(
        conn,
        order,
        total_equity=total_equity,
        current_symbol_value=symbol_value,
        daily_realized_pnl=daily_pnl,
        now=now,
    )
    if not check.allowed:
        if check.locked_until:
            guard.record_order(
                conn,
                order,
                status="LOCKED",
                order_type=order_type,
                max_loss_estimate=check.max_loss_estimate,
                reject_reason="；".join(check.reasons),
                now=now,
            )
        elif not check.requires_confirmation:
            guard.record_order(
                conn,
                order,
                status="REJECTED",
                order_type=order_type,
                max_loss_estimate=check.max_loss_estimate,
                reject_reason="；".join(check.reasons),
                now=now,
            )
        return (False, check, None)

    now_iso = _iso_now(now)
    notional = round(order.quantity * order.price, 2)

    with conn:
        pos = conn.execute("SELECT * FROM positions WHERE symbol = ?;", (order.symbol,)).fetchone()
        if side == "BUY":
            if notional > cash_balance:
                return (
                    False,
                    RiskCheckResult(allowed=False, reasons=[f"可用模擬現金不足（尚缺 {notional - cash_balance:,.0f} 元）"]),
                    None,
                )
            new_cash = round(cash_balance - notional, 2)
            old_qty = int(pos["quantity"]) if pos else 0
            old_cost = float(pos["avg_cost"]) if pos else 0.0
            new_qty = old_qty + order.quantity
            new_avg = round(((old_qty * old_cost) + notional) / new_qty, 4)
            sl_price = order.stop_loss_price if order.stop_loss_price is not None else (pos["stop_loss_price"] if pos else None)
            tp_price = take_profit_price if take_profit_price is not None else (pos["take_profit_price"] if pos else None)

            conn.execute(
                """
                INSERT INTO positions (symbol, quantity, avg_cost, stop_loss_price, take_profit_price, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    quantity = excluded.quantity,
                    avg_cost = excluded.avg_cost,
                    stop_loss_price = excluded.stop_loss_price,
                    take_profit_price = excluded.take_profit_price,
                    updated_at = excluded.updated_at;
                """,
                (order.symbol, new_qty, new_avg, sl_price, tp_price, now_iso),
            )
            conn.execute(
                "UPDATE account_state SET cash_balance = ?, updated_at = ? WHERE id = 1;",
                (new_cash, now_iso),
            )
        else:
            if pos is None or int(pos["quantity"]) < order.quantity:
                held = int(pos["quantity"]) if pos else 0
                return (
                    False,
                    RiskCheckResult(allowed=False, reasons=[f"目前僅持有 {held} 股，不足賣出 {order.quantity} 股"]),
                    None,
                )
            avg_cost = float(pos["avg_cost"])
            realized = round((order.price - avg_cost) * order.quantity, 2)
            new_cash = round(cash_balance + notional, 2)
            new_daily_pnl = round(daily_pnl + realized, 2)
            remaining = int(pos["quantity"]) - order.quantity
            if remaining == 0:
                conn.execute("DELETE FROM positions WHERE symbol = ?;", (order.symbol,))
            else:
                conn.execute(
                    "UPDATE positions SET quantity = ?, updated_at = ? WHERE symbol = ?;",
                    (remaining, now_iso, order.symbol),
                )
            conn.execute(
                "UPDATE account_state SET cash_balance = ?, daily_realized_pnl = ?, updated_at = ? WHERE id = 1;",
                (new_cash, new_daily_pnl, now_iso),
            )

        cur = conn.execute(
            """
            INSERT INTO orders
                (symbol, side, order_type, quantity, price, max_loss_estimate,
                 confirmed_by_user, status, reject_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 'FILLED', NULL, ?);
            """,
            (order.symbol, side, order_type, order.quantity, order.price, check.max_loss_estimate, now_iso),
        )
        order_id = int(cur.lastrowid)

    return (
        True,
        check,
        {
            "order_id": order_id,
            "symbol": order.symbol,
            "side": side,
            "order_type": order_type,
            "quantity": order.quantity,
            "price": order.price,
        },
    )


def check_stop_triggers(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """掃描所有持倉是否觸發自動停損或停利條件，觸發時自動賣出並記錄原因。"""
    latest_prices = get_latest_prices(conn, source=source)
    pos_rows = conn.execute("SELECT * FROM positions WHERE quantity > 0;").fetchall()
    triggered: list[dict[str, Any]] = []

    for row in pos_rows:
        symbol = row["symbol"]
        price = latest_prices.get(symbol)
        if price is None:
            continue

        sl = row["stop_loss_price"]
        tp = row["take_profit_price"]
        trigger_type: str | None = None
        if sl is not None and price <= float(sl):
            trigger_type = "STOP_LOSS"
        elif tp is not None and price >= float(tp):
            trigger_type = "TAKE_PROFIT"

        if trigger_type is None:
            continue

        req = OrderRequest(
            symbol=symbol,
            side="SELL",
            quantity=int(row["quantity"]),
            price=float(price),
            confirmed_by_user=True,
        )
        ok, _, filled = execute_order(conn, req, order_type=trigger_type, source=source, now=now)
        if ok and filled:
            triggered.append(filled)

    return triggered
