# behavioral_signature

Deterministic behavioral-signature extractor for conversation logs.

**Built once, used twice** (honors never-diverge): this package is (a) the
product surface that computes the companion's behavioral signature from any
conversation log, and (b) the codebook experiment's **H4 evaluator** — the
metric surface it consumes to judge "codebook vs renderer" behavior. The
public API below is the **stable shared contract**: the experiment imports
this package; it does not reimplement the metrics. Change the contract only
through a wave plan, and change it in both consumers at once.

Stdlib only (`json`, `datetime`, `statistics`-free pure Python, `dataclasses`).
No harness, numpy, or pandas imports — importable by the experiment without
coupling.

## Public API (verbatim)

```python
from behavioral_signature import (
    LogTurn,            # frozen dataclass: speaker, text, t_h, timestamp, turn_index, conversation_id
    LogRecord,          # frozen dataclass: conversation_id, turns: tuple[LogTurn, ...], opened_t_h, closed_t_h
    compute_signature,  # (record: LogRecord | Sequence[LogTurn]) -> Signature (8-float dict, 6dp-rounded)
    Signature,          # dict[str, float], keys exactly METRIC_NAMES
    METRIC_NAMES,       # canonical order: contact_frequency, initiative, warmth, verbosity,
                        #   latency, topic_selection, persistence, reactivity
    signature_to_json,  # (sig) -> str, byte-deterministic (sorted keys, indent=2)
    log_to_json,        # (record) -> str, canonical JSON (fixed field order, None omitted)
    log_from_json,      # (text) -> LogRecord, inverse of log_to_json
    time_kind,          # (record) -> 'datetime' | 't_h' — which time source the log uses
)
```

Canonical JSON document shape:

```json
{
  "conversation_id": "conv-3",
  "opened_t_h": 13.544419927199682,
  "closed_t_h": 15.415875082255857,
  "turns": [
    {"speaker": "user", "text": "Hi?", "t_h": 13.544419927199682, "turn_index": 0}
  ]
}
```

## Time sources (never mixed)

* `t_h` — absolute virtual hours since simulation start (harness convention;
  what the live trial DB stores today, pre-S1).
* `timestamp` — real wall-clock `datetime` (tz-aware), for post-S1 logs.

Rule: time-based metrics use real `timestamp` **only when every turn carries
one**; otherwise they fall back to `t_h`. Mixed logs are treated as `t_h`
logs. Turns lacking the chosen source are skipped by time-based metrics.

## The 8 metrics (operational definitions)

| metric | definition | range |
|---|---|---|
| contact_frequency | total turns ÷ elapsed span (first→last turn, chosen time source) | ≥0 turns/hour |
| initiative | companion turns that open an exchange (not preceded by a user turn) ÷ companion turns | 0..1 |
| warmth | companion turns containing ≥1 WARM_TOKENS token ÷ companion turns | 0..1 |
| verbosity | mean whitespace-separated words per companion turn | ≥0 |
| latency | median companion reply delay (companion turn − preceding user turn), clamped ≥0 | ≥0 hours |
| topic_selection | companion turns whose content words share none with the preceding user turn ÷ companion turns (no preceding user → shift) | 0..1 |
| persistence | companion turns after the first sharing ≥1 content word with any earlier companion turn ÷ companion turns after first | 0..1 |
| reactivity | user turns immediately followed by a companion turn ÷ user turns | 0..1 |

Content words = lowercase, apostrophes stripped, split on non-alphanumerics,
1-char tokens and the frozen `STOPWORDS` set dropped. `WARM_TOKENS` and
`STOPWORDS` are frozen constants — part of the contract. All values are
rounded to 6 decimal places. Channel names align with
`harness.behavior.derive_behavior` (initiative / reactivity / persistence /
warmth) but are measured from the log surface, not derived from engine state.

## conv-3 fixture (G4)

`tests/fixtures/conv3_log.json` — the 7 turns of live conversation conv-3,
exported **read-only** from the live trial DB. No turns padded or synthesized:
greeting exchange (t_h 13.544), the proactive turn (t_h ≈ 15.416), user "not
feeling it", river-trail reply. Regenerate with:

```bash
cd /home/vruizes/.hermes/projects/llh-wt-w4
/home/vruizes/.hermes/projects/llm-behavioral-harness/.venv/bin/python \
    -m behavioral_signature.export \
    --db /home/vruizes/.hermes/projects/llm-behavioral-harness/results/live-companion/companion.db \
    --conv conv-3
```

(The DB lives only in the main checkout — worktrees do not share untracked
files — hence the absolute `--db` path. The exporter always opens it with
`file:...?mode=ro`; it never writes to the DB.) The pinned golden signature
lives in `tests/test_behavioral_signature.py::test_golden_conv3_signature`.

## Codebook experiment (H4) consumption

```python
import json
from behavioral_signature import compute_signature, log_from_json

record = log_from_json(open("tests/fixtures/conv3_log.json").read())
sig = compute_signature(record)   # deterministic; same log → same signature
```
