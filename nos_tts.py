"""Vozes galegas do Proxecto Nós (Celtia, Sabela e Iago), rodando no computador.

Os modelos são VITS treinados pelo Proxecto Nós (Universidade de Santiago de
Compostela) com o Coqui TTS e convertidos para ONNX pelo OpenVoiceOS; rodam com
o ONNX Runtime (o mesmo do Piper), sem PyTorch e iguais no Windows e no Linux.
Cada voz (~130 MB) é baixada do Hugging Face no primeiro uso.

- Celtia lê o texto direto (grafemas).
- Sabela e Iago leem a transcrição fonética do Cotovía, gerada aqui pelo
  pycotovia (versão em Python puro do Cotovía, que só existe para Linux).

Antes, números viram palavras em galego, e a pontuação vira pausas. Licenças:
modelos Apache-2.0 (Proxecto Nós), vozes CC BY 4.0, pycotovia Apache-2.0.
"""
import json
import os
import re
import threading
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from platform_support import cache_dir

NOS_VOICE_DIR = cache_dir("nos-voices")
SAMPLE_RATE = 16000
NOISE_SCALE = 0.667
NOISE_SCALE_DURATION = 0.8
MAX_CHUNK_CHARS = 300  # períodos maiores são lidos em partes (vírgulas, pontos)
PAUSE_SECONDS = 0.12  # entre partes de um período longo


@dataclass(frozen=True)
class NosVoice:
    name: str
    gender: str
    folder: str  # nome da pasta local e sufixo do repositório
    phonemes: bool  # True: entrada é a transcrição do Cotovía
    vocab_order: str  # "vits" ou "graphemes" (ordem da tabela de símbolos do Coqui)

    @property
    def repo(self) -> str:
        return f"OpenVoiceOS/proxectonos-{self.folder}-onnx"


NOS_VOICES = {
    "gl-ES-Celtia": NosVoice("Celtia", "Female", "celtia-vits-graphemes", phonemes=False, vocab_order="vits"),
    "gl-ES-Sabela": NosVoice("Sabela", "Female", "sabela-vits-phonemes", phonemes=True, vocab_order="vits"),
    "gl-ES-Iago": NosVoice("Iago", "Male", "iago-vits-phonemes", phonemes=True, vocab_order="graphemes"),
}

# Pontuação que separa partes da fala (vira pausa) e a que o modelo conhece.
_BREAKS = set(".,;:!?¡¿()\"“”«»—–…")
_SENTENCE_END = set(".!?…")


# ---------- Números por extenso (galego) ----------
_UNITS = [
    "cero", "un", "dous", "tres", "catro", "cinco", "seis", "sete", "oito", "nove", "dez",
    "once", "doce", "trece", "catorce", "quince", "dezaseis", "dezasete", "dezaoito", "dezanove",
]
_TENS = {2: "vinte", 3: "trinta", 4: "corenta", 5: "cincuenta", 6: "sesenta", 7: "setenta", 8: "oitenta", 9: "noventa"}
_HUNDREDS = {
    1: "cento", 2: "douscentos", 3: "trescentos", 4: "catrocentos", 5: "cincocentos",
    6: "seiscentos", 7: "setecentos", 8: "oitocentos", 9: "novecentos",
}


