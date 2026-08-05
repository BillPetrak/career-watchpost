"""Deterministic job scoring against a candidate profile.

This module is the single presentation-agnostic scoring path for CareerOS.
Flat ranking (``score_job``/``score_jobs``) and grouped ranking
(``score_group``/``score_groups``) both funnel through :func:`score_facts`,
which turns raw job/group evidence into a :class:`MatchResult`. The CLI only
renders those results; it never reimplements scoring logic.

Scoring is a deterministic heuristic, not a probability of receiving an offer
and not an assessment of a person's overall suitability. Every component and
penalty below is applied identically for the same inputs.

Every scoring decision is recorded as a :class:`ScoreComponent` on the
:class:`MatchResult`; ``reasons`` is derived from those components so the
displayed explanation always matches the computed score. Optional review
feedback (:mod:`careeros.ranking.feedback`) is applied conservatively as a
bounded additive adjustment and exposed both as a ``feedback`` component and as
the ``MatchResult.feedback`` value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from careeros.database.groups import GroupedJob
from careeros.database.jobs import StoredJob
from careeros.database.runs import parse_utc
from careeros.ranking.feedback import (
    CandidateJob,
    FeedbackAdjustment,
    FeedbackSet,
    compute_feedback_adjustment,
    feedback_label,
    source_points,
)
from careeros.ranking.lexicon import term_matches
from careeros.text import clean_description, has_phrase

_TITLE_POINTS = 20
_TITLE_CAP = 2
_SKILL_POINTS = 8
_SKILL_CAP = 6
_LOCATION_POINTS = 15
_REMOTE_POINTS = 10
_EMPLOYMENT_POINTS = 5
_SENIORITY_POINTS = 5
_EXCLUDED_PENALTY = -5
_EXCLUDED_CAP = 3
_MISSING_REQUIRED_PENALTY = -4
_MISSING_REQUIRED_CAP = 3

# Deterministic seniority levels. Markers map onto a coarse canonical level so
# that "Sr", "Staff" and "Lead" all count as senior for conflict detection.
_SENIORITY_LEVELS: dict[str, str] = {
    "junior": "junior",
    "jr": "junior",
    "jr.": "junior",
    "entry": "junior",
    "entry level": "junior",
    "graduate": "junior",
    "intern": "junior",
    "internship": "junior",
    "mid": "mid",
    "mid level": "mid",
    "midlevel": "mid",
    "middle": "mid",
    "intermediate": "mid",
    "senior": "senior",
    "sr": "senior",
    "sr.": "senior",
    "staff": "senior",
    "principal": "senior",
    "lead": "senior",
    "expert": "senior",
    "head": "senior",
    "chief": "senior",
}


@dataclass(frozen=True)
class CandidateProfile:
    """A candidate's job preferences used for deterministic scoring."""

    desired_titles: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    preferred_locations: tuple[str, ...] = ()
    remote_preference: str | None = None
    excluded_terms: tuple[str, ...] = ()
    required_skills: tuple[str, ...] = ()
    hard_excluded_terms: tuple[str, ...] = ()
    preferred_employment_types: tuple[str, ...] = ()
    preferred_seniority: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchFacts:
    """Scoring inputs for one job or one canonical group.

    ``titles`` is a tuple so grouped scoring can carry the distinct titles of
    all group members without inventing a synthetic combined title.
    """

    titles: tuple[str, ...]
    description: str | None = None
    location: str | None = None
    remote_status: str | None = None
    employment_type: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class ScoreComponent:
    """One explainable additive (or veto) component of a match score.

    ``kind`` is a stable machine key (``title``, ``skill``, ``location``,
    ``remote``, ``employment``, ``seniority``, ``excluded``,
    ``missing_required``, ``feedback``, ``hard_excluded``). ``label`` is the
    human-readable line shown to users. ``points`` is the signed contribution to
    the raw score (``0`` for a veto). ``is_veto`` marks hard exclusions, which
    force the displayed score to 0 while leaving the raw score untouched.
    """

    kind: str
    label: str
    points: int
    is_veto: bool = False


