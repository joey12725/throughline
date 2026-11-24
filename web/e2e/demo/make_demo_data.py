"""Invented data for the onboarding walkthrough video (web/e2e/demo).

    uv run python web/e2e/demo/make_demo_data.py OUT_DIR

Writes, for a fictional owner "Jordan Reyes" and friends:
  Timeline.json     an 18-month Google Timeline export (visits and trips)
  mailbox.json      an IMAP fixture mailbox (IMAP_FIXTURE)
  beeper.json       a Beeper Desktop API recording (BEEPER_RECORDING)
  wave.json         a Wave API recording (WAVE_API_FIXTURE)

The same invented people appear across sources under slightly different
names so the people step has certain matches and real questions. Nothing here
is real personal data; every address uses example.com/.test.
"""

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

R = random.Random(47)
START = datetime(2025, 5, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 20, tzinfo=timezone.utc)
OWNER = ("Jordan Reyes", "jordan.reyes@example.com")

# name in email, address, name on chat (None = not on chat), chat network, name in Wave
PEOPLE = [
    ("Maya Okafor", "maya.okafor@example.com", "Maya Okafor", "discord", "Maya Okafor"),
    ("Theo Lindqvist", "theo.lindqvist@example.com", "theo", "discord", None),
    ("Priya Raman", "priya.raman@example.com", "Priya", "sms", "Priya Raman"),
    ("Samuel Ortiz", "sam.ortiz@example.com", "Sam Ortiz", "instagram", None),
    ("Dev Patel", "dev@patel-studio.example.com", "Dev Patel", "discord", "Dev Patel"),
    ("Lena Hart", "lena.hart@example.com", None, None, None),
    ("Noor Haddad", "noor.haddad@example.com", "Noor Haddad", "sms", None),
]

TOPICS = [
    ("Trip to the coast", ["Are we still on for the coast trip?", "I booked the cabin for Friday.",
                           "Bring the good camera this time.", "Tide is low at 6, perfect for the pools."]),
    ("Pottery class", ["Class moved to Thursday at 7.", "My bowl cracked in the kiln again.",
                       "Want to glaze together next week?"]),
    ("Launch plan", ["Draft of the launch plan is in the doc.", "Can we push the beta by a week?",
                     "Numbers look good, signups are up 18%.", "Let's sync Monday about pricing."]),
    ("Book club", ["Halfway through the novel, no spoilers!", "Next pick: something short please.",
                   "Meeting at the cafe on Elm, 6pm."]),
    ("Running", ["10k this Saturday?", "My knee is finally better.", "New personal best on the hill loop!"]),
    ("Dinner", ["Tacos tonight?", "I can bring dessert.", "Reservation is at 7:30 under my name."]),
]

PLACES = {
    "home": (45.5231, -122.6765),
    "office": (45.5170, -122.6810),
    "cafe": (45.5262, -122.6849),
    "gym": (45.5301, -122.6612),
    "park": (45.5120, -122.6930),
    "studio": (45.5402, -122.6703),
}


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def ll(p):
    return f"{p[0]:.6f}°, {p[1]:.6f}°"


def timeline():
    segments = []
    day = START
    while day < END:
        weekday = day.weekday() < 5
        plan = [("home", 0, 8)]
        if weekday:
            plan += [("office", 9, 17), (R.choice(["gym", "cafe", "home"]), 18, 20)]
        else:
            plan += [(R.choice(["park", "cafe", "studio"]), 10, 13), ("home", 14, 24)]
        last = None
        for place, h0, h1 in plan:
            a = day + timedelta(hours=h0, minutes=R.randint(0, 20))
            b = day + timedelta(hours=h1, minutes=-R.randint(0, 20)) if h1 < 24 else day + timedelta(hours=23, minutes=59)
            if last is not None and last[0] != place:
                trip_start = last[1]
                segments.append({"startTime": iso(trip_start), "endTime": iso(a),
                                 "activity": {"start": {"latLng": ll(PLACES[last[0]])},
                                              "end": {"latLng": ll(PLACES[place])}}})
            jitter = (PLACES[place][0] + R.uniform(-0.0004, 0.0004), PLACES[place][1] + R.uniform(-0.0004, 0.0004))
            segments.append({"startTime": iso(a), "endTime": iso(b),
                             "visit": {"hierarchyLevel": 0, "topCandidate": {"placeLocation": {"latLng": ll(jitter)}}}})
            last = (place, b)
        day += timedelta(days=1)
    return {"semanticSegments": segments, "rawSignals": []}


