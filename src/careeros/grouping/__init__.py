"""Deterministic cross-source duplicate detection for CareerOS.

This package provides conservative, dependency-free (stdlib-only) logic for
detecting when two vacancies published by different sources refer to the same
underlying job. It never mutates job rows; grouping is represented separately
via content-addressed canonical group IDs.
"""
