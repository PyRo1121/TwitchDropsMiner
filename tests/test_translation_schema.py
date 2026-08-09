from __future__ import annotations

import json
import string
import unittest
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from translate import default_translation


TranslationPath = tuple[str, ...]


def _string_leaves(
    value: Mapping[str, Any], path: TranslationPath = ()
) -> Iterator[tuple[TranslationPath, str]]:
    for key, item in value.items():
        item_path = (*path, key)
        if isinstance(item, str):
            yield item_path, item
        elif isinstance(item, Mapping):
            yield from _string_leaves(item, item_path)


def _format_fields(template: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name is not None
    }


class TranslationPlaceholderTests(unittest.TestCase):
    def _assert_known_schema(
        self,
        translation: Mapping[str, Any],
        default: Mapping[str, Any],
        *,
        language: str,
        path: TranslationPath = (),
    ) -> None:
        for key, value in translation.items():
            item_path = (*path, key)
            location = f"{language}:{'.'.join(item_path)}"
            if key not in default:
                self.fail(f"{location} is not part of the translation schema")
            default_value = default[key]
            if isinstance(default_value, Mapping):
                if not isinstance(value, Mapping):
                    self.fail(f"{location} must be an object")
                self._assert_known_schema(
                    value,
                    default_value,
                    language=language,
                    path=item_path,
                )
            elif not isinstance(value, str):
                self.fail(f"{location} must be a string")

    def test_translations_preserve_default_format_fields(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        default_strings = dict(_string_leaves(default_translation))

        for language_path in sorted((project_root / "lang").glob("*.json")):
            if language_path.stem == "English":
                continue
            try:
                translation = json.loads(language_path.read_text(encoding="utf8"))
            except (OSError, UnicodeError, ValueError) as exc:
                self.fail(f"Unable to load {language_path.name}: {exc}")
            with self.subTest(language=language_path.stem):
                translated_strings = dict(_string_leaves(translation))
                coverage = len(translated_strings) / len(default_strings)
                print(
                    f"[locale coverage] {language_path.stem}: "
                    f"{len(translated_strings)}/{len(default_strings)} "
                    f"({coverage:.1%}); missing keys use the English fallback"
                )
                self._assert_known_schema(
                    translation,
                    default_translation,
                    language=language_path.name,
                )
                for path, template in _string_leaves(translation):
                    default_template = default_strings[path]
                    self.assertEqual(
                        _format_fields(template),
                        _format_fields(default_template),
                        f"{language_path.name}:{'.'.join(path)} has incompatible format fields",
                    )


if __name__ == "__main__":
    unittest.main()
