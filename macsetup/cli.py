from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import transfer
from .graph import GraphError, StepGraph, format_graph
from .model import (
    CommandResult,
    CommandSpec,
    ResourceRef,
    Risk,
    SetupContext,
    Step,
    StepCheck,
    StepResult,
    StepStatus,
)
from .ownership import (
    UserIdentity,
    context_state_owner,
    context_state_root,
    invoking_user_identity,
    prepare_user_state_root,
)
from .preview import build_previews, format_previews
from .recipes import LEGACY_OMISSIONS, ProfileLoadError, SetupRecipe, load_recipe
from .reporting import (
    format_actionable_checks,
    format_check_lines,
    format_checks,
    format_remediations,
    format_results,
    write_journal,
)
from .runner import CommandRunner, LocalContext
from .steps import build_steps
from .unapply import (
    build_unapply_plan,
    candidate_steps_for_unapply,
    load_run_journal,
    unapply_step,
)

SUDO_PREFLIGHT_TIMEOUT_SECONDS = 120.0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


def _main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "sync":
        return transfer.run(args)

    repo_root = Path(__file__).resolve().parents[1]
    user_identity = invoking_user_identity()
    state_root = user_identity.home / ".macsetup"
    runner = CommandRunner(
        dry_run=getattr(args, "dry_run", False),
        verbose=getattr(args, "verbose", False),
        progress=args.command in {"apply", "unapply"},
    )
    context = LocalContext(
        home=user_identity.home,
        repo_root=repo_root,
        backup_root=state_root / "backups",
        runner=runner,
        dry_run=getattr(args, "dry_run", False),
        allow_bootstrap=not getattr(args, "no_bootstrap", False),
        allow_privileged=getattr(args, "allow_privileged", False),
        enable_dns_blocklist=not getattr(args, "disable_dns_blocklist", False),
        state_root=state_root,
        state_owner=user_identity,
    )
    seed_runtime_path_dirs(runner, context.home)
    profile_paths = (
        *discover_default_profile_paths(repo_root),
        *tuple(getattr(args, "profile", ())),
    )
    try:
        recipe = load_recipe(profile_paths)
    except ProfileLoadError as error:
        print(f"Invalid profile: {error}")
        return 2

    graph = StepGraph(build_steps(recipe))
    try:
        graph.validate_or_raise()
        steps = graph.selected(
            only_tags=parse_csv(args.tags), skip_tags=parse_csv(args.skip_tags)
        )
    except GraphError as error:
        print("Invalid setup graph:")
        for issue in error.issues:
            print(f"- {issue.detail}")
        return 2

    if args.command == "audit":
        print(render_audit(recipe))
        return 0

    if args.command == "graph":
        print(format_graph(steps))
        return 0

    if args.command == "unapply":
        return run_unapply(steps, context, args)

    stream_checks = args.command == "apply"
    checks = run_checks(steps, context, stream=stream_checks)
    if not stream_checks:
        print(format_checks(checks))

    if args.command in {"plan", "preview"}:
        if args.command == "preview" or getattr(args, "diff", False):
            print(format_previews(build_previews(steps, checks, context)))
        print_remediation_report(checks)
        return 1 if any(check.status == StepStatus.FAILED for check in checks) else 0

    if args.command == "apply":
        actionable = [
            check
            for check in checks
            if check.status in {StepStatus.NEEDS_CHANGE, StepStatus.UNKNOWN}
        ]
        blocked = [check for check in checks if check.status == StepStatus.BLOCKED]
        manual = [check for check in checks if check.status == StepStatus.MANUAL]
        if getattr(args, "diff", False):
            print(format_previews(build_previews(steps, checks, context)))
        if not actionable:
            print("\nNo actionable changes.")
            print_non_apply_report(blocked, manual)
            print_remediation_report(checks)
            return 1 if blocked else 0
        print("\n" + format_actionable_checks(actionable, total_count=len(checks)))
        print_non_apply_report(blocked, manual)
        if (
            not context.dry_run
            and not args.yes
            and not confirm_apply(actionable_count=len(actionable))
        ):
            print("Apply cancelled.")
            return 2
        if (
            not context.dry_run
            and context.allow_privileged
            and _has_privileged_actionable(actionable)
        ):
            auth = validate_sudo_for_apply(context)
            if not auth.ok:
                result = StepResult(
                    "sudo.auth",
                    "Validate sudo credentials",
                    StepStatus.FAILED,
                    auth.stderr.strip()
                    or auth.stdout.strip()
                    or "sudo credential validation failed",
                    (auth,),
                )
                print(format_results([result]))
                print_remediation_report(checks)
                return 1
        if not context.dry_run and not prepare_state_for_mutation(context):
            return 1
        results: list[StepResult] = []
        available_resources: set[ResourceRef] = set()
        provider_by_resource = selected_provider_by_resource(steps)
        current_step: Step | None = None
        current_phase = "apply"
        try:
            for step in steps:
                current_step = step
                current_phase = "dependency check"
                unmet = unmet_selected_requirements(
                    step, available_resources, provider_by_resource
                )
                if unmet:
                    runner.state(
                        "step.skip",
                        f"{step.id} unmet requirement(s): {', '.join(str(resource) for resource in unmet)}",
                    )
                    result = StepResult(
                        step.id,
                        step.title,
                        StepStatus.SKIPPED,
                        "unmet requirement(s): "
                        + ", ".join(str(resource) for resource in unmet),
                    )
                    results.append(result)
                    print(format_results([result]))
                    current_step = None
                    continue
                current_phase = "recheck"
                runner.state("step.recheck.start", f"{step.id} {step.title}")
                check = step.check(context)
                runner.state(
                    "step.recheck.done",
                    f"{step.id} {check.status.value}: {check.detail}",
                )
                if check.status == StepStatus.PRESENT:
                    available_resources.update(step.provides)
                    runner.state("step.skip.present", step.id)
                    current_step = None
                    continue
                if check.status not in {StepStatus.NEEDS_CHANGE, StepStatus.UNKNOWN}:
                    runner.state("step.skip.status", f"{step.id} {check.status.value}")
                    if check.status in {StepStatus.BLOCKED, StepStatus.MANUAL}:
                        result = StepResult(
                            step.id,
                            step.title,
                            check.status,
                            check.detail,
                            remediations=check.remediations,
                        )
                        results.append(result)
                        print(format_results([result]))
                    current_step = None
                    continue
                current_phase = "apply"
                runner.state("step.apply.start", f"{step.id} {step.title}")
                result = step.apply(context)
                runner.state(
                    "step.apply.done",
                    f"{step.id} {result.status.value}: {result.detail}",
                )
                results.append(result)
                print(format_results([result]))
                current_step = None
                if result.status in {StepStatus.PRESENT, StepStatus.APPLIED}:
                    available_resources.update(step.provides)
                if result.status == StepStatus.FAILED and not args.keep_going:
                    if context.dry_run:
                        print(
                            "\nStopped after failure. Dry-run did not write a journal."
                        )
                    else:
                        journal = write_run_journal(
                            context,
                            checks=checks,
                            results=results,
                            status="failed",
                        )
                        print(f"\nStopped after failure. Journal: {journal}")
                    print_remediation_report((*checks, *results))
                    return 1
        except KeyboardInterrupt:
            if current_step is not None:
                result = interrupted_step_result(current_step, current_phase, context)
                results.append(result)
                print(format_results([result]))
            if context.dry_run:
                print("\nInterrupted. Dry-run did not write a journal.")
            else:
                journal = write_run_journal(
                    context,
                    checks=checks,
                    results=results,
                    status="interrupted",
                    interrupted_step_id=current_step.id if current_step else None,
                )
                print(f"\nInterrupted. Journal: {journal}")
            print_remediation_report((*checks, *results))
            return 130
        if context.dry_run:
            print("\nDry-run complete. No journal written.")
        else:
            journal = write_run_journal(context, checks=checks, results=results)
            print(f"\nJournal: {journal}")
        print_remediation_report((*checks, *results))
        return (
            1
            if any(
                result.status in {StepStatus.FAILED, StepStatus.BLOCKED}
                for result in results
            )
            else 0
        )

    parser.error(f"unknown command {args.command}")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply the macOS setup recipe safely and repeatedly."
    )
    add_common_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_parser = subparsers.add_parser(
        "audit", help="Show the packaged defaults and modernization audit."
    )
    add_common_options(audit_parser, suppress_defaults=True)
    graph_parser = subparsers.add_parser(
        "graph", help="Show selected step resource graph metadata."
    )
    add_common_options(graph_parser, suppress_defaults=True)
    plan_parser = subparsers.add_parser("plan", help="Check what would change.")
    add_common_options(plan_parser, suppress_defaults=True)
    plan_parser.add_argument(
        "--diff",
        action="store_true",
        help="Show exact managed file diffs for planned changes.",
    )
    add_privileged_option(
        plan_parser,
        help_text="Allow plan checks to classify sudo-backed steps as actionable. Does not mutate.",
    )
    add_no_bootstrap_option(plan_parser)
    add_dns_blocklist_options(plan_parser)
    preview_parser = subparsers.add_parser(
        "preview", help="Check what would change and show managed file diffs."
    )
    add_common_options(preview_parser, suppress_defaults=True)
    add_privileged_option(
        preview_parser,
        help_text="Allow preview checks to classify sudo-backed steps as actionable. Does not mutate.",
    )
    add_no_bootstrap_option(preview_parser)
    add_dns_blocklist_options(preview_parser)

    apply_parser = subparsers.add_parser("apply", help="Apply needed changes.")
    add_common_options(apply_parser, suppress_defaults=True)
    apply_parser.add_argument(
        "--yes", action="store_true", help="Do not prompt before applying changes."
    )
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands and file actions without mutating.",
    )
    apply_parser.add_argument(
        "--diff",
        action="store_true",
        help="Show exact managed file diffs before applying.",
    )
    add_no_bootstrap_option(apply_parser)
    apply_parser.add_argument(
        "--allow-bootstrap",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    add_privileged_option(
        apply_parser, help_text="Allow sudo-backed service/cache operations."
    )
    add_dns_blocklist_options(apply_parser)
    apply_parser.add_argument(
        "--keep-going", action="store_true", help="Continue after a failed step."
    )
    unapply_parser = subparsers.add_parser(
        "unapply",
        help="Reverse applied setup steps from a run journal or force-selected current profile.",
    )
    add_common_options(unapply_parser, suppress_defaults=True)
    mode_group = unapply_parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--from-run", help='Run journal path/name to reverse, or "latest".'
    )
    mode_group.add_argument(
        "--force",
        action="store_true",
        help="Blindly reverse selected current-profile steps.",
    )
    unapply_parser.add_argument(
        "--yes", action="store_true", help="Do not prompt before unapplying changes."
    )
    unapply_parser.add_argument(
        "--dry-run", action="store_true", help="Print inverse actions without mutating."
    )
    unapply_parser.add_argument(
        "--allow-privileged",
        action="store_true",
        help="Allow sudo-backed inverse operations.",
    )
    unapply_parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue after a failed inverse step.",
    )
    transfer.add_sync_parser(subparsers)
    return parser


