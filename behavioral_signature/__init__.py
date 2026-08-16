"""behavioral_signature — deterministic behavioral signature extractor.

Public API (stable shared contract — see README.md):

    LogTurn, LogRecord          canonical conversation-log model
    compute_signature(record)   the 8-metric signature dict
    Signature                   dict[str, float] type alias
    METRIC_NAMES                canonical metric key order
    signature_to_json(sig)      byte-deterministic serialized signature
    log_to_json(record)         canonical JSON for a conversation log
    log_from_json(text)         parse canonical JSON back into a LogRecord
    time_kind(record)           which time source the log uses ('t_h' | 'datetime')

Built once, used twice: product surface AND the codebook experiment's H4
evaluator. Stdlib only — no harness, numpy, or pandas imports.
"""

from __future__ import annotations

from .log import LogRecord, LogTurn, log_from_json, log_to_json, time_kind
from .metrics import (
    METRIC_NAMES,
    Signature,
    compute_signature,
    signature_to_json,
)

__version__ = "0.1.0"

__all__ = [
    "LogRecord",
    "LogTurn",
    "METRIC_NAMES",
    "Signature",
    "compute_signature",
    "log_from_json",
    "log_to_json",
    "signature_to_json",
    "time_kind",
    "__version__",
]
