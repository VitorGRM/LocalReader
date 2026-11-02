"""OCR das páginas de PDF sem texto aproveitável (digitalizadas ou com a camada
de texto corrompida), devolvendo cada palavra com sua posição na página.

Usa o executável do Tesseract com saída TSV, que traz a caixa de cada palavra:
convertida para pontos do PDF, ela alimenta o mesmo destaque, seleção e realce
das páginas com texto nativo. Várias páginas são reconhecidas em paralelo (um
processo por página, cada um com uma thread) e o resultado fica num cache em
disco, então reabrir o mesmo arquivo é imediato.
"""
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable

import pymupdf

from pdf_cleanup import Token
from platform_support import SUBPROCESS_KWARGS, app_data_dir, available_tesseract_languages

OCR_DPI = 300
MAX_RENDER_PIXELS = 80_000_000
PREFERRED_LANGUAGES = ("por", "eng")
MIN_CONFIDENCE = 30  # abaixo disso, só fica a palavra que tem letras ou números
PAGE_TIMEOUT_S = 300
OCR_SECONDS_PER_PAGE = 5.0  # por página e por processo (uma thread), para estimar o tempo
_CACHE_VERSION = 1


class OcrCancelled(Exception):
    pass


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def default_workers() -> int:
    return max(1, min(6, (os.cpu_count() or 2) // 2))  # um processo por núcleo físico, até 6


def choose_languages() -> str:
    installed = available_tesseract_languages()
    chosen = [lang for lang in PREFERRED_LANGUAGES if lang in installed] or installed[:1] or ["eng"]
    return "+".join(chosen)


def default_cache_dir() -> str:
    return app_data_dir("ocr-cache")


def _cache_path(pdf_path: str, cache_dir: str | None) -> str:
    stat = os.stat(pdf_path)
    key = f"{os.path.normcase(os.path.abspath(pdf_path))}|{stat.st_size}|{stat.st_mtime_ns}|{OCR_DPI}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir or default_cache_dir(), f"{digest}.json")


def _load_cache(path: str) -> dict[int, list[Token]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("version") != _CACHE_VERSION:
            return {}
        return {
            int(page): [Token(text, (x0, y0, x1, y1), (block, line)) for text, x0, y0, x1, y1, block, line in words]
            for page, words in data["pages"].items()
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _save_cache(path: str, pages: dict[int, list[Token]]) -> None:
    data = {
        "version": _CACHE_VERSION,
        "pages": {str(page): [[t.text, *t.rect, *t.line] for t in tokens] for page, tokens in pages.items()},
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass  # sem cache, só fica mais lento na próxima vez


def parse_tsv(tsv: str, scale: float) -> list[Token]:
    """Palavras do TSV do Tesseract, em pontos do PDF (pixels / ``scale``)."""
    tokens = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        if row.get("level") != "5" or not text:
            continue
        try:
            confidence = float(row["conf"])
            left, top = int(row["left"]), int(row["top"])
            width, height = int(row["width"]), int(row["height"])
            block = int(row["block_num"]) * 1000 + int(row["par_num"])  # parágrafo vira bloco
            line = int(row["line_num"])
        except (KeyError, TypeError, ValueError):
            continue
        if confidence < MIN_CONFIDENCE and not any(char.isalnum() for char in text):
            continue  # sujeira da digitalização (bordas, manchas)
        rect = (left / scale, top / scale, (left + width) / scale, (top + height) / scale)
        tokens.append(Token(text, rect, (block, line)))
    return tokens


class _Tesseract:
    """Roda o Tesseract numa página; ``cancel`` encerra os processos em andamento."""

    def __init__(self, languages: str):
        self.languages = languages
        self._processes: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self._cancelled = False
        self._env = dict(os.environ, OMP_THREAD_LIMIT="1")  # o paralelismo é entre páginas

    def run(self, png: bytes) -> str:
        executable = shutil.which("tesseract")
        if not executable:
            raise RuntimeError("Tesseract não foi encontrado.")
        command = [executable, "stdin", "stdout", "-l", self.languages, "--psm", "3", "tsv"]
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._env, **SUBPROCESS_KWARGS,
        )
        with self._lock:
            if self._cancelled:
                process.kill()
            self._processes.add(process)
        try:
            stdout, stderr = process.communicate(png, timeout=PAGE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise RuntimeError("O Tesseract excedeu o tempo limite numa página.")
        finally:
            with self._lock:
                self._processes.discard(process)
        if self._cancelled:
            raise OcrCancelled()
        if process.returncode != 0:
            details = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Tesseract falhou: {details or 'erro desconhecido'}")
        return stdout.decode("utf-8", errors="replace")

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            for process in self._processes:
                process.kill()


def _render(page: pymupdf.Page) -> tuple[bytes, float]:
    # A página é renderizada como exibida (com rotação), igual às coordenadas do layout.
    scale = OCR_DPI / 72.0
    pixels = page.rect.width * scale * page.rect.height * scale
    if pixels > MAX_RENDER_PIXELS:
        scale *= (MAX_RENDER_PIXELS / pixels) ** 0.5
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY, alpha=False)
    return pixmap.tobytes("png"), scale


def ocr_pages(
    doc: pymupdf.Document,
    path: str,
    page_numbers: list[int],
    progress: Callable[[int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    cache_dir: str | None = None,
    workers: int | None = None,
) -> dict[int, list[Token]]:
    """Reconhece as páginas pedidas. Usa o cache mesmo sem o Tesseract instalado;
    as páginas que ficarem sem resultado simplesmente não aparecem no dicionário.
    ``progress(feitas, total)``."""
    cache_file = _cache_path(path, cache_dir)
    cached = _load_cache(cache_file)
    results = {page: cached[page] for page in page_numbers if page in cached}
    pending_pages = [page for page in page_numbers if page not in results]
    total = len(page_numbers)
    if progress is not None:
        progress(len(results), total)
    if not pending_pages or not tesseract_available():
        return results

    workers = workers or default_workers()
    tesseract = _Tesseract(choose_languages())
    running: dict = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        queue = list(pending_pages)
        while queue or running:
            # O PyMuPDF não é thread-safe: as páginas são renderizadas aqui, e só
            # o Tesseract roda nas threads; no máximo 2 imagens esperando por worker.
            while queue and len(running) < workers * 2:
                if is_cancelled is not None and is_cancelled():
                    raise OcrCancelled()
                page_no = queue.pop(0)
                png, scale = _render(doc[page_no])
                running[pool.submit(tesseract.run, png)] = (page_no, scale)
            done, _ = wait(running, timeout=0.2, return_when=FIRST_COMPLETED)
            if is_cancelled is not None and is_cancelled():
                raise OcrCancelled()
            for future in done:
                page_no, scale = running.pop(future)
                results[page_no] = parse_tsv(future.result(), scale)
                if progress is not None:
                    progress(len(results), total)
    except BaseException:
        tesseract.cancel()  # encerra os processos antes de esperar as threads
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=True)
        # O que já foi reconhecido fica guardado, mesmo se a abertura foi cancelada.
        if len(results) > len(cached):
            _save_cache(cache_file, {**cached, **results})
    return results
