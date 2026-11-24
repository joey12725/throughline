"""Settings of the image pipeline, read from the environment only.

Nothing names a host path or a host: Immich moves to another machine by
changing IMMICH_URL / IMMICH_ML_URL / IMMICH_DB_URL. Secrets are never printed.

    IMMICH_URL, IMMICH_API_KEY          Immich server and key
    IMMICH_ML_URL                       Immich machine learning (/predict), default http://127.0.0.1:3003
    IMMICH_DB_URL                       Immich's own Postgres (read only: SigLIP vectors, job status);
                                        else built from IMMICH_DB_HOST/IMMICH_DB_PORT and the compose
                                        .env names DB_USERNAME, DB_PASSWORD, DB_DATABASE_NAME (port 54332
                                        is what docker-compose.override.yml publishes on loopback)
    IMMICH_CLIP_MODEL                   default ViT-SO400M-16-SigLIP2-384__webli
    BEEPER_MEDIA_IMPORT_DIR             directory written by images/beeper_fetch.py on the Beeper host
                                        (manifest.jsonl + <sha256>.<ext>), default ~/.throughline/exports/beeper-media
    IMAGES_EMAIL_IMPORT_DIR             directory written by images/email_fetch.py on the mail host
    IMAGES_T3_HOME                      T3 Code home (statev2.sqlite and attachments/), default ~/.t3
    IMAGES_HERMES_EXPORT                Hermes export (.jsonl or .jsonl.gz)
    HERMES_AUTOMATION_SOURCES           sources whose sessions map to hermes.automation (default cron)
    IMAGES_NUDENET_PYTHON               python of the venv that has nudenet installed
    IMAGES_STATE_DIR                    spool and caches, default $XDG_STATE_HOME/throughline/images
    IMAGES_OWNER_IDENTIFIER             author of photos.immich messages, default immich:owner:owner
    IMAGES_OWNER_NAME                   its display name (optional)
    IMAGES_ENV_FILES                    ':'-separated env files loaded for unset names (CLI only)
"""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_FILES = "~/.throughline/immich.env:~/.throughline/gemini.env:~/.throughline/immich/.env"


def load_env_files(paths=None, environ=None):
    """KEY=VALUE lines (optionally 'export ', quoted) for names not yet set.

    Returns the names loaded (never their values)."""
    environ = os.environ if environ is None else environ
    loaded = []
    for raw in (paths or environ.get("IMAGES_ENV_FILES") or DEFAULT_ENV_FILES).split(":"):
        path = Path(os.path.expanduser(raw.strip())) if raw.strip() else None
        if not path or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if key and key.replace("_", "").isalnum() and key not in environ:
                environ[key] = value
                loaded.append(key)
    return loaded


def _state_dir(environ):
    explicit = environ.get("IMAGES_STATE_DIR")
    if explicit:
        return Path(explicit)
    base = environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "throughline" / "images"


@dataclass
class Settings:
    immich_url: str | None
    immich_api_key: str | None
    immich_ml_url: str
    immich_db_url: str | None
    clip_model: str
    beeper_import_dir: Path | None
    email_import_dir: Path | None
    t3_home: Path
    hermes_export: Path | None
    hermes_automation_sources: tuple
    nudenet_python: str | None
    state_dir: Path
    owner_identifier: str
    owner_name: str | None

    @property
    def spool_dir(self):
        return self.state_dir / "spool"

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        db = env.get("IMMICH_DB_URL")
        if not db and env.get("DB_PASSWORD") is not None:
            from psycopg.conninfo import make_conninfo

            db = make_conninfo(
                host=env.get("IMMICH_DB_HOST", "127.0.0.1"),
                port=env.get("IMMICH_DB_PORT", "54332"),
                user=env.get("DB_USERNAME", "postgres"),
                password=env.get("DB_PASSWORD"),
                dbname=env.get("DB_DATABASE_NAME", "immich"),
            )
        hermes = env.get("IMAGES_HERMES_EXPORT") or str(
            Path.home() / ".throughline" / "exports" / "hermes" / "tl-hermes.jsonl.gz"
        )
        email_dir = env.get("IMAGES_EMAIL_IMPORT_DIR")
        nudenet = env.get("IMAGES_NUDENET_PYTHON") or str(
            Path.home() / ".throughline" / "vision" / ".venv" / "bin" / "python"
        )
        return cls(
            immich_url=(env.get("IMMICH_URL") or "").rstrip("/") or None,
            immich_api_key=env.get("IMMICH_API_KEY") or None,
            immich_ml_url=(env.get("IMMICH_ML_URL") or "http://127.0.0.1:3003").rstrip("/"),
            immich_db_url=db or None,
            clip_model=env.get("IMMICH_CLIP_MODEL") or "ViT-SO400M-16-SigLIP2-384__webli",
            beeper_import_dir=Path(
                env.get("BEEPER_MEDIA_IMPORT_DIR")
                or Path.home() / ".throughline" / "exports" / "beeper-media"
            ),
            email_import_dir=Path(email_dir) if email_dir else None,
            t3_home=Path(env.get("IMAGES_T3_HOME") or Path.home() / ".t3"),
            hermes_export=Path(hermes) if hermes else None,
            hermes_automation_sources=tuple(
                s.strip().lower()
                for s in (env.get("HERMES_AUTOMATION_SOURCES") or "cron").split(",")
                if s.strip()
            ),
            nudenet_python=nudenet if Path(nudenet).exists() else None,
            state_dir=_state_dir(env),
            owner_identifier=env.get("IMAGES_OWNER_IDENTIFIER") or "immich:owner:owner",
            owner_name=env.get("IMAGES_OWNER_NAME") or None,
        )
