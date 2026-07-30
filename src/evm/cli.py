"""Command-line interface for EVM."""

import click


@click.command()
def main() -> None:
    """Manage Eiffel projects and their dependencies."""
    click.echo("Hello from evm!")
