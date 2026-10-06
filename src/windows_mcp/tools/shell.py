"""PowerShell tool — shell/command execution, plus PowerShellJob for long runs."""

from typing import Literal

from mcp.types import ToolAnnotations
from windows_mcp.infrastructure import with_analytics
from windows_mcp.powershell import PowerShellExecutor
from windows_mcp.powershell import jobs
from windows_mcp.powershell.service import job_finished_output, job_running_message
from fastmcp import Context


def _response(text: str, status: int | str) -> str:
    return f"Response: {text}\nStatus Code: {status}"


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="PowerShell",
        description=(
            "Shell/command execution. Keywords: shell, run, execute, cmd, terminal, command line, script. "
            "A comprehensive system tool for executing any PowerShell commands. Use it to navigate the file "
            "system, manage files and processes, and execute system-level operations. Capable of accessing web "
            "content (e.g., via Invoke-WebRequest), interacting with network resources, and performing complex "
            "administrative tasks. This tool provides full access to the underlying operating system "
            "capabilities, making it the primary interface for system automation, scripting, and deep system "
            "interaction.\n\n"
            "timeout is the hard limit in seconds: the command is killed when it expires (default 30). "
            "Choose it to fit the work, e.g. 900 for a long build, ssh session or script; there is no upper "
            "bound. A single tool call cannot last longer than about 200 s (the client gives up at 240 s), so "
            "when timeout is larger than that and the command is still running after ~200 s, the call returns "
            "early with the output so far, 'Status Code: running' and a job_id, and the command keeps running "
            "in the background. Then use the PowerShellJob tool to wait for it, check its output, or kill it. "
            "Do not use Start-Sleep polling loops for this. Output is read from files, so print progressively "
            "(e.g. python -u) if you want to see it while the command runs. Interactive prompts are not "
            "supported.\n\n"
            "return_id=true starts the command as a background job and returns its job_id at once, "
            "without waiting, whatever timeout is (timeout stays the hard limit). Use it only for commands "
            "you expect to take long and want to leave running while you do other work, then collect the "
            "result with PowerShellJob. The default false keeps the behaviour described above."
        ),
        annotations=ToolAnnotations(
            title="PowerShell",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @with_analytics(get_analytics(), "Powershell-Tool")
    def powershell_tool(
        command: str, timeout: int = 30, return_id: bool = False, ctx: Context = None
    ) -> str:
        try:
            response, status_code, job_id = PowerShellExecutor.run(
                command, timeout, return_id=return_id
            )
            if job_id is not None:
                return _response(response, "running")
            return _response(response, status_code)
        except Exception as e:
            raise

    @mcp.tool(
        name="PowerShellJob",
        description=(
            "Manage PowerShell commands that are still running in the background after the PowerShell tool "
            "returned 'Status Code: running' with a job_id (it always does with return_id=true). "
            "action='wait' blocks up to wait_seconds (capped at "
            "about 200 s per call) and returns the full output with the exit code once the command has finished, "
            "or the latest output if it is still running; call it again to keep waiting. action='status' returns "
            "immediately. action='kill' stops the command and its child processes. action='list' shows all jobs "
            "(job_id not needed). tail_chars limits how much output is shown while the command is still running. "
            "Every job is bound to the Windows-MCP server: if the server exits, the job's whole process tree ends "
            "with it, and after a restart the job is listed as killed with its output still readable for 6 hours."
        ),
        annotations=ToolAnnotations(
            title="PowerShellJob",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "PowerShellJob-Tool")
    def powershell_job_tool(
        job_id: str = "",
        action: Literal["wait", "status", "kill", "list"] = "wait",
        wait_seconds: int = 60,
        tail_chars: int = 4000,
        ctx: Context = None,
    ) -> str:
        jobs.sweep()
        if action == "list":
            items = jobs.list_jobs()
            if not items:
                return _response("No jobs.", 0)
            lines = []
            for job in items:
                code = "" if job.returncode is None else f", exit {job.returncode}"
                origin = " (from an earlier server run)" if job.adopted else ""
                left = jobs.leftovers(job)
                rest = f", {left} leftover process(es)" if left else ""
                lines.append(
                    f"{job.id}: {job.state}{code}{origin}{rest}, {job.elapsed():.0f}s, PID {job.pid}, "
                    f"hard timeout {job.hard_timeout:.0f}s: {job.command[:120]}"
                )
            return _response("\n".join(lines), 0)

        job = jobs.get(job_id)
        if job is None:
            return _response(
                f"Unknown job_id {job_id!r}. Use action='list' to see current jobs. Finished jobs are deleted "
                "6 hours after they end.",
                1,
            )

        tail = max(0, int(tail_chars))
        waited = 0.0
        if action == "kill":
            jobs.kill(job)
        elif action == "wait":
            waited = float(max(0, min(int(wait_seconds), jobs.return_after())))
            job.done.wait(waited)

        if job.done.is_set():
            output, status = job_finished_output(job)
            head = f"{job.id} finished: {job.state}, exit {job.returncode}, after {job.elapsed():.0f}s."
            left = jobs.leftovers(job)
            if left:
                head += (
                    f" {left} process(es) it started are still running and stay bound to this job; "
                    f"action='kill' ends them, and they end when the job is deleted or the server exits."
                )
            return _response(f"{head}\n{output}", status)
        if action == "kill":
            return _response(
                f"{job.id} could not be stopped yet: {job.note} Check again with action='status'.", 1
            )
        return _response(job_running_message(job, waited, tail), "running")
