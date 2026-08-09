from __future__ import annotations

import unittest

from game import Game


class GameSlugTests(unittest.TestCase):
    def test_decimal_string_id_is_supported(self) -> None:
        self.assertEqual(Game({"id": "42", "name": "Game"}).id, 42)

    def test_lossy_or_boolean_ids_are_rejected(self) -> None:
        for value in (True, 1.5, "1.5", -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Game({"id": value, "name": "Game"})

    def test_slug_from_data_is_used(self) -> None:
        game = Game({"id": 1, "name": "Counter-Strike", "slug": "csgo"})

        self.assertEqual(game.slug, "csgo")

    def test_slug_falls_back_to_computed_value(self) -> None:
        game = Game({"id": 1, "name": "Grand Theft Auto V"})

        self.assertEqual(game.slug, "grand-theft-auto-v")

    def test_slug_fallback_handles_special_characters(self) -> None:
        game = Game({"id": 1, "name": "No Man's Sky™"})

        self.assertEqual(game.slug, "no-mans-sky")


if __name__ == "__main__":
    unittest.main()
