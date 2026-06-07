from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BrewPackage:
    name: str
    tags: tuple[str, ...] = ("packages",)
    note: str = ""
    app_bundles: tuple[str, ...] = ()
    installer_bundles: tuple[str, ...] = ()


@dataclass(frozen=True)
class GitSetting:
    key: str
    value: str


@dataclass(frozen=True)
class MacDefaultsSetting:
    name: str
    domain: str
    key: str
    value: str
    value_type: str = "string"
    directories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ("macos", "defaults")


@dataclass(frozen=True)
class ManualApp:
    name: str
    install_method: str
    url: str
    tags: tuple[str, ...] = ("packages", "apps", "manual")
    app_bundles: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManualNote:
    name: str
    detail: str
    tags: tuple[str, ...] = ("macos", "manual")


@dataclass(frozen=True)
class SourceBuildPackage:
    name: str
    repo: str
    build_system: str = "cargo"
    binaries: tuple[str, ...] = ()
    tags: tuple[str, ...] = ("packages", "source-build")
    ref: str = ""
    source_dir: str = ""
    binary_dir: str = ""
    install_mode: str = "copy"
    install_dir: str = "~/.local/bin"
    build_commands: tuple[str, ...] = ()
    brew_dependencies: tuple[str, ...] = ()
    submodules: bool = False
    update: bool = True
    manage_path: bool = True
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SetupRecipe:
    python_version: str
    python_versions: tuple[str, ...]
    python_tooling_packages: tuple[str, ...]
    python_build_jobs: str
    python_build_env: tuple[tuple[str, str], ...]
    brew_formulas: tuple[BrewPackage, ...]
    brew_casks: tuple[BrewPackage, ...]
    npm_global_packages: tuple[str, ...]
    python_global_packages: tuple[str, ...]
    source_builds: tuple[SourceBuildPackage, ...]
    git_settings: tuple[GitSetting, ...]
    macos_defaults: tuple[MacDefaultsSetting, ...]
    manual_apps: tuple[ManualApp, ...]
    manual_notes: tuple[ManualNote, ...]
    dns_servers: tuple[str, ...]
    dns_passthrough_domains: tuple[str, ...]
    dns_blocked_domains: tuple[str, ...]


@dataclass(frozen=True)
class _RejectPolicy:
    brew_formulas: frozenset[str] = frozenset()
    brew_casks: frozenset[str] = frozenset()
    python_packages: frozenset[str] = frozenset()
    python_tooling_packages: frozenset[str] = frozenset()
    npm_packages: frozenset[str] = frozenset()
    source_builds: frozenset[str] = frozenset()
    git_settings: frozenset[str] = frozenset()
    macos_defaults: frozenset[str] = frozenset()
    manual_apps: frozenset[str] = frozenset()
    manual_notes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _RecipeState:
    recipe: SetupRecipe
    rejects: _RejectPolicy


class ProfileLoadError(ValueError):
    """Raised when a TOML profile cannot be compiled into a valid recipe."""


_EMPTY_RECIPE = SetupRecipe(
    python_version="",
    python_versions=(),
    python_tooling_packages=(),
    python_build_jobs="auto",
    python_build_env=(),
    brew_formulas=(),
    brew_casks=(),
    npm_global_packages=(),
    python_global_packages=(),
    source_builds=(),
    git_settings=(),
    macos_defaults=(),
    manual_apps=(),
    manual_notes=(),
    dns_servers=(),
    dns_passthrough_domains=(),
    dns_blocked_domains=(),
)


def load_recipe(profile_paths: Iterable[Path | str] = ()) -> SetupRecipe:
    state = _RecipeState(recipe=_EMPTY_RECIPE, rejects=_RejectPolicy())
    state = _apply_profile(state, _load_default_profile(), "packaged default profile")
    for profile_path in profile_paths:
        path = Path(profile_path).expanduser()
        state = _apply_profile(state, _load_profile_path(path), str(path))
    _validate_state(state)
    return state.recipe


def _load_default_profile() -> Mapping[str, Any]:
    text = (
        files("macsetup.defaults").joinpath("profile.toml").read_text(encoding="utf-8")
    )
    return tomllib.loads(text)


