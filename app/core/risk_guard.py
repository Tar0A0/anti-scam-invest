"""防呆與防上頭風控模組：執行單一標的資金上限、最大虧損提示、二次確認與冷靜鎖定機制。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def _to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _to_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class OrderRequest:
    """下單請求資料結構。"""

    symbol: str
    side: str  # "BUY" 或 "SELL"
    quantity: int
    price: float
    stop_loss_price: float | None = None
    confirmed_by_user: bool = False


@dataclass(frozen=True)
class RiskCheckResult:
    """風控檢查結果，包含是否放行、是否需要二次確認視窗與白話提示訊息。"""

    allowed: bool
    requires_confirmation: bool = False
    locked_until: str | None = None
    max_loss_estimate: float = 0.0
    reasons: list[str] = field(default_factory=list)


class RiskGuard:
    """新手友善防呆風控引擎（Anti-FOMO & Position Sizing）。"""

    def _load_settings(self, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM risk_settings WHERE id = 1;").fetchone()
        if row is None:
            raise RuntimeError("找不到 risk_settings 預設風控設定（請先執行 init_db）")
        return row

    def _get_active_lockout(self, conn: sqlite3.Connection, now_iso: str) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT reason, unlock_at FROM risk_lockouts
            WHERE unlock_at > ?
            ORDER BY unlock_at DESC
            LIMIT 1;
            """,
            (now_iso,),
        ).fetchone()

    def _create_lockout(
        self,
        conn: sqlite3.Connection,
        reason: str,
        now_utc: datetime,
        cooldown_minutes: int,
    ) -> str:
        unlock_iso = _iso(now_utc + timedelta(minutes=cooldown_minutes))
        with conn:
            conn.execute(
                "INSERT INTO risk_lockouts (reason, locked_at, unlock_at) VALUES (?, ?, ?);",
                (reason, _iso(now_utc), unlock_iso),
            )
        return unlock_iso

    def evaluate_order(
        self,
        conn: sqlite3.Connection,
        order: OrderRequest,
        *,
        total_equity: float,
        current_symbol_value: float,
        daily_realized_pnl: float,
        now: datetime | None = None,
    ) -> RiskCheckResult:
        """評估單筆下單是否符合資金控管、冷靜期與二次防呆確認要求。"""
        # 1. 邊界輸入檢查（信任邊界不可省略）
        side = (order.side or "").upper()
        if side not in ("BUY", "SELL"):
            return RiskCheckResult(allowed=False, reasons=["交易方向必須為 BUY（買進）或 SELL（賣出）"])
        if order.quantity <= 0 or order.price <= 0:
            return RiskCheckResult(allowed=False, reasons=["下單數量與價格必須大於 0"])
        if total_equity <= 0 or current_symbol_value < 0:
            return RiskCheckResult(allowed=False, reasons=["帳戶總資金必須大於 0，且持股價值不可為負"])
        if side == "BUY" and order.stop_loss_price is not None:
            if order.stop_loss_price <= 0 or order.stop_loss_price >= order.price:
                return RiskCheckResult(
                    allowed=False,
                    reasons=["買進時的停損價必須大於 0 且低於買進價格，才能有效保護本金"],
                )

        # 2. 估算最大可能虧損金額（提供給二次確認視窗顯示）
        order_notional = order.quantity * order.price
        reasons: list[str] = []
        if side == "BUY":
            if order.stop_loss_price is not None:
                max_loss = (order.price - order.stop_loss_price) * order.quantity
            else:
                max_loss = order_notional
                reasons.append("提醒：您未設定停損價，極端情況下最大可能虧損為整筆投入金額")
        else:
            max_loss = 0.0

        now_utc = _to_utc(now or datetime.now(timezone.utc))
        now_iso = _iso(now_utc)
        settings = self._load_settings(conn)

        # 賣出（含停損/獲利了結）不應被冷靜期擋住，確保隨時能撤退保命
        if side == "BUY":
            # 3. 檢查是否已在交易冷靜鎖定期內
            active_lock = self._get_active_lockout(conn, now_iso)
            if active_lock is not None:
                return RiskCheckResult(
                    allowed=False,
                    locked_until=active_lock["unlock_at"],
                    max_loss_estimate=max_loss,
                    reasons=[
                        f"目前處於防上頭冷靜期（解鎖時間：{active_lock['unlock_at']}），"
                        f"暫停買進以保護本金。原因：{active_lock['reason']}"
                    ],
                )

            # 4A. 防頻繁交易上頭檢查（過去 1 小時內下單次數）
            one_hour_ago_iso = _iso(now_utc - timedelta(hours=1))
            recent_count = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM orders
                WHERE created_at >= ? AND status != 'REJECTED';
                """,
                (one_hour_ago_iso,),
            ).fetchone()["cnt"]

            max_per_hour = int(settings["max_orders_per_hour"])
            cooldown_mins = int(settings["cooldown_minutes"])
            if recent_count >= max_per_hour:
                lock_reason = f"1 小時內已下單 {recent_count} 次，超過安全上限 {max_per_hour} 次，先休息一下避免衝動追價"
                unlock_at = self._create_lockout(conn, lock_reason, now_utc, cooldown_mins)
                return RiskCheckResult(
                    allowed=False,
                    locked_until=unlock_at,
                    max_loss_estimate=max_loss,
                    reasons=[lock_reason],
                )

            # 4B. 當日累計虧損達上限檢查（避免虧損後想翻本加碼）
            max_daily_loss_pct = float(settings["max_daily_loss_pct"])
            if daily_realized_pnl < 0 and abs(daily_realized_pnl) / total_equity >= max_daily_loss_pct:
                loss_pct = abs(daily_realized_pnl) / total_equity
                lock_reason = (
                    f"今日已實現虧損達 {loss_pct:.1%}（觸及 {max_daily_loss_pct:.0%} 防護線），"
                    "強制啟動冷靜期，明天再戰"
                )
                unlock_at = self._create_lockout(conn, lock_reason, now_utc, cooldown_mins)
                return RiskCheckResult(
                    allowed=False,
                    locked_until=unlock_at,
                    max_loss_estimate=max_loss,
                    reasons=[lock_reason],
                )

            # 5. 單一標的最大資金比例檢查（預設不超過總資金 20%）
            max_pos_pct = float(settings["max_single_position_pct"])
            projected_pct = (current_symbol_value + order_notional) / total_equity
            if projected_pct > max_pos_pct:
                return RiskCheckResult(
                    allowed=False,
                    max_loss_estimate=max_loss,
                    reasons=[
                        f"買進後單一標的佔總資金比例將達 {projected_pct:.1%}，"
                        f"超過安全上限 {max_pos_pct:.0%}，建議縮小買進股數以分散風險"
                    ],
                )

        # 6. 下單二次確認防呆視窗（顯示最大可能虧損金額）
        if not order.confirmed_by_user:
            reasons.append(f"請於確認視窗核對：本次操作最大可能虧損估算為 {max_loss:,.0f} 元")
            return RiskCheckResult(
                allowed=False,
                requires_confirmation=True,
                max_loss_estimate=max_loss,
                reasons=reasons,
            )

        return RiskCheckResult(
            allowed=True,
            requires_confirmation=False,
            max_loss_estimate=max_loss,
            reasons=reasons,
        )

    def record_order(
        self,
        conn: sqlite3.Connection,
        order: OrderRequest,
        *,
        status: str = "FILLED",
        order_type: str = "MANUAL",
        max_loss_estimate: float = 0.0,
        reject_reason: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """將下單或風控攔截結果寫入 orders 資料表。"""
        created_at = _iso(now or datetime.now(timezone.utc))
        with conn:
            cur = conn.execute(
                """
                INSERT INTO orders
                    (symbol, side, order_type, quantity, price, max_loss_estimate,
                     confirmed_by_user, status, reject_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    order.symbol.strip(),
                    order.side.upper(),
                    order_type,
                    order.quantity,
                    order.price,
                    max_loss_estimate,
                    1 if order.confirmed_by_user else 0,
                    status,
                    reject_reason,
                    created_at,
                ),
            )
        return int(cur.lastrowid)
