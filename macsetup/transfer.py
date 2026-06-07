from __future__ import annotations

import argparse
import contextlib
import getpass
import ipaddress
import os
import secrets
import select
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory

from . import content

LAN_IPV4_NETWORKS = tuple(
    ipaddress.IPv4Network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
    )
)

DEFAULT_PORT = 17000
DEFAULT_RSYNC_DAEMON_PORT = 18730
DEFAULT_RSYNC_DAEMON_MODULE = "sync"
DEFAULT_RSYNC_DAEMON_USER = "macsetup"
RSYNC_PASSWORD_ENV = "MACSETUP_RSYNC_PASSWORD"
LOCAL_EXCLUDES = Path("local/rsync-excludes.txt")
WAIT_HEARTBEAT_SECONDS = 30
DEFAULT_GENERATED_EXCLUDES: tuple[str, ...] = ()
DEFAULT_RSYNC_EXCLUDES: tuple[str, ...] = ()


def parse_rsync_filter_excludes(text: str) -> tuple[str, ...]:
    patterns: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            pattern = line[2:].strip()
        elif line.startswith("-"):
            pattern = line[1:].strip()
        else:
            continue
        if pattern and pattern not in seen:
            patterns.append(pattern)
            seen.add(pattern)
    return tuple(patterns)


def tar_exclude_patterns(patterns: Sequence[str]) -> tuple[str, ...]:
    expanded: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        stripped = pattern.strip()
        if not stripped:
            continue
        directory_rule = stripped.endswith("/")
        candidates = [stripped]
        without_trailing_slash = stripped.rstrip("/")
        if without_trailing_slash and without_trailing_slash != stripped:
            candidates.append(without_trailing_slash)
        if without_trailing_slash and "/" not in without_trailing_slash:
            candidates.append(f"*/{without_trailing_slash}")
            if directory_rule:
                candidates.append(f"{without_trailing_slash}/*")
                candidates.append(f"*/{without_trailing_slash}/*")
        for candidate in candidates:
            if candidate not in seen:
                expanded.append(candidate)
                seen.add(candidate)
    return tuple(expanded)


DEFAULT_GENERATED_EXCLUDES = parse_rsync_filter_excludes(content.RSYNC_GLOBAL_FILTER)
DEFAULT_RSYNC_EXCLUDES = DEFAULT_GENERATED_EXCLUDES


class ArchiveKind(Enum):
    DITTO = "ditto"
    TAR = "tar"


class TransferMethod(Enum):
    AUTO = "auto"
    DITTO = "ditto"
    TAR = "tar"
    RSYNC = "rsync"
    RSYNC_DAEMON = "rsync-daemon"


class SyncCommand(Enum):
    AUTO_SERVER = "auto-server"
    AUTO_SYNC = "auto-sync"
    DITTO_RECV = "ditto-recv"
    DITTO_SEND = "ditto-send"
    TAR_RECV = "tar-recv"
    TAR_SEND = "tar-send"
    RSYNC_REPOS = "rsync-repos"
    RSYNC_FROM = "rsync-from"
    RSYNC_DAEMON_SERVER = "rsync-daemon-server"
    RSYNC_DAEMON_SYNC = "rsync-daemon-sync"


class TransferState(Enum):
    PRECHECK = "precheck"
    AUTH = "auth"
    LISTEN = "listen"
    WAIT = "wait"
    ACCEPT = "accept"
    PREPARE = "prepare"
    RUN = "run"
    DONE = "done"


@dataclass(frozen=True)
class InterfaceAddress:
    interface: str
    address: str
    network: str | None = None


@dataclass(frozen=True)
class ExcludeInputs:
    patterns: tuple[str, ...]
    files: tuple[str, ...]

    @property
    def present(self) -> bool:
        return bool(self.patterns or self.files)


@dataclass(frozen=True)
class AutoDecision:
    method: TransferMethod
    port: int | None
    excludes: ExcludeInputs


@dataclass(frozen=True)
class PipelinePlan:
    commands: tuple[tuple[str, ...], ...]
    cwd: Path | None = None


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


def run(args: argparse.Namespace) -> int:
    command = transfer_command(args)

    if command is SyncCommand.AUTO_SERVER:
        return auto_server(args)
    if command is SyncCommand.AUTO_SYNC:
        return auto_sync(args)
    if command is SyncCommand.RSYNC_DAEMON_SERVER:
        return rsync_daemon_server(args)
    if command is SyncCommand.RSYNC_DAEMON_SYNC:
        return rsync_daemon_sync(args)
    if command is SyncCommand.DITTO_RECV:
        return recv_archive(args, archive=ArchiveKind.DITTO)
    if command is SyncCommand.DITTO_SEND:
        return send_archive(args, archive=ArchiveKind.DITTO)
    if command is SyncCommand.TAR_RECV:
        return recv_archive(args, archive=ArchiveKind.TAR)
    if command is SyncCommand.TAR_SEND:
        return send_archive(args, archive=ArchiveKind.TAR)
    if command is SyncCommand.RSYNC_REPOS:
        return rsync_repos(args)
    if command is SyncCommand.RSYNC_FROM:
        return rsync_from(args)

    raise SystemExit(f"unknown sync command: {command.value}")


def transfer_command(args: argparse.Namespace) -> SyncCommand:
    for attr in ("sync_command", "command"):
        value = getattr(args, attr, None)
        if value and value != "sync":
            return SyncCommand(value)
    raise SystemExit("missing sync command")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="macsetup-transfer",
        description="LAN-only Mac-to-Mac copy helpers for bulk archive streams and rsync incrementals.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_transfer_command_parsers(subparsers)
    return parser


def add_sync_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    sync_parser = subparsers.add_parser(
        "sync", help="Multi-system sync and transfer support."
    )
    sync_subparsers = sync_parser.add_subparsers(dest="sync_command", required=True)
    add_transfer_command_parsers(sync_subparsers)
    return sync_parser


def add_transfer_command_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    add_auto_server_parser(
        subparsers.add_parser(
            "auto-server", help="receive either ditto or tar on separate LAN-only ports"
        )
    )
    add_auto_sync_parser(
        subparsers.add_parser(
            "auto-sync",
            help="choose ditto, tar, or rsync from the requested sync shape",
        )
    )
    add_recv_parser(
        subparsers.add_parser("ditto-recv", help="receive a ditto archive stream"),
        ArchiveKind.DITTO,
    )
    add_send_parser(
        subparsers.add_parser("ditto-send", help="send a ditto archive stream"),
        ArchiveKind.DITTO,
    )
    add_recv_parser(
        subparsers.add_parser("tar-recv", help="receive a tar archive stream"),
        ArchiveKind.TAR,
    )
    add_send_parser(
        subparsers.add_parser(
            "tar-send", help="send a tar archive stream with excludes"
        ),
        ArchiveKind.TAR,
    )
    add_rsync_parser(
        subparsers.add_parser("rsync-repos", help="run tuned rsync repo sync")
    )
    add_rsync_from_parser(
        subparsers.add_parser(
            "rsync-from", help="pull from an old/source machine over SSH"
        )
    )
    add_rsync_daemon_server_parser(
        subparsers.add_parser(
            "rsync-daemon-server", help="run a LAN-only foreground rsync daemon"
        )
    )
    add_rsync_daemon_sync_parser(
        subparsers.add_parser(
            "rsync-daemon-sync", help="sync to a managed rsync daemon over direct TCP"
        )
    )


