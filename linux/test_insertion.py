import os
import subprocess
import unittest
from unittest.mock import patch

import insertion


class InsertionTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        dependencies = patch.object(insertion.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}")
        dependencies.start()
        self.addCleanup(dependencies.stop)

    @patch.object(insertion.subprocess, "run")
    def test_transcript_is_clipboard_data_never_keystrokes(self, run):
        transcript = "-c café, CTRL+C, £42\nsecond line\n"
        insertion.insert_text(transcript)
        copy, paste = run.call_args_list
        self.assertEqual(copy.args[0], ["/usr/bin/wl-copy", "--type", "text/plain;charset=utf-8"])
        self.assertEqual(copy.kwargs["input"], transcript.encode("utf-8"))
        self.assertEqual(paste.args[0], ["/usr/bin/wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"])
        # Exactly these two operations: no delayed clipboard restore or clear.
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertTrue(call.kwargs["check"])
            self.assertEqual(call.kwargs["timeout"], 5)

    @patch.object(insertion.subprocess, "run")
    def test_plain_paste_can_be_selected(self, run):
        os.environ["CODEX_VOICE_PASTE_SHORTCUT"] = "ctrl+v"
        insertion.insert_text("correct spelling")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"])

    @patch.object(insertion.subprocess, "run")
    def test_invalid_shortcut_does_not_change_clipboard(self, run):
        os.environ["CODEX_VOICE_PASTE_SHORTCUT"] = "ctrl+c"
        with self.assertRaisesRegex(RuntimeError, "must be"):
            insertion.insert_text("text")
        run.assert_not_called()

    @patch.object(insertion.subprocess, "run")
    def test_copy_failure_never_pastes_old_clipboard(self, run):
        for error in (subprocess.CalledProcessError(1, "wl-copy"), subprocess.TimeoutExpired("wl-copy", 5)):
            with self.subTest(error=error):
                run.reset_mock()
                run.side_effect = error
                with self.assertRaisesRegex(RuntimeError, "nothing was pasted"):
                    insertion.insert_text("text")
                self.assertEqual(run.call_count, 1)

    @patch.object(insertion.subprocess, "run")
    def test_paste_failure_preserves_transcript_for_manual_recovery(self, run):
        for error in (subprocess.CalledProcessError(1, "wtype"), subprocess.TimeoutExpired("wtype", 5)):
            with self.subTest(error=error):
                run.reset_mock()
                run.side_effect = [None, error]
                with self.assertRaisesRegex(RuntimeError, "paste it manually"):
                    insertion.insert_text("text")
                self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
