"""Append-only scoreboard: one JSON object per line, never rewritten.

A run adds one line. Nothing here opens the file for writing in any other
mode, so earlier results cannot be edited or reordered by the harness.
"""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def commit():
    """The checked-out commit, or None outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def append(path, row):
    """Add one row as one line. Returns the row as written."""
    row = dict(row)
    row.setdefault("at", datetime.now(UTC).isoformat())
    row.setdefault("commit", commit())
    line = json.dumps(row, sort_keys=True, default=str, ensure_ascii=False)
    if "\n" in line or "\r" in line:
        raise ValueError("a scoreboard row must be one line")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A previous writer that died mid-line must not glue two rows together.
    separator = ""
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as existing:
            existing.seek(-1, os.SEEK_END)
            if existing.read(1) != b"\n":
                separator = "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as board:
        board.write(separator + line + "\n")
    return row


def read(path):
    """Every complete row, oldest first."""
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text("utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows
