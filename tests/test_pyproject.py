import tomllib
import unittest
from pathlib import Path


class PyprojectTests(unittest.TestCase):
    def test_uv_entrypoint_is_declared(self) -> None:
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            pyproject["project"]["scripts"]["macsetup"], "macsetup.cli:main"
        )
        self.assertEqual(
            pyproject["project"]["scripts"]["macsetup-transfer"],
            "macsetup.transfer:main",
        )
        self.assertEqual(
            pyproject["project"]["scripts"]["mrsync"], "macsetup.mrsync:main"
        )
        self.assertEqual(pyproject["tool"]["uv"]["package"], True)
        self.assertEqual(pyproject["project"]["requires-python"], ">=3.11")
        self.assertEqual(
            pyproject["dependency-groups"]["dev"], ["mypy", "pytest", "ruff"]
        )
        self.assertIn(
            "macsetup.defaults.templates",
            pyproject["tool"]["setuptools"]["packages"],
        )
        self.assertEqual(
            pyproject["tool"]["setuptools"]["package-data"][
                "macsetup.defaults.templates"
            ],
            ["*"],
        )


if __name__ == "__main__":
    unittest.main()
