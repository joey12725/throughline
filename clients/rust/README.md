# throughline-client (Rust)

Snowpark-style programmatic Rust client for Throughline. Compiles lazy `DataFrame` chains to a single parameterized PostgreSQL `SELECT` over `api.*` views, streams large result sets directly into `serde::Deserialize` structs via `POST /v1/sql/stream`, and provides full parity with the Throughline MCP server (`find`, `timeline`, `orient`, `sql`, `person`, `describe`).

## Quickstart

```rust
use serde::Deserialize;
use throughline_client::{col, count, date_trunc, FindRequest, Session};

#[derive(Debug, Deserialize)]
struct Message {
    id: i64,
    happened_at: String,
    channel: String,
    content: String,
}

fn main() -> throughline_client::Result<()> {
    // Reads THROUGHLINE_URL, THROUGHLINE_TOKEN, optional THROUGHLINE_ASKER
    let session = Session::from_env()?;

    // 1. Snowpark-style lazy pushdown DataFrame -> typed Rust structs
    let msgs: Vec<Message> = session
        .table("messages")?
        .select(&["id", "happened_at", "channel", "content"])
        .filter(col("channel").eq("synthetic.chat").and(col("content").contains("orchard")))
        .order_by(vec![col("happened_at").desc()])
        .limit(20)
        .collect_as()?;

    println!("loaded {} messages", msgs.len());

    // 2. Server-side aggregation pushed down to PostgreSQL
    let daily = session
        .table("messages")?
        .group_by_expr(vec![date_trunc("day", "happened_at").alias("day"), col("channel")])
        .agg(vec![count("*").alias("n")])
        .collect()?;
    println!("{} daily buckets", daily.len());

    // 3. Stream arbitrarily large datasets lazily (NDJSON)
    for row in session.table("messages")?.select(&["id", "happened_at", "channel", "content"]).stream_as::<Message>(None)? {
        let msg = row?;
        let _ = msg;
    }

    // 4. Full MCP parity
    let _episodes = session.find(&FindRequest {
        query: "purple kites".into(),
        k: Some(10),
        ..Default::default()
    })?;
    let _briefs = session.person("Zibble", 5)?;
    let _schema = session.describe(None)?;

    Ok(())
}
```
