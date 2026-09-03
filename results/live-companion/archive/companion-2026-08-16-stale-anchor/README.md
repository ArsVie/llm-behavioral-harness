# Archived trial DB — 2026-08-16 run

Moved aside on 2026-09-02. This database holds the first live Telegram
exchange (6 messages, 3 LLM calls) and a real-time anchor stamped
2026-08-16 00:59 America/Chihuahua, with only day 0 in `daily_state`.

Resuming it would have started week 1 on fabricated history: the anchor maps
the current wall clock to virtual day ~18, so the first midnight rollover
calls `Session.ensure_day(18)`, which loops `finalize_day` + `_rollover` for
every missing day — verified on a copy, it wrote daily_state rows 0..18 in
1.5 s, each one scored by the judge. `experiments/live_companion.py` now
refuses this resume (`check_resume_gap`) instead of doing it silently.

Kept for the transcript and the llm_calls ledger. Do not resume it.
