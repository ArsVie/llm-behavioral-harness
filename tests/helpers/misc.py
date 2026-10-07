"""Misc builders consolidated from per-file duplicates (test_persona,
test_bootstrap)."""

from __future__ import annotations


def bucket_counts(profile):
    """How many of a persona profile's interests fall in each bucket."""
    counts = {"exact": 0, "adjacent": 0, "independent": 0}
    for interest in profile.interests:
        counts[interest.bucket] += 1
    return counts
