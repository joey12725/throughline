#!/bin/sh
# Leak guard: fail when the tree contains accidental secrets, unreplaced
# release placeholders, or any patterns configured in a local private
# pattern file (scripts/leak-patterns.private.txt, or $LEAK_PATTERNS_FILE /
# $LEAK_PATTERN). Run it from anywhere in a checkout, or point it at another
# directory (an export):
#
#   scripts/leak-check.sh            the checkout this script lives in
#   scripts/leak-check.sh DIR        any directory (git or not)
#
# Every hit must be fixed or listed in scripts/leak-allowlist.txt with a
# reason. Exit 0 when clean, 1 with the remaining hits printed.
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
root="${1:-$here/..}"
allowlist="${LEAK_ALLOWLIST:-$here/leak-allowlist.txt}"
[ -f "$allowlist" ] || allowlist="$root/scripts/leak-allowlist.txt"
private_allowlist="$here/leak-allowlist.private.txt"
patterns_file="${LEAK_PATTERNS_FILE:-$here/leak-patterns.private.txt}"

exec python3 -I - "$root" "$allowlist" "$private_allowlist" "$patterns_file" <<'PY'
import os
import re
import subprocess
import sys

root = os.path.abspath(sys.argv[1])
allowlist_paths = [sys.argv[2], sys.argv[3]]
patterns_file = sys.argv[4]

# Built-in secret patterns (always active).
PATTERNS = [
    re.compile(
        r"AKIA[0-9A-Z]{16}"
        r"|AIza[0-9A-Za-z_-]{35}"
        r"|xox[baprs]-[0-9A-Za-z-]{10,}"
        r"|ghp_[A-Za-z0-9]{20,}"
        r"|gho_[A-Za-z0-9]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}"
        r"|glpat-[A-Za-z0-9_-]{20,}"
        r"|-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
    ),
]

# In a public tree (no private working plan), unreplaced repo placeholders
# are a release error. Split so export-public.sh never rewrites this script.
PLACEHOLDER = "@GITHUB" "_REPO@"
if not os.path.exists(os.path.join(root, "OPEN_SOURCE_PLAN.md")):
    PATTERNS.append(re.compile(re.escape(PLACEHOLDER)))

if os.path.isfile(patterns_file):
    for raw in open(patterns_file, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        PATTERNS.append(re.compile(line, re.I))

extra = os.environ.get("LEAK_PATTERN", "").strip()
if extra:
    PATTERNS.append(re.compile(extra, re.I))

skip_paths, mask_words, allow_lines = [], [], []
for ap in allowlist_paths:
    if not ap or not os.path.isfile(ap):
        continue
    for raw in open(ap, encoding="utf-8"):
        line = raw.split("  #", 1)[0].rstrip("\n").strip()
        if not line or line.startswith("#"):
            continue
        kind, _, value = line.partition(":")
        value = value.strip()
        if kind == "path":
            skip_paths.append(re.compile(value))
        elif kind == "word":
            mask_words.append(re.compile(value, re.I))
        elif kind == "line":
            allow_lines.append(re.compile(value))
        else:
            sys.exit(f"leak-check: bad allowlist entry in {ap}: {raw.strip()}")


def files():
    try:
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            check=True, capture_output=True,
        ).stdout
        names = [n for n in out.decode().split("\0") if n]
        if names:
            return names
    except (OSError, subprocess.CalledProcessError):
        pass
    names = []
    for base, dirs, filenames in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv")]
        for name in filenames:
            names.append(os.path.relpath(os.path.join(base, name), root))
    return names


hits = 0
for rel in sorted(files()):
    rel = rel.replace(os.sep, "/")
    if any(p.search(rel) for p in skip_paths):
        continue
    path = os.path.join(root, rel)
    if not os.path.isfile(path) or os.path.islink(path):
        continue
    # File names count too (a path can leak as easily as its content).
    candidates = [(0, rel)]
    with open(path, "rb") as handle:
        data = handle.read()
    if b"\0" not in data[:8192]:
        text = data.decode("utf-8", "replace")
        candidates += list(enumerate(text.splitlines(), 1))
    for number, line in candidates:
        if not any(pat.search(line) for pat in PATTERNS):
            continue
        masked = line
        for word in mask_words:
            masked = word.sub("", masked)
        if not any(pat.search(masked) for pat in PATTERNS):
            continue
        where = f"{rel}:{number}:{line.strip()}"
        if any(p.search(where) for p in allow_lines):
            continue
        hits += 1
        print(where[:300])

if hits:
    print(f"leak-check: {hits} hit(s); fix them or allowlist them with a reason",
          file=sys.stderr)
    sys.exit(1)
print("leak-check: clean", file=sys.stderr)
PY
