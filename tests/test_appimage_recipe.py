from __future__ import annotations

import importlib
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "appimage/AppImageBuilder.yml"


class AppImageRecipeTests(unittest.TestCase):
    def test_every_script_item_is_a_string_and_schema_validates(self) -> None:
        try:
            recipe = importlib.import_module("appimagebuilder.recipe")
        except ModuleNotFoundError as exc:
            if exc.name != "appimagebuilder":
                raise
            self.skipTest("pinned appimage-builder toolchain is not installed")

        loaded: Any = recipe.Loader().load(RECIPE)
        self.assertIsInstance(loaded, dict)
        if not isinstance(loaded, dict):
            self.fail("AppImage recipe did not load as a mapping")

        script: Any = loaded.get("script")
        self.assertIsInstance(script, list)
        if not isinstance(script, list):
            self.fail("AppImage recipe script did not load as a list")
        self.assertTrue(script)
        for index, command in enumerate(script):
            self.assertIsInstance(
                command,
                str,
                f"AppImage recipe script item {index} is not a string",
            )

        recipe.Schema().validate(recipe.Roamer(loaded))


if __name__ == "__main__":
    unittest.main()
