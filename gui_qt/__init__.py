"""(c) Qt (PySide6) presentation layer for TwitchDropsMiner.

Implements the GUI contract the Python backend drives with a modern dark
design. Injected into ``Twitch`` via the ``gui_factory`` seam in ``twitch.py``.
"""
from __future__ import annotations

from .manager import QtGUIManager, QtSettings

__all__ = ["QtGUIManager", "QtSettings"]
