"""Primary TwitchDropsMiner launcher.

The application uses the PySide6 presentation layer. The production entry
point lives in ``qt_main.py``.
"""
from __future__ import annotations

from multiprocessing import freeze_support

from qt_main import main


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
