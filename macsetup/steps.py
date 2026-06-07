from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import content
from .files import (
    atomic_write,
    backup_file_for_context,
    expand_user,
    managed_block,
    text_file_change,
    upsert_block,
)
from .macos_defaults import (
    defaults_resource_name,
    defaults_write_command,
    desired_value,
    read_default,
    values_match,
)
from .model import (
    CommandResult,
    CommandSpec,
    FileChange,
    Remediation,
    ResourceRef,
    Risk,
    SetupContext,
    Step,
    StepCheck,
    StepResult,
    StepStatus,
    resource,
)
from .recipes import (
    DEFAULT_RECIPE,
    BrewPackage,
    GitSetting,
    MacDefaultsSetting,
    ManualApp,
    ManualNote,
    SetupRecipe,
    SourceBuildPackage,
)


def _tags(*values: str) -> frozenset[str]:
    return frozenset(values)


def _risks(*values: Risk) -> frozenset[Risk]:
    return frozenset(values)


NO_RESOURCES: frozenset[ResourceRef] = frozenset()
BREW = resource("tool", "brew")
HOMEBREW_ANALYTICS = resource("setting", "homebrew.analytics")
XCODE_DEVELOPER_DIR = resource("setting", "xcode.developer-dir")
XCODE_LICENSE = resource("setting", "xcode.license")
XCODE_METAL_TOOLCHAIN = resource("component", "xcode.metal-toolchain")
SUDO_TOUCH_ID = resource("setting", "sudo.touch-id")
HOMEBREW_SHARE_PERMISSIONS = resource("permission", "homebrew-share")
HOMEBREW_PHANTOMJS_CLEANUP = resource("cleanup", "homebrew-phantomjs")
SUDO_PREFLIGHT_TIMEOUT_SECONDS = 120.0
XCODE_APP_DEVELOPER_DIR = Path("/Applications/Xcode.app/Contents/Developer")
XCODE_SELECT_PATH = Path("/usr/bin/xcode-select")
XCODEBUILD_PATH = Path("/usr/bin/xcodebuild")
XCRUN_PATH = Path("/usr/bin/xcrun")


def brew_formula(name: str) -> ResourceRef:
    return resource("brew-formula", name)


def brew_cask(name: str) -> ResourceRef:
    return resource("brew-cask", name)


def file_block(path: str, marker: str) -> ResourceRef:
    return resource("file-block", f"{path}#{marker}")


def file_resource(path: str) -> ResourceRef:
    return resource("file", path)


def directory_resource(path: str) -> ResourceRef:
    return resource("directory", path)


def git_config_resource(key: str) -> ResourceRef:
    return resource("git-config", key)


def macos_default_resource(setting: MacDefaultsSetting) -> ResourceRef:
    return resource("macos-default", defaults_resource_name(setting))


def python_version_resource(version: str) -> ResourceRef:
    return resource("pyenv-python", version)


def python_package_resource(name: str) -> ResourceRef:
    return resource("python-package", name.lower())


def npm_package_resource(name: str) -> ResourceRef:
    return resource("npm-package", name)


def source_build_resource(name: str) -> ResourceRef:
    return resource("source-build", name)


def source_build_binary_resource(name: str, binary: str) -> ResourceRef:
    return resource("source-build-binary", f"{name}:{binary}")


PYENV_BUILD_REQUIREMENTS = frozenset(
    {
        brew_formula("pyenv"),
        brew_formula("openssl@3"),
        brew_formula("readline"),
        brew_formula("sqlite"),
        brew_formula("xz"),
        brew_formula("bzip2"),
        brew_formula("libffi"),
        brew_formula("pkg-config"),
        brew_formula("tcl-tk"),
        brew_formula("zlib"),
    }
)


def homebrew_analytics_off_command() -> CommandSpec:
    return CommandSpec(
        shell=(
            "if command -v brew >/dev/null 2>&1; then brew analytics off; "
            "elif [[ -x /opt/homebrew/bin/brew ]]; then /opt/homebrew/bin/brew analytics off; "
            "elif [[ -x /usr/local/bin/brew ]]; then /usr/local/bin/brew analytics off; "
            "else exit 127; fi"
        ),
        env={"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ANALYTICS": "1"},
    )


def _file_change_record(
    change_type: str, change: FileChange, **extra: object
) -> dict[str, object]:
    return {
        "type": change_type,
        "path": str(change.path),
        "before": change.before,
        "after": change.after,
        "existed_before": change.existed_before,
        "mode_before": change.mode_before,
        "mode_after": change.mode_after,
        **extra,
    }


@dataclass(frozen=True)
class SudoTouchIdProbe:
    status: StepStatus
    detail: str
    desired: str = ""


def _sudo_touch_id_probe() -> SudoTouchIdProbe:
    sudo_path = Path("/etc/pam.d/sudo")
    local_path = Path("/etc/pam.d/sudo_local")
    template_path = Path("/etc/pam.d/sudo_local.template")
    if not sudo_path.exists():
        return SudoTouchIdProbe(
            StepStatus.BLOCKED,
            "/etc/pam.d/sudo is missing; unsupported sudo PAM layout",
        )
    if not (
        Path("/usr/lib/pam/pam_tid.so").exists()
        or Path("/usr/lib/pam/pam_tid.so.2").exists()
    ):
        return SudoTouchIdProbe(
            StepStatus.BLOCKED, "pam_tid.so is not available on this Mac"
        )
    sudo_body = sudo_path.read_text(encoding="utf-8")
    if not _sudo_includes_local_config(sudo_body):
        return SudoTouchIdProbe(
            StepStatus.BLOCKED,
            "/etc/pam.d/sudo does not include sudo_local; refusing to edit legacy sudo PAM config directly",
        )
    if local_path.exists():
        existing = local_path.read_text(encoding="utf-8")
        desired = _enable_pam_tid(existing)
        if existing == desired:
            return SudoTouchIdProbe(StepStatus.PRESENT, "Touch ID is enabled for sudo")
        return SudoTouchIdProbe(
            StepStatus.NEEDS_CHANGE,
            "will enable pam_tid.so in /etc/pam.d/sudo_local",
            desired,
        )
    if template_path.exists():
        template = template_path.read_text(encoding="utf-8")
        return SudoTouchIdProbe(
            StepStatus.NEEDS_CHANGE,
            "will create /etc/pam.d/sudo_local from template",
            _enable_pam_tid(template),
        )
    desired = "# sudo_local: local config file which survives system update and is included for sudo\nauth       sufficient     pam_tid.so\n"
    return SudoTouchIdProbe(
        StepStatus.NEEDS_CHANGE,
        "will create /etc/pam.d/sudo_local with pam_tid.so",
        desired,
    )


def _sudo_includes_local_config(body: str) -> bool:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.fullmatch(r"auth\s+include\s+sudo_local", stripped):
            return True
    return False


def _enable_pam_tid(body: str) -> str:
    active = re.compile(r"^\s*auth\s+sufficient\s+pam_tid\.so\s*(?:#.*)?$")
    commented = re.compile(r"^(\s*)#\s*auth\s+sufficient\s+pam_tid\.so\s*(?:#.*)?$")
    lines = body.splitlines()
    for line in lines:
        if active.match(line):
            return body if body.endswith("\n") else body + "\n"
    for index, line in enumerate(lines):
        if commented.match(line):
            lines[index] = "auth       sufficient     pam_tid.so"
            return "\n".join(lines).rstrip("\n") + "\n"
    insert_at = 0
    while insert_at < len(lines) and (
        not lines[insert_at].strip() or lines[insert_at].lstrip().startswith("#")
    ):
        insert_at += 1
    lines.insert(insert_at, "auth       sufficient     pam_tid.so")
    return "\n".join(lines).rstrip("\n") + "\n"


@dataclass(frozen=True)
class XcodeDeveloperDirectoryStep:
    id: str = "macos.xcode-developer-directory"
    title: str = "Select Xcode.app developer directory when installed"
    tags: frozenset[str] = _tags("bootstrap", "packages", "macos", "xcode")
    risks: frozenset[Risk] = _risks(Risk.PRIVILEGED)
    requires: frozenset[ResourceRef] = NO_RESOURCES
    provides: frozenset[ResourceRef] = frozenset({XCODE_DEVELOPER_DIR})
    owns: frozenset[ResourceRef] = frozenset({XCODE_DEVELOPER_DIR})

    def check(self, context: SetupContext) -> StepCheck:
        probe = self._probe(context)
        if probe.status == StepStatus.NEEDS_CHANGE and not context.allow_privileged:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                f"{probe.detail}; requires privileged apply",
                self.tags,
                self.risks,
                remediations=(_xcode_developer_directory_remediation(),),
            )
        return StepCheck(
            self.id, self.title, probe.status, probe.detail, self.tags, self.risks
        )

    def apply(self, context: SetupContext) -> StepResult:
        previous = self._selected_developer_dir(context)
        probe = self._probe_from_previous(previous)
        if probe.status == StepStatus.PRESENT:
            return StepResult(self.id, self.title, StepStatus.PRESENT, probe.detail)
        if probe.status == StepStatus.BLOCKED:
            return StepResult(self.id, self.title, StepStatus.BLOCKED, probe.detail)
        if not context.allow_privileged:
            return StepResult(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                f"{probe.detail}; requires privileged apply",
                remediations=(_xcode_developer_directory_remediation(),),
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
                    self.id, self.title, StepStatus.FAILED, detail, tuple(results)
                )
        result = context.runner.run(
            CommandSpec(
                argv=(
                    "sudo",
                    str(XCODE_SELECT_PATH),
                    "-s",
                    str(XCODE_APP_DEVELOPER_DIR),
                )
            ),
            check=False,
        )
        results.append(result)
        if not result.ok:
            return StepResult(
                self.id,
                self.title,
                StepStatus.FAILED,
                result.stderr.strip()
                or result.stdout.strip()
                or "failed selecting Xcode.app developer directory",
                tuple(results),
            )
        detail = (
            f"dry-run would select {XCODE_APP_DEVELOPER_DIR}"
            if context.dry_run
            else f"selected {XCODE_APP_DEVELOPER_DIR}"
        )
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            detail,
            tuple(results),
            changes=(
                {
                    "type": "xcode_developer_dir",
                    "previous": previous.stdout.strip(),
                    "had_value": previous.ok,
                    "after": str(XCODE_APP_DEVELOPER_DIR),
                },
            ),
        )

    def _probe(self, context: SetupContext) -> StepCheck:
        return self._probe_from_previous(self._selected_developer_dir(context))

    def _probe_from_previous(self, selected: CommandResult) -> StepCheck:
        if not XCODE_SELECT_PATH.exists():
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "xcode-select is not available",
                self.tags,
                self.risks,
            )
        if not XCODE_APP_DEVELOPER_DIR.exists():
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "Xcode.app developer directory is absent; leaving active developer directory unchanged",
                self.tags,
                self.risks,
            )
        selected_path = selected.stdout.strip()
        if selected.ok and selected_path == str(XCODE_APP_DEVELOPER_DIR):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                f"active developer directory is {selected_path}",
                self.tags,
                self.risks,
            )
        detail = (
            f"active developer directory is {selected_path or 'unset'}; "
            f"expected {XCODE_APP_DEVELOPER_DIR}"
            if selected.ok
            else f"developer directory is not selected; expected {XCODE_APP_DEVELOPER_DIR}"
        )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            detail,
            self.tags,
            self.risks,
        )

    def _selected_developer_dir(self, context: SetupContext) -> CommandResult:
        if not XCODE_SELECT_PATH.exists():
            return CommandResult(str(XCODE_SELECT_PATH), 127)
        return context.runner.run(
            CommandSpec(argv=(str(XCODE_SELECT_PATH), "-p")),
            capture=True,
            dry_run=False,
        )


@dataclass(frozen=True)
class XcodeLicenseStep:
    id: str = "macos.xcode-license"
    title: str = "Accept installed Xcode license if pending"
    tags: frozenset[str] = _tags("bootstrap", "packages", "macos", "xcode")
    risks: frozenset[Risk] = _risks(Risk.PRIVILEGED)
    requires: frozenset[ResourceRef] = frozenset({XCODE_DEVELOPER_DIR})
    provides: frozenset[ResourceRef] = frozenset({XCODE_LICENSE})
    owns: frozenset[ResourceRef] = frozenset({XCODE_LICENSE})

    def check(self, context: SetupContext) -> StepCheck:
        if not XCODE_SELECT_PATH.exists() or not XCODEBUILD_PATH.exists():
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "Xcode command-line shims are not available",
                self.tags,
                self.risks,
            )
        selected = context.runner.run(
            CommandSpec(argv=(str(XCODE_SELECT_PATH), "-p")),
            capture=True,
            dry_run=False,
        )
        if not selected.ok:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "developer tools are not selected; no Xcode license to accept yet",
                self.tags,
                self.risks,
            )
        license_check = context.runner.run(
            CommandSpec(argv=(str(XCODEBUILD_PATH), "-license", "check")),
            capture=True,
            dry_run=False,
        )
        if license_check.ok:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "Xcode license is accepted",
                self.tags,
                self.risks,
            )
        detail = (
            license_check.stderr.strip()
            or license_check.stdout.strip()
            or "Xcode license is not accepted"
        )
        if not context.allow_privileged:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                f"{detail}; requires privileged apply",
                self.tags,
                self.risks,
                remediations=(_xcode_license_remediation(),),
            )
        return StepCheck(
            self.id, self.title, StepStatus.NEEDS_CHANGE, detail, self.tags, self.risks
        )

    def apply(self, context: SetupContext) -> StepResult:
        check = self.check(context)
        if check.status == StepStatus.PRESENT:
            return StepResult(self.id, self.title, StepStatus.PRESENT, check.detail)
        if not context.allow_privileged:
            return StepResult(self.id, self.title, StepStatus.BLOCKED, check.detail)
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
                    self.id, self.title, StepStatus.FAILED, detail, tuple(results)
                )
        result = context.runner.run(
            CommandSpec(argv=("sudo", str(XCODEBUILD_PATH), "-license", "accept")),
            check=False,
        )
        results.append(result)
        status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
        detail = (
            "dry-run would accept the installed Xcode license"
            if context.dry_run
            else "Xcode license accepted"
        )
        return StepResult(
            self.id,
            self.title,
            status,
            detail if result.ok else result.stderr.strip(),
            tuple(results),
        )


