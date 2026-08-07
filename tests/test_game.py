from __future__ import annotations

import unittest

from game import Game


class GameSlugTests(unittest.TestCase):
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
