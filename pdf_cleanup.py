"""Limpeza do texto de PDFs para a leitura em voz alta.

Trabalha sobre as palavras já posicionadas (``Token``), nunca sobre o texto
solto, para que cada palavra continue ligada ao seu lugar na página (destaque,
seleção e realces). Nada é apagado da página: as palavras só são marcadas como
margem (cabeçalho, rodapé, número de página) ou nota de rodapé, e quem monta o
texto decide o que entra na leitura.
"""
import re
from collections import defaultdict
from dataclasses import dataclass
from statistics import median

Rect = tuple[float, float, float, float]

BODY, MARGIN, FOOTNOTE, MARKER = "body", "margin", "footnote", "marker"  # MARKER: chamada de nota solta

MARGIN_BAND = 0.12  # faixa no topo e no pé da página onde ficam cabeçalhos e rodapés
MARGIN_LINES = 3  # linhas de cada ponta consideradas
NEIGHBOR_PAGES = 3  # um cabeçalho se repete em alguma página até esta distância
SAME_POSITION = 0.04  # tolerância de posição vertical (fração da altura da página)
TITLE_RATIO = 1.3  # linha bem maior que o corpo é título, não cabeçalho corrido
HEADING_RATIO = 1.2  # título colado ao parágrafo: fonte bem maior que a linha seguinte
NOTE_SIZE_RATIO = 0.92  # notas usam fonte menor que o corpo
NOTE_REGION = 0.5  # notas começam na metade de baixo da página

_PAGE_NUMBER_RE = re.compile(
    r"^[\W_]*(?:p[áa]g(?:ina)?\.?\s*|p\.\s*|page\s*)?(?:\d{1,4}|[ivxlcdm]{1,7})"
    r"(?:\s*(?:/|de|of)\s*\d{1,4})?[\W_]*$",
    re.IGNORECASE,
)
_UNICODE_SUP = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_TRAILING_SUP_RE = re.compile(rf"(?:(?<=[^\W\d_]{{2}})|(?<=[.,;:!?\"'”’)\]]))[{_UNICODE_SUP}]+$")
_LEADING_SUP_RE = re.compile(rf"^[{_UNICODE_SUP}]+(?=\S)")
_ONLY_SUP_RE = re.compile(rf"^[{_UNICODE_SUP}]+$")

HYPHENS = "-‐‑"
SOFT_HYPHEN = "­"
SENTENCE_END = ".!?…:;\"'”’»)]"
# Pronomes que seguem o hífen do verbo ("disse-lhe"): o hífen é da palavra.
CLITICS = {
    "o", "a", "os", "as", "lo", "la", "los", "las", "no", "na", "nos", "nas",
    "me", "te", "se", "lhe", "lhes", "vos", "mo", "ma", "to", "ta", "lho", "lha",
}


@dataclass(slots=True)
class Token:
    text: str
    rect: Rect
    line: tuple[int, int]  # (bloco, linha) dentro da página
    kind: str = BODY

    @property
    def block(self) -> int:
        return self.line[0]


def group_lines(tokens: list[Token]) -> list[list[Token]]:
    lines: list[list[Token]] = []
    for token in tokens:
        if lines and lines[-1][0].line == token.line:
            lines[-1].append(token)
        else:
            lines.append([token])
    return lines


def _box(line: list[Token]) -> Rect:
    return (
        min(t.rect[0] for t in line),
        min(t.rect[1] for t in line),
        max(t.rect[2] for t in line),
        max(t.rect[3] for t in line),
    )


def _height(tokens: list[Token]) -> float:
    return median(t.rect[3] - t.rect[1] for t in tokens) if tokens else 0.0


def _signature(text: str) -> str:
    text = re.sub(r"\d+", "#", text.casefold())
    return re.sub(r"\W+", " ", text).strip()


# ---------- Cabeçalhos, rodapés e números de página ----------
def mark_margins(pages: list[list[Token]], sizes: list[tuple[float, float]]) -> None:
    """Marca como MARGIN as linhas do topo/pé que são número de página ou que se
    repetem (no mesmo lugar) em páginas vizinhas — cabeçalhos e rodapés corridos,
    inclusive os que alternam entre páginas pares e ímpares."""
    candidates: list[list[tuple[str, str, float, list[Token]]]] = []
    index: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    for page_no, (tokens, (_, height)) in enumerate(zip(pages, sizes)):
        page_candidates = []
        lines = group_lines(tokens)
        if lines and height > 0:
            body_height = _height(tokens)
            ordered = sorted(lines, key=lambda line: _box(line)[1])
            seen: set[int] = set()
            edges = [("top", line) for line in ordered[:MARGIN_LINES]]
            edges += [("bottom", line) for line in ordered[-MARGIN_LINES:]]
            for band, line in edges:
                box = _box(line)
                in_band = box[3] <= height * MARGIN_BAND if band == "top" else box[1] >= height * (1 - MARGIN_BAND)
                if not in_band or id(line) in seen or _height(line) > body_height * TITLE_RATIO:
                    continue
                seen.add(id(line))
                text = " ".join(t.text for t in line)
                position = (box[1] + box[3]) / 2 / height
                page_candidates.append((band, text, position, line))
                signature = _signature(text)
                if signature:
                    index[(band, signature)].append((page_no, position))
        candidates.append(page_candidates)

    for page_no, page_candidates in enumerate(candidates):
        for band, text, position, line in page_candidates:
            repeated = any(
                other != page_no and abs(other - page_no) <= NEIGHBOR_PAGES and abs(pos - position) <= SAME_POSITION
                for other, pos in index.get((band, _signature(text)), ())
            )
            if repeated or _PAGE_NUMBER_RE.match(text):
                for token in line:
                    token.kind = MARGIN