@dataclass(frozen=True)
class XcodeMetalToolchainStep:
    id: str = "macos.xcode-metal-toolchain"
    title: str = "Install Xcode Metal toolchain component if missing"
    tags: frozenset[str] = _tags("bootstrap", "packages", "macos", "xcode")
    risks: frozenset[Risk] = _risks(Risk.NETWORK)
    requires: frozenset[ResourceRef] = frozenset({XCODE_DEVELOPER_DIR, XCODE_LICENSE})
    provides: frozenset[ResourceRef] = frozenset({XCODE_METAL_TOOLCHAIN})
    owns: frozenset[ResourceRef] = frozenset({XCODE_METAL_TOOLCHAIN})

    def check(self, context: SetupContext) -> StepCheck:
        if not XCODE_APP_DEVELOPER_DIR.exists():
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "Xcode.app developer directory is absent; no Metal toolchain component to download",
                self.tags,
                self.risks,
            )
        if not XCODEBUILD_PATH.exists():
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "xcodebuild is not available",
                self.tags,
                self.risks,
            )
        if XCRUN_PATH.exists():
            metal = context.runner.run(
                CommandSpec(argv=(str(XCRUN_PATH), "--find", "metal")),
                capture=True,
                dry_run=False,
            )
            if metal.ok and metal.stdout.strip():
                return StepCheck(
                    self.id,
                    self.title,
                    StepStatus.PRESENT,
                    f"Metal toolchain is available: {metal.stdout.strip()}",
                    self.tags,
                    self.risks,
                )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            "Metal toolchain component is not available",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        check = self.check(context)
        if check.status == StepStatus.PRESENT:
            return StepResult(self.id, self.title, StepStatus.PRESENT, check.detail)
        result = context.runner.run(
            CommandSpec(
                argv=(
                    str(XCODEBUILD_PATH),
                    "-downloadComponent",
                    "MetalToolchain",
                )
            ),
            check=False,
            capture=False,
            summarize_output=False,
        )
        status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
        detail = (
            "dry-run would download the Xcode Metal toolchain component"
            if context.dry_run
            else "downloaded the Xcode Metal toolchain component"
        )
        return StepResult(
            self.id,
            self.title,
            status,
            detail
            if result.ok
            else result.stderr.strip()
            or result.stdout.strip()
            or "failed downloading the Xcode Metal toolchain component",
            (result,),
            changes=(
                {
                    "type": "xcode_component",
                    "component": "MetalToolchain",
                    "installed_by_run": True,
                },
            )
            if result.ok
            else (),
        )


@dataclass(frozen=True)
class SudoTouchIdStep:
    id: str = "macos.sudo-touch-id"
    title: str = "Enable Touch ID authentication for sudo"
    tags: frozenset[str] = _tags("bootstrap", "macos", "security", "sudo", "terminal")
    risks: frozenset[Risk] = _risks(Risk.PRIVILEGED)
    requires: frozenset[ResourceRef] = NO_RESOURCES
    provides: frozenset[ResourceRef] = frozenset({SUDO_TOUCH_ID})
    owns: frozenset[ResourceRef] = frozenset({SUDO_TOUCH_ID})

    def check(self, context: SetupContext) -> StepCheck:
        probe = _sudo_touch_id_probe()
        if probe.status == StepStatus.PRESENT:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                probe.detail,
                self.tags,
                self.risks,
            )
        if probe.status == StepStatus.BLOCKED:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                probe.detail,
                self.tags,
                self.risks,
            )
        if not context.allow_privileged:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                f"{probe.detail}; requires privileged apply",
                self.tags,
                self.risks,
                remediations=(_sudo_touch_id_remediation(),),
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            probe.detail,
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        probe = _sudo_touch_id_probe()
        if probe.status == StepStatus.PRESENT:
            return StepResult(self.id, self.title, StepStatus.PRESENT, probe.detail)
        if probe.status == StepStatus.BLOCKED:
            return StepResult(self.id, self.title, StepStatus.BLOCKED, probe.detail)
        if not context.allow_privileged:
            return StepResult(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                f"{probe.detail}; requires privileged apply",
                remediations=(_sudo_touch_id_remediation(),),
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
                        "/etc/pam.d/sudo_local",
                    )
                ),
                check=False,
            )
            return StepResult(
                self.id,
                self.title,
                StepStatus.APPLIED,
                "dry-run would enable Touch ID for sudo in /etc/pam.d/sudo_local",
                (result,),
            )

        results: list[CommandResult] = []
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
                self.id, self.title, StepStatus.FAILED, detail, tuple(results)
            )
        target = Path("/etc/pam.d/sudo_local")
        before = target.read_text(encoding="utf-8") if target.exists() else ""
        backup = backup_file_for_context(target, context)
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False
            ) as temporary:
                temporary.write(probe.desired)
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
                        str(target),
                    )
                ),
                check=False,
            )
            results.append(result)
        finally:
            if temporary_name:
                with contextlib.suppress(FileNotFoundError):
                    Path(temporary_name).unlink()
        if not result.ok:
            return StepResult(
                self.id,
                self.title,
                StepStatus.FAILED,
                result.stderr.strip() or result.stdout.strip(),
                tuple(results),
            )
        detail = "enabled Touch ID for sudo in /etc/pam.d/sudo_local"
        if backup is not None:
            detail += f"; backup: {backup}"
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            detail,
            tuple(results),
            changes=(
                {
                    "type": "sudo_touch_id",
                    "path": str(target),
                    "before": before,
                    "after": probe.desired,
                    "existed_before": bool(before),
                },
            ),
        )


def _xcode_license_remediation() -> Remediation:
    return Remediation(
        summary="Accepting the installed Xcode license requires sudo-backed apply.",
        commands=("uv run macsetup apply --tags xcode --allow-privileged --yes",),
        manual_steps=(
            "Run `sudo /usr/bin/xcodebuild -license accept` manually if you prefer not to let macsetup perform this step.",
        ),
    )


def _xcode_developer_directory_remediation() -> Remediation:
    return Remediation(
        summary="Selecting Xcode.app as the active developer directory requires sudo-backed apply.",
        commands=("uv run macsetup apply --tags xcode --allow-privileged --yes",),
        manual_steps=(
            f"Run `sudo {XCODE_SELECT_PATH} -s {XCODE_APP_DEVELOPER_DIR}` manually if you prefer not to let macsetup perform this step.",
        ),
    )


def _sudo_touch_id_remediation() -> Remediation:
    return Remediation(
        summary="Touch ID sudo setup writes /etc/pam.d/sudo_local and requires sudo-backed apply.",
        commands=("uv run macsetup apply --tags sudo --allow-privileged --yes",),
        manual_steps=(
            "macsetup manages /etc/pam.d/sudo_local only and refuses to edit /etc/pam.d/sudo directly.",
            "After applying, run a sudo command in Terminal and confirm macOS offers Touch ID authentication.",
        ),
    )


@dataclass(frozen=True)
class HomebrewInstallStep:
    id: str = "homebrew.install"
    title: str = "Install Homebrew if missing"
    tags: frozenset[str] = _tags("bootstrap", "packages")
    risks: frozenset[Risk] = _risks(Risk.NETWORK, Risk.PACKAGE_INSTALL)
    requires: frozenset[ResourceRef] = frozenset({XCODE_LICENSE, XCODE_METAL_TOOLCHAIN})
    provides: frozenset[ResourceRef] = frozenset({BREW})
    owns: frozenset[ResourceRef] = NO_RESOURCES

    def check(self, context: SetupContext) -> StepCheck:
        if context.command_exists("brew"):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "brew is already on PATH",
                self.tags,
                self.risks,
            )
        if not context.allow_bootstrap:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "brew is missing; bootstrap disabled by --no-bootstrap",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            "brew is missing",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        check = self.check(context)
        if check.status == StepStatus.PRESENT:
            return StepResult(self.id, self.title, StepStatus.PRESENT, check.detail)
        if not context.allow_bootstrap:
            return StepResult(self.id, self.title, StepStatus.BLOCKED, check.detail)
        command = CommandSpec(
            shell='/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
            needs_tty=True,
        )
        result = context.runner.run(
            command, check=False, capture=False, summarize_output=False
        )
        results = [result]
        if result.ok:
            results.append(
                context.runner.run(homebrew_analytics_off_command(), check=False)
            )
        status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
        detail = (
            "dry-run would run Homebrew installer and disable analytics"
            if context.dry_run
            else "Homebrew installer finished and analytics disabled"
        )
        if result.ok and not results[-1].ok:
            status = StepStatus.FAILED
            detail = results[-1].stderr.strip() or results[-1].stdout.strip()
        return StepResult(
            self.id,
            self.title,
            status,
            detail if result.ok else result.stderr.strip(),
            tuple(results),
        )


@dataclass(frozen=True)
class HomebrewAnalyticsStep:
    id: str = "homebrew.analytics-off"
    title: str = "Disable Homebrew analytics"
    tags: frozenset[str] = _tags("bootstrap", "packages", "privacy")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE)
    requires: frozenset[ResourceRef] = frozenset({BREW})
    provides: frozenset[ResourceRef] = frozenset({HOMEBREW_ANALYTICS})
    owns: frozenset[ResourceRef] = frozenset({HOMEBREW_ANALYTICS})

    def check(self, context: SetupContext) -> StepCheck:
        if not context.command_exists("brew"):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "brew is not available",
                self.tags,
                self.risks,
            )
        result = context.runner.run(
            CommandSpec(
                argv=("brew", "analytics", "state"),
                env={"HOMEBREW_NO_AUTO_UPDATE": "1"},
            ),
            capture=True,
            dry_run=False,
        )
        text = f"{result.stdout}\n{result.stderr}".lower()
        if "analytics are disabled" in text or "analytics were destroyed" in text:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "Homebrew analytics are disabled",
                self.tags,
                self.risks,
            )
        if "analytics are enabled" in text:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.NEEDS_CHANGE,
                "Homebrew analytics are enabled",
                self.tags,
                self.risks,
            )
        detail = (
            result.stdout.strip()
            or result.stderr.strip()
            or "could not determine Homebrew analytics state"
        )
        return StepCheck(
            self.id, self.title, StepStatus.NEEDS_CHANGE, detail, self.tags, self.risks
        )

    def apply(self, context: SetupContext) -> StepResult:
        if not context.command_exists("brew") and not context.dry_run:
            return StepResult(
                self.id, self.title, StepStatus.BLOCKED, "brew is not available"
            )
        previous = "unknown"
        if context.command_exists("brew"):
            state = context.runner.run(
                CommandSpec(
                    argv=("brew", "analytics", "state"),
                    env={"HOMEBREW_NO_AUTO_UPDATE": "1"},
                ),
                capture=True,
                dry_run=False,
            )
            text = f"{state.stdout}\n{state.stderr}".lower()
            if "analytics are disabled" in text or "analytics were destroyed" in text:
                previous = "disabled"
            elif "analytics are enabled" in text:
                previous = "enabled"
        result = context.runner.run(homebrew_analytics_off_command(), check=False)
        status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
        detail = (
            "dry-run would disable Homebrew analytics"
            if context.dry_run
            else "Homebrew analytics disabled"
        )
        return StepResult(
            self.id,
            self.title,
            status,
            detail if result.ok else result.stderr.strip(),
            (result,),
            changes=(
                {
                    "type": "homebrew_analytics",
                    "previous": previous,
                    "after": "disabled",
                },
            )
            if result.ok
            else (),
        )


@dataclass(frozen=True)
class HomebrewSharePermissionsStep:
    id: str = "homebrew.share-permissions"
    title: str = "Set Homebrew share directory mode to 755"
    tags: frozenset[str] = _tags("bootstrap", "packages", "permissions")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE)
    requires: frozenset[ResourceRef] = frozenset({BREW})
    provides: frozenset[ResourceRef] = frozenset({HOMEBREW_SHARE_PERMISSIONS})
    owns: frozenset[ResourceRef] = frozenset({HOMEBREW_SHARE_PERMISSIONS})

    def check(self, context: SetupContext) -> StepCheck:
        path = _homebrew_share_path(context)
        if path is None:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "brew prefix is unavailable",
                self.tags,
                self.risks,
            )
        if not path.exists():
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                f"{path} does not exist yet",
                self.tags,
                self.risks,
            )
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode == 0o755:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                f"{path} mode is 755",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            f"{path} mode is {mode:o}; expected 755",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        path = _homebrew_share_path(context)
        if path is None:
            return StepResult(
                self.id, self.title, StepStatus.BLOCKED, "brew prefix is unavailable"
            )
        if not path.exists():
            return StepResult(
                self.id, self.title, StepStatus.PRESENT, f"{path} does not exist yet"
            )
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode == 0o755:
            return StepResult(
                self.id, self.title, StepStatus.PRESENT, f"{path} mode is 755"
            )
        result = context.runner.run(
            CommandSpec(argv=("chmod", "755", str(path))), check=False
        )
        status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
        detail = (
            f"dry-run would chmod 755 {path}"
            if context.dry_run
            else f"chmod 755 {path}"
        )
        return StepResult(
            self.id,
            self.title,
            status,
            detail if result.ok else result.stderr.strip(),
            (result,),
            changes=(
                {
                    "type": "chmod",
                    "path": str(path),
                    "previous_mode": mode,
                    "mode": 0o755,
                },
            )
            if result.ok
            else (),
        )


@dataclass(frozen=True)
class HomebrewPhantomJsCleanupStep:
    id: str = "homebrew.cleanup-phantomjs"
    title: str = "Remove legacy PhantomJS Homebrew metadata"
    tags: frozenset[str] = _tags("bootstrap", "packages", "cleanup")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE, Risk.PACKAGE_INSTALL)
    requires: frozenset[ResourceRef] = frozenset({BREW})
    provides: frozenset[ResourceRef] = frozenset({HOMEBREW_PHANTOMJS_CLEANUP})
    owns: frozenset[ResourceRef] = frozenset({HOMEBREW_PHANTOMJS_CLEANUP})

    def check(self, context: SetupContext) -> StepCheck:
        paths = _legacy_phantomjs_paths(context)
        if paths is None:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "brew prefix is unavailable",
                self.tags,
                self.risks,
            )
        if not paths:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "legacy PhantomJS metadata is absent",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            "will remove: " + ", ".join(str(path) for path in paths),
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        paths = _legacy_phantomjs_paths(context)
        if paths is None:
            return StepResult(
                self.id, self.title, StepStatus.BLOCKED, "brew prefix is unavailable"
            )
        if not paths:
            return StepResult(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "legacy PhantomJS metadata is absent",
            )
        if context.dry_run:
            for path in paths:
                context.runner.state("cleanup.remove.skip", str(path))
            return StepResult(
                self.id,
                self.title,
                StepStatus.APPLIED,
                "dry-run would remove: " + ", ".join(str(path) for path in paths),
            )
        removed: list[str] = []
        for path in paths:
            context.runner.state("cleanup.remove.start", str(path))
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as error:
                return StepResult(
                    self.id,
                    self.title,
                    StepStatus.FAILED,
                    f"failed removing {path}: {error}",
                )
            removed.append(str(path))
            context.runner.state("cleanup.remove.done", str(path))
        return StepResult(
            self.id, self.title, StepStatus.APPLIED, "removed: " + ", ".join(removed)
        )