def mailbox():
    messages, uid = [], 1
    t = START
    while t < END:
        for name, address, *_ in PEOPLE:
            if R.random() < 0.18:
                topic, lines = R.choice(TOPICS)
                when = t + timedelta(hours=R.randint(8, 20), minutes=R.randint(0, 59))
                messages.append({
                    "uid": uid, "flags": ["\\Seen"], "modseq": uid,
                    "internaldate": when.strftime("%d-%b-%Y %H:%M:%S +0000"),
                    "raw_lines": [
                        f"From: {name} <{address}>", f"To: {OWNER[0]} <{OWNER[1]}>", f"Subject: {topic}",
                        f"Date: {format_datetime(when)}", f"Message-ID: <demo-{uid}@example.com>",
                        "Content-Type: text/plain; charset=utf-8", "", R.choice(lines), "", f"— {name.split()[0]}",
                    ],
                })
                uid += 1
                if R.random() < 0.6:
                    reply = when + timedelta(minutes=R.randint(10, 300))
                    messages.append({
                        "uid": uid, "flags": ["\\Seen"], "modseq": uid,
                        "internaldate": reply.strftime("%d-%b-%Y %H:%M:%S +0000"),
                        "raw_lines": [
                            f"From: {OWNER[0]} <{OWNER[1]}>", f"To: {name} <{address}>", f"Subject: Re: {topic}",
                            f"Date: {format_datetime(reply)}", f"Message-ID: <demo-{uid}@example.com>",
                            f"In-Reply-To: <demo-{uid - 1}@example.com>",
                            "Content-Type: text/plain; charset=utf-8", "", R.choice(["Sounds great!", "Works for me.",
                                                                                     "Let me check and get back to you.", "Yes!"]),
                        ],
                    })
                    uid += 1
        t += timedelta(days=3)
    return {"capabilities": ["IMAP4REV1", "SPECIAL-USE", "CONDSTORE"], "password": "demo-app-password",
            "folders": [{"name": "INBOX", "flags": ["\\HasNoChildren"], "uidvalidity": 1700000001, "messages": messages}]}


def beeper():
    networks = sorted({p[3] for p in PEOPLE if p[3]})
    accounts = [{"accountID": f"acct-{n}", "network": n.capitalize() if n != "sms" else "Sms", "status": "connected",
                 "user": {"id": "owner"}} for n in networks]
    chats, responses = [], []
    for name, address, chat_name, network, _ in PEOPLE:
        if not chat_name:
            continue
        chat_id = f"chat-{network}-{chat_name.lower().replace(' ', '-')}"
        chats.append({"id": chat_id, "accountID": f"acct-{network}", "network": network, "title": chat_name, "type": "single"})
        sender = {"sms": "+1503555" + str(sum(map(ord, name)) * 37 % 10000).zfill(4), "discord": chat_name.lower().replace(" ", "") + "#0042",
                  "instagram": chat_name.lower().replace(" ", ".")}[network]
        items, t, n = [], END, 0
        while t > START:
            t -= timedelta(hours=R.randint(6, 90))
            topic, lines = R.choice(TOPICS)
            mine = R.random() < 0.45
            items.append({"id": f"{chat_id}-m{n}", "accountID": f"acct-{network}", "chatID": chat_id,
                          "senderID": "owner" if mine else sender, "senderName": OWNER[0] if mine else chat_name,
                          "timestamp": t.isoformat(), "sortKey": f"{n:06d}", "text": R.choice(lines),
                          "isSender": mine, "linkedMessageID": None})
            n += 1
        pages = [items[i:i + 100] for i in range(0, len(items), 100)]
        for k, page in enumerate(pages):
            params = {} if k == 0 else {"cursor": f"p{k}", "direction": "before"}
            last = k == len(pages) - 1
            responses.append({"path": f"v1/chats/{chat_id}/messages", "params": params,
                              "body": {"items": page, "hasMore": not last, "newestCursor": "head" if k == 0 else f"p{k}",
                                       "oldestCursor": "end" if last else f"p{k + 1}"}})
        responses.append({"path": f"v1/chats/{chat_id}/messages", "params": {"cursor": "head", "direction": "after"},
                          "body": {"items": [], "hasMore": False, "newestCursor": "head", "oldestCursor": "head"}})
        responses.append({"path": f"v1/chats/{chat_id}/messages", "params": {"cursor": "end", "direction": "before"},
                          "body": {"items": [], "hasMore": False, "newestCursor": "end", "oldestCursor": "end"}})
    responses.append({"path": "v1/accounts", "params": {}, "body": accounts})
    responses.append({"path": "v1/chats", "params": {"limit": 200}, "body": {"items": chats, "hasMore": False}})
    return {"responses": responses}


def wave():
    people = [p[4] for p in PEOPLE if p[4]]
    sessions, out = [], {}
    t = START + timedelta(days=5)
    i = 0
    while t < END:
        i += 1
        sid = f"demo-session-{i}"
        guest = R.choice(people)
        topic, lines = R.choice(TOPICS)
        sessions.append({"id": sid})
        segments, s = [], 0.0
        for turn in range(R.randint(8, 18)):
            speaker = OWNER[0] if turn % 2 == 0 else guest
            text = R.choice(lines)
            segments.append({"speaker": speaker, "start": s, "end": s + 6, "text": text})
            s += 7 + R.random() * 3
        out[f"sessions/{sid}"] = {"id": sid, "title": f"{topic} with {guest.split()[0]}", "timestamp": t.isoformat(),
                                  "duration_seconds": int(s), "type": "meeting", "platform": "desktop",
                                  "summary": f"Talked about {topic.lower()}.", "audio_deleted_at": None}
        out[f"sessions/{sid}/transcript"] = {"id": sid, "transcript": "\n".join(f"{x['speaker']}: {x['text']}" for x in segments),
                                             "segments": segments}
        t += timedelta(days=R.randint(4, 12))
    sessions.reverse()
    pages = [sessions[i:i + 25] for i in range(0, len(sessions), 25)]
    for k, page in enumerate(pages):
        key = "sessions" if k == 0 else f"sessions?cursor=c{k}"
        out[key] = {"sessions": page, "has_more": k < len(pages) - 1, "next_cursor": f"c{k + 1}" if k < len(pages) - 1 else None}
    return out


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, data in (("Timeline.json", timeline()), ("mailbox.json", mailbox()), ("beeper.json", beeper()), ("wave.json", wave())):
        (out / name).write_text(json.dumps(data))
        print(name, (out / name).stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo-data")
