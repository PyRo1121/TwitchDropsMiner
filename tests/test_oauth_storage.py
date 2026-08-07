from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from oauth_storage import OAuthTokenStore


class OAuthTokenStoreTests(unittest.TestCase):
    def test_save_load_and_clear_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            store = OAuthTokenStore(path)

            store.save("client-a", "refresh-secret")

            self.assertEqual(store.load("client-a"), "refresh-secret")
            self.assertIsNone(store.load("client-b"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            store.clear()
            self.assertIsNone(store.load("client-a"))
            self.assertFalse(path.exists())

    def test_malformed_or_mismatched_records_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            store = OAuthTokenStore(path)
            path.write_text('{"client_id":"other","refresh_token":"secret"}', encoding="utf8")
            self.assertIsNone(store.load("client-a"))
            path.write_text("not-json", encoding="utf8")
            self.assertIsNone(store.load("client-a"))

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires no-follow open support")
    def test_save_does_not_follow_a_temporary_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "oauth.json"
            target = root / "outside.txt"
            target.write_text("protected", encoding="utf8")
            path.with_name("oauth.json.new").symlink_to(target)
            store = OAuthTokenStore(path)

            with self.assertRaises(OSError):
                store.save("client-a", "fresh")

            self.assertEqual(target.read_text(encoding="utf8"), "protected")

    def test_save_replaces_a_partial_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            temporary = path.with_name("oauth.json.new")
            temporary.write_text("partial", encoding="utf8")
            store = OAuthTokenStore(path)

            store.save("client-a", "fresh")

            self.assertEqual(store.load("client-a"), "fresh")
            self.assertFalse(temporary.exists())


if __name__ == "__main__":
    unittest.main()
