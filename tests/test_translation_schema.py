from __future__ import annotations

import json
import string
import unicodedata
import unittest
from collections import Counter
from collections.abc import Iterator, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from translate import _, _validate_translation_catalog, default_translation


TranslationPath = tuple[str, ...]
FormatSignature = Counter[tuple[str, str | None, str | None]]


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


def _catalog_paths(project_root: Path) -> list[Path]:
    return sorted(
        path
        for path in (project_root / "lang").glob("*.json")
        if path.stem != "English"
    )


class TranslationCatalogTests(unittest.TestCase):
    project_root = Path(__file__).resolve().parents[1]
    default_strings = dict(_string_leaves(default_translation))

    def test_catalogs_exactly_match_english_schema_and_placeholders(self) -> None:
        for language_path in _catalog_paths(self.project_root):
            translation = _load_catalog(language_path)
            with self.subTest(language=language_path.stem):
                translated_strings = dict(_string_leaves(translation))
                known_paths = self.default_strings.keys() & translated_strings.keys()
                coverage = len(known_paths) / len(self.default_strings)
                non_identical = sum(
                    translated_strings[path] != self.default_strings[path]
                    for path in known_paths
                )
                non_identical_ratio = non_identical / len(self.default_strings)
                missing_count = len(self.default_strings.keys() - translated_strings.keys())
                unknown_count = len(translated_strings.keys() - self.default_strings.keys())
                print(
                    f"[locale coverage] {language_path.stem}: "
                    f"explicit={len(known_paths)}/{len(self.default_strings)} "
                    f"({coverage:.1%}), non_identical_to_english="
                    f"{non_identical}/{len(self.default_strings)} "
                    f"({non_identical_ratio:.1%}), missing={missing_count}, "
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
            for language_path in _catalog_paths(self.project_root):
                expected = dict(_string_leaves(_load_catalog(language_path)))
                with self.subTest(language=language_path.stem):
                    _.set_language(language_path.stem)
                    self.assertEqual(_.current, language_path.stem)
                    for path, value in expected.items():
                        self.assertEqual(_(*path), value, ".".join(path))
        finally:
            _.set_language("English")

    def test_catalogs_have_no_format_controls_or_unexpected_scripts(self) -> None:
        latin_locales = {
            "Dansk",
            "Deutsch",
            "Español",
            "Français",
            "Indonesian",
            "Italiano",
            "Magyar",
            "Nederlandse",
            "Norsk",
            "Polski",
            "Português",
            "Română",
            "Türkçe",
            "Čeština",
        }
        cyrillic_locales = {"Русский", "Українська"}

        def script(character: str) -> str | None:
            codepoint = ord(character)
            if 0x3400 <= codepoint <= 0x9FFF:
                return "Han"
            if 0x3040 <= codepoint <= 0x30FF:
                return "Kana"
            if 0x0400 <= codepoint <= 0x052F:
                return "Cyrillic"
            if 0x0600 <= codepoint <= 0x06FF:
                return "Arabic"
            return None

        for language_path in _catalog_paths(self.project_root):
            strings = dict(_string_leaves(_load_catalog(language_path)))
            with self.subTest(language=language_path.stem):
                controls = [
                    (path, character)
                    for path, value in strings.items()
                    for character in value
                    if unicodedata.category(character) == "Cf"
                ]
                self.assertEqual(controls, [], "format-control characters are forbidden")

                if language_path.stem in latin_locales:
                    forbidden_scripts = {"Han", "Kana", "Cyrillic", "Arabic"}
                elif language_path.stem in cyrillic_locales:
                    forbidden_scripts = {"Han", "Kana", "Arabic"}
                elif language_path.stem == "العربية":
                    forbidden_scripts = {"Han", "Kana", "Cyrillic"}
                elif language_path.stem in {"简体中文", "繁體中文"}:
                    forbidden_scripts = {"Kana", "Cyrillic", "Arabic"}
                else:  # Japanese intentionally uses both Kana and Han.
                    forbidden_scripts = {"Cyrillic", "Arabic"}
                unexpected = [
                    (path, character, character_script)
                    for path, value in strings.items()
                    for character in value
                    if (character_script := script(character)) in forbidden_scripts
                ]
                self.assertEqual(unexpected, [], "unexpected mixed-script content")

    def test_confirmed_action_and_grammar_regressions_are_absent(self) -> None:
        prohibited: dict[str, tuple[str, ...]] = {
            "Dansk": (
                "bedste egne",
                "et sendekanal",
                "Gyldiggør",
                "Gyldiggør autentiserings-token (log ud):",
            ),
            "Deutsch": ("Sender wird abgerufen",),
            "Magyar": (
                "流程",
                "két áram",
                "Kiválasztott a közvetített",
                "Az hitelesítési jelvény érvénytelenítése (kijelentkezés):",
            ),
            "Čeština": ("Ověřte toto použití", "Přeodhadování", "BEŽÍ"),
            "Українська": (
                "це застосунок",
                "device-code",
                "Сподиватися",
                "Скасувати токен авторизації (вийти в систему):",
            ),
            "Română": (
                "Urează",
                "a a două",
                "următoarea minut",
                "DERulare",
                "ÎNTERupt",
            ),
            "العربية": ("خصم",),
            "Nederlandse": ("streambekeking", "GEÏNTERREPT"),
            "Norsk": ("strømer", "Et sendekanal", "strømmetilsyn", "AVBROKKET"),
            "Русский": ("Экспериментальное просмотр",),
            "繁體中文": ("导致",),
        }
        for language, bad_values in prohibited.items():
            catalog = _load_catalog(self.project_root / "lang" / f"{language}.json")
            text = "\n".join(value for _, value in _string_leaves(catalog))
            with self.subTest(language=language):
                for bad_value in bad_values:
                    self.assertNotIn(bad_value, text)

    def test_help_catalogs_match_current_five_step_contract(self) -> None:
        for language_path in _catalog_paths(self.project_root):
            catalog = _load_catalog(language_path)
            gui = catalog["gui"]
            assert isinstance(gui, Mapping)
            help_text = gui["help"]
            assert isinstance(help_text, Mapping)
            how_it_works = help_text["how_it_works_text"]
            getting_started = help_text["getting_started_text"]
            assert isinstance(how_it_works, str)
            assert isinstance(getting_started, str)
            steps = getting_started.splitlines()
            with self.subTest(language=language_path.stem):
                self.assertNotIn("60", how_it_works)
                self.assertNotIn("\n", how_it_works)
                self.assertEqual(len(steps), 5)
                self.assertEqual(
                    [step[:3] for step in steps],
                    ["1. ", "2. ", "3. ", "4. ", "5. "],
                )

    def test_native_review_status_is_explicitly_pending(self) -> None:
        documentation = (self.project_root / "lang" / "README.md").read_text(
            encoding="utf8"
        )
        self.assertIn("Catalog schema version: `2`", documentation)
        self.assertIn("Help source revision: `2`", documentation)
        for language_path in _catalog_paths(self.project_root):
            with self.subTest(language=language_path.stem):
                self.assertIn(
                    f"| {language_path.stem} | Machine-assisted | Pending | — |",
                    documentation,
                )

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
