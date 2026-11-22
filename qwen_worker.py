"""Processo da Qwen3-TTS: lê pedidos em JSON (um por linha) e grava o áudio em WAV.

Roda com o Python do ambiente da Qwen (instalado pelo app em
``<dados do app>/qwen-runtime``, ou o do código-fonte) e não importa nada do
app: assim o PyTorch e as bibliotecas da Qwen nunca entram em conflito com as
do LocalReader. Só um modelo fica na placa de vídeo por vez.

Pedido:   {"id": 1, "op": "speak"|"design"|"clone"|"ping", "out": "saida.wav", ...}
Resposta: {"id": 1, "ok": true, "sample_rate": 24000} ou {"id": 1, "ok": false, "error": "..."}
Mensagens das bibliotecas vão para o stderr; o stdout é só do protocolo.
"""
import gc
import json
import sys
import traceback
import wave

MODEL_IDS = {
    "custom": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "base": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
}
_state = {"kind": None, "model": None}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def model(kind: str):
    if _state["kind"] != kind:
        import torch
        from faster_qwen3_tts import FasterQwen3TTS

        if not torch.cuda.is_available():
            raise RuntimeError("a Qwen3-TTS precisa de uma placa de vídeo NVIDIA com CUDA")
        if _state["model"] is not None:
            log(f"[Qwen] Liberando o modelo {_state['kind']} da placa de vídeo...")
            _state["model"], _state["kind"] = None, None
            gc.collect()
            torch.cuda.empty_cache()
        log(f"[Qwen] Carregando {MODEL_IDS[kind]} (~4 GB na primeira vez)...")
        _state["model"] = FasterQwen3TTS.from_pretrained(MODEL_IDS[kind], device="cuda", dtype=torch.bfloat16)
        _state["kind"] = kind
        log("[Qwen] Modelo carregado; a primeira frase também prepara a placa de vídeo.")
    return _state["model"]


def write_wav(path: str, audio, sample_rate: int) -> None:
    import numpy as np

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 1.0:
        samples = samples / peak
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes((np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes())


def handle(request: dict) -> dict:
    op = request["op"]
    if op == "ping":
        import torch

        return {"cuda": torch.cuda.is_available(), "torch": torch.__version__}
    language = request.get("language", "portuguese").lower()
    if op == "speak":
        wavs, rate = model("custom").generate_custom_voice(
            text=request["text"], speaker=request["speaker"].lower(), language=language,
            instruct=request.get("instruct") or None,
        )
    elif op == "design":
        if request.get("seed") is not None:
            import torch

            torch.manual_seed(int(request["seed"]))
        wavs, rate = model("design").generate_voice_design(
            text=request["text"], instruct=request["description"], language=language
        )
    elif op == "clone":
        reference_text = request.get("reference_text") or ""
        wavs, rate = model("base").generate_voice_clone(
            text=request["text"], language=language, ref_audio=request["reference"], ref_text=reference_text,
            xvec_only=not reference_text.strip(),  # sem a transcrição, só o timbre da referência
        )
    else:
        raise ValueError(f"operação desconhecida: {op}")
    write_wav(request["out"], wavs[0], int(rate))
    return {"sample_rate": int(rate)}


def main() -> None:
    # Protocolo em UTF-8 e JSON só com ASCII, qualquer que seja a codificação do sistema.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    protocol = sys.stdout
    sys.stdout = sys.stderr  # prints das bibliotecas não podem sujar o protocolo
    protocol.write(json.dumps({"ready": True}) + "\n")
    protocol.flush()
    for line in sys.stdin:
        if not line.strip():
            continue
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            response = {"id": request_id, "ok": True, **handle(request)}
        except Exception as exc:  # noqa: BLE001 - o erro vai para o app, o processo continua
            log(traceback.format_exc())
            response = {"id": request_id, "ok": False, "error": f"{exc}"}
        protocol.write(json.dumps(response) + "\n")
        protocol.flush()


if __name__ == "__main__":
    main()