@dataclass(frozen=True)
class PackageProbe:
    installed: tuple[str, ...]
    missing: tuple[str, ...]
    repair: tuple[PackageRepair, ...] = ()
    installer_pending: tuple[PackageInstallerPending, ...] = ()
    unmanaged_apps: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackageRepair:
    name: str
    reason: str


@dataclass(frozen=True)
class PackageInstallerPending:
    name: str
    installer_paths: tuple[str, ...]
    reason: str


def _package_probe_detail(probe: PackageProbe) -> str:
    parts = []
    if probe.installed:
        parts.append("installed: " + ", ".join(probe.installed))
    if probe.missing:
        parts.append("missing: " + ", ".join(probe.missing))
    if probe.repair:
        parts.append(
            "repair: "
            + ", ".join(f"{item.name} ({item.reason})" for item in probe.repair)
        )
    if probe.installer_pending:
        parts.append(
            "installer pending: "
            + ", ".join(
                f"{item.name} ({', '.join(item.installer_paths)})"
                for item in probe.installer_pending
            )
        )
    if probe.unmanaged_apps:
        parts.append("unmanaged app bundle(s): " + ", ".join(probe.unmanaged_apps))
    return "; ".join(parts) if parts else "no selected packages"


@dataclass(frozen=True)
class BrewPackagesStep:
    id: str
    title: str
    packages: tuple[BrewPackage, ...]
    cask: bool = False
    tags: frozenset[str] = _tags("packages")
    risks: frozenset[Risk] = _risks(Risk.NETWORK, Risk.PACKAGE_INSTALL)

    @property
    def requires(self) -> frozenset[ResourceRef]:
        required = {BREW, XCODE_LICENSE, HOMEBREW_ANALYTICS, HOMEBREW_SHARE_PERMISSIONS}
        if self.cask:
            required.add(HOMEBREW_PHANTOMJS_CLEANUP)
        return frozenset(required)

    @property
    def provides(self) -> frozenset[ResourceRef]:
        make_resource = brew_cask if self.cask else brew_formula
        return frozenset(make_resource(package.name) for package in self.packages)

    @property
    def owns(self) -> frozenset[ResourceRef]:
        return self.provides

    def check(self, context: SetupContext) -> StepCheck:
        probe_or_error = self._probe(context)
        if isinstance(probe_or_error, str):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                probe_or_error,
                self.tags,
                self.risks,
            )
        remediations = self._package_remediations(context, probe_or_error)
        if probe_or_error.unmanaged_apps:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                _package_probe_detail(probe_or_error),
                self.tags,
                self.risks,
                remediations=remediations,
            )
        if (
            probe_or_error.installer_pending
            and not probe_or_error.missing
            and not probe_or_error.repair
        ):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.MANUAL,
                _package_probe_detail(probe_or_error),
                self.tags,
                self.risks,
                remediations=remediations,
            )
        if (
            not probe_or_error.missing
            and not probe_or_error.repair
            and not probe_or_error.installer_pending
        ):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                _package_probe_detail(probe_or_error),
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            _package_probe_detail(probe_or_error),
            self.tags,
            self.risks,
            remediations=remediations,
        )

    def apply(self, context: SetupContext) -> StepResult:
        probe_or_error = self._probe(context)
        if isinstance(probe_or_error, str):
            return StepResult(self.id, self.title, StepStatus.BLOCKED, probe_or_error)
        remediations = self._package_remediations(context, probe_or_error)
        if probe_or_error.unmanaged_apps:
            return StepResult(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                _package_probe_detail(probe_or_error),
                remediations=remediations,
            )
        if (
            probe_or_error.installer_pending
            and not probe_or_error.missing
            and not probe_or_error.repair
        ):
            return StepResult(
                self.id,
                self.title,
                StepStatus.MANUAL,
                _package_probe_detail(probe_or_error),
                remediations=remediations,
            )
        if (
            not probe_or_error.missing
            and not probe_or_error.repair
            and not probe_or_error.installer_pending
        ):
            return StepResult(
                self.id,
                self.title,
                StepStatus.PRESENT,
                _package_probe_detail(probe_or_error),
            )
        results: list[CommandResult] = []
        changes: list[dict[str, object]] = []
        package_kind = "cask" if self.cask else "formula"
        context.runner.state(
            "brew.install.plan",
            f"{self.id} {len(probe_or_error.missing)} missing, {len(probe_or_error.repair)} repair, and {len(probe_or_error.installer_pending)} installer-pending {package_kind}(s)",
        )
        if probe_or_error.missing:
            argv = self._install_argv(probe_or_error.missing)
            package_names = ", ".join(probe_or_error.missing)
            context.runner.state(
                "brew.install.start",
                f"{len(probe_or_error.missing)} {package_kind}(s): {package_names}",
            )
            result = context.runner.run(CommandSpec(argv=argv), check=False)
            results.append(result)
            context.runner.state(
                "brew.install.done",
                f"{len(probe_or_error.missing)} {package_kind}(s) exit={result.returncode}",
            )
            if not result.ok:
                detail = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"brew install failed for {package_kind}(s): {package_names}"
                )
                return StepResult(
                    self.id, self.title, StepStatus.FAILED, detail, tuple(results)
                )
            for package_name in probe_or_error.missing:
                changes.append(
                    {
                        "type": "brew_package",
                        "name": package_name,
                        "cask": self.cask,
                        "installed_by_run": True,
                    }
                )
        for repair in probe_or_error.repair:
            argv = (
                ("brew", "reinstall", "--cask", repair.name)
                if self.cask
                else ("brew", "reinstall", repair.name)
            )
            context.runner.state(
                "brew.reinstall.start", f"{package_kind} {repair.name}: {repair.reason}"
            )
            result = context.runner.run(CommandSpec(argv=argv), check=False)
            results.append(result)
            context.runner.state(
                "brew.reinstall.done",
                f"{package_kind} {repair.name} exit={result.returncode}",
            )
            if not result.ok:
                detail = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"brew reinstall failed for {repair.name}"
                )
                return StepResult(
                    self.id, self.title, StepStatus.FAILED, detail, tuple(results)
                )
            changes.append(
                {
                    "type": "brew_package_repair",
                    "name": repair.name,
                    "cask": self.cask,
                    "reason": repair.reason,
                }
            )
        if context.dry_run:
            detail = _package_apply_detail("dry-run would", probe_or_error)
        else:
            detail = _package_apply_detail("completed", probe_or_error)
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            detail,
            tuple(results),
            changes=tuple(changes),
            remediations=remediations,
        )

    def _install_argv(self, package_names: tuple[str, ...]) -> tuple[str, ...]:
        if self.cask:
            return ("brew", "install", "--cask", *package_names)
        return ("brew", "install", *package_names)

    def _probe(self, context: SetupContext) -> PackageProbe | str:
        if not context.command_exists("brew"):
            return "brew is not available"
        mode = "--cask" if self.cask else "--formula"
        if self.cask:
            installed = self._installed_from_homebrew_dirs(context) or set()
        else:
            result = context.runner.run(
                CommandSpec(
                    argv=("brew", "list", mode, "--versions"),
                    env={
                        "HOMEBREW_NO_AUTO_UPDATE": "1",
                        "HOMEBREW_NO_ANALYTICS": "1",
                        "HOMEBREW_NO_INSTALL_FROM_API": "1",
                    },
                ),
                capture=True,
                dry_run=False,
            )
            if result.ok:
                installed = {
                    line.split()[0]
                    for line in result.stdout.splitlines()
                    if line.strip()
                }
            else:
                fallback_installed = self._installed_from_homebrew_dirs(context)
                if fallback_installed is None:
                    return (
                        result.stderr.strip()
                        or result.stdout.strip()
                        or "unable to list brew packages"
                    )
                installed = fallback_installed
        missing = []
        installed_selected = []
        repair = []
        installer_pending = []
        unmanaged_apps = []
        for package in self.packages:
            existing_apps = self._existing_app_bundles(context, package)
            valid_apps = self._valid_app_bundles(context, package)
            valid_installers = self._valid_installer_bundles(context, package)
            if package.name in installed:
                if self.cask and package.app_bundles and not valid_apps:
                    reason = self._missing_app_bundle_reason(context, package)
                    if valid_installers:
                        installer_pending.append(
                            PackageInstallerPending(
                                package.name, valid_installers, reason
                            )
                        )
                    else:
                        repair.append(PackageRepair(package.name, reason))
                elif valid_apps:
                    installed_selected.append(
                        f"{package.name} ({', '.join(valid_apps)})"
                    )
                else:
                    installed_selected.append(package.name)
                continue
            if existing_apps:
                unmanaged_apps.append(f"{package.name} ({', '.join(existing_apps)})")
                continue
            missing.append(package.name)
        return PackageProbe(
            installed=tuple(installed_selected),
            missing=tuple(missing),
            repair=tuple(repair),
            installer_pending=tuple(installer_pending),
            unmanaged_apps=tuple(unmanaged_apps),
        )

    def _installed_from_homebrew_dirs(self, context: SetupContext) -> set[str] | None:
        prefix = context.brew_prefix()
        if prefix is None:
            return None
        root = prefix / ("Caskroom" if self.cask else "Cellar")
        if not root.exists():
            return set()
        return {child.name for child in root.iterdir() if child.is_dir()}

    def _existing_app_bundles(
        self, context: SetupContext, package: BrewPackage
    ) -> tuple[str, ...]:
        if not self.cask or not package.app_bundles:
            return ()
        return self._existing_bundle_paths(context, package.app_bundles)

    def _existing_installer_bundles(
        self, context: SetupContext, package: BrewPackage
    ) -> tuple[str, ...]:
        if not self.cask or not package.installer_bundles:
            return ()
        return (
            *self._existing_bundle_paths(context, package.installer_bundles),
            *self._existing_caskroom_bundle_paths(
                context, package, package.installer_bundles
            ),
        )

    def _existing_bundle_paths(
        self, context: SetupContext, bundles: tuple[str, ...]
    ) -> tuple[str, ...]:
        existing: list[str] = []
        for root in (Path("/Applications"), context.home / "Applications"):
            for bundle in bundles:
                candidate = root / bundle
                if candidate.exists():
                    existing.append(str(candidate))
        return tuple(existing)

    def _existing_caskroom_bundle_paths(
        self, context: SetupContext, package: BrewPackage, bundles: tuple[str, ...]
    ) -> tuple[str, ...]:
        prefix = context.brew_prefix()
        if prefix is None:
            return ()
        root = prefix / "Caskroom" / package.name
        if not root.exists():
            return ()
        existing: list[str] = []
        for bundle in bundles:
            existing.extend(
                str(path) for path in sorted(root.rglob(bundle)) if path.exists()
            )
        return tuple(existing)

    def _valid_app_bundles(
        self, context: SetupContext, package: BrewPackage
    ) -> tuple[str, ...]:
        return tuple(
            path
            for path in self._existing_app_bundles(context, package)
            if _is_valid_app_bundle(Path(path))
        )

    def _valid_installer_bundles(
        self, context: SetupContext, package: BrewPackage
    ) -> tuple[str, ...]:
        return tuple(
            path
            for path in self._existing_installer_bundles(context, package)
            if _is_valid_app_bundle(Path(path))
        )

    def _missing_app_bundle_reason(
        self, context: SetupContext, package: BrewPackage
    ) -> str:
        expected = self._candidate_bundle_paths(context, package.app_bundles)
        return (
            "Homebrew cask is recorded but expected valid app bundle is missing; expected one of: "
            + ", ".join(expected)
        )

    def _candidate_bundle_paths(
        self, context: SetupContext, bundles: tuple[str, ...]
    ) -> tuple[str, ...]:
        return tuple(
            str(root / bundle)
            for root in (Path("/Applications"), context.home / "Applications")
            for bundle in bundles
        )

    def _package_remediations(
        self, context: SetupContext, probe: PackageProbe
    ) -> tuple[Remediation, ...]:
        packages_by_name = {package.name: package for package in self.packages}
        remediations: list[Remediation] = []
        for package_name in probe.missing:
            package = packages_by_name[package_name]
            if package.installer_bundles:
                remediations.append(
                    self._installer_staging_remediation(
                        context,
                        package,
                        install_command="brew install --cask "
                        + shlex.quote(package.name),
                    )
                )
        for repair in probe.repair:
            package = packages_by_name[repair.name]
            if package.installer_bundles:
                remediations.append(
                    self._installer_staging_remediation(
                        context,
                        package,
                        install_command="brew reinstall --cask "
                        + shlex.quote(package.name),
                    )
                )
            else:
                remediations.append(
                    Remediation(
                        summary=f"{package.name} cask record exists but its declared app bundle is missing or invalid.",
                        commands=(
                            "brew reinstall --cask " + shlex.quote(package.name),
                        ),
                        manual_steps=(
                            "If reinstall does not recreate the app, remove stale cask state with "
                            f"`brew uninstall --cask --force {package.name}`, then rerun macsetup apply for this tag.",
                        ),
                    )
                )
        for pending in probe.installer_pending:
            package = packages_by_name[pending.name]
            remediations.append(
                self._installer_pending_remediation(context, package, pending)
            )
        return tuple(remediations)

    def _installer_staging_remediation(
        self, context: SetupContext, package: BrewPackage, *, install_command: str
    ) -> Remediation:
        final_paths = self._candidate_bundle_paths(context, package.app_bundles)
        commands = (
            install_command,
            *self._caskroom_installer_open_commands(package),
        )
        return Remediation(
            summary=f"{package.name} stages a separate installer app; Homebrew alone does not create the final app.",
            commands=commands,
            manual_steps=(
                f"Complete the installer UI, verify one of {', '.join(final_paths)} exists, then rerun `uv run macsetup plan --tags apps`.",
            ),
        )

    def _caskroom_installer_open_commands(
        self, package: BrewPackage
    ) -> tuple[str, ...]:
        return tuple(
            _caskroom_installer_open_command(package.name, bundle)
            for bundle in package.installer_bundles
        )

    def _installer_pending_remediation(
        self,
        context: SetupContext,
        package: BrewPackage,
        pending: PackageInstallerPending,
    ) -> Remediation:
        final_paths = self._candidate_bundle_paths(context, package.app_bundles)
        commands = tuple(
            "open " + shlex.quote(path) for path in pending.installer_paths
        )
        return Remediation(
            summary=f"{package.name} cask staged an installer, but the final app is still missing.",
            commands=commands,
            manual_steps=(
                f"Complete the installer UI, verify one of {', '.join(final_paths)} exists, then rerun `uv run macsetup plan --tags apps`.",
                f"If the staged installer is stale or absent, run `brew reinstall --cask {package.name}` and then open the installer again.",
            ),
        )


def _package_apply_detail(prefix: str, probe: PackageProbe) -> str:
    actions = []
    if probe.missing:
        actions.append(
            f"install {len(probe.missing)} package(s): " + ", ".join(probe.missing)
        )
    if probe.repair:
        actions.append(
            f"repair {len(probe.repair)} package(s): "
            + ", ".join(item.name for item in probe.repair)
        )
    return prefix + " " + "; ".join(actions)


