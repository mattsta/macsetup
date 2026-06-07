import unittest
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from macsetup import steps as steps_module
from macsetup import unapply as unapply_module
from macsetup.model import CommandResult, CommandSpec, StepStatus
from macsetup.runner import LocalContext, display_command
from macsetup.steps import XcodeDeveloperDirectoryStep, XcodeMetalToolchainStep
from macsetup.unapply import JournalResult, unapply_step


@dataclass(frozen=True)
class XcodePaths:
    developer_dir: Path
    xcode_select: Path
    xcodebuild: Path
    xcrun: Path


@dataclass
class XcodeRunner:
    selected: str
    metal_path: str = ""
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def which(self, name: str) -> str | None:
        return name

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
        if command.shell is not None:
            return CommandResult(rendered, 127, stderr="unexpected shell command")
        argv = command.argv
        self.commands.append(argv)
        if argv == (str(steps_module.XCODE_SELECT_PATH), "-p"):
            if not self.selected:
                return CommandResult(rendered, 1, stderr="not selected")
            return CommandResult(rendered, 0, stdout=self.selected + "\n")
        if argv[:3] == ("sudo", str(steps_module.XCODE_SELECT_PATH), "-s"):
            self.selected = argv[3]
            return CommandResult(rendered, 0)
        if argv == (str(steps_module.XCRUN_PATH), "--find", "metal"):
            if not self.metal_path:
                return CommandResult(rendered, 1, stderr="metal not found")
            return CommandResult(rendered, 0, stdout=self.metal_path + "\n")
        if argv == (
            str(steps_module.XCODEBUILD_PATH),
            "-downloadComponent",
            "MetalToolchain",
        ):
            self.metal_path = "/Applications/Xcode.app/metal"
            return CommandResult(rendered, 0)
        if argv == (str(steps_module.XCODEBUILD_PATH), "-license", "check"):
            return CommandResult(rendered, 0)
        return CommandResult(rendered, 127, stderr="unexpected command")

    def state(self, state: str, detail: str) -> None:
        pass

    def sudo_validate(self, *, timeout_seconds: float = 120.0) -> CommandResult:
        return CommandResult("sudo -v", 0)


@contextmanager
def patched_xcode_paths(root: Path, *, installed: bool = True):
    developer_dir = root / "Applications" / "Xcode.app" / "Contents" / "Developer"
    xcode_select = root / "usr" / "bin" / "xcode-select"
    xcodebuild = root / "usr" / "bin" / "xcodebuild"
    xcrun = root / "usr" / "bin" / "xcrun"
    for path in (xcode_select, xcodebuild, xcrun):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    if installed:
        developer_dir.mkdir(parents=True)
    paths = XcodePaths(developer_dir, xcode_select, xcodebuild, xcrun)
    with (
        patch.object(steps_module, "XCODE_APP_DEVELOPER_DIR", developer_dir),
        patch.object(steps_module, "XCODE_SELECT_PATH", xcode_select),
        patch.object(steps_module, "XCODEBUILD_PATH", xcodebuild),
        patch.object(steps_module, "XCRUN_PATH", xcrun),
        patch.object(unapply_module, "XCODE_SELECT_PATH", xcode_select),
    ):
        yield paths


def context_for(runner: XcodeRunner, *, allow_privileged: bool = False) -> LocalContext:
    return LocalContext(
        home=Path("/example/home"),
        repo_root=Path("/example/macsetup"),
        backup_root=Path("/example/home/.macsetup/backups"),
        runner=runner,  # type: ignore[arg-type]
        dry_run=False,
        allow_privileged=allow_privileged,
    )


class XcodeStepTests(unittest.TestCase):
    def test_developer_directory_blocks_without_privileged_apply(self) -> None:
        with TemporaryDirectory() as directory:
            with patched_xcode_paths(Path(directory)) as paths:
                runner = XcodeRunner(selected="/Library/Developer/CommandLineTools")
                step = XcodeDeveloperDirectoryStep()

                check = step.check(context_for(runner))

        self.assertEqual(check.status, StepStatus.BLOCKED)
        self.assertIn("requires privileged apply", check.detail)
        self.assertIn(str(paths.developer_dir), check.detail)

    def test_developer_directory_apply_selects_installed_xcode(self) -> None:
        with TemporaryDirectory() as directory:
            with patched_xcode_paths(Path(directory)) as paths:
                runner = XcodeRunner(selected="/Library/Developer/CommandLineTools")
                step = XcodeDeveloperDirectoryStep()

                result = step.apply(context_for(runner, allow_privileged=True))

        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertEqual(runner.selected, str(paths.developer_dir))
        self.assertEqual(
            result.changes[0]["previous"], "/Library/Developer/CommandLineTools"
        )
        self.assertEqual(result.changes[0]["after"], str(paths.developer_dir))

    def test_developer_directory_noops_when_xcode_is_absent(self) -> None:
        with TemporaryDirectory() as directory:
            with patched_xcode_paths(Path(directory), installed=False):
                runner = XcodeRunner(selected="/Library/Developer/CommandLineTools")
                step = XcodeDeveloperDirectoryStep()

                check = step.check(context_for(runner, allow_privileged=True))

        self.assertEqual(check.status, StepStatus.PRESENT)
        self.assertIn("absent", check.detail)

    def test_metal_toolchain_downloads_when_xcrun_cannot_find_metal(self) -> None:
        with TemporaryDirectory() as directory:
            with patched_xcode_paths(Path(directory)):
                runner = XcodeRunner(selected=str(steps_module.XCODE_APP_DEVELOPER_DIR))
                step = XcodeMetalToolchainStep()

                result = step.apply(context_for(runner))

        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertEqual(runner.metal_path, "/Applications/Xcode.app/metal")

    def test_metal_toolchain_noops_when_metal_is_available(self) -> None:
        with TemporaryDirectory() as directory:
            with patched_xcode_paths(Path(directory)):
                runner = XcodeRunner(
                    selected=str(steps_module.XCODE_APP_DEVELOPER_DIR),
                    metal_path="/Applications/Xcode.app/metal",
                )
                step = XcodeMetalToolchainStep()

                check = step.check(context_for(runner))

        self.assertEqual(check.status, StepStatus.PRESENT)
        self.assertIn("Metal toolchain is available", check.detail)

    def test_unapply_restores_previous_developer_directory(self) -> None:
        with TemporaryDirectory() as directory:
            with patched_xcode_paths(Path(directory)) as paths:
                runner = XcodeRunner(selected=str(paths.developer_dir))
                step = XcodeDeveloperDirectoryStep()

                result = unapply_step(
                    step,
                    context_for(runner, allow_privileged=True),
                    force=False,
                    journal_result=JournalResult(
                        step.id,
                        step.title,
                        "applied",
                        "",
                        (),
                        (
                            {
                                "type": "xcode_developer_dir",
                                "previous": "/Library/Developer/CommandLineTools",
                                "had_value": True,
                                "after": str(paths.developer_dir),
                            },
                        ),
                    ),
                )

        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertEqual(runner.selected, "/Library/Developer/CommandLineTools")


if __name__ == "__main__":
    unittest.main()
