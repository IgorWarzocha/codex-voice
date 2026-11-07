#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import asdict
from pathlib import Path

from insertion import insert_text
from recording import CHANNELS, MAX_RECORDING_SECONDS, MIN_RECORDING_SECONDS, SAMPLE_RATE, Recordings
from transcription import Transcriber


APP_NAME = "Codex Voice"


def notify(body: str, urgency: str = "low") -> None:
    command = shutil.which("notify-send")
    if command:
        try:
            subprocess.run([command, "-a", APP_NAME, "-u", urgency, APP_NAME, body],
                           check=True, timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"warning: notification unavailable: {exc}", file=sys.stderr)


def report_error(error: Exception) -> None:
    notify(str(error), urgency="critical")
    print(f"error: {error}", file=sys.stderr)


def default_audio_source() -> str:
    override = os.environ.get("CODEX_VOICE_AUDIO_SOURCE")
    if override:
        return override
    pactl = shutil.which("pactl")
    if pactl:
        result = subprocess.run([pactl, "get-default-source"], capture_output=True, text=True, check=True, timeout=3)
        if result.stdout.strip():
            return result.stdout.strip()
    return "default"


def stop_recording(recordings: Recordings, transcriber: Transcriber) -> int:
    recording = recordings.begin_transcription()
    if recording is None:
        return 0
    audio_path = Path(recording.audio_path)
    try:
        if not audio_path.exists():
            raise RuntimeError("The recorder produced no audio. Check your microphone source.")
        with wave.open(str(audio_path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        if duration < MIN_RECORDING_SECONDS:
            recordings.deliver(recording, lambda: notify("Recording too short", urgency="normal"))
            return 0
        message = "Transcribing..."
        if time.time() - recording.started_at >= MAX_RECORDING_SECONDS:
            message = "Recording reached the five-minute limit. Transcribing..."
        if not recordings.deliver(recording, lambda: notify(message)):
            return 0
        transcript = transcriber.transcribe(audio_path).strip()
        if transcript:
            recordings.deliver(recording, lambda: insert_text(transcript))
        else:
            recordings.deliver(recording, lambda: notify("No speech detected", urgency="normal"))
        return 0
    except (RuntimeError, OSError, ValueError, EOFError, wave.Error, subprocess.SubprocessError) as exc:
        # Cancellation also suppresses a late error from the abandoned request.
        return 1 if recordings.deliver(recording, lambda: report_error(exc)) else 0
    finally:
        recordings.finish(recording)


def probe(transcriber: Transcriber) -> int:
    required = ["pw-record", "timeout", "wtype", "wl-copy"]
    dependencies = {name: bool(shutil.which(name)) for name in [*required, "codex", "notify-send", "pactl"]}
    print(json.dumps({"dependencies": dependencies, "audio_source": default_audio_source()}, indent=2))
    if not all(dependencies[name] for name in required):
        raise RuntimeError("One or more required dependencies are missing.")
    transcriber.read_credentials()
    with tempfile.TemporaryDirectory(prefix="codex-voice-probe-") as directory:
        path = Path(directory) / "silence.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(CHANNELS)
            audio.setsampwidth(2)
            audio.setframerate(SAMPLE_RATE)
            audio.writeframes(b"\x00\x00" * int(SAMPLE_RATE * 0.3))
        transcript = transcriber.transcribe(path)
        print(json.dumps({"transcribe_probe": "ok", "probe_text_length": len(transcript)}, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex Voice for Omarchy/Wayland")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "stop", "cancel", "status", "probe"):
        subparsers.add_parser(command)
    transcribe_parser = subparsers.add_parser("transcribe-file")
    transcribe_parser.add_argument("path", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    transcriber = Transcriber(
        auth_paths=(Path.home() / ".codex" / "auth.json", Path.home() / ".pi" / "agent" / "auth.json"),
        codex_cli_path=os.environ.get("CODEX_CLI_PATH") or shutil.which("codex"),
    )
    try:
        if args.command == "probe":
            return probe(transcriber)
        if args.command == "transcribe-file":
            print(transcriber.transcribe(args.path))
            return 0
        runtime_root = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/codex-voice-{os.getuid()}")
        recordings = Recordings(runtime_root / "codex-voice")
        if args.command == "start":
            if recordings.start(default_audio_source()):
                notify("Listening... release the hotkey to transcribe")
        elif args.command == "stop":
            return stop_recording(recordings, transcriber)
        elif args.command == "cancel":
            if recordings.cancel():
                notify("Cancelled")
        elif args.command == "status":
            recording = recordings.status()
            print(json.dumps(asdict(recording) if recording else {"status": "idle"}, indent=2))
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        report_error(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
