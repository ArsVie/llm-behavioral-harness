# Companion Vertical Slice — 2026-08-08

Status: EXECUTING (orchestrator: Hermes). Owner of record: Ars.
This file is the AUTHORITATIVE CONTRACT for all wave agents. Read fully before coding. The
orchestrator's operating rules, seams, gate checklists, and merge order below are binding;
the plan prose defines intent, the Seams section defines exact API shapes.

## 1. Iteration objective

Build one convincing end-to-end vertical slice in which the companion:

> has a persistent identity and interests -> maintains an off-screen life -> generates a
> daily agenda -> has a current activity -> remembers meaningful events -> develops grounded
> reasons to contact the user -> has stochastic state affecting how/when she communicates ->
> actually applies behavioral actuators -> changes proactive frequency based on state/history
> -> survives restart without losing continuity.

Success criterion: accelerate **30 simulated days**, stop/restart the system several times,
and still trace every spontaneous message back to an actual state, memory, agenda item, or
relationship event.

## 2. Explicitly deferred — FROZEN (never implement, even if "obvious")

1. conversation/history importer
2. style reconstruction from imported chats
3. individualized cycle phenotype
4. response-time monitoring / "she messaged and he hasn't answered"
5. UI polish
6. external vector DB / embeddings infra **unless** the local brute-force embedding seam
   (Amendment §9) proves inadequate — the POC uses local BLOB embeddings, NOT a vector DB.

## 3. Amendment 1 — ZifaMem memory architecture (2026-08-08, Ars-approved)

The advisor revised the memory design after reading the ZifaMem paper
(arXiv:2607.17564v1) and its public implementation. The following REPLACES the generic
flat-memory parts of this plan. Summary of deltas:

- A5 implements an L1/L2/L3/L4 memory pipeline, not a flat `MemoryRecord[]` bag:
  - L1 recent turns (exact text, session id, timestamp, speaker, metadata; NOT summarized immediately)
  - L2 session summaries (structured: summary, topics, user_facts, preference_updates,
    companion_events, relationship_events, callbacks, affect_observations, emotional_peak,
    importance, source_turn_ids)
  - L2->L3 explicit promotion: `promote(s) = 1[I_s >= 0.5 OR had_emotional_peak(s)]` behind
    `PromotionPolicy(importance_threshold=0.5, promote_emotional_peaks=True)` — configurable
    parameters, start from the evaluated design.
  - L3 episodic memories: summary, category, occurred_at, created_at, importance,
    access_count, last_accessed, affect metadata (valence, arousal, intensity, conflict,
    comfort, vulnerability, relationship relevance), source_session_id, source_turn_ids,
    verbatim_anchors[], tags[]. **Never make a summary the sole surviving representation of
    an important event** — keep pointers to exact turns.
  - L4 consolidated user model: identity, stable_preferences, current_preferences,
    boundaries, vulnerabilities, recurring interests, relationship-relevant patterns,
    important people/entities, and `assertions[]` each with {value, confidence, updated_at,
    source_memory_ids[], status}. **New evidence UPDATES the model** (supersede, keep
    provenance) rather than piling contradictory facts for the LLM to reconcile.
- Affect is **metadata on memories**, not a separate emotional memory subsystem. There is
  NO second "emotional memory DB".
- **User affect != companion state** (A1 contracts): `UserAffectObservation` and
  `CompanionBehaviorState` are distinct types with NO implicit conversion. The system may
  suppress companion playfulness when composing, but never concludes user=playful because
  the companion is playful.
- Retrieval reranker (evaluated formula, start here):
  `score(q,j) = 0.35*sem(q,j) + 0.30*strength_j + 0.35*importance_j`, with episodic strength
  recalculated at retrieval time from age, access count, importance and stored affect
  metadata. **Do NOT add a standalone emotional-intensity weight** (double counting).
- Semantic relevance without a vector DB: episode -> embedding -> SQLite BLOB / local array
  -> brute-force cosine. Embedder injectable (deterministic fake for tests; local bge-m3
  service at :11435 for live runs if reachable).
- Lane architecture: MEMORY lane (L1->L2->L3->L4), LIFE, PERSONA, STOCHASTIC STATE, CURRENT
  ACTIVITY, PROACTIVE INTENT — each subsystem owns its truth; they meet ONLY at
  `CompanionSnapshot` (context/execution composition). No module mutates another lane.
- A10 memory ablations become RAW_HISTORY / SIMPLE_RAG / STRUCTURED_MEMORY (identical
  downstream generation) + inside structured: STRUCTURED_MEMORY+STOCHASTIC_STATE vs
  STRUCTURED_MEMORY-STOCHASTIC_STATE (two separate research questions; see §14).
