"""Tela para instalar (ou remover) o ambiente da Qwen3-TTS sob demanda."""
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from qwen_setup import DOWNLOAD_ESTIMATE_GB, QwenSetup, SetupCancelled, nvidia_gpu
from qwen_voices import runtime_dir, runtime_installed


class _SetupThread(QThread):
    line = pyqtSignal(str)
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, action: str, parent=None):
        super().__init__(parent)
        self.action = action
        self._cancelled = False
        self.setup = QwenSetup(self.line.emit, lambda: self._cancelled)

    def cancel(self):
        self._cancelled = True
        self.setup.cancel()

    def run(self):
        try:
            if self.action == "install":
                self.setup.install()
            else:
                self.setup.uninstall()
            self.finished_ok.emit()
        except SetupCancelled:
            self.failed.emit("Instalação cancelada.")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class QwenSetupDialog(QDialog):
    changed = pyqtSignal(bool)  # instalado ou não

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Vozes Qwen3-TTS")
        self.resize(640, 480)
        self._thread: _SetupThread | None = None
        layout = QVBoxLayout(self)
        self.info = QLabel()
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        layout.addWidget(self.log, 1)
        row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        row.addWidget(self.progress, 1)
        self.install_btn = QPushButton("Instalar")
        self.remove_btn = QPushButton("Remover")
        self.cancel_btn = QPushButton("Cancelar")
        self.close_btn = QPushButton("Fechar")
        self.install_btn.clicked.connect(lambda: self._start("install"))
        self.remove_btn.clicked.connect(self._remove)
        self.cancel_btn.clicked.connect(self._cancel)
        self.close_btn.clicked.connect(self.close)
        for btn in (self.install_btn, self.remove_btn, self.cancel_btn, self.close_btn):
            row.addWidget(btn)
        layout.addLayout(row)
        self._gpu = nvidia_gpu()
        self._refresh()

    def _refresh(self):
        installed = runtime_installed()
        busy = self._thread is not None
        if installed:
            text = (
                "As vozes Qwen3-TTS estão instaladas: escolha o motor “Qwen3-TTS 1.7B (local)” e crie "
                "vozes em Áudio → Vozes personalizadas. Na primeira vez que cada modelo for usado, ele é "
                f"baixado (~4 GB).<br><br>Pasta: {runtime_dir()}"
            )
        elif self._gpu:
            text = (
                f"Placa de vídeo encontrada: <b>{self._gpu}</b>.<br><br>A Qwen3-TTS lê com voz neural muito "
                "natural e permite criar vozes por descrição ou clonando um áudio. Para isso o LocalReader "
                f"instala um ambiente próprio com o PyTorch: cerca de {DOWNLOAD_ESTIMATE_GB:.1f} GB de download "
                "agora (~6 GB em disco) e, depois, ~4 GB por modelo no primeiro uso. Leva de 5 a 20 minutos, "
                "conforme a internet; pode cancelar quando quiser."
            )
        else:
            text = (
                "Não encontrei uma placa de vídeo NVIDIA com o driver instalado (o comando nvidia-smi não "
                "respondeu). A Qwen3-TTS precisa de uma, com ~5 GB de memória de vídeo; sem ela, use as "
                "outras vozes (Edge, Piper, Proxecto Nós)."
            )
        self.info.setText(text)
        self.install_btn.setVisible(not installed)
        self.install_btn.setEnabled(not busy and bool(self._gpu))
        self.remove_btn.setVisible(installed)
        self.remove_btn.setEnabled(not busy)
        self.cancel_btn.setVisible(busy)
        self.close_btn.setEnabled(not busy)
        self.progress.setVisible(busy)

    def _remove(self):
        answer = QMessageBox.question(
            self, "Remover a Qwen3-TTS",
            "Remover o ambiente da Qwen3-TTS (PyTorch e pacotes)? Suas vozes personalizadas são mantidas.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start("uninstall")

    def _start(self, action: str):
        if self._thread is not None:
            return
        self.log.clear()
        self._thread = _SetupThread(action, self)
        self._thread.line.connect(self.log.appendPlainText)
        self._thread.finished_ok.connect(self._on_finished)
        self._thread.failed.connect(self._on_failed)
        self._thread.start()
        self._refresh()

    def _cancel(self):
        if self._thread is not None:
            self.log.appendPlainText("Cancelando...")
            self._thread.cancel()

    def _done(self):
        if self._thread is not None:
            self._thread.wait()
            self._thread = None
        self._refresh()
        self.changed.emit(runtime_installed())

    def _on_finished(self):
        self._done()
        if runtime_installed():
            QMessageBox.information(self, "Qwen3-TTS instalada", "Pronto! Escolha o motor “Qwen3-TTS 1.7B (local)”.")

    def _on_failed(self, message: str):
        self.log.appendPlainText(f"\nNão foi possível concluir: {message}")
        self._done()

    def closeEvent(self, event):
        if self._thread is not None:
            event.ignore()  # espera terminar ou cancelar
            return
        super().closeEvent(event)
