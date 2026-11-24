"""GET /v1/recipes and /v1/recipes/{name}: tested recipes (INT-9).

Both read api.recipes through the audited reader path, like every other read.
Recipes hold no personal data, so every asker sees the same list; a proposed
or retired recipe is never listed.
"""

from fastapi import APIRouter, HTTPException
from psycopg import sql

from ..deps import Token, audited

router = APIRouter()

_COLUMNS = "name,version,schema_version,status,summary,needs,statements,expect"


@router.get("/v1/recipes")
def recipes_list(token: Token):
    """Every tested recipe: name, version, summary, needs, statements, expect."""
    return audited(f"SELECT {_COLUMNS} FROM api.recipes ORDER BY name", token)["rows"]


@router.get("/v1/recipes/{name}")
def recipe_get(name: str, token: Token):
    """One tested recipe with its body (adaptation notes, troubleshooting)."""
    statement = (
        sql.SQL("SELECT " + _COLUMNS + ",body FROM api.recipes WHERE name={}")
        .format(sql.Literal(name))
        .as_string()
    )
    rows = audited(statement, token)["rows"]
    if not rows:
        raise HTTPException(404, "no tested recipe with that name")
    return rows[0]
