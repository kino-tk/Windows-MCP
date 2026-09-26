"""FileSystem write: line endings are what the caller asked for, not what the OS prefers."""

import asyncio

import pytest

from windows_mcp.filesystem.service import resolve_line_ending, write_file


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("WINDOWS_MCP_WRITE_LINE_ENDING", raising=False)


def test_default_keeps_lf_as_given(tmp_path):
    """The original code wrote CRLF here (Python text-mode translation on Windows)."""
    f = tmp_path / "a.txt"
    write_file(str(f), "line1\nline2\n")
    assert f.read_bytes() == b"line1\nline2\n"


def test_keep_preserves_crlf_that_is_given(tmp_path):
    f = tmp_path / "a.txt"
    write_file(str(f), "a\r\nb\nc", line_ending="keep")
    assert f.read_bytes() == b"a\r\nb\nc"


def test_lf_normalises(tmp_path):
    f = tmp_path / "a.txt"
    write_file(str(f), "a\r\nb\nc\r\n", line_ending="lf")
    assert f.read_bytes() == b"a\nb\nc\n"


def test_crlf_normalises_without_doubling(tmp_path):
    f = tmp_path / "a.bat"
    write_file(str(f), "@echo off\r\necho hi\npause", line_ending="crlf")
    assert f.read_bytes() == b"@echo off\r\necho hi\r\npause"


def test_append_uses_the_same_rule(tmp_path):
    f = tmp_path / "a.txt"
    write_file(str(f), "one\n", line_ending="lf")
    write_file(str(f), "two\r\n", append=True, line_ending="lf")
    assert f.read_bytes() == b"one\ntwo\n"


def test_japanese_utf8_with_lf(tmp_path):
    f = tmp_path / "jp.md"
    write_file(str(f), "見出し\n本文\n", line_ending="lf")
    assert f.read_bytes() == "見出し\n本文\n".encode("utf-8")


def test_environment_sets_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_WRITE_LINE_ENDING", "crlf")
    f = tmp_path / "a.txt"
    write_file(str(f), "a\nb")
    assert f.read_bytes() == b"a\r\nb"
    # An explicit argument still wins over the environment.
    write_file(str(f), "a\nb", line_ending="lf")
    assert f.read_bytes() == b"a\nb"


def test_bad_environment_value_falls_back_to_keep(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDOWS_MCP_WRITE_LINE_ENDING", "unix")
    assert resolve_line_ending() == "keep"
    f = tmp_path / "a.txt"
    write_file(str(f), "a\nb")
    assert f.read_bytes() == b"a\nb"


def test_bad_argument_is_an_error_and_writes_nothing(tmp_path):
    f = tmp_path / "a.txt"
    result = write_file(str(f), "a\nb", line_ending="unix")
    assert result.startswith("Error:") and "keep, lf, crlf" in result
    assert not f.exists()


def test_tool_passes_line_ending_through(tmp_path):
    from fastmcp import FastMCP
    from windows_mcp.tools.filesystem import register

    mcp = FastMCP(name="t")
    register(mcp, get_desktop=lambda: None, get_analytics=lambda: None)
    lf, crlf = tmp_path / "lf.txt", tmp_path / "crlf.txt"

    async def scenario():
        await mcp.call_tool("FileSystem", {"mode": "write", "path": str(lf), "content": "x\ny\n"})
        await mcp.call_tool(
            "FileSystem", {"mode": "write", "path": str(crlf), "content": "x\ny\n", "line_ending": "crlf"}
        )

    asyncio.run(scenario())
    assert lf.read_bytes() == b"x\ny\n"
    assert crlf.read_bytes() == b"x\r\ny\r\n"
