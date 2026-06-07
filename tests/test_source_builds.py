import stat
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from macsetup.model import CommandResult, CommandSpec, StepStatus
from macsetup.recipes import SourceBuildPackage
from macsetup.steps import SourceBuildStep, build_steps


@dataclass
class FakeRunner:
    commands: list[CommandSpec] = field(default_factory=list)
    states: list[tuple[str, str]] = field(default_factory=list)
    path_dirs: list[Path] = field(default_factory=list)

    def which(self, name: str) -> str | None:
        return f"/usr/local/bin/{name}" if name in {"git", "cargo", "zig"} else None

    def add_path_dir(self, directory: Path) -> None:
        self.path_dirs.insert(0, directory)

    def state(self, state: str, detail: str) -> None:
        self.states.append((state, detail))

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
        del check, capture, dry_run, summarize_output, timeout_seconds, heartbeat
        self.commands.append(command)
        if command.argv[:2] == ("git", "clone"):
            checkout = Path(command.argv[-1])
            (checkout / ".git").mkdir(parents=True)
            return CommandResult(command="git clone", returncode=0)
        if len(command.argv) >= 5 and command.argv[:4] == (
            "git",
            "-C",
            command.argv[2],
            "remote",
        ):
            return CommandResult(
                command="git remote get-url",
                returncode=0,
                stdout="git@github.com:example/hiproc.git\n",
            )
        if command.shell == "cargo build --release" and command.cwd is not None:
            binary = command.cwd / "target" / "release" / "hiproc"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            return CommandResult(command=command.shell, returncode=0)
        rendered = command.shell or " ".join(command.argv)
        return CommandResult(command=rendered, returncode=0)

    def sudo_validate(self, *, timeout_seconds: float = 120.0) -> CommandResult:
        del timeout_seconds
        return CommandResult(command="sudo -v", returncode=0)


@dataclass
class FakeContext:
    home: Path
    runner: FakeRunner
    repo_root: Path = Path("/tmp/macsetup")
    backup_root: Path = Path("/tmp/.macsetup/backups")
    dry_run: bool = False
    allow_bootstrap: bool = False
    allow_privileged: bool = False
    enable_dns_blocklist: bool = True

    def command_exists(self, name: str) -> bool:
        return self.runner.which(name) is not None

    def brew_prefix(self) -> Path:
        return Path("/opt/homebrew")


def hiproc_package(**overrides: Any) -> SourceBuildPackage:
    values: dict[str, Any] = {
        "name": "hiproc",
        "repo": "git@github.com:example/hiproc.git",
        "build_system": "cargo",
        "binaries": ("hiproc",),
        "binary_dir": "target/release",
        "build_commands": ("cargo build --release",),
    }
    values.update(overrides)
    return SourceBuildPackage(**values)


class SourceBuildStepTests(unittest.TestCase):
    def test_missing_checkout_needs_clone(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            step = SourceBuildStep(hiproc_package())
            check = step.check(FakeContext(home=home, runner=FakeRunner()))

        self.assertEqual(check.status, StepStatus.NEEDS_CHANGE)
        self.assertIn("will clone", check.detail)

    def test_clone_build_and_copy_installs_binary(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            runner = FakeRunner()
            context = FakeContext(home=home, runner=runner)
            step = SourceBuildStep(hiproc_package())

            result = step.apply(context)

            installed = home / ".local" / "bin" / "hiproc"
            git_commands = [command.argv for command in runner.commands if command.argv]
            shell_commands = [
                command.shell for command in runner.commands if command.shell
            ]

            self.assertEqual(result.status, StepStatus.APPLIED)
            self.assertTrue(installed.exists())
            self.assertTrue(installed.stat().st_mode & stat.S_IXUSR)
            self.assertIn(
                (
                    "git",
                    "clone",
                    "git@github.com:example/hiproc.git",
                    str(home / ".local" / "src" / "macsetup" / "hiproc"),
                ),
                git_commands,
            )
            self.assertIn("cargo build --release", shell_commands)
            self.assertIn(home / ".local" / "bin", runner.path_dirs)

    def test_source_build_steps_include_path_mode_profile_block(self) -> None:
        package = hiproc_package(install_mode="path")
        step_ids = {step.id for step in build_steps(hiproc_package_recipe(package))}

        self.assertIn("source-build.hiproc", step_ids)
        self.assertIn("source-build.hiproc.path", step_ids)

    def test_path_mode_registers_built_binary_dir_for_current_run(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            runner = FakeRunner()
            context = FakeContext(home=home, runner=runner)
            step = SourceBuildStep(hiproc_package(install_mode="path"))

            result = step.apply(context)

            self.assertEqual(result.status, StepStatus.APPLIED)
            self.assertIn(
                home / ".local" / "src" / "macsetup" / "hiproc" / "target" / "release",
                runner.path_dirs,
            )


def hiproc_package_recipe(package: SourceBuildPackage):
    from macsetup.recipes import DEFAULT_RECIPE, SetupRecipe

    return SetupRecipe(
        python_version=DEFAULT_RECIPE.python_version,
        python_versions=DEFAULT_RECIPE.python_versions,
        python_tooling_packages=DEFAULT_RECIPE.python_tooling_packages,
        python_build_jobs=DEFAULT_RECIPE.python_build_jobs,
        python_build_env=DEFAULT_RECIPE.python_build_env,
        brew_formulas=DEFAULT_RECIPE.brew_formulas,
        brew_casks=DEFAULT_RECIPE.brew_casks,
        npm_global_packages=DEFAULT_RECIPE.npm_global_packages,
        python_global_packages=DEFAULT_RECIPE.python_global_packages,
        source_builds=(package,),
        git_settings=DEFAULT_RECIPE.git_settings,
        macos_defaults=DEFAULT_RECIPE.macos_defaults,
        manual_apps=DEFAULT_RECIPE.manual_apps,
        manual_notes=DEFAULT_RECIPE.manual_notes,
        dns_servers=DEFAULT_RECIPE.dns_servers,
        dns_passthrough_domains=DEFAULT_RECIPE.dns_passthrough_domains,
        dns_blocked_domains=DEFAULT_RECIPE.dns_blocked_domains,
    )


if __name__ == "__main__":
    unittest.main()
