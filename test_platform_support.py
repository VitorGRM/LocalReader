import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import platform_support


@unittest.skipUnless(importlib.util.find_spec("PyQt6"), "PyQt6 não instalado")
class LegacyMigrationTest(unittest.TestCase):
    """Dados de quando o programa se chamava TTS Reader vão para LocalReader."""

    def setUp(self):
        from PyQt6.QtCore import QSettings

        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data = base / "data"
        self.home = base / "home"
        settings_dir = base / "settings"

        # Configurações em arquivos .ini na pasta temporária: nunca as do usuário.
        def temp_settings(application):
            return QSettings(str(settings_dir / f"{application}.ini"), QSettings.Format.IniFormat)

        self.temp_settings = temp_settings
        patches = [
            mock.patch.object(platform_support, "_settings", temp_settings),
            mock.patch.object(platform_support, "_generic_data_dir", lambda: str(self.data)),
            mock.patch.object(Path, "home", lambda: self.home),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _old_layout(self):
        old = self.temp_settings("TTSReader")
        self.assertTrue(old.fileName().startswith(self.tmp.name.replace(chr(92), "/")) or
                        old.fileName().startswith(self.tmp.name))
        old.setValue("voice/engine", "piper")
        old.setValue("theme", "dark")
        old.sync()
        (self.data / "LocalReader" / "TTSReader" / "highlights").mkdir(parents=True)
        (self.data / "LocalReader" / "TTSReader" / "highlights" / "a.json").write_text("{}")
        (self.home / ".cache" / "tts-reader" / "piper-voices").mkdir(parents=True)
        (self.home / ".cache" / "tts-reader" / "piper-voices" / "voz.onnx").write_text("x")

    def test_moves_settings_data_and_voices(self):
        self._old_layout()
        migrated = platform_support.migrate_legacy_data()
        self.assertTrue(migrated)
        settings = platform_support.app_settings()
        self.assertEqual(settings.value("voice/engine"), "piper")
        self.assertEqual(settings.value("theme"), "dark")
        self.assertTrue((self.data / "LocalReader" / "highlights" / "a.json").exists())
        self.assertFalse((self.data / "LocalReader" / "TTSReader").exists())
        self.assertTrue((self.home / ".cache" / "localreader" / "piper-voices" / "voz.onnx").exists())
        self.assertFalse((self.home / ".cache" / "tts-reader").exists())
        self.assertEqual(Path(platform_support.app_data_dir("highlights")), self.data / "LocalReader" / "highlights")

    def test_runs_only_once_and_keeps_new_settings(self):
        self._old_layout()
        platform_support.migrate_legacy_data()
        settings = platform_support.app_settings()
        settings.setValue("theme", "light")  # o usuário mudou depois da migração
        settings.sync()
        self.assertEqual(platform_support.migrate_legacy_data(), [])
        self.assertEqual(platform_support.app_settings().value("theme"), "light")

    def test_nothing_to_migrate(self):
        self.assertEqual(platform_support.migrate_legacy_data(), [])
        self.assertFalse(os.path.exists(self.home / ".cache" / "localreader"))


if __name__ == "__main__":
    unittest.main()