def galician_number(n: int) -> str:
    if n < 0:
        return "menos " + galician_number(-n)
    if n < 20:
        return _UNITS[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        return _TENS[tens] + (f" e {_UNITS[unit]}" if unit else "")
    if n == 100:
        return "cen"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return _HUNDREDS[hundreds] + (f" {galician_number(rest)}" if rest else "")
    if n < 1_000_000:
        thousands, rest = divmod(n, 1000)
        head = "mil" if thousands == 1 else f"{galician_number(thousands)} mil"
        return head + (f" {galician_number(rest)}" if rest else "")
    if n < 1_000_000_000_000:
        millions, rest = divmod(n, 1_000_000)
        head = "un millón" if millions == 1 else f"{galician_number(millions)} millóns"
        return head + (f" {galician_number(rest)}" if rest else "")
    return " ".join(_UNITS[int(digit)] for digit in str(n))


_NUMBER_RE = re.compile("[0-9]{1,3}(?:[.][0-9]{3})+(?:,[0-9]+)?|[0-9]+(?:,[0-9]+)?")


def _number_words(match: re.Match) -> str:
    text = match.group().replace(".", "")
    whole, _, decimals = text.partition(",")
    words = galician_number(int(whole))
    if decimals:
        words += " coma " + galician_number(int(decimals))
    return f" {words} "


def normalize_text(text: str) -> str:
    """Números, porcentagens e símbolos por extenso; espaços simples."""
    text = text.replace("%", " por cento ").replace("&", " e ").replace("º", "").replace("ª", "")
    text = _NUMBER_RE.sub(_number_words, text)
    return " ".join(text.split())


# ---------- Texto -> símbolos do modelo ----------
def split_chunks(text: str) -> list[tuple[str, str]]:
    """(trecho, pontuação depois dele): a pontuação é tratada à parte do G2P."""
    chunks: list[tuple[str, str]] = []
    current: list[str] = []
    for char in text:
        if char in _BREAKS:
            words = "".join(current).strip()
            if words or chunks:
                if not words and chunks:
                    previous, punctuation = chunks[-1]
                    chunks[-1] = (previous, punctuation + char)
                else:
                    chunks.append((words, char))
            current = []
        else:
            current.append(char)
    words = "".join(current).strip()
    if words:
        chunks.append((words, ""))
    return chunks


def _model_punctuation(marks: str) -> str:
    """Reduz a pontuação ao que os modelos conhecem: fim de frase ou vírgula."""
    if not marks:
        return ""
    for mark in ("?", "!"):
        if mark in marks:
            return mark
    return "." if any(mark in _SENTENCE_END for mark in marks) else ","


_ACCENTED = {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú", "A": "Á", "E": "É", "I": "Í", "O": "Ó", "U": "Ú"}
_PLAIN = {accented: vowel for vowel, accented in _ACCENTED.items()}
# Palavras átonas (artigos, preposições, conjunções, pronomes oblíquos), já
# transcritas: o pycotovia marca tônica em todo monossílabo, mas no treino das
# vozes elas vêm sem acento ("De kátro", "e kaBaléjros", "o BÉZo").
_UNSTRESSED = {
    "o", "a", "os", "as", "e", "De", "Do", "Da", "Dos", "Das", "de", "do", "da", "dos", "das",
    "eN", "en", "no", "na", "nos", "nas",
    "ao", "aos", "kon", "koN", "ko", "koa", "kos", "koas", "por", "polo", "pola", "polos", "polas",
    "ke", "se", "me", "te", "Ze", "Zes", "lle", "lles", "ow", "niN", "un", "uN", "ka",
}


def cotovia_phonemes(words: str) -> str:
    """Transcrição do Cotovía com a vogal tônica acentuada, como no treino ("kómo BáN")."""
    from pycotovia import phonemize

    phones = phonemize(words, "gl", 2)
    for vowel, accented in _ACCENTED.items():
        phones = phones.replace(vowel + "^", accented)
    result = []
    for word in phones.replace("^", "").split():
        plain = "".join(_PLAIN.get(char, char) for char in word)
        result.append(plain if plain in _UNSTRESSED else word)
    return " ".join(result)


def text_to_symbols(text: str, voice: NosVoice) -> str:
    """O texto como a sequência de símbolos que o modelo da voz espera."""
    parts = []
    for words, marks in split_chunks(normalize_text(text)):
        if voice.phonemes:
            spoken = cotovia_phonemes(words) if words else ""
        else:
            spoken = words.lower()
        punctuation = _model_punctuation(marks)
        if spoken:
            parts.append(spoken + punctuation)
        elif punctuation and parts:
            parts[-1] += punctuation
    return " ".join(parts)


class SymbolTable:
    """Tabela de símbolos do Coqui TTS, na mesma ordem usada no treino."""

    def __init__(self, characters_config: dict, order: str):
        chars = characters_config
        characters = list(chars["characters"])
        if chars.get("is_unique", True):
            characters = list(dict.fromkeys(characters))
        punctuations = list(chars["punctuations"])
        pad, eos, bos, blank = chars.get("pad"), chars.get("eos"), chars.get("bos"), chars.get("blank")
        if order == "vits":  # VitsCharacters: [pad] + pontuação + caracteres + [blank]
            vocab = [pad] + punctuations + characters + [blank or "<BLNK>"]
        else:  # Graphemes: [pad, eos, bos, blank] + caracteres em ordem + pontuação
            if chars.get("is_sorted", True):
                characters = sorted(characters)
            vocab = [s for s in (pad, eos, bos, blank) if s] + characters + punctuations
        self.ids = {symbol: index for index, symbol in enumerate(vocab)}
        self.blank_id = self.ids[blank or "<BLNK>"]

    def encode(self, symbols: str) -> list[int]:
        ids = [self.ids[char] for char in symbols if char in self.ids]
        # add_blank: o símbolo vazio entre todos os outros (e nas pontas), como no Coqui.
        result = [self.blank_id] * (len(ids) * 2 + 1)
        result[1::2] = ids
        return result


# ---------- Modelos ----------
class NosSynthesizer:
    def __init__(self, voice_dir: Path = NOS_VOICE_DIR):
        self.voice_dir = voice_dir
        self._loaded: dict[str, tuple[object, SymbolTable]] = {}
        self._lock = threading.Lock()

    def _download(self, voice: NosVoice) -> Path:
        folder = self.voice_dir / voice.folder
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("config.json", "model.onnx"):
            target = folder / name
            if target.exists() and target.stat().st_size > 0:
                continue
            print(f"[Nós] Baixando {voice.name}: {name} (só na primeira vez)...")
            url = f"https://huggingface.co/{voice.repo}/resolve/main/{name}"
            tmp = target.with_suffix(target.suffix + ".part")
            with urllib.request.urlopen(url, timeout=60) as response, open(tmp, "wb") as fh:
                while chunk := response.read(1 << 20):
                    fh.write(chunk)
            os.replace(tmp, target)
        return folder

    def _load(self, voice_id: str):
        with self._lock:
            if voice_id not in self._loaded:
                import onnxruntime

                voice = NOS_VOICES[voice_id]
                folder = self._download(voice)
                # O JSON do Coqui pode trazer Infinity, que o json do Python aceita.
                config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
                session = onnxruntime.InferenceSession(str(folder / "model.onnx"), providers=["CPUExecutionProvider"])
                self._loaded[voice_id] = (session, SymbolTable(config["characters"], voice.vocab_order))
            return self._loaded[voice_id]

    def synthesize(self, text: str, voice_id: str, rate_percent: int, output_path: str) -> None:
        voice = NOS_VOICES[voice_id]
        session, table = self._load(voice_id)
        length_scale = 1.0 / (1.0 + rate_percent / 100.0)
        scales = np.array([NOISE_SCALE, length_scale, NOISE_SCALE_DURATION], dtype=np.float32)
        pieces = []
        for part in _split_long(text_to_symbols(text, voice)):
            ids = table.encode(part)
            if len(ids) < 3:
                continue
            audio = session.run(
                None,
                {
                    "input": np.array([ids], dtype=np.int64),
                    "input_lengths": np.array([len(ids)], dtype=np.int64),
                    "scales": scales,
                },
            )[0]
            pieces.append(np.asarray(audio, dtype=np.float32).reshape(-1))
            pieces.append(np.zeros(int(SAMPLE_RATE * PAUSE_SECONDS), dtype=np.float32))
        samples = np.concatenate(pieces[:-1]) if pieces else np.zeros(SAMPLE_RATE // 10, dtype=np.float32)
        write_wav(output_path, samples, SAMPLE_RATE)


def _split_long(symbols: str) -> list[str]:
    """Divide sequências longas nas pausas, para o modelo não perder a entonação."""
    if len(symbols) <= MAX_CHUNK_CHARS:
        return [symbols] if symbols.strip() else []
    parts, current = [], ""
    for piece in re.split("(?<=[.,?!]) ", symbols):
        if current and len(current) + len(piece) > MAX_CHUNK_CHARS:
            parts.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        parts.append(current)
    return parts


def write_wav(path: str, samples: np.ndarray, sample_rate: int) -> None:
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 1.0:
        samples = samples / peak
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())
