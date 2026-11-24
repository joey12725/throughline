"""Faults: degrade the system only through its public configuration and
assert that the operation declares itself incomplete (BEN-20, LIB-13).

A fault is one of
- a request setting any caller may send (a tiny deadline_ms, an exhaustive
  recall over a row cap, an SQL row cap);
- an environment setting the system is started with (an embedding endpoint
  at a closed loopback port, a recall row cap, a controller endpoint served
  by StubController). Those need the system to run in this process (tests)
  or to be restarted with fault.env; over a remote URL they are skipped.

Nothing here patches code of the system under test.
"""

import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import arms as arm_registry


def closed_port():
    """A loopback port nothing listens on (bound, then released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Fault:
    name: str
    description: str
    # Request fields merged into the POST /v1/recall body.
    request: dict = field(default_factory=dict)
    # Environment the system must run with (applied only in-process).
    env: dict = field(default_factory=dict)
    # What the system must declare: substrings of its disclosures.
    expects: tuple = ()

    @property
    def needs_restart(self):
        return bool(self.env)


def faults():
    """The fault catalogue. Built on call: the closed port is fresh."""
    port = closed_port()
    return {
        fault.name: fault
        for fault in (
            Fault(
                "tiny_deadline",
                "A 1 ms deadline: legs cannot all run in time.",
                request=dict(budgets=dict(deadline_ms=1)),
            ),
            Fault(
                "row_cap",
                "An exhaustive recall over a scope larger than the row cap.",
                request=dict(
                    exhaustive=True, query="", scope=dict(channels=["bench.notes"])
                ),
                env=dict(THROUGHLINE_RECALL_EXHAUSTIVE_CAP="2"),
                expects=("truncated",),
            ),
            Fault(
                "embedding_closed_port",
                "The embedding endpoint is a closed loopback port.",
                env=dict(
                    THROUGHLINE_EMBEDDING_PROVIDER="http",
                    THROUGHLINE_EMBEDDING_BASE_URL=f"http://127.0.0.1:{port}/v1",
                    THROUGHLINE_EMBEDDING_MODEL="bench-closed",
                    THROUGHLINE_EMBEDDING_DIMENSIONS="64",
                    THROUGHLINE_EMBEDDING_QUERY_TIMEOUT_S="1",
                ),
                expects=("vector",),
            ),
        )
    }


@contextmanager
def applied(fault, environ=None):
    """Set a fault's environment for the block, then restore it."""
    environ = os.environ if environ is None else environ
    saved = {key: environ.get(key) for key in fault.env}
    environ.update(fault.env)
    try:
        yield fault
    finally:
        for key, value in saved.items():
            if value is None:
                environ.pop(key, None)
            else:
                environ[key] = value


def declared(execution):
    """Did the operation declare itself incomplete, and why."""
    execution = execution or {}
    reasons = arm_registry.disclosures_of(execution)
    stopped = any(
        s.get("status") in ("failed", "timeout")
        or str(s.get("reason") or "").startswith(("deadline", "timeout"))
        for s in execution.get("stages") or []
    )
    flagged = (
        arm_registry.incomplete_of(execution)
        or execution.get("completeness") == "truncated"
        or (execution.get("status") not in (None, "complete") and stopped)
    )
    return bool(flagged), reasons


def run_case(client, fault, question, in_process=True):
    """Recall one question under a fault: {fault, status, declared, reasons}.

    A fault that needs a restart is skipped unless the system runs in this
    process (in_process=True applies fault.env around the call).
    """
    if fault.needs_restart and not in_process:
        return dict(
            fault=fault.name,
            status=arm_registry.SKIPPED,
            reason="needs the system restarted with " + ", ".join(sorted(fault.env)),
        )
    body = arm_registry.recall_body(dict(k=arm_registry.DEFAULT_K), question)
    body.update(fault.request)
    try:
        with applied(fault) if fault.env else _nothing():
            response = client.post("/v1/recall", json=body)
    except Exception as exc:
        # An in-process client re-raises server errors: the operation failed
        # instead of declaring itself incomplete. Reported, never hidden.
        return dict(
            fault=fault.name,
            question=question.id,
            status=arm_registry.ERROR,
            reason=f"{type(exc).__name__}: {exc}"[:300],
            declared=False,
            correct=False,
        )
    if response.status_code != 200:
        return dict(
            fault=fault.name,
            question=question.id,
            status=arm_registry.ERROR,
            reason=f"HTTP {response.status_code}",
            declared=False,
            correct=False,
        )
    execution = response.json().get("execution") or {}
    flagged, reasons = declared(execution)
    expected = [t for t in fault.expects if not any(t in r for r in reasons)]
    return dict(
        fault=fault.name,
        question=question.id,
        status=arm_registry.OK,
        declared=flagged,
        reasons=reasons,
        missing_terms=expected,
        correct=flagged and not expected,
        execution_status=execution.get("status"),
        completeness=execution.get("completeness"),
    )


