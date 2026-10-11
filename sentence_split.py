"""
Separador de sentenças com rastreamento exato de offsets (start/end) no texto
original, para permitir highlight preciso no e-reader.
"""
import re
from bisect import bisect_right

ABBREVIATIONS = {
    "sr.", "sra.", "srta.", "dr.", "dra.", "prof.", "profa.", "exmo.", "exma.",
    "av.", "r.", "n.", "no.", "nº.", "pág.", "p.", "ex.", "etc.", "cia.",
    "ltda.", "vs.", "art.", "cap.", "fig.", "ed.", "trad.", "org.", "min.",
    "max.", "mr.", "mrs.", "ms.", "jr.", "vol.", "cf.", "ref.", "obs.",
    "ap.", "depto.", "esq.", "km.",
}

_BOUNDARY_RE = re.compile(r"[.!?…]+[\"'”’)\]]*")
_LAST_WORD_RE = re.compile(r"(\S+)$")
# Só a palavra antes do ponto importa; olhar um trecho curto (e não o texto
# inteiro até ali) evita tempo quadrático em livros longos.
_LOOKBEHIND = 64


def _is_false_positive(text: str, match: re.Match) -> bool:
    punct_start = match.start()
    punct_text = match.group()

    # Decimal number: "3.14" — char before the dot and char right after the
    # punctuation run are both digits.
    if punct_text == "." and punct_start > 0:
        after = match.end()
        if text[punct_start - 1].isdigit() and after < len(text) and text[after].isdigit():
            return True

    # Abbreviation / initials check — only relevant for a single trailing dot.
    if punct_text == ".":
        window_start = max(0, punct_start - _LOOKBEHIND)
        preceding = text[window_start:punct_start]
        word_match = _LAST_WORD_RE.search(preceding)
        # Palavra maior que a janela: não é abreviação nem inicial.
        if word_match and (word_match.start() > 0 or window_start == 0):
            word = word_match.group(1).lower()
            if word + "." in ABBREVIATIONS or word in ABBREVIATIONS:
                return True
            # Single letter initials: "J." "K."
            if len(word) == 1 and word.isalpha():
                return True

    return False


def _bracket_depth_at_each_position(text: str) -> list[int]:
    """Depth of unclosed '(' / '[' brackets at each character index, so that
    punctuation found inside parentheses or brackets (e.g. markdown links like
    '[texto](https://site.com/pagina.html)') is never treated as a sentence end."""
    depth_at = [0] * (len(text) + 1)
    depth = 0
    for i, ch in enumerate(text):
        depth_at[i] = depth
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
    depth_at[len(text)] = depth
    return depth_at


def split_sentences(text: str) -> list[tuple[str, int, int]]:
    """Split text into sentences, returning (sentence_text, start, end) so that
    text[start:end] == sentence_text for every entry."""
    if not text:
        return []

    depth_at = _bracket_depth_at_each_position(text)

    boundaries = []
    for match in _BOUNDARY_RE.finditer(text):
        if depth_at[match.start()] > 0:
            continue
        if _is_false_positive(text, match):
            continue
        boundaries.append(match.end())

    if not boundaries or boundaries[-1] != len(text):
        boundaries.append(len(text))

    sentences: list[tuple[str, int, int]] = []
    cursor = 0
    for end in boundaries:
        raw = text[cursor:end]
        stripped = raw.strip()
        if stripped and re.search(r"\w", stripped, flags=re.UNICODE):
            leading_ws = len(raw) - len(raw.lstrip())
            s_start = cursor + leading_ws
            s_end = s_start + len(stripped)
            sentences.append((stripped, s_start, s_end))
        cursor = end

    return sentences


# ---------------------------------------------------------------------------
# Navegação por palavra dentro dos períodos. A posição de leitura é
# (período, início da palavra no texto); ``None`` como palavra significa o
# início do período (sua primeira palavra).
_WORD_RE = re.compile(r"\S+")


def word_starts(sentence: tuple[str, int, int]) -> list[int]:
    """Início (no texto completo) de cada palavra do período."""
    text, start, _ = sentence
    return [start + m.start() for m in _WORD_RE.finditer(text)]


def word_span(sentence: tuple[str, int, int], word_offset: int) -> tuple[int, int]:
    """(início, fim) no texto completo da palavra que começa em ``word_offset``."""
    text, start, _ = sentence
    match = _WORD_RE.match(text, word_offset - start)
    end = start + match.end() if match else word_offset
    return word_offset, end


def step_word(
    sentences: list[tuple[str, int, int]], index: int, word_offset: int | None, step: int
) -> tuple[int, int | None]:
    """Anda ``step`` (+1/-1) palavras, passando para o período vizinho nas pontas."""
    starts = word_starts(sentences[index])
    position = 0 if word_offset is None else max(0, bisect_right(starts, word_offset) - 1)
    target = position + step
    if 0 <= target < len(starts):
        return index, None if target == 0 else starts[target]
    if target >= len(starts):
        return (index + 1, None) if index + 1 < len(sentences) else (index, word_offset)
    if index == 0:
        return index, word_offset
    previous = word_starts(sentences[index - 1])
    return index - 1, previous[-1] if len(previous) > 1 else None
