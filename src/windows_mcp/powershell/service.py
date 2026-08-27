""" PowerShell command executor service """

import base64
import ctypes
import ctypes.wintypes
import logging
import os
import winreg
import shutil
import subprocess

from windows_mcp.desktop.utils import is_elevated
from windows_mcp.powershell.utils import run_with_graceful_timeout

__all__ = ["PowerShellExecutor"]

logger = logging.getLogger(__name__)


def _read_reg_env(hkey: int, subkey: str) -> tuple[dict[str, str], str, str]:
    """Read all environment variables from a registry key.

    Returns (vars, path, pathext) where *vars* is a dict of name->value
    (excluding PATH/PATHEXT), and *path*/*pathext* are the raw expanded
    values for those two special keys (empty string if absent).
    """
    variables: dict[str, str] = {}
    path = ""
    pathext = ""

    try:
        with winreg.OpenKey(hkey, subkey) as key:
            i = 0
            while True:
                try:
                    name, value, reg_type = winreg.EnumValue(key, i)
                    if reg_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                        i += 1
                        continue
                    if reg_type == winreg.REG_EXPAND_SZ:
                        # Note: ExpandEnvironmentStrings uses the current process
                        # environment, which may be stripped by the MCP host. References
                        # to missing vars (e.g. %COMPUTERNAME%) won't expand correctly.
                        # In practice this is acceptable: most REG_EXPAND_SZ values under
                        # HKLM/HKCU\Environment reference %SystemRoot% or %USERPROFILE%
                        # which are almost always present.
                        value = winreg.ExpandEnvironmentStrings(value)
                    upper = name.upper()
                    if upper == "PATH":
                        path = value
                    elif upper == "PATHEXT":
                        pathext = value
                    else:
                        variables[name] = value
                    i += 1
                except OSError:
                    break
    except OSError:
        pass

    return variables, path, pathext


def _dedup_path(*segments: str) -> str:
    """Join PATH segments and deduplicate entries (case-insensitive)."""
    seen = set()
    deduped = []
    for p in ";".join(filter(None, segments)).split(";"):
        norm = p.lower().rstrip("\\")
        if norm and norm not in seen:
            seen.add(norm)
            deduped.append(p)
    return ";".join(deduped)


def _win32_name(dll: str, func: str) -> str:
    """Call a Win32 GetXxxNameW(LPWSTR, LPDWORD) function.

    Only suitable for APIs with the (buffer, &size) calling convention,
    e.g. kernel32.GetComputerNameW and advapi32.GetUserNameW.
    """
    buf = ctypes.create_unicode_buffer(257)
    size = ctypes.wintypes.DWORD(257)
    fn = getattr(ctypes.windll, dll)
    if getattr(fn, func)(buf, ctypes.byref(size)):
        return buf.value
    return ""


_FALLBACK_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.CPL;.PY;.PYW"


# KNOWNFOLDERID values from KnownFolders.h. Several environment variables map to
# the same folder: ALLUSERSPROFILE mirrors ProgramData, and ProgramW6432 mirrors
# ProgramFiles on a 64-bit process.
_KNOWN_FOLDER_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("ProgramData", "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}"),
    ("ALLUSERSPROFILE", "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}"),
    ("PUBLIC", "{DFDF76A2-C82A-4D63-906A-5644AC457385}"),
    ("ProgramFiles", "{6D809377-6AF0-444B-8957-A3773F02200E}"),
    ("ProgramW6432", "{6D809377-6AF0-444B-8957-A3773F02200E}"),
    ("ProgramFiles(x86)", "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}"),
    ("CommonProgramFiles", "{6365D5A7-0F0D-45E5-87F6-0DA56B6A4F7D}"),
    ("CommonProgramW6432", "{6365D5A7-0F0D-45E5-87F6-0DA56B6A4F7D}"),
    ("CommonProgramFiles(x86)", "{DE974D24-D9C6-4D3E-BF91-F4455120B917}"),
)


def _known_folder_path(folder_id: str) -> str | None:
    """Resolve a KNOWNFOLDERID string to its filesystem path."""
    ptr = ctypes.c_wchar_p()
    try:
        ole32 = ctypes.windll.ole32
        guid = ctypes.create_string_buffer(16)
        if ole32.CLSIDFromString(ctypes.c_wchar_p(folder_id), ctypes.byref(guid)) != 0:
            return None
        if ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(ptr)
        ) != 0:
            return None
        return ptr.value or None
    except Exception:
        logger.debug("Failed to resolve known folder %s", folder_id, exc_info=True)
        return None
    finally:
        if ptr:
            try:
                ctypes.windll.ole32.CoTaskMemFree(ptr)
            except Exception:
                logger.debug("Failed to free known folder path buffer", exc_info=True)

