"""Deterministic job scoring against a candidate profile."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from careeros.database.jobs import StoredJob


@dataclass(frozen=True)
class CandidateProfile:
    """A candidate's job preferences used for deterministic scoring."""

    desired_titles: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    preferred_locations: tuple[str, ...] = ()
    remote_preference: str | None = None
    excluded_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class RankedJob:
    """A scored job with explainable reasons."""

    job: StoredJob
    score: int
    reasons: tuple[str, ...]


_TITLE_POINTS = 20
_TITLE_CAP = 2
_SKILL_POINTS = 8
_SKILL_CAP = 6
_LOCATION_POINTS = 15
_REMOTE_POINTS = 10
_EXCLUDED_PENALTY = -5
_EXCLUDED_CAP = 3


def _word_tokens(text: str) -> list[str]:
    """Split text into lowercase word tokens, stripping punctuation."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _has_phrase(text: str, phrase: str) -> bool:
    """Check if phrase appears as a contiguous token subsequence in text."""
    text_tokens = _word_tokens(text)
    phrase_tokens = _word_tokens(phrase)
    if not phrase_tokens:
        return False
    window_len = len(phrase_tokens)
    for i in range(len(text_tokens) - window_len + 1):
        if text_tokens[i : i + window_len] == phrase_tokens:
            return True
    return False


def _contains(text: str | None, term: str) -> bool:
    """Case-insensitive substring check."""
    if not text:
        return False
    return term.lower() in text.lower()


def score_job(job: StoredJob, profile: CandidateProfile) -> RankedJob:
    """Score a single job against a candidate profile and return explainable results."""
    score = 0
    reasons: list[str] = []
    job_title = job.title or ""
    description = job.description or ""
    haystack = f"{job_title} {description}"

    matched_titles = {t for t in profile.desired_titles if _has_phrase(job_title, t)}
    for desired in list(matched_titles)[:_TITLE_CAP]:
        score += _TITLE_POINTS
        reasons.append(f"Title matches '{desired}' (+{_TITLE_POINTS})")

    matched_skills = {s for s in profile.skills if _contains(haystack, s)}
    for skill in list(matched_skills)[:_SKILL_CAP]:
        score += _SKILL_POINTS
        reasons.append(f"Has skill '{skill}' (+{_SKILL_POINTS})")

    job_location = job.location or ""
    for loc in profile.preferred_locations:
        if _contains(job_location, loc):
            score += _LOCATION_POINTS
            reasons.append(f"Location matches '{loc}' (+{_LOCATION_POINTS})")
            break

    if profile.remote_preference and job.remote_status == profile.remote_preference:
        score += _REMOTE_POINTS
        reasons.append(f"Remote position (+{_REMOTE_POINTS})")

    matched_excluded = {t for t in profile.excluded_terms if _contains(haystack, t)}
    for term in list(matched_excluded)[:_EXCLUDED_CAP]:
        score += _EXCLUDED_PENALTY
        reasons.append(f"Contains excluded term '{term}' ({_EXCLUDED_PENALTY})")

    return RankedJob(job=job, score=score, reasons=tuple(reasons))


def score_jobs(
    jobs: Iterable[StoredJob],
    profile: CandidateProfile,
) -> list[RankedJob]:
    """Score multiple jobs and return them sorted by descending score.

    Ties are broken by ascending job ID.
    """
    ranked = [score_job(job, profile) for job in jobs]
    ranked.sort(key=lambda rj: (-rj.score, rj.job.id))
    return ranked
