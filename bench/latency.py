"""Latency harness: timed sets with warm-up and a regression gate (BEN-19).

Targets are not assumed: the first recorded run is the baseline and later
runs are compared with it under a tolerance. measure() times arms (whole
questions); measure_operations() times the v1.1 operations apart from each
other (recall, expansion, match, librarian) so a regression in one is not
hidden by the others. bench/baseline_latency.json holds the first recorded
operation baseline of the offline twin.
"""

import json
import time
from pathlib import Path

from . import arms as arm_registry, score

TOLERANCE = 0.20
METRICS = ("p50", "p95")
OPERATIONS = ("recall", "expansion", "match", "librarian")
BASELINE_PATH = Path(__file__).with_name("baseline_latency.json")


def timed(call, repeats=5, warmup=1):
    """Milliseconds of each of `repeats` calls, after `warmup` unrecorded ones."""
    for _ in range(warmup):
        call()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def summarize(samples):
    return dict(n=len(samples), **score.percentiles(samples, (50, 95)))


def measure(client, questions, arms=None, available=None, repeats=3, warmup=1):
    """p50/p95 per arm over a timed set of bound questions.

    Each arm runs the whole set `warmup` times unrecorded, then `repeats`
    times recorded. An arm the system cannot serve is reported as skipped
    with no numbers.
    """
    if available is None:
        available = arm_registry.probe(client)
    out = {}
    for name in arms or ("keyword", "meaning", "fused"):
        arm = arm_registry.ARMS[name]
        unmet = arm.unmet(available)
        if unmet:
            out[name] = dict(
                status=arm_registry.SKIPPED, reason="system lacks " + ", ".join(unmet)
            )
            continue
        samples = []
        for round_no in range(warmup + repeats):
            for question in questions:
                result = arm.run(client, question)
                if round_no >= warmup and result.status == arm_registry.OK:
                    samples.append(result.latency_ms)
        if not samples:
            out[name] = dict(
                status=arm_registry.SKIPPED, reason="no question could be posed"
            )
            continue
        out[name] = dict(status=arm_registry.OK, **summarize(samples))
    return out


def _saved_set(client, question):
    """A saved recall set for a question (input of expansion and match)."""
    body = arm_registry.recall_body(dict(k=arm_registry.DEFAULT_K), question)
    body.update(save=True, trace=False)
    response = client.post("/v1/recall", json=body)
    if response.status_code != 200:
        return None
    return response.json().get("result_set")


def _calls(client, question, operation, handle):
    """The one HTTP call an operation is timed by, or None when it cannot
    be made (no saved set to expand or match)."""
    if operation == "recall":
        body = arm_registry.recall_body(dict(k=arm_registry.DEFAULT_K), question)
        body["trace"] = False
        return lambda: client.post("/v1/recall", json=body)
    if operation == "librarian":
        body = arm_registry.librarian_body(question)
        return lambda: client.post("/v1/librarian", json=body)
    if handle is None:
        return None
    if operation == "expansion":
        body = dict(input=handle, mode="context", before=1, after=1)
        return lambda: client.post("/v1/expand", json=body)
    body = dict(input=handle, condition=question.text[:1000], max_rows=20)
    return lambda: client.post("/v1/match", json=body)


OPERATION_NEEDS = dict(
    recall={"recall"},
    expansion={"recall", "expand"},
    match={"recall", "match"},
    librarian={"librarian"},
)


def measure_operations(
    client, questions, operations=OPERATIONS, available=None, repeats=3, warmup=1
):
    """p50/p95 per operation, each timed alone over the same questions.

    An operation the system does not offer is skipped with the reason; a
    call that does not answer 200 is not a sample (it is counted in errors).
    """
    if available is None:
        available = arm_registry.probe(client)
    out = {}
    handles = {}
    for name in operations:
        missing = sorted(OPERATION_NEEDS[name] - set(available))
        if missing:
            out[name] = dict(
                status=arm_registry.SKIPPED, reason="system lacks " + ", ".join(missing)
            )
            continue
        samples, errors = [], 0
        for question in questions:
            if name in ("expansion", "match") and question.id not in handles:
                handles[question.id] = _saved_set(client, question)
            call = _calls(client, question, name, handles.get(question.id))
            if call is None:
                continue
            for round_no in range(warmup + repeats):
                started = time.perf_counter()
                response = call()
                elapsed = (time.perf_counter() - started) * 1000
                if response.status_code != 200:
                    errors += 1
                    break
                if round_no >= warmup:
                    samples.append(elapsed)
        if not samples:
            out[name] = dict(
                status=arm_registry.SKIPPED,
                reason="no call answered" if errors else "no question could be posed",
                errors=errors,
            )
            continue
        out[name] = dict(status=arm_registry.OK, errors=errors, **summarize(samples))
    return out


def compare(baseline, current, tolerance=TOLERANCE):
    """Gate: no measured operation may be slower than baseline by > tolerance.

    Both arguments map an operation name to {"p50": ms, "p95": ms}. An
    operation without numbers on either side is listed under `unmeasured`
    and neither passes nor fails the gate.
    """
    regressions, unmeasured, checked = [], [], []
    for name in sorted(set(baseline) | set(current)):
        before, after = baseline.get(name) or {}, current.get(name) or {}
        if any(side.get(m) is None for side in (before, after) for m in METRICS):
            unmeasured.append(name)
            continue
        checked.append(name)
        for metric in METRICS:
            limit = before[metric] * (1 + tolerance)
            if after[metric] > limit:
                regressions.append(
                    dict(
                        operation=name,
                        metric=metric,
                        baseline=before[metric],
                        current=after[metric],
                        limit=limit,
                        ratio=after[metric] / before[metric]
                        if before[metric]
                        else None,
                    )
                )
    return dict(
        ok=not regressions,
        tolerance=tolerance,
        checked=checked,
        unmeasured=unmeasured,
        regressions=regressions,
    )


def read_baseline(path):
    path = Path(path)
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def write_baseline(path, measured):
    Path(path).write_text(
        json.dumps(measured, indent=2, sort_keys=True) + "\n", "utf-8", newline="\n"
    )
