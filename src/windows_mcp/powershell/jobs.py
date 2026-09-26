"""Background jobs for PowerShell commands that outlive one tool call.

MCP hosts give up on a tool call after a fixed time (Claude Desktop: 240 s)
and a server cannot extend that. A command given a ``timeout`` longer than
``return_after()`` therefore runs as a job: the tool call waits up to
``return_after()`` seconds, and if the command is still running it returns a
job id plus the output so far, while the command keeps running. The caller
then waits, polls or kills it with the PowerShellJob tool. The command is
stopped when its own ``timeout`` (the hard limit the caller chose) expires.

Lifetime guarantees
-------------------
Every job's process tree is bound to this server with a Windows Job Object
created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. The command is started
suspended, put into the Job Object, then resumed, so no descendant can escape
before binding. Consequences:

* ``kill`` and the hard timeout end the whole tree, at any depth, including
  descendants whose parent has already exited (``taskkill /T`` cannot find
  those).
* If the server exits for any reason, including a crash or being killed, the
  OS closes the Job Object handle and every process of every job ends with
  it. A job can never outlive its server unsupervised.
* When the command of a job that was handed back as a job ends, the Job
  Object is kept: anything it left running stays bound to the job (``list``
  shows it as leftover), ends on ``kill``, and ends when the job is deleted or
  the server exits. This holds however the command ended, including when its
  root process was killed from outside.
* A command that finishes within its first call is released instead, so a
  process it deliberately left running (``Start-Process``) survives, the same
  as with a plain call.

Records
-------
Each job has three files in ``job_dir()`` (``~/.windows-mcp/jobs``), named
``<server pid>-<job id>``: ``.out`` and ``.err`` hold the output, and ``.json``
holds the job's metadata (command head, PIDs with their creation times, hard
timeout, state, exit code, owning server). The metadata is rewritten
atomically whenever the state changes.

On start, ``reconcile()`` reads the metadata left by servers that are no
longer running. A job still marked running is checked: if its process is
alive (same PID *and* same creation time, so a reused PID is never touched)
it is killed with its tree. Either way the job is adopted as finished, so
``list`` and ``status`` still show it and its output. Jobs owned by another
live server are left alone.

Cleanup
-------
A finished job is kept for ``retention_seconds()`` (6 h by default,
``WINDOWS_MCP_JOB_RETENTION_HOURS``) after it ended, then its three files are
deleted. A sweeper thread started with the server checks every
``sweep_interval()`` seconds (90 min by default,
``WINDOWS_MCP_JOB_SWEEP_INTERVAL``), so a server left running for days still
cleans up on schedule; server start, any job start and any PowerShellJob call
also sweep. Output files without metadata (left by older versions) are
deleted once they are older than ``ORPHAN_FILE_GRACE_SECONDS``.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from windows_mcp.infrastructure import calllog
from windows_mcp.powershell.utils import kill_process_tree

try:  # pywin32 is a dependency; keep the module importable without it.
    import win32job
except ImportError:  # pragma: no cover
    win32job = None

__all__ = [
    "Job",
    "start",
    "get",
    "list_jobs",
    "kill",
    "read_output",
    "forget",
    "return_after",
    "reconcile",
    "shutdown",
    "sweep",
    "start_sweeper",
    "stop_sweeper",
]

logger = logging.getLogger(__name__)

DEFAULT_RETURN_AFTER = 200.0
# Stay clear of the host's 240 s limit, leaving time for the reply to travel.
MAX_RETURN_AFTER = 225.0
RETENTION_SECONDS = 6 * 3600
DEFAULT_SWEEP_INTERVAL = 5400.0  # 90 min
MIN_SWEEP_INTERVAL = 0.1
ORPHAN_FILE_GRACE_SECONDS = 600
GRACE_SECONDS = 2.0
META_VERSION = 1
_CREATE_SUSPENDED = 0x00000004
# Only files with exactly these names are ever read as records or deleted, so
# WINDOWS_MCP_JOB_DIR pointing at a shared folder cannot cost anyone a file.
_JOB_RECORD_RE = re.compile(r"^\d+-job-\d+\.json$")
_JOB_FILE_RE = re.compile(r"^\d+-job-\d+(?:\.out|\.err|\.json\.tmp)$")

_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_next_number = 1
_server_identity: tuple[int, float] | None = None
_sweeper: threading.Thread | None = None
_sweeper_stop = threading.Event()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def retention_seconds() -> float:
    """How long a finished job is kept (WINDOWS_MCP_JOB_RETENTION_HOURS)."""
    hours = _env_float("WINDOWS_MCP_JOB_RETENTION_HOURS", RETENTION_SECONDS / 3600)
    return max(0.0, hours * 3600)


def sweep_interval() -> float:
    """Seconds between background sweeps (WINDOWS_MCP_JOB_SWEEP_INTERVAL)."""
    return max(MIN_SWEEP_INTERVAL, _env_float("WINDOWS_MCP_JOB_SWEEP_INTERVAL", DEFAULT_SWEEP_INTERVAL))


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


def _identity() -> tuple[int, float]:
    global _server_identity
    if _server_identity is None:
        pid = os.getpid()
        _server_identity = (pid, psutil.Process(pid).create_time())
    return _server_identity


def _create_time(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


def _same_process_alive(pid: int | None, created: float | None) -> bool:
    """True only if *pid* is running and was created at *created*."""
    if not pid or created is None:
        return False
    actual = _create_time(pid)
    return actual is not None and abs(actual - created) < 0.01


@dataclass
class Job:
    id: str
    command: str
    pid: int
    pid_created: float | None
    out_path: Path
    err_path: Path
    meta_path: Path
    started: float
    hard_timeout: float
    process: subprocess.Popen | None = None  # None for a job adopted from an earlier server
    job_object: Any = None  # held while the tree is bound to this server
    state: str = "running"  # running | exited | timed_out | killed
    returncode: int | None = None
    ended: float | None = None
    note: str = ""
    adopted: bool = False
    kill_requested: bool = False
    done: threading.Event = field(default_factory=threading.Event)

    def elapsed(self) -> float:
        return (self.ended or time.time()) - self.started

    def deadline_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.started + self.hard_timeout))


# --- metadata -----------------------------------------------------------------


def _write_meta(job: Job) -> None:
    server_pid, server_created = _identity()
    data = {
        "version": META_VERSION,
        "id": job.id,
        "server_pid": server_pid,
        "server_created": server_created,
        "pid": job.pid,
        "pid_created": job.pid_created,
        "command": job.command,
        "hard_timeout": job.hard_timeout,
        "started": job.started,
        "state": job.state,
        "returncode": job.returncode,
        "ended": job.ended,
        "note": job.note,
        "job_object": job.job_object is not None,
        "adopted": job.adopted,
        "out": job.out_path.name,
        "err": job.err_path.name,
    }
    tmp = job.meta_path.with_name(job.meta_path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, job.meta_path)
    except OSError:
        logger.warning("Could not write job metadata %s", job.meta_path, exc_info=True)


# --- Job Object ---------------------------------------------------------------


def _bind_to_job_object(process: subprocess.Popen):
    """Put *process* in a new kill-on-close Job Object. Returns the handle or None."""
    if win32job is None:
        return None
    handle = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation, info)
    win32job.AssignProcessToJobObject(handle, int(process._handle))
    return handle


def _release_job_object(job: Job) -> None:
    """Unbind without killing: clear kill-on-close, then close the handle."""
    handle, job.job_object = job.job_object, None
    if handle is None:
        return
    try:
        info = win32job.QueryInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] &= ~win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation, info)
    except Exception:
        logger.warning("Could not clear kill-on-close for %s", job.id, exc_info=True)
    try:
        handle.Close()
    except Exception:
        logger.debug("Closing Job Object for %s failed", job.id, exc_info=True)


def _close_job_object(job: Job) -> None:
    """Close the handle as is; with kill-on-close set this ends any survivors."""
    handle, job.job_object = job.job_object, None
    if handle is not None:
        try:
            handle.Close()
        except Exception:
            logger.debug("Closing Job Object for %s failed", job.id, exc_info=True)


def _leftovers(job: Job) -> int:
    """Processes still alive in the job's Job Object (0 if it has none)."""
    if job.job_object is None:
        return 0
    try:
        info = win32job.QueryInformationJobObject(job.job_object, win32job.JobObjectBasicAccountingInformation)
        return int(info["ActiveProcesses"])
    except Exception:
        logger.debug("Could not query Job Object for %s", job.id, exc_info=True)
        return 0