def _caskroom_installer_open_command(package_name: str, bundle: str) -> str:
    return (
        f'caskroom="$(brew --prefix)/Caskroom/{package_name}"; '
        f'installer=$(find "$caskroom" -name {shlex.quote(bundle)} '
        '-type d -print -quit); test -n "$installer" && open "$installer"'
    )


def _is_valid_app_bundle(path: Path) -> bool:
    if path.suffix != ".app":
        return path.exists()
    return path.is_dir() and (path / "Contents" / "Info.plist").exists()


def _homebrew_share_path(context: SetupContext) -> Path | None:
    prefix = context.brew_prefix()
    if prefix is None:
        return None
    return prefix / "share"


def _legacy_phantomjs_paths(context: SetupContext) -> tuple[Path, ...] | None:
    prefix = context.brew_prefix()
    if prefix is None:
        return None
    candidates = (
        prefix / "Caskroom" / "phantomjs",
        prefix / "Cellar" / "phantomjs",
    )
    return tuple(path for path in candidates if path.exists())


@dataclass(frozen=True)
class ZshCompletionPermissionsStep:
    id: str = "shell.zsh-completion-permissions"
    title: str = "Tighten zsh completion directory permissions"
    tags: frozenset[str] = _tags("shell")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE)
    requires: frozenset[ResourceRef] = frozenset({BREW})
    provides: frozenset[ResourceRef] = frozenset(
        {resource("permission", "brew-zsh-completions")}
    )
    owns: frozenset[ResourceRef] = frozenset(
        {resource("permission", "brew-zsh-completions")}
    )

    def check(self, context: SetupContext) -> StepCheck:
        prefix = context.brew_prefix()
        if prefix is None:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "brew prefix is unavailable",
                self.tags,
                self.risks,
            )
        insecure = self._insecure_paths(prefix)
        if not insecure:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "Homebrew zsh completion paths are not group/world writable",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            "group/world writable: " + ", ".join(str(path) for path in insecure),
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        prefix = context.brew_prefix()
        if prefix is None:
            return StepResult(
                self.id, self.title, StepStatus.BLOCKED, "brew prefix is unavailable"
            )
        results: list[CommandResult] = []
        for path in self._parent_paths(prefix):
            if path.exists():
                results.append(
                    context.runner.run(
                        CommandSpec(argv=("chmod", "go-w", str(path))), check=False
                    )
                )
        for path in self._recursive_paths(prefix):
            if path.exists():
                results.append(
                    context.runner.run(
                        CommandSpec(argv=("chmod", "-R", "go-w", str(path))),
                        check=False,
                    )
                )
        failed = [result for result in results if not result.ok]
        if failed:
            return StepResult(
                self.id,
                self.title,
                StepStatus.FAILED,
                failed[0].stderr.strip(),
                tuple(results),
            )
        if context.dry_run:
            return StepResult(
                self.id,
                self.title,
                StepStatus.APPLIED,
                "dry-run would tighten permission bits",
                tuple(results),
            )
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            "permission bits tightened",
            tuple(results),
        )

    def _parent_paths(self, prefix: Path) -> tuple[Path, ...]:
        return (prefix, prefix / "share")

    def _recursive_paths(self, prefix: Path) -> tuple[Path, ...]:
        return (
            prefix / "share" / "zsh",
            prefix / "share" / "zsh-completions",
        )

    def _candidate_paths(self, prefix: Path) -> tuple[Path, ...]:
        return (*self._parent_paths(prefix), *self._recursive_paths(prefix))

    def _insecure_paths(self, prefix: Path) -> tuple[Path, ...]:
        insecure = []
        for path in self._parent_paths(prefix):
            if path.exists() and self._is_group_or_world_writable(path):
                insecure.append(path)
        for path in self._recursive_paths(prefix):
            if path.exists():
                insecure.extend(self._group_or_world_writable_paths(path))
        return tuple(dict.fromkeys(insecure))

    def _group_or_world_writable_paths(self, path: Path) -> tuple[Path, ...]:
        if path.is_file() or path.is_symlink():
            return (path,) if self._is_group_or_world_writable(path) else ()
        insecure = []
        for current, directories, files in os.walk(path):
            for name in [".", *directories, *files]:
                candidate = Path(current) if name == "." else Path(current) / name
                if self._is_group_or_world_writable(candidate):
                    insecure.append(candidate)
        return tuple(insecure)

    def _is_group_or_world_writable(self, path: Path) -> bool:
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            return False
        return bool(mode & (stat.S_IWGRP | stat.S_IWOTH))


@dataclass(frozen=True)
class ManagedDirectoryStep:
    path: str
    tags: frozenset[str] = _tags("files")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE)

    @property
    def id(self) -> str:
        return f"macos.directory.{_slug(self.path.replace('~/', ''))}"

    @property
    def title(self) -> str:
        return f"Create {self.path}"

    @property
    def requires(self) -> frozenset[ResourceRef]:
        return NO_RESOURCES

    @property
    def provides(self) -> frozenset[ResourceRef]:
        return frozenset({directory_resource(self.path)})

    @property
    def owns(self) -> frozenset[ResourceRef]:
        return self.provides

    def check(self, context: SetupContext) -> StepCheck:
        target = expand_user(self.path, context.home)
        if target.is_dir():
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                f"{target} exists",
                self.tags,
                self.risks,
            )
        if target.exists():
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                f"{target} exists but is not a directory",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            f"will create {target}",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        target = expand_user(self.path, context.home)
        if target.is_dir():
            return StepResult(
                self.id, self.title, StepStatus.PRESENT, f"{target} exists"
            )
        if target.exists():
            return StepResult(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                f"{target} exists but is not a directory",
            )
        result = context.runner.run(
            CommandSpec(argv=("mkdir", "-p", str(target))), check=False
        )
        if not result.ok:
            return StepResult(
                self.id,
                self.title,
                StepStatus.FAILED,
                result.stderr.strip()
                or result.stdout.strip()
                or f"failed creating {target}",
                (result,),
            )
        if not context.dry_run and not target.is_dir():
            return StepResult(
                self.id,
                self.title,
                StepStatus.FAILED,
                f"{target} was not created as a directory",
                (result,),
            )
        detail = (
            f"dry-run would create {target}" if context.dry_run else f"created {target}"
        )
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            detail,
            (result,),
            changes=(
                {
                    "type": "managed_directory",
                    "path": str(target),
                    "profile_path": self.path,
                    "existed_before": False,
                },
            ),
        )


@dataclass(frozen=True)
class MacDefaultsStep:
    setting: MacDefaultsSetting
    risks: frozenset[Risk] = _risks(Risk.USER_SETTING)

    @property
    def id(self) -> str:
        return f"macos.defaults.{self.setting.name}"

    @property
    def title(self) -> str:
        return f"Set macOS default {self.setting.domain} {self.setting.key}"

    @property
    def tags(self) -> frozenset[str]:
        return frozenset(self.setting.tags)

    @property
    def requires(self) -> frozenset[ResourceRef]:
        return frozenset(directory_resource(path) for path in self.setting.directories)

    @property
    def provides(self) -> frozenset[ResourceRef]:
        return frozenset({macos_default_resource(self.setting)})

    @property
    def owns(self) -> frozenset[ResourceRef]:
        return self.provides

    def check(self, context: SetupContext) -> StepCheck:
        if not context.command_exists("defaults"):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "defaults command is not available",
                self.tags,
                self.risks,
            )
        current, _ = read_default(context, self.setting)
        wanted = desired_value(self.setting, context.home)
        if current.had_value and values_match(
            self.setting, current.value, wanted, context.home
        ):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                f"{self.setting.domain} {self.setting.key} is {wanted}",
                self.tags,
                self.risks,
            )
        detail = (
            f"{self.setting.domain} {self.setting.key} is unset; expected {wanted}"
            if not current.had_value
            else (
                f"{self.setting.domain} {self.setting.key} is "
                f"{current.value}; expected {wanted}"
            )
        )
        return StepCheck(
            self.id, self.title, StepStatus.NEEDS_CHANGE, detail, self.tags, self.risks
        )

    def apply(self, context: SetupContext) -> StepResult:
        if not context.command_exists("defaults"):
            return StepResult(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "defaults command is not available",
            )
        previous, _ = read_default(context, self.setting)
        wanted = desired_value(self.setting, context.home)
        if previous.had_value and values_match(
            self.setting, previous.value, wanted, context.home
        ):
            return StepResult(
                self.id,
                self.title,
                StepStatus.PRESENT,
                f"{self.setting.domain} {self.setting.key} is {wanted}",
            )
        results: list[CommandResult] = []
        result = context.runner.run(
            defaults_write_command(self.setting, self.setting.value, context.home),
            check=False,
        )
        results.append(result)
        if not result.ok:
            return StepResult(
                self.id,
                self.title,
                StepStatus.FAILED,
                result.stderr.strip()
                or result.stdout.strip()
                or f"failed setting {defaults_resource_name(self.setting)}",
                tuple(results),
            )
        detail = (
            f"dry-run would set {defaults_resource_name(self.setting)} to {wanted}"
            if context.dry_run
            else f"set {defaults_resource_name(self.setting)} to {wanted}"
        )
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            detail,
            tuple(results),
            changes=(
                {
                    "type": "macos_default",
                    "name": self.setting.name,
                    "domain": self.setting.domain,
                    "key": self.setting.key,
                    "value_type": self.setting.value_type,
                    "previous": previous.value,
                    "had_value": previous.had_value,
                    "after": wanted,
                },
            ),
        )


@dataclass(frozen=True)
class GitConfigStep:
    setting: GitSetting
    tags: frozenset[str] = _tags("git")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE)

    @property
    def requires(self) -> frozenset[ResourceRef]:
        required = {brew_formula("git")}
        if self.setting.key in {
            "core.pager",
            "interactive.diffFilter",
            "pager.diff",
            "pager.log",
            "pager.show",
        } or self.setting.key.startswith("delta."):
            required.add(brew_formula("git-delta"))
        if self.setting.key == "pager.log":
            required.add(brew_formula("bat"))
            # pager.log points at the compiled git-log-pager dispatcher; ensure
            # it is built before we write the config that references it.
            required.add(file_resource("~/.local/bin/git-log-pager"))
        if self.setting.key.startswith("filter.lfs."):
            required.add(brew_formula("git-lfs"))
        if self.setting.key == "core.editor":
            required.add(brew_formula("neovim"))
        return frozenset(required)

    @property
    def provides(self) -> frozenset[ResourceRef]:
        return frozenset({git_config_resource(self.setting.key)})

    @property
    def owns(self) -> frozenset[ResourceRef]:
        return self.provides

    @property
    def id(self) -> str:
        return f"git.config.{self.setting.key}"

    @property
    def title(self) -> str:
        return f"Set git {self.setting.key}"

    def check(self, context: SetupContext) -> StepCheck:
        if not context.command_exists("git"):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "git is not available",
                self.tags,
                self.risks,
            )
        current = self._current_values(context)
        desired = self._desired_value(context)
        if current == (desired,):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "already configured",
                self.tags,
                self.risks,
            )
        detail = (
            "not configured"
            if not current
            else "current value(s): " + ", ".join(current)
        )
        return StepCheck(
            self.id, self.title, StepStatus.NEEDS_CHANGE, detail, self.tags, self.risks
        )

    def apply(self, context: SetupContext) -> StepResult:
        previous_values = self._current_values(context)
        desired = self._desired_value(context)
        if previous_values == (desired,):
            return StepResult(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "already configured",
            )
        result = context.runner.run(
            CommandSpec(
                argv=(
                    "git",
                    "config",
                    "--global",
                    "--replace-all",
                    self.setting.key,
                    desired,
                )
            ),
            check=False,
        )
        status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
        detail = "dry-run would configure" if context.dry_run else "configured"
        return StepResult(
            self.id,
            self.title,
            status,
            detail if result.ok else result.stderr.strip(),
            (result,),
            changes=(
                {
                    "type": "git_config",
                    "key": self.setting.key,
                    "previous": previous_values[0] if previous_values else "",
                    "previous_values": list(previous_values),
                    "had_value": bool(previous_values),
                    "after": desired,
                },
            )
            if result.ok
            else (),
        )

    def _desired_value(self, context: SetupContext) -> str:
        if self.setting.value.startswith("~/"):
            return str(context.home / self.setting.value[2:])
        return self.setting.value

    def _current_values(self, context: SetupContext) -> tuple[str, ...]:
        result = context.runner.run(
            CommandSpec(
                argv=("git", "config", "--global", "--get-all", self.setting.key)
            ),
            capture=True,
            dry_run=False,
        )
        if not result.ok:
            return ()
        return tuple(line for line in result.stdout.splitlines() if line)


@dataclass(frozen=True)
class ManagedBlockStep:
    id: str
    title: str
    path: str
    marker: str
    block: str
    comment_prefix: str = "#"
    adopt_existing_chunks: bool = False
    adopt_existing_patterns: tuple[str, ...] = ()
    # Canonical marker order for all macsetup-managed blocks in `path`. When set,
    # blocks are placed (and self-healed) into this order in the file rather than
    # appended in incidental run order. Empty => legacy append-to-end behavior.
    order: tuple[str, ...] = ()
    # Marker of the block that must be applied immediately before this one in the
    # same file. Chains the steps in the dependency graph so run order also matches
    # `order`, reinforcing the file-placement guarantee.
    previous_marker: str | None = None
    tags: frozenset[str] = _tags("files")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE)

    @property
    def requires(self) -> frozenset[ResourceRef]:
        if self.previous_marker is None:
            return NO_RESOURCES
        return frozenset({file_block(self.path, self.previous_marker)})

    @property
    def provides(self) -> frozenset[ResourceRef]:
        return frozenset({file_block(self.path, self.marker)})

    @property
    def owns(self) -> frozenset[ResourceRef]:
        return self.provides

    def check(self, context: SetupContext) -> StepCheck:
        change = self._change(context)
        if not change.changed:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                f"{change.path} is current",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            f"will update {change.path}",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        change = self._change(context)
        if not change.changed:
            return StepResult(
                self.id, self.title, StepStatus.PRESENT, f"{change.path} is current"
            )
        if context.dry_run:
            return StepResult(
                self.id,
                self.title,
                StepStatus.APPLIED,
                f"dry-run would update {change.path}",
            )
        backup = backup_file_for_context(change.path, context)
        atomic_write(change.path, change.after, change.mode_after)
        detail = f"updated {change.path}"
        if backup is not None:
            detail += f"; backup: {backup}"
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            detail,
            changes=(
                _file_change_record(
                    "managed_block",
                    change,
                    marker=self.marker,
                    comment_prefix=self.comment_prefix,
                ),
            ),
        )

    def preview(self, context: SetupContext) -> tuple[FileChange, ...]:
        change = self._change(context)
        return (change,) if change.changed else ()

    def _change(self, context: SetupContext) -> FileChange:
        target = expand_user(self.path, context.home)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        desired = upsert_block(
            existing,
            name=self.marker,
            content=self.block,
            comment_prefix=self.comment_prefix,
            adopt_existing_chunks=self.adopt_existing_chunks,
            adopt_existing_patterns=self.adopt_existing_patterns,
            order=self.order,
        )
        return text_file_change(target, desired)


