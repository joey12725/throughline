"""Query allow-list additions, one JSON file per migration.

A file NNNN_name.json next to this module holds
{"functions": [...], "scalar": [...]}: names of functions in schema api that
validated SQL may call. "functions" return rows (composite); "scalar" return
one value. A malformed file stops the process: an allow-list is never guessed.
"""

import json
import re
from pathlib import Path

NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def _names(path, data, key):
    names = data.get(key, [])
    if not isinstance(names, list) or not all(
        isinstance(n, str) and NAME.fullmatch(n) for n in names
    ):
        raise ValueError(f"{path.name}: {key} must be a list of lower-case names")
    return set(names)


def load(directory=None):
    """Return (row-returning names, scalar names) from every *.json file."""
    directory = Path(directory) if directory is not None else Path(__file__).parent
    functions, scalar = set(), set()
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{path.name}: not valid JSON ({exc})") from None
        if not isinstance(data, dict) or set(data) - {"functions", "scalar"}:
            raise ValueError(f"{path.name}: expected an object with functions, scalar")
        rows, single = _names(path, data, "functions"), _names(path, data, "scalar")
        if rows & single:
            raise ValueError(f"{path.name}: a name cannot be both row and scalar")
        functions |= rows
        scalar |= single
    if functions & scalar:
        raise ValueError("allow-list: a name cannot be both row and scalar")
    return functions, scalar