def discover_default_profile_paths(
    repo_root: Path, cwd: Path | None = None
) -> tuple[Path, ...]:
    roots = (repo_root, cwd or Path.cwd())
    paths: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        local_dir = root / "local"
        if not local_dir.is_dir():
            continue
        for path in sorted(local_dir.glob("*.toml")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            paths.append(path)
    return tuple(paths)


def seed_runtime_path_dirs(runner: CommandRunner, home: Path) -> None:
    for directory in (
        home / ".local" / "bin",
        home / ".pyenv" / "bin",
        home / ".pyenv" / "shims",
    ):
        runner.add_path_dir(directory)


def add_common_options(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    default = argparse.SUPPRESS if suppress_defaults else ""
    parser.add_argument(
        "--tags", default=default, help="Comma-separated tag allowlist."
    )
    parser.add_argument(
        "--skip-tags", default=default, help="Comma-separated tags to exclude."
    )
    if suppress_defaults:
        parser.add_argument(
            "--profile",
            action="append",
            default=argparse.SUPPRESS,
            help="Additional TOML profile overlay path. May be passed more than once.",
        )
    else:
        parser.add_argument(
            "--profile",
            action="append",
            default=[],
            help="Additional TOML profile overlay path. May be passed more than once.",
        )
    if suppress_defaults:
        parser.add_argument(
            "--verbose",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Print commands before running them.",
        )
    else:
        parser.add_argument(
            "--verbose", action="store_true", help="Print commands before running them."
        )


def add_dns_blocklist_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--disable-dns-blocklist",
        dest="disable_dns_blocklist",
        action="store_true",
        default=False,
        help="Disable the managed dnsmasq focus blocklist for this run.",
    )
    group.add_argument(
        "--enable-dns-blocklist",
        dest="disable_dns_blocklist",
        action="store_false",
        help=argparse.SUPPRESS,
    )


def add_no_bootstrap_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Do not run first-time network bootstrap installers such as Homebrew.",
    )


