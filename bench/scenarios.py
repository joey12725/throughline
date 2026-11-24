"""Seeded scenario generator for the public twin.

build(seed, scale) returns a Corpus: channels, messages, links and questions
with gold labels. Every word that carries meaning is invented (three
consonant-vowel syllables and a closing k or x), so nothing can collide with
real text, every planted answer is unique, and keyword behaviour is exact:
PostgreSQL stems none of these words and question templates use only English
stop words around them.

load(client, corpus, phase) sends a corpus through the public ingest API.
Phase 1 is the archive; phase 2 holds what arrives late (backfilled messages
and links made after a cutoff), so a runner can record a timestamp between
the two and ask questions "as known then". plant(client, corpus) then writes
what a curator or importer adds after the load: provenance from every page
revision to its sources, typed claims, link validity and the coverage report
of the declared import gap.

After the twin's classes the corpus gains one segment per behaviour
scenario, BEHAVIOUR_SCENARIOS, each with its own channels, people
and vocabulary, built from a generator seeded apart from the twin's, so the
twin's messages, questions and calibration constants are unchanged. Rows a
scenario loads itself (late backfill for a consumer) are kept in
corpus.held, never in corpus.messages.
"""

import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .gold import Cluster, Group, Question, Roles

PLUGIN = "bench"
CHAT_A, CHAT_B, NOTES, WIKI = (
    "bench.chat_a",
    "bench.chat_b",
    "bench.notes",
    "bench.wiki",
)
SYSTEMS = {CHAT_A: "bencha", CHAT_B: "benchb"}
BASE = datetime(2021, 3, 1, 9, tzinfo=UTC)
END = BASE + timedelta(days=700)
# bench.chat_b is registered and has data on both sides of this range but
# nothing inside it: an import gap the system is expected to disclose.
GAP = (BASE + timedelta(days=500), BASE + timedelta(days=560))
PER_CLASS = 12
FILLER_PER_SCALE = 900
MAX_BATCH = 5000
SYNONYMS_PATH = Path(__file__).with_name("synonyms.json")

CATEGORIES = (
    "exact_token",
    "paraphrase",
    "cross_platform",
    "person_handles",
    "ambiguous_person",
    "cross_source_time",
    "context_dependent",
    "changed_fact",
    "no_answer",
    "duplicate_cluster",
    "pattern",
    "page_assisted",
    "page_mutation",
    "temporal",
    "leakage",
    "import_gap",
)
MUTATIONS = ("missing", "stale", "contradictory", "wrong")
TEMPORAL_VARIANTS = (
    "subject_time",
    "revision_as_of",
    "interval_relationship",
    "late_backfill",
)
LEAKAGE_VARIANTS = ("fact", "link", "page_revision")

# Calibration, measured on seed 47 at scale 1 with the keyword arm
# (GET /v1/search, mode keyword, k 10, cutoff honoured). The generator is
# built so these hold exactly; the harness test compares a fresh run with
# them, so a change in either the generator or lexical search shows up.
KEYWORD_F05_SEED_47 = {
    # Every question word is in the answer: solved.
    "exact_token": 1.0,
    "cross_platform": 1.0,
    "changed_fact": 1.0,
    "duplicate_cluster": 1.0,
    "page_mutation": 1.0,
    "leakage": 1.0,
    "temporal": 1.0,
    # Finds the turn that shares words, not the one that answers (1 of 2).
    "context_dependent": 0.8333,
    "cross_source_time": 0.8333,
    # Supporting and comparison populations share words; counterexamples do not.
    "pattern": 0.9091,
    # No shared words with the evidence: meaning, identity or a page is needed.
    "paraphrase": 0.0,
    "person_handles": 0.0,
    "ambiguous_person": 0.0,
    "page_assisted": 0.0,
}
# Questions the keyword arm cannot pose (known_by, versions, subject time,
# links): 3 subject_time, 3 revision_as_of, 2 late_backfill, 4 leakage by link.
KEYWORD_SKIPPED_SEED_47 = 12

# --- Behaviour scenarios ---------------------------------------------------------

BEHAVIOUR_SCENARIOS = (
    "episode_count",
    "newest_relevant",
    "consumer_exactly_once",
    "coverage_honesty",
    "link_vocabulary",
    "restricted_inference",
    "dyad_vs_author",
    "open_loops",
    "media_contaminated",
    "unenrolled_voice",
    "suspected_link",
    "write_replay",
)
BEHAVIOUR_BASE = BASE + timedelta(days=600)
J_EPISODES, J_THREAD, J_FEED = (
    "bench.behaviour_episodes",
    "bench.behaviour_thread",
    "bench.behaviour_feed",
)
J_PARTIAL, J_COMPLETE, J_LINKS = (
    "bench.behaviour_partial",
    "bench.behaviour_complete",
    "bench.behaviour_links",
)
J_OPEN, J_HIDDEN = "bench.behaviour_open", "bench.behaviour_hidden"
J_DM, J_GROUP, J_OWNER = "bench.behaviour_dm", "bench.behaviour_group", "bench.behaviour_owner"
J_LOOPS, J_TRANSCRIPT, J_MEDIA = (
    "bench.behaviour_loops",
    "bench.behaviour_transcript",
    "bench.behaviour_media",
)
J_VOICE, J_SUSPECT, J_WRITES = (
    "bench.behaviour_voice",
    "bench.behaviour_suspect",
    "bench.behaviour_writes",
)
BEHAVIOUR_SYSTEM = "benchj"
for _channel in (J_EPISODES, J_THREAD, J_FEED, J_PARTIAL, J_COMPLETE, J_LINKS):
    SYSTEMS[_channel] = BEHAVIOUR_SYSTEM
for _channel in (J_OPEN, J_HIDDEN, J_DM, J_GROUP, J_OWNER, J_LOOPS, J_TRANSCRIPT):
    SYSTEMS[_channel] = BEHAVIOUR_SYSTEM
for _channel in (J_SUSPECT, J_WRITES):
    SYSTEMS[_channel] = BEHAVIOUR_SYSTEM
BEHAVIOUR_CATEGORIES = ("behaviour_episode_count", "behaviour_newest_relevant", "behaviour_coverage_honesty")
# Measured on seed 47 at scale 1 with the keyword arm, like the twin's
# constants. The episode question asks "how many times": those words are not
# stop words and no message holds them, so lexical search finds nothing
# (recall's episode grain is what answers it). The newest-relevant question
# shares its words with the old and the new statement only.
KEYWORD_F05_BEHAVIOUR_SEED_47 = {
    "behaviour_episode_count": 0.0,
    "behaviour_newest_relevant": 1.0,
}
# Every behaviour question can be posed by the keyword arm.
KEYWORD_SKIPPED_BEHAVIOUR_SEED_47 = 0

_CONSONANTS = "bdfgjklmnprtvwz"
_VOWELS = "aeiou"


def synonym_groups():
    """Groups of invented words that mean the same, from bench/synonyms.json.

    The file maps each word to its group. It is the only bridge between a
    paraphrase question and its answer, and the file the deterministic local
    embedder reads (THROUGHLINE_LOCAL_SYNONYMS).
    """
    groups = {}
    for word, group in json.loads(SYNONYMS_PATH.read_text("utf-8")).items():
        groups.setdefault(group, []).append(word)
    return [sorted(words) for _, words in sorted(groups.items())]


class _Words:
    """Unique invented words, never one that the synonym file uses."""

    def __init__(self, rng, reserved):
        self.rng = rng
        self.used = set(reserved)

    def __call__(self):
        while True:
            word = "".join(
                self.rng.choice(_CONSONANTS) + self.rng.choice(_VOWELS)
                for _ in range(3)
            ) + self.rng.choice("kx")
            if word not in self.used and set(word) & set("jvz"):
                self.used.add(word)
                return word


