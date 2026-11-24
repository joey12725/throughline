"""Arrival rules: condition evaluation, sliding-window count triggers, agent notifications, and table triggers (RUL-1..3)."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any, Callable

from psycopg.types.json import Jsonb

from ..db import connect
from ..providers import http
from .runner import run_lens
from .schema import LensSpecError, validate_lens_name

RULE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
RULE_WORKERS = ("code", "jev", "llm")
# "run_table" is the stored action_kind (tl.rules CHECK); "run_lens" is accepted as input.
ACTION_KINDS = ("notify", "run_table")
ACTION_KIND_ALIASES = {"run_lens": "run_table"}

_notifier_override: Callable[[dict[str, Any]], None] | None = None

# Lens runs a rule triggered that ended without the message's rows: they are
# kept in tl.rule_deliveries (payload kind 'run_table') and deliver_pending()
# runs them again, so an outage of the model endpoint delays them, never drops
# them. The rule cursor still moves on: the queue row is the record.
RETRY_LENS_STATUSES = ("provider_unavailable", "budget_exhausted", "approval_required", "failed")


def _lens_dedup_key(rule_id: int, message_id: int, lens: str) -> str:
    return hashlib.sha256(f"run_table:{rule_id}:{message_id}:{lens}".encode("utf-8")).hexdigest()[:32]


def _run_rule_lens(lens: str, message_id: int) -> dict[str, Any]:
    """run_lens for one message; an unexpected error becomes status 'failed'.
    A missing lens or invalid definition still raises: retrying cannot fix it."""
    try:
        return run_lens(lens, trigger_kind="rule", message_ids=[message_id])
    except (LookupError, LensSpecError):
        raise
    except Exception as exc:
        return {"lens": lens, "status": "failed", "errors": [str(exc)[:200]]}


def _queue_lens_retry(rule: dict[str, Any], message_id: int, lens: str, res: dict[str, Any]) -> None:
    error = "; ".join(str(e) for e in (res.get("errors") or [])[:3]) or str(res.get("status"))
    payload = {"kind": "run_table", "rule": rule["name"], "lens": lens, "message_id": message_id}
    with connect("app") as conn:
        conn.execute(
            """INSERT INTO tl.rule_deliveries(rule_id, message_id, dedup_key, status, attempts, last_error, payload)
            VALUES (%s,%s,%s,'failed',1,%s,%s)
            ON CONFLICT (dedup_key) DO UPDATE
            SET status='failed', attempts=tl.rule_deliveries.attempts+1, last_error=EXCLUDED.last_error""",
            (rule["id"], message_id, _lens_dedup_key(rule["id"], message_id, lens), error[:300], Jsonb(payload)),
        )


def set_notifier_override(fn: Callable[[dict[str, Any]], None] | None) -> None:
    """Test hook to intercept outbound rule notifications without real sockets."""
    global _notifier_override
    _notifier_override = fn


def validate_rule_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise LensSpecError("rule definition must be an object")
    name = spec.get("name")
    if not isinstance(name, str) or not RULE_NAME_RE.fullmatch(name.strip()):
        raise LensSpecError("rule name must match ^[a-z][a-z0-9_-]{0,62}$")
    name = name.strip()

    channels = spec.get("channels") or []
    if isinstance(channels, str):
        channels = [channels]
    if not isinstance(channels, list) or not all(isinstance(c, str) and c.strip() for c in channels):
        raise LensSpecError("rule channels must be a list of channel names")
    channels = [c.strip() for c in channels]

    flt = spec.get("filter") or {}
    if not isinstance(flt, dict):
        raise LensSpecError("rule filter must be an object")

    worker = str(spec.get("worker") or "code").strip().lower()
    if worker not in RULE_WORKERS:
        raise LensSpecError(f"rule worker must be one of {', '.join(RULE_WORKERS)}")
    worker_config = spec.get("worker_config") or {}
    if not isinstance(worker_config, dict):
        raise LensSpecError("rule worker_config must be an object")

    window_seconds = spec.get("window_seconds")
    if window_seconds is not None:
        window_seconds = int(window_seconds)
        if window_seconds <= 0:
            raise LensSpecError("window_seconds must be > 0")

    min_count = int(spec.get("min_count", 1))
    if min_count < 1:
        raise LensSpecError("min_count must be >= 1")

    action_kind = str(spec.get("action_kind") or "").strip().lower()
    action_kind = ACTION_KIND_ALIASES.get(action_kind, action_kind)
    if action_kind not in ACTION_KINDS:
        raise LensSpecError("action_kind must be one of notify, run_lens (alias run_table)")
    action_config = spec.get("action_config") or {}
    if not isinstance(action_config, dict):
        raise LensSpecError("action_config must be an object")
    if action_kind == "run_table":
        lens = validate_lens_name(action_config.get("lens") or action_config.get("table"))
        action_config = {**action_config, "lens": lens, "table": lens}

    return {
        "name": name,
        "description": str(spec.get("description") or "").strip(),
        "channels": channels,
        "filter": flt,
        "worker": worker,
        "worker_config": worker_config,
        "window_seconds": window_seconds,
        "min_count": min_count,
        "action_kind": action_kind,
        "action_config": action_config,
        "enabled": bool(spec.get("enabled", True)),
    }


def define_rule(spec: dict[str, Any], *, conn=None) -> dict[str, Any]:
    clean = validate_rule_spec(spec)

    def _apply(c):
        row = c.execute(
            """INSERT INTO tl.rules(
                name, description, channels, filter, worker, worker_config,
                window_seconds, min_count, action_kind, action_config, enabled
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (name) DO UPDATE SET
                description=EXCLUDED.description,
                channels=EXCLUDED.channels,
                filter=EXCLUDED.filter,
                worker=EXCLUDED.worker,
                worker_config=EXCLUDED.worker_config,
                window_seconds=EXCLUDED.window_seconds,
                min_count=EXCLUDED.min_count,
                action_kind=EXCLUDED.action_kind,
                action_config=EXCLUDED.action_config,
                enabled=EXCLUDED.enabled,
                updated_at=clock_timestamp()
            RETURNING *""",
            (
                clean["name"],
                clean["description"],
                clean["channels"],
                Jsonb(clean["filter"]),
                clean["worker"],
                Jsonb(clean["worker_config"]),
                clean["window_seconds"],
                clean["min_count"],
                clean["action_kind"],
                Jsonb(clean["action_config"]),
                clean["enabled"],
            ),
        ).fetchone()
        return dict(row)

    if conn is not None:
        return _apply(conn)
    with connect("app") as c:
        return _apply(c)


def list_rules(*, conn=None) -> list[dict[str, Any]]:
    def _read(c):
        return [dict(r) for r in c.execute("SELECT * FROM tl.rules ORDER BY name").fetchall()]

    if conn is not None:
        return _read(conn)
    with connect("app") as c:
        return _read(c)


def _matches_filter(msg: dict[str, Any], flt: dict[str, Any]) -> bool:
    content = msg.get("content") or ""
    low = content.lower()
    if flt.get("author"):
        want = str(flt["author"]).lower()
        got = (msg.get("person_handle") or msg.get("author_handle") or "").lower()
        if got != want:
            return False
    if flt.get("keywords"):
        kws = [str(k).lower() for k in flt["keywords"] if str(k).strip()]
        if kws and not any(k in low for k in kws):
            return False
    if flt.get("exclude_keywords"):
        ex = [str(k).lower() for k in flt["exclude_keywords"] if str(k).strip()]
        if any(k in low for k in ex):
            return False
    if flt.get("regex"):
        if not re.search(str(flt["regex"]), content, flags=re.I):
            return False
    if isinstance(flt.get("fields"), dict):
        mfields = msg.get("fields") or {}
        for k, expected in flt["fields"].items():
            if mfields.get(k) != expected:
                return False
    return True


def _deliver_one(delivery: dict[str, Any], action_config: dict[str, Any]) -> None:
    payload = {
        "rule": delivery["payload"].get("rule"),
        "message_id": int(delivery["message_id"]),
        "dedup_key": delivery["dedup_key"],
        "channel": delivery["payload"].get("channel"),
        "happened_at": delivery["payload"].get("happened_at"),
    }
    if _notifier_override is not None:
        _notifier_override(payload)
        return
    webhook = action_config.get("webhook_url")
    if webhook:
        http.request("POST", str(webhook), payload=payload, timeout=15, what="rule_notify")


def deliver_pending(*, rule_name: str | None = None) -> dict[str, Any]:
    """Retry pending/failed rule notifications (at-least-once with dedup key, RUL-2)
    and rule-triggered lens runs an outage interrupted (payload kind 'run_table')."""
    delivered = 0
    failed = 0
    with connect("app") as conn:
        if rule_name:
            rows = conn.execute(
                """SELECT d.*, r.action_config FROM tl.rule_deliveries d
                JOIN tl.rules r ON r.id=d.rule_id
                WHERE d.status IN ('pending','failed') AND r.name=%s
                ORDER BY d.id ASC""",
                (rule_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT d.*, r.action_config FROM tl.rule_deliveries d
                JOIN tl.rules r ON r.id=d.rule_id
                WHERE d.status IN ('pending','failed')
                ORDER BY d.id ASC"""
            ).fetchall()

    unavailable: set[str] = set()
    for row in rows:
        payload = row["payload"] or {}
        if payload.get("kind") == "run_table":
            lens = payload.get("lens")
            if lens in unavailable:
                continue  # still down this pass: one probe per lens, not one per message
            try:
                res = _run_rule_lens(lens, int(row["message_id"]))
            except (LookupError, LensSpecError) as exc:
                res = {"status": "failed", "errors": [str(exc)[:200]]}
            ok = res.get("status") not in RETRY_LENS_STATUSES
            if res.get("status") in ("provider_unavailable", "budget_exhausted"):
                unavailable.add(lens)
            with connect("app") as conn:
                if ok:
                    conn.execute(
                        """UPDATE tl.rule_deliveries
                        SET status='delivered', attempts=attempts+1, last_error=NULL, delivered_at=clock_timestamp()
                        WHERE id=%s""",
                        (row["id"],),
                    )
                else:
                    error = "; ".join(str(e) for e in (res.get("errors") or [])[:3]) or str(res.get("status"))
                    conn.execute(
                        """UPDATE tl.rule_deliveries
                        SET status='failed', attempts=attempts+1, last_error=%s
                        WHERE id=%s""",
                        (error[:300], row["id"]),
                    )
            if ok:
                delivered += 1
            else:
                failed += 1
            continue
        try:
            _deliver_one(dict(row), row["action_config"] or {})
            with connect("app") as conn:
                conn.execute(
                    """UPDATE tl.rule_deliveries
                    SET status='delivered', attempts=attempts+1, last_error=NULL, delivered_at=clock_timestamp()
                    WHERE id=%s""",
                    (row["id"],),
                )
            delivered += 1
        except Exception as exc:
            with connect("app") as conn:
                conn.execute(
                    """UPDATE tl.rule_deliveries
                    SET status='failed', attempts=attempts+1, last_error=%s
                    WHERE id=%s""",
                    (str(exc)[:300], row["id"]),
                )
            failed += 1
    return {"delivered": delivered, "failed": failed}


