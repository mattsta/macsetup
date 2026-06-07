import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from macsetup.model import CommandResult, CommandSpec
from macsetup.runner import (
    CommandRunner,
    _command_needs_controlling_tty,
    _path_with_homebrew_bins,
)


class CommandRunnerProgressTests(unittest.TestCase):
    def test_homebrew_bin_dirs_are_added_to_command_path(self) -> None:
        with TemporaryDirectory() as directory:
            first = Path(directory) / "opt-homebrew" / "bin"
            second = Path(directory) / "usr-local" / "bin"
            first.mkdir(parents=True)
            second.mkdir(parents=True)

            with patch("macsetup.runner.HOMEBREW_TOOL_DIRS", (first, second)):
                path = _path_with_homebrew_bins("/usr/bin")

        self.assertEqual(path.split(os.pathsep), [str(first), str(second), "/usr/bin"])

    def test_which_falls_back_to_homebrew_bin_dirs(self) -> None:
        with TemporaryDirectory() as directory:
            homebrew_bin = Path(directory) / "opt-homebrew" / "bin"
            homebrew_bin.mkdir(parents=True)
            tool = homebrew_bin / "brew"
            tool.write_text("#!/bin/sh\n", encoding="utf-8")
            tool.chmod(0o755)

            with (
                patch("macsetup.runner.HOMEBREW_TOOL_DIRS", (homebrew_bin,)),
                patch.dict(os.environ, {"PATH": "/usr/bin"}),
            ):
                found = CommandRunner().which("brew")

        self.assertEqual(found, str(tool))

    def test_registered_path_dirs_are_used_for_which_and_command_env(self) -> None:
        with TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            tool = bin_dir / "demo-tool"
            tool.write_text("#!/bin/sh\n", encoding="utf-8")
            tool.chmod(0o755)
            runner = CommandRunner()
            runner.add_path_dir(bin_dir)

            with (
                patch("macsetup.runner.HOMEBREW_TOOL_DIRS", ()),
                patch.dict(os.environ, {"PATH": "/usr/bin"}),
            ):
                found = runner.which("demo-tool")
                env = runner._env({})

        self.assertEqual(found, str(tool))
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(bin_dir))

    def test_running_commands_emit_heartbeat_state(self) -> None:
        runner = CommandRunner(progress=True, heartbeat_seconds=0.01)
        output = io.StringIO()

        with redirect_stdout(output):
            result = runner.run(
                CommandSpec(
                    argv=(sys.executable, "-c", "import time; time.sleep(0.05)")
                )
            )

        rendered = output.getvalue()
        self.assertTrue(result.ok)
        self.assertIn("STATE    command.start", rendered)
        self.assertIn("STATE    command.running", rendered)
        self.assertIn("STATE    command.done", rendered)

    def test_progress_output_is_summarized_but_still_captured(self) -> None:
        runner = CommandRunner(progress=True)
        output = io.StringIO()

        with redirect_stdout(output):
            result = runner.run(
                CommandSpec(
                    argv=(
                        sys.executable,
                        "-c",
                        "print('==> Installing demo', flush=True); print('ordinary detail', flush=True)",
                    )
                )
            )

        rendered = output.getvalue()
        self.assertTrue(result.ok)
        self.assertIn("OUTPUT   stdout ==> Installing demo", rendered)
        self.assertNotIn("OUTPUT   stdout ordinary detail", rendered)
        self.assertIn("ordinary detail", result.stdout)

    def test_prompt_fragments_are_shown_and_pause_heartbeats(self) -> None:
        runner = CommandRunner(progress=True, heartbeat_seconds=0.01)
        output = io.StringIO()

        with redirect_stdout(output):
            result = runner.run(
                CommandSpec(
                    argv=(
                        sys.executable,
                        "-c",
                        "import sys, time; sys.stderr.write('[sudo] password for user: '); sys.stderr.flush(); time.sleep(0.05)",
                    )
                )
            )

        rendered = output.getvalue()
        self.assertTrue(result.ok)
        self.assertIn("PROMPT   stderr [sudo] password for user:", rendered)
        self.assertNotIn("STATE    command.running", rendered)
        self.assertIn("[sudo] password for user:", result.stderr)

    def test_command_timeout_stops_process(self) -> None:
        runner = CommandRunner(progress=True, heartbeat_seconds=0.01)
        output = io.StringIO()

        with redirect_stdout(output):
            result = runner.run(
                CommandSpec(argv=(sys.executable, "-c", "import time; time.sleep(1)")),
                timeout_seconds=0.03,
            )

        rendered = output.getvalue()
        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 124)
        self.assertIn("STATE    command.timeout", rendered)
        self.assertIn("command timed out after", result.stderr)

    def test_sudo_commands_keep_controlling_terminal(self) -> None:
        self.assertTrue(
            _command_needs_controlling_tty(CommandSpec(argv=("sudo", "-v")))
        )
        self.assertTrue(
            _command_needs_controlling_tty(
                CommandSpec(
                    shell="sudo killall -HUP mDNSResponder && sudo dscacheutil -flushcache"
                )
            )
        )
        self.assertTrue(
            _command_needs_controlling_tty(
                CommandSpec(argv=(sys.executable, "-c", "pass"), needs_tty=True)
            )
        )
        self.assertFalse(
            _command_needs_controlling_tty(CommandSpec(argv=("brew", "install", "git")))
        )

    def test_sudo_validate_uses_interactive_sudo_command(self) -> None:
        runner = CommandRunner()

        with patch.object(
            runner,
            "run",
            return_value=CommandResult(command="sudo -v", returncode=0),
        ) as run:
            result = runner.sudo_validate(timeout_seconds=9)

        self.assertTrue(result.ok)
        command = run.call_args.args[0]
        self.assertEqual(command.argv, ("sudo", "-v"))
        self.assertTrue(command.needs_tty)
        self.assertFalse(run.call_args.kwargs["capture"])
        self.assertFalse(run.call_args.kwargs["heartbeat"])
        self.assertEqual(run.call_args.kwargs["timeout_seconds"], 9)


if __name__ == "__main__":
    unittest.main()