def add_auto_server_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-p",
        "--port",
        type=valid_port,
        default=env_port(ArchiveKind.DITTO),
        help=f"ditto TCP port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--tar-port",
        type=valid_port,
        help="tar TCP port to listen on (default: --port + 1)",
    )
    parser.add_argument(
        "-b",
        "--bind",
        default=env_bind(ArchiveKind.DITTO),
        help="private LAN IPv4 address to bind; auto-detected by default",
    )
    parser.add_argument(
        "-d",
        "--dest",
        default=env_dest(ArchiveKind.DITTO),
        help="destination root for extraction (default: current directory)",
    )
    parser.add_argument(
        "--no-sudo",
        action="store_true",
        help="do not run mkdir/archive extraction through sudo",
    )
    parser.add_argument(
        "--no-mac-metadata",
        action="store_true",
        help="skip tar ACL/xattr/resource-fork metadata handling",
    )
    parser.add_argument(
        "--no-pv", action="store_true", help="do not use pv even when installed"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="accept one transfer and exit instead of staying in server mode",
    )
    parser.add_argument(
        "--allow-unsafe-network",
        action="store_true",
        help="permit non-LAN bind addresses such as 0.0.0.0",
    )


def add_auto_sync_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("positionals", nargs="*", metavar="HOST SOURCE")
    parser.add_argument(
        "-H",
        "--host",
        help="destination/new Mac hostname or IPv4 address for initial archive streams",
    )
    parser.add_argument("-s", "--source", help="source file or directory to send")
    parser.add_argument(
        "-p",
        "--port",
        type=valid_port,
        default=env_port(ArchiveKind.DITTO),
        help=f"ditto TCP port to connect to (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--tar-port",
        type=valid_port,
        help="tar TCP port to connect to (default: --port + 1)",
    )
    parser.add_argument(
        "--rsync-dest",
        help="destination path for catch-up rsync mode, often an SMB mount",
    )
    parser.add_argument(
        "--rsync-daemon-port",
        type=valid_port,
        default=DEFAULT_RSYNC_DAEMON_PORT,
        help=f"rsync daemon TCP port for --method rsync-daemon (default: {DEFAULT_RSYNC_DAEMON_PORT})",
    )
    parser.add_argument(
        "--rsync-module",
        default=DEFAULT_RSYNC_DAEMON_MODULE,
        help=f"rsync daemon module for --method rsync-daemon (default: {DEFAULT_RSYNC_DAEMON_MODULE})",
    )
    parser.add_argument(
        "--rsync-user",
        default=DEFAULT_RSYNC_DAEMON_USER,
        help=f"rsync daemon auth user for --method rsync-daemon (default: {DEFAULT_RSYNC_DAEMON_USER})",
    )
    parser.add_argument(
        "--rsync-password-file", help="file containing the rsync daemon password"
    )
    parser.add_argument(
        "--rsync-password-env",
        default=RSYNC_PASSWORD_ENV,
        help=f"environment variable containing the rsync daemon password (default: {RSYNC_PASSWORD_ENV})",
    )
    parser.add_argument(
        "--rsync-module-path",
        default="",
        help="optional path inside the rsync daemon module",
    )
    parser.add_argument(
        "--method",
        choices=[method.value for method in TransferMethod],
        default=TransferMethod.AUTO.value,
        help="transfer method override (default: auto)",
    )
    parser.add_argument(
        "--contents",
        action="store_true",
        help="copy the contents of SOURCE, not SOURCE itself",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="add one exclude pattern; in initial archive mode this selects tar",
    )
    parser.add_argument(
        "--exclude-from",
        action="append",
        default=[],
        help="read exclude patterns from FILE; in initial archive mode this selects tar",
    )
    parser.add_argument(
        "--local-excludes",
        default=str(LOCAL_EXCLUDES),
        help="gitignored local exclude file to use when present",
    )
    parser.add_argument(
        "--no-local-excludes",
        action="store_true",
        help="do not use the local exclude file",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="do not use managed generated-state excludes in tar or rsync mode",
    )
    parser.add_argument(
        "--delete",
        dest="delete",
        action="store_true",
        default=True,
        help="delete destination files missing from source in rsync mode",
    )
    parser.add_argument(
        "--no-delete",
        dest="delete",
        action="store_false",
        help="do not delete destination-only files in rsync mode",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show rsync changes without writing in rsync mode",
    )
    parser.add_argument(
        "--itemize",
        action="store_true",
        help="show rsync itemized changes in rsync mode",
    )
    parser.add_argument(
        "--no-mac-metadata",
        action="store_true",
        help="skip extra macOS metadata in tar or rsync mode",
    )
    parser.add_argument(
        "--no-progress", action="store_true", help="disable rsync progress output"
    )
    parser.add_argument(
        "--no-sudo",
        action="store_true",
        help="do not run archive creation through sudo; archive modes only",
    )
    parser.add_argument(
        "--no-pv",
        action="store_true",
        help="do not use pv even when installed; archive modes only",
    )
    parser.add_argument(
        "--estimate-size",
        action="store_true",
        help="run du before archive send so pv can show ETA; can be slow on huge trees",
    )
    parser.add_argument(
        "--allow-unsafe-network",
        action="store_true",
        help="permit non-LAN destination addresses",
    )


def add_recv_parser(parser: argparse.ArgumentParser, archive: ArchiveKind) -> None:
    parser.add_argument(
        "-p",
        "--port",
        type=valid_port,
        default=env_port(archive),
        help=f"TCP port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "-b",
        "--bind",
        default=env_bind(archive),
        help="private LAN IPv4 address to bind; auto-detected by default",
    )
    parser.add_argument(
        "-d",
        "--dest",
        default=env_dest(archive),
        help="destination root for extraction (default: current directory)",
    )
    parser.add_argument(
        "--no-sudo",
        action="store_true",
        help="do not run mkdir/archive extraction through sudo",
    )
    if archive is ArchiveKind.TAR:
        parser.add_argument(
            "--no-mac-metadata",
            action="store_true",
            help="skip tar ACL/xattr/resource-fork metadata handling",
        )
    parser.add_argument(
        "--no-pv", action="store_true", help="do not use pv even when installed"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="accept one transfer and exit instead of staying in server mode",
    )
    parser.add_argument(
        "--allow-unsafe-network",
        action="store_true",
        help="permit non-LAN bind addresses such as 0.0.0.0",
    )


def add_send_parser(parser: argparse.ArgumentParser, archive: ArchiveKind) -> None:
    parser.add_argument("positionals", nargs="*", metavar="HOST SOURCE")
    parser.add_argument(
        "-H", "--host", help="destination/new Mac hostname or IPv4 address"
    )
    parser.add_argument("-s", "--source", help="source file or directory to send")
    parser.add_argument(
        "-p",
        "--port",
        type=valid_port,
        default=env_port(archive),
        help=f"TCP port to connect to (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--contents",
        action="store_true",
        help="copy the contents of SOURCE, not SOURCE itself",
    )
    if archive is ArchiveKind.TAR:
        parser.add_argument(
            "--exclude", action="append", default=[], help="add one tar exclude pattern"
        )
        parser.add_argument(
            "--exclude-from",
            action="append",
            default=[],
            help="read additional tar exclude patterns from FILE",
        )
        parser.add_argument(
            "--local-excludes",
            default=str(LOCAL_EXCLUDES),
            help="gitignored local exclude file to use when present",
        )
        parser.add_argument(
            "--no-local-excludes",
            action="store_true",
            help="do not use the local exclude file",
        )
        parser.add_argument(
            "--no-default-excludes",
            action="store_true",
            help="do not use managed generated-state excludes",
        )
        parser.add_argument(
            "--no-mac-metadata",
            action="store_true",
            help="skip tar ACL/xattr/resource-fork metadata handling",
        )
    parser.add_argument(
        "--no-sudo",
        action="store_true",
        help=f"do not run {archive.value} through sudo",
    )
    parser.add_argument(
        "--no-pv", action="store_true", help="do not use pv even when installed"
    )
    parser.add_argument(
        "--estimate-size",
        action="store_true",
        help="run du before archive send so pv can show ETA; can be slow on huge trees",
    )
    parser.add_argument(
        "--allow-unsafe-network",
        action="store_true",
        help="permit non-LAN destination addresses",
    )


