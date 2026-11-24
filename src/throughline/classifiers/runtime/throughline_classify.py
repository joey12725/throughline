"""throughline_classify: the whole contract of a Python classifier.

A classifier is one file with one function decorated @classifier. It reads a
conversation (a Thread) and returns marks; Throughline checks every mark and
writes it as annotations. The file runs in a sandbox: no network, no database,
no files. The only way out is `ctx` (Jev, the configured LLM, a log).

    from throughline_classify import classifier

    @classifier(grain="thread")
    def classify(thread, ctx):
        others = [p for p in thread.participants if not p.is_owner]
        if not others:
            return []                      # nothing but the owner talking
        level = ctx.jev.choose(
            "How much of this recording is media playing (TV, film, music, "
            "video) rather than people talking?",
            thread.render(), ["none", "partial", "full"])
        if level.answer == "full":
            return [thread.mark(is_noise=True, confidence=level.p)]   # every message
        ...

Objects (read-only):
  Thread       id, channel, title, started_at, duration_s, messages,
               participants, owner (Participant or None), others (non-owner
               participants), render(max_chars=24000)
  Participant  key, label, is_owner, person_id, messages, turns, words,
               share (fraction of the thread's words), text,
               render(context=None, max_chars=12000)
  Message      id, at, offset_s, participant (key), label, text, render()

Marks (return a list of them; [] means nothing to write):
  thread.mark(**value, confidence=, reason=, detail=)   value on every message
  participant.mark(**value, ...)                        value on that participant's messages
  message.mark(**value, ...)                            value on one message
  thread.note(**detail) / participant.note(**detail)    a verdict that writes nothing
                                                        on messages (e.g. level="partial")
  `value` fields must fit the classifier's attribute (its value_schema).

ctx:
  ctx.jev.probability(question, about) -> float 0..1
  ctx.jev.yes_no(question, about, threshold=0.5) -> Answer(yes, p)
  ctx.jev.choose(question, about, options) -> Choice(answer, p, probabilities)
  ctx.jev.ask_many(about, {key: question_text | (question_text, options)})
      -> {key: Answer | Choice}: several questions about the same `about` in
      ONE call (the text is sent and paid for once). A plain string is a
      yes/no question, a (question, options) pair a choice. Prefer it to
      separate calls whenever two questions read the same text.
  ctx.jev.decide(questions, about) -> raw Jev answers (dict of
      {"type": "noul"|"score", "instructions": ..., "criteria": [...]})
  ctx.llm.json(instruction, data, schema=None) -> dict
  ctx.llm.ask(instruction, data) -> str
  ctx.log(*values)
  ctx.owner_name
  `about` is text (e.g. thread.render()) or a JSON-able dict.

This module is also imported by the host to render threads the same way.
"""

import builtins
import json
import os
import sys
import traceback

__all__ = ["classifier", "Thread", "Participant", "Message", "Answer", "Choice", "Stop"]

ALLOWED_IMPORTS = frozenset(
    {
        "re", "json", "math", "statistics", "collections", "itertools", "functools",
        "datetime", "string", "textwrap", "dataclasses", "typing", "enum", "difflib",
        "unicodedata", "operator", "heapq", "bisect", "throughline_classify",
    }
)
GRAINS = ("thread",)
_REGISTERED = []


class Stop(Exception):
    """Raised by ctx calls when the run must stop (budget, provider down).
    Let it propagate: the thread is judged again on a later run."""


def classifier(fn=None, *, grain="thread"):
    """Mark the classifier function: fn(thread, ctx) -> list of marks."""
    if grain not in GRAINS:
        raise ValueError(f"grain must be one of {', '.join(GRAINS)}")

    def wrap(f):
        f.__classifier__ = {"grain": grain}
        _REGISTERED.append(f)
        return f

    return wrap(fn) if callable(fn) else wrap


# --- the objects ----------------------------------------------------------------


