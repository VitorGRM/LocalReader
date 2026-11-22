import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

import qwen_voices
from qwen_voices import (
    delete_custom_voice,
    get_custom_voice,
    list_custom_voices,
    rate_instruction,
    read_audio,
    save_custom_voice,
)


class CustomVoiceStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(qwen_voices, "voices_dir", lambda: Path(self.tmp.name) / "voices")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.rate = 24000
        self.samples = (0.1 * np.sin(np.linspace(0, 2000, self.rate * 25))).astype(np.float32)  # 25 s

    def test_save_list_and_delete(self):
        voice = save_custom_voice(
            "Professora Paulista", self.samples, self.rate, "  Bom dia a todos.  ", "Female",
            description="sotaque paulistano", source="design",
        )
        self.assertEqual(voice.id, "professora-paulista")
        self.assertEqual(voice.reference_text, "Bom dia a todos.")
        with wave.open(str(voice.reference)) as wav:
            self.assertEqual(wav.getframerate(), self.rate)
            self.assertEqual(wav.getnframes(), self.rate * 20)  # cortado em 20 s
        listed = list_custom_voices()
        self.assertEqual([(v.id, v.name, v.gender, v.source) for v in listed],
                         [("professora-paulista", "Professora Paulista", "Female", "design")])
        self.assertEqual(get_custom_voice("professora-paulista").description, "sotaque paulistano")
        delete_custom_voice("professora-paulista")
        self.assertEqual(list_custom_voices(), [])

    def test_names_ids_and_duplicates(self):
        save_custom_voice("Voz Ção!", self.samples, self.rate, "")
        self.assertEqual(list_custom_voices()[0].id, "voz-cao")
        with self.assertRaises(ValueError):
            save_custom_voice("voz ção!", self.samples, self.rate, "")  # mesmo nome
        with self.assertRaises(ValueError):
            save_custom_voice("   ", self.samples, self.rate, "")
        second = save_custom_voice("Voz Cao", self.samples, self.rate, "")  # nome outro, id igual
        self.assertEqual(second.id, "voz-cao-2")

    def test_broken_folders_are_ignored(self):
        save_custom_voice("Boa", self.samples, self.rate, "")
        broken = Path(self.tmp.name) / "voices" / "quebrada"
        broken.mkdir()
        (broken / "voice.json").write_text("{não é json", encoding="utf-8")
        self.assertEqual([v.id for v in list_custom_voices()], ["boa"])

    def test_delete_never_leaves_the_voices_folder(self):
        outside = Path(self.tmp.name) / "importante"
        outside.mkdir()
        delete_custom_voice("../importante")
        self.assertTrue(outside.exists())

    def test_read_stereo_wav_as_mono(self):
        path = os.path.join(self.tmp.name, "stereo.wav")
        frames = np.array([[1000, -1000]] * 100, dtype="<i2")
        with wave.open(path, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(frames.tobytes())
        samples, rate = read_audio(path)
        self.assertEqual(rate, 16000)
        self.assertEqual(samples.shape, (100,))
        self.assertTrue(np.allclose(samples, 0.0))


class RateInstructionTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(rate_instruction(5), "")
        self.assertIn("faster", rate_instruction(30))


if __name__ == "__main__":
    unittest.main()
