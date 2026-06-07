from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_DIR = Path("~/.config/macsetup/rsync")
GLOBAL_FILTER_NAME = "global.filter"
LOCAL_FILTER_NAME = "local.filter"
NO_GLOBAL_FILTERS_ENV = "MRSYNC_NO_GLOBAL_FILTERS"
NO_PROJECT_FILTERS_ENV = "MRSYNC_NO_PROJECT_FILTERS"


@dataclass(frozen=True)
class FilterSource:
    label: str
    path: Path
    required: bool = False

    @property
    def exists(self) -> bool:
        return self.path.is_file()


@dataclass(frozen=True)
class MrsyncOptions:
    audit: bool
    print_command: bool
    use_global_filters: bool
    use_project_filters: bool
    config_dir: Path
    extra_filter_files: tuple[Path, ...]
    rsync_args: tuple[str, ...]


@dataclass(frozen=True)
class MrsyncPlan:
    command: tuple[str, ...]
    filter_sources: tuple[FilterSource, ...]
    use_project_filters: bool


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(tuple(sys.argv[1:] if argv is None else argv))
    missing = [
        source
        for source in filter_sources(options)
        if source.required and not source.exists
    ]
    if missing:
        for source in missing:
            print(f"missing required filter file: {source.path}", file=sys.stderr)
        return 2

    plan = build_plan(options)
    if options.audit:
        print(format_audit(options, plan))
        return 0
    if options.print_command:
        print(shlex.join(plan.command))
        return 0
    return subprocess.run(plan.command).returncode


def parse_args(argv: Sequence[str]) -> MrsyncOptions:
    audit = False
    print_command = False
    use_global_filters = os.environ.get(NO_GLOBAL_FILTERS_ENV) is None
    use_project_filters = os.environ.get(NO_PROJECT_FILTERS_ENV) is None
    config_dir = Path(
        os.environ.get("MRSYNC_CONFIG_DIR", str(DEFAULT_CONFIG_DIR))
    ).expanduser()
    extra_filter_files: list[Path] = []
    rsync_args: list[str] = []

    iterator = iter(enumerate(argv))
    for index, argument in iterator:
        if argument == "--":
            rsync_args.extend(argv[index + 1 :])
            break
        if argument in {"-h", "--help"}:
            print(help_text())
            raise SystemExit(0)
        if argument == "--audit":
            audit = True
            continue
        if argument == "--print-command":
            print_command = True
            continue
        if argument == "--no-global-filters":
            use_global_filters = False
            continue
        if argument == "--no-project-filters":
            use_project_filters = False
            continue
        if argument == "--filter-file":
            try:
                _, value = next(iterator)
            except StopIteration as error:
                raise SystemExit("--filter-file requires a path") from error
            extra_filter_files.append(Path(value).expanduser())
            continue
        if argument.startswith("--filter-file="):
            extra_filter_files.append(Path(argument.split("=", 1)[1]).expanduser())
            continue
        if argument == "--config-dir":
            try:
                _, value = next(iterator)
            except StopIteration as error:
                raise SystemExit("--config-dir requires a path") from error
            config_dir = Path(value).expanduser()
            continue
        if argument.startswith("--config-dir="):
            config_dir = Path(argument.split("=", 1)[1]).expanduser()
            continue
        if argument == "--rsync-help":
            rsync_args.append("--help")
            continue
        rsync_args.append(argument)

    return MrsyncOptions(
        audit=audit,
        print_command=print_command,
        use_global_filters=use_global_filters,
        use_project_filters=use_project_filters,
        config_dir=config_dir,
        extra_filter_files=tuple(extra_filter_files),
        rsync_args=tuple(rsync_args),
    )


def build_plan(options: MrsyncOptions) -> MrsyncPlan:
    sources = filter_sources(options)
    command: list[str] = ["rsync"]
    for source in sources:
        if source.exists:
            command.append(f"--filter=merge {source.path}")
    if options.use_project_filters:
        command.extend(("-F", "-F"))
    command.extend(options.rsync_args)
    return MrsyncPlan(
        command=tuple(command),
        filter_sources=sources,
        use_project_filters=options.use_project_filters,
    )


def filter_sources(options: MrsyncOptions) -> tuple[FilterSource, ...]:
    sources: list[FilterSource] = []
    if options.use_global_filters:
        sources.extend(
            (
                FilterSource("managed global", options.config_dir / GLOBAL_FILTER_NAME),
                FilterSource("local user", options.config_dir / LOCAL_FILTER_NAME),
            )
        )
    sources.extend(
        FilterSource("explicit", path, required=True)
        for path in options.extra_filter_files
    )
    return tuple(sources)


def format_audit(options: MrsyncOptions, plan: MrsyncPlan) -> str:
    lines = [
        "mrsync audit",
        "",
        f"Config directory: {options.config_dir}",
        f"Global/local filters: {'enabled' if options.use_global_filters else 'disabled'}",
        f"Project .rsync-filter support: {'enabled (-F -F)' if options.use_project_filters else 'disabled'}",
        "",
        "Filter sources:",
    ]
    if not plan.filter_sources:
        lines.append("- none")
    for source in plan.filter_sources:
        status = "present" if source.exists else "missing"
        required = ", required" if source.required else ""
        lines.append(f"- {source.label}: {source.path} ({status}{required})")
        if source.exists:
            lines.extend(format_filter_lines(source.path))
    lines.extend(["", "Final command:", shlex.join(plan.command)])
    return "\n".join(lines)


def format_filter_lines(path: Path) -> list[str]:
    lines = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(f"  {number}: {line}")
    if not lines:
        lines.append("  (no active rules)")
    return lines


def help_text() -> str:
    return """Usage: mrsync [mrsync options] [--] RSYNC_ARGS...

Run rsync with managed macsetup filter files and project .rsync-filter support.

Mrsync options:
  --audit                 Show filter hierarchy and final command; do not copy.
  --print-command         Print the final rsync command; do not copy.
  --no-global-filters     Skip managed global/local filter files.
  --no-project-filters    Do not add -F -F for project .rsync-filter files.
  --filter-file FILE      Merge an additional required rsync filter file.
  --config-dir DIR        Override the filter config directory.
  --rsync-help            Pass --help through to rsync.
  -h, --help              Show this help.

Environment:
  MRSYNC_CONFIG_DIR        Override the filter config directory.
  MRSYNC_NO_GLOBAL_FILTERS Disable global/local filters when set.
  MRSYNC_NO_PROJECT_FILTERS Disable project .rsync-filter support when set.

Use -- before rsync args if an rsync option conflicts with a mrsync option.
"""


if __name__ == "__main__":
    raise SystemExit(main())