def leftovers(job: Job) -> int:
    """Processes the finished command left running that are still bound to the job."""
    return _leftovers(job) if job.done.is_set() else 0


def release(job: Job) -> None:
    """Let processes a finished command left running go free (plain-call behaviour)."""
    if job.done.is_set():
        _release_job_object(job)


def _resume(process: subprocess.Popen) -> None:
    status = ctypes.windll.ntdll.NtResumeProcess(ctypes.c_void_p(int(process._handle)))
    if status != 0:
        raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08X}")


def _stop_tree(job: Job, grace: float = GRACE_SECONDS) -> str:
    """CTRL_BREAK, then end the whole tree. Every wait is bounded."""
    process = job.process
    graceful = False
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT)
    except Exception:
        logger.debug("CTRL_BREAK to %s failed", job.id, exc_info=True)
    try:
        process.wait(timeout=grace)
        graceful = True
    except subprocess.TimeoutExpired:
        pass
    # Whether or not the root obeyed, end whatever is left of the tree.
    if job.job_object is not None:
        try:
            win32job.TerminateJobObject(job.job_object, 1)
        except Exception:
            logger.warning("TerminateJobObject for %s failed", job.id, exc_info=True)
    elif not graceful:
        kill_process_tree(job.pid)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return "The process could not be stopped and may still be running."
    if graceful:
        return "The command exited on CTRL_BREAK; any remaining child processes were ended."
    return f"The command ignored CTRL_BREAK for {grace:.0f}s; its whole process tree was ended."


