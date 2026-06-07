import unittest
from dataclasses import dataclass, field
from pathlib import Path

from macsetup.model import CommandResult, CommandSpec, StepStatus
from macsetup.recipes import GitSetting
from macsetup.runner import LocalContext, display_command
from macsetup.steps import GitConfigStep
from macsetup.unapply import JournalResult, unapply_step


@dataclass
class GitConfigRunner:
    values: dict[str, list[str]] = field(default_factory=dict)
    commands: list[tuple[str, ...]] = field(default_factory=list)
    dry_run: bool = False

    def which(self, name: str) -> str | None:
        return name if name == "git" else None

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
        if argv[:4] == ("git", "config", "--global", "--get-all"):
            key = argv[4]
            if key not in self.values:
                return CommandResult(rendered, 1)
            return CommandResult(rendered, 0, stdout="\n".join(self.values[key]) + "\n")
        if argv[:4] == ("git", "config", "--global", "--replace-all"):
            self.values[argv[4]] = [argv[5]]
            return CommandResult(rendered, 0)
        if argv[:4] == ("git", "config", "--global", "--unset-all"):
            self.values.pop(argv[4], None)
            return CommandResult(rendered, 0)
        if argv[:4] == ("git", "config", "--global", "--add"):
            self.values.setdefault(argv[4], []).append(argv[5])
            return CommandResult(rendered, 0)
        return CommandResult(rendered, 127, stderr="unexpected git command")

    def state(self, state: str, detail: str) -> None:
        pass

    def sudo_validate(self, *, timeout_seconds: float = 120.0) -> CommandResult:
        return CommandResult("sudo -v", 0)


def context_for(runner: GitConfigRunner) -> LocalContext:
    return LocalContext(
        home=Path("/example/home"),
        repo_root=Path("/example/macsetup"),
        backup_root=Path("/example/home/.macsetup/backups"),
        runner=runner,  # type: ignore[arg-type]
        dry_run=runner.dry_run,
    )


class GitConfigStepTests(unittest.TestCase):
    def test_check_treats_duplicate_values_as_needing_change(self) -> None:
        runner = GitConfigRunner(values={"pager.diff": ["delta", "delta"]})
        step = GitConfigStep(GitSetting("pager.diff", "delta"))

        check = step.check(context_for(runner))

        self.assertEqual(check.status, StepStatus.NEEDS_CHANGE)
        self.assertIn("current value(s): delta, delta", check.detail)

    def test_apply_replaces_all_existing_values_with_one_desired_value(self) -> None:
        runner = GitConfigRunner(values={"pager.diff": ["cat", "delta"]})
        step = GitConfigStep(GitSetting("pager.diff", "delta"))

        result = step.apply(context_for(runner))

        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertEqual(runner.values["pager.diff"], ["delta"])
        self.assertIn(
            ("git", "config", "--global", "--replace-all", "pager.diff", "delta"),
            runner.commands,
        )
        self.assertEqual(result.changes[0]["previous_values"], ["cat", "delta"])

    def test_apply_noops_when_exactly_one_desired_value_exists(self) -> None:
        runner = GitConfigRunner(values={"pager.diff": ["delta"]})
        step = GitConfigStep(GitSetting("pager.diff", "delta"))

        result = step.apply(context_for(runner))

        self.assertEqual(result.status, StepStatus.PRESENT)
        self.assertNotIn(
            ("git", "config", "--global", "--replace-all", "pager.diff", "delta"),
            runner.commands,
        )

    def test_apply_expands_user_scoped_global_ignore_path(self) -> None:
        runner = GitConfigRunner()
        step = GitConfigStep(GitSetting("core.excludesfile", "~/.gitignore_global"))

        result = step.apply(context_for(runner))

        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertEqual(
            runner.values["core.excludesfile"],
            ["/example/home/.gitignore_global"],
        )

    def test_unapply_restores_all_recorded_previous_values(self) -> None:
        runner = GitConfigRunner(values={"pager.diff": ["delta"]})
        step = GitConfigStep(GitSetting("pager.diff", "delta"))

        result = unapply_step(
            step,
            context_for(runner),
            force=False,
            journal_result=JournalResult(
                step.id,
                step.title,
                "applied",
                "",
                (),
                (
                    {
                        "type": "git_config",
                        "key": "pager.diff",
                        "previous": "cat",
                        "previous_values": ["cat", "less"],
                        "had_value": True,
                        "after": "delta",
                    },
                ),
            ),
        )

        self.assertEqual(result.status, StepStatus.APPLIED)
        self.assertEqual(runner.values["pager.diff"], ["cat", "less"])
        self.assertEqual(
            runner.commands[-3:],
            [
                ("git", "config", "--global", "--unset-all", "pager.diff"),
                ("git", "config", "--global", "--add", "pager.diff", "cat"),
                ("git", "config", "--global", "--add", "pager.diff", "less"),
            ],
        )

    def test_pager_steps_require_their_rendering_tools(self) -> None:
        log_step = GitConfigStep(
            GitSetting("pager.log", "$HOME/.local/bin/git-log-pager")
        )
        core_step = GitConfigStep(GitSetting("core.pager", "delta"))
        interactive_step = GitConfigStep(
            GitSetting("interactive.diffFilter", "delta --color-only")
        )
        diff_step = GitConfigStep(GitSetting("pager.diff", "delta"))

        log_requires = {str(item) for item in log_step.requires}
        self.assertIn("brew-formula:bat", log_requires)
        self.assertIn("brew-formula:git-delta", log_requires)
        # pager.log references the compiled dispatcher; it must build first.
        self.assertIn("file:~/.local/bin/git-log-pager", log_requires)
        self.assertIn(
            "brew-formula:git-delta", {str(item) for item in core_step.requires}
        )
        self.assertIn(
            "brew-formula:git-delta",
            {str(item) for item in interactive_step.requires},
        )
        self.assertIn(
            "brew-formula:git-delta",
            {str(item) for item in diff_step.requires},
        )


if __name__ == "__main__":
    unittest.main()
