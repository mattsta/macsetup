import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from macsetup.cli import build_parser
from macsetup.model import StepStatus
from macsetup.recipes import BrewPackage
from macsetup.runner import CommandRunner, LocalContext
from macsetup.steps import BrewPackagesStep, ManagedBlockStep
from macsetup.unapply import (
    JournalResult,
    build_unapply_plan,
    load_run_journal,
    unapply_step,
)


def context_for(
    home: Path,
    *,
    dry_run: bool = True,
    prefix: Path | None = None,
    allow_privileged: bool = False,
) -> LocalContext:
    class TestContext(LocalContext):
        def command_exists(self, name: str) -> bool:
            return name == "brew" or super().command_exists(name)

        def brew_prefix(self) -> Path | None:
            return prefix if prefix is not None else super().brew_prefix()

    return TestContext(
        home=home,
        repo_root=Path("/example/macsetup"),
        backup_root=home / ".macsetup" / "backups",
        runner=CommandRunner(dry_run=dry_run),
        dry_run=dry_run,
        allow_privileged=allow_privileged,
    )


def write_formula(prefix: Path, name: str, source: str) -> None:
    formula_dir = (
        prefix
        / "Library"
        / "Taps"
        / "homebrew"
        / "homebrew-core"
        / "Formula"
        / name[0].lower()
    )
    formula_dir.mkdir(parents=True, exist_ok=True)
    (formula_dir / f"{name}.rb").write_text(source, encoding="utf-8")


def write_receipt(prefix: Path, name: str, dependencies: tuple[str, ...]) -> None:
    receipt = prefix / "Cellar" / name / "1.0" / "INSTALL_RECEIPT.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    deps = ", ".join(f'{{"full_name": "{dependency}"}}' for dependency in dependencies)
    receipt.write_text(f'{{"runtime_dependencies": [{deps}]}}', encoding="utf-8")


