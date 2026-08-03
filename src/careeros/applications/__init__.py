"""Application-tracking workflow layer."""

from __future__ import annotations

from careeros.applications.models import APPLICATION_STATUSES, validate_status

__all__ = ["APPLICATION_STATUSES", "validate_status"]
