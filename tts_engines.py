"""
Abstração de motores de TTS: permite trocar entre serviços diferentes
(edge-tts, gTTS, Piper) para que, se um estiver indisponível ou com
qualidade ruim, os outros continuem funcionando.
"""
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

import edge_tts

# Em 10/10/2026 a Microsoft passou a recusar (403) o host antigo usado pelo
# edge-tts <= 7.2.8. O Edge atual usa api.msedgeservices.com, que ainda aceita o
# mesmo token do edge-tts. Só aplicamos o desvio enquanto o edge-tts instalado
# apontar para o host antigo: uma versão corrigida do upstream não é alterada.
_EDGE_OLD_HOST = "speech.platform.bing.com"
_EDGE_NEW_BASE = "api.msedgeservices.com/tts/cognitiveservices"


def _patch_edge_tts_endpoint() -> None:
    from edge_tts import communicate, constants, voices

    if _EDGE_OLD_HOST not in getattr(constants, "WSS_URL", ""):
        return
    key = constants.TRUSTED_CLIENT_TOKEN
    new_urls = {
        "WSS_URL": f"wss://{_EDGE_NEW_BASE}/websocket/v1?Ocp-Apim-Subscription-Key={key}",
        "VOICE_LIST": f"https://{_EDGE_NEW_BASE}/voices/list?Ocp-Apim-Subscription-Key={key}",
    }
    for module in (constants, communicate, voices):
        for name, url in new_urls.items():
            if hasattr(module, name):
                setattr(module, name, url)


try:
    _patch_edge_tts_endpoint()
except Exception as exc:  # noqa: BLE001 — sem o desvio, o app cai no aviso de troca de motor
    print(f"[tts_engines] Não foi possível ajustar o endpoint do edge-tts: {exc}")

PIPER_VOICE_DIR = Path.home() / ".cache" / "tts-reader" / "piper-voices"

