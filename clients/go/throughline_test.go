package throughline_test

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	tl "github.com/joey12725/throughline/clients/go"
)

func TestDataFrameCompilesParameterizedSQL(t *testing.T) {
	s, err := tl.New(tl.Config{URL: "http://example.invalid", Token: "tok"})
	if err != nil {
		t.Fatal(err)
	}

	df := s.Channel("health.oura.sleep").
		Select("happened_at", "score").
		Where(tl.Col("happened_at").Gte("2026-01-01").And(tl.Col("score").Gt(80))).
		Where(tl.Col("note").Contains("deep")).
		OrderBy(tl.Col("happened_at").Desc()).
		Limit(25)

	sql, params, err := df.ToSQL()
	if err != nil {
		t.Fatal(err)
	}
	wantSQL := `SELECT "happened_at", "score" FROM api.ch_health_oura_sleep WHERE (("happened_at" >= $1) AND ("score" > $2)) AND ("note" ILIKE $3) ORDER BY "happened_at" DESC LIMIT 25`
	if sql != wantSQL {
		t.Fatalf("SQL mismatch:\ngot:  %s\nwant: %s", sql, wantSQL)
	}
	if len(params) != 3 || params[0] != "2026-01-01" || params[1] != 80 || params[2] != "%deep%" {
		t.Fatalf("unexpected params: %#v", params)
	}
}

func TestGroupByAndJoinParamRenumbering(t *testing.T) {
	s, _ := tl.New(tl.Config{URL: "http://example.invalid", Token: "tok"})

	agg := s.Table("messages").
		Where(tl.Col("channel").In("chat.sms", "email.gmail")).
		GroupByExpr(tl.DateTrunc("day", "happened_at").As("day"), tl.Col("channel")).
		Agg(tl.Count("*").As("n"))

	sql, params, err := agg.ToSQL()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(sql, "GROUP BY 1, 2") {
		t.Fatalf("expected positional GROUP BY 1, 2, got: %s", sql)
	}
	if len(params) != 3 || params[0] != "day" || params[1] != "chat.sms" || params[2] != "email.gmail" {
		t.Fatalf("unexpected params: %#v", params)
	}

	left := s.Table("messages").Where(tl.Col("channel").Eq("chat.sms")).Alias("m")
	right := s.Table("threads").Where(tl.Col("title").Contains("trip")).Alias("t")
	joined := left.Join(right, "thread_id", "id", "inner").Select("m.id", "t.title")

	jsql, jparams, err := joined.ToSQL()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(jsql, "INNER JOIN") || !strings.Contains(jsql, "$1") || !strings.Contains(jsql, "$2") {
		t.Fatalf("unexpected join SQL: %s", jsql)
	}
	if len(jparams) != 2 || jparams[0] != "chat.sms" || jparams[1] != "%trip%" {
		t.Fatalf("unexpected join params: %#v", jparams)
	}
}

func TestSessionHTTPStreamingAndMCPParity(t *testing.T) {
	type msg struct {
		ID      int64  `json:"id"`
		Channel string `json:"channel"`
		Content string `json:"content"`
	}

	var gotAsker, gotClient string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer secret-tok" {
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"detail":"unauthorized"}`))
			return
		}
		gotAsker = r.Header.Get("X-Throughline-Asker")
		gotClient = r.Header.Get("X-Throughline-Client")

		switch r.URL.Path {
		case "/v1/sql":
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body["raw"] != true {
				t.Errorf("expected raw=true, got %#v", body["raw"])
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"rows":      []map[string]any{{"id": 1, "channel": "synthetic.chat", "content": "hello"}},
				"row_count": 1,
				"truncated": false,
			})
		case "/v1/sql/stream":
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body["format"] == "csv" {
				w.Header().Set("Content-Type", "text/csv; charset=utf-8")
				_, _ = w.Write([]byte("id,channel,content\n1,synthetic.chat,hello\n2,synthetic.chat,world\n"))
				return
			}
			w.Header().Set("Content-Type", "application/x-ndjson")
			_, _ = w.Write([]byte("{\"id\":1,\"channel\":\"synthetic.chat\",\"content\":\"hello\"}\n{\"id\":2,\"channel\":\"synthetic.chat\",\"content\":\"world\"}\n"))
		case "/v1/find":
			_ = json.NewEncoder(w).Encode(map[string]any{"episodes": []any{map[string]any{"id": 1}}})
		case "/v1/timeline":
			_ = json.NewEncoder(w).Encode(map[string]any{"total_episodes": 3})
		case "/v1/orient":
			_ = json.NewEncoder(w).Encode(map[string]any{"channels": map[string]any{"synthetic.chat": "10"}})
		case "/v1/describe":
			_ = json.NewEncoder(w).Encode(map[string]any{"channels": []string{"synthetic.chat"}})
		default:
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"detail":"not found"}`))
		}
	}))
	defer srv.Close()

	s, err := tl.New(tl.Config{URL: srv.URL, Token: "secret-tok", Asker: "guest"})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()

	// Bounded CollectAs uses /v1/sql
	msgs, err := tl.CollectAs[msg](ctx, s.Table("messages").Select("id", "channel", "content").Limit(5))
	if err != nil || len(msgs) != 1 || msgs[0].Content != "hello" {
		t.Fatalf("CollectAs failed: %v, %#v", err, msgs)
	}
	if gotAsker != "guest" || gotClient != "go-client" {
		t.Fatalf("headers mismatch: asker=%q client=%q", gotAsker, gotClient)
	}

	// Unbounded CollectAs uses /v1/sql/stream NDJSON
	all, err := tl.CollectAs[msg](ctx, s.Table("messages").Select("id", "channel", "content"))
	if err != nil || len(all) != 2 || all[1].Content != "world" {
		t.Fatalf("streaming CollectAs failed: %v, %#v", err, all)
	}

	// CSV stream
	rc, err := s.Table("messages").ToCSV(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	csvBytes, _ := io.ReadAll(rc)
	_ = rc.Close()
	if !strings.Contains(string(csvBytes), "2,synthetic.chat,world") {
		t.Fatalf("unexpected csv: %s", string(csvBytes))
	}

	// MCP parity endpoints
	if res, err := s.Find(ctx, tl.FindRequest{Query: "orchard", K: 5}); err != nil || res["episodes"] == nil {
		t.Fatalf("Find failed: %v, %#v", err, res)
	}
	if res, err := s.Timeline(ctx, tl.TimelineRequest{Queries: []string{"orchard"}}); err != nil || res["total_episodes"] != float64(3) {
		t.Fatalf("Timeline failed: %v, %#v", err, res)
	}
	if res, err := s.Orient(ctx, tl.OrientRequest{Days: 7}); err != nil || res["channels"] == nil {
		t.Fatalf("Orient failed: %v, %#v", err, res)
	}
	if res, err := s.Describe(ctx, ""); err != nil || res == nil {
		t.Fatalf("Describe failed: %v", err)
	}
	if rows, err := s.Person(ctx, "Zibble", 5); err != nil || len(rows) != 1 {
		t.Fatalf("Person failed: %v, %#v", err, rows)
	}
}
