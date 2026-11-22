"""Front-end-neutral factory operations.

Everything here reads or changes factory state and returns typed,
JSON-serializable results (``to_dict()``) with structured reason codes. Nothing
prints, prompts or parses arguments: the CLI in ``commands.py`` is one renderer,
and a TUI, web GUI or MCP server can call the same functions.
"""
