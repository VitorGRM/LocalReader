# -*- mode: python ; coding: utf-8 -*-
# Gera dist/TTSReader/TTSReader.exe (modo pasta: abre bem mais rápido que --onefile).
import os

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
# Pacotes com dados/binários nativos que a análise estática não encontra sozinha
# (espeak-ng-data do Piper, onnxruntime, certificados para o edge-tts, etc.).
for package in ("piper", "onnxruntime", "edge_tts", "gtts", "docx", "pymupdf", "certifi"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

icon = "assets/icon.ico" if os.path.exists("assets/icon.ico") else None

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TTSReader",
    console=False,
    upx=False,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="TTSReader",
)
