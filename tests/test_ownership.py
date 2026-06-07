import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from macsetup.ownership import (
    UserIdentity,
    invoking_user_identity,
    prepare_user_state_root,
)
from macsetup.reporting import write_journal


class FakePasswd:
    pw_name = "matt"
    pw_dir = "/Users/matt"


class OwnershipTests(unittest.TestCase):
    def test_invoking_user_identity_uses_sudo_user_when_running_as_root(self) -> None:
        with (
            patch("macsetup.ownership.os.geteuid", return_value=0),
            patch("macsetup.ownership.pwd.getpwnam", return_value=FakePasswd()),
        ):
            identity = invoking_user_identity(
                {
                    "SUDO_UID": "501",
                    "SUDO_GID": "20",
                    "SUDO_USER": "matt",
                }
            )

        self.assertEqual(identity.uid, 501)
        self.assertEqual(identity.gid, 20)
        self.assertEqual(identity.name, "matt")
        self.assertEqual(identity.home, Path("/Users/matt"))

    def test_prepare_user_state_root_creates_private_state_directories(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            owner = UserIdentity(os.getuid(), os.getgid(), home, "test")
            state_root = home / ".macsetup"

            prepare_user_state_root(state_root, owner, "backups")

            self.assertTrue((state_root / "backups").is_dir())
            self.assertEqual(stat.S_IMODE(state_root.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((state_root / "backups").stat().st_mode), 0o700
            )

    def test_write_journal_creates_private_manifest(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            owner = UserIdentity(os.getuid(), os.getgid(), home, "test")
            state_root = home / ".macsetup"

            journal = write_journal(state_root, checks=[], results=[], owner=owner)

            self.assertEqual(stat.S_IMODE((state_root / "runs").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
