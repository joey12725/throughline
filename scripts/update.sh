#!/bin/sh
# Update a Docker Compose install of Throughline in place.
#
#   scripts/update.sh              fetch, rebuild, migrate, restart (the usual)
#   scripts/update.sh --check      show what an update would bring; change nothing
#   scripts/update.sh --rollback   go back to the version before the last update
#
# What an update keeps: everything. The database, backups, settings and
# connector state live in Docker volumes; .env, throughline.env,
# connectors.env and docker/*.json are yours and git never touches them.
# Images are rebuilt while the old version keeps running, so the API is down
# only for the restart. Before a migration changes the database, the migrate
# service writes a pre-upgrade snapshot (docs/updating.md); --rollback
# restores it.
set -eu

cd "$(dirname "$0")/.."
STATE=.throughline-update
# The commit the running containers were built from (written after each
# successful deploy), so a checkout changed by hand still gets deployed.
DEPLOYED_FILE=.throughline-deployed
say() { printf '%s\n' "throughline update: $*" >&2; }
die() { say "$*"; exit 1; }

MODE=update
YES=0
for arg in "$@"; do
    case "$arg" in
        --check) MODE=check ;;
        --rollback) MODE=rollback ;;
        --yes|-y) YES=1 ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $arg (see --help)" ;;
    esac
done

command -v git >/dev/null 2>&1 || die "needs git"
docker compose version >/dev/null 2>&1 || die "needs Docker Compose v2 (docker compose)"
[ -f .env ] || die "no .env here: this does not look like an installed checkout"

# Keep the optional services that are running now (connectors, maintenance)
# running after the update, on top of any COMPOSE_PROFILES from .env.
profiles() {
    running="$(docker compose --profile connectors --profile maintenance ps --services --status running 2>/dev/null || true)"
    for p in connectors maintenance; do
        if printf '%s\n' "$running" | grep -qx "$p"; then printf -- '--profile %s ' "$p"; fi
    done
}

# Stop everything that writes, whichever profiles it was started with.
stop_writers() {
    docker compose --profile connectors --profile maintenance stop throughline connectors maintenance >/dev/null 2>&1 \
        || docker compose stop throughline >/dev/null 2>&1 || true
}

dirty() { git status --porcelain --untracked-files=no; }

wait_up() {
    # shellcheck disable=SC2086
    if ! docker compose $PROFILES up -d --remove-orphans --wait --wait-timeout 600; then
        say "the new version did not come up. Last migrate and API logs:"
        docker compose logs --no-log-prefix --tail 40 migrate throughline >&2 || true
        return 1
    fi
}

PROFILES="$(profiles)"

if [ "$MODE" = rollback ]; then
    [ -f "$STATE" ] || die "no previous update recorded in $STATE"
    # shellcheck disable=SC1090
    . "./$STATE"
    [ -n "${PREVIOUS:-}" ] || die "$STATE names no previous version"
    [ -z "$(dirty)" ] || die "tracked files have local edits; commit or stash them first"
    say "rolling back $(git rev-parse --short HEAD) -> $(git rev-parse --short "$PREVIOUS")"
    if [ -n "${SNAPSHOT:-}" ]; then
        say "the last update changed the database; it will be restored from"
        say "  $SNAPSHOT"
        say "Anything stored since that update will be lost."
        if [ "$YES" != 1 ]; then
            printf 'Type yes to continue: ' >&2
            read -r answer
            [ "$answer" = yes ] || die "nothing changed"
        fi
    fi
    if [ -n "${SNAPSHOT:-}" ]; then
        # Restored with the current image: the older one may predate the command.
        stop_writers
        docker compose run --rm --no-deps migrate throughline upgrade restore "$SNAPSHOT" --yes \
            || die "restore failed; nothing else changed (docs/updating.md)"
    fi
    git -c advice.detachedHead=false checkout --quiet "$PREVIOUS"
    git submodule update --recursive --quiet 2>/dev/null || true
    # shellcheck disable=SC2086
    docker compose $PROFILES build --pull || die "the older version did not build"
    wait_up || die "rollback did not come up; see the logs above"
    rm -f "$STATE"
    git rev-parse HEAD > "$DEPLOYED_FILE"
    say "rolled back to $(git rev-parse --short HEAD). Run scripts/update.sh to"
    say "return to the newest version when it is fixed."
    exit 0
fi

BRANCH="$(git symbolic-ref --quiet --short HEAD || true)"
if [ -z "$BRANCH" ]; then
    # After a rollback the checkout is detached at the older version; update
    # from there along the branch that contains it.
    BRANCH="$(git for-each-ref --format='%(refname:short)' --contains HEAD refs/heads | head -1)"
    [ -n "$BRANCH" ] || die "not on a branch; run git checkout <branch> first"
fi
UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name "$BRANCH@{u}" 2>/dev/null || true)"
[ -n "$UPSTREAM" ] || die "branch $BRANCH tracks no remote branch; git branch -u origin/$BRANCH"

