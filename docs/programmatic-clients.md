# Programmatic clients (Python, Go, Rust)

Throughline's retrieval endpoints and MCP server are optimized for agents working inside a context window. For scripts, pipelines, notebooks, and services written in **Python**, **Go**, or **Rust**, Throughline pairs two server-side SQL capabilities with Snowpark-style client libraries under [`clients/`](../clients/):

- **Parameterized read-only SQL (`POST /v1/sql`)** — bind PostgreSQL `$1, $2, ...` parameters cleanly without string interpolation or SQL injection. Bound values are never copied into `tl.audit_log` (only the `$1` template and `param_count` are recorded).
- **Streaming bulk SQL (`POST /v1/sql/stream`)** — streams raw, unshaped rows as **NDJSON** (`application/x-ndjson`) or **RFC 4180 CSV** (`text/csv`) directly from `throughline_reader` inside the same `READ ONLY` transaction, asker security barrier, and AST validator, bypassing the 10,000-row / 4 MiB JSON envelope and agent cell truncation.
- **Snowpark-style lazy pushdown `DataFrame` API** — build composable queries over `api.*` views, `api.ch_<channel>` typed views, and `api.lens_<name>` views that compile to a single parameterized PostgreSQL `SELECT` only when materialized.
- **Full MCP parity** — every MCP tool (`find`, `timeline`, `orient`, `sql`, `person`, `describe`) has a direct, structured method on `Session` across all three languages, alongside `context`, `recall`, `day`, `channels`, `status`, `pages`, and arrival `feeds`.

All three clients read `THROUGHLINE_URL`, `THROUGHLINE_TOKEN`, and optional `THROUGHLINE_ASKER` (`X-Throughline-Asker`) from the environment and speak HTTP only.

## MCP-to-client parity

| MCP tool / endpoint | Python (`throughline_client`) | Go (`clients/go`) | Rust (`throughline-client`) |
|---|---|---|---|
| `find` (`POST /v1/find`) | `s.find(query, ...)` | `s.Find(ctx, FindRequest{...})` | `s.find(&FindRequest { ... })` |
| `timeline` (`POST /v1/timeline`) | `s.timeline(queries, ...)` | `s.Timeline(ctx, TimelineRequest{...})` | `s.timeline(&TimelineRequest { ... })` |
| `orient` (`POST /v1/orient`) | `s.orient(days=..., people=...)` | `s.Orient(ctx, OrientRequest{...})` | `s.orient(&OrientRequest { ... })` |
| `sql` (`POST /v1/sql`) | `s.sql(stmt, params)` | `s.SQL(ctx, stmt, params, opts)` / `QueryAs[T]` | `s.sql(stmt, params, opts)` / `query_as::<T>` |
| Bulk SQL (`POST /v1/sql/stream`) | `s.stream_sql(...)` / `df.stream()` | `s.StreamSQL(...)` / `StreamAs[T]` | `s.stream_sql(...)` / `stream_as::<T>` |
| `person` (resolve + brief) | `s.person(name)` | `s.Person(ctx, name, limit)` | `s.person(name, limit)` |
| `describe` (`GET /v1/describe`) | `s.describe(subject=None)` | `s.Describe(ctx, subject)` | `s.describe(subject)` |
| Lazy table / channel query | `s.table("messages")`, `s.channel("...")` | `s.Table("messages")`, `s.Channel("...")` | `s.table("messages")`, `s.channel("...")` |
| Arrival cursor (`/v1/feeds/*`) | `s.feed("my_job")` | `s.Feed("my_job")` | `s.feed("my_job")` |

---

## Server-side SQL upgrades

### Parameterized `POST /v1/sql`

Pass positional PostgreSQL `$1, $2, ...` placeholders in `statement` and a JSON array in `params` (up to 200 values). Parameters are validated through PostgreSQL's real parser (`pglast`) and bound safely via psycopg — `$1` inside string literals or SQL comments is never rewritten, repeated `$1` references work, and `LIKE '%foo%'` patterns remain safe alongside parameters.

