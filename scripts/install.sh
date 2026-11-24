#!/bin/sh
# Install Throughline with Docker Compose, or apply changed settings to an
# install. Safe to run again at any time: nothing that exists is replaced.
#
#   scripts/install.sh                   first install, or re-apply settings
#   scripts/install.sh --connectors      also run server connectors
#   scripts/install.sh --no-maintenance  no scheduled maintenance service
#   scripts/install.sh --maintenance     turn it back on
#   scripts/install.sh --timezone Europe/Berlin --port 8100 --bind 0.0.0.0
#
# A first run writes .env (random passwords, owner-only permissions), builds
# the image, starts PostgreSQL, the API and the maintenance jobs, creates the
# owner token and prints it. Later runs keep .env and the database, start
# whatever changed and re-enroll the connectors for docker/connectors.json.
# Updates are scripts/update.sh; settings are described in docs/updating.md.
set -eu

cd "$(dirname "$0")/.."
say() { printf '%s\n' "throughline install: $*" >&2; }
die() { say "$*"; exit 1; }

CONNECTORS=0
MAINTENANCE=""
TIMEZONE=""
PORT=""
BIND=""
while [ $# -gt 0 ]; do
    case "$1" in
        --connectors) CONNECTORS=1 ;;
        --maintenance) MAINTENANCE=1 ;;
        --no-maintenance) MAINTENANCE=0 ;;
        --timezone) shift; TIMEZONE="${1:?--timezone needs a zone}" ;;
        --port) shift; PORT="${1:?--port needs a number}" ;;
        --bind) shift; BIND="${1:?--bind needs an address}" ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
    shift
done

docker compose version >/dev/null 2>&1 || die "needs Docker with Compose v2 (docker compose)"
# env_file entries with "required: false" need Compose 2.24 or newer.
version="$(docker compose version --short 2>/dev/null | sed 's/^v//')"
major="${version%%.*}"
minor="$(printf '%s' "$version" | cut -d. -f2)"
case "$major.$minor" in
    *[!0-9.]*|.*) ;;  # unknown format: let compose itself complain
    *) if [ "$major" -lt 2 ] || { [ "$major" = 2 ] && [ "$minor" -lt 24 ]; }; then
           die "needs Docker Compose 2.24 or newer (found $version)"
       fi ;;
esac
[ -f docker-compose.yml ] || die "run this from a Throughline checkout"

# --- .env ---------------------------------------------------------------------

secret() { od -An -tx1 -N24 /dev/urandom | tr -d ' \n'; }
# The value of KEY in .env ("" when missing).
get() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -1; }
# Set KEY=VALUE in .env, replacing an existing line.
put() {
    if grep -q "^$1=" .env 2>/dev/null; then
        tmp="$(mktemp .env.XXXXXX)"
        K="$1" V="$2" awk 'index($0, ENVIRON["K"] "=") == 1 { print ENVIRON["K"] "=" ENVIRON["V"]; next } { print }' .env > "$tmp"
        chmod 600 "$tmp"
        mv "$tmp" .env
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}
host_zone() {
    zone="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    [ -n "$zone" ] || zone="$(cat /etc/timezone 2>/dev/null || true)"
    if [ -z "$zone" ] && [ -L /etc/localtime ]; then
        zone="$(readlink /etc/localtime | sed -n 's#.*/zoneinfo/##p')"
    fi
    printf '%s' "${zone:-UTC}"
}