@dataclass(frozen=True)
class ManagedFileStep:
    id: str
    title: str
    path: str
    body: str
    mode: int | None = None
    tags: frozenset[str] = _tags("files")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE)

    @property
    def requires(self) -> frozenset[ResourceRef]:
        return NO_RESOURCES

    @property
    def provides(self) -> frozenset[ResourceRef]:
        return frozenset({file_resource(self.path)})

    @property
    def owns(self) -> frozenset[ResourceRef]:
        return self.provides

    def check(self, context: SetupContext) -> StepCheck:
        change = self._change(context)
        if not change.changed:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                f"{change.path} is current",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            f"will write {change.path}",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        change = self._change(context)
        if not change.changed:
            return StepResult(
                self.id, self.title, StepStatus.PRESENT, f"{change.path} is current"
            )
        if context.dry_run:
            return StepResult(
                self.id,
                self.title,
                StepStatus.APPLIED,
                f"dry-run would write {change.path}",
            )
        backup = backup_file_for_context(change.path, context)
        atomic_write(change.path, change.after, change.mode_after)
        detail = f"wrote {change.path}"
        if backup is not None:
            detail += f"; backup: {backup}"
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            detail,
            changes=(_file_change_record("managed_file", change),),
        )

    def preview(self, context: SetupContext) -> tuple[FileChange, ...]:
        change = self._change(context)
        return (change,) if change.changed else ()

    def _change(self, context: SetupContext) -> FileChange:
        target = expand_user(self.path, context.home)
        desired = self.body if self.body.endswith("\n") else self.body + "\n"
        return text_file_change(target, desired, self.mode)


@dataclass(frozen=True)
class MrsyncLauncherStep:
    id: str = "sync.mrsync-launcher"
    title: str = "Install mrsync launcher"
    tags: frozenset[str] = _tags("sync", "shell", "files")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE)
    requires: frozenset[ResourceRef] = NO_RESOURCES
    provides: frozenset[ResourceRef] = frozenset({file_resource("~/.local/bin/mrsync")})
    owns: frozenset[ResourceRef] = frozenset({file_resource("~/.local/bin/mrsync")})

    def check(self, context: SetupContext) -> StepCheck:
        change = self._change(context)
        if not change.changed:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                f"{change.path} is current",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            f"will write {change.path}",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        change = self._change(context)
        if not change.changed:
            return StepResult(
                self.id, self.title, StepStatus.PRESENT, f"{change.path} is current"
            )
        if context.dry_run:
            return StepResult(
                self.id,
                self.title,
                StepStatus.APPLIED,
                f"dry-run would write {change.path}",
            )
        backup = backup_file_for_context(change.path, context)
        atomic_write(change.path, change.after, change.mode_after)
        detail = f"wrote {change.path}"
        if backup is not None:
            detail += f"; backup: {backup}"
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            detail,
            changes=(_file_change_record("managed_file", change),),
        )

    def preview(self, context: SetupContext) -> tuple[FileChange, ...]:
        change = self._change(context)
        return (change,) if change.changed else ()

    def _change(self, context: SetupContext) -> FileChange:
        target = context.home / ".local" / "bin" / "mrsync"
        return text_file_change(target, self._body(context), 0o755)

    def _body(self, context: SetupContext) -> str:
        repo_root = shlex.quote(str(context.repo_root))
        return f"""#!/bin/sh
set -eu

repo_root={repo_root}
if ! command -v uv >/dev/null 2>&1; then
  printf '%s\\n' 'mrsync launcher requires uv on PATH.' >&2
  exit 127
fi

exec uv --directory "$repo_root" run mrsync "$@"
"""


@dataclass(frozen=True)
class GitLogPagerStep:
    """Deploy and compile the git-log-pager dispatch binary.

    git fires the same `pager.log` for `git log` and `git log -p`. We want plain
    logs rendered by bat and patches by delta. The previous approach buffered all
    of git's output to a temp file to sniff for a diff before choosing a pager,
    which defeated git's streaming and added seconds of latency on large repos.

    This compiles a tiny C dispatcher that peeks only a bounded prefix, then
    execs the chosen pager while streaming the rest with backpressure intact, so
    time-to-first-page matches a direct pager. The source is deployed alongside
    the binary so we can detect changes and recompile.
    """

    id: str = "git.log-pager"
    title: str = "Compile git-log-pager dispatch binary"
    source_path: str = "~/.config/macsetup/git-log-pager.c"
    binary_path: str = "~/.local/bin/git-log-pager"
    tags: frozenset[str] = _tags("git", "shell", "files")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE)
    requires: frozenset[ResourceRef] = frozenset(
        {brew_formula("git-delta"), brew_formula("bat")}
    )

    @property
    def provides(self) -> frozenset[ResourceRef]:
        return frozenset({file_resource(self.binary_path)})

    @property
    def owns(self) -> frozenset[ResourceRef]:
        return frozenset(
            {file_resource(self.binary_path), file_resource(self.source_path)}
        )

    def _source(self) -> str:
        body = content.GIT_LOG_PAGER_C
        return body if body.endswith("\n") else body + "\n"

    def _paths(self, context: SetupContext) -> tuple[Path, Path]:
        return (
            expand_user(self.source_path, context.home),
            expand_user(self.binary_path, context.home),
        )

    def _compiler(self, context: SetupContext) -> str | None:
        for candidate in ("cc", "clang", "gcc"):
            if context.command_exists(candidate):
                return candidate
        return None

    def _needs_build(self, context: SetupContext) -> bool:
        source, binary = self._paths(context)
        if not binary.exists():
            return True
        if not source.exists():
            return True
        return source.read_text(encoding="utf-8") != self._source()

    def check(self, context: SetupContext) -> StepCheck:
        if not self._needs_build(context):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "git-log-pager is current",
                self.tags,
                self.risks,
            )
        if self._compiler(context) is None:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "no C compiler (cc/clang) on PATH",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            "will compile git-log-pager",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        source, binary = self._paths(context)
        if not self._needs_build(context):
            return StepResult(
                self.id, self.title, StepStatus.PRESENT, "git-log-pager is current"
            )
        if context.dry_run:
            return StepResult(
                self.id,
                self.title,
                StepStatus.APPLIED,
                f"dry-run would compile {binary}",
            )
        compiler = self._compiler(context)
        if compiler is None:
            return StepResult(
                self.id,
                self.title,
                StepStatus.FAILED,
                "no C compiler (cc/clang) on PATH",
            )
        backup = backup_file_for_context(source, context)
        atomic_write(source, self._source(), 0o644)
        # Compile to a temp path then atomically move into place so a failed
        # build never leaves a half-written binary that git would try to exec.
        tmp_binary = binary.with_name(f".{binary.name}.macsetup.build")
        binary.parent.mkdir(parents=True, exist_ok=True)
        result = context.runner.run(
            CommandSpec(
                argv=(
                    compiler,
                    "-O2",
                    "-o",
                    str(tmp_binary),
                    str(source),
                )
            ),
            check=False,
        )
        if not result.ok:
            with contextlib.suppress(OSError):
                tmp_binary.unlink()
            return StepResult(
                self.id,
                self.title,
                StepStatus.FAILED,
                f"failed to compile {source}",
                (result,),
            )
        os.chmod(tmp_binary, 0o755)
        os.replace(tmp_binary, binary)
        detail = f"compiled {binary}"
        if backup is not None:
            detail += f"; backup: {backup}"
        return StepResult(self.id, self.title, StepStatus.APPLIED, detail, (result,))


@dataclass(frozen=True)
class OhMyZshStep:
    id: str = "shell.oh-my-zsh"
    title: str = "Install oh-my-zsh repository"
    tags: frozenset[str] = _tags("packages", "shell")
    risks: frozenset[Risk] = _risks(Risk.NETWORK, Risk.USER_FILE)
    requires: frozenset[ResourceRef] = frozenset({brew_formula("git")})
    provides: frozenset[ResourceRef] = frozenset({file_resource("~/.oh-my-zsh")})
    owns: frozenset[ResourceRef] = frozenset({file_resource("~/.oh-my-zsh")})

    def check(self, context: SetupContext) -> StepCheck:
        target = context.home / ".oh-my-zsh" / "oh-my-zsh.sh"
        if target.exists():
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "oh-my-zsh is present",
                self.tags,
                self.risks,
            )
        if not context.command_exists("git"):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "git is not available",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            "oh-my-zsh is missing",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        target_dir = context.home / ".oh-my-zsh"
        if (target_dir / "oh-my-zsh.sh").exists():
            return StepResult(
                self.id, self.title, StepStatus.PRESENT, "oh-my-zsh is present"
            )
        result = context.runner.run(
            CommandSpec(
                argv=(
                    "git",
                    "clone",
                    "--depth=1",
                    "https://github.com/ohmyzsh/ohmyzsh.git",
                    str(target_dir),
                )
            ),
            check=False,
        )
        status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
        detail = (
            "dry-run would clone oh-my-zsh" if context.dry_run else "cloned oh-my-zsh"
        )
        return StepResult(
            self.id,
            self.title,
            status,
            detail if result.ok else result.stderr.strip(),
            (result,),
        )


@dataclass(frozen=True)
class OhMyZshThemeStep:
    id: str = "shell.oh-my-zsh-theme.kphoen"
    title: str = "Deploy customized kphoen oh-my-zsh theme"
    tags: frozenset[str] = _tags("packages", "shell", "files")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE)
    requires: frozenset[ResourceRef] = frozenset({file_resource("~/.oh-my-zsh")})
    provides: frozenset[ResourceRef] = frozenset(
        {file_resource("~/.oh-my-zsh/themes/kphoen.zsh-theme")}
    )
    owns: frozenset[ResourceRef] = provides

    def check(self, context: SetupContext) -> StepCheck:
        theme_dir = context.home / ".oh-my-zsh" / "themes"
        if not theme_dir.exists():
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "oh-my-zsh themes directory is missing",
                self.tags,
                self.risks,
            )
        change = self._change(context)
        if not change.changed:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "custom kphoen theme is current",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            "will update customized kphoen theme",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        theme_dir = context.home / ".oh-my-zsh" / "themes"
        if not theme_dir.exists():
            return StepResult(
                self.id,
                self.title,
                StepStatus.FAILED,
                "oh-my-zsh themes directory is missing",
            )
        change = self._change(context)
        if not change.changed:
            return StepResult(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "custom kphoen theme is current",
            )
        if context.dry_run:
            return StepResult(
                self.id,
                self.title,
                StepStatus.APPLIED,
                "dry-run would update customized kphoen theme",
            )
        backup = backup_file_for_context(change.path, context)
        atomic_write(change.path, change.after, change.mode_after)
        detail = "updated customized kphoen theme"
        if backup is not None:
            detail += f"; backup: {backup}"
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            detail,
            changes=(_file_change_record("managed_file", change),),
        )

    def preview(self, context: SetupContext) -> tuple[FileChange, ...]:
        change = self._change(context)
        return (change,) if change.changed else ()

    def _change(self, context: SetupContext) -> FileChange:
        target = context.home / ".oh-my-zsh" / "themes" / "kphoen.zsh-theme"
        desired = content.KPHOEN_ZSH_THEME
        if not desired.endswith("\n"):
            desired += "\n"
        return text_file_change(target, desired)


def _register_pyenv_paths(context: SetupContext) -> None:
    pyenv_root = context.home / ".pyenv"
    context.runner.add_path_dir(pyenv_root / "bin")
    context.runner.add_path_dir(pyenv_root / "shims")


def _step_id_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "default"


def _pyenv_python_command(version: str, *args: str) -> CommandSpec:
    return CommandSpec(
        argv=("pyenv", "exec", "python", *args), env={"PYENV_VERSION": version}
    )


def _pyenv_python_pip_list(context: SetupContext, version: str) -> CommandResult:
    return context.runner.run(
        _pyenv_python_command(version, "-m", "pip", "list", "--format=json"),
        capture=True,
        dry_run=False,
    )


def _pyenv_python_pip_install_command(
    version: str, packages: tuple[str, ...]
) -> CommandSpec:
    return _pyenv_python_command(version, "-m", "pip", "install", *packages, "-U")


def _pyenv_install_command(
    version: str, build_jobs: str, build_env: tuple[tuple[str, str], ...]
) -> CommandSpec:
    return CommandSpec(
        argv=("pyenv", "install", "--skip-existing", version),
        env=_pyenv_build_env(build_jobs, build_env),
    )


def _pyenv_build_env(
    build_jobs: str, build_env: tuple[tuple[str, str], ...]
) -> dict[str, str]:
    env = dict(build_env)
    if build_jobs != "default" and "MAKE_OPTS" not in env and "MAKEOPTS" not in env:
        env["MAKE_OPTS"] = f"-j{_pyenv_build_job_count(build_jobs)}"
    return env


def _pyenv_build_job_count(build_jobs: str) -> str:
    if build_jobs == "auto":
        return str(max(1, os.cpu_count() or 1))
    return build_jobs


def _matching_pyenv_version(requested: str, versions_stdout: str) -> str | None:
    matches = [
        line.strip()
        for line in versions_stdout.splitlines()
        if _pyenv_version_matches(requested, line.strip())
    ]
    return matches[-1] if matches else None


def _pyenv_version_matches(requested: str, installed: str) -> bool:
    return installed == requested or installed.startswith(f"{requested}.")


