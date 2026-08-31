"""WS-D batteries: usage capture (client), v8 migration, pricing/report.

Covers the brief's verification batteries:

- Client battery: usage parsed and attached for every cache-field variant
  (DeepSeek flat hit/miss, OpenAI prompt_tokens_details.cached_tokens,
  Anthropic cache_read/creation), absent usage tolerated (None, no crash),
  lane attribution carried, gateway raw_cost captured.
- Migration battery: fresh DB reaches v8 with exactly one version row;
  a genuine v7 database (built with the store's own v1..v7 chain, stamped
  7) migrates additively — legacy llm_calls rows stay NULL, no data loss,
  idempotent re-open.
- Session end-to-end: a scripted FakeClient usage object + lane rides
  through Session._chat into the llm_calls row (lane attribution).
- Pricing/report battery: a hand-constructed few-call table priced by
  hand equals aggregate()/render_report() output — tiered cost math,
  cache-hit rate, cache savings, per-lane/model/day grouping, unpriced
  rows, pricing-pending banner, gateway raw_cost cross-check.

Uses placeholder rates from harness.pricing (PRICING_PENDING) — the math
is verified against those numbers, which is exactly what the user will
re-verify when they fill real rates.
"""

from __future__ import annotations

import sqlite3

import pytest

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient, Usage
from harness.clock import VirtualClock
from harness.judge import ScriptedJudge
from harness.pricing import MODELS, PRICING_PENDING, price_for
from harness.session import Session
from harness.spend import aggregate, aggregate_by, render_report
from harness.store import (
    SCHEMA_VERSION,
    SQLiteStore,
    _SCHEMA,
    _migrate_v2,
    _migrate_v3,
    _migrate_v4,
    _migrate_v5,
    _migrate_v6,
    _migrate_v7,
    schema_meta,
)

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 4242

RATES = MODELS["deepseek-v4-flash"]
IN_R = RATES["input_per_mtok"]
CACHED_R = RATES["cached_input_per_mtok"]
OUT_R = RATES["output_per_mtok"]


# Client battery: usage parsing across cache-field variants


def test_client_deepseek_flat_cache_variant():
    client = FakeClient(responses=[
        {"content": "ok", "usage": {
            "prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125,
            "prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 40,
        }},
    ])
    result = client.chat_with_meta([{"role": "user", "content": "hi"}])
    assert result.usage == Usage(100, 25, 125, 60, 40)


def test_client_openai_details_cached_tokens():
    client = FakeClient(responses=[
        {"content": "ok", "usage": {
            "prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125,
            "prompt_tokens_details": {"cached_tokens": 60},
        }},
    ])
    result = client.chat_with_meta([{"role": "user", "content": "hi"}])
    # miss derived: prompt - cached
    assert result.usage == Usage(100, 25, 125, 60, 40)


def test_client_openai_empty_details_is_all_miss():
    """The real gateway surfaces cached_tokens ONLY when a prefix is
    cached; an empty prompt_tokens_details means all-miss (probe 2026-08-16)."""
    client = FakeClient(responses=[
        {"content": "ok", "usage": {
            "prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125,
            "prompt_tokens_details": {},
        }},
    ])
    result = client.chat_with_meta([{"role": "user", "content": "hi"}])
    assert result.usage == Usage(100, 25, 125, None, 100)


def test_client_anthropic_read_and_creation_variant():
    client = FakeClient(responses=[
        {"content": "ok", "usage": {
            "prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125,
            "cache_read_input_tokens": 60, "cache_creation_input_tokens": 10,
        }},
    ])
    result = client.chat_with_meta([{"role": "user", "content": "hi"}])
    # reads are cache-served; creation writes are full-price -> miss bucket
    assert result.usage == Usage(100, 25, 125, 60, 10)


def test_client_absent_usage_tolerated():
    client = FakeClient(responses=[{"content": "ok"}])
    result = client.chat_with_meta([{"role": "user", "content": "hi"}])
    assert result.usage is None
    assert result.raw_cost is None


