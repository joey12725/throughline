use std::io::{BufRead, BufReader, Read};
use std::marker::PhantomData;
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::dataframe::DataFrame;
use crate::error::{Error, Result};

/// Untyped JSON object row.
pub type Row = Map<String, Value>;

/// Envelope returned by `POST /v1/sql`.
#[derive(Debug, Clone, Deserialize)]
pub struct SqlResult {
    pub rows: Vec<Row>,
    pub row_count: usize,
    pub truncated: bool,
    pub execution: Option<Value>,
}

/// Options for `POST /v1/sql`.
#[derive(Debug, Clone)]
pub struct SqlOptions {
    pub raw: bool,
    pub row_cap: usize,
    pub deadline_ms: Option<u64>,
    pub strict_exact: bool,
    pub dry_run: bool,
    pub trace: bool,
}

impl Default for SqlOptions {
    fn default() -> Self {
        Self {
            raw: true,
            row_cap: 1000,
            deadline_ms: None,
            strict_exact: false,
            dry_run: false,
            trace: false,
        }
    }
}

/// Options for `POST /v1/sql/stream`.
#[derive(Debug, Clone)]
pub struct StreamOptions {
    pub row_cap: usize,
    pub deadline_ms: Option<u64>,
    pub strict_exact: bool,
}

impl Default for StreamOptions {
    fn default() -> Self {
        Self {
            row_cap: 100_000,
            deadline_ms: None,
            strict_exact: false,
        }
    }
}

/// Request body for `POST /v1/find` (matches MCP `find` tool).
#[derive(Debug, Clone, Default, Serialize)]
pub struct FindRequest {
    pub query: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub k: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sort: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub window: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub channels: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub persons: Option<Vec<i64>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub after: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub before: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_chars: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub strategy: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ranker: Option<String>,
}

/// Request body for `POST /v1/timeline` (matches MCP `timeline` tool).
#[derive(Debug, Clone, Default, Serialize)]
pub struct TimelineRequest {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub query: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub queries: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub semantic: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_episodes: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub order: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sample: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub channels: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub persons: Option<Vec<i64>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub after: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub before: Option<String>,
}

/// Request body for `POST /v1/orient` (matches MCP `orient` tool).
#[derive(Debug, Clone, Default, Serialize)]
pub struct OrientRequest {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub days: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub people: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub as_of: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_chars: Option<usize>,
}

/// Request body for `POST /v1/recall`.
#[derive(Debug, Clone, Default, Serialize)]
pub struct RecallRequest {
    pub query: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub k: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scope: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub strategy: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ranker: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub view: Option<String>,
}

/// Lazy iterator over NDJSON rows from `POST /v1/sql/stream`.
pub struct RowStream<T> {
    reader: BufReader<Box<dyn Read + Send + Sync>>,
    buf: String,
    _marker: PhantomData<T>,
}

impl<T: DeserializeOwned> Iterator for RowStream<T> {
    type Item = Result<T>;

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            self.buf.clear();
            match self.reader.read_line(&mut self.buf) {
                Ok(0) => return None,
                Ok(_) => {
                    let trimmed = self.buf.trim();
                    if trimmed.is_empty() {
                        continue;
                    }
                    return Some(serde_json::from_str::<T>(trimmed).map_err(Error::from));
                }
                Err(err) => return Some(Err(Error::from(err))),
            }
        }
    }
}

/// Builder for [`Session`].
#[derive(Debug, Clone)]
pub struct SessionBuilder {
    url: Option<String>,
    token: Option<String>,
    asker: Option<String>,
    client_name: String,
    timeout: Duration,
    retries: usize,
}

impl Default for SessionBuilder {
    fn default() -> Self {
        Self {
            url: None,
            token: None,
            asker: None,
            client_name: "rust-client".to_string(),
            timeout: Duration::from_secs(60),
            retries: 3,
        }
    }
}

impl SessionBuilder {
    pub fn url(mut self, url: impl Into<String>) -> Self {
        self.url = Some(url.into());
        self
    }

    pub fn token(mut self, token: impl Into<String>) -> Self {
        self.token = Some(token.into());
        self
    }

