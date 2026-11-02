"""Texto de um PDF junto com a posição de cada período e palavra nas páginas.

O texto é montado a partir das palavras que o PyMuPDF devolve
(``get_text("words")``), guardando o retângulo de cada uma. Depois de dividir o
texto em períodos, cada período vira uma lista de retângulos (um por trecho de
linha), usados para destacar o período sobre a página e para saber em qual
período o mouse está; o retângulo de cada palavra destaca a palavra escolhida
com as setas. Trabalhar com palavras, e não caractere a caractere, mantém a
abertura de livros inteiros rápida.

Antes de virar texto, as palavras passam pela limpeza de ``pdf_cleanup``
(cabeçalhos, rodapés, números de página, chamadas e textos de notas, palavras
hifenizadas); páginas digitalizadas ou com texto ilegível são reconhecidas por
OCR (``pdf_ocr``), também palavra a palavra.
"""
import re
import unicodedata
from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

import pymupdf

from pdf_cleanup import (
    BODY,
    FOOTNOTE,
    MARKER,
    Token,
    continues_sentence,
    group_lines,
    is_heading_break,
    join_hyphenated,
    mark_footnotes,
    mark_margins,
    rebuild_lines,
    strip_unicode_superscripts,
)
from pdf_ocr import OcrCancelled, ocr_pages
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
    ocr_pages: list[int] = field(default_factory=list)  # páginas reconhecidas por OCR
    ocr_missing_pages: list[int] = field(default_factory=list)  # precisavam de OCR e ficaram sem
    ocr_error: str = ""
    scanned_text_pages: int = 0  # páginas digitalizadas que já vieram com texto (OCR anterior)
    _page_index: dict[int, list[tuple[Rect, int]]] = field(default_factory=dict, repr=False)
    _folded: str | None = field(default=None, repr=False)

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

    def word_near(self, page: int, x: float, y: float) -> int | None:
        """Índice da palavra da página mais próxima do ponto (x, y), para a seleção com o mouse."""
        first = bisect_left(self.words.pages, page)
        last = bisect_right(self.words.pages, page)
        best, best_dist = None, 0.0
        for i in range(first, last):
            x0, y0, x1, y1 = self.words.rect(i)
            dx = max(x0 - x, 0.0, x - x1)
            dy = max(y0 - y, 0.0, y - y1)
            dist = dx + 3 * dy  # prefere palavras da mesma linha
            if best is None or dist < best_dist:
                best, best_dist = i, dist
        return best

    def words_text(self, first: int, last: int) -> str:
        """Texto da palavra ``first`` até a ``last`` (em qualquer ordem), com as quebras de linha."""
        first, last = min(first, last), max(first, last)
        return self.text[self.words.starts[first]:self.words.ends[last]]

    def words_rects(self, first: int, last: int) -> list[tuple[int, Rect]]:
        """Retângulos (um por linha) da palavra ``first`` até a ``last`` (em qualquer ordem)."""
        first, last = min(first, last), max(first, last)
        return _rects_for_range(self.words, self.words.starts[first], self.words.ends[last])

    def range_rects(self, start: int, end: int) -> list[tuple[int, Rect]]:
        """Retângulos (um por linha) das palavras que tocam text[start:end]."""
        return _rects_for_range(self.words, start, end)

    def find(self, query: str) -> list[tuple[int, int]]:
        """(início, fim) de cada ocorrência de ``query`` no texto, sem diferenciar
        maiúsculas nem acentos; espaços na busca casam com qualquer quebra de linha."""
        tokens = fold_text(query).split()
        if not tokens:
            return []
        if self._folded is None:
            self._folded = fold_text(self.text)
        pattern = re.compile(r"\s+".join(map(re.escape, tokens)))
        return [match.span() for match in pattern.finditer(self._folded)]


@lru_cache(maxsize=4096)
def _fold_char(char: str) -> str:
    lower = char.lower()
    if len(lower) != 1:
        lower = char
    base = unicodedata.normalize("NFD", lower)[0]
    return base if unicodedata.category(base) != "Mn" else lower


def fold_text(text: str) -> str:
    """Minúsculas e sem acentos, com o mesmo tamanho (as posições continuam valendo)."""
    return "".join(char if char.isascii() and not char.isupper() else _fold_char(char) for char in text)


@dataclass
class ExtractOptions:
    read_footnotes: bool = False  # ler também o texto das notas de rodapé
    ocr: bool = True  # reconhecer (OCR) as páginas sem texto aproveitável
    # Refazer o OCR também das páginas digitalizadas que já trazem texto (de um
    # OCR antigo, como nos livros do Internet Archive), ignorando esse texto.
    force_ocr: bool = False
    ocr_cache_dir: str | None = None


