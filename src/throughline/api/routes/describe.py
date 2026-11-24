"""GET /v1/describe: what the archive holds and how to query it."""

from fastapi import APIRouter
from psycopg import sql

from ..deps import Token, audited

router = APIRouter()


@router.get("/v1/describe")
def describe(token: Token, subject: str | None = None):
    """Index (no subject), `functions`, `function:<name>`, `channel:<name>`,
    a computed subject, or a named section. Unknown subjects return null.

    Runs api.describe through the audited reader path, so the answer is
    scoped to the asker: a hidden channel reads exactly like an unknown one.
    """
    statement = (
        sql.SQL("SELECT api.describe({}) AS describe")
        .format(sql.Literal(subject))
        .as_string()
    )
    return audited(statement, token)["rows"][0]["describe"]