def add_privileged_option(parser: argparse.ArgumentParser, *, help_text: str) -> None:
    parser.add_argument(
        "--allow-privileged", action="store_true", default=False, help=help_text
    )


def parse_csv(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def confirm_apply(*, actionable_count: int) -> bool:
    return confirm_action("Apply", actionable_count=actionable_count)


def confirm_unapply(*, actionable_count: int) -> bool:
    return confirm_action("Unapply", actionable_count=actionable_count)


def confirm_action(action: str, *, actionable_count: int) -> bool:
    if not sys.stdin.isatty():
        print(
            f"Refusing to {action.lower()} without --yes because stdin is not interactive."
        )
        return False
    answer = (
        input(f"\n{action} {actionable_count} actionable change(s)? [y/N] ")
        .strip()
        .lower()
    )
    return answer in {"y", "yes"}


def print_remediation_report(
    items: tuple[StepCheck | StepResult, ...] | list[StepCheck] | list[StepResult],
) -> None:
    rendered = format_remediations(items)
    if rendered:
        print("\n" + rendered)


def print_non_apply_report(blocked: list[StepCheck], manual: list[StepCheck]) -> None:
    if blocked:
        print("\nBlocked Steps")
        for check in blocked:
            for line in format_check_lines(check):
                print(line)
    if manual:
        print("\nManual Steps")
        for check in manual:
            for line in format_check_lines(check):
                print(line)


def _has_privileged_actionable(checks: list[StepCheck]) -> bool:
    return any(Risk.PRIVILEGED in check.risks for check in checks)


def validate_sudo_for_apply(context: SetupContext) -> CommandResult:
    context.runner.state(
        "apply.sudo-auth", "validating sudo credentials before privileged actions"
    )
    return context.runner.sudo_validate(timeout_seconds=SUDO_PREFLIGHT_TIMEOUT_SECONDS)


def prepare_state_for_mutation(context: SetupContext) -> bool:
    state_root = context_state_root(context)
    owner = context_state_owner(context)
    try:
        prepare_user_state_root(state_root, owner, "backups")
    except PermissionError as error:
        if not repair_state_with_privileges(context, state_root, owner):
            print(f"Unable to prepare macsetup state directory: {error}")
            return False
        try:
            prepare_user_state_root(state_root, owner, "backups")
        except OSError as retry_error:
            print(f"Unable to prepare macsetup state directory: {retry_error}")
            return False
    except OSError as error:
        print(f"Unable to prepare macsetup state directory: {error}")
        return False
    return True


def repair_state_with_privileges(
    context: SetupContext,
    state_root: Path,
    owner: UserIdentity | None,
) -> bool:
    if not context.allow_privileged or owner is None:
        return False
    if state_root != context.home / ".macsetup" or state_root.is_symlink():
        return False
    uid = str(owner.uid)
    gid = str(owner.gid)
    owner_spec = f"{uid}:{gid}"
    commands = (
        CommandSpec(
            argv=(
                "sudo",
                "install",
                "-d",
                "-o",
                uid,
                "-g",
                gid,
                "-m",
                "700",
                str(state_root),
            )
        ),
        CommandSpec(
            argv=(
                "sudo",
                "install",
                "-d",
                "-o",
                uid,
                "-g",
                gid,
                "-m",
                "700",
                str(state_root / "backups"),
            )
        ),
        CommandSpec(
            argv=(
                "sudo",
                "install",
                "-d",
                "-o",
                uid,
                "-g",
                gid,
                "-m",
                "700",
                str(state_root / "runs"),
            )
        ),
        CommandSpec(
            argv=(
                "sudo",
                "find",
                str(state_root),
                "-type",
                "d",
                "-exec",
                "chown",
                owner_spec,
                "{}",
                "+",
            )
        ),
        CommandSpec(
            argv=(
                "sudo",
                "find",
                str(state_root),
                "-type",
                "d",
                "-exec",
                "chmod",
                "700",
                "{}",
                "+",
            )
        ),
        CommandSpec(
            argv=(
                "sudo",
                "find",
                str(state_root),
                "-type",
                "f",
                "-exec",
                "chown",
                owner_spec,
                "{}",
                "+",
            )
        ),
        CommandSpec(
            argv=(
                "sudo",
                "find",
                str(state_root / "runs"),
                "-type",
                "f",
                "-exec",
                "chmod",
                "600",
                "{}",
                "+",
            )
        ),
    )
    for command in commands:
        result = context.runner.run(command, capture=True, dry_run=False)
        if not result.ok:
            return False
    return True


def write_run_journal(
    context: SetupContext,
    *,
    checks: list[StepCheck],
    results: list[StepResult],
    operation: str = "apply",
    status: str = "completed",
    interrupted_step_id: str | None = None,
) -> Path:
    return write_journal(
        context_state_root(context),
        checks=checks,
        results=results,
        operation=operation,
        status=status,
        interrupted_step_id=interrupted_step_id,
        owner=context_state_owner(context),
    )


def interrupted_step_result(
    step: Step, phase: str, context: SetupContext
) -> StepResult:
    interrupted_command = getattr(context.runner, "last_interrupted_command", None)
    commands: tuple[CommandResult, ...] = ()
    if isinstance(interrupted_command, str) and interrupted_command:
        commands = (
            CommandResult(
                command=interrupted_command,
                returncode=130,
                stderr="interrupted by user",
            ),
        )
    return StepResult(
        step.id,
        step.title,
        StepStatus.FAILED,
        f"interrupted during {phase}; no completed changes were recorded for this step",
        commands=commands,
    )


def run_unapply(
    steps: tuple[Step, ...], context: SetupContext, args: argparse.Namespace
) -> int:
    journal = None
    if getattr(args, "from_run", None):
        try:
            journal = load_run_journal(context_state_root(context), args.from_run)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
            print(f"Invalid run journal: {error}")
            return 2
        if journal.operation == "unapply":
            print(f"Refusing to unapply an unapply journal: {journal.path}")
            return 2
        print(f"Unapplying from run journal: {journal.path}")
    candidates = candidate_steps_for_unapply(
        steps, force=getattr(args, "force", False), journal=journal
    )
    if not candidates:
        print("No unapply candidates.")
        return 0
    targets = build_unapply_plan(
        candidates, context, force=getattr(args, "force", False), journal=journal
    )
    if not targets:
        print("No unapply candidates.")
        return 0
    if (
        not context.dry_run
        and not args.yes
        and not confirm_unapply(actionable_count=len(targets))
    ):
        print("Unapply cancelled.")
        return 2
    if not context.dry_run and not prepare_state_for_mutation(context):
        return 1

    results: list[StepResult] = []
    current_step: Step | None = None
    try:
        for target in targets:
            current_step = target.step
            result = unapply_step(
                target.step,
                context,
                force=getattr(args, "force", False),
                journal_result=target.journal_result,
            )
            results.append(result)
            print(format_results([result]))
            current_step = None
            if result.status == StepStatus.FAILED and not args.keep_going:
                if context.dry_run:
                    print("\nStopped after failure. Dry-run did not write a journal.")
                else:
                    run_journal = write_run_journal(
                        context,
                        checks=[],
                        results=results,
                        operation="unapply",
                        status="failed",
                    )
                    print(f"\nStopped after failure. Journal: {run_journal}")
                return 1
    except KeyboardInterrupt:
        if current_step is not None:
            result = interrupted_step_result(current_step, "unapply", context)
            results.append(result)
            print(format_results([result]))
        if context.dry_run:
            print("\nInterrupted. Dry-run did not write a journal.")
        else:
            run_journal = write_run_journal(
                context,
                checks=[],
                results=results,
                operation="unapply",
                status="interrupted",
                interrupted_step_id=current_step.id if current_step else None,
            )
            print(f"\nInterrupted. Journal: {run_journal}")
        return 130
    if context.dry_run:
        print("\nDry-run complete. No journal written.")
    else:
        run_journal = write_run_journal(
            context, checks=[], results=results, operation="unapply"
        )
        print(f"\nJournal: {run_journal}")
    return 1 if any(result.status == StepStatus.FAILED for result in results) else 0


def run_checks(
    steps: tuple[Step, ...], context: SetupContext, *, stream: bool
) -> list[StepCheck]:
    checks: list[StepCheck] = []
    actual_resources: set[ResourceRef] = set()
    planned_resources: set[ResourceRef] = set()
    provider_by_resource = selected_provider_by_resource(steps)
    if stream:
        print(f"Checking {len(steps)} selected step(s)...", flush=True)
    for step in steps:
        if stream:
            print(f"CHECK    {step.id:<36} {step.title}", flush=True)
        satisfied_resources = actual_resources | planned_resources
        unmet = unmet_selected_requirements(
            step, satisfied_resources, provider_by_resource
        )
        if unmet:
            check = StepCheck(
                step.id,
                step.title,
                StepStatus.SKIPPED,
                "unmet requirement(s): "
                + ", ".join(str(resource) for resource in unmet),
                step.tags,
                step.risks,
            )
        else:
            planned_requirements = _planned_requirements(
                step, actual_resources, planned_resources, provider_by_resource
            )
            if planned_requirements:
                check = StepCheck(
                    step.id,
                    step.title,
                    StepStatus.UNKNOWN,
                    "will check after planned requirement(s): "
                    + ", ".join(str(resource) for resource in planned_requirements),
                    step.tags,
                    step.risks,
                )
            else:
                check = step.check(context)
        checks.append(check)
        if check.status == StepStatus.PRESENT:
            actual_resources.update(step.provides)
            planned_resources.update(step.provides)
        elif check.status in {StepStatus.NEEDS_CHANGE, StepStatus.UNKNOWN}:
            planned_resources.update(step.provides)
        if stream:
            for line in format_check_lines(check):
                print(line, flush=True)
    return checks


def _planned_requirements(
    step: Step,
    actual_resources: set[ResourceRef],
    planned_resources: set[ResourceRef],
    provider_by_resource: dict[ResourceRef, str],
) -> tuple[ResourceRef, ...]:
    planned = []
    for requirement in step.requires:
        provider_id = provider_by_resource.get(requirement)
        if provider_id is None or provider_id == step.id:
            continue
        if requirement not in actual_resources and requirement in planned_resources:
            planned.append(requirement)
    return tuple(sorted(planned))


def selected_provider_by_resource(
    steps: list[Step] | tuple[Step, ...],
) -> dict[ResourceRef, str]:
    return {provided: step.id for step in steps for provided in step.provides}


def unmet_selected_requirements(
    step: Step,
    available_resources: set[ResourceRef],
    provider_by_resource: dict[ResourceRef, str],
) -> tuple[ResourceRef, ...]:
    missing = []
    for requirement in step.requires:
        provider_id = provider_by_resource.get(requirement)
        if provider_id is None or provider_id == step.id:
            continue
        if requirement not in available_resources:
            missing.append(requirement)
    return tuple(sorted(missing))


def render_audit(recipe: SetupRecipe) -> str:
    formulas = ", ".join(package.name for package in recipe.brew_formulas)
    casks = ", ".join(package.name for package in recipe.brew_casks)
    python_versions = ", ".join(recipe.python_versions)
    python_tooling = ", ".join(recipe.python_tooling_packages) or "none"
    source_builds = (
        ", ".join(package.name for package in recipe.source_builds) or "none"
    )
    macos_defaults = (
        ", ".join(setting.name for setting in recipe.macos_defaults) or "none"
    )
    manual_apps = ", ".join(app.name for app in recipe.manual_apps) or "none"
    manual_notes = ", ".join(note.name for note in recipe.manual_notes) or "none"
    omitted = ", ".join(LEGACY_OMISSIONS)
    return f"""macsetup default recipe audit

Included:
- Homebrew bootstrap plus shellenv setup.
- Xcode.app developer-directory repair, license check, and Metal toolchain check before package bootstrap.
- Touch ID for sudo via /etc/pam.d/sudo_local on supported macOS layouts.
- Homebrew analytics are disabled immediately after bootstrap and audited on existing installs.
- Modern CLI/editor/terminal/media/network/build packages: {formulas}
- Homebrew casks cover modern fonts and terminal apps: {casks}
- Source-built Git packages: {source_builds}
- Managed macOS defaults: {macos_defaults}
- Manual app notes cover installs without reliable Homebrew automation: {manual_apps}
- Manual macOS notes cover System Settings bring-up checks: {manual_notes}
- Git aliases, optional identity, delta pager, LFS filters, merge style, and global ignore.
- pyenv-managed Python versions ({python_versions}), selected global Python, per-version baseline tooling ({python_tooling}), IPython startup imports, and global utility packages.
- oh-my-zsh, zsh completion setup, aliases, prompt, and helper functions.
- dnsmasq resolver config, including the managed focus blocklist unless --disable-dns-blocklist is passed.
- Neovim setup now uses lazy.nvim, Mason/LSP, nvim-cmp/LuaSnip, Telescope, and Trouble instead of the older packer/COQ flow.

Modernized:
- Homebrew prefix is detected, so Apple Silicon and Intel installs both work.
- Python target prefix is {recipe.python_version}; pyenv resolves the latest matching patch release.
- node replaces the old npm formula alias because Homebrew exposes npm through node.
- openssl@3 replaces the older unversioned openssl formula name.
- universal-ctags replaces the older ctags package.
- git-delta replaces the older diff-so-fancy pager setup.
- Python build dependencies include current pyenv build basics: readline, sqlite, xz, bzip2, libffi, pkg-config, and tcl-tk.
- Pyenv installs set build concurrency to {recipe.python_build_jobs}.
- pyenv shell initialization uses --no-rehash in every managed shell profile.
- Step ordering is generated from explicit require/provide/own resource metadata.
- Xcode developer-directory selection and license acceptance are gated behind --allow-privileged.
- Touch ID sudo setup is gated behind --allow-privileged and refuses direct edits to /etc/pam.d/sudo.
- dnsmasq uses a managed conf-dir snippet instead of editing the Homebrew formula service definition.

Intentionally omitted:
- {omitted}
- Historical Apple Silicon scipy/numba/LLVM build workarounds are not automated; modern wheels generally make those obsolete.
"""


if __name__ == "__main__":
    raise SystemExit(main())
