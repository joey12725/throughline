"""`throughline lib setup` and `throughline lib plugins`: onboarding on the box.

    throughline lib setup code          the one-time code for the first account
    throughline lib setup status        progress, detected sources, providers
    throughline lib setup prompt        the setup prompt to paste into an agent
    throughline lib setup restored      mark this install as restored (onboarding
                                        then treats existing sources as connected)
    throughline lib plugins validate PATH   check a plugin repository's metadata
    throughline lib plugins list        the catalog with install/connection state
"""

from pathlib import Path

import typer


def register(group):
    setup = typer.Typer(no_args_is_help=True)
    plugins = typer.Typer(no_args_is_help=True)
    group.add_typer(setup, name="setup", short_help="First-run onboarding.")
    group.add_typer(plugins, name="plugins", short_help="Plugin repositories and the catalog.")

    @setup.command("code")
    def code():
        """Print the first-account setup code (and the link to open)."""
        import os

        from ..onboarding import owner

        if owner.accounts_exist():
            typer.echo("An account already exists; sign in with it.")
            raise typer.Exit(0)
        value = owner.setup_code()
        base = os.getenv("THROUGHLINE_PUBLIC_URL") or "http://127.0.0.1:8000"
        typer.echo(f"Setup code: {value}\nOpen: {base.rstrip('/')}/ui/welcome?setup={value}")

    @setup.command("status")
    def status(json: bool = False):
        """Onboarding progress and what the install already holds."""
        from ..cli import output
        from ..onboarding import catalog, state, store

        store.apply()
        output(dict(state=state.get(), detected=catalog.detect()), json)

    @setup.command("prompt")
    def prompt(url: str = typer.Option("http://127.0.0.1:8000", help="Throughline URL as the agent reaches it")):
        """The setup prompt for an agent (paste it into Claude, ChatGPT, …)."""
        from ..onboarding import agent

        typer.echo(agent.setup_prompt(url))

    @setup.command("restored")
    def restored():
        """Mark this install as restored from a backup and keep onboarding open."""
        from ..onboarding import state, store

        path = store.setup_dir() / "restored"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("restored\n", encoding="utf-8")
        try:
            state.mark_restored(keep_steps=True)
        except Exception:
            pass
        typer.echo(f"Marked as restored ({path}). Onboarding will show existing sources as connected and stay open to add more.")

    @plugins.command("validate")
    def validate(path: Path):
        """Check a plugin repository against the onboarding contract."""
        from ..onboarding.contract import ContractError, load_repo

        try:
            index = load_repo(path)
        except ContractError as error:
            typer.echo(f"✗ {error}", err=True)
            for problem in error.problems:
                typer.echo(f"  {problem['where']}: {problem['message']}", err=True)
            raise typer.Exit(1) from None
        typer.echo(f"✓ {index.name}: {len(index.plugins)} plugin(s)")
        for spec in index.plugins:
            typer.echo(f"  {spec.id:<20} {spec.category:<10} {spec.name}")

    @plugins.command("list")
    def listing(json: bool = False):
        """The catalog: every plugin with its install and connection state."""
        from ..cli import output
        from ..onboarding import catalog

        view = catalog.catalog()
        if json:
            output(view, True)
            return
        for plugin in view["plugins"]:
            s = plugin["status"]
            flags = ("connected " if s["connected"] else "") + ("installed " if s["installed"] else "") + (
                "enabled" if s["enabled"] else "")
            typer.echo(f"{plugin['id']:<20} {plugin['category']:<10} {flags.strip()}")
