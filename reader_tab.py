"""Uma aba de leitura: documento, voz, posição e áudio próprios.

Cada aba guarda tudo o que é dela (texto, períodos, período atual, ajustes de
voz e o pipeline de áudio com seu cache), para que trocar de aba nunca misture
idioma, texto ou preferências. A janela principal só mostra e controla a aba
ativa.
"""
import hashlib
import os
import traceback
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace

from PyQt6.QtCore import QEvent, QPoint, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIntValidator, QKeySequence, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from document_loader import load_document
from pdf_annotations import (
    DEFAULT_COLOR,
    HIGHLIGHT_COLORS,
    Highlight,
    document_key,
    export_with_highlights,
    load_highlights,
    load_text_highlights,
    make_text_highlight,
    save_highlights,
)
from pdf_layout import (
    ExtractOptions,
    LoadCancelled,
    PdfLayout,
    extract_pdf_layout,
    find_in_folded,
    fold_text,
    sentence_index_at_offset,
)
from pdf_ocr import OCR_SECONDS_PER_PAGE, default_workers, tesseract_available
from pdf_view import MAX_DRAWN_MATCHES, PdfView, color_icon
from platform_support import app_settings
from theme import PDF_SEARCH, PDF_SEARCH_CURRENT
from reading_time import ReadingTimeEstimator
from sentence_split import split_sentences, word_span
from tts_engines import ENGINES
from tts_pipeline import SentencePipeline

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".md", ".txt")
FILE_DIALOG_FILTER = "Documentos (*.pdf *.docx *.md *.txt);;Todos os arquivos (*)"
SCROLL_TOP_MARGIN = 24
CLICK_SLOP_PX = 5
SEARCH_DELAY_MS = 250  # espera a digitação parar antes de buscar
MAX_TOC_ENTRIES = 500
BASE_FONT_SIZE, MIN_FONT_SIZE, MAX_FONT_SIZE = 14, 8, 40
TEXT_HIGHLIGHT_ALPHA = 120  # marca-texto sobre o texto (tema claro ou escuro)
NEW_TAB_TITLE = "Nova aba"
READY_MESSAGE = "Abra um arquivo ou cole um texto para começar."

PAGE_START, PAGE_LOADING, PAGE_TEXT, PAGE_PDF = range(4)
READ_FOOTNOTES_KEY = "pdf/read_footnotes"
FORCE_OCR_GROUP = "pdf_force_ocr"  # PDFs (por caminho) cujo texto é refeito por OCR


def _force_ocr_key(path: str) -> str:
    digest = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()
    return f"{FORCE_OCR_GROUP}/{digest}"


def pdf_extract_options(path: str) -> ExtractOptions:
    """Opções de extração escolhidas pelo usuário para este PDF."""
    settings = app_settings()
    return ExtractOptions(
        read_footnotes=settings.value(READ_FOOTNOTES_KEY, False, type=bool),
        force_ocr=settings.value(_force_ocr_key(path), False, type=bool),
    )


@dataclass
class VoiceSettings:
    engine_id: str = ""
    language: str = ""
    country: str = ""
    voice_id: str = ""
    rate: int = 0
    pitch: int = 0

    def copy(self) -> "VoiceSettings":
        return replace(self)


@dataclass
class LoadedDocument:
    kind: str  # "text" ou "pdf"
    text: str
    sentences: list[tuple[str, int, int]]
    layout: PdfLayout | None = None


# Threads de carregamento ainda rodando (inclusive as canceladas): a referência
# impede que o Python destrua um QThread em execução.
_LIVE_LOADERS: set["DocumentLoadThread"] = set()


def wait_for_loaders(msecs: int) -> None:
    for loader in list(_LIVE_LOADERS):
        loader.cancel()
        loader.wait(msecs)


class DocumentLoadThread(QThread):
    loaded = pyqtSignal(object)
    failed = pyqtSignal(str, str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, path: str, options: ExtractOptions | None = None, parent=None):
        super().__init__(parent)
        self.path = path
        self.options = options
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self):
        try:
            if os.path.splitext(self.path)[1].lower() == ".pdf":
                layout = extract_pdf_layout(
                    self.path,
                    progress=self.progress.emit,
                    is_cancelled=lambda: self._cancelled,
                    options=self.options,
                )
                result = LoadedDocument("pdf", layout.text, layout.sentences, layout)
            else:
                text = load_document(self.path)
                if not text.strip():
                    raise ValueError("Não foi possível extrair texto desse arquivo.")
                result = LoadedDocument("text", text, split_sentences(text))
            if not self._cancelled:
                self.loaded.emit(result)
        except LoadCancelled:
            pass
        except Exception as exc:  # noqa: BLE001
            if not self._cancelled:
                self.failed.emit(str(exc), traceback.format_exc())


