"""Companion devices and the desktop bridge (docs/companion.md).

A companion device is a desktop running the Throughline Companion app. It runs
the plugins that must read the desktop (coding-agent logs, browser history,
ActivityWatch, local apps) and answers bridge requests from plugins that run
on the server and need something only the desktop has (browser cookies for a
site, an allow-listed file, a localhost service).

Enrollment happens on the box, like token creation: `enroll` creates the
device token (a plugin token scoped to companion_<device>), one plugin token
per desktop plugin, and returns a pairing code the owner pastes into the app.

Bridge requests are rows of tl.companion_requests. The caller inserts one
and waits; the device long-polls, claims it, runs it locally and posts the
answer; the caller collects the answer, which clears it from the table.
"""

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from .db import connect, create_token

NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
PLUGIN = re.compile(r"^[a-z][a-z0-9_]*$")
# A device is online when it polled within this many seconds.
ONLINE_S = 90
# Largest bridge answer accepted from a device (bytes of JSON).
MAX_RESPONSE = 1_000_000
MAX_ARGS = 16_000
PAIRING_PREFIX = "tlc1."


def device_plugin(name):
    """The plugin name a device token is scoped to."""
    return "companion_" + name


def encode_pairing(bundle):
    raw = json.dumps(bundle, separators=(",", ":"), sort_keys=True).encode()
    return PAIRING_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_pairing(code):
    code = code.strip()
    if not code.startswith(PAIRING_PREFIX):
        raise ValueError("not a Throughline companion pairing code")
    body = code[len(PAIRING_PREFIX) :]
    body += "=" * (-len(body) % 4)
    return json.loads(base64.urlsafe_b64decode(body))


def enroll(name, url, plugins=(), *, location_plugins=()):
    """Create a device with its token and one plugin token per desktop plugin.

    Uses the owner database connection. Returns {device, pairing, tokens} where
    pairing is the code the companion app accepts. Token values are returned
    only here, once.
    """
    if not NAME.match(name):
        raise ValueError("device name must match ^[a-z][a-z0-9_]{0,40}$")
    plugins = sorted(set(plugins))
    for plugin in plugins:
        if not PLUGIN.match(plugin):
            raise ValueError(f"invalid plugin name: {plugin}")
    unknown = set(location_plugins) - set(plugins)
    if unknown:
        raise ValueError("location plugins must be enrolled too: " + ", ".join(sorted(unknown)))
    with connect() as conn:
        if conn.execute(
            "SELECT 1 FROM tl.companion_devices WHERE name=%s AND revoked_at IS NULL",
            (name,),
        ).fetchone():
            raise ValueError(f"device {name} is already enrolled (revoke it first)")
        # A revoked device frees its name: its tokens stay revoked in
        # tl.tokens, and its bridge requests (already expired) go with it.
        conn.execute(
            "DELETE FROM tl.companion_devices WHERE name=%s AND revoked_at IS NOT NULL",
            (name,),
        )
    device_token = create_token(f"companion {name}", "plugin", device_plugin(name))
    tokens = {}
    for plugin in plugins:
        tokens[plugin] = create_token(
            f"companion {name}: {plugin}",
            "plugin",
            plugin,
            location_source=plugin in location_plugins,
        )
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO tl.companion_devices(name,token_id) VALUES (%s,%s) RETURNING id,name,created_at",
            (name, device_token["id"]),
        ).fetchone()
    bundle = dict(
        v=1,
        url=url,
        device=name,
        device_token=device_token["token"],
        plugin_tokens={p: t["token"] for p, t in tokens.items()},
    )
    return dict(
        device=dict(row, token_id=device_token["id"]),
        token_ids={p: t["id"] for p, t in tokens.items()},
        pairing=encode_pairing(bundle),
    )


