"""
TTS Reader - leitor com TTS neural e OCR adaptativo de alta qualidade.

Abra um PDF, DOCX, Markdown ou TXT (ou cole um texto): o app divide o
conteúdo em períodos (sentenças), sintetiza o áudio de cada um sob demanda e
vai pré-carregando os próximos períodos em segundo plano enquanto você ouve
o atual, destacando e rolando o texto até o trecho lido. Use as setas para
cima/baixo (ou os botões) para navegar entre períodos. A segunda aba extrai
texto de PDFs e imagens e pode enviá-lo diretamente para o leitor.
"""
import asyncio
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta

from PyQt6.QtCore import Qt, QObject, QSettings, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QFont, QTextCharFormat, QTextCursor
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
    QSlider,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app_version import GITHUB_REPO, __version__
from audio_keepalive import AudioKeepAlive
from document_loader import load_document
from ocr_widget import OCRWidget
from platform_support import is_frozen, setup_bundled_tools
from reading_time import ReadingTimeEstimator, format_duration
from sentence_split import split_sentences
from theme import MODE_LABELS, MODES, ThemeManager
from tts_engines import ENGINE_ORDER, ENGINES, TTSEngine, VoiceInfo
from tts_pipeline import SentencePipeline
from update_ui import UpdateManager

PREFETCH_AHEAD = 3
UPDATE_CHECK_DELAY_MS = 3000
SENTENCE_END_DELAY_MS = 250
TIME_UPDATE_INTERVAL_S = 0.5
KEEP_AUDIO_KEY = "keep_audio_open"
SIDEBAR_WIDTH = 340
SCROLL_TOP_MARGIN = 24


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


# ---------------------------------------------------------------------------
class ReaderView(QTextEdit):
    """QTextEdit somente-leitura que emite navegação ao invés de mover o
    cursor de edição com as setas para cima/baixo e barra de espaço."""

    navigate_prev = pyqtSignal()
    navigate_next = pyqtSignal()
    toggle_play = pyqtSignal()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Up:
            self.navigate_prev.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Down:
            self.navigate_next.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Space:
            self.toggle_play.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class PasteTextDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Colar texto")
        self.resize(560, 400)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Cole o texto que deseja ler:"))
        self.text_edit = QTextEdit()
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


def parse_locale_name(locale_name: str, locale_code: str) -> tuple[str, str]:
    match = re.match(r"^(?P<lang>.*?)\s*\((?P<country>.*?)\)\s*$", locale_name or "")
    if match:
        return match.group("lang").strip(), match.group("country").strip()
    return locale_name or locale_code, locale_code