# ---------------------------------------------------------------------------
class ReaderView(QTextEdit):
    """Texto somente-leitura: destaca o período sob o mouse, e um clique
    escolhe o período. Setas para cima/baixo e espaço controlam a leitura.

    Como no PDF: seleção e cópia, marca-texto (desenhado aqui, guardado pela
    aba), resultados da busca e zoom (Ctrl + roda do mouse, Ctrl+= / Ctrl+- /
    Ctrl+0)."""

    navigate_prev = pyqtSignal()
    navigate_next = pyqtSignal()
    word_prev = pyqtSignal()
    word_next = pyqtSignal()
    toggle_play = pyqtSignal()
    sentence_clicked = pyqtSignal(int)
    selection_changed = pyqtSignal(bool)
    highlight_requested = pyqtSignal(str)  # cor do realce para a seleção ("" = a última usada)
    remove_highlight_requested = pyqtSignal(int)  # índice do realce

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setCursorWidth(0)
        self.setFont(QFont("Georgia", BASE_FONT_SIZE))
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._sentences: list[tuple[str, int, int]] = []
        self._starts: list[int] = []
        self._current = -1
        self._word: tuple[int, int] | None = None
        self._hover: int | None = None
        self._press_pos: QPoint | None = None
        self._highlight_color = None
        self._word_color = None
        self._hover_color = None
        self._user_highlights: list[tuple[int, int, str]] = []  # (início, fim, cor)
        self._matches: list[tuple[int, int]] = []
        self._match_index = -1
        self.copyAvailable.connect(self.selection_changed)

    def set_content(self, text: str, sentences: list[tuple[str, int, int]]) -> None:
        self._sentences = sentences
        self._starts = [start for _, start, _ in sentences]
        self._current = -1
        self._word = None
        self._hover = None
        self._user_highlights = []
        self._matches, self._match_index = [], -1
        self.setPlainText(text)

    # ---------- Marca-texto e busca ----------
    def set_user_highlights(self, highlights: list[tuple[int, int, str]]) -> None:
        self._user_highlights = list(highlights)
        self._refresh_selections()

    def selected_range(self) -> tuple[int, int] | None:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return None
        return cursor.selectionStart(), cursor.selectionEnd()

    def clear_selection(self) -> None:
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)

    def set_search(self, matches: list[tuple[int, int]], current: int) -> None:
        self._matches, self._match_index = matches, current
        self._refresh_selections()
        if 0 <= current < len(matches):
            QTimer.singleShot(0, lambda pos=matches[current][0]: self._ensure_visible(pos))

    def clear_search(self) -> None:
        self.set_search([], -1)

    def first_visible_offset(self) -> int:
        return self.cursorForPosition(QPoint(0, 0)).position()

    def _highlight_at(self, pos: QPoint) -> int | None:
        offset = self.cursorForPosition(pos).position()
        for index in range(len(self._user_highlights) - 1, -1, -1):
            start, end, _ = self._user_highlights[index]
            if start <= offset < end:
                return index
        return None

    # ---------- Zoom ----------
    def zoom_by(self, steps: int) -> None:
        size = self.font().pointSizeF() + steps
        self._set_font_size(max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size)))

    def reset_zoom(self) -> None:
        self._set_font_size(BASE_FONT_SIZE)

    def _set_font_size(self, size: float) -> None:
        font = self.font()
        font.setPointSizeF(size)
        self.setFont(font)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = round(event.angleDelta().y() / 120)
            if steps:
                self.zoom_by(steps)
            event.accept()
            return
        super().wheelEvent(event)

    def set_colors(self, highlight, word, hover) -> None:
        self._highlight_color, self._word_color, self._hover_color = highlight, word, hover
        self._refresh_selections()

    def set_current(self, index: int, scroll: bool = True, word: tuple[int, int] | None = None) -> None:
        """``word``: (início, fim) da palavra escolhida com as setas, destacada à parte."""
        self._current = index
        self._word = word
        if self._hover == index:
            self._hover = None
        self._refresh_selections()
        if not scroll or not 0 <= index < len(self._sentences):
            return
        # Adiado: o layout do documento precisa estar calculado para medir a posição.
        if word is not None:
            QTimer.singleShot(0, lambda pos=word[0]: self._ensure_visible(pos))
        else:
            start = self._sentences[index][1]
            QTimer.singleShot(0, lambda pos=start: self._scroll_to_top(pos))

    def _range_selection(self, start: int, end: int, color) -> QTextEdit.ExtraSelection:
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat()
        fmt.setBackground(color)
        selection = QTextEdit.ExtraSelection()
        selection.format = fmt
        selection.cursor = cursor
        return selection

    def _selection(self, index: int, color) -> QTextEdit.ExtraSelection:
        _, start, end = self._sentences[index]
        return self._range_selection(start, end, color)

    def _refresh_selections(self) -> None:
        # Camadas, de baixo para cima: marca-texto, período sob o mouse, período e
        # palavra lidos, resultados da busca. A seleção do mouse fica por cima de tudo.
        selections = []
        for start, end, color_id in self._user_highlights:
            _, rgb = HIGHLIGHT_COLORS.get(color_id, HIGHLIGHT_COLORS[DEFAULT_COLOR])
            selections.append(self._range_selection(start, end, QColor(*rgb, TEXT_HIGHLIGHT_ALPHA)))
        if self._hover is not None and self._hover_color is not None and self._hover < len(self._sentences):
            selections.append(self._selection(self._hover, self._hover_color))
        if 0 <= self._current < len(self._sentences) and self._highlight_color is not None:
            selections.append(self._selection(self._current, self._highlight_color))
            if self._word is not None and self._word_color is not None:
                selections.append(self._range_selection(*self._word, self._word_color))
        if len(self._matches) <= MAX_DRAWN_MATCHES:
            drawn = range(len(self._matches))
        else:
            drawn = [self._match_index] if 0 <= self._match_index < len(self._matches) else []
        for index in drawn:
            color = PDF_SEARCH_CURRENT if index == self._match_index else PDF_SEARCH
            selections.append(self._range_selection(*self._matches[index], color))
        self.setExtraSelections(selections)

    def _ensure_visible(self, char_pos: int) -> None:
        """Rola só o necessário para a posição ficar à vista (sem saltos ao andar por palavra)."""
        cursor = QTextCursor(self.document())
        cursor.setPosition(char_pos)
        rect = self.cursorRect(cursor)
        viewport = self.viewport().rect()
        if rect.top() < SCROLL_TOP_MARGIN or rect.bottom() > viewport.bottom() - SCROLL_TOP_MARGIN:
            self._scroll_to_top(char_pos)

    def _scroll_to_top(self, char_pos: int) -> None:
        cursor = QTextCursor(self.document())
        cursor.setPosition(char_pos)
        rect = self.cursorRect(cursor)
        scrollbar = self.verticalScrollBar()
        new_value = scrollbar.value() + rect.y() - SCROLL_TOP_MARGIN
        scrollbar.setValue(max(scrollbar.minimum(), min(scrollbar.maximum(), new_value)))

    def _sentence_at(self, pos: QPoint) -> int | None:
        if not self._sentences:
            return None
        cursor = self.cursorForPosition(pos)
        caret = self.cursorRect(cursor)
        # cursorForPosition sempre devolve a posição mais próxima; fora do texto
        # (margens, fim de linha, abaixo do último parágrafo) não há período.
        if abs(pos.y() - caret.center().y()) > caret.height() or abs(pos.x() - caret.x()) > 16:
            return None
        offset = cursor.position()
        for candidate in (offset, offset - 1):
            index = sentence_index_at_offset(self._sentences, self._starts, candidate)
            if index is not None:
                return index
        return None

    def _set_hover(self, index: int | None) -> None:
        if index == self._current:
            index = None
        if index == self._hover:
            return
        self._hover = index
        self._refresh_selections()
        cursor = Qt.CursorShape.PointingHandCursor if index is not None else Qt.CursorShape.ArrowCursor
        self.viewport().setCursor(cursor)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if event.buttons() == Qt.MouseButton.NoButton:
            self._set_hover(self._sentence_at(event.position().toPoint()))

    def leaveEvent(self, event):
        self._set_hover(None)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton or self._press_pos is None:
            return
        pos = event.position().toPoint()
        moved = (pos - self._press_pos).manhattanLength()
        self._press_pos = None
        if moved <= CLICK_SLOP_PX and not self.textCursor().hasSelection():
            index = self._sentence_at(pos)
            if index is not None:
                self.sentence_clicked.emit(index)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        has_selection = self.textCursor().hasSelection()
        copy_action = menu.addAction("Copiar")
        copy_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
        copy_action.setEnabled(has_selection)
        copy_action.triggered.connect(self.copy)
        highlight_menu = menu.addMenu("Realçar")
        highlight_menu.setEnabled(has_selection)
        for color_id, (label, rgb) in HIGHLIGHT_COLORS.items():
            action = highlight_menu.addAction(color_icon(rgb), label)
            action.triggered.connect(lambda _checked=False, c=color_id: self.highlight_requested.emit(c))

        hit = self._highlight_at(event.pos())
        if hit is not None:
            start, end, _ = self._user_highlights[hit]
            text = self.toPlainText()[start:end]
            menu.addSeparator()
            menu.addAction("Copiar texto realçado").triggered.connect(lambda: QApplication.clipboard().setText(text))
            menu.addAction("Remover realce").triggered.connect(lambda: self.remove_highlight_requested.emit(hit))

        menu.addSeparator()
        select_all_action = menu.addAction("Selecionar tudo")
        select_all_action.setShortcut(QKeySequence(QKeySequence.StandardKey.SelectAll))
        select_all_action.triggered.connect(self.selectAll)
        menu.exec(event.globalPos())

    def keyPressEvent(self, event):
        key = event.key()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if ctrl and key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_by(1)
        elif ctrl and key == Qt.Key.Key_Minus:
            self.zoom_by(-1)
        elif ctrl and key == Qt.Key.Key_0:
            self.reset_zoom()
        elif key == Qt.Key.Key_Escape and self.textCursor().hasSelection():
            self.clear_selection()
        elif key == Qt.Key.Key_Up:
            self.navigate_prev.emit()
        elif key == Qt.Key.Key_Down:
            self.navigate_next.emit()
        elif key == Qt.Key.Key_Left:
            self.word_prev.emit()
        elif key == Qt.Key.Key_Right:
            self.word_next.emit()
        elif key == Qt.Key.Key_Space:
            self.toggle_play.emit()
        else:
            super().keyPressEvent(event)
            return
        event.accept()


