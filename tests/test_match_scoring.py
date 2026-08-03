"""Tests for the deterministic profile-matching scoring engine.

All tests are offline and use synthetic jobs/profiles built directly (no
database, no network).
"""

import json
from pathlib import Path

import pytest

from careeros.database.groups import GroupedJob
from careeros.database.jobs import StoredJob
from careeros.ranking.profile import load_profile
from careeros.ranking.scorer import (
    CandidateProfile,
    score_group,
    score_job,
    score_jobs,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _make_job(
    *,
    job_id: int = 1,
    title: str = "Python Developer",
    company: str | None = "Tech Corp",
    location: str | None = "Athens",
    description: str | None = "Build software with Python.",
    remote_status: str | None = "remote",
    employment_type: str | None = "Full-time",
    published_at: str | None = "2026-07-29T10:00:00Z",
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
        employment_type=employment_type,
        remote_status=remote_status,
        published_at=published_at,
        collected_at="2026-07-29T12:00:00+00:00",
        created_at="2026-07-29T12:00:00+00:00",
        updated_at="2026-07-29T12:00:00+00:00",
    )


def _group(*members: StoredJob) -> GroupedJob:
    ordered = tuple(sorted(members, key=lambda job: job.id))
    sources = tuple(dict.fromkeys(member.source for member in ordered))
    urls = tuple(dict.fromkeys(member.url for member in ordered))
    return GroupedJob(
        group_id="group1",
        representative=ordered[0],
        members=ordered,
        sources=sources,
        urls=urls,
    )


# --- (1) perfect / near-perfect match -------------------------------------


def test_near_perfect_match_scores_100() -> None:
    job = _make_job(
        title="Senior Python Developer",
        location="Athens, Greece",
        description="Python SQL FastAPI AWS Docker Kubernetes",
    )
    profile = CandidateProfile(
        desired_titles=("Python Developer",),
        skills=("Python", "SQL", "FastAPI", "AWS", "Docker", "Kubernetes"),
        required_skills=("Python", "SQL"),
        preferred_locations=("Greece",),
        remote_preference="remote",
        preferred_employment_types=("Full-time",),
        preferred_seniority=("Senior",),
    )

    result = score_job(job, profile)

    assert result.match_score == 100
    assert result.raw_score == 103
    assert set(result.result.matched_skills) == {
        "Python",
        "SQL",
        "FastAPI",
        "AWS",
        "Docker",
        "Kubernetes",
    }


# --- (2) partial skill match -----------------------------------------------


def test_partial_skill_match() -> None:
    job = _make_job(description="Python and SQL are used.")
    profile = CandidateProfile(skills=("Python", "SQL", "Kubernetes", "Rust", "Go"))

    result = score_job(job, profile)

    assert result.result.matched_skills == ("Python", "SQL")
    assert result.result.preferred_skills_not_observed == (
        "Kubernetes",
        "Rust",
        "Go",
    )


# --- (3) missing description / metadata is neutral --------------------------


def test_missing_description_is_neutral() -> None:
    job = _make_job(title="Data Engineer", description=None)
    profile = CandidateProfile(
        skills=("Python",),
        required_skills=("Python", "SQL"),
        preferred_locations=("Berlin",),
    )

    result = score_job(job, profile)

    assert result.result.description_unavailable is True
    assert result.result.missing_required_skills == ("Python", "SQL")
    assert not any("Missing required" in reason for reason in result.reasons)
    assert result.score == 0
    assert result.match_score == 0


def test_missing_location_is_neutral() -> None:
    job = _make_job(location=None)
    profile = CandidateProfile(preferred_locations=("Berlin",))

    result = score_job(job, profile)

    assert result.score == 0
    assert result.reasons == ()


# --- (4) hard exclusion -----------------------------------------------------


def test_hard_excluded_skill_forces_zero() -> None:
    job = _make_job(
        title="Junior Data Intern",
        description="Entry level analysis with Python.",
    )
    profile = CandidateProfile(skills=("Python",), hard_excluded_terms=("intern",))

    result = score_job(job, profile)

    assert result.score == 8  # raw accumulation is retained
    assert result.match_score == 0
    assert result.result.hard_excluded_terms == ("intern",)
    assert any("Hard excluded" in reason for reason in result.reasons)

    assert result.raw_score == 8


# --- (5) preferred title phrase match ----------------------------------------


def test_preferred_title_phrase_match() -> None:
    job = _make_job(title="Senior Machine Learning Engineer")
    profile = CandidateProfile(desired_titles=("Machine Learning Engineer",))

    result = score_job(job, profile)

    assert result.score == 20
    assert result.result.matched_titles == ("Machine Learning Engineer",)


# --- (6) token boundaries: C versus unrelated --------------------------------


def test_c_skill_not_matched_by_false_positives() -> None:
    job = _make_job(description="We use C++ and C#, plus descriptions.")
    profile = CandidateProfile(skills=("C", "C++", "C#"))

    result = score_job(job, profile)

    assert result.result.matched_skills == ("C++", "C#")
    assert "C" not in result.result.matched_skills


def test_c_skill_matches_standalone() -> None:
    job = _make_job(title="C Developer", description="C programming.")
    profile = CandidateProfile(skills=("C",))

    result = score_job(job, profile)

    assert result.result.matched_skills == ("C",)


# --- (7) C++ / C# / .NET / Node.js / CI/CD normalization -------------------


def test_punctuated_technology_normalization() -> None:
    job = _make_job(description="We use C++, C#, dotnet, node.js and ci/cd every day.")
    profile = CandidateProfile(skills=("C++", "C#", ".NET", "Node.js", "CI/CD"))

    result = score_job(job, profile)

    assert result.result.matched_skills == ("C++", "C#", ".NET", "Node.js", "CI/CD")
    assert result.score == 5 * 8


# --- (8) case / punctuation / Unicode / whitespace normalization ------------


def test_case_and_whitespace_normalization() -> None:
    job = _make_job(title="  PYTHON     DEVELOPER  ", description="  PYTHON  ")
    profile = CandidateProfile(
        desired_titles=("python developer",),
        skills=("python",),
    )

    result = score_job(job, profile)

    assert result.score == 20 + 8


def test_unicode_folding_in_matching() -> None:
    job = _make_job(description="Z\u00fcri Loves Python")
    profile = CandidateProfile(skills=("Python",))

    result = score_job(job, profile)

    assert result.result.matched_skills == ("Python",)


# --- (9) remote preference match and conflict ------------------------------


def test_remote_match() -> None:
    job = _make_job(remote_status="remote")
    profile = CandidateProfile(remote_preference="remote")

    result = score_job(job, profile)

    assert result.score == 10
    assert result.result.remote_matched is True


def test_remote_conflict_is_neutral() -> None:
    job = _make_job(remote_status="onsite_or_unspecified")
    profile = CandidateProfile(remote_preference="remote")

    result = score_job(job, profile)

    assert result.score == 0
    assert result.reasons == ()
    assert result.result.remote_matched is False


# --- (10) location: match / generic worldwide / unknown ---------------------


def test_location_match_token_subset() -> None:
    job = _make_job(location="Athens, Greece")
    profile = CandidateProfile(preferred_locations=("Greece",))

    result = score_job(job, profile)

    assert result.score == 15
    assert result.result.location_matched == "Greece"


def test_location_distinct_tokens_do_not_match() -> None:
    job = _make_job(location="New York, USA")
    profile = CandidateProfile(preferred_locations=("Berlin",))

    result = score_job(job, profile)

    assert result.score == 0
    assert result.result.location_matched is None


def test_location_token_subset_within_larger_city() -> None:
    job = _make_job(location="New Berlin, USA")
    profile = CandidateProfile(preferred_locations=("Berlin",))

    result = score_job(job, profile)

    assert result.score == 15
    assert result.result.location_matched == "Berlin"


def test_location_generic_worldwide() -> None:
    job = _make_job(location="Worldwide", remote_status="remote")
    profile = CandidateProfile(preferred_locations=("Worldwide",))

    result = score_job(job, profile)

    assert result.score == 15


def test_location_unknown_is_neutral() -> None:
    job = _make_job(location=None)
    profile = CandidateProfile(preferred_locations=("Greece",))

    result = score_job(job, profile)

    assert result.score == 0
    assert result.result.location_matched is None


# --- (11) seniority match and mismatch ----------------------------------------


def test_seniority_match() -> None:
    job = _make_job(title="Senior Python Developer")
    profile = CandidateProfile(preferred_seniority=("Senior",))

    result = score_job(job, profile)

    assert result.score == 5
    assert result.result.seniority_matched == "senior"


def test_seniority_mismatch() -> None:
    job = _make_job(title="Junior Python Developer")
    profile = CandidateProfile(preferred_seniority=("Senior",))

    result = score_job(job, profile)

    assert result.score == -5
    assert result.result.seniority_mismatch is True
    assert any("Seniority mismatch" in reason for reason in result.reasons)


def test_seniority_absent_is_neutral() -> None:
    job = _make_job(title="Python Developer")
    profile = CandidateProfile(preferred_seniority=("Senior",))

    result = score_job(job, profile)

    assert result.score == 0
    assert result.result.seniority_matched is None
    assert result.result.seniority_mismatch is False


# --- (12) employment type match and unknown -----------------------------------


def test_employment_type_match_normalized() -> None:
    job = _make_job(employment_type="Full Time")
    profile = CandidateProfile(preferred_employment_types=("Full-time",))

    result = score_job(job, profile)

    assert result.score == 5
    assert result.result.employment_matched is not None


def test_employment_type_unknown_is_neutral() -> None:
    job = _make_job(employment_type=None)
    profile = CandidateProfile(preferred_employment_types=("Full-time",))

    result = score_job(job, profile)

    assert result.score == 0
    assert result.result.employment_mismatch is False


def test_employment_type_mismatch() -> None:
    job = _make_job(employment_type="Contract")
    profile = CandidateProfile(preferred_employment_types=("Full-time",))

    result = score_job(job, profile)

    assert result.score == -5
    assert result.result.employment_mismatch is True


# --- (13) score clamping -------------------------------------------------------


def test_score_clamped_above_100() -> None:
    job = _make_job(
        title="Senior Python Engineer",
        location="Athens, Greece",
        remote_status="remote",
        description="Python SQL FastAPI AWS Docker Kubernetes React Go Rust",
    )
    profile = CandidateProfile(
        desired_titles=("Python",),
        skills=("Python", "SQL", "FastAPI", "AWS", "Docker", "Kubernetes"),
        preferred_locations=("Greece",),
        remote_preference="remote",
        preferred_employment_types=("Full-time",),
        preferred_seniority=("Senior",),
    )

    result = score_job(job, profile)

    assert result.raw_score > 100
    assert result.match_score == 100


def test_clamped_below_zero_from_penalties() -> None:
    job = _make_job(title="Junior Intern")
    profile = CandidateProfile(excluded_terms=("junior", "intern", "student"))

    result = score_job(job, profile)

    assert result.raw_score < 0
    assert result.match_score == 0


# --- (14) deterministic reason ordering ---------------------------------------


def test_reason_ordering() -> None:
    job = _make_job(
        title="Senior Python Developer",
        location="Athens",
        description="Python with SQL.",
        employment_type="Full-time",
    )
    profile = CandidateProfile(
        desired_titles=("Python Developer",),
        skills=("Python", "SQL"),
        required_skills=("SQL",),
        preferred_locations=("Athens",),
        remote_preference="remote",
        preferred_employment_types=("Full-time",),
        preferred_seniority=("Senior",),
    )

    result = score_job(job, profile)

    categories = [reason.split(" ")[0] for reason in result.reasons]
    assert categories[:6] == [
        "Title",
        "Has",
        "Has",
        "Location",
        "Remote",
        "Employment",
    ]


# --- (15) deterministic tie-breaking -------------------------------------------


def test_tie_break_published_then_id() -> None:
    newer = _make_job(
        job_id=1,
        title="Python Engineer",
        published_at="2026-07-29T10:00:00Z",
    )
    older = _make_job(
        job_id=2,
        title="Python Engineer",
        published_at="2026-07-28T10:00:00Z",
    )
    profile = CandidateProfile(desired_titles=("Python",))

    results = score_jobs([older, newer], profile)

    assert [r.job.id for r in results] == [1, 2]

    same_published = [
        _make_job(job_id=5, title="Python Engineer"),
        _make_job(job_id=3, title="Python Engineer"),
    ]
    tied = score_jobs(same_published, profile)
    assert [r.job.id for r in tied] == [3, 5]


def test_deterministic_repeated_calls() -> None:
    job = _make_job(title="Python Developer")
    profile = CandidateProfile(skills=("Python",), required_skills=("Python", "SQL"))

    r1 = score_job(job, profile)
    r2 = score_job(job, profile)

    assert r1.score == r2.score
    assert r1.match_score == r2.match_score
    assert r1.reasons == r2.reasons


# --- (16) malformed profile validation (new fields) ------------------------------


def test_rejects_non_list_required_skills(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"required_skills": "Python"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a list"):
        load_profile(path)


def test_rejects_non_string_seniority(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"preferred_seniority": [1, 2]}), encoding="utf-8")
    with pytest.raises(ValueError, match="must contain only strings"):
        load_profile(path)