def add_rsync_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source")
    parser.add_argument("dest")
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show what would change without writing",
    )
    parser.add_argument(
        "--delete",
        dest="delete",
        action="store_true",
        default=True,
        help="delete destination files missing from source",
    )
    parser.add_argument(
        "--no-delete",
        dest="delete",
        action="store_false",
        help="do not delete destination-only files",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], help="add one rsync exclude pattern"
    )
    parser.add_argument(
        "--exclude-from",
        action="append",
        default=[],
        help="read additional rsync exclude patterns from FILE",
    )
    parser.add_argument(
        "--local-excludes",
        default=str(LOCAL_EXCLUDES),
        help="gitignored local exclude file to use when present",
    )
    parser.add_argument(
        "--no-local-excludes",
        action="store_true",
        help="do not use the local exclude file",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="do not use managed generated-state excludes",
    )
    parser.add_argument(
        "--no-mac-metadata",
        action="store_true",
        help="skip ACL/xattr/file-flag/create-time flags",
    )
    parser.add_argument(
        "--no-progress", action="store_true", help="disable progress output"
    )
    parser.add_argument(
        "--itemize", action="store_true", help="show rsync itemized changes"
    )


def add_rsync_from_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("positionals", nargs="*", metavar="HOST SOURCE DEST")
    parser.add_argument(
        "-H", "--host", help="old/source machine hostname or IPv4 address"
    )
    parser.add_argument(
        "-u",
        "--user",
        help="SSH user on the old/source machine; defaults to ssh's current-user behavior",
    )
    parser.add_argument("-s", "--source", help="remote source path on the old machine")
    parser.add_argument(
        "-d", "--dest", help="local destination path on this target machine"
    )
    parser.add_argument("--ssh-port", type=valid_port, help="SSH port on source host")
    parser.add_argument(
        "--ssh-option",
        action="append",
        default=[],
        help="additional ssh -o option, for example BatchMode=yes",
    )
    add_rsync_common_client_options(parser)
    parser.add_argument(
        "--allow-unsafe-network",
        action="store_true",
        help="permit non-LAN source addresses",
    )


def add_rsync_daemon_server_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-p",
        "--port",
        type=valid_port,
        default=DEFAULT_RSYNC_DAEMON_PORT,
        help=f"TCP port to listen on (default: {DEFAULT_RSYNC_DAEMON_PORT})",
    )
    parser.add_argument(
        "-b",
        "--bind",
        default="",
        help="private LAN IPv4 address to bind; auto-detected by default",
    )
    parser.add_argument(
        "-d",
        "--dest",
        required=True,
        help="destination root exposed as the writable rsync module",
    )
    parser.add_argument(
        "--module",
        default=DEFAULT_RSYNC_DAEMON_MODULE,
        help=f"module name exposed to clients (default: {DEFAULT_RSYNC_DAEMON_MODULE})",
    )
    parser.add_argument(
        "--user",
        default=DEFAULT_RSYNC_DAEMON_USER,
        help=f"daemon auth user (default: {DEFAULT_RSYNC_DAEMON_USER})",
    )
    parser.add_argument(
        "--password-file",
        help="read daemon password from this file instead of generating one",
    )
    parser.add_argument(
        "--hosts-allow",
        action="append",
        default=[],
        help="client IP/CIDR allowed to connect; defaults to the bind interface subnet",
    )
    parser.add_argument(
        "--allow-unsafe-network",
        action="store_true",
        help="permit non-LAN bind addresses such as 0.0.0.0",
    )


def add_rsync_daemon_sync_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("positionals", nargs="*", metavar="HOST SOURCE")
    parser.add_argument(
        "-H", "--host", help="destination/new Mac hostname or IPv4 address"
    )
    parser.add_argument("-s", "--source", help="source file or directory to send")
    parser.add_argument(
        "-p",
        "--port",
        type=valid_port,
        default=DEFAULT_RSYNC_DAEMON_PORT,
        help=f"rsync daemon TCP port (default: {DEFAULT_RSYNC_DAEMON_PORT})",
    )
    parser.add_argument(
        "--module",
        default=DEFAULT_RSYNC_DAEMON_MODULE,
        help=f"rsync daemon module (default: {DEFAULT_RSYNC_DAEMON_MODULE})",
    )
    parser.add_argument(
        "--user",
        default=DEFAULT_RSYNC_DAEMON_USER,
        help=f"rsync daemon auth user (default: {DEFAULT_RSYNC_DAEMON_USER})",
    )
    parser.add_argument(
        "--module-path", default="", help="optional path inside the rsync daemon module"
    )
    parser.add_argument(
        "--password-file", help="file containing the rsync daemon password"
    )
    parser.add_argument(
        "--password-env",
        default=RSYNC_PASSWORD_ENV,
        help=f"environment variable containing the rsync daemon password (default: {RSYNC_PASSWORD_ENV})",
    )
    add_rsync_common_client_options(parser)
    parser.add_argument(
        "--allow-unsafe-network",
        action="store_true",
        help="permit non-LAN destination addresses",
    )


def add_rsync_common_client_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show what would change without writing",
    )
    parser.add_argument(
        "--delete",
        dest="delete",
        action="store_true",
        default=True,
        help="delete destination files missing from source",
    )
    parser.add_argument(
        "--no-delete",
        dest="delete",
        action="store_false",
        help="do not delete destination-only files",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], help="add one rsync exclude pattern"
    )
    parser.add_argument(
        "--exclude-from",
        action="append",
        default=[],
        help="read additional rsync exclude patterns from FILE",
    )
    parser.add_argument(
        "--local-excludes",
        default=str(LOCAL_EXCLUDES),
        help="gitignored local exclude file to use when present",
    )
    parser.add_argument(
        "--no-local-excludes",
        action="store_true",
        help="do not use the local exclude file",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="do not use managed generated-state excludes",
    )
    parser.add_argument(
        "--no-mac-metadata",
        action="store_true",
        help="skip ACL/xattr/file-flag/create-time flags",
    )
    parser.add_argument(
        "--no-progress", action="store_true", help="disable progress output"
    )
    parser.add_argument(
        "--itemize", action="store_true", help="show rsync itemized changes"
    )


def env_port(archive: ArchiveKind) -> int:
    value = os.environ.get(f"{archive.value.upper()}_COPY_PORT") or os.environ.get(
        "DITTO_COPY_PORT"
    )
    if value is None:
        return DEFAULT_PORT
    return valid_port(value)


def env_bind(archive: ArchiveKind) -> str:
    return os.environ.get(f"{archive.value.upper()}_COPY_BIND") or os.environ.get(
        "DITTO_COPY_BIND", ""
    )


def env_dest(archive: ArchiveKind) -> str:
    return os.environ.get(f"{archive.value.upper()}_COPY_DEST") or os.environ.get(
        "DITTO_COPY_DEST", os.getcwd()
    )