```json
POST /v1/sql
{
  "statement": "SELECT id, happened_at, content FROM api.messages WHERE channel = $1 AND happened_at >= $2::timestamptz LIMIT $3",
  "params": ["chat.sms", "2026-01-01T00:00:00Z", 50],
  "raw": true
}
```

### Streaming `POST /v1/sql/stream`

For scripts exporting or scanning large datasets (up to 1,000,000 rows per request, default 100,000):

```json
POST /v1/sql/stream
{
  "statement": "SELECT id, happened_at, author_handle, content FROM api.messages WHERE channel = $1 ORDER BY happened_at",
  "params": ["chat.sms"],
  "format": "ndjson",
  "row_cap": 100000
}
```

- `format`: `"ndjson"` (default, `application/x-ndjson`, one JSON object per line) or `"csv"` (`text/csv; charset=utf-8`, header row + RFC 4180 rows).
- Validation, parameter checking, exactness checks (`strict_exact`), and authorization happen synchronously **before** headers are sent, so invalid queries return standard `400` / `403` JSON errors.
- Rows run under `throughline_reader` (`SET TRANSACTION READ ONLY`, asker security-barrier views, statement timeout, and per-row size cap).

---

## Python (`clients/python`)

Package: [`clients/python/README.md`](../clients/python/README.md) (`throughline-client`, depends only on `httpx`; optional `[pandas]` and `[polars]` extras).

```python
import throughline_client as tl
from throughline_client import F, col

with tl.connect() as s:
    # Lazy pushdown DataFrame over a typed channel view
    df = (
        s.channel("synthetic.chat")
        .select("happened_at", "author_handle", "rating", "content")
        .filter((col("happened_at") >= "2024-01-01") & (col("rating") >= 80))
        .order_by(col("happened_at").desc())
        .limit(50)
    )
    print(df.explain())
    rows = df.collect()  # raw=True; automatically streams when unbounded

    # Server-side aggregation pushed to PostgreSQL
    daily = (
        s.table("messages")
        .group_by(F.date_trunc("day", "happened_at").alias("day"), "channel")
        .agg(F.count("*").alias("n"))
        .order_by(col("day").desc())
        .to_pandas()
    )

    # Stream arbitrarily large result sets lazily
    for row in s.table("messages").filter(col("channel") == "synthetic.chat").stream():
        ...

    # Full MCP parity
    hits = s.find("purple kites", k=10)
    briefs = s.person("Zibble")
```

## Go (`clients/go`)

Module: [`clients/go/README.md`](../clients/go/README.md) (zero external dependencies — Go stdlib only).

```go
session, err := tl.FromEnv()
if err != nil {
    log.Fatal(err)
}

type Msg struct {
    ID         int64  `json:"id"`
    HappenedAt string `json:"happened_at"`
    Content    string `json:"content"`
}

df := session.Table("messages").
    Select("id", "happened_at", "content").
    Where(tl.Col("channel").Eq("synthetic.chat").And(tl.Col("content").Contains("orchard"))).
    OrderBy(tl.Col("happened_at").Desc()).
    Limit(20)

msgs, err := tl.CollectAs[Msg](ctx, df)
```

## Rust (`clients/rust`)

Crate: [`clients/rust/README.md`](../clients/rust/README.md) (`throughline-client`: `serde`, `serde_json`, `ureq`).

```rust
use serde::Deserialize;
use throughline_client::{col, Session};

#[derive(Deserialize)]
struct Msg {
    id: i64,
    happened_at: String,
    content: String,
}

let session = Session::from_env()?;
let msgs: Vec<Msg> = session
    .table("messages")?
    .select(&["id", "happened_at", "content"])
    .filter(col("channel").eq("synthetic.chat").and(col("content").contains("orchard")))
    .order_by(vec![col("happened_at").desc()])
    .limit(20)
    .collect_as()?;
```
