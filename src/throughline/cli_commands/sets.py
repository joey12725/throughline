"""`throughline lib set ...` and `throughline lib session show`: the caller's
own result sets, over HTTP with THROUGHLINE_TOKEN (the sets belong to that
token and its asker)."""

import json as json_module

import typer


def _object(text, name):
    if text is None:
        return None
    try:
        value = json_module.loads(text)
    except ValueError:
        raise typer.BadParameter(f"{name} must be JSON") from None
    return value


def register(group):
    sets = typer.Typer(no_args_is_help=True)
    group.add_typer(sets, name="set", short_help="Result sets (working state).")
    sessions = typer.Typer(no_args_is_help=True)
    group.add_typer(sessions, name="session", short_help="Exploration sessions.")

    @sets.command("show")
    def set_show(
        handle: str,
        limit: int | None = None,
        offset: int = 0,
        token_budget: int | None = None,
        json: bool = False,
    ):
        """Records of a result set, read live through the asker gate."""
        from ..cli import output, request

        params = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        if token_budget is not None:
            params["token_budget"] = token_budget
        result = request("GET", f"sets/{handle}", params=params)
        if json:
            output(result, True)
            return
        output(result["records"])
        output(result["info"])

    @sets.command("info")
    def set_info(handle: str, json: bool = False):
        """Completeness, counts, origin and lifetime of a result set."""
        from ..cli import output, request

        output(request("GET", f"sets/{handle}/info"), json)

    @sets.command("inspect")
    def set_inspect(
        handle: str,
        view: str = "summary",
        selector: str | None = typer.Option(None, help="JSON object or a value"),
        token_budget: int = 1000,
        json: bool = False,
    ):
        """Bounded view: summary, sample, by_time, by_channel, by_person,
        groups, missing_link, origin, history."""
        from ..cli import output, request

        chosen = selector
        if selector is not None and selector.lstrip().startswith("{"):
            chosen = _object(selector, "selector")
        output(
            request(
                "POST",
                f"sets/{handle}/inspect",
                data=dict(view=view, selector=chosen, token_budget=token_budget),
            ),
            json,
        )

    @sets.command("transform")
    def set_transform(
        op: str,
        handles: list[str],
        params: str | None = typer.Option(None, help="JSON object of parameters"),
        json: bool = False,
    ):
        """union, intersect, subtract, filter, order, head, sample, dedup,
        group or ungroup; prints the new set's info."""
        from ..cli import output, request

        body = dict(op=op, handles=handles, params=_object(params, "params") or {})
        output(request("POST", "sets/transform", data=body), json)

    @sets.command("drop")
    def set_drop(handle: str, json: bool = False):
        """Delete a result set now instead of at expiry."""
        from ..cli import output, request

        output(request("DELETE", f"sets/{handle}"), json)

    @sessions.command("show")
    def session_show(session_id: str, json: bool = False):
        """A session's sets in creation order and its unresolved needs."""
        from ..cli import output, request

        output(request("GET", f"sessions/{session_id}"), json)
