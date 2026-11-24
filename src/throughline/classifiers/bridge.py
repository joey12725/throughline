"""Model calls for classifiers: Jev Decisions and the configured LLM, under a
spend limit, recorded in the model ledger.

Limits, checked before every call (an estimate) and after it (the real cost):
  - the run's max_cost and max_calls,
  - the classifier's monthly_cap_usd (its ledger purpose this month),
  - THROUGHLINE_MONTHLY_MODEL_CAP_USD across every model job.
A limit that is reached raises Stopped: the item is not judged, and the next
run picks it up.

Keys: OPENROUTER_API_KEY (or THROUGHLINE_JEV_KEY) for Jev, the lens LLM
settings for the LLM (THROUGHLINE_TABLE_LLM_* / THROUGHLINE_MODEL_*). When
THROUGHLINE_OPENROUTER_KEY_COMMAND is set and no key is in the environment,
the command is run once per process and its output kept in memory only.
"""

import json
import os
import subprocess
import threading
import time

from ..lenses import workers
from ..providers.base import ProviderUnavailable, text_tokens

JEV_PAYLOAD_LIMIT = 31000
_key_cache = {}
_key_lock = threading.Lock()
_override = None


class Stopped(Exception):
    """A limit was reached or the provider is down: stop, retry later."""


def set_override(fn):
    """Test hook: fn(op, args) -> value replaces every model call (op is
    'jev', 'llm_json' or 'llm_ask'). None restores real calls."""
    global _override
    _override = fn


def _command_key(env_name):
    command = (os.getenv(env_name) or "").strip()
    if not command:
        return None
    with _key_lock:
        if command in _key_cache:
            return _key_cache[command]
        try:
            out = subprocess.run(
                ["/bin/sh", "-c", command], capture_output=True, text=True, timeout=30, check=False
            )
            value = (out.stdout or "").strip() or None
        except (OSError, subprocess.SubprocessError):
            value = None
        if value:
            _key_cache[command] = value
        return value


def jev_key():
    from .. import secrets

    raw = (os.getenv("OPENROUTER_API_KEY") or "").strip() or (
        os.getenv("THROUGHLINE_JEV_KEY") or ""
    ).strip()
    if raw:
        return (secrets.resolve(raw) or "").strip() or None
    return _command_key("THROUGHLINE_OPENROUTER_KEY_COMMAND")


def _state(about):
    if isinstance(about, str):
        return {"text": about}
    if isinstance(about, dict):
        return about
    raise ValueError("about must be text or a dict")


def _shrink(state, over):
    """Cut the longest text field of state by `over` bytes (and a margin)."""
    texts = [(k, v) for k, v in state.items() if isinstance(v, str)]
    if not texts:
        raise ValueError("Jev input is over 32,000 bytes; pass less text")
    key, text = max(texts, key=lambda kv: len(kv[1].encode("utf-8")))
    keep = max(200, len(text) - over - 400)
    head = keep * 2 // 3
    state = dict(state)
    state[key] = text[:head] + "\n... [cut to fit] ...\n" + text[-(keep - head):]
    return state


class Meter:
    """Spend and call counting for one run of one classifier."""

    def __init__(self, conn, classifier_name, *, max_cost=0.5, max_calls=2000, monthly_cap_usd=None, purpose=None):
        self.conn = conn
        self.name = classifier_name
        self.purpose = purpose or f"classifier.{classifier_name}"
        self.max_cost = float(max_cost) if max_cost is not None else None
        self.max_calls = int(max_calls) if max_calls is not None else None
        self.monthly_cap = float(monthly_cap_usd) if monthly_cap_usd is not None else None
        self.calls = 0
        self.tokens = 0
        self.cost = 0.0
        self._month = None

    def month_spent(self):
        if self._month is None:
            row = self.conn.execute(
                """SELECT coalesce(sum(cost),0)::float8 AS c FROM tl.model_ledger
                WHERE purpose=%s AND at>=date_trunc('month', now() AT TIME ZONE 'UTC')""",
                (self.purpose,),
            ).fetchone()
            self._month = float(row["c"] or 0.0)
        return self._month

    def before(self, estimate):
        if self.max_calls is not None and self.calls + 1 > self.max_calls:
            raise Stopped(f"call limit reached ({self.max_calls} calls this run)")
        if self.max_cost is not None and self.cost + estimate > self.max_cost:
            raise Stopped(f"cost limit reached (${self.max_cost:.2f} this run)")
        if self.monthly_cap is not None and self.month_spent() + self.cost + estimate > self.monthly_cap:
            raise Stopped(f"monthly cap reached (${self.monthly_cap:.2f} for {self.name})")
        ok, spent, cap = workers.check_monthly_cap(self.conn, self.cost + estimate)
        if not ok:
            raise Stopped(f"THROUGHLINE_MONTHLY_MODEL_CAP_USD reached (${spent:.2f} of ${cap:.2f})")

    def after(self, tokens, cost):
        self.calls += 1
        self.tokens += int(tokens or 0)
        self.cost += float(cost or 0.0)

    def to_dict(self):
        return {"calls": self.calls, "tokens": self.tokens, "cost": round(self.cost, 6)}


def call(meter, op, args, *, model=None):
    """Run one ctx call for a classifier. Returns the value; raises Stopped
    (stop the run) or ValueError (a bad call: the classifier's error)."""
    if op not in ("jev", "llm_json", "llm_ask"):
        raise ValueError(f"unknown call {op!r}")
    if _override is not None:
        meter.before(0.0)
        value = _override(op, args)
        meter.after(0, 0.0)
        return value
    if op == "jev":
        return _jev(meter, args, model=model)
    return _llm(meter, op, args)


