"""`throughline lib recipe list|show|run`: tested recipes (INT-9)."""

import typer


def register(group):
    recipe = typer.Typer(no_args_is_help=True, help="Tested query recipes.")
    group.add_typer(recipe, name="recipe")

    @recipe.command("list")
    def recipe_list(json: bool = False):
        """List tested recipes."""
        from ..cli import output, request

        rows = request("GET", "recipes")
        output(
            rows
            if json
            else [
                {"name": r["name"], "version": r["version"], "summary": r["summary"]}
                for r in rows
            ],
            json,
        )

    @recipe.command("show")
    def recipe_show(name: str, json: bool = False):
        """Show one recipe with its notes."""
        from ..cli import output, request

        row = request("GET", "recipes/" + name.strip("/").replace("/", ""))
        if json:
            output(row, True)
            return
        typer.echo(f"{row['name']} v{row['version']}: {row['summary']}")
        for index, statement in enumerate(row["statements"], 1):
            typer.echo(f"-- statement {index}\n{statement}")
        typer.echo(row["body"])

    @recipe.command("run")
    def recipe_run(name: str, json: bool = False):
        """Run a recipe's statements over the API and check its expectation."""
        from .. import recipes
        from ..cli import output, request

        row = request("GET", "recipes/" + name.strip("/").replace("/", ""))
        report = recipes.run(request, row)
        output(report, json)
        if report["status"] == "failed":
            raise typer.Exit(1)
