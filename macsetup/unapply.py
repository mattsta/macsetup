from __future__ import annotations

import contextlib
import datetime as _dt
import json
import shlex
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .files import (
    atomic_write,
    backup_file_for_context,
    expand_user,
    remove_block,
)
from .homebrew_resolver import resolver_from_homebrew_prefix
from .macos_defaults import (
    defaults_delete_command,
    defaults_resource_name,
    defaults_write_command,
    read_default,
    values_match,
)
from .model import (
    CommandResult,
    CommandSpec,
    FileChange,
    SetupContext,
    Step,
    StepResult,
    StepStatus,
)
from .ownership import (
    UserIdentity,
    context_state_owner,
    prepare_backup_root,
    repair_backup_root,
)
from .recipes import BrewPackage
from .steps import (
    XCODE_SELECT_PATH,
    BrewPackagesStep,
    DnsmasqConfigStep,
    DnsmasqMainConfigStep,
    GitConfigStep,
    GitLogPagerStep,
    HomebrewAnalyticsStep,
    HomebrewInstallStep,
    HomebrewPhantomJsCleanupStep,
    HomebrewSharePermissionsStep,
    MacDefaultsStep,
    ManagedBlockStep,
    ManagedDirectoryStep,
    ManagedFileStep,
    ManualAppStep,
    ManualNoteStep,
    ManualStep,
    MrsyncLauncherStep,
    NpmGlobalPackagesStep,
    OhMyZshStep,
    PrivilegedCommandStep,
    PyenvPythonStep,
    PythonPackagesStep,
    SourceBuildStep,
    SudoTouchIdStep,
    XcodeDeveloperDirectoryStep,
    XcodeLicenseStep,
    XcodeMetalToolchainStep,
    ZshCompletionPermissionsStep,
)

JSONMap = Mapping[str, Any]
CASK_UNINSTALL_TIMEOUT_SECONDS = 300.0
SUDO_PREFLIGHT_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class JournalResult:
    step_id: str
    title: str
    status: str
    detail: str
    commands: tuple[JSONMap, ...]
    changes: tuple[JSONMap, ...]


@dataclass(frozen=True)
class RunJournal:
    path: Path
    created_at: str
    operation: str
    results: tuple[JournalResult, ...]

    @property
    def applied_step_ids(self) -> frozenset[str]:
        return frozenset(
            result.step_id
            for result in self.results
            if result.status == StepStatus.APPLIED.value
        )

    def result_for(self, step_id: str) -> JournalResult | None:
        for result in self.results:
            if result.step_id == step_id and result.status == StepStatus.APPLIED.value:
                return result
        return None


@dataclass(frozen=True)
class UnapplyCandidate:
    step: Step
    journal_result: JournalResult | None


def load_run_journal(root: Path, spec: str) -> RunJournal:
    runs = root / "runs"
    if spec == "latest":
        candidates = sorted(runs.glob("*.json"))
        if not candidates:
            raise FileNotFoundError(f"no run journals found under {runs}")
        path = candidates[-1]
    else:
        path = Path(spec).expanduser()
        if not path.is_absolute():
            path = runs / spec
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = tuple(
        JournalResult(
            step_id=str(item.get("step_id", "")),
            title=str(item.get("title", "")),
            status=str(item.get("status", "")),
            detail=str(item.get("detail", "")),
            commands=tuple(
                command
                for command in item.get("commands", ())
                if isinstance(command, Mapping)
            ),
            changes=tuple(
                change
                for change in item.get("changes", ())
                if isinstance(change, Mapping)
            ),
        )
        for item in payload.get("results", ())
        if isinstance(item, Mapping)
    )
    return RunJournal(
        path=path,
        created_at=str(payload.get("created_at", "")),
        operation=str(payload.get("operation", "apply")),
        results=results,
    )


def candidate_steps_for_unapply(
    steps: Sequence[Step],
    *,
    force: bool,
    journal: RunJournal | None,
) -> tuple[Step, ...]:
    ordered = tuple(reversed(steps))
    if force:
        return ordered
    if journal is None:
        return ()
    applied = journal.applied_step_ids
    return tuple(step for step in ordered if step.id in applied)


