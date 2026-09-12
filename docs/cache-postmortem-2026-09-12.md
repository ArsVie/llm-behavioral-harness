---
type: postmortem
title: Why the prompt cache hits so little, measured against DeepSeek Harness
date: 2026-09-12
status: working
---

# Cache postmortem: our model loop vs DeepSeek Harness

Live run (`results/live-companion/companion.db`): **16.2%** over 43,376 prompt
tokens — the same bodies reach **87.6%** when replayed. That single contrast is
the whole finding, and it clears our request shape of the charge.

## Correction: the shape was never the problem

An earlier version of this note claimed a volatile block sat MID-ARRAY and paid
for it. That was an inference from a position, not from bytes. Comparing the
stored request envelopes of every consecutive pair — 14 calls, chat→chat and
chat→decide alike — the arrays are **byte-identical up to the trailing state
card**, which is where the entire request diverges:

    #2 chat  6 msgs  ->  #3 chat 10 msgs     first difference at msg 5  (the card)
    #3 chat 10 msgs  ->  #4 chat 14 msgs     first difference at msg 9  (the card)
    #12 chat 36 msgs ->  #13 decide 37 msgs  first difference at msg 35 (the card)
    #13 decide 37    ->  #14 decide 37       first difference at msg 36 (the card)

A card that sits mid-history is not volatile: it is frozen when written and
re-sent unchanged. We already have the append-only request DeepSeek Harness
asserts on every call after the first ("the first request has nothing to hit"),
and it is now pinned by
`tests/test_system_adjacency_2026_09_08.py::test_a_popup_call_extends_the_mainline_request`.

## The three candidate causes, tested and dismissed

1. **The tools asymmetry.** A pop-up call offers its function schema; the
   mainline runs textual with `tools=None`. Six rounds, each on a FRESH
   prefix, three requests per round (`/tmp/cache_shape_rounds.py`):

       1. no tools (cold)          cached 0     0.0%
       2. tools after it           cached 1536  86.2%
       3. no tools after tools     cached 1536  86.2%

       tools-after-no-tools: 6/6 rounds shared the prefix
       no-tools-after-tools: 6/6 rounds shared the prefix

   Twelve cross-shape measurements, one answer: the block is served, not
   broken, and the tool menu needs no change. The narrowed menu keeps
   protecting the pop-up from the wrong function.

   **Single readings on this gateway are not evidence.** An earlier one-shot
   run of the same probe recorded **0%** for the tools request and the repeat
   of the identical script recorded 79.6%; twelve fresh-prefix rounds then
   recorded 86.2% every time. One number here can be a cold route as easily as
   a real effect, so anything claimed about this cache needs repeats.

2. **A gap.** Same prefix, extended 2 s later and again after a real 300 s gap
   (`/tmp/cache_ttl.py`): 81.3% → **80.4%** → 80.4%. Five minutes is well
   inside the window.

3. **The request bodies.** Every stored production request, replayed in order
   against the same model (`/tmp/cache_replay_all.py`):

       call  lane                    live    replay   prompt
       1     chat                    99.0%    35.4%      724
       2     chat                    37.0%    52.2%      981
       6     chat                    17.7%    85.5%     2095
       9     chat                    16.1%    86.8%     3098
       12    chat                    10.8%    96.2%     4659
       13    tool_decide_event        6.7%    97.4%     5251
       15    tool_decide_proactive   17.5%    93.7%     5329

       live cached 7,040 vs replay cached 38,013 over 43,376 prompt tokens:
       **16.2% -> 87.6%**

   Identical bytes. The only difference is that the replay sent them seconds
   apart, in order, while the live run sent them minutes apart.

## What is actually happening (revised — two earlier conclusions retracted)

The "expired extension / pacing" reading below is WRONG, and so was the earlier
"volatile mid-array block". Measured, with the fresh-prefix and no-sleep controls
that settle each one:

- **Time is not the constraint.** `GAP_S=1500` (25 minutes, the run's real
  inter-turn spacing): 82.3% -> 81.3% after 2 s -> **80.4% after 1,500 s** ->
  80.4% on the repeat. The provider cache is content-keyed and long-lived — the
  probe's own "cold start" reading of 82.3% proved it, because that filler is a
  constant re-sent by every earlier run of the same script.
