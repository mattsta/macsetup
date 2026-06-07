from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass

from .model import FileChange, ResourceRef, SetupContext, Step, StepCheck, StepStatus


@dataclass(frozen=True)
class StepPreview:
    step_id: str
    title: str
    status: StepStatus
    detail: str
    file_changes: tuple[FileChange, ...] = ()


def build_previews(
    steps: Sequence[Step], checks: Sequence[StepCheck], context: SetupContext
) -> tuple[StepPreview, ...]:
    checks_by_id = {check.step_id: check for check in checks}
    available_resources: set[ResourceRef] = set()
    provider_by_resource = {
        provided: step.id for step in steps for provided in step.provides
    }
    previews: list[StepPreview] = []

    for step in steps:
        unmet = _unmet_selected_requirements(
            step, available_resources, provider_by_resource
        )
        if unmet:
            previews.append(
                StepPreview(
                    step.id,
                    step.title,
                    StepStatus.SKIPPED,
                    "unmet requirement(s): "
                    + ", ".join(str(resource) for resource in unmet),
                )
            )
            continue

        check = checks_by_id[step.id]
        if check.status == StepStatus.PRESENT:
            available_resources.update(step.provides)
            continue
        if check.status not in {StepStatus.NEEDS_CHANGE, StepStatus.UNKNOWN}:
            continue

        previews.append(
            StepPreview(
                step.id,
                step.title,
                check.status,
                check.detail,
                _file_changes(step, context),
            )
        )
        available_resources.update(step.provides)

    return tuple(previews)


def format_previews(previews: Sequence[StepPreview]) -> str:
    lines = ["", "Managed file preview:"]
    file_previews = [preview for preview in previews if preview.file_changes]
    non_file_previews = [
        preview
        for preview in previews
        if not preview.file_changes
        and preview.status in {StepStatus.NEEDS_CHANGE, StepStatus.UNKNOWN}
    ]
    skipped_previews = [
        preview for preview in previews if preview.status == StepStatus.SKIPPED
    ]

    if not file_previews:
        lines.append("No managed file changes.")
    for preview in file_previews:
        lines.extend(["", f"{preview.step_id}: {preview.title}"])
        for change in preview.file_changes:
            lines.extend(_format_file_change(change))

    if non_file_previews:
        lines.extend(["", "Non-file actionable changes:"])
        for preview in non_file_previews:
            lines.append(f"- {preview.step_id}: {preview.detail}")

    if skipped_previews:
        lines.extend(["", "Skipped by selected graph state:"])
        for preview in skipped_previews:
            lines.append(f"- {preview.step_id}: {preview.detail}")

    return "\n".join(lines)


def _file_changes(step: Step, context: SetupContext) -> tuple[FileChange, ...]:
    preview = getattr(step, "preview", None)
    if not callable(preview):
        return ()
    return tuple(change for change in preview(context) if change.changed)


def _format_file_change(change: FileChange) -> list[str]:
    lines = []
    if change.mode_after is not None:
        before = _format_mode(change.mode_before)
        after = _format_mode(change.mode_after)
        if before != after:
            lines.append(f"mode {change.path}: {before} -> {after}")
    if not change.content_changed:
        return lines

    before_label = "/dev/null" if not change.existed_before else str(change.path)
    after_label = str(change.path)
    diff = difflib.unified_diff(
        change.before.splitlines(keepends=True),
        change.after.splitlines(keepends=True),
        fromfile=before_label,
        tofile=after_label,
        lineterm="",
    )
    lines.extend(line.rstrip("\n") for line in diff)
    return lines


def _format_mode(mode: int | None) -> str:
    return "absent" if mode is None else f"{mode:04o}"


def _unmet_selected_requirements(
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
