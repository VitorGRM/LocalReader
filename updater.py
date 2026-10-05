"""Atualizações via GitHub Releases, sem dependências além da biblioteca padrão.

Fluxo: consulta ``releases/latest`` do repositório, compara com a versão atual,
baixa ``TTSReader-Setup.exe`` conferindo o SHA-256 publicado em
``TTSReader-Setup.exe.sha256`` e executa o instalador em modo silencioso.
Este módulo não depende de Qt; a interface fica em ``update_ui.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

import app_version

INSTALLER_NAME = "TTSReader-Setup.exe"
CHECKSUM_NAME = INSTALLER_NAME + ".sha256"
API_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 30
CHUNK_SIZE = 64 * 1024
USER_AGENT = "TTSReader-Updater"


class UpdateError(Exception):
    """Falha ao consultar, baixar ou validar uma atualização."""


class UpdateCancelled(Exception):
    """O usuário cancelou o download."""


@dataclass
class UpdateInfo:
    version: str
    notes: str
    page_url: str
    installer_url: str | None
    checksum_url: str | None

    @property
    def installable(self) -> bool:
        return bool(self.installer_url and self.checksum_url)


def configured_repo() -> str:
    return (app_version.GITHUB_REPO or "").strip()


def parse_version(text: str) -> tuple[int, ...]:
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    if not match:
        raise ValueError(f"Versão inválida: {text!r}")
    parts = [int(p) for p in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()  # 1.2 e 1.2.0 são a mesma versão
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def _request(url: str, timeout: float, accept: str | None = None):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def fetch_latest_release(repo: str) -> dict:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise UpdateError(f"Repositório inválido: {repo!r} (use o formato usuario/repositorio).")
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        with _request(url, API_TIMEOUT, "application/vnd.github+json") as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError(
                f"Nenhuma release encontrada em github.com/{repo}. "
                "O repositório precisa ser público e ter ao menos uma release publicada."
            ) from exc
        if exc.code == 403:
            raise UpdateError("Limite de consultas do GitHub atingido. Tente novamente mais tarde.") from exc
        raise UpdateError(f"O GitHub respondeu com erro HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Não foi possível acessar o GitHub: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UpdateError("Resposta inválida do GitHub.") from exc


def _asset_url(release: dict, name: str) -> str | None:
    for asset in release.get("assets", []):
        url = asset.get("browser_download_url", "")
        if asset.get("name") == name and url.startswith("https://"):
            return url
    return None


def check_for_update(repo: str, current: str) -> UpdateInfo | None:
    """Devolve a release mais nova que ``current`` ou None se já está atualizado."""
    release = fetch_latest_release(repo)
    tag = release.get("tag_name", "")
    try:
        newer = is_newer(tag, current)
    except ValueError as exc:
        raise UpdateError(f"A última release tem uma tag fora do padrão: {tag!r}.") from exc
    if not newer:
        return None
    return UpdateInfo(
        version=tag.lstrip("vV"),
        notes=(release.get("body") or "").strip(),
        page_url=release.get("html_url", f"https://github.com/{repo}/releases"),
        installer_url=_asset_url(release, INSTALLER_NAME),
        checksum_url=_asset_url(release, CHECKSUM_NAME),
    )


def download_installer(
    info: UpdateInfo,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """Baixa o instalador para uma pasta temporária e valida o SHA-256."""
    if not info.installable:
        raise UpdateError("A release não contém o instalador e o arquivo de checksum esperados.")

    try:
        with _request(info.checksum_url, API_TIMEOUT) as response:
            checksum_text = response.read(4096).decode("ascii", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Falha ao baixar o checksum: {exc}") from exc
    tokens = checksum_text.split()
    expected = tokens[0].lower() if tokens else ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise UpdateError("Arquivo de checksum inválido.")

    directory = tempfile.mkdtemp(prefix="tts_reader_update_")
    destination = os.path.join(directory, INSTALLER_NAME)
    digest = hashlib.sha256()
    try:
        with _request(info.installer_url, DOWNLOAD_TIMEOUT) as response, open(destination, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            while True:
                if cancelled is not None and cancelled():
                    raise UpdateCancelled()
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if progress is not None:
                    progress(received, total)
        if digest.hexdigest() != expected:
            raise UpdateError("O arquivo baixado não confere com o checksum publicado; instalação abortada.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise UpdateError(f"Falha durante o download: {exc}") from exc
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return destination


def launch_installer(path: str) -> None:
    """Inicia o instalador desacoplado; o chamador deve encerrar o app em seguida."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1"],
        creationflags=flags,
        close_fds=True,
    )
