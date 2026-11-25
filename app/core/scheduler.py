"""定時排程與執行日誌模組：支援自訂時段、失敗重試、完整稽核紀錄與一鍵手動重跑。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.db.database import get_connection

JobCallable = Callable[[], int | tuple[str, int, str | None]]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_cron_expr(expr: str) -> None:
    """驗證標準 5 欄 Cron 表達式（分 時 日 月 週），避免錯誤設定寫入資料庫。"""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Cron 表達式必須剛好包含 5 個欄位（分 時 日 月 週），收到：{expr!r}")


@dataclass(frozen=True)
class JobRunResult:
    """單次排程執行的結果摘要。"""

    job_name: str
    trigger_type: str
    status: str
    retry_count: int
    records_processed: int
    duration_ms: int
    error_message: str | None = None


class TaskScheduler:
    """排程管理器：整合 SQLite 排程設定、執行日誌寫入與 APScheduler 背景排程。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._jobs: dict[str, JobCallable] = {}
        self._bg_scheduler: Any = None

    def register_job(
        self,
        job_name: str,
        func: JobCallable,
        *,
        job_type: str,
        cron_expr: str,
        max_retries: int = 2,
        description: str = "",
    ) -> None:
        """註冊任務函式並確保其在 schedule_configs 中有對應設定列。"""
        _validate_cron_expr(cron_expr)
        if max_retries < 0:
            raise ValueError("max_retries 不可小於 0")

        self._jobs[job_name] = func
        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO schedule_configs
                        (job_name, job_type, cron_expr, enabled, max_retries, description)
                    VALUES (?, ?, ?, 1, ?, ?);
                    """,
                    (job_name, job_type, cron_expr.strip(), max_retries, description),
                )
        finally:
            conn.close()

    def update_schedule(
        self,
        job_name: str,
        *,
        cron_expr: str | None = None,
        enabled: bool | None = None,
        max_retries: int | None = None,
    ) -> None:
        """更新使用者自訂的排程時間、啟用狀態或重試次數。"""
        updates: list[str] = []
        params: list[Any] = []

        if cron_expr is not None:
            _validate_cron_expr(cron_expr)
            updates.append("cron_expr = ?")
            params.append(cron_expr.strip())
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
        if max_retries is not None:
            if max_retries < 0:
                raise ValueError("max_retries 不可小於 0")
            updates.append("max_retries = ?")
            params.append(max_retries)

        if not updates:
            return

        updates.append("updated_at = ?")
        params.extend([_utc_now_iso(), job_name])

        conn = get_connection(self.db_path)
        try:
            with conn:
                cur = conn.execute(
                    f"UPDATE schedule_configs SET {', '.join(updates)} WHERE job_name = ?;",
                    params,
                )
                if cur.rowcount == 0:
                    raise KeyError(f"找不到排程任務：{job_name}")
        finally:
            conn.close()

    def run_job(
        self,
        job_name: str,
        trigger_type: str = "scheduled",
        *,
        retry_delay_sec: float = 0.0,
    ) -> JobRunResult:
        """執行指定任務，支援自動重試並將執行結果寫入 job_logs。"""
        if job_name not in self._jobs:
            raise KeyError(f"任務尚未註冊：{job_name}")
        if trigger_type not in ("scheduled", "manual"):
            raise ValueError("trigger_type 必須為 'scheduled' 或 'manual'")

        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT max_retries FROM schedule_configs WHERE job_name = ?;",
                (job_name,),
            ).fetchone()
            max_retries = int(row["max_retries"]) if row else 0
        finally:
            conn.close()

        started_at = _utc_now_iso()
        t0 = time.perf_counter()
        retry_count = 0
        status = "FAILED"
        records_processed = 0
        error_message: str | None = None

        for attempt in range(max_retries + 1):
            try:
                raw_result = self._jobs[job_name]()
                if isinstance(raw_result, tuple):
                    status, records_processed, error_message = raw_result
                    if status not in ("SUCCESS", "FAILED", "PARTIAL"):
                        raise ValueError(f"無效的任務回傳狀態：{status}")
                else:
                    status = "SUCCESS"
                    records_processed = int(raw_result)
                    error_message = None
                break
            except Exception as exc:  # noqa: BLE001 - 捕捉任務執行期任何異常以記錄日誌與重試
                error_message = f"{type(exc).__name__}: {exc}"
                if attempt < max_retries:
                    retry_count += 1
                    if retry_delay_sec > 0:
                        time.sleep(retry_delay_sec)
                else:
                    status = "FAILED"

        duration_ms = max(0, int((time.perf_counter() - t0) * 1000))
        finished_at = _utc_now_iso()

        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO job_logs
                        (job_name, trigger_type, started_at, finished_at, duration_ms,
                         status, retry_count, records_processed, error_message)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        job_name,
                        trigger_type,
                        started_at,
                        finished_at,
                        duration_ms,
                        status,
                        retry_count,
                        records_processed,
                        error_message,
                    ),
                )
        finally:
            conn.close()

        return JobRunResult(
            job_name=job_name,
            trigger_type=trigger_type,
            status=status,
            retry_count=retry_count,
            records_processed=records_processed,
            duration_ms=duration_ms,
            error_message=error_message,
        )

    def rerun_job(self, job_name: str) -> JobRunResult:
        """提供給儀表板的「一鍵手動重跑」入口。"""
        return self.run_job(job_name, trigger_type="manual")

    def get_recent_logs(self, limit: int = 20) -> list[dict[str, Any]]:
        """查詢最近的排程執行日誌，供前端監控儀表板呈現。"""
        conn = get_connection(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT id, job_name, trigger_type, started_at, finished_at,
                       duration_ms, status, retry_count, records_processed, error_message
                FROM job_logs
                ORDER BY id DESC
                LIMIT ?;
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def start_background(self) -> None:
        """啟動 APScheduler 背景排程器，依 schedule_configs 載入啟用中的任務。"""
        # shortcut: 使用單機記憶體內 BackgroundScheduler；若未來部署多 Worker 需改用分散式鎖或 Celery
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        if self._bg_scheduler is not None and self._bg_scheduler.running:
            return

        scheduler = BackgroundScheduler(timezone="Asia/Taipei")
        conn = get_connection(self.db_path)
        try:
            configs = conn.execute(
                "SELECT job_name, cron_expr FROM schedule_configs WHERE enabled = 1;"
            ).fetchall()
        finally:
            conn.close()

        for cfg in configs:
            job_name = cfg["job_name"]
            if job_name in self._jobs:
                scheduler.add_job(
                    self.run_job,
                    trigger=CronTrigger.from_crontab(cfg["cron_expr"]),
                    args=[job_name, "scheduled"],
                    id=job_name,
                    replace_existing=True,
                )

        scheduler.start()
        self._bg_scheduler = scheduler

    def stop_background(self) -> None:
        """安全關閉背景排程器。"""
        if self._bg_scheduler is not None and self._bg_scheduler.running:
            self._bg_scheduler.shutdown(wait=False)
            self._bg_scheduler = None
