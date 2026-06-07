import json
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from macsetup.graph import StepGraph
from macsetup.model import CommandResult, CommandSpec, StepStatus
from macsetup.recipes import DEFAULT_RECIPE, load_recipe
from macsetup.steps import PyenvPythonStep, build_steps


@dataclass
class RecordingRunner:
    versions_stdout: str = ""
    global_stdout: str = "system\n"
    pip_packages: tuple[str, ...] = ()
    runs: list[CommandSpec] = field(default_factory=list)
    path_dirs: list[Path] = field(default_factory=list)

    def which(self, name: str) -> str | None:
        return f"/fake/bin/{name}" if name == "pyenv" else None

    def add_path_dir(self, directory: Path) -> None:
        self.path_dirs.append(directory)

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
        del check, capture, dry_run, summarize_output, timeout_seconds, heartbeat
        self.runs.append(command)
        command_text = command.shell or " ".join(command.argv)
        if command.argv == ("pyenv", "versions", "--bare"):
            return CommandResult(
                command=command_text, returncode=0, stdout=self.versions_stdout
            )
        if command.argv == ("pyenv", "global"):
            return CommandResult(
                command=command_text, returncode=0, stdout=self.global_stdout
            )
        if command.argv == (
            "pyenv",
            "exec",
            "python",
            "-m",
            "pip",
            "list",
            "--format=json",
        ):
            return CommandResult(
                command=command_text,
                returncode=0,
                stdout=json.dumps([{"name": name} for name in self.pip_packages]),
            )
        return CommandResult(command=command_text, returncode=0)

    def sudo_validate(self, *, timeout_seconds: float = 120.0) -> CommandResult:
        del timeout_seconds
        return CommandResult(command="sudo -v", returncode=0)


@dataclass
class PyenvContext:
    runner: RecordingRunner
    home: Path = Path("/tmp")
    repo_root: Path = Path("/tmp/macsetup")
    backup_root: Path = Path("/tmp/.macsetup/backups")
    dry_run: bool = False
    allow_bootstrap: bool = True
    allow_privileged: bool = False
    enable_dns_blocklist: bool = True

    def command_exists(self, name: str) -> bool:
        return name == "pyenv"

    def brew_prefix(self) -> None:
        return None


class PyenvPythonTests(unittest.TestCase):
    def test_default_recipe_has_pyenv_tooling_packages(self) -> None:
        self.assertEqual(
            DEFAULT_RECIPE.python_tooling_packages,
            ("pip", "wheel", "setuptools", "uv", "poetry"),
        )
        self.assertEqual(DEFAULT_RECIPE.python_build_jobs, "auto")
        self.assertEqual(DEFAULT_RECIPE.python_build_env, ())

    def test_apply_installs_tooling_inside_exact_pyenv_version(self) -> None:
        runner = RecordingRunner()
        context = PyenvContext(runner=runner)
        step = PyenvPythonStep("3.14", ("pip", "wheel", "setuptools", "uv", "poetry"))

        with patch("macsetup.steps.os.cpu_count", return_value=8):
            result = step.apply(context)

        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertIn(
            CommandSpec(
                argv=("pyenv", "install", "--skip-existing", "3.14"),
                env={"MAKE_OPTS": "-j8"},
            ),
            runner.runs,
        )
        self.assertIn(
            CommandSpec(
                argv=(
                    "pyenv",
                    "exec",
                    "python",
                    "-m",
                    "pip",
                    "install",
                    "pip",
                    "wheel",
                    "setuptools",
                    "uv",
                    "poetry",
                    "-U",
                ),
                env={"PYENV_VERSION": "3.14"},
            ),
            runner.runs,
        )
        self.assertIn(
            CommandSpec(argv=("pyenv", "global", "3.14")),
            runner.runs,
        )
        self.assertEqual(
            result.changes[0]["tooling_packages"],
            ["pip", "wheel", "setuptools", "uv", "poetry"],
        )

    def test_apply_respects_pyenv_build_env_override(self) -> None:
        runner = RecordingRunner()
        context = PyenvContext(runner=runner)
        step = PyenvPythonStep(
            "3.14",
            ("pip",),
            build_jobs="auto",
            build_env=(
                ("MAKE_OPTS", "-j4"),
                ("PYTHON_CONFIGURE_OPTS", "--enable-shared"),
            ),
        )

        result = step.apply(context)

        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertIn(
            CommandSpec(
                argv=("pyenv", "install", "--skip-existing", "3.14"),
                env={
                    "MAKE_OPTS": "-j4",
                    "PYTHON_CONFIGURE_OPTS": "--enable-shared",
                },
            ),
            runner.runs,
        )

    def test_check_requires_missing_tooling_packages(self) -> None:
        runner = RecordingRunner(
            versions_stdout="3.14.5\n",
            global_stdout="3.14.5\n",
            pip_packages=("pip", "wheel"),
        )
        context = PyenvContext(runner=runner)
        step = PyenvPythonStep("3.14", ("pip", "wheel", "setuptools", "uv", "poetry"))

        check = step.check(context)

        self.assertEqual(check.status, StepStatus.NEEDS_CHANGE)
        self.assertIn("missing tooling: setuptools, uv, poetry", check.detail)
        pip_probe = next(
            command
            for command in runner.runs
            if command.argv
            == ("pyenv", "exec", "python", "-m", "pip", "list", "--format=json")
        )
        self.assertEqual(pip_probe.env, {"PYENV_VERSION": "3.14.5"})

    def test_profile_can_manage_extra_versions_before_selected_global(self) -> None:
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.toml"
            profile.write_text(
                "[python]\nversions = ['3.11', '3.12', '3.13']\nbuild_jobs = 6\nbuild_env = { PYTHON_CONFIGURE_OPTS = '--enable-shared' }\n",
                encoding="utf-8",
            )
            recipe = load_recipe((profile,))

        steps = StepGraph(build_steps(recipe)).selected(
            only_tags=frozenset({"python"}), skip_tags=frozenset()
        )
        pyenv_steps = [step for step in steps if isinstance(step, PyenvPythonStep)]

        self.assertEqual(
            [step.version for step in pyenv_steps], ["3.11", "3.12", "3.13", "3.14"]
        )
        self.assertEqual(
            [step.id for step in pyenv_steps],
            [
                "python.pyenv-version.3.11",
                "python.pyenv-version.3.12",
                "python.pyenv-version.3.13",
                "python.pyenv-version",
            ],
        )
        self.assertFalse(pyenv_steps[0].select_global)
        self.assertFalse(pyenv_steps[1].select_global)
        self.assertFalse(pyenv_steps[2].select_global)
        self.assertTrue(pyenv_steps[3].select_global)
        self.assertTrue(all(step.build_jobs == "6" for step in pyenv_steps))
        self.assertTrue(
            all(
                step.build_env == (("PYTHON_CONFIGURE_OPTS", "--enable-shared"),)
                for step in pyenv_steps
            )
        )


if __name__ == "__main__":
    unittest.main()