def _load_profile_path(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as error:
        raise ProfileLoadError(f"profile not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ProfileLoadError(f"invalid TOML in {path}: {error}") from error


def _apply_profile(
    state: _RecipeState, data: Mapping[str, Any], source: str
) -> _RecipeState:
    recipe = state.recipe
    python = _optional_mapping(data, "python", source)
    javascript = _optional_mapping(data, "javascript", source)
    brew = _optional_mapping(data, "brew", source)
    source_builds_data = _optional_mapping(data, "source_builds", source)
    git = _optional_mapping(data, "git", source)
    macos = _optional_mapping(data, "macos", source)
    manual = _optional_mapping(data, "manual", source)
    dns = _optional_mapping(data, "dns", source)

    python_version = (
        _optional_string(python, "version", f"{source}.python") or recipe.python_version
    )
    python_versions = _python_versions_for_profile(
        current_version=recipe.python_version,
        current_versions=recipe.python_versions,
        next_version=python_version,
        explicit_versions=_optional_string_tuple(
            python, "versions", f"{source}.python"
        ),
    )
    python_tooling_packages = _apply_string_filters(
        current=recipe.python_tooling_packages,
        explicit=_optional_string_tuple(python, "tooling_packages", f"{source}.python"),
        extend=_optional_string_tuple(
            python, "tooling_package_extend", f"{source}.python"
        ),
        deny=_optional_string_tuple(python, "tooling_package_deny", f"{source}.python"),
        allow=_optional_string_tuple(
            python, "tooling_package_allow", f"{source}.python"
        ),
        reject=_optional_string_tuple(
            python, "tooling_package_reject", f"{source}.python"
        ),
        source=f"{source}.python.tooling_packages",
    )
    python_build_jobs = (
        _optional_python_build_jobs(python, "build_jobs", f"{source}.python")
        or recipe.python_build_jobs
    )
    python_build_env = _apply_string_mapping(
        current=recipe.python_build_env,
        override=_optional_string_mapping(python, "build_env", f"{source}.python")
        if "build_env" in python
        else None,
    )
    python_packages = _apply_string_filters(
        current=recipe.python_global_packages,
        explicit=_optional_string_tuple(python, "global_packages", f"{source}.python"),
        extend=_optional_string_tuple(
            python, "global_package_extend", f"{source}.python"
        ),
        deny=_optional_string_tuple(python, "global_package_deny", f"{source}.python"),
        allow=_optional_string_tuple(
            python, "global_package_allow", f"{source}.python"
        ),
        reject=_optional_string_tuple(
            python, "global_package_reject", f"{source}.python"
        ),
        source=f"{source}.python.global_packages",
    )
    npm_packages = _apply_string_filters(
        current=recipe.npm_global_packages,
        explicit=_optional_string_tuple(
            javascript, "npm_global_packages", f"{source}.javascript"
        ),
        extend=_optional_string_tuple(
            javascript, "npm_package_extend", f"{source}.javascript"
        ),
        deny=_optional_string_tuple(
            javascript, "npm_package_deny", f"{source}.javascript"
        ),
        allow=_optional_string_tuple(
            javascript, "npm_package_allow", f"{source}.javascript"
        ),
        reject=_optional_string_tuple(
            javascript, "npm_package_reject", f"{source}.javascript"
        ),
        source=f"{source}.javascript.npm_global_packages",
    )

    formulas = _apply_package_filters(
        current=recipe.brew_formulas,
        explicit=_optional_packages(brew, "formulas", f"{source}.brew"),
        extend=_optional_packages(brew, "formula_extend", f"{source}.brew"),
        deny=_optional_string_tuple(brew, "formula_deny", f"{source}.brew"),
        allow=_optional_string_tuple(brew, "formula_allow", f"{source}.brew"),
        reject=_optional_string_tuple(brew, "formula_reject", f"{source}.brew"),
        source=f"{source}.brew.formulas",
    )
    casks = _apply_package_filters(
        current=recipe.brew_casks,
        explicit=_optional_packages(brew, "casks", f"{source}.brew"),
        extend=_optional_packages(brew, "cask_extend", f"{source}.brew"),
        deny=_optional_string_tuple(brew, "cask_deny", f"{source}.brew"),
        allow=_optional_string_tuple(brew, "cask_allow", f"{source}.brew"),
        reject=_optional_string_tuple(brew, "cask_reject", f"{source}.brew"),
        source=f"{source}.brew.casks",
    )
    source_builds = _apply_source_build_filters(
        current=recipe.source_builds,
        explicit=_optional_source_builds(
            source_builds_data, "packages", f"{source}.source_builds"
        ),
        extend=_optional_source_builds(
            source_builds_data, "package_extend", f"{source}.source_builds"
        ),
        deny=_optional_string_tuple(
            source_builds_data, "package_deny", f"{source}.source_builds"
        ),
        allow=_optional_string_tuple(
            source_builds_data, "package_allow", f"{source}.source_builds"
        ),
        reject=_optional_string_tuple(
            source_builds_data, "package_reject", f"{source}.source_builds"
        ),
        source=f"{source}.source_builds.packages",
    )
    formulas = _with_source_build_dependencies(formulas, source_builds)
    git_settings = _apply_git_filters(
        current=recipe.git_settings,
        explicit=_optional_git_settings(git, "settings", f"{source}.git"),
        extend=_optional_git_settings(git, "setting_extend", f"{source}.git"),
        deny=_optional_string_tuple(git, "setting_deny", f"{source}.git"),
        allow=_optional_string_tuple(git, "setting_allow", f"{source}.git"),
        reject=_optional_string_tuple(git, "setting_reject", f"{source}.git"),
        source=f"{source}.git.settings",
    )
    macos_defaults = _apply_macos_defaults_filters(
        current=recipe.macos_defaults,
        explicit=_optional_macos_defaults(macos, "defaults", f"{source}.macos"),
        extend=_optional_macos_defaults(macos, "default_extend", f"{source}.macos"),
        deny=_optional_string_tuple(macos, "default_deny", f"{source}.macos"),
        allow=_optional_string_tuple(macos, "default_allow", f"{source}.macos"),
        reject=_optional_string_tuple(macos, "default_reject", f"{source}.macos"),
        source=f"{source}.macos.defaults",
    )
    manual_apps = _apply_manual_app_filters(
        current=recipe.manual_apps,
        explicit=_optional_manual_apps(manual, "apps", f"{source}.manual"),
        extend=_optional_manual_apps(manual, "app_extend", f"{source}.manual"),
        deny=_optional_string_tuple(manual, "app_deny", f"{source}.manual"),
        allow=_optional_string_tuple(manual, "app_allow", f"{source}.manual"),
        reject=_optional_string_tuple(manual, "app_reject", f"{source}.manual"),
        source=f"{source}.manual.apps",
    )
    manual_notes = _apply_manual_note_filters(
        current=recipe.manual_notes,
        explicit=_optional_manual_notes(manual, "notes", f"{source}.manual"),
        extend=_optional_manual_notes(manual, "note_extend", f"{source}.manual"),
        deny=_optional_string_tuple(manual, "note_deny", f"{source}.manual"),
        allow=_optional_string_tuple(manual, "note_allow", f"{source}.manual"),
        reject=_optional_string_tuple(manual, "note_reject", f"{source}.manual"),
        source=f"{source}.manual.notes",
    )

    next_recipe = SetupRecipe(
        python_version=python_version,
        python_versions=python_versions,
        python_tooling_packages=python_tooling_packages,
        python_build_jobs=python_build_jobs,
        python_build_env=python_build_env,
        brew_formulas=formulas,
        brew_casks=casks,
        npm_global_packages=npm_packages,
        python_global_packages=python_packages,
        source_builds=source_builds,
        git_settings=git_settings,
        macos_defaults=macos_defaults,
        manual_apps=manual_apps,
        manual_notes=manual_notes,
        dns_servers=_optional_string_tuple(dns, "servers", f"{source}.dns")
        or recipe.dns_servers,
        dns_passthrough_domains=_optional_string_tuple(
            dns, "passthrough_domains", f"{source}.dns"
        )
        or recipe.dns_passthrough_domains,
        dns_blocked_domains=_optional_string_tuple(
            dns, "blocked_domains", f"{source}.dns"
        )
        or recipe.dns_blocked_domains,
    )
    next_rejects = _RejectPolicy(
        brew_formulas=state.rejects.brew_formulas
        | frozenset(
            _optional_string_tuple(brew, "formula_reject", f"{source}.brew") or ()
        ),
        brew_casks=state.rejects.brew_casks
        | frozenset(
            _optional_string_tuple(brew, "cask_reject", f"{source}.brew") or ()
        ),
        python_packages=state.rejects.python_packages
        | frozenset(
            _optional_string_tuple(python, "global_package_reject", f"{source}.python")
            or ()
        ),
        python_tooling_packages=state.rejects.python_tooling_packages
        | frozenset(
            _optional_string_tuple(python, "tooling_package_reject", f"{source}.python")
            or ()
        ),
        npm_packages=state.rejects.npm_packages
        | frozenset(
            _optional_string_tuple(
                javascript, "npm_package_reject", f"{source}.javascript"
            )
            or ()
        ),
        source_builds=state.rejects.source_builds
        | frozenset(
            _optional_string_tuple(
                source_builds_data, "package_reject", f"{source}.source_builds"
            )
            or ()
        ),
        git_settings=state.rejects.git_settings
        | frozenset(
            _optional_string_tuple(git, "setting_reject", f"{source}.git") or ()
        ),
        macos_defaults=state.rejects.macos_defaults
        | frozenset(
            _optional_string_tuple(macos, "default_reject", f"{source}.macos") or ()
        ),
        manual_apps=state.rejects.manual_apps
        | frozenset(
            _optional_string_tuple(manual, "app_reject", f"{source}.manual") or ()
        ),
        manual_notes=state.rejects.manual_notes
        | frozenset(
            _optional_string_tuple(manual, "note_reject", f"{source}.manual") or ()
        ),
    )
    next_state = _RecipeState(recipe=next_recipe, rejects=next_rejects)
    _validate_state(next_state)
    return next_state


def _python_versions_for_profile(
    *,
    current_version: str,
    current_versions: tuple[str, ...],
    next_version: str,
    explicit_versions: tuple[str, ...] | None,
) -> tuple[str, ...]:
    if explicit_versions is not None:
        return _ordered_unique((*explicit_versions, next_version))
    if next_version and next_version != current_version:
        return (next_version,)
    return current_versions or ((next_version,) if next_version else ())


def _apply_string_mapping(
    *,
    current: tuple[tuple[str, str], ...],
    override: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    values = dict(current)
    if override is not None:
        values.update(override)
    return tuple(sorted(values.items()))


def _apply_package_filters(
    *,
    current: tuple[BrewPackage, ...],
    explicit: tuple[BrewPackage, ...] | None,
    extend: tuple[BrewPackage, ...] | None,
    deny: tuple[str, ...] | None,
    allow: tuple[str, ...] | None,
    reject: tuple[str, ...] | None,
    source: str,
) -> tuple[BrewPackage, ...]:
    ordered: list[str] = []
    by_name: dict[str, BrewPackage] = {}
    for package in explicit if explicit is not None else current:
        _put_package(package, ordered, by_name)
    for package in extend or ():
        _put_package(package, ordered, by_name)
    denied = set(deny or ())
    allowed = set(allow or ())
    rejected = set(reject or ())
    if denied:
        for name in denied:
            by_name.pop(name, None)
    if allowed:
        by_name = {
            name: package for name, package in by_name.items() if name in allowed
        }
    selected_rejected = sorted(set(by_name) & rejected)
    if selected_rejected:
        raise ProfileLoadError(
            f"{source} contains rejected package(s): {', '.join(selected_rejected)}"
        )
    return tuple(by_name[name] for name in ordered if name in by_name)


def _put_package(
    package: BrewPackage, ordered: list[str], by_name: dict[str, BrewPackage]
) -> None:
    if package.name not in by_name:
        ordered.append(package.name)
    by_name[package.name] = package


def _with_source_build_dependencies(
    formulas: tuple[BrewPackage, ...], source_builds: tuple[SourceBuildPackage, ...]
) -> tuple[BrewPackage, ...]:
    ordered = [package.name for package in formulas]
    by_name = {package.name: package for package in formulas}
    for dependency in _source_build_brew_dependencies(source_builds):
        if dependency in by_name:
            continue
        _put_package(
            BrewPackage(
                dependency,
                tags=("packages", "source-build", "build"),
                note="Automatically required by configured source build packages.",
            ),
            ordered,
            by_name,
        )
    return tuple(by_name[name] for name in ordered if name in by_name)


def _source_build_brew_dependencies(
    packages: tuple[SourceBuildPackage, ...],
) -> tuple[str, ...]:
    dependencies: list[str] = []
    for package in packages:
        for dependency in ("git", *_build_system_dependencies(package)):
            if dependency not in dependencies:
                dependencies.append(dependency)
        for dependency in package.brew_dependencies:
            if dependency not in dependencies:
                dependencies.append(dependency)
    return tuple(dependencies)


def _build_system_dependencies(package: SourceBuildPackage) -> tuple[str, ...]:
    if package.build_system == "cargo":
        return ("rust",)
    if package.build_system == "zig":
        return ("zig",)
    return ()


def _apply_source_build_filters(
    *,
    current: tuple[SourceBuildPackage, ...],
    explicit: tuple[SourceBuildPackage, ...] | None,
    extend: tuple[SourceBuildPackage, ...] | None,
    deny: tuple[str, ...] | None,
    allow: tuple[str, ...] | None,
    reject: tuple[str, ...] | None,
    source: str,
) -> tuple[SourceBuildPackage, ...]:
    ordered: list[str] = []
    by_name: dict[str, SourceBuildPackage] = {}
    for package in explicit if explicit is not None else current:
        _put_source_build(package, ordered, by_name)
    for package in extend or ():
        _put_source_build(package, ordered, by_name)
    denied = set(deny or ())
    allowed = set(allow or ())
    rejected = set(reject or ())
    if denied:
        for name in denied:
            by_name.pop(name, None)
    if allowed:
        by_name = {
            name: package for name, package in by_name.items() if name in allowed
        }
    selected_rejected = sorted(set(by_name) & rejected)
    if selected_rejected:
        raise ProfileLoadError(
            f"{source} contains rejected source build(s): "
            + ", ".join(selected_rejected)
        )
    return tuple(by_name[name] for name in ordered if name in by_name)


def _put_source_build(
    package: SourceBuildPackage,
    ordered: list[str],
    by_name: dict[str, SourceBuildPackage],
) -> None:
    if package.name not in by_name:
        ordered.append(package.name)
    by_name[package.name] = package


def _apply_string_filters(
    *,
    current: tuple[str, ...],
    explicit: tuple[str, ...] | None,
    extend: tuple[str, ...] | None,
    deny: tuple[str, ...] | None,
    allow: tuple[str, ...] | None,
    reject: tuple[str, ...] | None,
    source: str,
) -> tuple[str, ...]:
    values = list(explicit if explicit is not None else current)
    for value in extend or ():
        if value not in values:
            values.append(value)
    denied = set(deny or ())
    allowed = set(allow or ())
    rejected = set(reject or ())
    values = [value for value in values if value not in denied]
    if allowed:
        values = [value for value in values if value in allowed]
    selected_rejected = sorted(set(values) & rejected)
    if selected_rejected:
        raise ProfileLoadError(
            f"{source} contains rejected item(s): {', '.join(selected_rejected)}"
        )
    return tuple(values)


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _apply_git_filters(
    *,
    current: tuple[GitSetting, ...],
    explicit: tuple[GitSetting, ...] | None,
    extend: tuple[GitSetting, ...] | None,
    deny: tuple[str, ...] | None,
    allow: tuple[str, ...] | None,
    reject: tuple[str, ...] | None,
    source: str,
) -> tuple[GitSetting, ...]:
    ordered: list[str] = []
    by_key: dict[str, GitSetting] = {}
    for setting in explicit if explicit is not None else current:
        _put_git_setting(setting, ordered, by_key)
    for setting in extend or ():
        _put_git_setting(setting, ordered, by_key)
    denied = set(deny or ())
    allowed = set(allow or ())
    rejected = set(reject or ())
    if denied:
        for key in denied:
            by_key.pop(key, None)
    if allowed:
        by_key = {key: setting for key, setting in by_key.items() if key in allowed}
    selected_rejected = sorted(set(by_key) & rejected)
    if selected_rejected:
        raise ProfileLoadError(
            f"{source} contains rejected setting(s): {', '.join(selected_rejected)}"
        )
    return tuple(by_key[key] for key in ordered if key in by_key)


def _put_git_setting(
    setting: GitSetting, ordered: list[str], by_key: dict[str, GitSetting]
) -> None:
    if setting.key not in by_key:
        ordered.append(setting.key)
    by_key[setting.key] = setting


def _apply_macos_defaults_filters(
    *,
    current: tuple[MacDefaultsSetting, ...],
    explicit: tuple[MacDefaultsSetting, ...] | None,
    extend: tuple[MacDefaultsSetting, ...] | None,
    deny: tuple[str, ...] | None,
    allow: tuple[str, ...] | None,
    reject: tuple[str, ...] | None,
    source: str,
) -> tuple[MacDefaultsSetting, ...]:
    ordered: list[str] = []
    by_name: dict[str, MacDefaultsSetting] = {}
    for setting in explicit if explicit is not None else current:
        _put_macos_default(setting, ordered, by_name)
    for setting in extend or ():
        _put_macos_default(setting, ordered, by_name)
    denied = set(deny or ())
    allowed = set(allow or ())
    rejected = set(reject or ())
    if denied:
        for name in denied:
            by_name.pop(name, None)
    if allowed:
        by_name = {
            name: setting for name, setting in by_name.items() if name in allowed
        }
    selected_rejected = sorted(set(by_name) & rejected)
    if selected_rejected:
        raise ProfileLoadError(
            f"{source} contains rejected default(s): " + ", ".join(selected_rejected)
        )
    return tuple(by_name[name] for name in ordered if name in by_name)


def _put_macos_default(
    setting: MacDefaultsSetting,
    ordered: list[str],
    by_name: dict[str, MacDefaultsSetting],
) -> None:
    if setting.name not in by_name:
        ordered.append(setting.name)
    by_name[setting.name] = setting


def _apply_manual_app_filters(
    *,
    current: tuple[ManualApp, ...],
    explicit: tuple[ManualApp, ...] | None,
    extend: tuple[ManualApp, ...] | None,
    deny: tuple[str, ...] | None,
    allow: tuple[str, ...] | None,
    reject: tuple[str, ...] | None,
    source: str,
) -> tuple[ManualApp, ...]:
    ordered: list[str] = []
    by_name: dict[str, ManualApp] = {}
    for app in explicit if explicit is not None else current:
        _put_manual_app(app, ordered, by_name)
    for app in extend or ():
        _put_manual_app(app, ordered, by_name)
    denied = set(deny or ())
    allowed = set(allow or ())
    rejected = set(reject or ())
    if denied:
        for name in denied:
            by_name.pop(name, None)
    if allowed:
        by_name = {name: app for name, app in by_name.items() if name in allowed}
    selected_rejected = sorted(set(by_name) & rejected)
    if selected_rejected:
        raise ProfileLoadError(
            f"{source} contains rejected app(s): {', '.join(selected_rejected)}"
        )
    return tuple(by_name[name] for name in ordered if name in by_name)


def _put_manual_app(
    app: ManualApp, ordered: list[str], by_name: dict[str, ManualApp]
) -> None:
    if app.name not in by_name:
        ordered.append(app.name)
    by_name[app.name] = app


def _apply_manual_note_filters(
    *,
    current: tuple[ManualNote, ...],
    explicit: tuple[ManualNote, ...] | None,
    extend: tuple[ManualNote, ...] | None,
    deny: tuple[str, ...] | None,
    allow: tuple[str, ...] | None,
    reject: tuple[str, ...] | None,
    source: str,
) -> tuple[ManualNote, ...]:
    ordered: list[str] = []
    by_name: dict[str, ManualNote] = {}
    for note in explicit if explicit is not None else current:
        _put_manual_note(note, ordered, by_name)
    for note in extend or ():
        _put_manual_note(note, ordered, by_name)
    denied = set(deny or ())
    allowed = set(allow or ())
    rejected = set(reject or ())
    if denied:
        for name in denied:
            by_name.pop(name, None)
    if allowed:
        by_name = {name: note for name, note in by_name.items() if name in allowed}
    selected_rejected = sorted(set(by_name) & rejected)
    if selected_rejected:
        raise ProfileLoadError(
            f"{source} contains rejected note(s): {', '.join(selected_rejected)}"
        )
    return tuple(by_name[name] for name in ordered if name in by_name)


def _put_manual_note(
    note: ManualNote, ordered: list[str], by_name: dict[str, ManualNote]
) -> None:
    if note.name not in by_name:
        ordered.append(note.name)
    by_name[note.name] = note


def _optional_mapping(
    data: Mapping[str, Any], key: str, source: str
) -> Mapping[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProfileLoadError(f"{source}.{key} must be a table")
    return value


def _optional_string(data: Mapping[str, Any], key: str, source: str) -> str | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str):
        raise ProfileLoadError(f"{source}.{key} must be a string")
    return value


def _optional_string_tuple(
    data: Mapping[str, Any], key: str, source: str
) -> tuple[str, ...] | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProfileLoadError(f"{source}.{key} must be an array of strings")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ProfileLoadError(f"{source}.{key}[{index}] must be a string")
        result.append(item)
    return tuple(result)


def _optional_python_build_jobs(
    data: Mapping[str, Any], key: str, source: str
) -> str | None:
    if key not in data:
        return None
    value = data[key]
    if isinstance(value, int):
        if value < 1:
            raise ProfileLoadError(f"{source}.{key} must be positive")
        return str(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"auto", "default"}:
            return normalized
        if normalized.isdecimal() and int(normalized) >= 1:
            return str(int(normalized))
    raise ProfileLoadError(
        f'{source}.{key} must be "auto", "default", or a positive integer'
    )


def _required_string_tuple(
    data: Mapping[str, Any], key: str, source: str
) -> tuple[str, ...]:
    value = _optional_string_tuple(data, key, source)
    if not value:
        raise ProfileLoadError(f"{source}.{key} must be a non-empty array of strings")
    return value


def _optional_string_mapping(
    data: Mapping[str, Any], key: str, source: str
) -> dict[str, str]:
    if key not in data:
        return {}
    value = data[key]
    if not isinstance(value, Mapping):
        raise ProfileLoadError(f"{source}.{key} must be a table of strings")
    result: dict[str, str] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str):
            raise ProfileLoadError(f"{source}.{key} keys must be strings")
        if not isinstance(item_value, str):
            raise ProfileLoadError(f"{source}.{key}.{item_key} must be a string")
        result[item_key] = item_value
    return result


def _optional_bool(data: Mapping[str, Any], key: str, source: str) -> bool | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, bool):
        raise ProfileLoadError(f"{source}.{key} must be a boolean")
    return value


def _optional_packages(
    data: Mapping[str, Any], key: str, source: str
) -> tuple[BrewPackage, ...] | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProfileLoadError(f"{source}.{key} must be an array of package tables")
    packages = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ProfileLoadError(f"{source}.{key}[{index}] must be a table")
        item_source = f"{source}.{key}[{index}]"
        name = _required_string(item, "name", item_source)
        packages.append(
            BrewPackage(
                name=name,
                tags=_optional_string_tuple(item, "tags", item_source) or ("packages",),
                note=_optional_string(item, "note", item_source) or "",
                app_bundles=_optional_string_tuple(item, "app_bundles", item_source)
                or (),
                installer_bundles=_optional_string_tuple(
                    item, "installer_bundles", item_source
                )
                or (),
            )
        )
    return tuple(packages)


def _optional_source_builds(
    data: Mapping[str, Any], key: str, source: str
) -> tuple[SourceBuildPackage, ...] | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProfileLoadError(f"{source}.{key} must be an array of package tables")
    packages = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ProfileLoadError(f"{source}.{key}[{index}] must be a table")
        item_source = f"{source}.{key}[{index}]"
        build_system = _optional_string(item, "build_system", item_source) or "cargo"
        build_commands = _optional_string_tuple(item, "build_commands", item_source)
        binary_dir = _optional_string(item, "binary_dir", item_source)
        install_mode = _optional_string(item, "install_mode", item_source) or "copy"
        package = SourceBuildPackage(
            name=_required_string(item, "name", item_source),
            repo=_required_string(item, "repo", item_source),
            build_system=build_system,
            binaries=_required_string_tuple(item, "binaries", item_source),
            tags=_optional_string_tuple(item, "tags", item_source)
            or ("packages", "source-build"),
            ref=_optional_string(item, "ref", item_source) or "",
            source_dir=_optional_string(item, "source_dir", item_source) or "",
            binary_dir=binary_dir
            if binary_dir is not None
            else _default_binary_dir(build_system),
            install_mode=install_mode,
            install_dir=_optional_string(item, "install_dir", item_source)
            or "~/.local/bin",
            build_commands=build_commands
            if build_commands is not None
            else _default_build_commands(build_system, item_source),
            brew_dependencies=_optional_string_tuple(
                item, "brew_dependencies", item_source
            )
            or (),
            submodules=_optional_bool(item, "submodules", item_source) or False,
            update=True
            if _optional_bool(item, "update", item_source) is None
            else bool(_optional_bool(item, "update", item_source)),
            manage_path=True
            if _optional_bool(item, "manage_path", item_source) is None
            else bool(_optional_bool(item, "manage_path", item_source)),
            env=tuple(
                sorted(
                    _optional_string_mapping(item, "env", item_source).items()
                    if "env" in item
                    else ()
                )
            ),
        )
        _validate_source_build(package, item_source)
        packages.append(package)
    return tuple(packages)


def _default_binary_dir(build_system: str) -> str:
    if build_system == "cargo":
        return "target/release"
    if build_system == "zig":
        return "zig-out/bin"
    return ""


def _default_build_commands(build_system: str, source: str) -> tuple[str, ...]:
    if build_system == "cargo":
        return ("cargo build --release",)
    if build_system == "zig":
        return ("zig build -Doptimize=ReleaseFast",)
    if build_system == "custom":
        raise ProfileLoadError(f"{source}.build_commands is required for custom builds")
    raise ProfileLoadError(f"{source}.build_system must be one of: cargo, zig, custom")


def _validate_source_build(package: SourceBuildPackage, source: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", package.name):
        raise ProfileLoadError(
            f"{source}.name must use letters, numbers, dots, underscores, or hyphens"
        )
    if package.build_system not in {"cargo", "zig", "custom"}:
        raise ProfileLoadError(
            f"{source}.build_system must be one of: cargo, zig, custom"
        )
    if package.install_mode not in {"copy", "path"}:
        raise ProfileLoadError(f"{source}.install_mode must be one of: copy, path")
    for binary in package.binaries:
        if "/" in binary or binary in {"", ".", ".."}:
            raise ProfileLoadError(f"{source}.binaries contains invalid binary name")
    if not package.build_commands:
        raise ProfileLoadError(f"{source}.build_commands must not be empty")


def _optional_git_settings(
    data: Mapping[str, Any], key: str, source: str
) -> tuple[GitSetting, ...] | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProfileLoadError(f"{source}.{key} must be an array of setting tables")
    settings = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ProfileLoadError(f"{source}.{key}[{index}] must be a table")
        item_source = f"{source}.{key}[{index}]"
        settings.append(
            GitSetting(
                key=_required_string(item, "key", item_source),
                value=_required_string(item, "value", item_source),
            )
        )
    return tuple(settings)


def _optional_macos_defaults(
    data: Mapping[str, Any], key: str, source: str
) -> tuple[MacDefaultsSetting, ...] | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProfileLoadError(f"{source}.{key} must be an array of setting tables")
    settings = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ProfileLoadError(f"{source}.{key}[{index}] must be a table")
        item_source = f"{source}.{key}[{index}]"
        setting = MacDefaultsSetting(
            name=_required_string(item, "name", item_source),
            domain=_required_string(item, "domain", item_source),
            key=_required_string(item, "key", item_source),
            value=_required_string(item, "value", item_source),
            value_type=_optional_string(item, "value_type", item_source) or "string",
            directories=_optional_string_tuple(item, "directories", item_source) or (),
            tags=_optional_string_tuple(item, "tags", item_source)
            or ("macos", "defaults"),
        )
        _validate_macos_default(setting, item_source)
        settings.append(setting)
    return tuple(settings)


def _validate_macos_default(setting: MacDefaultsSetting, source: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", setting.name):
        raise ProfileLoadError(
            f"{source}.name must use letters, numbers, dots, underscores, or hyphens"
        )
    if setting.value_type not in {"string", "path", "bool", "int", "float"}:
        raise ProfileLoadError(
            f"{source}.value_type must be one of: string, path, bool, int, float"
        )
    if setting.value_type == "bool" and setting.value.lower() not in {
        "true",
        "false",
        "yes",
        "no",
        "1",
        "0",
    }:
        raise ProfileLoadError(f"{source}.value must be boolean-like for bool")
    if setting.value_type == "int" and not re.fullmatch(r"[+-]?\d+", setting.value):
        raise ProfileLoadError(f"{source}.value must be an integer for int")
    if setting.value_type == "float":
        try:
            float(setting.value)
        except ValueError as error:
            raise ProfileLoadError(
                f"{source}.value must be a number for float"
            ) from error
    for directory in setting.directories:
        if not directory:
            raise ProfileLoadError(f"{source}.directories must not contain empty paths")


def _optional_manual_apps(
    data: Mapping[str, Any], key: str, source: str
) -> tuple[ManualApp, ...] | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProfileLoadError(f"{source}.{key} must be an array of app tables")
    apps = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ProfileLoadError(f"{source}.{key}[{index}] must be a table")
        item_source = f"{source}.{key}[{index}]"
        apps.append(
            ManualApp(
                name=_required_string(item, "name", item_source),
                install_method=_required_string(item, "install_method", item_source),
                url=_required_string(item, "url", item_source),
                tags=_optional_string_tuple(item, "tags", item_source)
                or ("packages", "apps", "manual"),
                app_bundles=_optional_string_tuple(item, "app_bundles", item_source)
                or (),
            )
        )
    return tuple(apps)


def _optional_manual_notes(
    data: Mapping[str, Any], key: str, source: str
) -> tuple[ManualNote, ...] | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProfileLoadError(f"{source}.{key} must be an array of note tables")
    notes = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ProfileLoadError(f"{source}.{key}[{index}] must be a table")
        item_source = f"{source}.{key}[{index}]"
        notes.append(
            ManualNote(
                name=_required_string(item, "name", item_source),
                detail=_required_string(item, "detail", item_source),
                tags=_optional_string_tuple(item, "tags", item_source)
                or ("macos", "manual"),
            )
        )
    return tuple(notes)


def _required_string(data: Mapping[str, Any], key: str, source: str) -> str:
    value = _optional_string(data, key, source)
    if value is None or value == "":
        raise ProfileLoadError(f"{source}.{key} must be a non-empty string")
    return value


def _validate_state(state: _RecipeState) -> None:
    recipe = state.recipe
    if not recipe.python_version:
        raise ProfileLoadError("python.version is required")
    if recipe.python_version not in recipe.python_versions:
        raise ProfileLoadError("python.versions must include python.version")
    if recipe.python_build_jobs not in {"auto", "default"} and not (
        recipe.python_build_jobs.isdecimal() and int(recipe.python_build_jobs) >= 1
    ):
        raise ProfileLoadError(
            'python.build_jobs must be "auto", "default", or a positive integer'
        )
    _ensure_unique("brew.formulas", (package.name for package in recipe.brew_formulas))
    _ensure_unique("brew.casks", (package.name for package in recipe.brew_casks))
    _ensure_unique("python.versions", recipe.python_versions)
    _ensure_unique(
        "python.tooling_packages",
        (package.lower() for package in recipe.python_tooling_packages),
    )
    _ensure_unique(
        "python.global_packages",
        (package.lower() for package in recipe.python_global_packages),
    )
    _ensure_unique("javascript.npm_global_packages", recipe.npm_global_packages)
    _ensure_unique(
        "source_builds.packages", (package.name for package in recipe.source_builds)
    )
    _ensure_unique("git.settings", (setting.key for setting in recipe.git_settings))
    _ensure_unique(
        "macos.defaults", (setting.name for setting in recipe.macos_defaults)
    )
    _ensure_unique("manual.apps", (app.name for app in recipe.manual_apps))
    _ensure_unique("manual.notes", (note.name for note in recipe.manual_notes))
    _ensure_absent(
        "brew.formulas",
        (package.name for package in recipe.brew_formulas),
        state.rejects.brew_formulas,
    )
    _ensure_absent(
        "brew.casks",
        (package.name for package in recipe.brew_casks),
        state.rejects.brew_casks,
    )
    _ensure_absent(
        "python.tooling_packages",
        recipe.python_tooling_packages,
        state.rejects.python_tooling_packages,
    )
    _ensure_absent(
        "python.global_packages",
        recipe.python_global_packages,
        state.rejects.python_packages,
    )
    _ensure_absent(
        "javascript.npm_global_packages",
        recipe.npm_global_packages,
        state.rejects.npm_packages,
    )
    _ensure_absent(
        "source_builds.packages",
        (package.name for package in recipe.source_builds),
        state.rejects.source_builds,
    )
    _ensure_absent(
        "git.settings",
        (setting.key for setting in recipe.git_settings),
        state.rejects.git_settings,
    )
    _ensure_absent(
        "macos.defaults",
        (setting.name for setting in recipe.macos_defaults),
        state.rejects.macos_defaults,
    )
    _ensure_absent(
        "manual.apps",
        (app.name for app in recipe.manual_apps),
        state.rejects.manual_apps,
    )
    _ensure_absent(
        "manual.notes",
        (note.name for note in recipe.manual_notes),
        state.rejects.manual_notes,
    )


def _ensure_unique(source: str, values: Iterable[str]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen:
            duplicates.append(value)
        seen.add(value)
    if duplicates:
        raise ProfileLoadError(
            f"{source} contains duplicate value(s): {', '.join(sorted(set(duplicates)))}"
        )


def _ensure_absent(
    source: str, values: Iterable[str], rejected: frozenset[str]
) -> None:
    blocked = sorted(set(values) & rejected)
    if blocked:
        raise ProfileLoadError(
            f"{source} contains rejected value(s): {', '.join(blocked)}"
        )


DEFAULT_RECIPE = load_recipe()


LEGACY_OMISSIONS = (
    "kerl",
    "rebar3",
    "manual Erlang build/install commands",
)
