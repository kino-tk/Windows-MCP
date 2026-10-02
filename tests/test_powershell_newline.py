"""Line endings and BOM of text PowerShell writes (WINDOWS_MCP_POWERSHELL_NEWLINE)."""

import base64
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from windows_mcp.powershell import PowerShellExecutor, newline
from windows_mcp.powershell.service import _build_invocation

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")

needs_pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")

PY = sys.executable.replace("'", "''")
# Echoes the raw stdin bytes as hex, so the test sees exactly what was sent.
READ_STDIN = "import sys; print(sys.stdin.buffer.read().hex())"
JP = "日本"


@pytest.fixture(scope="session")
def cache_root(tmp_path_factory):
    # Shared by the tests so the encoding is compiled once per session.
    return tmp_path_factory.mktemp("newline-cache")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, cache_root):
    monkeypatch.setenv("WINDOWS_MCP_JOB_RETURN_AFTER", "5")
    monkeypatch.setenv("WINDOWS_MCP_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("WINDOWS_MCP_CALLLOG", str(tmp_path / "calls.log"))
    monkeypatch.delenv(newline.MODE_ENV, raising=False)
    monkeypatch.setattr(newline, "cache_dir", lambda: cache_root)
    newline._failed.clear()


def script_of(args):
    return base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16le")


def run(cmd, path="normal"):
    if path == "normal":
        out, status = PowerShellExecutor.execute_command(cmd, 60)
    else:
        out, status, job_id = PowerShellExecutor.run(cmd, timeout=600)
        assert job_id is None
    assert status == 0, out
    return out


def stdin_bytes(expr, path="normal"):
    out = run(f"{expr} | & '{PY}' -c '{READ_STDIN}'", path)
    return bytes.fromhex(out.strip().splitlines()[-1])


def file_bytes(tmp_path, template, path="normal"):
    target = tmp_path / "out.txt"
    run(template.replace("{f}", "'" + str(target).replace("'", "''") + "'"), path)
    return target.read_bytes()


# --- mode and preamble -------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, newline.DEFAULT_MODE),
    ("lf", "lf"),
    (" LF ", "lf"),
    ("native", "native"),
    ("crlf", newline.DEFAULT_MODE),
])
def test_mode(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv(newline.MODE_ENV, value)
    assert newline.mode() == expected


def test_native_mode_drops_the_bom_encoding(monkeypatch):
    monkeypatch.setenv(newline.MODE_ENV, "native")
    args, _, _ = _build_invocation("'x'", None)
    script = script_of(args)
    assert script.startswith(newline.NATIVE_PREAMBLE)
    assert "$OutputEncoding = [System.Text.Encoding]::UTF8" not in script
    assert "LoadFrom" not in script


def test_lf_mode_on_windows_powershell_only_drops_the_bom(monkeypatch):
    monkeypatch.setenv(newline.MODE_ENV, "lf")
    assert newline.preamble("powershell", {}) == newline.NATIVE_PREAMBLE
    assert newline.preamble(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", {}) \
        == newline.NATIVE_PREAMBLE


def test_lf_preamble_quotes_the_dll_path():
    text = newline.lf_preamble(Path(r"C:\it's\lf.dll"))
    assert r"LoadFrom('C:\it''s\lf.dll')" in text
    for name in ("Out-File", "Set-Content", "Add-Content", "Tee-Object", "Export-Csv"):
        assert f"$PSDefaultParameterValues['{name}:Encoding'] = $OutputEncoding;" in text


def test_build_failure_falls_back_and_is_logged_once(tmp_path, monkeypatch):
    monkeypatch.setenv(newline.MODE_ENV, "lf")
    monkeypatch.setattr(newline, "cache_dir", lambda: tmp_path / "empty-cache")
    calls = []

    def broken(exe, dll, env):
        calls.append(dll)
        raise RuntimeError("compiler missing")

    monkeypatch.setattr(newline, "_build", broken)
    assert newline.preamble("pwsh", {}) == newline.NATIVE_PREAMBLE
    assert newline.preamble("pwsh", {}) == newline.NATIVE_PREAMBLE
    assert len(calls) == 1
    records = [json.loads(line) for line in (tmp_path / "calls.log").read_text("utf-8").splitlines()]
    failed = [r for r in records if r["event"] == "newline_setup_failed"]
    assert len(failed) == 1
    assert "compiler missing" in failed[0]["error"]
    assert failed[0]["fallback"] == "native"


@needs_pwsh
def test_dll_is_built_once_and_reused(monkeypatch):
    monkeypatch.setenv(newline.MODE_ENV, "lf")
    first = newline.ensure_dll("pwsh", dict(os.environ))
    assert first is not None and first.is_file()

    def must_not_build(exe, dll, env):
        raise AssertionError("rebuilt although the DLL was cached")

    monkeypatch.setattr(newline, "_build", must_not_build)
    assert newline.ensure_dll("pwsh", {}) == first
    assert not list(first.parent.glob("*.tmp.dll")) and not list(first.parent.glob("*.cs"))

# --- lf mode: what actually reaches stdin and files ---------------------------

HERE = "@'\na\n" + JP + "\n'@"

LF_STDIN = [
    ("@('x','" + JP + "')", b"x\n" + JP.encode() + b"\n"),
    (HERE, b"a\n" + JP.encode() + b"\n"),
    ('"a`rb"', b"a\rb\n"),  # a lone CR is kept
]


@needs_pwsh
@pytest.mark.parametrize("path", ["normal", "job"])
@pytest.mark.parametrize("expr, expected", LF_STDIN)
def test_lf_stdin_of_native_command(monkeypatch, path, expr, expected):
    monkeypatch.setenv(newline.MODE_ENV, "lf")
    assert stdin_bytes(expr, path) == expected


LF_FILES = [
    ("'a','" + JP + "' | Set-Content {f}", b"a\n" + JP.encode() + b"\n"),
    ("'a','" + JP + "' | Out-File {f}", b"a\n" + JP.encode() + b"\n"),
    ("'a','" + JP + "' > {f}", b"a\n" + JP.encode() + b"\n"),
    ("'a' > {f}; 'b' >> {f}", b"a\nb\n"),
    ("'a' | Set-Content {f}; 'b' | Add-Content {f}", b"a\nb\n"),
    ("'a','b' | Tee-Object -FilePath {f} | Out-Null", b"a\nb\n"),
    ("[pscustomobject]@{x=1;y='" + JP + "'} | Export-Csv {f}",
     b'"x","y"\n"1","' + JP.encode() + b'"\n'),
    # Output of a native command, piped through a cmdlet, is re-encoded.
    ("cmd /c 'echo a& echo b' | Set-Content {f}", b"a\nb\n"),
]


@needs_pwsh
@pytest.mark.parametrize("path", ["normal", "job"])
@pytest.mark.parametrize("template, expected", LF_FILES)
def test_lf_files(tmp_path, monkeypatch, path, template, expected):
    monkeypatch.setenv(newline.MODE_ENV, "lf")
    assert file_bytes(tmp_path, template, path) == expected


@needs_pwsh
def test_lf_explicit_encoding_restores_crlf(tmp_path, monkeypatch):
    monkeypatch.setenv(newline.MODE_ENV, "lf")
    assert file_bytes(tmp_path, "'a','b' | Set-Content {f} -Encoding utf8") == b"a\r\nb\r\n"


@needs_pwsh
def test_lf_command_can_reassign_output_encoding(monkeypatch):
    monkeypatch.setenv(newline.MODE_ENV, "lf")
    expr = "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); 'x'"
    assert stdin_bytes(expr) == b"x\r\n"


ENCODER_SPLIT = (
    "$e = $OutputEncoding.GetEncoder(); $buf = [byte[]]::new(16); "
    "$a = [char[]]\"{a}\"; $b = [char[]]\"{b}\"; "
    "$n = $e.GetBytes($a, 0, $a.Length, $buf, 0, $false); "
    "$n += $e.GetBytes($b, 0, $b.Length, $buf, $n, $true); "
    "[System.BitConverter]::ToString($buf, 0, $n)"
)


@needs_pwsh
@pytest.mark.parametrize("a, b, expected", [
    ("a`r", "`nb", "61-0A-62"),   # CRLF split across two writes
    ("a`r", "b", "61-0D-62"),     # lone CR at a write boundary
    ("a`r", "", "61-0D"),         # CR at the very end is flushed
])
def test_lf_encoder_across_writes(monkeypatch, a, b, expected):
    monkeypatch.setenv(newline.MODE_ENV, "lf")
    out = run(ENCODER_SPLIT.replace("{a}", a).replace("{b}", b))
    assert out.strip() == expected


# --- native mode: Windows line endings, no BOM ---------------------------------

@needs_pwsh
@pytest.mark.parametrize("path", ["normal", "job"])
def test_native_stdin_has_no_bom_but_crlf(monkeypatch, path):
    monkeypatch.setenv(newline.MODE_ENV, "native")
    assert stdin_bytes("@('x','y')", path) == b"x\r\ny\r\n"


@needs_pwsh
def test_native_files_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv(newline.MODE_ENV, "native")
    assert file_bytes(tmp_path, "'a','b' | Set-Content {f}") == b"a\r\nb\r\n"


# --- README claims --------------------------------------------------------------

def test_csharp_source_ships_next_to_the_module():
    cs = Path(newline.__file__).with_name("lf_encoding.cs")
    assert cs.is_file()
    assert newline.CSHARP_SOURCE == cs.read_text(encoding="utf-8")
    assert "class LfUtf8Encoding" in newline.CSHARP_SOURCE


@needs_pwsh
def test_lf_utf8bom_adds_a_bom(tmp_path, monkeypatch):
    monkeypatch.setenv(newline.MODE_ENV, "lf")
    data = file_bytes(tmp_path, "'a','b' | Set-Content {f} -Encoding utf8BOM")
    assert data == b"\xef\xbb\xbfa\r\nb\r\n"


@needs_pwsh
def test_lf_defaults_can_be_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv(newline.MODE_ENV, "lf")
    data = file_bytes(tmp_path, "$PSDefaultParameterValues.Clear(); 'a','b' | Set-Content {f}")
    assert data == b"a\r\nb\r\n"


@needs_pwsh
@pytest.mark.parametrize("mode", ["native", "lf"])
def test_native_output_redirected_keeps_its_own_line_endings(tmp_path, monkeypatch, mode):
    monkeypatch.setenv(newline.MODE_ENV, mode)
    assert file_bytes(tmp_path, "cmd /c 'echo a& echo b' > {f}") == b"a\r\nb\r\n"
