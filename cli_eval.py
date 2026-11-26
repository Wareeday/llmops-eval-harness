"""Backwards-compatible shim: ``python cli_eval.py`` == ``llmops-eval``."""

from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
