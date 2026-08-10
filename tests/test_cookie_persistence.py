from __future__ import annotations

import asyncio
import os
import pickle
import tempfile
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch
from typing import Any, cast

import aiohttp  # pyright: ignore[reportMissingImports]
from yarl import URL  # pyright: ignore[reportMissingImports]

import data_migration
from data_migration import DataMigrationError, migrate_legacy_data
from http_transport import HttpTransport


class _CookieJar:
    def __init__(self, contents: str = "new") -> None:
        self.contents = contents

    def save(self, path: Path) -> None:
        path.write_text(self.contents, encoding="utf8")


class _FailingCookieJar(_CookieJar):
    def save(self, path: Path) -> None:
        path.write_text("partial", encoding="utf8")
        raise OSError("write failed")


class _UnsafePickle:
    def __init__(self, sentinel: Path) -> None:
        self._sentinel = sentinel

    def __reduce__(self):
        return os.system, (f'touch "{self._sentinel}"',)


class CookiePersistenceTests(unittest.TestCase):
    def test_cookie_save_replaces_the_file_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.jar"
            path.write_text("old", encoding="utf8")
            path.with_name("cookies.jar.new").write_text("obsolete", encoding="utf8")

            HttpTransport.save_cookie_jar(cast(Any, _CookieJar()), path)

            self.assertEqual(path.read_text(encoding="utf8"), "new")
            self.assertFalse(path.with_name("cookies.jar.new").exists())
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cookie_save_failure_keeps_the_last_good_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.jar"
            path.write_text("old", encoding="utf8")

            HttpTransport.save_cookie_jar(cast(Any, _FailingCookieJar()), path)

            self.assertEqual(path.read_text(encoding="utf8"), "old")
            self.assertFalse(path.with_name("cookies.jar.new").exists())
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_migrated_legacy_credential_cookie_uses_exact_host_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "portable"
            data = root / "profile"
            legacy.mkdir()
            source_loop = asyncio.new_event_loop()
            try:
                jar = aiohttp.CookieJar(loop=source_loop)
                jar.update_cookies(
                    {"auth-token": "secret"},
                    URL("https://www.twitch.tv/"),
                )
                (legacy / "cookies.jar").write_bytes(
                    pickle.dumps(jar._cookies, protocol=pickle.HIGHEST_PROTOCOL)
                )
            finally:
                source_loop.close()

            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

            converted = (data / "cookies.jar").read_bytes()
            self.assertTrue(converted.lstrip().startswith(b"{"))
            load_loop = asyncio.new_event_loop()
            self.addCleanup(load_loop.close)
            loaded = aiohttp.CookieJar(loop=load_loop)
            loaded.load(data / "cookies.jar")
            twitch_cookies = loaded.filter_cookies(URL("https://www.twitch.tv/"))
            subdomain_cookies = loaded.filter_cookies(
                URL("https://evil.www.twitch.tv/")
            )
            other_cookies = loaded.filter_cookies(URL("https://example.com/"))
            self.assertEqual(twitch_cookies["auth-token"].value, "secret")
            self.assertNotIn("auth-token", subdomain_cookies)
            self.assertNotIn("auth-token", other_cookies)
            self.assertIn(("www.twitch.tv", "auth-token"), loaded._host_only_cookies)
            self.assertFalse((legacy / "cookies.jar").exists())

    def test_legacy_noncredential_domain_cookie_retains_domain_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "portable"
            data = root / "profile"
            legacy.mkdir()
            source_loop = asyncio.new_event_loop()
            try:
                jar = aiohttp.CookieJar(loop=source_loop)
                domain_cookie = SimpleCookie()
                domain_cookie.load("locale=en; Domain=twitch.tv; Path=/")
                jar.update_cookies(domain_cookie, URL("https://www.twitch.tv/"))
                (legacy / "cookies.jar").write_bytes(
                    pickle.dumps(jar._cookies, protocol=pickle.HIGHEST_PROTOCOL)
                )
            finally:
                source_loop.close()

            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

            load_loop = asyncio.new_event_loop()
            self.addCleanup(load_loop.close)
            loaded = aiohttp.CookieJar(loop=load_loop)
            loaded.load(data / "cookies.jar")
            self.assertEqual(
                loaded.filter_cookies(URL("https://www.twitch.tv/"))["locale"].value,
                "en",
            )
            self.assertEqual(
                loaded.filter_cookies(URL("https://sub.twitch.tv/"))["locale"].value,
                "en",
            )
            self.assertNotIn(("twitch.tv", "locale"), loaded._host_only_cookies)

    def test_legacy_credential_domain_metadata_uses_conservative_exact_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "portable"
            data = root / "profile"
            legacy.mkdir()
            source_loop = asyncio.new_event_loop()
            try:
                jar = aiohttp.CookieJar(loop=source_loop)
                domain_cookie = SimpleCookie()
                domain_cookie.load("auth-token=secret; Domain=twitch.tv; Path=/")
                jar.update_cookies(domain_cookie, URL("https://www.twitch.tv/"))
                (legacy / "cookies.jar").write_bytes(
                    pickle.dumps(jar._cookies, protocol=pickle.HIGHEST_PROTOCOL)
                )
            finally:
                source_loop.close()

            migrate_legacy_data(legacy_dir=legacy, data_dir=data)

            load_loop = asyncio.new_event_loop()
            self.addCleanup(load_loop.close)
            loaded = aiohttp.CookieJar(loop=load_loop)
            loaded.load(data / "cookies.jar")
            self.assertEqual(
                loaded.filter_cookies(URL("https://twitch.tv/"))["auth-token"].value,
                "secret",
            )
            self.assertNotIn(
                "auth-token",
                loaded.filter_cookies(URL("https://www.twitch.tv/")),
            )
            self.assertIn(("twitch.tv", "auth-token"), loaded._host_only_cookies)

    def test_malformed_cookie_json_is_a_controlled_retryable_error(self) -> None:
        cases = (
            "{",
            "[]",
            '{"www.twitch.tv|/":{"auth-token":{}}}',
            (
                '{"www.twitch.tv|/":{"auth-token":'
                '{"key":"auth-token","value":"secret",'
                '"coded_value":"secret","expires_timestamp":1e309}}}'
            ),
        )
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                legacy = root / "portable"
                data = root / "profile"
                legacy.mkdir()
                source = legacy / "cookies.jar"
                source.write_text(payload, encoding="utf8")

                with self.assertRaisesRegex(DataMigrationError, "cookie JSON is malformed"):
                    migrate_legacy_data(legacy_dir=legacy, data_dir=data)

                self.assertEqual(source.read_text(encoding="utf8"), payload)
                self.assertFalse((data / "cookies.jar").exists())
                self.assertFalse((data / "storage.json").exists())
                self.assertFalse(
                    (data / "migration-quarantine" / "cookies.jar.canonical.invalid").exists()
                )

    def test_unexpected_cookie_parser_failure_is_controlled_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "portable"
            data = root / "profile"
            legacy.mkdir()
            source = legacy / "cookies.jar"
            source.write_text("{}", encoding="utf8")

            with (
                patch.object(
                    aiohttp.CookieJar,
                    "load",
                    side_effect=RuntimeError("injected parser failure"),
                ),
                self.assertRaisesRegex(DataMigrationError, "cookie parser failure"),
            ):
                migrate_legacy_data(legacy_dir=legacy, data_dir=data)

            self.assertEqual(source.read_text(encoding="utf8"), "{}")
            self.assertFalse((data / "storage.json").exists())

    def test_cookie_conversion_io_failure_is_retryable_not_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "portable"
            data = root / "profile"
            legacy.mkdir()
            source = legacy / "cookies.jar"
            source_loop = asyncio.new_event_loop()
            try:
                jar = aiohttp.CookieJar(loop=source_loop)
                jar.update_cookies(
                    {"auth-token": "secret"},
                    URL("https://www.twitch.tv/"),
                )
                source.write_bytes(
                    pickle.dumps(jar._cookies, protocol=pickle.HIGHEST_PROTOCOL)
                )
            finally:
                source_loop.close()
            real_read = data_migration._read_regular_file

            def fail_conversion_output(path: Path, maximum_bytes: int) -> bytes:
                if path.parent.name == "migration-work" and path.name.startswith(
                    "cookie-output-"
                ):
                    raise DataMigrationError("injected conversion read failure")
                return real_read(path, maximum_bytes)

            with (
                patch.object(
                    data_migration,
                    "_read_regular_file",
                    side_effect=fail_conversion_output,
                ),
                self.assertRaisesRegex(DataMigrationError, "injected conversion"),
            ):
                migrate_legacy_data(legacy_dir=legacy, data_dir=data)

            self.assertTrue(source.exists())
            self.assertFalse((data / "cookies.jar").exists())
            self.assertFalse((data / "storage.json").exists())
            self.assertFalse(
                (
                    data
                    / "migration-quarantine"
                    / "cookies.jar.canonical.invalid"
                ).exists()
            )

    def test_restricted_cookie_conversion_never_executes_pickle_globals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "portable"
            data = root / "profile"
            sentinel = root / "executed"
            legacy.mkdir()
            payload = pickle.dumps(
                _UnsafePickle(sentinel),
                protocol=pickle.HIGHEST_PROTOCOL,
            )
            (legacy / "cookies.jar").write_bytes(payload)

            result = migrate_legacy_data(legacy_dir=legacy, data_dir=data)

            self.assertFalse(sentinel.exists())
            self.assertFalse((data / "cookies.jar").exists())
            self.assertIn("cookies.jar", result.quarantined)
            self.assertEqual(
                (
                    data
                    / "migration-quarantine"
                    / "cookies.jar.canonical.invalid"
                ).read_bytes(),
                payload,
            )
            self.assertFalse((legacy / "cookies.jar").exists())
            self.assertTrue((data / "storage.json").exists())


if __name__ == "__main__":
    unittest.main()
