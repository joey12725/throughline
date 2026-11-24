"""POST /v1/orient: compact working-memory snapshot across visible channels.

Advice, planning, recent-state, and proxy questions depend on current context
that rarely matches literal keywords in a search query: latest health/sleep/
weight metrics, recent weather/finance/tasks digests, recent location visits,
upcoming calendar events, recent voice summaries, and the list of available
channels with their latest timestamps.

Which channels feed which section comes from channel roles declared by the
plugins (throughline.channel_roles): telemetry and location channels feed
recent_telemetry (ordered by orient_priority), calendar channels
upcoming_calendar, voice_summary channels recent_voice and chat channels
recent_threads. A section with no channel of its role is empty.

orient reads only through the caller-gated api.messages and api.people views
(plus api.person_brief when `people` are requested) so restricted askers see
only what their grants allow.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ... import channel_roles
from ...agent_view import local_time
from ...query import query_ex
from ...retrieval import images
from ...retrieval.scope_sql import lit
from ..deps import Token, audit, require, scope_of

router = APIRouter()

class OrientRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: datetime | None = None
    days: int = Field(default=14, ge=1, le=90)
    people: list[str] | None = Field(default=None, max_length=6)
    max_chars: int = Field(default=16000, ge=2000, le=60000)

    @model_validator(mode="before")
    @classmethod
    def coerce_aliases(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            if "person" in data and "people" not in data:
                val = data.pop("person")
                data["people"] = val if isinstance(val, list) else [val]
            if isinstance(data.get("people"), str):
                data["people"] = [data["people"]]
            if "date" in data and "as_of" not in data:
                data["as_of"] = data.pop("date")
            if "now" in data and "as_of" not in data:
                data["as_of"] = data.pop("now")
        return data

    @field_validator("as_of", mode="before")
    @classmethod
    def date_only(cls, value):
        if isinstance(value, str) and len(value.strip()) == 10:
            return value.strip() + "T23:59:59+00:00"
        return value

    @field_validator("as_of")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def _compact(text, limit=280):
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    return t[: limit - 3] + "..."


def _age_days(ref_dt, ts):
    if not ref_dt or not ts:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return round((ref_dt - ts).total_seconds() / 86400.0, 1)


@router.post("/v1/orient")
def orient(data: OrientRequest, token: Token):
    """Compact snapshot of visible channels, latest telemetry, calendar, and recent voice notes."""
    require(token, "owner", "agent")
    scope = scope_of(token)
    ref_dt = data.as_of or datetime.now(timezone.utc)
    ref_iso = ref_dt.isoformat()

    # 1. Visible channel overview (compact string map, replaces exploratory /v1/describe calls).
    ch_rows = query_ex(
        "SELECT channel, count(*) AS records, max(happened_at) AS latest"
        " FROM api.messages WHERE superseded_by IS NULL"
        " GROUP BY channel ORDER BY count(*) DESC",
        200,
        scope=scope,
        token_row=token,
        materialize=False,
    )["rows"]
    visible_channels = {r["channel"] for r in ch_rows}
    channels_out = {
        r["channel"]: f"{int(r['records'])} (latest {(local_time(r['latest']) or '')[:10]})"
        for r in ch_rows
    }

    # Roles of the visible channels (declared by plugins; see channel_roles).
    roles = channel_roles.load(visible_channels, scope=scope, token=token)

    # 2. Latest structured/telemetry entry (1 latest compact row per channel up to ref_dt).
    telem_channels = channel_roles.with_role(roles, *channel_roles.ORIENT_STATE_ROLES)

    telemetry_out = {}
    if telem_channels:
        t_stmt = (
            "SELECT id, channel, external_id, happened_at, left(content, 350) AS content FROM ("
            " SELECT id, channel, external_id, happened_at, content,"
            "  row_number() OVER (PARTITION BY channel ORDER BY happened_at DESC, id DESC) AS rn"
            " FROM api.messages"
            f" WHERE superseded_by IS NULL AND channel = ANY({lit(telem_channels, 'text[]')})"
            f"  AND happened_at <= {lit(ref_iso)}::timestamptz"
            ") s WHERE rn = 1 ORDER BY channel, happened_at DESC"
        )
        t_rows = query_ex(t_stmt, 50, scope=scope, token_row=token, materialize=False)["rows"]
        order = {name: i for i, name in enumerate(telem_channels)}
        t_rows.sort(key=lambda r: order.get(r["channel"], len(order)))
        for r in t_rows:
            telemetry_out[r["channel"]] = dict(
                time=local_time(r.get("happened_at")),
                age_days=_age_days(ref_dt, r.get("happened_at")),
                text=_compact(r.get("content"), 180),
                external_id=r.get("external_id"),
                message_id=r.get("id"),
            )

    # 3. Strictly upcoming calendar events [ref_dt, ref_dt + days] (never past events!).
    cal_out = []
    cal_channels = channel_roles.with_role(roles, channel_roles.CALENDAR)
    if cal_channels:
        cal_hi = (ref_dt + timedelta(days=data.days)).isoformat()
        cal_stmt = (
            "SELECT id, channel, external_id, happened_at, ended_at, left(content, 240) AS content"
            " FROM api.messages"
            f" WHERE superseded_by IS NULL AND channel = ANY({lit(cal_channels, 'text[]')})"
            f"  AND happened_at >= {lit(ref_iso)}::timestamptz"
            f"  AND happened_at <= {lit(cal_hi)}::timestamptz"
            " ORDER BY happened_at ASC, id ASC LIMIT 12"
        )
        cal_rows = query_ex(cal_stmt, 15, scope=scope, token_row=token, materialize=False)["rows"]
        for r in cal_rows:
            item = dict(
                time=local_time(r.get("happened_at")),
                text=_compact(r.get("content"), 160),
                channel=r.get("channel"),
                external_id=r.get("external_id"),
                message_id=r.get("id"),
            )
            if r.get("ended_at"):
                item["ended_at"] = local_time(r.get("ended_at"))
            cal_out.append(item)

    # 4. Recent voice recording summaries (latest 3).
    voice_out = []
    voice_channels = channel_roles.with_role(roles, channel_roles.VOICE_SUMMARY)
    if voice_channels:
        v_stmt = (
            "SELECT id, channel, external_id, happened_at, left(content, 240) AS content"
            " FROM api.messages"
            f" WHERE superseded_by IS NULL AND channel = ANY({lit(voice_channels, 'text[]')})"
            f"  AND happened_at <= {lit(ref_iso)}::timestamptz"
            " ORDER BY happened_at DESC, id DESC LIMIT 3"
        )
        v_rows = query_ex(v_stmt, 5, scope=scope, token_row=token, materialize=False)["rows"]
        for r in v_rows:
            voice_out.append(
                dict(
                    time=local_time(r.get("happened_at")),
                    text=_compact(r.get("content"), 160),
                    channel=r.get("channel"),
                    external_id=r.get("external_id"),
                    message_id=r.get("id"),
                )
            )

    # 5. Most recently active non-ignored chat threads within [ref_dt - days, ref_dt].
    recent_threads_out = []
    chat_channels = sorted(channel_roles.with_role(roles, channel_roles.CHAT))
    if chat_channels:
        lo_iso = (ref_dt - timedelta(days=data.days)).isoformat()
        rt_stmt = (
            "SELECT id, channel, external_id, thread_id, thread_title, happened_at,"
            " author, left(content, 220) AS content"
            " FROM ("
            "  SELECT m.id, m.channel, m.external_id, m.thread_id, m.thread_title, m.happened_at,"
            "   coalesce(m.display_name, m.author_handle) AS author, m.content,"
            "   row_number() OVER (PARTITION BY m.thread_id ORDER BY m.happened_at DESC, m.id DESC) AS rn"
            "  FROM api.messages m"
            "  LEFT JOIN api.people p ON p.id = m.person_id"
            f"  WHERE m.superseded_by IS NULL AND m.channel = ANY({lit(chat_channels, 'text[]')})"
            f"   AND m.thread_id IS NOT NULL AND length(trim(coalesce(m.content, ''))) > 0"
            f"   AND coalesce(p.status, '') <> 'ignored'"
            f"   AND m.happened_at >= {lit(lo_iso)}::timestamptz"
            f"   AND m.happened_at <= {lit(ref_iso)}::timestamptz"
            " ) s WHERE rn = 1 ORDER BY happened_at DESC, id DESC LIMIT 10"
        )
        try:
            rt_rows = query_ex(rt_stmt, 12, scope=scope, token_row=token, materialize=False)["rows"]
            for r in rt_rows:
                recent_threads_out.append(
                    dict(
                        time=local_time(r.get("happened_at")),
                        channel=r.get("channel"),
                        thread=r.get("thread_title"),
                        thread_id=r.get("thread_id"),
                        author=r.get("author"),
                        text=_compact(r.get("content"), 140),
                        external_id=r.get("external_id"),
                        message_id=r.get("id"),
                    )
                )
        except Exception:
            recent_threads_out = []

    # 6. Optional fast per-person summaries + recent cross-channel messages.
    people_out = []
    if data.people:
        for name in data.people:
            if not name or not name.strip():
                continue
            try:
                p_rows = query_ex(
                    f"SELECT id, display_name, handle, note FROM api.person({lit(name.strip())}) LIMIT 3",
                    5,
                    scope=scope,
                    token_row=token,
                    materialize=False,
                )["rows"]
                for pr in p_rows[:2]:
                    pid = int(pr["id"])
                    ch_stats = query_ex(
                        "SELECT channel, count(*) AS messages, max(happened_at) AS latest"
                        f" FROM api.messages WHERE superseded_by IS NULL AND person_id = {pid}"
                        f"  AND happened_at <= {lit(ref_iso)}::timestamptz"
                        " GROUP BY channel ORDER BY count(*) DESC LIMIT 10",
                        12,
                        scope=scope,
                        token_row=token,
                        materialize=False,
                    )["rows"]
                    rm_stmt = (
                        "SELECT id, channel, external_id, thread_title, happened_at,"
                        " coalesce(display_name, author_handle) AS author, left(content, 240) AS content"
                        " FROM api.messages"
                        f" WHERE superseded_by IS NULL AND person_id = {pid}"
                        f"  AND happened_at <= {lit(ref_iso)}::timestamptz"
                        "  AND length(trim(coalesce(content, ''))) > 0"
                        " ORDER BY happened_at DESC, id DESC LIMIT 6"
                    )
                    rm_rows = query_ex(rm_stmt, 8, scope=scope, token_row=token, materialize=False)["rows"]
                    recent_msgs = [
                        dict(
                            time=local_time(r.get("happened_at")),
                            channel=r.get("channel"),
                            thread=r.get("thread_title"),
                            author=r.get("author"),
                            text=_compact(r.get("content"), 180),
                            external_id=r.get("external_id"),
                            message_id=r.get("id"),
                        )
                        for r in rm_rows
                    ]
                    people_out.append(
                        dict(
                            query=name,
                            person_id=pid,
                            display_name=pr.get("display_name"),
                            handle=pr.get("handle"),
                            note=pr.get("note"),
                            channels={
                                r["channel"]: f"{int(r['messages'])} (latest {(local_time(r['latest']) or '')[:10]})"
                                for r in ch_stats
                            },
                            recent_messages=recent_msgs,
                        )
                    )
            except Exception:
                continue

    images.attach(
        images.reader(scope, token),
        cal_out
        + voice_out
        + recent_threads_out
        + [m for p in people_out for m in p["recent_messages"]],
    )

    result = dict(
        as_of=local_time(ref_dt),
        upcoming_calendar_from=local_time(ref_dt),
        channels=channels_out,
        recent_telemetry=telemetry_out,
        upcoming_calendar=cal_out,
        recent_voice=voice_out,
        recent_threads=recent_threads_out,
        note=(
            "Compact working-memory snapshot across your visible channels. "
            "upcoming_calendar only contains events at or after as_of. "
            "Use POST /v1/find for topic searches, POST /v1/timeline for exhaustive "
            "history, or SQL over api.messages / api.threads / api.ch_<channel> for full series."
        ),
    )
    if people_out:
        result["people"] = people_out
    audit(token, len(channels_out), operation="orient")
    return result
