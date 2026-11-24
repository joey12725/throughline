"""`throughline lib feed create|list|news|pending|mark|drop|settings`:
feeds (named arrival cursors), over HTTP with THROUGHLINE_TOKEN. Mark after
work. `throughline lib consumer ...` is a hidden, deprecated alias."""

import json as json_module
from urllib.parse import quote

import typer


def _json(text, name):
    try:
        return json_module.loads(text)
    except ValueError:
        raise typer.BadParameter(f"{name} must be JSON") from None


def _path(name):
    return "feeds/" + quote(name, safe="")


def register(group):
    feed = typer.Typer(no_args_is_help=True)
    group.add_typer(feed, name="feed", short_help="Feeds: arrival cursors.")
    group.add_typer(feed, name="consumer", hidden=True, help="Deprecated: use `feed`.")

    @feed.command("create")
    def create(
        name: str,
        scope: str | None = typer.Option(None, help="JSON feed scope"),
        start: str = typer.Option("now", help="now, beginning or an ISO time"),
        json: bool = False,
    ):
        """Create a named feed."""
        from ..cli import output, request

        body = {"name": name, "start": start}
        if scope:
            body["scope"] = _json(scope, "scope")
        output(request("POST", "feeds", body), json)

    @feed.command("list")
    def list_(json: bool = False):
        """Your feeds."""
        from ..cli import output, request

        output(request("GET", "feeds")["feeds"], json)

    @feed.command("news")
    def news(name: str, max: int = 200, json: bool = False):
        """A page of arrivals after the cursor."""
        from ..cli import output, request

        output(request("GET", _path(name) + "/news", params={"max": max}), json)

    @feed.command("pending")
    def pending(name: str, json: bool = False):
        """How many settled arrivals are not marked."""
        from ..cli import output, request

        output(request("GET", _path(name) + "/pending"), json)

    @feed.command("mark")
    def mark(
        name: str,
        to: str = typer.Option(..., help="JSON cursor {ingested_at, message_id}"),
        expected: str | None = typer.Option(None, help="JSON previous cursor"),
        json: bool = False,
    ):
        """Move the cursor after the work is done."""
        from ..cli import output, request

        body = {"to": _json(to, "to")}
        if expected:
            body["expected"] = _json(expected, "expected")
        output(request("POST", _path(name) + "/mark", body), json)

    @feed.command("drop")
    def drop(name: str, json: bool = False):
        """Delete a feed."""
        from ..cli import output, request

        output(request("DELETE", _path(name)), json)

    @feed.command("settings")
    def settings(
        settle_s: int | None = None, margin_s: int | None = None, json: bool = False
    ):
        """Show, or (owner) change, the settle window."""
        from ..cli import output, request

        if settle_s is None and margin_s is None:
            output(request("GET", "feeds/settings"), json)
            return
        body = {}
        if settle_s is not None:
            body["settle_s"] = settle_s
        if margin_s is not None:
            body["margin_s"] = margin_s
        output(request("PUT", "feeds/settings", body), json)
