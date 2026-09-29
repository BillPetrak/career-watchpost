"""Shared data models for job collectors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CollectedJob:
    """Normalized job collected from an external source."""

    source: str
    source_job_id: str
    title: str
    company: str | None
    location: str | None
    description: str | None
    url: str | None
    employment_type: str | None
    remote_status: str | None
    published_at: str | None
    collected_at: str


@dataclass(frozen=True)
class CollectorOutcome:
    """Optional structured result for configurable providers."""

    jobs: tuple[CollectedJob, ...]
    succeeded: bool = True
    error: str | None = None


__all__ = ["CollectedJob", "CollectorOutcome"]
