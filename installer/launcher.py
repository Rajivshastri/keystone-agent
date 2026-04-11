"""PyInstaller entry point for the Keystone agent.

The real entry is `agent.main:main`, but PyInstaller cannot use a
module-inside-a-package as its script — when it runs agent/main.py
directly, the relative `from .local_ui import ...` fails because
there is no parent package at runtime. This thin wrapper imports
the package normally and calls its main() function.
"""
from agent.main import main

if __name__ == "__main__":
    raise SystemExit(main())
