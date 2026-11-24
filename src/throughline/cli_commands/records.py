"""`throughline lib describe`: the archive's own description."""

import typer


def register(group):
    @group.command()
    def describe(
        subject: str = typer.Argument(None),
        json: bool = False,
    ):
        """Describe the archive or one subject."""
        from ..cli import output, request

        output(
            request(
                "GET", "describe", params={"subject": subject} if subject else None
            ),
            json,
        )
