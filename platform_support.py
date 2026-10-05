"""Ajustes de plataforma para rodar empacotado (PyInstaller) e no Windows."""
import os
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
