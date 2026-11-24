"""Asynchronous batch embedding: the vendors' cheaper, slower interface.

A historic load hands the backlog to the vendor as one job, which is answered
within hours at a lower price (Voyage: two thirds, Gemini: half), instead of
embedding it request by request. throughline.embedding_batches owns the job
rows; a client here only talks to the vendor:

    client = provider.batch_client()          # None: the vendor has none
    remote_id = client.submit(requests, dimensions=..., contextual=False)
    state = client.poll(remote_id)            # {'status': pending|done|failed}
    for custom_id, vectors, tokens in client.results(remote_id, state): ...

requests is a list of (custom_id, texts). Each yields one result with one
vector per text, or vectors None when that request failed. With
contextual=True the texts of a request are the chunks of one document.

The Gemini client follows the published schema of asyncBatchEmbedContent,
which has no worked REST example for inline requests; it accepts both
spellings of the job state and looks for the responses where either version
of the reference puts them. A job that cannot be read is reported failed, and
a failed job only returns its texts to the ordinary worker.
"""

import json

from . import http, vendors
from .base import ProviderUnavailable


class BatchClient:
    # Requests in one job, and estimated tokens in one request.
    max_requests = 50000
    request_tokens = vendors.REQUEST_TOKENS
    request_items = 128
    contextual = False

    def __init__(self, provider):
        self.provider = provider

    def submit(self, requests, *, dimensions, contextual=False):
        raise NotImplementedError

    def poll(self, remote_id):
        raise NotImplementedError

    def results(self, remote_id, state):
        raise NotImplementedError

    def cancel(self, remote_id):
        return False


