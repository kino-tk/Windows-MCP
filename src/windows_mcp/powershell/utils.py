import logging
import signal
import subprocess
import tempfile
from xml.sax.saxutils import escape as xml_escape

import psutil

__all__ = [
    "run_with_graceful_timeout",
    "ps_quote",
    "ps_quote_for_xml",
]

logger = logging.getLogger(__name__)


def ps_quote(value: str) -> str:
    """Wrap value in PowerShell single-quoted string literal (escapes ' as '')."""
    return "'" + value.replace("'", "''") + "'"


def ps_quote_for_xml(value: str) -> str:
    """XML-escape then ps_quote. Use for values in XML passed to PowerShell."""
    escaped = xml_escape(value, {'"': '&quot;', "'": '&apos;'})
    return ps_quote(escaped)


def check_pid_exists(pid: int) -> bool:
    """Check whether a process with the given PID is actively running."""
    try:
        proc = psutil.Process(pid)
        return proc.status() not in (psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _drain(handle) -> bytes | None:
    """Read back everything written to a capture file. Never raises."""
    if handle is None:
        return None
    try:
        handle.seek(0)
        return handle.read()
    except Exception:
        logger.debug("Failed to read back captured output.", exc_info=True)
        return b""


def run_with_graceful_timeout(
        *popenargs,
        input=None,
        capture_output=False,
        timeout=None,
        check=False,
        grace_period: float = 2.0,
        **kwargs,
):
    """A Windows-oriented variant migrated from ``subprocess.run``.

    This helper keeps the overall calling style and behavior of
    ``subprocess.run``, but adapts both the capture mechanism and the
    timeout-handling path for some Windows-specific edge cases.

    Args:
        *popenargs: Positional arguments to pass to ``subprocess.Popen``.
        input: Data to send to stdin (if not None).
        capture_output: If True, capture stdout and stderr into the returned CompletedProcess.
        timeout: Seconds to wait for process to complete before triggering shutdown.
        check: If True, raise CalledProcessError if the process exits with a non-zero code.
        grace_period: Seconds to wait after CTRL_BREAK before force-killing. Defaults to 2.0.

    Notes:
        Two distinct Windows problems are handled here.

        1. Orphaned descendants holding the capture handles.

        A command may spawn a detached grandchild and return immediately, for
        example ``pwsh -> [Diagnostics.Process]::Start("cmd.exe", "/c ...")``.
        The grandchild inherits the standard handles at creation time. When the
        direct child exits, that grandchild is orphaned: its parent PID refers
        to a dead process, so a ``taskkill /T`` on the child's PID cannot reach
        it and it keeps the inherited handles open.

        If those handles are pipes, the read end never reaches EOF, the reader
        threads stay blocked, and closing the pipes on ``Popen.__exit__`` waits
        for them. The call then blocks for as long as the orphan lives, even
        though the direct child finished in milliseconds and ``timeout`` has
        long since expired.

        Capturing into temporary files instead of pipes removes the failure
        mode entirely: an inherited file handle costs nothing to leave open,
        there are no reader threads, and cleanup never waits on a descendant.
        A detached job also keeps running, which is usually what the caller
        wanted when they detached it.

        2. Descendants that stay alive and keep producing output.

        Where the direct child itself does not exit (for example
        ``pwsh -NoProfile -Command "python -c 'while True: print(1)'"``),
        terminating only the top-level process may leave descendants running.
        The timeout path therefore keeps the two-stage shutdown:

        a. Send ``CTRL_BREAK_EVENT`` to the child process group, so console
           applications get a chance to exit cleanly.
        b. If that does not finish within ``grace_period``, terminate the whole
           tree via ``taskkill /T /F``.

        Related issues: #124, #146
    """

    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        kwargs["stdin"] = subprocess.PIPE

    out_file = err_file = None
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
        # Files, not pipes. See note 1 in the docstring.
        out_file = tempfile.TemporaryFile()
        err_file = tempfile.TemporaryFile()
        kwargs["stdout"] = out_file
        kwargs["stderr"] = err_file

    # Windows graceful-stop prerequisite: CREATE_NEW_PROCESS_GROUP is required
    # so that send_signal(CTRL_BREAK_EVENT) targets the child process group
    # rather than the current process (which would cause it to exit).
    creationflags = kwargs.get("creationflags", 0)
    creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
    kwargs["creationflags"] = creationflags

    try:
        with subprocess.Popen(*popenargs, **kwargs) as process:
            try:
                if input is not None and process.stdin is not None:
                    try:
                        process.stdin.write(input)
                    finally:
                        process.stdin.close()
                process.wait(timeout=timeout)

            except subprocess.TimeoutExpired as exc:
                logger.debug("Process did not exit within timeout, attempting graceful shutdown.")
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                except Exception:
                    logger.debug("Failed to send CTRL_BREAK_EVENT, will terminate the tree.")

                try:
                    process.wait(timeout=grace_period)
                    exc.add_note("Process exited after graceful CTRL_BREAK shutdown.")
                except subprocess.TimeoutExpired:
                    logger.debug(
                        f"Process {process.pid} (exist: {check_pid_exists(process.pid)}) did not "
                        f"exit gracefully after {grace_period} seconds, killing it and all child "
                        f"processes..."
                    )
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                    try:
                        process.wait(timeout=grace_period)
                    except subprocess.TimeoutExpired:
                        pass
                    exc.add_note(
                        f"Process killed after failing to exit gracefully within "
                        f"{grace_period} seconds."
                    )

                exc.stdout = _drain(out_file)
                exc.stderr = _drain(err_file)
                raise

            except BaseException:
                # Keep cleanup strategy consistent with timeout path
                logger.debug("Other exception occurred, attempting to kill process...")
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                raise

            retcode = process.returncode
            stdout = _drain(out_file)
            stderr = _drain(err_file)
            args = process.args
    finally:
        for handle in (out_file, err_file):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    logger.debug("Failed to close capture file.", exc_info=True)

    if check and retcode:
        raise subprocess.CalledProcessError(retcode, args, output=stdout, stderr=stderr)

    return subprocess.CompletedProcess(args, retcode, stdout, stderr)
