# -*- mode: python ; coding: utf-8 -*-
# Gera dist/LocalReader/LocalReader.exe (modo pasta: abre bem mais rápido que --onefile).
import os

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
# Pacotes com dados/binários nativos que a análise estática não encontra sozinha
# (espeak-ng-data do Piper, onnxruntime, certificados para o edge-tts, etc.).
for package in ("piper", "onnxruntime", "edge_tts", "gtts", "docx", "pymupdf", "certifi", "pycotovia"):
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
    # A Qwen3-TTS (PyTorch, vários GB) não cabe no instalador: só roda pelo código-fonte.
    excludes=["tkinter", "torch", "torchaudio", "qwen_tts", "faster_qwen3_tts", "transformers"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalReader",
    console=False,
    upx=False,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="LocalReader",
)
