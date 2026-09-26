<#
.SYNOPSIS
    List Windows-MCP PowerShell jobs from their records, and kill one as a last resort.

.DESCRIPTION
    Reads every job record (<server pid>-<job id>.json) in the job directory and
    prints everything it holds, plus two live checks:

      Running  True only if a process with the recorded PID exists AND its start
               time matches the recorded creation time. A PID that Windows has
               reused for an unrelated process shows as False.
      Server   Whether the Windows-MCP server that owns the job is still running,
               checked the same way.

    Use this when "PowerShellJob action=kill" did not stop a job, or when the
    server no longer answers. Normally it is never needed: every job is bound to
    its server by a Windows Job Object and ends when the server ends.

    With -Kill, the job's process tree is ended with "taskkill /PID <pid> /T /F",
    but only after the Running check passes. taskkill /T follows parent links, so
    a descendant whose parent has already exited is not reached. Such a process
    stays bound to the job: "PowerShellJob action=kill job_id=<id>" ends it, and
    it ends when the job is deleted or the server exits (restart Claude Desktop).

.PARAMETER JobDir
    The job directory. Default: $env:WINDOWS_MCP_JOB_DIR, else ~\.windows-mcp\jobs.

.PARAMETER Kill
    A job id (for example job-3) or a record name (for example 1234-job-3).
    If two records share the id, use the record name.

.PARAMETER Tail
    Also print the last N lines of each job's stdout and stderr.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\windows-mcp-jobs.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\windows-mcp-jobs.ps1 -Kill job-3
#>
param(
    [string]$JobDir = "",
    [string]$Kill = "",
    [int]$Tail = 0
)

if (-not $JobDir) {
    if ($env:WINDOWS_MCP_JOB_DIR) { $JobDir = $env:WINDOWS_MCP_JOB_DIR }
    else { $JobDir = Join-Path $HOME ".windows-mcp\jobs" }
}

function ConvertFrom-Epoch($t) {
    if ($null -eq $t) { return $null }
    return [DateTimeOffset]::FromUnixTimeMilliseconds([int64]([double]$t * 1000)).LocalDateTime
}

function Test-SameProcess($procId, $created) {
    if (-not $procId -or $null -eq $created) { return $false }
    $p = Get-Process -Id ([int]$procId) -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    try { $start = ([DateTimeOffset]$p.StartTime).ToUnixTimeMilliseconds() / 1000.0 } catch { return $false }
    return ([math]::Abs($start - [double]$created) -lt 1.0)
}

function Get-Tree([int]$root) {
    # Descendants reachable through parent links (what taskkill /T reaches).
    $all = @(Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, Name, CreationDate)
    $found = @()
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($root)
    while ($queue.Count -gt 0) {
        $cur = $queue.Dequeue()
        $parent = $all | Where-Object { $_.ProcessId -eq $cur } | Select-Object -First 1
        foreach ($c in @($all | Where-Object { $_.ParentProcessId -eq $cur })) {
            # A child cannot be older than its parent; older ones only reuse the parent's PID.
            if ($parent -and $c.CreationDate -lt $parent.CreationDate) { continue }
            $found += $c
            $queue.Enqueue([int]$c.ProcessId)
        }
    }
    return $found
}

if (-not (Test-Path -LiteralPath $JobDir)) {
    Write-Output "No job directory: $JobDir"
    exit 0
}

$records = @()
foreach ($f in Get-ChildItem -LiteralPath $JobDir -Filter *.json | Where-Object { $_.Name -match '^\d+-job-\d+\.json$' } | Sort-Object Name) {
    try {
        $d = Get-Content -LiteralPath $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Warning "Unreadable record: $($f.FullName)"
        continue
    }
    $stem = $f.BaseName
    $outFile = Join-Path $JobDir ([string]$d.out)
    $errFile = Join-Path $JobDir ([string]$d.err)
    $records += [pscustomobject]@{
        Id          = $d.id
        Record      = $stem
        State       = $d.state
        Running     = (Test-SameProcess $d.pid $d.pid_created)
        Pid         = $d.pid
        PidStarted  = ConvertFrom-Epoch $d.pid_created
        PidCreatedEpoch = $d.pid_created
        ExitCode    = $d.returncode
        Started     = ConvertFrom-Epoch $d.started
        Ended       = ConvertFrom-Epoch $d.ended
        HardTimeout = $d.hard_timeout
        ServerPid   = $d.server_pid
        Server      = $(if (Test-SameProcess $d.server_pid $d.server_created) { "running" } else { "gone" })
        JobObject   = $d.job_object
        Adopted     = $d.adopted
        Command     = $d.command
        Note        = $d.note
        OutFile     = $outFile
        OutBytes    = $(if (Test-Path -LiteralPath $outFile) { (Get-Item -LiteralPath $outFile).Length } else { $null })
        ErrFile     = $errFile
        ErrBytes    = $(if (Test-Path -LiteralPath $errFile) { (Get-Item -LiteralPath $errFile).Length } else { $null })
        RecordFile  = $f.FullName
    }
}

if (-not $Kill) {
    if ($records.Count -eq 0) { Write-Output "No job records in $JobDir"; exit 0 }
    foreach ($r in $records) {
        $r | Format-List | Out-String -Width 200 | Write-Output
        if ($Tail -gt 0) {
            Write-Output "--- last $Tail lines of stdout ---"
            if (Test-Path -LiteralPath $r.OutFile) { Get-Content -LiteralPath $r.OutFile -Tail $Tail -Encoding UTF8 }
            Write-Output "--- last $Tail lines of stderr ---"
            if (Test-Path -LiteralPath $r.ErrFile) { Get-Content -LiteralPath $r.ErrFile -Tail $Tail -Encoding UTF8 }
            Write-Output ""
        }
    }
    exit 0
}

$target = @($records | Where-Object { $_.Record -eq $Kill })
if ($target.Count -eq 0) { $target = @($records | Where-Object { $_.Id -eq $Kill }) }
if ($target.Count -eq 0) { Write-Output "No record for '$Kill'."; exit 1 }
if ($target.Count -gt 1) {
    Write-Output "Several records have id '$Kill'; pass the record name instead:"
    $target | ForEach-Object { Write-Output "  $($_.Record)  state=$($_.State) running=$($_.Running)" }
    exit 1
}
$job = $target[0]
if (-not $job.Running) {
    Write-Output "$($job.Id): nothing to kill. PID $($job.Pid) is not running as the recorded process (state: $($job.State))."
    exit 0
}

$tree = @(Get-Tree ([int]$job.Pid))
Write-Output "$($job.Id): killing PID $($job.Pid) and $($tree.Count) descendant(s) reachable through parent links:"
$tree | ForEach-Object { Write-Output "  $($_.ProcessId)  $($_.Name)" }
& taskkill /PID $job.Pid /T /F
Start-Sleep -Seconds 1
$left = @($tree | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
if (Test-SameProcess $job.Pid $job.PidCreatedEpoch) {
    Write-Output "PID $($job.Pid) is still running. Restart Claude Desktop: closing the server ends its Job Object and every process in it."
    exit 1
}
if ($left.Count -gt 0) {
    Write-Output "Still running: $(($left | ForEach-Object { $_.ProcessId }) -join ', ')"
    exit 1
}
Write-Output "Done. Descendants whose parent had already exited are not reachable through parent links. They stay bound to the job: 'PowerShellJob action=kill job_id=$($job.Id)' ends them, and they end when the job is deleted or the server exits (restart Claude Desktop)."
exit 0