def _strip_range(text: str, start: int, end: int) -> tuple[int, int]:
    """Tira espaços e quebras de linha das pontas de um trecho selecionado."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


# ---------------------------------------------------------------------------
class StartPage(QWidget):
    open_file_requested = pyqtSignal()
    paste_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.addStretch(1)

        column = QVBoxLayout()
        column.setSpacing(12)
        title = QLabel("O que você quer ouvir?")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 8)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(title)
        column.addSpacing(8)

        self.open_btn = QPushButton("📂  Abrir arquivo")
        self.paste_btn = QPushButton("📋  Colar texto")
        for btn in (self.open_btn, self.paste_btn):
            btn.setMinimumSize(280, 52)
            font = btn.font()
            font.setPointSize(font.pointSize() + 3)
            btn.setFont(font)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.clicked.connect(self.open_file_requested)
        self.paste_btn.clicked.connect(self.paste_requested)
        column.addWidget(self.open_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(self.paste_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.hint = QLabel("PDF, DOCX, Markdown ou TXT — ou arraste um arquivo para cá.")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addSpacing(4)
        column.addWidget(self.hint)

        outer.addLayout(column)
        outer.addStretch(2)

    def apply_theme(self, muted_color: str) -> None:
        self.hint.setStyleSheet(f"color: {muted_color};")


class LoadingPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        self.label = QLabel("Carregando documento...")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(320)
        self.progress.setRange(0, 0)
        layout.addWidget(self.label)
        layout.addWidget(self.progress, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(2)

    def set_progress(self, current: int, total: int, message: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(current)
        self.label.setText(message)


# ---------------------------------------------------------------------------
class ReaderTab(QWidget):
    open_file_requested = pyqtSignal()
    paste_requested = pyqtSignal()
    file_dropped = pyqtSignal(str)
    document_loaded = pyqtSignal()
    load_failed = pyqtSignal(str, str)
    status_changed = pyqtSignal(str)
    title_changed = pyqtSignal(str)
    sentence_clicked = pyqtSignal(int)
    navigate_prev = pyqtSignal()
    navigate_next = pyqtSignal()
    word_prev = pyqtSignal()
    word_next = pyqtSignal()
    toggle_play = pyqtSignal()
    sentence_ready = pyqtSignal(int, str)
    sentence_failed = pyqtSignal(int, str)

    def __init__(self, settings: VoiceSettings, gap_ms: int, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.title = NEW_TAB_TITLE
        self.path: str | None = None
        self.kind = "empty"  # "empty", "loading", "text" ou "pdf"
        self.text = ""
        self.sentences: list[tuple[str, int, int]] = []
        self.layout_info: PdfLayout | None = None
        self.current_index = -1
        # Palavra escolhida com ←/→ (início no texto); None = início do período.
        self.word_offset: int | None = None
        self.pipeline: SentencePipeline | None = None
        self.estimator = ReadingTimeEstimator(gap_ms=gap_ms)
        self.status_text = READY_MESSAGE
        self._loader: DocumentLoadThread | None = None
        self._restore_page: int | None = None  # página para reabrir após recarregar o PDF
        self._text_highlights: list[Highlight] = []  # realces de documentos de texto
        self._folded_text: str | None = None  # texto sem acentos/maiúsculas, para a busca
        self._disposed = False
        self.setAcceptDrops(True)
        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.toolbar = QWidget()
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(8, 4, 8, 4)
        self.pdf_mode_btn = QToolButton()
        self.pdf_mode_btn.setText("📄 PDF")
        self.text_mode_btn = QToolButton()
        self.text_mode_btn.setText("📝 Texto")
        self.pdf_mode_btn.setToolTip("Ler sobre as páginas do PDF")
        self.text_mode_btn.setToolTip("Ler o texto extraído do PDF")
        mode_group = QButtonGroup(self)
        for btn in (self.pdf_mode_btn, self.text_mode_btn):
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            mode_group.addButton(btn)
            bar.addWidget(btn)
        self.pdf_mode_btn.setChecked(True)
        self.pdf_mode_btn.clicked.connect(lambda: self._set_view(PAGE_PDF))
        self.text_mode_btn.clicked.connect(lambda: self._set_view(PAGE_TEXT))
        bar.addSpacing(16)

        # Navegação por página.
        self.prev_page_btn = QToolButton()
        self.prev_page_btn.setText("◀")
        self.prev_page_btn.setToolTip("Página anterior")
        self.page_edit = QLineEdit()
        self.page_edit.setFixedWidth(52)
        self.page_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_edit.setValidator(QIntValidator(1, 1, self.page_edit))
        self.page_edit.setToolTip("Digite a página e tecle Enter (Ctrl+G)")
        self.page_total_label = QLabel("de 0")
        self.next_page_btn = QToolButton()
        self.next_page_btn.setText("▶")
        self.next_page_btn.setToolTip("Próxima página")
        self.prev_page_btn.clicked.connect(lambda: self.pdf_view.go_to_page(self.pdf_view.current_page() - 1))
        self.next_page_btn.clicked.connect(lambda: self.pdf_view.go_to_page(self.pdf_view.current_page() + 1))
        self.page_edit.returnPressed.connect(self._go_to_typed_page)
        bar.addWidget(self.prev_page_btn)
        bar.addWidget(self.page_edit)
        bar.addWidget(self.page_total_label)
        bar.addWidget(self.next_page_btn)
        bar.addSpacing(16)

        # Marca-texto, busca, sumário e mais opções.
        self.highlight_btn = QToolButton()
        self.highlight_btn.setText("🖍 Realçar")
        self.highlight_btn.setToolTip("Realçar o texto selecionado (Ctrl+H)")
        self.highlight_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.highlight_btn.setEnabled(False)
        color_menu = QMenu(self.highlight_btn)
        for color_id, (label, rgb) in HIGHLIGHT_COLORS.items():
            action = color_menu.addAction(color_icon(rgb), label)
            action.triggered.connect(lambda _checked=False, c=color_id: self._highlight_selection(c))
        self.highlight_btn.setMenu(color_menu)
        self.highlight_btn.clicked.connect(lambda: self._highlight_selection())
        self.search_btn = QToolButton()
        self.search_btn.setText("🔍 Buscar")
        self.search_btn.setToolTip("Buscar (Ctrl+F)")
        self.search_btn.clicked.connect(self.open_find_bar)
        self.toc_btn = QToolButton()
        self.toc_btn.setText("📑 Sumário")
        self.toc_btn.setToolTip("Ir para um capítulo do PDF")
        self.toc_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.toc_btn.setMenu(QMenu(self.toc_btn))
        self.more_btn = QToolButton()
        self.more_btn.setText("⋯")
        self.more_btn.setToolTip("Mais opções")
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_menu = QMenu(self.more_btn)
        self._pdf_menu_items = []  # itens que só valem para PDF
        for text, slot, pdf_only in (
            ("Ir para a página...  (Ctrl+G)", self._focus_page_edit, True),
            (None, None, True),
            ("Salvar cópia do PDF com os realces...", self._export_with_highlights, True),
            ("Remover todos os realces...", self._clear_all_highlights, False),
            (None, None, True),
            ("Refazer o OCR deste PDF...", self._toggle_force_ocr, True),
        ):
            action = more_menu.addSeparator() if text is None else more_menu.addAction(text)
            if slot is not None:
                action.triggered.connect(lambda _checked=False, s=slot: s())
            if pdf_only:
                self._pdf_menu_items.append(action)
        self._force_ocr_action = action  # o último item: o texto muda conforme o estado
        more_menu.aboutToShow.connect(self._update_more_menu)
        self.more_btn.setMenu(more_menu)
        for btn in (self.highlight_btn, self.search_btn, self.toc_btn, self.more_btn):
            bar.addWidget(btn)
        bar.addStretch(1)

        self.zoom_out_btn = QToolButton()
        self.zoom_out_btn.setText("−")
        self.zoom_out_btn.setToolTip("Diminuir zoom (Ctrl + roda do mouse ou Ctrl -)")
        self.fit_btn = QToolButton()
        self.fit_btn.setText("Ajustar à largura")
        self.fit_btn.setToolTip("Ajustar à largura (Ctrl+0)")
        self.zoom_in_btn = QToolButton()
        self.zoom_in_btn.setText("+")
        self.zoom_in_btn.setToolTip("Aumentar zoom (Ctrl + roda do mouse ou Ctrl +)")
        for btn in (self.zoom_out_btn, self.fit_btn, self.zoom_in_btn):
            bar.addWidget(btn)
        for btn in self.toolbar.findChildren(QToolButton):
            btn.setAutoRaise(True)
        # Só fazem sentido sobre as páginas do PDF (não no modo Texto nem em
        # documentos de texto); realce, busca, mais opções e zoom valem para os dois.
        self._pdf_tools = [
            self.prev_page_btn,
            self.page_edit,
            self.page_total_label,
            self.next_page_btn,
            self.fit_btn,
        ]
        root.addWidget(self.toolbar)
        self.toolbar.hide()

        root.addWidget(self._build_find_bar())

        for shortcut, slot, pdf_only in (
            ("Ctrl+F", self.open_find_bar, False),
            ("Ctrl+G", self._focus_page_edit, True),
            ("Ctrl+H", lambda: self._highlight_selection(), False),
        ):
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            condition = self._is_pdf_view if pdf_only else self._is_reading_view
            action.triggered.connect(lambda _checked=False, s=slot, c=condition: c() and s())
            self.addAction(action)

        self.stack = QStackedWidget()
        self.start_page = StartPage()
        self.start_page.open_file_requested.connect(self.open_file_requested)
        self.start_page.paste_requested.connect(self.paste_requested)
        self.loading_page = LoadingPage()
        self.text_view = ReaderView()
        self.pdf_view = PdfView()
        for view in (self.text_view, self.pdf_view):
            view.navigate_prev.connect(self.navigate_prev)
            view.navigate_next.connect(self.navigate_next)
            view.word_prev.connect(self.word_prev)
            view.word_next.connect(self.word_next)
            view.toggle_play.connect(self.toggle_play)
            view.sentence_clicked.connect(self.sentence_clicked)
        self.pdf_view.page_changed.connect(self._on_page_changed)
        self.pdf_view.selection_changed.connect(lambda has: self._is_pdf_view() and self.highlight_btn.setEnabled(has))
        self.pdf_view.highlights_changed.connect(self._save_highlights)
        self.text_view.selection_changed.connect(
            lambda has: self.stack.currentIndex() == PAGE_TEXT and self.highlight_btn.setEnabled(has)
        )
        self.text_view.highlight_requested.connect(lambda color: self._highlight_selection(color or None))
        self.text_view.remove_highlight_requested.connect(self._remove_highlight)
        self.zoom_out_btn.clicked.connect(lambda: self._zoom(-1))
        self.zoom_in_btn.clicked.connect(lambda: self._zoom(1))
        self.fit_btn.clicked.connect(self.pdf_view.fit_width)
        for page in (self.start_page, self.loading_page, self.text_view, self.pdf_view):
            self.stack.addWidget(page)
        root.addWidget(self.stack, stretch=1)

    def _build_find_bar(self) -> QWidget:
        self.find_bar = QWidget()
        row = QHBoxLayout(self.find_bar)
        row.setContentsMargins(8, 0, 8, 4)
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Buscar (sem diferenciar maiúsculas nem acentos)")
        self.find_edit.setClearButtonEnabled(True)
        self.find_edit.setMaximumWidth(380)
        self.find_edit.installEventFilter(self)
        self.find_prev_btn = QToolButton()
        self.find_prev_btn.setText("↑")
        self.find_prev_btn.setToolTip("Resultado anterior (Shift+Enter)")
        self.find_next_btn = QToolButton()
        self.find_next_btn.setText("↓")
        self.find_next_btn.setToolTip("Próximo resultado (Enter)")
        self.find_count_label = QLabel()
        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setToolTip("Fechar a busca (Esc)")
        for btn in (self.find_prev_btn, self.find_next_btn, close_btn):
            btn.setAutoRaise(True)
        row.addWidget(self.find_edit)
        row.addWidget(self.find_prev_btn)
        row.addWidget(self.find_next_btn)
        row.addWidget(self.find_count_label)
        row.addStretch(1)
        row.addWidget(close_btn)

        self._matches: list[tuple[int, int]] = []
        self._match_index = -1
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._run_search)
        self.find_edit.textChanged.connect(lambda _text: self._search_timer.start())
        self.find_prev_btn.clicked.connect(lambda: self._step_match(-1))
        self.find_next_btn.clicked.connect(lambda: self._step_match(1))
        close_btn.clicked.connect(self.close_find_bar)
        self.find_bar.hide()
        return self.find_bar

    def _is_pdf_view(self) -> bool:
        return self.kind == "pdf" and self.stack.currentIndex() == PAGE_PDF

    def _is_reading_view(self) -> bool:
        """Um documento está na tela (páginas do PDF ou texto)."""
        return self.has_document and self.stack.currentIndex() in (PAGE_PDF, PAGE_TEXT)

    def _set_view(self, page: int) -> None:
        self.stack.setCurrentIndex(page)
        is_pdf_view = page == PAGE_PDF
        for widget in self._pdf_tools:
            widget.setVisible(is_pdf_view)
        for btn in (self.pdf_mode_btn, self.text_mode_btn):
            btn.setVisible(self.kind == "pdf")
        self.toc_btn.setVisible(is_pdf_view and not self.toc_btn.menu().isEmpty())
        has_selection = self.pdf_view.has_selection() if is_pdf_view else self.text_view.selected_range() is not None
        self.highlight_btn.setEnabled(has_selection)
        if self.find_bar.isVisible():
            # A busca continua aberta: mostra os resultados na view nova.
            self.pdf_view.clear_search()
            self.text_view.clear_search()
            self._show_match()
        self.highlight_current(scroll=True)
        self.focus_reader()

    def _zoom(self, steps: int) -> None:
        if self.stack.currentIndex() == PAGE_PDF:
            self.pdf_view.zoom_by(1.2 ** steps)
        else:
            self.text_view.zoom_by(steps)

    # ---------- Páginas ----------
    def _on_page_changed(self, page: int, total: int) -> None:
        self.page_total_label.setText(f"de {total}")
        self.page_edit.validator().setTop(max(total, 1))
        if not self.page_edit.hasFocus():
            self.page_edit.setText(str(page + 1) if total else "")
        self.prev_page_btn.setEnabled(page > 0)
        self.next_page_btn.setEnabled(page < total - 1)

    def _focus_page_edit(self) -> None:
        self.page_edit.setFocus()
        self.page_edit.selectAll()

    def _go_to_typed_page(self) -> None:
        text = self.page_edit.text().strip()
        if text.isdigit():
            self.pdf_view.go_to_page(int(text) - 1)
        else:
            self.page_edit.setText(str(self.pdf_view.current_page() + 1))
        self.pdf_view.setFocus()

    def _fill_toc(self) -> None:
        menu = self.toc_btn.menu()
        menu.clear()
        for level, title, page in self.pdf_view.toc()[:MAX_TOC_ENTRIES]:
            label = "    " * max(level - 1, 0) + (title.strip() or f"Página {page + 1}")
            action = menu.addAction(label)
            action.triggered.connect(lambda _checked=False, p=page: self._go_to_page_from_menu(p))

    def _go_to_page_from_menu(self, page: int) -> None:
        self.pdf_view.go_to_page(page)
        self.pdf_view.setFocus()

    # ---------- Marca-texto ----------
    # Num PDF, os realces são os do PdfView (por palavra) e aparecem também no
    # modo Texto; num documento de texto, ficam em ``_text_highlights`` (por posição).
    def _highlight_selection(self, color: str | None = None) -> None:
        if self._is_pdf_view():
            self.pdf_view.highlight_selection(color)
            return
        selected = self.text_view.selected_range()
        if self.stack.currentIndex() != PAGE_TEXT or selected is None:
            return
        start, end = _strip_range(self.text, *selected)
        color = color or self.pdf_view.highlight_color
        self.pdf_view.highlight_color = color  # a última cor usada vale para os dois modos
        if start >= end:
            return
        if self.kind == "pdf" and self.layout_info is not None:
            words = self.layout_info.words
            first = bisect_right(words.ends, start)  # primeira palavra que termina depois do início
            last = bisect_left(words.starts, end) - 1  # última que começa antes do fim
            if first <= last:
                self.pdf_view.add_highlight(first, last, color)
        else:
            self._text_highlights.append(make_text_highlight(self.text, start, end, color))
            self._save_text_highlights()
        self.text_view.clear_selection()

    def _remove_highlight(self, index: int) -> None:
        """Remove um realce escolhido no modo Texto (mesma ordem da lista exibida)."""
        if self.kind == "pdf":
            self.pdf_view.remove_highlight(index)
        elif 0 <= index < len(self._text_highlights):
            self._text_highlights.pop(index)
            self._save_text_highlights()

    def _all_highlights(self) -> list[Highlight]:
        return self.pdf_view.highlights() if self.kind == "pdf" else list(self._text_highlights)

    def _refresh_text_highlights(self) -> None:
        if self.kind == "pdf" and self.layout_info is not None:
            words = self.layout_info.words
            ranges = [(words.starts[h.first], words.ends[h.last], h.color) for h in self.pdf_view.highlights()]
        else:
            ranges = [(h.first, h.last, h.color) for h in self._text_highlights]
        self.text_view.set_user_highlights(ranges)

    def _save_highlights(self) -> None:
        """Realces do PDF mudaram (em qualquer um dos modos)."""
        self._refresh_text_highlights()
        if self.kind != "pdf" or not self.path:
            return
        try:
            save_highlights(self.path, self.pdf_view.highlights())
        except OSError as exc:
            self.set_status(f"Não foi possível salvar os realces: {exc}")

    def _save_text_highlights(self) -> None:
        self._refresh_text_highlights()
        try:
            save_highlights(document_key(self.path, self.text), self._text_highlights)
        except OSError as exc:
            self.set_status(f"Não foi possível salvar os realces: {exc}")

    def _export_with_highlights(self) -> None:
        if self.kind != "pdf" or not self.path or self.layout_info is None:
            return
        highlights = self.pdf_view.highlights()
        if not highlights:
            QMessageBox.information(
                self, "Sem realces", "Este PDF ainda não tem realces. Selecione um trecho e use 🖍 Realçar."
            )
            return
        base, _ = os.path.splitext(self.path)
        dest, _ = QFileDialog.getSaveFileName(
            self, "Salvar cópia com os realces", f"{base} (realces).pdf", "PDF (*.pdf)"
        )
        if not dest:
            return
        if os.path.normcase(os.path.abspath(dest)) == os.path.normcase(os.path.abspath(self.path)):
            QMessageBox.warning(
                self, "Escolha outro nome", "Salve a cópia com outro nome para não sobrescrever o PDF original."
            )
            return
        try:
            export_with_highlights(self.path, dest, self.layout_info, highlights)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Erro ao salvar", f"Não foi possível salvar a cópia:\n{exc}")
            return
        self.set_status(f"Cópia com realces salva em {dest}")

    def _clear_all_highlights(self) -> None:
        count = len(self._all_highlights())
        if not count:
            return
        answer = QMessageBox.question(self, "Remover realces", f"Remover os {count} realce(s) deste documento?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.kind == "pdf":
            self.pdf_view.clear_highlights()
        else:
            self._text_highlights = []
            self._save_text_highlights()

    # ---------- OCR refeito ----------
    def _is_ocr_forced(self) -> bool:
        return bool(self.path) and app_settings().value(
            _force_ocr_key(self.path), False, type=bool
        )

    def _update_more_menu(self) -> None:
        for action in self._pdf_menu_items:
            action.setVisible(self._is_pdf_view())
        if self._is_ocr_forced():
            self._force_ocr_action.setText("Voltar a usar o texto original do PDF")
        else:
            self._force_ocr_action.setText("Refazer o OCR deste PDF (para livros digitalizados com erros)...")

    def _toggle_force_ocr(self) -> None:
        if self.kind != "pdf" or not self.path:
            return
        forced = self._is_ocr_forced()
        if not forced:
            if not tesseract_available():
                QMessageBox.warning(self, "OCR indisponível", "O Tesseract (OCR) não foi encontrado neste computador.")
                return
            pages = self.pdf_view.page_count
            minutes = max(1, round(pages * OCR_SECONDS_PER_PAGE / default_workers() / 60))
            answer = QMessageBox.question(
                self,
                "Refazer o OCR",
                "O texto das páginas digitalizadas será reconhecido de novo pelo Tesseract, "
                "ignorando o texto que veio no arquivo. Ajuda em livros digitalizados cujo OCR "
                "antigo tem erros (palavras trocadas, símbolos no meio do texto).\n\n"
                f"Neste PDF de {pages} páginas isso leva cerca de {minutes} minuto(s), só na "
                "primeira vez: depois o resultado fica guardado.\n\nContinuar?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        settings = app_settings()
        if forced:
            settings.remove(_force_ocr_key(self.path))
        else:
            settings.setValue(_force_ocr_key(self.path), True)
        # Reabre o mesmo PDF na página em que estava.
        self._restore_page = self.pdf_view.current_page()
        self.load_file(self.path)

    # ---------- Busca ----------
    def open_find_bar(self) -> None:
        if not self._is_reading_view():
            return
        self.find_bar.show()
        self.find_edit.setFocus()
        self.find_edit.selectAll()
        if self.find_edit.text().strip():
            self._run_search()

    def close_find_bar(self) -> None:
        self._search_timer.stop()
        was_visible = self.find_bar.isVisible()
        self.find_bar.hide()
        self._matches, self._match_index = [], -1
        self.pdf_view.clear_search()
        self.text_view.clear_search()
        if was_visible:
            self.focus_reader()

    def _run_search(self) -> None:
        self._search_timer.stop()
        if self._folded_text is None:
            self._folded_text = fold_text(self.text)
        self._matches = find_in_folded(self._folded_text, self.find_edit.text()) if self.has_document else []
        self._match_index = -1
        if self._matches:
            # Começa no primeiro resultado do que está na tela, ou depois dele.
            layout = self.layout_info
            if self._is_pdf_view() and layout is not None:
                first_word = bisect_left(layout.words.pages, max(self.pdf_view.current_page(), 0))
                offset = layout.words.starts[first_word] if first_word < len(layout.words) else 0
            else:
                offset = self.text_view.first_visible_offset()
            self._match_index = next((i for i, (start, _) in enumerate(self._matches) if start >= offset), 0)
        self._show_match()

    def _step_match(self, step: int) -> None:
        if self._search_timer.isActive():
            self._run_search()
            return
        if not self._matches:
            return
        self._match_index = (self._match_index + step) % len(self._matches)
        self._show_match()

    def _show_match(self) -> None:
        if self._is_pdf_view():
            self.pdf_view.set_search(self._matches, self._match_index)
        else:
            self.text_view.set_search(self._matches, self._match_index)
        has_query = bool(self.find_edit.text().strip())
        if self._matches:
            self.find_count_label.setText(f"{self._match_index + 1} de {len(self._matches)}")
        else:
            self.find_count_label.setText("Nenhum resultado" if has_query else "")
        self.find_prev_btn.setEnabled(len(self._matches) > 1)
        self.find_next_btn.setEnabled(len(self._matches) > 1)

    def eventFilter(self, obj, event):
        if obj is self.find_edit and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                shift = event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                self._step_match(-1 if shift else 1)
                return True
            if key == Qt.Key.Key_Escape:
                self.close_find_bar()
                return True
        return super().eventFilter(obj, event)

    def focus_reader(self) -> None:
        page = self.stack.currentIndex()
        if page == PAGE_TEXT:
            self.text_view.setFocus()
        elif page == PAGE_PDF:
            self.pdf_view.setFocus()

    def apply_theme(self, theme) -> None:
        self.text_view.setStyleSheet(theme.reader_stylesheet())
        self.text_view.set_colors(theme.highlight_color, theme.word_highlight_color, theme.hover_color)
        self.pdf_view.set_background(theme.pdf_background_color)
        self.start_page.apply_theme(theme.muted_text_color())

    # ---------- Estado ----------
    @property
    def has_document(self) -> bool:
        return self.kind in ("text", "pdf")

    @property
    def is_loading(self) -> bool:
        return self.kind == "loading"

    @property
    def is_empty(self) -> bool:
        return self.kind == "empty"

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.status_changed.emit(text)

    def _set_title(self, title: str) -> None:
        self.title = title
        self.title_changed.emit(title)

    # ---------- Carregamento ----------
    def load_file(self, path: str) -> None:
        self._cancel_loader()
        self.path = path
        self.kind = "loading"
        self._set_title(os.path.basename(path))
        self.toolbar.hide()
        self.loading_page.label.setText("Carregando documento...")
        self.loading_page.progress.setRange(0, 0)
        self.stack.setCurrentIndex(PAGE_LOADING)
        self.set_status(f"Abrindo {os.path.basename(path)}...")

        loader = DocumentLoadThread(path, pdf_extract_options(path))
        self._loader = loader
        loader.progress.connect(self._on_load_progress)
        loader.loaded.connect(lambda doc, ldr=loader: self._on_loaded(ldr, doc))
        loader.failed.connect(lambda msg, details, ldr=loader: self._on_load_failed(ldr, msg, details))
        _LIVE_LOADERS.add(loader)
        loader.finished.connect(lambda ldr=loader: _LIVE_LOADERS.discard(ldr))
        loader.start()

    def load_text(self, text: str, title: str) -> None:
        self._cancel_loader()
        self.path = None
        self._set_title(title)
        self._apply_document(LoadedDocument("text", text, split_sentences(text)))

    def _cancel_loader(self) -> None:
        if self._loader is not None:
            self._loader.cancel()
            self._loader = None

    def _on_load_progress(self, current: int, total: int, message: str) -> None:
        self.loading_page.set_progress(current, total, message)

    def _on_loaded(self, loader: DocumentLoadThread, doc: LoadedDocument) -> None:
        if loader is not self._loader or self._disposed:
            return
        self._loader = None
        self._apply_document(doc)

    def _on_load_failed(self, loader: DocumentLoadThread, message: str, details: str) -> None:
        if loader is not self._loader or self._disposed:
            return
        self._loader = None
        self.kind = "empty"
        self.path = None
        self._set_title(NEW_TAB_TITLE)
        self.stack.setCurrentIndex(PAGE_START)
        self.set_status(READY_MESSAGE)
        self.load_failed.emit(message, details)

    def _apply_document(self, doc: LoadedDocument) -> None:
        self.kind = doc.kind
        self.text = doc.text
        self.sentences = doc.sentences
        self.layout_info = doc.layout
        self.current_index = 0 if self.sentences else -1
        self.word_offset = None
        self.text_view.set_content(doc.text, doc.sentences)
        self._folded_text = None
        self._text_highlights = []
        self.close_find_bar()
        self.toc_btn.menu().clear()
        restore_page, self._restore_page = self._restore_page, None
        if doc.kind == "pdf" and doc.layout is not None:
            if restore_page is not None:
                self.current_index = next(
                    (i for i, rects in enumerate(doc.layout.sentence_rects) if rects and rects[0][0] >= restore_page),
                    self.current_index,
                )
            self.pdf_view.set_document(self.path, doc.layout)
            self.pdf_view.set_highlights(load_highlights(self.path, doc.layout))
            self._refresh_text_highlights()
            self._fill_toc()
            self.toolbar.show()
            self.pdf_mode_btn.setChecked(True)
            self._set_view(PAGE_PDF)
        else:
            self.pdf_view.close_document()
            self._text_highlights = load_text_highlights(document_key(self.path, doc.text), doc.text)
            self._refresh_text_highlights()
            self.toolbar.show()
            self._set_view(PAGE_TEXT)
        self.document_loaded.emit()

    # ---------- Destaque ----------
    def highlight_current(self, scroll: bool = True) -> None:
        page = self.stack.currentIndex()
        word = None
        if self.word_offset is not None and 0 <= self.current_index < len(self.sentences):
            word = word_span(self.sentences[self.current_index], self.word_offset)
        # A view escondida também é atualizada (sem rolar) para a troca PDF/Texto
        # já abrir no período certo.
        self.text_view.set_current(self.current_index, scroll=scroll and page == PAGE_TEXT, word=word)
        if self.kind == "pdf":
            self.pdf_view.set_current(self.current_index, scroll=scroll and page == PAGE_PDF, word=word)

    @property
    def start_in_sentence(self) -> int:
        """Onde a leitura começa dentro do período atual (0 = do início)."""
        if self.word_offset is None or not 0 <= self.current_index < len(self.sentences):
            return 0
        return self.word_offset - self.sentences[self.current_index][1]

    # ---------- Áudio ----------
    def reset_pipeline(self) -> None:
        if self.pipeline is not None:
            try:
                self.pipeline.sentence_ready.disconnect(self.sentence_ready)
                self.pipeline.sentence_failed.disconnect(self.sentence_failed)
            except TypeError:
                pass
            self.pipeline.shutdown()
            self.pipeline = None

        # Voz, velocidade ou texto novos: as durações medidas até aqui não valem mais.
        self.estimator.reset([len(text.strip()) for text, _, _ in self.sentences], self.settings.rate)

        engine = ENGINES.get(self.settings.engine_id)
        if engine is None or not self.settings.voice_id or not self.sentences:
            return
        pitch = self.settings.pitch if engine.supports_pitch else 0
        self.pipeline = SentencePipeline(engine, self.settings.voice_id, self.settings.rate, pitch)
        self.pipeline.sentence_ready.connect(self.sentence_ready)
        self.pipeline.sentence_failed.connect(self.sentence_failed)

    def ensure_prefetch(self, ahead: int) -> None:
        if self.pipeline is None or self.current_index < 0:
            return
        lo = max(0, self.current_index - 1)
        hi = min(len(self.sentences) - 1, self.current_index + ahead)
        order = [self.current_index] + list(range(self.current_index + 1, hi + 1)) + list(
            range(self.current_index - 1, lo - 1, -1)
        )
        for idx in dict.fromkeys(order):
            self.pipeline.request(idx, self.sentences[idx][0])

    # ---------- Arrastar e soltar ----------
    @staticmethod
    def _dropped_path(event) -> str | None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            path = url.toLocalFile()
            if path and os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS:
                return path
        return None

    def dragEnterEvent(self, event):
        if self._dropped_path(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._dropped_path(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = self._dropped_path(event)
        if path:
            event.acceptProposedAction()
            self.file_dropped.emit(path)

    # ---------- Encerramento ----------
    def shutdown(self) -> None:
        """Libera áudio, cache e PDF. Seguro mesmo com um carregamento em andamento."""
        self._disposed = True
        self._cancel_loader()
        if self.pipeline is not None:
            self.pipeline.shutdown()
            self.pipeline = None
        self.pdf_view.close_document()
