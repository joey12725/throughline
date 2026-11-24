"""`throughline lib account ...`: web sign-in accounts (owner database
connection; docs/web/auth.md)."""

import sys

import typer


def _password(prompt, from_stdin):
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    return typer.prompt(prompt, hide_input=True, confirmation_prompt=True)


def register(group):
    account = typer.Typer(no_args_is_help=True, help="Web sign-in accounts.")
    group.add_typer(account, name="account")

    def run(fn, *args, json=False, **kwargs):
        from .. import cli

        try:
            cli.output(fn(*args, **kwargs), json)
        except ValueError as error:
            raise typer.BadParameter(str(error)) from None

    @account.command("create")
    def create(
        handle: str,
        name: str | None = typer.Option(None, help="Display name."),
        email: str | None = typer.Option(None, help="Links a provider identity with this verified address on first sign-in."),
        password: bool = typer.Option(False, "--password", help="Prompt for a password."),
        password_stdin: bool = typer.Option(False, help="Read the password from standard input."),
        json: bool = False,
    ):
        """Create an account with full owner access to this installation."""
        from ..auth import accounts

        secret = _password("Password", password_stdin) if password or password_stdin else None
        run(accounts.create, handle, name, email, secret, json=json)

    @account.command("list")
    def listing(json: bool = False):
        """Accounts, their sign-in methods and live sessions."""
        from ..auth import accounts

        run(accounts.listing, json=json)

    @account.command("update")
    def update(
        handle: str,
        name: str | None = typer.Option(None, help="Display name ('' clears)."),
        email: str | None = typer.Option(None, help="E-mail address ('' clears)."),
        json: bool = False,
    ):
        """Change an account's display name or e-mail address."""
        from ..auth import accounts

        run(accounts.update, handle, name, email, json=json)

    @account.command("password")
    def password(
        handle: str,
        remove: bool = typer.Option(False, help="Remove the password (provider sign-in only)."),
        password_stdin: bool = typer.Option(False, help="Read the password from standard input."),
        json: bool = False,
    ):
        """Set or remove an account's password; ends its password sessions."""
        from ..auth import accounts

        secret = None if remove else _password("New password", password_stdin)
        run(accounts.set_password, handle, secret, json=json)

    @account.command("link")
    def link(
        handle: str,
        provider: str = typer.Option(..., help="Provider id, as in THROUGHLINE_AUTH_PROVIDERS."),
        subject: str = typer.Option(..., help="The identity's subject (sub claim)."),
        json: bool = False,
    ):
        """Link an external identity to an account."""
        from ..auth import accounts

        run(accounts.link, handle, provider, subject, json=json)

    @account.command("unlink")
    def unlink(
        handle: str,
        provider: str = typer.Option(...),
        subject: str | None = typer.Option(None, help="Only this identity (default: all of the provider's)."),
        json: bool = False,
    ):
        """Remove a linked identity; ends the sessions it opened."""
        from ..auth import accounts

        run(accounts.unlink, handle, provider, subject, json=json)

    @account.command("disable")
    def disable(handle: str, json: bool = False):
        """Stop an account from signing in and end its sessions."""
        from ..auth import accounts

        run(accounts.set_status, handle, False, json=json)

    @account.command("enable")
    def enable(handle: str, json: bool = False):
        """Let a disabled account sign in again."""
        from ..auth import accounts

        run(accounts.set_status, handle, True, json=json)

    @account.command("sessions")
    def sessions(handle: str | None = typer.Argument(None), json: bool = False):
        """Live browser sessions, newest activity first."""
        from ..auth import accounts

        run(accounts.sessions, handle, json=json)

    @account.command("sign-out")
    def sign_out(
        handle: str | None = typer.Argument(None),
        session: int | None = typer.Option(None, help="Only this session id."),
        all_accounts: bool = typer.Option(False, "--all", help="Every account's sessions."),
        json: bool = False,
    ):
        """End browser sessions of one account, one session, or (--all) every one."""
        from ..auth import accounts

        if handle is None and session is None and not all_accounts:
            raise typer.BadParameter("name an account, --session ID or --all")
        run(accounts.revoke_sessions, handle, session, json=json)
