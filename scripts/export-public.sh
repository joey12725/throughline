#!/bin/sh
# Build the public Throughline tree from this (private) repository.
#
#   scripts/export-public.sh [options] TARGET_DIR
#
#   --repo OWNER/NAME     the public GitHub repository; replaces the GITHUB_REPO
#                         placeholder (between @ signs) in the exported text
#                         files and goes into the publish command
#   --ref REF             commit to export (default HEAD). Only committed
#                         content is exported: the working tree, untracked
#                         files, stashes and refs/* never are.
#   --allow-submodules    export even though REF still has .gitmodules (the
#                         submodule plugins are then MISSING from the export;
#                         for dry runs only)
#   --no-relock           do not refresh uv.lock when denylisted workspace
#                         members were left out
#
# TARGET_DIR must not exist or be empty. It becomes a fresh git repository
# with ONE commit by a neutral author, on branch main, with no remote. The
# export is checked (allowlist, denylist, leak check, the private history's
# author names and e-mails, gitleaks/trufflehog when installed, links) and
# the script prints the single command that would publish it. It never
# pushes, never creates a repository and never contacts GitHub.
set -eu

AUTHOR_NAME="Throughline contributors"
AUTHOR_EMAIL="noreply@throughline.invalid"

# Top-level entries the public tree may contain. Anything else at the top
# level of REF stops the export: decide, then add it here or to the denylist.
TOP_ALLOW=".dockerignore .env.example .github .gitignore .python-version
Dockerfile LICENSE NOTICE README.md CONTRIBUTING.md SECURITY.md
CODE_OF_CONDUCT.md CHANGELOG.md docker-compose.yml pyproject.toml uv.lock
assets bench clients companion docker docs fixtures plugins scripts sdk site skill src
tests tools web"

say() { printf 'export-public: %s\n' "$*" >&2; }
die() { say "$*"; exit 1; }

REPO="" REF="HEAD" ALLOW_SUBMODULES=0 RELOCK=1 TARGET=""
while [ $# -gt 0 ]; do
    case "$1" in
        --repo) shift; REPO="${1:?--repo needs OWNER/NAME}" ;;
        --ref) shift; REF="${1:?--ref needs a commit}" ;;
        --allow-submodules) ALLOW_SUBMODULES=1 ;;
        --no-relock) RELOCK=0 ;;
        -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown option $1 (see --help)" ;;
        *) [ -z "$TARGET" ] || die "one TARGET_DIR only"; TARGET="$1" ;;
    esac
    shift
