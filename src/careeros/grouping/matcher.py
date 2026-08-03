"""Deterministic cross-source duplicate grouping.

The matcher is pure: it turns stored jobs into canonical groups using only the
stdlib and the normalization helpers. It performs no I/O and never touches the
database; persistence lives in ``careeros.database.groups``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from careeros.database.jobs import StoredJob
from careeros.grouping.normalize import (
    normalize_company,
    normalize_location,
    normalize_title,
    remote_class,
)


@dataclass(frozen=True)
class JobIdentity:
    """Normalized signature of a single job used for duplicate detection."""

    company_sig: str
    title_sig: str
    remote_class: str | None
    location_sig: str


@dataclass(frozen=True)
class CanonicalGroup:
    """A group of jobs considered to be the same underlying vacancy."""

    group_id: str
    members: tuple[StoredJob, ...]
    reason: str
    remote_sig: str | None
    location_sig: str


def identity_for(job: StoredJob) -> JobIdentity:
    """Compute the normalized identity signature for a stored job."""
    return JobIdentity(
        company_sig=normalize_company(job.company),
        title_sig=normalize_title(job.title),
        remote_class=remote_class(job.remote_status),
        location_sig=normalize_location(job.location),
    )


def canonical_group_id(
    company_sig: str,
    title_sig: str,
    remote_class_sig: str | None,
    location_sig: str,
) -> str:
    """Return a content-addressed canonical group identifier.

    The ID is a truncated SHA-256 of the group's normalized identity tuple.
    Being content-addressed (rather than a sequential counter), it never
    renumbers when unrelated groups are added or removed: only a group whose
    own normalized identity changes gets a new ID, which happens exactly when
    the underlying matching rules evolve.
    """
    identity = (company_sig, title_sig, remote_class_sig, location_sig)
    blob = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def build_reason(
    company_sig: str,
    title_sig: str,
    remote_sig: str | None,
    location_sig: str,
) -> str:
    """Build a human-readable, deterministic explanation for a group."""
    parts = [f"company+title match ({company_sig} / {title_sig})"]
    parts.append(f"remote: {remote_sig or 'unspecified'}")
    if location_sig:
        parts.append(f"location: {location_sig}")
    return "; ".join(parts)


def _locations_compatible(a: str, b: str) -> bool:
    """Return True when location signatures are mutually compatible.

    Two signatures are compatible if one is a token-prefix of the other (e.g.
    "berlin" and "berlin germany"), which groups city vs. city + country
    listings while keeping genuinely different cities distinct.
    """
    a_tokens = a.split()
    b_tokens = b.split()
    if not a_tokens or not b_tokens:
        return True
    shorter, longer = (
        (a_tokens, b_tokens) if len(a_tokens) <= len(b_tokens) else (b_tokens, a_tokens)
    )
    return longer[: len(shorter)] == shorter


class _UnionFind:
    """Minimal union-find for clustering compatible members."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))
        self._size = [1] * n

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._size[ra] < self._size[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        self._size[ra] += self._size[rb]


def _location_clusters(
    located: list[StoredJob],
    identities: dict[int, JobIdentity],
) -> list[list[StoredJob]]:
    """Partition located members into location-compatible clusters."""
    n = len(located)
    if n == 0:
        return []
    uf = _UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if _locations_compatible(
                identities[located[i].id].location_sig,
                identities[located[j].id].location_sig,
            ):
                uf.union(i, j)
    clusters: dict[int, list[StoredJob]] = {}
    for index, member in enumerate(located):
        clusters.setdefault(uf.find(index), []).append(member)
    result = [sorted(c, key=lambda j: j.id) for c in clusters.values()]
    result.sort(key=lambda c: min(j.id for j in c))
    return result


def _build_group(
    cluster: list[StoredJob],
    identities: dict[int, JobIdentity],
) -> CanonicalGroup:
    """Build a CanonicalGroup from a fully compatible cluster (>1 member)."""
    members = tuple(sorted(cluster, key=lambda j: j.id))
    first = identities[members[0].id]
    locations = sorted(
        {
            identities[member.id].location_sig
            for member in members
            if identities[member.id].location_sig
        }
    )
    location_repr = locations[0] if locations else ""
    group_id = canonical_group_id(
        first.company_sig, first.title_sig, first.remote_class, location_repr
    )
    return CanonicalGroup(
        group_id=group_id,
        members=members,
        reason=build_reason(
            first.company_sig, first.title_sig, first.remote_class, location_repr
        ),
        remote_sig=first.remote_class,
        location_sig=location_repr,
    )


def assign_groups(jobs: Sequence[StoredJob]) -> list[CanonicalGroup]:
    """Deterministically partition jobs into canonical duplicate groups.

    The algorithm is deliberately conservative:
    - Jobs must share an equal normalized company and title signature.
    - A remote-status class conflict (remote vs onsite vs unspecified) splits.
    - Distinct, incompatible non-generic locations split.
    - Empty/generic locations merge with the sole located cluster, or form
      their own group when the targets are ambiguous (several located
      clusters).

    Jobs whose company or title normalizes to empty (unidentifiable) are never
    grouped. Only groups with at least two members are returned; the rest are
    singletons implied by their absence.
    """
    ordered = sorted(jobs, key=lambda job: job.id)
    identities = {job.id: identity_for(job) for job in ordered}

    buckets: dict[tuple[str, str], list[StoredJob]] = {}
    for job in ordered:
        ident = identities[job.id]
        if not ident.company_sig or not ident.title_sig:
            continue
        buckets.setdefault((ident.company_sig, ident.title_sig), []).append(job)

    groups: list[CanonicalGroup] = []
    for key in sorted(buckets):
        by_remote: dict[str | None, list[StoredJob]] = {}
        for job in buckets[key]:
            by_remote.setdefault(identities[job.id].remote_class, []).append(job)
        for remote in sorted(by_remote, key=lambda c: (c is None, c)):
            class_jobs = by_remote[remote]
            located = [job for job in class_jobs if identities[job.id].location_sig]
            empty = [job for job in class_jobs if not identities[job.id].location_sig]
            clusters = _location_clusters(located, identities)
            if not clusters:
                clusters = [empty] if empty else []
            elif len(clusters) == 1:
                clusters = [clusters[0] + empty] if empty else [clusters[0]]
            elif empty:
                clusters = clusters + [empty]
            for cluster in clusters:
                if len(cluster) > 1:
                    groups.append(_build_group(cluster, identities))

    groups.sort(key=lambda g: min(member.id for member in g.members))
    return groups
