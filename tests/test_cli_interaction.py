import io
import json
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from macsetup.cli import (
    build_parser,
    confirm_apply,
    discover_default_profile_paths,
    main,
    prepare_state_for_mutation,
    print_remediation_report,
    run_checks,
)
from macsetup.model import (
    CommandResult,
    CommandSpec,
    Remediation,
    ResourceRef,
    Risk,
    SetupContext,
    StepCheck,
    StepResult,
    StepStatus,
)
from macsetup.ownership import UserIdentity
from macsetup.reporting import format_actionable_checks, format_remediations
from macsetup.runner import CommandRunner
from macsetup.steps import SudoTouchIdProbe, SudoTouchIdStep


@dataclass
class DummyContext:
    home: Path = Path("/tmp")
    repo_root: Path = Path("/tmp/macsetup")
    backup_root: Path = Path("/tmp/.macsetup/backups")
    runner: CommandRunner = field(default_factory=lambda: CommandRunner(dry_run=True))
    dry_run: bool = True
    allow_bootstrap: bool = False
    allow_privileged: bool = False
    enable_dns_blocklist: bool = True

    def command_exists(self, name: str) -> bool:
        return False

    def brew_prefix(self) -> None:
        return None


@dataclass(frozen=True)
class SlowLookingStep:
    id: str = "example.step"
    title: str = "Example Step"
    tags: frozenset[str] = frozenset({"test"})
    risks: frozenset[Risk] = frozenset()
    requires: frozenset[ResourceRef] = frozenset()
    provides: frozenset[ResourceRef] = frozenset()
    owns: frozenset[ResourceRef] = frozenset()

    def check(self, context: SetupContext) -> StepCheck:
        return StepCheck(
            self.id, self.title, StepStatus.PRESENT, "ok", self.tags, self.risks
        )

    def apply(self, context: SetupContext) -> StepResult:
        return StepResult(self.id, self.title, StepStatus.PRESENT, "ok")


@dataclass(frozen=True)
class ActionableLookingStep:
    id: str = "example.action"
    title: str = "Example Action"
    tags: frozenset[str] = frozenset({"test"})
    risks: frozenset[Risk] = frozenset({Risk.USER_FILE})
    requires: frozenset[ResourceRef] = frozenset()
    provides: frozenset[ResourceRef] = frozenset()
    owns: frozenset[ResourceRef] = frozenset()

    def check(self, context: SetupContext) -> StepCheck:
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            "will update example",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        return StepResult(self.id, self.title, StepStatus.APPLIED, "updated example")


@dataclass(frozen=True)
class BootstrapAwareStep:
    id: str = "example.bootstrap"
    title: str = "Example Bootstrap"
    tags: frozenset[str] = frozenset({"test"})
    risks: frozenset[Risk] = frozenset({Risk.NETWORK})
    requires: frozenset[ResourceRef] = frozenset()
    provides: frozenset[ResourceRef] = frozenset()
    owns: frozenset[ResourceRef] = frozenset()

    def check(self, context: SetupContext) -> StepCheck:
        if not context.allow_bootstrap:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "bootstrap disabled",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            "will bootstrap",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        if not context.allow_bootstrap:
            return StepResult(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "bootstrap disabled",
            )
        return StepResult(self.id, self.title, StepStatus.APPLIED, "bootstrapped")


PLANNED_TOOL = ResourceRef("tool", "planned")


