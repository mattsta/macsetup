import stat
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from macsetup.model import CommandResult, CommandSpec, StepStatus
from macsetup.runner import CommandRunner
from macsetup.steps import (
    HomebrewInstallStep,
    HomebrewPhantomJsCleanupStep,
    HomebrewSharePermissionsStep,
    ZshCompletionPermissionsStep,
)


@dataclass
class PrefixContext:
    prefix: Path
    runner: CommandRunner
    home: Path = Path("/tmp")
    repo_root: Path = Path("/tmp/macsetup")
    backup_root: Path = Path("/tmp/.macsetup/backups")
    dry_run: bool = False
    allow_bootstrap: bool = False
    allow_privileged: bool = False
    enable_dns_blocklist: bool = True

    def command_exists(self, name: str) -> bool:
        return name == "brew"

    def brew_prefix(self) -> Path:
        return self.prefix


@dataclass
class RecordingRunner:
    runs: list[tuple[CommandSpec, bool, bool]] = field(default_factory=list)

    def which(self, name: str) -> str | None:
        del name
        return None

    def add_path_dir(self, directory: Path) -> None:
        del directory

    def state(self, state: str, detail: str) -> None:
        del state, detail

    def run(
        self,
        command: CommandSpec,
        *,
        check: bool = False,
        capture: bool = True,
        dry_run: bool | None = None,
        summarize_output: bool = True,
        timeout_seconds: float | None = None,
        heartbeat: bool = True,
    ) -> CommandResult:
        del check, dry_run, timeout_seconds, heartbeat
        self.runs.append((command, capture, summarize_output))
        return CommandResult(
            command=command.shell or " ".join(command.argv), returncode=0
        )

    def sudo_validate(self, *, timeout_seconds: float = 120.0) -> CommandResult:
        del timeout_seconds
        return CommandResult(command="sudo -v", returncode=0)


@dataclass
class BootstrapContext:
    runner: RecordingRunner
    home: Path = Path("/tmp")
    repo_root: Path = Path("/tmp/macsetup")
    backup_root: Path = Path("/tmp/.macsetup/backups")
    dry_run: bool = False
    allow_bootstrap: bool = True
    allow_privileged: bool = False
    enable_dns_blocklist: bool = True

    def command_exists(self, name: str) -> bool:
        del name
        return False

    def brew_prefix(self) -> None:
        return None


class HomebrewHygieneTests(unittest.TestCase):
    def test_homebrew_installer_keeps_terminal_and_streams_output(self) -> None:
        runner = RecordingRunner()
        context = BootstrapContext(runner=runner)

        result = HomebrewInstallStep().apply(context)

        installer, capture, summarize_output = runner.runs[0]
        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertTrue(installer.needs_tty)
        self.assertFalse(capture)
        self.assertFalse(summarize_output)

    def test_share_permissions_step_chmods_share_to_755(self) -> None:
        with TemporaryDirectory() as directory:
            prefix = Path(directory)
            share = prefix / "share"
            share.mkdir()
            share.chmod(0o775)
            context = PrefixContext(prefix=prefix, runner=CommandRunner())

            result = HomebrewSharePermissionsStep().apply(context)

            self.assertEqual(result.status, StepStatus.APPLIED)
            self.assertEqual(stat.S_IMODE(share.stat().st_mode), 0o755)

    def test_phantomjs_cleanup_removes_legacy_homebrew_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            prefix = Path(directory)
            phantomjs = prefix / "Caskroom" / "phantomjs"
            phantomjs.mkdir(parents=True)
            (phantomjs / ".metadata").mkdir()
            context = PrefixContext(prefix=prefix, runner=CommandRunner())

            result = HomebrewPhantomJsCleanupStep().apply(context)

            self.assertEqual(result.status, StepStatus.APPLIED)
            self.assertFalse(phantomjs.exists())

    def test_zsh_completion_permissions_cover_prefix_parents_and_completion_trees(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            prefix = Path(directory)
            share = prefix / "share"
            site_functions = share / "zsh" / "site-functions"
            zsh_completions = share / "zsh-completions"
            site_functions.mkdir(parents=True)
            zsh_completions.mkdir()
            completion_file = site_functions / "_demo"
            completion_file.write_text("#compdef demo\n", encoding="utf-8")
            prefix.chmod(0o775)
            share.chmod(0o775)
            site_functions.chmod(0o775)
            zsh_completions.chmod(0o775)
            completion_file.chmod(0o664)
            context = PrefixContext(prefix=prefix, runner=CommandRunner())
            step = ZshCompletionPermissionsStep()

            check = step.check(context)
            result = step.apply(context)

            self.assertEqual(check.status, StepStatus.NEEDS_CHANGE)
            self.assertIn(str(prefix), check.detail)
            self.assertIn(str(share), check.detail)
            self.assertIn(str(completion_file), check.detail)
            self.assertEqual(result.status, StepStatus.APPLIED)
            self.assertEqual(stat.S_IMODE(prefix.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(share.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(site_functions.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(zsh_completions.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(completion_file.stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
