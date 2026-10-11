"""Uma aba de leitura: documento, voz, posição e áudio próprios.

Cada aba guarda tudo o que é dela (texto, períodos, período atual, ajustes de
voz e o pipeline de áudio com seu cache), para que trocar de aba nunca misture
idioma, texto ou preferências. A janela principal só mostra e controla a aba
ativa.
"""
import os
import traceback
from dataclasses import dataclass, replace

from PyQt6.QtCore import QPoint, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from document_loader import load_document
from pdf_layout import LoadCancelled, PdfLayout, extract_pdf_layout, sentence_index_at_offset
from pdf_view import PdfView
from reading_time import ReadingTimeEstimator
from sentence_split import split_sentences
from tts_engines import ENGINES
from tts_pipeline import SentencePipeline

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".md", ".txt")
FILE_DIALOG_FILTER = "Documentos (*.pdf *.docx *.md *.txt);;Todos os arquivos (*)"
SCROLL_TOP_MARGIN = 24
CLICK_SLOP_PX = 5
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
        self._hover: int | None = None
        self._press_pos: QPoint | None = None
        self._highlight_color = None
        self._hover_color = None

    def set_content(self, text: str, sentences: list[tuple[str, int, int]]) -> None:
        self._sentences = sentences
        self._starts = [start for _, start, _ in sentences]
        self._current = -1
        self._hover = None
        self.setPlainText(text)

    def set_colors(self, highlight, hover) -> None:
        self._highlight_color, self._hover_color = highlight, hover
        self._refresh_selections()

    def set_current(self, index: int, scroll: bool = True) -> None:
        self._current = index
        if self._hover == index:
            self._hover = None
        self._refresh_selections()
        if scroll and 0 <= index < len(self._sentences):
            start = self._sentences[index][1]
            # Adiado: o layout do documento precisa estar calculado para medir a posição.
            QTimer.singleShot(0, lambda pos=start: self._scroll_to_top(pos))

    def _selection(self, index: int, color) -> QTextEdit.ExtraSelection:
        _, start, end = self._sentences[index]
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat()
        fmt.setBackground(color)
        selection = QTextEdit.ExtraSelection()
        selection.format = fmt
        selection.cursor = cursor
        return selection

    def _refresh_selections(self) -> None:
        selections = []
        if self._hover is not None and self._hover_color is not None and self._hover < len(self._sentences):
            selections.append(self._selection(self._hover, self._hover_color))
        if 0 <= self._current < len(self._sentences) and self._highlight_color is not None:
            selections.append(self._selection(self._current, self._highlight_color))
        self.setExtraSelections(selections)

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
        bar.addStretch(1)

        self.zoom_out_btn = QToolButton()
        self.zoom_out_btn.setText("−")
        self.zoom_out_btn.setToolTip("Diminuir zoom (Ctrl + roda do mouse)")
        self.fit_btn = QToolButton()
        self.fit_btn.setText("Ajustar à largura")
        self.zoom_in_btn = QToolButton()
        self.zoom_in_btn.setText("+")
        self.zoom_in_btn.setToolTip("Aumentar zoom (Ctrl + roda do mouse)")
        for btn in (self.zoom_out_btn, self.fit_btn, self.zoom_in_btn):
            btn.setAutoRaise(True)
            bar.addWidget(btn)
        root.addWidget(self.toolbar)
        self.toolbar.hide()

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
            view.toggle_play.connect(self.toggle_play)
            view.sentence_clicked.connect(self.sentence_clicked)
        self.zoom_out_btn.clicked.connect(lambda: self.pdf_view.zoom_by(1 / 1.2))
        self.zoom_in_btn.clicked.connect(lambda: self.pdf_view.zoom_by(1.2))
        self.fit_btn.clicked.connect(self.pdf_view.fit_width)
        for page in (self.start_page, self.loading_page, self.text_view, self.pdf_view):
            self.stack.addWidget(page)
        root.addWidget(self.stack, stretch=1)

    def _set_view(self, page: int) -> None:
        self.stack.setCurrentIndex(page)
        is_pdf_view = page == PAGE_PDF
        for btn in (self.zoom_out_btn, self.fit_btn, self.zoom_in_btn):
            btn.setVisible(is_pdf_view)
        self.highlight_current(scroll=True)
        self.focus_reader()

    def focus_reader(self) -> None:
        page = self.stack.currentIndex()
        if page == PAGE_TEXT:
            self.text_view.setFocus()
        elif page == PAGE_PDF:
            self.pdf_view.setFocus()

    def apply_theme(self, theme) -> None:
        self.text_view.setStyleSheet(theme.reader_stylesheet())
        self.text_view.set_colors(theme.highlight_color, theme.hover_color)
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
        self.text_view.set_content(doc.text, doc.sentences)
        if doc.kind == "pdf" and doc.layout is not None:
            self.pdf_view.set_document(self.path, doc.layout)
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
        # A view escondida também é atualizada (sem rolar) para a troca PDF/Texto
        # já abrir no período certo.
        self.text_view.set_current(self.current_index, scroll=scroll and page == PAGE_TEXT)
        if self.kind == "pdf":
            self.pdf_view.set_current(self.current_index, scroll=scroll and page == PAGE_PDF)

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
