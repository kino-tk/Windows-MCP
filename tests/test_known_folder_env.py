"""Known folders (ProgramData and its siblings) are synthesized by Windows, not
stored in the registry. A packaged MCP host can hand down an environment block
without them; _prepare_env() must put them back, without duplicating names."""

import shutil
import sys
from collections import Counter

import pytest

from windows_mcp.powershell import PowerShellExecutor
from windows_mcp.powershell.service import _known_folder_path, _prepare_env

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")

PROGRAM_DATA = "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}"


@pytest.fixture
def stripped(monkeypatch):
    """Simulate a packaged host: ProgramData and ALLUSERSPROFILE are missing."""
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    monkeypatch.delenv("ALLUSERSPROFILE", raising=False)


def _get(env: dict[str, str], name: str) -> list[str]:
    return [value for key, value in env.items() if key.upper() == name.upper()]


def test_known_folder_resolves():
    path = _known_folder_path(PROGRAM_DATA)
    assert path and path.lower().endswith("programdata")


def test_unknown_folder_id_gives_none():
    assert _known_folder_path("not-a-guid") is None


def test_missing_folders_are_restored(stripped):
    env = _prepare_env()
    expected = _known_folder_path(PROGRAM_DATA)
    assert _get(env, "ProgramData") == [expected]
    assert _get(env, "ALLUSERSPROFILE") == [expected]


def test_present_folders_are_kept_and_not_duplicated(monkeypatch):
    monkeypatch.setenv("PROGRAMDATA", r"D:\CustomData")
    env = _prepare_env()
    assert _get(env, "ProgramData") == [r"D:\CustomData"]
    counts = Counter(key.upper() for key in env)
    for name in ("PROGRAMDATA", "ALLUSERSPROFILE", "PUBLIC", "PROGRAMFILES", "PROGRAMW6432"):
        assert counts[name] <= 1, name


@pytest.mark.skipif(shutil.which("ssh") is None, reason="OpenSSH client not installed")
def test_ssh_starts_without_programdata_in_the_host_env(stripped):
    # Win32 OpenSSH exits 255 with no output at all when ProgramData is absent.
    out, status = PowerShellExecutor.execute_command("ssh -V 2>&1 | Out-String", 30)
    assert status == 0
    assert "OpenSSH" in out