@dataclass(frozen=True)
class PyenvPythonStep:
    version: str
    tooling_packages: tuple[str, ...]
    build_jobs: str = "auto"
    build_env: tuple[tuple[str, str], ...] = ()
    select_global: bool = True
    title: str = "Install and select pyenv Python"
    tags: frozenset[str] = _tags("python")
    risks: frozenset[Risk] = _risks(Risk.NETWORK, Risk.PACKAGE_INSTALL)

    @property
    def id(self) -> str:
        if self.select_global:
            return "python.pyenv-version"
        return f"python.pyenv-version.{_step_id_token(self.version)}"

    @property
    def requires(self) -> frozenset[ResourceRef]:
        return PYENV_BUILD_REQUIREMENTS

    @property
    def provides(self) -> frozenset[ResourceRef]:
        return frozenset({python_version_resource(self.version)})

    @property
    def owns(self) -> frozenset[ResourceRef]:
        return self.provides

    def check(self, context: SetupContext) -> StepCheck:
        if not context.command_exists("pyenv"):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "pyenv is not available",
                self.tags,
                self.risks,
            )
        _register_pyenv_paths(context)
        versions = context.runner.run(
            CommandSpec(argv=("pyenv", "versions", "--bare")),
            capture=True,
            dry_run=False,
        )
        if not versions.ok:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                versions.stderr.strip() or "pyenv versions failed",
                self.tags,
                self.risks,
            )
        installed_version = _matching_pyenv_version(self.version, versions.stdout)
        installed = installed_version is not None
        missing_tooling: list[str] = []
        tooling_probe_failed = ""
        if installed:
            tooling_probe = _pyenv_python_pip_list(
                context, installed_version or self.version
            )
            if tooling_probe.ok:
                try:
                    installed_packages = {
                        item["name"].lower()
                        for item in json.loads(tooling_probe.stdout)
                    }
                    missing_tooling = [
                        package
                        for package in self.tooling_packages
                        if package.lower() not in installed_packages
                    ]
                except (json.JSONDecodeError, KeyError, TypeError):
                    tooling_probe_failed = "could not parse Python tooling package list"
            else:
                tooling_probe_failed = (
                    tooling_probe.stderr.strip() or "Python tooling probe failed"
                )
        global_version = context.runner.run(
            CommandSpec(argv=("pyenv", "global")), capture=True, dry_run=False
        )
        selected_global = (
            global_version.stdout.strip().splitlines()[0:1] if global_version.ok else []
        )
        selected = not self.select_global or bool(
            selected_global and _pyenv_version_matches(self.version, selected_global[0])
        )
        if installed and selected and not missing_tooling and not tooling_probe_failed:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                self._present_detail(),
                self.tags,
                self.risks,
            )
        details = []
        if not installed:
            details.append(f"{self.version} is not installed")
        if not selected:
            details.append(
                f"global version is {global_version.stdout.strip() or 'unset'}"
            )
        if missing_tooling:
            details.append("missing tooling: " + ", ".join(missing_tooling))
        if tooling_probe_failed:
            details.append(tooling_probe_failed)
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            "; ".join(details),
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        versions = context.runner.run(
            CommandSpec(argv=("pyenv", "versions", "--bare")),
            capture=True,
            dry_run=False,
        )
        installed_before = versions.ok and bool(
            _matching_pyenv_version(self.version, versions.stdout)
        )
        global_version = context.runner.run(
            CommandSpec(argv=("pyenv", "global")), capture=True, dry_run=False
        )
        previous_global = (
            global_version.stdout.strip().splitlines()[0]
            if global_version.ok and global_version.stdout.strip()
            else "system"
        )
        results: list[CommandResult] = []
        install_command = _pyenv_install_command(
            self.version, self.build_jobs, self.build_env
        )
        result = context.runner.run(install_command, check=False)
        results.append(result)
        if not result.ok:
            return StepResult(
                self.id,
                self.title,
                StepStatus.FAILED,
                result.stderr.strip() or result.stdout.strip(),
                tuple(results),
            )
        runtime_version = self.version
        if not context.dry_run:
            versions_after = context.runner.run(
                CommandSpec(argv=("pyenv", "versions", "--bare")),
                capture=True,
                dry_run=False,
            )
            if versions_after.ok:
                runtime_version = (
                    _matching_pyenv_version(self.version, versions_after.stdout)
                    or self.version
                )
        commands: list[CommandSpec] = []
        if self.tooling_packages:
            commands.append(
                _pyenv_python_pip_install_command(
                    runtime_version, self.tooling_packages
                )
            )
        if self.select_global:
            commands.append(CommandSpec(argv=("pyenv", "global", self.version)))
        commands.append(CommandSpec(argv=("pyenv", "rehash")))
        for command in commands:
            result = context.runner.run(command, check=False)
            results.append(result)
            if not result.ok:
                return StepResult(
                    self.id,
                    self.title,
                    StepStatus.FAILED,
                    result.stderr.strip() or result.stdout.strip(),
                    tuple(results),
                )
        _register_pyenv_paths(context)
        if context.dry_run:
            return StepResult(
                self.id,
                self.title,
                StepStatus.APPLIED,
                f"dry-run would set pyenv global to {self.version}",
                tuple(results),
                changes=(
                    {
                        "type": "pyenv_python",
                        "version": self.version,
                        "installed_before": installed_before,
                        "previous_global": previous_global,
                        "selected_global": self.select_global,
                        "tooling_packages": list(self.tooling_packages),
                    },
                ),
            )
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            self._applied_detail(),
            tuple(results),
            changes=(
                {
                    "type": "pyenv_python",
                    "version": self.version,
                    "installed_before": installed_before,
                    "previous_global": previous_global,
                    "selected_global": self.select_global,
                    "tooling_packages": list(self.tooling_packages),
                },
            ),
        )

    def _present_detail(self) -> str:
        if self.select_global:
            return f"pyenv global is {self.version}; tooling packages are installed"
        return f"pyenv {self.version} and tooling packages are installed"

    def _applied_detail(self) -> str:
        if self.select_global:
            return f"pyenv global set to {self.version}; tooling packages installed/upgraded"
        return f"pyenv {self.version} tooling packages installed/upgraded"


@dataclass(frozen=True)
class PythonPackagesStep:
    packages: tuple[str, ...]
    python_version: str
    id: str = "python.global-packages"
    title: str = "Install global Python utility packages"
    tags: frozenset[str] = _tags("python")
    risks: frozenset[Risk] = _risks(Risk.NETWORK, Risk.PACKAGE_INSTALL)

    @property
    def requires(self) -> frozenset[ResourceRef]:
        return frozenset({python_version_resource(self.python_version)})

    @property
    def provides(self) -> frozenset[ResourceRef]:
        return frozenset(python_package_resource(package) for package in self.packages)

    @property
    def owns(self) -> frozenset[ResourceRef]:
        return self.provides

    def check(self, context: SetupContext) -> StepCheck:
        if not context.command_exists("pyenv"):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "pyenv is not available",
                self.tags,
                self.risks,
            )
        result = context.runner.run(
            CommandSpec(
                argv=("pyenv", "exec", "python", "-m", "pip", "list", "--format=json")
            ),
            capture=True,
            dry_run=False,
        )
        if not result.ok:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                result.stderr.strip() or "pip is not available",
                self.tags,
                self.risks,
            )
        try:
            installed = {item["name"].lower() for item in json.loads(result.stdout)}
        except (json.JSONDecodeError, KeyError, TypeError):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.UNKNOWN,
                "could not parse pip package list",
                self.tags,
                self.risks,
            )
        missing = [
            package for package in self.packages if package.lower() not in installed
        ]
        if not missing:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "all Python packages are installed",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            "missing: " + ", ".join(missing),
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        installed: set[str] | None = None
        probe = context.runner.run(
            CommandSpec(
                argv=("pyenv", "exec", "python", "-m", "pip", "list", "--format=json")
            ),
            capture=True,
            dry_run=False,
        )
        if probe.ok:
            try:
                installed = {item["name"].lower() for item in json.loads(probe.stdout)}
            except (json.JSONDecodeError, KeyError, TypeError):
                installed = None
        missing = [
            package
            for package in self.packages
            if installed is not None and package.lower() not in installed
        ]
        result = context.runner.run(
            CommandSpec(
                argv=(
                    "pyenv",
                    "exec",
                    "python",
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    *self.packages,
                )
            ),
            check=False,
        )
        status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
        detail = (
            "dry-run would install/upgrade Python packages"
            if context.dry_run
            else "Python packages installed/upgraded"
        )
        return StepResult(
            self.id,
            self.title,
            status,
            detail if result.ok else result.stderr.strip(),
            (result,),
            changes=tuple(
                {"type": "python_package", "name": package, "installed_by_run": True}
                for package in missing
            )
            if result.ok
            else (),
        )


@dataclass(frozen=True)
class NpmGlobalPackagesStep:
    packages: tuple[str, ...]
    id: str = "javascript.npm-global-packages"
    title: str = "Install global npm packages"
    tags: frozenset[str] = _tags("javascript")
    risks: frozenset[Risk] = _risks(Risk.NETWORK, Risk.PACKAGE_INSTALL)

    @property
    def requires(self) -> frozenset[ResourceRef]:
        return frozenset({brew_formula("node")})

    @property
    def provides(self) -> frozenset[ResourceRef]:
        return frozenset(npm_package_resource(package) for package in self.packages)

    @property
    def owns(self) -> frozenset[ResourceRef]:
        return self.provides

    def check(self, context: SetupContext) -> StepCheck:
        if not context.command_exists("npm"):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "npm is not available",
                self.tags,
                self.risks,
            )
        result = context.runner.run(
            CommandSpec(argv=("npm", "list", "-g", "--depth=0", "--json")),
            capture=True,
            dry_run=False,
        )
        if not result.ok and not result.stdout.strip():
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                result.stderr.strip() or "npm list failed",
                self.tags,
                self.risks,
            )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.UNKNOWN,
                "could not parse npm package list",
                self.tags,
                self.risks,
            )
        installed = set((payload.get("dependencies") or {}).keys())
        missing = [package for package in self.packages if package not in installed]
        if not missing:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "all npm packages are installed",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            "missing: " + ", ".join(missing),
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        installed: set[str] | None = None
        probe = context.runner.run(
            CommandSpec(argv=("npm", "list", "-g", "--depth=0", "--json")),
            capture=True,
            dry_run=False,
        )
        if probe.ok or probe.stdout.strip():
            try:
                payload = json.loads(probe.stdout or "{}")
                installed = set((payload.get("dependencies") or {}).keys())
            except json.JSONDecodeError:
                installed = None
        missing = [
            package
            for package in self.packages
            if installed is not None and package not in installed
        ]
        result = context.runner.run(
            CommandSpec(argv=("npm", "install", "-g", *self.packages)), check=False
        )
        status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
        detail = (
            "dry-run would install/upgrade npm packages"
            if context.dry_run
            else "npm packages installed/upgraded"
        )
        return StepResult(
            self.id,
            self.title,
            status,
            detail if result.ok else result.stderr.strip(),
            (result,),
            changes=tuple(
                {"type": "npm_package", "name": package, "installed_by_run": True}
                for package in missing
            )
            if result.ok
            else (),
        )


