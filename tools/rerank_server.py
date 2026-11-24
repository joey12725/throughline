"""A local cross-encoder rerank server for Throughline's rerank provider.

Serves POST /rerank {model, query, documents, top_n?} ->
{"results": [{"index", "relevance_score"}], "usage": {"total_tokens"}}, the
Cohere/Jina shape that throughline.providers.http.HttpReranker reads. Nothing
leaves the machine. Runs in its own environment (torch is not a Throughline
dependency):

    uv venv ~/.throughline/rerank/.venv
    uv pip install --python ~/.throughline/rerank/.venv/bin/python torch sentence-transformers fastapi uvicorn
    ~/.throughline/rerank/.venv/bin/python tools/rerank_server.py --port 8795

and point Throughline at it:

    THROUGHLINE_RERANK_PROVIDER=http
    THROUGHLINE_RERANK_BASE_URL=http://127.0.0.1:8795
    THROUGHLINE_RERANK_MODEL=BAAI/bge-reranker-v2-m3
    THROUGHLINE_DEFAULT_RANKER=cross_encoder
"""

import argparse
import threading

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

parser = argparse.ArgumentParser()
parser.add_argument("--model", default="BAAI/bge-reranker-v2-m3")
parser.add_argument("--port", type=int, default=8795)
parser.add_argument("--max-length", type=int, default=1024)
parser.add_argument("--batch-size", type=int, default=32)
args = parser.parse_args()

from sentence_transformers import CrossEncoder  # noqa: E402

model = CrossEncoder(args.model, max_length=args.max_length, automodel_args={"torch_dtype": "auto"})
try:
    model.model.half()
except Exception:
    pass
lock = threading.Lock()
app = FastAPI()


class RerankRequest(BaseModel):
    model: str | None = None
    query: str
    documents: list[str]
    top_n: int | None = None


@app.post("/rerank")
def rerank(data: RerankRequest):
    pairs = [(data.query, (doc or "")[:4000]) for doc in data.documents]
    with lock:
        scores = model.predict(pairs, batch_size=args.batch_size, activation_fn=None,
                               show_progress_bar=False) if pairs else []
    results = [dict(index=i, relevance_score=float(s)) for i, s in enumerate(scores)]
    results.sort(key=lambda r: -r["relevance_score"])
    if data.top_n:
        results = results[: data.top_n]
    tokens = sum(len(q) + len(d) for q, d in pairs) // 4
    return dict(model=args.model, results=results, usage=dict(total_tokens=tokens))


@app.get("/health")
def health():
    return dict(ok=True, model=args.model)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
