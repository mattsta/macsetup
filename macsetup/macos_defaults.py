from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .files import expand_user
from .model import CommandResult, CommandSpec, SetupContext
from .recipes import MacDefaultsSetting


@dataclass(frozen=True)
class DefaultsValue:
    had_value: bool
    value: str = ""


def defaults_resource_name(setting: MacDefaultsSetting) -> str:
    return f"{setting.domain}.{setting.key}"


def read_default(
    context: SetupContext, setting: MacDefaultsSetting
) -> tuple[DefaultsValue, CommandResult]:
    result = context.runner.run(
        defaults_read_command(setting),
        capture=True,
        dry_run=False,
    )
    if not result.ok:
        return DefaultsValue(False), result
    return DefaultsValue(True, result.stdout.strip()), result


def defaults_read_command(setting: MacDefaultsSetting) -> CommandSpec:
    return CommandSpec(argv=("defaults", "read", setting.domain, setting.key))


def defaults_write_command(
    setting: MacDefaultsSetting, value: str, home: Path
) -> CommandSpec:
    materialized = materialized_value(setting, value, home)
    return CommandSpec(
        argv=(
            "defaults",
            "write",
            setting.domain,
            setting.key,
            *_write_value_args(setting.value_type, materialized),
        )
    )


def defaults_delete_command(setting: MacDefaultsSetting) -> CommandSpec:
    return CommandSpec(argv=("defaults", "delete", setting.domain, setting.key))


def desired_value(setting: MacDefaultsSetting, home: Path) -> str:
    return materialized_value(setting, setting.value, home)


def materialized_value(setting: MacDefaultsSetting, value: str, home: Path) -> str:
    if setting.value_type == "path":
        return str(expand_user(value, home))
    if setting.value_type == "bool":
        return "true" if _bool_value(value) else "false"
    return value


def values_match(
    setting: MacDefaultsSetting, current: str, desired: str, home: Path
) -> bool:
    if setting.value_type == "path":
        return str(expand_user(current, home)) == str(expand_user(desired, home))
    if setting.value_type == "bool":
        try:
            return _bool_value(current) == _bool_value(desired)
        except ValueError:
            return False
    if setting.value_type == "int":
        try:
            return int(current) == int(desired)
        except ValueError:
            return False
    if setting.value_type == "float":
        try:
            return float(current) == float(desired)
        except ValueError:
            return False
    return current == desired


def _write_value_args(value_type: str, value: str) -> tuple[str, str]:
    if value_type == "bool":
        return ("-bool", value)
    if value_type == "int":
        return ("-int", value)
    if value_type == "float":
        return ("-float", value)
    return ("-string", value)


def _bool_value(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "yes", "1"}:
        return True
    if normalized in {"false", "no", "0"}:
        return False
    raise ValueError(value)
