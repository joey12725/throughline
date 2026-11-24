"""Proposal queue and concept linking (BRN-12, LIB-6).

Proposals are the shared owner-approval queue for agents, the librarian, and
restricted scopes (annotations, link types, links, concepts, provenance,
corrections, recipes). Every referenced message id or (channel, external_id)
is checked under the caller's scope; hidden and unknown records return the
same LookupError. Accepting a proposal creates the target object through its
validated path with its originating method and job, never owner.

Functions take an app-role connection; routes authorize first. Errors:
ValueError (400), PermissionError (403), LookupError (404).
"""

from psycopg.types.json import Jsonb

from . import lineage
from .ingest import add_link

PROPOSAL_KINDS = (
    "link",
    "concept",
    "correction",
    "recipe",
    "provenance",
    "link_type",
    "annotation",
)
PROPOSAL_ORIGINS = ("agent", "librarian")
MAX_SOURCES = lineage.MAX_SOURCES


def _id(value):
    return isinstance(value, int) and not isinstance(value, bool)


def link_concept(conn, token, target, entity, data=None):
    """An 'about' link from a page (revision message) or message to an entity
    of any kind (BRN-12): {id} or {kind, external_ref, name}; a new kind needs
    no migration. Evidence names the source messages."""
    data = data or {}
    if not isinstance(token, dict) or token.get("kind") not in ("owner", "agent"):
        raise PermissionError("token cannot link concepts")
    if not isinstance(target, dict):
        raise ValueError("target is {message_id} or {channel, external_id}")
    if not isinstance(entity, dict):
        raise ValueError("entity is {id} or {kind, external_ref, name}")
    if _id(target.get("message_id")):
        start = {"kind": "message", "id": target["message_id"]}
        evidence_ids = [target["message_id"]]
    elif isinstance(target.get("channel"), str) and isinstance(
        target.get("external_id"), str
    ):
        start = {"channel": target["channel"], "external_id": target["external_id"]}
        evidence_ids = []
    else:
        raise ValueError("target is {message_id} or {channel, external_id}")
    if _id(entity.get("id")):
        end = {"kind": "entity", "id": entity["id"]}
    elif (
        isinstance(entity.get("kind"), str)
        and entity["kind"].strip()
        and isinstance(entity.get("external_ref"), str)
        and entity["external_ref"].strip()
    ):
        end = {
            "kind": entity["kind"].strip(),
            "external_ref": entity["external_ref"],
            "name": entity.get("name"),
        }
    else:
        raise ValueError("entity is {id} or {kind, external_ref, name}")
    link = {
        "type": "about",
        "from": start,
        "to": end,
        "evidence": {"message_ids": evidence_ids},
    }
    if token["kind"] == "owner":
        link["method"] = data.get("method") or "owner"
    else:
        link.update(
            method="model",
            job=data.get("job") or "agent",
            job_version=str(data.get("job_version") or "unversioned"),
        )
    for key in ("job", "job_version", "confidence"):
        if data.get(key) is not None:
            link[key] = data[key]
    return add_link(conn, link)


def _named_records(value, depth=0):
    """(channel, external_id) pairs a JSON value names: objects with string
    channel and external_id, or page_channel and page_external_id."""
    found = set()
    if depth > 64:
        raise ValueError("payload is nested too deeply")
    if isinstance(value, dict):
        for ch, ext in (
            ("channel", "external_id"),
            ("page_channel", "page_external_id"),
        ):
            if isinstance(value.get(ch), str) and isinstance(value.get(ext), str):
                found.add((value[ch], value[ext]))
        for item in value.values():
            found |= _named_records(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            found |= _named_records(item, depth + 1)
    return found


def propose(
    conn,
    scope,
    token,
    kind,
    payload,
    evidence=None,
    origin="agent",
    investigation_id=None,
):
    """Queue a proposal. Every message id the payload or evidence names must
    be visible to the caller's scope; a hidden and an unknown id give the same
    LookupError. Never creates the object itself."""
    from .query import reader_transaction

    if not isinstance(token, dict) or token.get("kind") not in ("owner", "agent"):
        raise PermissionError("token cannot propose")
    if kind not in PROPOSAL_KINDS:
        raise ValueError("proposal kind must be one of " + ", ".join(PROPOSAL_KINDS))
    if origin not in PROPOSAL_ORIGINS:
        raise ValueError("origin must be one of " + ", ".join(PROPOSAL_ORIGINS))
    if not isinstance(payload, dict) or not payload:
        raise ValueError("payload must be a non-empty object")
    evidence = {} if evidence is None else evidence
    if not isinstance(evidence, (dict, list)):
        raise ValueError("evidence must be an object or a list")
    ids = conn.execute(
        "SELECT ARRAY(SELECT DISTINCT x FROM unnest(tl.json_message_ids(%s)||tl.json_message_ids(%s)) x ORDER BY x) AS ids",
        (Jsonb(payload), Jsonb(evidence)),
    ).fetchone()["ids"]
    named = sorted(_named_records(payload) | _named_records(evidence))
    if len(named) > MAX_SOURCES:
        raise ValueError(
            f"at most {MAX_SOURCES} records named by channel and external_id"
        )
    if ids or named:
        with reader_transaction(scope) as reader:
            visible = reader.execute(
                "SELECT count(*) FILTER (WHERE api.message_visible(i)) AS n FROM unnest(%s::bigint[]) i",
                (ids,),
            ).fetchone()["n"]
            found = 0
            if named:
                found = reader.execute(
                    "SELECT count(*) AS n FROM unnest(%s::text[],%s::text[]) p(ch,ext) WHERE EXISTS(SELECT 1 FROM api.messages m WHERE m.channel=p.ch AND m.external_id=p.ext)",
                    ([c for c, _ in named], [e for _, e in named]),
                ).fetchone()["n"]
        if visible != len(ids) or found != len(named):
            raise LookupError("referenced record not found")
    return conn.execute(
        "INSERT INTO tl.proposals(kind,payload,evidence,origin,token_id,asker,investigation_id) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (
            kind,
            Jsonb(payload),
            Jsonb(evidence),
            origin,
            token.get("id"),
            token.get("asker"),
            investigation_id,
        ),
    ).fetchone()


