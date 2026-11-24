"""`throughline lib transcripts noise run|status` and
`throughline lib transcripts speakers list|assign|ignore`: media-noise flags
and the speaker inbox, over HTTP with THROUGHLINE_TOKEN."""

import json as json_module

import typer


def _json(text, name):
    try:
        return json_module.loads(text)
    except ValueError:
        raise typer.BadParameter(f"{name} must be JSON") from None


def register(group):
    transcripts = typer.Typer(no_args_is_help=True)
    group.add_typer(
        transcripts, name="transcripts", short_help="Media noise and speakers."
    )
    noise = typer.Typer(no_args_is_help=True)
    transcripts.add_typer(noise, name="noise", short_help="Media-noise flags.")
    speakers = typer.Typer(no_args_is_help=True)
    transcripts.add_typer(speakers, name="speakers", short_help="Speaker inbox.")

    @noise.command("run")
    def noise_run(
        policy: str | None = typer.Option(None, help="JSON policy"),
        dry_run: bool = typer.Option(False, help="Count only; no call, no write"),
        json: bool = False,
    ):
        """One bounded media-noise run (fails open; see status and failures)."""
        from ..cli import output, request

        body = {"dry_run": dry_run}
        if policy:
            body["policy"] = _json(policy, "policy")
        output(request("POST", "transcripts/noise/run", body), json)

    @noise.command("status")
    def noise_status(json: bool = False):
        """Pending and judged recordings and the latest runs."""
        from ..cli import output, request

        output(request("GET", "transcripts/noise/status"), json)

    @speakers.command("list")
    def speakers_list(max: int = 100, json: bool = False):
        """Unclaimed speaker labels per recording."""
        from ..cli import output, request

        result = request("GET", "transcripts/speakers", params={"max_results": max})
        output(result if json else result["speakers"], json)

    @speakers.command("assign")
    def speakers_assign(
        thread_id: int,
        label: str,
        person_id: int,
        reason: str | None = None,
        json: bool = False,
    ):
        """Annotate the label's turns in that recording with a person."""
        from ..cli import output, request

        body = {"thread_id": thread_id, "label": label, "person_id": person_id}
        if reason:
            body["reason"] = reason
        output(request("POST", "transcripts/speakers/assign", body), json)

    @speakers.command("ignore")
    def speakers_ignore(
        thread_id: int, label: str, reason: str | None = None, json: bool = False
    ):
        """Ignore the label's turns in that recording (e.g. a television)."""
        from ..cli import output, request

        body = {"thread_id": thread_id, "label": label}
        if reason:
            body["reason"] = reason
        output(request("POST", "transcripts/speakers/ignore", body), json)
