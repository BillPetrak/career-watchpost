from __future__ import annotations

from pathlib import Path

import pytest

from careeros.source_config import (
    MAX_BOARDS,
    GreenhouseConfig,
    LeverConfig,
    SourceConfig,
    SourceConfigError,
    WorkableConfig,
    load_source_config,
)
from careeros.sources import BUILT_IN_SOURCES, resolve_source_catalog


def _write(path: Path, body: str) -> Path:
    path.write_text("version: 1\nsources:\n" + body, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_workable_config_and_old_positional_forms(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "sources.yaml",
        "  workable:\n"
        "    enabled: true\n"
        "    boards:\n"
        "      - subdomain: ' example-account '\n",
    )
    parsed = load_source_config(path)
    assert parsed.workable.present and parsed.workable.enabled
    assert parsed.workable.boards[0].subdomain == "example-account"
    assert SourceConfig(1, LeverConfig(False, False, ())).workable == WorkableConfig(
        False, False, ()
    )
    assert (
        SourceConfig(
            1,
            LeverConfig(False, False, ()),
            None,
            False,
            GreenhouseConfig(False, False, ()),
        ).workable.present
        is False
    )


@pytest.mark.parametrize(
    "value",
    ["", "ABC", "a_b", "-a", "a" * 101, "équipe", "a/b"],
)
def test_workable_subdomain_policy_rejects_invalid(tmp_path: Path, value: str) -> None:
    with pytest.raises(SourceConfigError):
        load_source_config(
            _write(
                tmp_path / "bad.yaml",
                "  workable:\n"
                "    enabled: true\n"
                "    boards:\n"
                f"      - subdomain: {value!r}\n",
            )
        )


def test_workable_schema_limits_duplicates_and_disabled_forms(tmp_path: Path) -> None:
    boards = "\n".join(f"      - subdomain: board-{i}" for i in range(MAX_BOARDS))
    parsed = load_source_config(
        _write(
            tmp_path / "many.yaml",
            f"  workable:\n    enabled: true\n    boards:\n{boards}\n",
        )
    )
    assert len(parsed.workable.boards) == MAX_BOARDS
    too_many = "\n".join(f"      - subdomain: board-{i}" for i in range(MAX_BOARDS + 1))
    with pytest.raises(SourceConfigError):
        load_source_config(
            _write(
                tmp_path / "too-many.yaml",
                f"  workable:\n    enabled: true\n    boards:\n{too_many}\n",
            )
        )
    disabled = load_source_config(
        _write(tmp_path / "disabled.yaml", "  workable:\n    enabled: false\n")
    )
    assert disabled.workable.present and not disabled.workable.enabled
    duplicate = _write(
        tmp_path / "duplicate.yaml",
        "  workable:\n"
        "    enabled: false\n"
        "    boards:\n"
        "      - subdomain: acme\n"
        "      - subdomain: ' acme '\n",
    )
    with pytest.raises(SourceConfigError, match="duplicate"):
        load_source_config(duplicate)


@pytest.mark.parametrize(
    "body",
    [
        "  workable: {enabled: true}\n",
        "  workable: {enabled: 'yes', boards: []}\n",
        "  workable: {enabled: true, boards: [{subdomain: acme, extra: 1}]}\n",
        "  workable: {enabled: true, boards: [{subdomain: 1}]}\n",
        "  workable: {enabled: true, boards: [{subdomain: acme}], extra: 1}\n",
    ],
)
def test_workable_exact_schema(tmp_path: Path, body: str) -> None:
    with pytest.raises(SourceConfigError):
        load_source_config(_write(tmp_path / "bad.yaml", body))


def test_provider_compatibility_and_fixed_runtime_order(tmp_path: Path) -> None:
    all_sources = _write(
        tmp_path / "all.yaml",
        "  workable:\n"
        "    enabled: true\n"
        "    boards: [{subdomain: workable}]\n"
        "  greenhouse:\n"
        "    enabled: true\n"
        "    boards: [{token: greenhouse}]\n"
        "  lever:\n"
        "    enabled: true\n"
        "    boards: [{id: lever, instance: global, company: Lever}]\n",
    )
    parsed = load_source_config(all_sources)
    catalog = resolve_source_catalog(config=parsed)
    assert catalog.active == (*BUILT_IN_SOURCES, "lever", "greenhouse", "workable")
    assert (
        load_source_config(
            _write(
                tmp_path / "lever.yaml",
                "  lever:\n"
                "    enabled: true\n"
                "    boards: [{id: lever, instance: global, company: Lever}]\n",
            )
        ).workable.present
        is False
    )
    assert (
        load_source_config(
            _write(
                tmp_path / "greenhouse.yaml",
                "  greenhouse:\n    enabled: true\n    boards: [{token: greenhouse}]\n",
            )
        ).workable.present
        is False
    )
    empty = load_source_config(_write(tmp_path / "empty.yaml", "  {}\n"))
    assert not empty.lever.present and not empty.greenhouse.present
    assert not empty.workable.present


def test_workable_subdomain_boundaries_and_disabled_retained_boards(
    tmp_path: Path,
) -> None:
    for index, subdomain in enumerate(("a", "a" * 100)):
        parsed = load_source_config(
            _write(
                tmp_path / f"boundary-{index}.yaml",
                "  workable:\n"
                "    enabled: true\n"
                "    boards:\n"
                f"      - subdomain: {subdomain}\n",
            )
        )
        assert parsed.workable.boards[0].subdomain == subdomain
    retained = load_source_config(
        _write(
            tmp_path / "retained.yaml",
            "  workable:\n    enabled: false\n    boards: [{subdomain: retained}]\n",
        )
    )
    assert retained.workable.boards[0].subdomain == "retained"
    assert (
        load_source_config(
            _write(
                tmp_path / "empty-disabled.yaml",
                "  workable:\n    enabled: false\n    boards: []\n",
            )
        ).workable.boards
        == ()
    )


@pytest.mark.parametrize(
    "body",
    [
        "  workable: {enabled: true, boards: 'acme'}\n",
        "  workable: {enabled: true, boards: {subdomain: acme}}\n",
        "  workable: {enabled: true, boards: [{subdomain: []}]}\n",
        "  workable: {enabled: true, boards: [{subdomain: true}]}\n",
        "  workable: {enabled: true, boards: [{subdomain: acme, host: x}]}\n",
    ],
)
def test_workable_board_types_and_exact_keys(tmp_path: Path, body: str) -> None:
    with pytest.raises(SourceConfigError):
        load_source_config(_write(tmp_path / "bad-types.yaml", body))
