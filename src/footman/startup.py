"""Operator entry point for the stdio MCP service."""

import argparse
import sys
import traceback
from collections.abc import Sequence

from footman.configuration import ConfigurationError, ConfiguredApplication
from footman.server import diagnostic


def main(argv: Sequence[str] | None = None) -> int:
    """Load the immutable operator configuration before protocol startup."""
    parser = DiagnosticParser(
        prog="footman",
        description="Staged-snapshot privileged command dispatcher over stdio MCP.",
        allow_abbrev=False,
    )
    parser.add_argument("--config", required=True, metavar="CONFIG.TOML")
    args = parser.parse_args(argv)
    try:
        application = ConfiguredApplication.from_path(args.config)
    except ConfigurationError as error:
        print(diagnostic(str(error)), file=sys.stderr)
        return 1
    try:
        return serve(application)
    except Exception as error:
        print(
            diagnostic("".join(traceback.format_exception(error)).rstrip("\n")),
            file=sys.stderr,
        )
        return 1


class DiagnosticParser(argparse.ArgumentParser):
    def _print_message(self, message: str | None, file=None) -> None:
        if message:
            target = file or sys.stderr
            target.write(diagnostic(message) if target is sys.stderr else message)


def serve(application: ConfiguredApplication) -> int:
    """Serve MCP only; there is no direct-dispatch CLI."""
    from footman.server import serve as run_server

    return run_server(application)
