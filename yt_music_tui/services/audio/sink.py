from __future__ import annotations

import shutil
import subprocess
from typing import List, Optional

"""
Launches a system audio-output process that accepts raw 16-bit PCM on stdin.
Tries PipeWire, then PulseAudio, then plain ALSA. Mirrors AudioSink.cs.
"""


def start(sample_rate: int, channels: int) -> Optional[subprocess.Popen]:
    return (
        _try_start(
            "pw-play",
            [
                "--container", "raw", "--format", "s16",
                "--rate", str(sample_rate), "--channels", str(channels), "-",
            ],
        )
        or _try_start(
            "paplay",
            ["--raw", f"--rate={sample_rate}", f"--channels={channels}", "--format=s16le"],
        )
        or _try_start(
            "aplay",
            ["-q", "-f", "S16_LE", "-r", str(sample_rate), "-c", str(channels)],
        )
    )


def _try_start(command: str, args: List[str]) -> Optional[subprocess.Popen]:
    path = shutil.which(command)
    if not path:
        return None
    try:
        return subprocess.Popen(
            [path, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