def add_plugin_token(name, plugin, *, location_source=False):
    """A new plugin token for an enrolled device (owner connection)."""
    if not PLUGIN.match(plugin):
        raise ValueError(f"invalid plugin name: {plugin}")
    with connect() as conn:
        if not conn.execute(
            "SELECT 1 FROM tl.companion_devices WHERE name=%s AND revoked_at IS NULL",
            (name,),
        ).fetchone():
            raise ValueError(f"no enrolled device named {name}")
    token = create_token(
        f"companion {name}: {plugin}", "plugin", plugin, location_source=location_source
    )
    return dict(plugin=plugin, token_id=token["id"], token=token["token"])


def revoke(name):
    """Revoke a device: its device token and every token issued for it."""
    with connect() as conn:
        device = conn.execute(
            "UPDATE tl.companion_devices SET revoked_at=now() WHERE name=%s AND revoked_at IS NULL RETURNING id,token_id",
            (name,),
        ).fetchone()
        if not device:
            raise ValueError(f"no enrolled device named {name}")
        revoked = conn.execute(
            "UPDATE tl.tokens SET revoked_at=now() WHERE revoked_at IS NULL AND (id=%s OR left(name,%s)=%s) RETURNING id",
            (device["token_id"], len(f"companion {name}: "), f"companion {name}: "),
        ).fetchall()
        conn.execute(
            "UPDATE tl.companion_requests SET status='expired',response=NULL,args='{}' WHERE device_id=%s AND status IN ('pending','claimed','done')",
            (device["id"],),
        )
    return dict(device=name, tokens_revoked=len(revoked))


def _token_ok(conn, token, token_id=None):
    """The active token row for a token value (and id, when given), or None."""
    if not token:
        return None
    row = conn.execute(
        "SELECT id,location_source FROM tl.tokens WHERE token_hash=%s AND revoked_at IS NULL",
        (hashlib.sha256(token.encode()).hexdigest(),),
    ).fetchone()
    if row and token_id is not None and row["id"] != token_id:
        return None
    return row


def provision(name, url, plugins, path, *, location_plugins=()):
    """Keep a server-side device (the Docker `connectors` service) enrolled
    with one token per plugin, its pairing bundle in the file at path.

    Idempotent; run by the one-shot migrate service on every start, so the
    owner never copies a pairing code: tokens for newly enabled plugins are
    added, tokens of plugins no longer listed are revoked, and a lost file or
    a revoked device is enrolled afresh. The file is written 0600 and holds
    tokens: only the connectors service mounts it. With no plugins and no
    device nothing is enrolled. Returns {status, device, plugins}.
    """
    import os
    from pathlib import Path

    path = Path(path)
    plugins = sorted(set(plugins))
    # Checked before anything is revoked: a bad settings file changes nothing.
    if not NAME.match(name):
        raise ValueError("device name must match ^[a-z][a-z0-9_]{0,40}$")
    for plugin in plugins:
        if not PLUGIN.match(plugin):
            raise ValueError(f"invalid plugin name: {plugin}")
    bundle = None
    if path.is_file():
        try:
            bundle = decode_pairing(path.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError):
            bundle = None
    with connect() as conn:
        device = conn.execute(
            "SELECT id,token_id FROM tl.companion_devices WHERE name=%s AND revoked_at IS NULL",
            (name,),
        ).fetchone()
        valid = bool(
            bundle
            and device
            and bundle.get("device") == name
            and _token_ok(conn, bundle.get("device_token"), device["token_id"])
        )
    if not valid:
        if device:
            revoke(name)
        if not plugins:
            if path.is_file():
                path.unlink()
            return dict(status="none", device=None, plugins=[])
        result = enroll(name, url, plugins, location_plugins=location_plugins)
        bundle = decode_pairing(result["pairing"])
        status = "enrolled"
    else:
        status = "unchanged"
        tokens = dict(bundle.get("plugin_tokens") or {})
        with connect() as conn:
            for plugin in sorted(set(tokens) - set(plugins)):
                row = _token_ok(conn, tokens.pop(plugin))
                if row:
                    conn.execute(
                        "UPDATE tl.tokens SET revoked_at=now() WHERE id=%s", (row["id"],)
                    )
                status = "updated"
            missing = []
            for plugin in plugins:
                row = _token_ok(conn, tokens.get(plugin))
                if not row:
                    missing.append(plugin)
                elif row["location_source"] != (plugin in location_plugins):
                    # "location_source" changed in the settings file.
                    conn.execute(
                        "UPDATE tl.tokens SET location_source=%s WHERE id=%s",
                        (plugin in location_plugins, row["id"]),
                    )
                    status = "updated"
        for plugin in missing:
            tokens[plugin] = add_plugin_token(
                name, plugin, location_source=plugin in location_plugins
            )["token"]
            status = "updated"
        if bundle.get("url") != url:
            status = "updated"
        bundle = dict(bundle, url=url, plugin_tokens=tokens)
    if status != "unchanged" or not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encode_pairing(bundle) + "\n")
        os.replace(tmp, path)
    return dict(status=status, device=name, plugins=sorted(bundle["plugin_tokens"]))