done
[ -n "$TARGET" ] || die "usage: scripts/export-public.sh [--repo OWNER/NAME] TARGET_DIR"
case "$REPO" in
    ""|*/*) ;;
    *) die "--repo must be OWNER/NAME" ;;
esac

SRC="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
COMMIT="$(git -C "$SRC" rev-parse --verify "$REF^{commit}")" || die "no commit $REF"
DENYLIST="$SRC/scripts/export-denylist.txt"
[ -f "$DENYLIST" ] || die "missing $DENYLIST"

if [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
    die "$TARGET exists and is not empty"
fi
mkdir -p "$TARGET"
TARGET="$(cd "$TARGET" && pwd)"
case "$TARGET/" in
    "$SRC"/*) die "TARGET_DIR must be outside the repository" ;;
esac

say "exporting $(git -C "$SRC" log -1 --format='%h %s' "$COMMIT")"

# --- submodules -------------------------------------------------------------------

if git -C "$SRC" cat-file -e "$COMMIT:.gitmodules" 2>/dev/null; then
    subs="$(git -C "$SRC" ls-tree -r "$COMMIT" | awk '$2 == "commit" { print $4 }' | grep -v '^private/' || true)"
    if [ -n "$subs" ]; then
        if [ "$ALLOW_SUBMODULES" = 1 ]; then
            say "WARNING: $REF still has non-private submodules; these are NOT exported:"
            printf '  %s\n' $subs >&2
        else
            say "$REF still has non-private submodules; convert them to in-tree directories first:"
            printf '  %s\n' $subs >&2
            die "(or pass --allow-submodules for a dry run)"
        fi
    fi
fi

# --- copy the committed tree, minus the denylist -------------------------------------

# Top-level allowlist.
for entry in $(git -C "$SRC" ls-tree --name-only "$COMMIT"); do
    case " $(echo $TOP_ALLOW) " in
        *" $entry "*) ;;
        *)
            denied=0
            while IFS= read -r pattern; do
                case "$pattern" in ""|"#"*) continue ;; esac
                pattern="${pattern%/}"
                # shellcheck disable=SC2254
                case "$entry" in $pattern) denied=1 ;; esac
            done < "$DENYLIST"
            [ "$denied" = 1 ] || die "top-level '$entry' is neither allowlisted (TOP_ALLOW in $0) nor denylisted"
            ;;
    esac
done

LIST="$(mktemp)"  # NUL-separated paths to leave out
trap 'rm -f "$LIST"' EXIT
# Regular files and symlinks only (submodule gitlinks are 'commit' entries).
git -C "$SRC" ls-tree -r -z --full-tree "$COMMIT" \
    | python3 -I -c '
import fnmatch, sys

deny = [l.strip() for l in open(sys.argv[1], encoding="utf-8")
        if l.strip() and not l.strip().startswith("#")]

def denied(path):
    parts = path.split("/")
    dirs = ["/".join(parts[:i]) for i in range(1, len(parts))]
    for pattern in deny:
        if pattern.endswith("/"):
            stem = pattern[:-1]
            if stem.startswith("**/"):
                # a directory of that name at any depth
                if any(fnmatch.fnmatchcase(d, stem[3:]) for d in parts[:-1]):
                    return True
            elif any(fnmatch.fnmatchcase(d, stem) for d in dirs):
                return True
        elif "/" in pattern:
            if fnmatch.fnmatchcase(path, pattern):
                return True
        elif fnmatch.fnmatchcase(parts[-1], pattern):
            return True  # a file name at any depth
    return False

kept = dropped = 0
with open(sys.argv[2], "w", encoding="utf-8") as out:
    for record in sys.stdin.buffer.read().split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        if meta.split()[1] != b"blob":
            continue
        path = path.decode("utf-8")
        if denied(path):
            dropped += 1
            out.write(path + "\0")
        else:
            kept += 1
print(f"export-public: {kept} files kept, {dropped} denylisted", file=sys.stderr)
' "$DENYLIST" "$LIST"

# The committed tree (export-ignore attributes apply), then the denylisted
# files removed again and empty directories (submodule mount points) pruned.
git -C "$SRC" archive --format=tar "$COMMIT" | tar -x -C "$TARGET"
(cd "$TARGET" && xargs -0 rm -f -- < "$LIST")
find "$TARGET" -depth -type d -empty -delete

# --- fill in the repository name ---------------------------------------------------

# Spelled in two parts so this script is not rewritten by itself.
PLACEHOLDER="@GITHUB""_REPO@"
if [ -n "$REPO" ]; then
    grep -rlI --exclude-dir=.git -F "$PLACEHOLDER" "$TARGET" | while IFS= read -r file; do
        R="$REPO" P="$PLACEHOLDER" python3 -I -c '
import os, sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
open(p, "w", encoding="utf-8").write(t.replace(os.environ["P"], os.environ["R"]))
' "$file"
    done
    say "filled in $PLACEHOLDER = $REPO"
else
    n="$(grep -rlI --exclude-dir=.git -F "$PLACEHOLDER" "$TARGET" | wc -l)"
    say "WARNING: no --repo given; $PLACEHOLDER placeholders remain in $n file(s)"
fi

# --- uv.lock without the left-out workspace members ---------------------------------

if [ "$RELOCK" = 1 ] && [ -f "$TARGET/uv.lock" ] && command -v uv >/dev/null 2>&1; then
    if ! (cd "$TARGET" && uv lock --check >/dev/null 2>&1); then
        say "uv.lock does not match the exported workspace; refreshing it (uv lock, no upgrades)"
        (cd "$TARGET" && uv lock -q) || die "uv lock failed in the export"
        (cd "$TARGET" && uv lock --check -q) || die "uv.lock still out of date"
    fi
