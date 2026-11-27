"""Knightly Review core: fetch games, analyse them, pick mistakes, write notes.

Pure Python with no Anki imports, so the same code runs inside the Anki add-on
(vendored), from the command line, and later in a companion process or a server.
Internal imports are relative; `chess` is imported by name and rewritten by the
add-on's build when vendored.
"""

__version__ = "0.2.0"
USER_AGENT = f"knightly-review/{__version__} (+https://github.com/NeTiFeSi/knightly-review)"