# Catálogo curado de vozes Piper: a melhor qualidade disponível para cada
# idioma/local suportado (nomes reais confirmados em
# https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json).
# "high" só existe para 9 idiomas hoje; os demais usam "medium" (melhor
# opção existente nesses casos).
PIPER_CURATED_VOICES = [
    ("ar_JO-kareem-medium", "ar-JO", "Arabic (Jordan)"),
    ("bg_BG-dimitar-medium", "bg-BG", "Bulgarian (Bulgaria)"),
    ("bn_BD-google-medium", "bn-BD", "Bengali (Bangladesh)"),
    ("ca_ES-upc_ona-medium", "ca-ES", "Catalan (Spain)"),
    ("cs_CZ-jirka-medium", "cs-CZ", "Czech (Czech Republic)"),
    ("cy_GB-bu_tts-medium", "cy-GB", "Welsh (Great Britain)"),
    ("da_DK-talesyntese-medium", "da-DK", "Danish (Denmark)"),
    ("de_DE-thorsten-high", "de-DE", "German (Germany)"),
    ("el_GR-joy-medium", "el-GR", "Greek (Greece)"),
    ("en_GB-cori-high", "en-GB", "English (Great Britain)"),
    ("en_US-lessac-high", "en-US", "English (United States)"),
    ("es_AR-daniela-high", "es-AR", "Spanish (Argentina)"),
    ("es_ES-davefx-medium", "es-ES", "Spanish (Spain)"),
    ("es_MX-claude-high", "es-MX", "Spanish (Mexico)"),
    ("eu_ES-antton-medium", "eu-ES", "Basque (Spain)"),
    ("fa_IR-amir-medium", "fa-IR", "Farsi (Iran)"),
    ("fi_FI-harri-medium", "fi-FI", "Finnish (Finland)"),
    ("fr_FR-siwis-medium", "fr-FR", "French (France)"),
    ("he_IL-saspeech-medium", "he-IL", "Hebrew (Israel)"),
    ("hi_IN-pratham-medium", "hi-IN", "Hindi (India)"),
    ("hu_HU-anna-medium", "hu-HU", "Hungarian (Hungary)"),
    ("hy_AM-gor-medium", "hy-AM", "Armenian (Armenia)"),
    ("id_ID-news_tts-medium", "id-ID", "Indonesian (Indonesia)"),
    ("is_IS-bui-medium", "is-IS", "Icelandic (Iceland)"),
    ("it_IT-serena-high", "it-IT", "Italian (Italy)"),
    ("ja_JA-hi_fi_captain-medium", "ja-JA", "Japanese (Japan)"),
    ("ka_GE-natia-medium", "ka-GE", "Georgian (Georgia)"),
    ("kk_KZ-issai-high", "kk-KZ", "Kazakh (Kazakhstan)"),
    ("ko_KR-kss-medium", "ko-KR", "Korean (South Korea)"),
    ("ku_TR-berfin_renas-medium", "ku-TR", "Kurmanji Kurdish (Turkey)"),
    ("lb_LU-marylux-medium", "lb-LU", "Luxembourgish (Luxembourg)"),
    ("lv_LV-aivars-medium", "lv-LV", "Latvian (Latvia)"),
    ("ml_IN-arjun-medium", "ml-IN", "Malayalam (India)"),
    ("mr_IN-google-medium", "mr-IN", "Marathi (India)"),
    ("ne_NP-chitwan-medium", "ne-NP", "Nepali (Nepal)"),
    ("nl_BE-nathalie-medium", "nl-BE", "Dutch (Belgium)"),
    ("nl_NL-mls-medium", "nl-NL", "Dutch (Netherlands)"),
    ("no_NO-talesyntese-medium", "no-NO", "Norwegian (Norway)"),
    ("pl_PL-bass-high", "pl-PL", "Polish (Poland)"),
    ("pt_BR-faber-medium", "pt-BR", "Portuguese (Brazil)"),
    ("pt_PT-tugão-medium", "pt-PT", "Portuguese (Portugal)"),
    ("ro_RO-mihai-medium", "ro-RO", "Romanian (Romania)"),
    ("ru_RU-irina-medium", "ru-RU", "Russian (Russia)"),
    ("sk_SK-lili-medium", "sk-SK", "Slovak (Slovakia)"),
    ("sl_SI-artur-medium", "sl-SI", "Slovenian (Slovenia)"),
    ("sq_AL-edon-medium", "sq-AL", "Albanian (Albania)"),
    ("sr_RS-serbski_institut-medium", "sr-RS", "Serbian (Serbia)"),
    ("sv_SE-nst-medium", "sv-SE", "Swedish (Sweden)"),
    ("sw_CD-lanfrica-medium", "sw-CD", "Swahili (Democratic Republic of the Congo)"),
    ("te_IN-maya-medium", "te-IN", "Telugu (India)"),
    ("tr_TR-dfki-medium", "tr-TR", "Turkish (Turkey)"),
    ("uk_UA-mykyta-high", "uk-UA", "Ukrainian (Ukraine)"),
    ("ur_PK-fasih-medium", "ur-PK", "Urdu (Pakistan)"),
    ("vi_VN-vais1000-medium", "vi-VN", "Vietnamese (Vietnam)"),
    ("zh_CN-huayan-medium", "zh-CN", "Chinese (China)"),
]


@dataclass
class VoiceInfo:
    id: str
    gender: str
    locale: str
    locale_name: str


class TTSEngine:
    id = ""
    display_name = ""
    description = ""
    requires_internet = True
    supports_pitch = True
    output_extension = ".mp3"

    async def list_voices(self) -> list[VoiceInfo]:
        raise NotImplementedError

    async def synthesize(
        self, text: str, voice_id: str, rate_percent: int, pitch_hz: int, output_path: str
    ) -> None:
        raise NotImplementedError


class EdgeEngine(TTSEngine):
    id = "edge"
    display_name = "Microsoft Edge (edge-tts)"
    description = (
        "Vozes neurais da Microsoft — as mesmas do recurso \"Ler em Voz Alta\" "
        "do navegador Edge. Melhor qualidade e mais natural entre os três, mas "
        "usa um serviço não-oficial que pode falhar esporadicamente. Requer "
        "internet."
    )
    requires_internet = True
    supports_pitch = True
    output_extension = ".mp3"

    async def list_voices(self) -> list[VoiceInfo]:
        raw = await edge_tts.list_voices()
        return [
            VoiceInfo(
                id=v["ShortName"],
                gender=v["Gender"],
                locale=v["Locale"],
                locale_name=v.get("LocaleName", v["Locale"]),
            )
            for v in raw
        ]

    async def synthesize(self, text, voice_id, rate_percent, pitch_hz, output_path):
        rate = f"{rate_percent:+d}%"
        pitch = f"{pitch_hz:+d}Hz"
        communicate = edge_tts.Communicate(text, voice=voice_id, rate=rate, pitch=pitch)
        await communicate.save(output_path)


