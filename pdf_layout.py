"""Texto de um PDF junto com a posição de cada período e palavra nas páginas.

O texto é montado a partir das palavras que o PyMuPDF devolve
(``get_text("words")``), guardando o retângulo de cada uma. Depois de dividir o
texto em períodos, cada período vira uma lista de retângulos (um por trecho de
linha), usados para destacar o período sobre a página e para saber em qual
período o mouse está; o retângulo de cada palavra destaca a palavra escolhida
com as setas. Trabalhar com palavras, e não caractere a caractere, mantém a
abertura de livros inteiros rápida.
"""
from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Callable

import pymupdf

from sentence_split import split_sentences

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1 em pontos da página

WORD_SEP = " "
LINE_SEP = "\n"
BLOCK_SEP = "\n\n"
PAGE_SEP = "\n\n"
_TEXT_FLAGS = pymupdf.TEXTFLAGS_WORDS & ~pymupdf.TEXT_PRESERVE_LIGATURES


class LoadCancelled(Exception):
    pass


@dataclass
class PdfWords:
    """Palavras do texto em arrays compactos (livros longos têm centenas de milhares)."""

    starts: array = field(default_factory=lambda: array("q"))
    ends: array = field(default_factory=lambda: array("q"))
    pages: array = field(default_factory=lambda: array("i"))
    lines: array = field(default_factory=lambda: array("i"))  # identificador global da linha
    rects: array = field(default_factory=lambda: array("d"))  # 4 valores por palavra

    def append(self, start: int, end: int, page: int, line: int, rect: Rect) -> None:
        self.starts.append(start)
        self.ends.append(end)
        self.pages.append(page)
        self.lines.append(line)
        self.rects.extend(rect)

    def rect(self, i: int) -> Rect:
        return tuple(self.rects[4 * i: 4 * i + 4])

    def __len__(self) -> int:
        return len(self.starts)


@dataclass
class PdfLayout:
    text: str
    sentences: list[tuple[str, int, int]]
    page_sizes: list[tuple[float, float]]
    # Para cada período: lista de (página, retângulo).
    sentence_rects: list[list[tuple[int, Rect]]]
    words: PdfWords = field(default_factory=PdfWords, repr=False)
    _page_index: dict[int, list[tuple[Rect, int]]] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        for index, rects in enumerate(self.sentence_rects):
            for page, rect in rects:
                self._page_index.setdefault(page, []).append((rect, index))

    def sentence_at(self, page: int, x: float, y: float) -> int | None:
        """Período sob o ponto (x, y) da página, ou None."""
        for (x0, y0, x1, y1), index in self._page_index.get(page, ()):
            if x0 <= x <= x1 and y0 <= y <= y1:
                return index
        return None

    def word_rect_at(self, offset: int) -> tuple[int, Rect] | None:
        """(página, retângulo) da palavra que começa em ``offset`` no texto."""
        i = bisect_left(self.words.starts, offset)
        if i < len(self.words) and self.words.starts[i] == offset:
            return self.words.pages[i], self.words.rect(i)
        return None


def _page_words(page: pymupdf.Page):
    """(bloco, linha, texto, retângulo) de cada palavra, na ordem de leitura do PyMuPDF."""
    matrix = page.rotation_matrix if page.rotation else None
    for x0, y0, x1, y1, word, block_no, line_no, _word_no in page.get_text("words", flags=_TEXT_FLAGS):
        if matrix is not None:
            r = pymupdf.Rect(x0, y0, x1, y1) * matrix
            x0, y0, x1, y1 = r.x0, r.y0, r.x1, r.y1
        yield block_no, line_no, word, (x0, y0, x1, y1)


def extract_pdf_layout(
    path: str,
    progress: Callable[[int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> PdfLayout:
    parts: list[str] = []
    length = 0
    words = PdfWords()
    page_sizes: list[tuple[float, float]] = []
    line_count = 0

    def add(text: str) -> None:
        nonlocal length
        parts.append(text)
        length += len(text)

    with pymupdf.open(path) as doc:
        total = doc.page_count
        for page_no, page in enumerate(doc):
            if is_cancelled is not None and is_cancelled():
                raise LoadCancelled()
            if progress is not None:
                progress(page_no, total)
            page_sizes.append((page.rect.width, page.rect.height))
            if page_no > 0:
                add(PAGE_SEP)

            current = None  # (bloco, linha)
            for block_no, line_no, word, rect in _page_words(page):
                if current is None:
                    line_count += 1
                elif block_no != current[0]:
                    add(BLOCK_SEP)
                    line_count += 1
                elif line_no != current[1]:
                    add(LINE_SEP)
                    line_count += 1
                else:
                    add(WORD_SEP)
                current = (block_no, line_no)
                words.append(length, length + len(word), page_no, line_count, rect)
                add(word)

    text = "".join(parts)
    sentences = split_sentences(text)
    sentence_rects = [_rects_for_range(words, start, end) for _, start, end in sentences]
    return PdfLayout(text, sentences, page_sizes, sentence_rects, words)


def _rects_for_range(words: PdfWords, start: int, end: int) -> list[tuple[int, Rect]]:
    """Um retângulo por linha com palavras que tocam text[start:end]."""
    first = bisect_right(words.ends, start)  # primeira palavra que termina depois de start
    last = bisect_left(words.starts, end)  # palavras com início antes de end
    rects: list[tuple[int, Rect]] = []
    current_line = None
    x0 = y0 = x1 = y1 = 0.0
    page = 0
    for i in range(first, last):
        bx0, by0, bx1, by1 = words.rect(i)
        line = words.lines[i]
        if line != current_line:
            if current_line is not None:
                rects.append((page, (x0, y0, x1, y1)))
            current_line, page = line, words.pages[i]
            x0, y0, x1, y1 = bx0, by0, bx1, by1
        else:
            x0, y0 = min(x0, bx0), min(y0, by0)
            x1, y1 = max(x1, bx1), max(y1, by1)
    if current_line is not None:
        rects.append((page, (x0, y0, x1, y1)))
    return rects


def sentence_index_at_offset(sentences: list[tuple[str, int, int]], starts: list[int], offset: int) -> int | None:
    """Período que contém a posição ``offset`` do texto (``starts`` = inícios ordenados)."""
    i = bisect_right(starts, offset) - 1
    if i >= 0 and offset < sentences[i][2]:
        return i
    return None
