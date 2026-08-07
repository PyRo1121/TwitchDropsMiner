from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_metadata import SteamMetadata, SteamMetadataProvider


class _Response:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def json(self, **_kwargs):
        return self._payload


class _Twitch:
    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def request(self, _method: str, url):
        text = str(url)
        self.urls.append(text)
        if "store.steampowered.com" in text:
            payload = self.responses["store"]
        else:
            payload = self.responses["players"]
        return _Response(payload)


class GameMetadataTests(unittest.TestCase):
    def test_normalization_is_conservative_and_case_insensitive(self) -> None:
        self.assertEqual(
            SteamMetadataProvider.normalize_name("Pokémon® Trading Card Game"),
            "pokemontradingcardgame",
        )
        self.assertEqual(
            SteamMetadataProvider.normalize_name("Marvel: Rivals"),
            SteamMetadataProvider.normalize_name("marvel rivals"),
        )

    def test_urls_fall_back_to_search_without_an_app_id(self) -> None:
        metadata = SteamMetadata("Game With Spaces")
        self.assertIn("search/?term=Game+With+Spaces", metadata.store_url)
        self.assertIn("q=Game+With+Spaces", metadata.steamdb_url)

    def test_price_formats_steam_cents_without_formatted_text(self) -> None:
        self.assertEqual(
            SteamMetadataProvider._price({"currency": "USD", "final": 1499}),
            ("$14.99", None),
        )

    def test_provider_matches_exact_game_and_fetches_players(self) -> None:
        twitch = _Twitch(
            {
                "store": {
                    "items": [
                        {
                            "id": 761890,
                            "name": "Albion Online",
                            "price": {
                                "final_formatted": "$0.00",
                                "discount_percent": 0,
                            },
                        }
                    ]
                },
                "players": {"response": {"player_count": 12345}},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / "steam-metadata.json"
            with patch.object(SteamMetadataProvider, "CACHE_FILE", cache_file):
                provider = SteamMetadataProvider(twitch)
                result = asyncio.run(provider.get("Albion Online"))
                provider.save(force=True)
                cached = json.loads(cache_file.read_text(encoding="utf8"))

        self.assertEqual(result.app_id, 761890)
        self.assertEqual(result.players, 12345)
        self.assertEqual(result.price, "$0.00")
        self.assertFalse(result.free_to_play)
        self.assertIn("/app/761890/", result.steamdb_url)
        self.assertEqual(len(twitch.urls), 2)
        self.assertTrue(cached)

    def test_provider_does_not_guess_a_nearby_game(self) -> None:
        twitch = _Twitch(
            {
                "store": {"items": [{"id": 1, "name": "Albion Online"}]},
                "players": {"response": {"player_count": 1}},
            }
        )
        provider = SteamMetadataProvider(twitch)
        result = asyncio.run(provider.get("Albion Offline"))
        self.assertIsNone(result.app_id)
        self.assertEqual(len(twitch.urls), 1)


if __name__ == "__main__":
    unittest.main()
