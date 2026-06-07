import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from macsetup.graph import StepGraph
from macsetup.model import CommandResult, CommandSpec, StepStatus
from macsetup.recipes import DEFAULT_RECIPE, MacDefaultsSetting
from macsetup.runner import LocalContext, display_command
from macsetup.steps import MacDefaultsStep, ManagedDirectoryStep, build_steps
from macsetup.unapply import JournalResult, unapply_step


@dataclass
class DefaultsRunner:
    dry_run: bool = False
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)

    def which(self, name: str) -> str | None:
        if name in {"defaults", "mkdir"}:
            return name
        return None

    def add_path_dir(self, directory: Path) -> None:
        pass

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
        rendered = display_command(command)
        self.commands.append(rendered)
        effective_dry_run = self.dry_run if dry_run is None else dry_run
        if effective_dry_run:
            return CommandResult(rendered, 0, skipped=True)
        if command.shell is not None:
            return CommandResult(rendered, 0)
        argv = command.argv
        if argv[:2] == ("defaults", "read"):
            key = (argv[2], argv[3])
            if key not in self.values:
                return CommandResult(rendered, 1, stderr="does not exist")
            return CommandResult(rendered, 0, stdout=self.values[key] + "\n")
        if argv[:2] == ("defaults", "write"):
            self.values[(argv[2], argv[3])] = argv[5]
            return CommandResult(rendered, 0)
        if argv[:2] == ("defaults", "delete"):
            self.values.pop((argv[2], argv[3]), None)
            return CommandResult(rendered, 0)
        if argv[:2] == ("mkdir", "-p"):
            Path(argv[2]).mkdir(parents=True, exist_ok=True)
            return CommandResult(rendered, 0)
        return CommandResult(rendered, 127, stderr="unknown command")

    def state(self, state: str, detail: str) -> None:
        pass

    def sudo_validate(self, *, timeout_seconds: float = 120.0) -> CommandResult:
        return CommandResult("sudo -v", 0)


def screenshot_setting() -> MacDefaultsSetting:
    return MacDefaultsSetting(
        name="screenshot-location",
        domain="com.apple.screencapture",
        key="location",
        value="~/Desktop/Screenshots",
        value_type="path",
        directories=("~/Desktop/Screenshots",),
        tags=("macos", "defaults", "screenshots"),
    )


def context_for(home: Path, runner: DefaultsRunner) -> LocalContext:
    return LocalContext(
        home=home,
        repo_root=Path("/example/macsetup"),
        backup_root=home / ".macsetup" / "backups",
        runner=runner,  # type: ignore[arg-type]
        dry_run=runner.dry_run,
    )


class MacDefaultsTests(unittest.TestCase):
    def test_screenshot_selection_orders_directory_before_default(self) -> None:
        steps = StepGraph(build_steps(DEFAULT_RECIPE)).selected(
            only_tags=frozenset({"screenshots"}), skip_tags=frozenset()
        )

        self.assertEqual(
            [step.id for step in steps],
            [
                "macos.directory.desktop-screenshots",
                "macos.defaults.screenshot-location",
            ],
        )

    def test_screenshot_apply_creates_directory_and_writes_default(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            runner = DefaultsRunner()
            context = context_for(home, runner)
            setting = screenshot_setting()
            directory_step = ManagedDirectoryStep(
                setting.directories[0], tags=frozenset(setting.tags)
            )
            defaults_step = MacDefaultsStep(setting)

            directory_result = directory_step.apply(context)
            defaults_result = defaults_step.apply(context)

            self.assertEqual(directory_result.status, StepStatus.APPLIED)
            self.assertEqual(defaults_result.status, StepStatus.APPLIED)
            self.assertTrue((home / "Desktop" / "Screenshots").is_dir())
        self.assertEqual(
            runner.values[("com.apple.screencapture", "location")],
            str(home / "Desktop" / "Screenshots"),
        )
        self.assertEqual(defaults_result.changes[0]["had_value"], False)

    def test_unapply_restores_previous_default_value(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            setting = screenshot_setting()
            desired = str(home / "Desktop" / "Screenshots")
            previous = str(home / "Desktop")
            runner = DefaultsRunner(
                values={("com.apple.screencapture", "location"): desired}
            )
            context = context_for(home, runner)
            step = MacDefaultsStep(setting)

            result = unapply_step(
                step,
                context,
                force=False,
                journal_result=JournalResult(
                    step.id,
                    step.title,
                    "applied",
                    "",
                    (),
                    (
                        {
                            "type": "macos_default",
                            "name": setting.name,
                            "domain": setting.domain,
                            "key": setting.key,
                            "value_type": setting.value_type,
                            "previous": previous,
                            "had_value": True,
                            "after": desired,
                        },
                    ),
                ),
            )

        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertEqual(
            runner.values[("com.apple.screencapture", "location")], previous
        )

    def test_unapply_deletes_default_when_it_was_unset_before_apply(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            setting = screenshot_setting()
            desired = str(home / "Desktop" / "Screenshots")
            runner = DefaultsRunner(
                values={("com.apple.screencapture", "location"): desired}
            )
            context = context_for(home, runner)
            step = MacDefaultsStep(setting)

            result = unapply_step(
                step,
                context,
                force=False,
                journal_result=JournalResult(
                    step.id,
                    step.title,
                    "applied",
                    "",
                    (),
                    (
                        {
                            "type": "macos_default",
                            "name": setting.name,
                            "domain": setting.domain,
                            "key": setting.key,
                            "value_type": setting.value_type,
                            "previous": "",
                            "had_value": False,
                            "after": desired,
                        },
                    ),
                ),
            )

        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertNotIn(("com.apple.screencapture", "location"), runner.values)

    def test_unapply_leaves_non_empty_managed_directory_in_place(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / "Desktop" / "Screenshots"
            path.mkdir(parents=True)
            (path / "screen.png").write_text("content", encoding="utf-8")
            runner = DefaultsRunner()
            context = context_for(home, runner)
            step = ManagedDirectoryStep("~/Desktop/Screenshots")

            result = unapply_step(
                step,
                context,
                force=False,
                journal_result=JournalResult(
                    step.id,
                    step.title,
                    "applied",
                    "",
                    (),
                    (
                        {
                            "type": "managed_directory",
                            "path": str(path),
                            "profile_path": "~/Desktop/Screenshots",
                            "existed_before": False,
                        },
                    ),
                ),
            )

            self.assertEqual(result.status, StepStatus.BLOCKED)
            self.assertTrue((path / "screen.png").exists())


if __name__ == "__main__":
    unittest.main()
