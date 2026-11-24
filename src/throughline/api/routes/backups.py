"""Backup settings and status over HTTP (throughline.backups), for the
settings screen: GET/PUT /v1/backup/settings and GET /v1/backup/status,
owner tokens only. PUT deep-merges the body into the settings file the
scheduler reads (a bad value is 400); the passphrase is never returned, only
whether its file exists. Running a bundle stays a CLI/timer job: it takes
minutes to hours."""

from fastapi import APIRouter, HTTPException

from ... import backups
from ..deps import Token, audit, authenticated_connection, require, require_unrestricted

router = APIRouter()


def _load():
    try:
        return backups.load()
    except ValueError as exc:
        raise HTTPException(500, str(exc)) from None


@router.get("/v1/backup/settings")
def backup_settings_get(token: Token):
    require_unrestricted(token)
    require(token, "owner")
    return backups.public(_load())


@router.put("/v1/backup/settings")
def backup_settings_put(data: dict, token: Token):
    require_unrestricted(token)
    require(token, "owner")
    data = {k: v for k, v in (data or {}).items() if k not in ("config_file",)}
    if isinstance(data.get("encryption"), dict):
        data["encryption"].pop("passphrase_present", None)
    try:
        settings = backups.save(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    audit(token, 1)
    return backups.public(settings)


@router.get("/v1/backup/status")
def backup_status(token: Token):
    require_unrestricted(token)
    require(token, "owner")
    settings = _load()
    with authenticated_connection(token) as conn:
        result = backups.status(conn, settings)
    audit(token, 1)
    return result