# ---------- Notas de rodapé ----------
def strip_unicode_superscripts(tokens: list[Token], markers: set[str], note_lines: set[tuple[int, int]]) -> None:
    """Tira chamadas de nota escritas com caracteres sobrescritos ("texto¹²")."""
    if not any(char in _UNICODE_SUP for token in tokens for char in token.text):
        return
    first_of_line = {line[0].line: line[0] for line in group_lines(tokens)}
    for token in tokens:
        if _ONLY_SUP_RE.match(token.text):
            markers.add(_plain_digits(token.text))
            token.kind = MARKER
            if first_of_line.get(token.line) is token:
                note_lines.add(token.line)
            continue
        match = _TRAILING_SUP_RE.search(token.text)
        if match:
            markers.add(_plain_digits(match.group()))
            token.text = token.text[: match.start()]
        match = _LEADING_SUP_RE.match(token.text)
        if match and first_of_line.get(token.line) is token:
            markers.add(_plain_digits(match.group()))
            token.text = token.text[match.end():]
            note_lines.add(token.line)


def _plain_digits(text: str) -> str:
    return text.translate(str.maketrans(_UNICODE_SUP, "0123456789"))


def mark_footnotes(
    tokens: list[Token], markers: set[str], note_lines: set[tuple[int, int]], page_height: float
) -> None:
    """Marca como FOOTNOTE as notas no pé da página: linhas em fonte menor, a
    partir da primeira que começa com uma chamada de nota."""
    body = [t for t in tokens if t.kind == BODY]
    if not body or not (markers or note_lines):
        return
    body_height = _height(body)
    lines = group_lines(body)
    start = None
    for line in lines:
        box = _box(line)
        if box[1] < page_height * NOTE_REGION or _height(line) >= body_height * NOTE_SIZE_RATIO:
            continue
        number = line[0].text.rstrip(".):]")
        if line[0].line in note_lines or number in markers:
            start = box[1] if start is None else min(start, box[1])
    if start is None:
        return
    for line in lines:
        if _box(line)[1] >= start - 1 and _height(line) < body_height * NOTE_SIZE_RATIO:
            for token in line:
                token.kind = FOOTNOTE


# ---------- Hifenização e junção de linhas ----------
def join_hyphenated(previous: str, following: str) -> tuple[str, str] | None:
    """Palavra partida no fim da linha: devolve (anterior, seguinte) para serem
    escritas coladas, ou None se não é hifenização."""
    if previous.endswith(SOFT_HYPHEN) and len(previous) > 1:
        return previous[:-1], following
    if len(previous) < 2 or previous[-1] not in HYPHENS or not previous[-2].isalpha():
        return None
    # Norma do português: ao partir no hífen da palavra, ele se repete na linha seguinte.
    if len(following) > 1 and following[0] in HYPHENS and following[1].isalpha():
        return previous, following[1:]
    if not following[:1].islower():
        return None
    if following.rstrip(SENTENCE_END + ",").lower() in CLITICS:
        return previous, following
    return previous[:-1], following


def is_heading_break(previous: str, previous_rect: Rect, following: str, following_rect: Rect) -> bool:
    """Título no mesmo bloco do texto: linha bem maior que a seguinte, sem pontuação final."""
    previous_height = previous_rect[3] - previous_rect[1]
    following_height = following_rect[3] - following_rect[1]
    return (
        bool(previous)
        and previous[-1] not in SENTENCE_END + ","
        and previous_height > following_height * HEADING_RATIO
        and following[:1].isupper()
    )


def continues_sentence(previous: str, following: str, new_page: bool) -> bool:
    """O texto segue no mesmo período apesar da troca de bloco ou de página?"""
    if not previous or not following or previous[-1] in SENTENCE_END:
        return False
    return new_page or following[0].islower() or previous[-1] == ","