    pub fn asker(mut self, asker: impl Into<String>) -> Self {
        self.asker = Some(asker.into());
        self
    }

    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    pub fn retries(mut self, retries: usize) -> Self {
        self.retries = retries;
        self
    }

    pub fn build(self) -> Result<Session> {
        let url = self
            .url
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| Error::Config("URL is required".to_string()))?;
        let token = self
            .token
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| Error::Config("token is required".to_string()))?;
        let agent = ureq::AgentBuilder::new().timeout(self.timeout).build();
        Ok(Session {
            base_url: format!("{}/v1/", url.trim_end_matches('/')),
            token,
            asker: self.asker.filter(|s| !s.trim().is_empty()),
            client_name: self.client_name,
            retries: self.retries,
            agent,
        })
    }
}

/// Programmatic Throughline client (Snowpark-style DataFrames + full MCP parity).
#[derive(Clone)]
pub struct Session {
    base_url: String,
    token: String,
    asker: Option<String>,
    client_name: String,
    retries: usize,
    agent: ureq::Agent,
}

fn valid_relation(name: &str) -> bool {
    let mut chars = name.chars();
    match chars.next() {
        Some(c) if c.is_ascii_lowercase() => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

fn valid_channel(name: &str) -> bool {
    !name.is_empty() && name.split('.').all(valid_relation)
}

impl Session {
    pub fn builder() -> SessionBuilder {
        SessionBuilder::default()
    }

    /// Connect using `THROUGHLINE_URL`, `THROUGHLINE_TOKEN`, and optional `THROUGHLINE_ASKER`.
    pub fn from_env() -> Result<Self> {
        let mut b = Session::builder();
        if let Ok(u) = std::env::var("THROUGHLINE_URL") {
            b = b.url(u);
        }
        if let Ok(t) = std::env::var("THROUGHLINE_TOKEN") {
            b = b.token(t);
        }
        if let Ok(a) = std::env::var("THROUGHLINE_ASKER") {
            if !a.is_empty() {
                b = b.asker(a);
            }
        }
        b.build()
    }

    fn endpoint(&self, path: &str) -> String {
        let clean = path.trim_start_matches('/');
        let clean = clean.strip_prefix("v1/").unwrap_or(clean);
        format!("{}{}", self.base_url, clean)
    }

    fn apply_headers(&self, mut req: ureq::Request) -> ureq::Request {
        req = req
            .set("Authorization", &format!("Bearer {}", self.token))
            .set("X-Throughline-Client", &self.client_name);
        if let Some(ref asker) = self.asker {
            req = req.set("X-Throughline-Asker", asker);
        }
        req
    }

    fn parse_http_error(status: u16, resp: ureq::Response) -> Error {
        let text = resp.into_string().unwrap_or_default();
        if let Ok(val) = serde_json::from_str::<Value>(&text) {
            if let Some(obj) = val.as_object() {
                let detail = obj
                    .get("detail")
                    .map(|d| match d {
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    })
                    .unwrap_or_else(|| text.clone());
                let code = obj
                    .get("code")
                    .and_then(|c| c.as_str())
                    .map(ToString::to_string);
                return Error::Api {
                    status,
                    detail,
                    code,
                };
            }
        }
        Error::Api {
            status,
            detail: if text.is_empty() {
                format!("HTTP {status}")
            } else {
                text
            },
            code: None,
        }
    }

    /// Low-level escape hatch to call any `/v1/*` HTTP route.
    pub fn request<T: DeserializeOwned>(
        &self,
        method: &str,
        path: &str,
        query: &[(&str, String)],
        body: Option<&Value>,
        retry: bool,
    ) -> Result<T> {
        let attempts = if retry { self.retries + 1 } else { 1 };
        let url = self.endpoint(path);
        for attempt in 0..attempts {
            let mut req = self.apply_headers(self.agent.request(method, &url));
            for (k, v) in query {
                req = req.query(k, v);
            }
            let outcome = match body {
                Some(val) => req.send_json(val.clone()),
                None => req.call(),
            };
            match outcome {
                Ok(resp) => {
                    let text = resp.into_string()?;
                    return Ok(serde_json::from_str::<T>(&text)?);
                }
                Err(ureq::Error::Status(status, resp)) => {
                    if (status == 429 || status >= 500) && attempt + 1 < attempts {
                        let ms = (100u64 << attempt).min(4000);
                        std::thread::sleep(Duration::from_millis(ms));
                        continue;
                    }
                    return Err(Self::parse_http_error(status, resp));
                }
                Err(ureq::Error::Transport(t)) => {
                    if attempt + 1 < attempts {
                        let ms = (100u64 << attempt).min(4000);
                        std::thread::sleep(Duration::from_millis(ms));
                        continue;
                    }
                    return Err(Error::Transport(t.to_string()));
                }
            }
        }
        Err(Error::Transport("request exhausted retries".to_string()))
    }

    // --- Snowpark-style DataFrame entry points ---

    /// Open a lazy DataFrame over an `api.<name>` view (e.g. `"messages"`, `"threads"`, `"people"`).
    pub fn table(&self, name: &str) -> Result<DataFrame> {
        let clean = name.strip_prefix("api.").unwrap_or(name);
        if !valid_relation(clean) {
            return Err(Error::Query(format!("invalid api view name: {name:?}")));
        }
        Ok(DataFrame::new(
            self.clone(),
            format!("api.{clean}"),
            vec![],
            None,
        ))
    }

    /// Open a lazy DataFrame over a channel's typed view (`api.ch_<channel>`).
    pub fn channel(&self, name: &str) -> Result<DataFrame> {
        if !valid_channel(name) {
            return Err(Error::Query(format!("invalid channel name: {name:?}")));
        }
        Ok(DataFrame::new(
            self.clone(),
            format!("api.ch_{}", name.replace('.', "_")),
            vec![],
            None,
        ))
    }

    /// Open a lazy DataFrame over a lens view (`api.lens_<name>`).
    pub fn lens(&self, name: &str) -> Result<DataFrame> {
        if !valid_relation(name) {
            return Err(Error::Query(format!("invalid lens name: {name:?}")));
        }
        Ok(DataFrame::new(
            self.clone(),
            format!("api.lens_{name}"),
            vec![],
            None,
        ))
    }

    /// Wrap any read-only `SELECT` as a composable lazy DataFrame.
    pub fn from_sql(&self, statement: &str, params: Vec<Value>) -> DataFrame {
        let trimmed = statement.trim().trim_end_matches(';');
        DataFrame::new(
            self.clone(),
            format!("({trimmed})"),
            params,
            Some("_q".to_string()),
        )
    }

    // --- Parameterized & Streaming SQL ---

    /// Run a validated read-only `SELECT` with optional `$1..$N` parameters (`raw: true` by default).
    pub fn sql(
        &self,
        statement: &str,
        params: &[Value],
        opts: Option<&SqlOptions>,
    ) -> Result<SqlResult> {
        let defaults = SqlOptions::default();
        let o = opts.unwrap_or(&defaults);
        let mut body = Map::new();
        body.insert("statement".into(), json!(statement));
        body.insert("raw".into(), json!(o.raw));
        body.insert("row_cap".into(), json!(o.row_cap));
        body.insert("strict_exact".into(), json!(o.strict_exact));
        body.insert("dry_run".into(), json!(o.dry_run));
        body.insert("trace".into(), json!(o.trace));
        if !params.is_empty() {
            body.insert("params".into(), Value::Array(params.to_vec()));
        }
        if let Some(ms) = o.deadline_ms {
            body.insert("deadline_ms".into(), json!(ms));
        }
        self.request("POST", "sql", &[], Some(&Value::Object(body)), true)
    }

    /// Execute a parameterized query and deserialize rows directly into `Vec<T>`.
    pub fn query_as<T: DeserializeOwned>(&self, statement: &str, params: &[Value]) -> Result<Vec<T>> {
        #[derive(Deserialize)]
        struct Envelope<U> {
            rows: Vec<U>,
        }
        let mut body = Map::new();
        body.insert("statement".into(), json!(statement));
        body.insert("raw".into(), json!(true));
        body.insert("row_cap".into(), json!(10_000));
        if !params.is_empty() {
            body.insert("params".into(), Value::Array(params.to_vec()));
        }
        let env: Envelope<T> =
            self.request("POST", "sql", &[], Some(&Value::Object(body)), true)?;
        Ok(env.rows)
    }

    fn open_stream(
        &self,
        statement: &str,
        params: &[Value],
        format: &str,
        opts: Option<&StreamOptions>,
    ) -> Result<Box<dyn Read + Send + Sync>> {
        let defaults = StreamOptions::default();
        let o = opts.unwrap_or(&defaults);
        let mut body = Map::new();
        body.insert("statement".into(), json!(statement));
        body.insert("format".into(), json!(format));
        body.insert("row_cap".into(), json!(o.row_cap));
        body.insert("strict_exact".into(), json!(o.strict_exact));
        if !params.is_empty() {
            body.insert("params".into(), Value::Array(params.to_vec()));
        }
        if let Some(ms) = o.deadline_ms {
            body.insert("deadline_ms".into(), json!(ms));
        }
        let req = self.apply_headers(self.agent.post(&self.endpoint("sql/stream")));
        match req.send_json(Value::Object(body)) {
            Ok(resp) => Ok(resp.into_reader()),
            Err(ureq::Error::Status(status, resp)) => Err(Self::parse_http_error(status, resp)),
            Err(ureq::Error::Transport(t)) => Err(Error::Transport(t.to_string())),
        }
    }

    /// Lazily stream untyped JSON rows via `POST /v1/sql/stream` (NDJSON).
    pub fn stream_sql(
        &self,
        statement: &str,
        params: &[Value],
        opts: Option<&StreamOptions>,
    ) -> Result<RowStream<Row>> {
        self.stream_as::<Row>(statement, params, opts)
    }

    /// Lazily stream typed structs `T` via `POST /v1/sql/stream` (NDJSON).
    pub fn stream_as<T: DeserializeOwned>(
        &self,
        statement: &str,
        params: &[Value],
        opts: Option<&StreamOptions>,
    ) -> Result<RowStream<T>> {
        let reader = self.open_stream(statement, params, "ndjson", opts)?;
        Ok(RowStream {
            reader: BufReader::new(reader),
            buf: String::new(),
            _marker: PhantomData,
        })
    }

    /// Open a streaming RFC 4180 CSV reader via `POST /v1/sql/stream`.
    pub fn stream_csv(
        &self,
        statement: &str,
        params: &[Value],
        opts: Option<&StreamOptions>,
    ) -> Result<Box<dyn Read + Send + Sync>> {
        self.open_stream(statement, params, "csv", opts)
    }

    // --- Full MCP parity + high-level retrieval ---

    pub fn find(&self, req: &FindRequest) -> Result<Value> {
        let body = serde_json::to_value(req)?;
        self.request("POST", "find", &[], Some(&body), true)
    }

    pub fn timeline(&self, req: &TimelineRequest) -> Result<Value> {
        let body = serde_json::to_value(req)?;
        self.request("POST", "timeline", &[], Some(&body), true)
    }

    pub fn orient(&self, req: &OrientRequest) -> Result<Value> {
        let body = serde_json::to_value(req)?;
        self.request("POST", "orient", &[], Some(&body), true)
    }

    pub fn recall(&self, req: &RecallRequest) -> Result<Value> {
        let mut body = serde_json::to_value(req)?;
        if let Some(obj) = body.as_object_mut() {
            obj.entry("view".to_string()).or_insert_with(|| json!("full"));
        }
        self.request("POST", "recall", &[], Some(&body), true)
    }

    pub fn describe(&self, subject: Option<&str>) -> Result<Value> {
        let mut q = Vec::new();
        if let Some(s) = subject {
            q.push(("subject", s.to_string()));
        }
        self.request("GET", "describe", &q, None, true)
    }

    pub fn context(&self, message_id: i64, before: usize, after: usize) -> Result<SqlResult> {
        let q = vec![
            ("message_id", message_id.to_string()),
            ("before", before.to_string()),
            ("after", after.to_string()),
        ];
        self.request("GET", "context", &q, None, true)
    }

    /// Resolve a name to matching person briefs (matching MCP's `person` tool).
    pub fn person(&self, name: &str, limit: usize) -> Result<Vec<Row>> {
        let res = self.sql(
            "SELECT p.id, b.* FROM api.person($1) p, LATERAL api.person_brief(p.id) b LIMIT $2",
            &[json!(name), json!(limit.max(1))],
            None,
        )?;
        Ok(res.rows)
    }

    pub fn person_brief(&self, person_id: i64) -> Result<Value> {
        self.request("GET", &format!("people/{person_id}/brief"), &[], None, true)
    }

    pub fn person_dms(
        &self,
        person_id: i64,
        after: Option<&str>,
        before: Option<&str>,
        limit: Option<usize>,
    ) -> Result<Value> {
        let mut q = Vec::new();
        if let Some(a) = after {
            q.push(("after", a.to_string()));
        }
        if let Some(b) = before {
            q.push(("before", b.to_string()));
        }
        if let Some(l) = limit {
            q.push(("max_results", l.to_string()));
        }
        self.request("GET", &format!("people/{person_id}/dms"), &q, None, true)
    }

    pub fn person_open_loops(&self, person_id: Option<i64>) -> Result<Value> {
        let path = match person_id {
            Some(id) => format!("people/{id}/open-loops"),
            None => "open-loops".to_string(),
        };
        self.request("GET", &path, &[], None, true)
    }

    pub fn day(
        &self,
        date: &str,
        tz: Option<&str>,
        channels: Option<&[&str]>,
        limit: Option<usize>,
    ) -> Result<Value> {
        let mut q = vec![("date", date.to_string())];
        if let Some(t) = tz {
            q.push(("tz", t.to_string()));
        }
        if let Some(ch) = channels {
            q.push(("channels", ch.join(",")));
        }
        if let Some(l) = limit {
            q.push(("max_results", l.to_string()));
        }
        self.request("GET", "day", &q, None, true)
    }

    pub fn channels(&self) -> Result<Vec<Row>> {
        self.request("GET", "channels", &[], None, true)
    }

    pub fn status(&self) -> Result<Value> {
        self.request("GET", "status", &[], None, true)
    }

    pub fn pages(
        &self,
        kind: Option<&str>,
        query: Option<&str>,
        limit: Option<usize>,
    ) -> Result<Vec<Row>> {
        let mut q = Vec::new();
        if let Some(k) = kind {
            q.push(("kind", k.to_string()));
        }
        if let Some(text) = query {
            q.push(("q", text.to_string()));
        }
        if let Some(l) = limit {
            q.push(("limit", l.to_string()));
        }
        self.request("GET", "pages", &q, None, true)
    }

    pub fn page(&self, slug: &str) -> Result<Value> {
        self.request(
            "GET",
            &format!("pages/{}", slug.trim_start_matches('/')),
            &[],
            None,
            true,
        )
    }

    pub fn feed(&self, name: impl Into<String>) -> Feed {
        Feed {
            session: self.clone(),
            name: name.into(),
        }
    }
}

/// Named arrival cursor handle (`/v1/feeds/{name}`).
#[derive(Clone)]
pub struct Feed {
    session: Session,
    pub name: String,
}

impl Feed {
    pub fn create(&self, scope: Option<Value>) -> Result<Value> {
        let mut body = Map::new();
        body.insert("name".into(), json!(self.name));
        if let Some(s) = scope {
            body.insert("scope".into(), s);
        }
        self.session
            .request("POST", "feeds", &[], Some(&Value::Object(body)), false)
    }

    pub fn news(&self, limit: Option<usize>) -> Result<Value> {
        let mut q = Vec::new();
        if let Some(l) = limit {
            q.push(("limit", l.to_string()));
        }
        self.session
            .request("GET", &format!("feeds/{}/news", self.name), &q, None, true)
    }

    pub fn pending(&self, limit: Option<usize>) -> Result<Value> {
        let mut q = Vec::new();
        if let Some(l) = limit {
            q.push(("limit", l.to_string()));
        }
        self.session.request(
            "GET",
            &format!("feeds/{}/pending", self.name),
            &q,
            None,
            true,
        )
    }

    pub fn mark(&self, ingested_at: &str, message_id: i64) -> Result<Value> {
        let body = json!({
            "seen_to_ingested_at": ingested_at,
            "seen_to_message_id": message_id,
        });
        self.session.request(
            "POST",
            &format!("feeds/{}/mark", self.name),
            &[],
            Some(&body),
            true,
        )
    }

    pub fn drop_feed(&self) -> Result<Value> {
        self.session
            .request("DELETE", &format!("feeds/{}", self.name), &[], None, false)
    }
}
