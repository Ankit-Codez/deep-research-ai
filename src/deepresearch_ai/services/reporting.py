"""Safe text rendering shared by the Phase 2 service and the graph writer."""

import html
import re


def display_title(value: str) -> str:
    """Decode title entities as plain text; never interpret them as HTML."""
    for _ in range(2):
        value = html.unescape(value)
    return ''.join(character for character in value if character.isprintable()).strip()


def escape_markdown(value: str) -> str:
    value = "".join(character if character.isprintable() else f"\\u{ord(character):04x}" for character in value)
    return re.sub(r"([\\`*{}\[\]()#+.!|_>~-])", r"\\\1", html.escape(value))
