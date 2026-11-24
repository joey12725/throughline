"""`throughline lib proposal|link-type|annotate|annotation ...`: proposals,
the link-type registry and annotations, over HTTP with THROUGHLINE_TOKEN."""

import json as json_module

import typer


def _json(text, name):
    try:
        return json_module.loads(text)
    except ValueError:
        raise typer.BadParameter(f"{name} must be JSON") from None


def register(group):
    proposal = typer.Typer(no_args_is_help=True)
    group.add_typer(proposal, name="proposal", short_help="Proposals queue.")
    link_type = typer.Typer(no_args_is_help=True)
    group.add_typer(link_type, name="link-type", short_help="Link-type registry.")
    annotation = typer.Typer(no_args_is_help=True)
    group.add_typer(annotation, name="annotation", short_help="Annotations.")

    @proposal.command("list")
    def proposal_list(status: str | None = None, limit: int = 100, json: bool = False):
        """Proposals (owner: all; others: their own)."""
        from ..cli import output, request

        params = {"limit": limit}
        if status:
            params["status"] = status
        output(request("GET", "proposals", params=params), json)

    @proposal.command("accept")
    def proposal_accept(proposal_id: int, note: str | None = None, json: bool = False):
        """Accept a proposal: the object is created with its originating method."""
        from ..cli import output, request

        output(
            request(
                "POST",
                f"proposals/{proposal_id}/decide",
                {"accept": True, "note": note},
            ),
            json,
        )

    @proposal.command("reject")
    def proposal_reject(proposal_id: int, note: str | None = None, json: bool = False):
        """Reject a proposal."""
        from ..cli import output, request

        output(
            request(
                "POST",
                f"proposals/{proposal_id}/decide",
                {"accept": False, "note": note},
            ),
            json,
        )

    @link_type.command("list")
    def link_type_list(
        status: str | None = None,
        with_counts: bool = False,
        json: bool = False,
    ):
        """Registered link types (counts and candidates: owner only)."""
        from ..cli import output, request

        params = {}
        if status:
            params["status"] = status
        if with_counts:
            params["with_counts"] = "true"
        output(request("GET", "link-types", params=params), json)

    @link_type.command("register")
    def link_type_register(
        name: str,
        description: str | None = None,
        inverse: str | None = None,
        json: bool = False,
    ):
        """Register a link type (owner)."""
        from ..cli import output, request

        output(
            request(
                "POST",
                "link-types",
                {"type": name, "description": description, "inverse": inverse},
            ),
            json,
        )

    @link_type.command("merge")
    def link_type_merge(name: str, into: str, json: bool = False):
        """Fold a type into another: synonym, re-typed links, duplicates collapsed."""
        from ..cli import output, request

        output(request("POST", f"link-types/{name}/merge", {"into": into}), json)

    @link_type.command("retire")
    def link_type_retire(name: str, into: str | None = None, json: bool = False):
        """Retire a type (types imports use need --into, which merges)."""
        from ..cli import output, request

        output(request("POST", f"link-types/{name}/retire", {"into": into}), json)

    @group.command("annotate")
    def annotate(
        message_id: int,
        attribute: str,
        value_json: str,
        reason: str | None = None,
        json: bool = False,
    ):
        """Annotate a message (owner: direct; others: a proposal)."""
        from ..cli import output, request

        output(
            request(
                "POST",
                "annotations",
                {
                    "message_id": message_id,
                    "attribute": attribute,
                    "value": _json(value_json, "VALUE_JSON"),
                    "reason": reason,
                },
            ),
            json,
        )

    @annotation.command("list")
    def annotation_list(message_id: int, json: bool = False):
        """Annotations of a message's record (through the asker gate)."""
        from ..cli import output, request

        output(request("GET", "annotations", params={"message_id": message_id}), json)

    @annotation.command("retract")
    def annotation_retract(
        annotation_id: int, reason: str | None = None, json: bool = False
    ):
        """Retract an annotation (owner)."""
        from ..cli import output, request

        output(
            request("POST", f"annotations/{annotation_id}/retract", {"reason": reason}),
            json,
        )
