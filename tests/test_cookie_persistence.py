from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from twitch import Twitch


class _CookieJar:
    def __init__(self, contents: str = "new") -> None:
        self.contents = contents
        self.calls = 0

    def save(self, path: Path) -> None:
        self.calls += 1
        path.write_text(self.contents, encoding="utf8")


class _FailingCookieJar:
    def save(self, path: Path) -> None:
        path.write_text("partial", encoding="utf8")
        raise OSError("disk full")


class CookiePersistenceTests(unittest.TestCase):
    def test_cookie_save_replaces_the_file_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.jar"
            path.write_text("old", encoding="utf8")
            jar = _CookieJar()

            Twitch._save_cookie_jar(cast(Any, jar), path)

            self.assertEqual(path.read_text(encoding="utf8"), "new")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(path.with_name("cookies.jar.new").exists())
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])
            self.assertEqual(jar.calls, 1)

    def test_cookie_save_failure_keeps_the_last_good_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.jar"
            path.write_text("old", encoding="utf8")

            Twitch._save_cookie_jar(cast(Any, _FailingCookieJar()), path)

            self.assertEqual(path.read_text(encoding="utf8"), "old")
            self.assertFalse(path.with_name("cookies.jar.new").exists())
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
