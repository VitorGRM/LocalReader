"""
Pipeline de geração de áudio por período (sentença), com cache em disco e
pré-carregamento em segundo plano: enquanto um período é ouvido, os próximos
já vão sendo sintetizados, para reduzir o tempo de espera entre períodos.
"""
import asyncio
import hashlib
import os
import shutil
import tempfile
import threading

from PyQt6.QtCore import QObject, pyqtSignal

from tts_engines import TTSEngine

MAX_CONCURRENCY = 2


class SentencePipeline(QObject):
    sentence_ready = pyqtSignal(int, str)
    sentence_failed = pyqtSignal(int, str)

    def __init__(self, engine: TTSEngine, voice_id: str, rate_percent: int, pitch_hz: int, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.voice_id = voice_id
        self.rate_percent = rate_percent
        self.pitch_hz = pitch_hz

        params_key = f"{engine.id}|{voice_id}|{rate_percent}|{pitch_hz}"
        digest = hashlib.sha1(params_key.encode("utf-8")).hexdigest()[:10]
        cache_root = os.path.join(tempfile.gettempdir(), "tts_reader_cache")
        os.makedirs(cache_root, exist_ok=True)
        # Diretório exclusivo por pipeline: ao trocar de documento/voz o pipeline antigo
        # ainda pode ter sínteses em andamento, que não podem colidir com os arquivos
        # do novo (WinError 32) nem deixar áudio do texto anterior para trás.
        self.cache_dir = tempfile.mkdtemp(prefix=f"{digest}_", dir=cache_root)

        self._pending: set[tuple[int, int]] = set()
        self._inflight: set[tuple[int, int]] = set()
        self._lock = threading.Lock()
        self._closed = False

        self._loop = asyncio.new_event_loop()
        self._semaphore: asyncio.Semaphore | None = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        self._loop.run_forever()
        # Encerrado: cancela as sínteses pendentes e fecha o loop de forma limpa,
        # em vez de deixar o coletor de lixo despejar erros no log.
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        self._loop.close()

    # ``start`` = posição (dentro do período) da palavra onde a leitura começa;
    # 0 é o período inteiro. Cada início tem seu próprio arquivo no cache.
    def path_for(self, index: int, start: int = 0) -> str:
        suffix = f"_{start:05d}" if start else ""
        return os.path.join(self.cache_dir, f"{index:06d}{suffix}{self.engine.output_extension}")

    def is_cached(self, index: int, start: int = 0) -> bool:
        return os.path.exists(self.path_for(index, start))

    def request(self, index: int, text: str, start: int = 0) -> None:
        if self._closed:
            return
        key = (index, start)
        with self._lock:
            if key in self._pending or key in self._inflight or self.is_cached(index, start):
                return
            self._pending.add(key)
        asyncio.run_coroutine_threadsafe(self._synth(index, start, text[start:]), self._loop)

    async def _synth(self, index: int, start: int, text: str) -> None:
        key = (index, start)
        async with self._semaphore:
            with self._lock:
                self._pending.discard(key)
                self._inflight.add(key)
            path = self.path_for(index, start)
            tmp_path = path + ".part"
            try:
                await self.engine.synthesize(text, self.voice_id, self.rate_percent, self.pitch_hz, tmp_path)
                # Rename atômico: só existe um arquivo com o nome final quando
                # a escrita está 100% completa, evitando que o player tente
                # abrir um áudio parcialmente escrito (0 bytes ou truncado).
                os.replace(tmp_path, path)
                if not self._closed:
                    self.sentence_ready.emit(index, path)
            except Exception as exc:  # noqa: BLE001
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                if not self._closed:
                    self.sentence_failed.emit(index, str(exc))
            finally:
                with self._lock:
                    self._inflight.discard(key)

    def shutdown(self) -> None:
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)
        shutil.rmtree(self.cache_dir, ignore_errors=True)
