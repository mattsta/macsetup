import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from macsetup.model import CommandResult, CommandSpec, StepStatus
from macsetup.recipes import BrewPackage
from macsetup.reporting import format_remediations
from macsetup.steps import BrewPackagesStep


@dataclass
class FakeRunner:
    commands: list[tuple[str, ...]] = field(default_factory=list)
    states: list[tuple[str, str]] = field(default_factory=list)
    formula_list_stdout: str = ""
    cask_list_stdout: str = ""

    def which(self, name: str) -> str | None:
        return "/opt/homebrew/bin/brew" if name == "brew" else None

    def add_path_dir(self, directory: Path) -> None:
        del directory

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
        self.commands.append(command.argv)
        rendered = " ".join(command.argv)
        if command.argv[:4] == ("brew", "list", "--formula", "--versions"):
            return CommandResult(
                command=rendered, returncode=0, stdout=self.formula_list_stdout
            )
        if command.argv[:3] == ("brew", "list", "--cask"):
            return CommandResult(
                command=rendered, returncode=0, stdout=self.cask_list_stdout
            )
        return CommandResult(command=rendered, returncode=0)

    def sudo_validate(self, *, timeout_seconds: float = 120.0) -> CommandResult:
        del timeout_seconds
        return CommandResult(command="sudo -v", returncode=0)


@dataclass
class FakeContext:
    runner: FakeRunner
    prefix: Path = Path("/opt/homebrew")
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


