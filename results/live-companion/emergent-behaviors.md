# Emergent behaviors — live week-1 (seed 8001)

Observed, not designed. Each entry: what happened, where the receipt is.

## 2026-09-04 — she reasons alone, and it must stay that way

At first conversation open (day 0, 21:44) the boundary sweep fast-forwarded
the whole day: 8 start/end popups decided in queue order, all verdicts
persisted (`decision_records` 1–8). One stale initiate (weekend-market
start, 10:08 window, decided 11h late) leaked its raw reason to Telegram
through the go path — a reply to nobody, since no conversation was live
when the steer arose.

Rule going in (`Session._omit_backlog_send`): verdicts are decided and
persisted, but an initiate reason only reaches the channel when its steer
was born inside the live conversation. Backlog catch-up stays internal.

Receipt: `decision_records` 1–8, `state_events` (`decision_catchup_omit`),
minus one unpersisted Telegram send that motivated the rule.
