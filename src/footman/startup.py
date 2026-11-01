"""Operator entry point; MCP startup will be implemented here later."""

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Parse startup options without pretending the MCP server exists yet."""
    parser = argparse.ArgumentParser(
        prog="footman",
        description="Stdio MCP dispatcher (project scaffold; not implemented yet).",
        allow_abbrev=False,
    )
    parser.add_argument("--config", required=True, metavar="CONFIG.TOML")
    parser.parse_args(argv)
    parser.error("MCP server is not implemented yet; no command was executed")
