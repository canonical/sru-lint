import logging

import typer

from sru_lint.common.launchpad_helper import LaunchpadHelper
from sru_lint.common.logging import set_log_level

app = typer.Typer(add_completion=False)


@app.command()
def main(package: str = typer.Argument(..., metavar="PACKAGE", help="Source package to query")):
    """List authenticated Ubuntu Pro and ESM source publications, like rmadison."""
    set_log_level(logging.WARNING)
    for publication in LaunchpadHelper().get_pro_publications(package):
        typer.echo(
            f"{package} | {publication.version} | {publication.series} | "
            f"{publication.stream} | {publication.pocket}"
        )