- A dedicated Memory Gate (§13) before A5 integration.

## 4. Agent topology

| Agent | Role | Primary ownership |
|---|---|---|
| A1 | Domain Contracts + Integration | `harness/domain.py`, later `session.py`, `assembler.py` |
| A2 | Persistence | `harness/store.py`, schema/migrations |
| A3 | Behavioral Actuation | `harness/actuation.py` (new), `behavior.py`, `client.py` |
| A4 | Life Simulation | `harness/life.py` |
| A5 | Memory (L1/L2/L3/L4) | `harness/memory.py` |
| A6 | Persona + Interest Graph | `harness/persona.py`, `harness/interests.py` |
| A7 | Proactivity + Timing | `harness/proactive.py` (new), `scheduler.py`, `runtime.py`, `gates.py` |
| A8 | Architecture Reviewer | read-only except review artifacts |
| A9 | Adversarial QA | new integration/adversarial tests only |
| A10 | Evaluation/Falsification | experiment scripts, experiment tests/results |

Orchestrator is the merge authority. Agents never merge other branches or fix files they do
not own.

## 5. Dependency graph

```text
                    +---------------+
                    | A1 contracts  |
                    +-------+-------+
                            | GATE 0
          +-----------------+-----------------+
          |                 |                 |
      +---+---+         +---+---+       +----+----+
      |A2 DB  |         |A4 Life|       |A6 Persona|
      +---+---+         +---+---+       +----+----+
          |                 |                 |
          |          +------+-----+           |
          |          |A5 Memory   |           |
          |          +------+-----+           |
          |                 |                 |
          +--------+--------+--------+--------+
                   |                 |
              +----+-----+      +----+-----+
              |A7 Intent  |      |A3 Actual |
              |+ timing   |      |actuators |
              +----+-----+      +----+-----+
                   |                 |
                   +--------+--------+
                            |
                   +--------+--------+
                   | A1 integration  |
                   | Snapshot->LLM   |
                   +--------+--------+
                            |
                          GATE 3
                            |
              +-------------+--------------+
              |                            |
         +----+----+                  +----+----+
         | A9 QA   |                  |A10 Eval |
         +----+----+                  +----+----+
              +------------+--------------+
                           |
                         GATE 4
```

A8 reviews every gate.

## 6. Wave 0 — freeze the new domain contracts

All 10 agents launch immediately (orchestrator batches them). A1 codes. A2-A7 inspect their
existing implementation and prepare against the prescribed contracts. A8 reviews proposed
boundaries. A9 identifies adversarial cases. A10 freezes evaluation criteria BEFORE seeing
results. Only A1 may modify shared contracts before Gate 0.

### A1 — Domain Contract Agent

Create `harness/domain.py`. Do NOT modify the frozen `engine/types.py`. The engine remains
responsible for stochastic state; `harness/domain.py` owns higher-order companion concepts.

Required immutable typed representations (final list, Amendment-inclusive):

```text
Interest, InterestRelation, Routine, PersonaProfile,
LifeArc, AgendaItem, DailyAgenda, CurrentActivity,
MemoryKind, AffectMetadata, SessionSummary, EpisodicMemory,
UserModel, UserModelAssertion, UserAffectObservation, CompanionBehaviorState,
MemoryContext,
ProactiveIntent, GenerationControls, BehaviorBrief, Turn, CompanionSnapshot
```

The critical invariant: **There can be no proactive reason without a source.** A schedule
event points to `agenda_item:pottery_2026_08_08`, not `reason="schedule"`.

`CompanionSnapshot` is the integration contract:

```text
CompanionSnapshot
|- persona
|- current_behavior
|- current_activity
|- today's relevant agenda
|- active life arcs
|- memory_context        (L1 recent + L2 context + L3 episodes + L4 projection + anchors)
|- recent_conversation
|- proactive_intent?
```

### Gate 0 (A8 approves; orchestrator merges)

- engine types untouched;
- domain objects don't know about SQLite;
- domain objects don't know about LLM APIs;
- `ProactiveIntent` requires evidence/source (no optional source fields);
- no menstrual/cycle label exposed through conversational-domain objects;
- no import machinery appears;
- no individualized hormonal phenotype appears;
- `UserAffectObservation` and `CompanionBehaviorState` are distinct types (no shared
  mutation, no implicit conversion documented);
- `MemoryContext` type exists with L1/L2/L3/L4 + anchors slots.

## 7. Wave 1 — six modules in parallel

