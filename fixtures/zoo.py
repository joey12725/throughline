"""Seeded "shape zoo": a rich synthetic corpus for building the web UI.

Every person, place name, message and number here is invented. The channel
schemas in ``zoo_channels.json`` mirror the field schemas of a real
installation (names, types and descriptions only), so lenses are developed
against realistic shapes without any personal data.

    uv run python -m fixtures.zoo --seed 47 [--scale 1.0]

reads THROUGHLINE_URL and THROUGHLINE_TOKEN (an owner token: the zoo also
writes people, claims and annotations). The corpus is a pure function of
(seed, scale) around the fixed ``ANCHOR`` instant, and every write is
replay-safe, so a second run changes nothing.
"""

import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from throughline_sdk import Channel, Client, Manifest, coverage, provenance

# "Now" of the zoo. Fixed, so the corpus is deterministic; some calendar
# events and research items lie after it on purpose.
ANCHOR = datetime(2026, 11, 20, 12, tzinfo=UTC)
BATCH = 2500
OWNER = "zoo-owner"

SCHEMAS = json.loads((Path(__file__).parent / "zoo_channels.json").read_text())
FIELD_TYPES = {c["name"]: {f["name"]: f["type"] for f in c["fields"]} for c in SCHEMAS}
PLUGIN_OF = {c["name"]: c["plugin"] for c in SCHEMAS}
PLUGINS = sorted(set(PLUGIN_OF.values()))
LINK_TYPES = {
    "beeper": ["reply_to"],
    "calendar": ["at_place", "participant"],
    "email": ["published_by"],
    "gbrain": ["about", "mentions", "related_to", "part_of", "cites"],
    "health": [],
    "morning_context": ["at_place"],
    "research": ["cites", "about", "derived_from"],
    "t3code": ["part_of"],
    "wave": ["transcript_of", "at_place"],
    "youtube": [],
    "youtube_feed": ["pick_of", "published_by"],
}
INTERVALS = {
    "beeper": 900,
    "calendar": 3600,
    "email": 900,
    "gbrain": 86400,
    "health": 3600,
    "morning_context": 86400,
    "research": 21600,
    "t3code": 900,
    "wave": 3600,
    "youtube": 86400,
    "youtube_feed": 3600,
}

FIRST = (
    "Zibble Floop Quibble Vexel Nimble Wobble Pindle Flizzle Dorb Tindle Yabble "
    "Zorple Bramble Squill Mopsy Fennick Glim Hobble Jinx Kettle Lumo Nettle "
    "Oswin Pippet Quark Rumple Sorrel Tansy Umber Wisp Brindle Clover Dabble "
    "Ember Fizz Gribble Huckle Ivo Juniper Knoll"
).split()
LAST = (
    "Mosskite Lantern Fernwheel Cloudspoon Starbucket Thistle Mooncup Pebble "
    "Sparkleaf Snowgear Twigboat Mistbell Quillfeather Dewlap Puddlejump "
    "Hazelwick Bumblecroft Candlefern Marshwhistle Gooseberry Tinsparrow "
    "Velvetcompass Acornledger Noodleatlas"
).split()
THINGS = (
    "moss lantern|kite spool|glass seeds|moon jam|fern wheel|cloud spoon|"
    "pebble radio|thistle soup|snow gear|twig boat|mist bell|star bucket|"
    "paper comet|button orchard|marble kettle|velvet compass|acorn ledger|"
    "tin sparrow|puddle map|hazel clock|lint telescope|jelly anchor|"
    "copper mitten|noodle atlas|feather abacus|pocket glacier|plum barometer|"
    "ribbon engine|soup violin|gravel piano|origami heron|turnip rocket"
).split("|")
SPOTS = (
    "the Kite Orchard|Café Moonjam|the Pebble Market|Lanternworks|the Fern Pier|"
    "Thistle Hall|the Cloud Stairs|Mooncup Lane|the Tin Sparrow|Starbucket Yard"
).split("|")
DAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()
SHORT = (
    "lol|haha yes|ok!|on my way|sounds good|no way|hmm|brb|same|exactly|ugh|"
    "nice|omg|wait what|deal|thanks!|np|good night|morning!|true|ha|yep|nope|"
    "be there in ten|one sec|so good|classic|say more?|who told you?"
).split("|")
STATEMENTS = [
    "The {a} finally arrived and it is even weirder than the photos.",
    "Just got back from {p}, the {a} stall was closed again.",
    "I tried the {a} trick with a {b} and it sort of worked.",
    "Honestly the {a} is the best thing I bought all year.",
    "Spent the whole evening untangling the {a}, zero regrets.",
    "They moved the {a} meetup to {p} this week.",
    "My {a} broke again so I am borrowing a {b} for now.",
    "Saw someone carrying a {a} through {p}, thought of you.",
    "The {b} recipe needs way more {a} than it says.",
    "We ended up talking about the {a} for two hours at {p}.",
    "Turns out the {a} was inside the {b} the entire time.",
    "Rain all day here, perfect weather for sorting the {a} drawer.",
]
QUESTIONS = [
    "Did you ever finish the {a}?",
    "Are you around on {d}?",
    "What time does the {a} thing at {p} start?",
    "Have you seen the new {a} at {p}?",
    "Do you still have my {a}?",
    "Is the {b} supposed to hum like that?",
]
REQUESTS = [
    "Can you bring the {a} on {d}?",
    "Could you send me the {a} notes?",
    "Please remind me to water the {a}.",
    "Let me know if {d} works for the {a} swap.",
    "Don't forget to label the {b} before {d}.",
]
PROMISES = [
    "I'll send you the {a} tomorrow.",
    "I will book {p} for {d}.",
    "I'm going to fix the {a} tonight.",
    "We'll bring the {b} on {d}.",
    "Let me check the {a} and get back to you.",
]
PROSE = [
    "The {a} project moved forward again once the {b} was calibrated.",
    "Nobody at {p} could agree on whether the {a} should be painted.",
    "A second batch of {a} parts is due before the next {b} fair.",
    "Notes from the workshop suggest the {b} works best in damp weather.",
    "The committee postponed the {a} vote until the {b} census is done.",
    "Early sketches of the {a} show a much smaller {b} than was built.",
    "Visitors to {p} are asked to leave their {a} at the door.",
    "It remains unclear who first paired a {a} with a {b}.",
    "The archive holds three letters about the {a} and one about {p}.",
    "After the flood the {a} was rebuilt using a borrowed {b}.",
]
EMOJI = ["👍", "😂", "❤️", "🎉", "😮", "🙏", "🔥", "👀"]


def iso(value):
    return value.isoformat()


def n_of(base, scale, minimum=1):
    return max(minimum, round(base * scale))


def slug(text):
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def line(rng, pool):
    return rng.choice(pool).format(
        a=rng.choice(THINGS),
        b=rng.choice(THINGS),
        p=rng.choice(SPOTS),
        d=rng.choice(DAYS),
    )


def prose(rng, sentences):
    return " ".join(line(rng, PROSE) for _ in range(sentences))


def hexid(rng, bits=64):
    return f"{rng.getrandbits(bits):0{bits // 4}x}"


def clamp(value, low, high):
    return max(low, min(high, value))


def placeholder_image(rng, alt):
    """A tiny inline SVG reference: nothing is downloaded."""
    hue = rng.randrange(360)
    w, h = rng.choice([(64, 48), (48, 64), (64, 64)])
    svg = (
        f"%3Csvg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}'%3E"
        f"%3Crect width='100%25' height='100%25' fill='hsl({hue} 60%25 70%25)'/%3E%3C/svg%3E"
    )
    return {
        "url": "data:image/svg+xml," + svg,
        "mime": "image/svg+xml",
        "width": w,
        "height": h,
        "alt": alt,
    }


def msg(channel, external_id, thread, at, content, fields, payload, **extra):
    thread = thread if isinstance(thread, dict) else {"external_id": thread}
    return dict(
        channel=channel,
        external_id=external_id,
        thread=thread,
        happened_at=iso(at),
        content=content,
        fields={k: v for k, v in fields.items() if v is not None},
        payload=payload,
        **{k: v for k, v in extra.items() if v is not None},
    )


def day_subject(day):
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return dict(
        start=iso(start),
        end=iso(start + timedelta(days=1)),
        start_precision="day",
        end_precision="day",
    )


# --- people ------------------------------------------------------------------


def make_people(seed, scale):
    rng = random.Random(f"{seed}:people")
    pairs = [(f, la) for f in FIRST for la in LAST]
    rng.shuffle(pairs)
    count = n_of(80, scale, 12)
    people = [
        dict(
            handle=OWNER,
            display_name="Juno Tumbleweed",
            is_owner=True,
            names=["Juno", "J. Tumbleweed", "Junebug"],
            ids={
                "discord": "junebug#0047",
                "instagram": "juno.tumbleweed",
                "sms": "+15550100047",
                "reddit": "u_kite_orchardist",
                "snapchat": "junebug47",
                "email": "juno@tumbleweed.example",
                "wave": "wave:zoo-owner",
            },
            facts={"city": "Lisbon", "employer": "Lanternworks Studio"},
            era=(0.0, 1.0),
            closeness=1.0,
        )
    ]
    for i in range(count):
        first, last = pairs[i]
        if i < 4:
            last = "Tumbleweed"  # family
        name = f"{first} {last}"
        h = "zoo-" + slug(name)
        kind = rng.choices(["life", "faded", "new", "blip"], [20, 35, 25, 20])[0]
        if i < 6:
            kind = "life"
        if kind == "life":
            era = (rng.uniform(0, 0.1), 1.0)
        elif kind == "faded":
            start = rng.uniform(0, 0.55)
            era = (start, min(0.9, start + rng.uniform(0.12, 0.45)))
        elif kind == "new":
            era = (rng.uniform(0.6, 0.93), 1.0)
        else:
            start = rng.uniform(0.05, 0.85)
            era = (start, start + rng.uniform(0.04, 0.12))
        ids = {}
        if i < 6 or rng.random() < 0.7:
            ids["discord"] = f"{first.lower()}{rng.randrange(1000, 9999)}"
        if rng.random() < 0.45:
            ids["instagram"] = f"{first.lower()}.{last.lower()}"
        if i < 6 or rng.random() < 0.4:
            ids["sms"] = f"+1555010{1000 + i}"
        if rng.random() < 0.5:
            ids["email"] = f"{first.lower()}@{last.lower()}.example"
        if i < 7:
            ids["wave"] = "wave:" + h
        if i in (4, 5):
            ids["snapchat"] = f"{first.lower()}_snaps"
        if not ids:
            ids["discord"] = f"{first.lower()}{rng.randrange(1000, 9999)}"
        facts = {}
        if rng.random() < 0.6:
            facts["city"] = rng.choice(
                ["Lisbon", "Porto", "Madrid", "Mossbridge", "Fernhaven"]
            )
        if rng.random() < 0.5:
            facts["birthday"] = f"{rng.randrange(1, 13):02d}-{rng.randrange(1, 29):02d}"
        if rng.random() < 0.35:
            facts["employer"] = rng.choice(ORG_NAMES)
        if rng.random() < 0.25:
            facts["favorite_snack"] = rng.choice(THINGS)
        names = [first]
        if rng.random() < 0.3:
            names.append(first[:3] + "sy")
        people.append(
            dict(
                handle=h,
                display_name=name,
                is_owner=False,
                names=names,
                ids=ids,
                facts=facts,
                era=era,
                closeness=min(40.0, rng.paretovariate(1.1)) * (4 if i < 6 else 1),
            )
        )
    rel = [
        (people[1]["handle"], OWNER, "parent"),
        (people[2]["handle"], OWNER, "parent"),
        (people[3]["handle"], OWNER, "sibling"),
        (people[4]["handle"], OWNER, "cousin"),
        (people[5]["handle"], OWNER, "partner"),
        (people[1]["handle"], people[2]["handle"], "partner"),
        (people[1]["handle"], people[3]["handle"], "parent"),
    ]
    for i, p in enumerate(people[6:], start=6):
        if p["facts"].get("employer") == "Lanternworks Studio" or i in (6, 7, 8):
            rel.append((p["handle"], OWNER, "colleague"))
        elif rng.random() < 0.5:
            rel.append((p["handle"], OWNER, "friend"))
    for _ in range(n_of(18, scale, 3)):
        a, b = rng.sample(people[6:], 2)
        rel.append(
            (a["handle"], b["handle"], rng.choice(["friend", "colleague", "sibling"]))
        )
    rel.append((people[6]["handle"], OWNER, "manager"))
    seen, relations = set(), []
    for r in rel:
        if r not in seen:
            seen.add(r)
            relations.append(r)
    return people, relations


ORG_NAMES = [
    "Lanternworks Studio",
    "Moss Weekly",
    "Pebble Radio Co",
    "Kite Orchard Society",
    "Fernwheel Cycles",
    "Cloudspoon Kitchens",
    "Starbucket Observatory",
    "Thistle Gym Collective",
    "Mooncup Press",
    "Twigboat Ferries",
    "Mistbell Records",
    "Snowgear Outfitters",
    "Sparkleaf Bank",
    "Puddlejump Airlines",
]
WORK_NAMES = [
    ("book", "The Velvet Compass"),
    ("book", "A Field Guide to Paper Comets"),
    ("book", "Noodle Atlas, Second Edition"),
    ("film", "Seven Marble Kettles"),
    ("film", "The Lint Telescope"),
    ("film", "Night of the Jelly Anchor"),
    ("album", "Songs for a Gravel Piano"),
    ("album", "Soup Violin Sessions"),
    ("show", "Pocket Glacier"),
    ("show", "The Turnip Rocket Hour"),
    ("game", "Button Orchard Tycoon"),
    ("podcast", "Plum Barometer Weekly"),
]
EVENT_NAMES = [
    "Kite Festival 2024",
    "Kite Festival 2025",
    "Kite Festival 2026",
    "Moss Lab Open Day",
    "Pebble Market Night",
    "Fern Pier Regatta",
    "Paper Comet Parade",
    "Lanternworks Demo Day",
]
# Invented names at real coordinates: one home city and two trips.
PLACES = {
    "home": ("Casa Tumbleweed", "Beco do Farol Inventado 12, Lisboa", 38.7139, -9.1296),
    "work": ("Lanternworks Studio", "Travessa do Musgo 4, Lisboa", 38.7075, -9.1450),
    "gym": ("Ginásio Thistle", "Rua do Cardo 88, Lisboa", 38.7250, -9.1500),
    "cafe": ("Café Moonjam", "Largo da Compota 3, Lisboa", 38.7107, -9.1366),
    "market": ("Mercado das Pedrinhas", "Praça do Seixo 1, Lisboa", 38.7167, -9.1363),
    "park": (
        "Jardim do Papagaio de Papel",
        "Avenida do Cometa, Lisboa",
        38.7381,
        -9.1535,
    ),
    "friend": ("Zibble's flat", "Rua da Lanterna 21, Lisboa", 38.7166, -9.1486),
    "viewpoint": (
        "Miradouro da Colher de Nuvem",
        "Calçada da Nuvem, Lisboa",
        38.7188,
        -9.1317,
    ),
    "lis_station": (
        "Estação Santa Lanterna",
        "Cais do Farol, Lisboa",
        38.7137,
        -9.1226,
    ),
    "lis_airport": (
        "Aeroporto, Terminal Pardal",
        "Alameda do Pardal, Lisboa",
        38.7742,
        -9.1342,
    ),
    "porto_station": (
        "Estação Campânula",
        "Largo da Campânula, Porto",
        41.1488,
        -8.5856,
    ),
    "porto_stay": ("Pensão Fernwheel", "Rua do Feto 9, Porto", 41.1408, -8.6130),
    "porto_sight": ("Torre do Sino de Névoa", "Rua da Névoa, Porto", 41.1458, -8.6139),
    "porto_cafe": ("Café Twigboat", "Praça do Graveto 5, Porto", 41.1496, -8.6109),
    "mad_airport": (
        "Aeropuerto, Terminal Gorrión",
        "Avenida del Gorrión, Madrid",
        40.4936,
        -3.5668,
    ),
    "mad_stay": ("Hostal Starbucket", "Calle del Cubo 14, Madrid", 40.4169, -3.7035),
    "mad_park": (
        "Parque del Cometa de Papel",
        "Paseo del Cometa, Madrid",
        40.4153,
        -3.6845,
    ),
    "mad_bar": (
        "Bar La Tetera de Mármol",
        "Calle de la Tetera 2, Madrid",
        40.4265,
        -3.7040,
    ),
    "mad_museum": (
        "Museo del Compás de Terciopelo",
        "Paseo del Compás, Madrid",
        40.4138,
        -3.6921,
    ),
}


