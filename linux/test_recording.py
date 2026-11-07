import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

import recording
from recording import Recording, Recordings, process_identity


class RecordingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Recordings(self.root / "runtime")

    def session(self, digit="a", **changes):
        audio = self.store.directory / f"recording-{digit * 32}.wav"
        audio.write_bytes(b"audio")
        return replace(Recording("recording", 99999999, "1", str(audio), "test", time.time()), **changes)

    def save(self, session):
        with self.store.locked():
            self.store._save(session)

    def test_new_start_supersedes_transcription_without_late_delivery_or_cleanup(self):
        old = self.session(status="transcribing", worker_pid=os.getpid(), worker_start=process_identity(os.getpid()))
        self.save(old)
        self.addCleanup(self.stop_local_capture)
        with self.capture_dependencies():
            self.assertTrue(self.store.start("test"))
        try:
            current = self.store.status()
            self.assertIsNotNone(current)
            self.assertEqual(current.status, "recording")
            self.assertNotEqual(current.audio_path, old.audio_path)
            self.assertFalse(Path(old.audio_path).exists())
            insert = Mock()
            self.assertFalse(self.store.deliver(old, insert))
            insert.assert_not_called()
            self.store.finish(old)
            self.assertEqual(self.store.status(), current)
        finally:
            self.store.cancel()

    def test_duplicate_start_keeps_original_recording(self):
        session = self.session(pid=os.getpid(), pid_start=process_identity(os.getpid()))
        self.save(session)
        with patch.object(recording.subprocess, "Popen") as launch:
            self.assertFalse(self.store.start("another source"))
        launch.assert_not_called()
        self.assertEqual(self.store.status(), session)

    def test_late_finish_and_delivery_cannot_touch_new_session(self):
        old = self.session()
        self.save(old)
        self.store.cancel()
        new = self.session("b", pid=os.getpid(), pid_start=process_identity(os.getpid()))
        self.save(new)
        insert = Mock()
        self.assertFalse(self.store.deliver(old, insert))
        self.store.finish(old)
        insert.assert_not_called()
        self.assertEqual(self.store.status(), new)
        self.assertTrue(Path(new.audio_path).exists())

    def test_cancelled_transcript_is_never_delivered(self):
        session = self.session()
        self.save(session)
        session = self.store.begin_transcription()
        self.assertIsNotNone(session)
        self.assertEqual(session.worker_pid, os.getpid())
        self.store.cancel()
        insert = Mock()
        self.assertFalse(self.store.deliver(session, insert))
        insert.assert_not_called()
        self.store.finish(session)
        self.assertFalse(Path(session.audio_path).exists())
        self.assertFalse(self.store.state_path.exists())

    def test_reused_pid_is_never_signalled(self):
        session = self.session(pid=os.getpid(), pid_start="0")
        self.save(session)
        with patch.object(recording.os, "killpg") as kill:
            self.store.cancel()
        kill.assert_not_called()
        self.assertFalse(Path(session.audio_path).exists())

    def test_stale_state_and_owned_orphans_are_removed_but_unrelated_files_remain(self):
        stale = self.session(status="transcribing", worker_pid=99999999, worker_start="1")
        orphan = self.session("b")
        unrelated = self.store.directory / "meeting.wav"
        unrelated.write_bytes(b"do not delete")
        self.save(stale)
        self.assertIsNone(self.store.status())
        self.assertFalse(self.store.state_path.exists())
        self.assertFalse(Path(stale.audio_path).exists())
        self.assertFalse(Path(orphan.audio_path).exists())
        self.assertEqual(unrelated.read_bytes(), b"do not delete")

    def test_bad_state_is_reported_and_cannot_delete_an_external_file(self):
        outside = self.root / f"recording-{'a' * 32}.wav"
        outside.write_bytes(b"keep")
        payload = asdict(self.session())
        payload["audio_path"] = str(outside)
        for body in ("{", "null", json.dumps(payload)):
            with self.subTest(body=body):
                self.store.state_path.write_text(body)
                with self.assertRaisesRegex(RuntimeError, "Invalid recording state"):
                    self.store.cancel()
                self.assertEqual(outside.read_bytes(), b"keep")

    def test_failed_atomic_write_keeps_previous_state(self):
        old = self.session()
        self.save(old)
        new = self.session("b")
        with patch.object(Path, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.save(new)
        self.assertEqual(json.loads(self.store.state_path.read_text())["audio_path"], old.audio_path)
        self.assertFalse((self.store.directory / ".state.json.tmp").exists())

    def capture_dependencies(self):
        # A controlled subprocess fixture, not a PipeWire compatibility test.
        helper = self.root / "capture-fixture"
        helper.write_text(f"#!{sys.executable}\n"
                          "import pathlib, sys, time\n"
                          "pathlib.Path(sys.argv[-1]).write_bytes(b'audio')\n"
                          "time.sleep(30)\n")
        helper.chmod(0o700)
        timeout = shutil.which("timeout")
        self.assertIsNotNone(timeout)
        return patch.object(recording.shutil, "which", side_effect=lambda name: str(helper) if name == "pw-record" else timeout)

    def stop_local_capture(self):
        child = self.store._local_recorder
        if child is not None:
            try:
                os.killpg(child.pid, 9)
            except ProcessLookupError:
                pass
            child.wait(timeout=3)

    def test_capture_has_a_real_hard_deadline_and_stale_audio_is_pruned(self):
        self.addCleanup(self.stop_local_capture)
        with self.capture_dependencies(), patch.object(recording, "MAX_RECORDING_SECONDS", 0.2):
            self.store.start("test")
        child = self.store._local_recorder
        self.assertEqual(child.wait(timeout=3), 124)
        self.assertIsNone(self.store.status())
        self.assertEqual(list(self.store.directory.glob("recording-*.wav")), [])

    def test_failed_state_save_stops_the_new_capture(self):
        self.addCleanup(self.stop_local_capture)
        children = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        with self.capture_dependencies(), patch.object(self.store, "_save", side_effect=OSError("disk error")), patch.object(
            recording.subprocess, "Popen", side_effect=launch
        ):
            with self.assertRaisesRegex(OSError, "disk error"):
                self.store.start("test")
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(children[0].pid, os.WNOHANG)
        self.assertFalse(self.store.state_path.exists())
        self.assertEqual(list(self.store.directory.glob("recording-*.wav")), [])


if __name__ == "__main__":
    unittest.main()
