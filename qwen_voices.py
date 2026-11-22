"""Vozes da Qwen3-TTS rodando no computador: modelos e vozes personalizadas.

Três modelos de 1.7B parâmetros (~4 GB cada, baixados do Hugging Face no
primeiro uso), acelerados com CUDA graphs pelo faster-qwen3-tts:

- CustomVoice: 9 falantes prontos;
- VoiceDesign: cria uma voz a partir de uma descrição em texto ("voz feminina,
  sotaque paulistano, tom neutro de leitura acadêmica");
- Base: clona uma voz a partir de alguns segundos de áudio de referência.

Uma placa de 8 GB comporta um modelo por vez: ``QwenRuntime`` mantém só o que
está em uso e troca quando outro é pedido. Tudo roda numa thread única, a mesma
em que os CUDA graphs foram capturados.

As vozes personalizadas (criadas por descrição ou clonadas de um áudio) ficam em
``<dados do app>/voices/<id>/``, com o áudio de referência e o que é dito nele;
o modelo Base usa os dois para ler qualquer texto com aquela voz.
"""
import gc
import importlib.util
import json
import re
import shutil
import unicodedata
import wave
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from platform_support import app_data_dir

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
    return all(importlib.util.find_spec(package) for package in ("torch", "faster_qwen3_tts"))


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


# ---------- Modelos ----------
class QwenRuntime:
    """Um modelo da Qwen por vez na placa de vídeo, numa thread só."""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qwen-tts")
        self._kind: str | None = None
        self._model = None

    def submit(self, function, *args, **kwargs) -> Future:
        return self._executor.submit(function, *args, **kwargs)

    def model(self, kind: str):
        """Só na thread da Qwen (via ``submit``)."""
        if self._kind != kind:
            import torch
            from faster_qwen3_tts import FasterQwen3TTS

            if not torch.cuda.is_available():
                raise RuntimeError("a Qwen3-TTS precisa de uma placa de vídeo NVIDIA com CUDA")
            if self._model is not None:
                print(f"[Qwen] Liberando o modelo {self._kind} da placa de vídeo...")
                self._model, self._kind = None, None
                gc.collect()
                torch.cuda.empty_cache()
            print(f"[Qwen] Carregando {MODEL_IDS[kind]} (~4 GB na primeira vez)...")
            self._model = FasterQwen3TTS.from_pretrained(MODEL_IDS[kind], device="cuda", dtype=torch.bfloat16)
            self._kind = kind
            print("[Qwen] Modelo carregado; a primeira frase também prepara a placa de vídeo.")
        return self._model

    # Cada função abaixo roda na thread da Qwen e devolve (amostras float32, taxa).
    def speak(self, text: str, speaker: str, language: str, instruct: str | None):
        wavs, rate = self.model("custom").generate_custom_voice(
            text=text, speaker=speaker.lower(), language=language.lower(), instruct=instruct or None
        )
        return _mono(wavs[0]), int(rate)

    def design(self, text: str, description: str, language: str, seed: int | None = None):
        if seed is not None:
            import torch

            torch.manual_seed(seed)
        wavs, rate = self.model("design").generate_voice_design(
            text=text, instruct=description, language=language.lower()
        )
        return _mono(wavs[0]), int(rate)

    def clone(self, text: str, language: str, reference: str, reference_text: str):
        wavs, rate = self.model("base").generate_voice_clone(
            text=text,
            language=language.lower(),
            ref_audio=reference,
            ref_text=reference_text,
            # Sem a transcrição, só o timbre da referência é usado.
            xvec_only=not reference_text.strip(),
        )
        return _mono(wavs[0]), int(rate)


def _mono(audio) -> np.ndarray:
    return np.asarray(audio, dtype=np.float32).reshape(-1)


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
    samples = _mono(samples)[: int(sample_rate * MAX_REFERENCE_SECONDS)]
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
