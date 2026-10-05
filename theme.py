"""Tema claro/escuro: Automático (segue o Windows), Claro ou Escuro.

A escolha é salva em QSettings. O tema escuro usa o estilo Fusion com uma
paleta própria; o claro volta ao estilo nativo da plataforma. Cores que não
vêm da paleta (leitor, destaque, textos secundários) ficam em ``ThemeManager``.
"""
from PyQt6.QtCore import QObject, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

MODES = ("system", "light", "dark")
MODE_LABELS = {"system": "Automático (igual ao Windows)", "light": "Claro", "dark": "Escuro"}
SETTINGS_KEY = "theme"

_SCHEME_FOR_MODE = {
    "system": Qt.ColorScheme.Unknown,
    "light": Qt.ColorScheme.Light,
    "dark": Qt.ColorScheme.Dark,
}

_LIGHT_COLORS = {
    "reader_background": "#fffdf7",
    "reader_text": "#2b2b2b",
    "highlight": "#ffe08a",
    "muted_text": "#555555",
    "info_background": "#f0f0f0",
    "info_text": "#444444",
}
_DARK_COLORS = {
    "reader_background": "#1b1c1f",
    "reader_text": "#e4e1d8",
    "highlight": "#5a4a12",
    "muted_text": "#a8abb0",
    "info_background": "#2f3135",
    "info_text": "#c9ccd1",
}


def _dark_palette() -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: "#2b2d30",
        QPalette.ColorRole.WindowText: "#e6e6e6",
        QPalette.ColorRole.Base: "#1e1f22",
        QPalette.ColorRole.AlternateBase: "#2f3135",
        QPalette.ColorRole.Text: "#e6e6e6",
        QPalette.ColorRole.Button: "#383a3e",
        QPalette.ColorRole.ButtonText: "#e6e6e6",
        QPalette.ColorRole.BrightText: "#ff6b6b",
        QPalette.ColorRole.ToolTipBase: "#383a3e",
        QPalette.ColorRole.ToolTipText: "#e6e6e6",
        QPalette.ColorRole.PlaceholderText: "#8d9095",
        QPalette.ColorRole.Highlight: "#2f65ca",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: "#6fa8ff",
        QPalette.ColorRole.LinkVisited: "#b48cff",
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    disabled = QColor("#7a7d82")
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor("#4a4d52"))
    return palette


class ThemeManager(QObject):
    changed = pyqtSignal()

    def __init__(self, app: QApplication):
        super().__init__(app)
        self._app = app
        self._native_style = app.style().objectName()
        self._settings = QSettings("LocalReader", "TTSReader")
        mode = str(self._settings.value(SETTINGS_KEY, "system"))
        self.mode = mode if mode in MODES else "system"
        self.is_dark = False
        self._applying = False
        app.styleHints().colorSchemeChanged.connect(self._on_system_scheme_changed)
        self.apply()

    def set_mode(self, mode: str) -> None:
        if mode not in MODES or mode == self.mode:
            return
        self.mode = mode
        self._settings.setValue(SETTINGS_KEY, mode)
        self.apply()

    def apply(self) -> None:
        if self._applying:
            return
        self._applying = True
        try:
            hints = self._app.styleHints()
            if hasattr(hints, "setColorScheme"):  # Qt 6.8+: também escurece a barra de título
                hints.setColorScheme(_SCHEME_FOR_MODE[self.mode])
            if self.mode == "system":
                dark = hints.colorScheme() == Qt.ColorScheme.Dark
            else:
                dark = self.mode == "dark"
            if dark:
                self._app.setStyle("Fusion")
                self._app.setPalette(_dark_palette())
            else:
                self._app.setStyle(self._native_style)
                self._app.setPalette(self._app.style().standardPalette())
            self.is_dark = dark
        finally:
            self._applying = False
        self.changed.emit()

    def _on_system_scheme_changed(self, *_args) -> None:
        if self.mode == "system":
            self.apply()

    def _color(self, name: str) -> QColor:
        return QColor((_DARK_COLORS if self.is_dark else _LIGHT_COLORS)[name])

    @property
    def highlight_color(self) -> QColor:
        return self._color("highlight")

    def reader_stylesheet(self) -> str:
        return (
            f"QTextEdit {{ background-color: {self._color('reader_background').name()}; "
            f"color: {self._color('reader_text').name()}; padding: 24px; }}"
        )

    def info_label_stylesheet(self) -> str:
        return (
            f"QLabel {{ color: {self._color('info_text').name()}; font-size: 11px; "
            f"background-color: {self._color('info_background').name()}; "
            "border-radius: 4px; padding: 6px; }"
        )

    def muted_text_color(self) -> str:
        return self._color("muted_text").name()
