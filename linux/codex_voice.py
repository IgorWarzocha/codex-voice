#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_NAME = "Codex Voice"
APP_VERSION = "0.2.0"
TRANSCRIBE_ENDPOINT = "https://chatgpt.com/backend-api/transcribe"
DEFAULT_SAMPLE_RATE = 44_100
DEFAULT_CHANNELS = 1
MIN_RECORDING_SECONDS = 0.12


class CodexVoiceError(RuntimeError):
    pass


@dataclass
class Credentials:
    access_token: str
    account_id: str


@dataclass
class Config:
    runtime_dir: Path
    state_path: Path
    lock_path: Path
    auth_paths: tuple[Path, ...]
    codex_cli_path: str | None


def build_config() -> Config:
    runtime_root = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/codex-voice-{os.getuid()}")
    runtime_dir = runtime_root / "codex-voice"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        runtime_dir=runtime_dir,
        state_path=runtime_dir / "state.json",
        lock_path=runtime_dir / "state.lock",
        auth_paths=(
            Path.home() / ".codex" / "auth.json",
            Path.home() / ".pi" / "agent" / "auth.json",
        ),
        codex_cli_path=os.environ.get("CODEX_CLI_PATH") or shutil.which("codex"),
    )


def notify(summary: str, body: str, urgency: str = "low") -> None:
    notify_send = shutil.which("notify-send")
    if not notify_send:
        return

    subprocess.run(
        [notify_send, "-a", APP_NAME, "-u", urgency, summary, body],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class StateLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def load_state(config: Config) -> dict[str, Any] | None:
    if not config.state_path.exists():
        return None
    try:
        return json.loads(config.state_path.read_text())
    except Exception:
        return None


def save_state(config: Config, state: dict[str, Any]) -> None:
    config.state_path.write_text(json.dumps(state, indent=2))


def clear_state(config: Config) -> None:
    try:
        config.state_path.unlink()
    except FileNotFoundError:
        pass


def process_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def ensure_dependency(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise CodexVoiceError(f"Required dependency not found: {name}")
    return path


def default_audio_source() -> str:
    env_value = os.environ.get("CODEX_VOICE_AUDIO_SOURCE")
    if env_value:
        return env_value

    pactl = shutil.which("pactl")
    if pactl:
        result = subprocess.run([pactl, "get-default-source"], capture_output=True, text=True, check=False)
        source = result.stdout.strip()
        if result.returncode == 0 and source:
            return source

    return "default"


def recording_path(config: Config) -> Path:
    return config.runtime_dir / f"recording-{uuid.uuid4().hex}.wav"


def kill_process(pid: int, timeout_seconds: float = 3.0) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not process_is_alive(pid):
            return
        time.sleep(0.05)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def start_recording(config: Config) -> int:
    pw_record = ensure_dependency("pw-record")
    source = default_audio_source()

    with StateLock(config.lock_path):
        state = load_state(config)
        if state and state.get("status") == "recording" and process_is_alive(state.get("pid")):
            return 0

        if state and state.get("audio_path"):
            try:
                Path(state["audio_path"]).unlink()
            except FileNotFoundError:
                pass

        audio_path = recording_path(config)
        process = subprocess.Popen(
            [
                pw_record,
                "--target",
                source,
                "--rate",
                str(DEFAULT_SAMPLE_RATE),
                "--channels",
                str(DEFAULT_CHANNELS),
                "--format",
                "s16",
                str(audio_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        save_state(
            config,
            {
                "status": "recording",
                "pid": process.pid,
                "audio_path": str(audio_path),
                "audio_source": source,
                "started_at": time.time(),
            },
        )

    notify(APP_NAME, "Listening… release Control+M to transcribe")
    return 0


def stop_recording(config: Config) -> int:
    with StateLock(config.lock_path):
        state = load_state(config)
        if not state or state.get("status") != "recording":
            return 0

        pid = int(state.get("pid") or 0)
        audio_path = Path(state.get("audio_path") or "")
        started_at = float(state.get("started_at") or time.time())

        save_state(
            config,
            {
                **state,
                "status": "transcribing",
                "transcribing_started_at": time.time(),
            },
        )

    if pid:
        kill_process(pid)

    duration = time.time() - started_at
    if duration < MIN_RECORDING_SECONDS or not audio_path.exists() or audio_path.stat().st_size == 0:
        try:
            audio_path.unlink()
        except FileNotFoundError:
            pass
        with StateLock(config.lock_path):
            clear_state(config)
        notify(APP_NAME, "Recording too short", urgency="normal")
        return 0

    notify(APP_NAME, "Transcribing…")

    try:
        transcript = transcribe_audio(config, audio_path)
        transcript = transcript.strip()
        if not transcript:
            notify(APP_NAME, "No speech detected", urgency="normal")
            return 0

        insert_text(transcript)
        return 0
    except Exception as exc:
        notify(APP_NAME, str(exc), urgency="critical")
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            audio_path.unlink()
        except FileNotFoundError:
            pass
        with StateLock(config.lock_path):
            clear_state(config)


def cancel_recording(config: Config) -> int:
    with StateLock(config.lock_path):
        state = load_state(config)
        if not state:
            return 0
        pid = int(state.get("pid") or 0)
        audio_path = Path(state.get("audio_path") or "")

    if pid:
        kill_process(pid, timeout_seconds=1.0)
    try:
        audio_path.unlink()
    except FileNotFoundError:
        pass

    with StateLock(config.lock_path):
        clear_state(config)

    notify(APP_NAME, "Cancelled")
    return 0


def read_credentials(config: Config) -> Credentials:
    found_auth = False
    for auth_path in config.auth_paths:
        if not auth_path.exists():
            continue
        found_auth = True
        try:
            data = json.loads(auth_path.read_text())
        except json.JSONDecodeError as exc:
            raise CodexVoiceError(f"Failed to parse {auth_path}: {exc}") from exc

        tokens = data.get("tokens") or {}
        pi_tokens = data.get("openai-codex") or {}
        access_token = tokens.get("access_token") or pi_tokens.get("access")
        account_id = tokens.get("account_id") or pi_tokens.get("accountId")
        if access_token and account_id:
            return Credentials(access_token=access_token, account_id=account_id)

    if not found_auth:
        paths = ", ".join(str(path) for path in config.auth_paths)
        raise CodexVoiceError(f"Codex or Pi auth file not found (checked {paths})")
    raise CodexVoiceError("Codex or Pi auth is incomplete. Please log in again.")


def refresh_credentials(config: Config) -> Credentials:
    if not config.codex_cli_path:
        raise CodexVoiceError("Codex CLI not found for auth refresh.")

    process = subprocess.Popen(
        [config.codex_cli_path, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    messages = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "codex-voice-linux", "version": APP_VERSION},
                "capabilities": {"experimentalApi": True, "optOutNotificationMethods": []},
            },
        },
        {"method": "initialized"},
        {"id": 2, "method": "account/read", "params": {"refreshToken": True}},
    ]

    assert process.stdin is not None
    assert process.stdout is not None

    for message in messages:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    deadline = time.time() + 8
    saw_account_response = False

    while time.time() < deadline:
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                break
            time.sleep(0.05)
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue

        if payload.get("id") == 2 and "result" in payload:
            saw_account_response = True
            break

    try:
        process.terminate()
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()

    if not saw_account_response:
        stderr_output = ""
        if process.stderr is not None:
            stderr_output = process.stderr.read().strip()
        raise CodexVoiceError(stderr_output or "Codex CLI did not confirm auth refresh.")

    return read_credentials(config)


def make_multipart_body(audio_path: Path, boundary: str) -> bytes:
    parts: list[bytes] = []
    header = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{audio_path.name}\"\r\n"
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    parts.append(header)
    parts.append(audio_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts)


def perform_transcription(audio_path: Path, credentials: Credentials) -> str:
    boundary = f"----codex-voice-{uuid.uuid4().hex}"
    body = make_multipart_body(audio_path, boundary)

    request = urllib.request.Request(
        TRANSCRIBE_ENDPOINT,
        data=body,
        method="POST",
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
            return str(payload.get("text") or "")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("unauthorized") from exc
        body_text = exc.read().decode(errors="replace").strip()
        raise CodexVoiceError(body_text or f"Transcription failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise CodexVoiceError(f"Network error while transcribing: {exc}") from exc


def transcribe_audio(config: Config, audio_path: Path) -> str:
    credentials = read_credentials(config)
    try:
        return perform_transcription(audio_path, credentials)
    except PermissionError:
        credentials = refresh_credentials(config)
        return perform_transcription(audio_path, credentials)


def insert_text(text: str) -> None:
    wtype = ensure_dependency("wtype")

    result = subprocess.run([wtype, text], check=False)
    if result.returncode == 0:
        return

    paste_text(text)


def paste_text(text: str) -> None:
    wl_copy = ensure_dependency("wl-copy")
    wl_paste = shutil.which("wl-paste")
    wtype = ensure_dependency("wtype")

    previous_text: bytes | None = None
    if wl_paste:
        previous = subprocess.run([wl_paste, "--no-newline"], capture_output=True, check=False)
        if previous.returncode == 0:
            previous_text = previous.stdout

    subprocess.run([wl_copy], input=text.encode(), check=True)
    time.sleep(0.08)
    paste = subprocess.run([wtype, "-M", "ctrl", "-k", "v", "-m", "ctrl"], check=False)
    if paste.returncode != 0:
        raise CodexVoiceError("Failed to insert transcript with wtype.")

    time.sleep(0.2)
    if previous_text is not None:
        subprocess.run([wl_copy], input=previous_text, check=False)
    else:
        subprocess.run([wl_copy, "--clear"], check=False)


def print_status(config: Config) -> int:
    with StateLock(config.lock_path):
        state = load_state(config)

    if not state:
        print(json.dumps({"status": "idle"}, indent=2))
        return 0

    if state.get("status") == "recording" and not process_is_alive(state.get("pid")):
        clear_state(config)
        print(json.dumps({"status": "idle"}, indent=2))
        return 0

    print(json.dumps(state, indent=2))
    return 0


def probe(config: Config) -> int:
    dependencies = {
        name: bool(shutil.which(name))
        for name in ["codex", "pw-record", "wtype", "wl-copy", "notify-send", "pactl"]
    }
    print(json.dumps({"dependencies": dependencies, "audio_source": default_audio_source()}, indent=2))

    if not all(dependencies[name] for name in ["codex", "pw-record", "wtype", "wl-copy"]):
        raise CodexVoiceError("One or more required dependencies are missing.")

    read_credentials(config)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        temp_path = Path(handle.name)

    try:
        create_silence_wav(temp_path, seconds=0.3)
        transcript = transcribe_audio(config, temp_path)
        print(json.dumps({"transcribe_probe": "ok", "probe_text_length": len(transcript)}, indent=2))
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass

    return 0


def create_silence_wav(path: Path, seconds: float) -> None:
    import wave

    frame_count = int(DEFAULT_SAMPLE_RATE * seconds)
    silence = b"\x00\x00" * frame_count

    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(DEFAULT_CHANNELS)
        wav_file.setsampwidth(2)
        wav_file.setframerate(DEFAULT_SAMPLE_RATE)
        wav_file.writeframes(silence)


def transcribe_file(config: Config, path: Path) -> int:
    if not path.exists():
        raise CodexVoiceError(f"Audio file not found: {path}")
    print(transcribe_audio(config, path))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex Voice for Omarchy/Wayland")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start")
    subparsers.add_parser("stop")
    subparsers.add_parser("cancel")
    subparsers.add_parser("status")
    subparsers.add_parser("probe")
    transcribe_parser = subparsers.add_parser("transcribe-file")
    transcribe_parser.add_argument("path", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = build_config()

    try:
        if args.command == "start":
            return start_recording(config)
        if args.command == "stop":
            return stop_recording(config)
        if args.command == "cancel":
            return cancel_recording(config)
        if args.command == "status":
            return print_status(config)
        if args.command == "probe":
            return probe(config)
        if args.command == "transcribe-file":
            return transcribe_file(config, args.path)
        raise CodexVoiceError(f"Unknown command: {args.command}")
    except CodexVoiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