def build_unapply_plan(
    candidates: Sequence[Step],
    context: SetupContext,
    *,
    force: bool,
    journal: RunJournal | None,
) -> tuple[UnapplyCandidate, ...]:
    planned: list[UnapplyCandidate | None] = []
    formula_index: int | None = None
    formula_packages: list[str] = []
    formula_tags: set[str] = set()
    for step in candidates:
        journal_result = journal.result_for(step.id) if journal is not None else None
        if isinstance(step, BrewPackagesStep) and not step.cask:
            formula_tags.update(step.tags)
            formula_packages.extend(
                _brew_packages_for_unapply(
                    step, force=force, journal_result=journal_result
                )
            )
            if formula_index is None:
                formula_index = len(planned)
                planned.append(None)
            continue
        planned.append(UnapplyCandidate(step=step, journal_result=journal_result))

    if formula_index is not None:
        selected = tuple(dict.fromkeys(formula_packages))
        if selected:
            ordered = resolver_from_homebrew_prefix(
                context.brew_prefix()
            ).uninstall_order(selected)
            context.runner.state(
                "brew.uninstall.order", "formula " + ", ".join(ordered)
            )
            tags = frozenset(formula_tags or {"packages"})
            formula_step = BrewPackagesStep(
                id="brew.formulas.selected",
                title="Uninstall selected Homebrew formulas",
                packages=tuple(
                    BrewPackage(name, tags=tuple(sorted(tags))) for name in ordered
                ),
                cask=False,
                tags=tags,
            )
            formula_journal = JournalResult(
                step_id=formula_step.id,
                title=formula_step.title,
                status=StepStatus.APPLIED.value,
                detail="",
                commands=(),
                changes=tuple(
                    {"type": "brew_package", "name": name, "cask": False}
                    for name in ordered
                ),
            )
            planned[formula_index] = UnapplyCandidate(
                step=formula_step, journal_result=formula_journal
            )
        else:
            del planned[formula_index]
    return tuple(candidate for candidate in planned if candidate is not None)


