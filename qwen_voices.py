"""Vozes da Qwen3-TTS rodando no computador: modelos e vozes personalizadas.

Três modelos de 1.7B parâmetros (~4 GB cada, baixados do Hugging Face no
primeiro uso), acelerados com CUDA graphs pelo faster-qwen3-tts:

- CustomVoice: 9 falantes prontos;
- VoiceDesign: cria uma voz a partir de uma descrição em texto ("voz feminina,
  sotaque paulistano, tom neutro de leitura acadêmica");
- Base: clona uma voz a partir de alguns segundos de áudio de referência.

Os modelos rodam num processo à parte (``qwen_worker.py``), com o Python do
ambiente da Qwen: instalado pelo app em ``<dados do app>/qwen-runtime`` (veja
``qwen_setup``) ou, rodando pelo código-fonte, o próprio. Uma placa de 8 GB
comporta um modelo por vez: o processo mantém só o que está em uso.

As vozes personalizadas (criadas por descrição ou clonadas de um áudio) ficam em
``<dados do app>/voices/<id>/``, com o áudio de referência e o que é dito nele;
o modelo Base usa os dois para ler qualquer texto com aquela voz.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unicodedata
import wave
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from platform_support import SUBPROCESS_KWARGS, app_data_dir

MODEL_IDS = {
    "custom": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "base": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
}
# Idioma do modelo -> (locale, nome no catálogo do app).
LANGUAGES = {
    "Portuguese": ("pt-BR", "Portuguese (Brazil)"),
    "English": ("en-US", "English (United States)"),
    "Spanish": ("es-ES", "Spanish (Spain)"),
    "French": ("fr-FR", "French (France)"),
    "German": ("de-DE", "German (Germany)"),
    "Italian": ("it-IT", "Italian (Italy)"),
    "Russian": ("ru-RU", "Russian (Russia)"),
    "Japanese": ("ja-JP", "Japanese (Japan)"),
    "Korean": ("ko-KR", "Korean (South Korea)"),
    "Chinese": ("zh-CN", "Chinese (China)"),
}
# Falantes prontos do modelo CustomVoice (nativos de outras línguas, por isso
# com sotaque em português; para sotaque brasileiro, use uma voz personalizada).
SPEAKERS = [
    ("Vivian", "Female", "jovem, brilhante"),
    ("Serena", "Female", "jovem, calorosa e suave"),
    ("Ono_Anna", "Female", "leve e brincalhona"),
    ("Sohee", "Female", "calorosa e expressiva"),
    ("Ryan", "Male", "dinâmica e ritmada"),
    ("Aiden", "Male", "clara e ensolarada"),
    ("Dylan", "Male", "jovem e natural"),
    ("Eric", "Male", "animada, levemente rouca"),
    ("Uncle_Fu", "Male", "madura, grave e suave"),
]
# Leitura de textos acadêmicos: sem a dramaticidade padrão dos falantes prontos.
NEUTRAL_INSTRUCTION = (
    "Read this text aloud in a calm, neutral and steady voice, with even pacing and no "
    "dramatic emphasis, like a professional narrator reading an academic paper."
)
DESIGN_EXAMPLE = (
    "Mulher brasileira de uns 35 anos, de São Paulo, falando português com sotaque "
    "paulistano natural. Voz calma, clara, neutra e profissional, como uma professora "
    "universitária lendo um texto acadêmico em voz alta. Ritmo constante, sem ênfase dramática."
)
SAMPLE_TEXTS = {
    "Portuguese": "Bom dia. Neste capítulo, apresento os principais argumentos do trabalho e explico, "
                  "de forma clara e objetiva, como os dados foram analisados.",
    "English": "Good morning. In this chapter, I present the main arguments of the study and explain, "
               "clearly and objectively, how the data were analyzed.",
}
MAX_REFERENCE_SECONDS = 20  # mais que isso não melhora a imitação e deixa tudo mais lento
CUSTOM_PREFIX = "custom:"


def qwen_available() -> bool:
    return worker_python() is not None


def rate_instruction(rate_percent: int) -> str:
    """O modelo não tem parâmetro de velocidade: ela vai como instrução."""
    if rate_percent >= 40:
        return "Speak very fast."
    if rate_percent >= 15:
        return "Speak a little faster than normal."
    if rate_percent <= -40:
        return "Speak very slowly."
    if rate_percent <= -15:
        return "Speak a little slower than normal."
    return ""


# ---------- Processo da Qwen ----------
def runtime_dir() -> Path:
    """Ambiente da Qwen instalado pelo app (Python, PyTorch e faster-qwen3-tts)."""
    return Path(app_data_dir("qwen-runtime"))


def runtime_python() -> Path:
    env = runtime_dir() / "env"
    return env / "Scripts" / "python.exe" if os.name == "nt" else env / "bin" / "python"


def runtime_installed() -> bool:
    return runtime_python().is_file() and (runtime_dir() / "ready.json").is_file()


def worker_python() -> str | None:
    """O Python que roda a Qwen: o do ambiente instalado pelo app ou, rodando
    pelo código-fonte com os pacotes instalados, o próprio."""
    if runtime_installed():
        return str(runtime_python())
    if not getattr(sys, "frozen", False) and all(
        importlib.util.find_spec(package) for package in ("torch", "faster_qwen3_tts")
    ):
        return sys.executable
    return None


def worker_script() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "qwen_worker.py"


class QwenRuntime:
    """Fala com o processo da Qwen (qwen_worker.py), um pedido por vez.

    O processo é iniciado no primeiro pedido e reiniciado se cair; os modelos
    ficam carregados nele entre um pedido e outro.
    """

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qwen-tts")
        self._process: subprocess.Popen | None = None
        self._next_id = 0

    def submit(self, function, *args, **kwargs) -> Future:
        return self._executor.submit(function, *args, **kwargs)

    def _ensure_process(self) -> subprocess.Popen:
        if self._process is not None and self._process.poll() is None:
            return self._process
        python = worker_python()
        if python is None:
            raise RuntimeError("a Qwen3-TTS não está instalada (Áudio → Instalar vozes Qwen)")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        env.pop("PYTHONPATH", None)
        self._process = subprocess.Popen(
            [python, "-u", str(worker_script())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=env, **SUBPROCESS_KWARGS,
        )
        threading.Thread(target=self._forward_log, args=(self._process,), daemon=True).start()
        first = self._process.stdout.readline()
        if not first.strip().startswith("{"):
            self._process.kill()
            self._process = None
            raise RuntimeError("o processo da Qwen3-TTS não iniciou")
        return self._process

    @staticmethod
    def _forward_log(process: subprocess.Popen) -> None:
        for line in process.stderr:
            line = line.rstrip()
            if line:
                print(line)

    def call(self, op: str, **params) -> dict:
        """Só na thread da Qwen (via ``submit``)."""
        process = self._ensure_process()
        self._next_id += 1
        request = {"id": self._next_id, "op": op, **params}
        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
        except OSError as exc:
            raise RuntimeError(f"o processo da Qwen3-TTS parou ({exc})") from None
        if not line:
            self._process = None
            raise RuntimeError("o processo da Qwen3-TTS parou; veja o registro de logs")
        response = json.loads(line)
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or "falha desconhecida na Qwen3-TTS")
        return response

    def _generate(self, op: str, **params) -> tuple[np.ndarray, int]:
        handle, path = tempfile.mkstemp(prefix="localreader_qwen_", suffix=".wav")
        os.close(handle)
        try:
            self.call(op, out=path, **params)
            return read_audio(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    # Cada função abaixo roda na thread da Qwen e devolve (amostras float32, taxa).
    def speak(self, text: str, speaker: str, language: str, instruct: str | None):
        return self._generate("speak", text=text, speaker=speaker, language=language, instruct=instruct or "")

    def design(self, text: str, description: str, language: str, seed: int | None = None):
        return self._generate("design", text=text, description=description, language=language, seed=seed)

    def clone(self, text: str, language: str, reference: str, reference_text: str):
        return self._generate("clone", text=text, language=language, reference=reference,
                              reference_text=reference_text)

    def shutdown(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            try:
                process.stdin.close()
                process.wait(timeout=3)
            except Exception:  # noqa: BLE001
                process.kill()


RUNTIME = QwenRuntime()


# ---------- Vozes personalizadas ----------
@dataclass
class CustomVoice:
    id: str
    name: str
    gender: str  # "Female", "Male" ou "N/A"
    reference_text: str  # o que é dito no áudio de referência
    description: str = ""  # a descrição usada para criá-la (se foi criada assim)
    source: str = "audio"  # "design" ou "audio"

    @property
    def folder(self) -> Path:
        return voices_dir() / self.id

    @property
    def reference(self) -> Path:
        return self.folder / "reference.wav"


def voices_dir() -> Path:
    return Path(app_data_dir("voices"))


def list_custom_voices() -> list[CustomVoice]:
    voices = []
    root = voices_dir()
    if not root.is_dir():
        return voices
    for folder in sorted(root.iterdir()):
        try:
            data = json.loads((folder / "voice.json").read_text(encoding="utf-8"))
            voice = CustomVoice(**{key: data[key] for key in CustomVoice.__dataclass_fields__ if key in data})
        except (OSError, ValueError, TypeError):
            continue
        if voice.id == folder.name and voice.reference.is_file():
            voices.append(voice)
    return sorted(voices, key=lambda v: v.name.casefold())


def get_custom_voice(voice_id: str) -> CustomVoice | None:
    return next((voice for voice in list_custom_voices() if voice.id == voice_id), None)


def _slug(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub("[^a-z0-9]+", "-", text).strip("-") or "voz"


def save_custom_voice(
    name: str,
    samples: np.ndarray,
    sample_rate: int,
    reference_text: str,
    gender: str = "N/A",
    description: str = "",
    source: str = "audio",
) -> CustomVoice:
    """Guarda uma voz: o áudio de referência (até 20 s, mono) e o que é dito nele."""
    name = " ".join(name.split())
    if not name:
        raise ValueError("Dê um nome à voz.")
    if any(voice.name.casefold() == name.casefold() for voice in list_custom_voices()):
        raise ValueError(f"Já existe uma voz chamada “{name}”.")
    base = _slug(name)
    voice_id, counter = base, 2
    while (voices_dir() / voice_id).exists():
        voice_id, counter = f"{base}-{counter}", counter + 1
    voice = CustomVoice(voice_id, name, gender, reference_text.strip(), description.strip(), source)
    voice.folder.mkdir(parents=True)
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)[: int(sample_rate * MAX_REFERENCE_SECONDS)]
    write_wav(str(voice.reference), samples, sample_rate)
    (voice.folder / "voice.json").write_text(json.dumps(asdict(voice), ensure_ascii=False, indent=1), encoding="utf-8")
    return voice


def delete_custom_voice(voice_id: str) -> None:
    folder = voices_dir() / voice_id
    if folder.is_dir() and folder.parent == voices_dir():
        shutil.rmtree(folder)


def read_audio(path: str) -> tuple[np.ndarray, int]:
    """Lê um áudio de referência (WAV, FLAC, OGG, MP3...) como mono float32."""
    if path.lower().endswith(".wav"):
        try:
            with wave.open(path, "rb") as wav:
                frames = wav.readframes(wav.getnframes())
                width, channels, rate = wav.getsampwidth(), wav.getnchannels(), wav.getframerate()
            if width == 2:
                samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768
                return samples.reshape(-1, channels).mean(axis=1), rate
        except wave.Error:
            pass  # WAV em formato que o módulo wave não lê (float, 24 bits): tenta o soundfile
    import soundfile

    samples, rate = soundfile.read(path, dtype="float32", always_2d=True)
    return samples.mean(axis=1), int(rate)


def write_wav(path: str, samples: np.ndarray, sample_rate: int) -> None:
    from nos_tts import write_wav as _write

    _write(path, samples, sample_rate)
