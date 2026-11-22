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


APP_NAME = "LocalReader"
# Até a v1.5.x o programa se chamava "TTS Reader": configurações e dados ficavam
# com esse nome e são trazidos por ``migrate_legacy_data``.
_LEGACY_APP = "TTSReader"
_LEGACY_CACHE = "tts-reader"
_MIGRATED_KEY = "migrated_from_tts_reader"


def _settings(application: str):
    from PyQt6.QtCore import QSettings

    return QSettings(APP_NAME, application)


def app_settings():
    """Configurações do app (QSettings)."""
    return _settings(APP_NAME)


def _generic_data_dir() -> str:
    from PyQt6.QtCore import QStandardPaths

    return QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)


def app_data_dir(*parts: str) -> str:
    """Pasta de dados do app (Windows: %LOCALAPPDATA%\\LocalReader; Linux: ~/.local/share/LocalReader)."""
    return os.path.join(_generic_data_dir(), APP_NAME, *parts)


def cache_dir(*parts: str) -> Path:
    """Pasta das vozes baixadas (~/.cache/localreader)."""
    return Path.home() / ".cache" / APP_NAME.lower() / Path(*parts)


def migrate_legacy_data() -> list[str]:
    """Traz configurações, realces, cache do OCR e vozes do nome antigo. Só age na
    primeira abertura depois da troca de nome; devolve o que foi migrado."""
    migrated = []
    new_settings = app_settings()
    if not new_settings.value(_MIGRATED_KEY, False, type=bool):
        # Copia cada configuração que ainda não existe no nome novo (mesmo que
        # alguma já tenha sido gravada antes, por exemplo numa abertura anterior).
        old_settings = _settings(_LEGACY_APP)
        copied = [key for key in old_settings.allKeys() if not new_settings.contains(key)]
        for key in copied:
            new_settings.setValue(key, old_settings.value(key))
        new_settings.setValue(_MIGRATED_KEY, True)
        new_settings.sync()
        if copied:
            migrated.append("configurações")

    moves = [
        (Path(_generic_data_dir()) / APP_NAME / _LEGACY_APP, Path(app_data_dir())),
        (Path.home() / ".cache" / _LEGACY_CACHE, cache_dir()),
    ]
    for old_dir, new_dir in moves:
        if not old_dir.is_dir():
            continue
        new_dir.mkdir(parents=True, exist_ok=True)
        for child in list(old_dir.iterdir()):
            target = new_dir / child.name
            if not target.exists():
                try:
                    os.replace(child, target)
                    migrated.append(str(target))
                except OSError:
                    pass  # fica no lugar antigo; o app recria o que precisar
        try:
            old_dir.rmdir()
        except OSError:
            pass
    return migrated


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