fi

# --- one commit, neutral author ----------------------------------------------------

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$TARGET/pyproject.toml" | head -1)"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME="$AUTHOR_NAME" GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL"
export GIT_COMMITTER_NAME="$AUTHOR_NAME" GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL"
git -C "$TARGET" init -q -b main
git -C "$TARGET" add -A
git -C "$TARGET" -c commit.gpgsign=false commit -q --no-verify \
    -m "Throughline ${VERSION:-0.1.0}" \
    -m "Initial public release."
EXPORTED="$(git -C "$TARGET" rev-parse HEAD)"

# --- checks -------------------------------------------------------------------------

FAIL=0
refs="$(git -C "$TARGET" for-each-ref --format='%(refname)')"
[ "$refs" = "refs/heads/main" ] || { say "FAIL: unexpected refs: $refs"; FAIL=1; }
[ "$(git -C "$TARGET" rev-list --count HEAD)" = 1 ] || { say "FAIL: more than one commit"; FAIL=1; }
[ -z "$(git -C "$TARGET" remote)" ] || { say "FAIL: the export has a remote"; FAIL=1; }
[ ! -e "$TARGET/.gitmodules" ] || { say "FAIL: .gitmodules exported"; FAIL=1; }

say "leak check (private patterns + public guard)"
if [ -x "$SRC/scripts/leak-check.sh" ]; then
    "$SRC/scripts/leak-check.sh" "$TARGET" || FAIL=1
fi
if [ -x "$TARGET/scripts/leak-check.sh" ]; then
    "$TARGET/scripts/leak-check.sh" "$TARGET" || FAIL=1
else
    say "FAIL: the export has no scripts/leak-check.sh"; FAIL=1
fi

# Every e-mail address that ever authored or committed in the private
# history must be absent from the export. (Names are the leak check's job:
# some authors are tool names that the docs mention legitimately.)
say "private-history e-mail addresses"
IDS="$(mktemp)"
git -C "$SRC" log --all --format='%ae%n%ce' 2>/dev/null \
    | sort -u | grep -v -x -F -e "$AUTHOR_EMAIL" -e "noreply@github.com" -e "" > "$IDS" || true
if [ -s "$IDS" ]; then
    if git -C "$TARGET" grep -n -I -i -F -f "$IDS" >/dev/null 2>&1; then
        say "FAIL: the export contains an e-mail address from the private history:"
        git -C "$TARGET" grep -n -I -i -F -f "$IDS" | head -20 >&2
        FAIL=1
    fi
fi
rm -f "$IDS"

if command -v gitleaks >/dev/null 2>&1; then
    say "gitleaks"
    gitleaks detect --no-banner --redact --source "$TARGET" || FAIL=1
else
    say "gitleaks not installed: skipped (https://github.com/gitleaks/gitleaks)"
fi
if command -v trufflehog >/dev/null 2>&1; then
    say "trufflehog"
    trufflehog git "file://$TARGET" --only-verified --fail --no-update || FAIL=1
else
    say "trufflehog not installed: skipped (https://github.com/trufflesecurity/trufflehog)"
fi

if [ -f "$TARGET/scripts/check-links.py" ]; then
    say "links"
    python3 -I "$TARGET/scripts/check-links.py" "$TARGET" || FAIL=1
fi

FILES="$(git -C "$TARGET" ls-files | wc -l | tr -d ' ')"
SIZE="$(du -sh --exclude=.git "$TARGET" 2>/dev/null | cut -f1)"
say "exported $FILES files ($SIZE) as commit $EXPORTED"
say "author: $AUTHOR_NAME <$AUTHOR_EMAIL>"
if [ "$FAIL" != 0 ]; then
    die "checks FAILED: fix the private repository, commit, and export again"
fi

say "all checks passed. Inspect $TARGET, then publish with this one command:"
printf '\n  gh repo create %s --public --source %s --push\n\n' "${REPO:-OWNER/NAME}" "$TARGET"
say "(not run: publishing is irreversible; see docs/releasing.md)"
