from __future__ import annotations

import os
import pwd
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UserIdentity:
    uid: int
    gid: int
    home: Path
    name: str | None = None


def invoking_user_identity(
    environ: Mapping[str, str] | None = None,
) -> UserIdentity:
    env = os.environ if environ is None else environ
    if os.geteuid() == 0:
        sudo_uid = env.get("SUDO_UID", "")
        sudo_gid = env.get("SUDO_GID", "")
        if sudo_uid.isdecimal() and sudo_gid.isdecimal():
            uid = int(sudo_uid)
            gid = int(sudo_gid)
            record = _passwd_for_sudo_user(env.get("SUDO_USER"), uid)
            home = Path(record.pw_dir) if record is not None else Path.home()
            name = record.pw_name if record is not None else env.get("SUDO_USER")
            return UserIdentity(uid=uid, gid=gid, home=home, name=name)

    uid = os.getuid()
    gid = os.getgid()
    record = _getpwuid(uid)
    name = record.pw_name if record is not None else None
    return UserIdentity(uid=uid, gid=gid, home=Path.home(), name=name)


def context_state_root(context: object) -> Path:
    state_root = getattr(context, "state_root", None)
    if isinstance(state_root, Path):
        return state_root
    home = getattr(context, "home")
    return home / ".macsetup"


def context_state_owner(context: object) -> UserIdentity | None:
    owner = getattr(context, "state_owner", None)
    return owner if isinstance(owner, UserIdentity) else None


def prepare_user_state_root(
    state_root: Path,
    owner: UserIdentity | None,
    *relative_dirs: str,
) -> None:
    ensure_private_dir(state_root, owner)
    for relative_dir in relative_dirs:
        ensure_private_dir(state_root / relative_dir, owner)


def prepare_backup_root(
    backup_root: Path,
    owner: UserIdentity | None,
) -> None:
    state_root = backup_state_root(backup_root)
    if state_root is not None:
        prepare_user_state_root(state_root, owner, "backups")
        return
    ensure_private_dir(backup_root, owner)


def repair_backup_root(
    backup_root: Path,
    owner: UserIdentity | None,
) -> None:
    state_root = backup_state_root(backup_root)
    if state_root is not None:
        repair_user_state_tree(state_root, owner)
    elif backup_root.exists():
        repair_user_state_tree(backup_root, owner)


def backup_state_root(backup_root: Path) -> Path | None:
    if backup_root.name == "backups" and backup_root.parent.name == ".macsetup":
        return backup_root.parent
    return None


def ensure_private_dir(path: Path, owner: UserIdentity | None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, 0o700)
    _chown(path, owner)


def ensure_private_file(path: Path, owner: UserIdentity | None) -> None:
    _chmod(path, 0o600)
    _chown(path, owner)


def repair_user_state_tree(state_root: Path, owner: UserIdentity | None) -> None:
    if not state_root.exists():
        return
    for root, dirs, files in os.walk(state_root, followlinks=False):
        root_path = Path(root)
        _chmod(root_path, 0o700)
        _chown(root_path, owner)
        for directory in dirs:
            path = root_path / directory
            if path.is_symlink():
                continue
            _chmod(path, 0o700)
            _chown(path, owner)
        for filename in files:
            path = root_path / filename
            if path.is_symlink():
                continue
            _chown(path, owner)


def _passwd_for_sudo_user(
    sudo_user: str | None,
    uid: int,
) -> pwd.struct_passwd | None:
    if sudo_user:
        try:
            return pwd.getpwnam(sudo_user)
        except KeyError:
            pass
    return _getpwuid(uid)


def _getpwuid(uid: int) -> pwd.struct_passwd | None:
    try:
        return pwd.getpwuid(uid)
    except KeyError:
        return None


def _chmod(path: Path, mode: int) -> None:
    if path.is_symlink():
        return
    os.chmod(path, mode)


def _chown(path: Path, owner: UserIdentity | None) -> None:
    if owner is None or os.geteuid() != 0 or path.is_symlink():
        return
    os.chown(path, owner.uid, owner.gid)