def _clock(seconds):
    seconds = int(max(0, seconds or 0))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fit(lines, max_chars):
    """Join lines; when too long keep evenly spaced runs of consecutive lines
    and say how many were left out between them."""
    total = sum(len(x) + 1 for x in lines)
    if total <= max_chars or not lines:
        return "\n".join(lines)
    windows = 8
    budget = max(200, max_chars // windows - 60)
    n = len(lines)
    starts = sorted({int(i * n / windows) for i in range(windows)})
    out, last_end = [], 0
    for start in starts:
        if start < last_end:
            start = last_end
        if start >= n:
            break
        if start > last_end:
            out.append(f"... [{start - last_end} turns left out] ...")
        used, i = 0, start
        while i < n and (used == 0 or used + len(lines[i]) + 1 <= budget):
            line = lines[i] if len(lines[i]) <= budget else lines[i][: budget - 3] + "..."
            out.append(line)
            used += len(line) + 1
            i += 1
        last_end = i
    if last_end < n:
        out.append(f"... [{n - last_end} turns left out] ...")
    return "\n".join(out)


class _Markable:
    _target = None

    def _key(self):
        raise NotImplementedError

    def mark(self, confidence=None, reason=None, detail=None, **value):
        """Write `value` on the messages this covers."""
        if not value:
            raise ValueError("mark() needs the value's fields, e.g. mark(is_noise=True)")
        return _mark(self._target, self._key(), value, confidence, reason, detail)

    def note(self, reason=None, confidence=None, **detail):
        """Record a verdict about this thread or participant that writes
        nothing on messages (for example level='partial')."""
        if self._target == "message":
            raise ValueError("a message has no note(); use mark()")
        return _mark(self._target, self._key(), None, confidence, reason, detail)


def _mark(target, key, value, confidence, reason, detail):
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be a number from 0 to 1")
        confidence = round(float(confidence), 4)
    if reason is not None:
        reason = str(reason)[:500]
    if detail is not None and not isinstance(detail, dict):
        raise ValueError("detail must be a dict")
    return {
        "__mark__": True,
        "target": target,
        "key": key,
        "value": value,
        "confidence": confidence,
        "reason": reason,
        "detail": dict(detail or {}),
    }


class Message(_Markable):
    _target = "message"

    def __init__(self, data, thread):
        self.id = data["id"]
        self.at = data.get("at")
        self.offset_s = data.get("offset_s")
        self.participant = data.get("participant")
        self.text = data.get("text") or ""
        self._thread = thread

    def _key(self):
        return self.id

    @property
    def label(self):
        p = self._thread.participant(self.participant)
        return p.label if p else "unknown"

    def render(self):
        p = self._thread.participant(self.participant)
        who = (p.label + (" (owner)" if p.is_owner else "")) if p else "unknown"
        stamp = _clock(self.offset_s) if self.offset_s is not None else (self.at or "")
        return f"[{stamp}] {who}: {self.text}"

    def __repr__(self):
        return f"Message({self.id}, {self.label!r}, {self.text[:40]!r})"


class Participant(_Markable):
    _target = "participant"

    def __init__(self, data, thread):
        self.key = data["key"]
        self.label = data.get("label") or self.key
        self.is_owner = bool(data.get("is_owner"))
        self.person_id = data.get("person_id")
        self._thread = thread

    def _key(self):
        return self.key

    @property
    def messages(self):
        return [m for m in self._thread.messages if m.participant == self.key]

    @property
    def turns(self):
        return len(self.messages)

    @property
    def text(self):
        return "\n".join(m.text for m in self.messages)

    @property
    def words(self):
        return sum(len(m.text.split()) for m in self.messages)

    @property
    def share(self):
        total = sum(p.words for p in self._thread.participants) or 1
        return round(self.words / total, 4)

    def render(self, context=None, max_chars=12000):
        """This participant's turns. With context=thread, also a shorter
        excerpt of the whole conversation, so a model can compare."""
        own = _fit([m.render() for m in self.messages], max_chars if context is None else max_chars * 2 // 3)
        head = f"Participant: {self.label}{' (owner)' if self.is_owner else ''}, {self.turns} turns, {round(self.share * 100)}% of words"
        if context is None:
            return head + "\n" + own
        excerpt = context.render(max_chars=max_chars // 3)
        return f"{head}\n--- their turns ---\n{own}\n--- the conversation around them ---\n{excerpt}"

    def __repr__(self):
        return f"Participant({self.key!r}, {self.label!r}, owner={self.is_owner}, turns={self.turns})"


class Thread(_Markable):
    _target = "thread"

    def __init__(self, data):
        self.id = data["id"]
        self.channel = data.get("channel")
        self.title = data.get("title")
        self.started_at = data.get("started_at")
        self.duration_s = data.get("duration_s")
        self.participants = [Participant(p, self) for p in data.get("participants") or []]
        self._by_key = {p.key: p for p in self.participants}
        self.messages = [Message(m, self) for m in data.get("messages") or []]

    def _key(self):
        return None

    def participant(self, key):
        return self._by_key.get(key)

    @property
    def owner(self):
        return next((p for p in self.participants if p.is_owner), None)

    @property
    def others(self):
        return [p for p in self.participants if not p.is_owner]

    def render(self, max_chars=24000):
        lines = [m.render() for m in self.messages]
        names = ", ".join(p.label + (" (owner)" if p.is_owner else "") for p in self.participants)
        head = f"{self.channel or 'conversation'}: {self.title or ''} ({_clock(self.duration_s)} long; participants: {names})"
        return head + "\n" + _fit(lines, max(500, max_chars - len(head) - 1))

    def __repr__(self):
        return f"Thread({self.id}, {self.title!r}, {len(self.messages)} messages, {len(self.participants)} participants)"


class Answer:
    def __init__(self, yes, p):
        self.yes, self.p = bool(yes), float(p)

    def __bool__(self):
        return self.yes

    def __repr__(self):
        return f"Answer(yes={self.yes}, p={self.p:.3f})"


class Choice:
    def __init__(self, answer, p, probabilities):
        self.answer, self.p, self.probabilities = answer, float(p), dict(probabilities)

    def __repr__(self):
        return f"Choice({self.answer!r}, p={self.p:.3f})"


# --- ctx: calls go to the host ---------------------------------------------------


class _Channel:
    def __init__(self, reader, writer):
        self.reader, self.writer, self.n = reader, writer, 0

    def send(self, msg):
        self.writer.write(json.dumps(msg, default=str) + "\n")
        self.writer.flush()

    def call(self, op, **args):
        self.n += 1
        self.send({"type": "call", "id": self.n, "op": op, "args": args})
        line = self.reader.readline()
        if not line:
            raise Stop("host went away")
        reply = json.loads(line)
        if reply.get("type") != "result" or reply.get("id") != self.n:
            raise Stop("protocol error")
        if reply.get("stop"):
            raise Stop(reply.get("error") or "stopped")
        if reply.get("error"):
            raise RuntimeError(reply["error"])
        return reply.get("value")


class _Jev:
    def __init__(self, channel):
        self._c = channel

    def decide(self, questions, about):
        if not isinstance(questions, dict) or not questions:
            raise ValueError("questions must be a dict of Jev questions")
        return self._c.call("jev", questions=questions, about=about)

    def probability(self, question, about):
        ans = self.decide({"q": {"type": "noul", "instructions": str(question)}}, about)
        return float(ans["q"]["noul"])

    def yes_no(self, question, about, threshold=0.5):
        p = self.probability(question, about)
        return Answer(p >= threshold, p)

    def choose(self, question, about, options):
        options = [str(o) for o in options]
        if len(options) < 2:
            raise ValueError("choose() needs at least two options")
        ans = self.decide({"q": {"type": "score", "instructions": str(question), "criteria": options}}, about)["q"]
        return _choice(ans, options)

    def ask_many(self, about, questions, threshold=0.5):
        """Several questions about the same text in one call."""
        if not isinstance(questions, dict) or not questions:
            raise ValueError("ask_many() needs a dict of questions")
        raw, kinds = {}, {}
        for key, q in questions.items():
            key = str(key)
            if isinstance(q, str):
                raw[key] = {"type": "noul", "instructions": q}
                kinds[key] = None
            elif isinstance(q, (tuple, list)) and len(q) == 2:
                options = [str(o) for o in q[1]]
                if len(options) < 2:
                    raise ValueError(f"question {key!r} needs at least two options")
                raw[key] = {"type": "score", "instructions": str(q[0]), "criteria": options}
                kinds[key] = options
            else:
                raise ValueError(f"question {key!r} must be text or (text, options)")
        answers = self.decide(raw, about)
        out = {}
        for key, options in kinds.items():
            if options is None:
                p = float(answers[key]["noul"])
                out[key] = Answer(p >= threshold, p)
            else:
                out[key] = _choice(answers[key], options)
        return out


def _choice(ans, options):
    probs = ans.get("probabilities") or {}
    dist = {opt: float(probs.get(str(i), 0.0)) for i, opt in enumerate(options)}
    if not any(dist.values()):
        idx = min(len(options) - 1, max(0, int(round(float(ans.get("score", 0))))))
        dist = {opt: (1.0 if i == idx else 0.0) for i, opt in enumerate(options)}
    best = max(dist, key=dist.get)
    return Choice(best, dist[best], dist)


class _Llm:
    def __init__(self, channel):
        self._c = channel

    def json(self, instruction, data, schema=None):
        return self._c.call("llm_json", instruction=str(instruction), data=data, schema=schema)

    def ask(self, instruction, data):
        return str(self._c.call("llm_ask", instruction=str(instruction), data=data))


class Ctx:
    def __init__(self, channel, owner_name=None):
        self.jev = _Jev(channel)
        self.llm = _Llm(channel)
        self.owner_name = owner_name
        self._channel = channel

    def log(self, *values):
        self._channel.send({"type": "log", "text": " ".join(str(v) for v in values)[:2000]})


# --- the child process -----------------------------------------------------------


def _guard_imports():
    real = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        caller = (globals or {}).get("__name__", "")
        if caller == "classifier_code" and (level or name.split(".")[0] not in ALLOWED_IMPORTS):
            raise ImportError(f"import of {name!r} is not allowed in a classifier (allowed: {', '.join(sorted(ALLOWED_IMPORTS - {'throughline_classify'}))})")
        return real(name, globals, locals, fromlist, level)

    builtins.__import__ = guarded


def _load(code):
    del _REGISTERED[:]
    namespace = {"__name__": "classifier_code", "__builtins__": builtins}
    exec(compile(code, "classifier.py", "exec"), namespace)
    fns = list(_REGISTERED) or ([namespace["classify"]] if callable(namespace.get("classify")) else [])
    if len(fns) != 1:
        raise ValueError("the file must define exactly one @classifier function (or one function named classify)")
    fn = fns[0]
    meta = getattr(fn, "__classifier__", {"grain": "thread"})
    return fn, meta


def _check_marks(result):
    if result is None:
        return []
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, (list, tuple)):
        raise TypeError("the classifier must return a list of marks")
    out = []
    for item in result:
        if not (isinstance(item, dict) and item.get("__mark__")):
            raise TypeError("every item returned must be a mark (thread.mark, participant.mark, message.mark or .note)")
        out.append({k: v for k, v in item.items() if k != "__mark__"})
    return out


def main():
    # Run as a script, this module is __main__; classifier code imports it by name.
    sys.modules.setdefault("throughline_classify", sys.modules[__name__])
    proto_out = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr  # print() in a classifier never corrupts the protocol
    channel = _Channel(sys.stdin, proto_out)
    fn = meta = None
    _guard_imports()
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        kind = msg.get("type")
        if kind == "load":
            try:
                fn, meta = _load(msg["code"])
                channel.send({"type": "loaded", "grain": meta.get("grain")})
            except Exception as exc:
                channel.send({"type": "error", "error": _short(exc)})
        elif kind == "run":
            if fn is None:
                channel.send({"type": "error", "error": "no classifier loaded"})
                continue
            try:
                ctx = Ctx(channel, msg.get("owner_name"))
                marks = _check_marks(fn(Thread(msg["thread"]), ctx))
                channel.send({"type": "done", "marks": marks})
            except Stop as exc:
                channel.send({"type": "stopped", "error": str(exc)[:500]})
            except Exception as exc:
                channel.send({"type": "error", "error": _short(exc)})
        elif kind == "exit":
            break


def _short(exc):
    tb = traceback.extract_tb(exc.__traceback__)
    where = next((f"line {f.lineno}" for f in reversed(tb) if f.filename == "classifier.py"), None)
    text = f"{type(exc).__name__}: {exc}"
    return (f"{text} ({where})" if where else text)[:1000]


if __name__ == "__main__":
    main()
