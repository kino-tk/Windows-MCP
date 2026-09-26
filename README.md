> [!NOTE]
> **This is a fork of [CursorTouch/Windows-MCP](https://github.com/CursorTouch/Windows-MCP)**, based on upstream `main` at `a868ed6` (version 0.8.5), submitted by [kino-tk](https://github.com/kino-tk).
> I made these changes for my own use. The upstream PowerShell tool kept getting in my way under Claude Desktop (hanging calls, failing `ssh`, commands cut off after four minutes), so I changed it to work the way I wanted. They are published as they are.
> The original README follows unchanged after this section.

## About this fork

### Why

Under Claude Desktop, an MSIX-packaged host, the PowerShell tool had four practical problems:

1. **Calls hung after the command had finished.** A detached grandchild inherited the capture pipes, so the reader never saw EOF.
2. **`ssh`, `scp` and `sftp` failed with exit code 255 and no output.** The packaged host hands down an environment block without `ProgramData` and the other known-folder variables, and Win32 OpenSSH aborts before it even parses its arguments.
3. **Anything longer than about four minutes was cut off.** The host abandons a tool call after 240 s, and a server cannot extend that.
4. **When a call did hang, nothing showed which tool or command it was.** The host's log records only that a `tools/call` was sent.

### Installation

The upstream instructions further down (`uvx windows-mcp`, PyPI, the Claude Desktop extension) install the upstream package, which does **not** include these changes. To use this fork, run it from a clone:

1. Clone the fork. [uv](https://docs.astral.sh/uv/) is required; it fetches Python 3.14 and the dependencies on the first run.

   ```powershell
   git clone https://github.com/kino-tk/Windows-MCP-private.git C:\path\to\Windows-MCP-private
   ```

2. Add the server to `claude_desktop_config.json` (for Claude Desktop: `%APPDATA%\Claude\claude_desktop_config.json`, also reachable from Settings → Developer → Edit Config):

   ```json
   {
     "mcpServers": {
       "windows-mcp": {
         "command": "uv",
         "args": ["--directory", "C:\\path\\to\\Windows-MCP-private", "run", "windows-mcp", "serve"]
       }
     }
   }
   ```

   If Claude Desktop cannot find `uv`, give its full path in `command` (run `where uv` to see it).

3. **Restart Claude Desktop** to load the server. Quit it completely, including the tray icon and any Claude processes left in Task Manager, then start it again.

Verified on 2026-09-26: a fresh clone started this way lists 21 tools, including `PowerShellJob`.

### Changes

| Commit | Source files | Change |
|---|---|---|
| `fc33e7f` | `powershell/utils.py` | Capture output into temporary files instead of pipes, so an orphaned grandchild cannot block the call. Measured on a 60 s detached job with `timeout=5`: 63.3 s of blocking before, immediate return after. See upstream [#124](https://github.com/CursorTouch/Windows-MCP/issues/124) and [#146](https://github.com/CursorTouch/Windows-MCP/issues/146). |
| `ed6cc0a` | `powershell/service.py` | Restore `ProgramData`, `ALLUSERSPROFILE`, `PUBLIC`, `ProgramFiles`, `ProgramFiles(x86)`, `ProgramW6432` and their `CommonProgram*` counterparts through `SHGetKnownFolderPath` when the host strips them. Only missing variables are filled. |
| `f789895` | `powershell/utils.py`, `infrastructure/calllog.py` (new), `infrastructure/analytics.py` | Bound every wait on a child process tree. `taskkill` gets a 10 s limit, and the `Popen` is no longer a context manager, because its `__exit__` ends in an unbounded `wait()`. Before: a child that ignores CTRL_BREAK held a call for 30.1 s against a 1 s timeout. Also adds the tool call log. |
| `1574769` | `powershell/jobs.py` (new), `powershell/service.py`, `tools/shell.py` | Background jobs for long PowerShell commands, and the new `PowerShellJob` tool. |
| `cb96450` | `powershell/service.py` | Default `PYTHONIOENCODING=utf-8` for spawned shells, so non-ASCII text printed by Python is not garbled. |
| `7d86103` | `powershell/jobs.py`, `tools/shell.py`, `__main__.py` | Bind every job to the server with a Windows Job Object, record each job in a metadata file, and clean up after an earlier server run at start. See [Job lifetime and cleanup](#job-lifetime-and-cleanup). |
| `45098ab` | `powershell/jobs.py`, `__main__.py` | Sweep finished jobs and stray files on a schedule, so a server left running for days still cleans up. Retention and interval are configurable. |
| `3768989` | `powershell/jobs.py`, `powershell/service.py`, `tools/shell.py`, `scripts/windows-mcp-jobs.ps1` (new) | Keep what a finished job left running bound to the job, even if its root process was killed from outside; add a script that reports every job record and, as a last resort, kills a verified PID. The scheduled sweep defaults to every 90 minutes. |
| `3ff68c0` | `filesystem/service.py`, `tools/filesystem.py` | FileSystem `write` no longer turns every `\n` into `\r\n`; a new `line_ending` argument chooses `keep`, `lf` or `crlf`. See [Line endings in FileSystem write](#line-endings-in-filesystem-write). |

Paths are under `src/windows_mcp/`, except `scripts/`.

### Long commands: `timeout` and `PowerShellJob`

#### Command syntax

```text
PowerShell     command=<string> [timeout=<seconds>]

PowerShellJob  action=list
PowerShellJob  action=status  job_id=<id> [tail_chars=<n>]
PowerShellJob  [action=wait]  job_id=<id> [wait_seconds=<0-200>] [tail_chars=<n>]
PowerShellJob  action=kill    job_id=<id>
```

- `[ ]` marks an optional argument; `<...>` is a value you supply. Anything not listed for a form is ignored.
- MCP arguments are passed by name, so their order does not matter; the forms above show the conventional order.
- `action=wait` is the default, so `PowerShellJob job_id=<id>` waits.

| Argument | Tool | Default | Meaning |
|---|---|---|---|
| `command` | PowerShell | (required) | The PowerShell command line to run. |
| `timeout` | PowerShell | `30` | Hard limit in seconds; the command is killed when it expires. No upper bound. |
| `action` | PowerShellJob | `wait` | `wait`, `status`, `kill` or `list`. |
| `job_id` | PowerShellJob | (required except for `list`) | The id returned by PowerShell, for example `job-1`. |
| `wait_seconds` | PowerShellJob | `60` | For `wait`: how long to block. Capped at about 200 s per call. |
| `tail_chars` | PowerShellJob | `4000` | While the command is still running: how many of the latest characters of stdout and of stderr to show. A finished job always shows its full output. |

#### How it behaves

- If `timeout` is longer than about 200 s and the command is still running at that point, the PowerShell call returns early with the output so far, `Status Code: running` and a job id. The command keeps running.
- A command that finishes within 200 s returns exactly what a plain call would.
- Calls whose `timeout` is 200 s or less take the pre-job code path (`execute_command`), which still has the bounded waits added in `f789895`.
- `wait` returns the full output and the exit code once the command has finished. If it is still running, `wait` returns the latest output with `Status Code: running`; call it again.
- `status` returns at once. `kill` stops the command and every process it started. `list` shows every job with its state.
- Output is written to files, so it can be read while the command runs. Programs must print progressively to show partial output (for Python, use `-u`). Interactive prompts are not supported.

#### Examples

```text
PowerShell    command="ssh host 'long-task.sh'" timeout=600
  -> Still running after 200s (waited 200s in this call). The command continues in the
     background as job-1 (PID 4196); it is stopped at its hard timeout of 600s ...
     --- output so far (last 4000 chars of each stream) ---
     ...
     Status Code: running

PowerShellJob action=status job_id=job-1 tail_chars=500
  -> Still running after 245s ... (only the last 500 characters of output)
     Status Code: running

PowerShellJob job_id=job-1 wait_seconds=200
  -> job-1 finished: exited, exit 0, after 302s.
     <full output>
     Status Code: 0

PowerShellJob action=kill job_id=job-2
  -> job-2 finished: killed, exit 1, after 40s. ...
     Status Code: 1

PowerShellJob action=list
  -> job-1: exited, exit 0, 302s, PID 4196, hard timeout 600s: ssh host 'long-task.sh'
     job-2: killed, exit 1, 40s, PID 5120, hard timeout 900s: ...
     Status Code: 0
```

The first and third results are from a verified run under Claude Desktop.

#### Listing jobs

```text
PowerShellJob action=list
```

Example response (illustrative values):

```text
Response: job-1: killed (from an earlier server run), 5s, PID 77412, hard timeout 900s: & 'C:\tools\python.exe' -u 'C:\work\train.py'
job-3: running, 412s, PID 80844, hard timeout 900s: & 'C:\tools\python.exe' -u 'C:\work\long-task.py'
job-4: exited, exit 0, 245s, PID 81120, hard timeout 600s: ssh host 'backup.sh'
job-5: exited, exit 0, 1 leftover process(es), 230s, PID 81502, hard timeout 600s: & '.\start-worker.ps1'
Status Code: 0
```

Jobs are listed in the order they started. Each line is `<job id>: <state>[, exit <code>][, (from an earlier server run)][, <n> leftover process(es)], <elapsed>, PID <pid>, hard timeout <seconds>: <command>`. The PID is that of the PowerShell process that runs the command. `No jobs.` means there are none.

#### If `kill` does not work (last resort)

Normally `PowerShellJob action=kill job_id=<id>` is enough, and if the server itself is gone, every job's processes have already ended with it. If a job still seems to be running, or the server does not answer, use the job records directly:

1. **Find the PID.** If the server answers, `PowerShellJob action=list` shows it. If not, run the report script from the clone. It reads every record (`~/.windows-mcp/jobs/*.json`) and prints all its fields, plus two live checks: `Running` is `True` only if the recorded PID still belongs to the same process (same PID *and* same start time), and `Server` says whether the owning server is still running. Add `-Tail 20` to see the last 20 lines of each job's output.

   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\path\to\Windows-MCP-private\scripts\windows-mcp-jobs.ps1
   ```

   ```text
   Id              : job-3
   Record          : 75036-job-3
   State           : running
   Running         : True
   Pid             : 80844
   PidStarted      : 2026/09/26 21:10:05
   PidCreatedEpoch : 1790424605.123
   ExitCode        :
   Started         : 2026/09/26 21:10:05
   Ended           :
   HardTimeout     : 900
   ServerPid       : 75036
   Server          : running
   JobObject       : True
   Adopted         : False
   Command         : & 'C:\tools\python.exe' -u 'C:\work\long-task.py'
   Note            :
   OutFile         : C:\Users\you\.windows-mcp\jobs\75036-job-3.out
   OutBytes        : 1834
   ErrFile         : C:\Users\you\.windows-mcp\jobs\75036-job-3.err
   ErrBytes        : 0
   RecordFile      : C:\Users\you\.windows-mcp\jobs\75036-job-3.json
   ```

   (Illustrative values; dates follow your locale.)

2. **Kill it, only if `Running` is `True`.** Either let the script do it, which re-checks the PID and lists what it will kill:

   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\path\to\Windows-MCP-private\scripts\windows-mcp-jobs.ps1 -Kill job-3
   ```

   or run taskkill yourself with the PID from step 1:

   ```powershell
   taskkill /PID 80844 /T /F
   ```

   If `Running` is `False`, do **not** use that PID: the job's process is gone, and Windows may have given the number to an unrelated process.

3. **Descendants whose parent has already exited** are not reached by `taskkill /T`, which follows parent links. They stay bound to the job, so `PowerShellJob action=kill job_id=job-3` ends them. If the server does not answer, restart Claude Desktop: when the server exits, Windows ends every process in its jobs.

### Job lifetime and cleanup

Every job is bound to the server process that started it. Normally you never need to find and kill a job's processes yourself; the last-resort steps above are for when something has gone wrong.

| Situation | What happens |
|---|---|
| The command finishes | Its output and exit code stay available to `status`, `wait` and `list`. If the command was handed back as a job, any process it left running stays bound to the job: `list` shows it as a leftover, `kill` ends it, and it ends when the job is deleted or the server exits. This holds however the command ended, including when its PowerShell process was killed from outside. A command that finishes within 200 s behaves exactly like a plain call instead: a process it deliberately left running (for example with `Start-Process`) keeps running. |
| `kill`, or the hard timeout expires | CTRL_BREAK is sent. As soon as the command exits, or after 2 s if it does not, whatever is left of its process tree is ended through the Job Object, including grandchildren whose parent has already exited. |
| The server shuts down normally | Running jobs are stopped and recorded as killed, with the reason. |
| The server crashes or is killed | Windows closes the server's Job Object handles, which immediately ends every process bound to a job, running or left over. No job keeps running unsupervised. (In the fallback case described below, leftovers are instead killed at the next server start.) |
| The next server starts | It reads the records left by servers that are gone. A leftover process is killed only if both its PID and its creation time match the record, so a process that merely reused the PID is never touched. The job is then listed as `killed (from an earlier server run)`, with its output still readable. Jobs of another server that is still running are left alone. |

How this works: each command is started suspended, placed in its own Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, and only then resumed, so nothing it starts can escape the Job Object. Only a command that finishes within its first call has its Job Object released without killing. If a Job Object cannot be created, the job still runs: this is logged and recorded as `"job_object": false`, `kill` and the hard timeout fall back to `taskkill /T`, and the next server start still kills verified leftovers.

**No depth limit.** Windows puts every process that a job's process starts into the same Job Object, at any depth, so there is no cut-off at children or grandchildren. Verified on 2026-09-26:

- A chain of 7 processes, in which every intermediate generation had already exited (so the deepest one was an orphan several levels down and ignored CTRL_BREAK), was ended completely by `kill` and by closing the Job Object.
- A child that asks to leave the Job Object (`CREATE_BREAKAWAY_FROM_JOB`) is refused with "access denied".

What is not covered: a program that is not a descendant of the command, because Windows starts it on behalf of another process. Examples are a program launched through a Windows service, Task Scheduler, WMI (`Win32_Process.Create`), an out-of-process COM server, or with elevation (UAC). Such a program is outside the Job Object, just as it is outside the command's process tree. (This follows from how Windows creates those processes; it was not tested here.)

**Files.** Each job keeps three files in `~/.windows-mcp/jobs`, named `<server PID>-<job id>`:

| File | Content |
|---|---|
| `.out` | stdout |
| `.err` | stderr |
| `.json` | The job's record: id, PID and its creation time, the first 200 characters of the command, hard timeout, state, exit code, start and end times, note, and the server that owns it. It is rewritten atomically whenever the state changes. |

**When files are deleted.**

- A finished job is kept for **6 hours after it ended** (`WINDOWS_MCP_JOB_RETENTION_HOURS`). Until then it stays in `list`.
- A sweeper thread in the server checks every **90 minutes** (`WINDOWS_MCP_JOB_SWEEP_INTERVAL`) and deletes the three files of every job past its retention. A server left running for days therefore still cleans up on schedule, even if no one touches it. Server start, any new job and any `PowerShellJob` call sweep as well. Deleting a job also ends any leftover process still bound to it.
- A job that finishes within the first 200 s of its call is deleted right away, because its output was already returned.
- Files that no job record claims (for example output left by older versions of this fork) are deleted by the same sweep once they are older than 10 minutes.
- Job numbers continue across restarts, so an id is never reused while an older job with it is still on disk.

**Changing the cleanup settings.** Add an `env` block to the server's entry in `claude_desktop_config.json`. Values are strings; the interval is in seconds and the retention in hours:

```json
{
  "mcpServers": {
    "windows-mcp": {
      "command": "uv",
      "args": ["--directory", "C:\\path\\to\\Windows-MCP-private", "run", "windows-mcp", "serve"],
      "env": {
        "WINDOWS_MCP_JOB_SWEEP_INTERVAL": "600",
        "WINDOWS_MCP_JOB_RETENTION_HOURS": "24"
      }
    }
  }
}
```

This example sweeps every 10 minutes and keeps finished jobs for 24 hours. The server reads its environment only when it starts, so **restart Claude Desktop completely** (tray icon and any Claude processes in Task Manager included) for the change to take effect. Remove the lines to go back to the defaults (90 minutes, 6 hours).

### Line endings in FileSystem write

Upstream, the `FileSystem` tool's `write` mode opens files in text mode, so on Windows every `\n` becomes `\r\n` and files meant for Linux always come out with CRLF line endings. In this fork the line endings are exactly what you ask for:

```text
FileSystem  mode=write path=<file> content=<text> [append=true] [encoding=<name>] [line_ending=keep|lf|crlf]
```

| `line_ending` | Result |
|---|---|
| `keep` (default) | The content is written exactly as given. Text sent with `\n` stays LF; an explicit `\r\n` stays CRLF. |
| `lf` | Every line break becomes LF, the Linux line ending. |
| `crlf` | Every line break becomes CRLF, for files such as `.bat` or `.cmd` that need it. An existing `\r\n` is not doubled. |

`append=true` follows the same rule. Windows Notepad reads and keeps LF files correctly since Windows 10 version 1809 (it shows "Unix (LF)" in the status bar), so LF is safe for files you also open on Windows.

To change the default for every write, set `WINDOWS_MCP_WRITE_LINE_ENDING` in the `env` block of the server's entry in `claude_desktop_config.json` (the same place as the cleanup settings above), then restart Claude Desktop completely:

```json
"env": {
  "WINDOWS_MCP_WRITE_LINE_ENDING": "lf"
}
```

An explicit `line_ending` argument always wins over the default. An unknown value in the environment variable is ignored (the default stays `keep`); an unknown argument value is reported as an error and nothing is written.

### Tool call log

Every tool call writes a `start` and an `end` record to `~/.windows-mcp/calls.log` as JSON lines, plus a `cancelled` record if the host gives up first. Jobs add `job_start` and `job_end` records, and `job_adopted` when a new server takes over the record of a job from an earlier run.

- `id` is `<server PID>-<sequence>` and pairs a call's `start`, `cancelled` and `end`. Job records use the job id.
- `tool` is the server's internal label (for example `Powershell-Tool`), not the MCP tool name.
- A call stuck inside the server shows up as a `start` without an `end`.
- For sync tools the `end` record is written from the worker thread, so after a `cancelled` it still shows when the work really stopped.
- Commands are truncated to 200 characters. Free-text arguments (`content`, `text`, `value`, `data`, `input`) are logged by length only.
- The file rotates at 1 MB and keeps 3 older generations, about 4 MB in total.

A call that became a job (from a verified run; the command is replaced, and `job_start` also shows the `job_object` field that the current version adds):

```json
{"ts": "2026-09-26T09:21:06.411+09:00", "event": "start", "id": "9696-1", "tool": "Powershell-Tool", "args": {"command": "ssh host 'long-task.sh'", "timeout": 600}, "client": "claude-ai"}
{"ts": "2026-09-26T09:21:06.447+09:00", "event": "job_start", "id": "job-1", "tool": "PowerShell", "pid": 4196, "hard_timeout": 600.0, "job_object": true, "command": "ssh host 'long-task.sh'"}
{"ts": "2026-09-26T09:24:26.471+09:00", "event": "end", "id": "9696-1", "tool": "Powershell-Tool", "status": "ok", "duration_ms": 200059, "result_chars": 788, "exit": "running"}
{"ts": "2026-09-26T09:26:07.956+09:00", "event": "job_end", "id": "job-1", "tool": "PowerShell", "state": "exited", "returncode": 0, "duration_ms": 301509}
```

A job whose server was killed, taken over by the next server (real record):

```json
{"ts": "2026-09-26T18:56:47.000+09:00", "event": "job_adopted", "id": "job-1", "tool": "PowerShell", "state": "killed", "previous_server_pid": 75036, "note": "Its Windows-MCP server exited while it was running; the job was ended together with the server."}
```

A call the host abandoned (illustrative values, in the format the server writes):

```json
{"ts": "2026-09-26T10:00:00.010+09:00", "event": "start", "id": "9696-7", "tool": "Screenshot-Tool", "args": {}, "client": "claude-ai"}
{"ts": "2026-09-26T10:04:00.012+09:00", "event": "cancelled", "id": "9696-7", "tool": "Screenshot-Tool", "after_ms": 240002}
{"ts": "2026-09-26T10:05:13.550+09:00", "event": "end", "id": "9696-7", "tool": "Screenshot-Tool", "status": "ok", "duration_ms": 313540, "result_chars": 5120}
```

### Environment variables added by this fork

| Variable | Default | Meaning |
|---|---|---|
| `WINDOWS_MCP_JOB_RETURN_AFTER` | `200` | Seconds a call waits before handing a command over as a job (capped at 225) |
| `WINDOWS_MCP_JOB_DIR` | `~/.windows-mcp/jobs` | Where job output and records are written |
| `WINDOWS_MCP_JOB_RETENTION_HOURS` | `6` | How long a finished job is kept |
| `WINDOWS_MCP_JOB_SWEEP_INTERVAL` | `5400` | Seconds between scheduled sweeps (90 minutes) |
| `WINDOWS_MCP_CALLLOG` | `~/.windows-mcp/calls.log` | Call log path, or `off` to disable it |
| `WINDOWS_MCP_CALLLOG_MAX_BYTES` | `1000000` | Rotation threshold in bytes |
| `WINDOWS_MCP_CALLLOG_BACKUPS` | `3` | Older generations to keep |
| `WINDOWS_MCP_WRITE_LINE_ENDING` | `keep` | Default line endings for FileSystem `write`: `keep`, `lf` or `crlf` |

`PYTHONIOENCODING` is set to `utf-8` only when it is not already set, and a command can still override it.

### Notes

- PowerShell reports a failing native command as exit code 1. Append `; exit $LASTEXITCODE` to pass the original code on. This is unchanged upstream behaviour.
- Text output is UTF-8 end to end for PowerShell itself and for the programs checked on 2026-09-26 (`cmd`, `dir`, `where`, `findstr`, git, node, and Python with the default above). The exception is older tools that read text piped into them in the system's ANSI code page, such as `sort.exe` on a Japanese system; setting `[Console]::InputEncoding` does not change that. Use the PowerShell equivalent (`Sort-Object`) instead.
- Tests: 571 passed, against an upstream baseline of 508. The new tests are `tests/test_kill_bounds.py`, `tests/test_calllog.py`, `tests/test_powershell_jobs.py`, `tests/test_job_lifecycle.py`, `tests/test_python_io_encoding.py` and `tests/test_line_endings.py`.

### License

MIT, the same as upstream. Copyright (c) 2025 JEOMON GEORGE; see [LICENSE.md](LICENSE.md). The changes in this fork are provided under the same license.

---

<div align="center">
  <h1>🪟 Windows-MCP</h1>

  <a href="https://github.com/CursorTouch/Windows-MCP/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </a>
  <img src="https://img.shields.io/badge/python-3.13%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/platform-Windows%207–11-blue" alt="Platform: Windows 7 to 11">
  <img src="https://img.shields.io/github/last-commit/CursorTouch/Windows-MCP" alt="Last Commit">
  <a href="https://pepy.tech/projects/windows-mcp">
    <img src="https://static.pepy.tech/personalized-badge/windows-mcp?period=total&amp;units=INTERNATIONAL_SYSTEM&amp;left_color=BLACK&amp;right_color=GREEN&amp;left_text=downloads" alt="PyPI Downloads">
  </a>
  <br>
  <a href="https://x.com/CursorTouch">
    <img src="https://img.shields.io/badge/follow-%40CursorTouch-1DA1F2?logo=twitter&style=flat" alt="Follow on Twitter">
  </a>
  <a href="https://discord.com/invite/Aue9Yj2VzS">
    <img src="https://img.shields.io/badge/Join%20on-Discord-5865F2?logo=discord&logoColor=white&style=flat" alt="Join us on Discord">
  </a>

  <a href="https://trendshift.io/repositories/20935?utm_source=trendshift-badge&amp;utm_medium=badge&amp;utm_campaign=badge-trendshift-20935" target="_blank" rel="noopener noreferrer"><img src="https://trendshift.io/api/badge/trendshift/repositories/20935/daily?language=Python" alt="CursorTouch%2FWindows-MCP | Trendshift" width="250" height="55"/></a>

</div>

**Windows-MCP** is a lightweight, open-source project that enables seamless integration between AI agents and the Windows operating system. Acting as an MCP server bridges the gap between LLMs and the Windows operating system, allowing agents to perform tasks such as **file navigation, application control, UI interaction, QA testing,** and more.

mcp-name: io.github.CursorTouch/Windows-MCP

## Updates
- Windows-MCP reached `2M+ Users` in [Claude Desktop Extensiosn](https://claude.ai/directory). 
- Try out [🪟Windows-Use](https://pypi.org/project/windows-use/), an agent built using Windows-MCP.
- Windows-MCP is now available on [PyPI](https://pypi.org/project/windows-mcp/) (thus supports `uvx windows-mcp`)
- Windows-MCP is added to [MCP Registry](https://github.com/modelcontextprotocol/registry)

### Supported Operating Systems

- Windows 7
- Windows 8, 8.1
- Windows 10
- Windows 11  

## 🎥 Demos

<https://github.com/user-attachments/assets/d0e7ed1d-6189-4de6-838a-5ef8e1cad54e>

<https://github.com/user-attachments/assets/d2b372dc-8d00-4d71-9677-4c64f5987485>

## ✨ Key Features

- **Seamless Windows Integration**  
  Interacts natively with Windows UI elements, opens apps, controls windows, simulates user input, and more.

- **Use Any LLM (Vision Optional)**
   Unlike many automation tools, Windows-MCP doesn't rely on any traditional computer vision techniques or specific fine-tuned models; it works with any LLMs, reducing complexity and setup time.

- **Rich Toolset for UI Automation**  
  Includes tools for basic keyboard, mouse operation and capturing window/UI state.

- **Lightweight & Open-Source**  
  Minimal dependencies and easy setup with full source code available under MIT license.

- **Customizable & Extendable**  
  Easily adapt or extend tools to suit your unique automation or AI integration needs.

- **Real-Time Interaction**  
  Typical latency between actions (e.g., from one mouse click to the next) ranges from **0.2 to 0.5 secs**, and may slightly vary based on the number of active applications and system load, also the inferencing speed of the llm.

- **DOM Mode for Browser Automation**  
  Special `use_dom=True` mode for State-Tool that focuses exclusively on web page content, filtering out browser UI elements for cleaner, more efficient web automation. Supports Chrome, Edge, and Firefox (Firefox uses an IAccessible2 fallback since it doesn't expose `RootWebArea` via UIA).

## 🛠️Installation

**Note:** When you install this MCP server for the first time it may take a minute or two because of installing the dependencies in `pyproject.toml`. In the first run the server may timeout ignore it and restart it.

### Prerequisites

- Python 3.13+
- UV (Package Manager) from Astra, install with `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `English` as the default language in Windows preferred else disable the `App-Tool` in the MCP Server for Windows with other languages.

### Run at Login

Run the server directly when needed:

```shell
uvx windows-mcp serve
uvx windows-mcp serve --transport sse --host localhost --port 8000
uvx windows-mcp serve --transport streamable-http --host localhost --port 8000
```

Install it as a background task that starts now and at every login:

```shell
windows-mcp install

# Or choose the HTTP transport and bind address explicitly
windows-mcp install --transport sse --host 127.0.0.1 --port 8000
```

This creates a per-user Scheduled Task named `windows-mcp-server` and a wrapper script at
`~/.windows-mcp/start-server.cmd`. Use `windows-mcp uninstall` to remove it. Logs are written
to `~/.windows-mcp/server.log` and `~/.windows-mcp/server.error.log`.

<details>
  <summary>Install in Claude Desktop</summary>

  1. Install [Claude Desktop](https://claude.ai/download).

```shell
npm install -g @anthropic-ai/mcpb
```

  2. Configure the MCP server.

  **Option A: Install from PyPI (Recommended)**
  
  Use `uvx` to run the latest version directly from PyPI.

  Add this to your `claude_desktop_config.json`:
  ```json
  {
    "mcpServers": {
      "windows-mcp": {
        "command": "uvx",
        "args": [
          "windows-mcp",
          "serve"
        ]
      }
    }
  }
  ```

  **Option B: Install from Source**

  1. Clone the repository:
  ```shell
  git clone https://github.com/CursorTouch/Windows-MCP.git
  cd Windows-MCP
  ```

  2. Add this to your `claude_desktop_config.json`:
  ```json
  {
    "mcpServers": {
      "windows-mcp": {
        "command": "uv",
        "args": [
          "--directory",
          "<path to the windows-mcp directory>",
          "run",
          "windows-mcp",
          "serve"
        ]
      }
    }
  }
  ```
  3. Fully restart Claude Desktop and verify the server appears in the MCP tools list.

  **Claude Desktop MSIX (Windows Store)**

  The MSIX-packaged Claude Desktop (Microsoft Store version) virtualizes `%APPDATA%`. This causes two main issues:
  1. The config file is located at: `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` (not `%APPDATA%\Claude\`).
  2. Automatic installation from the "Claude Directory" will fail because the `${__dirname}` variable resolves to the incorrect (non-virtualized) path.

  **To configure Windows-MCP on the Windows Store version of Claude:**
  
  You must manually edit the configuration file. Note that Electron apps in the MSIX sandbox do not inherit the system `PATH`, so you must use the **full absolute path** to `uvx.exe` (or `uv.exe`).

  **Option A: Using pre-installed executable**

  1. In a terminal, run `uv tool install windows-mcp`.
  2. Use the generated executable in your config:
  ```json
  {
    "mcpServers": {
      "windows-mcp": {
        "command": "C:\\Users\\<user>\\.local\\bin\\windows-mcp.exe",
        "args": ["serve"]
      }
    }
  }
  ```

  **Option B: Using uvx**
  ```json
  {
    "mcpServers": {
      "windows-mcp": {
        "command": "C:\\Users\\<user>\\.local\\bin\\uvx.exe",
        "args": ["windows-mcp", "serve"]
      }
    }
  }
  ```

  **Option C: Install from Source**
  ```json
  {
    "mcpServers": {
      "windows-mcp": {
        "command": "C:\\Users\\<user>\\.local\\bin\\uv.exe",
        "args": [
          "--directory",
          "C:\\path\\to\\Windows-MCP",
          "run",
          "windows-mcp",
          "serve"
        ]
      }
    }
  }
  ```

  Replace `<user>` with your Windows username. To find the correct paths, run `where uvx`, `where windows-mcp`, or `where uv`. Fully quit Claude Desktop (Tray → Quit) and reopen after saving the config.

  For additional Claude Desktop integration troubleshooting, see the [MCP documentation](https://modelcontextprotocol.io/quickstart/server#claude-for-desktop-integration-issues).
</details>

<details>
  <summary>Install in Perplexity Desktop</summary>

  1. Install [Perplexity Desktop](https://apps.microsoft.com/detail/xp8jnqfbqh6pvf).
  2. Open Perplexity Desktop and go to `Settings -> Connectors -> Add Connector -> Advanced`.
  3. Enter the name as `Windows-MCP`, then paste one of the following configs.


  **Option A: Install from PyPI (Recommended)**

  ```json
  {
    "command": "uvx",
    "args": [
      "windows-mcp",
      "serve"
    ]
  }
  ```

  **Option B: Install from Source**

  ```json
  {
    "command": "uv",
    "args": [
      "--directory",
      "<path to the windows-mcp directory>",
      "run",
      "windows-mcp",
      "serve"
    ]
  }
  ```

  4. Click `Save`, then restart Perplexity Desktop if needed.

For additional Claude Desktop integration troubleshooting, see the [Perplexity MCP Support](https://www.perplexity.ai/help-center/en/articles/11502712-local-and-remote-mcps-for-perplexity). The documentation includes helpful tips for checking logs and resolving common issues.
</details>

<details>
  <summary> Install in Gemini CLI</summary>

  1. Install Gemini CLI.

```shell
npm install -g @google/gemini-cli
```

  2. Open `%USERPROFILE%/.gemini/settings.json`.
  3. Add the `windows-mcp` config and save it.

```json
{
  "theme": "Default",
  ...
  "mcpServers": {
    "windows-mcp": {
      "command": "uvx",
      "args": [
        "windows-mcp",
        "serve"
      ]
    }
  }
}
```
*Note: To run from source, replace the command with `uv` and args with `["--directory", "<path>", "run", "windows-mcp", "serve"]`.*

  4. Restart Gemini CLI.
</details>

<details>
  <summary>Install in Qwen Code</summary>
  1. Install Qwen Code.

```shell
npm install -g @qwen-code/qwen-code@latest
```
  2. Open `%USERPROFILE%/.qwen/settings.json`.
  3. Add the `windows-mcp` config and save it.

```json
{
  "mcpServers": {
    "windows-mcp": {
      "command": "uvx",
      "args": [
        "windows-mcp",
        "serve"
      ]
    }
  }
}
```
*Note: To run from source, replace the command with `uv` and args with `["--directory", "<path>", "run", "windows-mcp", "serve"]`.*

  4. Restart Qwen Code.
</details>

<details>
  <summary>Install in Codex CLI</summary>
  1. Install Codex CLI.

```shell
npm install -g @openai/codex
```
  2. Open `%USERPROFILE%/.codex/config.toml`.
  3. Add the `windows-mcp` config and save it.

```toml
[mcp_servers.windows-mcp]
command="uvx"
args=[
  "windows-mcp",
  "serve"
]
```
*Note: To run from source, replace the command with `uv` and args with `["--directory", "<path>", "run", "windows-mcp", "serve"]`.*

  4. Restart Codex CLI.
</details>

<details>
  <summary>Install in Autohand Code</summary>

  Add the published stdio server from a Windows terminal:

  ```shell
  autohand mcp add windows-mcp uvx windows-mcp serve
  ```

  Add `--scope project` after `add` to keep the server configuration in the current project. See [Autohand Code](https://github.com/autohandai/code-cli/) for current installation and CLI details.
</details>

<details>
  <summary>Install in Claude Code</summary>

  1. Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview):

```shell
npm install -g @anthropic-ai/claude-code
```

  2. Configure the server:

  **Option A: Install from PyPI (Recommended)**

  Use `uvx` to run the latest version directly from PyPI.

  ```shell
  claude mcp add --transport stdio windows-mcp -- uvx windows-mcp serve
  ```

  **Option B: Install from Source**

  1. Clone the repository:
  ```shell
  git clone https://github.com/CursorTouch/Windows-MCP.git
  cd Windows-MCP
  ```

  2. Run the following command in your terminal:
  ```shell
  claude mcp add --transport stdio windows-mcp -- uv --directory "<path>" run windows-mcp serve
  ```

  *Note: To make the server available across all projects, add `--scope user` to the command.*

  3. Rerun Claude Code in terminal. Enjoy 🥳

  **Note:** On Windows, if you encounter "Connection closed" errors, use the full path to `uvx.exe`:

  ```shell
  claude mcp add --transport stdio windows-mcp -- C:\Users\<user>\.local\bin\uvx.exe windows-mcp serve
  ```

  To verify the server is registered, run `claude mcp list`. Inside Claude Code, use `/mcp` to check server status.

  **WSL (Windows Subsystem for Linux)**

  If you run Claude Code from WSL, the MCP server must still execute on the Windows side (it needs Windows APIs for UI automation). Use `powershell.exe` as the command to bridge WSL and Windows:

  1. Install `uv` on **Windows** (from a PowerShell terminal):
  ```powershell
  irm https://astral.sh/uv/install.ps1 | iex
  ```

  2. From your **WSL terminal**, register the server:
  ```shell
  claude mcp add windows-mcp --transport stdio -s user -- powershell.exe -Command "C:\Users\<user>\.local\bin\uvx.exe windows-mcp serve"
  ```

  Replace `<user>` with your Windows username. The `-s user` flag makes the server available across all projects.

  3. Restart Claude Code and verify with `/mcp`.
</details>

---

## 🖥️ Running Windows-MCP

Windows-MCP runs directly on your Windows machine and exposes its tools to the connected MCP client.

```shell
# Runs with stdio transport (default)
uvx windows-mcp serve

# Or with SSE/Streamable HTTP for network access
uvx windows-mcp serve --transport sse --host localhost --port 8000
uvx windows-mcp serve --transport streamable-http --host localhost --port 8000
```

Optional environment variables can be set to customize behavior — see [Environment Variables](#-environment-variables) below.

### Security for Remote Access

For network access, enable authentication and TLS:

```shell
windows-mcp serve --transport sse --host 0.0.0.0 \
  --auth-key "your_secret_token" \
  --ip-allowlist "203.0.113.0/24" \
  --ssl-certfile cert.pem --ssl-keyfile key.pem
```

See [🔐 Security & Access Control](#-security--access-control) for all options.

### Transport Options

| Transport | Command | Use Case |
|---|---|---|
| `stdio` (default) | `serve --transport stdio` | Direct connection from MCP clients like Claude Desktop, Cursor, etc. |
| `sse` | `serve --transport sse --host HOST --port PORT` | Network-accessible via Server-Sent Events |
| `streamable-http` | `serve --transport streamable-http --host HOST --port PORT` | Network-accessible via HTTP streaming (recommended for production) |

---

## 🔐 Security & Access Control

### Authentication
```shell
windows-mcp serve --transport sse --host 0.0.0.0 --auth-key "your_token"
```
Requires `Authorization: Bearer your_token` header on all requests.

### IP Allowlist
```shell
windows-mcp serve --auth-key "token" --ip-allowlist "203.0.113.0/24,198.51.100.5"
```
Restricts connections to specified CIDR ranges. Blocks private/loopback IPs by default.

### CORS Origins

By default, **no CORS headers are emitted**. Browsers block cross-origin requests via their own Same-Origin Policy, which means arbitrary websites cannot reach the MCP control plane even if the server is on `localhost`. Host-header validation (DNS rebinding protection) is also applied automatically based on the bind address.

If you need a browser-based MCP client to reach the server, opt in with an explicit origin allowlist:

```shell
windows-mcp serve --cors-origins "https://my-client.example.com,https://other.example.com"
```

Only the listed origins receive `Access-Control-Allow-Origin` headers; all other cross-origin requests are rejected by the browser. The equivalent environment variable is `WINDOWS_MCP_CORS_ORIGINS`.

### Tool Selection
All tools are enabled by default. Use `--tools` to whitelist specific tools, or `--exclude-tools` to block specific ones.

```shell
windows-mcp serve --tools "Screenshot,Click,Snapshot"   # Enable only these tools
windows-mcp serve --exclude-tools "PowerShell,Registry" # Disable specific tools
```

### TLS/HTTPS
```shell
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes

windows-mcp serve --ssl-certfile cert.pem --ssl-keyfile key.pem
```

### OAuth 2.0 + PKCE

For MCP clients that use OAuth (e.g. Claude Desktop) instead of a static API key:

```shell
windows-mcp serve --transport streamable-http --host 0.0.0.0 \
  --ssl-certfile ~/.windows-mcp/cert.pem \
  --ssl-keyfile  ~/.windows-mcp/key.pem \
  --oauth-client-id my-client \
  --oauth-client-secret my-secret
```

**Claude Desktop config:**
```json
{
  "mcpServers": {
    "windows-mcp": {
      "type": "http",
      "url": "https://<host>:8000/mcp/",
      "oauth": {
        "clientId": "my-client",
        "clientSecret": "my-secret"
      }
    }
  }
}
```

The OAuth server exposes:
- `GET /.well-known/oauth-authorization-server` — server metadata (RFC 8414)
- `GET /oauth/authorize` — Authorization Code + PKCE (`S256` required)
- `POST /oauth/token` — token exchange (client secret required)
- `POST /oauth/register` — disabled; clients must be pre-provisioned

Dynamic client registration is disabled. Redirect URIs must be loopback `http(s)` only.
Auth key and OAuth can coexist — both are accepted as valid Bearer tokens.

### Config File (`~/.windows-mcp/config.toml`)

Instead of passing flags every time, store your configuration in `~/.windows-mcp/config.toml`. CLI flags always override config file values.

**Search order:**
1. `--config /path/to/config.toml`
2. `~/.windows-mcp/config.toml`

**stdio** — local only, no security needed:
```toml
[server]
transport = "stdio"
```

**SSE** — network access with auth and IP restriction:
```toml
[server]
transport = "sse"
host      = "0.0.0.0"
port      = 8000
auth_key  = "your-secret-key"

[security]
ip_allowlist = ["192.168.1.0/24"]
```

**Streamable HTTP** — with auth, TLS, and tool exclusions:
```toml
[server]
transport    = "streamable-http"
host         = "0.0.0.0"
port         = 8000
auth_key     = "your-secret-key"
ssl_certfile = "cert.pem"   # resolved relative to ~/.windows-mcp/
ssl_keyfile  = "key.pem"

[security]
ip_allowlist        = ["192.168.1.0/24"]
cors_origins        = ["https://my-client.example.com"]   # optional — browser CORS opt-in
oauth_client_id     = "my-client"      # optional — enables OAuth 2.0 + PKCE
oauth_client_secret = "my-secret"

[tools]
exclude = ["PowerShell", "Registry"]   # disable specific tools
```

Place cert and key files in the same directory:

```
~/.windows-mcp/
├── config.toml
├── cert.pem
└── key.pem
```

Generate a self-signed cert directly into that directory:

```shell
mkdir -p ~/.windows-mcp
openssl req -x509 -newkey rsa:4096 \
  -keyout ~/.windows-mcp/key.pem \
  -out ~/.windows-mcp/cert.pem \
  -days 365 -nodes
```

### `auth` Helper

Generate an auth key and save a working config to `~/.windows-mcp/config.toml`:

```shell
windows-mcp auth
```

Generate auth plus a self-signed TLS certificate:

```shell
windows-mcp auth --transport streamable-http --host 0.0.0.0 --port 8000 --with-tls
```

This command writes the auth key into the config file, can generate `cert.pem` and `key.pem`, and prints an example MCP client configuration for the selected transport.

### SSRF Protection
`Scrape` tool blocks: private IPs, loopback, link-local, credentials-in-URLs, non-HTTP schemes.

---

## ⚙️ Environment Variables

All variables are optional unless noted. Set them via the `env` key in `claude_desktop_config.json` (or your MCP client's equivalent config).

### Screenshot & Snapshot

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_SCREENSHOT_SCALE` | `1.0` | Scale factor applied to screenshots before encoding. Accepts a float in the range `0.1`–`1.0`. Useful on high-resolution displays (1440p, 4K) where the default produces images that exceed Claude Desktop's 1 MB tool-result limit. Set to `0.5` to halve both dimensions (quarter the file size). |
| `WINDOWS_MCP_SCREENSHOT_BACKEND` | `auto` | Screenshot capture backend. Accepted values: `auto` (tries dxcam → mss → pillow in order), `dxcam`, `mss`, `pillow`. Use `mss` or `pillow` if `dxcam` is unavailable or causes issues on your GPU. |
| `WINDOWS_MCP_PROFILE_SNAPSHOT` | _(disabled)_ | Set to `1`, `true`, `yes`, or `on` to emit per-stage timing logs for Screenshot/Snapshot calls. Useful for diagnosing slow captures. |
| `WINDOWS_MCP_DISABLE_FLASH` | _(disabled)_ | Set to `1`, `true`, `yes`, or `on` to suppress the orange-red glowing border that briefly highlights the captured area after every screenshot. The flash is rendered on a transparent always-on-top window *after* capture so it never appears in the captured image. |

### Security

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_AUTH_KEY` | _(none)_ | Bearer token required on all HTTP requests. Alternative to `--auth-key` CLI flag. |
| `WINDOWS_MCP_IP_ALLOWLIST` | _(none)_ | Comma-separated list of allowed client IPs or CIDR ranges (e.g., `203.0.113.0/24,198.51.100.5`). Alternative to `--ip-allowlist` CLI flag. |
| `WINDOWS_MCP_CORS_ORIGINS` | _(none)_ | Comma-separated list of origins permitted to make cross-origin browser requests (e.g., `https://my-client.example.com`). No CORS headers are emitted when unset. Alternative to `--cors-origins` CLI flag. |
| `WINDOWS_MCP_TOOLS` | _(all enabled)_ | Comma-separated explicit list of tools to enable (e.g., `Screenshot,Click,Snapshot`). Alternative to `--tools` CLI flag. |
| `WINDOWS_MCP_EXCLUDE_TOOLS` | _(none)_ | Comma-separated list of tools to disable (e.g., `PowerShell,Registry`). Alternative to `--exclude-tools` CLI flag. |
| `WINDOWS_MCP_SSL_CERTFILE` | _(none)_ | Path to TLS certificate file (.pem) for HTTPS. Must be provided with `WINDOWS_MCP_SSL_KEYFILE`. |
| `WINDOWS_MCP_SSL_KEYFILE` | _(none)_ | Path to TLS private key file (.pem) for HTTPS. Must be provided with `WINDOWS_MCP_SSL_CERTFILE`. |
| `WINDOWS_MCP_OAUTH_CLIENT_ID` | _(none)_ | OAuth client ID for HTTP transports. Must be provided with `WINDOWS_MCP_OAUTH_CLIENT_SECRET`. |
| `WINDOWS_MCP_OAUTH_CLIENT_SECRET` | _(none)_ | OAuth client secret for HTTP transports. Must be provided with `WINDOWS_MCP_OAUTH_CLIENT_ID`. |
| `WINDOWS_MCP_STATELESS_HTTP` | `false` | Set to `1`, `true`, `yes`, or `on` to run `streamable-http` without `Mcp-Session-Id` connection state. Useful for reconnects after restarts and for horizontally scaled deployments. |

[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/cursortouch-windows-mcp-badge.png)](https://mseep.ai/app/cursortouch-windows-mcp)

### Telemetry

| Variable | Default | Description |
|---|---|---|
| `ANONYMIZED_TELEMETRY` | `true` | Set to `false` to disable anonymous usage telemetry. No personal data, tool arguments, or outputs are ever collected regardless of this setting. |
| `POSTHOG_API_KEY` | Project default | Override the PostHog project write key used for anonymous telemetry. Set to an empty string to skip PostHog client initialization. |
| `POSTHOG_HOST` | `https://us.i.posthog.com` | Override the PostHog host for anonymous telemetry, such as for a self-hosted PostHog deployment. |

### Debug

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_DEBUG` | `false` | Set to `1`, `true`, `yes`, or `on` to enable debug mode, which sets the log level to DEBUG for verbose output. Also available as the `--debug` CLI flag. |

### WatchDog

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_WATCHDOG` | `true` | Set to `off`, `0`, `false`, `no`, or `disabled` (case-insensitive) to disable the UIA focus watchdog that keeps the accessibility tree current. On unstable UIA environments the watchdog can degrade after long uptime (e.g. across a sleep/resume or session change); disabling it trades away automatic focus tracking for stability — the accessibility tree still refreshes on-demand for tool calls. |

**Example `claude_desktop_config.json`:**

Local (no security):
```json
{
  "mcpServers": {
    "windows-mcp": {
      "command": "uvx",
      "args": ["windows-mcp", "serve"],
      "env": { "WINDOWS_MCP_SCREENSHOT_SCALE": "0.5" }
    }
  }
}
```

Remote (with auth + IP allowlist + TLS):
```json
{
  "mcpServers": {
    "windows-mcp": {
      "command": "uvx",
      "args": ["windows-mcp", "serve", "--transport", "sse", "--host", "0.0.0.0"],
      "env": {
        "WINDOWS_MCP_AUTH_KEY": "your_token",
        "WINDOWS_MCP_IP_ALLOWLIST": "203.0.113.0/24",
        "WINDOWS_MCP_SSL_CERTFILE": "/path/to/cert.pem",
        "WINDOWS_MCP_SSL_KEYFILE": "/path/to/key.pem"
      }
    }
  }
}
```

---

## 🔨MCP Tools

MCP Client can access the following tools to interact with Windows:

- `Click`: Click on the screen at the given coordinates.
- `Type`: Type text on an element (optionally clears existing text).
- `Scroll`: Scroll vertically or horizontally on the window or specific regions.
- `Move`: Move mouse pointer or drag (set drag=True) to coordinates. For deterministic
  drag, set `from_loc=[x, y]` with `drag=True` to press at an explicit start point and
  release at `loc` in one tool call. Optional `duration` adds bounded intermediate
  movement.
- `Shortcut`: Press keyboard shortcuts (`Ctrl+c`, `Alt+Tab`, etc).
- `Wait`: Pause for a defined duration.
- `WaitFor`: Wait until text, an active window, an element, or a focused element appears by polling UI state inside one tool call.
- `DisplayInventory`: Read display layout, work areas, effective DPI, and scale metadata.
- `Screenshot`: Fast screenshot-first desktop capture with cursor position, active/open windows, and an image. Skips UI tree extraction for speed and should be the default first call when you mainly need visual context. Supports `display=[0]` or `display=[0,1]` using zero-based active Windows display indices, and `region=[left, top, right, bottom]` (virtual-desktop pixel coordinates) to capture just that rectangle instead of the whole screen — cheaper on tokens when you already know which area matters. `region` takes precedence over `display` when both are given; an invalid or out-of-bounds region raises an error. After capture, a brief orange-red glowing border is drawn inside the captured area as a visual confirmation (set `WINDOWS_MCP_DISABLE_FLASH=1` to disable).
- `Snapshot`: Full desktop state capture for workflows that need interactive element ids, scrollable regions, or `use_dom=True` browser extraction. Supports `use_vision=True` for including screenshots, `display=[0]` or `display=[0,1]` using zero-based active Windows display indices, and `region=[left, top, right, bottom]` (virtual-desktop pixel coordinates) to inspect just that rectangle instead of the whole screen; `region` takes precedence over `display` when both are given, and an invalid or out-of-bounds region raises an error.
- `App`: Launch an application by Start Menu name or strictly by executable path with separated argv and optional cwd; resize, move, and switch between windows.
- `PowerShell`: To execute PowerShell commands.
- `FileSystem`: Read, write, copy, move, delete, list, search, and inspect files and directories.
- `Scrape`: To scrape the entire webpage for information.
- `MultiSelect`: Select multiple items (files, folders, checkboxes) with optional Ctrl key. Uses bulk label-to-coordinate resolution when labels are provided.
- `MultiEdit`: Enter text into multiple input fields at specified coordinates. Uses bulk label-to-coordinate resolution when labels are provided.
- `Clipboard`: Read or set Windows clipboard content.
- `Process`: List running processes or terminate them by PID or name.
- `Notification`: Send a Windows toast notification with a title and message.
- `Registry`: Read, write, delete, or list Windows Registry values and keys.


## 🤝 Connect with Us
Stay updated and join our community:

- 📢 Follow us on [X](https://x.com/CursorTouch) for the latest news and updates

- 💬 Join our [Discord Community](https://discord.com/invite/Aue9Yj2VzS)

## Star History

[![Star History Chart](https://star-history.dera.page/svg?repos=CursorTouch/Windows-MCP&type=Date)](https://star-history.dera.page/#CursorTouch/Windows-MCP&Date)

## 👥 Contributors

Thanks to all the amazing people who have contributed to Windows-MCP! 🎉

<a href="https://github.com/CursorTouch/Windows-MCP/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=CursorTouch/Windows-MCP" />
</a>

We appreciate every contribution, whether it's code, documentation, bug reports, or feature suggestions. Want to contribute? Check out our [Contributing Guidelines](CONTRIBUTING)!

## 🔒 Security

**Important**: Windows-MCP operates with full system access and can perform irreversible operations. Please review our comprehensive security guidelines before deployment.

For detailed security information, including:
- Tool-specific risk assessments
- Deployment recommendations
- Vulnerability reporting procedures
- Compliance and auditing guidelines

Please read our [Security Policy](SECURITY.md).

## 📊 Telemetry

Windows-MCP collects usage data to help improve the MCP server. No personal information, no tool arguments, no outputs are tracked.

To disable telemetry, set `ANONYMIZED_TELEMETRY` to `false` in your MCP client configuration:

```json
{
  "mcpServers": {
    "windows-mcp": {
      "command": "uvx",
      "args": [
        "windows-mcp",
        "serve"
      ],
      "env": {
        "ANONYMIZED_TELEMETRY": "false"
      }
    }
  }
}
```

See the [Environment Variables](#-environment-variables) section for the full list of configurable options.

For detailed information on what data is collected and how it is handled, please refer to the [Telemetry and Data Privacy](SECURITY.md#telemetry-and-data-privacy) section in our Security Policy.

## 📝 Limitations

- Selecting specific sections of the text in a paragraph, as the MCP is relying on a11y tree. (⌛ Working on it.)
- `Type-Tool` is meant for typing text, not programming in IDE because of it types program as a whole in a file. (⌛ Working on it.)
- This MCP server can't be used to play video games 🎮.

## 🪪 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgements

Windows-MCP makes use of several excellent open-source projects that power its Windows automation features:

- [UIAutomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows)

Huge thanks to the maintainers and contributors of these libraries for their outstanding work and open-source spirit.

## 🤝Contributing

Contributions are welcome! Please see [CONTRIBUTING](CONTRIBUTING) for setup instructions and development guidelines.

Made with ❤️ by [CursorTouch](https://github.com/CursorTouch)

## Citation

```bibtex
@software{
  author       = {CursorTouch},
  title        = {Windows-MCP: Lightweight open-source project for integrating LLM agents with Windows},
  year         = {2024},
  publisher    = {GitHub},
  url={https://github.com/CursorTouch/Windows-MCP}
}
```