# --- lifecycle ----------------------------------------------------------------


def start(args: list[str], *, env: dict[str, str], cwd: str, hard_timeout: float, command: str) -> Job:
    """Launch *args* as a job. The caller waits on ``job.done`` as it likes."""
    global _next_number
    sweep()
    directory = job_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with _lock:
        number = _next_number
        _next_number += 1
    job_id = f"job-{number}"
    stem = f"{os.getpid()}-{job_id}"
    out_path = directory / f"{stem}.out"
    err_path = directory / f"{stem}.err"
    meta_path = directory / f"{stem}.json"
    process = None
    job_object = None
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
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED,
            )
        try:
            job_object = _bind_to_job_object(process)
        except Exception:
            logger.warning("Could not bind %s to a Job Object; falling back to taskkill", job_id, exc_info=True)
            job_object = None
        _resume(process)
    except BaseException:
        if process is not None:
            try:
                process.kill()
            except Exception:
                pass
        if job_object is not None:
            try:
                job_object.Close()
            except Exception:
                pass
        for path in (out_path, err_path, meta_path):
            path.unlink(missing_ok=True)
        raise

    job = Job(
        id=job_id,
        command=calllog.summarize_args({"command": command}).get("command", ""),
        pid=process.pid,
        pid_created=_create_time(process.pid),
        out_path=out_path,
        err_path=err_path,
        meta_path=meta_path,
        started=time.time(),
        hard_timeout=float(hard_timeout),
        process=process,
        job_object=job_object,
    )
    _write_meta(job)
    with _lock:
        _jobs[job_id] = job
    calllog.record(
        "job_start",
        job_id,
        "PowerShell",
        pid=process.pid,
        hard_timeout=job.hard_timeout,
        job_object=job_object is not None,
        command=job.command,
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
            job.note = _stop_tree(job)
            state = "timed_out"
    except Exception:
        logger.debug("Job watcher for %s failed", job.id, exc_info=True)
    if job.kill_requested:
        state = "killed"
    _finish(job, state)


def _finish(job: Job, state: str) -> None:
    with _lock:
        if job.done.is_set():
            return
        job.returncode = job.process.poll() if job.process is not None else None
        job.ended = time.time()
        job.state = state
    if state != "exited":
        # The tree was already ended; closing the handle ends any straggler.
        _close_job_object(job)
    # For "exited" the Job Object is kept: anything the command left running
    # stays bound to the job until it is killed, deleted, or the server exits.
    # (A job that finished within its first call is released by the caller,
    # which then behaves exactly like a plain call.)
    _write_meta(job)
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
    sweep()
    with _lock:
        return sorted(_jobs.values(), key=lambda j: j.started)


def kill(job: Job, wait: float = 5.0, reason: str = "") -> None:
    """Stop the job's whole process tree. Bounded; returns even if it survives.

    For a finished job, this ends any processes the command left running.
    """
    if job.done.is_set():
        count = _leftovers(job)
        if count and job.job_object is not None:
            try:
                win32job.TerminateJobObject(job.job_object, 1)
                job.note = (job.note + f" {count} leftover process(es) were ended by kill.").strip()
            except Exception:
                logger.warning("Could not end leftovers of %s", job.id, exc_info=True)
            _close_job_object(job)
            _write_meta(job)
        return
    if job.process is None:
        return
    job.kill_requested = True
    note = _stop_tree(job)
    job.note = f"{reason} {note}".strip()
    if not job.done.wait(wait) and job.process.poll() is not None:
        _finish(job, "killed")


def shutdown() -> None:
    """Stop every running job and the sweeper; called when the server shuts down normally."""
    stop_sweeper()
    with _lock:
        running = [j for j in _jobs.values() if not j.done.is_set() and j.process is not None]
        finished = [j for j in _jobs.values() if j.done.is_set() and j.job_object is not None]
    for job in running:
        kill(job, wait=3.0, reason="Stopped because the Windows-MCP server shut down.")
    for job in finished:
        _close_job_object(job)  # ends leftovers, as the OS would on exit


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
    """Drop a finished job and delete its output and metadata.

    If the job still holds its Job Object, closing it ends anything the
    command left running. Call ``release`` first to let such processes go.
    """
    if not job.done.is_set():
        return
    _close_job_object(job)
    with _lock:
        _jobs.pop(job.id, None)
    for path in (job.out_path, job.err_path, job.meta_path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not delete %s", path, exc_info=True)


def sweep() -> None:
    """Delete finished jobs past the retention period, and stray old files."""
    now = time.time()
    keep = retention_seconds()
    with _lock:
        expired = [j for j in _jobs.values() if j.ended is not None and now - j.ended > keep]
    for job in expired:
        forget(job)
    _remove_stray_files(now)


def _remove_stray_files(now: float) -> int:
    """Delete old job output files that no job record claims. Returns how many.

    Only names of the form ``<digits>-job-<digits>.out|.err|.json.tmp`` are
    considered; any other file in the directory is never touched.
    """
    directory = job_dir()
    if not directory.is_dir():
        return 0
    try:
        names = {p.name for p in directory.iterdir()}
    except OSError:
        return 0
    claimed = set()
    for name in names:
        if _JOB_RECORD_RE.match(name):
            stem = name[: -len(".json")]
            claimed.update({f"{stem}.out", f"{stem}.err"})
    removed = 0
    for name in names - claimed:
        if not _JOB_FILE_RE.match(name):
            continue
        path = directory / name
        try:
            if path.is_file() and now - path.stat().st_mtime > ORPHAN_FILE_GRACE_SECONDS:
                path.unlink()
                removed += 1
        except OSError:
            logger.debug("Could not remove stray job file %s", path, exc_info=True)
    return removed


def _sweeper_loop() -> None:
    while not _sweeper_stop.wait(sweep_interval()):
        try:
            sweep()
        except Exception:
            logger.warning("Background job sweep failed", exc_info=True)


def start_sweeper() -> None:
    """Start the background sweeper thread (idempotent)."""
    global _sweeper
    with _lock:
        if _sweeper is not None and _sweeper.is_alive():
            return
        _sweeper_stop.clear()
        _sweeper = threading.Thread(target=_sweeper_loop, name="windows-mcp-job-sweeper", daemon=True)
        _sweeper.start()


def stop_sweeper(timeout: float = 5.0) -> None:
    global _sweeper
    _sweeper_stop.set()
    thread, _sweeper = _sweeper, None
    if thread is not None:
        thread.join(timeout)


# --- server start -------------------------------------------------------------


def _kill_orphan(pid: int, created: float) -> bool:
    """Kill a leftover tree, but only if *pid* is still the same process."""
    if not _same_process_alive(pid, created):
        return False
    try:
        root = psutil.Process(pid)
        procs = root.children(recursive=True) + [root]
    except psutil.Error:
        return False
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)
    return True


def reconcile() -> dict[str, int]:
    """Clean up after servers that are gone; call once at server start.

    Returns counts for logging and tests.
    """
    global _next_number
    counts = {"adopted": 0, "killed": 0, "foreign_live": 0, "stray_files_removed": 0}
    directory = job_dir()
    if not directory.is_dir():
        return counts
    now = time.time()
    me = _identity()
    highest = 0

    for meta_path in sorted(p for p in directory.glob("*.json") if _JOB_RECORD_RE.match(p.name)):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            number = int(str(data["id"]).rsplit("-", 1)[1])
        except Exception:
            logger.warning("Unreadable job metadata %s", meta_path, exc_info=True)
            continue
        highest = max(highest, number)
        stem = meta_path.name[: -len(".json")]
        owner = (data.get("server_pid"), data.get("server_created"))
        if owner != me and _same_process_alive(*owner):
            counts["foreign_live"] += 1  # another server is running it; not ours to touch
            continue

        state = data.get("state", "running")
        note = data.get("note") or ""
        ended = data.get("ended")
        if state == "running":
            killed = _kill_orphan(data.get("pid"), data.get("pid_created"))
            counts["killed"] += int(killed)
            state = "killed"
            ended = ended or now
            note = (
                "Its Windows-MCP server exited while it was running; the job was ended "
                + ("at the next server start." if killed else "together with the server.")
            )

        job_id = str(data["id"])
        with _lock:
            if job_id in _jobs:
                job_id = f"{job_id}@{data.get('server_pid')}"
        job = Job(
            id=job_id,
            command=data.get("command", ""),
            pid=data.get("pid") or 0,
            pid_created=data.get("pid_created"),
            out_path=directory / data.get("out", f"{stem}.out"),
            err_path=directory / data.get("err", f"{stem}.err"),
            meta_path=meta_path,
            started=float(data.get("started") or now),
            hard_timeout=float(data.get("hard_timeout") or 0),
            state=state,
            returncode=data.get("returncode"),
            ended=float(ended) if ended else now,
            note=note,
            adopted=True,
        )
        job.done.set()
        _write_meta(job)
        with _lock:
            _jobs[job.id] = job
        counts["adopted"] += 1
        calllog.record(
            "job_adopted",
            job.id,
            "PowerShell",
            state=state,
            previous_server_pid=owner[0],
            **({"note": note} if note else {}),
        )

    counts["stray_files_removed"] = _remove_stray_files(now)

    with _lock:
        _next_number = max(_next_number, highest + 1)
    sweep()
    return counts