class BrewPackagesStepTests(unittest.TestCase):
    def test_installs_missing_formulas_in_one_batch_with_package_states(self) -> None:
        runner = FakeRunner()
        context = FakeContext(runner=runner)
        step = BrewPackagesStep(
            id="brew.formulas.test",
            title="Install test formulas",
            packages=(BrewPackage("alpha"), BrewPackage("beta")),
        )

        result = step.apply(context)

        install_commands = [
            command for command in runner.commands if command[:2] == ("brew", "install")
        ]
        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertEqual(
            install_commands,
            [("brew", "install", "alpha", "beta")],
        )
        self.assertIn(
            ("brew.install.start", "2 formula(s): alpha, beta"), runner.states
        )
        self.assertIn(("brew.install.done", "2 formula(s) exit=0"), runner.states)

    def test_installs_missing_casks_in_one_batch(self) -> None:
        with TemporaryDirectory() as directory:
            prefix_path = Path(directory) / "prefix"
            home = Path(directory) / "home"
            runner = FakeRunner()
            context = FakeContext(runner=runner, prefix=prefix_path, home=home)
            step = BrewPackagesStep(
                id="brew.casks.test",
                title="Install test casks",
                packages=(BrewPackage("wezterm"), BrewPackage("firefox")),
                cask=True,
            )

            result = step.apply(context)

        install_commands = [
            command for command in runner.commands if command[:2] == ("brew", "install")
        ]
        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertEqual(
            install_commands,
            [("brew", "install", "--cask", "wezterm", "firefox")],
        )

    def test_cask_probe_uses_caskroom_dirs_without_loading_cask_definitions(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            prefix_path = Path(directory)
            (prefix_path / "Caskroom" / "phantomjs").mkdir(parents=True)
            runner = FakeRunner()
            context = FakeContext(runner=runner, prefix=prefix_path)
            step = BrewPackagesStep(
                id="brew.casks.test",
                title="Install test casks",
                packages=(BrewPackage("wezterm"),),
                cask=True,
            )

            check = step.check(context)

        self.assertEqual(check.status, StepStatus.NEEDS_CHANGE)
        self.assertFalse(
            any(
                command[:3] == ("brew", "list", "--cask") for command in runner.commands
            )
        )

    def test_package_plan_lists_installed_and_missing_names(self) -> None:
        with TemporaryDirectory() as directory:
            prefix_path = Path(directory)
            (prefix_path / "Cellar" / "alpha").mkdir(parents=True)
            runner = FakeRunner(formula_list_stdout="alpha 1.0\n")
            context = FakeContext(runner=runner, prefix=prefix_path)
            step = BrewPackagesStep(
                id="brew.formulas.test",
                title="Install test formulas",
                packages=(BrewPackage("alpha"), BrewPackage("beta")),
            )

            check = step.check(context)

        self.assertEqual(check.status, StepStatus.NEEDS_CHANGE)
        self.assertIn("installed: alpha", check.detail)
        self.assertIn("missing: beta", check.detail)

    def test_cask_plan_lists_unmanaged_app_bundles(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            applications = home / "Applications"
            applications.mkdir(parents=True)
            (applications / "Windscribe.app").mkdir()
            runner = FakeRunner()
            context = FakeContext(
                runner=runner, prefix=Path(directory) / "prefix", home=home
            )
            step = BrewPackagesStep(
                id="brew.casks.test",
                title="Install test casks",
                packages=(BrewPackage("windscribe", app_bundles=("Windscribe.app",)),),
                cask=True,
            )

            check = step.check(context)

        self.assertEqual(check.status, StepStatus.BLOCKED)
        self.assertIn("unmanaged app bundle(s): windscribe", check.detail)
        self.assertIn("Windscribe.app", check.detail)

    def test_cask_plan_requires_declared_app_bundle_when_caskroom_exists(self) -> None:
        with TemporaryDirectory() as directory:
            prefix_path = Path(directory) / "prefix"
            (prefix_path / "Caskroom" / "remotevpn").mkdir(parents=True)
            home = Path(directory) / "home"
            runner = FakeRunner()
            context = FakeContext(runner=runner, prefix=prefix_path, home=home)
            step = BrewPackagesStep(
                id="brew.casks.test",
                title="Install test casks",
                packages=(BrewPackage("remotevpn", app_bundles=("Remote VPN.app",)),),
                cask=True,
            )

            check = step.check(context)

        self.assertEqual(check.status, StepStatus.NEEDS_CHANGE)
        self.assertIn("repair: remotevpn", check.detail)
        self.assertIn(
            "Homebrew cask is recorded but expected valid app bundle is missing",
            check.detail,
        )
        self.assertIn("/Applications/Remote VPN.app", check.detail)

    def test_cask_apply_repairs_declared_app_bundle_drift_with_reinstall(self) -> None:
        with TemporaryDirectory() as directory:
            prefix_path = Path(directory) / "prefix"
            (prefix_path / "Caskroom" / "remotevpn").mkdir(parents=True)
            home = Path(directory) / "home"
            runner = FakeRunner()
            context = FakeContext(runner=runner, prefix=prefix_path, home=home)
            step = BrewPackagesStep(
                id="brew.casks.test",
                title="Install test casks",
                packages=(BrewPackage("remotevpn", app_bundles=("Remote VPN.app",)),),
                cask=True,
            )

            result = step.apply(context)

        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertIn(("brew", "reinstall", "--cask", "remotevpn"), runner.commands)
        self.assertIn("repair 1 package(s): remotevpn", result.detail)

    def test_installer_backed_cask_reports_staged_installer_as_manual_recovery(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            prefix_path = Path(directory) / "prefix"
            (prefix_path / "Caskroom" / "remotevpn").mkdir(parents=True)
            home = Path(directory) / "home"
            installer = home / "Applications" / "Remote VPN Installer.app"
            (installer / "Contents").mkdir(parents=True)
            (installer / "Contents" / "Info.plist").write_text(
                "<plist />", encoding="utf-8"
            )
            runner = FakeRunner()
            context = FakeContext(runner=runner, prefix=prefix_path, home=home)
            step = BrewPackagesStep(
                id="brew.casks.test",
                title="Install test casks",
                packages=(
                    BrewPackage(
                        "remotevpn",
                        app_bundles=("Remote VPN.app",),
                        installer_bundles=("Remote VPN Installer.app",),
                    ),
                ),
                cask=True,
            )

            check = step.check(context)
            rendered_recovery = format_remediations((check,))

        self.assertEqual(check.status, StepStatus.MANUAL)
        self.assertIn("installer pending: remotevpn", check.detail)
        self.assertIn(str(installer), check.detail)
        self.assertIn("Recommended Recovery", rendered_recovery)
        self.assertIn("open ", rendered_recovery)
        self.assertIn(str(installer), rendered_recovery)
        self.assertIn("brew reinstall --cask remotevpn", rendered_recovery)

    def test_installer_backed_missing_cask_reports_manual_followup_after_brew_install(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            prefix_path = Path(directory) / "prefix"
            home = Path(directory) / "home"
            runner = FakeRunner()
            context = FakeContext(runner=runner, prefix=prefix_path, home=home)
            step = BrewPackagesStep(
                id="brew.casks.test",
                title="Install test casks",
                packages=(
                    BrewPackage(
                        "remotevpn",
                        app_bundles=("Remote VPN.app",),
                        installer_bundles=("Remote VPN Installer.app",),
                    ),
                ),
                cask=True,
            )

            check = step.check(context)
            result = step.apply(context)
            rendered_recovery = format_remediations((check, result))

        self.assertEqual(check.status, StepStatus.NEEDS_CHANGE)
        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertIn(("brew", "install", "--cask", "remotevpn"), runner.commands)
        self.assertIn("brew install --cask remotevpn", rendered_recovery)
        self.assertNotIn("/Applications/Remote VPN Installer.app", rendered_recovery)
        self.assertIn(
            'caskroom="$(brew --prefix)/Caskroom/remotevpn"',
            rendered_recovery,
        )
        self.assertIn(
            "find \"$caskroom\" -name 'Remote VPN Installer.app'",
            rendered_recovery,
        )

    def test_cask_plan_finds_staged_installer_under_homebrew_caskroom(self) -> None:
        with TemporaryDirectory() as directory:
            prefix_path = Path(directory) / "prefix"
            caskroom = prefix_path / "Caskroom" / "remotevpn" / "1.0.0"
            installer = caskroom / "Remote VPN Installer.app" / "Contents"
            installer.mkdir(parents=True)
            (installer / "Info.plist").write_text("<plist />", encoding="utf-8")
            home = Path(directory) / "home"
            runner = FakeRunner()
            context = FakeContext(runner=runner, prefix=prefix_path, home=home)
            step = BrewPackagesStep(
                id="brew.casks.test",
                title="Install test casks",
                packages=(
                    BrewPackage(
                        "remotevpn",
                        app_bundles=("Remote VPN.app",),
                        installer_bundles=("Remote VPN Installer.app",),
                    ),
                ),
                cask=True,
            )

            check = step.check(context)
            rendered_recovery = format_remediations((check,))

        self.assertEqual(check.status, StepStatus.MANUAL)
        self.assertIn("installer pending: remotevpn", check.detail)
        self.assertIn("open ", rendered_recovery)
        self.assertIn(str(caskroom), rendered_recovery)
        self.assertIn("Remote VPN Installer.app", rendered_recovery)

    def test_cask_plan_lists_installed_app_bundle_path_when_receipt_and_app_exist(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            prefix_path = Path(directory) / "prefix"
            (prefix_path / "Caskroom" / "remotevpn").mkdir(parents=True)
            home = Path(directory) / "home"
            applications = home / "Applications"
            applications.mkdir(parents=True)
            (applications / "Remote VPN.app" / "Contents").mkdir(parents=True)
            (applications / "Remote VPN.app" / "Contents" / "Info.plist").write_text(
                "<plist />", encoding="utf-8"
            )
            runner = FakeRunner()
            context = FakeContext(runner=runner, prefix=prefix_path, home=home)
            step = BrewPackagesStep(
                id="brew.casks.test",
                title="Install test casks",
                packages=(BrewPackage("remotevpn", app_bundles=("Remote VPN.app",)),),
                cask=True,
            )

            check = step.check(context)

        self.assertEqual(check.status, StepStatus.PRESENT)
        self.assertIn("installed: remotevpn", check.detail)
        self.assertIn("Remote VPN.app", check.detail)


if __name__ == "__main__":
    unittest.main()
