// Package throughline provides a zero-dependency, Snowpark-style Go client for Throughline.
package throughline

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// Config configures a Throughline Session.
type Config struct {
	URL        string
	Token      string
	Asker      string
	ClientName string
	Timeout    time.Duration
	Retries    int
	HTTPClient *http.Client
}

// APIError represents a non-2xx HTTP response from Throughline.
type APIError struct {
	StatusCode int
	Detail     string
	Code       string
}

func (e *APIError) Error() string {
	if e.Code != "" {
		return fmt.Sprintf("throughline HTTP %d: %s (SQLSTATE %s)", e.StatusCode, e.Detail, e.Code)
	}
	return fmt.Sprintf("throughline HTTP %d: %s", e.StatusCode, e.Detail)
}

// Row is an untyped SQL result row.
type Row map[string]any

// SQLResult is the response envelope from POST /v1/sql.
type SQLResult struct {
	Rows      []Row          `json:"rows"`
	RowCount  int            `json:"row_count"`
	Truncated bool           `json:"truncated"`
	Execution map[string]any `json:"execution,omitempty"`
}

// SQLOptions configures POST /v1/sql requests.
type SQLOptions struct {
	Raw         *bool
	RowCap      int
	DeadlineMS  int
	StrictExact bool
	DryRun      bool
	Trace       bool
	Budgets     map[string]any
	ApprovalID  string
}

// StreamOptions configures POST /v1/sql/stream requests.
type StreamOptions struct {
	RowCap      int
	DeadlineMS  int
	StrictExact bool
	Budgets     map[string]any
	ApprovalID  string
}

// Session is a programmatic Throughline client with Snowpark-style lazy DataFrames
// and full parity with Throughline's MCP and HTTP retrieval endpoints.
type Session struct {
	baseURL    string
	token      string
	asker      string
	clientName string
	retries    int
	http       *http.Client
}

// New creates a Session from explicit configuration.
func New(cfg Config) (*Session, error) {
	if strings.TrimSpace(cfg.URL) == "" {
		return nil, errors.New("throughline: URL is required")
	}
	if strings.TrimSpace(cfg.Token) == "" {
		return nil, errors.New("throughline: Token is required")
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = 60 * time.Second
	}
	httpClient := cfg.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: timeout}
	}
	retries := cfg.Retries
	if retries < 0 {
		retries = 0
	}
	clientName := cfg.ClientName
	if clientName == "" {
		clientName = "go-client"
	}
	return &Session{
		baseURL:    strings.TrimRight(cfg.URL, "/") + "/v1/",
		token:      cfg.Token,
		asker:      cfg.Asker,
		clientName: clientName,
		retries:    retries,
		http:       httpClient,
	}, nil
}

// FromEnv creates a Session using THROUGHLINE_URL, THROUGHLINE_TOKEN, and optional THROUGHLINE_ASKER.
func FromEnv() (*Session, error) {
	return New(Config{
		URL:     os.Getenv("THROUGHLINE_URL"),
		Token:   os.Getenv("THROUGHLINE_TOKEN"),
		Asker:   os.Getenv("THROUGHLINE_ASKER"),
		Retries: 3,
	})
}

func (s *Session) endpointURL(path string, query url.Values) string {
	clean := strings.TrimLeft(path, "/")
	clean = strings.TrimPrefix(clean, "v1/")
	u := s.baseURL + clean
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	return u
}

func (s *Session) applyHeaders(req *http.Request, hasBody bool) {
	req.Header.Set("Authorization", "Bearer "+s.token)
	req.Header.Set("X-Throughline-Client", s.clientName)
	if s.asker != "" {
		req.Header.Set("X-Throughline-Asker", s.asker)
	}
	if hasBody {
		req.Header.Set("Content-Type", "application/json")
	}
}