def test_client_malformed_usage_tolerated():
    client = FakeClient(responses=[
        {"content": "ok", "usage": "not-a-dict"},
        {"content": "ok", "usage": {"prompt_tokens": "junk", "total_tokens": 7}},
    ])
    r1 = client.chat_with_meta([{"role": "user", "content": "hi"}])
    assert r1.usage is None
    r2 = client.chat_with_meta([{"role": "user", "content": "hi"}])
    # junk prompt ignored, present total kept
    assert r2.usage == Usage(None, None, 7, None, None)


def test_client_raw_cost_captured():
    client = FakeClient(responses=[{"content": "ok", "cost": 0.00123}])
    result = client.chat_with_meta([{"role": "user", "content": "hi"}])
    assert result.raw_cost == pytest.approx(0.00123)


def test_client_lane_stamp_carried():
    client = FakeClient(lane="research")
    assert client.lane == "research"
    client2 = FakeClient(lane="product")
    assert client2.lane == "product"
    assert FakeClient().lane is None


# Store battery: usage/lane/model/raw_cost persistence


def test_log_llm_call_persists_usage_lane_model_raw_cost(tmp_path):
    store = SQLiteStore(tmp_path / "u.db")
    cid = store.log_llm_call(
        0, 1.0, "chat", "p", "r", "deepseek-v4-flash",
        usage=Usage(100, 25, 125, 60, 40),
        lane="research", raw_cost=0.0005,
    )
    row = store.get_llm_call(cid)
    assert row["prompt_tokens"] == 100
    assert row["completion_tokens"] == 25
    assert row["total_tokens"] == 125
    assert row["cached_tokens"] == 60
    assert row["cache_miss_tokens"] == 40
    assert row["lane"] == "research"
    assert row["model"] == "deepseek-v4-flash"
    assert row["raw_cost"] == pytest.approx(0.0005)

    # plain-dict usage accepted too
    cid2 = store.log_llm_call(
        0, 2.0, "chat", "p", "r", "deepseek-v4-flash",
        usage={"prompt_tokens": 10, "completion_tokens": 5,
               "total_tokens": 15, "cached_tokens": 0, "cache_miss_tokens": 10},
        lane="product",
    )
    row2 = store.get_llm_call(cid2)
    assert (row2["prompt_tokens"], row2["cache_miss_tokens"], row2["lane"]) == (10, 10, "product")
    store.close()


def test_log_llm_call_without_usage_leaves_columns_null(tmp_path):
    """Legacy call shape (no usage/lane kwargs): the v8 columns stay NULL —
    replay parity for pre-WS-D callers."""
    store = SQLiteStore(tmp_path / "l.db")
    cid = store.log_llm_call(0, 1.0, "chat", "p", "r", "fake-model")
    row = store.get_llm_call(cid)
    for col in ("prompt_tokens", "completion_tokens", "total_tokens",
                "cached_tokens", "cache_miss_tokens", "lane", "raw_cost"):
        assert row[col] is None, col
    assert row["response"] == "r" and row["model"] == "fake-model"
    store.close()


# Migration battery: v7 -> v8


