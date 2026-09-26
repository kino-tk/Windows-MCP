"""A job's processes never outlive their server unsupervised, and every job
leaves a record that the next server can verify and clean up."""

import json
import os
import subprocess
import sys
import textwrap
import time

import psutil
import pytest

from windows_mcp.powershell import PowerShellExecutor, jobs

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")

# The real interpreter, not the venv redirector: a venv python.exe on Windows
# is a launcher that runs the base interpreter as a child in its own Job
# Object, which would blur what these tests observe.
PY = getattr(sys, "_base_executable", sys.executable)


def _server_env():
    """Environment in which the base interpreter can import windows_mcp.

    PYTHONPATH alone does not process .pth files, which pywin32 needs, so the
    helper calls site.addsitedir() on the directories passed here. It runs
    with -S so the base interpreter's own site-packages stay out of the way.
    """
    import site

    import windows_mcp

    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(windows_mcp.__file__))
    env["TEST_SITE_DIRS"] = os.pathsep.join(site.getsitepackages())
    return env

TREE = textwrap.dedent(
    """
    import signal, subprocess, sys, time
    mode = sys.argv[1]
    if mode == "grand":
        # Like many real tools, ignore CTRL_BREAK, so only a real tree kill ends it.
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        time.sleep(120)
    elif mode == "mid":
        p = subprocess.Popen([sys.executable, __file__, "grand"])
        print(p.pid, flush=True)
    else:
        m = subprocess.Popen([sys.executable, __file__, "mid"], stdout=subprocess.PIPE, text=True)
        g = m.stdout.readline().strip()
        m.wait()
        print("grand", g, flush=True)
        if mode == "root-exit":
            sys.exit(0)
        if mode == "root-exit-late":
            time.sleep(3)  # outlive the first call, then exit and leave the grandchild
            sys.exit(0)
        time.sleep(120)
    """
)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    jobdir = tmp_path / "jobs"
    monkeypatch.setenv("WINDOWS_MCP_JOB_RETURN_AFTER", "2")
    monkeypatch.setenv("WINDOWS_MCP_JOB_DIR", str(jobdir))
    monkeypatch.setenv("WINDOWS_MCP_CALLLOG", str(tmp_path / "calls.log"))
    tree = tmp_path / "tree.py"
    tree.write_text(TREE, encoding="utf-8")
    spawned: list[int] = []
    yield {"jobdir": jobdir, "tree": tree, "spawned": spawned, "tmp": tmp_path}
    jobs.stop_sweeper()
    for job in list(jobs._jobs.values()):
        if not job.done.is_set():
            jobs.kill(job)
        jobs.forget(job)
    for pid in spawned:
        _reap(pid)


def _reap(pid):
    try:
        proc = psutil.Process(pid)
        for c in proc.children(recursive=True):
            c.kill()
        proc.kill()
    except psutil.Error:
        pass


def _alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _gone_within(pids, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.2)
    return False


def _tree_cmd(tree, mode="root"):
    return f"& '{PY}' -u '{tree}' {mode}"


