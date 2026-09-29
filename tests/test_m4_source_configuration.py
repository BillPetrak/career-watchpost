from pathlib import Path

import pytest

from careeros.source_config import (
    MAX_BOARDS,
    GreenhouseConfig,
    LeverConfig,
    SmartRecruitersConfig,
    SourceConfig,
    SourceConfigError,
    WorkableConfig,
    load_source_config,
)


def _write(path: Path, body: str) -> Path:
    path.write_text("version: 1\nsources:\n" + body, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_smartrecruiters_config_and_old_positional_form(tmp_path: Path) -> None:
    parsed = load_source_config(
        _write(
            tmp_path / "sources.yaml",
            "  smartrecruiters:\n"
            "    enabled: true\n"
            "    companies:\n"
            "      - identifier: ' example-company '\n",
        )
    )
    assert parsed.smartrecruiters.present and parsed.smartrecruiters.enabled
    assert parsed.smartrecruiters.companies[0].identifier == "example-company"
    assert SourceConfig(1, LeverConfig(False, False, ())).smartrecruiters == (
        SmartRecruitersConfig(False, False, ())
    )


def test_all_configurable_sources_and_omitted_provider_remain_valid(
    tmp_path: Path,
) -> None:
    parsed = load_source_config(
        _write(
            tmp_path / "all.yaml",
            "  smartrecruiters:\n"
            "    enabled: true\n"
            "    companies: [{identifier: smartrecruiters}]\n"
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
    )
    assert parsed.lever.present and parsed.greenhouse.present
    assert parsed.workable.present and parsed.smartrecruiters.present
    omitted = load_source_config(_write(tmp_path / "omitted.yaml", "  {}\n"))
    assert omitted.smartrecruiters == SmartRecruitersConfig(False, False, ())
    assert SourceConfig(
        1,
        LeverConfig(False, False, ()),
        None,
        False,
        GreenhouseConfig(False, False, ()),
        WorkableConfig(False, False, ()),
    ).smartrecruiters == SmartRecruitersConfig(False, False, ())


@pytest.mark.parametrize("value", ["", "ABC", "a_b", "-a", "a" * 101, "équipe", "a/b"])
def test_smartrecruiters_identifier_policy(tmp_path: Path, value: str) -> None:
    with pytest.raises(SourceConfigError):
        load_source_config(
            _write(
                tmp_path / "bad.yaml",
                "  smartrecruiters:\n"
                "    enabled: true\n"
                f"    companies: [{{identifier: {value!r}}}]\n",
            )
        )


def test_smartrecruiters_identifier_one_and_hundred_boundaries(tmp_path: Path) -> None:
    for index, identifier in enumerate(("a", "a" * 100)):
        parsed = load_source_config(
            _write(
                tmp_path / f"boundary-{index}.yaml",
                "  smartrecruiters:\n"
                "    enabled: true\n"
                f"    companies: [{{identifier: {identifier}}}]\n",
            )
        )
        assert parsed.smartrecruiters.companies[0].identifier == identifier


def test_smartrecruiters_identifier_101_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SourceConfigError):
        load_source_config(
            _write(
                tmp_path / "too-long.yaml",
                "  smartrecruiters:\n"
                "    enabled: true\n"
                f"    companies: [{{identifier: {'a' * 101}}}]\n",
            )
        )


def test_smartrecruiters_boundaries_disabled_and_duplicates(tmp_path: Path) -> None:
    companies = "\n".join(
        f"      - identifier: company-{index}" for index in range(MAX_BOARDS)
    )
    parsed = load_source_config(
        _write(
            tmp_path / "many.yaml",
            f"  smartrecruiters:\n    enabled: true\n    companies:\n{companies}\n",
        )
    )
    assert len(parsed.smartrecruiters.companies) == MAX_BOARDS
    with pytest.raises(SourceConfigError):
        load_source_config(
            _write(
                tmp_path / "too-many.yaml",
                "  smartrecruiters:\n    enabled: true\n    companies:\n"
                + "\n".join(
                    f"      - identifier: company-{index}"
                    for index in range(MAX_BOARDS + 1)
                )
                + "\n",
            )
        )
    disabled = load_source_config(
        _write(tmp_path / "disabled.yaml", "  smartrecruiters:\n    enabled: false\n")
    )
    assert disabled.smartrecruiters.present
    assert not disabled.smartrecruiters.enabled
    duplicate = _write(
        tmp_path / "duplicate.yaml",
        "  smartrecruiters:\n"
        "    enabled: false\n"
        "    companies:\n"
        "      - identifier: acme\n"
        "      - identifier: ' acme '\n",
    )
    with pytest.raises(SourceConfigError, match="duplicate"):
        load_source_config(duplicate)


def test_smartrecruiters_unknown_keys_and_enabled_empty_rejected(
    tmp_path: Path,
) -> None:
    for body in (
        "  smartrecruiters: {enabled: true}\n",
        "  smartrecruiters: {enabled: 'yes', companies: []}\n",
        (
            "  smartrecruiters: {enabled: true, "
            "companies: [{identifier: acme, extra: 1}]}\n"
        ),
        (
            "  smartrecruiters: {enabled: true, "
            "companies: [{identifier: acme}], extra: 1}\n"
        ),
    ):
        with pytest.raises(SourceConfigError):
            load_source_config(_write(tmp_path / "bad.yaml", body))


def test_smartrecruiters_config_keeps_secure_loader_mode_requirement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unsafe.yaml"
    path.write_text(
        "version: 1\nsources:\n  smartrecruiters:\n    enabled: false\n",
        encoding="utf-8",
    )
    path.chmod(0o644)
    with pytest.raises(SourceConfigError, match="mode 0600"):
        load_source_config(path)