def list_proposals(conn, token, unrestricted, status=None, limit=100):
    """Owner (unrestricted): every proposal. Others: their own token and asker."""
    if status is not None and status not in ("pending", "accepted", "rejected"):
        raise ValueError("status must be pending, accepted or rejected")
    everyone = token.get("kind") == "owner" and unrestricted
    return conn.execute(
        "SELECT * FROM tl.proposals WHERE (%s OR (token_id=%s AND asker IS NOT DISTINCT FROM %s)) AND (%s::text IS NULL OR status=%s) ORDER BY id DESC LIMIT %s",
        (
            everyone,
            token.get("id"),
            token.get("asker"),
            status,
            status,
            max(1, min(int(limit), 1000)),
        ),
    ).fetchall()


def _accept(conn, token, proposal):
    from . import annotations, vocabulary

    payload = dict(proposal["payload"])
    origin = proposal["origin"]
    method = (
        payload.get("method") if payload.get("method") in ("rule", "model") else "model"
    )
    version = str(payload.get("job_version") or "unversioned")
    job = {
        "job": origin,
        "job_version": version,
        "method": method,
        "token_id": proposal["token_id"],
        "asker": proposal["asker"],
        "origin": "agent" if origin == "agent" else "job",
        "accepted_by": token["id"],
    }
    kind = proposal["kind"]
    if kind in ("link", "concept"):
        if kind == "concept":
            target, entity = payload.get("target"), payload.get("entity")
            link = {
                "type": "about",
                "from": target,
                "to": entity
                if isinstance(entity, dict) and "external_ref" in entity
                else {"kind": "entity", "id": (entity or {}).get("id")},
                "evidence": payload.get("evidence") or proposal["evidence"] or {},
            }
        else:
            link = {
                k: val
                for k, val in payload.items()
                if k not in ("method", "job", "job_version")
            }
        link.update(method=method, job=origin, job_version=version)
        if not isinstance(link.get("evidence"), dict) or not link["evidence"].get(
            "message_ids"
        ):
            ids = list(proposal["message_ids"] or [])
            link["evidence"] = dict(link.get("evidence") or {}, message_ids=ids)
        row = add_link(conn, link)
        return {"link_id": row["id"]}
    if kind == "link_type":
        if payload.get("merge_into"):
            return {
                "merge": vocabulary.merge(
                    conn, token, payload.get("type"), payload["merge_into"]
                )
            }
        row = vocabulary.register(
            conn,
            token,
            payload.get("type"),
            payload.get("description"),
            payload.get("inverse"),
        )
        return {"type": row["type"]}
    if kind == "annotation":
        row = annotations.create(
            conn,
            job,
            payload.get("target"),
            payload.get("attribute"),
            payload.get("value"),
            reason=payload.get("reason"),
            validity={k: payload[k] for k in lineage.VALIDITY if k in payload},
            target_version=payload.get("target_version"),
            confidence=payload.get("confidence"),
        )
        if row.get("annotation") is None:
            raise ValueError(
                "an owner annotation of this attribute exists; the proposal cannot override it"
            )
        return {"annotation_id": row["annotation"]["id"]}
    if kind == "provenance":
        derived = payload.get("derived") or {}
        written = lineage.add_provenance(
            conn,
            origin,
            derived.get("kind"),
            derived.get("id"),
            payload.get("sources") or [],
            payload.get("relation") or "derived_from",
            "agent" if origin == "agent" else "job",
            origin,
            version,
        )
        return {"written": written}
    return {"recorded": True}


def decide(conn, token, proposal_id, accept, note=None):
    """Accept (create the object through its validated path, with the
    originating method and job, never owner) or reject a pending proposal.
    Owner only."""
    if not isinstance(token, dict) or token.get("kind") != "owner":
        raise PermissionError("only the owner decides proposals")
    proposal = conn.execute(
        "SELECT * FROM tl.proposals WHERE id=%s FOR UPDATE", (proposal_id,)
    ).fetchone()
    if not proposal:
        raise LookupError("proposal not found")
    if proposal["status"] != "pending":
        raise ValueError(f"proposal is already {proposal['status']}")
    result = _accept(conn, token, proposal) if accept else {}
    if note:
        result["note"] = str(note)[:2000]
    return conn.execute(
        "UPDATE tl.proposals SET status=%s,decided_by=%s,decided_at=clock_timestamp(),result=%s WHERE id=%s RETURNING *",
        ("accepted" if accept else "rejected", token["id"], Jsonb(result), proposal_id),
    ).fetchone()