def sql_row_cap(client, statement="SELECT id FROM api.messages", cap=1):
    """POST /v1/sql under a row cap must say truncated."""
    response = client.post("/v1/sql", json=dict(statement=statement, row_cap=cap))
    if response.status_code != 200:
        return dict(fault="sql_row_cap", status=arm_registry.ERROR)
    data = response.json()
    return dict(
        fault="sql_row_cap",
        status=arm_registry.OK,
        declared=bool(data.get("truncated")),
        correct=bool(data.get("truncated")) and len(data["rows"]) <= cap,
        rows=len(data["rows"]),
    )


def run_all(client, question, in_process=True):
    """Every fault on one question, plus the SQL row cap."""
    cases = [run_case(client, f, question, in_process) for f in faults().values()]
    cases.append(sql_row_cap(client))
    return cases


@contextmanager
def _nothing():
    yield None


# --- a scripted controller endpoint ------------------------------------------------


class StubController:
    """An OpenAI-compatible chat endpoint on loopback that scripts the
    librarian's controller (THROUGHLINE_CONTROLLER_PROVIDER=http).

    behaviour:
      invalid_sql  -> a plan whose only node is an SQL statement that does
                      not parse (a failed avenue; the fallback packet must
                      come back);
      timeout      -> answers after `delay_s` seconds (past the deadline);
      error        -> HTTP 500;
      not_json     -> a reply that is not a JSON object;
      deliver      -> deliver at once with no selection (the fallback).
    Every request body is kept in .requests (to check that stored text was
    sent as data, never as instructions).
    """

    BEHAVIOURS = ("invalid_sql", "timeout", "error", "not_json", "deliver")

    def __init__(self, behaviour="invalid_sql", delay_s=5.0):
        if behaviour not in self.BEHAVIOURS:
            raise ValueError("behaviour must be one of " + ", ".join(self.BEHAVIOURS))
        self.behaviour = behaviour
        self.delay_s = delay_s
        self.requests = []
        self._server = None
        self._thread = None

    def reply(self):
        if self.behaviour == "invalid_sql":
            return dict(
                plan=dict(
                    nodes=[
                        dict(
                            id="broken",
                            op="sql",
                            args=dict(
                                statement="SELEC id FRM api.messages", max_rows=5
                            ),
                        )
                    ]
                )
            )
        if self.behaviour == "deliver":
            return dict(deliver=dict(selection_handle=None, orientation_notes=[]))
        return dict(deliver=dict())

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return None

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                try:
                    stub.requests.append(json.loads(body or b"{}"))
                except ValueError:
                    stub.requests.append(None)
                if stub.behaviour == "timeout":
                    time.sleep(stub.delay_s)
                if stub.behaviour == "error":
                    self.send_response(500)
                    self.end_headers()
                    return
                content = (
                    "not json"
                    if stub.behaviour == "not_json"
                    else json.dumps(stub.reply())
                )
                payload = json.dumps(
                    dict(
                        choices=[dict(message=dict(role="assistant", content=content))],
                        usage=dict(prompt_tokens=10, completion_tokens=10),
                    )
                ).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass

        return Handler

    def __enter__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self):
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/v1"

    def env(self):
        return dict(
            THROUGHLINE_CONTROLLER_PROVIDER="http",
            THROUGHLINE_CONTROLLER_BASE_URL=self.url,
            THROUGHLINE_CONTROLLER_MODEL="bench-stub",
        )

    def fault(self):
        return Fault(
            "controller_" + self.behaviour,
            f"The librarian's controller answers with '{self.behaviour}'.",
            env=self.env(),
        )
