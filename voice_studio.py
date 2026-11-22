"""Tela de vozes personalizadas da Qwen3-TTS: criar por descrição ou clonar de um áudio.

O trabalho pesado roda na thread da Qwen (``qwen_voices.RUNTIME``); o resultado
volta para a interface por um sinal. Cada voz é ouvida antes de ser salva, e as
salvas aparecem com ★ na lista de vozes do motor Qwen3-TTS.
"""
import os
import random
import tempfile

from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from qwen_voices import (
    DESIGN_EXAMPLE,
    LANGUAGES,
    RUNTIME,
    SAMPLE_TEXTS,
    delete_custom_voice,
    list_custom_voices,
    read_audio,
    save_custom_voice,
    write_wav,
)

LANGUAGE_LABELS = {
    "Portuguese": "Português", "English": "Inglês", "Spanish": "Espanhol", "French": "Francês",
    "German": "Alemão", "Italian": "Italiano", "Russian": "Russo", "Japanese": "Japonês",
    "Korean": "Coreano", "Chinese": "Chinês",
}
GENDERS = [("Feminina", "Female"), ("Masculina", "Male"), ("Não informar", "N/A")]
AUDIO_FILTER = "Áudio (*.wav *.flac *.ogg *.mp3 *.m4a);;Todos os arquivos (*)"


