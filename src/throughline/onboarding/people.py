"""Who's who: the onboarding people resolver.

Ingest records every author as an *identifier* (system, kind, value) but never
decides who is who. After the first imports this resolver pieces people
together across sources, the way a careful assistant would:

1. **You.** It proposes which identifiers are the owner (the busiest sender
   in each source, names that match the account or a plugin's owner
   setting); the owner confirms with one click.
2. **Certain matches** are applied without asking: the same e-mail address
   or phone number in two systems, an identifier whose name matches exactly
   one existing person, the same full name with a shared conversation.
   Each becomes a person (or joins one) asserted by the resolver, so it can be
   split later like any merge.
3. **Ambiguous matches** become short natural questions ("Is Birdie on
   Discord the same person as Robin Vale from email?") with the evidence
   that suggested them. Answers are owner assertions; "different" records a
   not-same pair so nothing proposes the merge again.

Everything is reversible through the identity events the core already
keeps.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter, defaultdict

from psycopg.types.json import Jsonb

from .. import identity
from ..db import connect
from . import jobs
from .errors import SetupError

AUTO = 0.9
ASK = 0.55
SPEAKER_KINDS = {"speaker_label", "speaker"}
GENERIC = re.compile(r"^(unknown|speaker|voice|participant|guest|me|you|user|owner)\b|^(speaker|voice) [a-z0-9]+$")
MAX_QUESTIONS = 60
ACTOR = "agent"

SYSTEM_LABELS = {
    "email": "email", "imap": "email", "gmail": "Gmail", "discord": "Discord", "instagram": "Instagram",
    "sms": "SMS", "imessage": "iMessage", "signal": "Signal", "telegram": "Telegram", "whatsapp": "WhatsApp",
    "beeper": "Beeper", "slack": "Slack", "wave": "Wave", "granola": "Granola", "zoom": "Zoom",
    "gbrain": "GBrain", "github": "GitHub", "bluesky": "Bluesky", "mastodon": "Mastodon", "phone": "phone",
}


def _conn(token_id=None):
    conn_cm = connect("app")
    return _WithToken(conn_cm, token_id)


class _WithToken:
    def __init__(self, cm, token_id):
        self.cm, self.token_id = cm, token_id

    def __enter__(self):
        self.conn = self.cm.__enter__()
        if self.token_id:
            self.conn.execute("SELECT set_config('throughline.token_id',%s,true)", (str(self.token_id),))
        return self.conn

    def __exit__(self, *exc):
        return self.cm.__exit__(*exc)


def norm_name(name):
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s'-]", " ", text.lower())
    text = re.sub(r"[_\d]+", " ", text)
    return " ".join(text.split())


def _phone_key(value):
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 7 else None


def system_label(system, kind=None):
    label = SYSTEM_LABELS.get((system or "").lower())
    if label:
        return label
    if kind in ("email", "phone"):
        return kind
    return (system or kind or "a source").replace("_", " ").title()


# ───────────── loading ─────────────


def load(conn):
    """Identifiers with names, counts, activity and shared threads."""
    idents = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT i.id,i.person_id,i.system,i.kind,i.value,i.value_norm,i.status,i.label,i.asserted_by,"
            " p.display_name AS person_name, p.is_owner"
            " FROM tl.identifiers i LEFT JOIN tl.people p ON p.id=i.person_id WHERE i.status<>'ignored'"
        ).fetchall()
    }
    stats = conn.execute(
        "SELECT author_identifier_id AS id,count(*) AS n,max(happened_at) AS last_at,"
        " mode() WITHIN GROUP (ORDER BY author_handle) AS name"
        " FROM tl.messages WHERE author_identifier_id IS NOT NULL AND superseded_by IS NULL GROUP BY 1"
    ).fetchall()
    for row in stats:
        ident = idents.get(row["id"])
        if ident:
            ident.update(messages=row["n"], last_at=row["last_at"], seen_name=row["name"])
    threads = defaultdict(set)
    for row in conn.execute(
        "SELECT DISTINCT author_identifier_id AS id, thread_id FROM tl.messages"
        " WHERE author_identifier_id IS NOT NULL AND thread_id IS NOT NULL AND superseded_by IS NULL"
    ).fetchall():
        threads[row["id"]].add(row["thread_id"])
    for ident in idents.values():
        ident.setdefault("messages", 0)
        ident.setdefault("last_at", None)
        ident.setdefault("seen_name", None)
        ident["name"] = ident["person_name"] or ident["label"] or ident["seen_name"] or _name_from_value(ident)
        ident["key"] = norm_name(ident["name"])
        ident["threads"] = threads.get(ident["id"], set())
    people = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT id,handle,display_name,is_owner,asserted_by FROM tl.people WHERE merged_into IS NULL"
        ).fetchall()
    }
    for row in conn.execute("SELECT person_id,name FROM tl.person_names").fetchall():
        if row["person_id"] in people:
            people[row["person_id"]].setdefault("names", set()).add(norm_name(row["name"]))
    for person in people.values():
        person.setdefault("names", set()).add(norm_name(person["display_name"] or person["handle"]))
    return idents, people


def _name_from_value(ident):
    value = ident["value"] or ""
    if ident["kind"] == "email":
        local = value.split("@")[0]
        parts = re.split(r"[._+-]+", local)
        if len(parts) >= 2 and all(p.isalpha() for p in parts[:2]):
            return " ".join(p.capitalize() for p in parts[:2])
        return local
    return value


# ───────────── owner ─────────────


def me_candidates(conn, idents, people):
    owner = next((p for p in people.values() if p["is_owner"]), None)
    hints = set()
    for row in conn.execute("SELECT display_name,handle FROM tl.accounts").fetchall():
        hints.add(norm_name(row["display_name"]))
        hints.add(norm_name(row["handle"]))
    from . import store

    for plugin_values in _all_plugin_values(store):
        for key, value in plugin_values.items():
            if key.endswith(("OWNER_NAME", "OWNER_IDENTIFIER", "OWNER_EMAIL", "OWNER_HANDLE", "OWNER_NUMBER")) and value:
                hints.add(norm_name(value))
                hints.add(value.strip().lower())
    hints.discard("")
    by_system = defaultdict(list)
    for ident in idents.values():
        if ident["person_id"] and owner and ident["person_id"] == owner["id"]:
            continue
        if ident["kind"] in SPEAKER_KINDS or ident["kind"] == "voice_profile":
            continue  # voices are assigned through transcripts, not claimed
        by_system[ident["system"]].append(ident)
    out = []
    for system, items in by_system.items():
        items.sort(key=lambda i: -i["messages"])
        total = sum(i["messages"] for i in items) or 1
        for ident in items[:3]:
            reason = None
            if ident["key"] in hints or (ident["value_norm"] or "").lower() in hints:
                reason = "Matches your account or a source's owner setting"
            elif ident["messages"] / total > 0.35 and ident["messages"] >= 20:
                reason = f"Wrote {ident['messages'] / total:.0%} of the {system_label(system, ident['kind'])} messages"
            if reason:
                out.append((ident, reason))
    out.sort(key=lambda pair: (pair[1].startswith("Matches") is False, -pair[0]["messages"]))
    return owner, out[:8]


def _all_plugin_values(store):
    try:
        data = store._read("settings.json")["plugins"]
    except Exception:
        return []
    return list(data.values())


# ───────────── matching ─────────────


class _Union:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def candidate_pairs(idents, people, not_same):
    """[(confidence, id_a, id_b, reasons)] over identifier ids."""
    pairs = {}

    def add(a, b, confidence, reason):
        if a == b:
            return
        ia, ib = idents[a], idents[b]
        if ia["person_id"] and ia["person_id"] == ib["person_id"]:
            return
        if ia["person_id"] and ib["person_id"] and tuple(sorted((ia["person_id"], ib["person_id"]))) in not_same:
            return
        key = tuple(sorted((a, b)))
        old = pairs.get(key)
        if old is None:
            pairs[key] = [confidence, [reason]]
        else:
            old[0] = max(old[0], confidence) + (0.03 if reason not in old[1] else 0)
            if reason not in old[1]:
                old[1].append(reason)

    by_email, by_phone, by_name = defaultdict(list), defaultdict(list), defaultdict(list)
    for ident in idents.values():
        value = (ident["value_norm"] or "").lower()
        if ident["kind"] == "email" or re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", value):
            by_email[value].append(ident["id"])
        elif ident["kind"] in ("phone", "number") or re.fullmatch(r"\+?[\d\s().-]{7,}", ident["value"] or ""):
            key = _phone_key(ident["value"])
            if key:
                by_phone[key].append(ident["id"])
        if ident["key"]:
            by_name[ident["key"]].append(ident["id"])
    for value, ids in by_email.items():
        for a in ids:
            for b in ids:
                if a < b:
                    add(a, b, 0.99, f"Both use the address {value}")
    for value, ids in by_phone.items():
        for a in ids:
            for b in ids:
                if a < b:
                    add(a, b, 0.98, "Both use the same phone number")
    # Per-recording speaker labels ("rec-17:Maya Okafor") with the same name
    # in one system are one voice.
    voice = {}
    for ident in idents.values():
        if ident["kind"] in SPEAKER_KINDS and ident["key"] and not GENERIC.match(ident["key"]):
            voice.setdefault((ident["system"], ident["key"]), []).append(ident["id"])
    for (system, key), ids in voice.items():
        for b in ids[1:]:
            add(ids[0], b, 0.95, f"The same speaker name ({idents[ids[0]]['name']}) in several {system_label(system)} recordings")

    def entity(i):
        ident = idents[i]
        if ident["kind"] in SPEAKER_KINDS:
            return (ident["system"], "voice", ident["key"])
        return i

    for key, ids in by_name.items():
        if GENERIC.match(key):
            continue
        words = key.split()
        systems = defaultdict(set)
        for i in ids:
            systems[idents[i]["system"]].add(entity(i))
        for a in ids:
            for b in ids:
                if a >= b or idents[a]["system"] == idents[b]["system"]:
                    continue
                unique = len(systems[idents[a]["system"]]) == 1 and len(systems[idents[b]["system"]]) == 1
                shared = idents[a]["threads"] & idents[b]["threads"]
                active = idents[a]["messages"] >= 2 and idents[b]["messages"] >= 2
                name = idents[a]["name"]
                if len(words) >= 2 and unique and shared:
                    add(a, b, 0.95, f"Both are called {name} and appear in the same conversation")
                elif len(words) >= 2 and unique and active:
                    add(a, b, 0.91, f"Both are called {name}, and nobody else in either source is")
                elif len(words) >= 2 and unique:
                    add(a, b, 0.82, f"Both are called {name}")
                elif len(words) >= 2:
                    add(a, b, 0.62, f"Both are called {name}, but so are others")
                elif unique and shared:
                    add(a, b, 0.7, f"Both go by {name} and appear in the same conversation")
                elif unique:
                    add(a, b, 0.58, f"Both go by {name}")
    # Near names: "Robin Vale" vs "Robin Vale 🌸", nickname-ish handles.
    keys = [k for k in by_name if len(k) >= 4]
    first_index = defaultdict(list)
    for key in keys:
        first_index[key.split()[0]].append(key)
    for group in first_index.values():
        if len(group) > 40:
            continue
        for x in group:
            for y in group:
                if x >= y:
                    continue
                ratio = difflib.SequenceMatcher(None, x, y).ratio()
                xs, ys = set(x.split()), set(y.split())
                contained = xs < ys or ys < xs
                if ratio < 0.84 and not contained:
                    continue
                for a in by_name[x]:
                    for b in by_name[y]:
                        if idents[a]["system"] == idents[b]["system"]:
                            continue
                        shared = idents[a]["threads"] & idents[b]["threads"]
                        base = 0.66 if contained else 0.6
                        add(a, b, base + (0.1 if shared else 0),
                            f"{idents[a]['name']} and {idents[b]['name']} are nearly the same name")
    # Email local part that spells a handle elsewhere (robin.vale ~ robinvale).
    squashed = defaultdict(list)
    for ident in idents.values():
        value = (ident["value_norm"] or "").lower()
        local = value.split("@")[0] if "@" in value else value
        token = re.sub(r"[^a-z0-9]", "", local)
        if len(token) >= 6:
            squashed[token].append(ident["id"])
    for token, ids in squashed.items():
        for a in ids:
            for b in ids:
                if a < b and idents[a]["system"] != idents[b]["system"]:
                    add(a, b, 0.72, f"Their usernames match ({token})")
    return [(min(c, 0.995), a, b, reasons) for (a, b), (c, reasons) in pairs.items()]


def _person_of(ident):
    return ident["person_id"]


# ───────────── applying ─────────────


def _slug(name, conn):
    base = re.sub(r"[^a-z0-9]+", "-", norm_name(name)).strip("-")[:40] or "person"
    handle, n = base, 1
    while conn.execute("SELECT 1 FROM tl.people WHERE handle=%s", (handle,)).fetchone():
        n += 1
        handle = f"{base}-{n}"
    return handle


def _create_person(conn, name, actor=ACTOR):
    row = conn.execute(
        "INSERT INTO tl.people(handle,display_name,asserted_by) VALUES (%s,%s,%s) RETURNING id",
        (_slug(name, conn), name, actor),
    ).fetchone()
    identity.event(conn, "create", actor, dict(person_id=row["id"], display_name=name, by="setup"))
    return row["id"]


def _best_name(items):
    def score(ident):
        name = ident["name"] or ""
        words = len(norm_name(name).split())
        return (ident["person_name"] is not None, words >= 2, "@" not in name, ident["messages"], len(name))

    return max(items, key=score)["name"]


def _join(conn, idents, ids, actor):
    """Make every identifier in ids belong to one person; returns person id."""
    persons = sorted({idents[i]["person_id"] for i in ids if idents[i]["person_id"]})
    owner = conn.execute(
        "SELECT id FROM tl.people WHERE is_owner AND id=ANY(%s)", (persons,)
    ).fetchone() if persons else None
    if owner:
        # Everyone else joins the owner, never the other way round.
        persons.remove(owner["id"])
        persons.insert(0, owner["id"])
    if owner:
        # Joining the owner is an owner assertion: the setup run acts for the
        # owner, who started it, and only on certain matches.
        actor = "owner"
    if persons:
        target = persons[0]
        for other in persons[1:]:
            identity.merge(conn, other, target, actor)
    else:
        target = _create_person(conn, _best_name([idents[i] for i in ids]), actor)
    for i in ids:
        if idents[i]["person_id"] != target and not idents[i]["person_id"]:
            identity.attach(conn, i, target, actor)
        idents[i]["person_id"] = target
    for i, ident in idents.items():
        if ident["person_id"] in persons:
            ident["person_id"] = target
    return target


def resolve(job_id=None, token_id=None):
    tracker = jobs.Tracker(job_id) if job_id else None

    def phase(text, done=None, total=None):
        if tracker:
            tracker.update(phase=text, done=done, total=total, unit="steps")

    phase("Reading everyone who appears in your data", 0, 4)
    with _conn(token_id) as conn:
        idents, people = load(conn)
        not_same = {
            tuple(sorted((r["person_a"], r["person_b"])))
            for r in conn.execute("SELECT person_a,person_b FROM tl.not_same").fetchall()
        }
        answered = {
            (r["ident_a"], r["ident_b"])
            for r in conn.execute("SELECT ident_a,ident_b FROM tl.merge_questions WHERE status<>'open'").fetchall()
        }
    owner = next((p for p in people.values() if p["is_owner"]), None)
    # The owner's own identifiers never get folded into someone else.
    phase("Comparing names, addresses and conversations", 1, 4)
    pairs = candidate_pairs(idents, people, not_same)
    pairs = [p for p in pairs if (min(p[1], p[2]), max(p[1], p[2])) not in answered]
    if owner:
        pairs = [
            p for p in pairs
            if not (idents[p[1]]["person_id"] == owner["id"]) ^ (idents[p[2]]["person_id"] == owner["id"])
            or p[0] >= AUTO
        ]
    # Speaker labels never become people (the database refuses it): they
    # are assigned to the person they match through owner speaker
    # annotations instead, after the people exist.
    speakers = {i for i, ident in idents.items() if ident["kind"] in SPEAKER_KINDS}
    voice_pairs = [p for p in pairs if p[1] in speakers or p[2] in speakers]
    pairs = [p for p in pairs if p[1] not in speakers and p[2] not in speakers]
    union = _Union()
    auto = [p for p in pairs if p[0] >= AUTO]
    for confidence, a, b, reasons in auto:
        union.join(a, b)
    groups = defaultdict(list)
    for ident_id in idents:
        if ident_id not in speakers:
            groups[union.find(ident_id)].append(ident_id)
    phase("Linking certain matches", 2, 4)
    created = merged = 0
    auto_log = []
    with _conn(token_id) as conn:
        # Existing people with a matching name claim lone identifiers.
        name_owner = defaultdict(set)
        for person in people.values():
            for name in person["names"]:
                if len(name.split()) >= 2:
                    name_owner[name].add(person["id"])
        for root, ids in groups.items():
            if len(ids) == 1:
                ident = idents[ids[0]]
                if ident["person_id"] or not ident["messages"]:
                    continue
                claim = name_owner.get(ident["key"]) or set()
                if len(claim) == 1:
                    target = next(iter(claim))
                    identity.attach(conn, ident["id"], target, ACTOR)
                    ident["person_id"] = target
                    merged += 1
                    auto_log.append(dict(name=people[target]["display_name"] or ident["name"],
                                         merged=[_describe(ident)], reason="Same name as an existing person", confidence=0.92))
                else:
                    _join(conn, idents, ids, ACTOR)
                    created += 1
                continue
            before = {idents[i]["person_id"] for i in ids if idents[i]["person_id"]}
            reasons = sorted({r for c, a, b, rs in auto if union.find(a) == root for r in rs})
            target = _join(conn, idents, ids, ACTOR)
            created += 0 if before else 1
            merged += len(ids) - 1
            auto_log.append(dict(
                name=_best_name([idents[i] for i in ids]),
                merged=[_describe(idents[i]) for i in ids],
                reason="; ".join(reasons[:2]) or "Certain match",
                confidence=max((c for c, a, b, _ in auto if union.find(a) == root), default=AUTO),
            ))
            del target
        assigned = _assign_voices(conn, token_id, idents, speakers, voice_pairs, auto_log)
        merged += assigned
        phase("Writing questions for the unclear ones", 3, 4)
        asked = 0
        questions = sorted((p for p in pairs if ASK <= p[0] < AUTO), key=lambda p: -p[0])
        seen_people = set()
        for confidence, a, b, reasons in questions:
            pa, pb = idents[a]["person_id"], idents[b]["person_id"]
            if pa and pa == pb:
                continue
            key = tuple(sorted((pa or -a, pb or -b)))
            if key in seen_people:
                continue
            seen_people.add(key)
            if asked >= MAX_QUESTIONS:
                break
            prompt = _prompt(idents[a], idents[b])
            conn.execute(
                "INSERT INTO tl.merge_questions(ident_a,ident_b,person_a,person_b,confidence,reasons,prompt,suggested_name)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (ident_a,ident_b) DO UPDATE SET"
                " confidence=EXCLUDED.confidence,reasons=EXCLUDED.reasons,prompt=EXCLUDED.prompt,"
                " person_a=EXCLUDED.person_a,person_b=EXCLUDED.person_b WHERE tl.merge_questions.status='open'",
                (min(a, b), max(a, b), pa, pb, round(confidence, 3), Jsonb(reasons), prompt,
                 _best_name([idents[a], idents[b]])),
            )
            asked += 1
        conn.execute(
            "INSERT INTO tl.setup_kv(key,value) VALUES ('people_auto',%s)"
            " ON CONFLICT (key) DO UPDATE SET value=tl.setup_kv.value || EXCLUDED.value",
            (Jsonb(auto_log[:500]),),
        )
    phase(f"Done: {merged} links made, {asked} to confirm", 4, 4)
    return dict(created=created, merged=merged, questions=asked)


def _assign_voices(conn, token_id, idents, speakers, voice_pairs, auto_log):
    """Assign recording speaker labels that certainly match a person."""
    from .. import transcripts

    if not speakers or not token_id:
        return 0
    token = conn.execute("SELECT * FROM tl.tokens WHERE id=%s", (token_id,)).fetchone()
    if not token or token["kind"] != "owner":
        return 0
    token = dict(token, scope=None)
    voices = _Union()
    best = {}
    for confidence, a, b, reasons in voice_pairs:
        if a in speakers and b in speakers:
            voices.join(a, b)
    for confidence, a, b, reasons in voice_pairs:
        label, other = (a, b) if a in speakers else (b, a)
        if other in speakers or confidence < AUTO:
            continue
        person = idents[other]["person_id"]
        if not person:
            continue
        root = voices.find(label)
        if root not in best or confidence > best[root][0]:
            best[root] = (confidence, person, reasons)
    done = 0
    for label in speakers:
        hit = best.get(voices.find(label))
        if not hit:
            continue
        confidence, person, reasons = hit
        ident = idents[label]
        name = ident["name"]
        for thread_id in ident["threads"]:
            try:
                with conn.transaction():
                    transcripts.assign_speaker(
                        conn, token, thread_id, ident["value"], person,
                        f"setup: the speaker {name} matches {reasons[0] if reasons else 'a known person'}",
                    )
                done += 1
            except Exception:
                continue
    if done:
        auto_log.append(dict(name="Voices in recordings", merged=[f"{done} speaker label(s) assigned to people"],
                             reason="Speaker names that match exactly one person", confidence=AUTO))
    return done


def _describe(ident):
    return f"{ident['name']} ({system_label(ident['system'], ident['kind'])}: {ident['value']})" if ident["name"] != ident["value"] else f"{ident['value']} ({system_label(ident['system'], ident['kind'])})"


def _prompt(a, b):
    def who(i):
        place = system_label(i["system"], i["kind"])
        if i["name"] and i["name"] != i["value"]:
            return f"{i['name']} ({i['value']} on {place})"
        return f"{i['value']} on {place}"

    return f"Is {who(a)} the same person as {who(b)}?"


def start(token_id=None):
    job_id = jobs.create("people", "Working out who's who")
    return jobs.submit(job_id, lambda jid: resolve(jid, token_id))


# ───────────── view & answers ─────────────


def _card(conn, person_id, ident=None):
    if person_id:
        p = conn.execute("SELECT id,display_name,handle FROM tl.people WHERE id=%s", (person_id,)).fetchone()
        handles = conn.execute(
            "SELECT system,kind,value FROM tl.identifiers WHERE person_id=%s ORDER BY id LIMIT 8", (person_id,)
        ).fetchall()
        stat = conn.execute(
            "SELECT count(*) AS n,max(happened_at) AS last_at FROM tl.messages WHERE person_id=%s AND superseded_by IS NULL",
            (person_id,),
        ).fetchone()
        return dict(id=p["id"], name=p["display_name"] or p["handle"], handles=[dict(h) for h in handles],
                    messages=stat["n"], last_at=stat["last_at"])
    stat = conn.execute(
        "SELECT count(*) AS n,max(happened_at) AS last_at,mode() WITHIN GROUP (ORDER BY author_handle) AS name"
        " FROM tl.messages WHERE author_identifier_id=%s AND superseded_by IS NULL",
        (ident["id"],),
    ).fetchone()
    return dict(id=-ident["id"], name=stat["name"] or ident["value"],
                handles=[dict(system=ident["system"], kind=ident["kind"], value=ident["value"])],
                messages=stat["n"], last_at=stat["last_at"])


def view(token_id=None):
    with _conn(token_id) as conn:
        idents, people = load(conn)
        owner, cands = me_candidates(conn, idents, people)
        me = _card(conn, owner["id"]) if owner else None
        rows = conn.execute(
            "SELECT q.*, a.system AS a_system,a.kind AS a_kind,a.value AS a_value,a.person_id AS a_person,"
            " b.system AS b_system,b.kind AS b_kind,b.value AS b_value,b.person_id AS b_person"
            " FROM tl.merge_questions q JOIN tl.identifiers a ON a.id=q.ident_a JOIN tl.identifiers b ON b.id=q.ident_b"
            " ORDER BY (q.status='open') DESC, q.confidence DESC, q.id LIMIT 200"
        ).fetchall()
        questions = []
        for r in rows:
            if r["status"] == "open" and r["a_person"] and r["a_person"] == r["b_person"]:
                continue
            a = _card(conn, r["a_person"], dict(id=r["ident_a"], system=r["a_system"], kind=r["a_kind"], value=r["a_value"]))
            b = _card(conn, r["b_person"], dict(id=r["ident_b"], system=r["b_system"], kind=r["b_kind"], value=r["b_value"]))
            questions.append(dict(id=r["id"], prompt=r["prompt"], confidence=r["confidence"], reasons=r["reasons"],
                                  a=a, b=b, suggested_name=r["suggested_name"], status=r["status"]))
        kv = conn.execute("SELECT value FROM tl.setup_kv WHERE key='people_auto'").fetchone()
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM tl.people WHERE merged_into IS NULL) AS people,"
            "(SELECT count(*) FROM tl.identifiers) AS identifiers,"
            "(SELECT count(*) FROM tl.identifiers WHERE person_id IS NULL AND status<>'ignored') AS unclaimed,"
            "(SELECT count(*) FROM tl.merge_questions WHERE status='open') AS open,"
            "(SELECT count(*) FROM tl.merge_questions WHERE status IN ('same','different','skipped')) AS answered"
        ).fetchone()
        running = conn.execute(
            "SELECT * FROM tl.setup_jobs WHERE kind='people' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    auto = (kv or {}).get("value") or []
    return dict(
        me=me,
        me_candidates=_group_candidates(cands),
        stats=dict(people=counts["people"], identifiers=counts["identifiers"], unclaimed=counts["unclaimed"],
                   auto_merged=sum(max(len(a.get("merged") or []) - 1, 1) for a in auto),
                   created=counts["people"], questions_open=counts["open"], answered=counts["answered"]),
        auto=auto[-200:],
        questions=[q for q in questions if q["status"] == "open"],
        running=jobs.public(running) if running and running["status"] in ("queued", "running") else None,
        llm_assist=False,
    )


def _group_candidates(cands):
    """One card per name: the owner's accounts in several sources are one
    choice. The card's id is its first identifier (negative); its
    identifier_ids claim them all."""
    groups, order = {}, []
    for ident, reason in cands:
        key = ident["key"] if len(ident["key"].split()) >= 2 else f"id:{ident['id']}"
        if key not in groups:
            groups[key] = dict(_ident_card(ident), reason=reason, identifier_ids=[ident["id"]], systems=[])
            order.append(key)
        else:
            card = groups[key]
            card["handles"].extend(_ident_card(ident)["handles"])
            card["messages"] += ident["messages"]
            card["identifier_ids"].append(ident["id"])
            card["last_at"] = max(filter(None, [card["last_at"], ident["last_at"]]), default=None)
        groups[key]["systems"].append(system_label(ident["system"], ident["kind"]))
    out = []
    for key in order:
        card = groups[key]
        if len(card["systems"]) > 1:
            card["reason"] = "The busiest sender in " + ", ".join(dict.fromkeys(card["systems"]))
        card.pop("systems")
        out.append(card)
    return out


def _ident_card(ident):
    return dict(id=-ident["id"], name=ident["name"],
                handles=[dict(system=ident["system"], kind=ident["kind"], value=ident["value"])],
                messages=ident["messages"], last_at=ident["last_at"])


def set_me(token_id, person_id=None, identifier_ids=None, display_name=None):
    ids = [abs(i) for i in (identifier_ids or [])]
    with _conn(token_id) as conn:
        owner = conn.execute("SELECT id FROM tl.people WHERE is_owner AND merged_into IS NULL").fetchone()
        if person_id and person_id > 0:
            if owner and owner["id"] != person_id:
                identity.merge(conn, person_id, owner["id"], "owner")
                target = owner["id"]
            else:
                conn.execute("UPDATE tl.people SET is_owner=true,asserted_by='owner' WHERE id=%s", (person_id,))
                target = person_id
        elif person_id and person_id < 0:
            ids.append(-person_id)
            # The candidate card for this identifier may stand for the same
            # name in several sources: claim them all.
            idents, people = load(conn)
            picked = idents.get(-person_id)
            if picked and len(picked["key"].split()) >= 2:
                _, cands = me_candidates(conn, idents, people)
                ids.extend(i["id"] for i, _ in cands if i["key"] == picked["key"])
            target = owner["id"] if owner else None
        else:
            target = owner["id"] if owner else None
        if target is None:
            named = conn.execute(
                "SELECT label FROM tl.identifiers WHERE id=ANY(%s) AND label IS NOT NULL", (ids,)
            ).fetchall()
            counted = Counter(r["label"] for r in named if r["label"] and len(r["label"].split()) >= 2)
            name = (display_name
                    or (conn.execute("SELECT display_name FROM tl.accounts ORDER BY id LIMIT 1").fetchone() or {}).get("display_name")
                    or (counted.most_common(1)[0][0] if counted else None)
                    or "Me")
            target = conn.execute(
                "INSERT INTO tl.people(handle,display_name,is_owner,asserted_by) VALUES (%s,%s,true,'owner') RETURNING id",
                (_slug(name, conn), name),
            ).fetchone()["id"]
            identity.event(conn, "create", "owner", dict(person_id=target, is_owner=True, by="setup"))
        if display_name:
            conn.execute("UPDATE tl.people SET display_name=%s WHERE id=%s", (display_name, target))
        for ident_id in ids:
            row = conn.execute("SELECT person_id,kind FROM tl.identifiers WHERE id=%s", (ident_id,)).fetchone()
            if not row or row["kind"] in SPEAKER_KINDS:
                continue
            if row["person_id"] and row["person_id"] != target:
                identity.merge(conn, row["person_id"], target, "owner", override_not_same=True)
            elif not row["person_id"]:
                identity.attach(conn, ident_id, target, "owner")
    return view(token_id)


def answer(token_id, question_id, decision, name=None):
    if decision not in ("same", "different", "skip"):
        raise SetupError("Unknown answer", "Answer same, different or skip.", status=422)
    with _conn(token_id) as conn:
        q = conn.execute("SELECT * FROM tl.merge_questions WHERE id=%s FOR UPDATE", (question_id,)).fetchone()
        if not q:
            raise SetupError("Question not found", "It may have been answered already.", status=404)
        a = conn.execute("SELECT * FROM tl.identifiers WHERE id=%s", (q["ident_a"],)).fetchone()
        b = conn.execute("SELECT * FROM tl.identifiers WHERE id=%s", (q["ident_b"],)).fetchone()
        if decision == "same":
            pa, pb = a["person_id"], b["person_id"]
            if pa and pb and pa != pb:
                identity.merge(conn, pb, pa, "owner", override_not_same=True)
                target = pa
            elif pa or pb:
                target = pa or pb
                other = b if pa else a
                if not other["person_id"]:
                    identity.attach(conn, other["id"], target, "owner")
            else:
                target = _create_person(conn, name or q["suggested_name"] or a["value"], "owner")
                identity.attach(conn, a["id"], target, "owner")
                identity.attach(conn, b["id"], target, "owner")
            if name:
                conn.execute("UPDATE tl.people SET display_name=%s WHERE id=%s", (name, target))
        elif decision == "different":
            pa = a["person_id"] or _create_person(conn, q["suggested_name"] or a["value"], "owner")
            if not a["person_id"]:
                identity.attach(conn, a["id"], pa, "owner")
            pb = b["person_id"] or _create_person(conn, b["value"], "owner")
            if not b["person_id"]:
                identity.attach(conn, b["id"], pb, "owner")
            if pa != pb:
                lo, hi = sorted((pa, pb))
                conn.execute(
                    "INSERT INTO tl.not_same(person_a,person_b,reason) VALUES (%s,%s,'owner answered in setup') ON CONFLICT DO NOTHING",
                    (lo, hi),
                )
        conn.execute(
            "UPDATE tl.merge_questions SET status=%s,answered_at=now() WHERE id=%s",
            ({"skip": "skipped"}.get(decision, decision), question_id),
        )
    return view(token_id)