def jev_shared(meter, questions, about, *, model=None, purposes=()):
    """One Jev call that also answers other classifiers' questions about the
    same input. questions: {key: question} for every classifier; purposes:
    the other classifiers' ledger purposes. Tokens and cost are split evenly:
    this meter is charged its share, each other purpose gets a ledger row of
    its own share. Returns (answers, tokens_share, cost_share)."""
    n = 1 + len(purposes)
    if _override is not None:
        meter.before(0.0)
        value = _override("jev", {"questions": questions, "about": about})
        meter.after(0, 0.0)
        return value, 0, 0.0
    answers, data, payload, tokens, cost, ms = _jev_call(meter, {"questions": questions, "about": about}, model=model, share=n)
    used_model = str(data.get("model") or payload["model"])
    own_tokens = tokens - (tokens // n) * (n - 1)
    meter.after(own_tokens, cost / n)
    for purpose in (meter.purpose, *purposes):
        workers._record_ledger(
            meter.conn,
            purpose=purpose,
            provider_name="openrouter-jev",
            model=used_model,
            tokens=own_tokens if purpose == meter.purpose else tokens // n,
            cost=cost / n,
            calls=1 if purpose == meter.purpose else 0,
            ms=ms / n,
            local=False,
            origin="classifier",
        )
    return answers, tokens // n, cost / n


def _jev(meter, args, *, model=None):
    answers, data, payload, tokens, cost, ms = _jev_call(meter, args, model=model)
    meter.after(tokens, cost)
    workers._record_ledger(
        meter.conn,
        purpose=meter.purpose,
        provider_name="openrouter-jev",
        model=str(data.get("model") or payload["model"]),
        tokens=tokens,
        cost=cost,
        calls=1,
        ms=ms,
        local=False,
        origin="classifier",
    )
    return answers


def _jev_call(meter, args, *, model=None, share=1):
    questions = args.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be a dict")
    for key, q in questions.items():
        if not isinstance(q, dict) or q.get("type") not in ("noul", "score"):
            raise ValueError(f"question {key!r} must have type noul or score")
        if q.get("type") == "score" and not (isinstance(q.get("criteria"), list) and len(q["criteria"]) >= 2):
            raise ValueError(f"question {key!r} needs two or more criteria")
    state = _state(args.get("about"))
    payload = {"model": model or workers.JEV_DEFAULT_MODEL, "state": state, "questions": questions}
    for _ in range(4):
        size = len(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size < JEV_PAYLOAD_LIMIT:
            break
        payload["state"] = _shrink(payload["state"], size - JEV_PAYLOAD_LIMIT)
    estimate = (text_tokens(json.dumps(payload, default=str)) + 512) * workers.JEV_PRICE_PER_MILLION / 1e6
    meter.before(estimate / share)
    key = jev_key()
    if not key:
        raise Stopped("Jev needs OPENROUTER_API_KEY (or THROUGHLINE_OPENROUTER_KEY_COMMAND)")
    started = time.perf_counter()
    try:
        data, tokens, cost = workers._call_jev_decisions(payload, endpoint=workers.JEV_ENDPOINT, api_key=key)
        workers._validate_jev_response(data, questions)
    except ProviderUnavailable as exc:
        raise Stopped(f"Jev unavailable: {str(exc)[:200]}") from None
    except workers.LensSpecError as exc:
        raise Stopped(f"Jev answer rejected: {str(exc)[:200]}") from None
    return data.get("answers") or {}, data, payload, tokens, cost, (time.perf_counter() - started) * 1000.0


def _llm(meter, op, args):
    instruction = str(args.get("instruction") or "").strip()
    if not instruction:
        raise ValueError("an LLM call needs an instruction")
    data = args.get("data")
    base, model, key, price = workers._resolve_llm_endpoint({})
    if not key and "openrouter.ai" in base:
        key = jev_key()
    schema = args.get("schema") if op == "llm_json" else {
        "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    system = instruction + (
        "\nAnswer with one JSON object." if op == "llm_json" else "\nAnswer with a JSON object {\"answer\": \"...\"}.")
    payload = {"data": data}
    estimate = (text_tokens(system, json.dumps(payload, default=str)) + 800) * price / 1e6
    meter.before(estimate)
    started = time.perf_counter()
    try:
        parsed, tokens, cost = workers._chat_structured(
            base_url=base, model=model, api_key=key, system=system, user_payload=payload,
            json_schema=schema if isinstance(schema, dict) else None, schema_name="classifier",
            price_per_million=price,
        )
    except ProviderUnavailable as exc:
        raise Stopped(f"LLM unavailable: {str(exc)[:200]}") from None
    meter.after(tokens, cost)
    workers._record_ledger(
        meter.conn, purpose=meter.purpose, provider_name="table_llm", model=model, tokens=tokens,
        cost=cost, calls=1, ms=(time.perf_counter() - started) * 1000.0,
        local=not workers.egress.leaves_host(base), origin="classifier",
    )
    if op == "llm_ask":
        if isinstance(parsed, dict) and "answer" in parsed:
            return str(parsed["answer"])
        return str(parsed)
    if not isinstance(parsed, dict):
        raise ValueError("the LLM did not answer with a JSON object")
    return parsed


def leaves_host(op):
    """True when this kind of call sends text off this machine."""
    if op == "jev":
        return workers.egress.leaves_host(workers.JEV_ENDPOINT)
    base, _, _, _ = workers._resolve_llm_endpoint({})
    return workers.egress.leaves_host(base)
