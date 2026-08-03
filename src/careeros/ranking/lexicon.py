"""Data-driven technical-skill lexicon and boundary-safe term matching.

The lexicon maps common spellings of a technology onto a small set of
regular-expression patterns that are anchored to sensible boundaries, so a
profile term like "C" never matches the "c" inside "C++", "C#", or ordinary
words. It is deliberately small: profile terms that are not in the lexicon are
matched by the generic token/phrase matcher from ``careeros.text``.

The data lives in :data:`SKILL_LEXICON`. Add a technology by appending a
:class:`LexiconEntry`; its patterns are compiled once at import time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from careeros.text import has_phrase, prepare_text


@dataclass(frozen=True)
class LexiconEntry:
    """A technology with profile-side spelling variants and match patterns."""

    canonical: str
    variants: tuple[str, ...]
    patterns: tuple[str, ...]


SKILL_LEXICON: tuple[LexiconEntry, ...] = (
    LexiconEntry(
        canonical="C",
        variants=("c",),
        patterns=(
            # Standalone "c"; not the "c" inside "c++", "c#", "c/c++", or words.
            r"(?<![a-z0-9])c(?![a-z0-9+#/])",
        ),
    ),
    LexiconEntry(
        canonical="C++",
        variants=("c++", "cpp"),
        patterns=(
            r"(?<![a-z0-9])c\+\+(?![a-z0-9])",
            r"(?<![a-z0-9])cpp(?![a-z0-9])",
        ),
    ),
    LexiconEntry(
        canonical="C#",
        variants=("c#", "csharp"),
        patterns=(
            r"(?<![a-z0-9])c#(?![a-z0-9])",
            r"(?<![a-z0-9])csharp(?![a-z0-9])",
        ),
    ),
    LexiconEntry(
        canonical=".NET",
        variants=(".net", "dotnet"),
        patterns=(
            r"(?<![a-z0-9])\.net(?![a-z0-9])",
            r"(?<![a-z0-9])dotnet(?![a-z0-9])",
        ),
    ),
    LexiconEntry(
        canonical="Node.js",
        variants=("node", "node.js", "nodejs"),
        patterns=(
            r"\bnode(?:\.js)?\b",
            r"\bnodejs\b",
        ),
    ),
    LexiconEntry(
        canonical="JavaScript",
        variants=("javascript", "js"),
        patterns=(
            r"\bjavascript\b",
            # "js" only as a standalone token: never inside "nodejs" or "json".
            r"\bjs\b",
        ),
    ),
    LexiconEntry(
        canonical="TypeScript",
        variants=("typescript", "ts"),
        patterns=(
            r"\btypescript\b",
            r"\bts\b",
        ),
    ),
    LexiconEntry(
        canonical="React",
        variants=("react", "react.js", "reactjs"),
        patterns=(
            r"\breact\b",
            r"\breactjs\b",
        ),
    ),
    LexiconEntry(
        canonical="CI/CD",
        variants=("ci/cd", "cicd"),
        patterns=(
            r"\bci\s*/\s*cd\b",
            r"\bcicd\b",
        ),
    ),
    LexiconEntry(
        canonical="AWS",
        variants=("aws", "amazon web services"),
        patterns=(
            r"\baws\b",
            r"\bamazon\s+web\s+services\b",
        ),
    ),
    LexiconEntry(
        canonical="PostgreSQL",
        variants=("postgresql", "postgres", "psql"),
        patterns=(
            r"\bpostgresql\b",
            r"\bpostgres\b",
            r"\bpsql\b",
        ),
    ),
    LexiconEntry(
        canonical="SQL",
        variants=("sql",),
        patterns=(
            # Standalone "sql"; does not match "sqlite" or "postgresql".
            r"\bsql\b",
        ),
    ),
    LexiconEntry(
        canonical="FastAPI",
        variants=("fastapi", "fast api"),
        patterns=(
            r"\bfastapi\b",
            r"\bfast\s+api\b",
        ),
    ),
)

# Map every profile-side variant to its entry so the matcher can expand a
# user's spelling ("postgres") into the entry's full pattern set.
_LOWER_TO_ENTRY: dict[str, LexiconEntry] = {
    variant: entry for entry in SKILL_LEXICON for variant in entry.variants
}

_COMPILED_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    entry.canonical: tuple(re.compile(pattern) for pattern in entry.patterns)
    for entry in SKILL_LEXICON
}


def term_matches(term: str, text: str) -> bool:
    """Return True when ``term`` appears in ``text`` with sensible boundaries.

    Lexicon terms use boundary-safe patterns; unknown terms fall back to the
    generic token/phrase matcher. Matching is case-insensitive and accent-folded.
    """
    entry = _LOWER_TO_ENTRY.get(term.strip().lower())
    if entry is not None:
        prepared = prepare_text(text)
        compiled = _COMPILED_PATTERNS[entry.canonical]
        return any(pattern.search(prepared) for pattern in compiled)
    return has_phrase(text, term)
