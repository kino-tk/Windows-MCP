"""PowerShell commands that outlive one tool call are handed over as jobs."""

import asyncio
import os
import re
import sys
import time

import psutil
import pytest

from windows_mcp.powershell import PowerShellExecutor, jobs
from windows_mcp.powershell.service import job_finished_output

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")

PY = sys.executable.replace("'", "''")


@pytest.fixture(autouse=True)
def job_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_JOB_RETURN_AFTER", "2")
    monkeypatch.setenv("WINDOWS_MCP_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("WINDOWS_MCP_CALLLOG", str(tmp_path / "calls.log"))
    yield tmp_path / "jobs"
    for job in list(jobs._jobs.values()):
        if not job.done.is_set():
            jobs.kill(job)
        jobs.forget(job)


def _py(code: str) -> str:
    """A PowerShell command line running *code* with this interpreter, unbuffered."""
    return f"& '{PY}' -u -c \"{code}\""


def test_short_timeout_is_plain_execute_command(monkeypatch):
    calls = []
    real = PowerShellExecutor.execute_command

    def spy(command, timeout=10, shell=None):
        calls.append(timeout)
        return real(command, timeout, shell)

    monkeypatch.setattr(PowerShellExecutor, "execute_command", staticmethod(spy))
    out, status, job_id = PowerShellExecutor.run("'plain'", timeout=2)
    assert (out.strip(), status, job_id) == ("plain", 0, None)
    assert calls == [2]


def test_fast_command_in_job_path_matches_normal_output(job_env):
    cmd = "'hello'; 'こんにちは'; Write-Output ('x' * 3)"
    expected, expected_status = PowerShellExecutor.execute_command(cmd, 30)
    out, status, job_id = PowerShellExecutor.run(cmd, timeout=600)
    assert job_id is None
    assert (out, status) == (expected, expected_status)
    assert list(job_env.iterdir()) == []  # finished job's files were removed


def test_exit_code_and_stderr_match_normal_output():
    cmd = "Write-Error 'boom'; exit 3"
    expected = PowerShellExecutor.execute_command(cmd, 30)
    out, status, job_id = PowerShellExecutor.run(cmd, timeout=600)
    assert job_id is None
    assert (out, status) == expected
    assert status == 3


def test_long_command_returns_job_with_partial_output():
    cmd = _py("import time\nfor i in range(8):\n    print('line', i); time.sleep(0.5)")
    started = time.monotonic()
    out, status, job_id = PowerShellExecutor.run(cmd, timeout=600)
    assert time.monotonic() - started < 6
    assert status is None and job_id and job_id.startswith("job-")
    assert "line 0" in out and "PowerShellJob" in out
    job = jobs.get(job_id)
    assert job.done.wait(15)
    final, code = job_finished_output(job)
    assert code == 0 and job.state == "exited"
    assert [ln for ln in final.splitlines() if ln.startswith("line")] == [f"line {i}" for i in range(8)]


def test_reading_while_running_does_not_disturb_output():
    cmd = _py("import time\nfor i in range(400):\n    print(f'{i:05d}', 'abcdefghij' * 5)\n    time.sleep(0.005)")
    out, status, job_id = PowerShellExecutor.run(cmd, timeout=600)
    job = jobs.get(job_id)
    assert job is not None
    while not job.done.is_set():
        jobs.read_output(job, 200)
        jobs.read_output(job)
    final, code = job_finished_output(job)
    lines = final.splitlines()
    assert code == 0
    assert lines == [f"{i:05d} " + "abcdefghij" * 5 for i in range(400)]


def test_hard_timeout_stops_the_job():
    cmd = _py("import time; time.sleep(60)")
    out, status, job_id = PowerShellExecutor.run(cmd, timeout=4)
    assert job_id is not None
    job = jobs.get(job_id)
    assert job.done.wait(20)
    assert job.state == "timed_out"
    assert not psutil.pid_exists(job.pid) or psutil.Process(job.pid).status() == psutil.STATUS_ZOMBIE
    text, code = job_finished_output(job)
    assert code == 1 and "hard timeout of 4s" in text


def test_kill_stops_the_whole_tree():
    cmd = _py(
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('child', c.pid, flush=True)\n"
        "time.sleep(60)"
    )
    out, status, job_id = PowerShellExecutor.run(cmd, timeout=600)
    job = jobs.get(job_id)
    deadline = time.monotonic() + 10
    child_pid = None
    while child_pid is None and time.monotonic() < deadline:
        m = re.search(r"child (\d+)", jobs.read_output(job)[0])
        child_pid = int(m.group(1)) if m else None
        time.sleep(0.2)
    assert child_pid and psutil.pid_exists(child_pid)
    jobs.kill(job)
    assert job.done.is_set() and job.state == "killed"
    time.sleep(0.5)
    assert not psutil.pid_exists(child_pid)


def test_job_start_and_end_are_logged(job_env):
    import json

    cmd = _py("import time; time.sleep(3); print('done')")
    _, _, job_id = PowerShellExecutor.run(cmd, timeout=600)
    assert jobs.get(job_id).done.wait(15)
    log = job_env.parent / "calls.log"
    recs = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    mine = [r for r in recs if r["id"] == job_id]
    assert [r["event"] for r in mine] == ["job_start", "job_end"]
    assert mine[1]["state"] == "exited" and mine[1]["returncode"] == 0


def test_stale_files_from_old_runs_are_swept(job_env):
    job_env.mkdir(parents=True, exist_ok=True)
    old = job_env / "999-job-1.out"
    old.write_text("x")
    two_days = time.time() - 2 * 86400
    os.utime(old, (two_days, two_days))
    fresh = job_env / "999-job-2.out"
    fresh.write_text("y")
    jobs._stale_swept = False
    PowerShellExecutor.run("'x'", timeout=600)
    assert not old.exists()
    assert fresh.exists()


def _text(result) -> str:
    return result.content[0].text


def test_tools_end_to_end():
    from fastmcp import FastMCP
    from windows_mcp.tools.shell import register

    mcp = FastMCP(name="t")
    register(mcp, get_desktop=lambda: None, get_analytics=lambda: None)

    async def scenario():
        quick = _text(await mcp.call_tool("PowerShell", {"command": "'quick'"}))
        assert quick.startswith("Response: quick") and quick.endswith("Status Code: 0")

        # PowerShell folds a native command's failure into exit 1 unless the
        # script passes $LASTEXITCODE on (same as without jobs).
        cmd = _py("import time\nprint('begin')\ntime.sleep(4)\nprint('end')\nraise SystemExit(5)") + "; exit $LASTEXITCODE"
        first = _text(await mcp.call_tool("PowerShell", {"command": cmd, "timeout": 600}))
        assert first.endswith("Status Code: running")
        job_id = re.search(r'job_id="(job-\d+)"', first).group(1)

        listed = _text(await mcp.call_tool("PowerShellJob", {"action": "list"}))
        assert job_id in listed and "running" in listed

        status = _text(await mcp.call_tool("PowerShellJob", {"job_id": job_id, "action": "status"}))
        assert status.endswith("Status Code: running") and "begin" in status

        waited = _text(await mcp.call_tool("PowerShellJob", {"job_id": job_id, "wait_seconds": 2}))
        if waited.endswith("Status Code: running"):
            waited = _text(await mcp.call_tool("PowerShellJob", {"job_id": job_id, "wait_seconds": 2}))
        assert f"{job_id} finished: exited, exit 5" in waited
        assert "begin" in waited and "end" in waited
        assert waited.endswith("Status Code: 5")

        unknown = _text(await mcp.call_tool("PowerShellJob", {"job_id": "job-99999"}))
        assert "Unknown job_id" in unknown and unknown.endswith("Status Code: 1")

        cmd2 = _py("import time; time.sleep(60)")
        second = _text(await mcp.call_tool("PowerShell", {"command": cmd2, "timeout": 600}))
        job2 = re.search(r'job_id="(job-\d+)"', second).group(1)
        killed = _text(await mcp.call_tool("PowerShellJob", {"job_id": job2, "action": "kill"}))
        assert f"{job2} finished: killed" in killed

    asyncio.run(scenario())


def test_normal_call_is_not_blocked_by_a_running_job():
    from fastmcp import FastMCP
    from windows_mcp.tools.shell import register

    mcp = FastMCP(name="t2")
    register(mcp, get_desktop=lambda: None, get_analytics=lambda: None)

    async def scenario():
        long = asyncio.create_task(
            mcp.call_tool("PowerShell", {"command": _py("import time; time.sleep(1.5); print('slow')"), "timeout": 600})
        )
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        quick = _text(await mcp.call_tool("PowerShell", {"command": "'fast'"}))
        quick_elapsed = time.monotonic() - t0
        slow = _text(await long)
        return quick, quick_elapsed, slow

    quick, quick_elapsed, slow = asyncio.run(scenario())
    assert "fast" in quick and quick_elapsed < 1.5
    assert "slow" in slow and slow.endswith("Status Code: 0")
