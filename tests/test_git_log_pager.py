"""Behavioral tests for the git-log-pager C dispatcher.

These compile the actual shipped C source and run the resulting binary with
stub `delta`/`bat` programs on PATH, asserting that:
  - input containing a `diff --git` line is routed to delta,
  - input without one (a plain `git log`) is routed to bat,
  - the binary streams its entire stdin through to the chosen pager verbatim.

The compile/dispatch tests are skipped automatically if no C compiler is
available, so the suite still passes in toolchain-less environments.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from macsetup.content import GIT_LOG_PAGER_C


def _compiler() -> str | None:
    for candidate in ("cc", "clang", "gcc"):
        if shutil.which(candidate):
            return candidate
    return None


# A stub pager that prints a tag identifying itself, then echoes stdin. Lets us
# observe both which pager was chosen and that the full stream arrived.
_STUB = """#!/bin/sh
printf '%s\\n' "PAGER={tag}"
cat
"""


class GitLogPagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compiler = _compiler()
        if self.compiler is None:
            self.skipTest("no C compiler available")
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        src = self.root / "git-log-pager.c"
        src.write_text(GIT_LOG_PAGER_C, encoding="utf-8")
        self.binary = self.root / "git-log-pager"
        result = subprocess.run(
            [self.compiler, "-O2", "-o", str(self.binary), str(src)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"compile failed: {result.stderr}")
        # Stub delta/bat on a PATH dir the binary will find via execvp.
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        for name, tag in (("delta", "delta"), ("bat", "bat")):
            stub = self.bindir / name
            stub.write_text(_STUB.format(tag=tag), encoding="utf-8")
            os.chmod(stub, 0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = f"{self.bindir}:{self.env.get('PATH', '')}"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, stdin: str) -> str:
        result = subprocess.run(
            [str(self.binary)],
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_patch_output_routes_to_delta(self) -> None:
        log_p = (
            "commit abc123\n"
            "Author: Someone <a@b.c>\n\n"
            "    a commit message\n\n"
            "diff --git a/file.txt b/file.txt\n"
            "index 111..222 100644\n"
            "--- a/file.txt\n"
            "+++ b/file.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        out = self._run(log_p)
        self.assertTrue(out.startswith("PAGER=delta\n"), out[:40])
        # Full stream passed through.
        self.assertIn("diff --git a/file.txt b/file.txt", out)
        self.assertIn("+new", out)

    def test_plain_log_routes_to_bat(self) -> None:
        log_plain = (
            "commit abc123\n"
            "Author: Someone <a@b.c>\n\n"
            "    just a log message, no diff here\n\n"
            "commit def456\n"
            "Author: Someone <a@b.c>\n\n"
            "    another message\n"
        )
        out = self._run(log_plain)
        self.assertTrue(out.startswith("PAGER=bat\n"), out[:40])
        self.assertIn("another message", out)

    def test_color_escaped_patch_routes_to_delta(self) -> None:
        # git colorizes its output to the pager by default. The "diff --git"
        # header arrives prefixed with one or more ANSI CSI escapes (real git
        # stacks several), e.g. "\x1b[33m\x1b[38;2;..m\x1b[33mdiff --git ...".
        # The dispatcher must still detect it and route to delta.
        esc = "\x1b"
        colored = (
            f"{esc}[33mcommit abc123{esc}[m\n"
            "Author: Someone <a@b.c>\n\n"
            "    a message mentioning the word diff in prose\n\n"
            f"{esc}[33m{esc}[38;2;248;248;242m{esc}[33mdiff --git a/f b/f{esc}[m\n"
            f"{esc}[33mindex 1..2 100644{esc}[m\n"
            "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n"
        )
        out = self._run(colored)
        self.assertTrue(out.startswith("PAGER=delta\n"), out[:60])

    def test_diff_in_commit_message_body_does_not_route_to_delta(self) -> None:
        # The literal text appearing mid-line inside a commit message must NOT
        # be mistaken for a diff header (anchored to line start).
        body = (
            "commit abc123\n"
            "Author: Someone <a@b.c>\n\n"
            "    I ran diff --git earlier and it printed nothing\n\n"
            "commit def456\n"
            "    another message\n"
        )
        out = self._run(body)
        self.assertTrue(out.startswith("PAGER=bat\n"), out[:60])

    def test_large_stream_passes_through_completely(self) -> None:
        # A diff with many lines after the marker must arrive intact (streaming,
        # not truncated at the peek boundary).
        lines = ["diff --git a/x b/x", "index 1..2 100644", "--- a/x", "+++ b/x"]
        lines += [f"+line {i}" for i in range(20000)]
        payload = "\n".join(lines) + "\n"
        out = self._run(payload)
        self.assertTrue(out.startswith("PAGER=delta\n"))
        self.assertIn("+line 0\n", out)
        self.assertIn("+line 19999\n", out)


if __name__ == "__main__":
    unittest.main()
