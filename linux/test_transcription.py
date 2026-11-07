import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import transcription
from transcription import Transcriber


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.auth = self.root / "auth.json"
        self.auth.write_text(json.dumps({"tokens": {"access_token": "old-token", "account_id": "account"}}))
        self.audio = self.root / "recording.wav"
        self.audio.write_bytes(b"RIFF\x00wave-data")
        self.transcriber = Transcriber((self.auth,), None)

    @staticmethod
    def response(payload):
        return io.BytesIO(json.dumps(payload).encode())

    @staticmethod
    def unauthorized():
        return urllib.error.HTTPError("https://example.invalid", 401, "Unauthorized", {}, io.BytesIO())

    def test_auth_path_and_field_precedence(self):
        second = self.root / "pi.json"
        second.write_text(json.dumps({"openai-codex": {"access": "pi-token", "accountId": "pi-account"}}))
        self.auth.write_text(json.dumps({
            "tokens": {"access_token": "codex-token"},
            "openai-codex": {"access": "other-token", "accountId": "mixed-account"},
        }))
        transcriber = Transcriber((self.auth, second), None)
        self.assertEqual(transcriber.read_credentials(), transcription.Credentials("codex-token", "mixed-account"))
        self.auth.write_text('{"tokens": {}}')
        self.assertEqual(transcriber.read_credentials(), transcription.Credentials("pi-token", "pi-account"))

    def test_invalid_auth_is_reported_not_silently_skipped(self):
        for payload in ([], {"tokens": []}, {"openai-codex": "bad"},
                        {"tokens": {"access_token": 42, "account_id": "account"}},
                        {"tokens": {"access_token": "token\r\nInjected: true", "account_id": "account"}}):
            with self.subTest(payload=payload):
                self.auth.write_text(json.dumps(payload))
                with self.assertRaisesRegex(RuntimeError, "Invalid auth"):
                    self.transcriber.read_credentials()
        self.auth.write_text("not JSON")
        with self.assertRaisesRegex(RuntimeError, "Failed to parse"):
            self.transcriber.read_credentials()

    def test_missing_and_incomplete_auth_are_distinguished(self):
        self.auth.unlink()
        with self.assertRaisesRegex(RuntimeError, "not found"):
            self.transcriber.read_credentials()
        self.auth.write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.transcriber.read_credentials()

    def test_wire_headers_and_multipart_preserved(self):
        with patch.object(transcription.urllib.request, "urlopen", return_value=self.response({"text": " café\n"})) as open_url:
            self.assertEqual(self.transcriber.transcribe(self.audio), " café\n")
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/transcribe")
        self.assertEqual(request.method, "POST")
        headers = dict((key.lower(), value) for key, value in request.header_items())
        self.assertEqual(headers["authorization"], "Bearer old-token")
        self.assertEqual(headers["chatgpt-account-id"], "account")
        self.assertEqual(headers["originator"], "Codex Desktop")
        self.assertEqual(headers["user-agent"], "Codex Voice Linux/0.2.0")
        self.assertEqual(headers["accept"], "application/json")
        boundary = headers["content-type"].split("boundary=")[1]
        self.assertEqual(request.data, (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="recording.wav"\r\n'
            'Content-Type: audio/wav\r\n\r\n'
        ).encode() + self.audio.read_bytes() + f"\r\n--{boundary}--\r\n".encode())
        self.assertEqual(open_url.call_args.kwargs["timeout"], 60)

    def test_invalid_response_never_becomes_transcript(self):
        for payload in ([], {}, {"text": None}, {"text": 42}, {"text": {"message": "no"}}):
            with self.subTest(payload=payload), patch.object(
                transcription.urllib.request, "urlopen", return_value=self.response(payload)
            ):
                with self.assertRaisesRegex(RuntimeError, "text string"):
                    self.transcriber.transcribe(self.audio)
        for body in (b"not JSON", b"\xff"):
            with self.subTest(body=body), patch.object(
                transcription.urllib.request, "urlopen", return_value=io.BytesIO(body)
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                    self.transcriber.transcribe(self.audio)

    def test_empty_speech_is_a_valid_response(self):
        with patch.object(transcription.urllib.request, "urlopen", return_value=self.response({"text": ""})):
            self.assertEqual(self.transcriber.transcribe(self.audio), "")

    def test_network_and_http_failures_do_not_refresh(self):
        for error, message in (
            (urllib.error.URLError("offline"), "Network error"),
            (TimeoutError("timed out"), "Network error"),
            (urllib.error.HTTPError("https://example.invalid", 503, "Unavailable", {}, io.BytesIO(b"overloaded")), "overloaded"),
        ):
            with self.subTest(error=error), patch.object(transcription.urllib.request, "urlopen", side_effect=error) as open_url:
                with self.assertRaisesRegex(RuntimeError, message):
                    self.transcriber.transcribe(self.audio)
                self.assertEqual(open_url.call_count, 1)

    def test_audio_permission_failure_does_not_trigger_auth_refresh(self):
        with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(RuntimeError, "Could not read audio"):
                self.transcriber.transcribe(self.audio)

    def make_server(self, source):
        server = self.root / "codex"
        server.write_text(f"#!{sys.executable}\n" + source)
        server.chmod(0o700)
        self.transcriber.codex_cli_path = str(server)

    def refresh(self, responses=None):
        processes = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        try:
            with patch.object(transcription, "REFRESH_TIMEOUT_SECONDS", 0.3), patch.object(
                transcription.subprocess, "Popen", side_effect=launch
            ), patch.object(transcription.urllib.request, "urlopen", side_effect=(
                responses if responses is not None else [self.unauthorized(), self.response({"text": "success"})]
            )):
                return self.transcriber.transcribe(self.audio)
        finally:
            for process in processes:
                self.assertIsNotNone(process.returncode)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(process.pid, os.WNOHANG)
                self.assertTrue(process.stdin.closed)
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)

    def successful_server(self):
        # This checks our handshake order and persisted-token reread, not server compatibility.
        self.make_server("\n".join([
            "import json, sys, time",
            "from pathlib import Path",
            "initialize = json.loads(sys.stdin.readline())",
            "assert initialize['method'] == 'initialize'",
            "assert initialize['params']['clientInfo']['version'] == '0.2.0'",
            "print(json.dumps({'id': initialize['id'], 'result': {}}), flush=True)",
            "assert json.loads(sys.stdin.readline()) == {'method': 'initialized'}",
            "account = json.loads(sys.stdin.readline())",
            "assert account['method'] == 'account/read' and account['params'] == {'refreshToken': True}",
            f"Path({str(self.auth)!r}).write_text(json.dumps({{'tokens': {{'access_token': 'new-token', 'account_id': 'account'}}}}))",
            "print(json.dumps({'id': account['id'], 'result': {'account': None}}), flush=True)",
            "time.sleep(30)",
        ]))

    def test_refresh_handshake_rereads_credentials_and_reaps_server(self):
        self.successful_server()
        requests = []

        def respond(request, **kwargs):
            requests.append(request)
            if len(requests) == 1:
                raise self.unauthorized()
            self.assertEqual(request.get_header("Authorization"), "Bearer new-token")
            return self.response({"text": "success"})

        self.assertEqual(self.refresh(respond), "success")
        self.assertEqual(len(requests), 2)

    def test_refresh_retries_only_once(self):
        self.successful_server()
        with self.assertRaisesRegex(RuntimeError, "unauthorized after auth refresh"):
            self.refresh([self.unauthorized(), self.unauthorized()])

    def test_silent_partial_line_and_full_stderr_servers_are_bounded(self):
        for action in ("pass", "os.write(1, b'{')", "os.write(2, b'x' * 200000)"):
            with self.subTest(action=action):
                self.make_server(f"import os, time\n{action}\ntime.sleep(30)\n")
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    self.refresh()
                self.assertLess(time.monotonic() - started, 2)

    def test_refresh_errors_and_early_exit_reap_server(self):
        for source, message in (
            ("pass\n", "closed before confirming"),
            ("import json\nprint(json.dumps({'id': 1, 'error': {'message': 'denied'}}), flush=True)\n", "rejected"),
        ):
            with self.subTest(source=source):
                self.make_server(source)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.refresh()

    def test_interrupted_refresh_reaps_server(self):
        self.make_server("import time\ntime.sleep(30)\n")
        with patch.object(Transcriber, "_refresh_session", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.refresh()

    def test_missing_cli_is_a_domain_error(self):
        with self.assertRaisesRegex(RuntimeError, "CLI not found"):
            self.refresh()
        self.transcriber.codex_cli_path = str(self.root / "missing-codex")
        with self.assertRaisesRegex(RuntimeError, "Could not refresh"):
            self.refresh()


if __name__ == "__main__":
    unittest.main()