# Chamada de nota em fonte sobrescrita: "texto12", "12Nota" ou "12" sozinho.
_SUP_TEXT_RE = re.compile(r"^[\d*†‡]{1,3}$")
_MARKER_CANDIDATE_RE = re.compile(r"(?:[^\W\d_]|[.,;:!?)\]\"'”’])\d{1,3}$|^\d{1,3}(?:[^\W\d_]|$)|[*†‡]")
_GARBAGE_CATEGORIES = {"Co", "Cc", "Cs"}  # uso privado, controle: fonte sem mapeamento de texto
MIN_TEXT_CHARS = 40
SCANNED_IMAGE_COVERAGE = 0.5


def _superscript_spans(page: pymupdf.Page, textpage) -> list[tuple[Rect, str]]:
    """Trechos curtos (números, *, †) em fonte menor e acima da linha de base."""
    found = []
    for block in page.get_text("dict", textpage=textpage)["blocks"]:
        for line in block.get("lines", ()):
            spans = [span for span in line["spans"] if span["text"].strip()]
            if not spans:
                continue
            main_size = max(span["size"] for span in spans)
            baseline = max(span["origin"][1] for span in spans if span["size"] >= main_size * 0.95)
            for span in spans:
                text = span["text"].strip()
                if not _SUP_TEXT_RE.match(text):
                    continue
                raised = span["origin"][1] < baseline - 0.2 * span["size"]
                if span["flags"] & 1 or (span["size"] < main_size * 0.85 and raised):
                    found.append((tuple(span["bbox"]), text))
    return found


def _apply_superscripts(tokens: list[Token], sups, markers: set[str], note_lines: set) -> None:
    """Tira das palavras as chamadas de nota sobrescritas, ajustando o retângulo."""
    first_in_line = {id(line_tokens[0]) for line_tokens in group_lines(tokens)}
    for token in tokens:
        wx0, wy0, wx1, wy1 = token.rect
        for (sx0, sy0, sx1, sy1), sup in sups:
            overlap_x = min(wx1, sx1) - max(wx0, sx0)
            overlap_y = min(wy1, sy1) - max(wy0, sy0)
            if overlap_x <= 0 or overlap_y <= 0 or overlap_x * overlap_y < 0.5 * (sx1 - sx0) * (sy1 - sy0):
                continue
            first = id(token) in first_in_line
            text = token.text
            if text == sup:
                token.kind = MARKER
                starts_note = first
            elif text.endswith(sup) and sx0 > wx0 + 0.5 and not text[-len(sup) - 1].isdigit():
                token.text, token.rect = text[: -len(sup)], (wx0, wy0, sx0, wy1)
                starts_note = False
            elif text.startswith(sup) and sx1 < wx1 - 0.5 and first:
                token.text, token.rect = text[len(sup):], (sx1, wy0, wx1, wy1)
                starts_note = True
            else:
                continue  # expoente de número (10⁶) ou coincidência: fica como está
            markers.add(sup)
            if starts_note:
                note_lines.add(token.line)
            break


def _native_tokens(page: pymupdf.Page) -> tuple[list[Token], set[str], set]:
    """Palavras da camada de texto da página, já sem as chamadas de nota."""
    textpage = page.get_textpage(flags=_TEXT_FLAGS)  # uma análise da página para palavras e fontes
    tokens = [
        Token(word, (x0, y0, x1, y1), (block_no, line_no))
        for x0, y0, x1, y1, word, block_no, line_no, _ in page.get_text("words", textpage=textpage)
    ]
    # Tudo daqui em diante nas coordenadas da página como é exibida (com rotação),
    # onde as linhas de texto são horizontais.
    matrix = page.rotation_matrix if page.rotation else None
    if matrix is not None:
        for token in tokens:
            token.rect = _transform(token.rect, matrix)
    rebuild_lines(tokens)
    markers: set[str] = set()
    note_lines: set = set()
    # O "dict" (com tamanho de fonte) é mais lento: só nas páginas com candidatos.
    if any(_MARKER_CANDIDATE_RE.search(token.text) for token in tokens):
        sups = _superscript_spans(page, textpage)
        if matrix is not None:
            sups = [(_transform(rect, matrix), text) for rect, text in sups]
        if sups:
            _apply_superscripts(tokens, sups, markers, note_lines)
    strip_unicode_superscripts(tokens, markers, note_lines)
    return tokens, markers, note_lines


def _transform(rect: Rect, matrix: pymupdf.Matrix) -> Rect:
    r = pymupdf.Rect(rect) * matrix
    return r.x0, r.y0, r.x1, r.y1


def _is_scanned(page: pymupdf.Page) -> bool:
    """Uma imagem cobre boa parte da página (página digitalizada)."""
    page_area = page.rect.get_area()
    if page_area <= 0:
        return False
    covered = sum(pymupdf.Rect(info["bbox"]).get_area() for info in page.get_image_info())
    return covered >= SCANNED_IMAGE_COVERAGE * page_area


def _has_text(tokens: list[Token]) -> bool:
    return sum(len(token.text) for token in tokens) >= MIN_TEXT_CHARS


