"""`throughline lib person ...`: the person-centric read model over HTTP with
THROUGHLINE_TOKEN. Start with `lib person brief ID`."""

import typer


def _show(result, json):
    from ..cli import output

    if json:
        output(result, True)
        return
    rows = result.get("rows")
    if rows is None:
        for key, value in result.items():
            if key != "execution":
                output(value if isinstance(value, dict) else {key: value})
    else:
        for row in rows:
            output(row)
    incomplete = (result.get("execution") or {}).get("incomplete") or []
    for item in incomplete:
        output({"incomplete": item.get("reason"), "stage": item.get("stage")})


def _params(**values):
    return {k: v for k, v in values.items() if v is not None}


def register(group):
    person = typer.Typer(no_args_is_help=True)
    group.add_typer(person, name="person", short_help="People: brief first.")

    @person.command("brief")
    def brief(person_id: int, json: bool = False):
        """Who the person is and the shape of contact (the default first step)."""
        from ..cli import request

        _show(request("GET", f"people/{person_id}/brief"), json)

    @person.command("timeline")
    def timeline(
        person_id: int,
        after: str | None = None,
        before: str | None = None,
        max_results: int = 200,
        json: bool = False,
    ):
        """Messages from and to the person, newest first."""
        from ..cli import request

        params = _params(after=after, before=before, max_results=max_results)
        _show(request("GET", f"people/{person_id}/timeline", params=params), json)

    @person.command("arc")
    def arc(person_id: int, grain: str = "month", json: bool = False):
        """Per period counts with first, peak and last periods."""
        from ..cli import request

        params = dict(grain=grain)
        _show(request("GET", f"people/{person_id}/arc", params=params), json)

    @person.command("dms")
    def dms(
        person_id: int,
        after: str | None = None,
        before: str | None = None,
        max_results: int = 200,
        json: bool = False,
    ):
        """Both sides of the one-to-one threads with the owner."""
        from ..cli import request

        params = _params(after=after, before=before, max_results=max_results)
        _show(request("GET", f"people/{person_id}/dms", params=params), json)

    @person.command("last")
    def last(person_id: int, json: bool = False):
        """Last from them, last to them, last exchange."""
        from ..cli import request

        _show(request("GET", f"people/{person_id}/last-contact"), json)

    @person.command("loops")
    def loops(
        person_id: int | None = typer.Argument(None),
        older_than: str = "1 day",
        horizon_days: int = 30,
        max_results: int = 100,
        json: bool = False,
    ):
        """Open loops either way (owner_owes | they_owe)."""
        from ..cli import request

        params = _params(
            person=person_id,
            older_than=older_than,
            horizon_days=horizon_days,
            max_results=max_results,
        )
        _show(request("GET", "open-loops", params=params), json)

    @person.command("circle")
    def circle(
        person_id: int,
        mode: str = "absolute",
        after: str | None = None,
        before: str | None = None,
        max_results: int = 50,
        json: bool = False,
    ):
        """Ties ranked by strength (exchanges + shared conversations)."""
        from ..cli import request

        params = _params(mode=mode, after=after, before=before, max_results=max_results)
        _show(request("GET", f"people/{person_id}/circle", params=params), json)

    @person.command("ties")
    def ties(
        a: int,
        b: int,
        after: str | None = None,
        before: str | None = None,
        json: bool = False,
    ):
        """Pair breakdown of a tie."""
        from ..cli import request

        params = _params(a=a, b=b, after=after, before=before)
        _show(request("GET", "people/ties", params=params), json)

    @person.command("rhythm")
    def rhythm(
        person_id: int,
        after: str | None = None,
        before: str | None = None,
        tz: str | None = None,
        json: bool = False,
    ):
        """Messages by weekday and hour, and by month."""
        from ..cli import request

        params = _params(after=after, before=before, tz=tz)
        _show(request("GET", f"people/{person_id}/rhythm", params=params), json)

    @person.command("mentions")
    def mentions(
        person_id: int,
        after: str | None = None,
        before: str | None = None,
        max_results: int = 100,
        json: bool = False,
    ):
        """Messages naming the person, not by the person."""
        from ..cli import request

        params = _params(after=after, before=before, max_results=max_results)
        _show(request("GET", f"people/{person_id}/mentions", params=params), json)

    @person.command("around")
    def around(
        at: str,
        window_s: int = 1800,
        persons: str | None = typer.Option(None, help="Comma-separated ids"),
        max_results: int = 100,
        include_ignored: bool = False,
        json: bool = False,
    ):
        """Records across channels around a time."""
        from ..cli import request

        params = _params(
            at=at,
            window_s=window_s,
            persons=persons,
            max_results=max_results,
            include_ignored=include_ignored or None,
        )
        _show(request("GET", "around", params=params), json)

    @person.command("day")
    def day(
        date: str,
        tz: str | None = None,
        persons: str | None = typer.Option(None, help="Comma-separated ids"),
        max_results: int = 500,
        include_ignored: bool = False,
        json: bool = False,
    ):
        """Records of one local day."""
        from ..cli import request

        params = _params(
            date=date,
            tz=tz,
            persons=persons,
            max_results=max_results,
            include_ignored=include_ignored or None,
        )
        _show(request("GET", "day", params=params), json)

    @person.command("together")
    def together(
        persons: str = typer.Argument(..., help="Comma-separated ids"),
        after: str | None = None,
        before: str | None = None,
        max_results: int = 100,
        json: bool = False,
    ):
        """Episodes where every listed person has a message."""
        from ..cli import request

        params = _params(
            persons=persons, after=after, before=before, max_results=max_results
        )
        _show(request("GET", "together", params=params), json)

    @person.command("between")
    def between(
        persons: str = typer.Argument(..., help="Comma-separated ids"),
        after: str | None = None,
        before: str | None = None,
        max_results: int = 200,
        json: bool = False,
    ):
        """Both sides of the threads the listed persons share."""
        from ..cli import request

        params = _params(
            persons=persons, after=after, before=before, max_results=max_results
        )
        _show(request("GET", "between", params=params), json)

    @person.command("index")
    def index(json: bool = False):
        """Refresh the exchange index within the budget; show its state."""
        from ..cli import request

        _show(request("GET", "people/index"), json)

    @person.command("status")
    def status(
        identifier_id: int,
        value: str = typer.Argument(..., help="confirmed | suspected | ignored"),
        reason: str | None = None,
        json: bool = False,
    ):
        """Set an account identifier's status (owner token)."""
        from ..cli import request

        body = _params(status=value, reason=reason)
        _show(
            request("POST", f"identity/identifiers/{identifier_id}/status", data=body),
            json,
        )
