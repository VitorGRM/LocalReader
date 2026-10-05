"""Aba PyQt6 do OCR adaptativo."""
from __future__ import annotations

import shutil
import traceback
from pathlib import Path

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ocr_engine import (
    OCRCancelled,
    OCRProcessor,
    OCRResult,
    OCRSettings,
    available_tesseract_languages,
    create_searchable_pdf,
    export_docx,
    language_label,
)


class OCRWorker(QThread):
    progress = pyqtSignal(int, int, str)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str, str)
    cancelled = pyqtSignal()

    def __init__(self, path: str, settings: OCRSettings, parent=None):
        super().__init__(parent)
        self.path = path
        self.settings = settings
        self.processor: OCRProcessor | None = None

    def run(self):
        try:
            self.processor = OCRProcessor(
                self.settings,
                progress=lambda current, total, message: self.progress.emit(current, total, message),
                cancelled=self.isInterruptionRequested,
            )
            self.completed.emit(self.processor.process(self.path))
        except OCRCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc), traceback.format_exc())
        finally:
            self.processor = None

    def stop(self):
        self.requestInterruption()
        if self.processor is not None:
            self.processor.cancel()


class SearchablePDFWorker(QThread):
    completed = pyqtSignal(str)
    failed = pyqtSignal(str, str)
    cancelled = pyqtSignal()

    def __init__(
        self, source: str, destination: str, languages: tuple[str, ...], dpi: int,
        force: bool, page_expression: str,
    ):
        super().__init__()
        self.source = source
        self.destination = destination
        self.languages = languages
        self.dpi = dpi
        self.force = force
        self.page_expression = page_expression

    def run(self):
        try:
            create_searchable_pdf(
                self.source,
                self.destination,
                self.languages,
                self.dpi,
                self.force,
                cancelled=self.isInterruptionRequested,
                page_expression=self.page_expression,
            )
            self.completed.emit(self.destination)
        except OCRCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc), traceback.format_exc())

    def stop(self):
        self.requestInterruption()


