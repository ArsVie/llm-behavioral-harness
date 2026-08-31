"""Misc builders consolidated from per-file duplicates."""

from __future__ import annotations


def bucket_counts(profile):
    counts = {"exact": 0, "adjacent": 0, "independent": 0}
    for interest in profile.interests:
        counts[interest.bucket] += 1
    return counts