NEW=0
if [ ! -f .env ]; then
    # New passwords would not open a database that exists already.
    project="$(POSTGRES_PASSWORD=x THROUGHLINE_APP_PASSWORD=x THROUGHLINE_READER_PASSWORD=x \
        docker compose config --format json 2>/dev/null | sed -n 's/^ *"name": *"\([^"]*\)".*/\1/p' | head -1)"
    if [ -n "$project" ] && [ -n "$(docker volume ls -q \
        --filter "label=com.docker.compose.project=$project" \
        --filter label=com.docker.compose.volume=postgres)" ]; then
        die "no .env, but the database volume of '$project' exists: restore that .env
  (its POSTGRES_PASSWORD opens the database) instead of making new passwords"
    fi
    NEW=1
    old_umask="$(umask)"
    umask 077
    {
        echo "# Written by scripts/install.sh. Keep it private: it holds the database"
        echo "# superuser password. Optional settings go in throughline.env."
    } > .env
    umask "$old_umask"
    say "writing .env with new random passwords"
fi
chmod 600 .env
# Passwords are only filled in where they are missing or blank: a database
# that exists already keeps the passwords it was created with.
for key in POSTGRES_PASSWORD THROUGHLINE_APP_PASSWORD THROUGHLINE_READER_PASSWORD; do
    [ -n "$(get "$key")" ] || put "$key" "$(secret)"
done
if [ -n "$TIMEZONE" ]; then
    put THROUGHLINE_TIMEZONE "$TIMEZONE"
elif [ -z "$(get THROUGHLINE_TIMEZONE)" ]; then
    put THROUGHLINE_TIMEZONE "$(host_zone)"
fi
[ -z "$PORT" ] || put THROUGHLINE_PORT "$PORT"
[ -z "$BIND" ] || put THROUGHLINE_BIND "$BIND"

profiles="$(get COMPOSE_PROFILES)"
# Maintenance is on by default: for a new .env, and for one written before
# install.sh existed (no COMPOSE_PROFILES line at all).
if [ -z "$MAINTENANCE" ] && ! grep -q '^COMPOSE_PROFILES=' .env; then
    MAINTENANCE=1
fi
has_profile() { printf ',%s,' "$profiles" | grep -q ",$1,"; }
add_profile() { has_profile "$1" || profiles="${profiles:+$profiles,}$1"; }
drop_profile() { profiles="$(printf '%s' "$profiles" | tr ',' '\n' | grep -vx "$1" | paste -sd, -)"; }
[ "$MAINTENANCE" != 1 ] || add_profile maintenance
[ "$MAINTENANCE" != 0 ] || drop_profile maintenance
[ "$CONNECTORS" = 0 ] || add_profile connectors
put COMPOSE_PROFILES "$profiles"

if has_profile connectors && [ ! -f docker/connectors.json ]; then
    say "the connectors service runs the connectors enabled in docker/connectors.json,"
    say "which does not exist yet: start from docker/connectors.example.json, put"
    say "their secrets in connectors.env, and run scripts/install.sh again."
fi

# --- start ----------------------------------------------------------------------

PORT="$(get THROUGHLINE_PORT)"
PORT="${PORT:-8000}"
say "building and starting (the first build takes a few minutes)"
if ! docker compose up -d --build --remove-orphans --wait --wait-timeout 900; then
    say "Throughline did not come up. Last migrate and API logs:"
    docker compose logs --no-log-prefix --tail 40 migrate throughline >&2 || true
    exit 1
fi
# migrate re-enrolled the connectors for the current docker/connectors.json;
# a running connectors service reads its tokens at start.
if has_profile connectors; then
    docker compose restart connectors >/dev/null
fi

put THROUGHLINE_URL "http://127.0.0.1:$PORT"

# --- owner token -------------------------------------------------------------------

admin() { docker compose run --rm --no-deps -T migrate "$@"; }
TOKEN="$(get THROUGHLINE_TOKEN)"
if [ -z "$TOKEN" ]; then
    owners="$(admin sh -c 'throughline token list --json | python -c "import json,sys; print(sum(1 for t in json.load(sys.stdin) if t[\"kind\"] == \"owner\" and not t[\"revoked_at\"]))"')"
    if [ "$owners" = 0 ]; then
        TOKEN="$(admin sh -c 'throughline token create owner --json | python -c "import json,sys; print(json.load(sys.stdin)[\"token\"])"')"
        [ -n "$TOKEN" ] || die "could not create the owner token"
        put THROUGHLINE_TOKEN "$TOKEN"
        CREATED=1
    else
        say "an owner token exists already and .env has no THROUGHLINE_TOKEN:"
        say "  set it yourself, or create another with"
        say "  docker compose run --rm migrate throughline token create owner --json"
    fi
fi

# Backups (run by maintenance) are encrypted with a passphrase kept in the
# throughline-home volume; without one every scheduled backup fails.
KEY="$(admin python -c 'from throughline import backups; r = backups.init_key(); print(("created " if r["created"] else "") + r["file"])')"
case "$KEY" in
    "created "*)
        KEY="${KEY#created }"
        say "created the backup passphrase $KEY (in the throughline-home volume)."
        say "Keep a copy off this machine; without it no backup can be restored:"
        say "  docker compose run --rm --no-deps -T migrate cat $KEY"
        ;;
esac

say "running: $(docker compose ps --services --status running | paste -sd' ' -)"
BOUND="$(get THROUGHLINE_BIND)"
say "API: http://127.0.0.1:$PORT (published on ${BOUND:-127.0.0.1}:$PORT)"
if [ "${CREATED:-0}" = 1 ]; then
    say "owner token (also saved as THROUGHLINE_TOKEN in .env; it is shown only here"
    say "and stored in the database as a hash):"
    printf '%s\n' "$TOKEN"
fi
say "try: curl -s -H \"Authorization: Bearer \$THROUGHLINE_TOKEN\" http://127.0.0.1:$PORT/v1/status"
