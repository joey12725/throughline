#!/usr/bin/env python3
"""Check that every relative link in the Markdown docs resolves.

    python3 scripts/check-links.py [ROOT]

Checks the top-level *.md files, docs/, sdk/ and web/: each relative link
(inline, reference-style or an <a href>/<img src>) must name a file or
directory that exists, and a #fragment must match a heading of the target
(GitHub's anchor rules). External links (http:, https:, mailto:) are not
fetched. Links inside code blocks and inline code are ignored. Exit 1 with
one line per broken link.
"""

import os
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
SCOPES = ["*.md", "docs/**/*.md", "sdk/**/*.md", "web/*.md"]
SKIP_DIRS = {"node_modules", ".venv", ".git"}

INLINE = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFDEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$", re.M)
HTML = re.compile(r"<(?:a|img)\b[^>]*?\b(?:href|src)=\"([^\"]+)\"", re.I)
FENCE = re.compile(r"^(```|~~~)")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
EXPLICIT_ANCHOR = re.compile(r"<a\s+(?:name|id)=\"([^\"]+)\"", re.I)


def strip_code(text):
    """The text with fenced blocks blanked and inline code removed (line
    numbers kept)."""
    out, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line.lstrip()):
            fenced = not fenced
            out.append("")
            continue
        out.append("" if fenced else re.sub(r"`+[^`]*`+", "", line))
    return out


def slug(heading):
    text = re.sub(r"<[^>]+>", "", heading)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links -> text
    text = text.replace("`", "")
    text = unicodedata.normalize("NFKC", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


_anchors = {}


def anchors(path):
    if path not in _anchors:
        found, seen = set(), {}
        for line in strip_code(path.read_text(encoding="utf-8", errors="replace")):
            m = HEADING.match(line)
            if m:
                base = slug(m.group(2))
                n = seen.get(base, 0)
                seen[base] = n + 1
                found.add(base if n == 0 else f"{base}-{n}")
            found.update(EXPLICIT_ANCHOR.findall(line))
        _anchors[path] = found
    return _anchors[path]


def files():
    seen = set()
    for pattern in SCOPES:
        for path in sorted(ROOT.glob(pattern)):
            if any(part in SKIP_DIRS for part in path.parts) or path in seen:
                continue
            seen.add(path)
            yield path


def main():
    broken = checked = 0
    for path in files():
        lines = strip_code(path.read_text(encoding="utf-8", errors="replace"))
        for number, line in enumerate(lines, 1):
            targets = INLINE.findall(line) + HTML.findall(line) + REFDEF.findall(line)
            for target in targets:
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("//"):
                    continue  # external (http:, mailto:, ...)
                if "@GITHUB" "_REPO@" in target:  # filled in by export-public.sh
                    continue
                checked += 1
                ref, _, fragment = target.partition("#")
                ref = ref.split("?", 1)[0]
                dest = path if not ref else (path.parent / ref)
                if ref.startswith("/"):
                    dest = ROOT / ref.lstrip("/")
                dest = Path(os.path.normpath(dest))
                where = f"{path.relative_to(ROOT)}:{number}: {target}"
                if not dest.exists():
                    print(f"{where}  (missing {os.path.relpath(dest, ROOT)})")
                    broken += 1
                elif fragment and dest.is_file() and dest.suffix == ".md":
                    if fragment.lower() not in anchors(dest):
                        print(f"{where}  (no heading #{fragment})")
                        broken += 1
    if broken:
        print(f"check-links: {broken} broken of {checked} relative links", file=sys.stderr)
        return 1
    print(f"check-links: {checked} relative links resolve", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