def _build_v7_db(path) -> None:
    """A genuine v7 database via the store's own v1..v7 chain, stamped 7."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    con.executescript(schema_meta(SCHEMA_VERSION))
    _migrate_v2(con)
    _migrate_v3(con)
    _migrate_v4(con)
    _migrate_v5(con)
    _migrate_v6(con)
    _migrate_v7(con)
    con.execute("DELETE FROM schema_meta")
    con.execute("INSERT INTO schema_meta (version) VALUES (7)")
    con.commit()
    con.close()


def _seed_v7_llm_calls(path) -> int:
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO llm_calls (day, t_h, role, model, prompt_hash, response, meta) "
        "VALUES (0, 1.0, 'chat', 'deepseek-v4-flash', 'abc123', 'legacy reply', NULL)"
    )
    con.execute(
        "INSERT INTO llm_calls (day, t_h, role, model, prompt_hash, response, meta, "
        "repro_json) VALUES (1, 2.0, 'chat', 'fake', 'def456', 'legacy 2', NULL, '{}')"
    )
    con.execute(
        "INSERT INTO daily_state (day, M, m_level, g, p, arg, mu, eta, cycle_day, "
        "phase_label, seed, score) VALUES (0, 5, 2.5, 0.7, 0.5, 0.3, 0.1, 0.0, "
        "0.0, 'neutral', 12345, 0.5)"
    )
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    con.close()
    return n


def test_fresh_db_reaches_v8_with_one_version_row(tmp_path):
    store = SQLiteStore(tmp_path / "fresh.db")
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION == 8
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(llm_calls)")}
    for col in ("prompt_tokens", "completion_tokens", "total_tokens",
                "cached_tokens", "cache_miss_tokens", "lane", "raw_cost"):
        assert col in cols, col
    store.close()


def test_v7_db_migrates_additively_legacy_rows_null(tmp_path):
    db = tmp_path / "v7.db"
    _build_v7_db(db)
    n_before = _seed_v7_llm_calls(db)

    store = SQLiteStore(db)
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION

    # legacy rows: data intact, v8 columns NULL
    legacy = store.conn.execute(
        "SELECT prompt_tokens, completion_tokens, total_tokens, cached_tokens, "
        "cache_miss_tokens, lane, raw_cost, model, response "
        "FROM llm_calls ORDER BY id"
    ).fetchall()
    assert len(legacy) == n_before
    for row in legacy:
        for col in ("prompt_tokens", "completion_tokens", "total_tokens",
                    "cached_tokens", "cache_miss_tokens", "lane", "raw_cost"):
            assert row[col] is None, col
    assert legacy[0]["response"] == "legacy reply"
    assert legacy[0]["model"] == "deepseek-v4-flash"
    # unrelated tables untouched
    assert store.load_daily_state(0)["M"] == 5
    store.close()

    # idempotent re-open: no duplicate version rows, data still there
    store2 = SQLiteStore(db)
    rows = store2.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION
    assert store2.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == n_before
    store2.close()


# Session end-to-end: usage + lane ride into the llm_calls row


def test_session_turn_persists_usage_and_lane(tmp_path):
    store = SQLiteStore(tmp_path / "e2e.db")
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(lane="research", responses=[
        {"content": "main reply", "usage": {
            "prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125,
            "prompt_tokens_details": {"cached_tokens": 60},
        }, "cost": 0.0005},
    ])
    session = Session(
        store, persona=PERSONA, timing=TIMING, variant=VARIANT, seed=SEED,
        client=client, clock=clock, judge=ScriptedJudge(score=0.5).judge_day,
    )
    result = session.on_message("hello")
    assert result.reply == "main reply"

    row = store.conn.execute(
        "SELECT * FROM llm_calls ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["role"] == "chat"
    assert row["prompt_tokens"] == 100
    assert row["completion_tokens"] == 25
    assert row["total_tokens"] == 125
    assert row["cached_tokens"] == 60
    assert row["cache_miss_tokens"] == 40
    assert row["lane"] == "research"
    assert row["raw_cost"] == pytest.approx(0.0005)
    store.close()


# Pricing/report battery: hand-computed table == script output

# Hand-built rows (deepseek-v4-flash placeholder rates)
_R1 = {"model": "deepseek-v4-flash", "prompt_tokens": 1000, "completion_tokens": 500,
       "total_tokens": 1500, "cached_tokens": 600, "cache_miss_tokens": 400,
       "lane": "product", "day": 0, "raw_cost": 0.0012}
_R2 = {"model": "deepseek-v4-flash", "prompt_tokens": 800, "completion_tokens": 300,
       "total_tokens": 1100, "cached_tokens": 0, "cache_miss_tokens": 800,
       "lane": "product", "day": 0}
_R3 = {"model": "deepseek-v4-flash", "prompt_tokens": 2000, "completion_tokens": 1000,
       "total_tokens": 3000, "cached_tokens": 1500, "cache_miss_tokens": 500,
       "lane": "research", "day": 1}
_R4 = {"model": None, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
       "cached_tokens": None, "cache_miss_tokens": None, "lane": None, "day": 1}

ROWS = [_R1, _R2, _R3, _R4]


def _cost(cached, miss, out):
    return cached / 1e6 * CACHED_R + miss / 1e6 * IN_R + out / 1e6 * OUT_R


def test_aggregate_matches_hand_computed_math():
    total = aggregate(ROWS)
    assert total.calls == 4
    assert total.priced_calls == 3 and total.unpriced_calls == 1
    assert total.prompt_tokens == 3810
    assert total.completion_tokens == 1805
    assert total.total_tokens == 5615
    assert total.cached_tokens == 2100 and total.cache_miss_tokens == 1700
    # cache-hit rate: 2100 / (2100 + 1700)
    assert total.cache_hit_rate == pytest.approx(2100 / 3800)
    # tiered cost by hand
    expect_cost = (
        _cost(600, 400, 500) + _cost(0, 800, 300) + _cost(1500, 500, 1000)
    )
    assert total.cost_usd == pytest.approx(expect_cost)
    # savings = uncached - actual = cached * (in - cached) / 1e6
    expect_savings = (600 + 1500) / 1e6 * (IN_R - CACHED_R)
    assert total.savings_usd == pytest.approx(expect_savings)
    assert total.uncached_cost_usd == pytest.approx(expect_cost + expect_savings)
    assert total.raw_cost_usd == pytest.approx(0.0012)


def test_grouping_by_lane_model_day():
    by_lane = aggregate_by(ROWS, lambda r: r.get("lane") or "unknown")
    # legacy/unknowable rows group under "unknown" (NULL lane)
    assert set(by_lane) == {"product", "research", "unknown"}
    assert by_lane["product"].calls == 2 and by_lane["research"].calls == 1
    assert by_lane["unknown"].calls == 1 and by_lane["unknown"].cost_usd == 0.0
    assert by_lane["product"].cost_usd == pytest.approx(_cost(600, 400, 500) + _cost(0, 800, 300))
    assert by_lane["research"].cost_usd == pytest.approx(_cost(1500, 500, 1000))

    by_model = aggregate_by(ROWS, lambda r: r.get("model") or "unknown")
    assert set(by_model) == {"deepseek-v4-flash", "unknown"}
    assert by_model["unknown"].calls == 1 and by_model["unknown"].cost_usd == 0.0
    assert by_model["deepseek-v4-flash"].priced_calls == 3

    by_day = aggregate_by(ROWS, lambda r: f"day {r['day']}")
    assert set(by_day) == {"day 0", "day 1"}
    assert by_day["day 0"].calls == 2 and by_day["day 1"].calls == 2


def test_render_report_contains_math_and_pending_banner(capsys):
    total = aggregate(ROWS)
    report = render_report(
        total,
        aggregate_by(ROWS, lambda r: r.get("lane") or "unknown"),
        aggregate_by(ROWS, lambda r: r.get("model") or "unknown"),
        aggregate_by(ROWS, lambda r: f"day {r['day']}"),
        window_desc="all time", pricing_pending=True,
    )
    assert "PRICING PENDING" in report
    assert "placeholder" in report.lower()
    assert "cache-hit rate" in report
    assert f"{total.cache_hit_rate * 100:.1f}%" in report
    assert f"${total.cost_usd:,.4f}" in report
    assert f"${total.savings_usd:,.4f}" in report
    assert "deepseek-v4-flash" in report
    assert "product (Lily live bot)" in report
    assert "gateway-reported cost" in report


def test_price_for_behavior():
    # PRICING_PENDING is now False (real OpenRouter rates filled 2026-08-20)
    rates = price_for("deepseek-v4-flash")
    assert rates is not None
    assert rates["cached_input_per_mtok"] == pytest.approx(rates["input_per_mtok"] / 5)
    # unknown-but-named model falls back to the default tier
    assert price_for("some-other-model")["input_per_mtok"] > 0
    # absent/blank model is unpriced
    assert price_for(None) is None
    assert price_for("") is None


def test_spend_cli_writes_report_file(tmp_path):
    store = SQLiteStore(tmp_path / "sp.db")
    store.log_llm_call(0, 1.0, "chat", "p", "r", "deepseek-v4-flash",
                       usage={"prompt_tokens": 1000, "completion_tokens": 500,
                              "total_tokens": 1500, "cached_tokens": 600,
                              "cache_miss_tokens": 400},
                       lane="product", raw_cost=0.0012)
    store.close()

    from harness.spend import main
    out = tmp_path / "report.txt"
    rc = main([str(tmp_path / "sp.db"), "--out", str(out), "--days", "30"])
    assert rc == 0
    text = out.read_text()
    assert "LLM SPEND REPORT" in text
    assert "calls logged        : 1" in text
    assert f"${_cost(600, 400, 500):,.4f}" in text
