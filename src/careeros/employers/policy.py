"""Deterministic, side-effect-free employer/feed authority policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from careeros.employers.identity import EmployerId, ExactFeedTarget
from careeros.employers.models import (
    CatalogPage,
    EmployerCatalogRecord,
    EmployerCatalogState,
    EmployerFeedAttribution,
    EvidenceState,
    FeedLifecycle,
    FeedLifecycleMetadata,
    FeedSuccession,
    InheritedOwnerVeto,
    JobEligibilityInput,
    MonitoringState,
    PermissionState,
    ProviderPermissions,
    RelationshipKind,
    RuntimeConstraints,
    SafetyState,
    SuccessionState,
    SuccessorInheritance,
    VerificationState,
    VisibilityState,
)


class CollectionAuthority(StrEnum):
    YAML = "yaml"
    MONITORING = "monitoring"
    CATALOG = "catalog"


class SuppressionKind(StrEnum):
    SAFETY = "safety"
    PERMISSION = "permission"
    OWNER_VETO = "owner_veto"
    STATE = "state"
    RUNTIME = "runtime"


class PriorityClass(StrEnum):
    NORMAL = "normal"
    FOLLOWED = "followed"


class PolicyReason(StrEnum):
    YAML_REQUESTED = "yaml_requested"
    MONITORING_REQUESTED = "monitoring_requested"
    CATALOG_REQUESTED = "catalog_requested"
    MONITORING_PAUSED = "monitoring_paused"
    CATALOG_CONSENT_MISSING = "catalog_consent_missing"
    CATALOG_INELIGIBLE = "catalog_ineligible"
    SAFETY_UNKNOWN = "safety_unknown"
    SAFETY_FAILED = "safety_failed"
    PROVIDER_DISABLED = "provider_disabled"
    FEED_BLOCKED = "feed_blocked"
    INHERITED_FEED_BLOCK = "inherited_feed_block"
    INHERITED_MONITORING_PAUSE = "inherited_monitoring_pause"
    SUCCESSOR_QUARANTINED_MONITORING = "successor_quarantined_monitoring"
    SUCCESSOR_QUARANTINED_CATALOG = "successor_quarantined_catalog"
    EMPLOYER_EXCLUDED_DEDICATED_FEED = "employer_excluded_dedicated_feed"
    FEED_RETIRED = "feed_retired"
    VERIFICATION_REQUIRED = "verification_required"
    PERMISSION_UNKNOWN = "permission_unknown"
    PERMISSION_DENIED = "permission_denied"
    RUNTIME_SUPPRESSED = "runtime_suppressed"
    HIDDEN = "hidden"
    FOLLOWED_PRIORITY = "followed_priority"
    AUTHORITY_EFFECTIVE_YAML = "authority_effective_yaml"
    AUTHORITY_EFFECTIVE_MONITORING = "authority_effective_monitoring"
    AUTHORITY_EFFECTIVE_CATALOG = "authority_effective_catalog"


@dataclass(frozen=True)
class AuthoritySuppression:
    kind: SuppressionKind
    reason: PolicyReason
    authority: CollectionAuthority | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SuppressionKind):
            raise TypeError("kind must be a SuppressionKind")
        if not isinstance(self.reason, PolicyReason):
            raise TypeError("reason must be a PolicyReason")
        if self.authority is not None and not isinstance(
            self.authority, CollectionAuthority
        ):
            raise TypeError("authority must be a CollectionAuthority or None")


@dataclass(frozen=True)
class FeedPolicyInput:
    feed_target: ExactFeedTarget
    catalog_consent: bool = False
    catalog_eligible: bool = False
    monitoring: MonitoringState = MonitoringState.OFF
    yaml_authorized: bool = False
    provider_disabled: bool = False
    feed_blocked: bool = False
    quarantined: bool = False
    excluded_employers: frozenset[EmployerId] = frozenset()
    followed: bool = False
    visibility: VisibilityState = VisibilityState.VISIBLE
    metadata: FeedLifecycleMetadata = FeedLifecycleMetadata()
    safety: SafetyState = SafetyState.UNKNOWN
    permissions: ProviderPermissions = ProviderPermissions()
    attributions: tuple[EmployerFeedAttribution, ...] = ()
    inherited_vetoes: frozenset[InheritedOwnerVeto] = frozenset()
    runtime: RuntimeConstraints = RuntimeConstraints()

    def __post_init__(self) -> None:
        if not isinstance(self.feed_target, ExactFeedTarget):
            raise TypeError("feed_target must be an ExactFeedTarget")
        for value, label in (
            (self.catalog_consent, "catalog_consent"),
            (self.catalog_eligible, "catalog_eligible"),
            (self.yaml_authorized, "yaml_authorized"),
            (self.provider_disabled, "provider_disabled"),
            (self.feed_blocked, "feed_blocked"),
            (self.followed, "followed"),
            (self.quarantined, "quarantined"),
        ):
            if type(value) is not bool:
                raise TypeError(f"{label} must be a bool")
        if not isinstance(self.monitoring, MonitoringState):
            raise TypeError("monitoring must be a MonitoringState")
        if not isinstance(self.visibility, VisibilityState):
            raise TypeError("visibility must be a VisibilityState")
        if not isinstance(self.safety, SafetyState):
            raise TypeError("safety must be a SafetyState")
        if not isinstance(self.metadata, FeedLifecycleMetadata):
            raise TypeError("metadata must be FeedLifecycleMetadata")
        if not isinstance(self.permissions, ProviderPermissions):
            raise TypeError("permissions must be ProviderPermissions")
        if not isinstance(self.runtime, RuntimeConstraints):
            raise TypeError("runtime must be RuntimeConstraints")
        if type(self.attributions) is not tuple or not all(
            isinstance(item, EmployerFeedAttribution) for item in self.attributions
        ):
            raise TypeError(
                "attributions must be a tuple of EmployerFeedAttribution values"
            )
        if type(self.excluded_employers) is not frozenset or not all(
            isinstance(item, EmployerId) for item in self.excluded_employers
        ):
            raise TypeError(
                "excluded_employers must be a frozenset of EmployerId values"
            )
        if type(self.inherited_vetoes) is not frozenset or not all(
            isinstance(item, InheritedOwnerVeto) for item in self.inherited_vetoes
        ):
            raise TypeError(
                "inherited_vetoes must be a frozenset of InheritedOwnerVeto values"
            )
        if any(item.feed_target != self.feed_target for item in self.attributions):
            raise ValueError("all attributions must refer to feed_target")


@dataclass(frozen=True)
class FeedPolicyDecision:
    requested_authorities: frozenset[CollectionAuthority]
    effective_authorities: frozenset[CollectionAuthority]
    safety_suppressions: tuple[AuthoritySuppression, ...]
    permission_suppressions: tuple[AuthoritySuppression, ...]
    owner_vetoes: tuple[AuthoritySuppression, ...]
    state_suppressions: tuple[AuthoritySuppression, ...]
    runtime_suppressions: tuple[AuthoritySuppression, ...]
    collection_eligible: bool
    presentation_visible: bool
    priority: PriorityClass
    reason_codes: tuple[PolicyReason, ...]

    def __post_init__(self) -> None:
        for authorities, label in (
            (self.requested_authorities, "requested_authorities"),
            (self.effective_authorities, "effective_authorities"),
        ):
            if type(authorities) is not frozenset or not all(
                isinstance(authority, CollectionAuthority) for authority in authorities
            ):
                raise TypeError(
                    f"{label} must be a frozenset of CollectionAuthority values"
                )
        for suppressions, label in (
            (self.safety_suppressions, "safety_suppressions"),
            (self.permission_suppressions, "permission_suppressions"),
            (self.owner_vetoes, "owner_vetoes"),
            (self.state_suppressions, "state_suppressions"),
            (self.runtime_suppressions, "runtime_suppressions"),
        ):
            if type(suppressions) is not tuple or not all(
                isinstance(suppression, AuthoritySuppression)
                for suppression in suppressions
            ):
                raise TypeError(
                    f"{label} must be a tuple of AuthoritySuppression values"
                )
        for value, label in (
            (self.collection_eligible, "collection_eligible"),
            (self.presentation_visible, "presentation_visible"),
        ):
            if type(value) is not bool:
                raise TypeError(f"{label} must be a bool")
        if not isinstance(self.priority, PriorityClass):
            raise TypeError("priority must be a PriorityClass")
        if type(self.reason_codes) is not tuple or not all(
            isinstance(reason, PolicyReason) for reason in self.reason_codes
        ):
            raise TypeError("reason_codes must be a tuple of PolicyReason values")
        if not self.effective_authorities.issubset(self.requested_authorities):
            raise ValueError(
                "effective_authorities must be a subset of requested_authorities"
            )
        if self.collection_eligible is not bool(self.effective_authorities):
            raise ValueError("collection_eligible must reflect effective_authorities")


_AUTHORITY_ORDER = (
    CollectionAuthority.YAML,
    CollectionAuthority.MONITORING,
    CollectionAuthority.CATALOG,
)


def _validate_excluded_employers(
    excluded_employers: frozenset[EmployerId],
) -> None:
    if type(excluded_employers) is not frozenset or not all(
        isinstance(employer_id, EmployerId) for employer_id in excluded_employers
    ):
        raise TypeError("excluded_employers must be a frozenset of EmployerId values")


def _dedicated_confirmed_employer(
    attributions: tuple[EmployerFeedAttribution, ...],
) -> EmployerId | None:
    confirmed_relationships = {
        (item.employer_id, item.relationship)
        for item in attributions
        if item.evidence is EvidenceState.CONFIRMED
    }
    if len(confirmed_relationships) != 1:
        return None
    employer_id, relationship = next(iter(confirmed_relationships))
    return employer_id if relationship is RelationshipKind.DIRECT else None


def _permission_for(
    permissions: ProviderPermissions, authority: CollectionAuthority
) -> PermissionState:
    if authority is CollectionAuthority.YAML:
        return permissions.yaml
    if authority is CollectionAuthority.MONITORING:
        return permissions.monitoring
    return permissions.catalog


def resolve_feed_policy(inputs: FeedPolicyInput) -> FeedPolicyDecision:
    """Resolve requested and effective authority in deterministic precedence."""
    if not isinstance(inputs, FeedPolicyInput):
        raise TypeError("inputs must be a FeedPolicyInput")
    requested: set[CollectionAuthority] = set()
    reasons: list[PolicyReason] = []
    if inputs.yaml_authorized:
        requested.add(CollectionAuthority.YAML)
        reasons.append(PolicyReason.YAML_REQUESTED)
    if inputs.monitoring is not MonitoringState.OFF:
        requested.add(CollectionAuthority.MONITORING)
        reasons.append(PolicyReason.MONITORING_REQUESTED)
    if inputs.catalog_consent:
        requested.add(CollectionAuthority.CATALOG)
        reasons.append(PolicyReason.CATALOG_REQUESTED)
    else:
        reasons.append(PolicyReason.CATALOG_CONSENT_MISSING)

    effective = set(requested)
    safety_suppressions: list[AuthoritySuppression] = []
    permission_suppressions: list[AuthoritySuppression] = []
    owner_vetoes: list[AuthoritySuppression] = []
    state_suppressions: list[AuthoritySuppression] = []
    runtime_suppressions: list[AuthoritySuppression] = []

    def suppress_all(
        destination: list[AuthoritySuppression],
        kind: SuppressionKind,
        reason: PolicyReason,
    ) -> None:
        if requested:
            destination.append(AuthoritySuppression(kind, reason))
        effective.clear()
        reasons.append(reason)

    if inputs.safety is not SafetyState.PASSED:
        safety_reason = (
            PolicyReason.SAFETY_FAILED
            if inputs.safety is SafetyState.FAILED
            else PolicyReason.SAFETY_UNKNOWN
        )
        suppress_all(safety_suppressions, SuppressionKind.SAFETY, safety_reason)

    if inputs.provider_disabled:
        suppress_all(
            owner_vetoes,
            SuppressionKind.OWNER_VETO,
            PolicyReason.PROVIDER_DISABLED,
        )
    if inputs.feed_blocked:
        suppress_all(
            owner_vetoes, SuppressionKind.OWNER_VETO, PolicyReason.FEED_BLOCKED
        )
    if InheritedOwnerVeto.FEED_BLOCK in inputs.inherited_vetoes:
        suppress_all(
            owner_vetoes,
            SuppressionKind.OWNER_VETO,
            PolicyReason.INHERITED_FEED_BLOCK,
        )

    dedicated_employer = _dedicated_confirmed_employer(inputs.attributions)
    dedicated_excluded = (
        dedicated_employer is not None
        and dedicated_employer in inputs.excluded_employers
    )
    if dedicated_excluded:
        suppress_all(
            owner_vetoes,
            SuppressionKind.OWNER_VETO,
            PolicyReason.EMPLOYER_EXCLUDED_DEDICATED_FEED,
        )

    if inputs.metadata.lifecycle is FeedLifecycle.RETIRED:
        suppress_all(
            state_suppressions, SuppressionKind.STATE, PolicyReason.FEED_RETIRED
        )

    if inputs.quarantined:
        for authority, reason in (
            (
                CollectionAuthority.MONITORING,
                PolicyReason.SUCCESSOR_QUARANTINED_MONITORING,
            ),
            (
                CollectionAuthority.CATALOG,
                PolicyReason.SUCCESSOR_QUARANTINED_CATALOG,
            ),
        ):
            if authority not in requested:
                continue
            effective.discard(authority)
            state_suppressions.append(
                AuthoritySuppression(SuppressionKind.STATE, reason, authority)
            )
            reasons.append(reason)

    if CollectionAuthority.MONITORING in effective and (
        inputs.monitoring is MonitoringState.PAUSED
        or InheritedOwnerVeto.MONITORING_PAUSE in inputs.inherited_vetoes
    ):
        effective.remove(CollectionAuthority.MONITORING)
        reason = (
            PolicyReason.INHERITED_MONITORING_PAUSE
            if InheritedOwnerVeto.MONITORING_PAUSE in inputs.inherited_vetoes
            else PolicyReason.MONITORING_PAUSED
        )
        owner_vetoes.append(
            AuthoritySuppression(
                SuppressionKind.OWNER_VETO,
                reason,
                CollectionAuthority.MONITORING,
            )
        )
        reasons.append(reason)

    if CollectionAuthority.CATALOG in effective and not inputs.catalog_eligible:
        effective.remove(CollectionAuthority.CATALOG)
        state_suppressions.append(
            AuthoritySuppression(
                SuppressionKind.STATE,
                PolicyReason.CATALOG_INELIGIBLE,
                CollectionAuthority.CATALOG,
            )
        )
        reasons.append(PolicyReason.CATALOG_INELIGIBLE)

    for authority in _AUTHORITY_ORDER:
        if authority not in effective:
            continue
        if (
            authority in {CollectionAuthority.MONITORING, CollectionAuthority.CATALOG}
            and inputs.metadata.verification is not VerificationState.VERIFIED
        ):
            effective.remove(authority)
            permission_suppressions.append(
                AuthoritySuppression(
                    SuppressionKind.PERMISSION,
                    PolicyReason.VERIFICATION_REQUIRED,
                    authority,
                )
            )
            reasons.append(PolicyReason.VERIFICATION_REQUIRED)
            continue
        permission = _permission_for(inputs.permissions, authority)
        allowed = permission is PermissionState.ALLOWED or (
            authority is CollectionAuthority.YAML
            and permission is PermissionState.LEGACY_YAML
        )
        if not allowed:
            effective.remove(authority)
            reason = (
                PolicyReason.PERMISSION_DENIED
                if permission is PermissionState.DENIED
                else PolicyReason.PERMISSION_UNKNOWN
            )
            permission_suppressions.append(
                AuthoritySuppression(SuppressionKind.PERMISSION, reason, authority)
            )
            reasons.append(reason)

    if not inputs.runtime.allowed:
        suppress_all(
            runtime_suppressions,
            SuppressionKind.RUNTIME,
            PolicyReason.RUNTIME_SUPPRESSED,
        )

    if inputs.visibility is VisibilityState.HIDDEN:
        reasons.append(PolicyReason.HIDDEN)
    priority = PriorityClass.FOLLOWED if inputs.followed else PriorityClass.NORMAL
    if inputs.followed:
        reasons.append(PolicyReason.FOLLOWED_PRIORITY)

    effective_reasons = {
        CollectionAuthority.YAML: PolicyReason.AUTHORITY_EFFECTIVE_YAML,
        CollectionAuthority.MONITORING: PolicyReason.AUTHORITY_EFFECTIVE_MONITORING,
        CollectionAuthority.CATALOG: PolicyReason.AUTHORITY_EFFECTIVE_CATALOG,
    }
    for authority in _AUTHORITY_ORDER:
        if authority in effective:
            reasons.append(effective_reasons[authority])

    return FeedPolicyDecision(
        requested_authorities=frozenset(requested),
        effective_authorities=frozenset(effective),
        safety_suppressions=tuple(safety_suppressions),
        permission_suppressions=tuple(permission_suppressions),
        owner_vetoes=tuple(owner_vetoes),
        state_suppressions=tuple(state_suppressions),
        runtime_suppressions=tuple(runtime_suppressions),
        collection_eligible=bool(effective),
        presentation_visible=(
            inputs.visibility is VisibilityState.VISIBLE
            and not dedicated_excluded
            and inputs.metadata.lifecycle is FeedLifecycle.ACTIVE
        ),
        priority=priority,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def evaluate_succession(
    succession: FeedSuccession,
    *,
    predecessor_monitoring: MonitoringState,
    predecessor_blocked: bool,
) -> SuccessorInheritance:
    """Apply only the approved, explicit succession inheritance contract."""
    if not isinstance(succession, FeedSuccession):
        raise TypeError("succession must be a FeedSuccession")
    if not isinstance(predecessor_monitoring, MonitoringState):
        raise TypeError("predecessor_monitoring must be a MonitoringState")
    if type(predecessor_blocked) is not bool:
        raise TypeError("predecessor_blocked must be a bool")
    negative: set[InheritedOwnerVeto] = set()
    if predecessor_blocked:
        negative.add(InheritedOwnerVeto.FEED_BLOCK)
    if predecessor_monitoring is MonitoringState.PAUSED:
        negative.add(InheritedOwnerVeto.MONITORING_PAUSE)

    accepted = succession.state is SuccessionState.CONFIRMED
    requested_monitoring = predecessor_monitoring if accepted else MonitoringState.OFF
    inherit_negative = succession.state in {
        SuccessionState.CANDIDATE,
        SuccessionState.CONFIRMED,
    }
    inherited_vetoes = frozenset(negative if inherit_negative else ())
    return SuccessorInheritance(
        target=succession.successor,
        requested_monitoring=requested_monitoring,
        inherited_vetoes=inherited_vetoes,
        quarantined=succession.state is SuccessionState.CANDIDATE,
    )


def job_is_default_eligible(
    job: JobEligibilityInput, excluded_employers: frozenset[EmployerId]
) -> bool:
    """Suppress only a job's confirmed excluded-employer attribution."""
    if not isinstance(job, JobEligibilityInput):
        raise TypeError("job must be a JobEligibilityInput")
    _validate_excluded_employers(excluded_employers)
    return not any(
        attribution.evidence is EvidenceState.CONFIRMED
        and attribution.employer_id in excluded_employers
        for attribution in job.attributions
    )


