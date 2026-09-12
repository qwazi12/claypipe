"""Entry point for `python -m claypipe`.

The console script installed by pyproject is `claypipe`; this makes the module
form work identically, which is what a venv-relative invocation
(`.venv/bin/python -m claypipe ...`) needs when the script is not on PATH.
"""

from .cli import app

if __name__ == "__main__":
    app()