say "checking for a new version ($UPSTREAM)"
git fetch --quiet --recurse-submodules=on-demand
CURRENT="$(git rev-parse HEAD)"
TARGET="$(git rev-parse "$UPSTREAM")"
DEPLOYED="$(cat "$DEPLOYED_FILE" 2>/dev/null || true)"
if git merge-base --is-ancestor "$TARGET" "$CURRENT"; then
    TARGET="$CURRENT"
    if [ "$DEPLOYED" = "$CURRENT" ]; then
        say "already up to date ($(git rev-parse --short HEAD))"
        exit 0
    fi
    say "the running containers were not built from this checkout ($(git rev-parse --short HEAD)): deploying it"
else
    git merge-base --is-ancestor "$CURRENT" "$TARGET" \
        || die "$BRANCH has local commits that $UPSTREAM does not; merge or rebase them yourself"
    say "new commits:"
    git --no-pager log --oneline --no-decorate "$CURRENT..$TARGET" | sed 's/^/  /' >&2
fi
# The version to return to on --rollback: the one running now, when known.
PREVIOUS="${DEPLOYED:-$CURRENT}"
git cat-file -e "$PREVIOUS^{commit}" 2>/dev/null || PREVIOUS="$CURRENT"
if [ "$MODE" = check ]; then
    new_migrations="$(git diff --name-only --diff-filter=A "$PREVIOUS" "$TARGET" -- src/throughline/migrations | sed 's#.*/#  #')"
    if [ -n "$new_migrations" ]; then
        say "database migrations (a snapshot is taken first):"
        printf '%s\n' "$new_migrations" >&2
    fi
    exit 0
fi

if [ -n "$(dirty)" ]; then
    say "these tracked files have local edits, which an update would overwrite:"
    dirty | sed 's/^/  /' >&2
    say "Move settings to throughline.env or docker-compose.override.yml (both"
    say "kept across updates; docs/updating.md), then run 'git checkout -- <file>'."
    exit 1
fi

if [ "$(git symbolic-ref --quiet --short HEAD || true)" != "$BRANCH" ]; then
    git checkout --quiet "$BRANCH"
fi
git merge --ff-only --quiet "$TARGET"
git submodule update --recursive --quiet 2>/dev/null || true
printf 'PREVIOUS=%s\nBRANCH=%s\nSNAPSHOT=\n' "$PREVIOUS" "$BRANCH" > "$STATE"

IMAGES="throughline"
for image in $IMAGES; do
    if docker image inspect "$image:latest" >/dev/null 2>&1; then
        docker image tag "$image:latest" "$image:previous"
    fi
done
revert_code() {
    git reset --quiet --keep "$CURRENT"
    for image in $IMAGES; do
        if docker image inspect "$image:previous" >/dev/null 2>&1; then
            docker image tag "$image:previous" "$image:latest"
        fi
    done
    rm -f "$STATE"
}

say "building $(git rev-parse --short HEAD) (the running version stays up meanwhile)"
# shellcheck disable=SC2086
if ! docker compose $PROFILES build --pull; then
    revert_code
    die "the build failed; nothing was changed and the old version is still running"
fi

# Writers pause while the schema changes, so the snapshot holds every record
# and a restore loses none. Connectors retry; desktops queue and catch up.
say "migrating (the API pauses for this step)"
stop_writers
LOG="$(mktemp)"
if ! docker compose run --rm migrate > "$LOG" 2>&1; then
    cat "$LOG" >&2
    SNAPSHOT_FILE="$(sed -n 's/^pre-upgrade snapshot: \(.*\) (.*$/\1/p' "$LOG" | tail -1)"
    rm -f "$LOG"
    if [ -n "$SNAPSHOT_FILE" ]; then
        # With the new image, which wrote the snapshot (an older one may
        # predate the restore command).
        say "migration failed; restoring the database from $SNAPSHOT_FILE"
        docker compose run --rm --no-deps migrate throughline upgrade restore "$SNAPSHOT_FILE" --yes \
            || die "restore failed; the snapshot is still at $SNAPSHOT_FILE (docs/updating.md)"
    fi
    revert_code
    wait_up || true
    die "migration failed; back on $(git rev-parse --short HEAD) with the database as it was"
fi
cat "$LOG" >&2
SNAPSHOT_FILE="$(sed -n 's/^pre-upgrade snapshot: \(.*\) (.*$/\1/p' "$LOG" | tail -1)"
rm -f "$LOG"
printf 'PREVIOUS=%s\nBRANCH=%s\nSNAPSHOT=%s\n' "$PREVIOUS" "$BRANCH" "$SNAPSHOT_FILE" > "$STATE"

if ! wait_up; then
    die "update applied but the API is unhealthy; fix and rerun, or scripts/update.sh --rollback"
fi
git rev-parse HEAD > "$DEPLOYED_FILE"
if [ "$PREVIOUS" = "$(git rev-parse HEAD)" ]; then
    say "deployed $(git rev-parse --short HEAD)"
else
    say "updated $(git rev-parse --short "$PREVIOUS") -> $(git rev-parse --short HEAD)"
fi
[ -z "$SNAPSHOT_FILE" ] || say "rollback point: $SNAPSHOT_FILE (scripts/update.sh --rollback)"
