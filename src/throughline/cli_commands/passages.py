"""`throughline lib passages build|status`: the passage store of page revisions."""

import typer


def register(group):
    passages_group = typer.Typer(no_args_is_help=True)
    group.add_typer(
        passages_group, name="passages", short_help="Passages of page revisions."
    )

    @passages_group.command("build")
    def build(
        limit: int = typer.Option(200, min=1, help="Revisions per batch."),
        embed: bool = typer.Option(
            False, help="Also embed passage texts with the configured model."
        ),
        json: bool = False,
    ):
        """Split every page revision that has no passages yet."""
        from .. import passages
        from ..cli import output
        from ..db import connect

        built = 0
        while True:
            with connect("app") as conn:
                count = passages.build(conn, limit)
            built += count
            if count < limit:
                break
        embedded = 0
        if embed:
            while True:
                count = passages.embed_once()
                embedded += count
                if not count:
                    break
        with connect("app") as conn:
            state = passages.pending(conn)
        output(dict(built=built, embedded=embedded, **state), json)

    @passages_group.command("status")
    def status(json: bool = False):
        """Revisions without passages and passage texts without a vector."""
        from .. import passages
        from ..cli import output
        from ..db import connect

        with connect("app") as conn:
            state = passages.pending(conn)
            state["passages"] = conn.execute(
                "SELECT count(*) n FROM tl.passages"
            ).fetchone()["n"]
            state["distinct_texts"] = conn.execute(
                "SELECT count(DISTINCT text_hash) n FROM tl.passages"
            ).fetchone()["n"]
        output(state, json)