func parseAPIError(resp *http.Response) error {
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	apiErr := &APIError{
		StatusCode: resp.StatusCode,
		Detail:     strings.TrimSpace(string(body)),
	}
	var parsed struct {
		Detail any    `json:"detail"`
		Code   string `json:"code"`
	}
	if json.Unmarshal(body, &parsed) == nil && parsed.Detail != nil {
		apiErr.Detail = fmt.Sprint(parsed.Detail)
		apiErr.Code = parsed.Code
	}
	if apiErr.Detail == "" {
		apiErr.Detail = http.StatusText(resp.StatusCode)
	}
	return apiErr
}

// Request sends an authenticated HTTP request to any /v1/* route and decodes JSON into out.
func (s *Session) Request(ctx context.Context, method, path string, query url.Values, body any, out any, retry bool) error {
	var rawBody []byte
	if body != nil {
		var err error
		rawBody, err = json.Marshal(body)
		if err != nil {
			return err
		}
	}
	attempts := 1
	if retry {
		attempts = s.retries + 1
	}
	target := s.endpointURL(path, query)
	var lastErr error
	for attempt := 0; attempt < attempts; attempt++ {
		var reader io.Reader
		if rawBody != nil {
			reader = bytes.NewReader(rawBody)
		}
		req, err := http.NewRequestWithContext(ctx, method, target, reader)
		if err != nil {
			return err
		}
		s.applyHeaders(req, rawBody != nil)
		resp, err := s.http.Do(req)
		if err != nil {
			lastErr = err
			if attempt+1 < attempts && ctx.Err() == nil {
				sleepBackoff(ctx, attempt)
				continue
			}
			return err
		}
		if (resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500) && attempt+1 < attempts {
			_ = resp.Body.Close()
			sleepBackoff(ctx, attempt)
			continue
		}
		if resp.StatusCode >= 400 {
			apiErr := parseAPIError(resp)
			_ = resp.Body.Close()
			return apiErr
		}
		defer resp.Body.Close()
		if out == nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			return nil
		}
		if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
			return err
		}
		return nil
	}
	return lastErr
}

