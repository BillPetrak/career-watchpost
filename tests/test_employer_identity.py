"""Offline identity and attribution tests for the M1 kernel."""

from __future__ import annotations

import pytest

from careeros.employers import (
    ATSProvider,
    EmployerCatalogRecord,
    EmployerFeedAttribution,
    EmployerId,
    EvidenceProvenance,
    EvidenceSource,
    EvidenceState,
    ExactFeedTarget,
    FeedIdentity,
    FeedLineageId,
    FeedSuccession,
    RelationshipKind,
    SuccessionState,
)

_EMPLOYER_A = EmployerId("00000000-0000-0000-0000-000000000001")
_EMPLOYER_B = EmployerId("00000000-0000-0000-0000-000000000002")
_LINEAGE_A = FeedLineageId("10000000-0000-0000-0000-000000000001")
_LINEAGE_B = FeedLineageId("10000000-0000-0000-0000-000000000002")
_PROVENANCE = EvidenceProvenance(EvidenceSource.OWNER, "owner-confirmation-1")


def _target(
    provider: ATSProvider = ATSProvider.LEVER,
    identifier: str = "acme",
    instance: str = "global",
) -> ExactFeedTarget:
    return ExactFeedTarget(provider, instance, identifier)


def test_exact_identical_feed_keys_compare_equal() -> None:
    assert _target() == _target()
    assert _target().key == ("lever", "global", "acme")


def test_providers_instances_and_targets_do_not_collide() -> None:
    targets = {
        _target(),
        _target(ATSProvider.GREENHOUSE),
        _target(identifier="other"),
        _target(instance="eu"),
    }
    assert len(targets) == 4


@pytest.mark.parametrize(
    "provider,instance",
    (
        (ATSProvider.LEVER, "global"),
        (ATSProvider.LEVER, "eu"),
        (ATSProvider.GREENHOUSE, "global"),
        (ATSProvider.WORKABLE, "global"),
        (ATSProvider.SMARTRECRUITERS, "global"),
    ),
)
def test_provider_specific_string_instances_are_preserved(
    provider: ATSProvider, instance: str
) -> None:
    assert ExactFeedTarget(provider, instance, "acme").instance == instance


def test_non_string_instance_equal_to_global_is_rejected() -> None:
    class GlobalImpostor:
        def __hash__(self) -> int:
            return hash("global")

        def __eq__(self, other: object) -> bool:
            return other == "global"

    with pytest.raises(TypeError):
        ExactFeedTarget(  # type: ignore[arg-type]
            ATSProvider.LEVER,
            GlobalImpostor(),
            "acme",
        )


def test_equal_names_do_not_merge_employer_identity() -> None:
    first = EmployerCatalogRecord(_EMPLOYER_A, "Same Company")
    second = EmployerCatalogRecord(_EMPLOYER_B, "Same Company")
    assert first.display_name == second.display_name
    assert first.employer_id != second.employer_id


def test_attribution_is_many_to_many() -> None:
    first_feed = _target(identifier="first")
    second_feed = _target(ATSProvider.GREENHOUSE, "second")
    relationships = (
        EmployerFeedAttribution(
            _EMPLOYER_A,
            first_feed,
            RelationshipKind.DIRECT,
            EvidenceState.CONFIRMED,
            _PROVENANCE,
        ),
        EmployerFeedAttribution(
            _EMPLOYER_A,
            second_feed,
            RelationshipKind.DIRECT,
            EvidenceState.CONFIRMED,
            _PROVENANCE,
        ),
        EmployerFeedAttribution(
            _EMPLOYER_B,
            second_feed,
            RelationshipKind.REPRESENTED,
            EvidenceState.CONFIRMED,
            _PROVENANCE,
        ),
    )
    assert {
        item.feed_target for item in relationships if item.employer_id == _EMPLOYER_A
    } == {
        first_feed,
        second_feed,
    }
    assert {
        item.employer_id for item in relationships if item.feed_target == second_feed
    } == {
        _EMPLOYER_A,
        _EMPLOYER_B,
    }


@pytest.mark.parametrize(
    "relationship", (RelationshipKind.OPERATOR, RelationshipKind.REPRESENTED)
)
def test_shared_relationships_never_imply_board_wide_attribution(
    relationship: RelationshipKind,
) -> None:
    attribution = EmployerFeedAttribution(
        _EMPLOYER_A,
        _target(),
        relationship,
        EvidenceState.CONFIRMED,
        _PROVENANCE,
    )
    assert not attribution.implies_board_wide_employer


def test_only_confirmed_direct_relationship_implies_board_wide_attribution() -> None:
    candidate = EmployerFeedAttribution(
        _EMPLOYER_A,
        _target(),
        RelationshipKind.DIRECT,
        EvidenceState.CANDIDATE,
        _PROVENANCE,
    )
    confirmed = EmployerFeedAttribution(
        _EMPLOYER_A,
        _target(),
        RelationshipKind.DIRECT,
        EvidenceState.CONFIRMED,
        _PROVENANCE,
    )
    assert not candidate.implies_board_wide_employer
    assert confirmed.implies_board_wide_employer


def test_target_identity_and_lineage_identity_are_independent() -> None:
    target = _target()
    assert FeedIdentity(target, _LINEAGE_A) != FeedIdentity(target, _LINEAGE_B)


def test_successor_target_remains_distinct() -> None:
    predecessor = _target(identifier="old-board")
    successor = _target(identifier="new-board")
    succession = FeedSuccession(
        predecessor,
        successor,
        SuccessionState.CONFIRMED,
        _PROVENANCE,
    )
    assert succession.predecessor != succession.successor


@pytest.mark.parametrize(
    "provider,instance,identifier",
    (
        (ATSProvider.LEVER, "us", "acme"),
        (ATSProvider.GREENHOUSE, "eu", "acme"),
        (ATSProvider.WORKABLE, "global", "Acme"),
        (ATSProvider.SMARTRECRUITERS, "global", "acme_inc"),
        (ATSProvider.LEVER, "global", "-acme"),
        (ATSProvider.LEVER, "global", "a" * 101),
    ),
)
def test_invalid_canonical_targets_fail_closed(
    provider: ATSProvider, instance: str, identifier: str
) -> None:
    with pytest.raises(ValueError):
        ExactFeedTarget(provider, instance, identifier)


def test_unsupported_provider_including_ashby_fails_closed() -> None:
    with pytest.raises(ValueError):
        ATSProvider("ashby")
    with pytest.raises(TypeError):
        ExactFeedTarget("ashby", "global", "acme")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "identity_type",
    (EmployerId, FeedLineageId),
)
def test_opaque_identities_require_canonical_uuids(identity_type: type[object]) -> None:
    with pytest.raises(ValueError):
        identity_type("Acme")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        identity_type("00000000000000000000000000000001")  # type: ignore[call-arg]


def test_succession_cannot_be_created_from_identical_targets() -> None:
    with pytest.raises(ValueError):
        FeedSuccession(
            _target(),
            _target(),
            SuccessionState.CANDIDATE,
            _PROVENANCE,
        )
