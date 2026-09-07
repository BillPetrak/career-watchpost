from pathlib import Path

STATIC = Path(__file__).parents[1] / "src/careeros/api/static"


def js() -> str:
    return (STATIC / "app.js").read_text()


def test_preparation_job_detail_contract_and_ordering() -> None:
    source = js()
    assert 'preparation: "/applications/" + jobId + "/preparation"' in source
    assert (
        'var keys = ["job", "group", "application", "preparation", '
        '"package", "review", "reminders"];' in source
    )
    assert (
        'fillDetailSection(content, job, "application", results.application);' in source
    )
    assert (
        'fillDetailSection(content, job, "preparation", results.preparation);' in source
    )
    assert 'fillDetailSection(content, job, "package", results.package);' in source
    assert "Preparation is unavailable until you explicitly track" in source
    assert "X-CareerOS-Preparation-Revision" in source
    assert '"X-CareerOS-Write": "1"' in source


def test_preparation_accessibility_privacy_and_deadline_safety() -> None:
    source = js()
    assert "fieldset" in source and "legend" in source
    assert "Confirm requirement" in source and "Clear item confirmation" in source
    assert '"Reapply unsent change"' in source
    assert '"CareerOS records what you captured; it has NOT verified' in source
    assert "authoritative external source" in source
    assert '["open", "not_applicable"]' in source
    for forbidden in (
        ".innerHTML",
        "localStorage",
        "sessionStorage",
        "document.cookie",
        ".sqlite",
    ):
        assert forbidden not in source
    assert "Create reminder…" in source
    assert "No reminder was created." in source


def test_preparation_reminder_handoff_is_explicit_and_deadline_only() -> None:
    source = js()
    start = source.index("function focusReminderFromRequirement")
    end = source.index("function buildRequirementRow", start)
    handoff = source[start:end]
    assert 'reminderForm.querySelector("#reminder-due")' in handoff
    assert "localDatetimeValue(item.deadline_at)" in handoff
    assert 'new Event("input", { bubbles: true })' in handoff
    assert "due.focus()" in handoff
    assert "fetch(" not in handoff
    assert "postJson(" not in handoff
    assert "item.label" not in handoff
    assert "item.detail" not in handoff
    assert "var reminderJump" not in source
