from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path


APP_VERSION = "0.2.0"
TRANSCRIBE_ENDPOINT = "https://chatgpt.com/backend-api/transcribe"
REFRESH_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class Credentials:
    access_token: str
    account_id: str


class Transcriber:
    def __init__(self, auth_paths: tuple[Path, ...], codex_cli_path: str | None):
        self.auth_paths = auth_paths
        self.codex_cli_path = codex_cli_path

    def read_credentials(self) -> Credentials:
        found_auth = False
        for auth_path in self.auth_paths:
            try:
                contents = auth_path.read_text()
            except FileNotFoundError:
                continue
            except (OSError, UnicodeError) as exc:
                raise RuntimeError(f"Failed to read {auth_path}: {exc}") from exc
            found_auth = True
            try:
                data = json.loads(contents)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Failed to parse {auth_path}: {exc}") from exc
            if not isinstance(data, dict):
                raise RuntimeError(f"Invalid auth object in {auth_path}")
            tokens = data.get("tokens")
            pi_tokens = data.get("openai-codex")
            if any(value is not None and not isinstance(value, dict) for value in (tokens, pi_tokens)):
                raise RuntimeError(f"Invalid auth tokens in {auth_path}")
            tokens = tokens or {}
            pi_tokens = pi_tokens or {}
            access_token = tokens.get("access_token") or pi_tokens.get("access")
            account_id = tokens.get("account_id") or pi_tokens.get("accountId")
            for value in (access_token, account_id):
                if value is not None and (
                    not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value
                ):
                    raise RuntimeError(f"Invalid auth credentials in {auth_path}")
            if access_token and account_id:
                return Credentials(access_token, account_id)
        if not found_auth:
            paths = ", ".join(str(path) for path in self.auth_paths)
            raise RuntimeError(f"Codex or Pi auth file not found (checked {paths})")
        raise RuntimeError("Codex or Pi auth is incomplete. Please log in again.")

    def transcribe(self, audio_path: Path) -> str:
        credentials = self.read_credentials()
        try:
            return self._perform_transcription(audio_path, credentials)
        except PermissionError:
            credentials = self._refresh_credentials()
        try:
            return self._perform_transcription(audio_path, credentials)
        except PermissionError as exc:
            raise RuntimeError("Transcription unauthorized after auth refresh. Please log in again.") from exc

    def _refresh_credentials(self) -> Credentials:
        if not self.codex_cli_path:
            raise RuntimeError("Codex CLI not found for auth refresh.")
        try:
            with subprocess.Popen(
                [self.codex_cli_path, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ) as process:
                try:
                    self._refresh_session(process)
                finally:
                    # Killing before waiting also handles a server stuck writing stderr.
                    process.kill()
                    process.wait()
        except OSError as exc:
            raise RuntimeError(f"Could not refresh Codex auth: {exc}") from exc
        return self.read_credentials()

    @staticmethod
    def _refresh_session(process: subprocess.Popen) -> None:
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        initialize = {
            "id": 1, "method": "initialize",
            "params": {
                "clientInfo": {"name": "codex-voice-linux", "version": APP_VERSION},
                "capabilities": {"experimentalApi": True, "optOutNotificationMethods": []},
            },
        }
        deadline = time.monotonic() + REFRESH_TIMEOUT_SECONDS
        pending = (json.dumps(initialize) + "\n").encode()
        output = b""
        errors = b""
        expected_id = 1
        with selectors.DefaultSelector() as selector:
            # Nonblocking IO keeps even partial lines, full stderr, and stalled stdin bounded.
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                for key, _ in selector.select(max(0, deadline - time.monotonic())):
                    if key.fileobj is process.stdin:
                        pending = pending[os.write(key.fd, pending):]
                        if not pending:
                            selector.unregister(process.stdin)
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if key.fileobj is process.stdout:
                            raise RuntimeError(errors.decode(errors="replace").strip() or "Codex CLI closed before confirming auth refresh.")
                        continue
                    if key.fileobj is process.stderr:
                        errors = (errors + chunk)[-4096:]
                        continue
                    output += chunk
                    if len(output) > 1_048_576:
                        raise RuntimeError("Codex CLI auth response is too large.")
                    while b"\n" in output:
                        line, output = output.split(b"\n", 1)
                        try:
                            payload = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if not isinstance(payload, dict) or payload.get("id") != expected_id:
                            continue
                        if "error" in payload or not isinstance(payload.get("result"), dict):
                            raise RuntimeError("Codex CLI rejected auth refresh.")
                        if expected_id == 2:
                            return
                        expected_id = 2
                        pending = (
                            json.dumps({"method": "initialized"}) + "\n"
                            + json.dumps({"id": 2, "method": "account/read", "params": {"refreshToken": True}}) + "\n"
                        ).encode()
                        selector.register(process.stdin, selectors.EVENT_WRITE)
        raise RuntimeError("Codex CLI auth refresh timed out.")

    @staticmethod
    def _perform_transcription(audio_path: Path, credentials: Credentials) -> str:
        boundary = f"----codex-voice-{uuid.uuid4().hex}"
        try:
            audio = audio_path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"Could not read audio file {audio_path}: {exc}") from exc
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{audio_path.name}"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode() + audio + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            TRANSCRIBE_ENDPOINT, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {credentials.access_token}",
                "ChatGPT-Account-Id": credentials.account_id,
                "originator": "Codex Desktop",
                "User-Agent": f"Codex Voice Linux/{APP_VERSION}",
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            with exc:
                if exc.code == 401:
                    raise PermissionError("unauthorized") from exc
                body_text = exc.read().decode(errors="replace").strip()
            raise RuntimeError(body_text or f"Transcription failed with HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(f"Network error while transcribing: {exc}") from exc
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("Transcription returned invalid JSON.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise RuntimeError("Transcription response must contain a text string.")
        return payload["text"]
