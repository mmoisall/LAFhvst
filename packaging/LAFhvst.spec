# -*- mode: python ; coding: utf-8 -*-
"""LAFhvst onedir 빌드 스펙.

세 개의 실행 파일을 같은 `_internal` 폴더로 배포한다:
  - LAFhvst.exe        : GUI (main.py, 트레이 상주)
  - LAFhvst-server.exe : 서버 콘솔 (packaging/server_cli.py, 창 없음)
  - gallery-dl.exe     : gallery-dl 콘솔 (core/gdl_executor 가 같은 폴더에서 탐색)
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

for package in ("playwright", "webview", "pythonnet", "clr_loader", "gallery_dl", "pystray"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "clr",
    "webview.platforms.edgechromium",
    "pystray._win32",
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


def _analysis(script):
    return Analysis(
        [script],
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


gui_analysis = _analysis(os.path.join(ROOT, "main.py"))
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

server_analysis = _analysis(os.path.join(ROOT, "packaging", "server_cli.py"))
server_pyz = PYZ(server_analysis.pure)
server_exe = EXE(
    server_pyz,
    server_analysis.scripts,
    [],
    exclude_binaries=True,
    name="LAFhvst-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=ICON,
)

gdl_analysis = _analysis(os.path.join(ROOT, "packaging", "gallery_dl_cli.py"))
gdl_pyz = PYZ(gdl_analysis.pure)
gdl_exe = EXE(
    gdl_pyz,
    gdl_analysis.scripts,
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
    server_exe,
    server_analysis.binaries,
    server_analysis.datas,
    gdl_exe,
    gdl_analysis.binaries,
    gdl_analysis.datas,
    strip=False,
    upx=False,
    name="LAFhvst",
)
