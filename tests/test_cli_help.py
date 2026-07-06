from click.testing import CliRunner

from newspaper_scrapper.cli.main import cli


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "auth-export-cookies" in result.output
    assert "auth-import-cookies" in result.output
    assert "capture-viewer-screenshot" in result.output
    assert "screenshot-pages" in result.output
    assert "screenshot-pages-production" in result.output
    assert "download-pages" in result.output
    assert "build-source-artifact-manifest" in result.output
    assert "validate-source-artifact-manifest" in result.output
    assert "shard-manifest" in result.output
    assert "torch-check" in result.output
