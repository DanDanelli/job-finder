"""Text helpers used by normalization and deduplication."""

import re
import unicodedata

from selectolax.lexbor import LexborHTMLParser

_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def key(text: str) -> str:
    """Canonical form for comparison: lowercase, no accents, letters/digits only."""
    simple = strip_accents(text).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", simple).split())


def html_to_text(html: str | None) -> str | None:
    """Strips tags, keeping paragraph breaks; returns None when empty."""
    if not html:
        return None
    tree = LexborHTMLParser(html)
    for node in tree.css("script, style"):
        node.decompose()
    body = tree.body or tree.root
    if body is None:
        return None
    text = body.text(separator="\n")
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_LINES.sub("\n\n", text).strip()
    return text or None
