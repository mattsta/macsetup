import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from macsetup.mrsync import build_plan, format_audit, parse_args


class MrsyncTests(unittest.TestCase):
    def test_build_plan_merges_global_local_and_project_filters(self) -> None:
        with TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "global.filter").write_text(
                "- node_modules/\n", encoding="utf-8"
            )
            (config_dir / "local.filter").write_text(
                "- large-cache/\n", encoding="utf-8"
            )

            options = parse_args(
                ("--config-dir", str(config_dir), "-a", "src/", "dest/")
            )
            plan = build_plan(options)

        self.assertEqual(
            plan.command,
            (
                "rsync",
                f"--filter=merge {config_dir / 'global.filter'}",
                f"--filter=merge {config_dir / 'local.filter'}",
                "-F",
                "-F",
                "-a",
                "src/",
                "dest/",
            ),
        )

    def test_build_plan_can_disable_managed_filters(self) -> None:
        options = parse_args(
            ("--no-global-filters", "--no-project-filters", "-a", "src/", "dest/")
        )
        plan = build_plan(options)
        self.assertEqual(plan.command, ("rsync", "-a", "src/", "dest/"))

    def test_audit_shows_filter_source_lines_and_command(self) -> None:
        with TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "global.filter").write_text(
                "# comment\n- .venv/\n", encoding="utf-8"
            )
            options = parse_args(
                ("--config-dir", str(config_dir), "--audit", "-a", "src/", "dest/")
            )
            plan = build_plan(options)
            audit = format_audit(options, plan)

        self.assertIn("managed global", audit)
        self.assertIn("2: - .venv/", audit)
        self.assertIn("local.filter (missing", audit)
        self.assertIn("Final command:", audit)

    def test_explicit_filter_file_is_required(self) -> None:
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.filter"
            options = parse_args(("--filter-file", str(missing), "-a", "src/", "dest/"))
            self.assertTrue(
                any(
                    source.required and not source.exists
                    for source in build_plan(options).filter_sources
                )
            )


if __name__ == "__main__":
    unittest.main()
