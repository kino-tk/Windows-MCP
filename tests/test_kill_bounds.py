"""Every wait on a child process tree must be bounded.

A tool call that cannot tear down its child must still return; otherwise the
MCP host's request timeout fires and the server keeps a stuck worker thread.
"""

import subprocess
import sys
import time

import psutil
import pytest

from windows_mcp.powershell import utils

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")

# A child that ignores CTRL_BREAK and would otherwise live for 30 s.
_STUBBORN = [
    sys.executable,
    "-c",
    "import signal, time; signal.signal(signal.SIGBREAK, signal.SIG_IGN); time.sleep(30)",
]


def _reap(pid: int) -> None:
    try:
        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            child.kill()
        proc.kill()
    except psutil.NoSuchProcess:
        pass


def _python_children() -> set[int]:
    me = psutil.Process()
    return {c.pid for c in me.children(recursive=True)}


def test_kill_process_tree_is_bounded_when_taskkill_hangs(monkeypatch):
    # Stand in for a taskkill that never finishes. Extra arguments (the PID)
    # are ignored by ``python -c``.
    monkeypatch.setattr(
        utils, "_TASKKILL_CMD", [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    started = time.monotonic()
    assert utils.kill_process_tree(12345, timeout=1.0) is False
    assert time.monotonic() - started < 8


def test_kill_process_tree_kills_a_real_tree():
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time;"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
            "time.sleep(30)",
        ],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        deadline = time.monotonic() + 10
        while not psutil.Process(parent.pid).children() and time.monotonic() < deadline:
            time.sleep(0.1)
        child_pids = [c.pid for c in psutil.Process(parent.pid).children()]
        assert child_pids, "grandchild did not start"
        assert utils.kill_process_tree(parent.pid) is True
        parent.wait(timeout=5)
        for pid in child_pids:
            assert not utils.check_pid_exists(pid)
    finally:
        _reap(parent.pid)


def test_timeout_returns_even_if_the_tree_cannot_be_killed(monkeypatch):
    """The original code ended in ``Popen.__exit__``'s unbounded wait()."""
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if args and "taskkill" in str(args[0]).lower():
            return subprocess.CompletedProcess(args, 0)  # taskkill that achieves nothing
        return real_run(args, *a, **kw)

    monkeypatch.setattr(utils.subprocess, "run", fake_run)
    before = _python_children()
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        utils.run_with_graceful_timeout(
            _STUBBORN, capture_output=True, timeout=1, grace_period=0.5
        )
    elapsed = time.monotonic() - started
    for pid in _python_children() - before:
        _reap(pid)
    assert elapsed < 10, f"call held for {elapsed:.1f}s"


def test_terminate_gracefully_reports_survivor(monkeypatch):
    monkeypatch.setattr(utils, "kill_process_tree", lambda pid, timeout=None: False)
    proc = subprocess.Popen(_STUBBORN, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        time.sleep(0.5)
        started = time.monotonic()
        note = utils.terminate_gracefully(proc, grace_period=0.5)
        assert time.monotonic() - started < 5
        assert "could not be killed" in note
    finally:
        _reap(proc.pid)


def test_terminate_gracefully_kills_stubborn_child():
    proc = subprocess.Popen(_STUBBORN, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        time.sleep(0.5)
        note = utils.terminate_gracefully(proc, grace_period=0.5)
        assert "killed" in note
        assert proc.poll() is not None
    finally:
        _reap(proc.pid)


def test_normal_command_output_unchanged():
    result = utils.run_with_graceful_timeout(
        [sys.executable, "-c", "print('hello')"], capture_output=True, timeout=30
    )
    assert result.returncode == 0
    assert result.stdout.strip() == b"hello"
