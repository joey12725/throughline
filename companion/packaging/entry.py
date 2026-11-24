"""PyInstaller entry point of the console build (throughline-companion)."""

import sys

from throughline_companion.cli import main

if __name__ == "__main__":
    sys.exit(main())