def unapply_step(
    step: Step,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    context.runner.state("step.unapply.start", f"{step.id} {step.title}")
    result = _unapply_step(step, context, force=force, journal_result=journal_result)
    context.runner.state(
        "step.unapply.done", f"{step.id} {result.status.value}: {result.detail}"
    )
    return result


def _unapply_step(
    step: Step,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    if isinstance(step, ManagedBlockStep):
        return _unapply_managed_block(step, context)
    if isinstance(step, ManagedDirectoryStep):
        return _unapply_managed_directory(
            step, context, force=force, journal_result=journal_result
        )
    if isinstance(step, (ManagedFileStep, MrsyncLauncherStep)):
        return _unapply_file_change(
            step.id, step.title, step._change(context), context, force=force
        )
    if isinstance(step, GitLogPagerStep):
        return _unapply_git_log_pager(step, context)
    if isinstance(step, DnsmasqMainConfigStep):
        change = step._change(context)
        if change is None:
            return StepResult(
                step.id, step.title, StepStatus.BLOCKED, "brew prefix is unavailable"
            )
        return _unapply_block_change(
            step.id, step.title, change.path, "dnsmasq-conf-dir", "#", context
        )
    if isinstance(step, DnsmasqConfigStep):
        change = step._change(context)
        if change is None:
            return StepResult(
                step.id, step.title, StepStatus.BLOCKED, "brew prefix is unavailable"
            )
        return _unapply_file_change(step.id, step.title, change, context, force=force)
    if isinstance(step, BrewPackagesStep):
        return _unapply_brew_packages(
            step, context, force=force, journal_result=journal_result
        )
    if isinstance(step, GitConfigStep):
        return _unapply_git_config(
            step, context, force=force, journal_result=journal_result
        )
    if isinstance(step, MacDefaultsStep):
        return _unapply_macos_default(
            step, context, force=force, journal_result=journal_result
        )
    if isinstance(step, HomebrewAnalyticsStep):
        return _unapply_homebrew_analytics(
            step, context, force=force, journal_result=journal_result
        )
    if isinstance(step, HomebrewSharePermissionsStep):
        return _unapply_homebrew_share_permissions(
            step, context, force=force, journal_result=journal_result
        )
    if isinstance(step, HomebrewPhantomJsCleanupStep):
        return StepResult(
            step.id,
            step.title,
            StepStatus.MANUAL,
            "cleanup removed legacy files and is not reversible without an archive",
        )
    if isinstance(step, SudoTouchIdStep):
        return _unapply_sudo_touch_id(step, context)
    if isinstance(step, XcodeDeveloperDirectoryStep):
        return _unapply_xcode_developer_directory(
            step, context, force=force, journal_result=journal_result
        )
    if isinstance(step, XcodeMetalToolchainStep):
        return StepResult(
            step.id,
            step.title,
            StepStatus.MANUAL,
            "Xcode component downloads are not automatically removed",
        )
    if isinstance(step, OhMyZshStep):
        return _unapply_oh_my_zsh(step, context)
    if isinstance(step, PyenvPythonStep):
        return _unapply_pyenv_python(
            step, context, force=force, journal_result=journal_result
        )
    if isinstance(step, PythonPackagesStep):
        return _unapply_python_packages(
            step, context, force=force, journal_result=journal_result
        )
    if isinstance(step, NpmGlobalPackagesStep):
        return _unapply_npm_packages(
            step, context, force=force, journal_result=journal_result
        )
    if isinstance(step, SourceBuildStep):
        return _unapply_source_build(
            step, context, force=force, journal_result=journal_result
        )
    if isinstance(step, ZshCompletionPermissionsStep):
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            "recursive zsh permission tightening has no safe blind inverse",
        )
    if isinstance(step, HomebrewInstallStep):
        return StepResult(
            step.id,
            step.title,
            StepStatus.MANUAL,
            "Homebrew bootstrap is not automatically uninstalled",
        )
    if isinstance(step, XcodeLicenseStep):
        return StepResult(
            step.id,
            step.title,
            StepStatus.MANUAL,
            "Xcode license acceptance is not reversible by macsetup",
        )
    if isinstance(step, PrivilegedCommandStep):
        return StepResult(
            step.id,
            step.title,
            StepStatus.MANUAL,
            "one-shot privileged command has no inverse",
        )
    if isinstance(step, (ManualStep, ManualAppStep, ManualNoteStep)):
        return StepResult(
            step.id,
            step.title,
            StepStatus.MANUAL,
            "manual step must be undone manually",
        )
    return StepResult(
        step.id,
        step.title,
        StepStatus.BLOCKED,
        "step type does not expose an unapply inverse",
    )


def _unapply_git_log_pager(step: GitLogPagerStep, context: SetupContext) -> StepResult:
    # The step owns both the compiled binary and the deployed C source; remove
    # both (with backups). Both being absent is a clean no-op.
    targets = [
        expand_user(step.binary_path, context.home),
        expand_user(step.source_path, context.home),
    ]
    present = [p for p in targets if p.exists()]
    if not present:
        return StepResult(
            step.id, step.title, StepStatus.PRESENT, "git-log-pager is absent"
        )
    if context.dry_run:
        joined = ", ".join(str(p) for p in present)
        return StepResult(
            step.id, step.title, StepStatus.APPLIED, f"dry-run would remove {joined}"
        )
    removed: list[str] = []
    for path in present:
        backup = _delete_path_with_backup(path, context)
        entry = str(path)
        if backup is not None:
            entry += f" (backup: {backup})"
        removed.append(entry)
    return StepResult(
        step.id, step.title, StepStatus.APPLIED, "removed " + ", ".join(removed)
    )


def _unapply_managed_block(step: ManagedBlockStep, context: SetupContext) -> StepResult:
    target = expand_user(step.path, context.home)
    return _unapply_block_change(
        step.id, step.title, target, step.marker, step.comment_prefix, context
    )


def _unapply_block_change(
    step_id: str,
    title: str,
    path: Path,
    marker: str,
    comment_prefix: str,
    context: SetupContext,
) -> StepResult:
    if not path.exists():
        return StepResult(step_id, title, StepStatus.PRESENT, f"{path} is absent")
    before = path.read_text(encoding="utf-8")
    after = remove_block(before, name=marker, comment_prefix=comment_prefix)
    if before == after:
        return StepResult(
            step_id,
            title,
            StepStatus.PRESENT,
            f"{path} does not contain macsetup:{marker}",
        )
    if context.dry_run:
        return StepResult(
            step_id,
            title,
            StepStatus.APPLIED,
            f"dry-run would remove macsetup:{marker} from {path}",
        )
    backup = backup_file_for_context(path, context)
    atomic_write(path, after)
    detail = f"removed macsetup:{marker} from {path}"
    if backup is not None:
        detail += f"; backup: {backup}"
    return StepResult(step_id, title, StepStatus.APPLIED, detail)


def _unapply_file_change(
    step_id: str,
    title: str,
    change: FileChange,
    context: SetupContext,
    *,
    force: bool,
) -> StepResult:
    path = change.path
    if not path.exists():
        return StepResult(step_id, title, StepStatus.PRESENT, f"{path} is absent")
    if not force and path.read_text(encoding="utf-8") != change.after:
        return StepResult(
            step_id,
            title,
            StepStatus.BLOCKED,
            f"{path} differs from managed content; rerun with --force to remove it anyway",
        )
    if context.dry_run:
        return StepResult(
            step_id, title, StepStatus.APPLIED, f"dry-run would remove {path}"
        )
    backup = _delete_path_with_backup(path, context)
    detail = f"removed {path}"
    if backup is not None:
        detail += f"; backup: {backup}"
    return StepResult(step_id, title, StepStatus.APPLIED, detail)


def _unapply_managed_directory(
    step: ManagedDirectoryStep,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    change = _first_change(journal_result, "managed_directory")
    if not force and not change:
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            "journal lacks directory creation state; rerun with --force to remove it if empty",
        )
    existed_before = bool(change.get("existed_before")) if change else False
    if existed_before and not force:
        return StepResult(
            step.id,
            step.title,
            StepStatus.PRESENT,
            "directory existed before apply",
        )
    path = (
        Path(str(change.get("path")))
        if change and change.get("path")
        else expand_user(step.path, context.home)
    )
    if not path.exists():
        return StepResult(step.id, step.title, StepStatus.PRESENT, f"{path} is absent")
    if not path.is_dir():
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            f"{path} exists but is not a directory",
        )
    try:
        has_entries = any(path.iterdir())
    except OSError as error:
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            f"could not inspect {path}: {error}",
        )
    if has_entries:
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            f"{path} is not empty; leaving user content in place",
        )
    if context.dry_run:
        return StepResult(
            step.id,
            step.title,
            StepStatus.APPLIED,
            f"dry-run would remove empty directory {path}",
        )
    path.rmdir()
    return StepResult(
        step.id,
        step.title,
        StepStatus.APPLIED,
        f"removed empty directory {path}",
    )