class MainWindow(QMainWindow):
    def __init__(self, log_manager: "LogManager", theme: ThemeManager):
        super().__init__()
        self.log_manager = log_manager
        self.theme = theme
        self.log_dialog: LogViewerDialog | None = None
        self.estimator = ReadingTimeEstimator(gap_ms=SENTENCE_END_DELAY_MS)
        self._playing_index: int | None = None  # período cujo áudio está no player
        self._last_time_update = 0.0
        self.settings = QSettings("LocalReader", "TTSReader")
        self._keep_audio_open = self.settings.value(KEEP_AUDIO_KEY, True, type=bool)
        self.audio_keepalive = AudioKeepAlive(self.log_manager.log, self)

        self.setWindowTitle("TTS Reader — voz neural e OCR adaptativo")
        self.resize(1280, 800)

        self.full_text = ""
        self.sentences: list[tuple[str, int, int]] = []
        self.current_index = -1
        self.is_playing_intent = False
        self.waiting_index: int | None = None
        self.catalog: dict[str, dict[str, list[VoiceInfo]]] = {}
        self.pipeline: SentencePipeline | None = None
        self._voice_threads: list[VoiceLoaderThread] = []
        self._engine_voices: dict[str, list[VoiceInfo]] = {}

        self._build_ui()
        self._build_menu()
        self._setup_player()
        self.theme.changed.connect(self._apply_theme)
        self._apply_theme()
        self._on_engine_changed()
        self.audio_keepalive.set_enabled(self._keep_audio_open)

        self.update_manager = UpdateManager(self, self.log_manager.log)
        if is_frozen():
            QTimer.singleShot(UPDATE_CHECK_DELAY_MS, lambda: self.update_manager.check(silent=True))

    # ---------- Menu ----------
    def _build_menu(self):
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
        root_layout.addWidget(self._build_reader_panel(), stretch=1)

        self.ocr_widget = OCRWidget()
        self.ocr_widget.send_to_tts.connect(self._receive_ocr_text)
        self.ocr_widget.log_message.connect(self.log_manager.log)
        self.tabs.addTab(tts_tab, "🔊 Leitor TTS")
        self.tabs.addTab(self.ocr_widget, "🔎 OCR de alta qualidade")

    def _receive_ocr_text(self, text: str):
        self._load_document_text(text)
        self.tabs.setCurrentIndex(0)
        self.setWindowTitle("TTS Reader — Texto reconhecido por OCR")
        self.status_label.setText(
            f"OCR enviado ao leitor: {len(self.sentences)} períodos. Pronto para ler."
        )

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        outer_layout = QVBoxLayout(sidebar)
        outer_layout.setContentsMargins(8, 8, 8, 8)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_content = QWidget()
        layout = QVBoxLayout(scroll_content)

        # Abrir arquivo / colar texto
        self.open_file_btn = QPushButton("📂 Abrir Arquivo (PDF/DOCX/MD/TXT)")
        self.open_file_btn.clicked.connect(self._on_open_file_clicked)
        self.paste_btn = QPushButton("📋 Colar Texto")
        self.paste_btn.clicked.connect(self._on_paste_text_clicked)
        layout.addWidget(self.open_file_btn)
        layout.addWidget(self.paste_btn)

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
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        self.country_combo.currentIndexChanged.connect(self._on_country_changed)
        self.voice_combo.currentIndexChanged.connect(self._on_voice_params_changed)

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
        self.rate_slider.sliderReleased.connect(self._on_voice_params_changed)
        rate_row = QHBoxLayout()
        rate_row.addWidget(self.rate_slider)
        rate_row.addWidget(self.rate_label)
        params_layout.addRow("Velocidade:", rate_row)

        self.pitch_slider = QSlider(Qt.Orientation.Horizontal)
        self.pitch_slider.setRange(-50, 50)
        self.pitch_slider.setValue(0)
        self.pitch_label = QLabel("+0Hz")
        self.pitch_slider.valueChanged.connect(lambda v: self.pitch_label.setText(f"{v:+d}Hz"))
        self.pitch_slider.sliderReleased.connect(self._on_voice_params_changed)
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

        self.status_label = QLabel("Carregando lista de vozes...")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addStretch(1)
        scroll_area.setWidget(scroll_content)
        outer_layout.addWidget(scroll_area, stretch=1)

        self.logs_btn = QPushButton("🪵 Ver Logs")
        self.logs_btn.clicked.connect(self._on_show_logs_clicked)
        outer_layout.addWidget(self.logs_btn)

        return sidebar

    def _build_reader_panel(self) -> QWidget:
        self.text_view = ReaderView()
        self.text_view.setReadOnly(True)
        self.text_view.setCursorWidth(0)
        self.text_view.setFont(QFont("Georgia", 14))
        self.text_view.navigate_prev.connect(self._go_previous)
        self.text_view.navigate_next.connect(self._go_next)
        self.text_view.toggle_play.connect(self._toggle_play_pause)
        return self.text_view

    def _apply_theme(self):
        """Reaplica as cores que não vêm da paleta (leitor, destaque, textos secundários)."""
        self.text_view.setStyleSheet(self.theme.reader_stylesheet())
        self.engine_info_label.setStyleSheet(self.theme.info_label_stylesheet())
        self.ocr_widget.apply_theme(self.theme.muted_text_color())
        self._highlight_current(scroll=False)

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

    # ---------- Engine / voice loading ----------
    def _current_engine(self) -> TTSEngine | None:
        engine_id = self.engine_combo.currentData()
        return ENGINES.get(engine_id) if engine_id else None

    def _on_engine_changed(self):
        engine = self._current_engine()
        if engine is None:
            return
        self.engine_info_label.setText(engine.description)
        self.pitch_slider.setEnabled(engine.supports_pitch)
        self.pitch_label.setEnabled(engine.supports_pitch)

        if engine.id in self._engine_voices:
            self._apply_voice_catalog(self._engine_voices[engine.id])
        else:
            self.status_label.setText(f"Carregando vozes de {engine.display_name}...")
            self._start_voice_loading(engine)
        # _apply_voice_catalog (síncrona ou via callback assíncrono) sempre
        # encadeia até _on_voice_params_changed, que recria o pipeline com o
        # novo motor a partir do período atual e retoma a leitura se estava
        # tocando — não é preciso chamar de novo aqui.

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
        if engine_id != self.engine_combo.currentData():
            return  # usuário já trocou de motor antes desta lista chegar
        self._apply_voice_catalog(voices)
        engine = ENGINES[engine_id]
        self.status_label.setText(
            f"{len(voices)} vozes de {engine.display_name} carregadas. Abra um arquivo ou cole um texto para começar."
        )

    def _apply_voice_catalog(self, voices: list[VoiceInfo]):
        self.catalog = {}
        for v in voices:
            lang, country = parse_locale_name(v.locale_name, v.locale)
            self.catalog.setdefault(lang, {}).setdefault(country, []).append(v)

        self.language_combo.blockSignals(True)
        self.language_combo.clear()
        self.language_combo.addItems(sorted(self.catalog.keys()))
        self.language_combo.blockSignals(False)

        default_index = self.language_combo.findText("Portuguese")
        self.language_combo.setCurrentIndex(max(default_index, 0))
        self._on_language_changed()

    def _on_voices_failed(self, message: str):
        self.status_label.setText("Falha ao carregar vozes. Verifique sua conexão com a internet.")
        self.log_manager.log(f"Falha ao carregar vozes: {message}")
        QMessageBox.critical(self, "Erro ao carregar vozes", message)

    def _on_language_changed(self):
        lang = self.language_combo.currentText()
        countries = sorted(self.catalog.get(lang, {}).keys())
        self.country_combo.blockSignals(True)
        self.country_combo.clear()
        self.country_combo.addItems(countries)
        self.country_combo.blockSignals(False)

        default_index = self.country_combo.findText("Brazil")
        if countries:
            self.country_combo.setCurrentIndex(max(default_index, 0))
        self._on_country_changed()

    def _on_country_changed(self):
        lang = self.language_combo.currentText()
        country = self.country_combo.currentText()
        voice_list = self.catalog.get(lang, {}).get(country, [])

        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        for v in voice_list:
            label = f"{v.id} ({v.gender})" if v.gender != "N/A" else v.id
            self.voice_combo.addItem(label, userData=v.id)
        self.voice_combo.blockSignals(False)
        self._on_voice_params_changed()

    # ---------- File / paste loading ----------
    def _on_open_file_clicked(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Abrir arquivo",
            "",
            "Documentos (*.pdf *.docx *.md *.txt);;Todos os arquivos (*)",
        )
        if not path:
            return
        try:
            text = load_document(path)
        except Exception as exc:  # noqa: BLE001
            self.log_manager.log(f"Falha ao abrir arquivo '{path}': {exc}\n{traceback.format_exc()}")
            QMessageBox.critical(self, "Erro ao abrir arquivo", str(exc))
            return
        if not text.strip():
            QMessageBox.warning(self, "Arquivo vazio", "Não foi possível extrair texto desse arquivo.")
            return
        self._load_document_text(text)
        self.setWindowTitle(f"TTS Reader — {path.rsplit('/', 1)[-1]}")

    def _on_paste_text_clicked(self):
        dialog = PasteTextDialog(self)
        if dialog.exec():
            text = dialog.get_text().strip()
            if text:
                self._load_document_text(text)
                self.setWindowTitle("TTS Reader — Texto colado")

    def _load_document_text(self, text: str):
        self.is_playing_intent = False
        self.waiting_index = None
        self.player.stop()

        self.full_text = text
        self.sentences = split_sentences(text)
        self.text_view.setPlainText(self.full_text)

        self._reset_pipeline()

        has_sentences = bool(self.sentences)
        self.current_index = 0 if has_sentences else -1
        for btn in (self.prev_btn, self.play_pause_btn, self.next_btn, self.restart_btn):
            btn.setEnabled(has_sentences)

        self._highlight_current(scroll=True)
        self._update_progress_label()
        self._update_play_button_label()
        if has_sentences:
            self._ensure_prefetch()
            self.status_label.setText(f"Documento carregado: {len(self.sentences)} períodos. Pronto para ler.")
        else:
            self.status_label.setText("Nenhum período reconhecível foi encontrado no texto.")

    # ---------- Pipeline ----------
    def _reset_pipeline(self):
        if self.pipeline is not None:
            try:
                self.pipeline.sentence_ready.disconnect(self._on_sentence_ready)
                self.pipeline.sentence_failed.disconnect(self._on_sentence_failed)
            except TypeError:
                pass
            self.pipeline.shutdown()

        # Voz, velocidade ou texto novos: as durações medidas até aqui não valem mais.
        self.estimator.reset([len(text.strip()) for text, _, _ in self.sentences], self.rate_slider.value())
        self._playing_index = None
        self._update_time_display()

        engine = self._current_engine()
        voice_id = self.voice_combo.currentData()
        if engine is None or not voice_id:
            self.pipeline = None
            return

        rate_percent = self.rate_slider.value()
        pitch_hz = self.pitch_slider.value() if engine.supports_pitch else 0
        self.pipeline = SentencePipeline(engine, voice_id, rate_percent, pitch_hz)
        self.pipeline.sentence_ready.connect(self._on_sentence_ready)
        self.pipeline.sentence_failed.connect(self._on_sentence_failed)

    def _on_voice_params_changed(self):
        if not self.sentences:
            return
        was_playing = self.is_playing_intent
        self.player.stop()
        self.waiting_index = None
        self._reset_pipeline()
        self._ensure_prefetch()
        if was_playing:
            self._play_current()

    def _ensure_prefetch(self):
        if self.pipeline is None or self.current_index < 0:
            return
        lo = max(0, self.current_index - 1)
        hi = min(len(self.sentences) - 1, self.current_index + PREFETCH_AHEAD)
        order = (
            [self.current_index]
            + list(range(self.current_index + 1, hi + 1))
            + list(range(self.current_index - 1, lo - 1, -1))
        )
        seen = set()
        for idx in order:
            if idx < 0 or idx >= len(self.sentences) or idx in seen:
                continue
            seen.add(idx)
            self.pipeline.request(idx, self.sentences[idx][0])

    def _on_sentence_ready(self, index: int, path: str):
        if index == self.waiting_index:
            self.waiting_index = None
            self._play_path(path)

    def _on_sentence_failed(self, index: int, message: str):
        self.log_manager.log(f"Falha ao gerar período {index + 1}: {message}")
        if index == self.current_index:
            self.waiting_index = None
            self.status_label.setText(f"Falha ao gerar período {index + 1}: {message}")
            self._update_play_button_label()

    # ---------- Playback ----------
    def _play_current(self):
        if self.pipeline is None or self.current_index < 0:
            return
        if self.pipeline.is_cached(self.current_index):
            self._play_path(self.pipeline.path_for(self.current_index))
        else:
            self.waiting_index = self.current_index
            self.status_label.setText(f"Carregando período {self.current_index + 1}...")
            self.pipeline.request(self.current_index, self.sentences[self.current_index][0])
            self._update_play_button_label()

    def _play_path(self, path: str):
        self.player.stop()
        self._playing_index = self.current_index
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self.status_label.setText(f"Lendo período {self.current_index + 1} de {len(self.sentences)}")
        self._update_play_button_label()

    def _record_loaded_duration(self):
        """Guarda a duração real do áudio carregado para refinar a estimativa."""
        index = self._playing_index
        duration = self.player.duration()
        if index is None or duration <= 0:
            return
        loaded_file = os.path.basename(self.player.source().toLocalFile())
        if loaded_file.startswith(f"{index:06d}"):  # garante que o áudio é mesmo deste período
            self.estimator.record_duration(index, duration)
            self._update_time_display()

    def _on_position_changed(self, _position: int):
        if time.monotonic() - self._last_time_update >= TIME_UPDATE_INTERVAL_S:
            self._update_time_display()

    def _update_time_display(self):
        self._last_time_update = time.monotonic()
        if self.current_index < 0 or not self.sentences:
            self.time_label.setText("⏱ Restante: --:--")
            self.eta_label.setText("")
            return
        position = self.player.position() if self._playing_index == self.current_index else 0
        remaining = self.estimator.remaining_ms(self.current_index, position)
        total = self.estimator.total_ms()
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
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.is_playing_intent:
            # O Qt pode avisar o fim antes do áudio terminar de sair pelo alto-falante;
            # trocar de período na hora cortaria o final da frase.
            index = self.current_index
            QTimer.singleShot(SENTENCE_END_DELAY_MS, lambda: self._advance_after_end(index))

    def _advance_after_end(self, finished_index: int):
        if self.is_playing_intent and self.current_index == finished_index:
            self._go_next()

    def _on_player_error(self, error, error_string: str):
        if error != QMediaPlayer.Error.NoError:
            self.status_label.setText(f"Erro de reprodução: {error_string}")
            self.log_manager.log(f"Erro de reprodução (período {self.current_index + 1}): {error_string}")

    def _toggle_play_pause(self):
        if self.current_index < 0:
            return
        if self.is_playing_intent:
            self.is_playing_intent = False
            self.player.pause()
        else:
            self.is_playing_intent = True
            if (
                self.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
                and self.player.source().isValid()
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

    # ---------- Navigation ----------
    def _go_next(self):
        if self.current_index < 0:
            return
        if self.current_index + 1 >= len(self.sentences):
            self.is_playing_intent = False
            self.waiting_index = None
            self.player.stop()
            self.status_label.setText("Fim do documento.")
            self._update_play_button_label()
            return
        self.current_index += 1
        self.waiting_index = None
        self._highlight_current(scroll=True)
        self._update_progress_label()
        self._ensure_prefetch()
        if self.is_playing_intent:
            self._play_current()
        else:
            self.player.stop()

    def _go_previous(self):
        if self.current_index <= 0:
            return
        self.current_index -= 1
        self.waiting_index = None
        self._highlight_current(scroll=True)
        self._update_progress_label()
        self._ensure_prefetch()
        if self.is_playing_intent:
            self._play_current()
        else:
            self.player.stop()

    def _on_restart(self):
        if not self.sentences:
            return
        self.current_index = 0
        self.waiting_index = None
        self._highlight_current(scroll=True)
        self._update_progress_label()
        self._ensure_prefetch()
        if self.is_playing_intent:
            self._play_current()
        else:
            self.player.stop()

    # ---------- Highlight / auto-scroll (estilo teleprompter) ----------
    def _highlight_current(self, scroll: bool = True):
        if self.current_index < 0 or self.current_index >= len(self.sentences):
            self.text_view.setExtraSelections([])
            return

        _, start, end = self.sentences[self.current_index]

        cursor = self.text_view.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)

        fmt = QTextCharFormat()
        fmt.setBackground(self.theme.highlight_color)
        selection = QTextEdit.ExtraSelection()
        selection.format = fmt
        selection.cursor = cursor
        self.text_view.setExtraSelections([selection])

        if scroll:
            # Adiado para o próximo tick: garante que o layout do documento
            # já esteja calculado antes de medirmos a posição do cursor.
            QTimer.singleShot(0, lambda pos=start: self._scroll_sentence_to_top(pos))

    def _scroll_sentence_to_top(self, char_pos: int):
        cursor = QTextCursor(self.text_view.document())
        cursor.setPosition(char_pos)
        rect = self.text_view.cursorRect(cursor)
        scrollbar = self.text_view.verticalScrollBar()
        new_value = scrollbar.value() + rect.y() - SCROLL_TOP_MARGIN
        new_value = max(scrollbar.minimum(), min(scrollbar.maximum(), new_value))
        scrollbar.setValue(new_value)

    def _update_progress_label(self):
        total = len(self.sentences)
        idx = self.current_index + 1 if self.current_index >= 0 else 0
        self.progress_bar.setMaximum(max(total, 1))
        self.progress_bar.setValue(idx)
        self.position_label.setText(f"Período {idx} / {total}")
        self._update_time_display()

    def closeEvent(self, event):
        self.is_playing_intent = False
        self.update_manager.shutdown()
        self.audio_keepalive.set_enabled(False)
        self.ocr_widget.shutdown()
        if self.pipeline is not None:
            self.pipeline.shutdown()
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
