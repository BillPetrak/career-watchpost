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


# Jobicy's 60-minute minimum interval is a documented fair-use requirement,
# and Remotive's 6-hour minimum honors its "no more than four times a day"
# guidance. Himalayas' 24-hour minimum honors its documented statement that the
# feed is refreshed every 24 hours and that there is no benefit to polling more
# frequently than once per day. The intervals for arbeitnow, remoteok,
# weworkremotely, and workingnomads are PROVISIONAL defaults, not implied
# production values; they are deliberately conservative and can be tuned per
# source. Working Nomads publishes no rate-limit guidance: its collector targets
# an undocumented internal search endpoint, so its 6-hour floor is
# intentionally cautious.
SOURCE_POLICIES: dict[CollectorName, SourcePolicy] = {
    "arbeitnow": SourcePolicy("arbeitnow", timedelta(hours=6)),
    "remoteok": SourcePolicy("remoteok", timedelta(hours=4)),
    "weworkremotely": SourcePolicy("weworkremotely", timedelta(hours=6)),
    "jobicy": SourcePolicy("jobicy", timedelta(minutes=60), documented=True),
    "remotive": SourcePolicy("remotive", timedelta(hours=6), documented=True),
    "workingnomads": SourcePolicy("workingnomads", timedelta(hours=6)),
    "himalayas": SourcePolicy("himalayas", timedelta(days=1), documented=True),
    "lever": SourcePolicy("lever", timedelta(hours=6)),
    "greenhouse": SourcePolicy("greenhouse", timedelta(hours=6)),
    # Workable publishes no public-feed polling cadence; this is provisional.
    "workable": SourcePolicy("workable", timedelta(hours=6)),
    # SmartRecruiters publishes no polling cadence; this is provisional.
    "smartrecruiters": SourcePolicy("smartrecruiters", timedelta(hours=6)),
}
