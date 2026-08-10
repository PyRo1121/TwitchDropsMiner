from __future__ import annotations

import json
import string
import unittest
from collections import Counter
from collections.abc import Iterator, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from translate import _, _validate_translation_catalog, default_translation


TranslationPath = tuple[str, ...]
FormatSignature = Counter[tuple[str, str | None, str | None]]
MINIMUM_LOCALIZED_RATIO = 0.85


def _string_leaves(
    value: Mapping[str, Any], path: TranslationPath = ()
) -> Iterator[tuple[TranslationPath, str]]:
    for key, item in value.items():
        item_path = (*path, key)
        if isinstance(item, str):
            yield item_path, item
        elif isinstance(item, Mapping):
            yield from _string_leaves(item, item_path)


def _format_signature(template: str) -> FormatSignature:
    return Counter(
        (field_name, conversion, format_spec)
        for _, field_name, format_spec, conversion in string.Formatter().parse(template)
        if field_name is not None
    )


def _schema_errors(
    translation: Mapping[str, Any],
    default: Mapping[str, Any],
    *,
    path: TranslationPath = (),
) -> list[str]:
    errors: list[str] = []
    missing = sorted(default.keys() - translation.keys())
    unknown = sorted(translation.keys() - default.keys())
    errors.extend(f"{'.'.join((*path, key))}: missing key" for key in missing)
    errors.extend(f"{'.'.join((*path, key))}: unknown key" for key in unknown)

    for key in default.keys() & translation.keys():
        item_path = (*path, key)
        location = ".".join(item_path)
        default_value = default[key]
        value = translation[key]
        if isinstance(default_value, Mapping):
            if not isinstance(value, Mapping):
                errors.append(f"{location}: expected object, got {type(value).__name__}")
            else:
                errors.extend(_schema_errors(value, default_value, path=item_path))
        elif not isinstance(value, str):
            errors.append(f"{location}: expected string, got {type(value).__name__}")
        elif not value.strip():
            errors.append(f"{location}: empty translation")
    return errors


def _load_catalog(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AssertionError(f"Unable to load {path.name}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise AssertionError(f"{path.name}: catalog root must be an object")
    return value


class TranslationCatalogTests(unittest.TestCase):
    project_root = Path(__file__).resolve().parents[1]
    default_strings = dict(_string_leaves(default_translation))

    def test_catalogs_exactly_match_english_schema_and_placeholders(self) -> None:
        for language_path in sorted((self.project_root / "lang").glob("*.json")):
            translation = _load_catalog(language_path)
            with self.subTest(language=language_path.stem):
                translated_strings = dict(_string_leaves(translation))
                known_paths = self.default_strings.keys() & translated_strings.keys()
                coverage = len(known_paths) / len(self.default_strings)
                localized = sum(
                    translated_strings[path] != self.default_strings[path]
                    for path in known_paths
                )
                localized_ratio = localized / len(self.default_strings)
                missing_count = len(self.default_strings.keys() - translated_strings.keys())
                unknown_count = len(translated_strings.keys() - self.default_strings.keys())
                print(
                    f"[locale coverage] {language_path.stem}: "
                    f"explicit={len(known_paths)}/{len(self.default_strings)} "
                    f"({coverage:.1%}), localized={localized}/{len(self.default_strings)} "
                    f"({localized_ratio:.1%}), missing={missing_count}, "
                    f"unknown={unknown_count}"
                )

                errors = _schema_errors(translation, default_translation)
                self.assertFalse(
                    errors,
                    f"{language_path.name} does not match the English schema:\n"
                    + "\n".join(errors),
                )
                self.assertEqual(
                    coverage,
                    1.0,
                    f"{language_path.name} has incomplete explicit coverage",
                )
                self.assertGreaterEqual(
                    localized_ratio,
                    MINIMUM_LOCALIZED_RATIO,
                    f"{language_path.name} copies too much English to be a real translation",
                )

                for path, template in translated_strings.items():
                    try:
                        signature = _format_signature(template)
                    except ValueError as exc:
                        self.fail(
                            f"{language_path.name}:{'.'.join(path)} has an invalid "
                            f"format string: {exc}"
                        )
                    self.assertEqual(
                        signature,
                        _format_signature(self.default_strings[path]),
                        f"{language_path.name}:{'.'.join(path)} has incompatible "
                        "placeholder names, counts, conversions, or format specs",
                    )

                _validate_translation_catalog(
                    translation,
                    language=language_path.name,
                )

    def test_runtime_validator_rejects_schema_and_placeholder_drift(self) -> None:
        cases: list[tuple[str, dict[str, Any], str]] = []

        missing = cast(dict[str, Any], deepcopy(default_translation))
        del missing["status"]["watching"]
        cases.append(("missing", missing, "missing keys: watching"))

        unknown = cast(dict[str, Any], deepcopy(default_translation))
        unknown["status"]["surprise"] = "Surprise"
        cases.append(("unknown", unknown, "unknown keys: surprise"))

        type_drift = cast(dict[str, Any], deepcopy(default_translation))
        type_drift["status"]["watching"] = 3
        cases.append(("type", type_drift, "expected a string"))

        placeholder_drift = cast(dict[str, Any], deepcopy(default_translation))
        placeholder_drift["status"]["watching"] = "Watching: {streamer}"
        cases.append(("placeholder", placeholder_drift, "placeholders do not match"))

        for name, catalog, message in cases:
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, message):
                _validate_translation_catalog(catalog, language="Test")

    def test_every_catalog_loads_without_english_fallback(self) -> None:
        try:
            for language_path in sorted((self.project_root / "lang").glob("*.json")):
                expected = dict(_string_leaves(_load_catalog(language_path)))
                with self.subTest(language=language_path.stem):
                    _.set_language(language_path.stem)
                    self.assertEqual(_.current, language_path.stem)
                    for path, value in expected.items():
                        self.assertEqual(_(*path), value, ".".join(path))
        finally:
            _.set_language("English")

    def test_arabic_catalog_contains_native_rtl_text(self) -> None:
        arabic = _load_catalog(self.project_root / "lang" / "العربية.json")
        text = "".join(
            value
            for path, value in _string_leaves(arabic)
            if path != ("english_name",)
        )
        letters = [character for character in text if character.isalpha()]
        rtl_letters = [
            character for character in letters if "\u0600" <= character <= "\u06ff"
        ]
        self.assertGreater(len(letters), 1000)
        self.assertGreaterEqual(
            len(rtl_letters) / len(letters),
            0.80,
            "Arabic locale has insufficient Arabic-script content for an RTL UI",
        )


if __name__ == "__main__":
    unittest.main()