@dataclass(frozen=True)
class PlannedProviderStep:
    id: str = "example.provider"
    title: str = "Example Provider"
    tags: frozenset[str] = frozenset({"test"})
    risks: frozenset[Risk] = frozenset()
    requires: frozenset[ResourceRef] = frozenset()
    provides: frozenset[ResourceRef] = frozenset({PLANNED_TOOL})
    owns: frozenset[ResourceRef] = frozenset()
    status: StepStatus = StepStatus.NEEDS_CHANGE

    def check(self, context: SetupContext) -> StepCheck:
        return StepCheck(
            self.id,
            self.title,
            self.status,
            "provider check",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        return StepResult(self.id, self.title, StepStatus.APPLIED, "provided")


@dataclass(frozen=True)
class RequiresPlannedProviderStep:
    id: str = "example.dependent"
    title: str = "Example Dependent"
    tags: frozenset[str] = frozenset({"test"})
    risks: frozenset[Risk] = frozenset()
    requires: frozenset[ResourceRef] = frozenset({PLANNED_TOOL})
    provides: frozenset[ResourceRef] = frozenset()
    owns: frozenset[ResourceRef] = frozenset()

    def check(self, context: SetupContext) -> StepCheck:
        raise AssertionError("dependent check should be deferred")

    def apply(self, context: SetupContext) -> StepResult:
        return StepResult(self.id, self.title, StepStatus.APPLIED, "dependent")


@dataclass(frozen=True)
class PrivilegedActionableLookingStep(ActionableLookingStep):
    id: str = "example.privileged"
    title: str = "Example Privileged Action"
    risks: frozenset[Risk] = frozenset({Risk.PRIVILEGED})


@dataclass(frozen=True)
class InterruptingApplyStep(ActionableLookingStep):
    id: str = "example.interrupt"
    title: str = "Example Interrupt"

    def apply(self, context: SetupContext) -> StepResult:
        if isinstance(context.runner, CommandRunner):
            context.runner.last_interrupted_command = "brew install demo"
        raise KeyboardInterrupt


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[CommandSpec] = []

    def run(self, command: CommandSpec, **kwargs: object) -> CommandResult:
        self.commands.append(command)
        return CommandResult(command="sudo repair", returncode=0)


class CliInteractionTests(unittest.TestCase):
    def test_default_profile_discovery_loads_repo_local_toml(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            local = repo / "local"
            local.mkdir(parents=True)
            first = local / "matt.toml"
            second = local / "work.toml"
            first.write_text("[profile]\nname = 'matt'\n", encoding="utf-8")
            second.write_text("[profile]\nname = 'work'\n", encoding="utf-8")

            discovered = discover_default_profile_paths(repo, cwd=Path(directory))

        self.assertEqual(discovered, (first, second))

    def test_default_profile_discovery_deduplicates_cwd_repo_match(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            local = repo / "local"
            local.mkdir()
            profile = local / "matt.toml"
            profile.write_text("[profile]\nname = 'matt'\n", encoding="utf-8")

            discovered = discover_default_profile_paths(repo, cwd=repo)

        self.assertEqual(discovered, (profile,))

    def test_dns_blocklist_is_enabled_by_default_and_can_be_disabled(self) -> None:
        parser = build_parser()

        self.assertFalse(
            parser.parse_args(("apply", "--dry-run")).disable_dns_blocklist
        )
        self.assertTrue(
            parser.parse_args(
                ("apply", "--dry-run", "--disable-dns-blocklist")
            ).disable_dns_blocklist
        )
        self.assertFalse(
            parser.parse_args(
                ("apply", "--dry-run", "--enable-dns-blocklist")
            ).disable_dns_blocklist
        )
        self.assertTrue(
            parser.parse_args(("plan", "--disable-dns-blocklist")).disable_dns_blocklist
        )

    def test_plan_and_preview_accept_allow_privileged_for_planning(self) -> None:
        parser = build_parser()

        self.assertTrue(
            parser.parse_args(("plan", "--allow-privileged")).allow_privileged
        )
        self.assertTrue(
            parser.parse_args(("preview", "--allow-privileged")).allow_privileged
        )

    def test_bootstrap_is_enabled_unless_explicitly_disabled(self) -> None:
        parser = build_parser()

        self.assertFalse(parser.parse_args(("apply",)).no_bootstrap)
        self.assertTrue(parser.parse_args(("apply", "--no-bootstrap")).no_bootstrap)
        self.assertTrue(parser.parse_args(("plan", "--no-bootstrap")).no_bootstrap)
        self.assertTrue(parser.parse_args(("preview", "--no-bootstrap")).no_bootstrap)
        self.assertFalse(parser.parse_args(("apply", "--allow-bootstrap")).no_bootstrap)

    def test_run_checks_streams_each_step(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            checks = run_checks((SlowLookingStep(),), DummyContext(), stream=True)

        rendered = output.getvalue()
        self.assertEqual(len(checks), 1)
        self.assertIn("Checking 1 selected step(s)", rendered)
        self.assertIn("CHECK    example.step", rendered)
        self.assertIn("OK       example.step", rendered)

    def test_run_checks_defers_dependents_with_planned_requirements(self) -> None:
        checks = run_checks(
            (PlannedProviderStep(), RequiresPlannedProviderStep()),
            DummyContext(),
            stream=False,
        )

        self.assertEqual(checks[0].status, StepStatus.NEEDS_CHANGE)
        self.assertEqual(checks[1].status, StepStatus.UNKNOWN)
        self.assertIn("will check after planned requirement(s)", checks[1].detail)
        self.assertIn(str(PLANNED_TOOL), checks[1].detail)

    def test_run_checks_skips_dependents_with_blocked_requirements(self) -> None:
        checks = run_checks(
            (
                PlannedProviderStep(status=StepStatus.BLOCKED),
                RequiresPlannedProviderStep(),
            ),
            DummyContext(),
            stream=False,
        )

        self.assertEqual(checks[0].status, StepStatus.BLOCKED)
        self.assertEqual(checks[1].status, StepStatus.SKIPPED)
        self.assertIn("unmet requirement(s)", checks[1].detail)
        self.assertIn(str(PLANNED_TOOL), checks[1].detail)

    def test_remediation_report_prints_recovery_commands(self) -> None:
        check = StepCheck(
            "example.step",
            "Example Step",
            StepStatus.MANUAL,
            "installer pending",
            frozenset({"test"}),
            frozenset(),
            remediations=(
                Remediation(
                    "installer must be completed manually",
                    commands=("open '/Applications/Example Installer.app'",),
                    manual_steps=("Finish the installer UI.",),
                ),
            ),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            print_remediation_report([check])

        rendered = output.getvalue()
        self.assertIn("Recommended Recovery", rendered)
        self.assertIn("command: open '/Applications/Example Installer.app'", rendered)
        self.assertIn("manual: Finish the installer UI.", rendered)

    def test_actionable_summary_lists_only_apply_subset(self) -> None:
        actionable = (
            StepCheck(
                "example.change",
                "Example Change",
                StepStatus.NEEDS_CHANGE,
                "will update example",
                frozenset({"files", "test"}),
                frozenset({Risk.USER_FILE}),
            ),
            StepCheck(
                "example.unknown",
                "Example Unknown",
                StepStatus.UNKNOWN,
                "needs runtime check",
                frozenset({"packages"}),
                frozenset({Risk.NETWORK}),
            ),
        )

        rendered = format_actionable_checks(actionable, total_count=5)

        self.assertIn("Actionable Changes To Apply (2 of 5 checked)", rendered)
        self.assertIn("CHANGE example.change: Example Change", rendered)
        self.assertIn("UNKNOWN example.unknown: Example Unknown", rendered)
        self.assertIn("detail: will update example", rendered)
        self.assertIn("tags: files,test", rendered)
        self.assertIn("risks: user-file", rendered)

    def test_apply_prints_actionable_summary_before_confirmation_prompt(self) -> None:
        output = io.StringIO()
        with (
            patch(
                "macsetup.cli.build_steps",
                return_value=(ActionableLookingStep(), SlowLookingStep()),
            ),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="n"),
            redirect_stdout(output),
        ):
            code = main(("apply", "--tags", "test"))

        rendered = output.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("Actionable Changes To Apply (1 of 2 checked)", rendered)
        self.assertIn("CHANGE example.action: Example Action", rendered)
        self.assertIn("OK       example.step", rendered)
        self.assertLess(
            rendered.index("Actionable Changes To Apply"),
            rendered.index("Apply cancelled."),
        )

    def test_apply_allows_bootstrap_by_default(self) -> None:
        output = io.StringIO()
        with (
            patch("macsetup.cli.build_steps", return_value=(BootstrapAwareStep(),)),
            patch(
                "macsetup.cli.write_journal",
                return_value=Path("/tmp/macsetup-test-journal.json"),
            ),
            redirect_stdout(output),
        ):
            code = main(("apply", "--tags", "test", "--yes"))

        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("CHANGE example.bootstrap: Example Bootstrap", rendered)
        self.assertIn("APPLIED  example.bootstrap", rendered)

    def test_apply_no_bootstrap_reports_blocked_steps(self) -> None:
        output = io.StringIO()
        with (
            patch("macsetup.cli.build_steps", return_value=(BootstrapAwareStep(),)),
            redirect_stdout(output),
        ):
            code = main(("apply", "--tags", "test", "--no-bootstrap", "--yes"))

        rendered = output.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("No actionable changes.", rendered)
        self.assertIn("Blocked Steps", rendered)
        self.assertIn("BLOCKED  example.bootstrap", rendered)

    def test_apply_validates_sudo_before_privileged_actions(self) -> None:
        output = io.StringIO()
        with (
            patch(
                "macsetup.cli.build_steps",
                return_value=(PrivilegedActionableLookingStep(),),
            ),
            patch.object(
                CommandRunner,
                "sudo_validate",
                return_value=CommandResult(command="sudo -v", returncode=0),
            ) as sudo_validate,
            patch(
                "macsetup.cli.write_journal",
                return_value=Path("/tmp/macsetup-test-journal.json"),
            ),
            redirect_stdout(output),
        ):
            code = main(("apply", "--tags", "test", "--allow-privileged", "--yes"))

        self.assertEqual(code, 0)
        sudo_validate.assert_called_once()
        self.assertIn("Actionable Changes To Apply (1 of 1 checked)", output.getvalue())

    def test_sudo_touch_id_blocked_check_reports_copy_paste_apply_command(self) -> None:
        with patch(
            "macsetup.steps._sudo_touch_id_probe",
            return_value=SudoTouchIdProbe(
                StepStatus.NEEDS_CHANGE,
                "will create /etc/pam.d/sudo_local",
                "auth sufficient pam_tid.so\n",
            ),
        ):
            check = SudoTouchIdStep().check(DummyContext())

        rendered = format_remediations((check,))
        self.assertEqual(check.status, StepStatus.BLOCKED)
        self.assertIn("requires privileged apply", check.detail)
        self.assertIn(
            "uv run macsetup apply --tags sudo --allow-privileged --yes", rendered
        )

    def test_confirm_apply_ctrl_c_is_cleanly_handled_by_main(self) -> None:
        with patch("macsetup.cli._main", side_effect=KeyboardInterrupt):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(("apply",))

        self.assertEqual(code, 130)
        self.assertEqual(output.getvalue(), "\nCancelled.\n")

    def test_confirm_apply_ctrl_c_propagates_to_main_handler(self) -> None:
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                confirm_apply(actionable_count=1)

    def test_apply_ctrl_c_writes_interrupted_partial_journal(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            output = io.StringIO()
            with (
                patch(
                    "macsetup.cli.build_steps",
                    return_value=(ActionableLookingStep(), InterruptingApplyStep()),
                ),
                patch("macsetup.cli.Path.home", return_value=home),
                redirect_stdout(output),
            ):
                code = main(("apply", "--tags", "test", "--yes"))

            journals = sorted((home / ".macsetup" / "runs").glob("*.json"))
            self.assertEqual(len(journals), 1)
            payload = json.loads(journals[0].read_text(encoding="utf-8"))

        self.assertEqual(code, 130)
        self.assertIn("Interrupted. Journal:", output.getvalue())
        self.assertEqual(payload["operation"], "apply")
        self.assertEqual(payload["status"], "interrupted")
        self.assertEqual(payload["interrupted_step_id"], "example.interrupt")
        self.assertEqual(
            [result["step_id"] for result in payload["results"]],
            ["example.action", "example.interrupt"],
        )
        self.assertEqual(payload["results"][0]["status"], "applied")
        self.assertEqual(payload["results"][1]["status"], "failed")
        self.assertIn("interrupted during apply", payload["results"][1]["detail"])
        self.assertEqual(
            payload["results"][1]["commands"][0]["command"], "brew install demo"
        )

    def test_prepare_state_repairs_root_owned_state_when_privileged(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            state_root = home / ".macsetup"
            runner = RecordingRunner()
            context = DummyContext(
                home=home,
                backup_root=state_root / "backups",
                runner=runner,  # type: ignore[arg-type]
                dry_run=False,
                allow_privileged=True,
            )
            context.state_root = state_root  # type: ignore[attr-defined]
            context.state_owner = UserIdentity(501, 20, home, "matt")  # type: ignore[attr-defined]

            with patch(
                "macsetup.cli.prepare_user_state_root",
                side_effect=(PermissionError("denied"), None),
            ):
                prepared = prepare_state_for_mutation(context)

        self.assertTrue(prepared)
        self.assertEqual(len(runner.commands), 7)
        self.assertEqual(
            runner.commands[0].argv,
            (
                "sudo",
                "install",
                "-d",
                "-o",
                "501",
                "-g",
                "20",
                "-m",
                "700",
                str(state_root),
            ),
        )
        self.assertEqual(runner.commands[3].argv[:3], ("sudo", "find", str(state_root)))


if __name__ == "__main__":
    unittest.main()
