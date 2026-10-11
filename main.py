"""
TTS Reader - leitor com TTS neural e OCR adaptativo de alta qualidade.

Cada documento abre numa aba própria (como num navegador), com texto, voz,
velocidade e posição de leitura independentes. O app divide o conteúdo em
períodos (sentenças), sintetiza o áudio de cada um sob demanda e vai
pré-carregando os próximos em segundo plano enquanto você ouve o atual,
destacando o trecho lido — sobre a própria página, no caso de PDFs. Passe o
mouse sobre um período e clique para pular direto para ele; as setas para
cima/baixo também navegam. A segunda aba extrai texto de PDFs e imagens por
OCR e pode enviá-lo para o leitor.
"""
import asyncio
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta

from PyQt6.QtCore import Qt, QObject, QSettings, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QFont, QKeySequence, QTextCursor
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app_version import GITHUB_REPO, __version__
from audio_keepalive import AudioKeepAlive
from ocr_widget import OCRWidget
from platform_support import is_frozen, setup_bundled_tools
from reader_tab import (
    FILE_DIALOG_FILTER,
    READY_MESSAGE,
    ReaderTab,
    VoiceSettings,
    wait_for_loaders,
)
from reading_time import format_duration
from theme import MODE_LABELS, MODES, ThemeManager
from tts_engines import ENGINE_ORDER, ENGINES, TTSEngine, VoiceInfo
from update_ui import UpdateManager

PREFETCH_AHEAD = 3
UPDATE_CHECK_DELAY_MS = 3000
SENTENCE_END_DELAY_MS = 250
TIME_UPDATE_INTERVAL_S = 0.5
KEEP_AUDIO_KEY = "keep_audio_open"
LAST_DIR_KEY = "last_open_dir"
VOICE_SETTINGS_PREFIX = "voice/"
SIDEBAR_WIDTH = 340
DEFAULT_LANGUAGE = "Portuguese"
DEFAULT_COUNTRY = "Brazil"


# ---------------------------------------------------------------------------
# Log capture: redireciona stdout/stderr para um buffer visível na UI, e
# instala um excepthook para que erros não tratados também apareçam lá.
# ---------------------------------------------------------------------------
class LogManager(QObject):
    message = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.history: list[str] = []
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = self
        sys.stderr = self

    def write(self, text: str) -> None:
        if not text:
            return
        self.history.append(text)
        self.message.emit(text)
        try:
            self._orig_stderr.write(text)
        except Exception:
            pass

    def flush(self) -> None:
        try:
            self._orig_stderr.flush()
        except Exception:
            pass

    def full_text(self) -> str:
        return "".join(self.history)

    def log(self, message: str) -> None:
        self.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")


def install_excepthook(log_manager: "LogManager") -> None:
    def _hook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log_manager.log(f"ERRO NÃO TRATADO:\n{text}")

    sys.excepthook = _hook


