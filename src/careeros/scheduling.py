"""Pure scheduling logic for due-source selection.

This module decides which registered sources are due for collection based on
their last completed attempt and their polling policy. It performs no I/O and
never touches the database; callers pass in the last-attempt times.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from careeros.collection_policies import SourcePolicy
from careeros.collectors import CollectorName


def due_sources(
    *,
    now: datetime,
    policies: Mapping[CollectorName, SourcePolicy],
    last_attempts: Mapping[CollectorName, datetime | None],
    order: Sequence[CollectorName],
) -> list[CollectorName]:
    """Return the sources whose polling interval has expired, in ``order``.

    A source with no completed attempt is due immediately. Scheduling uses the
    last completed attempt (success or failure alike), so a failed run delays
    the next automatic attempt by exactly one interval rather than retrying
    continuously.
    """
    due: list[CollectorName] = []
    for source in order:
        last = last_attempts[source]
        if last is None or now - last >= policies[source].min_interval:
            due.append(source)
    return due