@dataclass(frozen=True)
class MatchResult:
    """The outcome of scoring one job/group, with explainable facts.

    ``score`` is the raw deterministic accumulation and may be negative or
    exceed 100; ``match_score`` is the user-facing 0-100 value. The raw score is
    retained for future extensibility; current display and ordering use
    ``match_score``.

    ``components`` is the ordered, single source of truth for every explanation
    (including any review feedback adjustment); ``reasons`` is derived from it.
    ``feedback`` holds the applied review-feedback adjustment, or None when no
    non-zero feedback applied.
    """

    score: int
    matched_titles: tuple[str, ...]
    matched_skills: tuple[str, ...]
    missing_required_skills: tuple[str, ...]
    preferred_skills_not_observed: tuple[str, ...]
    location_matched: str | None
    remote_matched: bool
    employment_matched: str | None
    employment_mismatch: bool
    seniority_matched: str | None
    seniority_mismatch: bool
    excluded_terms: tuple[str, ...]
    hard_excluded_terms: tuple[str, ...]
    description_unavailable: bool
    components: tuple[ScoreComponent, ...] = ()
    feedback: FeedbackAdjustment | None = None

    @property
    def reasons(self) -> tuple[str, ...]:
        """Ordered human-readable explanation lines.

        Review-feedback components are excluded here because they are surfaced
        separately (the ``Feedback:`` line in text, ``score_components`` in
        JSON); every other component's label is shown verbatim.
        """
        return tuple(
            component.label
            for component in self.components
            if component.kind != "feedback"
        )

    @property
    def match_score(self) -> int:
        """The user-facing 0-100 score (0 on hard exclusion, otherwise clamped)."""
        if self.hard_excluded_terms:
            return 0
        return max(0, min(100, self.score))


@dataclass(frozen=True)
class RankedJob:
    """A scored job with explainable reasons."""

    job: StoredJob
    result: MatchResult

    @property
    def score(self) -> int:
        """Backward-compatible raw score."""
        return self.result.score

    @property
    def raw_score(self) -> int:
        """The raw deterministic score, retained for future extensibility."""
        return self.result.score

    @property
    def match_score(self) -> int:
        """The user-facing 0-100 score."""
        return self.result.match_score

    @property
    def reasons(self) -> tuple[str, ...]:
        """Ordered human-readable explanation lines."""
        return self.result.reasons


@dataclass(frozen=True)
class RankedGroup:
    """A scored canonical group with explainable reasons."""

    group: GroupedJob
    result: MatchResult

    @property
    def score(self) -> int:
        return self.result.score

    @property
    def raw_score(self) -> int:
        """The raw deterministic score, retained for future extensibility."""
        return self.result.score

    @property
    def match_score(self) -> int:
        return self.result.match_score

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.result.reasons


def _description_text(description: str | None) -> str:
    """Clean a stored description to readable text (empty string when absent)."""
    return clean_description(description) if description else ""


def _location_matches(job_location: str | None, preferred: str) -> bool:
    """Return True when preferred tokens are a subset of the job's location tokens.

    Token-subset matching avoids substring false positives (e.g. "Berlin" never
    matches "New Berlin") while still accepting "Greece" inside "Athens, Greece"
    and generic locations such as "Remote" or "Worldwide".
    """
    if not job_location:
        return False
    job_tokens = set(re.findall(r"[a-z0-9]+", job_location.lower()))
    preferred_tokens = set(re.findall(r"[a-z0-9]+", preferred.lower()))
    if not job_tokens or not preferred_tokens:
        return False
    return preferred_tokens.issubset(job_tokens)


