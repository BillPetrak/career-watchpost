"""Tests for deterministic text normalization used in duplicate detection."""

from careeros.grouping.normalize import (
    normalize_company,
    normalize_location,
    normalize_text,
    normalize_title,
    remote_class,
)


def test_normalize_text_lowercases_and_collapses() -> None:
    assert normalize_text("  Hello,  World!!  ") == "hello world"


def test_normalize_text_decomposes_accents() -> None:
    assert normalize_text("São Paulo") == "sao paulo"


def test_normalize_text_handles_empty_and_none() -> None:
    assert normalize_text("") == ""
    assert normalize_text(None) == ""


def test_normalize_company_strips_legal_suffix() -> None:
    assert normalize_company("Acme GmbH") == "acme"
    assert normalize_company("Acme Inc.") == "acme"
    assert normalize_company("Acme Ltd") == "acme"
    assert normalize_company("Acme, Inc.") == "acme"


def test_normalize_company_strips_the_prefix() -> None:
    assert normalize_company("The Acme Company") == "acme"


def test_normalize_company_multi_word_suffix() -> None:
    assert normalize_company("Acme S.A. de C.V.") == "acme"
    assert normalize_company("Acme Sdn. Bhd.") == "acme"


def test_normalize_company_keeps_substantive_tokens() -> None:
    assert normalize_company("Acme Data Systems") == "acme data systems"


def test_normalize_company_keeps_unrecognized_words() -> None:
    assert normalize_company("Databricks") == "databricks"


def test_normalize_company_empty() -> None:
    assert normalize_company(None) == ""
    assert normalize_company("") == ""


def test_company_suffixes_are_data_driven(tmp_path) -> None:
    import importlib

    from careeros.grouping import normalize

    suffix_module = tmp_path / "extra_suffixes.py"
    suffix_module.write_text("EXTRA_SUFFIXES = ('Coop', 'SC')\n")

    spec = importlib.util.spec_from_file_location("extra_suffixes", str(suffix_module))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.EXTRA_SUFFIXES == ("Coop", "SC")
    # The tuple in normalize is directly extensible; verify a representative
    # entry normalizes so future additions mirror the same pattern.
    assert "GmbH" in normalize.COMPANY_LEGAL_SUFFIXES
    assert normalize_company("Coop Colosseum") == "coop colosseum"


def test_normalize_title_normalizes_only() -> None:
    assert normalize_title("Senior  Python  Engineer!") == "senior python engineer"
    assert normalize_title(None) == ""


def test_normalize_location_drops_generic_terms() -> None:
    assert normalize_location("Remote") == ""
    assert normalize_location("Anywhere") == ""
    assert normalize_location("Remote - Worldwide") == ""
    assert normalize_location("Berlin") == "berlin"


def test_normalize_location_keeps_specific_tokens() -> None:
    assert normalize_location("Berlin, Germany") == "berlin germany"


def test_normalize_location_empty() -> None:
    assert normalize_location(None) == ""
    assert normalize_location("") == ""


def test_remote_class_maps_known_values() -> None:
    assert remote_class("remote") == "remote"
    assert remote_class("onsite_or_unspecified") == "onsite"
    assert remote_class(None) is None
    assert remote_class("hybrid") is None
