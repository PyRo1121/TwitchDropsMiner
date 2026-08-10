from __future__ import annotations

import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path, PurePosixPath

from build_tools.package_release import ArchiveEntry, create_archive, parse_entry


class ReleasePackagingTests(unittest.TestCase):
    def test_archive_is_ordered_and_independent_of_source_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            payload = root / "payload"
            payload.mkdir()
            executable = payload / "miner"
            executable.write_bytes(b"native payload\n")
            executable.chmod(0o755)
            manual = root / "manual.txt"
            manual.write_text("instructions\n", encoding="utf8")
            entries = [
                ArchiveEntry(manual, PurePosixPath("Twitch Drops Miner/manual.txt")),
                ArchiveEntry(payload, PurePosixPath("Twitch Drops Miner/app")),
            ]
            first = root / "first.zip"
            second = root / "second.zip"

            create_archive(first, entries, epoch=1_700_000_001)
            os.utime(manual, (1_800_000_000, 1_800_000_000))
            os.utime(executable, (1_900_000_000, 1_900_000_000))
            create_archive(second, list(reversed(entries)), epoch=1_700_000_001)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), sorted(archive.namelist()))
                executable_info = archive.getinfo("Twitch Drops Miner/app/miner")
                self.assertEqual(stat.S_IMODE(executable_info.external_attr >> 16), 0o755)
                self.assertEqual(executable_info.date_time[-1] % 2, 0)

    @unittest.skipIf(os.name == "nt", "creating symlinks requires extra Windows privileges")
    def test_archive_preserves_bundle_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundle = root / "Example.app"
            versions = bundle / "Contents" / "Frameworks" / "Example.framework" / "Versions"
            current = versions / "Current"
            target = versions / "A"
            target.mkdir(parents=True)
            current.symlink_to("A", target_is_directory=True)
            output = root / "bundle.zip"

            create_archive(output, [ArchiveEntry(bundle, PurePosixPath("Example.app"))], 0)

            with zipfile.ZipFile(output) as archive:
                link = archive.getinfo("Example.app/Contents/Frameworks/Example.framework/Versions/Current")
                self.assertTrue(stat.S_ISLNK(link.external_attr >> 16))
                self.assertEqual(archive.read(link), b"A")

    def test_entry_paths_reject_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "artifact"
            source.touch()
            with self.assertRaisesRegex(ValueError, "unsafe archive path"):
                parse_entry(f"{source}=../artifact")


if __name__ == "__main__":
    unittest.main()
