"""The helper without a model (THROUGHLINE_ASSISTANT_PROVIDER=local).

Ask is a finder: recipes, channels and their declared fields, people and full
text are searched for the question's words, and the best match becomes a
starter statement (a time series of a matching field, a weekly count of a
matching channel, the matching person's activity, or a matching recipe).
Fix applies the rule table of sqlfix to what the validator and the database
report. Explain describes the statement's structure. Refine applies a few
phrase rules (a grain, a recent window, a top N). Everything runs through the
same belt as a model would, under the request's scope; nothing leaves the
machine.
"""

import re

from . import sqlfix
from .tools import identifier, literal, overlap, terms

GRAINS = (
    ("hour", ("hour", "hourly")),
    ("day", ("day", "daily", "days")),
    ("week", ("week", "weekly", "weeks")),
    ("month", ("month", "monthly", "months")),
    ("year", ("year", "yearly", "annual", "years")),
)
LAST = re.compile(
    r"\b(?:last|past|previous)\s+(\d{1,4})\s+(day|week|month|year)s?\b", re.IGNORECASE
)
# Fields every plugin adds for its own bookkeeping: never the answer.
BOOKKEEPING = frozenset(("mapping_revision", "provider", "operation_count", "server_timestamp"))
TOP = re.compile(r"\b(?:top|first|limit)\s+(\d{1,5})\b", re.IGNORECASE)


def _grain(question, default="week"):
    # "last 30 days" is a window, not a grain.
    question = LAST.sub(" ", question or "")
    words = set(re.findall(r"[a-z]+", question.lower()))
    for grain, names in GRAINS:
        if words & set(names):
            return grain
    return default


def _parts(name):
    return set(re.split(r"[._\s-]+", (name or "").lower())) - {""}


def _time_bounds(scope):
    """WHERE fragments for the request's time scope (literals, validated by the database)."""
    clauses = []
    scope = scope or {}
    if scope.get("from"):
        clauses.append(
            "happened_at >= " + literal(str(scope["from"])) + "::timestamptz"
        )
    if scope.get("to"):
        clauses.append("happened_at < " + literal(str(scope["to"])) + "::timestamptz")
    return clauses


def _bucket(grain, scope):
    tz = (scope or {}).get("tz")
    column = "happened_at"
    if isinstance(tz, str) and re.fullmatch(
        r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){0,2}|UTC", tz
    ):
        column = f"happened_at AT TIME ZONE {literal(tz)}"
    return f"date_trunc({literal(grain)}, {column})"