- **Back-to-back calls are not the constraint.** A fresh prefix's first send
  reads ~4.9%; the SAME prefix re-sent with 0.0 s, 1.0 s and 3.0 s gaps reads
  **97.4% every time** (`/tmp/cache_burst.py`). No population lag.
- **The tools payload is not the constraint.** The real stored bodies replayed
  WITH the real one-function payload: 85.9% first send, 97.2% on the repeat
  (`/tmp/cache_live_tools.py`); 12 fresh-prefix cross-shape rounds at 86.2%
  (`/tmp/cache_shape_rounds.py`); a leg-kind switch with the switched payload at
  96.8% (`/tmp/cache_kind_switch.py`).
- **The system block is not the constraint.** Byte-identical across all 23
  consecutive live pairs, and the message arrays diverge only near the tail
  (`/tmp/live_prefix_offline.py`, `/tmp/live_divergence.py`).

The live bodies therefore re-measure at **83-97%**, and the run's own later
stretch (ids 17-24, decide legs) already recorded 80-97%.

### The number itself is not trustworthy

All 24 rows of that run carry `cache_miss_tokens = 0`, i.e. `cached + miss !=
prompt` on EVERY row: they were written by a build that trusted commandcode's
always-zero `cache_creation_input_tokens` (the bug fixed in `_parse_cache_split`,
which now derives `miss = prompt - cached`). A run measured by a build with a
known-broken split cannot settle the harness's cache behaviour, so both the 16.2%
aggregate and the live column in the table above are unusable as measurements:
they are the numbers of a build that could not count.

### What remains, honestly

Same bytes, same tools, same system, no timing sensitivity — and the live run
still read 4.7-6.7% on calls that re-measure at 83-97%. The residual is
provider-side and unreproduced. One experiment settles it before anyone touches
the assembler: log `(prompt_hash, cached, miss, tools_hash, temperature,
reasoning_effort)` per live call and replay the identical bytes in the same
process. If the replay is warm while the live call was cold on identical bytes,
the cause is the gateway's routing/cache partition and no harness change fixes it.

## Levers (revised)

1. **Verify before optimizing: re-measure on a run written by the fixed build.**
   Rows must satisfy `cached + miss == prompt`. Until then the run's hit rate is
   unknowable rather than low.
2. **Nothing structural.** The append-only property holds (byte-stable system,
   arrays diverging only at the tail) and is guarded by a test.
3. **Prompt size is the cost knob that always pays.** A turn that starts cold
   re-bills its whole prompt; shrinking the steady-state prompt is the only lever
   independent of cache warmth.
4. **Keep the head byte-stable.** The system block is the floor of every
   measurement; the day-start persona rewrite costs one full miss per day.
5. Keep the inspector's `prefix-shared-but-not-cached` check — the port of
   DeepSeek's `cacheReadTokens > 0` assertion — but read it only on runs whose
   split adds up.

## Reproduce

    .venv/bin/python /tmp/leg_diff.py /tmp/verify-live.db        # where arrays diverge
    .venv/bin/python /tmp/cache_measure.py                       # per-leg hits
    .venv/bin/python /tmp/cadence.py                             # gap vs hit
    set -a; . ./.env; set +a; .venv/bin/python /tmp/cache_cross.py   # tools, both ways
    set -a; . ./.env; set +a; GAP_S=300 .venv/bin/python /tmp/cache_ttl.py
    set -a; . ./.env; set +a; .venv/bin/python /tmp/cache_replay_all.py 24
    .venv/bin/python /tmp/live_rows_valid.py                        # do the rows add up?
    .venv/bin/python /tmp/live_prefix_offline.py                    # is the system byte-stable?
    .venv/bin/python /tmp/live_divergence.py                        # where arrays diverge, element-wise
    set -a; . ./.env; set +a; .venv/bin/python /tmp/cache_burst.py      # gap 0/1/3 s on a fresh prefix
    set -a; . ./.env; set +a; .venv/bin/python /tmp/cache_live_tools.py # real bodies + real tools
    set -a; . ./.env; set +a; .venv/bin/python /tmp/cache_kind_switch.py # per-kind payload
