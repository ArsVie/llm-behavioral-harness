# Decisions that lived in code, not in docs (2026-09-13)

Result of the comment-trim sweep across `harness/`, `observability/`,
`experiments/`, and `tests/` (owner directive 2026-09-13: architectural
decisions must not live in code comments; comments must not be narration;
plain comments max 2 lines). Superseded rationale/history was removed from
code; the entries below were **decision-bearing content that is NOT reflected
in any repo doc** (`docs/*.md`, `docs/internal/*.md`, `README.md`,
`CONVENTIONS.md`). Each is a candidate to fold into the proper doc.

## harness/

- `harness/audit.py:140-143` — turn-to-call matching is by (day, t_h) with a response-equality tiebreak because the reply message is persisted before its call row.
- `harness/bootstrap.py:141-156` — the stored interest sentence is carried across VERBATIM (built in draw order, stored name-sorted; recomputing it rewrote interests).
- `harness/bootstrap.py:235-242` — persona file is authoritative on warm starts too; before this the voice existed only as a hand-edited DB row.
- `harness/channels/telegram.py:149-156` — single-poller flock per bot token: Telegram gives each update to one getUpdates consumer; a second instance is refused loudly (Conflict-loops both otherwise).
- `harness/channels/telegram.py:17-20` — opt-in HARNESS_TYPING refreshes the typing chat action every 4.5 s inside typing_context; off by default means no chat actions ever.
- `harness/channels/telegram.py:233-237` — inbound is fail-open when TELEGRAM_CHAT_ID is unset (any chat can talk to the companion); set the var to lock it down.
- `harness/client.py:588-595` — streaming transport errors are deliberately not caught: the endpoint is unreachable and a non-streaming call would fail the same way.
- `harness/clock.py:1-6` — virtual clock is the only time source; the engine never reads real time.
- `harness/concurrency.py:1-8,45-61` — one owned ThreadPoolExecutor per runtime (the asyncio default is a process-global that outlives it) plus shutdown ordering: executor first, then owned resources; injected ones never closed.
- `harness/concurrency.py:25-43` — SQLite thread/connection ownership contract: the store owns the DB+schema, the runtime owns the re-opened connection, one asyncio.Lock serializes access, the runtime never closes the store.
- `harness/config.py:1-8` — one active channel per process; HARNESS_CHANNEL read by caller; lazy channel imports.
- `harness/domain.py:1-4` — UserAffectObservation and CompanionBehaviorState are distinct types with no implicit conversion; any mapping must be an explicit transformation elsewhere.
- `harness/interest_extension.py:124-127` — proposal-cache schema marker must be bumped whenever the prompt or accepted shape changes so a proposal answered under other instructions is never reused.
- `harness/interests.py:1-4` — the hubless island exists so the independent bucket is always fillable: sample_independent can never come up empty.
- `harness/life.py:579-590` — replenishment policy exists so active life never permanently dies; spawn is certain with zero active arcs, boosted by recent good days, candidates in a fixed origin order.
- `harness/memory.py:1-5` — duck-typed store contract: the exact method set plus which optional kwargs are detected by signature (session_id, category, supersede provenance).
- `harness/memory.py:1-5,91-102` — the topicality boost applies ONLY under the separately named experimental policy; under STRUCTURED_MEMORY the 0.35/0.30/0.35 formula is applied exactly.
- `harness/metering.py:39-41` — chat_stream deliberately excluded from the metered methods.
- `harness/negotiation_contract.py:37-42` — the END boundary is not a phase and never calls the model; a fully passed window resolves in life.transition_past_windows (planned -> completed).
- `harness/negotiation_episodes.py:1-15` — memory.py is must-not-touch; store.insert_episode is the only adapter seam, with ids/summaries derived from episode fields so a re-emission upserts one row.
- `harness/persona_file.py:3-16` — authored voice lives in a config-named file, authoritative on every start; a 2026-09-08 DB reset had erased a hand-edited persona.core.
- `harness/proactive.py:1-21` — candidates are store-backed only (no imports from life.py/memory.py) and hooks are composed deterministically from source fields so the content gate can re-derive them.
- `harness/proposal_cache.py:9-26` — cache lives on disk, not in the store (store absent at onboarding); key is the question: order-normalized digest + per-namespace schema string.
- `harness/reset.py:73-77` — CLEAR_TABLES listed rather than inferred; unlisted tables still cleared and printed.
- `harness/reset.py:158-170` — active_writer needs both fd scan and cmdline; the store has no advisory lock.
- `harness/routine_setup.py:3-17` — routine catalog is model-built at onboarding instead of the shared six hardcoded rows; day-specific names rejected (no weekday awareness in the engine).
- `harness/routine_setup.py:87-94` — setup budget is cold-start only, never reused on a live turn (would block a turn); SETUP_REASONING_EFFORT deliberately not read from HARNESS_THINKING_EFFORT.
- `harness/routine_setup.py:284-288` — routine-catalog cache keyed on the interest SET only; the display name is excluded so the cache does not fragment.
- `harness/runtime.py:135-136` — turn-failure policy: bounded experiment cells fail fast on a broken turn; the live companion reports the failure and stays up.
- `harness/runtime.py:164-169` — the store connection re-opened for worker-thread session calls is registered owned=False because SQLiteStore.close() owns it.
- `harness/runtime.py:265-278` — the retarget-capable sleep is sliced, not raced against an asyncio.Event (the injected wall-clock sleeper does not yield; racing deadlocked the anchor tests); anchor resume raises on clock skew instead of guessing.
- `harness/runtime.py:328-334` — live scheduling always passes real day_scores; scores=None is never used in live planning.
- `harness/runtime.py:351-363` — inbound user messages are also enqueued as user_message_mid_turn steers (decision-layer coupling, WS4); the lock guarantees no message arrives mid-generation.
- `harness/runtime.py:404-407` — the typing indicator stays up while the reply is composed and response_delay_s elapses; the send happens after the context exits.
- `harness/runtime.py:423-432` — the bubble send path is identical whether bubbles were split post-hoc or streamed off the wire (HARNESS_BUBBLE_STREAM).
- `harness/runtime.py:463-471` — clock discipline: the rollover parks at a pending event so an accelerated midnight jump cannot expire a still-valid event.
- `harness/runtime.py:841-844` — commands route to harness.commands.handle_command under the runtime lock, never session.on_message (read-only context, no memory writes).
- `harness/scheduler.py:9-26` — scores=None (adj = 1) is kept only for tests/legacy callers; live scheduling always passes a concrete per-day scores array.
- `harness/scheduler.py:198-203` — opportunities persist only through an optional duck-typed store seam; the live store had no contact_opportunities table.
- `harness/scheduler.py:509-550` — STRUCTURED_NO_STATE claim = 15% count divergence + 10% inter-contact gap divergence, sized for the real matrix while the pre-flight gate uses a lower bar.
- `harness/score.py:10` — the synthetic score noise sd (0.2) was frozen by the Phase-1 plan.
- `harness/store.py:86-88` — memory_turns view reuses messages (one append-only log, no dual-write).
- `harness/store.py:162-165` — test suite runs PRAGMA synchronous=OFF (measured ~4.8 ms vs ~0.01 ms per commit).
- `harness/summarization.py:5-13` — deterministic extractor is the TESTING path; the LLM-backed extractor is the research-quality production path.
- `harness/summarization.py:20-26` — provenance invariant: no summarization-generated fact becomes authoritative without source turns; L4 facts are re-extracted from raw messages.
- `harness/summarization_facts.py:4-8` — extraction is conservative and regex-based because its output can become an authoritative L4 assertion — it must never guess.
- `harness/tools.py:68-72` — per-day no-reply budget: 0 = must always reply, unset/empty = off (unlimited); at exhaustion the no-reply verdict is rejected, a reply is forced and budget_exhausted_forced_reply is recorded.
- `harness/tools.py:74-77` — decision_source=server_draw draws the verdict from an injected seeded RNG on a dedicated stream (never the day_rng order) purely for the model-vs-draw comparison; the server_draw/abort parse-failure fallbacks belong to the same comparison.
- `harness/trace.py:39-40` — CACHE_FLOOR = 0.6: a prompt with a proven byte-prefix predecessor should cache >=60% of its tokens.
- `harness/trace.py:170-175` — timeline sorts by virtual instant while decisions/steers print the later delivery instant alongside (replay contract).
- `harness/trace.py:741-742` — a null raw_cost is normal (spend rebuilt from tokens+pricing); a model missing from pricing uses the fallback tier, which over-states.
- `harness/tunables.py:1-12` — tunables live in code, not a file, so a replay is pinned to the code version.
- `harness/anchor.py:1-6,51-57` — anchor is a pure module freezing one epoch->virtual-hour point; t_h0 = absolute hours since local midnight, DST-safe via UTC instants. (only mentioned in results/)
- `harness/assembler.py:103-110` — default persona core: a persona states what someone IS; defining it by negation or by generic companion traits reads as hedging to the model. (only mentioned in results/)
- `harness/audit.py:1-6` — prompt persistence is verified, not rebuilt (llm_calls.repro_json); hash-only rows raise or render a note, never faked coverage. (only mentioned in results/)
- `harness/bubbles.py:128-142` — separator ruling: any run of newlines is ONE boundary, so a single newline and a blank line are the same send (WS-B). (only mentioned in results/)
- `harness/channels/telegram.py:10-16` — opt-in HARNESS_DEBOUNCE merge policy: buffer after the owner filter, one merged message, trailing 4.5 s / hard cap 12 s, a /command flushes first; default off. (only mentioned in results/)
- `harness/channels/telegram.py:307-309` — check_token exists as a gate-style liveness check for a borrowed/stolen token: getMe proves the token works without sending a message. (only mentioned in results/)
- `harness/client.py:61,198-215` — reasoning read from reasoning_content then reasoning, no fallback; replayed `content: null` normalized to ""; reasoning_tokens kept out of completion_tokens (priced differently). (only mentioned in results/)
- `harness/credentials.py:1-31` — two-lane token contract: lane token required, no LLM_API_KEY fallback, values never logged. (only mentioned in results/)
- `harness/domain.py:187` — ProactiveIntent has no optional source fields: there can be no proactive reason without a grounded source. (only mentioned in results/)
- `harness/domain_memory.py:1-2` — memory-layer types split into their own module because only the memory pipeline reads them; harness.domain re-exports all names. (only mentioned in results/)
- `harness/embeddings.py:15-19` — eval comparison rule: VERBATIM_RAG and STRUCTURED_MEMORY must share one embedder instance; a policy change never swaps it. (only mentioned in results/)
- `harness/env.py:3-9` — env helpers stay a leaf module so no import cycle forms. (only mentioned in results/)
- `harness/judge.py:14-32` — the feedback judge is NEVER used as the ablation evaluator; every runner must build the judge's client on the research lane so judge spend is never attributed to the product lane. (only mentioned in results/)
- `harness/life.py:1-5` — LIFE draws come from reserved rng stream key 4 (init) / stream 4 + day; never day_rng, which would desync the Session's shared day generator. (only mentioned in results/)
- `harness/memory.py:403-412` — no provenance -> no truth: nothing is promoted and no assertion created without source turn ids. (only mentioned in results/)
- `harness/metering.py:33-36` — per-thread sibling stores for ledger writes from worker threads. (only mentioned in results/)
- `harness/negotiation_contract.py:61-63` — SHORT_AFK_H (decide bomb) is deliberately distinct from USER_LEFT_THRESHOLD_H (user-away); both measured from _last_user_turn_t_h. (only mentioned in results/)
- `harness/negotiation_coordinator.py:1-26` — the mixin is a file split, not a decoupling; the Session entanglement is a deliberate parked trade (2026-08-28 code-quality review) and parity tests are the gate. (only mentioned in results/)
- `harness/negotiation_state.py:1-39` — replay contract: full JSON snapshot state event per mutation, rebuilt at init; the decide id derives from the current delay index so replay-by-decision_id returns the recorded verdict. (only mentioned in results/)
- `harness/persona.py:37-40` — persona draws use the reserved engine.rng stream key 5; there is no global RNG state. (only mentioned in results/)
- `harness/pricing.py:1-8` — rates were PENDING-USER placeholders; PRICING_PENDING gates the banner. (only mentioned in results/)
- `harness/proposal_cache.py:43-48` — test suite disables the cache suite-wide via empty env var. (only mentioned in results/)
- `harness/runtime.py:272-278` — anchor resume raises on clock skew (persisted state already past the anchor's mapping) instead of guessing. (only mentioned in results/)

### harness/session.py (second pass)

Second pass over `harness/session.py` (not applied — the drafting script's
per-edit assertions failed before any write; the docstring trim there remains
open). Same rule: decision-bearing content that is NOT reflected in any doc.

- `harness/session.py:339-345` — a decided verdict's `reason` is never pasted into the channel as dialogue; it lives only in `decision_records`, and `decided_notes` tells the turn what she decided so she generates her own words.
- `harness/session.py:353-378` — tool-call markup (DSML fullwidth bars, ASCII forms, harness textual-fallback marker) is stripped from replies (a 2026-09-08 reply was persisted and sent verbatim); closing tags need the optional slash or bodies read as prose.
- `harness/session.py:410-432` — markup that OPENS a reply means the whole reply is a tool call (its text is model rationale, not her words) -> return ''; markup after prose is stripped and prose kept; a surviving machinery token means give up rather than salvage.
- `harness/session.py:856-857` — a failed judge call must not kill the day: the judge is a noisy sensor and degrades to a logged neutral score.
- `harness/session.py:911-945` — life arcs are seeded once per life epoch; a life wipe (including NO_LIFE day-boundary wipes) bounds an epoch so the next seeding gets a FRESH arc-id namespace and never reuses wiped ids.
- `harness/session.py:1306-1337` — the closing draw keys on (conversation sequence, companion turn index), never turn index alone; the first companion turn is exempt so at least one full exchange always happens; two-phase close keeps draw keys/consumption unchanged.
- `harness/session.py:1541-1555` — state-card reuse: only the clock reading is normalized, while weekday, day period, day index, agenda partition and window times stay material because those are what she reasons about.
- `harness/session.py:1620-1633` — day-scoped state (agenda/arcs/user model) moved out of the per-turn card into the stream because it cannot live in the system prefix (the agenda mutates); idempotent via a persisted marker.
- `harness/session.py:1691-1709` — past decisions are replayed as native assistant tool_calls + role='tool' pairs; the earlier prose-summary form taught the model nothing.
- `harness/session.py:2581-2586` — the heads-up steer is queued only while the window is still genuinely ahead; a lead instant for an already-started event is never queued so a replayed morning gets decisions, not stale warnings.
- `harness/session.py:2689-2692` — pop-up decisions are stamped with the boundary instant, not the drain instant, so the pop-up's `Time:` and the 'recorded at' tool result agree on replay; the drain instant survives as delivered_t_h.
- `harness/session.py:2952-2959` — the decide leg's stored request identity records `tools` (hash + names) and decode controls by value, so a stored call can be proven identical to what went on the wire.
- `harness/session.py:3047-3052` — pop-up context must go through the single stream builder (None content -> '', user turns stamped); a raw rebuild over stored rows diverged at the first user message and banked only the system block.
- `harness/session.py:3124-3137` — pop-up decision calls are metered into llm_calls like mainline turns (same model, same lane); ledger `role` is the pop-up kind so decision and chat lanes can be told apart.
- `harness/session.py:3168-3188` — a server-side event close marks the agenda item skipped so NOW-semantics stops showing it; only outcomes she actually decided/said are recorded — nothing is invented.

## observability/ + experiments/

- `experiments/cvs_common.py:76-80` — the recall embedder replaced the 64-dim test embedder because it was too collision-prone for the frozen M3/M4 bars.
- `experiments/cvs_common.py:348,376` — the canonical Track A lanes use MemoryAgent(RAW_CONTEXT) and VERBATIM_RAG as the faithful implementations.
- `experiments/cvs_common.py:459-470` — an empty day-finalize yields judgement 0.0 "no interaction", loses L2/L3/L4 rows and produces a spurious neutral score (confounder E0).
- `experiments/cvs_common.py:1780-1824` — FULL's event-chain aggregate of 0.333 is 'the honest headline'; the fair RAW_HISTORY probe exists to make that comparison.
- `experiments/cvs_judge.py:1-27` — judge protocol v2 (forced pairwise, Bradley-Terry) replaces the v1 absolute 1-9 rating; constants are verbatim in the B9 report, consumed by B10/G4.
- `experiments/cvs_judge.py:690-697` — v1 legacy severity offsets rationale plus the measured '+1.84 pts' example.
- `experiments/cvs_g6.py:40-51` — opencode-go 2026-08-10 episode (flash returned 100% empty content) and the 2026-08-13 probe pitfall (max_tokens cap starves a reasoning model's content).
- `experiments/cvs_preflight.py:1-9` — F4 rationale: 5 of 7 ablations read null after 4h12m of API spend, hence the cheap fake-client gate.
- `experiments/cvs_preflight.py:26-32` — G2 placeholder-substitution history (B4 flat set replaced by per-condition claims; 15%/10% thresholds dropped).
- `experiments/cvs_preflight.py:166-176` — B4 provenance (merge f48683d, 'reporte B4 §FROZEN TARGET RANGES'); the committed target ranges stay as numbers.
- `experiments/cvs_preflight.py:204-212` — it2 SIMPLE_RAG failure mode: store populated but retrieval identical to FULL, AnyEvidence 0.0.
- `experiments/live_companion.py:98-101` — resume-gap policy rationale (a wall-clock anchor can manufacture skipped virtual days); the mechanism stays in check_resume_gap.
- `experiments/live_companion.py:234-243` — why a live trial uses the real judge instead of the matrix DeterministicJudge, and that judge spend is attributed to the research lane.
- `experiments/live_companion.py:272-273` — contrast: experiment cells keep fail-fast; live runs must not.
- `experiments/q1_actuator.py:1-9` — STATUS note: the original scratchpad q1_actuator was LOST; this file is a re-derivation from the G0 record and its numbers are hypotheses.
- `experiments/tier1_wsa_remeasure.py:11-14` — commit 772b0f0 provenance and the '≈ +10% prediction' paragraph.
- `experiments/decision_probe.py:1-10,347-351` — steering directive L361 / session item #22 provenance and WS2/WS3/WS-C wave tags.
- `experiments/negotiation_scenarios.py:1-45` — A4/G0/A1/A2 iteration tags and 'so the tests hold against the merged A1/A2 implementation'.
- `experiments/e2e_ablation.py:1-12` — 'W-E3' tag and 'advisor review 2026-08-08' provenance.
- `experiments/companion_vertical_slice.py:1-27` — 'prototipo E0 / Iteración 2 / Gate 4-6' provenance tags.
- `experiments/memory_backends_benchmark.py:3-17` — rationale for the benchmark design ('deliberately small so the semantic embedder cannot answer by accident'; run backends one at a time).
- `experiments/it3_manifest.py:43` — 'Margin decision 2026-08-10' label; the 14.4%/12.5%/0.15 values stay.
- `experiments/validation/hard_invariants.py:1-20,60-80` — blind-audit F1 history (auditor stayed silent on 40% blanks and returned validated:true), it2 corpus stats (18-40% blanks), plan §11 DoD / B7/B8/B10 tags.
- `experiments/w34_temporizacion.py:72-79` — 'that was already validated in the W1.4 tests'.
- `observability/server.py:1-8` — the guard design: a request's run id is matched against the discovered run list so no URL path ever reaches the filesystem.
- `observability/db.py:140-148` — a deployed run is defined by live_*.sh launchers because the run database records no channel.
- `observability/events.py:1-18,53-59` — tie-breaking rationale for the composite stream cursor (seq = round(t_h*1e6)*10 + rank, so ?after= never swallows a sibling).
- `observability/context.py:1-16` — the DSH comparison ('replay fold' vs pricing the persisted envelope); the decision itself is covered in docs/design-note-observability-2026-09-12.md.

## tests/

- `tests/test_credentials.py:89-93` — a missing lane token must fail loudly; there is deliberately NO silent fallback to LLM_API_KEY / OPENCODE_GO_API_KEY.
- `tests/test_client_retries.py:1-7,79-91` — one retry budget covers a retryable status, a transport error and a malformed 200; a non-retryable HTTP status also consumed the whole budget (pinned as current behaviour, arguably wrong).
- `tests/test_client_retries.py:150-154` — a reasoning-only reply is a legitimate empty-content turn, not an error.
- `tests/test_adversarial_restart.py:239-243` — a still-valid overdue event recovered during quiet hours must be deferred to the next awake instant or expired, never silently consumed as fired.
- `tests/test_adversarial_restart.py:399-411` — NOW semantics: an activity is current only while an item is genuinely in progress; the old highest-salience fallback is gone.
- `tests/test_adversarial_restart.py:456-460` — close_session / promote must be idempotent across a restart: no duplicated summaries or episodes.
- `tests/test_adversarial_restart.py:500-502` — live planning must never run with scores=None; the neutral A=1 fallback is used instead.
- `tests/test_adversarial_restart.py:683-688` — deferral across restarts: a 12h check_in validity outlives the quiet window (R-10).
- `tests/test_adversarial_restart.py:750-753` — every proactive row must carry a real intent_id resolving to an existing source; reactive rows carry None.
- `tests/test_adversarial_grounding.py:60` — a superseded source must suppress with source_superseded, never hallucinate and never fall back to a generic 'schedule' reason.
- `tests/test_adversarial_grounding.py:235` — intent lifecycle is idempotent: an upsert must not resurrect a fired row, and expired intents are never reused.
- `tests/test_adversarial_memory.py:210` — no sourceless episode: promote() refuses unprovenanced summaries (write-seam rule).
- `tests/test_adversarial_prompt.py:80` — retrieved memory is quoted data: MEMORY_EVIDENCE_HEADER must precede every byte of user-authored text; no imperative wrapper is derived from it.
- `tests/test_conversations.py:261,361-365` — the legacy MAX_TURNS hard cap is off; conversation closing is boundary-driven (threshold read from harness.tunables, never hard-coded).
- `tests/test_bubbles.py:20-25` — bubble contract: at least 2 parts are required; [] and [one] both mean one message; no path may lose a message.
- `tests/test_cvs_common.py:50-54` — in real runs the clock freezes during LLM calls, so identical (role, content, t_h, day) tuples occur; the recall key must include intent_id.
- `tests/test_cvs_common.py:384-387` — F5: the metric must read the condition's lane (SimpleRagMemory) instead of always building a MemoryAgent.
- `tests/test_cvs_common.py:414-417` — RAW_HISTORY fair probe scores recoverability from the raw window as a real verdict, not a constant 0.
- `tests/test_adversarial_runtime.py:179-189` — open finding R1-F1: the deferral sleep does not advance the virtual clock, the run never terminates; the wait is bounded so the suite fails fast.
- `tests/test_live_trial_hardening.py:130` — an overdue pending row must be dropped instead of spinning the firing loop against a failing provider.
- `tests/test_live_trial_hardening.py:204` — Session.ensure_day rolls missing days forward through the judge, so a stale-anchor resume would fabricate days.
- `tests/test_live_trial_hardening.py:265` — build_persona hard-codes 'Nova' in the core, so a companion rename must rewrite the persona core, not just the DB column.
- `tests/test_event_steer_2026_09_08.py:57` — the _as_bool('defer') bug applied a deferral as an abandon; the fix is pinned by the tri-state test.
- `tests/test_assembler.py:241-266` — 2026-09-07 swap: the persona leads the prompt and the card rules follow it, with the cache contract unchanged.
- `tests/test_cache_prefix_gate.py:200-217` — pop-ups ride the turn's own generation so one queue mixes lanes, and bare markers were refused because DSML reached the live channel (BACKLOG/CONVENTIONS cover the verdict/reason rules, not the DSML + first-marker detail).
- `tests/test_persona_file.py:1` — voice lives in a file named by HARNESS_PERSONA_FILE, applied on EVERY start (not only a cold one), composed with the drawn interest sentence, falls back to Nova when unset/broken.
- `tests/test_routine_setup.py:1` — routine catalog is built at setup, cached, and never trusted raw: the catalog is an INPUT to build_persona, nothing the model says is trusted as number/name/count, and the same interest SET must cost no second model call.
- `tests/test_renderer_anticollapse.py:1` — brief renderer quantizes valence into 3 bands (+-0.35) and energy into 3 bands (0.35/0.7) = only ~9 distinguishable states; widening to ~6 bands per axis is the intended fix.
- `tests/test_memory.py:1` — memory reranker weights 0.35/0.30/0.35 with the topicality boost confined to a separately named experiment; store contract = category stored on the row, bucket keys never parsed, get_assertion returns the newest row regardless of status.
- `tests/test_runtime_anchor.py:1` — anchor-mode additions are ALL default-off; resume resumes at anchor.t_h_at(now) and clock skew RAISES instead of guessing; ControlCommand dispatch must use a function-level import; /tz applies at the next rollover; /mute defers (never consumes) pending events.
- `tests/test_runtime_clock_advance.py:1` — anchored inbound messages advance the virtual clock to the REAL arrival instant and messages.sent_at is stamped from real arrival via the anchor's inverse; unanchored path is frozen-clock with sent_at NULL.
- `tests/test_process_exit.py:1` — invariant 17 ('runtime tests must terminate their Python process') defines the whole file: subprocess harness with a hard timeout, child thread self-check, plus two negative controls. Its cited plan (§5-A6) is not in the repo docs.
- `tests/test_observability.py:181` — event-stream cursor encoding seq = round(t_h*1e6)*1e6 + rank*1e5 + id, chosen so the cursor time and the time the row DISPLAYS are the same instant; also the aux-lane envelope hoist of kwargs.system for the prefix panel.
- `tests/test_negotiation_wake_2026_09_09.py:1` — rejected alternative, stated as a contract decision: the window-close backstop still wins when the runtime arrives after end_t_h, and running the missed negotiation leg retroactively was refused because it would fire a model call for every skipped window.
- `tests/test_metering.py:1` — MeteredClient wraps the four aux callers (day planner, interest extension, routine setup, judge) so their spend lands in llm_calls, with a non-interference contract: same result, same capability gates, same exceptions, no row for an incomplete call.
- `tests/test_mood.py:24` — statistical acceptance policy for the nu=inf binomial tests: chi-square alpha=0.01 with tails grouped below expected count 5, plus a DKW-band check instead of scipy.stats.kstest (discrete CDF false rejections). Its 'CONVENTIONS.md §5' pointer does not exist.
- `tests/test_proactive_it2.py:319` — invariant 7: two simultaneous proactive intents with the SAME reason must fire the exact validated id (message.intent_id) and never a same-reason sibling. Plan §5-A3 is not in the tree.
- `tests/test_pricing_and_config.py:1` — the pricing table is the only thing turning the token ledger into dollars (this gateway reports no cost field); channel selection is what keeps `import harness.config` free of python-telegram-bot.
- `tests/test_reset.py:1` — reset keeps the onboarding cache, clears the trial and never deletes: a live writer is refused and the old database is moved rather than removed.
- `tests/test_prompt_content_2026_09_07.py:1` — the pop-up aux call must be a byte-identical EXTENSION of the mainline call (a differing system message costs the whole prefix); recorded decisions are projected back into context; the stable prefix leads with persona and carries neither closing guidance nor steer trust prose.
- `tests/test_session_close_parity.py:1` — flag-off close path is frozen by a sha256 pin over the canonical persisted trace; while the closing draw is flagged off the wind-down machinery is unreachable, so the flag-on arm must be byte-identical to the flag-off arm.
- `tests/test_session_close_parity.py:125` — regenerating the parity pin is an orchestrator decision, never a silent test edit (survives inside an assert message; left in place).
- `tests/test_system_adjacency_2026_09_08.py:1` — deliberately NOT done, pinned so they are not 'fixed' later: no synthetic assistant turn interleaved between system blocks, and replayed decisions keep their real assistant tool_calls -> role='tool' exchange.
- `tests/test_serializer_null_hardening.py:3` — serializer rule: content on the wire is always a string — '' for reasoning-only turns, never null — on both request and response sides.
- `tests/test_store_it3.py:1` — eval-mode rows persist the full system prompt + message payload; default rows stay hash-only with no payload (privacy default) and the leak scan must report them as NOT verifiable.
- `tests/test_setup_onboarding.py:68,140,332,373` — a DB reset with unchanged interests replays the accepted proposal from disk; the model proposes NAMES, never buckets (40/40/20 recomputed); /setup runs inside the runtime lock and the WAIT is bounded by a timeout budget; the extension call sends low reasoning_effort and JSON mode through the meta-capable chat surface.
- `tests/test_session.py:579,751` — resume on an already-progressed store with a driver clock starting at day 0 initializes at the store's day; NOW semantics: an agenda item is 'current' only while genuinely in progress (out-of-window -> None).
- `tests/test_spend.py:101` — a zero cache_creation_input_tokens must never claim a full cache hit; the miss bucket is the prompt remainder whenever the prompt total is known.
- `tests/test_scheduler.py:388` — B5 state->timing coupling goes through the patchable session seam (day_scores + state_factors_for_plan); STRUCTURED_NO_STATE's neutral patch collapses both the state vector and the A·I fold to neutral.
- `tests/test_telegram_commands.py:135` — /state is deliberately NOT user-visible: absent from the registered command menu and from the USER_COMMANDS contract.
- `tests/test_trace_inspector.py:4` — inspector tests build tables with raw SQL rather than SQLiteStore, because the inspector must read runs whose schema does not match current code.
- `tests/test_store_migrations_v6.py:8` (and `_v7.py:443`) — live-DB migration verification runs against a COPY of the real populated companion.db; the original is opened read-only and byte-compared before/after and never touched.
- `tests/test_steering.py:254` — the seen marker is persisted with the turn, which is what makes delivery one-shot and detectable on replay.
- `tests/test_session_close.py:1` — two-phase close: the closing draw persists closing_pending_t_h instead of closing, the wind-down expires at closing_pending_t_h + WIND_DOWN_GRACE_H, and closes use reason closing_tendency; flag OFF by default, on via kwarg or HARNESS_TWO_PHASE_CLOSE.
- `tests/test_w2w3_time_aware.py:1` — masking rule: unanchored runs omit the temporal section entirely (t_h never rendered raw) and only clock-shaped times (HH:MM) plus the temporal line's day index may be numeric in the prompt.
- `tests/test_telegram_debounce.py:6` — debounce windows are env-configurable (HARNESS_DEBOUNCE_TRAILING_S / HARNESS_DEBOUNCE_MAX_WAIT_S) with 4.5 s trailing / 12.0 s cap defaults chosen as the human follow-up window; tests import the defaults.
