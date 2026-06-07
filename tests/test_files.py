import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from macsetup.files import (
    atomic_write,
    backup_file,
    current_mode,
    managed_block,
    remove_block,
    upsert_block,
)
from macsetup.ownership import UserIdentity


class ManagedBlockTests(unittest.TestCase):
    def test_upsert_block_adds_marker_once(self) -> None:
        first = upsert_block("", name="shell", content="export EDITOR=nvim")
        second = upsert_block(first, name="shell", content="export EDITOR=nvim")
        self.assertEqual(first, second)
        self.assertEqual(first.count("macsetup:shell"), 2)
        self.assertFalse(first.startswith("\n"))

    def test_upsert_block_replaces_existing_content(self) -> None:
        original = managed_block("shell", "export EDITOR=vim")
        updated = upsert_block(original, name="shell", content="export EDITOR=nvim")
        self.assertIn("EDITOR=nvim", updated)
        self.assertNotIn("EDITOR=vim", updated)

    def test_upsert_lua_comment_marker(self) -> None:
        updated = upsert_block(
            "", name="nvim", content='require("macsetup")', comment_prefix="--"
        )
        self.assertIn("-- >>> macsetup:nvim >>>", updated)

    def test_upsert_block_can_adopt_existing_line_chunks(self) -> None:
        existing = "export EDITOR=nvim\nalias vim=nvim\n"
        content = "\n".join(
            (
                "export EDITOR=nvim",
                "export VISUAL=nvim",
                "",
                "alias vim=nvim",
                "alias ip=ipython",
            )
        )

        updated = upsert_block(
            existing,
            name="zshrc",
            content=content,
            adopt_existing_chunks=True,
        )

        self.assertEqual(updated.count("export EDITOR=nvim"), 1)
        self.assertEqual(updated.count("alias vim=nvim"), 1)
        self.assertIn("export VISUAL=nvim", updated)
        self.assertIn("alias ip=ipython", updated)
        self.assertIn("macsetup:zshrc", updated)

    def test_upsert_block_adoption_is_noop_when_every_chunk_exists(self) -> None:
        existing = "export EDITOR=nvim\nalias vim=nvim\n"

        updated = upsert_block(
            existing,
            name="zshrc",
            content=existing,
            adopt_existing_chunks=True,
        )

        self.assertEqual(updated, existing)

    def test_upsert_block_adoption_updates_marker_with_only_missing_chunks(
        self,
    ) -> None:
        existing = "alias vim=nvim\n\n" + managed_block(
            "zshrc", "alias vim=nvim\nalias old=old"
        )

        updated = upsert_block(
            existing,
            name="zshrc",
            content="alias vim=nvim\nalias ip=ipython",
            adopt_existing_chunks=True,
        )

        self.assertEqual(updated.count("alias vim=nvim"), 1)
        self.assertIn("alias ip=ipython", updated)
        self.assertNotIn("alias old=old", updated)

    def test_upsert_block_adoption_hints_skip_unmarked_equivalent_config(
        self,
    ) -> None:
        existing = 'eval "$(/opt/homebrew/bin/brew shellenv)"\n'
        content = "\n".join(
            (
                "if [[ -x /opt/homebrew/bin/brew ]]; then",
                '  eval "$(/opt/homebrew/bin/brew shellenv)"',
                "fi",
            )
        )

        updated = upsert_block(
            existing,
            name="zprofile-homebrew",
            content=content,
            adopt_existing_chunks=True,
            adopt_existing_patterns=(r"\bbrew\s+shellenv\b",),
        )

        self.assertEqual(updated, existing)

    def test_upsert_block_adoption_hints_do_not_remove_existing_marker(self) -> None:
        existing = 'eval "$(/opt/homebrew/bin/brew shellenv)"\n\n' + managed_block(
            "zprofile-homebrew",
            "if [[ -x /opt/homebrew/bin/brew ]]; then\n"
            '  eval "$(/opt/homebrew/bin/brew shellenv)"\n'
            "fi",
        )
        content = (
            "if [[ -x /opt/homebrew/bin/brew ]]; then\n"
            '  eval "$(/opt/homebrew/bin/brew shellenv)"\n'
            "elif [[ -x /usr/local/bin/brew ]]; then\n"
            '  eval "$(/usr/local/bin/brew shellenv)"\n'
            "fi"
        )

        updated = upsert_block(
            existing,
            name="zprofile-homebrew",
            content=content,
            adopt_existing_chunks=True,
            adopt_existing_patterns=(r"\bbrew\s+shellenv\b",),
        )

        self.assertIn("macsetup:zprofile-homebrew", updated)
        self.assertIn("/usr/local/bin/brew", updated)

    def test_upsert_block_without_order_appends_to_end(self) -> None:
        existing = managed_block("b", "B")
        updated = upsert_block(existing, name="a", content="A")
        self.assertLess(
            updated.index("macsetup:b"),
            updated.index("macsetup:a"),
            "legacy behavior must append, not reorder",
        )

    def test_upsert_block_inserts_new_block_at_canonical_position(self) -> None:
        order = ("first", "middle", "last")
        existing = managed_block("first", "F") + managed_block("last", "L")
        updated = upsert_block(existing, name="middle", content="M", order=order)
        self.assertLess(
            updated.index("macsetup:first"), updated.index("macsetup:middle")
        )
        self.assertLess(
            updated.index("macsetup:middle"), updated.index("macsetup:last")
        )

    def test_upsert_block_self_heals_scrambled_order(self) -> None:
        order = ("a", "b", "c")
        scrambled = (
            "# user header\n\n"
            + managed_block("c", "C")
            + managed_block("a", "A")
            + managed_block("b", "B")
            + "\n# user footer\n"
        )
        # Re-applying any block re-sorts all managed blocks into canonical order.
        updated = upsert_block(scrambled, name="a", content="A", order=order)
        self.assertLess(updated.index("macsetup:a"), updated.index("macsetup:b"))
        self.assertLess(updated.index("macsetup:b"), updated.index("macsetup:c"))
        self.assertIn("# user header", updated)
        self.assertIn("# user footer", updated)

    def test_upsert_block_leaves_canonical_order_unchanged(self) -> None:
        order = ("a", "b")
        existing = managed_block("a", "A") + managed_block("b", "B")
        updated = upsert_block(existing, name="b", content="B", order=order)
        self.assertLess(updated.index("macsetup:a"), updated.index("macsetup:b"))
        self.assertEqual(updated.count("macsetup:a"), 2)  # start + end marker
        self.assertEqual(updated.count("macsetup:b"), 2)

    def test_remove_block_removes_only_managed_marker(self) -> None:
        existing = (
            "before\n\n" + managed_block("shell", "export EDITOR=nvim") + "\nafter\n"
        )
        updated = remove_block(existing, name="shell")

        self.assertEqual(updated, "before\n\nafter\n")

    def test_atomic_write_preserves_existing_mode_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "example"
            path.write_text("old\n", encoding="utf-8")
            os.chmod(path, 0o600)

            atomic_write(path, "new\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(current_mode(path), 0o600)

    def test_backup_file_creates_private_backup_root(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "example"
            source.write_text("old\n", encoding="utf-8")
            owner = UserIdentity(os.getuid(), os.getgid(), home, "test")
            backup_root = home / ".macsetup" / "backups"

            backup = backup_file(source, backup_root, owner)

            self.assertIsNotNone(backup)
            assert backup is not None
            self.assertEqual(backup.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(stat.S_IMODE(backup_root.stat().st_mode), 0o700)

    def test_backup_file_repairs_and_retries_permission_failure(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "example"
            source.write_text("old\n", encoding="utf-8")
            owner = UserIdentity(os.getuid(), os.getgid(), home, "test")
            backup_root = home / ".macsetup" / "backups"

            with (
                patch(
                    "macsetup.files._copy_backup",
                    side_effect=(PermissionError("denied"), None),
                ) as copy_backup,
                patch("macsetup.files.repair_backup_root") as repair_backup_root,
            ):
                backup = backup_file(source, backup_root, owner)

            self.assertIsNotNone(backup)
            self.assertEqual(copy_backup.call_count, 2)
            repair_backup_root.assert_called_once_with(backup_root, owner)


if __name__ == "__main__":
    unittest.main()