def make_entities(seed):
    rng = random.Random(f"{seed}:entities")
    out = []
    for name in ORG_NAMES:
        out.append(
            dict(
                kind="organization",
                name=name,
                external_ref="zoo-org-" + slug(name),
                metadata={"zoo": True, "domain": slug(name) + ".example"},
            )
        )
    for kind, name in WORK_NAMES:
        out.append(
            dict(
                kind="work",
                name=name,
                external_ref="zoo-work-" + slug(name),
                metadata={
                    "zoo": True,
                    "work_type": kind,
                    "year": rng.randrange(1998, 2027),
                },
            )
        )
    for key, (name, address, lat, lon) in PLACES.items():
        out.append(
            dict(
                kind="place",
                name=name,
                external_ref="zoo-place-" + key,
                metadata={"zoo": True, "lat": lat, "lon": lon, "address": address},
            )
        )
    for city, lat, lon in (
        ("Lisboa", 38.7223, -9.1393),
        ("Porto", 41.1496, -8.6110),
        ("Madrid", 40.4168, -3.7038),
    ):
        out.append(
            dict(
                kind="place",
                name=city,
                external_ref="zoo-city-" + slug(city),
                metadata={"zoo": True, "lat": lat, "lon": lon, "place_type": "city"},
            )
        )
    for name in EVENT_NAMES:
        out.append(
            dict(
                kind="event",
                name=name,
                external_ref="zoo-event-" + slug(name),
                metadata={"zoo": True},
            )
        )
    for i in range(30):
        topic = THINGS[i % len(THINGS)]
        url = f"https://{slug(rng.choice(ORG_NAMES))}.example/notes/{slug(topic)}-{i}"
        out.append(
            dict(
                kind="web_page",
                name=f"Notes on the {topic}",
                external_ref=url,
                metadata={"zoo": True, "url": url},
            )
        )
    return out


def ent(kind, ref):
    return {"kind": kind, "external_ref": ref}


def key(channel, external_id):
    return {"channel": channel, "external_id": external_id}


def link(kind, a, b, **extra):
    return {"type": kind, "from": a, "to": b, "method": "source", **extra}


# --- chats -------------------------------------------------------------------

HOURS = list(range(24))
DIURNAL = [
    1,
    0.5,
    0.3,
    0.2,
    0.2,
    0.3,
    0.8,
    2,
    3.5,
    4.5,
    5,
    5.5,
    6,
    5.5,
    5,
    5,
    5.5,
    6.5,
    8,
    9,
    9.5,
    8.5,
    6,
    3,
]
DIURNAL_WEEKEND = [
    2.5,
    1.5,
    0.8,
    0.4,
    0.2,
    0.2,
    0.3,
    0.6,
    1.5,
    3,
    4.5,
    5.5,
    6,
    6,
    5.5,
    5.5,
    5.5,
    6,
    6.5,
    7,
    7.5,
    7.5,
    6.5,
    4.5,
]
WEEK = {
    "dm": [0.8, 0.8, 0.85, 0.85, 1.0, 1.0, 0.95],
    "group": [0.6, 0.65, 0.7, 0.8, 1.0, 1.0, 0.8],
    "channel": [1.0, 1.0, 1.0, 0.95, 0.8, 0.45, 0.4],
}
SHAPES = [(1, 2.5), (2.5, 1), (2, 2), (1, 1), (0.6, 3), (3, 0.7)]
NETWORKS = [
    dict(
        net="discord",
        msgs=120000,
        threads=300,
        years=9.2,
        kinds=(55, 25, 20),
        short=0.4,
        attach=0.05,
        react=0.08,
        strangers=12,
    ),
    dict(
        net="instagram",
        msgs=12000,
        threads=90,
        years=8.0,
        kinds=(90, 10, 0),
        short=0.6,
        attach=0.12,
        react=0.15,
        strangers=6,
    ),
    dict(
        net="sms",
        msgs=9000,
        threads=60,
        years=3.8,
        kinds=(92, 8, 0),
        short=0.45,
        attach=0.03,
        react=0.04,
        strangers=5,
    ),
    dict(
        net="reddit",
        msgs=2500,
        threads=60,
        years=1.15,
        kinds=(50, 0, 50),
        short=0.15,
        attach=0.01,
        react=0.0,
        strangers=24,
    ),
    dict(
        net="snapchat",
        msgs=250,
        threads=2,
        years=0.035,
        kinds=(100, 0, 0),
        short=0.75,
        attach=0.3,
        react=0.1,
        strangers=0,
    ),
]
GLOBAL_YEARS = 9.2


def chat_text(rng, short):
    r = rng.random()
    if r < short:
        return rng.choice(SHORT)
    r = rng.random()
    if r < 0.6:
        text = line(rng, STATEMENTS)
    elif r < 0.8:
        text = line(rng, QUESTIONS)
    elif r < 0.9:
        text = line(rng, REQUESTS)
    else:
        text = line(rng, PROMISES)
    if rng.random() < 0.15:
        text += " " + line(rng, STATEMENTS)
    return text