def device_of(conn, token):
    """The active device a token belongs to, or None."""
    if token.get("kind") != "plugin" or not str(token.get("plugin") or "").startswith(
        "companion_"
    ):
        return None
    return conn.execute(
        "SELECT * FROM tl.companion_devices WHERE token_id=%s AND revoked_at IS NULL",
        (token["id"],),
    ).fetchone()


def _clean_capabilities(items):
    out = []
    for item in items or []:
        if not isinstance(item, dict) or not NAME.match(str(item.get("name") or "").replace(".", "_")):
            raise ValueError("each capability needs a name like cookies or file.read")
        allowed = item.get("allowed_plugins") or []
        if not isinstance(allowed, list) or not all(
            isinstance(p, str) and (p == "*" or PLUGIN.match(p)) for p in allowed
        ):
            raise ValueError("allowed_plugins must be plugin names")
        out.append(
            dict(
                name=item["name"],
                description=str(item.get("description") or "")[:300],
                allowed_plugins=sorted(set(allowed)),
            )
        )
    return out


def _clean_plugins(items):
    out = []
    for item in items or []:
        if not isinstance(item, dict) or not PLUGIN.match(str(item.get("name") or "")):
            raise ValueError("each plugin needs a name")
        out.append(
            {
                k: (str(item[k])[:300] if item.get(k) is not None else None)
                for k in ("name", "state", "last_run_at", "last_error")
            }
        )
    return out


def hello(conn, device, data):
    """Record what a device declares: platform, version, capabilities, plugins."""
    capabilities = _clean_capabilities(data.get("capabilities"))
    plugins = _clean_plugins(data.get("plugins"))
    row = conn.execute(
        "UPDATE tl.companion_devices SET platform=%s,app_version=%s,capabilities=%s,plugins=%s,last_seen_at=now() WHERE id=%s RETURNING name,platform,app_version,capabilities,plugins,last_seen_at",
        (
            str(data.get("platform") or "")[:100] or None,
            str(data.get("app_version") or "")[:50] or None,
            Jsonb(capabilities),
            Jsonb(plugins),
            device["id"],
        ),
    ).fetchone()
    return row


def expire(conn):
    """Expire unanswered requests and drop answers nobody collected."""
    conn.execute(
        "UPDATE tl.companion_requests SET status='expired',response=NULL,args='{}' WHERE status IN ('pending','claimed','done','failed') AND expires_at<now()"
    )
    conn.execute(
        "DELETE FROM tl.companion_requests WHERE finished_at<now()-interval '7 days' OR (status='expired' AND expires_at<now()-interval '7 days')"
    )


def claim(conn, device, limit=10):
    """Claim this device's pending requests (oldest first) and mark it seen."""
    conn.execute(
        "UPDATE tl.companion_devices SET last_seen_at=now() WHERE id=%s", (device["id"],)
    )
    return conn.execute(
        """UPDATE tl.companion_requests r SET status='claimed',claimed_at=now()
           WHERE r.id IN (SELECT id FROM tl.companion_requests
                          WHERE device_id=%s AND status='pending' AND expires_at>now()
                          ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED)
           RETURNING r.id,r.capability,r.args,r.plugin,r.expires_at""",
        (device["id"], limit),
    ).fetchall()