class VoiceStudioDialog(QDialog):
    voices_changed = pyqtSignal()
    _job_finished = pyqtSignal(object, object)  # (future, callback)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Vozes personalizadas (Qwen3-TTS)")
        self.resize(720, 640)
        self._busy = False
        self._preview_dir = tempfile.mkdtemp(prefix="localreader_vozes_")
        self._design_result = None  # (amostras, taxa, texto) da última voz criada
        self._clone_reference = None  # (amostras, taxa) do áudio escolhido
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._job_finished.connect(self._on_job_finished)
        self._build_ui()
        self._refresh_voices()

    # ---------- Interface ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        intro = QLabel(
            "Crie vozes com o sotaque e o jeito de ler que você quiser. Elas aparecem com ★ "
            "na lista de vozes do motor Qwen3-TTS. Na primeira vez, cada modo baixa um modelo "
            "(~4 GB) e a primeira geração demora alguns minutos; depois leva segundos."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        saved = QGroupBox("Suas vozes")
        saved_layout = QHBoxLayout(saved)
        self.voice_list = QListWidget()
        self.voice_list.setMaximumHeight(110)
        saved_layout.addWidget(self.voice_list, 1)
        buttons = QVBoxLayout()
        self.play_saved_btn = QPushButton("▶ Ouvir")
        self.delete_saved_btn = QPushButton("Excluir")
        self.play_saved_btn.clicked.connect(self._play_saved)
        self.delete_saved_btn.clicked.connect(self._delete_saved)
        buttons.addWidget(self.play_saved_btn)
        buttons.addWidget(self.delete_saved_btn)
        buttons.addStretch(1)
        saved_layout.addLayout(buttons)
        root.addWidget(saved)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_design_tab(), "Criar por descrição")
        self.tabs.addTab(self._build_clone_tab(), "Clonar de um áudio")
        root.addWidget(self.tabs, 1)

        status_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(140)
        self.progress.hide()
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.progress)
        status_row.addWidget(self.status_label, 1)
        close_btn = QPushButton("Fechar")
        close_btn.clicked.connect(self.close)
        status_row.addWidget(close_btn)
        root.addLayout(status_row)

    def _language_combo(self) -> QComboBox:
        combo = QComboBox()
        for language in LANGUAGES:
            combo.addItem(LANGUAGE_LABELS.get(language, language), language)
        return combo

    def _gender_combo(self) -> QComboBox:
        combo = QComboBox()
        for label, value in GENDERS:
            combo.addItem(label, value)
        return combo

    def _build_design_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.design_name = QLineEdit()
        self.design_name.setPlaceholderText("Ex.: Professora paulista")
        self.design_gender = self._gender_combo()
        self.design_language = self._language_combo()
        self.design_description = QPlainTextEdit(DESIGN_EXAMPLE)
        self.design_description.setToolTip(
            "Descreva quem fala: gênero, idade, região/sotaque, timbre e o jeito de ler. "
            "Pode escrever em português."
        )
        self.design_text = QLineEdit(SAMPLE_TEXTS["Portuguese"])
        self.design_text.setToolTip("O que a voz vai falar na amostra (fica como referência dela).")
        self.design_language.currentIndexChanged.connect(
            lambda: self.design_text.setText(SAMPLE_TEXTS.get(self.design_language.currentData(), self.design_text.text()))
        )
        form.addRow("Nome:", self.design_name)
        form.addRow("Gênero:", self.design_gender)
        form.addRow("Idioma:", self.design_language)
        form.addRow("Descrição da voz:", self.design_description)
        form.addRow("Frase da amostra:", self.design_text)
        row = QHBoxLayout()
        self.design_generate_btn = QPushButton("Gerar amostra")
        self.design_play_btn = QPushButton("▶ Ouvir amostra")
        self.design_save_btn = QPushButton("Salvar esta voz")
        self.design_generate_btn.setToolTip("Cada clique cria uma voz diferente com a mesma descrição.")
        self.design_generate_btn.clicked.connect(self._generate_design)
        self.design_play_btn.clicked.connect(lambda: self._play(self._preview_path("design")))
        self.design_save_btn.clicked.connect(self._save_design)
        for btn in (self.design_generate_btn, self.design_play_btn, self.design_save_btn):
            row.addWidget(btn)
        form.addRow(row)
        self.design_play_btn.setEnabled(False)
        self.design_save_btn.setEnabled(False)
        return tab

    def _build_clone_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.clone_name = QLineEdit()
        self.clone_name.setPlaceholderText("Ex.: Minha voz")
        self.clone_gender = self._gender_combo()
        self.clone_language = self._language_combo()
        file_row = QHBoxLayout()
        self.clone_file = QLineEdit()
        self.clone_file.setReadOnly(True)
        self.clone_file.setPlaceholderText("10 a 20 segundos de fala limpa, sem música nem ruído")
        choose_btn = QPushButton("Escolher...")
        choose_btn.clicked.connect(self._choose_audio)
        file_row.addWidget(self.clone_file, 1)
        file_row.addWidget(choose_btn)
        self.clone_transcript = QPlainTextEdit()
        self.clone_transcript.setPlaceholderText(
            "Escreva exatamente o que é dito no áudio. É opcional, mas melhora muito a imitação "
            "do sotaque e do jeito de falar."
        )
        self.clone_transcript.setMaximumHeight(90)
        self.clone_test = QLineEdit(SAMPLE_TEXTS["Portuguese"])
        form.addRow("Nome:", self.clone_name)
        form.addRow("Gênero:", self.clone_gender)
        form.addRow("Idioma do teste:", self.clone_language)
        form.addRow("Áudio de referência:", file_row)
        form.addRow("O que é dito no áudio:", self.clone_transcript)
        form.addRow("Frase de teste:", self.clone_test)
        row = QHBoxLayout()
        self.clone_generate_btn = QPushButton("Gerar teste com esta voz")
        self.clone_play_btn = QPushButton("▶ Ouvir teste")
        self.clone_save_btn = QPushButton("Salvar esta voz")
        self.clone_generate_btn.clicked.connect(self._generate_clone)
        self.clone_play_btn.clicked.connect(lambda: self._play(self._preview_path("clone")))
        self.clone_save_btn.clicked.connect(self._save_clone)
        for btn in (self.clone_generate_btn, self.clone_play_btn, self.clone_save_btn):
            row.addWidget(btn)
        form.addRow(row)
        self.clone_play_btn.setEnabled(False)
        self.clone_save_btn.setEnabled(False)
        return tab

    # ---------- Vozes salvas ----------
    def _refresh_voices(self):
        self.voice_list.clear()
        for voice in list_custom_voices():
            origin = "criada por descrição" if voice.source == "design" else "clonada de um áudio"
            item = QListWidgetItem(f"★ {voice.name}  ({origin})")
            item.setData(256, voice.id)
            item.setToolTip(voice.description or voice.reference_text)
            self.voice_list.addItem(item)
        has_voices = self.voice_list.count() > 0
        if has_voices:
            self.voice_list.setCurrentRow(0)
        self.play_saved_btn.setEnabled(has_voices)
        self.delete_saved_btn.setEnabled(has_voices)

    def _selected_voice(self):
        item = self.voice_list.currentItem()
        if item is None:
            return None
        voice_id = item.data(256)
        return next((voice for voice in list_custom_voices() if voice.id == voice_id), None)

    def _play_saved(self):
        voice = self._selected_voice()
        if voice is not None:
            self._play(str(voice.reference))

    def _delete_saved(self):
        voice = self._selected_voice()
        if voice is None:
            return
        answer = QMessageBox.question(self, "Excluir voz", f"Excluir a voz “{voice.name}”?")
        if answer == QMessageBox.StandardButton.Yes:
            self._player.stop()
            delete_custom_voice(voice.id)
            self._refresh_voices()
            self.voices_changed.emit()

    # ---------- Criar por descrição ----------
    def _generate_design(self):
        description = self.design_description.toPlainText().strip()
        text = self.design_text.text().strip()
        if not description or not text:
            self._status("Preencha a descrição da voz e a frase da amostra.")
            return
        language = self.design_language.currentData()
        seed = random.randint(1, 2**31 - 1)  # outra voz a cada clique

        def done(result):
            samples, rate = result
            self._design_result = (samples, rate, text)
            write_wav(self._preview_path("design"), samples, rate)
            self.design_play_btn.setEnabled(True)
            self.design_save_btn.setEnabled(True)
            self._status("Amostra pronta. Gostou? Salve a voz; senão, gere outra.")
            self._play(self._preview_path("design"))

        self._start("Criando a voz...", done, RUNTIME.design, text, description, language, seed)

    def _save_design(self):
        if self._design_result is None:
            return
        samples, rate, text = self._design_result
        self._save(
            self.design_name.text(), samples, rate, text, self.design_gender.currentData(),
            description=self.design_description.toPlainText(), source="design",
        )

    # ---------- Clonar de um áudio ----------
    def _choose_audio(self):
        path, _ = QFileDialog.getOpenFileName(self, "Áudio de referência", "", AUDIO_FILTER)
        if not path:
            return
        try:
            samples, rate = read_audio(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Áudio inválido", f"Não foi possível ler o áudio:\n{exc}")
            return
        seconds = len(samples) / rate if rate else 0
        if seconds < 3:
            QMessageBox.warning(self, "Áudio curto demais", "Use pelo menos 3 segundos (o ideal é 10 a 20).")
            return
        self._clone_reference = (samples, rate)
        self.clone_file.setText(path)
        write_wav(self._preview_path("reference"), samples[: rate * 20], rate)
        note = " (só os primeiros 20 s serão usados)" if seconds > 20 else ""
        self._status(f"Áudio de {seconds:.0f} s carregado{note}. Gere um teste para ouvir a voz.")
        self.clone_save_btn.setEnabled(True)

    def _generate_clone(self):
        if self._clone_reference is None:
            self._status("Escolha primeiro o áudio de referência.")
            return
        text = self.clone_test.text().strip()
        if not text:
            self._status("Escreva a frase de teste.")
            return

        def done(result):
            samples, rate = result
            write_wav(self._preview_path("clone"), samples, rate)
            self.clone_play_btn.setEnabled(True)
            self._status("Teste pronto. Se gostou, salve a voz.")
            self._play(self._preview_path("clone"))

        self._start(
            "Lendo a frase com a voz do áudio...", done, RUNTIME.clone, text,
            self.clone_language.currentData(), self._preview_path("reference"), self.clone_transcript.toPlainText(),
        )

    def _save_clone(self):
        if self._clone_reference is None:
            return
        samples, rate = self._clone_reference
        self._save(self.clone_name.text(), samples, rate, self.clone_transcript.toPlainText(),
                   self.clone_gender.currentData(), source="audio")

    # ---------- Comum ----------
    def _save(self, name, samples, rate, reference_text, gender, description="", source="audio"):
        try:
            voice = save_custom_voice(name, samples, rate, reference_text, gender, description, source)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Não foi possível salvar", str(exc))
            return
        self._refresh_voices()
        self.voices_changed.emit()
        self._status(f"Voz “{voice.name}” salva. Escolha-a com ★ no motor Qwen3-TTS.")

    def _preview_path(self, kind: str) -> str:
        return os.path.join(self._preview_dir, f"{kind}.wav")

    def _play(self, path: str):
        if not os.path.exists(path):
            return
        self._player.stop()
        self._player.setSource(QUrl())
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def _status(self, text: str):
        self.status_label.setText(text)

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.progress.setVisible(busy)
        for btn in (self.design_generate_btn, self.clone_generate_btn):
            btn.setEnabled(not busy)

    def _start(self, message, callback, function, *args):
        if self._busy:
            return
        self._set_busy(True)
        self._status(message)
        future = RUNTIME.submit(function, *args)
        # O callback roda na thread da Qwen; o sinal traz o resultado para a interface.
        future.add_done_callback(lambda f: self._emit_finished(f, callback))

    def _emit_finished(self, future, callback):
        try:
            self._job_finished.emit(future, callback)
        except RuntimeError:
            pass  # a janela foi fechada antes do fim

    def _on_job_finished(self, future, callback):
        self._set_busy(False)
        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001
            self._status(f"Falhou: {exc}")
            return
        callback(result)

    def closeEvent(self, event):
        self._player.stop()
        super().closeEvent(event)