def make_chats(seed, scale, people):
    messages, links = [], []
    owner = people[0]
    t0 = ANCHOR - timedelta(days=365.25 * GLOBAL_YEARS)
    total_s = (ANCHOR - t0).total_seconds()
    for spec in NETWORKS:
        net = spec["net"]
        rng = random.Random(f"{seed}:chat:{net}")
        channel = "chat." + net
        kind_account = "phone" if net == "sms" else "account"
        net0 = 1 - spec["years"] / GLOBAL_YEARS
        members = [
            p for p in people[1:] if net in p["ids"] and p["era"][1] > net0 + 0.002
        ]
        strangers = [
            dict(
                handle=None,
                display_name=None
                if net == "sms"
                else f"{rng.choice(THINGS).replace(' ', '_')}_{rng.randrange(100)}",
                ids={
                    net: (
                        f"+1555019{rng.randrange(1000, 9999)}"
                        if net == "sms"
                        else f"{net}_stranger_{i}"
                    )
                },
                era=(net0, 1.0),
                closeness=0.6,
            )
            for i in range(spec["strangers"])
        ]
        if not members:
            members = strangers or [
                dict(
                    handle=None,
                    display_name=None,
                    ids={net: f"{net}_stranger_x"},
                    era=(net0, 1.0),
                    closeness=1.0,
                )
            ]
        n_threads = n_of(spec["threads"], scale, 2)
        n_msgs = n_of(spec["msgs"], scale, 40)
        threads = []
        dm_pool = (
            sorted(members, key=lambda p: -p["closeness"])
            + strangers[: max(1, len(strangers) // 2)]
        )
        dm_taken = 0
        for i in range(n_threads):
            kind = rng.choices(["dm", "group", "channel"], spec["kinds"])[0]
            if kind == "dm" and dm_taken >= len(dm_pool):
                kind = (
                    "group"
                    if spec["kinds"][1]
                    else ("channel" if spec["kinds"][2] else "dm")
                )
            if kind == "dm":
                person = dm_pool[dm_taken]
                dm_taken += 1
                lo, hi = max(person["era"][0], net0), person["era"][1]
                who = [person]
                title = person["display_name"] or person["ids"][net]
                tid = f"{net}:dm:{person['ids'][net]}"
                if any(t["tid"] == tid for t in threads):
                    tid += f":{i}"
                weight = person["closeness"] * rng.uniform(0.5, 1.5)
                guild = None
            else:
                lo = rng.uniform(net0, 0.95)
                hi = min(1.0, lo + rng.uniform(0.1, 0.6)) if rng.random() < 0.6 else 1.0
                pool = [
                    p for p in members if p["era"][0] < hi and p["era"][1] > lo
                ] or members
                size = rng.randrange(2, 7) if kind == "group" else rng.randrange(5, 20)
                who = rng.sample(pool, min(size, len(pool)))
                if kind == "channel" or net == "reddit":
                    who += rng.sample(
                        strangers, min(len(strangers), rng.randrange(1, 5))
                    )
                guild = f"guild-{rng.randrange(1, 6)}" if kind == "channel" else None
                topic = rng.choice(THINGS)
                title = (
                    ("#" + slug(topic))
                    if kind == "channel"
                    else f"{topic.title()} Club"
                )
                tid = f"{net}:{guild or 'group'}:{i}"
                weight = (
                    min(40.0, rng.paretovariate(1.3))
                    * 0.5
                    * (1.5 if kind == "channel" else 1)
                )
            threads.append(
                dict(
                    tid=tid,
                    kind=kind,
                    who=who,
                    title=title,
                    lo=lo,
                    hi=hi,
                    weight=weight,
                    guild=guild,
                    no=i,
                )
            )
        total_w = sum(t["weight"] for t in threads)
        for t in threads:
            want = max(3, round(n_msgs * t["weight"] / total_w))
            a, b = rng.choice(SHAPES)
            silence = None
            if rng.random() < 0.4:
                s = rng.uniform(0.15, 0.7)
                silence = (s, s + rng.uniform(0.1, 0.3))
            start = t0 + timedelta(seconds=total_s * t["lo"])
            span = total_s * (t["hi"] - t["lo"])
            owner_share = {"dm": 0.46, "group": 0.2, "channel": 0.08}[t["kind"]]
            speakers = [owner] + t["who"]
            weights = [owner_share] + [(1 - owner_share) / len(t["who"])] * len(
                t["who"]
            )
            rows, left, guard = [], want, 0
            while left > 0 and guard < want * 20 + 200:
                guard += 1
                u = rng.betavariate(a, b)
                if silence and silence[0] < u < silence[1]:
                    continue
                day = (start + timedelta(seconds=span * u)).date()
                weekday = day.weekday()
                if rng.random() > WEEK[t["kind"]][weekday]:
                    continue
                hour = rng.choices(HOURS, DIURNAL_WEEKEND if weekday >= 5 else DIURNAL)[
                    0
                ]
                at = datetime(
                    day.year,
                    day.month,
                    day.day,
                    hour,
                    rng.randrange(60),
                    rng.randrange(60),
                    tzinfo=UTC,
                )
                size = min(left, 1 + int(rng.lognormvariate(1.9, 1.0)))
                speaker = rng.choices(speakers, weights)[0]
                for _ in range(size):
                    at += timedelta(seconds=rng.expovariate(1 / 50) + 3)
                    if rng.random() < 0.06:
                        at += timedelta(minutes=rng.uniform(4, 25))
                    if at > ANCHOR:
                        break
                    if rng.random() > 0.4:
                        speaker = rng.choices(speakers, weights)[0]
                    rows.append((at, speaker))
                    left -= 1
            rows.sort(key=lambda r: r[0])
            recent = []
            for i, (at, speaker) in enumerate(rows):
                eid = f"{net}-{t['no']:04d}-{i:06d}"
                mine = speaker is owner
                text = chat_text(rng, spec["short"])
                fields = dict(
                    direction="outgoing" if mine else "incoming",
                    time_precision="exact",
                    identity_quality=rng.choices(
                        ["native_id", "beeper_id", "fingerprint"], [90, 7, 3]
                    )[0],
                    conversation_kind=t["kind"],
                    guild_id=t["guild"],
                )
                images = None
                if rng.random() < 0.004:
                    fields["time_precision"] = "approximate"
                    fields["time_inferred_from"] = {
                        "method": "neighbours",
                        "window_s": 600,
                    }
                if rng.random() < 0.02:
                    fields["edited_at"] = iso(
                        at + timedelta(seconds=rng.randrange(20, 900))
                    )
                if recent and rng.random() < 0.03:
                    target = rng.choice(recent)
                    if target[1] is not speaker:
                        fields["reply_to"] = target[0]
                        links.append(
                            link("reply_to", key(channel, eid), key(channel, target[0]))
                        )
                if rng.random() < spec["attach"]:
                    kind = rng.choices(
                        ["image", "file", "audio", "video"], [60, 15, 15, 10]
                    )[0]
                    ext = {
                        "image": "jpg",
                        "file": "pdf",
                        "audio": "ogg",
                        "video": "mp4",
                    }[kind]
                    name = f"{slug(rng.choice(THINGS))}.{ext}"
                    fields["attachments"] = [
                        dict(
                            type=kind,
                            name=name,
                            mime={
                                "image": "image/jpeg",
                                "file": "application/pdf",
                                "audio": "audio/ogg",
                                "video": "video/mp4",
                            }[kind],
                            size=rng.randrange(20_000, 9_000_000),
                        )
                    ]
                    if kind == "image" and rng.random() < 0.25:
                        images = [placeholder_image(rng, name)]
                if rng.random() < spec["react"]:
                    fields["reactions"] = [
                        dict(emoji=e, count=rng.randrange(1, 4))
                        for e in rng.sample(EMOJI, rng.randrange(1, 3))
                    ]
                value = speaker["ids"][net]
                thread = {"external_id": t["tid"], "title": t["title"]}
                if i == 0:
                    thread["metadata"] = {
                        "network": net,
                        "kind": t["kind"],
                        "members": len(t["who"]) + 1,
                        "guild_id": t["guild"],
                    }
                messages.append(
                    msg(
                        channel,
                        eid,
                        thread,
                        at,
                        text,
                        fields,
                        {
                            "zoo": True,
                            "id": eid,
                            "chat": t["tid"],
                            "sender": value,
                            "text": text,
                        },
                        author=dict(
                            system=net,
                            kind=kind_account,
                            value=value,
                            display_name=speaker["display_name"],
                        ),
                        images=images,
                    )
                )
                recent.append((eid, speaker))
                if len(recent) > 20:
                    recent.pop(0)
    return messages, links


# --- transcripts ---------------------------------------------------------------


def make_wave(seed, scale, people):
    rng = random.Random(f"{seed}:wave")
    messages, links, annotations = [], [], []
    voices = [p for p in people if "wave" in p["ids"]]
    for r in range(n_of(120, scale, 3)):
        start = ANCHOR - timedelta(days=rng.uniform(0.2, 175))
        start = start.replace(
            hour=rng.choices(HOURS, DIURNAL)[0],
            minute=rng.randrange(60),
            second=0,
            microsecond=0,
        )
        if start > ANCHOR - timedelta(hours=3):
            start -= timedelta(days=1)
        rid = f"wave-rec-{r:04d}"
        cast = [people[0]] + rng.sample(
            voices[1:], min(len(voices) - 1, rng.randrange(1, 4))
        )
        unknown = rng.random() < 0.45
        labels = [p["display_name"] for p in cast] + (
            ["Speaker " + str(len(cast) + 1)] if unknown else []
        )
        kind = rng.choice(["meeting", "walk", "call", "dinner", "voice_note"])
        place = rng.choice(["home", "work", "cafe", "park", "friend"])
        turns, at_s = [], 0.0
        for i in range(max(4, int(rng.lognormvariate(4.1, 0.6)))):
            who = rng.randrange(len(labels))
            text = " ".join(
                line(
                    rng,
                    rng.choice([STATEMENTS, STATEMENTS, QUESTIONS, REQUESTS, PROMISES]),
                )
                for _ in range(rng.randrange(1, 4))
            )
            noise = rng.random() < 0.015
            if noise:
                text = "And now the weather for the Fern Pier: more paper comets after the break."
            length = max(2.0, len(text) / 14 + rng.uniform(-1, 2))
            turns.append((i, who, text, at_s, at_s + length, noise))
            at_s += length + rng.uniform(0.2, 3)
        end = start + timedelta(seconds=at_s)
        title = f"{kind.replace('_', ' ').title()} about the {rng.choice(THINGS)}"
        summary = (
            f"{title}\n\n"
            + prose(rng, rng.randrange(4, 12))
            + "\n\nAction items:\n"
            + "\n".join("- " + line(rng, PROMISES) for _ in range(rng.randrange(1, 4)))
        )
        messages.append(
            msg(
                "recordings.wave",
                rid,
                {"external_id": rid, "title": title},
                start,
                summary,
                dict(
                    time_precision="exact",
                    duration_s=round(at_s, 1),
                    device=rng.choice(["pebble-phone", "lapel-mic"]),
                    source_platform="wave",
                    recording_type=kind,
                    turn_count=len(turns),
                ),
                {"zoo": True, "recording": rid, "speakers": labels},
                ended_at=iso(end),
            )
        )
        links.append(
            link(
                "at_place",
                key("recordings.wave", rid),
                ent("place", "zoo-place-" + place),
            )
        )
        for i, who, text, s0, s1, noise in turns:
            person = cast[who] if who < len(cast) else None
            eid = f"{rid}-t{i:04d}"
            messages.append(
                msg(
                    "transcripts.wave",
                    eid,
                    {"external_id": rid, "title": title},
                    start + timedelta(seconds=s0),
                    text,
                    dict(
                        speaker_label=labels[who],
                        start_s=round(s0, 2),
                        end_s=round(s1, 2),
                        speaker_provenance="voiceprint" if person else "diarization",
                        ignore_reason="media_noise" if noise else None,
                        place=PLACES[place][0],
                    ),
                    {"zoo": True, "recording": rid, "turn": i},
                    ended_at=iso(start + timedelta(seconds=s1)),
                    author=dict(
                        system="wave",
                        kind="speaker",
                        value=person["ids"]["wave"],
                        display_name=person["display_name"],
                    )
                    if person
                    else None,
                )
            )
        links.append(
            link(
                "transcript_of",
                key("transcripts.wave", f"{rid}-t0000"),
                key("recordings.wave", rid),
            )
        )
        if unknown and len(annotations) < 6:
            first = next((t for t in turns if t[1] >= len(cast)), None)
            if first:
                annotations.append(
                    dict(
                        channel="transcripts.wave",
                        external_id=f"{rid}-t{first[0]:04d}",
                        attribute="speaker",
                        value={"person": rng.choice(people[8:12])["handle"]},
                        reason="Recognised the voice (zoo)",
                    )
                )
        if r in (0, 1):
            annotations.append(
                dict(
                    channel="recordings.wave",
                    external_id=rid,
                    attribute="effective_place",
                    value={"name": PLACES[place][0]},
                    reason="Recorded there (zoo)",
                )
            )
        if r == 2:
            annotations.append(
                dict(
                    channel="recordings.wave",
                    external_id=rid,
                    attribute="ignore",
                    value={"ignored": True, "reason": "pocket recording"},
                    reason="Accidental recording (zoo)",
                )
            )
    return messages, links, annotations


# --- e-mail ----------------------------------------------------------------------


def make_email(seed, scale, people):
    rng = random.Random(f"{seed}:email")
    messages, links = [], []
    me = people[0]["ids"]["email"]
    account = "ca_zoo_primary"
    writers = [p for p in people[1:] if "email" in p["ids"]] or people[1:2]
    lists = [
        (o, f"<{rng.choice(['digest', 'news', 'updates'])}.{slug(o)}.example>")
        for o in ORG_NAMES[1:]
    ]
    count = n_of(4000, scale, 30)
    done = 0
    while done < count:
        at = ANCHOR - timedelta(days=rng.uniform(0, 160))
        at = at.replace(
            hour=rng.choices(HOURS, DIURNAL)[0],
            minute=rng.randrange(60),
            second=rng.randrange(60),
        )
        if at > ANCHOR:
            continue
        kind = rng.choices(["list", "receipt", "personal", "work"], [45, 25, 20, 10])[0]
        tid = hexid(rng)
        list_id = org = None
        replies = 1
        if kind == "list":
            org, list_id = rng.choice(lists)
            subject = f"{org} digest: {rng.choice(THINGS)} and {rng.choice(THINGS)}"
            sender, sender_name = f"hello@{slug(org)}.example", org
            labels = [
                "INBOX",
                rng.choice(
                    ["CATEGORY_UPDATES", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS"]
                ),
            ]
            body = "\n\n".join(
                f"## {rng.choice(THINGS).title()}\n\n{prose(rng, rng.randrange(4, 12))}\n\n{prose(rng, rng.randrange(3, 9))}\n\nRead more: https://{slug(org)}.example/issue/{rng.randrange(1, 400)}"
                for _ in range(rng.randrange(3, 12))
            )
        elif kind == "receipt":
            org = rng.choice(ORG_NAMES)
            subject = rng.choice(
                [
                    "Your order #{n} has shipped",
                    "Receipt for booking {n}",
                    "Reminder: appointment {n}",
                    "Invoice {n} is ready",
                ]
            ).format(n=rng.randrange(1000, 99999))
            sender, sender_name = f"no-reply@{slug(org)}.example", org
            labels = ["INBOX", "CATEGORY_UPDATES"]
            body = f"Hello Juno,\n\n{prose(rng, 2)}\n\nItem: {rng.choice(THINGS)} x{rng.randrange(1, 4)}\nTotal: {rng.randrange(4, 240)}.{rng.randrange(100):02d} invented credits\n\nThank you,\n{org}"
        else:
            person = rng.choice(writers)
            subject = rng.choice(
                [
                    "About the {a}",
                    "{a} on {d}?",
                    "Notes from {p}",
                    "Re-planning the {b} weekend",
                ]
            ).format(
                a=rng.choice(THINGS),
                b=rng.choice(THINGS),
                p=rng.choice(SPOTS),
                d=rng.choice(DAYS),
            )
            sender, sender_name = person["ids"]["email"], person["display_name"]
            labels = ["INBOX", "CATEGORY_PERSONAL"] + (
                ["IMPORTANT"] if kind == "work" else []
            )
            body = f"Hi Juno,\n\n{prose(rng, rng.randrange(2, 7))}\n\n{line(rng, rng.choice([QUESTIONS, REQUESTS]))}\n\n{person['display_name'].split()[0]}"
            replies = rng.choices([1, 2, 3, 4], [70, 18, 8, 4])[0]
        previous = None
        for step in range(replies):
            outgoing = step % 2 == 1
            mid = hexid(rng)
            rfc = f"<{mid}@mail.{'tumbleweed' if outgoing else slug(sender_name)}.example>"
            text = (
                body
                if step == 0
                else f"{'Thanks! ' if outgoing else ''}{prose(rng, rng.randrange(1, 4))}\n\n{line(rng, PROMISES if outgoing else QUESTIONS)}"
            )
            when = at + timedelta(hours=step * rng.uniform(0.3, 20))
            if when > ANCHOR:
                break
            frm, frm_name = (
                (me, "Juno Tumbleweed") if outgoing else (sender, sender_name)
            )
            to = (
                [{"name": sender_name, "address": sender}]
                if outgoing
                else [{"name": "Juno Tumbleweed", "address": me}]
            )
            attach = (
                [
                    dict(
                        filename=f"{slug(rng.choice(THINGS))}.pdf",
                        mime_type="application/pdf",
                        size=rng.randrange(9_000, 2_000_000),
                    )
                ]
                if rng.random() < 0.08
                else []
            )
            fields = dict(
                account=account,
                provider="gmail",
                gmail_message_id=mid,
                gmail_thread_id=tid,
                gmail_history_id=str(rng.randrange(10**6, 10**7)),
                rfc822_message_id=rfc,
                in_reply_to=previous,
                references=previous,
                list_id=list_id,
                subject=("Re: " if step else "") + subject,
                from_address=frm,
                from_name=frm_name,
                to=to,
                cc=[],
                bcc=[],
                reply_to=[],
                participants=sorted({frm, to[0]["address"]}),
                labels=["SENT"]
                if outgoing
                else labels + (["UNREAD"] if rng.random() < 0.2 else []),
                label_ids=["SENT"] if outgoing else labels,
                snippet=text[:120].replace("\n", " "),
                direction="outgoing" if outgoing else "incoming",
                is_unread=False if outgoing else rng.random() < 0.2,
                is_starred=rng.random() < 0.03,
                is_important=kind == "work" or rng.random() < 0.05,
                date_header=when.strftime("%a, %d %b %Y %H:%M:%S +0000"),
                body_format="markdown" if kind == "list" else "text",
                body_chars=len(text),
                body_truncated=False,
                attachment_count=len(attach),
                attachments=attach,
                size_estimate=len(text) * 3 + sum(a["size"] for a in attach),
                partial=False,
                mapping_revision=3,
            )
            author = None
            if kind in ("personal", "work"):
                author = dict(
                    system="email", kind="email", value=frm, display_name=frm_name
                )
            messages.append(
                msg(
                    "email.message",
                    mid,
                    {"external_id": tid, "title": subject},
                    when,
                    f"{fields['subject']}\n\n{text}",
                    fields,
                    {
                        "zoo": True,
                        "id": mid,
                        "threadId": tid,
                        "labelIds": fields["label_ids"],
                    },
                    author=author,
                )
            )
            if org and step == 0 and rng.random() < 0.15:
                links.append(
                    link(
                        "published_by",
                        key("email.message", mid),
                        ent("organization", "zoo-org-" + slug(org)),
                    )
                )
            previous = rfc
            done += 1
    return messages, links


# --- calendar --------------------------------------------------------------------


def make_calendar(seed, scale, people):
    rng = random.Random(f"{seed}:calendar")
    messages, links = [], []
    me = people[0]["ids"]["email"]
    back = timedelta(days=max(45, 365 * 3 * scale))
    ahead = timedelta(days=max(20, 180 * min(1.0, scale * 2)))
    first, last = ANCHOR - back, ANCHOR + ahead
    calendars = {
        "primary": "Juno (personal)",
        "work": "Lanternworks",
        "family": "Tumbleweed family",
        "birthdays": "Birthdays",
        "holidays": "Invented holidays",
    }
    guests = [p for p in people[1:] if "email" in p["ids"]] or people[1:3]

    def emit(
        cal,
        event_id,
        series,
        title,
        start,
        end,
        all_day=False,
        place=None,
        who=(),
        created=None,
    ):
        status = (
            "cancelled"
            if rng.random() < 0.03
            else rng.choices(["confirmed", "tentative"], [95, 5])[0]
        )
        attendees = [
            dict(
                email=me,
                name="Juno Tumbleweed",
                response=rng.choice(
                    ["accepted", "accepted", "tentative", "needsAction"]
                ),
                self=True,
            )
        ] + [
            dict(
                email=p["ids"]["email"],
                name=p["display_name"],
                response=rng.choice(
                    ["accepted", "accepted", "declined", "needsAction", "tentative"]
                ),
            )
            for p in who
        ]
        created = created or (start - timedelta(days=rng.uniform(1, 60)))
        organizer = who[0] if who and rng.random() < 0.4 else None
        fields = dict(
            account="ca_zoo_primary",
            calendar_id=f"{cal}@calendar.example",
            calendar_name=calendars[cal],
            event_id=event_id,
            series_id=series,
            ical_uid=f"{series or event_id}@calendar.example",
            title=title,
            status=status,
            all_day=all_day,
            place=PLACES[place][0] if place else None,
            observed_at=iso(min(ANCHOR, max(created, start - timedelta(days=30)))),
            mapping_revision=2,
            start_time_zone="Europe/Lisbon",
            original_start=iso(start) if series else None,
            description=prose(rng, rng.randrange(1, 4)) if rng.random() < 0.4 else None,
            organizer_email=organizer["ids"]["email"] if organizer else me,
            organizer_name=organizer["display_name"]
            if organizer
            else "Juno Tumbleweed",
            attendees=attendees if who else [],
            attendee_count=len(attendees) if who else 0,
            my_response=attendees[0]["response"],
            conference_url=f"https://meet.calendar.example/{hexid(rng, 32)}"
            if cal == "work" and rng.random() < 0.6
            else None,
            html_link=f"https://calendar.example/event?eid={event_id}",
            event_type="birthday" if cal == "birthdays" else "default",
            visibility="default",
            transparency="transparent" if all_day else "opaque",
            event_created=iso(min(created, ANCHOR)),
            event_updated=iso(min(created + timedelta(days=rng.uniform(0, 5)), ANCHOR)),
        )
        eid = f"{cal}:{event_id}"
        precision = "day" if all_day else "exact"
        content = (
            f"{title}\n{start:%Y-%m-%d}"
            + ("" if all_day else f" {start:%H:%M}-{end:%H:%M}")
            + (f"\nAt {fields['place']}" if place else "")
            + ("\nWith " + ", ".join(p["display_name"] for p in who) if who else "")
        )
        messages.append(
            msg(
                "calendar.events",
                eid,
                {"external_id": f"{cal}@calendar.example", "title": calendars[cal]},
                start,
                content,
                fields,
                {"zoo": True, "id": event_id, "status": status},
                ended_at=iso(end),
                subject=dict(
                    start=iso(start),
                    end=iso(end),
                    start_precision=precision,
                    end_precision=precision,
                ),
            )
        )
        if place and rng.random() < 0.5:
            links.append(
                link(
                    "at_place",
                    key("calendar.events", eid),
                    ent("place", "zoo-place-" + place),
                )
            )
        for p in who[:2]:
            if rng.random() < 0.3:
                links.append(
                    link(
                        "participant",
                        key("calendar.events", eid),
                        {"person": p["handle"]},
                    )
                )
        return fields

    def change(
        event_id,
        series,
        title,
        at,
        what,
        fields,
        changed=None,
        recurrence=None,
        occurrences=None,
    ):
        if at > ANCHOR:
            return
        cid = f"{event_id}:{what}:{int(at.timestamp())}"
        messages.append(
            msg(
                "calendar.changes",
                cid,
                {"external_id": series or event_id, "title": title},
                at,
                f"{what.title()}: {title}"
                + (f" ({', '.join(changed)})" if changed else ""),
                dict(
                    account=fields["account"],
                    calendar_id=fields["calendar_id"],
                    calendar_name=fields["calendar_name"],
                    event_id=event_id,
                    series_id=series,
                    ical_uid=fields["ical_uid"],
                    title=title,
                    status="cancelled" if what == "cancelled" else "confirmed",
                    all_day=fields["all_day"],
                    place=fields.get("place"),
                    observed_at=iso(at),
                    mapping_revision=2,
                    change=what,
                    changed_fields=changed or [],
                    recurrence=recurrence,
                    creator_email=fields["organizer_email"],
                    organizer_email=fields["organizer_email"],
                    time_precision="exact",
                    occurrence_count=occurrences,
                ),
                {"zoo": True, "event": event_id, "change": what},
            )
        )

    def series(
        cal,
        name,
        title,
        weekday,
        hour,
        minutes,
        every_days,
        place=None,
        who=(),
        since=None,
    ):
        start = max(first, since or first)
        day = start + timedelta(days=(weekday - start.weekday()) % 7)
        day = day.replace(hour=hour, minute=0, second=0, microsecond=0)
        sid, count, fields = f"zoo-series-{name}", 0, None
        while day < last:
            if rng.random() > 0.06:  # skipped occurrences
                fields = emit(
                    cal,
                    f"{sid}_{day:%Y%m%dT%H%M%SZ}",
                    sid,
                    title,
                    day,
                    day + timedelta(minutes=minutes),
                    place=place,
                    who=who,
                    created=start - timedelta(days=3),
                )
                count += 1
            day += timedelta(days=every_days)
        if fields:
            change(
                sid,
                sid,
                title,
                start - timedelta(days=3),
                "created",
                fields,
                recurrence=[f"RRULE:FREQ=DAILY;INTERVAL={every_days}"],
                occurrences=count,
            )

    work = [p for p in guests[:6]]
    series("work", "sync", "Studio sync", 0, 10, 30, 7, "work", work[:4])
    series(
        "work",
        "one-on-one",
        f"1:1 with {work[0]['display_name']}",
        2,
        14,
        45,
        14,
        "work",
        work[:1],
    )
    series("primary", "kite-club", "Kite Club", 5, 15, 120, 28, "park", guests[2:5])
    series(
        "primary",
        "circuit",
        "Thistle circuit class",
        3,
        18,
        75,
        7,
        "gym",
        since=ANCHOR - timedelta(days=400),
    )
    series(
        "family",
        "dinner",
        "Family dinner",
        6,
        19,
        120,
        14,
        "home",
        people[1:4] if all("email" in p["ids"] for p in people[1:4]) else (),
    )
    year = first.year
    while year <= last.year:
        for i, p in enumerate(people[1 : n_of(20, scale, 4)]):
            month, dom = (
                int(x)
                for x in p["facts"]
                .get("birthday", f"{1 + i % 12:02d}-{1 + (i * 7) % 28:02d}")
                .split("-")
            )
            day = datetime(year, month, dom, tzinfo=UTC)
            if first <= day < last:
                emit(
                    "birthdays",
                    f"zoo-bday-{slug(p['display_name'])}_{year}",
                    f"zoo-bday-{slug(p['display_name'])}",
                    f"{p['display_name']}'s birthday",
                    day,
                    day + timedelta(days=1),
                    all_day=True,
                )
        for i, name in enumerate(
            [
                "Paper Comet Day",
                "Moss Equinox",
                "Lantern Night",
                "Kite Monday",
                "Pebble Eve",
                "Feast of the Fern",
            ]
        ):
            day = datetime(year, 1 + (i * 2) % 12, 3 + i * 4, tzinfo=UTC)
            if first <= day < last:
                emit(
                    "holidays",
                    f"zoo-holiday-{slug(name)}_{year}",
                    None,
                    name,
                    day,
                    day + timedelta(days=1),
                    all_day=True,
                )
        year += 1
    span = (last - first).total_seconds()
    for i in range(n_of(350, scale, 12)):
        start = first + timedelta(seconds=rng.uniform(0, span))
        start = start.replace(
            hour=rng.choice([8, 9, 12, 13, 16, 18, 19, 20]),
            minute=rng.choice([0, 0, 15, 30]),
            second=0,
            microsecond=0,
        )
        kind = rng.choices(["social", "errand", "trip", "show"], [45, 25, 10, 20])[0]
        event_id = "zoo-evt-" + hexid(rng, 48)
        who = (
            rng.sample(guests, min(len(guests), rng.randrange(1, 4)))
            if kind in ("social", "show")
            else ()
        )
        if kind == "trip":
            start = start.replace(hour=0, minute=0)
            title = (
                f"Trip: {rng.choice(['Porto', 'Madrid', 'Mossbridge', 'Fernhaven'])}"
            )
            fields = emit(
                "primary",
                event_id,
                None,
                title,
                start,
                start + timedelta(days=rng.randrange(2, 6)),
                all_day=True,
            )
        else:
            title = {
                "social": rng.choice(
                    ["Dinner with {n}", "Coffee with {n}", "Board games with {n}"]
                ).format(n=who[0]["display_name"].split()[0] if who else "nobody"),
                "errand": rng.choice(
                    [
                        "Dentist",
                        "Bike repair",
                        "Passport office",
                        "Haircut",
                        "Vet for the paper heron",
                    ]
                ),
                "show": f"{rng.choice(EVENT_NAMES)} outing",
            }[kind]
            place = rng.choice(["cafe", "market", "friend", "park", "viewpoint", None])
            fields = emit(
                "primary",
                event_id,
                None,
                title,
                start,
                start + timedelta(minutes=rng.choice([30, 60, 90, 120, 180])),
                place=place,
                who=who,
            )
        created = datetime.fromisoformat(fields["event_created"])
        change(event_id, None, title, created, "created", fields)
        if rng.random() < 0.18:
            change(
                event_id,
                None,
                title,
                created + timedelta(days=rng.uniform(0.1, 6)),
                "changed",
                fields,
                changed=rng.sample(
                    ["start", "end", "place", "title", "attendees"], rng.randrange(1, 3)
                ),
            )
        if fields["status"] == "cancelled":
            change(
                event_id,
                None,
                title,
                created + timedelta(days=rng.uniform(0.5, 8)),
                "cancelled",
                fields,
                changed=["status"],
            )
    return messages, links


# --- health -----------------------------------------------------------------------


def make_health(seed, scale):
    rng = random.Random(f"{seed}:health")
    messages = []
    days = n_of(900, scale, 40)
    start = (ANCHOR - timedelta(days=days)).date()
    gap = (days // 3, days // 3 + max(2, round(9 * min(1, scale * 4))))
    sick = {days // 5 + i for i in range(5)} | {days - 60 + i for i in range(4)}
    travel = {days - 14, days - 13, days - 12, days - 8, days - 7, days - 6, days - 5}
    short_night, long_night, big_hike = days // 2, days // 2 + 40, days - 33
    gap_window = (start + timedelta(days=gap[0]), start + timedelta(days=gap[1]))
    for t in range(days):
        day = start + timedelta(days=t)
        midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
        if gap[0] <= t < gap[1] or (rng.random() < 0.03 and t < days - 5):
            continue
        weekend = day.weekday() >= 5
        fit = 0.5 + 0.2 * math.sin(2 * math.pi * t / 365) + 0.25 * t / days
        ill = t in sick
        hours = (
            7.2
            + (0.6 if weekend else 0)
            + rng.gauss(0, 0.7)
            - (0.9 if t in travel else 0)
            + (1.2 if ill else 0)
        )
        if t == short_night:
            hours = 3.1
        if t == long_night:
            hours = 11.2
        hours = clamp(hours, 2.5, 12)
        hrv = clamp(42 + 18 * fit + rng.gauss(0, 6) - (14 if ill else 0), 12, 110)
        rhr = clamp(58 - 6 * fit + rng.gauss(0, 2) + (8 if ill else 0), 40, 85)
        steps = int(
            clamp(
                (7500 + 2500 * fit)
                * rng.lognormvariate(0, 0.35)
                * (0.75 if weekend and rng.random() < 0.6 else 1)
                * (0.3 if ill else 1)
                * (1.5 if t in travel else 1),
                300,
                40000,
            )
        )
        if t == big_hike:
            steps = 31482
        sleep_score = int(clamp(55 + (hours - 5) * 10 + rng.gauss(0, 5), 20, 99))
        readiness = int(
            clamp(
                72
                + (hrv - 52) * 0.6
                - (rhr - 55) * 1.2
                + rng.gauss(0, 4)
                - (15 if ill else 0),
                15,
                99,
            )
        )
        activity = int(clamp(45 + steps / 250 + rng.gauss(0, 5), 10, 100))
        temp = round(rng.gauss(0, 0.15) + (1.1 if ill else 0), 2)
        in_bed = hours * 3600 / rng.uniform(0.84, 0.95)
        bed = (
            midnight
            - timedelta(minutes=50)
            + timedelta(minutes=rng.gauss(0, 45) + (40 if weekend else 0))
        )
        wake = bed + timedelta(seconds=in_bed)
        total_s = int(hours * 3600)
        deep = int(total_s * rng.uniform(0.13, 0.22))
        rem = int(total_s * rng.uniform(0.17, 0.26))
        contrib = lambda base: int(clamp(base + rng.gauss(0, 8), 1, 100))  # noqa: E731
        active_cal = int(steps * 0.045 * rng.uniform(0.8, 1.2))
        high, medium, low = (
            int(steps * 0.25),
            int(steps * 1.3),
            int(14000 + steps * 0.6),
        )
        thread = {"external_id": f"oura-{day:%Y-%m}", "title": f"Oura {day:%B %Y}"}
        daily = dict(
            provider="oura",
            mapping_revision=4,
            day=str(day),
            sleep_score=sleep_score,
            sleep_contrib_deep_sleep=contrib(75),
            sleep_contrib_efficiency=contrib(85),
            sleep_contrib_latency=contrib(80),
            sleep_contrib_rem_sleep=contrib(70),
            sleep_contrib_restfulness=contrib(72),
            sleep_contrib_timing=contrib(65),
            sleep_contrib_total_sleep=int(clamp(hours * 12, 1, 100)),
            readiness_score=readiness,
            temperature_deviation=temp,
            temperature_trend_deviation=round(temp * 0.4 + rng.gauss(0, 0.05), 2),
            readiness_contrib_activity_balance=contrib(80),
            readiness_contrib_body_temperature=contrib(95 - (40 if ill else 0)),
            readiness_contrib_hrv_balance=contrib(hrv + 20),
            readiness_contrib_previous_day_activity=contrib(78),
            readiness_contrib_previous_night=contrib(sleep_score),
            readiness_contrib_recovery_index=contrib(82),
            readiness_contrib_resting_heart_rate=contrib(130 - rhr),
            readiness_contrib_sleep_balance=contrib(76),
            readiness_contrib_sleep_regularity=contrib(70),
            activity_score=activity,
            steps=steps,
            active_calories=active_cal,
            total_calories=2100 + active_cal,
            target_calories=450,
            equivalent_walking_distance_m=int(steps * 0.76),
            high_activity_s=high,
            medium_activity_s=medium,
            low_activity_s=low,
            sedentary_s=max(0, 86400 - high - medium - low - int(in_bed)),
            resting_s=int(in_bed),
            non_wear_s=rng.choice([0, 0, 0, 600, 3600]),
            high_activity_met_minutes=high // 8,
            medium_activity_met_minutes=medium // 15,
            low_activity_met_minutes=low // 40,
            sedentary_met_minutes=rng.randrange(4, 14),
            average_met_minutes=round(1.2 + steps / 20000, 3),
            inactivity_alerts=rng.randrange(0, 4),
            meters_to_target=max(-9000, int(8000 - steps * 0.76)),
            target_meters=8000,
            activity_contrib_meet_daily_targets=contrib(70),
            activity_contrib_move_every_hour=contrib(85),
            activity_contrib_recovery_time=contrib(90),
            activity_contrib_stay_active=contrib(65),
            activity_contrib_training_frequency=contrib(75),
            activity_contrib_training_volume=contrib(70),
            stress_high_s=int(
                clamp(rng.gauss(5400, 3000) + (6000 if ill else 0), 0, 30000)
            ),
            recovery_high_s=int(clamp(rng.gauss(4000, 2500), 0, 20000)),
            stress_day_summary=rng.choice(["restored", "normal", "normal", "stressful"])
            if not ill
            else "stressful",
            spo2_average=round(clamp(rng.gauss(96.8, 0.6), 91, 100), 1),
            breathing_disturbance_index=rng.randrange(0, 12),
            resilience_level=rng.choice(
                ["limited", "adequate", "solid", "solid", "strong"]
            ),
            resilience_sleep_recovery=round(clamp(rng.gauss(70, 10), 0, 100), 1),
            resilience_daytime_recovery=round(clamp(rng.gauss(40, 15), 0, 100), 1),
            resilience_stress=round(clamp(rng.gauss(55, 12), 0, 100), 1),
            vascular_age=int(34 - 4 * fit),
            vo2_max=round(41 + 6 * fit, 1),
            main_sleep_total_s=total_s,
            main_sleep_time_in_bed_s=int(in_bed),
            main_sleep_efficiency=int(100 * total_s / in_bed),
            main_sleep_lowest_heart_rate=int(rhr - 5),
            main_sleep_average_hrv=round(hrv, 1),
            main_sleep_start=iso(bed),
            main_sleep_end=iso(wake),
            sleep_periods=1,
            daily_sources=[
                "daily_sleep",
                "daily_readiness",
                "daily_activity",
                "daily_stress",
                "daily_spo2",
            ],
        )
        messages.append(
            msg(
                "health.oura.daily",
                f"oura-daily-{day}",
                thread,
                midnight + timedelta(hours=12),
                f"Oura {day}: sleep {sleep_score}, readiness {readiness}, activity {activity}; {steps} steps, {hours:.1f} h sleep, HRV {hrv:.0f} ms, resting HR {rhr:.0f} bpm",
                daily,
                {"zoo": True, "day": str(day)},
                subject=day_subject(day),
            )
        )
        messages.append(
            msg(
                "health.oura.sleep",
                f"oura-sleep-{day}-1",
                thread,
                bed,
                f"Sleep {day}: {hours:.1f} h asleep of {in_bed / 3600:.1f} h in bed, efficiency {daily['main_sleep_efficiency']}%, lowest HR {daily['main_sleep_lowest_heart_rate']} bpm",
                dict(
                    provider="oura",
                    mapping_revision=4,
                    day=str(day),
                    sleep_type="long_sleep",
                    main_sleep=True,
                    total_sleep_s=total_s,
                    time_in_bed_s=int(in_bed),
                    deep_sleep_s=deep,
                    light_sleep_s=total_s - deep - rem,
                    rem_sleep_s=rem,
                    awake_s=int(in_bed) - total_s,
                    latency_s=rng.randrange(120, 1800),
                    efficiency=daily["main_sleep_efficiency"],
                    average_heart_rate=round(rhr + 3, 1),
                    lowest_heart_rate=daily["main_sleep_lowest_heart_rate"],
                    average_hrv=round(hrv, 1),
                    average_breath=round(rng.gauss(14.5, 0.6), 2),
                    restless_periods=rng.randrange(40, 320),
                    readiness_score=readiness,
                    temperature_deviation=temp,
                    sleep_period=0,
                ),
                {"zoo": True, "day": str(day)},
                ended_at=iso(wake),
            )
        )
        if rng.random() < 0.06:
            nap = midnight + timedelta(hours=rng.uniform(13.5, 16.5))
            nap_s = rng.randrange(1200, 4500)
            messages.append(
                msg(
                    "health.oura.sleep",
                    f"oura-sleep-{day}-2",
                    thread,
                    nap,
                    f"Nap {day}: {nap_s // 60} min",
                    dict(
                        provider="oura",
                        mapping_revision=4,
                        day=str(day),
                        sleep_type=rng.choice(["late_nap", "rest"]),
                        main_sleep=False,
                        total_sleep_s=nap_s,
                        time_in_bed_s=nap_s + 300,
                        deep_sleep_s=0,
                        light_sleep_s=nap_s,
                        rem_sleep_s=0,
                        awake_s=300,
                        latency_s=300,
                        efficiency=int(100 * nap_s / (nap_s + 300)),
                        average_heart_rate=round(rhr + 6, 1),
                        lowest_heart_rate=int(rhr),
                        average_hrv=round(hrv * 0.9, 1),
                        average_breath=15.0,
                        restless_periods=rng.randrange(2, 20),
                        sleep_period=1,
                    ),
                    {"zoo": True, "day": str(day)},
                    ended_at=iso(nap + timedelta(seconds=nap_s + 300)),
                )
            )
        samples = [
            int(
                clamp(
                    rhr
                    + 6
                    + 22 * max(0, math.sin((h - 7) * math.pi / 15))
                    + rng.gauss(0, 5),
                    38,
                    190,
                )
            )
            for h in range(24)
        ]
        messages.append(
            msg(
                "health.oura.heartrate",
                f"oura-hr-{day}",
                thread,
                midnight,
                f"Heart rate {day}: {min(samples)}-{max(samples)} bpm, mean {sum(samples) / 24:.0f}",
                dict(
                    provider="oura",
                    mapping_revision=4,
                    day=str(day),
                    sample_count=rng.randrange(180, 290),
                    bpm_min=min(samples),
                    bpm_max=max(samples),
                    bpm_mean=round(sum(samples) / 24, 1),
                    bpm_mean_awake=round(sum(samples[8:23]) / 15, 1),
                    bpm_mean_rest=round(rhr + 2, 1),
                    bpm_mean_sleep=round(sum(samples[:7]) / 7, 1),
                    bpm_mean_workout=round(rhr + 70 + rng.gauss(0, 8), 1)
                    if rng.random() < 0.3
                    else None,
                    first_sample_at=iso(midnight + timedelta(minutes=2)),
                    last_sample_at=iso(midnight + timedelta(hours=23, minutes=55)),
                ),
                {
                    "zoo": True,
                    "day": str(day),
                    "samples": [{"h": h, "bpm": b} for h, b in enumerate(samples)],
                },
                ended_at=iso(midnight + timedelta(hours=23, minutes=59, seconds=59)),
                subject=day_subject(day),
            )
        )
        slot = {1: 18, 3: 18, 5: 10}.get(day.weekday())
        plans = []
        if slot is not None and not ill and t not in travel and rng.random() < 0.75:
            plans.append(
                (
                    slot,
                    rng.choice(
                        ["strength_training", "strength_training", "circuit", "yoga"]
                    ),
                    "manual",
                )
            )
        if rng.random() < (0.5 if t in travel else 0.12) and not ill:
            plans.append(
                (
                    rng.choice([8, 12, 16]),
                    rng.choice(["walking", "running", "cycling", "hiking", "swimming"]),
                    "autodetected",
                )
            )
        for n, (hour, act, source) in enumerate(plans):
            at = midnight + timedelta(hours=hour, minutes=rng.randrange(0, 12))
            dur = rng.randrange(1500, 5400)
            dist = (
                round(
                    dur
                    * {
                        "walking": 1.3,
                        "running": 2.8,
                        "cycling": 5.5,
                        "hiking": 1.1,
                        "swimming": 0.7,
                    }.get(act, 0)
                    * rng.uniform(0.85, 1.15),
                    1,
                )
                or None
            )
            if at + timedelta(seconds=dur) > ANCHOR:
                continue
            messages.append(
                msg(
                    "health.oura.workout",
                    f"oura-workout-{day}-{n}",
                    thread,
                    at,
                    f"{act.replace('_', ' ').title()} for {dur // 60} min"
                    + (f", {dist / 1000:.1f} km" if dist else ""),
                    dict(
                        provider="oura",
                        mapping_revision=4,
                        day=str(day),
                        activity=act,
                        intensity=rng.choice(["easy", "moderate", "moderate", "hard"]),
                        calories=round(dur / 60 * rng.uniform(5, 11), 1),
                        distance_m=dist,
                        duration_s=dur,
                        label="Thistle circuit" if act == "circuit" else None,
                        workout_source=source,
                    ),
                    {"zoo": True, "day": str(day)},
                    ended_at=iso(at + timedelta(seconds=dur)),
                )
            )
    t, n, horizon = rng.uniform(0, 4), 0, n_of(560, scale, 30)
    quiet = [(horizon * 0.3, horizon * 0.3 + 38), (horizon * 0.7, horizon * 0.7 + 24)]
    while t < horizon:
        if not any(a <= t < b for a, b in quiet):
            at = (ANCHOR - timedelta(days=horizon - t)).replace(
                hour=7, minute=rng.randrange(60), second=rng.randrange(60)
            )
            kg = (
                81.5
                - 5.5 * (1 - math.exp(-t / 200))
                + 0.3 * math.sin(2 * math.pi * t / 7)
                + rng.gauss(0, 0.35)
            )
            typo = n == 11
            if typo:
                kg = 8.1
            fat = clamp(24 - 3.5 * (1 - math.exp(-t / 200)) + rng.gauss(0, 0.5), 5, 50)
            messages.append(
                msg(
                    "health.weight",
                    f"wg-{n:05d}",
                    {
                        "external_id": f"weight-{at:%Y-%m}",
                        "title": f"Weight {at:%B %Y}",
                    },
                    at,
                    f"Weight {kg * 2.20462:.1f} lb ({kg:.1f} kg), body fat {fat:.1f}%"
                    + (" (deleted)" if typo else ""),
                    dict(
                        provider="weight_gurus",
                        mapping_revision=2,
                        weight=round(kg * 2.20462, 1),
                        weight_unit="lb",
                        weight_kg=round(kg, 2),
                        bmi=round(kg / 1.78**2, 1),
                        body_fat_pct=round(fat, 1),
                        muscle_mass_pct=round(100 - fat - 22 + rng.gauss(0, 0.4), 1),
                        water_pct=round(clamp(rng.gauss(55, 1.2), 40, 70), 1),
                        deleted=typo,
                        scale_source="bluetooth_scale",
                        server_timestamp=iso(
                            at + timedelta(seconds=rng.randrange(2, 90))
                        ),
                        operation_count=2 if typo else 1,
                    ),
                    {"zoo": True, "entry": n},
                )
            )
            n += 1
        t += rng.expovariate(1 / 5) + 0.6
    return messages, gap_window


# --- location ---------------------------------------------------------------------


def _day_plan(rng, day, trips):
    """Stays of one local day: [(start minute, end minute, place key)]."""
    offset = (ANCHOR.date() - day).days
    j = lambda spread=10: rng.randrange(-spread, spread + 1)  # noqa: E731
    trip = trips.get(offset)
    if trip == "porto_out":
        return [
            (0, 495 + j(), "home"),
            (555, 960, "work"),
            (985, 1010, "lis_station"),
            (1190, 1200, "porto_station"),
            (1220, 1440, "porto_stay"),
        ]
    if trip == "porto_day":
        return [
            (0, 570 + j(), "porto_stay"),
            (590, 650, "porto_cafe"),
            (670, 780 + j(), "porto_sight"),
            (800, 900, "porto_cafe"),
            (1140, 1440, "porto_stay"),
        ]
    if trip == "porto_back":
        return [
            (0, 600 + j(), "porto_stay"),
            (620, 690, "porto_cafe"),
            (900, 920, "porto_station"),
            (1100, 1110, "lis_station"),
            (1125, 1440, "home"),
        ]
    if trip == "mad_out":
        return [
            (0, 360, "home"),
            (400, 490, "lis_airport"),
            (635, 660, "mad_airport"),
            (710, 780, "mad_stay"),
            (810, 960, "mad_museum"),
            (1020, 1200, "mad_stay"),
            (1230, 1410, "mad_bar"),
            (1430, 1440, "mad_stay"),
        ]
    if trip == "mad_day":
        return [
            (0, 570 + j(), "mad_stay"),
            (600, 750, "mad_park"),
            (780, 870, "mad_bar"),
            (900, 1020 + j(), "mad_museum"),
            (1080, 1440, "mad_stay"),
        ]
    if trip == "mad_back":
        return [
            (0, 660, "mad_stay"),
            (720, 820, "mad_airport"),
            (900, 925, "lis_airport"),
            (970, 1440, "home"),
        ]
    weekday = day.weekday()
    if weekday == 5:
        return [
            (0, 580 + j(), "home"),
            (600, 675, "gym"),
            (695, 750 + j(), "cafe"),
            (780, 930 + j(20), "home"),
            (960, 1080 + j(20), "park"),
            (1110, 1440, "home"),
        ]
    if weekday == 6:
        return [
            (0, 660 + j(20), "home"),
            (680, 750, "viewpoint"),
            (765, 840 + j(), "market"),
            (870, 1440, "home"),
        ]
    plan = [(0, 495 + j(), "home"), (515, 540, "cafe"), (555, 760 + j(), "work")]
    if rng.random() < 0.5:
        plan += [(775, 815, "market"), (830, 1065 + j(), "work")]
    else:
        plan[-1] = (555, 1065 + j(), "work")
    if weekday in (1, 3):
        plan += [(1085, 1160, "gym"), (1185, 1440, "home")]
    elif weekday == 4:
        plan += [
            (1090, 1180, "home"),
            (1200, 1390 + j(), "friend"),
            (1415, 1440, "home"),
        ]
    else:
        plan += [(1090, 1440, "home")]
    return plan


def make_location(seed, scale):
    rng = random.Random(f"{seed}:location")
    days = n_of(21, scale, 3)
    trips = {
        14: "porto_out",
        13: "porto_day",
        12: "porto_back",
        8: "mad_out",
        7: "mad_day",
        6: "mad_day",
        5: "mad_back",
    }
    trips = {k: v for k, v in trips.items() if days >= 21}
    stays = []
    for back in range(days, -1, -1):
        day = ANCHOR.date() - timedelta(days=back)
        midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
        for a, b, place in _day_plan(rng, day, trips):
            start, end = (
                midnight + timedelta(minutes=a),
                midnight + timedelta(minutes=b),
            )
            if start >= ANCHOR:
                continue
            end = min(end, ANCHOR)
            if (
                stays
                and stays[-1][2] == place
                and (start - stays[-1][1]) < timedelta(minutes=2)
            ):
                stays[-1] = (stays[-1][0], end, place)
            else:
                stays.append((start, end, place))
    messages, track, row = [], [], 0

    def point(at, lat, lon, device, place=None):
        nonlocal row
        glitch = rng.random() < 0.004
        spread = 0.002 if glitch else 0.00012
        lat, lon = (
            round(lat + rng.gauss(0, spread), 6),
            round(lon + rng.gauss(0, spread), 6),
        )
        accuracy = round(rng.uniform(180, 400) if glitch else rng.uniform(5, 38), 1)
        row += 1
        fields = dict(
            record_type="position",
            source_table="positions",
            row_id=str(row),
            latitude=lat,
            longitude=lon,
            accuracy_meters=accuracy,
            place_name=PLACES[place][0] if place and rng.random() < 0.4 else None,
            device=device,
            time_basis="device_time",
            columns={
                "speed_mps": round(
                    abs(rng.gauss(0, 0.2)) if place else rng.uniform(1, 9), 2
                ),
                "battery": rng.randrange(8, 100),
            },
            mapping_revision=2,
        )
        messages.append(
            msg(
                "location.timeline",
                f"positions:{row}",
                {
                    "external_id": f"{at:%Y-%m-%d}:{device}",
                    "title": f"{at:%Y-%m-%d} {device}",
                },
                at,
                f"Position {lat:.5f}, {lon:.5f} (±{accuracy:.0f} m)"
                + (f" near {fields['place_name']}" if fields["place_name"] else ""),
                fields,
                {"zoo": True, "row": row},
                location=dict(lat=lat, lon=lon, accuracy_m=accuracy),
            )
        )
        track.append(
            dict(
                at=iso(at),
                lat=lat,
                lon=lon,
                accuracy_m=accuracy,
                source="morning_context",
            )
        )

    previous = None
    for n, (start, end, place) in enumerate(stays):
        name, address, lat, lon = PLACES[place]
        if previous:
            p_end, p_place = previous
            _, _, lat0, lon0 = PLACES[p_place]
            far = abs(lat0 - lat) + abs(lon0 - lon)
            gap_s = (start - p_end).total_seconds()
            flying = "airport" in place and "airport" in p_place
            if gap_s > 120 and not flying:
                step = 480 if far > 1 else 180
                bend = rng.gauss(0, 0.0015) if far < 1 else rng.gauss(0, 0.05)
                t = step * rng.uniform(0.3, 0.9)
                while t < gap_s:
                    f = t / gap_s
                    arc = math.sin(math.pi * f) * bend
                    point(
                        p_end + timedelta(seconds=t),
                        lat0 + (lat - lat0) * f + arc,
                        lon0 + (lon - lon0) * f - arc,
                        "pebble-phone",
                    )
                    t += step * rng.uniform(0.7, 1.3)
        device = "pebble-phone"
        at = start + timedelta(seconds=rng.uniform(20, 240))
        while at < end:
            point(
                at,
                lat,
                lon,
                "moss-tablet" if place == "home" and rng.random() < 0.12 else device,
                place,
            )
            at += timedelta(minutes=rng.uniform(7, 16))
        duration = (end - start).total_seconds()
        if duration >= 600:
            messages.append(
                msg(
                    "location.timeline",
                    f"stays:{n}",
                    {
                        "external_id": f"{start:%Y-%m-%d}:{device}",
                        "title": f"{start:%Y-%m-%d} {device}",
                    },
                    start,
                    f"Stay at {name} for {duration / 60:.0f} min",
                    dict(
                        record_type="stay",
                        source_table="stays",
                        row_id=str(n),
                        latitude=lat,
                        longitude=lon,
                        accuracy_meters=25.0,
                        place_name=name,
                        address=address,
                        device=device,
                        duration_s=duration,
                        time_basis="computed",
                        columns={"confidence": round(rng.uniform(0.6, 0.99), 2)},
                        mapping_revision=2,
                    ),
                    {"zoo": True, "stay": n},
                    ended_at=iso(end),
                    location=dict(lat=lat, lon=lon, accuracy_m=25.0),
                )
            )
        if place not in ("home", "work") and rng.random() < 0.3:
            at = start + timedelta(seconds=duration * rng.uniform(0.2, 0.8))
            messages.append(
                msg(
                    "location.timeline",
                    f"captures:{n}",
                    {
                        "external_id": f"{at:%Y-%m-%d}:{device}",
                        "title": f"{at:%Y-%m-%d} {device}",
                    },
                    at,
                    f"Photo capture at {name}",
                    dict(
                        record_type="capture",
                        source_table="captures",
                        row_id=str(n),
                        latitude=lat,
                        longitude=lon,
                        accuracy_meters=12.0,
                        place_name=name,
                        address=address,
                        device=device,
                        time_basis="exif",
                        columns={"camera": "pebble-phone rear"},
                        mapping_revision=2,
                    ),
                    {"zoo": True, "capture": n},
                    location=dict(lat=lat, lon=lon, accuracy_m=12.0),
                    images=[placeholder_image(rng, f"Photo at {name}")],
                )
            )
        previous = (end, place)
    for n, (k, (name, address, lat, lon)) in enumerate(PLACES.items()):
        at = ANCHOR - timedelta(days=min(days, 17), hours=n * 5)
        messages.append(
            msg(
                "location.places",
                f"place_cache:{n}",
                {"external_id": "place-cache", "title": "Place lookups"},
                at,
                f"{name}\n{address}",
                dict(
                    record_type="place_lookup",
                    source_table="place_cache",
                    row_id=str(n),
                    latitude=lat,
                    longitude=lon,
                    place_name=name,
                    address=address,
                    time_basis="lookup_time",
                    columns={"provider": "zoo-geocoder", "category": k.split("_")[-1]},
                    mapping_revision=2,
                    query=f"{lat:.4f},{lon:.4f}",
                ),
                {"zoo": True, "place": k},
            )
        )
    for n in range(n_of(45, scale, 3)):
        lat, lon = 38.72 + rng.gauss(0, 0.02), -9.14 + rng.gauss(0, 0.025)
        name = f"{rng.choice(['Café', 'Loja', 'Jardim', 'Bar', 'Oficina'])} {rng.choice(THINGS).title()}"
        at = ANCHOR - timedelta(days=rng.uniform(0, min(days, 17)))
        messages.append(
            msg(
                "location.places",
                f"place_search:{n}",
                {"external_id": "place-cache", "title": "Place lookups"},
                at,
                f"{name}\nRua Inventada {n + 1}, Lisboa",
                dict(
                    record_type="place_search",
                    source_table="place_search",
                    row_id=str(n),
                    latitude=round(lat, 6),
                    longitude=round(lon, 6),
                    place_name=name,
                    address=f"Rua Inventada {n + 1}, Lisboa",
                    time_basis="lookup_time",
                    columns={"provider": "zoo-geocoder", "rank": n % 5 + 1},
                    mapping_revision=2,
                    query=name.lower(),
                ),
                {"zoo": True, "search": n},
            )
        )
    return messages, track


# --- feeds -------------------------------------------------------------------------


def make_youtube(seed, scale):
    rng = random.Random(f"{seed}:youtube")
    messages, links, prov = [], [], []
    channels = []
    for i in range(n_of(60, scale, 5)):
        title = f"{rng.choice(THINGS).title()} {rng.choice(['Workshop', 'Diaries', 'Lab', 'Explained', 'Radio', 'Weekly', 'Garage'])}"
        channels.append(
            dict(
                id="UCzoo" + hexid(rng, 72),
                title=title,
                weight=min(25, rng.paretovariate(1.0)),
                hour=rng.randrange(8, 22),
                shorts=rng.random() < 0.25,
            )
        )
    titles = [
        "I built a {a} out of a {b}",
        "{a} review after six months",
        "Why every {a} needs a {b}",
        "Restoring a rusty {a}",
        "The {a} problem nobody talks about",
        "{a} vs {b}: which one hums louder?",
        "A quiet afternoon with the {a}",
        "Live: fixing the {b}",
    ]
    uploads = []
    for i in range(n_of(4000, scale, 40)):
        ch = rng.choices(channels, [c["weight"] for c in channels])[0]
        age = min(
            rng.expovariate(1 / 200) if rng.random() < 0.7 else rng.uniform(0, 3700),
            3700,
        )
        at = (ANCHOR - timedelta(days=age)).replace(
            hour=(ch["hour"] + rng.randrange(-1, 2)) % 24,
            minute=rng.choice([0, 0, 15, 30, 45]),
            second=rng.randrange(60),
        )
        if at > ANCHOR:
            at -= timedelta(days=1)
        vid = "zoo" + hexid(rng, 32)
        short = ch["shorts"] and rng.random() < 0.6
        live = not short and rng.random() < 0.03
        duration = (
            rng.randrange(12, 60)
            if short
            else int(clamp(rng.lognormvariate(6.6, 0.8), 70, 14400))
        )
        title = (
            rng.choice(titles)
            .format(a=rng.choice(THINGS), b=rng.choice(THINGS))
            .capitalize()
        )
        url = f"https://www.youtube.example/watch?v={vid}"
        chapters = "\n".join(
            f"{m:02d}:00 {rng.choice(THINGS).title()}"
            for m in range(0, max(1, duration // 60), max(1, duration // 300))[:8]
        )
        description = f"{prose(rng, rng.randrange(2, 6))}\n\nChapters:\n{chapters}\n\nSupport: https://{slug(ch['title'])}.example/club"
        fields = dict(
            mapping_revision=2,
            video_id=vid,
            url=url,
            title=title,
            youtube_channel_id=ch["id"],
            channel_title=ch["title"],
            duration_s=duration,
            description=description,
            is_short=short,
            is_live=live,
            transport="rss",
            time_precision="exact",
        )
        messages.append(
            msg(
                "youtube.uploads",
                vid,
                {"external_id": ch["id"], "title": ch["title"]},
                at,
                f"{title}\n\n{description}",
                fields,
                {"zoo": True, "id": vid, "channel": ch["id"]},
            )
        )
        uploads.append((at, fields))
    for ch in channels[:12]:
        first = next(
            (f for _, f in uploads if f["youtube_channel_id"] == ch["id"]), None
        )
        if first:
            links.append(
                link(
                    "published_by",
                    key("youtube.uploads", first["video_id"]),
                    {
                        "kind": "organization",
                        "external_ref": "zoo-yt-" + ch["id"],
                        "name": ch["title"],
                    },
                )
            )
    for at, f in uploads:
        if at < ANCHOR - timedelta(days=45):
            continue
        scored = min(ANCHOR, at + timedelta(minutes=rng.uniform(5, 90)))
        watch = round(clamp(rng.betavariate(1.2, 2.5), 0, 1), 3)
        junk = round(
            clamp(rng.betavariate(1.2, 4) + (0.3 if f["is_short"] else 0), 0, 1), 3
        )
        later = round(clamp(rng.betavariate(1.1, 5), 0, 1), 3)
        verdict = (
            "junk"
            if junk > 0.6
            else "pick"
            if watch > 0.55
            else "watch_later"
            if later > 0.4
            else "skip"
        )
        picked = verdict == "pick"
        reason = {
            "pick": "Matches recent interest in the {a}; channel is watched to the end.",
            "junk": "Short bait-style upload about the {a}; low completion expected.",
            "watch_later": "Long-form piece on the {a}; better for the weekend.",
            "skip": "Topic ({a}) rarely watched from this channel.",
        }[verdict].format(a=rng.choice(THINGS))
        eid = "pick-" + f["video_id"]
        messages.append(
            msg(
                "youtube.picks",
                eid,
                {"external_id": "router", "title": "Feed router"},
                scored,
                f"{verdict.upper()} {f['title']} ({f['channel_title']}): {reason}",
                dict(
                    mapping_revision=2,
                    video_id=f["video_id"],
                    url=f["url"],
                    title=f["title"],
                    youtube_channel_id=f["youtube_channel_id"],
                    channel_title=f["channel_title"],
                    duration_s=f["duration_s"],
                    verdict=verdict,
                    picked=picked,
                    watch_probability=watch,
                    progress_if_watched=round(rng.uniform(0.2, 1), 3),
                    junk_food_probability=junk,
                    watch_later_probability=later,
                    is_junk_food=junk > 0.6,
                    reason=reason,
                    scorer="zoo-router-v3",
                    origin="feed",
                    scored_at=iso(scored),
                    thresholds={"pick": 0.55, "junk": 0.6, "watch_later": 0.4},
                    playlist_id="PLzoo-picks" if picked else None,
                    playlist_added_at=iso(scored + timedelta(seconds=30))
                    if picked
                    else None,
                ),
                {"zoo": True, "video": f["video_id"]},
                subject=dict(start=iso(at)),
            )
        )
        prov.append(
            provenance(
                key("youtube.picks", eid), [key("youtube.uploads", f["video_id"])]
            ).data
        )
        if picked:
            links.append(
                link(
                    "pick_of",
                    key("youtube.picks", eid),
                    key("youtube.uploads", f["video_id"]),
                )
            )
    return messages, links, prov, [f["video_id"] for _, f in uploads]


# --- research ------------------------------------------------------------------------


def make_research(seed, scale, video_ids, web_pages):
    rng = random.Random(f"{seed}:research")
    messages, links, prov = [], [], []
    streams = [
        ("lisbon-events", "Lisbon events worth leaving the house for", "events"),
        ("porto-events", "Porto weekend events", "events"),
        ("kite-tech", "Kite technology news", "news"),
        ("moss-papers", "Moss lantern research papers", "papers"),
        ("pebble-radio", "Pebble radio hobby scene", "news"),
        ("fern-markets", "Fern wheel second-hand market", "listings"),
        ("comet-festivals", "Paper comet festivals", "events"),
        ("soup-violin", "Soup violin releases", "news"),
    ]
    streams = streams[: n_of(len(streams), scale * 4, 2)]
    runs = n_of(300, scale, 8)
    earlier = []
    for r in range(runs):
        sid, stitle, kind = streams[r % len(streams)]
        search = f"{sid}-s{rng.randrange(1, 4)}"
        run = (ANCHOR - timedelta(days=rng.uniform(0, 120))).replace(
            hour=5, minute=rng.randrange(60), second=rng.randrange(60)
        )
        rid = f"{search}:{run:%Y-%m-%d}"
        if any(rid == e for e in earlier):
            continue
        status = rng.choices(["ok", "empty", "error"], [90, 6, 4])[0]
        urls = (
            [
                f"https://{slug(rng.choice(ORG_NAMES))}.example/{kind}/{hexid(rng, 24)}"
                for _ in range(rng.randrange(2, 9))
            ]
            if status == "ok"
            else []
        )
        n_items = (
            rng.randrange(3, 11)
            if kind in ("events", "listings") and status == "ok"
            else 0
        )
        findings = "\n".join(
            f"- **{rng.choice(THINGS).title()}**: {line(rng, PROSE)} [{i + 1}]"
            for i in range(len(urls))
        )
        body = (
            f"# {stitle}: {run:%Y-%m-%d}\n\n## Summary\n\n{prose(rng, rng.randrange(3, 7))}\n\n## Findings\n\n{findings}\n\n## Sources\n\n"
            + "\n".join(f"{i + 1}. {u}" for i, u in enumerate(urls))
            if status == "ok"
            else f"# {stitle}: {run:%Y-%m-%d}\n\nNo findings ({status})."
        )
        prompt = f"Find {kind} about {stitle.lower()} between {run - timedelta(days=7):%Y-%m-%d} and {run + timedelta(days=21):%Y-%m-%d}."
        messages.append(
            msg(
                "research.results",
                rid,
                {"external_id": search, "title": f"{stitle} ({search})"},
                run,
                body,
                dict(
                    stream=sid,
                    stream_title=stitle,
                    search_id=search,
                    search_label=f"{stitle} / {search[-2:]}",
                    cluster=kind,
                    variant=rng.choice(["broad", "narrow"]),
                    search_kind=kind,
                    run_date=f"{run:%Y-%m-%d}",
                    window_start=f"{run - timedelta(days=7):%Y-%m-%d}",
                    window_end=f"{run + timedelta(days=21):%Y-%m-%d}",
                    provider="zoo-search",
                    model="lumen-large-2",
                    import_path="streams/" + sid,
                    prompt=prompt,
                    prompt_hash=hashlib.sha256(prompt.encode()).hexdigest()[:16],
                    status=status,
                    cited_urls=urls,
                    citations=[
                        {
                            "index": i + 1,
                            "url": u,
                            "title": f"Notes on the {rng.choice(THINGS)}",
                        }
                        for i, u in enumerate(urls)
                    ],
                    citation_count=len(urls),
                    item_count=n_items,
                    prompt_tokens=rng.randrange(300, 900),
                    completion_tokens=rng.randrange(400, 3000),
                    stream_revision="r" + str(rng.randrange(1, 5)),
                    mapping_revision=3,
                ),
                {"zoo": True, "result": rid},
                subject=dict(
                    start=iso(run - timedelta(days=7)),
                    end=iso(run + timedelta(days=21)),
                    start_precision="day",
                    end_precision="day",
                ),
            )
        )
        sources = [
            key("youtube.uploads", v)
            for v in rng.sample(video_ids, min(len(video_ids), rng.randrange(0, 4)))
        ]
        if earlier and rng.random() < 0.5:
            sources.append(key("research.results", rng.choice(earlier)))
        if sources and status == "ok":
            prov.append(provenance(key("research.results", rid), sources, "cites").data)
        if status == "ok" and rng.random() < 0.5:
            for ref in rng.sample(web_pages, 2):
                links.append(
                    link("cites", key("research.results", rid), ent("web_page", ref))
                )
        earlier.append(rid)
        for pos in range(n_items):
            when = (run + timedelta(days=rng.uniform(-3, 40))).replace(
                hour=rng.choice([11, 15, 18, 19, 20, 21]),
                minute=rng.choice([0, 30]),
                second=0,
            )
            title = f"{rng.choice(['Night of the', 'Festival of the', 'Workshop: the', 'Open mic at the', 'Market of the'])} {rng.choice(THINGS).title()}"
            venue = rng.choice(list(PLACES.values()))
            iid = f"{rid}:{pos}"
            certain = rng.random() < 0.8
            messages.append(
                msg(
                    "research.items",
                    iid,
                    {"external_id": sid, "title": stitle},
                    run + timedelta(seconds=pos),
                    f"{title}\n{when:%a %d %b %Y %H:%M} at {venue[0]}\n{line(rng, PROSE)}",
                    dict(
                        stream=sid,
                        search_id=search,
                        result_id=rid,
                        run_date=f"{run:%Y-%m-%d}",
                        item_type="event",
                        item_key=slug(title) + f"-{when:%Y%m%d}",
                        position=pos,
                        title=title,
                        description=prose(rng, 2),
                        venue=venue[0],
                        address=venue[1],
                        city=venue[1].split(", ")[-1],
                        organizer=rng.choice(ORG_NAMES),
                        door_time=f"{when - timedelta(minutes=30):%H:%M}",
                        price=rng.choice(
                            ["free", "5 credits", "12 credits", "pay what you want"]
                        ),
                        audience=rng.choice(["all ages", "adults", "families"]),
                        source_tier=rng.choice(["primary", "aggregator", "social"]),
                        date_certainty="confirmed" if certain else "tentative",
                        notes=None
                        if certain
                        else "Date taken from an aggregator listing.",
                        canonical_url=rng.choice(urls),
                        evidence_url=rng.choice(urls),
                        ticket_url=f"https://tickets.example/{hexid(rng, 24)}"
                        if rng.random() < 0.4
                        else None,
                        primary_confirmed=certain,
                        occurrence_start=iso(when),
                        occurrence_end=iso(
                            when + timedelta(hours=rng.choice([2, 3, 4]))
                        ),
                        date_precision="exact" if certain else "day",
                        details={"tags": rng.sample(THINGS, 2)},
                        mapping_revision=3,
                    ),
                    {"zoo": True, "item": iid},
                    subject=dict(
                        start=iso(when),
                        end=iso(when + timedelta(hours=3)),
                        start_precision="exact" if certain else "day",
                        end_precision="exact" if certain else "day",
                    ),
                )
            )
            prov.append(
                provenance(
                    key("research.items", iid), [key("research.results", rid)]
                ).data
            )
    return messages, links, prov


# --- wiki ---------------------------------------------------------------------------------


def make_wiki(seed, scale, people, entities):
    rng = random.Random(f"{seed}:wiki")
    pages = []

    def page(kind, title, about=None, person=None):
        pages.append(
            dict(
                kind=kind,
                title=title,
                slug=f"{kind}/{slug(title)}",
                about=about,
                person=person,
                out=[],
            )
        )

    for p in people[: n_of(40, scale, 5)]:
        page("people", p["display_name"], person=p["handle"])
    by_kind = defaultdict(list)
    for e in entities:
        by_kind[e["kind"]].append(e)
    for e in by_kind["organization"][: n_of(12, scale, 2)]:
        page("companies", e["name"], about=e)
    for e in by_kind["place"][: n_of(15, scale, 3)]:
        page("places", e["name"], about=e)
    for e in by_kind["work"][: n_of(12, scale, 2)]:
        page("media", e["name"], about=e)
    for e in by_kind["event"][: n_of(8, scale, 1)]:
        page("events", e["name"], about=e)
    for t in THINGS[: n_of(28, scale, 4)]:
        page("concepts", t.title())
    for i in range(n_of(20, scale, 2)):
        page("projects", f"Project {THINGS[-1 - i].title()}")
    for i in range(n_of(60, scale, 3)):
        page("daily", f"{(ANCHOR - timedelta(days=i * 9 + 1)):%Y-%m-%d}")
    hubs = ["People", "Projects", "Places", "Concepts"]
    for h in hubs:
        page("index", h + " index")
    by_slug = {p["slug"]: p for p in pages}
    popularity = [1.0] * len(pages)
    for i, p in enumerate(pages):
        if p["kind"] == "index":
            p["out"] = [
                q["slug"] for q in pages if q["kind"] == p["title"].split()[0].lower()
            ][:60]
            continue
        want = min(len(pages) - 1, rng.randrange(2, 7))
        while len(p["out"]) < want:
            q = rng.choices(range(len(pages)), popularity)[0]
            if (
                q != i
                and pages[q]["slug"] not in p["out"]
                and pages[q]["kind"] != "index"
            ):
                p["out"].append(pages[q]["slug"])
                popularity[q] += 1.5
    messages, revisions, links, annotations = [], [], [], []
    web = by_kind["web_page"]
    for i, p in enumerate(pages):
        created = ANCHOR - timedelta(days=rng.uniform(20, 1950))
        updated = min(ANCHOR, created + timedelta(days=rng.uniform(0, 900)))
        eid = "default:" + p["slug"]

        def body(revision):
            r = random.Random(f"{seed}:wiki:{p['slug']}:{revision}")
            wl = lambda s: f"[[{s}|{by_slug[s]['title']}]]"  # noqa: E731
            sections = [f"# {p['title']}", f"> {line(r, PROSE)}"]
            heads = {
                "people": [
                    "Overview",
                    "How we met",
                    "Things to remember",
                    "Open questions",
                ],
                "projects": ["Goal", "Status", "Decisions", "Next steps"],
                "daily": ["Morning", "Afternoon", "Evening"],
                "index": ["Pages"],
            }.get(p["kind"], ["Overview", "Details", "History", "Open questions"])
            targets = list(p["out"])
            long = rng.random() < 0.06 and revision == "final"
            for h in heads[: len(heads) if revision == "final" else 2]:
                sections.append(f"## {h}")
                if p["kind"] == "index":
                    sections.append("\n".join(f"- {wl(s)}" for s in targets))
                    continue
                for _ in range(r.randrange(1, 4) * (6 if long else 1)):
                    para = prose(r, r.randrange(2, 6))
                    if targets and r.random() < 0.8:
                        para += f" See {wl(targets[r.randrange(len(targets))])}."
                    if web and r.random() < 0.25:
                        e = r.choice(web)
                        para += f" ([{e['name']}]({e['external_ref']}))"
                    sections.append(para)
                if r.random() < 0.4:
                    sections.append(
                        "\n".join(
                            f"- {line(r, PROSE)}" for _ in range(r.randrange(2, 6))
                        )
                    )
                if r.random() < 0.15:
                    sections.append(
                        "### Table\n\n| Item | Count |\n|---|---|\n"
                        + "\n".join(
                            f"| {r.choice(THINGS)} | {r.randrange(1, 40)} |"
                            for _ in range(3)
                        )
                    )
            if p["kind"] != "index":
                sections.append(
                    "## See also\n\n" + "\n".join(f"- {wl(s)}" for s in targets)
                )
            return "\n\n".join(sections)

        def record(revision, at):
            text = body(revision)
            tags = sorted(
                {
                    p["kind"],
                    *rng.sample(
                        ["zoo", "draft", "evergreen", "todo", "favourite"],
                        rng.randrange(0, 3),
                    ),
                }
            )
            return msg(
                "gbrain.pages",
                eid,
                {"external_id": eid, "title": p["title"]},
                at,
                text,
                dict(
                    source_hash=hashlib.sha256(text.encode()).hexdigest()[:24],
                    slug=p["slug"],
                    source_id="default",
                    page_type=p["kind"].rstrip("s")
                    if p["kind"] != "companies"
                    else "company",
                    page_kind="index" if p["kind"] == "index" else "note",
                    title=p["title"],
                    tags=tags,
                    aliases=[p["title"].split()[0]] if p["kind"] == "people" else [],
                    frontmatter={"title": p["title"], "type": p["kind"], "tags": tags},
                    updated_at=iso(at),
                    deleted_at=iso(at)
                    if i in (7, 23) and revision == "final"
                    else None,
                    source_uri=f"gbrain://default/{p['slug']}",
                ),
                {
                    "zoo": True,
                    "slug": p["slug"],
                    "revision": revision,
                    "compiled_truth": text,
                },
            )

        if rng.random() < 0.25:
            revisions.append(dict(record("first", created), expected_version=0))
        messages.append(record("final", updated))
        for s in p["out"]:
            links.append(
                link(
                    "mentions" if p["kind"] != "index" else "related_to",
                    key("gbrain.pages", eid),
                    key("gbrain.pages", "default:" + s),
                )
            )
        if p["about"]:
            links.append(
                link(
                    "about",
                    key("gbrain.pages", eid),
                    ent(p["about"]["kind"], p["about"]["external_ref"]),
                )
            )
        if p["person"]:
            links.append(
                link("about", key("gbrain.pages", eid), {"person": p["person"]})
            )
        if p["kind"] in ("people", "projects", "places", "events"):
            for n in range(rng.randrange(1, 6)):
                at = ANCHOR - timedelta(days=rng.uniform(1, 2500))
                at = at.replace(hour=12, minute=0, second=0, microsecond=0)
                tid = f"{eid}#timeline-{n}"
                text = f"{at:%Y-%m-%d}: {line(rng, PROSE)} {line(rng, STATEMENTS)}"
                messages.append(
                    msg(
                        "gbrain.timeline",
                        tid,
                        {"external_id": eid, "title": p["title"]},
                        at,
                        text,
                        dict(
                            source_hash=hashlib.sha256(text.encode()).hexdigest()[:24],
                            source="timeline",
                            page_external_id=eid,
                        ),
                        {"zoo": True, "page": eid, "entry": n},
                        subject=day_subject(at.date()),
                    )
                )
                links.append(
                    link(
                        "part_of", key("gbrain.timeline", tid), key("gbrain.pages", eid)
                    )
                )
    return messages, revisions, links, annotations, [p for p in pages]


# --- AI coding ------------------------------------------------------------------------------


def make_t3code(seed, scale):
    rng = random.Random(f"{seed}:t3code")
    messages, links = [], []
    envs = [("env-pebble", "pebble-laptop"), ("env-burrow", "cloud-burrow")]
    projects = []
    for i in range(n_of(12, scale, 2)):
        name = slug(THINGS[i * 2 % len(THINGS)]) + rng.choice(
            ["-web", "-firmware", "-api", "-tools", ""]
        )
        projects.append(
            dict(
                id="proj-" + hexid(rng, 32),
                name=name,
                env=envs[i % 2],
                weight=min(20, rng.paretovariate(1.0)),
                threads=0,
                last=None,
            )
        )
    models = [
        ("claude", "fable-5"),
        ("claude", "opal-4.8"),
        ("codex", "sol-5.6"),
        ("codex", "terra-5.6"),
    ]
    asks = [
        "Add a {a} endpoint and tests.",
        "Why does the {a} test flake on CI?",
        "Refactor the {b} loader, keep behaviour.",
        "Review the {a} diff for races.",
        "Write a migration for the {a} table.",
        "Profile the {b} importer and make it faster.",
    ]
    threads = []
    span_days = 137
    busy = {rng.randrange(span_days) for _ in range(12)}
    for i in range(n_of(500, scale, 6)):
        proj = rng.choices(projects, [p["weight"] for p in projects])[0]
        d = (
            rng.choice(sorted(busy))
            if rng.random() < 0.35
            else rng.uniform(0, span_days)
        )
        start = (ANCHOR - timedelta(days=int(d) + 1)).replace(
            hour=rng.choices(HOURS, DIURNAL)[0],
            minute=rng.randrange(60),
            second=rng.randrange(60),
        )
        provider, model = rng.choice(models)
        sub = bool(threads) and rng.random() < 0.35
        parent = rng.choice(threads[-30:]) if sub else None
        if parent:
            proj, start = (
                parent["proj"],
                parent["start"] + timedelta(minutes=rng.uniform(1, 30)),
            )
        tid = "t3-" + hexid(rng, 48)
        title = line(rng, asks).rstrip(".")
        th = dict(
            id=tid,
            proj=proj,
            start=start,
            title=title,
            provider=provider,
            model=model,
            parent=parent,
            root=(parent["root"] if parent else tid),
        )
        threads.append(th)
        proj["threads"] += 1
        at, last = start, start
        for m in range(max(2, int(rng.lognormvariate(2.5, 0.8)))):
            user = m % 2 == 0
            if user:
                text = line(rng, asks) + (
                    " " + line(rng, PROSE) if rng.random() < 0.5 else ""
                )
                kind = "agent" if parent and m == 0 else "owner"
            else:
                code = f"```python\ndef {slug(rng.choice(THINGS)).replace('-', '_')}(items):\n    return [item for item in items if item.{rng.choice(['ready', 'valid', 'fresh'])}]\n```"
                text = (
                    f"{prose(rng, rng.randrange(3, 12))}\n\n{code}\n\n{prose(rng, rng.randrange(1, 6))}\n\n"
                    + "\n".join(
                        f"- {line(rng, PROSE)}" for _ in range(rng.randrange(0, 4))
                    )
                )
                kind = "agent"
            at += timedelta(seconds=rng.expovariate(1 / 240) + 5)
            if at > ANCHOR:
                break
            last = at
            messages.append(
                msg(
                    "t3code.messages",
                    f"{tid}:{m:04d}",
                    {"external_id": tid, "title": title},
                    at,
                    text,
                    dict(
                        role="user" if user else "assistant",
                        author_kind=kind,
                        project_id=proj["id"],
                        project=proj["name"],
                        t3_thread_id=tid,
                        title=title,
                        root_thread=th["root"],
                        is_subagent=bool(parent),
                        provider=provider,
                        model=model,
                        run=f"run-{m // 2}",
                        creation_source="subagent" if parent else "ui",
                        text_length=len(text),
                        text_truncated=False,
                        attachment_count=1 if user and rng.random() < 0.04 else 0,
                        mapping_revision=5,
                        environment_id=proj["env"][0],
                        environment=proj["env"][1],
                    ),
                    {"zoo": True, "thread": tid, "seq": m},
                )
            )
        th["last"] = last
        proj["last"] = max(proj["last"] or last, last)
    for th in threads:
        proj = th["proj"]
        messages.append(
            msg(
                "t3code.threads",
                th["id"],
                {"external_id": proj["id"], "title": proj["name"]},
                th["start"],
                f"{th['title']}\nProject {proj['name']}, {th['provider']} {th['model']}"
                + (" (subagent)" if th["parent"] else ""),
                dict(
                    t3_thread_id=th["id"],
                    project_id=proj["id"],
                    project=proj["name"],
                    title=th["title"],
                    provider=th["provider"],
                    model=th["model"],
                    created_by="agent" if th["parent"] else "owner",
                    creation_source="subagent" if th["parent"] else "ui",
                    branch=f"zoo/{slug(th['title'])[:30]}",
                    worktree_path=f"/work/{proj['name']}/.worktrees/{th['id'][-6:]}",
                    is_subagent=bool(th["parent"]),
                    parent_thread=th["parent"]["id"] if th["parent"] else None,
                    root_thread=th["root"],
                    relationship_to_parent="spawned" if th["parent"] else None,
                    archived=rng.random() < 0.2,
                    deleted=False,
                    last_updated=iso(th["last"]),
                    mapping_revision=5,
                    environment_id=proj["env"][0],
                    environment=proj["env"][1],
                ),
                {"zoo": True, "thread": th["id"]},
            )
        )
        if th["parent"]:
            links.append(
                link(
                    "part_of",
                    key("t3code.threads", th["id"]),
                    key("t3code.threads", th["parent"]["id"]),
                )
            )
    for proj in projects:
        mine = [t for t in threads if t["proj"] is proj]
        at = min((t["start"] for t in mine), default=ANCHOR - timedelta(days=100))
        messages.append(
            msg(
                "t3code.projects",
                proj["id"],
                {"external_id": "projects", "title": "T3 Code projects"},
                at,
                f"{proj['name']}\n/work/{proj['name']}\n{len(mine)} threads",
                dict(
                    project_id=proj["id"],
                    project=proj["name"],
                    workspace_root=f"/work/{proj['name']}",
                    deleted=False,
                    top_level_threads=sum(1 for t in mine if not t["parent"]),
                    all_threads=len(mine),
                    last_updated=iso(proj["last"] or at),
                    mapping_revision=5,
                    environment_id=proj["env"][0],
                    environment=proj["env"][1],
                ),
                {"zoo": True, "project": proj["id"]},
            )
        )
    subs = [
        ("juno@claude", "Claude Max", "claude", "max_20x", 1.0),
        ("juno@codex", "Codex Pro", "codex", "pro", 0.7),
        ("studio@claude", "Claude Team", "claude", "team", 0.25),
    ]
    weeks = n_of(8, scale * 2, 2)
    for s, (account, name, provider, plan, appetite) in enumerate(subs):
        latest = {}
        for window, minutes, label, span in (
            ("weekly", 10080, "Weekly", weeks * 7 * 24),
            ("five_hour", 300, "5 hours", min(weeks * 7 * 24, 14 * 24)),
        ):
            if window == "five_hour" and s == 2:
                continue
            wid = f"{account}:{window}"
            start = ANCHOR - timedelta(hours=span)
            reset_every = timedelta(minutes=minutes)
            w_start = start - timedelta(hours=rng.uniform(0, minutes / 60))
            used, last_used, last_at, credits = 0.0, None, None, 2
            cycle = rng.uniform(0.5, 1.6)
            step = timedelta(minutes=60 if window == "weekly" else 30)
            at = start
            while at <= ANCHOR:
                if at >= w_start + reset_every or (
                    window == "weekly"
                    and s == 0
                    and used >= 100
                    and rng.random() < 0.01
                    and credits
                ):
                    early = at < w_start + reset_every
                    if window == "weekly" or used > 60:
                        messages.append(
                            msg(
                                "t3code.resets",
                                f"{wid}:reset:{int(at.timestamp())}",
                                {"external_id": account, "title": name},
                                at,
                                f"{name} {label.lower()} window {'reset early with a credit' if early else 'reset'} at {used:.0f}% used",
                                dict(
                                    event="early_reset" if early else "scheduled_reset",
                                    account=account,
                                    subscription=name,
                                    provider=provider,
                                    window_id=wid,
                                    window_label=label,
                                    used_before_percent=round(used, 1),
                                    left_before_percent=round(100 - used, 1),
                                    next_reset_at=iso(at + reset_every),
                                    hours_since_previous_reset=round(
                                        (at - w_start).total_seconds() / 3600, 2
                                    ),
                                    scheduled_reset_at=iso(w_start + reset_every),
                                    available_credits=credits - (1 if early else 0),
                                    credit_change=-1 if early else 0,
                                    credit_options=[
                                        {"kind": "weekly_reset", "count": credits}
                                    ],
                                    mapping_revision=5,
                                ),
                                {"zoo": True, "window": wid},
                            )
                        )
                    if early:
                        credits -= 1
                    w_start, used, cycle, last_used = (
                        at,
                        0.0,
                        rng.uniform(0.4, 1.7),
                        None,
                    )
                weekend = at.weekday() >= 5
                activity = (
                    DIURNAL[at.hour] / 9.5 * (0.35 if weekend else 1) * appetite * cycle
                )
                per_hour = (100 / (5 * 14)) if window == "weekly" else 14
                burst = rng.lognormvariate(0, 0.7) if rng.random() < 0.6 else 0
                used = min(
                    100.0,
                    used + activity * per_hour * burst * (step.total_seconds() / 3600),
                )
                shown = round(used)
                if last_used is None or shown != last_used:
                    elapsed = (at - w_start) / reset_every * 100
                    left_h = (w_start + reset_every - at).total_seconds() / 3600
                    hours_since = (
                        (at - last_at).total_seconds() / 3600 if last_at else None
                    )
                    projected = (
                        min(250.0, used / elapsed * 100) if elapsed > 2 else None
                    )
                    status = (
                        "limit_reached"
                        if shown >= 100
                        else "ahead_of_pace"
                        if used - elapsed > 10
                        else "ok"
                    )
                    fields = dict(
                        event="status",
                        account=account,
                        subscription=name,
                        provider=provider,
                        window_id=wid,
                        window_label=label,
                        window_minutes=minutes,
                        used_percent=float(shown),
                        left_percent=float(100 - shown),
                        resets_at=iso(w_start + reset_every),
                        window_start=iso(w_start),
                        time_left_hours=round(left_h, 2),
                        time_left=f"{int(left_h // 24)}d {int(left_h % 24)}h"
                        if left_h >= 24
                        else f"{int(left_h)}h {int(left_h * 60 % 60)}m",
                        window_elapsed_percent=round(elapsed, 1),
                        pace_points=round(used - elapsed, 1),
                        projected_used_at_reset=round(projected, 1)
                        if projected
                        else None,
                        burn_since_previous=float(shown - (last_used or 0)),
                        hours_since_previous=round(hours_since, 2)
                        if hours_since
                        else None,
                        cycle_burn_per_day=round(
                            used / max(0.05, (at - w_start).total_seconds() / 86400), 1
                        ),
                        status=status,
                        plan_type=plan,
                        source="poll",
                        mapping_revision=5,
                    )
                    messages.append(
                        msg(
                            "t3code.usage",
                            f"{wid}:{int(at.timestamp())}",
                            {"external_id": wid, "title": f"{name} {label.lower()}"},
                            at,
                            f"{name} {label.lower()}: {shown}% used, {100 - shown}% left, resets in {fields['time_left']} ({status.replace('_', ' ')})",
                            fields,
                            {"zoo": True, "window": wid},
                        )
                    )
                    last_used, last_at = shown, at
                    latest[window] = fields
                at += step
        messages.append(
            msg(
                "t3code.subscriptions",
                account,
                {"external_id": "subscriptions", "title": "AI subscriptions"},
                ANCHOR - timedelta(minutes=5),
                f"{name} ({provider}, {plan}): "
                + ", ".join(
                    f"{w} {f['used_percent']:.0f}% used" for w, f in latest.items()
                ),
                dict(
                    account=account,
                    subscription=name,
                    provider=provider,
                    instance="default",
                    plan=plan,
                    auth_type="oauth",
                    enabled=True,
                    usage_unavailable="team plans report no five hour window"
                    if s == 2
                    else None,
                    windows=[
                        {
                            "id": f["window_id"],
                            "label": f["window_label"],
                            "used_percent": f["used_percent"],
                            "resets_at": f["resets_at"],
                            "status": f["status"],
                        }
                        for f in latest.values()
                    ],
                    available_credits=1 if s == 0 else 0,
                    mapping_revision=5,
                ),
                {"zoo": True, "account": account},
            )
        )
    return messages, links


# --- morning briefing --------------------------------------------------------------------------

MORNING = {
    "activity": [("chat_digest", "messages"), ("coding_threads", "coding")],
    "calendar": [("calendar_window", "calendar")],
    "email": [("unread_mail", "email")],
    "events": [
        ("local_listings", "search"),
        ("venue_scrape", "scrape"),
        ("friends_plans", "listing"),
    ],
    "finance": [("accounts", "ledger"), ("net_worth", "ledger")],
    "health": [("oura_summary", "health"), ("weight_trend", "health")],
    "location": [("presence", "location"), ("time_zone", "location")],
    "media": [("watchlist", "media")],
    "news": [
        ("moss_weekly", "newsletter"),
        ("kite_tech", "feed"),
        ("headlines", "feed"),
    ],
    "other": [("mystery_bundle", "unknown")],
    "pipeline": [
        ("report_spec", "config"),
        ("feedback", "audit"),
        ("source_health", "audit"),
    ],
    "tasks": [("work_tracker", "tasks")],
    "weather": [("forecast", "weather")],
}


def make_morning(seed, scale):
    rng = random.Random(f"{seed}:morning")
    messages = []
    days = n_of(120, scale, 5)
    for back in range(days):
        day = (ANCHOR - timedelta(days=back)).date()
        collected = datetime(
            day.year, day.month, day.day, 5, 30, tzinfo=UTC
        ) + timedelta(minutes=rng.uniform(0, 20))
        for name, sources in MORNING.items():
            if name == "other" and back < days * 0.75:
                continue
            if name == "media" and back > days * 0.55:
                continue
            for source, kind in sources:
                status = rng.choices(["ok", "empty", "error"], [92, 4, 4])[0]
                count = 0 if status != "ok" else rng.randrange(1, 14)
                if name == "weather":
                    high = (
                        16
                        + 8
                        * math.sin((day.timetuple().tm_yday - 100) * 2 * math.pi / 365)
                        + rng.gauss(0, 2)
                    )
                    items = [
                        {
                            "hour": h,
                            "temp_c": round(
                                high - 6 + 6 * math.sin((h - 6) * math.pi / 12), 1
                            ),
                            "rain_mm": round(max(0, rng.gauss(0, 0.6)), 1),
                        }
                        for h in range(6, 24, 3)
                    ]
                    text = (
                        f"Weather for {day}: high {high:.0f} °C, low {high - 7:.0f} °C, {rng.choice(['clear', 'cloudy', 'showers', 'windy', 'fog'])}.\n"
                        + "\n".join(
                            f"- {i['hour']:02d}:00 {i['temp_c']} °C, rain {i['rain_mm']} mm"
                            for i in items
                        )
                    )
                elif name == "finance":
                    items = [
                        {
                            "payee": rng.choice(ORG_NAMES),
                            "amount": -round(rng.uniform(2, 120), 2),
                            "category": rng.choice(
                                ["groceries", "transport", "fun", "bills"]
                            ),
                        }
                        for _ in range(count)
                    ]
                    text = (
                        f"{source.replace('_', ' ').title()} for {day}: {count} transactions, balance {4200 + 900 * math.sin(back / 9) + rng.gauss(0, 60):.2f} invented credits.\n"
                        + "\n".join(
                            f"- {i['payee']}: {i['amount']} ({i['category']})"
                            for i in items
                        )
                    )
                else:
                    items = [
                        {"title": line(rng, PROSE), "rank": i + 1} for i in range(count)
                    ]
                    text = (
                        f"{source.replace('_', ' ').title()} for {day}: {count} items.\n"
                        + "\n".join(f"- {i['title']}" for i in items)
                    )
                error = None
                if status == "error":
                    error = rng.choice(
                        [
                            "TimeoutError: source did not answer in 30 s",
                            "HTTP 503 from the invented upstream",
                            "ParseError: unexpected bundle shape",
                        ]
                    )
                    text = (
                        f"{source.replace('_', ' ').title()} for {day} failed: {error}"
                    )
                    items = []
                fields = dict(
                    report_date=str(day),
                    source_name=source,
                    source_kind=kind,
                    status=status,
                    item_count=count,
                    collected_at=iso(collected),
                    normalized={"items": items},
                    source_provenance={
                        "collector": "zoo-morning",
                        "run": f"{day}-0530",
                    },
                    error=error,
                    mapping_revision=3,
                )
                if name == "other":
                    fields["raw_json"] = {
                        "bundle": source,
                        "keys": ["alpha", "beta"],
                        "note": "unrecognised shape",
                    }
                messages.append(
                    msg(
                        f"morning.{name}",
                        f"{day}:{source}",
                        {
                            "external_id": source,
                            "title": source.replace("_", " ").title(),
                        },
                        collected,
                        text,
                        fields,
                        {"zoo": True, "day": str(day), "source": source},
                        subject=day_subject(day),
                    )
                )
    return messages


# --- assembly -------------------------------------------------------------------------------------


def corpus(seed=47, scale=1.0):
    """The whole zoo as plain data; a pure function of (seed, scale)."""
    people, relations = make_people(seed, scale)
    entities = make_entities(seed)
    messages, links, prov, annotations = [], [], [], []
    chat, chat_links = make_chats(seed, scale, people)
    cap = n_of(4000, scale, 20)
    messages += chat
    links += chat_links[:cap]
    wave, wave_links, wave_notes = make_wave(seed, scale, people)
    messages += wave
    links += wave_links
    annotations += wave_notes
    for part in (make_email(seed, scale, people), make_calendar(seed, scale, people)):
        messages += part[0]
        links += part[1]
    health, health_gap = make_health(seed, scale)
    messages += health
    location, track = make_location(seed, scale)
    messages += location
    yt, yt_links, yt_prov, video_ids = make_youtube(seed, scale)
    messages += yt
    links += yt_links
    prov += yt_prov
    web = [e["external_ref"] for e in entities if e["kind"] == "web_page"]
    research, research_links, research_prov = make_research(seed, scale, video_ids, web)
    messages += research
    links += research_links
    prov += research_prov
    wiki, revisions, wiki_links, wiki_notes, pages = make_wiki(
        seed, scale, people, entities
    )
    messages += wiki
    links += wiki_links
    annotations += wiki_notes
    t3, t3_links = make_t3code(seed, scale)
    messages += t3
    links += t3_links
    messages += make_morning(seed, scale)
    rng = random.Random(f"{seed}:extras")
    for e in entities:
        if e["kind"] == "place" and e["external_ref"].startswith("zoo-place-"):
            city = e["metadata"]["address"].split(", ")[-1]
            links.append(
                link(
                    "part_of",
                    ent("place", e["external_ref"]),
                    ent("place", "zoo-city-" + slug(city)),
                )
            )
        if e["kind"] == "work":
            links.append(
                link(
                    "published_by",
                    ent("work", e["external_ref"]),
                    ent("organization", "zoo-org-" + slug(rng.choice(ORG_NAMES))),
                )
            )
        if e["kind"] == "event":
            links.append(
                link(
                    "at_place",
                    ent("event", e["external_ref"]),
                    ent(
                        "place",
                        "zoo-place-"
                        + rng.choice(["park", "market", "work", "viewpoint"]),
                    ),
                )
            )
    for i, p in enumerate(people[1:]):
        employer = p["facts"].get("employer")
        if employer:
            year = 2016 + i % 9
            links.append(
                link(
                    "related_to",
                    {"person": p["handle"]},
                    ent("organization", "zoo-org-" + slug(employer)),
                    valid_from=f"{year}-01-01",
                    valid_from_precision="year",
                    **(
                        {"valid_to": f"{year + 2}-01-01", "valid_to_precision": "year"}
                        if i % 3 == 0
                        else {}
                    ),
                )
            )
    sample = [m for m in chat if len(m["content"]) > 40]
    for m in rng.sample(sample, min(len(sample), n_of(120, scale, 6))):
        work = rng.choice(WORK_NAMES)[1]
        links.append(
            link(
                "mentions",
                key(m["channel"], m["external_id"]),
                ent("work", "zoo-work-" + slug(work)),
            )
        )
    for m in rng.sample(chat, min(len(chat), 4)):
        annotations.append(
            dict(
                channel=m["channel"],
                external_id=m["external_id"],
                attribute="ignore",
                value={"ignored": True, "reason": "spam"},
                reason="Not worth keeping in recall (zoo)",
            )
        )
    claims = []
    cited = rng.sample(sample, min(len(sample), 40))
    for i, p in enumerate(people[1:9]):
        source = cited[i % len(cited)]
        claims.append(
            dict(
                subject={"kind": "person", "person": p["handle"]},
                claim_type="observation",
                statement=f"{p['display_name']} collects every {THINGS[i]} they can find.",
                confidence=0.8,
                sources=[key(source["channel"], source["external_id"])],
            )
        )
    for i, e in enumerate(
        [e for e in entities if e["kind"] in ("organization", "work")][:8]
    ):
        source = cited[(i + 9) % len(cited)]
        kind = ["inference", "hypothesis", "observation"][i % 3]
        claims.append(
            dict(
                subject={"kind": "entity", "entity": (e["kind"], e["external_ref"])},
                claim_type=kind,
                statement=f"{e['name']} is tied to the {THINGS[i + 9]} revival.",
                confidence=round(0.4 + 0.07 * i, 2),
                uncertainty="Single chat mention." if kind == "hypothesis" else None,
                sources=[key(source["channel"], source["external_id"])],
            )
        )
    for i in range(4):
        claims.append(
            dict(
                subject={"kind": "concept", "label": THINGS[i + 20]},
                claim_type="judgment",
                statement=f"The {THINGS[i + 20]} is overrated.",
                confidence=0.6,
                attribution={"person": people[2 + i]["handle"]},
                sources=[
                    key(
                        cited[(i + 20) % len(cited)]["channel"],
                        cited[(i + 20) % len(cited)]["external_id"],
                    )
                ],
            )
        )
    for i in range(3):
        claims.append(
            dict(
                subject={"kind": "concept", "label": THINGS[i + 26]},
                claim_type="hypothesis",
                statement=f"Nobody has finished a {THINGS[i + 26]} since the flood.",
                confidence=0.3,
                no_source_reason="Owner hunch, nothing written down (zoo).",
            )
        )
    for i, p in enumerate(pages[:3]):
        claims.append(
            dict(
                subject={"kind": "page"},
                page_channel="gbrain.pages",
                page_external_id="default:" + p["slug"],
                claim_type="observation",
                statement=f"The page on {p['title']} was written after the {THINGS[i + 3]} fair.",
                confidence=0.7,
                sources=[
                    key(
                        cited[(i + 30) % len(cited)]["channel"],
                        cited[(i + 30) % len(cited)]["external_id"],
                    )
                ],
            )
        )
    return dict(
        people=people,
        relations=relations,
        entities=entities,
        messages=messages,
        revisions=revisions,
        links=links,
        provenance=prov,
        annotations=annotations,
        claims=claims,
        track=track,
        health_gap=[str(d) for d in health_gap],
    )


# --- loading -----------------------------------------------------------------------------------------


def _sql(client, statement, row_cap=10000):
    return client.request(
        "POST", "sql", {"statement": statement, "row_cap": row_cap}, retry=True
    )["rows"]


def _ingest(client, messages, tolerate=()):
    """Ingest in batches; returns (inserted, new_versions, unchanged)."""
    totals = [0, 0, 0]
    for start in range(0, len(messages), BATCH):
        answer = client.request(
            "POST",
            "ingest/messages",
            dict(
                plugin=client.plugin,
                messages=messages[start : start + BATCH],
                links=[],
                dry_run=False,
            ),
            retry=True,
        )
        bad = [r for r in answer["rejected"] if r.get("reason") not in tolerate]
        if bad:
            raise ValueError(f"{client.plugin}: {len(bad)} rejected, first: {bad[0]}")
        for i, name in enumerate(("inserted", "new_versions", "unchanged")):
            totals[i] += answer[name]
    return totals


def _manifests():
    for plugin in PLUGINS:
        yield Manifest(
            name=plugin,
            version="0.0.47",
            channels=[
                Channel(
                    name=c["name"],
                    description=c["description"],
                    fields=c["fields"],
                    is_reference=c["is_reference"],
                    is_outside=c["is_outside"],
                    conversation_whole_thread=c["conversation_whole_thread"],
                    record_kind=c["record_kind"],
                )
                for c in SCHEMAS
                if c["plugin"] == plugin
            ],
            run_mode="stream" if plugin == "t3code" else "poll",
            default_interval_s=INTERVALS[plugin],
            link_types=LINK_TYPES[plugin],
        )


def _load_people(client, data, log):
    existing = {p["handle"]: p for p in client.identity("people", method="GET")}
    known = {
        (r["system"], r["value"]) for r in client.registry("identifiers", method="GET")
    }
    names = {
        (r["person_id"], r["name"]) for r in client.registry("names", method="GET")
    }
    fact_keys = {r["key"] for r in client.registry("fact-keys", method="GET")}
    facts = {(r["person_id"], r["key"]) for r in client.registry("facts", method="GET")}
    kinds = {r["kind"] for r in client.registry("relation-kinds", method="GET")}
    relations = {
        (r["from_id"], r["to_id"], r["kind"])
        for r in client.registry("relations", method="GET")
    }
    ids = {}
    owner_exists = any(p.get("is_owner") for p in existing.values())
    for p in data["people"]:
        row = existing.get(p["handle"])
        if row is None:
            row = client.identity(
                "people",
                dict(
                    handle=p["handle"],
                    display_name=p["display_name"],
                    is_owner=p["is_owner"] and not owner_exists,
                    note="Invented person of the shape zoo",
                    source_system="zoo",
                    source_id=p["handle"],
                ),
            )
        ids[p["handle"]] = row["id"]
        for n, (system, value) in enumerate(sorted(p["ids"].items())):
            kind = {"sms": "phone", "email": "email", "wave": "speaker"}.get(
                system, "account"
            )
            if (system, value) not in known:
                suspected = (
                    not p["is_owner"] and system == "instagram" and row["id"] % 11 == 0
                )
                client.registry(
                    "identifiers",
                    dict(
                        person_id=row["id"],
                        system=system,
                        kind=kind,
                        value=value,
                        status="suspected" if suspected else "confirmed",
                        label=f"{system} (zoo)",
                    ),
                )
        for name in p["names"]:
            if (row["id"], name) not in names:
                client.registry(
                    "names", dict(person_id=row["id"], name=name, note="zoo")
                )
        for k, value in sorted(p["facts"].items()):
            if k not in fact_keys:
                client.registry("fact-keys", dict(key=k))
                fact_keys.add(k)
            if (row["id"], k) not in facts:
                client.registry(
                    "facts",
                    dict(
                        person_id=row["id"],
                        key=k,
                        value=value,
                        note="zoo",
                        source="zoo",
                    ),
                )
    for kind, inverse in (
        ("parent", "child"),
        ("child", "parent"),
        ("sibling", "sibling"),
        ("partner", "partner"),
        ("cousin", "cousin"),
        ("friend", "friend"),
        ("colleague", "colleague"),
        ("manager", "report"),
        ("report", "manager"),
    ):
        if kind not in kinds:
            client.registry("relation-kinds", dict(kind=kind, inverse=inverse))
    for a, b, kind in data["relations"]:
        if (ids[a], ids[b], kind) not in relations:
            client.registry(
                "relations", dict(from_id=ids[a], to_id=ids[b], kind=kind, note="zoo")
            )
    log(f"people: {len(ids)}")
    return ids


def _resolve(end, people):
    if "person" in end:
        return {"kind": "person", "id": people[end["person"]]}
    return end


def load(url, token, seed=47, scale=1.0, log=lambda text: None):
    """Load the zoo through the public HTTP routes; returns a summary dict."""
    started = time.monotonic()
    data = corpus(seed, scale)
    log(
        f"corpus built: {len(data['messages'])} messages in {time.monotonic() - started:.1f}s"
    )
    clients = {p: Client(url, token, p) for p in PLUGINS}
    owner = Client(url, token, "zoo")
    try:
        for manifest in _manifests():
            clients[manifest.name].register(manifest)
        people = _load_people(owner, data, log)
        entity_ids = {}
        for start in range(0, len(data["entities"]), 1000):
            chunk = data["entities"][start : start + 1000]
            answer = owner.request(
                "POST", "entities/batch", {"entities": chunk}, retry=True
            )
            for item in answer["results"]:
                if item["status"] == "error":
                    raise ValueError(f"entity refused: {item}")
                entity_ids[
                    (chunk[item["index"]]["kind"], chunk[item["index"]]["external_ref"])
                ] = item["entity_id"]
        log(f"entities: {len(entity_ids)}")

        by_channel = defaultdict(list)
        for m in data["messages"]:
            by_channel[m["channel"]].append(m)
        for rows in by_channel.values():
            rows.sort(key=lambda m: m["happened_at"])
        _ingest(clients["gbrain"], data["revisions"], tolerate=("version_conflict",))
        counts = defaultdict(lambda: [0, 0, 0])

        def deliver(channel):
            return channel, _ingest(clients[PLUGIN_OF[channel]], by_channel[channel])

        with ThreadPoolExecutor(max_workers=6) as pool:
            for channel, totals in pool.map(
                deliver, sorted(by_channel, key=lambda c: -len(by_channel[c]))
            ):
                counts[channel] = totals
        log(f"messages ingested in {time.monotonic() - started:.1f}s")

        for start in range(0, len(data["track"]), 5000):
            clients["morning_context"].track(data["track"][start : start + 5000])
        visits = owner.request("POST", "track/detect-visits") if data["track"] else {}
        log(f"track: {len(data['track'])} points, visits {visits}")

        links = [
            dict(
                item,
                **{
                    "from": _resolve(item["from"], people),
                    "to": _resolve(item["to"], people),
                },
            )
            for item in data["links"]
        ]
        outcomes = defaultdict(int)
        for item in owner.link_batch(links):
            outcomes[item["status"]] += 1
            if item["status"] in ("error", "refused"):
                raise ValueError(f"link refused: {item} {links[item['index']]}")
        log(f"links: {dict(outcomes)}")
        written = unresolved = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            for answer in pool.map(
                lambda record: clients[
                    PLUGIN_OF[record["derived"]["channel"]]
                ].provenance(record),
                data["provenance"],
            ):
                written += answer["written"]
                unresolved += answer["unresolved"]
        log(
            f"provenance: {len(data['provenance'])} records, {written} new rows, {unresolved} unresolved"
        )

        extras = _load_extras(owner, data, people, entity_ids, log)
        health = _plugin_health(clients, data, by_channel, counts, seed)
        # The reply index is rebuilt within a time budget per read: ask until
        # no thread is stale, so the people views are complete at once.
        stale = None
        for _ in range(120):
            stale = owner.request("GET", "people/index")["rows"][0]["stale_threads"]
            if not stale:
                break
        log(f"exchange index: {stale} stale threads")
        summary = dict(
            seed=seed,
            scale=scale,
            messages=len(data["messages"]),
            people=len(people),
            entities=len(entity_ids),
            links=dict(outcomes),
            provenance=dict(
                records=len(data["provenance"]), written=written, unresolved=unresolved
            ),
            track_points=len(data["track"]),
            visits=visits,
            ingest={
                c: dict(zip(("inserted", "new_versions", "unchanged"), t))
                for c, t in sorted(counts.items())
            },
            **extras,
            **health,
            seconds=round(time.monotonic() - started, 1),
        )
        return summary
    finally:
        owner.close()
        for client in clients.values():
            client.close()


def _message_ids(client, keys):
    """{(channel, external_id): message id} for the given keys."""
    out = {}
    by_channel = defaultdict(list)
    for channel, external_id in keys:
        by_channel[channel].append(external_id)
    for channel, values in by_channel.items():
        quoted = ",".join("'" + v.replace("'", "''") + "'" for v in sorted(set(values)))
        for row in _sql(
            client,
            f"SELECT id, external_id FROM api.messages WHERE channel='{channel}' AND external_id IN ({quoted})",
        ):
            out[(channel, row["external_id"])] = row["id"]
    return out


def _load_extras(client, data, people, entity_ids, log):
    """Annotations, replay-safe."""
    annotated = {
        (r["channel"], r["external_id"], r["attribute"])
        for r in _sql(
            client,
            "SELECT channel, target_external_id AS external_id, attribute FROM api.annotations",
        )
    }
    notes = 0
    for note in data["annotations"]:
        if (note["channel"], note["external_id"], note["attribute"]) in annotated:
            continue
        value = dict(note["value"])
        if "person" in value:
            value = {"person_id": people[value["person"]]}
        client.request(
            "POST",
            "annotations",
            dict(
                channel=note["channel"],
                external_id=note["external_id"],
                attribute=note["attribute"],
                value=value,
                reason=note["reason"],
            ),
        )
        notes += 1
    log(f"annotations +{notes}")
    return dict(
        annotations_created=notes,
    )


def _plugin_health(clients, data, by_channel, counts, seed):
    """Cursor, heartbeat and coverage variety through the plugin routes."""

    def window(channel):
        rows = by_channel.get(channel) or []
        return (
            (rows[0]["happened_at"], min(rows[-1]["happened_at"], iso(ANCHOR)))
            if rows
            else None
        )

    def delivered(plugin):
        return sum(t[0] + t[1] for c, t in counts.items() if PLUGIN_OF[c] == plugin)

    reports = defaultdict(list)
    for channel, rows in sorted(by_channel.items()):
        plugin = PLUGIN_OF[channel]
        if plugin in ("email", "beeper"):
            continue
        reports[plugin].append(
            coverage(
                channel,
                status="complete",
                window=window(channel),
                high_water=window(channel)[1],
                rows_delivered=len(rows),
            )
        )
    for channel in (
        "chat.discord",
        "chat.instagram",
        "chat.sms",
        "chat.reddit",
        "chat.snapchat",
    ):
        reports["beeper"].append(
            coverage(
                channel,
                scope_key="account:zoo",
                status="best_effort",
                reasons=["partial_history"],
                high_water=window(channel)[1],
                rows_delivered=len(by_channel[channel]),
                detail={
                    "note": "the bridge lists no history older than its own install"
                },
            )
        )
    gap = (iso(ANCHOR - timedelta(days=41)), iso(ANCHOR - timedelta(days=33)))
    reports["email"].append(
        coverage(
            "email.message",
            scope_key="ca_zoo_primary",
            status="partial",
            reasons=["rate_limited", "source_cap"],
            window=window("email.message"),
            high_water=window("email.message")[1],
            rows_delivered=len(by_channel["email.message"]),
            gaps=[{"from": gap[0], "to": gap[1], "reason": "rate_limited"}],
            detail={
                "note": "the invented mail API stopped listing after its daily quota"
            },
        )
    )
    g0, g1 = data["health_gap"]
    reports["health"].append(
        coverage(
            "health.oura.daily",
            scope_key="oura",
            # No window: a covering complete report would close its own gap.
            status="partial",
            reasons=["source_error"],
            rows_delivered=len(by_channel["health.oura.daily"]),
            gaps=[
                {
                    "from": g0 + "T00:00:00+00:00",
                    "to": g1 + "T00:00:00+00:00",
                    "reason": "source_error",
                }
            ],
        )
    )
    reports["morning_context"].append(
        coverage(
            "morning.other",
            status="partial",
            reasons=["quarantined_rows", "unknown_shape"],
            rows_delivered=len(by_channel.get("morning.other", [])),
            detail={"quarantined": 3},
        )
    )
    out = {}
    for plugin, client in clients.items():
        if plugin == "youtube":
            continue  # registered, never run: no cursor, heartbeat or coverage
        rows = [
            m for c, ms in by_channel.items() if PLUGIN_OF[c] == plugin for m in ms[-1:]
        ]
        client.checkpoint(
            {
                "high_water": max((m["happened_at"] for m in rows), default=None),
                "zoo_seed": seed,
            }
        )
        client.coverage_attempt()
        answer = client.coverage(reports[plugin])
        out[plugin] = dict(
            stored=answer["stored"],
            degraded=len(answer["degraded"]),
            refused=answer["refused"],
            gaps_opened=len(answer["gaps_opened"]),
        )
        # gbrain: an interval of one second makes its last success stale at once.
        client.heartbeat(
            interval_s=1 if plugin == "gbrain" else INTERVALS[plugin],
            messages_ingested=delivered(plugin),
        )
    clients["research"].heartbeat(
        interval_s=INTERVALS["research"],
        error="RuntimeError: zoo-search answered 529 (overloaded) for stream lisbon-events",
        messages_ingested=0,
    )
    return dict(coverage=out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seed", type=int, default=47)
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args(argv)
    summary = load(
        os.environ["THROUGHLINE_URL"],
        os.environ["THROUGHLINE_TOKEN"],
        args.seed,
        args.scale,
        log=lambda text: print(text, file=sys.stderr, flush=True),
    )
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
