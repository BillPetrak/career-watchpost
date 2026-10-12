"""Immutable offline employer/feed policy inputs and metadata."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from careeros.employers.identity import EmployerId, ExactFeedTarget


class RelationshipKind(StrEnum):
    DIRECT = "direct"
    OPERATOR = "operator"
    REPRESENTED = "represented"


class EvidenceState(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class EvidenceSource(StrEnum):
    OWNER = "owner"
    PACKAGED_CATALOG = "packaged_catalog"
    PROVIDER = "provider"
    STRUCTURED_SIGNAL = "structured_signal"


class FeedLifecycle(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class VerificationState(StrEnum):
    UNKNOWN = "unknown"
    VERIFIED = "verified"
    STALE = "stale"
    FAILED = "failed"


class FeedHealth(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class PermissionState(StrEnum):
    """Provider permission evidence for one authority mode.

    ``LEGACY_YAML`` preserves the released configured-source compatibility
    contract. It is not permission for catalog or monitoring collection.
    """

    UNKNOWN = "unknown"
    ALLOWED = "allowed"
    DENIED = "denied"
    LEGACY_YAML = "legacy_yaml"


class SafetyState(StrEnum):
    UNKNOWN = "unknown"
    PASSED = "passed"
    FAILED = "failed"


class MonitoringState(StrEnum):
    OFF = "off"
    ACTIVE = "active"
    PAUSED = "paused"


class VisibilityState(StrEnum):
    VISIBLE = "visible"
    HIDDEN = "hidden"


class SuccessionState(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    RETIRED = "retired"


class InheritedOwnerVeto(StrEnum):
    FEED_BLOCK = "feed_block"
    MONITORING_PAUSE = "monitoring_pause"


class EmployerLifecycle(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    RETIRED = "retired"


class EmployerCatalogState(StrEnum):
    ACTIVE_VISIBLE = "active_visible"
    HIDDEN = "hidden"
    EXCLUDED = "excluded"
    INACTIVE = "inactive"
    RETIRED = "retired"


@dataclass(frozen=True)
class EvidenceProvenance:
    source: EvidenceSource
    reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, EvidenceSource):
            raise TypeError("source must be an EvidenceSource")
        if (
            not isinstance(self.reference, str)
            or self.reference != self.reference.strip()
            or not self.reference
            or len(self.reference) > 256
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.reference
            )
        ):
            raise ValueError("provenance reference is invalid")


@dataclass(frozen=True)
class EmployerFeedAttribution:
    """Evidence-bearing many-to-many employer/feed relationship."""

    employer_id: EmployerId
    feed_target: ExactFeedTarget
    relationship: RelationshipKind
    evidence: EvidenceState
    provenance: EvidenceProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.employer_id, EmployerId):
            raise TypeError("employer_id must be an EmployerId")
        if not isinstance(self.feed_target, ExactFeedTarget):
            raise TypeError("feed_target must be an ExactFeedTarget")
        if not isinstance(self.relationship, RelationshipKind):
            raise TypeError("relationship must be a RelationshipKind")
        if not isinstance(self.evidence, EvidenceState):
            raise TypeError("evidence must be an EvidenceState")
        if not isinstance(self.provenance, EvidenceProvenance):
            raise TypeError("provenance must be EvidenceProvenance")

    @property
    def implies_board_wide_employer(self) -> bool:
        return (
            self.relationship is RelationshipKind.DIRECT
            and self.evidence is EvidenceState.CONFIRMED
        )


@dataclass(frozen=True)
class FeedLifecycleMetadata:
    lifecycle: FeedLifecycle = FeedLifecycle.ACTIVE
    verification: VerificationState = VerificationState.UNKNOWN
    health: FeedHealth = FeedHealth.UNKNOWN

    def __post_init__(self) -> None:
        if not isinstance(self.lifecycle, FeedLifecycle):
            raise TypeError("lifecycle must be a FeedLifecycle")
        if not isinstance(self.verification, VerificationState):
            raise TypeError("verification must be a VerificationState")
        if not isinstance(self.health, FeedHealth):
            raise TypeError("health must be a FeedHealth")


@dataclass(frozen=True)
class ProviderPermissions:
    yaml: PermissionState = PermissionState.UNKNOWN
    monitoring: PermissionState = PermissionState.UNKNOWN
    catalog: PermissionState = PermissionState.UNKNOWN

    def __post_init__(self) -> None:
        for value in (self.yaml, self.monitoring, self.catalog):
            if not isinstance(value, PermissionState):
                raise TypeError("permissions must be PermissionState values")
        if self.monitoring is PermissionState.LEGACY_YAML:
            raise ValueError("legacy YAML compatibility cannot grant monitoring")
        if self.catalog is PermissionState.LEGACY_YAML:
            raise ValueError(
                "legacy YAML compatibility cannot grant catalog collection"
            )


@dataclass(frozen=True)
class RuntimeConstraints:
    allowed: bool = True
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise TypeError("allowed must be a bool")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("reason must be a string or None")
        if self.allowed and self.reason is not None:
            raise ValueError("allowed runtime constraints cannot have a reason")
        if not self.allowed and (not self.reason or self.reason != self.reason.strip()):
            raise ValueError("suppressed runtime constraints require a reason")


@dataclass(frozen=True)
class FeedSuccession:
    predecessor: ExactFeedTarget
    successor: ExactFeedTarget
    state: SuccessionState
    provenance: EvidenceProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.predecessor, ExactFeedTarget):
            raise TypeError("predecessor must be an ExactFeedTarget")
        if not isinstance(self.successor, ExactFeedTarget):
            raise TypeError("successor must be an ExactFeedTarget")
        if self.predecessor == self.successor:
            raise ValueError("a successor target must differ from its predecessor")
        if not isinstance(self.state, SuccessionState):
            raise TypeError("state must be a SuccessionState")
        if not isinstance(self.provenance, EvidenceProvenance):
            raise TypeError("provenance must be EvidenceProvenance")


@dataclass(frozen=True)
class SuccessorInheritance:
    target: ExactFeedTarget
    requested_monitoring: MonitoringState
    inherited_vetoes: frozenset[InheritedOwnerVeto]
    quarantined: bool
    verification: VerificationState = VerificationState.UNKNOWN
    permissions: ProviderPermissions = ProviderPermissions()
    health: FeedHealth = FeedHealth.UNKNOWN

    def __post_init__(self) -> None:
        if not isinstance(self.target, ExactFeedTarget):
            raise TypeError("target must be an ExactFeedTarget")
        if not isinstance(self.requested_monitoring, MonitoringState):
            raise TypeError("requested_monitoring must be a MonitoringState")
        if type(self.inherited_vetoes) is not frozenset or not all(
            isinstance(veto, InheritedOwnerVeto) for veto in self.inherited_vetoes
        ):
            raise TypeError(
                "inherited_vetoes must be a frozenset of InheritedOwnerVeto values"
            )
        if type(self.quarantined) is not bool:
            raise TypeError("quarantined must be a bool")
        if not isinstance(self.verification, VerificationState):
            raise TypeError("verification must be a VerificationState")
        if not isinstance(self.permissions, ProviderPermissions):
            raise TypeError("permissions must be ProviderPermissions")
        if not isinstance(self.health, FeedHealth):
            raise TypeError("health must be a FeedHealth")


@dataclass(frozen=True)
class JobEmployerAttribution:
    employer_id: EmployerId
    evidence: EvidenceState

    def __post_init__(self) -> None:
        if not isinstance(self.employer_id, EmployerId):
            raise TypeError("employer_id must be an EmployerId")
        if not isinstance(self.evidence, EvidenceState):
            raise TypeError("evidence must be an EvidenceState")


@dataclass(frozen=True)
class JobEligibilityInput:
    job_id: int
    attributions: tuple[JobEmployerAttribution, ...] = ()

    def __post_init__(self) -> None:
        if type(self.job_id) is not int:
            raise TypeError("job_id must be an integer")
        if self.job_id < 1:
            raise ValueError("job_id must be a positive integer")
        if type(self.attributions) is not tuple or not all(
            isinstance(attribution, JobEmployerAttribution)
            for attribution in self.attributions
        ):
            raise TypeError(
                "attributions must be a tuple of JobEmployerAttribution values"
            )


@dataclass(frozen=True)
class EmployerCatalogRecord:
    employer_id: EmployerId
    display_name: str
    lifecycle: EmployerLifecycle = EmployerLifecycle.ACTIVE
    catalog_eligible: bool = True
    visibility: VisibilityState = VisibilityState.VISIBLE
    excluded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.employer_id, EmployerId):
            raise TypeError("employer_id must be an EmployerId")
        if (
            not isinstance(self.display_name, str)
            or not self.display_name.strip()
            or self.display_name != self.display_name.strip()
        ):
            raise ValueError("display_name must be a non-empty trimmed string")
        if not isinstance(self.lifecycle, EmployerLifecycle):
            raise TypeError("lifecycle must be an EmployerLifecycle")
        if type(self.catalog_eligible) is not bool:
            raise TypeError("catalog_eligible must be a bool")
        if not isinstance(self.visibility, VisibilityState):
            raise TypeError("visibility must be a VisibilityState")
        if type(self.excluded) is not bool:
            raise TypeError("excluded must be a bool")

    @property
    def catalog_state(self) -> EmployerCatalogState:
        if self.lifecycle is EmployerLifecycle.RETIRED:
            return EmployerCatalogState.RETIRED
        if self.excluded:
            return EmployerCatalogState.EXCLUDED
        if self.visibility is VisibilityState.HIDDEN:
            return EmployerCatalogState.HIDDEN
        if self.lifecycle is EmployerLifecycle.INACTIVE or not self.catalog_eligible:
            return EmployerCatalogState.INACTIVE
        return EmployerCatalogState.ACTIVE_VISIBLE

    @property
    def restorable(self) -> bool:
        return self.catalog_state is not EmployerCatalogState.ACTIVE_VISIBLE


@dataclass(frozen=True)
class CatalogPage:
    items: tuple[EmployerCatalogRecord, ...]
    total_active_visible: int
    offset: int
    limit: int

    def __post_init__(self) -> None:
        if type(self.items) is not tuple or not all(
            isinstance(item, EmployerCatalogRecord) for item in self.items
        ):
            raise TypeError("items must be a tuple of EmployerCatalogRecord values")
        for value, label in (
            (self.total_active_visible, "total_active_visible"),
            (self.offset, "offset"),
            (self.limit, "limit"),
        ):
            if type(value) is not int:
                raise TypeError(f"{label} must be an integer")
        if self.total_active_visible < 0 or self.offset < 0 or self.limit < 1:
            raise ValueError("catalog page bounds are invalid")


__all__ = [
    "CatalogPage",
    "EmployerCatalogRecord",
    "EmployerCatalogState",
    "EmployerFeedAttribution",
    "EmployerLifecycle",
    "EvidenceProvenance",
    "EvidenceSource",
    "EvidenceState",
    "FeedHealth",
    "FeedLifecycle",
    "FeedLifecycleMetadata",
    "FeedSuccession",
    "InheritedOwnerVeto",
    "JobEligibilityInput",
    "JobEmployerAttribution",
    "MonitoringState",
    "PermissionState",
    "ProviderPermissions",
    "RelationshipKind",
    "RuntimeConstraints",
    "SafetyState",
    "SuccessionState",
    "SuccessorInheritance",
    "VerificationState",
    "VisibilityState",
]