class UnapplyTests(unittest.TestCase):
    def test_parser_accepts_unapply_modes(self) -> None:
        parser = build_parser()

        self.assertTrue(parser.parse_args(("unapply", "--force", "--dry-run")).force)
        self.assertEqual(
            parser.parse_args(
                ("unapply", "--from-run", "latest", "--dry-run")
            ).from_run,
            "latest",
        )

    def test_managed_block_unapply_removes_marker(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".zshrc"
            step = ManagedBlockStep(
                "shell.example", "Example", "~/.zshrc", "example", "export EDITOR=nvim"
            )
            target.write_text(step._change(context_for(home)).after, encoding="utf-8")
            context = context_for(home, dry_run=False)

            result = unapply_step(
                step,
                context,
                force=False,
                journal_result=JournalResult(
                    step.id, step.title, "applied", "", (), ()
                ),
            )

            self.assertEqual(result.status, StepStatus.APPLIED)
            self.assertNotIn("macsetup:example", target.read_text(encoding="utf-8"))

    def test_from_run_brew_unapply_uses_recorded_install_command(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            prefix = Path(directory) / "prefix"
            (prefix / "Cellar" / "alpha").mkdir(parents=True)
            context = context_for(home, prefix=prefix)
            step = BrewPackagesStep(
                id="brew.formulas.test",
                title="Install test formulas",
                packages=(BrewPackage("alpha"), BrewPackage("beta")),
            )
            journal_result = JournalResult(
                step_id=step.id,
                title=step.title,
                status="applied",
                detail="",
                commands=(
                    {
                        "command": "brew install alpha",
                        "returncode": 0,
                        "skipped": False,
                    },
                ),
                changes=(),
            )

            with redirect_stdout(io.StringIO()):
                result = unapply_step(
                    step, context, force=False, journal_result=journal_result
                )

            self.assertEqual(result.status, StepStatus.APPLIED)
            self.assertEqual(result.commands[0].command, "brew uninstall alpha")
            self.assertTrue(result.commands[0].skipped)
            self.assertNotIn("beta", result.detail)

    def test_brew_formula_unapply_orders_dependents_before_dependencies(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            prefix = Path(directory) / "prefix"
            (prefix / "Cellar" / "bash").mkdir(parents=True)
            (prefix / "Cellar" / "bash-completion@2").mkdir(parents=True)
            write_formula(prefix, "bash", "")
            write_formula(prefix, "bash-completion@2", 'depends_on "bash"\n')
            context = context_for(home, prefix=prefix)
            step = BrewPackagesStep(
                id="brew.formulas.test",
                title="Install test formulas",
                packages=(BrewPackage("bash"), BrewPackage("bash-completion@2")),
            )

            with redirect_stdout(io.StringIO()):
                result = unapply_step(step, context, force=True, journal_result=None)

            self.assertEqual(result.status, StepStatus.APPLIED)
            self.assertEqual(
                [command.command for command in result.commands],
                ["brew uninstall bash-completion@2", "brew uninstall bash"],
            )

    def test_brew_formula_unapply_blocks_external_installed_dependents(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            prefix = Path(directory) / "prefix"
            (prefix / "Cellar" / "bash").mkdir(parents=True)
            (prefix / "Cellar" / "bash-completion@2").mkdir(parents=True)
            write_receipt(prefix, "bash-completion@2", ("bash",))
            context = context_for(home, prefix=prefix)
            step = BrewPackagesStep(
                id="brew.formulas.test",
                title="Install test formulas",
                packages=(BrewPackage("bash"),),
            )

            with redirect_stdout(io.StringIO()):
                result = unapply_step(step, context, force=True, journal_result=None)

            self.assertEqual(result.status, StepStatus.BLOCKED)
            self.assertIn("bash required by bash-completion@2", result.detail)
            self.assertEqual(result.commands, ())

    def test_unapply_plan_consolidates_formula_steps_for_global_dependency_order(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            prefix = Path(directory) / "prefix"
            write_formula(prefix, "bash", "")
            write_formula(prefix, "bash-completion@2", 'depends_on "bash"\n')
            context = context_for(home, prefix=prefix)
            bash_step = BrewPackagesStep(
                id="brew.formulas.shell-a",
                title="Install bash",
                packages=(BrewPackage("bash", tags=("packages", "shell")),),
                tags=frozenset({"packages", "shell"}),
            )
            completion_step = BrewPackagesStep(
                id="brew.formulas.shell-b",
                title="Install bash completion",
                packages=(
                    BrewPackage("bash-completion@2", tags=("packages", "shell")),
                ),
                tags=frozenset({"packages", "shell"}),
            )

            plan = build_unapply_plan(
                (bash_step, completion_step), context, force=True, journal=None
            )

            self.assertEqual(len(plan), 1)
            self.assertEqual(plan[0].step.id, "brew.formulas.selected")
            self.assertEqual(
                [package.name for package in plan[0].step.packages],  # type: ignore[attr-defined]
                ["bash-completion@2", "bash"],
            )

    def test_real_cask_unapply_requires_privileged_flag(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            prefix = Path(directory) / "prefix"
            (prefix / "Caskroom" / "windscribe").mkdir(parents=True)
            context = context_for(home, dry_run=False, prefix=prefix)
            step = BrewPackagesStep(
                id="brew.casks.test",
                title="Install test casks",
                packages=(BrewPackage("windscribe"),),
                cask=True,
            )

            with redirect_stdout(io.StringIO()):
                result = unapply_step(step, context, force=True, journal_result=None)

            self.assertEqual(result.status, StepStatus.BLOCKED)
            self.assertIn("--allow-privileged", result.detail)
            self.assertEqual(result.commands, ())

    def test_dry_run_cask_unapply_does_not_require_privileged_flag(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            prefix = Path(directory) / "prefix"
            (prefix / "Caskroom" / "windscribe").mkdir(parents=True)
            context = context_for(home, prefix=prefix)
            step = BrewPackagesStep(
                id="brew.casks.test",
                title="Install test casks",
                packages=(BrewPackage("windscribe"),),
                cask=True,
            )

            with redirect_stdout(io.StringIO()):
                result = unapply_step(step, context, force=True, journal_result=None)

            self.assertEqual(result.status, StepStatus.APPLIED)
            self.assertEqual(
                result.commands[0].command, "brew uninstall --cask --force windscribe"
            )
            self.assertTrue(result.commands[0].skipped)

    def test_load_run_journal_reads_changes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "runs" / "example.json"
            run.parent.mkdir()
            run.write_text(
                json.dumps(
                    {
                        "created_at": "20260531T000000Z",
                        "operation": "apply",
                        "results": [
                            {
                                "step_id": "git.config.core.editor",
                                "title": "Set git core.editor",
                                "status": "applied",
                                "detail": "configured",
                                "commands": [],
                                "changes": [
                                    {
                                        "type": "git_config",
                                        "key": "core.editor",
                                        "had_value": False,
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            journal = load_run_journal(root, "example.json")

        self.assertEqual(
            journal.applied_step_ids, frozenset({"git.config.core.editor"})
        )
        journal_result = journal.result_for("git.config.core.editor")
        self.assertIsNotNone(journal_result)
        assert journal_result is not None
        self.assertEqual(journal_result.changes[0]["type"], "git_config")


if __name__ == "__main__":
    unittest.main()
