# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller configuration for the Qt production launcher."""
from __future__ import annotations

import atexit
import fnmatch
import shutil
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

from PyInstaller.building.api import EXE, COLLECT, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.building.datastruct import TOC
from PyInstaller.building.osx import BUNDLE
from PyInstaller.depend import bindepend
from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
if str(SPEC_DIR) not in sys.path:
    sys.path.insert(0, str(SPEC_DIR))

from constants import WORKING_DIR, DEFAULT_LANG


upx = False
console = False
one_dir = False
optimize = None
app_name = "Twitch Drops Miner (by DevilXD)"

# Qt and the runtime locale/icon assets are the only application data needed by
# the production launcher. PyInstaller's Qt hooks collect platform plugins.
datas: list[tuple[str, str]] = []
for path in (
    WORKING_DIR / "icons" / "pickaxe.ico",
    WORKING_DIR / "icons" / "active.ico",
    WORKING_DIR / "icons" / "idle.ico",
    WORKING_DIR / "icons" / "error.ico",
    WORKING_DIR / "icons" / "maint.ico",
):
    if not path.exists():
        raise FileNotFoundError(str(path))
    datas.append((str(path), "icons"))
brand_symbol = WORKING_DIR / "gui_qt" / "assets" / "drop_deck_brand.png"
if not brand_symbol.exists():
    raise FileNotFoundError(str(brand_symbol))
datas.append((str(brand_symbol), "gui_qt/assets"))
for path in (WORKING_DIR / "lang").glob("*.json"):
    if path.stem != DEFAULT_LANG:
        datas.append((str(path), "lang"))

hiddenimports = [
    "qasync",
    "qtawesome",
    "qtpy",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]
# Keep qasync's optional loop integrations discoverable in frozen builds.
hiddenimports.extend(collect_submodules("qasync"))

# Some standalone Python distributions mark libpython's GNU_STACK segment as
# executable. That flag is unnecessary for the embedded interpreter and can be
# rejected by hardened Linux kernels when the one-file app starts. Patch only a
# temporary copy, never the active interpreter installation.
_original_python_library = bindepend.get_python_library_path
_patched_python_library: str | None = None


def _patch_python_library_stack(source: str) -> str:
    global _patched_python_library
    if _patched_python_library is not None or sys.platform != "linux":
        return _patched_python_library or source
    raw = bytearray(Path(source).read_bytes())
    if raw[:4] != b"\x7fELF" or raw[4] not in (1, 2) or raw[5] not in (1, 2):
        return source
    order = "<" if raw[5] == 1 else ">"
    if raw[4] == 2:
        phoff = struct.unpack_from(f"{order}Q", raw, 32)[0]
        phentsize = struct.unpack_from(f"{order}H", raw, 54)[0]
        phnum = struct.unpack_from(f"{order}H", raw, 56)[0]
        flags_offset = 4
    else:
        phoff = struct.unpack_from(f"{order}I", raw, 28)[0]
        phentsize = struct.unpack_from(f"{order}H", raw, 42)[0]
        phnum = struct.unpack_from(f"{order}H", raw, 44)[0]
        flags_offset = 24
    changed = False
    for index in range(phnum):
        entry = phoff + index * phentsize
        p_type = struct.unpack_from(f"{order}I", raw, entry)[0]
        if p_type != 0x6474E551:  # PT_GNU_STACK
            continue
        current = struct.unpack_from(f"{order}I", raw, entry + flags_offset)[0]
        if current & 1:  # PF_X
            struct.pack_into(f"{order}I", raw, entry + flags_offset, current & ~1)
            changed = True
    if not changed:
        return source
    directory = Path(tempfile.mkdtemp(prefix="tdm-libpython-"))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    patched = directory / Path(source).name
    patched.write_bytes(raw)
    _patched_python_library = str(patched)
    return _patched_python_library


def _get_python_library_path() -> str:
    return _patch_python_library_stack(_original_python_library())


bindepend.get_python_library_path = _get_python_library_path

analysis = Analysis(
    [str(WORKING_DIR / "main.py")],
    pathex=[str(WORKING_DIR)],
    datas=datas,
    binaries=[],
    hiddenimports=hiddenimports,
    excludes=["pystray", "tkinter", "PIL.ImageTk"],
    hooksconfig={},
)

# Qt/PyInstaller already supplies the required platform plugins. Keep the
# historical size exclusions that are safe for this application.
excluded = [
    "libicudata.so.*",
    "libicuuc.so.*",
    "librsvg-*.so.*",
]
if sys.platform == "linux":
    # Image downloads are decoded by Pillow and converted to PNG before Qt
    # consumes them; the optional TIFF plugin is not used by the application.
    excluded.append("*libqtiff.so*")
analysis.binaries = TOC(
    item for item in analysis.binaries if not any(fnmatch.fnmatch(item[0], pattern) for pattern in excluded)
)

if one_dir:
    exe_args: tuple[Any, ...] = ()
    collect_args: tuple[Any, ...] = (analysis.datas, analysis.binaries)
else:
    exe_args = (analysis.datas, analysis.binaries)
    collect_args = ()

pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    *exe_args,
    upx=upx,
    debug=False,
    name=app_name,
    console=console,
    optimize=optimize,
    exclude_binaries=one_dir,
    icon=str(WORKING_DIR / "icons" / "pickaxe.ico"),
)

if one_dir:
    coll = COLLECT(exe, *collect_args, upx=upx, name=app_name)

if sys.platform == "darwin":
    source = coll if one_dir else exe
    app = BUNDLE(
        source,
        name=f"{app_name}.app",
        icon=str(WORKING_DIR / "icons" / "pickaxe.ico"),
        bundle_identifier="com.twitchdrops.miner",
    )
