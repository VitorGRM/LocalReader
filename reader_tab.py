"""Uma aba de leitura: documento, voz, posição e áudio próprios.

Cada aba guarda tudo o que é dela (texto, períodos, período atual, ajustes de
voz e o pipeline de áudio com seu cache), para que trocar de aba nunca misture
idioma, texto ou preferências. A janela principal só mostra e controla a aba
ativa.
"""
import os
import traceback
from bisect import bisect_left
from dataclasses import dataclass, replace

from PyQt6.QtCore import QEvent, QPoint, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QIntValidator, QKeySequence, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
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
from pdf_annotations import HIGHLIGHT_COLORS, export_with_highlights, load_highlights, save_highlights
from pdf_layout import LoadCancelled, PdfLayout, extract_pdf_layout, sentence_index_at_offset
from pdf_view import PdfView, color_icon
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
NEW_TAB_TITLE = "Nova aba"
READY_MESSAGE = "Abra um arquivo ou cole um texto para começar."

PAGE_START, PAGE_LOADING, PAGE_TEXT, PAGE_PDF = range(4)


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
    progress = pyqtSignal(int, int)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self):
        try:
            if os.path.splitext(self.path)[1].lower() == ".pdf":
                layout = extract_pdf_layout(
                    self.path, progress=self.progress.emit, is_cancelled=lambda: self._cancelled
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
    escolhe o período. Setas para cima/baixo e espaço controlam a leitura."""

    navigate_prev = pyqtSignal()
    navigate_next = pyqtSignal()
    word_prev = pyqtSignal()
    word_next = pyqtSignal()
    toggle_play = pyqtSignal()
    sentence_clicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setCursorWidth(0)
        self.setFont(QFont("Georgia", 14))
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

    def set_content(self, text: str, sentences: list[tuple[str, int, int]]) -> None:
        self._sentences = sentences
        self._starts = [start for _, start, _ in sentences]
        self._current = -1
        self._word = None
        self._hover = None
        self.setPlainText(text)

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
        selections = []
        if self._hover is not None and self._hover_color is not None and self._hover < len(self._sentences):
            selections.append(self._selection(self._hover, self._hover_color))
        if 0 <= self._current < len(self._sentences) and self._highlight_color is not None:
            selections.append(self._selection(self._current, self._highlight_color))
            if self._word is not None and self._word_color is not None:
                selections.append(self._range_selection(*self._word, self._word_color))
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

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Up:
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

    def set_progress(self, current: int, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(current)
        self.label.setText(f"Lendo o PDF: página {current + 1} de {total}...")


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
            action.triggered.connect(lambda _checked=False, c=color_id: self.pdf_view.highlight_selection(c))
        self.highlight_btn.setMenu(color_menu)
        self.highlight_btn.clicked.connect(lambda: self.pdf_view.highlight_selection())
        self.search_btn = QToolButton()
        self.search_btn.setText("🔍 Buscar")
        self.search_btn.setToolTip("Buscar no PDF (Ctrl+F)")
        self.search_btn.clicked.connect(self.open_find_bar)
        self.toc_btn = QToolButton()
        self.toc_btn.setText("📑 Sumário")
        self.toc_btn.setToolTip("Ir para um capítulo do PDF")
        self.toc_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.toc_btn.setMenu(QMenu(self.toc_btn))
        self.more_btn = QToolButton()
        self.more_btn.setText("⋯")
        self.more_btn.setToolTip("Mais opções do PDF")
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_menu = QMenu(self.more_btn)
        for text, slot in (
            ("Ir para a página...  (Ctrl+G)", self._focus_page_edit),
            (None, None),
            ("Salvar cópia do PDF com os realces...", self._export_with_highlights),
            ("Remover todos os realces...", self._clear_all_highlights),
        ):
            if text is None:
                more_menu.addSeparator()
            else:
                more_menu.addAction(text).triggered.connect(lambda _checked=False, s=slot: s())
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
        # Só fazem sentido sobre as páginas do PDF (não no modo Texto).
        self._pdf_tools = [
            self.prev_page_btn,
            self.page_edit,
            self.page_total_label,
            self.next_page_btn,
            self.highlight_btn,
            self.search_btn,
            self.more_btn,
            self.zoom_out_btn,
            self.fit_btn,
            self.zoom_in_btn,
        ]
        root.addWidget(self.toolbar)
        self.toolbar.hide()

        root.addWidget(self._build_find_bar())

        for shortcut, slot in (
            ("Ctrl+F", self.open_find_bar),
            ("Ctrl+G", self._focus_page_edit),
            ("Ctrl+H", lambda: self.pdf_view.highlight_selection()),
        ):
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(lambda _checked=False, s=slot: self._is_pdf_view() and s())
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
        self.pdf_view.selection_changed.connect(self.highlight_btn.setEnabled)
        self.pdf_view.highlights_changed.connect(self._save_highlights)
        self.zoom_out_btn.clicked.connect(lambda: self.pdf_view.zoom_by(1 / 1.2))
        self.zoom_in_btn.clicked.connect(lambda: self.pdf_view.zoom_by(1.2))
        self.fit_btn.clicked.connect(self.pdf_view.fit_width)
        for page in (self.start_page, self.loading_page, self.text_view, self.pdf_view):
            self.stack.addWidget(page)
        root.addWidget(self.stack, stretch=1)

    def _build_find_bar(self) -> QWidget:
        self.find_bar = QWidget()
        row = QHBoxLayout(self.find_bar)
        row.setContentsMargins(8, 0, 8, 4)
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Buscar no PDF (sem diferenciar maiúsculas nem acentos)")
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

    def _set_view(self, page: int) -> None:
        self.stack.setCurrentIndex(page)
        is_pdf_view = page == PAGE_PDF
        for widget in self._pdf_tools:
            widget.setVisible(is_pdf_view)
        self.toc_btn.setVisible(is_pdf_view and not self.toc_btn.menu().isEmpty())
        if not is_pdf_view:
            self.close_find_bar()
        self.highlight_current(scroll=True)
        self.focus_reader()

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
    def _save_highlights(self) -> None:
        if self.kind != "pdf" or not self.path:
            return
        try:
            save_highlights(self.path, self.pdf_view.highlights())
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
        count = len(self.pdf_view.highlights())
        if not count:
            return
        answer = QMessageBox.question(self, "Remover realces", f"Remover os {count} realce(s) deste PDF?")
        if answer == QMessageBox.StandardButton.Yes:
            self.pdf_view.clear_highlights()

    # ---------- Busca ----------
    def open_find_bar(self) -> None:
        if not self._is_pdf_view():
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
        if was_visible:
            self.focus_reader()

    def _run_search(self) -> None:
        self._search_timer.stop()
        query = self.find_edit.text()
        layout = self.layout_info
        self._matches = layout.find(query) if layout is not None and self.kind == "pdf" else []
        self._match_index = -1
        if self._matches and layout is not None:
            # Começa no primeiro resultado da página que está na tela, ou depois dela.
            first_word = bisect_left(layout.words.pages, max(self.pdf_view.current_page(), 0))
            offset = layout.words.starts[first_word] if first_word < len(layout.words) else 0
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
        self.pdf_view.set_search(self._matches, self._match_index)
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

        loader = DocumentLoadThread(path)
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

    def _on_load_progress(self, current: int, total: int) -> None:
        self.loading_page.set_progress(current, total)

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
        self.close_find_bar()
        self.toc_btn.menu().clear()
        if doc.kind == "pdf" and doc.layout is not None:
            self.pdf_view.set_document(self.path, doc.layout)
            self.pdf_view.set_highlights(load_highlights(self.path, doc.layout))
            self._fill_toc()
            self.toolbar.show()
            self.pdf_mode_btn.setChecked(True)
            self._set_view(PAGE_PDF)
        else:
            self.pdf_view.close_document()
            self.toolbar.hide()
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