@dataclass(frozen=True)
class SourceBuildStep:
    package: SourceBuildPackage

    @property
    def id(self) -> str:
        return f"source-build.{self.package.name}"

    @property
    def title(self) -> str:
        return f"Build source package {self.package.name}"

    @property
    def tags(self) -> frozenset[str]:
        return frozenset(self.package.tags)

    @property
    def risks(self) -> frozenset[Risk]:
        return _risks(Risk.NETWORK, Risk.PACKAGE_INSTALL, Risk.USER_FILE)

    @property
    def requires(self) -> frozenset[ResourceRef]:
        return frozenset(
            brew_formula(dependency) for dependency in self._brew_dependencies()
        )

    @property
    def provides(self) -> frozenset[ResourceRef]:
        return frozenset(
            {
                source_build_resource(self.package.name),
                *(
                    source_build_binary_resource(self.package.name, binary)
                    for binary in self.package.binaries
                ),
            }
        )

    @property
    def owns(self) -> frozenset[ResourceRef]:
        owned = {source_build_resource(self.package.name)}
        if self.package.install_mode == "copy":
            owned.update(file_resource(str(path)) for path in self._install_paths(None))
        return frozenset(owned)

    def check(self, context: SetupContext) -> StepCheck:
        blocked = self._blocked_reason(context)
        if blocked:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                blocked,
                self.tags,
                self.risks,
            )
        source_dir = self._source_dir(context)
        if not (source_dir / ".git").exists():
            return StepCheck(
                self.id,
                self.title,
                StepStatus.NEEDS_CHANGE,
                f"will clone {self.package.repo} into {source_dir}",
                self.tags,
                self.risks,
            )
        remote = self._remote_origin(context)
        if remote is not None and remote != self.package.repo:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                f"{source_dir} uses remote {remote}, expected {self.package.repo}",
                self.tags,
                self.risks,
            )
        if self._fixed_ref_mismatch(context):
            return StepCheck(
                self.id,
                self.title,
                StepStatus.NEEDS_CHANGE,
                f"will checkout {self.package.ref}",
                self.tags,
                self.risks,
            )
        missing = [
            path for path in self._expected_binary_paths(context) if not path.exists()
        ]
        if missing:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.NEEDS_CHANGE,
                "missing binary path(s): " + ", ".join(str(path) for path in missing),
                self.tags,
                self.risks,
            )
        self._register_runtime_path_dirs(context)
        return StepCheck(
            self.id,
            self.title,
            StepStatus.PRESENT,
            f"{self.package.name} is built",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        blocked = self._blocked_reason(context)
        if blocked:
            return StepResult(self.id, self.title, StepStatus.BLOCKED, blocked)
        if context.dry_run:
            return StepResult(
                self.id,
                self.title,
                StepStatus.APPLIED,
                f"dry-run would clone/build/install {self.package.name}",
                changes=(self._change_record(context),),
            )

        source_dir = self._source_dir(context)
        if source_dir.exists() and not (source_dir / ".git").exists():
            return StepResult(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                f"{source_dir} exists but is not a git checkout",
            )
        source_dir.parent.mkdir(parents=True, exist_ok=True)
        results: list[CommandResult] = []
        if not source_dir.exists():
            clone_command = ["git", "clone"]
            if self.package.submodules:
                clone_command.append("--recurse-submodules")
            clone_command.extend((self.package.repo, str(source_dir)))
            result = context.runner.run(
                CommandSpec(argv=tuple(clone_command)), check=False
            )
            results.append(result)
            if not result.ok:
                return self._failed_result("git clone failed", results)
        else:
            remote = self._remote_origin(context)
            if remote is not None and remote != self.package.repo:
                return StepResult(
                    self.id,
                    self.title,
                    StepStatus.BLOCKED,
                    f"{source_dir} uses remote {remote}, expected {self.package.repo}",
                    tuple(results),
                )

        if self.package.ref:
            result = context.runner.run(
                CommandSpec(argv=("git", "-C", str(source_dir), "fetch", "--tags")),
                check=False,
            )
            results.append(result)
            if not result.ok:
                return self._failed_result("git fetch failed", results)
            result = context.runner.run(
                CommandSpec(
                    argv=("git", "-C", str(source_dir), "checkout", self.package.ref)
                ),
                check=False,
            )
            results.append(result)
            if not result.ok:
                return self._failed_result("git checkout failed", results)
        elif self.package.update:
            result = context.runner.run(
                CommandSpec(argv=("git", "-C", str(source_dir), "pull", "--ff-only")),
                check=False,
            )
            results.append(result)
            if not result.ok:
                return self._failed_result("git pull failed", results)

        if self.package.submodules:
            result = context.runner.run(
                CommandSpec(
                    argv=(
                        "git",
                        "-C",
                        str(source_dir),
                        "submodule",
                        "update",
                        "--init",
                        "--recursive",
                    )
                ),
                check=False,
            )
            results.append(result)
            if not result.ok:
                return self._failed_result("git submodule update failed", results)

        for command in self.package.build_commands:
            context.runner.state(
                "source-build.start", f"{self.package.name}: {command}"
            )
            result = context.runner.run(
                CommandSpec(
                    shell=command,
                    cwd=source_dir,
                    env=dict(self.package.env),
                ),
                check=False,
            )
            results.append(result)
            context.runner.state(
                "source-build.done",
                f"{self.package.name}: exit={result.returncode} {command}",
            )
            if not result.ok:
                return self._failed_result(f"build command failed: {command}", results)

        missing = [
            path for path in self._built_binary_paths(context) if not path.exists()
        ]
        if missing:
            return StepResult(
                self.id,
                self.title,
                StepStatus.FAILED,
                "build did not produce binary path(s): "
                + ", ".join(str(path) for path in missing),
                tuple(results),
            )
        if self.package.install_mode == "copy":
            self._copy_binaries(context)
        self._register_runtime_path_dirs(context)
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            f"built {self.package.name}",
            tuple(results),
            changes=(self._change_record(context),),
        )

    def _blocked_reason(self, context: SetupContext) -> str:
        missing = [
            command
            for command in self._required_commands()
            if not context.command_exists(command)
        ]
        if missing:
            return "missing command(s): " + ", ".join(missing)
        return ""

    def _required_commands(self) -> tuple[str, ...]:
        commands = ["git"]
        if self.package.build_system == "cargo":
            commands.append("cargo")
        elif self.package.build_system == "zig":
            commands.append("zig")
        return tuple(commands)

    def _brew_dependencies(self) -> tuple[str, ...]:
        dependencies = ["git"]
        if self.package.build_system == "cargo":
            dependencies.append("rust")
        elif self.package.build_system == "zig":
            dependencies.append("zig")
        dependencies.extend(self.package.brew_dependencies)
        return tuple(dict.fromkeys(dependencies))

    def _source_dir(self, context: SetupContext) -> Path:
        configured = (
            self.package.source_dir or f"~/.local/src/macsetup/{self.package.name}"
        )
        return expand_user(configured, context.home)

    def _built_binary_paths(self, context: SetupContext) -> tuple[Path, ...]:
        source_dir = self._source_dir(context)
        binary_dir = Path(self.package.binary_dir)
        root = source_dir / binary_dir if self.package.binary_dir else source_dir
        return tuple(root / binary for binary in self.package.binaries)

    def _install_paths(self, context: SetupContext | None) -> tuple[Path, ...]:
        if context is None:
            install_dir = Path(self.package.install_dir)
        else:
            install_dir = expand_user(self.package.install_dir, context.home)
        return tuple(install_dir / binary for binary in self.package.binaries)

    def _expected_binary_paths(self, context: SetupContext) -> tuple[Path, ...]:
        if self.package.install_mode == "copy":
            return self._install_paths(context)
        return self._built_binary_paths(context)

    def _copy_binaries(self, context: SetupContext) -> None:
        install_dir = expand_user(self.package.install_dir, context.home)
        install_dir.mkdir(parents=True, exist_ok=True)
        for source, target in zip(
            self._built_binary_paths(context),
            self._install_paths(context),
            strict=True,
        ):
            shutil.copy2(source, target)
            target.chmod(
                target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )

    def _runtime_path_dirs(self, context: SetupContext) -> tuple[Path, ...]:
        if self.package.install_mode == "copy":
            return (expand_user(self.package.install_dir, context.home),)
        source_dir = self._source_dir(context)
        binary_dir = Path(self.package.binary_dir)
        return (source_dir / binary_dir if self.package.binary_dir else source_dir,)

    def _register_runtime_path_dirs(self, context: SetupContext) -> None:
        if not self.package.manage_path:
            return
        for directory in self._runtime_path_dirs(context):
            context.runner.add_path_dir(directory)

    def _remote_origin(self, context: SetupContext) -> str | None:
        source_dir = self._source_dir(context)
        result = context.runner.run(
            CommandSpec(
                argv=("git", "-C", str(source_dir), "remote", "get-url", "origin")
            ),
            capture=True,
            dry_run=False,
        )
        if not result.ok:
            return None
        return result.stdout.strip() or None

    def _fixed_ref_mismatch(self, context: SetupContext) -> bool:
        if not _looks_like_git_commit(self.package.ref):
            return False
        source_dir = self._source_dir(context)
        result = context.runner.run(
            CommandSpec(argv=("git", "-C", str(source_dir), "rev-parse", "HEAD")),
            capture=True,
            dry_run=False,
        )
        return result.ok and result.stdout.strip() != self.package.ref

    def _change_record(self, context: SetupContext) -> dict[str, object]:
        return {
            "type": "source_build",
            "name": self.package.name,
            "repo": self.package.repo,
            "ref": self.package.ref,
            "source_dir": str(self._source_dir(context)),
            "install_mode": self.package.install_mode,
            "binary_paths": tuple(
                str(path) for path in self._expected_binary_paths(context)
            ),
        }

    def _failed_result(
        self, detail: str, results: Sequence[CommandResult]
    ) -> StepResult:
        last = results[-1] if results else None
        output = ""
        if last is not None:
            output = last.stderr.strip() or last.stdout.strip()
        return StepResult(
            self.id,
            self.title,
            StepStatus.FAILED,
            f"{detail}: {output}" if output else detail,
            tuple(results),
        )


def _looks_like_git_commit(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{40}", value))


@dataclass(frozen=True)
class DnsmasqConfigStep:
    recipe: SetupRecipe
    id: str = "dns.dnsmasq-config"
    title: str = "Deploy dnsmasq resolver config"
    tags: frozenset[str] = _tags("dns")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE)
    requires: frozenset[ResourceRef] = frozenset({brew_formula("dnsmasq")})
    provides: frozenset[ResourceRef] = frozenset(
        {resource("config", "dnsmasq.snippet")}
    )
    owns: frozenset[ResourceRef] = frozenset(
        {file_resource("$(brew --prefix)/etc/dnsmasq.d/00-macsetup.conf")}
    )

    def check(self, context: SetupContext) -> StepCheck:
        change = self._change(context)
        if change is None:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "brew prefix is unavailable",
                self.tags,
                self.risks,
            )
        if not change.changed:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                f"{change.path} is current",
                self.tags,
                self.risks,
            )
        detail = f"will write {change.path}"
        if not context.enable_dns_blocklist:
            detail += " with the focus blocklist disabled"
        return StepCheck(
            self.id, self.title, StepStatus.NEEDS_CHANGE, detail, self.tags, self.risks
        )

    def apply(self, context: SetupContext) -> StepResult:
        change = self._change(context)
        if change is None:
            return StepResult(
                self.id, self.title, StepStatus.BLOCKED, "brew prefix is unavailable"
            )
        if not change.changed:
            return StepResult(
                self.id, self.title, StepStatus.PRESENT, f"{change.path} is current"
            )
        if context.dry_run:
            return StepResult(
                self.id,
                self.title,
                StepStatus.APPLIED,
                f"dry-run would write {change.path}",
            )
        backup = backup_file_for_context(change.path, context)
        atomic_write(change.path, change.after, change.mode_after)
        detail = f"wrote {change.path}"
        if backup is not None:
            detail += f"; backup: {backup}"
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            detail,
            changes=(_file_change_record("managed_file", change),),
        )

    def preview(self, context: SetupContext) -> tuple[FileChange, ...]:
        change = self._change(context)
        return (change,) if change is not None and change.changed else ()

    def _body(self, context: SetupContext) -> str:
        body = content.dnsmasq_config(
            servers=self.recipe.dns_servers,
            passthrough_domains=self.recipe.dns_passthrough_domains,
            blocked_domains=self.recipe.dns_blocked_domains,
            enable_blocklist=context.enable_dns_blocklist,
        )
        return managed_block("dnsmasq", body, "#")

    def _change(self, context: SetupContext) -> FileChange | None:
        prefix = context.brew_prefix()
        if prefix is None:
            return None
        target = prefix / "etc" / "dnsmasq.d" / "00-macsetup.conf"
        return text_file_change(target, self._body(context))


@dataclass(frozen=True)
class DnsmasqMainConfigStep:
    id: str = "dns.dnsmasq-main-conf-dir"
    title: str = "Ensure dnsmasq loads dnsmasq.d snippets"
    tags: frozenset[str] = _tags("dns")
    risks: frozenset[Risk] = _risks(Risk.USER_FILE)
    requires: frozenset[ResourceRef] = frozenset({brew_formula("dnsmasq")})
    provides: frozenset[ResourceRef] = frozenset({resource("config", "dnsmasq.main")})
    owns: frozenset[ResourceRef] = frozenset(
        {file_block("$(brew --prefix)/etc/dnsmasq.conf", "dnsmasq-conf-dir")}
    )

    def check(self, context: SetupContext) -> StepCheck:
        change = self._change(context)
        if change is None:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "brew prefix is unavailable",
                self.tags,
                self.risks,
            )
        if not change.changed:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                f"{change.path} loads dnsmasq.d",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            f"will update {change.path}",
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        change = self._change(context)
        if change is None:
            return StepResult(
                self.id, self.title, StepStatus.BLOCKED, "brew prefix is unavailable"
            )
        if not change.changed:
            return StepResult(
                self.id,
                self.title,
                StepStatus.PRESENT,
                f"{change.path} loads dnsmasq.d",
            )
        if context.dry_run:
            return StepResult(
                self.id,
                self.title,
                StepStatus.APPLIED,
                f"dry-run would update {change.path}",
            )
        backup = backup_file_for_context(change.path, context)
        atomic_write(change.path, change.after, change.mode_after)
        detail = f"updated {change.path}"
        if backup is not None:
            detail += f"; backup: {backup}"
        return StepResult(
            self.id,
            self.title,
            StepStatus.APPLIED,
            detail,
            changes=(
                _file_change_record(
                    "managed_block",
                    change,
                    marker="dnsmasq-conf-dir",
                    comment_prefix="#",
                ),
            ),
        )

    def preview(self, context: SetupContext) -> tuple[FileChange, ...]:
        change = self._change(context)
        return (change,) if change is not None and change.changed else ()

    def _change(self, context: SetupContext) -> FileChange | None:
        prefix = context.brew_prefix()
        if prefix is None:
            return None
        path = prefix / "etc" / "dnsmasq.conf"
        block = f"conf-dir={prefix}/etc/dnsmasq.d,*.conf"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        desired = upsert_block(
            existing, name="dnsmasq-conf-dir", content=block, comment_prefix="#"
        )
        return text_file_change(path, desired)


@dataclass(frozen=True)
class PrivilegedCommandStep:
    id: str
    title: str
    command: CommandSpec
    detail: str
    tags: frozenset[str]
    risks: frozenset[Risk] = _risks(Risk.PRIVILEGED)
    requires: frozenset[ResourceRef] = NO_RESOURCES
    provides: frozenset[ResourceRef] = NO_RESOURCES
    owns: frozenset[ResourceRef] = NO_RESOURCES

    def check(self, context: SetupContext) -> StepCheck:
        if not context.allow_privileged:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.BLOCKED,
                "requires --allow-privileged",
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.NEEDS_CHANGE,
            self.detail,
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        if not context.allow_privileged:
            return StepResult(
                self.id, self.title, StepStatus.BLOCKED, "requires --allow-privileged"
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
                    self.id, self.title, StepStatus.FAILED, detail, tuple(results)
                )
        result = context.runner.run(self.command, check=False)
        results.append(result)
        status = StepStatus.APPLIED if result.ok else StepStatus.FAILED
        detail = f"dry-run would {self.detail}" if context.dry_run else self.detail
        return StepResult(
            self.id,
            self.title,
            status,
            detail if result.ok else result.stderr.strip(),
            tuple(results),
        )


@dataclass(frozen=True)
class ManualStep:
    id: str
    title: str
    detail: str
    tags: frozenset[str]
    risks: frozenset[Risk] = _risks(Risk.MANUAL)
    requires: frozenset[ResourceRef] = NO_RESOURCES
    provides: frozenset[ResourceRef] = NO_RESOURCES
    owns: frozenset[ResourceRef] = NO_RESOURCES

    def check(self, context: SetupContext) -> StepCheck:
        return StepCheck(
            self.id, self.title, StepStatus.MANUAL, self.detail, self.tags, self.risks
        )

    def apply(self, context: SetupContext) -> StepResult:
        return StepResult(self.id, self.title, StepStatus.MANUAL, self.detail)