# --- (17) backward compatibility with the example profile ------------------------


def test_example_profile_loads_and_scores() -> None:
    example = PROJECT_ROOT / "config" / "candidate_profile.example.json"
    profile = load_profile(example)

    job = _make_job(
        title="Senior Data Scientist",
        description="Python Machine Learning SQL",
    )
    result = score_job(job, profile)

    assert result.score == result.score
    assert result.reasons == result.reasons


# --- (18) grouped scoring combines member evidence --------------------------------


def test_grouped_union_of_descriptions() -> None:
    member_a = _make_job(job_id=1, description=None)
    member_b = _make_job(job_id=2, description="Python SQL Docker required.")
    group = _group(member_a, member_b)
    profile = CandidateProfile(skills=("Python", "SQL", "Docker"))

    result = score_group(group, profile)

    assert result.result.matched_skills == ("Python", "SQL", "Docker")
    assert all(
        skill in result.result.matched_skills for skill in ("Python", "SQL", "Docker")
    )


# --- (19) grouped duplicates never inflate skill points ------------------------


def test_grouped_duplicate_skill_not_inflated() -> None:
    member_a = _make_job(job_id=1, description="Python Python Python")
    member_b = _make_job(job_id=2, description="Python everywhere")
    group = _group(member_a, member_b)
    profile = CandidateProfile(skills=("Python",))

    result = score_group(group, profile)

    assert result.result.matched_skills == ("Python",)
    skill_reasons = [r for r in result.reasons if r.startswith("Has skill")]
    assert len(skill_reasons) == 1


# --- (20) flat and grouped use the same engine ----------------------------------


def test_flat_and_grouped_single_member_agree() -> None:
    job = _make_job(job_id=1, description="Python and SQL backend.")
    group = _group(job)
    profile = CandidateProfile(skills=("Python", "SQL"), remote_preference="remote")

    flat = score_job(job, profile)
    grouped = score_group(group, profile)

    assert flat.match_score == grouped.match_score
    assert flat.reasons == grouped.reasons


# --- sanity: empty profile is neutral --------------------------------------------


def test_empty_profile_neutral() -> None:
    job = _make_job(title="Python Developer")
    result = score_job(job, CandidateProfile())

    assert result.score == 0
    assert result.reasons == ()
