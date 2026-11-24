use std::fmt;

/// Result alias for Throughline client operations.
pub type Result<T> = std::result::Result<T, Error>;

/// Errors returned by [`crate::Session`] and [`crate::DataFrame`].
#[derive(Debug)]
pub enum Error {
    /// Non-2xx response returned by the Throughline server.
    Api {
        status: u16,
        detail: String,
        code: Option<String>,
    },
    /// Transport or I/O error.
    Transport(String),
    /// JSON serialization/deserialization error.
    Json(serde_json::Error),
    /// Invalid configuration or missing environment variables.
    Config(String),
    /// Invalid SQL identifier, type, or query builder usage.
    Query(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Api {
                status,
                detail,
                code,
            } => match code {
                Some(c) => write!(f, "throughline HTTP {status}: {detail} (SQLSTATE {c})"),
                None => write!(f, "throughline HTTP {status}: {detail}"),
            },
            Error::Transport(msg) => write!(f, "throughline transport error: {msg}"),
            Error::Json(err) => write!(f, "throughline json error: {err}"),
            Error::Config(msg) => write!(f, "throughline config error: {msg}"),
            Error::Query(msg) => write!(f, "throughline query error: {msg}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<serde_json::Error> for Error {
    fn from(value: serde_json::Error) -> Self {
        Error::Json(value)
    }
}

impl From<std::io::Error> for Error {
    fn from(value: std::io::Error) -> Self {
        Error::Transport(value.to_string())
    }
}
