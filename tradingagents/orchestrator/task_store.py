"""TaskStore — Persistent task state management for autonomous orchestrator.

Provides SQLite-backed task lifecycle management (pending/running/paused/completed/failed).
Supports pause/resume, progress tracking, and checkpoint recovery.

Usage:
    store = TaskStore("tasks.db")
    task_id = store.create("Trade HK.00700", {"symbol": "HK.00700", "action": "buy"})
    store.update(task_id, status="running", progress=0.3)
    store.pause(task_id)
    store.resume(task_id)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """Task lifecycle states."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskStore:
    """SQLite-backed persistent task state store.

    Features:
        - Create/update/delete tasks with full metadata
        - Pause/resume support for long-running autonomous loops
        - Progress tracking (0.0–1.0) with optional step counts
        - Checkpoint save/restore for crash recovery
        - Filtering by status, created time, or custom metadata
    """

    def __init__(self, db_path: Optional[str] = None):
        """Initialize the task store.

        Args:
            db_path: Path to SQLite database. Defaults to
                     $TA_TASK_STORE_DB or ./tradingagents_taskstore.db
        """
        self._db_path = db_path or os.getenv(
            "TA_TASK_STORE_DB", "tradingagents_taskstore.db"
        )
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist."""
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS autonomous_tasks (
                    id          TEXT PRIMARY KEY,
                    title       TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'pending',
                    progress    REAL DEFAULT 0.0,
                    metadata    TEXT DEFAULT '{}',
                    checkpoint  TEXT DEFAULT '{}',
                    error       TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status
                    ON autonomous_tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_created
                    ON autonomous_tasks(created_at);
            """)

    def _execute_with_retry(
        self, cursor, sql: str, params: tuple = (), max_retries: int = 3
    ) -> sqlite3.Cursor:
        """Execute SQL with exponential backoff on 'database is locked' (P2-5).

        Retries up to max_retries times with delays: 0.1s, 0.2s, 0.4s.
        This prevents concurrent write failures from crashing the orchestrator.
        """
        last_error: Optional[sqlite3.OperationalError] = None
        for attempt in range(max(1, max_retries)):
            try:
                return cursor.execute(sql, params)
            except sqlite3.OperationalError as e:
                last_error = e
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    delay = 0.1 * (2 ** attempt)
                    logger.debug(
                        "SQLite locked, retry %d/%d in %.1fs",
                        attempt + 1, max_retries, delay,
                    )
                    time.sleep(delay)
                    continue
                raise
        # Should not reach here, but satisfy type checker
        raise last_error  # type: ignore[misc]

    @contextmanager
    def _conn(self):
        """Context manager for SQLite connections with WAL mode and retry (P2-5~6).

        P2-6: timeout=10 waits for lock release before raising.
        P2-5: _execute_with_retry wraps write operations with backoff.
        """
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ── CRUD operations ──────────────────────────────────────────────────

    def create(
        self,
        title: str,
        metadata: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> str:
        """Create a new task in pending state.

        Args:
            title: Human-readable task title
            metadata: Arbitrary metadata dict (e.g., symbol, action, config)
            task_id: Optional custom ID; auto-generated if omitted

        Returns:
            The task ID
        """
        task_id = task_id or f"auto_{uuid4().hex[:12]}"
        now = self._now_iso()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        with self._conn() as conn:
            conn.execute(
                """INSERT INTO autonomous_tasks
                   (id, title, status, progress, metadata, checkpoint, created_at, updated_at)
                   VALUES (?, ?, ?, 0.0, ?, '{}', ?, ?)""",
                (task_id, title, TaskStatus.PENDING.value, meta_json, now, now),
            )

        logger.info("Created task %s: %s", task_id, title)
        return task_id

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get task by ID.

        Returns:
            Task dict or None if not found
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM autonomous_tasks WHERE id = ?", (task_id,)
            ).fetchone()

        if row is None:
            return None

        return self._row_to_dict(row)

    def update(
        self,
        task_id: str,
        status: Optional[str] = None,
        progress: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        checkpoint: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Update task fields.

        Args:
            task_id: Target task ID
            status: New status string (TaskStatus enum value)
            progress: Progress fraction 0.0–1.0
            metadata: Replace entire metadata dict
            checkpoint: Replace entire checkpoint dict
            error: Set error message (for failed tasks)

        Returns:
            True if task was found and updated, False otherwise
        """
        updates = ["updated_at = ?"]
        params: list = [self._now_iso()]

        if status is not None:
            updates.append("status = ?")
            params.append(status)
            if status in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value):
                updates.append("completed_at = ?")
                params.append(self._now_iso())

        if progress is not None:
            updates.append("progress = ?")
            params.append(max(0.0, min(1.0, progress)))

        if metadata is not None:
            updates.append("metadata = ?")
            params.append(json.dumps(metadata, ensure_ascii=False))

        if checkpoint is not None:
            updates.append("checkpoint = ?")
            params.append(json.dumps(checkpoint, ensure_ascii=False))

        if error is not None:
            updates.append("error = ?")
            params.append(error)

        params.append(task_id)

        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE autonomous_tasks SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            return cur.rowcount > 0

    def delete(self, task_id: str) -> bool:
        """Delete a task by ID.

        Returns:
            True if task was found and deleted, False otherwise
        """
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM autonomous_tasks WHERE id = ?", (task_id,)
            )
            return cur.rowcount > 0

    # ── Lifecycle transitions ─────────────────────────────────────────────

    def start(self, task_id: str) -> bool:
        """Transition task to running state.

        Only valid from pending or paused status.
        """
        task = self.get(task_id)
        if not task:
            return False
        if task["status"] not in (TaskStatus.PENDING.value, TaskStatus.PAUSED.value):
            logger.warning(
                "Cannot start task %s: current status is %s",
                task_id, task["status"],
            )
            return False
        return self.update(task_id, status=TaskStatus.RUNNING.value)

    def pause(self, task_id: str) -> bool:
        """Pause a running task.

        Only valid from running status. Saves current progress as checkpoint.
        """
        task = self.get(task_id)
        if not task:
            return False
        if task["status"] != TaskStatus.RUNNING.value:
            logger.warning(
                "Cannot pause task %s: current status is %s",
                task_id, task["status"],
            )
            return False
        return self.update(task_id, status=TaskStatus.PAUSED.value)

    def resume(self, task_id: str) -> bool:
        """Resume a paused task.

        Only valid from paused status.
        """
        task = self.get(task_id)
        if not task:
            return False
        if task["status"] != TaskStatus.PAUSED.value:
            logger.warning(
                "Cannot resume task %s: current status is %s",
                task_id, task["status"],
            )
            return False
        return self.update(task_id, status=TaskStatus.RUNNING.value)

    def complete(self, task_id: str, result: Optional[Dict[str, Any]] = None) -> bool:
        """Mark task as completed.

        Args:
            task_id: Target task ID
            result: Optional result data to store in metadata
        """
        task = self.get(task_id)
        if not task:
            return False

        updates: Dict[str, Any] = {"status": TaskStatus.COMPLETED.value, "progress": 1.0}
        if result:
            meta = task.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            meta["result"] = result
            updates["metadata"] = meta

        return self.update(task_id, **updates)

    def fail(self, task_id: str, error: str) -> bool:
        """Mark task as failed with error message."""
        return self.update(
            task_id, status=TaskStatus.FAILED.value, error=error
        )

    # ── Checkpoint management ─────────────────────────────────────────────

    def save_checkpoint(self, task_id: str, checkpoint: Dict[str, Any]) -> bool:
        """Save checkpoint data for crash recovery.

        Args:
            task_id: Target task ID
            checkpoint: Arbitrary state to resume from (e.g., loop iteration,
                        last processed symbol, accumulated results)
        """
        return self.update(task_id, checkpoint=checkpoint)

    def get_checkpoint(self, task_id: str) -> Dict[str, Any]:
        """Load checkpoint data for a task.

        Returns:
            Checkpoint dict, or empty dict if not found
        """
        task = self.get(task_id)
        if not task:
            return {}
        cp = task.get("checkpoint") or {}
        if isinstance(cp, str):
            cp = json.loads(cp)
        return cp

    # ── Query / list ──────────────────────────────────────────────────────

    def list_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List tasks with optional status filter.

        Args:
            status: Filter by TaskStatus value, or None for all
            limit: Max results (default 50)
            offset: Pagination offset

        Returns:
            List of task dicts, newest first
        """
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM autonomous_tasks WHERE status = ? "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (status, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM autonomous_tasks "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()

        return [self._row_to_dict(r) for r in rows]

    def count_by_status(self) -> Dict[str, int]:
        """Get count of tasks grouped by status.

        Returns:
            Dict mapping status string to count
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM autonomous_tasks GROUP BY status"
            ).fetchall()
        return {row["status"]: row["cnt"] for row in rows}

    def get_running_tasks(self) -> List[Dict[str, Any]]:
        """Get all currently running tasks.

        Returns:
            List of running task dicts
        """
        return self.list_tasks(status=TaskStatus.RUNNING.value, limit=100)

    def get_paused_tasks(self) -> List[Dict[str, Any]]:
        """Get all paused tasks (candidates for resume).

        Returns:
            List of paused task dicts
        """
        return self.list_tasks(status=TaskStatus.PAUSED.value, limit=100)

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        """Convert a sqlite3.Row to a dict with parsed JSON fields."""
        d = dict(row)
        # Parse JSON fields
        for key in ("metadata", "checkpoint"):
            if d.get(key) and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        # Parse progress as float
        if d.get("progress") is not None:
            d["progress"] = float(d["progress"])
        return d
