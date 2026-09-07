from __future__ import annotations

import pytest

from careeros.application_packages.compatibility import requirement_compatibility


def req(kind: str, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": kind,
        "label": "Question",
        "detail": "Detail",
        "fulfillment_state": "open",
        "required": True,
        "deadline_at": None,
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    ("old", "current", "expected"),
    [
        (req("material"), req("material"), (True, False, False)),
        (req("material"), req("material", label="Changed"), (False, False, False)),
        (req("question"), req("question"), (False, True, True)),
        (req("question"), req("question", label="Changed"), (False, True, False)),
        (req("question"), req("question", required=False), (False, True, True)),
        (
            req("question"),
            req("question", fulfillment_state="satisfied"),
            (False, True, True),
        ),
        (
            req("question"),
            req("question", deadline_at="2030-01-01T00:00:00+00:00"),
            (False, True, True),
        ),
        (
            req("question"),
            req("question", fulfillment_state="not_applicable"),
            (False, False, False),
        ),
        (req("question"), req("link"), (False, False, False)),
        (req("task"), req("task"), (False, False, False)),
    ],
)
def test_frozen_compatibility_matrix(old, current, expected):
    assert requirement_compatibility(old, current) == expected
