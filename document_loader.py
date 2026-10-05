"""Extração de texto de arquivos PDF, DOCX, TXT e Markdown."""
import os
import re

import docx
import pymupdf


def load_txt(path: str) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Não foi possível decodificar o arquivo de texto: {path}")


_MD_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_MD_HEADER_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)
_MD_LIST_MARKER_RE = re.compile(r"^\s*([-*+]|\d+\.)\s+", re.MULTILINE)
_MD_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$", re.MULTILINE)
_MD_EMPHASIS_RE = re.compile(r"(\*\*\*|\*\*|\*|___|__|_)")


def load_md(path: str) -> str:
    text = load_txt(path)
    text = _MD_CODE_BLOCK_RE.sub(" ", text)
    text = _MD_IMAGE_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_HEADER_RE.sub("", text)
    text = _MD_BLOCKQUOTE_RE.sub("", text)
    text = _MD_LIST_MARKER_RE.sub("", text)
    text = _MD_HR_RE.sub("", text)
    text = _MD_EMPHASIS_RE.sub("", text)
    return text


def load_docx(path: str) -> str:
    document = docx.Document(path)
    paragraphs = [p.text for p in document.paragraphs]
    return "\n\n".join(p for p in paragraphs if p.strip())


def load_pdf(path: str) -> str:
    text_parts = []
    with pymupdf.open(path) as pdf:
        for page in pdf:
            text_parts.append(page.get_text("text"))
    return "\n\n".join(text_parts)


def load_document(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".txt":
        text = load_txt(path)
    elif ext == ".md":
        text = load_md(path)
    elif ext == ".docx":
        text = load_docx(path)
    elif ext == ".pdf":
        text = load_pdf(path)
    else:
        raise ValueError(f"Formato de arquivo não suportado: {ext}")

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
