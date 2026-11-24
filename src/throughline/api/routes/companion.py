"""Companion devices and the desktop bridge over HTTP (docs/companion.md).

Device side (the device token, a plugin token scoped to companion_<device>):

    POST /v1/companion/hello            declare platform, capabilities, plugins
    GET  /v1/companion/poll?wait=25     long-poll: claim pending requests
    POST /v1/companion/requests/{id}    answer one claimed request

Caller side (a plugin token, or the owner):

    POST /v1/companion/call             {capability, args, device?, timeout_s?}
                                        waits for the device's answer

Owner: GET /v1/companion/devices lists devices, online state and what they
offer. Plugin tokens are owner scope, so every route also passes
require_unrestricted. Bodies are never audited: only the capability, the
device and the outcome.
"""

import asyncio
import time

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ... import companion
from ..deps import (
    Token,
    audit,
    authenticated_connection,
    own_working_state,
    require,
    require_unrestricted,
)

router = APIRouter()

MAX_WAIT_S = 30
MAX_CALL_S = 120


def _device(token):
    with authenticated_connection(token) as conn:
        device = companion.device_of(conn, token)
    if not device:
        raise HTTPException(403, "not a companion device token")
    return device


@router.post("/v1/companion/hello")
@own_working_state
def companion_hello(token: Token, data: dict):
    require_unrestricted(token)
    device = _device(token)
    with authenticated_connection(token) as conn:
        try:
            row = companion.hello(conn, device, data or {})
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        pending = conn.execute(
            "SELECT count(*) AS n FROM tl.companion_requests WHERE device_id=%s AND status='pending'",
            (device["id"],),
        ).fetchone()["n"]
    audit(token, device=device["name"], capabilities=len(row["capabilities"]))
    return dict(device=row, pending=pending)


def _claim(token, device):
    with authenticated_connection(token) as conn:
        companion.expire(conn)
        return companion.claim(conn, device)


@router.get("/v1/companion/poll")
@own_working_state
async def companion_poll(request: Request, token: Token, wait: int = 25):
    require_unrestricted(token)
    if not 0 <= wait <= MAX_WAIT_S:
        raise HTTPException(422, f"wait must be 0..{MAX_WAIT_S}")
    device = await run_in_threadpool(_device, token)
    deadline = time.monotonic() + wait
    while True:
        rows = await run_in_threadpool(_claim, token, device)
        if rows or time.monotonic() >= deadline or await request.is_disconnected():
            break
        await asyncio.sleep(0.5)
    audit(token, len(rows), device=device["name"])
    return dict(requests=rows)


@router.post("/v1/companion/requests/{request_id}")
@own_working_state
def companion_answer(request_id: int, token: Token, data: dict):
    require_unrestricted(token)
    device = _device(token)
    with authenticated_connection(token) as conn:
        row = companion.answer(conn, device, request_id, data or {})
    if not row:
        raise HTTPException(404, "no claimed request with that id for this device")
    audit(token, device=device["name"], request=request_id, status=row["status"])
    return dict(row)


REASONS = {
    "no_device": (404, "no companion device is enrolled"),
    "not_offered": (
        403,
        "no companion device offers this capability to this plugin (allow it in the companion app)",
    ),
    "offline": (503, "the companion device that offers this capability is offline"),
}


def _submit(token, capability, args, name, timeout_s):
    plugin = token.get("plugin") if token["kind"] == "plugin" else None
    with authenticated_connection(token) as conn:
        try:
            device = companion.pick_device(conn, capability, plugin, name)
        except LookupError as error:
            status, detail = REASONS[str(error)]
            raise HTTPException(
                status, dict(reason=str(error), message=detail)
            ) from None
        try:
            request_id = companion.submit(conn, device, capability, args, token, timeout_s)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
    return device, request_id


def _collect(token, request_id):
    with authenticated_connection(token) as conn:
        return companion.collect(conn, request_id)


def _cancel(token, request_id):
    with authenticated_connection(token) as conn:
        companion.cancel(conn, request_id)


@router.post("/v1/companion/call")
@own_working_state
async def companion_call(request: Request, token: Token, data: dict):
    require_unrestricted(token)
    require(token, "owner", "plugin")
    if str(token.get("plugin") or "").startswith("companion_"):
        raise HTTPException(403, "a device token cannot call the bridge")
    capability = (data or {}).get("capability")
    if not isinstance(capability, str) or not capability:
        raise HTTPException(400, "capability is required")
    args = data.get("args") or {}
    if not isinstance(args, dict):
        raise HTTPException(400, "args must be an object")
    timeout_s = data.get("timeout_s", 30)
    if not isinstance(timeout_s, int) or not 1 <= timeout_s <= MAX_CALL_S:
        raise HTTPException(422, f"timeout_s must be 1..{MAX_CALL_S}")
    device, request_id = await run_in_threadpool(
        _submit, token, capability, args, data.get("device"), timeout_s
    )
    deadline = time.monotonic() + timeout_s
    row = None
    try:
        while time.monotonic() < deadline:
            row = await run_in_threadpool(_collect, token, request_id)
            if row or await request.is_disconnected():
                break
            await asyncio.sleep(0.25)
    finally:
        if not row:
            await run_in_threadpool(_cancel, token, request_id)
    outcome = row["status"] if row else "timeout"
    audit(token, capability=capability, device=device["name"], outcome=outcome)
    if not row:
        raise HTTPException(
            504, dict(reason="timeout", message="the companion device did not answer in time")
        )
    if row["status"] != "done":
        raise HTTPException(
            502, dict(reason=row["status"], message=row["error"] or "the device refused")
        )
    return dict(device=device["name"], result=row["response"])


@router.get("/v1/companion/devices")
def companion_devices(token: Token):
    require_unrestricted(token)
    require(token, "owner")
    with authenticated_connection(token) as conn:
        rows = companion.devices(conn)
    audit(token, len(rows))
    return dict(devices=rows)
