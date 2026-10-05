"""Mantém a saída de áudio do Windows aberta tocando silêncio.

Entre um período e outro o QMediaPlayer fecha e reabre o fluxo de áudio. Em
alguns dispositivos (fones Bluetooth, drivers com economia de energia) cada
abertura "acorda" a saída: toca um som de alerta e corta o começo da frase. Com
um fluxo silencioso sempre aberto, a saída não volta a dormir entre os períodos.
"""
from typing import Callable

from PyQt6.QtCore import QIODevice, QObject
from PyQt6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices


class _SilenceDevice(QIODevice):
    """Dispositivo de leitura infinito que só entrega silêncio."""

    def __init__(self, silence_byte: int, parent=None):
        super().__init__(parent)
        self._silence_byte = silence_byte

    def isSequential(self) -> bool:
        return True

    def bytesAvailable(self) -> int:
        return (1 << 16) + super().bytesAvailable()

    def readData(self, maxlen: int) -> bytes:
        return bytes([self._silence_byte]) * maxlen

    def writeData(self, data) -> int:
        return -1


class AudioKeepAlive(QObject):
    def __init__(self, log: Callable[[str], None], parent=None):
        super().__init__(parent)
        self._log = log
        self._enabled = False
        self._sink: QAudioSink | None = None
        self._source: _SilenceDevice | None = None
        # Se o usuário trocar o dispositivo padrão do Windows, acompanha o novo.
        self._devices = QMediaDevices(self)
        self._devices.audioOutputsChanged.connect(self._restart)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._restart()

    def _restart(self) -> None:
        self._stop()
        if self._enabled:
            self._start()

    def _start(self) -> None:
        try:
            device = QMediaDevices.defaultAudioOutput()
            if device.isNull():
                return
            audio_format = device.preferredFormat()
            silence = 0x80 if audio_format.sampleFormat() == QAudioFormat.SampleFormat.UInt8 else 0
            source = _SilenceDevice(silence, self)
            source.open(QIODevice.OpenModeFlag.ReadOnly)
            sink = QAudioSink(device, audio_format, self)
            sink.start(source)
            if sink.error() != QAudio.Error.NoError:
                self._log(f"Áudio contínuo indisponível neste dispositivo (erro {sink.error().name}).")
                sink.stop()
                source.close()
                return
            self._sink, self._source = sink, source
        except Exception as exc:  # noqa: BLE001 — recurso opcional, nunca derruba o app
            self._log(f"Áudio contínuo não pôde ser iniciado: {exc}")

    def _stop(self) -> None:
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
            self._sink = None
        if self._source is not None:
            self._source.close()
            self._source.deleteLater()
            self._source = None