@dataclass
class Corpus:
    seed: int
    scale: int
    channels: list = field(default_factory=list)
    people: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    # Load phase of each message and link, aligned by position.
    phases: list = field(default_factory=list)
    links: list = field(default_factory=list)
    link_phases: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    # Subject interval of a message, sent only when the API accepts it.
    subjects: dict = field(default_factory=dict)
    # Validity interval of a link, by position, for systems that store it.
    link_validity: dict = field(default_factory=dict)
    # Pages and the raw records they were written from (planted as provenance
    # by later suites): {key, sources, mutation}.
    pages: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    # Behaviour scenario segments: {scenario: {channels, people, keys, ...}}.
    behaviours: dict = field(default_factory=dict)
    # Rows a scenario loads itself, at the moment its check needs them.
    held: dict = field(default_factory=dict)
    # What scenarios.plant wrote after the last load (counts per step).
    planted: dict = field(default_factory=dict)

    def by_category(self):
        out = {}
        for question in self.questions:
            out.setdefault(question.category, []).append(question)
        return out

    def phase_numbers(self):
        return sorted(set(self.phases) | set(self.link_phases))


class _Builder:
    def __init__(self, seed, scale):
        self.rng = random.Random(seed)
        groups = synonym_groups()
        self.word = _Words(self.rng, {w for g in groups for w in g})
        self.synonyms = groups
        self.rng.shuffle(self.synonyms)
        self.corpus = Corpus(seed=seed, scale=scale)
        self.fillers = [self.word() for _ in range(240)]
        self.filler_people = []
        for k in range(6):
            self.filler_people.append(self.person(f"bench-filler-{k}"))

    # -- primitives ---------------------------------------------------------

    def person(self, handle, display_name=None, channels=(CHAT_A, CHAT_B)):
        name = display_name or f"{self.word().capitalize()} {self.word().capitalize()}"
        person = dict(
            handle=handle,
            display_name=name,
            accounts=[
                dict(system=SYSTEMS[c], kind="account", value=f"{handle}-{SYSTEMS[c]}")
                for c in channels
            ],
        )
        self.corpus.people.append(person)
        return person

    def author(self, person, channel):
        for account in person["accounts"]:
            if account["system"] == SYSTEMS[channel]:
                return dict(account)
        raise ValueError("person has no account on " + channel)

    def at(self, category, item, days=0, minutes=0):
        return BASE + timedelta(
            days=CATEGORIES.index(category) * 20 + item + days, minutes=minutes
        )

    def message(
        self,
        channel,
        external_id,
        at,
        content,
        *,
        category,
        thread=None,
        person=None,
        ended=None,
        phase=1,
        subject=None,
        extra=None,
    ):
        data = dict(
            channel=channel,
            external_id=external_id,
            thread={"external_id": thread or external_id},
            happened_at=at.isoformat(),
            content=content,
            payload={"synthetic": True, "bench": category, **(extra or {})},
        )
        if ended:
            data["ended_at"] = ended.isoformat()
        if channel in SYSTEMS:
            data["author"] = self.author(
                person or self.rng.choice(self.filler_people), channel
            )
        self.corpus.messages.append(data)
        self.corpus.phases.append(phase)
        if subject:
            self.corpus.subjects[(channel, external_id)] = subject
        return (channel, external_id)

    def link(self, type_, source, target, phase=1, validity=None):
        end = (
            dict(channel=target[0], external_id=target[1])
            if isinstance(target, tuple)
            else target
        )
        if validity:
            self.corpus.link_validity[len(self.corpus.links)] = validity
        self.corpus.links.append(
            {
                "type": type_,
                "from": dict(channel=source[0], external_id=source[1]),
                "to": end,
                "method": "source",
            }
        )
        self.corpus.link_phases.append(phase)

    def question(self, category, item, text, **labels):
        labels.setdefault("cutoff", END)
        labels.setdefault("source", "canary")
        question = Question(
            id=f"{category}-{item:03d}", text=text, category=category, **labels
        )
        self.corpus.questions.append(question)
        return question

    def chat(self, item):
        return (CHAT_A, CHAT_B)[item % 2]

    # -- planted classes ----------------------------------------------------

    def exact_token(self, i):
        cat = "exact_token"
        thing, attr, other, value, value2 = (self.word() for _ in range(5))
        key = self.message(
            self.chat(i),
            f"{cat}-{i}",
            self.at(cat, i),
            f"The {attr} of the {thing} is {value}.",
            category=cat,
            thread=f"{cat}-{i}",
        )
        self.message(
            self.chat(i),
            f"{cat}-{i}-near",
            self.at(cat, i, minutes=7),
            f"The {other} of the {thing} is {value2}.",
            category=cat,
            thread=f"{cat}-{i}",
        )
        self.question(
            cat,
            i,
            f"What is the {attr} of the {thing}?",
            groups=[Group(id="fact", alternatives=[key])],
        )

    def paraphrase(self, i):
        cat = "paraphrase"
        first, second = self.synonyms[2 * i], self.synonyms[2 * i + 1]
        value = self.word()
        key = self.message(
            self.chat(i),
            f"{cat}-{i}",
            self.at(cat, i),
            f"The {first[0]} of the {second[0]} is {value}.",
            category=cat,
            thread=f"{cat}-{i}",
        )
        if i % 2:
            # A lexical trap: the question's own words, about something else.
            self.message(
                self.chat(i + 1),
                f"{cat}-{i}-trap",
                self.at(cat, i, minutes=30),
                f"No {first[1]} and no {second[1]} today, only {self.word()}.",
                category=cat,
                thread=f"{cat}-{i}-trap",
            )
        self.question(
            cat,
            i,
            f"What is the {first[1]} of the {second[1]}?",
            groups=[Group(id="fact", alternatives=[key])],
        )

    def cross_platform(self, i):
        cat = "cross_platform"
        thing, attr, value = (self.word() for _ in range(3))
        person = self.rng.choice(self.filler_people)
        a = self.message(
            CHAT_A,
            f"{cat}-{i}",
            self.at(cat, i),
            f"The {attr} of the {thing} is {value}.",
            category=cat,
            person=person,
        )
        b = self.message(
            CHAT_B,
            f"{cat}-{i}",
            self.at(cat, i, minutes=3),
            f"Again for this side: the {thing} has {value} as its {attr}.",
            category=cat,
            person=person,
        )
        self.question(
            cat,
            i,
            f"What is the {attr} of the {thing}?",
            groups=[Group(id="fact", alternatives=[a, b])],
        )

    def person_handles(self, i):
        cat = "person_handles"
        topic, v1, v2, v3 = (self.word() for _ in range(4))
        person = self.person(f"bench-handles-{i}")
        a = self.message(
            CHAT_A,
            f"{cat}-{i}",
            self.at(cat, i),
            f"My {topic} pick is the {v1}.",
            category=cat,
            person=person,
        )
        b = self.message(
            CHAT_B,
            f"{cat}-{i}",
            self.at(cat, i, minutes=50),
            f"For the {topic} I will also take the {v2}.",
            category=cat,
            person=person,
        )
        self.message(
            CHAT_A,
            f"{cat}-{i}-other",
            self.at(cat, i, minutes=5),
            f"My {topic} pick is the {v3}.",
            category=cat,
            person=self.filler_people[i % len(self.filler_people)],
        )
        self.question(
            cat,
            i,
            f"What did {person['display_name']} pick for the {topic}?",
            source="structure",
            groups=[
                Group(id="first_handle", alternatives=[a]),
                Group(id="second_handle", alternatives=[b]),
            ],
        )

    def ambiguous_person(self, i):
        cat = "ambiguous_person"
        thing, attr, v1, v2 = (self.word() for _ in range(4))
        one = self.person(f"bench-twin-{i}-a", channels=(CHAT_A,))
        two = self.person(
            f"bench-twin-{i}-b", display_name=one["display_name"], channels=(CHAT_B,)
        )
        a = self.message(
            CHAT_A,
            f"{cat}-{i}",
            self.at(cat, i),
            f"The {attr} of the {thing} is {v1}.",
            category=cat,
            person=one,
        )
        b = self.message(
            CHAT_B,
            f"{cat}-{i}",
            self.at(cat, i, minutes=90),
            f"The {attr} of the {thing} is {v2}.",
            category=cat,
            person=two,
        )
        self.question(
            cat,
            i,
            f"What did {one['display_name']} say is the {attr} of the {thing}?",
            source="structure",
            groups=[
                Group(id="first_person", alternatives=[a]),
                Group(id="second_person", alternatives=[b]),
            ],
            expects_disclosure=["ambiguous_person"],
        )

    def cross_source_time(self, i):
        cat = "cross_source_time"
        event, thing, value, thing2, value2 = (self.word() for _ in range(5))
        start = self.at(cat, i)
        anchor = self.message(
            NOTES,
            f"{cat}-{i}",
            start,
            f"Began the {event} session.",
            category=cat,
            ended=start + timedelta(hours=1),
        )
        answer = self.message(
            self.chat(i),
            f"{cat}-{i}",
            start + timedelta(minutes=20),
            f"The {thing} came out {value}.",
            category=cat,
        )
        self.message(
            self.chat(i),
            f"{cat}-{i}-later",
            start + timedelta(hours=5),
            f"The {thing2} came out {value2}.",
            category=cat,
            thread=f"{cat}-{i}-later",
        )
        self.question(
            cat,
            i,
            f"What was there during the {event}?",
            source="structure",
            groups=[
                Group(id="anchor", alternatives=[anchor]),
                Group(id="same_time", alternatives=[answer]),
            ],
        )

    def context_dependent(self, i):
        cat = "context_dependent"
        thing, attr, value = (self.word() for _ in range(3))
        channel = self.chat(i)
        ask = self.message(
            channel,
            f"{cat}-{i}-ask",
            self.at(cat, i),
            f"Which {attr} do we use for the {thing}?",
            category=cat,
            thread=f"{cat}-{i}",
            person=self.filler_people[0],
        )
        reply = self.message(
            channel,
            f"{cat}-{i}-reply",
            self.at(cat, i, minutes=1),
            f"The {value} one, same as before.",
            category=cat,
            thread=f"{cat}-{i}",
            person=self.filler_people[1],
        )
        self.message(
            channel,
            f"{cat}-{i}-after",
            self.at(cat, i, minutes=2),
            f"Fine, and bring the {self.word()} too.",
            category=cat,
            thread=f"{cat}-{i}",
            person=self.filler_people[0],
        )
        self.question(
            cat,
            i,
            f"Which {attr} do we use for the {thing}?",
            groups=[
                Group(id="answer_turn", alternatives=[reply]),
                Group(id="asking_turn", alternatives=[ask]),
            ],
        )

    def changed_fact(self, i):
        cat = "changed_fact"
        thing, attr, v1, v2 = (self.word() for _ in range(4))
        old = self.message(
            self.chat(i),
            f"{cat}-{i}-old",
            self.at(cat, i),
            f"The {attr} of the {thing} is {v1}.",
            category=cat,
            thread=f"{cat}-{i}",
        )
        new = self.message(
            self.chat(i),
            f"{cat}-{i}-new",
            self.at(cat, i, days=30),
            f"The {attr} of the {thing} is now {v2}.",
            category=cat,
            thread=f"{cat}-{i}",
        )
        self.question(
            cat,
            i,
            f"What is the {attr} of the {thing} now?",
            groups=[Group(id="latest", alternatives=[new])],
            helpful=[old],
        )

    def no_answer(self, i):
        cat = "no_answer"
        thing, attr, absent, value = (self.word() for _ in range(4))
        self.message(
            self.chat(i),
            f"{cat}-{i}",
            self.at(cat, i),
            f"The {attr} of the {thing} is {value}.",
            category=cat,
        )
        self.question(cat, i, f"What is the {absent} of the {thing}?", no_answer=True)

    def duplicate_cluster(self, i):
        cat = "duplicate_cluster"
        experience = self.word()
        places = [self.word() for _ in range(3)]
        clusters, groups, unsure = [], [], []
        for n, place in enumerate(places):
            day = self.at(cat, i, days=n * 3)
            members = [
                self.message(
                    CHAT_A,
                    f"{cat}-{i}-{n}-a",
                    day,
                    f"Did the {experience} at the {place} today.",
                    category=cat,
                ),
                self.message(
                    CHAT_B,
                    f"{cat}-{i}-{n}-b",
                    day + timedelta(minutes=40),
                    f"The {experience} at the {place} went well.",
                    category=cat,
                ),
                self.message(
                    NOTES,
                    f"{cat}-{i}-{n}-n",
                    day + timedelta(hours=3),
                    f"Log: {experience}, {place}.",
                    category=cat,
                ),
            ]
            ambiguous = []
            if n == 0:
                # Could be this visit or a fourth one nobody else mentions.
                ambiguous.append(
                    self.message(
                        NOTES,
                        f"{cat}-{i}-maybe",
                        day + timedelta(days=1),
                        f"Another {experience}, the place may have been the {place}.",
                        category=cat,
                    )
                )
                unsure += ambiguous
            clusters.append(
                Cluster(id=f"occurrence_{n}", members=members, ambiguous=ambiguous)
            )
            groups.append(Group(id=f"occurrence_{n}", alternatives=members))
        self.question(
            cat,
            i,
            f"When was the {experience}?",
            source="structure",
            groups=groups,
            clusters=clusters,
            helpful=unsure,
        )

    def pattern(self, i):
        cat = "pattern"
        activity, outcome, other = (self.word() for _ in range(3))
        supporting = [
            self.message(
                self.chat(n),
                f"{cat}-{i}-s{n}",
                self.at(cat, i, minutes=n * 60),
                f"Did the {activity} today, then the {outcome}.",
                category=cat,
                thread=f"{cat}-{i}-s{n}",
            )
            for n in range(3)
        ]
        counter = [
            self.message(
                self.chat(n),
                f"{cat}-{i}-c{n}",
                self.at(cat, i, days=1, minutes=n * 60),
                f"Did the {activity} today, then only the {other}.",
                category=cat,
                thread=f"{cat}-{i}-c{n}",
            )
            for n in range(2)
        ]
        comparison = [
            self.message(
                NOTES,
                f"{cat}-{i}-k{n}",
                self.at(cat, i, days=2, minutes=n * 60),
                f"No {activity} today and still the {outcome}.",
                category=cat,
            )
            for n in range(2)
        ]
        self.question(
            cat,
            i,
            f"Is the {outcome} there with the {activity}?",
            source="structure",
            groups=[
                Group(id="supporting", alternatives=supporting),
                Group(id="counterexample", alternatives=counter),
                Group(id="comparison", alternatives=comparison),
            ],
            roles=Roles(
                supporting=supporting, counterexample=counter, comparison=comparison
            ),
        )

    def page(self, external_id, revisions, *, category, sources, mutation=None):
        """Revisions of one page: same external_id, increasing happened_at."""
        for at, content in revisions:
            self.message(
                WIKI,
                external_id,
                at,
                content,
                category=category,
                thread="bench-wiki",
                extra={"body": content, "sources": [list(s) for s in sources]},
            )
        self.corpus.pages.append(
            dict(key=(WIKI, external_id), sources=list(sources), mutation=mutation)
        )
        return (WIKI, external_id)

    def page_assisted(self, i):
        cat = "page_assisted"
        raw_attr, raw_thing, value, title, topic = (self.word() for _ in range(5))
        first = self.message(
            self.chat(i),
            f"{cat}-{i}",
            self.at(cat, i),
            f"The {raw_attr} of the {raw_thing} is {value}.",
            category=cat,
        )
        second = self.message(
            NOTES,
            f"{cat}-{i}",
            self.at(cat, i, minutes=45),
            f"Checked the {raw_thing} {raw_attr} again, {value}.",
            category=cat,
        )
        page = self.page(
            f"{cat}-{i}",
            [
                (self.at(cat, i, days=1), f"{title} {topic}. A page was opened."),
                (
                    self.at(cat, i, days=2),
                    f"{title} {topic}. The {raw_attr} of the {raw_thing} was settled.",
                ),
            ],
            category=cat,
            sources=[first, second],
        )
        # Only the page shares words with the question; the raw records do not.
        self.question(
            cat,
            i,
            f"What was settled about the {title} {topic}?",
            groups=[Group(id="raw", alternatives=[first, second])],
            helpful=[page],
        )

    def page_mutation(self, i):
        cat = "page_mutation"
        mutation = MUTATIONS[i % len(MUTATIONS)]
        thing, attr, v1, v2, wrong = (self.word() for _ in range(5))
        old = self.message(
            self.chat(i),
            f"{cat}-{i}-old",
            self.at(cat, i),
            f"The {attr} of the {thing} is {v1}.",
            category=cat,
            thread=f"{cat}-{i}",
        )
        new = self.message(
            self.chat(i),
            f"{cat}-{i}-new",
            self.at(cat, i, days=10),
            f"The {attr} of the {thing} is now {v2}.",
            category=cat,
            thread=f"{cat}-{i}",
        )
        helpful = [old]
        if mutation == "stale":
            helpful.append(
                self.page(
                    f"{cat}-{i}",
                    [(self.at(cat, i, days=1), f"{thing}: the {attr} is {v1}.")],
                    category=cat,
                    sources=[old],
                    mutation=mutation,
                )
            )
        elif mutation == "contradictory":
            helpful.append(
                self.page(
                    f"{cat}-{i}",
                    [(self.at(cat, i, days=11), f"{thing}: the {attr} is {wrong}.")],
                    category=cat,
                    sources=[new],
                    mutation=mutation,
                )
            )
        elif mutation == "wrong":
            unrelated = self.message(
                NOTES,
                f"{cat}-{i}-unrelated",
                self.at(cat, i, days=5),
                f"The {self.word()} of the {self.word()} is {wrong}.",
                category=cat,
            )
            self.page(
                f"{cat}-{i}",
                [(self.at(cat, i, days=11), f"{thing}: the {attr} is {wrong}.")],
                category=cat,
                sources=[unrelated],
                mutation=mutation,
            )
        self.question(
            cat,
            i,
            f"What is the {attr} of the {thing} now?",
            variant=mutation,
            groups=[Group(id="raw_latest", alternatives=[new])],
            helpful=helpful,
            expects_disclosure=[] if mutation == "missing" else [f"{mutation}_page"],
        )

    def temporal(self, i):
        cat = "temporal"
        variant = TEMPORAL_VARIANTS[i % len(TEMPORAL_VARIANTS)]
        thing, attr, v1, v2 = (self.word() for _ in range(4))
        at = self.at(cat, i)
        if variant == "subject_time":
            # Written long after the day it is about; a second note is written
            # on that day but is about another time.
            day = at - timedelta(days=200)
            about = self.message(
                NOTES,
                f"{cat}-{i}",
                at,
                f"Looking back, the {attr} of the {thing} was {v1}.",
                category=cat,
                subject=dict(
                    start=day.isoformat(),
                    end=(day + timedelta(days=1)).isoformat(),
                    start_precision="day",
                    end_precision="day",
                ),
            )
            elsewhere = day - timedelta(days=300)
            self.message(
                NOTES,
                f"{cat}-{i}-other",
                day + timedelta(hours=2),
                f"Long ago the {attr} of the {thing} was {v2}.",
                category=cat,
                subject=dict(
                    start=elsewhere.isoformat(),
                    end=(elsewhere + timedelta(days=1)).isoformat(),
                    start_precision="day",
                    end_precision="day",
                ),
            )
            self.question(
                cat,
                i,
                f"What was the {attr} of the {thing} on {day.date().isoformat()}?",
                variant=variant,
                after=day,
                before=day + timedelta(days=1),
                groups=[Group(id="about_that_day", alternatives=[about])],
                needs=["subject_time"],
            )
        elif variant == "revision_as_of":
            self.page(
                f"{cat}-{i}",
                [
                    (at, f"{thing}: the {attr} is {v1}."),
                    (at + timedelta(days=20), f"{thing}: the {attr} is {v2}."),
                ],
                category=cat,
                sources=[],
            )
            self.question(
                cat,
                i,
                f"What did the {thing} page give as the {attr}?",
                variant=variant,
                as_of=at + timedelta(days=10),
                cutoff=at + timedelta(days=10),
                groups=[
                    Group(id="revision_then", alternatives=[(WIKI, f"{cat}-{i}", 1)])
                ],
                forbidden=[(WIKI, f"{cat}-{i}", 2)],
                needs=["as_of", "versions"],
            )
        elif variant == "interval_relationship":
            role = attr
            first = self.message(
                self.chat(i),
                f"{cat}-{i}-first",
                at,
                f"From today {v1} has the {role}.",
                category=cat,
                thread=f"{cat}-{i}",
            )
            second = self.message(
                self.chat(i),
                f"{cat}-{i}-second",
                at + timedelta(days=30),
                f"From today {v2} has the {role}.",
                category=cat,
                thread=f"{cat}-{i}",
            )
            target = dict(kind="bench_role", external_ref=role, name=role)
            self.link(
                "bench_holds",
                first,
                target,
                validity=dict(
                    valid_from=at.isoformat(),
                    valid_to=(at + timedelta(days=30)).isoformat(),
                ),
            )
            self.link(
                "bench_holds",
                second,
                target,
                validity=dict(valid_from=(at + timedelta(days=30)).isoformat()),
            )
            self.question(
                cat,
                i,
                f"Who has the {role}?",
                variant=variant,
                as_of=at + timedelta(days=15),
                cutoff=at + timedelta(days=15),
                groups=[Group(id="holder_then", alternatives=[first])],
                forbidden=[second],
                needs=["as_of"],
            )
        else:
            # The correction happened early but is only loaded in phase 2.
            early = self.message(
                self.chat(i),
                f"{cat}-{i}-early",
                at,
                f"The {attr} of the {thing} is {v1}.",
                category=cat,
                thread=f"{cat}-{i}",
            )
            late = self.message(
                self.chat(i),
                f"{cat}-{i}-late",
                at + timedelta(days=1),
                f"The {attr} of the {thing} is now {v2}.",
                category=cat,
                thread=f"{cat}-{i}",
                phase=2,
            )
            if (i // len(TEMPORAL_VARIANTS)) % 2:
                # Asked today: evidence that arrived late must be found.
                self.question(
                    cat,
                    i,
                    f"What is the {attr} of the {thing} now?",
                    variant=variant,
                    groups=[Group(id="late_evidence", alternatives=[late])],
                    helpful=[early],
                )
            else:
                self.question(
                    cat,
                    i,
                    f"What is the {attr} of the {thing} now?",
                    variant=variant,
                    known_by_phase=1,
                    groups=[Group(id="known_then", alternatives=[early])],
                    forbidden=[late],
                    needs=["known_by"],
                )

    def leakage(self, i):
        cat = "leakage"
        variant = LEAKAGE_VARIANTS[i % len(LEAKAGE_VARIANTS)]
        thing, attr, v1, v2 = (self.word() for _ in range(4))
        at = self.at(cat, i)
        cutoff = at + timedelta(days=20)
        if variant == "link":
            event, thing2 = self.word(), self.word()
            anchor = self.message(
                NOTES,
                f"{cat}-{i}",
                at,
                f"Began the {event} log.",
                category=cat,
            )
            known = self.message(
                self.chat(i),
                f"{cat}-{i}-known",
                at + timedelta(days=1),
                f"The {thing} read {v1}.",
                category=cat,
                thread=f"{cat}-{i}-known",
            )
            later = self.message(
                self.chat(i),
                f"{cat}-{i}-later",
                at + timedelta(days=2),
                f"The {thing2} read {v2}.",
                category=cat,
                thread=f"{cat}-{i}-later",
            )
            self.link("bench_about", known, anchor)
            # Both records are old; only the link between them is new.
            self.link("bench_about", later, anchor, phase=2)
            self.question(
                cat,
                i,
                f"What is linked to the {event} log?",
                variant=variant,
                source="structure",
                known_by_phase=1,
                groups=[
                    Group(id="anchor", alternatives=[anchor]),
                    Group(id="linked_then", alternatives=[known]),
                ],
                forbidden=[later],
                needs=["known_by", "links"],
            )
            return
        before = self.message(
            self.chat(i),
            f"{cat}-{i}-before",
            at,
            f"The {attr} of the {thing} is {v1}.",
            category=cat,
            thread=f"{cat}-{i}",
        )
        after = self.message(
            self.chat(i),
            f"{cat}-{i}-after",
            at + timedelta(days=40),
            f"The {attr} of the {thing} is now {v2}.",
            category=cat,
            thread=f"{cat}-{i}",
        )
        forbidden, helpful = [after], []
        if variant == "page_revision":
            self.page(
                f"{cat}-{i}",
                [
                    (at + timedelta(days=1), f"{thing}: the {attr} is {v1}."),
                    (at + timedelta(days=41), f"{thing}: the {attr} is {v2}."),
                ],
                category=cat,
                sources=[before, after],
            )
            helpful.append((WIKI, f"{cat}-{i}", 1))
            forbidden.append((WIKI, f"{cat}-{i}", 2))
        self.question(
            cat,
            i,
            f"What is the {attr} of the {thing}?",
            variant=variant,
            as_of=cutoff,
            cutoff=cutoff,
            groups=[Group(id="known_then", alternatives=[before])],
            helpful=helpful,
            forbidden=forbidden,
            needs=["as_of"],
        )

    def import_gap(self, i):
        cat = "import_gap"
        thing, attr = self.word(), self.word()
        inside = GAP[0] + timedelta(days=2 + i * 4)
        pointer = self.message(
            NOTES,
            f"{cat}-{i}",
            inside + timedelta(hours=1),
            f"The {thing} talk moved to the other chat.",
            category=cat,
        )
        self.question(
            cat,
            i,
            f"What is the {attr} of the {thing}?",
            source="structure",
            after=inside,
            before=inside + timedelta(days=1),
            no_answer=True,
            expects_incomplete=True,
            expects_disclosure=["import_gap"],
            helpful=[pointer],
        )

    def filler(self, count):
        """Distractors in filler vocabulary, spread over the whole timeline."""
        span = (END - BASE - timedelta(days=60)).total_seconds()
        for n in range(count):
            channel = (CHAT_A, CHAT_B, NOTES)[n % 3]
            at = BASE + timedelta(seconds=int(self.rng.random() * span))
            if channel == CHAT_B and GAP[0] <= at < GAP[1]:
                at += GAP[1] - GAP[0]
            words = self.rng.sample(self.fillers, 5)
            self.message(
                channel,
                f"filler-{n}",
                at,
                f"{words[0].capitalize()} and {words[1]} by the {words[2]},"
                f" then {words[3]} with {words[4]}.",
                category="filler",
                thread=f"filler-{channel.rsplit('.', 1)[1]}-{n // 120}",
            )


class _Behaviours:
    """The behaviour segments.

    Uses the twin builder's word source (so no word is ever reused) with a
    generator of its own, seeded from the twin's seed only: the segments are
    the same at every scale and the twin's own stream is never touched.
    """

    def __init__(self, builder, seed):
        self.b = builder
        self.rng = random.Random(seed * 1_000_003 + 7)
        builder.word.rng = self.rng
        self.corpus = builder.corpus

    def word(self):
        return self.b.word()

    def person(self, key, accounts=True):
        handle = f"bench-behaviour-{key}"
        name = f"{self.word().capitalize()} {self.word().capitalize()}"
        person = dict(
            handle=handle,
            display_name=name,
            accounts=[
                dict(system=BEHAVIOUR_SYSTEM, kind="account", value=f"{handle}-{BEHAVIOUR_SYSTEM}")
            ]
            if accounts
            else [],
        )
        self.corpus.people.append(person)
        return person

    def message(
        self,
        scenario,
        channel,
        external_id,
        at,
        content,
        *,
        thread=None,
        author=None,
        phase=1,
        held=False,
        extra=None,
    ):
        data = dict(
            channel=channel,
            external_id=external_id,
            thread={"external_id": thread or external_id},
            happened_at=at.isoformat(),
            content=content,
            payload={"synthetic": True, "bench": "behaviour_" + scenario, **(extra or {})},
        )
        if author is not None:
            data["author"] = dict(author)
        if held:
            self.corpus.held.setdefault(scenario, []).append(data)
        else:
            self.corpus.messages.append(data)
            self.corpus.phases.append(phase)
        return (channel, external_id)

    @staticmethod
    def account(person):
        return dict(person["accounts"][0])

    def channel(self, name, description, **extra):
        self.corpus.channels.append(
            dict(name=name, plugin=PLUGIN, description=description, **extra)
        )

    def question(self, scenario, text, number=0, **labels):
        labels.setdefault("cutoff", END)
        labels.setdefault("source", "structure")
        question = Question(
            id=f"behaviour_{scenario}-{number:03d}",
            text=text,
            category="behaviour_" + scenario,
            **labels,
        )
        self.corpus.questions.append(question)
        return question

    def segment(self, scenario, day, **info):
        start = BEHAVIOUR_BASE + timedelta(days=day)
        self.corpus.behaviours[scenario] = dict(start=start, needs=[], **info)
        return start

    # -- the six scenarios checked here ---------------------------------------

    def episode_count(self):
        name = "episode_count"
        start = self.segment(name, 0)
        self.channel(J_EPISODES, "Synthetic chat for the episode count")
        topic = self.word()
        people = [self.person("episodes-a"), self.person("episodes-b")]
        incidents, keys = [], []
        for n in range(3):
            at = start + timedelta(hours=6 * n)
            texts = (
                (0, f"The {topic} is back."),
                (4, f"Still the {topic}, now with {self.word()}."),
                (9, f"The {topic} is over for now."),
            )
            members = [
                self.message(
                    name,
                    J_EPISODES,
                    f"behaviour-episode-{n}-{m}",
                    at + timedelta(minutes=minutes),
                    text,
                    thread="behaviour-episodes",
                    author=self.account(people[m % 2]),
                )
                for m, (minutes, text) in enumerate(texts)
            ]
            incidents.append(members)
            keys += members
            # Chatter between incidents, without the topic.
            self.message(
                name,
                J_EPISODES,
                f"behaviour-episode-{n}-chatter",
                at + timedelta(hours=3),
                f"{self.word().capitalize()} and {self.word()}.",
                thread="behaviour-episodes",
                author=self.account(people[n % 2]),
            )
        self.corpus.behaviours[name].update(
            channels=[J_EPISODES], topic=topic, incidents=incidents, keys=keys
        )
        self.question(
            name,
            f"How many times was the {topic} there?",
            groups=[
                Group(id=f"incident_{n}", alternatives=members)
                for n, members in enumerate(incidents)
            ],
            expects_incidents=3,
        )

    def newest_relevant(self):
        name = "newest_relevant"
        start = self.segment(name, 2)
        self.channel(J_THREAD, "Synthetic chat for the newest relevant answer")
        one, two = self.person("newest-a"), self.person("newest-b")
        thing, attr, v1, v2 = (self.word() for _ in range(4))
        old = self.message(
            name,
            J_THREAD,
            "behaviour-newest-old",
            start,
            f"The {attr} of the {thing} is {v1}.",
            thread="behaviour-newest",
            author=self.account(one),
        )
        answer = self.message(
            name,
            J_THREAD,
            "behaviour-newest-answer",
            start + timedelta(days=1),
            f"The {attr} of the {thing} is now {v2}.",
            thread="behaviour-newest",
            author=self.account(two),
        )
        chatter = self.message(
            name,
            J_THREAD,
            "behaviour-newest-chatter",
            start + timedelta(days=1, hours=2),
            f"Fine, {self.word()} and {self.word()} then.",
            thread="behaviour-newest",
            author=self.account(one),
        )
        self.corpus.behaviours[name].update(
            channels=[J_THREAD],
            query=f"{attr} {thing}",
            old=old,
            answer=answer,
            newest_absolute=chatter,
        )
        self.question(
            name,
            f"What is the {attr} of the {thing} now?",
            groups=[Group(id="latest", alternatives=[answer])],
            helpful=[old],
            expects_newest=answer,
        )

    def consumer_exactly_once(self):
        name = "consumer_exactly_once"
        start = self.segment(name, 4)
        self.channel(J_FEED, "Synthetic feed read by a consumer")
        person = self.person("feed")
        first = [
            self.message(
                name,
                J_FEED,
                f"behaviour-feed-{n}",
                start + timedelta(hours=n),
                f"Feed {self.word()} {n + 1}.",
                thread="behaviour-feed",
                author=self.account(person),
            )
            for n in range(3)
        ]
        # Backfilled after the consumer has read: older record times, later
        # arrival. Loaded by the behaviour, never by load().
        late = [
            self.message(
                name,
                J_FEED,
                f"behaviour-feed-late-{n}",
                start - timedelta(days=1) + timedelta(hours=n),
                f"Feed {self.word()} late {n + 1}.",
                thread="behaviour-feed",
                author=self.account(person),
                held=True,
            )
            for n in range(3)
        ]
        self.corpus.behaviours[name].update(
            channels=[J_FEED], first=first, late=late, needs=["consumers"]
        )

    def coverage_honesty(self):
        name = "coverage_honesty"
        start = self.segment(name, 6)
        self.channel(J_PARTIAL, "Synthetic chat with a reported coverage gap")
        self.channel(J_COMPLETE, "Synthetic chat with complete coverage")
        window = (start, start + timedelta(days=10))
        gap = (start + timedelta(days=3), start + timedelta(days=6))
        person = self.person("coverage")
        for channel in (J_PARTIAL, J_COMPLETE):
            for n, day in enumerate((1, 8)):
                self.message(
                    name,
                    channel,
                    f"behaviour-coverage-{n}",
                    start + timedelta(days=day),
                    f"The {self.word()} was {self.word()}.",
                    thread="behaviour-coverage",
                    author=self.account(person),
                )
        event, other = self.word(), self.word()
        self.corpus.behaviours[name].update(
            channels=[J_PARTIAL, J_COMPLETE],
            window=window,
            gap=gap,
            event=event,
            other_event=other,
            needs=["coverage"],
        )
        self.question(
            name,
            f"What happened at the {event}?",
            variant="inside_gap",
            after=gap[0],
            before=gap[1],
            no_answer=True,
            expects_absence_statement="not_found_in_coverage",
        )
        self.question(
            name,
            f"What happened at the {other}?",
            number=1,
            variant="complete_channel",
            after=gap[0],
            before=gap[1],
            no_answer=True,
            expects_absence_statement="not_found_in_covered_sources",
        )

    def link_vocabulary(self):
        name = "link_vocabulary"
        start = self.segment(name, 8)
        self.channel(J_LINKS, "Synthetic chat whose messages are linked")
        person = self.person("links")
        keys = [
            self.message(
                name,
                J_LINKS,
                f"behaviour-links-{n}",
                start + timedelta(minutes=10 * n),
                f"The {self.word()} note {n + 1}.",
                thread="behaviour-links",
                author=self.account(person),
            )
            for n in range(4)
        ]
        canonical, synonym, candidate, free = (self.word() for _ in range(4))
        self.corpus.behaviours[name].update(
            channels=[J_LINKS],
            keys=keys,
            canonical=f"bench_{canonical}",
            synonym=f"bench_{synonym}",
            synonym_raw=f"Bench {synonym.capitalize()}",
            candidate=f"bench_{candidate}",
            free_text=f"bench {free} as written",
            needs=["link_types", "principals"],
        )

    def restricted_inference(self):
        name = "restricted_inference"
        start = self.segment(name, 10)
        self.channel(J_OPEN, "Synthetic chat a restricted asker may read")
        self.channel(J_HIDDEN, "Synthetic chat hidden from the restricted asker")
        canary = self.word()
        person = self.person("restricted")
        visible = [
            self.message(
                name,
                J_OPEN,
                f"behaviour-open-{n}",
                start + timedelta(minutes=5 * n),
                f"The {canary} is open {n + 1}.",
                thread="behaviour-open",
                author=self.account(person),
            )
            for n in range(3)
        ]
        hidden = [
            self.message(
                name,
                J_HIDDEN,
                f"behaviour-hidden-{n}",
                start + timedelta(hours=8 * (n // 2), minutes=5 * n),
                f"The {canary} is hidden {n + 1}.",
                thread="behaviour-hidden",
                author=self.account(person),
            )
            for n in range(4)
        ]
        self.corpus.behaviours[name].update(
            channels=[J_OPEN, J_HIDDEN],
            granted=[J_OPEN],
            asker="bench-behaviour-guest",
            canary=canary,
            visible=visible,
            hidden=hidden,
            visible_episodes=1,
            hidden_episodes=2,
            needs=["recall", "principals"],
        )

    # -- planted here, checked by bench-behaviours --------------------------------

    def dyad_vs_author(self):
        name = "dyad_vs_author"
        start = self.segment(name, 12)
        self.channel(J_DM, "Synthetic direct messages of two people")
        self.channel(J_GROUP, "Synthetic group chat of three people")
        self.channel(J_OWNER, "Synthetic chat of the owner with one person")
        a, b, c = (self.person(f"dyad-{x}") for x in "abc")
        owner = self.person("owner", accounts=False)
        topic = self.word()
        keys = dict(dm=[], group=[], owner=[])
        for n in range(4):
            keys["dm"].append(
                self.message(
                    name,
                    J_DM,
                    f"behaviour-dm-{n}",
                    start + timedelta(minutes=3 * n),
                    f"The {topic} part {n + 1}.",
                    thread="behaviour-dm",
                    author=self.account((a, b)[n % 2]),
                )
            )
        for n in range(6):
            keys["group"].append(
                self.message(
                    name,
                    J_GROUP,
                    f"behaviour-group-{n}",
                    start + timedelta(hours=2, minutes=3 * n),
                    f"Also the {topic} {n + 1}.",
                    thread="behaviour-group",
                    author=self.account((a, b, c)[n % 3]),
                )
            )
        for n in range(4):
            outgoing = n % 2 == 0
            keys["owner"].append(
                self.message(
                    name,
                    J_OWNER,
                    f"behaviour-owner-{n}",
                    start + timedelta(hours=4, minutes=3 * n),
                    f"Back to the {topic} {n + 1}.",
                    thread="behaviour-owner",
                    author=None if outgoing else self.account(b),
                    extra=dict(direction="outgoing" if outgoing else "incoming"),
                )
            )
        self.corpus.behaviours[name].update(
            channels=[J_DM, J_GROUP, J_OWNER],
            topic=topic,
            people=dict(a=a["handle"], b=b["handle"], c=c["handle"]),
            owner=owner["handle"],
            keys=keys,
            needs=["person_role_between"],
        )

    def open_loops(self):
        name = "open_loops"
        start = self.segment(name, 14)
        self.channel(J_LOOPS, "Synthetic chat with asks and answers")
        other = self.person("loops")

        def say(key, at, text, outgoing):
            return self.message(
                name,
                J_LOOPS,
                f"behaviour-loops-{key}",
                at,
                text,
                thread=f"behaviour-loops-{key.split('-')[0]}",
                author=None if outgoing else self.account(other),
                extra=dict(direction="outgoing" if outgoing else "incoming"),
            )

        loops = dict(
            owner_owes=say("a-ask", start, f"Can you send the {self.word()}?", False),
            they_owe=say(
                "b-ask",
                start + timedelta(hours=1),
                f"Can you check the {self.word()}?",
                True,
            ),
        )
        thing = self.word()
        answered = [
            say(
                "c-ask", start + timedelta(hours=2), f"Can you send the {thing}?", False
            ),
            say("c-reply", start + timedelta(hours=3), f"Sent the {thing}.", True),
        ]
        wound = [
            say("d-0", start + timedelta(hours=4), "Fine, see you.", False),
            say("d-1", start + timedelta(hours=4, minutes=2), "See you.", True),
        ]
        self.corpus.behaviours[name].update(
            channels=[J_LOOPS],
            person=other["handle"],
            open=loops,
            answered=answered,
            wound_down=wound,
            unanswered_after_s=int(timedelta(days=1).total_seconds()),
            needs=["open_loops"],
        )

    def media_contaminated(self):
        name = "media_contaminated"
        start = self.segment(name, 16)
        self.channel(J_MEDIA, "Synthetic media captions")
        self.channel(
            J_TRANSCRIPT,
            "Synthetic transcript, one thread per recording",
            conversation_whole_thread=True,
        )
        speaker = self.person("speaker")
        words = [self.word() for _ in range(4)]
        caption = " ".join(words)
        media = self.message(
            name,
            J_MEDIA,
            "behaviour-media-0",
            start,
            f"Caption: {caption}.",
            thread="behaviour-media",
            extra=dict(media=True),
        )
        played = [
            self.message(
                name,
                J_TRANSCRIPT,
                f"behaviour-transcript-played-{n}",
                start + timedelta(seconds=20 * n),
                f"{caption.capitalize()}.",
                thread="behaviour-transcript",
                author=self.account(speaker),
            )
            for n in range(2)
        ]
        discussed = [
            self.message(
                name,
                J_TRANSCRIPT,
                f"behaviour-transcript-talk-{n}",
                start + timedelta(minutes=2, seconds=20 * n),
                f"That {words[n]} part was good.",
                thread="behaviour-transcript",
                author=self.account(speaker),
            )
            for n in range(2)
        ]
        self.corpus.behaviours[name].update(
            channels=[J_MEDIA, J_TRANSCRIPT],
            media=media,
            played=played,
            discussed=discussed,
            needs=["media_noise"],
        )

    def unenrolled_voice(self):
        name = "unenrolled_voice"
        start = self.segment(name, 18)
        self.channel(J_VOICE, "Synthetic transcript with unnamed speakers")
        labels = [f"bench-behaviour-voice-{n}" for n in range(2)]
        keys = [
            self.message(
                name,
                J_VOICE,
                f"behaviour-voice-{n}",
                start + timedelta(seconds=30 * n),
                f"The {self.word()} is {self.word()}.",
                thread="behaviour-voice",
                author=dict(
                    system=BEHAVIOUR_SYSTEM, kind="speaker_label", value=labels[n % 2]
                ),
            )
            for n in range(4)
        ]
        self.corpus.behaviours[name].update(
            channels=[J_VOICE], labels=labels, keys=keys, needs=["speaker_inbox"]
        )

    def suspected_link(self):
        name = "suspected_link"
        start = self.segment(name, 20)
        self.channel(J_SUSPECT, "Synthetic chat with a suspected account")
        person = self.person("suspect")
        alt = dict(system=BEHAVIOUR_SYSTEM, kind="account", value="bench-behaviour-suspect-alt")
        person["accounts"].append(alt)
        own = [
            self.message(
                name,
                J_SUSPECT,
                f"behaviour-suspect-own-{n}",
                start + timedelta(minutes=n),
                f"The {self.word()} is mine.",
                thread="behaviour-suspect",
                author=self.account(person),
            )
            for n in range(2)
        ]
        suspected = [
            self.message(
                name,
                J_SUSPECT,
                f"behaviour-suspect-alt-{n}",
                start + timedelta(minutes=10 + n),
                f"The {self.word()} is maybe mine.",
                thread="behaviour-suspect",
                author=alt,
            )
            for n in range(2)
        ]
        self.corpus.behaviours[name].update(
            channels=[J_SUSPECT],
            person=person["handle"],
            identifier=(alt["system"], alt["value"]),
            own=own,
            suspected=suspected,
            needs=["identifier_status"],
        )

    def write_replay(self):
        name = "write_replay"
        start = self.segment(name, 22)
        self.channel(J_WRITES, "Synthetic chat written to twice")
        person = self.person("writes")
        keys = [
            self.message(
                name,
                J_WRITES,
                f"behaviour-writes-{n}",
                start + timedelta(minutes=n),
                f"The {self.word()} is {self.word()}.",
                thread="behaviour-writes",
                author=self.account(person),
            )
            for n in range(3)
        ]
        self.corpus.behaviours[name].update(
            channels=[J_WRITES], keys=keys, needs=["idempotency"]
        )

    def build(self):
        for scenario in BEHAVIOUR_SCENARIOS:
            getattr(self, scenario)()


def build(seed=47, scale=1):
    """The corpus for a seed. Same seed and scale, same corpus, always.

    scale multiplies only the distractor volume, so quality numbers stay
    comparable while latency is measured on a larger archive.
    """
    if scale < 1:
        raise ValueError("scale must be at least 1")
    builder = _Builder(seed, scale)
    corpus = builder.corpus
    corpus.channels = [
        dict(name=CHAT_A, plugin=PLUGIN, description="Synthetic chat, first app"),
        dict(
            name=CHAT_B,
            plugin=PLUGIN,
            description="Synthetic chat, second app; nothing was imported between "
            f"{GAP[0].date().isoformat()} and {GAP[1].date().isoformat()}",
        ),
        dict(name=NOTES, plugin=PLUGIN, description="Synthetic notes"),
        dict(
            name=WIKI,
            plugin=PLUGIN,
            description="Synthetic pages; one external_id per page, one message per revision",
            is_reference=True,
        ),
    ]
    corpus.gaps = [
        dict(channel=CHAT_B, after=GAP[0].isoformat(), before=GAP[1].isoformat())
    ]
    for category in CATEGORIES:
        plant = getattr(builder, category)
        for item in range(PER_CLASS):
            plant(item)
    builder.filler(FILLER_PER_SCALE * scale)
    _Behaviours(builder, seed).build()
    return corpus


def features(client):
    """What the running system offers, read from its own OpenAPI document."""
    response = client.get("/v1/openapi.json")
    if response.status_code != 200:
        return set()
    document = response.json()
    schemas = document.get("components", {}).get("schemas", {})
    found = set()
    if "record_kind" in schemas.get("Channel", {}).get("properties", {}):
        found.add("record_kind")
    message = (
        schemas.get("Ingest", {})
        .get("properties", {})
        .get("messages", {})
        .get("items", {})
    )
    if "subject" in message.get("properties", {}):
        found.add("subject")
    for path in document.get("paths", {}):
        found.add("path:" + path)
    return found


def _check(response, what):
    if response.status_code != 200:
        raise RuntimeError(f"{what} failed: {response.status_code} {response.text}")
    return response.json()


def _register(client, corpus, found):
    for channel in corpus.channels:
        data = dict(channel)
        if data["name"] == WIKI and "record_kind" in found:
            data["record_kind"] = "page_revision"
        _check(client.post("/v1/channels", json=data), "channel registration")
    _check(
        client.post(
            f"/v1/plugins/{PLUGIN}/manifest",
            json=dict(
                name=PLUGIN,
                version="1.1.0",
                synthetic=True,
                channels=[c["name"] for c in corpus.channels],
                known_source_gaps=corpus.gaps,
            ),
        ),
        "manifest",
    )
    existing = {
        p["handle"]: p for p in _check(client.get("/v1/identity/people"), "people")
    }
    known = {
        (r["system"], r["value"]): r["person_id"]
        for r in _check(client.get("/v1/registry/identifiers"), "identifiers")
    }
    for person in corpus.people:
        row = existing.get(person["handle"]) or _check(
            client.post(
                "/v1/identity/people",
                json=dict(handle=person["handle"], display_name=person["display_name"]),
            ),
            "person",
        )
        for account in person["accounts"]:
            if known.get((account["system"], account["value"])) != row["id"]:
                _check(
                    client.post(
                        "/v1/registry/identifiers",
                        json=dict(
                            person_id=row["id"],
                            value_norm=account["value"],
                            **account,
                        ),
                    ),
                    "identifier",
                )


def load(client, corpus, phase=None, batch=MAX_BATCH):
    """Send the corpus through the ingest API, at most 5,000 messages a call.

    phase=None loads every phase in order; a number loads just that phase, so
    the caller can record a timestamp between phases. Channels and people are
    registered with phase 1. Returns counts per phase.

    Meant for an archive that does not hold the corpus yet. Loading a second
    time adds no record, but pages replay their history: every earlier
    revision differs from the current one and is stored as a further version.
    """
    if not 1 <= batch <= MAX_BATCH:
        raise ValueError(f"batch must be 1..{MAX_BATCH}")
    found = features(client)
    totals = {}
    for number in corpus.phase_numbers() if phase is None else [phase]:
        if number == 1:
            _register(client, corpus, found)
        messages = []
        for data, at in zip(corpus.messages, corpus.phases):
            if at != number:
                continue
            subject = corpus.subjects.get((data["channel"], data["external_id"]))
            if subject and "subject" in found:
                data = dict(data, subject=subject)
            messages.append(data)
        links = [
            link for link, at in zip(corpus.links, corpus.link_phases) if at == number
        ]
        counts = dict(messages=len(messages), links=len(links), batches=0)
        for key in ("inserted", "new_versions", "unchanged", "rejected"):
            counts[key] = 0
        for start in range(0, max(len(messages), 1), batch):
            part = messages[start : start + batch]
            last = start + batch >= len(messages)
            if not part and not (last and links):
                continue
            result = _check(
                client.post(
                    "/v1/ingest/messages",
                    json=dict(
                        plugin=PLUGIN, messages=part, links=links if last else []
                    ),
                ),
                "ingest",
            )
            counts["batches"] += 1
            for key in ("inserted", "new_versions", "unchanged"):
                counts[key] += result[key]
            counts["rejected"] += len(result["rejected"])
            if result["rejected"]:
                raise RuntimeError(f"ingest rejected rows: {result['rejected'][:3]}")
        totals[number] = counts
    return totals


def server_time(client):
    """The database clock, for a known_by cutoff between two load phases."""
    response = client.post("/v1/sql", json={"statement": "SELECT now() AS now"})
    return datetime.fromisoformat(_check(response, "clock")["rows"][0]["now"])


# --- after the load: what a curator or importer adds -----------------------------


def mutate(revisions, mutation, *, at=None, text=None):
    """Page revisions under a mutation, as the generator plants them.

    missing: the page is omitted (no revision); stale: the revisions stop
    before the fact changed (unchanged list); contradictory and wrong: one
    more revision at `at` stating `text` (contradictory cites the right
    source, wrong an unrelated one; the caller chooses the sources).
    """
    if mutation not in MUTATIONS:
        raise ValueError("mutation must be one of " + ", ".join(MUTATIONS))
    revisions = list(revisions)
    if mutation == "missing":
        return []
    if mutation == "stale":
        return revisions
    if at is None or text is None:
        raise ValueError(f"a {mutation} page needs the time and text of its revision")
    return revisions + [(at, text)]


def _sql(client, statement, row_cap=10000):
    response = client.post("/v1/sql", json={"statement": statement, "row_cap": row_cap})
    return _check(response, "sql")["rows"]


def _paths(client):
    response = client.get("/v1/openapi.json")
    if response.status_code != 200:
        return set()
    return set(response.json().get("paths", {}))


def plant(client, corpus, ids=None):
    """Write what a curator or importer adds after the load, over HTTP.

    - provenance from every revision of every bench.wiki page to the page's
      source records (POST /v1/provenance, owner, one call per revision);
    - one typed claim per page with sources (observation; the claim of a
      contradictory page names the raw record it contradicts);
    - the validity interval of every validity-bounded link (PATCH /v1/links);
    - the coverage report of each declared import gap, when the system takes
      coverage reports (POST /v1/plugins/bench/coverage).

    Every step is skipped when its route is absent. Returns counts per step;
    planting twice writes nothing new (provenance and claims are idempotent).
    """
    paths = _paths(client)
    counts = dict(provenance=0, link_validity=0, coverage=0)
    if ids is None:
        keys = [p["key"] for p in corpus.pages]
        keys += [s for p in corpus.pages for s in p["sources"]]
        from .gold import resolve

        ids = resolve(client, keys)
    revisions = {}
    if corpus.pages:
        for row in _sql(
            client,
            "SELECT id,external_id,version FROM api.message_versions"
            f" WHERE channel='{WIKI}' ORDER BY external_id,version",
        ):
            revisions.setdefault(row["external_id"], []).append(row)
    if "/v1/provenance" in paths:
        # A revision rests only on sources that existed when it was written.
        times = {
            (m["channel"], m["external_id"]): m["happened_at"] for m in corpus.messages
        }
        written = {}
        for m in corpus.messages:
            if m["channel"] == WIKI:
                written.setdefault(m["external_id"], []).append(m["happened_at"])
        for page in corpus.pages:
            when = written.get(page["key"][1]) or []
            for number, revision in enumerate(revisions.get(page["key"][1], [])):
                at = when[min(number, len(when) - 1)] if when else None
                sources = [
                    dict(channel=s[0], external_id=s[1])
                    for s in page["sources"]
                    if at is None
                    or datetime.fromisoformat(times.get(tuple(s), at))
                    <= datetime.fromisoformat(at)
                ]
                if not sources:
                    continue
                result = _check(
                    client.post(
                        "/v1/provenance",
                        json=dict(
                            derived=dict(kind="message", id=revision["id"]),
                            sources=sources,
                        ),
                    ),
                    "provenance",
                )
                counts["provenance"] += result["written"]
    if corpus.link_validity and "/v1/links/{link_id}" in paths:
        from .gold import resolve

        ends = {
            number: (
                corpus.links[number]["from"]["channel"],
                corpus.links[number]["from"]["external_id"],
            )
            for number in corpus.link_validity
        }
        found = resolve(client, list(set(ends.values())))
        rows = _sql(
            client, "SELECT id,from_id FROM api.links WHERE type LIKE 'bench_%'"
        )
        for number, validity in sorted(corpus.link_validity.items()):
            source = found.get(ends[number])
            for row in rows:
                if source is not None and row["from_id"] == source:
                    body = dict(validity)
                    body.setdefault("valid_to", None)
                    response = client.patch(f"/v1/links/{row['id']}", json=body)
                    counts["link_validity"] += response.status_code == 200
    if "/v1/plugins/{name}/coverage" in paths:
        reports = [
            dict(
                channel=gap["channel"],
                status="partial",
                reasons=["partial_history"],
                window={"from": BASE.isoformat(), "to": END.isoformat()},
                gaps=[
                    {
                        "from": gap["after"],
                        "to": gap["before"],
                        "reason": "partial_history",
                    }
                ],
            )
            for gap in corpus.gaps
        ]
        response = client.post(
            f"/v1/plugins/{PLUGIN}/coverage", json=dict(reports=reports)
        )
        if response.status_code == 200:
            counts["coverage"] = response.json().get("stored", len(reports))
    corpus.planted = dict(counts)
    return counts


def load_held(client, corpus, scenario):
    """Ingest the rows a scenario keeps back (late backfill): counts."""
    messages = corpus.held.get(scenario) or []
    if not messages:
        return dict(messages=0, inserted=0)
    result = _check(
        client.post("/v1/ingest/messages", json=dict(plugin=PLUGIN, messages=messages)),
        "ingest",
    )
    if result["rejected"]:
        raise RuntimeError(f"ingest rejected rows: {result['rejected'][:3]}")
    return dict(messages=len(messages), inserted=result["inserted"])
