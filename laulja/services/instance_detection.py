"""Detect other running laulja processes (e.g. on another terminal).

Used to warn the user when they request a play from a laulja instance while
another laulja process is already running — those plays often don't take
effect in the instance you're looking at.
"""
from __future__ import annotations

import os
import subprocess
from typing import List

_PROC_DIR = "/proc"


def _scan_proc() -> List[int]:
    """Scan /proc for cmdlines mentioning laulja (Linux fallback)."""
    pids: List[int] = []
    try:
        entries = os.listdir(_PROC_DIR)
    except OSError:
        return []
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"{_PROC_DIR}/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
        if "laulja" in cmdline and pid != os.getpid():
            pids.append(pid)
    return pids


def other_laulja_pids() -> List[int]:
    """PIDs of other laulja processes (this one excluded). Best effort."""
    found: List[int] = []
    try:
        out = subprocess.run(["pgrep", "-f", "laulja"], capture_output=True, text=True, timeout=5)
        for token in out.stdout.split():
            if token.strip().lstrip("-").isdigit():
                pid = int(token.strip())
                if pid != os.getpid():
                    found.append(pid)
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        found = _scan_proc()
    return sorted(set(found))
