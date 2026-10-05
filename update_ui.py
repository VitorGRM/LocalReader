"""Interface Qt do atualizador: verificação em segundo plano, diálogos e instalação."""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QObject, Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QMessageBox, QProgressDialog, QWidget

import app_version
import updater
from platform_support import is_frozen

NOTES_PREVIEW_CHARS = 900


class CheckThread(QThread):
    found = pyqtSignal(object)
    up_to_date = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, repo: str, parent=None):
        super().__init__(parent)
        self.repo = repo

    def run(self):
        try:
            info = updater.check_for_update(self.repo, app_version.__version__)
        except updater.UpdateError as exc:
            self.failed.emit(str(exc))
            return
        if info is None:
            self.up_to_date.emit()
        else:
            self.found.emit(info)


class DownloadThread(QThread):
    progress = pyqtSignal(int)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, info: updater.UpdateInfo, parent=None):
        super().__init__(parent)
        self.info = info
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        last = -1

        def on_progress(received: int, total: int):
            nonlocal last
            percent = int(received * 100 / total) if total else 0
            if percent != last:
                last = percent
                self.progress.emit(percent)

        try:
            path = updater.download_installer(self.info, on_progress, lambda: self._cancel)
        except updater.UpdateCancelled:
            return
        except updater.UpdateError as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(path)


class UpdateManager(QObject):
    def __init__(self, window: QWidget, log: Callable[[str], None]):
        super().__init__(window)
        self.window = window
        self._log = log
        self._silent = True
        self._check_thread: CheckThread | None = None
        self._download_thread: DownloadThread | None = None
        self._progress: QProgressDialog | None = None

    # ---------- verificação ----------
    def check(self, silent: bool) -> None:
        """silent=True (verificação automática) só interrompe o usuário se houver versão nova."""
        if self._check_thread is not None or self._download_thread is not None:
            if not silent:
                QMessageBox.information(self.window, "Atualizações", "Já existe uma atualização em andamento.")
            return
        repo = updater.configured_repo()
        if not repo:
            if not silent:
                QMessageBox.information(
                    self.window,
                    "Atualizações",
                    "O repositório de atualizações não está configurado.\n"
                    "Defina GITHUB_REPO em app_version.py ou gere o app pelo build_windows.ps1.",
                )
            return
        self._silent = silent
        thread = CheckThread(repo, self)
        thread.found.connect(self._on_found)
        thread.up_to_date.connect(self._on_up_to_date)
        thread.failed.connect(self._on_check_failed)
        thread.finished.connect(self._on_check_finished)
        self._check_thread = thread
        thread.start()

    def _on_check_finished(self):
        thread, self._check_thread = self._check_thread, None
        if thread is not None:
            thread.deleteLater()

    def _on_up_to_date(self):
        self._log(f"Atualizações: versão {app_version.__version__} é a mais recente.")
        if not self._silent:
            QMessageBox.information(
                self.window, "Atualizações", f"Você já está na versão mais recente ({app_version.__version__})."
            )

    def _on_check_failed(self, message: str):
        self._log(f"Atualizações: {message}")
        if not self._silent:
            QMessageBox.warning(self.window, "Atualizações", message)

    def _on_found(self, info: updater.UpdateInfo):
        self._log(f"Atualizações: versão {info.version} disponível.")
        notes = info.notes
        if len(notes) > NOTES_PREVIEW_CHARS:
            notes = notes[:NOTES_PREVIEW_CHARS].rstrip() + "…"
        text = f"A versão {info.version} está disponível (você usa a {app_version.__version__})."
        if notes:
            text += f"\n\nNovidades:\n{notes}"

        if is_frozen() and info.installable:
            text += "\n\nBaixar e instalar agora? O TTS Reader será fechado e reaberto automaticamente."
            answer = QMessageBox.question(self.window, "Atualização disponível", text)
            if answer == QMessageBox.StandardButton.Yes:
                self._start_download(info)
        else:
            text += "\n\nAbrir a página da release para baixar?"
            answer = QMessageBox.question(self.window, "Atualização disponível", text)
            if answer == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl(info.page_url))

    # ---------- download e instalação ----------
    def _start_download(self, info: updater.UpdateInfo):
        dialog = QProgressDialog("Baixando atualização...", "Cancelar", 0, 100, self.window)
        dialog.setWindowTitle("Atualizando o TTS Reader")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)

        thread = DownloadThread(info, self)
        dialog.canceled.connect(thread.cancel)
        thread.progress.connect(dialog.setValue)
        thread.done.connect(lambda path: self._on_downloaded(path))
        thread.failed.connect(lambda message, i=info: self._on_download_failed(message, i))
        thread.finished.connect(self._on_download_finished)
        self._progress = dialog
        self._download_thread = thread
        thread.start()

    def _on_download_finished(self):
        thread, self._download_thread = self._download_thread, None
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        if thread is not None:
            thread.deleteLater()

    def _on_download_failed(self, message: str, info: updater.UpdateInfo):
        self._log(f"Atualizações: falha no download — {message}")
        QMessageBox.critical(
            self.window,
            "Falha na atualização",
            f"{message}\n\nVocê pode baixar manualmente em:\n{info.page_url}",
        )

    def _on_downloaded(self, path: str):
        self._log(f"Atualizações: instalador baixado e validado em {path}")
        if self._progress is not None:
            self._progress.close()
        try:
            updater.launch_installer(path)
        except OSError as exc:
            self._log(f"Atualizações: falha ao iniciar o instalador — {exc}")
            QMessageBox.critical(self.window, "Falha na atualização", f"Não foi possível iniciar o instalador:\n{exc}")
            return
        self.window.close()

    def shutdown(self) -> None:
        if self._download_thread is not None:
            self._download_thread.cancel()
        for thread in (self._check_thread, self._download_thread):
            if thread is not None and thread.isRunning():
                thread.wait(3000)
