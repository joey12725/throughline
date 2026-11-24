"""Classifiers, the annotation attribute registry and group annotations over
HTTP (migration 0068, docs/v11/classifiers.md).

Reads and definitions: unrestricted owner and agent tokens (app-role reads of
tl tables). An agent's definition is stored 'proposed'; approving, pausing,
running, backfilling and taking marks back are owner-only, as are direct
group annotations. try runs a definition on a sample and writes nothing but
the model ledger (it spends: Jev or the LLM).

Write routes take an Idempotency-Key and answer with a receipt.
"""

from fastapi import APIRouter, HTTPException, Request

from ... import classifiers, writes
from ...classifiers import groups, registry, threads
from ..deps import Token, audit, authenticated_connection, require, require_unrestricted

router = APIRouter()


def _handle(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PermissionError as exc:
        raise HTTPException(403, str(exc) or "forbidden") from None
    except LookupError as exc:
        raise HTTPException(404, str(exc) or "not found") from None
    except (classifiers.ClassifierError, registry.AttributeError_) as exc:
        raise HTTPException(422, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


def _reader(token):
    require_unrestricted(token)
    require(token, "owner", "agent")


def _owner(token):
    require_unrestricted(token)
    require(token, "owner")


# --- guide, attributes ----------------------------------------------------------


@router.get("/v1/classifiers/guide")
def classifier_guide(token: Token):
    """Everything an agent needs to write a classifier: workflow, contract, examples."""
    _reader(token)
    return classifiers.guide()


@router.get("/v1/annotation-attributes")
def attribute_list(token: Token, include_retired: bool = False):
    _reader(token)
    with authenticated_connection(token) as conn:
        rows = registry.list_attributes(conn, include_retired)
    audit(token, len(rows))
    return {"attributes": rows}


@router.post("/v1/annotation-attributes")
def attribute_define(data: dict, token: Token, request: Request):
    _reader(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            row = _handle(registry.define, conn, token, data)
            receipt = writes.receipt(conn, "annotation_attribute", row["name"])
            audit(token, 1, attribute=row["name"])
            return slot.store(200, {"attribute": row, "receipt": receipt})


@router.post("/v1/annotation-attributes/{name}/retire")
def attribute_retire(name: str, token: Token, request: Request):
    _owner(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, {"name": name}) as slot:
            if slot.replayed:
                return slot.response
            row = _handle(registry.retire, conn, token, name)
            receipt = writes.receipt(conn, "annotation_attribute", row["name"])
            return slot.store(200, {"attribute": row, "receipt": receipt})


# --- classifiers -----------------------------------------------------------------


@router.get("/v1/classifiers")
def classifier_list(token: Token):
    _reader(token)
    with authenticated_connection(token) as conn:
        rows = classifiers.list_classifiers(conn)
        sandbox_mode = _handle(classifiers.sandbox.mode)
    audit(token, len(rows))
    return {"classifiers": rows, "sandbox": sandbox_mode}


@router.post("/v1/classifiers/check")
def classifier_check(data: dict, token: Token):
    """Validate a definition without storing or running it."""
    _reader(token)
    definition = data.get("definition", data)
    errors, out = [], {}
    if not isinstance(definition, dict):
        raise HTTPException(400, "definition must be an object")
    kind = definition.get("kind") or ("python" if definition.get("code") else "message")
    if kind == "python":
        out = classifiers.check.check_code(definition.get("code"))
    with authenticated_connection(token) as conn:
        try:
            classifiers.engine._draft(conn, token, definition)
        except (classifiers.ClassifierError, registry.AttributeError_, ValueError) as exc:
            msg = str(exc)
            if not msg.startswith("the code does not pass"):
                errors.append(msg)
    all_errors = list(out.get("errors") or []) + errors
    return {"ok": not all_errors, "errors": all_errors, "warnings": out.get("warnings") or [],
            "grain": out.get("grain"), "kind": kind}


@router.post("/v1/classifiers/try")
def classifier_try(data: dict, token: Token):
    """Run a definition (or {name}) on a sample; nothing is written but the
    model ledger. sample: {limit} | {thread_ids} | {message_ids}, channels."""
    _reader(token)
    definition = data.get("name") or data.get("definition")
    if definition is None:
        raise HTTPException(400, "give definition (an object) or name (a stored classifier)")
    with authenticated_connection(token) as conn:
        out = _handle(classifiers.try_, conn, token, definition, data.get("sample") or {})
    audit(token, len(out.get("items") or []), cost=out.get("cost"))
    return out


@router.post("/v1/classifiers/draft")
def classifier_draft(data: dict, token: Token):
    """Draft a definition from a plain-language goal with the LLM; checks it
    and lets the model repair what fails. Nothing is stored or run."""
    from ...classifiers import draft

    _reader(token)
    with authenticated_connection(token) as conn:
        try:
            out = _handle(draft.draft_definition, conn, token, data.get("goal"), kind=data.get("kind") or "auto",
                          channels=data.get("channels"), attribute=data.get("attribute"))
        except draft.DraftUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
    audit(token, 1, cost=out.get("cost"))
    return out


@router.post("/v1/classifiers")
def classifier_define(data: dict, token: Token, request: Request):
    """Create or change a classifier (owner: active; agent: proposed)."""
    _reader(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            out = _handle(classifiers.define, conn, token, data)
            out["receipt"] = writes.receipt(conn, "classifier", out["classifier"]["id"])
            audit(token, 1, classifier=out["classifier"]["name"], status=out["status"])
            return slot.store(200, out)


@router.get("/v1/classifiers/{name}")
def classifier_get(name: str, token: Token):
    _reader(token)
    with authenticated_connection(token) as conn:
        out = _handle(classifiers.get, conn, name)
    audit(token, 1)
    return out


@router.get("/v1/classifiers/{name}/results")
def classifier_results(name: str, token: Token, limit: int = 50, offset: int = 0, verdict: str | None = None):
    _reader(token)
    with authenticated_connection(token) as conn:
        out = _handle(classifiers.results, conn, name, limit=limit, offset=offset, verdict=verdict)
    audit(token, len(out["items"]))
    return out


@router.post("/v1/classifiers/{name}/status")
def classifier_status(name: str, data: dict, token: Token, request: Request):
    """Owner: approve (status active), pause, resume (active) or reject."""
    _owner(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, {"name": name, **data}) as slot:
            if slot.replayed:
                return slot.response
            row = _handle(classifiers.set_status, conn, token, name, data.get("status"))
            receipt = writes.receipt(conn, "classifier", row["id"])
            audit(token, 1, classifier=name, status=row["status"])
            return slot.store(200, {"classifier": row, "receipt": receipt})


@router.post("/v1/classifiers/{name}/run")
def classifier_run(name: str, data: dict, token: Token, request: Request):
    """Owner: run an active classifier now (it also runs every maintenance tick)."""
    _owner(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, {"name": name, **data}) as slot:
            if slot.replayed:
                return slot.response
            row = _handle(classifiers.engine._row, conn, name)
            conn.commit()
            out = _handle(classifiers.run, name, trigger="manual", max_items=data.get("max_items"),
                          thread_ids=data.get("thread_ids"))
            out["receipt"] = writes.receipt(conn, "classifier", row["id"])
            audit(token, out.get("threads", out.get("judged", 0)), classifier=name, status=out.get("status"))
            return slot.store(200, out)


@router.post("/v1/classifiers/{name}/backfill")
def classifier_backfill(name: str, data: dict, token: Token, request: Request):
    """Owner: also judge older items ('all', '<n>d' or an ISO date)."""
    _owner(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, {"name": name, **data}) as slot:
            if slot.replayed:
                return slot.response
            row = _handle(classifiers.backfill, conn, token, name, data.get("since", "all"))
            receipt = writes.receipt(conn, "classifier", row["id"])
            return slot.store(200, {"classifier": classifiers.engine.present(row), "receipt": receipt})


@router.post("/v1/classifiers/{name}/retract-marks")
def classifier_retract(name: str, token: Token, request: Request):
    """Owner: take back every annotation the classifier wrote."""
    _owner(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, {"name": name}) as slot:
            if slot.replayed:
                return slot.response
            out = _handle(classifiers.retract_all, conn, token, name)
            row = classifiers.engine._row(conn, name)
            out["receipt"] = writes.receipt(conn, "classifier", row["id"])
            audit(token, out["annotations"], classifier=name)
            return slot.store(200, out)


# --- threads and group annotations ------------------------------------------------


@router.get("/v1/threads/{thread_id}/classification")
def thread_classification(thread_id: int, token: Token, attribute: str = "media_noise",
                          classifier: str | None = None, limit: int = 400):
    """A thread for review: participants, messages with their effective value
    of `attribute`, the active group annotations and classifier verdicts."""
    _reader(token)
    limit = max(1, min(int(limit), 2000))
    with authenticated_connection(token) as conn:
        data, rows = _handle(threads.load, conn, thread_id)
        flags = {}
        for r in conn.execute(
            """SELECT DISTINCT ON (m.id) m.id,a.value,a.method,a.group_annotation_id,a.job
            FROM tl.messages m JOIN tl.annotations a ON a.target_channel_id=m.channel_id AND a.target_external_id=m.external_id
            WHERE m.thread_id=%s AND m.superseded_by IS NULL AND a.attribute=%s AND a.status='active'
            ORDER BY m.id,CASE a.method WHEN 'owner' THEN 0 WHEN 'rule' THEN 2 ELSE 3 END,a.id DESC""",
            (thread_id, attribute),
        ):
            flags[r["id"]] = {"value": r["value"], "method": r["method"], "group_annotation_id": r["group_annotation_id"], "job": r["job"]}
        verdicts = [dict(r) for r in conn.execute(
            """SELECT c.name,c.kind,s.result,s.judged_at,s.version FROM tl.classifier_threads s
            JOIN tl.classifiers c ON c.id=s.classifier_id WHERE s.thread_id=%s
            AND (%s::text IS NULL OR c.name=%s) ORDER BY s.judged_at DESC""",
            (thread_id, classifier, classifier))]
        group_rows = groups.for_thread(conn, thread_id)
    counts = {}
    for m in data["messages"]:
        counts[m["participant"]] = counts.get(m["participant"], 0) + 1
    for p in data["participants"]:
        p["turns"] = counts.get(p["key"], 0)
        p["flagged"] = sum(1 for m in data["messages"] if m["participant"] == p["key"] and m["id"] in flags)
    messages = [{**m, "annotation": flags.get(m["id"])} for m in data["messages"][:limit]]
    audit(token, len(messages))
    return {
        "thread": {k: data[k] for k in ("id", "channel", "title", "started_at", "duration_s")},
        "attribute": attribute,
        "participants": data["participants"],
        "messages": messages,
        "messages_total": len(data["messages"]),
        "flagged_total": len(flags),
        "group_annotations": group_rows,
        "verdicts": verdicts,
    }


@router.post("/v1/group-annotations")
def group_annotation_create(data: dict, token: Token, request: Request):
    """Owner: a verdict on a whole thread or on one participant of it, copied
    onto the covered messages. value null makes a note."""
    _owner(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            if not isinstance(data.get("thread_id"), int):
                raise HTTPException(400, "thread_id is required")
            out = _handle(
                groups.owner_set, conn, token, thread_id=data["thread_id"], attribute=data.get("attribute"),
                participant_key=data.get("participant_key"), value=data.get("value"),
                detail=data.get("detail"), reason=data.get("reason"),
            )
            out["receipt"] = writes.receipt(conn, "group_annotation", out["group_annotation"]["id"])
            audit(token, 1, thread_id=data["thread_id"])
            return slot.store(200, out)


@router.post("/v1/group-annotations/{group_annotation_id}/retract")
def group_annotation_retract(group_annotation_id: int, token: Token, request: Request):
    _owner(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, {"id": group_annotation_id}) as slot:
            if slot.replayed:
                return slot.response
            out = _handle(groups.retract, conn, token, group_annotation_id)
            out["receipt"] = writes.receipt(conn, "group_annotation", group_annotation_id)
            return slot.store(200, out)
