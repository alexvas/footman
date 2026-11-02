"""Operator entry point; MCP startup will be implemented here later."""

import argparse
import sys
from collections.abc import Sequence

from footman.configuration import ConfigurationError, ConfiguredApplication


def main(argv: Sequence[str] | None = None) -> int:
    """Parse startup options without pretending the MCP server exists yet."""
    parser = argparse.ArgumentParser(
        prog="footman",
        description="Stdio MCP dispatcher (project scaffold; not implemented yet).",
        allow_abbrev=False,
    )
    parser.add_argument("--config", required=True, metavar="CONFIG.TOML")
    args = parser.parse_args(argv)
    try:
        application = ConfiguredApplication.from_path(args.config)
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 1
    return serve(application)


def serve(application: ConfiguredApplication) -> int:
    """Protocol adapter is delivered in Phase 8; never dispatch from the CLI."""
    print("MCP server is not implemented yet; no command was executed", file=sys.stderr)
    return 1