Merge order (orchestrator, after per-branch A8 review): **A2 -> A6 -> A4 -> A5 -> A3 -> A7**.
Full suite after EVERY merge.

### A2 — Persistence agent

Own: `harness/store.py`, `tests/test_store.py`, `tests/test_store_migrations.py` (new).

- Add `schema_meta(version)` with additive migrations from the current schema.
- New persisted concepts: persona, interests, life_arcs, agenda_items, proactive_intents,
  and the memory tiers: memory_sessions, memory_turns (reuse existing messages where
  sensible), memory_session_summaries, memory_episodes, memory_episode_sources,
  user_model_assertions, memory_embeddings. No business logic in the store.
- CRUD/query ops (see Seams §store): persona save/load; interests save/list; life-arc
  upsert/list/status; agenda save/load/item-status; memory tiers ops; proactive intent
  save/load/list/status; `resolve_intent_source`; load previous judgement; latest
  interaction time.
- **Critical migration test**: create a DB with the current ZIP schema, insert existing
  state/messages/schedules, instantiate new store: migration succeeds, old messages/daily
  state/judgements/proactive events remain interpretable, migration runs twice. No
  destructive migration this iteration.

### A3 — Actual behavioral actuators

Own: `harness/actuation.py` (new), `harness/behavior.py`, `harness/client.py`,
`tests/test_actuation.py`, `tests/test_client.py`. Do NOT touch session.py/runtime.py.

