"""Source polling policies for scheduled collection.

This module is configuration only: it defines how often each registered job
source may be polled. Scheduling logic lives in ``careeros.scheduling`` and
consumes this configuration; polling intervals must not be hardcoded
elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from careeros.collectors import CollectorName


@dataclass(frozen=True)
class SourcePolicy:
    """Minimum polling interval for a single job source.

    ``documented`` records whether the interval is a documented fair-use
    requirement (jobicy) or a provisional default; it is informational and
    does not affect scheduling.
    """

    source: CollectorName
    min_interval: timedelta
    documented: bool = False


# Jobicy's 60-minute minimum interval is a documented fair-use requirement.
# The intervals for arbeitnow, remoteok, and weworkremotely are PROVISIONAL
# defaults, not implied production values; they are deliberately conservative
# and can be tuned per source.
SOURCE_POLICIES: dict[CollectorName, SourcePolicy] = {
    "arbeitnow": SourcePolicy("arbeitnow", timedelta(hours=6)),
    "remoteok": SourcePolicy("remoteok", timedelta(hours=4)),
    "weworkremotely": SourcePolicy("weworkremotely", timedelta(hours=6)),
    "jobicy": SourcePolicy("jobicy", timedelta(minutes=60), documented=True),
}
