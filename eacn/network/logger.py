"""Network-side global event logger with @log_event annotation middleware.

Design principles:
- Append-only event store
- @log_event is non-blocking: logging failures don't block main function
- Records task lifecycle state changes and platform-routed communications
- Access restricted to network operators
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from eacn.core.models import LogEntry

_logger = logging.getLogger(__name__)


class GlobalLogger:
    """Records global task lifecycle events from all servers."""

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []

    def record(self, entry: LogEntry) -> None:
        self._entries.append(entry)

    def get_entries(
        self,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        server_id: str | None = None,
        fn_name: str | None = None,
    ) -> list[LogEntry]:
        """Query entries with optional filters (all AND-combined)."""
        result = self._entries
        if task_id:
            result = [e for e in result if e.task_id == task_id]
        if agent_id:
            result = [e for e in result if e.agent_id == agent_id]
        if server_id:
            result = [e for e in result if e.server_id == server_id]
        if fn_name:
            result = [e for e in result if e.fn_name == fn_name]
        return result

    def get_agent_events(self, agent_id: str) -> list[LogEntry]:
        """All events involving an agent (for reputation calculation)."""
        return [e for e in self._entries if e.agent_id == agent_id]

    def get_task_timeline(self, task_id: str) -> list[LogEntry]:
        """Chronological event timeline for a task."""
        entries = [e for e in self._entries if e.task_id == task_id]
        entries.sort(key=lambda e: e.timestamp)
        return entries

    @property
    def size(self) -> int:
        return len(self._entries)


def log_event(
    logger: GlobalLogger,
    *,
    task_id_param: str | None = None,
    agent_id_param: str | None = None,
    server_id_param: str | None = None,
) -> Callable:
    """Annotation-based event middleware.

    Usage:
        @log_event(logger, task_id_param="task_id", agent_id_param="agent_id")
        async def submit_bid(task_id: str, agent_id: str, ...):
            ...

    The decorator inspects function kwargs to extract task_id, agent_id, etc.
    Logging failures are swallowed (non-blocking sidecar).
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Extract context IDs from kwargs
            extracted_task_id = kwargs.get(task_id_param) if task_id_param else None
            extracted_agent_id = kwargs.get(agent_id_param) if agent_id_param else None
            extracted_server_id = kwargs.get(server_id_param) if server_id_param else None

            timestamp = datetime.now(timezone.utc).isoformat()
            error_msg: str | None = None
            result = None

            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                try:
                    entry = LogEntry(
                        fn_name=func.__name__,
                        args=_safe_serialize_kwargs(kwargs),
                        result=_safe_serialize(result),
                        timestamp=timestamp,
                        error=error_msg,
                        task_id=extracted_task_id,
                        agent_id=extracted_agent_id,
                        server_id=extracted_server_id,
                    )
                    logger.record(entry)
                except Exception:
                    _logger.warning(
                        "Failed to log event for %s", func.__name__, exc_info=True
                    )

            return result

        return wrapper

    return decorator


def _safe_serialize_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Best-effort serialization of kwargs for logging."""
    result = {}
    for k, v in kwargs.items():
        result[k] = _safe_serialize(v)
    return result


def _safe_serialize(value: Any) -> Any:
    """Convert value to JSON-safe form."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return str(value)
    except Exception:
        return str(value)
