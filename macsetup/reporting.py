from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from .model import Remediation, StepCheck, StepResult, StepStatus
from .ownership import UserIdentity, ensure_private_file, prepare_user_state_root

STATUS_SYMBOL = {
    StepStatus.PRESENT: "OK",
    StepStatus.NEEDS_CHANGE: "CHANGE",
    StepStatus.BLOCKED: "BLOCKED",
    StepStatus.MANUAL: "MANUAL",
    StepStatus.SKIPPED: "SKIPPED",
    StepStatus.UNKNOWN: "UNKNOWN",
    StepStatus.APPLIED: "APPLIED",
    StepStatus.FAILED: "FAILED",
}


def format_checks(checks: Iterable[StepCheck]) -> str:
    lines = []
    for check in checks:
        lines.extend(format_check_lines(check))
    return "\n".join(lines)


def format_actionable_checks(checks: Iterable[StepCheck], *, total_count: int) -> str:
    actionable = tuple(checks)
    if not actionable:
        return ""
    lines = [
        f"Actionable Changes To Apply ({len(actionable)} of {total_count} checked)"
    ]
    for check in actionable:
        tags = ",".join(sorted(check.tags))
        risks = ",".join(
            risk.value for risk in sorted(check.risks, key=lambda item: item.value)
        )
        lines.append(f"- {STATUS_SYMBOL[check.status]} {check.step_id}: {check.title}")
        lines.append(f"  detail: {check.detail}")
        if tags:
            lines.append(f"  tags: {tags}")
        if risks:
            lines.append(f"  risks: {risks}")
    return "\n".join(lines)


def format_check_lines(check: StepCheck) -> list[str]:
    tags = ",".join(sorted(check.tags))
    risks = ",".join(
        risk.value for risk in sorted(check.risks, key=lambda item: item.value)
    )
    lines = [
        f"{STATUS_SYMBOL[check.status]:<8} {check.step_id:<36} [{tags}] {check.detail}"
    ]
    if risks:
        lines.append(f"{'':<8} {'':<36} risks: {risks}")
    return lines


def format_results(results: Iterable[StepResult]) -> str:
    lines = []
    for result in results:
        lines.append(
            f"{STATUS_SYMBOL[result.status]:<8} {result.step_id:<36} {result.detail}"
        )
        for command in result.commands:
            if command.skipped:
                lines.append(f"{'':<8} {'':<36} dry-run: {command.command}")
            elif not command.ok:
                tail = command.stderr.strip() or command.stdout.strip()
                label = "timed out command" if command.timed_out else "failed command"
                lines.append(f"{'':<8} {'':<36} {label}: {command.command}")
                if tail:
                    lines.append(f"{'':<8} {'':<36} {tail}")
    return "\n".join(lines)


class _RemediationOwner(Protocol):
    @property
    def step_id(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def remediations(self) -> tuple[Remediation, ...]: ...


def format_remediations(items: Iterable[_RemediationOwner]) -> str:
    lines: list[str] = []
    seen = set()
    for item in items:
        for remediation in item.remediations:
            key = (
                item.step_id,
                remediation.summary,
                remediation.commands,
                remediation.manual_steps,
            )
            if key in seen:
                continue
            seen.add(key)
            if not lines:
                lines.append("Recommended Recovery")
            lines.append(f"- {item.step_id}: {remediation.summary}")
            for command in remediation.commands:
                lines.append(f"  command: {command}")
            for step in remediation.manual_steps:
                lines.append(f"  manual: {step}")
    return "\n".join(lines)


def write_journal(
    path: Path,
    *,
    checks: list[StepCheck],
    results: list[StepResult],
    operation: str = "apply",
    status: str = "completed",
    interrupted_step_id: str | None = None,
    owner: UserIdentity | None = None,
) -> Path:
    stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    target = path / "runs" / f"{stamp}.json"
    prepare_user_state_root(path, owner, "runs")
    payload = {
        "created_at": stamp,
        "operation": operation,
        "status": status,
        "checks": [
            {
                "step_id": check.step_id,
                "title": check.title,
                "status": check.status.value,
                "detail": check.detail,
                "tags": sorted(check.tags),
                "risks": sorted(risk.value for risk in check.risks),
                "remediations": [
                    _remediation_record(remediation)
                    for remediation in check.remediations
                ],
            }
            for check in checks
        ],
        "results": [
            {
                "step_id": result.step_id,
                "title": result.title,
                "status": result.status.value,
                "detail": result.detail,
                "changes": list(result.changes),
                "remediations": [
                    _remediation_record(remediation)
                    for remediation in result.remediations
                ],
                "commands": [
                    {
                        "command": command.command,
                        "returncode": command.returncode,
                        "skipped": command.skipped,
                        "timed_out": command.timed_out,
                        "stdout": command.stdout,
                        "stderr": command.stderr,
                    }
                    for command in result.commands
                ],
            }
            for result in results
        ],
    }
    if interrupted_step_id is not None:
        payload["interrupted_step_id"] = interrupted_step_id
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    ensure_private_file(target, owner)
    return target


def _remediation_record(remediation: Remediation) -> dict[str, object]:
    return {
        "summary": remediation.summary,
        "commands": list(remediation.commands),
        "manual_steps": list(remediation.manual_steps),
    }
