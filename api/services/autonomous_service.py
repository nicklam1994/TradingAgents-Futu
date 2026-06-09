"""Autonomous Service — API service layer for autonomous orchestrator.

Provides the business logic for the /v1/autonomous/* API endpoints.
Manages task lifecycle, status queries, and orchestrator integration.

Usage:
    service = AutonomousService()
    task_id = service.create_task("2w美金閉環模擬交易 HK.00700")
    status = service.get_status(task_id)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from tradingagents.orchestrator.task_store import TaskStore, TaskStatus
from tradingagents.orchestrator.autonomous_loop import (
    AutonomousLoop,
    AutonomousTaskConfig,
)

logger = logging.getLogger(__name__)

# Singleton instances
_store: Optional[TaskStore] = None
_loop: Optional[AutonomousLoop] = None


def _get_store() -> TaskStore:
    """Get or create the singleton TaskStore."""
    global _store
    if _store is None:
        db_path = os.getenv("TA_TASK_STORE_DB", "tradingagents_taskstore.db")
        _store = TaskStore(db_path=db_path)
    return _store


def create_task(
    command: str,
    budget: Optional[float] = None,
    currency: str = "USD",
    mode: str = "simulate",
    max_iterations: int = 30,
    fixed_symbols: Optional[List[str]] = None,
    llm_api_key: Optional[str] = None,
    llm_provider: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Create and start a new autonomous trading task.

    Args:
        command: Natural language trading command
        budget: Total capital (overrides parsed value)
        currency: Currency code
        mode: "simulate" or "live" (only simulate supported)
        max_iterations: Max OODA loop iterations
        fixed_symbols: Optional fixed stock pool
        llm_api_key: User's LLM API key (from DB)
        llm_provider: LLM provider name
        llm_base_url: LLM base URL
        llm_model: LLM model name

    Returns:
        Dict with task_id, status, and initial configuration
    """
    # Reset singleton so it picks up new LLM config
    global _loop
    _loop = None

    loop = AutonomousLoop(
        task_store=_get_store(),
        llm_api_key=llm_api_key,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
    )

    config = AutonomousTaskConfig(
        command=command,
        budget=budget or 10000.0,
        currency=currency,
        mode=mode,
        max_iterations=max_iterations,
        fixed_symbols=fixed_symbols or [],
    )

    task_id = loop.start(command, config)

    return {
        "task_id": task_id,
        "status": "running",
        "command": command,
        "budget": config.budget,
        "currency": config.currency,
        "mode": config.mode,
        "max_iterations": config.max_iterations,
    }


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Get full task details including status and checkpoint.

    Returns:
        Task dict or None if not found
    """
    store = _get_store()
    task = store.get(task_id)
    if not task:
        return None

    # Enrich with loop status
    global _loop
    if _loop is None:
        _loop = AutonomousLoop(task_store=_get_store())
    loop = _loop
    loop_status = loop.get_status(task_id)

    return {
        "task_id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "progress": task["progress"],
        "metadata": task.get("metadata"),
        "checkpoint": task.get("checkpoint"),
        "error": task.get("error"),
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "completed_at": task.get("completed_at"),
        "loop_status": loop_status,
    }


def pause_task(task_id: str) -> Dict[str, Any]:
    """Pause a running autonomous task."""
    global _loop
    if _loop is None:
        _loop = AutonomousLoop(task_store=_get_store())
    success = _loop.pause(task_id)
    if not success:
        return {"error": "Task not found or not in running state"}
    return {"task_id": task_id, "status": "paused", "message": "Task paused successfully"}


def resume_task(task_id: str) -> Dict[str, Any]:
    """Resume a paused autonomous task."""
    global _loop
    if _loop is None:
        _loop = AutonomousLoop(task_store=_get_store())
    success = _loop.resume(task_id)
    if not success:
        return {"error": "Task not found or not in paused state"}
    return {"task_id": task_id, "status": "running", "message": "Task resumed successfully"}


def stop_task(task_id: str) -> Dict[str, Any]:
    """Stop an autonomous task permanently."""
    global _loop
    if _loop is None:
        _loop = AutonomousLoop(task_store=_get_store())
    success = _loop.stop(task_id)
    if not success:
        return {"error": "Task not found"}
    return {"task_id": task_id, "status": "completed", "message": "Task stopped by user"}


def list_tasks(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """List autonomous tasks with optional status filter."""
    store = _get_store()
    tasks = store.list_tasks(status=status, limit=limit, offset=offset)
    counts = store.count_by_status()
    return {
        "tasks": [
            {
                "task_id": t["id"],
                "title": t["title"],
                "status": t["status"],
                "progress": t["progress"],
                "created_at": t["created_at"],
                "updated_at": t["updated_at"],
            }
            for t in tasks
        ],
        "total": sum(counts.values()),
        "counts": counts,
    }


def get_task_alerts(task_id: str) -> List[Dict[str, Any]]:
    """Get position alerts for a specific task."""
    store = _get_store()
    task = store.get(task_id)
    if not task:
        return []
    checkpoint = task.get("checkpoint") or {}
    if isinstance(checkpoint, str):
        checkpoint = json.loads(checkpoint)
    state = checkpoint.get("state", {})
    return state.get("position_alerts", [])
