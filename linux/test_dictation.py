import os
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import codex_voice
from recording import Recording, Recordings, process_identity


class DictationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Recordings(Path(directory.name))
        self.audio = self.store.directory / f"recording-{'a' * 32}.wav"
        with wave.open(str(self.audio), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 16000)
        self.session = Recording("recording", 99999999, "1", str(self.audio), "test", time.time() - 1)
        with self.store.locked():
            self.store._save(self.session)
        self.transcriber = Mock()
        notifications = patch.object(codex_voice, "notify")
        self.notify = notifications.start()
        self.addCleanup(notifications.stop)

    @patch.object(codex_voice, "insert_text")
    def test_success_delivers_once_and_removes_audio_and_state(self, insert):
        self.transcriber.transcribe.return_value = " Correct café. "
        self.assertEqual(codex_voice.stop_recording(self.store, self.transcriber), 0)
        self.assertEqual(codex_voice.stop_recording(self.store, self.transcriber), 0)
        insert.assert_called_once_with("Correct café.")
        self.assertFalse(self.audio.exists())
        self.assertFalse(self.store.state_path.exists())

    @patch.object(codex_voice, "report_error")
    def test_network_failure_still_cleans_up(self, report):
        error = RuntimeError("network unavailable")
        self.transcriber.transcribe.side_effect = error
        self.assertEqual(codex_voice.stop_recording(self.store, self.transcriber), 1)
        report.assert_called_once_with(error)
        self.assertFalse(self.audio.exists())
        self.assertFalse(self.store.state_path.exists())

    @patch.object(codex_voice, "report_error")
    def test_invalid_audio_still_cleans_up_without_network(self, report):
        self.audio.write_bytes(b"broken WAV")
        self.assertEqual(codex_voice.stop_recording(self.store, self.transcriber), 1)
        report.assert_called_once()
        self.transcriber.transcribe.assert_not_called()
        self.assertFalse(self.audio.exists())
        self.assertFalse(self.store.state_path.exists())

    @patch.object(codex_voice, "report_error")
    @patch.object(codex_voice, "insert_text")
    def test_cancel_during_request_drops_late_result_or_error_without_erasing_new_session(self, insert, report):
        new_audio = self.store.directory / f"recording-{'b' * 32}.wav"
        new_session = Recording("recording", os.getpid(), process_identity(os.getpid()), str(new_audio), "test", time.time())

        def cancelled_request(path):
            self.store.cancel()
            new_audio.write_bytes(b"new recording")
            with self.store.locked():
                self.store._save(new_session)
            if fail:
                raise RuntimeError("late ASR error")
            return "late transcript"

        for fail in (False, True):
            with self.subTest(fail=fail):
                with self.store.locked():
                    self.store._save(self.session)
                # The WAV was removed by the preceding iteration's finalizer.
                with wave.open(str(self.audio), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16000)
                    audio.writeframes(b"\x00\x00" * 16000)
                self.transcriber.transcribe.side_effect = cancelled_request
                self.assertEqual(codex_voice.stop_recording(self.store, self.transcriber), 0)
                self.assertEqual(self.store.status(), new_session)
                self.assertEqual(new_audio.read_bytes(), b"new recording")
        insert.assert_not_called()
        report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
