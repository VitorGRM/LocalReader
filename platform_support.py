"""Ajustes de plataforma para rodar empacotado (PyInstaller) e no Windows."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Sem isso, cada chamada ao Tesseract abriria uma janela de console piscando
# quando o app roda sem console (.exe).
SUBPROCESS_KWARGS: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_data_dir(*parts: str) -> str:
    """Pasta de dados do app (no Windows, %LOCALAPPDATA%\\LocalReader\\TTSReader\\...)."""
    from PyQt6.QtCore import QStandardPaths

    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    return os.path.join(base, "LocalReader", "TTSReader", *parts)


def setup_bundled_tools() -> Path | None:
    """Coloca o Tesseract no PATH e devolve a pasta usada (ou None).

    No app empacotado usa o Tesseract embutido ao lado do executável e força
    o seu ``tessdata``. No Windows rodando do código-fonte, aceita uma
    instalação padrão em Program Files sem sobrescrever TESSDATA_PREFIX.
    """
    bundled = []
    system = []
    if is_frozen():
        bundled.append(Path(sys.executable).parent / "tesseract")
    elif os.name == "nt":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(variable)
            if base:
                system.append(Path(base) / "Tesseract-OCR")

    for directory in bundled + system:
        if not (directory / "tesseract.exe").is_file():
            continue
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
        tessdata = directory / "tessdata"
        if tessdata.is_dir():
            if directory in bundled:
                os.environ["TESSDATA_PREFIX"] = str(tessdata)
            else:
                os.environ.setdefault("TESSDATA_PREFIX", str(tessdata))
        return directory
    return None


def available_tesseract_languages() -> list[str]:
    """Retorna os modelos realmente instalados, sem incluir o modelo OSD."""
    executable = shutil.which("tesseract")
    if not executable:
        return []
    try:
        completed = subprocess.run(
            [executable, "--list-langs"], capture_output=True, text=True, timeout=15, check=False,
            **SUBPROCESS_KWARGS,
        )
        lines = (completed.stdout + "\n" + completed.stderr).splitlines()
        langs = [line.strip() for line in lines if re.fullmatch(r"[A-Za-z0-9_]+", line.strip())]
        return sorted({lang for lang in langs if lang != "osd"})
    except (OSError, subprocess.SubprocessError):
        return []
