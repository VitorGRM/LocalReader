import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import qwen_setup
import qwen_voices

# Processo falso no lugar do qwen_worker.py: mesmo protocolo, sem PyTorch.
FAKE_WORKER = textwrap.dedent('''
    import json, sys, wave
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        if request["op"] == "crash":
            sys.exit(3)
        if request.get("text") == "erro":
            print(json.dumps({"id": request["id"], "ok": False, "error": "falhou de propósito"}), flush=True)
            continue
        print("log do modelo", file=sys.stderr, flush=True)
        with wave.open(request["out"], "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
            out.writeframes(b"\\x00\\x10" * 2400)
        print(json.dumps({"id": request["id"], "ok": True, "sample_rate": 24000, "op": request["op"]}), flush=True)
''')


class WorkerClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        script = Path(self.tmp.name) / "fake_worker.py"
        script.write_text(FAKE_WORKER, encoding="utf-8")
        patches = [
            mock.patch.object(qwen_voices, "worker_python", lambda: sys.executable),
            mock.patch.object(qwen_voices, "worker_script", lambda: script),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runtime = qwen_voices.QwenRuntime()
        self.addCleanup(self.runtime.shutdown)
        self.addCleanup(self.tmp.cleanup)

    def test_generates_audio_through_the_process(self):
        samples, rate = self.runtime.submit(self.runtime.speak, "Olá", "Vivian", "Portuguese", "calmo").result(30)
        self.assertEqual(rate, 24000)
        self.assertEqual(len(samples), 2400)
        samples, _ = self.runtime.submit(self.runtime.clone, "Olá", "Portuguese", "ref.wav", "").result(30)
        self.assertEqual(len(samples), 2400)

    def test_errors_from_the_process_are_raised(self):
        with self.assertRaisesRegex(RuntimeError, "falhou de propósito"):
            self.runtime.submit(self.runtime.speak, "erro", "Vivian", "Portuguese", "").result(30)
        # O processo continua vivo para o próximo pedido.
        self.runtime.submit(self.runtime.speak, "ok", "Vivian", "Portuguese", "").result(30)

    def test_process_that_dies_is_restarted(self):
        with self.assertRaisesRegex(RuntimeError, "parou"):
            self.runtime.submit(self.runtime.call, "crash").result(30)
        samples, _ = self.runtime.submit(self.runtime.speak, "de novo", "Vivian", "Portuguese", "").result(30)
        self.assertEqual(len(samples), 2400)

    def test_not_installed(self):
        with mock.patch.object(qwen_voices, "worker_python", lambda: None):
            runtime = qwen_voices.QwenRuntime()
            with self.assertRaisesRegex(RuntimeError, "não está instalada"):
                runtime.submit(runtime.speak, "x", "Vivian", "Portuguese", "").result(30)


class WorkerPythonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(qwen_voices, "runtime_dir", lambda: Path(self.tmp.name) / "qwen-runtime")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _install_fake_runtime(self, ready=True):
        python = qwen_voices.runtime_python()
        python.parent.mkdir(parents=True)
        python.write_text("")
        if ready:
            (qwen_voices.runtime_dir() / "ready.json").write_text("{}")

    def test_installed_runtime_is_preferred(self):
        self._install_fake_runtime()
        self.assertEqual(qwen_voices.worker_python(), str(qwen_voices.runtime_python()))
        self.assertTrue(qwen_voices.qwen_available())

    def test_half_installed_runtime_is_ignored(self):
        self._install_fake_runtime(ready=False)
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertIsNone(qwen_voices.worker_python())

    def test_frozen_app_without_runtime_has_no_qwen(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertIsNone(qwen_voices.worker_python())
            self.assertFalse(qwen_voices.qwen_available())

    def test_worker_script_is_next_to_the_app(self):
        self.assertTrue(qwen_voices.worker_script().is_file())
        with mock.patch.object(sys, "_MEIPASS", self.tmp.name, create=True):
            self.assertEqual(qwen_voices.worker_script(), Path(self.tmp.name) / "qwen_worker.py")


class SetupTest(unittest.TestCase):
    def test_uv_asset_for_this_system(self):
        asset, executable = qwen_setup._uv_asset()
        if os.name == "nt":
            self.assertEqual((asset, executable), ("uv-x86_64-pc-windows-msvc.zip", "uv.exe"))
        else:
            self.assertTrue(asset.endswith("-unknown-linux-gnu.tar.gz"))

    def test_no_nvidia_driver(self):
        with mock.patch.object(qwen_setup.shutil, "which", lambda _name: None):
            self.assertIsNone(qwen_setup.nvidia_gpu())

    def test_uv_with_wrong_checksum_is_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(qwen_setup, "runtime_dir", lambda: Path(tmp.name)):
            setup = qwen_setup.QwenSetup(lambda _line: None)
        fetched = {".sha256": b"0" * 64 + b"  uv.zip", "": b"conteudo adulterado"}
        setup._fetch = lambda url: fetched[".sha256" if url.endswith(".sha256") else ""]
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            setup._download_uv()
        self.assertFalse(any(Path(tmp.name, "tools").glob("uv*")))

    def test_cancel_before_starting(self):
        with mock.patch.object(qwen_setup, "runtime_dir", lambda: Path(tempfile.gettempdir()) / "lr-test-runtime"):
            setup = qwen_setup.QwenSetup(lambda _line: None, cancelled=lambda: True)
        with self.assertRaises(qwen_setup.SetupCancelled):
            setup._fetch("https://example.invalid/uv.zip")


class WorkerProtocolTest(unittest.TestCase):
    def test_real_worker_answers_ready_and_reports_errors_without_torch(self):
        """O qwen_worker.py de verdade, com uma operação inválida (não precisa do PyTorch)."""
        import subprocess

        process = subprocess.Popen(
            [sys.executable, "-u", str(Path(qwen_voices.__file__).with_name("qwen_worker.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        try:
            self.assertTrue(json.loads(process.stdout.readline())["ready"])
            process.stdin.write(json.dumps({"id": 7, "op": "voar", "out": "x.wav"}) + "\n")
            process.stdin.flush()
            response = json.loads(process.stdout.readline())
            self.assertEqual((response["id"], response["ok"]), (7, False))
            self.assertIn("voar", response["error"])
        finally:
            process.stdin.close()
            process.wait(10)
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
