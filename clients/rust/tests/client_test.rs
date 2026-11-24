use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::thread;

use serde::Deserialize;
use serde_json::json;
use throughline_client::{
    col, count, date_trunc, FindRequest, JoinType, OrientRequest, Session, TimelineRequest,
};

#[test]
fn dataframe_compiles_parameterized_sql() {
    let s = Session::builder()
        .url("http://127.0.0.1:1")
        .token("tok")
        .build()
        .unwrap();

    let df = s
        .channel("health.oura.sleep")
        .unwrap()
        .select(&["happened_at", "score"])
        .filter(col("happened_at").gte("2026-01-01").and(col("score").gt(80)))
        .filter(col("note").contains("deep"))
        .order_by(vec![col("happened_at").desc()])
        .limit(25);

    let (sql, params) = df.to_sql().unwrap();
    assert_eq!(
        sql,
        r#"SELECT "happened_at", "score" FROM api.ch_health_oura_sleep WHERE (("happened_at" >= $1) AND ("score" > $2)) AND ("note" ILIKE $3) ORDER BY "happened_at" DESC LIMIT 25"#
    );
    assert_eq!(params, vec![json!("2026-01-01"), json!(80), json!("%deep%")]);
}

#[test]
fn group_by_and_join_renumber_params() {
    let s = Session::builder()
        .url("http://127.0.0.1:1")
        .token("tok")
        .build()
        .unwrap();

    let agg = s
        .table("messages")
        .unwrap()
        .filter(col("channel").is_in(["chat.sms", "email.gmail"]))
        .group_by_expr(vec![
            date_trunc("day", "happened_at").alias("day"),
            col("channel"),
        ])
        .agg(vec![count("*").alias("n")]);

    let (sql, params) = agg.to_sql().unwrap();
    assert!(sql.contains("GROUP BY 1, 2"), "got: {sql}");
    assert_eq!(params, vec![json!("day"), json!("chat.sms"), json!("email.gmail")]);

    let left = s
        .table("messages")
        .unwrap()
        .filter(col("channel").eq("chat.sms"))
        .alias("m")
        .unwrap();
    let right = s
        .table("threads")
        .unwrap()
        .filter(col("title").contains("trip"))
        .alias("t")
        .unwrap();
    let joined = left
        .join(&right, "thread_id", "id", JoinType::Inner)
        .select(&["m.id", "t.title"]);

    let (jsql, jparams) = joined.to_sql().unwrap();
    assert!(jsql.contains("INNER JOIN"), "got: {jsql}");
    assert!(jsql.contains("$1") && jsql.contains("$2"), "got: {jsql}");
    assert_eq!(jparams, vec![json!("chat.sms"), json!("%trip%")]);
}

fn start_mock_server(expected_requests: usize) -> (String, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let handle = thread::spawn(move || {
        for _ in 0..expected_requests {
            let (mut stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut request_line = String::new();
            reader.read_line(&mut request_line).unwrap();
            let path = request_line
                .split_whitespace()
                .nth(1)
                .unwrap_or("/")
                .to_string();

            let mut content_len = 0usize;
            let mut has_auth = false;
            let mut has_asker = false;
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" || line.is_empty() {
                    break;
                }
                let lower = line.to_ascii_lowercase();
                if lower.starts_with("content-length:") {
                    content_len = line.split(':').nth(1).unwrap().trim().parse().unwrap_or(0);
                } else if lower.starts_with("authorization:") && line.contains("Bearer secret-tok") {
                    has_auth = true;
                } else if lower.starts_with("x-throughline-asker:") && line.contains("guest") {
                    has_asker = true;
                }
            }
            let mut body_bytes = vec![0u8; content_len];
            if content_len > 0 {
                reader.read_exact(&mut body_bytes).unwrap();
            }
            assert!(has_auth, "missing Authorization header on {path}");
            assert!(has_asker, "missing X-Throughline-Asker header on {path}");

            let (ctype, resp_body) = match path.as_str() {
                "/v1/sql" => (
                    "application/json",
                    r#"{"rows":[{"id":1,"channel":"synthetic.chat","content":"hello"}],"row_count":1,"truncated":false}"#,
                ),
                "/v1/sql/stream" => {
                    let body_str = String::from_utf8_lossy(&body_bytes);
                    if body_str.contains(r#""format":"csv""#) {
                        (
                            "text/csv; charset=utf-8",
                            "id,channel,content\n1,synthetic.chat,hello\n2,synthetic.chat,world\n",
                        )
                    } else {
                        (
                            "application/x-ndjson",
                            "{\"id\":1,\"channel\":\"synthetic.chat\",\"content\":\"hello\"}\n{\"id\":2,\"channel\":\"synthetic.chat\",\"content\":\"world\"}\n",
                        )
                    }
                }
                "/v1/find" => ("application/json", r#"{"episodes":[{"id":1}]}"#),
                "/v1/timeline" => ("application/json", r#"{"total_episodes":3}"#),
                "/v1/orient" => ("application/json", r#"{"channels":{"synthetic.chat":"10"}}"#),
                "/v1/describe" => ("application/json", r#"{"channels":["synthetic.chat"]}"#),
                _ => ("application/json", r#"{"detail":"not found"}"#),
            };
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{resp_body}",
                resp_body.len()
            );
            stream.write_all(response.as_bytes()).unwrap();
        }
    });
    (format!("http://{addr}"), handle)
}

#[test]
fn session_http_streaming_and_mcp_parity() {
    #[derive(Debug, Deserialize)]
    struct Msg {
        id: i64,
        channel: String,
        content: String,
    }

    let (base_url, handle) = start_mock_server(8);
    let s = Session::builder()
        .url(base_url)
        .token("secret-tok")
        .asker("guest")
        .build()
        .unwrap();

    // 1. Bounded collect_as -> /v1/sql
    let msgs: Vec<Msg> = s
        .table("messages")
        .unwrap()
        .select(&["id", "channel", "content"])
        .limit(5)
        .collect_as()
        .unwrap();
    assert_eq!(msgs.len(), 1);
    assert_eq!(msgs[0].id, 1);
    assert_eq!(msgs[0].channel, "synthetic.chat");
    assert_eq!(msgs[0].content, "hello");

    // 2. Unbounded collect_as -> /v1/sql/stream NDJSON
    let all: Vec<Msg> = s
        .table("messages")
        .unwrap()
        .select(&["id", "channel", "content"])
        .collect_as()
        .unwrap();
    assert_eq!(all.len(), 2);
    assert_eq!(all[1].content, "world");

    // 3. CSV stream
    let csv = s.table("messages").unwrap().to_csv(None).unwrap();
    assert!(csv.contains("2,synthetic.chat,world"), "got: {csv}");

    // 4. MCP parity endpoints
    let find_res = s
        .find(&FindRequest {
            query: "orchard".into(),
            k: Some(5),
            ..Default::default()
        })
        .unwrap();
    assert!(find_res.get("episodes").is_some());

    let tl_res = s
        .timeline(&TimelineRequest {
            queries: Some(vec!["orchard".into()]),
            ..Default::default()
        })
        .unwrap();
    assert_eq!(tl_res["total_episodes"], json!(3));

    let orient_res = s
        .orient(&OrientRequest {
            days: Some(7),
            ..Default::default()
        })
        .unwrap();
    assert!(orient_res.get("channels").is_some());

    let desc_res = s.describe(None).unwrap();
    assert!(desc_res.get("channels").is_some());

    let people = s.person("Zibble", 5).unwrap();
    assert_eq!(people.len(), 1);

    handle.join().unwrap();
}
