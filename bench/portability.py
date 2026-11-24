"""Portability contract (BEN-18): one question, any host.

A Runner answers a bench question the way an agent host would and reports
what it cited and which tools it called:

    runner.answer(question) -> {answer, cited_ids, tool_log}

Two adapters ship: 'http' calls POST /v1/recall directly, 'cli' runs the
command-line client (`throughline lib recall ... --json`) through Typer's
CliRunner with its HTTP calls routed to the same target. Both run the same
questions; compare() scores each and reports the gap between them. Real
agent hosts plug in later by implementing answer().
"""

import json
import time
from contextlib import contextmanager

from . import arms as arm_registry, score


class Runner:
    name = "runner"

    def answer(self, question):  # pragma: no cover - interface
        raise NotImplementedError


def _answer(records, cited):
    by_id = {r.get("message_id"): r for r in records}
    return " ".join((by_id[c].get("content") or "") for c in cited if c in by_id)


class HttpRunner(Runner):
    """POST /v1/recall with the question as the query."""

    name = "http"

    def __init__(self, client, k=arm_registry.DEFAULT_K):
        self.client = client
        self.k = k

    def answer(self, question):
        body = arm_registry.recall_body(dict(k=self.k), question)
        body.pop("trace", None)
        started = time.perf_counter()
        response = self.client.post("/v1/recall", json=body)
        log = [
            dict(
                tool="POST /v1/recall",
                status=response.status_code,
                ms=round((time.perf_counter() - started) * 1000, 3),
            )
        ]
        if response.status_code != 200:
            return dict(answer="", cited_ids=[], tool_log=log)
        records = response.json().get("records") or []
        delivered = [r["message_id"] for r in records if r.get("message_id")]
        cited = arm_registry.cite(question, records, delivered)
        return dict(answer=_answer(records, cited), cited_ids=cited, tool_log=log)


class CliRunner(Runner):
    """`throughline lib recall QUERY --json` through Typer's CliRunner.

    The command-line client's HTTP helper is pointed at the same client for
    the duration of each call, so both adapters reach the same system.
    """

    name = "cli"

    def __init__(self, client, k=arm_registry.DEFAULT_K):
        self.client = client
        self.k = k

    @contextmanager
    def _routed(self):
        import typer

        from throughline import cli

        client = self.client
        original = cli.request

        def request(method, path, data=None, params=None):
            response = client.request(method, "/v1/" + path, json=data, params=params)
            if response.is_error:
                raise typer.BadParameter(
                    f"HTTP {response.status_code}: {response.text}"
                )
            return response.json()

        cli.request = request
        try:
            yield cli
        finally:
            cli.request = original

    def args(self, question):
        args = ["lib", "recall", question.text, "--k", str(self.k), "--json"]
        scope = arm_registry.scope_for(question)
        for option, key in (
            ("--as-of", "as_of"),
            ("--known-by", "known_by"),
            ("--after", "after"),
            ("--before", "before"),
            ("--time-basis", "time_basis"),
        ):
            if scope.get(key):
                args += [option, scope[key]]
        return args

    def answer(self, question):
        from typer.testing import CliRunner as TyperRunner

        started = time.perf_counter()
        with self._routed() as cli:
            result = TyperRunner().invoke(cli.app, self.args(question))
        log = [
            dict(
                tool="throughline lib recall",
                status=result.exit_code,
                ms=round((time.perf_counter() - started) * 1000, 3),
            )
        ]
        if result.exit_code != 0:
            return dict(answer="", cited_ids=[], tool_log=log)
        try:
            records = json.loads(result.stdout).get("records") or []
        except ValueError:
            return dict(answer="", cited_ids=[], tool_log=log)
        delivered = [r["message_id"] for r in records if r.get("message_id")]
        cited = arm_registry.cite(question, records, delivered)
        return dict(answer=_answer(records, cited), cited_ids=cited, tool_log=log)


ADAPTERS = {"http": HttpRunner, "cli": CliRunner}


def evaluate(runner, questions):
    """Per-runner scores over the same questions, from cited ids only."""
    rows = []
    for question in questions:
        out = runner.answer(question)
        cited = list(out.get("cited_ids") or [])
        row = dict(
            question=question.id,
            cited=cited,
            tools=len(out.get("tool_log") or []),
            ok=all(
                str(t.get("status")) in ("200", "0") for t in out.get("tool_log") or []
            ),
        )
        if question.required_groups():
            row["f05"] = score.f_beta(cited, question)
            row["covered"] = score.group_coverage(cited, question)["fraction"]
        row["faithfulness"] = score.citation_faithfulness(cited, cited, question)
        rows.append(row)
    return dict(
        runner=runner.name,
        questions=len(rows),
        failures=sum(1 for r in rows if not r["ok"]),
        f05=score.mean(r.get("f05") for r in rows),
        group_fraction=score.mean(r.get("covered") for r in rows),
        faithfulness=score.mean(r["faithfulness"] for r in rows),
        rows=rows,
    )


def compare(runners, questions):
    """BEN-18 report: every runner on the same questions and the gap.

    The gap is the largest difference in F0.5 (points) between runners and
    the questions where their cited ids differ."""
    results = {r.name: evaluate(r, questions) for r in runners}
    values = [v["f05"] for v in results.values() if v["f05"] is not None]
    differing = []
    if len(results) > 1:
        first, *others = results.values()
        for index, row in enumerate(first["rows"]):
            if any(o["rows"][index]["cited"] != row["cited"] for o in others):
                differing.append(row["question"])
    return dict(
        suite="portability",
        runners={
            name: {k: v for k, v in res.items() if k != "rows"}
            for name, res in results.items()
        },
        gap=dict(
            f05_points=round((max(values) - min(values)) * 100, 4)
            if len(values) > 1
            else None,
            differing_questions=differing,
        ),
    )
