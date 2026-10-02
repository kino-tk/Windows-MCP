"""Line endings and BOM of text that PowerShell writes for a command.

pwsh picks the line ending itself in two places: the lines of pipeline input
it feeds to a native command's stdin, and the files written by Out-File (and
so by ``>`` and ``>>``), Set-Content, Add-Content, Tee-Object and Export-Csv.
On Windows that ending is CRLF. The UTF-8 encoding this server used to put in
``$OutputEncoding`` also carries a BOM, which pwsh writes at the start of
every stdin it feeds to a native command. Text sent through ``ssh`` to a
POSIX shell, or written for a tool that expects LF, then arrives with a stray
BOM and CRs.

``WINDOWS_MCP_POWERSHELL_NEWLINE`` selects the behaviour:

``native``
    Keep the Windows line ending. Only the BOM on native stdin is dropped.
``lf`` (default in this fork)
    Write LF instead of CRLF, without a BOM, on all of the paths above. This
    uses a small .NET encoding (``lf_encoding.cs``) that drops a CR only
    when an LF follows it, so a lone CR, such as a progress line redrawn in
    place, is kept. It is compiled once with ``Add-Type`` into
    ``~/.windows-mcp/cache`` and loaded on each call (about 30 ms). If it
    cannot be built, the call falls back to ``native`` and the failure is
    recorded once in the call log.

Bytes a native command writes itself (``native > file``, ``native | native``)
do not pass through PowerShell's text encoding in pwsh 7.4+, so they keep the
line endings that program chose. Piping them through a cmdlet
(``native | Set-Content file``) re-encodes them.

A command opts out for one cmdlet with ``-Encoding utf8`` (UTF-8 without a
BOM, CRLF), or for the whole call by assigning ``$OutputEncoding`` or
``$PSDefaultParameterValues`` itself. Windows PowerShell 5.1 cannot pass an
encoding object to these cmdlets, so there only the BOM is dropped.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import subprocess
import threading
from importlib import resources
from pathlib import Path

from windows_mcp.infrastructure import calllog

logger = logging.getLogger(__name__)

MODE_ENV = "WINDOWS_MCP_POWERSHELL_NEWLINE"
MODES = ("native", "lf")
DEFAULT_MODE = "lf"

CLASS_NAME = "WindowsMcp.Text.LfUtf8Encoding"

# The C# source lives next to this module so it is versioned and reviewed as C#.
CSHARP_SOURCE = (
    resources.files(__package__).joinpath("lf_encoding.cs").read_text(encoding="utf-8")
)
_SOURCE_HASH = hashlib.sha256(CSHARP_SOURCE.encode("utf-8")).hexdigest()

# What every call starts with in ``native`` mode, and the fallback for ``lf``.
NATIVE_PREAMBLE = "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "

_FILE_CMDLETS = ("Out-File", "Set-Content", "Add-Content", "Tee-Object", "Export-Csv")

_lock = threading.Lock()
# Cache keys whose build failed in this process; not retried until restart.
_failed: set[str] = set()


def mode() -> str:
    """Return the configured mode; unknown values mean the default."""
    value = os.environ.get(MODE_ENV, "").strip().lower()
    return value if value in MODES else DEFAULT_MODE


def cache_dir() -> Path:
    return Path("~/.windows-mcp").expanduser() / "cache"


def _quote(text: str) -> str:
    """Single-quoted PowerShell string literal."""
    return "'" + text.replace("'", "''") + "'"


def lf_preamble(dll: Path) -> str:
    """Statements that make the call write LF without a BOM."""
    defaults = "".join(
        f"$PSDefaultParameterValues['{name}:Encoding'] = $OutputEncoding; "
        for name in _FILE_CMDLETS
    )
    return (
        f"try {{ [void][System.Reflection.Assembly]::LoadFrom({_quote(str(dll))}); "
        f"$OutputEncoding = [{CLASS_NAME}]::new(); {defaults}}} "
        "catch { $OutputEncoding = [System.Text.UTF8Encoding]::new($false) }; "
    )


def preamble(shell: str, env: dict[str, str]) -> str:
    """Return the statements to run before the user's command."""
    if mode() != "lf":
        return NATIVE_PREAMBLE
    if os.path.basename(shell).lower().removesuffix(".exe") != "pwsh":
        return NATIVE_PREAMBLE
    dll = ensure_dll(shell, env)
    return lf_preamble(dll) if dll is not None else NATIVE_PREAMBLE


def _cache_key(exe: str) -> str:
    # A pwsh upgrade may move to a newer .NET; rebuild against it.
    try:
        st = os.stat(exe)
        stamp = f"{exe}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        stamp = exe
    return hashlib.sha256(f"{_SOURCE_HASH}|{stamp}".encode("utf-8")).hexdigest()[:16]


def ensure_dll(shell: str, env: dict[str, str]) -> Path | None:
    """Return the compiled encoding, building it on first use; None on failure."""
    exe = shutil.which(shell) or shell
    key = _cache_key(exe)
    dll = cache_dir() / f"lf-encoding-{key}.dll"
    if dll.is_file():
        return dll
    with _lock:
        if dll.is_file():
            return dll
        if key in _failed:
            return None
        try:
            _build(exe, dll, env)
        except Exception as e:
            _failed.add(key)
            error = f"{type(e).__name__}: {e}"
            logger.warning("LF encoding unavailable, using native line endings: %s", error)
            calllog.record(
                "newline_setup_failed", calllog.new_call_id(), "PowerShell",
                error=error[:500], fallback="native",
            )
            return None
    return dll


def _run(exe: str, script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _tail(result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or result.stdout or b"").decode("utf-8", "replace")
    return f"rc={result.returncode} {text.strip()[-300:]}"


def _build(exe: str, dll: Path, env: dict[str, str]) -> None:
    """Compile the encoding, check that it behaves, then move it into place."""
    dll.parent.mkdir(parents=True, exist_ok=True)
    tag = f"{os.getpid()}-{threading.get_ident()}"
    src = dll.parent / f"lf-encoding-{tag}.cs"
    tmp = dll.parent / f"lf-encoding-{tag}.tmp.dll"
    try:
        src.write_text(CSHARP_SOURCE, encoding="utf-8")
        result = _run(exe, (
            f"Add-Type -TypeDefinition ([System.IO.File]::ReadAllText({_quote(str(src))})) "
            f"-OutputAssembly {_quote(str(tmp))} -OutputType Library"
        ), env)
        if result.returncode != 0 or not tmp.is_file():
            raise RuntimeError(f"Add-Type failed: {_tail(result)}")
        # CRLF -> LF, lone CR kept, no BOM.
        result = _run(exe, (
            f"[void][System.Reflection.Assembly]::LoadFrom({_quote(str(tmp))}); "
            f"$e = [{CLASS_NAME}]::new(); "
            "[Console]::Out.Write([System.BitConverter]::ToString($e.GetBytes(\"a`r`nb`rc\"))"
            " + '|' + $e.GetPreamble().Length)"
        ), env)
        got = result.stdout.decode("ascii", "replace").strip()
        if got != "61-0A-62-0D-63|0":
            raise RuntimeError(f"self-check failed: got {got!r}, {_tail(result)}")
        try:
            os.replace(tmp, dll)
        except OSError:
            # Another server process put it in place first and has it loaded.
            if not dll.is_file():
                raise
    finally:
        for leftover in (src, tmp):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
