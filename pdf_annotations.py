"""Realces (marca-texto) dos PDFs.

Os realces ficam num JSON por documento, na pasta de dados do usuário, e não
no próprio PDF: abrir um arquivo para ouvir nunca o modifica. Cada realce é um
intervalo de palavras do ``PdfLayout`` mais o texto realçado, usado para
descartar realces que não batem mais (o arquivo mudou no mesmo caminho). Uma
cópia do PDF com os realces como anotações de verdade pode ser exportada.
"""
import hashlib
import json
import os
from dataclasses import asdict, dataclass

import pymupdf
from PyQt6.QtCore import QStandardPaths

from pdf_layout import PdfLayout

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
    first: int  # índice da primeira palavra
    last: int  # índice da última palavra (inclusive)
    color: str
    text: str


def default_store_dir() -> str:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    return os.path.join(base, "LocalReader", "TTSReader", "highlights")


def _store_path(pdf_path: str, store_dir: str | None) -> str:
    key = os.path.normcase(os.path.abspath(pdf_path))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(store_dir or default_store_dir(), f"{digest}.json")


def make_highlight(layout: PdfLayout, first: int, last: int, color: str) -> Highlight:
    first, last = min(first, last), max(first, last)
    return Highlight(first, last, color, layout.words_text(first, last))


def load_highlights(pdf_path: str, layout: PdfLayout, store_dir: str | None = None) -> list[Highlight]:
    try:
        with open(_store_path(pdf_path, store_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    result = []
    for item in data.get("highlights", []) if isinstance(data, dict) else []:
        try:
            highlight = Highlight(int(item["first"]), int(item["last"]), str(item["color"]), str(item["text"]))
        except (KeyError, TypeError, ValueError):
            continue
        valid = (
            0 <= highlight.first <= highlight.last < len(layout.words)
            and highlight.color in HIGHLIGHT_COLORS
            and layout.words_text(highlight.first, highlight.last) == highlight.text
        )
        if valid:
            result.append(highlight)
    return result


def save_highlights(pdf_path: str, highlights: list[Highlight], store_dir: str | None = None) -> None:
    path = _store_path(pdf_path, store_dir)
    if not highlights:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "version": _FORMAT_VERSION,
        "pdf": os.path.abspath(pdf_path),
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
