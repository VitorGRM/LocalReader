"""
Separador de sentenças com rastreamento exato de offsets (start/end) no texto
original, para permitir highlight preciso no e-reader.
"""
import re

ABBREVIATIONS = {
    "sr.", "sra.", "srta.", "dr.", "dra.", "prof.", "profa.", "exmo.", "exma.",
    "av.", "r.", "n.", "no.", "nº.", "pág.", "p.", "ex.", "etc.", "cia.",
    "ltda.", "vs.", "art.", "cap.", "fig.", "ed.", "trad.", "org.", "min.",
    "max.", "mr.", "mrs.", "ms.", "jr.", "vol.", "cf.", "ref.", "obs.",
    "ap.", "depto.", "esq.", "km.",
}

_BOUNDARY_RE = re.compile(r"[.!?…]+[\"'”’)\]]*")


def _is_false_positive(text: str, match: re.Match) -> bool:
    punct_start = match.start()
    punct_text = match.group()

    # Decimal number: "3.14" — char before the dot and char right after the
    # punctuation run are both digits.
    if punct_text == "." and punct_start > 0:
        rest = text[match.end():]
        if text[punct_start - 1].isdigit() and rest and rest[0].isdigit():
            return True

    # Abbreviation / initials check — only relevant for a single trailing dot.
    if punct_text == ".":
        preceding = text[:punct_start]
        word_match = re.search(r"(\S+)$", preceding)
        if word_match:
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
