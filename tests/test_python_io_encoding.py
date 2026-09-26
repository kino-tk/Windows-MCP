"""Python children get UTF-8 stdio by default, so Japanese output survives."""

import sys

import pytest

from windows_mcp.powershell import PowerShellExecutor
from windows_mcp.powershell.service import _build_invocation

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")

PY = sys.executable.replace("'", "''")
JP = "日本語OK"


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.setenv("WINDOWS_MCP_JOB_RETURN_AFTER", "5")
    monkeypatch.setenv("WINDOWS_MCP_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("WINDOWS_MCP_CALLLOG", "off")


def test_default_is_utf8_when_unset():
    _, env, _ = _build_invocation("'x'", None)
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_host_value_is_kept(monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "utf-16")
    _, env, _ = _build_invocation("'x'", None)
    assert env["PYTHONIOENCODING"] == "utf-16"


@pytest.mark.parametrize("path", ["normal", "job"])
def test_japanese_from_python_is_not_garbled(path):
    cmd = f"& '{PY}' -c \"print('{JP}')\""
    if path == "normal":
        out, status = PowerShellExecutor.execute_command(cmd, 30)
    else:
        out, status, job_id = PowerShellExecutor.run(cmd, timeout=600)
        assert job_id is None
    assert status == 0
    assert out.strip() == JP


def test_command_can_still_override():
    cmd = f"$env:PYTHONIOENCODING='cp932'; & '{PY}' -c \"import sys; print(sys.stdout.encoding)\""
    out, status = PowerShellExecutor.execute_command(cmd, 30)
    assert status == 0 and out.strip() == "cp932"