@dataclass(frozen=True)
class ManualAppStep:
    app: ManualApp
    risks: frozenset[Risk] = _risks(Risk.MANUAL)
    requires: frozenset[ResourceRef] = NO_RESOURCES
    provides: frozenset[ResourceRef] = NO_RESOURCES
    owns: frozenset[ResourceRef] = NO_RESOURCES

    @property
    def id(self) -> str:
        return f"manual.app.{_slug(self.app.name)}"

    @property
    def title(self) -> str:
        return f"Install {self.app.name}"

    @property
    def tags(self) -> frozenset[str]:
        return frozenset(self.app.tags)

    def check(self, context: SetupContext) -> StepCheck:
        existing = self._existing_app_bundles(context)
        if existing:
            return StepCheck(
                self.id,
                self.title,
                StepStatus.PRESENT,
                "installed: " + ", ".join(existing),
                self.tags,
                self.risks,
            )
        return StepCheck(
            self.id,
            self.title,
            StepStatus.MANUAL,
            self._detail(),
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        check = self.check(context)
        return StepResult(self.id, self.title, check.status, check.detail)

    def _detail(self) -> str:
        return f"Install from {self.app.install_method}: {self.app.url}"

    def _existing_app_bundles(self, context: SetupContext) -> tuple[str, ...]:
        roots = (Path("/Applications"), context.home / "Applications")
        existing = []
        for root in roots:
            for bundle in self.app.app_bundles:
                candidate = root / bundle
                if candidate.exists():
                    existing.append(str(candidate))
        return tuple(existing)


@dataclass(frozen=True)
class ManualNoteStep:
    note: ManualNote
    risks: frozenset[Risk] = _risks(Risk.MANUAL)
    requires: frozenset[ResourceRef] = NO_RESOURCES
    provides: frozenset[ResourceRef] = NO_RESOURCES
    owns: frozenset[ResourceRef] = NO_RESOURCES

    @property
    def id(self) -> str:
        return f"manual.note.{_slug(self.note.name)}"

    @property
    def title(self) -> str:
        return self.note.name

    @property
    def tags(self) -> frozenset[str]:
        return frozenset(self.note.tags)

    def check(self, context: SetupContext) -> StepCheck:
        return StepCheck(
            self.id,
            self.title,
            StepStatus.MANUAL,
            self.note.detail,
            self.tags,
            self.risks,
        )

    def apply(self, context: SetupContext) -> StepResult:
        return StepResult(self.id, self.title, StepStatus.MANUAL, self.note.detail)


def build_steps(recipe: SetupRecipe = DEFAULT_RECIPE) -> tuple[Step, ...]:
    return (
        XcodeDeveloperDirectoryStep(),
        XcodeLicenseStep(),
        XcodeMetalToolchainStep(),
        SudoTouchIdStep(),
        HomebrewInstallStep(),
        HomebrewAnalyticsStep(),
        HomebrewSharePermissionsStep(),
        HomebrewPhantomJsCleanupStep(),
        *_brew_package_steps(recipe.brew_formulas, cask=False),
        *_brew_package_steps(recipe.brew_casks, cask=True),
        *(ManualAppStep(app) for app in recipe.manual_apps),
        *(ManualNoteStep(note) for note in recipe.manual_notes),
        *_macos_default_directory_steps(recipe.macos_defaults),
        *(MacDefaultsStep(setting) for setting in recipe.macos_defaults),
        ZshCompletionPermissionsStep(),
        *_zprofile_block_steps(),
        ManagedBlockStep(
            "shell.local-bin-path",
            "Add ~/.local/bin to PATH",
            "~/.zprofile",
            "local-bin-path",
            content.LOCAL_BIN_PATH_BLOCK,
            adopt_existing_chunks=True,
            tags=_tags("shell", "files", "sync", "packages"),
        ),
        GitLogPagerStep(),
        OhMyZshStep(),
        OhMyZshThemeStep(),
        *_zshrc_block_steps(),
        *_inputrc_block_steps(),
        ManagedFileStep(
            "sync.rsync-global-filter",
            "Deploy managed mrsync global filter",
            "~/.config/macsetup/rsync/global.filter",
            content.RSYNC_GLOBAL_FILTER,
            tags=_tags("sync", "files"),
        ),
        ManagedFileStep(
            "sync.rsync-local-filter-example",
            "Deploy mrsync local filter example",
            "~/.config/macsetup/rsync/local.filter.example",
            content.RSYNC_LOCAL_FILTER_EXAMPLE,
            tags=_tags("sync", "files"),
        ),
        MrsyncLauncherStep(),
        ManagedBlockStep(
            "sync.mrsync-shell",
            "Deploy mrsync shell function",
            "~/.zshrc",
            "mrsync",
            content.MRSYNC_SHELL_BLOCK,
            tags=_tags("sync", "shell", "files"),
        ),
        ManagedBlockStep(
            "git.global-ignore",
            "Deploy global gitignore entries",
            "~/.gitignore_global",
            "gitignore",
            content.GLOBAL_GITIGNORE,
            adopt_existing_chunks=True,
            tags=_tags("git", "files"),
        ),
        *(GitConfigStep(setting) for setting in recipe.git_settings),
        *_pyenv_python_steps(recipe),
        PythonPackagesStep(recipe.python_global_packages, recipe.python_version),
        NpmGlobalPackagesStep(recipe.npm_global_packages),
        *_source_build_steps(recipe.source_builds),
        *_source_build_path_steps(recipe.source_builds),
        ManagedFileStep(
            "python.ipython-startup",
            "Deploy IPython startup imports",
            "~/.ipython/profile_default/startup/00-macsetup.py",
            content.IPYTHON_STARTUP,
            tags=_tags("python", "files"),
        ),
        ManagedBlockStep(
            "editor.nvim-init",
            "Load macsetup Neovim module",
            "~/.config/nvim/init.lua",
            "nvim-init",
            content.NVIM_INIT_BLOCK,
            comment_prefix="--",
            adopt_existing_chunks=True,
            tags=_tags("editor", "files"),
        ),
        ManagedFileStep(
            "editor.nvim-module",
            "Deploy macsetup Neovim module",
            "~/.config/nvim/lua/macsetup/init.lua",
            content.NVIM_MACSETUP_LUA,
            tags=_tags("editor", "files"),
        ),
        DnsmasqMainConfigStep(),
        DnsmasqConfigStep(recipe),
        PrivilegedCommandStep(
            id="dns.restart-dnsmasq",
            title="Restart dnsmasq as a privileged Homebrew service",
            command=CommandSpec(
                argv=("sudo", "brew", "services", "restart", "dnsmasq")
            ),
            detail="dnsmasq restarted",
            tags=_tags("dns"),
            requires=frozenset(
                {
                    resource("config", "dnsmasq.main"),
                    resource("config", "dnsmasq.snippet"),
                }
            ),
            provides=frozenset({resource("service", "dnsmasq")}),
            owns=frozenset({resource("service", "dnsmasq")}),
        ),
        PrivilegedCommandStep(
            id="dns.flush-cache",
            title="Flush macOS DNS caches",
            command=CommandSpec(
                shell="sudo killall -HUP mDNSResponder && sudo dscacheutil -flushcache"
            ),
            detail="DNS caches flushed",
            tags=_tags("dns"),
            requires=frozenset({resource("service", "dnsmasq")}),
            provides=frozenset({resource("cache", "macos-dns-flushed")}),
            owns=frozenset({resource("cache", "macos-dns-flushed")}),
        ),
        ManualStep(
            id="macos.network-dns",
            title="Set active network DNS server to 127.0.0.1 when using dnsmasq",
            detail="After dnsmasq is running, set the active network service DNS server to 127.0.0.1 in macOS Network Settings.",
            tags=_tags("dns", "manual"),
        ),
        ManualStep(
            id="macos.privacy-prompts",
            title="Pre-trigger macOS protected directory prompts",
            detail='From the home directory, run "fd > /dev/null" or "find ." and approve Terminal access prompts you want enabled.',
            tags=_tags("macos", "manual"),
        ),
        ManualStep(
            id="browser.dns-over-https",
            title="Review browser DNS-over-HTTPS settings",
            detail="If local dnsmasq filtering should apply inside browsers, disable browser DNS-over-HTTPS and flush browser DNS caches.",
            tags=_tags("dns", "browser", "manual"),
        ),
        ManualStep(
            id="editor.nvim-sync",
            title="Sync Neovim plugins",
            detail='Open nvim and run ":Lazy sync"; Mason will install configured LSP servers.',
            tags=_tags("editor", "manual"),
        ),
    )


def _macos_default_directory_steps(
    settings: Sequence[MacDefaultsSetting],
) -> tuple[ManagedDirectoryStep, ...]:
    ordered: list[str] = []
    tags_by_path: dict[str, set[str]] = {}
    for setting in settings:
        for directory in setting.directories:
            if directory not in tags_by_path:
                ordered.append(directory)
                tags_by_path[directory] = {"files"}
            tags_by_path[directory].update(setting.tags)
    return tuple(
        ManagedDirectoryStep(path, tags=frozenset(tags_by_path[path]))
        for path in ordered
    )


def _managed_block_file_steps(
    blocks: Sequence[tuple[str, str]],
    *,
    path: str,
    id_prefix: str,
    marker_prefix: str,
    titles: dict[str, str],
    default_title: str,
    adopt_patterns: dict[str, tuple[str, ...]],
) -> tuple[ManagedBlockStep, ...]:
    """Build one ManagedBlockStep per feature block in a multi-block file.

    Every step for the file shares the same canonical `order` (the marker names in
    template order), and each step after the first `requires` the previous block's
    marker. Together these make both file placement and graph run-order match the
    template, so ordering invariants (e.g. zsh history settings after oh-my-zsh) can
    no longer be broken by incidental step execution order.
    """
    order = tuple(f"{marker_prefix}{name}" for name, _ in blocks)
    steps: list[ManagedBlockStep] = []
    previous_marker: str | None = None
    for name, body in blocks:
        marker = f"{marker_prefix}{name}"
        steps.append(
            ManagedBlockStep(
                id=f"{id_prefix}{name}",
                title=titles.get(name, default_title.format(name=name)),
                path=path,
                marker=marker,
                block=body,
                adopt_existing_chunks=True,
                adopt_existing_patterns=adopt_patterns.get(name, ()),
                order=order,
                previous_marker=previous_marker,
                tags=_tags("shell", "files"),
            )
        )
        previous_marker = marker
    return tuple(steps)


def _zprofile_block_steps() -> tuple[ManagedBlockStep, ...]:
    return _managed_block_file_steps(
        content.ZPROFILE_BLOCKS,
        path="~/.zprofile",
        id_prefix="shell.zprofile.",
        marker_prefix="zprofile-",
        titles={
            "homebrew": "Deploy zprofile Homebrew shellenv block",
            "pyenv": "Deploy zprofile pyenv initialization",
        },
        default_title="Deploy zprofile {name} block",
        adopt_patterns={
            "homebrew": (r"\bbrew\s+shellenv\b",),
            "pyenv": (r"\bpyenv\s+init\s+--path\b",),
        },
    )


def _zshrc_block_steps() -> tuple[ManagedBlockStep, ...]:
    return _managed_block_file_steps(
        content.ZSHRC_BLOCKS,
        path="~/.zshrc",
        id_prefix="shell.zshrc.",
        marker_prefix="zshrc-",
        titles={
            "env": "Deploy zsh environment defaults",
            "history": "Deploy zsh history and key bindings",
            "oh-my-zsh": "Deploy oh-my-zsh shell setup",
            "options": "Deploy zsh option defaults",
            "aliases": "Deploy zsh aliases",
            "optional-tools": "Deploy optional tool shell integration",
            "completions": "Deploy zsh Homebrew completions",
            "pyenv": "Deploy zsh pyenv initialization",
            "prompt": "Preserve oh-my-zsh theme prompt setup",
            "functions": "Deploy zsh helper functions",
        },
        default_title="Deploy zsh {name} block",
        adopt_patterns={
            "history": (
                r"\bup-line-or-beginning-search\b",
                r"\bdown-line-or-beginning-search\b",
            ),
            "oh-my-zsh": (r"oh-my-zsh\.sh",),
            "options": (
                r"\bsetopt\s+(?:auto_cd|autocd)\b",
                r"\bsetopt\s+menu_complete\b",
            ),
            "optional-tools": (r"\bBUN_INSTALL\b", r"\bbroot/launcher\b"),
            "completions": (r"\bcompinit\b",),
            "pyenv": (r"\bpyenv\s+init\s+-\s+zsh\b",),
            "prompt": (r"^\s*(?:PROMPT|RPROMPT)=",),
        },
    )


def _inputrc_block_steps() -> tuple[ManagedBlockStep, ...]:
    return _managed_block_file_steps(
        content.INPUTRC_BLOCKS,
        path="~/.inputrc",
        id_prefix="shell.inputrc.",
        marker_prefix="inputrc-",
        titles={
            "completion": "Deploy readline completion defaults",
            "history": "Deploy readline history search bindings",
        },
        default_title="Deploy readline {name} block",
        adopt_patterns={
            "history": (r"\bhistory-search-backward\b", r"\bhistory-search-forward\b"),
        },
    )


def _pyenv_python_steps(recipe: SetupRecipe) -> tuple[PyenvPythonStep, ...]:
    extra_versions = [
        version
        for version in recipe.python_versions
        if version != recipe.python_version
    ]
    return (
        *(
            PyenvPythonStep(
                version,
                recipe.python_tooling_packages,
                recipe.python_build_jobs,
                recipe.python_build_env,
                select_global=False,
            )
            for version in extra_versions
        ),
        PyenvPythonStep(
            recipe.python_version,
            recipe.python_tooling_packages,
            recipe.python_build_jobs,
            recipe.python_build_env,
            select_global=True,
        ),
    )


def _source_build_steps(
    packages: Sequence[SourceBuildPackage],
) -> tuple[SourceBuildStep, ...]:
    return tuple(SourceBuildStep(package) for package in packages)


def _source_build_path_steps(
    packages: Sequence[SourceBuildPackage],
) -> tuple[ManagedBlockStep, ...]:
    steps = []
    for package in packages:
        path_entry = _source_build_path_entry(package)
        if path_entry is None or not package.manage_path:
            continue
        steps.append(
            ManagedBlockStep(
                id=f"source-build.{package.name}.path",
                title=f"Add {package.name} source build binary directory to PATH",
                path="~/.zprofile",
                marker=f"source-build-{package.name}-path",
                block=_source_build_path_block(path_entry),
                adopt_existing_chunks=True,
                tags=frozenset({*package.tags, "shell", "files"}),
            )
        )
    return tuple(steps)


def _source_build_path_entry(package: SourceBuildPackage) -> str | None:
    if package.install_mode == "path":
        source_dir = package.source_dir or f"~/.local/src/macsetup/{package.name}"
        return _join_profile_path(source_dir, package.binary_dir)
    if _is_default_local_bin(package.install_dir):
        return None
    return package.install_dir


def _join_profile_path(base: str, child: str) -> str:
    if not child:
        return base
    return base.rstrip("/") + "/" + child.lstrip("/")


def _is_default_local_bin(path: str) -> bool:
    return path in {"~/.local/bin", "$HOME/.local/bin"}


def _source_build_path_block(path: str) -> str:
    shell_path = _shell_path_expr(path)
    return f"""
macsetup_source_build_path="{shell_path}"
if [[ -d "$macsetup_source_build_path" ]]; then
  case ":$PATH:" in
    *":$macsetup_source_build_path:"*) ;;
    *) export PATH="$macsetup_source_build_path:$PATH" ;;
  esac
fi
unset macsetup_source_build_path
""".strip()


def _shell_path_expr(path: str) -> str:
    if path.startswith("~/"):
        path = "$HOME/" + path[2:]
    return path.replace('"', r"\"")


def _brew_package_steps(
    packages: Sequence[BrewPackage], *, cask: bool
) -> tuple[BrewPackagesStep, ...]:
    grouped: dict[tuple[str, ...], list[BrewPackage]] = {}
    for package in packages:
        grouped.setdefault(tuple(sorted(package.tags)), []).append(package)
    prefix = "cask" if cask else "formula"
    return tuple(
        BrewPackagesStep(
            id=f"brew.{prefix}s.{_tag_slug(tags)}",
            title=f"Install Homebrew {prefix}s for {_tag_slug(tags)}",
            packages=tuple(group),
            cask=cask,
            tags=frozenset(tags),
        )
        for tags, group in sorted(grouped.items(), key=lambda item: item[0])
    )


def _tag_slug(tags: Sequence[str]) -> str:
    meaningful = [tag for tag in tags if tag != "packages"]
    return "-".join(meaningful or ["packages"])


def _slug(value: str) -> str:
    result = []
    previous_dash = False
    for character in value.lower():
        if character.isalnum():
            result.append(character)
            previous_dash = False
        elif not previous_dash:
            result.append("-")
            previous_dash = True
    return "".join(result).strip("-") or "app"
