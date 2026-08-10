"""Qt (PySide6) launcher for TwitchDropsMiner.

Equivalent to ``main.py`` and wires the backend to the Qt presentation layer.

Usage:
    python qt_main.py [--tray] [-v] [--log] [--dump]
"""
from __future__ import annotations

import io
import sys
import asyncio
import logging
import argparse
import warnings
import traceback
from typing import NoReturn

try:
    import truststore  # pyright: ignore[reportMissingImports]

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001 - truststore is optional
    pass

import qasync  # pyright: ignore[reportMissingImports]

from PySide6.QtWidgets import (  # pyright: ignore[reportMissingImports]
    QApplication,
    QMessageBox,
)

from data_migration import (
    DataMigrationError,
    migrate_legacy_data,
)
from translate import _
from twitch import Twitch
from gui_qt import QtGUIManager
from settings import Settings
from version import __version__
from utils import lock_file_set
from constants import (
    FILE_FORMATTER,
    LOCK_PATH,
    LOGGING_LEVELS,
    LOG_PATH,
    WORKING_DIR,
)


def _show_error(title: str, text: str) -> None:
    app = QApplication.instance() or QApplication([])
    QMessageBox.critical(None, title, text)


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._message: io.StringIO = io.StringIO()

    def _print_message(self, message: str, file=None) -> None:
        self._message.write(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        try:
            super().exit(status, message)
        finally:
            text = self._message.getvalue()
            if status == 0:
                sys.stdout.write(text)
            else:
                _show_error("Argument Parser Error", text)


class ParsedArgs(argparse.Namespace):
    _verbose: int
    _debug_ws: bool
    _debug_gql: bool
    log: bool
    tray: bool
    dump: bool
    self_test: bool
    allow_insecure_oauth_file: bool

    @property
    def logging_level(self) -> int:
        return LOGGING_LEVELS[min(self._verbose, 4)]

    @property
    def debug_ws(self) -> int:
        if self._debug_ws:
            return logging.DEBUG
        elif self._verbose >= 4:
            return logging.INFO
        return logging.NOTSET

    @property
    def debug_gql(self) -> int:
        if self._debug_gql:
            return logging.DEBUG
        elif self._verbose >= 4:
            return logging.INFO
        return logging.NOTSET


def _build_parser():
    parser = Parser(
        "Twitch Drops Miner",
        description="AFK-mine timed drops on Twitch (Qt UI edition).",
    )
    parser.add_argument("--version", action="version", version=f"v{__version__}")
    parser.add_argument("-v", dest="_verbose", action="count", default=0)
    parser.add_argument("--tray", action="store_true")
    parser.add_argument("--log", action="store_true")
    parser.add_argument("--dump", action="store_true")
    parser.add_argument(
        "--allow-insecure-oauth-file",
        action="store_true",
        help=(
            "explicitly allow a mode-0600 OAuth token file when the native "
            "credential provider is unavailable"
        ),
    )
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug-ws", dest="_debug_ws", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug-gql", dest="_debug_gql", action="store_true", help=argparse.SUPPRESS)
    return parser


async def _self_test(settings: Settings) -> int:
    """Exercise the frozen Qt startup and shutdown path without network I/O."""
    client = Twitch(settings, QtGUIManager)
    try:
        client.gui.start()
        await asyncio.sleep(0)
        client.gui.close()
        await client.shutdown()
        await client.gui.stop()
        client.gui.close_window()
    finally:
        if not client.gui.close_requested:
            client.gui.close()
    return 0


async def _main(settings: Settings, args: ParsedArgs) -> int:
    # language must be set before the UI manager reads translator names
    try:
        _.set_language(settings.language)
    except ValueError:
        pass
    if settings.logging_level > logging.DEBUG:
        logging.getLogger().addHandler(logging.NullHandler())
    logger = logging.getLogger("TwitchDrops")
    logger.setLevel(settings.logging_level)
    if args.log:
        try:
            handler = logging.FileHandler(LOG_PATH)
        except OSError as exc:
            logger.warning("File logging unavailable: %s", type(exc).__name__)
        else:
            handler.setFormatter(FILE_FORMATTER)
            logger.addHandler(handler)
    logging.getLogger("TwitchDrops.gql").setLevel(settings.debug_gql)
    logging.getLogger("TwitchDrops.websocket").setLevel(settings.debug_ws)

    exit_status = 0
    client = Twitch(settings, QtGUIManager)
    loop = asyncio.get_running_loop()
    for sig in (__import__("signal").SIGINT, __import__("signal").SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda *_: client.gui.close())
        except (NotImplementedError, RuntimeError):
            pass  # signal handlers unavailable in this loop/platform
    try:
        await client.run()
    except Exception:
        exit_status = 1
        client.prevent_close()
        client.print(_("gui", "text", "fatal_error"))
        client.print(traceback.format_exc())
    finally:
        try:
            for sig in (__import__("signal").SIGINT, __import__("signal").SIGTERM):
                loop.remove_signal_handler(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        try:
            client.print(_("gui", "status", "exiting"))
        except Exception:
            logger.exception("Unable to report shutdown status")
        try:
            await client.shutdown()
        except Exception:
            exit_status = 1
            logger.exception("Backend shutdown failed")

    try:
        if not client.gui.close_requested:
            client.gui.tray.change_icon("error")
            client.print(_("status", "terminated"))
            client.gui.status.update(_("gui", "status", "terminated"))
            client.gui.grab_attention(sound=True)
        await client.gui.wait_until_closed()
    except Exception:
        exit_status = 1
        logger.exception("GUI close wait failed")
    finally:
        try:
            client.save(force=True)
        except Exception:
            exit_status = 1
            logger.exception("Final save failed")
        try:
            await client.gui.stop()
        except Exception:
            exit_status = 1
            logger.exception("GUI task shutdown failed")
        try:
            client.gui.close_window()
        except Exception:
            exit_status = 1
            logger.exception("GUI window cleanup failed")
    return exit_status


def main() -> int:
    warnings.simplefilter("default", ResourceWarning)

    parser = _build_parser()
    args = parser.parse_args(namespace=ParsedArgs())

    # Lock the executable-relative location first, then the per-user location.
    # Older binaries know only the first path; retaining both locks for this
    # process lifetime prevents old/new credential and settings writers from
    # running concurrently during or after migration.
    legacy_lock_path = WORKING_DIR / "lock.file"
    try:
        success, lock_files = lock_file_set((legacy_lock_path, LOCK_PATH))
    except OSError:
        _show_error(_("gui", "text", "startup_error"), traceback.format_exc())
        return 1
    if not success:
        return 3

    try:
        try:
            migrate_legacy_data()
        except DataMigrationError:
            _show_error(_("gui", "text", "startup_error"), traceback.format_exc())
            return 1

        try:
            settings = Settings(args)
        except Exception:
            _show_error(_("gui", "text", "settings_error"), traceback.format_exc())
            return 4

        app = QApplication([sys.argv[0]])
        if settings.dark_mode:
            app.setStyle("Fusion")
        loop = qasync.QEventLoop(app)
        asyncio.set_event_loop(loop)
        with loop:
            coroutine = _self_test(settings) if args.self_test else _main(settings, args)
            return loop.run_until_complete(coroutine)
    finally:
        for lock in reversed(lock_files):
            lock.close()


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    sys.exit(main())
