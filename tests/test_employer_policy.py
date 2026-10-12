"""Pure owner-policy, succession, eligibility, and cardinality tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from careeros.collectors import COLLECTORS
from careeros.employers import (
    ATSProvider,
    AuthoritySuppression,
    CollectionAuthority,
    EmployerCatalogRecord,
    EmployerCatalogState,
    EmployerFeedAttribution,
    EmployerId,
    EmployerLifecycle,
    EvidenceProvenance,
    EvidenceSource,
    EvidenceState,
    ExactFeedTarget,
    FeedLifecycle,
    FeedLifecycleMetadata,
    FeedPolicyDecision,
    FeedPolicyInput,
    FeedSuccession,
    InheritedOwnerVeto,
    JobEligibilityInput,
    JobEmployerAttribution,
    MonitoringState,
    PermissionState,
    PolicyReason,
    PriorityClass,
    ProviderPermissions,
    RelationshipKind,
    RuntimeConstraints,
    SafetyState,
    SuccessionState,
    SuccessorInheritance,
    SuppressionKind,
    VerificationState,
    VisibilityState,
    active_visible_catalog_page,
    eligible_group_members,
    evaluate_succession,
    job_is_default_eligible,
    resolve_feed_policy,
)
from careeros.source_config import MAX_BOARDS
from careeros.sources import BUILT_IN_SOURCES, CONFIGURABLE_SOURCES, KNOWN_SOURCES

_EMPLOYER_A = EmployerId("00000000-0000-0000-0000-000000000001")
_EMPLOYER_B = EmployerId("00000000-0000-0000-0000-000000000002")
_TARGET = ExactFeedTarget(ATSProvider.LEVER, "global", "acme")
_PROVENANCE = EvidenceProvenance(EvidenceSource.OWNER, "owner-policy-1")
_PROVENANCE_2 = EvidenceProvenance(EvidenceSource.PROVIDER, "provider-metadata-2")
_ALLOWED = ProviderPermissions(
    yaml=PermissionState.ALLOWED,
    monitoring=PermissionState.ALLOWED,
    catalog=PermissionState.ALLOWED,
)
_VERIFIED = FeedLifecycleMetadata(verification=VerificationState.VERIFIED)


def _policy(**overrides: object):
    values: dict[str, object] = {
        "feed_target": _TARGET,
        "safety": SafetyState.PASSED,
        "permissions": _ALLOWED,
        "metadata": _VERIFIED,
    }
    values.update(overrides)
    return FeedPolicyInput(**values)  # type: ignore[arg-type]


def _attribution(
    employer: EmployerId = _EMPLOYER_A,
    *,
    relationship: RelationshipKind = RelationshipKind.DIRECT,
    evidence: EvidenceState = EvidenceState.CONFIRMED,
    provenance: EvidenceProvenance = _PROVENANCE,
) -> EmployerFeedAttribution:
    return EmployerFeedAttribution(
        employer, _TARGET, relationship, evidence, provenance
    )


def test_hidden_eligible_feed_remains_collection_eligible() -> None:
    decision = resolve_feed_policy(
        _policy(
            monitoring=MonitoringState.ACTIVE,
            visibility=VisibilityState.HIDDEN,
        )
    )
    assert decision.collection_eligible
    assert decision.effective_authorities == {CollectionAuthority.MONITORING}
    assert not decision.presentation_visible
    assert PolicyReason.HIDDEN in decision.reason_codes


def test_excluded_employer_is_not_active_visible_but_is_restorable() -> None:
    record = EmployerCatalogRecord(_EMPLOYER_A, "Acme", excluded=True)
    assert record.catalog_state is EmployerCatalogState.EXCLUDED
    assert record.restorable
    assert active_visible_catalog_page((record,)).items == ()


def test_exclusion_suppresses_exclusively_dedicated_direct_feed() -> None:
    decision = resolve_feed_policy(
        _policy(
            monitoring=MonitoringState.ACTIVE,
            excluded_employers=frozenset({_EMPLOYER_A}),
            attributions=(_attribution(),),
        )
    )
    assert not decision.collection_eligible
    assert PolicyReason.EMPLOYER_EXCLUDED_DEDICATED_FEED in decision.reason_codes


def test_duplicate_confirmed_direct_evidence_remains_one_dedicated_relationship() -> (
    None
):
    decision = resolve_feed_policy(
        _policy(
            monitoring=MonitoringState.ACTIVE,
            excluded_employers=frozenset({_EMPLOYER_A}),
            attributions=(
                _attribution(),
                _attribution(provenance=_PROVENANCE_2),
            ),
        )
    )
    assert not decision.collection_eligible
    assert PolicyReason.EMPLOYER_EXCLUDED_DEDICATED_FEED in decision.reason_codes


def test_distinct_confirmed_employers_prevent_dedicated_treatment() -> None:
    decision = resolve_feed_policy(
        _policy(
            monitoring=MonitoringState.ACTIVE,
            excluded_employers=frozenset({_EMPLOYER_A}),
            attributions=(_attribution(), _attribution(_EMPLOYER_B)),
        )
    )
    assert decision.effective_authorities == {CollectionAuthority.MONITORING}


@pytest.mark.parametrize(
    "relationship", (RelationshipKind.OPERATOR, RelationshipKind.REPRESENTED)
)
def test_confirmed_shared_relationship_prevents_dedicated_treatment(
    relationship: RelationshipKind,
) -> None:
    decision = resolve_feed_policy(
        _policy(
            monitoring=MonitoringState.ACTIVE,
            excluded_employers=frozenset({_EMPLOYER_A}),
            attributions=(_attribution(relationship=relationship),),
        )
    )
    assert decision.effective_authorities == {CollectionAuthority.MONITORING}


@pytest.mark.parametrize(
    "relationship", (RelationshipKind.OPERATOR, RelationshipKind.REPRESENTED)
)
def test_direct_plus_shared_relationship_is_not_dedicated(
    relationship: RelationshipKind,
) -> None:
    decision = resolve_feed_policy(
        _policy(
            monitoring=MonitoringState.ACTIVE,
            excluded_employers=frozenset({_EMPLOYER_A}),
            attributions=(
                _attribution(),
                _attribution(relationship=relationship),
            ),
        )
    )
    assert decision.effective_authorities == {CollectionAuthority.MONITORING}


@pytest.mark.parametrize("evidence", (EvidenceState.CANDIDATE, EvidenceState.REJECTED))
def test_unconfirmed_evidence_does_not_establish_an_employer_relationship(
    evidence: EvidenceState,
) -> None:
    decision = resolve_feed_policy(
        _policy(
            monitoring=MonitoringState.ACTIVE,
            excluded_employers=frozenset({_EMPLOYER_A}),
            attributions=(_attribution(evidence=evidence),),
        )
    )
    assert decision.effective_authorities == {CollectionAuthority.MONITORING}


def test_shared_feed_remains_eligible_with_independent_authority() -> None:
    decision = resolve_feed_policy(
        _policy(
            monitoring=MonitoringState.ACTIVE,
            excluded_employers=frozenset({_EMPLOYER_A}),
            attributions=(
                _attribution(relationship=RelationshipKind.REPRESENTED),
                _attribution(_EMPLOYER_B, relationship=RelationshipKind.REPRESENTED),
            ),
        )
    )
    assert decision.collection_eligible
    assert CollectionAuthority.MONITORING in decision.effective_authorities


@pytest.mark.parametrize("veto", ("feed", "provider"))
def test_owner_feed_and_provider_vetoes_override_all_authorities(veto: str) -> None:
    decision = resolve_feed_policy(
        _policy(
            yaml_authorized=True,
            monitoring=MonitoringState.ACTIVE,
            catalog_consent=True,
            catalog_eligible=True,
            feed_blocked=veto == "feed",
            provider_disabled=veto == "provider",
        )
    )
    assert not decision.collection_eligible
    assert not decision.effective_authorities
    expected = (
        PolicyReason.FEED_BLOCKED if veto == "feed" else PolicyReason.PROVIDER_DISABLED
    )
    assert expected in decision.reason_codes


def test_monitoring_is_independent_of_global_catalog_consent() -> None:
    decision = resolve_feed_policy(
        _policy(monitoring=MonitoringState.ACTIVE, catalog_consent=False)
    )
    assert decision.effective_authorities == {CollectionAuthority.MONITORING}


def test_monitoring_pause_removes_only_monitoring_authority() -> None:
    decision = resolve_feed_policy(
        _policy(
            yaml_authorized=True,
            monitoring=MonitoringState.PAUSED,
            catalog_consent=True,
            catalog_eligible=True,
        )
    )
    assert decision.requested_authorities == {
        CollectionAuthority.YAML,
        CollectionAuthority.MONITORING,
        CollectionAuthority.CATALOG,
    }
    assert decision.effective_authorities == {
        CollectionAuthority.YAML,
        CollectionAuthority.CATALOG,
    }


def test_inherited_pause_preserves_yaml_and_catalog_authority() -> None:
    decision = resolve_feed_policy(
        _policy(
            yaml_authorized=True,
            monitoring=MonitoringState.ACTIVE,
            catalog_consent=True,
            catalog_eligible=True,
            inherited_vetoes=frozenset({InheritedOwnerVeto.MONITORING_PAUSE}),
        )
    )
    assert decision.effective_authorities == {
        CollectionAuthority.YAML,
        CollectionAuthority.CATALOG,
    }
    assert PolicyReason.INHERITED_MONITORING_PAUSE in decision.reason_codes


@pytest.mark.parametrize(
    "metadata,permissions,reason",
    (
        (
            FeedLifecycleMetadata(),
            _ALLOWED,
            PolicyReason.VERIFICATION_REQUIRED,
        ),
        (
            _VERIFIED,
            ProviderPermissions(catalog=PermissionState.UNKNOWN),
            PolicyReason.PERMISSION_UNKNOWN,
        ),
        (
            _VERIFIED,
            ProviderPermissions(catalog=PermissionState.DENIED),
            PolicyReason.PERMISSION_DENIED,
        ),
    ),
)
def test_catalog_consent_cannot_bypass_verification_or_permission(
    metadata: FeedLifecycleMetadata,
    permissions: ProviderPermissions,
    reason: PolicyReason,
) -> None:
    decision = resolve_feed_policy(
        _policy(
            catalog_consent=True,
            catalog_eligible=True,
            metadata=metadata,
            permissions=permissions,
        )
    )
    assert not decision.collection_eligible
    assert reason in decision.reason_codes


def test_legacy_yaml_compatibility_does_not_grant_new_modes() -> None:
    decision = resolve_feed_policy(
        _policy(
            yaml_authorized=True,
            monitoring=MonitoringState.ACTIVE,
            catalog_consent=True,
            catalog_eligible=True,
            permissions=ProviderPermissions(yaml=PermissionState.LEGACY_YAML),
        )
    )
    assert decision.effective_authorities == {CollectionAuthority.YAML}


def test_following_without_authority_only_changes_priority() -> None:
    decision = resolve_feed_policy(_policy(followed=True))
    assert not decision.collection_eligible
    assert decision.priority is PriorityClass.FOLLOWED


def test_runtime_constraint_suppresses_otherwise_effective_authority() -> None:
    decision = resolve_feed_policy(
        _policy(
            monitoring=MonitoringState.ACTIVE,
            runtime=RuntimeConstraints(False, "request budget unavailable"),
        )
    )
    assert not decision.collection_eligible
    assert PolicyReason.RUNTIME_SUPPRESSED in decision.reason_codes


@pytest.mark.parametrize(
    "field,value",
    (
        ("kind", "safety"),
        ("reason", "safety_failed"),
        ("authority", "yaml"),
    ),
)
def test_authority_suppression_rejects_string_enum_values(
    field: str, value: str
) -> None:
    values: dict[str, object] = {
        "kind": SuppressionKind.SAFETY,
        "reason": PolicyReason.SAFETY_FAILED,
        "authority": CollectionAuthority.YAML,
    }
    values[field] = value
    with pytest.raises(TypeError):
        AuthoritySuppression(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value",
    (
        ("requested_authorities", frozenset({"yaml"})),
        ("effective_authorities", frozenset({"yaml"})),
        ("priority", "normal"),
        ("reason_codes", ("yaml_requested",)),
    ),
)
def test_feed_policy_decision_rejects_string_enum_values(
    field: str, value: object
) -> None:
    decision = resolve_feed_policy(_policy(yaml_authorized=True))
    with pytest.raises(TypeError):
        replace(decision, **{field: value})


@pytest.mark.parametrize(
    "field,value",
    (
        ("requested_authorities", {CollectionAuthority.YAML}),
        ("effective_authorities", {CollectionAuthority.YAML}),
        ("safety_suppressions", []),
        ("permission_suppressions", []),
        ("owner_vetoes", []),
        ("state_suppressions", []),
        ("runtime_suppressions", []),
    ),
)
def test_feed_policy_decision_rejects_mutable_containers(
    field: str, value: object
) -> None:
    decision = resolve_feed_policy(_policy(yaml_authorized=True))
    with pytest.raises(TypeError):
        replace(decision, **{field: value})


@pytest.mark.parametrize(
    "field,value",
    (
        ("requested_authorities", frozenset({"authority"})),
        ("effective_authorities", frozenset({"authority"})),
        ("safety_suppressions", ("suppression",)),
        ("reason_codes", ("reason",)),
    ),
)
def test_feed_policy_decision_rejects_wrong_nested_element_types(
    field: str, value: object
) -> None:
    decision = resolve_feed_policy(_policy(yaml_authorized=True))
    with pytest.raises(TypeError):
        replace(decision, **{field: value})


@pytest.mark.parametrize("field", ("collection_eligible", "presentation_visible"))
def test_feed_policy_decision_rejects_string_booleans(field: str) -> None:
    decision = resolve_feed_policy(_policy(yaml_authorized=True))
    with pytest.raises(TypeError):
        replace(decision, **{field: "false"})


def test_feed_policy_decision_rejects_effective_authority_not_requested() -> None:
    decision = resolve_feed_policy(_policy(yaml_authorized=True))
    with pytest.raises(ValueError):
        replace(
            decision,
            effective_authorities=frozenset({CollectionAuthority.MONITORING}),
        )


def test_feed_policy_decision_rejects_incoherent_collection_eligibility() -> None:
    decision = resolve_feed_policy(_policy(yaml_authorized=True))
    with pytest.raises(ValueError):
        replace(decision, collection_eligible=False)


def test_resolver_output_remains_constructible_immutable_and_deterministic() -> None:
    inputs = _policy(
        yaml_authorized=True,
        monitoring=MonitoringState.PAUSED,
        catalog_consent=True,
        catalog_eligible=True,
    )
    first = resolve_feed_policy(inputs)
    second = resolve_feed_policy(inputs)
    reconstructed = FeedPolicyDecision(**vars(first))

    assert first == second == reconstructed
    assert first.effective_authorities == {
        CollectionAuthority.YAML,
        CollectionAuthority.CATALOG,
    }
    assert type(first.requested_authorities) is frozenset
    assert type(first.owner_vetoes) is tuple
    assert type(first.reason_codes) is tuple
    with pytest.raises(FrozenInstanceError):
        first.collection_eligible = False  # type: ignore[misc]


@pytest.mark.parametrize(
    "field,value",
    (
        ("catalog_consent", "disabled"),
        ("catalog_eligible", "false"),
        ("yaml_authorized", "false"),
        ("provider_disabled", "false"),
        ("feed_blocked", "false"),
        ("followed", "false"),
        ("quarantined", "false"),
    ),
)
def test_feed_policy_boolean_fields_reject_strings(field: str, value: str) -> None:
    with pytest.raises(TypeError):
        _policy(**{field: value})


def test_feed_policy_monitoring_rejects_string_enum_value() -> None:
    with pytest.raises(TypeError):
        _policy(monitoring="off")


def test_runtime_constraints_reject_string_boolean() -> None:
    with pytest.raises(TypeError):
        RuntimeConstraints(allowed="false")  # type: ignore[arg-type]


def test_runtime_constraints_validate_reason_type_and_consistency() -> None:
    with pytest.raises(TypeError):
        RuntimeConstraints(False, 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RuntimeConstraints(True, "unexpected")
    with pytest.raises(ValueError):
        RuntimeConstraints(False, " untrimmed ")


@pytest.mark.parametrize(
    "field,value",
    (
        ("lifecycle", "active"),
        ("verification", "verified"),
        ("health", "healthy"),
    ),
)
def test_feed_lifecycle_metadata_rejects_string_enum_values(
    field: str, value: str
) -> None:
    with pytest.raises(TypeError):
        FeedLifecycleMetadata(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value",
    (
        ("visibility", "hidden"),
        ("safety", "passed"),
        ("metadata", "verified"),
        ("permissions", "allowed"),
        ("runtime", "allowed"),
    ),
)
def test_feed_policy_typed_fields_reject_strings(field: str, value: str) -> None:
    with pytest.raises(TypeError):
        _policy(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    (
        ("attributions", []),
        ("excluded_employers", {_EMPLOYER_A}),
        ("inherited_vetoes", {InheritedOwnerVeto.FEED_BLOCK}),
    ),
)
def test_feed_policy_rejects_mutable_policy_containers(
    field: str, value: object
) -> None:
    with pytest.raises(TypeError):
        _policy(**{field: value})


def test_feed_policy_rejects_wrong_container_elements_and_target_mismatch() -> None:
    with pytest.raises(TypeError):
        _policy(attributions=("attribution",))
    with pytest.raises(TypeError):
        _policy(excluded_employers=frozenset({"employer"}))
    with pytest.raises(TypeError):
        _policy(inherited_vetoes=frozenset({"feed_block"}))
    other_target = ExactFeedTarget(ATSProvider.LEVER, "global", "other")
    with pytest.raises(ValueError):
        _policy(
            attributions=(
                EmployerFeedAttribution(
                    _EMPLOYER_A,
                    other_target,
                    RelationshipKind.DIRECT,
                    EvidenceState.CONFIRMED,
                    _PROVENANCE,
                ),
            )
        )


def test_unknown_safety_fails_closed() -> None:
    decision = resolve_feed_policy(
        _policy(monitoring=MonitoringState.ACTIVE, safety=SafetyState.UNKNOWN)
    )
    assert not decision.collection_eligible
    assert PolicyReason.SAFETY_UNKNOWN in decision.reason_codes


def test_retired_feed_is_not_collectible() -> None:
    decision = resolve_feed_policy(
        _policy(
            monitoring=MonitoringState.ACTIVE,
            metadata=replace(_VERIFIED, lifecycle=FeedLifecycle.RETIRED),
        )
    )
    assert not decision.collection_eligible
    assert PolicyReason.FEED_RETIRED in decision.reason_codes


def test_candidate_successor_inherits_no_positive_authority_and_is_quarantined() -> (
    None
):
    successor = ExactFeedTarget(ATSProvider.LEVER, "global", "acme-next")
    result = evaluate_succession(
        FeedSuccession(_TARGET, successor, SuccessionState.CANDIDATE, _PROVENANCE),
        predecessor_monitoring=MonitoringState.ACTIVE,
        predecessor_blocked=True,
    )
    assert result.target == successor
    assert result.requested_monitoring is MonitoringState.OFF
    assert result.inherited_vetoes == {InheritedOwnerVeto.FEED_BLOCK}
    assert result.quarantined


def test_quarantined_candidate_suppresses_fresh_catalog_authority() -> None:
    successor = ExactFeedTarget(ATSProvider.LEVER, "global", "acme-next")
    inheritance = evaluate_succession(
        FeedSuccession(_TARGET, successor, SuccessionState.CANDIDATE, _PROVENANCE),
        predecessor_monitoring=MonitoringState.PAUSED,
        predecessor_blocked=False,
    )
    decision = resolve_feed_policy(
        FeedPolicyInput(
            feed_target=successor,
            catalog_consent=True,
            catalog_eligible=True,
            monitoring=inheritance.requested_monitoring,
            quarantined=inheritance.quarantined,
            inherited_vetoes=inheritance.inherited_vetoes,
            safety=SafetyState.PASSED,
            metadata=_VERIFIED,
            permissions=_ALLOWED,
        )
    )
    assert inheritance.quarantined
    assert not decision.effective_authorities
    assert decision.state_suppressions == (
        AuthoritySuppression(
            SuppressionKind.STATE,
            PolicyReason.SUCCESSOR_QUARANTINED_CATALOG,
            CollectionAuthority.CATALOG,
        ),
    )


def test_independent_yaml_authority_survives_successor_quarantine() -> None:
    successor = ExactFeedTarget(ATSProvider.LEVER, "global", "acme-next")
    inheritance = evaluate_succession(
        FeedSuccession(_TARGET, successor, SuccessionState.CANDIDATE, _PROVENANCE),
        predecessor_monitoring=MonitoringState.PAUSED,
        predecessor_blocked=False,
    )
    decision = resolve_feed_policy(
        FeedPolicyInput(
            feed_target=successor,
            catalog_consent=True,
            catalog_eligible=True,
            monitoring=inheritance.requested_monitoring,
            yaml_authorized=True,
            quarantined=inheritance.quarantined,
            inherited_vetoes=inheritance.inherited_vetoes,
            safety=SafetyState.PASSED,
            metadata=_VERIFIED,
            permissions=_ALLOWED,
        )
    )
    assert decision.effective_authorities == {CollectionAuthority.YAML}


def test_confirmed_successor_pause_preserves_independent_yaml_and_catalog() -> None:
    successor = ExactFeedTarget(ATSProvider.LEVER, "global", "acme-next")
    inheritance = evaluate_succession(
        FeedSuccession(_TARGET, successor, SuccessionState.CONFIRMED, _PROVENANCE),
        predecessor_monitoring=MonitoringState.PAUSED,
        predecessor_blocked=False,
    )
    decision = resolve_feed_policy(
        FeedPolicyInput(
            feed_target=successor,
            catalog_consent=True,
            catalog_eligible=True,
            monitoring=inheritance.requested_monitoring,
            yaml_authorized=True,
            quarantined=inheritance.quarantined,
            inherited_vetoes=inheritance.inherited_vetoes,
            safety=SafetyState.PASSED,
            metadata=_VERIFIED,
            permissions=_ALLOWED,
        )
    )
    assert not inheritance.quarantined
    assert decision.effective_authorities == {
        CollectionAuthority.YAML,
        CollectionAuthority.CATALOG,
    }
    assert PolicyReason.INHERITED_MONITORING_PAUSE in decision.reason_codes


def test_candidate_successor_of_blocked_predecessor_has_no_authority() -> None:
    successor = ExactFeedTarget(ATSProvider.LEVER, "global", "acme-next")
    inheritance = evaluate_succession(
        FeedSuccession(_TARGET, successor, SuccessionState.CANDIDATE, _PROVENANCE),
        predecessor_monitoring=MonitoringState.ACTIVE,
        predecessor_blocked=True,
    )
    decision = resolve_feed_policy(
        FeedPolicyInput(
            feed_target=successor,
            catalog_consent=True,
            catalog_eligible=True,
            yaml_authorized=True,
            quarantined=inheritance.quarantined,
            inherited_vetoes=inheritance.inherited_vetoes,
            safety=SafetyState.PASSED,
            metadata=_VERIFIED,
            permissions=_ALLOWED,
        )
    )
    assert not decision.effective_authorities
    assert PolicyReason.INHERITED_FEED_BLOCK in decision.reason_codes


def test_candidate_successor_never_inherits_fresh_positive_evidence() -> None:
    successor = ExactFeedTarget(ATSProvider.LEVER, "global", "acme-next")
    inheritance = evaluate_succession(
        FeedSuccession(_TARGET, successor, SuccessionState.CANDIDATE, _PROVENANCE),
        predecessor_monitoring=MonitoringState.ACTIVE,
        predecessor_blocked=False,
    )
    assert inheritance.quarantined
    assert inheritance.verification is VerificationState.UNKNOWN
    assert inheritance.permissions == ProviderPermissions()


def test_quarantine_reasons_and_suppressions_are_deterministic() -> None:
    decision = resolve_feed_policy(
        _policy(
            monitoring=MonitoringState.ACTIVE,
            catalog_consent=True,
            catalog_eligible=True,
            quarantined=True,
        )
    )
    assert decision.state_suppressions == (
        AuthoritySuppression(
            SuppressionKind.STATE,
            PolicyReason.SUCCESSOR_QUARANTINED_MONITORING,
            CollectionAuthority.MONITORING,
        ),
        AuthoritySuppression(
            SuppressionKind.STATE,
            PolicyReason.SUCCESSOR_QUARANTINED_CATALOG,
            CollectionAuthority.CATALOG,
        ),
    )
    assert decision.reason_codes == (
        PolicyReason.MONITORING_REQUESTED,
        PolicyReason.CATALOG_REQUESTED,
        PolicyReason.SUCCESSOR_QUARANTINED_MONITORING,
        PolicyReason.SUCCESSOR_QUARANTINED_CATALOG,
    )


def test_confirmed_successor_inherits_intent_and_veto_but_not_fresh_evidence() -> None:
    successor = ExactFeedTarget(ATSProvider.LEVER, "eu", "acme-next")
    result = evaluate_succession(
        FeedSuccession(_TARGET, successor, SuccessionState.CONFIRMED, _PROVENANCE),
        predecessor_monitoring=MonitoringState.ACTIVE,
        predecessor_blocked=True,
    )
    assert result.requested_monitoring is MonitoringState.ACTIVE
    assert result.inherited_vetoes == {InheritedOwnerVeto.FEED_BLOCK}
    assert result.verification is VerificationState.UNKNOWN
    assert result.permissions == ProviderPermissions()
    assert not result.quarantined


def test_rejected_or_retired_succession_inherits_nothing() -> None:
    successor = ExactFeedTarget(ATSProvider.LEVER, "global", "acme-next")
    for state in (SuccessionState.REJECTED, SuccessionState.RETIRED):
        result = evaluate_succession(
            FeedSuccession(_TARGET, successor, state, _PROVENANCE),
            predecessor_monitoring=MonitoringState.PAUSED,
            predecessor_blocked=True,
        )
        assert result.requested_monitoring is MonitoringState.OFF
        assert not result.inherited_vetoes


def test_evaluate_succession_rejects_unvalidated_direct_arguments() -> None:
    succession = FeedSuccession(
        _TARGET,
        ExactFeedTarget(ATSProvider.LEVER, "global", "acme-next"),
        SuccessionState.CONFIRMED,
        _PROVENANCE,
    )
    with pytest.raises(TypeError):
        evaluate_succession(  # type: ignore[arg-type]
            "confirmed",
            predecessor_monitoring=MonitoringState.ACTIVE,
            predecessor_blocked=False,
        )
    with pytest.raises(TypeError):
        evaluate_succession(
            succession,
            predecessor_monitoring="active",  # type: ignore[arg-type]
            predecessor_blocked=False,
        )
    with pytest.raises(TypeError):
        evaluate_succession(
            succession,
            predecessor_monitoring=MonitoringState.ACTIVE,
            predecessor_blocked="false",  # type: ignore[arg-type]
        )


def test_feed_succession_rejects_wrong_target_state_and_provenance_types() -> None:
    successor = ExactFeedTarget(ATSProvider.LEVER, "global", "acme-next")
    with pytest.raises(TypeError):
        FeedSuccession(  # type: ignore[arg-type]
            "predecessor", successor, SuccessionState.CANDIDATE, _PROVENANCE
        )
    with pytest.raises(TypeError):
        FeedSuccession(  # type: ignore[arg-type]
            _TARGET, successor, "candidate", _PROVENANCE
        )
    with pytest.raises(TypeError):
        FeedSuccession(  # type: ignore[arg-type]
            _TARGET, successor, SuccessionState.CANDIDATE, "owner"
        )


def test_successor_inheritance_rejects_wrong_types_and_mutable_vetoes() -> None:
    with pytest.raises(TypeError):
        SuccessorInheritance(  # type: ignore[arg-type]
            target=_TARGET,
            requested_monitoring="off",
            inherited_vetoes=frozenset(),
            quarantined=False,
        )
    with pytest.raises(TypeError):
        SuccessorInheritance(  # type: ignore[arg-type]
            target=_TARGET,
            requested_monitoring=MonitoringState.OFF,
            inherited_vetoes=set(),
            quarantined=False,
        )
    with pytest.raises(TypeError):
        SuccessorInheritance(  # type: ignore[arg-type]
            target=_TARGET,
            requested_monitoring=MonitoringState.OFF,
            inherited_vetoes=frozenset(),
            quarantined="false",
        )
    with pytest.raises(TypeError):
        SuccessorInheritance(  # type: ignore[arg-type]
            target="target",
            requested_monitoring=MonitoringState.OFF,
            inherited_vetoes=frozenset(),
            quarantined=False,
        )
    with pytest.raises(TypeError):
        SuccessorInheritance(  # type: ignore[arg-type]
            target=_TARGET,
            requested_monitoring=MonitoringState.OFF,
            inherited_vetoes=frozenset(),
            quarantined=False,
            verification="unknown",
        )
    with pytest.raises(TypeError):
        SuccessorInheritance(  # type: ignore[arg-type]
            target=_TARGET,
            requested_monitoring=MonitoringState.OFF,
            inherited_vetoes=frozenset(),
            quarantined=False,
            permissions="unknown",
        )
    with pytest.raises(TypeError):
        SuccessorInheritance(  # type: ignore[arg-type]
            target=_TARGET,
            requested_monitoring=MonitoringState.OFF,
            inherited_vetoes=frozenset(),
            quarantined=False,
            health="unknown",
        )


def test_ambiguous_attribution_does_not_suppress_a_job() -> None:
    job = JobEligibilityInput(
        1, (JobEmployerAttribution(_EMPLOYER_A, EvidenceState.CANDIDATE),)
    )
    assert job_is_default_eligible(job, frozenset({_EMPLOYER_A}))


def test_mixed_group_exclusion_filters_only_the_excluded_job() -> None:
    excluded = JobEligibilityInput(
        1, (JobEmployerAttribution(_EMPLOYER_A, EvidenceState.CONFIRMED),)
    )
    eligible = JobEligibilityInput(
        2, (JobEmployerAttribution(_EMPLOYER_B, EvidenceState.CONFIRMED),)
    )
    assert eligible_group_members((excluded, eligible), frozenset({_EMPLOYER_A})) == (
        eligible,
    )


@pytest.mark.parametrize("excluded", (set(), [], ""))
def test_eligibility_helpers_reject_malformed_exclusion_collections(
    excluded: object,
) -> None:
    job = JobEligibilityInput(1)
    with pytest.raises(TypeError):
        job_is_default_eligible(job, excluded)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        eligible_group_members((), excluded)  # type: ignore[arg-type]


def test_eligibility_helpers_reject_malformed_job_containers() -> None:
    with pytest.raises(TypeError):
        job_is_default_eligible("job", frozenset())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        eligible_group_members([], frozenset())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        eligible_group_members(("job",), frozenset())  # type: ignore[arg-type]


def test_job_policy_models_reject_wrong_identity_and_mutable_attributions() -> None:
    with pytest.raises(TypeError):
        JobEmployerAttribution("employer", EvidenceState.CONFIRMED)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        JobEmployerAttribution(_EMPLOYER_A, "confirmed")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        JobEligibilityInput(True)
    with pytest.raises(ValueError):
        JobEligibilityInput(0)
    with pytest.raises(TypeError):
        JobEligibilityInput(1, [])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        JobEligibilityInput(1, ("attribution",))  # type: ignore[arg-type]


def test_catalog_is_uncapped_and_nonactive_states_do_not_consume_page_slots() -> None:
    active = tuple(
        EmployerCatalogRecord(
            EmployerId(f"00000000-0000-0000-0000-{index:012d}"),
            f"Employer {index}",
        )
        for index in range(1, 33)
    )
    nonactive = (
        EmployerCatalogRecord(
            EmployerId("10000000-0000-0000-0000-000000000001"),
            "Hidden",
            visibility=VisibilityState.HIDDEN,
        ),
        EmployerCatalogRecord(
            EmployerId("10000000-0000-0000-0000-000000000002"),
            "Excluded",
            excluded=True,
        ),
        EmployerCatalogRecord(
            EmployerId("10000000-0000-0000-0000-000000000003"),
            "Inactive",
            lifecycle=EmployerLifecycle.INACTIVE,
        ),
        EmployerCatalogRecord(
            EmployerId("10000000-0000-0000-0000-000000000004"),
            "Retired",
            lifecycle=EmployerLifecycle.RETIRED,
        ),
    )
    page = active_visible_catalog_page(nonactive + active, offset=8, limit=8)
    assert page.total_active_visible == 32
    assert len(page.items) == 8
    assert all(
        item.catalog_state is EmployerCatalogState.ACTIVE_VISIBLE for item in page.items
    )
    assert (
        active_visible_catalog_page(nonactive + active, offset=24, limit=20).items
        == active[24:]
    )


def test_catalog_page_rejects_malformed_record_containers() -> None:
    record = EmployerCatalogRecord(_EMPLOYER_A, "Acme")
    with pytest.raises(TypeError):
        active_visible_catalog_page([record])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        active_visible_catalog_page(("record",))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value",
    (
        ("employer_id", "employer"),
        ("lifecycle", "active"),
        ("catalog_eligible", "false"),
        ("visibility", "visible"),
        ("excluded", "false"),
    ),
)
def test_catalog_records_reject_wrong_policy_field_types(
    field: str, value: str
) -> None:
    values: dict[str, object] = {
        "employer_id": _EMPLOYER_A,
        "display_name": "Acme",
    }
    values[field] = value
    with pytest.raises(TypeError):
        EmployerCatalogRecord(**values)  # type: ignore[arg-type]


def test_valid_policy_models_retain_immutable_containers() -> None:
    policy = _policy(
        excluded_employers=frozenset({_EMPLOYER_A}),
        attributions=(_attribution(),),
        inherited_vetoes=frozenset({InheritedOwnerVeto.MONITORING_PAUSE}),
    )
    inheritance = SuccessorInheritance(
        target=_TARGET,
        requested_monitoring=MonitoringState.OFF,
        inherited_vetoes=frozenset(),
        quarantined=False,
    )
    job = JobEligibilityInput(
        1, (JobEmployerAttribution(_EMPLOYER_A, EvidenceState.CONFIRMED),)
    )
    assert type(policy.attributions) is tuple
    assert type(policy.excluded_employers) is frozenset
    assert type(policy.inherited_vetoes) is frozenset
    assert type(inheritance.inherited_vetoes) is frozenset
    assert type(job.attributions) is tuple


def test_released_source_and_yaml_compatibility_contracts_are_unchanged() -> None:
    assert (
        tuple(COLLECTORS)
        == BUILT_IN_SOURCES
        == (
            "arbeitnow",
            "remoteok",
            "weworkremotely",
            "jobicy",
            "remotive",
            "workingnomads",
            "himalayas",
        )
    )
    assert CONFIGURABLE_SOURCES == (
        "lever",
        "greenhouse",
        "workable",
        "smartrecruiters",
    )
    assert KNOWN_SOURCES == (*BUILT_IN_SOURCES, *CONFIGURABLE_SOURCES)
    assert MAX_BOARDS == 8


@pytest.mark.parametrize(
    "provider,identifier",
    (
        (ATSProvider.LEVER, "lever-board"),
        (ATSProvider.GREENHOUSE, "greenhouse-board"),
        (ATSProvider.WORKABLE, "workable-account"),
        (ATSProvider.SMARTRECRUITERS, "smartrecruiters-company"),
    ),
)
def test_configurable_provider_source_identities_remain_exact(
    provider: ATSProvider, identifier: str
) -> None:
    assert ExactFeedTarget(provider, "global", identifier).source_name == provider.value
