"""Console entry points for the demo package."""

import sys

from .core import greet


def cli_main():
    name = sys.argv[1] if len(sys.argv) > 1 else None
    print("cli_main:", greet(name or "cli"))
    return 0


if __name__ == "__main__":
    cli_main()
