from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .model import ResourceRef, Step


@dataclass(frozen=True)
class GraphIssue:
    kind: str
    detail: str


class GraphError(RuntimeError):
    def __init__(self, issues: Sequence[GraphIssue]) -> None:
        super().__init__("\n".join(issue.detail for issue in issues))
        self.issues = tuple(issues)


@dataclass(frozen=True)
class StepGraph:
    steps: tuple[Step, ...]

    def selected(
        self, *, only_tags: frozenset[str], skip_tags: frozenset[str]
    ) -> tuple[Step, ...]:
        candidates = tuple(
            step for step in self.steps if not (skip_tags and step.tags & skip_tags)
        )
        selected = tuple(
            step for step in candidates if not only_tags or step.tags & only_tags
        )
        return self.ordered(self._with_provider_closure(selected, candidates))

    def _with_provider_closure(
        self, selected: Sequence[Step], candidates: Sequence[Step]
    ) -> tuple[Step, ...]:
        provider_by_resource = self._resource_owner(candidates, "provides")
        selected_by_id = {step.id: step for step in selected}
        pending = list(selected)
        while pending:
            step = pending.pop(0)
            for requirement in step.requires:
                provider = provider_by_resource.get(requirement)
                if provider is None or provider.id in selected_by_id:
                    continue
                selected_by_id[provider.id] = provider
                pending.append(provider)
        return tuple(step for step in candidates if step.id in selected_by_id)

    def ordered(self, selected: Iterable[Step]) -> tuple[Step, ...]:
        selected_steps = tuple(selected)
        self.validate_or_raise(selected_steps)
        selected_by_id = {step.id: step for step in selected_steps}
        provider_by_resource = self._resource_owner(selected_steps, "provides")
        prerequisites: dict[str, set[str]] = {step.id: set() for step in selected_steps}
        dependents: dict[str, set[str]] = {step.id: set() for step in selected_steps}
        for step in selected_steps:
            for requirement in step.requires:
                provider = provider_by_resource.get(requirement)
                if provider is None or provider.id == step.id:
                    continue
                prerequisites[step.id].add(provider.id)
                dependents[provider.id].add(step.id)

        index = {step.id: position for position, step in enumerate(selected_steps)}
        ready = sorted(
            (step_id for step_id, deps in prerequisites.items() if not deps),
            key=index.__getitem__,
        )
        ordered_ids: list[str] = []
        while ready:
            step_id = ready.pop(0)
            ordered_ids.append(step_id)
            for dependent_id in sorted(dependents[step_id], key=index.__getitem__):
                prerequisites[dependent_id].discard(step_id)
                if (
                    not prerequisites[dependent_id]
                    and dependent_id not in ordered_ids
                    and dependent_id not in ready
                ):
                    ready.append(dependent_id)
            ready.sort(key=index.__getitem__)

        if len(ordered_ids) != len(selected_steps):
            cyclic = sorted(step_id for step_id, deps in prerequisites.items() if deps)
            raise GraphError(
                (
                    GraphIssue(
                        "cycle", "dependency cycle involving: " + ", ".join(cyclic)
                    ),
                )
            )
        return tuple(selected_by_id[step_id] for step_id in ordered_ids)

    def validate_or_raise(self, selected: Sequence[Step] | None = None) -> None:
        issues = self.validate(self.steps if selected is None else selected)
        if issues:
            raise GraphError(issues)

    def validate(self, selected: Sequence[Step]) -> tuple[GraphIssue, ...]:
        issues: list[GraphIssue] = []
        step_ids: dict[str, str] = {}
        for step in selected:
            if step.id in step_ids:
                issues.append(
                    GraphIssue("duplicate-step", f"duplicate step id: {step.id}")
                )
            step_ids[step.id] = step.title

        owners: dict[ResourceRef, str] = {}
        for step in selected:
            for owned in step.owns:
                owner = owners.get(owned)
                if owner is not None and owner != step.id:
                    issues.append(
                        GraphIssue(
                            "ownership-conflict",
                            f"{owned} is owned by both {owner} and {step.id}",
                        )
                    )
                owners[owned] = step.id

        providers: dict[ResourceRef, str] = {}
        for step in selected:
            for provided in step.provides:
                provider = providers.get(provided)
                if provider is not None and provider != step.id:
                    issues.append(
                        GraphIssue(
                            "provider-conflict",
                            f"{provided} is provided by both {provider} and {step.id}",
                        )
                    )
                providers[provided] = step.id
        return tuple(issues)

    def _resource_owner(
        self, steps: Sequence[Step], attribute: str
    ) -> dict[ResourceRef, Step]:
        resources: dict[ResourceRef, Step] = {}
        for step in steps:
            for resource in getattr(step, attribute):
                resources[resource] = step
        return resources


def format_graph(steps: Sequence[Step]) -> str:
    lines: list[str] = []
    for step in steps:
        lines.append(f"{step.id} [{','.join(sorted(step.tags))}]")
        lines.append(f"  requires: {_format_resources(step.requires)}")
        lines.append(f"  provides: {_format_resources(step.provides)}")
        lines.append(f"  owns:     {_format_resources(step.owns)}")
    return "\n".join(lines)


def _format_resources(resources: frozenset[ResourceRef]) -> str:
    if not resources:
        return "-"
    return ", ".join(str(resource) for resource in sorted(resources))
