import unittest
from dataclasses import dataclass

from macsetup.cli import selected_provider_by_resource, unmet_selected_requirements
from macsetup.graph import GraphError, StepGraph
from macsetup.model import (
    ResourceRef,
    Risk,
    SetupContext,
    StepCheck,
    StepResult,
    StepStatus,
    resource,
)
from macsetup.recipes import DEFAULT_RECIPE
from macsetup.steps import build_steps


@dataclass(frozen=True)
class DummyStep:
    id: str
    title: str
    requires: frozenset[ResourceRef]
    provides: frozenset[ResourceRef]
    owns: frozenset[ResourceRef]
    tags: frozenset[str] = frozenset({"test"})
    risks: frozenset[Risk] = frozenset()

    def check(self, context: SetupContext) -> StepCheck:
        return StepCheck(
            self.id, self.title, StepStatus.PRESENT, "ok", self.tags, self.risks
        )

    def apply(self, context: SetupContext) -> StepResult:
        return StepResult(self.id, self.title, StepStatus.PRESENT, "ok")


class GraphTests(unittest.TestCase):
    def test_provider_is_ordered_before_dependent(self) -> None:
        provided = resource("tool", "example")
        dependent = DummyStep(
            "dependent", "Dependent", frozenset({provided}), frozenset(), frozenset()
        )
        provider = DummyStep(
            "provider", "Provider", frozenset(), frozenset({provided}), frozenset()
        )
        ordered = StepGraph((dependent, provider)).ordered((dependent, provider))
        self.assertEqual([step.id for step in ordered], ["provider", "dependent"])

    def test_zshrc_blocks_order_oh_my_zsh_before_options(self) -> None:
        from macsetup.steps import _zshrc_block_steps

        steps = _zshrc_block_steps()
        # Feed them to the graph in reverse to prove ordering comes from the
        # requires-chain, not declaration order.
        ordered = StepGraph(tuple(steps)).ordered(tuple(reversed(steps)))
        ordered_ids = [step.id for step in ordered]
        self.assertLess(
            ordered_ids.index("shell.zshrc.oh-my-zsh"),
            ordered_ids.index("shell.zshrc.options"),
        )
        self.assertEqual(ordered_ids, [step.id for step in steps])

    def test_ownership_conflicts_are_rejected(self) -> None:
        owned = resource("file", "~/.example")
        first = DummyStep(
            "first", "First", frozenset(), frozenset(), frozenset({owned})
        )
        second = DummyStep(
            "second", "Second", frozenset(), frozenset(), frozenset({owned})
        )
        with self.assertRaises(GraphError):
            StepGraph((first, second)).ordered((first, second))

    def test_homebrew_analytics_precedes_package_groups(self) -> None:
        steps = StepGraph(build_steps(DEFAULT_RECIPE)).selected(
            only_tags=frozenset({"packages"}), skip_tags=frozenset()
        )
        ids = [step.id for step in steps]
        self.assertLess(
            ids.index("macos.xcode-developer-directory"),
            ids.index("macos.xcode-license"),
        )
        self.assertLess(ids.index("macos.xcode-license"), ids.index("homebrew.install"))
        self.assertLess(
            ids.index("macos.xcode-metal-toolchain"),
            ids.index("homebrew.install"),
        )
        self.assertLess(
            ids.index("homebrew.analytics-off"), ids.index("brew.formulas.shell")
        )
        self.assertLess(
            ids.index("homebrew.share-permissions"), ids.index("brew.formulas.shell")
        )
        self.assertLess(
            ids.index("homebrew.cleanup-phantomjs"), ids.index("brew.casks.terminal")
        )
        self.assertIn("shell.oh-my-zsh", ids)

    def test_tag_selection_includes_transitive_providers(self) -> None:
        steps = StepGraph(build_steps(DEFAULT_RECIPE)).selected(
            only_tags=frozenset({"git"}), skip_tags=frozenset()
        )
        ids = [step.id for step in steps]

        self.assertIn("macos.xcode-license", ids)
        self.assertIn("macos.xcode-developer-directory", ids)
        self.assertIn("homebrew.install", ids)
        self.assertIn("homebrew.analytics-off", ids)
        self.assertIn("brew.formulas.git", ids)
        self.assertIn("brew.formulas.editor", ids)
        self.assertLess(ids.index("homebrew.install"), ids.index("brew.formulas.git"))
        self.assertLess(
            ids.index("brew.formulas.editor"), ids.index("git.config.core.editor")
        )

    def test_skip_tags_excludes_provider_closure(self) -> None:
        steps = StepGraph(build_steps(DEFAULT_RECIPE)).selected(
            only_tags=frozenset({"git"}), skip_tags=frozenset({"packages"})
        )
        ids = {step.id for step in steps}

        self.assertIn("git.config.core.editor", ids)
        self.assertNotIn("homebrew.install", ids)
        self.assertNotIn("brew.formulas.editor", ids)

    def test_unmet_requirements_are_detected_for_selected_providers(self) -> None:
        provided = resource("tool", "example")
        provider = DummyStep(
            "provider", "Provider", frozenset(), frozenset({provided}), frozenset()
        )
        dependent = DummyStep(
            "dependent", "Dependent", frozenset({provided}), frozenset(), frozenset()
        )
        providers = selected_provider_by_resource((provider, dependent))
        self.assertEqual(
            unmet_selected_requirements(dependent, set(), providers), (provided,)
        )
        self.assertEqual(
            unmet_selected_requirements(dependent, {provided}, providers), ()
        )


if __name__ == "__main__":
    unittest.main()
