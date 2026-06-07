import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from macsetup.homebrew_resolver import (
    HomebrewFormulaDependencyResolver,
    InstalledReceiptSource,
    LocalFormulaFileSource,
    parse_formula_dependencies,
)


class StaticFormulaSource:
    def __init__(self, dependencies: dict[str, frozenset[str]]) -> None:
        self.dependencies = dependencies

    def dependencies_for(self, formula: str) -> frozenset[str]:
        return self.dependencies.get(formula, frozenset())


class HomebrewResolverTests(unittest.TestCase):
    def test_parse_formula_dependencies_keeps_runtime_and_skips_build_deps(
        self,
    ) -> None:
        source = """
class BashCompletionAT2 < Formula
  depends_on "autoconf" => :build
  depends_on "automake" => [:build, :test]

  on_macos do
    depends_on "bash"
  end
end
"""

        self.assertEqual(parse_formula_dependencies(source), frozenset({"bash"}))

    def test_uninstall_order_places_dependents_before_dependencies(self) -> None:
        resolver = HomebrewFormulaDependencyResolver(
            StaticFormulaSource(
                {
                    "bash-completion@2": frozenset({"bash"}),
                    "bash": frozenset({"readline"}),
                    "readline": frozenset(),
                }
            )
        )

        self.assertEqual(
            resolver.uninstall_order(("bash", "readline", "bash-completion@2")),
            ("bash-completion@2", "bash", "readline"),
        )

    def test_local_source_reads_homebrew_tap_formula_layout(self) -> None:
        with TemporaryDirectory() as directory:
            prefix = Path(directory)
            formula_root = (
                prefix
                / "Library"
                / "Taps"
                / "homebrew"
                / "homebrew-core"
                / "Formula"
                / "b"
            )
            formula_root.mkdir(parents=True)
            (formula_root / "bash-completion@2.rb").write_text(
                'depends_on "bash"\n', encoding="utf-8"
            )

            source = LocalFormulaFileSource.from_prefix(prefix)

            self.assertEqual(
                source.dependencies_for("bash-completion@2"), frozenset({"bash"})
            )

    def test_installed_receipt_source_reads_runtime_dependencies(self) -> None:
        with TemporaryDirectory() as directory:
            prefix = Path(directory)
            receipt = (
                prefix
                / "Cellar"
                / "bash-completion@2"
                / "2.17.0"
                / "INSTALL_RECEIPT.json"
            )
            receipt.parent.mkdir(parents=True)
            receipt.write_text(
                """
{
  "runtime_dependencies": [
    {"full_name": "bash", "declared_directly": true},
    {"full_name": "homebrew/core/readline", "declared_directly": false}
  ]
}
""",
                encoding="utf-8",
            )

            source = InstalledReceiptSource.from_prefix(prefix)

            self.assertEqual(
                source.dependencies_for("bash-completion@2"),
                frozenset({"bash", "readline"}),
            )

    def test_receipt_source_can_drive_uninstall_order_without_formula_tap(self) -> None:
        with TemporaryDirectory() as directory:
            prefix = Path(directory)
            receipt = (
                prefix
                / "Cellar"
                / "bash-completion@2"
                / "2.17.0"
                / "INSTALL_RECEIPT.json"
            )
            receipt.parent.mkdir(parents=True)
            receipt.write_text(
                '{"runtime_dependencies": [{"full_name": "bash"}]}', encoding="utf-8"
            )
            source = InstalledReceiptSource.from_prefix(prefix)
            resolver = HomebrewFormulaDependencyResolver(source)

            self.assertEqual(
                resolver.uninstall_order(("bash", "bash-completion@2")),
                ("bash-completion@2", "bash"),
            )

    def test_blocking_dependents_reports_installed_formulas_outside_target_set(
        self,
    ) -> None:
        resolver = HomebrewFormulaDependencyResolver(
            StaticFormulaSource(
                {
                    "bash-completion@2": frozenset({"bash"}),
                    "bash": frozenset(),
                }
            )
        )

        self.assertEqual(
            resolver.blocking_dependents(("bash",), ("bash", "bash-completion@2")),
            {"bash": ("bash-completion@2",)},
        )


if __name__ == "__main__":
    unittest.main()
