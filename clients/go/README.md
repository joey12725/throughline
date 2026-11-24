# Throughline Go Client

Zero-dependency (Go stdlib only) programmatic client for Throughline with a Snowpark-style lazy pushdown `DataFrame` API, streaming NDJSON/CSV reads, typed struct decoding (`CollectAs[T]`, `StreamAs[T]`), and full MCP parity (`Find`, `Timeline`, `Orient`, `SQL`, `Person`, `Describe`).

## Quickstart

```go
package main

import (
	"context"
	"fmt"
	"log"

	tl "github.com/joey12725/throughline/clients/go"
)

type Message struct {
	ID         int64  `json:"id"`
	HappenedAt string `json:"happened_at"`
	Channel    string `json:"channel"`
	Content    string `json:"content"`
}

func main() {
	ctx := context.Background()
	session, err := tl.FromEnv() // THROUGHLINE_URL, THROUGHLINE_TOKEN, optional THROUGHLINE_ASKER
	if err != nil {
		log.Fatal(err)
	}

	// 1. Snowpark-style lazy pushdown DataFrame -> typed structs
	df := session.Table("messages").
		Select("id", "happened_at", "channel", "content").
		Where(tl.Col("channel").Eq("synthetic.chat").And(tl.Col("content").Contains("orchard"))).
		OrderBy(tl.Col("happened_at").Desc()).
		Limit(20)

	msgs, err := tl.CollectAs[Message](ctx, df)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("loaded %d messages\n", len(msgs))

	// 2. Stream arbitrarily large datasets lazily (NDJSON)
	stream, err := tl.StreamDF[Message](ctx, session.Table("messages").Select("id", "happened_at", "channel", "content"), nil)
	if err != nil {
		log.Fatal(err)
	}
	defer stream.Close()
	for stream.Next() {
		m := stream.Row()
		_ = m
	}
	if err := stream.Err(); err != nil {
		log.Fatal(err)
	}

	// 3. Full MCP parity
	_, _ = session.Find(ctx, tl.FindRequest{Query: "purple kites", K: 10})
	_, _ = session.Timeline(ctx, tl.TimelineRequest{Queries: []string{"orchard", "moon jam"}})
	_, _ = session.Orient(ctx, tl.OrientRequest{Days: 14, People: []string{"Zibble"}})
	_, _ = session.Person(ctx, "Zibble", 5)
	_, _ = session.Describe(ctx, "")
}
```