def _split_employment_types(value: str | None) -> list[str]:
    """Split a stored (possibly comma-joined) employment-type string."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _normalize_employment(value: str) -> str:
    """Normalize an employment type for comparison (case/separators)."""
    return " ".join(value.strip().lower().replace("-", " ").replace("_", " ").split())


def _normalize_seniority(term: str) -> str:
    """Normalize a seniority term to a lookup key."""
    return " ".join(term.strip().lower().split())


def _seniority_level(term: str) -> str | None:
    """Map a profile seniority term onto its canonical level, or None."""
    return _SENIORITY_LEVELS.get(_normalize_seniority(term))


def _detect_seniority(titles: tuple[str, ...]) -> list[str]:
    """Return canonical seniority levels found in the titles, in level order."""
    detected: list[str] = []
    for level in ("junior", "mid", "senior"):
        markers = [
            marker for marker, mapped in _SENIORITY_LEVELS.items() if mapped == level
        ]
        if any(has_phrase(title, marker) for title in titles for marker in markers):
            detected.append(level)
    return detected


def feedback_summary(adjustment: FeedbackAdjustment) -> str:
    """A human-readable description of one feedback adjustment.

    Joins every contributing reviewed opportunity (``review_later`` sources are
    never included) with its signed points, e.g. ``similar to shortlisted job 3
    (Senior Engineer, Acme) (+5)``.
    """
    parts = [
        f"{feedback_label(source)} ({source_points(source):+d})"
        for source in adjustment.sources
    ]
    return "; ".join(parts)


def component_to_dict(component: ScoreComponent) -> dict[str, object]:
    """Serialize one score component for structured JSON output."""
    return {
        "kind": component.kind,
        "label": component.label,
        "points": component.points,
        "is_veto": component.is_veto,
    }


def _candidate_from_job(job: StoredJob) -> CandidateJob:
    """Build feedback-similarity evidence for one stored job."""
    return CandidateJob(
        job_ids=frozenset({job.id}),
        titles=(job.title,),
        company=job.company,
        location=job.location,
        remote_status=job.remote_status,
        employment_type=job.employment_type,
    )


def _candidate_from_group(group: GroupedJob) -> CandidateJob:
    """Build feedback-similarity evidence for a canonical group.

    Mirrors ``_facts_from_group``: every member contributes titles so a
    compatible title is found even when a source spells it differently, and the
    remaining evidence uses the first non-empty member value with the same
    remote/employment unioning the group scorer applies.
    """
    members = group.members or (group.representative,)
    return CandidateJob(
        job_ids=frozenset(member.id for member in members),
        titles=tuple(dict.fromkeys(member.title for member in members)),
        company=next(
            (member.company for member in members if member.company),
            None,
        ),
        location=next(
            (member.location for member in members if member.location),
            group.representative.location,
        ),
        remote_status=(
            "remote"
            if any(member.remote_status == "remote" for member in members)
            else group.representative.remote_status
        ),
        employment_type=", ".join(
            dict.fromkeys(
                member.employment_type for member in members if member.employment_type
            )
        )
        or None,
    )


def score_facts(
    facts: MatchFacts,
    profile: CandidateProfile,
    *,
    feedback: FeedbackSet | None = None,
    candidate: CandidateJob | None = None,
) -> MatchResult:
    """Score one job/group's facts against a profile (deterministic).

    Missing job data is neutral: absent description, location, remote status,
    employment type, or seniority simply contributes no points and no penalty.
    Hard exclusions are the only thing that forces the displayed score to 0.

    When ``feedback`` is given, a conservative review-feedback adjustment is
    applied on top of the raw score (see :mod:`careeros.ranking.feedback`);
    ``candidate`` supplies the similarity evidence (single job or group). The
    feedback adjustment never hard-excludes and can never bypass a hard
    exclusion.
    """
    components: list[ScoreComponent] = []
    score = 0

    cleaned_description = _description_text(facts.description)
    description_unavailable = not bool(cleaned_description.strip())
    titles_text = " ".join(facts.titles)
    haystack = f"{titles_text} {cleaned_description}".strip()

    # Title match (title field only, token/phrase match, capped).
    matched_titles: list[str] = []
    seen_titles: set[str] = set()
    for desired in profile.desired_titles:
        if desired in seen_titles:
            continue
        if any(has_phrase(title, desired) for title in facts.titles):
            seen_titles.add(desired)
            matched_titles.append(desired)
            if len(matched_titles) >= _TITLE_CAP:
                break
    for desired in matched_titles:
        score += _TITLE_POINTS
        components.append(
            ScoreComponent(
                kind="title",
                label=f"Title matches '{desired}' (+{_TITLE_POINTS})",
                points=_TITLE_POINTS,
            )
        )

    # Skill match: the union of preferred and required skills, each counted once
    # no matter how often a source (or several group members) mentions it.
    skill_terms = list(dict.fromkeys([*profile.skills, *profile.required_skills]))
    matched_skills: list[str] = []
    seen_skills: set[str] = set()
    for skill in skill_terms:
        if skill in seen_skills:
            continue
        if term_matches(skill, haystack):
            seen_skills.add(skill)
            matched_skills.append(skill)
            if len(matched_skills) >= _SKILL_CAP:
                break
    for skill in matched_skills:
        score += _SKILL_POINTS
        components.append(
            ScoreComponent(
                kind="skill",
                label=f"Has skill '{skill}' (+{_SKILL_POINTS})",
                points=_SKILL_POINTS,
            )
        )

    # Missing required skills: penalized only when the posting has usable
    # description text, so an absent description reads as "unavailable", never
    # as a mismatch. A required skill present in the title is already matched
    # above; title-only absence is weak evidence and stays neutral.
    matched_skill_set = set(matched_skills)
    missing_required: list[str] = []
    if profile.required_skills:
        for skill in profile.required_skills:
            if skill not in matched_skill_set and skill not in missing_required:
                missing_required.append(skill)
        if not description_unavailable:
            for skill in missing_required[:_MISSING_REQUIRED_CAP]:
                score += _MISSING_REQUIRED_PENALTY
                components.append(
                    ScoreComponent(
                        kind="missing_required",
                        label=(
                            f"Missing required skill '{skill}' "
                            f"({_MISSING_REQUIRED_PENALTY})"
                        ),
                        points=_MISSING_REQUIRED_PENALTY,
                    )
                )

    # Location match (single, token-subset, first preferred location wins).
    location_matched: str | None = None
    for preferred in profile.preferred_locations:
        if _location_matches(facts.location, preferred):
            location_matched = preferred
            score += _LOCATION_POINTS
            components.append(
                ScoreComponent(
                    kind="location",
                    label=f"Location matches '{preferred}' (+{_LOCATION_POINTS})",
                    points=_LOCATION_POINTS,
                )
            )
            break

    # Remote preference: exact match awards points; a conflict is neutral. The
    # stored "onsite_or_unspecified" value conflates onsite with unspecified, so
    # a missing bonus must never read as a penalty.
    remote_matched = bool(
        profile.remote_preference
        and facts.remote_status is not None
        and facts.remote_status == profile.remote_preference
    )
    if remote_matched:
        score += _REMOTE_POINTS
        components.append(
            ScoreComponent(
                kind="remote",
                label=f"Remote position (+{_REMOTE_POINTS})",
                points=_REMOTE_POINTS,
            )
        )

    # Employment type: +5 on match, -5 only on an explicit disjoint type, and
    # neutral when the job does not state an employment type.
    employment_matched: str | None = None
    employment_mismatch = False
    if profile.preferred_employment_types:
        job_types = _split_employment_types(facts.employment_type)
        job_types_norm = {_normalize_employment(value) for value in job_types}
        preferred_norm = {
            _normalize_employment(value) for value in profile.preferred_employment_types
        }
        if job_types_norm and preferred_norm:
            hit = next(
                (
                    value
                    for value in job_types
                    if _normalize_employment(value) in preferred_norm
                ),
                None,
            )
            if hit is not None:
                employment_matched = hit
                score += _EMPLOYMENT_POINTS
                components.append(
                    ScoreComponent(
                        kind="employment",
                        label=(
                            f"Employment type matches '{hit}' (+{_EMPLOYMENT_POINTS})"
                        ),
                        points=_EMPLOYMENT_POINTS,
                    )
                )
            else:
                employment_mismatch = True
                score -= _EMPLOYMENT_POINTS
                components.append(
                    ScoreComponent(
                        kind="employment",
                        label=(
                            f"Employment type mismatch (prefers "
                            f"{', '.join(profile.preferred_employment_types)}) "
                            f"(-{_EMPLOYMENT_POINTS})"
                        ),
                        points=-_EMPLOYMENT_POINTS,
                    )
                )

    # Seniority: +5 on an accepted level, -5 only on an explicit conflicting
    # marker in the title, neutral when the title carries no seniority signal.
    seniority_matched: str | None = None
    seniority_mismatch = False
    if profile.preferred_seniority:
        detected = _detect_seniority(facts.titles)
        accepted_levels = {
            level
            for level in (
                _seniority_level(term) for term in profile.preferred_seniority
            )
            if level is not None
        }
        raw_terms = {_normalize_seniority(term) for term in profile.preferred_seniority}
        hit = next(
            (
                level
                for level in detected
                if level in accepted_levels or level in raw_terms
            ),
            None,
        )
        if hit is not None:
            seniority_matched = hit
            score += _SENIORITY_POINTS
            components.append(
                ScoreComponent(
                    kind="seniority",
                    label=f"Seniority matches '{hit}' (+{_SENIORITY_POINTS})",
                    points=_SENIORITY_POINTS,
                )
            )
        elif detected:
            seniority_mismatch = True
            score -= _SENIORITY_POINTS
            components.append(
                ScoreComponent(
                    kind="seniority",
                    label=(
                        f"Seniority mismatch (posting is '{detected[0]}', profile "
                        f"prefers {', '.join(profile.preferred_seniority)}) "
                        f"(-{_SENIORITY_POINTS})"
                    ),
                    points=-_SENIORITY_POINTS,
                )
            )

    # Soft excluded terms: a bounded penalty, matching the previous milestone.
    matched_excluded: list[str] = []
    seen_excluded: set[str] = set()
    for term in profile.excluded_terms:
        if term in seen_excluded:
            continue
        if term_matches(term, haystack):
            seen_excluded.add(term)
            matched_excluded.append(term)
            if len(matched_excluded) >= _EXCLUDED_CAP:
                break
    for term in matched_excluded:
        score += _EXCLUDED_PENALTY
        components.append(
            ScoreComponent(
                kind="excluded",
                label=f"Contains excluded term '{term}' ({_EXCLUDED_PENALTY})",
                points=_EXCLUDED_PENALTY,
            )
        )

    # Hard exclusions: explicit profile terms that force the displayed score to
    # 0. The raw score is kept as computed for future extensibility.
    matched_hard: list[str] = []
    for term in profile.hard_excluded_terms:
        if term not in matched_hard and term_matches(term, haystack):
            matched_hard.append(term)
    for term in matched_hard:
        components.append(
            ScoreComponent(
                kind="hard_excluded",
                label=f"Hard excluded: contains '{term}'",
                points=0,
                is_veto=True,
            )
        )

    required_set = set(profile.required_skills)
    preferred_not_observed = [
        skill
        for skill in profile.skills
        if skill not in matched_skill_set and skill not in required_set
    ]

    # Review feedback: a conservative bounded adjustment from decided groups.
    # Never a veto; only applied when at least one similar source contributes
    # non-zero points. The candidate's own decisions are excluded inside
    # ``compute_feedback_adjustment``.
    feedback_adjustment: FeedbackAdjustment | None = None
    if feedback is not None:
        if candidate is None:
            raise ValueError("feedback requires candidate evidence")
        adjustment = compute_feedback_adjustment(candidate, feedback.sources)
        if adjustment.points != 0:
            score += adjustment.points
            components.append(
                ScoreComponent(
                    kind="feedback",
                    label=feedback_summary(adjustment),
                    points=adjustment.points,
                )
            )
            feedback_adjustment = adjustment

    return MatchResult(
        score=score,
        matched_titles=tuple(matched_titles),
        matched_skills=tuple(matched_skills),
        missing_required_skills=tuple(missing_required),
        preferred_skills_not_observed=tuple(preferred_not_observed),
        location_matched=location_matched,
        remote_matched=remote_matched,
        employment_matched=employment_matched,
        employment_mismatch=employment_mismatch,
        seniority_matched=seniority_matched,
        seniority_mismatch=seniority_mismatch,
        excluded_terms=tuple(matched_excluded),
        hard_excluded_terms=tuple(matched_hard),
        description_unavailable=description_unavailable,
        components=tuple(components),
        feedback=feedback_adjustment,
    )


def _facts_from_job(job: StoredJob) -> MatchFacts:
    """Build scoring facts from a single stored job."""
    return MatchFacts(
        titles=(job.title,),
        description=job.description,
        location=job.location,
        remote_status=job.remote_status,
        employment_type=job.employment_type,
        published_at=job.published_at,
    )


def score_job(
    job: StoredJob,
    profile: CandidateProfile,
    *,
    feedback: FeedbackSet | None = None,
) -> RankedJob:
    """Score a single job against a candidate profile."""
    return RankedJob(
        job=job,
        result=score_facts(
            _facts_from_job(job),
            profile,
            feedback=feedback,
            candidate=_candidate_from_job(job),
        ),
    )


def _publish_key(published_at: str | None) -> tuple[int, int]:
    """Stable secondary sort key: published desc (NULLS LAST), then id."""
    if published_at is None:
        return (1, 0)
    parsed = parse_utc(published_at)
    if parsed is None:
        return (1, 0)
    return (0, -int(parsed.timestamp()))


def score_jobs(
    jobs: Iterable[StoredJob],
    profile: CandidateProfile,
    *,
    feedback: FeedbackSet | None = None,
) -> list[RankedJob]:
    """Score multiple jobs, sorted deterministically.

    Ordering: descending match score, then publication time (newest first,
    unknown last), then ascending job ID.
    """
    ranked = [score_job(job, profile, feedback=feedback) for job in jobs]
    ranked.sort(
        key=lambda rj: (
            -rj.match_score,
            *_publish_key(rj.job.published_at),
            rj.job.id,
        )
    )
    return ranked


def _facts_from_group(group: GroupedJob) -> MatchFacts:
    """Build scoring facts from a canonical group, unioning member evidence.

    All members contribute evidence so a gap in one source's posting (e.g. a
    missing description) is filled by another. Identical evidence is collapsed
    and skills are counted once by the scorer, so duplicates never inflate the
    score. Attribution (sources/URLs) is carried by ``group`` itself, untouched.
    """
    members = group.members or (group.representative,)
    titles = tuple(dict.fromkeys(member.title for member in members))
    descriptions = tuple(
        dict.fromkeys(member.description for member in members if member.description)
    )
    return MatchFacts(
        titles=titles,
        description="\n".join(descriptions) or None,
        location=next(
            (member.location for member in members if member.location),
            group.representative.location,
        ),
        remote_status=(
            "remote"
            if any(member.remote_status == "remote" for member in members)
            else group.representative.remote_status
        ),
        employment_type=", ".join(
            dict.fromkeys(
                member.employment_type for member in members if member.employment_type
            )
        )
        or None,
        published_at=_newest_published(members),
    )


def _newest_published(members: tuple[StoredJob, ...]) -> str | None:
    """Return the newest parseable ``published_at`` among members, or None."""
    best_key: tuple[int, int] | None = None
    best_value: str | None = None
    for member in members:
        if member.published_at is None:
            continue
        parsed = parse_utc(member.published_at)
        if parsed is None:
            continue
        key = (0, -int(parsed.timestamp()))
        if best_key is None or key < best_key:
            best_key = key
            best_value = member.published_at
    return best_value


def score_group(
    group: GroupedJob,
    profile: CandidateProfile,
    *,
    feedback: FeedbackSet | None = None,
) -> RankedGroup:
    """Score a canonical group against a candidate profile using all members."""
    facts = _facts_from_group(group)
    return RankedGroup(
        group=group,
        result=score_facts(
            facts,
            profile,
            feedback=feedback,
            candidate=_candidate_from_group(group),
        ),
    )


def score_groups(
    groups: Iterable[GroupedJob],
    profile: CandidateProfile,
    *,
    feedback: FeedbackSet | None = None,
) -> list[RankedGroup]:
    """Score multiple groups, sorted deterministically.

    Ordering mirrors flat ranking: descending match score, then newest member
    publication time, then the representative's (lowest) job ID.
    """
    ranked = [score_group(group, profile, feedback=feedback) for group in groups]
    ranked.sort(
        key=lambda rg: (
            -rg.match_score,
            *_publish_key(_newest_published(rg.group.members)),
            rg.group.representative.id,
        )
    )
    return ranked
