"""Deterministic text normalization for cross-source job grouping.

These helpers are deliberately conservative and dependency-free (stdlib only).
Their purpose is to map two boards' slightly different spellings of the same
vacancy onto identical signatures while leaving genuinely different vacancies
distinct.
"""

from __future__ import annotations

import re
import unicodedata

# Legal-entity suffixes stripped from the trailing edge of a company name
# before grouping. This tuple is DATA-DRIVEN by design: add any locale's
# suffix here (e.g. "Oy", "S.A. de C.V.", "K.K.", "D.O.O.") and it is picked
# up by ``normalize_company`` without any code changes. Entries are kept in
# their human-readable form; normalization handles casing and punctuation, so
# "GmbH", "B.V." and "S.A." all collapse onto the intended entry.
COMPANY_LEGAL_SUFFIXES: tuple[str, ...] = (
    # English / US / UK / Commonwealth
    "Inc",
    "Incorporated",
    "Corp",
    "Corporation",
    "Co",
    "Company",
    "LLC",
    "L.L.C.",
    "Ltd",
    "Limited",
    "LLP",
    "PLLC",
    "PLC",
    "P.L.C.",
    "Pty",
    "Pte",
    "L.P.",
    "LP",
    # German / Austrian / Swiss
    "GmbH",
    "GmbH & Co KG",
    "AG",
    "A.G.",
    "KG",
    "KGaA",
    "UG",
    "e.K.",
    "e.G.",
    # Benelux
    "BV",
    "B.V.",
    "NV",
    "N.V.",
    # Nordic
    "Oy",
    "AB",
    "A.B.",
    "AS",
    "A/S",
    "ApS",
    "Kb",
    "HB",
    # France / Monaco
    "SA",
    "S.A.",
    "SAS",
    "SARL",
    "S.A.R.L.",
    "EURL",
    "SNC",
    # Italy / San Marino
    "S.r.l.",
    "SRL",
    "S.p.A.",
    "SPA",
    # Spain / Portugal / Latin America
    "SL",
    "S.L.",
    "S.A. de C.V.",
    "SA de CV",
    "LTDA",
    "Ltda.",
    # Japan / Asia
    "K.K.",
    "KK",
    "Kabushiki Kaisha",
    "\u682a\u5f0f\u4f1a\u793e",
    "\u6709\u9650\u4f1a\u793e",
    "JSC",
    "Sdn",
    "Bhd",
    "Sdn. Bhd.",
    # Eastern Europe
    "OOO",
    "O.O.O.",
    "ZAO",
    "AO",
    "A.O.",
    "D.O.O.",
    "doo",
    "Kft",
    "Zrt",
)

# Tokens that make a location "generic" (remote / anywhere). A location that
# normalizes to only these tokens carries no geographic signal and therefore
# never triggers a duplicate-detection split.
_GENERIC_LOCATION_TERMS: frozenset[str] = frozenset(
    {
        "anywhere",
        "world",
        "worldwide",
        "global",
        "remote",
        "home",
        "any",
        "all",
        "the",
        "in",
        "of",
        "on",
        "office",
        "work",
        "from",
        "based",
    }
)

_PUNCTUATION = re.compile(r"[\W_]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    """Lowercase, decompose accents, and collapse punctuation to spaces."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value).lower()
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("&", " and ")
    text = re.sub(r"\band\b", " ", text)
    text = _PUNCTUATION.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text


_COMPANY_SUFFIX_SET: frozenset[str] = frozenset(
    normalize_text(suffix) for suffix in COMPANY_LEGAL_SUFFIXES
)
_MAX_SUFFIX_TOKENS = max(
    (len(normalize_text(suffix).split()) for suffix in COMPANY_LEGAL_SUFFIXES),
    default=1,
)


def normalize_company(value: str | None) -> str:
    """Normalize a company name, stripping legal-entity suffixes and "The"."""
    text = normalize_text(value)
    if not text:
        return ""
    if text.startswith("the "):
        text = text[len("the ") :]
    tokens = text.split()
    while tokens:
        removed = False
        for length in range(min(len(tokens), _MAX_SUFFIX_TOKENS), 0, -1):
            candidate = " ".join(tokens[-length:])
            if candidate in _COMPANY_SUFFIX_SET:
                del tokens[-length:]
                removed = True
                break
        if not removed:
            break
    return " ".join(tokens)


def normalize_title(value: str | None) -> str:
    """Normalize a job title (casing/punctuation only; seniority is kept)."""
    return normalize_text(value)


def normalize_location(value: str | None) -> str:
    """Normalize a location, dropping generic remote/global filler tokens."""
    text = normalize_text(value)
    if not text:
        return ""
    tokens = [token for token in text.split() if token not in _GENERIC_LOCATION_TERMS]
    return " ".join(tokens)


def remote_class(status: str | None) -> str | None:
    """Map a stored remote status onto a coarse grouping class.

    Only the explicit values produced by the collectors are recognized;
    anything else (including NULL) is treated as "unspecified".
    """
    if status == "remote":
        return "remote"
    if status == "onsite_or_unspecified":
        return "onsite"
    return None
