"""Small, reproducible comparison of the project memory and two OSS backends.

The benchmark deliberately keeps the semantic embedding fixed across backends.
The local llama.cpp server is used only for the extraction/generation step in
the Mem0 and Graphiti adapters; llama.cpp's server in this environment does
not expose /v1/embeddings.

Run one backend at a time because the project, Mem0, and Graphiti are being
tested in different Python environments::

    python experiments/memory_backends_benchmark.py --backend current
    python experiments/memory_backends_benchmark.py --backend mem0
    python experiments/memory_backends_benchmark.py --backend graphiti

Each command prints one JSON document.  The dataset is intentionally small:
eight facts followed by twenty filler sessions so that the project's recent
turn window cannot answer the questions by accident.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SERVER_BASE_URL = "http://127.0.0.1:8111/v1"
EMBED_DIM = 1024
EMBED_SEED = 0


@dataclass(frozen=True)
class FactCase:
    name: str
    text: str
    query: str
    expected: str
    t_h: float


CASES = (
    FactCase(
        "dog_name",
        "My dog's name is Bruno. We adopted him from a shelter last spring.",
        "What is my dog's name?",
        "bruno",
        24.0,
    ),
    FactCase(
        "pottery",
        "I take a pottery class on Tuesday evenings at the community studio.",
        "What recurring class do I take?",
        "pottery",
        48.0,
    ),
    FactCase(
        "sister_job",
        "My sister Maya works at Northwind Labs.",
        "Where does my sister work?",
        "northwind",
        72.0,
    ),
    FactCase(
        "callback",
        "Please remind me to water the orchids before Friday.",
        "What did I ask you to remind me about?",
        "orchids",
        96.0,
    ),
    FactCase(
        "old_home",
        "I live in Austin.",
        "Where did I live before moving?",
        "austin",
        120.0,
    ),
    FactCase(
        "new_home",
        "I moved to Denver last month, so I live in Denver now.",
        "Where do I live now?",
        "denver",
        144.0,
    ),
    FactCase(
        "old_music",
        "I used to listen to metal music every day.",
        "What music did I used to listen to?",
        "metal",
        168.0,
    ),
    FactCase(
        "new_music",
        "These days I barely listen to metal anymore; I mostly play jazz.",
        "What music do I mostly listen to now?",
        "jazz",
        192.0,
    ),
)

FILLER_TEXT = (
    "We chatted about a quiet afternoon and a cup of tea. "
    "There was no new personal fact to save."
)

QUERIES = tuple((c.name, c.query, c.expected) for c in CASES) + (
    ("negative_unknown_color", "What is my favorite color?", ""),
)


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def fixed_embedding(text: str, *, dim: int = EMBED_DIM, seed: int = EMBED_SEED) -> list[float]:
    """The same signed SHA-256 feature hash used by harness.embeddings."""

    vec = [0.0] * dim
    for token in _TOKEN_RE.findall(text.lower()):
        digest = hashlib.sha256(f"{seed}:{token}".encode("utf-8")).digest()
        idx = int.from_bytes(digest[:8], "little") % dim
        vec[idx] += 1.0 if digest[8] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


class FixedEmbedder:
    """Mem0-compatible fixed embedding adapter."""

    def embed(self, text: str, action: str = "search") -> list[float]:
        return fixed_embedding(str(text))

    def embed_batch(self, texts: list[str], action: str = "add") -> list[list[float]]:
        return [fixed_embedding(str(text)) for text in texts]


def _contains(text: str, expected: str) -> bool:
    return bool(expected) and expected.lower() in text.lower()


def _evaluate(
    backend: str,
    query_fn: Callable[[str], list[str]],
    *,
    ingest_seconds: float,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    query_start = time.perf_counter()
    for name, query, expected in QUERIES:
        ranked = [str(x) for x in query_fn(query)]
        joined = "\n".join(ranked[:3])
        rows.append(
            {
                "name": name,
                "query": query,
                "expected": expected,
                "top1": bool(ranked and _contains(ranked[0], expected)),
                "top3": _contains(joined, expected),
                "top8": _contains("\n".join(ranked[:8]), expected),
                "returned": len(ranked),
                "results": ranked[:3],
            }
        )
    query_seconds = time.perf_counter() - query_start
    positive = [r for r in rows if r["expected"]]
    unknown = next(r for r in rows if not r["expected"])
    result: dict[str, Any] = {
        "backend": backend,
        "embedding": {
            "type": "fixed_sha256_feature_hash",
            "dim": EMBED_DIM,
            "seed": EMBED_SEED,
            "llama_embeddings_endpoint": False,
        },
        "metrics": {
            "positive_cases": len(positive),
            "top1_recall": sum(r["top1"] for r in positive) / len(positive),
            "top3_recall": sum(r["top3"] for r in positive) / len(positive),
            "top8_recall": sum(r["top8"] for r in positive) / len(positive),
            "unknown_query_returned": unknown["returned"],
            "unknown_query_mentions_expected": bool(unknown["results"]),
            "ingest_seconds": round(ingest_seconds, 3),
            "query_seconds": round(query_seconds, 3),
        },
        "cases": rows,
    }
    if metadata:
        result["metadata"] = metadata
    return result


def _run_current() -> dict[str, Any]:
    from harness.embeddings import DeterministicHashEmbedder
    from harness.memory import MemoryAgent
    from harness.store import SQLiteStore

    with tempfile.TemporaryDirectory(prefix="memory-benchmark-current-") as temp_dir:
        store = SQLiteStore(Path(temp_dir) / "memory.sqlite3")
        try:
            agent = MemoryAgent(
                store,
                embedder=DeterministicHashEmbedder(dim=EMBED_DIM, seed=EMBED_SEED),
            )
            ingest_start = time.perf_counter()
            records = list(CASES) + [
                FactCase(f"filler_{i}", FILLER_TEXT, "", "", 216.0 + i * 24.0)
                for i in range(20)
            ]
            for day, item in enumerate(records, start=1):
                session_id = f"day-{day}"
                store.open_session(session_id, item.t_h)
                agent.record_turn("user", item.text, item.t_h, session_id)
                store.save_judgement(day, 0.9, "benchmark", "benchmark", True)
                summary = agent.close_session(session_id, ended_at_t_h=item.t_h + 1.0)
                store.close_session(session_id, item.t_h + 1.0)
                agent.promote(summary)
                agent.update_user_model(summary)
            ingest_seconds = time.perf_counter() - ingest_start

            def query_fn(query: str) -> list[str]:
                context = agent.retrieve(query, context={"t_h": 720.0}, limit=8)
                ranked: list[str] = []
                ranked.extend(ep.summary for ep in context.episodes)
                ranked.extend(context.evidence_anchors)
                if context.user_model is not None:
                    for field in (
                        "identity",
                        "stable_preferences",
                        "current_preferences",
                        "boundaries",
                        "vulnerabilities",
                        "recurring_interests",
                        "relationship_patterns",
                        "important_entities",
                    ):
                        value = getattr(context.user_model, field, None)
                        if isinstance(value, str):
                            ranked.append(value)
                        elif value:
                            ranked.extend(
                                f"{getattr(a, 'key', '')}: {getattr(a, 'value', a)}"
                                for a in value
                            )
                return ranked

            return _evaluate(
                "project_structured_memory",
                query_fn,
                ingest_seconds=ingest_seconds,
                metadata={
                    "pipeline": "L1 turns -> L2 summaries -> L3 episodes -> L4 assertions",
                    "retrieval": "0.35 semantic + 0.30 strength + 0.35 importance",
                    "filler_sessions": 20,
                },
            )
        finally:
            store.close()


def _run_mem0() -> dict[str, Any]:
    from mem0 import Memory

    with tempfile.TemporaryDirectory(prefix="memory-benchmark-mem0-") as temp_dir:
        root = Path(temp_dir)
        config = {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": "local",
                    "api_key": "local",
                    "openai_base_url": SERVER_BASE_URL,
                    "temperature": 0.0,
                    "max_tokens": 512,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "local",
                    "api_key": "local",
                    "openai_base_url": SERVER_BASE_URL,
                },
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "memory_backend_benchmark",
                    "path": str(root / "qdrant"),
                    "embedding_model_dims": EMBED_DIM,
                },
            },
            "history_db_path": str(root / "history.sqlite3"),
        }
        memory = Memory.from_config(config)
        # All vectors use the same fixed embedder as the project and the Graphiti adapter.
        memory.embedding_model = FixedEmbedder()
        ingest_start = time.perf_counter()
        added: list[dict[str, Any]] = []
        for item in CASES:
            added.append(
                memory.add(
                    [{"role": "user", "content": item.text}],
                    user_id="benchmark-user",
                    infer=True,
                )
            )
        # One extra call adds retrieval-distance pressure without more extraction calls.
        memory.add(
            [{"role": "user", "content": FILLER_TEXT}],
            user_id="benchmark-user",
            infer=True,
        )
        ingest_seconds = time.perf_counter() - ingest_start

        def query_fn(query: str) -> list[str]:
            rows = memory.search(
                query,
                filters={"user_id": "benchmark-user"},
                top_k=8,
                threshold=0.0,
                rerank=False,
            )["results"]
            return [str(row.get("memory", row)) for row in rows]

        result = _evaluate(
            "mem0_oss",
            query_fn,
            ingest_seconds=ingest_seconds,
            metadata={
                "package": "mem0ai",
                "pipeline": "LLM-extracted memories -> Qdrant local -> SQLite history",
                "extraction_calls": len(CASES) + 1,
                "added_batches": added,
            },
        )
        memory.close()
        try:
            memory.vector_store.client.close()
        except Exception:
            pass
        return result


async def _run_graphiti() -> dict[str, Any]:
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.graphiti import Graphiti
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from graphiti_core.nodes import EpisodeType
    from redislite.async_falkordb_client import AsyncFalkorDB

    class FixedGraphitiEmbedder(EmbedderClient):
        async def create(self, input_data: Any) -> list[float]:
            if isinstance(input_data, str):
                return fixed_embedding(input_data)
            return fixed_embedding(" ".join(map(str, input_data)))

        async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
            return [fixed_embedding(text) for text in input_data_list]

    class FixedCrossEncoder(CrossEncoderClient):
        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            # Deterministic lexical reranker; avoids a second LLM call.
            query_tokens = set(_TOKEN_RE.findall(query.lower()))
            scored = []
            for passage in passages:
                passage_tokens = set(_TOKEN_RE.findall(passage.lower()))
                score = len(query_tokens & passage_tokens) / max(len(query_tokens), 1)
                scored.append((passage, float(score)))
            return sorted(scored, key=lambda item: item[1], reverse=True)

    with tempfile.TemporaryDirectory(prefix="memory-benchmark-graphiti-") as temp_dir:
        db_file = str(Path(temp_dir) / "falkordb.db")
        async_db = AsyncFalkorDB(dbfilename=db_file)
        driver = FalkorDriver(falkor_db=async_db, database="memory_backend_benchmark")
        llm_config = LLMConfig(
            api_key="local",
            model="local",
            small_model="local",
            base_url=SERVER_BASE_URL,
            temperature=0.0,
            max_tokens=512,
        )
        llm = OpenAIGenericClient(
            config=llm_config,
            max_tokens=512,
            structured_output_mode="json_schema",
        )
        graphiti = Graphiti(
            graph_driver=driver,
            llm_client=llm,
            embedder=FixedGraphitiEmbedder(),
            cross_encoder=FixedCrossEncoder(),
            max_coroutines=1,
        )
        try:
            await graphiti.build_indices_and_constraints(delete_existing=True)
            ingest_start = time.perf_counter()
            for index, item in enumerate(CASES):
                await graphiti.add_episode(
                    name=f"benchmark-{index}-{item.name}",
                    episode_body=item.text,
                    source_description="memory backend benchmark",
                    reference_time=datetime.fromtimestamp(item.t_h * 3600, tz=timezone.utc),
                    source=EpisodeType.message,
                    group_id="benchmark-user",
                )
            await graphiti.add_episode(
                name="benchmark-filler",
                episode_body=FILLER_TEXT,
                source_description="memory backend benchmark",
                reference_time=datetime.fromtimestamp(720 * 3600, tz=timezone.utc),
                source=EpisodeType.message,
                group_id="benchmark-user",
            )
            ingest_seconds = time.perf_counter() - ingest_start

            async def query_fn_async(query: str) -> list[str]:
                edges = await graphiti.search(
                    query,
                    group_ids=["benchmark-user"],
                    num_results=8,
                )
                return [str(getattr(edge, "fact", edge)) for edge in edges]

            async def evaluate_graphiti() -> dict[str, Any]:
                query_start = time.perf_counter()
                rows: list[dict[str, Any]] = []
                for name, query, expected in QUERIES:
                    ranked = await query_fn_async(query)
                    joined = "\n".join(ranked[:3])
                    rows.append(
                        {
                            "name": name,
                            "query": query,
                            "expected": expected,
                            "top1": bool(ranked and _contains(ranked[0], expected)),
                            "top3": _contains(joined, expected),
                            "top8": _contains("\n".join(ranked[:8]), expected),
                            "returned": len(ranked),
                            "results": ranked[:3],
                        }
                    )
                query_seconds = time.perf_counter() - query_start
                positive = [r for r in rows if r["expected"]]
                unknown = next(r for r in rows if not r["expected"])
                return {
                    "backend": "graphiti_core",
                    "embedding": {
                        "type": "fixed_sha256_feature_hash",
                        "dim": EMBED_DIM,
                        "seed": EMBED_SEED,
                        "llama_embeddings_endpoint": False,
                    },
                    "metrics": {
                        "positive_cases": len(positive),
                        "top1_recall": sum(r["top1"] for r in positive) / len(positive),
                        "top3_recall": sum(r["top3"] for r in positive) / len(positive),
                        "top8_recall": sum(r["top8"] for r in positive) / len(positive),
                        "unknown_query_returned": unknown["returned"],
                        "unknown_query_mentions_expected": bool(unknown["results"]),
                        "ingest_seconds": round(ingest_seconds, 3),
                        "query_seconds": round(query_seconds, 3),
                    },
                    "cases": rows,
                    "metadata": {
                        "package": "graphiti-core",
                        "pipeline": "LLM-extracted entities/edges -> embedded FalkorDB -> hybrid graph search",
                        "extraction_calls": len(CASES) + 1,
                        "graph_database": "FalkorDB Lite",
                    },
                }

            return await evaluate_graphiti()
        finally:
            await graphiti.close()
            await async_db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("current", "mem0", "graphiti"), required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.backend == "current":
        result = _run_current()
    elif args.backend == "mem0":
        result = _run_mem0()
    else:
        import asyncio

        result = asyncio.run(_run_graphiti())

    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