def valid_port(value: str | int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(f"invalid port: {value}") from error
    if not 0 < port < 65536:
        raise argparse.ArgumentTypeError(f"port out of range: {value}")
    return port


def recv_archive(args: argparse.Namespace, *, archive: ArchiveKind) -> int:
    require(archive.value)

    bind_ip = select_bind_ip(args.bind, allow_unsafe=args.allow_unsafe_network)
    dest = Path(args.dest)
    use_sudo = not args.no_sudo

    if use_sudo:
        require("sudo")
        log_state(
            TransferState.AUTH,
            "refreshing sudo credentials before opening the listener",
        )
        run_checked(["sudo", "-v"])
        run_checked(["sudo", "mkdir", "-p", str(dest)])
    else:
        dest.mkdir(parents=True, exist_ok=True)

    listener = open_listen_socket(bind_ip, args.port)
    last_code = 0
    try:
        log_state(
            TransferState.LISTEN,
            f"listening for {archive.value} on {bind_ip}:{args.port}; destination={dest}",
        )
        while True:
            _, accepted_archive, connection, peer = accept_archive_connection(
                {listener: archive}
            )
            with connection:
                code = receive_archive_connection(
                    archive=accepted_archive,
                    connection=connection,
                    peer=peer,
                    dest=dest,
                    use_sudo=use_sudo,
                    no_pv=args.no_pv,
                    mac_metadata=not getattr(args, "no_mac_metadata", False),
                )
            last_code = code
            log_state(TransferState.DONE, f"transfer exited with status {code}")
            if args.once:
                return code
            log_state(
                TransferState.LISTEN, "ready for next transfer; press Ctrl-C to stop"
            )
    except KeyboardInterrupt:
        log_state(TransferState.DONE, "server stopped by user")
        return 130 if last_code == 0 else last_code
    finally:
        listener.close()


def auto_server(args: argparse.Namespace) -> int:
    require(ArchiveKind.DITTO.value)
    require(ArchiveKind.TAR.value)

    bind_ip = select_bind_ip(args.bind, allow_unsafe=args.allow_unsafe_network)
    ditto_port = args.port
    tar_port = args.tar_port or next_port(ditto_port)
    if tar_port == ditto_port:
        print("tar port must differ from ditto port", file=sys.stderr)
        return 2

    dest = Path(args.dest)
    use_sudo = not args.no_sudo
    if use_sudo:
        require("sudo")
        log_state(
            TransferState.AUTH, "refreshing sudo credentials before opening listeners"
        )
        run_checked(["sudo", "-v"])
        run_checked(["sudo", "mkdir", "-p", str(dest)])
    else:
        dest.mkdir(parents=True, exist_ok=True)

    ditto_socket = open_listen_socket(bind_ip, ditto_port)
    tar_socket = open_listen_socket(bind_ip, tar_port)
    sockets = {ditto_socket: ArchiveKind.DITTO, tar_socket: ArchiveKind.TAR}

    log_state(
        TransferState.LISTEN,
        f"listening for ditto on {bind_ip}:{ditto_port}; destination={dest}",
    )
    log_state(
        TransferState.LISTEN,
        f"listening for tar on {bind_ip}:{tar_port}; destination={dest}",
    )

    last_code = 0
    try:
        while True:
            _, archive, connection, peer = accept_archive_connection(sockets)
            with connection:
                code = receive_archive_connection(
                    archive=archive,
                    connection=connection,
                    peer=peer,
                    dest=dest,
                    use_sudo=use_sudo,
                    no_pv=args.no_pv,
                    mac_metadata=not args.no_mac_metadata,
                )
            last_code = code
            log_state(TransferState.DONE, f"transfer exited with status {code}")
            if args.once:
                return code
            log_state(
                TransferState.LISTEN, "ready for next transfer; press Ctrl-C to stop"
            )
    except KeyboardInterrupt:
        log_state(TransferState.DONE, "server stopped by user")
        return 130 if last_code == 0 else last_code
    finally:
        for listener in sockets:
            listener.close()


def send_archive(args: argparse.Namespace, *, archive: ArchiveKind) -> int:
    require("nc")
    require(archive.value)

    host, source = send_host_and_source(args)
    validate_destination_host(host, allow_unsafe=args.allow_unsafe_network)

    source_path = Path(source)
    if not source_path.exists():
        print(f"source does not exist: {source}", file=sys.stderr)
        return 2
    log_state(
        TransferState.PRECHECK,
        f"source={source_path}; host={host}; archive={archive.value}; port={args.port}",
    )

    use_sudo = not args.no_sudo
    if use_sudo:
        require("sudo")
        log_state(
            TransferState.AUTH,
            "refreshing sudo credentials before starting archive creation",
        )
        run_checked(["sudo", "-v"])

    excludes = ExcludeInputs((), ())
    if archive is ArchiveKind.DITTO:
        create_cmd = build_ditto_create_command(
            source_path, keep_parent=not args.contents, sudo=use_sudo
        )
        create_plan = PipelinePlan(commands=(tuple(create_cmd),))
    else:
        excludes = collect_excludes(args)
        for exclude_file in excludes.files:
            if not Path(exclude_file).is_file():
                print(f"exclude file does not exist: {exclude_file}", file=sys.stderr)
                return 2
        create_plan = build_tar_create_pipeline(
            source_path,
            keep_parent=not args.contents,
            excludes=excludes.patterns,
            exclude_from=excludes.files,
            sudo=use_sudo,
            mac_metadata=not getattr(args, "no_mac_metadata", False),
        )
        log_exclude_summary(excludes)
        log_state(TransferState.PREPARE, "tar input manifest skips Unix socket files")

    pipeline: list[list[str]] = [list(command) for command in create_plan.commands]
    size_bytes = None
    if (
        getattr(args, "estimate_size", False)
        and not args.no_pv
        and shutil.which("pv") is not None
    ):
        log_state(
            TransferState.PREPARE,
            "estimating source size with du for pv ETA; this can be slow on huge trees",
        )
        size_bytes = du_size_bytes(source_path, sudo=use_sudo)
    pv = pv_command(args.no_pv, size_bytes=size_bytes)
    if pv is not None:
        pipeline.append(pv)
    pipeline.append(["nc", "-4", host, str(args.port)])

    log_state(TransferState.RUN, f"sending {source_path} to {host}:{args.port}")
    log_pipeline("sender", pipeline)
    code = run_pipeline(pipeline, cwd=create_plan.cwd)
    log_state(TransferState.DONE, f"sender exited with status {code}")
    return code


def auto_sync(args: argparse.Namespace) -> int:
    host, source = auto_sync_host_and_source(args)
    requested_method = TransferMethod(args.method)
    excludes = collect_excludes(
        args, include_defaults=requested_method is not TransferMethod.DITTO
    )
    if args.rsync_dest and requested_method in {
        TransferMethod.DITTO,
        TransferMethod.TAR,
        TransferMethod.RSYNC_DAEMON,
    }:
        print(
            f"--rsync-dest conflicts with --method {requested_method.value}",
            file=sys.stderr,
        )
        return 2

    decision = choose_auto_method(
        requested=requested_method,
        has_rsync_dest=bool(args.rsync_dest),
        excludes=excludes,
        ditto_port=args.port,
        tar_port=args.tar_port or next_port(args.port),
    )

    target = (
        f"{host}:{decision.port}"
        if decision.port is not None and host is not None
        else args.rsync_dest or host or "<unset>"
    )
    log_state(
        TransferState.PREPARE,
        f"selected method={decision.method.value}; target={target}; source={source}",
    )
    if decision.method is TransferMethod.RSYNC:
        rsync_args = argparse.Namespace(
            source=source,
            dest=args.rsync_dest,
            dry_run=args.dry_run,
            delete=args.delete,
            exclude=list(args.exclude),
            exclude_from=list(args.exclude_from),
            local_excludes=args.local_excludes,
            no_local_excludes=args.no_local_excludes,
            no_default_excludes=args.no_default_excludes,
            no_mac_metadata=args.no_mac_metadata,
            no_progress=args.no_progress,
            itemize=args.itemize,
        )
        return rsync_repos(rsync_args)

    if decision.method is TransferMethod.RSYNC_DAEMON:
        if host is None:
            print("rsync daemon sync requires --host", file=sys.stderr)
            return 2
        daemon_args = argparse.Namespace(
            host=host,
            source=source,
            positionals=[],
            port=args.rsync_daemon_port,
            module=args.rsync_module,
            user=args.rsync_user,
            module_path=args.rsync_module_path,
            password_file=args.rsync_password_file,
            password_env=args.rsync_password_env,
            dry_run=args.dry_run,
            delete=args.delete,
            exclude=list(args.exclude),
            exclude_from=list(args.exclude_from),
            local_excludes=args.local_excludes,
            no_local_excludes=args.no_local_excludes,
            no_default_excludes=args.no_default_excludes,
            no_mac_metadata=args.no_mac_metadata,
            no_progress=args.no_progress,
            itemize=args.itemize,
            allow_unsafe_network=args.allow_unsafe_network,
        )
        return rsync_daemon_sync(daemon_args)

    if host is None:
        print("initial archive sync requires --host", file=sys.stderr)
        return 2

    send_args = argparse.Namespace(
        host=host,
        source=source,
        positionals=[],
        port=decision.port,
        contents=args.contents,
        no_sudo=args.no_sudo,
        no_pv=args.no_pv,
        estimate_size=args.estimate_size,
        no_mac_metadata=args.no_mac_metadata,
        allow_unsafe_network=args.allow_unsafe_network,
        exclude=list(decision.excludes.patterns),
        exclude_from=list(decision.excludes.files),
        local_excludes=args.local_excludes,
        no_local_excludes=True,
        no_default_excludes=True,
    )
    return send_archive(send_args, archive=archive_for_method(decision.method))


def auto_sync_host_and_source(args: argparse.Namespace) -> tuple[str | None, str]:
    positionals = list(args.positionals)
    host = args.host
    source = args.source

    if args.rsync_dest:
        if len(positionals) > 1:
            raise SystemExit(
                "catch-up rsync mode accepts only SOURCE as a positional argument"
            )
        if positionals:
            if source:
                raise SystemExit("source provided both as option and positional")
            source = positionals.pop(0)
        if host:
            raise SystemExit("catch-up rsync mode does not use --host")
    else:
        if positionals:
            if host:
                raise SystemExit("host provided both as option and positional")
            host = positionals.pop(0)
        if positionals:
            if source:
                raise SystemExit("source provided both as option and positional")
            source = positionals.pop(0)
        if positionals:
            raise SystemExit("too many positional arguments")

    if not source:
        raise SystemExit("missing SOURCE")
    return host, source


def collect_excludes(
    args: argparse.Namespace, *, include_defaults: bool = True
) -> ExcludeInputs:
    patterns: list[str] = []
    if include_defaults and not getattr(args, "no_default_excludes", False):
        patterns.extend(DEFAULT_GENERATED_EXCLUDES)
    patterns.extend(getattr(args, "exclude", ()) or ())
    files = list(getattr(args, "exclude_from", ()) or ())
    local_excludes = Path(getattr(args, "local_excludes", LOCAL_EXCLUDES))
    if not getattr(args, "no_local_excludes", False) and local_excludes.is_file():
        files.append(str(local_excludes))
    return ExcludeInputs(patterns=tuple(patterns), files=tuple(files))


def choose_auto_method(
    *,
    requested: str | TransferMethod,
    has_rsync_dest: bool,
    excludes: ExcludeInputs,
    ditto_port: int,
    tar_port: int,
) -> AutoDecision:
    requested_method = (
        TransferMethod(requested) if isinstance(requested, str) else requested
    )

    if requested_method is TransferMethod.AUTO:
        if has_rsync_dest:
            return AutoDecision(
                method=TransferMethod.RSYNC, port=None, excludes=ExcludeInputs((), ())
            )
        method = TransferMethod.TAR if excludes.present else TransferMethod.DITTO
    else:
        method = requested_method

    if method is TransferMethod.RSYNC:
        if not has_rsync_dest:
            raise SystemExit("--method rsync requires --rsync-dest")
        return AutoDecision(
            method=TransferMethod.RSYNC, port=None, excludes=ExcludeInputs((), ())
        )
    if method is TransferMethod.RSYNC_DAEMON:
        return AutoDecision(
            method=TransferMethod.RSYNC_DAEMON,
            port=None,
            excludes=ExcludeInputs((), ()),
        )
    if method is TransferMethod.DITTO:
        if excludes.present:
            raise SystemExit(
                "ditto does not support excludes; use --method auto or --method tar"
            )
        return AutoDecision(
            method=TransferMethod.DITTO, port=ditto_port, excludes=ExcludeInputs((), ())
        )
    if method is TransferMethod.TAR:
        return AutoDecision(method=TransferMethod.TAR, port=tar_port, excludes=excludes)

    raise ValueError(f"unknown method: {requested_method}")


def archive_for_method(method: TransferMethod) -> ArchiveKind:
    if method is TransferMethod.DITTO:
        return ArchiveKind.DITTO
    if method is TransferMethod.TAR:
        return ArchiveKind.TAR
    raise ValueError(f"method does not map to archive kind: {method.value}")


def send_host_and_source(args: argparse.Namespace) -> tuple[str, str]:
    positionals = list(args.positionals)
    host = args.host
    source = args.source

    if positionals:
        if host:
            raise SystemExit("host provided both as option and positional")
        host = positionals.pop(0)
    if positionals:
        if source:
            raise SystemExit("source provided both as option and positional")
        source = positionals.pop(0)
    if positionals:
        raise SystemExit("too many positional arguments")
    if not host:
        raise SystemExit("missing HOST")
    if not source:
        raise SystemExit("missing SOURCE")
    return host, source


def build_extract_command(
    archive: ArchiveKind, dest: Path, *, sudo: bool, mac_metadata: bool = True
) -> list[str]:
    if archive is ArchiveKind.DITTO:
        command = ["ditto", "-x", "-", str(dest)]
    elif archive is ArchiveKind.TAR:
        command = ["tar"]
        if mac_metadata:
            command.extend(("--mac-metadata", "--acls", "--xattrs"))
        command.extend(("-xpf", "-", "-C", str(dest)))
    else:
        raise ValueError(f"unknown archive type: {archive.value}")
    return ["sudo", *command] if sudo else command


def build_ditto_create_command(
    source: Path, *, keep_parent: bool, sudo: bool
) -> list[str]:
    command = ["ditto", "-c"]
    if keep_parent:
        command.append("--keepParent")
    command.extend((str(source), "-"))
    return ["sudo", *command] if sudo else command


def build_tar_create_command(
    source: Path,
    *,
    keep_parent: bool,
    excludes: Sequence[str],
    exclude_from: Sequence[str],
    sudo: bool,
    mac_metadata: bool = True,
) -> list[str]:
    command = ["tar"]
    if mac_metadata:
        command.extend(("--mac-metadata", "--acls", "--xattrs"))
    command.extend(("-cpf", "-"))
    for pattern in excludes:
        command.append(f"--exclude={pattern}")
    for file in exclude_from:
        command.append(f"--exclude-from={file}")

    if keep_parent:
        command.extend(("-C", str(source.parent.resolve()), source.name))
    else:
        if not source.is_dir():
            raise SystemExit("--contents requires SOURCE to be a directory")
        command.extend(("-C", str(source.resolve()), "."))
    return ["sudo", *command] if sudo else command


def build_tar_create_pipeline(
    source: Path,
    *,
    keep_parent: bool,
    excludes: Sequence[str],
    exclude_from: Sequence[str],
    sudo: bool,
    mac_metadata: bool = True,
) -> PipelinePlan:
    cwd, start = tar_manifest_root(source, keep_parent=keep_parent)
    file_patterns = read_exclude_file_patterns(exclude_from)
    find_cmd = build_find_manifest_command(
        start, excludes=(*excludes, *file_patterns), sudo=sudo
    )
    tar_cmd = build_tar_create_from_manifest_command(
        excludes=tar_exclude_patterns(excludes),
        exclude_from=exclude_from,
        sudo=sudo,
        mac_metadata=mac_metadata,
    )
    return PipelinePlan(commands=(tuple(find_cmd), tuple(tar_cmd)), cwd=cwd)


def tar_manifest_root(source: Path, *, keep_parent: bool) -> tuple[Path, str]:
    if keep_parent:
        return source.parent.resolve(), source.name
    if not source.is_dir():
        raise SystemExit("--contents requires SOURCE to be a directory")
    return source.resolve(), "."


def build_find_manifest_command(
    start: str, *, excludes: Sequence[str], sudo: bool
) -> list[str]:
    command = ["find", start]
    prune_names = find_prune_names(excludes)
    if prune_names:
        command.extend(("(", "-type", "d", "("))
        for index, name in enumerate(prune_names):
            if index:
                command.append("-o")
            command.extend(("-name", name))
        command.extend((")", "-prune", ")", "-o"))
    command.extend(("!", "-type", "s", "-print0"))
    return ["sudo", *command] if sudo else command


def find_prune_names(patterns: Sequence[str]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        stripped = pattern.strip()
        if not stripped.endswith("/"):
            continue
        name = stripped.rstrip("/")
        if not name or "/" in name or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return tuple(names)


def read_exclude_file_patterns(paths: Sequence[str]) -> tuple[str, ...]:
    patterns: list[str] = []
    for path in paths:
        for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return tuple(patterns)


def build_tar_create_from_manifest_command(
    *,
    excludes: Sequence[str],
    exclude_from: Sequence[str],
    sudo: bool,
    mac_metadata: bool = True,
) -> list[str]:
    command = ["tar"]
    if mac_metadata:
        command.extend(("--mac-metadata", "--acls", "--xattrs"))
    command.extend(("-cpf", "-", "--null", "--no-recursion", "-T", "-"))
    for pattern in excludes:
        command.append(f"--exclude={pattern}")
    for file in exclude_from:
        command.append(f"--exclude-from={file}")
    return ["sudo", *command] if sudo else command


def rsync_repos(args: argparse.Namespace) -> int:
    require("rsync")
    command = ["rsync", *build_rsync_client_args(args), args.source, args.dest]
    print(f"Running: {shlex.join(command)}", file=sys.stderr)
    return subprocess.run(command).returncode


def rsync_from(args: argparse.Namespace) -> int:
    require("rsync")
    host, _, _ = rsync_from_host_source_dest(args)
    validate_source_host(host, allow_unsafe=args.allow_unsafe_network)
    command = build_rsync_from_command(args)
    print(f"Running: {shlex.join(command)}", file=sys.stderr)
    return subprocess.run(command).returncode


def rsync_daemon_server(args: argparse.Namespace) -> int:
    require("rsync")

    bind_ip = select_bind_ip(args.bind, allow_unsafe=args.allow_unsafe_network)
    dest = Path(args.dest).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    validate_module_name(args.module)
    validate_daemon_name(args.user, label="user")

    hosts_allow = (
        tuple(args.hosts_allow)
        if args.hosts_allow
        else (hosts_allow_for_bind(bind_ip),)
    )
    password, generated = daemon_password(args.password_file)

    state_root = Path.home() / ".macsetup" / "sync" / "rsyncd"
    state_root.mkdir(parents=True, exist_ok=True)
    state_root.chmod(0o700)

    with TemporaryDirectory(prefix="run-", dir=state_root) as temp_name:
        temp_dir = Path(temp_name)
        config_path, secrets_path = write_rsyncd_files(
            temp_dir=temp_dir,
            bind_ip=bind_ip,
            port=args.port,
            module=args.module,
            user=args.user,
            password=password,
            dest=dest,
            hosts_allow=hosts_allow,
        )

        print(f"Rsync daemon listening on {bind_ip}:{args.port}", file=sys.stderr)
        print(f"Module: {args.module}", file=sys.stderr)
        print(f"Destination: {dest}", file=sys.stderr)
        print(f"Hosts allow: {', '.join(hosts_allow)}", file=sys.stderr)
        print(f"Config: {config_path}", file=sys.stderr)
        if generated:
            print(
                f"Generated one-time rsync daemon password for user {args.user}: {password}",
                file=sys.stderr,
            )
            print(
                "Set it on the sender via MACSETUP_RSYNC_PASSWORD or a local password file.",
                file=sys.stderr,
            )

        command = [
            "rsync",
            "--daemon",
            "--no-detach",
            "--config",
            str(config_path),
            "--address",
            bind_ip,
            "--port",
            str(args.port),
        ]
        print(f"Running: {shlex.join(command)}", file=sys.stderr)
        return subprocess.run(command).returncode


def rsync_daemon_sync(args: argparse.Namespace) -> int:
    require("rsync")
    host, source = send_host_and_source(args)
    validate_destination_host(host, allow_unsafe=args.allow_unsafe_network)
    validate_module_name(args.module)
    validate_daemon_name(args.user, label="user")

    destination = rsync_daemon_url(
        host=host,
        port=args.port,
        module=args.module,
        user=args.user,
        module_path=args.module_path,
    )
    rsync_args = build_rsync_client_args(args)
    with daemon_client_password_file(
        args.password_file, args.password_env
    ) as password_file:
        command = [
            "rsync",
            *rsync_args,
            "--port",
            str(args.port),
            "--password-file",
            str(password_file),
            source,
            destination,
        ]
        print(
            f"Running: {shlex.join(redact_password_file_command(command, password_file))}",
            file=sys.stderr,
        )
        return subprocess.run(command).returncode


def rsync_from_host_source_dest(args: argparse.Namespace) -> tuple[str, str, str]:
    positionals = list(args.positionals)
    host = args.host
    source = args.source
    dest = args.dest

    if positionals:
        if host:
            raise SystemExit("host provided both as option and positional")
        host = positionals.pop(0)
    if positionals:
        if source:
            raise SystemExit("source provided both as option and positional")
        source = positionals.pop(0)
    if positionals:
        if dest:
            raise SystemExit("dest provided both as option and positional")
        dest = positionals.pop(0)
    if positionals:
        raise SystemExit("too many positional arguments")
    if not host:
        raise SystemExit("missing HOST")
    if not source:
        raise SystemExit("missing SOURCE")
    if not dest:
        raise SystemExit("missing DEST")
    return host, source, dest


def build_rsync_from_command(args: argparse.Namespace) -> list[str]:
    host, source, dest = rsync_from_host_source_dest(args)
    rsync_args = build_rsync_client_args(args)
    remote_shell = build_rsync_remote_shell(args)
    if remote_shell:
        rsync_args.extend(("-e", shlex.join(remote_shell)))
    return [
        "rsync",
        *rsync_args,
        rsync_remote_source(host=host, user=args.user, source=source),
        dest,
    ]


def build_rsync_remote_shell(args: argparse.Namespace) -> list[str]:
    command = ["ssh"]
    if args.ssh_port:
        command.extend(("-p", str(args.ssh_port)))
    for option in args.ssh_option:
        command.extend(("-o", option))
    return command if len(command) > 1 else []


def rsync_remote_source(*, host: str, user: str | None, source: str) -> str:
    if user:
        validate_ssh_user(user)
        return f"{user}@{host}:{source}"
    return f"{host}:{source}"


def validate_ssh_user(user: str) -> None:
    if any(character in user for character in ":/@ \t\r\n"):
        raise SystemExit("SSH user must not contain whitespace, colon, slash, or @")


def build_rsync_client_args(args: argparse.Namespace) -> list[str]:
    help_text = rsync_help()
    rsync_args = ["-a", "--whole-file", "--no-compress", "--partial"]

    if not args.no_mac_metadata:
        for flag in ("--acls", "--xattrs", "--fileflags", "--crtimes"):
            if flag in help_text:
                rsync_args.append(flag)

    if args.delete:
        rsync_args.extend(("--delete", "--delete-after"))
    if args.dry_run:
        rsync_args.append("--dry-run")
    if args.itemize:
        rsync_args.append("--itemize-changes")
    if not args.no_progress:
        rsync_args.append(
            "--info=progress2" if "--info=" in help_text else "--progress"
        )
    if "--mkpath" in help_text:
        rsync_args.append("--mkpath")

    if not args.no_default_excludes:
        for pattern in DEFAULT_RSYNC_EXCLUDES:
            rsync_args.append(f"--exclude={pattern}")
    for pattern in args.exclude:
        rsync_args.append(f"--exclude={pattern}")
    for file in args.exclude_from:
        if not Path(file).is_file():
            raise SystemExit(f"exclude file does not exist: {file}")
        rsync_args.append(f"--exclude-from={file}")

    local_excludes = Path(args.local_excludes)
    if not args.no_local_excludes and local_excludes.is_file():
        rsync_args.append(f"--exclude-from={local_excludes}")

    return rsync_args


def rsync_help() -> str:
    result = subprocess.run(
        ["rsync", "--help"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.stdout


def validate_module_name(value: str) -> None:
    validate_daemon_name(value, label="module")


def validate_daemon_name(value: str, *, label: str) -> None:
    if not value or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in value
    ):
        raise SystemExit(
            f"{label} must contain only letters, numbers, underscores, and hyphens"
        )


def daemon_password(password_file: str | None) -> tuple[str, bool]:
    if password_file:
        password = (
            Path(password_file).read_text(encoding="utf-8").splitlines()[0].strip()
        )
        if not password:
            raise SystemExit("password file is empty")
        return password, False
    return secrets.token_urlsafe(32), True


def write_rsyncd_files(
    *,
    temp_dir: Path,
    bind_ip: str,
    port: int,
    module: str,
    user: str,
    password: str,
    dest: Path,
    hosts_allow: Sequence[str],
) -> tuple[Path, Path]:
    config_path = temp_dir / "rsyncd.conf"
    secrets_path = temp_dir / "rsyncd.secrets"
    pid_path = temp_dir / "rsyncd.pid"
    lock_path = temp_dir / "rsyncd.lock"
    log_path = temp_dir / "rsyncd.log"

    secrets_path.write_text(f"{user}:{password}\n", encoding="utf-8")
    secrets_path.chmod(0o600)

    config = f"""# Generated by macsetup sync rsync-daemon-server.
address = {bind_ip}
port = {port}
pid file = {pid_path}
lock file = {lock_path}
log file = {log_path}
reverse lookup = false
forward lookup = false
max connections = 1
timeout = 600

[{module}]
    path = {dest}
    comment = macsetup sync target
    read only = false
    list = false
    use chroot = false
    munge symlinks = no
    auth users = {user}:rw
    secrets file = {secrets_path}
    strict modes = true
    hosts allow = {", ".join(hosts_allow)}
    hosts deny = *
"""
    config_path.write_text(config, encoding="utf-8")
    config_path.chmod(0o600)
    return config_path, secrets_path


def hosts_allow_for_bind(bind_ip: str) -> str:
    for candidate in private_ipv4_candidates():
        if candidate.address == bind_ip and candidate.network:
            return candidate.network
    raise SystemExit(
        "could not infer bind interface subnet; pass --hosts-allow <client-ip-or-cidr>"
    )


def rsync_daemon_url(
    *, host: str, port: int, module: str, user: str, module_path: str
) -> str:
    clean_path = module_path.strip("/")
    suffix = f"/{clean_path}/" if clean_path else "/"
    return f"rsync://{user}@{host}:{port}/{module}{suffix}"


@contextlib.contextmanager
def daemon_client_password_file(
    password_file: str | None, password_env: str
) -> Iterator[Path]:
    if password_file:
        path = Path(password_file)
        if not path.is_file():
            raise SystemExit(f"password file does not exist: {password_file}")
        yield path
        return

    password = os.environ.get(password_env)
    if password is None:
        if not sys.stdin.isatty():
            raise SystemExit(
                f"set {password_env} or pass --password-file for rsync daemon auth"
            )
        password = getpass.getpass("Rsync daemon password: ")
    if not password:
        raise SystemExit("rsync daemon password is empty")

    state_root = Path.home() / ".macsetup" / "sync"
    state_root.mkdir(parents=True, exist_ok=True)
    state_root.chmod(0o700)
    temp_path = state_root / f"rsync-password-{os.getpid()}-{secrets.token_hex(8)}"
    try:
        temp_path.write_text(f"{password}\n", encoding="utf-8")
        temp_path.chmod(0o600)
        yield temp_path
    finally:
        temp_path.unlink(missing_ok=True)


def redact_password_file_command(
    command: Sequence[str], password_file: Path
) -> list[str]:
    redacted = []
    skip_next = False
    for item in command:
        if skip_next:
            redacted.append("<password-file>")
            skip_next = False
            continue
        redacted.append(item)
        if item == "--password-file":
            skip_next = True
        elif item.startswith("--password-file=") and item.endswith(str(password_file)):
            redacted[-1] = "--password-file=<password-file>"
    return redacted


def pv_command(no_pv: bool, *, size_bytes: int | None = None) -> list[str] | None:
    if no_pv:
        log_state(TransferState.PREPARE, "pv progress disabled by --no-pv")
        return None
    if shutil.which("pv") is None:
        log_state(
            TransferState.PREPARE,
            "pv not found on PATH; byte-rate progress is unavailable",
        )
        return None
    if size_bytes is None:
        return ["pv", "-rabt"]
    return ["pv", "-s", str(size_bytes), "-rabt"]


def require(command: str) -> None:
    if shutil.which(command) is None:
        raise SystemExit(f"missing required command: {command}")


def run_checked(command: Sequence[str]) -> None:
    subprocess.run(command, check=True)


def log_state(state: TransferState, message: str) -> None:
    print(
        f"[{time.strftime('%H:%M:%S')}] {state.value}: {message}",
        file=sys.stderr,
        flush=True,
    )


def log_pipeline(label: str, commands: Sequence[Sequence[str]]) -> None:
    for index, command in enumerate(commands, start=1):
        log_state(
            TransferState.RUN,
            f"{label} pipeline[{index}/{len(commands)}]: {shlex.join(command)}",
        )


def log_exclude_summary(excludes: ExcludeInputs) -> None:
    pattern_count = len(excludes.patterns)
    file_count = len(excludes.files)
    log_state(
        TransferState.PREPARE,
        f"effective excludes: {pattern_count} pattern(s), {file_count} exclude file(s)",
    )
    for pattern in excludes.patterns[:20]:
        log_state(TransferState.PREPARE, f"exclude pattern: {pattern}")
    if pattern_count > 20:
        log_state(
            TransferState.PREPARE, f"... {pattern_count - 20} more exclude pattern(s)"
        )
    for file in excludes.files:
        log_state(TransferState.PREPARE, f"exclude file: {file}")


def archive_extract_pipeline(
    extract_cmd: Sequence[str], *, no_pv: bool
) -> list[list[str]]:
    commands = []
    pv = pv_command(no_pv)
    if pv is not None:
        commands.append(pv)
    commands.append(list(extract_cmd))
    return commands


def receive_archive_connection(
    *,
    archive: ArchiveKind,
    connection: socket.socket,
    peer: tuple[str, int],
    dest: Path,
    use_sudo: bool,
    no_pv: bool,
    mac_metadata: bool,
) -> int:
    log_state(
        TransferState.ACCEPT,
        f"accepted {archive.value} stream from {peer[0]}:{peer[1]}",
    )
    if use_sudo:
        log_state(
            TransferState.AUTH,
            "refreshing sudo credentials immediately before extraction",
        )
        sudo = subprocess.run(["sudo", "-v"], check=False)
        if sudo.returncode != 0:
            log_state(
                TransferState.DONE,
                f"sudo refresh failed with status {sudo.returncode}; transfer not extracted",
            )
            return sudo.returncode
    extract_cmd = build_extract_command(
        archive, dest, sudo=use_sudo, mac_metadata=mac_metadata
    )
    commands = archive_extract_pipeline(extract_cmd, no_pv=no_pv)
    log_pipeline("receiver", commands)
    with connection.makefile("rb", buffering=0) as stream:
        return run_pipeline_from_input(stream, commands)


def accept_archive_connection(
    sockets: dict[socket.socket, ArchiveKind],
) -> tuple[socket.socket, ArchiveKind, socket.socket, tuple[str, int]]:
    summary = ", ".join(
        f"{archive.value}={listener.getsockname()[0]}:{listener.getsockname()[1]}"
        for listener, archive in sockets.items()
    )
    while True:
        log_state(TransferState.WAIT, f"waiting for incoming connection ({summary})")
        ready, _, _ = select.select(tuple(sockets), (), (), WAIT_HEARTBEAT_SECONDS)
        if not ready:
            continue
        selected = ready[0]
        archive = sockets[selected]
        connection, peer = selected.accept()
        return selected, archive, connection, (str(peer[0]), int(peer[1]))


def run_pipeline(commands: Sequence[Sequence[str]], *, cwd: Path | None = None) -> int:
    processes: list[subprocess.Popen[bytes]] = []
    previous_stdout = None

    for index, command in enumerate(commands):
        stdout = subprocess.PIPE if index < len(commands) - 1 else None
        process = subprocess.Popen(
            command, stdin=previous_stdout, stdout=stdout, cwd=cwd
        )
        if previous_stdout is not None:
            previous_stdout.close()
        previous_stdout = process.stdout
        processes.append(process)

    exit_codes = [process.wait() for process in processes]
    for command, exit_code in zip(commands, exit_codes):
        if exit_code != 0:
            print(
                f"command failed ({exit_code}): {shlex.join(command)}", file=sys.stderr
            )
            return exit_code
    return 0


def run_pipeline_from_input(input_stream, commands: Sequence[Sequence[str]]) -> int:  # type: ignore[no-untyped-def]
    processes: list[subprocess.Popen[bytes]] = []
    previous_input = input_stream

    for index, command in enumerate(commands):
        stdout = subprocess.PIPE if index < len(commands) - 1 else None
        process = subprocess.Popen(command, stdin=previous_input, stdout=stdout)
        if previous_input is not input_stream and previous_input is not None:
            previous_input.close()
        previous_input = process.stdout
        processes.append(process)

    exit_codes = [process.wait() for process in processes]
    for command, exit_code in zip(commands, exit_codes):
        if exit_code != 0:
            print(
                f"command failed ({exit_code}): {shlex.join(command)}", file=sys.stderr
            )
            return exit_code
    return 0


def open_listen_socket(bind_ip: str, port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((bind_ip, port))
        listener.listen(1)
    except OSError:
        listener.close()
        raise
    return listener


def next_port(port: int) -> int:
    if port >= 65535:
        raise SystemExit("cannot infer tar port from 65535; pass --tar-port")
    return port + 1


def du_size_bytes(path: Path, *, sudo: bool) -> int | None:
    command = ["du", "-sk", str(path)]
    if sudo:
        command.insert(0, "sudo")
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    return int(result.stdout.split()[0]) * 1024


def is_lan_ipv4(value: str) -> bool:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        return False
    return any(address in network for network in LAN_IPV4_NETWORKS)


def resolve_ipv4s(host: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return ()
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def validate_destination_host(host: str, *, allow_unsafe: bool) -> None:
    validate_lan_host(host, allow_unsafe=allow_unsafe, label="destination")


def validate_source_host(host: str, *, allow_unsafe: bool) -> None:
    validate_lan_host(host, allow_unsafe=allow_unsafe, label="source")


def validate_lan_host(host: str, *, allow_unsafe: bool, label: str) -> None:
    addresses = resolve_ipv4s(host)
    if not addresses:
        if allow_unsafe:
            warn_unsafe(f"could not verify {label} address for {host}")
            return
        raise SystemExit(
            f"could not resolve '{host}' to IPv4; pass a private LAN IPv4 address or use --allow-unsafe-network"
        )

    unsafe = tuple(address for address in addresses if not is_lan_ipv4(address))
    if unsafe:
        if allow_unsafe:
            warn_unsafe(f"{label} has non-LAN IPv4 address(es): {', '.join(unsafe)}")
            return
        raise SystemExit(f"refusing non-LAN {label} address(es): {', '.join(unsafe)}")


def select_bind_ip(bind_ip: str, *, allow_unsafe: bool) -> str:
    if bind_ip:
        if is_lan_ipv4(bind_ip):
            return bind_ip
        if allow_unsafe:
            warn_unsafe(
                f"binding plaintext transfer listener to non-LAN address: {bind_ip}"
            )
            return bind_ip
        raise SystemExit(
            f"refusing non-LAN bind address '{bind_ip}'; use --bind with RFC1918/link-local IPv4 or pass --allow-unsafe-network"
        )

    candidates = private_ipv4_candidates()
    if len(candidates) == 1:
        return candidates[0].address

    print("Private IPv4 candidates:", file=sys.stderr)
    for candidate in candidates:
        print(f"- {candidate.interface} {candidate.address}", file=sys.stderr)
    raise SystemExit(
        "could not choose one private bind address; rerun with --bind <private-lan-ip>"
    )


def private_ipv4_candidates() -> tuple[InterfaceAddress, ...]:
    result = subprocess.run(
        ["ifconfig"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return ()

    candidates = []
    interface = ""
    for line in result.stdout.splitlines():
        if line and not line[0].isspace():
            interface = line.split(":", 1)[0]
            continue
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == "inet":
            address = parts[1]
            if interface != "lo0" and is_lan_ipv4(address):
                candidates.append(
                    InterfaceAddress(
                        interface=interface,
                        address=address,
                        network=interface_network(address=address, parts=parts),
                    )
                )
    return tuple(candidates)


def interface_network(*, address: str, parts: Sequence[str]) -> str | None:
    try:
        netmask_index = parts.index("netmask")
    except ValueError:
        return None
    if netmask_index + 1 >= len(parts):
        return None

    prefix = netmask_to_prefix(parts[netmask_index + 1])
    if prefix is None:
        return None
    return str(ipaddress.IPv4Network(f"{address}/{prefix}", strict=False))


def netmask_to_prefix(value: str) -> int | None:
    try:
        if value.startswith("0x"):
            integer = int(value, 16)
            mask = ipaddress.IPv4Address(integer)
        else:
            mask = ipaddress.IPv4Address(value)
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    except (ipaddress.AddressValueError, ValueError):
        return None


def warn_unsafe(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)
    print(
        "This plaintext transfer can expose data outside your LAN. Sleeping 5 seconds...",
        file=sys.stderr,
    )
    time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
