from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class FormulaDependencySource(Protocol):
    def dependencies_for(self, formula: str) -> frozenset[str]: ...


class FormulaDependencyResolver(Protocol):
    def uninstall_order(self, formulas: Sequence[str]) -> tuple[str, ...]: ...

    def blocking_dependents(
        self, targets: Sequence[str], installed: Sequence[str]
    ) -> Mapping[str, tuple[str, ...]]: ...


@dataclass(frozen=True)
class LocalFormulaFileSource:
    formula_roots: tuple[Path, ...]
    _cache: dict[str, frozenset[str]] = field(
        default_factory=dict, init=False, repr=False
    )

    @classmethod
    def from_prefix(cls, prefix: Path) -> LocalFormulaFileSource:
        taps = prefix / "Library" / "Taps"
        if not taps.exists():
            return cls(())
        roots = tuple(
            sorted(path for path in taps.glob("*/*/Formula") if path.is_dir())
        )
        return cls(roots)

    def dependencies_for(self, formula: str) -> frozenset[str]:
        cached = self._cache.get(formula)
        if cached is not None:
            return cached
        path = self.formula_path(formula)
        if path is None:
            dependencies = frozenset[str]()
        else:
            dependencies = parse_formula_dependencies(path.read_text(encoding="utf-8"))
        self._cache[formula] = dependencies
        return dependencies

    def formula_path(self, formula: str) -> Path | None:
        token = formula.rsplit("/", 1)[-1]
        if not token:
            return None
        relative_candidates = (
            Path(token[0].lower()) / f"{token}.rb",
            Path(f"{token}.rb"),
        )
        for root in self.formula_roots:
            for relative in relative_candidates:
                candidate = root / relative
                if candidate.exists():
                    return candidate
        return None


@dataclass(frozen=True)
class InstalledReceiptSource:
    cellar: Path
    _cache: dict[str, frozenset[str]] = field(
        default_factory=dict, init=False, repr=False
    )

    @classmethod
    def from_prefix(cls, prefix: Path) -> InstalledReceiptSource:
        return cls(prefix / "Cellar")

    def dependencies_for(self, formula: str) -> frozenset[str]:
        cached = self._cache.get(formula)
        if cached is not None:
            return cached
        dependencies = self._read_dependencies(formula)
        self._cache[formula] = dependencies
        return dependencies

    def _read_dependencies(self, formula: str) -> frozenset[str]:
        formula_dir = self.cellar / formula
        if not formula_dir.exists():
            return frozenset()
        dependencies: set[str] = set()
        for receipt in formula_dir.glob("*/INSTALL_RECEIPT.json"):
            try:
                payload = json.loads(receipt.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for item in payload.get("runtime_dependencies", ()):
                if not isinstance(item, Mapping):
                    continue
                full_name = item.get("full_name")
                if isinstance(full_name, str) and full_name:
                    dependencies.add(full_name.rsplit("/", 1)[-1])
        return frozenset(dependencies)


@dataclass(frozen=True)
class CompositeFormulaDependencySource:
    sources: tuple[FormulaDependencySource, ...]

    def dependencies_for(self, formula: str) -> frozenset[str]:
        dependencies: set[str] = set()
        for source in self.sources:
            dependencies.update(source.dependencies_for(formula))
        return frozenset(dependencies)


@dataclass(frozen=True)
class HomebrewFormulaDependencyResolver:
    source: FormulaDependencySource

    def uninstall_order(self, formulas: Sequence[str]) -> tuple[str, ...]:
        requested = tuple(dict.fromkeys(formulas))
        selected = frozenset(requested)
        dependencies = {
            formula: frozenset(
                dependency
                for dependency in self.source.dependencies_for(formula)
                if dependency in selected
            )
            for formula in requested
        }
        return _dependents_before_dependencies(requested, dependencies)

    def blocking_dependents(
        self, targets: Sequence[str], installed: Sequence[str]
    ) -> Mapping[str, tuple[str, ...]]:
        selected = frozenset(targets)
        blockers: dict[str, list[str]] = {target: [] for target in targets}
        for formula in dict.fromkeys(installed):
            if formula in selected:
                continue
            for dependency in self.source.dependencies_for(formula):
                if dependency in selected:
                    blockers[dependency].append(formula)
        return {
            target: tuple(dependents)
            for target, dependents in blockers.items()
            if dependents
        }


@dataclass(frozen=True)
class NullFormulaDependencyResolver:
    def uninstall_order(self, formulas: Sequence[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(formulas))

    def blocking_dependents(
        self, targets: Sequence[str], installed: Sequence[str]
    ) -> Mapping[str, tuple[str, ...]]:
        return {}


_DEPENDS_ON_RE = re.compile(r'^\s*depends_on\s+"([^"]+)"(?P<rest>.*)$')
_NON_RUNTIME_QUALIFIERS = (":build", ":test", ":optional")


def parse_formula_dependencies(source: str) -> frozenset[str]:
    dependencies = set[str]()
    for raw_line in source.splitlines():
        match = _DEPENDS_ON_RE.match(raw_line)
        if match is None:
            continue
        qualifiers = match.group("rest") or ""
        if any(qualifier in qualifiers for qualifier in _NON_RUNTIME_QUALIFIERS):
            continue
        dependencies.add(match.group(1))
    return frozenset(dependencies)


def resolver_from_homebrew_prefix(prefix: Path | None) -> FormulaDependencyResolver:
    if prefix is None:
        return NullFormulaDependencyResolver()
    sources: list[FormulaDependencySource] = []
    receipt_source = InstalledReceiptSource.from_prefix(prefix)
    if receipt_source.cellar.exists():
        sources.append(receipt_source)
    formula_source = LocalFormulaFileSource.from_prefix(prefix)
    if formula_source.formula_roots:
        sources.append(formula_source)
    if not sources:
        return NullFormulaDependencyResolver()
    return HomebrewFormulaDependencyResolver(
        CompositeFormulaDependencySource(tuple(sources))
    )


def _dependents_before_dependencies(
    requested: tuple[str, ...],
    dependencies: Mapping[str, frozenset[str]],
) -> tuple[str, ...]:
    selected = frozenset(requested)
    outgoing = {
        formula: set(dependencies.get(formula, frozenset()) & selected)
        for formula in requested
    }
    incoming_count = {formula: 0 for formula in requested}
    for formula_dependencies in outgoing.values():
        for dependency in formula_dependencies:
            incoming_count[dependency] += 1

    ready = [formula for formula in requested if incoming_count[formula] == 0]
    ordered: list[str] = []
    while ready:
        formula = ready.pop(0)
        ordered.append(formula)
        for dependency in requested:
            if dependency not in outgoing.get(formula, set()):
                continue
            incoming_count[dependency] -= 1
            if incoming_count[dependency] == 0:
                ready.append(dependency)

    if len(ordered) != len(requested):
        ordered_set = set(ordered)
        ordered.extend(formula for formula in requested if formula not in ordered_set)
    return tuple(ordered)
