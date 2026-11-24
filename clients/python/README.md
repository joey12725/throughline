# throughline-client (Python)

Snowpark-style programmatic Python client for Throughline. Every DataFrame chain stays lazy and pushes down to a single read-only parameterized PostgreSQL `SELECT` over `api.*` views (enforcing Throughline's `throughline_reader` role, asker security barriers, and audit log). Includes full parity with the Throughline MCP server (`find`, `timeline`, `orient`, `sql`, `person`, `describe`) plus NDJSON/CSV streaming and arrival feeds.

Runtime dependency: `httpx` only (`pandas` and `polars` are optional).

## Quickstart

```python
import throughline_client as tl
from throughline_client import F, col

# Reads THROUGHLINE_URL, THROUGHLINE_TOKEN, optional THROUGHLINE_ASKER
with tl.connect() as session:
    # 1. Snowpark-style lazy pushdown DataFrame
    df = (
        session.channel("synthetic.chat")
        .select("happened_at", "author_handle", "content", "rating")
        .filter((col("happened_at") >= "2020-01-01") & col("rating").is_not_null())
        .order_by(col("happened_at").desc())
        .limit(20)
    )
    print(df.explain())
    df.show()

    # 2. Server-side aggregation pushed to PostgreSQL
    by_day = (
        session.table("messages")
        .filter(col("channel").isin(["chat.sms", "email.gmail"]))
        .group_by(F.date_trunc("day", "happened_at").alias("day"), "channel")
        .agg(F.count("*").alias("messages"))
        .order_by(col("day").desc())
        .collect()
    )

    # 3. Stream arbitrarily large datasets lazily (NDJSON, past the 10k / 4 MiB cap)
    for row in session.table("messages").filter(col("channel") == "chat.sms").stream():
        process(row)

    # 4. Full MCP parity
    episodes = session.find("purple kites", k=10)
    history = session.timeline(["orchard", "moon jam"], semantic=False)
    snapshot = session.orient(days=14, people=["Zibble"])
    briefs = session.person("Zibble")
    schema = session.describe()

    # 5. Parameterized SQL ($1, $2) — safe against SQL injection
    res = session.sql(
        "SELECT id, happened_at, content FROM api.messages WHERE channel = $1 AND happened_at >= $2 LIMIT $3",
        ["synthetic.chat", "2020-01-01T00:00:00Z", 50],
    )
```

## Optional DataFrame adapters

```python
pdf = session.channel("health.oura.sleep").filter(col("happened_at") >= "2026-01-01").to_pandas()
pldf = session.table("visits").to_polars()
session.table("messages").filter(col("channel") == "synthetic.chat").to_csv("export.csv")
```
