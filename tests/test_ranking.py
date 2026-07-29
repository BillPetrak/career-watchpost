"""Tests for deterministic job ranking."""

from careeros.database.jobs import StoredJob
from careeros.ranking.scorer import (
    CandidateProfile,
    score_job,
    score_jobs,
)


def _make_job(
    *,
    job_id: int = 1,
    title: str = "Python Developer",
    company: str | None = "Tech Corp",
    location: str | None = "Athens",
    description: str | None = "Build software with Python.",
    remote_status: str | None = "remote",
) -> StoredJob:
    return StoredJob(
        id=job_id,
        source="test",
        source_job_id=f"test-{job_id}",
        title=title,
        company=company,
        location=location,
        description=description,
        url="https://example.com",
        employment_type="Full-time",
        remote_status=remote_status,
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-07-29T12:00:00+00:00",
        created_at="2026-07-29T12:00:00+00:00",
        updated_at="2026-07-29T12:00:00+00:00",
    )


def test_title_token_match() -> None:
    job = _make_job(title="Senior Python Developer")
    profile = CandidateProfile(desired_titles=("Python Developer",))

    result = score_job(job, profile)

    assert result.score == 20
    assert any("Python Developer" in r for r in result.reasons)


def test_title_no_false_positive_substring() -> None:
    job = _make_job(title="Pythonista Developer")
    profile = CandidateProfile(desired_titles=("Python",))

    result = score_job(job, profile)

    assert result.score == 0
    assert result.reasons == ()


def test_skill_weight_and_cap() -> None:
    job = _make_job(
        title="Full Stack Developer",
        description="Python Java JavaScript TypeScript React Node.js Go Rust",
    )
    profile = CandidateProfile(
        skills=(
            "Python",
            "Java",
            "JavaScript",
            "TypeScript",
            "React",
            "Node.js",
            "Go",
            "Rust",
        ),
    )

    result = score_job(job, profile)

    assert result.score == 6 * 8  # 48, capped at 6
    assert len([r for r in result.reasons if r.startswith("Has skill")]) == 6


def test_duplicate_skill_not_double_counted() -> None:
    job = _make_job(
        title="Python Developer",
        description="Python developer wanted. Must know Python.",
    )
    profile = CandidateProfile(skills=("Python",))

    result = score_job(job, profile)

    assert result.score == 8  # one skill match, counted once despite multiple mentions
    skill_reasons = [r for r in result.reasons if r.startswith("Has skill")]
    assert len(skill_reasons) == 1


def test_location_match() -> None:
    job = _make_job(location="Athens, Greece")
    profile = CandidateProfile(preferred_locations=("Greece",))

    result = score_job(job, profile)

    assert result.score == 15
    assert any("Greece" in r for r in result.reasons)


def test_remote_preference() -> None:
    job = _make_job(remote_status="remote")
    profile = CandidateProfile(remote_preference="remote")

    result = score_job(job, profile)

    assert result.score == 10
    assert any("Remote" in r for r in result.reasons)


def test_remote_preference_no_match() -> None:
    job = _make_job(remote_status="onsite_or_unspecified")
    profile = CandidateProfile(remote_preference="remote")

    result = score_job(job, profile)

    assert result.score == 0
    assert result.reasons == ()


def test_excluded_term_penalty_and_cap() -> None:
    job = _make_job(
        title="Junior Intern",
        description="Entry level internship for students.",
    )
    profile = CandidateProfile(
        excluded_terms=("junior", "intern", "entry", "student"),
    )

    result = score_job(job, profile)

    assert result.score == -15  # -5 * 3, capped
    excluded_reasons = [r for r in result.reasons if r.startswith("Contains")]
    assert len(excluded_reasons) == 3


def test_case_insensitive_matching() -> None:
    job = _make_job(title="SENIOR PYTHON DEVELOPER")
    profile = CandidateProfile(
        desired_titles=("python developer",),
        skills=("python",),
    )

    result = score_job(job, profile)

    assert result.score == 20 + 8  # title + skill


def test_empty_profile() -> None:
    job = _make_job(title="Python Developer")
    profile = CandidateProfile()

    result = score_job(job, profile)

    assert result.score == 0
    assert result.reasons == ()


def test_stable_reason_ordering() -> None:
    job = _make_job(
        title="Python Developer",
        location="Athens",
        description="Python development with Django.",
        remote_status="remote",
    )
    profile = CandidateProfile(
        desired_titles=("Python Developer",),
        skills=("Python", "Django"),
        preferred_locations=("Athens",),
        remote_preference="remote",
    )

    result = score_job(job, profile)
    categories = [r.split(" ")[0] for r in result.reasons]

    assert categories == ["Title", "Has", "Has", "Location", "Remote"]


def test_score_jobs_descending_order() -> None:
    high = _make_job(job_id=1, title="Python Developer", description="Python")
    mid = _make_job(job_id=2, title="Java Developer", description="Java")
    low = _make_job(job_id=3, title="Rust Engineer", description="Rust")
    profile = CandidateProfile(desired_titles=("Python Developer",))

    results = score_jobs([low, high, mid], profile)

    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_deterministic_tie_break() -> None:
    job_a = _make_job(job_id=2, title="Python Developer")
    job_b = _make_job(job_id=1, title="Python Engineer")
    profile = CandidateProfile(desired_titles=("Python",))

    results = score_jobs([job_a, job_b], profile)

    assert results[0].job.id == 1  # lower ID first
    assert results[1].job.id == 2
    assert results[0].score == results[1].score


def test_deterministic_repeated_calls() -> None:
    job = _make_job(title="Python Developer")
    profile = CandidateProfile(
        desired_titles=("Python Developer",),
        skills=("Python",),
        preferred_locations=("Athens",),
        remote_preference="remote",
        excluded_terms=("junior",),
    )

    r1 = score_job(job, profile)
    r2 = score_job(job, profile)

    assert r1.score == r2.score
    assert r1.reasons == r2.reasons
