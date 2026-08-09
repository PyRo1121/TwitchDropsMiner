from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from yarl import URL

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
        self.transport = self

    def request(self, _method: str, url):
        text = str(url)
        self.urls.append(text)
        if URL(text).host == "store.steampowered.com":
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
            cache_path = Path(directory)
            cache_file = cache_path / "steam-metadata.json"
            with patch("game_metadata.CACHE_PATH", cache_path), patch.object(
                SteamMetadataProvider, "CACHE_FILE", cache_file
            ):
                provider = SteamMetadataProvider(twitch)
                result = asyncio.run(provider.get("Albion Online"))
                provider.save(force=True)
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf8"))
                except (OSError, UnicodeError, ValueError) as exc:
                    self.fail(f"Unable to load saved metadata: {exc}")

        self.assertEqual(result.app_id, 761890)
        self.assertEqual(result.players, 12345)
        self.assertEqual(result.price, "$0.00")
        self.assertFalse(result.free_to_play)
        self.assertIn("/app/761890/", result.steamdb_url)
        self.assertEqual(len(twitch.urls), 2)
        self.assertTrue(cached)

    def test_missing_price_is_not_mislabeled_free_to_play(self) -> None:
        twitch = _Twitch(
            {
                "store": {"items": [{"id": 10, "name": "Unpriced Game"}]},
                "players": {"response": {"player_count": 1}},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory)
            with patch("game_metadata.CACHE_PATH", cache_path), patch.object(
                SteamMetadataProvider,
                "CACHE_FILE",
                cache_path / "steam-metadata.json",
            ):
                result = asyncio.run(
                    SteamMetadataProvider(twitch).get("Unpriced Game")
                )

        self.assertIsNone(result.price)
        self.assertIsNone(result.free_to_play)

    def test_transient_failures_are_not_cached(self) -> None:
        twitch = _Twitch({"store": {}, "players": {}})
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory)
            cache_file = cache_path / "steam-metadata.json"
            with patch("game_metadata.CACHE_PATH", cache_path), patch.object(
                SteamMetadataProvider,
                "CACHE_FILE",
                cache_file,
            ):
                provider = SteamMetadataProvider(twitch)
                request = AsyncMock(side_effect=OSError("temporary"))
                provider._json = request  # type: ignore[method-assign]

                async def exercise() -> tuple[SteamMetadata, SteamMetadata]:
                    first = await provider.get("Albion Online")
                    await asyncio.sleep(0)
                    second = await provider.get("Albion Online")
                    return first, second

                first, second = asyncio.run(exercise())

        self.assertEqual(first.error, "OSError")
        self.assertEqual(second.error, "OSError")
        self.assertEqual(request.await_count, 2)
        self.assertFalse(provider._cache)

    def test_provider_does_not_guess_a_nearby_game(self) -> None:
        twitch = _Twitch(
            {
                "store": {"items": [{"id": 1, "name": "Albion Online"}]},
                "players": {"response": {"player_count": 1}},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory)
            cache_file = cache_path / "steam-metadata.json"
            with patch("game_metadata.CACHE_PATH", cache_path), patch.object(
                SteamMetadataProvider, "CACHE_FILE", cache_file
            ):
                provider = SteamMetadataProvider(twitch)
                result = asyncio.run(provider.get("Albion Offline"))
        self.assertIsNone(result.app_id)
        self.assertEqual(len(twitch.urls), 1)

    def test_cache_pruning_keeps_only_recent_bounded_entries(self) -> None:
        twitch = _Twitch({"store": {}, "players": {}})
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory)
            cache_file = cache_path / "steam-metadata.json"
            with patch("game_metadata.CACHE_PATH", cache_path), patch.object(
                SteamMetadataProvider,
                "CACHE_FILE",
                cache_file,
            ), patch("game_metadata.time.time", return_value=1000.0):
                provider = SteamMetadataProvider(twitch)
                provider._cache = {
                    f"game-{index}": {
                        "updated_at": 900.0 + index,
                        "free_to_play": True,
                    }
                    for index in range(4)
                }
                with patch.object(
                    SteamMetadataProvider,
                    "MAX_CACHE_ENTRIES",
                    2,
                ):
                    provider._prune_cache()

        self.assertEqual(set(provider._cache), {"game-2", "game-3"})

    def test_cancelled_waiter_does_not_cancel_shared_lookup(self) -> None:
        twitch = _Twitch({"store": {}, "players": {}})

        async def exercise() -> None:
            started = asyncio.Event()
            release = asyncio.Event()
            calls = 0
            with tempfile.TemporaryDirectory() as directory:
                cache_path = Path(directory)
                cache_file = cache_path / "steam-metadata.json"
                with patch("game_metadata.CACHE_PATH", cache_path), patch.object(
                    SteamMetadataProvider,
                    "CACHE_FILE",
                    cache_file,
                ):
                    provider = SteamMetadataProvider(twitch)

                    async def fetch(name: str, _key: str) -> SteamMetadata:
                        nonlocal calls
                        calls += 1
                        started.set()
                        await release.wait()
                        return SteamMetadata(game_name=name, app_id=1)

                    with patch.object(provider, "_fetch", side_effect=fetch):
                        first = asyncio.create_task(provider.get("Game"))
                        await started.wait()
                        second = asyncio.create_task(provider.get("Game"))
                        await asyncio.sleep(0)
                        first.cancel()
                        await asyncio.gather(first, return_exceptions=True)
                        release.set()
                        result = await second

            self.assertEqual(result.app_id, 1)
            self.assertEqual(calls, 1)

        asyncio.run(exercise())

    def test_last_cancelled_waiter_cancels_underlying_lookup(self) -> None:
        twitch = _Twitch({"store": {}, "players": {}})

        async def exercise() -> None:
            started = asyncio.Event()
            cancelled = asyncio.Event()
            with tempfile.TemporaryDirectory() as directory:
                cache_path = Path(directory)
                cache_file = cache_path / "steam-metadata.json"
                with patch("game_metadata.CACHE_PATH", cache_path), patch.object(
                    SteamMetadataProvider,
                    "CACHE_FILE",
                    cache_file,
                ):
                    provider = SteamMetadataProvider(twitch)

                    async def fetch(name: str, _key: str) -> SteamMetadata:
                        started.set()
                        try:
                            await asyncio.Event().wait()
                        finally:
                            cancelled.set()
                        return SteamMetadata(game_name=name)

                    with patch.object(provider, "_fetch", side_effect=fetch):
                        waiter = asyncio.create_task(provider.get("Game"))
                        await started.wait()
                        waiter.cancel()
                        await asyncio.gather(waiter, return_exceptions=True)
                        await asyncio.wait_for(cancelled.wait(), timeout=1)
                        await asyncio.sleep(0)

                    self.assertEqual(provider._inflight, {})
                    self.assertEqual(provider._waiters, {})

        asyncio.run(exercise())

    def test_save_disables_unavailable_persistence(self) -> None:
        twitch = _Twitch({"store": {}, "players": {}})
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache"
            cache_path.write_text("not a directory", encoding="utf8")
            cache_file = cache_path / "steam-metadata.json"
            with patch("game_metadata.CACHE_PATH", cache_path), patch.object(
                SteamMetadataProvider, "CACHE_FILE", cache_file
            ):
                provider = SteamMetadataProvider(twitch)
                provider._altered = True
                provider.save(force=True)
                self.assertFalse(provider._persistence_available)


if __name__ == "__main__":
    unittest.main()
