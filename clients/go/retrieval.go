package throughline

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

// FindRequest mirrors POST /v1/find (and the MCP `find` tool).
type FindRequest struct {
	Query            string   `json:"query"`
	K                int      `json:"k,omitempty"`
	Sort             string   `json:"sort,omitempty"`
	Window           *int     `json:"window,omitempty"`
	Channels         []string `json:"channels,omitempty"`
	Persons          []int64  `json:"persons,omitempty"`
	After            string   `json:"after,omitempty"`
	Before           string   `json:"before,omitempty"`
	MaxChars         int      `json:"max_chars,omitempty"`
	Strategy         string   `json:"strategy,omitempty"`
	Ranker           string   `json:"ranker,omitempty"`
	Pages            *int     `json:"pages,omitempty"`
	IncludeReference *bool    `json:"include_reference,omitempty"`
}

// TimelineRequest mirrors POST /v1/timeline (and the MCP `timeline` tool).
type TimelineRequest struct {
	Query       string   `json:"query,omitempty"`
	Queries     []string `json:"queries,omitempty"`
	Semantic    *bool    `json:"semantic,omitempty"`
	MaxEpisodes int      `json:"max_episodes,omitempty"`
	Order       string   `json:"order,omitempty"`
	Sample      string   `json:"sample,omitempty"`
	TextChars   int      `json:"text_chars,omitempty"`
	Channels    []string `json:"channels,omitempty"`
	Persons     []int64  `json:"persons,omitempty"`
	After       string   `json:"after,omitempty"`
	Before      string   `json:"before,omitempty"`
}

// OrientRequest mirrors POST /v1/orient (and the MCP `orient` tool).
type OrientRequest struct {
	Days     int      `json:"days,omitempty"`
	People   []string `json:"people,omitempty"`
	AsOf     string   `json:"as_of,omitempty"`
	MaxChars int      `json:"max_chars,omitempty"`
}

// RecallRequest mirrors POST /v1/recall.
type RecallRequest struct {
	Query    string         `json:"query"`
	K        int            `json:"k,omitempty"`
	Scope    map[string]any `json:"scope,omitempty"`
	Strategy string         `json:"strategy,omitempty"`
	Ranker   string         `json:"ranker,omitempty"`
	View     string         `json:"view,omitempty"`
}

