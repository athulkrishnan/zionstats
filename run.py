"""PyInstaller entry point: absolute imports only."""
import sys

from zionstats.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