class Finder:
    def __init__(self, belt, request, emit):
        self.belt = belt
        self.request = request
        self.emit = emit
        self.catalog = belt.catalog

    def step(self, tool, arguments, outcome=None, **extra):
        """Run one belt tool (or record a computed step) as an event."""
        if outcome is None:
            outcome = self.belt.call(tool, arguments)
        self.emit(tool, arguments, outcome, **extra)
        return outcome

    # -- matching ---------------------------------------------------------------
    def channels(self, wanted):
        scored = []
        for channel in self.catalog.channels():
            name = _parts(channel["name"])
            described = terms(channel["description"] or "")
            score = 3 * overlap(wanted, name) + overlap(wanted, described)
            fields = []
            for f in channel["fields"]:
                hit = 3 * overlap(wanted, _parts(f["name"])) + overlap(
                    wanted, terms(f["description"] or "")
                )
                if f["name"] in BOOKKEEPING:
                    hit = 0
                if hit:
                    fields.append((hit, f))
            fields.sort(key=lambda x: -x[0])
            score += sum(h for h, _ in fields[:3])
            if score:
                scored.append((score, channel, [f for _, f in fields[:5]]))
        scored.sort(key=lambda s: (-s[0], s[1]["name"]))
        return scored

    def people(self, wanted):
        if not wanted:
            return []
        rows = self.belt.run(
            "SELECT id, handle, display_name FROM api.people WHERE merged_into IS NULL ORDER BY id",
            5000,
        )["rows"]
        found = []
        for row in rows:
            names = terms(
                (row.get("display_name") or "")
                + " "
                + (row.get("handle") or "").replace("-", " ")
            )
            hit = len(wanted & names)
            if hit:
                found.append((hit, row))
        found.sort(key=lambda x: (-x[0], x[1]["id"]))
        return [dict(row, matched=hit) for hit, row in found[:5]]

    # -- statements -------------------------------------------------------------
    def field_series(self, channel, field, grain):
        relation = channel["view"] or "api.messages"
        if channel["view"]:
            expr = identifier(field["name"])
        else:
            expr = "(fields ->> " + literal(field["name"]) + ")::float8"
        where = [] if channel["view"] else ["channel = " + literal(channel["name"])]
        where += _time_bounds(self.request.get("scope"))
        statement = (
            f"SELECT {_bucket(grain, self.request.get('scope'))} AS {grain}, avg({expr})::float8 AS {identifier(field['name'])},"
            f" min({expr})::float8 AS lo, max({expr})::float8 AS hi FROM {relation}"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " GROUP BY 1 ORDER BY 1"
        )
        encode = dict(x=grain, y=field["name"], lo="lo", hi="hi")
        return (
            statement,
            "timeseries",
            encode,
            f"{field['name']} per {grain} in {channel['name']}",
        )

    def channel_counts(self, channel, grain):
        relation = channel["view"] or "api.messages"
        where = [] if channel["view"] else ["channel = " + literal(channel["name"])]
        where += _time_bounds(self.request.get("scope"))
        statement = (
            f"SELECT {_bucket(grain, self.request.get('scope'))} AS {grain}, count(*)::bigint AS records FROM {relation}"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " GROUP BY 1 ORDER BY 1"
        )
        return (
            statement,
            "spine",
            dict(x=grain, y="records"),
            f"records per {grain} in {channel['name']}",
        )

    def person_counts(self, person, grain):
        where = ["person_id = " + str(int(person["id"]))] + _time_bounds(
            self.request.get("scope")
        )
        statement = (
            f"SELECT {_bucket(grain, self.request.get('scope'))} AS {grain}, channel, count(*)::bigint AS records"
            " FROM api.messages WHERE "
            + " AND ".join(where)
            + " GROUP BY 1, 2 ORDER BY 1, 2"
        )
        return (
            statement,
            "timeseries",
            dict(x=grain, y="records", series="channel"),
            f"records per {grain} by this person",
        )

    def overview(self):
        where = _time_bounds(self.request.get("scope"))
        statement = (
            "SELECT channel, count(*)::bigint AS records FROM api.messages"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " GROUP BY channel ORDER BY records DESC LIMIT 30"
        )
        return (
            statement,
            "top_values",
            dict(label="channel", value="records"),
            "records per channel",
        )

    def first_valid(self, candidates):
        for statement, lens, encode, title, channel in candidates:
            if not statement:
                continue
            check = self.belt.validate(statement)
            if check["valid"]:
                spec, _ = self.belt.spec(
                    statement, lens, encode, title=title, channel=channel
                )
                return statement, spec, check
        return None, None, None

    # -- verbs ------------------------------------------------------------------
    def ask(self):
        question = self.request.get("question") or ""
        focus = self.request.get("channel")
        wanted = terms(question)
        grain = _grain(question)
        lines, notes = (
            [],
            [
                "No model is configured (local helper): this is a finder over the catalogue, recipes, people and full text."
            ],
        )

        recipes = self.step("find_recipes", {"text": question or "overview"}).data or []
        index = self.step("describe", {"subject": None})
        channels = self.channels(wanted)
        if focus:
            focused = self.catalog.channel(focus)
            if focused is not None:
                channels = [c for c in channels if c[1]["name"] == focus] or [
                    (1, focused, [])
                ]
                channels += [c for c in self.channels(wanted) if c[1]["name"] != focus]
        self.emit(
            "match_channels",
            {"text": question},
            _computed(
                f"{len(channels)} channels match",
                "nothing: the local helper sends no data anywhere",
            ),
        )
        people = []
        try:
            people = self.people(wanted)
        except Exception:
            people = []
        self.emit(
            "match_people",
            {"text": question},
            _computed(
                f"{len(people)} people match",
                "nothing: the local helper sends no data anywhere",
            ),
        )
        recall = None
        if wanted:
            recall = self.step(
                "recall", {"text": question, **({"channel": focus} if focus else {})}
            )

        candidates = []
        if people and people[0]["matched"] >= 2:
            # A full name in the question: that person's activity first.
            candidates.append(self.person_counts(people[0], grain) + (None,))
        for score, channel, fields in channels[:3]:
            numeric = [f for f in fields if f["type"] in ("integer", "number")]
            if numeric:
                candidates.append(
                    self.field_series(channel, numeric[0], grain) + (channel["name"],)
                )
        if people:
            candidates.append(self.person_counts(people[0], grain) + (None,))
        for recipe in recipes[:2]:
            if (
                len(
                    wanted
                    & terms(
                        recipe["name"].replace("_", " ")
                        + " "
                        + (recipe.get("summary") or "")
                    )
                )
                >= 2
            ):
                body = self.belt.call("recipe", {"name": recipe["name"]}).data or {}
                statements = body.get("statements") or []
                if statements and isinstance(statements[0], str):
                    candidates.append(
                        (
                            statements[0],
                            "table",
                            {},
                            recipe["summary"] or recipe["name"],
                            None,
                        )
                    )
        for score, channel, fields in channels[:3]:
            candidates.append(self.channel_counts(channel, grain) + (channel["name"],))
        if (
            recall is not None
            and not recall.is_error
            and recall.data
            and recall.data.get("counts")
        ):
            best = max(recall.data["counts"].items(), key=lambda kv: kv[1])[0]
            found = self.catalog.channel(best)
            if found is not None:
                candidates.append(self.channel_counts(found, grain) + (found["name"],))
        candidates.append(self.overview() + (None,))
        statement, spec, check = self.first_valid(candidates)
        if statement:
            self.emit(
                "validate",
                {"statement": statement},
                _computed(
                    "statement validates",
                    "nothing: the local helper sends no data anywhere",
                ),
                sql=statement,
                columns=check["columns"],
            )

        lines.append(
            "**Best guess:** "
            + (spec["title"] if spec else "no statement could be built")
            + "."
        )
        if channels:
            lines.append("")
            lines.append("**Channels**")
            for score, channel, fields in channels[:5]:
                described = (
                    f" ({', '.join('`' + f['name'] + '`' for f in fields[:4])})"
                    if fields
                    else ""
                )
                lines.append(
                    f"- `{channel['name']}`{described}"
                    + (f": {channel['description']}" if channel["description"] else "")
                )
        if recipes:
            lines.append("")
            lines.append("**Recipes**")
            for recipe in recipes[:4]:
                lines.append(f"- `{recipe['name']}`: {recipe['summary']}")
        if people:
            lines.append("")
            lines.append("**People**")
            for person in people:
                lines.append(
                    f"- {person.get('display_name') or person.get('handle')} (person {person['id']})"
                )
        if recall is not None and not recall.is_error and recall.data:
            counts = recall.data.get("counts") or {}
            lines.append("")
            if counts:
                lines.append("**Full text**")
                for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:6]:
                    lines.append(f"- `{name}`: {n} of the top hits")
            else:
                lines.append("**Full text:** no record matches those words.")
        if not (channels or recipes or people):
            lines.append("")
            lines.append(
                "Nothing in the catalogue matches those words; the statement is an overview of every channel."
            )
            notes.append("Try the name of a channel or a field (describe lists them).")
        del index
        return dict(
            answer="\n".join(lines), statement=statement, spec=spec, notes=notes
        )

    def fix(self):
        statement = self.request.get("statement") or ""
        given = self.request.get("error") or {}
        check = self.belt.validate(statement)
        self.emit(
            "validate",
            {"statement": statement},
            _computed(
                "valid"
                if check["valid"]
                else (check["error"] or "invalid")
                + (f" ({check['code']})" if check["code"] else ""),
                "nothing: the local helper sends no data anywhere",
            ),
            sql=statement,
            error=None if check["valid"] else check["error"],
            columns=check["columns"],
        )
        message = check["error"] if not check["valid"] else (given.get("message") or "")
        code = check["code"] if not check["valid"] else given.get("code")
        fixer = sqlfix.Fixer(self.catalog)
        current, applied, explanations = statement, [], []
        for _ in range(3):
            suggestions = fixer.fix(current, message, code, check.get("detail"))
            explanations.extend(
                s["explanation"]
                for s in suggestions
                if s["explanation"] not in explanations
            )
            proposed = next((s for s in suggestions if s["statement"]), None)
            if proposed is None:
                break
            applied.append(proposed["rule"])
            current = proposed["statement"]
            check = self.belt.validate(current)
            if check["valid"]:
                break
            message, code = check["error"], check["code"]
        self.emit(
            "rules",
            {"code": code, "message": message},
            _computed(
                ", ".join(applied) or "no mechanical fix",
                "nothing: the local helper sends no data anywhere",
            ),
        )
        fixed_ok = current != statement and check["valid"]
        lines = []
        if fixed_ok:
            lines.append(
                "**Fixed:** " + ", ".join(r.replace("_", " ") for r in applied) + "."
            )
        elif check["valid"] and current == statement and not given:
            lines.append("The statement validates as it is.")
        else:
            lines.append("**Not fixed automatically.**")
        for text in explanations[:4]:
            lines.append("- " + text)
        notes = []
        if current != statement and not check["valid"]:
            notes.append(
                "The proposed statement still does not validate: "
                + (check["error"] or "")
                + (f" ({check['code']})" if check["code"] else "")
            )
        return dict(
            answer="\n".join(lines),
            statement=current if fixed_ok else statement,
            spec=None,
            notes=notes,
        )

    def explain(self):
        statement = self.request.get("selection") or self.request.get("statement") or ""
        check = self.belt.validate(statement)
        self.emit(
            "validate",
            {"statement": statement},
            _computed(
                "valid" if check["valid"] else "does not validate",
                "nothing: the local helper sends no data anywhere",
            ),
            sql=statement,
            columns=check["columns"],
            error=None if check["valid"] else check["error"],
        )
        text = sqlfix.explain(statement, self.catalog)
        if check["valid"] and check["columns"]:
            text += (
                "\n- Output columns: "
                + ", ".join(
                    f"`{c['name']}` ({c['type']})" for c in check["columns"][:20]
                )
                + "."
            )
        elif not check["valid"]:
            text += (
                "\n- It does not validate yet: "
                + (check["error"] or "")
                + (f" (SQLSTATE {check['code']})" if check["code"] else "")
                + ". Use Fix."
            )
        return dict(
            answer=text,
            statement=self.request.get("statement") or statement,
            spec=None,
            notes=[],
        )

    def refine(self):
        statement = (self.request.get("statement") or "").strip()
        question = self.request.get("question") or ""
        changed = []
        current = statement
        grain = _grain(question, default=None)
        if grain:
            # An output column named after the old grain follows it.
            for old, alias in re.findall(
                r"date_trunc\(\s*'(hour|day|week|month|quarter|year)'[^)]*\)\s+AS\s+(\w+)",
                current,
                flags=re.IGNORECASE,
            ):
                if alias.lower() == old.lower() and old.lower() != grain:
                    current = sqlfix._replace_word(current, alias, grain)
            replaced = re.sub(
                r"date_trunc\(\s*'(hour|day|week|month|quarter|year)'",
                f"date_trunc('{grain}'",
                current,
            )
            if replaced != current:
                current = replaced
                changed.append(f"grain {grain}")
        match = LAST.search(question)
        if match and "happened_at" in current:
            window = f"happened_at >= now() - interval '{int(match.group(1))} {match.group(2).lower()}s'"
            if re.search(r"\bWHERE\b", current, re.IGNORECASE):
                current = re.sub(
                    r"\bWHERE\b",
                    f"WHERE {window} AND",
                    current,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                current = (
                    re.sub(
                        r"\b(GROUP BY|ORDER BY|LIMIT)\b",
                        f"WHERE {window} \\1",
                        current,
                        count=1,
                        flags=re.IGNORECASE,
                    )
                    if re.search(
                        r"\b(GROUP BY|ORDER BY|LIMIT)\b", current, re.IGNORECASE
                    )
                    else current + " WHERE " + window
                )
            changed.append(f"last {match.group(1)} {match.group(2)}s")
        match = TOP.search(question)
        if match:
            n = int(match.group(1))
            if re.search(r"\bLIMIT\s+\d+", current, re.IGNORECASE):
                current = re.sub(
                    r"\bLIMIT\s+\d+", f"LIMIT {n}", current, flags=re.IGNORECASE
                )
            else:
                current = current.rstrip("; \n") + f" LIMIT {n}"
            changed.append(f"limit {n}")
        check = self.belt.validate(current)
        self.emit(
            "validate",
            {"statement": current},
            _computed(
                "valid" if check["valid"] else "does not validate",
                "nothing: the local helper sends no data anywhere",
            ),
            sql=current,
            columns=check["columns"],
            error=None if check["valid"] else check["error"],
        )
        if not changed:
            found = self.ask()
            found["notes"].append(
                "The local helper refines only a grain (by day, week, month), a recent window (last 30 days) or a top N; the finder ran instead."
            )
            return found
        if not check["valid"]:
            return dict(
                answer="The refinement does not validate, so the statement is unchanged: "
                + (check["error"] or ""),
                statement=statement,
                spec=None,
                notes=[],
            )
        return dict(
            answer="**Refined:** " + ", ".join(changed) + ".",
            statement=current,
            spec=None,
            notes=[],
        )


class _Outcome:
    def __init__(self, summary, sent):
        self.summary = summary
        self.sent = sent
        self.event = {}
        self.is_error = False
        self.data = None


def _computed(summary, sent):
    return _Outcome(summary, sent)


def answer(belt, request, emit):
    """The final {answer, statement, spec, notes} of a local run."""
    finder = Finder(belt, request, emit)
    verb = request.get("verb")
    if verb == "fix":
        return finder.fix()
    if verb == "explain":
        return finder.explain()
    if verb == "refine":
        return finder.refine()
    return finder.ask()