// Find runs hybrid keyword + semantic search returning compact episodes.
func (s *Session) Find(ctx context.Context, req FindRequest) (map[string]any, error) {
	var out map[string]any
	if err := s.Request(ctx, http.MethodPost, "find", nil, req, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// Timeline runs exhaustive chronological episode search across synonyms.
func (s *Session) Timeline(ctx context.Context, req TimelineRequest) (map[string]any, error) {
	var out map[string]any
	if err := s.Request(ctx, http.MethodPost, "timeline", nil, req, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// Orient returns a working-memory snapshot across visible channels.
func (s *Session) Orient(ctx context.Context, req OrientRequest) (map[string]any, error) {
	var out map[string]any
	if err := s.Request(ctx, http.MethodPost, "orient", nil, req, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// Recall runs the lower-level programmatic recall pipeline.
func (s *Session) Recall(ctx context.Context, req RecallRequest) (map[string]any, error) {
	if req.View == "" {
		req.View = "full"
	}
	var out map[string]any
	if err := s.Request(ctx, http.MethodPost, "recall", nil, req, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// Describe returns the archive schema (channels, fields, SQL functions, recipes).
func (s *Session) Describe(ctx context.Context, subject string) (any, error) {
	q := url.Values{}
	if subject != "" {
		q.Set("subject", subject)
	}
	var out any
	if err := s.Request(ctx, http.MethodGet, "describe", q, nil, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// Context returns surrounding messages in a thread around messageID.
func (s *Session) Context(ctx context.Context, messageID int64, before, after int) (*SQLResult, error) {
	q := url.Values{
		"message_id": {strconv.FormatInt(messageID, 10)},
		"before":     {strconv.Itoa(before)},
		"after":      {strconv.Itoa(after)},
	}
	var out SQLResult
	if err := s.Request(ctx, http.MethodGet, "context", q, nil, &out, true); err != nil {
		return nil, err
	}
	return &out, nil
}

// Person resolves a name and returns matching person briefs (matching MCP's `person` tool).
func (s *Session) Person(ctx context.Context, name string, limit int) ([]Row, error) {
	if limit <= 0 {
		limit = 5
	}
	res, err := s.SQL(
		ctx,
		"SELECT p.id, b.* FROM api.person($1) p, LATERAL api.person_brief(p.id) b LIMIT $2",
		[]any{name, limit},
		nil,
	)
	if err != nil {
		return nil, err
	}
	return res.Rows, nil
}

// PersonBrief returns GET /v1/people/{id}/brief.
func (s *Session) PersonBrief(ctx context.Context, personID int64) (map[string]any, error) {
	var out map[string]any
	if err := s.Request(ctx, http.MethodGet, fmt.Sprintf("people/%d/brief", personID), nil, nil, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// PersonDMs returns GET /v1/people/{id}/dms.
func (s *Session) PersonDMs(ctx context.Context, personID int64, after, before string, limit int) (map[string]any, error) {
	q := url.Values{}
	if after != "" {
		q.Set("after", after)
	}
	if before != "" {
		q.Set("before", before)
	}
	if limit > 0 {
		q.Set("max_results", strconv.Itoa(limit))
	}
	var out map[string]any
	if err := s.Request(ctx, http.MethodGet, fmt.Sprintf("people/%d/dms", personID), q, nil, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// PersonOpenLoops returns open loops for a person (or all people when personID <= 0).
func (s *Session) PersonOpenLoops(ctx context.Context, personID int64) (map[string]any, error) {
	path := "open-loops"
	if personID > 0 {
		path = fmt.Sprintf("people/%d/open-loops", personID)
	}
	var out map[string]any
	if err := s.Request(ctx, http.MethodGet, path, nil, nil, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// Day returns everything that happened on a YYYY-MM-DD date.
func (s *Session) Day(ctx context.Context, date, tz string, channels []string, limit int) (map[string]any, error) {
	q := url.Values{"date": {date}}
	if tz != "" {
		q.Set("tz", tz)
	}
	if len(channels) > 0 {
		q.Set("channels", strings.Join(channels, ","))
	}
	if limit > 0 {
		q.Set("max_results", strconv.Itoa(limit))
	}
	var out map[string]any
	if err := s.Request(ctx, http.MethodGet, "day", q, nil, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// Channels lists registered channels (GET /v1/channels).
func (s *Session) Channels(ctx context.Context) ([]Row, error) {
	var out []Row
	if err := s.Request(ctx, http.MethodGet, "channels", nil, nil, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// Status returns system health, plugin staleness, and channel counts (GET /v1/status).
func (s *Session) Status(ctx context.Context) (map[string]any, error) {
	var out map[string]any
	if err := s.Request(ctx, http.MethodGet, "status", nil, nil, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// Pages lists or searches pages (GET /v1/pages).
func (s *Session) Pages(ctx context.Context, kind, query string, limit int) ([]Row, error) {
	q := url.Values{}
	if kind != "" {
		q.Set("kind", kind)
	}
	if query != "" {
		q.Set("q", query)
	}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	var out []Row
	if err := s.Request(ctx, http.MethodGet, "pages", q, nil, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// Page reads one page by slug (GET /v1/pages/{slug}).
func (s *Session) Page(ctx context.Context, slug string) (map[string]any, error) {
	var out map[string]any
	if err := s.Request(ctx, http.MethodGet, "pages/"+strings.TrimLeft(slug, "/"), nil, nil, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// Feed returns a handle to a named arrival cursor (/v1/feeds/{name}).
func (s *Session) Feed(name string) *Feed {
	return &Feed{session: s, Name: name}
}

// Feed wraps Throughline's named arrival cursor API for incremental pipelines.
type Feed struct {
	session *Session
	Name    string
}

func (f *Feed) Create(ctx context.Context, scope map[string]any) (map[string]any, error) {
	body := map[string]any{"name": f.Name}
	if scope != nil {
		body["scope"] = scope
	}
	var out map[string]any
	if err := f.session.Request(ctx, http.MethodPost, "feeds", nil, body, &out, false); err != nil {
		return nil, err
	}
	return out, nil
}

func (f *Feed) News(ctx context.Context, limit int) (map[string]any, error) {
	q := url.Values{}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	var out map[string]any
	if err := f.session.Request(ctx, http.MethodGet, "feeds/"+url.PathEscape(f.Name)+"/news", q, nil, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

func (f *Feed) Pending(ctx context.Context, limit int) (map[string]any, error) {
	q := url.Values{}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	var out map[string]any
	if err := f.session.Request(ctx, http.MethodGet, "feeds/"+url.PathEscape(f.Name)+"/pending", q, nil, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

func (f *Feed) Mark(ctx context.Context, ingestedAt string, messageID int64) (map[string]any, error) {
	body := map[string]any{
		"seen_to_ingested_at": ingestedAt,
		"seen_to_message_id":  messageID,
	}
	var out map[string]any
	if err := f.session.Request(ctx, http.MethodPost, "feeds/"+url.PathEscape(f.Name)+"/mark", nil, body, &out, true); err != nil {
		return nil, err
	}
	return out, nil
}

func (f *Feed) Drop(ctx context.Context) (map[string]any, error) {
	var out map[string]any
	if err := f.session.Request(ctx, http.MethodDelete, "feeds/"+url.PathEscape(f.Name), nil, nil, &out, false); err != nil {
		return nil, err
	}
	return out, nil
}
