import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from macsetup.cli import build_parser
from macsetup.preview import build_previews, format_previews
from macsetup.runner import CommandRunner, LocalContext
from macsetup.steps import ManagedBlockStep, MrsyncLauncherStep


def context_for(home: Path) -> LocalContext:
    return LocalContext(
        home=home,
        repo_root=Path("/example/macsetup"),
        backup_root=home / ".macsetup" / "backups",
        runner=CommandRunner(dry_run=True),
        dry_run=True,
    )


class PreviewTests(unittest.TestCase):
    def test_managed_block_preview_shows_materialized_diff(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".zshrc"
            target.write_text("export EDITOR=vim\n", encoding="utf-8")
            step = ManagedBlockStep(
                "shell.example",
                "Example shell block",
                "~/.zshrc",
                "example",
                "export EDITOR=nvim",
            )
            context = context_for(home)

            check = step.check(context)
            rendered = format_previews(build_previews((step,), (check,), context))

        self.assertIn("shell.example: Example shell block", rendered)
        self.assertIn("+++ ", rendered)
        self.assertIn("+export EDITOR=nvim", rendered)
        self.assertIn("macsetup:example", rendered)

    def test_launcher_preview_shows_mode_change(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".local" / "bin" / "mrsync"
            target.parent.mkdir(parents=True)
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(target, 0o644)
            step = MrsyncLauncherStep()
            context = context_for(home)

            check = step.check(context)
            rendered = format_previews(build_previews((step,), (check,), context))

        self.assertIn("mode", rendered)
        self.assertIn("0644 -> 0755", rendered)

    def test_launcher_uses_uv_without_pythonpath(self) -> None:
        with TemporaryDirectory() as directory:
            body = MrsyncLauncherStep()._body(context_for(Path(directory)))

        self.assertIn('uv --directory "$repo_root" run mrsync "$@"', body)
        self.assertNotIn("PYTHONPATH", body)
        self.assertNotIn("python3 -m", body)

    def test_cli_accepts_preview_and_diff_options(self) -> None:
        parser = build_parser()
        self.assertEqual(
            parser.parse_args(("preview", "--tags", "sync")).command, "preview"
        )
        self.assertTrue(parser.parse_args(("plan", "--diff")).diff)
        self.assertTrue(parser.parse_args(("apply", "--dry-run", "--diff")).diff)


if __name__ == "__main__":
    unittest.main()