def _grand_pid(job, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in jobs.read_output(job)[0].splitlines():
            if line.startswith("grand "):
                return int(line.split()[1])
        time.sleep(0.2)
    raise AssertionError("grandchild pid not seen")


def _start(env, mode="root", timeout=600):
    out, status, job_id = PowerShellExecutor.run(_tree_cmd(env["tree"], mode), timeout=timeout)
    assert job_id, out
    job = jobs.get(job_id)
    grand = _grand_pid(job)
    env["spawned"].append(grand)
    return job, grand


def _meta(job):
    return json.loads(job.meta_path.read_text(encoding="utf-8"))


def _dead_identity():
    """PID and creation time of a process that has already exited."""
    p = subprocess.Popen([PY, "-c", "import time; time.sleep(0.5)"])
    created = psutil.Process(p.pid).create_time()
    p.wait()
    return p.pid, created


def _write_foreign_meta(jobdir, *, job_no, server, pid, pid_created, state="running", ended=None, extra=None):
    jobdir.mkdir(parents=True, exist_ok=True)
    stem = f"{server[0]}-job-{job_no}"
    (jobdir / f"{stem}.out").write_bytes(b"previous output\n")
    (jobdir / f"{stem}.err").write_text("", encoding="utf-8")
    data = {
        "version": 1, "id": f"job-{job_no}", "server_pid": server[0], "server_created": server[1],
        "pid": pid, "pid_created": pid_created, "command": "long-task", "hard_timeout": 600,
        "started": time.time() - 100, "state": state, "returncode": None, "ended": ended, "note": "",
        "job_object": True, "adopted": False, "out": f"{stem}.out", "err": f"{stem}.err",
    }
    data.update(extra or {})
    (jobdir / f"{stem}.json").write_text(json.dumps(data), encoding="utf-8")
    return jobdir / stem


# --- the whole tree is always under control -----------------------------------


def test_kill_ends_grandchild_whose_parent_already_exited(env):
    job, grand = _start(env)
    assert _alive(grand)
    jobs.kill(job)
    assert job.state == "killed"
    assert _gone_within([job.pid, grand], 5), "orphaned grandchild survived kill"


def test_hard_timeout_ends_grandchild_too(env):
    job, grand = _start(env, timeout=6)
    assert job.done.wait(20)
    assert job.state == "timed_out"
    assert _gone_within([job.pid, grand], 5), "orphaned grandchild survived the hard timeout"


def test_server_death_ends_the_job_tree(env):
    """Kill the owning process outright: the OS must end the job's tree."""
    helper = env["tmp"] / "server.py"
    helper.write_text(
        textwrap.dedent(
            f"""
            import os, site, sys, time
            for d in os.environ["TEST_SITE_DIRS"].split(os.pathsep):
                site.addsitedir(d)
            from windows_mcp.powershell import PowerShellExecutor, jobs
            out, status, job_id = PowerShellExecutor.run({_tree_cmd(env['tree'])!r}, timeout=600)
            job = jobs.get(job_id)
            while "grand " not in jobs.read_output(job)[0]:
                time.sleep(0.2)
            grand = [l for l in jobs.read_output(job)[0].splitlines() if l.startswith("grand ")][0].split()[1]
            print(job.pid, grand, job.meta_path, flush=True)
            time.sleep(120)
            """
        ),
        encoding="utf-8",
    )
    server = subprocess.Popen(
        [PY, "-S", "-u", str(helper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_server_env()
    )
    env["spawned"].append(server.pid)
    first = server.stdout.readline()
    if not first:
        pytest.fail("helper server failed to start:\n" + server.stderr.read()[-2000:])
    root, grand, meta_path = first.split(maxsplit=2)
    root, grand = int(root), int(grand)
    env["spawned"] += [root, grand]
    assert _alive(root) and _alive(grand)
    psutil.Process(server.pid).kill()  # no cleanup code runs in the "server"
    assert _gone_within([root, grand], 10), "job processes outlived their server"
    assert json.loads(open(meta_path.strip(), encoding="utf-8").read())["state"] == "running"

    counts = jobs.reconcile()
    assert counts["adopted"] == 1 and counts["killed"] == 0
    adopted = next(j for j in jobs.list_jobs() if j.adopted)
    assert adopted.state == "killed" and "together with the server" in adopted.note
    assert "grand" in jobs.read_output(adopted)[0]  # output still readable


def test_normal_exit_leaves_deliberate_background_process_running(env):
    """Same as a plain call: what the command leaves behind is not killed."""
    out, status, job_id = PowerShellExecutor.run(_tree_cmd(env["tree"], "root-exit"), timeout=600)
    assert job_id is None and status == 0, out
    grand = int([l for l in out.splitlines() if l.startswith("grand ")][0].split()[1])
    env["spawned"].append(grand)
    time.sleep(1.0)
    assert _alive(grand), "a process the command left running was killed when the job ended"


def test_shutdown_stops_running_jobs_and_records_why(env):
    job, grand = _start(env)
    jobs.shutdown()
    assert job.done.is_set() and job.state == "killed"
    assert "shut down" in job.note
    assert _gone_within([job.pid, grand], 5)
    assert _meta(job)["state"] == "killed"


# --- records ------------------------------------------------------------------


def test_metadata_tracks_the_job(env):
    job, grand = _start(env)
    meta = _meta(job)
    assert meta["state"] == "running" and meta["pid"] == job.pid
    assert abs(meta["pid_created"] - psutil.Process(job.pid).create_time()) < 0.01
    assert meta["server_pid"] == os.getpid() and meta["job_object"] is True
    assert meta["hard_timeout"] == 600 and "tree.py" in meta["command"]
    jobs.kill(job)
    meta = _meta(job)
    assert meta["state"] == "killed" and meta["ended"] is not None


def test_forget_deletes_output_and_metadata(env):
    job, grand = _start(env)
    jobs.kill(job)
    paths = [job.out_path, job.err_path, job.meta_path]
    assert all(p.exists() for p in paths)
    jobs.forget(job)
    assert not any(p.exists() for p in paths)


# --- reconcile at server start ------------------------------------------------


def test_reconcile_kills_a_verified_survivor(env):
    survivor = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"])
    env["spawned"].append(survivor.pid)
    _write_foreign_meta(
        env["jobdir"], job_no=3, server=_dead_identity(),
        pid=survivor.pid, pid_created=psutil.Process(survivor.pid).create_time(),
    )
    counts = jobs.reconcile()
    assert counts["killed"] == 1 and counts["adopted"] == 1
    assert _gone_within([survivor.pid], 5)
    job = jobs.get("job-3")
    assert job.adopted and job.state == "killed" and "next server start" in job.note
    assert jobs.read_output(job)[0] == "previous output\n"


def test_reconcile_never_kills_a_reused_pid(env):
    innocent = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"])
    env["spawned"].append(innocent.pid)
    _write_foreign_meta(
        env["jobdir"], job_no=4, server=_dead_identity(),
        pid=innocent.pid, pid_created=psutil.Process(innocent.pid).create_time() - 3600,
    )
    counts = jobs.reconcile()
    assert counts["killed"] == 0
    time.sleep(0.5)
    assert _alive(innocent.pid), "a process that merely reused the PID was killed"
    assert jobs.get("job-4").state == "killed"


def test_reconcile_leaves_a_live_servers_jobs_alone(env):
    other_server = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"])
    env["spawned"].append(other_server.pid)
    worker = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"])
    env["spawned"].append(worker.pid)
    stem = _write_foreign_meta(
        env["jobdir"], job_no=5,
        server=(other_server.pid, psutil.Process(other_server.pid).create_time()),
        pid=worker.pid, pid_created=psutil.Process(worker.pid).create_time(),
    )
    counts = jobs.reconcile()
    assert counts == {"adopted": 0, "killed": 0, "foreign_live": 1, "stray_files_removed": 0}
    assert _alive(worker.pid) and jobs.get("job-5") is None
    assert all((stem.parent / f"{stem.name}{s}").exists() for s in (".out", ".err", ".json"))


def test_reconcile_removes_stray_files_and_expired_jobs(env):
    jobdir = env["jobdir"]
    old_ended = time.time() - jobs.RETENTION_SECONDS - 60
    expired = _write_foreign_meta(
        jobdir, job_no=6, server=_dead_identity(), pid=0, pid_created=None,
        state="exited", ended=old_ended, extra={"returncode": 0},
    )
    recent = _write_foreign_meta(
        jobdir, job_no=7, server=_dead_identity(), pid=0, pid_created=None,
        state="exited", ended=time.time() - 60, extra={"returncode": 0},
    )
    stray_old = jobdir / "999-job-1.out"
    stray_old.write_text("x")
    an_hour_ago = time.time() - 3600
    os.utime(stray_old, (an_hour_ago, an_hour_ago))
    stray_new = jobdir / "999-job-2.out"
    stray_new.write_text("y")

    counts = jobs.reconcile()
    assert counts["stray_files_removed"] == 1
    assert not stray_old.exists() and stray_new.exists()
    assert not any((expired.parent / f"{expired.name}{s}").exists() for s in (".out", ".err", ".json"))
    assert jobs.get("job-6") is None
    assert jobs.get("job-7").state == "exited" and jobs.get("job-7").returncode == 0
    assert all((recent.parent / f"{recent.name}{s}").exists() for s in (".out", ".err", ".json"))


def test_job_numbers_continue_after_a_restart(env):
    _write_foreign_meta(env["jobdir"], job_no=41, server=_dead_identity(), pid=0, pid_created=None,
                        state="exited", ended=time.time(), extra={"returncode": 0})
    jobs.reconcile()
    job, grand = _start(env)
    assert int(job.id.split("-")[1]) >= 42


# --- no depth limit, no escape ------------------------------------------------

CHAIN = textwrap.dedent(
    """
    import os, signal, subprocess, sys, time
    depth, maxd = int(sys.argv[1]), int(sys.argv[2])
    if depth < maxd:
        p = subprocess.Popen([sys.executable, __file__, str(depth + 1), sys.argv[2]], stdout=subprocess.PIPE, text=True)
        rest = p.stdout.readline().strip()
        print(os.getpid(), rest, flush=True)
        if depth >= 1:
            sys.exit(0)  # every intermediate generation exits, leaving orphans below it
        time.sleep(120)
    else:
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        print(os.getpid(), flush=True)
        time.sleep(120)
    """
)


def test_kill_ends_every_generation(env):
    chain = env["tmp"] / "chain.py"
    chain.write_text(CHAIN, encoding="utf-8")
    out, status, job_id = PowerShellExecutor.run(f"& '{PY}' -u '{chain}' 0 6", timeout=600)
    job = jobs.get(job_id)
    deadline = time.monotonic() + 20
    pids = []
    while len(pids) < 7 and time.monotonic() < deadline:
        first = jobs.read_output(job)[0].strip().splitlines()
        pids = [int(x) for x in first[0].split()] if first else []
        time.sleep(0.2)
    assert len(pids) == 7, pids
    env["spawned"].extend(pids)
    deepest = pids[-1]
    assert _alive(deepest) and not any(_alive(p) for p in pids[1:-1])
    jobs.kill(job)
    assert _gone_within(pids, 5), "a descendant several generations down survived"


def test_a_child_cannot_break_away(env):
    script = env["tmp"] / "breakaway.py"
    script.write_text(
        "import subprocess, sys\n"
        "try:\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=0x01000000)\n"
        "    print('escaped')\n"
        "except OSError as e:\n"
        "    print('refused', e.winerror)\n",
        encoding="utf-8",
    )
    out, status, job_id = PowerShellExecutor.run(
        f"& '{PY}' -u '{script}'; Start-Sleep 3", timeout=600
    )
    job = jobs.get(job_id)
    assert job.done.wait(20)
    text = jobs.read_output(job)[0]
    assert "refused 5" in text and "escaped" not in text


# --- leftovers of a finished job stay under control ---------------------------


def test_leftovers_of_a_long_job_stay_bound_and_kill_ends_them(env):
    job, grand = _start(env, mode="root-exit-late")
    assert job.done.wait(20) and job.state == "exited"
    assert _alive(grand)
    assert jobs.leftovers(job) >= 1
    jobs.kill(job)
    assert _gone_within([grand], 5), "leftover survived kill of a finished job"
    assert jobs.leftovers(job) == 0 and "leftover" in job.note


def test_deleting_a_job_ends_its_leftovers(env):
    job, grand = _start(env, mode="root-exit-late")
    assert job.done.wait(20)
    assert _alive(grand)
    jobs.forget(job)
    assert _gone_within([grand], 5), "leftover survived deletion of its job"


def test_root_killed_from_outside_then_server_death_ends_everything(env):
    """Regression: an external kill of the root must not release the tree."""
    helper = env["tmp"] / "server2.py"
    helper.write_text(
        textwrap.dedent(
            f"""
            import os, site, sys, time
            for d in os.environ["TEST_SITE_DIRS"].split(os.pathsep):
                site.addsitedir(d)
            from windows_mcp.powershell import PowerShellExecutor, jobs
            out, status, job_id = PowerShellExecutor.run({_tree_cmd(env['tree'])!r}, timeout=600)
            job = jobs.get(job_id)
            while "grand " not in jobs.read_output(job)[0]:
                time.sleep(0.2)
            grand = [l for l in jobs.read_output(job)[0].splitlines() if l.startswith("grand ")][0].split()[1]
            print(job.pid, grand, flush=True)
            job.done.wait(60)
            print("root-ended", job.state, jobs.leftovers(job), flush=True)
            time.sleep(120)
            """
        ),
        encoding="utf-8",
    )
    server = subprocess.Popen(
        [PY, "-S", "-u", str(helper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_server_env()
    )
    env["spawned"].append(server.pid)
    first = server.stdout.readline()
    if not first:
        pytest.fail("helper server failed to start:\n" + server.stderr.read()[-2000:])
    root, grand = (int(x) for x in first.split())
    env["spawned"] += [root, grand]
    subprocess.run(["taskkill", "/PID", str(root), "/T", "/F"], capture_output=True)
    ended = server.stdout.readline().split()
    assert ended[0] == "root-ended" and int(ended[2]) >= 1, ended
    assert _alive(grand), "the orphaned grandchild should still be bound, not dead yet"
    psutil.Process(server.pid).kill()
    assert _gone_within([grand], 10), "a leftover outlived its server after the root was killed externally"


# --- scheduled cleanup --------------------------------------------------------


def test_sweeper_cleans_up_without_any_other_activity(env, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_JOB_RETENTION_HOURS", str(1 / 3600))  # 1 s
    monkeypatch.setenv("WINDOWS_MCP_JOB_SWEEP_INTERVAL", "0.3")
    out, status, job_id = PowerShellExecutor.run(f"& '{PY}' -c \"import time; time.sleep(3)\"", timeout=600)
    job = jobs.get(job_id)
    assert job.done.wait(20)
    files = [job.out_path, job.err_path, job.meta_path]
    assert all(p.exists() for p in files)
    stray = env["jobdir"] / "999-job-9.out"
    stray.write_text("x")
    old = time.time() - jobs.ORPHAN_FILE_GRACE_SECONDS - 60
    os.utime(stray, (old, old))

    jobs.start_sweeper()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (any(p.exists() for p in files) or stray.exists()):
        time.sleep(0.1)
    assert not any(p.exists() for p in files), "expired job not swept on schedule"
    assert not stray.exists(), "stray file not swept on schedule"
    assert jobs.get(job_id) is None


def test_sweeper_is_idempotent_and_stops(env, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_JOB_SWEEP_INTERVAL", "0.2")
    jobs.start_sweeper()
    first = jobs._sweeper
    jobs.start_sweeper()
    assert jobs._sweeper is first and first.is_alive()
    jobs.stop_sweeper()
    first.join(2)
    assert not first.is_alive() and jobs._sweeper is None


def test_cleanup_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("WINDOWS_MCP_JOB_SWEEP_INTERVAL", raising=False)
    monkeypatch.delenv("WINDOWS_MCP_JOB_RETENTION_HOURS", raising=False)
    assert jobs.sweep_interval() == 5400  # 90 minutes
    assert jobs.retention_seconds() == 6 * 3600
    monkeypatch.setenv("WINDOWS_MCP_JOB_SWEEP_INTERVAL", "600")
    monkeypatch.setenv("WINDOWS_MCP_JOB_RETENTION_HOURS", "24")
    assert jobs.sweep_interval() == 600 and jobs.retention_seconds() == 24 * 3600
    monkeypatch.setenv("WINDOWS_MCP_JOB_SWEEP_INTERVAL", "not a number")
    assert jobs.sweep_interval() == 5400
