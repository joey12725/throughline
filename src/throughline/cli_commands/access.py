"""`throughline lib asker ...` and `throughline lib access ...`: who may read
what, and the sensitivity labels that narrow it. Owner database connection."""

import typer

ACTOR = "cli"


def _run(operation, as_json, *args, **kwargs):
    from .. import access
    from ..cli import output

    try:
        output(getattr(access, operation)(*args, **kwargs), as_json)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from None


def _split(labels):
    return [part.strip() for part in (labels or "").split(",") if part.strip()]


def _grant_commands(group):
    """grant, extend and revoke; mounted on both groups."""

    @group.command("grant")
    def grant(
        name: str,
        channel: str | None = None,
        thread: int | None = None,
        labels: str | None = typer.Option(
            None, help="Comma-separated labels this grant permits."
        ),
        purpose: str | None = typer.Option(None, help="Why (kept in the audit)."),
        expires: str | None = typer.Option(None, help="ISO 8601 expiry time."),
        json: bool = False,
    ):
        """Grant one channel (--channel NAME) or one thread (--thread ID)."""
        _run(
            "grant",
            json,
            name,
            channel=channel,
            thread_id=thread,
            labels=_split(labels),
            purpose=purpose,
            expires_at=expires,
            actor=ACTOR,
        )

    @group.command("extend")
    def extend(
        name: str,
        channel: str | None = None,
        thread: int | None = None,
        expires: str = typer.Option(
            ...,
            help="New ISO 8601 expiry, or 'never' to remove the expiry.",
        ),
        purpose: str | None = typer.Option(None, help="Why (kept in the audit)."),
        json: bool = False,
    ):
        """Move the expiry of one held grant. --expires is required, so a
        forgotten option never turns an expiring grant into a permanent one."""
        never = expires.strip().lower() in ("never", "none")
        _run(
            "extend",
            json,
            name,
            None if never else expires,
            channel=channel,
            thread_id=thread,
            purpose=purpose,
            actor=ACTOR,
        )

    @group.command("revoke")
    def revoke(
        name: str,
        channel: str | None = None,
        thread: int | None = None,
        purpose: str | None = typer.Option(None, help="Why (kept in the audit)."),
        json: bool = False,
    ):
        """Withdraw one channel or thread grant."""
        _run(
            "revoke",
            json,
            name,
            channel=channel,
            thread_id=thread,
            purpose=purpose,
            actor=ACTOR,
        )


def register(group):
    asker = typer.Typer(no_args_is_help=True)
    group.add_typer(asker, name="asker", short_help="Askers and their grants.")

    @asker.command("add")
    def asker_add(name: str, mode: str = "restricted", json: bool = False):
        """Register an asker: restricted (grants only) or owner (everything)."""
        _run("register_asker", json, name, mode, actor=ACTOR)

    @asker.command("remove")
    def asker_remove(name: str, json: bool = False):
        """Delete an asker and its grants."""
        _run("remove_asker", json, name, actor=ACTOR)

    _grant_commands(asker)

    @asker.command("list")
    def asker_list(json: bool = False):
        """List askers with their grants and the policy version."""
        from .. import access
        from ..cli import output

        output(
            dict(askers=access.list_askers(), policy_version=access.policy_version()),
            json,
        )

    disclosure = typer.Typer(no_args_is_help=True)
    group.add_typer(
        disclosure, name="access", short_help="Sensitivity labels and grants."
    )

    @disclosure.command("label")
    def access_label(
        label: str,
        channel: str | None = None,
        thread: int | None = None,
        purpose: str | None = typer.Option(None, help="Why (kept in the audit)."),
        json: bool = False,
    ):
        """Label one channel (--channel NAME) or one thread (--thread ID)."""
        _run(
            "label",
            json,
            label,
            channel=channel,
            thread_id=thread,
            purpose=purpose,
            actor=ACTOR,
        )

    @disclosure.command("unlabel")
    def access_unlabel(
        label: str,
        channel: str | None = None,
        thread: int | None = None,
        purpose: str | None = typer.Option(None, help="Why (kept in the audit)."),
        json: bool = False,
    ):
        """Remove a label from one channel or thread."""
        _run(
            "unlabel",
            json,
            label,
            channel=channel,
            thread_id=thread,
            purpose=purpose,
            actor=ACTOR,
        )

    @disclosure.command("labels")
    def access_labels(json: bool = False):
        """List label kinds with the channels and threads carrying them."""
        _run("list_labels", json)

    @disclosure.command("kind")
    def access_kind(
        label: str,
        description: str = "",
        purpose: str | None = typer.Option(None, help="Why (kept in the audit)."),
        json: bool = False,
    ):
        """Add a label kind (messages whose access_label names it are labelled)."""
        _run(
            "add_label_kind",
            json,
            label,
            description,
            purpose=purpose,
            actor=ACTOR,
        )

    @disclosure.command("egress")
    def access_egress(
        label: str,
        allow: bool = typer.Option(
            False, "--allow", help="Content under the label may be sent."
        ),
        deny: bool = typer.Option(
            False, "--deny", help="Content under the label stays on this host."
        ),
        purpose: str | None = typer.Option(None, help="Why (kept in the audit)."),
        json: bool = False,
    ):
        """Let content under a label reach model providers off this host, or
        keep it here (the default)."""
        if allow == deny:
            raise typer.BadParameter("give exactly one of --allow and --deny")
        _run("set_label_egress", json, label, allow, purpose=purpose, actor=ACTOR)

    _grant_commands(disclosure)

    @disclosure.command("grants")
    def access_grants(asker: str | None = None, json: bool = False):
        """List grants with labels, purpose and expiry."""
        _run("list_grants", json, asker)

    @disclosure.command("events")
    def access_events(asker: str | None = None, limit: int = 100, json: bool = False):
        """The newest label and grant changes."""
        _run("access_events", json, asker, limit)