def _prepare_env() -> dict[str, str]:
    """Prepare a complete environment block for the PowerShell subprocess.

    MCP hosts (e.g. Claude Desktop) may launch this server with a stripped
    environment block missing session-level variables. This function starts
    from os.environ and fills in missing variables from:
      1. System-level env vars from the registry (HKLM)
      2. User-level env vars from the registry (HKCU)
      3. Dynamic vars (COMPUTERNAME, USERNAME, etc.) via Win32 API
    Existing values in os.environ are never overwritten, only missing ones
    are supplemented. PATH is special-cased: inherited entries keep their
    priority and registry entries are appended to fill in missing paths.
    """
    env = os.environ.copy()

    # PYTHONHOME is set by uv to pin its own managed interpreter for this
    # server process. Leaking it into a spawned shell makes any Python found
    # on PATH there resolve its standard library against the server's
    # interpreter instead of its own, breaking unrelated installs (#350).
    env.pop("PYTHONHOME", None)

    try:
        machine_vars, machine_path, machine_pathext = _read_reg_env(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        )
        user_vars, user_path, user_pathext = _read_reg_env(winreg.HKEY_CURRENT_USER, r"Environment")

        # Supplement missing vars. User-level (HKCU) takes precedence over
        # system-level (HKLM) for same-named keys, matching Windows' resolution order.
        for name, value in {**machine_vars, **user_vars}.items():
            if not env.get(name):
                env[name] = value

        # PATH: inherited entries keep their priority; registry entries are
        # appended to fill in anything missing (e.g. stripped MCP host env).
        if machine_path or user_path:
            env["PATH"] = _dedup_path(env.get("PATH", ""), machine_path, user_path)

        # PATHEXT: use registry value if the inherited one looks incomplete
        effective_pathext = user_pathext or machine_pathext
        if effective_pathext and ".EXE" not in env.get("PATHEXT", "").upper():
            env["PATHEXT"] = effective_pathext

    except Exception:
        logger.debug("Failed to read environment from registry", exc_info=True)
        if ".EXE" not in env.get("PATHEXT", "").upper():
            env["PATHEXT"] = _FALLBACK_PATHEXT

    # Dynamic variables not stored in registry — only fill if missing
    if not env.get("COMPUTERNAME"):
        try:
            computer_name = _win32_name("kernel32", "GetComputerNameW")
            if computer_name:
                env["COMPUTERNAME"] = computer_name
        except Exception as e:
            logger.debug("Failed to get COMPUTERNAME via Win32 API: %s", e)

    if not env.get("USERNAME"):
        try:
            user_name = _win32_name("advapi32", "GetUserNameW")
            if user_name:
                env["USERNAME"] = user_name
        except Exception as e:
            logger.debug("Failed to get USERNAME via Win32 API: %s", e)

    user_profile = os.path.expanduser("~")
    env.setdefault("USERPROFILE", user_profile)
    drive, tail = os.path.splitdrive(user_profile)
    env.setdefault("HOMEDRIVE", drive)
    env.setdefault("HOMEPATH", tail)
    # On domain-joined machines USERDOMAIN is the NetBIOS domain name (e.g.
    # "CORP"), not the computer name. This fallback is only correct for workgroup
    # machines. USERDOMAIN is a session variable set at logon and is unlikely to
    # be missing on domain-joined hosts, so this is an acceptable last resort.
    env.setdefault("USERDOMAIN", env.get("COMPUTERNAME", ""))

    # Known folders are synthesized by Windows when it builds a process
    # environment block; they are not stored in the registry, so the reads above
    # cannot recover them. Packaged (MSIX) MCP hosts hand down a block without
    # them. Any tool that resolves its own configuration through these variables
    # then fails: Win32 OpenSSH, for example, aborts during startup with exit
    # code 255 and no diagnostics at all when ProgramData is absent, which makes
    # every ssh/scp/sftp/ssh-keygen call look like an unexplained failure.
    for _name, _folder_id in _KNOWN_FOLDER_ENV_VARS:
        if not env.get(_name):
            _path = _known_folder_path(_folder_id)
            if _path:
                env[_name] = _path
    return env


class PowerShellExecutor:
    """Static utility class for executing PowerShell commands."""

    @staticmethod
    def execute_command(
            command: str, timeout: int = 10, shell: str | None = None
    ) -> tuple[str, int]:
        try:
            # $OutputEncoding: controls how PS5.1 encodes output written to its stdout pipe.
            # Without this set to UTF-8, PS5.1 uses the system codepage and native process
            # stdout is silently lost when Python reads the pipe.
            # [Console]::OutputEncoding: controls how PS decodes bytes from native exe stdout.
            utf8_command = (
                "$OutputEncoding = [System.Text.Encoding]::UTF8; "
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                f"{command}"
            )
            encoded = base64.b64encode(utf8_command.encode("utf-16le")).decode("ascii")
            env = _prepare_env()
            # NO_COLOR suppresses ANSI escape sequences in pwsh 7.2+ (and many other CLI tools).
            # PS5.1 has no ANSI output, so this is harmlessly ignored there.
            # https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_ansi_terminals#disabling-ansi-output
            env["NO_COLOR"] = "1"

            shell = shell or ("pwsh" if shutil.which("pwsh") else "powershell")

            args = [shell, "-NoProfile"]
            # Only older Windows PowerShell (5.1) uses -OutputFormat Text successfully here
            shell_name = os.path.basename(shell).lower().replace(".exe", "")
            if shell_name == "powershell":
                args.extend(["-OutputFormat", "Text"])
            args.extend(["-EncodedCommand", encoded])

            result = run_with_graceful_timeout(
                args,
                stdin=subprocess.DEVNULL,  # Prevent child processes from inheriting the MCP pipe stdin
                capture_output=True,  # No errors='ignore' - let subprocess return bytes
                timeout=timeout,
                cwd=os.path.expanduser(path="~"),
                env=env,
            )
            # Handle both bytes and str output (subprocess behavior varies by environment)
            stdout = result.stdout
            stderr = result.stderr
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            output = stdout or stderr
            # If the command failed with "Access is denied" and we aren't elevated, add a helpful hint
            if result.returncode != 0 and "Access is denied" in output and not is_elevated():
                output += (
                    "\n\nHINT: This command may require an elevated (Administrator) terminal. "
                    "The Windows-MCP server is currently running at a lower integrity level."
                )
            return output, result.returncode
        except subprocess.TimeoutExpired:
            return "Command execution timed out", 1
        except Exception as e:
            return f"Command execution failed: {type(e).__name__}: {e}", 1