def _unapply_brew_packages(
    step: BrewPackagesStep,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    if not context.command_exists("brew"):
        return StepResult(
            step.id, step.title, StepStatus.BLOCKED, "brew is not available"
        )
    packages = _brew_packages_for_unapply(
        step, force=force, journal_result=journal_result
    )
    if not packages:
        detail = (
            "no recorded package installs; rerun with --force to uninstall profile packages"
            if not force
            else "no packages selected"
        )
        return StepResult(
            step.id,
            step.title,
            StepStatus.PRESENT if force else StepStatus.BLOCKED,
            detail,
        )
    installed = step._installed_from_homebrew_dirs(context) or set()
    targets = [package for package in packages if package in installed]
    if targets and not step.cask:
        resolver = resolver_from_homebrew_prefix(context.brew_prefix())
        targets = list(resolver.uninstall_order(targets))
        context.runner.state("brew.uninstall.order", "formula " + ", ".join(targets))
        blockers = resolver.blocking_dependents(targets, tuple(sorted(installed)))
        if blockers:
            return StepResult(
                step.id,
                step.title,
                StepStatus.BLOCKED,
                _format_formula_blockers(blockers),
            )
    if not targets:
        return StepResult(
            step.id,
            step.title,
            StepStatus.PRESENT,
            "selected packages are already absent",
        )
    if step.cask and not context.dry_run and not context.allow_privileged:
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            "cask uninstall may need sudo or app/service cleanup; rerun with --allow-privileged",
        )
    results: list[CommandResult] = []
    if step.cask and not context.dry_run:
        context.runner.state(
            "brew.uninstall.auth", "validating sudo before cask uninstall"
        )
        auth = context.runner.sudo_validate(
            timeout_seconds=SUDO_PREFLIGHT_TIMEOUT_SECONDS
        )
        results.append(auth)
        if not auth.ok:
            detail = (
                auth.stderr.strip() or "sudo validation failed before cask uninstall"
            )
            return StepResult(
                step.id, step.title, StepStatus.FAILED, detail, tuple(results)
            )
    for package in targets:
        argv = (
            ("brew", "uninstall", "--cask", "--force", package)
            if step.cask
            else ("brew", "uninstall", package)
        )
        context.runner.state("brew.uninstall.start", package)
        timeout = CASK_UNINSTALL_TIMEOUT_SECONDS if step.cask else None
        result = context.runner.run(
            CommandSpec(argv=argv), check=False, timeout_seconds=timeout
        )
        context.runner.state(
            "brew.uninstall.done", f"{package} exit={result.returncode}"
        )
        results.append(result)
        if not result.ok:
            if result.timed_out:
                detail = f"timed out uninstalling {package}; quit the app/VPN helper if it is still running, then retry with --allow-privileged"
                return StepResult(
                    step.id, step.title, StepStatus.FAILED, detail, tuple(results)
                )
            return StepResult(
                step.id,
                step.title,
                StepStatus.FAILED,
                result.stderr.strip()
                or result.stdout.strip()
                or f"failed uninstalling {package}",
                tuple(results),
            )
    action = "dry-run would uninstall" if context.dry_run else "uninstalled"
    return StepResult(
        step.id,
        step.title,
        StepStatus.APPLIED,
        f"{action}: " + ", ".join(targets),
        tuple(results),
    )


