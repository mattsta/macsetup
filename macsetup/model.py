from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class StepStatus(StrEnum):
    PRESENT = "present"
    NEEDS_CHANGE = "needs-change"
    BLOCKED = "blocked"
    MANUAL = "manual"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"
    APPLIED = "applied"
    FAILED = "failed"


class Risk(StrEnum):
    USER_FILE = "user-file"
    USER_SETTING = "user-setting"
    PACKAGE_INSTALL = "package-install"
    NETWORK = "network"
    PRIVILEGED = "privileged"
    MANUAL = "manual"


@dataclass(frozen=True, order=True)
class ResourceRef:
    kind: str
    name: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}"


def resource(kind: str, name: str) -> ResourceRef:
    return ResourceRef(kind=kind, name=name)


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...] = ()
    shell: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    needs_tty: bool = False

    def __post_init__(self) -> None:
        if bool(self.argv) == bool(self.shell):
            raise ValueError("CommandSpec needs exactly one of argv or shell")


@dataclass(frozen=True)
class CommandResult:
    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    skipped: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class Remediation:
    summary: str
    commands: tuple[str, ...] = ()
    manual_steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepCheck:
    step_id: str
    title: str
    status: StepStatus
    detail: str
    tags: frozenset[str]
    risks: frozenset[Risk]
    remediations: tuple[Remediation, ...] = ()


@dataclass(frozen=True)
class StepResult:
    step_id: str
    title: str
    status: StepStatus
    detail: str
    commands: tuple[CommandResult, ...] = ()
    changes: tuple[Mapping[str, Any], ...] = ()
    remediations: tuple[Remediation, ...] = ()


@dataclass(frozen=True)
class FileChange:
    path: Path
    before: str
    after: str
    existed_before: bool
    mode_before: int | None = None
    mode_after: int | None = None

    @property
    def content_changed(self) -> bool:
        return self.before != self.after

    @property
    def mode_changed(self) -> bool:
        return self.mode_after is not None and self.mode_before != self.mode_after

    @property
    def changed(self) -> bool:
        return self.content_changed or self.mode_changed


class Step(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def tags(self) -> frozenset[str]: ...

    @property
    def risks(self) -> frozenset[Risk]: ...

    @property
    def requires(self) -> frozenset[ResourceRef]: ...

    @property
    def provides(self) -> frozenset[ResourceRef]: ...

    @property
    def owns(self) -> frozenset[ResourceRef]: ...

    def check(self, context: SetupContext) -> StepCheck: ...

    def apply(self, context: SetupContext) -> StepResult: ...


class CommandRunnerProtocol(Protocol):
    def which(self, name: str) -> str | None: ...

    def add_path_dir(self, directory: Path) -> None: ...

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
    ) -> CommandResult: ...

    def state(self, state: str, detail: str) -> None: ...

    def sudo_validate(self, *, timeout_seconds: float = 120.0) -> CommandResult: ...


class SetupContext(Protocol):
    @property
    def home(self) -> Path: ...

    @property
    def repo_root(self) -> Path: ...

    @property
    def backup_root(self) -> Path: ...

    @property
    def runner(self) -> CommandRunnerProtocol: ...

    @property
    def dry_run(self) -> bool: ...

    @property
    def allow_bootstrap(self) -> bool: ...

    @property
    def allow_privileged(self) -> bool: ...

    @property
    def enable_dns_blocklist(self) -> bool: ...

    def command_exists(self, name: str) -> bool: ...

    def brew_prefix(self) -> Path | None: ...