class VoyageBatch(BatchClient):
    """Files API + /v1/batches: a JSONL file of requests, a JSONL file of
    answers matched by custom_id."""

    max_requests = 50000

    def _call(self, method, path, **kwargs):
        p = self.provider
        return http.request(
            method,
            p.base + path,
            key=p.key,
            timeout=max(p.timeout, 120),
            what="embedding batch",
            retries=3,
            **kwargs,
        )

    def submit(self, requests, *, dimensions, contextual=False):
        lines = []
        for custom_id, texts in requests:
            body = {"inputs": [list(texts)]} if contextual else {"input": list(texts)}
            lines.append(json.dumps({"custom_id": custom_id, "body": body}))
        uploaded = self._call(
            "POST",
            "/files",
            data={"purpose": "batch"},
            files={
                "file": (
                    "throughline-embeddings.jsonl",
                    ("\n".join(lines) + "\n").encode("utf-8"),
                    "application/jsonl",
                )
            },
        )
        params = {
            "model": self.provider.model,
            "input_type": "document",
            "output_dimension": dimensions,
        }
        created = self._call(
            "POST",
            "/batches",
            payload={
                "endpoint": "/v1/contextualizedembeddings"
                if contextual
                else "/v1/embeddings",
                "completion_window": "12h",
                "request_params": params,
                "input_file_id": _text(uploaded, "id"),
            },
        )
        return _text(created, "id")

    def poll(self, remote_id):
        data = self._call("GET", "/batches/" + remote_id)
        status = data.get("status")
        if status == "completed":
            return dict(status="done", output_file_id=data.get("output_file_id"))
        if status in ("failed", "cancelled", "expired"):
            return dict(status="failed", detail=str(status))
        return dict(status="pending", detail=str(status))

    def results(self, remote_id, state):
        file_id = state.get("output_file_id")
        if not file_id:
            return
        body = self._call("GET", f"/files/{file_id}/content", raw=True)
        for line in body.decode("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                custom_id = row["custom_id"]
            except (ValueError, KeyError, TypeError):
                continue
            answer = (row.get("response") or {}).get("body")
            vectors, tokens = None, 0
            if isinstance(answer, dict) and not row.get("error"):
                try:
                    data = answer["data"]
                    if data and isinstance(data[0].get("data"), list):
                        # Contextual: one document per request.
                        data = data[0]["data"]
                    vectors = vendors._indexed(data)
                    tokens = http.reported_tokens(answer, 0)
                except (KeyError, TypeError, IndexError, ProviderUnavailable):
                    vectors = None
            yield custom_id, vectors, tokens

    def cancel(self, remote_id):
        self._call("POST", f"/batches/{remote_id}/cancel", payload={})
        return True


class GeminiBatch(BatchClient):
    """models/{model}:asyncBatchEmbedContent with inline requests: one text
    per request, answered in request order."""

    # Inline requests must stay under 20 MB in total; a text is at most a
    # conversation chunk (about 6,000 characters).
    max_requests = 2000
    request_items = 1

    def _call(self, method, path, **kwargs):
        p = self.provider
        return http.request(
            method,
            p.base + path,
            headers={"x-goog-api-key": p.key} if p.key else None,
            timeout=max(p.timeout, 120),
            what="embedding batch",
            retries=3,
            **kwargs,
        )

    def submit(self, requests, *, dimensions, contextual=False):
        p = self.provider
        inline = []
        for custom_id, texts in requests:
            (text,) = texts
            inline.append(
                {
                    "request": p.request_for(text, p.model, dimensions, "document"),
                    "metadata": {"key": custom_id},
                }
            )
        created = self._call(
            "POST",
            f"/models/{p.model}:asyncBatchEmbedContent",
            payload={
                "batch": {
                    "model": "models/" + p.model,
                    "displayName": "throughline-embeddings",
                    "inputConfig": {"requests": {"requests": inline}},
                }
            },
        )
        # The answer is an operation whose metadata is the batch; the batch
        # name ("batches/...") is what is polled.
        metadata = created.get("metadata")
        name = (metadata.get("name") if isinstance(metadata, dict) else None) or (
            created.get("name")
        )
        if not isinstance(name, str) or not name:
            raise ProviderUnavailable("invalid embedding batch response")
        # The order of the requests is the order of the answers.
        return json.dumps({"name": name, "ids": [r[0] for r in requests]})

    @staticmethod
    def _remote(remote_id):
        try:
            value = json.loads(remote_id)
            return value["name"], list(value["ids"])
        except (ValueError, KeyError, TypeError):
            raise ProviderUnavailable("unknown embedding batch") from None

    def poll(self, remote_id):
        name, _ = self._remote(remote_id)
        data = self._call("GET", "/" + name)
        metadata = (
            data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        )
        state = str(metadata.get("state") or data.get("state") or "")
        if state.endswith("_SUCCEEDED") or (data.get("done") and "response" in data):
            return dict(status="done", body=data)
        if state.endswith(("_FAILED", "_CANCELLED", "_EXPIRED")) or data.get("error"):
            return dict(status="failed", detail=state or "error")
        return dict(status="pending", detail=state)

    def results(self, remote_id, state):
        _, ids = self._remote(remote_id)
        rows = _find(state.get("body"), "inlinedResponses")
        while isinstance(rows, dict):
            rows = rows.get("inlinedResponses")
        if not isinstance(rows, list) or len(rows) != len(ids):
            return
        dimensions = self.provider.dimensions
        for custom_id, row in zip(ids, rows):
            vectors, tokens = None, 0
            answer = row.get("response") if isinstance(row, dict) else None
            if isinstance(answer, dict):
                try:
                    vectors = [vendors.fit(answer["embedding"]["values"], dimensions)]
                    tokens = vendors.gemini_tokens(answer, 0)
                except (KeyError, TypeError):
                    vectors = None
            yield custom_id, vectors, tokens

    def cancel(self, remote_id):
        name, _ = self._remote(remote_id)
        self._call("POST", f"/{name}:cancel", payload={})
        return True


def _text(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ProviderUnavailable("invalid embedding batch response")
    return value


def _find(value, key):
    """The first value stored under `key` anywhere in a JSON value."""
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            found = _find(item, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find(item, key)
            if found is not None:
                return found
    return None