def eligible_group_members(
    jobs: tuple[JobEligibilityInput, ...],
    excluded_employers: frozenset[EmployerId],
) -> tuple[JobEligibilityInput, ...]:
    """Filter members independently; grouping never spreads an exclusion."""
    if type(jobs) is not tuple or not all(
        isinstance(job, JobEligibilityInput) for job in jobs
    ):
        raise TypeError("jobs must be a tuple of JobEligibilityInput values")
    _validate_excluded_employers(excluded_employers)
    return tuple(
        job for job in jobs if job_is_default_eligible(job, excluded_employers)
    )


def active_visible_catalog_page(
    records: tuple[EmployerCatalogRecord, ...], *, offset: int = 0, limit: int = 20
) -> CatalogPage:
    """Page the uncapped active-visible projection deterministically."""
    if type(records) is not tuple or not all(
        isinstance(record, EmployerCatalogRecord) for record in records
    ):
        raise TypeError("records must be a tuple of EmployerCatalogRecord values")
    if type(offset) is not int:
        raise TypeError("offset must be an integer")
    if offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(limit) is not int:
        raise TypeError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be a positive integer")
    active = tuple(
        sorted(
            (
                record
                for record in records
                if record.catalog_state is EmployerCatalogState.ACTIVE_VISIBLE
            ),
            key=lambda record: record.employer_id.value,
        )
    )
    return CatalogPage(active[offset : offset + limit], len(active), offset, limit)


__all__ = [
    "AuthoritySuppression",
    "CollectionAuthority",
    "FeedPolicyDecision",
    "FeedPolicyInput",
    "PolicyReason",
    "PriorityClass",
    "SuppressionKind",
    "active_visible_catalog_page",
    "eligible_group_members",
    "evaluate_succession",
    "job_is_default_eligible",
    "resolve_feed_policy",
]
