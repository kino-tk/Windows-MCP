import asyncio
import json
import time

import pytest

from windows_mcp.infrastructure import calllog
from windows_mcp.infrastructure.analytics import with_analytics


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    path = tmp_path / "calls.log"
    monkeypatch.setenv("WINDOWS_MCP_CALLLOG", str(path))
    monkeypatch.delenv("WINDOWS_MCP_CALLLOG_MAX_BYTES", raising=False)
    monkeypatch.delenv("WINDOWS_MCP_CALLLOG_BACKUPS", raising=False)
    return path


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_record_writes_json_lines_with_japanese(log_file):
    calllog.record("start", "1-1", "PowerShell", args={"command": "echo 日本語"})
    calllog.record("end", "1-1", "PowerShell", status="ok")
    recs = _records(log_file)
    assert [r["event"] for r in recs] == ["start", "end"]
    assert recs[0]["args"]["command"] == "echo 日本語"
    assert recs[0]["ts"].endswith("+09:00") or "T" in recs[0]["ts"]


def test_summarize_args_truncates_and_hides_free_text():
    long_cmd = "x" * 500
    out = calllog.summarize_args(
        {"command": long_cmd, "content": "secret text", "text": "typed", "ctx": object(),
         "timeout": 30, "flag": True, "paths": ["a", "b"], "none": None}
    )
    assert out["command"] == "x" * calllog.DETAIL_CHARS + "…"
    assert out["content"] == "<11 chars>"
    assert out["text"] == "<5 chars>"
    assert "ctx" not in out and "none" not in out
    assert out["timeout"] == 30 and out["flag"] is True
    assert out["paths"] == "<list of 2>"


def test_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_CALLLOG", "off")
    monkeypatch.chdir(tmp_path)
    calllog.record("start", "1-1", "X")
    assert calllog.log_path() is None
    assert list(tmp_path.iterdir()) == []


def test_rotation_caps_total_size(log_file, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_CALLLOG_MAX_BYTES", "600")
    monkeypatch.setenv("WINDOWS_MCP_CALLLOG_BACKUPS", "2")
    for i in range(80):
        calllog.record("start", f"1-{i}", "PowerShell", args={"command": "a" * 60})
    names = sorted(p.name for p in log_file.parent.iterdir())
    assert names == ["calls.log", "calls.log.1", "calls.log.2"]
    for p in log_file.parent.iterdir():
        assert p.stat().st_size <= 600
    # Newest record is in the live file, and nothing was lost between .1 and live.
    live = _records(log_file)
    assert live[-1]["id"] == "1-79"
    older = _records(log_file.with_name("calls.log.1"))
    assert int(older[-1]["id"].split("-")[1]) + 1 == int(live[0]["id"].split("-")[1])


def test_rotation_skipped_while_another_handle_is_open(log_file, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_CALLLOG_MAX_BYTES", "300")
    monkeypatch.setenv("WINDOWS_MCP_CALLLOG_BACKUPS", "50")  # keep everything
    for i in range(5):
        calllog.record("start", f"1-{i}", "X", args={"command": "b" * 60})
    holder = open(log_file, "rb")  # blocks rename on Windows
    try:
        for i in range(5, 10):
            calllog.record("start", f"1-{i}", "X", args={"command": "b" * 60})
        # Rotation was due but could not move the held file: it kept growing.
        assert log_file.stat().st_size > 300
        held_ids = [r["id"] for r in _records(log_file)]
        assert held_ids[-5:] == [f"1-{i}" for i in range(5, 10)]
    finally:
        holder.close()
    # Released: the next write rotates, and no record was lost along the way.
    calllog.record("start", "1-10", "X", args={"command": "b" * 60})
    ids = []
    for p in sorted(log_file.parent.iterdir()):
        ids += [r["id"] for r in _records(p)]
    assert sorted(ids, key=lambda s: int(s.split("-")[1])) == [f"1-{i}" for i in range(11)]
    assert log_file.stat().st_size <= 300


def test_record_never_raises(monkeypatch, tmp_path):
    # A directory where the file should be: open() fails.
    target = tmp_path / "calls.log"
    target.mkdir()
    monkeypatch.setenv("WINDOWS_MCP_CALLLOG", str(target))
    calllog.record("start", "1-1", "X")  # must not raise


def test_wrapper_logs_start_and_end_for_sync_tool(log_file):
    @with_analytics(None, "PowerShell-Tool")
    def tool(command: str, timeout: int = 30):
        return "Response: ok\nStatus Code: 0"

    assert asyncio.run(tool(command="Get-Date", timeout=5)).endswith("Status Code: 0")
    start, end = _records(log_file)
    assert start["event"] == "start" and start["args"] == {"command": "Get-Date", "timeout": 5}
    assert end["event"] == "end" and end["id"] == start["id"]
    assert end["status"] == "ok" and end["exit"] == "0"


def test_wrapper_logs_error(log_file):
    @with_analytics(None, "Boom")
    def tool():
        raise ValueError("x")

    with pytest.raises(ValueError):
        asyncio.run(tool())
    assert _records(log_file)[-1]["status"] == "error:ValueError"


def test_cancelled_sync_call_logs_cancel_then_real_end(log_file):
    @with_analytics(None, "Slow")
    def tool():
        time.sleep(1.0)
        return "done"

    async def main():
        task = asyncio.create_task(tool())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.5)  # let the worker thread finish

    asyncio.run(main())
    events = [r["event"] for r in _records(log_file)]
    assert events == ["start", "cancelled", "end"]
    end = _records(log_file)[-1]
    assert end["duration_ms"] >= 900


def test_async_tool_logged(log_file):
    @with_analytics(None, "Async")
    async def tool(x: int):
        return f"Status Code: {x}"

    asyncio.run(tool(x=3))
    assert [r["event"] for r in _records(log_file)] == ["start", "end"]
    assert _records(log_file)[-1]["exit"] == "3"