def _needs_ocr(tokens: list[Token], scanned: bool) -> bool:
    """Página digitalizada sem texto, ou com camada de texto ilegível."""
    if not _has_text(tokens):
        return scanned
    chars = [char for token in tokens for char in token.text if not char.isspace()]
    garbage = sum(char == "\ufffd" or unicodedata.category(char) in _GARBAGE_CATEGORIES for char in chars)
    letters = sum(char.isalnum() for char in chars)
    return garbage > 0.1 * len(chars) or letters < 0.4 * len(chars)


def _assemble(pages: list[list[Token]], kinds: set[str]) -> tuple[str, PdfWords]:
    """Monta o texto de leitura: separa linhas, blocos e páginas, junta palavras
    hifenizadas e não quebra o período que continua no bloco ou página seguinte."""
    entries: list[list] = []  # [texto, retângulo, página, linha, separador antes]
    line_count = 0
    previous_page = previous_line = None
    for page_no, tokens in enumerate(pages):
        for token in tokens:
            if token.kind not in kinds or not token.text:
                continue
            text = token.text
            new_page = bool(entries) and page_no != previous_page
            if not entries:
                separator = ""
            elif new_page:
                separator = PAGE_SEP
            elif token.line != previous_line:
                separator = BLOCK_SEP if token.block != previous_line[0] else LINE_SEP
            else:
                separator = WORD_SEP
            if separator != WORD_SEP:
                line_count += 1
            if entries and separator not in (WORD_SEP, ""):
                last = entries[-1]
                joined = join_hyphenated(last[0], text)
                if joined is not None:
                    last[0], text = joined
                    separator = ""
                elif separator != LINE_SEP and continues_sentence(last[0], text, new_page):
                    separator = LINE_SEP
                elif separator == LINE_SEP and is_heading_break(last[0], last[1], text, token.rect):
                    separator = BLOCK_SEP
            entries.append([text, token.rect, page_no, line_count, separator])
            previous_page, previous_line = page_no, token.line

    parts: list[str] = []
    length = 0
    words = PdfWords()
    for text, rect, page_no, line, separator in entries:
        parts.append(separator)
        length += len(separator)
        words.append(length, length + len(text), page_no, line, rect)
        parts.append(text)
        length += len(text)
    return "".join(parts), words


def extract_pdf_layout(
    path: str,
    progress: Callable[[int, int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    options: ExtractOptions | None = None,
) -> PdfLayout:
    """``progress(atual, total, mensagem)``."""
    options = options or ExtractOptions()
    report = progress or (lambda _current, _total, _message: None)
    pages: list[list[Token]] = []
    markers: list[set[str]] = []
    note_lines: list[set] = []
    page_sizes: list[tuple[float, float]] = []
    need_ocr: list[int] = []
    ocr_done: list[int] = []
    ocr_missing: list[int] = []
    ocr_error = ""
    scanned_text_pages = 0

    with pymupdf.open(path) as doc:
        total = doc.page_count
        for page_no, page in enumerate(doc):
            if is_cancelled is not None and is_cancelled():
                raise LoadCancelled()
            report(page_no, total, f"Lendo o PDF: página {page_no + 1} de {total}...")
            page_sizes.append((page.rect.width, page.rect.height))
            tokens, page_markers, page_notes = _native_tokens(page)
            scanned = _is_scanned(page)
            if scanned and _has_text(tokens):
                scanned_text_pages += 1
            if options.ocr and (_needs_ocr(tokens, scanned) or (options.force_ocr and scanned)):
                need_ocr.append(page_no)
            pages.append(tokens)
            markers.append(page_markers)
            note_lines.append(page_notes)

        if need_ocr:
            try:
                results = ocr_pages(
                    doc,
                    path,
                    need_ocr,
                    progress=lambda done, count: report(
                        done, count, f"Reconhecendo o texto (OCR): {done} de {count} página(s)..."
                    ),
                    is_cancelled=is_cancelled,
                    cache_dir=options.ocr_cache_dir,
                )
            except OcrCancelled:
                raise LoadCancelled() from None
            except Exception as exc:  # noqa: BLE001 - sem OCR, o resto do PDF ainda abre
                ocr_error = str(exc)
                results = {}
            for page_no, tokens in results.items():
                pages[page_no], markers[page_no], note_lines[page_no] = tokens, set(), set()
            ocr_done = sorted(results)
        ocr_missing = [page_no for page_no in need_ocr if page_no not in ocr_done]

    mark_margins(pages, page_sizes)
    for tokens, page_markers, page_notes, (_, height) in zip(pages, markers, note_lines, page_sizes):
        mark_footnotes(tokens, page_markers, page_notes, height)
    text, words = _assemble(pages, {BODY, FOOTNOTE} if options.read_footnotes else {BODY})
    sentences = split_sentences(text)
    sentence_rects = [_rects_for_range(words, start, end) for _, start, end in sentences]
    layout = PdfLayout(text, sentences, page_sizes, sentence_rects, words)
    layout.ocr_pages, layout.ocr_missing_pages, layout.ocr_error = ocr_done, ocr_missing, ocr_error
    layout.scanned_text_pages = scanned_text_pages
    return layout


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
