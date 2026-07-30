from click.testing import CliRunner

from evm.cli import main


def test_main_prints_greeting() -> None:
    result = CliRunner().invoke(main)

    assert result.exit_code == 0
    assert result.output == "Hello from evm!\n"
