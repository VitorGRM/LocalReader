"""Realces (marca-texto) dos documentos.

Os realces ficam num JSON por documento, na pasta de dados do usuário, e não
no próprio arquivo: abrir um documento para ouvir nunca o modifica. Num PDF,
cada realce é um intervalo de palavras do ``PdfLayout``; num documento de texto
(TXT, DOCX, Markdown ou texto colado), um intervalo de posições do texto. Os
dois guardam também o texto realçado, usado para reencontrar o realce quando o
documento muda. Uma cópia do PDF com os realces como anotações de verdade pode
ser exportada.
"""
import hashlib
import json
import os
import re
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass

import pymupdf

from pdf_layout import PdfLayout
from platform_support import app_data_dir

# id -> (rótulo, RGB)
HIGHLIGHT_COLORS = {
    "yellow": ("Amarelo", (255, 226, 0)),
    "green": ("Verde", (100, 215, 90)),
    "blue": ("Azul", (90, 170, 255)),
    "pink": ("Rosa", (255, 120, 190)),
}
DEFAULT_COLOR = "yellow"
_FORMAT_VERSION = 1


@dataclass(frozen=True)
class Highlight:
    # PDF: índices da primeira e da última palavra (inclusive).
    # Texto: posições de início e fim (exclusivo) no texto.
    first: int
    last: int
    color: str
    text: str


PASTED_PREFIX = "pasted:"


def default_store_dir() -> str:
    return app_data_dir("highlights")


def document_key(path: str | None, text: str) -> str:
    """Chave dos realces: o caminho do arquivo ou, para texto colado, o próprio texto."""
    if path:
        return os.path.normcase(os.path.abspath(path))
    return PASTED_PREFIX + hashlib.sha1(text.encode("utf-8")).hexdigest()


def _store_path(key: str, store_dir: str | None) -> str:
    if not key.startswith(PASTED_PREFIX):
        key = os.path.normcase(os.path.abspath(key))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(store_dir or default_store_dir(), f"{digest}.json")


def _read_saved(key: str, store_dir: str | None) -> list[Highlight]:
    try:
        with open(_store_path(key, store_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    result = []
    for item in data.get("highlights", []) if isinstance(data, dict) else []:
        try:
            highlight = Highlight(int(item["first"]), int(item["last"]), str(item["color"]), str(item["text"]))
        except (KeyError, TypeError, ValueError):
            continue
        if highlight.color in HIGHLIGHT_COLORS:
            result.append(highlight)
    return result


def _occurrences(text: str, highlighted: str):
    """(início, fim) de cada ocorrência do texto realçado, aceitando outros espaços."""
    tokens = highlighted.split()
    if not tokens:
        return
    pattern = re.compile(r"\s+".join(map(re.escape, tokens)))
    for match in pattern.finditer(text):
        yield match.span()


def make_highlight(layout: PdfLayout, first: int, last: int, color: str) -> Highlight:
    first, last = min(first, last), max(first, last)
    return Highlight(first, last, color, layout.words_text(first, last))


def load_highlights(pdf_path: str, layout: PdfLayout, store_dir: str | None = None) -> list[Highlight]:
    result = []
    for highlight in _read_saved(pdf_path, store_dir):
        valid = (
            0 <= highlight.first <= highlight.last < len(layout.words)
            and layout.words_text(highlight.first, highlight.last) == highlight.text
        )
        if not valid:
            # A extração mudou (outra versão do app, notas lidas ou não): procura o
            # mesmo texto, na ocorrência mais perto de onde estava.
            highlight = _relocate(layout, highlight)
        if highlight is not None:
            result.append(highlight)
    return result


def _relocate(layout: PdfLayout, highlight: Highlight) -> Highlight | None:
    words = layout.words
    best = None
    for start, end in _occurrences(layout.text, highlight.text):
        first = bisect_left(words.starts, start)
        last = bisect_right(words.starts, end - 1) - 1
        if first >= len(words) or words.starts[first] != start or words.ends[last] != end:
            continue  # pedaço de palavra
        distance = abs(first - highlight.first)
        if best is None or distance < best[0]:
            best = (distance, first, last)
    if best is None:
        return None
    return make_highlight(layout, best[1], best[2], highlight.color)


# ---------- Documentos de texto ----------
def make_text_highlight(text: str, start: int, end: int, color: str) -> Highlight:
    start, end = min(start, end), max(start, end)
    return Highlight(start, end, color, text[start:end])


def load_text_highlights(key: str, text: str, store_dir: str | None = None) -> list[Highlight]:
    result = []
    for highlight in _read_saved(key, store_dir):
        if 0 <= highlight.first < highlight.last <= len(text) and text[highlight.first:highlight.last] == highlight.text:
            result.append(highlight)
            continue
        spans = list(_occurrences(text, highlight.text))
        if spans:  # o texto mudou: a ocorrência mais perto de onde estava
            start, end = min(spans, key=lambda span: abs(span[0] - highlight.first))
            result.append(make_text_highlight(text, start, end, highlight.color))
    return result


def save_highlights(key: str, highlights: list[Highlight], store_dir: str | None = None) -> None:
    """Grava os realces do documento (``key``: caminho do arquivo ou ``document_key``)."""
    path = _store_path(key, store_dir)
    if not highlights:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "version": _FORMAT_VERSION,
        "document": key if key.startswith(PASTED_PREFIX) else os.path.abspath(key),
        "highlights": [asdict(h) for h in highlights],
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def export_with_highlights(src: str, dest: str, layout: PdfLayout, highlights: list[Highlight]) -> None:
    """Salva em ``dest`` uma cópia de ``src`` com os realces como anotações do PDF."""
    with pymupdf.open(src) as doc:
        for highlight in highlights:
            _, rgb = HIGHLIGHT_COLORS.get(highlight.color, HIGHLIGHT_COLORS[DEFAULT_COLOR])
            for page_no, rect in layout.words_rects(highlight.first, highlight.last):
                page = doc[page_no]
                # O layout está nas coordenadas da página como é exibida; as
                # anotações usam as da página sem rotação.
                area = pymupdf.Rect(rect) * page.derotation_matrix if page.rotation else pymupdf.Rect(rect)
                annot = page.add_highlight_annot(area)
                annot.set_colors(stroke=[c / 255 for c in rgb])
                annot.update()
        doc.save(dest, garbage=1, deflate=True)
