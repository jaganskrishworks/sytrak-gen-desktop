# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build:  pyinstaller --noconfirm --clean sytrak_gen.spec
# Produces a single self-contained dist/SyTrakGen.exe (no Python needed on target).
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [("assets", "assets")]
datas += collect_data_files("ttkbootstrap")

# PIL._tkinter_finder / _imagingtk: loaded at runtime by ImageTk on Linux, PyInstaller misses them.
hiddenimports = collect_submodules("ttkbootstrap") + [
    "PIL.ImageTk", "PIL._tkinter_finder", "PIL._imagingtk", "qrcode.image.pil",
]

a = Analysis(
    ["main.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "numpy", "paho"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SyTrakGen",
    icon="assets/icons/app.ico",
    console=False,
    upx=False,
    debug=False,
    strip=False,
    runtime_tmpdir=None,
)
