import os
import shutil
import subprocess


def insert_text(text: str) -> None:
    # Never type the transcript as key events: compositor bindings can consume
    # individual letters even when wtype reports success.
    shortcut = os.environ.get("CODEX_VOICE_PASTE_SHORTCUT", "ctrl+shift+v")
    if shortcut not in {"ctrl+shift+v", "ctrl+v"}:
        raise RuntimeError("CODEX_VOICE_PASTE_SHORTCUT must be ctrl+shift+v or ctrl+v.")

    wl_copy = shutil.which("wl-copy")
    wtype = shutil.which("wtype")
    if not wl_copy or not wtype:
        raise RuntimeError("Transcript insertion requires wl-copy and wtype.")
    modifiers = shortcut.split("+")[:-1]
    command = [wtype]
    for modifier in modifiers:
        command.extend(["-M", modifier])
    command.extend(["-k", "v"])
    for modifier in reversed(modifiers):
        command.extend(["-m", modifier])

    try:
        subprocess.run(
            [wl_copy, "--type", "text/plain;charset=utf-8"],
            input=text.encode("utf-8"), check=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Could not copy transcript to the clipboard; nothing was pasted.") from exc

    # Leave the transcript available. Timed restoration races asynchronous paste
    # requests and can replace the transcript before the receiving app reads it.
    try:
        subprocess.run(command, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Paste failed. The transcript is on the clipboard; paste it manually.") from exc
