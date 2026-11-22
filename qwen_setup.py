"""Instala o ambiente da Qwen3-TTS sob demanda (PyTorch com CUDA, faster-qwen3-tts).

O PyTorch tem vários GB e não cabe no instalador do app. Quando o usuário
pede, este módulo baixa o ``uv`` (instalador de Python da Astral, versão fixa e
conferida pelo SHA-256 publicado), e com ele cria em ``<dados do app>/qwen-runtime``
um Python 3.12 próprio com os pacotes da Qwen. O app usa esse ambiente pelo
processo ``qwen_worker.py``, sem misturar bibliotecas. Remover é apagar a pasta.
Funciona igual no Windows e no Linux (x86_64 e ARM64).
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from typing import Callable

from platform_support import SUBPROCESS_KWARGS
from qwen_voices import runtime_dir, runtime_python

UV_VERSION = "0.13.6"
PYTHON_VERSION = "3.12"
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"  # CUDA 12.8: placas RTX 50xx e anteriores
PACKAGES = ["faster-qwen3-tts>=0.5.4,<0.6", "transformers==5.15.1"]
DOWNLOAD_ESTIMATE_GB = 3.5  # PyTorch com CUDA e demais pacotes (o modelo vem depois, ~4 GB)


class SetupCancelled(Exception):
    pass


def nvidia_gpu() -> str | None:
    """Nome e memória da placa NVIDIA, ou None se não houver (ou sem driver)."""
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        output = subprocess.run(
            [executable, "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15, check=True, **SUBPROCESS_KWARGS,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return output.splitlines()[0].strip() if output else None


def _uv_asset() -> tuple[str, str]:
    """(nome do arquivo do uv para este sistema, nome do executável dentro dele)."""
    machine = platform.machine().lower()
    if os.name == "nt":
        return "uv-x86_64-pc-windows-msvc.zip", "uv.exe"
    arch = "aarch64" if machine in ("aarch64", "arm64") else "x86_64"
    return f"uv-{arch}-unknown-linux-gnu.tar.gz", "uv"


class QwenSetup:
    """Instala ou remove o ambiente. ``log(texto)`` recebe o andamento linha a linha."""

    def __init__(self, log: Callable[[str], None], cancelled: Callable[[], bool] | None = None):
        self.log = log
        self.cancelled = cancelled or (lambda: False)
        self.root = runtime_dir()
        self._process: subprocess.Popen | None = None

    # ---------- Etapas ----------
    def install(self) -> None:
        started = time.monotonic()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "ready.json").unlink(missing_ok=True)
        uv = self._download_uv()
        env = self._uv_env()
        python = str(runtime_python())
        self.log(f"Criando um Python {PYTHON_VERSION} próprio para a Qwen...")
        self._run([uv, "venv", "--python", PYTHON_VERSION, "--seed", str(self.root / "env")], env)
        self.log("Instalando o PyTorch com CUDA (a maior parte do download)...")
        self._run([uv, "pip", "install", "--python", python, "torch", "torchaudio", "--index-url", TORCH_INDEX], env)
        self.log("Instalando a Qwen3-TTS...")
        self._run([uv, "pip", "install", "--python", python, *PACKAGES], env)
        self.log("Conferindo se a placa de vídeo está acessível...")
        check = self._run(
            [python, "-c", "import torch, faster_qwen3_tts; print(torch.__version__, torch.cuda.is_available())"],
            env, capture=True,
        )
        version, _, cuda = check.strip().rpartition("\n")[-1].partition(" ")
        if cuda.strip() != "True":
            raise RuntimeError("o PyTorch foi instalado, mas não encontrou a placa de vídeo (CUDA)")
        self._cleanup_cache()
        info = {"torch": version, "uv": UV_VERSION, "installed_at": time.strftime("%Y-%m-%d %H:%M")}
        (self.root / "ready.json").write_text(json.dumps(info), encoding="utf-8")
        self.log(f"Pronto em {(time.monotonic() - started) / 60:.0f} min (PyTorch {version}).")

    def uninstall(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.log("Ambiente da Qwen removido.")

    # ---------- uv ----------
    def _download_uv(self) -> str:
        asset, executable = _uv_asset()
        tools = self.root / "tools"
        uv = tools / executable
        if uv.is_file():
            return str(uv)
        tools.mkdir(parents=True, exist_ok=True)
        base = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{asset}"
        self.log(f"Baixando o instalador uv {UV_VERSION}...")
        expected = self._fetch(base + ".sha256").decode("ascii", "replace").split()[0].lower()
        archive = tools / asset
        data = self._fetch(base)
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError("o arquivo do uv baixado não confere com o checksum publicado")
        archive.write_bytes(data)
        if asset.endswith(".zip"):
            with zipfile.ZipFile(archive) as zipped:
                member = next(name for name in zipped.namelist() if name.endswith("/" + executable) or name == executable)
                uv.write_bytes(zipped.read(member))
        else:
            with tarfile.open(archive) as tarred:
                member = next(m for m in tarred.getmembers() if m.name.endswith("/" + executable))
                uv.write_bytes(tarred.extractfile(member).read())
            uv.chmod(0o755)
        archive.unlink()
        return str(uv)

    def _uv_env(self) -> dict:
        # Tudo dentro da pasta do ambiente: o Python baixado pelo uv e o cache.
        return dict(
            os.environ,
            UV_PYTHON_INSTALL_DIR=str(self.root / "python"),
            UV_CACHE_DIR=str(self.root / "cache"),
            UV_LINK_MODE="copy",
            UV_NO_PROGRESS="1",
        )

    def _cleanup_cache(self) -> None:
        shutil.rmtree(self.root / "cache", ignore_errors=True)  # só serviu para instalar (~3 GB)

    # ---------- Execução ----------
    def _fetch(self, url: str) -> bytes:
        self._check_cancelled()
        request = urllib.request.Request(url, headers={"User-Agent": "LocalReader"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()

    def _run(self, command: list[str], env: dict, capture: bool = False) -> str:
        self._check_cancelled()
        self._process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=env, **SUBPROCESS_KWARGS,
        )
        lines = []
        for line in self._process.stdout:
            line = line.rstrip()
            if line:
                lines.append(line)
                self.log(line)
            if self.cancelled():
                self._process.kill()
        code = self._process.wait()
        self._process = None
        self._check_cancelled()
        if code != 0:
            raise RuntimeError(f"o passo falhou (código {code}): {lines[-1] if lines else command[0]}")
        return "\n".join(lines) if capture else ""

    def _check_cancelled(self) -> None:
        if self.cancelled():
            raise SetupCancelled()

    def cancel(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()