func sleepBackoff(ctx context.Context, attempt int) {
	delay := time.Duration(100*(1<<attempt)) * time.Millisecond
	if delay > 4*time.Second {
		delay = 4 * time.Second
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}

// SQL executes a validated read-only SELECT with optional $1..$N positional parameters.
// Raw defaults to true so programmatic callers receive untruncated values.
func (s *Session) SQL(ctx context.Context, statement string, params []any, opts *SQLOptions) (*SQLResult, error) {
	rawFlag := true
	rowCap := 1000
	body := map[string]any{
		"statement": statement,
	}
	if len(params) > 0 {
		body["params"] = params
	}
	if opts != nil {
		if opts.Raw != nil {
			rawFlag = *opts.Raw
		}
		if opts.RowCap > 0 {
			rowCap = opts.RowCap
		}
		if opts.DeadlineMS > 0 {
			body["deadline_ms"] = opts.DeadlineMS
		}
		if opts.StrictExact {
			body["strict_exact"] = true
		}
		if opts.DryRun {
			body["dry_run"] = true
		}
		if opts.Trace {
			body["trace"] = true
		}
		if opts.Budgets != nil {
			body["budgets"] = opts.Budgets
		}
		if opts.ApprovalID != "" {
			body["approval_id"] = opts.ApprovalID
		}
	}
	body["raw"] = rawFlag
	body["row_cap"] = rowCap
	var res SQLResult
	if err := s.Request(ctx, http.MethodPost, "sql", nil, body, &res, true); err != nil {
		return nil, err
	}
	return &res, nil
}

// QueryAs executes a parameterized SQL query and decodes rows into a typed slice []T.
func QueryAs[T any](ctx context.Context, s *Session, statement string, params ...any) ([]T, error) {
	body := map[string]any{
		"statement": statement,
		"raw":       true,
		"row_cap":   10000,
	}
	if len(params) > 0 {
		body["params"] = params
	}
	var envelope struct {
		Rows []T `json:"rows"`
	}
	if err := s.Request(ctx, http.MethodPost, "sql", nil, body, &envelope, true); err != nil {
		return nil, err
	}
	return envelope.Rows, nil
}

// RowStream lazily decodes NDJSON rows from POST /v1/sql/stream.
type RowStream[T any] struct {
	body    io.ReadCloser
	scanner *bufio.Scanner
	current T
	err     error
}

// Next advances the stream to the next row.
func (rs *RowStream[T]) Next() bool {
	if rs.err != nil {
		return false
	}
	for rs.scanner.Scan() {
		line := bytes.TrimSpace(rs.scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		var item T
		if err := json.Unmarshal(line, &item); err != nil {
			rs.err = err
			return false
		}
		rs.current = item
		return true
	}
	if err := rs.scanner.Err(); err != nil {
		rs.err = err
	}
	return false
}

// Row returns the most recently scanned row.
func (rs *RowStream[T]) Row() T {
	return rs.current
}

// Err returns the first error encountered during streaming.
func (rs *RowStream[T]) Err() error {
	return rs.err
}

// Close closes the underlying HTTP response body.
func (rs *RowStream[T]) Close() error {
	if rs.body == nil {
		return nil
	}
	return rs.body.Close()
}

// All drains the remaining stream into a slice and closes the stream.
func (rs *RowStream[T]) All() ([]T, error) {
	defer rs.Close()
	var items []T
	for rs.Next() {
		items = append(items, rs.Row())
	}
	if err := rs.Err(); err != nil {
		return nil, err
	}
	return items, nil
}

func (s *Session) openStream(ctx context.Context, statement string, params []any, format string, opts *StreamOptions) (io.ReadCloser, error) {
	body := map[string]any{
		"statement": statement,
		"format":    format,
		"row_cap":   100000,
	}
	if len(params) > 0 {
		body["params"] = params
	}
	if opts != nil {
		if opts.RowCap > 0 {
			body["row_cap"] = opts.RowCap
		}
		if opts.DeadlineMS > 0 {
			body["deadline_ms"] = opts.DeadlineMS
		}
		if opts.StrictExact {
			body["strict_exact"] = true
		}
		if opts.Budgets != nil {
			body["budgets"] = opts.Budgets
		}
		if opts.ApprovalID != "" {
			body["approval_id"] = opts.ApprovalID
		}
	}
	rawBody, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.endpointURL("sql/stream", nil), bytes.NewReader(rawBody))
	if err != nil {
		return nil, err
	}
	s.applyHeaders(req, true)
	resp, err := s.http.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		apiErr := parseAPIError(resp)
		_ = resp.Body.Close()
		return nil, apiErr
	}
	return resp.Body, nil
}

// StreamSQL opens a lazy NDJSON stream of untyped rows via POST /v1/sql/stream.
func (s *Session) StreamSQL(ctx context.Context, statement string, params []any, opts *StreamOptions) (*RowStream[Row], error) {
	return StreamAs[Row](ctx, s, statement, params, opts)
}

// StreamAs opens a lazy NDJSON stream decoded directly into typed structs T.
func StreamAs[T any](ctx context.Context, s *Session, statement string, params []any, opts *StreamOptions) (*RowStream[T], error) {
	rc, err := s.openStream(ctx, statement, params, "ndjson", opts)
	if err != nil {
		return nil, err
	}
	sc := bufio.NewScanner(rc)
	// Allow rows up to 8 MiB (matches server MAX_ROW_TEXT).
	sc.Buffer(make([]byte, 64*1024), 8*1024*1024)
	return &RowStream[T]{body: rc, scanner: sc}, nil
}

// StreamCSV opens an RFC 4180 CSV stream via POST /v1/sql/stream. Caller must close it.
func (s *Session) StreamCSV(ctx context.Context, statement string, params []any, opts *StreamOptions) (io.ReadCloser, error) {
	return s.openStream(ctx, statement, params, "csv", opts)
}
