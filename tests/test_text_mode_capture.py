"""run_with_graceful_timeout captures into files, not pipes. The result must
still match ``subprocess.run`` byte for byte and character for character,
including text mode (decoding, errors, newline translation)."""

import subprocess
import sys

import pytest

from windows_mcp.powershell.utils import run_with_graceful_timeout

# The child writes raw bytes so the expected decoding does not depend on the
# console code page of the machine running the tests.
_WRITE = (
    "import sys; sys.stdout.buffer.write({out!r}); sys.stdout.flush(); "
    "sys.stderr.buffer.write({err!r}); sys.stderr.flush()"
)


def _child(out: bytes, err: bytes = b"") -> list[str]:
    return [sys.executable, "-c", _WRITE.format(out=out, err=err)]


CASES = [
    pytest.param(b"a\r\nb\rc\n", b"e\r\n", {}, id="bytes-untouched"),
    pytest.param(b"a\r\nb\rc\n", b"e\r\n", {"text": True}, id="text-newlines"),
    pytest.param(b"a\r\nb\n", b"", {"universal_newlines": True}, id="universal-newlines"),
    pytest.param("日本\r\n".encode("utf-8"), b"", {"encoding": "utf-8"}, id="encoding"),
    pytest.param(b"ok\xff\r\n", b"\xfe", {"encoding": "utf-8", "errors": "replace"}, id="errors"),
    pytest.param(b"plain ascii\r\n", b"", {"text": True, "errors": "strict"}, id="text-errors"),
]


@pytest.mark.parametrize("out,err,kw", CASES)
def test_matches_subprocess_run(out, err, kw):
    want = subprocess.run(_child(out, err), capture_output=True, **kw)
    got = run_with_graceful_timeout(_child(out, err), capture_output=True, **kw)
    assert type(got.stdout) is type(want.stdout)
    assert (got.stdout, got.stderr, got.returncode) == (want.stdout, want.stderr, want.returncode)


def test_text_input_round_trip():
    echo = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"]
    want = subprocess.run(echo, input="abc\ndef\n", capture_output=True, text=True)
    got = run_with_graceful_timeout(echo, input="abc\ndef\n", capture_output=True, text=True)
    assert got.stdout == want.stdout == "ABC\nDEF\n"


def test_strict_decoding_error_is_raised():
    # As subprocess does on POSIX. (On Windows, subprocess.run decodes in a
    # reader thread and the error is lost with the output.)
    with pytest.raises(UnicodeDecodeError):
        run_with_graceful_timeout(_child(b"\xff"), capture_output=True, encoding="utf-8")


def test_check_error_carries_text():
    fail = [sys.executable, "-c", "import sys; sys.stderr.write('bad\\n'); sys.exit(3)"]
    with pytest.raises(subprocess.CalledProcessError) as info:
        run_with_graceful_timeout(fail, capture_output=True, text=True, check=True)
    assert info.value.returncode == 3 and info.value.stderr == "bad\n"
