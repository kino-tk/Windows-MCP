"""Background jobs for PowerShell commands that outlive one tool call.

MCP hosts give up on a tool call after a fixed time (Claude Desktop: 240 s)
and a server cannot extend that. A command given a ``timeout`` longer than
``return_after()`` therefore runs as a job: the tool call waits up to
``return_after()`` seconds, and if the command is still running it returns a
job id plus the output so far, while the command keeps running. The caller
then waits, polls or kills it with the PowerShellJob tool. The command is only
stopped when its own ``timeout`` (the hard limit the caller chose) expires.

Output goes to files under ``~/.windows-mcp/jobs`` rather than to pipes, so it
can be read while the command runs and no reader thread can block on a
descendant that keeps a handle open. Reads use their own handle, so they never
move the child's write position.

Jobs live in this server process. If the server restarts, the registry is lost
but the processes keep running until they finish; their files are deleted
after 24 hours.
"""

from __future__ import annotations

import itertools
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from windows_mcp.infrastructure import calllog
from windows_mcp.powershell.utils import terminate_gracefully

__all__ = ["Job", "start", "get", "list_jobs", "kill", "read_output", "forget", "return_after"]

logger = logging.getLogger(__name__)

DEFAULT_RETURN_AFTER = 200.0
# Stay clear of the host's 240 s limit, leaving time for the reply to travel.
MAX_RETURN_AFTER = 225.0
RETENTION_SECONDS = 6 * 3600
STALE_FILE_SECONDS = 24 * 3600

_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_seq = itertools.count(1)
_stale_swept = False


def return_after() -> float:
    """Seconds a tool call waits before handing a command over as a job."""
    try:
        value = float(os.environ.get("WINDOWS_MCP_JOB_RETURN_AFTER", DEFAULT_RETURN_AFTER))
    except ValueError:
        value = DEFAULT_RETURN_AFTER
    return max(1.0, min(value, MAX_RETURN_AFTER))


def job_dir() -> Path:
    configured = os.environ.get("WINDOWS_MCP_JOB_DIR")
    return Path(configured).expanduser() if configured else Path("~/.windows-mcp/jobs").expanduser()


@dataclass
class Job:
    id: str
    command: str
    process: subprocess.Popen
    out_path: Path
    err_path: Path
    started: float
    hard_timeout: float
    state: str = "running"  # running | exited | timed_out | killed
    returncode: int | None = None
    ended: float | None = None
    note: str = ""
    kill_requested: bool = False
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def pid(self) -> int:
        return self.process.pid

    def elapsed(self) -> float:
        return (self.ended or time.time()) - self.started

    def deadline_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.started + self.hard_timeout))


def start(args: list[str], *, env: dict[str, str], cwd: str, hard_timeout: float, command: str) -> Job:
    """Launch *args* as a job. The caller waits on ``job.done`` as it likes."""
    _cleanup()
    directory = job_dir()
    directory.mkdir(parents=True, exist_ok=True)
    job_id = f"job-{next(_seq)}"
    stem = f"{os.getpid()}-{job_id}"
    out_path = directory / f"{stem}.out"
    err_path = directory / f"{stem}.err"
    try:
        # Our handles are closed right after the spawn; the child keeps its own.
        with open(out_path, "wb") as out_fh, open(err_path, "wb") as err_fh:
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=out_fh,
                stderr=err_fh,
                cwd=cwd,
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
    except BaseException:
        for path in (out_path, err_path):
            path.unlink(missing_ok=True)
        raise

    job = Job(
        id=job_id,
        command=command,
        process=process,
        out_path=out_path,
        err_path=err_path,
        started=time.time(),
        hard_timeout=float(hard_timeout),
    )
    with _lock:
        _jobs[job_id] = job
    calllog.record(
        "job_start",
        job_id,
        "PowerShell",
        pid=process.pid,
        hard_timeout=job.hard_timeout,
        command=calllog.summarize_args({"command": command}).get("command", ""),
    )
    threading.Thread(target=_watch, args=(job,), name=f"windows-mcp-{job_id}", daemon=True).start()
    return job


def _watch(job: Job) -> None:
    remaining = job.hard_timeout - (time.time() - job.started)
    state = "exited"
    try:
        job.process.wait(timeout=max(0.0, remaining))
    except subprocess.TimeoutExpired:
        if not job.kill_requested:
            job.note = terminate_gracefully(job.process)
            state = "timed_out"
    except Exception:
        logger.debug("Job watcher for %s failed", job.id, exc_info=True)
    if job.kill_requested:
        state = "killed"
    # If the tree survived a timeout or kill, returncode stays None; report
    # that rather than waiting any longer.
    _finish(job, state)


def _finish(job: Job, state: str) -> None:
    with _lock:
        if job.done.is_set():
            return
        job.returncode = job.process.poll()
        job.ended = time.time()
        job.state = state
        job.done.set()
    calllog.record(
        "job_end",
        job.id,
        "PowerShell",
        state=state,
        returncode=job.returncode,
        duration_ms=int(job.elapsed() * 1000),
        **({"note": job.note} if job.note else {}),
    )


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def list_jobs() -> list[Job]:
    _cleanup()
    with _lock:
        return list(_jobs.values())


def kill(job: Job, wait: float = 5.0) -> None:
    """Stop the job's process tree. Bounded; returns even if it survives."""
    if job.done.is_set():
        return
    job.kill_requested = True
    job.note = terminate_gracefully(job.process)
    if not job.done.wait(wait) and job.process.poll() is not None:
        _finish(job, "killed")


def _read_tail(path: Path, tail_chars: int | None) -> str:
    try:
        with open(path, "rb") as fh:
            if tail_chars is not None:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                # UTF-8 is at most 4 bytes per character.
                fh.seek(max(0, size - tail_chars * 4))
            data = fh.read()
    except FileNotFoundError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if tail_chars is not None and len(text) > tail_chars:
        text = text[-tail_chars:]
    return text


def read_output(job: Job, tail_chars: int | None = None) -> tuple[str, str]:
    """Return (stdout, stderr), whole or only the last *tail_chars* of each."""
    return _read_tail(job.out_path, tail_chars), _read_tail(job.err_path, tail_chars)


def forget(job: Job) -> None:
    """Drop a finished job and delete its files."""
    if not job.done.is_set():
        return
    with _lock:
        _jobs.pop(job.id, None)
    for path in (job.out_path, job.err_path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not delete %s", path, exc_info=True)


def _cleanup() -> None:
    global _stale_swept
    now = time.time()
    with _lock:
        expired = [j for j in _jobs.values() if j.ended is not None and now - j.ended > RETENTION_SECONDS]
    for job in expired:
        forget(job)
    if _stale_swept:
        return
    _stale_swept = True
    directory = job_dir()
    if not directory.is_dir():
        return
    with _lock:
        live = {p for j in _jobs.values() for p in (j.out_path, j.err_path)}
    for path in directory.iterdir():
        try:
            if path not in live and now - path.stat().st_mtime > STALE_FILE_SECONDS:
                path.unlink()
        except OSError:
            logger.debug("Could not remove stale job file %s", path, exc_info=True)
