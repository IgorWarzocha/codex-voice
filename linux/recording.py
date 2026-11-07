from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, Literal


SAMPLE_RATE = 44_100
CHANNELS = 1
MAX_RECORDING_SECONDS = 300
MIN_RECORDING_SECONDS = 0.12
RECORDING_NAME = re.compile(r"recording-[0-9a-f]{32}\.wav")


def process_identity(pid: int) -> str | None:
    """Start time distinguishes a recorder from an unrelated reused PID."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    return None if fields[0] == "Z" else fields[19]


@dataclass(frozen=True)
class Recording:
    status: Literal["recording", "transcribing"]
    pid: int
    pid_start: str | None
    audio_path: str
    audio_source: str
    started_at: float
    worker_pid: int | None = None
    worker_start: str | None = None
    transcribing_started_at: float | None = None

    @property
    def capture_alive(self) -> bool:
        return self.pid_start is not None and process_identity(self.pid) == self.pid_start

    @property
    def worker_alive(self) -> bool:
        return self.worker_pid is not None and self.worker_start is not None and process_identity(self.worker_pid) == self.worker_start


class Recordings:
    """Own the one active session, its recorder process group and temporary files."""

    def __init__(self, runtime_dir: Path):
        self.directory = runtime_dir
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path = runtime_dir / "state.json"
        self.lock_path = runtime_dir / "state.lock"
        self._local_recorder: subprocess.Popen[bytes] | None = None

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _load(self) -> Recording | None:
        try:
            payload = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid recording state: {self.state_path}") from exc

        try:
            if not isinstance(payload, dict):
                raise ValueError("expected an object")
            status = payload["status"]
            pid = payload["pid"]
            audio_path = Path(payload["audio_path"])
            started_at = payload["started_at"]
            if status not in {"recording", "transcribing"}:
                raise ValueError("invalid status")
            if type(pid) is not int or pid <= 1:
                raise ValueError("invalid recorder PID")
            if audio_path.parent != self.directory or not RECORDING_NAME.fullmatch(audio_path.name):
                raise ValueError("audio path is outside the recording store")
            if type(started_at) not in {int, float} or not math.isfinite(started_at):
                raise ValueError("invalid start time")
            pid_start = payload.get("pid_start")
            worker_pid = payload.get("worker_pid")
            worker_start = payload.get("worker_start")
            if worker_pid is not None and (type(worker_pid) is not int or worker_pid <= 1):
                raise ValueError("invalid worker PID")
            if any(value is not None and (not isinstance(value, str) or not value.isdecimal()) for value in (pid_start, worker_start)):
                raise ValueError("invalid process identity")
            if not isinstance(payload.get("audio_source"), str):
                raise ValueError("invalid audio source")
            # Read the old on-disk format only when the process still identifies
            # itself as this exact recorder. Never signal an unverified old PID.
            if pid_start is None:
                try:
                    command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                except FileNotFoundError:
                    command = []
                if command and Path(os.fsdecode(command[0])).name == "pw-record" and os.fsencode(audio_path) in command:
                    pid_start = process_identity(pid)
            return Recording(status, pid, pid_start, str(audio_path), payload["audio_source"], started_at,
                             worker_pid, worker_start, payload.get("transcribing_started_at"))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid recording state in {self.state_path}: {exc}") from exc

    def _save(self, recording: Recording) -> None:
        temporary = self.directory / ".state.json.tmp"
        try:
            temporary.write_text(json.dumps(asdict(recording), indent=2))
            temporary.replace(self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _stop_capture(self, recording: Recording) -> None:
        try:
            if not recording.capture_alive:
                return
            # New captures have a timeout supervisor as group leader. Legacy
            # pw-record captures also used start_new_session=True.
            try:
                if os.getpgid(recording.pid) != recording.pid:
                    raise RuntimeError("Refusing to stop a recorder outside its own process group.")
                os.killpg(recording.pid, signal.SIGINT)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + 2
            while recording.capture_alive and time.monotonic() < deadline:
                time.sleep(0.02)
            if recording.capture_alive:
                try:
                    os.killpg(recording.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            if self._local_recorder is not None and self._local_recorder.pid == recording.pid:
                self._local_recorder.wait(timeout=3)
                self._local_recorder = None

    def _discard(self, recording: Recording) -> None:
        self._stop_capture(recording)
        Path(recording.audio_path).unlink(missing_ok=True)
        self.state_path.unlink(missing_ok=True)

    def _prune(self, keep: str | None = None) -> None:
        for path in self.directory.glob("recording-*.wav"):
            if RECORDING_NAME.fullmatch(path.name) and str(path) != keep:
                path.unlink(missing_ok=True)
        (self.directory / ".state.json.tmp").unlink(missing_ok=True)

    def start(self, source: str) -> bool:
        recorder = shutil.which("pw-record")
        timeout = shutil.which("timeout")
        if not recorder or not timeout:
            raise RuntimeError("Recording requires pw-record and GNU timeout.")
        with self.locked():
            current = self._load()
            if current:
                if current.status == "recording" and current.capture_alive:
                    return False
                # A fresh press supersedes pending transcription. Its worker
                # may still return, but deliver/finish only affect its own session.
                self._discard(current)
            self._prune()
            audio_path = self.directory / f"recording-{uuid.uuid4().hex}.wav"
            # The timeout outlives this short CLI invocation. A lost key release
            # cannot leave an unbounded capture running in the background.
            process = subprocess.Popen(
                [timeout, "--signal=INT", "--kill-after=2s", f"{MAX_RECORDING_SECONDS}s", recorder,
                 "--target", source, "--rate", str(SAMPLE_RATE), "--channels", str(CHANNELS),
                 "--format", "s16", str(audio_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
            self._local_recorder = process
            recording = Recording("recording", process.pid, process_identity(process.pid), str(audio_path), source, time.time())
            try:
                if recording.pid_start is None:
                    raise RuntimeError("Audio recorder failed to start.")
                self._save(recording)
            except BaseException:
                self._stop_capture(recording)
                audio_path.unlink(missing_ok=True)
                raise
        return True

    def begin_transcription(self) -> Recording | None:
        with self.locked():
            current = self._load()
            if current is None or current.status != "recording":
                return None
            self._stop_capture(current)
            recording = replace(current, status="transcribing", worker_pid=os.getpid(),
                                worker_start=process_identity(os.getpid()), transcribing_started_at=time.time())
            try:
                self._save(recording)
            except BaseException:
                self._discard(current)
                raise
            return recording

    def deliver(self, recording: Recording, insert: Callable[[], None]) -> bool:
        # Cancellation and delivery are ordered by the same lock. An in-flight
        # network response cannot paste into a later session after cancellation.
        with self.locked():
            current = self._load()
            if current is None or current.audio_path != recording.audio_path:
                return False
            insert()
            return True

    def finish(self, recording: Recording) -> None:
        with self.locked():
            current = self._load()
            Path(recording.audio_path).unlink(missing_ok=True)
            if current and current.audio_path == recording.audio_path:
                self.state_path.unlink(missing_ok=True)

    def cancel(self) -> bool:
        with self.locked():
            current = self._load()
            if current:
                self._discard(current)
            self._prune()
            return current is not None

    def status(self) -> Recording | None:
        with self.locked():
            current = self._load()
            if current and not (current.capture_alive if current.status == "recording" else current.worker_alive):
                self._discard(current)
                current = None
            self._prune(current.audio_path if current else None)
            return current