def evaluate_rules(
    *,
    rule_name: str | None = None,
    message_ids: list[int] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Evaluate enabled arrival rules against new messages (RUL-1, RUL-2, RUL-3)."""
    with connect("app") as conn:
        if rule_name:
            rules = conn.execute(
                "SELECT * FROM tl.rules WHERE enabled IS TRUE AND name=%s ORDER BY id",
                (rule_name,),
            ).fetchall()
        else:
            rules = conn.execute(
                "SELECT * FROM tl.rules WHERE enabled IS TRUE ORDER BY id"
            ).fetchall()

    if not rules:
        # RUL-3: With no rules defined, make zero outbound calls.
        return {"evaluated_rules": 0, "matched": 0, "notified": 0, "triggered_lenses": [], "deliveries": []}

    matched_total = 0
    notified_total = 0
    triggered_lenses: list[dict[str, Any]] = []
    deliveries: list[dict[str, Any]] = []

    for rule in rules:
        channels = list(rule["channels"] or [])
        with connect("app") as conn:
            clauses = ["m.superseded_by IS NULL"]
            params: list[Any] = []
            if channels:
                clauses.append("c.name = ANY(%s)")
                params.append(channels)
            if message_ids is not None:
                if not message_ids:
                    continue
                clauses.append("m.id = ANY(%s)")
                params.append(list(message_ids))
            else:
                clauses.append("m.id > %s")
                params.append(int(rule["last_message_id"]))
            params.append(int(limit))
            msgs = [
                dict(r)
                for r in conn.execute(
                    f"""SELECT m.id, c.name AS channel, m.happened_at, m.author_handle,
                        p.handle AS person_handle, m.content, m.fields
                    FROM tl.messages m
                    JOIN tl.channels c ON c.id=m.channel_id
                    LEFT JOIN tl.people p ON p.id=m.person_id
                    WHERE {' AND '.join(clauses)}
                    ORDER BY m.id ASC LIMIT %s""",
                    params,
                ).fetchall()
            ]

        if not msgs:
            continue

        matched_msgs = [m for m in msgs if _matches_filter(m, rule["filter"] or {})]
        # Optional sliding-window count threshold (RUL-1: e.g. "five such messages in an hour").
        min_count = int(rule["min_count"] or 1)
        window_s = rule["window_seconds"]
        firing_msgs: list[dict[str, Any]] = []
        if min_count <= 1:
            firing_msgs = matched_msgs
        else:
            for idx, m in enumerate(matched_msgs):
                if window_s is None:
                    count_in_win = idx + 1
                else:
                    t_end = m["happened_at"]
                    t_start = t_end - timedelta(seconds=int(window_s)) if isinstance(t_end, datetime) else None
                    count_in_win = sum(
                        1
                        for prev in matched_msgs[: idx + 1]
                        if t_start is None or prev["happened_at"] >= t_start
                    )
                if count_in_win >= min_count:
                    firing_msgs.append(m)

        matched_total += len(firing_msgs)
        action_kind = rule["action_kind"]
        action_cfg = rule["action_config"] or {}

        for m in firing_msgs:
            mid = int(m["id"])
            if action_kind == "run_table":
                lens_name = action_cfg.get("lens") or action_cfg["table"]
                res = _run_rule_lens(lens_name, mid)
                queued = res.get("status") in RETRY_LENS_STATUSES
                if queued:
                    _queue_lens_retry(dict(rule), mid, lens_name, res)
                triggered_lenses.append(
                    {
                        "rule": rule["name"],
                        "message_id": mid,
                        "lens": lens_name,
                        "result": res,
                        **({"queued_for_retry": True} if queued else {}),
                    }
                )
            elif action_kind == "notify":
                dedup_key = hashlib.sha256(f"{rule['id']}:{mid}".encode("utf-8")).hexdigest()[:32]
                payload = {
                    "rule": rule["name"],
                    "message_id": mid,
                    "channel": m["channel"],
                    "happened_at": m["happened_at"].isoformat() if isinstance(m["happened_at"], datetime) else str(m["happened_at"]),
                }
                with connect("app") as conn:
                    drow = conn.execute(
                        """INSERT INTO tl.rule_deliveries(rule_id, message_id, dedup_key, status, payload)
                        VALUES (%s,%s,%s,'pending',%s)
                        ON CONFLICT (dedup_key) DO NOTHING
                        RETURNING *""",
                        (rule["id"], mid, dedup_key, Jsonb(payload)),
                    ).fetchone()
                if drow:
                    try:
                        _deliver_one(dict(drow), action_cfg)
                        with connect("app") as conn:
                            conn.execute(
                                """UPDATE tl.rule_deliveries
                                SET status='delivered', attempts=1, delivered_at=clock_timestamp()
                                WHERE id=%s""",
                                (drow["id"],),
                            )
                        notified_total += 1
                        deliveries.append({"rule": rule["name"], "message_id": mid, "dedup_key": dedup_key, "status": "delivered"})
                    except Exception as exc:
                        with connect("app") as conn:
                            conn.execute(
                                """UPDATE tl.rule_deliveries
                                SET status='failed', attempts=1, last_error=%s
                                WHERE id=%s""",
                                (str(exc)[:300], drow["id"]),
                            )
                        deliveries.append({"rule": rule["name"], "message_id": mid, "dedup_key": dedup_key, "status": "failed", "error": str(exc)[:200]})

        if message_ids is None:
            max_seen = max(int(m["id"]) for m in msgs)
            with connect("app") as conn:
                conn.execute(
                    "UPDATE tl.rules SET last_message_id=GREATEST(last_message_id,%s), updated_at=clock_timestamp() WHERE id=%s",
                    (max_seen, rule["id"]),
                )

    return {
        "evaluated_rules": len(rules),
        "matched": matched_total,
        "notified": notified_total,
        "triggered_lenses": triggered_lenses,
        "deliveries": deliveries,
    }
