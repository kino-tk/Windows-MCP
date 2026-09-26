"""Append-only log of tool calls, for diagnosing calls that never return.

The MCP host's own log records that a ``tools/call`` was sent and when its
response arrived, but not which tool ran or with what arguments. When a call
hangs inside the server there is then nothing to go on. This log fills that
gap: every call writes a ``start`` record before the tool runs and an ``end``
record when it actually finishes, so a call stuck inside the server shows up
as a ``start`` with no matching ``end``. If the host gives up first, a
``cancelled`` record is written at that moment and the ``end`` record still
follows once the work really stops.

Records are JSON lines in ``~/.windows-mcp/calls.log``. The file rotates by
size (1 MB by default) and keeps 3 older generations, so it never grows past
about 4 MB. Each record is written by opening, appending and closing the file,
so no handle is held open between calls. On Windows a rename fails while any
process has the file open; if another server process is mid-write when this
one wants to rotate, rotation is skipped and retried on the next write.

Logging never raises into a tool call.

Environment variables:
    WINDOWS_MCP_CALLLOG             Path of the log file, or ``off`` to disable.
    WINDOWS_MCP_CALLLOG_MAX_BYTES   Rotation threshold in bytes (default 1000000).
    WINDOWS_MCP_CALLLOG_BACKUPS     Older generations to keep (default 3).
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["record", "new_call_id", "summarize_args", "log_path"]

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_BACKUPS = 3
DETAIL_CHARS = 200

# Arguments that may carry free text the user typed or wrote (clipboard, typed
# text, file contents). Only their length is logged.
_LENGTH_ONLY_KEYS = frozenset({"content", "text", "value", "data", "input"})
_DISABLED = frozenset({"", "0", "off", "false", "no", "none"})

_lock = threading.Lock()
_seq = itertools.count(1)


def log_path() -> Path | None:
    """Return the log file path, or None when logging is disabled."""
    configured = os.environ.get("WINDOWS_MCP_CALLLOG")
    if configured is not None:
        if configured.strip().lower() in _DISABLED:
            return None
        return Path(configured).expanduser()
    return Path("~/.windows-mcp").expanduser() / "calls.log"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def new_call_id() -> str:
    """A per-process unique id; the PID keeps ids apart across restarts."""
    return f"{os.getpid()}-{next(_seq)}"


def summarize_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Reduce tool arguments to something safe and short enough to log."""
    out: dict[str, Any] = {}
    for key, value in kwargs.items():
        if value is None or key == "ctx":
            continue
        if isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, str):
            if key in _LENGTH_ONLY_KEYS:
                out[key] = f"<{len(value)} chars>"
            elif len(value) > DETAIL_CHARS:
                out[key] = value[:DETAIL_CHARS] + "…"
            else:
                out[key] = value
        elif isinstance(value, list | tuple):
            out[key] = f"<{type(value).__name__} of {len(value)}>"
        else:
            out[key] = f"<{type(value).__name__}>"
    return out


def _rotate_if_needed(path: Path, incoming: int) -> None:
    max_bytes = _env_int("WINDOWS_MCP_CALLLOG_MAX_BYTES", DEFAULT_MAX_BYTES)
    backups = _env_int("WINDOWS_MCP_CALLLOG_BACKUPS", DEFAULT_BACKUPS)
    if max_bytes <= 0:
        return
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size + incoming <= max_bytes:
        return
    try:
        if backups <= 0:
            path.unlink()
            return
        for i in range(backups - 1, 0, -1):
            older = path.with_name(f"{path.name}.{i}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{i + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        # Another process has one of the files open. Keep appending to the
        # current file and try again on the next write.
        logger.debug("Call log rotation skipped", exc_info=True)


def record(event: str, call_id: str, tool: str, **fields: Any) -> None:
    """Append one record. Never raises."""
    try:
        path = log_path()
        if path is None:
            return
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "event": event,
            "id": call_id,
            "tool": tool,
            **fields,
        }
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        data = line.encode("utf-8")
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(path, len(data))
            with open(path, "ab") as fh:
                fh.write(data)
    except Exception:
        logger.debug("Call log write failed", exc_info=True)
