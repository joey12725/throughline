"""Transcripts over HTTP (IDN-6, IDN-8).

POST /v1/transcripts/noise/run {policy, dry_run}: owner tokens with an
unrestricted scope; one bounded media-noise run (fail open: the response
says partial with the reasons, nothing is hidden). GET
/v1/transcripts/noise/status: owner or agent, unrestricted. GET
/v1/transcripts/speakers: the speaker inbox (api.speaker_inbox), unrestricted
only, read through the reader path. POST /v1/transcripts/speakers/assign
and /ignore: owner tokens with an unrestricted scope.

Writes use the pinned writes interface (throughline.writes: idempotency key
and receipts) when it is present: the noise run answers with a receipt of
kind noise_run (the run row read back), the speaker routes with one
annotation receipt per turn written.
"""

from fastapi import APIRouter, HTTPException, Request

from ... import transcripts
from ..deps import (
    Token,
    audit,
    authenticated_connection,
    require,
    require_unrestricted,
    scope_of,
)

router = APIRouter()
MAX_RECEIPTS = 1000


def _writes():
    """throughline.writes (the write contract) when present, else None."""
    try:
        from ... import writes
    except ImportError:
        return None
    if not hasattr(writes, "idempotent"):
        return None
    return writes


def _run(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except PermissionError as error:
        raise HTTPException(403, str(error) or "forbidden") from None
    except LookupError as error:
        raise HTTPException(404, str(error) or "not found") from None


def _body(data):
    if not isinstance(data, dict):
        raise HTTPException(422, "body must be an object")
    return data


def _write(token, request, data, work):
    """Run work(conn) -> result in one app-role transaction, through the
    writes interface when present: a replayed key returns the stored
    response; otherwise the response is stored before commit."""
    writes = _writes()
    with authenticated_connection(token) as conn:
        if writes is None:
            return work(conn, None)
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            result = work(conn, writes)
            slot.store(200, result)
            return result


def _run_receipt(conn, writes, run_id):
    """Receipt of a noise run: the run row read back (counts and ids only).
    A dry run writes no row; its receipt has id 0 and status dry_run."""
    row = None
    if run_id is not None:
        row = conn.execute(
            "SELECT id,status,dry_run,finished_at,summary FROM tl.transcript_noise_runs WHERE id=%s",
            (run_id,),
        ).fetchone()
    if row is None:
        return writes.row_receipt("noise_run", 0, {}, dict(status="dry_run"))
    return writes.row_receipt(
        "noise_run", row["id"], row, dict(written_at=row["finished_at"])
    )


def _receipts(conn, writes, ids):
    if writes is None:
        return None
    return [writes.receipt(conn, "annotation", i) for i in ids[:MAX_RECEIPTS]]


@router.post("/v1/transcripts/noise/run")
def noise_run(data: dict, token: Token, request: Request):
    """One bounded media-noise run: {recordings, turns_judged, flagged,
    by_length_bucket, model_calls, cost, status, failures}."""
    require_unrestricted(token)
    require(token, "owner")
    data = _body(data)
    unknown = set(data) - {"policy", "dry_run"}
    if unknown:
        raise HTTPException(422, "unknown keys: " + ", ".join(sorted(unknown)))
    dry_run = data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise HTTPException(422, "dry_run must be a boolean")
    policy = transcripts.policy_from(data.get("policy"))

    def work(conn, writes):
        result = transcripts.run_noise(
            conn, policy, None, dry_run, token=token, scope=scope_of(token)
        )
        if writes is not None:
            result["receipt"] = _run_receipt(conn, writes, result.get("run_id"))
        return result

    result = _write(token, request, data, work)
    audit(
        token,
        result.get("turns_judged", 0),
        flagged=result.get("flagged"),
        status=result.get("status"),
        run_id=result.get("run_id"),
    )
    return result


@router.get("/v1/transcripts/noise/status")
def noise_status(token: Token):
    """Pending and judged recordings, active flags and the latest runs."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        result = transcripts.noise_status(conn)
    audit(token, len(result["runs"]))
    return result


@router.get("/v1/transcripts/speakers")
def speakers(token: Token, max_results: int = 100):
    """The speaker inbox: unclaimed speaker labels per recording."""
    require_unrestricted(token)
    require(token, "owner", "agent")
    rows = transcripts.speaker_inbox(scope_of(token), max(1, min(max_results, 1000)))
    audit(token, len(rows))
    return dict(speakers=rows)


def _speaker_args(data):
    data = _body(data)
    thread_id, label = data.get("thread_id"), data.get("label")
    if isinstance(thread_id, bool) or not isinstance(thread_id, int):
        raise HTTPException(422, "thread_id must be a thread id")
    if not isinstance(label, str) or not label.strip():
        raise HTTPException(422, "label must be text")
    return data, thread_id, label


@router.post("/v1/transcripts/speakers/assign")
def speaker_assign(data: dict, token: Token, request: Request):
    """Owner 'speaker' annotations on the label's turns in that recording."""
    require_unrestricted(token)
    require(token, "owner")
    data, thread_id, label = _speaker_args(data)

    def work(conn, writes):
        result = _run(
            transcripts.assign_speaker,
            conn,
            token,
            thread_id,
            label,
            data.get("person_id"),
            data.get("reason"),
        )
        receipts = _receipts(conn, writes, result["annotation_ids"])
        if receipts is not None:
            result["receipts"] = receipts
        return result

    result = _write(token, request, data, work)
    audit(token, result["turns"], thread_id=thread_id, written=result["written"])
    return result


@router.post("/v1/transcripts/speakers/ignore")
def speaker_ignore(data: dict, token: Token, request: Request):
    """Owner ignore annotations on the label's turns; the label is ignored."""
    require_unrestricted(token)
    require(token, "owner")
    data, thread_id, label = _speaker_args(data)

    def work(conn, writes):
        result = _run(
            transcripts.ignore_speaker,
            conn,
            token,
            thread_id,
            label,
            data.get("reason"),
        )
        receipts = _receipts(conn, writes, result["annotation_ids"])
        if receipts is not None:
            result["receipts"] = receipts
        return result

    result = _write(token, request, data, work)
    audit(token, result["turns"], thread_id=thread_id, written=result["written"])
    return result
