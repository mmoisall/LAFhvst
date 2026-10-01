# -*- mode: python ; coding: utf-8 -*-
"""LAFhvst onedir 빌드 스펙.

두 개의 실행 파일을 같은 `_internal` 폴더로 배포한다:
  - LAFhvst.exe    : GUI (main.py)
  - gallery-dl.exe : gallery-dl 콘솔 (core/gdl_executor 가 같은 폴더에서 탐색)
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.dirname(os.path.abspath(SPECPATH))

datas = [
    (os.path.join(ROOT, "frontend"), "frontend"),
    (os.path.join(ROOT, "assets"), "assets"),
    (os.path.join(ROOT, "gallery-dl.conf.example"), "."),
]
binaries = []
hiddenimports = []

for package in ("playwright", "webview", "pythonnet", "clr_loader", "gallery_dl"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "clr",
    "webview.platforms.edgechromium",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]

ICON = os.path.join(ROOT, "assets", "LAFhvst.ico")

gui_analysis = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
gui_pyz = PYZ(gui_analysis.pure)
gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="LAFhvst",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
)

cli_analysis = Analysis(
    [os.path.join(ROOT, "packaging", "gallery_dl_cli.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
cli_pyz = PYZ(cli_analysis.pure)
cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="gallery-dl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

COLLECT(
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    name="LAFhvst",
)
