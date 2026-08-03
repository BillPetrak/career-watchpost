"""Shared text normalization helpers for cleaning and matching.

These helpers are deliberately dependency-free (stdlib only) and deterministic.
They are used both by the CLI renderer and by the scoring engine so that the
two never drift apart in how job text is cleaned or how phrases are matched.
"""

from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser


class _DescriptionCleaner(HTMLParser):
    """Strips HTML tags, preserves paragraph/list boundaries, and unescapes entities."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._parts: list[str] = []
        self._pending_newline = False

    def _emit_newline(self) -> None:
        self._pending_newline = True

    def _flush_newline(self) -> None:
        if self._pending_newline:
            if not self._parts or self._parts[-1] != "\n":
                self._parts.append("\n")
            self._pending_newline = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._emit_newline()

    def handle_endtag(self, tag: str) -> None:
        if tag in {
            "p",
            "div",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "tr",
            "td",
            "th",
            "ul",
            "ol",
        }:
            self._emit_newline()

    def handle_data(self, data: str) -> None:
        self._flush_newline()
        cleaned = " ".join(data.split())
        if cleaned:
            if self._parts and self._parts[-1] != "\n":
                self._parts.append(" ")
            self._parts.append(cleaned)

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"\n+", "\n", text)
        return text.strip()


def clean_description(html_text: str) -> str:
    """Strip HTML tags, preserve paragraph breaks, and unescape entities."""
    if not html_text:
        return ""
    text = html.unescape(html_text)
    cleaner = _DescriptionCleaner()
    cleaner.feed(text)
    return cleaner.get_text()


def word_tokens(text: str) -> list[str]:
    """Split text into lowercase word tokens, folding accents and punctuation."""
    decomposed = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return re.findall(r"[a-z0-9]+", folded.lower())


def has_phrase(text: str, phrase: str) -> bool:
    """Check if phrase appears as a contiguous token subsequence in text."""
    text_tokens = word_tokens(text)
    phrase_tokens = word_tokens(phrase)
    if not phrase_tokens:
        return False
    window_len = len(phrase_tokens)
    for i in range(len(text_tokens) - window_len + 1):
        if text_tokens[i : i + window_len] == phrase_tokens:
            return True
    return False


def prepare_text(value: str) -> str:
    """Normalize text for lexicon matching: lowercase, fold accents, collapse spaces.

    Punctuation that is meaningful to technical terms (``+``, ``#``, ``.``,
    ``/``) is preserved so that patterns such as ``c++``, ``c#``, ``.net`` and
    ``ci/cd`` remain matchable.
    """
    text = unicodedata.normalize("NFKD", value).lower()
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"\s+", " ", text).strip()
    return text