- Convert `BehaviorDirective` into mechanical controls:
  - response length: `max_tokens = T0 * response_length_scale` bounded [min,max];
    `LLMClient.chat(..., max_tokens=...)`; FakeClient records it. Deterministic test:
    low directive -> smaller budget, high -> larger.
  - delivery latency: map `response_delay_s` into `GenerationControls`; NO sleep() in this
    module (A7's runtime applies it).
  - closing tendency: observable via generation budget + explicit continuation policy
    (prompt guidance; must not remain an unused number).
  - initiative: deterministic multiplier `r_I = exp(beta*(I-0.5))` bounded; mechanically
    enters scheduling (A7).
- Extend `BehaviorDirective` with `response_length_scale`, `response_delay_s`,
  `closing_tendency` (defaults; derive_behavior fills them from channels).

### A4 — Persistent life simulation

Own: `harness/life.py`, `tests/test_life.py`.

- Persistent arcs first: LifeArc (photography, started_day=4, progress=0.37, status=active,
  next_intention="practice portraits"; reading a novel, learning pottery, watching a series,
  training for a 10k, taking care of plants, planning a weekend activity, working on a
  drawing).
- Persona + interests + routines + LifeArcs -> DailyAgenda -> CurrentActivity.
- Agenda items: start/end, activity, source arc/interest/routine, salience, status.
  NOT followed perfectly: statuses planned/completed/skipped/shifted with modest stochastic
  variation. A person deviates from plans.
- Core test: simulate 30 days with fixed seed: active arcs survive day boundaries; some
  progress; some activities recur; not every day identical; activities come predominantly
  from the companion's actual interests; restart/reload reproduces the same persistent
  state. Output = a life trajectory, not 30 independent generated calendars.

### A5 — Structured memory (ZifaMem-style, Amendment §3)

Own: `harness/memory.py`, `tests/test_memory.py`.

- Memory pipeline L1/L2/L3/L4 per Amendment §3: turn buffer, session summaries,
  promotion policy, episodic memories with verbatim anchors + affect metadata, consolidated
  user model with assertions {value, confidence, updated_at, source_memory_ids, status}.
- Retrieval: `score(q,j) = 0.35*sem + 0.30*strength + 0.35*importance`, strength
  recalculated at retrieval (age, access count, importance, affect). NO extra emotional
  weight. Hard budget on returned context.
- Semantic relevance via injectable embedder + brute-force cosine over stored BLOBs
  (deterministic fake for tests).
- No provenance -> no truth: extractor never becomes source of truth without source.
- Synthetic tests: "User says on day 2: 'My dog's name is Bruno.'" After >12 turns and
  several days, transcript no longer contains it; a relevant day-20 conversation still
  retrieves dog=Bruno (proves crossing the 12-turn horizon). Also irrelevant-memory
  suppression; promotion gate behavior; L4 consolidation/revision; affect metadata; user vs
  companion separation.

### A6 — Persona + 40/40/20 interest model

Own: `harness/interests.py`, `harness/persona.py`, `tests/test_interests.py`,
`tests/test_persona.py`.

- Typed interest graph (nodes, edges with strengths) instead of asking the LLM what
  "adjacent" means. Example clusters: mathematics {physics, statistics, puzzles,
  programming}; metal {rock, live music, guitar, alternative music}.
- Portfolio around target 40% exact + 40% adjacent + 20% independent, SAMPLED around it
  (not forced per-companion); population mean converges near target across seeds.
- Structured `PersonaProfile` construction; LLM may decorate into prose later but does NOT
  decide bucket membership.
- Tests: mean bucket distribution approx 40/40/20 across seeds; no duplicate interests;
  adjacent interests have graph paths; independents aren't secretly exact; same seed
  reproduces profile; different seeds create variation.

### A7 — Grounded proactivity + adaptive scheduler

Own: `harness/proactive.py` (new), `harness/scheduler.py`, `harness/runtime.py`,
`harness/gates.py`, `tests/test_scheduler.py`, `tests/test_runtime.py`,
`tests/test_gates.py`, `tests/test_proactive.py`.

1. **Fix the restart bug**: `next_pending(t_h)` must not ignore overdue pending rows. On
   recovery: pending event <= now -> still valid? yes -> evaluate/fire, no -> expire.
   At `now == event_time` it must be visible. Regression tests: restart exactly at event,
   restart 10 min after, within validity window, beyond validity window, multiple overdue.
2. **Separate contact opportunity from contact reason**: Weibull process = "she feels like
   contacting around now", not a fabricated schedule reason. At opportunity time:
   stochastic opportunity -> IntentResolver -> candidates from current/recent agenda,
   companion life event, callback memory, shared-interest memory, legitimate check-in
   context -> ranked intent -> content gate. No grounded candidate -> `SUPPRESS:
   no_grounded_reason` (legitimate outcome).
3. **Content gate becomes real**: verifies source exists; not deleted/superseded; intent
   still timely; the supplied hook is actually attached to that source. Not
   `reason in VALID_REASONS`.
4. **Connect timing feedback**: stop live scheduling with `scores=None`. For each newly
   planned day use previous day's real judge score + today's behavioral initiative. Plan
   only the CURRENT day (today's DayRecord is available). Effective modulator:
   `h(tau,t) = h0(tau) * C(t) * P(t) * A(score_{d-1}) * I(t)` (circadian, phase, previous-day
   adjustment, initiative). No response-time monitoring.
5. **Apply latency**: when A1 returns a TurnResult with delivery controls: LLM finishes ->
   runtime waits requested `response_delay_s` -> channel.send(). Injectable/mock sleeper in
   tests; suite must not literally wait seconds.

### Wave 1 review gate

When A2-A7 finish they do NOT immediately get integrated. A8 reviews each independently; A9
begins adversarial testing against their branches. Orchestrator accepts a module only if:
(1) owned tests pass; (2) no foreign files modified; (3) contracts match A1's domain;
(4) no cycle labels leak; (5) no real-clock reads below runtime; (6) stochastic code uses
deterministic seeded RNG; (7) persistent state survives reopen; (8) no temporary dummy
sources used to satisfy groundedness.

## 8. Wave 2 — A1 central integration

A1 switches from contract owner to integration owner.

Own: `harness/session.py`, `harness/assembler.py`, `tests/test_session.py`,
`tests/test_assembler.py`, `tests/test_snapshot.py`.

System prompt assembled from `CompanionSnapshot`, not persona string + BehaviorDirective.

Reactive turn: user message -> persist -> retrieve current DayRecord -> BehaviorDirective ->
GenerationControls -> CurrentActivity -> relevant memories (MemoryContext) -> relevant life
arcs -> CompanionSnapshot -> Assembler -> LLM.

Proactive turn: contact opportunity -> grounded ProactiveIntent -> CompanionSnapshot
(intent=...) -> Assembler -> LLM. Proactive prompt contains the concrete hook
("You just finished the pottery class scheduled this afternoon. You had been nervous about
glazing the bowl.") — NOT "Contact reason: schedule".

Context budget — bounded sections: persona core; current behavioral brief; current
activity; 1-3 relevant life arcs; N relevant memories (hard budget); recent conversation;
proactive intent if applicable. Don't dump the database into the prompt.

Lane rule: memory, life, persona, stochastic state, activity, proactive intent meet ONLY at
the Snapshot. No cross-module mutation.

### Gate 2 — seam review (A8 reviews INTERFACES, not style)

- Persona -> Life: do activities actually come from persona interests?
- Life -> Proactivity: can every schedule/life proactive hook resolve to a persistent source?
- Memory -> Context: do old relevant memories beat new irrelevant ones?
- Behavior -> Execution: do actual generation parameters change?
- Judge -> Timing: does yesterday's score measurably affect today's hazard?
- Snapshot -> Assembler: can raw `phase_label/mu/eta/g/cycle_day` accidentally leak? They must not.

## 9. A9 — adversarial QA

Attacks the integrated branch. Cases:

- Restart attacks: exactly at proactive event; just after; during quiet hours; after two
  missed events; at midnight; after agenda generation but before activity completion; after
  memory write; immediately before/after judge finalization.
- Grounding attacks: delete/source-expire an agenda item before firing -> message
  suppressed, not hallucinated. Shared-interest reason with no actual shared-interest
  record -> `no_grounded_reason`.
- Memory attacks: contradictory facts ("I have a cat named Luna." / "I don't have Luna
  anymore.") — retrieval must not blindly surface stale truth; L4 revision handles it.
  Irrelevant high-salience memories; provenance checks.
- Life attacks: accelerate 60 days: immortal unfinished activities; everything completing;
  identical schedules; impossible overlaps; dead arcs never cleaned; spontaneous interests
  not belonging to persona; "goldfish resets".
- Actuator attacks: mechanical controls still work when FakeClient completely ignores
  prompt wording.

## 10. A10 — evaluation/falsification harness

Own new experiment code only: `experiments/companion_vertical_slice.py`,
`results/companion-vertical-slice/`. **Freeze criteria BEFORE seeing final results.**

### Structural suite

30 accelerated days x multiple seeds. Measure: grounded proactive rate; invalid-source
message rate; memory recall; memory false recall; life-arc continuity; agenda
recurrence/diversity; restart loss; behavior actuator differences; proactive rate vs
previous score; proactive rate vs initiative; state leakage.

Hard invariants: ungrounded proactive messages = 0; lost persistent state after restart =
0; cycle-label leakage = 0; stranded overdue schedule events = 0.

### Behavioral ablation

Full system plus informative ablations (Amendment §3 matrix):

```text
FULL                        structured memory + stochastic state + life + actuators + timing feedback
NO_ACTUATORS                actuators disabled (no generation-parameter changes)
NO_LIFE                     no persistent life arcs (agenda only, shallow)
NO_TIMING_FEEDBACK          scheduler ignores previous-day score
RAW_HISTORY                 memory lane replaced by raw transcript tail (identical generation)
SIMPLE_RAG                  memory lane replaced by lexical top-k retrieval (identical generation)
STRUCTURED_NO_STATE         structured memory, stochastic-state lane disabled (behavior channels neutral)
```

Judge blindly; do NOT tell the evaluator which condition produced the transcript. Metrics:
same-person continuity, sense of independent life, natural variation, grounded initiative,
memory usefulness, annoyance/intrusiveness, caricature, naturalness.

Research questions: (1) does STRUCTURED_MEMORY beat RAW_HISTORY / SIMPLE_RAG? (2) inside
structured memory, does our state machinery add perceptible value because it controls real
behavior rather than injecting emotion text?

The existing E2E evaluator results calculated but discarded from the report must now appear
in the report.

## 11. Gate 3 — architectural acceptance

Before live evaluation, orchestrator runs `MPLBACKEND=Agg .venv/bin/python -m pytest`.
Then A8+A9 independently review. Release only if: old 341-test baseline green plus new
tests; legacy SQLite database migrates; restart matrix passes; every proactive message has
a valid intent_id; every intent resolves to a real source; no source -> no proactive
generation; length actuator reaches actual generation budget; delay actuator reaches
runtime; initiative reaches scheduler; judge score reaches scheduler; 30-day life preserves
arcs; >12-turn memory retrieval demonstrably works; persona interest generation
statistically approaches 40/40/20; raw cycle state never reaches conversational context.

## 12. Gate 4 — falsification gate

A10 runs the experiment; A8 reviews the interpretation afterward. Rule: **a test passing
its own configured parameter expectation is not evidence of human-likeness.** The report
distinguishes Engineering Validation (behaves per specification) from Product Evidence
(blind evaluations indicate people/models perceive more continuity, independent life,
natural variation, etc.). If FULL doesn't outperform NO_LIFE or NO_MEMORY-policy, don't
tune the judge — investigate the architecture. If actuator differences are measurable
numerically but the blind evaluator cannot detect them, the actuator mapping is too weak.

## 13. Memory Gate (before integrating A5 — Amendment §3)

1. L1->L2 works: completed sessions deterministically produce summaries.
2. L2->L3 promotion works: high-importance or emotional-peak sessions promote; mundane
   ones often don't.
3. L3 retains evidence: every generated episodic memory links back to exact turns.
4. L4 is consolidated: repeated compatible info strengthens a current model, no endless
   duplicate facts.
5. Revision works: new evidence supersedes old assertions without deleting provenance.
6. Affect is metadata on memory; no second disconnected "emotional memory DB".
7. User affect != companion state: architectural separation enforced.
8. Retrieval uses relevance + strength + importance (0.35/0.30/0.35).
9. Temporal anchors survive summarization.
10. Three policy baselines exist: raw history, simple retrieval, structured memory.
11. Memory context has a hard token budget.
12. No summarization hallucination becomes L4 truth without source evidence.

## 14. Orchestrator operating rules (binding)

1. Use at most 10 subagents: A1-A10 as defined.
2. Launch all immediately; before Gate 0 only A1 modifies shared domain contracts.
3. Every implementation agent works in an independent branch/worktree.
4. Agents never merge their own work into main; return commit SHA(s), files changed, tests
   executed + results, contract assumptions, unresolved issues, foreign-file changes needed.
5. File ownership is exclusive. Never "quick-fix" another agent's module; report to the
   orchestrator.
6. engine/types.py, engine/rng.py, tests/conftest.py, pyproject.toml, CONVENTIONS.md stay
   frozen unless orchestrator explicitly overrides after an architecture review.
7. Preserve existing behavior by default. Prefer additive schema and APIs.
8. Run module-local tests before returning a patch.
9. Orchestrator runs the complete pytest suite after every merged implementation branch.
10. A8 reviews contracts and each integration gate. A9 independently attacks restart,
    grounding, persistence, cross-module invariants.
11. A10 freezes evaluation metrics before final integrated results; thresholds never change
    after seeing results.
12. Do not implement: cycle phenotype personalization, historical conversation importer,
    imported style reconstruction, response-time monitoring, vector DB (unless approved).
13. Never let an LLM invent the factual basis for a proactive message. No grounded source
    -> no proactive message.
14. Never expose phase labels, hormone variables, mu, eta, g, or raw internal stochastic
    state to the conversational model.
15. Final deliverable: reproducible 30-day vertical-slice report showing persistent life,
    memory, grounded initiative, actual behavioral actuation, adaptive timing, restart
    continuity + ablations showing which pieces matter.

## 15. Seams — orchestrator-frozen API shapes

Exact shapes below are binding; the owning agent may add private helpers but must not
rename/move these public names.

### domain.py (A1) — all frozen dataclasses, stdlib only

```python
@dataclass(frozen=True)
class Interest: name: str; bucket: str            # "exact"|"adjacent"|"independent"
                salience: float                   # 0..1
@dataclass(frozen=True)
class InterestRelation: from_interest: str; to_interest: str; strength: float
@dataclass(frozen=True)
class Routine: name: str; start_frac: float       # 0..1 of day
               duration_h: float; cadence: float  # daily probability 0..1; salience: float
@dataclass(frozen=True)
class PersonaProfile: name: str; core: str        # <=2 sentences prose
                      interests: tuple[Interest, ...]; routines: tuple[Routine, ...]
@dataclass(frozen=True)
class LifeArc: id: str; name: str; interest: str  # Interest.name
               started_day: int; progress: float  # 0..1
               status: str                        # "active"|"completed"|"abandoned"
               next_intention: str
@dataclass(frozen=True)
class AgendaItem: id: str; start_t_h: float; end_t_h: float; activity: str
                  source_type: str                # "arc"|"interest"|"routine"
                  source_id: str; salience: float
                  status: str                     # "planned"|"completed"|"skipped"|"shifted"
@dataclass(frozen=True)
class DailyAgenda: day: int; items: tuple[AgendaItem, ...]
@dataclass(frozen=True)
class CurrentActivity: t_h: float; item: AgendaItem | None; description: str

class MemoryKind(Enum):
    USER_FACT, USER_PREFERENCE, SHARED_EPISODE, COMPANION_EPISODE,
    RELATIONSHIP_EVENT, CALLBACK

@dataclass(frozen=True)
class AffectMetadata: user_valence: float; user_arousal: float; companion_valence: float
                      intensity: float; conflict: float; comfort: float
                      vulnerability: float; relationship_relevance: float
                      emotional_peak: bool
@dataclass(frozen=True)
class SessionSummary: session_id: str; started_at_t_h: float; ended_at_t_h: float
                      summary: str; topics: tuple[str, ...]; user_facts: tuple[str, ...]
                      preference_updates: tuple[str, ...]; companion_events: tuple[str, ...]
                      relationship_events: tuple[str, ...]; callbacks: tuple[str, ...]
                      affect_observations: tuple[AffectMetadata, ...]
                      emotional_peak: bool; importance: float; source_turn_ids: tuple[int, ...]
@dataclass(frozen=True)
class EpisodicMemory: id: str; summary: str; category: MemoryKind; occurred_at_t_h: float
                      created_at_t_h: float; importance: float; access_count: int
                      last_accessed_t_h: float | None; affect: AffectMetadata | None
                      source_session_id: str; source_turn_ids: tuple[int, ...]
                      verbatim_anchors: tuple[str, ...]; tags: tuple[str, ...]
@dataclass(frozen=True)
class UserModelAssertion: key: str; value: str; confidence: float; updated_at_t_h: float
                          source_memory_ids: tuple[str, ...]; status: str  # "current"|"superseded"
@dataclass(frozen=True)
class UserModel: identity: str; stable_preferences: tuple[UserModelAssertion, ...]
                 current_preferences: tuple[UserModelAssertion, ...]
                 boundaries: tuple[UserModelAssertion, ...]
                 vulnerabilities: tuple[UserModelAssertion, ...]
                 recurring_interests: tuple[UserModelAssertion, ...]
                 relationship_patterns: tuple[UserModelAssertion, ...]
                 important_entities: tuple[UserModelAssertion, ...]
@dataclass(frozen=True)
class UserAffectObservation: t_h: float; valence: float; arousal: float; label: str
@dataclass(frozen=True)
class CompanionBehaviorState: directive_ref: str   # opaque id of the BehaviorDirective used
                              initiative: float; energy: float; warmth: float; playfulness: float
# NOTE: no field may be shared/coerced between UserAffectObservation and
# CompanionBehaviorState; no implicit conversion, ever.
@dataclass(frozen=True)
class MemoryContext: recent_turns: tuple[Turn, ...]      # L1 slice
                     session_context: tuple[SessionSummary, ...]  # L2 (bounded)
                     episodes: tuple[EpisodicMemory, ...]          # L3 (bounded)
                     user_model: UserModel | None                 # L4 projection
                     evidence_anchors: tuple[str, ...]            # exact verbatim excerpts
@dataclass(frozen=True)
class ProactiveIntent: id: str; reason: str; source_type: str; source_id: str; hook: str
                       created_t_h: float; valid_until_t_h: float; salience: float
                       evidence: str     # provenance chain, REQUIRED
@dataclass(frozen=True)
class GenerationControls: max_tokens: int; response_delay_s: float
                          closing_tendency: float; initiative_factor: float
@dataclass(frozen=True)
class BehaviorBrief: valence: float; energy: float; reactivity: float; warmth: float
                     expressiveness: float; playfulness: float; reflectiveness: float
                     initiative: float; response_length_scale: float
                     response_delay_s: float; closing_tendency: float
@dataclass(frozen=True)
class Turn: role: str; text: str; t_h: float
@dataclass(frozen=True)
class CompanionSnapshot: persona: PersonaProfile; current_behavior: BehaviorBrief | None
                         current_activity: CurrentActivity | None
                         agenda: tuple[AgendaItem, ...]; life_arcs: tuple[LifeArc, ...]
                         memory_context: MemoryContext
                         recent_conversation: tuple[Turn, ...]
                         proactive_intent: ProactiveIntent | None
```

### store seam (A2)

```python
save_persona(profile) / load_persona() -> PersonaProfile | None
save_interests(list[Interest]) / list_interests() -> list[Interest]
upsert_life_arc(arc) / get_life_arc(id) / list_life_arcs(status=None) / update_life_arc_status(id, status)
save_agenda(day, DailyAgenda) / load_agenda(day) -> DailyAgenda | None
update_agenda_item_status(item_id, status) / list_agenda_items(day=None, status=None)
# memory tiers
save_session_summary(SessionSummary) / load_session_summary(session_id)
insert_episode(EpisodicMemory) -> id / get_episode(id)
list_episodes(limit=500, category=None)
touch_episode(id, t_h)                        # access_count += 1, last_accessed
save_embedding(episode_id, vector) / load_embeddings() -> list[(episode_id, vector)]
upsert_assertion(UserModelAssertion)          # supersedes same-key current by status flip
list_assertions(status="current") / get_assertion(key)
load_user_model() -> UserModel
# proactive
save_proactive_intent(intent) / load_proactive_intent(id)
list_proactive_intents(status=None) / update_proactive_intent_status(id, status)
resolve_intent_source(intent) -> AgendaItem | LifeArc | EpisodicMemory | None
# existing (keep working)
load_previous_judgement(day) -> float | None / latest_interaction_t_h() -> float | None
```

### persona seam (A6)

```python
# interests.py
class InterestGraph:
    add_relation(from_, to, strength) / neighbors(name) -> list[str]
    path_exists(a, b, max_hops=3) -> bool
    sample_exact(rng) / sample_adjacent(rng) / sample_independent(rng) -> str
# persona.py
def build_persona(seed: int, *, graph: InterestGraph, n_exact=4, n_adjacent=4,
                  n_independent=2, rng=None) -> PersonaProfile
```

### life seam (A4)

```python
def init_life(seed, persona: PersonaProfile, store, start_day=1) -> list[LifeArc]
def generate_agenda(day, persona, arcs, store, rng) -> DailyAgenda
def step_life(day, persona, arcs, agenda, store, rng) -> LifeStepResult
# LifeStepResult: updated_arcs: list[LifeArc]; agenda: DailyAgenda (statuses updated);
#                 current_activity: CurrentActivity | None
```

### memory seam (A5)

```python
class MemoryAgent:
    def __init__(self, store, *, embedder=None, policy=PromotionPolicy(), rng=None,
                 summarizer=None): ...
    def record_turn(self, role, text, t_h, session_id) -> None
    def close_session(self, session_id, *, ended_at_t_h) -> SessionSummary
    def promote(self, summary: SessionSummary) -> list[EpisodicMemory]
    def update_user_model(self, summary: SessionSummary) -> list[UserModelAssertion]
    def retrieve(self, query, *, context=None, limit=8) -> MemoryContext
# PromotionPolicy(importance_threshold=0.5, promote_emotional_peaks=True)
# score(q,j) = 0.35*sem + 0.30*strength + 0.35*importance; strength from age/access/importance/affect
```

### actuation seam (A3)

```python
# behavior.py: BehaviorDirective gains response_length_scale, response_delay_s,
#              closing_tendency (defaults); derive_behavior fills from channels.
# actuation.py:
def to_brief(directive) -> BehaviorBrief
def controls_from_directive(directive, *, base_max_tokens=600, min_tokens=96,
                            max_tokens=1500, beta=2.0) -> GenerationControls
# client.py: chat(..., max_tokens: int | None = None); FakeClient records last max_tokens.
```

### proactive seam (A7)

```python
# proactive.py
class IntentResolver:
    def __init__(self, store, *, rng=None): ...
    def resolve(self, opportunity_t_h: float) -> ProactiveIntent | None
#   None -> SUPPRESS: no_grounded_reason. Candidates: today's agenda (current/recent),
#   completed agenda items (life events), CALLBACK memories, shared-interest memories,
#   legitimate check-in context. Ranked by salience x recency x validity.
# gates.py: content_gate(intent, store) -> GateDecision  # source exists, not superseded,
#           timely, hook attached to that source.
# scheduler.py: next_pending fix (overdue visible at now==t; valid->evaluate/fire,
#               expired->expire). Plan CURRENT day only with scores + initiative.
#               h = h0*C*P*A(score_{d-1})*I(t). Never scores=None in live scheduling.
# runtime.py: delivery latency via injectable sleeper (default asyncio.sleep).
```

## 16. Worktree & merge protocol

- One worktree per track from clean main: `git worktree add -b wip/vslice-<track> ../llh-wt-<track> HEAD`.
- Workers commit on their branch with targeted `git add <owned-files>` (never `-A`), NEVER
  merge or touch main. Gate merges `--no-ff` in the plan's merge order; full suite after
  each gate; then `git worktree remove` the merged worktrees.
- Worktrees have no .venv: use the MAIN repo's `.venv/bin/python`; ALWAYS run pytest FROM
  THE WORKTREE ROOT (`python -m pytest` puts cwd first on sys.path, beating the editable
  install). For visible counts: `-o addopts="" -q` (repo addopts already has -q).
- Tracks that depend on A2's store for their module tests (A4, A5, A7) may
  `git merge wip/vslice-a2 --no-edit` inside their worktree (disjoint ownership, safe);
  fallback = seam-faithful fake store, reported to the orchestrator.
- Briefs: `/tmp/llh-vslice/{common,<track>}.md` restate ONLY the worker's seams VERBATIM +
  ownership + MUST-NOT-TOUCH + verify command + commit protocol. The plan file is the
  authoritative contract workers re-read.
- Verify commands: A2 `pytest tests/test_store.py tests/test_store_migrations.py`; A3
  `pytest tests/test_actuation.py tests/test_client.py`; A4 `pytest tests/test_life.py`;
  A5 `pytest tests/test_memory.py`; A6 `pytest tests/test_interests.py tests/test_persona.py`;
  A7 `pytest tests/test_scheduler.py tests/test_runtime.py tests/test_gates.py tests/test_proactive.py`.