class LogViewerDialog(QDialog):
    def __init__(self, log_manager: "LogManager", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Logs do Programa")
        self.resize(720, 480)
        layout = QVBoxLayout(self)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setFont(QFont("Monospace", 10))
        self.text_edit.setPlainText(log_manager.full_text())
        layout.addWidget(self.text_edit)

        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Copiar Tudo")
        copy_btn.clicked.connect(self._copy_all)
        clear_btn = QPushButton("Limpar")
        clear_btn.clicked.connect(self.text_edit.clear)
        close_btn = QPushButton("Fechar")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        log_manager.message.connect(self._append)

    def _append(self, text: str) -> None:
        self.text_edit.moveCursor(QTextCursor.MoveOperation.End)
        self.text_edit.insertPlainText(text)
        self.text_edit.moveCursor(QTextCursor.MoveOperation.End)

    def _copy_all(self) -> None:
        QApplication.clipboard().setText(self.text_edit.toPlainText())


class PasteTextDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Colar texto")
        self.resize(560, 400)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Cole o texto que deseja ler:"))
        self.text_edit = QTextEdit()
        self.text_edit.setAcceptRichText(False)
        layout.addWidget(self.text_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_text(self) -> str:
        return self.text_edit.toPlainText()


class VoiceLoaderThread(QThread):
    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, engine: TTSEngine, parent=None):
        super().__init__(parent)
        self.engine = engine

    def run(self):
        try:
            voices = asyncio.run(self.engine.list_voices())
            self.loaded.emit(voices)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


def summarize_error(message: str, engine_name: str = "", limit: int = 160) -> str:
    """Resume um erro de síntese em uma frase curta para o rótulo de status.

    As exceções de rede trazem URLs enormes e sem pontos de quebra; num QLabel
    com quebra de linha isso alarga a barra lateral inteira e corta os botões.
    """
    if "403" in message and "Invalid response status" in message:
        prefix = f"{engine_name}: " if engine_name else ""
        return f"{prefix}o serviço de voz recusou o acesso (erro 403)."
    first_line = " ".join(message.split())
    return first_line if len(first_line) <= limit else first_line[: limit - 1] + "…"


def parse_locale_name(locale_name: str, locale_code: str) -> tuple[str, str]:
    match = re.match(r"^(?P<lang>.*?)\s*\((?P<country>.*?)\)\s*$", locale_name or "")
    if match:
        return match.group("lang").strip(), match.group("country").strip()
    return locale_name or locale_code, locale_code


def _select_text(combo: QComboBox, preferred: list[str]) -> None:
    for text in preferred:
        index = combo.findText(text) if text else -1
        if index >= 0:
            combo.setCurrentIndex(index)
            return
    if combo.count():
        combo.setCurrentIndex(0)


class MainWindow(QMainWindow):
    def __init__(self, log_manager: "LogManager", theme: ThemeManager):
        super().__init__()
        self.log_manager = log_manager
        self.theme = theme
        self.log_dialog: LogViewerDialog | None = None
        self._last_time_update = 0.0
        self.settings = QSettings("LocalReader", "TTSReader")
        self._keep_audio_open = self.settings.value(KEEP_AUDIO_KEY, True, type=bool)
        self.audio_keepalive = AudioKeepAlive(self.log_manager.log, self)

        self.setWindowTitle("TTS Reader")
        self.resize(1280, 800)

        # Só a aba ativa toca; trocar de aba pausa a leitura da anterior.
        self._active_tab: ReaderTab | None = None
        self.is_playing_intent = False
        self.waiting_index: int | None = None
        self._playing_tab: ReaderTab | None = None  # aba e período cujo áudio está no player
        self._playing_index: int | None = None
        self._syncing_controls = False

        self.catalog: dict[str, dict[str, list[VoiceInfo]]] = {}
        self._voice_threads: list[VoiceLoaderThread] = []
        self._engine_voices: dict[str, list[VoiceInfo]] = {}
        self._engine_failure_notified: set[str] = set()

        self._build_ui()
        self._build_menu()
        self._setup_player()
        self.theme.changed.connect(self._apply_theme)
        self._new_tab(self._saved_voice_settings())
        self._apply_theme()
        self.audio_keepalive.set_enabled(self._keep_audio_open)

        self.update_manager = UpdateManager(self, self.log_manager.log)
        if is_frozen():
            QTimer.singleShot(UPDATE_CHECK_DELAY_MS, lambda: self.update_manager.check(silent=True))

    # ---------- Menu ----------
    def _build_menu(self):
        file_menu = self.menuBar().addMenu("&Arquivo")
        for text, shortcut, slot in (
            ("&Nova aba", "Ctrl+T", lambda: self._new_tab()),
            ("&Abrir arquivo...", "Ctrl+O", lambda: self._open_file_dialog(self._active_tab)),
            ("&Colar texto...", "Ctrl+Shift+V", lambda: self._paste_into(self._active_tab)),
            ("&Fechar aba", "Ctrl+W", lambda: self._close_tab(self.doc_tabs.currentIndex())),
        ):
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
            file_menu.addAction(action)
        file_menu.addSeparator()
        for text, shortcut, step in (("Próxima aba", "Ctrl+Tab", 1), ("Aba anterior", "Ctrl+Shift+Tab", -1)):
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(lambda _checked=False, s=step: self._cycle_tab(s))
            file_menu.addAction(action)

        view_menu = self.menuBar().addMenu("&Exibir")
        theme_menu = view_menu.addMenu("&Tema")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for mode in MODES:
            action = QAction(MODE_LABELS[mode], self)
            action.setCheckable(True)
            action.setChecked(mode == self.theme.mode)
            action.triggered.connect(lambda _checked, m=mode: self.theme.set_mode(m))
            theme_group.addAction(action)
            theme_menu.addAction(action)

        audio_menu = self.menuBar().addMenu("Á&udio")
        keepalive_action = QAction("Manter saída de áudio ativa (evita som e corte entre períodos)", self)
        keepalive_action.setCheckable(True)
        keepalive_action.setChecked(self._keep_audio_open)
        keepalive_action.toggled.connect(self._set_keep_audio_open)
        audio_menu.addAction(keepalive_action)

        help_menu = self.menuBar().addMenu("&Ajuda")
        check_action = QAction("Verificar &atualizações...", self)
        check_action.triggered.connect(lambda: self.update_manager.check(silent=False))
        about_action = QAction("&Sobre", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(check_action)
        help_menu.addSeparator()
        help_menu.addAction(about_action)

    def _set_keep_audio_open(self, enabled: bool):
        self._keep_audio_open = enabled
        self.settings.setValue(KEEP_AUDIO_KEY, enabled)
        self.audio_keepalive.set_enabled(enabled)

    def _show_about(self):
        repo_line = f"\nRepositório: github.com/{GITHUB_REPO}" if GITHUB_REPO else ""
        QMessageBox.about(
            self,
            "Sobre o TTS Reader",
            f"TTS Reader + OCR\nVersão {__version__}{repo_line}",
        )

    # ---------- UI ----------
    def _build_ui(self):
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.setCentralWidget(self.tabs)

        tts_tab = QWidget()
        root_layout = QHBoxLayout(tts_tab)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_sidebar())
        root_layout.addWidget(self._build_document_tabs(), stretch=1)

        self.ocr_widget = OCRWidget()
        self.ocr_widget.send_to_tts.connect(self._receive_ocr_text)
        self.ocr_widget.log_message.connect(self.log_manager.log)
        self.tabs.addTab(tts_tab, "🔊 Leitor TTS")
        self.tabs.addTab(self.ocr_widget, "🔎 OCR de alta qualidade")

    def _build_document_tabs(self) -> QWidget:
        self.doc_tabs = QTabWidget()
        self.doc_tabs.setTabsClosable(True)
        self.doc_tabs.setMovable(True)
        self.doc_tabs.setDocumentMode(True)
        self.doc_tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.doc_tabs.setUsesScrollButtons(True)
        self.doc_tabs.tabBar().setStyleSheet("QTabBar::tab { max-width: 240px; }")
        self.doc_tabs.tabCloseRequested.connect(self._close_tab)
        self.doc_tabs.currentChanged.connect(self._on_tab_changed)

        new_tab_btn = QToolButton()
        new_tab_btn.setText("+")
        new_tab_btn.setToolTip("Nova aba (Ctrl+T)")
        new_tab_btn.setAutoRaise(True)
        font = new_tab_btn.font()
        font.setPointSize(font.pointSize() + 4)
        new_tab_btn.setFont(font)
        new_tab_btn.clicked.connect(lambda: self._new_tab())
        self.doc_tabs.setCornerWidget(new_tab_btn, Qt.Corner.TopRightCorner)
        return self.doc_tabs

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        outer_layout = QVBoxLayout(sidebar)
        outer_layout.setContentsMargins(8, 8, 8, 8)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_content = QWidget()
        layout = QVBoxLayout(scroll_content)

        hint = QLabel("Os ajustes abaixo valem para a aba aberta.")
        hint.setWordWrap(True)
        self.sidebar_hint = hint
        layout.addWidget(hint)

        # Voz
        voice_group = QGroupBox("Voz")
        voice_layout = QVBoxLayout(voice_group)

        voice_layout.addWidget(QLabel("Motor de voz (TTS):"))
        self.engine_combo = QComboBox()
        for engine_id in ENGINE_ORDER:
            engine = ENGINES[engine_id]
            suffix = "" if engine.requires_internet else " — offline"
            self.engine_combo.addItem(f"{engine.display_name}{suffix}", userData=engine_id)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        voice_layout.addWidget(self.engine_combo)

        self.engine_info_label = QLabel("")
        self.engine_info_label.setWordWrap(True)
        voice_layout.addWidget(self.engine_info_label)
        voice_layout.addSpacing(6)

        self.language_combo = QComboBox()
        self.country_combo = QComboBox()
        self.voice_combo = QComboBox()
        for combo in (self.engine_combo, self.language_combo, self.country_combo, self.voice_combo):
            # Nomes de voz longos não podem alargar a barra lateral.
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        self.country_combo.currentIndexChanged.connect(self._on_country_changed)
        self.voice_combo.currentIndexChanged.connect(self._on_controls_changed)

        voice_layout.addWidget(QLabel("Idioma:"))
        voice_layout.addWidget(self.language_combo)
        voice_layout.addWidget(QLabel("País/Região:"))
        voice_layout.addWidget(self.country_combo)
        voice_layout.addWidget(QLabel("Voz:"))
        voice_layout.addWidget(self.voice_combo)
        layout.addWidget(voice_group)

        # Ajustes
        params_group = QGroupBox("Ajustes")
        params_layout = QFormLayout(params_group)

        self.rate_slider = QSlider(Qt.Orientation.Horizontal)
        self.rate_slider.setRange(-50, 100)
        self.rate_slider.setValue(0)
        self.rate_label = QLabel("+0%")
        self.rate_slider.valueChanged.connect(lambda v: self.rate_label.setText(f"{v:+d}%"))
        self.rate_slider.sliderReleased.connect(self._on_controls_changed)
        rate_row = QHBoxLayout()
        rate_row.addWidget(self.rate_slider)
        rate_row.addWidget(self.rate_label)
        params_layout.addRow("Velocidade:", rate_row)

        self.pitch_slider = QSlider(Qt.Orientation.Horizontal)
        self.pitch_slider.setRange(-50, 50)
        self.pitch_slider.setValue(0)
        self.pitch_label = QLabel("+0Hz")
        self.pitch_slider.valueChanged.connect(lambda v: self.pitch_label.setText(f"{v:+d}Hz"))
        self.pitch_slider.sliderReleased.connect(self._on_controls_changed)
        pitch_row = QHBoxLayout()
        pitch_row.addWidget(self.pitch_slider)
        pitch_row.addWidget(self.pitch_label)
        params_layout.addRow("Tom (pitch):", pitch_row)

        layout.addWidget(params_group)

        # Player
        player_group = QGroupBox("Leitura")
        player_layout = QVBoxLayout(player_group)

        self.play_pause_btn = QPushButton("▶ Play")
        self.play_pause_btn.clicked.connect(self._toggle_play_pause)
        player_layout.addWidget(self.play_pause_btn)

        nav_row = QHBoxLayout()
        self.prev_btn = QPushButton("⏮ Anterior (↑)")
        self.prev_btn.clicked.connect(self._go_previous)
        self.next_btn = QPushButton("Próximo (↓) ⏭")
        self.next_btn.clicked.connect(self._go_next)
        nav_row.addWidget(self.prev_btn)
        nav_row.addWidget(self.next_btn)
        player_layout.addLayout(nav_row)

        self.restart_btn = QPushButton("🔁 Reiniciar")
        self.restart_btn.clicked.connect(self._on_restart)
        player_layout.addWidget(self.restart_btn)

        for btn in (self.prev_btn, self.play_pause_btn, self.next_btn, self.restart_btn):
            btn.setEnabled(False)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(1)
        self.progress_bar.setValue(0)
        self.position_label = QLabel("Período 0 / 0")
        self.position_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_label = QLabel("⏱ Restante: --:--")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_label.setToolTip(
            "Estimativa a partir do tamanho do texto; ela se ajusta à velocidade real "
            "da voz conforme a leitura avança."
        )
        self.eta_label = QLabel("")
        self.eta_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        player_layout.addWidget(self.progress_bar)
        player_layout.addWidget(self.position_label)
        player_layout.addWidget(self.time_label)
        player_layout.addWidget(self.eta_label)

        layout.addWidget(player_group)

        self.status_label = QLabel(READY_MESSAGE)
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.status_label)

        layout.addStretch(1)
        scroll_area.setWidget(scroll_content)
        outer_layout.addWidget(scroll_area, stretch=1)

        self.logs_btn = QPushButton("🪵 Ver Logs")
        self.logs_btn.clicked.connect(self._on_show_logs_clicked)
        outer_layout.addWidget(self.logs_btn)

        return sidebar

    def _apply_theme(self):
        """Reaplica as cores que não vêm da paleta (leitor, destaque, textos secundários)."""
        self.engine_info_label.setStyleSheet(self.theme.info_label_stylesheet())
        self.sidebar_hint.setStyleSheet(f"color: {self.theme.muted_text_color()};")
        self.ocr_widget.apply_theme(self.theme.muted_text_color())
        for tab in self._all_tabs():
            tab.apply_theme(self.theme)
            tab.highlight_current(scroll=False)

    def _setup_player(self):
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.errorOccurred.connect(self._on_player_error)

    # ---------- Logs ----------
    def _on_show_logs_clicked(self):
        if self.log_dialog is None:
            self.log_dialog = LogViewerDialog(self.log_manager, self)
        self.log_dialog.show()
        self.log_dialog.raise_()
        self.log_dialog.activateWindow()

    # ---------- Abas ----------
    def _all_tabs(self) -> list[ReaderTab]:
        return [self.doc_tabs.widget(i) for i in range(self.doc_tabs.count())]

    def _new_tab(self, settings: VoiceSettings | None = None, activate: bool = True) -> ReaderTab:
        if settings is None:
            # Nova aba herda a voz da aba atual; a partir daí, cada uma segue a sua.
            settings = self._active_tab.settings.copy() if self._active_tab else self._saved_voice_settings()
        tab = ReaderTab(settings, gap_ms=SENTENCE_END_DELAY_MS)
        tab.apply_theme(self.theme)

        tab.open_file_requested.connect(lambda: self._open_file_dialog(tab))
        tab.paste_requested.connect(lambda: self._paste_into(tab))
        tab.file_dropped.connect(lambda path: self._open_path(path, tab))
        tab.document_loaded.connect(lambda: self._on_document_loaded(tab))
        tab.load_failed.connect(lambda msg, details: self._on_load_failed(tab, msg, details))
        tab.status_changed.connect(lambda text: self._on_tab_status(tab, text))
        tab.title_changed.connect(lambda _title: self._update_tab_title(tab))
        tab.sentence_clicked.connect(lambda index: self._on_sentence_clicked(tab, index))
        tab.navigate_prev.connect(lambda: tab is self._active_tab and self._go_previous())
        tab.navigate_next.connect(lambda: tab is self._active_tab and self._go_next())
        tab.toggle_play.connect(lambda: tab is self._active_tab and self._toggle_play_pause())
        tab.sentence_ready.connect(lambda index, path: self._on_sentence_ready(tab, index, path))
        tab.sentence_failed.connect(lambda index, message: self._on_sentence_failed(tab, index, message))

        index = self.doc_tabs.addTab(tab, tab.title)
        if activate:
            self.doc_tabs.setCurrentIndex(index)
        return tab

    def _cycle_tab(self, step: int):
        count = self.doc_tabs.count()
        if count > 1:
            self.doc_tabs.setCurrentIndex((self.doc_tabs.currentIndex() + step) % count)

    def _close_tab(self, index: int):
        tab = self.doc_tabs.widget(index)
        if tab is None:
            return
        if tab is self._active_tab or tab is self._playing_tab:
            self._stop_playback()
        if tab is self._playing_tab:
            self._playing_tab = None
        self.doc_tabs.removeTab(index)
        tab.shutdown()
        tab.deleteLater()
        if self.doc_tabs.count() == 0:
            self._new_tab()

    def _on_tab_changed(self, index: int):
        tab = self.doc_tabs.widget(index) if index >= 0 else None
        if tab is self._active_tab:
            return
        self._stop_playback()
        self._active_tab = tab
        if tab is None:
            return
        self._sync_controls_from_tab(tab)
        self._refresh_reader_controls()
        tab.focus_reader()

    def _update_tab_title(self, tab: ReaderTab):
        index = self.doc_tabs.indexOf(tab)
        if index < 0:
            return
        self.doc_tabs.setTabText(index, tab.title)
        self.doc_tabs.setTabToolTip(index, tab.path or tab.title)
        if tab is self._active_tab:
            self.setWindowTitle(f"TTS Reader — {tab.title}")

    def _on_tab_status(self, tab: ReaderTab, text: str):
        if tab is self._active_tab:
            self.status_label.setText(text)

    def _refresh_reader_controls(self):
        """Mostra no menu lateral o estado da aba ativa."""
        tab = self._active_tab
        if tab is None:
            return
        has_sentences = bool(tab.sentences)
        for btn in (self.prev_btn, self.play_pause_btn, self.next_btn, self.restart_btn):
            btn.setEnabled(has_sentences)
        self.status_label.setText(tab.status_text)
        self.setWindowTitle(f"TTS Reader — {tab.title}")
        self._update_progress_label()
        self._update_play_button_label()

    # ---------- Abrir / colar ----------
    def _target_tab(self, tab: ReaderTab | None) -> ReaderTab:
        """A própria aba, se estiver vazia; senão o documento abre numa aba nova."""
        if tab is not None and tab.is_empty:
            return tab
        return self._new_tab()

    def _open_file_dialog(self, tab: ReaderTab | None):
        start_dir = str(self.settings.value(LAST_DIR_KEY, ""))
        path, _ = QFileDialog.getOpenFileName(self, "Abrir arquivo", start_dir, FILE_DIALOG_FILTER)
        if not path:
            return
        self.settings.setValue(LAST_DIR_KEY, os.path.dirname(path))
        self._open_path(path, tab)

    def _open_path(self, path: str, tab: ReaderTab | None):
        target = self._target_tab(tab)
        self.doc_tabs.setCurrentWidget(target)
        target.load_file(path)

    def _paste_into(self, tab: ReaderTab | None):
        dialog = PasteTextDialog(self)
        if not dialog.exec():
            return
        text = dialog.get_text().strip()
        if text:
            target = self._target_tab(tab)
            self.doc_tabs.setCurrentWidget(target)
            target.load_text(text, "Texto colado")

    def _receive_ocr_text(self, text: str):
        target = self._target_tab(self._active_tab)
        self.doc_tabs.setCurrentWidget(target)
        target.load_text(text, "Texto do OCR")
        self.tabs.setCurrentIndex(0)

    def _on_document_loaded(self, tab: ReaderTab):
        if tab is self._active_tab:
            self._stop_playback()
        tab.reset_pipeline()
        tab.ensure_prefetch(PREFETCH_AHEAD)
        if tab.sentences:
            tab.set_status(f"Documento carregado: {len(tab.sentences)} períodos. Pronto para ler.")
        elif tab.kind == "pdf":
            tab.set_status(
                "Este PDF não tem texto selecionável (provavelmente é digitalizado). "
                "Use a aba OCR para reconhecer o texto e enviá-lo ao leitor."
            )
        else:
            tab.set_status("Nenhum período reconhecível foi encontrado no texto.")
        if tab is self._active_tab:
            self._refresh_reader_controls()

    def _on_load_failed(self, tab: ReaderTab, message: str, details: str):
        self.log_manager.log(f"Falha ao abrir arquivo: {message}\n{details}")
        QMessageBox.critical(self, "Erro ao abrir arquivo", summarize_error(message, limit=400))
        if tab is self._active_tab:
            self._refresh_reader_controls()

    # ---------- Configurações de voz (por aba) ----------
    def _saved_voice_settings(self) -> VoiceSettings:
        s, p = self.settings, VOICE_SETTINGS_PREFIX
        engine_id = str(s.value(p + "engine", ENGINE_ORDER[0] if ENGINE_ORDER else ""))
        if engine_id not in ENGINES and ENGINE_ORDER:
            engine_id = ENGINE_ORDER[0]
        return VoiceSettings(
            engine_id=engine_id,
            language=str(s.value(p + "language", DEFAULT_LANGUAGE)),
            country=str(s.value(p + "country", DEFAULT_COUNTRY)),
            voice_id=str(s.value(p + "voice_id", "")),
            rate=s.value(p + "rate", 0, type=int),
            pitch=s.value(p + "pitch", 0, type=int),
        )

    def _save_voice_settings(self, settings: VoiceSettings) -> None:
        """A última escolha vira o padrão das próximas sessões."""
        p = VOICE_SETTINGS_PREFIX
        self.settings.setValue(p + "engine", settings.engine_id)
        self.settings.setValue(p + "language", settings.language)
        self.settings.setValue(p + "country", settings.country)
        self.settings.setValue(p + "voice_id", settings.voice_id)
        self.settings.setValue(p + "rate", settings.rate)
        self.settings.setValue(p + "pitch", settings.pitch)

    def _current_engine(self) -> TTSEngine | None:
        engine_id = self.engine_combo.currentData()
        return ENGINES.get(engine_id) if engine_id else None

    def _sync_controls_from_tab(self, tab: ReaderTab):
        """Coloca os controles do menu lateral nos valores da aba."""
        self._syncing_controls = True
        try:
            index = self.engine_combo.findData(tab.settings.engine_id)
            self.engine_combo.setCurrentIndex(max(index, 0))
            self.rate_slider.setValue(tab.settings.rate)
            self.pitch_slider.setValue(tab.settings.pitch)
            self._show_engine_info()
            self._fill_voice_combos(tab)
        finally:
            self._syncing_controls = False
        # Ajusta a aba se algo salvo não existe mais (ex.: voz removida do catálogo).
        self._on_controls_changed()

    def _show_engine_info(self):
        engine = self._current_engine()
        if engine is None:
            return
        self.engine_info_label.setText(engine.description)
        self.pitch_slider.setEnabled(engine.supports_pitch)
        self.pitch_label.setEnabled(engine.supports_pitch)

    def _fill_voice_combos(self, tab: ReaderTab):
        """Preenche idioma/país/voz com o catálogo do motor, preferindo o que a aba usa."""
        engine = self._current_engine()
        combos = (self.language_combo, self.country_combo, self.voice_combo)
        for combo in combos:
            combo.blockSignals(True)
        try:
            for combo in combos:
                combo.clear()
            if engine is None:
                return
            if engine.id not in self._engine_voices:
                if not any(t.engine.id == engine.id for t in self._voice_threads):
                    self._start_voice_loading(engine)
                self.status_label.setText(f"Carregando vozes de {engine.display_name}...")
                return
            self.catalog = {}
            for v in self._engine_voices[engine.id]:
                lang, country = parse_locale_name(v.locale_name, v.locale)
                self.catalog.setdefault(lang, {}).setdefault(country, []).append(v)
            self.language_combo.addItems(sorted(self.catalog.keys()))
            _select_text(self.language_combo, [tab.settings.language, DEFAULT_LANGUAGE])
            self._fill_countries(tab)
        finally:
            for combo in combos:
                combo.blockSignals(False)

    def _fill_countries(self, tab: ReaderTab):
        lang = self.language_combo.currentText()
        self.country_combo.blockSignals(True)
        self.country_combo.clear()
        self.country_combo.addItems(sorted(self.catalog.get(lang, {}).keys()))
        _select_text(self.country_combo, [tab.settings.country, DEFAULT_COUNTRY])
        self.country_combo.blockSignals(False)
        self._fill_voices(tab)

    def _fill_voices(self, tab: ReaderTab):
        lang = self.language_combo.currentText()
        country = self.country_combo.currentText()
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        for v in self.catalog.get(lang, {}).get(country, []):
            label = f"{v.id} ({v.gender})" if v.gender != "N/A" else v.id
            self.voice_combo.addItem(label, userData=v.id)
        index = self.voice_combo.findData(tab.settings.voice_id)
        if self.voice_combo.count():
            self.voice_combo.setCurrentIndex(max(index, 0))
        self.voice_combo.blockSignals(False)

    def _on_engine_changed(self):
        if self._syncing_controls or self._active_tab is None:
            return
        self._show_engine_info()
        self._fill_voice_combos(self._active_tab)
        # _fill_voice_combos pode ter disparado o carregamento assíncrono das vozes;
        # quando ele terminar, _on_voices_loaded aplica a escolha nesta aba.
        self._on_controls_changed()

    def _on_language_changed(self):
        if self._syncing_controls or self._active_tab is None:
            return
        self._fill_countries(self._active_tab)
        self._on_controls_changed()

    def _on_country_changed(self):
        if self._syncing_controls or self._active_tab is None:
            return
        self._fill_voices(self._active_tab)
        self._on_controls_changed()

    def _on_controls_changed(self):
        """Grava os controles na aba ativa e recria o áudio dela se algo mudou."""
        tab = self._active_tab
        if self._syncing_controls or tab is None:
            return
        engine_id = self.engine_combo.currentData() or ""
        new = VoiceSettings(
            engine_id=engine_id,
            rate=self.rate_slider.value(),
            pitch=self.pitch_slider.value(),
        )
        if self.voice_combo.count():
            new.language = self.language_combo.currentText()
            new.country = self.country_combo.currentText()
            new.voice_id = self.voice_combo.currentData() or ""
        else:
            # Catálogo ainda carregando: mantém a escolha da aba até as vozes chegarem.
            new.language, new.country = tab.settings.language, tab.settings.country
            new.voice_id = tab.settings.voice_id if engine_id == tab.settings.engine_id else ""
        if new == tab.settings and (tab.pipeline is not None or not tab.sentences or not new.voice_id):
            return
        tab.settings = new
        if new.voice_id:
            self._save_voice_settings(new)
        if not tab.has_document:
            return
        was_playing = self.is_playing_intent
        self.player.stop()
        self.waiting_index = None
        self._playing_index = None
        tab.reset_pipeline()
        tab.ensure_prefetch(PREFETCH_AHEAD)
        self._update_time_display()
        if was_playing and tab.pipeline is not None:
            self._play_current()
        else:
            self._update_play_button_label()

    # ---------- Carregamento das vozes ----------
    def _start_voice_loading(self, engine: TTSEngine):
        thread = VoiceLoaderThread(engine)
        self._voice_threads.append(thread)

        def _cleanup():
            if thread in self._voice_threads:
                self._voice_threads.remove(thread)

        thread.finished.connect(_cleanup)
        thread.loaded.connect(lambda voices, eid=engine.id: self._on_voices_loaded(eid, voices))
        thread.failed.connect(self._on_voices_failed)
        thread.start()

    def _on_voices_loaded(self, engine_id: str, voices: list[VoiceInfo]):
        self._engine_voices[engine_id] = voices
        tab = self._active_tab
        if tab is None or engine_id != self.engine_combo.currentData():
            return  # usuário já trocou de motor/aba antes desta lista chegar
        self._syncing_controls = True
        try:
            self._fill_voice_combos(tab)
        finally:
            self._syncing_controls = False
        self._on_controls_changed()
        if not tab.has_document:
            engine = ENGINES[engine_id]
            tab.set_status(f"{len(voices)} vozes de {engine.display_name} carregadas. {READY_MESSAGE}")
        else:
            self.status_label.setText(tab.status_text)

    def _on_voices_failed(self, message: str):
        self.status_label.setText("Falha ao carregar vozes. Verifique sua conexão com a internet.")
        self.log_manager.log(f"Falha ao carregar vozes: {message}")
        QMessageBox.critical(self, "Erro ao carregar vozes", summarize_error(message, limit=400))

    # ---------- Áudio gerado ----------
    def _on_sentence_ready(self, tab: ReaderTab, index: int, path: str):
        if tab is self._active_tab and index == self.waiting_index:
            self.waiting_index = None
            self._play_path(tab, path)

    def _on_sentence_failed(self, tab: ReaderTab, index: int, message: str):
        self.log_manager.log(f"[{tab.title}] Falha ao gerar período {index + 1}: {message}")
        if index != tab.current_index:
            return
        engine = ENGINES.get(tab.settings.engine_id)
        engine_name = engine.display_name if engine else ""
        tab.set_status(f"Falha ao gerar período {index + 1}. {summarize_error(message, engine_name)}")
        if tab is not self._active_tab:
            return
        was_waiting = self.waiting_index == index
        self.waiting_index = None
        self._update_play_button_label()
        # Só interrompe com um aviso se o usuário estava esperando o áudio.
        if was_waiting and engine is not None:
            self._offer_engine_switch(engine, message)

    def _offer_engine_switch(self, failed_engine: TTSEngine, message: str):
        """Explica a falha do motor atual e oferece trocar por outro disponível."""
        if failed_engine.id in self._engine_failure_notified:
            return
        self._engine_failure_notified.add(failed_engine.id)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Não foi possível gerar o áudio")
        box.setText(f"O motor \"{failed_engine.display_name}\" não conseguiu gerar o áudio.")
        box.setInformativeText(
            f"{summarize_error(message, limit=240)}\n\n"
            "Isso costuma acontecer quando o serviço online muda e deixa de aceitar o "
            "acesso. Você pode continuar lendo com outro motor de voz."
        )
        buttons: dict[object, str] = {}
        for engine_id, label in (
            ("piper", "Usar Piper (offline)"),
            ("gtts", "Usar Google (gTTS)"),
        ):
            if engine_id != failed_engine.id and engine_id in ENGINES:
                buttons[box.addButton(label, QMessageBox.ButtonRole.AcceptRole)] = engine_id
        box.addButton("Agora não", QMessageBox.ButtonRole.RejectRole)
        box.exec()

        target_id = buttons.get(box.clickedButton())
        if target_id is None:
            return
        self.is_playing_intent = True  # retoma a leitura sozinho após a troca
        self.engine_combo.setCurrentIndex(self.engine_combo.findData(target_id))

    # ---------- Playback ----------
    def _stop_playback(self):
        self.is_playing_intent = False
        self.waiting_index = None
        self.player.stop()
        self._playing_index = None
        self._update_play_button_label()

    def _play_current(self):
        tab = self._active_tab
        if tab is None or tab.pipeline is None or tab.current_index < 0:
            return
        if tab.pipeline.is_cached(tab.current_index):
            self._play_path(tab, tab.pipeline.path_for(tab.current_index))
        else:
            self.waiting_index = tab.current_index
            tab.set_status(f"Carregando período {tab.current_index + 1}...")
            tab.pipeline.request(tab.current_index, tab.sentences[tab.current_index][0])
            self._update_play_button_label()

    def _play_path(self, tab: ReaderTab, path: str):
        self.player.stop()
        self._playing_tab = tab
        self._playing_index = tab.current_index
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        tab.set_status(f"Lendo período {tab.current_index + 1} de {len(tab.sentences)}")
        self._update_play_button_label()

    def _record_loaded_duration(self):
        """Guarda a duração real do áudio carregado para refinar a estimativa."""
        tab, index = self._playing_tab, self._playing_index
        duration = self.player.duration()
        if tab is None or tab is not self._active_tab or tab.pipeline is None:
            return
        if index is None or duration <= 0:
            return
        source = os.path.normcase(os.path.normpath(self.player.source().toLocalFile()))
        expected = os.path.normcase(os.path.normpath(tab.pipeline.path_for(index)))
        if source == expected:  # garante que o áudio é mesmo deste período desta aba
            tab.estimator.record_duration(index, duration)
            self._update_time_display()

    def _on_position_changed(self, _position: int):
        if time.monotonic() - self._last_time_update >= TIME_UPDATE_INTERVAL_S:
            self._update_time_display()

    def _update_time_display(self):
        self._last_time_update = time.monotonic()
        tab = self._active_tab
        if tab is None or tab.current_index < 0 or not tab.sentences:
            self.time_label.setText("⏱ Restante: --:--")
            self.eta_label.setText("")
            return
        playing_here = self._playing_tab is tab and self._playing_index == tab.current_index
        position = self.player.position() if playing_here else 0
        remaining = tab.estimator.remaining_ms(tab.current_index, position)
        total = tab.estimator.total_ms()
        self.time_label.setText(
            f"⏱ Restante: {format_duration(remaining)} (de ~{format_duration(total)})"
        )
        if self.is_playing_intent:
            now = datetime.now()
            end = now + timedelta(milliseconds=remaining)
            pattern = "%H:%M" if end.date() == now.date() else "%d/%m %H:%M"
            self.eta_label.setText(f"Término previsto: {end.strftime(pattern)}")
        else:
            self.eta_label.setText("Término previsto: ao iniciar a leitura")

    def _on_media_status_changed(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self._record_loaded_duration()
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.is_playing_intent and self._active_tab:
            # O Qt pode avisar o fim antes do áudio terminar de sair pelo alto-falante;
            # trocar de período na hora cortaria o final da frase.
            tab, index = self._active_tab, self._active_tab.current_index
            QTimer.singleShot(SENTENCE_END_DELAY_MS, lambda: self._advance_after_end(tab, index))

    def _advance_after_end(self, tab: ReaderTab, finished_index: int):
        if self.is_playing_intent and tab is self._active_tab and tab.current_index == finished_index:
            self._go_next()

    def _on_player_error(self, error, error_string: str):
        if error != QMediaPlayer.Error.NoError and self._active_tab is not None:
            self._active_tab.set_status(f"Erro de reprodução: {error_string}")
            self.log_manager.log(
                f"Erro de reprodução (período {self._active_tab.current_index + 1}): {error_string}"
            )

    def _toggle_play_pause(self):
        tab = self._active_tab
        if tab is None or tab.current_index < 0:
            return
        if self.is_playing_intent:
            self.is_playing_intent = False
            self.player.pause()
        else:
            self.is_playing_intent = True
            if (
                self.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
                and self.player.source().isValid()
                and self._playing_tab is tab
                and self._playing_index == tab.current_index
            ):
                self.player.play()
            else:
                self._play_current()
        self._update_play_button_label()

    def _update_play_button_label(self):
        if self.waiting_index is not None:
            self.play_pause_btn.setText("⏳ Carregando...")
        elif self.is_playing_intent:
            self.play_pause_btn.setText("⏸ Pause")
        else:
            self.play_pause_btn.setText("▶ Play")
        self._update_time_display()

    # ---------- Navegação ----------
    def _jump_to(self, index: int, scroll: bool = True):
        tab = self._active_tab
        if tab is None or not 0 <= index < len(tab.sentences):
            return
        tab.current_index = index
        self.waiting_index = None
        tab.highlight_current(scroll=scroll)
        self._update_progress_label()
        tab.ensure_prefetch(PREFETCH_AHEAD)
        if self.is_playing_intent:
            self._play_current()
        else:
            self.player.stop()
            self._playing_index = None
            self._update_play_button_label()

    def _go_next(self):
        tab = self._active_tab
        if tab is None or tab.current_index < 0:
            return
        if tab.current_index + 1 >= len(tab.sentences):
            self._stop_playback()
            tab.set_status("Fim do documento.")
            return
        self._jump_to(tab.current_index + 1)

    def _go_previous(self):
        tab = self._active_tab
        if tab is not None and tab.current_index > 0:
            self._jump_to(tab.current_index - 1)

    def _on_restart(self):
        self._jump_to(0)

    def _on_sentence_clicked(self, tab: ReaderTab, index: int):
        # O período clicado já está à vista: não rola a tela debaixo do mouse.
        if tab is self._active_tab:
            self._jump_to(index, scroll=False)

    def _update_progress_label(self):
        tab = self._active_tab
        total = len(tab.sentences) if tab else 0
        idx = tab.current_index + 1 if tab and tab.current_index >= 0 else 0
        self.progress_bar.setMaximum(max(total, 1))
        self.progress_bar.setValue(idx)
        self.position_label.setText(f"Período {idx} / {total}")
        self._update_time_display()

    def closeEvent(self, event):
        self.is_playing_intent = False
        self.player.stop()
        self.update_manager.shutdown()
        self.audio_keepalive.set_enabled(False)
        self.ocr_widget.shutdown()
        for tab in self._all_tabs():
            tab.shutdown()
        wait_for_loaders(3000)
        for thread in list(self._voice_threads):
            if thread.isRunning():
                thread.wait(2000)
        super().closeEvent(event)


def main():
    tools_dir = setup_bundled_tools()
    app = QApplication(sys.argv)
    theme = ThemeManager(app)
    log_manager = LogManager()
    install_excepthook(log_manager)
    log_manager.log(f"TTS Reader {__version__} — Tesseract: {tools_dir or 'usando o PATH do sistema'}")
    window = MainWindow(log_manager, theme)
    window.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