class OCRWidget(QWidget):
    send_to_tts = pyqtSignal(str)
    log_message = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source_path = ""
        self.result: OCRResult | None = None
        self.worker: OCRWorker | None = None
        self.pdf_worker: SearchablePDFWorker | None = None
        self._build_ui()
        self._refresh_languages()
        self._update_controls()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)

        source_row = QHBoxLayout()
        self.open_button = QPushButton("📄 Abrir PDF ou imagem")
        self.open_button.clicked.connect(self._choose_source)
        self.source_label = QLabel("Nenhum arquivo selecionado")
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.source_label.setStyleSheet("QLabel { padding: 6px; color: #444; }")
        source_row.addWidget(self.open_button)
        source_row.addWidget(self.source_label, 1)
        root.addLayout(source_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_options_panel())
        splitter.addWidget(self._build_output_panel())
        splitter.setSizes([390, 850])
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    def _build_options_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(330)
        content = QWidget()
        layout = QVBoxLayout(content)

        strategy_group = QGroupBox("Estratégia")
        strategy = QFormLayout(strategy_group)
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("Adaptativa — nativo + OCR seletivo", "adaptive")
        self.engine_combo.addItem("OCR completo — Tesseract em todas as páginas", "tesseract")
        self.engine_combo.addItem("Somente texto incorporado — sem OCR", "native")
        self.engine_combo.currentIndexChanged.connect(self._update_controls)
        strategy.addRow("Motor:", self.engine_combo)

        self.page_edit = QLineEdit()
        self.page_edit.setPlaceholderText("Todas (ex.: 1-5, 8, 12-)")
        strategy.addRow("Páginas:", self.page_edit)

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(150, 600)
        self.dpi_spin.setSingleStep(50)
        self.dpi_spin.setValue(300)
        self.dpi_spin.setSuffix(" dpi")
        strategy.addRow("Resolução:", self.dpi_spin)

        self.sensitivity_combo = QComboBox()
        self.sensitivity_combo.addItem("Equilibrada", 55)
        self.sensitivity_combo.addItem("Preferir OCR (mais precisão)", 72)
        self.sensitivity_combo.addItem("Preferir texto nativo (mais rápido)", 38)
        strategy.addRow("Decisão automática:", self.sensitivity_combo)
        layout.addWidget(strategy_group)

        language_group = QGroupBox("Idiomas simultâneos")
        language_layout = QVBoxLayout(language_group)
        language_help = QLabel(
            "Marque um ou mais modelos. Para maior precisão, selecione apenas os idiomas prováveis."
        )
        language_help.setWordWrap(True)
        language_help.setStyleSheet("color: #555; font-size: 11px;")
        language_layout.addWidget(language_help)
        self.language_list = QListWidget()
        self.language_list.setMaximumHeight(165)
        self.language_list.itemChanged.connect(self._update_action_buttons)
        language_layout.addWidget(self.language_list)
        refresh_button = QPushButton("↻ Atualizar modelos instalados")
        refresh_button.clicked.connect(self._refresh_languages)
        language_layout.addWidget(refresh_button)
        self.language_status = QLabel()
        self.language_status.setWordWrap(True)
        self.language_status.setStyleSheet("color: #555; font-size: 11px;")
        language_layout.addWidget(self.language_status)
        layout.addWidget(language_group)

        layout_group = QGroupBox("Layout e colunas")
        layout_form = QFormLayout(layout_group)
        self.layout_combo = QComboBox()
        self.layout_combo.addItem("Automático (recomendado)", "auto")
        self.layout_combo.addItem("Uma coluna / bloco uniforme", "single")
        self.layout_combo.addItem("Duas colunas (ordem esq. → dir.)", "columns_2")
        self.layout_combo.addItem("Três colunas (ordem esq. → dir.)", "columns_3")
        self.layout_combo.addItem("Texto esparso / formulários", "sparse")
        self.layout_combo.addItem("Linha única / código curto", "raw")
        layout_form.addRow("Disposição:", self.layout_combo)

        self.preprocess_combo = QComboBox()
        self.preprocess_combo.addItem("Automático adaptativo", "auto")
        self.preprocess_combo.addItem("Sem tratamento", "none")
        self.preprocess_combo.addItem("Escala de cinza", "grayscale")
        self.preprocess_combo.addItem("Contraste reforçado", "contrast")
        self.preprocess_combo.addItem("Binarização Otsu", "binary")
        layout_form.addRow("Pré-processamento:", self.preprocess_combo)
        layout.addWidget(layout_group)

        cleanup_group = QGroupBox("Correções automáticas")
        cleanup_layout = QVBoxLayout(cleanup_group)
        self.rotate_check = QCheckBox("Detectar e corrigir orientação (OSD)")
        self.rotate_check.setChecked(True)
        self.deskew_check = QCheckBox("Corrigir pequenas inclinações")
        self.deskew_check.setChecked(True)
        self.dehyphenate_check = QCheckBox("Unir palavras hifenizadas entre linhas")
        self.dehyphenate_check.setChecked(True)
        self.margins_check = QCheckBox("Remover cabeçalhos/rodapés repetidos")
        self.margins_check.setChecked(True)
        self.breaks_check = QCheckBox("Identificar separações de página no texto")
        self.breaks_check.setChecked(True)
        for checkbox in (
            self.rotate_check, self.deskew_check, self.dehyphenate_check,
            self.margins_check, self.breaks_check,
        ):
            cleanup_layout.addWidget(checkbox)
        layout.addWidget(cleanup_group)

        self.start_button = QPushButton("✨ Extrair texto com OCR")
        self.start_button.setMinimumHeight(38)
        self.start_button.clicked.connect(self._start_ocr)
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.clicked.connect(self._cancel_ocr)
        self.cancel_button.setEnabled(False)
        layout.addWidget(self.start_button)
        layout.addWidget(self.cancel_button)
        layout.addStretch(1)
        scroll.setWidget(content)
        return scroll

    def _build_output_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 0, 0)
        title = QLabel("Texto reconhecido")
        title.setFont(QFont("Sans Serif", 12, QFont.Weight.Bold))
        layout.addWidget(title)
        self.output_edit = QTextEdit()
        self.output_edit.setAcceptRichText(False)
        self.output_edit.setPlaceholderText(
            "O texto aparecerá aqui. O resultado é editável e pode ser enviado diretamente para o leitor TTS."
        )
        self.output_edit.setFont(QFont("Georgia", 12))
        self.output_edit.textChanged.connect(self._update_action_buttons)
        layout.addWidget(self.output_edit, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status_label = QLabel("Selecione um PDF ou uma imagem para começar.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        actions = QHBoxLayout()
        self.send_button = QPushButton("🔊 Enviar ao TTS")
        self.send_button.clicked.connect(self._send_to_tts)
        self.save_button = QPushButton("💾 Salvar texto…")
        self.save_button.clicked.connect(self._save_result)
        self.searchable_button = QPushButton("🔎 Criar PDF pesquisável…")
        self.searchable_button.clicked.connect(self._create_searchable_pdf)
        actions.addWidget(self.send_button)
        actions.addWidget(self.save_button)
        actions.addWidget(self.searchable_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        return panel

    def _choose_source(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Abrir documento para OCR",
            "",
            "PDF e imagens (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.pnm *.pgm *.ppm);;Todos os arquivos (*)",
        )
        if not path:
            return
        self.source_path = path
        self.source_label.setText(path)
        self.source_label.setToolTip(path)
        self.result = None
        self.status_label.setText("Arquivo pronto. Ajuste as opções ou use a configuração adaptativa.")
        self._update_controls()

    def _refresh_languages(self):
        selected = set(self._selected_languages())
        languages = available_tesseract_languages()
        self.language_list.clear()
        preferred = ("por", "eng", "spa")
        for code in languages:
            item = QListWidgetItem(language_label(code))
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            should_check = code in selected or (not selected and code == next((x for x in preferred if x in languages), languages[0] if languages else ""))
            item.setCheckState(Qt.CheckState.Checked if should_check else Qt.CheckState.Unchecked)
            self.language_list.addItem(item)
        if languages:
            missing_pt = " O modelo português ainda não está instalado." if "por" not in languages else ""
            self.language_status.setText(f"{len(languages)} modelo(s) disponível(is).{missing_pt}")
        else:
            self.language_status.setText("Tesseract não encontrado ou sem modelos de idioma.")

    def _selected_languages(self) -> tuple[str, ...]:
        selected = []
        for index in range(self.language_list.count()):
            item = self.language_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                selected.append(item.data(Qt.ItemDataRole.UserRole))
        return tuple(selected)

    def _settings(self) -> OCRSettings:
        languages = self._selected_languages()
        if self.engine_combo.currentData() != "native" and not languages:
            raise ValueError("Selecione pelo menos um idioma OCR.")
        return OCRSettings(
            engine=self.engine_combo.currentData(),
            languages=languages,
            dpi=self.dpi_spin.value(),
            layout=self.layout_combo.currentData(),
            preprocessing=self.preprocess_combo.currentData(),
            rotate_pages=self.rotate_check.isChecked(),
            deskew=self.deskew_check.isChecked(),
            adaptive_threshold=self.sensitivity_combo.currentData(),
            page_expression=self.page_edit.text(),
            preserve_page_breaks=self.breaks_check.isChecked(),
            dehyphenate=self.dehyphenate_check.isChecked(),
            remove_repeated_margins=self.margins_check.isChecked(),
        )

    def _start_ocr(self):
        if not self.source_path:
            QMessageBox.information(self, "Selecione um arquivo", "Abra primeiro um PDF ou uma imagem.")
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Opções de OCR", str(exc))
            return
        self.result = None
        self.output_edit.clear()
        self.worker = OCRWorker(self.source_path, settings, self)
        self.worker.progress.connect(self._on_progress)
        self.worker.completed.connect(self._on_completed)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.finished.connect(self._on_worker_finished)
        self.progress.setRange(0, 0)
        self.status_label.setText("Preparando OCR…")
        self._set_running(True)
        self.worker.start()

    def _cancel_ocr(self):
        if self.worker is not None:
            self.status_label.setText("Cancelando…")
            self.worker.stop()
        elif self.pdf_worker is not None:
            self.status_label.setText("Cancelando criação do PDF…")
            self.pdf_worker.stop()

    def _on_progress(self, current: int, total: int, message: str):
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(current)
        self.status_label.setText(message)

    def _on_completed(self, result: OCRResult):
        self.result = result
        self.output_edit.setPlainText(result.text)
        confidence = (
            f" Confiança OCR média: {result.average_confidence:.1f}%."
            if result.average_confidence is not None else ""
        )
        self.status_label.setText(
            f"Concluído em {result.elapsed:.1f}s: {len(result.pages)} página(s), "
            f"{result.native_pages} nativa(s) e {result.ocr_pages} processada(s) por OCR.{confidence}"
        )

    def _on_failed(self, message: str, details: str):
        self.log_message.emit(f"Falha no OCR: {message}\n{details}")
        self.status_label.setText(f"Falha: {message}")
        QMessageBox.critical(self, "Erro no OCR", message)

    def _on_cancelled(self):
        self.status_label.setText("OCR cancelado.")

    def _on_worker_finished(self):
        self.worker = None
        self.progress.setRange(0, 1)
        self.progress.setValue(1 if self.result else 0)
        self._set_running(False)

    def _set_running(self, running: bool):
        running = running or (self.pdf_worker is not None and self.pdf_worker.isRunning())
        self.start_button.setEnabled(not running and bool(self.source_path))
        self.open_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.send_button.setEnabled(not running and bool(self.output_edit.toPlainText().strip()))
        self.save_button.setEnabled(not running and bool(self.output_edit.toPlainText().strip()))
        self.searchable_button.setEnabled(
            not running
            and self.source_path.lower().endswith(".pdf")
            and bool(self._selected_languages())
            and shutil.which("ocrmypdf") is not None
        )

    def _update_action_buttons(self, *_args):
        running = (self.worker is not None and self.worker.isRunning()) or (
            self.pdf_worker is not None and self.pdf_worker.isRunning()
        )
        self._set_running(running)

    def _update_controls(self):
        native_only = self.engine_combo.currentData() == "native"
        adaptive = self.engine_combo.currentData() == "adaptive"
        self.dpi_spin.setEnabled(not native_only)
        self.preprocess_combo.setEnabled(not native_only)
        self.rotate_check.setEnabled(not native_only)
        self.deskew_check.setEnabled(not native_only)
        self.language_list.setEnabled(not native_only)
        self.sensitivity_combo.setEnabled(adaptive)
        self._set_running(self.worker is not None and self.worker.isRunning())

    def _send_to_tts(self):
        text = self.output_edit.toPlainText().strip()
        if text:
            self.send_to_tts.emit(text)

    def _save_result(self):
        text = self.output_edit.toPlainText().strip()
        if not text:
            return
        base = Path(self.source_path).stem if self.source_path else "ocr"
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Salvar texto reconhecido",
            f"{base}_ocr.txt",
            "Texto simples (*.txt);;Word (*.docx);;Markdown (*.md)",
        )
        if not path:
            return
        try:
            suffix = Path(path).suffix.lower()
            if "Word" in selected_filter or suffix == ".docx":
                if suffix != ".docx":
                    path += ".docx"
                if self.result is not None and text == self.result.text.strip():
                    export_docx(path, self.result)
                else:
                    from docx import Document
                    document = Document()
                    for paragraph in text.split("\n\n"):
                        document.add_paragraph(paragraph)
                    document.save(path)
            else:
                if not suffix:
                    path += ".md" if "Markdown" in selected_filter else ".txt"
                with open(path, "w", encoding="utf-8") as file:
                    file.write(text + "\n")
            self.status_label.setText(f"Resultado salvo em {path}")
        except Exception as exc:  # noqa: BLE001
            self.log_message.emit(f"Falha ao salvar OCR: {exc}\n{traceback.format_exc()}")
            QMessageBox.critical(self, "Erro ao salvar", str(exc))

    def _create_searchable_pdf(self):
        if not self.source_path.lower().endswith(".pdf"):
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Opções de OCR", str(exc))
            return
        base = str(Path(self.source_path).with_name(Path(self.source_path).stem + "_pesquisavel.pdf"))
        destination, _ = QFileDialog.getSaveFileName(
            self, "Criar PDF pesquisável", base, "PDF (*.pdf)"
        )
        if not destination:
            return
        if not destination.lower().endswith(".pdf"):
            destination += ".pdf"
        force = settings.engine == "tesseract"
        self.pdf_worker = SearchablePDFWorker(
            self.source_path, destination, settings.languages, settings.dpi, force,
            settings.page_expression,
        )
        self.pdf_worker.completed.connect(self._on_pdf_completed)
        self.pdf_worker.failed.connect(self._on_failed)
        self.pdf_worker.cancelled.connect(self._on_cancelled)
        self.pdf_worker.finished.connect(self._on_pdf_finished)
        self.status_label.setText("Criando PDF pesquisável em segundo plano…")
        self._set_running(True)
        self.pdf_worker.start()

    def _on_pdf_completed(self, destination: str):
        self.status_label.setText(f"PDF pesquisável criado em {destination}")
        QMessageBox.information(self, "PDF concluído", f"Arquivo criado com sucesso:\n{destination}")

    def _on_pdf_finished(self):
        self.pdf_worker = None
        self._set_running(False)

    def shutdown(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        if self.pdf_worker is not None and self.pdf_worker.isRunning():
            self.pdf_worker.stop()
            self.pdf_worker.wait(6000)
