"""First lenses, pages and queries for a new install.

Onboarding offers a few **starter lenses** (structured tables an AI keeps
filled from your conversations), the **page families** (daily pages, person
profiles, event pages) and a handful of **first queries**, all adapted to
the sources this install actually has. One **pace** setting trades freshness
for cost: it sets how often lenses and pages run, how far back the first run
reads, and the per-run budgets.

Lenses and pages call the model configured for the Librarian role
(THROUGHLINE_TABLE_LLM_*, set by onboarding.providers); nothing here runs a
model until the owner presses Run or the scheduler's next tick.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..db import connect
from .errors import SetupError

# ───────────── pace ─────────────

PACES = {
    "live": dict(
        label="Live",
        description="Pages and lenses catch up within the hour. Most model calls.",
        lens_schedule="*/30 * * * *",
        page_schedule="*/30 * * * *",
        page_min_hours=1,
        people_schedule="15 * * * *",
        events_schedule="0 */6 * * *",
        lookback_days=30,
        max_cost_per_run=0.5,
        calls_per_day=48,
    ),
    "balanced": dict(
        label="Balanced",
        description="A few times a day. Fresh enough for daily life, a fraction of the calls.",
        lens_schedule="0 */3 * * *",
        page_schedule="0 */3 * * *",
        page_min_hours=4,
        people_schedule="15 */6 * * *",
        events_schedule="0 6 * * *",
        lookback_days=14,
        max_cost_per_run=0.25,
        calls_per_day=8,
    ),
    "thrifty": dict(
        label="Thrifty",
        description="Once a night. Yesterday is always written up; the cheapest option.",
        lens_schedule="30 3 * * *",
        page_schedule="0 4 * * *",
        page_min_hours=20,
        people_schedule="30 4 * * *",
        events_schedule="0 5 * * 1",
        lookback_days=7,
        max_cost_per_run=0.1,
        calls_per_day=1,
    ),
}
DEFAULT_PACE = "balanced"


def paces():
    return [dict(id=key, **{k: v for k, v in value.items()}) for key, value in PACES.items()]


def _pace(pace):
    if pace not in PACES:
        raise SetupError("Unknown pace", f"{pace!r} is not a pace.", "Choose live, balanced or thrifty.")
    return PACES[pace]


# ───────────── what the install has ─────────────

CONVERSATION_CATEGORIES = {"messages", "meetings", "email", "social"}


def _channels(conn):
    return conn.execute(
        "SELECT c.name, c.plugin, count(m.id) AS n FROM tl.channels c"
        " LEFT JOIN tl.messages m ON m.channel_id=c.id AND m.superseded_by IS NULL"
        " WHERE NOT c.is_reference GROUP BY c.name, c.plugin HAVING count(m.id) > 0 ORDER BY count(m.id) DESC"
    ).fetchall()


CONVERSATION_ROLES = ("chat", "transcript", "email")


def _channel_roles(names, conn=None):
    """{name: ChannelRole} of the given channels (owner view, all channels;
    the legacy naming conventions apply when none declares a role)."""
    from .. import channel_roles

    names = [n for n in names if n]
    if not names:
        return {}
    statement = (
        "SELECT name, role, orient_priority, context_window_min FROM tl.channels WHERE name = ANY(%s)"
    )
    try:
        if conn is not None:
            rows = conn.execute(statement, (names,)).fetchall()
        else:
            with connect("app") as own:
                rows = own.execute(statement, (names,)).fetchall()
    except Exception:
        rows = []
    seen = {r["name"] for r in rows}
    return channel_roles.resolve(list(rows) + [dict(name=n) for n in names if n not in seen])


def conversation_channels(conn=None):
    """Channels holding people talking: chats, transcripts, email."""
    from . import catalog

    own = conn is None
    if own:
        cm = connect("app")
        conn = cm.__enter__()
    try:
        rows = _channels(conn)
    finally:
        if own:
            cm.__exit__(None, None, None)
    by_plugin = {}
    try:
        for _, spec in catalog._all_specs():
            by_plugin[spec.id] = spec
    except Exception:
        pass
    out = []
    roles = _channel_roles([row["name"] for row in rows], conn if not own else None)
    declared = any(r.declared for r in roles.values())
    for row in rows:
        name = row["name"]
        if declared:
            # Plugins declare roles: a conversation channel is one whose role
            # says people talk in it.
            if roles[name].role in CONVERSATION_ROLES:
                out.append(name)
            continue
        spec = by_plugin.get(row["plugin"])
        category = spec.category if spec else None
        looks = re.match(r"^(chat|transcripts|recordings|email|imap|messages|sms|signal|telegram|imessage|granola|zoom|wave)\b", name)
        if (category in CONVERSATION_CATEGORIES or looks) and not name.startswith(("pages.", "recordings.")):
            out.append(name)
    return out


def _owner_name(conn):
    row = conn.execute(
        "SELECT coalesce(p.display_name, a.display_name) AS name FROM tl.accounts a"
        " LEFT JOIN tl.people p ON p.is_owner AND p.merged_into IS NULL ORDER BY a.id LIMIT 1"
    ).fetchone()
    return (row or {}).get("name") or "the owner"


def _tz():
    from .. import tz

    return tz.name() or "UTC"


def model_status():
    """Whether lenses and pages have a model, from the Librarian role."""
    from .providers import current_role

    role = current_role("semantic")
    base = os.getenv("THROUGHLINE_TABLE_LLM_BASE_URL") or os.getenv("THROUGHLINE_MODEL_BASE_URL")
    key = os.getenv("THROUGHLINE_TABLE_LLM_KEY") or os.getenv("OPENROUTER_API_KEY") or os.getenv("THROUGHLINE_MODEL_KEY")
    ready = bool(base or key)
    return dict(
        ready=ready,
        provider=role.get("provider"),
        model=os.getenv("THROUGHLINE_TABLE_LLM_MODEL") or role.get("model"),
        hint=None if ready else "Lenses and pages need a model: choose one for the Librarian in the Intelligence step"
        " (a CLI proxy or local model costs nothing per call).",
    )


# ───────────── lens templates ─────────────

COMMON_RULES = (
    "{owner} lives in {tz}. date_context.now_local is the current time; each item has its own local_time.\n"
    "You are reading ONE DAY of {owner}'s conversations (or one part of a busy day), all sources together. "
    "The same thing is often mentioned in several threads: emit ONE row per real-world item, citing every message "
    "that supports it. Ignore bots, AI assistants and automated notifications. Returning zero rows is normal and "
    "preferred over weak guesses. Never write raw speaker labels; use the person's name if the conversation makes "
    "it clear, else 'someone in a recording'.\n"
    "context_query_results lists rows this lens already holds: if today is about the same item, set same_as_row_id "
    "to that row's row_id and fold the new information in.\n"
)

LENS_TEMPLATES = {
    "plans": dict(
        title="Plans & commitments",
        pitch="Events, plans and promises with dates, pulled from your conversations — your calendar's missing half.",
        icon="calendar-check",
        natural_key=["title", "when_text"],
        prompt=(
            "Extract items that belong on {owner}'s calendar or dated to-do list: events or meetups {owner} is "
            "attending, invited to or hosting; commitments {owner} made or someone relies on {owner} for; concrete "
            "plans {owner} is part of; deadlines. Skip other people's plans and anything already over.\n"
            "Resolve relative dates against the local_time of the message that says them (look weekdays up in "
            "date_context.calendar). start_at: ISO-8601 with the local offset, or null if no date is known; "
            "all_day when only the date is known. notes: a short verbatim quote."
        ),
        columns=[
            ("title", "text", False, "Short title of the event, plan or commitment"),
            ("when_text", "text", True, "When, as said in the conversation"),
            ("start_at", "timestamp", True, "Resolved start time, if known"),
            ("all_day", "boolean", True, "Only the date is known"),
            ("with_people", "text", True, "Who else is involved"),
            ("kind", "text", True, "event, plan, commitment or deadline"),
            ("notes", "text", True, "Details and a short quote"),
        ],
        context="SELECT id AS row_id, title, when_text, start_at FROM api.lens_{name} "
        "WHERE start_at IS NULL OR start_at >= now() - interval '3 days' ORDER BY start_at NULLS LAST LIMIT 300",
    ),
    "recommendations": dict(
        title="Recommendations",
        pitch="Every book, show, film, album, place or product someone recommended to you — or you to them.",
        icon="sparkles",
        natural_key=["title", "kind"],
        prompt=(
            "Extract every specific recommendation involving {owner}: someone recommends something to {owner}, "
            "{owner} recommends something, or {owner} says they want to try something specific that came up. "
            "kind: book, tv_show, movie, music, podcast, place, restaurant, product or other. Use the canonical "
            "title (fix obvious transcription errors). Skip passing mentions with no recommendation."
        ),
        columns=[
            ("title", "text", False, "What was recommended"),
            ("kind", "text", False, "book, tv_show, movie, music, podcast, place, restaurant, product or other"),
            ("recommended_by", "text", True, "Who recommended it"),
            ("recommended_to", "text", True, "Who it was recommended to"),
            ("notes", "text", True, "Why, what it's like, and a short quote"),
        ],
        context="SELECT id AS row_id, title, kind, recommended_by FROM api.lens_{name} ORDER BY title LIMIT 400",
    ),
    "important_dates": dict(
        title="Important dates",
        pitch="Birthdays, anniversaries and other dates people mention, so you never miss one.",
        icon="cake",
        natural_key=["person", "occasion"],
        prompt=(
            "Extract recurring or significant dates tied to a person: birthdays, anniversaries, due dates, "
            "graduations, moves, surgeries. Only dates actually stated or clearly implied in the conversation. "
            "month_day: MM-DD when known; year when known."
        ),
        columns=[
            ("person", "text", False, "Whose date it is"),
            ("occasion", "text", False, "birthday, anniversary, due date, …"),
            ("month_day", "text", True, "MM-DD"),
            ("year", "integer", True, "Year, if known"),
            ("notes", "text", True, "A short quote"),
        ],
        context="SELECT id AS row_id, person, occasion, month_day FROM api.lens_{name} ORDER BY person LIMIT 400",
    ),
}


def lens_name(template_id):
    return f"starter_{template_id}"


def _worker_config():
    cfg = dict(timeout_s=300, max_chars_per_item=120000, min_confidence=0.5, dedupe=True, include_fields=False)
    price = os.getenv("THROUGHLINE_TABLE_LLM_PRICE_PER_MILLION")
    if price:
        cfg["price_per_million"] = float(price)
    return cfg


def build_lens(template_id, channels, pace, owner, tz):
    t = LENS_TEMPLATES[template_id]
    p = _pace(pace)
    name = lens_name(template_id)
    fmt = dict(owner=owner, tz=tz, name=name)
    return dict(
        name=name,
        description=t["pitch"],
        channels=list(channels),
        grain="day",
        worker="llm",
        schedule=p["lens_schedule"],
        natural_key=list(t["natural_key"]),
        max_items_per_run=10,
        max_cost_per_run=p["max_cost_per_run"],
        prompt=COMMON_RULES.format(**fmt) + t["prompt"].format(**fmt),
        context_query=t["context"].format(**fmt),
        worker_config=dict(_worker_config(), lookback_days=p["lookback_days"]),
        columns=[dict(name=n, type=ty, nullable=nl, description=d) for n, ty, nl, d in t["columns"]],
    )


def lenses_view():
    from .. import lenses as L

    with connect("app") as conn:
        channels = conversation_channels(conn)
        existing = {r["name"]: r for r in L.list_lenses(conn=conn)}
    templates = []
    for key, t in LENS_TEMPLATES.items():
        name = lens_name(key)
        row = existing.get(name)
        templates.append(dict(
            id=key, title=t["title"], pitch=t["pitch"], icon=t["icon"], lens=name,
            columns=[c[0] for c in t["columns"]],
            available=bool(channels),
            why_not=None if channels else "Connect a chat, email or meeting source first.",
            enabled=bool(row and row.get("enabled", True)),
            rows=(row or {}).get("active_rows") or 0,
        ))
    from .state import get as get_state

    choices = get_state()["choices"]
    return dict(
        model=model_status(),
        paces=paces(),
        pace=choices.get("pace") or DEFAULT_PACE,
        channels=channels,
        templates=templates,
        others=[dict(name=n, rows=r.get("active_rows") or 0, description=r.get("description") or "")
                for n, r in existing.items() if not n.startswith("starter_")],
    )


def apply_lenses(template_ids, pace, token=None):
    from .. import lenses as L
    from .state import save as save_state

    pace = pace or DEFAULT_PACE
    _pace(pace)
    unknown = [t for t in template_ids if t not in LENS_TEMPLATES]
    if unknown:
        raise SetupError("Unknown lens", "No starter lens called " + ", ".join(unknown) + ".", status=404)
    with connect("app") as conn:
        channels = conversation_channels(conn)
        owner = _owner_name(conn)
    if template_ids and not channels:
        raise SetupError("Nothing for lenses to read yet",
                         "Lenses read conversations, and no chat, email or meeting source has data yet.",
                         "Import a conversation source first, or come back after the first import finishes.", status=409)
    defined = []
    for key in template_ids:
        spec = build_lens(key, channels, pace, owner, _tz())
        try:
            row = L.define_lens(spec, token=token)
        except (ValueError, LookupError) as error:
            raise SetupError("The lens couldn't be created", str(error), status=422) from None
        quote = _quote(row["name"], pace)
        defined.append(dict(name=row["name"], template=key, quote=quote))
    # Starter lenses the owner unticked are switched off, not deleted.
    for key in LENS_TEMPLATES:
        if key in template_ids:
            continue
        try:
            current = L.get_lens(lens_name(key))
        except LookupError:
            continue
        spec = build_lens(key, current.get("channels") or channels, pace, owner, _tz())
        spec["enabled"] = False
        try:
            L.define_lens(spec, token=token)
        except Exception:
            pass
    save_state({"choices": {"pace": pace, "lenses": list(template_ids)}})
    return dict(lenses=defined, view=lenses_view())


def _quote(name, pace):
    from .. import lenses as L
    from datetime import date, timedelta

    since = (date.today() - timedelta(days=_pace(pace)["lookback_days"])).isoformat()
    try:
        q = L.quote_lens(name, since=since)
    except Exception as error:
        return dict(error=str(error))
    keep = {k: q.get(k) for k in ("items", "tokens", "estimated_tokens", "calls", "cost_usd", "monthly_cap_usd", "within_monthly_cap")}
    keep["since"] = since
    return keep


def run_lens_job(job_id, name, pace):
    from .. import lenses as L
    from datetime import date, timedelta

    from . import jobs

    tracker = jobs.Tracker(job_id)
    tracker.update(phase="Reading your recent days")
    since = (date.today() - timedelta(days=_pace(pace)["lookback_days"])).isoformat()
    try:
        result = L.run_lens(name, trigger_kind="manual", since=since, max_cost=_pace(pace)["max_cost_per_run"] * 4)
    except Exception as error:
        raise SetupError("The lens run failed", str(error),
                         "Check the Librarian model in the Intelligence step, then retry.") from None
    rows = 0
    try:
        rows = L.get_lens(name).get("active_rows") or 0
    except Exception:
        pass
    status = (result or {}).get("status")
    tracker.update(done=1, total=1, unit="runs", delivered=rows,
                   phase=f"Found {rows} row{'s' if rows != 1 else ''}" + (f" ({status})" if status and status not in ("ok", "done", "completed") else ""))
    tracker.flush(force=True)


# ───────────── pages ─────────────
#
# Pages are lenses with output = "pages" (docs/v11/pages.md): the Librarian
# reads each window and writes cited pages. A page family here is one of the
# example pages lenses, defined with the pace's schedule and lookback, reading
# the install's conversation channels (plus the example's other channels that
# hold data). Markdown export is the one global sink in pages.toml.

PAGE_FAMILIES = {
    "daily": dict(title="Daily pages", icon="calendar-days", lens="daily_pages", kind="days",
                  schedule="page_schedule",
                  pitch="One page per day: who you saw, what you talked about, plans, decisions and the details worth keeping."),
    "people": dict(title="Person profiles", icon="contact", lens="people_pages", kind="people",
                   schedule="people_schedule",
                   pitch="A living profile for each person who matters: tastes, opinions, important details, open threads."),
    "events": dict(title="Event pages", icon="calendar-range", lens="event_pages", kind="events",
                   schedule="events_schedule",
                   pitch="A page for every gathering, trip or event: when, where, who was there, what happened."),
}


def _pages_module():
    try:
        from .. import pages
    except ImportError:
        return None
    return pages


def _family_spec(family, pace, channels, have_channels=()):
    from .. import lenses as L
    from ..pages import librarian

    f = PAGE_FAMILIES[family]
    spec = L.load_spec_file(Path(librarian.__file__).parent / "examples" / f"{f['lens']}.toml")
    p = _pace(pace)
    spec["schedule"] = p[f["schedule"]]
    have = list(have_channels)
    have_set = set(have)
    wanted_roles = set(spec.pop("channel_roles", None) or [])
    roles = _channel_roles(have) if wanted_roles else {}
    named = [c for c in spec.get("channels") or [] if c in have_set]
    by_role = [c for c in have if roles.get(c) and roles[c].role in wanted_roles]
    spec["channels"] = list(dict.fromkeys(list(channels) + named + by_role))
    spec["max_cost_per_run"] = p["max_cost_per_run"]
    cfg = dict(spec.get("worker_config") or {})
    cfg["lookback_days"] = min(p["lookback_days"], 30)
    spec["worker_config"] = cfg
    spec["description"] = f"{spec.get('description') or f['pitch']} (set up by onboarding)"
    # The example's model block is a comment; the Librarian model applies.
    return spec


OURS = "# Written by Throughline onboarding"


def _sinks_toml(markdown_dir):
    path = str(Path(markdown_dir).expanduser() / "{slug}.md").replace("\\", "\\\\").replace('"', '\\"')
    body = f'\n[[sinks]]\nkind = "markdown"\nname = "markdown"\npath = "{path}"\n'
    import hashlib

    digest = hashlib.sha256(body.encode()).hexdigest()[:16]
    return f"{OURS} ({digest}); edit freely: once edited, onboarding leaves it alone.{body}"


def _ours(path):
    """The file is absent, or onboarding wrote it and nobody edited it since."""
    import hashlib

    if not path.exists():
        return True
    text = path.read_text()
    head, _, body = text.partition("\n")
    if not head.startswith(OURS + " ("):
        return False
    digest = head[len(OURS) + 2:].split(")")[0]
    return hashlib.sha256(("\n" + body).encode()).hexdigest()[:16] == digest


def _onboarding_lenses():
    from .state import get as get_state

    return set(get_state()["choices"].get("pages_lenses") or [])


def pages_view():
    pages = _pages_module()
    from .. import lenses as L
    from .state import get as get_state

    choices = get_state()["choices"]
    if pages is None:
        return dict(available=False, why_not="This build doesn't include Pages.", families=[], paces=paces(),
                    pace=choices.get("pace") or DEFAULT_PACE, model=model_status(), markdown_dir=None)
    with connect("app") as conn:
        channels = conversation_channels(conn)
        defined = {r["name"]: r for r in L.list_lenses(conn=conn)}
        counts = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, count(*) AS n FROM tl.pages WHERE status='active' GROUP BY kind")}
    families = []
    for key, f in PAGE_FAMILIES.items():
        row = defined.get(f["lens"])
        families.append(dict(id=key, title=f["title"], pitch=f["pitch"], icon=f["icon"], lens=f["lens"], kind=f["kind"],
                             enabled=bool(row and row.get("enabled", True)),
                             available=bool(channels),
                             why_not=None if channels else "Connect a chat, email or meeting source first.",
                             pages=counts.get(f["kind"], 0)))
    from ..pages import sinks

    return dict(available=True, why_not=None, families=families, paces=paces(),
                pace=choices.get("pace") or DEFAULT_PACE, model=model_status(),
                markdown_dir=choices.get("pages_markdown_dir"), sinks_config=str(sinks.config_path()))


def apply_pages(family_ids, pace, markdown_dir=None, token=None):
    pages = _pages_module()
    if pages is None:
        raise SetupError("Pages aren't available", "This build doesn't include Pages.", status=404)
    from .. import lenses as L
    from ..pages import sinks
    from .state import save as save_state

    pace = pace or DEFAULT_PACE
    _pace(pace)
    unknown = [f for f in family_ids if f not in PAGE_FAMILIES]
    if unknown:
        raise SetupError("Unknown page family", ", ".join(unknown), status=404)
    with connect("app") as conn:
        channels = conversation_channels(conn)
        have = [r["name"] for r in _channels(conn)]
    if family_ids and not channels:
        raise SetupError("Nothing for pages to read yet", "No chat, email or meeting source has data yet.",
                         "Import a conversation source first.", status=409)
    ours = _onboarding_lenses()
    written, kept, plans = [], [], {}
    for key, f in PAGE_FAMILIES.items():
        name = f["lens"]
        try:
            current = L.get_lens(name)
        except LookupError:
            current = None
        if current is not None and name not in ours:
            # A pages lens the owner defined: never redefined from here.
            if key in family_ids:
                kept.append(key)
            continue
        if key not in family_ids and current is None:
            continue
        spec = _family_spec(key, pace, channels, have)
        if key not in family_ids:
            # Switched off, not deleted (its pages stay).
            spec["enabled"] = False
        try:
            L.define_lens(spec, token=token)
        except (ValueError, LookupError) as error:
            raise SetupError("The page settings aren't valid", str(error), status=422) from None
        ours.add(name)
        # Recorded at once: a later failure must not make this lens look owner-defined.
        save_state({"choices": {"pages_lenses": sorted(ours)}})
        if key in family_ids:
            written.append(key)
    for key in written + kept:
        q = _quote(PAGE_FAMILIES[key]["lens"], pace)
        plans[key] = dict(error=q["error"]) if q.get("error") else dict(due=int(q.get("items") or 0), cost_usd=q.get("cost_usd"))
    sinks_note = None
    path = sinks.config_path()
    if markdown_dir:
        if _ours(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_sinks_toml(markdown_dir))
        else:
            sinks_note = f"{path} was written by hand, so it was left alone; add a markdown sink there yourself."
    elif path.exists() and _ours(path):
        # Export switched off: remove the sink file onboarding wrote (never one the owner wrote).
        path.unlink()
    save_state({"choices": {"pace": pace, "pages": list(family_ids), "pages_markdown_dir": markdown_dir,
                            "pages_lenses": sorted(ours)}})
    return dict(written=written, kept=kept, plans=plans, sinks_note=sinks_note, view=pages_view())


def run_pages_job(job_id, family, max_subjects=2):
    from datetime import date, datetime, timedelta, timezone

    from .. import lenses as L
    from . import jobs
    from .state import get as get_state

    if family not in PAGE_FAMILIES:
        raise SetupError("Unknown page family", family, status=404)
    name = PAGE_FAMILIES[family]["lens"]
    pace = get_state()["choices"].get("pace") or DEFAULT_PACE
    tracker = jobs.Tracker(job_id)
    tracker.update(phase="Writing your most recent pages", done=0, total=max_subjects, unit="pages")
    started = datetime.now(timezone.utc)
    max_cost = _pace(pace)["max_cost_per_run"] * 4
    # Day-grain lenses take the oldest pending days first, so start with the
    # last few days; only when those hold nothing, widen to the pace's lookback.
    result = None
    for days in (max_subjects + 1, _pace(pace)["lookback_days"]):
        since = (date.today() - timedelta(days=days)).isoformat()
        try:
            result = L.run_lens(name, trigger_kind="manual", since=since, limit=max_subjects, max_cost=max_cost)
        except Exception as error:
            raise SetupError("Writing pages failed", str(error),
                             "Check the Librarian model in the Intelligence step, then retry.") from None
        if result.get("status") != "done" or result.get("considered"):
            break
    status = result.get("status")
    if status == "busy":
        raise SetupError("Pages are already being written", "Another run of this lens is in progress.",
                         "Wait a minute and retry.", status=409)
    if status == "provider_unavailable":
        raise SetupError("The Librarian model didn't answer", "; ".join(
            str(e.get("error") if isinstance(e, dict) else e) for e in (result.get("errors") or [])[:3]) or "The model endpoint was unreachable.",
            "Check the Librarian model in the Intelligence step, then retry.")
    if status == "budget_exhausted":
        raise SetupError("The cost cap stopped this run", "This run hit its cost cap before writing a page.",
                         "Raise the monthly cap or choose a cheaper pace, then retry.", status=409)
    with connect("app") as conn:
        n = conn.execute("SELECT count(*) AS n FROM tl.pages WHERE updated_by=%s AND updated_at >= %s",
                         (f"lens:{name}", started)).fetchone()["n"]
    if status == "failed" and not n:
        detail = "; ".join(str(e.get("error") if isinstance(e, dict) else e) for e in (result.get("errors") or [])[:3])
        raise SetupError("No page could be written", detail or "The run failed.",
                         "Check the Librarian model in the Intelligence step, then retry.")
    tracker.update(done=n, total=max(n, 1), delivered=n,
                   phase=f"Wrote {n} page{'s' if n != 1 else ''}" if n else "Nothing new to write yet")
    tracker.flush(force=True)


# ───────────── first queries ─────────────


def queries():
    """Starter questions, each as a runnable request, chosen by what the
    install holds. kind: sql (POST /v1/sql), find (POST /v1/find) or timeline."""
    with connect("app") as conn:
        counts = {r["name"]: r["n"] for r in _channels(conn)}
        has_people = conn.execute(
            "SELECT count(*) AS n FROM tl.people WHERE merged_into IS NULL AND NOT is_owner"
        ).fetchone()["n"]
        track = conn.execute("SELECT count(*) AS n FROM tl.location_track").fetchone()["n"]
        top = conn.execute(
            "SELECT p.display_name FROM tl.messages m JOIN tl.people p ON p.id=m.person_id"
            " WHERE NOT p.is_owner AND m.superseded_by IS NULL GROUP BY p.display_name"
            " ORDER BY count(*) DESC LIMIT 1"
        ).fetchone()
        lens_rows = conn.execute(
            "SELECT d.name FROM tl.table_defs d WHERE d.name LIKE 'starter_%' ORDER BY d.name"
        ).fetchall()
    out = []
    convo = bool(counts)
    if convo:
        out.append(dict(
            id="search", title="Search everything", kind="find",
            question="What have I said about plans this month?",
            explain="POST /v1/find mixes keyword and meaning search and returns whole moments of conversation, not just lines.",
            request={"query": "plans this month", "k": 5},
        ))
        out.append(dict(
            id="sources", title="What's in here?", kind="sql",
            question="How many records does each source hold, and how far back?",
            explain="Every source is one channel in api.messages. Plain SQL works on all of it.",
            request={"statement": "SELECT channel, count(*) AS records, min(happened_at)::date AS since,"
                     " max(happened_at)::date AS latest FROM api.messages GROUP BY channel ORDER BY records DESC"},
        ))
        out.append(dict(
            id="week", title="My week at a glance", kind="sql",
            question="How busy was each day this past week?",
            explain="Group by day in your own time zone.",
            request={"statement": "SELECT (happened_at AT TIME ZONE '" + _tz().replace("'", "") + "')::date AS day,"
                     " count(*) AS records, count(DISTINCT person_id) AS people FROM api.messages"
                     " WHERE happened_at > now() - interval '7 days' GROUP BY 1 ORDER BY 1"},
        ))
    if has_people:
        out.append(dict(
            id="people", title="Who I talk to most", kind="sql",
            question="Who did I exchange the most messages with in the last 90 days?",
            explain="People are linked across sources, so this counts every channel together.",
            request={"statement": "SELECT display_name AS person, count(*) AS messages, max(happened_at)::date AS last"
                     " FROM api.messages WHERE person_id IS NOT NULL AND person_id <> coalesce((SELECT id FROM api.people"
                     " WHERE is_owner), 0) AND happened_at > now() - interval '90 days'"
                     " GROUP BY display_name ORDER BY messages DESC LIMIT 10"},
        ))
    if top and top["display_name"]:
        name = top["display_name"].replace("'", "''")
        out.append(dict(
            id="brief", title=f"Catch up on {top['display_name']}", kind="sql",
            question=f"What's going on with {top['display_name']}?",
            explain="api.person_brief(id) gathers a person's channels, recent messages and open loops in one call.",
            request={"statement": f"SELECT b.* FROM api.person('{name}') p, LATERAL api.person_brief(p.id) b LIMIT 1"},
        ))
    if track:
        out.append(dict(
            id="where", title="Where I've been", kind="sql",
            question="Which days have the most location history?",
            explain="Location points live in api.location_track; Throughline also places messages on the map.",
            request={"statement": "SELECT (at AT TIME ZONE '" + _tz().replace("'", "") + "')::date AS day, count(*) AS points,"
                     " round(min(lat)::numeric,3) AS south, round(max(lat)::numeric,3) AS north"
                     " FROM api.location_track GROUP BY 1 ORDER BY 1 DESC LIMIT 14"},
        ))
    for row in lens_rows:
        out.append(dict(
            id=f"lens:{row['name']}", title=f"Your {row['name'].removeprefix('starter_').replace('_', ' ')} lens",
            kind="sql", question="What has this lens found so far?",
            explain=f"Every lens is a SQL view: api.lens_{row['name']}.",
            request={"statement": f"SELECT * FROM api.lens_{row['name']} ORDER BY id DESC LIMIT 20"},
        ))
    if convo:
        out.append(dict(
            id="timeline", title="Every time it came up", kind="timeline",
            question="Every time I talked about travel",
            explain="POST /v1/timeline finds every episode, in order — good for 'how often' and 'when did this start'.",
            request={"queries": ["trip", "flight", "travel", "vacation"], "max_episodes": 20, "semantic": False},
        ))
    return dict(queries=out, tz=_tz())