def _brew_packages_for_unapply(
    step: BrewPackagesStep,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> tuple[str, ...]:
    if force:
        return tuple(package.name for package in step.packages)
    if journal_result is None:
        return ()
    from_changes = [
        str(change["name"])
        for change in journal_result.changes
        if change.get("type") == "brew_package"
        and bool(change.get("cask")) == step.cask
        and change.get("name")
    ]
    if from_changes:
        return tuple(dict.fromkeys(from_changes))
    packages: list[str] = []
    for command in journal_result.commands:
        rendered = command.get("command")
        if isinstance(rendered, str):
            packages.extend(_parse_brew_install_command(rendered, cask=step.cask))
    return tuple(dict.fromkeys(packages))


def _format_formula_blockers(blockers: Mapping[str, tuple[str, ...]]) -> str:
    parts = [
        f"{formula} required by {', '.join(dependents)}"
        for formula, dependents in sorted(blockers.items())
    ]
    return (
        "formula uninstall blocked by installed dependents outside this unapply set: "
        + "; ".join(parts)
    )


def _parse_brew_install_command(rendered: str, *, cask: bool) -> tuple[str, ...]:
    try:
        parts = shlex.split(rendered)
    except ValueError:
        return ()
    if len(parts) < 3 or Path(parts[0]).name != "brew" or parts[1] != "install":
        return ()
    is_cask = "--cask" in parts
    if is_cask != cask:
        return ()
    packages = []
    for part in parts[2:]:
        if part.startswith("-"):
            continue
        packages.append(part)
    return tuple(packages)


def _unapply_git_config(
    step: GitConfigStep,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    if not context.command_exists("git"):
        return StepResult(
            step.id, step.title, StepStatus.BLOCKED, "git is not available"
        )
    change = _first_change(journal_result, "git_config")
    if change and not force:
        previous_values = _git_previous_values(change)
        current_values = step._current_values(context)
        if current_values == previous_values:
            return StepResult(
                step.id,
                step.title,
                StepStatus.PRESENT,
                "previous git config is already restored",
            )
        commands: list[CommandSpec] = []
        if current_values:
            commands.append(
                CommandSpec(
                    argv=("git", "config", "--global", "--unset-all", step.setting.key)
                )
            )
        commands.extend(
            CommandSpec(
                argv=("git", "config", "--global", "--add", step.setting.key, value)
            )
            for value in previous_values
        )
        if not commands:
            return StepResult(
                step.id,
                step.title,
                StepStatus.PRESENT,
                f"git {step.setting.key} is already unset",
            )
        results: list[CommandResult] = []
        for command in commands:
            result = context.runner.run(command, check=False)
            results.append(result)
            if not result.ok:
                return StepResult(
                    step.id,
                    step.title,
                    StepStatus.FAILED,
                    result.stderr.strip() or result.stdout.strip(),
                    tuple(results),
                )
        detail = (
            "dry-run would restore previous git config"
            if context.dry_run
            else "restored previous git config"
        )
        return StepResult(
            step.id,
            step.title,
            StepStatus.APPLIED,
            detail,
            tuple(results),
        )
    if not force:
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            "journal lacks previous git value; rerun with --force to unset it",
        )
    if not step._current_values(context):
        return StepResult(
            step.id,
            step.title,
            StepStatus.PRESENT,
            f"git {step.setting.key} is already unset",
        )
    result = context.runner.run(
        CommandSpec(
            argv=("git", "config", "--global", "--unset-all", step.setting.key)
        ),
        check=False,
    )
    status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
    detail = (
        f"dry-run would unset git {step.setting.key}"
        if context.dry_run
        else f"unset git {step.setting.key}"
    )
    return StepResult(
        step.id,
        step.title,
        status,
        detail if result.ok else result.stderr.strip(),
        (result,),
    )


def _git_previous_values(change: JSONMap) -> tuple[str, ...]:
    previous_values = change.get("previous_values")
    if isinstance(previous_values, Sequence) and not isinstance(
        previous_values, (str, bytes)
    ):
        return tuple(str(value) for value in previous_values)
    if bool(change.get("had_value")):
        return (str(change.get("previous", "")),)
    return ()


def _unapply_macos_default(
    step: MacDefaultsStep,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    if not context.command_exists("defaults"):
        return StepResult(
            step.id, step.title, StepStatus.BLOCKED, "defaults command is not available"
        )
    change = _first_change(journal_result, "macos_default")
    if not force and not change:
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            "journal lacks previous macOS default value; rerun with --force to delete the managed key",
        )
    had_value = bool(change.get("had_value")) if change else False
    previous = str(change.get("previous", "")) if change else ""
    current, _ = read_default(context, step.setting)
    results: list[CommandResult] = []
    if had_value and not force:
        if current.had_value and values_match(
            step.setting, current.value, previous, context.home
        ):
            return StepResult(
                step.id,
                step.title,
                StepStatus.PRESENT,
                f"{defaults_resource_name(step.setting)} is already restored",
            )
        result = context.runner.run(
            defaults_write_command(step.setting, previous, context.home), check=False
        )
        results.append(result)
        action = f"restore {defaults_resource_name(step.setting)} to previous value"
    else:
        if not current.had_value:
            return StepResult(
                step.id,
                step.title,
                StepStatus.PRESENT,
                f"{defaults_resource_name(step.setting)} is already unset",
            )
        result = context.runner.run(defaults_delete_command(step.setting), check=False)
        results.append(result)
        action = f"delete {defaults_resource_name(step.setting)}"
    if not result.ok:
        return StepResult(
            step.id,
            step.title,
            StepStatus.FAILED,
            result.stderr.strip() or result.stdout.strip() or f"failed to {action}",
            tuple(results),
        )
    if action.startswith("restore "):
        detail = (
            "dry-run would restore previous macOS default"
            if context.dry_run
            else "restored previous macOS default"
        )
        after = previous
    else:
        detail = (
            f"dry-run would {action}"
            if context.dry_run
            else f"deleted {defaults_resource_name(step.setting)}"
        )
        after = ""
    return StepResult(
        step.id,
        step.title,
        StepStatus.APPLIED,
        detail,
        tuple(results),
        changes=(
            {
                "type": "macos_default",
                "name": step.setting.name,
                "domain": step.setting.domain,
                "key": step.setting.key,
                "value_type": step.setting.value_type,
                "previous": current.value,
                "had_value": current.had_value,
                "after": after,
            },
        ),
    )


def _unapply_homebrew_analytics(
    step: HomebrewAnalyticsStep,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    change = _first_change(journal_result, "homebrew_analytics")
    previous = str(change.get("previous", "")) if change else ""
    if not force and previous not in {"enabled", "disabled"}:
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            "journal lacks previous analytics state; rerun with --force to enable analytics",
        )
    if previous == "disabled" and not force:
        return StepResult(
            step.id,
            step.title,
            StepStatus.PRESENT,
            "Homebrew analytics were already disabled before apply",
        )
    result = context.runner.run(
        CommandSpec(
            argv=("brew", "analytics", "on"), env={"HOMEBREW_NO_AUTO_UPDATE": "1"}
        ),
        check=False,
    )
    status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
    detail = (
        "dry-run would enable Homebrew analytics"
        if context.dry_run
        else "Homebrew analytics enabled"
    )
    return StepResult(
        step.id,
        step.title,
        status,
        detail if result.ok else result.stderr.strip(),
        (result,),
    )


def _unapply_homebrew_share_permissions(
    step: HomebrewSharePermissionsStep,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    change = _first_change(journal_result, "chmod")
    previous_mode = change.get("previous_mode") if change else None
    if previous_mode is None:
        detail = (
            "journal lacks previous mode; permission changes are not blindly inverted"
        )
        if force:
            detail += " even with --force"
        return StepResult(step.id, step.title, StepStatus.BLOCKED, detail)
    prefix = context.brew_prefix()
    if prefix is None:
        return StepResult(
            step.id, step.title, StepStatus.BLOCKED, "brew prefix is unavailable"
        )
    path = prefix / "share"
    result = context.runner.run(
        CommandSpec(argv=("chmod", f"{int(previous_mode):o}", str(path))), check=False
    )
    status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
    detail = (
        f"dry-run would restore {path} to mode {int(previous_mode):o}"
        if context.dry_run
        else f"restored {path} to mode {int(previous_mode):o}"
    )
    return StepResult(
        step.id,
        step.title,
        status,
        detail if result.ok else result.stderr.strip(),
        (result,),
    )


def _unapply_sudo_touch_id(step: SudoTouchIdStep, context: SetupContext) -> StepResult:
    path = Path("/etc/pam.d/sudo_local")
    if not path.exists():
        return StepResult(
            step.id, step.title, StepStatus.PRESENT, "/etc/pam.d/sudo_local is absent"
        )
    existing = path.read_text(encoding="utf-8")
    desired = _disable_pam_tid(existing)
    if existing == desired:
        return StepResult(
            step.id,
            step.title,
            StepStatus.PRESENT,
            "Touch ID sudo line is already disabled",
        )
    if not context.allow_privileged:
        return StepResult(
            step.id, step.title, StepStatus.BLOCKED, "requires --allow-privileged"
        )
    if context.dry_run:
        result = context.runner.run(
            CommandSpec(
                argv=(
                    "sudo",
                    "install",
                    "-o",
                    "root",
                    "-g",
                    "wheel",
                    "-m",
                    "0444",
                    "<generated-sudo_local>",
                    str(path),
                )
            ),
            check=False,
        )
        return StepResult(
            step.id,
            step.title,
            StepStatus.APPLIED,
            "dry-run would disable Touch ID for sudo",
            (result,),
        )
    results: list[CommandResult] = []
    auth = context.runner.sudo_validate(timeout_seconds=SUDO_PREFLIGHT_TIMEOUT_SECONDS)
    results.append(auth)
    if not auth.ok:
        detail = (
            auth.stderr.strip()
            or auth.stdout.strip()
            or "sudo credential validation failed"
        )
        return StepResult(
            step.id, step.title, StepStatus.FAILED, detail, tuple(results)
        )
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False
        ) as temporary:
            temporary.write(desired)
            temporary_name = temporary.name
        result = context.runner.run(
            CommandSpec(
                argv=(
                    "sudo",
                    "install",
                    "-o",
                    "root",
                    "-g",
                    "wheel",
                    "-m",
                    "0444",
                    temporary_name,
                    str(path),
                )
            ),
            check=False,
        )
        results.append(result)
    finally:
        if temporary_name:
            with contextlib.suppress(FileNotFoundError):
                Path(temporary_name).unlink()
    status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
    return StepResult(
        step.id,
        step.title,
        status,
        "disabled Touch ID for sudo" if result.ok else result.stderr.strip(),
        tuple(results),
    )


def _unapply_xcode_developer_directory(
    step: XcodeDeveloperDirectoryStep,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    change = _first_change(journal_result, "xcode_developer_dir")
    previous = str(change.get("previous", "")) if change else ""
    had_value = bool(change.get("had_value")) if change else False
    if not had_value or not previous:
        detail = "journal lacks previous Xcode developer directory"
        if force:
            detail += "; refusing to guess a replacement developer directory"
        else:
            detail += "; rerun with a journal containing xcode_developer_dir state"
        return StepResult(step.id, step.title, StepStatus.BLOCKED, detail)
    current = step._selected_developer_dir(context)
    if current.ok and current.stdout.strip() == previous:
        return StepResult(
            step.id,
            step.title,
            StepStatus.PRESENT,
            f"active developer directory is already {previous}",
        )
    if not context.allow_privileged:
        return StepResult(
            step.id, step.title, StepStatus.BLOCKED, "requires --allow-privileged"
        )
    results: list[CommandResult] = []
    if not context.dry_run:
        auth = context.runner.sudo_validate(
            timeout_seconds=SUDO_PREFLIGHT_TIMEOUT_SECONDS
        )
        results.append(auth)
        if not auth.ok:
            detail = (
                auth.stderr.strip()
                or auth.stdout.strip()
                or "sudo credential validation failed"
            )
            return StepResult(
                step.id, step.title, StepStatus.FAILED, detail, tuple(results)
            )
    result = context.runner.run(
        CommandSpec(argv=("sudo", str(XCODE_SELECT_PATH), "-s", previous)), check=False
    )
    results.append(result)
    status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
    detail = (
        f"dry-run would restore Xcode developer directory to {previous}"
        if context.dry_run
        else f"restored Xcode developer directory to {previous}"
    )
    return StepResult(
        step.id,
        step.title,
        status,
        detail
        if result.ok
        else result.stderr.strip()
        or result.stdout.strip()
        or "failed restoring Xcode developer directory",
        tuple(results),
    )


def _disable_pam_tid(existing: str) -> str:
    lines = []
    for line in existing.splitlines():
        stripped = line.lstrip()
        if "pam_tid.so" in stripped and not stripped.startswith("#"):
            indent = line[: len(line) - len(stripped)]
            lines.append(f"{indent}# {stripped}")
        else:
            lines.append(line)
    return "\n".join(lines).rstrip("\n") + "\n"


def _unapply_oh_my_zsh(step: OhMyZshStep, context: SetupContext) -> StepResult:
    path = context.home / ".oh-my-zsh"
    if not path.exists():
        return StepResult(step.id, step.title, StepStatus.PRESENT, f"{path} is absent")
    if context.dry_run:
        return StepResult(
            step.id, step.title, StepStatus.APPLIED, f"dry-run would remove {path}"
        )
    backup = _delete_path_with_backup(path, context)
    return StepResult(
        step.id, step.title, StepStatus.APPLIED, f"removed {path}; backup: {backup}"
    )


def _unapply_pyenv_python(
    step: PyenvPythonStep,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    if not context.command_exists("pyenv"):
        return StepResult(
            step.id, step.title, StepStatus.BLOCKED, "pyenv is not available"
        )
    change = _first_change(journal_result, "pyenv_python")
    if not force and not change:
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            "journal lacks pyenv previous state; rerun with --force to uninstall the configured version",
        )
    versions = context.runner.run(
        CommandSpec(argv=("pyenv", "versions", "--bare")), capture=True, dry_run=False
    )
    installed = versions.ok and step.version in {
        line.strip() for line in versions.stdout.splitlines()
    }
    global_version = context.runner.run(
        CommandSpec(argv=("pyenv", "global")), capture=True, dry_run=False
    )
    selected = global_version.ok and global_version.stdout.strip().splitlines()[
        0:1
    ] == [step.version]
    commands: list[CommandSpec] = []
    selected_global_by_apply = (
        bool(change.get("selected_global", True)) if change else step.select_global
    )
    if selected and selected_global_by_apply:
        previous_global = (
            str(change.get("previous_global", "system")) if change else "system"
        )
        commands.append(
            CommandSpec(argv=("pyenv", "global", previous_global or "system"))
        )
    if installed and (force or not bool(change and change.get("installed_before"))):
        commands.append(CommandSpec(argv=("pyenv", "uninstall", "-f", step.version)))
    if commands:
        commands.append(CommandSpec(argv=("pyenv", "rehash")))
    if not commands:
        return StepResult(
            step.id,
            step.title,
            StepStatus.PRESENT,
            f"pyenv {step.version} is already unapplied",
        )
    results: list[CommandResult] = []
    for command in commands:
        result = context.runner.run(command, check=False)
        results.append(result)
        if not result.ok:
            return StepResult(
                step.id,
                step.title,
                StepStatus.FAILED,
                result.stderr.strip() or result.stdout.strip(),
                tuple(results),
            )
    detail = (
        f"dry-run would unapply pyenv {step.version}"
        if context.dry_run
        else f"unapplied pyenv {step.version}"
    )
    return StepResult(step.id, step.title, StepStatus.APPLIED, detail, tuple(results))


def _unapply_python_packages(
    step: PythonPackagesStep,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    packages = _package_names_from_changes(journal_result, "python_package")
    if force:
        packages = tuple(step.packages)
    if not packages:
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            "journal lacks installed Python package list; rerun with --force to uninstall profile packages",
        )
    if not context.command_exists("pyenv"):
        return StepResult(
            step.id, step.title, StepStatus.BLOCKED, "pyenv is not available"
        )
    result = context.runner.run(
        CommandSpec(
            argv=("pyenv", "exec", "python", "-m", "pip", "uninstall", "-y", *packages)
        ),
        check=False,
    )
    status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
    detail = (
        "dry-run would uninstall Python packages"
        if context.dry_run
        else "Python packages uninstalled"
    )
    return StepResult(
        step.id,
        step.title,
        status,
        detail if result.ok else result.stderr.strip(),
        (result,),
    )


def _unapply_npm_packages(
    step: NpmGlobalPackagesStep,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    packages = _package_names_from_changes(journal_result, "npm_package")
    if force:
        packages = tuple(step.packages)
    if not packages:
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            "journal lacks installed npm package list; rerun with --force to uninstall profile packages",
        )
    if not context.command_exists("npm"):
        return StepResult(
            step.id, step.title, StepStatus.BLOCKED, "npm is not available"
        )
    result = context.runner.run(
        CommandSpec(argv=("npm", "uninstall", "-g", *packages)), check=False
    )
    status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
    detail = (
        "dry-run would uninstall npm packages"
        if context.dry_run
        else "npm packages uninstalled"
    )
    return StepResult(
        step.id,
        step.title,
        status,
        detail if result.ok else result.stderr.strip(),
        (result,),
    )


def _unapply_source_build(
    step: SourceBuildStep,
    context: SetupContext,
    *,
    force: bool,
    journal_result: JournalResult | None,
) -> StepResult:
    if step.package.install_mode != "copy":
        return StepResult(
            step.id,
            step.title,
            StepStatus.MANUAL,
            "path-mode source builds are exposed through managed PATH blocks; remove the checkout manually if desired",
        )
    paths = _source_build_paths_from_changes(journal_result)
    if force:
        paths = tuple(step._install_paths(context))
    if not paths:
        return StepResult(
            step.id,
            step.title,
            StepStatus.BLOCKED,
            "journal lacks copied source-build binary path list; rerun with --force to remove current profile binaries",
        )
    existing = tuple(path for path in paths if path.exists())
    if not existing:
        return StepResult(
            step.id, step.title, StepStatus.PRESENT, "source-build binaries are absent"
        )
    if context.dry_run:
        return StepResult(
            step.id,
            step.title,
            StepStatus.APPLIED,
            "dry-run would remove source-build binary path(s): "
            + ", ".join(str(path) for path in existing),
        )
    for path in existing:
        path.unlink()
    return StepResult(
        step.id,
        step.title,
        StepStatus.APPLIED,
        "removed source-build binary path(s): "
        + ", ".join(str(path) for path in existing),
    )


def _source_build_paths_from_changes(
    journal_result: JournalResult | None,
) -> tuple[Path, ...]:
    if journal_result is None:
        return ()
    paths: list[Path] = []
    for change in journal_result.changes:
        if change.get("type") != "source_build":
            continue
        value = change.get("binary_paths")
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            continue
        for item in value:
            if isinstance(item, str):
                paths.append(Path(item))
    return tuple(dict.fromkeys(paths))


def _package_names_from_changes(
    journal_result: JournalResult | None, change_type: str
) -> tuple[str, ...]:
    if journal_result is None:
        return ()
    return tuple(
        dict.fromkeys(
            str(change["name"])
            for change in journal_result.changes
            if change.get("type") == change_type and change.get("name")
        )
    )


def _first_change(
    journal_result: JournalResult | None, change_type: str
) -> JSONMap | None:
    if journal_result is None:
        return None
    for change in journal_result.changes:
        if change.get("type") == change_type:
            return change
    return None


def _delete_path_with_backup(path: Path, context: SetupContext) -> Path | None:
    if not path.exists():
        return None
    backup_root = context.backup_root
    owner = context_state_owner(context)
    if path.is_dir() and not path.is_symlink():
        destination = _backup_destination(path, backup_root)
        try:
            _move_directory_backup(path, destination, backup_root, owner)
        except PermissionError:
            repair_backup_root(backup_root, owner)
            _move_directory_backup(path, destination, backup_root, owner)
        return destination
    backup = backup_file_for_context(path, context)
    path.unlink()
    return backup


def _move_directory_backup(
    path: Path,
    destination: Path,
    backup_root: Path,
    owner: UserIdentity | None,
) -> None:
    prepare_backup_root(backup_root, owner)
    destination.parent.mkdir(parents=True, exist_ok=True)
    repair_backup_root(backup_root, owner)
    shutil.move(str(path), str(destination))
    repair_backup_root(backup_root, owner)


def _backup_destination(path: Path, backup_root: Path) -> Path:
    stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    absolute = path.resolve()
    relative = Path(*absolute.parts[1:]) if absolute.is_absolute() else absolute
    destination = backup_root / stamp / relative
    suffix = 1
    while destination.exists():
        destination = (
            backup_root / stamp / relative.with_name(f"{relative.name}.{suffix}")
        )
        suffix += 1
    return destination