def answer(conn, device, request_id, data):
    """Store a device's answer to one of its claimed requests."""
    ok = bool(data.get("ok"))
    result = data.get("result")
    if ok and len(json.dumps(result, default=str)) > MAX_RESPONSE:
        ok, result, error = False, None, "answer too large"
    else:
        error = None if ok else str(data.get("error") or "failed")[:500]
    row = conn.execute(
        "UPDATE tl.companion_requests SET status=%s,response=%s,error=%s,finished_at=now() WHERE id=%s AND device_id=%s AND status='claimed' RETURNING id,status",
        (
            "done" if ok else "failed",
            Jsonb(result) if ok else None,
            error,
            request_id,
            device["id"],
        ),
    ).fetchone()
    return row


def _now():
    return datetime.now(timezone.utc)


def offers(device, capability, plugin):
    """Whether a device row offers capability to plugin (None: an owner call)."""
    for item in device["capabilities"] or []:
        if item.get("name") != capability:
            continue
        allowed = item.get("allowed_plugins") or []
        return plugin is None or "*" in allowed or plugin in allowed
    return False


def pick_device(conn, capability, plugin, name=None):
    """The online device that offers capability to plugin, preferring the most
    recently seen. Raises LookupError(reason) when there is none."""
    rows = conn.execute(
        "SELECT * FROM tl.companion_devices WHERE revoked_at IS NULL AND (%s::text IS NULL OR name=%s) ORDER BY last_seen_at DESC NULLS LAST",
        (name, name),
    ).fetchall()
    if not rows:
        raise LookupError("no_device")
    allowed = [r for r in rows if offers(r, capability, plugin)]
    if not allowed:
        raise LookupError("not_offered")
    cutoff = _now() - timedelta(seconds=ONLINE_S)
    online = [r for r in allowed if r["last_seen_at"] and r["last_seen_at"] >= cutoff]
    if not online:
        raise LookupError("offline")
    return online[0]


def submit(conn, device, capability, args, token, timeout_s):
    if len(json.dumps(args, default=str)) > MAX_ARGS:
        raise ValueError("arguments too large")
    return conn.execute(
        "INSERT INTO tl.companion_requests(device_id,capability,args,requested_by,plugin,expires_at) VALUES (%s,%s,%s,%s,%s,now()+make_interval(secs=>%s)) RETURNING id",
        (
            device["id"],
            capability,
            Jsonb(args),
            token["id"],
            token.get("plugin") if token.get("kind") == "plugin" else None,
            timeout_s + 5,
        ),
    ).fetchone()["id"]


def collect(conn, request_id):
    """The finished answer of a request, cleared from the table; None while it
    is still open."""
    row = conn.execute(
        "SELECT status,response,error FROM tl.companion_requests WHERE id=%s",
        (request_id,),
    ).fetchone()
    if not row or row["status"] in ("pending", "claimed"):
        return None
    conn.execute(
        "UPDATE tl.companion_requests SET status=CASE WHEN status='done' THEN 'collected' ELSE status END,response=NULL,args='{}' WHERE id=%s",
        (request_id,),
    )
    return row


def cancel(conn, request_id):
    conn.execute(
        "UPDATE tl.companion_requests SET status='expired',response=NULL,args='{}' WHERE id=%s AND status IN ('pending','claimed','done','failed')",
        (request_id,),
    )


def devices(conn):
    cutoff = _now() - timedelta(seconds=ONLINE_S)
    rows = conn.execute(
        "SELECT id,name,platform,app_version,capabilities,plugins,last_seen_at,created_at,revoked_at FROM tl.companion_devices ORDER BY name"
    ).fetchall()
    for row in rows:
        row["online"] = bool(
            row["revoked_at"] is None
            and row["last_seen_at"]
            and row["last_seen_at"] >= cutoff
        )
    return rows