class GTTSEngine(TTSEngine):
    id = "gtts"
    display_name = "Google Translate (gTTS)"
    description = (
        "Motor alternativo baseado no Google Tradutor. Voz mais robótica que o "
        "edge-tts, mas serve como alternativa simples quando ele estiver fora "
        "do ar. Só permite velocidade normal ou lenta, sem controle de tom. "
        "Requer internet."
    )
    requires_internet = True
    supports_pitch = False
    output_extension = ".mp3"

    async def list_voices(self) -> list[VoiceInfo]:
        from gtts.lang import tts_langs

        langs = tts_langs()
        return [
            VoiceInfo(id=code, gender="N/A", locale=code, locale_name=f"{name} (Genérico)")
            for code, name in sorted(langs.items(), key=lambda kv: kv[1])
        ]

    async def synthesize(self, text, voice_id, rate_percent, pitch_hz, output_path):
        from gtts import gTTS

        slow = rate_percent < -20

        def _run():
            gTTS(text=text, lang=voice_id, slow=slow).save(output_path)

        await asyncio.to_thread(_run)


class PiperEngine(TTSEngine):
    id = "piper"
    display_name = "Piper (offline)"
    description = (
        "Motor neural que roda 100% no seu computador, sem depender de nenhum "
        "serviço online (após baixar a voz escolhida uma única vez, ~50-100MB). "
        "Qualidade boa e sempre disponível mesmo sem internet, mas o catálogo "
        "de vozes é menor e não há controle de tom."
    )
    requires_internet = False
    supports_pitch = False
    output_extension = ".wav"

    def __init__(self):
        self._loaded_voices: dict[str, object] = {}

    async def list_voices(self) -> list[VoiceInfo]:
        return [
            VoiceInfo(id=voice_id, gender="N/A", locale=locale, locale_name=locale_name)
            for voice_id, locale, locale_name in PIPER_CURATED_VOICES
        ]

    def _ensure_model_files(self, voice_id: str) -> tuple[Path, Path]:
        PIPER_VOICE_DIR.mkdir(parents=True, exist_ok=True)
        model_path = PIPER_VOICE_DIR / f"{voice_id}.onnx"
        config_path = PIPER_VOICE_DIR / f"{voice_id}.onnx.json"
        if not model_path.exists() or model_path.stat().st_size == 0:
            from piper.download_voices import download_voice

            print(f"[Piper] Baixando voz '{voice_id}' pela primeira vez (isso só acontece uma vez)...")
            download_voice(voice_id, PIPER_VOICE_DIR)
            print(f"[Piper] Voz '{voice_id}' baixada e salva em {PIPER_VOICE_DIR}")
        return model_path, config_path

    def _get_voice(self, voice_id: str):
        if voice_id not in self._loaded_voices:
            from piper import PiperVoice

            model_path, config_path = self._ensure_model_files(voice_id)
            self._loaded_voices[voice_id] = PiperVoice.load(str(model_path), str(config_path))
        return self._loaded_voices[voice_id]

    async def synthesize(self, text, voice_id, rate_percent, pitch_hz, output_path):
        import wave

        from piper.config import SynthesisConfig

        def _run():
            voice = self._get_voice(voice_id)
            length_scale = 1.0 / (1.0 + rate_percent / 100.0)
            syn_config = SynthesisConfig(length_scale=length_scale)
            wav_tmp = output_path + ".tmp.wav"
            with wave.open(wav_tmp, "wb") as wav_file:
                voice.synthesize_wav(text, wav_file, syn_config=syn_config)
            os.replace(wav_tmp, output_path)

        await asyncio.to_thread(_run)


ENGINES: dict[str, TTSEngine] = {}


def _register(engine_cls) -> None:
    try:
        instance = engine_cls()
        ENGINES[instance.id] = instance
    except Exception as exc:  # noqa: BLE001
        print(f"[tts_engines] Motor '{engine_cls.__name__}' indisponível: {exc}")


_register(EdgeEngine)
_register(PiperEngine)
_register(GTTSEngine)

ENGINE_ORDER = [eid for eid in ("edge", "piper", "gtts") if eid in ENGINES]
